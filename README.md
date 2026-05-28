# dch — detachable terminal sessions, the simple way

A tiny terminal multiplexer built on top of [dtach][dtach]. No windows, no
panes, no tabs, no config file. One session per project, auto-named, attached
on demand. Close the laptop, drop the SSH, quit the terminal — your shell
keeps running.

![dch --help](docs/dch-help.png)

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
- **No config file, no shell hooks.** One binary on `$PATH`.

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
dch -k                         # → pick one and kill it
dch -kl                        # → kill ALL sessions
```

The auto-name uses cwd basename when you're not in a git repo, and
`<repo-toplevel>-<branch>` when you are — slashes in branch names get
replaced with `_` so they're safe.

## Quick install

```sh
git clone https://github.com/ventsislav-georgiev/dch.git
cd dch
./configure
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

## Cheatsheet

```sh
dch              # attach the auto-named session, or create it
dch <cmd...>     # same, but if new, run <cmd> in it
dch -ls          # list sessions
dch -l           # pick a session (arrow keys) and attach
dch -k [name]    # kill a session (interactive picker if no name)
dch -kl          # kill ALL sessions
dch -d [name]    # detach all clients of a session (sends SIGUSR1)
dch -n <name>    # override the auto-name
dch -f           # force-attach even if another client is connected
```

Inside a session: `Ctrl-\` to detach. Works in vim, fzf, less, Claude Code,
etc. — terminals that swallow most control keys still pass `Ctrl-\` through.

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

## License

GPLv2 (inherited from dtach). See `COPYING`.
