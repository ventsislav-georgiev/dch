#!/usr/bin/env python3
"""Double-tapping the detach key switches sessions; one tap still detaches.

Covers, in order:
  1. two taps land you in a *different* session picked from the list;
  2. the session you left keeps no client marker behind (live_clients_on()
     counts those, so a leak makes a free session look busy forever);
  3. one tap detaches even while the session floods output — the window is a
     deadline, not an idle timeout, or a chatty program would hold it open
     forever and the press would never land;
  4. typing something else inside the window commits the detach (x there
     is the X-ray chord, #187);
  5. DCH_DOUBLE_TAP_MS=0 turns the shortcut off and detach is instant;
  6. detach_key = switch (#082) swaps them: one press opens the picker, two
     detach; with DCH_DOUBLE_TAP_MS=0 one press opens the picker at once;
  7. a switch from a session this same client created (#109): the spawn
     left DCH_SESSION set in the client, so the next attach hit the nesting
     refusal and dch exited to the shell. The new session still gets it;
  8. the picker inside a session (#591): Enter on another switches the
     terminal attached there, the session keeps running with its shell
     back at the prompt; the session's own output cannot trigger that, a
     passive client is never switched, and with no active client the
     nesting refusal stays; -a, -n, -- inside it are refused, never a
     switch (#594); a second active client (-f) that did not type the
     Enter stays on the session (#595), and once made passive at runtime
     (--passive --pid) its own pick switches nothing (#596);
  9. the pick's Enter from a source that cannot switch (dch --keys, a
     passive client, a -E client) after the active terminal typed the rest
     moves nothing and the inner dch is refused (#598); a --keys pick
     with an active client that never typed is refused; of two active
     clients only the one that types the pick switches, the newer or the
     older (#600).

Run:  python3 tests/switch_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/switch_test.py
"""
import os, sys, pty, signal, select, time, errno, re, subprocess, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
TAG = "sw%d" % os.getpid()
A, B, C, D, E = (TAG + s for s in "abcde")
NOSUCH, NEWN = TAG + "x", TAG + "n"	# #594: never created
DETACH = b"\x1c"
SOCKDIR = os.path.join(tempfile.mkdtemp(prefix="dchswitch"), "sock")
SWAPCFG = os.path.join(os.path.dirname(SOCKDIR), "config")
with open(SWAPCFG, "w") as f:
    f.write("detach_key = switch\n")


def dch(*args, **kw):
    env = dict(os.environ, DCH_SOCKET_DIR=SOCKDIR)
    env.pop("DCH_SESSION", None)
    env.update(kw.pop("env", {}))
    return subprocess.run([DCH] + list(args), env=env, capture_output=True,
                          text=True, timeout=30)


def drain(fd, seconds, until=None):
    out = b""
    end = time.time() + seconds
    while time.time() < end:
        if until and until in out:
            break
        r, _, _ = select.select([fd], [], [], 0.1)
        if fd in r:
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EAGAIN):
                    break
                raise
            if not chunk:
                break
            out += chunk
    return out.decode("utf-8", "replace")


def spawn_client(extra, env=None):
    """Attach a real client on its own pty. No -E: we need the detach key."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)
        os.environ["DCH_SOCKET_DIR"] = SOCKDIR
        os.environ.update(env or {})
        os.execv(DCH, [DCH] + extra)
        os._exit(127)
    # pty.fork()'s master is inheritable: the next kid would hold it,
    # and closing ours would not hang this pty up (QA 1.59.0 MED-T2)
    os.set_inheritable(fd, False)
    return pid, fd


def reap(pid, fd, seconds):
    """Wait for pid; return its status, or None if it outlived `seconds`.
    Keeps draining fd so the client never blocks on a full pty buffer."""
    end = time.time() + seconds
    while time.time() < end:
        got, status = os.waitpid(pid, os.WNOHANG)
        if got:
            return status
        if fd is not None:
            drain(fd, 0.05)
        else:
            time.sleep(0.05)
    return None


def kill_client(pid, fd=None):
    if pid is None:
        return
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        if reap(pid, fd, 3.0) is not None:
            return


def picker_rows(text):
    """Session names as drawn by the picker, top row first."""
    rows = []
    # The picker positions each line with CUP rather than newlines.
    for line in re.split(r"\n|\x1b\[\d+;1H", text):
        m = re.search(r"(%s[abcde])" % TAG, line)
        if m and m.group(1) not in rows:
            rows.append(m.group(1))
    return rows


def client_markers(session):
    try:
        return [f for f in os.listdir(SOCKDIR)
                if f.startswith(session + ".sock.client.")]
    except OSError:
        return []


def fail(msg, *extra):
    print("FAIL:", msg)
    for e in extra:
        print("  ", repr(e))
    return 1


def main():
    if not os.access(DCH, os.X_OK):
        return fail("dch not executable at " + DCH)

    # Two headless sessions with distinct, identifiable screens. A also keeps
    # printing, so case 3 exercises the deadline against a chatty session.
    dch("--spawn", A, "--size", "80x24", "sh", "-c",
        "while :; do echo AAA_ALPHA; sleep 0.05; done")
    dch("--spawn", B, "--size", "80x24", "sh", "-c",
        "echo BBB_BRAVO; sleep 600")
    if dch("--wait", A, "--match", "AAA_ALPHA", "--timeout", "10000").returncode:
        return fail("session A never produced output")
    if dch("--wait", B, "--match", "BBB_BRAVO", "--timeout", "10000").returncode:
        return fail("session B never produced output")

    pid, fd = None, None
    try:
        # 1. Double tap -> picker -> land in the other session.
        pid, fd = spawn_client(["-f", "-n", A])
        if "AAA_ALPHA" not in drain(fd, 10.0, b"AAA_ALPHA"):
            return fail("client never showed session A")
        os.write(fd, DETACH)
        time.sleep(0.05)
        os.write(fd, DETACH)
        # Drain the whole picker frame, not just up to the tabs row (#252: no header) — the rows
        # arrive in the same write but `until` would stop us at the first one.
        out = drain(fd, 5.0, b"Sessions") + drain(fd, 0.5)
        if "Sessions" not in out:
            return fail("double tap did not open the picker", out)
        rows = picker_rows(out)
        if B not in rows:
            return fail("picker did not list the other session", rows, out)
        # the picker opens on the session just left (#098)
        d = rows.index(B) - (rows.index(A) if A in rows else 0)
        os.write(fd, (b"j" * d if d > 0 else b"k" * -d) + b"\r")
        landed = drain(fd, 10.0, b"BBB_BRAVO")
        if "BBB_BRAVO" not in landed:
            return fail("switch did not attach to the picked session", landed)

        # 2. No client marker left behind on the session we walked away from.
        stale = client_markers(A)
        if stale:
            return fail("stale client marker on the old session", stale)
        if not client_markers(B):
            return fail("no client marker on the session we switched into")
        kill_client(pid, fd)
        pid = None

        # 3. One tap detaches even while the session floods output.
        pid, fd = spawn_client(["-f", "-n", A])
        drain(fd, 10.0, b"AAA_ALPHA")
        os.write(fd, DETACH)
        if reap(pid, fd, 5.0) is None:
            return fail("single tap did not detach a chatty session")
        pid = None

        # 4. Any other key inside the window commits the detach (not x:
        # that is the X-ray chord, #187, xray_view_shell_test.py).
        pid, fd = spawn_client(["-f", "-n", B])
        drain(fd, 10.0, b"BBB_BRAVO")
        os.write(fd, DETACH)
        time.sleep(0.02)
        os.write(fd, b"y")
        if reap(pid, fd, 5.0) is None:
            return fail("a key inside the window did not commit the detach")
        pid = None

        # 5. Opting out restores instant detach.
        # the fastest of 3: one stalled run on a slow runner can't fake a wait
        for _ in range(3):
            pid, fd = spawn_client(["-f", "-n", B],
                                   env={"DCH_DOUBLE_TAP_MS": "0"})
            drain(fd, 10.0, b"BBB_BRAVO")
            t0 = time.time()
            os.write(fd, DETACH)
            if reap(pid, fd, 5.0) is None:
                return fail("DCH_DOUBLE_TAP_MS=0 did not detach")
            pid = None
            if time.time() - t0 <= 0.25:
                break
        else:
            return fail("DCH_DOUBLE_TAP_MS=0 still waited out a window")

        # 6. detach_key = switch: one press opens the picker; q there
        # leaves the session running and the client exits.
        swap = {"DCH_CONFIG": SWAPCFG}
        pid, fd = spawn_client(["-f", "-n", B], env=swap)
        drain(fd, 10.0, b"BBB_BRAVO")
        os.write(fd, DETACH)
        out = drain(fd, 5.0, b"Sessions")
        if "Sessions" not in out:
            return fail("detach_key=switch: one press did not open the "
                        "picker", out)
        os.write(fd, b"q")
        if reap(pid, fd, 5.0) is None:
            return fail("detach_key=switch: q in the picker did not leave")
        pid = None
        # two presses detach, no picker
        pid, fd = spawn_client(["-f", "-n", B], env=swap)
        drain(fd, 10.0, b"BBB_BRAVO")
        os.write(fd, DETACH)
        time.sleep(0.05)
        os.write(fd, DETACH)
        if reap(pid, fd, 5.0) is None:
            return fail("detach_key=switch: two presses did not detach")
        pid = None
        # no double press: one opens the picker at once
        pid, fd = spawn_client(["-f", "-n", B],
                               env=dict(swap, DCH_DOUBLE_TAP_MS="0"))
        drain(fd, 10.0, b"BBB_BRAVO")
        os.write(fd, DETACH)
        out = drain(fd, 5.0, b"Sessions")
        if "Sessions" not in out:
            return fail("detach_key=switch, DCH_DOUBLE_TAP_MS=0: one press "
                        "did not open the picker", out)
        os.write(fd, b"q")
        if reap(pid, fd, 5.0) is None:
            return fail("detach_key=switch: q in the picker did not leave")
        pid = None
        if dch("--read", B).returncode not in (0, 3):
            return fail("the session did not survive the picker")

        # 7. Switch away from a session this client created (#109).
        pid, fd = spawn_client(["-n", C, "sh", "-c",
                                'echo "CCC_$DCH_SESSION"; sleep 600'])
        if "CCC_" + C not in drain(fd, 10.0, ("CCC_" + C).encode()):
            return fail("the created session did not get DCH_SESSION")
        os.write(fd, DETACH)
        time.sleep(0.05)
        os.write(fd, DETACH)
        out = drain(fd, 5.0, b"Sessions") + drain(fd, 0.5)
        rows = picker_rows(out)
        if B not in rows:
            return fail("#109: picker did not list B", rows, out)
        d = rows.index(B) - (rows.index(C) if C in rows else 0)
        os.write(fd, (b"j" * d if d > 0 else b"k" * -d) + b"\r")
        landed = drain(fd, 10.0, b"BBB_BRAVO")
        if "BBB_BRAVO" not in landed or "nesting disabled" in landed:
            return fail("#109: a switch from a session this client created "
                        "did not attach", landed)
        kill_client(pid, fd)
        pid = None

        # 8. The picker inside a session switches its terminal (#591).
        pid, fd = spawn_client(["-n", D, "env", "PS1=DDD> ", "sh", "-i"])
        if "DDD> " not in drain(fd, 10.0, b"DDD> "):
            return fail("#591: session D's shell never prompted")
        # (c) the session's output, the handover's shape with a key it
        # cannot know, switches nothing
        os.write(fd, b"printf '\\033]5911;0123456789abcdef0123456789abcd"
                     b"ef;%s\\a'; echo FORGED_OK\r" % B.encode())
        out = drain(fd, 5.0, b"FORGED_OK\r\n") + drain(fd, 0.5)
        if "FORGED_OK" not in out or "BBB_BRAVO" in out or \
                reap(pid, fd, 0.3) is not None:
            return fail("#591: printed bytes switched the client", out)
        # #594: only a pick switches; -a, -n and -- inside D are refused
        # and leave the terminal on D
        for n, cmd in enumerate(("-a " + NOSUCH,
                                 "-n " + NEWN + " sh -c 'echo X'",
                                 "-- true")):
            os.write(fd, b"%s %s; echo R%d=$?\r" % (DCH.encode(),
                                                     cmd.encode(), n))
            out = drain(fd, 5.0, b"R%d=1" % n) + drain(fd, 0.3)
            if "nesting disabled" not in out or "R%d=1" % n not in out or \
                    reap(pid, fd, 0.3) is not None:
                return fail("#594: dch %s inside D was not refused" % cmd,
                            out)
        if [f for f in os.listdir(SOCKDIR)
                if f.startswith(NOSUCH) or f.startswith(NEWN)]:
            return fail("#594: a refused attach inside D created a session",
                        os.listdir(SOCKDIR))
        # (b) a passive client on D rides along, never switched
        ppid, pfd = spawn_client(["-a", D, "--passive"])
        # #595: a second active client, which never types the Enter
        spid, sfd = spawn_client(["-f", "-a", D])
        try:
            drain(pfd, 3.0, b"DDD> ")
            drain(sfd, 3.0, b"DDD> ")
            # a --restart carries the client's secret (the release flow)
            rs = dch("--restart", D)
            if rs.returncode:
                return fail("#591: --restart of D failed", rs.stderr)
            drain(fd, 1.0)
            # (a) bare dch inside D: Enter on B
            os.write(fd, DCH.encode() + b"; echo INNER_RC=$?\r")
            out = drain(fd, 5.0, b"Sessions") + drain(fd, 0.5)
            rows = picker_rows(out)
            if B not in rows:
                return fail("#591: the picker inside D did not list B",
                            rows, out)
            # the "/" filter (#096) selects B: rows move as they update
            os.write(fd, b"/" + B.encode())
            drain(fd, 0.5)
            os.write(fd, b"\r")
            landed = drain(fd, 10.0, b"BBB_BRAVO")
            if "BBB_BRAVO" not in landed or "nesting disabled" in landed:
                return fail("#591: Enter on B inside D did not switch the "
                            "terminal to B", landed)
            time.sleep(0.5)
            scr = dch("--read", D)
            if scr.returncode or "INNER_RC=0" not in scr.stdout or \
                    not scr.stdout.rstrip().endswith("DDD>"):
                return fail("#591: D is not live at its prompt with the "
                            "inner dch exited 0", scr.stdout, scr.stderr)
            # the passive client is still on D
            pout = drain(pfd, 1.0)
            if reap(ppid, pfd, 0.2) is not None or "BBB_BRAVO" in pout:
                return fail("#591: the passive client was switched", pout)
            # #595: the other active client is still on D, getting its output
            os.write(sfd, b"echo SECOND_$DCH_SESSION\r")
            sout = drain(sfd, 5.0, ("SECOND_" + D).encode())
            if reap(spid, sfd, 0.2) is not None or "BBB_BRAVO" in sout or \
                    "SECOND_" + D not in sout:
                return fail("#595: the active client that did not pick was "
                            "switched too", sout)
            # (b') made passive at runtime (#596): it keeps its key, the
            # master alone must skip it
            rp = dch("--passive", D, "--pid", str(spid))
            if rp.returncode:
                return fail("#596: --passive --pid failed", rp.stderr)
            # its own switch loop goes on: double tap opens the picker
            os.write(fd, DETACH)
            time.sleep(0.05)
            os.write(fd, DETACH)
            out = drain(fd, 5.0, b"Sessions")
            if "Sessions" not in out:
                return fail("#591: double tap after the switch did not "
                            "open the picker", out)
            os.write(fd, b"q")
            if reap(pid, fd, 5.0) is None:
                return fail("#591: q in the picker did not leave")
            pid = None
            # (d) only passive clients left, one of them keyed and made
            # passive at runtime: its own pick of B is refused (#596)
            os.write(sfd, DCH.encode() + b"; echo INNER2_RC=$?\r")
            drain(sfd, 5.0, b"Sessions")
            drain(sfd, 0.5)
            os.write(sfd, b"/" + B.encode())
            drain(sfd, 0.5)
            os.write(sfd, b"\r")
            out = drain(sfd, 5.0, b"INNER2_RC=1") + drain(sfd, 0.5)
            if "nesting disabled" not in out or "INNER2_RC=1" not in out or \
                    "BBB_BRAVO" in out or reap(spid, sfd, 0.2) is not None:
                return fail("#596: a client made passive at runtime was "
                            "switched, or no nesting refusal", out)
        finally:
            kill_client(ppid, pfd)
            kill_client(spid, sfd)

        # 9. Only the newest input of any source makes the pick (#598).
        pid, fd = spawn_client(["-n", E, "env", "PS1=EEE> ", "sh", "-i"])
        if "EEE> " not in drain(fd, 10.0, b"EEE> "):
            return fail("#598: session E's shell never prompted")
        # #600: a --keys pick while the only active client never typed
        dch("--run", E, "%s; echo R0=$?" % DCH)
        if dch("--wait", E, "--match", "Sessions",
               "--timeout", "5000").returncode:
            return fail("#600: the picker inside E never opened")
        dch("--send", E, "/" + B)
        time.sleep(0.5)
        dch("--keys", E, "enter")
        out = drain(fd, 5.0, b"R0=") + drain(fd, 0.5)
        if "nesting disabled" not in out or "R0=1" not in out or \
                "BBB_BRAVO" in out or reap(pid, fd, 0.2) is not None:
            return fail("#600: a --keys pick switched an active client that "
                        "never typed", out)
        ppid, pfd = spawn_client(["-a", E, "--passive"])
        epid, efd = spawn_client(["-E", "-a", E])
        spid, sfd = None, None
        try:
            drain(pfd, 3.0, b"EEE> ")
            drain(efd, 3.0, b"EEE> ")

            def keys_enter():
                dch("--keys", E, "enter")

            # the active terminal types the rest, another source the Enter
            for n, (who, enter) in enumerate((
                    ("dch --keys", keys_enter),
                    ("a passive client", lambda: os.write(pfd, b"\r")),
                    ("a -E client", lambda: os.write(efd, b"\r"))), 1):
                os.write(fd, DCH.encode() + b"; echo R%d=$?\r" % n)
                drain(fd, 5.0, b"Sessions")
                drain(fd, 0.5)
                os.write(fd, b"/" + B.encode())
                drain(fd, 0.5)
                drain(pfd, 0.2)	# a client blocked on its output reads
                drain(efd, 0.2)	# no keys
                enter()
                out = drain(fd, 5.0, b"R%d=" % n) + drain(fd, 0.5)
                if "nesting disabled" not in out or "R%d=1" % n not in out \
                        or "BBB_BRAVO" in out or \
                        reap(pid, fd, 0.2) is not None:
                    return fail("#598: the pick's Enter from %s moved the "
                                "active terminal, or no nesting refusal"
                                % who, out)
            # #600: of two active clients the one that types the whole
            # pick switches, the other stays on E: the newer one (-f)
            # picks, then the older one next to a newer, so neither list
            # order lets "the first keyed client" pass
            for picker, newer in (("newer", True), ("older", False)):
                kill_client(spid, sfd)
                spid, sfd = spawn_client(["-f", "-a", E])
                drain(sfd, 3.0, b"EEE> ")
                tfd, op, ofd = (sfd, pid, fd) if newer else (fd, spid, sfd)
                os.write(tfd, DCH.encode() + b"; echo R9=$?\r")
                drain(tfd, 5.0, b"Sessions")
                drain(tfd, 0.5)
                os.write(tfd, b"/" + B.encode())
                drain(tfd, 0.5)
                drain(ofd, 0.2)
                os.write(tfd, b"\r")
                landed = drain(tfd, 10.0, b"BBB_BRAVO")
                out = drain(ofd, 0.5)
                if "BBB_BRAVO" not in landed or "nesting disabled" in landed:
                    return fail("#600: the %s active client's pick did not "
                                "switch it" % picker, landed)
                if "BBB_BRAVO" in out or reap(op, ofd, 0.2) is not None:
                    return fail("#600: the other active client was switched "
                                "by the %s one's pick" % picker, out)
            # #601: a passive client's focus report between the pick and its
            # Enter is no typing, so the active terminal's Enter still picks
            kill_client(spid, sfd)
            spid, sfd = spawn_client(["-f", "-a", E])
            drain(sfd, 3.0, b"EEE> ")
            os.write(sfd, DCH.encode() + b"; echo R10=$?\r")
            drain(sfd, 5.0, b"Sessions")
            drain(sfd, 0.5)
            os.write(sfd, b"/" + B.encode())
            drain(sfd, 0.5)
            kill_client(ppid, pfd)
            ppid, pfd = spawn_client(["-E", "-a", E, "--passive"],
                                    {"DCH_FOCUS_RESIZE": "0"})
            drain(pfd, 3.0, b"EEE> ")
            # The pick is read a moment after its Enter: the burst types the
            # Enter itself, then reports right behind it, 0.2 ms apart, so
            # one lands in that window (any one there made it refused).
            # Paced, not a flood (#636): a flood fills the tty queue, and a
            # lagging client's reads then cut reports at any byte; ESC | [I
            # is typing (focus_resize off: no client hold rejoins a lone ESC).
            # Every other report comes as ESC [ | I, as a slow link splits
            # it: the master holds that ESC [, and it is no typing either.
            # Blocking writes: a non-blocking pty write can land 2 of the 3
            # bytes, a broken report that is real input (#618).
            stop = threading.Event()

            def focus_burst():
                try:
                    os.write(sfd, b"\r")
                    # each write closes the last one's ESC [, has a whole
                    # report, and ends in the next ESC [: every push holds
                    # one, as reads cut after ESC [ by a lagging client do
                    # (#636); 500 bytes, never a full queue
                    os.write(pfd, b"\033[")
                    for _ in range(100):
                        os.write(pfd, b"I\033[I\033[")
                    os.write(pfd, b"I")
                    for _ in range(150):
                        os.write(pfd, b"\033[I")
                        time.sleep(0.0002)
                        os.write(pfd, b"\033[")
                        time.sleep(0.0002)
                        os.write(pfd, b"I")
                        time.sleep(0.0002)
                except OSError:
                    pass	# the client is gone: nothing to report to

            # the passive client's output is drained, so it never blocks on
            # it and stops reading the burst (nightly ASan 10-min hangs)
            def drain_passive():
                while not stop.is_set():
                    drain(pfd, 0.1)

            burst = threading.Thread(target=focus_burst, daemon=True)
            pdrain = threading.Thread(target=drain_passive, daemon=True)
            pdrain.start()
            burst.start()
            landed = drain(sfd, 10.0, b"BBB_BRAVO")
            stop.set()
            burst.join(10.0)
            pdrain.join(10.0)
            if burst.is_alive():
                return fail("#601: the focus burst stayed blocked on the "
                            "passive client's input", landed)
            if "BBB_BRAVO" not in landed or "nesting disabled" in landed:
                return fail("#601: a passive client's focus report refused "
                            "the active terminal's pick", landed)
        finally:
            kill_client(ppid, pfd)
            kill_client(epid, efd)
            kill_client(spid, sfd)

        print("PASS: double tap switches sessions, single tap detaches; "
              "detach_key = switch swaps them; the picker inside a "
              "session switches its terminal.")
        return 0
    finally:
        kill_client(pid, fd)
        for s in (A, B, C, D, E, NOSUCH, NEWN):
            dch("-k", s)


if __name__ == "__main__":
    sys.exit(main())
