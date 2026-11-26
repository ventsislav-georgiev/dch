"""A painted Codex 0.167 TUI for the --say Codex tests (#658), run by
tests/fake_codex.c under FAKE_CODEX_TUI. Strings from the parity spec, A1-A5.

$FAKE_CODEX_DIR/mode holds the screen: idle (the empty composer), busy (the
same plus the Working row), dialog (an exec approval), none (no composer),
side (the side conversation's composer), stream (an answer streaming: no
busy row, the plain composer, fresh output; Tab queues there). Every read from the terminal is
appended to $FAKE_CODEX_DIR/keys.log as one JSON string per line. A paste
fills the composer; Enter submits it (shown as history), Tab queues it while
busy (shown under "Queued follow-up inputs"); a dialog takes no input.
"""
import json, os, select, sys, time, tty

D = os.environ["FAKE_CODEX_DIR"]
DIM, OFF = "\x1b[2m", "\x1b[0m"


def mode():
    try:
        with open(os.path.join(D, "mode")) as f:
            return f.read().strip()
    except OSError:
        return "idle"


def paint(m, draft, hist, queued):
    rows = [">_ OpenAI Codex (v0.167.2)", ""] + ["› " + h for h in hist]
    if m == "none":
        rows += ["Loading..."]
    elif m == "dialog":
        rows += ["Would you like to run the following command?", "",
                 "$ rm -rf build", "",
                 "› 1. Yes, proceed (y)",
                 "  2. No, and tell Codex what to do differently (esc)", "",
                 "  Press enter to confirm or esc to cancel"]
    else:
        if m == "busy":
            rows += ["• Working (3s • esc to interrupt)"]
        if queued:
            rows += ["  Queued follow-up inputs"] + \
                    ["    ↳ " + q for q in queued]
        rows += [""]
        ph = "Ask a follow-up question" if m == "side" else \
             "Ask Codex to do anything"
        rows += ["› " + (draft if draft else DIM + ph + OFF)]
        rows += ["  " + " " * 40 + "100% context left" if draft else
                 "  ? for shortcuts" + " " * 30 + "100% context left"]
    sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(rows))
    sys.stdout.flush()


def main():
    fd = sys.stdin.fileno()
    tty.setraw(fd)
    sys.stdout.write("\x1b[?2004h")
    draft, hist, queued, buf = "", [], [], ""
    last = None
    log = open(os.path.join(D, "keys.log"), "a")
    while True:
        m = mode()
        if m == "exit":
            return
        st = (m, draft, len(hist), len(queued),
              int(time.time() * 10) if m == "stream" else 0)
        if st != last:
            paint(m, draft, hist, queued)
            last = st
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r:
            continue
        data = os.read(fd, 65536).decode("utf-8", "replace")
        if not data:
            return
        log.write(json.dumps(data) + "\n")
        log.flush()
        if m in ("dialog", "none"):
            continue
        buf += data
        while buf:
            if buf.startswith("\x1b[200~"):
                e = buf.find("\x1b[201~")
                if e < 0:
                    break
                draft += buf[6:e]
                buf = buf[e + 6:]
            elif buf[0] == "\r" and draft:
                hist.append(draft)
                draft, buf = "", buf[1:]
            elif buf[0] == "\t" and draft:
                (queued if m in ("busy", "stream") else hist).append(draft)
                draft, buf = "", buf[1:]
            else:
                if buf[0] >= " ":
                    draft += buf[0]
                buf = buf[1:]
        last = None


if __name__ == "__main__":
    main()
