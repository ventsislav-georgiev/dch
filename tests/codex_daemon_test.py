"""Codex's shared app-server daemon must not bind or name a dch session (#083).

codex >= 0.167 runs threads in a background daemon (ppid 1) unless the TUI
is started with --no-daemon. The daemon carries the env of the session that
started it, so its shell snapshots look like that session's own.

  - dch adds --no-daemon to a Codex TUI whose --help lists it, and leaves an
    older Codex (no such flag) alone
  - while a Codex outside the session's master carries the session's
    DCH_SESSION and bridge marker, the sidecar publishes no peer, --ls-json
    names nothing, and --agent-send is refused
  - a session without such a daemon still publishes and is named

Run with DCH=/path/to/dch python3 this-file.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

DCH = os.environ.get("DCH", str(Path(__file__).resolve().parents[1] / "dch"))
THREAD = "123e4567-e89b-12d3-a456-4266141740aa"
OWN_THREAD = "123e4567-e89b-12d3-a456-4266141740bb"

# A native fake: dch only probes non-script executables, and a process is a
# Codex to dch by its executable's name, which for a script is the
# interpreter. Its env must be readable, so not a copied platform binary.
FAKE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
  const char *log = getenv("FAKE_CODEX_LOG"), *m = getenv("DCH_NATIVE_BRIDGE_ID");
  FILE *f;
  if (argc > 1 && !strcmp(argv[1], "--help")) {
    puts(getenv("FAKE_CODEX_OLD") ? "Usage: codex [OPTIONS]"
                                  : "      --no-daemon\n          Run without");
    return 0;
  }
  if (log && (f = fopen(log, "a"))) {
    fprintf(f, "%ld %s", (long)getpid(), m && *m ? m : "-");
    for (int i = 1; i < argc; i++)
      fprintf(f, " %s", argv[i]);
    fputc('\n', f);
    fclose(f);
  }
  for (;;)
    pause();
}
"""


def wait_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


def log_line(path):
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return None
    return lines[0].split(" ") if lines else None


def detached(argv, env):
    """Double fork, like a daemon: the process ends up under init (or a
    subreaper), never under a dch master."""
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            if os.fork() == 0:
                fd = os.open(os.devnull, os.O_RDWR)
                for n in (0, 1, 2):
                    os.dup2(fd, n)
                os.execve(argv[0], argv, env)
        finally:
            os._exit(0)
    os.waitpid(pid, 0)


def main():
    failures = []

    def check(label, condition):
        print("  %s %s" % ("ok  " if condition else "FAIL", label))
        failures.extend([] if condition else [label])

    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at " + DCH)
        return 1
    with tempfile.TemporaryDirectory(
        prefix="dch-codexd-", dir=os.path.realpath("/tmp")
    ) as raw:
        root = Path(raw)
        home, runtime = root / "home", root / "runtime"
        sessions = home / ".claude" / "sessions"
        codex_home = home / "codex-state"
        snapshots = codex_home / "shell_snapshots"
        for d in (sessions, runtime / "sockets", snapshots):
            d.mkdir(parents=True, mode=0o700)
        (root / "bin").mkdir()
        fake = root / "bin" / "codex"
        src = root / "fake.c"
        src.write_text(FAKE)
        built = subprocess.run(
            ["/usr/bin/cc", "-o", str(fake), str(src)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if built.returncode != 0:
            print("FAIL: cannot build the fake codex\n" + built.stderr)
            return 1
        env = dict(
            os.environ,
            HOME=str(home),
            CODEX_HOME=str(codex_home),
            CLAUDE_CONFIG_DIR=str(home / ".claude"),
            XDG_RUNTIME_DIR=str(runtime),
            DCH_SOCKET_DIR=str(runtime / "sockets"),
            DCH_NO_DETECT="1",
        )
        for inherited in (
            "DCH_SESSION",
            "DCH_NATIVE_BRIDGE_ID",
            "DCH_BRIDGE_SIDECAR_PID",
            "FAKE_CODEX_OLD",
        ):
            env.pop(inherited, None)
        (codex_home / "session_index.jsonl").write_text(
            "".join(
                json.dumps({"id": i, "thread_name": t}, separators=(",", ":"))
                + "\n"
                for i, t in ((THREAD, "Foreign Thread"), (OWN_THREAD, "Own Thread"))
            )
        )
        daemons = []

        def spawn(name, *args, **extra):
            log = root / (name + ".log")
            run = subprocess.run(
                [DCH, "--spawn", name, str(fake), *args],
                env=dict(env, FAKE_CODEX_LOG=str(log), **extra),
                capture_output=True,
                text=True,
                timeout=15,
            )
            return run.returncode == 0 and wait_until(lambda: log_line(log))

        def snapshot(thread, name, marker):
            path = snapshots / (thread + ".100.sh")
            path.write_text(
                "# snapshot\nexport DCH_SESSION=%s\n"
                "export DCH_NATIVE_BRIDGE_ID=%s\n" % (name, marker)
            )
            path.chmod(0o600)

        def published(marker):
            for path in sessions.glob("*.json"):
                try:
                    value = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if value.get("dchBridgeMarker") == marker:
                    return value
            return None

        def harness(name):
            out = subprocess.run(
                [DCH, "--ls-json"], env=env, capture_output=True, text=True,
                timeout=10,
            ).stdout
            return {e["name"]: e for e in json.loads(out)}.get(name, {}).get(
                "harness"
            )

        def daemon(name, marker):
            log = root / ("daemon-%d.log" % len(list(root.glob("daemon-*"))))
            detached(
                [str(fake), "app-server"],
                dict(env, DCH_SESSION=name, DCH_NATIVE_BRIDGE_ID=marker,
                     FAKE_CODEX_LOG=str(log)),
            )
            line = wait_until(lambda: log_line(log))
            if line:
                daemons.append(int(line[0]))
            return bool(line)

        try:
            line = spawn("cd-shared")
            check(
                "a Codex whose --help lists it gets --no-daemon",
                bool(line) and line[2:] == ["--no-daemon"] and line[1] != "-",
            )
            old = spawn("cd-old", "resume", "--last", FAKE_CODEX_OLD="1")
            check(
                "an older Codex without the flag keeps its argv",
                bool(old) and old[2:] == ["resume", "--last"],
            )
            sub = spawn("cd-exec", "exec", "hi")
            check(
                "a non-TUI subcommand keeps its argv",
                bool(sub) and sub[2:] == ["exec", "hi"],
            )
            if not line:
                return 1
            marker = line[1]
            check(
                "a detached Codex carries the session's identity",
                daemon("cd-shared", marker),
            )
            snapshot(THREAD, "cd-shared", marker)
            time.sleep(2.5)  # the sidecar polls every 250 ms
            check(
                "the sidecar publishes no peer while the daemon lives",
                published(marker) is None,
            )
            check(
                "--ls-json names nothing from the daemon's snapshot",
                harness("cd-shared") == "",
            )
            for pid in daemons:
                os.kill(pid, signal.SIGKILL)
            time.sleep(1.5)
            check(
                "a daemon seen once keeps the sidecar refusing",
                published(marker) is None,
            )

            own = spawn("cd-own")
            own_marker = own[1] if own else ""
            snapshot(OWN_THREAD, "cd-own", own_marker)
            record = wait_until(lambda: published(own_marker), timeout=10)
            check(
                "a session without a daemon publishes its own peer",
                bool(record) and record.get("name") == "Own Thread",
            )
            check(
                "and --ls-json names its own thread",
                harness("cd-own") == "Own Thread",
            )
            check(
                "a daemon of another session leaves it alone",
                daemon("cd-shared", marker) and harness("cd-own") == "Own Thread",
            )
            check(
                "a daemon carrying its identity refuses --agent-send",
                daemon("cd-own", own_marker)
                and "shared Codex daemon"
                in subprocess.run(
                    [DCH, "--agent-send", "anyone", "hello"],
                    env=dict(env, DCH_SESSION="cd-own",
                             DCH_NATIVE_BRIDGE_ID=own_marker),
                    capture_output=True,
                    text=True,
                    timeout=20,
                ).stderr,
            )
        finally:
            for pid in daemons:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            for name in ("cd-shared", "cd-old", "cd-exec", "cd-own"):
                subprocess.run([DCH, "-k", name], env=env, capture_output=True,
                               timeout=10)

    if failures:
        print("FAIL: %d check(s)" % len(failures))
        return 1
    print("PASS: shared Codex daemon guarded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
