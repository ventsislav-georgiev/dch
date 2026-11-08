# dch — detachable terminal sessions, the simple way

A tiny terminal multiplexer built on top of [dtach][dtach]. No windows, no
panes, no tabs, no config file needed. One session per project, auto-named, attached
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
  the existing session instead of starting a duplicate. If another terminal
  is attached to it, you get the next free `<name>-2`, `<name>-3`, …; `-f`
  (or `-n <name>`) joins the busy one instead.
- **Survives client death.** The master daemon is split from the client, so
  killing the terminal window (crash, GUI quit, SSH drop) leaves the shell
  alive. Open a new window and `dch` reattaches.
- **A picker that acts.** `dch -l` shows every session's live state, agents
  and PRs, ended sessions to resume, and the settings. See
  [The session picker](#the-session-picker).
- **Agents can drive sessions headlessly.** Each session keeps an in-memory
  terminal mirror (via [libghostty-vt][ghostty]), so scripts and coding
  agents can send keys, read the rendered screen, and wait for output —
  without attaching, without a pty, without disturbing a human who *is*
  attached. See [Agent API](#agent-api).
- **Knows Claude Code.** Opt-in [auto-allow](#auto-allow) of plain permission
  prompts, `--say` to message it, `--stop` to pause and end it, and
  `--restart --include-agents` to restart it on a newly installed Claude
  Code without losing the conversation ([details](#restarting-claude-code-agents)).
- **Sessions come back.** Every session leaves a record; after a reboot or
  crash `dch --revive --all` restarts them with Claude resumed. See
  [Bringing sessions back](#bringing-sessions-back).
- **Claude and Codex talk natively.** A Codex session is a peer Claude's own
  `SendMessage` reaches, and Codex answers with `dch --agent-send`. See
  [Native Claude and Codex messages](#native-claude-and-codex-messages).
- **Upgrades and heals in place.** `dch --restart --all` moves live sessions
  onto a new `dch` without stopping them ([details](#upgrading-a-live-session)),
  and a master whose socket was swept from `/tmp` rebinds it
  ([details](#socket-loss-and-self-heal)).
- **No config file needed, no shell hooks.** One binary on `$PATH`; the
  optional [config file](#config-file) holds only opt-in features.

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
dch -ls                        # → list sessions: display-name (real-name), or real-name
dch -l                         # → the picker: states, actions, history, settings (,)
dch -rl                        # → pick one, type a new display name, repeat
dch -k                         # → pick one and kill it
dch -kl                        # → kill ALL sessions

dch --config auto_allow true   # → masters answer Claude's plain permission prompts
dch --revive --all             # → after a reboot or crash: sessions back, Claude resumed
dch --stop work                # → pause its Claude, end it; dch --revive work wakes it
dch --restart --all --include-agents  # → after an upgrade: new dch, new Claude Code
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
`dch -m <name> [alias]` sets or clears one without the picker; a name with
no live session is refused (`no session: <name>`, exit 1), as `dch -k` does.

When a session is running a **named Claude Code or Codex session**, the picker
and `-ls` show that name instead: `postman (solution.postman-fix_hop-asset-cache-host-var)`.
The join is exact and live: Claude supplies `~/.claude/sessions/<pid>.json`;
Codex supplies titles in `~/.codex/session_index.jsonl` and current releases
supply the thread ID through the matching shell-snapshot filename (older
releases' process environment IDs remain supported). In both cases dch also
requires a live harness process with the matching `DCH_SESSION`, so old records
and a title for another dch session do not overlay. A Codex snapshot names a
session only while no Codex outside that session's master, such as Codex's
shared app-server daemon, carries its `DCH_SESSION` (see
[Native Claude and Codex messages](#native-claude-and-codex-messages)). Claude auto-titles and empty
Codex titles are skipped; Codex does not mark derived titles separately.
Explicit aliases win. `--ls-json` keeps the real `name`, `alias`, and resolved
`harness` as separate fields.

### The session picker

`dch -l`, `dch -k` / `dch -d` without a name, and the detach-key switch open
the same full-screen picker, modelled on Prosper's Agent Island. It has up to
three tabs, each with its count: **Sessions**; **Sidekicks**, the live
sessions whose name, alias or harness title (the Claude or Codex name the row
shows) matches `sidekick_pattern` in the config file (a POSIX extended regex,
case-insensitive, default `-qa`; an invalid one, or one that matches every
name, is matched as plain text; set it empty for no tab), shown only when
there are some; and **History**, the ended sessions of `--history`, pinned
first, at most 50. Every list has column headers and fixed, aligned columns,
sized to the rows on screen. A session row shows NAME (alias, else harness
name, else name), STATE as a glyph and a word (Needs input, red and the
loudest; Working, in the accent; Done, green; Idle, faint), ACTIVE (how long
ago it last printed: `now`, `22m`, `3h`, `2d`), AGENTS, MERGED (`✓ 251`) and
PULL REQUESTS (from `--meta`'s `prs`: `solution#22330`, the repo said once
when they share it, `+N` for those that do not fit). A History row shows its
pin (`◆`), NAME, ENDED (closed, stopped, exited, crashed …), WHEN and the
DIRECTORY, cut from the left. As the window narrows the last column goes
first, then merged, agents and the age; the name shrinks last, cut with `…`,
and nothing wraps. The list reloads about once a second and keeps the
selection on the same session.

The look follows Apple's Human Interface Guidelines in a terminal: one accent
(Claude's clay orange) for the active tab, the keys and the working state,
colour otherwise only for state, everything secondary faint, and the selection
a quiet tinted bar the width of the screen with an accent `▌` edge, each cell
keeping its own colour on it. The bottom lines, under a faint rule, are the
status line and the key hints for the view and the selected row, grouped:
the actions, the agents' (`R` `S` `a` `b` `B`, `a` only on a row that needs
input), the view, moving about; destructive keys are red. A window too narrow
for all of them drops the agents' group, then the view's, never cutting a
hint.

| Key | Sessions / Sidekicks | History |
| --- | --- | --- |
| `↑` `↓` / `j` `k`, `PgUp` `PgDn`, `g` `G` | move | move |
| `Tab` `→` `l` / `Shift-Tab` `←` `h` | next / previous tab | next / previous tab |
| `Enter` | attach (`-k`: kill, `-d`: detach) | resume (`--revive`, with the carry-on message) |
| `r` | rename (set the alias; empty clears) | |
| `R` | restart the agent (`--restart <n> --include-agents -f`), asks first | |
| `S` | stop (`--stop`), asks first | |
| `x` | kill (`-k`), asks first | |
| `a` | allow a plain Yes/No permission prompt (presses `1`, Yes), asks first; a question form or any other prompt is refused: attach to answer it | |
| `b` / `B` | restart stale agents / restart all agents (force), asks first | |
| `s` | sort by activity (blocked, working, done, idle, newest first) or by name (`a2` before `a10`); saved as `picker_sort`, so the next picker starts in it | |
| `m` | | resume without the message (`--no-message`) |
| `I` | | resume interrupted (n) (`--revive --all`), asks first |
| `p` | | pin / unpin |
| `d` | | remove (`--forget`), asks first; a pinned record must be unpinned first |
| `,` | settings | settings |
| `q` `Esc` `Ctrl-C` `Ctrl-D` | quit | quit |

`,` opens **Settings**: every [config file](#config-file) key with its value
(a bool as `● on` / `○ off`, a choice as all of them with the set one in the
accent), `(default)` when the file has none, and what it is for. `Enter`
changes the selected one — a bool toggles (turning `auto_allow` on asks
first), a choice (`picker_sort`, `detach_key`) steps to the next, a string
(`sidekick_pattern`; empty: no Sidekicks tab) is typed on the status line,
`Ctrl-D` keeping it. `u` resets the key to its default, `Esc`, `q` or `,` goes
back. Each change is written like `dch --config` does and applies at once (the
sort, the Sidekicks split).

A question waits for `y`; any other key cancels. Actions run the matching
`dch` verb in the background: the status line shows `restarting …` while it
runs, then its result (`Resumed as <name>`, `killed: <name>`, or the error).
Resuming a record whose name is live again resumes it as `<name>-2`.
Colour is truecolor when `COLORTERM` is `truecolor` or `24bit`, else the 256
palette for a `*256color` `TERM`, else the basic eight (the selection in
reverse video); a light background in `COLORFGBG` picks lighter tints.
`NO_COLOR` draws no colour at all (the layout, the `▌` edge and the active
tab's brackets carry it); `TERM=dumb` also draws ASCII markers. Without a
terminal (`/dev/tty`), `-l` prints the session list instead.

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
libghostty-vt (~3 MB binary). Both builds support the [Agent API](#agent-api).
**dch-lite** omits only the terminal mirror; what that costs is in
[dch-lite and `DCH_NO_VT`](#dch-lite-and-dch_no_vt). `dch --build` prints
`full` or `lite`.
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
dch -lj          # list as name<TAB>alias<TAB>activity_epoch
dch -l           # session picker: tabs, live states, actions; Enter attaches
dch -rl          # rename: pick a session, type a display alias, repeat
dch -m <name> [alias]  # set the alias without the picker (none: clear it)
dch -k [name]    # kill a session (interactive picker if no name)
dch -kl          # kill ALL sessions
dch -d [name]    # detach all clients of a session (sends SIGUSR1)
dch -n <name>    # override the auto-name
dch -f           # force-attach even if another client is connected
dch -e <c>       # detach key: a char, or ^X for Ctrl-X (default ^\)
dch -E           # no detach key; use dch -d
dch -V           # version; dch --build prints full or lite

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
dch --meta <name> '<json object>'|clear         # attach metadata (PRs, ...)
dch --ls-json                                   # sessions as a JSON array
dch --restart <name>|--all [-f]                 # upgrade a live session in place
dch --restart <name>|--all --include-agents [-f] [--json]  # ...then restart its stale Claude agents
dch --respawn <name> [-- cmd...]                # on child exit, start cmd in its place
dch --respawn <name> --cancel                   # drop a pending respawn
dch --cmd <name>                                # child pid, state, argv, exit status (JSON)
dch --history [--json]                          # sessions that ended, and how
dch --revive <name> [--as <new>]|--all [--no-message] [--json]  # bring ended sessions back, claude resumed
dch --pin|--unpin <name>... [--json]            # keep a record past the 30-day prune, or stop
dch --forget <name>... [-f]|--all [--json]      # delete ended sessions' records
dch --say <name>...|--all [--json] -- <text...>  # message each Claude Code, busy too (queued), never over a prompt
dch --stop <name>...|--all [--json]             # pause Claude, end the session; --revive wakes it
dch --config [<key> [<value>|--unset]]          # show, read or set the config file
dch --agent-list [--json]                       # native Claude peers
dch --agent-send <peer> <message...>            # message one (from a Codex session)
```

Inside a session: `Ctrl-\` to detach. Works in vim, fzf, less, Claude Code,
etc. — terminals that swallow most control keys still pass `Ctrl-\` through.

Press `Ctrl-\` **twice quickly** to switch sessions: dch detaches and shows the
same picker as `dch -l`, so you can hop straight into another session. Quitting
the picker (`q` / `Esc`) just leaves you detached. A single press still detaches
— it waits out the double-press window first. `DCH_DOUBLE_TAP_MS` sets that
window in milliseconds (default `300`); `DCH_DOUBLE_TAP_MS=0` turns switching
off and makes detach instant again.

`detach_key = switch` in the [config file](#config-file) swaps the two: one
press opens the picker, two detach. With `DCH_DOUBLE_TAP_MS=0` one press then
opens the picker at once and the key never detaches; quit the picker (`q`) or
use `dch -d`. Read at each attach.

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
| `--read <n> [--ansi] [--cursor] [--recent [N]]` | Print rendered screen. `--ansi` keeps colors and OSC 8 hyperlinks (each line opens and closes its own, so a `--recent` cut never splits one); `--recent [N]` = last N lines incl. scrollback (default 100; the mirror keeps at least `DCH_SCROLLBACK` lines of history, default 2000, max 100000, set when the session is spawned; at most about one page more, ~600 lines at 80 cols, ~240 at 200; 0 keeps only the screen); `--cursor` also prints `cursor <row> <col> <visible> <wrap>` on **stderr** (see below). Plain output drops all styling — to tell a TUI's own ghost text (placeholders, argument hints) from what the human typed, read with `--ansi --cursor`; see [docs/ghost-text.md](docs/ghost-text.md). | 0 ok, 1 error, 3 no mirror |
| `--wait <n> --match <str> [--timeout ms]` | Block until screen/scrollback contains `<str>` (literal substring, ≤512 bytes); prints the matching line. Default timeout 10 s. | 0 hit, 2 timeout, 3 no mirror |
| `--status <n>` | Print the session state: `working`, `idle`, `blocked`, or `done` — resolved from the reported state, the screen-content detection, and the output heuristic (see [Agent state detection](#agent-state-detection)). | 0 ok, 1 no session |
| `--report <n> <state>` | Push a semantic state: `working`, `idle`, `blocked`, or `done` (closed set, unknown tokens rejected); `clear` reverts to auto (detection + heuristic). Last write wins; cleared automatically when the session ends. | 0 ok, 1 no session / bad state |
| `--meta <n> '<json>'` | Attach one JSON object of metadata to the session (see [Optional: session metadata](#optional-session-metadata)); `clear` removes it. Replaced atomically, last write wins, never interpreted; a byte-identical push is a no-op; rejected input leaves the stored object unchanged. Removed automatically when the session's program exits. | 0 ok, 1 no session / not a single-line JSON object ≤ 16 KB |
| `--wait <n> --state <s[,s...]> [--timeout ms]` | Block until the state matches any of the comma list (e.g. `idle,blocked,done` = "turn over"); prints the matched state. `active` is accepted as an alias for `working`. Polls every 100 ms. | 0 hit, 2 timeout, 1 no session |
| `--ls-json` | All sessions as JSON: `[{"name","alias","harness","activity_epoch","state","version","agents","meta","meta_epoch","auto_allowed","claude_session_id","transcript_path"}]`. `harness` is the name of the Claude Code or Codex session running inside, or `""`; the real `name` remains separate. `state` follows the same resolution rule as `--status`. `version` is the dch version of the master serving that session, or `""` for a master old enough not to report one. `agents` is the number of live subagents on Claude Code's agent panel (the `⏺ main` block under the prompt), `0` when no panel is on screen — including while a prompt covers it — or `DCH_NO_DETECT=1` is set. `meta` is the object last pushed with `--meta`, verbatim, or `null`; `meta_epoch` is when that content last changed (epoch seconds, like `activity_epoch`), `0` with no meta. `auto_allowed` is the permission prompt the master answered last with [auto-allow](#auto-allow), `{"at":<epoch seconds>,"title":"<the dialog's first line, e.g. Bash command>"}`, or `null`; cleared when the session's program exits, like `meta`. `claude_session_id` is the `sessionId` of the one live interactive Claude Code under the session's program, read fresh from its own `<config>/sessions/<pid>.json` (pid descending from the program, `procStart` matching the process; `CLAUDE_CONFIG_DIR` honoured), so it follows `/clear` and resume; `null` when no Claude runs there, when more than one does, once it exits, or when the id is not a UUID. `transcript_path` is that conversation's absolute `<config>/projects/<dir>/<sessionId>.jsonl` when exactly one such file exists, else `null` (also right after `/clear`, until the new conversation writes its first line). | 0 |
| `--restart <n>` \| `--restart --all` `[--include-agents] [-f] [--json]` | Re-exec the session's master onto the current `dch` binary, in place — the program inside keeps running (see [Upgrading a live session](#upgrading-a-live-session)). `--all` restarts only sessions whose `version` differs from this binary's; `-f` restarts all of them. With `--include-agents`, it then restarts the stale Claude Code agents of those sessions — Claude as the session's command; one typed into its shell is skipped (`in_shell`); every session with `--all`, only `<n>` otherwise; `-f` forces current agents too (see [Restarting Claude Code agents](#restarting-claude-code-agents)). Without the flag no agent is touched, `--all` included. `--include-agents` without `--restart` is a usage error. `--json`: stdout is NDJSON, the master pass included, with or without `--include-agents` (see [Machine-readable output](#machine-readable-output)). | 0 ok, 1 failed / master too old / an agent restart failed |
| `--respawn <n> [-- cmd...]` | Arm a one-shot respawn: when the session's program next exits, the master keeps the session (same socket, pty size, attached clients) and starts `cmd` in its place. No `cmd` arms a *hold*: the session stays up childless until a second `--respawn <n> -- cmd` starts one, or until `DCH_HOLD_MS` (default 120000) ends it. An arm that nothing consumes expires after `DCH_RESPAWN_TTL_MS` (default 300000); both are read from the master's environment. `--restart` refuses while a respawn is armed or held. | 0 ok, 1 no session / refused / master too old |
| `--respawn <n> --cancel` | Drop a pending arm. On a held (childless) session it ends the session. | 0 ok, 1 no session |
| `--cmd <n>` | Print `{"pid","state","argv","exit_code","exit_signal","tail"}`: the child's pid (`null` while held), `state` `running`, `armed` or `held`, the argv it was started with, the exit status of a child reaped under an arm (`null` otherwise), and `tail`, the last 8 KB the child wrote since the arm, raw. | 0 ok, 1 no session / master too old |
| `--history [--json]` | List the sessions that ended, newest first, one line each: `name (alias)  when  end  cwd  $ argv  [claude <name> <session id>]`. `end` is `exit N`, `signal N`, `killed` (`dch -k`), `stopped` (`dch --stop`) or `vanished` (the master died without a word: SIGKILL, power loss). Only the records of the current socket dir are listed. A pinned record (`--pin`) ends its line with `  [pinned]`, one a new session under its name moved aside with `  [record <name>.<ended>]`. `--json` prints the stored records as an array, a pinned one with `"pinned":true` added (absent otherwise), every one with `"file"`, its record file without `.json` (`work`, or `work.<ended>` for a moved one), the exact handle for `--pin`/`--unpin`/`--forget <file>.json` (see [Bringing sessions back](#bringing-sessions-back)). | 0 |
| `--revive <n> [--as <new>] [--no-message]` \| `--revive --all [--no-message]` | Start ended sessions again: in their cwd, with their argv, with the Claude Code agent in them resumed via `--resume <id>` and told to carry on (see [Bringing sessions back](#bringing-sessions-back)). `--no-message`: resumed only, no message typed (`ok`, the line says it was not sent). `<n>` revives its newest ended record, whatever its end — never a live session's; `<file>.json` (or the bare file `--history` prints, `[record <file>]`) exactly that record file; a live `<n>` is refused unless `--as <new>` names another session. `--all` revives every session that died — anything but `exit 0`, `killed` and `stopped` — within the last 3 days after running at least 5 minutes, and gives one whose name is now taken the first free `<n>-2`, `<n>-3`, …. Older or shorter-lived sessions stay in `--history` and revive by name. Never two Claudes on one conversation: a record whose Claude id a live Claude already runs is skipped by `--all` and refused by name (`live_sid`, naming the holder), and of two records carrying one id `--all` revives only the newest (`dup_sid`). One line per session. `--json`: stdout is NDJSON, each result carrying `"as"`, the name it came back under (see [Machine-readable output](#machine-readable-output)). | 0 ok (skips included), 1 no history / ambiguous / name live / id live / a revive or resume failed |
| `--pin <n>... [--json]` \| `--unpin <n>... [--json]` | Pin a session's history record so the 30-day prune never takes it, or unpin it (see [Bringing sessions back](#bringing-sessions-back)). `--pin` needs a record for the name, live or ended (`<n>: failed — …`, `not_found`, otherwise), and pins every record of the name; pinning a pinned one is `ok`. `--unpin` unpins every record of the name and is `ok` with or without a pin, but `not_found` when there is no record (nor a stray pin) to act on. An argument ending in `.json` names exactly one record file instead, never a name: `--pin work.json` pins the primary `work.json` only, `--pin work.<ended>.json` only that moved record. One that is also a session's name (sessions `a` and `a.json`: `a.json` would be both) is refused, `ambiguous`, nothing touched: `a.json.json` names session `a.json`'s record. A bare argument also matches a record file exactly as given, so the `[record <file>]` that `--history` prints works for a long name's moved record (which the name shortening would hash again). One line per name: `<n>: pinned`, `<n>: unpinned` or `<n>: failed — <why>`. `-f` is a usage error. | 0 nothing failed, 1 usage error / a name failed |
| `--forget <n>... [-f] [--json]` \| `--forget --all [--json]` | Delete history records of the current socket dir, matched by name as `--revive` matches them (every record that matches); `<file>.json` (`work.json`, `work.<ended>.json`) deletes exactly that one record file; one that is also a session's name is refused, `ambiguous`, as for `--pin`. Never a live session's (`live`: its master would write it back; `-f` does not change that); a pinned one only with `-f`, which removes its pin too (`pinned` otherwise); `not_found` when no record matches. One line per record: `<n>: forgotten` or `<n>: failed — <why>`. `--all` deletes every record that is neither live nor pinned and skips the rest: one `<n>: forgotten` per record deleted, then `forgot N (K pinned, L live kept)`; `-f` with `--all` is a usage error (it never removes a pin). | 0 nothing failed (skips included), 1 usage error / a name failed |
| `--say <n>... [--json] -- <text...>` \| `--say --all [--json] -- <text...>` | Type one message into the Claude Code running in each session — as its command or typed into its shell — and submit it, idle or busy: a busy Claude (or one running a background shell) queues the message, just as Enter does when you type it yourself, and takes it once its turn ends; one with messages already queued queues it after them. Typed only when the screen shows its empty composer (its placeholder, also the "Press up to edit queued messages" one), never over a prompt or dialog (the Enter would answer it) or an unsent draft, never waiting for either. Text is the words after the required `--` joined with spaces. Sessions are typed into concurrently; Enter is pressed again while the text still sits in the composer, never once a dialog is up; text that left the composer — submitted, or shown queued above it — is `sent`. One result per session as it finishes: `<n>: sent`, `<n>: skipped — <why>` (`not_claude` under `--all`, `self` = `$DCH_SESSION`, `prompt`, `draft`, `no_screen` — always on dch-lite) or `<n>: failed — <why>` (`no_session`, `not_submitted`, `not_typed`, `old_master`). `--json` prints one `{"name","result":"ok\|skipped\|failed","reason","detail"}` object per line instead. | 0 nothing failed (skips included), 1 usage error / a session failed |
| `--stop <n>... [--json]` \| `--stop --all [--json]` | End sessions so `--revive` can bring them back. A session whose Claude Code is live — its command, or typed into its shell — is paused first, the way `--include-agents` pauses it: a pause message (`DCH_PAUSE_MSG` replaces it) and a wait until Claude is provably idle (`DCH_AGENT_TIMEOUT_MS`, 600000). Claude as the session's command then gets `/exit` through the same composer gate, its resume id read from the exit line (else from its session record). Claude typed into the shell never gets `/exit` — the shell would run whatever followed `claude` on its line (`claude && git push`, a loop): its resume id is read from its session record, and the session's end hangs up the shell and Claude together. Then the session ends. Claude's session record is looked up in the sessions dir of Claude's own `CLAUDE_CONFIG_DIR` (else its `HOME`), read from the process, so a Claude run as `CLAUDE_CONFIG_DIR=~/.claude-work claude` is found from any env; the history record, `--say` and `--include-agents` look it up the same way. A session without a live Claude ends at once; one where a process plainly is an interactive Claude (`claude` as its argv[0]) but no record of it can be read is left running, `failed` `claude_unreadable` — ending it would hang Claude up unpaused, its id unknown. A non-interactive run — `claude -p`/`--print`, or a subcommand such as `claude mcp serve` or `claude update` — has no composer to pause and counts as no Claude. The record ends `stopped` and keeps the agent with that id (see [Bringing sessions back](#bringing-sessions-back)). Sessions are handled concurrently; one line per session as it finishes: `<n>: stopped (claude <name> <id>; revive with dch --revive <n>)` — or, when the session kept no record (`DCH_NO_HISTORY`, a failed write), `<n>: stopped (no session record kept, so dch --revive cannot bring it back; resume by hand: claude --resume <id>)`, the same text in `--json`'s `detail`, `<n>: skipped — <why>` (`self` = `$DCH_SESSION`, `respawn_pending`, `no_screen` — a Claude session on dch-lite), `<n>: failed — <why>` (`no_session`, `old_master`, `claude_unreadable`, `prompt`, `draft`, `queued`, `not_typed`, `not_idle`, `exit_failed`, `claude_exited`, `end_failed`) or `<n>: interrupted — <why>`; once dch has read Claude's id, a line that is not `ok` carries it too. A failure leaves the session running and types a resume message into its Claude (`DCH_RESUME_MSG` replaces it); so does a first SIGINT/SIGTERM for every Claude not yet past `/exit` (a second one reports those past `/exit` as `interrupted` with their id and leaves their `.stopped` marker, so the record still ends `stopped`; a third exits at once). A master that stops answering after `/exit` keeps its marker too: `failed`, with the id, and the record ends `stopped` once the master runs again. Both lines end `if claude is still running, run dch --stop <n> again`: the marker stays until then. `--json`: stdout is NDJSON — `{"name","phase":"pausing\|exiting\|ending"}` as a session reaches each (no `exiting` for Claude typed into the shell), then one `{"name","result":"ok\|skipped\|failed\|interrupted","reason","detail","session_id"}` per session (`session_id` `""` when none). | 0 nothing failed (skips included), 1 usage error / a session failed or was interrupted |
| `--agent-list [--json]` | List the native Claude peers, read from Claude Code's own session records, bridged Codex sessions included (see [Native Claude and Codex messages](#native-claude-and-codex-messages)). `--json`: `[{"name","session_id"}]`. | 0 ok, 1 registry ambiguous or too large |
| `--agent-send <peer> <message...>` | Send one native message to the peer of that exact name, from inside a bridged Codex session only. | 0 written, 1 refused / ambiguous / timed out / not in a bridged Codex session |

### Machine-readable output

`--say`, `--stop`, `--restart`, `--revive`, `--pin`, `--unpin` and `--forget` take `--json`: stdout is then
NDJSON, one object per line, flushed as it happens. stderr keeps its human
text (usage errors, warnings, the reason behind a master-pass failure). Exit
codes are the same as without `--json`. Anywhere else `--json` is a usage
error (`--history` and `--agent-list` take their own).

- **Phase** — `{"name","phase"}` as a session reaches each step:
  `--stop` `pausing`, `exiting` (Claude as the command only), `ending`;
  `--restart --include-agents`
  `pausing`, `exiting`, `relaunching`, `resuming`; `--revive` `starting`,
  `resuming` (only with the mirror and without `--no-message`: otherwise no
  resume message is typed).
- **Result** — one per session, after its phases:
  `{"name","result","reason","detail","session_id"}`. `result` is `ok`,
  `skipped`, `failed` or `interrupted`; `reason` a code below, `""` on `ok`;
  `detail` the text the line would carry without `--json`, `""` when none;
  `session_id` the Claude conversation id when there is one (`--stop`: once
  read, right before `/exit`), else `""`. `--say` prints `{"name","result","reason","detail"}`
  (no `session_id`, never `interrupted`). `--revive` adds `"as"` after
  `"name"`: the name the session came back under. `--pin`, `--unpin` and
  `--forget` print one per name (`--forget --all`: per record, `skipped`
  `live` or `pinned` for the ones it keeps), `session_id` the record's
  Claude conversation id.
- **Summary** — `--pin`, `--unpin` and `--forget` only: one last line
  `{"ok":N,"skipped":S,"failed":F}`, counting the results. It has no
  `"name"`, which is how to tell it from a result.
- **Master pass** of `--restart` — one
  `{"name","phase":"master","result","reason","detail","session_id":""}` per
  session it looked at, before any agent line: `ok` (`detail`
  `restarted on dch X`), `skipped` `current` (`--all` without `-f`, already
  on this dch), `skipped` `no_sidecar` (`--all`; `failed` for one name),
  `failed` `no_session` or `restart_failed`. It carries `result` too: tell it
  from an agent result by its `phase`. With `--include-agents` every live
  session then gets one agent result (`skipped` `not_claude` when no Claude
  runs in it), and no summary line is printed.

| Reason | Verbs | Meaning |
| --- | --- | --- |
| `not_claude` | say, restart | No Claude Code runs in the session. |
| `self` | say, stop, restart | The session you run in (`$DCH_SESSION`, or dch runs under its Claude). |
| `prompt`, `draft` | say, stop, restart, revive | A prompt or dialog, an unsent draft: nothing typed over it. |
| `queued` | stop, restart, revive | A queued message is pending: nothing typed over it (`--say` queues its message after it). |
| `no_screen` | say, stop, restart, revive | The composer is not on screen, or the session has no mirror (dch-lite). `--revive` on dch-lite: `ok` with this reason — revived and resumed, resume message not typed (with `--no-message`: `ok`, reason `""`). |
| `no_session` | say, stop, restart | No such session. |
| `old_master` | say, stop, restart | The session's master predates `--cmd` (`--stop`: predates dch 1.24.0, or has no version sidecar); `dch --restart` it. |
| `not_typed`, `not_submitted` | say, stop, restart, revive | Text could not be typed or did not land / stayed in the composer through every Enter. |
| `respawn_pending` | stop, restart | A `--respawn` is armed or held. |
| `not_idle` | stop, restart, revive | Claude did not go idle in time. |
| `claude_exited` | stop, restart | Claude exited while pausing. |
| `exit_failed` | stop, restart | `/exit` could not be typed or did not end Claude, or Claude crashed on it. |
| `claude_unreadable` | stop | An interactive Claude runs in the session (`claude` as a process's argv[0], not `-p`/`--print` or a subcommand like `mcp`), but no session record of it can be read: the session is left running, nothing typed. |
| `end_failed` | stop | Claude quit (or, typed into the shell, was paused), but the session did not end. |
| `signal` | stop, restart | `interrupted` by SIGINT/SIGTERM/SIGHUP. |
| `current` | restart | Agent: already on the installed claude. Master pass: already on this dch. |
| `no_record` | restart | Claude is the command, but no live Claude session record joins it. |
| `in_shell` | restart | Claude was typed into the session's shell, not run as its command: `/exit` would hand the shell back to run whatever followed `claude` on its line. |
| `cwd_mismatch` | restart | Claude's cwd is not the session's launch cwd. |
| `version_unknown` | restart | `claude --version` could not be run as the relaunch would. |
| `relaunch_failed` | restart, revive | The relaunch was refused, could not be typed, or Claude never showed its composer. |
| `not_updated` | restart | Relaunched and resumed, but still on the old version. |
| `no_sidecar`, `restart_failed` | restart | Master pass: version sidecar missing (age unknown); the re-exec failed. |
| `no_history` | revive | No record for that name. |
| `not_found` | forget, pin, unpin | No record for that name (`--unpin`: nor a stray pin). |
| `ambiguous` | revive, forget, pin, unpin | The argument ends in `.json` and is also a session's name (or, bare, is one record's file and another's name): nothing touched; pass `<file>.json`, the record's `"file"` in `--history --json` (session `a.json`: `a.json.json`). |
| `live` | revive, forget | The name is a live session: `--revive`, use `--as`; `--forget` never deletes a live session's record (`--forget --all`: `skipped`). |
| `pinned` | forget | The record is pinned: `--forget <n> -f` deletes it with its pin; `--forget --all` keeps it (`skipped`). |
| `io_error` | forget, pin, unpin | The record or pin file could not be written or removed (`detail` says why). |
| `cwd_gone` | revive | The recorded cwd no longer exists. |
| `live_sid` | revive | A live Claude already runs on the record's Claude id (`session_id`): in a live session (`detail` names it), or outside any (`claude pid N`). `--all`: `skipped`; by name, with or without `--as`: `failed`. |
| `dup_sid` | revive | `--all`, `skipped`: a newer record with the same Claude id is revived in this run (`detail` names the skipped record file). |
| `not_utf8` | revive | The recorded command, cwd or Claude launch had bytes that are not UTF-8, stored as U+FFFD: it would run something else. `--history` still lists it; the detail gives `claude --resume <id>`. |
| `not_started` | revive | The session did not start (the program cannot be executed: the reason is on stderr). |
| `launch_unknown` | revive | Revived, but its Claude was not resumed: the launch command is unknown. |

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
| `blocked` | Permission/confirmation prompts: "Do you want to proceed?", "Esc to cancel · Tab to amend" (Write/Edit), "Allow command?", "Press enter to confirm or esc to cancel", "permission required", "waiting for approval", … (Claude Code, Codex, Gemini, opencode, Cursor). With Claude Code's input box on screen, only the rows below it count: a reply that quotes a prompt is not one |
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
the PRs an agent has open, say — and every
`--ls-json` consumer sees it as `meta`, next to `state`. dch stores and
passes it through; it never interprets the keys.

Push it from a status line, a hook, or any script running in the session.
`DCH_SESSION` is set in every dch session:

```sh
dch --meta "$DCH_SESSION" '{"prs":{"open":[],"merged":3}}' 2>/dev/null; true
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

**Standard keys.** Optional, and consumers must tolerate it being
absent. Other top-level keys pass through unchanged, so give your own
distinctive names.

| Key | Shape | Meaning |
| --- | --- | --- |
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

## Config file

dch runs without one. The optional config holds opt-in features: `key = value`
lines, `#` comments, the last line for a key wins. It lives at `$DCH_CONFIG`,
else `$XDG_CONFIG_HOME/dch/config`, else `~/.config/dch/config`, and is read
fresh every time it is needed, so an edit takes effect at once, also in
running sessions.

| Key | Value | Default |
| --- | --- | --- |
| `auto_allow` | `true`/`false` (also `1`/`0`, `yes`/`no`, `on`/`off`): the master answers Claude Code's plain permission prompts, see [Auto-allow](#auto-allow) | `false` |
| `sidekick_pattern` | a string, read by the session picker; empty: no Sidekicks tab | `-qa` |
| `picker_sort` | `activity` or `name`: the session picker's sort, also set by its `s` key | `activity` |
| `detach_key` | `detach` or `switch`: what one `Ctrl-\` press does; two presses do the other (see [Cheatsheet](#cheatsheet)) | `detach` |

```sh
dch --config                         # the path, then every known key's effective value
dch --config auto_allow              # one value; exit 1 when unset
dch --config auto_allow true         # set it
dch --config auto_allow --unset      # remove it
```

Setting rewrites the file whole (a temp file renamed over it, the target of a
symlinked one), keeps every other line and comment, and creates the parent
directory. An unknown key, a bool key given anything else, or a choice key
given a value not its own, is refused with exit 1. The session picker's
settings view (`,`) sets the same keys the same way.

### Auto-allow

With `auto_allow = true`, the session's master answers Claude Code's plain
permission prompt itself: it types `1`, the "Yes" the caret already sits on,
one keystroke and never Enter. It is the rule of Prosper's Agent Island
"Always allow", now in dch, so it works on Linux and remote hosts without
Prosper. Only exactly this shape is answered:

```
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for …     (optional, never picked)
   3. No, and tell Claude what to do differently (esc)
```

The question is exactly "Do you want to proceed?", option 1 a bare `Yes`, the
last option a `No…`, neither with wrapped lines, and between them at most one
"Yes, and don't ask again …" row (a curly `’` counts as `'`) — never one that
changes the permission mode (`(shift+tab)`). The dialog must be the last thing on the screen (below it only
rules, its "Esc to cancel" footer and Claude Code's session-title rule), and
the screen must read `blocked` as `--status` reads it, so a dialog quoted in
the transcript above the composer is never answered. Plan approvals, trust and
edit dialogs, question forms, pickers, four options, a "don't ask again"-only
Yes: all left to you. Hook- or ask-rule-raised prompts ("Hook … requires
confirmation for this command", "Permission rule … requires confirmation for
this …", "Ask rule … overrides auto mode for this …") are always left to you. As in Prosper, a dialog whose head scrolled off the top
(a long heredoc) and one with hidden characters are still answered: Always
allow approves any plain permission prompt, whatever the command.

The master checks once its program has been quiet for 300 ms after output
(at most 2 s into output that never pauses) and again 2 s after an answer, and
only answers when a Claude Code runs under the session's program. The loop
guard: a prompt it answered is never answered again while it stays up, nor
within 30 s once gone and back — that one is yours. Claude Code's ticking "⚠ Claude Code
will automatically block this request in M:SS, …" line counts as the same prompt. Each answer is reported as
`auto_allowed` in `--ls-json` (`<sock>.allowed` next to the socket). Needs the
terminal mirror: not in dch-lite or under `DCH_NO_VT`; `DCH_NO_DETECT=1` in
the master's environment turns it off too.


## Environment

Every variable dch reads, in one place; the linked section has the detail.
"Master" means the environment the session was spawned from: changing it
later does nothing to a running session.

| Variable | Read by | Default | Does |
| --- | --- | --- | --- |
| `DCH_SESSION` | every `dch` | set by dch | The session name, set for everything inside a session; hooks use it ([reporting](#optional-explicit-state-reporting)). Attaching refuses while it is set: no nesting. |
| `DCH_SOCKET_DIR` | every `dch` | `$XDG_RUNTIME_DIR/dch-$UID`, else `/tmp/dch-$UID` | Where sockets and sidecars live ([self-heal](#socket-loss-and-self-heal)). Records of one socket dir never mix with another's. |
| `DCH_CONFIG` | every `dch` | `$XDG_CONFIG_HOME/dch/config`, else `~/.config/dch/config` | The [config file](#config-file). |
| `DCH_DOUBLE_TAP_MS` | attaching client | `300` | The double-press window of `Ctrl-\`; `0`: one press acts at once ([Cheatsheet](#cheatsheet)). |
| `DCH_NO_DETECT` | client and master | unset | Anything but empty or `0`: no screen-content [state detection](#agent-state-detection), `agents` reads `0`, the master does no [auto-allow](#auto-allow), and `--say`, `--stop` and the agent restart see no screen, as on dch-lite. |
| `DCH_NO_VT` | master | unset | Anything but empty or `0`: no terminal mirror ([dch-lite and `DCH_NO_VT`](#dch-lite-and-dch_no_vt)). |
| `DCH_NO_REPLAY` | master | unset | Anything but empty or `0`: attach clears and asks for a repaint instead of [replaying the screen](#attaching-replays-the-screen). |
| `DCH_SCROLLBACK` | master | `2000` | Mirror scrollback lines, max `100000`, `0` none (`--read --recent`, [Verbs](#verbs)). |
| `DCH_NO_HISTORY` | master | unset | Anything but empty or `0`: no history record ([Bringing sessions back](#bringing-sessions-back)). |
| `DCH_RECORD_MS` | master | `60000` | How often the history record is refreshed. |
| `DCH_HEAL_MS` | master | `3600000` | The [self-heal](#socket-loss-and-self-heal) interval. |
| `DCH_HOLD_MS`, `DCH_RESPAWN_TTL_MS` | master | `120000`, `300000` | How long a held session waits, and an unused `--respawn` arm lives ([Verbs](#verbs)). |
| `DCH_PAUSE_MSG`, `DCH_RESUME_MSG`, `DCH_AGENT_*_MS` | client | see the table | Agent pause/resume text and timeouts of `--include-agents` and `--stop` ([table](#restarting-claude-code-agents)). |
| `DCH_REVIVE_MSG` | client | built in | The carry-on message `--revive` types ([Bringing sessions back](#bringing-sessions-back)). |
| `DCH_LOGIN_ENV`, `DCH_LOGIN_ENV_MS` | client | on, `10000` | `--revive` takes your login shell's environment; `0` skips it. |
| `DCH_DEBUG` | client and master | unset | A path: append a trace there; `1`: `/tmp/dch.<uid>.trace`. |
| `XDG_STATE_HOME` | client and master | `~/.local/state` | Where [history records](#bringing-sessions-back) live. |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME` | client | `~/.claude`, `~/.codex` | Where dch looks for Claude's and Codex's session records and titles. |
| `NO_COLOR`, `TERM=dumb` | picker | unset | Draw the [picker](#the-session-picker) without color. |

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
within `DCH_AGENT_STEP_MS`. A second signal stops that wait: each agent past
`/exit` is reported interrupted with its id. One already relaunched says
`claude --resume <id> is started in the session; resume message not typed`
— it runs on its conversation, so type the message there, never a second
`claude --resume`. One still exiting says its hold is left armed, and if
Claude is still running, `dch --respawn <n> --cancel` disarms it. A third
exits at once. SIGKILL cannot be
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
| `DCH_AGENT_LAND_MS` | `30000` | How long dch waits for typed text to show in the composer before giving up without pressing Enter. The wait ends as soon as it shows; a just-resumed Claude under load can take seconds. |
| `DCH_AGENT_STEP_MS` | `60000` | Limit for `/exit` to end Claude and for the relaunch to show a composer. |
| `DCH_AGENT_REENTER_MS` | `5000` | After Enter, how long a message may stay in the composer before Enter is pressed again (at most 5 more times, never on a dialog). A just-resumed Claude can drop the first Enter. |

Limitations:

- **Claude Code only.** Codex sessions are left alone.
- **Typed into a shell.** A `claude` typed into a session's shell (`exec
  claude` included) is skipped with a note (`in_shell`): `/exit` would hand
  the terminal back to the shell, which runs whatever followed `claude` on
  its line — `claude && git push`, `claude; make deploy`, or a `while` loop
  that starts a new Claude. `dch --stop` then `dch --revive` brings such a
  session back resumed, never running the rest of the line.
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
ended. A revived session's record carries the agent it resumed from the
first write, so it keeps the resume id even if it ends within that minute.
A reboot, a crash or a SIGKILL ends sessions without a word; the
record outlives them.

A new session under a dead session's name — `dch` in the same repo after
`dch --stop`, `dch work` again, `--revive other --as work` — would replace
`work.json`. When that record still holds a Claude id (it ended `stopped`,
`killed`, on a signal or `vanished` with Claude alive), the new master first
moves it aside to `work.<ended>.json` (`<ended>` the epoch it ended, else
last seen), so the id is kept as history: `--history` lists it with `[record
work.<ended>]` (`"file"` in `--json`), and `--revive work` takes the newest
ended record of the name, never the live one — so with the new `work` still
running, `--revive work --as work2` brings the stopped one back. `--revive
work.<ended>`, `--pin`, `--forget` take it by that name too, as printed —
for a long name too, whose moved record's file name would not survive the
name shortening. `--revive`, `--pin`, `--unpin` and `--forget` take
`<file>.json` for exactly one record: `work.json` is the primary alone,
`work.<ended>.json` the moved one alone (bare `work` hits every record of the
name). A `.json` argument that is also a session's name is refused as
`ambiguous` and touches nothing: with sessions `a` and `a.json`, `a.json`
could be either; `a.json.json` is session `a.json`'s record. A record with no
Claude id is simply replaced, and so is the one a `--revive` under the same
name carries on.

```sh
dch --history                # what ended, when, how, where
dch --revive work            # bring one back
dch --revive work --as work2 # ...under another name
dch --revive --all           # every session that died lately, see below
dch --revive work --no-message  # resume its Claude, type nothing into it
dch --stop work              # pause its Claude, end it: "stopped"
dch --revive work            # ...and later wake it, Claude resumed
```

`--all` takes a session only when all three hold: it ended abnormally
(anything but `exit 0`, `killed` and `stopped`, `vanished` included), it was last seen
within the last 3 days, and it ran at least 5 minutes (last seen − started;
last seen is `ended`, else `updated`). That leaves out short test sessions
that end on a signal on purpose, and last week's sessions. They are still in
`--history`; `dch --revive <name>` takes any record.

A revived session starts in its recorded cwd with its recorded argv. If the
argv runs `claude` (directly or through a wrapper such as `headroom wrap
claude`), `--resume <id>` goes in after the `claude` word and any old
`--resume`/`--continue` comes out. If claude was typed into a shell inside the
session, the shell comes back and dch types the launch command again: `cd
<claude's cwd> && <the command> --resume <id>`. A Claude that ran under a
config dir of its own (the record's `config_dir`: `CLAUDE_CONFIG_DIR=~/.claude-work
claude` typed into the shell, `CLAUDE_CONFIG_DIR` in the session's
environment, or another `HOME`) is resumed under it, where its conversation
is: as the command, `CLAUDE_CONFIG_DIR=<config_dir>` is in its environment;
typed, the line is `env CLAUDE_CONFIG_DIR=<config_dir> <the command> --resume
<id>` (`env`, so any shell takes it), unless the command sets
`CLAUDE_CONFIG_DIR=` itself. A Claude whose `HOME` alone differed comes back
with `CLAUDE_CONFIG_DIR=<that HOME>/.claude`, not with that `HOME`. A record
with an empty `config_dir`, or none (dch before 1.28.2), is resumed in the
reviver's environment as before. Then, on the full build, dch
waits for the composer and types a message telling the agent the session
ended and to re-read its ledger, re-arm its monitors and carry on
(`DCH_REVIVE_MSG` replaces it; `--no-message` skips it: claude is resumed,
nothing is typed into it, and the result is `ok` with `resume message not sent
(--no-message)`). dch-lite revives and resumes but cannot see the
composer, so it says the message was not sent. The caller's own Claude
identity (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, …), its IDE
link and hook files (`CLAUDE_CODE_SSE_PORT`, `CLAUDE_ENV_FILE`,
`CLAUDE_PROJECT_DIR`) are removed from the revived session's environment, so
`--revive` works from inside an agent; run from Claude Code (`CLAUDECODE`
set), so is what it sets for its own tool commands (`GIT_EDITOR=true`, which
would abort a later `git commit` without `-m`, `AI_AGENT`,
`COREPACK_ENABLE_AUTO_PIN`, `NoDefaultCurrentDirectoryInExePath`) — the login
shell below gives back any your rc files set. The environment is the one your login shell builds:
`--revive` runs `$SHELL -l -i` once and takes its variables (all but
`DCH_*`), so a revive started from a GUI app or a daemon still gets the
`PATH` your rc files set up. A shell that fails or takes longer than
`DCH_LOGIN_ENV_MS` (default 10000) leaves the caller's environment as it
is; `DCH_LOGIN_ENV=0` skips the step. A session ended by `dch --stop` comes back the same way;
its message says it was stopped by `dch --stop` and to re-arm what it
stopped for the pause.

`dch --stop` is the deliberate half of that round trip. It pauses the Claude
in the session, `/exit`s it, reads its resume id from the exit line and ends
the session; the record ends `stopped` and its `agent` keeps that id, even
though Claude quit cleanly first (a clean quit removes Claude's session file,
which otherwise drops the agent from the record). The mechanism is a
`<socket>.stopped` marker dch writes right before `/exit` — the agent as last
seen — and again with the exit line's id: while it exists the master's
refreshes keep that agent, and the master's exit write takes it. A Claude
typed into the session's shell is never `/exit`ed, since the shell would then
run whatever followed `claude` on its line: once it is paused and idle, dch
writes the marker with the id from Claude's session record at that moment
and ends the session, whose hangup takes the shell and Claude together. `--stop`
returns once the master has written its record, so a `--revive` right after
finds it `stopped`.

`--revive` never starts a second Claude on one conversation. Two records can
carry one Claude id: a session crashes, a new session under its name moves its
record aside, and the Claude in the new one is resumed on the same id by hand
(`claude --resume <id>`). `--revive --all` takes the newest record of an id
and skips the others (`<n>: skipped — claude <id> is revived from a newer
record as <n> (record <file>)`, reason `dup_sid`); they stay in `--history`.
A record whose id a live Claude runs right now is skipped by `--all` (`<n>:
skipped — claude <id> already runs in live session <s> (record <file>)`) and
refused by name, `--as` or not (`live_sid`, exit 1). Live ids come from each
live session's own probe (its Claude process and that Claude's session
record, as `--say` finds it), not from the session's history record, which
lags up to `DCH_RECORD_MS` behind; then from Claude's sessions dir, and the
sessions dir of each record's `config_dir`, for a Claude running outside any
live session (one that outlived its master, or one in a plain terminal:
`claude pid <pid>, outside any live session`).

A session whose directory is gone, or whose program cannot be executed, is
reported and skipped; the others still come back. A revive under a new name retires the old record — once the new session has written its own (not under `DCH_NO_HISTORY`). Records older
than 30 days are pruned, in every socket dir's subdir, when a master starts,
and subdirs left empty are removed.

A record you want to keep gets a pin; one you are done with can go now:

```sh
dch --pin work          # never pruned; --history shows [pinned]
dch --unpin work        # back to the 30-day prune
dch --forget old        # delete its record (a pinned one needs -f)
dch --forget --all      # delete every record neither live nor pinned
```

A pin is an empty `<name>.pin` beside `<name>.json`. The master never touches
it, so it holds through every refresh and through a revive under the same
name, and the cold-start prune skips any record that has one, whatever its
age. The pin belongs to the name, not to one run: a new session started
under a pinned name writes the same record, and that record is pinned too;
a record it moves aside gets its own `<name>.<ended>.pin`, so it stays
pinned as well — a pinned name stopped and reopened often gathers pinned
records the prune never takes; `--unpin <name>` releases them all,
`--forget <name>.<ended>.json` drops one. `--pin` and `--unpin` act on every record of the name;
`--pin work.json` (or `work.<ended>.json`) on that one record only.
A revive under another name (`--as`, or `--all`'s `<name>-N`) moves the pin
to the new name along with the record. The exemption holds for masters from
dch 1.25.0 on: a master of an older dch still prunes by age alone when it
cold-starts. `--forget` never deletes a live session's record (its master
would write it back), and `--forget --all` leaves pinned records alone.

The record is one line of JSON, mode 0600 in a 0700 directory:

```json
{"name":"work","sock_dir":"/tmp/dch-502","alias":"My work","cwd":"/src/app","argv":["/opt/homebrew/bin/headroom","wrap","claude"],
 "started":1761400000,"updated":1761403600,"ended":1761403612,"end":"signal 9",
 "agent":{"kind":"claude","session_id":"…","name":"fix-tests","cwd":"/src/app",
          "launch":["/usr/local/bin/headroom","wrap","claude"],"config_dir":""}}
```

`sock_dir` is the socket dir the session lived in; a record whose `sock_dir`
is not the current one is ignored. `ended` is `0` and `end` `""` while the
session runs (and after a master that
died without writing them; `--history` shows that as `vanished`). `agent` is
`null` without a Claude Code agent; `launch` is the argv of the process the
master's child started it from — the child itself for a command session, the
typed command for a shell session. `config_dir` is the config dir Claude ran
under, taken from where its session record was found: its own
`CLAUDE_CONFIG_DIR`, or `<its HOME>/.claude`; `""` when that is the recording
dch's own `~/.claude`, the default. `--revive` resumes under it.

A bare `argv[0]` is stored as the path the session's PATH found it on (a
symlink is kept, not resolved), so a revive from a thinner PATH, such as a
launchd-started app's, still finds it; one not found stays bare. The argv is
otherwise stored as given, so a secret passed on a command line lands in
the record the way it lands in shell history. The environment is never
stored. A byte that is not UTF-8 (a Linux file name, say) is stored as
U+FFFD, in every field, so the record always parses; `--revive` refuses one
whose command, cwd or Claude launch holds U+FFFD (`not_utf8`, with the
Claude id to resume by hand) rather than run something else. `DCH_NO_HISTORY=1` (any value but empty or `0`) in the environment a
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
| `--stop` | Sessions with a live Claude are `skipped`/`no_screen`: nothing is typed without the mirror to gate on. Sessions without one still stop. |
| `--read`, `--wait --match` | Exit 3 with a message pointing at the full build. |
| [Auto-allow](#auto-allow) | Unavailable: the master reads the prompt off its mirror. Nothing is answered; `auto_allowed` stays `null`. |
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
other than `/tmp` (see [Environment](#environment)) so the cleaner never
has a reason to touch it in the first place.

## dch serve (Prosper remote hosts)

`dch serve` lets the Prosper iPhone app drive the dch sessions of a host
that runs no Prosper, such as a Linux box on your tailnet. It speaks
Prosper's "remote frames" protocol 1.0 on TCP port 8771, the port the phone
dials. It runs in the foreground and logs to stderr with timestamps.
SIGINT or SIGTERM stops it and leaves every session running.

```sh
dch serve                    # list, attach, kill, rename, watch
dch serve --allow-create     # also let the phone start sessions
dch serve --check-peer 100.101.102.103   # would this peer get in?
```

**Trust model.** Only your own devices on your tailnet get in. Every check
runs right after the connection is accepted, before a byte is read, and a
refused peer is closed without a reply.

- **Address.** `dch serve` listens only on this host's Tailscale IPv4: the
  first line of `tailscale ip -4`. When that reports no address (tailscale
  stopped or logged out), it refuses to start; it does not scan interfaces
  for a 100.64.0.0/10 address, because an ISP's CGNAT address sits in the
  same range. It never listens on 0.0.0.0.
- **Peer range.** A peer must be an IPv4 address in 100.64.0.0/10.
- **Not this host.** A peer on one of this host's own addresses (its tailnet
  addresses, the bound one, any interface's) is refused, and so is one whose
  whois names this host's own node. Any local user's process can connect
  from there, and whois would call it the owner.
- **Owner.** `tailscale whois --json <peer>` must name the same numeric user
  id as `Self.UserID` in `tailscale status --json`, which is read once at
  startup. Login names are never compared for the owner. With no `tailscale`
  CLI, or a `status` that fails, the owner is unknown and `dch serve` refuses
  to start, whatever the flags. The CLI is the first `tailscale` in an
  absolute `PATH` entry (empty and relative entries are skipped), else
  `/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin` or
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale`; its path is logged
  at startup. Identity is the node, not the person on it: every local user
  of any node you own (a shared Linux box, a CI runner) counts as you.
- **Tagged nodes.** A tagged peer is refused. No user owns a tagged host, so
  on one `dch serve` refuses to start unless you name who may connect:
  `--allow-user <id|login>`, repeatable. Prefer the numeric user id (the
  `UserProfile.ID` that `tailscale whois --json` prints). A login is matched
  against `UserProfile.LoginName` without regard to ASCII case, as Tailscale
  treats login names; `--allow-user` grants the serving user's full shell.
  `--allow-user` is only for tagged hosts; an untagged host serves its owner,
  and `dch serve` refuses `--allow-user` there.
- **Whois down.** When `tailscale whois` fails, the peer is refused and a
  line is logged. `--whois-optional` accepts it instead, with a warning, only
  when tailscaled itself is not answering (`tailscale status` fails too). A
  whois that fails while `status` works (the peer is not a node tailscaled
  knows), and any answer that is not clean (unparseable, a duplicate key,
  an id that is not an integer, over 256 KiB, timed out), is refused with or
  without the flag. The owner must still be known at startup.
- **Repeat refusals.** A peer refused on tailscaled's word is refused again
  for 30 s before any process is started for it (at most 64 addresses
  remembered).

**Flags.**

| Flag | Effect |
| --- | --- |
| `--bind <ipv4>` | Listen there instead. Relaxes nothing: the peer checks above still apply, so an address that is not on your tailnet admits nobody (it warns). A wildcard or non-unicast address is refused. |
| `--port <n>` | Another port, 0 for any (tests). The phone always dials 8771. |
| `--allow-create` | Let the phone start a session with its own command (0x02). Off by default, and `create` is then missing from the ops the phone sees, and attach (0x01) only reaches a session already in the list (else 0x13 and a close). With it, attach to a new name starts a shell session, as `dch -n` does. |
| `--allow-user <u>` | On a tagged host, accept this user's nodes (numeric id or login). Repeatable. |
| `--whois-optional` | Accept a tailnet peer while tailscaled is not answering (whois and status both fail), with a warning (see above). The owner must still be known at startup. |
| `--check-peer <ip>` | Print whether that peer would get in, then exit 0 (accept) or 1. |

**What the phone gets.** The session list (`dch --ls-json`, re-encoded: name,
alias, state, agent count, and only GitHub pull-request links from `--meta`),
a live list for its home screen (polled every 2 s), attach with resize and
redraw, kill, rename, and a screen snapshot from the terminal mirror (empty in
a dch-lite build). An attached connection runs a real `dch -E -f -n <name>`
client in a pseudo-terminal, so the newest attach to a session retires the
older connection. Commands run as argument vectors, never through a shell.

**Port in use.** On a Mac where Prosper's Remote Terminal is on, Prosper
already holds port 8771, and `dch serve` refuses to start with a message
saying so (the same for a second `dch serve`). It never takes another port
and never retries: restarting is the service manager's job.

**systemd** (`~/.config/systemd/user/dch-serve.service`, then
`systemctl --user enable --now dch-serve`; run `loginctl enable-linger $USER`
so it starts without a login):

```ini
[Unit]
Description=dch serve (Prosper remote host)
After=tailscaled.service network-online.target
Wants=tailscaled.service network-online.target

[Service]
ExecStart=/usr/local/bin/dch serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

The `tailscale` CLI must be on the service's `PATH` or in one of the fixed
locations above (systemd's default `PATH` includes `/usr/bin` and
`/usr/local/bin`).

**launchd** (`~/Library/LaunchAgents/dev.dch.serve.plist`, then
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.dch.serve.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.dch.serve</string>
  <key>ProgramArguments</key>
  <array><string>/opt/homebrew/bin/dch</string><string>serve</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardErrorPath</key><string>/tmp/dch-serve.log</string>
</dict>
</plist>
```

**Test only.** `DCH_SERVE_TEST_LOOPBACK=1` together with a loopback `--bind`
lets loopback peers past the address check, so the test suite can run the
server on 127.0.0.1 with a fake `tailscale`. The owner check still runs. It
logs a loud warning and is removed from the environment at startup, so
nothing `dch serve` starts inherits it; never set it on a real host.

**Log.** Session names and device ids come from the phone. Every log line
escapes control characters (C0, DEL, C1, bidi overrides and isolates) as
`\xNN` or `\u{NNNN}`, and a session name containing one is refused.

**Limits.** At most 64 connections, 16 per peer, and 4 live-list streams per
phone. A connection that is not attached or watching closes after 30 s
without a whole frame, and any connection closes when its output goes untaken
for 20 s. Control frames over 64 KiB, and any frame over the protocol's
256 MiB guard, close the connection. Sent frames are at most 8 MiB.

The golden fixtures in `tests/remote-frames/` are Prosper's conformance set;
`tests/serve_test.py` runs them.

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

Codex 0.167 and later run threads in a shared background app-server daemon
unless the TUI is started with `--no-daemon`. The daemon keeps the environment
of the session that started it, so its shell snapshots carry that session's
`DCH_SESSION` and launch marker for every TUI's threads. dch therefore starts
`codex …` and `headroom wrap codex …` with `--no-daemon` added after the
`codex` word, when that Codex's own `--help` lists the flag (a probe of about
20 ms at launch; a script launcher such as npm's `codex.js` is not probed). It
leaves the argv alone for a non-TUI subcommand (`exec`, `review`, `app-server`,
…), with `--remote`, and when you already passed `--no-daemon`. The recorded
argv keeps what you typed. Any other launch form gets no flag: if a Codex
process that is not under the session's own dch master (the daemon, parent
pid 1) carries the session's `DCH_SESSION` (and launch marker), the session
refuses to bind a peer, the picker and `--ls-json` show no Codex title for it,
inbound messages are refused, and `--agent-send` from it is refused. Once a
sidecar has seen such a daemon it keeps refusing for its lifetime. Start the
session again with `codex --no-daemon` to recover.

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
- Everything else in this README: state detection, the action picker, live
  `--restart`, history and `--revive`, Claude Code control (`--say`,
  `--stop`, auto-allow), native Claude/Codex peers and socket self-heal.

## License

GPLv2 (inherited from dtach). See `COPYING`.
