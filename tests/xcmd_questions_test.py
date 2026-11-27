#!/usr/bin/env python3
"""`dch --xray <n> --questions --json` with a questions_command (#652): the
tasks_command contract (run, env, lines, limits, warn, title line) with no
built-in source: no command, no row or a failure is source none, no row.

An ended Claude session (a history record; no transcript is read). Covered:
no command -> source none, no rows; bare and {"question":{...}} rows ->
source command, the title line, every field; a failing command -> the
header's warn, source none; the row cap and a line over 64 KB as --tasks
counts them; the command's environment; no --json and the flags it
excludes (exit 2). Temp HOME, DCH_CONFIG, runtime dir.

Run:  python3 tests/xcmd_questions_test.py        (uses ./dch; run.sh isolates)
"""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:900])


T = os.path.realpath(tempfile.mkdtemp(prefix="xcq",
                                      dir=os.environ.get("DCH_TEST_TMP")))
HOME = os.path.join(T, "home")
PROJ = os.path.join(T, "proj dir")
os.makedirs(HOME)
os.makedirs(PROJ)
CONFIG = os.path.join(T, "config")
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_SESSION", "DCH_SOCKET_DIR", "DCH_DEBUG",
                            "CLAUDE", "Malloc"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=CONFIG, DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
SID = "11111111-2222-4333-8444-5555555555e1"
PY = sys.executable
NONE = {"schema": 1, "session": "xq", "session_id": SID, "source": "none",
        "title": None, "partial": False}
DONE0 = {"done": True, "count": 0, "dropped": 0}


def dch(*a, timeout=30):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=timeout, env=ENV, stdin=subprocess.DEVNULL)


def qs(*extra):
    """(rc, raw stdout, [parsed lines], stderr)"""
    r = dch("--xray", "xq", "--questions", "--json", *extra)
    try:
        ls = [json.loads(l) for l in r.stdout.splitlines()]
    except ValueError:
        ls = []
    return r.returncode, r.stdout, ls, r.stderr


def config(cmd, answer=None):
    with open(CONFIG, "w") as f:
        if cmd is not None:
            f.write("questions_command = %s\n" % cmd)
        if answer is not None:
            f.write("answer_command = %s\n" % answer)


def script(name, body, py=False):
    p = os.path.join(T, name)
    with open(p, "w") as f:
        f.write(("#!%s\n" % PY if py else "#!/bin/sh\n") + body)
    os.chmod(p, 0o755)
    return p


def ended():
    sock = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions", sock.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    with open(os.path.join(hdir, "xq.json"), "w") as f:
        json.dump({"name": "xq", "cwd": PROJ, "end": "exited",
                   "argv": ["claude"], "started": 1794299400,
                   "updated": 1794300000, "ended": 1794300000,
                   "sock_dir": sock,
                   "agent": {"session_id": SID, "cwd": PROJ,
                             "config_dir": os.path.join(HOME, ".claude")}},
                  f)


def main():
    ended()

    # --- no command: source none, no rows ---------------------------------
    config(None)
    rc, out, ls, err = qs()
    check("no questions_command: source none, no rows, exit 0",
          rc == 0 and ls == [NONE, DONE0], (rc, out, err))
    config(script("empty.sh", "exit 0\n"))
    rc, out, ls, err = qs()
    check("an empty answer: source none, no rows, no warn",
          rc == 0 and ls == [NONE, DONE0], (rc, out, err))

    # --- bare and wrapped rows -------------------------------------------
    config(script("rows.py", r'''
import json
print(json.dumps({"title": "demo questions"}))
print(json.dumps({"id": "OQ-1", "title": "Ship it Friday?",
                  "status": "pending", "label": "OPEN",
                  "note": "needs the operator", "created":
                  "2026-11-20T10:00:00Z", "updated": "2026-11-21T11:00:00Z",
                  "url": "https://example.invalid/oq/1"}))
print(json.dumps({"question": {"id": "OQ-2", "title": "Keep the flag?",
                               "status": "done", "label": "ANSWERED",
                               "active": "x", "group": "g",
                               "blocked_by": ["OQ-1"]}}))
print(json.dumps({"task": {"title": "a task wrapper is no question"}}))
print(json.dumps({"title": "Third", "status": "dropped", "id": 7}))
''', py=True))
    rc, out, ls, err = qs()
    check("rows: source command, the title line, the session's header",
          rc == 0 and ls and ls[0] == dict(NONE, source="command",
                                           title="demo questions"),
          (rc, out, err))
    check("...a bare row with every field",
          len(ls) > 1 and ls[1] == {"question": {
              "id": "OQ-1", "title": "Ship it Friday?", "status": "pending",
              "label": "OPEN", "note": "needs the operator",
              "created": "2026-11-20T10:00:00Z",
              "updated": "2026-11-21T11:00:00Z",
              "url": "https://example.invalid/oq/1"}}, out)
    check("...a wrapped row, task-only keys left out, nulls for the rest",
          len(ls) > 2 and ls[2] == {"question": {
              "id": "OQ-2", "title": "Keep the flag?", "status": "done",
              "label": "ANSWERED", "note": None, "created": None,
              "updated": None, "url": None}}, out)
    check("...a {\"task\":{...}} line has no title here: skipped; a "
          "numeric id, the status word as the label",
          len(ls) == 5 and ls[3] == {"question": {
              "id": "7", "title": "Third", "status": "dropped",
              "label": "dropped", "note": None, "created": None,
              "updated": None, "url": None}} and
          ls[4] == {"done": True, "count": 3, "dropped": 1}, out)

    # --- the environment, as tasks_command's ------------------------------
    config(script("envs.py", r'''
import json, os, sys
for k in ("DCH_SESSION", "DCH_SESSION_CWD", "DCH_AGENT",
          "DCH_AGENT_SESSION_ID", "DCH_ROWS_MAX"):
    print(json.dumps({"id": k, "title": "<%s>" % os.environ.get(k, "UNSET")}))
print(json.dumps({"id": "cwd", "title": "<%s>" % os.getcwd()}))
print(json.dumps({"id": "argv", "title": "<%s>" % " ".join(sys.argv[1:])}))
''', py=True) + " 'a b' c")
    rc, out, ls, err = qs()
    got = {l["question"]["id"]: l["question"]["title"]
           for l in ls if "question" in l}
    check("the command's env, cwd and argv are tasks_command's",
          rc == 0 and got == {"DCH_SESSION": "<xq>",
                              "DCH_SESSION_CWD": "<%s>" % PROJ,
                              "DCH_AGENT": "<claude>",
                              "DCH_AGENT_SESSION_ID": "<%s>" % SID,
                              "DCH_ROWS_MAX": "<50000>",
                              "cwd": "<%s>" % PROJ, "argv": "<a b c>"},
          (got, err))

    # --- a failure: the warn, source none ---------------------------------
    config(script("fail.sh", "echo 'ledger down' >&2\nexit 1\n"))
    rc, out, ls, err = qs()
    check("a failing command: source none, the header's warn, no rows",
          rc == 0 and ls == [dict(NONE, warn="questions_command: exit 1: "
                                  "ledger down"), DONE0], (rc, out, err))
    config(os.path.join(T, "no-such-program"))
    rc, out, ls, err = qs()
    check("a missing program: source none, a cannot-run warn",
          rc == 0 and len(ls) == 2 and ls[0]["source"] == "none" and
          ls[0].get("warn", "").startswith("questions_command: ") and
          "cannot run" in ls[0]["warn"] and ls[1] == DONE0, out)
    config(script("junk.sh", "echo not json\n"))
    rc, out, ls, err = qs()
    check("only lines that are no row: source none, the warn",
          rc == 0 and ls == [dict(NONE, warn="questions_command: no valid "
                                  "row (1 lines skipped)"), DONE0], out)
    config(script("exit3.sh", "echo '{\"id\":1,\"title\":\"a row\"}'\n"
                  "echo 'half done' >&2\nexit 3\n"))
    rc, out, ls, err = qs()
    check("rows then exit 3: the rows, done carries the warn",
          rc == 0 and len(ls) == 3 and ls[0]["source"] == "command" and
          ls[2] == {"done": True, "count": 1, "dropped": 0,
                    "warn": "questions_command: exit 3: half done"}, out)

    # --- caps: as --tasks ------------------------------------------------
    config(script("drop.py", r'''
import json
print("not json")
print("{" + " " * 70000 + "}")
for k in range(50001):
    print(json.dumps({"id": k, "title": "r%d" % k}))
''', py=True))
    rc, out, ls, err = qs()
    check("caps: an invalid line, a line over 64 KB and the row past the "
          "50000 cap (count 50000, dropped 3)",
          rc == 0 and ls and ls[0]["source"] == "command" and
          len(ls) == 50002 and
          ls[-1] == {"done": True, "count": 50000, "dropped": 3,
                     "warn": "questions_command: stopped early at 50000 "
                             "rows"},
          (rc, ls[-1:] if ls else out[-300:], err))
    config(script("long.sh", "printf '{\"id\":1,\"title\":\"%s\"}\\n' "
                  "\"$(printf '%070000d' 0)\"\n"
                  "echo '{\"id\":2,\"title\":\"kept\"}'\n"))
    rc, out, ls, err = qs()
    check("an oversize row line is skipped, the next row kept",
          rc == 0 and len(ls) == 3 and ls[1]["question"]["title"] == "kept"
          and ls[2] == {"done": True, "count": 1, "dropped": 1}, out)

    # --- #691: options, recommended, asker; the header's answer ----------
    config(script("new.py", r'''
import json
def q(**k):
    print(json.dumps(dict(title="t", **k)))
q(id="N1", options=["A", "B"], recommended="B", asker="sess-1.a_b")
q(id="N2", options=["o%d" % i for i in range(10)])
q(id="N3", options=["x" * 400, 5, "ok\nline\tx", None, ["z"]],
  recommended="r" * 400)
q(id="N4", options="notlist", recommended=7, asker=3)
q(id="N5", asker="-x")
q(id="N6", asker="a" * 70)
q(id="N7", asker="a/b")
q(id="N8", options=[5, ""], recommended="")
q(id="N9", asker="a" * 64)
q(id="N10", options=["a\u0085b\u202ec", "\u009bz"], recommended="r\u2066s")
''', py=True), answer="/bin/true --flag")
    rc, out, ls, err = qs()
    R = {l["question"]["id"]: l["question"] for l in ls if "question" in l}
    check("answer_command set: the header says \"answer\":true",
          rc == 0 and ls[0].get("answer") is True, (rc, out, err))
    check("...all three fields pass through",
          R.get("N1", {}).get("options") == ["A", "B"] and
          R["N1"].get("recommended") == "B" and
          R["N1"].get("asker") == "sess-1.a_b", R.get("N1"))
    check("options: at most 8, the rest dropped",
          R.get("N2", {}).get("options") == ["o%d" % i for i in range(8)],
          R.get("N2"))
    check("options: 400 bytes cut at 300, a non-string skipped, controls "
          "as blanks; recommended cut at 300",
          R.get("N3", {}).get("options") == ["x" * 300, "ok line x"] and
          R["N3"].get("recommended") == "r" * 300, R.get("N3"))
    check("XB7: C1 and bidi controls out of options and recommended",
          R.get("N10", {}).get("options") == ["abc", "z"] and
          R["N10"].get("recommended") == "rs", R.get("N10"))
    check("wrong types and empty strings: the keys are left out",
          all(k not in R.get(i, {"options": 1}) for i in ("N4", "N8")
              for k in ("options", "recommended", "asker")), (R.get("N4"),
                                                              R.get("N8")))
    check("a bad asker (-x, 70 bytes, a/b): left out; 64 bytes kept",
          all("asker" not in R.get(i, {"asker": 1}) for i in
              ("N5", "N6", "N7")) and R.get("N9", {}).get("asker") == "a" * 64,
          [R.get(i) for i in ("N5", "N6", "N7", "N9")])
    config(script("new.py", "exit 0\n"), answer="")
    rc, out, ls, err = qs()
    check("answer_command empty: no \"answer\" key", rc == 0 and
          all("answer" not in l for l in ls), (rc, out))
    config(script("one.sh", "echo '{\"id\":1,\"title\":\"a\"}'\n"))
    rc, out, ls, err = qs()
    check("answer_command unset: no \"answer\" key, no extra fields",
          rc == 0 and "answer" not in ls[0] and
          ls[1]["question"].keys() == {"id", "title", "status", "label",
                                       "note", "created", "updated", "url"},
          (rc, out))

    # --- the plain form and the flags it excludes -------------------------
    r = dch("--xray", "xq", "--questions")
    check("without --json: exit 2, as --tasks (no plain rendering)",
          r.returncode == 2 and r.stdout == "" and
          r.stderr == "dch: bad usage: --json is required (the --xray view "
          "takes no flag)\n", (r.returncode, r.stdout, r.stderr))
    t = dch("--xray", "xq", "--tasks")
    check("...the same answer --tasks gives",
          (t.returncode, t.stderr) == (r.returncode, r.stderr), t.stderr)
    for extra, name in ((["--tasks"], "--tasks"), (["--memory"], "--memory"),
                        (["--ref", "#1"], "--ref"),
                        (["--chapter", "a:0"], "--chapter"),
                        (["--follow"], "--follow (it carries --tasks, "
                                       "--memory and --schedules only)")):
        rc, out, ls, err = qs(*extra)
        check("--questions with %s: exit 2" % extra[0],
              rc == 2 and out == "" and
              err == "dch: bad usage: --questions and %s\n" % name,
              (rc, out, err))
    rc, out, ls, err = qs("--questions")
    check("--questions twice: exit 2",
          rc == 2 and err == "dch: bad usage: --questions twice\n", err)
    return 1 if fails[0] else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print("all ok" if not rc else "%d FAILED" % fails[0])
    sys.exit(rc)
