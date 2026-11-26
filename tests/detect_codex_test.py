"""Codex 0.167 state detection: --status over painted Codex screens.

Screens are the strings of openai/codex rust-v0.167.2 (approval overlay,
MCP elicitation form, request_user_input footer, trust-folder and
hooks-review startup dialogs, resume cwd picker, transcript pager, busy
row, idle composer). A quiet `sh` with echo off paints each screen, then
--status is read with the output stamp fresh (5 s heuristic says working)
or stale (says idle), so only a detection rule can say otherwise.

Run:  python3 tests/detect_codex_test.py        (uses ./dch)
"""
import os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
FOOT = "  Press enter to confirm or esc to cancel"

# (id, screen, output age, expected --status): True fresh, False stale,
# or seconds ago; 10 s is past the 5 s heuristic, inside the 30 s a
# detected busy row accepts.
ROWS = [
    ("exec approval", """\
  Would you like to run the following command?

  Reason: clean the build directory

  $ rm -rf build

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for this command in this session (a)
  3. No, and tell Codex what to do differently (esc)

""" + FOOT, False, "blocked"),
    ("apply_patch approval", """\
  Would you like to make the following edits?

  Destination: src/main.rs

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for these files (a)
  3. No, and tell Codex what to do differently (esc)

""" + FOOT, False, "blocked"),
    ("network approval", """\
  Do you want to approve network access to "example.com"?

  Reason: fetch the crate index

› 1. Yes, just this once (y)
  2. Yes, and allow this host for this conversation (a)
  3. No, and block this host in the future (d)

""" + FOOT, False, "blocked"),
    ("cross-thread approval footer", """\
  Thread: worker [explorer]
  Would you like to run the following command?
  $ cargo test
› 1. Yes, proceed (y)

  Press enter to confirm or esc to cancel or o to open thread""",
     False, "blocked"),
    ("MCP elicitation form", """\
  Field 1/1
  Allow this request?
› 1. Allow   Run the tool and continue
  2. Allow for this session
  3. Always allow
  4. Cancel  Cancel this tool call

  enter to submit | esc to cancel""", False, "blocked"),
    ("request_user_input last question", """\
  Which database should the service use?
› 1. Postgres
  2. SQLite
  Type your answer (optional)

  tab to add notes | enter to submit all | esc to interrupt""",
     True, "blocked"),
    ("trust this folder", """\
  Folder access
  /work/project
  Trust this folder? Codex can read, edit, and run files here, subject to your permission settings.
› 1. Trust and continue
  2. Quit
  enter continue and create sandbox · esc quit""", False, "blocked"),
    ("hooks need review", """\
  Hooks need review
  2 hooks are new or changed.
  Hooks can run outside the sandbox after you trust them.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
  enter confirm · esc skip""", False, "blocked"),
    ("resume cwd picker", """\
  Working directory · Resume
› 1. Use session directory (/work/a)
  2. Use current directory (/work/b)
  3. Always use session directory
  4. Always use current directory""", False, "blocked"),
    # ordinary output quoting the same words mid-line is not a dialog
    ("approval footer quoted mid-line", """\
• The overlay says Press enter to confirm or esc to cancel at the bottom.
• The form says enter to submit | esc to cancel there.
• Startup asks: Trust this folder? Then: Hooks need review, then the
  picker Working directory · Resume when the cwd differs.

› Ask Codex to do anything
  ? for shortcuts                                       100% context left""",
     False, "idle"),
    # busy row; the interrupt key is configurable
    ("busy row", """\
• Working (12s • esc to interrupt)

› Ask Codex to do anything""", True, "working"),
    ("busy row with a remapped interrupt key", """\
• Thinking (3s • ctrl+g to interrupt)

› Ask Codex to do anything""", 10, "working"),
    ("busy row, frozen frame", """\
• Working (12s • esc to interrupt)

› Ask Codex to do anything""", False, "idle"),
    # the busy row is hidden while the answer streams: the idle composer
    # with fresh output reads working through the output heuristic
    ("streaming answer (busy row hidden)", """\
• Here is the first part of the answer, still streaming

› Ask Codex to do anything
  ? for shortcuts                                       100% context left""",
     True, "working"),
    ("idle composer", """\
• Done.

› Ask Codex to do anything
  ? for shortcuts                                       100% context left""",
     False, "idle"),
    # the transcript pager (Ctrl-T) replays an old approval: suppressed
    ("transcript pager replays an approval", """\
  Would you like to run the following command?
  $ rm -rf build
""" + FOOT + """
  ↑/↓ scroll  pgup/pgdn/⌃b/⌃f/⌃u/⌃d page  q close""",
     True, "working"),
]


def run(*args):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=20)


def main():
    tmp = tempfile.mkdtemp()
    os.environ.pop("DCH_SESSION", None)
    os.environ.pop("DCH_SOCKET_DIR", None)
    os.environ["XDG_RUNTIME_DIR"] = tmp
    act = os.path.join(tmp, "dch-%d" % os.getuid(), "cdx.sock.act")
    fails = 0
    try:
        subprocess.run([DCH, "--spawn", "cdx", "--size", "100x30", "sh"],
                       check=True, capture_output=True, timeout=20)
        run("--run", "cdx", "stty -echo")
        for i, (name, screen, fresh, want) in enumerate(ROWS):
            f = os.path.join(tmp, "s%d" % i)
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(screen + "\n")
            run("--run", "cdx", "printf '\\033c'; cat '%s'; echo sent_%d"
                % (f, i))
            run("--wait", "cdx", "--match", "sent_%d" % i, "--timeout", "5000")
            t = (time.time() if fresh is True else
                 946684800 if fresh is False else  # 2000-01-01
                 time.time() - fresh)
            os.utime(act, (t, t))
            got = run("--status", "cdx").stdout.strip()
            print("  %s %s" % ("ok  " if got == want else "FAIL", name))
            if got != want:
                print("       want %r\n       got  %r" % (want, got))
                fails += 1
    finally:
        run("-k", "cdx")
    print("PASS" if not fails else "%d FAIL" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
