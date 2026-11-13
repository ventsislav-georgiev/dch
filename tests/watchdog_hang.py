"""run.sh's watchdog self-check: a test that hangs (#238).

With DCH_WD_PIDS set: starts a child, a dch session in this run's socket
dir and one in a temp dir of its own under DCH_TEST_TMP (#250), writes
"<own pid> <child pid> <master pid> <session name> <master 2 pid> <temp dir>"
there and hangs until the watchdog ends it. Without: passes at once.
"""
import os, socket, struct, subprocess, sys, tempfile

out = os.environ.get("DCH_WD_PIDS")
if not out:
    sys.exit(0)
kid = subprocess.Popen(["sleep", "30"])
name = "wd%d" % os.getpid()


def spawn(sockdir):
    """Start session name in sockdir; the master's pid off its socket."""
    subprocess.run([os.environ["DCH"], "--spawn", name, "sleep", "30"],
                   env=dict(os.environ, DCH_SOCKET_DIR=sockdir),
                   stdout=subprocess.DEVNULL, check=True)
    s = socket.socket(socket.AF_UNIX)
    s.connect(os.path.join(sockdir, name + ".sock"))
    if sys.platform == "darwin":  # SOL_LOCAL, LOCAL_PEERPID
        pid = struct.unpack("i", s.getsockopt(0, 2, 4))[0]
    else:
        pid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET,
                                               socket.SO_PEERCRED, 12))[0]
    s.close()
    return pid


master = spawn(os.path.join(os.environ["XDG_RUNTIME_DIR"],
                            "dch-%d" % os.getuid()))
td = tempfile.mkdtemp(prefix="wd", dir=os.environ["DCH_TEST_TMP"])
master2 = spawn(os.path.join(td, "s"))
with open(out, "w") as f:
    f.write("%d %d %d %s %d %s\n" % (os.getpid(), kid.pid, master, name,
                                     master2, td))
kid.wait()
