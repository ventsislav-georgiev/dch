"""The dch client for remote hosts (#087): -ls, --ls-json/-lj --remote, -a.

A real `dch serve` runs on 127.0.0.1 (DCH_SERVE_TEST_LOOPBACK=1, --bind
127.0.0.1 --port 0, a fake `tailscale` first on PATH), its sessions in their
own socket dir; the client has another, and reaches the server only through
--hosts label=127.0.0.1:port. DCH_NO_REMOTE=1 on the client, so discovery
never runs except in the checks that feed it a stub status and name an
unknown host, or one whois refuses (neither dials anything). Fake servers on loopback play a hostile
peer, a hanging one, a newer protocol and a link that drops.

Covers: remote rows in -ls, -lj and --ls-json with --remote only, and plain
-ls, -lj and --ls-json never connecting; -a host:name shows the screen,
input round-trips, a resize arrives, the detach leaves the session alive;
--host <h> -a <name>; an exact local name wins; an unknown host (and
discovery's peer filter, by its known list, only with BackendState Running);
a discovered host refused unless `tailscale whois` says it is the user's,
untagged; a hostile server's control bytes escaped (C1 and bidi too, in
--ls-json); a hanging host within the time box, then its cached rows marked
stale; a tampered cache (an "at" out of range, a host that is not a label)
shown nowhere, the picker included; a newer major skipped; a dropped link
exits 1 with a message and the terminal restored; a 0x13 then a reset keeps
the 0x13's reason; 0x12 ends the attach with 0; a host whose gate refuses us
(a 0x13 with a code, #094): -a says why and exits 1, --ls/--ls-json --remote
say why and show none of its cached rows, against a fake and dch serve.

Run:  python3 tests/remote_client_test.py        (uses ./dch)
"""
import errno, json, os, re, select, shutil, signal, socket
import struct, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
# the 300 ms box plus a process start; ASan (DCH_TEST_NO_BUDGET) is slower
BUDGET = 10.0 if os.environ.get("DCH_TEST_NO_BUDGET") else 1.5
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
# is ever dialled: the checks name an unknown host, or one whois refuses
# before any connect. mybox's address is in 100.115.92.0/23, which
# Tailscale never assigns (ChromeOS reserves it), should one ever be.
PEERS = dict(OWNER, BackendState="Running", Peer={
    "k1": {"DNSName": "mybox.tail1.ts.net.", "HostName": "MyBox",
           "UserID": 42, "Online": True, "TailscaleIPs": ["100.115.92.1"]},
    "k2": {"DNSName": "theirs.tail1.ts.net.", "UserID": 7, "Online": True,
           "TailscaleIPs": ["100.64.9.2"]},
    "k3": {"DNSName": "server.tail1.ts.net.", "UserID": 42, "Online": True,
           "Tags": ["tag:server"], "TailscaleIPs": ["100.64.9.3"]},
    "k4": {"DNSName": "sleepy.tail1.ts.net.", "UserID": 42,
           "Online": False, "TailscaleIPs": ["100.64.9.4"]},
    "k5": {"DNSName": "noip.tail1.ts.net.", "UserID": 42, "Online": True,
           "TailscaleIPs": ["8.8.8.8"]}})


REFUSAL = {"code": "not_owner",
           "message": "this host serves only its owner's devices"}


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
        self.bodies = []  # each 0x01's payload: the name it attaches
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
            if self.mode == "refuse":
                # dch serve's gate (#094): one 0x13 with a code before a
                # byte is read, the FIN, then read off what the client sent
                c.sendall(frame(0x13, REFUSAL))
                c.shutdown(socket.SHUT_WR)
                while c.recv(65536):
                    pass
                return
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": self.proto,
                                               "server": "fake"}))
                    elif t == 0x03:
                        c.sendall(frame(0x11, self.rows))
                    elif t == 0x01:
                        self.bodies.append(body)
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
                        if self.mode == "errrst":
                            c.sendall(frame(0x13, {"message": "bye-why"}))
                            time.sleep(0.3)
                            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                         struct.pack("ii", 1, 0))
                            c.close()
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
    # POSIX only, so windows_client_test.py can import Fake and frame
    import fcntl, pty, termios
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
    if os.environ.get("DCH_TEST_NO_BUDGET"):
        # ASan: a loopback dch serve can miss the 300 ms box
        cenv["DCH_REMOTE_BOX_MS"] = "5000"
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
        quiet = Fake("list", rows=[{"name": "q1"}])
        fakes.append(quiet)
        qh = "q=127.0.0.1:%d" % quiet.port
        r = dch(cenv, "-ls", "--hosts", qh)
        r2 = dch(cenv, "-lj", "--hosts", qh)
        r3 = dch(cenv, "--ls-json", "--hosts", qh)
        time.sleep(0.3)
        check("plain -ls, -lj, --ls-json: local rows only, no connection",
              r.stdout.splitlines() == ["loc1"] and "q:" not in r2.stdout
              and "q1" not in r3.stdout and quiet.conns == [],
              (r.stdout, r2.stdout, r3.stdout, len(quiet.conns)))
        r = dch(cenv, "-ls", "--remote", "--hosts", qh)
        check("-ls --remote: it connects and lists q:q1",
              "q:q1" in r.stdout.splitlines() and len(quiet.conns) == 1,
              (r.stdout, len(quiet.conns)))
        r = dch(cenv, "--ls", "--remote", "--hosts", hosts)
        lines = r.stdout.splitlines()
        check("--ls --remote: the local session, then srv:rs1",
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
        r = dch(cenv, "--remote", "-kl")
        check("--remote with -kl is refused", r.returncode == 1
              and "--remote only works" in r.stderr, r.stderr)
        r = dch(cenv, "-ls", "--hosts", "a b;c")
        check("a bad --hosts entry is refused", r.returncode == 1
              and "bad entry" in r.stderr, r.stderr)
        r = dch(cenv, "--host", "srv", "-ls")
        check("--host without -a is refused", r.returncode == 1, r.stderr)

        # --- attach ---------------------------------------------------
        pid, fd = attach(["--hosts", hosts, "-a", "srv:rs1"])
        out = read(fd, b"$ ", 3)
        if b"$ " not in out:
            # lite: no replay, and an idle sh repaints nothing; prompt it
            os.write(fd, b"\r")
            out += read(fd, b"$ ", 8)
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
        put("status.json", dict(PEERS, BackendState="Stopped"))
        r = dch(denv, "-a", "nope:x")
        check("discovery: none unless BackendState is Running",
              r.returncode == 1 and "none:" in r.stderr, r.stderr)
        put("status.json", PEERS)
        for why, w in (("another user", dict(WHOIS, UserProfile={
                           "ID": 7, "LoginName": "them@example.com"})),
                       ("a tagged node", dict(WHOIS, Node=dict(
                           WHOIS["Node"], Tags=["tag:x"]))),
                       ("failing", None)):
            if w is None:
                os.unlink(os.path.join(fake, "whois.json"))
            else:
                put("whois.json", w)
            r = dch(denv, "-a", "mybox:x")
            check("a discovered host, whois %s: refused before connecting"
                  % why, r.returncode == 1 and "tailscale whois" in r.stderr,
                  r.stderr)
        put("whois.json", WHOIS)
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
        r = dch(cenv, "--ls", "--remote", "--hosts", eh)
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
        c1 = Fake("list", rows=[{"name": "c1\u009b31m", "alias":
                                 "bi\u202edi\u2066x"}])
        fakes.append(c1)
        r = subprocess.run([DCH, "--ls-json", "--remote", "--hosts",
                            "c=127.0.0.1:%d" % c1.port], env=cenv,
                           capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=20)
        out = r.stdout
        rows = [x for x in json.loads(out) if "host" in x]
        check("C1 and bidi in --ls-json: \\u-escaped, the same value",
              b"\xc2\x9b" not in out and b"\xe2\x80\xae" not in out
              and b"\xe2\x81\xa6" not in out and b"c1\\u009b31m" in out
              and b"\\u202e" in out and b"\\u2066" in out
              and rows and rows[0]["name"] == "c1\u009b31m"
              and rows[0]["alias"] == "bi\u202edi\u2066x", out)

        # --- a hanging host, then its rows stale ----------------------
        hang = Fake("hang")
        fakes.append(hang)
        t0 = time.time()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "%s,h=127.0.0.1:%d" % (hosts, hang.port))
        dt = time.time() - t0
        check("a hanging host does not stall --ls (%.2fs)" % dt,
              dt < BUDGET and "srv:rs1" in r.stdout.splitlines(), r)
        t0 = time.time()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % hang.port)
        dt = time.time() - t0
        check("then its cached rows show stale (%.2fs)" % dt,
              dt < BUDGET and "srv:rs1 [stale]" in r.stdout.splitlines(),
              r.stdout)
        r = dch(cenv, "--ls-json", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % hang.port)
        rem = [x for x in json.loads(r.stdout) if "host" in x]
        check("--ls-json marks them stale", rem and rem[0]["stale"] is True,
              r.stdout)

        # --- a tampered cache -----------------------------------------
        cache = os.path.join(tmp, "cli", "st", "dch", "sessions",
                             ".remote-cache")
        now = int(time.time())
        bad = [{"host": "srv", "name": "past", "at": -2 ** 63},
               {"host": "srv", "name": "future", "at": 99999999999},
               {"host": "srv", "name": "zero", "at": 0},
               {"host": "srv\u202e\u001b[2J:zz", "name": "spoof",
                "at": now},
               {"host": "srv", "name": "keep", "at": now}]

        def put_cache():
            with open(cache, "w") as f:
                f.write(json.dumps(bad))
        put_cache()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % hang.port)
        lines = r.stdout.splitlines()
        check("a tampered cache: an at out of range is not listed",
              r.returncode == 0 and "srv:keep [stale]" in lines
              and not any(x in r.stdout for x in ("past", "future", "zero",
                                                  "spoof")), r)
        put_cache()
        pid, fd = attach(["--hosts", "srv=127.0.0.1:%d" % hang.port, "-l"])
        out = read(fd, b"keep", 4)
        out += read(fd, b"\0never", 0.5)
        os.write(fd, b"q")
        st, more = reap(pid, fd, 5)
        out += more
        check("a tampered cache in the picker: only label hosts, in range",
              b"srv:\x1b[0mkeep" in out and b"spoof" not in out
              and b"\xe2\x80\xae" not in out and b"future" not in out,
              (st, out))

        # --- a host whose gate refuses us (#094) -----------------------
        refuse = Fake("refuse")
        fakes.append(refuse)
        put_cache()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % refuse.port)
        check("a refusing host: no stale rows, its reason on stderr (#094)",
              r.returncode == 0 and "srv:" not in r.stdout
              and "dch: srv refused: this host serves only its owner's "
                  "devices" in r.stderr, r)
        put_cache()
        r = dch(cenv, "--ls-json", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % refuse.port)
        check("--ls-json: no rows of a refusing host, the reason on stderr",
              r.returncode == 0 and not [x for x in json.loads(r.stdout)
                                         if "host" in x]
              and "srv refused: this host serves only" in r.stderr, r)
        r = dch(cenv, "--hosts", "srv=127.0.0.1:%d" % refuse.port,
                "-a", "srv:keep")
        check("-a to a refusing host: its reason, exit 1 (#094)",
              r.returncode == 1 and r.stderr == "dch: srv refused: this host "
              "serves only its owner's devices\n", r)

        # --- a newer protocol -----------------------------------------
        newer = Fake("list", rows=[{"name": "n1"}], proto="2.0")
        fakes.append(newer)
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "nw=127.0.0.1:%d" % newer.port)
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
        rst = Fake("errrst", rows=[{"name": "x1"}])
        fakes.append(rst)
        pid, fd = attach(["--hosts", "rs=127.0.0.1:%d" % rst.port,
                          "-a", "rs:x1"])
        st, out = reap(pid, fd, 8)
        check("a 0x13 then a reset: the 0x13's reason, not the reset",
              st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 1
              and b"lost the connection to rs: it says: bye-why" in out,
              (st, out))

        # --- the real dch serve's gate refusing us (#094), last: it then
        # refuses 127.0.0.1 for 30 s
        put("whois.json", dict(WHOIS, UserProfile={
            "ID": 7, "LoginName": "other@example.com"}))
        r = dch(cenv, "--hosts", hosts, "-a", "srv:rs1")
        check("dch serve refusing -a: its reason, exit 1 (#094)",
              r.returncode == 1 and r.stderr == "dch: srv refused: this host "
              "serves only its owner's devices\n", r)
        r = dch(cenv, "--ls", "--remote", "--hosts", hosts)
        check("and --ls --remote: no srv rows, its reason (the cached "
              "refusal)", r.returncode == 0 and "srv:" not in r.stdout
              and "dch: srv refused: this host serves only" in r.stderr, r)
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
