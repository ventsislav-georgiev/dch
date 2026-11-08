"""The dch client for remote hosts (#087): -ls, --ls-json/-lj --remote, -a.

A real `dch serve` runs on 127.0.0.1 (DCH_SERVE_TEST_LOOPBACK=1, --bind
127.0.0.1 --port 0, a fake `tailscale` first on PATH), its sessions in their
own socket dir; the client has another, and reaches the server only through
--hosts label=127.0.0.1:port. DCH_NO_REMOTE=1 on the client, so discovery
never runs except in the one check that feeds it a stub status and names an
unknown host (which dials nothing). Fake servers on loopback play a hostile
peer, a hanging one, a newer protocol and a link that drops.

Covers: remote rows in --ls and in --ls-json/-lj with --remote only; -a
host:name shows the screen, input round-trips, a resize arrives, the detach
leaves the session alive; --host <h> -a <name>; an exact local name wins; an
unknown host (and discovery's peer filter, by its known list); a hostile
server's control bytes escaped; a hanging host within the time box, then its
cached rows marked stale; a newer major skipped; a dropped link exits 1 with
a message and the terminal restored; 0x12 ends the attach with 0.

Run:  python3 tests/remote_client_test.py        (uses ./dch)
"""
import errno, fcntl, json, os, pty, re, select, shutil, signal, socket
import struct, subprocess, sys, tempfile, termios, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))

FAKE_TS = """#!/bin/sh
d=$(dirname "$0")
case "$1" in
status) [ -f "$d/status.json" ] && exec cat "$d/status.json"; exit 1 ;;
whois) [ -f "$d/whois.json" ] && exec cat "$d/whois.json"; exit 1 ;;
ip) [ -f "$d/ip.txt" ] && exec cat "$d/ip.txt"; exit 1 ;;
esac
exit 1
"""
OWNER = {"Self": {"ID": "1001", "UserID": 42, "Tags": []},
         "User": {"42": {"LoginName": "me@example.com"}}}
WHOIS = {"UserProfile": {"ID": 42, "LoginName": "me@example.com"},
         "Node": {"ID": 2002, "StableID": "2002", "Tags": []}}
# discovery's input: only "mybox" is the user's, online and untagged. None
# is ever dialled: the check names an unknown host.
PEERS = dict(OWNER, Peer={
    "k1": {"DNSName": "mybox.tail1.ts.net.", "HostName": "MyBox",
           "UserID": 42, "Online": True, "TailscaleIPs": ["100.64.9.1"]},
    "k2": {"DNSName": "theirs.tail1.ts.net.", "UserID": 7, "Online": True,
           "TailscaleIPs": ["100.64.9.2"]},
    "k3": {"DNSName": "server.tail1.ts.net.", "UserID": 42, "Online": True,
           "Tags": ["tag:server"], "TailscaleIPs": ["100.64.9.3"]},
    "k4": {"DNSName": "sleepy.tail1.ts.net.", "UserID": 42,
           "Online": False, "TailscaleIPs": ["100.64.9.4"]},
    "k5": {"DNSName": "noip.tail1.ts.net.", "UserID": 42, "Online": True,
           "TailscaleIPs": ["8.8.8.8"]}})


def frame(t, payload=b""):
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload).encode()
    return struct.pack(">BI", t, len(payload)) + payload


class Fake:
    """A remote-frames server on loopback that answers as `mode` says."""

    def __init__(self, mode, rows=None, proto="1.0"):
        self.mode, self.rows, self.proto = mode, rows or [], proto
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        self.attached = threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            if self.mode != "hang":
                threading.Thread(target=self.serve, args=(c,),
                                 daemon=True).start()

    def serve(self, c):
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    buf = buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": self.proto,
                                               "server": "fake"}))
                    elif t == 0x03:
                        c.sendall(frame(0x11, self.rows))
                    elif t == 0x01:
                        self.attached.set()
                        c.sendall(frame(0x10, b"FAKE-SCREEN\r\n"))
                        if self.mode == "drop":
                            time.sleep(0.3)
                            c.close()
                            return
                        if self.mode == "exit":
                            time.sleep(0.3)
                            c.sendall(frame(0x12, {"code": 0}))
                            c.shutdown(socket.SHUT_WR)
                            return
        except OSError:
            return

    def close(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchrc", dir="/tmp"))
    fake = os.path.join(tmp, "fake")
    os.makedirs(fake)
    with open(os.path.join(fake, "tailscale"), "w") as f:
        f.write(FAKE_TS)
    os.chmod(os.path.join(fake, "tailscale"), 0o755)

    def put(name, obj):
        with open(os.path.join(fake, name), "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    put("status.json", OWNER)
    put("whois.json", WHOIS)
    put("ip.txt", "100.100.1.1\n")
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("DCH_", "MallocStackLogging"))}

    def side(name, **kw):
        d = os.path.join(tmp, name)
        os.makedirs(os.path.join(d, "home"))
        e = dict(base, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=os.path.join(d, "st"),
                 HOME=os.path.join(d, "home"), DCH_LOGIN_ENV="0",
                 DCH_CONFIG=os.path.join(d, "config"), SHELL="/bin/sh",
                 PATH=fake + ":/usr/bin:/bin", TERM="xterm")
        e.update(kw)
        return e

    senv = side("srv")
    cenv = side("cli", DCH_NO_REMOTE="1", DCH_DOUBLE_TAP_MS="0")
    csock = os.path.join(tmp, "cli", "dch-%d" % os.getuid())
    procs, fakes, clients = [], [], []
    fails = []

    def check(msg, cond, extra=""):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)
            if extra:
                print("       " + str(extra).replace("\n", "\n       "))

    def dch(env, *args, timeout=20):
        return subprocess.run([DCH] + list(args), env=env, timeout=timeout,
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def read(fd, until, secs):
        out = b""
        end = time.time() + secs
        while time.time() < end and until not in out:
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd in r:
                try:
                    c = os.read(fd, 65536)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise
                if not c:
                    break
                out += c
        return out

    def attach(args, cols=100, rows=30):
        """dch <args> on its own pty of cols x rows: (pid, fd)."""
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(DCH, [DCH] + args, cenv)
            os._exit(127)
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        clients.append(pid)
        return pid, fd

    def reap(pid, fd, secs):
        end = time.time() + secs
        out = b""
        while time.time() < end:
            got, st = os.waitpid(pid, os.WNOHANG)
            if got:
                clients.remove(pid)
                return st, out + read(fd, b"\0never", 0.2)
            out += read(fd, b"\0never", 0.05)
        return None, out

    try:
        # the server and its session
        log = os.path.join(tmp, "serve.log")
        with open(log, "w") as lf:
            sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                                   "--port", "0"],
                                  env=dict(senv, DCH_SERVE_TEST_LOOPBACK="1"),
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=lf)
        procs.append(sp)
        port = None
        end = time.time() + 10
        while time.time() < end and port is None and sp.poll() is None:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(log).read())
            port = int(m.group(1)) if m else None
            time.sleep(0.05)
        check("dch serve listens on a loopback test port", port is not None,
              open(log).read())
        if port is None:
            return 1
        r = dch(senv, "--spawn", "rs1", "--size", "80x24", "--", "/bin/sh")
        check("a session on the server", r.returncode == 0, r.stderr)
        r = dch(cenv, "--spawn", "loc1", "--size", "80x24", "--", "/bin/sh")
        check("a local session on the client", r.returncode == 0, r.stderr)
        hosts = "srv=127.0.0.1:%d" % port

        # --- listing --------------------------------------------------
        r = dch(cenv, "--ls", "--hosts", hosts)
        lines = r.stdout.splitlines()
        check("--ls: the local session, then srv:rs1",
              r.returncode == 0 and "loc1" in lines and "srv:rs1" in lines
              and lines.index("loc1") < lines.index("srv:rs1"), r)
        r = dch(cenv, "--ls-json", "--hosts", hosts)
        rows = json.loads(r.stdout)
        check("--ls-json without --remote: local rows only",
              [x["name"] for x in rows] == ["loc1"]
              and all("host" not in x for x in rows), r.stdout)
        r = dch(cenv, "--ls-json", "--remote", "--hosts", hosts)
        rows = json.loads(r.stdout)
        rem = [x for x in rows if "host" in x]
        check("--ls-json --remote: a row with host srv and name rs1",
              len(rem) == 1 and rem[0]["host"] == "srv"
              and rem[0]["name"] == "rs1" and rem[0]["stale"] is False
              and "loc1" in [x["name"] for x in rows], r.stdout)
        r = dch(cenv, "-lj", "--hosts", hosts)
        check("-lj without --remote: no remote rows",
              "srv:" not in r.stdout and "loc1\t" in r.stdout, r.stdout)
        r = dch(cenv, "-lj", "--remote", "--hosts", hosts)
        check("-lj --remote: srv:rs1<TAB>alias<TAB>0",
              "srv:rs1\t\t0" in r.stdout.splitlines(), r.stdout)
        r = dch(cenv, "--remote", "-ls")
        check("--remote with -ls is refused", r.returncode == 1
              and "--remote only works" in r.stderr, r.stderr)
        r = dch(cenv, "-ls", "--hosts", "a b;c")
        check("a bad --hosts entry is refused", r.returncode == 1
              and "bad entry" in r.stderr, r.stderr)
        r = dch(cenv, "--host", "srv", "-ls")
        check("--host without -a is refused", r.returncode == 1, r.stderr)

        # --- attach ---------------------------------------------------
        pid, fd = attach(["--hosts", hosts, "-a", "srv:rs1"])
        out = read(fd, b"$ ", 8)
        check("-a srv:rs1 shows the remote screen", b"$ " in out, out)
        os.write(fd, b"echo rt$((6*7))\r")
        out = read(fd, b"rt42", 5)
        check("input round-trips", b"rt42" in out, out)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 111, 0, 0))
        os.kill(pid, signal.SIGWINCH)
        time.sleep(0.5)
        os.write(fd, b"stty size\r")
        out = read(fd, b"33 111", 5)
        check("a resize arrives", b"33 111" in out, out)
        os.write(fd, b"\x1c")
        st, out = reap(pid, fd, 5)
        check("the detach key detaches (exit 0)",
              st is not None and os.WIFEXITED(st)
              and os.WEXITSTATUS(st) == 0, (st, out))
        r = dch(senv, "-ls")
        check("the remote session lives on", "rs1" in r.stdout.split(),
              r.stdout)

        pid, fd = attach(["--hosts", hosts, "--host", "srv", "-a", "rs1"])
        out = read(fd, b"$ ", 8)
        os.write(fd, b"echo h$((2+3))\r")
        out += read(fd, b"h5", 5)
        os.write(fd, b"\x1c")
        st, _ = reap(pid, fd, 5)
        check("--host srv -a rs1 attaches", b"h5" in out and st is not None
              and os.WEXITSTATUS(st) == 0, out)

        # --- precedence -----------------------------------------------
        r = dch(cenv, "--spawn", "srv:rs1", "--size", "80x24", "--",
                "/bin/sh")
        check("a local session named srv:rs1", r.returncode == 0, r.stderr)
        # the remote is unreachable now: only a local attach can work
        pid, fd = attach(["--hosts", "srv=127.0.0.1:1", "-a", "srv:rs1"])
        read(fd, b"\0never", 1.5)
        marks = [f for f in os.listdir(csock)
                 if f.startswith("srv:rs1.sock.client.")]
        os.write(fd, b"\x1c")
        st, out = reap(pid, fd, 5)
        check("an exact local name wins", marks and st is not None
              and os.WEXITSTATUS(st) == 0, (marks, st, out))
        dch(cenv, "-k", "srv:rs1")

        r = dch(cenv, "--hosts", hosts, "-a", "nope:rs1")
        check("an unknown host: a clear error, exit 1", r.returncode == 1
              and "nope is not a known host" in r.stderr
              and "known: srv" in r.stderr, r.stderr)
        r = dch(cenv, "--hosts", hosts, "-a", "nosuch")
        check("-a never creates", r.returncode == 1
              and "no session: nosuch" in r.stderr
              and "nosuch" not in dch(cenv, "-ls").stdout, r.stderr)
        r = dch(cenv, "--hosts", hosts, "-a", "srv:gone")
        check("-a host:missing says so, creates nothing", r.returncode == 1
              and "no session gone on srv" in r.stderr
              and "gone" not in dch(senv, "-ls").stdout, r.stderr)
        put("status.json", PEERS)
        denv = dict(cenv)
        del denv["DCH_NO_REMOTE"]
        r = dch(denv, "-a", "nope:x")
        check("discovery: only the user's online untagged peers",
              r.returncode == 1 and "known: mybox)" in r.stderr, r.stderr)
        r = dch(dict(denv, DCH_NO_REMOTE="1"), "-a", "nope:x")
        check("DCH_NO_REMOTE=1: no hosts", r.returncode == 1
              and "remote hosts are off" in r.stderr, r.stderr)
        put("status.json", OWNER)

        # --- a hostile server -----------------------------------------
        evil = Fake("list", rows=[
            {"name": "ev\x1b]0;pwn\x07\x1b[31mred", "alias": "a\x1b[2Jb"},
            {"name": "tab\there\nnl", "state": "\x1b[5mworking"},
            {"no": "name"}, {"name": 5}, "junk"])
        fakes.append(evil)
        eh = "evil=127.0.0.1:%d" % evil.port
        r = dch(cenv, "--ls", "--hosts", eh)
        check("hostile names in --ls: no ESC, BEL, tab or newline reaches "
              "the terminal", r.returncode == 0 and "\x1b" not in r.stdout
              and "\x07" not in r.stdout and "\t" not in r.stdout
              and "evil:ev\\x1b]0;pwn" in r.stdout
              and "evil:tab\\x09here\\x0anl" in r.stdout, repr(r.stdout))
        r = dch(cenv, "-lj", "--remote", "--hosts", eh)
        check("hostile names in -lj: 3 fields a line, escaped",
              "\x1b" not in r.stdout and all(
                  len(l.split("\t")) == 3 for l in r.stdout.splitlines()),
              repr(r.stdout))
        r = dch(cenv, "--ls-json", "--remote", "--hosts", eh)
        rows = [x for x in json.loads(r.stdout) if "host" in x]
        check("hostile names in --ls-json: valid JSON, JSON-escaped, a bad "
              "state dropped", "\x1b" not in r.stdout and len(rows) == 2
              and rows[0]["name"].startswith("ev\x1b]")
              and all(x["state"] == "" for x in rows), r.stdout)

        # --- a hanging host, then its rows stale ----------------------
        hang = Fake("hang")
        fakes.append(hang)
        t0 = time.time()
        r = dch(cenv, "--ls", "--hosts",
                "%s,h=127.0.0.1:%d" % (hosts, hang.port))
        dt = time.time() - t0
        check("a hanging host does not stall --ls (%.2fs)" % dt,
              dt < 1.5 and "srv:rs1" in r.stdout.splitlines(), r)
        t0 = time.time()
        r = dch(cenv, "--ls", "--hosts", "srv=127.0.0.1:%d" % hang.port)
        dt = time.time() - t0
        check("then its cached rows show stale (%.2fs)" % dt,
              dt < 1.5 and "srv:rs1 [stale]" in r.stdout.splitlines(),
              r.stdout)
        r = dch(cenv, "--ls-json", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % hang.port)
        rem = [x for x in json.loads(r.stdout) if "host" in x]
        check("--ls-json marks them stale", rem and rem[0]["stale"] is True,
              r.stdout)

        # --- a newer protocol -----------------------------------------
        newer = Fake("list", rows=[{"name": "n1"}], proto="2.0")
        fakes.append(newer)
        r = dch(cenv, "--ls", "--hosts", "nw=127.0.0.1:%d" % newer.port)
        check("a newer major: skipped, said on stderr",
              "nw:n1" not in r.stdout and "speaks protocol 2.0" in r.stderr,
              r)

        # --- the link drops, the session ends -------------------------
        drop = Fake("drop", rows=[{"name": "d1"}])
        fakes.append(drop)
        pid, fd = attach(["--hosts", "dr=127.0.0.1:%d" % drop.port,
                          "-a", "dr:d1"])
        st, out = reap(pid, fd, 8)
        check("a dropped link: exit 1, a clear message",
              st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 1
              and b"FAKE-SCREEN" in out
              and b"lost the connection to dr" in out, (st, out))
        check("the terminal restored before the message",
              0 <= out.find(b"\x1b[?1049l") < out.find(b"lost the connection"),
              out)
        ex = Fake("exit", rows=[{"name": "e1"}])
        fakes.append(ex)
        pid, fd = attach(["--hosts", "ex=127.0.0.1:%d" % ex.port,
                          "-a", "ex:e1"])
        st, out = reap(pid, fd, 8)
        check("the session exits (0x12): exit 0, no message",
              st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0
              and b"lost" not in out, (st, out))
    finally:
        for pid in clients:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
        for f in fakes:
            f.close()
        dch(senv, "-kl")
        dch(cenv, "-kl")
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("all ok" if not fails else "%d FAILED" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
