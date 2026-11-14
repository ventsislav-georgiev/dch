#!/usr/bin/env python3
"""`dch --xray <n> --tasks --json` with a tasks_command, and `--memory` with
a memory_command (#321): the user's command answers first, the built-in
source only when it gives no row.

An ended Claude session (a history record and a synthetic transcript with
one task) and a live shell session. Covered: rows -> source command with
the command's title, the header at the first row (before the command
ends), the done line; no row and exit 0 -> the built-in answer byte for
byte; exit 1 with no row -> the built-in rows and a header warn; a missing
program -> the same; rows then a hang -> the rows and a done warn inside
the 5 s limit; a shell session -> source command too; the command's
environment and cwd per session kind. --memory: rows -> source command, a
body cut at 16 KB; no row -> the built-in answer byte for byte; exit 1 ->
the built-in rows and a header warn; rows then a hang -> a done warn.
The done line's dropped (invalid, over 64 KB, past the row cap); a
session with no corpus: a failed command still says its warn.
Temp HOME, DCH_CONFIG, runtime dir.

Run:  python3 tests/xcmd_tasks_test.py        (uses ./dch; run.sh isolates)
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:900])


T = os.path.realpath(tempfile.mkdtemp(prefix="xct",
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
SID = "11111111-2222-4333-8444-5555555555d1"
PY = sys.executable


def dch(*a, timeout=30):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=timeout, env=ENV, stdin=subprocess.DEVNULL)


def tasks(n, what="--tasks"):
    """(rc, raw stdout, [parsed lines], stderr)"""
    r = dch("--xray", n, what, "--json")
    try:
        ls = [json.loads(l) for l in r.stdout.splitlines()]
    except ValueError:
        ls = []
    return r.returncode, r.stdout, ls, r.stderr


def config(cmd, key="tasks_command"):
    with open(CONFIG, "w") as f:
        if cmd is not None:
            f.write("%s = %s\n" % (key, cmd))


def script(name, body, py=False):
    p = os.path.join(T, name)
    with open(p, "w") as f:
        f.write(("#!%s\n" % PY if py else "#!/bin/sh\n") + body)
    os.chmod(p, 0o755)
    return p


def ended_claude():
    """xt-ended: a History Claude whose transcript made one task"""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-proj")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, SID + ".jsonl"), "w") as f:
        for o in ({"type": "user", "cwd": PROJ,
                   "timestamp": "2026-11-14T07:00:00Z",
                   "message": {"content": "plan it"}},
                  {"type": "assistant", "timestamp": "2026-11-14T07:00:01Z",
                   "message": {"content": [{
                       "type": "tool_use", "id": "toolu_1",
                       "name": "TaskCreate", "input": {
                           "subject": "Built-in task", "description": "",
                           "activeForm": "Building"}}]}},
                  {"type": "user", "timestamp": "2026-11-14T07:00:02Z",
                   "toolUseResult": {"task": {"id": "1"}},
                   "message": {"content": [{"type": "tool_result",
                                            "tool_use_id": "toolu_1",
                                            "content": "ok"}]}}):
            f.write(json.dumps(o) + "\n")
    mdir = os.path.join(pdir, "memory")	# the built-in --memory's one row
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, "note.md"), "w") as f:
        f.write("Built-in memory\n")
    sock = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions", sock.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    with open(os.path.join(hdir, "xt-ended.json"), "w") as f:
        json.dump({"name": "xt-ended", "cwd": PROJ, "end": "exited",
                   "argv": ["claude"], "started": 1794299400,
                   "updated": 1794300000, "ended": 1794300000,
                   "sock_dir": sock,
                   "agent": {"session_id": SID, "cwd": PROJ,
                             "config_dir": cfg}}, f)


# rows naming what the command saw: its env, cwd and argv
ENVS = script("envs.py", r'''
import json, os, sys
print(json.dumps({"title": "Env list"}))
for k in ("DCH_SESSION", "DCH_SESSION_CWD", "DCH_AGENT",
          "DCH_AGENT_SESSION_ID", "DCH_ROWS_MAX"):
    print(json.dumps({"id": k, "title": "<%s>" % os.environ.get(k, "UNSET"),
                      "status": "in_progress"}))
print(json.dumps({"id": "cwd", "title": "<%s>" % os.getcwd()}))
print(json.dumps({"id": "argv", "title": "<%s>" % " ".join(sys.argv[1:])}))
''', py=True)


def seen(ls):
    return {l["task"]["id"]: l["task"]["title"] for l in ls if "task" in l}


def memory():
    """--memory with a memory_command: the same rule as --tasks"""
    config(None)
    rc, base, bls, err = tasks("xt-ended", "--memory")
    ids = [l["memory"]["id"] for l in bls if "memory" in l]
    check("memory, no command: the built-in answer (source claude, note.md)",
          rc == 0 and bls and bls[0]["source"] == "claude" and "note" in ids
          and "warn" not in bls[0], (rc, base, err))
    config(script("mem.py", r'''
import json, os
print(json.dumps({"title": "My notes"}))
print(json.dumps({"memory": {"id": "a", "title": "Note A", "kind": "user",
                             "body": "x" * 20000}}))
print(json.dumps({"id": "env", "title": "<%s|%s|%s>" % (
    os.environ.get("DCH_SESSION"), os.environ.get("DCH_AGENT"),
    os.getcwd())}))
''', py=True), "memory_command")
    rc, out, ls, err = tasks("xt-ended", "--memory")
    m = [l["memory"] for l in ls if "memory" in l]
    check("memory rows: source command, the title, no session keys",
          rc == 0 and ls and ls[0] == {"schema": 1, "source": "command",
                                       "title": "My notes", "partial": False},
          (out[:400], err))
    check("memory rows: only the command's, the body cut at 16 KB, done",
          len(m) == 2 and "Built-in memory" not in out and
          m[0]["title"] == "Note A" and len(m[0]["body"]) == 16384 and
          m[0].get("cut") is True and
          m[1]["title"] == "<xt-ended|claude|%s>" % PROJ and
          ls[-1] == {"done": True, "count": 2, "dropped": 0},
          (out[-400:], err))
    config(script("mempty.sh", "exit 0\n"), "memory_command")
    rc, out, ls, err = tasks("xt-ended", "--memory")
    check("memory, an empty answer: the built-in answer byte for byte",
          rc == 0 and out == base, (out, base))
    config(script("mfail.sh", "echo 'notes down' >&2\nexit 1\n"),
           "memory_command")
    rc, out, ls, err = tasks("xt-ended", "--memory")
    check("memory, exit 1 with no row: the built-in rows, the header's warn",
          rc == 0 and ls and
          ls[0].get("warn") == "memory_command: exit 1: notes down" and
          {k: v for k, v in ls[0].items() if k != "warn"} == bls[0] and
          ls[1:] == bls[1:], (out, err))
    config(script("mhang.sh", "echo '{\"id\":\"h\",\"title\":\"before the hang\"}'\n"
                  "exec sleep 30\n"), "memory_command")
    t0 = time.monotonic()
    rc, out, ls, err = tasks("xt-ended", "--memory")
    wall = time.monotonic() - t0
    check("memory rows then a hang: the rows, done carries the warn (%.1f s)"
          % wall,
          rc == 0 and len(ls) == 3 and ls[0]["source"] == "command" and
          ls[1]["memory"]["title"] == "before the hang" and
          ls[2] == {"done": True, "count": 1, "dropped": 0,
                    "warn": "memory_command: stopped early after 5 s"} and
          wall < 7, (out, err))
    config("%s" % ENVS, "memory_command")
    rc, out, ls, err = tasks("xt-ended")
    check("a memory_command does not answer --tasks",
          rc == 0 and ls and ls[0]["source"] == "claude", out[:300])


def main():
    ended_claude()
    config(None)
    rc, base, bls, err = tasks("xt-ended")
    check("no tasks_command: the built-in answer (source claude, 1 task)",
          rc == 0 and len(bls) == 3 and bls[0]["source"] == "claude" and
          bls[1]["task"]["title"] == "Built-in task" and
          bls[2] == {"done": True, "count": 1, "dropped": 0}, (rc, base, err))

    # --- rows: the command is the source ----------------------------------
    config("%s 'one arg' two" % ENVS)
    rc, out, ls, err = tasks("xt-ended")
    got = seen(ls)
    check("rows: source command, the command's title, the header's keys",
          rc == 0 and ls and list(ls[0]) == ["schema", "session",
                                             "session_id", "source", "title",
                                             "partial"] and
          ls[0]["source"] == "command" and ls[0]["title"] == "Env list" and
          ls[0]["session"] == "xt-ended" and ls[0]["session_id"] == SID and
          ls[0]["partial"] is False, (rc, out[:600], err))
    check("rows: only the command's rows, canonical, then done, no warn",
          len(ls) == 9 and "Built-in task" not in out and
          ls[1]["task"]["status"] == "active" and
          ls[1]["task"]["label"] == "in_progress" and
          ls[-1] == {"done": True, "count": 7, "dropped": 0}, out[-600:])
    check("rows: an ended Claude's environment (session, agent, its id)",
          got.get("DCH_SESSION") == "<xt-ended>" and
          got.get("DCH_AGENT") == "<claude>" and
          got.get("DCH_AGENT_SESSION_ID") == "<%s>" % SID and
          got.get("DCH_ROWS_MAX") == "<50000>", got)
    check("rows: cwd is the session's (the agent's cwd, a space in it)",
          got.get("DCH_SESSION_CWD") == "<%s>" % PROJ and
          got.get("cwd") == "<%s>" % PROJ, got)
    check("rows: argv as the config line splits it, no shell",
          got.get("argv") == "<one arg two>", got)

    # --- the header comes at the first row, before the command ends ------
    slow = script("slow.sh", "echo '{\"title\":\"Slow\"}'\n"
                  "echo '{\"id\":1,\"title\":\"first\"}'\nsleep 2\n"
                  "echo '{\"id\":2,\"title\":\"second\"}'\n")
    config(slow)
    t0 = time.monotonic()
    p = subprocess.Popen([DCH, "--xray", "xt-ended", "--tasks", "--json"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=ENV, stdin=subprocess.DEVNULL)
    h, r1 = p.stdout.readline(), p.stdout.readline()
    t1 = time.monotonic() - t0
    rest = p.stdout.read()
    p.wait(15)
    check("streaming: the header and the first row arrive while the "
          "command still runs (%.2f s)" % t1,
          t1 < 1.8 and b'"source":"command"' in h and b'"first"' in r1 and
          b'"second"' in rest and
          rest.endswith(b'{"done":true,"count":2,"dropped":0}\n'),
          (t1, h, r1, rest))
    p.stdout.close()
    p.stderr.close()

    # --- no row: the built-in source, exactly ------------------------------
    config(script("empty.sh", "exit 0\n"))
    rc, out, ls, err = tasks("xt-ended")
    check("an empty answer, exit 0: the built-in answer byte for byte",
          rc == 0 and out == base, (out, base))
    config(script("fail.sh", "echo 'no ledger here' >&2\nexit 1\n"))
    rc, out, ls, err = tasks("xt-ended")
    check("exit 1, no row: the built-in rows, the header's warn",
          rc == 0 and ls and ls[0].get("warn") ==
          "tasks_command: exit 1: no ledger here" and
          ls[0]["source"] == "claude" and
          {k: v for k, v in ls[0].items() if k != "warn"} == bls[0] and
          ls[1:] == bls[1:], (out, err))
    config(os.path.join(T, "no-such-program"))
    rc, out, ls, err = tasks("xt-ended")
    check("a missing program: the built-in rows, the header's warn",
          rc == 0 and ls and "cannot run" in ls[0].get("warn", "") and
          ls[1:] == bls[1:], out)
    config(script("junk.sh", "echo not json\n"))
    rc, out, ls, err = tasks("xt-ended")
    check("only lines that are no row: the built-in rows, a warn",
          rc == 0 and ls and ls[0].get("warn") ==
          "tasks_command: no valid row (1 lines skipped)" and
          ls[1:] == bls[1:], out)

    # --- rows, then the clock ----------------------------------------------
    config(script("hang.sh", "echo '{\"id\":1,\"title\":\"before the hang\"}'"
                  "\nexec sleep 30\n"))
    t0 = time.monotonic()
    rc, out, ls, err = tasks("xt-ended")
    wall = time.monotonic() - t0
    check("rows then a hang: the rows stay, done carries the warn (%.1f s)"
          % wall,
          rc == 0 and len(ls) == 3 and ls[0]["source"] == "command" and
          "warn" not in ls[0] and ls[1]["task"]["title"] == "before the hang"
          and ls[2] == {"done": True, "count": 1, "dropped": 0,
                        "warn": "tasks_command: stopped early after 5 s"} and
          wall < 7, (out, err))
    config(script("exit3.sh", "echo '{\"id\":1,\"title\":\"a row\"}'\n"
                  "echo 'half done' >&2\nexit 3\n"))
    rc, out, ls, err = tasks("xt-ended")
    check("rows then exit 3: the rows, done carries the warn",
          rc == 0 and len(ls) == 3 and ls[0]["source"] == "command" and
          ls[2].get("warn") == "tasks_command: exit 3: half done", out)

    # --- dropped: lines left out (#321 fix round) ------------------------
    config(script("drop.py", r'''
import json
print("not json")
print("{" + " " * 70000 + "}")
for k in range(50001):
    print(json.dumps({"id": k, "title": "r%d" % k}))
''', py=True))
    rc, out, ls, err = tasks("xt-ended")
    check("dropped: an invalid line, a line over 64 KB and the one row "
          "past the 50000 cap (count 50000, dropped 3)",
          rc == 0 and ls and ls[0]["source"] == "command" and
          len(ls) == 50002 and
          ls[-1] == {"done": True, "count": 50000, "dropped": 3,
                     "warn": "tasks_command: stopped early at 50000 rows"},
          (rc, ls[-1:] if ls else out[-300:], err))
    config(script("drop2.sh", "echo junk\necho '{\"id\":1,\"title\":\"a\"}'\n"))
    rc, out, ls, err = tasks("xt-ended")
    check("dropped: an invalid line beside a row, no warn",
          rc == 0 and ls and
          ls[-1] == {"done": True, "count": 1, "dropped": 1}, out)

    # --- no corpus: a failed command is still said ------------------------
    sock = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions", sock.replace("/", "-"))
    with open(os.path.join(hdir, "xt-gone.json"), "w") as f:
        json.dump({"name": "xt-gone", "cwd": PROJ, "end": "exited",
                   "argv": ["claude"], "started": 1794299400,
                   "updated": 1794300000, "ended": 1794300000,
                   "sock_dir": sock,
                   "agent": {"session_id": SID[:-2] + "ff", "cwd": PROJ,
                             "config_dir": os.path.join(HOME, ".claude")}},
                  f)
    config(None)
    r0 = dch("--xray", "xt-gone", "--tasks", "--json")
    check("no corpus, no command: exit 1, the stderr message, no stdout",
          r0.returncode == 1 and r0.stdout == "" and
          r0.stderr == "dch: no x-ray corpus\n", (r0.returncode, r0.stdout,
                                                  r0.stderr))
    config(script("empty3.sh", "exit 0\n"))
    r = dch("--xray", "xt-gone", "--tasks", "--json")
    check("no corpus, an empty answer: the same, byte for byte",
          (r.returncode, r.stdout, r.stderr) ==
          (r0.returncode, r0.stdout, r0.stderr), (r.returncode, r.stdout,
                                                  r.stderr))
    config(script("fail3.sh", "echo 'ledger gone' >&2\nexit 1\n"))
    rc, out, ls, err = tasks("xt-gone")
    check("no corpus, the command failed: source none, the header's warn, "
          "done, exit 0",
          rc == 0 and len(ls) == 2 and ls[0]["source"] == "none" and
          ls[0]["session"] == "xt-gone" and
          ls[0].get("warn") == "tasks_command: exit 1: ledger gone" and
          ls[1] == {"done": True, "count": 0, "dropped": 0}, (rc, out, err))

    memory()

    # --- a live shell session ---------------------------------------------
    shcwd = os.path.join(T, "shell cwd")
    os.makedirs(shcwd)
    config(None)
    r = dch("--spawn", "xt-sh", "--cwd", shcwd, "sh", "-c", "sleep 120")
    check("shell: spawned", r.returncode == 0, r.stderr)
    try:
        rc, sbase, sls, err = tasks("xt-sh")
        check("shell, no command: source none, no task",
              rc == 0 and len(sls) == 2 and sls[0]["source"] == "none" and
              sls[1] == {"done": True, "count": 0, "dropped": 0},
              (rc, sbase, err))
        config(ENVS)
        rc, out, ls, err = tasks("xt-sh")
        got = seen(ls)
        check("shell with a command: source command, its rows",
              rc == 0 and ls and ls[0]["source"] == "command" and
              ls[0]["session_id"] == sls[0]["session_id"] and
              ls[-1]["count"] == 7, (out[:500], err))
        check("shell: no agent, the session's cwd",
              got.get("DCH_AGENT") == "<>" and
              got.get("DCH_AGENT_SESSION_ID") == "<>" and
              got.get("DCH_SESSION") == "<xt-sh>" and
              got.get("DCH_SESSION_CWD") == "<%s>" % shcwd and
              got.get("cwd") == "<%s>" % shcwd, got)
        config(script("empty2.sh", "exit 0\n"))
        rc, out, ls, err = tasks("xt-sh")
        check("shell, an empty answer: source none, byte for byte",
              rc == 0 and out == sbase, (out, sbase))
    finally:
        dch("-k", "xt-sh")
    return 1 if fails[0] else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print("all ok" if not rc else "%d FAILED" % fails[0])
    sys.exit(rc)
