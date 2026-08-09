# Telling ghost text from real typed input

A watcher that polls `dch --read` to answer *"has the human left an unsent
message in the prompt box?"* has to deal with **ghost text**: characters the
TUI painted itself — a placeholder, an argument hint, an autocomplete
suggestion — that sit in the input box but were never typed.

**Short answer: the signal is in the buffer, and `dch --read --ansi` already
carries it, byte for byte.** Plain `dch --read` throws it away. Anything
reading the plain dump *cannot* tell the difference and never will — that is a
property of the plain format, not of the mirror.

Two independent signals survive into the mirror:

1. **Styling.** Ghost text is drawn with an explicit SGR run. Real typed text
   in the box is drawn with the terminal default foreground and emits no SGR
   at all.
2. **Caret position.** Ghost text is painted *at or after* the caret. Text the
   human typed lies *before* it. `--cursor` reports the caret from the same
   snapshot as the screen, so the two line up.

Use both. Either alone has an edge case; together they are solid.

## What the mirror actually preserves

`dch --read --ansi` round-trips the faint attribute and the exact foreground
color:

```console
$ dch --spawn probe --size 40x6 sh -c \
    'printf "\033[2J\033[1;1Hreal\033[2m ghost\033[0m\033[3;1H\033[38;2;153;153;153mhint\033[0m"; sleep 30'
$ dch --read probe --ansi | cat -v
real^[[0m^[[2m ghost^[[0m
                                       <- blank row
^[[0m^[[38;2;153;153;153mhint^[[0m
```

Note `real` carries no SGR of its own. That is the shape every check below
relies on. `tests/run.sh` pins it as a regression test.

## Claude Code specifically

Measured against Claude Code 2.1.223 running under `dch`, at a 120x40 mirror.

### Argument hint — the case that actually bites

Type a slash command that takes arguments and stop at the space. The hint is
painted inside the box, on the same row as what you typed:

```console
$ dch --send cc "/loop "
$ dch --read cc --ansi | cat -v | grep '❯'
❯ ^[[0m^[[38;2;177;185;249m/loop  ^[[0m^[[38;2;153;153;153m[interval] [prompt]^[[0m

$ dch --read cc --cursor 2>&1 >/dev/null
cursor 37 9 1 0
```

Reading that row as plain text yields `❯ /loop  [interval] [prompt]` — which
is exactly the false positive to avoid. In ANSI it splits cleanly:

| span | SGR | what it is |
| --- | --- | --- |
| `/loop ` | `38;2;177;185;249` | typed by the human, highlighted as a command |
| `[interval] [prompt]` | `38;2;153;153;153` | ghost |

and the caret at column 9 sits at the boundary: everything from column 9
rightward is paint, not input.

Note the typed part is **not** default-colored here — Claude Code highlights
recognised slash commands. So "styled means ghost" is wrong on its own; the
caret is what separates these two colored spans.

### Empty-box placeholder

Claude Code renders its `Try "..."` placeholder through `chalk.dim`, i.e.
SGR 2 — the same attribute the probe above proves survives. It is gated on
"no messages yet, nothing submitted, prompt suggestions enabled", so it does
not appear in every session; it did not render in the sessions measured here.
The caret rule covers it regardless: an empty box puts the caret at the start
of the text area, so the whole placeholder is at-or-after the caret.

### Completion menus are not this problem

`/rel`, `@path` and friends open a *menu* on rows above the box. Those rows are
outside the input row entirely, so a watcher scoped to the box row never sees
them.

## Recipe

Scope to the input row, split it into styled spans, drop everything at or
after the caret, then drop spans that are faint or drawn in the theme's
secondary gray. What is left is real unsent input.

```python
import re, subprocess

SGR = re.compile(r"\x1b\[([0-9;]*)m")
GHOST_FG = {(153, 153, 153)}      # your theme's secondary text color


def read_sgr(params, faint, fg):
    """Apply one SGR sequence's parameters. Returns the new (faint, fg)."""
    it = iter(params.split(";") if params else [""])
    for p in it:
        n = int(p) if p.isdigit() else 0          # empty param == 0 == reset
        if n == 0:
            faint, fg = False, None
        elif n == 2:
            faint = True
        elif n == 22:
            faint = False
        elif n == 39:
            fg = None
        elif n in (38, 48):
            # extended color: 5;<idx> or 2;<r>;<g>;<b>. Consume its arguments
            # so an inner "2" is never mistaken for the faint attribute.
            mode = next(it, "")
            args = [next(it, "0") for _ in range(3 if mode == "2" else 1)]
            if n == 38:
                fg = tuple(int(a) for a in args) if mode == "2" else args[0]
    return faint, fg


def unsent_input(session, marker="❯"):
    """Return the human's unsent text in the prompt box, or ''."""
    p = subprocess.run(["dch", "--read", session, "--ansi", "--cursor"],
                       capture_output=True)
    if p.returncode != 0:
        return ""
    cur = next((l for l in p.stderr.decode().splitlines()
                if l.startswith("cursor ")), None)
    if cur is None:
        return ""                      # no caret reported: refuse to guess
    caret_row, caret_col = (int(x) for x in cur.split()[1:3])

    rows = p.stdout.decode("utf-8", "replace").split("\n")
    if not 1 <= caret_row <= len(rows):
        return ""
    row = rows[caret_row - 1].rstrip("\r")

    spans, pos, params = [], 0, ""
    for m in SGR.finditer(row):
        spans.append((params, row[pos:m.start()]))
        params, pos = m.group(1), m.end()
    spans.append((params, row[pos:]))

    kept, col, faint, fg = [], 1, False, None
    for params, chunk in spans:
        faint, fg = read_sgr(params, faint, fg)
        ghost = faint or fg in GHOST_FG
        for ch in chunk:
            if col < caret_col and not ghost:
                kept.append(ch)         # everything else is paint, not input
            col += 1
    text = "".join(kept)
    return text.split(marker, 1)[-1].strip() if marker in text else ""
```

Caveats worth knowing before you trust it:

- **`GHOST_FG` is theme-dependent.** `153,153,153` is Claude Code's default
  dark-theme secondary text. A different theme paints a different triple. The
  caret rule is theme-independent; the color rule is a second opinion.
- **The caret rule alone mislabels a mid-line edit.** Move the cursor left
  inside typed text and everything right of the caret is real input that the
  caret rule discards. That is why the two rules are ANDed rather than either
  one used alone: with both, a mid-line edit only loses characters that are
  *also* styled as ghost.
- **`--cursor` is refused with `--recent`.** The caret is a visible-screen
  property; a tail-cut dump would point at the wrong row, so the master
  withholds it and this recipe returns `""` rather than guessing.
- **Column counting assumes single-width cells.** Wide CJK glyphs and emoji in
  the box will drift the column against the caret.

## Why dch does not decide this for you

Terminals carry no "this text is a suggestion" bit. The only carriers are the
styling and the caret, and dch already hands over both, unmodified. Turning
them into a verdict needs to know which row is the input box, what the prompt
marker is, and which color the app calls "secondary" — all facts about the app
being watched, not about the terminal. Encoding Claude Code's box layout into
`dch --read` would make it wrong for the next TUI.
