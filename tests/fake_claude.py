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
twice within 2s on an empty composer exits as /exit does.

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
                    exit_ms (/exit takes that long to quit),
                    dialog_on_kill (the first Ctrl-U raises a known
                    prompt over the draft instead of killing a row),
                    type_on_kill (text a human types after each Ctrl-U
                    kills its row: the composer never empties),
                    screen (rows painted in place of the composer and
                    footer: a dialog; every key typed then is logged as
                    key, and a digit dismisses it until the knob changes,
                    unless sticky), above (rows painted above the composer),
                    name (the session name Claude Code 2.1.322 paints into
                    the composer's top rule), bgwork (background work an
                    idle record does not show, a monitor say: /exit then
                    raises Claude Code's "Background work is running"
                    prompt, as it does under shell; "1" or Enter exits, "3"
                    or Esc stays), exit_eaten (every Enter on a typed
                    /exit is ignored: it stays in the composer)
  <session>.log     JSON lines: start (argv, cwd, sid, the CLAUDE*,
                    DCH_REVIVE* and Claude Code tool env keys, CLAUDE_CONFIG_DIR's value as
                    cfg), line (typed), turn (a message taken),
                    enter_dialog (Enter while a prompt is up: it would have
                    answered it), enter_eaten, enter_folded, key (typed while a screen
                    knob is up), paste (a paste's start, before lag_ms),
                    exit_prompt (the work it lists), exit_answer (the key),
                    exit
<session> is DCH_SESSION, which the master sets for its child.
"""
import json, os, select, signal, subprocess, sys, termios, time
import tty, uuid

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
    tmp = REC + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps(rec).encode())
    os.close(fd)
    os.rename(tmp, REC)


def bye(code=0, *_):
    try:
        os.unlink(REC)
    except OSError:
        pass
    os._exit(code)


signal.signal(signal.SIGTERM, lambda *a: bye(0))
signal.signal(signal.SIGHUP, lambda *a: bye(0))

log(ev="start", argv=sys.argv[1:], cwd=os.getcwd(), sid=SID,
    env=sorted(k for k in os.environ
               if k.startswith(("CLAUDE", "DCH_REVIVE")) or k in (
                   "GIT_EDITOR", "AI_AGENT", "COREPACK_ENABLE_AUTO_PIN",
                   "NoDefaultCurrentDirectoryInExePath")),
    cfg=os.environ.get("CLAUDE_CONFIG_DIR"))
saved = termios.tcgetattr(0)
tty.setraw(0)
out = sys.stdout.buffer
transcript, queue, draft = [], [], ""
collapsed, prompt_up, pstart, eaten = False, False, 0, 0
kill_dialog = False   # dialog_on_kill: a Ctrl-U raised a prompt
in_paste = False
ctrl_d = 0.0         # the first Ctrl-D of an exit's two
turn_end = 0.0
dismissed = None     # the screen knob a digit answered
exit_up = []         # /exit's "Background work is running" prompt, its work


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


def paint(c):
    rows = ["  Claude Code v%s (fake)" % VERSION, ""] + transcript[-8:]
    if screen_up(c):
        out.write(("\033[H\033[2J" + "\r\n".join(rows + c["screen"]))
                  .encode())
        out.flush()
        return
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
    if exit_up:
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
        if d:
            # like Ink: a long paste collapses, a long draft word-wraps
            # at the pane's width under a two-space indent, newlines kept
            if collapsed:
                d = "[Pasted text #1]"
            st = row_starts(d) + [len(d) + 1]
            w = [d[a:b].rstrip("\n ") for a, b in zip(st, st[1:])]
            comp = "\r\n".join(["❯ " + w[0]] +
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
        rows += [top, comp, rule]
    rows.append("  ⏵⏵ auto mode on" +
                (" · 1 shell" if status == "shell" else ""))
    if c.get("subagents"):
        rows.append("⏺ main")
        rows += ["◯ general-purpose  digging"] * int(c["subagents"])
    out.write(("\033[H\033[2J" + "\r\n".join(rows)).encode())
    out.flush()


def take(text):
    """Start a turn on `text`."""
    global turn_end
    log(ev="turn", text=text)
    transcript.append("❯ " + text[:50])
    if ctl().get("decoy"):
        transcript.append("  (see: claude --resume %s)" % DECOY)
    set_status("busy")
    turn_end = time.time() + ctl().get("turn_ms", 500) / 1000.0


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
        env = dict(os.environ)
        env.pop("DCH_SESSION", None)   # leave only ancestry to find us
        with open(os.path.join(D, SESS + ".run.out"), "w") as f:
            subprocess.Popen(text[5:], shell=True, stdout=f, stderr=f,
                             env=env)
        return
    if status == "busy":
        queue.append(text)
    else:
        take(text)


# start busy if asked, so a status read before the first tick never sees idle
if ctl().get("busy"):
    set_status("busy")
    turn_end = float("inf")
else:
    set_status("idle")
last = None
buf = b""


def resumed(*_):
    """fg after Ctrl-Z: raw mode back and a repaint, as Claude Code's Ink
    does on SIGCONT (the shell restored its own tty modes meanwhile)."""
    global last
    tty.setraw(0)
    last = None


signal.signal(signal.SIGCONT, resumed)
while True:
    c = ctl()
    r, _, _ = select.select([0], [], [], 0.1)
    if r:
        try:
            chunk = os.read(0, 4096)
        except OSError:
            bye(0)
        if not chunk:
            bye(0)
        buf += chunk
        burst = len(chunk) > 1
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
                elif seq == b"\033[201~":
                    in_paste = False
                    collapsed = len(draft) - pstart > 800
                    prompt_up = bool(c.get("dialog_on_paste"))
                continue
            ch, buf = buf[:1], buf[1:]
            if in_paste and ch in (b"\r", b"\n"):
                draft += "\n"      # a paste keeps its line breaks
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
            elif ch == b"\r":
                collapsed = False
                # the draft is bytes as latin-1: submit what was typed, UTF-8
                text, draft = draft, ""
                submit(text.encode("latin-1").decode("utf-8", "replace"), c)
            elif ch == b"\x04" and not draft:
                if time.time() - ctrl_d < 2:
                    submit("/exit", c)
                ctrl_d = time.time()
            elif ch == b"\x7f":
                draft = draft[:-1]
            elif ch == b"\x15" and (kill_dialog or c.get("dialog_on_kill")):
                kill_dialog = True     # the key went to the prompt
                log(ev="key", text="\x15")
            elif ch == b"\x15" and collapsed:
                draft, collapsed = "", False    # one row: the placeholder
            elif ch == b"\x15":
                draft = kill_row(draft) + c.get("type_on_kill", "")
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
            prompt_up, collapsed, dismissed, kill_dialog, len(exit_up))
    if view != last:
        paint(c)
        last = view
