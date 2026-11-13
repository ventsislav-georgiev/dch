"""Remote history (#115): --history --remote, --history --json --remote,
the picker's History rows of other hosts, and --ls-json's "cwd".

A real `dch serve` runs on 127.0.0.1 (DCH_SERVE_TEST_LOOPBACK=1, --bind
127.0.0.1 --port 0, a fake `tailscale` first on PATH), its sessions in their
own socket and state dirs; the client has others and reaches the server
only through --hosts label=127.0.0.1:port, DCH_NO_REMOTE=1. Fake servers on
loopback play an old host (no ctl), a hostile one, a hanging one, one that
refuses us and one that sends too much. Nothing else is ever dialled.

Covers: --ls-json rows gain "cwd" last (local: the live record's dir;
remote: the host's, through dch serve's listResp), every other key in its
old place; --history and --history --json without --remote unchanged and
offline; with --remote the local records first, byte for byte, then the
host's as host:name (text) or with "host" and "stale" (JSON); the 0x30 is
exactly --history --json; an old host: one stderr line, exit 0, the local
listing whole; a hostile host's control bytes escaped in text and \\u in
JSON, a record with its own "host" dropped, a reply that is no array, too
big or a failed run shows nothing; a hanging host within the box, then its
cached records [stale]; a refusing one says so and shows none; the picker
fetches a host's history once at its open, then only while its History tab
shows (#163); the picker's History tab lists srv:gone, Enter there says the host's create-gate refusal
in one line, and with --allow-create resumes it there; p on it says what
works. #167: a record a strict JSON reader rejects (lone surrogate, 1e999,
1000 deep) or with a key given twice is dropped from text, JSON and the
picker; bidi marks and U+2028/9 escaped; with a local twin tw:gone the
cursor stays on host tw's row and p pins nothing; remote rows sort among
the local ones. #254: local sessions running under the names of host tw's
records leave its rows as they were. #248: o on a remote History row
revives it there and attaches the host's session, with a local session of
that very host:name running; a revive whose name is too long to attach
leaves no host route for the next pick.

Run:  python3 tests/remote_history_test.py      (uses ./dch)
"""
import json, os, re, shutil, socket, struct, subprocess, sys, tempfile
import threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import FAKE_TS, OWNER, REFUSAL, WHOIS, frame  # noqa

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
# the 300 ms box plus a process start; ASan (DCH_TEST_NO_BUDGET) is slower
BUDGET = 10.0 if os.environ.get("DCH_TEST_NO_BUDGET") else 1.5
# what a terminal acts on: C0 (but \n), DEL, C1, the bidi controls and
# marks, the line and paragraph separators (#167)
CTL = re.compile("[\u200e\u200f\u061c\u2028\u2029\x00-\x09\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
LS_KEYS = ["name", "alias", "harness", "activity_epoch", "state", "version",
           "agents", "meta", "meta_epoch", "auto_allowed",
           "claude_session_id", "transcript_path"]


class HFake:
    """A remote-frames server on loopback: a hello with `ops`, then a ctl
    answered with `reply` (0x31s; a dict: by the ctl's argv[0]) and exit
    `xit`; or it hangs, or its gate refuses."""

    def __init__(self, reply=b"[]", ops=("ctl",), mode="ok", xit=0):
        self.reply, self.ops, self.mode, self.xit = reply, list(ops), mode, xit
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns, self.bodies = [], []
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
                c.sendall(frame(0x13, REFUSAL))
                c.shutdown(socket.SHUT_WR)
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
                        c.sendall(frame(0x18, {"proto": "1.1",
                                               "server": "fake",
                                               "ops": self.ops}))
                    elif t == 0x03:
                        c.sendall(frame(0x11, []))
                    elif t == 0x30:
                        self.bodies.append(json.loads(body))
                        r = self.reply
                        if isinstance(r, dict):
                            r = r[self.bodies[-1]["argv"][0]]
                        for k in range(0, len(r), 60000):
                            c.sendall(frame(0x31, r[k:k + 60000]))
                        c.sendall(frame(0x33, {"exit": self.xit}))
                        c.shutdown(socket.SHUT_WR)
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
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchrh", dir="/tmp"))
    fake = os.path.join(tmp, "fake")
    os.makedirs(fake)
    with open(os.path.join(fake, "tailscale"), "w") as f:
        f.write(FAKE_TS)
    os.chmod(os.path.join(fake, "tailscale"), 0o755)
    for name, obj in (("status.json", OWNER), ("whois.json", WHOIS)):
        with open(os.path.join(fake, name), "w") as f:
            json.dump(obj, f)
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
    # the real server's ctl runs a dch: not the box under test here (the
    # hanging fake checks that), so a loaded machine cannot flake it
    slow = dict(cenv, DCH_REMOTE_BOX_MS="5000")
    if os.environ.get("DCH_TEST_NO_BUDGET"):
        cenv["DCH_REMOTE_BOX_MS"] = "5000"
    procs, fakes, fails = [], [], []

    def check(msg, cond, extra=""):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)
            if extra:
                print("       " + str(extra).replace("\n", "\n       "))

    def dch(env, *args, timeout=30):
        return subprocess.run([DCH] + list(args), env=env, timeout=timeout,
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def serve_at(log, *args):
        with open(log, "w") as lf:
            sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                                   "--port", "0"] + list(args),
                                  env=dict(senv, DCH_SERVE_TEST_LOOPBACK="1"),
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=lf)
        procs.append(sp)
        end = time.time() + 10
        while time.time() < end and sp.poll() is None:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(log).read())
            if m:
                return int(m.group(1))
            time.sleep(0.05)
        return None

    def until(f, secs=10):
        end = time.time() + secs
        while time.time() < end:
            v = f()
            if v:
                return v
            time.sleep(0.1)
        return f()

    def fake(*a, **kw):
        f = HFake(*a, **kw)
        fakes.append(f)
        return f

    try:
        port = serve_at(os.path.join(tmp, "serve.log"))
        check("dch serve listens on a loopback test port", port is not None)
        if port is None:
            return 1
        H = ["--hosts", "srv=127.0.0.1:%d" % port]
        wd, wd2, cwd = (os.path.join(tmp, x) for x in ("wd", "wd2", "cwd"))
        for d in (wd, wd2, cwd):
            os.makedirs(d)
        rev = os.path.join(tmp, "rev")
        # ends at once (exit 3); revived once rev exists, it stays
        dch(senv, "--spawn", "gone", "--cwd", wd, "--", "/bin/sh", "-c",
            "[ -f %s ] && exec sleep 600; exit 3" % rev)
        dch(senv, "--spawn", "alive", "--cwd", wd2, "--", "/bin/sh", "-c",
            "exec sleep 600")
        dch(cenv, "--spawn", "loc1", "--cwd", cwd, "--", "/bin/sh", "-c",
            "exec sleep 600")
        dch(cenv, "--spawn", "lgone", "--", "/bin/sh", "-c", "exit 0")
        check("the server's gone and the client's lgone are History",
              until(lambda: "gone  " in dch(senv, "--history").stdout
                    and "lgone  " in dch(cenv, "--history").stdout))

        # --- --ls-json "cwd" --------------------------------------------
        r = dch(cenv, "--ls-json")
        row = next((x for x in json.loads(r.stdout) if x["name"] == "loc1"),
                   {})
        keys = list(row)
        check("--ls-json: cwd last, the live record's dir, every other key "
              "in its old place",
              row.get("cwd") == cwd and keys[-1] == "cwd"
              and keys[:len(LS_KEYS)] == LS_KEYS
              and set(keys[len(LS_KEYS):-1]) <= {"cols", "rows", "clients"},
              r.stdout)
        r = dch(slow, "--ls-json", "--remote", *H)
        rem = [x for x in json.loads(r.stdout) if "host" in x]
        check("--ls-json --remote: srv:alive with the host's cwd, last",
              [list(x) for x in rem] == [["name", "alias", "host", "state",
                                          "stale", "cwd"]]
              and rem[0]["name"] == "alive" and rem[0]["cwd"] == wd2, r.stdout)

        # --- --history [--json] without --remote: as before, offline -----
        quiet = fake(b'[{"name":"q1"}]')
        QH = ["--hosts", "q=127.0.0.1:%d" % quiet.port]
        t0 = dch(cenv, "--history")
        j0 = dch(cenv, "--history", "--json")
        t1 = dch(cenv, "--history", *QH)
        j1 = dch(cenv, "--history", "--json", *QH)
        time.sleep(0.3)
        check("--history, --history --json: no --remote, no connection, the "
              "same bytes", quiet.conns == [] and t0.stdout == t1.stdout
              and j0.stdout == j1.stdout and "q:" not in t1.stdout
              and j0.returncode == 0, (t1.stdout, j1.stdout))

        # --- with --remote: local first, then host:name -----------------
        r = dch(slow, "--history", "--remote", *H)
        lines = r.stdout.splitlines()
        sg = [x for x in lines if x.startswith("srv:gone  ")]
        check("--history --remote: the local lines first, unchanged, then "
              "srv:gone with its end and dir", r.returncode == 0
              and r.stdout.startswith(t0.stdout) and len(sg) == 1
              and "  exit 3  %s  $ /bin/sh -c" % wd in sg[0]
              and not sg[0].endswith("[stale]"), r)
        r = dch(slow, "--history", "--json", "--remote", *H)
        recs = json.loads(r.stdout)
        loc = json.loads(j0.stdout)
        g = [x for x in recs if x.get("host") == "srv"]
        check("--history --json --remote: the local records byte for byte, "
              "then srv's with host and stale", r.returncode == 0
              and r.stdout.startswith(j0.stdout.rstrip()[:-1])
              and recs[:len(loc)] == loc and len(g) == 1
              and g[0]["name"] == "gone" and g[0]["stale"] is False
              and g[0]["file"] == "gone" and g[0]["cwd"] == wd
              and g[0]["end"] == "exit 3" and list(g[0])[-2:] ==
              ["host", "stale"], r.stdout)
        r = dch(cenv, "--history", "--json", "--remote", *QH)
        check("the 0x30 is --history --json, nothing else",
              quiet.bodies == [{"argv": ["--history", "--json"]}]
              and '"host":"q"' in r.stdout, (quiet.bodies, r.stdout))
        r = dch(cenv, "--remote", "--history", "--hosts", "a b;c")
        check("--hosts still checked with --history --remote",
              r.returncode == 1 and "bad entry" in r.stderr, r)

        # --- an old host: one line, the local listing whole -------------
        old = fake(ops=[])
        r = dch(cenv, "--history", "--remote", "--hosts",
                "old=127.0.0.1:%d" % old.port)
        check("a host without ctl: one stderr line, exit 0, the local lines",
              r.returncode == 0 and r.stdout == t0.stdout and r.stderr ==
              "dch: old: does not support remote history (needs dch serve "
              "1.41+)\n" and old.bodies == [], r)
        r = dch(cenv, "--history", "--json", "--remote", "--hosts",
                "old=127.0.0.1:%d" % old.port)
        check("... and --json is the local array", r.returncode == 0
              and json.loads(r.stdout) == loc, r)

        # --- a hostile host ---------------------------------------------
        evil = [
            {"name": "ev\x1b]0;pwn\x07\u009b31m", "alias": "a\x1b[2Jb",
             "cwd": "/x‮/y\x7f", "end": "exit 1\x1b[31m",
             "argv": ["sh", "-c", "echo \x07⁦"], "file": "ev",
             "agent": {"session_id": "s\x1b[1m", "name": "n\u009d"},
             "ended": 1700000000, "pinned": True},
            {"name": "spoof", "host": "other"},
            {"name": "spoof2", "stale": False},
            {"alias": "noname"}, "str", 7, None,
            {"name": "x" * 300},
            {"name": "ok2", "ended": 99999999999999999999},
        ]
        hf = fake(json.dumps(evil, ensure_ascii=False).encode())
        EH = ["--hosts", "q=127.0.0.1:%d" % hf.port]
        r = dch(cenv, "--history", "--remote", *EH)
        ql = [x for x in r.stdout.splitlines() if x.startswith("q:")]
        check("a hostile host, text: every control byte escaped, the "
              "spoofs, the nameless and the oversized dropped",
              r.returncode == 0 and len(ql) == 2 and not CTL.search(r.stdout)
              and "spoof" not in r.stdout and "noname" not in r.stdout
              and ql[0].startswith("q:ev\\x1b]0;pwn\\x07") and "[pinned]"
              in ql[0] and ql[1].startswith("q:ok2  -  vanished"), r.stdout)
        r = dch(cenv, "--history", "--json", "--remote", *EH)
        try:
            got = [x for x in json.loads(r.stdout) if x.get("host") == "q"]
        except ValueError:
            got = []
        check("a hostile host, JSON: C1, DEL and bidi as \\u escapes, the "
              "fields as sent, the spoofs dropped",
              len(got) == 2 and got[0]["name"] == evil[0]["name"]
              and got[0]["cwd"] == evil[0]["cwd"]
              and not re.search("[\x7f-\x9f‪-‮⁦-⁩]",
                                r.stdout)
              and "spoof" not in r.stdout, r.stdout)
        # records a strict JSON reader rejects, keys given twice, bidi
        # marks and separators (#167): the first two kinds dropped from
        # every surface, the last escaped
        mark = "m\u200f\u061c x"
        sf = fake((
            '[{"name":"sur","x":"\\ud800"},'
            '{"name":"sur2","x":"\\udc00\\ud800"},'
            '{"name":"pair","x":"\\ud83d\\ude00"},'
            '{"name":"inf","n":1e999},{"name":"ninf","n":-1E400},'
            '{"name":"deep","d":%s},'
            '{"name":"d1","file":"d1","file":"evil-file"},'
            '{"name":"d2","alias":"A1","alias":"A2"},'
            '{"name":"d3","end":"exit 0","end":"exit 9"},'
            '{"name":"d4","cwd":"/a","cwd":"/b"},'
            '{"name":"d5","agent":{"name":"n1","name":"n2"}},'
            '{"name":"%s","alias":"a\u2028b\u2029c","cwd":"/\u200e"}]'
            % ("[" * 1000 + "]" * 1000, mark)).encode())
        SH = ["--hosts", "s=127.0.0.1:%d" % sf.port]
        r = dch(cenv, "--history", "--remote", *SH)
        sl = [x for x in r.stdout.splitlines() if x.startswith("s:")]
        check("text: a strict reader's rejects and the duplicate keys "
              "dropped, the bidi marks and separators escaped",
              r.returncode == 0 and len(sl) == 2
              and sl[0].startswith("s:pair  ") and
              sl[1].startswith("s:m\\u{200f}\\u{061c} x (a\\u{2028}b"
                               "\\u{2029}c)  ") and "/\\u{200e}" in sl[1]
              and not CTL.search(r.stdout), r.stdout)
        r = dch(cenv, "--history", "--json", "--remote", *SH)

        def nodup(kv):
            if len({k for k, _ in kv}) != len(kv):
                raise ValueError("duplicate key")
            return dict(kv)
        try:
            doc = json.loads(r.stdout, object_pairs_hook=nodup,
                             parse_constant=float)
            got = [x for x in doc if x.get("host") == "s"]
            json.dumps(doc, allow_nan=False).encode("utf-8")
        except (ValueError, RecursionError, UnicodeError) as e:
            got = [str(e)]
        check("JSON: strict, the same two records, the marks as \\u "
              "escapes", [x.get("name") if isinstance(x, dict) else x
                          for x in got] == ["pair", mark]
              and got[1]["alias"] == "a\u2028b\u2029c"
              and not CTL.search(r.stdout), r.stdout[-600:])
        for label, f in (
                ("a reply that is no array", fake(b'{"name":"x"}')),
                ("a reply over 2 MiB", fake(json.dumps(
                    [{"name": "big", "pad": "x" * (3 << 20)}]).encode())),
                ("a run that exits 1", fake(b'[{"name":"x"}]', xit=1))):
            r = dch(slow, "--history", "--remote", "--hosts",
                    "z=127.0.0.1:%d" % f.port)
            check("%s: nothing listed, exit 0" % label, r.returncode == 0
                  and r.stdout == t0.stdout and r.stderr == "", r)

        # --- a hanging host: the box, then the cache, stale --------------
        good = fake(b'[{"name":"cached","end":"exit 0"}]')
        dch(cenv, "--history", "--remote", "--hosts",
            "h=127.0.0.1:%d" % good.port)
        hang = fake(mode="hang")
        t = time.time()
        r = dch(cenv, "--history", "--remote", "--hosts",
                "h=127.0.0.1:%d" % hang.port)
        took = time.time() - t
        check("a hanging host: within the box, its cached records [stale]",
              r.returncode == 0 and took < BUDGET and
              any(x.startswith("h:cached  ") and x.endswith("  [stale]")
                  for x in r.stdout.splitlines()), (took, r))
        r = dch(cenv, "--history", "--json", "--remote", "--hosts",
                "h=127.0.0.1:%d" % hang.port)
        check("... and stale:true in JSON",
              [x.get("stale") for x in json.loads(r.stdout)
               if x.get("host") == "h"] == [True], r.stdout)
        ref = fake(mode="refuse")
        r = dch(cenv, "--history", "--remote", "--hosts",
                "h=127.0.0.1:%d" % ref.port)
        check("a refusing host: says why, none of its cached records",
              r.returncode == 0 and "h:" not in r.stdout and r.stderr ==
              "dch: h refused: %s\n" % REFUSAL["message"], r)

        # --- the picker ---------------------------------------------------
        if dch(cenv, "--build").stdout.strip() != "full":
            print("  skip: the picker checks need the terminal mirror")
        else:
            picker_checks(dch, check, cenv, senv, H, serve_at, tmp, rev,
                          until)
            refetch_checks(dch, check, cenv, fake, until)
            twin_checks(dch, check, cenv, fake, until)
            stale_route_checks(dch, check, cenv, fake, until)
    finally:
        for c in ("srv", "cli"):
            e = senv if c == "srv" else cenv
            for n in dch(e, "-ls").stdout.split():
                dch(e, "-k", n)
        for f in fakes:
            f.close()
        for p in procs:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("%d failed" % len(fails))
        return 1
    return 0


ROW_RE = re.compile("^([>▌ ]) . (\\S+)")


def below_tabs(s):
    """The lines under the tabs row and the column headers: the tabs row
    is the first on dch -l and the switch picker (#252), the second under
    -k's and -d's title."""
    ls = s.split("\n")
    i = next((k for k, l in enumerate(ls[:2])
              if re.match(r"[\[ ]?(Sessions|Settings)\b", l)), 1)
    return ls[i + 2:]


def picker_checks(dch, check, cenv, senv, H, serve_at, tmp, rev, until):
    def screen(n):
        return dch(cenv, "--read", n).stdout

    def rows(s):
        return [m.group(2) for m in map(ROW_RE.match, below_tabs(s))
                if m]

    def cur(s):
        for m in map(ROW_RE.match, below_tabs(s)):
            if m and m.group(1) != " ":
                return m.group(2)

    def open_on(pk, hosts, row="srv:gone"):
        dch(cenv, "--spawn", pk, "--size", "140x30", "--", "env", "-u",
            "DCH_SESSION", "NO_COLOR=1", "TERM=xterm-256color", DCH,
            *hosts, "-l")
        until(lambda: "History" in screen(pk))
        for _ in range(4):
            if "[History" in screen(pk):
                break
            dch(cenv, "--keys", pk, "tab")
            time.sleep(0.3)
        # the background probe fetches the host's history, the next
        # reload shows it
        s = until(lambda: row in rows(screen(pk)) and screen(pk), 15)
        for _ in range(10):
            s = screen(pk)
            r, c = rows(s), cur(s)
            if c == row or row not in r or c not in r:
                break
            d = r.index(row) - r.index(c)
            dch(cenv, "--keys", pk, *(["down"] * d if d > 0 else ["up"] * -d))
            time.sleep(0.3)
        return s

    s = open_on("pk1", H)
    check("the picker's History lists srv:gone and the local lgone",
          "srv:gone" in rows(s) and "lgone" in rows(s), s)
    check("srv:gone selected", cur(screen("pk1")) == "srv:gone",
          screen("pk1"))
    dch(cenv, "--keys", "pk1", "p")
    s = until(lambda: "another host" in screen("pk1") and screen("pk1"), 5)
    check("p on a remote row says what works there",
          "srv:gone: on another host; Enter resumes it there" in s, s)
    dch(cenv, "--keys", "pk1", "Enter")
    want = "dch: srv: does not support --revive (needs dch serve " \
           "--allow-create)"
    s = until(lambda: want in screen("pk1") and screen("pk1"), 15)
    check("Enter on it without --allow-create there: the gate's refusal "
          "in one line", want in s, s)
    dch(cenv, "-k", "pk1")

    port = serve_at(os.path.join(tmp, "serve-create.log"), "--allow-create")
    with open(rev, "w"):
        pass
    s = open_on("pk2", ["--hosts", "srv=127.0.0.1:%d" % port])
    dch(cenv, "--keys", "pk2", "Enter")
    s = until(lambda: "Resumed as srv:gone" in screen("pk2")
              and screen("pk2"), 30)
    check("Enter with --allow-create there: Resumed as srv:gone, live there",
          "Resumed as srv:gone" in s and "gone" in
          dch(senv, "-ls").stdout.split(), (s, dch(senv, "-ls").stdout))
    dch(cenv, "-k", "pk2")

    # #248: o on host srv's og, a local session literally named srv:og
    # running: the revive there, then the host's og attached, not the local
    rev2 = rev + "2"
    dch(senv, "--spawn", "og", "--", "/bin/sh", "-c", "[ -f %s ] && while "
        ":; do echo HOST-MARK; sleep 1; done; exit 3" % rev2)
    check("the server's og is History",
          until(lambda: "og  " in dch(senv, "--history").stdout))
    with open(rev2, "w"):
        pass
    dch(cenv, "--spawn", "srv:og", "--", "/bin/sh", "-c",
        "while :; do echo LOCAL-MARK; sleep 1; done")
    slog = os.path.join(tmp, "serve-create.log")

    def host_attaches():
        with open(slog) as f:
            return f.read().count(" attach og (")

    def local_clients():
        return [x.get("clients") for x in json.loads(
            dch(cenv, "--ls-json").stdout) if x["name"] == "srv:og"]

    s = open_on("pk5", ["--hosts", "srv=127.0.0.1:%d" % port], "srv:og")
    check("o: srv:og selected in History, the local srv:og running",
          cur(s) == "srv:og" and local_clients() == [0],
          (s, local_clients()))
    n0 = host_attaches()
    dch(cenv, "--keys", "pk5", "o")
    ok = until(lambda: host_attaches() > n0, 30)
    s = until(lambda: "HOST-MARK" in screen("pk5") and screen("pk5"), 10)
    check("o on host srv's og with a local srv:og running: revived there, "
          "the host's og attached, the local srv:og no client (#248)",
          ok and s and "LOCAL-MARK" not in s and local_clients() == [0],
          (host_attaches() - n0, local_clients(), s or screen("pk5")))
    dch(cenv, "-k", "pk5")
    dch(cenv, "-k", "srv:og")


def refetch_checks(dch, check, cenv, fake, until):
    """#163: one fetch at the picker's open; none while Sessions shows (the
    re-fetch interval is 10 s: one bounded 12 s wait); on History, at once
    and again each interval."""
    hf = fake(b'[{"name":"hx","end":"exit 0"}]')
    pk = "pk3"
    dch(cenv, "--spawn", pk, "--size", "140x30", "--", "env", "-u",
        "DCH_SESSION", "NO_COLOR=1", "TERM=xterm-256color", DCH,
        "--hosts", "h=127.0.0.1:%d" % hf.port, "-l")
    until(lambda: "History" in dch(cenv, "--read", pk).stdout)
    check("the picker opens on Sessions and fetches the history once",
          until(lambda: len(hf.bodies) >= 1, 15)
          and "[History" not in dch(cenv, "--read", pk).stdout, hf.bodies)
    time.sleep(12)
    s = dch(cenv, "--read", pk).stdout
    check("... and not again in 12 s while Sessions shows",
          len(hf.bodies) == 1 and "[History" not in s, (hf.bodies, s))
    for _ in range(4):
        if "[History" in dch(cenv, "--read", pk).stdout:
            break
        dch(cenv, "--keys", pk, "tab")
        time.sleep(0.3)
    check("History shown: a fetch at once",
          until(lambda: len(hf.bodies) >= 2, 6), hf.bodies)
    check("... and the next within the interval",
          until(lambda: len(hf.bodies) >= 3, 15), hf.bodies)
    dch(cenv, "-k", pk)


def twin_checks(dch, check, cenv, fake, until):
    """#167: a local record tw:gone and host tw's gone are two rows; the
    cursor stays on the remote one across the picker's reloads, and p there
    pins nothing. A remote row sorts among the local ones by host:name; a
    record with a key given twice is no row."""
    def screen():
        return dch(cenv, "--read", pk).stdout

    def lines(s):
        return [x for x in below_tabs(s) if ROW_RE.match(x)]

    def cur(s):
        return next((x for x in lines(s) if ROW_RE.match(x).group(1) != " "),
                    "")

    def remote_row(x):
        return re.match("^. . tw:gone +exited \\(3\\)", x)

    dch(cenv, "--spawn", "tw:gone", "--", "/bin/sh", "-c", "exit 5")
    dch(cenv, "--spawn", "zz", "--", "/bin/sh", "-c", "exit 0")
    until(lambda: "tw:gone  " in dch(cenv, "--history").stdout
          and "zz  " in dch(cenv, "--history").stdout)
    # local sessions running under the names host tw's records have (#254):
    # gone not pinned, aaa pinned; the host's rows stay as they were
    for n in ("gone", "aaa"):
        dch(cenv, "--spawn", n, "--", "/bin/sh", "-c", "sleep 600")
    until(lambda: all(x in dch(cenv, "-ls").stdout.split()
                      for x in ("gone", "aaa")))
    until(lambda: dch(cenv, "--pin", "aaa").returncode == 0)
    hf = fake(b'[{"name":"gone","end":"exit 3","ended":1700000000},'
              b'{"name":"aaa","end":"exit 0","ended":1700000000},'
              b'{"name":"tdup","file":"a","file":"b"}]')
    pk = "pk4"
    dch(cenv, "--spawn", pk, "--size", "140x30", "--", "env", "-u",
        "DCH_SESSION", "NO_COLOR=1", "TERM=xterm-256color", DCH,
        "--hosts", "tw=127.0.0.1:%d" % hf.port, "-l")
    until(lambda: "History" in screen())
    for _ in range(4):
        if "[History" in screen():
            break
        dch(cenv, "--keys", pk, "tab")
        time.sleep(0.3)
    s = until(lambda: any(map(remote_row, lines(screen()))) and screen(), 15)
    rows = [ROW_RE.match(x).group(2) for x in lines(s or "")]
    check("History: the local tw:gone and host tw's gone, tw:aaa among the "
          "local rows (before zz), no row for a record with a key twice",
          rows.count("tw:gone") == 2 and "tw:aaa" in rows and "zz" in rows
          and rows.index("tw:aaa") < rows.index("zz")
          and not any(x.startswith("tw:tdup") for x in rows), s)
    ls = lines(s or "")
    check("a local session running as gone (not pinned) and as aaa "
          "(pinned): host tw's rows unchanged, local gone no row, local aaa "
          "running (#254)",
          rows.count("gone") == 0 and rows.count("aaa") == 1 and
          any(re.match("^. . aaa +running", x) for x in ls) and
          any(re.match("^. . tw:aaa +exited ", x) for x in ls) and
          any(map(remote_row, ls)), s)
    for _ in range(10):
        s = screen()
        ls = lines(s)
        c = next((k for k, x in enumerate(ls) if x == cur(s)), -1)
        t = next((k for k, x in enumerate(ls) if remote_row(x)), -1)
        if c < 0 or t < 0 or c == t:
            break
        dch(cenv, "--keys", pk, *(["down"] * (t - c) if t > c
                                  else ["up"] * (c - t)))
        time.sleep(0.3)
    check("the cursor on host tw's gone", remote_row(cur(screen())), screen())
    time.sleep(3.5)	# reloads about once a second
    s = screen()
    check("... and still there 3.5 s later", remote_row(cur(s)), s)
    dch(cenv, "--keys", pk, "p")
    s = until(lambda: "another host" in screen() and screen(), 5)
    h = [x for x in json.loads(dch(cenv, "--history", "--json").stdout)
         if x.get("name") == "tw:gone"]
    check("p there says it is on another host and pins no local record",
          "tw:gone: on another host" in (s or "") and len(h) == 1
          and "pinned" not in h[0], (s, h))
    dch(cenv, "-k", pk)


def stale_route_checks(dch, check, cenv, fake, until):
    """#248: o on host lh's lr, whose revive answers a name too long to
    attach: the picker stays. Then Enter on the local Sessions row lh:loc
    attaches it here; the revive's host route is not left for that pick."""
    def screen():
        return dch(cenv, "--read", pk).stdout

    def rows(s):
        return [m.group(2) for m in map(ROW_RE.match, below_tabs(s)) if m]

    def cur(s):
        for m in map(ROW_RE.match, below_tabs(s)):
            if m and m.group(1) != " ":
                return m.group(2)

    def tab_to(name):
        for _ in range(4):
            if "[" + name in screen():
                return True
            dch(cenv, "--keys", pk, "tab")
            time.sleep(0.3)
        return "[" + name in screen()

    def go_row(row):
        until(lambda: row in rows(screen()), 15)
        for _ in range(10):
            s = screen()
            r, c = rows(s), cur(s)
            if c == row or row not in r or c not in r:
                return c == row
            d = r.index(row) - r.index(c)
            dch(cenv, "--keys", pk, *(["down"] * d if d > 0 else ["up"] * -d))
            time.sleep(0.3)
        return cur(screen()) == row

    hf = fake({"--history": b'[{"name":"lr","end":"exit 0","ended":1}]',
               "--revive": b'{"result":"ok","as":"%s"}\n' % (b"x" * 560)},
              ops=("ctl", "create"))
    dch(cenv, "--spawn", "lh:loc", "--", "/bin/sh", "-c",
        "while :; do echo LOCAL-MARK; sleep 1; done")
    pk = "pk6"
    dch(cenv, "--spawn", pk, "--size", "140x30", "--", "env", "-u",
        "DCH_SESSION", "NO_COLOR=1", "TERM=xterm-256color", DCH,
        "--hosts", "lh=127.0.0.1:%d" % hf.port, "-l")
    until(lambda: "History" in screen())
    ok = tab_to("History") and go_row("lh:lr")
    dch(cenv, "--keys", pk, "o")
    s = until(lambda: "Resumed as lh:xxx" in screen() and screen(), 15)
    check("o on lh:lr, revived as a name too long to attach: said, the "
          "picker stays", ok and s and "--revive" in
          [b["argv"][0] for b in hf.bodies], (ok, hf.bodies, s or screen()))
    ok = tab_to("Sessions") and go_row("lh:loc")
    dch(cenv, "--keys", pk, "Enter")
    s = until(lambda: "LOCAL-MARK" in screen() and screen(), 10)
    check("... then Enter on the local lh:loc attaches it here, no host "
          "route left by the revive (#248)", ok and s and [
              x.get("clients") for x in json.loads(dch(cenv, "--ls-json")
                                                   .stdout)
              if x["name"] == "lh:loc"] == [1], (ok, s or screen()))
    dch(cenv, "-k", pk)
    dch(cenv, "-k", "lh:loc")


if __name__ == "__main__":
    sys.exit(main())
