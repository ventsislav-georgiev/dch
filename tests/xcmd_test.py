"""X-ray's command source runner (#321), xcmd.c, against fake commands.

tests/xcmd_unit.c is built here: its units run, then its driver mode
(`xcmd_unit run task|memory CMD CWD SESSION AGENT SID`): rows on stdout as
they arrive, one JSON result line on stderr. The fake commands are sh and
python scripts under a temp dir. Covered: bare and wrapped rows, the title
line, exits with and without rows, an empty answer, a missing program, the
5 s clock with and without rows, an endless stream, a 100 MB line, 2000 /
20000 / 60000 rows with timings and peak memory, a child that ignores
SIGTERM, grandchildren left in the group, the driver itself ended by
SIGTERM, env and cwd, no shell expansion of the config line, EINTR.

Run:  python3 tests/xcmd_test.py        (CC from ./Makefile, else cc)
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
fails = 0


def check(what, ok, detail=""):
    global fails
    if ok:
        print("  ok   " + what)
    else:
        fails += 1
        print("  FAIL %s %s" % (what, detail))


def compiler():
    cc = os.environ.get("XCC")
    if not cc and os.path.exists("Makefile"):
        with open("Makefile") as f:
            m = re.search(r"^CC = (.*)$", f.read(), re.M)
            cc = m and m.group(1).strip()
    return (cc or "cc").split()


T = os.path.realpath(tempfile.mkdtemp(prefix="dchxc",
                                      dir=os.environ.get("DCH_TEST_TMP")))
HOME = os.path.join(T, "home")
os.makedirs(os.path.join(HOME, "bin"))
DRV = os.path.join(T, "xcmd_unit")
PY = sys.executable


def script(name, body, py=False):
    p = os.path.join(T, name)
    with open(p, "w") as f:
        f.write(("#!%s\n" % PY if py else "#!/bin/sh\n") + body)
    os.chmod(p, 0o755)
    return p


def env(**extra):
    e = {k: v for k, v in os.environ.items()
         if k not in ("MallocStackLogging", "MallocStackLoggingNoCompact")}
    e.update(HOME=HOME, DCH_SESSION="outer-session")
    e.update(extra)
    return e


def run(cmd, kind="task", cwd=None, session="s1", agent="", sid="",
        extra=None, pass_fds=()):
    t = time.monotonic()
    r = subprocess.run([DRV, "run", kind, cmd, cwd or T, session, agent, sid],
                       capture_output=True, text=True, env=env(**(extra or {})),
                       timeout=60, pass_fds=pass_fds)
    wall = time.monotonic() - t
    res = json.loads(r.stderr.strip().splitlines()[-1])
    rows = [json.loads(l) for l in r.stdout.splitlines()]
    return rows, res, wall


def gone(pid, within=3.0):
    """pid and its process group no longer exist (init reaps orphans)"""
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        time.sleep(0.05)
    return False


def readpid(name):
    with open(os.path.join(T, name)) as f:
        return int(f.read().split()[0])


def row(i, extra=""):
    return '{"id":%d,"title":"Task %d","status":"pending"%s}' % (i, i, extra)


# ~480 bytes a row, as the largest real ledger lists
GEN = script("gen.py", r'''
import sys
n = int(sys.argv[1])
pad = "x" * 380
w = sys.stdout.write
for i in range(n):
    w('{"id":%d,"title":"Task %d","status":"pending","note":"%s"}\n'
      % (i, i, pad))
''', py=True)


def main():
    r = subprocess.run(compiler() + ["-O1", "-I" + SRC, "-o", DRV,
                                     os.path.join(HERE, "xcmd_unit.c"),
                                     os.path.join(SRC, "xcmd.c"),
                                     os.path.join(SRC, "xray.c"),
                                     os.path.join(SRC, "json.c")],
                       capture_output=True, text=True)
    check("the driver builds", r.returncode == 0, r.stderr[-2000:])
    if r.returncode:
        return 1
    r = subprocess.run([DRV], capture_output=True, text=True, env=env())
    check("the units: the argv split and the line validator (xcmd_unit)",
          r.returncode == 0 and r.stdout.endswith("all ok\n"),
          "\n".join(l for l in r.stdout.splitlines()
                    if not l.startswith("  ok")))

    # --- the happy path: title, bare and wrapped, garbage counted ----------
    p = script("happy.sh", r'''
echo '{"title":"My list"}'
echo '%s'
echo ''
echo 'not json'
echo '{"task":{"id":"b","title":"Wrapped","status":"completed","x":1}}'
printf '%%s' '{"id":3,"title":"No newline at the end"}'
''' % row(1))
    rows, res, _ = run(p)
    check("happy: rows, title, one skipped line, exit 0, no warn",
          res["ret"] == 3 and res["rows"] == 3 and res["skipped"] == 1 and
          res["title"] == "My list" and res["code"] == 0 and
          res["warn"] == "" and not res["stopped"] and res["ran"] == 1, res)
    check("happy: the canonical rows, in order",
          [x["task"]["id"] for x in rows] == ["1", "b", "3"] and
          rows[1]["task"]["status"] == "done" and
          rows[1]["task"]["label"] == "completed" and
          "x" not in rows[1]["task"] and rows[0]["task"]["chapter"] is None,
          rows)
    check("happy: no child left unreaped", res["children"] == 0, res)

    rows, res, _ = run(script("mem.sh", r'''
echo '{"memory":{"id":"m1","title":"Memory one","kind":"feedback","body":"b"}}'
echo '{"id":"m2","title":"Memory two"}'
echo '{"task":{"title":"a task, not a memory"}}'
'''), kind="memory")
    check("memory kind: wrapped and bare rows, a task wrapper skipped",
          res["rows"] == 2 and res["skipped"] == 1 and
          rows[0]["memory"]["kind"] == "feedback" and
          rows[1]["memory"]["id"] == "m2", (res, rows))

    # --- env, cwd, stdin, group, fds ----------------------------------------
    sub = os.path.join(T, "cwd dir")
    os.makedirs(sub)
    p = script("env.py", r'''
import json, os, sys
def out(k, v):
    print(json.dumps({"id": k, "title": "<%s>" % v}))
for k in ("DCH_SESSION", "DCH_SESSION_CWD", "DCH_AGENT",
          "DCH_AGENT_SESSION_ID", "DCH_ROWS_MAX"):
    out(k, os.environ.get(k, "UNSET"))
out("cwd", os.getcwd())
out("stdin", os.read(0, 10) == b"" and
    os.fstat(0).st_rdev == os.stat("/dev/null").st_rdev)
out("group", os.getpgid(0) == os.getpid())
out("fds", sorted(int(f) for f in os.listdir("/dev/fd") if int(f) > 2
                  and int(f) != 3))
for i, a in enumerate(sys.argv[1:]):
    out("arg%d" % i, a)
''', py=True)
    fd = os.open(os.path.join(T, "happy.sh"), os.O_RDONLY)
    keep = os.dup2(fd, 50)
    os.close(fd)
    rows, res, _ = run(p, cwd=sub, session="sess-a", agent="claude",
                       sid="0f0e-sid", pass_fds=(keep,))
    got = {x["task"]["id"]: x["task"]["title"] for x in rows}
    check("env: DCH_SESSION (over the outer one), cwd, agent, id, rows max",
          got.get("DCH_SESSION") == "<sess-a>" and
          got.get("DCH_SESSION_CWD") == "<%s>" % sub and
          got.get("DCH_AGENT") == "<claude>" and
          got.get("DCH_AGENT_SESSION_ID") == "<0f0e-sid>" and
          got.get("DCH_ROWS_MAX") == "<50000>", got)
    check("env: cwd is the session's cwd (a space in it)",
          got.get("cwd") == "<%s>" % sub, got)
    check("env: stdin is /dev/null, own process group",
          got.get("stdin") == "<True>" and got.get("group") == "<True>", got)
    check("env: no fd of the caller's reaches the command (fd %d)" % keep,
          got.get("fds") == "<[]>", got)
    os.close(keep)
    rows, res, _ = run(p, agent="")
    got = {x["task"]["id"]: x["task"]["title"] for x in rows}
    check("env: no agent is an empty DCH_AGENT and id",
          got.get("DCH_AGENT") == "<>" and
          got.get("DCH_AGENT_SESSION_ID") == "<>", got)

    # --- no shell, ever ----------------------------------------------------
    shutil.copy(p, os.path.join(HOME, "bin", "envt"))
    rows, res, _ = run("~/bin/envt $HOME ; `touch %s/pwned` * 'a b' "
                       "\"c 'd\" && echo >%s/pwned2 $(id)" % (T, T))
    got = {x["task"]["id"]: x["task"]["title"] for x in rows}
    args = [got[k] for k in sorted((k for k in got if k.startswith("arg")),
                                   key=lambda k: int(k[3:]))]
    check("no shell: ~/ is HOME, $HOME ; backticks * && > $() stay words",
          args == ["<%s>" % a for a in
                   ["$HOME", ";", "`touch", "%s/pwned`" % T, "*", "a b",
                    "c 'd", "&&", "echo", ">%s/pwned2" % T, "$(id)"]] and
          not os.path.exists(os.path.join(T, "pwned")) and
          not os.path.exists(os.path.join(T, "pwned2")), got)
    rows, res, _ = run("echo '{\"title\":\"x\"}' | cat")
    check("no shell: a pipe is echo's argument, not a pipe",
          res["rows"] == 0 and res["skipped"] == 1 and res["warn"] ==
          "tasks_command: no valid row (1 lines skipped)", res)
    rows, res, _ = run("'a b")
    check("a config line with an open quote: no run, a warn",
          res["ran"] == 0 and res["ret"] == 0 and
          res["warn"].startswith("tasks_command: not a command line"), res)
    rows, res, _ = run("")
    check("no command configured: no run, no warn",
          res["ran"] == 0 and res["ret"] == 0 and res["warn"] == "", res)

    # --- failures and the fallback signal ----------------------------------
    rows, res, _ = run(script("fail1.sh", "echo '%s'\necho 'it broke' >&2\n"
                              "echo second >&2\nexit 3\n" % row(1)))
    check("non-zero exit with a row: the row stays, warn with stderr's line",
          res["ret"] == 1 and res["code"] == 3 and
          res["err"] == "it broke" and
          res["warn"] == "tasks_command: exit 3: it broke", res)
    rows, res, _ = run(script("fail0.sh", "echo 'no db' >&2\nexit 1\n"))
    check("non-zero exit, no row: 0 (built-in source), warn",
          res["ret"] == 0 and res["warn"] == "tasks_command: exit 1: no db",
          res)
    rows, res, _ = run(script("empty.sh", "exit 0\n"))
    check("an empty answer: 0, silent",
          res["ret"] == 0 and res["warn"] == "" and res["code"] == 0, res)
    rows, res, _ = run(script("kill9.sh", "echo '%s'\nkill -9 $$\n" % row(1)))
    check("ended by a signal: the row stays, warn names it",
          res["rows"] == 1 and res["code"] == -1 and
          res["warn"] == "tasks_command: ended by signal 9", res)
    for cmd in ("/nonexistent/prog", "no-such-command-xc321"):
        rows, res, _ = run(cmd)
        check("a missing program (%s): not run, warn, nothing left" % cmd,
              res["ran"] == 0 and res["ret"] == 0 and res["children"] == 0
              and "cannot run" in res["warn"] and
              "No such file" in res["warn"], res)
    rows, res, _ = run(p, cwd=os.path.join(T, "no-such-dir"))
    check("a session cwd that is gone: not run, warn",
          res["ran"] == 0 and "cannot run" in res["warn"], res)
    rows, res, _ = run(script("errflood.py", r'''
import os, sys
os.write(2, b"first error line\n")
for i in range(50):
    os.write(2, b"e" * (1 << 20))
print('{"title":"after the flood"}', flush=True)
print('{"title":"second"}')
''', py=True))
    check("50 MB on stderr is drained, never blocks the command",
          res["rows"] == 1 and res["title"] == "after the flood" and
          res["code"] == 0 and res["err"] == "first error line", res)

    # --- the clock -----------------------------------------------------------
    p = script("sleep0.sh", "echo $$ > %s/sleep0.pid\nexec sleep 30\n" % T)
    rows, res, wall = run(p)
    check("a sleeper, no row: stopped early inside 5 s + grace, 0, warn",
          res["ret"] == 0 and res["stopped"] == 1 and
          res["warn"] == "tasks_command: stopped early after 5 s" and
          4.9 <= wall < 6.5, (res, wall))
    check("a sleeper: its group is gone", gone(readpid("sleep0.pid")))
    p = script("sleep2.sh", "echo '%s'\necho '%s'\necho $$ > %s/sleep2.pid\n"
               "exec sleep 30\n" % (row(1), row(2), T))
    rows, res, wall = run(p)
    check("a sleeper after 2 rows: the rows stay, stopped early",
          res["ret"] == 2 and len(rows) == 2 and res["stopped"] == 1 and
          res["warn"] == "tasks_command: stopped early after 5 s" and
          res["first_ms"] < 2000 and wall < 6.5, (res, wall))
    check("a sleeper after 2 rows: its group is gone",
          gone(readpid("sleep2.pid")))
    p = script("noterm.sh", "trap '' TERM\necho $$ > %s/noterm.pid\n"
               "echo '%s'\nwhile :; do sleep 1; done\n" % (T, row(1)))
    rows, res, wall = run(p)
    check("a child ignoring SIGTERM: SIGKILL after the grace",
          res["rows"] == 1 and res["stopped"] == 1 and wall < 6.5 and
          res["children"] == 0, (res, wall))
    check("a child ignoring SIGTERM: gone", gone(readpid("noterm.pid")))
    p = script("closed.sh", "echo '%s'\nexec >&- 2>&-\necho $$ > %s/closed.pid"
               "\nexec sleep 30\n" % (row(1), T))
    rows, res, wall = run(p)
    check("output closed, still running: ended at the clock, the row stays",
          res["rows"] == 1 and res["stopped"] == 1 and 4.9 <= wall < 6.5,
          (res, wall))
    check("output closed, still running: gone", gone(readpid("closed.pid")))

    # --- the process group -------------------------------------------------
    p = script("gchild.sh", "sleep 300 >/dev/null 2>&1 &\necho $! > %s/g.pid\n"
               "echo $$ > %s/gl.pid\necho '%s'\nexit 0\n" % (T, T, row(1)))
    rows, res, wall = run(p)
    check("a grandchild left detached: the answer is prompt and clean",
          res["rows"] == 1 and res["code"] == 0 and res["warn"] == "" and
          wall < 2, (res, wall))
    check("a grandchild left detached: ended with the group",
          gone(readpid("g.pid")) and gone(readpid("gl.pid")))
    p = script("gchild2.sh", "sleep 300 &\necho $! > %s/g2.pid\necho '%s'\n"
               "exit 0\n" % (T, row(1)))
    rows, res, wall = run(p)
    check("a grandchild holding stdout: read to the clock, the row stays",
          res["rows"] == 1 and res["stopped"] == 1 and wall < 6.5,
          (res, wall))
    check("a grandchild holding stdout: ended", gone(readpid("g2.pid")))
    p = script("long.sh", "echo $$ > %s/long.pid\necho '%s'\nexec sleep 300\n"
               % (T, row(1)))
    d = subprocess.Popen([DRV, "run", "task", p, T, "s", "", ""],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env())
    d.stdout.readline()
    d.send_signal(signal.SIGTERM)
    d.wait(10)
    check("the querying process ended by SIGTERM: it dies of it",
          d.returncode == -signal.SIGTERM, d.returncode)
    check("the querying process ended by SIGTERM: the command's group too",
          gone(readpid("long.pid")))
    d.stdout.close()
    d.stderr.close()

    # --- caps ----------------------------------------------------------------
    yes = shutil.which("yes") or "/usr/bin/yes"
    rows, res, wall = run("%s '%s'" % (yes, row(1)), extra={"XCMD_QUIET": "1"})
    check("an endless row stream: the first 50000, stopped early, bounded",
          res["ret"] == 50000 and res["seen"] == 50000 and
          res["stopped"] == 1 and
          res["warn"] == "tasks_command: stopped early at 50000 rows" and
          res["maxrss_kb"] < 16384 and wall < 5, (res, wall))
    rows, res, wall = run("%s nope" % yes, extra={"XCMD_QUIET": "1"})
    check("an endless garbage stream: stopped at 32 MB, 0, bounded",
          res["ret"] == 0 and res["stopped"] == 1 and
          res["warn"] == "tasks_command: stopped early at 32 MB of output" and
          res["skipped"] > 1000000 and res["maxrss_kb"] < 16384 and wall < 6,
          (res, wall))
    big = script("big.py", r'''
import os, sys
n = int(sys.argv[1]) << 20
b = b"a" * (1 << 20)
while n > 0:
    os.write(1, b[:min(n, len(b))])
    n -= len(b)
os.write(1, b'\n{"title":"after the long line"}\n')
''', py=True)
    rows, res, wall = run(big + " 1")
    check("a 1 MB line: skipped and counted, the next row read",
          res["rows"] == 1 and res["skipped"] == 1 and
          rows[0]["task"]["title"] == "after the long line" and
          res["maxrss_kb"] < 16384, res)
    rows, res, wall = run(big + " 100")
    check("a 100 MB line: stopped at 32 MB, memory bounded, nothing left",
          res["ret"] == 0 and res["stopped"] == 1 and
          "32 MB" in res["warn"] and res["maxrss_kb"] < 16384 and
          res["children"] == 0 and wall < 6, (res, wall))
    rows, res, wall = run("%s '%s'" % (yes, row(1)),
                          extra={"XCMD_STOP_AFTER": "3"})
    check("the callback ends the run: 3 rows, not stopped early, no warn",
          res["ret"] == 3 and len(rows) == 3 and res["stopped"] == 0 and
          res["warn"] == "" and wall < 2, (res, wall))

    # --- sizes: one measurement per target ---------------------------------
    timing = []
    for n, want in ((2000, 2000), (20000, 20000), (60000, 50000)):
        rows, res, wall = run("%s %s %d" % (PY, GEN, n),
                              extra={"XCMD_QUIET": "1"})
        timing.append("%d rows: first %d ms, all %d ms, peak %d KB"
                      % (n, res["first_ms"], res["total_ms"],
                         res["maxrss_kb"]))
        check("%d rows: %d kept%s" % (n, want, ", stopped early at the cap"
                                      if n > want else ""),
              res["ret"] == want and res["seen"] == want and
              res["stopped"] == (n > want) and res["maxrss_kb"] < 16384 and
              res["total_ms"] < (1000 if n <= 2000 else 3000 if n <= 20000
                                 else 5000), res)
    for t in timing:
        print("       " + t)
    rows, res, wall = run("%s %s 20000" % (PY, GEN),
                          extra={"XCMD_QUIET": "1", "XCMD_EINTR": "1"})
    check("EINTR every 1 ms: 20000 rows, none lost",
          res["ret"] == 20000 and res["code"] == 0 and res["warn"] == "", res)
    rows, res, wall = run(script("happy2.sh", "echo '%s'\nsleep 1\necho '%s'\n"
                                 % (row(1), row(2))),
                          extra={"XCMD_EINTR": "1"})
    check("EINTR every 1 ms across a pause: both rows, exit 0",
          res["ret"] == 2 and res["code"] == 0 and res["warn"] == "", res)
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    sys.exit(rc)
