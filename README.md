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
dch -ls                        # → list every session as display-name (real-name)
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

When a session is running a **named Claude Code or Codex session**, the picker
and `-ls` show that name instead: `postman (solution.postman-fix_hop-asset-cache-host-var)`.
The join is exact and live: Claude supplies `~/.claude/sessions/<pid>.json`;
Codex supplies titles in `~/.codex/session_index.jsonl` and current releases
supply the thread ID through the matching shell-snapshot filename (older
releases' process environment IDs remain supported). In both cases dch also
requires a live harness process with the matching `DCH_SESSION`, so old records
and a title for another dch session do not overlay. Claude auto-titles and empty
Codex titles are skipped; Codex does not mark derived titles separately.
Explicit aliases win. `--ls-json` keeps the real `name`, `alias`, and resolved
`harness` as separate fields.

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
brew install ventsislav-georgiev/tap/dch        # terminal mirror included
brew install ventsislav-georgiev/tap/dch-lite   # no terminal mirror
```

Two formulas, `conflicts_with` each other — pick one. **dch** embeds
libghostty-vt (~2 MB binary). Both builds support the [Agent API](#agent-api).
**dch-lite** omits only the terminal mirror: `--send`/`--run`/`--keys` still
work (keys via a legacy encoding), but `--read` and `--wait --match` need the
mirror and exit 3.
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
dch -ls          # list sessions as alias/harness (real-name), or real-name
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
dch --meta <name> '<json object>'|clear         # attach metadata (tasks, PRs, ...)
dch --ls-json                                   # sessions as a JSON array
dch --restart <name>|--all [-f]                 # upgrade a live session in place
dch --restart <name>|--all --include-agents [-f]  # ...then restart its stale Claude agents
dch --respawn <name> [-- cmd...]                # on child exit, start cmd in its place
dch --respawn <name> --cancel                   # drop a pending respawn
dch --cmd <name>                                # child pid, state, argv, exit status (JSON)
dch --history [--json]                          # sessions that ended, and how
dch --revive <name> [--as <new>]|--all          # bring ended sessions back, claude resumed
dch --say <name>...|--all [--json] -- <text...>  # message each idle Claude Code, never over a prompt
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
| `--spawn <n> [--size CxR] [--env K=V]... [cmd...]` | Start headless session (default: `$SHELL`, 80x24 or `$COLUMNS`/`$LINES`). `--env` (max 32, first `=` splits) adds variables to the child; reserved keys (`DCH_SESSION`, `TERM` fixup) always win. Prints the name. A program that cannot be executed is an error (`dch: <n>: <cmd>: <reason>`); one that runs and exits at once is not. | 0 ok, 1 exists/failed/not executable/bad --env |
| `--send <n> <text...>` | Type text (joined with spaces), no enter. | 0 ok |
| `--run <n> <text...>` | `--send` + enter. | 0 ok |
| `--keys <n> <key...>` | Encode+send keys mode-aware: `ctrl+c`, `alt+x`, `shift+tab`, `up`, `f5`, `enter`, `esc`, single chars. | 0 ok, 1 unknown key |
| `--read <n> [--ansi] [--cursor] [--recent [N]]` | Print rendered screen. `--ansi` keeps colors; `--recent [N]` = last N lines incl. scrollback (default 100); `--cursor` also prints `cursor <row> <col> <visible> <wrap>` on **stderr** (see below). Plain output drops all styling — to tell a TUI's own ghost text (placeholders, argument hints) from what the human typed, read with `--ansi --cursor`; see [docs/ghost-text.md](docs/ghost-text.md). | 0 ok, 1 error, 3 no mirror |
| `--wait <n> --match <str> [--timeout ms]` | Block until screen/scrollback contains `<str>` (literal substring, ≤512 bytes); prints the matching line. Default timeout 10 s. | 0 hit, 2 timeout, 3 no mirror |
| `--status <n>` | Print the session state: `working`, `idle`, `blocked`, or `done` — resolved from the reported state, the screen-content detection, and the output heuristic (see [Agent state detection](#agent-state-detection)). | 0 ok, 1 no session |
| `--report <n> <state>` | Push a semantic state: `working`, `idle`, `blocked`, or `done` (closed set, unknown tokens rejected); `clear` reverts to auto (detection + heuristic). Last write wins; cleared automatically when the session ends. | 0 ok, 1 no session / bad state |
| `--meta <n> '<json>'` | Attach one JSON object of metadata to the session (see [Optional: session metadata](#optional-session-metadata)); `clear` removes it. Replaced atomically, last write wins, never interpreted; a byte-identical push is a no-op; rejected input leaves the stored object unchanged. Removed automatically when the session's program exits. | 0 ok, 1 no session / not a single-line JSON object ≤ 16 KB |
| `--wait <n> --state <s[,s...]> [--timeout ms]` | Block until the state matches any of the comma list (e.g. `idle,blocked,done` = "turn over"); prints the matched state. `active` is accepted as an alias for `working`. Polls every 100 ms. | 0 hit, 2 timeout, 1 no session |
| `--ls-json` | All sessions as JSON: `[{"name","alias","harness","activity_epoch","state","version","agents","meta","meta_epoch"}]`. `harness` is the name of the Claude Code or Codex session running inside, or `""`; the real `name` remains separate. `state` follows the same resolution rule as `--status`. `version` is the dch version of the master serving that session, or `""` for a master old enough not to report one. `agents` is the number of live subagents on Claude Code's agent panel (the `⏺ main` block under the prompt), `0` when no panel is on screen — including while a prompt covers it — or `DCH_NO_DETECT=1` is set. `meta` is the object last pushed with `--meta`, verbatim, or `null`; `meta_epoch` is when that content last changed (epoch seconds, like `activity_epoch`), `0` with no meta. | 0 |
| `--restart <n>` \| `--restart --all` `[--include-agents] [-f]` | Re-exec the session's master onto the current `dch` binary, in place — the program inside keeps running (see [Upgrading a live session](#upgrading-a-live-session)). `--all` restarts only sessions whose `version` differs from this binary's; `-f` restarts all of them. With `--include-agents`, it then restarts the stale Claude Code agents of those sessions — every session with `--all`, only `<n>` otherwise; `-f` forces current agents too (see [Restarting Claude Code agents](#restarting-claude-code-agents)). Without the flag no agent is touched, `--all` included. `--include-agents` without `--restart` is a usage error. | 0 ok, 1 failed / master too old / an agent restart failed |
| `--respawn <n> [-- cmd...]` | Arm a one-shot respawn: when the session's program next exits, the master keeps the session (same socket, pty size, attached clients) and starts `cmd` in its place. No `cmd` arms a *hold*: the session stays up childless until a second `--respawn <n> -- cmd` starts one, or until `DCH_HOLD_MS` (default 120000) ends it. An arm that nothing consumes expires after `DCH_RESPAWN_TTL_MS` (default 300000); both are read from the master's environment. `--restart` refuses while a respawn is armed or held. | 0 ok, 1 no session / refused / master too old |
| `--respawn <n> --cancel` | Drop a pending arm. On a held (childless) session it ends the session. | 0 ok, 1 no session |
| `--cmd <n>` | Print `{"pid","state","argv","exit_code","exit_signal","tail"}`: the child's pid (`null` while held), `state` `running`, `armed` or `held`, the argv it was started with, the exit status of a child reaped under an arm (`null` otherwise), and `tail`, the last 8 KB the child wrote since the arm, raw. | 0 ok, 1 no session / master too old |
| `--history [--json]` | List the sessions that ended, newest first, one line each: `name (alias)  when  end  cwd  $ argv  [claude <name> <session id>]`. `end` is `exit N`, `signal N`, `killed` (`dch -k`) or `vanished` (the master died without a word: SIGKILL, power loss). Only the records of the current socket dir are listed. `--json` prints the stored records as an array (see [Bringing sessions back](#bringing-sessions-back)). | 0 |
| `--revive <n> [--as <new>]` \| `--revive --all` | Start ended sessions again: in their cwd, with their argv, with the Claude Code agent in them resumed via `--resume <id>` and told to carry on (see [Bringing sessions back](#bringing-sessions-back)). `<n>` revives that one, whatever its end; a live `<n>` is refused unless `--as <new>` names another session. `--all` revives every session that died — anything but `exit 0` and `killed` — within the last 3 days after running at least 5 minutes, and gives one whose name is now taken the first free `<n>-2`, `<n>-3`, …. Older or shorter-lived sessions stay in `--history` and revive by name. One line per session. | 0 ok, 1 no history / name live / a revive or resume failed |
| `--say <n>... [--json] -- <text...>` \| `--say --all [--json] -- <text...>` | Type one message into the Claude Code running in each session — as its command or typed into its shell — and submit it, under the same gate as `--include-agents`: only when Claude Code's session record says `idle` and the screen shows its empty composer, never over a prompt, a draft or a queued message, and never waiting for a busy Claude. Text is the words after the required `--` joined with spaces. Sessions are typed into concurrently; Enter is pressed again while the text still sits in the composer. One result per session as it finishes: `<n>: sent`, `<n>: skipped — <why>` (`not_claude` under `--all`, `self` = `$DCH_SESSION`, `busy`, `prompt`, `draft`, `queued`, `no_screen` — always on dch-lite) or `<n>: failed — <why>` (`no_session`, `not_submitted`, `not_typed`, `old_master`). `--json` prints one `{"name","result":"ok\|skipped\|failed","reason","detail"}` object per line instead. | 0 nothing failed (skips included), 1 usage error / a session failed |

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
| `blocked` | Permission/confirmation prompts: "Do you want to proceed?", "Esc to cancel · Tab to amend" (Write/Edit), "Allow command?", "Press enter to confirm or esc to cancel", "permission required", "waiting for approval", … (Claude Code, Codex, Gemini, opencode, Cursor) |
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

### Optional: session metadata

A program inside a session can publish a small JSON object about itself —
a status line's task counts, the PRs an agent has open — and every
`--ls-json` consumer sees it as `meta`, next to `state`. dch stores and
passes it through; it never interprets the keys.

Push it from a status line, a hook, or any script running in the session.
`DCH_SESSION` is set in every dch session:

```sh
dch --meta "$DCH_SESSION" '{"tasks":{"done":3,"total":5,"in_progress":1,"queued":1}}' 2>/dev/null; true
```

Re-push periodically or on every render rather than relying on a single
push: dch may have missed one, and a respawned program starts with none (dch
clears it on exit). Pushing unchanged content is cheap and safe. dch ignores
a byte-identical push and keeps `meta_epoch` at the time the content last
changed, which it stamps itself. Like reports, a push must never break its
caller: silence stderr and swallow the exit code.

**Rules dch enforces** (anything else exits 1 and keeps the stored object):

- one JSON object (`{...}`), strictly valid, UTF-8;
- at most 16384 bytes;
- at most 1024 JSON tokens. Every object, array, key, string, number and
  `true`/`false`/`null` counts as one, so `{"a":[1,2]}` is 5. That is room
  for about 1000 values; a list of 50 four-field PR objects is about 450;
- a single line: no byte below 0x20 (no raw newline or tab), which keeps
  `--ls-json` one line. Compact `JSON.stringify` / `json.dumps` output
  qualifies.

**Standard keys.** All are optional, and consumers must tolerate any of them
being absent. Other top-level keys pass through unchanged, so give your own
distinctive names.

| Key | Shape | Meaning |
| --- | --- | --- |
| `tasks` | `{"done":int,"total":int,"in_progress":int,"queued":int}`, non-negative integers | `done` finished items out of `total`; `in_progress` items being worked now; `queued` open items not in progress, blocked or parked included. |
| `prs` | `{"open":[{"url","repo","num","title"}],"merged":int}` | `open` lists the not-yet-merged PRs, at most 50, e.g. `{"url":"https://github.com/o/r/pull/12","repo":"o/r","num":12,"title":"..."}`, with `url` an absolute `https` link. `merged` is a count. |

**Clearing.** The metadata describes the program running in the session, so
dch removes it when that program exits (including the exit before a
`--respawn` relaunch), when the session is killed, and when its master
exits. An agent started by hand inside a shell session outlives none of
those, so clear from its exit hook — for Claude Code, a `SessionEnd` hook:

| Hook event | Command |
| --- | --- |
| `SessionEnd` | `dch --meta "$DCH_SESSION" clear 2>/dev/null; true` |

**Freshness.** `meta_epoch` is the time the content last changed, not a
liveness signal: a status line typically redraws only on activity, and
identical re-pushes leave it alone, so an idle agent's metadata can be hours
old and still current. Pair it with `state` and
`activity_epoch` for liveness.

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
dch --restart --all --include-agents   # ...and their stale Claude agents
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

### Restarting Claude Code agents

Upgrading Claude Code leaves every running `claude` on the old version until it
is restarted. After its master pass, `dch --restart --all --include-agents`
does that for each session whose program is Claude Code, without losing the
conversation; `dch --restart <name> --include-agents` does it for that one
session only. The step is opt-in: without `--include-agents`, `--restart`
(`--all` included) restarts masters only and leaves every agent alone.

1. **Find.** A session qualifies when its command (`dch --cmd`) has a `claude`
   token — directly or behind a wrapper such as `headroom wrap claude --chrome`
   — and a live Claude session record (`~/.claude/sessions/<pid>.json`, or
   under `$CLAUDE_CONFIG_DIR`) belongs to a descendant of the session's child.
   The record must be interactive, its pid alive with a matching start time,
   and its working directory the session's. The agent is *stale* when the
   record's version differs from what that `claude` (resolved on the master's
   `PATH`) reports for `--version`; `-f` restarts current agents too.
2. **Pause.** Each stale agent gets the pause message, typed only when its
   composer is on screen and empty — never over a draft, a queued message, a
   permission prompt or a dialog. Enter is pressed only once the text shows
   in the composer row (or Claude's `[Pasted text #N]` placeholder does); a
   prompt that comes up in between gets no Enter, and the agent is reported
   as failed. The same check guards `/exit` and the resume message. All
   agents are paused first, then waited on together.
3. **Wait.** An agent is ready when its record says `idle`, it has visibly
   taken the pause message (it went busy, or its record changed after the
   message with nothing left queued), no subagents show on the agent panel,
   and all of that holds for a settle window. A background shell or monitor
   keeps the record off `idle`.
4. **Restart.** dch arms a hold on the session, types `/exit`, and reads the
   session id from the last `claude --resume <id>` line Claude prints on exit.
   It then starts the original command again, with `--resume <id>` inserted
   right after the `claude` token. Any `--resume`, `-r`, `--continue`, `-c`,
   `--session-id` and `--fork-session` from the original command are dropped.
   The session keeps its name, socket and attached clients.
5. **Resume.** Once the new Claude is idle with an empty composer, it gets the
   resume message, and dch checks that it now reports the installed version.

Each agent gets one result line. The command exits 1 if any agent it tried to
restart failed.

The step waits up to `DCH_AGENT_TIMEOUT_MS` (10 minutes by default) for an agent
to pause. Run it from a Claude Code session with `run_in_background`, because
the Bash tool times out after 2 minutes. Ctrl-C, SIGTERM or SIGHUP do not leave
agents paused. An agent still waiting to pause gets the resume message and is
reported as interrupted. An agent already past `/exit` finishes its restart,
within `DCH_AGENT_STEP_MS`. A second signal exits at once. SIGKILL cannot be
caught: it leaves a waiting agent paused, and you must type the resume
message yourself. If a second signal lands after Claude has already exited
into the hold, nothing is relaunched and the session ends when `DCH_HOLD_MS`
runs out (default 120 s). To recover, run `claude --resume <id>` by hand; the id
is in the `tail` field of `dch --cmd <session>` until the hold ends. A failed
agent is left running, never killed: that covers a
gate that refused to type, a timeout (the resume message is typed if
possible), or an `/exit` that did not end Claude (the hold is cancelled). If
Claude crashed on `/exit` (a signal or a non-zero exit), it is not relaunched
and the session ends.

| Env (client side) | Default | Meaning |
| --- | --- | --- |
| `DCH_PAUSE_MSG` | `pause all work, i need to restart you. …` | Text typed to pause an agent. |
| `DCH_RESUME_MSG` | `resume all work. …` | Text typed after the relaunch. |
| `DCH_AGENT_TIMEOUT_MS` | `600000` | How long an agent may take to pause. |
| `DCH_AGENT_SETTLE_MS` | `3000` | How long it must stay ready before `/exit`. |
| `DCH_AGENT_LAND_MS` | `3000` | How long dch waits for typed text to show in the composer before giving up without pressing Enter. |
| `DCH_AGENT_STEP_MS` | `60000` | Limit for `/exit` to end Claude and for the relaunch to show a composer. |
| `DCH_AGENT_REENTER_MS` | `5000` | After Enter, how long a message may stay in the composer before Enter is pressed again (at most 5 more times, never on a dialog). A just-resumed Claude can drop the first Enter. |

Limitations:

- **Claude Code only.** Codex sessions are left alone.
- **Started by hand.** A `claude` started by hand from a shell inside a
  session is skipped with a note: there is no command to relaunch.
- **Positional prompt.** A positional prompt argument (`claude "fix the
  tests"`) is kept and sent again on resume. dch cannot tell it apart from an
  option value cheaply.
- **Own session.** The session running `dch --restart --include-agents` itself
  is skipped. Run it from outside dch to restart that one.
- **dch-lite.** dch-lite (no mirror) skips this step. Given
  `--include-agents`, it prints a note when some session runs claude.
- **Respawn pending.** A session with a pending `--respawn` (armed or held)
  is skipped.
- **Turn-start race.** A turn can start between the last idle check and
  `/exit`, for example when a background task finishes at that moment. Claude
  may then queue `/exit` behind that turn. If the turn ends within
  `DCH_AGENT_STEP_MS`, the restart goes through as usual. If not, dch cancels
  the hold and reports the agent as not restarted. When the queued `/exit`
  runs later, Claude quits and the session ends with it. The composer check
  narrows this window but cannot close it: typing and the agent's next turn
  are not atomic.

### Bringing sessions back

Every master keeps a record of its session in
`$XDG_STATE_HOME/dch/sessions/<slug>/<name>.json` (`~/.local/state/dch/sessions`
when `XDG_STATE_HOME` is unset or relative): where it ran, what it ran, and
the Claude Code agent inside it. `<slug>` is the socket dir with each `/` as
`-` (`/tmp/dch-502` → `-tmp-dch-502`), so sessions in a test's own
`XDG_RUNTIME_DIR` or `DCH_SOCKET_DIR` never mix with, overwrite or get revived
as the real ones; `--history` and `--revive` read only the current socket
dir's records. A relative socket dir keeps no records. It is written at start, refreshed every
`DCH_RECORD_MS` (60000) while the session runs, and closed with how it
ended. A reboot, a crash or a SIGKILL ends sessions without a word; the
record outlives them.

```sh
dch --history                # what ended, when, how, where
dch --revive work            # bring one back
dch --revive work --as work2 # ...under another name
dch --revive --all           # every session that died lately, see below
```

`--all` takes a session only when all three hold: it ended abnormally
(anything but `exit 0` and `killed`, `vanished` included), it was last seen
within the last 3 days, and it ran at least 5 minutes (last seen − started;
last seen is `ended`, else `updated`). That leaves out short test sessions
that end on a signal on purpose, and last week's sessions. They are still in
`--history`; `dch --revive <name>` takes any record.

A revived session starts in its recorded cwd with its recorded argv. If the
argv runs `claude` (directly or through a wrapper such as `headroom wrap
claude`), `--resume <id>` goes in after the `claude` word and any old
`--resume`/`--continue` comes out. If claude was typed into a shell inside the
session, the shell comes back and dch types the launch command again: `cd
<claude's cwd> && <the command> --resume <id>`. Then, on the full build, dch
waits for the composer and types a message telling the agent the session
ended and to re-read its ledger, re-arm its monitors and carry on
(`DCH_REVIVE_MSG` replaces it). dch-lite revives and resumes but cannot see the
composer, so it says the message was not sent. The caller's own Claude
identity (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, …) is
removed from the revived session's environment, so `--revive` works from
inside an agent.

A session whose directory is gone, or whose program cannot be executed, is
reported and skipped; the others still come back. A revive under a new name retires the old record. Records older
than 30 days are pruned, in every socket dir's subdir, when a master starts,
and subdirs left empty are removed.

The record is one line of JSON, mode 0600 in a 0700 directory:

```json
{"name":"work","sock_dir":"/tmp/dch-502","alias":"My work","cwd":"/src/app","argv":["/opt/homebrew/bin/headroom","wrap","claude"],
 "started":1761400000,"updated":1761403600,"ended":1761403612,"end":"signal 9",
 "agent":{"kind":"claude","session_id":"…","name":"fix-tests","cwd":"/src/app",
          "launch":["/usr/local/bin/headroom","wrap","claude"]}}
```

`sock_dir` is the socket dir the session lived in; a record whose `sock_dir`
is not the current one is ignored. `ended` is `0` and `end` `""` while the
session runs (and after a master that
died without writing them; `--history` shows that as `vanished`). `agent` is
`null` without a Claude Code agent; `launch` is the argv of the process the
master's child started it from — the child itself for a command session, the
typed command for a shell session.

A bare `argv[0]` is stored as the path the session's PATH found it on (a
symlink is kept, not resolved), so a revive from a thinner PATH, such as a
launchd-started app's, still finds it; one not found stays bare. The argv is
otherwise stored as given, so a secret passed on a command line lands in
the record the way it lands in shell history. The environment is never
stored. `DCH_NO_HISTORY=1` (any value but empty or `0`) in the environment a
session is started from (`dch`, `dch --spawn`, `dch --revive`) keeps no
record of it at all: the master writes nothing and prunes nothing under the
state dir, so the session never shows in `--history` and cannot be revived.
`--env` does not set it; that reaches only the session's program.

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
| `--spawn`, `--send`, `--run`, `--status`, `--report`, `--meta`, `--wait --state`, `--ls-json` | Work unchanged (plain pty writes / sidecar reads). State detection needs the master's mirror, so against a lite/`DCH_NO_VT` master `--status` falls back to reported state + output heuristic. |
| `--restart --include-agents` agent step | Skipped with a note by a lite client: it needs the mirror to see the composer. |
| `--revive` resume message | Not typed; the line says so. The session and its `claude --resume` still come back. |
| `--read`, `--wait --match` | Exit 3 with a message pointing at the full build. |
| `--keys` | Falls back to a fixed legacy xterm table client-side (exit 0 + stderr note). Mode-aware apps may misread modified keys. |

The client side is protocol-only in both builds: a lite `dch` binary can
drive every verb against a session spawned by a full `dch` master.

## Socket loss and self-heal

A session's socket lives under `$DCH_SOCKET_DIR` (default
`$XDG_RUNTIME_DIR/dch-$UID`, falling back to `/tmp/dch-$UID` when
`XDG_RUNTIME_DIR` isn't set — the common case on macOS). On macOS,
`/usr/libexec/tmp_cleaner` sweeps `/tmp` daily and deletes any plain file
whose atime, mtime, *and* ctime are all older than 3 days. That never
matches the socket node itself, but it does match the sidecar files next
to it (`.ver`, `.act`, `.state`, `.alias`) once a session sits idle for
3+ days, and — if that empties the directory — the `dch-$UID` directory too.

A master whose socket node is gone keeps running and keeps serving whoever
is already attached, but it's unreachable: `--status`/`--ls-json` report no
session, and the next attach starts a *second* master under the same name
instead of finding the first. To close that gap, each master's idle loop
runs a self-heal pass roughly once an hour (`DCH_HEAL_MS` overrides the
interval in milliseconds — mainly a test knob): a missing or replaced
socket node is re-bound (recreating the directory too, if it was swept),
a missing `.ver` sidecar is re-stamped, and the surviving sidecars get
their access time refreshed so the cleaner's 3-day clock keeps resetting
on a session that's actually alive. A socket node that's present is left strictly alone — the heal can't rescue a session whose
name a second master already took over, and it never risks stealing a
healthy one.

For a session meant to run for days, point `DCH_SOCKET_DIR` somewhere
other than `/tmp` (see `dch.1`'s ENVIRONMENT section) so the cleaner never
has a reason to touch it in the first place.

## Native Claude and Codex messages

A dch session that runs `codex`, directly or through a wrapper such as `headroom wrap codex`, registers as a Claude native peer.
Its peer name is the Codex thread title when the thread has one, otherwise the `DCH_SESSION` name; dch re-reads the title every five seconds. Claude can find it with native
`ListAgents` and send with native `SendMessage`. No helper, app-server, remote
mode, or terminal injection is involved.

Codex can use the peer network from its shell tools:

```sh
dch --agent-list
dch --agent-list --json
dch --agent-send '<exact peer name>' 'message text'
```

Inside Codex, these commands authenticate to the session's persistent sidecar.
The sidecar performs the process-identity checks and peer lookup, so Codex's
sandbox does not need permission to run `ps`.

An inbound message identifies its authenticated sender and includes the reply
command before the untrusted content. `--agent-send` exits 0 once the frame is
written to the peer; Claude sends no receipt for a message it accepts. Refused,
ambiguous, and timed-out sends exit 1.

The peer stays absent until Codex writes its shell snapshot and dch can bind
the current session to exactly one snapshot UUID.
If `/new`, a nested Codex command, or inherited state produces more than one
matching UUID, dch refuses to guess. Start a fresh dch Codex session. Sessions
started by older dch versions lack the launch marker and cannot register.

A native message is typed into the Codex composer through the session
master, as one bracketed paste followed by Enter, so a running turn steers it
in after the current tool call instead of waiting for the end of the turn.
dch types only while the screen shows the empty composer placeholder. While an
approval prompt, form, or pager is on screen the message waits up to ten
minutes; a draft in the composer or an unknown screen waits ten seconds. After
that, and always in a build without the terminal mirror, delivery falls back to
`codex queue`, which the TUI submits at the end of the current turn. If the
bound thread has no rollout yet, the queue path retains up to 16 messages in
FIFO order and retries every five seconds. This does not start a first turn in
an unused TUI. The bridge stays responsive to list and send requests while
messages wait. It never retries other queue failures or an uncertain subprocess
result.

Like Claude itself, the bridge sends no receipt for a message it accepts or
holds; Claude would render one as its own permission-mode approval notice. A
sender hears back only when a message is lost: `refused` (rejected outright)
or `dropped` (uncertain subprocess result, or pending work discarded by a
restart).

Held messages live in the sidecar's bounded memory. On an orderly restart or
shutdown, the old sidecar attempts `dropped` receipts within one shared cleanup
deadline before removing its socket. A crash, `SIGKILL`, or power loss is not
durable; there is no disk spool. Messages are limited to 16,384 bytes and wire
frames to 65,536 bytes. The bridge does not support attachments or broadcast
and is available in both builds.

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
