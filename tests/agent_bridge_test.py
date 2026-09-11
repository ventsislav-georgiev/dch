"""Native-agent bridge regression. Run with DCH=/path/to/dch python3 this-file."""

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

DCH = os.environ.get("DCH", str(Path(__file__).resolve().parents[1] / "dch"))
THREAD = "123e4567-e89b-12d3-a456-426614174000"
MAX_FRAME, MAX_MESSAGE = 65536, 16384
QUEUE_DEADLINE = 30


def wire(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


class Frames:
    def __init__(self, conn):
        self.conn, self.data = conn, bytearray()

    def read(self):
        while b"\n" not in self.data:
            if len(self.data) > MAX_FRAME:
                return None
            try:
                part = self.conn.recv(min(4096, MAX_FRAME + 1 - len(self.data)))
            except (OSError, socket.timeout):
                return None
            if not part:
                return None
            self.data.extend(part)
        line, _, rest = self.data.partition(b"\n")
        self.data = bytearray(rest)
        if not line or len(line) > MAX_FRAME:
            return None
        try:
            return json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


def frame_self_check():
    left, right = socket.socketpair()
    try:
        left.settimeout(1)
        right.sendall(
            wire({"type": "auth", "token": "a"}) + wire({"type": "user", "msg_id": "b"})
        )
        reader = Frames(left)
        malformed = b'{"content":"BAD_JSON"\n'
        invalid_utf8 = b'{"content":"BAD_UTF8_\xff"}\n'
        snowman = wire({"content": "snowman ☃"})
        return (
            reader.read() == {"type": "auth", "token": "a"}
            and reader.read() == {"type": "user", "msg_id": "b"}
            and malformed.endswith(b"\n")
            and b"\\n" not in malformed
            and invalid_utf8.endswith(b"\n")
            and b"\xff" in invalid_utf8
            and b"snowman \\u2603" in snowman
            and b"\xe2\x98\x83" not in snowman
        )
    finally:
        left.close()
        right.close()


def key_path(sessions, pid, path):
    return sessions / (
        "%s.%s.key" % (pid, hashlib.sha256(str(path).encode()).hexdigest())
    )


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def wait_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    return None


def proc_start(pid):
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=2,
            env=dict(os.environ, LC_ALL="C", TZ="UTC"),
        )
    except (OSError, PermissionError, subprocess.TimeoutExpired):
        return None
    return (
        result.stdout.strip()
        if result.returncode == 0 and result.stdout.strip()
        else None
    )


class ClaudePeer:
    def __init__(self, sessions, root, name, pid, start):
        self.sessions, self.name, self.pid, self.start = sessions, name, pid, start
        self.path, self.token = root / (name + ".sock"), name + "-token"
        self.key, self.record = key_path(sessions, pid, self.path), sessions / (
            str(pid) + ".json"
        )
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(4)
        os.chmod(self.path, 0o600)

    def publish(self, peer_name, name_source="user", bridge_marker=None):
        self.key.write_text(
            json.dumps(
                {
                    "peerToken": self.token,
                    "procStart": self.start,
                    "pidDomain": sys.platform,
                }
            )
            + "\n"
        )
        self.key.chmod(0o600)
        record = {
            "pid": self.pid,
            "sessionId": self.name + "-session",
            "procStart": self.start,
            "pidDomain": sys.platform,
            "peerProtocol": 1,
            "peerFeatures": ["reply_across_default_dirs"],
            "messagingSocketPath": str(self.path),
            "name": peer_name,
            "nameSource": name_source,
            "status": "idle",
        }
        if bridge_marker is not None:
            record["dchBridgeMarker"] = bridge_marker
        self.record.write_text(json.dumps(record) + "\n")
        self.record.chmod(0o644)

    def accept(self, timeout=5):
        self.server.settimeout(timeout)
        try:
            conn, _ = self.server.accept()
        except socket.timeout:
            return None
        with conn:
            conn.settimeout(2)
            reader = Frames(conn)
            return reader.read(), reader.read()

    def exchange(self, response, timeout=5):
        self.server.settimeout(timeout)
        conn, _ = self.server.accept()
        with conn:
            conn.settimeout(2)
            reader = Frames(conn)
            request = (reader.read(), reader.read())
            conn.sendall(wire(response))
            return request

    def receipt(
        self, source, msg_id, status="delivered", auth_token=None, from_path=None
    ):
        records = [
            json.loads(p.read_text())
            for p in self.sessions.glob("*.json")
            if p.is_file()
        ]
        source_record = next(
            r for r in records if r.get("messagingSocketPath") == str(source)
        )
        receipt_token = json.loads(
            key_path(self.sessions, source_record["pid"], source).read_text()
        )["peerToken"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect(str(source))
            conn.sendall(
                wire(
                    {
                        "type": "auth",
                        "token": receipt_token if auth_token is None else auth_token,
                    }
                )
                + wire(
                    {
                        "type": "control",
                        "action": "peer_message_status",
                        "status": status,
                        "orig_msg_id": msg_id,
                        "from": "uds:"
                        + str(self.path if from_path is None else from_path),
                    }
                )
            )

    def close(self):
        self.server.close()
        for path in (self.record, self.key, self.path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def native_send(path, token, payload=None, raw=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(str(path))
        conn.sendall(
            wire({"type": "auth", "token": token})
            + (raw if raw is not None else wire(payload))
        )


def receipt_for(peer, msg_id, expected_from, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        frame = peer.accept(min(1, end - time.monotonic()))
        if (
            frame
            and frame[0] == {"type": "auth", "token": peer.token}
            and frame[1]
            and frame[1].get("type") == "control"
            and frame[1].get("action") == "peer_message_status"
            and frame[1].get("orig_msg_id") == msg_id
            and frame[1].get("from") == "uds:" + str(expected_from)
        ):
            return frame
    return None


def process_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def queued_contents(path):
    prefix = "Untrusted peer content as a JSON string follows:\n"
    contents = []
    for line in path.read_text().splitlines() if path.exists() else []:
        argv = json.loads(line)
        if len(argv) == 5 and argv[:4] == ["queue", "--thread", THREAD, "--message"]:
            envelope = argv[4]
            if prefix in envelope:
                contents.append(json.loads(envelope.rsplit(prefix, 1)[1]))
    return contents


def bridge_core_self_check(root):
    source = Path(__file__).resolve().parents[1] / "bridge.c"
    binary = root / "bridge-core-self-check"
    built = subprocess.run(
        [
            "/usr/bin/cc",
            "-std=gnu11",
            "-W",
            "-Wall",
            "-I.",
            "-DDCH_BRIDGE_SELFTEST",
            str(source),
            "-o",
            str(binary),
        ],
        cwd=source.parent,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if built.returncode != 0:
        return False
    ran = subprocess.run([str(binary)], text=True, capture_output=True, timeout=5)
    return ran.returncode == 0 and ran.stdout.strip() == "bridge core self-check: ok"


def main():
    failures = []

    def check(label, condition):
        print("  %s %s" % ("ok  " if condition else "FAIL", label))
        failures.extend([] if condition else [label])

    check("coalesced auth and payload frames keep both lines", frame_self_check())
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at " + DCH)
        return 1
    with tempfile.TemporaryDirectory(
        prefix="dch-bridge-", dir=os.path.realpath("/tmp")
    ) as raw:
        root = Path(raw)
        check(
            "bridge core SHA, parser, Unicode, and bounds self-check",
            bridge_core_self_check(root),
        )
        home, runtime = root / "home", root / "runtime"
        socket_dir = runtime / "sockets"
        sessions = home / ".claude" / "sessions"
        sessions.mkdir(parents=True, mode=0o700)
        runtime.mkdir(mode=0o700)
        socket_dir.mkdir(mode=0o700)
        env = dict(
            os.environ,
            HOME=str(home),
            CODEX_HOME=str(home / "custom-codex-state"),
            CLAUDE_CONFIG_DIR=str(home / ".claude"),
            XDG_RUNTIME_DIR=str(runtime),
            DCH_SOCKET_DIR=str(socket_dir),
        )
        for inherited_identity in (
            "DCH_SESSION",
            "DCH_NATIVE_BRIDGE_ID",
            "DCH_BRIDGE_SIDECAR_PID",
        ):
            env.pop(inherited_identity, None)
        bridge_list = subprocess.run(
            [DCH, "--agent-list"], env=env, capture_output=True, timeout=3
        )
        check("native agent bridge is available", bridge_list.returncode == 0)
        if bridge_list.returncode != 0:
            return 1

        source_session = "fixture-source"
        source_marker = "%d.%s" % (os.getpid(), "a" * 32)
        source = ClaudePeer(
            sessions, root, "cli-source", os.getpid(), "fixture-source-start"
        )
        source.publish(source_session, bridge_marker=source_marker)
        source_env = dict(
            env,
            DCH_SESSION=source_session,
            DCH_NATIVE_BRIDGE_ID=source_marker,
        )

        def source_call(arguments, response):
            process = subprocess.Popen(
                [DCH, *arguments],
                env=source_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                request = source.exchange(response)
                stdout, stderr = process.communicate(timeout=5)
                return request, process.returncode, stdout, stderr
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(2)

        listed_peers = [
            {"name": "alpha", "session_id": "peer-alpha"},
            {"name": "beta", "session_id": "peer-beta"},
        ]
        try:
            request, returncode, stdout, _ = source_call(
                ["--agent-list", "--json"], listed_peers
            )
            check(
                "agent-list routes an authenticated exact dch_list through source",
                request
                == (
                    {"type": "auth", "token": source.token},
                    {"type": "dch_list"},
                )
                and returncode == 0
                and json.loads(stdout) == listed_peers,
            )
            plain_request, plain_code, plain_stdout, _ = source_call(
                ["--agent-list"], listed_peers
            )
            check(
                "plain agent-list prints exact peer names and newlines",
                plain_request
                == (
                    {"type": "auth", "token": source.token},
                    {"type": "dch_list"},
                )
                and plain_code == 0
                and plain_stdout == "alpha\nbeta\n",
            )
            error_request, error_code, _, _ = source_call(
                ["--agent-list", "--json"],
                {"status": "refused", "detail": "fixture list failure"},
            )
            check(
                "agent-list rejects a sidecar error object",
                error_request
                == (
                    {"type": "auth", "token": source.token},
                    {"type": "dch_list"},
                )
                and error_code != 0,
            )
            malformed_request, malformed_code, _, _ = source_call(
                ["--agent-list", "--json"],
                [{"name": False, "session_id": "peer-invalid"}],
            )
            check(
                "agent-list rejects malformed array elements",
                malformed_request
                == (
                    {"type": "auth", "token": source.token},
                    {"type": "dch_list"},
                )
                and malformed_code != 0,
            )
            send_request, send_code, _, _ = source_call(
                ["--agent-send", "alpha", "test-message"],
                {"status": "delivered", "detail": "peer accepted message"},
            )
            check(
                "agent-send routes exact dch_send through the same source",
                send_request
                == (
                    {"type": "auth", "token": source.token},
                    {
                        "type": "dch_send",
                        "target": "alpha",
                        "message": "test-message",
                    },
                )
                and send_code == 0,
            )
            held_request, held_code, held_stdout, _ = source_call(
                ["--agent-send", "alpha", "held-message"],
                {
                    "status": "held",
                    "detail": "peer accepted message for later delivery",
                },
            )
            check(
                "agent-send treats a held receipt as accepted pending work",
                held_request[1].get("message") == "held-message"
                and held_code == 0
                and held_stdout == "held\n",
            )
        finally:
            source.close()

        own_start = proc_start(os.getpid())
        if own_start is None:
            print("UNAVAILABLE: /bin/ps cannot provide process start identity")
            return 1
        log, child_log, hang_log, codex = (
            root / "queue.jsonl",
            root / "child.jsonl",
            root / "hang.pid",
            root / "codex",
        )
        codex.write_text("""#!%s
import json,os,pathlib,signal,sys
root=pathlib.Path(os.environ['CODEX_HOME'])/'shell_snapshots'
root.mkdir(parents=True,exist_ok=True)
if len(sys.argv)>1 and sys.argv[1]=='queue':
 open(os.environ['FAKE_CODEX_LOG'],'a').write(json.dumps(sys.argv[1:])+'\\n')
 if 'NOT_READY_TRAILING' in sys.argv[-1]:
  sys.stderr.write('Error: failed to queue session message: thread/queue/add failed: failed to read thread: invalid thread-store request: no rollout found for thread id '+sys.argv[3]+' (code -32603)\\n'+'x'*2500)
  raise SystemExit(1)
 if 'NOT_READY' in sys.argv[-1]:
  ready=pathlib.Path(os.environ['FAKE_READY_GATE'])
  if 'ALWAYS_NOT_READY' in sys.argv[-1] or not ready.exists():
   sys.stderr.write('Error: failed to queue session message: thread/queue/add failed: failed to read thread: invalid thread-store request: no rollout found for thread id '+sys.argv[3]+' (code -32603)\\n')
   raise SystemExit(1)
  open(os.environ['FAKE_READY_SUCCESS'],'a').write('success\\n')
 if 'HANG_QUEUE' in sys.argv[-1]:
  open(os.environ['FAKE_HANG_LOG'],'w').write(str(os.getpid()))
  signal.pause()
 raise SystemExit(1 if 'FAIL_QUEUE' in sys.argv[-1] else 0)
open(os.environ['FAKE_CHILD_LOG'],'a').write(json.dumps({'pid':os.getpid(),'argv':sys.argv[1:]})+'\\n')
m=os.environ.get('DCH_NATIVE_BRIDGE_ID','')
s=os.environ.get('DCH_SESSION','')
(root/'%s.100.sh').write_text('# shell snapshot\\nexport DCH_SESSION='+s+'\\nexport DCH_NATIVE_BRIDGE_ID='+m+'\\n')
(root/'123e4567-e89b-12d3-a456-426614174001.999.sh').write_text('# old\\nexport DCH_SESSION='+s+'\\nexport DCH_NATIVE_BRIDGE_ID=stale-marker\\n')
signal.pause()
""" % (sys.executable, THREAD))
        codex.chmod(0o700)
        env.update(
            FAKE_CODEX_LOG=str(log),
            FAKE_CHILD_LOG=str(child_log),
            FAKE_HANG_LOG=str(hang_log),
            FAKE_READY_GATE=str(root / "ready-gate"),
            FAKE_READY_SUCCESS=str(root / "ready-success"),
        )
        session, peers = "bridge-%d" % os.getpid(), []
        try:
            spawned = subprocess.run(
                [DCH, "--spawn", session, str(codex)],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            check("normal --spawn starts the Codex session", spawned.returncode == 0)
            check(
                "Codex child argv stays unchanged",
                wait_until(child_log.exists)
                and json.loads(child_log.read_text().splitlines()[0])["argv"] == [],
            )
            snapshot = (
                Path(env["CODEX_HOME"]) / "shell_snapshots" / (THREAD + ".100.sh")
            )
            check(
                "fake snapshot uses normal leading-comment format",
                wait_until(snapshot.exists) and snapshot.read_text().startswith("#"),
            )

            def bridge_record(except_pid=None):
                for path in sessions.glob("*.json"):
                    try:
                        value = json.loads(path.read_text())
                    except (OSError, json.JSONDecodeError):
                        continue
                    if (
                        value.get("name") == session
                        and value.get("peerProtocol") == 1
                        and value.get("pid") != except_pid
                    ):
                        return path, value
                return None

            published = wait_until(bridge_record)
            check(
                "spawn publishes one peer despite a different-marker snapshot",
                bool(published),
            )
            if not published:
                return 1
            record_path, record = published
            bridge_path = Path(record["messagingSocketPath"])
            bridge_key = key_path(sessions, record["pid"], bridge_path)
            token = (
                json.loads(bridge_key.read_text())["peerToken"]
                if bridge_key.exists()
                else ""
            )
            check(
                "bridge record has a safe real-record mode",
                mode(record_path) in (0o600, 0o644),
            )
            check(
                "record, SHA256 key, and private socket publish",
                record.get("name") == session
                and record.get("nameSource") == "user"
                and bridge_key.exists()
                and bridge_path.is_socket()
                and mode(bridge_key) == mode(bridge_path) == 0o600,
            )
            reply = ClaudePeer(sessions, root, "reply", os.getpid(), own_start)
            reply.publish("reply-peer", name_source="derived")
            peers.append(reply)
            listed = subprocess.run(
                [DCH, "--agent-list", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            check(
                "agent-list accepts a real 0644 derived-name Claude record",
                listed.returncode == 0
                and any(
                    x.get("name") == "reply-peer" for x in json.loads(listed.stdout)
                ),
            )
            base = {
                "type": "user",
                "message": {"role": "user", "content": "native nonce"},
                "msg_id": "native-1",
                "from": "uds:" + str(reply.path),
                "priority": "next",
            }
            native_send(
                bridge_path,
                token,
                dict(base, priority="future", peerFeatures=["future"]),
            )
            receipt = receipt_for(reply, "native-1", bridge_path)
            args = (
                json.loads(log.read_text().splitlines()[0])
                if wait_until(log.exists)
                else []
            )
            check(
                "authenticated unknown fields deliver exact original content",
                receipt
                and receipt[1].get("status") == "delivered"
                and len(args) == 5
                and args[:4] == ["queue", "--thread", THREAD, "--message"]
                and queued_contents(log) == ["native nonce"]
                and "--remote" not in args,
            )

            marker = (
                snapshot.read_text()
                .split("DCH_NATIVE_BRIDGE_ID=", 1)[1]
                .split("\n", 1)[0]
            )
            source_env = dict(env, DCH_SESSION=session, DCH_NATIVE_BRIDGE_ID=marker)
            wait_proc = subprocess.Popen(
                [sys.executable, "-c", "import signal\nsignal.pause()"]
            )
            wait_peer = ClaudePeer(
                sessions, root, "wait-target", wait_proc.pid, proc_start(wait_proc.pid)
            )
            wait_peer.publish("wait-target")
            peers.append(wait_peer)

            early = dict(
                base,
                msg_id="early",
                message={"role": "user", "content": "NOT_READY early"},
            )
            native_send(bridge_path, token, early)
            early_held = receipt_for(reply, "early", bridge_path)
            native_send(
                bridge_path,
                token,
                dict(
                    early,
                    message={"role": "user", "content": "changed duplicate"},
                ),
            )
            native_send(bridge_path, token, early)
            duplicate_held = receipt_for(reply, "early", bridge_path)
            queued = []
            for index in range(15):
                msg_id = "behind-%02d" % index
                content = "queued behind %02d" % index
                native_send(
                    bridge_path,
                    token,
                    dict(
                        base,
                        msg_id=msg_id,
                        message={"role": "user", "content": content},
                    ),
                )
                queued.append((msg_id, content, receipt_for(reply, msg_id, bridge_path)))
            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="overflow",
                    message={"role": "user", "content": "queue overflow"},
                ),
            )
            overflow = receipt_for(reply, "overflow", bridge_path)
            responsive = subprocess.run(
                [DCH, "--agent-list", "--json"],
                env=source_env,
                text=True,
                capture_output=True,
                timeout=2,
            )
            waiting_send = subprocess.Popen(
                [DCH, "--agent-send", "wait-target", "wait while queue retries"],
                env=source_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            waiting_outbound = wait_peer.accept()
            Path(env["FAKE_READY_GATE"]).touch()
            early_delivered = receipt_for(reply, "early", bridge_path, 8)
            queued_delivered = [
                receipt_for(reply, msg_id, bridge_path, 5)
                for msg_id, _, _ in queued
            ]
            if waiting_outbound:
                wait_peer.receipt(bridge_path, waiting_outbound[1]["msg_id"])
            waiting_stdout, _ = waiting_send.communicate(timeout=5)
            queued_contents_after = queued_contents(log)
            check(
                "bounded early queue coalesces duplicates and drains while local send waits",
                early_held
                and early_held[1].get("status") == "held"
                and duplicate_held
                and duplicate_held[1].get("status") == "held"
                and all(receipt and receipt[1].get("status") == "held" for _, _, receipt in queued)
                and overflow
                and overflow[1].get("status") == "refused"
                and responsive.returncode == 0
                and early_delivered
                and early_delivered[1].get("status") == "delivered"
                and all(receipt and receipt[1].get("status") == "delivered" for receipt in queued_delivered)
                and waiting_send.returncode == 0
                and waiting_stdout == "delivered\n"
                and queued_contents_after.count("NOT_READY early") >= 2
                and Path(env["FAKE_READY_SUCCESS"]).read_text().splitlines()
                == ["success"]
                and queued_contents_after[-15:] == [content for _, content, _ in queued]
                and "changed duplicate" not in queued_contents_after
                and "queue overflow" not in queued_contents_after,
            )

            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="pinned-old",
                    message={"role": "user", "content": "ALWAYS_NOT_READY pinned"},
                ),
            )
            pinned_held = receipt_for(reply, "pinned-old", bridge_path)
            switched_thread = "123e4567-e89b-12d3-a456-426614174003"
            switched_snapshot = snapshot.with_name(switched_thread + ".101.sh")
            snapshot_body = snapshot.read_text()
            switched_snapshot.write_text(snapshot_body)
            snapshot.unlink()
            pinned_refused = receipt_for(reply, "pinned-old", bridge_path, 8)
            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="new-thread",
                    message={"role": "user", "content": "new unique thread"},
                ),
            )
            new_thread_delivered = receipt_for(reply, "new-thread", bridge_path)
            queue_calls = [json.loads(line) for line in log.read_text().splitlines()]
            check(
                "pending UUID stays pinned when a different UUID becomes unique",
                pinned_held
                and pinned_held[1].get("status") == "held"
                and pinned_refused
                and pinned_refused[1].get("status") == "refused"
                and new_thread_delivered
                and new_thread_delivered[1].get("status") == "delivered"
                and sum("ALWAYS_NOT_READY pinned" in call[-1] for call in queue_calls)
                == 1
                and next(
                    call[2] for call in queue_calls if "ALWAYS_NOT_READY pinned" in call[-1]
                )
                == THREAD
                and next(
                    call[2] for call in queue_calls if "new unique thread" in call[-1]
                )
                == switched_thread,
            )
            switched_snapshot.unlink()
            snapshot.write_text(snapshot_body)

            baseline_contents = queued_contents(log)
            invalid = {
                "BAD_JSON": b'{"type":"user","message":{"role":"user","content":"BAD_JSON"},"msg_id":"bad-json","from":"uds:'
                + str(reply.path).encode()
                + b'","priority":"next",}\n',
                "BAD_DUPLICATE": b'{"type":"user","message":{"role":"user","content":"BAD_DUPLICATE"},"msg_id":"dupe","from":"uds:'
                + str(reply.path).encode()
                + b'","priority":"next","\\u006dsg_id":"dupe-2"}\n',
                "BAD_UTF8": b'{"type":"user","message":{"role":"user","content":"BAD_UTF8_\xff"},"msg_id":"utf8","from":"uds:'
                + str(reply.path).encode()
                + b'","priority":"next"}\n',
            }
            for raw_frame in invalid.values():
                native_send(bridge_path, token, raw=raw_frame)
            bad_payloads = [
                (
                    "BAD_TYPE",
                    dict(
                        base,
                        msg_id="bad-type",
                        type="control",
                        message={"role": "user", "content": "BAD_TYPE"},
                    ),
                ),
                (
                    "BAD_MIXED_TYPES",
                    dict(
                        base,
                        msg_id=4,
                        message={"role": "user", "content": "BAD_MIXED_TYPES"},
                    ),
                ),
                (
                    "BAD_ATTACHMENT",
                    dict(
                        base,
                        msg_id="attachment",
                        message={"role": "user", "content": "BAD_ATTACHMENT"},
                        attachments=[{}],
                    ),
                ),
                (
                    "BAD_OVERSIZE",
                    dict(
                        base,
                        msg_id="oversize",
                        message={
                            "role": "user",
                            "content": "BAD_OVERSIZE" + "x" * (MAX_MESSAGE + 1),
                        },
                    ),
                ),
                (
                    "BAD_SESSION",
                    dict(
                        base,
                        msg_id="bad-session",
                        message={"role": "user", "content": "BAD_SESSION"},
                        session_id="wrong",
                    ),
                ),
            ]
            for marker, bad in bad_payloads:
                invalid[marker] = b""
                native_send(bridge_path, token, bad)
            invalid["BAD_AUTH"] = b""
            native_send(
                bridge_path,
                "wrong",
                dict(
                    base,
                    msg_id="bad-auth",
                    message={"role": "user", "content": "BAD_AUTH"},
                ),
            )
            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="barrier",
                    message={"role": "user", "content": "VALID_BARRIER"},
                ),
            )
            barrier = receipt_for(reply, "barrier", bridge_path)
            contents = queued_contents(log)
            check(
                "every invalid frame is absent after a delivered barrier",
                barrier
                and barrier[1].get("status") == "delivered"
                and contents == baseline_contents + ["VALID_BARRIER"]
                and all(marker not in contents for marker in invalid),
            )

            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="unicode",
                    message={"role": "user", "content": "snowman ☃"},
                    metadata={"content": 4, "session_id": "wrong"},
                ),
            )
            unicode_receipt = receipt_for(reply, "unicode", bridge_path)
            check(
                "Unicode content and nested harmless metadata deliver",
                unicode_receipt
                and unicode_receipt[1].get("status") == "delivered"
                and queued_contents(log)[-1] == "snowman ☃",
            )
            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="native-refused",
                    message={"role": "user", "content": "FAIL_QUEUE"},
                ),
            )
            refused = receipt_for(reply, "native-refused", bridge_path)
            check(
                "queue failure returns an authenticated refusal",
                refused and refused[1].get("status") == "refused",
            )
            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="trailing-error",
                    message={"role": "user", "content": "NOT_READY_TRAILING"},
                ),
            )
            trailing = receipt_for(reply, "trailing-error", bridge_path)
            check(
                "readiness text with oversized trailing stderr is not retried",
                trailing and trailing[1].get("status") == "refused",
            )
            started = time.monotonic()
            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="hang",
                    message={"role": "user", "content": "HANG_QUEUE"},
                ),
            )
            hung = receipt_for(reply, "hang", bridge_path, QUEUE_DEADLINE + 5)
            hang_pid = int(hang_log.read_text()) if hang_log.exists() else 0
            check(
                "hung queue is killed and dropped at the documented deadline",
                hung
                and hung[1].get("status") == "dropped"
                and QUEUE_DEADLINE - 1
                <= time.monotonic() - started
                <= QUEUE_DEADLINE + 5
                and hang_pid
                and wait_until(lambda: process_gone(hang_pid), 3),
            )
            contents_after_failures = queued_contents(log)
            check(
                "permanent and uncertain queue failures are attempted once",
                contents_after_failures.count("FAIL_QUEUE") == 1
                and contents_after_failures.count("HANG_QUEUE") == 1
                and contents_after_failures.count("NOT_READY_TRAILING") == 1,
            )

            target_proc = subprocess.Popen(
                [sys.executable, "-c", "import signal\nsignal.pause()"]
            )
            target_start = proc_start(target_proc.pid)
            target = ClaudePeer(sessions, root, "target", target_proc.pid, target_start)
            target.publish("native-target")
            peers.append(target)
            target_list = subprocess.run(
                [DCH, "--agent-list", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            check(
                "agent-list sees a live target before reverse send",
                target_start
                and target_list.returncode == 0
                and any(
                    x.get("name") == "native-target"
                    for x in json.loads(target_list.stdout)
                ),
            )
            marker = (
                snapshot.read_text()
                .split("DCH_NATIVE_BRIDGE_ID=", 1)[1]
                .split("\n", 1)[0]
            )
            source_env = dict(env, DCH_SESSION=session, DCH_NATIVE_BRIDGE_ID=marker)
            send = subprocess.Popen(
                [DCH, "--agent-send", "native-target", "reverse nonce"],
                env=source_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            outbound = target.accept()
            check(
                "Codex CLI uses the persistent sidecar address",
                outbound
                and outbound[0] == {"type": "auth", "token": target.token}
                and outbound[1].get("from") == "uds:" + str(bridge_path)
                and outbound[1].get("message", {}).get("content") == "reverse nonce",
            )
            before_wait = queued_contents(log)
            if outbound:
                target.receipt(bridge_path, outbound[1]["msg_id"], auth_token="wrong")
                target.receipt(bridge_path, "wrong-id")
                target.receipt(bridge_path, outbound[1]["msg_id"], from_path=reply.path)
                native_send(
                    bridge_path,
                    token,
                    dict(
                        base,
                        msg_id="during-outbound",
                        message={
                            "role": "user",
                            "content": "DURING_OUTBOUND_BARRIER",
                        },
                    ),
                )
                during = receipt_for(reply, "during-outbound", bridge_path)
                check(
                    "native refusal barrier proves wrong receipts did not complete send",
                    during
                    and during[1].get("status") == "refused"
                    and send.poll() is None
                    and queued_contents(log) == before_wait,
                )
                target.receipt(bridge_path, outbound[1]["msg_id"])
            try:
                send.wait(5)
            except subprocess.TimeoutExpired:
                send.kill()
                send.wait(2)
            check("agent-send returns after matching receipt", send.returncode == 0)
            later_payload = dict(
                base,
                msg_id="later",
                message={"role": "user", "content": "later reply"},
                **{"from": "uds:" + str(target.path)}
            )
            native_send(bridge_path, token, later_payload)
            later = receipt_for(target, "later", bridge_path)
            check(
                "later native reply remains routable after CLI exit",
                later
                and later[1].get("status") == "delivered"
                and wait_until(lambda: "later reply" in log.read_text())
                and queued_contents(log) == before_wait + ["later reply"],
            )

            duplicate_proc = subprocess.Popen(
                [sys.executable, "-c", "import signal\nsignal.pause()"]
            )
            duplicate = ClaudePeer(
                sessions,
                root,
                "duplicate",
                duplicate_proc.pid,
                proc_start(duplicate_proc.pid),
            )
            duplicate.publish("native-target")
            peers.append(duplicate)
            ambiguous = subprocess.run(
                [DCH, "--agent-send", "native-target", "nope"],
                env=source_env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            check(
                "agent-send reports live exact-name ambiguity",
                ambiguous.returncode != 0 and "ambigu" in ambiguous.stderr.lower(),
            )
            duplicate.close()
            peers.remove(duplicate)
            duplicate_proc.terminate()
            duplicate_proc.wait(2)
            long_name = "n" * 511
            target.publish(long_name)
            accepted = subprocess.run(
                [DCH, "--agent-list", "--json"],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            overlong = subprocess.run(
                [DCH, "--agent-send", long_name + "x", "nope"],
                env=source_env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            check(
                "overlong name cannot truncate into an accepted 511-byte peer",
                any(x.get("name") == long_name for x in json.loads(accepted.stdout))
                and overlong.returncode != 0
                and target.accept(0.2) is None,
            )
            target.publish("native-target")
            conflict = snapshot.with_name("123e4567-e89b-12d3-a456-426614174002.888.sh")
            conflict.write_text(
                "# conflict\nexport DCH_SESSION=%s\nexport DCH_NATIVE_BRIDGE_ID=%s\n"
                % (session, marker)
            )
            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="conflict",
                    message={"role": "user", "content": "must not queue"},
                ),
            )
            conflict_receipt = receipt_for(reply, "conflict", bridge_path)
            check(
                "two current snapshot UUIDs refuse instead of choosing by mtime",
                conflict_receipt
                and conflict_receipt[1].get("status") == "refused"
                and conflict_receipt[1].get("orig_msg_id") == "conflict"
                and "must not queue" not in log.read_text(),
            )
            conflict.unlink()

            child_before = child_log.read_text()
            old_pid = record["pid"]
            native_send(
                bridge_path,
                token,
                dict(
                    base,
                    msg_id="restart-held",
                    message={"role": "user", "content": "ALWAYS_NOT_READY"},
                ),
            )
            restart_held = receipt_for(reply, "restart-held", bridge_path)
            incomplete = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            incomplete.settimeout(2)
            incomplete.connect(str(bridge_path))
            incomplete.sendall(
                wire({"type": "auth", "token": token}) + b'{"type":"user"'
            )
            try:
                restarted = subprocess.run(
                    [DCH, "--restart", session],
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
                replacement = wait_until(
                    lambda: (
                        bridge_record(old_pid)
                        if not record_path.exists()
                        and not bridge_key.exists()
                        and not bridge_path.exists()
                        else None
                    )
                )
                restart_dropped = receipt_for(reply, "restart-held", bridge_path)
                check(
                    "restart drops held work, interrupts a frame, and replaces sidecar",
                    restarted.returncode == 0
                    and restart_held
                    and restart_held[1].get("status") == "held"
                    and restart_dropped
                    and restart_dropped[1].get("status") == "dropped"
                    and replacement
                    and replacement[1]["pid"] != old_pid
                    and not record_path.exists()
                    and not bridge_key.exists()
                    and not bridge_path.exists()
                    and child_log.read_text() == child_before,
                )
            finally:
                incomplete.close()
            if replacement:
                current_path, current = replacement
                current_socket = Path(current["messagingSocketPath"])
                current_key = key_path(sessions, current["pid"], current_socket)
                replacement_file = current_path.with_suffix(".replacement")
                replacement_file.write_text("replacement\n")
                os.chmod(replacement_file, 0o644)
                os.replace(replacement_file, current_path)
                os.kill(current["pid"], signal.SIGTERM)
                check(
                    "sidecar termination preserves live replacement and PTY",
                    wait_until(
                        lambda: not current_key.exists() and not current_socket.exists()
                    )
                    and current_path.read_text() == "replacement\n"
                    and subprocess.run(
                        [DCH, "--status", session],
                        env=env,
                        capture_output=True,
                        timeout=5,
                    ).returncode
                    == 0,
                )
            subprocess.run(
                [DCH, "-k", session], env=env, capture_output=True, timeout=5
            )
            check(
                "owned cleanup preserves a replacement record",
                replacement and current_path.read_text() == "replacement\n",
            )
        finally:
            waiting = locals().get("waiting_send")
            if waiting and waiting.poll() is None:
                waiting.kill()
                try:
                    waiting.wait(2)
                except subprocess.TimeoutExpired:
                    pass
            pending = locals().get("send")
            if pending and pending.poll() is None:
                pending.kill()
                try:
                    pending.wait(2)
                except subprocess.TimeoutExpired:
                    pass
            try:
                subprocess.run(
                    [DCH, "-k", session], env=env, capture_output=True, timeout=5
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            for peer in peers:
                try:
                    peer.close()
                except OSError:
                    pass
            for proc in (
                locals().get("wait_proc"),
                locals().get("target_proc"),
                locals().get("duplicate_proc"),
            ):
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(2)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
