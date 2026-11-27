"""--history keeps a Codex thread id; --revive resumes it (#655).

A session whose command is an interactive Codex records the thread it runs
("codex":{"thread_id","home"}): seen live (its shell snapshot, Codex 0.167
D3, while it runs) or read from the `codex resume <id>` line Codex prints
on exit (B). --history shows it ([codex <id>] in text, the object in
--json). --revive relaunches `codex resume <id> -C <cwd>` with the resume
message as the PROMPT positional, the original resume/--last/fork, -C and
prompt gone, --no-daemon kept, under the recorded CODEX_HOME. With no id
it revives the argv as before.

A native fake `codex` (built here) stands in: it logs its argv and
CODEX_HOME, writes a shell snapshot for FAKE_CODEX_TID when FAKE_CODEX_SNAP
is set, and on SIGUSR1 prints Codex's exit summary and exits 0. No real
Codex runs; HOME, the runtime and state dirs are temporary.

Run:  python3 tests/codex_resume_test.py        (uses ./dch)
"""
import json, os, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
T1 = "019a5555-1111-7222-8333-444455556601"
T2 = "019a5555-1111-7222-8333-444455556602"
SEP = "\x1f"
FAKE_C = r'''
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static volatile sig_atomic_t quit;
static void on(int s) { (void)s; quit = 1; }
int main(int argc, char **argv) {
  const char *log = getenv("FAKE_CODEX_LOG"), *tid = getenv("FAKE_CODEX_TID");
  const char *ch = getenv("CODEX_HOME"), *s = getenv("DCH_SESSION");
  const char *m = getenv("DCH_NATIVE_BRIDGE_ID");
  char snap[2048] = "";
  FILE *f;
  int k;
  if (argc > 1 && !strcmp(argv[1], "--help")) {
    puts("Usage: codex [OPTIONS]\n      --no-daemon  Run in-process");
    return 0;
  }
  signal(SIGUSR1, on);
  if (tid && getenv("FAKE_CODEX_SNAP") && ch && s) {
    snprintf(snap, sizeof snap, "%s/shell_snapshots/%s.1.sh", ch, tid);
    if ((f = fopen(snap, "w"))) {
      /* the shell env it snapshots: dch's session and bridge marker */
      fprintf(f, "# Snapshot file\nexport DCH_SESSION=%s\n", s);
      if (m && *m)
        fprintf(f, "export DCH_NATIVE_BRIDGE_ID=%s\n", m);
      fclose(f);
    }
  }
  if (log && (f = fopen(log, "a"))) {
    fprintf(f, "%ld\x1f%s", (long)getpid(), ch ? ch : "-");
    for (k = 0; k < argc; k++)
      fprintf(f, "\x1f%s", argv[k]);
    fputc('\n', f);
    fclose(f);
  }
  while (!quit)
    pause();
  if (snap[0])
    unlink(snap);  /* Codex drops its snapshot when the session ends */
  if (tid)
    printf("Token usage: total=12 input=10 output=2\n"
           "To continue this session, run:\n  \033[36mcodex resume %s\033[39m\n",
           tid);
  fflush(stdout);
  return 0;
}
'''
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:1500])


def until(fn, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchcx"))
    home, bindir = os.path.join(tmp, "home"), os.path.join(tmp, "bin")
    proj, cxhome = os.path.join(tmp, "proj"), os.path.join(tmp, "cxhome")
    state = os.path.join(tmp, "state")
    for d in (home, bindir, proj, os.path.join(cxhome, "shell_snapshots"),
              os.path.join(home, ".codex", "shell_snapshots")):
        os.makedirs(d)
    fake = os.path.join(bindir, "codex")
    src = os.path.join(tmp, "fake_codex.c")
    with open(src, "w") as f:
        f.write(FAKE_C)
    b = subprocess.run(["cc", "-o", fake, src], capture_output=True, text=True)
    if b.returncode:
        print("FAIL: fake codex does not build", b.stderr)
        return 1
    log = os.path.join(tmp, "codex.log")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DCH_", "CLAUDE", "CODEX", "Malloc"))}
    env.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
               DCH_CONFIG=os.path.join(tmp, "config"), DCH_LOGIN_ENV="0",
               DCH_NO_REMOTE="1", DCH_RECORD_MS="300", FAKE_CODEX_LOG=log,
               PATH=bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin"))
    recdir = os.path.join(state, "dch", "sessions",
                          os.path.join(tmp, "dch-%d" % os.getuid())
                          .replace("/", "-"))
    names, pids = [], []

    def dch(*a, extra=None):
        return subprocess.run([DCH] + list(a), capture_output=True,
                              text=True, timeout=60, cwd=proj,
                              env=dict(env, **(extra or {})))

    def launches():
        try:
            lines = open(log, errors="replace").read().splitlines()
        except OSError:
            return []
        return [l.split(SEP) for l in lines]

    def spawn(n, argv, extra=None):
        names.append(n)
        k = len(launches())
        r = dch("--spawn", n, "--size", "100x30", *argv, extra=extra)
        check("%s: spawned" % n, r.returncode == 0, r.stderr)
        got = until(lambda: len(launches()) > k and launches()[k])
        if got:
            pids.append(int(got[0]))
        return got

    def rec(n):
        try:
            return json.load(open(os.path.join(recdir, n + ".json")))
        except (OSError, ValueError):
            return None

    def ended(n):
        return until(lambda: (rec(n) or {}).get("end"), 15)

    def hist(n):
        r = dch("--history", "--json")
        try:
            return [o for o in json.loads(r.stdout) if o["name"] == n]
        except (ValueError, KeyError):
            return []

    def revive(n, *a, extra=None):
        k = len(launches())
        r = dch("--revive", n, *a, extra=extra)
        got = until(lambda: len(launches()) > k and launches()[k])
        if got:
            pids.append(int(got[0]))
        return r, got

    try:
        # 1. live: a snapshot names the thread while Codex runs
        got = spawn("cx1", [fake, "-m", "o3", "fix the bug"],
                    {"CODEX_HOME": cxhome, "FAKE_CODEX_TID": T1,
                     "FAKE_CODEX_SNAP": "1"})
        r = until(lambda: ((rec("cx1") or {}).get("codex") or {})
                  .get("thread_id") == T1 and rec("cx1"), 10)
        check("live: the running session's record holds the Codex thread "
              "and its CODEX_HOME", r and r["codex"] == {
                  "thread_id": T1, "home": cxhome}, rec("cx1"))
        # SIGKILL: no exit line, the snapshot stays; the live id must hold
        if got:
            os.kill(int(got[0]), signal.SIGKILL)
        ended("cx1")
        h = hist("cx1")
        check("--history --json shows the codex object", len(h) == 1 and
              h[0].get("codex") == {"thread_id": T1, "home": cxhome}, h)
        out = dch("--history").stdout
        line = [l for l in out.splitlines() if l.startswith("cx1 ")]
        check("--history text shows [codex <id>]", line and
              ("[codex %s]" % T1) in line[0], out)

        # 2. --revive: codex resume <id> -C <cwd> <message>, under the home
        r, got = revive("cx1")
        want = [fake, "--no-daemon", "resume", T1, "-C", proj, "-m", "o3"]
        check("revive runs codex resume <id> -C <cwd>, its options kept, "
              "the old prompt gone, --no-daemon kept", got and
              got[2:-1] == want, got)
        check("revive passes the resume message as the PROMPT", got and
              got[-1].startswith("resume all work. This session ended") and
              "codex resume" in got[-1], got and got[-1])
        check("revived Codex runs under the recorded CODEX_HOME", got and
              got[1] == cxhome, got)
        check("revive says codex resumed", r.returncode == 0 and
              ("codex resumed %s" % T1) in r.stdout, r.stdout + r.stderr)
        check("the revived session's record starts with the thread",
              until(lambda: ((rec("cx1") or {}).get("codex") or {})
                    .get("thread_id") == T1), rec("cx1"))
        if got:
            os.kill(int(got[0]), signal.SIGKILL)
        ended("cx1")
        until(lambda: (rec("cx1") or {}).get("end"), 10)

        # 3. exit line: no snapshot, Codex's exit summary carries the id
        got = spawn("cx2", [fake, "resume", "--last", "-C", "/elsewhere",
                            "--no-alt-screen"], {"FAKE_CODEX_TID": T2})
        time.sleep(1)
        if got:
            os.kill(int(got[0]), signal.SIGUSR1)
        ended("cx2")
        r2 = rec("cx2") or {}
        check("exit line: the record holds the thread from `codex resume "
              "<id>`", (r2.get("codex") or {}).get("thread_id") == T2 and
              r2["codex"]["home"] == os.path.join(home, ".codex"), r2)
        r, got = revive("cx2", "--json", "--no-message")
        check("revive drops resume, --last and the old -C; --no-message "
              "passes no prompt", got and got[2:] == [
                  fake, "--no-daemon", "resume", T2, "-C", proj,
                  "--no-alt-screen"], got)
        fin = [json.loads(l) for l in r.stdout.splitlines() if '"result"' in l]
        check("--json: ok with the thread as session_id", fin and
              fin[-1]["result"] == "ok" and fin[-1]["session_id"] == T2,
              r.stdout)
        check("#676 --json: a Codex record says \"agent\":\"codex\", "
              "running last", fin and fin[-1].get("agent") == "codex" and
              list(fin[-1])[-1] == "running", r.stdout)
        if got:
            os.kill(int(got[0]), signal.SIGKILL)
        ended("cx2")

        # 4. a message that starts with '-' stays a PROMPT
        r, got = revive("cx2", extra={"DCH_REVIVE_MSG": "-carry on"})
        check("DCH_REVIVE_MSG goes after --", got and
              got[-2:] == ["--", "-carry on"], got)
        if got:
            os.kill(int(got[0]), signal.SIGKILL)
        ended("cx2")

        # 5. no id: revived as before, the argv as it was
        got = spawn("cx3", [fake, "hello there"])
        time.sleep(0.8)
        if got:
            os.kill(int(got[0]), signal.SIGKILL)
        ended("cx3")
        check("no id: no codex object", "codex" not in (rec("cx3") or {}),
              rec("cx3"))
        r, got = revive("cx3")
        check("no id: revived with its argv as before", got and
              got[2:] == [fake, "--no-daemon", "hello there"] and
              "codex resumed" not in r.stdout, (got, r.stdout))

        # 6. (#662) an inherited CODEX_THREAD_ID (dch run from a Codex's
        # tool shell) is not the session's thread: its snapshot's is
        TIN, TOUT = T1.replace("6601", "6611"), T1.replace("6601", "6612")
        got = spawn("cx4", [fake, "-m", "o3"],
                    {"CODEX_HOME": cxhome, "CODEX_THREAD_ID": TOUT,
                     "FAKE_CODEX_TID": TIN, "FAKE_CODEX_SNAP": "1"})
        until(lambda: ((rec("cx4") or {}).get("codex") or {})
              .get("thread_id"), 10)
        if got:
            os.kill(int(got[0]), signal.SIGKILL)
        ended("cx4")
        r4 = rec("cx4") or {}
        check("inherited CODEX_THREAD_ID: the record keeps the snapshot's "
              "thread after SIGKILL",
              (r4.get("codex") or {}).get("thread_id") == TIN, r4)

        # 7. (#662) a plain shell that prints a `codex resume <id>` line
        # gets no codex record
        names.append("cx5")
        dch("--spawn", "cx5", "--size", "100x30", "sh", "-c",
            "printf 'To continue this session, run:\\n  codex resume %s\\n'"
            "; sleep 0.5" % T2)
        r5 = until(lambda: (rec("cx5") or {}).get("end") and rec("cx5"), 15)
        check("a shell printing `codex resume <id>`: no codex object",
              r5 and "codex" not in r5, r5)

        # 8. (#676) --revive --json says the agent; a not_utf8 Codex
        # record names `codex resume <id>`, not claude
        r = dch("--revive", "nosuch676", "--json")
        fin = [json.loads(l) for l in r.stdout.splitlines() if '"result"' in l]
        check("#676 no_history: \"agent\":null", fin and
              fin[-1].get("reason") == "no_history" and
              "agent" in fin[-1] and fin[-1]["agent"] is None, r.stdout)
        got = spawn("cx6", [fake, "x\udcffy"],
                    {"CODEX_HOME": cxhome, "FAKE_CODEX_TID": T1,
                     "FAKE_CODEX_SNAP": "1"})
        until(lambda: ((rec("cx6") or {}).get("codex") or {})
              .get("thread_id"), 10)
        if got:
            os.kill(int(got[0]), signal.SIGKILL)
        ended("cx6")
        r = dch("--revive", "cx6", "--json")
        fin = [json.loads(l) for l in r.stdout.splitlines() if '"result"' in l]
        check("#676 not_utf8 Codex record: agent codex, detail says "
              "`codex resume <id>`", fin and fin[-1].get("reason") ==
              "not_utf8" and fin[-1]["agent"] == "codex" and
              fin[-1]["detail"].endswith("codex resume " + T1) and
              "claude" not in fin[-1]["detail"], r.stdout)
        r = dch("--revive", "cx6")
        check("#676 not_utf8 Codex record, text: same hint",
              ("codex resume " + T1) in r.stdout, r.stdout)
    finally:
        for n in names:
            dch("-k", n)
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("codex_resume_test: %s" % ("PASS" if not fails[0] else
                                     "%d FAIL" % fails[0]))
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
