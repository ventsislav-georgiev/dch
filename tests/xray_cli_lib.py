"""Shared by tests/xray_shell_test.py, xray_claude_test.py and xray_follow_test.py
(split from the one xray_cli_test.py): the isolated env, check(), the dch and
xray runners, and the helpers more than one of them uses."""
import json, os, random, re, shutil, socket, stat, struct, subprocess, sys
import tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xray_ring as X  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ID = "m0123456789abcdef"
UP = "M0123456789ABCDEF"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:800])


T = tempfile.mkdtemp(prefix="xc")
HOME = os.path.join(T, "home")
os.makedirs(HOME)
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_SESSION", "DCH_SOCKET_DIR", "DCH_DEBUG",
                            "CLAUDE", "Malloc"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
os.environ.update(XDG_RUNTIME_DIR=T)     # xray_ring.sock_path
os.environ.pop("DCH_SOCKET_DIR", None)


def dch(*a, env=None):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=env or ENV)


def xray(*a, env=None):
    r = dch("--xray", *a, env=env)
    try:
        return r.returncode, json.loads(r.stdout), r.stderr
    except ValueError:
        return r.returncode, None, r.stderr


def wait_for(fn, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def screen(name):
    return dch("--read", name).stdout


def sock(name):
    return os.path.join(T, "dch-%d" % os.getuid(), name + ".sock")


# ---- 3.8: a 16M ring under 16 MB ------------------------------------------

# ASan's shadow memory and quarantine inflate RSS past any budget for dch
# itself; scripts/sanitize.sh exports ASAN_OPTIONS.
SANITIZED = "ASAN_OPTIONS" in os.environ
HELPER = os.environ.get("DCH_MAXRSS")   # tests/maxrss.c, built by run.sh


def maxrss(argv, env):
    """(rc, stdout, max RSS in bytes) of one run, through tests/maxrss.c: a
    child Python spawns starts at Python's RSS high-water mark on Linux."""
    if not HELPER:
        return -1, "", 1 << 62
    r = subprocess.run([HELPER] + argv, capture_output=True, text=True,
                       timeout=90, env=env)
    m = re.search(r"^maxrss (\d+)$", r.stderr, re.M)
    return r.returncode, r.stdout, int(m.group(1)) if m else 1 << 62


def rss_ok(what, rss):
    """The 16 MiB budget (3.8); skipped, visibly, under sanitizers."""
    if SANITIZED:
        print("  skip %s: %.1f MB, no RSS budget under ASan" % (what, rss / 1e6))
        return
    check("%s: under 16 MB (3.8): %.1f MB" % (what, rss / 1e6),
          rss < 16 << 20, "no DCH_MAXRSS helper" if not HELPER else rss)


# ---- a live Claude and the open seam --------------------------------------

def key(cwd):
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in cwd)


def is_master(pid):
    """pid runs `dch --master-of ...`: /proc on Linux, ps elsewhere."""
    try:
        a = open("/proc/%d/cmdline" % pid, "rb").read()
    except OSError:
        a = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                           capture_output=True).stdout
    return b"--master-of" in a


def files(root):
    """Less a live master's history record mid-write (master.c rec_write:
    <name>.json.<its pid>.tmp, then a rename, every DCH_RECORD_MS; #283):
    the pid is a `--master-of` process, so never the X-ray's own write."""
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in dn + fn:
            p = os.path.join(dp, f)
            m = re.search(r"\.json\.(\d+)\.tmp$", f)
            if m and is_master(int(m.group(1))):
                continue
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


# ---- refs (#135, XR2a): the index's lifetime, --ref's budget and stop -----

RSID = "11111111-2222-4333-8444-5555555555a1"


def ref_lines(out):
    try:
        return [json.loads(l) for l in out.splitlines() if l.strip()]
    except ValueError:
        return None


# ---- 3.5: --chapter <anchor> --json (#156, XR3b) --------------------------

def chapter(n, anchor, env=None):
    r = dch("--xray", n, "--chapter", anchor, "--json", env=env)
    try:
        c = json.loads(r.stdout)
    except ValueError:
        return r.returncode, None, r.stderr, r.stdout
    # #356: a transcript entry's record offset, a number (xray_view_test's
    # twins rows check where it points); the rows here compare the rest
    for e in c.get("entries", []):
        if isinstance(e.get("offset"), int) and e["offset"] >= 0:
            del e["offset"]
    return r.returncode, c, r.stderr, r.stdout


def ent(role, text, tool=None, error=False):
    return {"role": role, "tool": tool, "text": text, "error": error}
