#!/usr/bin/env python3
"""The X-ray view's o and names with a colon (#232, QA 1.58.0 cr-view).

Remote hosts on, a fake host on loopback in remote_hosts (nothing else is
dialled); each view on a pty this test owns (xray_remote_view_test's Pty).

Covers:
  1. a local session literally named h:s whose session is gone between the
     view's last update and o (its socket moved away under the view, the
     view's follow still connected, so the view still thinks it live):
     dch --xray h:s's o says "h:s has ended: no session to open" and stays,
     q exits 0; host h is not dialled and no new h:s is started;
  2. the same from the picker's x (dch -l): o says it in the view, which
     stays; q goes back to the picker; no new local h:s, host h not asked;
  3. a remote view of q:s whose host answers "session":"t", next to a local
     session literally named q:t: o attaches t on host q (its 0x01 names t),
     never the local q:t.

Run:  python3 tests/xray_view_o_test.py      (uses ./dch)
"""
import json, os, shutil, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xray_remote_view_test as X  # noqa: E402
from xray_remote_view_test import (CENV, DCH, Pty, check, dch,  # noqa: E402
                                   hosts, until)
from remote_client_test import Fake, frame  # noqa: E402

SOCKS = os.path.join(CENV["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid())
ENDED = b"h:s has ended: no session to open"


def sock(name):
    return os.path.join(SOCKS, name + ".sock")


def spawn(name):
    r = dch(dict(CENV, DCH_NO_REMOTE="1"), "--spawn", name, "--size",
            "80x24", "sh", "-c",
            "while :; do echo LOCAL-MARK; sleep 1; done")
    check("a local session named %s spawned" % name,
          r.returncode == 0 and os.path.exists(sock(name)), r.stderr)


def kill(name):
    """h:s's master, and whatever an o may have started in its place."""
    if os.path.exists(sock(name)):
        dch(dict(CENV, DCH_NO_REMOTE="1"), "-k", name)
    if os.path.exists(sock(name) + ".away"):
        os.rename(sock(name) + ".away", sock(name))
        dch(dict(CENV, DCH_NO_REMOTE="1"), "-k", name)


def ended_o(what, v, host):
    """The view v of the local h:s is up: its session goes (its socket
    moved away, the follow's connection kept) and o is pressed."""
    os.rename(sock("h:s"), sock("h:s") + ".away")
    n0 = len(host.conns)
    mark = len(v.out)
    v.key(b"o", 1.5)
    check("%s: o on a local h:s that has ended says so and the view stays"
          % what, v.wait(ENDED, 5, start=mark) and v.reap() is None,
          (v.rc, v.out[mark:][-500:]))
    check("%s: no new local h:s was started" % what,
          not os.path.exists(sock("h:s")), os.listdir(SOCKS))
    check("%s: host h was not dialled, nothing attached there" % what,
          len(host.conns) == n0 and not host.attached.is_set(),
          (len(host.conns) - n0, host.bodies))


def test_xray_ended():
    host = Host()
    hosts("h=127.0.0.1:%d" % host.port)
    spawn("h:s")
    v = Pty([DCH, "--xray", "h:s"])
    try:
        check("dch --xray h:s: the local session's view, o offered",
              v.wait(b"X-ray  h:s") and v.wait(b"o open session"),
              v.out[-600:])
        ended_o("dch --xray h:s", v, host)
        v.key(b"q")
        rc = v.exit()
        check("dch --xray h:s: q exits 0", rc == 0, (rc, v.out[-200:]))
    finally:
        v.close()
        kill("h:s")
        host.close()


def test_picker_ended():
    host = Host()
    hosts("h=127.0.0.1:%d" % host.port)
    spawn("h:s")
    v = Pty([DCH, "-l"], cols=140)
    try:
        v.wait(b"h:s", 15)
        v.pump(1.0)
        mark = len(v.out)
        v.key(b"x", 1.0)
        check("picker: x on the local h:s row opens its view, o offered",
              v.wait(b"X-ray  h:s", start=mark) and
              v.wait(b"o open session", start=mark), v.out[mark:][-600:])
        ended_o("picker", v, host)
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("picker: q in the view returns to the picker",
              v.reap() is None and v.wait(b"Sessions", start=mark),
              v.out[mark:][-600:])
        v.key(b"\033")
        check("picker: Esc quits it", v.exit() is not None, v.out[-80:])
    finally:
        v.close()
        kill("h:s")
        host.close()


class Host(Fake):
    """A host with ctl, ctl-cancel and xray that lists rows: its --follow
    answers a live snapshot of "session":"t" whatever was asked, and holds
    the stream; any other ctl (the picker's --history) ends 0 with nothing;
    its 0x01s are kept in bodies (Fake's)."""

    def __init__(self, rows=()):
        super().__init__("ok", rows=list(rows),
                         ops=["ctl", "ctl-cancel", "xray"])

    def serve(self, c):
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": "1.0",
                                               "server": "fake",
                                               "ops": self.ops}))
                    elif t == 0x03:
                        c.sendall(frame(0x11, self.rows))
                    elif t == 0x01:
                        self.bodies.append(body)
                        self.attached.set()
                        c.sendall(frame(0x10, b"FAKE-SCREEN\r\n"))
                    elif t == 0x30 and "--follow" in json.loads(body)["argv"]:
                        c.sendall(frame(0x31, json.dumps({
                            "schema": 1, "snapshot": {
                                "session": "t", "corpus": "shell",
                                "session_id": "m0000000000000001",
                                "live": True, "lines": 1,
                                "project_label": "/x", "chapters": [],
                                "refs": []}}).encode() + b"\n"))
                    elif t == 0x30:
                        c.sendall(frame(0x33, {"exit": 0}))
        except OSError:
            return


def test_remote_renamed():
    host = Host([{"name": "t"}])
    hosts("q=127.0.0.1:%d" % host.port)
    spawn("q:t")
    v = Pty([DCH, "--xray", "q:s"])
    try:
        check("q:s: the host's view, o offered",
              v.wait(b"X-ray  q:s") and v.wait(b"o open session"),
              v.out[-600:])
        mark = len(v.out)
        v.key(b"o", 0.3)
        ok = until(host.attached.is_set)
        v.pump(1.5)
        names = [json.loads(b).get("name") for b in host.bodies]
        check("q:s answered as t: o attaches t on host q, never the local "
              "q:t", ok and names == ["t"] and b"FAKE-SCREEN" in
              v.out[mark:] and b"LOCAL-MARK" not in v.out[mark:],
              (names, v.rc, v.out[mark:][-400:]))
    finally:
        v.close()
        dch(dict(CENV, DCH_NO_REMOTE="1"), "-k", "q:t")
        host.close()


def main():
    try:
        test_xray_ended()
        test_picker_ended()
        test_remote_renamed()
    finally:
        shutil.rmtree(X.T, ignore_errors=True)
    if X.fails[0]:
        print("%d failed" % X.fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
