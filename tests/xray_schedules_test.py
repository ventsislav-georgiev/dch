#!/usr/bin/env python3
"""`dch --xray <n> --schedules --json` (#685): a session's crons, its /loop
wake-up and Codex heartbeats as JSON lines, over fake transcripts only.

  - the header ("block":"schedules", live) and row key order, exactly
  - pending, deleted, fired (a one-shot) and a recurring fire; the 120
    character prompt cap; a /loop wake-up row; createdAt over 7 days: expired
  - live from the running (fake) Claude; its History record after it exits,
    and a planted one: live false, pending rows "ended"
  - an ended Codex rollout: a heartbeat envelope's row; none: the warn
  - a shell: source "none"; the usage errors
  - the tasks and memory headers carry "block" too
  - durable crons (#689) against the project's .claude/scheduled_tasks.json:
    listed stays pending with its lastFiredAt, not listed ended, a job only
    the file names not added; absent, a symlink or over 1 MiB: the
    transcript's answer; unparsable: that answer plus a warn

Run:  python3 tests/xray_schedules_test.py    (uses ./dch; run.sh isolates)
"""
import json, os, shutil, sys, time

from xray_cli_lib import *  # noqa: E402,F401,F403

HEAD = ["schema", "block", "session", "session_id", "source", "live",
        "title", "partial"]
KEYS = ["id", "kind", "status", "recurring", "cron", "human", "durable",
        "prompt", "reason", "created", "next", "last_fired", "expires",
        "fires", "noop_streak", "chapter", "created_chapter"]
HSID = "11111111-2222-4333-8444-5555555555d1"
TID = "019a3333-4444-7555-8666-7777888899d1"
TID2 = "019a3333-4444-7555-8666-7777888899d2"
NOW = int(time.time()) // 60 * 60
B = NOW - 3600			# the transcript's first line
WEEK = 7 * 86400


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def ts(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def scheds(n, env=None):
    """(rc, header, [schedule], done, stderr)"""
    r = dch("--xray", n, "--schedules", "--json", env=env)
    ls = ref_lines(r.stdout) or [{}]
    return (r.returncode, ls[0], [l.get("schedule") for l in ls[1:-1]],
            ls[-1], r.stderr, r.stdout)


def ask(t, text, cwd):
    return {"type": "user", "cwd": cwd, "timestamp": ts(t),
            "message": {"content": text}}


def use(t, tuid, name, inp):
    return {"type": "assistant", "timestamp": ts(t), "message": {"content": [
        {"type": "tool_use", "id": tuid, "name": name, "input": inp}]}}


def res(t, tuid, r):
    return {"type": "user", "timestamp": ts(t), "toolUseResult": r,
            "message": {"content": [{"type": "tool_result",
                                     "tool_use_id": tuid,
                                     "content": "ok"}]}}


def fire(t, tid, **kw):
    return dict({"type": "system", "subtype": "scheduled_task_fire",
                 "timestamp": ts(t), "taskId": tid, "cron": "* * * * *",
                 "prompt": "fired", "isMeta": True}, **kw)


def cron(t, tuid, cid, prompt, recurring, cr="*/10 * * * *",
         human="every 10 minutes", nxt=None):
    r = {"id": cid, "humanSchedule": human, "recurring": recurring,
         "durable": False}
    if nxt is not None:
        r["nextFireAt"] = nxt * 1000
    return [use(t, tuid, "CronCreate", {"cron": cr, "prompt": prompt,
                                        "recurring": recurring}),
            res(t, tuid, r)]


LONG = "watch the deploy " + "x" * 200


def transcript(cwd):
    """the lines, the rows they make (chapters filled in later)"""
    return ([ask(B, "set up the jobs", cwd)] +
            cron(B + 60, "tu1", "aaaa0001", "check CI on #812", True,
                 nxt=NOW + 600) +
            cron(B + 60, "tu2", "bbbb0002", "drop me", False, cr="5 9 * * *",
                 human="at 09:05", nxt=NOW + 900) +
            cron(B + 60, "tu3", "cccc0003", LONG, False, cr="30 8 * * *",
                 human="at 08:30", nxt=B + 600) +
            [use(B + 120, "w1", "ScheduleWakeup",
                 {"delaySeconds": 1200, "reason": "waiting on CI",
                  "prompt": "check again", "noop": False}),
             res(B + 120, "w1", {"scheduledFor": (B + 1320) * 1000,
                                 "clampedDelaySeconds": 1200,
                                 "wasClamped": False}),
             {"type": "attachment", "timestamp": ts(B + 150), "attachment": {
                 "type": "session_cron_carry",
                 "tasks": [{"id": "aaaa0001", "cron": "*/10 * * * *",
                            "prompt": "check CI on #812",
                            "createdAt": (B + 60) * 1000, "recurring": True},
                           {"id": "bbbb0002", "cron": "5 9 * * *",
                            "prompt": "drop me",
                            "createdAt": (B + 60) * 1000, "recurring": False},
                           {"id": "cccc0003", "cron": "30 8 * * *",
                            "prompt": LONG,
                            "createdAt": (B + 60) * 1000, "recurring": False},
                           {"id": "eeee0005", "cron": "0 9 * * *",
                            "prompt": "old daily",
                            "createdAt": (NOW - 8 * 86400) * 1000,
                            "recurring": True}],
                 "ended": []}},
             ask(B + 600, "go on", cwd),
             fire(B + 600, "cccc0003"),
             use(B + 660, "tu4", "CronDelete", {"id": "bbbb0002"}),
             res(B + 660, "tu4", {"id": "bbbb0002"}),
             fire(B + 1320, "zz99", taskKind="loop", cronKind="loop"),
             use(B + 1380, "w2", "ScheduleWakeup",
                 {"delaySeconds": 1800, "reason": "still waiting",
                  "prompt": "check again", "noop": True}),
             res(B + 1380, "w2", {"scheduledFor": (NOW + 1800) * 1000,
                                  "clampedDelaySeconds": 1800,
                                  "wasClamped": False})])


def want(a0, a1, live):
    """the rows in their order: pending by next, the wake-up, the rest
    newest change first (not live: the ended cron among the rest)"""
    pend = "pending" if live else "ended"
    row = lambda **kw: dict(dict.fromkeys(KEYS), **kw)
    v = [
        row(id="aaaa0001", kind="cron", status=pend, recurring=True,
            cron="*/10 * * * *", human="every 10 minutes", durable=False,
            prompt="check CI on #812", created=iso(B + 60),
            next=iso(NOW + 600) if live else None, expires=iso(B + 60 + WEEK),
            fires=0, chapter=a0, created_chapter=a0),
        row(id="wakeup", kind="wakeup", status=pend, recurring=False,
            human="in 30m", durable=False, prompt="check again",
            reason="still waiting", created=iso(B + 1380),
            next=iso(NOW + 1800) if live else None,
            last_fired=iso(B + 1320), fires=1, noop_streak=1,
            chapter=a1, created_chapter=a1),
        row(id="bbbb0002", kind="cron", status="deleted", recurring=False,
            cron="5 9 * * *", human="at 09:05", durable=False,
            prompt="drop me", created=iso(B + 60), fires=0,
            chapter=a1, created_chapter=a0),
        row(id="cccc0003", kind="cron", status="fired", recurring=False,
            cron="30 8 * * *", human="at 08:30", durable=False,
            prompt=LONG[:120], created=iso(B + 60), last_fired=iso(B + 600),
            fires=1, chapter=a1, created_chapter=a0),
        row(id="eeee0005", kind="cron", status="expired", recurring=True,
            cron="0 9 * * *", durable=False, prompt="old daily",
            created=iso(NOW - 8 * 86400),
            expires=iso(NOW - 8 * 86400 + WEEK), fires=0,
            chapter=a0, created_chapter=a0)]
    return v if live else v[1:] + v[:1]


def write(path, recs):
    with open(path, "w") as f:
        for o in recs:
            f.write(json.dumps(o) + "\n")


def anchors(n):
    rc, o, err = xray(n, "--json")
    return [c["anchor"] for c in (o or {}).get("chapters", [])]


def check_rows(what, n, sid, live, a, source="claude"):
    rc, h, rows, done, err, out = scheds(n)
    check("%s: the header, its keys in order" % what,
          rc == 0 and list(h) == HEAD and
          h == {"schema": 1, "block": "schedules", "session": n,
                "session_id": sid, "source": source, "live": live,
                "title": None, "partial": False}, (rc, out, err))
    check("%s: each row's keys in order, then done" % what,
          all(r and list(r) == KEYS for r in rows) and
          done == {"done": True, "count": 5, "dropped": 0}, out)
    w = want(a[0], a[1], live) if len(a) == 2 else None
    check("%s: pending, deleted, fired, the wake-up, expired; the prompt "
          "cut at 120; %s" % (what, "next set" if live else
                              "pending rows ended, no next"),
          rows == w, [(i, k, r.get(k), w[i].get(k)) for i, r in
                      enumerate(rows) if w and i < len(w) for k in KEYS
                      if r.get(k) != w[i].get(k)] if w else (rows, a))
    return out


def history(name, sid, cfg, cwd):
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": name, "cwd": cwd, "end": "exited", "argv": ["claude"],
           "started": NOW - 7200, "updated": NOW - 60, "ended": NOW - 60,
           "sock_dir": sockdir,
           "agent": {"session_id": sid, "cwd": cwd, "config_dir": cfg}}
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def codex(name, tid, beat):
    """an ended Codex: its History record and a fake rollout"""
    home = os.path.join(T, "codexhome")
    day = os.path.join(home, "sessions", "2026", "11", "10")
    os.makedirs(day, exist_ok=True)
    line = lambda s, typ, p: json.dumps({"timestamp": ts(B + s), "type": typ,
                                         "payload": p}) + "\n"
    msg = lambda role, kind, text: line(1, "response_item", {
        "type": "message", "role": role,
        "content": [{"type": kind, "text": text}]})
    body = (line(0, "session_meta", {"id": tid, "cwd": T,
                                     "cli_version": "0.167.2"}) +
            msg("user", "input_text", "fix the parser"))
    if beat:
        body += msg("user", "input_text",
                    "<heartbeat>\n  <automation_id>auto-1</automation_id>\n"
                    "  <current_time_iso>%s</current_time_iso>\n"
                    "  <instructions>\nCheck the build\n  </instructions>\n"
                    "</heartbeat>" % iso(B + 300))
    body += msg("assistant", "output_text", "done")
    p = os.path.join(day, "rollout-2026-11-10T10-00-00-%s.jsonl" % tid)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, 0o600)
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, mode=0o700, exist_ok=True)
    rec = dict(name=name, sock_dir=sockdir, alias="", cwd=T, argv=["codex"],
               started=NOW - 660, updated=NOW - 60, ended=NOW - 60,
               end="exit 0", agent=None,
               codex={"thread_id": tid, "home": home})
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def rec_get(n):
    p = os.path.join(T, "state", "dch", "sessions",
                     os.path.join(T, "dch-%d" % os.getuid()).replace("/", "-"),
                     n + ".json")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def test_live_claude():
    """a fake Claude: live true; after it exits, its History record: live
    false, the pending rows ended"""
    n = "xs-live"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "proj")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir, DCH_RECORD_MS="300")
    r = dch("--spawn", n, "--size", "80x24", "--cwd", proj, "claude",
            env=env)
    check("live: fake claude spawned", r.returncode == 0, r.stderr)
    sdir = os.path.join(HOME, ".claude", "sessions")
    rec = wait_for(lambda: [f for f in os.listdir(sdir)
                            if f.endswith(".json")]
                   if os.path.isdir(sdir) else None, 10)
    if not rec:
        check("live: the fake's session record", False)
        dch("-k", n)
        return
    sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
    pdir = os.path.join(HOME, ".claude", "projects", key(proj))
    os.makedirs(pdir, exist_ok=True)
    write(os.path.join(pdir, sid + ".jsonl"), transcript(proj))
    try:
        a = wait_for(lambda: (lambda x: x if len(x) == 2 else None)
                     (anchors(n)), 10) or []
        check_rows("live Claude", n, sid, True, a)
        # the record keeps the agent once a refresh saw it
        check("live: the record holds the agent",
              wait_for(lambda: (rec_get(n).get("agent") or {})
                       .get("session_id") == sid, 10), rec_get(n))
    finally:
        dch("-k", n)
    check("live: the exited session's History record",
          wait_for(lambda: rec_get(n).get("end"), 10), rec_get(n))
    check_rows("exited Claude (History)", n, sid, False, a)


def test_history():
    """a planted ended record: live false, ended rows"""
    cfg = os.path.join(HOME, ".claude")
    cwd = "/home/demo/sched"
    pdir = os.path.join(cfg, "projects", key(cwd))
    os.makedirs(pdir, exist_ok=True)
    write(os.path.join(pdir, HSID + ".jsonl"), transcript(cwd))
    history("xs-hist", HSID, cfg, cwd)
    check_rows("ended History record", "xs-hist", HSID, False,
               anchors("xs-hist"))
    # the read range cut: partial, as --tasks
    caps = dict(ENV, DCH_XRAY_CAPS="0:0:0:%d:%d" % (200, 600))
    rc, h, rows, done, err, out = scheds("xs-hist", env=caps)
    check("history: a read range that skipped bytes is partial",
          rc == 0 and h.get("partial") is True and done.get("done"), out)
    # the other blocks' headers say which block they are (#685)
    r = dch("--xray", "xs-hist", "--tasks", "--json")
    h = (ref_lines(r.stdout) or [{}])[0]
    check("tasks header: \"block\":\"tasks\" after schema",
          r.returncode == 0 and list(h)[:3] == ["schema", "block", "session"]
          and h["block"] == "tasks", r.stdout)
    r = dch("--xray", "xs-hist", "--memory", "--json")
    h = (ref_lines(r.stdout) or [{}])[0]
    check("memory header: \"block\":\"memory\" after schema",
          r.returncode == 0 and list(h)[:3] == ["schema", "block", "source"]
          and h["block"] == "memory", r.stdout)


def test_codex():
    codex("xs-cx", TID, True)
    rc, h, rows, done, err, out = scheds("xs-cx")
    check("Codex: a heartbeat envelope gives a fired heartbeat row",
          rc == 0 and list(h) == HEAD and h["source"] == "codex" and
          h["session_id"] == TID and h["live"] is False and
          rows == [dict(dict.fromkeys(KEYS), id="auto-1", kind="heartbeat",
                        status="fired", recurring=False, durable=False,
                        prompt="Check the build", created=iso(B + 300),
                        last_fired=iso(B + 300), fires=1,
                        chapter=rows[0]["chapter"] if rows else None,
                        created_chapter=rows[0]["created_chapter"]
                        if rows else None)] and
          done == {"done": True, "count": 1, "dropped": 0}, out)
    codex("xs-cx2", TID2, False)
    rc, h, rows, done, err, out = scheds("xs-cx2")
    check("Codex: no heartbeat: no row, the warn in the header",
          rc == 0 and h.get("source") == "codex" and rows == [] and
          list(h) == HEAD + ["warn"] and
          h["warn"] == "Codex CLI has no session scheduler; Codex app "
          "automations show when they fire" and
          done == {"done": True, "count": 0, "dropped": 0}, out)


def test_shell():
    n = "xs-sh"
    try:
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c", "sleep 60")
        check("shell: spawned", r.returncode == 0, r.stderr)
        rc, o, err = xray(n, "--json")
        rc, h, rows, done, err, out = scheds(n)
        check("shell: source none, live false, no row",
              rc == 0 and list(h) == HEAD and h["source"] == "none" and
              h["live"] is False and h["session"] == n and
              h["session_id"] == (o or {}).get("session_id") and
              rows == [] and
              done == {"done": True, "count": 0, "dropped": 0}, out)
    finally:
        dch("-k", n)


def test_usage():
    for a, msg in (
            (["--ref", "#1"], "--schedules and --ref"),
            (["--search", "needle"], "--schedules and --search"),
            (["--chapter", "f1:0"], "--schedules and --chapter"),
            (["--clear"], "--schedules and --clear"),
            (["--tasks"], "--schedules and --tasks"),
            (["--memory"], "--schedules and --memory"),
            (["--questions"], "--questions and --schedules"),
            (["--schedules"], "--schedules twice"),
            (["--since", "1h"], "--since takes --json only")):
        r = dch("--xray", "xs-hist", "--schedules", *a, "--json")
        check("usage: --schedules with %s: exit 2" % a[0],
              r.returncode == 2 and r.stdout == "" and
              r.stderr == "dch: bad usage: %s\n" % msg,
              (r.returncode, r.stdout, r.stderr))
    r = dch("--xray", "xs-hist", "--schedules")
    check("usage: --schedules without --json: exit 2",
          r.returncode == 2 and r.stdout == "" and
          "--json is required" in r.stderr, (r.returncode, r.stderr))


def test_durable():
    """an ended record whose transcript made two durable crons, the fake
    project's .claude/scheduled_tasks.json rewritten between asks"""
    cfg = os.path.join(HOME, ".claude")
    cwd = os.path.join(T, "dproj")
    sid = "11111111-2222-4333-8444-5555555555d9"
    pdir = os.path.join(cfg, "projects", key(cwd))
    cdir = os.path.join(cwd, ".claude")
    os.makedirs(pdir, exist_ok=True)
    os.makedirs(cdir, exist_ok=True)
    lines = [ask(B, "durable jobs", cwd)]
    for tu, cid, rec in (("d1", "dddd0001", True), ("d2", "dddd0002", False)):
        lines += [use(B + 60, tu, "CronCreate", {"cron": "*/10 * * * *",
                                                 "prompt": "p " + cid,
                                                 "recurring": rec,
                                                 "durable": True}),
                  res(B + 60, tu, {"id": cid, "humanSchedule": "x",
                                   "recurring": rec, "durable": True,
                                   "nextFireAt": (NOW + 600) * 1000})]
    write(os.path.join(pdir, sid + ".jsonl"), lines)
    history("xs-dur", sid, cfg, cwd)
    fpath = os.path.join(cdir, "scheduled_tasks.json")

    def task(cid, **kw):
        return dict({"id": cid, "cron": "*/10 * * * *", "prompt": "p " + cid,
                     "createdAt": (B + 60) * 1000}, **kw)

    def put(body):
        if os.path.lexists(fpath):
            os.unlink(fpath)
        if body is not None:
            with open(fpath, "w") as f:
                f.write(body)

    def ask_rows(what, st1, st2, warn=False, last=None):
        rc, h, rows, done, err, out = scheds("xs-dur")
        by = {r["id"]: r for r in rows if r}
        check("durable: %s" % what,
              rc == 0 and h.get("live") is False and
              sorted(by) == ["dddd0001", "dddd0002"] and
              by["dddd0001"]["status"] == st1 and
              by["dddd0002"]["status"] == st2 and
              by["dddd0001"]["durable"] is True and
              by["dddd0001"]["last_fired"] == last and
              (list(h) == HEAD + ["warn"] and
               "scheduled_tasks.json" in h["warn"] if warn
               else list(h) == HEAD), out)

    put(None)
    ask_rows("no file: both pending (the transcript's), no warn",
             "pending", "pending")
    put(json.dumps({"tasks": [task("dddd0001", recurring=True,
                                   lastFiredAt=(NOW - 30) * 1000),
                              task("ffff0009")]}))
    ask_rows("listed stays pending with the file's lastFiredAt; not listed "
             "ended; a file-only job is not added",
             "pending", "ended", last=iso(NOW - 30))
    put(json.dumps({"tasks": []}))
    ask_rows("an empty task list: both ended", "ended", "ended")
    put('{"tasks": [ {"id": "dddd0001"')
    ask_rows("a malformed file: the transcript's answer and a warn",
             "pending", "pending", warn=True)
    other = os.path.join(T, "elsewhere.json")
    with open(other, "w") as f:
        f.write(json.dumps({"tasks": []}))
    put(None)
    os.symlink(other, fpath)
    ask_rows("a symlinked file is refused: the transcript's answer, no warn",
             "pending", "pending")
    put(json.dumps({"tasks": []}) + " " * (1 << 20))
    ask_rows("a file over 1 MiB is refused: the transcript's answer, no "
             "warn", "pending", "pending")
    put(None)
    # a durable recurring cron 8 days old: permanent in the file never ages
    old = NOW - 8 * 86400
    sid8 = "11111111-2222-4333-8444-5555555555da"
    write(os.path.join(pdir, sid8 + ".jsonl"),
          [ask(old, "old job", cwd),
           use(old, "p1", "CronCreate", {"cron": "0 9 * * *", "prompt": "daily",
                                         "recurring": True, "durable": True}),
           res(old, "p1", {"id": "eeee0008", "humanSchedule": "x",
                           "recurring": True, "durable": True})])
    history("xs-dur8", sid8, cfg, cwd)
    for perm, st, exp in ((True, "pending", None),
                          (False, "expired", iso(old + WEEK))):
        t8 = dict(task("eeee0008", recurring=True, createdAt=old * 1000),
                  **({"permanent": True} if perm else {}))
        put(json.dumps({"tasks": [t8]}))
        rc, h, rows, done, err, out = scheds("xs-dur8")
        check("durable: a recurring cron 8 days old, %s: %s, expires %s" %
              ("permanent" if perm else "not permanent", st, exp),
              rc == 0 and len(rows) == 1 and rows[0]["status"] == st and
              rows[0]["expires"] == exp, out)
    put(None)


def main():
    print("xray_schedules_test: %s" % DCH)
    try:
        test_history()
        test_usage()
        test_codex()
        test_shell()
        test_live_claude()
        test_durable()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
