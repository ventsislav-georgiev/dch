#!/usr/bin/env python3
"""The picker's MERGED column lines its marks up (#110): each count is padded
to the widest one, so "✓  385" sits over "✓ 1437" and "✓    7", the ✓ in one
column and the digits right-aligned. Self-isolating (own XDG_RUNTIME_DIR).

Run:  python3 tests/picker_merged_test.py        (uses ./dch)
"""
import os, pty, re, select, shutil, struct, subprocess, sys, tempfile, time
import fcntl, termios

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


def main():
    tmp = tempfile.mkdtemp(dir="/tmp", prefix="dpm")
    env = {k: v for k, v in os.environ.items()
           if k not in ("DCH_SESSION", "DCH_SOCKET_DIR")}
    env.update(XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=tmp, HOME=tmp,
               DCH_LOGIN_ENV="0", DCH_CONFIG=tmp + "/config",
               DCH_NO_REMOTE="1", TERM="xterm-256color", LANG="en_US.UTF-8")
    env.pop("NO_COLOR", None)
    counts = {"pma": 385, "pmb": 1437, "pmc": 7}
    pid = None
    try:
        for n, m in counts.items():
            subprocess.run([DCH, "--spawn", n, "sh", "-c", "sleep 60"],
                           env=env, check=True, capture_output=True)
            subprocess.run([DCH, "--meta", n,
                            '{"prs":{"merged":%d}}' % m], env=env, check=True)
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(DCH, [DCH, "-l"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 140, 0, 0))
        out, end = b"", time.time() + 10
        cells = []
        while time.time() < end and len(cells) < 3:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    break
            txt = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "\n",
                         out.decode("utf-8", "replace"))
            cells = sorted({m.group(0) for m in
                            re.finditer(r"✓ *\d+", txt)}, key=len)
        os.write(fd, b"q")
        want = ["✓    7", "✓  385", "✓ 1437"]
        got = sorted(cells, key=lambda c: int(c.split()[-1]))
        ok = got == want
        print("  %s merged counts padded to one width: %r" %
              ("ok  " if ok else "FAIL", got))
        return 0 if ok else 1
    finally:
        if pid:
            # drain while it quits; a picker still up after 5 s is killed
            end = time.time() + 5
            while time.time() < end and not os.waitpid(pid, os.WNOHANG)[0]:
                try:
                    if select.select([fd], [], [], 0.1)[0]:
                        os.read(fd, 65536)
                except OSError:
                    pass
            else:
                if time.time() >= end:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
        subprocess.run([DCH, "-kl"], env=env, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
