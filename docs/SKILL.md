---
name: dch-sessions
description: Drive and observe TUI programs headlessly through dch sessions — spawn, type, read the rendered screen, wait for output. Use when a task needs an interactive/long-running terminal program (k9s, htop, REPLs, watch loops, builds) without holding a terminal open.
---

# Driving TUI sessions with dch

`dch` keeps programs alive in detachable sessions and mirrors each session's
terminal in memory. You control sessions with one-shot CLI verbs — no pty, no
expect scripts, no attaching.

## Verbs

```sh
dch --spawn <name> [--size CxR] [cmd...]        # start headless (default $SHELL)
dch --send  <name> <text...>                    # type text, no enter
dch --run   <name> <text...>                    # type text + enter
dch --keys  <name> <key...>                     # ctrl+c, alt+x, shift+tab, up, f5, enter, esc...
dch --read  <name> [--ansi] [--recent [N]]      # print rendered screen (stdout)
dch --read  <name> --cursor                     # + "cursor row col visible wrap" (stderr)
dch --wait  <name> --match <str> [--timeout ms] # block until screen shows <str>
dch --ls-json                                   # sessions as JSON (incl. version)
dch --restart <name>|--all [-f]                 # re-exec the master onto this
                                                # dch binary; session keeps running
dch -k <name>                                   # kill session when done
```

Exit codes: `0` ok/match, `1` error, `2` wait timeout, `3` session has no
terminal mirror (lite build — `--read`/`--wait` unavailable, everything else
works).

## The loop

Interact like a careful human: act, wait for the UI to settle, read, decide.

```sh
dch --spawn infra --size 120x40 k9s
dch --wait infra --match "Pods" --timeout 15000 || exit 1  # k9s is up

dch --keys infra : && dch --run infra deployments
dch --wait infra --match "Deployments" --timeout 5000

dch --read infra            # inspect the screen, pick a row
dch --keys infra down down enter

dch --read infra --recent 200   # includes scrollback, good after fast output
dch -k infra
```

## Rules of thumb

- **Always `--wait` between action and read** when the app redraws async;
  matching a marker beats sleeping.
- `--read` shows the *rendered* screen (what a human would see), not the raw
  byte stream; spinners collapse to one line.
- `--keys` encodes for the app's actual keyboard mode (kitty protocol,
  application cursor keys) — prefer it over `--send` for control characters.
- Long output: `--read --recent N` pulls the last N lines including
  scrollback (default mirror keeps 2000 lines).
- Repainting the screen elsewhere: `--read --cursor` reports the caret as
  `cursor <row> <col> <visible> <wrap>` on stderr (1-based, from the same
  snapshot as stdout) instead of leaving you to guess it. Not valid with
  `--recent`.
- **Plain `--read` cannot tell a TUI's ghost text from typed input.**
  Placeholders and argument hints sit in the input box and read back as if
  someone typed them. Only the styling and the caret separate the two, and
  plain output keeps neither — decide with `--read --ansi --cursor`. See
  `docs/ghost-text.md`.
- Sessions persist until killed — reuse them across steps, `dch -k` when done.
