"""dch --ls-json [--remote] --follow (#103): the list as a stream.

A real `dch serve` on 127.0.0.1 (the fake `tailscale` and the test-only
loopback bypass, as remote_client_test.py runs it) is the host that lists
"watch"; fake servers on loopback play a host without it, one that lists
it and then hangs, and one that sends hostile stream frames. The client
has its own socket dir, DCH_NO_REMOTE=1, and reaches hosts only through
--hosts. DCH_REMOTE_EVERY_MS and DCH_REMOTE_QUIET_MS shorten (or, for the
watch check, lengthen) the poll tick and a silent stream's limit.

Covers: the first array is --ls-json --remote's; a session started and
one ended on a watch host each make an array, well inside the poll tick,
with no new connection; nothing for over a tick when nothing changes; a
host without "watch" is polled; a host that hangs goes stale while the
local rows and another host keep updating, and comes back; a closed
stdout ends the stream (exit 0, no process left, the host sees the
close); oversized, truncated, unreadable and wrong-type stream frames;
SIGTERM ends it with 0; the usage errors (exit 2). --ls-json --follow
without --remote streams the local rows and dials nothing.

QA 1.62.0: 16 followers on one machine leave it a slot on dch serve for
its next connection (LOW-R3); SIGTERM and SIGINT end it with 0 while its
reader has stopped reading and the pipe is full, and a line cut there is
the last output (MED-R1); a host that floods changed 0x11s makes at most one array
per 200 ms and its last rows still come, one that floods the same 0x11
writes the cache not at all (LOW-R2); a stdout on /dev/null keeps it
running (NIT-R1, macOS).

QA 1.64.0: a stdout closed at exec (`>&-`) ends it with 0 at once, local
and --remote (LOW-R14); with 16 followers up a 17th watch is refused busy,
told why, while a plain connection still fits, and a 17th follower is
polled, its rows never stale; a watch connection's rename is refused
(LOW-R15).

#340: a host that answers hello+list after 2 s is fresh under a box
(DCH_REMOTE_BOX_MS) over that, stale and dialled again under the default
one; the 16 and the 17th followers get a box that scales with the runner.

Run:  python3 tests/ls_follow_test.py        (uses ./dch)
"""
import glob, json, os, re, select, signal, socket, struct, subprocess, sys
import tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from serve_lib import fake_ts  # noqa
from remote_client_test import OWNER, WHOIS, Fake, frame  # noqa

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
SLOW = bool(os.environ.get("DCH_TEST_NO_BUDGET"))	# ASan
BUDGET = 10.0 if SLOW else 2.0


class WFake:
    """A host whose hello lists "watch": 0x2e opens a stream, its list then
    a 0x14 every `beat` s. push() sends bytes down every open stream;
    hang stops the beats and every answer (connections are still taken);
    delay holds each hello's answer that many seconds (a slow link)."""

    def __init__(self, rows, beat=0.3, delay=0):
        self.rows, self.beat, self.hang = rows, beat, False
        self.delay = delay
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self.streams, self.conns, self.eofs = [], 0, 0
        self.watches = []  # each 0x2e's payload
        threading.Thread(target=self.loop, daemon=True).start()
        threading.Thread(target=self.beats, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns += 1
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    break
                if self.hang:
                    continue
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        time.sleep(self.delay)
                        c.sendall(frame(0x18, {"proto": "1.1", "ops":
                                               ["ctl", "watch"]}))
                    elif t == 0x03:
                        c.sendall(frame(0x11, self.rows))
                    elif t == 0x2e:
                        self.watches.append(body)
                        with self.lock:
                            c.sendall(frame(0x11, self.rows))
                            self.streams.append(c)
        except OSError:
            pass
        with self.lock:
            if c in self.streams:
                self.streams.remove(c)
            self.eofs += 1
        c.close()

    def push(self, b):
        with self.lock:
            for s in list(self.streams):
                try:
                    s.sendall(b)
                except OSError:
                    self.streams.remove(s)

    def set_rows(self, rows):
        self.rows = rows
        self.push(frame(0x11, rows))

    def beats(self):
        while True:
            time.sleep(self.beat)
            if not self.hang:
                self.push(frame(0x14, b"{}"))

    def close(self):
        self.sock.close()
        with self.lock:
            for s in self.streams:
                try:
                    s.close()
                except OSError:
                    pass


class Stream:
    """dch <args> in its own process group, its stdout a pipe: arrays."""

    def __init__(self, env, *args):
        self.p = subprocess.Popen([DCH] + list(args), env=env,
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  start_new_session=True)
        self.buf, self.lines = b"", []

    def line(self, secs):
        """The next line (bytes), or None after secs."""
        end = time.time() + secs
        while b"\n" not in self.buf:
            left = end - time.time()
            if left <= 0:
                return None
            r, _, _ = select.select([self.p.stdout], [], [], left)
            if r:
                c = os.read(self.p.stdout.fileno(), 1 << 20)
                if not c:
                    return None
                self.buf += c
        ln, self.buf = self.buf.split(b"\n", 1)
        self.lines.append(ln)
        return ln

    def until(self, ok, secs):
        """Arrays until ok(rows): (rows, seconds taken), or (None, secs)."""
        t0 = time.time()
        while time.time() - t0 < secs:
            ln = self.line(secs - (time.time() - t0))
            if ln is None:
                break
            rows = json.loads(ln)
            if ok(rows):
                return rows, time.time() - t0
        return None, time.time() - t0

    def stop(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
        try:
            return self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            return self.p.wait()


def rem(rows, host):
    """host's rows: {name: stale}"""
    return {r["name"]: r["stale"] for r in rows if r.get("host") == host}


def local(rows):
    return [r["name"] for r in rows if "host" not in r]


def pgroup(pgid):
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid="],
                         capture_output=True, text=True).stdout
    return [ln.split()[0] for ln in out.splitlines()
            if len(ln.split()) == 2 and ln.split()[1] == str(pgid)]


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchlf", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    fake = os.path.join(tmp, "fake")
    os.makedirs(fake)
    # the compiled fake (#277): the 17th follower below dials about 3
    # times a second, a whois each (#375)
    fake_ts(fake)
    for name, obj in (("status.json", OWNER), ("whois.json", WHOIS)):
        with open(os.path.join(fake, name), "w") as f:
            json.dump(obj, f)
    with open(os.path.join(fake, "ip.txt"), "w") as f:
        f.write("100.100.1.1\n")
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
    cenv = side("cli", DCH_NO_REMOTE="1")
    if SLOW:
        cenv["DCH_REMOTE_BOX_MS"] = "5000"
    procs, fakes, streams, fails = [], [], [], []

    def check(msg, cond, extra=""):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)
            if extra:
                print("       " + str(extra).replace("\n", "\n       "))

    def dch(env, *args):
        return subprocess.run([DCH] + list(args), env=env, timeout=20,
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def follow(*args, **kw):
        s = Stream(dict(cenv, **kw), "--ls-json", *args)
        streams.append(s)
        return s

    def raw(port, *frs):
        c = socket.create_connection(("127.0.0.1", port), 5)
        c.settimeout(5)
        c.sendall(b"".join(frs))
        return c

    def reply(c, types, secs=5):
        """The first frame of one of types: (type, body), or None."""
        buf, end = b"", time.time() + secs
        while time.time() < end:
            while len(buf) >= 5:
                t, n = struct.unpack(">BI", buf[:5])
                if len(buf) < 5 + n:
                    break
                body, buf = buf[5:5 + n], buf[5 + n:]
                if t in types:
                    return t, body
            try:
                d = c.recv(65536)
            except OSError:
                return None
            if not d:
                return None
            buf += d
        return None

    log = os.path.join(tmp, "serve.log")
    try:
        with open(log, "w") as lf:
            sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                                   "--port", "0"],
                                  env=dict(senv, DCH_SERVE_TEST_LOOPBACK="1"),
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=lf)
        procs.append(sp)
        port, end = None, time.time() + 10
        while port is None and time.time() < end and sp.poll() is None:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(log).read())
            port = int(m.group(1)) if m else None
            time.sleep(0.05)
        check("dch serve listens on a loopback test port", port is not None,
              open(log).read())
        if port is None:
            return 1
        hosts = "srv=127.0.0.1:%d" % port
        for env, name in ((senv, "rs1"), (cenv, "loc1")):
            r = dch(env, "--spawn", name, "--size", "80x24", "--", "/bin/sh")
            check("session %s" % name, r.returncode == 0, r.stderr)
            # a reported state holds still; a detected one can flip under
            # load, and that is a change the stream rightly prints
            dch(env, "--report", name, "idle")
        time.sleep(1.5)

        # --- usage ---------------------------------------------------------
        for args in (["--ls-json", "--follow", "x"],
                     ["--ls-json", "--follow", "--follow", "x"],
                     ["--ls-json", "--follow", "-ls"]):
            r = dch(cenv, *args)
            check("refused with exit 2: dch %s" % " ".join(args),
                  r.returncode == 2 and r.stderr.startswith("dch: bad usage")
                  and r.stdout == "", (r.returncode, r.stderr))

        # --- 1. a watch host: dch serve ------------------------------------
        last = {}

        # the followers' box (#340): beside 16 watch handlers, each running
        # dch --ls-json every 2 s, a loaded ASan host answers a dial in
        # over 5 s, inside the 10 s the 17th follower gets (#401)
        renv = dict(cenv, DCH_REMOTE_BOX_MS="10000") if SLOW else cenv

        def ref():
            t0, n, m = time.time(), 0, len(open(log).read())
            # each try waits up to renv's box (ASan 10 s): room for 3 of them
            end = t0 + max(10, 3 * BUDGET)
            while True:
                r = dch(renv, "--ls-json", "--remote", "--hosts", hosts)
                n += 1
                if '"stale":false' in r.stdout or time.time() > end:
                    # why it took this long, for a failure's detail
                    text = open(log).read()
                    last.update(secs=round(time.time() - t0, 2), tries=n,
                                let_in=text[m:].count("loopback peer let in"),
                                refused=sorted(set(re.findall(
                                    r"refused[^\n]*", text))),
                                stderr=r.stderr[-300:])
                    return r.stdout

        want = ref()
        t0 = time.time()
        s = follow("--remote", "--follow", "--hosts", hosts,
                   DCH_REMOTE_EVERY_MS="6000")
        first = s.line(BUDGET + 2)
        took = time.time() - t0
        got = json.loads(first) if first else None
        check("the first array within the --ls box (%.2f s)" % took,
              first is not None and took < BUDGET, took)
        # "stale" is read at that instant (a loaded box reads true on one
        # side): the one volatile field; every other byte must be equal
        unstale = lambda t: re.sub(r'"stale": ?(true|false)', '"stale":X', t)
        check("the first array is --ls-json --remote's, byte for byte "
              "(but the stale flag)",
              first is not None and
              unstale(first.decode() + "\n") == unstale(want),
              (first, want))
        check("one line, a JSON array: loc1, srv:rs1 fresh",
              got and local(got) == ["loc1"]
              and rem(got, "srv") == {"rs1": False}, got)
        time.sleep(0.5)
        nconn = open(log).read().count("loopback peer let in")
        check("the host was asked to watch (0x2e)",
              re.search(r"watch \(device dch-\d+\)", open(log).read())
              is not None, open(log).read()[-600:])
        r = dch(senv, "--spawn", "rs2", "--size", "80x24", "--", "/bin/sh")
        rows, dt = s.until(lambda x: "rs2" in rem(x, "srv"), 5.5)
        check("a session started there: an array in %.2f s, well inside "
              "the 6 s tick" % dt, rows is not None and dt < 4.0,
              s.lines[-3:])
        r = dch(senv, "-k", "rs2")
        rows, dt = s.until(lambda x: "rs2" not in rem(x, "srv"), 5.5)
        check("a session ended there: an array in %.2f s" % dt,
              rows is not None and dt < 4.0 and rem(rows, "srv") ==
              {"rs1": False}, s.lines[-3:])
        check("the watch path: no new connection to the host",
              open(log).read().count("loopback peer let in") == nconn,
              open(log).read()[-800:])
        n0 = len(s.lines)
        ln = s.line(7.0)
        check("nothing for over a tick (7 s) while nothing changes",
              ln is None and len(s.lines) == n0, ln)
        s.p.send_signal(signal.SIGTERM)
        try:
            rc = s.p.wait(3)
        except subprocess.TimeoutExpired:
            rc = None
        check("SIGTERM ends it with exit 0", rc == 0, rc)

        # a watch connection reads the list, nothing else (QA 1.64.0 LOW-R15)
        c = raw(port, frame(0x08, {"proto": "1.1"}),
                frame(0x2e, {"device_id": "t-rename"}))
        first = reply(c, (0x11, 0x13))
        c.sendall(frame(0x06, {"name": "rs1", "alias": "by-watch"}))
        got = reply(c, (0x13, 0x14))
        c.close()
        r = dch(senv, "--ls-json")
        check("a watch connection's rename: refused (0x13), rs1 unrenamed",
              first is not None and first[0] == 0x11 and got is not None
              and got[0] == 0x13 and b"cannot kill or rename" in got[1]
              and "by-watch" not in r.stdout, (first, got, r.stdout[:300]))

        # 16 followers from this machine: its --read still gets a slot
        mark = len(open(log).read())
        nw = len(re.findall(r"watch \(device", open(log).read()))
        # a dial past its limit (16 at once, a slow runner) is retried after
        # EVERY, doubled per failure: a short one keeps that inside the wait;
        # the box, the limit when over 2 s (#340), scales with the runner
        fbox = str(int(BUDGET * 1000))  # 2 s, ASan 10 s
        many = [follow("--remote", "--follow", "--hosts", hosts,
                       DCH_REMOTE_EVERY_MS="1500", DCH_REMOTE_BOX_MS=fbox)
                for _ in range(16)]
        t0, end = time.time(), time.time() + 10 * BUDGET  # 20 s, ASan 100 s
        while (len(re.findall(r"watch \(device", open(log).read())) < nw + 16
               and time.time() < end):
            time.sleep(0.2)
        time.sleep(0.5)
        # the 17th rows below need 16 live streams: say what came instead
        tail = open(log).read()[mark:]
        up = len(re.findall(r"watch \(device", tail))
        check("16 followers here: 16 watch streams up (%d in %.1f s, %d "
              "connections)" % (up, time.time() - t0,
                                tail.count("loopback peer let in")),
              up == 16, (tail[-1500:], [os.read(f.p.stderr.fileno(), 300)
                                        for f in many if select.select(
                                            [f.p.stderr], [], [], 0)[0]]))
        got = ref()  # a one-shot --ls-json --remote: one more connection
        check("16 followers here: one more connection there still gets a "
              "slot", '"stale":false' in got
              and all(f.p.poll() is None for f in many),
              (got, open(log).read()[-600:]))
        if up == 16:  # never the 17th against fewer
            # watch streams have a cap of their own (QA 1.64.0 LOW-R15)
            c = raw(port, frame(0x08, {"proto": "1.1"}),
                    frame(0x2e, {"device_id": "t-17"}))
            got = reply(c, (0x11, 0x13))
            c.close()
            msg = json.loads(got[1]) if got and got[0] == 0x13 else {}
            check("16 followers here: a 17th watch is refused busy, told why",
                  msg.get("code") == "busy" and "16 session list streams"
                  in msg.get("message", ""), (got, open(log).read()[mark:]
                                              [-1500:]))
            # each poll is a dial: stale only when one is over the box
            s17 = follow("--remote", "--follow", "--hosts", hosts,
                         DCH_REMOTE_EVERY_MS="300", DCH_REMOTE_BOX_MS=fbox)
            many.append(s17)
            rows, _ = s17.until(lambda x: rem(x, "srv") == {"rs1": False},
                                BUDGET + 2)
            time.sleep(2.0)  # past the refusal, polled every 300 ms
            while s17.line(0.1):
                pass
            stale = [ln for ln in s17.lines if rem(json.loads(ln), "srv")
                     != {"rs1": False}]
            check("a 17th follower: polled, srv:rs1 never stale (dials "
                  "within %s ms)" % fbox,
                  rows is not None and not stale and s17.p.poll() is None
                  and len(re.findall(r"watch \(device", open(log).read()))
                  == nw + 16, (s17.lines[-3:], stale[:2], s17.p.poll(),
                               open(log).read()[mark:][-1500:]))
        got = ref()
        check("and a plain connection still fits beside them (%.1f s, %d "
              "tries)" % (last["secs"], last["tries"]),
              '"stale":false' in got, (got, last))
        for f in many:
            f.stop()

        # --- 1b. a slow host: hello+list after 2 s (#340) -------------------
        d = WFake([{"name": "d1"}], delay=2.6)
        fakes.append(d)
        dh = "d=127.0.0.1:%d" % d.port
        s = follow("--remote", "--follow", "--hosts", dh,
                   DCH_REMOTE_EVERY_MS="6000", DCH_REMOTE_BOX_MS="5000")
        rows, dt = s.until(lambda x: rem(x, "d") == {"d1": False},
                           2.6 + BUDGET + 1)
        check("a host that answers in 2.6 s, the box 5 s: fresh under "
              "--follow (%.2f s), one dial" % dt,
              rows is not None and d.conns == 1, (s.lines[-2:], d.conns))
        s.stop()
        c0 = d.conns
        t0 = time.time()
        s = follow("--remote", "--follow", "--hosts", dh,
                   DCH_REMOTE_EVERY_MS="300", DCH_REMOTE_BOX_MS="0")
        # until a stale array and a second dial, 5.5 s at most: a fresh one
        # would come at 2.6 s, inside the first dial's box
        end = t0 + 5.5
        while time.time() < end and not (d.conns - c0 >= 2 and any(
                rem(json.loads(ln), "d") == {"d1": True} for ln in s.lines)):
            s.line(0.1)
        # then read to the 5.5 s bound: a redial's answer (5 s box) must not
        # come fresh either
        while time.time() < end:
            s.line(0.1)
        while s.line(0.1):
            pass
        seen = [rem(json.loads(ln), "d") for ln in s.lines]
        check("the default box: the same host never fresh, stale and "
              "dialled again (%d dials)" % (d.conns - c0),
              {"d1": True} in seen and {"d1": False} not in seen
              and d.conns - c0 >= 2, (seen, d.conns - c0))
        s.stop()

        # --- 2. a host without watch: polled --------------------------------
        q = Fake("list", rows=[{"name": "p1"}])
        fakes.append(q)
        qh = "q=127.0.0.1:%d" % q.port
        s = follow("--remote", "--follow", "--hosts", qh,
                   DCH_REMOTE_EVERY_MS="300")
        rows, dt = s.until(lambda x: rem(x, "q") == {"p1": False}, BUDGET)
        check("a host without watch: its rows", rows is not None, s.lines)
        c0 = len(q.conns)
        q.rows = [{"name": "p1"}, {"name": "p2", "state": "idle"}]
        rows, dt = s.until(lambda x: rem(x, "q") == {"p1": False,
                                                     "p2": False}, 3)
        check("polled: a change there makes an array (%.2f s)" % dt,
              rows is not None and len(q.conns) > c0, s.lines[-2:])
        s.stop()

        # --- 3. a watch host that hangs -------------------------------------
        w = WFake([{"name": "w1"}])
        fakes.append(w)
        wh = "w=127.0.0.1:%d" % w.port
        q.rows = [{"name": "p1"}]
        s = follow("--remote", "--follow", "--hosts", "%s,%s" % (wh, qh),
                   DCH_REMOTE_EVERY_MS="300", DCH_REMOTE_QUIET_MS="1500")
        rows, _ = s.until(lambda x: rem(x, "w") == {"w1": False}
                          and rem(x, "q") == {"p1": False}, BUDGET + 1)
        check("a watch fake and a polled fake: both fresh", rows is not None,
              s.lines)
        time.sleep(0.5)
        check("one 0x2e, its device id this process's",
              len(w.watches) == 1 and json.loads(w.watches[0]) ==
              {"device_id": "dch-%d" % s.p.pid}, w.watches)
        w.set_rows([{"name": "w1"}, {"name": "w2"}])
        rows, dt = s.until(lambda x: "w2" in rem(x, "w"), 5 if SLOW else 2)
        check("its pushed 0x11 makes an array at once (%.2f s)" % dt,
              rows is not None and dt < (4.0 if SLOW else 1.0),
              s.lines[-2:])
        w.hang = True
        rows, dt = s.until(lambda x: rem(x, "w") == {"w1": True, "w2": True},
                           5)
        check("it hangs: its rows go stale (%.2f s, the quiet is 1.5 s)" % dt,
              rows is not None and rem(rows, "q") == {"p1": False},
              s.lines[-2:])
        q.rows = [{"name": "p1"}, {"name": "p3"}]
        rows, dt = s.until(lambda x: "p3" in rem(x, "q"), 3)
        check("meanwhile the other host keeps updating",
              rows is not None and rem(rows, "w") == {"w1": True, "w2": True},
              s.lines[-2:])
        r = dch(cenv, "--spawn", "loc2", "--size", "80x24", "--", "/bin/sh")
        rows, dt = s.until(lambda x: "loc2" in local(x), 3)
        check("and so do the local rows", rows is not None, s.lines[-2:])
        w.hang = False
        rows, dt = s.until(lambda x: rem(x, "w") == {"w1": False,
                                                     "w2": False}, 10)
        check("it comes back: fresh again (%.2f s)" % dt, rows is not None,
              s.lines[-2:])
        check("still running", s.p.poll() is None, s.p.returncode)
        s.stop()
        dch(cenv, "-k", "loc2")

        # --- 4. the reader goes ---------------------------------------------
        e0 = w.eofs
        s = follow("--remote", "--follow", "--hosts", wh,
                   DCH_REMOTE_EVERY_MS="300")
        rows, _ = s.until(lambda x: rem(x, "w") == {"w1": False,
                                                    "w2": False}, BUDGET)
        time.sleep(0.5)
        live = len(w.streams)
        s.p.stdout.close()
        try:
            rc = s.p.wait(3)
        except subprocess.TimeoutExpired:
            rc = None
        time.sleep(0.3)
        check("stdout closed: it exits 0 promptly", rows is not None
              and rc == 0, (rc, s.lines))
        check("no process of it left", pgroup(s.p.pid) == [],
              pgroup(s.p.pid))
        check("the host sees its stream closed", live == 1
              and w.eofs > e0 and not w.streams, (live, w.eofs, e0))

        # --- 5. hostile stream frames ---------------------------------------
        h = WFake([{"name": "h1"}], beat=0.3)
        fakes.append(h)
        hh = "h=127.0.0.1:%d" % h.port
        s = follow("--remote", "--follow", "--hosts", hh,
                   DCH_REMOTE_EVERY_MS="300", DCH_REMOTE_QUIET_MS="1500")
        rows, _ = s.until(lambda x: rem(x, "h") == {"h1": False}, BUDGET)
        check("a hostile host: fresh at first", rows is not None, s.lines)
        time.sleep(0.3)
        h.push(frame(0x55, b"junk" * 100) + frame(0x12, {"code": 0}) +
               frame(0x18, {"proto": "9.0"}) + frame(0x10, b"\x1b[2J"))
        h.set_rows([{"name": "h1"}, {"name": "h2"}])
        rows, _ = s.until(lambda x: rem(x, "h") == {"h1": False,
                                                    "h2": False}, 2)
        check("wrong types are skipped: the stream goes on", rows is not None,
              s.lines[-2:])
        evil = "\x1b]0;x\x07‮evil\x7f"
        h.set_rows([{"name": evil, "alias": "\x9b31m"}])
        rows, _ = s.until(lambda x: evil in rem(x, "h"), 5 if SLOW else 2)
        check("its names arrive JSON-escaped: no raw control byte",
              rows is not None and not re.search(rb"[\x00-\x1f\x7f]",
                                                 s.lines[-1]), s.lines[-1:])
        cases = [
            ("a 0x11 over the 1 MiB limit", struct.pack(">BI", 0x11, 2 << 20)),
            ("a 0x11 that is not an array", frame(0x11, {"a": 1})),
            ("a 0x11 with a NUL", frame(0x11, b'[{"name":"a\x00"}]')),
            ("a coded 0x13", frame(0x13, {"code": "not_owner",
                                          "message": "no"})),
            ("a truncated 0x11, then silence",
             struct.pack(">BI", 0x11, 100) + b'[{"name"'),
        ]
        for what, b in cases:
            h.set_rows([{"name": "h1"}])
            rows, _ = s.until(lambda x: rem(x, "h") == {"h1": False}, 10)
            ok = rows is not None
            if what.startswith("a truncated"):
                h.hang = True  # no beat completes the frame
            h.push(b)
            rows, dt = s.until(lambda x: rem(x, "h") in ({"h1": True}, {}), 5)
            h.hang = False
            check("%s: the host goes stale, nothing crashes (%.2f s)"
                  % (what, dt), ok and rows is not None
                  and s.p.poll() is None, s.lines[-3:])
        rows, _ = s.until(lambda x: rem(x, "h") == {"h1": False}, 10)
        check("and it comes back after each", rows is not None, s.lines[-2:])
        rc = s.stop()
        check("still exit 0 at the end", rc == 0, rc)
        err = s.p.stderr.read().decode(errors="replace")
        check("the coded 0x13 said once on stderr",
              err.count("dch: h refused: no") == 1, err)

        # --- 6. a reader that stops reading; floods; /dev/null --------------
        big = lambda k: [{"name": "b%d-%03d-%s" % (k, i, "x" * 120)}
                         for i in range(300)]
        f6 = WFake(big(0))
        fakes.append(f6)
        fh = "f=127.0.0.1:%d" % f6.port
        for sig in (signal.SIGTERM, signal.SIGINT):
            c0 = f6.conns
            s = follow("--remote", "--follow", "--hosts", fh,
                       DCH_REMOTE_EVERY_MS="300")
            # its watch stream up (BUDGET at most), then the pushes fill it
            end = time.time() + BUDGET
            while time.time() < end and not (f6.conns > c0
                                             and len(f6.streams) == 1):
                time.sleep(0.05)
            for k in range(1, 6):  # 60 KB arrays nobody reads: pipe full
                f6.set_rows(big(k))
                time.sleep(0.4)
            alive = s.p.poll() is None
            s.p.send_signal(sig)
            try:
                rc = s.p.wait(3)
            except subprocess.TimeoutExpired:
                rc = None
                s.p.kill()  # it reads no more 0x11s: the pushes below
                s.p.wait()  # would block on it
            out = s.p.stdout.read() if rc is not None else b""
            lines = out.split(b"\n")

            def whole(ln):
                try:
                    return isinstance(json.loads(ln), list)
                except ValueError:
                    return False
            check("%s with the pipe full and unread: exit 0 within 3 s"
                  % sig.name, alive and rc == 0, (alive, rc))
            check("%s: whole lines, then at most one cut one, then nothing"
                  % sig.name, rc == 0 and len(lines) > 1
                  and all(whole(x) for x in lines[:-1])
                  and not whole(lines[-1]), (len(out), lines[-1][-80:]))
        # a flood of changed lists: one array per 200 ms at most
        s = follow("--remote", "--follow", "--hosts", fh,
                   DCH_REMOTE_EVERY_MS="300")
        rows, _ = s.until(lambda x: "f" in {r.get("host") for r in x}
                          and not any(r["stale"] for r in x
                                      if r.get("host")), BUDGET + 1)
        f6.set_rows([{"name": "f0"}])
        rows, _ = s.until(lambda x: rem(x, "f") == {"f0": False}, 3)
        n0, t0 = len(s.lines), time.time()

        def flood(same, secs):
            i = 0
            while time.time() - t0 < secs:
                f6.push(b"".join(frame(0x11, [{"name": "fend" if same
                                               else "f%d" % (i + j)}])
                                 for j in range(50)))
                i += 50
        th = threading.Thread(target=flood, args=(False, 2.0), daemon=True)
        th.start()
        while th.is_alive():
            s.line(0.2)
        f6.set_rows([{"name": "fend"}])
        nf = len(s.lines) - n0
        # 4x limit under DCH_TEST_NO_BUDGET (ASan CI saw 1.06 s)
        rows, dt = s.until(lambda x: rem(x, "f") == {"fend": False},
                           5 if SLOW else 2)
        check("a flood of changed lists for 2 s: %d arrays (at most one "
              "per 200 ms)" % nf, rows is not None and 2 <= nf <= 16,
              (nf, rows is not None))
        check("and its last rows come after it (%.2f s)" % dt,
              rows is not None and dt < (4.0 if SLOW else 1.0), s.lines[-2:])
        cache = glob.glob(os.path.join(cenv["XDG_STATE_HOME"], "**",
                                       ".remote-cache"), recursive=True)
        seen, t0 = set(), time.time()
        th = threading.Thread(target=flood, args=(True, 2.0), daemon=True)
        th.start()
        while th.is_alive():
            for c in cache:
                try:
                    st = os.stat(c)
                    seen.add((st.st_ino, st.st_mtime_ns))
                except OSError:
                    pass
            time.sleep(0.01)
        check("a flood of the same list: the cache not rewritten "
              "(%d versions seen)" % len(seen), len(cache) == 1
              and len(seen) <= 2, (cache, len(seen)))
        check("still running", s.p.poll() is None, s.p.returncode)
        s.stop()
        p = subprocess.Popen([DCH, "--ls-json", "--remote", "--follow",
                              "--hosts", fh], env=dict(cenv,
                             DCH_REMOTE_EVERY_MS="300"),
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        time.sleep(2)
        alive = p.poll() is None
        if alive:
            p.send_signal(signal.SIGTERM)
        try:
            rc = p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait()
        check("stdout on /dev/null: it keeps running, SIGTERM exits 0",
              alive and rc == 0, (alive, rc))
        # fd 1 closed at exec: nobody reads (QA 1.64.0 LOW-R14)
        for args in (["--follow"], ["--remote", "--follow", "--hosts", fh]):
            t0 = time.time()
            p = subprocess.Popen(["/bin/sh", "-c", 'exec "$@" >&-', "sh",
                                  DCH, "--ls-json"] + args,
                                 env=dict(cenv, DCH_REMOTE_EVERY_MS="300"),
                                 stdin=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            try:
                rc = p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
                rc = None
            check("stdout closed at exec (>&-), %s: exit 0 within 2 s "
                  "(%.2f s)" % (args[0], time.time() - t0), rc == 0, rc)

        # --- local only: --ls-json --follow --------------------------------
        q0 = len(q.conns)
        s = follow("--follow", "--hosts", qh, DCH_REMOTE_EVERY_MS="300")
        ln = s.line(BUDGET)
        check("--ls-json --follow: the local array, no host dialled",
              ln is not None and local(json.loads(ln)) == ["loc1"]
              and all("host" not in x for x in json.loads(ln)), ln)
        r = dch(cenv, "--spawn", "loc3", "--size", "80x24", "--", "/bin/sh")
        rows, dt = s.until(lambda x: "loc3" in local(x), 3)
        check("a local session started: an array (%.2f s)" % dt,
              rows is not None, s.lines[-2:])
        dch(cenv, "-k", "loc3")
        rows, dt = s.until(lambda x: "loc3" not in local(x), 3)
        check("and one when it ends", rows is not None, s.lines[-2:])
        check("no host dialled", len(q.conns) == q0, len(q.conns) - q0)
        check("exit 0 on SIGTERM", s.stop() == 0)
    finally:
        for s in streams:
            s.stop()
        for f in fakes:
            f.close()
        for env in (senv, cenv):
            subprocess.run([DCH, "-kl"], env=env, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=20)
        for p in procs:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        subprocess.run(["rm", "-rf", tmp])
    print("%d failed" % len(fails) if fails else "all ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
