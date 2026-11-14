#!/usr/bin/env python3
"""dch serve keeps X-ray's test switches from its ctl children (QA 1.60.0
LOW-T8): a serve started with DCH_XRAY_NOCLOCK and DCH_XRAY_CAPS exported
runs every ctl (here the `--ls-json` behind a peer's list) without them, as
it does DCH_SERVE_TEST_LOOPBACK and DCH_CTL_*. The serve runs as `dch` found
on PATH, a wrapper there that logs the DCH_XRAY_ variables each ctl gets and
execs the real dch. Loopback only, a fake tailscale, temp HOME.
LOW-XN1 (QA 1.63.0): a peer's --search and --ref through serve answer as
the host's own dch does, and the text is never in the ctl child's argv
(the wrapper logs "$@", what the host's ps shows): "-" there, the text on
its stdin. #319: a peer's --tasks answers as the host's dch.

Run:  python3 tests/serve_xray_env_test.py        (uses ./dch)
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import OWNER, WHOIS  # noqa: E402
from xray_deny_test import FAKE_TS  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


SECRET, REF = "Zq7SecretNeedle", "https://github.com/o/r/pull/41"


def xn1(senv, cenv, logs, check):
    """LOW-XN1: the host's session xa holds SECRET and REF; a peer's
    --search and --ref of them answer as the host's dch run there does,
    and no ctl child of serve's had either in its argv."""
    sid = "11111111-2222-4333-8444-5555555555f1"
    cfg = os.path.join(senv["HOME"], ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir)
    with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
        for who, t in (("user", "find %s in %s now"),
                       ("assistant", "found %s at %s")):
            c = t % (SECRET, REF)
            f.write(json.dumps({"type": who, "cwd": "/home/demo/proj",
                                "message": {"content": c if who == "user"
                                            else [{"type": "text",
                                                   "text": c}]}}) + "\n")
    sock = os.path.join(senv["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid())
    hdir = os.path.join(senv["XDG_STATE_HOME"], "dch", "sessions",
                        sock.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    with open(os.path.join(hdir, "xa.json"), "w") as f:
        json.dump({"name": "xa", "cwd": "/home/demo/proj", "end": "exited",
                   "argv": ["claude"], "started": 1794299400,
                   "updated": 1794300000, "ended": 1794300000,
                   "sock_dir": sock,
                   "agent": {"session_id": sid, "cwd": "/home/demo/proj",
                             "config_dir": cfg}}, f)
    # the host's own run, with the environment serve gives its ctls
    henv = {k: v for k, v in senv.items() if not k.startswith("DCH_XRAY_")}
    clock = re.compile(rb'"elapsed_ms":\d+')
    for flag, val in (("--search", SECRET), ("--ref", REF)):
        loc = subprocess.run([DCH, "--xray", "xa", flag, val, "--json"],
                             env=henv, capture_output=True, timeout=30,
                             stdin=subprocess.DEVNULL)
        rem = subprocess.run([DCH, "--xray", "srv:xa", flag, val, "--json"],
                             env=cenv, capture_output=True, timeout=60,
                             stdin=subprocess.DEVNULL)
        check("a peer's %s answers as the host's own dch (%d bytes)"
              % (flag, len(loc.stdout)),
              loc.returncode == 0 and val.encode() in loc.stdout and
              (rem.returncode, clock.sub(b"", rem.stdout)) ==
              (0, clock.sub(b"", loc.stdout)),
              (loc.returncode, loc.stdout[:300], loc.stderr[:200],
               rem.returncode, rem.stdout[:300], rem.stderr[:300]))
    argvs = []
    for n in sorted(os.listdir(logs)):
        if n.startswith("argv."):
            with open(os.path.join(logs, n)) as f:
                argvs.append(f.read().split("\n")[:-1])
    ran = [a for a in argvs if "--search" in a or "--ref" in a]
    check("both ran as ctl children of serve's (%d)" % len(ran),
          len(ran) == 2, argvs)
    check("...with \"-\" for the text in their argv, never the text",
          ran and all(a[a.index(f) + 1] == "-" for a in ran
                      for f in ("--search", "--ref") if f in a) and
          not any(SECRET in x or "pull/41" in x for a in argvs for x in a),
          ran)


def tasks(senv, cenv, check):
    """#319: a peer's --tasks through serve answers as the host's own dch:
    the host's session xt, a Claude with one task."""
    sid = "11111111-2222-4333-8444-5555555555f2"
    cfg = os.path.join(senv["HOME"], ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
        for o in ({"type": "user", "cwd": "/home/demo/proj",
                   "timestamp": "2026-11-14T07:00:00Z",
                   "message": {"content": "plan it"}},
                  {"type": "assistant", "timestamp": "2026-11-14T07:00:01Z",
                   "message": {"content": [{
                       "type": "tool_use", "id": "toolu_1",
                       "name": "TaskCreate", "input": {
                           "subject": "Port the parser",
                           "description": "", "activeForm": "Porting"}}]}},
                  {"type": "user", "timestamp": "2026-11-14T07:00:02Z",
                   "toolUseResult": {"task": {"id": "1"}},
                   "message": {"content": [{"type": "tool_result",
                                            "tool_use_id": "toolu_1",
                                            "content": "ok"}]}}):
            f.write(json.dumps(o) + "\n")
    sock = os.path.join(senv["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid())
    hdir = os.path.join(senv["XDG_STATE_HOME"], "dch", "sessions",
                        sock.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    with open(os.path.join(hdir, "xt.json"), "w") as f:
        json.dump({"name": "xt", "cwd": "/home/demo/proj", "end": "exited",
                   "argv": ["claude"], "started": 1794299400,
                   "updated": 1794300000, "ended": 1794300000,
                   "sock_dir": sock,
                   "agent": {"session_id": sid, "cwd": "/home/demo/proj",
                             "config_dir": cfg}}, f)
    henv = {k: v for k, v in senv.items() if not k.startswith("DCH_XRAY_")}
    loc = subprocess.run([DCH, "--xray", "xt", "--tasks", "--json"],
                         env=henv, capture_output=True, timeout=30,
                         stdin=subprocess.DEVNULL)
    rem = subprocess.run([DCH, "--xray", "srv:xt", "--tasks", "--json"],
                         env=cenv, capture_output=True, timeout=60,
                         stdin=subprocess.DEVNULL)
    ls = loc.stdout.splitlines()
    check("a peer's --tasks answers as the host's own dch: the header, "
          "the task, done",
          loc.returncode == 0 and len(ls) == 3 and
          b'"source":"claude"' in ls[0] and b'"Port the parser"' in ls[1] and
          ls[2] == b'{"done":true,"count":1,"dropped":0}' and
          (rem.returncode, rem.stdout) == (0, loc.stdout),
          (loc.returncode, loc.stdout[:300], loc.stderr[:200],
           rem.returncode, rem.stdout[:300], rem.stderr[:300]))


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchsxe", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    fake, wrap, logs = (os.path.join(tmp, d) for d in ("fake", "wrap", "logs"))
    for d in (fake, wrap, logs):
        os.makedirs(d)
    with open(os.path.join(fake, "tailscale"), "w") as f:
        f.write(FAKE_TS)
    for name, obj in (("status.json", OWNER), ("whois.json", WHOIS)):
        with open(os.path.join(fake, name), "w") as f:
            json.dump(obj, f)
    with open(os.path.join(fake, "ip.txt"), "w") as f:
        f.write("100.100.1.1\n")
    with open(os.path.join(wrap, "dch"), "w") as f:
        # written aside, then renamed: a ctl.<pid> file is complete
        f.write("#!/bin/sh\nprintf '%%s\\n' \"$@\" > '%s/.argv.'$$\n"
                "/bin/mv '%s/.argv.'$$ '%s/argv.'$$\n"
                "/usr/bin/env | /usr/bin/grep '^DCH_XRAY_' "
                "> '%s/.ctl.'$$\n/bin/mv '%s/.ctl.'$$ '%s/ctl.'$$\n"
                "exec '%s' \"$@\"\n" % ((logs,) * 6 + (DCH,)))
    for p in (os.path.join(fake, "tailscale"), os.path.join(wrap, "dch")):
        os.chmod(p, 0o755)
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("DCH_", "MallocStackLogging"))}

    def side(name, **kw):
        d = os.path.join(tmp, name)
        os.makedirs(os.path.join(d, "home"))
        return dict(base, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=d + "/st",
                    HOME=d + "/home", DCH_LOGIN_ENV="0",
                    DCH_CONFIG=d + "/config", SHELL="/bin/sh",
                    PATH=wrap + ":" + fake + ":/usr/bin:/bin", **kw)

    senv = side("srv", DCH_NO_REMOTE="1", DCH_SERVE_TEST_LOOPBACK="1",
                DCH_XRAY_NOCLOCK="1", DCH_XRAY_CAPS="1:1000")
    # the list's 300 ms box (RM_BOX_MS) can end before serve has forked
    # its ctl on a loaded machine; the box is not what this test is about
    cenv = side("cli", DCH_REMOTE_BOX_MS="5000")
    log = os.path.join(tmp, "serve.log")
    fails = []

    def check(msg, cond, extra=""):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)
            if extra:
                print("       " + str(extra)[:900])

    with open(log, "w") as lf:
        sp = subprocess.Popen(["dch", "serve", "--bind", "127.0.0.1",
                               "--port", "0"], executable=DCH, env=senv,
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=lf)
    try:
        port, end = None, time.time() + 30
        while time.time() < end and port is None and sp.poll() is None:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(log).read())
            port = int(m.group(1)) if m else None
            time.sleep(0.05)
        check("dch serve listens on a loopback test port", port is not None,
              open(log).read()[-600:])
        if port is not None:
            with open(cenv["DCH_CONFIG"], "w") as f:
                f.write("remote_hosts = srv=127.0.0.1:%d\n" % port)
            # ask again until a ctl's log is complete, 30 s in all
            got, end, r = {}, time.time() + 30, None
            while not got and time.time() < end:
                try:
                    r = subprocess.run([DCH, "--ls-json", "--remote"],
                                       env=cenv, capture_output=True,
                                       timeout=max(1, end - time.time()),
                                       text=True, stdin=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    pass
                for n in sorted(os.listdir(logs)):
                    if n.startswith("ctl."):
                        with open(os.path.join(logs, n)) as f:
                            got[n] = f.read()
            check("a peer's list ran a ctl child of serve's",
                  len(got) > 0, (r and (r.returncode, r.stdout, r.stderr),
                                 open(log).read()[-600:]))
            check("...without DCH_XRAY_NOCLOCK or DCH_XRAY_CAPS in its "
                  "environment", got and not any(got.values()), got)
            xn1(senv, cenv, logs, check)
            tasks(senv, cenv, check)
    finally:
        if sp.poll() is None:
            sp.send_signal(signal.SIGTERM)
            try:
                sp.wait(10)
            except subprocess.TimeoutExpired:
                sp.kill()
                sp.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)
    print("all ok" if not fails else "%d FAILED" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
