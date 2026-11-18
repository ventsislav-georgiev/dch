"""Shared by the three files of the X-ray view of a remote session (#188,
XR5c): xray_remote_view_test.py, xray_remote_fakes_test.py and
xray_remote_chord_test.py. Each runs alone, with its own temp dir, its own
loopback dch serve and its own client: `dch --xray host:name` and the
picker's x on a remote row.

A real `dch serve` runs on 127.0.0.1 (DCH_SERVE_TEST_LOOPBACK=1, --bind
127.0.0.1 --port 0, a fake `tailscale` first on PATH), its sessions in their
own socket and state dirs. The client has others and names the host in
remote_hosts (dch --xray takes no --hosts), without DCH_NO_REMOTE. Fake
servers on loopback play a hostile host, one without "xray", one whose gate
refuses us; a closed port is an unreachable one. Nothing else is dialled.
Each view runs on a pty this test owns (pty.fork), so it needs no terminal
mirror on the client.
"""
import fcntl, json, os, pty, re, select, shutil, signal, socket, struct
import subprocess, sys, tempfile, termios, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from serve_lib import fake_ts  # noqa: E402
from remote_client_test import (OWNER, REFUSAL, WHOIS, Fake,  # noqa
                                frame)

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
RSID = "11111111-2222-4333-8444-5555555555c8"
RESTORE = b"\033[?1049l"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + repr(detail)[-900:])


T = os.path.realpath(tempfile.mkdtemp(prefix="xrv", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
FAKE = os.path.join(T, "fake")
os.makedirs(FAKE)
fake_ts(FAKE)
for nm, o in (("status.json", OWNER), ("whois.json", WHOIS)):
    with open(os.path.join(FAKE, nm), "w") as f:
        f.write(json.dumps(o))
BASE = {k: v for k, v in os.environ.items()
        if not k.startswith(("DCH_", "Malloc", "CLAUDE", "NO_COLOR",
                             "COLORTERM"))}


def side(name, **kw):
    d = os.path.join(T, name)
    os.makedirs(os.path.join(d, "home"))
    e = dict(BASE, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=os.path.join(d, "st"),
             HOME=os.path.join(d, "home"), DCH_LOGIN_ENV="0",
             DCH_CONFIG=os.path.join(d, "config"), SHELL="/bin/sh",
             PATH=FAKE + ":/usr/bin:/bin", TERM="xterm-256color",
             NO_COLOR="1")
    e.update(kw)
    return e


SENV = side("srv")
CENV = side("cli")
# the picker probes the host once inside --ls's box (300 ms): a loaded
# machine or a sanitizer build misses it and the picker says "no sessions"
CENV["DCH_REMOTE_BOX_MS"] = "5000"
SLOG = os.path.join(T, "serve.log")


def hosts(line):
    with open(CENV["DCH_CONFIG"], "w") as f:
        f.write("remote_hosts = %s\n" % line)


def dch(env, *a):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=env, stdin=subprocess.DEVNULL)


class Pty:
    """A program on a pty: out holds every byte it wrote."""

    def __init__(self, argv, rows=30, cols=100, env=None):
        self.out = b""
        self.rc = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            os.execve(argv[0], argv, env or CENV)
        # pty.fork()'s master is inheritable: the next kid would hold it,
        # and closing ours would not hang this pty up (QA 1.59.0 MED-T2)
        os.set_inheritable(self.fd, False)

    def pump(self, secs=0.05):
        end = time.time() + secs
        while True:
            if time.time() >= end:
                return
            r, _, _ = select.select([self.fd], [], [],
                                    max(0, end - time.time()))
            if not r:
                return
            try:
                b = os.read(self.fd, 65536)
            except OSError:
                b = b""
            if not b:
                self.reap()
                return
            self.out += b

    def reap(self):
        if self.rc is None:
            p, st = os.waitpid(self.pid, os.WNOHANG)
            if p:
                self.rc = os.waitstatus_to_exitcode(st)
        return self.rc

    def wait(self, what, secs=10.0, start=0):
        end = time.time() + secs
        while what not in self.out[start:] and time.time() < end:
            self.pump(0.1)
            if self.rc is not None:
                break
        return what in self.out[start:]

    def key(self, b, settle=0.3, want=None):
        """Without want, a fixed settle. With want: until want is drawn since
        the key (a remote answer comes after a round trip, so not on quiet
        alone), then until 0.1 s of quiet, at most settle more (#393)."""
        mark = len(self.out)
        try:
            os.write(self.fd, b)
        except OSError:
            pass    # it has exited: the checks say so
        if want is None:
            self.pump(settle)
            return
        self.wait(want, 10, start=mark)
        end = time.time() + settle
        while self.rc is None and time.time() < end:
            n = len(self.out)
            self.pump(min(0.1, end - time.time()))
            if len(self.out) == n:
                return

    def resize(self, rows, cols):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))

    def exit(self, secs=10.0):
        end = time.time() + secs
        while self.reap() is None and time.time() < end:
            self.pump(0.1)
        self.pump(0.1)
        return self.rc

    def close(self):
        # the fd first: on macOS a killed pty leader with output unread
        # cannot finish exiting, so the waitpid would never return
        os.close(self.fd)
        if self.reap() is None:
            os.kill(self.pid, signal.SIGKILL)
            end = time.time() + 10
            while self.reap() is None and time.time() < end:
                time.sleep(0.05)
            if self.rc is None:
                print("  leaving pty child %d unreaped" % self.pid)


def ctls(what):
    with open(SLOG) as f:
        return f.read().count(" ctl %s " % what)


def refused():
    """The host's gate refusals: a whois past its 3 s is refused and cached
    30 s, and no ctl of that window is logged (#249). For failure details."""
    with open(SLOG) as f:
        return [l for l in f.read().splitlines() if ": refused" in l][-3:]


def serve():
    with open(SLOG, "w") as lf:
        sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                               "--port", "0"],
                              env=dict(SENV, DCH_SERVE_TEST_LOOPBACK="1"),
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=lf)
    end = time.time() + 30     # a loaded machine: room, still bounded
    while time.time() < end and sp.poll() is None:
        m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(SLOG).read())
        if m:
            return sp, int(m.group(1))
        time.sleep(0.05)
    return sp, None


# ---- the host's corpus: a History Claude record, hostile text -------------

TITLE = ("look at #831 and o/r#7 \033]0;pwned\007 evil‮title "
         "al؜m ls ep")


def fixture():
    cfg = os.path.join(SENV["HOME"], ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    out = ("# #831 Canary \033[31mred\033[0m passage\n"
           "See #831 again and؜ PROJ-5")
    lines = [
        {"type": "user", "cwd": "/home/demo/proj",
         "timestamp": "2026-11-10T10:00:00Z", "message": {"content": TITLE}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "gh issue view 831"}}]}},
        {"type": "user", "toolUseResult": {"stdout": out},
         "message": {"content": [{"type": "tool_result",
                                  "tool_use_id": "b1", "content": out}]}},
        {"type": "user", "cwd": "/home/demo/proj",
         "timestamp": "2026-11-10T11:00:00Z",
         "message": {"content": "second prompt about PROJ-5"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "noted"}]}}]
    with open(os.path.join(pdir, RSID + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(SENV["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid())
    hdir = os.path.join(SENV["XDG_STATE_HOME"], "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": "xv-cl", "cwd": "/home/demo/proj", "end": "exited",
           "argv": ["claude"], "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": RSID, "cwd": "/home/demo/proj",
                     "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-cl.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def shell():
    with open(SENV["DCH_CONFIG"], "w") as f:
        f.write("xray_ring = 64K\n")
    prog = ("while :; do printf '\\033]133;A\\007$ \\033]133;B\\007'; "
            "read -r cmd || exit; printf '\\033]133;C\\007'; eval \"$cmd\"; "
            "printf '\\033]133;D;0\\007'; done")
    r = dch(SENV, "--spawn", "xv-sh", "--size", "80x24", "sh", "-c", prog)
    check("host: the shell spawned", r.returncode == 0, r.stderr)
    for _ in range(2):
        time.sleep(0.4)
        dch(SENV, "--send", "xv-sh", "echo 'see o/r#41'\n")
    time.sleep(0.5)


def shown(v, mark):
    """The text last drawn on each row since mark, its escapes removed."""
    got, parts = {}, re.split(rb"\033\[(\d+);1H", v.out[mark:])
    for i in range(1, len(parts) - 1, 2):
        got[int(parts[i])] = re.sub(rb"\033\[[0-9;?]*[A-Za-z]", b"",
                                    parts[i + 1]).decode("utf-8", "replace")
    return "\n".join(got[k] for k in sorted(got))


def record_o(name, sid, lines, proj="-home-demo-proj"):
    """A History Claude record of the host's, its transcript lines."""
    cfg = os.path.join(SENV["HOME"], ".claude")
    pdir = os.path.join(cfg, "projects", proj)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(SENV["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid())
    hdir = os.path.join(SENV["XDG_STATE_HOME"], "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": name, "cwd": "/home/demo/proj", "end": "exited",
           "argv": ["claude"], "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": sid, "cwd": "/home/demo/proj",
                     "config_dir": cfg}}
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def say_o(text, ts=None):
    o = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}
    if ts:
        o["timestamp"] = ts
    return o


def settle(v, secs=30.0):
    """until the card's scan is done: no row says searching"""
    until(lambda: v.pump(0.2) or not any(
        "searching" in r for r in screen_rows(v).values()), secs)
    v.pump(0.5)


def screen_rows(v):
    """The text last drawn on each row, its escapes removed."""
    got, parts = {}, re.split(rb"\033\[(\d+);1H", v.out)
    for i in range(1, len(parts) - 1, 2):
        got[int(parts[i])] = re.sub(rb"\033\[[0-9;?]*[A-Za-z]", b"",
                                    parts[i + 1]).decode("utf-8", "replace")
    return got


def items(v):
    """Responses' items on screen, top down: (its title, [its lines])."""
    out, scr = [], screen_rows(v)
    for k in sorted(x for x in scr if x >= 4):
        r = scr[k]
        if r.startswith(("\u2500", "-")):
            break
        if re.match(r"^.\s{0,3}\d+  ", r):
            out.append((r.rstrip().split("  ")[-1].strip(), []))
        elif out and r.strip():
            out[-1][1].append(r.strip())
    return out


def head(v):
    sel = [r for r in screen_rows(v).values() if r.startswith("\u258c")]
    return sel[0] if sel else ""


def tabbar(v, want, secs=30.0):
    """The tab bar once it holds want (a count is faint: its own SGR). 30 s:
    beside the other test files the host's --memory list took over 10 s."""
    return until(lambda: (v.pump(0.1), want in screen_rows(v).get(2, ""))[1],
                 secs)


def sgroups(v):
    """Search's turns on screen, top down (#457): (number, title, [(who,
    [snippet lines])]); a head row's time (or its blank) at column 2, a
    place's who at column 7, its snippet's next lines further in."""
    out, scr = [], screen_rows(v)
    for k in sorted(x for x in scr if x >= 5):
        r = scr[k]
        if r.startswith(("\u2500", "-")):
            break
        if re.match(r"^.\s\d\d-\d\d \d\d:\d\d|^.\s{14}\S", r):
            num, _, title = r[15:].rstrip().partition("  ")
            if not num.startswith("#"):
                num, title = "", r[15:].strip()
            out.append((num, title.strip(), []))
        elif out and re.match(r"^.\s{6}\S", r):
            who, _, text = r[7:].rstrip().partition("  ")
            out[-1][2].append((who, [text.strip()]))
        elif out and out[-1][2] and r.strip():
            out[-1][2][-1][1].append(r.strip())
    return out


def shits(v):
    """Search's places on screen, top down: (who, its turn's title,
    [snippet lines]); a turn's head row is none."""
    return [(w, t, ls) for _, t, ps in sgroups(v) for w, ls in ps]


def kids_of(pid):
    """The view's children: {pid: ps state}."""
    o = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,stat="],
                       capture_output=True, text=True).stdout
    return {int(p[0]): p[2] for p in (ln.split() for ln in o.splitlines())
            if len(p) == 3 and p[1] == str(pid)}


def alive(pids):
    """Which of pids still run as a dch (pid reuse aside)."""
    if not pids:
        return []
    o = subprocess.run(["ps", "-o", "pid=,comm=", "-p",
                        ",".join(str(p) for p in pids)],
                       capture_output=True, text=True).stdout
    return [ln.strip() for ln in o.splitlines() if "dch" in ln]


def until(fn, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def host_ran(v, n, what, late):
    """Pump v until the host has logged n ctl --xray: a key that starts the
    next query stops the running one, sent or not (#374); else what goes
    to late, for the failure detail."""
    if not until(lambda: v.pump(0.05) or ctls("--xray") >= n, 30):
        late.append(what)


def run(body, sh=False):
    """main() of each file: serve, the host's xv-cl record (and with sh its
    live shell xv-sh), remote_hosts naming srv, then body(port); exit 1 on
    any failure."""
    sp = None
    try:
        sp, port = serve()
        if port is None:
            # one failure for the serve half, not one per check; a return
            # here skipped the exit status below (#249)
            check("dch serve listens on a loopback test port: %s; its log "
                  "tail:" % ("exited %d" % sp.returncode if sp.poll() is not
                             None else "no listening line in 30 s"), False)
            for l in open(SLOG).read().splitlines()[-12:]:
                print("       " + l)
            print("  skip every other check: they need that serve")
            return 1
        check("dch serve listens on a loopback test port", True)
        fixture()
        if sh:
            shell()
        hosts("srv=127.0.0.1:%d" % port)
        body(port)
    finally:
        dch(SENV, "-k", "xv-sh")
        dch(SENV, "-k", "xv-rx")
        dch(SENV, "-k", "xv-md")
        if sp:
            sp.terminate()
            try:
                sp.wait(10)
            except subprocess.TimeoutExpired:
                sp.kill()
                sp.wait()
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)
