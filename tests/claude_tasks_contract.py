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
  (#724, what va_parse reads off a row)
  agent-item     a local_agent row item is {label: description, icon:
                 <status glyph>, meta: [status...]}
  row-columns    the row component takes {item, iconWidth, nameWidth,
                 factsWidth} and draws an icon box (width iconWidth,
                 marginRight 1), the label in a name column nameWidth wide
                 with a margin of 3 (truncate-end), the meta joined " \xb7 "
                 in a facts column; the name column is at most 50 wide
  local-agents   the agents' section heading reads "Local agents"
  dialog-title   the list's title is "Background", drawn on the line under
                 the dialog's rule of "\u2500"
  (XI7)
  dialog-hints   the list's hints row ends with the escape key's hint,
                 action "close" (outside the actions submenu), drawn
                 "Esc to close" ("<key> to <action>", escape as "Esc")

Run:  python3 tests/claude_tasks_contract.py [path/to/claude]
Exit: 0 all ok (or SKIP), 1 a check failed.
"""
import glob, mmap, os, re, sys

W = rb'[\w$]+'


def row_columns(b):
    m = re.search(rb'function ' + W + rb'\((?P<o>' + W + rb')\)\{let ' + W +
                  rb'=' + W + rb'\(\d+\),\{item:' + W + rb',iconWidth:(?P<iw>' +
                  W + rb'),nameWidth:(?P<nw>' + W + rb'),factsWidth:(?P<fw>' +
                  W + rb'),rowLines:' + W + rb'\}=(?P=o),\{icon:' + W +
                  rb',meta:' + W + rb',\.\.\.' + W + rb'\}=', b)
    if not m:
        return False
    body = b[m.end():m.end() + 4000]
    g = {k: re.escape(v) for k, v in m.groupdict().items()}
    # the facts: so(meta, factsWidth), the meta's items joined " · "
    so = re.search(rb'\?(' + W + rb')\(' + W + rb',' + g["fw"] + rb'\):void 0',
                   body)
    mg = re.search(rb'===void 0\?0:(?P<io>' + W + rb'),', body)
    widths = re.search(rb'var ' + W + rb'=2,' + (re.escape(mg.group("io"))
                       if mg else b"@") + rb'=3,(?P<cap>' + W + rb')=50,', b)
    return bool(
        re.search(rb'width:' + g["iw"] + rb',marginRight:1', body) and
        re.search(rb'===void 0\?void 0:' + g["nw"] + rb',', body) and
        re.search(rb'wrap:"truncate-end",children:', body) and
        re.search(rb'maxWidth:' + g["fw"] + rb',', body) and
        so and re.search(rb'function ' + re.escape(so.group(1)) +
                         rb'\(' + W + rb',' + W + rb'\)\{return .{0,80}\.join\('
                         rb'" \\xB7 "\)', b) and mg and widths and
        re.search(rb'Math\.min\(' + re.escape(widths.group("cap")) +
                  rb',Math\.max\(0,', b[widths.end():widths.end() + 3000]))


def dialog_title(b):
    if not re.search(rb'\(' + W + rb',\{title:"Background",subtitle:', b):
        return False
    # the rule: the dialog frame's paddingTop:1, its rule, then the column
    # whose first child is the title; the rule's default char is U+2500
    r = re.search(rb'function ' + W + rb'\(' + W + rb'\)\{let ' + W + rb'=' +
                  W + rb'\(\d+\),\{width:' + W + rb',color:' + W +
                  rb',char:(?P<c>' + W + rb'),padding:' + W + rb',title:' +
                  W + rb',titleAlign:' + W + rb'\}=' + W + rb',' + W + rb'=(?P=c)'
                  rb'===void 0\?(?P<d>' + W + rb'):', b)
    return bool(
        r and
        re.search(rb'flexDirection:"column",paddingTop:1,children:\[', b) and
        re.search(rb'[,\s]' + re.escape(r.group("d")) + rb'="\\u2500"', b))


def dialog_hints(b):
    a = re.search(rb'const (?P<a>' + W + rb')=' + W + rb'\.mode==="actions"'
                  rb'\?"go back":"close"', b)
    return bool(
        a and re.search(rb'\(' + W + rb',\{chord:"escape",action:' +
                        re.escape(a.group("a")) + rb',\.\.\.',
                        b[a.end():a.end() + 300]) and
        re.search(rb'=' + W + rb'\?" ":" to ";', b) and
        re.search(rb'escape:\["Esc",', b))


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
    ("agent-item",
     [rb'case"local_agent":\{let .{0,300}?return\{label:(?P<h>[\w$]+)'
      rb'\.description,icon:.{0,200}?[\w$]+\((?P=h)\.status\),meta:\['
      rb'`\$\{(?P=h)\.status']),
    ("row-columns", row_columns),
    ("local-agents",
     [rb'key:"local-agents",items:[\w$]+,heading:[\w$]+\([\w$]+,'
      rb'\{label:"Local agents",size:']),
    ("dialog-title", dialog_title),
    ("dialog-hints", dialog_hints),
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
        if callable(pats):
            ok, pats = bool(pats(b)), []
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
