"""run.sh's watchdog self-check: a test that hangs (#238).

With DCH_WD_PIDS set: starts a child, a dch session in this run's socket
dir and one in a temp dir of its own under DCH_TEST_TMP (#250), and a
session named -x in each (QA 1.60.0 LOW-T10: a name dch takes for a flag,
its master started directly, as an older dch made one), stops the first
two masters with SIGSTOP (LOW-T4: dch -k's SIGTERM waits for a SIGCONT),
writes
"<own pid> <child pid> <master pid> <session name> <master 2 pid> <temp dir>
<-x master pid> <-x master 2 pid>"
there and hangs until the watchdog ends it. Without: passes at once.
"""
import os, signal, socket, struct, subprocess, sys, tempfile, time

out = os.environ.get("DCH_WD_PIDS")
if not out:
    sys.exit(0)
kid = subprocess.Popen(["sleep", "30"])
name = "wd%d" % os.getpid()


def spawn(sockdir, n=name):
    """Start session n in sockdir; the master's pid off its socket."""
    sock = os.path.join(sockdir, n + ".sock")
    if n.startswith("-"):   # --spawn refuses it; out of our group, as --spawn's
        subprocess.Popen([os.environ["DCH"], "--master-of", sock, "--",
                          "sleep", "30"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        end = time.time() + 10
        while not os.path.exists(sock) and time.time() < end:
            time.sleep(0.05)
    else:
        subprocess.run([os.environ["DCH"], "--spawn", n, "sleep", "30"],
                       env=dict(os.environ, DCH_SOCKET_DIR=sockdir),
                       stdout=subprocess.DEVNULL, check=True)
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    if sys.platform == "darwin":  # SOL_LOCAL, LOCAL_PEERPID
        pid = struct.unpack("i", s.getsockopt(0, 2, 4))[0]
    else:
        pid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET,
                                               socket.SO_PEERCRED, 12))[0]
    # A MSG_READ opens the pty gate a master started directly holds until a
    # first client: then it exits when sleep does, even if no reaper reaches
    # it (two sat 10 hours, QA 1.63.0). --spawn sends one itself.
    s.sendall(struct.pack("<BH", 6, 4) + bytes(4))
    s.close()
    return pid


sockdir = os.path.join(os.environ["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid())
master = spawn(sockdir)
td = tempfile.mkdtemp(prefix="wd", dir=os.environ["DCH_TEST_TMP"])
master2 = spawn(os.path.join(td, "s"))
dash = spawn(sockdir, "-x")
os.makedirs(os.path.join(td, "d"))
dash2 = spawn(os.path.join(td, "d"), "-x")
os.kill(master, signal.SIGSTOP)
os.kill(master2, signal.SIGSTOP)
with open(out, "w") as f:
    f.write("%d %d %d %s %d %s %d %d\n" % (os.getpid(), kid.pid, master, name,
                                           master2, td, dash, dash2))
kid.wait()
