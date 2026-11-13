"""The dch client for remote hosts (#087): -ls, --ls-json/-lj --remote, -a.

A real `dch serve` runs on 127.0.0.1 (DCH_SERVE_TEST_LOOPBACK=1, --bind
127.0.0.1 --port 0, a fake `tailscale` first on PATH), its sessions in their
own socket dir; the client has another, and reaches the server only through
--hosts label=127.0.0.1:port. DCH_NO_REMOTE=1 on the client, so discovery
never runs except in the checks that feed it a stub status and name an
unknown host, or one whois refuses (neither dials anything). Fake servers on loopback play a hostile
peer, a hanging one, a newer protocol and a link that drops.

Covers: remote rows in -ls, -lj and --ls-json with --remote only, and plain
-ls, -lj and --ls-json never connecting; -a host:name shows the screen,
input round-trips, a resize arrives, the detach leaves the session alive;
--host <h> -a <name>; an exact local name wins; an unknown host (and
discovery's peer filter, by its known list, only with BackendState Running);
a discovered host refused unless `tailscale whois` says it is the user's,
untagged; a hostile server's control bytes escaped (C1 and bidi too, in
--ls-json); a hanging host within the time box, then its cached rows marked
stale; a tampered cache (an "at" out of range, a host that is not a label)
shown nowhere, the picker included; a newer major skipped; a dropped link
exits 1 with a message and the terminal restored; a 0x13 then a reset keeps
the 0x13's reason; 0x12 ends the attach with 0; a host whose gate refuses us
(a 0x13 with a code, #094): -a says why and exits 4 (#102), --ls/--ls-json --remote
say why and show none of its cached rows, against a fake and dch serve; a
busy host (#095) is transient: -a says its message and exits 1, --ls/--ls-json
--remote keep its cached rows as stale. #101: --scrollback and
DCH_REPLAY_SCROLLBACK put "scrollback" in the 0x01 (none for 0), and over a
real dch serve the rows reach the terminal. #102: a newer attach to the same
session over a real dch serve ends the older with its "taken" message and
exit 5 (and a fake's 0x13 "taken" the same). #114: each control verb on
host:name (and --host) over a dch serve --allow-create prints and exits
byte for byte as the same verb run there; --cwd; the client's own refusals;
exit 6 for a host without ctl (or create), 7 for a closed port or a cut
link, 4 for a coded 0x13, 1 with the reason for an uncoded one. #157: each
--xray form on srv:<n> (remote_hosts) byte for byte as run on the host,
3.1's value rules checked before anything is sent, exit 6 for a host
without "xray", 4 and 7 as above, and Ctrl-C, SIGTERM or a closed stdout
ending the host's --follow. #158: --xray srv:<n> --requests --json, every
rule of spec 4.3 (requests_checks), with a relay (Proxy) that hides
"ctl-cancel", cuts the link or holds a request.

Run:  python3 tests/remote_client_test.py        (uses ./dch)
"""
import errno, json, os, re, select, shutil, signal, socket
import struct, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
# the 300 ms box plus a process start; ASan (DCH_TEST_NO_BUDGET) is slower
BUDGET = 10.0 if os.environ.get("DCH_TEST_NO_BUDGET") else 1.5
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))

# Builtins only, no fork: serve gives each call 3 s (TS_MS), and under load
# a slower fake turns into a whois_failed refusal cached 30 s (#246, #249).
FAKE_TS = """#!/bin/sh
d=${0%/*}
case "$1" in
status) f=status.json ;; whois) f=whois.json ;; ip) f=ip.txt ;; *) exit 1 ;;
esac
[ -f "$d/$f" ] || exit 1
while IFS= read -r l; do printf '%s\\n' "$l"; done < "$d/$f"
printf '%s' "$l"
"""
OWNER = {"Self": {"ID": "1001", "UserID": 42, "Tags": []},
         "User": {"42": {"LoginName": "me@example.com"}}}
WHOIS = {"UserProfile": {"ID": 42, "LoginName": "me@example.com"},
         "Node": {"ID": 2002, "StableID": "2002", "Tags": []}}
# discovery's input: only "mybox" is the user's, online and untagged. None
# is ever dialled: the checks name an unknown host, or one whois refuses
# before any connect. mybox's address is in 100.115.92.0/23, which
# Tailscale never assigns (ChromeOS reserves it), should one ever be.
PEERS = dict(OWNER, BackendState="Running", Peer={
    "k1": {"DNSName": "mybox.tail1.ts.net.", "HostName": "MyBox",
           "UserID": 42, "Online": True, "TailscaleIPs": ["100.115.92.1"]},
    "k2": {"DNSName": "theirs.tail1.ts.net.", "UserID": 7, "Online": True,
           "TailscaleIPs": ["100.64.9.2"]},
    "k3": {"DNSName": "server.tail1.ts.net.", "UserID": 42, "Online": True,
           "Tags": ["tag:server"], "TailscaleIPs": ["100.64.9.3"]},
    "k4": {"DNSName": "sleepy.tail1.ts.net.", "UserID": 42,
           "Online": False, "TailscaleIPs": ["100.64.9.4"]},
    "k5": {"DNSName": "noip.tail1.ts.net.", "UserID": 42, "Online": True,
           "TailscaleIPs": ["8.8.8.8"]}})


# dch -a's exit codes (#102): 0 detached/ended, 1 lost/down/busy (retry ok)
REFUSED_EXIT, TAKEN_EXIT = 4, 5
TAKEN = {"code": "taken", "message": "another client attached to this "
         "session"}
REFUSAL = {"code": "not_owner",
           "message": "this host serves only its owner's devices"}
# the one transient code (#095): try again later, not a refusal
BUSY = {"code": "busy",
        "message": "this host is at its connection limit: try again shortly"}


def read_clock_at(want, got, secs):
    """want, got: lines (str or bytes) of two runs. A live shell's screen
    rows carry the clock at read time as their "at" (dch.c ring_screen:
    time(NULL)), so a run a second later says a second later. got's mention
    "at" takes want's where it is later by 0 to secs (the seconds between
    the runs), never earlier; every other byte still has to match."""
    if len(want) != len(got):
        return got
    out = []
    for w, g in zip(want, got):
        p = MENTION_AT[isinstance(w, bytes)]
        mw, mg = p.search(w), p.search(g)
        if mw and mg and 0 <= int(mg[2]) - int(mw[2]) <= secs:
            g = g[:mg.start(2)] + mw[2] + g[mg.end(2):]
        out.append(g)
    return out


MENTION_AT = {False: re.compile(r'("mention":\{.*?"at":)(\d+)'),
              True: re.compile(rb'("mention":\{.*?"at":)(\d+)')}


def frame(t, payload=b""):
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload).encode()
    return struct.pack(">BI", t, len(payload)) + payload


class Fake:
    """A remote-frames server on loopback that answers as `mode` says."""

    def __init__(self, mode, rows=None, proto="1.0", ops=None):
        self.mode, self.rows, self.proto = mode, rows or [], proto
        self.ops = ops
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        self.attached = threading.Event()
        self.bodies = []  # each 0x01's payload: the name it attaches
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            if self.mode != "hang":
                threading.Thread(target=self.serve, args=(c,),
                                 daemon=True).start()

    def serve(self, c):
        buf = b""
        try:
            if self.mode in ("refuse", "busy"):
                # dch serve's gate (#094) or its limits (#095): one 0x13
                # with a code before a byte is read, the FIN, then read off
                # what the client sent
                c.sendall(frame(0x13, REFUSAL if self.mode == "refuse"
                                else BUSY))
                c.shutdown(socket.SHUT_WR)
                while c.recv(65536):
                    pass
                return
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
                        hello = {"proto": self.proto, "server": "fake"}
                        if self.ops is not None:
                            hello["ops"] = self.ops
                        c.sendall(frame(0x18, hello))
                    elif t == 0x30:
                        # a ctl (#114): what the mode says, then the FIN
                        self.bodies.append(body)
                        c.sendall({
                            "ctlcoded": frame(0x13, REFUSAL),
                            "ctlerr": frame(0x13, {"message": "nope-why"}),
                            "ctlcut": frame(0x31, b"part\n")
                            + frame(0x32, b"err\n")
                            + frame(0x33, {"exit": 2, "why": "cap"}),
                            # #129: 60 bytes, 240 escaped: past why[100]
                            "ctlwhy": frame(0x33, {"exit": 2,
                                                   "why": "\x01" * 60}),
                            "ctldrop": frame(0x31, b"half"),
                            # #130: a why or a 0x33 we cannot read
                            "ctlwhylong": frame(0x33, {"exit": 2,
                                                       "why": "\x01" * 64}),
                            "ctlwhynul": frame(0x33, {"exit": 2,
                                                      "why": "a\x00b"}),
                            "ctlwhynum": frame(0x33, {"exit": 2, "why": 7}),
                            "ctlbig": frame(0x33, {"exit": 2,
                                                   "why": "\x1b" * 100}),
                        }[self.mode])
                        c.shutdown(socket.SHUT_WR)
                    elif t == 0x03:
                        c.sendall(frame(0x11, self.rows))
                    elif t == 0x01:
                        self.bodies.append(body)
                        self.attached.set()
                        c.sendall(frame(0x10, b"FAKE-SCREEN\r\n"))
                        if self.mode == "drop":
                            time.sleep(0.3)
                            c.close()
                            return
                        if self.mode == "exit":
                            time.sleep(0.3)
                            c.sendall(frame(0x12, {"code": 0}))
                            c.shutdown(socket.SHUT_WR)
                            return
                        if self.mode == "taken":
                            time.sleep(0.3)
                            c.sendall(frame(0x13, TAKEN))
                            c.shutdown(socket.SHUT_WR)
                            while c.recv(65536):
                                pass
                            return
                        if self.mode == "errrst":
                            c.sendall(frame(0x13, {"message": "bye-why"}))
                            time.sleep(0.3)
                            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                         struct.pack("ii", 1, 0))
                            c.close()
                            return
        except OSError:
            return

    def close(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


# dch's exits for a control verb on a remote host (#114): 0-3 the remote
# dch's own, 1 refused (uncoded) or a bad request, 4 refused (coded), 6 the
# host lacks the op, 7 unreachable
UNSUPPORTED_EXIT, UNREACHABLE_EXIT = 6, 7


def ctl_checks(dch, check, senv, cenv, serve_at, procs, fakes, tmp, hosts):

    """#114: each control verb on srv (a dch serve with --allow-create)
    prints and exits exactly as the same verb run there locally: the local
    one on session ctl-qa, the remote one on ctl-qb, read as ctl-qb."""
    slog = os.path.join(tmp, "serve-ctl.log")
    port = serve_at(slog, "--allow-create")
    check("a dch serve --allow-create for the control verbs",
          port is not None)
    if port is None:
        return
    H = ["--hosts", "srv=127.0.0.1:%d" % port]
    wd = os.path.join(tmp, "wd")
    os.makedirs(wd)

    def same(label, *args, a="ctl-qa", b="ctl-qb", settle=0):
        loc = dch(senv, *[x.replace("@", a) for x in args])
        time.sleep(settle)
        # a verb with no name (--history) is remote by --host alone
        rem = dch(cenv, *H, *([] if "@" in args else ["--host", "srv"]),
                  *[x.replace("@", "srv:" + b) for x in args])
        got = (rem.stdout, rem.stderr, rem.returncode)
        want = (loc.stdout.replace(a, b), loc.stderr.replace(a, b),
                loc.returncode)
        check("ctl %s: byte for byte the local verb's (exit %d)"
              % (label, loc.returncode), got == want, (want, got))
        return rem

    spawn = ["--spawn", "@", "--size", "20x2", "--cwd", wd, "--",
             "/bin/sh", "-c", "pwd >> wd.txt; exec /bin/cat"]
    same("--spawn --cwd", *spawn)
    time.sleep(0.5)
    with open(os.path.join(wd, "wd.txt")) as f:
        check("--cwd: both sessions started in it",
              f.read().split() == [os.path.realpath(wd)] * 2)
    same("--send -- text", "--send", "@", "--", "say", "-f", "--config")
    same("--run", "--run", "@", "printf", "x")
    same("--keys", "--keys", "@", "Enter", "Tab")
    same("--keys with a bad key", "--keys", "@", "C-a")
    time.sleep(0.5)
    same("--read", "--read", "@")
    same("--read --ansi --cursor", "--read", "@", "--ansi", "--cursor")
    same("--read --recent 5", "--read", "@", "--recent", "5")
    same("--read --recent --cursor (dch refuses)", "--read", "@",
         "--recent", "--cursor")
    # "working" lasts a moment past the last output: both idle before the pair
    for _ in range(100):
        if (dch(senv, "--status", "ctl-qa").stdout,
                dch(senv, "--status", "ctl-qb").stdout) == ("idle\n",) * 2:
            break
        time.sleep(0.1)
    same("--status", "--status", "@")
    same("-m", "-m", "@", "nick")
    same("--pin --json", "--pin", "@", "--json")
    same("--unpin --json", "--unpin", "@", "--json")
    same("--say --json --", "--say", "@", "--json", "--", "hello")
    same("-d", "-d", "@")
    same("--history --json", "--history", "--json")
    same("--status of no session", "--status", "@", a="nx", b="nx")
    same("--stop --json", "--stop", "@", "--json")
    same("-k of a stopped session", "-k", "@")
    same("--forget --json", "--forget", "@", "--json")
    same("--revive of no session", "--revive", "@", "--json", a="nx", b="nx")
    rem = dch(cenv, "--host", "srv", *H, "--status", "ctl-qb")
    check("--host srv --status <name>: the unambiguous form",
          rem.returncode == 1 and rem.stderr == "no session: ctl-qb\n", rem)

    # --follow: frames as they come, the exit when the session ends
    for n in ("fa", "fb"):
        dch(senv, "--spawn", n, "--size", "20x2", "--", "/bin/sh", "-c",
            "sleep 1.5")
    pl = subprocess.Popen([DCH, "--follow", "fa"], env=senv,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL)
    pr = subprocess.Popen([DCH] + H + ["--follow", "srv:fb"], env=cenv,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL)
    lo, le = pl.communicate(timeout=20)
    ro, re_ = pr.communicate(timeout=20)
    mirror = dch(cenv, "--build").stdout.strip() == "full"
    check("ctl --follow: the local verb's frames, its exit at the end",
          (ro, re_, pr.returncode) == (lo, le, pl.returncode)
          and (not mirror or (pr.returncode == 0
                              and ro.startswith(b"dch-frame"))),
          ((lo, le, pl.returncode), (ro, re_, pr.returncode)))

    # a reader that closes our stdout ends a remote --follow with 0
    dch(senv, "--spawn", "fc", "--size", "20x2", "--", "/bin/cat")
    if mirror:
        pr = subprocess.Popen([DCH] + H + ["--follow", "srv:fc"], env=cenv,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL)
        first = pr.stdout.read(9)
        pr.stdout.close()
        dch(senv, "--send", "fc", "--", "more")
        try:
            rc = pr.wait(10)
        except subprocess.TimeoutExpired:
            pr.kill()
            rc = pr.wait()
        check("ctl --follow into a closed pipe: exit 0, as the local one",
              first == b"dch-frame" and rc == 0, (first, rc))
        # #118: Ctrl-C cancels it on the host (a 0x34), then dies of SIGINT
        pr = subprocess.Popen([DCH] + H + ["--follow", "srv:fc"], env=cenv,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL)
        first = pr.stdout.read(9)
        pr.send_signal(signal.SIGINT)
        try:
            _, err = pr.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pr.kill()
            _, err = pr.communicate()
        with open(slog) as f:
            cut = f.read().count("ctl cut short: cancel")
        check("ctl --follow, SIGINT: a 0x34 each (closed pipe too), "
              "then the signal's death, quietly",
              first == b"dch-frame" and pr.returncode == -signal.SIGINT
              and err == b"" and cut == 2, (first, pr.returncode, err, cut))
    dch(senv, "-k", "fc")

    # what the client refuses itself, and what the host refuses
    r = dch(cenv, *H, "--read", "srv:ctl-qa", "-f")
    check("off the allowlist: refused here, exit 1", r.returncode == 1
          and r.stderr == "dch: can't run on a remote host: -f is not "
          "allowed\n", r)
    # #120: --xray is argv[1] only, so after --hosts it is never sent;
    # dch --xray srv:<n> goes to the host (#157, xray_checks)
    r = dch(cenv, *H, "--xray", "srv:ctl-qa")
    check("--xray is no control verb here", r.returncode == 2
          and "--xray comes first" in r.stderr, r)
    r = dch(cenv, *H, "--read", "srv:ctl-qa", "--cwd", "/")
    check("--cwd with a verb but --spawn: a usage error", r.returncode == 1
          and "--cwd only works with --spawn" in r.stderr, r)
    r = dch(cenv, *H, "--cwd", "/nonexistent-dch-114", "--spawn", "srv:cz",
            "--", "/bin/cat")
    check("the host refusing the request: dch: srv: why, exit 1",
          r.returncode == 1 and r.stderr == "dch: srv: no such directory\n",
          r)
    r = dch(cenv, "--cwd", "/nonexistent-dch-114", "--spawn", "lz", "--",
            "/bin/cat")
    check("--cwd for a local --spawn must be a directory",
          r.returncode == 1 and "no such directory" in r.stderr, r)
    r = dch(cenv, *H, "--read", "srv:ctl-qa", "nope:cb")
    check("names on two hosts: refused", r.returncode == 1, r)
    r = dch(cenv, "--hosts", hosts, "--spawn", "srv:cz", "--", "/bin/cat")
    check("--spawn on a host without --allow-create: exit %d"
          % UNSUPPORTED_EXIT, r.returncode == UNSUPPORTED_EXIT
          and r.stderr == "dch: srv: does not support --spawn (needs dch "
          "serve --allow-create)\n", r)
    old = Fake("list", proto="1.0")
    fakes.append(old)
    r = dch(cenv, "--hosts", "q=127.0.0.1:%d" % old.port, "--read", "q:x")
    check("a host whose hello lacks ctl: exit %d" % UNSUPPORTED_EXIT,
          r.returncode == UNSUPPORTED_EXIT and r.stderr == "dch: q: does "
          "not support --read (needs dch serve 1.41+)\n" and not old.bodies,
          r)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    r = dch(cenv, "--hosts", "q=127.0.0.1:%d" % dead, "--status", "q:x")
    check("a closed port: unreachable, exit %d" % UNREACHABLE_EXIT,
          r.returncode == UNREACHABLE_EXIT
          and r.stderr.startswith("dch: q: unreachable: "), r)
    UNREADABLE = "dch: q: --status stopped by the host (unreadable)"
    for mode, want, err in (
            ("refuse", REFUSED_EXIT, "dch: q refused: " + REFUSAL["message"]),
            ("ctlcoded", REFUSED_EXIT,
             "dch: q refused: " + REFUSAL["message"]),
            ("ctlerr", 1, "dch: q: nope-why"),
            ("ctldrop", UNREACHABLE_EXIT,
             "dch: q: unreachable: the connection was lost"),
            ("ctlcut", 2, "err\ndch: q: --status stopped by the host (cap)"),
            ("ctlwhy", 2, "dch: q: --status stopped by the host (%s)"
             % ("\\x01" * 23)),
            ("ctlwhylong", 2, UNREADABLE),
            ("ctlwhynul", 2, UNREADABLE),
            ("ctlwhynum", 2, UNREADABLE),
            ("ctlbig", 1, UNREADABLE)):
        f = Fake(mode, ops=["ctl", "watch"])
        fakes.append(f)
        r = dch(cenv, "--hosts", "q=127.0.0.1:%d" % f.port, "--status",
                "q:x")
        check("a host that answers %s: exit %d" % (mode, want),
              r.returncode == want and r.stderr == err + "\n"
              and not re.search(r"[\x00-\x09\x0b-\x1f\x7f]", r.stderr)
              and r.stdout == {"ctlcut": "part\n",
                               "ctldrop": "half"}.get(mode, ""), r)
    body = json.loads(f.bodies[-1])
    check("the 0x30 is the argv as typed, the host: off",
          body == {"argv": ["--status", "x"]}, body)
    dch(senv, "-k", "ctl-qa")
    dch(senv, "-k", "ctl-qb")


def xray_checks(dch, check, senv, cenv, port, fakes, tmp, slog):
    """#157 (spec #832 4.1-4.2, 9.5 Remote): every --xray form on srv:<n>
    goes over ctl to srv's dch serve (no --allow-create) and prints and
    exits exactly as the same argv run there; 3.1's value rules are checked
    here first, nothing sent; a host without "xray" in its ops is exit 6
    with nothing sent, a refusal 4, an unreachable host 7; Ctrl-C, SIGTERM
    and a closed stdout end the host's dch. dch --xray takes no --hosts
    (argv[1] only), so the client names srv in remote_hosts."""
    def side(hosts):
        cfg = os.path.join(tmp, "xcfg-%d" % len(fakes))
        with open(cfg, "w") as f:
            f.write("remote_hosts = %s\n" % hosts)
        e = dict(cenv, DCH_CONFIG=cfg)
        e.pop("DCH_NO_REMOTE", None)
        return e

    xenv = side("srv=127.0.0.1:%d" % port)
    elapsed = re.compile(r'"elapsed_ms":\d+')

    def ctls():
        with open(slog) as f:
            return f.read().count(" ctl --xray ")

    def same(label, *args, n="xr1", mask=False):
        """args with "@" for the name: n there, srv:n here."""
        t0 = int(time.time())
        loc = dch(senv, "--xray", *[n if a == "@" else a for a in args])
        rem = dch(xenv, "--xray", *["srv:" + n if a == "@" else a
                                    for a in args])
        secs = int(time.time()) - t0
        got = (rem.stdout, rem.stderr, rem.returncode)
        want = (loc.stdout, loc.stderr, loc.returncode)
        if mask:
            # progress lines are timing (one per 250 ms of scan), as in
            # xray_golden.py: under load one run crosses 250 ms, the other not
            got, want = [("".join(x for x in elapsed.sub(
                              '"elapsed_ms":0', o).splitlines(True)
                              if not x.startswith('{"schema":1,"progress":')),)
                         + g[1:] for o, g in ((got[0], got), (want[0], want))]
            got = ("".join(read_clock_at(want[0].splitlines(True),
                                         got[0].splitlines(True), secs)),
                   ) + got[1:]
        check("xray %s: byte for byte the host's own run (exit %d)"
              % (label, loc.returncode), got == want, (want, got))

    dch(senv, "--spawn", "xr1", "--size", "60x6", "--", "/bin/sh")
    dch(senv, "--send", "xr1", "--", "echo see #831 and o/r#7")
    time.sleep(0.8)
    n0 = ctls()
    same("--json", "@", "--json")
    same("--json, flags first", "--json", "@")
    same("--ref --scope session", "@", "--ref", "#831", "--scope",
         "session", "--json", mask=True)
    same("--ref --full --budget-ms", "--json", "--full", "--ref", "o/r#7",
         "@", "--budget-ms", "50", "--scope", "session", mask=True)
    same("--ref --scope project", "@", "--ref", "#831", "--scope",
         "project", "--json", mask=True)
    same("--ref --scope all", "@", "--ref", "#831", "--scope", "all",
         "--json", mask=True)
    same("--chapter", "@", "--chapter", "s1", "--json")
    same("--clear", "@", "--clear", "--json")
    same("a Claude session UUID", "@", "--json",
         n="9f9f9f9f-0000-4000-8000-000000000000")
    same("a <file>.json record name", "@", "--json", n="nosuch.json")
    same("no such session", "@", "--json", n="xr-nope")
    check("xray: each remote form ran one ctl on the host",
          ctls() - n0 == 11, ctls() - n0)

    # #168: forms that answer with content on the host, not only errors.
    # The host's store gets History Claude sessions: xrs0 and xrs1 in one
    # project dir (a real dir on this host, with a file to resolve), xrs2
    # in another. srv:<n> must print exactly what the host prints.
    cfg = os.path.join(senv["HOME"], ".claude")
    sockdir = os.path.join(senv["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid())
    hdir = os.path.join(senv["XDG_STATE_HOME"], "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    proj = os.path.join(tmp, "xproj")
    os.makedirs(os.path.join(proj, "src"))
    with open(os.path.join(proj, "src", "w.c"), "w") as f:
        f.write("".join("line %d\n" % k for k in range(1, 41)))

    def hist(k, cwd, prompts):
        sid = "33333333-4444-4555-8666-%012d" % k
        pdir = os.path.join(cfg, "projects", cwd.replace("/", "-"))
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
            for t in prompts:   # a reply after each: one chapter a prompt
                f.write(json.dumps({"type": "user", "cwd": cwd,
                                    "message": {"content": t}}) + "\n")
                f.write(json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text", "text": "done"}]}}) + "\n")
        with open(os.path.join(hdir, "xrs%d.json" % k), "w") as f:
            f.write(json.dumps({
                "name": "xrs%d" % k, "cwd": cwd, "end": "exited",
                "argv": ["claude"], "started": 1794299400,
                "updated": 1794300000 + k, "ended": 1794300000 + k,
                "sock_dir": sockdir,
                "agent": {"session_id": sid, "cwd": cwd,
                          "config_dir": cfg}}) + "\n")

    hist(0, proj, ["fix #931 in src/w.c:20", "then o/r#7"])
    hist(1, proj, ["older #931"])
    hist(2, "/home/demo/elsewhere", ["far #931"])
    loc = dch(senv, "--xray", "xrs0", "--json")
    o = json.loads(loc.stdout) if loc.returncode == 0 else {}
    anchors = [c["anchor"] for c in o.get("chapters", [])]
    check("xray parity setup: xrs0 has two chapters on the host",
          len(anchors) == 2, (loc.returncode, loc.stdout, loc.stderr))
    n0 = ctls()
    for a in anchors:
        loc = dch(senv, "--xray", "xrs0", "--chapter", a, "--json")
        check("xray --chapter %s on the host: its entries" % a,
              loc.returncode == 0 and '"entries":[{"role":"user"'
              in loc.stdout, loc)
        same("--chapter <a real anchor>", "@", "--chapter", a, "--json",
             n="xrs0")
    loc = dch(senv, "--xray", "xrs0", "--ref", "#931", "--scope", "all",
              "--full", "--json")
    tal = [json.loads(l)["tally"]["session"] for l in loc.stdout.splitlines()
           if '"tally"' in l]
    check("xray --ref --scope all on the host: xrs0, its project, then the "
          "other dir", loc.returncode == 0 and tal == ["xrs0", "xrs1", "xrs2"],
          (tal, loc.stderr))
    same("--ref --scope all --full, other sessions' transcripts", "@",
         "--ref", "#931", "--scope", "all", "--full", "--json", n="xrs0",
         mask=True)
    same("--ref --scope all, light", "@", "--ref", "#931", "--scope", "all",
         "--json", n="xrs0", mask=True)
    # a dash-led anchor is --chapter's value on the host, never a flag
    for a in ("-f", "--config", "--json"):
        loc = dch(senv, "--xray", "xrs0", "--chapter", a, "--json")
        check("xray --chapter %s on the host: read as the anchor (chapter "
              "gone)" % a, loc.returncode == 1 and
              loc.stderr == "dch: chapter gone\n", loc)
        same("--chapter %s (a dash-led anchor)" % a, "@", "--chapter", a,
             "--json", n="xrs0")
    # --ref --full resolves a file on the host: its resolved line, first
    loc = dch(senv, "--xray", "xrs0", "--ref", "src/w.c:20", "--scope",
              "session", "--full", "--json")
    rv = [json.loads(l)["resolved"] for l in loc.stdout.splitlines()
          if '"resolved"' in l]
    check("xray --ref <file> --full on the host: a resolved line, ok, "
          "first 14", loc.returncode == 0 and len(rv) == 1 and
          rv[0]["kind"] == "file" and rv[0]["state"] == "ok" and
          rv[0].get("first") == 14 and rv[0]["lines"][6] == "line 20",
          (rv, loc.stderr))
    same("--ref <file> --full, the resolved line", "@", "--ref",
         "src/w.c:20", "--scope", "session", "--full", "--json", n="xrs0",
         mask=True)
    # #176: at --scope all the resolved line follows xrs0's own tally,
    # before the other sessions' lines; srv:xrs0 carries the same order
    loc = dch(senv, "--xray", "xrs0", "--ref", "src/w.c:20", "--scope",
              "all", "--full", "--json")
    seq = [k for l in loc.stdout.splitlines() for k in
           (json.loads(l),) if "progress" not in k]
    seq = ["resolved" if "resolved" in k else "done" if "done" in k else
           k["tally"]["session"] if "tally" in k else list(k)[1]
           for k in seq]
    check("xray --ref <file> --scope all --full on the host: resolved right "
          "after xrs0's tally, before xrs1 and xrs2",
          loc.returncode == 0 and seq[seq.index("resolved") - 1:] ==
          ["xrs0", "resolved", "xrs1", "xrs2", "done"], (seq, loc.stderr))
    same("--ref <file> --scope all --full, the resolved line's order", "@",
         "--ref", "src/w.c:20", "--scope", "all", "--full", "--json",
         n="xrs0", mask=True)
    check("xray parity: each remote form ran one ctl on the host",
          ctls() - n0 == 9, ctls() - n0)

    # QA 1.55.0 LOW-7: the host's JSON escapes U+061C, U+2028 and U+2029
    # as \u, so its lines pass --requests as objects, byte for byte. A
    # title ends at U+2028 (a newline), so the title carries U+061C and
    # the chapter's entry all three.
    hist(3, proj, ["see\u061c it a\u2028b\u2029c"])
    loc = dch(senv, "--xray", "xrs3", "--json")
    o = json.loads(loc.stdout) if loc.returncode == 0 else {}
    ch = o.get("chapters", [])
    check("xray xrs3 on the host: U+061C in its title, written \\u061c",
          len(ch) == 1 and ch[0]["title"] == "see\u061c it a"
          and "\\u061c" in loc.stdout and "\u061c" not in loc.stdout,
          (loc.returncode, loc.stdout[:400], loc.stderr))
    rq = Requests(xenv, "srv:xrs3")
    rq.line()
    for req, argv, esc in (
            (1, ["--json"], [b"\\u061c"]),
            (2, ["--chapter", ch[0]["anchor"] if ch else "s1", "--json"],
             [b"\\u061c", b"\\u2028", b"\\u2029"])):
        lr = subprocess.run([DCH, "--xray", "xrs3"] + argv, env=senv,
                            capture_output=True, stdin=subprocess.DEVNULL)
        rq.send({"req": req, "argv": argv})
        got = rq.until(req)
        want = [b'{"req":%d,"line":%s}' % (req, x)
                for x in lr.stdout.splitlines()]
        check("requests %s on xrs3: U+061C/U+2028/U+2029 \\u-escaped, "
              "the host's lines as objects, byte for byte" % " ".join(argv),
              lr.returncode == 0 and all(e in lr.stdout for e in esc)
              and got == want + [b'{"req":%d,"exit":0,"why":null}' % req],
              (want, got))
    rq.end()

    # #184: a project in <repo>/.claude/worktrees/<name> (a Claude Code
    # worktree, its .git a file) reads its own a.txt and refuses its .env,
    # on the host and as srv:xrs4
    wt = os.path.join(tmp, "wrepo", ".claude", "worktrees", "w")
    os.makedirs(os.path.join(tmp, "wrepo", ".git"))
    os.makedirs(wt)
    for name, body in ((".git", "gitdir: ../../../.git/wt/w\n"),
                       ("a.txt", "FAKE A\n"), (".env", "FAKE=1\n")):
        with open(os.path.join(wt, name), "w") as f:
            f.write(body)
    hist(4, wt, ["read a.txt:1"])
    for key, state in (("a.txt:1", "ok"),
                       (os.path.join(wt, ".env") + ":1", "secret_name")):
        loc = dch(senv, "--xray", "xrs4", "--ref", key, "--scope",
                  "session", "--full", "--json")
        rv = [json.loads(l)["resolved"] for l in loc.stdout.splitlines()
              if '"resolved"' in l]
        check("xray --ref %s --full in a .claude/worktrees project on the "
              "host: %s" % (os.path.basename(key), state),
              loc.returncode == 0 and len(rv) == 1 and
              rv[0]["state"] == state and (rv[0].get("lines") == ["FAKE A"]
              if state == "ok" else not rv[0].get("lines") and
              "FAKE" not in loc.stdout), (rv, loc.stderr))
        same("--ref <file> --full in a .claude/worktrees project (%s)"
             % state, "@", "--ref", key, "--scope", "session", "--full",
             "--json", n="xrs4", mask=True)

    # 3.1's value rules, here first: the local run's line, nothing sent
    n0 = ctls()
    for a, line in ((["--ref", "#1\x01"], "dch: bad ref"),
                    (["--ref", "#1\u202e"], "dch: bad ref"),
                    (["--ref", "x"], "dch: not a ref"),
                    (["--chapter", "a/b"], "dch: bad anchor"),
                    (["--ref", "#831", "--scope", "none"], "dch: bad scope"),
                    (["--ref", "#831", "--budget-ms", "0x40"],
                     "dch: bad budget"),
                    (["--json"], "dch: bad usage: --json twice")):
        r = dch(xenv, "--xray", "srv:xr1", "--json", *a)
        check("xray value rule %r: %s, exit 2, nothing sent" % (a, line),
              r.returncode == 2 and r.stderr == line + "\n", r)
    r = dch(xenv, "--xray", "srv:xr1")
    check("xray view form on srv:xr1 with no terminal: exit 2, nothing sent "
          "(the view itself: xray_remote_view_test.py)",
          r.returncode == 2 and r.stderr == "dch: the --xray view needs a "
          "terminal (--json prints the object)\n", r)
    check("xray: no value-rule case reached the host", ctls() == n0)

    # --follow: the snapshot as the host's own; Ctrl-C and SIGTERM cancel
    # it there (0x34), a closed stdout ends it with 0
    pl = subprocess.Popen([DCH, "--xray", "xr1", "--follow", "--json"],
                          env=senv, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    a = pl.stdout.readline()
    pl.terminate()
    pl.communicate(timeout=10)
    mirror = dch(cenv, "--build").stdout.strip() == "full"
    for sig in (signal.SIGINT, signal.SIGTERM, None):
        if not sig and not mirror:
            # a closed stdout shows at the next line written, and a lite
            # shell's --follow writes none after its snapshot
            print("  skip xray --follow (stdout closed): the shell writes "
                  "no delta (build has no terminal mirror)")
            continue
        with open(slog) as f:
            cut0 = f.read().count("ctl cut short: cancel")
        pr = subprocess.Popen([DCH, "--xray", "srv:xr1", "--follow",
                               "--json"], env=xenv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL)
        b = pr.stdout.readline()
        if sig:
            pr.send_signal(sig)
        else:
            pr.stdout.close()
            # a new ref is a delta line: its write meets the EPIPE
            dch(senv, "--send", "xr1", "--", "echo more o/r#11")
        try:
            pr.wait(10)
        except subprocess.TimeoutExpired:
            pr.kill()
            pr.wait()
        err = pr.stderr.read()
        time.sleep(0.3)
        with open(slog) as f:
            cut = f.read().count("ctl cut short: cancel") - cut0
        check("xray --follow (%s): the host's snapshot line, then %s"
              % (sig and sig.name or "stdout closed",
                 "its death, quietly, the host's dch cancelled" if sig
                 else "exit 0"),
              a.startswith(b'{"schema":1,"snapshot":') and b == a
              and err == b"" and cut == 1
              and pr.returncode == (-sig if sig else 0),
              (a[:60], b[:60], err, cut, pr.returncode))
    dch(senv, "-k", "xr1")

    # ctl's own exits: 6 without "xray" (nothing sent), 4, 7
    nox = Fake("ctlerr", ops=["ctl", "ctl-cancel", "watch"])
    fakes.append(nox)
    r = dch(side("q=127.0.0.1:%d" % nox.port), "--xray", "q:x", "--json")
    check("xray on a host whose ops lack xray: exit %d, nothing sent"
          % UNSUPPORTED_EXIT, r.returncode == UNSUPPORTED_EXIT
          and r.stderr == "dch: q: does not support --xray (needs dch "
          "serve 1.55.0+)\n" and not nox.bodies, r)
    for mode, want, err in (
            ("refuse", REFUSED_EXIT, "dch: q refused: " + REFUSAL["message"]),
            ("ctlcoded", REFUSED_EXIT,
             "dch: q refused: " + REFUSAL["message"]),
            ("ctlerr", 1, "dch: q: nope-why"),
            ("ctldrop", UNREACHABLE_EXIT,
             "dch: q: unreachable: the connection was lost")):
        f = Fake(mode, ops=["ctl", "ctl-cancel", "watch", "xray"])
        fakes.append(f)
        r = dch(side("q=127.0.0.1:%d" % f.port), "--xray", "q:x",
                "--follow", "--json")
        check("xray on a host that answers %s: exit %d" % (mode, want),
              r.returncode == want and r.stderr == err + "\n", r)
    body = json.loads(f.bodies[-1])
    check("xray: the 0x30 is --xray <name> first, the host: off",
          body == {"argv": ["--xray", "x", "--follow", "--json"]}, body)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    r = dch(side("q=127.0.0.1:%d" % dead), "--xray", "q:x", "--json")
    check("xray on a closed port: unreachable, exit %d" % UNREACHABLE_EXIT,
          r.returncode == UNREACHABLE_EXIT
          and r.stderr.startswith("dch: q: unreachable: "), r)


class Proxy:
    """#158: a loopback relay to a dch serve, frame by frame. "nocancel"
    takes "ctl-cancel" out of the hello's ops; "cut" closes the first
    connection at its first 0x30 (a lost link); "hold" swallows the first
    connection's 0x30s and 0x34s (a request that never ends); "lines" sends
    `lines` in place of the first connection's first 0x31, as frames of
    type `lt` of 1 MiB at most, and drops its other 0x31s (a host's own
    stdout or stderr bytes). #231: "slow07" passes each 0x07 (a redraw's
    re-arm) `delay` seconds late, a slow link; "no07" drops them, a host
    that never re-arms."""

    def __init__(self, up, mode, lines=b"", lt=0x31, delay=0.0):
        self.up, self.mode, self.n, self.lines = up, mode, 0, lines
        self.lt, self.delay = lt, delay
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
                u = socket.create_connection(("127.0.0.1", self.up))
            except OSError:
                return
            self.n += 1
            self.conns += [c, u]
            for a, b, up in ((c, u, True), (u, c, False)):
                threading.Thread(target=self.pipe, args=(a, b, up,
                                                         self.n == 1),
                                 daemon=True).start()

    def pipe(self, a, b, up, first):
        buf, sent = b"", False
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    b.shutdown(socket.SHUT_WR)
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if up and first and t == 0x30 and self.mode == "cut":
                        a.close()
                        b.close()
                        return
                    if up and first and t in (0x30, 0x34) \
                            and self.mode == "hold":
                        continue
                    if up and t == 0x07 and self.mode == "no07":
                        continue
                    if up and t == 0x07 and self.mode == "slow07":
                        time.sleep(self.delay)
                    if not up and t == 0x18 and self.mode == "nocancel":
                        h = json.loads(body)
                        h["ops"] = [o for o in h["ops"] if o != "ctl-cancel"]
                        body = json.dumps(h).encode()
                    if not up and first and t == 0x31 \
                            and self.mode == "lines":
                        if not sent:
                            for i in range(0, len(self.lines), 1 << 20):
                                b.sendall(frame(self.lt,
                                                self.lines[i:i + (1 << 20)]))
                        sent = True
                        continue
                    b.sendall(frame(t, body))
        except OSError:
            return

    def close(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


class Requests:
    """#158: one `dch --xray <n> --requests --json`, its stdin and stdout
    as NDJSON lines, raw bytes kept for the byte-exact checks."""

    def __init__(self, env, n):
        self.p = subprocess.Popen([DCH, "--xray", n, "--requests", "--json"],
                                  env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.buf = b""

    def send(self, *lines):
        try:
            self.p.stdin.write(b"".join(
                (x if isinstance(x, bytes) else json.dumps(x).encode())
                + b"\n" for x in lines))
            self.p.stdin.flush()
        except BrokenPipeError:
            pass	# it has gone: the checks say so

    def line(self, secs=15):
        """the next stdout line (no newline), or None"""
        end = time.time() + secs
        fd = self.p.stdout.fileno()
        while b"\n" not in self.buf and time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                d = os.read(fd, 65536)
                if not d:
                    break
                self.buf += d
        if b"\n" not in self.buf:
            return None
        ln, self.buf = self.buf.split(b"\n", 1)
        return ln

    def until(self, req, secs=15):
        """every line up to req's end line (its "exit")"""
        out = []
        while True:
            ln = self.line(secs)
            if ln is None:
                return out
            out.append(ln)
            j = json.loads(ln)
            if j.get("req") == req and "exit" in j:
                return out

    def quiet(self, secs=0.5):
        """nothing more on stdout within secs"""
        return self.line(secs) is None

    def end(self, secs=15):
        """stdin closed: (exit, the rest of stdout, stderr)"""
        try:
            self.p.stdin.close()
        except BrokenPipeError:
            pass
        self.p.stdin = None	# communicate() would flush it
        try:
            out, err = self.p.communicate(timeout=secs)
        except subprocess.TimeoutExpired:
            self.p.kill()
            out, err = self.p.communicate()
        return self.p.returncode, self.buf + out, err


def requests_checks(dch, check, senv, cenv, serve_at, fakes, tmp, slog):
    """#158 (spec #832 4.3): dch --xray srv:<n> --requests --json holds one
    warm ctl connection to a dch serve on loopback. The ready line carries
    the host's ops; each {"req":N,"argv":[...]} on stdin runs on the host,
    its stdout lines wrapped byte for byte, its stderr escaped, one end line
    each; the argv is checked here first (3.1's value rules, the request
    forms only), refusals sent nowhere; one at a time (busy); {"cancel":N}
    is a 0x34, or without "ctl-cancel" the close and a quiet re-dial; an
    idle close or a lost link re-dials, the request in flight ends exit 7
    "lost"; stdin EOF exits 0; a local name exits 2; a host without "xray"
    gets one exit 6 line."""
    cfgs = []

    def side(hosts):
        cfg = os.path.join(tmp, "rcfg-%d" % len(cfgs))
        cfgs.append(cfg)
        with open(cfg, "w") as f:
            f.write("remote_hosts = %s\n" % hosts)
        e = dict(cenv, DCH_CONFIG=cfg)
        e.pop("DCH_NO_REMOTE", None)
        return e

    def count(what, log=slog):
        with open(log) as f:
            return f.read().count(what)

    def ctls(log=slog):
        return count(" ctl --xray ", log)

    def conns(log=slog):
        return count("let in by the test-only bypass", log)

    port = int(re.search(r"listening on 127\.0\.0\.1:(\d+)",
                         open(slog).read()).group(1))
    xenv = side("srv=127.0.0.1:%d" % port)
    elapsed = re.compile(rb'"elapsed_ms":\d+')
    dch(senv, "--spawn", "xq1", "--size", "60x6", "--", "/bin/sh")
    dch(senv, "--send", "xq1", "--", "echo see #831 and o/r#7")
    time.sleep(0.8)

    rq = Requests(xenv, "srv:xq1")
    ready = rq.line()
    check("requests: the ready line carries the host's ops",
          ready == b'{"schema":1,"ready":{"ops":["ctl","ctl-cancel",'
          b'"watch","xray"]}}', ready)
    c0 = conns()

    def same(req, argv, mask=False):
        """argv on the warm connection, as the host's own run of it"""
        t0 = int(time.time())
        loc = subprocess.run([DCH, "--xray", "xq1"] + argv, env=senv,
                             capture_output=True, stdin=subprocess.DEVNULL)
        rq.send({"req": req, "argv": argv})
        got = rq.until(req)
        secs = int(time.time()) - t0
        lines = [g for g in got if g.startswith(b'{"req":%d,"line":' % req)]
        errs = [json.loads(g)["stderr"] for g in got if b'"stderr":' in g]
        want = [b'{"req":%d,"line":%s}' % (req, x)
                for x in loc.stdout.splitlines()]
        cmp = lines
        if mask:
            # progress lines are timing (see the xray same() above)
            prog = b'{"req":%d,"line":{"schema":1,"progress":' % req
            cmp = [elapsed.sub(b'"elapsed_ms":0', x) for x in lines
                   if not x.startswith(prog)]
            want = [elapsed.sub(b'"elapsed_ms":0', x) for x in want
                    if not x.startswith(prog)]
            cmp = read_clock_at(want, cmp, secs)
        end = json.loads(got[-1]) if got else None
        check("requests %s: each host line byte for byte, its stderr, one "
              "end line (exit %d)" % (" ".join(argv), loc.returncode),
              cmp == want and errs == loc.stderr.decode().splitlines()
              and end == {"req": req, "exit": loc.returncode, "why": None}
              and len(got) == len(lines) + len(errs) + 1,
              (want, loc.stderr, got))

    n0 = ctls()
    same(1, ["--json"])
    same(2, ["--ref", "#831", "--scope", "session", "--json"], mask=True)
    same(3, ["--json", "--full", "--ref", "o/r#7", "--budget-ms", "50",
             "--scope", "session"], mask=True)
    same(4, ["--chapter", "s1", "--json"])
    same(5, ["--clear", "--json"])
    check("requests: each ran one ctl, all on the one warm connection",
          ctls() - n0 == 5 and conns() == c0, (ctls() - n0, conns() - c0))

    # the argv checked here first: refused, nothing sent
    n0 = ctls()
    req = 10
    for argv, line in (
            (["--follow", "--json"],
             "dch: bad usage: --follow is not a request"),
            (["--requests", "--json"],
             "dch: bad usage: --requests is not allowed with --xray"),
            ([], "dch: bad usage: --xray needs --json (the view is "
             "client-side only)"),
            (["--ref", "#1\x01", "--json"], "dch: bad ref"),
            (["--ref", "#1‮", "--json"], "dch: bad ref"),
            (["--ref", "x", "--json"], "dch: not a ref"),
            (["--chapter", "a/b", "--json"], "dch: bad anchor"),
            (["--ref", "#831", "--scope", "none", "--json"],
             "dch: bad scope"),
            (["--ref", "#831", "--budget-ms", "0x40", "--json"],
             "dch: bad budget"),
            (["--json", "--json"], "dch: bad usage: --json twice"),
            (["--clear", "--full", "--json"], "dch: bad usage: --scope, "
             "--full and --budget-ms come with --ref"),
            (["xq2", "--json"], "dch: bad usage: unexpected xq2 after "
             "--xray"),
            (["--all", "--json"], "dch: bad usage: --all is not allowed")):
        req += 1
        rq.send({"req": req, "argv": argv})
        got = [json.loads(g) for g in rq.until(req)]
        check("requests refuse %r: %s, exit 2, refused" % (argv, line),
              got == [{"req": req, "stderr": line},
                      {"req": req, "exit": 2, "why": "refused"}], got)
    bad = "dch: bad request: send {\"req\":N,\"argv\":[...]} or " \
          "{\"cancel\":N}, one per line"
    for raw, rn in ((b"garbage", None),
                    (b'{"req":0,"argv":["--json"]}', None),
                    (b'{"req":2147483648,"argv":["--json"]}', None),
                    (b'{"req":-3,"argv":["--json"]}', None),
                    (b'{"req":1.5,"argv":["--json"]}', None),
                    (b'{"req":"7","argv":["--json"]}', None),
                    (b'{"req":40,"argv":"--json"}', 40),
                    (b'{"req":41,"argv":["--json",1]}', 41),
                    (b'{"req":42,"argv":["--json"],"x":1}', 42),
                    (b'{"req":43,"req":43,"argv":["--json"]}', None),
                    (b'{"req":44}', 44),
                    (b'{"cancel":45,"x":1}', None),
                    (b'{"req":46,"argv":["--json"]}\x00', None),
                    (b'[1]', None)):
        rq.send(raw)
        got = [json.loads(g) for g in rq.until(rn)]
        check("requests: a malformed line %r is refused as req %s"
              % (raw, rn), got == [{"req": rn, "stderr": bad},
                                   {"req": rn, "exit": 2,
                                    "why": "refused"}], got)
    rq.send(b"", b"  \r")
    rq.send(b'{"req":47,"argv":["--json"]}'.replace(b"--json", b"x" * 70000))
    got = [json.loads(g) for g in rq.until(None)]
    check("requests: blank lines are skipped, a line over 64 KiB refused",
          got == [{"req": None,
                   "stderr": "dch: bad request: a line over 64 KiB"},
                  {"req": None, "exit": 2, "why": "refused"}], got)
    check("requests: no refused request reached the host", ctls() == n0,
          ctls() - n0)

    # one at a time: a second while one runs is busy, the first goes on
    rq.send({"req": 50, "argv": ["--json"]}, {"req": 51, "argv": ["--json"]})
    a = rq.line()
    got = rq.until(50)
    check("requests: a second request while one runs: busy, exit 1",
          json.loads(a) == {"req": 51, "exit": 1, "why": "busy"}
          and json.loads(got[-1]) == {"req": 50, "exit": 0, "why": None}
          and len(got) == 2, (a, got))

    # cancel: a 0x34 ("ctl-cancel"), its 0x33 ends it; the connection stays
    cut0, n0 = count("ctl cut short: cancel"), ctls()
    rq.send({"req": 60, "argv": ["--json"]}, {"cancel": 60})
    got = [json.loads(g) for g in rq.until(60)]
    time.sleep(0.3)
    check("requests: {\"cancel\":N} is a 0x34, the host's 0x33 ends it",
          got == [{"req": 60, "exit": 2, "why": "cancel"}]
          and count("ctl cut short: cancel") - cut0 == 1, got)
    rq.send({"cancel": 60}, {"cancel": 99})
    check("requests: a cancel with nothing of its req running: nothing",
          rq.quiet())
    same(61, ["--json"])
    check("requests: after a cancel the same warm connection takes more",
          conns() == c0 and ctls() - n0 == 2, conns() - c0)
    rc, rest, err = rq.end()
    check("requests: stdin EOF closes the connection, exit 0",
          rc == 0 and rest == b"" and err == b"", (rc, rest, err))

    # without "ctl-cancel": the close and a quiet re-dial
    px = Proxy(port, "nocancel")
    fakes.append(px)
    rq = Requests(side("q=127.0.0.1:%d" % px.port), "q:xq1")
    ready = rq.line()
    rq.send({"req": 1, "argv": ["--json"]}, {"cancel": 1})
    got = [json.loads(g) for g in rq.until(1)]
    time.sleep(0.3)
    check("requests without ctl-cancel: a cancel closes and re-dials",
          b'"ctl-cancel"' not in ready
          and got == [{"req": 1, "exit": 2, "why": "cancel"}]
          and px.n == 2, (ready, got, px.n))
    rq.send({"req": 2, "argv": ["--json"]})
    got = [json.loads(g) for g in rq.until(2) if b'"exit"' in g]
    check("requests without ctl-cancel: the next request runs on the new "
          "connection", got == [{"req": 2, "exit": 0, "why": None}]
          and px.n == 2, (got, px.n))
    rq.end()

    # a lost link: the request in flight ends exit 7 "lost"; the next dials
    px = Proxy(port, "cut")
    fakes.append(px)
    rq = Requests(side("q=127.0.0.1:%d" % px.port), "q:xq1")
    rq.line()
    rq.send({"req": 1, "argv": ["--json"]})
    got = [json.loads(g) for g in rq.until(1)]
    check("requests: a lost link ends the request in flight exit 7 lost",
          got == [{"req": 1, "stderr": "dch: q: unreachable: the "
                   "connection was lost"},
                  {"req": 1, "exit": UNREACHABLE_EXIT, "why": "lost"}], got)
    rq.send({"req": 2, "argv": ["--json"]})
    got = rq.until(2)
    check("requests: after a lost link the next request re-dials",
          json.loads(got[-1]) == {"req": 2, "exit": 0, "why": None}
          and px.n == 2, (got, px.n))
    rq.end()

    # stdin EOF with one in flight: it ends "cancel", exit 0; a 0x33 that
    # never comes after the 0x34 is the close's cancel too
    px = Proxy(port, "hold")
    fakes.append(px)
    rq = Requests(side("q=127.0.0.1:%d" % px.port), "q:xq1")
    rq.line()
    rq.send({"req": 1, "argv": ["--json"]}, {"cancel": 1})
    t0 = time.time()
    got = [json.loads(g) for g in rq.until(1)]
    took = time.time() - t0
    end = time.time() + 10
    while px.n < 2 and time.time() < end:
        time.sleep(0.05)	# the quiet re-dial, after the end line (#226)
    check("requests: a 0x34 with no 0x33 in 3 s: the close cancels it",
          got == [{"req": 1, "exit": 2, "why": "cancel"}]
          and took > 2.5 and px.n == 2, (got, took, px.n))
    px.n = 0	# the next connection is "first" again: it holds
    time.sleep(0.2)
    rq2 = Requests(side("q=127.0.0.1:%d" % px.port), "q:xq1")
    rq2.line()
    rq2.send({"req": 3, "argv": ["--json"]})
    time.sleep(0.5)
    rc, rest, err = rq2.end()
    check("requests: stdin EOF with one in flight: it ends cancel, exit 0",
          rc == 0 and rest == b'{"req":3,"exit":2,"why":"cancel"}\n'
          and err == b"", (rc, rest, err))
    rq.end()

    # a host stdout line passes byte for byte only when it is one JSON
    # object any reader takes, also once wrapped a level deeper; else it is
    # a JSON string (raw bytes compared: Python's json stops short of 1024)
    def deep(d):
        return b'{"a":' * (d - 1) + b"{}" + b"}" * (d - 1)
    keep = [b'{"a":1}', b'{"a":"\\ud800","b":1e999}', deep(1023)]
    wrap = [b'{"a":"\xff"}', b'{"a":"\x01"}', b'{"a":1}x', b'{"a":1} {"b":2}',
            b'{"a":01}', b'{"a":1.}', b'{"a":.5}', b'{"a":+1}', b'{"a":-}',
            b'{"a":NaN}', b'{"a":tRUE}', b'{"a":1,}', b"{a:1}", b"{'a':1}",
            b'{"a":1}\x7f', b'{"a":"\x7f"}', b'{"a":"\xc2\x9b"}',
            b'{"a":"\xe2\x80\xae"}', b'{"a":1}\r', deep(1024)]
    px = Proxy(port, "lines", b"".join(x + b"\n" for x in keep + wrap))
    fakes.append(px)
    rq = Requests(side("q=127.0.0.1:%d" % px.port), "q:xq1")
    rq.line()
    rq.send({"req": 1, "argv": ["--json"]})
    got = [rq.line() for _ in keep + wrap + [0]]
    check("requests: a host object line passes byte for byte, escapes, "
          "numbers and depth 1023 as they are",
          got[:len(keep)] == [b'{"req":1,"line":%s}' % x for x in keep],
          got[:len(keep)])
    for x, g in zip(wrap, got[len(keep):]):
        check("requests: host line %r comes back as a JSON string" % x[:24],
              g is not None and g.startswith(b'{"req":1,"line":"')
              and isinstance(json.loads(g)["line"], str), g)
    check("requests: then the host's end line",
          got[-1] == b'{"req":1,"exit":0,"why":null}', got[-1])
    rq.end()

    # a host line past what dch carries (one over 8 MiB, or one it cannot
    # escape, about 2.67 MiB): the request in flight ends exit 2 "cap", the
    # close and a quiet re-dial; one queued behind it got "busy", the next
    # runs on the new connection, and stdin EOF is still exit 0
    for what, lt, body in (
            ("a 3 MiB stderr line", 0x32, b"e" * (3 << 20) + b"\n"),
            ("a 3 MiB stdout line, no object", 0x31, b"x" * (3 << 20) + b"\n"),
            ("a 9 MiB stdout object", 0x31,
             b'{"a":"' + b"y" * (9 << 20) + b'"}\n')):
        px = Proxy(port, "lines", body, lt)
        fakes.append(px)
        rq = Requests(side("q=127.0.0.1:%d" % px.port), "q:xq1")
        rq.line()
        rq.send({"req": 1, "argv": ["--json"]}, {"req": 2, "argv": ["--json"]})
        got = [json.loads(g) for g in rq.until(1, 30)]
        rq.send({"req": 3, "argv": ["--json"]})
        got3 = rq.until(3)
        rc, rest, err = rq.end()
        check("requests: %s: exit 2 cap, re-dialled; the next runs" % what,
              got == [{"req": 2, "exit": 1, "why": "busy"},
                      {"req": 1, "exit": 2, "why": "cap"}]
              and [json.loads(g).get("req") for g in got3] == [3] * len(got3)
              and got3[-1:] == [b'{"req":3,"exit":0,"why":null}']
              and px.n == 2 and rc == 0 and rest == b"" and err == b"",
              (got, got3[-1:], px.n, rc, rest[:80], err[:200]))

    # the host's idle close, and its cap: re-dialled, wrapped
    ilog = os.path.join(tmp, "serve-idle.log")
    iport = serve_at(ilog, DCH_CTL_IDLE_MS="400", DCH_CTL_CAP="64")
    check("a dch serve with a short idle and a small cap", iport is not None)
    if iport is not None:
        rq = Requests(side("i=127.0.0.1:%d" % iport), "i:xq1")
        rq.line()
        # lite: a shell has no corpus, so no --ref line; --json still cuts
        full = dch(cenv, "--build").stdout.strip() == "full"
        rq.send({"req": 1, "argv": ["--ref", "#831", "--scope", "session",
                                    "--json"] if full else ["--json"]})
        got = rq.until(1)
        check("requests: the host's cap: a cut line as a string, exit 2 cap",
              json.loads(got[-1]) == {"req": 1, "exit": 2, "why": "cap"}
              and all(g.startswith(b'{"req":1,"line":') for g in got[:-1])
              and isinstance(json.loads(got[-2])["line"], str), got)
        time.sleep(1.2)
        check("requests: the host's idle close: nothing on stdout",
              count("idle; closing", ilog) == 1 and rq.quiet(0.2))
        rq.send({"req": 2, "argv": ["--json"]})
        got = rq.until(2)
        check("requests: after an idle close the next request re-dials",
              json.loads(got[-1]) == {"req": 2, "exit": 2, "why": "cap"}
              and conns(ilog) == 2, (got, conns(ilog)))
        rq.end()
        loc = subprocess.run([DCH, "--xray", "xq-none", "--json"], env=senv,
                             capture_output=True, stdin=subprocess.DEVNULL)
        rq = Requests(side("i=127.0.0.1:%d" % iport), "i:xq-none")
        rq.line()
        rq.send({"req": 3, "argv": ["--json"]})
        got = [json.loads(g) for g in rq.until(3)]
        check("requests: the host's stderr a line of its own, its exit",
              got == [{"req": 3, "stderr": loc.stderr.decode().rstrip("\n")},
                      {"req": 3, "exit": loc.returncode, "why": None}]
              and loc.returncode == 1, (got, loc))
        rq.end()

    # #176: a wide --full read over --requests carries the host's order
    # (resolved right after xrs0's own file, before the others); a host cap
    # that cuts it after that line keeps the line: no done, exit 2 "cap",
    # over --requests and over the plain ctl path alike
    argv = ["--ref", "src/w.c:20", "--scope", "all", "--full", "--json"]
    loc = subprocess.run([DCH, "--xray", "xrs0"] + argv, env=senv,
                         capture_output=True, stdin=subprocess.DEVNULL)
    prog = re.compile(rb'^\{"schema":1,"progress":')

    def bare(lines):
        return [elapsed.sub(b'"elapsed_ms":0', x) for x in lines
                if not prog.match(x)]

    rq = Requests(xenv, "srv:xrs0")
    rq.line()
    rq.send({"req": 1, "argv": argv})
    got = rq.until(1)
    lines = [g[len(b'{"req":1,"line":'):-1] for g in got
             if g.startswith(b'{"req":1,"line":')]
    check("requests --ref <file> --scope all --full: the host's lines in "
          "its order, resolved before xrs1's tally",
          loc.returncode == 0 and bare(lines) ==
          bare(loc.stdout.splitlines()) and
          json.loads(got[-1]) == {"req": 1, "exit": 0, "why": None} and
          loc.stdout.index(b'"resolved"') <
          loc.stdout.index(b'"tally":{"session":"xrs1"'), (loc, got))
    rq.end()
    # the cap ends the stream inside xrs1's tally, the first other session
    out = loc.stdout
    t1 = out.find(b'{"schema":1,"tally":{"session":"xrs1"')
    clog = os.path.join(tmp, "serve-cap.log")
    cport = serve_at(clog, DCH_CTL_CAP=str(out.index(b"\n", t1)))
    check("a dch serve whose cap cuts the wide read in xrs1's tally",
          cport is not None and t1 > 0)
    if cport is not None:
        cenv2 = side("c=127.0.0.1:%d" % cport)
        rq = Requests(cenv2, "c:xrs0")
        rq.line()
        rq.send({"req": 1, "argv": argv})
        got = rq.until(1)
        objs = [json.loads(g)["line"] for g in got[:-1]]
        objs = [o for o in objs if isinstance(o, dict)]
        check("requests: a host cap in the other sessions: the resolved "
              "line, no done, exit 2 cap",
              json.loads(got[-1]) == {"req": 1, "exit": 2, "why": "cap"}
              and any("resolved" in o for o in objs)
              and not any("done" in o for o in objs), got)
        rq.end()
        r = subprocess.run([DCH, "--xray", "c:xrs0"] + argv, env=cenv2,
                           capture_output=True, stdin=subprocess.DEVNULL)
        check("xray c:xrs0: a host cap in the other sessions: the resolved "
              "line, no done, exit 2 (cap)", r.returncode == 2 and
              b'{"schema":1,"resolved":' in r.stdout and
              b'"done":true' not in r.stdout and
              r.stderr.endswith(b"stopped by the host (cap)\n"), r)

    # a local name, and the wrong flags: exit 2, nothing dialled
    for args, line in (
            (["xq1"], "dch: bad usage: --requests takes a remote "
             "host:name"),
            (["srv:xq1", "--ref", "#831"], "dch: bad usage: --requests "
             "takes host:name and --json, nothing else"),
            (["srv:xq1", "--follow"], "dch: bad usage: --requests takes "
             "host:name and --json, nothing else"),
            (["srv:xq1", "--requests"], "dch: bad usage: --requests "
             "twice")):
        r = dch(xenv, "--xray", "--requests", "--json", *args)
        check("requests %r: exit 2" % args, r.returncode == 2
              and r.stderr == line + "\n" and r.stdout == "", r)
    r = dch(xenv, "--xray", "srv:xq1", "--requests")
    check("requests without --json: exit 2", r.returncode == 2
          and "takes host:name and --json" in r.stderr, r)

    # no warm connection at all: one exit line, its status
    nox = Fake("ctlerr", ops=["ctl", "ctl-cancel", "watch"])
    fakes.append(nox)
    r = dch(side("q=127.0.0.1:%d" % nox.port), "--xray", "q:x",
            "--requests", "--json")
    msg = "dch: q: does not support --xray (needs dch serve 1.55.0+)"
    check("requests on a host whose ops lack xray: one exit 6 line",
          r.returncode == UNSUPPORTED_EXIT and r.stderr == msg + "\n"
          and json.loads(r.stdout) == {"schema": 1, "exit": 6,
                                       "stderr": msg}
          and r.stdout.count("\n") == 1 and not nox.bodies, r)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    r = dch(side("q=127.0.0.1:%d" % dead), "--xray", "q:x", "--requests",
            "--json")
    check("requests on a closed port: one exit 7 line",
          r.returncode == UNREACHABLE_EXIT
          and json.loads(r.stdout)["exit"] == UNREACHABLE_EXIT
          and r.stderr.startswith("dch: q: unreachable: "), r)
    dch(senv, "-k", "xq1")


# --- #180 (QA 1.55.0 LOW-6, LOW-8, LOW-9, LOW-10, NIT-4, NIT-8, NIT-9) -------

class XFake:
    """#180: a loopback host whose hello has ctl, ctl-cancel and xray, and
    whose 0x30 answer is `mode`'s: "silent" a partial stdout line, then
    nothing; "cut" whole lines, a partial stdout and stderr line, then the
    close with no 0x33; "nul" a NUL in
    a stdout and a stderr line; "order" stdout, stderr, stdout in one
    write; "osc" OSC 52 and title escapes on both streams; "hold" answers
    only a 0x34 (its 0x33 cancel); "dark" answers the first connection's
    0x30 and closes it, and every later connection gets no hello; "hist"
    answers `out` as stdout, exit 0 (a --history --json); #197: "slow"
    1025 KiB of 1 KiB lines, then 1 s later one more and its 0x33; "esc" a
    line that escapes past 16 MiB (3 MiB of 0x01 in a string); "crlf" CR
    as JSON space and in a string; "capcut" whole lines, a partial line,
    then a 0x33 "cap"; "holdcut" whole lines and a partial line, its 0x33
    only for a 0x34; "crash" whole lines, a partial line, then the 0x33 of
    a host dch SIGKILLed (exit 137, no why); "raw" a 17 MiB line with no
    control, in serve's 64 KiB frames; #226: "late" its hello 0.4 s late,
    then `{"ok":1}`, exit 0 and the close (shut() waits for it)."""

    def __init__(self, mode, out=b""):
        self.mode, self.n, self.out = mode, 0, out
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns, self.said = [], []
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.n += 1
            self.conns.append(c)
            if self.mode != "dark" or self.n == 1:
                threading.Thread(target=self.serve, args=(c,),
                                 daemon=True).start()

    def serve(self, c):
        buf = b""
        answers = {
            "nul": frame(0x31, b'{"a":1}\x00junk\n')
            + frame(0x32, b"e\x00rr\n") + frame(0x33, {"exit": 0}),
            "order": frame(0x31, b'{"o":1}\n') + frame(0x32, b"e1\n")
            + frame(0x31, b'{"o":2}\n') + frame(0x32, b"e2\n")
            + frame(0x33, {"exit": 0}),
            "osc": frame(0x31, b'{"schema":1}\x1b]52;c;cHduZWQ=\x07\x1b[2J'
                         b'\n{"t":"a\xc2\x9bb\xe2\x80\xaec"}\n')
            + frame(0x32, b"dch: \x1b]0;PWNED\x07x\n")
            + frame(0x33, {"exit": 0}),
            "dark": frame(0x33, {"exit": 0}),
            "late": frame(0x31, b'{"ok":1}\n') + frame(0x33, {"exit": 0}),
            "silent": frame(0x31, b'{"cut":'),
            "cut": frame(0x31, b'{"a":1}\n{"b":2}\n{"cut":')
            + frame(0x32, b"e-cut"),
            "hist": frame(0x31, self.out) + frame(0x33, {"exit": 0}),
            "esc": b"".join(frame(0x31, x[i:i + 60000]) for x in
                            [b'{"s":"' + b"\x01" * (3 << 20)
                             + b'"}\n{"after":1}\n'] for i in
                            range(0, len(x), 60000))
            + frame(0x33, {"exit": 0}),
            "crlf": frame(0x31, b'{"f":1}\r\n{"g":"a\rb"}\n{"h":\r2}\r\n')
            + frame(0x33, {"exit": 0}),
            "capcut": frame(0x31, b'{"ok":1}\n{"cut":"aaaa')
            + frame(0x33, {"exit": 2, "why": "cap"}),
            "holdcut": frame(0x31, b'{"ok":1}\n{"cut":'),
            "crash": frame(0x31, b'{"ok":1}\n{"half":')
            + frame(0x32, b"e-half") + frame(0x33, {"exit": 137}),
            "raw": b"".join(frame(0x31, x[i:i + 65536]) for x in
                            [b'{"s":"' + b"a" * (17 << 20) + b'"}\n']
                            for i in range(0, len(x), 65536))
            + frame(0x33, {"exit": 0})}
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
                    buf = buf[5 + n:]
                    if t == 0x08:
                        if self.mode == "late":
                            time.sleep(0.4)
                        c.sendall(frame(0x18, {
                            "proto": "1.1", "server": "fake",
                            "ops": ["ctl", "ctl-cancel", "xray"]}))
                    elif t == 0x34 and self.mode in ("hold", "holdcut"):
                        c.sendall(frame(0x33, {"exit": 2, "why": "cancel"}))
                    elif t == 0x30 and self.mode == "slow":
                        pad = b"x" * 1000
                        c.sendall(b"".join(frame(0x31, b"".join(
                            b'{"n":%d,"p":"%s"}\n' % (i + j, pad)
                            for j in range(32))) for i in range(0, 1024, 32)))
                        time.sleep(1)
                        c.sendall(frame(0x31, b'{"last":1}\n')
                                  + frame(0x33, {"exit": 0}))
                    elif t == 0x30 and self.mode in answers:
                        if self.mode in ("dark", "cut", "late"):
                            self.said.append(c)
                        c.sendall(answers[self.mode])
                        if self.mode in ("dark", "cut", "late"):
                            time.sleep(0.2)
                            c.close()
                            return
        except OSError:
            return

    def shut(self, secs=10):
        """#279: until each connection it answered and closes is closed: a
        request sent before that goes to a socket closing with it unread
        (a reset, exit 7 lost), which dch serve never does 0.2 s after an
        answer (it keeps the connection warm for CTL_IDLE_MS)"""
        end = time.time() + secs
        while any(c.fileno() >= 0 for c in self.said) and time.time() < end:
            time.sleep(0.01)

    def close(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


def nfds(pid):
    """pid's open descriptors, counted"""
    if os.path.isdir("/proc/%d/fd" % pid):
        return len(os.listdir("/proc/%d/fd" % pid))
    lsof = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"
    return len(subprocess.run([lsof, "-p", str(pid)], capture_output=True,
                              text=True).stdout.splitlines())


def dial_cancel_checks(check, side, fake):
    """#226 (QA 1.56.0 LOW-10 residual): --requests reads stdin while it
    dials, so a {"cancel":N} for the request still dialling, or stdin's
    end, ends it "cancel" at once, not "lost" once the dial gives up. The
    host is dark (XFake "dark": the first connection answers, later ones
    get no hello), so a dial takes RM_ATTACH_MS, 5 s: the bounds are
    against that, with room for a loaded machine."""
    dark = "dch: q: unreachable: no answer within 5000 ms"
    cancel = [{"req": n, "exit": 2, "why": "cancel"} for n in range(4)]

    def dialling():
        """a --requests whose warm connection the dark host has closed:
        the next request dials"""
        f = fake("dark")
        rq = Requests(side(f.port), "q:x")
        rq.line()
        rq.send({"req": 1, "argv": ["--json"]})
        rq.until(1)
        time.sleep(0.5)	# the host's close reaches dch, quietly
        return f, rq

    # the dialling request's cancel; one for an id not running: nothing
    f, rq = dialling()
    t0 = time.time()
    rq.send({"req": 2, "argv": ["--json"]})
    time.sleep(0.3)
    rq.send({"cancel": 9}, {"cancel": 2})
    got = [json.loads(g) for g in rq.until(2, 30)]
    took = time.time() - t0
    check("#226: a cancel while its request dials ends it cancel, exit 2, "
          "at once, not lost when the 5 s dial gives up; a cancel for an id "
          "not running writes nothing", got == cancel[2:3] and took < 4,
          (got, took))
    # the same dark host after the cancel: the dial goes on, quietly; the
    # next request waits on it and is cancelled the same way; the one
    # after that ends lost when that one dial gives up
    rq.send({"req": 3, "argv": ["--json"]})
    time.sleep(0.3)
    t0 = time.time()
    rq.send({"cancel": 3})
    got = [json.loads(g) for g in rq.until(3, 30)]
    took = time.time() - t0
    rq.send({"req": 4, "argv": ["--json"]})
    got4 = [json.loads(g) for g in rq.until(4, 30)]
    rc, rest, err = rq.end()
    check("#226: the next request to the dark host after the cancel: its "
          "cancel too ends it at once; the one after ends lost when the one "
          "dial gives up, no dial of its own",
          got == cancel[3:4] and took < 3
          and got4 == [{"req": 4, "stderr": dark},
                       {"req": 4, "exit": UNREACHABLE_EXIT, "why": "lost"}]
          and f.n == 2 and rc == 0 and rest == b"",
          (got, took, got4, f.n, rc, rest, err))

    # a request queued behind the dialling one waits; the dialling one's
    # cancel ends it, the queued one then waits on the same dial, and its
    # cancel ends it too
    f, rq = dialling()
    t0 = time.time()
    rq.send({"req": 2, "argv": ["--json"]}, {"req": 3, "argv": ["--json"]})
    time.sleep(0.3)
    rq.send({"cancel": 2})
    got = [json.loads(g) for g in rq.until(2, 30)]
    rq.send({"cancel": 3})
    got += [json.loads(g) for g in rq.until(3, 30)]
    took = time.time() - t0
    rc, rest, err = rq.end()
    check("#226: a request queued behind the dialling one: the dialling "
          "one's cancel ends it at once, then the queued one's ends it",
          got == cancel[2:4] and took < 4 and f.n == 2 and rc == 0
          and rest == b"", (got, took, f.n, rc, rest, err))

    # stdin's end while a request dials, one queued behind it
    f, rq = dialling()
    rq.send({"req": 2, "argv": ["--json"]}, {"req": 3, "argv": ["--json"]})
    time.sleep(0.3)
    t0 = time.time()
    rc, rest, err = rq.end(30)
    took = time.time() - t0
    check("#226: stdin EOF while a request dials: it and the one queued "
          "behind it end cancel, exit 0, at once",
          rc == 0 and [json.loads(x) for x in rest.splitlines()]
          == cancel[2:4] and took < 4, (rc, rest, err, took))

    # a dial that ends as the cancel arrives, either side of it: each
    # request ends once, cancel or the host's exit 0, and no socket stays
    f = fake("late")
    rq = Requests(side(f.port), "q:x")
    rq.line()
    rq.send({"req": 1, "argv": ["--json"]})
    rq.until(1)
    f.shut()
    time.sleep(0.5)
    n0 = nfds(rq.p.pid)
    got = []
    for i, d in enumerate((0.1, 0.2, 0.3, 0.4, 0.5, 0.6)):
        rq.send({"req": 10 + i, "argv": ["--json"]})
        time.sleep(d)
        rq.send({"cancel": 10 + i})
        got.append([json.loads(g) for g in rq.until(10 + i, 30)])
        f.shut()	# #279: the host's close, then the next request dials
        time.sleep(0.3)
    rq.send({"req": 20, "argv": ["--json"]})
    last = [json.loads(g) for g in rq.until(20, 30)]
    f.shut()
    time.sleep(0.5)	# the host's close reaches dch
    n1 = nfds(rq.p.pid)
    rc, rest, err = rq.end()
    ends = [g[-1] for g in got if g]
    check("#226: a dial ending as its cancel arrives: one end line each, "
          "cancel or the host's exit 0, no descriptor left behind",
          all(len([x for x in g if "exit" in x]) == 1 for g in got)
          and all(e in ({"req": e["req"], "exit": 2, "why": "cancel"},
                        {"req": e["req"], "exit": 0, "why": None})
                  for e in ends) and len(ends) == 6
          and last[-1:] == [{"req": 20, "exit": 0, "why": None}]
          and n1 == n0 and rc == 0 and rest == b"",
          (got, last, n0, n1, rc, rest, err))


def robust_checks(dch, check, cenv, fakes, tmp):
    """#180: remote History's depth and duplicate rules (NIT-8, NIT-9); the
    one-shot remote --xray --json escapes a host's controls
    (LOW-6) and gives up on a host that never answers (LOW-10); --requests
    ends a request once even when its id is reused (LOW-8), keeps a NUL
    (LOW-9), the host's stdout/stderr order (NIT-4), bounds a silent host
    (LOW-10) and a dark host's dial once for the requests queued behind it
    (LOW-10). The host's ctl timeout is lowered to 1 ms on the client
    (DCH_CTL_TIMEOUT_MS, the test knob dch serve reads), so a silent host
    is lost after RM_ATTACH_MS, 5 s."""
    n = [0]

    def side(port, **kw):
        n[0] += 1
        cfg = os.path.join(tmp, "rbcfg-%d" % n[0])
        with open(cfg, "w") as f:
            f.write("remote_hosts = q=127.0.0.1:%d\n" % port)
        e = dict(cenv, DCH_CONFIG=cfg, **kw)
        e.pop("DCH_NO_REMOTE", None)
        return e

    def fake(mode, out=b""):
        f = XFake(mode, out)
        fakes.append(f)
        return f

    # NIT-8, NIT-9: remote History keeps what a strict reader takes (objects
    # and arrays to 64 deep in a record, keys not counted) and drops a
    # record with any key twice in one object, read by dch or not
    def deep(d, o="{", c="}", k='"a":'):
        return (o + k) * (d - 1) + o + c + c * (d - 1) if o == "{" \
            else o * d + c * d
    f = fake("hist", (
        '[{"name":"k33","d":%s},{"name":"k64","d":%s},'
        '{"name":"k65","d":%s},{"name":"a64","d":%s},{"name":"a65","d":%s},'
        '{"name":"u1","x":1,"x":2},{"name":"u2","n":{"y":1,"y":2}},'
        '{"name":"u3","z":1,"\\u007a":2},{"name":"u4","l":[{"w":1,"w":1}]},'
        '{"name":"ok","x":1,"n":{"x":1},"l":[{"x":1},{"x":2}]}]'
        % (deep(33), deep(64), deep(65), deep(64, "[", "]"),
           deep(65, "[", "]"))).encode())

    def nodup(kv):
        if len({k for k, _ in kv}) != len(kv):
            raise ValueError("duplicate key")
        return dict(kv)
    r = dch(cenv, "--history", "--json", "--remote", "--hosts",
            "q=127.0.0.1:%d" % f.port)
    try:
        got = [x["name"] for x in json.loads(r.stdout, object_pairs_hook=nodup)
               if x.get("host") == "q"]
    except (ValueError, RecursionError) as e:
        got = [str(e)]
    check("NIT-8, NIT-9: remote History: 33 and 64 deep kept, 65 dropped; "
          "a key twice anywhere dropped; the document strict",
          r.returncode == 0 and got == ["k33", "k64", "a64", "ok"],
          (got, r.stderr))

    # LOW-6: one-shot --xray --json, every control a \u escape
    f = fake("osc")
    r = subprocess.run([DCH, "--xray", "q:x", "--json"], env=side(f.port),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=20)
    check("LOW-6: one-shot --xray --json escapes a host's OSC 52, C1 and "
          "bidi, exit 0", r.returncode == 0
          and r.stdout == b'{"schema":1}\\u001b]52;c;cHduZWQ=\\u0007'
          b'\\u001b[2J\n{"t":"a\\u009bb\\u202ec"}\n'
          and r.stderr == b"dch: \\u001b]0;PWNED\\u0007x\n"
          and json.loads(r.stdout.splitlines()[1]) == {"t": "a\x9bb\u202ec"},
          r)

    # NIT-5 (Prosper's ruling): one-shot, the link lost mid-line: the whole
    # lines, the partial stdout line dropped, stderr's kept, exit 7
    f = fake("cut")
    r = subprocess.run([DCH, "--xray", "q:x", "--json"], env=side(f.port),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=20)
    check("NIT-5: one-shot --xray, the link lost mid-line: whole lines out, "
          "the partial stdout line dropped, exit 7",
          r.returncode == UNREACHABLE_EXIT
          and r.stdout == b'{"a":1}\n{"b":2}\n'
          and r.stderr == b"e-cutdch: q: unreachable: the connection was "
          b"lost\n", r)

    # LOW-10: one-shot, a host that stops mid-line and never ends: exit 7
    # after 5 s, the partial line dropped (NIT-5)
    f = fake("silent")
    t0 = time.time()
    r = subprocess.run([DCH, "--xray", "q:x", "--json"],
                       env=side(f.port, DCH_CTL_TIMEOUT_MS="1"),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=60)
    took = time.time() - t0
    check("LOW-10: one-shot --xray on a silent host: exit 7, no answer "
          "within 5001 ms, its partial line dropped", r.returncode == UNREACHABLE_EXIT
          and r.stderr == b"dch: q: unreachable: no answer within 5001 ms\n"
          and r.stdout == b"" and 4.5 < took < 40, (r, took))

    # LOW-10: --requests, the same silent host: exit 7 lost, once
    rq = Requests(side(f.port, DCH_CTL_TIMEOUT_MS="1"), "q:x")
    rq.line()
    t0 = time.time()
    rq.send({"req": 1, "argv": ["--json"]})
    got = [json.loads(g) for g in rq.until(1, 60)]
    took = time.time() - t0
    rc, rest, err = rq.end()
    check("LOW-10: --requests on a silent host: exit 7 lost after 5 s",
          got == [{"req": 1, "stderr": "dch: q: unreachable: no answer "
                   "within 5001 ms"},
                  {"req": 1, "exit": UNREACHABLE_EXIT, "why": "lost"}]
          and 4.5 < took < 40 and rc == 0 and rest == b"",
          (got, took, rc, rest, err))

    # LOW-10: a dark host: one dial for the requests queued behind it
    f = fake("dark")
    rq = Requests(side(f.port), "q:x")
    rq.line()
    rq.send({"req": 1, "argv": ["--json"]})
    got = [json.loads(g) for g in rq.until(1)]
    end = time.time() + 10
    while f.n < 1 and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.5)	# the host's close reaches dch, quietly
    rq.send({"req": 2, "argv": ["--json"]}, {"req": 3, "argv": ["--json"]},
            {"cancel": 3})
    got2 = [json.loads(g) for g in rq.until(2, 30)]
    t2 = time.time()
    got3 = [json.loads(g) for g in rq.until(3, 30)]
    t3 = time.time()
    dark = "dch: q: unreachable: no answer within 5000 ms"
    check("LOW-10: a dark host's dial: its request exit 7 lost, the one "
          "queued behind it at once, with no dial of its own",
          got == [{"req": 1, "exit": 0, "why": None}]
          and got2 == [{"req": 2, "stderr": dark},
                       {"req": 2, "exit": UNREACHABLE_EXIT, "why": "lost"}]
          and got3 == [{"req": 3, "stderr": dark},
                       {"req": 3, "exit": UNREACHABLE_EXIT, "why": "lost"}]
          and t3 - t2 < 2 and f.n == 2 and rq.quiet(0.3),
          (got, got2, got3, t3 - t2, f.n))
    time.sleep(5.2)
    rq.send({"req": 4, "argv": ["--json"]})
    end = time.time() + 10
    while f.n < 3 and time.time() < end:
        time.sleep(0.05)
    check("LOW-10: past RM_ATTACH_MS the next request dials again",
          f.n == 3, f.n)
    rq.end(30)
    dial_cancel_checks(check, side, fake)	# #226

    # LOW-8: a reused id while it runs: busy as req null, one end for it
    f = fake("hold")
    rq = Requests(side(f.port), "q:x")
    rq.line()
    rq.send({"req": 1, "argv": ["--json"]}, {"req": 1, "argv": ["--json"]})
    a = rq.line()
    rq.send({"cancel": 1})
    got = rq.until(1)
    rc, rest, err = rq.end()
    check("LOW-8: a reused running id: busy with req null, then its one "
          "end line", json.loads(a) == {"req": None, "exit": 1,
                                        "why": "busy"}
          and [json.loads(g) for g in got]
          == [{"req": 1, "exit": 2, "why": "cancel"}]
          and rc == 0 and rest == b"", (a, got, rc, rest))

    # LOW-9: a NUL in a host line, stdout and stderr: kept as \u0000
    f = fake("nul")
    rq = Requests(side(f.port), "q:x")
    rq.line()
    rq.send({"req": 1, "argv": ["--json"]})
    got = rq.until(1)
    rq.end()
    check("LOW-9: a NUL in a host line: the whole line, \\u0000 for it",
          got == [b'{"req":1,"line":"{\\"a\\":1}\\u0000junk"}',
                  b'{"req":1,"stderr":"e\\u0000rr"}',
                  b'{"req":1,"exit":0,"why":null}'], got)

    # NIT-4: the host's order of stdout and stderr lines, in one read
    f = fake("order")
    rq = Requests(side(f.port), "q:x")
    rq.line()
    rq.send({"req": 1, "argv": ["--json"]})
    got = [json.loads(g) for g in rq.until(1)]
    rq.end()
    check("NIT-4: stdout and stderr lines in the host's order",
          got == [{"req": 1, "line": {"o": 1}}, {"req": 1, "stderr": "e1"},
                  {"req": 1, "line": {"o": 2}}, {"req": 1, "stderr": "e2"},
                  {"req": 1, "exit": 0, "why": None}], got)

    # #197 (QA 1.56.0 MED-R1): one-shot, a reader that stalls past the
    # quiet bound (5001 ms here) is not the host's silence: every line,
    # exit 0
    f = fake("slow")
    p = subprocess.Popen([DCH, "--xray", "q:x", "--json"],
                         env=side(f.port, DCH_CTL_TIMEOUT_MS="1"),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    time.sleep(8)
    out, err = p.communicate(timeout=60)
    lines = out.splitlines()
    check("MED-R1: one-shot --xray, its reader stalled 8 s: all 1025 "
          "lines, exit 0", p.returncode == 0 and err == b""
          and len(lines) == 1025 and lines[-1] == b'{"last":1}',
          (p.returncode, len(lines), err))

    # ... and a reader that goes away still ends it at once (EPIPE: 0)
    p = subprocess.Popen([DCH, "--xray", "q:x", "--json"],
                         env=side(f.port, DCH_CTL_TIMEOUT_MS="1"),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    first = p.stdout.readline()
    t0 = time.time()
    p.stdout.close()
    rc = p.wait(timeout=30)
    took = time.time() - t0
    check("MED-R1: one-shot --xray, its reader gone: exit 0 within the "
          "cancel's 3 s", rc == 0 and first.startswith(b'{"n":0,')
          and took < 4.5 and p.stderr.read() == b"", (rc, first[:20], took))
    p.stderr.close()

    # #197 (LOW-R2): a host line past 16 MiB once escaped is an error, not
    # a quiet exit 0 taken for a closed stdout
    f = fake("esc")
    r = subprocess.run([DCH, "--xray", "q:x", "--json"], env=side(f.port),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=60)
    check("LOW-R2: a host line over 16 MiB escaped: exit 7, said, nothing "
          "of it written", r.returncode == UNREACHABLE_EXIT
          and r.stdout == b"" and r.stderr == b"dch: q: unreachable: it "
          b"sent a line over 16 MiB escaped\n",
          (r.returncode, r.stdout[:40], r.stderr[:200]))

    # #197 (NIT-R-A): a CR outside a string is JSON space and is dropped,
    # one in a string a \u escape: every line still JSON
    f = fake("crlf")
    r = subprocess.run([DCH, "--xray", "q:x", "--json"], env=side(f.port),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=20)
    try:
        js = [json.loads(x) for x in r.stdout.splitlines()]
    except ValueError as e:
        js = [str(e)]
    check("NIT-R-A: CRLF and a CR between tokens dropped, a CR in a string "
          "\\u000d: each line JSON", r.returncode == 0
          and r.stdout == b'{"f":1}\n{"g":"a\\u000db"}\n{"h":2}\n'
          and js == [{"f": 1}, {"g": "a\rb"}, {"h": 2}], (r, js))

    # #197 (NIT-R-B): a host's cap and a cancel drop the partial line, as
    # a lost link does (NIT-5): whole lines or nothing
    f = fake("capcut")
    r = subprocess.run([DCH, "--xray", "q:x", "--json"], env=side(f.port),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=20)
    check("NIT-R-B: a host's cap: the whole lines, the partial line "
          "dropped", r.returncode == 2 and r.stdout == b'{"ok":1}\n'
          and r.stderr == b"dch: q: --xray stopped by the host (cap)\n", r)
    f = fake("holdcut")
    p = subprocess.Popen([DCH, "--xray", "q:x", "--json"],
                         env=side(f.port), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first = p.stdout.readline()
    p.send_signal(signal.SIGINT)
    out, err = p.communicate(timeout=20)
    check("NIT-R-B: Ctrl-C: the whole lines, the partial line dropped",
          first == b'{"ok":1}\n' and out == b""
          and p.returncode == -signal.SIGINT, (first, out, err,
                                               p.returncode))

    # QA 1.58.0 LOW-R7: the host's dch killed by a signal mid-line: the
    # partial stdout line dropped, as for a cut; said; exit 128 + N
    f = fake("crash")
    r = subprocess.run([DCH, "--xray", "q:x", "--json"], env=side(f.port),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=20)
    check("LOW-R7: a host dch killed mid-line: the whole lines, the partial "
          "line dropped, said, exit 137", r.returncode == 137
          and r.stdout == b'{"ok":1}\n' and r.stderr == b"e-halfdch: q: "
          b"--xray on the host ended by signal 9\n", r)

    # QA 1.58.0 NIT-R3: a raw host line over 16 MiB (no control in it) is
    # a line over 16 MiB, not "out of memory": exit 7, nothing written
    f = fake("raw")
    r = subprocess.run([DCH, "--xray", "q:x", "--json"], env=side(f.port),
                       capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=60)
    check("NIT-R3: a raw host line over 16 MiB: exit 7, the line's size "
          "said, nothing written", r.returncode == UNREACHABLE_EXIT
          and r.stdout == b"" and r.stderr == b"dch: q: unreachable: it "
          b"sent a line over 16 MiB escaped\n",
          (r.returncode, r.stdout[:40], r.stderr[:200]))


def main():
    # POSIX only, so windows_client_test.py can import Fake and frame
    import fcntl, pty, termios
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchrc", dir="/tmp"))
    fake = os.path.join(tmp, "fake")
    os.makedirs(fake)
    with open(os.path.join(fake, "tailscale"), "w") as f:
        f.write(FAKE_TS)
    os.chmod(os.path.join(fake, "tailscale"), 0o755)

    def put(name, obj):
        with open(os.path.join(fake, name), "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    put("status.json", OWNER)
    put("whois.json", WHOIS)
    put("ip.txt", "100.100.1.1\n")
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("DCH_", "MallocStackLogging"))}

    def side(name, **kw):
        d = os.path.join(tmp, name)
        os.makedirs(os.path.join(d, "home"))
        e = dict(base, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=os.path.join(d, "st"),
                 HOME=os.path.join(d, "home"), DCH_LOGIN_ENV="0",
                 DCH_CONFIG=os.path.join(d, "config"), SHELL="/bin/sh",
                 PATH=fake + ":/usr/bin:/bin", TERM="xterm")
        e.update(kw)
        return e

    senv = side("srv")
    cenv = side("cli", DCH_NO_REMOTE="1", DCH_DOUBLE_TAP_MS="0")
    if os.environ.get("DCH_TEST_NO_BUDGET"):
        # ASan: a loopback dch serve can miss the 300 ms box
        cenv["DCH_REMOTE_BOX_MS"] = "5000"
    csock = os.path.join(tmp, "cli", "dch-%d" % os.getuid())
    procs, fakes, clients = [], [], []
    fails = []

    def check(msg, cond, extra=""):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)
            if extra:
                print("       " + str(extra).replace("\n", "\n       "))

    def dch(env, *args, timeout=20):
        return subprocess.run([DCH] + list(args), env=env, timeout=timeout,
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def read(fd, until, secs):
        out = b""
        end = time.time() + secs
        while time.time() < end and until not in out:
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd in r:
                try:
                    c = os.read(fd, 65536)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise
                if not c:
                    break
                out += c
        return out

    def attach(args, cols=100, rows=30):
        """dch <args> on its own pty of cols x rows: (pid, fd)."""
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(DCH, [DCH] + args, cenv)
            os._exit(127)
        # pty.fork()'s master is inheritable: a later client would hold it,
        # and closing ours would not hang this pty up (QA 1.59.0 MED-T2)
        os.set_inheritable(fd, False)
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        clients.append((pid, fd))
        return pid, fd

    def reap(pid, fd, secs):
        end = time.time() + secs
        out = b""
        while time.time() < end:
            got, st = os.waitpid(pid, os.WNOHANG)
            if got:
                clients.remove((pid, fd))
                return st, out + read(fd, b"\0never", 0.2)
            out += read(fd, b"\0never", 0.05)
        return None, out

    def serve_at(log, *args, **env):
        """dch serve on a loopback test port: the port, or None."""
        with open(log, "w") as lf:
            sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                                   "--port", "0"] + list(args),
                                  env=dict(senv, DCH_SERVE_TEST_LOOPBACK="1",
                                           **env),
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=lf)
        procs.append(sp)
        port = None
        end = time.time() + 10
        while time.time() < end and port is None and sp.poll() is None:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(log).read())
            port = int(m.group(1)) if m else None
            time.sleep(0.05)
        return port

    try:
        # the server and its session
        log = os.path.join(tmp, "serve.log")
        port = serve_at(log)
        check("dch serve listens on a loopback test port", port is not None,
              open(log).read())
        if port is None:
            return 1
        r = dch(senv, "--spawn", "rs1", "--size", "80x24", "--", "/bin/sh")
        check("a session on the server", r.returncode == 0, r.stderr)
        r = dch(cenv, "--spawn", "loc1", "--size", "80x24", "--", "/bin/sh")
        check("a local session on the client", r.returncode == 0, r.stderr)
        hosts = "srv=127.0.0.1:%d" % port

        # --- listing --------------------------------------------------
        quiet = Fake("list", rows=[{"name": "q1"}])
        fakes.append(quiet)
        qh = "q=127.0.0.1:%d" % quiet.port
        r = dch(cenv, "-ls", "--hosts", qh)
        r2 = dch(cenv, "-lj", "--hosts", qh)
        r3 = dch(cenv, "--ls-json", "--hosts", qh)
        time.sleep(0.3)
        check("plain -ls, -lj, --ls-json: local rows only, no connection",
              r.stdout.splitlines() == ["loc1"] and "q:" not in r2.stdout
              and "q1" not in r3.stdout and quiet.conns == [],
              (r.stdout, r2.stdout, r3.stdout, len(quiet.conns)))
        r = dch(cenv, "-ls", "--remote", "--hosts", qh)
        check("-ls --remote: it connects and lists q:q1",
              "q:q1" in r.stdout.splitlines() and len(quiet.conns) == 1,
              (r.stdout, len(quiet.conns)))
        def listed(ok, *args):
            """args run until ok(r): under load the host's answer can miss
            the 300 ms --ls box; 10 s, as the other waits here"""
            end = time.time() + 10
            r = dch(cenv, *args)
            while not ok(r) and time.time() < end:
                time.sleep(0.2)
                r = dch(cenv, *args)
            return r

        r = listed(lambda r: "srv:rs1" in r.stdout.splitlines(),
                   "--ls", "--remote", "--hosts", hosts)
        lines = r.stdout.splitlines()
        check("--ls --remote: the local session, then srv:rs1",
              r.returncode == 0 and "loc1" in lines and "srv:rs1" in lines
              and lines.index("loc1") < lines.index("srv:rs1"), r)
        r = dch(cenv, "--ls-json", "--hosts", hosts)
        rows = json.loads(r.stdout)
        check("--ls-json without --remote: local rows only",
              [x["name"] for x in rows] == ["loc1"]
              and all("host" not in x for x in rows), r.stdout)
        r = listed(lambda r: '"stale":false' in r.stdout,
                   "--ls-json", "--remote", "--hosts", hosts)
        rows = json.loads(r.stdout)
        rem = [x for x in rows if "host" in x]
        check("--ls-json --remote: a row with host srv and name rs1",
              len(rem) == 1 and rem[0]["host"] == "srv"
              and rem[0]["name"] == "rs1" and rem[0]["stale"] is False
              and "loc1" in [x["name"] for x in rows], r.stdout)
        r = dch(cenv, "-lj", "--hosts", hosts)
        check("-lj without --remote: no remote rows",
              "srv:" not in r.stdout and "loc1\t" in r.stdout, r.stdout)
        r = listed(lambda r: "srv:rs1\t\t0" in r.stdout.splitlines(),
                   "-lj", "--remote", "--hosts", hosts)
        check("-lj --remote: srv:rs1<TAB>alias<TAB>0",
              "srv:rs1\t\t0" in r.stdout.splitlines(), r.stdout)
        r = dch(cenv, "--remote", "-kl")
        check("--remote with -kl is refused", r.returncode == 1
              and "--remote only works" in r.stderr, r.stderr)
        r = dch(cenv, "-ls", "--hosts", "a b;c")
        check("a bad --hosts entry is refused", r.returncode == 1
              and "bad entry" in r.stderr, r.stderr)
        r = dch(cenv, "--host", "srv", "-ls")
        check("--host without -a is refused", r.returncode == 1, r.stderr)

        # --- attach ---------------------------------------------------
        pid, fd = attach(["--hosts", hosts, "-a", "srv:rs1"])
        out = read(fd, b"$ ", 3)
        if b"$ " not in out:
            # lite: no replay, and an idle sh repaints nothing; prompt it
            os.write(fd, b"\r")
            out += read(fd, b"$ ", 8)
        check("-a srv:rs1 shows the remote screen", b"$ " in out, out)
        os.write(fd, b"echo rt$((6*7))\r")
        out = read(fd, b"rt42", 5)
        check("input round-trips", b"rt42" in out, out)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 111, 0, 0))
        os.kill(pid, signal.SIGWINCH)
        time.sleep(0.5)
        os.write(fd, b"stty size\r")
        out = read(fd, b"33 111", 5)
        check("a resize arrives", b"33 111" in out, out)
        os.write(fd, b"\x1c")
        st, out = reap(pid, fd, 5)
        check("the detach key detaches (exit 0)",
              st is not None and os.WIFEXITED(st)
              and os.WEXITSTATUS(st) == 0, (st, out))
        r = dch(senv, "-ls")
        check("the remote session lives on", "rs1" in r.stdout.split(),
              r.stdout)

        # --- #102: a newer attach takes the session -------------------
        pid, fd = attach(["--hosts", hosts, "-a", "srv:rs1"])
        out = read(fd, b"$ ", 3)
        if b"$ " not in out:
            os.write(fd, b"\r")
            out += read(fd, b"$ ", 8)
        pid2, fd2 = attach(["--hosts", hosts, "-a", "srv:rs1"])
        read(fd2, b"$ ", 3)
        st, out = reap(pid, fd, 8)
        check("a newer attach takes the session: the older says so, exit "
              "%d (#102)" % TAKEN_EXIT,
              st is not None and os.WIFEXITED(st)
              and os.WEXITSTATUS(st) == TAKEN_EXIT
              and b"dch: srv: " + TAKEN["message"].encode() in out
              and b"lost the connection" not in out, (st, out[-300:]))
        check("the terminal restored before the message",
              0 <= out.find(b"\x1b[?1049l") < out.find(b"dch: srv: another"),
              out[-300:])
        os.write(fd2, b"\x1c")
        st, out = reap(pid2, fd2, 5)
        check("the newer one stays attached, then detaches (exit 0)",
              st is not None and os.WIFEXITED(st)
              and os.WEXITSTATUS(st) == 0, (st, out))

        pid, fd = attach(["--hosts", hosts, "--host", "srv", "-a", "rs1"])
        out = read(fd, b"$ ", 8)
        os.write(fd, b"echo h$((2+3))\r")
        out += read(fd, b"h5", 5)
        os.write(fd, b"\x1c")
        st, _ = reap(pid, fd, 5)
        check("--host srv -a rs1 attaches", b"h5" in out and st is not None
              and os.WEXITSTATUS(st) == 0, out)

        # --- #101: --scrollback / DCH_REPLAY_SCROLLBACK in the 0x01 ------
        sbf = Fake("exit", rows=[{"name": "sb1"}])
        fakes.append(sbf)
        sbh = "sb=127.0.0.1:%d" % sbf.port
        for args, ev, want in ((["--scrollback", "7"], None, 7),
                               ([], "4", 4), ([], None, None),
                               (["--scrollback", "0"], "4", None)):
            if ev:
                cenv["DCH_REPLAY_SCROLLBACK"] = ev
            pid, fd = attach(["--hosts", sbh, "-a", "sb:sb1"] + args)
            reap(pid, fd, 8)
            cenv.pop("DCH_REPLAY_SCROLLBACK", None)
            body = json.loads(sbf.bodies[-1]) if sbf.bodies else {}
            check("0x01 scrollback for %s %s: %s" % (args, ev, want),
                  body.get("scrollback") == want and body.get("name") == "sb1"
                  and list(body) == sorted(body), body)
        mirror = dch(cenv, "--build").stdout.strip() == "full"
        if mirror:
            # end to end: the client's 0x01 key, dch serve's --scrollback for
            # the client it spawns, MSG_ATTACH's len, the master's replay
            r = dch(senv, "--spawn", "rsb", "--size", "40x10", "--", "sh",
                    "-c", "seq 1 40; printf END; exec sleep 300")
            end = time.time() + 10
            while time.time() < end and "END" not in \
                    dch(senv, "--read", "rsb").stdout:
                time.sleep(0.1)
            pid, fd = attach(["--hosts", hosts, "-a", "srv:rsb",
                              "--scrollback", "5"], cols=40, rows=10)
            out = read(fd, b"END", 8)
            os.write(fd, b"\x1c")
            reap(pid, fd, 5)
            check("-a srv:rsb --scrollback 5: the rows reach the terminal",
                  b"27\r\n28\r\n29\r\n30\r\n31\x1b[0m\x1b[999H" in out,
                  out[-400:])
            dch(senv, "-k", "rsb")

        # --- ctl (#114): the control verbs on a remote host ------------
        ctl_checks(dch, check, senv, cenv, serve_at, procs, fakes, tmp,
                   hosts)
        xray_checks(dch, check, senv, cenv, port, fakes, tmp, log)
        requests_checks(dch, check, senv, cenv, serve_at, fakes, tmp, log)
        robust_checks(dch, check, cenv, fakes, tmp)	# #180

        # --- precedence -----------------------------------------------
        # (--spawn srv:rs1 is remote now, #114: -n makes the local one)
        pid, fd = attach(["-n", "srv:rs1", "--", "/bin/sh"])
        read(fd, b"$ ", 3)
        os.write(fd, b"\x1c")
        reap(pid, fd, 5)
        check("a local session named srv:rs1",
              "srv:rs1" in dch(cenv, "-ls").stdout.split())
        # the remote is unreachable now: only a local attach can work
        pid, fd = attach(["--hosts", "srv=127.0.0.1:1", "-a", "srv:rs1"])
        read(fd, b"\0never", 1.5)
        marks = [f for f in os.listdir(csock)
                 if f.startswith("srv:rs1.sock.client.")]
        os.write(fd, b"\x1c")
        st, out = reap(pid, fd, 5)
        check("an exact local name wins", marks and st is not None
              and os.WEXITSTATUS(st) == 0, (marks, st, out))
        dch(cenv, "-k", "srv:rs1")

        r = dch(cenv, "--hosts", hosts, "-a", "nope:rs1")
        check("an unknown host: a clear error, exit 1", r.returncode == 1
              and "nope is not a known host" in r.stderr
              and "known: srv" in r.stderr, r.stderr)
        r = dch(cenv, "--hosts", hosts, "-a", "nosuch")
        check("-a never creates", r.returncode == 1
              and "no session: nosuch" in r.stderr
              and "nosuch" not in dch(cenv, "-ls").stdout, r.stderr)
        r = dch(cenv, "--hosts", hosts, "-a", "srv:gone")
        check("-a host:missing says so, creates nothing", r.returncode == 1
              and "no session gone on srv" in r.stderr
              and "gone" not in dch(senv, "-ls").stdout, r.stderr)
        put("status.json", PEERS)
        denv = dict(cenv)
        del denv["DCH_NO_REMOTE"]
        r = dch(denv, "-a", "nope:x")
        check("discovery: only the user's online untagged peers",
              r.returncode == 1 and "known: mybox)" in r.stderr, r.stderr)
        put("status.json", dict(PEERS, BackendState="Stopped"))
        r = dch(denv, "-a", "nope:x")
        check("discovery: none unless BackendState is Running",
              r.returncode == 1 and "none:" in r.stderr, r.stderr)
        neg = json.loads(json.dumps(PEERS))     # #129: -1 is "unknown"
        neg["Self"]["UserID"] = -1
        for peer in neg["Peer"].values():
            peer["UserID"] = -1 if peer["UserID"] == 42 else peer["UserID"]
        put("status.json", neg)
        r = dch(denv, "-a", "nope:x")
        check("discovery: none for a negative Self.UserID",
              r.returncode == 1 and "none:" in r.stderr, r.stderr)
        put("status.json", PEERS)
        for why, w in (("another user", dict(WHOIS, UserProfile={
                           "ID": 7, "LoginName": "them@example.com"})),
                       ("a tagged node", dict(WHOIS, Node=dict(
                           WHOIS["Node"], Tags=["tag:x"]))),
                       ("failing", None)):
            if w is None:
                os.unlink(os.path.join(fake, "whois.json"))
            else:
                put("whois.json", w)
            r = dch(denv, "-a", "mybox:x")
            check("a discovered host, whois %s: refused before connecting"
                  % why, r.returncode == 1 and "tailscale whois" in r.stderr,
                  r.stderr)
        put("whois.json", WHOIS)
        r = dch(dict(denv, DCH_NO_REMOTE="1"), "-a", "nope:x")
        check("DCH_NO_REMOTE=1: -a host:name stays local (#122)",
              r.returncode == 1 and "no session: nope:x" in r.stderr
              and "known host" not in r.stderr, r.stderr)
        put("status.json", OWNER)

        # --- a hostile server -----------------------------------------
        evil = Fake("list", rows=[
            {"name": "ev\x1b]0;pwn\x07\x1b[31mred", "alias": "a\x1b[2Jb"},
            {"name": "tab\there\nnl", "state": "\x1b[5mworking"},
            {"no": "name"}, {"name": 5}, "junk"])
        fakes.append(evil)
        eh = "evil=127.0.0.1:%d" % evil.port
        r = dch(cenv, "--ls", "--remote", "--hosts", eh)
        check("hostile names in --ls: no ESC, BEL, tab or newline reaches "
              "the terminal", r.returncode == 0 and "\x1b" not in r.stdout
              and "\x07" not in r.stdout and "\t" not in r.stdout
              and "evil:ev\\x1b]0;pwn" in r.stdout
              and "evil:tab\\x09here\\x0anl" in r.stdout, repr(r.stdout))
        r = dch(cenv, "-lj", "--remote", "--hosts", eh)
        check("hostile names in -lj: 3 fields a line, escaped",
              "\x1b" not in r.stdout and all(
                  len(l.split("\t")) == 3 for l in r.stdout.splitlines()),
              repr(r.stdout))
        r = dch(cenv, "--ls-json", "--remote", "--hosts", eh)
        rows = [x for x in json.loads(r.stdout) if "host" in x]
        check("hostile names in --ls-json: valid JSON, JSON-escaped, a bad "
              "state dropped", "\x1b" not in r.stdout and len(rows) == 2
              and rows[0]["name"].startswith("ev\x1b]")
              and all(x["state"] == "" for x in rows), r.stdout)
        c1 = Fake("list", rows=[{"name": "c1\u009b31m\x7f", "alias":
                                 "bi\u202edi\u2066x\u200e\u200fy"}])
        fakes.append(c1)
        r = subprocess.run([DCH, "--ls-json", "--remote", "--hosts",
                            "c=127.0.0.1:%d" % c1.port], env=cenv,
                           capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=20)
        out = r.stdout
        rows = [x for x in json.loads(out) if "host" in x]
        check("C1, DEL and bidi in --ls-json: \\u-escaped, the same value",
              b"\xc2\x9b" not in out and b"\xe2\x80\xae" not in out
              and b"\xe2\x81\xa6" not in out and b"\x7f" not in out
              and b"\xe2\x80\x8e" not in out and b"\xe2\x80\x8f" not in out
              and b"c1\\u009b31m\\u007f" in out
              and b"\\u202e" in out and b"\\u2066" in out
              and b"\\u200e\\u200fy" in out
              and rows and rows[0]["name"] == "c1\u009b31m\x7f"
              and rows[0]["alias"] == "bi\u202edi\u2066x\u200e\u200fy", out)

        # --- a hanging host, then its rows stale ----------------------
        hang = Fake("hang")
        fakes.append(hang)
        t0 = time.time()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "%s,h=127.0.0.1:%d" % (hosts, hang.port))
        dt = time.time() - t0
        check("a hanging host does not stall --ls (%.2fs)" % dt,
              dt < BUDGET and "srv:rs1" in r.stdout.splitlines(), r)
        t0 = time.time()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % hang.port)
        dt = time.time() - t0
        check("then its cached rows show stale (%.2fs)" % dt,
              dt < BUDGET and "srv:rs1 [stale]" in r.stdout.splitlines(),
              r.stdout)
        r = dch(cenv, "--ls-json", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % hang.port)
        rem = [x for x in json.loads(r.stdout) if "host" in x]
        check("--ls-json marks them stale", rem and rem[0]["stale"] is True,
              r.stdout)

        # --- a tampered cache -----------------------------------------
        cache = os.path.join(tmp, "cli", "st", "dch", "sessions",
                             ".remote-cache")
        now = int(time.time())
        bad = [{"host": "srv", "name": "past", "at": -2 ** 63},
               {"host": "srv", "name": "future", "at": 99999999999},
               {"host": "srv", "name": "zero", "at": 0},
               {"host": "srv\u202e\u001b[2J:zz", "name": "spoof",
                "at": now},
               {"host": "srv", "name": "keep", "at": now}]

        def put_cache():
            with open(cache, "w") as f:
                f.write(json.dumps(bad))
        put_cache()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % hang.port)
        lines = r.stdout.splitlines()
        check("a tampered cache: an at out of range is not listed",
              r.returncode == 0 and "srv:keep [stale]" in lines
              and not any(x in r.stdout for x in ("past", "future", "zero",
                                                  "spoof")), r)
        put_cache()
        pid, fd = attach(["--hosts", "srv=127.0.0.1:%d" % hang.port, "-l"])
        out = read(fd, b"keep", 4)
        out += read(fd, b"\0never", 0.5)
        os.write(fd, b"q")
        st, more = reap(pid, fd, 5)
        out += more
        check("a tampered cache in the picker: only label hosts, in range",
              b"srv:\x1b[0mkeep" in out and b"spoof" not in out
              and b"\xe2\x80\xae" not in out and b"future" not in out,
              (st, out))

        # --- a host whose gate refuses us (#094) -----------------------
        refuse = Fake("refuse")
        fakes.append(refuse)
        put_cache()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % refuse.port)
        check("a refusing host: no stale rows, its reason on stderr (#094)",
              r.returncode == 0 and "srv:" not in r.stdout
              and "dch: srv refused: this host serves only its owner's "
                  "devices" in r.stderr, r)
        put_cache()
        r = dch(cenv, "--ls-json", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % refuse.port)
        check("--ls-json: no rows of a refusing host, the reason on stderr",
              r.returncode == 0 and not [x for x in json.loads(r.stdout)
                                         if "host" in x]
              and "srv refused: this host serves only" in r.stderr, r)
        r = dch(cenv, "--hosts", "srv=127.0.0.1:%d" % refuse.port,
                "-a", "srv:keep")
        check("-a to a refusing host: its reason, exit %d (#094, #102)"
              % REFUSED_EXIT, r.returncode == REFUSED_EXIT and r.stderr == "dch: srv refused: this host "
              "serves only its owner's devices\n", r)

        # --- a busy host (#095): transient, its cached rows stale -----
        busy = Fake("busy")
        fakes.append(busy)
        put_cache()
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % busy.port)
        check("a busy host: its cached rows stay, stale (#095)",
              r.returncode == 0 and "srv:keep [stale]" in r.stdout.splitlines()
              and "refused" not in r.stderr, r)
        r = dch(cenv, "--ls-json", "--remote", "--hosts",
                "srv=127.0.0.1:%d" % busy.port)
        rem = [x for x in json.loads(r.stdout) if "host" in x]
        check("--ls-json: a busy host's rows stay, stale (#095)",
              r.returncode == 0 and rem and rem[0]["name"] == "keep"
              and rem[0]["stale"] is True and "refused" not in r.stderr, r)
        r = dch(cenv, "--hosts", "srv=127.0.0.1:%d" % busy.port,
                "-a", "srv:keep")
        check("-a to a busy host: its message, not a refusal, exit 1 (#095)",
              r.returncode == 1 and r.stderr == "dch: srv: says: " +
              BUSY["message"] + "\n", r)

        # --- a newer protocol -----------------------------------------
        newer = Fake("list", rows=[{"name": "n1"}], proto="2.0")
        fakes.append(newer)
        r = dch(cenv, "--ls", "--remote", "--hosts",
                "nw=127.0.0.1:%d" % newer.port)
        check("a newer major: skipped, said on stderr",
              "nw:n1" not in r.stdout and "speaks protocol 2.0" in r.stderr,
              r)

        # --- the link drops, the session ends -------------------------
        drop = Fake("drop", rows=[{"name": "d1"}])
        fakes.append(drop)
        pid, fd = attach(["--hosts", "dr=127.0.0.1:%d" % drop.port,
                          "-a", "dr:d1"])
        st, out = reap(pid, fd, 8)
        check("a dropped link: exit 1, a clear message",
              st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 1
              and b"FAKE-SCREEN" in out
              and b"lost the connection to dr" in out, (st, out))
        check("the terminal restored before the message",
              0 <= out.find(b"\x1b[?1049l") < out.find(b"lost the connection"),
              out)
        ex = Fake("exit", rows=[{"name": "e1"}])
        fakes.append(ex)
        pid, fd = attach(["--hosts", "ex=127.0.0.1:%d" % ex.port,
                          "-a", "ex:e1"])
        st, out = reap(pid, fd, 8)
        check("the session exits (0x12): exit 0, no message",
              st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0
              and b"lost" not in out, (st, out))
        rst = Fake("errrst", rows=[{"name": "x1"}])
        fakes.append(rst)
        pid, fd = attach(["--hosts", "rs=127.0.0.1:%d" % rst.port,
                          "-a", "rs:x1"])
        st, out = reap(pid, fd, 8)
        check("a 0x13 then a reset: the 0x13's reason, not the reset",
              st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 1
              and b"lost the connection to rs: it says: bye-why" in out,
              (st, out))
        tk = Fake("taken", rows=[{"name": "t1"}])
        fakes.append(tk)
        pid, fd = attach(["--hosts", "tk=127.0.0.1:%d" % tk.port,
                          "-a", "tk:t1"])
        st, out = reap(pid, fd, 8)
        check("a 0x13 \"taken\": its message, exit %d (#102)" % TAKEN_EXIT,
              st is not None and os.WIFEXITED(st)
              and os.WEXITSTATUS(st) == TAKEN_EXIT
              and b"dch: tk: " + TAKEN["message"].encode() in out, (st, out))

        # --- the real dch serve's gate refusing us (#094), last: it then
        # refuses 127.0.0.1 for 30 s
        put("whois.json", dict(WHOIS, UserProfile={
            "ID": 7, "LoginName": "other@example.com"}))
        r = dch(cenv, "--hosts", hosts, "-a", "srv:rs1")
        check("dch serve refusing -a: its reason, exit %d (#094, #102)"
              % REFUSED_EXIT, r.returncode == REFUSED_EXIT and r.stderr == "dch: srv refused: this host "
              "serves only its owner's devices\n", r)
        r = dch(cenv, "--ls", "--remote", "--hosts", hosts)
        check("and --ls --remote: no srv rows, its reason (the cached "
              "refusal)", r.returncode == 0 and "srv:" not in r.stdout
              and "dch: srv refused: this host serves only" in r.stderr, r)
    finally:
        for pid, fd in clients:
            try:
                os.kill(pid, signal.SIGKILL)
                os.close(fd)
                end = time.time() + 10
                while not os.waitpid(pid, os.WNOHANG)[0]:
                    if time.time() > end:
                        print("  leaving client %d unreaped" % pid)
                        break
                    time.sleep(0.05)
            except OSError:
                pass
        for f in fakes:
            f.close()
        dch(senv, "-kl")
        dch(cenv, "-kl")
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("all ok" if not fails else "%d FAILED" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
