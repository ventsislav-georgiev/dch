# dch — detachable terminal sessions, the simple way

A tiny terminal multiplexer built on top of [dtach][dtach]. No windows, no
panes, no tabs, no config file. One session per project, auto-named, attached
on demand. Close the laptop, drop the SSH, quit the terminal — your shell
keeps running.

The name is short for **ditch** — ditch your terminal, keep the session.

<img width="641" height="914" alt="image" src="https://github.com/user-attachments/assets/dbd94f7a-ecc6-42c3-a8dd-f348fffd8ebc" />


[dtach]: https://github.com/crigler/dtach

---

## What it is

`dch` wraps a small fork of `dtach` into one statically-built C binary. It
gives you the **one** thing screen/tmux/zellij are useful for — surviving
disconnect — and nothing else:

- **No windows / panes / tabs.** If you want those, use tmux.
- **One session per project.** Auto-named `<repo>-<branch>` when run from a
  git worktree, otherwise the directory basename.
- **Attach is idempotent.** Run `dch` again from the same dir — it reattaches
  the existing session instead of starting a duplicate.
- **Survives client death.** The master daemon is split from the client, so
  killing the terminal window (crash, GUI quit, SSH drop) leaves the shell
  alive. Open a new window and `dch` reattaches.
- **Agents can drive sessions headlessly.** Each session keeps an in-memory
  terminal mirror (via [libghostty-vt][ghostty]), so scripts and coding
  agents can send keys, read the rendered screen, and wait for output —
  without attaching, without a pty, without disturbing a human who *is*
  attached. See [Agent API](#agent-api).
- **No config file, no shell hooks.** One binary on `$PATH`.

[ghostty]: https://github.com/ghostty-org/ghostty

## Examples

The two forms you'll use 90% of the time are bare `dch` and `dch <cmd>` —
both auto-name the session from the current directory.

```sh
cd ~/code/coolapp              # git repo, currently on branch "feat-auth"
dch claude                     # → session "coolapp-feat-auth" if absent;
                               #   then runs claude inside the session

# new terminal, or after detach:
git checkout master
dch                            # → DIFFERENT session: "coolapp-master" (branch is part of the name)

# new terminal, or after detach:
cd /tmp/scratch                # not a git repo
dch                            # → session "scratch", starts default shell

dch -n release zsh             # → override auto-name; session "release"
dch -ls                        # → list every running session
dch -l                         # → pick one (TUI, arrow keys) and attach
dch -rl                        # → pick one, type a new display name, repeat
dch -k                         # → pick one and kill it
dch -kl                        # → kill ALL sessions
```

The auto-name uses cwd basename when you're not in a git repo, and
`<repo-toplevel>-<branch>` when you are — slashes in branch names get
replaced with `_` so they're safe.

`dch -rl` renames a session interactively: pick one, type a new display
name, and you're dropped back on the list to rename the next. The new name
is a display **alias** only — stored in a sidecar file next to the socket,
shown with priority in `-l`/`-rl` as `alias (real-name)`. The underlying
session name is untouched, so attach/kill/detach by real name keep working.
An empty input clears the alias; killing a session removes its alias too.

When a session is running a **named Claude Code session**, the picker shows
that name instead: `postman (solution.postman-fix_hop-asset-cache-host-var)`.
The join is exact — claude writes `~/.claude/sessions/<pid>.json` (pid, name)
for every live process, and that process's inherited `DCH_SESSION` env var
names the dch session it runs inside. Auto-titled claude names are skipped,
explicit aliases win. Resolution costs well under a millisecond, so it's
always on; `--ls-json` carries it as the `harness` field.

An agent (or plain shell script) can drive a TUI in a session it never
attaches to:

```sh
dch --spawn infra --size 120x40 k9s   # headless session running k9s
dch --wait infra --match "Pods" --timeout 15000   # block until it's up
dch --keys infra :                    # k9s command mode
dch --run infra deployments           # type the view name, press enter
dch --read infra                      # print the rendered screen, no attach
dch --read infra --ansi > snap.txt    # same, with colors
dch --read infra --cursor             # + "cursor <row> <col> ..." on stderr
```

## Install with Homebrew

```sh
brew install ventsislav-georgiev/tap/dch        # full: agent API included
brew install ventsislav-georgiev/tap/dch-lite   # ~100 KB, no terminal mirror
```

Two formulas, `conflicts_with` each other — pick one. **dch** embeds
libghostty-vt (~2 MB binary) and supports every verb in the
[Agent API](#agent-api). **dch-lite** is the classic ~100 KB
attach/detach tool: `--send`/`--run`/`--keys` still work (keys via a
legacy encoding), but `--read`/`--wait` need the mirror and exit 3.
The formulas are auto-published to the tap by dch's release workflow on
every `v*` tag.

## Quick install

```sh
git clone https://github.com/ventsislav-georgiev/dch.git
cd dch
./configure               # or: ./configure --without-libghostty  (lite build)
make
mkdir -p ~/.local/bin
install -m 0755 dch ~/.local/bin/dch     # or anywhere on $PATH
```

Make sure `~/.local/bin` is in your `$PATH`, then `dch -h` to confirm.

**Dependencies:** a C compiler (`clang` or `gcc`) and `autoconf`.
- macOS: `xcode-select --install` (clang + make) + `brew install autoconf`.
- Debian/Ubuntu: `sudo apt install build-essential autoconf`.

## Install with an agent

Point any coding agent (Claude Code, Cursor, Aider, …) at this link:

> **[`docs/AGENT_INSTALL.md`](docs/AGENT_INSTALL.md)**

…and say *"install dch by following this file"*. The agent will clone,
build, install to `~/.local/bin`, and verify in one go.

Once installed, agents should read the [Agent API](#agent-api) section —
it's the whole point of giving them dch.

## Cheatsheet

```sh
dch              # attach the auto-named session, or create it
dch <cmd...>     # same, but if new, run <cmd> in it
dch -ls          # list sessions
dch -l           # pick a session (arrow keys) and attach
dch -rl          # rename: pick a session, type a display alias, repeat
dch -k [name]    # kill a session (interactive picker if no name)
dch -kl          # kill ALL sessions
dch -d [name]    # detach all clients of a session (sends SIGUSR1)
dch -n <name>    # override the auto-name
dch -f           # force-attach even if another client is connected

# agent / scripting verbs (never attach; see Agent API)
dch --spawn <name> [--size CxR] [--env K=V]... [cmd...]  # headless session
dch --send <name> <text...>                     # type text into the session
dch --run <name> <text...>                      # type text, press enter
dch --keys <name> <key...>                      # send keys: ctrl+c up f2 ...
dch --read <name> [--ansi] [--recent [N]]       # print the rendered screen
dch --read <name> --cursor                      # ... + caret row/col on stderr
dch --wait <name> --match <str> [--timeout ms]  # block until output matches
dch --wait <name> --state <s[,s]> [--timeout ms]  # block until state matches
dch --status <name>                             # print session state
dch --report <name> <state>                     # push state (harness hooks)
dch --ls-json                                   # sessions as a JSON array
dch --restart <name>|--all [-f]                 # upgrade a live session in place
```

Inside a session: `Ctrl-\` to detach. Works in vim, fzf, less, Claude Code,
etc. — terminals that swallow most control keys still pass `Ctrl-\` through.

Press `Ctrl-\` **twice quickly** to switch sessions: dch detaches and shows the
same picker as `dch -l`, so you can hop straight into another session. Quitting
the picker (`q` / `Esc`) just leaves you detached. A single press still detaches
— it waits out the double-press window first. `DCH_DOUBLE_TAP_MS` sets that
window in milliseconds (default `300`); `DCH_DOUBLE_TAP_MS=0` turns switching
off and makes detach instant again.

## Agent API

Every dch session keeps an in-memory **terminal mirror**: the master feeds
each pty byte into an embedded [libghostty-vt][ghostty] terminal (Ghostty's
VT engine as a C library, no rendering). The mirror is what `--read` prints
and `--wait` scans, and it tracks the modes (kitty keyboard protocol,
application cursor keys) that `--keys` uses to encode keys exactly the way
the running app expects.

Control verbs use a dedicated connection type: they never attach, never
allocate a pty, and never echo to attached humans. Reading a session that
someone is watching is invisible to them.

### Verbs

| Verb | Does | Exit codes |
| --- | --- | --- |
| `--spawn <n> [--size CxR] [--env K=V]... [cmd...]` | Start headless session (default: `$SHELL`, 80x24 or `$COLUMNS`/`$LINES`). `--env` (max 32, first `=` splits) adds variables to the child; reserved keys (`DCH_SESSION`, `TERM` fixup) always win. Prints the name. | 0 ok, 1 exists/failed/bad --env |
| `--send <n> <text...>` | Type text (joined with spaces), no enter. | 0 ok |
| `--run <n> <text...>` | `--send` + enter. | 0 ok |
| `--keys <n> <key...>` | Encode+send keys mode-aware: `ctrl+c`, `alt+x`, `shift+tab`, `up`, `f5`, `enter`, `esc`, single chars. | 0 ok, 1 unknown key |
| `--read <n> [--ansi] [--cursor] [--recent [N]]` | Print rendered screen. `--ansi` keeps colors; `--recent [N]` = last N lines incl. scrollback (default 100); `--cursor` also prints `cursor <row> <col> <visible> <wrap>` on **stderr** (see below). Plain output drops all styling — to tell a TUI's own ghost text (placeholders, argument hints) from what the human typed, read with `--ansi --cursor`; see [docs/ghost-text.md](docs/ghost-text.md). | 0 ok, 1 error, 3 no mirror |
| `--wait <n> --match <str> [--timeout ms]` | Block until screen/scrollback contains `<str>` (literal substring, ≤512 bytes); prints the matching line. Default timeout 10 s. | 0 hit, 2 timeout, 3 no mirror |
| `--status <n>` | Print the session state: `working`, `idle`, `blocked`, or `done` — resolved from the reported state, the screen-content detection, and the output heuristic (see [Agent state detection](#agent-state-detection)). | 0 ok, 1 no session |
| `--report <n> <state>` | Push a semantic state: `working`, `idle`, `blocked`, or `done` (closed set, unknown tokens rejected); `clear` reverts to auto (detection + heuristic). Last write wins; cleared automatically when the session ends. | 0 ok, 1 no session / bad state |
| `--wait <n> --state <s[,s...]> [--timeout ms]` | Block until the state matches any of the comma list (e.g. `idle,blocked,done` = "turn over"); prints the matched state. `active` is accepted as an alias for `working`. Polls every 100 ms. | 0 hit, 2 timeout, 1 no session |
| `--ls-json` | All sessions as JSON: `[{"name","alias","harness","activity_epoch","state","version"}]`. `harness` is the name of the Claude Code session running inside, or `""`. `state` follows the same resolution rule as `--status`. `version` is the dch version of the master serving that session, or `""` for a master old enough not to report one. | 0 |
| `--restart <n>` \| `--restart --all [-f]` | Re-exec the session's master onto the current `dch` binary, in place — the program inside keeps running (see [Upgrading a live session](#upgrading-a-live-session)). `--all` restarts only sessions whose `version` differs from this binary's; `-f` restarts all of them. | 0 ok, 1 failed / master too old |

A typical agent loop:

```sh
dch --spawn build --size 120x40
dch --run build "make -j8 2>&1 | tee build.log"
if dch --wait build --match "error:" --timeout 300000; then
    dch --read build --recent 40      # grab context around the failure
fi
```

### Cursor position (`--read --cursor`)

A tool that paints the screen dump somewhere else — its own mirror pane, a
web view — also has to place the caret. Inferring it from the byte stream
gets it wrong the moment the app moves the cursor with anything but plain
printing. `--cursor` asks the mirror instead:

One call, two streams — the screen on stdout, the caret on stderr:

```sh
cur=$(dch --read ui --cursor 2>&1 >screen.txt)   # "cursor 7 12 1 0"
```

Keep it to a single invocation: two reads would put the screen at one instant
and the caret at another, which is the misplacement this flag exists to
remove. (Capturing both in variables needs a third descriptor:
`exec 3>&1; cur=$(dch --read ui --cursor 2>&1 1>&3); exec 3>&-`.)

Fields are `row col visible wrap`: 1-based like `CUP` (`\e[row;colH`), `row`
counted from the top of the visible screen, `visible` = DEC mode 25, and
`wrap` = 1 when `col` is the last column and the *next* printed character
soft-wraps to the next row — the caret still sits on `col`, not past it.

It comes from the *same* snapshot as the screen on stdout — one round trip,
nothing can move in between — and stdout stays byte-identical, so existing
consumers are unaffected. Not valid with `--recent` (the caret belongs to the
visible screen, not a scrollback tail). Exit 1, with the caret withheld, if
the master predates the flag or the screen was big enough to truncate the
response (over 2 MB: the kept tail no longer starts at screen row 1, so the
row would be a lie).

`row` counts *screen* rows and `--read` trims trailing blank lines, so a
caret parked below the last line of content reports a `row` larger than the
number of lines printed — pad the pane out to it instead of clamping to the
line count. Leading and interior blank rows are preserved, so row `N` of the
report is line `N` of the dump whenever the dump is that long.

Still a snapshot, though: bytes the app emits *after* the read move the real
caret, so a client that reads, paints, then types needs another read rather
than trusting the old coordinate.

### Agent state detection

`--status` and `--ls-json` answer "what is this agent doing?" with zero
setup: dch reads the session's rendered screen (the same mirror `--read`
prints) and looks for the strings coding-agent TUIs paint when they are
busy or waiting on a human.

| State | Signal |
| --- | --- |
| `blocked` | Permission/confirmation prompts: "Do you want to proceed?", "Allow command?", "Press enter to confirm or esc to cancel", "permission required", "waiting for approval", … (Claude Code, Codex, Gemini, opencode, Cursor) |
| `working` | Busy footers ("esc to interrupt", "ctrl+c to interrupt") or an animated braille spinner at the start of a line — only honored if the session also produced output in the last 30 s, so a frozen frame can't read as busy |
| `idle` / `working` | Fallback output heuristic: pty output within the last 5 s = `working`, else `idle` |

Resolution order, per query:

1. A **reported** `done` (see below) always wins — no screen can prove completion.
2. A **detected** `blocked` beats any reported state except `done`: a live permission prompt on screen is ground truth, and this also rescues sessions whose harness hook died mid-turn and left a stale `working` behind.
3. Otherwise: reported state, else detected state, else the output heuristic.

Honest limits: detection reads the terminal body text, not window titles,
so a harness that shows its busy indicator only in the title falls back to
the output heuristic (still a correct busy/quiet answer, just without
screen confirmation). The rules are English-only and can lag a harness UI
redesign — updating dch updates the rules for every session, nothing else
to install. Transcript/pager views that replay old prompts are recognized
and suppressed, but a program that happens to print a matching string can
still false-positive. `DCH_NO_DETECT=1` in the client's environment
disables detection entirely (reported state + heuristic only).

Detection needs the session master's VT mirror: against a lite or
`DCH_NO_VT=1` master it falls back to the reported state + heuristic. A
lite *client* against a full master detects normally.

For multi-agent runs, name sessions `<run>.<role>` (e.g. `crew1.planner`,
`crew1.coder`) and filter `--ls-json` by prefix — dch keeps a flat
namespace on purpose.

### Optional: explicit state reporting

Detection can't see everything — `done` in particular is a semantic fact
only the harness knows. A harness running inside the session can push its
state with `--report` (herdr-style): `working`, `idle`, `blocked`, `done`.
A reported state overrides the heuristic (and, except for a live on-screen
permission prompt, the detection) until the next report,
`--report <name> clear`, or session end (the sidecar is removed with the
socket).

Every process inside a dch session has `DCH_SESSION` set to the session
name, so hooks need zero configuration. Reports must never break the
harness: silence stderr and swallow the exit code, as below.

**Claude Code** (`~/.claude/settings.json`) — each entry is
`{"hooks": [{"type": "command", "command": "<cmd>"}]}` with:

| Hook event | Command |
| --- | --- |
| `UserPromptSubmit`, `PreToolUse` | `dch --report "$DCH_SESSION" working 2>/dev/null; true` |
| `Notification` | `dch --report "$DCH_SESSION" blocked 2>/dev/null; true` |
| `Stop` | `dch --report "$DCH_SESSION" idle 2>/dev/null; true` |
| `SessionEnd` | `dch --report "$DCH_SESSION" clear 2>/dev/null; true` |

`PreToolUse` flips `blocked` back to `working` once a permission prompt is
approved; `SessionEnd` clears the report so a closed harness can't leave a
stale `working` behind (a hard-killed one can, until the next report or
session end — same trade-off herdr makes, which uses process-exit rather
than a timeout to expire state).

**OpenAI Codex** (`~/.codex/config.toml`) — Codex only signals
turn-complete:

```toml
notify = ["sh", "-c", "dch --report \"$DCH_SESSION\" idle 2>/dev/null; true"]
```

**Anything else**: call `dch --report "$DCH_SESSION" <state>` from the
harness's hook/notify mechanism. Outside a dch session `DCH_SESSION` is
empty and the report fails silently.

Then a watcher blocks on the state instead of polling screens:

```sh
dch --wait agent1 --state idle,blocked,done --timeout 600000  # turn over?
```

### Honest caveats

- **The mirror is a model, not a screenshot.** libghostty-vt implements the
  same VT emulation Ghostty ships, so it's accurate for real-world TUIs
  (vim, k9s, htop, fzf) — but an app probing exotic terminal features could
  in principle render differently on the mirror than on your terminal.
- **`--wait` matches rendered text**, post-VT-processing: a spinner that
  redraws in place produces one line, not hundreds.
- **Timing is yours to handle.** `--send` types instantly; TUIs that debounce
  input may need a `--wait` between verbs, exactly like a human pausing.

## Upgrading a live session

A session is served by a master process that was started by whichever `dch`
binary spawned it. Upgrade `dch` and the sessions you already have keep running
the old code — new features simply aren't there until the session is recreated,
which is exactly what you don't want to do to a long-running agent.

`dch --restart <name>` fixes that without killing anything:

```sh
brew upgrade dch
dch --restart --all      # every session not already on this version
```

The master serialises its state, then replaces its own image with
`execv(2)` — no fork. Its pid, its pty, the program running inside it, the
listening socket and every attached client's connection all survive, because
file descriptors and process identity are preserved across `exec`. The image it
execs is the `dch` that ran the command, not the path the session was started
from: on a versioned prefix (Homebrew's Cellar, the Nix store) those differ
after an upgrade, and re-execing the old path would report success and change
nothing. The `--all` form reads the `version` field of `--ls-json` and skips
sessions already current; `-f` restarts them anyway.

If the re-exec fails (binary missing mid-upgrade, say), `execv` simply
returns and the old master carries on serving — the rollback is free. `dch
--restart` reports which of the two happened, and the acknowledgement it waits
for comes from the *new* image, so exit 0 means the new binary is serving.

Two things do not survive: scrollback held only in the master's mirror (the
visible screen does), and a master too old to know the verb at all — those
sessions have to be recreated once, and only once. `--restart --all` reports
those separately and does not count them as failures, since on the first
upgrade to a `dch` that has live restart, that is every session you have.

### Attaching replays the screen

Attaching sends you the session's current screen from the master's mirror
before anything else. dtach's original contract was to clear your screen and
ask the program to repaint; diff-based renderers — Ink, and so Claude Code —
don't repaint, because they compare against their own model of a screen dch
just invalidated behind their back. The result was a blank attach that only
filled in when you resized the window. Set `DCH_NO_REPLAY=1` in the master's
environment at spawn to get the old behavior back.

### dch-lite and `DCH_NO_VT`

The mirror can be absent: the **dch-lite** build (`./configure
--without-libghostty`) omits it, and `DCH_NO_VT=1` in the master's
environment disables it at spawn.

| Verb | Without mirror |
| --- | --- |
| `--spawn`, `--send`, `--run`, `--status`, `--report`, `--wait --state`, `--ls-json` | Work unchanged (plain pty writes / sidecar reads). State detection needs the master's mirror, so against a lite/`DCH_NO_VT` master `--status` falls back to reported state + output heuristic. |
| `--read`, `--wait --match` | Exit 3 with a message pointing at the full build. |
| `--keys` | Falls back to a fixed legacy xterm table client-side (exit 0 + stderr note). Mode-aware apps may misread modified keys. |

The client side is protocol-only in both builds: a lite `dch` binary can
drive every verb against a session spawned by a full `dch` master.

## How it differs from upstream dtach

This fork (the `dch.c` entry point + small `attach.c` / `master.c` patches)
adds:

- Auto-named sessions (`<repo>-<branch>` or cwd basename), parsed natively
  from `.git/HEAD` — no `git` fork/exec.
- Static-TUI session picker for `-l` / `-k` / `-d` (no external dep like
  `gum` or `fzf`).
- Server/client split so the daemon survives the client closing.
- Per-client PID files for orphan reaping (PPID=1, no controlling TTY).
- Whole-buffer scan for the detach char and `VSUSP` — fixes upstream bug
  where bursty/pasted input could swallow the keypress.
- 16 KB replay buffer (upstream is 4 KB).
- Terminal restore on exit clears mouse-tracking / bracketed-paste /
  alt-screen state that inner apps (vim, fzf) leave on.
- An in-master terminal mirror (embedded libghostty-vt) plus an agent
  control protocol: `--spawn`, `--send`, `--run`, `--keys`, `--read`,
  `--wait`, `--ls-json` — drive and observe TUI sessions without attaching.

## License

GPLv2 (inherited from dtach). See `COPYING`.
