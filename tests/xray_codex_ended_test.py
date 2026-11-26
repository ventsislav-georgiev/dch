#!/usr/bin/env python3
"""`dch --xray` over an ended Codex session (#656).

A History record carrying "codex":{"thread_id","home"} (#655) and no live
master: X-ray reads that thread's rollout under the record's CODEX_HOME,
as it reads an ended Claude transcript. All under a temp HOME, with a fake
CODEX_HOME of fixture rollouts; no Codex ever runs.

  - the plain rollout, the .jsonl.zst one (Codex compresses rollouts after
    7 days; made here with the zstd CLI, the row SKIPs without it) and the
    reverted thread's -<tid>_<rollout-id>.jsonl name
  - .jsonl wins over its .zst sibling
  - a corrupt .zst: corpus none, reason "corrupt", no crash
  - --ref, --search, --tasks and --chapter over the ended rollout

Run:  python3 tests/xray_codex_ended_test.py    (uses ./dch; run.sh isolates)
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TIDS = ["019a3333-4444-7555-8666-7777888899%02d" % k for k in range(1, 9)]
RID = "019a3333-4444-7555-8666-777788889999"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:1200])


T = os.path.realpath(tempfile.mkdtemp(prefix="xce"))
HOME = os.path.join(T, "home")
CODEX = os.path.join(HOME, ".codex")
DAY = os.path.join(CODEX, "sessions", "2026", "11", "10")
PROJ = os.path.join(T, "proj")
for _d in (HOME, DAY, PROJ, os.path.join(T, "tmp")):
    os.makedirs(_d, mode=0o700)
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_", "CLAUDE", "CODEX", "Malloc"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, TMPDIR=os.path.join(T, "tmp"),
           DCH_CONFIG=os.path.join(T, "config"), DCH_LOGIN_ENV="0",
           DCH_NO_REMOTE="1", DCH_NO_GITHUB="1")
SOCKDIR = os.path.join(T, "dch-%d" % os.getuid())
RECDIR = os.path.join(T, "state", "dch", "sessions", SOCKDIR.replace("/", "-"))
ZSTD = shutil.which("zstd")


def dch(*a):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=ENV)


def xray(n):
    r = dch("--xray", n, "--json")
    try:
        return r.returncode, json.loads(r.stdout), r.stderr
    except ValueError:
        return r.returncode, None, r.stdout + r.stderr


def line(sec, typ, payload):
    return json.dumps({"timestamp": "2026-11-10T10:00:%02d.000Z" % sec,
                       "type": typ, "payload": payload},
                      separators=(",", ":")) + "\n"


def body(tid, turn):
    return (line(0, "session_meta", {"id": tid, "cwd": PROJ,
                                     "cli_version": "0.167.2"}) +
            line(1, "response_item", {"type": "message", "role": "user",
                                      "content": [{"type": "input_text",
                                                   "text": turn}]}) +
            line(2, "response_item", {"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text",
                                                   "text": "done, see #41"}]}))


def rollout(tid, turn, suffix="", zst=False, keep_plain=False):
    p = os.path.join(DAY, "rollout-2026-11-10T10-00-00-%s%s.jsonl"
                     % (tid, suffix))
    with open(p, "w") as f:
        f.write(body(tid, turn))
    os.chmod(p, 0o600)
    if zst:
        subprocess.run([ZSTD, "-q", "-f", p, "-o", p + ".zst"], check=True)
        os.chmod(p + ".zst", 0o600)
        if not keep_plain:
            os.unlink(p)
    return p


def plant(n, tid):
    """An ended record with a Codex thread, as the master writes it."""
    now = int(time.time())
    r = dict(name=n, sock_dir=SOCKDIR, alias="", cwd=PROJ,
             argv=["codex"], started=now - 660, updated=now - 60,
             ended=now - 60, end="exit 0", agent=None,
             codex={"thread_id": tid, "home": CODEX})
    os.makedirs(RECDIR, mode=0o700, exist_ok=True)
    p = os.path.join(RECDIR, n + ".json")
    fd = os.open(p + ".t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.rename(p + ".t", p)


def answers(n, tid, title, what):
    rc, o, err = xray(n)
    ok = (rc == 0 and o is not None and o["corpus"] == "codex" and
          o["live"] is False and o["available"] is True and
          o["session_id"] == tid and
          [c["title"] for c in o["chapters"]] == [title])
    check("ended Codex, %s: --xray --json reads its rollout" % what, ok,
          (rc, o, err))
    return o if ok else None


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    print("xray_codex_ended_test: %s" % DCH)

    plant("cx-plain", TIDS[0])
    rollout(TIDS[0], "fix the parser, see #41")
    o = answers("cx-plain", TIDS[0], "fix the parser, see #41", "plain .jsonl")
    if o:
        r = dch("--xray", "cx-plain", "--ref", "#41", "--scope", "session",
                "--json")
        check("ended Codex: --ref finds #41 in the rollout",
              r.returncode == 0 and "done, see #41" in r.stdout and
              TIDS[0] in r.stdout, (r.returncode, r.stdout, r.stderr))
        r = dch("--xray", "cx-plain", "--search", "#41", "--json")
        sr = json.loads(r.stdout) if r.returncode == 0 else {}
        check("ended Codex: --search hits the answer and the turn",
              sr.get("corpus") == "codex" and sr.get("session_id") == TIDS[0]
              and [h["who"] for h in sr.get("hits", [])] ==
              ["assistant", "user"], (r.returncode, r.stdout, r.stderr))
        r = dch("--xray", "cx-plain", "--chapter",
                o["chapters"][0]["anchor"], "--json")
        check("ended Codex: --chapter over its rollout",
              r.returncode == 0 and "done, see #41" in r.stdout,
              (r.returncode, r.stdout[:400], r.stderr))
        with open(os.path.join(CODEX, "AGENTS.md"), "w") as f:
            f.write("codex user\n")
        r = dch("--xray", "cx-plain", "--memory", "--json")
        check("ended Codex: --memory reads the record's CODEX_HOME",
              r.returncode == 0 and '"source":"codex"' in r.stdout and
              os.path.join(CODEX, "AGENTS.md") in r.stdout,
              (r.returncode, r.stdout[:400], r.stderr))
        r = dch("--xray", "cx-plain", "--tasks", "--json")
        check("ended Codex: --tasks names the thread",
              r.returncode == 0 and TIDS[0] in r.stdout,
              (r.returncode, r.stdout[:400], r.stderr))

    plant("cx-rid", TIDS[2])
    rollout(TIDS[2], "a reverted thread", suffix="_" + RID)
    answers("cx-rid", TIDS[2], "a reverted thread", "-<tid>_<rollout-id> name")

    if not ZSTD:
        print("  SKIP .zst rows: no zstd CLI to make the fixture")
    else:
        plant("cx-zst", TIDS[1])
        rollout(TIDS[1], "compressed turn about #41", zst=True)
        o = answers("cx-zst", TIDS[1], "compressed turn about #41",
                    ".jsonl.zst only")
        if o:
            again = xray("cx-zst")[1]
            check("ended Codex .zst: the anchor holds from run to run "
                  "(the decoded copy is cached)",
                  again and again["chapters"][0]["anchor"] ==
                  o["chapters"][0]["anchor"], (o, again))
            r = dch("--xray", "cx-zst", "--ref", "#41", "--scope", "session",
                    "--json")
            check("ended Codex .zst: --ref reads the decoded rollout",
                  r.returncode == 0 and "compressed turn about #41" in
                  r.stdout, (r.returncode, r.stdout, r.stderr))
            r = dch("--xray", "cx-zst", "--search", "compressed", "--json")
            check("ended Codex .zst: --search reads it too",
                  r.returncode == 0 and
                  json.loads(r.stdout).get("hits"), (r.stdout, r.stderr))

        # the copy cache is bounded: a new copy drops copies and stray
        # .dec.* files unused for 24 h, and keeps itself
        cache = os.path.join(T, "tmp", "dch-zst-%d" % os.getuid())
        old = time.time() - 2 * 86400
        os.makedirs(cache, mode=0o700, exist_ok=True)
        stale = [os.path.join(cache, "1-2-3-4.ok.jsonl"),
                 os.path.join(cache, ".dec.x")]
        for f in stale:
            with open(f, "w") as fh:
                fh.write("old\n")
            os.utime(f, (old, old))
        plant("cx-prune", TIDS[7])
        rollout(TIDS[7], "pruning turn", zst=True)
        answers("cx-prune", TIDS[7], "pruning turn", "a new .zst copy")
        left = os.listdir(cache)
        check("the cache drops a copy and a .dec.* older than 24 h, "
              "keeps the new copy",
              not any(os.path.exists(f) for f in stale) and
              # cx-zst's copy and cx-prune's new one
              len([n for n in left if n.endswith(".ok.jsonl")]) == 2,
              left)

        plant("cx-both", TIDS[3])
        rollout(TIDS[3], "the plain one", zst=True, keep_plain=True)
        z = os.path.join(DAY, "rollout-2026-11-10T10-00-00-%s.jsonl.zst"
                         % TIDS[3])
        # the .zst now holds another turn: only the plain one may be read
        with open(z + ".src", "w") as f:
            f.write(body(TIDS[3], "the zst one"))
        subprocess.run([ZSTD, "-q", "-f", z + ".src", "-o", z], check=True)
        os.unlink(z + ".src")
        answers("cx-both", TIDS[3], "the plain one", ".jsonl beside .zst")

        plant("cx-bad", TIDS[4])
        p = os.path.join(DAY, "rollout-2026-11-10T10-00-00-%s.jsonl.zst"
                         % TIDS[4])
        with open(p, "wb") as f:
            f.write(b"\x28\xb5\x2f\xfd" + b"\xff" * 64)
        os.chmod(p, 0o600)
        bad = "corrupt"
        rc, o, err = xray("cx-bad")
        check("ended Codex, corrupt .zst: corpus none, reason " + bad,
              rc == 0 and o and o["corpus"] == "none" and
              o["available"] is False and o["reason"] == bad,
              (rc, o, err))
        r = dch("--xray", "cx-bad", "--ref", "#41", "--json")
        check("ended Codex, corrupt .zst: --ref says why, exit 1",
              r.returncode == 1 and bad in r.stderr, r.stderr)

        plant("cx-cut", TIDS[5])
        rollout(TIDS[5], "x" * 300000, zst=True)
        p = os.path.join(DAY, "rollout-2026-11-10T10-00-00-%s.jsonl.zst"
                         % TIDS[5])
        data = open(p, "rb").read()
        with open(p, "wb") as f:
            f.write(data[:len(data) - 8])
        rc, o, err = xray("cx-cut")

        check("ended Codex, truncated .zst: reason or why corrupt, no crash",
              rc == 0 and o and (o["reason"] == bad or
                                 o["why"] == bad), (rc, o, err))

    plant("cx-none", TIDS[6])
    rc, o, err = xray("cx-none")
    check("ended Codex with no rollout: corpus none, the thread id",
          rc == 0 and o and o["corpus"] == "none" and
          o["session_id"] == TIDS[6] and o["reason"] is None, (rc, o, err))

    print("xray_codex_ended_test: %s" % ("FAIL (%d)" % fails[0] if fails[0]
                                         else "PASS"))
    return 1 if fails[0] else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    sys.exit(rc)
