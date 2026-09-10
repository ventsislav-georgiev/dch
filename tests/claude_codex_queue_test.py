#!/usr/bin/env python3
"""Focused proof for claude_codex_queue.py. Run: python3 tests/claude_codex_queue_test.py"""
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "claude_codex_queue.py"


def frame(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def recv_frames(conn):
    data = bytearray()
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            return [json.loads(line) for line in data.splitlines()]
        data.extend(chunk)


def main():
    failures = []

    def check(label, condition):
        if condition:
            print("  ok   " + label)
        else:
            failures.append(label)
            print("  FAIL " + label)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "claude"
        sessions = home / "sessions"
        sessions.mkdir(parents=True, mode=0o700)
        socket_path = root / "peer.sock"
        reply_path = root / "reply.sock"
        log_path = root / "codex-args.json"
        fake = root / "codex"
        fake.write_text("#!%s\nimport json, os, sys\n"
                        "with open(os.environ['FAKE_CODEX_LOG'], 'a') as out: json.dump(sys.argv[1:], out); out.write('\\n')\n"
                        "raise SystemExit(1 if sys.argv[-1] == 'fail' else 0)\n" % sys.executable)
        fake.chmod(0o700)

        reply_token = "reply-test-token"
        digest = hashlib.sha256(str(reply_path).encode()).hexdigest()
        source_key = sessions / ("999.%s.key" % digest)
        source_key.write_text(json.dumps({"peerToken": reply_token,
                                          "procStart": "source-start",
                                          "pidDomain": "darwin"}) + "\n")
        source_key.chmod(0o600)
        reply = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        reply.bind(str(reply_path))
        os.chmod(reply_path, 0o600)
        reply.listen(1)
        received = []

        def receive_receipt():
            with reply.accept()[0] as conn:
                received.extend(recv_frames(conn))

        listener = threading.Thread(target=receive_receipt)
        listener.start()
        env = dict(os.environ, FAKE_CODEX_LOG=str(log_path))
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE), "--thread", "named-thread",
             "--remote", "unix://", "--codex", str(fake),
             "--claude-home", str(home), "--socket", str(socket_path),
             "--proc-start", "test-start"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            ready_fd, _, _ = select.select([proc.stdout], [], [], 5)
            check("bridge registers", bool(ready_fd))
            ready = json.loads(proc.stdout.readline()) if ready_fd else {}
            peer_record = sessions / ("%d.json" % proc.pid)
            peer_key = sessions / ("%d.%s.key" % (
                proc.pid, hashlib.sha256(str(socket_path).encode()).hexdigest()))
            record = json.loads(peer_record.read_text()) if peer_record.exists() else {}
            check("registration record names the peer",
                  record.get("sessionId") == ready.get("session_id") and
                  record.get("messagingSocketPath") == str(socket_path) and
                  record.get("peerProtocol") == 1 and record.get("procStart") == "test-start" and
                  record.get("peerFeatures") == ["reply_across_default_dirs"] and
                  record.get("name") == "codex-named-thread")
            check("published record has a ready socket and key",
                  peer_record.exists() and peer_key.exists() and socket_path.is_socket())
            check("record, key, and socket are owner-only",
                  peer_record.exists() and peer_key.exists() and socket_path.exists() and
                  all(stat.S_IMODE(path.stat().st_mode) == 0o600
                      for path in (peer_record, peer_key, socket_path)))
            published_key = json.loads(peer_key.read_text())
            peer_token = published_key.get("peerToken")
            check("registration key matches Claude's peer schema",
                  set(published_key) == {"peerToken", "procStart", "pidDomain"} and
                  isinstance(peer_token, str) and len(peer_token) == 32 and
                  all("0" <= char <= "9" or "a" <= char <= "f" for char in peer_token))

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bad:
                bad.connect(str(socket_path))
                bad.sendall(frame({"type": "auth", "token": "wrong"}))
                bad.shutdown(socket.SHUT_WR)
                check("bad auth is rejected", bad.recv(1) == b"")

            token = json.loads(peer_key.read_text())["peerToken"]
            def send_user(value):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
                    sender.connect(str(socket_path))
                    sender.sendall(frame({"type": "auth", "token": token}) + frame(value))
                    sender.shutdown(socket.SHUT_WR)

            native = {
                "type": "user", "message": {"role": "user", "content": "hello from claude"},
                "msg_id": "message-1", "from": "uds:" + str(reply_path), "priority": "next",
                "uuid": "8b1c02ea-0f3a-4e2f-9eb9-7727ac2d3198", "generated_at": 1234567890,
            }
            send_user(dict(native, session_id="other-session"))
            send_user(dict(native, msg_id=1))
            send_user(dict(native, priority="immediate"))
            send_user(dict(native, file_attachments=[{"name": "ignored.txt"}]))
            time.sleep(0.1)
            check("mismatched session id, malformed trusted fields, and attachments are rejected",
                  not log_path.exists())
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
                sender.connect(str(socket_path))
                sender.sendall(frame({"type": "auth", "token": token}) + frame(native))
                sender.shutdown(socket.SHUT_WR)
            listener.join(5)
            check("accepted delivery gets an authenticated receipt", len(received) == 2 and
                  received[0] == {"type": "auth", "token": reply_token} and
                  received[1].get("type") == "control" and
                  received[1].get("action") == "peer_message_status" and
                  received[1].get("status") == "delivered" and
                  received[1].get("orig_msg_id") == "message-1")
            check("Codex queue receives the native arguments",
                  log_path.exists() and json.loads(log_path.read_text()) == [
                      "queue", "--remote", "unix://", "--thread", "named-thread",
                      "--message", "hello from claude"])

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
                sender.connect(str(socket_path))
                sender.sendall(frame({"type": "auth", "token": token}) + frame({
                    "type": "user", "message": {"role": "user", "content": "fail"},
                    "msg_id": "message-2", "from": "uds:" + str(reply_path),
                    "session_id": ready["session_id"], "priority": "next"}))
                sender.shutdown(socket.SHUT_WR)
            reply.settimeout(1)
            try:
                reply.accept()[0].close()
                failed_receipt = True
            except socket.timeout:
                failed_receipt = False
            check("failed Codex queue has no delivery receipt", not failed_receipt)

            peer_record.unlink()
            peer_record.write_text("replacement\n")
            peer_record.chmod(0o600)
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            reply.close()
        check("cleanup preserves a replacement but removes owned artifacts",
              peer_record.read_text() == "replacement\n" and
              not peer_key.exists() and not socket_path.exists())

    if failures:
        return 1
    print("PASS: Claude peer registration forwards through codex queue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
