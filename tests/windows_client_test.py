"""The Windows client (#077): dch.exe's -ls, -lj, --ls-json and -a.

Fake remote-frames servers (remote_client_test.Fake) on 127.0.0.1 answer as
a host, a hostile peer, a hanging one and a newer protocol; dch reaches them
only through --hosts (or remote_hosts in a config of its own), with
DCH_NO_REMOTE=1. Discovery runs only against a fake tailscale (a stub that
prints the status and whois documents beside it): built with gcc on Windows,
a shell script elsewhere; without gcc on Windows those checks are skipped.

Covers: rows in -ls, -lj and --ls-json; --hosts with DCH_NO_REMOTE=1, the
remote_hosts config, remote_hosts = off; hostile names escaped; a hanging
host not stalling past the time box, then its cached rows stale; a hanging
tailscale cut off at the box; a newer major skipped; a refusing host (a
0x13 with a code, #094) said, its cached rows not shown; a busy host (#095)
said, its cached rows stale; an unknown host's
error; -a host:missing; -a with no console (the attach's checks and the
0x01's path up to the console); discovery's peer filter and whois refusal.
On Windows also: -ls implies --remote, and -a wants host:name; dch alone
with no console refused; and the picker (#090) in a console of its own
(console_picker): the rows, j and Enter attach, the double tap back to the
picker, Enter, one tap ends dch; Esc quits.

It runs against the POSIX dch too (every list passes --remote), which is
how it is checked off Windows; CI runs it against dch.exe.

Run:  python tests/windows_client_test.py      (DCH, else ./dch.exe or ./dch)
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import Fake, OWNER, PEERS, WHOIS  # noqa: E402

WIN = os.name == "nt"
ROOT = os.path.join(HERE, "..")
DCH = os.path.abspath(os.environ.get("DCH") or os.path.join(
    ROOT, "dch.exe" if WIN else "dch"))
# the time box (1000 ms on Windows, 300 elsewhere) plus a process start
BUDGET = 10.0 if os.environ.get("DCH_TEST_NO_BUDGET") else (
    3.0 if WIN else 1.5)

FAKE_TS_C = r"""
#include <stdio.h>
#include <string.h>
#include <windows.h>
int main(int argc, char **argv)
{
	char p[1024], *s;
	FILE *f;
	int c;

	if (argc < 2 || strlen(argv[0]) > 900)
		return 1;
	strcpy(p, argv[0]);
	if (!(s = strrchr(p, '\\')))
		return 1;
	strcpy(s + 1, "hang");
	if ((f = fopen(p, "rb")))
		Sleep(20000);
	sprintf(s + 1, "%.20s.json", argv[1]);
	if (!(f = fopen(p, "rb")))
		return 1;
	while ((c = getc(f)) != EOF)
		putchar(c);
	return 0;
}
"""
FAKE_TS_SH = """#!/bin/sh
d=$(dirname "$0")
[ -f "$d/hang" ] && sleep 20
[ -f "$d/$1.json" ] && exec cat "$d/$1.json"
exit 1
"""


def fake_tailscale(d):
    """A fake tailscale in d: True, or False when it cannot be built."""
    if not WIN:
        p = os.path.join(d, "tailscale")
        with open(p, "w") as f:
            f.write(FAKE_TS_SH)
        os.chmod(p, 0o755)
        return True
    cc = shutil.which(os.environ.get("FAKE_CC", "gcc"))
    if not cc:
        return False
    src = os.path.join(d, "fake_ts.c")
    with open(src, "w") as f:
        f.write(FAKE_TS_C)
    r = subprocess.run([cc, "-O1", "-o", os.path.join(d, "tailscale.exe"),
                        src], capture_output=True, text=True)
    if r.returncode:
        print("       fake tailscale: " + r.stderr)
    return r.returncode == 0


def console_picker(qh):
    """--console-picker: dch.exe alone, the picker, in a console of its own
    that this process attaches to: keys in by WriteConsoleInputW, the screen
    out by ReadConsoleOutputCharacterW. What it saw as JSON on stdout. A
    process of its own: it leaves its console (main's) for dch's."""
    import ctypes
    from ctypes import wintypes as W

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class KEY(ctypes.Structure):
        _fields_ = [("down", W.BOOL), ("rep", W.WORD), ("vk", W.WORD),
                    ("sc", W.WORD), ("ch", W.WCHAR), ("st", W.DWORD)]

    class REC(ctypes.Structure):
        _fields_ = [("type", W.WORD), ("key", KEY)]

    class SBI(ctypes.Structure):
        _fields_ = [("size", W._COORD), ("cur", W._COORD), ("attr", W.WORD),
                    ("win", W.SMALL_RECT), ("max", W._COORD)]

    k32.CreateFileW.restype = W.HANDLE
    k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID,
                                W.DWORD, W.DWORD, W.HANDLE]
    k32.CloseHandle.argtypes = [W.HANDLE]
    k32.GetConsoleScreenBufferInfo.argtypes = [W.HANDLE, ctypes.POINTER(SBI)]
    k32.ReadConsoleOutputCharacterW.argtypes = [
        W.HANDLE, W.LPWSTR, W.DWORD, W._COORD, ctypes.POINTER(W.DWORD)]
    k32.WriteConsoleInputW.argtypes = [W.HANDLE, ctypes.POINTER(REC),
                                       W.DWORD, ctypes.POINTER(W.DWORD)]

    def con(name):  # GENERIC_READ|WRITE, shared, OPEN_EXISTING
        return k32.CreateFileW(name, 0xC0000000, 3, None, 3, 0, None)

    def screen():
        h, sbi, got = con("CONOUT$"), SBI(), W.DWORD()  # the active buffer
        s = ""
        if k32.GetConsoleScreenBufferInfo(h, ctypes.byref(sbi)):
            w = sbi.size.X
            n = w * (sbi.win.Bottom - sbi.win.Top + 1)
            b = ctypes.create_unicode_buffer(n)
            k32.ReadConsoleOutputCharacterW(h, b, n, W._COORD(0, sbi.win.Top),
                                            ctypes.byref(got))
            s = "\n".join(b[i:i + w].rstrip()
                          for i in range(0, got.value, w))
        k32.CloseHandle(h)
        return s

    def keys(*cs):  # each a press and a release
        recs = (REC * (2 * len(cs)))()
        for i, c in enumerate(cs):
            for j in (0, 1):
                r = recs[2 * i + j]
                r.type, r.key.down, r.key.rep, r.key.ch = 1, 1 - j, 1, c
        h = con("CONIN$")
        k32.WriteConsoleInputW(h, recs, len(recs), ctypes.byref(W.DWORD()))
        k32.CloseHandle(h)

    def until(cond, t=15):
        end = time.time() + t
        while True:
            s = screen()
            if cond(s) or time.time() > end:
                return cond(s), s
            time.sleep(0.1)

    def start():
        p = subprocess.Popen([DCH, "--hosts", qh],
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        k32.FreeConsole()
        for _ in range(100):
            if k32.AttachConsole(p.pid):
                break
            time.sleep(0.05)
        return p

    out, p = {}, None
    try:
        p = start()
        ok, s = until(lambda s: "q:q1" in s and "q:q2" in s)
        out["picker"], out["screen"] = ok and "attach session" in s, s
        # the second row, as drawn
        out["second"] = "q1" if s.find("q:q1") > s.find("q:q2") else "q2"
        out["first"] = "q2" if out["second"] == "q1" else "q1"
        keys("j", "\r")
        out["attached"], out["screen"] = until(lambda s: "FAKE-SCREEN" in s)
        keys("\x1c", "\x1c")
        out["switch"], out["screen"] = until(
            lambda s: "switch to session:" in s and "q:q2" in s)
        keys("\r")
        ok, out["screen"] = until(lambda s: "switch to session:" not in s)
        time.sleep(1)  # the attach under way: its relay reads the key
        keys("\x1c")
        p.wait(15)
        out["tap_rc"] = p.returncode
        p = start()
        ok, out["screen"] = until(lambda s: "q:q1" in s)
        keys("\x1b")
        p.wait(15)
        out["esc_rc"] = p.returncode
    except Exception as e:  # what it got to, for the checks to say
        out["error"] = repr(e)
    finally:
        if p and p.poll() is None:
            p.kill()
    print(json.dumps(out))


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchwc"))
    fake = os.path.join(tmp, "fake")
    os.makedirs(fake)
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("DCH_", "MallocStackLogging"))}
    for d in ("local", "roam", "home", "st"):
        os.makedirs(os.path.join(tmp, d))
    conf = os.path.join(tmp, "config")
    env = dict(base, LOCALAPPDATA=os.path.join(tmp, "local"),
               APPDATA=os.path.join(tmp, "roam"), DCH_CONFIG=conf,
               HOME=os.path.join(tmp, "home"), XDG_RUNTIME_DIR=tmp,
               XDG_STATE_HOME=os.path.join(tmp, "st"), DCH_LOGIN_ENV="0",
               DCH_NO_REMOTE="1")
    if os.environ.get("DCH_TEST_NO_BUDGET"):
        env["DCH_REMOTE_BOX_MS"] = "5000"
    fakes, fails = [], []

    def check(msg, cond, extra=""):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)
            if extra:
                print("       " + str(extra).replace("\n", "\n       "))

    def dch(*args, e=None, timeout=30):
        r = subprocess.run([DCH] + list(args), env=e or env,
                           capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=timeout)
        r.out = r.stdout.decode("utf-8", "replace")
        r.err = r.stderr.decode("utf-8", "replace")
        return r

    def put(name, obj):
        with open(os.path.join(fake, name), "w") as f:
            f.write(json.dumps(obj))

    try:
        r = dch("--version")
        check("--version", r.returncode == 0 and r.out.startswith("dch "),
              r.out + r.err)
        r = dch("-h")
        check("-h: exit 0, lists --hosts", r.returncode == 0
              and "--hosts" in r.out, r.out + r.err)

        # --- listing --------------------------------------------------
        q = Fake("list", rows=[{"name": "q1", "alias": "Al"},
                               {"name": "q2", "state": "idle"}])
        fakes.append(q)
        qh = "q=127.0.0.1:%d" % q.port
        r = dch("-ls", "--remote", "--hosts", qh)
        lines = r.out.splitlines()
        check("-ls --remote --hosts: the rows, alias first",
              r.returncode == 0 and "Al (q:q1)" in lines and "q:q2" in lines,
              (r.out, r.err))
        r = dch("--ls", "--remote", "--hosts", qh)
        check("--ls --remote: the same", "q:q2" in r.out.splitlines(), r.out)
        if WIN:
            r = dch("-ls", "--hosts", qh)
            check("Windows: -ls implies --remote",
                  "q:q2" in r.out.splitlines(), (r.out, r.err))
        r = dch("-lj", "--remote", "--hosts", qh)
        check("-lj --remote: host:name<TAB>alias<TAB>0",
              "q:q1\tAl\t0" in r.out.splitlines()
              and "q:q2\t\t0" in r.out.splitlines(), repr(r.out))
        r = dch("--ls-json", "--remote", "--hosts", qh)
        try:
            rows = [x for x in json.loads(r.out) if "host" in x]
        except ValueError:
            rows = []
        check("--ls-json --remote: rows with host, state, stale",
              len(rows) == 2 and rows[0] == {"name": "q1", "alias": "Al",
                                             "host": "q", "state": "",
                                             "stale": False}
              and rows[1]["state"] == "idle", r.out)
        check("--ls-json ends in a newline, no CR", r.out.endswith("]\n")
              and "\r" not in r.out, repr(r.out))
        r = dch("-ls", "--remote")
        check("DCH_NO_REMOTE=1, no --hosts: no rows, exit 0",
              r.returncode == 0 and r.out == "", (r.out, r.err))
        with open(conf, "w") as f:
            f.write("# a comment\nremote_hosts = %s\n" % qh)
        e = dict(env)
        del e["DCH_NO_REMOTE"]
        r = dch("-ls", "--remote", e=e)
        check("the remote_hosts config", "q:q2" in r.out.splitlines(),
              (r.out, r.err))
        with open(conf, "w") as f:
            f.write("remote_hosts = off\n")
        r = dch("-ls", "--remote", e=e)
        check("remote_hosts = off: none", r.returncode == 0 and r.out == "",
              (r.out, r.err))
        os.unlink(conf)
        r = dch("-ls", "--remote", "--hosts", "a b;c")
        check("a bad --hosts entry is refused", r.returncode == 1
              and "bad entry" in r.err, r.err)
        r = dch("--host", "q", "-ls", "--remote")
        check("--host without -a is refused", r.returncode == 1, r.err)

        # --- a hostile server -----------------------------------------
        evil = Fake("list", rows=[
            {"name": "ev\x1b]0;pwn\x07\x1b[31mred", "alias": "a\x1b[2Jb"},
            {"name": "tab\there\nnl", "state": "\x1b[5mworking"},
            {"name": "c1\u009b31m", "alias": "bi\u202edi"},
            {"no": "name"}, {"name": 5}, "junk"])
        fakes.append(evil)
        eh = "evil=127.0.0.1:%d" % evil.port
        r = dch("-ls", "--remote", "--hosts", eh)
        check("hostile names in -ls: no ESC, BEL, tab, C1 or bidi",
              r.returncode == 0 and "\x1b" not in r.out
              and "\x07" not in r.out and "\t" not in r.out
              and "\u009b" not in r.out and "\u202e" not in r.out
              and "evil:ev\\x1b]0;pwn" in r.out
              and "evil:tab\\x09here\\x0anl" in r.out, repr(r.out))
        r = dch("-lj", "--remote", "--hosts", eh)
        check("hostile names in -lj: 3 fields a line, escaped",
              "\x1b" not in r.out and len(r.out.splitlines()) == 3
              and all(len(x.split("\t")) == 3 for x in r.out.splitlines()),
              repr(r.out))
        r = dch("--ls-json", "--remote", "--hosts", eh)
        try:
            rows = [x for x in json.loads(r.out) if "host" in x]
        except ValueError:
            rows = []
        check("hostile names in --ls-json: valid JSON, escaped, the same "
              "value, a bad state dropped",
              "\x1b" not in r.out and "\u009b" not in r.out and len(rows) == 3
              and any(x["name"].startswith("ev\x1b]0;pwn") for x in rows)
              and any(x["alias"] == "bi\u202edi" for x in rows)
              and all(x["state"] == "" for x in rows), repr(r.out))

        # --- a hanging host, then its rows stale ----------------------
        hang = Fake("hang")
        fakes.append(hang)
        t0 = time.time()
        r = dch("-ls", "--remote", "--hosts",
                "%s,h=127.0.0.1:%d" % (qh, hang.port))
        dt = time.time() - t0
        check("a hanging host does not stall -ls (%.2fs)" % dt,
              dt < BUDGET and "q:q2" in r.out.splitlines(), (r.out, r.err))
        r = dch("-ls", "--remote", "--hosts", "q=127.0.0.1:%d" % hang.port)
        check("then its cached rows show stale",
              "q:q2 [stale]" in r.out.splitlines(), (r.out, r.err))
        r = dch("--ls-json", "--remote", "--hosts",
                "q=127.0.0.1:%d" % hang.port)
        check("--ls-json marks them stale",
              '"stale":true' in r.out and '"stale":false' not in r.out, r.out)

        # --- a busy host (#095): transient, its cached rows stale -----
        busy = Fake("busy")
        fakes.append(busy)
        r = dch("-ls", "--remote", "--hosts", "q=127.0.0.1:%d" % busy.port)
        check("a busy host: its cached rows stay, stale",
              "q:q2 [stale]" in r.out.splitlines() and "refused" not in r.err,
              (r.out, r.err))
        r = dch("--hosts", "bz=127.0.0.1:%d" % busy.port, "-a", "bz:x")
        check("-a to a busy host: its message, not a refusal, exit 1",
              r.returncode == 1 and "dch: bz: says: this host is at its "
              "connection limit: try again shortly" in r.err, r.err)

        # --- a newer protocol -----------------------------------------
        newer = Fake("list", rows=[{"name": "n1"}], proto="2.0")
        fakes.append(newer)
        r = dch("-ls", "--remote", "--hosts", "nw=127.0.0.1:%d" % newer.port)
        check("a newer major: skipped, said on stderr",
              "nw:n1" not in r.out and "speaks protocol 2.0" in r.err,
              (r.out, r.err))

        # --- a host whose gate refuses us (#094) -----------------------
        refuse = Fake("refuse")
        fakes.append(refuse)
        r = dch("-ls", "--remote", "--hosts", "q=127.0.0.1:%d" % refuse.port)
        check("a refusing host: none of its cached rows, why on stderr",
              "q:" not in r.out and "dch: q refused: this host serves only "
              "its owner's devices" in r.err, (r.out, r.err))
        r = dch("--hosts", "rf=127.0.0.1:%d" % refuse.port, "-a", "rf:x")
        check("-a to a refusing host: its reason, exit 1", r.returncode == 1
              and "dch: rf refused: this host serves only" in r.err, r.err)

        # --- attach: the checks before the console --------------------
        r = dch("--hosts", qh, "-a", "nope:x")
        check("an unknown host: a clear error, exit 1", r.returncode == 1
              and "nope is not a known host" in r.err
              and "known: q" in r.err, r.err)
        r = dch("-a", "nope:x")
        check("DCH_NO_REMOTE=1: no hosts", r.returncode == 1
              and "remote hosts are off" in r.err, r.err)
        r = dch("--hosts", qh, "-a", "q:gone")
        check("-a host:missing says so", r.returncode == 1
              and "no session gone on q" in r.err, r.err)
        r = dch("--hosts", qh, "-a", "q:q1")
        check("-a with no terminal: refused after the list, exit 1",
              r.returncode == 1 and "attaching requires a terminal" in r.err,
              r.err)
        r = dch("--hosts", qh, "--host", "q", "-a", "q1")
        check("--host q -a q1: the same path",
              r.returncode == 1 and "attaching requires a terminal" in r.err,
              r.err)
        if WIN:
            r = dch("--hosts", qh, "-a", "q1")
            check("Windows: -a wants host:name", r.returncode == 1
                  and "host:name" in r.err, r.err)
            r = dch("--hosts", qh)
            check("Windows: dch alone with no console: the picker says so",
                  r.returncode == 1 and "needs a console" in r.err, r.err)

        # --- the picker in a console (#090) ---------------------------
        if WIN:
            nb = len(q.bodies)
            r = subprocess.run([sys.executable, os.path.abspath(__file__),
                                "--console-picker", qh], env=env,
                               capture_output=True, timeout=120)
            try:
                res = json.loads(r.stdout)
            except ValueError:
                res = {}
            why = (res.get("error"), res.get("screen"), r.stderr)
            check("picker: dch alone shows the remote rows", res.get("picker"),
                  why)
            got = q.bodies[nb:]
            check("picker: j, Enter attaches the second row",
                  res.get("attached") and got
                  and res["second"].encode() in got[0], (got, why))
            check("picker: Ctrl-\\ twice, back to the picker",
                  res.get("switch"), why)
            check("picker: Enter there attaches the first row, one Ctrl-\\ "
                  "ends dch (exit 0)", len(got) == 2
                  and res["first"].encode() in got[1]
                  and res.get("tap_rc") == 0, (got, why))
            check("picker: Esc quits (exit 0)", res.get("esc_rc") == 0, why)

        # --- discovery, against a fake tailscale ----------------------
        if not fake_tailscale(fake):
            print("  skip discovery: no gcc for a fake tailscale.exe")
        else:
            put("status.json", PEERS)
            put("whois.json", WHOIS)
            de = dict(env, PATH=fake + os.pathsep + env.get("PATH", ""))
            del de["DCH_NO_REMOTE"]
            r = dch("-a", "nope:x", e=de)
            check("discovery: only the user's online untagged peers",
                  r.returncode == 1 and "known: mybox)" in r.err, r.err)
            put("status.json", dict(PEERS, BackendState="Stopped"))
            r = dch("-a", "nope:x", e=de)
            check("discovery: none unless BackendState is Running",
                  r.returncode == 1 and "none:" in r.err, r.err)
            put("status.json", PEERS)
            put("whois.json", dict(WHOIS, UserProfile={
                "ID": 7, "LoginName": "them@example.com"}))
            r = dch("-a", "mybox:x", e=de)
            check("a discovered host, whois another user: refused before "
                  "connecting", r.returncode == 1
                  and "tailscale whois" in r.err, r.err)
            os.unlink(os.path.join(fake, "whois.json"))
            r = dch("-a", "mybox:x", e=de)
            check("a discovered host, whois failing: refused",
                  r.returncode == 1 and "tailscale whois" in r.err, r.err)
            put("status.json", OWNER)
            with open(os.path.join(fake, "hang"), "w"):
                pass
            t0 = time.time()
            r = dch("-ls", "--remote", e=de)
            dt = time.time() - t0
            check("a hanging tailscale is cut off at the box (%.2fs)" % dt,
                  dt < BUDGET and r.returncode == 0 and r.out == "",
                  (r.out, r.err))
    finally:
        for f in fakes:
            f.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print("all ok" if not fails else "%d FAILED" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--console-picker"]:
        sys.exit(console_picker(sys.argv[2]))
    sys.exit(main())
