#!/usr/bin/env python3
"""Stand-in for Claude Code, for agent_restart_test.py. Not a test itself.

Installed as `claude` first on PATH. Like the real CLI it writes
$HOME/.claude/sessions/<pid>.json (CLAUDE_CONFIG_DIR wins), paints a composer
between two rules with a dim placeholder, goes busy for a turn per submitted
message, queues messages typed while busy, exits on /exit printing the
`claude --resume <id>` line, and accepts --resume <id>. Its composer
word-wraps at the pane's width and keeps a paste's line breaks; Ctrl-U kills
to the start of the visual row, or the newline before it, as Claude Code
2.1.318's deleteToLineStart does; Backspace deletes one character. Ctrl-D
twice within 2s on an empty composer exits as /exit does. A "!" typed (not
pasted) into an empty composer enters bash mode, its prefix "!" in place of
"❯"; Backspace on an empty bash query leaves it; Enter there runs only a "run <cmd>",
logs bash and leaves it.

State lives in $FAKE_CLAUDE_DIR:
  version           installed version (read at start and by --version)
  <session>.ctl     JSON knobs the test flips while the fake runs:
                    shell, subagents, busy, dialog, draft, noexit, crash,
                    mangle, decoy, quote, turn_ms, dialog_on_paste (a prompt comes
                    up as a paste ends; "known": one dch detects),
                    pasted_decoy (a "[Pasted text #1
                    +3 lines]" transcript line), eat_enters (ignore that
                    many Enters on a draft, as a just-resumed Claude did),
                    lag_ms (a paste shows in the composer that late),
                    fold (an Enter read together with typed text folds
                    into it, as Claude Code's paste detection does),
                    exit_ms (/exit takes that long to quit), die_ms
                    (a kill or exit leaves the process that long),
                    peer (fields merged into its session record, as a
                    native-messaging Claude publishes),
                    dialog_on_kill (the first Ctrl-U raises a known
                    prompt over the draft instead of killing a row),
                    type_on_kill (text a human types after each Ctrl-U
                    kills its row: the composer never empties),
                    screen (rows painted in place of the composer and
                    footer: a dialog; every key typed then is logged as
                    key, and a digit dismisses it until the knob changes,
                    unless sticky), above (rows painted above the composer),
                    tear ([torn_ms, whole_ms]: while a draft shows, the
                    screen is caught mid-repaint torn_ms out of every
                    torn_ms + whole_ms: the rows above the composer drawn,
                    the composer and below not yet, as a mirror read
                    between the pty chunks of a long draft's frame sees it),
                    name (the session name Claude Code 2.1.322 paints into
                    the composer's top rule), bgwork (background work an
                    idle record does not show, a monitor say: /exit then
                    raises Claude Code's "Background work is running"
                    prompt, as it does under shell; "1" or Enter exits, "3"
                    or Esc stays), exit_eaten (every Enter on a typed
                    /exit is ignored: it stays in the composer), mcp_ms
                    (read at start: its MCP servers connect that long after
                    start, the record says idle at once unless
                    MCP_CONNECTION_NONBLOCKING=0 makes it wait; each turn logs mcp, true once they
                    have), bash (the composer is in bash mode, as a
                    human left it), key_cap (a draft stops growing at
                    that many characters: typed text never lands whole),
                    tasks (#713, read at start for its files: a /tasks
                    dialog laid out as Claude Code 2.1.322's "Background"
                    list, see TASKS below)
  <session>.log     JSON lines: start (argv, cwd, sid, the CLAUDE*,
                    DCH_REVIVE* and Claude Code tool env keys, CLAUDE_CONFIG_DIR's value as
                    cfg, MCP_CONNECTION_NONBLOCKING's value as nonblocking), line (typed), turn (a message taken),
                    enter_dialog (Enter while a prompt is up: it would have
                    answered it), enter_eaten, enter_folded, key (typed while a screen
                    knob is up), paste (a paste's start, before lag_ms),
                    exit_prompt (the work it lists), exit_answer (the key),
                    bash (a bash mode submit, its query),
                    exit
<session> is DCH_SESSION, which the master sets for its child.
"""
import json, os, select, signal, subprocess, sys, termios, time
import tty, unicodedata, uuid

D = os.environ["FAKE_CLAUDE_DIR"]
SESS = os.environ.get("DCH_SESSION", "nosession")
DECOY = "00000000-dead-beef-0000-000000000000"


def slurp(name, default=""):
    try:
        with open(os.path.join(D, name)) as f:
            return f.read().strip()
    except OSError:
        return default


VERSION = slurp("version", "0.0.0")
if sys.argv[1:2] == ["--version"]:
    print(VERSION + " (Claude Code)")
    sys.exit(0)

PID = os.getpid()
base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
    os.environ["HOME"], ".claude")
SDIR = os.path.join(base, "sessions")
os.makedirs(SDIR, mode=0o700, exist_ok=True)
REC = os.path.join(SDIR, "%d.json" % PID)
LOG = os.path.join(D, SESS + ".log")
args = sys.argv[1:]
SID = str(uuid.uuid4())
if "--resume" in args:
    i = args.index("--resume")
    if i + 1 < len(args):
        SID = args[i + 1]
PROC_START = subprocess.run(
    ["/bin/ps", "-o", "lstart=", "-p", str(PID)], capture_output=True,
    text=True, env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()


def log(**ev):
    ev["pid"] = PID
    with open(LOG, "a") as f:
        f.write(json.dumps(ev) + "\n")


def ctl():
    try:
        return json.loads(slurp(SESS + ".ctl", "{}") or "{}")
    except ValueError:
        return {}


status, stamp = None, 0


def set_status(s):
    global status, stamp
    status, stamp = s, int(time.time() * 1000)
    rec = {"pid": PID, "sessionId": SID, "cwd": os.getcwd(),
           "startedAt": stamp, "procStart": PROC_START, "version": VERSION,
           "kind": "interactive", "entrypoint": "cli", "status": s,
           "updatedAt": stamp, "statusUpdatedAt": stamp}
    rec.update(ctl().get("peer") or {})   # a native peer's record fields
    tmp = REC + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps(rec).encode())
    os.close(fd)
    os.rename(tmp, REC)


def bye(code=0, *_):
    time.sleep(ctl().get("die_ms", 0) / 1000.0)    # lingers after the kill
    try:
        os.unlink(REC)
    except OSError:
        pass
    os._exit(code)


signal.signal(signal.SIGTERM, lambda *a: bye(0))
signal.signal(signal.SIGHUP, lambda *a: bye(0))

log(ev="start", argv=sys.argv[1:], cwd=os.getcwd(), sid=SID,
    env=sorted(k for k in os.environ
               if k.startswith(("CLAUDE", "DCH_REVIVE", "ANTHROPIC")) or k in (
                   "GIT_EDITOR", "AI_AGENT", "COREPACK_ENABLE_AUTO_PIN",
                   "NoDefaultCurrentDirectoryInExePath")),
    cfg=os.environ.get("CLAUDE_CONFIG_DIR"),
    nonblocking=os.environ.get("MCP_CONNECTION_NONBLOCKING"))
saved = termios.tcgetattr(0)
tty.setraw(0)
RAW = termios.tcgetattr(0)
out = sys.stdout.buffer
transcript, queue, draft = [], [], ""
collapsed, prompt_up, pstart, eaten = False, False, 0, 0
kill_dialog = False   # dialog_on_kill: a Ctrl-U raised a prompt
in_paste = False
bash = False         # bash mode: a typed "!" on an empty composer
ctrl_d = 0.0         # the first Ctrl-D of an exit's two
turn_end = 0.0
dismissed = None     # the screen knob a digit answered
exit_up = []         # /exit's "Background work is running" prompt, its work


# --- /tasks (#713) ---------------------------------------------------------
# The tasks knob: {"version": written into every transcript line (default
# the installed one), "agents": [{"id","type","desc","state":"running|done|
# failed","bg"}] written as <transcript>/subagents files and Agent calls in
# its transcript ($CONFIG/projects/-fake/<sid>.jsonl), "rows": [{"label",
# "kind":"shell|agent|completed","id","state","meta"}] the dialog's rows
# below the "main" leader row, which shows while viewing an agent or, with
# "main_after_ms", that long after the dialog opened (agent rows painted as
# row_cells: icon, name, facts; "meta" [] leaves the facts column out, so
# the label shows whole), "view": the agent viewed at start, "shut": /tasks
# opens nothing (a busy turn queues it), "hints": false leaves the list's
# "Esc to close" hints row out}.
# /tasks (immediate: busy too) opens the list, focus on row 0, or with one row while on main that row's detail
# pane ("<type> › <label>"). List: Up/Down move, f takes main or an agent
# (the view switches) and closes, Enter opens a detail pane, Esc closes, x
# logs stop. Detail: f takes the agent, Space or Enter close, Left goes
# back, Esc closes. Outside the dialog Esc or Ctrl-C while viewing an agent
# logs kill (it would stop the agent) and goes back to main. Every byte read
# is logged as byte (hex) with the view and dialog; also tasks_open, view,
# esc, kill, stop.
vagent = None        # the agent id the main view shows, None main
dlg = None           # None, "list" or "detail"
dfocus, ddet, dopen = 0, None, 0.0


def tasks():
    return ctl().get("tasks")


def drows():
    t = tasks() or {}
    rows = list(t.get("rows", []))
    ma = t.get("main_after_ms")
    if vagent is not None or (ma is not None and dlg and
                            time.time() - dopen >= ma / 1000.0):
        rows.insert(0, {"label": "main", "kind": "main"})
    return rows


def tasks_files():
    """The transcript, its lines stamped with the version, and the
    subagents' files (sub_collect reads them)."""
    t = tasks()
    if not t:
        return
    ver = t.get("version", VERSION)
    d = os.path.join(base, "projects", "-fake")
    os.makedirs(os.path.join(d, SID, "subagents"), exist_ok=True)
    now = time.time()

    def iso(x):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(x))

    def jl(o):
        o.update(version=ver, sessionId=SID, timestamp=iso(now))
        return json.dumps(o, separators=(",", ":")) + "\n"
    lines = [jl({"type": "user", "message": {"role": "user",
                                             "content": "hi"}})]
    for a in t.get("agents", []):
        tu = "toolu_" + a["id"]
        sd = os.path.join(d, SID, "subagents")
        with open(os.path.join(sd, "agent-%s.meta.json" % a["id"]), "w") as f:
            json.dump({"agentType": a.get("type", "general-purpose"),
                       "description": a["desc"], "toolUseId": tu}, f)
        with open(os.path.join(sd, "agent-%s.jsonl" % a["id"]), "w") as f:
            f.write(json.dumps({"type": "user", "isSidechain": True,
                                "agentId": a["id"],
                                "timestamp": iso(now)}) + "\n")
        inp = {"description": a["desc"], "prompt": "p"}
        if a.get("bg"):
            inp["run_in_background"] = True
        lines.append(jl({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tu, "name": "Agent", "input": inp}]}}))
        if a.get("bg"):
            lines.append(jl({"type": "user", "message": {"content": [
                {"tool_use_id": tu, "type": "tool_result", "content": "ok"}]},
                "toolUseResult": {"status": "async_launched"}}))
        if a.get("state") in ("done", "failed"):
            blk = {"tool_use_id": tu, "type": "tool_result", "content": "ok"}
            if a["state"] == "failed":
                blk["is_error"] = True
            lines.append(jl({"type": "user", "message": {"content": [blk]},
                             "toolUseResult": {"status": "completed"}}))
    with open(os.path.join(d, SID + ".jsonl"), "w") as f:
        f.writelines(lines)


ICONS = {"running": "\u25cf", "done": "\u2714", "failed": "\u2718"}


def cw(t):
    """Columns text t takes (CJK and fullwidth 2)."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in t)


def cut_end(t, w):
    """Ink's wrap truncate-end: t in w columns, cut with "…"."""
    if cw(t) <= w:
        return t
    o = ""
    for c in t:
        if cw(o + c) > w - 1:
            break
        o += c
    return o + "\u2026"


def facts(meta, w):
    """so(): the longest " · " join of meta's leading items within w."""
    j = [" \u00b7 ".join(meta[:i + 1]) for i in range(len(meta))]
    return ([x for i, x in enumerate(j) if i == 0 or cw(x) <= w] or [""])[-1]


def row_cells(rows, width):
    """2.1.322's _o rows (zn's widths): an icon box iconWidth wide and a
    blank, the label in a name column nameWidth wide (truncate-end) and 3
    blanks, then the meta joined " · " in a facts column. A Local agents row
    is {label: description, icon: rk(status), meta: [status, model/effort]}.
    A label that already starts with an icon is painted as given (QA probes
    lay rows out themselves)."""
    def meta(r):
        if r.get("kind", "agent") not in ("agent", "completed"):
            return []
        st = r.get("state", "running" if r.get("kind", "agent") == "agent"
                   else "done")
        return [x for x in r.get("meta", [st, "opus/medium"]) if x]

    def icon(r):
        if r.get("kind", "agent") not in ("agent", "completed"):
            return ""
        return ICONS.get(r.get("state", "running"), ICONS["running"])
    items = [r for r in rows if r.get("kind") != "main"]
    W = max([cw(icon(r)) for r in items] + [0])
    B = [r for r in items if meta(r)]
    H = min(50, max([cw(r["label"]) for r in B] + [0]))
    G = max([cw(" \u00b7 ".join(meta(r))) for r in B] + [0])
    x = width - (W + 1 if W > 0 else 0) - 3
    S = x - min(H, max(-(-x // 2), x - G))
    V = 0 if H + G > x and x < 12 else \
        min(S, max([cw(facts(meta(r), S)) for r in B] + [0]))
    h = min(H, x - V)
    out = []
    for r in rows:
        lab = r["label"]
        if r.get("kind") == "main" or lab[:1] in ICONS.values():
            out.append(lab)
            continue
        box = (icon(r) + " " * W)[:W] + " " if W > 0 else ""
        f = facts(meta(r), V) if V > 0 and meta(r) else None
        if f is None:
            out.append(box + cut_end(lab, max(x + 3, 1)))
            continue
        name = cut_end(lab, h)
        # Ink moves over blank cells with CSI C instead of writing spaces
        pad = "\033[C" * (h - cw(name) + 3)
        out.append(box + name + pad + cut_end(f, V))
    return out


def tasks_paint():
    """The dialog in place of the composer: 2.1.322's row layout, a
    two-column glyph (the focused row's "❯", else blank) and a gap, then the
    row (row_cells); section headings "<Label> (<n>)" in the same column."""
    rows = drows()
    if dlg == "detail":
        r = rows[ddet] if ddet is not None and ddet < len(rows) else {}
        return ["─" * 60, " %s \u203a %s" % (
            "shell" if r.get("kind") == "shell" else "general-purpose",
            r.get("label", "?")), "  Running", "",
            " \u2190 to go back \u00b7 Space to close \u00b7 f to foreground"]
    try:
        width = os.get_terminal_size(0).columns - 2 - 3
    except OSError:
        width = 75
    cells = row_cells(rows, width)
    out = ["─" * 60, " Background", " %d active" % len(rows), ""]
    heads = {"shell": "Shells", "agent": "Local agents",
             "completed": "Completed"}
    last = None
    for i, r in enumerate(rows):
        k = r.get("kind", "agent")
        if k in heads and k != last:
            n = sum(1 for x in rows if x.get("kind", "agent") == k)
            if last is not None:
                out.append("")
            out.append("   %s (%d)" % (heads[k], n))
        last = k
        out.append(" %s %s" % ("\u276f" if i == dfocus else " ", cells[i]))
    if (tasks() or {}).get("hints", True):
        out += ["", " \u2191/\u2193 to select \u00b7 Enter to vagent \u00b7 f to "
                "foreground \u00b7 x to stop \u00b7 Esc to close"]
    return out


def tasks_open():
    global dlg, dfocus, ddet, dopen
    dopen, dfocus = time.time(), 0
    rows = drows()
    dlg = "list"
    if vagent is None and len(rows) == 1 and rows[0].get("kind") != "main":
        dlg, ddet = "detail", 0
    log(ev="tasks_open", text=dlg)


def tasks_close(why):
    global dlg
    dlg = None
    log(ev="tasks_close", text=why)


def set_view(v):
    global vagent
    vagent = v
    log(ev="view", text=v or "main")


def tasks_take(r):
    """f or Enter that takes a row's vagent: main or an agent."""
    if r.get("kind") == "main":
        set_view(None)
    elif r.get("kind") in ("agent", "completed"):
        set_view(r.get("id"))
    else:
        return False
    tasks_close("vagent")
    return True


def tasks_key(k):
    """A key while the dialog is up ("up", "down", or the byte)."""
    global dfocus, dlg, ddet
    rows = drows()
    if dlg == "detail":
        r = rows[ddet] if ddet is not None and ddet < len(rows) else {}
        if k == b"f":
            tasks_take(r)
        elif k in (b" ", b"\r"):
            tasks_close("space")
        elif k == b"\x1b":
            log(ev="esc", text="detail", vagent=vagent)
            tasks_close("esc")
        elif k == "left":
            dlg = "list"
        elif k == b"x":
            log(ev="stop", text=r.get("label"))
        return
    if k == "up":
        dfocus = max(dfocus - 1, 0)
    elif k == "down":
        dfocus = min(dfocus + 1, len(rows) - 1)
    elif k == b"\x1b":
        log(ev="esc", text="list", vagent=vagent)
        tasks_close("esc")
    elif k == b"x":
        log(ev="stop", text=rows[dfocus]["label"])
    elif k == b"f" and dfocus < len(rows):
        tasks_take(rows[dfocus])
    elif k == b"\r" and dfocus < len(rows):
        r = rows[dfocus]
        if r.get("kind") in ("main", "completed"):
            tasks_take(r)
        else:
            dlg, ddet = "detail", dfocus


def screen_up(c):
    return c.get("screen") and json.dumps(c) != dismissed
idle_gap = 0.0       # a turn ended with a message queued: idle this long


def row_starts(d):
    """Where each visual row of draft d starts: d's lines, each word-wrapped
    at the composer's width (the pane's, less the 2-column glyph)."""
    try:
        w = max(os.get_terminal_size(0).columns - 2, 10)
    except OSError:
        w = 70
    starts, i = [], 0
    for line in d.split("\n"):
        starts.append(i)
        off, rest = i, line
        while len(rest) > w:
            cut = rest.rfind(" ", 0, w + 1)
            cut = cut + 1 if cut > 0 else w
            off, rest = off + cut, rest[cut:]
            starts.append(off)
        i += len(line) + 1
    return starts


def kill_row(d):
    """Ctrl-U, as Claude Code 2.1.318's deleteToLineStart: right after a
    newline it deletes that newline, else it kills back to the start of the
    cursor's visual row (the cursor is at the end of the draft); at that
    row's column 0, the row before it."""
    if d.endswith("\n"):
        return d[:-1]
    st = row_starts(d)
    return d[:st[-1] if st[-1] < len(d) or len(st) < 2 else st[-2]]


def torn(c):
    t = c.get("tear")
    return bool(t and (draft or c.get("draft")) and
                time.time() * 1000 % (t[0] + t[1]) < t[0])


def paint(c):
    rows = ["  Claude Code v%s (fake)" % VERSION, ""] + transcript[-8:]
    if screen_up(c):
        n = out.write(("\033[H\033[2J" + "\r\n".join(rows + c["screen"]))
                      .encode())
        out.flush()
        return n
    rows += c.get("above", [])
    if c.get("quote"):
        # a reply quoting a prompt above the composer (Prosper #757)
        rows.append("⏺ The prompt reads \u201cDo you want to proceed?\u201d")
    if c.get("pasted_decoy"):
        rows.append("❯ [Pasted text #1 +3 lines]")
    if status == "busy":
        rows.append("✻ Working…")
    for q in queue:
        rows += ["❯ " + q, "  ctrl+enter to send now"]
    rule = "─" * 60
    if dlg:
        rows += tasks_paint()
    elif exit_up:
        # Claude Code 2.1.322's, under a ▔ rule (dch reads it as a prompt)
        rows += ["▔" * 60, "   Background work is running",
                 "   The following will stop when you exit:"]
        rows += ["   " + w for w in exit_up]
        rows += ["   ❯ 1. Exit and stop tasks",
                 "     2. Move to background and exit", "     3. Stay",
                 "   Enter to confirm · Esc to cancel"]
    elif prompt_up and c.get("dialog_on_paste") == "known":
        # a permission prompt dch knows (screen_modal: CG_DIALOG)
        rows += [rule, " Bash command", "", " Do you want to proceed?",
                 " ❯ 1. Yes", "   2. No", "", " Esc to cancel"]
    elif prompt_up:
        # a prompt dch's detection does not know (like the trust dialog):
        # only the composer check stands between it and our Enter
        rows += [rule, " A new kind of prompt", "", " \u276f 1. Yes",
                 "   2. No", "", " Enter to confirm \u00b7 Esc to cancel"]
    elif c.get("dialog") or kill_dialog:
        rows += [rule, " Bash command", "", " Do you want to proceed?",
                 " ❯ 1. Yes", "   2. No", "", " Esc to cancel"]
    else:
        d = draft or c.get("draft", "")
        g = "!" if bash or c.get("bash") else "❯"
        if g == "!" and not d:
            comp = "!\u00a0"     # Claude Code shows no placeholder there
        elif d:
            # like Ink: a long paste collapses, a long draft word-wraps
            # at the pane's width under a two-space indent, newlines kept
            if collapsed:
                d = "[Pasted text #1]"
            st = row_starts(d) + [len(d) + 1]
            w = [d[a:b].rstrip("\n ") for a, b in zip(st, st[1:])]
            comp = "\r\n".join([g + "\u00a0" + w[0]] +
                                 ["  " + l for l in w[1:]])
            # Ink moves the cursor over blanks instead of writing spaces,
            # so ghostty 1.4's dump has CSI C where the spaces were
            comp = comp.replace(" ", "\033[C")
        elif queue:
            comp = "❯ \033[2mPress up to edit queued messages\033[0m"
        else:
            comp = "❯ \033[0m\033[2mTry \"fix the tests\"\033[0m"
        top = rule
        if c.get("name"):
            top = "─" * 50 + " %s ─" % c["name"]
        if torn(c):
            n = out.write(("\033[H\033[2J" + "\r\n".join(rows)).encode())
            out.flush()
            return n
        rows += [top, comp, rule]
    rows.append("  ⏵⏵ auto mode on" +
                (" · 1 shell" if status == "shell" else ""))
    if c.get("subagents"):
        rows.append("⏺ main")
        rows += ["◯ general-purpose  digging"] * int(c["subagents"])
    n = out.write(("\033[H\033[2J" + "\r\n".join(rows)).encode())
    out.flush()
    return n


def take(text):
    """Start a turn on `text`."""
    global turn_end
    log(ev="turn", text=text, mcp=time.time() >= MCP_AT)
    transcript.append("❯ " + text[:50])
    if ctl().get("decoy"):
        transcript.append("  (see: claude --resume %s)" % DECOY)
    set_status("busy")
    turn_end = time.time() + ctl().get("turn_ms", 500) / 1000.0


def run_cmd(cmd):
    """A "run <cmd>" line (pasted "!run", or bash mode's "run"): run it"""
    env = dict(os.environ)
    env.pop("DCH_SESSION", None)   # leave only ancestry to find us
    with open(os.path.join(D, SESS + ".run.out"), "w") as f:
        subprocess.Popen(cmd, shell=True, stdout=f, stderr=f, env=env)


def submit(text, c, confirmed=False):
    global exit_up
    log(ev="line", text=text)
    if text.strip() == "/exit":
        if c.get("noexit"):
            return
        work = (["shell · sleep 300"] if status == "shell" else []) + \
            c.get("bgwork", [])
        if work and not confirmed:
            exit_up = work
            log(ev="exit_prompt", text="; ".join(work))
            set_status("waiting")
            return
        time.sleep(c.get("exit_ms", 0) / 1000.0)
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
        if c.get("crash"):
            out.write(b"\033[2J\033[HError: fake claude crashed\r\n"
                      b"  at teardown (fake.js:42)\r\n\r\n")
            out.flush()
            bye(3)
        line = "claude --resume " + SID
        if c.get("mangle"):
            # Ink styles the id apart, and a narrow pane hard-wraps it
            line = ("claude --resume \033[1m" + SID[:10] + "\033[22m" +
                    SID[10:20] + "\r\n" + SID[20:])
        out.write(("\033[2J\033[H\033[2mResume this session with:\033[22m\r\n"
                   "\033[2m" + line + "\033[22m\r\n").encode())
        out.flush()
        log(ev="exit")
        bye(0)
    if text.startswith("!run "):
        run_cmd(text[5:])
        return
    if text.strip() == "/tasks" and tasks() is not None and \
            not tasks().get("shut"):
        tasks_open()      # immediate: a busy turn does not queue it
        return
    if status == "busy":
        queue.append(text)
    else:
        take(text)


tasks_files()
vagent = (tasks() or {}).get("view")     # already viewing that agent
# its MCP servers connect in the background, after the idle record (#364)
MCP_AT = time.time() + ctl().get("mcp_ms", 0) / 1000.0
# start busy if asked, so a status read before the first tick never sees idle
if ctl().get("busy"):
    set_status("busy")
    turn_end = float("inf")
else:
    # MCP_CONNECTION_NONBLOCKING=0: Claude waits for its servers before it
    # is usable, so the idle record comes only after they connected
    if os.environ.get("MCP_CONNECTION_NONBLOCKING") == "0":
        time.sleep(max(0, MCP_AT - time.time()))
    set_status("idle")
last = None
buf = b""
conted = False      # SIGCONT came: raw mode back and a repaint, in the loop
cont_paints = 0     # paints after a SIGCONT still to log (#330)


def stopped(*_):
    """Ctrl-Z (SIGTSTP): log it, then stop as the default action would."""
    log(ev="stop")
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    os.kill(PID, signal.SIGTSTP)
    signal.signal(signal.SIGTSTP, stopped)


def resumed(*_):
    """fg after Ctrl-Z: raw mode back and a repaint, as Claude Code's Ink
    does on SIGCONT (the shell restored its own tty modes meanwhile). The
    handler only flags it: the loop sets raw with TCSANOW, never setraw's
    TCSAFLUSH, which waits for output to drain (#330)."""
    global conted
    log(ev="cont")
    conted = True


signal.signal(signal.SIGTSTP, stopped)
signal.signal(signal.SIGCONT, resumed)
while True:
    if conted:
        conted = False
        try:
            termios.tcsetattr(0, termios.TCSANOW, RAW)
            log(ev="raw")
        except termios.error as e:
            log(ev="raw", text="error: %s" % e)
        last, cont_paints = None, 3
    c = ctl()
    r, _, _ = select.select([0], [], [], 0.02 if c.get("tear") else 0.1)
    if r:
        try:
            chunk = os.read(0, 4096)
        except OSError:
            bye(0)
        if not chunk:
            bye(0)
        buf += chunk
        burst = len(chunk) > 1
        if tasks() is not None:
            for bt in chunk:
                log(ev="byte", text="%02x" % bt, view=vagent, dlg=dlg)
        while buf:
            if buf.startswith(b"\033["):          # paste marker / key
                end = 2
                while end < len(buf) and not 0x40 <= buf[end] <= 0x7e:
                    end += 1
                if end >= len(buf):
                    break
                seq, buf = buf[:end + 1], buf[end + 1:]
                if seq == b"\033[200~":
                    in_paste = True
                    pstart = len(draft)
                    log(ev="paste")    # before the lag: it has the paste
                    # busy right after --resume: a paste shows this late
                    time.sleep(c.get("lag_ms", 0) / 1000.0)
                elif dlg and seq in (b"\033[A", b"\033[B", b"\033[D"):
                    tasks_key({b"A": "up", b"B": "down",
                               b"D": "left"}[seq[2:]])
                elif seq == b"\033[201~":
                    in_paste = False
                    collapsed = len(draft) - pstart > 800
                    prompt_up = bool(c.get("dialog_on_paste"))
                continue
            ch, buf = buf[:1], buf[1:]
            if in_paste and ch in (b"\r", b"\n"):
                draft += "\n"      # a paste keeps its line breaks
            elif dlg:
                tasks_key(ch)
            elif ch in (b"\x1b", b"\x03") and vagent is not None and \
                    tasks() is not None:
                log(ev="kill", text=vagent)   # Claude Code stops the agent
                set_view(None)
            elif exit_up and ch in (b"1", b"\r"):
                log(ev="exit_answer", text=ch.decode())
                exit_up = []
                submit("/exit", c, True)
            elif exit_up and ch in (b"3", b"\x1b"):
                log(ev="exit_answer", text=ch.decode())
                exit_up = []
                transcript.append("❯ /exit")
                set_status("shell" if c.get("shell") else "idle")
            elif exit_up:
                pass               # the prompt takes no other key
            elif screen_up(c):
                log(ev="key", text=ch.decode("latin-1"))
                if ch.isdigit() and not c.get("sticky"):
                    dismissed = json.dumps(c)
            elif ch == b"\r" and (prompt_up or c.get("dialog") or kill_dialog):
                log(ev="enter_dialog", text=draft)
            elif ch == b"\r" and burst and c.get("fold"):
                log(ev="enter_folded", text=draft)
            elif ch == b"\r" and draft == "/exit" and c.get("exit_eaten"):
                log(ev="enter_eaten", text=draft)
            elif ch == b"\r" and draft and eaten < c.get("eat_enters", 0):
                eaten += 1
                log(ev="enter_eaten", text=draft)
            elif ch == b"\r" and bash:
                log(ev="bash", text=draft)      # runs only a "run <cmd>"
                if draft.startswith("run "):
                    run_cmd(draft[4:])
                draft, bash = "", False
            elif ch == b"\r":
                collapsed = False
                # the draft is bytes as latin-1: submit what was typed, UTF-8
                text, draft = draft, ""
                submit(text.encode("latin-1").decode("utf-8", "replace"), c)
            elif ch == b"\x04" and not draft:
                if time.time() - ctrl_d < 2:
                    submit("/exit", c)
                ctrl_d = time.time()
            elif ch == b"\x7f" and bash and not draft:
                bash = False
            elif ch == b"\x7f":
                draft = draft[:-1]
            elif ch == b"\x15" and (kill_dialog or c.get("dialog_on_kill")):
                kill_dialog = True     # the key went to the prompt
                log(ev="key", text="\x15")
            elif ch == b"\x15" and collapsed:
                draft, collapsed = "", False    # one row: the placeholder
            elif ch == b"\x15":
                draft = kill_row(draft) + c.get("type_on_kill", "")
            elif ch == b"!" and not draft and not bash and not in_paste:
                bash = True
            elif ch >= b" " and len(draft) >= c.get("key_cap", 1 << 30):
                pass               # lost: the text never lands whole
            elif ch >= b" ":
                draft += ch.decode("latin-1")
    now = time.time()
    if c.get("busy") and status != "busy":
        set_status("busy")
        turn_end = float("inf")
    elif status == "busy" and not c.get("busy") and (
            now >= turn_end or turn_end == float("inf")):
        transcript.append("⏺ ok")
        if queue:
            set_status("idle")
            idle_gap = now + 1.0
        else:
            set_status("shell" if c.get("shell") else "idle")
    elif status in ("idle", "shell") and queue and now >= idle_gap:
        take(queue.pop(0))
    elif status in ("idle", "shell") and not queue and not exit_up:
        want = "shell" if c.get("shell") else "idle"
        if want != status:
            set_status(want)
    view = (status, len(transcript), len(queue), draft, json.dumps(c),
            prompt_up, collapsed, dismissed, kill_dialog, len(exit_up),
            torn(c), bash, dlg, dfocus, len(drows()) if dlg else 0, vagent)
    if view != last:
        if cont_paints:
            cont_paints -= 1
            try:
                log(ev="paint", text="%s bytes" % paint(c))
            except OSError as e:
                log(ev="paint", text="OSError: %s" % e)
                raise
        else:
            paint(c)
        last = view
