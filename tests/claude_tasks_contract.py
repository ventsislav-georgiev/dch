#!/usr/bin/env python3
"""The Claude Code /tasks keys `dch --view-agent` relies on (#713, Prosper
#2050/#2053), checked in an installed Claude Code bundle read as data.

dch sends Esc to the /tasks list only on the Claude Code version it was
verified on (VA_VERIFIED in dch.c). Bump that constant only after this
script prints ok for every check on the new version's bundle. Not run in CI
(no bundle there): with no bundle found it prints SKIP and exits 0.

Checks:
  confirm-no     the Confirmation context binds escape to confirm:no
                 (the list's Esc only cancels the dialog)
  escape-kill    the global Esc that stops a viewed agent is gated on
                 <te>==="viewing-agent", <te> read from viewSelectionMode
  tasks-command  the "tasks" command is immediate:!0 (opens mid-turn)
  f-switch       f in the list switches to the agent (a function that sets
                 viewSelectionMode:"viewing-agent", then "Viewing agent")
  space-close    the detail pane closes on Space (onClose)

Run:  python3 tests/claude_tasks_contract.py [path/to/claude]
Exit: 0 all ok (or SKIP), 1 a check failed.
"""
import glob, mmap, os, re, sys

CHECKS = [
    ("confirm-no",
     [rb'context:"Confirmation",bindings:\{[^}]{0,200}escape:"confirm:no"']),
    ("escape-kill",
     [rb'if\(\w+\.name==="escape"&&(?P<te>[\w$]+)==="viewing-agent"\)',
      # its variable is the viewSelectionMode selector, near it
      None]),
    ("tasks-command",
     [rb'name:"tasks",[^}]{0,300}immediate:!0']),
    ("f-switch",
     [rb'\.key==="f"&&![\w$]+\.ctrl&&![\w$]+\.meta',
      rb'type==="local_agent"&&[\w$]+\([\w$]+\.task\)\)\{return'
      rb' ?(?P<fn>[\w$]+)\([\w$]+\.id,[\w$]+\),[\w$]+\("Viewing agent"',
      None]),
    ("space-close",
     [rb'function [\w$]+\(\{onClose:(?P<c>[\w$]+),onBack:[\w$]+,onKill:'
      rb'[\w$]+,onEnter:[\w$]+\}\)\{.{0,200}?if\([\w$]+\.key===" "\)'
      rb'[\w$]+\.preventDefault\(\),(?P=c)\(\)']),
]


def newest():
    c = glob.glob("/opt/homebrew/Caskroom/claude-code*/*/claude")
    key = lambda p: [int(x) if x.isdigit() else 0 for x in
                     re.split(r"[.-]", os.path.basename(os.path.dirname(p)))]
    return max(c, key=key) if c else None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else newest()
    if not path or not os.path.isfile(path):
        print("SKIP: no Claude Code bundle found")
        return 0
    with open(path, "rb") as f:
        b = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    m = re.search(rb'VERSION:"(\d+\.\d+\.\d+)"', b)
    print("bundle:", path)
    print("version:", m.group(1).decode() if m else
          os.path.basename(os.path.dirname(path)))
    bad = 0
    for name, pats in CHECKS:
        ok = True
        m = None
        for p in pats:
            if p is None and name == "escape-kill":
                te = re.escape(m.group("te"))
                ok = re.search(rb'[,{ ]' + te + rb'=[\w$]+\(\(([\w$]+)\)=>\1'
                               rb'\.viewSelectionMode\)',
                               b[max(0, m.start() - 20000):m.end()]) \
                    is not None
            elif p is None and name == "f-switch":
                # minified names repeat across chunks: any one of them
                fn = re.escape(m.group("fn"))
                ok = any(re.search(rb'viewSelectionMode:"viewing-agent"',
                                   b[d.end():d.end() + 1500])
                         for d in re.finditer(rb'function ' + fn + rb'\(', b))
            else:
                m = re.search(p, b)
                ok = m is not None
            if not ok:
                break
        print("%-14s %s" % (name, "ok" if ok else "FAIL"))
        bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
