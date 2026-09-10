#!/usr/bin/env python3
"""Register one Codex queue as a Claude Code peer, then forward messages."""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

MAX_FRAME_BYTES = 65536
MAX_MESSAGE_BYTES = 16384


def now_ms():
    return int(time.time() * 1000)


def require_text(value, label, limit=256):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("%s must be a non-empty exact string" % label)
    if "\x00" in value or len(value.encode()) > limit:
        raise ValueError("%s is too long or contains NUL" % label)
    return value


def private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
            info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("%s must be a private directory owned by this user" % path)
    os.chmod(path, 0o700)


def valid_socket_path(path, label):
    if not os.path.isabs(path) or "\x00" in path or "/../" in path or path.endswith("/.."):
        raise ValueError("%s must be an absolute path without '..'" % label)
    if len(path.encode()) > 100:
        raise ValueError("%s is too long for a Unix socket" % label)


def private_path(path, label):
    valid_socket_path(path, label)
    private_dir(os.path.dirname(path))
    if os.path.lexists(path):
        raise ValueError("%s already exists" % path)


def process_start(pid):
    result = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                            capture_output=True, text=True, check=False)
    value = result.stdout.strip()
    if result.returncode or not value:
        raise RuntimeError("cannot read this process start time")
    return value


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    info = os.fstat(fd)
    identity = info.st_dev, info.st_ino
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(data)
        os.chmod(path, 0o600)
        return identity
    except BaseException:
        unlink_if_same(path, identity)
        raise


def identity_of(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def unlink_if_same(path, identity):
    if identity is not None and identity_of(path) == identity:
        os.unlink(path)


def recv_frame(conn, data):
    while True:
        newline = data.find(b"\n")
        if newline >= 0:
            if newline == 0 or newline > MAX_FRAME_BYTES:
                return None, bytearray()
            raw = bytes(data[:newline])
            del data[:newline + 1]
            try:
                return json.loads(raw.decode("utf-8")), data
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None, bytearray()
        if len(data) > MAX_FRAME_BYTES:
            return None, bytearray()
        chunk = conn.recv(min(4096, MAX_FRAME_BYTES + 1 - len(data)))
        if not chunk:
            return None, bytearray()
        data.extend(chunk)


def valid_auth(frame, token):
    return (isinstance(frame, dict) and frame.get("type") == "auth" and
            set(frame) == {"type", "token"} and
            isinstance(frame.get("token"), str) and
            hmac.compare_digest(frame["token"], token))


def user_message(frame, session_id):
    if not isinstance(frame, dict) or frame.get("type") != "user":
        return None
    message = frame.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    msg_id = frame.get("msg_id")
    if (not isinstance(message, dict) or set(message) != {"role", "content"} or
            message.get("role") != "user" or not isinstance(content, str) or
            not isinstance(msg_id, str) or not msg_id or len(msg_id.encode()) > 256 or
            not isinstance(frame.get("from"), str) or not frame["from"].startswith("uds:") or
            len(frame["from"].encode()) > 256 or
            ("session_id" in frame and (not isinstance(frame["session_id"], str) or
                                       frame["session_id"] != session_id)) or
            frame.get("file_attachments") or not content or
            frame.get("priority") != "next" or len(content.encode()) > MAX_MESSAGE_BYTES):
        return None
    return content, msg_id, frame["from"]


class Peer:
    def __init__(self, args):
        self.args = args
        self.pid = os.getpid()
        self.socket_path = args.socket or "/tmp/cc-socks/%d.sock" % self.pid
        self.sessions = os.path.join(args.claude_home, "sessions")
        self.record = os.path.join(self.sessions, "%d.json" % self.pid)
        digest = hashlib.sha256(self.socket_path.encode()).hexdigest()
        self.key = os.path.join(self.sessions, "%d.%s.key" % (self.pid, digest))
        self.server = None
        self.socket_identity = None
        self.key_identity = None
        self.record_identity = None
        self.token = secrets.token_hex(16)
        self.session_id = str(uuid.uuid4())
        self.proc_start = args.proc_start or process_start(self.pid)
        self.pid_domain = "darwin" if sys.platform == "darwin" else sys.platform

    def start(self):
        private_dir(self.sessions)
        private_path(self.socket_path, "socket path")
        if os.path.lexists(self.record) or os.path.lexists(self.key):
            raise RuntimeError("peer files already exist for this process")
        timestamp = now_ms()
        record = {
            "pid": self.pid,
            "sessionId": self.session_id,
            "cwd": os.getcwd(),
            "startedAt": timestamp,
            "procStart": self.proc_start,
            "version": "codex-queue-proof",
            "peerProtocol": 1,
            "peerFeatures": ["reply_across_default_dirs"],
            "kind": "interactive",
            "entrypoint": "cli",
            "pidDomain": self.pid_domain,
            "messagingSocketPath": self.socket_path,
            "name": self.args.name,
            "nameSource": "user",
            "nameSince": timestamp,
            "status": "idle",
            "updatedAt": timestamp,
            "statusUpdatedAt": timestamp,
        }
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        self.socket_identity = identity_of(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.server.listen(8)
        self.key_identity = write_private(self.key, json.dumps({"peerToken": self.token,
                                                                 "procStart": self.proc_start,
                                                                 "pidDomain": self.pid_domain},
                                                                separators=(",", ":")) + "\n")
        self.record_identity = write_private(self.record,
                                             json.dumps(record, separators=(",", ":")) + "\n")

    def close(self):
        unlink_if_same(self.record, self.record_identity)
        if self.server is not None:
            self.server.close()
        unlink_if_same(self.socket_path, self.socket_identity)
        unlink_if_same(self.key, self.key_identity)

    def deliver(self, content):
        command = [self.args.codex, "queue", "--remote", self.args.remote,
                   "--thread", self.args.thread, "--message", content]
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=self.args.queue_timeout).returncode == 0

    def peer_token(self, socket_path):
        digest = hashlib.sha256(socket_path.encode()).hexdigest()
        suffix = ".%s.key" % digest
        matches = [entry.path for entry in os.scandir(self.sessions)
                   if entry.name.endswith(suffix)]
        if len(matches) != 1:
            return None
        info = os.lstat(matches[0])
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                info.st_uid != os.getuid() or info.st_mode & 0o077):
            return None
        try:
            with open(matches[0], encoding="utf-8") as source:
                key = json.load(source)
        except (OSError, json.JSONDecodeError):
            return None
        token = key.get("peerToken") if isinstance(key, dict) else None
        if (not isinstance(key, dict) or set(key) != {"peerToken", "procStart", "pidDomain"} or
                not isinstance(key.get("procStart"), str) or not key["procStart"] or
                not isinstance(key.get("pidDomain"), str) or not key["pidDomain"]):
            return None
        return token if isinstance(token, str) and token and len(token.encode()) <= 256 else None

    def receipt(self, reply_to, msg_id):
        path = reply_to[4:]
        try:
            valid_socket_path(path, "reply socket")
            info = os.lstat(os.path.dirname(path))
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                    info.st_uid != os.getuid() or info.st_mode & 0o077):
                return False
            info = os.lstat(path)
            if (not stat.S_ISSOCK(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                    info.st_uid != os.getuid() or info.st_mode & 0o077):
                return False
        except (OSError, ValueError):
            return False
        token = self.peer_token(path)
        if token is None:
            return False
        receipt = {"type": "control", "action": "peer_message_status",
                   "status": "delivered", "orig_msg_id": msg_id,
                   "from": "uds:" + self.socket_path}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as reply:
                reply.settimeout(10)
                reply.connect(path)
                reply.sendall((json.dumps({"type": "auth", "token": token},
                                          separators=(",", ":")) + "\n").encode())
                reply.sendall((json.dumps(receipt, separators=(",", ":")) + "\n").encode())
            return True
        except OSError:
            return False

    def serve(self):
        while True:
            conn, _ = self.server.accept()
            with conn:
                conn.settimeout(10)
                try:
                    frame, buffered = recv_frame(conn, bytearray())
                except OSError:
                    continue
                if not valid_auth(frame, self.token):
                    continue
                try:
                    frame, buffered = recv_frame(conn, buffered)
                except OSError:
                    continue
                parsed = user_message(frame, self.session_id)
                if parsed is None:
                    continue
                content, msg_id, reply_to = parsed
                try:
                    delivered = self.deliver(content)
                except (OSError, subprocess.SubprocessError):
                    delivered = False
                if delivered:
                    self.receipt(reply_to, msg_id)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True, help="Codex UUID or exact session name")
    parser.add_argument("--remote", required=True, help="Codex daemon unix:// endpoint")
    parser.add_argument("--name", help="Claude peer name, default: codex-<thread>")
    parser.add_argument("--codex", default="codex", help="Codex executable")
    parser.add_argument("--claude-home", default=os.path.expanduser("~/.claude"))
    parser.add_argument("--socket", help="absolute peer socket path")
    parser.add_argument("--queue-timeout", type=float, default=30)
    parser.add_argument("--proc-start", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.thread = require_text(args.thread, "--thread")
    args.name = require_text(args.name or "codex-" + args.thread, "--name")
    if args.proc_start:
        args.proc_start = require_text(args.proc_start, "--proc-start")
    if args.remote != "unix://" and not args.remote.startswith("unix:///"):
        parser.error("--remote must be unix:// or unix:///absolute/path")
    if args.queue_timeout <= 0:
        parser.error("--queue-timeout must be positive")
    args.claude_home = os.path.abspath(args.claude_home)
    if args.socket:
        args.socket = os.path.abspath(args.socket)
    return args


def main():
    args = parse_args()
    peer = Peer(args)
    old_handlers = {}

    def stop(signum, frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.signal(signum, stop)
    try:
        peer.start()
        print(json.dumps({"session_id": peer.session_id, "name": args.name,
                          "socket": peer.socket_path}, separators=(",", ":")), flush=True)
        peer.serve()
    except KeyboardInterrupt:
        return 0
    finally:
        peer.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
