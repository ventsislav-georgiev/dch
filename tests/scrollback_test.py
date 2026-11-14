#!/usr/bin/env python3
"""Attach replay with scrollback (#101): --scrollback N / DCH_REPLAY_SCROLLBACK.

MSG_ATTACH's len asks for N rows of the mirror's history; the master prints
them from the top of the cleared screen and scrolls them off with line feeds
on the bottom row, so they land in the client terminal's own scrollback, then
replays the screen exactly as before.

Checks, on the wire (a raw socket to the master): N=0 is today's replay, byte
for byte; N=5 is the 5 newest history rows, the line feeds, then that same
replay; N past the history gives what exists, and a len past the cap is
clamped; the alternate screen gives no history; a history past the replay
budget is cut to its newest rows and the client is not dropped; a blank
screen reads empty. Through a
real terminal (the attach client running inside another dch session, whose
mirror is the terminal): --scrollback 5 and DCH_REPLAY_SCROLLBACK=5 leave
exactly those rows above the screen, with no blank row between; none leaves
the screen alone. In every build (lite too): an attach that asks for
scrollback still attaches and types. An old master (a fake that frames
packets as 1.38.0 does) still gets ATTACH, the sizing REDRAW and the typed
keys as whole packets, and shows its screen; with DCH_OLD_MASTER=<an old dch>
the real one is attached too. Self-isolating (own XDG_RUNTIME_DIR).

Run:  python3 tests/scrollback_test.py            (uses ./dch)
"""
import os, sys, time, json, fcntl, struct, termios, socket, select
import shutil, signal, tempfile, threading, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
DUE = 30.0 if os.environ.get("DCH_TEST_NO_BUDGET") else 8.0
MSG_PUSH, MSG_ATTACH, MSG_DETACH, MSG_WINCH, MSG_REDRAW = 0, 1, 2, 3, 4
REDRAW_WINCH = 3
HOME_ERASE = b"\x1b[H\x1b[J"
MID = b"\x1b[0m\x1b[999H"
OUT_CAP = 4 * 1024 * 1024

TMP = tempfile.mkdtemp(dir=os.environ.get("DCH_TEST_TMP", "/tmp"), prefix="dchsb")
ENV = dict(os.environ, XDG_RUNTIME_DIR=TMP, XDG_STATE_HOME=TMP + "/state",
           HOME=TMP + "/home", DCH_CONFIG=TMP + "/config",
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", SHELL="/bin/sh")
for k in ("DCH_SOCKET_DIR", "DCH_SESSION", "DCH_REPLAY_SCROLLBACK",
          "DCH_NO_REPLAY", "DCH_NO_VT", "DCH_SCROLLBACK"):
    ENV.pop(k, None)
SOCKDIR = os.path.join(TMP, "dch-%d" % os.getuid())
fails = []


def dch(*args, env=None):
    return subprocess.run([DCH] + list(args), env=env or ENV,
                          capture_output=True, text=True, errors="replace",
                          timeout=60)


def expect(cond, what, extra=None):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)
        if extra is not None:
            print("       " + repr(extra)[:1500])
    return cond


def wait_for(pred, deadline):
    """pred() until true; the deadline is taken BEFORE the action."""
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return pred()


def screen(name, *extra):
    return dch("--read", name, *extra).stdout


def pkt(t, length=0, payload=b""):
    return struct.pack("<BH", t, length) + payload


def ws(cols, rows):
    return struct.pack("HHHH", rows, cols, 0, 0)


def recv_until_idle(s, until, deadline, idle=0.4):
    """Read s until `until` was seen and nothing more came for `idle` s."""
    out = b""
    seen_at = None
    while time.time() < deadline:
        if seen_at is None and until in out:
            seen_at = True
        r, _, _ = select.select([s], [], [], idle if seen_at else 0.1)
        if not r:
            if seen_at:
                break
            continue
        c = s.recv(1 << 20)
        if not c:
            break
        out += c
    return out


def raw_attach(name, n, cols=40, rows=10, until=b"END", then=None):
    """MSG_ATTACH(len=n) + the sizing REDRAW on a raw socket: the replay."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(os.path.join(SOCKDIR, name + ".sock"))
    s.sendall(pkt(MSG_ATTACH, n) + pkt(MSG_REDRAW, REDRAW_WINCH,
                                        ws(cols, rows)))
    out = recv_until_idle(s, until, time.time() + DUE)
    if then:
        s.sendall(pkt(MSG_PUSH, len(then[0]), then[0]))
        out += recv_until_idle(s, then[1], time.time() + DUE, 0.1)
    s.sendall(pkt(MSG_DETACH))
    s.close()
    return out


def rows_text(lo, hi):
    return b"\r\n".join(b"%d" % i for i in range(lo, hi + 1))


def spawn(name, script, size="40x10"):
    r = dch("--spawn", name, "--size", size, "--", "sh", "-c", script)
    expect(r.returncode == 0, "spawn %s" % name, r.stderr)


def nested(outer, inner, args, env_extra=None):
    """The attach client inside another session: that session's mirror is
    the client's terminal. Returns the outer's --read --recent lines."""
    cmd = ["env", "-u", "DCH_SESSION"] + \
          ["%s=%s" % kv for kv in (env_extra or {}).items()] + \
          [DCH, "-E", "-a", inner] + args
    r = dch("--spawn", outer, "--size", "40x10", "--", *cmd)
    expect(r.returncode == 0, "spawn %s" % outer, r.stderr)
    # END is the replay's last row: once it shows, every row before it has
    # landed in the outer's mirror (no settle sleep needed)
    got = wait_for(lambda: "END" in screen(outer), time.time() + DUE)
    lines = screen(outer, "--recent", "200").splitlines()
    dch("-k", outer)
    return lines if got else ["(no END on %s)" % outer] + lines


class OldMaster:
    """A session socket framing packets as 1.38.0's master does: ATTACH and
    DETACH carry no payload whatever len says, WINCH/REDRAW a winsize, the
    rest len bytes. Reads whole packets; answers ATTACH with a screen."""

    def __init__(self, name):
        self.path = os.path.join(SOCKDIR, name + ".sock")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        self.got = []
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 3:
                    t, n = buf[0], buf[1] | buf[2] << 8
                    plen = 8 if t in (MSG_WINCH, MSG_REDRAW) else \
                        0 if t in (MSG_ATTACH, MSG_DETACH) else n
                    if len(buf) < 3 + plen:
                        break
                    self.got.append((t, n, buf[3:3 + plen]))
                    buf = buf[3 + plen:]
                    if t == MSG_ATTACH:
                        c.sendall(b"OLD-MASTER-SCREEN\r\n")
        except OSError:
            return

    def close(self):
        self.srv.close()
        os.unlink(self.path)


def client(args, cols=40, rows=10, env=None):
    pid, fd = os.forkpty()
    if pid == 0:
        # sized before exec: set from the parent, it races the client's
        # first REDRAW (a 0x0 one, then a WINCH)
        fcntl.ioctl(0, termios.TIOCSWINSZ, ws(cols, rows))
        os.execve(DCH, [DCH, "-E"] + args, env or ENV)
        os._exit(127)
    return pid, fd


def pty_read(fd, until, deadline):
    out = b""
    while time.time() < deadline and until not in out:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                c = os.read(fd, 65536)
            except OSError:
                break
            if not c:
                break
            out += c
    return out


def stop(pid, fd):
    try:
        os.kill(pid, signal.SIGKILL)
        os.close(fd)
        os.waitpid(pid, 0)
    except OSError:
        pass


def main():
    os.makedirs(SOCKDIR, mode=0o700, exist_ok=True)
    mirror = dch("--build").stdout.strip() == "full"
    hist = "seq 1 40; printf END; exec sleep 300"

    # --- every build: asking for scrollback still attaches and types -----
    spawn("sbsh", "exec sh", "80x24")
    out = raw_attach("sbsh", 5, 80, 24, until=b"\0none",
                     then=(b"echo SB-$((6*7))\r", b"SB-42"))
    expect(b"SB-42" in out, "ATTACH len=5: attached, input reaches the "
           "program", out[-300:])
    dch("-k", "sbsh")

    # --- an old master (1.38.0 framing) -----------------------------------
    old = OldMaster("sbold")
    pid, fd = client(["-a", "sbold", "--scrollback", "5"])
    out = pty_read(fd, b"OLD-MASTER-SCREEN", time.time() + DUE)
    os.write(fd, b"typed")
    wait_for(lambda: any(t == MSG_PUSH for t, _, _ in old.got),
             time.time() + DUE)
    stop(pid, fd)
    kinds = [(t, n, p) for t, n, p in old.got]
    expect(b"OLD-MASTER-SCREEN" in out, "old master: the client shows its "
           "screen", out)
    expect(len(kinds) >= 3 and kinds[0] == (MSG_ATTACH, 5, b"")
           and kinds[1][0] == MSG_REDRAW
           and kinds[1][2] == ws(40, 10)
           and (MSG_PUSH, 5, b"typed") in kinds,
           "old master: ATTACH(len 5), the sizing REDRAW and the keys arrive "
           "as whole packets", kinds)
    old.close()

    olddch = os.environ.get("DCH_OLD_MASTER")
    if olddch:
        # a real master from before #101 (e.g. a released dch)
        r = subprocess.run([olddch, "--spawn", "sbreal", "--size", "40x10",
                            "--", "sh", "-c", hist], env=ENV,
                           capture_output=True, text=True, timeout=30)
        expect(r.returncode == 0, "old master spawn", r.stderr)
        subprocess.run([olddch, "--read", "sbreal"], env=ENV,
                       capture_output=True, timeout=30)
        lines = nested("sbrealo", "sbreal", ["--scrollback", "5"])
        expect(lines == [str(i) for i in range(32, 41)] + ["END"],
               "a real old master (%s): attaches, the screen and nothing "
               "above it" % olddch, lines)
        subprocess.run([olddch, "-k", "sbreal"], env=ENV,
                       capture_output=True, timeout=30)

    if not mirror:
        print("  skip replay checks (build has no terminal mirror)")
        return finish()

    # --- a blank screen reads as nothing (the snapshot refactor's guard) ---
    spawn("sbblank", "exec sleep 300")
    reads = [subprocess.run([DCH, "--read", "sbblank"], env=ENV,
                            capture_output=True, timeout=30).stdout
             for _ in range(10)]
    expect(all(not r.strip() for r in reads), "a blank screen reads empty",
           [r for r in reads if r.strip()][:1])
    dch("-k", "sbblank")

    # --- on the wire -------------------------------------------------------
    spawn("sbh", hist)
    dl = time.time() + DUE
    expect(wait_for(lambda: "END" in screen("sbh"), dl), "history session up")
    r0 = raw_attach("sbh", 0)
    today = HOME_ERASE + rows_text(32, 40) + b"\r\nEND\x1b[0m\x1b[10;4H"
    expect(r0 == today, "N=0: today's replay, byte for byte", r0)
    r5 = raw_attach("sbh", 5)
    expect(r5 == HOME_ERASE + rows_text(27, 31) + MID + b"\n" * 5 + r0,
           "N=5: the 5 newest history rows, 5 line feeds, then the "
           "same screen", r5)
    rall = raw_attach("sbh", 10000)
    expect(rall == HOME_ERASE + rows_text(1, 31) + MID + b"\n" * 10 + r0,
           "N past the history: all 31 rows, a screenful of line feeds",
           rall)
    expect(raw_attach("sbh", 65535) == rall, "len past 10000 is clamped")

    # --- the alternate screen has no history to send -----------------------
    spawn("sbalt", "seq 1 40; printf '\\033[?1049hALT'; exec sleep 300")
    dl = time.time() + DUE
    expect(wait_for(lambda: "ALT" in screen("sbalt"), dl), "alt session up")
    ra = raw_attach("sbalt", 5, until=b"ALT")
    expect(ra.startswith(b"\x1b[?1049h") and MID not in ra
           and b"40" not in ra, "alternate screen: modes and screen only",
           ra)
    dch("-k", "sbalt")

    # --- past the byte budget: the newest rows, the client kept -----------
    gen = ("import sys\n"
           "for i in range(400):\n"
           "    sys.stdout.write('L%04d' % i + ''.join("
           "'\\033[3%dmx' % (k % 7 + 1) for k in range(990)) + "
           "'\\033[0m\\r\\n')\n"
           "sys.stdout.write('BIGEND')\nsys.stdout.flush()\n")
    with open(os.path.join(TMP, "gen.py"), "w") as f:
        f.write(gen)
    r = dch("--spawn", "sbbig", "--size", "1000x10", "--", "sh", "-c",
            "%s %s/gen.py; exec sleep 300" % (sys.executable, TMP))
    expect(r.returncode == 0, "spawn sbbig", r.stderr)
    dl = time.time() + DUE
    expect(wait_for(lambda: "BIGEND" in screen("sbbig"), dl), "big session up",
           screen("sbbig")[:300])
    rb = raw_attach("sbbig", 10000, 1000, 10, until=b"BIGEND")
    expect(len(rb) <= OUT_CAP // 2 and MID in rb and b"BIGEND" in rb
           and b"L0000" not in rb and b"L0390" in rb,
           "a history past the budget: cut to the newest rows, under "
           "OUT_CAP/2, the screen after it (%d bytes)" % len(rb),
           rb[:200])
    dch("-k", "sbbig")

    # --- through a terminal: rows land in its scrollback -------------------
    want5 = [str(i) for i in range(27, 41)] + ["END"]
    lines = nested("sbo1", "sbh", ["--scrollback", "5"])
    expect(lines == want5, "--scrollback 5: those rows above the screen, "
           "no gap", lines)
    lines = nested("sbo2", "sbh", [], {"DCH_REPLAY_SCROLLBACK": "5"})
    expect(lines == want5, "DCH_REPLAY_SCROLLBACK=5 does the same", lines)
    lines = nested("sbo3", "sbh", [])
    expect(lines == [str(i) for i in range(32, 41)] + ["END"],
           "no scrollback asked: the screen alone", lines)
    lines = nested("sbo4", "sbh", ["--scrollback", "0"],
                   {"DCH_REPLAY_SCROLLBACK": "5"})
    expect(lines == [str(i) for i in range(32, 41)] + ["END"],
           "--scrollback 0 wins over the variable", lines)
    r = dch("-a", "sbh", "--scrollback", "x")
    expect(r.returncode == 1 and "--scrollback wants" in r.stderr,
           "--scrollback x is refused", r.stderr)
    dch("-k", "sbh")
    return finish()


def finish():
    dch("-kl")
    shutil.rmtree(TMP, ignore_errors=True)
    print("%d failure(s)" % len(fails) if fails else "ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
