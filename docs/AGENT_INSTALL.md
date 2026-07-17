# Agent install instructions for `dch`

You are an agent. Follow this file end-to-end to install `dch` on the user's
machine. Run each step in order. If a step fails, stop and report the failure
verbatim — do not "fix" it silently.

`dch` is a detachable-terminal session manager (one C binary, no runtime
deps, no config file). Repo: `https://github.com/<your-fork>/dtach`.

---

## 0. Sanity check the environment

```sh
uname -s            # Darwin or Linux
command -v cc       # any C compiler
command -v make
command -v autoconf
```

If a C compiler or `autoconf` is missing:

- **macOS:** `xcode-select --install` then `brew install autoconf`.
- **Debian/Ubuntu:** `sudo apt-get update && sudo apt-get install -y build-essential autoconf`.
- **Fedora/RHEL:** `sudo dnf install -y gcc make autoconf`.
- **Arch:** `sudo pacman -S --needed base-devel`.

## 1. Clone

```sh
mkdir -p "$HOME/src"
cd "$HOME/src"
git clone https://github.com/<your-fork>/dtach.git
cd dtach
```

(If the user already has a clone, `cd` into it and `git pull`.)

## 2. Build

```sh
./configure
make
```

Expected: a binary called `dch` in the repo root.

```sh
file ./dch          # should show "Mach-O" (macOS) or "ELF" (Linux) executable
./dch -h            # should print the help screen
```

## 3. Install to `$PATH`

Prefer `~/.local/bin`. Create it if missing and ensure it's on `$PATH`.

```sh
mkdir -p "$HOME/.local/bin"
install -m 0755 ./dch "$HOME/.local/bin/dch"
```

Check whether `~/.local/bin` is on `$PATH`:

```sh
case ":$PATH:" in
  *":$HOME/.local/bin:"*) echo "PATH OK" ;;
  *) echo "ADD TO PATH" ;;
esac
```

If "ADD TO PATH", append the right line to the user's shell rc file:

- `zsh` → `~/.zshrc`
- `bash` → `~/.bashrc` (Linux) or `~/.bash_profile` (macOS)
- `fish` → `~/.config/fish/config.fish` (use `fish_add_path` instead)

Append (for zsh/bash):

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Tell the user to open a new shell or `source` the file.

## 4. Verify

```sh
which dch                          # should resolve to ~/.local/bin/dch
dch -h | head -5                   # should print the dch help screen
dch -ls                            # should list zero sessions
```

## 5. (Optional) Wire up VSCode

If `/Applications/Visual\ Studio\ Code.app` exists and the user wants every
new terminal to open a `dch` session, patch
`~/Library/Application Support/Code/User/settings.json` (macOS path; adjust
for Linux/Windows) to include:

```jsonc
{
  "terminal.integrated.defaultProfile.osx": "dch",
  "terminal.integrated.profiles.osx": {
    "dch": { "path": "/Users/<you>/.local/bin/dch" }
  }
}
```

Replace `<you>` with the result of `echo $USER`.

Use `jq` or a JSON-aware edit — do **not** clobber existing settings.

## 6. Hand off

Tell the user:

- Detach key inside a session: `Ctrl-\`.
- Force-detach from outside: `dch -d` (or `kill -USR1 <pid>`).
- List/kill: `dch -ls`, `dch -k`, `dch -kl`.
- Full help: `dch -h`.

Then read the **Agent API** section of the README (`--spawn`, `--send`,
`--run`, `--keys`, `--read`, `--wait`, `--ls-json`) — those verbs let you
drive TUI sessions headlessly, which is the main reason you were asked to
install dch. `docs/SKILL.md` is a ready-made skill file describing the loop.

You're done. Do not also configure shells, prompts, or aliases unless the
user asks.
