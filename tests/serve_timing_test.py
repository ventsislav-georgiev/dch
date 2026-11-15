"""#124 and #201 of tests/serve_test.py: serve's timers. A --say or a
--status cut by the close or a 0x34 gets its grace; a 6 MiB answer comes whole
to a stalled reader; 16 peers that never read free their slots by the stall
rule (#246).

Run:  python3 tests/serve_timing_test.py        (uses ./dch)
"""
import serve_lib as lib
from serve_lib import *


def main():
    L = lib.setup()
    (tmp, fake, home, env, benv, procs, fails, whois_fail_ok, slow_whois,
     check, put, ts, log_n, serve, stop, calls, run_fail, text, conn, send,
     recv_exact, frame, frame_not, reply, refused, refusal, sessions, ctl1) = (
        L[k] for k in lib.NAMES)

    try:
        ts()
        # --- #124: a CTL_LONG verb (--say here) cut by the close or a 0x34
        # gets CTL_LONG_MS after its SIGTERM, not 2 s; any other verb
        # (--status) is SIGKILLed 2 s after. A `dch` first on serve's PATH
        # stands in for those two (the rest it execs): this measures serve's
        # grace, not dch. The real --run and --say under the cut: #126 below.
        wrap = os.path.join(tmp, "wrap")
        os.makedirs(wrap)
        with open(os.path.join(wrap, "dch"), "w") as f:
            f.write("""#!/bin/sh
W=%s
case "$1 $2" in
"--say slow"|"--status slow")
    v=${1#--}; rm -f "$W/$v.log"
    if [ "$v" = say ]; then
        trap 'echo term >> "$W/$v.log"; sleep 4; echo done >> "$W/$v.log"
              exit 0' TERM
    else
        trap 'echo term >> "$W/$v.log"' TERM
    fi
    echo $$ > "$W/$v.pid"
    echo started
    while :; do sleep 0.1; done ;;
"--status big")
    exec /bin/cat "$W/big" ;;
esac
exec %s "$@"
""" % (wrap, DCH))
        # the 6 MiB is a file written now: the stand-in is sh and cat, no
        # Python start-up, so under load it still writes within serve's 3 s
        # ctl timeout and a never-reader has bytes pending at the cut (#246)
        with open(os.path.join(wrap, "big"), "wb") as f:
            f.write((b"x" * 65535 + b"\n") * 96 + b"END\n")
        os.chmod(os.path.join(wrap, "dch"), 0o755)

        def slow_cut(port, verb, cancel):
            """Cut a slow verb (cancel: a 0x34, else the close): seconds
            from its SIGTERM to its end, and whether it finished its own
            SIGTERM work."""
            argv = ["--say", "slow", "--json", "--", "hi"] if verb == "say" \
                else ["--status", "slow"]
            vlog = os.path.join(wrap, verb + ".log")
            s = conn(port)
            send(s, 0x30, {"argv": argv})
            frame(s)		# "started": the trap is set
            pid = int(text(os.path.join(wrap, verb + ".pid")))
            if cancel:
                send(s, 0x34)
            else:
                s.close()
            t0, gone = time.time(), None
            while time.time() - t0 < 15 and gone is None:
                try:
                    os.kill(pid, 0)	# a probe only
                except ProcessLookupError:
                    gone = time.time()
                time.sleep(0.05)
            if cancel:
                try:
                    t, pl = frame_not(s, (0x31,))
                except (EOFError, OSError):
                    t, pl = None, b""
                ok = t == 0x33 and json.loads(pl) == {"exit": 2,
                                                       "why": "cancel"}
                s.close()
            else:
                ok = True
            got = text(vlog) if os.path.exists(vlog) else ""
            return ok and "term" in got, \
                gone - t0 if gone else None, "done" in got

        p, sport, slog_ = serve(argv0="dch", e=dict(
            benv, PATH="%s:%s:/usr/bin:/bin" % (wrap, fake)))
        check("#124: a serve whose ctls run the stand-in dch starts",
              sport is not None)
        if sport is not None:
            for cancel, how in ((False, "the close"), (True, "a 0x34")):
                ok, secs, done = slow_cut(sport, "say", cancel)
                check("#124: --say cut by %s: SIGTERM, not SIGKILLed at 2 s, "
                      "finishes on its own (%s s)" % (how, secs and
                                                       round(secs, 1)),
                      ok and secs is not None and secs >= 3.5 and done)
                ok, secs, done = slow_cut(sport, "status", cancel)
                check("#124: --status cut by %s: SIGTERM, SIGKILLed about "
                      "2 s later (%s s)" % (how, secs and round(secs, 1)),
                      ok and secs is not None and 1.5 <= secs <= 3.5
                      and not done)
            stop(p)

        # --- #201: a one-shot ctl's answer past the socket and pipe buffers
        # (6 MiB of the stand-in's --status big) comes whole to a reader
        # that stalls (30 s before #367, now until the 16 never-readers are freed, about 23 s), past STALL_MS's 20 s: a ctl with a timeout is
        # bounded by it, its output may sit untaken until 20 s past it. A
        # peer that never reads still frees its 16 slots by then: here a
        # serve of its own with DCH_CTL_TIMEOUT_MS 3 s, so by about 23 s.
        # Both run at once, to keep the wall time near 31 s.
        wenv = dict(benv, PATH="%s:%s:/usr/bin:/bin" % (wrap, fake))
        p, sport, slog_ = serve(argv0="dch", e=wenv)
        q, qport, qlog = serve(argv0="dch", e=dict(
            wenv, DCH_CTL_TIMEOUT_MS="3000"))
        check("#201: two serves whose ctls run the stand-in dch start",
              sport is not None and qport is not None)
        if sport is not None and qport is not None:
            big = ["--status", "big"]
            s = conn(sport)
            send(s, 0x30, {"argv": big})
            t, pl = frame(s)
            out = bytearray(pl if t == 0x31 else b"")
            # An 8 KiB receive buffer, set before the connect so it caps
            # the window: macOS otherwise grows both loopback buffers until
            # they hold all serve had left after the cut, nothing stays
            # pending, and the idle close (30 s) decides, not the stall
            # rule (#246). Linux doubles it to 16 KiB and grows serve's
            # send buffer with cwnd only, which a shut window holds at its
            # start (about 1.4 MiB at most on lo). Either way the 6 MiB
            # answer leaves serve over OUT_HIGH (64 KiB) pending at the cut.
            never = []
            for _ in range(16):
                n = socket.socket()
                n.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
                n.settimeout(5)
                n.connect(("127.0.0.1", qport))
                never.append(n)
                send(n, 0x30, {"argv": big})
            t_end = time.time() + 3	# their ctl end, DCH_CTL_TIMEOUT_MS
            # the 17th is busy once serve holds the 16 (a poll, not a pause)
            full = reply(qport)
            for _ in range(100):
                if isinstance(full, bytes) and b'"busy"' in full:
                    break
                time.sleep(0.05)
                full = reply(qport)
            # s reads nothing until q has freed its 16 slots, which is
            # past STALL_MS's 20 s (the ctl timeout 3 s + 20 s): a real
            # product timer, so this waits for it with a 40 s deadline
            end = time.time() + 40
            while time.time() < end and \
                    text(qlog).count("output not taken for 20 s") < 16:
                time.sleep(0.1)
            end = None
            s.settimeout(30)
            try:
                while True:
                    t, pl = frame(s)
                    if t == 0x31:
                        out += pl
                    elif t == 0x33:
                        end = json.loads(pl)
                        break
            except (EOFError, OSError):
                pass
            s.close()
            check("#201: a 6 MiB answer whose reader stalls past 20 s comes whole "
                  "(%d bytes), then 0x33 exit 0" % len(out),
                  len(out) == 96 * 65536 + 4 and out.endswith(b"x\nEND\n")
                  and end == {"exit": 0}
                  and "not taken" not in text(slog_))
            ql = text(qlog)
            ok = isinstance(full, bytes) and b'"busy"' in full \
                and ql.count("ctl cut short: timeout") == 16 \
                and ql.count("output not taken for 20 s") == 16
            r17 = reply(qport)
            check("#201: 16 peers that never read hold the peer's 16 slots "
                  "(a 17th busy), then all are free 20 s past their timeout: "
                  "16 timeout cuts, 16 closes",
                  ok and r17 == "accept")
            if not (ok and r17 == "accept"):
                # what it saw: each event's time from the ctl end, and
                # what every never-reader still had queued
                ev = {}
                for m in re.finditer(r"^(\S+)\.(\d+)Z dch serve\[\d+\]: "
                                     r"127\.0\.0\.1: (ctl cut short: timeout"
                                     r"|output not taken|idle; closing)",
                                     ql, re.M):
                    t = calendar.timegm(time.strptime(
                        m.group(1), "%Y-%m-%dT%H:%M:%S")) \
                        + int(m.group(2)) / 1000
                    ev.setdefault(m.group(3), []).append(
                        "%+.1f" % (t - t_end))
                print("       17th: %r; %s" % (full, "; ".join(
                    "%d %s at %s s" % (len(v), k, " ".join(v))
                    for k, v in ev.items()) or "no cut or close logged"))
                for i, n in enumerate(never):
                    got, eof = 0, False
                    n.settimeout(0.5)
                    try:
                        while True:
                            b = n.recv(65536)
                            if not b:
                                eof = True
                                break
                            got += len(b)
                    except OSError:
                        pass
                    print("       never-reader %d: %s, %d bytes unread"
                          % (i, "closed" if eof else "OPEN", got))
            for n in never:
                n.close()
            stop(p)
            stop(q)

    finally:
        lib.cleanup(L)
    lib.verdict(L)


if __name__ == "__main__":
    main()
