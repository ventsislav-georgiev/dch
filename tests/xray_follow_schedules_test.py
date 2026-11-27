#!/usr/bin/env python3
"""`dch --xray <n> --follow --schedules --json` (#686): the schedules block
as the stream's block [2], over fake transcripts and a fake Claude only.

  - after the snapshot, the one-shot --schedules lines ("block":"schedules")
  - a CronCreate (tool_use and result) appended: its pending row; a
    CronDelete: the row deleted (row lines by id, as the tasks block)
  - the fake Claude exiting in its shell: reset rotated, the shell's
    snapshot, then the block again, live false, its pending row "ended"
  - --follow --tasks --memory --schedules: each block once, in that order;
    the session's end: the block once more, live false, then exit 0

Run:  python3 tests/xray_follow_schedules_test.py   (uses ./dch; run.sh
isolates)
"""
import json, os, shutil, subprocess, sys, time

from xray_cli_lib import *  # noqa: E402,F401,F403
from xray_follow_test import Follow, blocks, is_reset  # noqa: E402

NOW = int(time.time())


def ts(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def use(tuid, name, inp):
    return {"type": "assistant", "timestamp": ts(NOW), "message": {
        "content": [{"type": "tool_use", "id": tuid, "name": name,
                     "input": inp}]}}


def res(tuid, r):
    return {"type": "user", "timestamp": ts(NOW), "toolUseResult": r,
            "message": {"content": [{"type": "tool_result",
                                     "tool_use_id": tuid, "content": "ok"}]}}


def cron(tuid, cid, prompt):
    return [use(tuid, "CronCreate", {"cron": "*/10 * * * *",
                                     "prompt": prompt, "recurring": True}),
            res(tuid, {"id": cid, "humanSchedule": "every 10 minutes",
                       "recurring": True, "durable": False,
                       "nextFireAt": (NOW + 600) * 1000})]


def sched_lines(n):
    r = dch("--xray", n, "--schedules", "--json")
    return [json.loads(l) for l in r.stdout.splitlines()]


def block_after(objs, k):
    """the schedules block that starts at objs[k] (a header with block
    schedules): header, rows, done"""
    for j in range(k, len(objs)):
        if "done" in objs[j]:
            return objs[k:j + 1]
    return None


def sched_blocks(objs):
    return [block_after(objs, k) for k, o in enumerate(objs)
            if o.get("block") == "schedules"]


def rows(blk):
    return {o["schedule"]["id"]: o["schedule"] for o in blk or []
            if "schedule" in o}


def fake_env(tag):
    bindir = os.path.join(T, "bin-" + tag)
    fdir = os.path.join(T, "fake-" + tag)
    proj = os.path.join(T, "proj-" + tag)
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    env.pop("DCH_RECORD_MS", None)
    return env, proj


def new_record(had):
    sdir = os.path.join(HOME, ".claude", "sessions")
    rec = wait_for(lambda: [f for f in os.listdir(sdir)
                            if f.endswith(".json") and f not in had]
                   if os.path.isdir(sdir) else None, 10)
    return json.load(open(os.path.join(sdir, rec[0])))["sessionId"]


def records():
    sdir = os.path.join(HOME, ".claude", "sessions")
    return set(os.listdir(sdir)) if os.path.isdir(sdir) else set()


def test_shell_claude_exit():
    """a shell running the fake Claude, followed with --schedules only"""
    n = "xfs-sh"
    env, proj = fake_env("sh")
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "sh"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("shell spawned", r.returncode == 0, r.stderr)
    fl = None
    try:
        wait_for(lambda: "$" in screen(n) or "#" in screen(n), 5)
        had = records()
        dch("--send", n, "claude\r", env=env)
        sid = new_record(had)
        pdir = os.path.join(HOME, ".claude", "projects",
                            key(os.path.realpath(proj)))
        os.makedirs(pdir, exist_ok=True)
        tp = os.path.join(pdir, sid + ".jsonl")

        def put(objs, mode="a"):
            with open(tp, mode) as f:
                f.write("".join(json.dumps(o) + "\n" for o in objs))

        put([{"type": "user", "cwd": proj, "timestamp": ts(NOW),
              "message": {"content": "set up the jobs"}}] +
            cron("tu1", "aaaa0001", "check CI"), "w")
        wait_for(lambda: (xray(n, "--json")[1] or {}).get("corpus") ==
                 "claude", 10)
        one = sched_lines(n)
        fl = Follow(n, extra=["--schedules"])
        fl.wait(lambda o: "done" in o)
        got = fl.objs()
        check("--follow takes --schedules (no exit 2): the snapshot, then "
              "the one-shot --schedules lines, block schedules",
              fl.p.poll() is None and "snapshot" in got[0] and
              got[1:] == one and one[0].get("block") == "schedules" and
              one[0]["live"] is True and
              rows(one)["aaaa0001"]["status"] == "pending",
              (fl.p.poll(), got, one))

        time.sleep(0.4)
        k0 = len(fl.got)
        put(cron("tu2", "bbbb0002", "second job"))
        new = fl.wait(lambda o: "schedule" in o and
                      o["schedule"]["id"] == "bbbb0002", frm=k0)
        _, _, _ = None, None, None
        check("a CronCreate appended sends the new row, pending",
              new and new[0]["schedule"]["status"] == "pending" and
              new[0]["schedule"]["prompt"] == "second job", fl.objs(k0))

        time.sleep(0.4)
        k1 = len(fl.got)
        put([use("tu3", "CronDelete", {"id": "bbbb0002"}),
             res("tu3", {"id": "bbbb0002"})])
        d = fl.wait(lambda o: "schedule" in o and
                    o["schedule"]["id"] == "bbbb0002" and
                    o["schedule"]["status"] == "deleted", frm=k1)
        time.sleep(0.4)
        _, _, _ = None, None, None
        st = {}
        for blk in sched_blocks(fl.objs()):
            st = rows(blk)
        for o in fl.objs():
            if "schedule" in o:
                st[o["schedule"]["id"]] = o["schedule"]
        now1 = sched_lines(n)
        check("a CronDelete sends the row, deleted; the stream's rows are "
              "the one-shot's now", d and rows(now1) == st, (fl.objs(k1),
                                                             now1))

        time.sleep(0.4)
        k2 = len(fl.got)
        t0 = time.time()
        dch("--send", n, "/exit\r", env=env)
        b = wait_for(lambda: [x for x in sched_blocks(fl.objs(k2)) if x], 15)
        secs = time.time() - t0
        ob = fl.objs(k2)
        blk = b[0] if b else None
        check("the Claude's exit: reset rotated, the shell's snapshot, then "
              "the schedules block whole, live false, the pending row ended "
              "and the deleted one kept (%.1f s)" % secs,
              blk and is_reset("rotated")(ob[0]) and "snapshot" in ob[1] and
              ob[2] == blk[0] and blk[0]["live"] is False and
              blk[0]["source"] == "claude" and blk[0]["session_id"] == sid and
              rows(blk)["aaaa0001"]["status"] == "ended" and
              rows(blk)["aaaa0001"]["next"] is None and
              rows(blk)["bbbb0002"]["status"] == "deleted" and
              blk[-1] == {"done": True, "count": 2, "dropped": 0}, ob)
    finally:
        dch("-k", n)
        if fl:
            wait_for(lambda: fl.p.poll() is not None, 5)
            rc = fl.stop()
            check("shell: the session's end exits 0", rc == 0, rc)


def test_all_blocks():
    """--tasks --memory --schedules on a Claude session; its end"""
    n = "xfs-all"
    env, proj = fake_env("all")
    had = records()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("all: claude spawned", r.returncode == 0, r.stderr)
    fl = None
    try:
        sid = new_record(had)
        pdir = os.path.join(HOME, ".claude", "projects", key(proj))
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
            f.write("".join(json.dumps(o) + "\n" for o in
                            [{"type": "user", "cwd": proj,
                              "timestamp": ts(NOW),
                              "message": {"content": "jobs"}}] +
                            cron("tu1", "cccc0003", "nightly")))
        wait_for(lambda: (xray(n, "--json")[1] or {}).get("corpus") ==
                 "claude", 10)
        fl = Follow(n, extra=["--tasks", "--memory", "--schedules"])
        fl.wait(lambda o: "done" in o and
                sum("done" in x for x in fl.objs()) >= 3)
        time.sleep(0.5)
        got = fl.objs()
        heads = [o.get("block") for o in got if "block" in o]
        check("all: the snapshot, then tasks, memory and schedules, each "
              "block once, in that order", fl.p.poll() is None and
              "snapshot" in got[0] and
              heads == ["tasks", "memory", "schedules"] and
              sum("done" in o for o in got) == 3 and
              rows(sched_blocks(got)[0])["cccc0003"]["status"] == "pending",
              got)
        hdr, tk, mem = blocks(got)
        check("all: the tasks/memory reader of #446 still reads its two "
              "blocks", "task" in hdr and "memory" in hdr, hdr)
        k0 = len(fl.got)
        dch("-k", n)
        n = None
        wait_for(lambda: fl.p.poll() is not None, 10)
        rc = fl.stop()
        b = sched_blocks(fl.objs(k0))
        check("all: the session's end sends the schedules block once more, "
              "live false, the row ended, then exit 0",
              rc == 0 and len(b) == 1 and b[0] and
              b[0][0]["live"] is False and
              rows(b[0])["cccc0003"]["status"] == "ended" and
              fl.objs()[-1] == b[0][-1], (rc, fl.objs(k0)))
        fl = None
    finally:
        if n:
            dch("-k", n)
        if fl:
            fl.stop()


def main():
    print("xray_follow_schedules_test: %s" % DCH)
    try:
        test_shell_claude_exit()
        test_all_blocks()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
