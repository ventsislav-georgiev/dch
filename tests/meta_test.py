"""Session metadata: `dch --meta <name> '<json>'|clear` and --ls-json "meta".

A program inside a session (status line, hook, script) publishes one JSON
object about itself; dch stores it in <sock>.meta and --ls-json passes it
through verbatim with "meta_epoch" (the push time). It describes the running
program, so it goes away when the child exits (respawn or not), on kill, and
on master exit.

Run:  python3 tests/meta_test.py        (uses ./dch)
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))

META = {"tasks": {"done": 22, "total": 26, "in_progress": 2, "queued": 2},
        "prs": {"open": [{"url": "https://github.com/o/r/pull/12",
                          "repo": "o/r", "num": 12,
                          "title": 'say "hi" {x} \\ é'}], "merged": 18},
        "x-custom": [1, 2.5, True, None]}


def run(*args):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True)


def until(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not fn():
        time.sleep(0.05)
    return fn()


def main():
    tmp = tempfile.mkdtemp()
    os.environ.pop("DCH_SESSION", None)
    os.environ["XDG_RUNTIME_DIR"] = tmp
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    fails = 0

    def check(msg, got, want):
        nonlocal fails
        print("  %s %s" % ("ok  " if got == want else "FAIL", msg))
        if got != want:
            print("       want %r\n       got  %r" % (want, got))
            fails += 1

    def rows():
        r = run("--ls-json")
        try:
            out = {e["name"]: e for e in json.loads(r.stdout)}
        except ValueError:
            out = None
        check("--ls-json valid JSON, single line, exit 0, no stderr",
              (out is not None, r.stdout.count("\n"), r.returncode, r.stderr),
              (True, 1, 0, ""))
        return out or {}

    def meta(name):
        e = rows().get(name, {})
        return e.get("meta", "<absent>"), e.get("meta_epoch", "<absent>")

    def flagged(name):
        """A child that exits when <tmp>/<name>.go appears."""
        flag = os.path.join(tmp, name + ".go")
        subprocess.run([DCH, "--spawn", name, "sh", "-c",
                        "while [ ! -e '%s' ]; do sleep 0.05; done" % flag],
                       check=True, capture_output=True)
        return flag

    try:
        for n in ("m-main", "m-none"):
            subprocess.run([DCH, "--spawn", n, "sleep", "60"], check=True,
                           capture_output=True)
        compact = json.dumps(META, separators=(",", ":"))

        check("a valid object is stored (exit 0)",
              run("--meta", "m-main", compact).returncode, 0)
        check("sidecar sits next to the socket",
              os.path.isfile(os.path.join(sockdir, "m-main.sock.meta")), True)
        m, ep = meta("m-main")
        check("--ls-json round-trips the object", m, META)
        check("meta_epoch is the push time",
              isinstance(ep, int) and abs(ep - time.time()) < 60, True)
        check("emitted verbatim", compact in run("--ls-json").stdout, True)
        side = os.path.join(sockdir, "m-main.sock.meta")
        os.utime(side, (1000000000, 1000000000))  # as if pushed long ago
        check("identical push exits 0",
              run("--meta", "m-main", compact).returncode, 0)
        check("identical push keeps meta_epoch (content unchanged)",
              meta("m-main")[1], 1000000000)
        check("changed push advances meta_epoch",
              (run("--meta", "m-main", compact[:-1] + ',"v":2}').returncode,
               meta("m-main")[1] > 1000000000), (0, True))
        run("--meta", "m-main", compact)
        check("a session without meta gives null / 0",
              meta("m-none"), (None, 0))

        for why, arg in (("not an object", "[1]"),
                         ("invalid JSON", '{"a":'),
                         ("over 16 KB", json.dumps({"p": "x" * 16400})),
                         ("a newline inside", '{"a":\n1}'),
                         ("no argument", None)):
            r = run("--meta", "m-main", *([arg] if arg is not None else []))
            check("rejects %s: exit 1, one stderr line" % why,
                  (r.returncode, r.stderr.count("\n")), (1, 1))
        check("rejections leave the stored meta intact",
              meta("m-main")[0], META)

        r = run("--meta", "m-nosuch", "{}")
        check("missing session: exit 1", r.returncode, 1)

        check("clear exits 0", run("--meta", "m-main", "clear").returncode, 0)
        check("cleared: null / 0", meta("m-main"), (None, 0))
        check("clear with nothing stored exits 0",
              run("--meta", "m-main", "clear").returncode, 0)

        # A planted bad sidecar must not break --ls-json.
        with open(os.path.join(sockdir, "m-none.sock.meta"), "w") as f:
            f.write('{"a":1}\n{"b":2}')
        check("a hand-mangled sidecar reads as null / 0",
              meta("m-none"), (None, 0))

        # --- child exit before a --respawn relaunch clears it ------------
        flag = flagged("m-resp")
        run("--meta", "m-resp", '{"who":"first"}')
        check("respawn session has meta",
              meta("m-resp")[0], {"who": "first"})
        check("--respawn arms",
              run("--respawn", "m-resp", "--", "sleep", "60").returncode, 0)
        open(flag, "w").close()
        check("child exit before the relaunch clears meta",
              until(lambda: meta("m-resp") == (None, 0)), True)
        check("the session itself survives the respawn",
              "m-resp" in rows(), True)

        # --- child exit without respawn: master exits, sidecar goes -------
        flag = flagged("m-exit")
        run("--meta", "m-exit", '{"who":"exit"}')
        side = os.path.join(sockdir, "m-exit.sock.meta")
        check("exiting session has its sidecar", os.path.isfile(side), True)
        open(flag, "w").close()
        check("master exit removes the sidecar",
              until(lambda: not os.path.exists(side)), True)

        # --- kill removes it ----------------------------------------------
        run("--meta", "m-main", "{}")
        side = os.path.join(sockdir, "m-main.sock.meta")
        check("empty object accepted", meta("m-main")[0], {})
        run("-k", "m-main")
        check("kill removes the sidecar", os.path.exists(side), False)
    finally:
        subprocess.run([DCH, "-kl"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails == 0:
        print("PASS: session metadata round-trips and is cleared.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
