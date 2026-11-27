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
  - xv-cx, an ended Codex with no heartbeat: the tab, empty, with the warn

Run:  python3 tests/xray_view_schedules_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)

SCSID = "11111111-2222-4333-8444-5555555555e7"
SNSID = "11111111-2222-4333-8444-5555555555e8"
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
                            "payload": {"id": tid, "cwd": T,
                                        "cli_version": "0.167.2"}}) + "\n" +
                msg("user", "input_text", "fix the parser") +
                msg("assistant", "output_text", "done"))
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = dict(name=name, sock_dir=sockdir, alias="", cwd=T, argv=["codex"],
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


def test_codex_warn():
    v = Pty([DCH, "--xray", "xv-cx"], rows=30, cols=110)
    try:
        ok = tabbar(v, "Schedules 0")
        v.key(b"\033[Z", 0.8)
        r = tasks_rows(v)
        check("Schedules: a Codex with no heartbeat: the tab, empty, the "
              "reason in place of rows", ok and r ==
              ["Codex CLI has no session scheduler; Codex app automations "
               "show when they fire"], (rows(v.out).get(2), r))
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def main():
    run_steps(fixture_sched, test_sched, test_none, test_codex_warn)


if __name__ == "__main__":
    main()
