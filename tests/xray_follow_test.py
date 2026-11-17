#!/usr/bin/env python3
"""`dch --xray <n> --follow` and the primary screen (#140): follow through a
live Claude, late and stale refs, agents, the screen read with the ring, the
seam and a resize.

Split from xray_cli_test.py; helpers in tests/xray_cli_lib.py.
Run:  python3 tests/xray_follow_test.py          (uses ./dch; run.sh isolates)
"""
import json, os, random, re, shutil, socket, stat, struct, subprocess, sys
import tempfile, threading, time

from xray_cli_lib import *  # noqa: E402,F401,F403
from xray_cli_lib import X  # noqa: E402,F401


# ---- --follow (#136, XR2b; spec 3.3, 9.5's tail row) ---------------------

class Follow:
    """`dch --xray <n> --follow --json`, its lines read off as they come,
    each with its arrival time."""

    def __init__(self, n, env=None, extra=()):
        self.p = subprocess.Popen([DCH, "--xray", n, "--follow", "--json"] +
                                  list(extra), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env or ENV)
        self.got = []
        self.th = threading.Thread(target=self.run, daemon=True)
        self.th.start()

    def run(self):
        for raw in self.p.stdout:
            try:
                o = json.loads(raw)
            except ValueError:
                o = {"bad": raw.decode(errors="replace")}
            self.got.append((time.time(), o))

    def objs(self, frm=0):
        return [o for _, o in self.got[frm:]]

    def kinds(self, frm=0):
        return [next((k for k in o if k != "schema"), "?")
                for o in self.objs(frm)]

    def wait(self, pred, secs=10.0, frm=0):
        return wait_for(lambda: [o for o in self.objs(frm) if pred(o)], secs)

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(5)
        self.th.join(5)
        return self.p.returncode


def is_ref(key, **kv):
    return lambda o: "ref" in o and o["ref"]["key"] == key and \
        all(o["ref"].get(k) == v for k, v in kv.items())


def is_reset(why):
    return lambda o: o.get("reset") == {"why": why}


def trace_from(path, off):
    with open(path) as f:
        f.seek(off)
        return f.read()


def test_follow_claude():
    n = "xc-fl"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "fproj")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("follow: claude spawned", r.returncode == 0, r.stderr)
    trace = os.path.join(T, "trace-follow")
    open(trace, "w").close()
    dbg = dict(ENV, DCH_DEBUG=trace)
    fl = None
    try:
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        rp = os.path.join(sdir, rec[0])
        sid = json.load(open(rp))["sessionId"]
        pdir = os.path.join(HOME, ".claude", "projects", key(proj))
        os.makedirs(pdir, exist_ok=True)
        tp = os.path.join(pdir, sid + ".jsonl")

        def put(objs, mode="a"):
            with open(tp, mode) as f:
                f.write("".join(json.dumps(o) + "\n" for o in objs))

        put([{"type": "user", "cwd": proj, "message": {
                "content": "first ask #11"}},
             {"type": "assistant", "message": {"content": [
                 {"type": "text", "text": "ok"}]}}], "w")
        wait_for(lambda: (xray(n, "--json")[1] or {}).get("corpus") ==
                 "claude", 10)
        _, one, _ = xray(n, "--json")
        fl = Follow(n, dbg)
        snap = fl.wait(lambda o: "snapshot" in o)
        check("follow: the first line is a snapshot, the --json object",
              snap and fl.kinds()[0] == "snapshot" and
              snap[0]["snapshot"] == one, (fl.objs(), one))

        # mutant 34: idle, no timer and no wake (9.5: a counting seam)
        time.sleep(0.2)
        off = os.path.getsize(trace)
        time.sleep(1.0)
        idle = trace_from(trace, off)
        check("follow: a small transcript gets one snapshot, no reset "
              "(#447: no fast pass under 32 MB)",
              fl.kinds().count("snapshot") == 1 and
              "reset" not in fl.kinds(), fl.kinds())
        check("follow: idle, no deadline armed and no wake (no timer)",
              "xray follow: deadline" not in idle and
              "xray follow: woke" not in idle and fl.p.poll() is None, idle)

        # a tool_use, then its tool_result in a later read (QA A2)
        k0 = len(fl.got)
        put([{"type": "user", "message": {"content": "second ask #12"}},
             {"type": "assistant", "message": {"content": [
                 {"type": "tool_use", "id": "t1", "name": "Bash",
                  "input": {"command": "gh issue view 12"}}]}}])
        a = fl.wait(is_ref("issue:#12"), frm=k0)
        ch = fl.wait(lambda o: "chapter" in o, frm=k0)
        check("follow: appended lines give a chapter and a ref delta",
              a and not a[0]["ref"]["defined"] and ch and
              ch[0]["chapter"]["title"] == "second ask #12",
              fl.objs(k0))
        time.sleep(0.4)
        k1 = len(fl.got)
        put([{"type": "user", "toolUseResult": {"stdout": "# #12 Twelve"},
              "message": {"content": [{"type": "tool_result",
                                       "tool_use_id": "t1",
                                       "content": "# #12 Twelve"}]}}])
        d = fl.wait(is_ref("issue:#12", defined=True), frm=k1)
        check("follow: a tool_result in the read after its tool_use pairs "
              "(the tool_use ring survives)", d and
              d[0]["ref"]["title"] == "Twelve", fl.objs(k1))
        time.sleep(0.4)
        _, now, _ = xray(n, "--json")
        state = {"chapters": {}, "refs": {}}
        for o in fl.objs():
            if "snapshot" in o:
                state = {"chapters": {c["anchor"]: c for c in
                                      o["snapshot"]["chapters"]},
                         "refs": {x["key"]: x for x in
                                  o["snapshot"]["refs"]}}
            elif "chapter" in o:
                state["chapters"][o["chapter"]["anchor"]] = o["chapter"]
            elif "ref" in o:
                state["refs"][o["ref"]["key"]] = o["ref"]
        strip = lambda c: {k: v for k, v in c.items() if k != "i"}
        check("follow: the snapshot plus its deltas is the --json of now",
              now and sorted(state["refs"].values(), key=str) ==
              sorted(now["refs"], key=str) and
              [strip(c) for c in sorted(state["chapters"].values(),
                                        key=lambda c: c["offset"])] ==
              [strip(c) for c in now["chapters"]], (state, now))

        # coalescing: lines 30 ms apart, at most one batch per 250 ms (the
        # trace's batch times)
        k2 = len(fl.got)
        off = os.path.getsize(trace)
        for i in range(12):
            put([{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "see #%d" % (200 + i)}]}}])
            time.sleep(0.03)
        fl.wait(is_ref("issue:#211"), frm=k2)
        ts = [float(l.split()[0]) for l in trace_from(trace, off).splitlines()
              if "xray follow: batch" in l]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        nref = sum("ref" in o for o in fl.objs(k2))
        check("follow: deltas coalesce, batches >= 250 ms apart (%d refs in "
              "%d batches, gaps %s)" % (nref, len(ts),
                                        ["%.3f" % g for g in gaps]),
              # the count depends on how fast the lines land (a loaded
              # runner spreads them): the gap is the rule
              nref == 12 and 1 < len(ts) <= nref and
              all(g >= 0.245 for g in gaps), ts)

        # shrink: cut back to the first line
        time.sleep(0.4)
        k3 = len(fl.got)
        first = len(json.dumps({"type": "user", "cwd": proj, "message": {
            "content": "first ask #11"}})) + 1
        os.truncate(tp, first)
        s = fl.wait(lambda o: "snapshot" in o, frm=k3)
        check("follow: a shrunk transcript sends reset shrunk, then a "
              "snapshot", s and fl.kinds(k3)[:2] == ["reset", "snapshot"] and
              fl.objs(k3)[0] == {"schema": 1, "reset": {"why": "shrunk"}} and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["first ask #11"], fl.objs(k3))

        # mutant 37: rotated (a /clear: a new id, a new file)
        time.sleep(0.4)
        k4 = len(fl.got)
        sid2 = "22222222-3333-4444-8555-66666666666b"
        r0 = json.load(open(rp))
        r0["sessionId"] = sid2
        with open(rp + ".tmp", "w") as f:
            json.dump(r0, f)
        os.rename(rp + ".tmp", rp)
        with open(os.path.join(pdir, sid2 + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                "content": "rotated ask #13"}}) + "\n")
        s = fl.wait(lambda o: "snapshot" in o, frm=k4)
        check("follow: a rotated transcript sends reset rotated, then the "
              "new session_id's snapshot", s and
              fl.objs(k4)[0] == {"schema": 1, "reset": {"why": "rotated"}} and
              s[0]["snapshot"]["session_id"] == sid2 and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["rotated ask #13"], fl.objs(k4))
        _, one, _ = xray(n, "--json")
        check("follow: the rotated snapshot is the --json object",
              s and s[0]["snapshot"] == one, (s, one))

        # the same id at a new inode (the file replaced): rotated too
        time.sleep(0.4)
        k4 = len(fl.got)
        tp2 = os.path.join(pdir, sid2 + ".jsonl")
        with open(tp2 + ".new", "w") as f:
            f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                "content": "replaced ask #15"}}) + "\n")
        os.rename(tp2 + ".new", tp2)
        s = fl.wait(lambda o: "snapshot" in o, frm=k4)
        check("follow: a transcript replaced under the same id (a new "
              "inode) sends reset rotated, then its snapshot", s and
              fl.objs(k4)[0] == {"schema": 1, "reset": {"why": "rotated"}} and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["replaced ask #15"], fl.objs(k4))

        # QA 1.52.0 LOW-6: a /clear whose new transcript appears before the
        # record names it, the record then rewritten in place (no dir event)
        time.sleep(0.4)
        k4 = len(fl.got)
        sid3 = "33333333-4444-4555-8666-77777777777c"
        with open(os.path.join(pdir, sid3 + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                "content": "cleared ask #16"}}) + "\n")
        time.sleep(0.5)
        early = fl.objs(k4)
        r0 = json.load(open(rp))
        r0["sessionId"] = sid3
        with open(rp, "w") as f:
            f.write(json.dumps(r0))
        s = fl.wait(lambda o: "snapshot" in o, frm=k4)
        check("follow: a new transcript before its record, the record "
              "rewritten in place, sends reset rotated, then its snapshot",
              not early and s and
              fl.objs(k4)[0] == {"schema": 1, "reset": {"why": "rotated"}} and
              s[0]["snapshot"]["session_id"] == sid3 and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["cleared ask #16"], (early, fl.objs(k4)))

        # the reader leaves: exit 0 at once, idle
        rd, wr = os.pipe()
        p = subprocess.Popen([DCH, "--xray", n, "--follow", "--json"],
                             stdout=wr, stderr=subprocess.PIPE, env=ENV)
        os.close(wr)
        line = b""
        while not line.endswith(b"\n"):
            b = os.read(rd, 65536)
            if not b:
                break
            line += b
        t0 = time.time()
        os.close(rd)
        try:
            rc = p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait()
        check("follow: the reader's close ends an idle follower, exit 0 "
              "(%.2fs)" % (time.time() - t0), rc == 0 and
              time.time() - t0 < 1.0 and not p.stderr.read(), rc)

        # nothing written by a follow (9.5)
        before = files(T)
        k5 = len(fl.got)
        put([{"type": "user", "message": {"content": "late #14"}}])
        fl.wait(is_ref("issue:#14"), frm=k5)
        tr = open(trace).read()
        new = sorted(set(files(T)) - set(before))
        check("follow: nothing written, no passage in the trace",
              not [p for p in new if p != trace] and
              "late #14" not in tr and "rotated ask" not in tr, new)

        # the session ends: exit 0
        dch("-k", n)
        try:
            rc = fl.p.wait(5)
        except subprocess.TimeoutExpired:
            rc = None
        check("follow: the session's end ends it, exit 0", rc == 0, rc)
        tr = open(trace).read()
        m = re.search(r"xray follow: end rc=(-?\d+) woke=(\d+) "
                      r"deadlines=(\d+)", tr)
        check("follow: the end trace counts its wakes and deadlines",
              m and m.group(1) == "0", tr[-400:])
    finally:
        if fl:
            fl.stop()
        dch("-k", n)


def lines(*a, env=None):
    r = dch("--xray", *a, env=env)
    return r.returncode, [json.loads(l) for l in r.stdout.splitlines()], \
        r.stderr


def blocks(objs):
    """A --follow --tasks --memory stream's state: the blocks after each
    snapshot, then the task and memory deltas applied by id (#446)."""
    tk, mem, cur, hdr = {}, {}, None, {}
    for o in objs:
        if "snapshot" in o or "reset" in o:
            cur = None
        elif "schema" in o and "source" in o:
            cur = "task" if "session" in o else "memory"
            hdr[cur] = o
            (tk if cur == "task" else mem).clear()
        elif "done" in o:
            cur = None
        elif "task" in o or "memory" in o:
            k = "task" if "task" in o else "memory"
            d = tk if k == "task" else mem
            if o[k].get("gone"):
                d.pop(o[k]["id"], None)
            else:
                d[o[k]["id"]] = o[k]
    return hdr, tk, mem


def test_follow_tasks_memory():
    """--follow --tasks --memory (#446): after the snapshot the one-shot
    --tasks and --memory blocks, then a task's or a memory's whole row when
    it moves, a gone line for a memory removed; a reset sends all again; a
    tasks_command's answer is a whole block again when it moves."""
    n = "xc-ftm"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "fproj-tm")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("tasks+memory: claude spawned", r.returncode == 0, r.stderr)
    fl = cf = None
    use = lambda i, name, inp: {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": i, "name": name, "input": inp}]}}
    made = lambda i, tid: {"type": "user",
                           "toolUseResult": {"task": {"id": tid}},
                           "message": {"content": [{
                               "type": "tool_result", "tool_use_id": i,
                               "content": "ok"}]}}
    try:
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
        pdir = os.path.join(HOME, ".claude", "projects", key(proj))
        mdir = os.path.join(pdir, "memory")
        os.makedirs(mdir, exist_ok=True)
        with open(os.path.join(mdir, "a.md"), "w") as f:
            f.write("---\nname: Alpha\ndescription: the first\n---\nbody a\n")
        tp = os.path.join(pdir, sid + ".jsonl")

        def put(objs, mode="a"):
            with open(tp, mode) as f:
                f.write("".join(json.dumps(o) + "\n" for o in objs))

        first = [{"type": "user", "cwd": proj,
                  "message": {"content": "plan it #31"}},
                 use("u1", "TaskCreate", {"subject": "First task",
                                          "description": ""}),
                 made("u1", "1")]
        put(first, "w")
        wait_for(lambda: (xray(n, "--json")[1] or {}).get("corpus") ==
                 "claude", 10)
        _, one, _ = xray(n, "--json")
        _, tk1, _ = lines(n, "--tasks", "--json")
        _, mm1, _ = lines(n, "--memory", "--json")
        fl = Follow(n, extra=["--tasks", "--memory"])
        fl.wait(lambda o: "done" in o and
                sum("done" in x for x in fl.objs()) >= 2)
        got = fl.objs()
        check("tasks+memory: --follow takes --tasks and --memory (no "
              "exit 2); the snapshot, then the one-shot --tasks lines, "
              "then the one-shot --memory lines", fl.p.poll() is None and
              got[:1] == [{"schema": 1, "snapshot": one}] and
              got[1:1 + len(tk1)] == tk1 and
              got[1 + len(tk1):1 + len(tk1) + len(mm1)] == mm1 and
              len(got) == 1 + len(tk1) + len(mm1) and
              [t["task"]["id"] for t in tk1 if "task" in t] == ["1"] and
              [m["memory"]["id"] for m in mm1 if "memory" in m] == ["a"],
              (got, tk1, mm1))

        # a TaskUpdate and a TaskCreate: each task's whole row
        time.sleep(0.4)
        k0 = len(fl.got)
        put([{"type": "user", "message": {"content": "go on #32"}},
             use("u2", "TaskUpdate", {"taskId": "1",
                                      "status": "in_progress"}),
             use("u3", "TaskCreate", {"subject": "Second task",
                                      "description": ""}),
             made("u3", "2")])
        fl.wait(lambda o: "task" in o and o["task"]["id"] == "2", frm=k0)
        fl.wait(lambda o: "task" in o and o["task"]["id"] == "1" and
                o["task"]["label"] == "in_progress", frm=k0)
        time.sleep(0.4)
        _, tk2, _ = lines(n, "--tasks", "--json")
        rows = [o for o in fl.objs(k0) if "task" in o]
        hdr, st, sm = blocks(fl.objs())
        check("tasks+memory: a task delta is the task's whole row, keyed "
              "by id, and the stream's tasks are the one-shot's now",
              rows and all(o in tk2 for o in rows) and
              sorted(st) == ["1", "2"] and
              [st[t["task"]["id"]] for t in tk2 if "task" in t] ==
              [t["task"] for t in tk2 if "task" in t] and
              not [o for o in fl.objs(k0) if "memory" in o or
                   "source" in o], (fl.objs(k0), tk2))

        # a memory added, then one read (its session and chapter), then
        # one removed: gone
        time.sleep(0.4)
        k1 = len(fl.got)
        with open(os.path.join(mdir, "b.md"), "w") as f:
            f.write("body b\n")
        # inotify reports the create before the write lands: the row may
        # come first with an empty body, then whole (close-write, #449)
        b = fl.wait(lambda o: "memory" in o and o["memory"]["id"] == "b" and
                    o["memory"].get("body") == "body b\n", frm=k1)
        check("tasks+memory: a memory file added sends its row",
              b and b[0]["memory"]["body"] == "body b\n" and
              not b[0]["memory"].get("gone"), fl.objs(k1))
        time.sleep(0.4)
        k2 = len(fl.got)
        put([use("r1", "Read", {"file_path": os.path.join(mdir, "a.md")})])
        a = fl.wait(lambda o: "memory" in o and o["memory"]["id"] == "a" and
                    o["memory"]["session"] == "loaded", frm=k2)
        _, mm2, _ = lines(n, "--memory", "--json")
        check("tasks+memory: a Read of a memory sends its row, loaded, as "
              "the one-shot has it", a and a[-1] in mm2, (fl.objs(k2), mm2))
        time.sleep(0.4)
        k3 = len(fl.got)
        os.unlink(os.path.join(mdir, "b.md"))
        g = fl.wait(lambda o: "memory" in o and o["memory"].get("gone"),
                    frm=k3)
        check("tasks+memory: a memory removed sends a gone line",
              g and g[0] == {"memory": {"id": "b", "gone": True}},
              fl.objs(k3))
        time.sleep(0.4)
        _, mm3, _ = lines(n, "--memory", "--json")
        hdr, st, sm = blocks(fl.objs())
        check("tasks+memory: the stream's memories are the one-shot's now",
              [sm.get(m["memory"]["id"]) for m in mm3 if "memory" in m] ==
              [m["memory"] for m in mm3 if "memory" in m] and
              len(sm) == len([m for m in mm3 if "memory" in m]),
              (sm, mm3))

        # TodoWrite takes over (the kind used last): rows replaced by
        # position, one left out is gone; an emptied list leaves an empty
        # block (no row to diff against is no failure)
        time.sleep(0.4)
        k5 = len(fl.got)
        put([use("w1", "TodoWrite", {"todos": [{
            "content": "Only todo", "status": "pending",
            "activeForm": "Doing"}]})])
        fl.wait(lambda o: "task" in o and o["task"].get("gone"), frm=k5)
        time.sleep(0.4)
        tw = [o["task"] for o in fl.objs(k5) if "task" in o]
        k6 = len(fl.got)
        put([use("w2", "TodoWrite", {"todos": []})])
        fl.wait(lambda o: "task" in o and o["task"].get("gone"), frm=k6)
        time.sleep(0.4)
        _, tk6, _ = lines(n, "--tasks", "--json")
        check("tasks+memory: a TodoWrite list replaces the rows (a row left "
              "out: gone), an emptied one sends a gone line per row, and "
              "the stream goes on",
              fl.p.poll() is None and
              sorted((t["id"], t.get("gone", False), t.get("title"))
                     for t in tw) ==
              [("1", False, "Only todo"), ("2", True, None)] and
              [o for o in fl.objs(k6) if "task" in o] ==
              [{"task": {"id": "1", "gone": True}}] and
              not [t for t in tk6 if "task" in t] and
              blocks(fl.objs())[1] == {}, (tw, fl.objs(k6), tk6))

        # a reset: the snapshot and both blocks again, whole
        time.sleep(0.4)
        k4 = len(fl.got)
        put(first, "w")
        fl.wait(lambda o: "done" in o and
                sum("done" in x for x in fl.objs(k4)) >= 2, frm=k4)
        _, tk4, _ = lines(n, "--tasks", "--json")
        _, mm4, _ = lines(n, "--memory", "--json")
        got = fl.objs(k4)
        check("tasks+memory: a reset sends the snapshot, then both blocks "
              "whole again", fl.kinds(k4)[:2] == ["reset", "snapshot"] and
              got[2:] == tk4 + mm4, (got, tk4, mm4))

        # #449: a file created empty, its content 0.3 s later (the dir sees
        # no second write): its row with the content within 2 s
        time.sleep(0.4)
        k7 = len(fl.got)
        cp = os.path.join(mdir, "c.md")
        open(cp, "w").close()
        time.sleep(0.3)
        with open(cp, "w") as f:
            f.write("---\nname: Gamma\ndescription: written late\n---\n"
                    "body c\n")
        t0 = time.time()
        c = fl.wait(lambda o: "memory" in o and o["memory"]["id"] == "c" and
                    "body c" in o["memory"].get("body", ""), 2.5, frm=k7)
        check("tasks+memory: a memory written 0.3 s after its create sends "
              "its content within 2 s", c and time.time() - t0 < 2.2,
              fl.objs(k7))

        # the one-shot forms keep their exclusions
        r = dch("--xray", n, "--tasks", "--memory", "--json")
        check("tasks+memory: without --follow, --tasks and --memory still "
              "exclude each other (exit 2)", r.returncode == 2 and
              r.stderr == "dch: bad usage: --memory and --tasks\n",
              (r.returncode, r.stderr))

        # a tasks_command: its answer whole, again when it moves (5 s)
        cfg = os.path.join(T, "config-tm")
        rows = os.path.join(T, "tm-rows")
        sh = os.path.join(T, "tm-cmd.sh")
        with open(sh, "w") as f:
            f.write("#!/bin/sh\nexec cat %s\n" % rows)
        os.chmod(sh, 0o755)
        with open(cfg, "w") as f:
            f.write("tasks_command = %s\n" % sh)
        with open(rows, "w") as f:
            f.write(json.dumps({"id": "c1", "title": "From the command"}) +
                    "\n")
        cenv = dict(ENV, DCH_CONFIG=cfg)
        _, tc1, _ = lines(n, "--tasks", "--json", env=cenv)
        cf = Follow(n, cenv, ["--tasks"])
        cf.wait(lambda o: "done" in o)
        check("tasks+memory: a tasks_command's answer is the tasks block",
              cf.objs()[1:] == tc1 and tc1[0]["source"] == "command",
              (cf.objs(), tc1))
        k5 = len(cf.got)
        with open(rows, "w") as f:
            f.write(json.dumps({"id": "c2", "title": "Moved"}) + "\n")
        cf.wait(lambda o: "done" in o, 9, frm=k5)
        _, tc2, _ = lines(n, "--tasks", "--json", env=cenv)
        check("tasks+memory: the command's moved answer comes whole again",
              cf.objs(k5) == tc2, (cf.objs(k5), tc2))
    finally:
        for x in (fl, cf):
            if x:
                x.stop()
        dch("-k", n)


def test_follow_fast():
    """A fast first paint (#447): past 32 MB, the newest 32 MB's snapshot
    (why range, the tail's chapters only) and its blocks first, then reset
    full and the one-shot's snapshot and blocks; a line written meanwhile
    comes after it. Under 32 MB the one snapshot stays (test_follow_claude,
    the small-transcript row)."""
    n = "xc-ff"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "fproj-ff")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("fast: claude spawned", r.returncode == 0, r.stderr)
    fl = tp = None
    jl = lambda objs: "".join(json.dumps(o) + "\n" for o in objs)
    task = lambda i, subj: [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": i, "name": "TaskCreate",
             "input": {"subject": subj, "description": ""}}]}},
        {"type": "user", "toolUseResult": {"task": {"id": i}},
         "message": {"content": [{"type": "tool_result", "tool_use_id": i,
                                  "content": "ok"}]}}]
    # a turn of tool calls and results (8 KB each, stored twice), about
    # 165 KB: the full read costs what a real transcript's does
    big = "x" * 8192
    turn = [{"type": "user", "message": {"content": "turn TURN"}}]
    for c in range(10):
        turn += [{"type": "assistant", "message": {"content": [
                     {"type": "tool_use", "id": "TURN-%d" % c, "name": "Bash",
                      "input": {"command": "make %d" % c}}]}},
                 {"type": "user", "toolUseResult": {"stdout": big},
                  "message": {"content": [{
                      "type": "tool_result", "tool_use_id": "TURN-%d" % c,
                      "content": big}]}}]
    turn = jl(turn)
    try:
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
        pdir = os.path.join(HOME, ".claude", "projects", key(proj))
        mdir = os.path.join(pdir, "memory")
        os.makedirs(mdir, exist_ok=True)
        with open(os.path.join(mdir, "a.md"), "w") as f:
            f.write("---\nname: Alpha\ndescription: the first\n---\nbody\n")
        tp = os.path.join(pdir, sid + ".jsonl")
        # 250 MB, under the 272 MB range (the one-shot reads it all): the
        # old ask and task at the head, the new at the tail
        with open(tp + ".tmp", "w") as f:
            f.write(jl([{"type": "user", "cwd": proj,
                         "message": {"content": "old ask #71"}}] +
                       task("o1", "Old task")))
            for i in range((250 << 20) // len(turn)):
                f.write(turn.replace("TURN", str(i)))
            f.write(jl([{"type": "user",
                         "message": {"content": "new ask #72"}}] +
                       task("n1", "New task")))
        os.rename(tp + ".tmp", tp)
        size = os.path.getsize(tp)
        wait_for(lambda: (xray(n, "--json")[1] or {}).get("corpus") ==
                 "claude", 20)
        _, one, _ = xray(n, "--json")
        t0 = time.time()
        fl = Follow(n)
        fl.wait(lambda o: True, 10)
        first = fl.got[0][0] - t0 if fl.got else 99
        # a line written while the full read runs
        with open(tp, "a") as f:
            f.write(jl([{"type": "user",
                         "message": {"content": "later ask #73"}}]))
        wrote = time.time()
        fl.wait(lambda o: "ref" in o and o["ref"]["key"] == "issue:#73", 20)
        ks = fl.kinds()
        objs = fl.objs()
        f0 = (objs[0] if objs else {}).get("snapshot") or {}
        offs = [c["offset"] for c in f0.get("chapters", [])]
        keys0 = [x["key"] for x in f0.get("refs", [])]
        check("fast: past 32 MB the first line is a snapshot of the newest "
              "32 MB, complete false, why range, the tail's chapters only",
              ks[:1] == ["snapshot"] and f0.get("complete") is False and
              f0.get("why") == "range" and offs and
              min(offs) >= size - (32 << 20) and "issue:#72" in keys0 and
              "issue:#71" not in keys0, (ks[:6], f0.get("why"), keys0))
        # a wall-clock bound, skipped under sanitizers (1.7 s on CI's
        # ASan runner); the window rows above hold there too
        check("fast: the first line came in under 0.5 s (%.3f s)" % first,
              SANITIZED or first < 0.5, first)
        check("fast: then reset full and the one-shot --json snapshot, "
              "exactly one pair", ks[:3] == ["snapshot", "reset", "snapshot"]
              and objs[1] == {"schema": 1, "reset": {"why": "full"}} and
              ks.count("snapshot") == 2 and ks.count("reset") == 1 and
              objs[2].get("snapshot") == one, (ks[:12],))
        check("fast: a line written during the full read comes after it, "
              "as a delta", len(fl.got) > 2 and fl.got[2][0] > wrote and
              [o for o in objs[3:] if "ref" in o and
               o["ref"]["key"] == "issue:#73"],
              (ks, fl.got[2][0] - wrote if len(fl.got) > 2 else None))
        fl.stop()

        # --tasks --memory: each snapshot followed by both blocks
        _, one, _ = xray(n, "--json")
        _, tk1, _ = lines(n, "--tasks", "--json")
        _, mm1, _ = lines(n, "--memory", "--json")
        fl = Follow(n, extra=["--tasks", "--memory"])
        fl.wait(lambda o: "done" in o and
                sum("done" in x for x in fl.objs()) >= 4, 20)
        ks = fl.kinds()
        objs = fl.objs()
        snaps = [i for i, k in enumerate(ks) if k == "snapshot"]
        s2 = snaps[1] if len(snaps) > 1 else len(objs)
        hdr0, tk0, _ = blocks(objs[:s2])
        check("fast: with --tasks --memory, the fast snapshot's blocks "
              "follow it (the window's tasks: the new, not the old)",
              len(snaps) == 2 and ks[1] == "session" and
              sorted(hdr0) == ["memory", "task"] and
              [t["title"] for t in tk0.values()] == ["New task"],
              (ks[:8], tk0))
        nb = len(tk1) + len(mm1)
        check("fast: then reset full, the one-shot snapshot and both "
              "blocks again, the one-shot --tasks and --memory",
              objs[s2 - 1:s2] == [{"schema": 1, "reset": {"why": "full"}}]
              and s2 < len(objs) and objs[s2]["snapshot"] == one and
              objs[s2 + 1:s2 + 1 + nb] == tk1 + mm1,
              (ks, tk1, mm1))
    finally:
        if fl:
            fl.stop()
        dch("-k", n)
        if tp and os.path.exists(tp):
            os.unlink(tp)


def test_follow_late():
    """QA 1.52.0: a project dir made after the follow started is watched
    (LOW-6); time blocked on a stalled reader is no build time (LOW-8)."""
    n = "xc-fl2"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "fproj-late")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("follow late: claude spawned", r.returncode == 0, r.stderr)
    trace = os.path.join(T, "trace-follow-late")
    open(trace, "w").close()
    dbg = dict(ENV, DCH_DEBUG=trace)
    fl = p = None
    try:
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
        # the dir dch watches: the key of Claude's cwd, as its record has it
        pdir = os.path.join(HOME, ".claude", "projects",
                            key(os.path.realpath(proj)))
        tp = os.path.join(pdir, sid + ".jsonl")

        def put(objs, mode="a"):
            with open(tp, mode) as f:
                f.write("".join(json.dumps(o) + "\n" for o in objs))

        # LOW-6: no project dir yet; it is made, its first file 0.5 s later
        fl = Follow(n, dbg)
        snap = fl.wait(lambda o: "snapshot" in o)
        check("follow late: no transcript, no project dir: a corpus none "
              "snapshot", snap and snap[0]["snapshot"]["corpus"] == "none" and
              not os.path.isdir(pdir), fl.objs())
        k0 = len(fl.got)
        os.makedirs(pdir)
        time.sleep(0.5)
        put([{"type": "user", "cwd": proj, "message": {
            "content": "late dir ask #21"}}], "w")
        s = fl.wait(lambda o: "snapshot" in o, frm=k0)
        check("follow late: a file in a project dir made after the follow "
              "started sends reset rotated, then its snapshot", s and
              fl.objs(k0)[0] == {"schema": 1, "reset": {"why": "rotated"}} and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["late dir ask #21"], fl.objs(k0))
        fl.stop()

        # LOW-8: a batch stalled 3 s on a full pipe, then a small one: its
        # deadline is the 250 ms spacing, not twice the stall
        p = subprocess.Popen([DCH, "--xray", n, "--follow", "--json"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=dbg)
        first = p.stdout.readline()
        # 1200 refs (120 a line: under the 128 spans of a line), 1120 ref
        # lines: issues, pulls and tickets, each kind in its room (#262)
        put([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": " ".join(
                ("#%d" if j < 4 else "o/r#%d" if j < 8 else "PROJ-%d") %
                (1000 + 120 * j + i) for i in range(120))}]}}
             for j in range(10)])
        time.sleep(3.0)
        got = []
        th = threading.Thread(target=lambda: got.extend(p.stdout),
                              daemon=True)
        th.start()
        wait_for(lambda: '"ticket:PROJ-2199"'.encode() in b"".join(got), 15)
        wait_for(lambda: "xray follow: batch" in open(trace).read(), 5)
        off = os.path.getsize(trace)
        put([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "see #99"}]}}])
        wait_for(lambda: b'"issue:#99"' in b"".join(got), 15)
        dl = [int(m) for m in re.findall(r"xray follow: deadline (\d+) ms",
                                         trace_from(trace, off))]
        check("follow late: after a 3 s stall on the reader, the next "
              "deadline is the batch spacing (%s ms, < 1500)" % dl,
              b'"snapshot"' in first and len(got) > 1000 and
              b'"issue:#99"' in b"".join(got) and
              all(d < 1500 for d in dl), (len(got), dl))
    finally:
        if fl:
            fl.stop()
        if p and p.poll() is None:
            p.terminate()
            p.wait(5)
        dch("-k", n)


def test_follow_stale():
    """QA 1.52.0 LOW-7 (Prosper's ruling A): a shell follower that leaves a
    wake unanswered 3 s loses its slot to a fifth; when it reads again and no
    slot is free it exits 1 with `dch: busy` and writes nothing more."""
    import signal
    n = "xc-fst"
    with open(ENV["DCH_CONFIG"], "w") as f:
        f.write("xray_ring = 64K\n")
    prog = ("while :; do printf '\\033]133;A\\007$ \\033]133;B\\007'; "
            "read -r cmd || exit; printf '\\033]133;C\\007'; eval \"$cmd\"; "
            "printf '\\033]133;D;0\\007'; done")
    cwd = os.path.join(T, "fst proj")
    os.makedirs(cwd, exist_ok=True)
    fls, stopped = [], None
    try:
        r = dch("--spawn", n, "--size", "80x24", "--cwd", cwd, "sh", "-c",
                prog)
        check("follow stale: spawned", r.returncode == 0, r.stderr)
        _, o, _ = xray(n, "--json")
        if (o or {}).get("reason") == "lite":
            # dch-lite keeps no ring: no wake, so nothing to leave unanswered
            check("follow stale: lite, corpus none (no ring to subscribe)",
                  o["corpus"] == "none", o)
            return
        wait_for(lambda: "$" in screen(n), 5)
        dch("--send", n, "echo 'see o/r#51'\n")
        wait_for(lambda: "see o/r#51" in screen(n), 5)
        for _ in range(4):
            fls.append(Follow(n))
        ok = all(f.wait(lambda o: "snapshot" in o) for f in fls)
        time.sleep(0.5)
        stale = fls[3]
        k = len(stale.got)
        os.kill(stale.p.pid, signal.SIGSTOP)	# our own child: a paused reader
        stopped = stale
        dch("--send", n, "echo 'more o/r#52'\n")	# a wake it leaves unanswered
        time.sleep(3.5)
        fifth = Follow(n)
        fls.append(fifth)
        took = fifth.wait(lambda o: "snapshot" in o)
        os.kill(stale.p.pid, signal.SIGCONT)
        stopped = None
        try:
            rc = stale.p.wait(10)
        except subprocess.TimeoutExpired:
            rc = None
        stale.th.join(5)
        err = stale.p.stderr.read() if rc is not None else None
        check("follow stale: the stalled follower, its slot taken by a "
              "fifth, exits 1 with dch: busy and no further line",
              ok and took and rc == 1 and err == b"dch: busy\n" and
              len(stale.got) == k, (ok, bool(took), rc, err,
                                    stale.objs(k)))
        fls.remove(stale)
    finally:
        if stopped:
            os.kill(stopped.p.pid, signal.SIGCONT)
        for f in fls:
            f.stop()
        dch("-k", n)


def test_follow_agent(off=False):
    """A shell's follower across an agent's start and exit, on the default
    60 s record tick: while a follower is subscribed the master probes for
    an agent every second and wakes it on a change, ring or no ring. The
    start: reset rotated, then a snapshot of corpus claude (one of corpus
    none may come first, while the transcript is not written). The exit, on an
    idle shell (no row pushed): reset rotated, then a snapshot of corpus
    shell (none with xray_ring 0). Each within 10 s: the probe is 1 s,
    the bound leaves room for a loaded runner."""
    n = "xc-fa" + ("o" if off else "")
    tag = "follow agent%s: " % (" (xray_ring 0)" if off else "")
    base = "none" if off else "shell"
    cfg = ENV["DCH_CONFIG"]
    bindir = os.path.join(T, "bin-fa")
    fdir = os.path.join(T, "fake-fa")
    proj = os.path.join(T, "faproj")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    env.pop("DCH_RECORD_MS", None)
    if off:
        with open(cfg, "w") as f:
            f.write("xray_ring = 0\n")
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "sh"], capture_output=True, text=True, timeout=30,
                       env=env)
    check(tag + "shell spawned", r.returncode == 0, r.stderr)
    fl = None
    try:
        wait_for(lambda: "$" in screen(n) or "#" in screen(n), 5)
        fl = Follow(n)
        s0 = fl.wait(lambda o: "snapshot" in o)
        check(tag + "the first snapshot is corpus " + base,
              s0 and s0[0]["snapshot"]["corpus"] == base, fl.objs())

        k0 = len(fl.got)
        t0 = time.time()
        dch("--send", n, "claude\r", env=env)
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        if rec:
            sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
            # the key of the real path, as Claude Code names it and as the
            # follower watches for a transcript that is not there yet
            pdir = os.path.join(HOME, ".claude", "projects",
                                key(os.path.realpath(proj)))
            os.makedirs(pdir, exist_ok=True)
            with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
                f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                    "content": "ask #11"}}) + "\n")

        def rotated_then(k0, want):
            # the object after a reset rotated. A snapshot of corpus none
            # on the way to `want` is passed over: the probe can see the
            # Claude before its transcript is on disk (written above, after
            # its session record), and a second reset rotated follows
            ob = fl.objs(k0)
            for j, o in enumerate(ob[:-1]):
                nx = ob[j + 1]
                if is_reset("rotated")(o) and (
                        want == "none" or "snapshot" not in nx or
                        nx["snapshot"]["corpus"] != "none"):
                    return nx
            return None
        wait_for(lambda: rotated_then(k0, "claude"), 15)
        secs = time.time() - t0
        nxt = rotated_then(k0, "claude")
        check(tag + "the agent's start sends reset rotated, then a "
              "snapshot of corpus claude (%.1f s)" % secs,
              nxt is not None and "snapshot" in nxt and
              nxt["snapshot"]["corpus"] == "claude" and secs < 10,
              fl.objs(k0))
        k1 = len(fl.got)
        t1 = time.time()
        dch("--send", n, "/exit\r", env=env)
        wait_for(lambda: rotated_then(k1, base), 15)
        secs = time.time() - t1
        nxt = rotated_then(k1, base)
        check(tag + "the agent's exit on an idle shell sends reset "
              "rotated, then a snapshot of corpus %s (%.1f s)" % (base, secs),
              nxt is not None and "snapshot" in nxt and
              nxt["snapshot"]["corpus"] == base and secs < 10,
              fl.objs(k1))
    finally:
        dch("-k", n)
        if off and os.path.exists(cfg):
            os.unlink(cfg)
        if fl:
            wait_for(lambda: fl.p.poll() is not None, 5)
            rc = fl.stop()
            check(tag + "the session's end exits 0", rc == 0,
                  (rc, fl.p.stderr.read() if fl.p.stderr else ""))

def test_screen_agent():
    """QA 1.54.0 MED-2: an agent's TUI, on the primary screen, never enters
    a shell answer, on the default 60 s record tick. The master probes for
    an agent before it answers a ring or a screen read. A follower: no ref
    from the TUI, then off corpus shell. A one-shot --ref with no follower:
    no count, both for a Claude the client maps itself and for one it
    cannot (a sessionId that is no UUID), where the master's pause alone
    keeps the TUI out."""
    bindir = os.path.join(T, "bin-sa")
    fdir = os.path.join(T, "fake-sa")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    env.pop("DCH_RECORD_MS", None)

    def start(n):
        with open(os.path.join(fdir, n + ".ctl"), "w") as f:
            json.dump({"above": ["see q/z#909 in the agent TUI"]}, f)
        r = dch("--spawn", n, "--size", "80x24", "sh", env=env)
        check("screen agent: %s spawned" % n, r.returncode == 0, r.stderr)
        wait_for(lambda: "$" in screen(n) or "#" in screen(n), 5)

    fl = None
    names = ["xc-saf", "xc-sao", "xc-sau"]
    try:
        n = names[0]
        start(n)
        if dch("--read", n).returncode == 3:
            print("  skip screen agent: dch-lite reads no screen")
            return
        fl = Follow(n, env=env)
        s0 = fl.wait(lambda o: "snapshot" in o)
        check("screen agent: the follower starts on corpus shell",
              s0 and s0[0]["snapshot"]["corpus"] == "shell", fl.objs())
        k0 = len(fl.got)
        dch("--send", n, "claude\r", env=env)
        up = wait_for(lambda: "q/z#909" in screen(n), 10)
        # no transcript here: the agent's corpus is claude or none
        snap = fl.wait(lambda o: "snapshot" in o and
                       o["snapshot"]["corpus"] != "shell", 10, frm=k0)
        time.sleep(0.5)
        leak = [o for o in fl.objs(k0) if "q/z#909" in json.dumps(o)]
        check("screen agent: the follower emits nothing from the agent's TUI",
              up and not leak, (bool(up), leak))
        check("screen agent: ... and leaves corpus shell", snap,
              fl.objs(k0))

        for n, cmd, what in ((names[1], "claude\r", "a Claude the client "
                              "maps"),
                             (names[2], "claude --resume not-a-uuid\r",
                              "a Claude with no UUID sessionId")):
            start(n)
            dch("--send", n, cmd, env=env)
            up = wait_for(lambda: "q/z#909" in screen(n), 10)
            r = dch("--xray", n, "--ref", "q/z#909", "--scope", "session",
                    "--full", "--json", env=env)
            ls = ref_lines(r.stdout) or []
            leak = [l for l in ls if "mention" in l or
                    l.get("tally", {}).get("count")]
            check("screen agent: a one-shot --ref, %s: no mention, no count "
                  "(no corpus, or count 0)" % what, up and not leak and
                  (r.returncode == 0 or "no x-ray corpus" in r.stderr),
                  (bool(up), r.returncode, r.stdout[-600:], r.stderr))
    finally:
        for n in names:
            dch("-k", n)
        if fl:
            fl.stop()


def test_follow_cap():
    """Past the per-file range (3.3 item 1): the capped snapshot, then
    exactly one reset full and the whole file's. Sparse: the holes are
    over-long lines, cheap to read and to skip."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    u = lambda t: (json.dumps({"type": "user", "cwd": "/home/demo/proj",
                               "message": {"content": t}}) + "\n").encode()
    with open(tp, "wb") as f:
        f.write(u("head #61"))
        f.seek(30 << 20)
        f.write(b"\n" + u("middle #62"))
        f.seek(300 << 20)
        f.write(b"\n" + u("tail #63"))
    n = "xc-fc"
    # a live Claude for the History record's transcript: the fake's id
    # pointed at RSID (a follow of a History session ends at once)
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", T,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    fl = None
    try:
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        rp = os.path.join(sdir, rec[0])
        r0 = json.load(open(rp))
        r0["sessionId"] = RSID
        with open(rp + ".tmp", "w") as f:
            json.dump(r0, f)
        os.rename(rp + ".tmp", rp)
        fl = Follow(n)
        s = fl.wait(lambda o: "snapshot" in o and len(
            [x for x in fl.objs() if "snapshot" in x]) >= 3, 30)
        time.sleep(1.0)
        ks = fl.kinds()
        keys = [[x["key"] for x in o["snapshot"]["refs"]]
                for o in fl.objs() if "snapshot" in o]
        o = fl.objs()
        check("follow: past the cap, the newest 32 MB's snapshot (#447: "
              "tail #63 alone), reset full, the capped one (no middle #62), "
              "then exactly one more reset full and the whole file's",
              ks == ["snapshot", "reset", "snapshot", "reset",
                     "snapshot"] and
              o[1] == o[3] == {"schema": 1, "reset": {"why": "full"}} and
              len(keys) == 3 and keys[0] == ["issue:#63"] and
              "issue:#61" in keys[1] and "issue:#62" not in keys[1] and
              "issue:#62" in keys[2] and
              not o[0]["snapshot"]["complete"] and
              not o[2]["snapshot"]["complete"] and
              o[4]["snapshot"]["complete"], (ks, keys))
    finally:
        if fl:
            fl.stop()
        dch("-k", n)
        os.unlink(tp)


def test_follow_shell():
    """A shell's --follow over a 64K ring: typed blocks give chapter and ref
    deltas, an eviction its evict line first, then counts down and
    underline off (mutant 42); a clear (--clear, ED 3, RIS) reset cleared,
    never evict (T9g); more than 256 keys evicted reset evicted; a fifth
    subscriber busy."""
    n = "xc-fs"
    cfg = ENV["DCH_CONFIG"]
    with open(cfg, "w") as f:
        f.write("xray_ring = 64K\n")
    prog = ("while :; do printf '\\033]133;A\\007$ \\033]133;B\\007'; "
            "read -r cmd || exit; printf '\\033]133;C\\007'; eval \"$cmd\"; "
            "printf '\\033]133;D;0\\007'; done")
    cwd = os.path.join(T, "fs proj")
    os.makedirs(cwd, exist_ok=True)
    trace = os.path.join(T, "trace-fs")
    open(trace, "w").close()
    dbg = dict(ENV, DCH_DEBUG=trace)
    fls = []
    try:
        r = dch("--spawn", n, "--size", "80x24", "--cwd", cwd, "sh", "-c",
                prog)
        check("follow shell: spawned under xray_ring 64K", r.returncode == 0,
              r.stderr)
        wait_for(lambda: "$" in screen(n), 5)
        for _ in range(2):
            dch("--send", n, "echo 'see o/r#41'; seq 1 30\n")
            time.sleep(0.3)
        wait_for(lambda: "30" in screen(n), 5)
        time.sleep(0.3)
        _, one, _ = xray(n, "--json")
        fl = Follow(n, dbg)
        fls.append(fl)
        snap = fl.wait(lambda o: "snapshot" in o)
        r41 = [x for x in (one or {}).get("refs", [])
               if x["key"] == "pull:o/r#41"]
        check("follow shell: the snapshot is the --json object",
              snap and snap[0]["snapshot"] == one and r41 and
              r41[0]["underline"], (snap, one))

        time.sleep(0.2)
        off = os.path.getsize(trace)
        time.sleep(1.0)
        idle = trace_from(trace, off)
        check("follow shell: idle, no deadline and no wake (no timer)",
              "xray follow: deadline" not in idle and
              "xray follow: woke" not in idle, idle)

        k0 = len(fl.got)
        dch("--send", n, "echo 'more o/r#42'; seq 1 30\n")
        a = fl.wait(is_ref("pull:o/r#42"), frm=k0)
        c = fl.wait(lambda o: "chapter" in o, frm=k0)
        check("follow shell: a typed block gives a chapter and a ref delta",
              a and c and c[0]["chapter"]["title"] == "echo 'more o/r#42'; seq 1 30",
              fl.objs(k0))
        time.sleep(0.4)
        _, now, _ = xray(n, "--json")
        cur = {x["key"]: x for x in snap[0]["snapshot"]["refs"]} if snap \
            else {}
        for o in fl.objs():
            if "ref" in o:
                cur[o["ref"]["key"]] = o["ref"]
        check("follow shell: the snapshot plus its deltas is the --json of "
              "now (refs)", now and sorted(cur.values(), key=str) ==
              sorted(now["refs"], key=str), (cur, now and now["refs"]))

        # mutant 42: the eviction step's evict line first, then underline off
        k1 = len(fl.got)
        dch("--send", n, "seq 1 20000\n")
        gone = fl.wait(is_ref("pull:o/r#41", count=0), 15, frm=k1)
        ob = fl.objs(k1)
        i = next((j for j, o in enumerate(ob) if is_ref("pull:o/r#41",
                                                         count=0)(o)), None)
        j = i
        while j is not None and j > 0 and ("ref" in ob[j - 1] or
                                           "chapter" in ob[j - 1]):
            j -= 1
        ev = ob[j - 1] if j else None
        check("follow shell: an eviction sends evict first, then the gone "
              "key as count 0, underline and defined off", gone and
              ev and "evict" in ev and gone[0]["ref"]["underline"] is False
              and gone[0]["ref"]["defined"] is False,
              [list(o)[1:] for o in ob][:40])
        chs = (one or {}).get("chapters", [])
        check("follow shell: evict's seq is past the evicted block's anchor",
              ev and "evict" in ev and chs and
              ev["evict"]["seq"] > chs[0]["offset"], (ev, chs))
        check("follow shell: no reset for a small eviction",
              not [o for o in ob if "reset" in o], ob[:20])
        wait_for(lambda: "20000" in screen(n), 10)

        # T9g: a clear sends reset cleared and a snapshot, never evict
        for what, how in (("--clear", None), ("ED 3", "printf '\\033[3J'\n"),
                          ("RIS", "printf '\\033c'\n")):
            time.sleep(0.4)
            k = len(fl.got)
            if how:
                dch("--send", n, "echo 'pre o/r#43'; seq 1 30\n")
                fl.wait(is_ref("pull:o/r#43"), frm=k)
                time.sleep(0.4)
                k = len(fl.got)
                dch("--send", n, how)
            else:
                rc, cl, err = xray(n, "--clear", "--json")
            s = fl.wait(lambda o: "snapshot" in o, frm=k)
            ob = fl.objs(k)
            check("follow shell: %s sends reset cleared, then a snapshot, "
                  "and no evict" % what, s and
                  ob[0] == {"schema": 1, "reset": {"why": "cleared"}} and
                  "snapshot" in ob[1] and
                  not [o for o in ob if "evict" in o] and
                  not [x for x in s[0]["snapshot"]["refs"]
                       if x["key"] in ("pull:o/r#41", "pull:o/r#43")],
                  [list(o)[1:] for o in ob][:10])

        # 4 subscribers per master: a fifth is busy, before any line
        for _ in range(3):
            fls.append(Follow(n))
        ok = all(f.wait(lambda o: "snapshot" in o) for f in fls)
        r = dch("--xray", n, "--follow", "--json")
        check("follow shell: a fifth subscriber gets busy, exit 1, nothing "
              "on stdout", ok and r.returncode == 1 and not r.stdout and
              r.stderr == "dch: busy\n", (ok, r.returncode, r.stdout[:200],
                                          r.stderr))

        # the session's end ends every follower, exit 0
        dch("-k", n)
        rcs = []
        for f in fls:
            try:
                rcs.append(f.p.wait(5))
            except subprocess.TimeoutExpired:
                rcs.append(None)
        check("follow shell: the session's end ends every follower, exit 0",
              rcs == [0] * len(fls), rcs)
        tr = open(trace).read()
        check("follow shell: the trace holds no passage",
              "see o/r#41" not in tr and "more o/r" not in tr, tr[-300:])
    finally:
        for f in fls:
            f.stop()
        dch("-k", n)

    # more than 256 keys in one eviction step: reset evicted. The keys sit
    # on three adjacent rows of 100 (a 1000-column pane; 128 mentions per
    # entry, 3.8), so one step evicts them all.
    n = "xc-fs2"
    fl = None
    try:
        r = dch("--spawn", n, "--size", "1000x24", "--cwd", cwd, "sh", "-c",
                prog)
        wait_for(lambda: "$" in screen(n), 5)
        fl = Follow(n)
        fl.wait(lambda o: "snapshot" in o)
        # typed as a loop: a canonical-mode line holds 1024 bytes
        dch("--send", n, "for i in $(seq 1000 1299); do printf 'o/r#%s ' "
            "$i; [ $((i % 100)) = 99 ] && echo; done; seq 1 30\n")
        fl.wait(is_ref("pull:o/r#1299"), 10)
        time.sleep(0.4)
        k = len(fl.got)
        dch("--send", n, "seq 1 20000\n")
        s = fl.wait(is_reset("evicted"), 15, frm=k)
        ob = fl.objs(k)
        i = next((j for j, o in enumerate(ob) if is_reset("evicted")(o)), 0)
        check("follow shell: an eviction of more than 256 keys sends evict, "
              "reset evicted, then a snapshot", s and i > 0 and
              "evict" in ob[i - 1] and i + 1 < len(ob) and
              "snapshot" in ob[i + 1] and
              not [x for x in ob[i + 1]["snapshot"]["refs"]
                   if x["key"] == "pull:o/r#1000"],
              [list(o)[1:] for o in ob][:12])
    finally:
        if fl:
            fl.stop()
        dch("-k", n)
        if os.path.exists(cfg):
            os.unlink(cfg)


# ---- #140 (Q16): the current primary screen, never the alt buffer ---------

def test_screen():
    """A shell's answer is the ring plus the current primary screen: a ref
    printed but not yet scrolled is found by --json, --ref and --follow, and
    lines counts the rows read. While a program holds the alt screen, neither
    the alt buffer nor the primary screen is read: the ring alone, no error,
    no why (never "alt"); the follower drops the screen's ref with no ring
    row moved, and takes it back when alt exits."""
    n = "xc-scr"
    fl = None
    r = dch("--spawn", n, "--size", "80x24", "sh")
    check("screen: spawned", r.returncode == 0, r.stderr)

    def refs(o):
        return {x["key"]: x for x in (o or {}).get("refs", [])}

    def rows():
        return [l for l in screen(n).split("\n") if l.strip()]

    try:
        wait_for(lambda: screen(n).strip(), 5)
        # \043 is #: the typed line holds no ref, only the output does
        dch("--send", n, "printf 'see q/w\\043140 here\\n'\n")
        wait_for(lambda: "q/w#140" in screen(n), 5)
        _, recs = X.read_all(n, 0)
        rc, o, err = xray(n, "--json")
        check("screen: a ref on the primary screen, not yet in the ring, is "
              "found", rc == 0 and o and not recs and "pull:q/w#140" in
              refs(o) and refs(o)["pull:q/w#140"]["count"] == 1 and
              o["complete"] and o["why"] is None, (rc, o, recs, err))
        check("screen: lines counts the rows read (the screen's non-blank "
              "rows; the ring is empty)", o and o["lines"] == len(rows()),
              (o and o["lines"], rows()))
        r = dch("--xray", n, "--ref", "q/w#140", "--scope", "session",
                "--full", "--json")
        men = [json.loads(l).get("mention") for l in r.stdout.splitlines()]
        men = [m for m in men if m]
        check("screen: --ref --full gives its mention line", r.returncode == 0
              and len(men) == 1 and men[0]["line"] == "see q/w#140 here",
              r.stdout)
        fl = Follow(n)
        snap = fl.wait(lambda x: "snapshot" in x)
        check("screen: the follower's snapshot is the --json object",
              snap and snap[0]["snapshot"] == o, (snap, o))

        k = len(fl.got)
        dch("--send", n, "printf '\\033[?1049halt k/l\\043150\\n'; read x; "
            "printf '\\033[?1049l'\n")
        wait_for(lambda: "k/l#150" in screen(n), 5)
        gone = fl.wait(is_ref("pull:q/w#140", count=0), 5, frm=k)
        rc, a, err = xray(n, "--json")
        _, recs = X.read_all(n, 0)
        check("screen: alt active, neither the alt buffer nor the primary "
              "screen is read (the ring alone)", rc == 0 and a and not
              set(refs(a)) & {"pull:k/l#150", "pull:q/w#140"} and
              a["lines"] == len([x for x in recs if x["kind"] != 4]),
              (rc, a, err))
        check("screen: alt is no error: exit 0, nothing on stderr, "
              "available, complete, why null (never \"alt\")", rc == 0 and
              a and a["available"] and a["complete"] and a["why"] is None and
              err == "", (rc, a, err))
        check("screen: the follower drops the screen's ref on the alt switch "
              "(no ring row moved)", gone, fl.objs(k))
        r = dch("--xray", n, "--ref", "k/l#150", "--scope", "session",
                "--full", "--json")
        check("screen: --ref never reads the alt buffer", r.returncode == 0
              and '"count":0' in r.stdout and '{"mention"' not in
              r.stdout.replace('"schema":1,', ''), r.stdout)

        k = len(fl.got)
        dch("--send", n, "\n")
        wait_for(lambda: "k/l#150" not in screen(n), 5)
        back = fl.wait(is_ref("pull:q/w#140", count=1), 5, frm=k)
        time.sleep(0.4)
        rc, b, err = xray(n, "--json")
        check("screen: alt exited, the screen's rows rejoin --json",
              rc == 0 and b and "pull:q/w#140" in refs(b) and
              "pull:k/l#150" not in refs(b) and b["why"] is None and
              b["lines"] == len(rows()), (rc, b, rows()))
        check("screen: ... and the follower, with no new output",
              back, fl.objs(k))
        cur = refs(snap[0]["snapshot"]) if snap else {}
        for x in fl.objs():
            if "ref" in x:
                cur[x["ref"]["key"]] = x["ref"]
        cur = {kk: v for kk, v in cur.items() if v["count"]}
        check("screen: the snapshot plus its deltas is the --json of now",
              b and cur == refs(b), (cur, b and refs(b)))
    finally:
        if fl:
            fl.stop()
        dch("-k", n)


def test_screen_seam():
    """#171 (QA 1.54.0 MED-1), QA's repro: zsh with --shell-init, 80x10,
    `seq 1 30`, `echo x/y#1 a`, `echo x/y#1 b`. The ring ends inside
    `seq`'s open block (its D waits on the prompt row, on screen): no screen
    row takes that block's chapter, and the count is the same while the rows
    are on screen and after they scroll, so a follower reports nothing on a
    pure scroll."""
    zsh = shutil.which("zsh")
    if not zsh:
        print("  skip screen seam: no zsh installed")
        return
    n = "xc-seam"
    fl = None
    d = os.path.join(T, "seam-zdot")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".zshrc"), "w") as f:
        f.write("PS1='zp%% '\n" + dch("--shell-init", "zsh").stdout)
    r = dch("--spawn", n, "--size", "80x10", zsh, "-i",
            env=dict(ENV, ZDOTDIR=d))
    check("screen seam: spawned", r.returncode == 0, r.stderr)

    def count(o):
        return {x["key"]: x["count"] for x in (o or {}).get("refs", [])}.get(
            "pull:x/y#1")

    def mentions():
        r = dch("--xray", n, "--ref", "x/y#1", "--scope", "session",
                "--full", "--json")
        return [m["mention"] for m in ref_lines(r.stdout) if "mention" in m]

    try:
        wait_for(lambda: "zp%" in screen(n), 5)
        for cmd, out in (("seq 1 30", "\n30\n"), ("echo x/y#1 a", "1 a\n"),
                         ("echo x/y#1 b", "1 b\n")):
            dch("--send", n, cmd + "\n")
            wait_for(lambda: out in screen(n) and
                     screen(n).rstrip().endswith("zp%"), 5)
        rc, o, err = xray(n, "--json")
        seq = [c["anchor"] for c in (o or {}).get("chapters", [])
               if c["title"] == "seq 1 30"]
        men = mentions()
        check("screen seam: on screen, count 2 (one per command, as the "
              "ring will count them)", rc == 0 and count(o) == 2 and seq,
              (rc, o, err))
        check("screen seam: no screen row takes the open block's chapter",
              len(men) == 4 and not [m for m in men if m["chapter"]],
              (seq, men))
        fl = Follow(n)
        fl.wait(lambda x: "snapshot" in x)
        k = len(fl.got)
        dch("--send", n, "seq 1 12\n")
        wait_for(lambda: "\n12\n" in screen(n) and
                 screen(n).rstrip().endswith("zp%"), 5)
        time.sleep(1.0)             # four follower batches
        rc, b, err = xray(n, "--json")
        men = mentions()
        check("screen seam: scrolled into the ring, still count 2", rc == 0
              and count(b) == 2 and "echo x/y#1 b" in
              [c["title"] for c in b["chapters"]], (rc, b, err))
        check("screen seam: ... each output row now in its own command's "
              "chapter, never seq's", len(men) == 4 and
              all(m["chapter"] not in seq for m in men) and
              [bool(m["chapter"]) for m in men] == [False, True, False, True],
              men)
        # #262: a ref line may come, its last_offset moved with the rows'
        # provisional seqs; its count stays
        check("screen seam: the follower reports no count change on the "
              "scroll", not [x for x in fl.objs(k)
                             if is_ref("pull:x/y#1")(x) and
                             x["ref"]["count"] != 2], fl.objs(k))
    finally:
        if fl:
            fl.stop()
        dch("-k", n)


def test_screen_resize():
    """#171 (QA 1.54.0 LOW-3): a grow pulls ring rows back on screen; the
    screen read starts after the newest captured row, so they are not read
    twice: the count and `lines` stay as they were."""
    n = "xc-grow"
    r = dch("--spawn", n, "--size", "80x10", "sh")
    check("screen grow: spawned", r.returncode == 0, r.stderr)

    def answer():
        rc, o, err = xray(n, "--json")
        return (rc, {x["key"]: x["count"] for x in (o or {}).get("refs", [])},
                o and o["lines"], o and o["complete"])

    try:
        wait_for(lambda: screen(n).strip(), 5)
        # \043 is #: the typed line holds no ref, only row 12 of the output
        dch("--send", n, "i=0; while [ $i -lt 30 ]; do i=$((i+1)); "
            "if [ $i = 12 ]; then printf 'x/y\\0439\\n'; else echo l$i; fi; "
            "done; echo end\n")
        wait_for(lambda: "\nend\n" in screen(n), 5)
        before = answer()
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(sock(n))
        # [MSG_WINCH][len][struct winsize 30x80]: the pane grows
        c.sendall(bytes([3, 8, 0]) + struct.pack("=HHHH", 30, 80, 0, 0))
        time.sleep(0.5)
        c.close()
        wait_for(lambda: "x/y#9" in screen(n), 5)
        after = answer()
        check("screen grow: x/y#9 pulled back on screen, counted once",
              before[0] == 0 and before[1].get("pull:x/y#9") == 1 and
              after[1] == before[1], (before, after))
        check("screen grow: lines unchanged (no pulled-back row read twice)",
              after[2] == before[2] and after[3], (before, after))
    finally:
        dch("-k", n)


def main():
    try:
        test_follow_claude()
        test_follow_tasks_memory()
        test_follow_fast()
        test_follow_late()
        test_follow_cap()
        test_follow_shell()
        test_follow_stale()
        test_follow_agent()
        test_follow_agent(off=True)
        test_screen_agent()
        test_screen()
        test_screen_seam()
        test_screen_resize()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
