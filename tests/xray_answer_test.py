#!/usr/bin/env python3
"""`dch --xray <n> --answer <qid> - --json` (#692): the operator's answer to
an open question, recorded by the user's answer_command.

An ended Claude session (a history record), a fake answer_command that
records its argv, its DCH_* environment and its stdin, and exits by the
qid: OQ-ok 0, OQ-5 and OQ-3 5/3 (not open), OQ-4 4 (no such question),
OQ-2 2 (refused), OQ-9 9 (failed), OQ-sleep sleeps past the 5 s clock. Covered: each exit mapped to
its result line and exit; stdin byte-exact plus one "\\n"; the qid as the
last word; qid and text bounds (nothing runs on a refusal); every usage
refusal; the self-guard (DCH_SESSION set); no answer_command; host:name
over a loopback dch serve (the text reaches the host's command, never a ctl
child's argv; serve refuses a forged request's bad qid or text; --answer is
no --requests line). Temp HOME, DCH_CONFIG, runtime dirs.

Run:  python3 tests/xray_answer_test.py        (uses ./dch; run.sh isolates)
"""
import json, os, re, shutil, signal, socket, struct, subprocess, sys
import tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import OWNER, WHOIS  # noqa: E402
from serve_lib import fake_ts  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
PY = sys.executable
# XAN_OLD_HOST=<an older dch>: serve runs it, only the old-host row (by hand)
OLD = os.environ.get("XAN_OLD_HOST")
SID = "11111111-2222-4333-8444-5555555555a7"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:900])


T = os.path.realpath(tempfile.mkdtemp(prefix="xan",
                                      dir=os.environ.get("DCH_TEST_TMP")))
BASE = {k: v for k, v in os.environ.items()
        if not k.startswith(("DCH_", "CLAUDE", "Malloc"))}


def side(name, **kw):
    d = os.path.join(T, name)
    os.makedirs(os.path.join(d, "home"))
    os.makedirs(os.path.join(d, "proj dir"))
    return dict(BASE, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=d + "/st",
                HOME=d + "/home", DCH_LOGIN_ENV="0", DCH_CONFIG=d + "/config",
                SHELL="/bin/sh", **kw)


def ended(env, name="xq"):
    d = env["XDG_RUNTIME_DIR"]
    sock = os.path.join(d, "dch-%d" % os.getuid())
    hdir = os.path.join(env["XDG_STATE_HOME"], "dch", "sessions",
                        sock.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    proj = os.path.join(d, "proj dir")
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        json.dump({"name": name, "cwd": proj, "end": "exited",
                   "argv": ["claude"], "started": 1794299400,
                   "updated": 1794300000, "ended": 1794300000,
                   "sock_dir": sock,
                   "agent": {"session_id": SID, "cwd": proj,
                             "config_dir": os.path.join(env["HOME"],
                                                        ".claude")}}, f)
    return proj


def fake(env):
    """the fake answer_command and its record file, in env's HOME"""
    rec = os.path.join(env["HOME"], "rec.json")
    p = os.path.join(env["HOME"], "answer")
    with open(p, "w") as f:
        f.write("#!%s\n" % PY + r'''import json, os, sys, time
data = sys.stdin.buffer.read()
json.dump({"argv": sys.argv[1:], "stdin": data.hex(), "cwd": os.getcwd(),
           "env": {k: v for k, v in os.environ.items()
                   if k.startswith(("DCH_SESSION", "DCH_AGENT", "DCH_REF"))}},
          open(%r, "w"))
q = sys.argv[-1]
print("stdout is ignored")
if q == "OQ-sleep":
    time.sleep(30)
code = {"OQ-2": 2, "OQ-3": 3, "OQ-4": 4, "OQ-5": 5, "OQ-9": 9}.get(q, 0)
print('{"answered":%%s}' %% ("false" if code else "true"))
if code:
    sys.stderr.write({2: "xray: bad ruling", 3: "xray: OQ-3 is ANSWERED",
                      4: "xray: no OQ-4", 5: "xray: OQ-5 is DROPPED",
                      9: "ledger down"}[code] + "\nsecond line\n")
sys.exit(code)
''' % rec)
    os.chmod(p, 0o755)
    return p, rec


def config(env, line):
    with open(env["DCH_CONFIG"], "w") as f:
        f.write(line)


def run(env, *a, inp=b"", timeout=60):
    return subprocess.run([DCH, "--xray"] + list(a), env=env, input=inp,
                          capture_output=True, timeout=timeout,
                          cwd=env["HOME"])


def took(rec):
    try:
        with open(rec) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def local():
    env = side("loc")
    proj = ended(env)
    cmd, rec = fake(env)

    def ans(qid, inp, *extra, e=None):
        if os.path.exists(rec):
            os.unlink(rec)
        r = run(e or env, "xq", "--answer", qid, "-", "--json", *extra,
                inp=inp)
        return r.returncode, r.stdout.decode(), r.stderr.decode(), took(rec)

    # --- no answer_command ----------------------------------------------
    config(env, "")
    rc, out, err, got = ans("OQ-ok", b"yes")
    check("no answer_command: no_command, exit 1",
          rc == 1 and out == '{"answered":false,"why":"no_command"}\n',
          (rc, out, err))

    config(env, "answer_command = %s arg1\n" % cmd)
    # --- answered: the qid last, the text byte-exact on stdin -----------
    text = "Yes, ship it.\n\tthen tag\nUTF-8: naïve — \U0001f680"
    rc, out, err, got = ans("OQ-ok", text.encode())
    check("exit 0: answered, the one result line, exit 0",
          rc == 0 and out == '{"answered":true,"id":"OQ-ok"}\n' and not err,
          (rc, out, err))
    check("...the qid is the last argv word, after the config's words",
          got and got["argv"] == ["arg1", "OQ-ok"], got)
    check("...stdin is the text byte-exact plus one LF (multi-line, tab, "
          "UTF-8)", got and bytes.fromhex(got["stdin"]) ==
          text.encode() + b"\n", got)
    check("...the session environment as questions_command's, no DCH_REF",
          got and got["env"].get("DCH_SESSION") == "xq" and
          got["env"].get("DCH_SESSION_CWD") == proj and
          got["env"].get("DCH_AGENT") == "claude" and
          got["env"].get("DCH_AGENT_SESSION_ID") == SID and
          not any(k.startswith("DCH_REF") for k in got["env"]) and
          got["cwd"] == proj, got)
    rc, out, err, got = ans("OQ-ok", b"one line\n")
    check("...one trailing LF (echo's) is not doubled",
          rc == 0 and got and bytes.fromhex(got["stdin"]) == b"one line\n",
          (rc, got))

    # --- the command's exit mapped --------------------------------------
    rc, out, err, got = ans("OQ-3", b"no")
    check("exit 3: not_open, its stderr's first line, exit 5",
          rc == 5 and json.loads(out) == {
              "answered": False, "id": "OQ-3", "why": "not_open",
              "detail": "xray: OQ-3 is ANSWERED"}, (rc, out, err))
    rc, out, err, got = ans("OQ-5", b"no")
    check("exit 5: not_open, exit 5",
          rc == 5 and json.loads(out) == {
              "answered": False, "id": "OQ-5", "why": "not_open",
              "detail": "xray: OQ-5 is DROPPED"}, (rc, out, err))
    rc, out, err, got = ans("OQ-2", b"no")
    check("exit 2: refused, its stderr's first line, exit 3",
          rc == 3 and json.loads(out) == {
              "answered": False, "why": "refused",
              "detail": "xray: bad ruling"}, (rc, out, err))
    rc, out, err, got = ans("OQ-ok", b"-rf --not-an-option\n")
    check("a text starting with - reaches stdin intact, never argv",
          rc == 0 and got and got["argv"] == ["arg1", "OQ-ok"] and
          bytes.fromhex(got["stdin"]) == b"-rf --not-an-option\n",
          (rc, out, err, got))
    rc, out, err, got = ans("OQ-4", b"no")
    check("exit 4: no_question, exit 5",
          rc == 5 and json.loads(out) == {
              "answered": False, "id": "OQ-4", "why": "no_question",
              "detail": "xray: no OQ-4"}, (rc, out, err))
    rc, out, err, got = ans("OQ-9", b"no")
    check("another exit: failed, its stderr's first line, exit 3",
          rc == 3 and json.loads(out) == {
              "answered": False, "why": "failed", "detail": "ledger down"},
          (rc, out, err))
    t0 = time.time()
    rc, out, err, got = ans("OQ-sleep", b"no")
    dt = time.time() - t0
    check("a command past the clock: failed, timeout, exit 3, within "
          "5-15 s (%.1f s)" % dt,
          rc == 3 and json.loads(out) == {
              "answered": False, "why": "failed", "detail": "timeout"} and
          4.5 < dt < 15, (rc, out, err))
    config(env, "answer_command = %s/missing\n" % env["HOME"])
    rc, out, err, got = ans("OQ-ok", b"no")
    check("a command that cannot run: failed, exit 3",
          rc == 3 and '"why":"failed"' in out and "cannot run" in out,
          (rc, out, err))
    config(env, "answer_command = %s arg1\n" % cmd)

    # --- the qid --------------------------------------------------------
    q32 = "Q" * 32
    rc, out, err, got = ans(q32, b"ok")
    check("a 32-byte qid of . _ - runs",
          rc == 0 and got and got["argv"][-1] == q32, (rc, out, err))
    rc, out, err, got = ans("a.b_c-1", b"ok")
    check("...and a qid of . _ - too", rc == 0 and got, (rc, out, err))
    for q in ("-x", "--json", "Q" * 33, "", "a/b", "a:b", "a b", "é"):
        rc, out, err, got = ans(q, b"ok")
        check("qid %r: exit 2, bad qid, nothing runs" % q,
              rc == 2 and not out and got is None and
              (err.startswith("dch: bad qid") or q == ""), (rc, out, err))

    # --- the text bounds ------------------------------------------------
    rc, out, err, got = ans("OQ-ok", b"x" * 4000)
    check("a 4000-byte text: answered",
          rc == 0 and got and len(bytes.fromhex(got["stdin"])) == 4001,
          (rc, err))
    rc, out, err, got = ans("OQ-ok", b"x" * 3999 + b"\n")
    check("...4000 with echo's LF: answered", rc == 0 and got, (rc, err))
    for what, inp in (("4001 bytes", b"x" * 4001),
                      ("4097 bytes", b"x" * 4097),
                      ("1 MB", b"x" * (1 << 20)),
                      ("empty", b""), ("a lone LF", b"\n"),
                      ("a NUL", b"a\x00b"), ("an ESC", b"a\x1b[31mb"),
                      ("a CR", b"a\rb"), ("a DEL", b"a\x7fb"),
                      ("a C1 (U+0085)", "a\u0085b".encode()),
                      ("a C1 (U+009B)", "a\u009bb".encode()),
                      ("invalid UTF-8", b"a\xffb"),
                      ("a lone continuation byte", b"a\x80b"),
                      ("an overlong /", b"a\xc0\xafb"),
                      ("a surrogate", b"a\xed\xa0\x80b")):
        rc, out, err, got = ans("OQ-ok", inp)
        check("text %s: exit 2, bad answer, nothing runs" % what,
              rc == 2 and not out and got is None and
              err.startswith("dch: bad answer"), (rc, out, err))

    # --- usage ----------------------------------------------------------
    def usage(args, want, e=None):
        if os.path.exists(rec):
            os.unlink(rec)
        r = run(e or env, *args, inp=b"ok")
        check("usage %s: exit 2, %r" % (" ".join(args), want),
              r.returncode == 2 and not r.stdout and
              r.stderr.decode() == want + "\n" and not os.path.exists(rec),
              (r.returncode, r.stdout, r.stderr))

    A = ["xq", "--answer", "OQ-ok", "-", "--json"]
    for flag in (["--follow"], ["--tasks"], ["--memory"], ["--questions"],
                 ["--schedules"], ["--ref", "dch.c:1"], ["--search", "abc"],
                 ["--chapter", "c1"], ["--clear"]):
        usage(A + flag, "dch: bad usage: --answer and %s" % flag[0])
        usage(flag + A, "dch: bad usage: --answer and %s" % flag[0])
    usage(A + ["--since", "1h"], "dch: bad usage: --answer and --since")
    bad = "dch: bad usage: --answer <qid> - (the answer on stdin)"
    usage(["xq", "--answer", "OQ-ok", "the text", "--json"], bad)
    usage(["xq", "--answer", "OQ-ok", "--json", "-"], bad)
    usage(["xq", "--json", "--answer", "OQ-ok"], bad)
    usage(["xq", "--json", "--answer"], bad)
    usage(A + ["more"], "dch: bad usage: one session per --xray")
    usage(A + ["--answer", "OQ-3", "-"], "dch: bad usage: --answer twice")
    usage(["xq", "--answer", "OQ-ok", "-"],
          "dch: bad usage: --json is required (the --xray view takes no "
          "flag)")
    usage(["srv:xq", "--requests", "--json", "--answer", "OQ-ok", "-"],
          "dch: bad usage: --requests takes host:name and --json, nothing "
          "else")

    # --- the self-guard -------------------------------------------------
    for v in ("xq", "other"):
        usage(A, "dch: --answer is the operator's: not from inside a "
              "session", e=dict(env, DCH_SESSION=v))


def forged(port, argv):
    """one raw 0x30 to the loopback serve: the 0x13's message, or what
    came"""
    s = socket.create_connection(("127.0.0.1", port), timeout=20)
    try:
        p = json.dumps({"argv": argv}).encode()
        s.sendall(struct.pack(">BI", 0x30, len(p)) + p)
        while True:
            h = b""
            while len(h) < 5:
                c = s.recv(5 - len(h))
                if not c:
                    return "closed"
                h += c
            t, n = struct.unpack(">BI", h)
            body = b""
            while len(body) < n:
                c = s.recv(n - len(body))
                if not c:
                    return "closed"
                body += c
            if t == 0x13:
                return json.loads(body).get("message")
            if t == 0x33:
                return "ran %s" % body.decode()
    except OSError as e:
        return "lost: %s" % e
    finally:
        s.close()


def remote():
    tmp = os.path.join(T, "net")
    fk, wrap, logs = (os.path.join(tmp, d) for d in ("fake", "wrap", "logs"))
    for d in (fk, wrap, logs):
        os.makedirs(d)
    fake_ts(fk)
    for name, obj in (("status.json", OWNER), ("whois.json", WHOIS)):
        with open(os.path.join(fk, name), "w") as f:
            json.dump(obj, f)
    with open(os.path.join(fk, "ip.txt"), "w") as f:
        f.write("100.100.1.1\n")
    with open(os.path.join(wrap, "dch"), "w") as f:
        # each ctl child's argv, what the host's ps shows
        f.write("#!/bin/sh\nprintf '%%s\\n' \"$@\" > '%s/.argv.'$$\n"
                "/bin/mv '%s/.argv.'$$ '%s/argv.'$$\nexec '%s' \"$@\"\n"
                % (logs, logs, logs, DCH))
    for p in (os.path.join(fk, "tailscale"), os.path.join(wrap, "dch")):
        os.chmod(p, 0o755)
    path = wrap + ":" + fk + ":/usr/bin:/bin"
    senv = side("srv", DCH_NO_REMOTE="1", DCH_SERVE_TEST_LOOPBACK="1",
                PATH=path)
    cenv = side("cli", DCH_REMOTE_BOX_MS="5000", PATH=path)
    ended(senv)
    cmd, rec = fake(senv)
    config(senv, "answer_command = %s\n" % cmd)
    ccmd, crec = fake(cenv)
    log = os.path.join(tmp, "serve.log")
    with open(log, "w") as lf:
        sp = subprocess.Popen(["dch", "serve", "--bind", "127.0.0.1",
                               "--port", "0"], executable=OLD or DCH, env=senv,
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
        if port is None:
            return
        config(cenv, "remote_hosts = srv=127.0.0.1:%d\n"
                     "answer_command = %s\n" % (port, ccmd))
        if OLD:
            r = run(cenv, "srv:xq", "--answer", "OQ-ok", "-", "--json",
                    inp=b"x")
            check("a host whose dch does not know --answer: exit 1, said",
                  r.returncode == 1 and not r.stdout and
                  b"dch: srv: the host's dch does not know --answer" in
                  r.stderr, (r.returncode, r.stdout, r.stderr))
            return
        text = "Remote: two\nlines\tand ümläut"
        r = run(cenv, "srv:xq", "--answer", "OQ-ok", "-", "--json",
                inp=text.encode() + b"\n")
        got = took(rec)
        check("host:name: the host's answer_command answers, exit 0",
              r.returncode == 0 and
              r.stdout == b'{"answered":true,"id":"OQ-ok"}\n',
              (r.returncode, r.stdout, r.stderr, open(log).read()[-500:]))
        check("...the text reaches it byte-exact, the qid last, the host's "
              "session env", got and
              bytes.fromhex(got["stdin"]) == text.encode() + b"\n" and
              got["argv"] == ["OQ-ok"] and
              got["env"].get("DCH_SESSION") == "xq", got)
        check("...the client's own answer_command never ran",
              took(crec) is None)
        ctl = []
        for n in sorted(os.listdir(logs)):
            with open(os.path.join(logs, n), encoding="utf-8") as f:
                a = f.read().split("\n")[:-1]
            if "--answer" in a:
                ctl.append(a)
        check("...the ctl child's argv holds \"-\", never the text",
              ctl == [["--xray", "xq", "--answer", "OQ-ok", "-", "--json"]],
              ctl)
        check("...and serve's log line names no text",
              "Remote" not in open(log, encoding="utf-8",
                                   errors="replace").read())
        for q, rc, why in (("OQ-3", 5, "not_open"),
                           ("OQ-4", 5, "no_question"), ("OQ-9", 3, "failed")):
            r = run(cenv, "srv:xq", "--answer", q, "-", "--json", inp=b"x")
            check("host:name %s: why %s, exit %d" % (q, why, rc),
                  r.returncode == rc and
                  json.loads(r.stdout).get("why") == why,
                  (r.returncode, r.stdout, r.stderr))
        os.unlink(rec)
        r = run(cenv, "srv:xq", "--answer", "OQ-ok", "-", "--json",
                inp=b"a\x1bb")
        check("host:name, a bad text: the client refuses it, exit 2",
              r.returncode == 2 and b"bad answer" in r.stderr and
              took(rec) is None, (r.returncode, r.stderr))
        r = run(cenv, "srv:xq", "--answer", "-q", "-", "--json", inp=b"x")
        check("host:name, a bad qid: the client refuses it, exit 2",
              r.returncode == 2 and b"bad qid" in r.stderr, r.stderr)
        r = run(dict(cenv, DCH_SESSION="peer"), "srv:xq", "--answer",
                "OQ-ok", "-", "--json", inp=b"x")
        check("host:name from inside a session: the self-guard, exit 2",
              r.returncode == 2 and b"operator's" in r.stderr, r.stderr)
        # a forged request: serve's own check, nothing runs
        A = ["--xray", "xq", "--answer"]
        for what, argv, want in (
                ("an ESC", A + ["OQ-ok", "a\x1bb", "--json"], "bad answer"),
                ("a C1", A + ["OQ-ok", "a\u0085b", "--json"], "bad answer"),
                ("4001 bytes", A + ["OQ-ok", "x" * 4001, "--json"],
                 "bad answer"),
                ("an empty text", A + ["OQ-ok", "", "--json"],
                 "bad answer"),
                ("a qid -x", A + ["-x", "ok", "--json"], "bad qid"),
                ("a 33-byte qid", A + ["Q" * 33, "ok", "--json"],
                 "bad qid"),
                ("no text", ["--xray", "xq", "--json", "--answer", "OQ-ok"],
                 "--answer needs a qid and a text"),
                ("--answer with --follow",
                 A + ["OQ-ok", "ok", "--json", "--follow"], "exclude"),
                ("no --json", A + ["OQ-ok", "ok"], "needs --json")):
            got = forged(port, argv)
            check("serve refuses a forged --answer with %s: %r" % (what, want),
                  isinstance(got, str) and want in got and
                  took(rec) is None, got)
        got = forged(port, A + ["OQ-ok", "fine\nby me", "--json"])
        check("...and runs a forged one that passes, the text on stdin",
              got.startswith("ran") and took(rec) and
              bytes.fromhex(took(rec)["stdin"]) == b"fine\nby me\n", got)
        r = subprocess.run([DCH, "--xray", "srv:xq", "--requests", "--json"],
                           env=cenv, capture_output=True, timeout=60,
                           input=b'{"req":1,"argv":["--answer","OQ-ok","x",'
                                 b'"--json"]}\n')
        check("--answer is no --requests line",
              b"--answer is not a request" in r.stdout + r.stderr,
              (r.returncode, r.stdout, r.stderr))
    finally:
        if sp.poll() is None:
            sp.send_signal(signal.SIGTERM)
            try:
                sp.wait(10)
            except subprocess.TimeoutExpired:
                sp.kill()
                sp.wait(10)


def main():
    try:
        local()
        remote()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print("all ok" if not fails[0] else "%d FAILED" % fails[0])
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
