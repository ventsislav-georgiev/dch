#!/usr/bin/env python3
"""The X-ray view of a remote session (#188, XR5c) from the in-attach
chord over `dch -a host:name`.

The host, the fake tailscale and the harness: xray_remote_view_lib.py.

Covers:
  6. the in-attach chord over `dch -a srv:xv-rx` (#189): the detach key then
     x (a later read, one burst, the kitty CSI form, --host srv -a xv-rx,
     detach_key = switch) opens the view of srv:xv-rx over the attach, on
     Refs (#294), the screen read with its own ctl --read; the session never gets the detach
     key or the x; q goes back, the host is asked to redraw (the program's
     SIGWINCH), a key after reaches the session, and the host saw one attach
     (never retired); next to a local srv:xv-rx too (LOW-R2). Off under -E
     and DCH_DOUBLE_TAP_MS=0, as the local chord. dch -d xv-rx on the
     host, or dch -d srv:xv-rx from the client, with the view up: the
     client leaves at once, exit 0, the view and the relay gone, the
     session untouched (QA 1.58.0 MED-R6); a session that ends under the
     view, or whose master is SIGKILLed there (its socket file left, #230),
     leaves it up until q, as before;
  7. the view's failure lines over an attach to a fake host without "xray"
     (exit 6), whose gate refuses the view's connection (exit 4), or whose
     port closes once the attach is in (exit 7): the line, q, any key, and
     the attach goes on (the host gets the redraw, then only the next key);
     an arrow key, a UTF-8 character or a paste as the any key is all
     swallowed (QA 1.58.0 LOW-R8), and so is a paste over 4096 bytes,
     bracketed or not, and one in several writes 0.3 s apart (#230);
  8. the host re-arms on its redraw (#189 round 2): a program on the
     alternate screen with bracketed paste, mouse reporting and kitty flags
     on, over `dch -a srv:xv-md`; after the chord's view the client's output
     past the view's ESC[?1049l holds ESC[?1049h, then the kitty push (on the
     alternate screen), ESC[?2004h, ESC[?1000h and ESC[?1006h; the full build
     repaints the screen from its mirror, dch-lite has none and the program
     repaints on its SIGWINCH. Three SIGUSR2 redraws (0x07, no reset before
     them) re-arm again, each with one kitty pop before its one push, so the
     flag stack does not grow (a per-screen stack model of the whole attach
     ends at 1 on the alternate screen, 0 on the main one); the first attach
     pops nothing. A key after still reaches the session;

Run:  python3 tests/xray_remote_chord_test.py      (uses ./dch)
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xray_remote_view_lib import *  # noqa: E402,F401,F403


# ---- 6: the in-attach chord over a remote attach (#189) ---------------------

# The host's probe session prints each read it gets as RX:<hex>, appends the
# hex to a file and says WINCH on a SIGWINCH (the host's redraw), so a test
# sees exactly the bytes that reached it, dch-lite too.
RXF = os.path.join(T, "rx.hex")
RX = ("import os, signal, sys, tty\n"
      "tty.setraw(0)\n"
      "signal.signal(signal.SIGWINCH,\n"
      "              lambda *a: os.write(1, b'WINCH\\r\\n'))\n"
      "f = open(sys.argv[1], 'a')\n"
      "os.write(1, b'READY\\r\\n')\n"
      "while True:\n"
      "    b = os.read(0, 256)\n"
      "    if not b: break\n"
      "    f.write(b.hex()); f.flush()\n"
      "    os.write(1, b'RX:' + b.hex().encode() + b'\\r\\n')\n")


def rx():
    """Every byte the host's session has received, in order, as hex."""
    try:
        with open(RXF) as f:
            return f.read()
    except OSError:
        return ""


def attaches():
    with open(SLOG) as f:
        return f.read().count(" attach xv-rx (")


def rattach(env=None, extra=(), tail=("-a", "srv:xv-rx")):
    n0 = attaches()
    a = Pty([DCH] + list(extra) + list(tail), env=env)
    until(lambda: attaches() > n0)
    # the client reads keys once it put the terminal in raw mode (#249)
    until(lambda: not termios.tcgetattr(a.fd)[3] & termios.ICANON)
    a.wait(b"READY", 3)
    a.pump(0.5)
    return a, n0


def chord_case(what, keys, env=None, tail=("-a", "srv:xv-rx")):
    a, n0 = rattach(env, tail=tail)
    try:
        got = rx()
        r0 = ctls("--read")
        mark = len(a.out)
        for k in keys:
            a.key(k, 0.05)
        ok = a.wait(b"X-ray  srv:xv-rx", start=mark) and \
            a.wait(b"[Refs", start=mark)
        check("remote chord, %s: the view of srv:xv-rx opens over the attach"
              % what, ok, a.out[mark:][-600:])
        check("remote chord, %s: the client is still running" % what,
              a.reap() is None)
        check("remote chord, %s: Refs (#294) reads the screen over its own "
              "ctl --read" % what,
              until(lambda: a.pump(0.1) or ctls("--read") > r0, 15),
              (ctls("--read") - r0, refused()))
        # two checks (#233, #249): MARK189 goes dch --send -> master -> pty,
        # not through the attach, so its absence is a stalled session, not
        # the chord; a leak is any other byte. The details print even empty
        sent = dch(SENV, "--send", "xv-rx", "MARK189").returncode
        until(lambda: "4d41524b313839" in rx()[len(got):], 5)
        a.pump(0.3)
        rxd = rx()[len(got):]
        check("remote chord, %s: the text sent while the view was up reached "
              "the session (dch --send, not the attach)" % what,
              "4d41524b313839" in rxd, (rxd, sent))
        check("remote chord, %s: nothing else reached the session (no detach "
              "key, no x)" % what,
              rxd.replace("4d41524b313839", "", 1) == "", (rxd, sent))
        if not ok or a.reap() is not None:
            return
        mark = len(a.out)
        a.key(b"q", 0.05)
        check("remote chord, %s: q leaves the view's alternate screen" % what,
              a.wait(RESTORE, start=mark), a.out[mark:][-300:])
        back = a.out.rfind(RESTORE)
        check("remote chord, %s: back on the attach, the host was asked to "
              "redraw (the program got its SIGWINCH)" % what,
              a.wait(b"WINCH", start=back), a.out[back:])
        before = rx()
        a.key(b"z", 0.3)
        check("remote chord, %s: the attach was never retired (a key after "
              "reaches the session, the host saw one attach)" % what,
              until(lambda: rx()[len(before):] == "7a", 5) and
              a.reap() is None and attaches() == n0 + 1,
              (rx()[len(before):], a.rc, attaches() - n0))
    finally:
        a.close()
    time.sleep(0.5)


def test_chord(port):
    hosts("srv=127.0.0.1:%d" % port)
    r = dch(SENV, "--spawn", "xv-rx", "--size", "100x30", sys.executable,
            "-c", RX, RXF)
    check("remote chord: the probe session spawned on the host",
          r.returncode == 0, r.stderr)
    chord_case("x in a later read", [b"\x1c", b"x"])
    chord_case("x in the same burst", [b"\x1cx"])
    chord_case("the kitty CSI form", [b"\x1c", b"\033[120u"])
    chord_case("--host srv -a xv-rx", [b"\x1cx"],
               tail=("--host", "srv", "-a", "xv-rx"))
    # a local session named srv:xv-rx, in its own client dirs: the
    # attach's own view still opens (QA 1.57.0 LOW-R2)
    lenv = side("same-rx", DCH_CONFIG=CENV["DCH_CONFIG"])
    r = dch(dict(lenv, DCH_NO_REMOTE="1"), "--spawn", "srv:xv-rx", "--size",
            "80x24", "sh", "-c", "while :; do sleep 1; done")
    check("remote chord: a local session named srv:xv-rx spawned",
          r.returncode == 0, r.stderr)
    chord_case("--host srv -a xv-rx, a local srv:xv-rx too", [b"\x1cx"],
               lenv, tail=("--host", "srv", "-a", "xv-rx"))
    dch(dict(lenv, DCH_NO_REMOTE="1"), "-k", "srv:xv-rx")
    cfg = os.path.join(T, "config-switch")
    with open(cfg, "w") as f:
        f.write("remote_hosts = srv=127.0.0.1:%d\ndetach_key = switch\n"
                % port)
    chord_case("detach_key = switch", [b"\x1c", b"x"],
               dict(CENV, DCH_CONFIG=cfg))

    # -E: no detach key, no chord; the session gets both bytes
    a, _ = rattach(extra=["-E"])
    try:
        got = rx()
        mark = len(a.out)
        a.key(b"\x1c", 0.05)
        a.key(b"x", 0.5)
        until(lambda: rx()[len(got):] == "1c78", 5)
        check("remote chord, -E: the session receives the detach key and x, "
              "no view", rx()[len(got):] == "1c78" and
              b"X-ray" not in a.out[mark:] and a.reap() is None,
              (rx()[len(got):], a.out[mark:]))
    finally:
        a.close()
    time.sleep(0.5)

    # DCH_DOUBLE_TAP_MS=0: the press detaches at once, no view, nothing
    # reaches the session
    for keys in ([b"\x1c", b"x"], [b"\x1cx"]):
        a, _ = rattach(dict(CENV, DCH_DOUBLE_TAP_MS="0"))
        try:
            got = rx()
            mark = len(a.out)
            a.key(keys[0], 0.05)
            rc = a.exit(5)
            if len(keys) > 1:
                try:
                    os.write(a.fd, keys[1])
                except OSError:
                    pass
            time.sleep(0.5)
            check("remote chord, DCH_DOUBLE_TAP_MS=0, %s: the detach key "
                  "detaches at once (exit 0), the session receives nothing, "
                  "no view" % ("x after" if len(keys) > 1 else "one burst"),
                  rc == 0 and rx()[len(got):] == "" and
                  b"X-ray" not in a.out[mark:],
                  (rc, rx()[len(got):], a.out[mark:]))
        finally:
            a.close()
        time.sleep(0.5)


def test_chord_detach():
    """QA 1.58.0 MED-R6: dch -d xv-rx on the host, or dch -d srv:xv-rx from
    the client, with the chord's view up: the client leaves as a local
    dch -d makes it (#196): the view goes, exit 0, no line, and the session
    gets nothing and lives on. A session that ends under the view leaves
    the view up as before (#217: serve's 0x12 tells the two apart), then q
    exits 0. So does a master SIGKILLed under the view (#230): its socket
    file is left behind, and that is no detach."""
    for how in ("on the host", "-d srv:xv-rx from the client"):
        a, _ = rattach()
        try:
            got = rx()
            mark = len(a.out)
            a.key(b"\x1cx", 0.05)
            ok = a.wait(b"X-ray  srv:xv-rx", start=mark)
            check("remote -d, %s: the view opens" % how, ok,
                  a.out[mark:][-300:])
            kids = list(kids_of(a.pid))
            mark = len(a.out)
            r = dch(SENV, "-d", "xv-rx") if how == "on the host" else \
                dch(CENV, "-d", "srv:xv-rx")
            check("remote -d, %s: it says it detached one client" % how,
                  r.returncode == 0 and "detached 1 client" in r.stdout,
                  (r.returncode, r.stdout, r.stderr))
            # the bug keeps the view up until q: 15 s is far past a detach
            rc = a.exit(15)
            tail = a.out[mark:]
            check("remote -d, %s: the client leaves without q, exit 0, no "
                  "line" % how, rc == 0 and b"lost the connection" not in
                  tail, (rc, tail[-300:]))
            check("remote -d, %s: the view's alternate screen was left" % how,
                  RESTORE in tail, tail[-300:])
            check("remote -d, %s: the view and the relay are gone" % how,
                  kids and until(lambda: not alive(kids), 5), kids)
            dch(SENV, "--send", "xv-rx", "alive")
            check("remote -d, %s: the session got nothing and lives on" % how,
                  until(lambda: rx()[len(got):] == "616c697665", 5),
                  rx()[len(got):])
        finally:
            a.close()
        time.sleep(0.5)

    # the session ends under the view: the view stays up, as before
    for nm, how in (("xv-re", "end"), ("xv-rk", "SIGKILL")):
        chord_end(nm, how)


def chord_end(nm, how):
    """The session nm ends under the chord's view: its program exits, or
    (#230) its master is SIGKILLed, leaving its socket file."""
    what = "remote end" if how == "end" else "remote SIGKILL"
    r = dch(SENV, "--spawn", nm, "--size", "100x30", "sh", "-c",
            "echo READY; read x; echo BYE")
    check("%s: the session spawned on the host" % what, r.returncode == 0,
          r.stderr)
    a = Pty([DCH, "-a", "srv:" + nm])
    try:
        a.wait(b"READY")
        a.pump(0.5)
        mark = len(a.out)
        r0 = ctls("--read")
        a.key(b"\x1cx", 0.05)
        # its screen read before the end (dch-lite: read, no mirror): a
        # view slower than that (ASan) says "no such session" itself.
        # Refs draws no screen (#294): the host's ctl --read says it read
        a.wait(("X-ray  srv:" + nm).encode(), start=mark)
        check("%s: the view opens and reads the screen" % what,
              until(lambda: a.pump(0.1) or ctls("--read") > r0),
              (ctls("--read") - r0, a.out[mark:][-300:]))
        mark = len(a.out)
        sock = "/%s.sock -- " % nm
        pids = [int(ln.split()[0]) for ln in subprocess.run(
                ["ps", "-eo", "pid,command"], capture_output=True,
                text=True).stdout.splitlines()
                if "--master-of" in ln and T in ln and sock in ln]
        if how == "end":
            dch(SENV, "--send", nm, "go\n")
        elif len(pids) == 1:
            os.kill(pids[0], signal.SIGKILL)	# this test's session's master
        ended = until(lambda: " %s exited" % nm in open(SLOG).read())
        a.pump(3)
        check("%s: the session ended under the view; the view stays" % what,
              ended and a.reap() is None and RESTORE not in a.out[mark:],
              (ended, pids, a.rc, a.out[mark:][-300:]))
        a.key(b"q", 0.3)
        rc = a.exit(10)
        check("%s: q leaves, exit 0, no line" % what, rc == 0 and
              b"lost the connection" not in a.out[mark:],
              (rc, a.out[mark:][-300:]))
    finally:
        a.close()
        dch(SENV, "-k", nm)
    time.sleep(0.5)


# ---- 7: the view's failure lines over a remote attach ----------------------

class Attachable(Fake):
    """A host without "xray": its first connection is the attach, its 0x10
    keys kept in keys and its 0x07 redraws counted; a later one (the view's)
    is Fake's, `mode` "ok" (a hello without "xray") or "refuse" (the gate's
    0x13)."""

    def __init__(self, mode):
        self.keys, self.redraws = b"", 0
        super().__init__(mode, rows=[{"name": "fk"}],
                         ops=["ctl", "ctl-cancel"])

    def serve(self, c):
        if c is not self.conns[0]:
            return Fake.serve(self, c)
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
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": "1.0",
                                               "server": "fake",
                                               "ops": self.ops}))
                    elif t == 0x03:
                        c.sendall(frame(0x11, self.rows))
                    elif t == 0x01:
                        self.attached.set()
                        c.sendall(frame(0x10, b"FAKE-SCREEN\r\n"))
                    elif t == 0x10:
                        self.keys += body
                    elif t == 0x07:
                        self.redraws += 1
        except OSError:
            return


def chord_fail(what, mode, line, close=False, anykey=b" "):
    h = Attachable(mode)
    hosts("q=127.0.0.1:%d" % h.port)
    a = Pty([DCH, "-a", "q:fk"])
    try:
        ok = a.wait(b"FAKE-SCREEN") and h.attached.wait(5)
        if close:
            # the attach is in; the view's dial is refused. Linux keeps a
            # listener accepting while a thread sits in accept(): shutdown
            # ends it there (macOS says ENOTCONN and close() is enough)
            try:
                h.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            h.sock.close()
        a.pump(0.5)
        mark = len(a.out)
        a.key(b"\x1cx", 0.5)
        check("remote chord, %s: the view opens with its line" % what,
              ok and a.wait(line.encode(), start=mark), a.out[mark:][-600:])
        mark = len(a.out)
        a.key(b"q", 0.5)
        check("remote chord, %s: q says the line and waits for a key" % what,
              a.wait(b"(any key: back to the session)", start=mark) and
              a.reap() is None, a.out[mark:][-300:])
        for k in anykey if isinstance(anykey, list) else [anykey]:
            a.key(k, 0.3)
        a.pump(0.2)
        a.key(b"z", 0.3)
        until(lambda: h.keys == b"z", 5)
        check("remote chord, %s: a key goes back to the attach: the host got "
              "the redraw, then only the next key (no detach key, x or the "
              "key that left)" % what, h.keys == b"z" and h.redraws == 1 and
              a.reap() is None, (h.keys, h.redraws, a.rc))
    finally:
        a.close()
        h.close()


def test_chord_fail():
    chord_fail("a host without xray (exit 6)", "ok",
               "dch: q: does not support --xray (needs dch serve 1.55.0+)")
    chord_fail("a gate that refuses the view (exit 4)", "refuse",
               "dch: q refused: " + REFUSAL["message"])
    chord_fail("an unreachable host (exit 7)", "ok", "dch: q: unreachable: ",
               close=True)
    # QA 1.58.0 LOW-R8: the any key is the whole read, none of it for the
    # session
    for what, k in (("an arrow key", b"\033[A"),
                    ("a UTF-8 character", "\u00a9".encode()),
                    ("a paste", b"\033[200~pasted text\033[201~"),
                    # #230: past the one 4096-byte read, and over several
                    ("a bracketed paste of 10000 bytes",
                     b"\033[200~" + b"p" * 10000 + b"\033[201~"),
                    ("a plain paste of 10000 bytes", b"q" * 10000),
                    ("a bracketed paste in three writes 0.3 s apart",
                     [b"\033[200~" + b"a" * 3000, b"b" * 3000,
                      b"c" * 3000 + b"\033[201~"])):
        chord_fail("%s as the any key" % what, "ok",
                   "dch: q: does not support --xray (needs dch serve "
                   "1.55.0+)", anykey=k)


# ---- 8: the host re-arms the session's modes on 0x07 (#189 round 2) --------

MD = ("import os, signal, sys, tty\n"
      "tty.setraw(0)\n"
      "signal.signal(signal.SIGWINCH,\n"
      "              lambda *a: os.write(1, b'WINCH\\r\\n'))\n"
      "os.write(1, b'\\033[?1049h\\033[?2004h\\033[?1000h\\033[?1006h'\n"
      "         b'\\033[>1uALTSCREEN\\r\\n')\n"
      "while True:\n"
      "    b = os.read(0, 256)\n"
      "    if not b: break\n"
      "    os.write(1, b'GOT:' + b.hex().encode() + b'\\r\\n')\n")
MODES = (b"\033[?2004h", b"\033[?1000h", b"\033[?1006h", b"\033[>1u")
KSEQ = re.compile(rb"\033\[(\?1049[hl]|>\d*u|<\d*u)")


def kitty_stacks(out):
    """The kitty flag stack depths (main, alt) a terminal ends with after
    out: a stack per screen, 1049h/l switch, ESC[<Nu pops N (default 1)."""
    st, alt = [0, 0], 0
    for m in KSEQ.finditer(out):
        t = m.group(1)
        if t[:1] == b"?":
            alt = int(t[-1:] == b"h")
        elif t[:1] == b">":
            st[alt] += 1
        else:
            st[alt] = max(0, st[alt] - int(t[1:-1] or 1))
    return tuple(st)


def rearmed(out, at):
    """The modes missing from out past at."""
    return [m for m in MODES + (b"\033[?1049h",) if m not in out[at:]]


def test_rearm():
    lite = dch(SENV, "--build").stdout.strip() == "lite"
    r = dch(SENV, "--spawn", "xv-md", "--size", "100x30", sys.executable,
            "-c", MD)
    check("re-arm: the probe session spawned on the host", r.returncode == 0,
          r.stderr)
    a = Pty([DCH, "-a", "srv:xv-md"])
    try:
        # dch-lite keeps no screen: the attach re-arms the modes only
        ok = a.wait(b"\033[>1u" if lite else b"ALTSCREEN")
        a.pump(0.5)
        check("re-arm: the attach shows the program's modes%s"
              % ("" if lite else " and screen"),
              ok and not rearmed(a.out, 0), a.out[-300:])
        check("re-arm: the first attach pops no kitty flags (push only)",
              b"\033[<" not in a.out and a.out.count(b"\033[>1u") == 1,
              a.out[-300:])
        mark = len(a.out)
        a.key(b"\x1cx", 0.05)
        ok = a.wait(b"X-ray  srv:xv-md", start=mark)
        check("re-arm: the chord opens the view over the attach", ok,
              a.out[mark:][-600:])
        if not ok:
            return
        mark = len(a.out)
        a.key(b"q", 0.5)
        a.wait(RESTORE, start=mark)
        back = a.out.find(RESTORE, mark)
        until(lambda: (a.pump(0.1), not rearmed(a.out, back))[1], 5)
        a.pump(0.5)
        out = a.out
        check("re-arm: after the view's ESC[?1049l the host re-armed "
              "bracketed paste, mouse reporting, kitty flags and 1049h",
              back >= 0 and not rearmed(out, back),
              (rearmed(out, back), out[back:][-600:]))
        alt = out.find(b"\033[?1049h", back)
        check("re-arm: the program is back on the alternate screen, the "
              "kitty push after its 1049h (on that screen)",
              alt > back and out.find(b"\033[>1u", alt) > alt,
              out[back:][-600:])
        if lite:
            # no mirror to replay: the program repaints on its SIGWINCH
            check("re-arm, dch-lite: no screen replay, the program got its "
                  "SIGWINCH after the 1049h", alt > back and
                  b"ALTSCREEN" not in out[alt:] and
                  a.wait(b"WINCH", start=alt), out[back:][-600:])
        else:
            check("re-arm: the screen is replayed from the mirror after the "
                  "1049h", alt > back and b"ALTSCREEN" in out[alt:],
                  out[back:][-600:])
        # redraws with no reset before them (SIGUSR2: 0x07) re-arm again,
        # one pop before each push: the stack stays as it was
        for i in range(1, 4):
            mark = len(a.out)
            os.kill(a.pid, signal.SIGUSR2)
            until(lambda: (a.pump(0.1), not rearmed(a.out, mark))[1], 5)
            a.pump(0.5)
            got = a.out[mark:]
            check("re-arm: SIGUSR2 redraw %d over the attach re-arms again"
                  % i, not rearmed(a.out, mark),
                  (rearmed(a.out, mark), got[-400:]))
            check("re-arm: SIGUSR2 redraw %d: one kitty pop, before its one "
                  "push" % i, got.count(b"\033[<u") == 1 and
                  len(re.findall(rb"\033\[<\d*u", got)) == 1 and
                  got.count(b"\033[>1u") == 1 and
                  got.find(b"\033[<u") < got.find(b"\033[>1u"), got[-400:])
        check("re-arm: over the whole attach pushes minus pops is 1 (the "
              "stacks end at main 0, alternate 1)",
              kitty_stacks(a.out) == (0, 1), kitty_stacks(a.out))
        mark = len(a.out)
        a.key(b"z", 0.3)
        check("re-arm: the attach goes on (a key after reaches the session)",
              a.wait(b"GOT:7a", start=mark, secs=5) and a.reap() is None,
              (a.rc, a.out[mark:][-300:]))
    finally:
        a.close()
    time.sleep(0.5)


# ---- 12: a ctrl-z suspend over the attach drops the host's output (#211) ---

BZ = ("import os, select, sys, time, tty\n"
      "tty.setraw(0)\n"
      "os.write(1, b'\\033[?1049hBUSY\\r\\n')\n"
      "while True:\n"
      "    if select.select([0], [], [], 0.0005)[0]:\n"
      "        b = os.read(0, 256)\n"
      "        if not b: break\n"
      "        os.write(1, b'GOT:' + b.hex().encode() + b'\\r\\n')\n"
      "    os.write(1, b'\\033[5;1HT%.4f ' % time.time())\n")
# the client as a shell runs a job: its own foreground process group, so
# its ctrl-z SIGTSTP really stops it; STOPPED is written then, after its
# own clock's time (the resume is a full second after that time, however
# late the test reads the marker), and it is continued a second later;
# exits with the client's exit code
JOB = ("import os, signal, sys, time\n"
       "job = os.fork()\n"
       "if job == 0:\n"
       "    os.setpgid(0, 0)\n"
       "    signal.signal(signal.SIGTTOU, signal.SIG_IGN)\n"
       "    os.tcsetpgrp(0, os.getpgrp())\n"
       "    signal.signal(signal.SIGTTOU, signal.SIG_DFL)\n"
       "    os.execv(sys.argv[1], sys.argv[1:])\n"
       "while True:\n"
       "    _, st = os.waitpid(job, os.WUNTRACED)\n"
       "    if not os.WIFSTOPPED(st):\n"
       "        sys.exit(os.waitstatus_to_exitcode(st))\n"
       "    os.write(1, b'<<at %.4f>><<stopped>>' % time.time())\n"
       "    time.sleep(1)\n"
       "    os.kill(job, signal.SIGCONT)\n")
STOPPED = b"<<stopped>>"


def test_suspend():
    r = dch(SENV, "--spawn", "xv-bz", "--size", "100x30", sys.executable,
            "-c", BZ)
    check("suspend: the busy session spawned on the host", r.returncode == 0,
          r.stderr)
    a = Pty([sys.executable, "-c", JOB, DCH, "-a", "srv:xv-bz"])
    # the attach is live: the session's ticks reach us after `at` (#249)
    live = lambda at: until(lambda: a.pump(0.1) or TICK.search(a.out, at), 10)
    try:
        a.wait(b"BUSY" if dch(SENV, "--build").stdout.strip() == "full"
               else b"\033[?1049h")
        live(len(a.out))
        for i in range(1, 4):
            mark = len(a.out)
            a.key(b"\x1a", 0)
            ok = a.wait(STOPPED, start=mark)
            stop = a.out.find(STOPPED, mark) + len(STOPPED)
            at = re.findall(rb"<<at (\d+\.\d+)>>", a.out[mark:stop])
            t0 = float(at[-1]) if at else time.time()
            a.wait(b"\033[?1049h", start=stop)
            alt = a.out.find(b"\033[?1049h", stop)
            late = [t for t in re.findall(rb"T(\d+\.\d+) ",
                                          a.out[stop:alt]) if
                    t0 + 0.05 < float(t) < t0 + 0.85]
            check("suspend: ctrl-z %d over the attach: no output the host "
                  "sent while it was stopped before the resume's 1049h" % i,
                  ok and alt > stop and not late,
                  (ok, alt - stop,
                   ["+%.2f" % (float(t) - t0) for t in late][:5],
                   a.out[stop:stop + 200]))
            live(alt)    # the next ctrl-z over a live attach, not a resume
        mark = len(a.out)
        a.key(b"z", 0.3)
        check("suspend: the attach goes on (a key after reaches the session)",
              a.wait(b"GOT:7a", start=mark, secs=5) and a.reap() is None,
              (a.rc, a.out[mark:][-300:]))
        mark = len(a.out)
        a.key(b"\x1a", 0)
        ok = a.wait(STOPPED, start=mark)
        dch(SENV, "-k", "xv-bz")
        rc = a.exit()
        check("suspend: the session ending while the attach is stopped ends "
              "it at the resume, exit 0, no lost-link line",
              ok and rc == 0 and b"lost the connection" not in a.out[mark:],
              (ok, rc, a.out[mark:][-300:]))
    finally:
        a.close()
        dch(SENV, "-k", "xv-bz")
    time.sleep(0.5)


# #231: the host's re-arm after a resume (or the X-ray overlay's return) is
# an RTT away; a proxy passes the 0x07 SLOW s late so that window is wide.
# What the busy session sent in it is for the main screen the reset left.
SLOW = 0.25    # well inside the relay's 1 s hold (RM_REARM_MS)
TICK = re.compile(rb"T\d+\.\d+ ")


def test_suspend_rtt(port):
    from remote_client_test import Proxy
    r = dch(SENV, "--spawn", "xv-bw", "--size", "100x30", sys.executable,
            "-c", BZ)
    check("suspend rtt: the busy session spawned on the host",
          r.returncode == 0, r.stderr)
    full = dch(SENV, "--build").stdout.strip() == "full"
    px = Proxy(port, "slow07", delay=SLOW)
    hosts("srv=127.0.0.1:%d" % px.port)
    a = Pty([sys.executable, "-c", JOB, DCH, "-a", "srv:xv-bw"])
    try:
        a.wait(b"BUSY" if full else b"\033[?1049h")
        a.pump(0.5)
        for i in range(1, 4):
            mark = len(a.out)
            a.key(b"\x1a", 0)
            ok = a.wait(STOPPED, start=mark)
            stop = a.out.find(STOPPED, mark) + len(STOPPED)
            a.wait(b"\033[?1049h", start=stop)
            alt = a.out.find(b"\033[?1049h", stop)
            got = TICK.findall(a.out[stop:alt])
            check("suspend rtt: ctrl-z %d, the host's re-arm %.2f s late: "
                  "no session output on the main screen before the "
                  "resume's 1049h" % (i, SLOW),
                  ok and alt > stop and not got,
                  (ok, len(got), a.out[stop:stop + 200]))
            a.pump(0.3)
        mark = len(a.out)
        a.key(b"\x1cx", 0.05)
        ok = a.wait(b"X-ray  srv:xv-bw", start=mark)
        mark = len(a.out)
        a.key(b"q", 0.05)
        a.wait(RESTORE, start=mark)
        back = a.out.find(RESTORE, mark)
        a.wait(b"\033[?1049h", start=back)
        alt = a.out.find(b"\033[?1049h", back)
        got = TICK.findall(a.out[back:alt])
        check("suspend rtt: the X-ray overlay's return, the re-arm %.2f s "
              "late: no session output on the main screen before its 1049h"
              % SLOW, ok and back >= mark and alt > back and not got,
              (ok, len(got), a.out[back:back + 200]))
        mark = len(a.out)
        a.key(b"z", 0.3)
        check("suspend rtt: the attach goes on (a key after reaches the "
              "session)", a.wait(b"GOT:7a", start=mark, secs=5) and
              a.reap() is None, (a.rc, a.out[mark:][-300:]))
    finally:
        a.close()
        px.close()
    # a host that never re-arms (its 0x07 lost, as a host before 1.58.0
    # never re-arms): the client must not hold the session's output forever
    px = Proxy(port, "no07")
    hosts("srv=127.0.0.1:%d" % px.port)
    a = Pty([sys.executable, "-c", JOB, DCH, "-a", "srv:xv-bw"])
    try:
        a.wait(b"BUSY" if full else b"\033[?1049h")
        a.pump(0.5)
        mark = len(a.out)
        a.key(b"\x1a", 0)
        ok = a.wait(STOPPED, start=mark)
        stop = a.out.find(STOPPED, mark) + len(STOPPED)
        until(lambda: (a.pump(0.1), TICK.search(a.out, stop))[1], 6)
        check("suspend rtt: a host that never re-arms: the session's output "
              "still comes back after the resume",
              ok and TICK.search(a.out, stop) is not None and
              a.reap() is None, (ok, a.rc, a.out[stop:stop + 200]))
    finally:
        a.close()
        px.close()
        dch(SENV, "-k", "xv-bw")
        hosts("srv=127.0.0.1:%d" % port)
    time.sleep(0.5)


def main(port):
    test_chord(port)
    test_chord_detach()
    test_chord_fail()
    hosts("srv=127.0.0.1:%d" % port)
    test_rearm()
    test_suspend()
    test_suspend_rtt(port)


if __name__ == "__main__":
    sys.exit(run(main, sh=True))
