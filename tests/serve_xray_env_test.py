#!/usr/bin/env python3
"""dch serve keeps X-ray's test switches from its ctl children (QA 1.60.0
LOW-T8): a serve started with DCH_XRAY_NOCLOCK and DCH_XRAY_CAPS exported
runs every ctl (here the `--ls-json` behind a peer's list) without them, as
it does DCH_SERVE_TEST_LOOPBACK and DCH_CTL_*. The serve runs as `dch` found
on PATH, a wrapper there that logs the DCH_XRAY_ variables each ctl gets and
execs the real dch. Loopback only, a fake tailscale, temp HOME.

Run:  python3 tests/serve_xray_env_test.py        (uses ./dch)
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import OWNER, WHOIS  # noqa: E402
from xray_deny_test import FAKE_TS  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


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
        f.write("#!/bin/sh\n/usr/bin/env | /usr/bin/grep '^DCH_XRAY_' "
                "> '%s/ctl.'$$\nexec '%s' \"$@\"\n" % (logs, DCH))
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
    cenv = side("cli")
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
            r = subprocess.run([DCH, "--ls-json", "--remote"], env=cenv, timeout=30,
                               capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
            got = {}
            for n in sorted(os.listdir(logs)):
                with open(os.path.join(logs, n)) as f:
                    got[n] = f.read()
            check("a peer's list ran a ctl child of serve's",
                  len(got) > 0, (r.returncode, r.stdout, r.stderr,
                                 open(log).read()[-600:]))
            check("...without DCH_XRAY_NOCLOCK or DCH_XRAY_CAPS in its "
                  "environment", got and not any(got.values()), got)
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
