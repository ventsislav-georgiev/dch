#!/usr/bin/env python3
"""Stress loop for a kill -TSTP of the whole job with the X-ray view open
(#410, #416; QA 1.82.0 LOW-VB1/VB2, after QA's probe410.py).

An attach under attach_cont_test's job-control shell, the view opened over
it (^\\ x), then N times killpg(job, SIGTSTP) and 0-4 ms later
killpg(job, SIGCONT). After each, a job member still stopped ('T') 0.4 s
after the CONT is counted and continued again. Then q must close the view
and re-arm the attach. On a hang, ps and a 1 s sample of each member go to
stdout, then a STOP and a CONT tell the kernel wedge (see below) apart. Only the job's own pgid, made here, is ever signalled.

Not in tests/run.sh: 300 iterations take about a minute. Run isolated:
  DCH=./dch python3 tests/attach_cont_stress.py [N]    (default 300)
Exit 0 only with no member left stopped and the q re-arm back (directly,
or after the STOP+CONT that frees a kernel wedge: RESULT wedge=True)."""
import os, shutil, signal, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attach_cont_test as A  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
GAPS = (0, 0, 0.0002, 0.0005, 0.001, 0.002, 0.004)


def members(pg):
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,wchan="],
                         capture_output=True, text=True, timeout=10).stdout
    m = [l.split() for l in out.splitlines()]
    m = [(int(f[0]), int(f[1]), f[3], f[4] if len(f) > 4 else "")
         for f in m if len(f) >= 4 and f[2] == str(pg)]
    pids = {p for p, _, _, _ in m}

    def role(pid, ppid):
        if pid == pg:
            return "client"
        return "view" if ppid == pg else ("query" if ppid in pids else "other")
    return [(role(p, pp), p, st, wc) for p, pp, st, wc in m]


def stopped(pg):
    return [x for x in members(pg) if x[2].startswith("T")]


def main():
    tmp = tempfile.mkdtemp(prefix="dch-stress-")
    env = dict(os.environ, DCH_SOCKET_DIR=tmp)
    stuck, back, wedge = [], False, False
    r = subprocess.run([A.DCH, "--spawn", A.SESS, "--size", "80x24",
                        sys.executable, "-c", A.CHILD], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    if r.returncode:
        print("FAIL spawn", r.stderr)
        return 1
    j = A.Job(["-a", A.SESS], env=env)
    try:
        if not A.attached(j, "stress"):
            return 1
        os.write(j.fd, b"\x1cx")
        if not j.pump(20, lambda: b"X-ray  " in j.out):
            print("FAIL the view did not open")
            return 1
        j.pump(0.5)
        print("members:", members(j.job))
        t0 = time.time()
        for i in range(N):
            os.killpg(j.job, signal.SIGTSTP)
            g = GAPS[i % len(GAPS)]
            if g:
                time.sleep(g)
            os.killpg(j.job, signal.SIGCONT)
            j.pump(0.08)
            st = stopped(j.job)
            if st:
                j.pump(0.3)
                st = stopped(j.job)
            if st:
                stuck.append((i, g, st))
                os.killpg(j.job, signal.SIGCONT)
                j.pump(0.2)
        print("loop %d iterations in %.1fs, load %s" % (
            N, time.time() - t0, os.getloadavg()[0]))
        for s in stuck:
            print("  stuck iter %d gap %s: %s" % s)
        j.pump(0.5)
        mark = len(j.out)
        os.write(j.fd, b"q")
        back = j.pump(15, lambda: A.PUSH in j.out[mark:])
        print("q closes the view and re-arms:", back, "members:",
              members(j.job))
        if not back:
            for role, pid, st, wc in members(j.job):
                s = subprocess.run(["/usr/bin/sample", str(pid), "1"],
                                   capture_output=True, text=True, timeout=30)
                print("---- sample %s %d %s %s" % (role, pid, st, wc))
                print("\n".join(l for l in s.stdout.splitlines()
                                if "+" in l)[:6000])
            print("tty tail:", j.out[-300:])
            # a macOS wedge, not dch's (#416): a process that stops itself
            # in a signal handler while SIGCONT is caught can be left
            # running-but-suspended (ps S) by a CONT that lands in that stop;
            # more CONTs do nothing, a STOP then a CONT frees it
            mark = len(j.out)
            os.killpg(j.job, signal.SIGSTOP)
            time.sleep(0.05)
            os.killpg(j.job, signal.SIGCONT)
            os.write(j.fd, b"q")
            wedge = j.pump(10, lambda: A.PUSH in j.out[mark:])
            print("kernel wedge: freed by STOP+CONT, q re-armed:", wedge,
                  members(j.job))
    finally:
        j.close()
        subprocess.run([A.DCH, "-k", A.SESS], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT stuck=%d/%d q_back=%s wedge=%s" % (len(stuck), N, back,
                                                     not back and wedge))
    return 0 if (back or wedge) and not stuck else 1


if __name__ == "__main__":
    sys.exit(main())
