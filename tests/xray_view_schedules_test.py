#!/usr/bin/env python3
"""The `dch --xray <n>` view's Schedules tab (#687), over fake transcripts:

  - xv-sc, an ended Claude: a durable pending cron (next in 10 m), a fired
    one-shot, a deleted cron and a /loop wake-up (ended: not live) that
    fired once and then rescheduled as a no-op. The tab after Tasks, its
    count the pending rows; Pending, Fired, Ended with their counts; the
    row's human schedule, "next 9m", "fired ... ago", x fires, noop N
  - Enter reads the row's turn, q back; c opens the card (the whole cron,
    its schedule, created, expires), Enter there reads the turn too
  - xv-sn, no schedule: no Schedules tab, Tab never lands on one
  - xv-cx, an ended Codex with no heartbeat and an AGENTS.md: no Schedules
    tab (its warn is no row), Memory the last tab
  - xv-sl, a live Claude: a CronCreate appended reaches the shown tab from
    the follow's schedules block, no --schedules child forked

Run:  python3 tests/xray_view_schedules_test.py      (uses ./dch)
"""
import threading
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)

SCSID = "11111111-2222-4333-8444-5555555555e7"
SNSID = "11111111-2222-4333-8444-5555555555e8"
SLSID = "11111111-2222-4333-8444-5555555555e9"
CXTID = "019a3333-4444-7555-8666-7777888899e7"
NOW = time.time()


def ms(ago):
    return int((NOW - ago) * 1000)


def cron(ts, tid, cid, prompt, rec, cr, human, nxt, durable):
    return [at(call(tid, "CronCreate", {"cron": cr, "prompt": prompt,
                                        "recurring": rec}), ts),
            at(result(tid, "ok", {"toolUseResult": {
                "id": cid, "humanSchedule": human, "recurring": rec,
                "durable": durable, "nextFireAt": nxt}}), ts)]


def wake(ts, tid, delay, noop):
    return [at(call(tid, "ScheduleWakeup", {
                "delaySeconds": delay, "reason": "waiting on CI",
                "prompt": "check again", "noop": noop}), ts),
            at(result(tid, "ok", {"toolUseResult": {
                "scheduledFor": ms(-delay), "clampedDelaySeconds": delay,
                "wasClamped": False}}), ts)]


def fire(ts, tid, **kw):
    return dict({"type": "system", "subtype": "scheduled_task_fire",
                 "timestamp": ts, "taskId": tid, "cron": "* * * * *",
                 "prompt": "fired", "isMeta": True}, **kw)


def codex_rec(name, tid):
    """an ended Codex with no heartbeat: its History record, a rollout"""
    cwd = os.path.join(T, "cxwork")
    os.makedirs(cwd, exist_ok=True)
    with open(os.path.join(cwd, "AGENTS.md"), "w") as f:
        f.write("codex agents text\n")
    home = os.path.join(T, "codexhome")
    day = os.path.join(home, "sessions", "2026", "11", "10")
    os.makedirs(day, exist_ok=True)
    msg = lambda role, kind, text: json.dumps({
        "timestamp": iso(600), "type": "response_item", "payload": {
            "type": "message", "role": role,
            "content": [{"type": kind, "text": text}]}}) + "\n"
    p = os.path.join(day, "rollout-2026-11-10T10-00-00-%s.jsonl" % tid)
    with open(p, "w") as f:
        f.write(json.dumps({"timestamp": iso(600), "type": "session_meta",
                            "payload": {"id": tid, "cwd": cwd,
                                        "cli_version": "0.167.2"}}) + "\n" +
                msg("user", "input_text", "fix the parser") +
                msg("assistant", "output_text", "done"))
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = dict(name=name, sock_dir=sockdir, alias="", cwd=cwd, argv=["codex"],
               started=int(NOW) - 660, updated=int(NOW) - 60,
               ended=int(NOW) - 60, end="exit 0", agent=None,
               codex={"thread_id": tid, "home": home})
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def fixture_sched():
    os.makedirs(os.path.dirname(tl(SCSID)), exist_ok=True)
    t0 = iso(3600)
    record("xv-sc", SCSID,
           turn("set up the jobs", t0,
                *(cron(t0, "c1", "aaaa0001", "check CI on #812", True,
                       "*/10 * * * *", "every 10 minutes", ms(-600), True) +
                  cron(t0, "c2", "bbbb0002", "drop me", False, "5 9 * * *",
                       "at 09:05", ms(-900), False) +
                  cron(t0, "c3", "cccc0003", "deploy check", False,
                       "30 8 * * *", "at 08:30", ms(1800), True) +
                  wake(t0, "w1", 1200, False))) +
           turn("go on", iso(1800),
                fire(iso(1800), "cccc0003"),
                at(call("c4", "CronDelete", {"id": "bbbb0002"}), iso(1700)),
                at(result("c4", "ok", {"toolUseResult": {"id": "bbbb0002"}}),
                   iso(1700)),
                fire(iso(1200), "zz99", taskKind="loop", cronKind="loop"),
                *wake(iso(1100), "w2", 1800, True)))
    record("xv-sn", SNSID, turn("no schedules here", iso(60), say("fine")))
    codex_rec("xv-cx", CXTID)


def tab_list(v):
    tabs = []
    for _ in range(5):
        v.key(b"\t", 0.4)
        tabs.append(rows(v.out).get(2, ""))
    return tabs


def test_sched():
    v = Pty([DCH, "--xray", "xv-sc"], rows=30, cols=110)
    try:
        ok = tabbar(v, "Schedules 1")
        t = rows(v.out).get(2, "")
        check("Schedules: a tab after Search, its count the pending rows",
              ok and 0 <= t.find("Prompts") < t.find("Search") <
              t.find("Schedules 1") and re.search(r"Schedules 1\s*$", t), t)
        v.key(b"\033[Z", 0.8)
        r = tasks_rows(v)
        grp = [x for x in r if re.match(r"^(Pending|Fired|Ended) \d+$", x)]
        check("Schedules: Shift-Tab opens it; Pending, Fired, Ended, each "
              "counted", grp == ["Pending 1", "Fired 1", "Ended 2"], r)
        line = lambda w: next((x for x in r if w in x), "")
        check("Schedules: the pending cron selected first: mark, id, prompt, "
              "human, next 9m",
              re.search(r"^▌○ aaaa0001 check CI on #812 every 10 minutes "
                        r"next (9|10)m$", line("aaaa0001")), r)
        check("Schedules: the fired one-shot: fired 30m ago, ×1",
              re.search(r"✓ cccc0003 deploy check at 08:30 fired 30m ago "
                        r"×1$", line("cccc0003")), r)
        check("Schedules: the deleted cron and the wake-up in Ended: their "
              "status, the wake-up's ×1 and noop 1",
              re.search(r"✗ bbbb0002 drop me at 09:05 deleted$",
                        line("bbbb0002")) and
              re.search(r"✗ wakeup check again .*ended ×1 noop 1$",
                        line("wakeup")), r)
        keys = rows(v.out)
        check("Schedules: the keys say read and card",
              any("read" in x and "card" in x for x in keys.values()), keys)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Schedules: Enter reads the row's turn",
              v.wait(b"set up the jobs", start=mark) and
              v.wait(b"CronCreate", start=mark), screen(v, mark))
        v.key(b"q", 1.0)
        check("Schedules: q back to the same row", tabbar(v, "Schedules 1")
              and "aaaa0001" in head(v), head(v))
        mark = len(v.out)
        v.key(b"c", 1.0)
        s = screen(v, mark)
        check("Schedules: c opens the card: id, the whole cron, schedule, "
              "next, created, expires, the turn",
              all(w in s for w in ("Schedule aaaa0001", "*/10 * * * *",
                                   "every 10 minutes", "pending cron, "
                                   "recurring", "Next", "Created",
                                   "Expires", "enter reads it")), s)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Schedules: Enter on the card reads the turn",
              v.wait(b"CronCreate", start=mark), screen(v, mark))
        v.key(b"q", 0.8)
        v.key(b"q", 0.8)
        v.key(b"jj", 0.5)  # the wake-up: Ended's first row
        mark = len(v.out)
        v.key(b"c", 1.0)
        s = screen(v, mark)
        check("Schedules: the wake-up's card: its reason, fires and noop",
              "Schedule wakeup" in s and "waiting on CI" in s and
              "1 (the last 1 no-op)" in s, s)
        v.key(b"q", 0.5)
        v.key(b"q")
        check("Schedules: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_none():
    v = Pty([DCH, "--xray", "xv-sn"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 1")
        v.pump(1.5)
        tabs = tab_list(v)
        check("Schedules: no schedule, no tab, Tab never lands on one",
              all("Schedules" not in t for t in tabs) and
              any(re.search(r"\[Prompts", t) for t in tabs), tabs)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_codex_none():
    v = Pty([DCH, "--xray", "xv-cx"], rows=30, cols=110)
    try:
        ok = tabbar(v, "Memory 1")
        v.pump(1.5)
        t = rows(v.out).get(2, "")
        v.key(b"\033[Z", 0.8)
        r = [x.lstrip("▌ ") for x in tasks_rows(v)]
        check("Schedules: a Codex with no heartbeat: no tab; Memory the "
              "last tab", ok and "Schedules" not in t and
              re.search(r"Memory 1\s*$", t) and r[:1] == ["Instructions 1"],
              (t, r))
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_live():
    """a live Claude's CronCreate reaches the shown tab from the follow
    (#686's block [2]); the view forks no --schedules child"""
    cwd = os.path.realpath(os.path.join(T, "slive"))
    os.makedirs(cwd, exist_ok=True)
    proj = os.path.join(HOME, ".claude", "projects", cwd.replace("/", "-"))
    os.makedirs(proj, exist_ok=True)
    path = os.path.join(proj, SLSID + ".jsonl")

    def add(recs, mode="a"):
        with open(path, mode) as f:
            for o in recs:
                f.write(json.dumps(dict(o, cwd=cwd) if "cwd" in o else o) +
                        "\n")

    t0 = iso(600)
    add(turn("watch it", t0, *cron(t0, "c1", "dddd0001", "first watch",
                                    True, "*/5 * * * *", "every 5 minutes",
                                    ms(-300), False)), "w")
    dch("--spawn", "xv-sl", "/bin/sleep", "300")
    pid = until(lambda: json.loads(dch("--cmd", "xv-sl").stdout or "{}")
                .get("pid"), 5)
    start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    sd = os.path.join(HOME, ".claude", "sessions")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "%d.json" % pid), "w") as f:
        json.dump({"pid": pid, "sessionId": SLSID, "cwd": cwd,
                   "procStart": start, "version": "2.1.0",
                   "kind": "interactive", "status": "busy",
                   "statusUpdatedAt": 1}, f)
    v = Pty([DCH, "--xray", "xv-sl"], rows=30, cols=110)
    seen, stop = {}, []

    def sample():  # every child of the view and its command line
        while not stop:
            r = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,args="],
                               capture_output=True, text=True)
            for x in r.stdout.splitlines():
                f = x.split(None, 2)
                if len(f) == 3 and int(f[1]) == v.pid:
                    seen[int(f[0])] = f[2][:160]
            time.sleep(0.02)

    th = threading.Thread(target=sample, daemon=True)
    try:
        ok = tabbar(v, "Schedules 1", 15)
        th.start()
        v.key(b"\033[Z", 0.8)
        check("live: the tab from the follow, its one cron", ok and any(
            "first watch" in x for x in tasks_rows(v)), tasks_rows(v))
        ts = iso(1)
        add(turn("one more", ts, *cron(ts, "c2", "eeee0002",
                                       "second watch", True, "0 * * * *",
                                       "every hour", ms(-3000), False)))
        ok = tabbar(v, "Schedules 2", 15)
        until(lambda: (v.pump(0.1), any("second watch" in x
                                        for x in tasks_rows(v)))[1], 10)
        r = tasks_rows(v)
        check("live: a CronCreate appended reaches the shown tab without "
              "reopening", ok and any("eeee0002 second watch" in x
                                      for x in r), r)
        stop.append(1)
        th.join(2)
        check("live: one child, the follow, the whole time: no "
              "--schedules query forked", len(seen) == 1, seen)
        v.key(b"q")
        check("live: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        stop.append(1)
        v.close()
        dch("-k", "xv-sl")


def main():
    run_steps(fixture_sched, test_sched, test_none, test_codex_none,
              test_live)


if __name__ == "__main__":
    main()
