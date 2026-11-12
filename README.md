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
dch -l                         # → the picker: states, actions, history, filter (/), settings (,)
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
there are some; and **History**, the ended sessions of `--history`, one row
per session name (its newest record, pinned when any record of the name is):
every pinned name, then the newest other names while there are fewer than 50
rows, so pinned names alone can make more (`--history` itself still lists
every record, newest first). With remote hosts on, History also lists the
other hosts' ended sessions as `host:name`, the host faint, at most 50 names
per host on top of the local rows, so a full local History never hides them
(see [Remote hosts](#remote-hosts)). Rows sort pinned first (a remote row
never is), then by the name shown (alias, else agent name, else name; a
remote row's starts with `host:`, and it sorts among the local ones),
case-insensitive, digit runs by value (`a2` before `a10`). On a remote row, `Enter`, `m` and `Shift-Enter`/`o`
resume the record on its host (`o` then attaches to `host:<as>`; see
[Remote hosts](#remote-hosts) for the command), a host
whose `dch serve` lacks `--allow-create` answering in one status line, and
`p` and `d` say they are for this host's records. Every list has column headers and fixed, aligned columns,
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
| `Shift-Enter` / `o` | attach (`Shift-Enter`; as `Enter`) | resume, then attach to it once it is up (open); `dch -l` and the switch only, the `-k` / `-d` pickers say so and do nothing |
| `r` | rename (set the alias; empty clears) | |
| `R` | restart the agent (`--restart <n> --include-agents -f`), asks first | |
| `S` | stop (`--stop`), asks first | |
| `x` | kill (`-k`), asks first | |
| `X` | open the session's [X-ray view](#x-ray-view-dch---xray-n); `q` comes back (a remote row's runs on its host) | the same, for the row's record |
| `a` | allow a plain Yes/No permission prompt (presses `1`, Yes), asks first; a question form or any other prompt is refused: attach to answer it | |
| `b` / `B` | restart stale agents / restart all agents (force), asks first | |
| `s` | sort by activity (blocked, working, done, idle, newest first) or by name (`a2` before `a10`); saved as `picker_sort`, so the next picker starts in it | |
| `m` | | resume without the message (`--no-message`) |
| `I` | | resume interrupted (n) (`--revive --all`), asks first |
| `p` | | pin / unpin every record of the row's name |
| `d` | | remove every ended record of the row's name (`--forget`), asks first; a pinned name must be unpinned first. While a session of that name is live, one press removes at most 8 of its ended records (the rest stay a row: press `d` again) |
| `/` | filter (below) | filter (below) |
| `,` | settings | settings |
| `q` `Esc` `Ctrl-C` `Ctrl-D` | quit (`Esc` clears a filter first) | quit (`Esc` clears a filter first) |

`/` filters, as in vim: a `/` prompt with a caret opens on the status line and
what you type narrows every tab to the rows whose shown name, dch name or
directory contains it (case-insensitive; a remote row's `host:name` too). The
tab counts show the filtered counts, the selection starts on the top match,
and the filter stays across tab switches and reloads, with `No match` for an
empty result. While typing, every printable key goes to the filter (no letter
binding fires; `j` `k` type too), `Tab` switches tabs, `Backspace` edits (on
an empty filter it closes the prompt), `Ctrl-U` clears the text, `↑` `↓`
close the prompt and move, keeping the filter, so the letter actions (`x`,
`S`, `R`, ...) work on the filtered rows, and `Enter` closes the prompt and
acts on the selected row as it always does. `/` reopens the prompt on the
kept text; the filter stays until `Esc`, which clears it, and a later `Esc`
quits.

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
`Shift-Enter` or `o` on a History row resumes it the same way and, once the
revive reports `ok`, attaches to the session it made (by the name the revive
answered, so never another session of that name); a failed revive leaves you
in the picker with its line. The picker turns on the kitty keyboard protocol's
disambiguate flag (`CSI > 1 u`) while it is up, popped again before it exits
or attaches, so a terminal that speaks it (Ghostty, kitty, WezTerm, foot)
sends `Shift-Enter` apart from `Enter`; xterm's modifyOtherKeys form works
too. Where the terminal cannot tell them apart, `o` does the same.
A session the picker opened (`dch -l`, the switch, `Shift-Enter`) that ends —
its program exits, Claude Code after `Ctrl-D` twice, a shell's `exit` — takes
you back to the picker instead of the shell, on History with the record that
end wrote selected. `q` there leaves for the shell; so does `Esc`, but a
filter kept from finding the row takes the first `Esc` and the second leaves.
A session attached
with `dch <name>` or `dch -a <name>`, or a remote one, still exits to the
shell when it ends.
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
dch --follow infra                    # stream the screen as it changes
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
Brew pours a prebuilt bottle on macOS 14 or later (arm64 and x86_64) and on
Linux (x86_64 and arm64), so no compiler is needed there; anywhere else it
builds from source. The formulas and bottles are published to the tap by
dch's release workflow on every release.

## Install a prebuilt binary

Each [release](https://github.com/ventsislav-georgiev/dch/releases) carries
`dch-<version>-<os>-<arch>.tar.gz` (full) and
`dch-lite-<version>-<os>-<arch>.tar.gz`, each with a `.sha256` beside it, for
`darwin-arm64` and `darwin-x86_64` (macOS 14 or later) and `linux-aarch64`
and `linux-x86_64` (glibc 2.28 or later: RHEL 8, Debian 10, Ubuntu 18.10 and
newer). A tarball holds `dch`, `dch.1`, `COPYING` and this README; the
full build also holds `LICENSE.libghostty-vt`.

```sh
v=1.57.0 t=darwin-arm64           # or darwin-x86_64, linux-aarch64, linux-x86_64
d=dch-$v-$t                       # or dch-lite-$v-$t
u=https://github.com/ventsislav-georgiev/dch/releases/download/v$v
curl -fLO $u/$d.tar.gz -fLO $u/$d.tar.gz.sha256
shasum -a 256 -c $d.tar.gz.sha256 # Linux: sha256sum -c $d.tar.gz.sha256
tar -xzf $d.tar.gz
mkdir -p ~/.local/bin ~/.local/share/man/man1
install -m 0755 $d/dch ~/.local/bin/dch
install -m 0644 $d/dch.1 ~/.local/share/man/man1/dch.1
dch --version
```

Make sure `~/.local/bin` is in your `$PATH`. A file fetched with curl
carries no quarantine flag, so macOS runs it as is. One downloaded in a
browser does: clear it on the installed binary with
`xattr -d com.apple.quarantine ~/.local/bin/dch`.

## Build from source

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
dch -a host:name # attach to a session on another host (see Remote hosts)
dch --read host:name  # a control verb on another host (dch serve 1.41+)
dch -a <name> --scrollback 200  # attach with the last 200 lines of history above the screen
dch -rl          # rename: pick a session, type a display alias, repeat
dch -m <name> [alias]  # set the alias without the picker (none: clear it)
dch -k [name]    # kill a session (interactive picker if no name)
dch -k -- <name> # kill one whose name starts with '-' (an older dch made it)
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
dch --follow <name>                             # stream screen frames on change
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
dch --shell-init zsh|bash|fish                 # OSC 133 prompt marks for your rc file (X-ray)
dch --xray <n>                                  # the X-ray view: chapters, refs, on-screen hints (full screen)
dch --xray <n> --json                           # a session's chapters (prompts / commands) as one JSON object
dch --xray <n> --clear --json                   # empty a shell session's X-ray ring
dch --xray <n> --follow --json                  # the same object, then its deltas as NDJSON until the session ends
dch --xray <n> --ref <ref> [--scope session|project|all] [--full] [--budget-ms <ms>] --json  # one ref's mentions and definitions, NDJSON
dch --xray <n> --chapter <anchor> --json        # one chapter's entries (prompt, replies, tool calls, output)
dch --agent-list [--json]                       # native Claude peers
dch --agent-send <peer> <message...>            # message one (from a Codex session)
```

Inside a session: `Ctrl-\` to detach. Works in vim, fzf, less, Claude Code,
etc. — terminals that swallow most control keys still pass `Ctrl-\` through.

Press `Ctrl-\` **twice quickly** to switch sessions: dch detaches and shows the
same picker as `dch -l`, so you can hop straight into another session. It opens
where the last picker of that dch left off (its tab, sort and `/` filter) with
the session you just left selected, on whichever tab holds it now (Sessions
or Sidekicks); the first switch after a plain attach opens on that session
too, and so does a session opened from History with `Shift-Enter`. Quitting
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
| `--spawn <n> [--size CxR] [--env K=V]... [cmd...]` | Start headless session (default: `$SHELL`, 80x24 or `$COLUMNS`/`$LINES`). `--env` (max 32, first `=` splits) adds variables to the child; reserved keys (`DCH_SESSION`, `TERM` fixup) always win. Like `--revive` (and a plain `dch <new>`), it drops the calling Claude Code session's identity (`CLAUDECODE`, `CLAUDE_CODE_*` session vars), plus `GIT_EDITOR` and the other harness vars when `CLAUDECODE` is set, and sets `LC_TERMINAL=ghostty` unless the caller has it (dch keeps OSC 8 links, but under `TERM=xterm-256color` with no `TERM_PROGRAM` Claude Code would strip its statusline's, and `LC_TERMINAL` is the one signal its check reads that supports-hyperlinks and other Node CLIs ignore; ssh sends it on, by macOS's default `SendEnv LC_*`). Like any session variable it reaches every program in the session, not only Claude Code; a tool that keys on `ghostty` may assume Ghostty-only features (kitty graphics, for one) that the terminal you attach from may lack. dch sets no `FORCE_HYPERLINK`: those CLIs honour it before they check for a terminal, so it put OSC 8 into pipes and files (`stylelint t.css > out`, an agent's Bash tool); a caller's own value still reaches the session. An explicit `--env` (`--env LC_TERMINAL=` included) still wins. Prints the name. A program that cannot be executed is an error (`dch: <n>: <cmd>: <reason>`); one that runs and exits at once is not. | 0 ok, 1 exists/failed/not executable/bad --env |
| `--xray <n> --json` | One JSON object: the session's corpus, chapters and refs (see [X-ray queries](#x-ray-queries---xray)). Without `--json` or any other flag: the full-screen [X-ray view](#x-ray-view-dch---xray-n) (`host:name` too; a non-terminal stdout exits 2). `--clear` empties a shell's ring instead; `--ref` streams one ref's lines; `--follow` streams the object and then its changes; `--chapter` prints one chapter's entries. `<n>` as `host:name` runs it on that host (dch serve 1.55.0+); `host:name --requests --json` holds one connection there for many requests read off standard input. Must be the first argument. | 0 ok, 1 no such session/ambiguous/no corpus, 2 usage, 3 read failure; remote: 4 gate, 6 host lacks `xray`, 7 unreachable |
| `--send <n> <text...>` | Type text (joined with spaces), no enter. | 0 ok |
| `--run <n> <text...>` | `--send` + enter. Into a session whose Claude Code runs — as its command or typed into its shell — the text goes through `--say`'s composer gate instead: typed only over an empty composer, Enter pressed once it shows and again while it stays (Claude Code folds an Enter sent with the text into the paste). A `/command` is typed raw, a message pasted. A shell or any other program, a master without a mirror (`DCH_NO_VT`), dch-lite and your own session (`$DCH_SESSION`) keep the plain burst. A gate failure prints `dch: <n>: <reason> — <why>` on stderr: `prompt` and `draft` (nothing typed), `not_typed` (the text never showed in the composer, no Enter), `not_submitted` (it stayed there through every Enter); `no_screen` when the composer is not on screen; `interrupted`, `withdrawn` or `draft_left` on a SIGINT, SIGTERM or SIGHUP, as for `--say`. | 0 ok, 1 gate failure |
| `--keys <n> <key...>` | Encode+send keys mode-aware: `ctrl+c`, `alt+x`, `shift+tab`, `up`, `f5`, `enter`, `esc`, single chars. | 0 ok, 1 unknown key |
| `--read <n> [--ansi] [--cursor] [--recent [N]]` | Print rendered screen. `--ansi` keeps colors and OSC 8 hyperlinks (each line opens and closes its own, so a `--recent` cut never splits one); `--recent [N]` = last N lines incl. scrollback (default 100; the mirror keeps at least `DCH_SCROLLBACK` lines of history, default 2000, max 100000, set when the session is spawned; at most about one page more, ~600 lines at 80 cols, ~240 at 200; 0 keeps only the screen); `--cursor` also prints `cursor <row> <col> <visible> <wrap>` on **stderr** (see below). Plain output drops all styling — to tell a TUI's own ghost text (placeholders, argument hints) from what the human typed, read with `--ansi --cursor`; see [docs/ghost-text.md](docs/ghost-text.md). | 0 ok, 1 error, 3 no mirror |
| `--follow <n>` | Stream the screen, read-only, as self-delimited frames: one at once, then one per change, at most one per 150 ms (`DCH_FOLLOW_MS`). Each frame is the full `--read --ansi` screen plus size and caret; see [Following a screen](#following-a-screen---follow). Runs until the session ends or the reader closes the pipe. | 0 session ended / reader closed, 1 no session / error / master predates `--follow`, 3 no mirror |
| `--wait <n> --match <str> [--timeout ms]` | Block until screen/scrollback contains `<str>` (literal substring, ≤512 bytes); prints the matching line. Default timeout 10 s. | 0 hit, 2 timeout, 3 no mirror |
| `--status <n>` | Print the session state: `working`, `idle`, `blocked`, or `done` — resolved from the reported state, the screen-content detection, and the output heuristic (see [Agent state detection](#agent-state-detection)). | 0 ok, 1 no session |
| `--report <n> <state>` | Push a semantic state: `working`, `idle`, `blocked`, or `done` (closed set, unknown tokens rejected); `clear` reverts to auto (detection + heuristic). Last write wins; cleared automatically when the session ends. | 0 ok, 1 no session / bad state |
| `--meta <n> '<json>'` | Attach one JSON object of metadata to the session (see [Optional: session metadata](#optional-session-metadata)); `clear` removes it. Replaced atomically, last write wins, never interpreted; a byte-identical push is a no-op; rejected input leaves the stored object unchanged. Removed automatically when the session's program exits. | 0 ok, 1 no session / not a single-line JSON object ≤ 16 KB |
| `--wait <n> --state <s[,s...]> [--timeout ms]` | Block until the state matches any of the comma list (e.g. `idle,blocked,done` = "turn over"); prints the matched state. `active` is accepted as an alias for `working`. Polls every 100 ms. | 0 hit, 2 timeout, 1 no session |
| `--ls-json` | All sessions as JSON: `[{"name","alias","harness","activity_epoch","state","version","agents","meta","meta_epoch","auto_allowed","claude_session_id","transcript_path","cols","rows","clients"}]`. `cols` and `rows` are the pty size the session's master holds (set by the attached clients' windows, or `--spawn --size`), `clients` how many clients are attached right now; read in the same control round trip as the rest, never by attaching or resizing, and left out (not `0`) for a master too old to report them and for `--remote` rows. Every row ends with `"cwd"`, after every other key: the directory the session started in, from its live history record, `null` when it has none (a master from before history records) or the record already says it ended; a `--remote` row's is the host's, from its `dch serve` list, `null` from a server that does not send one. `harness` is the name of the Claude Code or Codex session running inside, or `""`; the real `name` remains separate. `state` follows the same resolution rule as `--status`. `version` is the dch version of the master serving that session, or `""` for a master old enough not to report one. `agents` is the number of live subagents on Claude Code's agent panel (the `⏺ main` block under the prompt), `0` when no panel is on screen — including while a prompt covers it — or `DCH_NO_DETECT=1` is set. `meta` is the object last pushed with `--meta`, verbatim, or `null`; `meta_epoch` is when that content last changed (epoch seconds, like `activity_epoch`), `0` with no meta. `auto_allowed` is the permission prompt the master answered last with [auto-allow](#auto-allow), `{"at":<epoch seconds>,"title":"<the dialog's first line, e.g. Bash command>"}`, or `null`; cleared when the session's program exits, like `meta`. `claude_session_id` is the `sessionId` of the one live interactive Claude Code under the session's program, read fresh from its own `<config>/sessions/<pid>.json` (pid descending from the program, `procStart` matching the process; `CLAUDE_CONFIG_DIR` honoured), so it follows `/clear` and resume; `null` when no Claude runs there, when more than one does, once it exits, or when the id is not a UUID. `transcript_path` is that conversation's absolute `<config>/projects/<dir>/<sessionId>.jsonl` when exactly one such file exists, else `null` (also right after `/clear`, until the new conversation writes its first line). | 0 |
| `--restart <n>` \| `--restart --all` `[--include-agents] [-f] [--json]` | Re-exec the session's master onto the current `dch` binary, in place — the program inside keeps running (see [Upgrading a live session](#upgrading-a-live-session)). `--all` restarts only sessions whose `version` differs from this binary's; `-f` restarts all of them. With `--include-agents`, it then restarts the stale Claude Code agents of those sessions — Claude as the session's command; one typed into its shell is skipped (`in_shell`); every session with `--all`, only `<n>` otherwise; `-f` forces current agents too (see [Restarting Claude Code agents](#restarting-claude-code-agents)). Without the flag no agent is touched, `--all` included. `--include-agents` without `--restart` is a usage error. `--json`: stdout is NDJSON, the master pass included, with or without `--include-agents` (see [Machine-readable output](#machine-readable-output)). | 0 ok, 1 failed / master too old / an agent restart failed |
| `--respawn <n> [-- cmd...]` | Arm a one-shot respawn: when the session's program next exits, the master keeps the session (same socket, pty size, attached clients) and starts `cmd` in its place. No `cmd` arms a *hold*: the session stays up childless until a second `--respawn <n> -- cmd` starts one, or until `DCH_HOLD_MS` (default 120000) ends it. An arm that nothing consumes expires after `DCH_RESPAWN_TTL_MS` (default 300000); both are read from the master's environment. `--restart` refuses while a respawn is armed or held. | 0 ok, 1 no session / refused / master too old |
| `--respawn <n> --cancel` | Drop a pending arm. On a held (childless) session it ends the session. | 0 ok, 1 no session |
| `--cmd <n>` | Print `{"pid","state","argv","exit_code","exit_signal","tail"}`: the child's pid (`null` while held), `state` `running`, `armed` or `held`, the argv it was started with, the exit status of a child reaped under an arm (`null` otherwise), and `tail`, the last 8 KB the child wrote since the arm, raw. | 0 ok, 1 no session / master too old |
| `--history [--json]` | List the sessions that ended, newest first, one line each: `name (alias)  when  end  cwd  $ argv  [claude <name> <session id>]`. `end` is `exit N`, `signal N`, `signal N from <pid> <command>` (the master itself got SIGTERM or SIGINT from that process: its command line made printable, cut at 120 bytes; `signal N from <pid>` when it was already gone), `killed` (`dch -k`), `stopped` (`dch --stop`) or `vanished` (the master died without a word: SIGKILL, power loss). Only the records of the current socket dir are listed. A pinned record (`--pin`) ends its line with `  [pinned]`, one a new session under its name moved aside with `  [record <name>.<ended>]`. `--json` prints the stored records as an array, a pinned one with `"pinned":true` added (absent otherwise), every one with `"file"`, its record file without `.json` (`work`, or `work.<ended>` for a moved one), the exact handle for `--pin`/`--unpin`/`--forget <file>.json` (see [Bringing sessions back](#bringing-sessions-back)). With `--remote`, after the local records (unchanged, byte for byte) come the remote hosts' (see [Remote hosts](#remote-hosts)): text lines as `host:name ...`, ending `  [stale]` for a host's cached answer; JSON records as the host sent them plus `"host"` and `"stale"`. | 0 |
| `--revive <n> [--as <new>] [--no-message]` \| `--revive --all [--no-message]` | Start ended sessions again: in their cwd, with their argv, with the Claude Code agent in them resumed via `--resume <id>` and told to carry on (see [Bringing sessions back](#bringing-sessions-back)). `--no-message`: resumed only, no message typed (`ok`, the line says it was not sent). `<n>` revives its newest ended record, whatever its end — never a live session's; `<file>.json` (or the bare file `--history` prints, `[record <file>]`) exactly that record file; a live `<n>` is refused unless `--as <new>` names another session. `--all` revives every session that died — anything but `exit 0`, `killed` and `stopped` — within the last 3 days after running at least 5 minutes, and gives one whose name is now taken the first free `<n>-2`, `<n>-3`, …. Older or shorter-lived sessions stay in `--history` and revive by name. Never two Claudes on one conversation: a record whose Claude id a live Claude already runs is skipped by `--all` and refused by name (`live_sid`, naming the holder), and of two records carrying one id `--all` revives only the newest (`dup_sid`). One line per session. `--json`: stdout is NDJSON, each result carrying `"as"`, the name it came back under (see [Machine-readable output](#machine-readable-output)). | 0 ok (skips included), 1 no history / ambiguous / name live / id live / a revive or resume failed |
| `--pin <n>... [--json]` \| `--unpin <n>... [--json]` | Pin a session's history record so the 30-day prune never takes it, or unpin it (see [Bringing sessions back](#bringing-sessions-back)). `--pin` needs a record for the name, live or ended (`<n>: failed — …`, `not_found`, otherwise), and pins every record of the name; pinning a pinned one is `ok`. `--unpin` unpins every record of the name and is `ok` with or without a pin, but `not_found` when there is no record (nor a stray pin) to act on. An argument ending in `.json` names exactly one record file instead, never a name: `--pin work.json` pins the primary `work.json` only, `--pin work.<ended>.json` only that moved record. One that is also a session's name (sessions `a` and `a.json`: `a.json` would be both) is refused, `ambiguous`, nothing touched: `a.json.json` names session `a.json`'s record. A bare argument also matches a record file exactly as given, so the `[record <file>]` that `--history` prints works for a long name's moved record (which the name shortening would hash again). One line per name: `<n>: pinned`, `<n>: unpinned` or `<n>: failed — <why>`. `-f` is a usage error. | 0 nothing failed, 1 usage error / a name failed |
| `--forget <n>... [-f] [--json]` \| `--forget --all [--json]` | Delete history records of the current socket dir, matched by name as `--revive` matches them (every record that matches); `<file>.json` (`work.json`, `work.<ended>.json`) deletes exactly that one record file; one that is also a session's name is refused, `ambiguous`, as for `--pin`. Never a live session's (`live`: its master would write it back; `-f` does not change that); a pinned one only with `-f`, which removes its pin too (`pinned` otherwise); `not_found` when no record matches. One line per record: `<n>: forgotten` or `<n>: failed — <why>`. `--all` deletes every record that is neither live nor pinned and skips the rest: one `<n>: forgotten` per record deleted, then `forgot N (K pinned, L live kept)`; `-f` with `--all` is a usage error (it never removes a pin). | 0 nothing failed (skips included), 1 usage error / a name failed |
| `--say <n>... [--json] -- <text...>` \| `--say --all [--json] -- <text...>` | Type one message into the Claude Code running in each session — as its command or typed into its shell — and submit it, idle or busy: a busy Claude (or one running a background shell) queues the message, just as Enter does when you type it yourself, and takes it once its turn ends; one with messages already queued queues it after them. Typed only when the screen shows its empty composer (its placeholder, also the "Press up to edit queued messages" one), never over a prompt or dialog (the Enter would answer it) or an unsent draft, never waiting for either. Text is the words after the required `--` joined with spaces. Sessions are typed into concurrently; Enter is pressed again while the text still sits in the composer, never once a dialog is up; text that left the composer — submitted, or shown queued above it — is `sent`. One result per session as it finishes: `<n>: sent`, `<n>: skipped — <why>` (`not_claude` under `--all`, `self` = `$DCH_SESSION`, `prompt`, `draft`, `no_screen` — always on dch-lite) or `<n>: failed — <why>` (`no_session`, `not_submitted`, `not_typed`, `old_master`, `interrupted`, `withdrawn`, `draft_left`). A first SIGINT, SIGTERM or SIGHUP stops every session at a safe point: once it is handled nothing more is typed and no Enter is pressed; text already in the composer is taken back out one Ctrl-U at a time (Claude Code's Ctrl-U kills one wrapped row), the screen re-read after each; each session not yet `sent` fails with one of the last three; a second signal exits at once (`--run` and `--revive` do the same). A paste still painting is waited for first (up to `DCH_AGENT_LAND_MS`, 30 s). Limit: an Enter sent before the signal that Claude takes only after it sends the text, which is then reported `withdrawn`. `--json` prints one `{"name","result":"ok\|skipped\|failed","reason","detail"}` object per line instead. | 0 nothing failed (skips included), 1 usage error / a session failed |
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
  `"name"`: the name the session came back under, and `"running"` last:
  `true` when the session's agent process is alive as the line is written,
  on every result and reason, else `false`. `--pin`, `--unpin` and
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
| `interrupted` | say, run, revive | A SIGINT/SIGTERM/SIGHUP came before anything was typed (`--revive`: before the session started, or before its resume message): nothing typed. |
| `withdrawn` | say, run, revive | The signal came once the text was typed: it left the composer under dch's Ctrl-Us, not sent (unless Claude took an earlier Enter late, after the signal: then it was sent). |
| `draft_left` | say, run, revive | The signal came once the text was typed and it could not be taken back: still there at the deadline, a dialog over it (the keys went to the dialog), or a paste that had not painted by the end of the land wait. It may still sit in the composer, not sent. |
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

### Following a screen (`--follow`)

A tool that mirrors a session's screen live — a pane in another app — would
otherwise poll `--read --ansi --cursor` on a timer and trail a busy session by
a whole period. `dch --follow <name>` streams it instead: one frame at once,
then one whenever the screen, its size or the caret changed, at most one per
150 ms (`DCH_FOLLOW_MS`, 20–10000). Each frame on stdout is

```
dch-frame <len> <rows>x<cols> <crow> <ccol> <cvis>\n
<exactly len bytes of payload>
```

- The header is one ASCII line, fields separated by single spaces, ending in
  `\n`. `dch-frame` names this format; a future incompatible one gets a new
  first token, so reject a header that does not start with `dch-frame `.
- `len`: payload byte count (decimal). The payload is byte-for-byte what
  `dch --read <name> --ansi` prints, its trailing newline included: read
  exactly `len` bytes, then the next header starts.
- `rows`x`cols`: the screen size (the mirror's grid, capped at 1024x1024).
- `crow` `ccol`: the caret, 1-based like `--cursor` (row counted from the top
  of the visible screen). `0 0` only when the screen rendered over 2 MB and
  the dump was tail-cut, so the caret has no row in it.
- `cvis`: `1` caret visible, `0` hidden (DEC mode 25).
- Every frame is whole: a frame sent only because the caret moved or was
  hidden still carries the size and the full payload. No short forms.

A frame is sent only when it differs from the last one sent (payload, size
or caret). An idle session costs a few syscalls per tick: the master tells
`--follow` "unchanged" without rendering. Following is read-only like
`--read`: it never attaches, writes nothing to the session, sends no size,
and leaves the activity stamp, `--status` and attached clients alone. A
`--restart` of the session is followed through (it reconnects).

Exit 0 when the session ends or the reader closes the pipe (quietly: no
`SIGPIPE`, nothing on stderr); 1 when there is no such session, the master
stopped answering, or the session's master predates `--follow` ("restart
the session"); 3 when the session has no mirror (dch-lite, `DCH_NO_VT`),
like `--read`.

### Agent state detection

`--status` and `--ls-json` answer "what is this agent doing?" with zero
setup: dch reads the session's rendered screen (the same mirror `--read`
prints) and looks for the strings coding-agent TUIs paint when they are
busy or waiting on a human.

| State | Signal |
| --- | --- |
| `blocked` | Permission/confirmation prompts: "Do you want to proceed?", "Esc to cancel · Tab to amend" (Write/Edit), "Claude has written up a plan …" (plan approval), "Allow command?", "Press enter to confirm or esc to cancel", "permission required", "waiting for approval", … (Claude Code, Codex, Gemini, opencode, Cursor). With Claude Code's input box on screen, only the rows below it count: a reply that quotes a prompt is not one |
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
| `remote_hosts` | empty: your tailnet peers; `off`: none; else a `[label=]host[:port],...` list (see [Remote hosts](#remote-hosts)) | empty |
| `focus_resize` | `true`/`false`: an attached client takes the session's size when its window gains focus (see [The focused client sets the size](#the-focused-client-sets-the-size)) | `true` |
| `xray_ring` | `0` (off) to `16M` (`K`/`M` suffixes, rounded up to 64 KiB): the shell output each new session keeps in its master's memory for X-ray, the rows that scroll off its primary screen, as text, with OSC 133 prompt marks. Read when a session starts (see [Shell output ring](#shell-output-ring-x-ray)); kept across `--restart`; never on disk, never in core dumps. dch-lite keeps none | `2M` |

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

**Prompt marks for X-ray.** A shell inside dch that marks its prompts, commands
and exit codes with OSC 133 lets X-ray tell commands and their output apart.
Many prompts and terminal integrations already emit them (fish 4 does). For the
rest, add one line to your rc file:

```sh
eval "$(dch --shell-init zsh)"      # ~/.zshrc
eval "$(dch --shell-init bash)"     # ~/.bashrc (bash 4.4+)
dch --shell-init fish | source      # ~/.config/fish/config.fish (fish < 4)
```

The hooks are idempotent: sourcing twice adds nothing. A terminal without OSC 133
support ignores the marks.

### Shell output ring (X-ray)

With the terminal mirror on (not in dch-lite), every session's master keeps
the plain text of the shell's output in memory, in a ring of `xray_ring` bytes
(default 2 MiB, allocated in 64 KiB chunks as it fills).
- **What goes in.** A row goes in when it scrolls off the top of the
  primary screen. The text is ANSI-free, with trailing blanks trimmed and soft
  wraps joined. The alternate screen (vim, less, full-screen TUIs) is never
  captured.
- **Clearing.** Erasing the scrollback (`clear`, `ESC[3J`, `reset`/`ESC c`)
  empties the ring; `--restart` keeps it. A terminal's own clear-screen key
  (Cmd-K) may not clear X-ray: it clears the terminal's window and sends
  nothing to the shell. Run `clear`.
- **Agents.** The ring pauses while an agent (Claude Code, Codex) runs in the
  shell, because the agent's own transcript (a Codex's rollout) is its record.
  The master looks for one on its record tick (once a minute), every second
  while a `--follow` is subscribed, on a clear, and before it answers an
  X-ray read (when the screen changed since its last look, else at most
  every 100 ms). So an agent's screen is not read into a shell answer. Only
  what Claude Code draws before it writes its session record can be, until
  the first look after that record (100 ms at most). Rows that scroll off
  before the first look go into the ring. It resumes at the shell's next
  prompt.
- **`--restart`.** The ring survives `dch --restart`: it is handed to the new
  image in memory and never written to disk. A `dch --restart` from a client
  older than this release starts the session on an empty ring: such a client
  may be a dch from before the ring, so the master hands it no ring and puts
  your core-dump limit back before the re-exec.
- **Core dumps.** The master lowers its own soft core-dump limit to 0 so ring
  text cannot land in a core file. The program it runs keeps your limit.

The ring is read over the session's own control socket (same user only). It
is not a ctl verb, so `dch serve` does not expose it.

Prompt and command marks make the output searchable by command. They come from
OSC 133 (`A` prompt, `B` input, `C` output, `D;<exit>` end). Shells started
by a dch master do not get the outer terminal's shell integration, so add the
hooks to your rc file:

```sh
eval "$(dch --shell-init zsh)"     # ~/.zshrc
eval "$(dch --shell-init bash)"    # ~/.bashrc (bash 4.4 or newer: it needs PS0)
dch --shell-init fish | source     # ~/.config/fish/config.fish (fish 4 emits them itself)
```

They are idempotent: sourcing twice adds nothing. A shell that already emits
OSC 133 needs none of this. A command block counts as typed only if client
input (keystrokes, `--send`, `--keys`) reached the pty between its `B` and its
`C`. A program that prints a fake prompt and command in one burst gets no
credit for it.

### X-ray view (`dch --xray <n>`)

`dch --xray <n>` with no other flag opens a full-screen view of session
`<n>`'s X-ray. `X` on a picker row opens the same view, and `q` returns to
the picker. The view asks the same engine as `--json` and holds its index as
`--follow` does, so a live session's new chapters and refs appear while it is
open. It writes nothing to disk. Text from the session (titles, lines,
passages) is drawn with every control and bidi character shown as `?`.

The view has three tabs:
- **Chapters**: one row per prompt or command. `/` filters. `Enter` opens
  the chapter read-only (prompt, replies, tool calls, output), scrollable.
  A chapter whose prompt the harness wrote, not you (a task notification,
  `"auto"` in `--json`), is hidden until `n` shows it, faint and titled by
  its summary (by its title when it has none); the footer says how many are
  hidden, and `/` matches a summary too. The tab's count is the chapters
  shown without `n`. A host or an older dch that sends no `auto` shows
  every chapter. `o`, here and in a chapter, leaves the view (as `q` does)
  and attaches to the live session, as `dch -a <n>` does (`host:name` too;
  from the picker's `X`, as the picker's `Enter`; the `-k` and `-d` pickers
  say it is not theirs). On an ended session `o` says so and the view
  stays. The attach opens at the session's live screen, not at the
  chapter.
- **Refs**: what the session touched: the files, commits, pull requests,
  issues and URLs it mentions, one row each, a faint line over them saying
  so. Rows are by count, highest first, then the most recent first (later in
  the index; a ref seen again while the view is open moves up its count's
  rows). A ref with a definition or two or more mentions is bold. A file
  under the session's directory (top right) shows relative to it, else one
  under your home starts with `~`. A path too long for the row keeps its
  end, the `…` in front; a long URL drops its `https://` and keeps its host
  and its end. Files under `/tmp`, `/private/tmp`, `/var/folders` and
  `$TMPDIR` (a remote view: not `$TMPDIR`, which is this machine's) are
  scratch: hidden until `t` shows them, and the footer says how many are
  hidden. The tab's count is every ref, hidden ones included. `/` filters on
  the full ref. `Enter` opens its card.
- **On screen**: the session's screen as it is now, with a hint label (`a`,
  `s`, `d`, …) on each ref the Refs tab shows bold. Typing a label opens that
  ref's card. The tab's count is the labels on the screen, shown once the
  screen has been read (0 included); before that, and when there is no
  screen, no number. An ended session and dch-lite have no screen to show.

A ref's card shows its definitions (two or more as "N definitions"), its
first and latest mention, its counts (this session, this project, all
projects) and its mentions. It opens light, as `--ref` does without `--full`.
`f` runs the full scan, `o` scans other projects too, `y` copies the ref to
the clipboard (OSC 52). `Enter` on a mention in this session opens its
chapter. A shell session's light card shows titles and counts only; `f`
shows its lines.

The line under the tabs names the corpus: `Shell · last N rows`, `Shell · no
shell integration · mentions only`, `Codex · mentions only`, `X-ray needs the
full dch build` (dch-lite), `X-ray is off for shells (xray_ring = 0)`, and so
on. A shell's line adds "a terminal's own clear-screen key may not clear
X-ray; run `clear`".

| Key | List | Card / reader |
| --- | --- | --- |
| `Tab` `→` `l` / `Shift-Tab` `←` `h` | next / previous tab (`h` `l` are labels on On screen) | `←` `h`: back |
| `↑` `↓` / `j` `k`, `PgUp` `PgDn`, `g` `G` | move | move / scroll |
| `Enter` | read the chapter / open the card | card: read the mention's chapter |
| `/` | filter | |
| `t` | Refs: show / hide the temp files | |
| `n` | Chapters: show / hide the auto chapters (task notifications) | |
| `o` | Chapters: attach to the session (in the chord: back to it) | reader: the same |
| `f` `o` `y` | | card: full scan, other projects, copy |
| `q` | quit | back |
| `Esc` | clear the typed label or the filter, else quit | back |
| `Ctrl-C` `Ctrl-D` | quit | quit |

**While attached**, press the detach key (`Ctrl-\`) and then `x` within the
double-press window (`DCH_DOUBLE_TAP_MS`, default 300 ms). The view opens
over the session on its On screen tab, and the session keeps running
underneath: the attach is kept, never retired. Leaving the view (`q`,
`Esc`, or `o` on Chapters or in a chapter) returns to the session with its screen repainted as it is now,
output that came in meanwhile included (dch-lite has no screen to repaint
from: the program gets a SIGWINCH, as on any re-attach). The `x` is never
typed into the session; keys that arrive in the same read as the `x` (a
paste, a fast burst) reach neither the view nor the session. The chord is off under `-E` (no detach key) and
with `DCH_DOUBLE_TAP_MS=0` (one press acts at once); the keys then do what
they did before. Over a remote attach (`dch -a host:name`) the chord opens
the view of `host:name`, as `dch --xray host:name` does: its queries dial
their own ctl connections, and the attach connection is kept. Leaving it,
the host resizes the program for a beat so it repaints, and dch serve
1.58.0+ also re-arms the modes the view turned off (bracketed paste, mouse
reporting, the kitty keyboard flags, the alternate screen). An older dch
serve or the Prosper server only resizes: those modes stay off on your
terminal until you attach again. There is no
`xray_key` setting: the chord takes no key from the program. In the picker,
lowercase `x` is still kill, which asks first; `X` opens the view.

A window smaller than 24x8 shows only "X-ray: the window is too small".

`dch --xray host:name` opens the view of a session on another host (dch
serve 1.55.0+), and so does `X` on a remote row in the picker. Every query
runs on the host, each over its own ctl connection. On screen reads the
host's screen with `--read <name> --ansi`. The host's text is drawn
as a local session's is, every control and bidi character as `?`. A host
that refuses us, lacks `--xray` or cannot be reached shows its line in place
of the tabs (`dch: <host>: does not support --xray (needs dch serve
1.55.0+)`, ...); `q` leaves with that exit code.

Exit codes are those of `--json` below: 0 when you quit, 1 for no such
session, 2 for a flag or a stdout that is not a terminal; for a remote host
also 4 (its gate refused us), 6 (it lacks `--xray`) and 7 (unreachable).

### X-ray queries (`--xray`)

`dch --xray <n> --json` prints one JSON object about session `<n>`: its
corpus and a chapter per prompt. It runs entirely in the calling process. No
master parses anything, nothing is written to disk and nothing is shelled out.

`<n>` is one of these (checked in this order):
- an agent UUID (36 characters of `0-9a-f-`): the live session whose Claude
  Code has that session id, else the newest ended History record carrying
  it (a Codex thread id is not one: name its session);
- a History record name, `<file>.json`: that ended record, even when a new
  session has taken its name since. It is answered from History only and
  never reads a live ring. A plain shell's `<name>.json` is rewritten when the
  name starts again; it still answers `corpus: "none"`, `reason: "ended"`,
  with the record's directory as its label. A value that is also a live
  session's name, or a record's, is refused as ambiguous;
- a session name: the live session, else that name's newest ended record.

A `<n>` with a `:` is resolved locally first: a live session or a History
record of that name. Only when nothing local matches and the part before the
`:` is a known host (`--hosts`, `remote_hosts`, or a cached remote row) is it
`host:name`. With `--json`, dch first checks the flags and values by its
own rules (exit 2 and nothing sent on a bad one), then runs `--xray <name>` and the other
flags as typed on that host's `dch serve` over [ctl](#control-verbs-ctl). It
writes the host's output and standard error line by line as they come (the
`--follow` and `--ref` streams included) and exits with the host's exit
status. A control character, DEL, C1, bidi control or U+2028/U+2029 the host
sends raw is written as a `\u` escape, and a byte that is not UTF-8 as
`\ufffd` (a CR outside a string, JSON space, is dropped): a JSON line stays
the same JSON, and the host cannot drive your terminal. A host that sends
nothing for 65 s (its own 60 s timeout, plus 5 s; time spent waiting on a
slow reader of dch's output does not count) is a lost connection, exit 7;
`--follow` has no such limit. A partial line the host was writing is dropped
when the link is lost, on a cancel and when the host cuts the answer short.
A host line over 16 MiB once escaped ends it: `dch: <host>: unreachable: it
sent a line over 16 MiB escaped`, exit 7. The
host answers by its own release: `--scope`, `--chapter` and the corpus are
the host's. A host whose hello lacks the `xray` token gets nothing and dch
prints `dch: <host>: does not support --xray (needs dch serve 1.55.0+)`,
exit 6. A refusal by the host's gate exits 4, its own refusal of the
request exits 1 (`dch: <host>: <why>`), and an unreachable host or a lost
connection exits 7. Ctrl-C, SIGTERM or a closed standard output cancel the
query on the host, as for the [remote control verbs](#remote-hosts). A
closed standard output shows at the next line the host sends. A `--follow`
of a shell with no ring (dch-lite, `xray_ring 0`) sends nothing after its
snapshot until an agent starts: it runs on until a signal or the session
ends. There is no `--host` form.

`dch --xray host:name --requests --json` holds one warm connection to the
host for many requests, one at a time: it is how a client such as Prosper
asks a remote session one question after another without dialling each
time. It takes `host:name` and `--json` only; a local name exits 2 (run one
`dch --xray` per request there). On connect it writes
`{"schema":1,"ready":{"ops":[...]}}`, the host's ops. Then each line on
standard input is a request, as NDJSON:
- `{"req":N,"argv":["--ref","#831","--full","--json"]}`: the flags after
  `--xray <name>`, `N` from 1 to 2147483647. Only the request forms are
  taken: `--json`, `--ref` (with its `--scope`, `--full`, `--budget-ms`),
  `--chapter` and `--clear`. dch checks them by its own rules first; any
  other form (`--follow`, `--requests`, the view, an unknown flag) or a bad
  value is answered `{"req":N,"stderr":"dch: ..."}` and
  `{"req":N,"exit":2,"why":"refused"}`, and nothing is sent.
- `{"cancel":N}`: stops request `N` on the host. Nothing is written when
  `N` is not running.

Each line the host writes comes back as `{"req":N,"line":<the line>}`, the
line byte for byte (a line that is not one JSON object comes as a JSON
string, a NUL in it as `\u0000`); each standard-error line as
`{"req":N,"stderr":"..."}`, escaped. Output and standard-error lines come
in the order the host sent them.
Every request ends with exactly one `{"req":N,"exit":E,"why":W}`: `E` is
the host's exit status, `W` is `null`, `"cancel"`, `"cap"` or `"timeout"`
(the host cut it short; `"cap"` with exit 2 also when one host line is more
than dch carries: over 8 MiB, or over about 2.67 MiB as a standard-error
line or a line that comes as a string; dch then closes the connection and
dials again, unseen), `"busy"` (exit 1: a request was already running;
the running one goes on; one that reuses the running one's `N` gets it as
`"req":null`), `"refused"` (exit 2 as above, or the host refused it: 1, or
4 by its gate) or `"lost"` (exit 7: the connection dropped while it ran, or
the host sent nothing for 65 s; a partial line the host was writing is
dropped). A line that is no request gets the same `refused` pair with
`"req":null`; an end line with `"req":null` ends no request. A blank line is skipped, a line over 64 KiB refused. On a host
without `ctl-cancel` a cancel closes the connection and dch dials again,
unseen. After the host's idle close (5 minutes) or a lost connection, the
next request dials again. A dial that fails stands for 5 s from its end:
a request in that time ends at once, without a dial, with the same
standard-error line and end line (`"lost"`, or `"refused"` with 4, 6 or 1);
the first request after it dials again. End of standard input closes the connection and
exits 0; a request still running then ends `"why":"cancel"`. With no
connection at the start, dch writes one `{"schema":1,"exit":E,"stderr":...}`
line, prints the message on standard error and exits `E`: 6 for a host
without `xray`, 7 unreachable, 4 refused by the gate.

The corpus:
- **Claude Code.** The chapters come from the session's transcript
  (`<config>/projects/<dir>/<session id>.jsonl`). The file is found by one
  directory listing and must be the only regular file of that name, in a
  real directory: a symlinked `<dir>` is not followed. With two or more,
  the answer is `corpus: "none"` and `project` is the session cwd's key.
  - It is opened with `O_NOFOLLOW` and must still be the file the lookup
    saw. A transcript that is there but cannot be opened (permissions, an
    I/O error) exits 3.
  - It is read in 1 MiB chunks: the first 16 MiB and the newest 256 MiB.
    Past that, `complete` is `false` with `why: "range"`. So it is when the
    file shrinks during the read, or when more than 5000 chapters were
    found (the oldest go).
  - A line over 1 MiB or over 100000 JSON tokens counts as one failed step.
- **Codex.** A live Codex in the session (the first process named `codex`
  below the session's shell) answers from its rollout: `corpus: "codex"`,
  always `mentions_only: true` (Prosper's label: "Codex · mentions only").
  Its chapters are its user turns and compactions, its refs are mentions, and
  nothing is ever a definition. `session_id` is the Codex thread id.
  - The thread comes only from Codex's shell snapshots,
    `<CODEX_HOME>/shell_snapshots/<id>.<n>.sh`. Exactly one UUID-named
    snapshot must export the session's `DCH_SESSION` (and its bridge marker,
    when the Codex has one). It must be yours and not group- or
    world-writable. No snapshot, two thread ids, or a Codex outside the
    session's master that carries the session (the shared app-server daemon)
    answer `corpus: "none"` with `session_id: ""`, never the newest rollout.
  - `CODEX_HOME` is the Codex process's own, else its `HOME`'s `.codex`. It
    must be absolute with no `..` component, or nothing under it is read.
  - The rollout is found by names only: `sessions/YYYY/MM/DD`, newest first,
    at most 400 day directories and 400 year and month directories, a file
    whose name ends `-<id>.jsonl`. It must be the only one, a regular file
    that is yours and not group- or world-writable, under real directories (a
    symlinked day directory or file is not followed); otherwise the answer is
    `corpus: "none"` with the id. It is opened and read like a Claude Code
    transcript, with the same limits.
  - A user turn is a `response_item` user message, its text parts joined.
    The parts Codex adds itself (its `AGENTS.md` instructions, and any part
    wrapped in one tag, as `<environment_context>` or an `<image>`
    placeholder) are left out; a message of only those is no turn. A
    `compacted` record is a `compact` chapter, and its summary is prose.
  - Assistant text uses the prose rules. Tool calls (`function_call`
    arguments, `custom_tool_call` input, `local_shell_call` action) and their
    outputs use the code rules, with the tool clips. Developer messages,
    reasoning, every `event_msg` (a user turn's twin among them) and any other
    record type give nothing.
  - Rollouts are read as Codex 0.151 to 0.167 writes them (a `timestamp`,
    `type`, `payload` line). `project_label` is the Codex process's cwd, or
    the rollout's last `cwd` when that cannot be read; file refs resolve
    against the rollout's `cwd`.
  - Nothing in the Codex directory is run or written.
- **Shell.** The chapters come from the [ring](#shell-output-ring-x-ray)'s
  OSC 133 marks: one per typed command block (`A`, `B`, then `C` with client
  input before it and a non-blank command). A `B` or `C` inside an open
  `C`..`D` orphans that block: it gets no chapter. Without marks there are no
  chapters (`mentions_only: true`).
  - Commands typed ahead in one burst (one `--send` holding three lines, a
    pasted multi-line block) give only the first a chapter: the input all
    arrives inside the first prompt's `B`..`C`, and a later command's `C` with
    no client input since its own `B` is read as forged by output. Sent one at
    a time, each gets its own.
  - Rows, and the marks on them, enter the ring only when they scroll off
    the primary screen. So the commands still on the visible screen (in a
    fresh shell or after a `clear`, up to a screen's worth) have no chapter
    yet, and with none older the answer says `mentions_only: true`.
  - A query also reads the current primary screen, after the ring: its rows
    (soft wraps joined, blank ones dropped) count as output, so a ref still
    on screen is found by `--json`, `--ref` and `--follow`. They carry no
    marks yet, so they give no chapter and no definition, not even the
    chapter of the command whose output ends the ring. For the count they
    stay with that command until a row that starts with its prompt (then a
    space, or the row's end), which starts the next command, so a ref counts
    the same before and after its rows scroll. A prompt that changes between
    commands (a new directory in it) or is longer than 255 bytes starts no
    command there. A row a grow pulled back on screen is in the ring
    already and is not read again. A line soft-wrapped across the screen's
    top edge is read as two, its head from the ring, until it scrolls: a ref
    across that wrap is missed until then. A screen read that fails (no answer
    within 2 s, a busy or vanished master) or comes back cut (a screen over
    2 MB keeps its tail; its first row is dropped) makes the answer
    `complete: false` with `why: "range"`, and `--ref`'s `partial: true`.
    `--ref`'s light mode waits for the screen only as long as its budget
    lasts (`why: "budget"`). A screen row's `offset` is a provisional seq:
    the row gets another one when it scrolls into the ring, and a later
    ring row can take this one. Their `at` is the
    time of the read, not the time the row was written: two queries a
    second apart can give the same row different `at` values. While the alternate
    screen is active (vim, less), neither screen is read: the answer is the
    ring's alone, with no error and no `why`, and the screen's rows come back
    once it exits. A master started by an older dch answers from its ring
    alone until `dch --restart`.
  - `lines` counts the rows read: the ring's, plus the screen's when it was
    read.
  - `complete` is `false` with `why: "range"` when the ring has evicted rows
    or holds a gap. The master reports evicted rows itself (a status bit, reset
    by a clear), so the querying process's `xray_ring` does not matter. A dch
    1.43.0 master has no such bit: there only a gap, a lost range or a cut
    first row says so.
  - The ring is read 1 MiB at a time, and only up to its tail as it stood
    when the read began. A query stays under 16 MB whatever `xray_ring` is.
- **None.** An ended or History shell (`reason: "ended"`), dch-lite
  (`"lite"`), `xray_ring 0` (`"off"`), a Claude session whose transcript is
  gone, or a Codex with no thread id or rollout (`reason: null`). An ended
  Codex session has no corpus either: History records carry only a Claude
  agent, so it answers as an ended shell (`reason: "ended"`).

```json
{"schema":1,"session":"api","session_id":"0b1f6a52-4c1e-4d7a-9a51-2f6d8c3e7a10",
 "live":true,"corpus":"claude","reason":null,"available":true,
 "mentions_only":false,"lines":null,"complete":true,"why":null,
 "project":"-home-me-api","project_label":"/home/me/api","other_projects":0,
 "chapters":[{"anchor":"f3a41c:0","offset":0,"i":1,"at":1794304860,
   "title":"Fix the flaky build","kind":"prompt","partial":false,"error":true}],
 "chapters_evicted":false,"refs":[],"dropped_keys":0}
```

(Printed on one line.)
- **`other_projects`.** How many other sessions of this store have a
  transcript or a live Codex rollout in another project dir: what `--ref
  --scope all` would add.
  Counted from names only; nothing is opened.
- **`session_id`.** The agent UUID for Claude Code, the thread id for Codex
  (`""` while it has none), the ring's id (`m` + 16 hex) for a live shell,
  and the record name for an ended shell.
- **Chapters.** Oldest first; at most 5000 (the newest).
  - `anchor` is opaque: `f<inode>:<offset>` for Claude Code and Codex,
    `s<seq of the prompt record>` for a shell.
  - `kind` is `prompt`, `command` or `compact`.
  - `title` is at most 120 Unicode scalars.
  - `auto` (`true`) and `summary` are there only on a Claude Code prompt that
    the harness wrote, not the user: one made wholly of
    `<task-notification>` and `<system-reminder>` blocks. `summary` is the
    first notification's `<summary>`, else its `<event>`, made as a `title`
    is (whitespace collapsed, at most 120 scalars); it is left out when
    there is neither. The `title` stays as it was. Any other chapter has
    neither key. At most 256 KiB of summaries is held at a time: past that,
    the oldest auto chapters give theirs up (they keep `auto`).
- **`refs`.** The refs the corpus mentions (see [Refs](#refs)), at most 2000
  keys. The rest are counted in `dropped_keys`, exactly up to 32768 distinct
  keys and as a lower bound past that. `[]` for corpus `none`.
- **Size.** The object is capped at 4 MiB: past that, `refs` go first, from
  the end of the list, each counted in `dropped_keys`; then the oldest
  chapters go. `why` is `"output"`.

`dch --xray <n> --clear --json` empties a live shell session's ring through
the master's control socket. Nothing is typed into the session. It prints
`{"schema":1,"session","session_id","corpus","ring_cleared"}`.
- **Screen.** A clear blanks the mirror too, as ⌘K does: the master feeds
  ED 2 and ED 3 into its terminal mirror (never into the pty). `--read`,
  `--read --recent` and the next attach's replay then show an empty screen
  and no scrollback. A client attached at that moment keeps showing the old
  screen until the program repaints. Only a live shell ring is cleared this
  way; see the next point.
- **Resolution.** Here `<n>` is first matched as a live ring's exact
  `session_id`, then as a name. A `session_id` match asks each live master
  for its ring's header only, never its records (a dch 1.43.0 master sends one
  reply of records, which is dropped unread). The ring is cleared only if it
  still carries that id.
- **Agent sessions.** A Claude Code or Codex session has no ring:
  `ring_cleared: false`, with `corpus` `"claude"` or `"codex"` and its id (a
  Codex with no thread id: `"none"` and `""`). So it is with `xray_ring 0` and while an agent
  runs in a shell (the ring is paused). Then nothing is cleared: the
  screen and scrollback stay, for `--read`, attach replay and the say gate.
  Once that agent has exited the ring runs again: a clear right after is a
  real one, with no wait for the next record tick.
  A master asked to clear anyway (an older client) ignores it there.
- **Failures.** An ended session or a History record exits 1 with
  `dch: no such session`, as does a master that hangs up mid-request,
  before or after the clear. A dch-lite master exits 1 with
  `dch: no x-ray corpus`.

#### Refs

A ref is an issue (`#831`, `dch#120`), a pull request (`o/r#7`), a ticket
(`PROJ-12`, not `UTF-8` or `SHA-256`), a commit (7 to 40 lowercase hex with a
letter and a digit), a file path (`src/main.c:12`, keyed absolute against the
session's cwd; `~/` is kept as written with `..` at `~` dropped, `~user/` is no
path, and a relative path seen before any cwd is none) or a URL (a GitHub
issue or pull URL keys as `owner/repo#n`,
a commit URL as the commit; only one that starts exactly `https://github.com/`
does, so `https://GitHub.com/o/r/pull/7` stays a plain URL, as Prosper's pull
links validate it). Numbers have set lengths: a bare `#n` takes 2 to 6 digits
(`#5` and `#0` are none, `#00` is one), `repo#n` and `owner/repo#n` 1 to 7
(`dch#5` and `o/r#0` are refs), a ticket 1 to 6, a GitHub URL 1 to 9. A number
keys as written: `#00012` and `#12` are two refs. A bare `repo#n` needs a repo of at least 3
characters that starts with a letter, ends with a letter or digit and is not a
glue word or standard (`issue#12`, `step#3`, `PKCS#12`, `PR#831`, `a#12` are
none; `pre-#831` is the issue `#831`). A ref needs a word boundary on both sides,
Unicode-aware: `#831é` is none. Prompts and assistant text use every rule outside code spans and fences;
tool input and output, and a shell's ring, use the code rules only (URLs,
`owner/repo#n`, paths with a line number, commits), so a bare `#831`
mid-line in output is no ref. Texts are clipped before the scan: 6000 Unicode
scalars of prose and of a Bash command, 3000 of tool output, 240 per tool
field. A shell line (rows joined at soft wraps, at most 64 KiB) gives at most
128 mentions.

Each entry of `refs` carries `key` (`<shape>:<text>`), `shape`, `text`,
`full` (a commit's hex as written), `project`, `count`, `defined`,
`definitions`, `title` and `underline` (defined, or counted at least twice).
`count` is per Claude or Codex mention, per shell block with OSC 133 marks,
and per row without. A definition comes only from a paired tool result or a finished,
typed shell block that did not fail: a heading (`# #831 Agent panes`), a
`#831` line with `title:` within 3 lines, a JSON object or array with a number
or id and a title (the whole tool result, up to 256 KiB, not the 3000-scalar
clip), or a `--title` on the command with its ref on the command
or in the output. An orphan result, a forged or orphaned shell block, an error,
a fence, a diff, a listing command (`rg`, `grep`, `ls`, ...), a file printed
from outside the cwd, a heading with an empty title, and more than 3 headings
of one shape never define. The command counts in every simple command of it
(split at `;`, `&`, `&&`, `|`, `||`, parentheses and newlines; a `#` comment
and each here-doc's body are not a command, and a command with a body the
shell expands, `$(…)` under an unquoted delimiter, never defines), past env
assignments, redirects, `{`, `!`, `if`, `then`, `elif`,
`else`, `do`, `while`, `until`, and `sudo`, `doas`, `env`, `command`, `exec`,
`xargs`, `nice`, `nohup`, `stdbuf`, `time` and `timeout`, by name or by path
(`/usr/bin/env`). It counts inside `sh -c '…'` (`sh`, `bash`, `zsh`, `dash`,
`ksh`, `fish`; `-lc` too), `eval`, `watch` and `env -S` (`-S'…'` and
`--split-string` too), and inside `$(…)` and
backquotes, quoted or not. A search or a printed file found only inside
`$(…)` or backquotes still leaves the command's `--title` to define: its text
feeds the command, it is not the output. A command of more than 256 words
never defines. The file printers are `cat`, `head`, `tail`, `bat`, `batcat`,
`less`, `more`, `nl`, `awk`, `sed`, `jq`, `yq`, `tac`, `sort`, `cut`, `uniq`,
`rev`, `column`, `fold`, `fmt`, `glow`, `zcat` and `gzcat`; with no file
argument (`… | jq .`) they print their input and define, but under `xargs`
they print files named by their input and never define. A `<` source is a
printed file of a printer, of a compound command (`done < f`, `} < f`) and
of a redirect with no command (`$(< f)`). A printed file is
outside the cwd when its path, made absolute against the cwd or the directory
an earlier `cd` or `pushd` went to, leaves the cwd (`/etc/x`, `../x`, a `~/`
path read against `HOME`), when it holds `$` or a backquote, or after a `cd`
to a place it cannot follow (`cd`, `cd $X`, `cd ~/x`, `pushd +1`).

`dch --xray <n> --ref <ref> --json` streams one ref's
lines as NDJSON, each with `schema:1`: `first` (the key, `mode`, `scope` and
`budget_ms`), then `definition` and `mention` lines, a `tally` per file,
`progress` at most every 250 ms, and `done` last (`partial`, `why`, `counts`,
`files`, `bytes`, `lines_too_long`). `<ref>` is 1 to 256 bytes of UTF-8 with no
control character and none of the bidi controls U+200E, U+200F, U+202A to
U+202E and U+2066 to U+2069 (`dch: bad ref`), and must parse as exactly one
ref (`dch: not a ref`), relative paths against the session's cwd.
- **Scope.** `--scope session` reads `<n>`'s own transcript, or its ring and
  screen. `--scope project`, the default (also for a remote caller), reads
  that first, then the Claude Code transcript or Codex rollout of every other
  session of this store in `<n>`'s project dir, newest `lastSeen` first. `--scope all` then
  adds the other project dirs, the dir with the newest session first. The
  other sessions are the live ones (each master is asked for its command,
  never for its ring or screen) and the History records, one per agent id.
  Each transcript is found and opened as `<n>`'s is (a uuid, a valid
  `config_dir`, one regular file in the store's project dirs, no symlink);
  one that cannot be found is skipped and never opened. A live Codex's
  rollout is found as `<n>`'s is (its thread id, then its rollout by name);
  its project dir is the Codex's cwd's, its `lastSeen` the rollout's last
  write, and History records hold none. A shell `<n>`'s project dir is its
  cwd's. Another session's lines carry `chapter: null`.
- **Light.** The default stops at its first result, or after `--budget-ms`
  (50 to 300, default 300, across all files) with `why: "budget"`. With
  `--scope project` or `all`, `<n>`'s own file is read to its end and only
  another session's result stops the scan. A live shell's lines and another
  session's give titles and counts only: no `mention`, no `passage`.
- **Full.** `--full` reads the whole corpus up to its caps: 64 files
  (`why: "files"`), 1 GiB (`why: "bytes"`) and 10 s (`why: "time"`). A
  `--budget-ms` with it has no effect, but it is still checked: outside 50
  to 300 it exits 2 (`dch: bad budget`).
- **Counts.** `done.counts` has `session` (`<n>`'s mentions), `project` and
  `project_sessions` (the other sessions of its project dir: their mentions,
  and how many mention it), and `all` and `all_sessions` for the other dirs
  (`null` unless `--scope all`). `progress` carries the counts so far. A file gives its first `mention` line
and its newest 199; the `tally`'s `mentions_dropped` counts the mention lines
left out, and its `count` stays exact. `definition` lines come once per title,
at most 8 per file. The output is capped at 4 MiB with room kept for the
`tally` and `done`; past the cap the scan stops and `done` says
`partial: true, why: "output"`.
A session with no corpus exits 1
with `dch: no x-ray corpus`. Nothing is written to disk: the index lives in the
calling process for that one query. A reader that closes the pipe cancels the
scan: dch stops at its next write, at most 250 ms later (the `progress`
cadence), prints no `done` and exits 0; `--json` and `--clear` exit 0
silently too. Any other write failure prints `dch: X-ray failed on this
session` and exits 3. SIGTERM ends
it at once, with no `done` either.

With `--full`, a commit or file ref also gets one `resolved` line, right
after `<n>`'s own file (its `tally`) and before any other session is read:
`{"schema":1,"resolved":{"kind","state","title","subtitle","lines"}}`,
plus `first` for a file shown (below),
`kind` `"commit"` or `"file"`, `lines` at most 14, each at most 200
characters; the light scan never resolves. Every string is the text with
invalid UTF-8 and NUL as U+FFFD, the controls JSON-escaped. The resolver
reads only the ref and `<n>`'s cwd, so with `--scope session` the line is
just before `done`, and a wider read that a cap or the time limit stops
(`done` with `partial: true`), a cancel, a read failure or a remote host's
cut (no `done`) still carries it once `<n>`'s own file was read. A live
Codex `<n>` gets it too.

- **Commit.** The SHA is checked again (7 to 40 lowercase hex). An ended
  session runs nothing: `state: "ended"`. Otherwise dch runs git directly,
  never through a shell: on macOS the first of the Command Line Tools git,
  the git under `/var/db/xcode_select_link`, Xcode.app's,
  `/opt/homebrew/bin/git` and `/usr/local/bin/git` (never `/usr/bin/git`,
  the shim that can prompt to install them); elsewhere `/usr/bin/git`, then
  `/usr/local/bin/git`; none is `no_git`. The argv is
  `git --no-pager -c core.fsmonitor= -c core.hooksPath=/dev/null -c protocol.allow=never -c diff.external= -c color.ui=never -c log.showSignature=false -C <cwd>`,
  then `rev-parse --is-inside-work-tree` (not `true`: `not_repo`), then,
  for a commit URL with a short SHA only,
  `config --get-regexp ^remote\..*\.url$`, then
  `show --no-color --no-ext-diff --no-textconv --stat=80 --stat-count=14 --format=%H%x00%an%x00%at%x00%s --end-of-options <sha>^{commit}`.
  The environment is only `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, `GIT_NO_LAZY_FETCH=1`,
  `PATH=/usr/bin:/bin`, `HOME` and `LANG=en_US.UTF-8`; stdin is
  `/dev/null`, and no other fd of dch's reaches git.
  All runs share one 3 s deadline (`timeout`: git's process group gets
  SIGTERM, then SIGKILL a second later; a git that even SIGKILL cannot end,
  such as one stuck on a stalled network mount, is left behind a second
  after that); dch keeps 64 KiB of
  its output and 4 KiB of its errors and drains the rest. `ok` gives the
  subject as `title`, `author · 3h ago · abc1234` as `subtitle` and the
  stat as `lines` (past 14: the first 13 and the summary); a short SHA
  matching several objects is `ambiguous`; one git does not know
  (`unknown revision`) or that names a tree, a blob or a tag of one
  (`expected commit type`) is `not_commit`. git's own "not a git repository"
  (or a cwd that is gone) is `not_repo`. Any other git failure gives no
  `resolved` line, never a wrong state: for example a `.gitmodules` with
  an invalid `update` value, or a blob missing from a partial clone.
  A commit URL (`https://github.com/<owner>/<repo>/commit/<sha>`) with a
  full 40-hex SHA resolves as a bare SHA does. With a shorter SHA, it
  resolves only when one of the repo's remote URLs names that owner and
  repo on github.com (any case; `https://`, `ssh://` or `git@github.com:`,
  with or without `.git`). Otherwise it is `not_commit` and `show` never
  runs. A remote reached only through `url.<base>.insteadOf` does not
  match, and hosts other than github.com never match. If the `config` run
  fails or times out, there is no `resolved` line. The remote URLs are only
  compared, never printed or traced (they can hold a token). This only
  stops a short link to another repo from showing an unrelated local
  commit; the repo's own config decides its remotes. A repo's
  `objects/info/alternates` can make git read
  objects from outside the project: the stat may then name another repo's
  files. No code runs and nothing is written, and the lines go to the same
  user who can read that repo.
- **File.** The ref's path (a relative one against the session's cwd, a
  `~/` one against `HOME`), in an ended session too, must lie inside the
  project: the cwd's git root (the nearest parent holding `.git`), else the
  cwd. A root that is `/`, `$HOME` or a directory above `$HOME` (by real
  path; `HOME` and the passwd entry's home both count) is no project: every
  file is then `outside_project`. So is `~/.config` or `~/Library`, and a
  root inside either that is not a git root below it (a shell in
  `~/.config/rclone`): apps keep tokens there under any name. A git root
  below them (`~/.config/nvim` as its own repo) is a project. Outside the
  project is `outside_project` and nothing is touched. A path on the deny
  list is `secret_name` and is not opened. The list is matched without
  regard to case against every part of the whole path, as given and again
  as its real path:
  - a part that starts with `.env`, `.ssh`, `.gnupg`, `.aws`, `.netrc`,
    `.npmrc`, `.pypirc` or `.git-credentials`;
  - a directory `.git`, `.kube`, `.docker`, `.azure`, `.oci`, `.claude`,
    `.codex`, `.gcloud`, `.password-store`, `.m2`, `.bash_sessions`,
    `.config/gh`, `.config/gcloud`, `.config/hub` or `.config/op`, and
    everything under it;
  - the name `.claude.json`, `.pgpass`, `.my.cnf`, `.vault-token`,
    `.htpasswd`, `.s3cfg`, `credentials`, `credentials.json`,
    `credentials.toml`, `credentials.db`,
    `application_default_credentials.json`, `secrets.yaml`, `secrets.yml`,
    `secrets.json`, `credentials.tfrc.json`, `.yarnrc.yml`,
    `gradle.properties`, `rclone.conf`, `.boto`, `.dockercfg` or
    `.Xauthority`;
  - a name that ends in `.pem`, `.key`, `.p12`, `.pfx`, `.p8`, `.ppk`,
    `.jks`, `.keystore`, `.keychain`, `.keychain-db`, `.keyring`,
    `.tfstate`, `.tfstate.backup` or `history` (shell and REPL history);
  - an `id_` name with no dot (`id_rsa`, never `id_rsa.pub`).

  One exception for the directories: when the project is a git root whose
  own path, as given, lies in one of them (a Claude Code worktree in
  `<repo>/.claude/worktrees/<name>`), they are matched only on the part of
  the path below that root, as given and as real path. The worktree reads
  its own `a.txt` and still refuses its `.env`, its own `.claude/` and the
  like. A root reached through a symlink that hides the directory, or a
  shell in one with no git root, gets no exception. Whatever the root, a
  path whose real path (or path as given) enters one of these directories
  of the home (`~/.claude`, `~/.codex`, `~/.config/gh`, ...) is
  `secret_name`, so a git repo inside `~/.claude` shows nothing.

  This guards the common credential files by name. It is not a guarantee:
  a secret in a file with any other name (a `config.yml`, a script, a log)
  is shown. The real path must also lie inside the project's real path, so
  a symlinked directory on the way is followed only while it stays inside;
  else it is `outside_project`. A file with more than one hard link is
  `outside_project` too: its other names can lie outside. Over 2 MB is
  `too_large`. `ok` reads it, the file itself never a symlink, and
  gives the name as `title`, the path from the cwd (`· line N`
  with one) as `subtitle`. A file with a NUL byte in its first 8000 bytes
  (git's own test) is binary: `ok` with `· binary` in the subtitle and no
  lines. Otherwise it gives up to 14 lines with line N the seventh, CRs
  dropped, kept whole at either end of the file; `first`, only here, is the
  1-based number of `lines[0]` (1 for a ref with no line), so a consumer
  can tell which row is line N at the end of the file. A path
  that is gone, a symlink or not a regular file gives no `resolved` line.

`dch --xray <n> --follow --json` streams NDJSON, each line with
`schema:1`. The first line is `{"schema":1,"snapshot":{…}}`, holding the
`--json` object. Then come deltas as the corpus grows, at most one batch
every 250 ms:
- `{"schema":1,"chapter":{…}}`: a new chapter. A known `anchor` replaces
  that chapter, for example when its `error` turns on.
- `{"schema":1,"ref":{…}}`: the whole ref object for a key whose count,
  definition, title or `underline` changed. For a shell, `count` can go down
  and `underline` can turn off. `count:0` removes the key.
- `{"schema":1,"evict":{"seq":S}}` (shell only): the ring's oldest records
  went, and its head is now `S`. It is the first line of its batch. Drop the
  chapters whose `offset` is below `S`, then apply the batch's refs.
- `{"schema":1,"reset":{"why":…}}`: drop what you hold for the session. A new
  snapshot follows at once.
  - `"rotated"` means a Claude Code `/clear`, another transcript or none, a
    Codex whose thread id changed (a new thread, or none) or whose rollout
    appeared or was replaced, an agent that exited, or a shell whose ring
    paused for an agent, resumed after one, or got a new `session_id`.
  - `"shrunk"` means the transcript or rollout was cut.
  - `"cleared"` means `--clear`, ED 3 or RIS emptied a shell's ring. It never
    comes with `evict`.
  - `"evicted"` means one eviction changed more than 256 keys (sent at most
    once a second), or a shell chapter went while its prompt record is still
    in the ring, which no line could remove.
  - `"full"`: a transcript past the per-file range first sends the capped
    snapshot. Its whole-file snapshot follows, read in the background.

Applying the deltas to the snapshot gives the `--json` answer of that moment.
Top-level fields (`lines`, `complete`, `why`, `mentions_only`,
`chapters_evicted`, `dropped_keys`) change only with a snapshot. `refs` match
`--json` as a set: `--json` orders refs of the same count by first
appearance, and a `ref` line carries no position. Past 5000 chapters, a
Claude Code stream sends no line for the oldest chapter `--json` drops, so a
client can hold more chapters than `--json` shows. When the 4 MiB cap left
a ref out of the snapshot, a change to that ref or its removal sends no line
until the next snapshot. A new key still sends one.

Nothing runs on a timer:
- a Claude Code transcript is watched with kqueue (inotify on Linux);
- its project directory is watched for a new file, and its
  `sessions/<pid>.json` for a rewrite (a `/clear` can make the new file
  before it names it); the master is then asked about them. With no
  transcript yet, the watch is on the deepest of the project directory,
  `projects` and the config directory that exists, and moves down as they
  are made;
- a Codex rollout is watched the same way, with its `shell_snapshots`
  directory for a new thread and, until the rollout exists, the deepest of
  today's `sessions/YYYY/MM/DD` there (Codex names it by local time);
- a shell is followed through the master's wake, sent when its ring or its
  screen changes (an alternate-screen switch included).

An agent starting or ending in a followed session sends `reset` `rotated`
within 2 s, with the ring on or with `xray_ring = 0`. While a follower holds
one of the master's 4 subscriber slots, the master looks for the agent once a
second; with no follower it does not. A Claude Code follower past the 4 slots
is not told when its agent exits.

Rules for shells and endings:
- A master takes 4 shell subscribers. A fifth gets `dch: busy` and exit 1.
  A follower that leaves the master's wake unanswered for 3 s (a paused
  reader) can lose its slot to a fifth. When it reads again and no slot is
  free, it writes `dch: busy` to stderr and exits 1, with no further line on
  stdout. Exit 1 with `busy` can come after the snapshot: then it means the
  stream stopped, not that the session ended. Run `--follow` again for a
  new snapshot.
  A remote follower whose phone or host is gone without closing the
  connection lets its slot go in about 30 s: `dch serve` sets TCP keepalive
  on every connection.
- An ended session or a History record writes its one snapshot and exits 0.
- A live one exits 0 when the session ends, when its connection drops (a
  `--restart`), or when the reader closes the pipe, idle or not.

`dch --xray <n> --chapter <anchor> --json` prints one chapter's entries as
one object: `{"schema":1,"session","session_id","anchor","title","at","kind","entries":[…],"truncated":bool}`,
with `auto` and `summary` after `kind` when the chapter list has them.
`<anchor>` is a chapter's `anchor` from `--json`: 1 to 128 bytes of
`A-Z`, `a-z`, `0-9`, `:`, `_`, `.` and `-`, else exit 2 with `dch: bad
anchor`. dch reads the session's chapters again and answers only when one of
them has exactly that anchor; it never seeks to the offset in it. Otherwise it
exits 1 with `dch: chapter gone` (the transcript was rewritten, or the ring
evicted or cleared the block): read `--json` again. `--chapter` does not go
with `--ref`, `--follow` or `--clear`.

Each entry is `{"role","tool","text","error"}`:
- Claude Code: `user` is the prompt (a slash command as `/name args`, a
  compaction's summary too), `assistant` its text. `tool` is a call, with the
  tool's name: a Bash command, the path of an Edit, MultiEdit, Write or Read,
  else the input's first 8 fields as `key: value` lines, each value cut at 240
  characters. `output` is its result, in the call's chapter, with the call's
  name (`null` for a result with no call); `error` is the result's error flag,
  never set for a Grep or Glob that found nothing. Thinking, images, notices
  and subagent turns are left out.
- A live Codex: `user` is the turn's prompt (a compaction's summary too),
  `assistant` its text. `tool` is a call with its name: a `function_call`'s
  arguments as `key: value` lines as above (the arguments string itself
  when it is not a JSON object), a `custom_tool_call`'s input, a
  `local_shell_call`'s action. `output` is a call's output where it lands,
  named by its `call_id`'s call in the chapter (else `null`); `error` is
  always `false`, as a rollout has no error flag: it means "not known",
  never "succeeded". Reasoning and events are left out.
- A shell: one `tool` entry with the typed command and one `output` entry
  with the block's output rows, soft-wrapped rows joined; `error` is a
  non-zero exit. Only rows that scrolled into the ring count: rows still on
  the screen are not in it. A block with no marks has no chapter.

`text` is cut at 6000 Unicode scalars (3000 for an `output`) and escaped like
`--ls-json`. The whole object is capped at 1 MiB: entries past the cap are
left out and `truncated` is `true`. An ended or History Claude Code session is
read from its transcript, a live Codex from its rollout. An ended shell, a
ring that is off or paused, a session with no transcript, and a Codex with
no thread id or no rollout exit 1 with `dch: no x-ray corpus`. Nothing is
written to disk. A reader that closes the pipe gets exit 0 and no message;
SIGTERM ends it at once.

No new session or rename takes a name of exactly `m` + 16 hex in any case
(`M0123456789ABCDEF` too: on a case-insensitive file system its socket also
answers to the lowercase id), because that is a ring `session_id`. `-n`,
`--spawn`, `--revive --as`, `-m`, the pickers' rename, and `dch serve`'s
create and rename all refuse it with exit 2 and `dch: name looks like a
session_id`, also with whitespace around it (the listing trims it). A session
that already has such a name keeps it, and `-n` and `--xray` reach it by its
exact listed name only: `-n` refuses a case variant of it as a new name, and
`--xray` reads a case variant of it as an id. Other verbs (`-a`, `--read`,
`--status`, `--send`, `-k`) reach it by any case on a case-insensitive file
system, like any name.

| Exit | Meaning |
| --- | --- |
| 0 | the object was printed (the view: quit); a `--follow` ended with its session, its connection, or its reader |
| 1 | `dch: no such session` (also a master that hangs up after the connect, before its answer is complete, in every form: nothing more is written; `--ref` keeps the lines already out and writes no `done`; `--follow` once its snapshot is out exits 0 instead), `dch: ambiguous session`, `dch: no x-ray corpus` (`--clear`, `--ref`, `--chapter`), `dch: chapter gone` (`--chapter`: no chapter has that anchor now), `dch: busy` (a fifth `--follow` on one shell; also after the snapshot, when a stalled follower's slot went to a fifth: the stream stopped, the session did not end) |
| 2 | bad usage: `--json` missing (the view: any other flag, or a stdout that is not a terminal), a flag twice, `--follow` with `--ref` or `--clear`, `--chapter` with `--ref`, `--follow` or `--clear`, a flag of a later release, `--xray` not first, a bad `<n>`, a bad `--ref`, `--chapter` anchor, `--scope` or `--budget-ms` |
| 4, 6, 7 | a remote `host:name` only: refused by the host's gate; the host lacks `xray`; the host is unreachable or the connection was lost |
| 3 | `dch: X-ray failed on this session`: the session answered garbage, the transcript could not be read (it is there, but opening or reading it failed), memory ran out, or a write failed with an error other than a closed pipe |

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
| `DCH_REPLAY_SCROLLBACK` | attaching client | `0` | Lines of the session's scrollback the attach replay prints above the screen, max `10000`; `--scrollback` wins ([Scrollback above the screen](#attaching-replays-the-screen)). Sent to a remote host too. |
| `DCH_FOCUS_RESIZE` | attaching client | config `focus_resize`, else on | `0`, `false`, `no` or `off` (as in the config): the client does not enable focus reporting and does not take the size on focus-in. Any other value turns it on ([The focused client sets the size](#the-focused-client-sets-the-size)). |
| `DCH_NO_DETECT` | client and master | unset | Anything but empty or `0`: no screen-content [state detection](#agent-state-detection), `agents` reads `0`, the master does no [auto-allow](#auto-allow), and `--say`, `--stop` and the agent restart see no screen, as on dch-lite. |
| `DCH_NO_VT` | master | unset | Anything but empty or `0`: no terminal mirror ([dch-lite and `DCH_NO_VT`](#dch-lite-and-dch_no_vt)). |
| `DCH_NO_REPLAY` | master | unset | Anything but empty or `0`: attach clears and asks for a repaint instead of [replaying the screen](#attaching-replays-the-screen). |
| `DCH_SCROLLBACK` | master | `2000` | Mirror scrollback lines, max `100000`, `0` none (`--read --recent`, [Verbs](#verbs)). |
| `DCH_FOLLOW_MS` | `--follow` | `150` | Least time between two frames, `20`–`10000` ms ([Following a screen](#following-a-screen---follow)). |
| `DCH_NO_HISTORY` | master | unset | Anything but empty or `0`: no history record ([Bringing sessions back](#bringing-sessions-back)). |
| `DCH_NO_REMOTE` | `dch -l`, `-a`, `--remote` | unset | Anything but empty or `0`: no [remote hosts](#remote-hosts) in the picker, `-a` or `--remote` (`--hosts` still applies). |
| `DCH_REMOTE_BOX_MS` | `--remote`, a picker opening empty | `300` | The time box for probing every remote host in the foreground, max `60000`, for a slow link. |
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
   and all of that holds for a settle window. A background shell keeps the
   record at `shell`, not `idle`. Claude Code can report `shell` for a shell
   the agent cannot see or stop, so once the record has read `shell` for
   `DCH_AGENT_SHELL_MS` after the pause, the agent counts as idle.
4. **Restart.** dch arms a hold on the session, types `/exit`, and reads the
   session id from the last `claude --resume <id>` line Claude prints on exit.
   If `/exit` brings up Claude Code's "Background work is running" prompt,
   dch picks `1. Exit and stop tasks`. The result line then names what that
   stopped: `(at /exit claude stopped the background work it still ran: …)`.
   The resume message then tells the agent what was stopped, so it can
   re-arm what it still needs.
   If `/exit` does not end Claude, dch takes it back out of the composer, so
   that a later Enter cannot end the session without the hold.
   It then starts the original command again, with `--resume <id>` inserted
   right after the `claude` token. Any `--resume`, `-r`, `--continue`, `-c`,
   `--session-id` and `--fork-session` from the original command are dropped,
   and so is Claude's one-shot prompt (see
   [Bringing sessions back](#bringing-sessions-back)).
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
| `DCH_AGENT_SHELL_MS` | `60000` | How long the record may read `shell` after the pause before the agent counts as idle. `/exit` then stops that shell. |
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
  tests"`) is dropped on resume (see [Bringing sessions
  back](#bringing-sessions-back)). After an option dch does not know, followed
  by a word, the whole command is kept and the prompt is sent again.
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

Claude's startup prompt is sent once. `dch --spawn api -- claude --name api --
"fix the login bug"` starts Claude with that first message, and dch keeps it
out of everything after: the record stores the command without it, and
`--revive`, the picker's resume, `--restart`'s agent step and a revive with
no Claude id all start `claude --name api` (with `--resume <id>`), every other
option kept, one written after the prompt too (`claude "fix it" --model opus`
comes back as `claude --model opus`). `--history`, `--history --json` and `--ls-json` never show it;
a record an older dch wrote with one is read without it. The prompt is
everything after a bare `--`, and every argument that is neither an option
nor an option's value, the options read the way Claude Code 2.1.318's
`--help` lists them. It is cut only where `claude` is the program run: the
command itself, after an `env` prefix (`env FOO=1 claude ...`), or behind a
wrapper (`headroom wrap claude ...`) once dch has seen Claude run in the
session. Known limit: in such a session the first word named `claude` counts
as the program even when it is a launcher's path argument (`make -C
~/src/claude dev`, where `make` starts Claude), so the words after it are cut
as the prompt (`dev` here) and `--resume <id>` goes in right after it; start
Claude directly, or through a launcher with no word named `claude`. Any other command that merely holds a `claude` word (`git -C
src/claude log`, `docker run --name claude img`, `ssh host claude ...`, a
`sh -c` argument) is kept whole, and so are `npx @anthropic-ai/claude-code`,
a renamed claude and `claude.exe`, which hold no word named `claude`. Put it
after `--`: an
option dch does not know, followed by a word, keeps the whole command, prompt
included, rather than guess which word is its value; an unknown `--no-<name>`
is the exception, a flag that never takes one. Claude Code itself runs
a one-word prompt that names one of its commands (`update`, `doctor`, `mcp`,
…) as that command, `--` or not, so give such a prompt more words.

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

A session name cannot start with `-`: every verb would read it as a flag.
`--spawn -h`, `-n -h`, `--revive <n> --as -h` and a revive of such a record
are refused (`dch: a session name cannot start with '-'`, exit 2; `--as`
exits 1), and a directory named `-x` auto-names its session `_x`. An older
dch could make one; `--` reaches it: `--pin`, `--unpin`, `--forget` and
`--stop` read every word after `--` as a name or record file
(`dch --forget -- -h`), and `dch -k -- -x` kills it. In the picker such a
row attaches and kills (live), pins and removes (History, `p` and `d`); any
other key says so.

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
died without writing them; `--history` shows that as `vanished`). `end` is `signal N from <pid> <command>` when
another process signalled the master itself, so a stray `pkill` names itself:
the sender's argv with NULs as spaces and anything but printable ASCII as `?`,
cut at 120 bytes, or just `signal N from <pid>` when it could not be read. `agent` is
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

**Scrollback above the screen.** `--scrollback N` on an attach (`dch -a
<name> --scrollback N`, and any other attach: `-n`, the picker), or
`DCH_REPLAY_SCROLLBACK=N` in the attaching client's environment, makes the
replay first print the last N lines of the mirror's scrollback, then the
screen as above. The lines go to your terminal as ordinary output and are
scrolled off the top of its screen, so they land in its own scrollback: an
embedded terminal (a SwiftTerm view, say) gets recent history to scroll back
through. A line is a row of the mirror at your window's width, so a wrapped
line counts once per row. `--scrollback` wins over the variable; `0`, the
default, is the replay exactly as before. N is capped at 10000 and at what
the mirror keeps (`DCH_SCROLLBACK`, default 2000), and the whole replay at
2 MiB: past that the oldest lines are dropped. The alternate screen (vim,
less, a full-screen TUI) has no scrollback, so it gets none. It is ignored,
without a message, by a lite build, a `DCH_NO_VT` or `DCH_NO_REPLAY` master,
and a master from a dch before this option (`--restart` the session to pick
it up); the attach itself works the same. Remote: see [Remote hosts](#remote-hosts).

### The focused client sets the size

One session can have several clients at once, for example a terminal tab and
an app pane. The pty has only one size, and it belongs to the client the user
is focused on. Each attaching client turns on focus reporting (DEC 1004) in
its own terminal. When that terminal reports focus-in (`ESC [ I`), the client
sends its window size to the master again, even when the size has not
changed. Clicking back into a window gives the session that window's size.

Focus reports reach the program in the session only while that program has
focus reporting on itself, as Claude Code does. The client tracks
`ESC[?1004h`/`l` in the session's output. On attach the master re-sends the
program's modes, so the client knows the state from the first byte.
Otherwise the client drops the reports, so no stray `[I`/`[O` is typed into a
shell. All other input passes through unchanged. A read that ends in a bare
`Esc` waits up to 50 ms for the rest of a report, so a report split by a slow
link or a loaded host is still recognised. A lone Esc key is delayed by up to
that much. When another key follows the Esc inside those 50 ms, the Esc goes to
the program at once and the key follows after the same gap it had behind the
Esc, so the program reads Esc and the key apart, not as Alt-key. A
terminal's own Alt-key (Esc and the key in one write) still arrives as one.

`DCH_FOCUS_RESIZE=0` (or `false`, `no`, `off`) in the client's environment,
or `focus_resize = false`
in the [config file](#config-file), turns this off. The client then does not
enable focus reporting and does not intercept any input.

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
Prosper's "remote frames" protocol 1.1 (1.0 plus the [control
verbs](#control-verbs-ctl)) on TCP port 8771, the port the phone dials, and
serves dch's own [remote client](#remote-hosts) the same way. It runs in the foreground and logs to stderr with timestamps.
SIGINT or SIGTERM stops it and leaves every session running.

```sh
dch serve                    # list, attach, kill, rename, watch, control verbs
dch serve --allow-create     # also let the phone (and dch --spawn) start sessions
dch serve --check-peer 100.101.102.103   # would this peer get in?
```

**Trust model.** Only your own devices on your tailnet get in. Every check
runs right after the connection is accepted, before a byte is read. A
refused tailnet peer is told why in one frame and closed (see
[Refusals](#refusals)), and so is one past the connection limits (`busy`);
an address outside 100.64.0.0/10 is closed without a byte.

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
| `--allow-create` | Let the phone start a session with its own command (0x02). Off by default, and `create` is then missing from the ops the phone sees, and attach (0x01) only reaches a session already in the list (else 0x13 and a close). With it, attach to a new name starts a shell session, as `dch -n` does. It also gates the control verbs that start sessions, `--spawn` and `--revive`. |
| `--allow-user <u>` | On a tagged host, accept this user's nodes (numeric id or login). Repeatable. |
| `--whois-optional` | Accept a tailnet peer while tailscaled is not answering (whois and status both fail), with a warning (see above). The owner must still be known at startup. |
| `--check-peer <ip>` | Print whether that peer would get in, and the refusal code in parentheses, then exit 0 (accept) or 1. |

#### Refusals

A tailnet peer the checks above refuse, or one past the connection limits
(see Limits below), gets exactly one frame, then the connection's FIN. It is sent first thing, before the server reads a byte
and before any `0x18`, whether or not the client already sent its hello:

```
0x13  {"code":"<code>","message":"<text>"}
```

`code` is a contract: these values are stable and keep their meaning, and a
client should key on it. `message` is for people and may be reworded. A
code not in these tables may be added later; treat one you do not know as a
refusal you cannot explain. A refusal never names the host's owner, a user,
a node or the `--allow-user` list. A peer refused again within 30 s (the
repeat-refusal cache) gets the same code. Other `0x13` frames (a failed
attach, say) have no `code`, but for `taken`, below.

| `code` | When | `message` |
| --- | --- | --- |
| `not_owner` | An untagged host, and the peer is another user's node. | `this host serves only its owner's devices` |
| `not_allowed` | A tagged host, and the peer's user is not on its `--allow-user`. | `this host is tagged and serves only the users it names: start dch serve there with --allow-user <your login or id>` |
| `tagged_peer` | The peer is a tagged node. | `this host does not serve tagged nodes` |
| `whois_unavailable` | tailscaled is not answering (whois and `status` both fail), and no `--whois-optional`. | `this host could not verify you: tailscaled is not answering` |
| `whois_failed` | whois failed while `status` works (a node tailscaled does not know), or its answer was unusable. | `this host could not verify you (tailscale whois failed)` |
| `self` | The peer is this host itself: one of its addresses, or its own node. | `this host does not serve connections from itself` |

Every code above means "refused": the same peer will be refused again, so
do not retry. One code is transient and means "try again later":

| `code` | When | `message` |
| --- | --- | --- |
| `busy` | The host is at its connection limit: 64 connections in total, or 16 from this peer. Nothing is wrong with the peer; retry with your own backoff (there is no retry-after field). | `this host is at its connection limit: try again shortly` |

`busy` is never remembered by the repeat-refusal cache: the next connection
once a slot frees is served as usual. Its message says nothing about other
users or connections.

One code is not a refusal at all but the end of an attach that was let in
(#102): ended by another client, do not auto-retry (a retry would take the
session back from that client):

| `code` | When | `message` |
| --- | --- | --- |
| `taken` | A newer attach to the same session retired this one. It is the attach's last frame: no `0x12`, then the FIN. | `another client attached to this session` |

The `0x13` goes out in one non-blocking write that is never retried, so a
peer that does not read cannot hold the server up; the server then waits up
to 1 s for the peer to close (reading off and dropping what it sent, unparsed)
so the close is a FIN and not a reset that could lose the frame. A peer
outside 100.64.0.0/10 (or loopback without the test bypass) is closed with
no frame: it is not a tailnet peer, so it is told nothing. A peer past the
connection limits gets `busy` (it used to get a bare close). Up to 16
refused or busy peers wait for their close at once; past that, one is closed
at once, and a reset may then lose its frame.

**What the phone gets.** The session list (`dch --ls-json`, re-encoded: name,
alias, state, agent count, `cwd` when known, and only GitHub pull-request
links from `--meta`),
a live list for its home screen (polled every 2 s), attach with resize and
redraw, kill, rename, and a screen snapshot from the terminal mirror (empty in
a dch-lite build). An attached connection runs a real `dch -E -f -n <name>`
client in a pseudo-terminal, so the newest attach to a session retires the
older connection, which gets a `0x13` `taken` (see [Refusals](#refusals))
before the close.

**Attach request keys.** `0x01` (and `0x02`) carry a JSON object: `name`,
`cols` and `rows` as in remote frames 1.0, plus one optional key this server
reads (#101): `"scrollback": N`, an integer, the lines of the session's
scrollback to print above the screen ([Scrollback above the
screen](#attaching-replays-the-screen)). It reaches the client dch serve
spawns as `--scrollback N`, clamped to 0..10000; missing, `0`, negative or
not an integer means none. It is additive: a server that does not know it
ignores it (an unknown key in a JSON object), so it needed no new protocol
version. Commands run as argument vectors, never through a shell.

**Redraw.** On a `0x07`, dch serve resizes the program one row down and back
120 ms later, so it repaints. A dch serve 1.58.0+ also re-attaches its
client, and the attach stream then carries the session's DEC modes (mouse,
bracketed paste, focus, the alternate screen), for the kitty keyboard flags
a pop and then the push (so the flag stack does not grow), and on the full
build the screen replayed from the mirror.

#### Control verbs (ctl)

Remote frames 1.1 adds one op, `ctl`: a client runs one of dch's own verbs
on the host and gets its output and exit status, exactly as if it had run
there. `0x18` says `"proto":"1.1"` and always lists `ctl`, `ctl-cancel` and
`xray` in `ops` (`create` only with `--allow-create`). A client checks for
the `ctl-cancel` token, not the proto, before it sends a `0x34` or a second
`0x30`, and for the `xray` token before it sends an `--xray`. One request at
a time:

```
→ 0x30  {"argv":["--read","api"],"cwd":"/abs/dir"}    cwd optional
← 0x31  stdout bytes, as they come       (any number)
← 0x32  stderr bytes, as they come       (any number)
→ 0x34  cancel (any payload)             optional, while it runs
← 0x33  {"exit":N}                       last for this request
→ 0x30  the next request, same connection
```

Each `0x30` that runs gets exactly one `0x33`. After it the connection stays
open (warm) for the next `0x30`; a warm connection with no frame for 300 s
closes. A `0x34` while the verb runs ends it (SIGTERM, then SIGKILL of its
process group 2 s later; 660 s later for `--say`, `--stop`, `--revive` and
`--run`, which stop at a safe point on a SIGTERM) and its `0x33` is
`{"exit":2,"why":"cancel"}`. `--stop` types the resume message into an agent
still waiting to pause. `--say`, `--run` and `--revive` type nothing more,
press no Enter once the signal is handled, take text already typed back out
of the composer one Ctrl-U at a time and exit 1, usually within seconds (up
to the 30 s land wait, plus as much again, when a paste is still painting or
will not come out), with reason `interrupted`, `withdrawn` or `draft_left`
(see the reason codes). The `0x33` keeps `"why":"cancel"`; the dch's own exit
goes to the server's log (`ctl done: cancel, dch exit 1`). Until the dch is
reaped the server keeps reading its pipes and drops what it reads, so a verb
on its way out never blocks on a full pipe or dies of a SIGPIPE. A `0x34` with nothing running
(before any `0x30`, after a `0x33`, or once the verb was already cut) is
ignored. A `0x30` while one runs gets `0x13`
`{"code":"busy","message":"a ctl is already running on this connection"}`;
nothing closes and the running one goes on to its `0x33`. A client that
sends a `0x34` and then the FIN still gets the `0x33`.

`argv` is dch's command line without the `dch`: the verb first. The server
runs its own dch binary with it, with fork and exec (no shell), in `cwd` or
else `$HOME`, stdin `/dev/null`, and streams its output while it runs. `exit`
is dch's exit status, or 128 + the signal that ended it. A request it
refuses gets one `0x13` `{"message":"<why>"}` (no `code`), then the FIN
(on a warm connection too), and nothing runs. A `0x30` on a connection
that is attached or watching is refused (`ctl needs its own connection`),
and a ctl connection, running or warm, stays one: anything but a hello
(`0x08`), a list (`0x03`), a `0x30` or a `0x34` gets `0x13` `{"message":"a
ctl connection takes only ctl frames"}`, then the FIN.

Only these forms run. `<n>` is a session name: it must pass the same check
as an attach's, never start with `-`, and never contain `:` (`a session name
with ':' is local only`). The host's dch would read such a name as
`host:name` and dial on with the host's own identity, so it is refused, even
for a local session named that way.

| Verb | Form | Timeout |
| --- | --- | --- |
| `--read` | `--read <n> [--ansi] [--recent [N]] [--cursor]` | 60 s |
| `--send` | `--send <n> [--] <text...>` | 60 s |
| `--run` | `--run <n> [--] <text...>` (into Claude through the gate, up to about 60 s itself) | 660 s |
| `--keys` | `--keys <n> <key...>` | 60 s |
| `--follow` | `--follow <n>` | none |
| `--status` | `--status <n>` | 60 s |
| `-k`, `-d` | `-k <n>`, `-d <n>` | 60 s |
| `-m` | `-m <n> <alias>` | 60 s |
| `--spawn` | `--spawn <n> [--size CxR] [--] <cmd...>`, with `cwd`; needs `--allow-create` | 60 s |
| `--history` | `--history --json` | 60 s |
| `--revive` | `--revive <n> [--as <new>] [--no-message] --json`; needs `--allow-create` | 660 s |
| `--pin`, `--unpin`, `--forget` | `--pin <n>... --json`; `--forget --all --json` | 60 s |
| `--stop` | `--stop <n>... --json` | 660 s |
| `--say` | `--say <n> --json -- <text>` | 660 s |
| `--xray` | `--xray <n> --json [--clear \| --chapter <anchor>]` | 60 s |
| `--xray` | `--xray <n> --json --follow` (no cap either) | none |
| `--xray` | `--xray <n> --ref <ref> [--scope <s>] [--full] [--budget-ms <ms>] --json` | 60 s |

Refused, before anything runs: an `argv` of fewer than 2 elements; a verb
or flag not in the table; a bad name; `--config`, `serve`, `-h` or `-f`
anywhere outside the free text (`--send`'s words, `--keys`' keys,
`--spawn`'s command, `--say`'s text, `-m`'s alias); `--all` except in
`--forget --all --json`; a `cwd` that is no directory (`no such directory`);
`--spawn` or `--revive` without `--allow-create` (`creating sessions is off
on this host (dch serve --allow-create)`).

`--xray` takes its name right after it, then its flags in any order, each
once (`--json twice`). A flag's value is the next element even when it
starts with `-`. Refused: no `--json` (`--xray needs --json (the view is
client-side only)`); two of `--ref`, `--follow`, `--chapter` and `--clear`;
`--scope`, `--full` or `--budget-ms` without `--ref`; a flag not in the
table (`--requests is not allowed with --xray`); a missing value (`--ref
needs a value`); and a value that fails `dch --xray`'s rules (`bad ref`,
`not a ref`, `bad anchor`, `bad scope`, `bad budget`). `--clear` needs no
`--allow-create`, only the gate, like `-k`. Everything else, `--scope
project` for one, is the host's dch's to answer.

**What `xray` lets a peer read.** More than the chapters of the session it
names. `--ref <file ref> --full` returns 14-line windows of files on this
host: any file inside that session's project (its cwd's git root, else its
cwd; none when that is `/`, `$HOME` or above it) that is a regular file of
at most 2 MB with one link, not itself a symlink and not on the deny list
(`.env*`, `.ssh`, `.git/`, `.claude/`, `.config/gh/`, `*.pem`, `id_rsa`,
`*history`, ...: the File resolver's full list; nothing under `~/.config` or
`~/Library` but a git root below them). The list catches the
common credential files by name, not every file that holds a secret. A commit
ref returns that commit's `git show --stat` from the session's repo.
`--scope all` returns lines (and with `--full`, definition passages) from
every other session's Claude Code transcript of this store, in every project
dir; `--scope project`, the default, from those in the session's own project
dir. Nothing is written to disk; `--clear` empties a shell session's X-ray
ring. Let in only peers you would let read those files and
transcripts.

**Its limits.** Stdout and stderr together get 8 MiB (`--xray --follow`:
no cap). At that cap the server
stops forwarding, sends the dch SIGTERM (SIGKILL 2 s later) and ends with
`{"exit":2,"why":"cap"}`. Past its timeout (the table) it does the same with
`"why":"timeout"`. A device (its tailnet address) runs at most 2
`--xray --follow` of one session: a third ends the oldest the same way, with
`"why":"timeout"`, and is run once more 1 s later if it met `busy` first. A client that closes the connection (or half-closes it)
while the verb runs ends it as a `0x34` does (the same 2 s or 660 s grace),
with no `0x33` (the log line reads `ctl done after the close: dch exit 1`);
the handler then holds its connection slot, one of the peer's 16, until the
dch has exited or been killed (a server stop cuts the grace to 2 s). The slot
stays counted on purpose: it is that peer's dch still running, and the verbs
above stop within seconds, so a peer that opens and drops connections cannot
pile up running verbs past its cap. The server's
loop never waits on the dch: one slow verb holds up nothing else. The ctl op has the same gate
as attach: only peers the [trust model](#dch-serve-prosper-remote-hosts)
lets in. For tests, `DCH_CTL_TIMEOUT_MS` replaces every timeout (`--follow`
included), `DCH_CTL_CAP` lowers the cap and `DCH_CTL_IDLE_MS` the warm
connection's 300 s; `dch serve` reads them at startup and removes them from
the environment. A remote `dch --xray` (one-shot or `--requests`) reads
`DCH_CTL_TIMEOUT_MS` too, in place of the host's 60 s, for its own wait on a
silent host (that plus 5 s).

**The dch client.** `dch --read host:api` and the other verbs open one
connection per verb. Ctrl-C (SIGINT), SIGTERM, or a reader that closes its
stdout (EPIPE) cancel the verb on the host: with `ctl-cancel` in `ops`, dch
sends a `0x34` and waits up to 3 s for the `0x33`, then exits (128 + the
signal, re-raised so the shell sees it; 0 for a closed stdout). A host
without `ctl-cancel` gets the close instead, which ends the verb there too.
A second Ctrl-C exits at once.

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
escapes control characters (C0, DEL, C1, bidi overrides, isolates and
marks, U+2028 and U+2029) as
`\xNN` or `\u{NNNN}`, and a session name containing one is refused.

**Limits.** At most 64 connections, 16 per peer, and 4 live-list streams per
phone. A connection past the first two gets `busy` (see
[Refusals](#refusals)) and the close. A connection that is not attached or watching closes after 30 s
without a whole frame, and any connection closes when its output goes untaken
for 20 s. A peer gone without a close (no answer to TCP keepalive) is
closed in about 30 s. Control frames over 64 KiB, and any frame over the protocol's
256 MiB guard, close the connection. Sent frames are at most 8 MiB. A
control verb has its own cap and timeouts ([above](#control-verbs-ctl)).

The golden fixtures in `tests/remote-frames/` are Prosper's conformance set,
plus dch's own from 17 on for the control verbs; `tests/serve_test.py` runs
them.

## Remote hosts

dch is also the client: it lists and attaches to the sessions of your other
machines, the ones serving them over Prosper's remote frames on port 8771
(`dch serve` above, or Prosper's own server on a Mac), and runs dch's
control verbs there.

```sh
dch --ls --remote                 # local sessions, then host:name for remote ones
dch -a mybox:api                  # attach to session api on mybox
dch --host mybox -a api           # the same, for a name with a ':' in it
dch -l                            # the picker lists remote rows too
dch --ls-json --remote            # remote rows, each with a "host" field
dch --history --remote            # ended sessions here, then host:name (dch serve 1.41+)
dch --hosts mini,pi=100.64.1.2:8771 --ls --remote   # these hosts, not your tailnet
dch --read mybox:api              # a control verb on mybox (dch serve 1.41+)
dch --host mybox --send api -- make test
dch --cwd /srv/app --spawn mybox:build -- make   # needs dch serve --allow-create
dch --xray mybox:api --json       # X-ray a session on mybox (dch serve 1.55.0+)
```

**Which hosts.** `--hosts` (a comma-separated `[label=]host[:port]` list), else
the `remote_hosts` config key in the same form, else your tailnet: the peers
in `tailscale status --json` that are online, owned by the same Tailscale user
as this host (by numeric user id), and untagged, each at its 100.64.0.0/10
address and named by the first label of its MagicDNS name, and only while
tailscaled reports `BackendState` `Running` (a stopped tailscaled still lists
its peers, and off the tailnet 100.64.0.0/10 is any ISP's carrier-grade NAT).
A tagged peer is only reached when you list it. dch never dials anything
else. With no `tailscale` CLI and no peers, there are simply no remote rows.
`remote_hosts = off` or `DCH_NO_REMOTE=1` turns remote hosts off (`--hosts`
still works). `--host <h>` then reaches only a host named in that command's
`--hosts`; any other is `dch: <h> is not a known host (remote hosts are off:
...)`, exit 1.

**Listing.** `-ls`, `-lj` and `--ls-json` stay local, with no network and no
`tailscale` call, unless you add `--remote`: scripts and keep-awake checks run
`dch -ls` often. With `--remote` dch probes every host at once and takes at
most 300 ms in all, the `tailscale status` call and any name lookups
included, so a host that is down or hanging, or a slow resolver, never holds
it up. A host that does not answer in time shows the rows it last answered,
marked `[stale]`, for two minutes (a cache in dch's state dir). The picker
always shows remote rows, in Sessions with their host faint, only for hosts
that are candidates now, and re-probes in the background every 2 s; a stale
row is faint and says Stale. A host speaking a newer protocol major is
skipped, with a note on stderr. A host whose `dch serve` refuses us (a
`0x13` with a `code`, see [Refusals](#refusals)) is not down: `--ls` and
`--ls-json` say `dch: <host> refused: <message>` on stderr and show none of
its cached rows, and the picker drops them. A `busy` host is not a refusal:
it is treated like a host that is down, its cached rows shown `[stale]`.

**Remote history.** `dch --history --remote` and `dch --history --json
--remote` print the local records first, exactly as without `--remote`, then
each host's ended sessions, fetched as its own `dch --history --json` over
`dch serve`'s `ctl` op (the `0x30` argv is exactly `["--history","--json"]`).
- *Budget.* The same as `--ls --remote`: every host at once within 300 ms
  in all (`DCH_REMOTE_BOX_MS`), discovery included. A host that does not
  answer in time, or whose connection fails, shows the records it last sent,
  `[stale]` (`"stale":true`), for two minutes, from
  `.remote-history` beside the row cache. The picker reads only that cache;
  its background probe refreshes it once when the picker opens (the
  History tab's count), then every 10 s, not every 2 s, and only while the
  History tab shows, since each fetch runs a `dch` on the host and logs two
  lines there. It shows a host's records stale once they are 30 s old, so
  after a while on another tab they show stale until the fetch that
  switching to History starts lands.
- *Shape.* Text: `host:name (alias)  when  end  cwd  $ argv  [claude <name>
  <id>]  [pinned]  [record <file>]  [stale]`, every field escaped. JSON:
  each record as the host sent it, its C1, DEL and bidi controls written as
  `\u` escapes, plus `"host"` (its label) and `"stale"` last; a record that
  already holds a `"host"` or `"stale"` key, or has no `"name"`, is dropped,
  so a host cannot pass for another. So is one a strict JSON reader would
  reject (a lone surrogate `\u` escape, a number past a double's range,
  objects and arrays nested over 64 deep in it, keys not counted), and one
  that gives any key twice in one object (decoded, so `"a"` and `"\u0061"`
  are the same key), so text, the picker and JSON show the same records
  and a reader that refuses duplicate keys takes the document. At most 500 records and 2 MiB per host.
  Without `--remote` nothing is dialled and the output is unchanged.
- *An old or odd host.* One whose hello lacks `ctl` (before dch serve 1.41,
  or an older Prosper) gets one stderr line, `dch: <host>: does not support remote
  history (needs dch serve 1.41+)`, and no records; a refusal is `dch:
  <host> refused: <message>`, its cached records dropped; a reply that is
  not a JSON array or is too big, or a remote dch that failed, counts as a
  host that is down (its cached records, stale, if any). None of these
  change the exit status (0) or the local listing, and a hanging host costs
  at most the box.
- *The picker.* History sorts them among the local rows (above). `Enter`
  runs `dch --revive <file>.json --host <host>` there, which needs `dch
  serve --allow-create` on the host; without it the status line says `dch:
  <host>: does not support --revive (needs dch serve --allow-create)`.

**Attaching.** `dch -a host:name`, or Enter on a remote row in the picker,
attaches the way a local attach does: raw terminal, the detach key and its
double tap (to the picker) handled locally, the window size following yours.
Detaching closes the connection and nothing else; the remote session runs on.
`-a` only attaches, never creates: it checks the name is in the host's list
and sends the attach on that same connection. `--scrollback N` (or
`DCH_REPLAY_SCROLLBACK`) goes to the host as the `0x01`'s `"scrollback"` key
(see [dch serve](#dch-serve-prosper-remote-hosts)); a host that does not know
it attaches without scrollback. A host that refuses us
prints `dch: <host> refused: <message>` and exits 4; a `busy` one prints
`dch: <host>: says: <message>` and exits 1, like a host that is down. An exact local session name wins over `host:name`, and with remote hosts off
(`DCH_NO_REMOTE=1`, `remote_hosts = off`) the name stays local; `--host` is the
unambiguous form. A lost connection restores the terminal, says so, and exits
1. An attach another client took over (a newer attach to the same session,
the `0x13` `taken`) restores the terminal, prints
`dch: <host>: another client attached to this session` and exits 5.

`dch -a host:name` exit codes (#102), the same for `dch.exe`:

| Code | Means | Retry |
| --- | --- | --- |
| 0 | Detached, or the remote session ended. | - |
| 1 | The connection was lost, the host is down or `busy`, or another error (unknown host, no such session, no terminal). | A lost link or `busy`: yes. |
| 4 | Refused: the host's gate sent a coded `0x13` (see [Refusals](#refusals)). | No: it will refuse again. |
| 5 | Taken: another client attached to the session. | No: it would take the session back. |

**Control verbs.** The verbs in the [ctl table](#control-verbs-ctl) take
`host:name` too: such a name is remote only when no local session or
History record has exactly that name and `host` is a known host (`--hosts`,
`remote_hosts`, or a cached remote row from `dch -ls --remote` or the
picker); never with remote hosts off (`DCH_NO_REMOTE=1`, `remote_hosts =
off`). Anything else stays local, so a local session named `proj:task`
(`dch -n proj:task`) can be spawned, revived, pinned and forgotten by name.
When such a name fails here while remote hosts are on, one more stderr line
names the host: `(<host> is not a known host; --host <host> to reach it)`;
the exit status is the local one. The line is printed when the verb returns
its failure; one that exits dch on the spot prints none. With remote hosts
off no such line comes, since `--host` could not reach the host either.
`--host <h>` is the unambiguous form, the one for a host only discovery
knows, and the only one for `--history`, which takes no name; a discovered
host passes the same whois check. Names on two hosts in one command are refused. dch runs the verb on
the host through its `dch serve` and writes its stdout and stderr as they
come, byte for byte: the output and the exit status are the remote dch's.
Anything off the table is refused here, before any connection (`dch: can't
run on a remote host: <why>`, exit 1). `--cwd <dir>` is `--spawn`'s start
directory: on the host for a remote spawn, here (it must exist) for a local
one; it goes with no other verb. `dch --xray host:name ... --json` goes the
same way (see [X-ray queries](#x-ray-queries---xray)): `--xray` must come first, so
it has no `--host` form, and dch checks its values by its own rules before
any connection (exit 2).

| Code | Means |
| --- | --- |
| 0-3 | The remote dch's own exit status. 2 with `dch: <host>: <verb> stopped by the host (cap\|timeout)` on stderr: the host cut it short. |
| 1 | Also: the host refused the request (`dch: <host>: <why>`), an unknown host, or a request dch would not send. |
| 4 | Refused: the host's gate sent a coded `0x13` (`dch: <host> refused: <message>`). |
| 6 | The host lacks the op: `dch: <host>: does not support <verb> (needs dch serve 1.41+)`, or `(needs dch serve --allow-create)` for `--spawn` and `--revive`, or `(needs dch serve 1.55.0+)` for `--xray` when the hello lacks `xray`; nothing is sent. |
| 7 | Unreachable: `dch: <host>: unreachable: <why>` (down, `busy`, or the connection lost before the exit status). |

`-a`'s codes are unchanged.

**Trust.** Before an attach to a host that discovery found, dch asks
`tailscale whois` about its address and goes on only when it is your own
Tailscale user's untagged machine; if whois fails, it does not connect. A
host you name in `--hosts` or `remote_hosts` is taken as given: no whois,
since it may be on a LAN or behind a name tailscale does not know. Every byte
from a peer is untrusted: frames and JSON fields are bounded, and every name
or message a peer sent is printed with its control characters escaped
(`\xNN`), in `--ls`, `-lj` and the picker; `--ls-json` writes C1 and bidi
controls, U+2028 and U+2029 as `\u` escapes, as all of dch's JSON does. Only the screen bytes of an attach go to the
terminal as they are, as a local session's do.

**Not yet.** No live list stream (the picker polls), no remote rename (`-rl`)
and no remote kill or create from the picker: the control verbs above are
the command-line form, and the picker's row actions stay local, but for
resuming a remote History row.

### Windows client

`dch.exe` is the client half alone, for Windows 10 or later: it lists and
attaches to the sessions of your other machines and hosts none (no pty, no
master, no `dch serve`). It is a plain Win32 console program, built with
mingw-w64 and linked statically, so it needs no Cygwin or MSYS2 runtime.
Each [release](https://github.com/ventsislav-georgiev/dch/releases) carries
`dch.exe` and `dch.exe.sha256`; check the download with
`certutil -hashfile dch.exe SHA256` or PowerShell `Get-FileHash dch.exe`.
Or build it:

```sh
scripts/build-windows.sh            # x86_64-w64-mingw32-gcc; CC=gcc in MSYS2's UCRT64
make windows-check                  # the same under -Werror, when that compiler is installed
```

```bat
dch                                 :: the picker: the remote rows, Enter attaches
dch -ls                             :: remote rows: on Windows -ls implies --remote
dch -lj
dch --ls-json
dch --hosts mini,pi=100.64.1.2:8771 -ls
dch -a mybox:api                    :: host:name is required, there are no local sessions
dch --host mybox -a api
dch --read mybox:api                :: a control verb, as on macOS and Linux
dch --host mybox --cwd /srv --spawn build -- make
```

The hosts, discovery and trust are the same as on macOS and Linux, including
`tailscale whois` before an attach to a discovered host, failing closed.
`tailscale.exe` is found on `PATH`, else in `C:\Program Files\Tailscale\`.
The config is `%APPDATA%\dch\config` (or `DCH_CONFIG`); the stale-rows cache
is in `%LOCALAPPDATA%\dch\`, which your profile's ACL already keeps private,
so there is no owner check like the POSIX one. `DCH_NO_REMOTE` and
`DCH_REMOTE_BOX_MS` work as documented; the box defaults to 1000 ms here,
not 300, because starting `tailscale.exe` costs more on Windows.

An attach puts the console in VT mode (Windows Terminal, or conhost on
Windows 10 or later), passes keys through raw, follows the window size, and
restores the console on every exit, Ctrl-C and Ctrl-Break included. Ctrl-\
detaches, and twice quickly switches to the picker, as on macOS and Linux
(`DCH_DOUBLE_TAP_MS`, `detach_key`). A lost
connection restores the console, says why, and exits 1; a refused or taken
one exits 4 or 5, as on macOS and Linux. The [control
verbs](#control-verbs-ctl) work as there, every name `host:name` (or
`--host`), with the same exit codes; `--host`, `--hosts` and `--cwd` go
before the verb.

`dch` alone is the picker, `dch -l`'s, with the remote rows only: arrows or
`j`/`k` move, Enter attaches, `q` or Esc quits; it redraws on a resize and
re-probes the hosts in the background. No History or Sidekicks tab, no row
actions, no settings view.

Not yet on Windows: the kitty-keyboard form of the detach key.

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
