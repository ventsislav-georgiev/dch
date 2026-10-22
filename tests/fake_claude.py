#!/usr/bin/env python3
"""Stand-in for Claude Code, for agent_restart_test.py. Not a test itself.

Installed as `claude` first on PATH. Like the real CLI it writes
$HOME/.claude/sessions/<pid>.json (CLAUDE_CONFIG_DIR wins), paints a composer
between two rules with a dim placeholder, goes busy for a turn per submitted
message, queues messages typed while busy, exits on /exit printing the
`claude --resume <id>` line, and accepts --resume <id>.

State lives in $FAKE_CLAUDE_DIR:
  version           installed version (read at start and by --version)
  <session>.ctl     JSON knobs the test flips while the fake runs:
                    shell, subagents, busy, dialog, draft, noexit, crash,
                    mangle, decoy, turn_ms, dialog_on_paste (a prompt comes
                    up as a paste ends), pasted_decoy (a "[Pasted text #1
                    +3 lines]" transcript line)
  <session>.log     JSON lines: start (argv, cwd, sid), line (typed), turn
                    (a message taken), enter_dialog (Enter while a prompt
                    is up: it would have answered it), exit
<session> is DCH_SESSION, which the master sets for its child.
"""
import json, os, select, signal, subprocess, sys, termios, textwrap, time
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

log(ev="start", argv=sys.argv[1:], cwd=os.getcwd(), sid=SID)
saved = termios.tcgetattr(0)
tty.setraw(0)
out = sys.stdout.buffer
transcript, queue, draft = [], [], ""
collapsed, prompt_up, pstart = False, False, 0
turn_end = 0.0
idle_gap = 0.0       # a turn ended with a message queued: idle this long


def paint(c):
    rows = ["  Claude Code v%s (fake)" % VERSION, ""] + transcript[-8:]
    if c.get("pasted_decoy"):
        rows.append("❯ [Pasted text #1 +3 lines]")
    if status == "busy":
        rows.append("✻ Working…")
    for q in queue:
        rows += ["❯ " + q, "  ctrl+enter to send now"]
    rule = "─" * 60
    if prompt_up:
        # a prompt dch's detection does not know (like the trust dialog):
        # only the composer check stands between it and our Enter
        rows += [rule, " A new kind of prompt", "", " \u276f 1. Yes",
                 "   2. No", "", " Enter to confirm \u00b7 Esc to cancel"]
    elif c.get("dialog"):
        rows += [rule, " Bash command", "", " Do you want to proceed?",
                 " ❯ 1. Yes", "   2. No", "", " Esc to cancel"]
    else:
        d = draft or c.get("draft", "")
        if d:
            # like Ink: a long paste collapses, a long draft word-wraps
            # under a two-space indent
            if collapsed:
                d = "[Pasted text #1]"
            w = textwrap.wrap(d, 70) or [d]
            comp = "\r\n".join(["❯ " + w[0]] +
                                 ["  " + l for l in w[1:]])
        elif queue:
            comp = "❯ \033[2mPress up to edit queued messages\033[0m"
        else:
            comp = "❯ \033[0m\033[2mTry \"fix the tests\"\033[0m"
        rows += [rule, comp, rule]
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


def submit(text, c):
    log(ev="line", text=text)
    if text.strip() == "/exit":
        if c.get("noexit"):
            return
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


set_status("idle")
last = None
buf = b""
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
        while buf:
            if buf.startswith(b"\033["):          # paste marker / key
                end = 2
                while end < len(buf) and not 0x40 <= buf[end] <= 0x7e:
                    end += 1
                if end >= len(buf):
                    break
                seq, buf = buf[:end + 1], buf[end + 1:]
                if seq == b"\033[200~":
                    pstart = len(draft)
                elif seq == b"\033[201~":
                    collapsed = len(draft) - pstart > 800
                    prompt_up = bool(c.get("dialog_on_paste"))
                continue
            ch, buf = buf[:1], buf[1:]
            if ch == b"\r" and (prompt_up or c.get("dialog")):
                log(ev="enter_dialog", text=draft)
            elif ch == b"\r":
                collapsed = False
                text, draft = draft, ""
                submit(text, c)
            elif ch == b"\x7f":
                draft = draft[:-1]
            elif ch == b"\x15":
                draft = ""
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
    elif status in ("idle", "shell") and not queue:
        want = "shell" if c.get("shell") else "idle"
        if want != status:
            set_status(want)
    view = (status, len(transcript), len(queue), draft, json.dumps(c),
            prompt_up, collapsed)
    if view != last:
        paint(c)
        last = view
