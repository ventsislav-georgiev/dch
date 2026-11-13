#!/usr/bin/env python3
"""Input the program is not reading never blocks the master (#258).

The master wrote a session's input to the pty with a blocking write. A program
not reading its input stopped the whole master: no output to the attached
clients, no other verb served. A program that prints before it reads then
deadlocked with it for good: it waited for the master to read its output, the
master waited for it to read its input. The input now waits in the master
until the pty takes it.

  busy      the program waits for a go file, prints 300 KB, then reads. 96 KB
            of --send (more than a pty holds on macOS and Linux) and a
            --keys Enter go in before the go.
  exit      a program that never reads ends with 7 while 96 KB wait for it.
            The master is stopped (SIGSTOP, exact pid) across the end and a
            key typed on the attach, so the next pass meets the key, the dead
            pty and the waiting input at once: the write fails, and the end
            must still be the program's (exit 7), not a failed write's.
  held      the same under a hold (--respawn with no argv): the session is
            held with exit 7, and the program --respawn starts gets none of
            the old input.

Run:  python3 tests/send_busy_pty_test.py      (uses ./dch)
"""
import errno, json, os, pty, select, shutil, signal, subprocess, sys
import tempfile, termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
OUT = 300000
SENT = "A" * 48000 + "B" * 48000
# raw before the ready file: tty.setraw's TCSAFLUSH drops input already there
BUSY = ("import os, sys, time, tty\n"
        "tty.setraw(0)\n"
        "d = sys.argv[1]\n"
        "open(d + '/ready', 'w').close()\n"
        "while not os.path.exists(d + '/go'): time.sleep(0.02)\n"
        "os.write(1, b'x' * %d)\n"
        # quiet before it reads: what flushes the rest of the input then is
        # the pty's own wake that it can take more, nothing else
        "time.sleep(1)\n"
        "got = b''\n"
        "while len(got) < %d:\n"
        "    got += os.read(0, 65536)\n"
        "    open(d + '/count', 'w').write(str(len(got)))\n"
        "open(d + '/got.tmp', 'wb').write(got)\n"
        "os.rename(d + '/got.tmp', d + '/got')\n"
        "time.sleep(600)\n" % (OUT, len(SENT) + 1))
DEAF = ("import os, sys, time, tty\n"
        "tty.setraw(0)\n"
        "d = sys.argv[1]\n"
        "open(d + '/pid', 'w').write(str(os.getpid()))\n"
        "while not os.path.exists(d + '/die'): time.sleep(0.02)\n"
        "os._exit(7)\n")
RX = ("import os, sys, tty\n"
      "tty.setraw(0)\n"
      "f = open(sys.argv[1], 'ab', 0)\n"
      "open(sys.argv[1] + '.ready', 'w').close()\n"
      "while True:\n"
      "    b = os.read(0, 65536)\n"
      "    if not b: break\n"
      "    f.write(b)\n")
fails = 0


def check(name, ok, detail=""):
    global fails
    if ok:
        print("  ok   " + name)
    else:
        fails += 1
        print("  FAIL %s %s" % (name, detail))


def until(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    return cond()


def readf(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return b"" if "b" in mode else ""


class Attach:
    def __init__(self, env, name):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(DCH, [DCH, "-a", name], env)
            os._exit(127)
        os.set_inheritable(self.fd, False)
        self.out = b""

    def pump(self, secs, cond):
        end = time.time() + secs
        while time.time() < end and not cond():
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    self.out += os.read(self.fd, 65536)
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    time.sleep(0.05)
        return cond()

    def raw(self):
        # the client's own terminal in raw mode: it is attached
        return self.pump(20, lambda: not termios.tcgetattr(self.fd)[3] &
                         termios.ICANON)

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        os.close(self.fd)
        os.waitpid(self.pid, 0)


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchbp", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    home, state = os.path.join(tmp, "home"), os.path.join(tmp, "state")
    os.makedirs(home)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("DCH_") and not k.startswith("CLAUDE")}
    env.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
               DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    recdir = os.path.join(state, "dch", "sessions",
                          sockdir.replace("/", "-"))
    names, atts = [], []

    def dch(*a, timeout=30):
        try:
            return subprocess.run([DCH] + list(a), env=env,
                                  stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True,
                                  timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            return "no answer in %d s" % timeout

    def spawn(name, prog, *args):
        d = os.path.join(tmp, name + ".d")
        os.makedirs(d)
        names.append(name)
        rc = dch("--spawn", name, "--size", "80x24", sys.executable, "-c",
                 prog, *args)
        check("%s: the session spawned" % name, rc == 0, rc)
        return d

    def attach(name):
        a = Attach(env, name)
        atts.append(a)
        check("%s: attached" % name, a.raw(), a.out[-200:])
        return a

    def master(name):
        sock = os.path.join(sockdir, name + ".sock")
        out = subprocess.run(["ps", "-eo", "pid,command"],
                             capture_output=True, text=True).stdout
        for l in out.splitlines():
            if "--master-of" in l and l.rstrip().split(" -- ")[0].endswith(
                    " " + sock):
                return int(l.split()[0])
        return 0

    def rec(name):
        try:
            return json.loads(readf(os.path.join(recdir, name + ".json")))
        except ValueError:
            return None

    def zombie(pid):
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout
        return r.strip().startswith("Z")

    def deaf_end(name, d, a):
        """96 KB waiting, the master stopped, the program ended (exit 7),
        a key typed on the attach, the master continued."""
        rc = dch("--send", name, SENT, timeout=20)
        check("%s: --send of 96 KB to a program not reading: exit 0" % name,
              rc == 0, rc)
        mp, cp = master(name), until(lambda: readf(d + "/pid"), 10)
        os.kill(mp, signal.SIGSTOP)
        open(d + "/die", "w").close()
        dead = until(lambda: zombie(int(cp)), 10)
        os.write(a.fd, b"k")
        a.pump(0.5, lambda: False)
        check("%s: the program ended while the master was stopped" % name,
              dead, (mp, cp))
        os.kill(mp, signal.SIGCONT)
        return mp

    try:
        # --- busy: a program printing before it reads ---------------------
        d = spawn("busy", BUSY, os.path.join(tmp, "busy.d"))
        a = attach("busy")
        check("busy: the program is raw", until(
            lambda: os.path.exists(d + "/ready"), 20))
        rc = dch("--send", "busy", SENT, timeout=20)
        check("busy: --send of 96 KB to a program not reading: exit 0",
              rc == 0, rc)
        rc = dch("--keys", "busy", "Enter", timeout=20)
        check("busy: --keys behind it: answered, exit 0", rc == 0, rc)
        open(d + "/go", "w").close()
        check("busy: the program's 300 KB of output reached the attach while "
              "its input waited (the master did not block on the pty)",
              a.pump(30, lambda: a.out.count(b"x") >= OUT), a.out.count(b"x"))
        until(lambda: os.path.exists(d + "/got"), 30)
        data = readf(d + "/got", "rb")
        check("busy: the program then read all the input, in order",
              data == SENT.encode() + b"\r",
              (len(data), readf(d + "/count"), data[:20], data[-20:]))

        # --- exit: the program ends with its input still waiting ----------
        d = spawn("deaf", DEAF, os.path.join(tmp, "deaf.d"))
        a = attach("deaf")
        mp = deaf_end("deaf", d, a)
        gone = until(lambda: master("deaf") != mp, 10)
        end = until(lambda: (rec("deaf") or {}).get("end"), 10)
        check("exit: the master ended with its program, the record says "
              "exit 7 (not a failed write's exit 1)",
              gone and end == "exit 7", (gone, end))

        # --- held: a hold armed, the next program gets none of it ---------
        d = spawn("held", DEAF, os.path.join(tmp, "held.d"))
        rc = dch("--respawn", "held")
        check("held: a hold armed", rc == 0, rc)
        a = attach("held")
        mp = deaf_end("held", d, a)

        def cmd():
            r = subprocess.run([DCH, "--cmd", "held"], env=env,
                               capture_output=True, text=True, timeout=30)
            return json.loads(r.stdout) if r.returncode == 0 else None

        c = until(lambda: (lambda c: c if c and c["state"] == "held"
                           else None)(cmd()), 10)
        check("held: the session is held, the program's exit 7 kept",
              c and c["exit_code"] == 7 and master("held") == mp, c)
        rxf = os.path.join(d, "rx")
        rc = dch("--respawn", "held", "--", sys.executable, "-c", RX, rxf)
        c = until(lambda: (lambda c: c if c and c["pid"] else None)(cmd()),
                  10)
        # raw first, or setraw's flush would drop NEW too
        until(lambda: os.path.exists(rxf + ".ready"), 20)
        dch("--send", "held", "NEW")
        check("held: the program --respawn starts gets none of the old "
              "input, only what is sent to it",
              rc == 0 and c and until(lambda: readf(rxf) == "NEW", 10),
              (rc, c and c["state"], readf(rxf)[:40]))
    finally:
        for a in atts:
            a.close()
        for n in names:
            dch("-k", n)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d check(s) failed" % ("FAIL" if fails else "PASS", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
