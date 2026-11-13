#!/usr/bin/env python3
"""The X-ray reader reads on into the next and the previous chapter (#288).

A History Claude record, xv-nx, has four chapters: a prompt, a task
notification (an auto row, hidden in the list until n), a long middle prompt
that names PROJ-9, and a last prompt. The view runs on a pty this test owns,
so it needs no terminal mirror and runs the same on dch-lite (where the
engine still reads a Claude transcript).

Covers:
  1. Enter on the middle chapter: the header says "3 of 4" and its title;
  2. G, then j at its end: one line says the next chapter and the reader
     stays; j again opens chapter 4 at its top;
  3. [ and ]: the previous and the next chapter, at their tops; ] on the
     last says there is none and the reader stays;
  4. k at the middle's top: said, then k again opens the previous shown
     chapter (1, the auto row skipped while n hides it) at its end;
  5. q from the reader after turns: back on the list, the selection on the
     row the reader was opened from (the middle one);
  6. n shows the auto rows: [ from the middle reads the notification (2);
  7. from a card: Enter on PROJ-9's mention reads the middle chapter, ]
     turns, Esc goes back to the card, not to the middle chapter.
The view says prompt, never chapter (#295): the tab is Prompts, the footer
"] next prompt", the lines "end; again for the next prompt (4 of 4): ...",
"no next prompt", "top; again for the previous prompt (...)".

Run:  python3 tests/xray_reader_next_test.py      (uses ./dch)
"""
import fcntl, json, os, pty, re, select, shutil, signal, struct, subprocess
import sys, tempfile, termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
SID = "11111111-2222-4333-8444-5555555555d8"
P = "/home/demo/proj"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + repr(detail)[-900:])


T = tempfile.mkdtemp(prefix="xn")
HOME = os.path.join(T, "home")
os.makedirs(HOME)
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_SESSION", "DCH_SOCKET_DIR", "DCH_DEBUG",
                            "CLAUDE", "Malloc", "NO_COLOR", "COLORTERM"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", NO_COLOR="1",
           TERM="xterm-256color", LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")


class Pty:
    """A program on a pty: out holds every byte it wrote."""

    def __init__(self, argv, rows=30, cols=100):
        self.out = b""
        self.rc = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            os.execve(argv[0], argv, ENV)
        os.set_inheritable(self.fd, False)

    def pump(self, secs=0.05):
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [],
                                    max(0, end - time.time()))
            if not r:
                return
            try:
                b = os.read(self.fd, 65536)
            except OSError:
                b = b""
            if not b:
                self.reap()
                return
            self.out += b

    def reap(self):
        if self.rc is None:
            p, st = os.waitpid(self.pid, os.WNOHANG)
            if p:
                self.rc = os.waitstatus_to_exitcode(st)
        return self.rc

    def key(self, b):
        try:
            os.write(self.fd, b)
        except OSError:
            pass

    def close(self):
        os.close(self.fd)
        if self.reap() is None:
            os.kill(self.pid, signal.SIGKILL)
            end = time.time() + 10
            while self.reap() is None and time.time() < end:
                time.sleep(0.05)


CUP = re.compile(rb"\033\[(\d+);1H")


def rows(out):
    """The text last drawn on each row, its escapes removed."""
    got = {}
    parts = CUP.split(out)
    for i in range(1, len(parts) - 1, 2):
        got[int(parts[i])] = re.sub(rb"\033\[[0-9;?]*[A-Za-z]", b"",
                                    parts[i + 1]).decode("utf-8", "replace")
    return got


def screen(v):
    r = rows(v.out)
    return "\n".join(re.sub(" +", " ", r[k]) for k in sorted(r))


def head(v):
    return re.sub(" +", " ", rows(v.out).get(2, ""))


def wait(v, fn, secs=20.0):
    """fn(screen) true within secs (a loaded machine: room, still bounded)."""
    end = time.time() + secs
    while time.time() < end:
        v.pump(0.1)
        if fn(screen(v)):
            return True
        if v.rc is not None:
            break
    return bool(fn(screen(v)))


def press(v, k, fn, secs=20.0):
    v.key(k)
    return wait(v, fn, secs)


def note(inner):
    return "<task-notification>\n" + inner + "\n</task-notification>"


MID = "\n".join("middle line %02d" % i for i in range(1, 61))


def fixture():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    prompts = [("first prompt", "reply one"),
               (note("<task-id>t1</task-id>\n<status>completed</status>\n"
                     "<summary>Build finished</summary>"), "reply auto"),
               ("middle prompt about PROJ-9", MID),
               ("last prompt", "reply last")]
    lines = []
    for i, (t, r) in enumerate(prompts):
        o = {"type": "user", "cwd": P, "timestamp": "2026-11-10T1%d:00:00Z" % i,
             "message": {"content": t}}
        if t.startswith("<task-notification>"):
            o["origin"] = {"kind": "task-notification"}
        lines += [o, {"type": "assistant", "message": {"content": [
            {"type": "text", "text": r}]}}]
    with open(os.path.join(pdir, SID + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": "xv-nx", "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": SID, "cwd": P, "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-nx.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def at(n, title):
    """The header says chapter n of 4 and its title."""
    return lambda s, n=n, title=title: \
        ("%d of 4 %s" % (n, title)) in s.split("\n")[1] if "\n" in s else False


def selected(v, text):
    for r in rows(v.out).values():
        if text in r:
            return r.startswith(("▌", ">"))
    return False


def test_list():
    v = Pty([DCH, "--xray", "xv-nx"])
    try:
        check("open: Prompts (#295) counts the three shown",
              wait(v, lambda s: "Prompts 3" in s and "last prompt" in s),
              screen(v)[-600:])
        v.key(b"k")
        check("the selection on the middle chapter",
              wait(v, lambda s: selected(v, "middle prompt")), screen(v))
        ok = press(v, b"\r", lambda s: at(3, "middle prompt about PROJ-9")(s)
                   and "middle line 01" in s)
        check("Enter: the header says 3 of 4 and the middle's title", ok,
              head(v))
        check("the footer shows ] next prompt and [ previous (#295)",
              "] next prompt" in screen(v) and "[ previous" in screen(v)
              and "chapter" not in screen(v),
              screen(v)[-400:])
        press(v, b"G", lambda s: "middle line 60" in s)
        ok = press(v, b"j", lambda s: "end; again for the next prompt "
                   "(4 of 4): last prompt" in s)
        check("j at the end: one line says the next prompt (#295)", ok,
              screen(v)[-300:])
        check("... and the reader stays on the middle chapter",
              at(3, "middle prompt about PROJ-9")(screen(v)) and
              "middle line 60" in screen(v), screen(v))
        ok = press(v, b"j", lambda s: at(4, "last prompt")(s) and
                   "reply last" in s)
        check("j again: chapter 4 of 4, its text", ok, screen(v))
        ok = press(v, b"[", lambda s: at(3, "middle prompt about PROJ-9")(s))
        check("[: the previous chapter, 3 of 4, at its top",
              ok and "middle line 01" in screen(v) and
              "middle line 60" not in screen(v), screen(v))
        ok = press(v, b"]", lambda s: at(4, "last prompt")(s))
        check("]: the next chapter, 4 of 4", ok, head(v))
        ok = press(v, b"]", lambda s: "no next prompt" in s)
        check("] on the last: said, the reader stays",
              ok and at(4, "last prompt")(screen(v)), screen(v)[-300:])
        press(v, b"k", lambda s: "top; again for the previous prompt" in s)
        ok = press(v, b"k", lambda s: at(3, "middle prompt about PROJ-9")(s))
        check("k twice at the top of 4: the previous chapter, at its end",
              ok and "middle line 60" in screen(v) and
              "middle line 01" not in screen(v), screen(v))
        press(v, b"g", lambda s: "middle line 01" in s)
        ok = press(v, b"k", lambda s: "top; again for the previous prompt "
                   "(1 of 4): first prompt" in s)
        check("k at the top: says the previous shown chapter, the auto row "
              "skipped", ok, screen(v)[-300:])
        ok = press(v, b"k", lambda s: at(1, "first prompt")(s) and
                   "reply one" in s)
        check("k again: chapter 1 of 4 (not the hidden 2)", ok, screen(v))
        ok = press(v, b"q", lambda s: "Prompts 3" in s and
                   selected(v, "middle prompt"))
        check("q after the turns: back on the list, on the row it was "
              "opened from", ok, screen(v))
        # n shows the auto rows: the turns follow what the list shows
        press(v, b"n", lambda s: "Build finished" in s)
        press(v, b"\r", lambda s: at(3, "middle prompt about PROJ-9")(s))
        ok = press(v, b"[", lambda s: at(2, "")(s) and "reply auto" in s)
        check("n, then [ from the middle: the auto row, 2 of 4", ok,
              screen(v))
        v.key(b"q")
        wait(v, lambda s: "Prompts 3" in s)
        v.key(b"q")
        end = time.time() + 10
        while v.reap() is None and time.time() < end:
            v.pump(0.1)
        check("q quits, exit 0", v.rc == 0, v.rc)
    finally:
        v.close()


def test_card():
    v = Pty([DCH, "--xray", "xv-nx"])
    try:
        wait(v, lambda s: "Prompts 3" in s)
        v.key(b"\033[Z")  # Shift-Tab: Prompts back to Refs (#290)
        wait(v, lambda s: "PROJ-9" in s)
        ok = press(v, b"\r", lambda s: "Mentions: this session" in s)
        check("card: PROJ-9 opens", ok, screen(v)[-600:])
        ok = press(v, b"\r", lambda s: at(3, "middle prompt about PROJ-9")(s))
        check("card: Enter on the mention reads the middle chapter", ok,
              screen(v))
        ok = press(v, b"]", lambda s: at(4, "last prompt")(s))
        check("card's reader: ] turns to 4 of 4", ok, head(v))
        ok = press(v, b"\033", lambda s: "Mentions: this session" in s and
                   " of 4 " not in head(v))
        check("Esc: back to the card, not to the middle chapter", ok,
              screen(v))
        v.key(b"q")
        wait(v, lambda s: "Prompts 3" in s)
        v.key(b"q")
        end = time.time() + 10
        while v.reap() is None and time.time() < end:
            v.pump(0.1)
    finally:
        v.close()


def main():
    try:
        fixture()
        o = json.loads(subprocess.run(
            [DCH, "--xray", "xv-nx", "--json"], capture_output=True,
            text=True, timeout=60, env=ENV).stdout or "{}")
        ch = o.get("chapters", [])
        check("fixture: four chapters, the second auto",
              len(ch) == 4 and [c.get("auto", False) for c in ch] ==
              [False, True, False, False], ch)
        test_list()
        test_card()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
