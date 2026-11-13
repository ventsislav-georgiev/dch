"""run.sh's watchdog self-check: a test that hangs (#238).

With DCH_WD_PIDS set: starts a child and a dch session in this run's socket
dir, writes "<own pid> <child pid> <master pid> <session name>" there and
hangs until the watchdog ends it. Without: passes at once.
"""
import os, socket, struct, subprocess, sys

out = os.environ.get("DCH_WD_PIDS")
if not out:
    sys.exit(0)
kid = subprocess.Popen(["sleep", "30"])
name = "wd%d" % os.getpid()
subprocess.run([os.environ["DCH"], "--spawn", name, "sleep", "30"],
               stdout=subprocess.DEVNULL, check=True)
s = socket.socket(socket.AF_UNIX)
s.connect(os.path.join(os.environ["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid(),
                       name + ".sock"))
if sys.platform == "darwin":  # SOL_LOCAL, LOCAL_PEERPID
    master = struct.unpack("i", s.getsockopt(0, 2, 4))[0]
else:
    master = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET,
                                              socket.SO_PEERCRED, 12))[0]
s.close()
with open(out, "w") as f:
    f.write("%d %d %d %s\n" % (os.getpid(), kid.pid, master, name))
kid.wait()
