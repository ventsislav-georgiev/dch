"""dch serve (#073): Prosper's remote frames 1.1 over TCP.

The server runs on 127.0.0.1 with a fake `tailscale` first on PATH and the
test-only loopback bypass (DCH_SERVE_TEST_LOOPBACK=1 plus a loopback --bind);
it never binds a tailnet address. Ports are the kernel's pick (--port 0).
Covers: the golden fixtures (tests/remote-frames, --mutating); the peer gate
(owner mismatch, tagged peer, whois down closed and open, no bypass); startup
refusals (tagged host without --allow-user, --allow-user on an untagged host,
tailscale ip -4 failing or empty, wildcard and non-unicast binds, the port in
use); --allow-user by id and by login (case-insensitive); create off without
--allow-create; the newest attach retiring the older, told so with a 0x13
{"code":"taken"} before the FIN (#102); 0x09 from the mirror;
a newer proto major; an oversize control frame; a client half-close; SIGTERM;
0x01's optional "scrollback" reaching the spawned client (#101). #114: the
ctl op (goldens 17-39, some against a dch serve of their own: create off,
a lowered cap, a lowered timeout); a 0x30 on an attached connection; a
client's close ending its dch. #118: 0x34 cancel, warm reuse, busy, the
idle close (goldens 40-43); a 0x34 then the FIN still gets its 0x33.
#124: a --say stand-in cut by the close or a 0x34 outlives the 2 s grace and
ends on its own; a --status is SIGKILLed 2 s after its SIGTERM. #126: the
real --run and --say cut mid-gate take their text back out of a fake
Claude's composer and end at once; #128: a --say with a short first line
cut by the close leaves the composer empty.
Security review F1-F8: --whois-optional only while tailscaled is down; this
host's own address and node refused; log escaping; the CLI from absolute
PATH entries; odd whois/status JSON failing closed; the test variable unset
for children; refused peers cached before fork. #094: every refusal of a
tailnet peer is one exact 0x13 {"code","message"} then the close (the
cached one too); an address off the tailnet gets no byte. #095: past the
per-peer limit, the transient {"code":"busy"} then the close, never cached.
#202: a device runs at most 2 --xray --follow of one session; a third ends
the oldest as a timeout cut, never busy for it; 4 stalled follows of one
device leave 2 master slots free. #201: a one-shot ctl's 6 MiB answer comes
whole to a reader that stalls 30 s; 16 peers that never read free their
slots 20 s past their ctl timeout.

Run:  python3 tests/serve_test.py        (uses ./dch)
"""
import json, os, re, shutil, signal, socket, struct, subprocess, sys
import tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
RUNPY = os.path.join(HERE, "remote-frames", "golden", "run.py")

FAKE_TS = """#!/bin/sh
d=$(dirname "$0")
echo "$1 ${DCH_SERVE_TEST_LOOPBACK:-unset}" >> "$d/calls"
case "$1" in
status) [ -f "$d/status.json" ] && exec cat "$d/status.json"; exit 1 ;;
whois) [ -f "$d/whois.json" ] && exec cat "$d/whois.json"; exit 1 ;;
ip) [ -f "$d/ip.txt" ] && exec cat "$d/ip.txt"; exit 1 ;;
esac
exit 1
"""

# Headscale-shaped: status Self.ID is the host's StableNodeID, digits in a
# string; whois Node.ID is an int64 and Node.StableID the StableNodeID string.
# SAAS_* are tailscale.com-shaped: the StableNodeID is "n...CNTRL".
OWNER = {"Self": {"ID": "1001", "UserID": 42, "Tags": []},
         "User": {"42": {"LoginName": "me@example.com"}}}
TAGGED = {"Self": {"ID": "1001", "UserID": 9, "Tags": ["tag:server"]},
          "User": {"9": {"LoginName": "tagged-devices"}}}


SAAS_OWNER = {"Self": {"ID": "nXyZ123CNTRL", "UserID": 42, "Tags": []},
              "User": {"42": {"LoginName": "me@example.com"}}}


# The refusal codes and messages (#094): a contract, README and dch.1.
CODES = {
    "not_owner": "this host serves only its owner's devices",
    "not_allowed": "this host is tagged and serves only the users it names: "
                   "start dch serve there with --allow-user <your login or "
                   "id>",
    "tagged_peer": "this host does not serve tagged nodes",
    "whois_unavailable": "this host could not verify you: tailscaled is not "
                         "answering",
    "whois_failed": "this host could not verify you (tailscale whois "
                    "failed)",
    "self": "this host does not serve connections from itself",
}


# the 0x13 a retired attach gets (#102), byte for byte
TAKEN = json.dumps({"code": "taken", "message": "another client attached "
                    "to this session"}, separators=(",", ":")).encode()


def whois(uid, login="me@example.com", tags=(), node="2002", nid=2002):
    return {"UserProfile": {"ID": uid, "LoginName": login},
            "Node": {"ID": nid, "StableID": node, "Tags": list(tags)}}


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchsv", dir="/tmp"))
    fake = os.path.join(tmp, "fake")
    os.makedirs(fake)
    with open(os.path.join(fake, "tailscale"), "w") as f:
        f.write(FAKE_TS)
    os.chmod(os.path.join(fake, "tailscale"), 0o755)
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DCH_", "MallocStackLogging"))}
    env.update(XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=os.path.join(tmp, "state"),
               HOME=home, DCH_LOGIN_ENV="0",
               DCH_CONFIG=os.path.join(tmp, "config"), SHELL="/bin/sh",
               PATH=fake + ":/usr/bin:/bin")
    benv = dict(env, DCH_SERVE_TEST_LOOPBACK="1")
    procs = []
    fails = []

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)

    def put(name, obj):
        p = os.path.join(fake, name)
        if obj is None:
            if os.path.exists(p):
                os.unlink(p)
            return
        with open(p, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def ts(status=OWNER, who=whois(42), ip="100.100.1.1\n"):
        put("status.json", status)
        put("whois.json", who)
        put("ip.txt", ip)

    log_n = [0]

    def serve(*args, e=benv, bind=True, cwd=None, argv0=None):
        """Start dch serve: (proc, port, log path); port None when it exited.
        argv0 "dch": found on e's PATH, so that is the dch its ctls run."""
        log_n[0] += 1
        log = os.path.join(tmp, "serve%d.log" % log_n[0])
        argv = [argv0 or DCH, "serve"] + (["--bind", "127.0.0.1"] if bind else []) + \
            ["--port", "0"] + list(args)
        with open(log, "w") as lf:
            p = subprocess.Popen(argv, executable=DCH, env=e,
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=lf,
                                 cwd=cwd)
        procs.append(p)
        end = time.time() + 10
        while time.time() < end:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", text(log))
            if m:
                return p, int(m.group(1)), log
            if p.poll() is not None:
                return p, None, log
            time.sleep(0.05)
        return p, None, log

    def stop(p):
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        return p.returncode

    def calls():
        return text(os.path.join(fake, "calls")).splitlines() \
            if os.path.exists(os.path.join(fake, "calls")) else []

    def run_fail(*args, bind=True, e=benv):
        """A start that must refuse: (exit code, its stderr)."""
        p, port, log = serve(*args, e=e, bind=bind)
        if port is not None:
            stop(p)
            return None, text(log)
        return p.wait(10), text(log)

    def text(path):
        with open(path, errors="replace") as f:
            return f.read()

    def conn(port, src=None):
        s = socket.create_connection(("127.0.0.1", port), timeout=5,
                                     source_address=(src, 0) if src else None)
        return s

    def send(s, t, payload=b""):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        s.sendall(struct.pack(">BI", t, len(payload)) + payload)

    def recv_exact(s, n):
        b = b""
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise EOFError
            b += c
        return b

    def frame(s):
        h = recv_exact(s, 5)
        t, n = struct.unpack(">BI", h)
        return t, recv_exact(s, n)

    def frame_not(s, skip=(0x10,)):
        while True:
            t, p = frame(s)
            if t not in skip:
                return t, p

    def reply(port, hello=True):
        """What a peer gets, having sent a hello (or nothing): "accept"
        (0x18), "closed" (EOF with no byte at all), "reset: ..." (no byte
        either), the raw payload of a 0x13 followed by EOF (#094), else a
        string saying what came."""
        s = conn(port)
        try:
            if hello:
                send(s, 0x08)
            t, body = frame(s)
            if t == 0x18:
                return "accept"
            if t != 0x13:
                return "frame 0x%02x" % t
            return body if s.recv(1) == b"" else "more after the 0x13"
        except EOFError:
            return "closed"
        except (ConnectionResetError, BrokenPipeError) as e:
            return "reset: %s" % e
        except socket.timeout:
            return "timeout"
        finally:
            s.close()

    def refused(port):
        return reply(port) != "accept"

    def refusal(code):
        """The exact 0x13 payload dch serve sends for code (#094)."""
        return json.dumps({"code": code, "message": CODES[code]},
                          separators=(",", ":")).encode()

    def sessions():
        r = subprocess.run([DCH, "-ls"], env=env, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.split()

    def ctl1(port, argv, s=None):
        """One 0x30 of argv: ("ran", exit, why, stdout, stderr) at its 0x33,
        or ("refused", message) at a 0x13."""
        s = s or conn(port)
        out, err = b"", b""
        try:
            send(s, 0x30, {"argv": argv})
            while True:
                t, pl = frame(s)
                if t == 0x13:
                    return "refused", json.loads(pl).get("message")
                if t == 0x31:
                    out += pl
                elif t == 0x32:
                    err += pl
                elif t == 0x33:
                    j = json.loads(pl)
                    return "ran", j["exit"], j.get("why"), out, err
        except (EOFError, OSError) as e:
            return "lost", str(e)
        finally:
            s.close()

    def xray_checks(port, log):
        """#157 (spec #832 4.4, 9.5): the --xray allowlist entries on a dch
        serve without --allow-create. Each accepted form runs the host's
        dch, its bytes and exit those of the same argv run locally; each
        refused one is a 0x13 naming 3.1's rule, nothing run."""
        def chk(msg, cond, extra):
            check(msg, cond)
            if not cond:
                print("       %r" % (extra,))

        n = "svx-none"		# no such session: every run ends at once
        bidi = ["\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
                "\u202d", "\u202e", "\u2066", "\u2067", "\u2068",
                "\u2069"]
        url = "https://example.com/" + "a" * 236		# 256 bytes, a ref
        ok = [["--json"], ["--follow", "--json"], ["--json", "--follow"],
              ["--clear", "--json"], ["--json", "--clear"],
              ["--ref", "#831", "--json"],
              ["--json", "--budget-ms", "50", "--full", "--scope",
               "session", "--ref", "#831"],
              ["--ref", url, "--scope", "session", "--json"],
              ["--ref", "#831", "--scope", "project", "--json"],
              ["--ref", "#831", "--scope", "all", "--json"],
              ["--ref", "#831", "--budget-ms", "300", "--json"],
              ["--chapter", "a", "--json"],
              ["--chapter", "Az09:_.-", "--json"],
              ["--chapter", "-f", "--json"],
              ["--chapter", "c" * 128, "--json"]]
        names = ["9f9f9f9f-0000-4000-8000-000000000000", "rec.json",
                 "m0123456789abcdef"]
        no = [([], "--xray needs --json (the view is client-side only)"),
              (["--follow"], "--xray needs --json (the view is "
               "client-side only)"),
              (["--requests", "--json"],
               "--requests is not allowed with --xray"),
              (["--json", "--json"], "--json twice"),
              (["--clear", "--json", "--clear"], "--clear twice"),
              (["--ref", "#1", "--ref", "#2", "--json"], "--ref twice"),
              (["--json", "--ref"], "--ref needs a value"),
              (["--follow", "--ref", "#1", "--json"],
               "--ref, --follow, --chapter and --clear exclude each other"),
              (["--clear", "--chapter", "a", "--json"],
               "--ref, --follow, --chapter and --clear exclude each other"),
              (["--clear", "--full", "--json"],
               "--scope, --full and --budget-ms come with --ref"),
              (["--scope", "session", "--json"],
               "--scope, --full and --budget-ms come with --ref"),
              (["--json", "--config"], "--config is not allowed"),
              (["--json", "--cwd"], "--cwd is not allowed with --xray"),
              (["--json", "other"], "unexpected other after --xray"),
              (["--ref", "", "--json"], "bad ref"),
              (["--ref", url + "a", "--json"], "bad ref"),
              (["--ref", "#1\x01", "--json"], "bad ref"),
              (["--ref", "#1\x1b", "--json"], "bad ref"),
              (["--ref", "#1\x7f", "--json"], "bad ref"),
              (["--ref", "#1\x85", "--json"], "bad ref"),
              (["--ref", "#1\x9f", "--json"], "bad ref"),
              (["--ref", "x", "--json"], "not a ref"),
              # dash-led: the value, not a flag (else "--json twice")
              (["--ref", "--json", "--json"], "not a ref"),
              (["--chapter", "", "--json"], "bad anchor"),
              (["--chapter", "c" * 129, "--json"], "bad anchor"),
              (["--chapter", "a/b", "--json"], "bad anchor"),
              (["--chapter", "a b", "--json"], "bad anchor"),
              (["--chapter", "\u00e9", "--json"], "bad anchor"),
              (["--ref", "#1", "--scope", "Session", "--json"], "bad scope"),
              (["--ref", "#1", "--scope", "", "--json"], "bad scope"),
              (["--ref", "#1", "--scope", "none", "--json"], "bad scope")]
        no += [(["--ref", "#1" + b, "--json"], "bad ref") for b in bidi]
        no += [(["--ref", "#1", "--budget-ms", b, "--json"], "bad budget")
               for b in ("49", "301", "+50", "0x40", "4294967346", "",
                         " 50", "-50")]
        bad_names = [("-x", "a session name starting with - can't be "
                      "passed to dch"),
                     ("a:b", "a session name with ':' is local only"),
                     ("a/b", "invalid session name"),
                     ("", "invalid session name"),
                     ("a\x1bb", "invalid session name")]
        before = text(log).count(" ctl --xray ")
        for a in ok:
            got = ctl1(port, ["--xray", n] + a)
            loc = subprocess.run([DCH, "--xray", n] + a, env=env,
                                 capture_output=True, timeout=20)
            check("xray serve: --xray <n> %s runs, as local (exit %d)"
                  % (" ".join(x[:12] for x in a), loc.returncode),
                  got == ("ran", loc.returncode, None, loc.stdout,
                          loc.stderr))
        for nm in names:
            a = ["--clear", "--json"] if nm[0] == "m" else ["--json"]
            got = ctl1(port, ["--xray", nm] + a)
            loc = subprocess.run([DCH, "--xray", nm] + a, env=env,
                                 capture_output=True, timeout=20)
            check("xray serve: the %s name form runs, as local" % nm,
                  got == ("ran", loc.returncode, None, loc.stdout,
                          loc.stderr))
        ran = text(log).count(" ctl --xray ")
        for a, why in no:
            got = ctl1(port, ["--xray", n] + a)
            check("xray serve: %r refused: %s" % (a, why),
                  got == ("refused", why))
        for nm, why in bad_names:
            got = ctl1(port, ["--xray", nm, "--json"])
            check("xray serve: name %r refused: %s" % (nm, why),
                  got == ("refused", why))
        check("xray serve: every accepted form ran a dch, no refused one",
              ran - before == len(ok) + len(names)
              and text(log).count(" ctl --xray ") == ran)

        # a live session: --clear (owner-gated like -k, no --allow-create)
        # and --follow, which no cap and no timeout cut; a 0x34 and a
        # close each end it
        subprocess.run([DCH, "--spawn", "svx", "--size", "40x5", "--",
                        "/bin/sh"], env=env, capture_output=True, timeout=15)
        subprocess.run([DCH, "--send", "svx", "--", "echo svx-mark"],
                       env=env, capture_output=True, timeout=15)
        time.sleep(0.5)
        got = ctl1(port, ["--xray", "svx", "--clear", "--json"])
        loc = subprocess.run([DCH, "--xray", "svx", "--clear", "--json"],
                             env=env, capture_output=True, timeout=20)
        chk("xray serve: --clear on a live session, create off: as local",
              got == ("ran", loc.returncode, None, loc.stdout, loc.stderr)
              and (not mirror or got[1] == 0), (got, loc))

        def follow(s):
            send(s, 0x30, {"argv": ["--xray", "svx", "--follow", "--json"]})
            t, pl = frame(s)
            m = None
            end = time.time() + 5
            while time.time() < end and not m:
                m = re.findall(r"ctl --xray svx \(pid (\d+)\)", text(log))
                time.sleep(0.05)
            return t, pl, int(m[-1]) if m else None

        def gone(pid):
            end = time.time() + 8
            while time.time() < end:
                try:
                    os.kill(pid, 0)	# a probe only
                except ProcessLookupError:
                    return True
                time.sleep(0.05)
            return False

        s = conn(port)
        t, pl, pid = follow(s)
        send(s, 0x34)
        try:
            t2, p2 = frame_not(s, (0x31, 0x32))
        except (EOFError, OSError):
            t2, p2 = None, b""
        s.settimeout(0.5)
        try:
            more = s.recv(1)
        except socket.timeout:
            more = None
        s.close()
        want = b'{"schema":1,"snapshot":{"schema":1,"session":"svx"'
        chk("xray serve: --follow streams 0x31; a 0x34 ends its dch with "
              "exactly one 0x33 cancel",
              # lite too: a ringless shell's --follow streams its corpus
              # none snapshot and waits for an agent (it never exits alone)
              t == 0x31 and pid and gone(pid) and pl.startswith(want)
              and (t2, p2) == (0x33, b'{"exit":2,"why":"cancel"}')
              and more is None, (t, pl[:60], t2, p2))
        if mirror:
            s = conn(port)
            t, pl, pid = follow(s)
            s.close()
            check("xray serve: a peer close ends the --follow dch (one "
                  "ctl done after the close)", t == 0x31 and pid
                  and gone(pid) and text(log).count(
                      "ctl done after the close") == 1)
        subprocess.run([DCH, "-k", "svx"], env=env, capture_output=True,
                       timeout=15)

        # the owner gate (whois) comes first: another user's peer never
        # reaches --clear (a server of its own: the refusal is cached)
        ts(who=whois(7, "other@example.com"))
        gp, gport, glog = serve()
        got = ctl1(gport, ["--xray", "svx", "--clear", "--json"])
        stop(gp)
        ts()
        chk("xray serve: --clear from another user's node: not_owner, "
              "nothing run", got == ("refused", CODES["not_owner"])
              and " ctl " not in text(glog), got)

        # DCH_CTL_CAP: --json is cut at the cap, --follow is not (4.4)
        cp, cport, clog = serve(e=dict(benv, DCH_CTL_CAP="64"))
        subprocess.run([DCH, "--spawn", "svc", "--size", "40x5", "--",
                        "/bin/sh"], env=env, capture_output=True, timeout=15)
        time.sleep(0.3)
        got = ctl1(cport, ["--xray", "svc", "--json"])
        chk("xray serve: --json under DCH_CTL_CAP=64 is cut (why cap)",
              got[:3] == ("ran", 2, "cap") and len(got[3]) == 64, got)
        if mirror:
            s = conn(cport)
            send(s, 0x30, {"argv": ["--xray", "svc", "--follow", "--json"]})
            out, t = b"", None
            s.settimeout(3)
            try:
                while len(out) < 200:
                    t, pl = frame(s)
                    if t != 0x31:
                        break
                    out += pl
            except (EOFError, OSError):
                pass
            s.close()
            chk("xray serve: --follow under DCH_CTL_CAP=64 streams on "
                  "past it", len(out) >= 200 and t == 0x31, (t, out[:80]))
        stop(cp)
        subprocess.run([DCH, "-k", "svc"], env=env, capture_output=True,
                       timeout=15)

    def follow_cap_checks():
        """#202 (QA 1.56.0 LOW-R4, spec v3.2.4): a device (its address)
        runs at most 2 --xray --follow of one session; a third closes its
        oldest, which ends as a timeout cut does (0x33 exit 2, "timeout").
        The master's 4 subscriber slots stay as they are: a fifth of
        another identity is still busy (QA 1.52.0 LOW-7)."""
        ts()
        p, port, log = serve()
        want = b'{"schema":1,"snapshot":{"schema":1,"session":"'
        for n in ("svf", "svg"):
            subprocess.run([DCH, "--spawn", n, "--size", "40x5", "--",
                            "/bin/sh"], env=env, capture_output=True,
                           timeout=15)
        time.sleep(0.5)
        locs, evn = [], [0]

        def ran(n):
            return text(log).count(" ctl --xray %s (pid" % n)

        def fl(n="svf", src=None, order=("--follow", "--json"), read=True):
            """A follow from src: its socket, once its dch runs (and, read,
            with its first frame, the snapshot, in fl.first)."""
            k = ran(n)
            s = conn(port, src)
            send(s, 0x30, {"argv": ["--xray", n] + list(order)})
            end = time.time() + 8
            while time.time() < end and ran(n) <= k:
                time.sleep(0.05)
            fl.first = frame(s) if read else None
            return s

        def end_of(s, secs):
            """The 0x33 that ends s's follow within secs, else None."""
            s.settimeout(secs)
            try:
                t, pl = frame_not(s, (0x31, 0x32))
                return (t, pl)
            except socket.timeout:
                return None
            except (EOFError, OSError) as e:
                return ("lost", str(e))

        def event(socks):
            """Each of socks gets a 0x31 (a ref delta) after a line in svf
            that names a new ref."""
            evn[0] += 1
            subprocess.run([DCH, "--send", "svf", "--", "echo see o/r#%d"
                            % (202000 + evn[0])], env=env,
                           capture_output=True, timeout=15)
            got = []
            for s in socks:
                s.settimeout(6)
                try:
                    got.append(frame(s)[0] == 0x31)
                except (socket.timeout, EOFError, OSError):
                    got.append(False)
            return all(got)

        def local():
            """A local --xray svf --json --follow: "following" while it
            runs (its slot), else its exit and stderr."""
            out = os.path.join(tmp, "loc%d" % len(locs))
            lp = subprocess.Popen([DCH, "--xray", "svf", "--json",
                                   "--follow"], env=env,
                                  stdin=subprocess.DEVNULL,
                                  stdout=open(out, "w"),
                                  stderr=subprocess.PIPE)
            locs.append(lp)
            try:
                return lp.wait(3), lp.stderr.read()
            except subprocess.TimeoutExpired:
                return "following", text(out)[:len(want)].encode()

        timeout = (0x33, b'{"exit":2,"why":"timeout"}')
        # three of one session from one device: the oldest ends as a
        # timeout cut; the two newest (one with its flags the other way
        # round) still run and still deliver an event
        a = fl()
        b = fl(order=("--json", "--follow"))
        c = fl()
        firsts = fl.first
        ea = end_of(a, 5)
        eb, ec = end_of(b, 1), end_of(c, 1)
        check("#202: a third --xray --follow of a session from one device "
              "ends its oldest: 0x33 exit 2, why timeout", ea == timeout)
        check("#202: the two newest still run (no 0x33)",
              eb is None and ec is None and firsts[0] == 0x31
              and firsts[1].startswith(want))
        if mirror:
            check("#202: the two newest still deliver an event",
                  event([b, c]))
        check("#202: the parent says which it closed",
              "the device's oldest --xray --follow of svf" in text(log))
        # another session's follows from the same device do not count
        g1, g2 = fl("svg"), fl("svg")
        check("#202: follows of another session from the device do not "
              "count with these", end_of(b, 1.5) is None
              and end_of(c, 0.2) is None and end_of(g1, 0.2) is None
              and end_of(g2, 0.2) is None)
        for s in (a, b, c, g1, g2):
            s.close()
        time.sleep(1)
        # QA 1.58.0 LOW-R6: counted by the session the name opens. On a
        # case-insensitive volume SVF opens svf: svf x2 then SVF x2 close
        # both svf; on a case-sensitive one SVF is a session of its own and
        # nothing is closed
        sd = os.path.join(tmp, "dch-%d" % os.getuid())
        alias = os.path.exists(os.path.join(sd, "SVF.sock"))
        if not alias:
            subprocess.run([DCH, "--spawn", "SVF", "--size", "40x5", "--",
                            "/bin/sh"], env=env, capture_output=True,
                           timeout=15)
            time.sleep(0.5)
        print("#202 LOW-R6: the socket dir is case-%s" %
              ("insensitive" if alias else "sensitive"))
        k = text(log).count("--follow of SVF")
        a, b = fl(), fl()
        c, d = fl("SVF"), fl("SVF")
        if alias:
            check("#202 LOW-R6: svf x2 then SVF x2 from one device (one "
                  "session, case-insensitive): both svf closed, the SVF "
                  "run", end_of(a, 5) == timeout and end_of(b, 5) == timeout
                  and end_of(c, 0.5) is None and end_of(d, 0.2) is None
                  and text(log).count("--follow of SVF") == k + 2)
            if mirror:
                check("#202 LOW-R6: ... and a local --xray --follow finds a "
                      "slot", local() == ("following", want))
        else:
            check("#202 LOW-R6: svf x2 and SVF x2 from one device (two "
                  "sessions, case-sensitive): none closed",
                  all(end_of(s, 1 if s is a else 0.2) is None
                      for s in (a, b, c, d))
                  and text(log).count("--follow of SVF") == k)
        for s in (a, b, c, d):
            s.close()
        for lp in locs:
            lp.terminate()
            lp.wait(5)
        del locs[:]
        if not alias:
            subprocess.run([DCH, "-k", "SVF"], env=env, capture_output=True,
                           timeout=15)
        time.sleep(1.5)
        # another device: its own 2, never closing this device's
        try:
            with socket.socket() as t:
                t.bind(("127.0.0.2", 0))
            two = True
        except OSError:
            two = False
            print("skip #202 two devices (no 127.0.0.2 here: macOS lo0 "
                  "has only 127.0.0.1 unless aliased)")
        if two:
            d1, d2 = fl(), fl()
            o1, o2 = fl(src="127.0.0.2"), fl(src="127.0.0.2")
            check("#202: two devices, 2 follows each: none closed",
                  all(end_of(s, 1 if s is d1 else 0.2) is None
                      for s in (d1, d2, o1, o2)))
            o3 = fl(src="127.0.0.2")
            check("#202: a third from one device closes its own oldest "
                  "only", end_of(o1, 5) == timeout
                  and all(end_of(s, 0.5 if s is d1 else 0.2) is None
                          for s in (d1, d2, o2, o3)))
            for s in (d1, d2, o1, o2, o3):
                s.close()
            time.sleep(1)
        if mirror:
            # 4 that never read, from one device: 2 master slots stay free
            k = text(log).count("--follow of svf")
            st = [fl(read=False) for _ in range(4)]
            end = time.time() + 8
            while time.time() < end and \
                    text(log).count("--follow of svf") < k + 2:
                time.sleep(0.05)
            time.sleep(0.5)
            check("#202: a device's 4 stalled follows leave a local "
                  "--xray --follow a slot (not busy, LOW-R4)",
                  local() == ("following", want))
            for s in st:
                s.close()
            for lp in locs:
                lp.terminate()
                lp.wait(5)
            del locs[:]
            time.sleep(1.5)
            # 2 local followers and 2 of the device fill the 4 slots: the
            # device's third does not get busy for its own closing one
            l1, l2 = local(), local()
            a, b = fl(), fl()
            c = fl()
            firsts = fl.first
            check("#202: with the 4 slots full, the device's third gets "
                  "its snapshot, not busy",
                  l1 == l2 == ("following", want) and firsts[0] == 0x31
                  and firsts[1].startswith(want) and end_of(a, 5) == timeout
                  and end_of(c, 0.5) is None)
            check("#202: ... and runs on: an event reaches it", event([b, c]))
            check("#202: a fifth of another identity is still busy "
                  "(LOW-7 stands)", local()[0] == 1)
            for s in (a, b, c):
                s.close()
            for lp in locs:
                lp.terminate()
                lp.wait(5)
            del locs[:]
            time.sleep(1.5)
            # 4 local followers: the device's first is busy, after one run
            # more (its own older one might have been closing)
            ls = [local() for _ in range(4)]
            k = text(log).count("busy at its start")
            got = ctl1(port, ["--xray", "svf", "--follow", "--json"])
            check("#202: the slots full of others: a device's follow is "
                  "busy, once retried",
                  all(x == ("following", want) for x in ls)
                  and got == ("ran", 1, None, b"", b"dch: busy\n")
                  and text(log).count("busy at its start") == k + 1)
            for lp in locs:
                lp.terminate()
                lp.wait(5)
        stop(p)
        for n in ("svf", "svg"):
            subprocess.run([DCH, "-k", n], env=env, capture_output=True,
                           timeout=15)

    try:
        mirror = subprocess.run([DCH, "--build"], env=env, capture_output=True,
                                text=True).stdout.strip() == "full"

        # --- the goldens, every fixture, --mutating ----------------------
        # dch's own (17+, #114) with a "dch_serve" key need a server started
        # their way: plain run.py skips them (their requires_op is an op no
        # server names), so each group runs here against its own dch serve.
        # --read, --keys, --follow (dch_mirror) are the full build's.
        groups = {}
        gdir = os.path.dirname(RUNPY)
        for name in sorted(os.listdir(gdir)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(gdir, name)) as f:
                fx = json.load(f)
            if fx.get("dch_mirror") and not mirror:
                continue
            key = json.dumps(fx.get("dch_serve"), sort_keys=True)
            if "dch_serve" in fx:
                fx["requires_op"] = "ctl"
            groups.setdefault(key, []).append(fx)

        def golden(port, fxs, tag):
            d = os.path.join(tmp, "golden-" + tag)
            os.makedirs(d)
            for fx in fxs:
                with open(os.path.join(d, fx["id"] + ".json"), "w") as f:
                    json.dump(fx, f)
            r = subprocess.run([sys.executable, RUNPY, "127.0.0.1:%d" % port,
                                "--mutating", "--fixtures", d],
                               capture_output=True, text=True, timeout=180)
            if r.returncode or r.stdout.count("PASS ") != len(fxs):
                print(r.stdout + r.stderr)
            return r.returncode == 0 and r.stdout.count("PASS ") == len(fxs)

        ts()
        p, port, log = serve("--allow-create")
        check("serves on a loopback test port", port is not None)
        main_fx = groups.pop("null")
        check("golden fixtures 01-15, 17-34 and 39-42 pass (--mutating, %d)"
              % len(main_fx), len(main_fx) == 37 - 7 * (not mirror)
              and golden(port, main_fx, "main"))
        check("the bypass is logged loudly",
              "WARNING: DCH_SERVE_TEST_LOOPBACK=1" in text(log))
        for n, (key, fxs) in enumerate(sorted(groups.items())):
            cfg = json.loads(key)
            gp, gport, _ = serve(*cfg["args"], e=dict(benv, **cfg["env"]))
            check("golden %s pass against dch serve %s %s"
                  % (", ".join(fx["id"][:2] for fx in fxs),
                     " ".join(cfg["args"]) or "(no flags)",
                     " ".join("%s=%s" % kv for kv in cfg["env"].items())),
                  gport is not None and golden(gport, fxs, str(n)))
            stop(gp)
        check("golden 35-38 and 43: every server-config group ran",
              sum(len(v) for v in groups.values()) == 5 - 2 * (not mirror))

        # --- QA 1.55.0 LOW-5 (#180): TCP keepalive on serve's connections,
        # so a peer gone with no FIN (its idle --follow holding a session's
        # subscriber slots) is closed in about 30 s. lsof -T f shows socket
        # options on macOS; where it does not, the check is skipped.
        ka = conn(port)
        send(ka, 0x08, {"proto": "1.0"})
        frame(ka)
        try:
            lo = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-T", "f"],
                                capture_output=True, text=True,
                                timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            lo = ""
        side = [ln for ln in lo.splitlines()
                if "127.0.0.1:%d->" % port in ln]
        if "SO=" in lo:
            # lsof prints the keepalive timer in ms (15022 seen for 15 s)
            ka_ms = [re.search(r"KEEPALIVE=(\d+)", ln) for ln in side]
            check("LOW-5: serve's end of a connection has SO_KEEPALIVE "
                  "(15 s idle)", side and all(
                      m and 14000 <= int(m.group(1)) <= 16000
                      for m in ka_ms))
        else:
            print("  skip LOW-5: lsof shows no socket options here")
        ka.close()

        # --- 0x18 ---------------------------------------------------------
        s = conn(port)
        send(s, 0x08, {"proto": "1.0"})
        t, pl = frame(s)
        info = json.loads(pl)
        check("0x18 hello keys", t == 0x18 and info.get("server") == "dch"
              and info.get("proto") == "1.1" and info.get("hostname")
              and info.get("name") and info.get("os")
              and re.fullmatch(r"[0-9a-f]{64}", info.get("device_id", "")))
        check("ops lists create (and ctl, ctl-cancel, xray) with "
              "--allow-create", info.get("ops")
              == ["create", "ctl", "ctl-cancel", "watch", "xray"])
        send(s, 0x08, {"proto": "2.0"})
        t, pl = frame(s)
        check("a newer proto major gets 0x13",
              t == 0x13 and "newer" in json.loads(pl)["message"])
        # an oversize control frame (over the 64 KiB guard) closes
        s.sendall(struct.pack(">BI", 0x03, 70000))
        got = b""
        try:
            while True:
                c = s.recv(65536)
                if not c:
                    break
                got += c
        except (ConnectionResetError, socket.timeout):
            pass
        check("an oversize control frame closes the connection",
              got.startswith(b"\x13") or got == b"")
        s.close()
        dev1 = info["device_id"]

        # --- ctl (#114): its own connection; a client close ends its dch --
        subprocess.run([DCH, "--spawn", "svc-ctl", "--size", "20x2", "--",
                        "/bin/cat"], env=env, capture_output=True, timeout=15)
        s = conn(port)
        send(s, 0x01, {"name": "svc-ctl", "cols": 20, "rows": 2})
        frame(s)
        send(s, 0x30, {"argv": ["--status", "svc-ctl"]})
        t, pl = frame_not(s)
        check("a 0x30 on an attached connection is a 0x13",
              t == 0x13 and json.loads(pl)["message"]
              == "ctl needs its own connection")
        s.close()
        if mirror:
            # --follow runs until stopped (the mirror's: lite has none)
            s = conn(port)
            send(s, 0x30, {"argv": ["--follow", "svc-ctl"]})
            t, _ = frame(s)
            s.close()
            gone, end = False, time.time() + 5
            while time.time() < end and not gone:
                m = re.search(r"ctl --follow svc-ctl \(pid (\d+)\)",
                              text(log))
                if m and "closed by the client; stopping its ctl" in \
                        text(log):
                    try:
                        os.kill(int(m.group(1)), 0)	# a probe only
                    except ProcessLookupError:
                        gone = True
                time.sleep(0.05)
            check("--follow streams; the client's close ends that dch",
                  t == 0x31 and gone)
            # #118: a 0x34, then the FIN at once: the 0x33 still comes
            s = conn(port)
            send(s, 0x30, {"argv": ["--follow", "svc-ctl"]})
            frame(s)
            send(s, 0x34)
            s.shutdown(socket.SHUT_WR)
            try:
                t, pl = frame_not(s, (0x10, 0x31))
            except (EOFError, OSError):
                t, pl = None, b""
            check("a 0x34 then the FIN: 0x33 why cancel, then the close",
                  t == 0x33 and json.loads(pl) == {"exit": 2, "why": "cancel"}
                  and s.recv(1) == b"")
            s.close()
        subprocess.run([DCH, "-k", "svc-ctl"], env=env, capture_output=True,
                       timeout=15)

        # --- #124: a CTL_LONG verb (--say here) cut by the close or a 0x34
        # gets CTL_LONG_MS after its SIGTERM, not 2 s; any other verb
        # (--status) is SIGKILLed 2 s after. A `dch` first on serve's PATH
        # stands in for those two (the rest it execs): this measures serve's
        # grace, not dch. The real --run and --say under the cut: #126 below.
        wrap = os.path.join(tmp, "wrap")
        os.makedirs(wrap)
        with open(os.path.join(wrap, "dch"), "w") as f:
            f.write("""#!/bin/sh
W=%s
case "$1 $2" in
"--say slow"|"--status slow")
    v=${1#--}; rm -f "$W/$v.log"
    if [ "$v" = say ]; then
        trap 'echo term >> "$W/$v.log"; sleep 4; echo done >> "$W/$v.log"
              exit 0' TERM
    else
        trap 'echo term >> "$W/$v.log"' TERM
    fi
    echo $$ > "$W/$v.pid"
    echo started
    while :; do sleep 0.1; done ;;
"--status big")
    exec %s -c 'import sys
for i in range(96): sys.stdout.buffer.write(b"x" * 65535 + b"\\n")
sys.stdout.buffer.write(b"END\\n")' ;;
esac
exec %s "$@"
""" % (wrap, sys.executable, DCH))
        os.chmod(os.path.join(wrap, "dch"), 0o755)

        def slow_cut(port, verb, cancel):
            """Cut a slow verb (cancel: a 0x34, else the close): seconds
            from its SIGTERM to its end, and whether it finished its own
            SIGTERM work."""
            argv = ["--say", "slow", "--json", "--", "hi"] if verb == "say" \
                else ["--status", "slow"]
            vlog = os.path.join(wrap, verb + ".log")
            s = conn(port)
            send(s, 0x30, {"argv": argv})
            frame(s)		# "started": the trap is set
            pid = int(text(os.path.join(wrap, verb + ".pid")))
            if cancel:
                send(s, 0x34)
            else:
                s.close()
            t0, gone = time.time(), None
            while time.time() - t0 < 15 and gone is None:
                try:
                    os.kill(pid, 0)	# a probe only
                except ProcessLookupError:
                    gone = time.time()
                time.sleep(0.05)
            if cancel:
                try:
                    t, pl = frame_not(s, (0x31,))
                except (EOFError, OSError):
                    t, pl = None, b""
                ok = t == 0x33 and json.loads(pl) == {"exit": 2,
                                                       "why": "cancel"}
                s.close()
            else:
                ok = True
            got = text(vlog) if os.path.exists(vlog) else ""
            return ok and "term" in got, \
                gone - t0 if gone else None, "done" in got

        p, sport, slog_ = serve(argv0="dch", e=dict(
            benv, PATH="%s:%s:/usr/bin:/bin" % (wrap, fake)))
        check("#124: a serve whose ctls run the stand-in dch starts",
              sport is not None)
        if sport is not None:
            for cancel, how in ((False, "the close"), (True, "a 0x34")):
                ok, secs, done = slow_cut(sport, "say", cancel)
                check("#124: --say cut by %s: SIGTERM, not SIGKILLed at 2 s, "
                      "finishes on its own (%s s)" % (how, secs and
                                                       round(secs, 1)),
                      ok and secs is not None and secs >= 3.5 and done)
                ok, secs, done = slow_cut(sport, "status", cancel)
                check("#124: --status cut by %s: SIGTERM, SIGKILLed about "
                      "2 s later (%s s)" % (how, secs and round(secs, 1)),
                      ok and secs is not None and 1.5 <= secs <= 3.5
                      and not done)
            stop(p)

        # --- #201: a one-shot ctl's answer past the socket and pipe buffers
        # (6 MiB of the stand-in's --status big) comes whole to a reader
        # that stalls 30 s, past STALL_MS's 20 s: a ctl with a timeout is
        # bounded by it, its output may sit untaken until 20 s past it. A
        # peer that never reads still frees its 16 slots by then: here a
        # serve of its own with DCH_CTL_TIMEOUT_MS 3 s, so by about 23 s.
        # Both run at once, to keep the wall time near 31 s.
        wenv = dict(benv, PATH="%s:%s:/usr/bin:/bin" % (wrap, fake))
        p, sport, slog_ = serve(argv0="dch", e=wenv)
        q, qport, qlog = serve(argv0="dch", e=dict(
            wenv, DCH_CTL_TIMEOUT_MS="3000"))
        check("#201: two serves whose ctls run the stand-in dch start",
              sport is not None and qport is not None)
        if sport is not None and qport is not None:
            big = ["--status", "big"]
            s = conn(sport)
            send(s, 0x30, {"argv": big})
            t, pl = frame(s)
            out = bytearray(pl if t == 0x31 else b"")
            never = []
            for _ in range(16):
                never.append(conn(qport))
                send(never[-1], 0x30, {"argv": big})
            time.sleep(1)
            full = reply(qport)
            time.sleep(29)
            end = None
            s.settimeout(30)
            try:
                while True:
                    t, pl = frame(s)
                    if t == 0x31:
                        out += pl
                    elif t == 0x33:
                        end = json.loads(pl)
                        break
            except (EOFError, OSError):
                pass
            s.close()
            check("#201: a 6 MiB answer whose reader stalls 30 s comes whole "
                  "(%d bytes), then 0x33 exit 0" % len(out),
                  len(out) == 96 * 65536 + 4 and out.endswith(b"x\nEND\n")
                  and end == {"exit": 0}
                  and "not taken" not in text(slog_))
            ql = text(qlog)
            check("#201: 16 peers that never read hold the peer's 16 slots "
                  "(a 17th busy), then all are free 20 s past their timeout: "
                  "16 timeout cuts, 16 closes",
                  isinstance(full, bytes) and b'"busy"' in full
                  and ql.count("ctl cut short: timeout") == 16
                  and ql.count("output not taken for 20 s") == 16
                  and reply(qport) == "accept")
            for n in never:
                n.close()
            stop(p)
            stop(q)

        # --- #126: the real dch --run and --say, cut by the close or a 0x34
        # with their text in a fake Claude's composer (every Enter eaten,
        # so the gate is still re-Entering): the text is taken back out,
        # nothing is sent, and the dch ends at once, not 660 s later.
        if mirror:
            fb, fc = os.path.join(tmp, "fbin"), os.path.join(tmp, "fc")
            for d in (fb, fc, os.path.join(home, ".claude", "sessions")):
                os.makedirs(d, exist_ok=True)
            shutil.copy(os.path.join(HERE, "fake_claude.py"),
                        os.path.join(fb, "claude"))
            os.chmod(os.path.join(fb, "claude"), 0o755)
            cenv = dict(env, FAKE_CLAUDE_DIR=fc, DCH_AGENT_REENTER_MS="5000",
                        PATH="%s:%s" % (fb, env["PATH"]))
            p, cport, clog = serve(e=dict(benv, **{k: cenv[k] for k in (
                "FAKE_CLAUDE_DIR", "DCH_AGENT_REENTER_MS", "PATH")}))
            check("#126: a serve for the real --run/--say cut starts",
                  cport is not None)

            def screen(n):
                return subprocess.run([DCH, "--read", n], env=cenv,
                                      capture_output=True, text=True,
                                      timeout=15).stdout

            def composer(n, t):
                return any(l.startswith("\u276f") and
                           t in l.replace("\xa0", " ")
                           for l in screen(n).splitlines())

            def wait(fn, secs):
                end = time.time() + secs
                while time.time() < end and not fn():
                    time.sleep(0.1)
                return fn()

            def empty(n):
                return '\u276f Try "fix the tests"' in \
                    screen(n).replace("\xa0", " ")

            # #128: the last --say's first line is short, so "Hi," is all
            # that is left before the last Ctrl-Us; it must still go
            for k, (verb, cancel, msg) in enumerate(
                    (("run", False, None), ("run", True, None),
                     ("say", False, None), ("say", True, None),
                     ("say", False, "Hi,\n\nThanks! Please review the "
                      "change on the branch and tell me what breaks."))
                    if cport else ()):
                n = "svc-cl%d" % k
                with open(os.path.join(fc, n + ".ctl"), "w") as f:
                    json.dump({"eat_enters": 99}, f)
                subprocess.run([DCH, "--spawn", n, "--size", "100x30",
                                "claude"], env=cenv, capture_output=True,
                               timeout=15)
                up = wait(lambda: "auto mode on" in screen(n), 15)
                msg = msg or "cut %s %d" % (verb, k)
                argv = ["--run", n, "--"] + msg.split(" ") if verb == "run" \
                    else ["--say", n, "--json", "--"] + msg.split(" ")
                s = conn(cport)
                send(s, 0x30, {"argv": argv})
                held = wait(lambda: composer(n, msg.split("\n")[0]), 15)
                t0 = time.time()
                if cancel:
                    send(s, 0x34)
                    try:
                        s.settimeout(30)
                        t, pl = frame_not(s, (0x31, 0x32))
                    except (EOFError, OSError):
                        t, pl = None, b""
                    ended = t == 0x33 and json.loads(pl) == {
                        "exit": 2, "why": "cancel"}
                else:
                    done = r"ctl done after the close: dch exit 1"
                    before = len(re.findall(done, text(clog)))
                    s.close()
                    ended = wait(lambda: len(re.findall(done, text(clog)))
                                 > before, 30)
                secs = time.time() - t0
                if cancel:
                    s.close()
                gone = wait(lambda: empty(n), 5)
                lines = []
                try:
                    with open(os.path.join(fc, n + ".log")) as f:
                        lines = [json.loads(l) for l in f if l.strip()]
                except OSError:
                    pass
                sent = [e for e in lines if e["ev"] == "line"]
                check("#126: the real --%s%s cut by %s mid-gate: taken back "
                      "out of the composer, nothing sent, dch ended (%.1f s%s)"
                      % (verb, " (#128: a short first line)" if "\n" in msg
                         else "", "a 0x34" if cancel else "the close", secs,
                         "" if gone and not sent else
                         "; the composer still holds it" if not gone else
                         "; it was sent"),
                      up and held and ended and gone and not sent and
                      secs < 10)
                subprocess.run([DCH, "-k", n], env=cenv, capture_output=True,
                               timeout=15)
            stop(p)

        # --- #118: a warm ctl connection takes no attach, watch or kill --
        for t0, body in ((0x01, {"name": "svc-x", "cols": 20, "rows": 2}),
                         (0x2e, {}), (0x04, {"name": "svc-x"})):
            s = conn(port)
            send(s, 0x30, {"argv": ["--history", "--json"]})
            t = frame_not(s, (0x31, 0x32))[0]
            send(s, t0, body)
            try:
                t2, pl = frame(s)
                gone = s.recv(1) == b""
            except (EOFError, OSError):
                t2, pl, gone = None, b"", False
            check("a warm ctl connection refuses 0x%02x, then the close" % t0,
                  t == 0x33 and t2 == 0x13 and gone and
                  b"only ctl frames" in pl)
            s.close()

        # --- half-close: the reply still comes ---------------------------
        s = conn(port)
        send(s, 0x03)
        s.shutdown(socket.SHUT_WR)
        t, pl = frame(s)
        check("a half-closed client still gets its 0x11", t == 0x11
              and isinstance(json.loads(pl), list))
        check("then the FIN", s.recv(1) == b"")
        s.close()

        # --- with --allow-create, attach is attach-or-create --------------
        s = conn(port)
        send(s, 0x01, {"name": "svc-made", "cols": 80, "rows": 24})
        t, _ = frame(s)
        check("create on: attach to a new name starts it",
              t == 0x10 and "svc-made" in sessions())
        s.close()

        # --- INFO-2 (#120): no new session takes a session_id's shape ----
        for typ, nm in ((0x01, "m0123456789abcdef"),
                        (0x02, "m0123456789abcdef"),
                        (0x01, " m0123456789abcdef"),
                        (0x02, "m0123456789abcdef "),
                        # LOW-1 (QA 1.45.1): any case
                        (0x02, "M0123456789ABCDEF"),
                        (0x01, "m0123456789ABCdef")):
            s = conn(port)
            send(s, typ, {"name": nm, "cols": 80, "rows": 24})
            t, pl = frame_not(s)
            check("%s refuses a name shaped like a session_id (%r)"
                  % ("attach-or-create" if typ == 1 else "create", nm),
                  t == 0x13 and "looks like a session_id" in
                  json.loads(pl)["message"] and
                  nm not in sessions())
            s.close()
        for alias in ("m0123456789abcdef", " m0123456789abcdef\t",
                      "M0123456789ABCDEF"):
            s = conn(port)
            send(s, 0x06, {"name": "svc-made", "alias": alias})
            t, pl = frame_not(s)
            check("0x06 rename refuses an alias shaped like a session_id "
                  "(%r: padding trimmed)" % alias,
                  t == 0x13 and "looks like a session_id" in
                  json.loads(pl)["message"])
            s.close()

        # --- create with a command, exit code ----------------------------
        s = conn(port)
        send(s, 0x02, {"name": "svc-create", "cols": 80, "rows": 24,
                       "command": ["/bin/sh", "-c", "exit 3"]})
        try:
            t, pl = frame_not(s)
            code = json.loads(pl).get("code") if t == 0x12 else None
            # the code is the dch client's (as Prosper's), not the command's
            check("create runs its command; its end is 0x12 {code}",
                  isinstance(code, int))
            check("then the FIN", s.recv(1) == b"")
        except (EOFError, socket.timeout):
            check("create runs its command; its end is 0x12 {code}", False)
        s.close()

        # --- retire: the newest attach wins ------------------------------
        r = subprocess.run([DCH, "--spawn", "svc-ret", "--size", "80x24",
                            "sh"], env=env, capture_output=True, timeout=30)
        check("spawned a session to attach", r.returncode == 0)
        # A lite build has no mirror to replay, and an idle sh (dash) does
        # not repaint on its own: a newline makes it print a prompt.
        a = conn(port)
        send(a, 0x01, {"name": "svc-ret", "cols": 80, "rows": 24})
        send(a, 0x10, b"\n")
        t, _ = frame(a)
        check("attach streams 0x10", t == 0x10)
        b = conn(port)
        send(b, 0x01, {"name": "svc-ret", "cols": 80, "rows": 24})
        send(b, 0x10, b"\n")
        t, _ = frame(b)
        got = []
        try:
            while True:
                t, pl = frame(a)
                if t != 0x10:
                    got.append((t, pl))
        except (EOFError, ConnectionResetError):
            got.append("eof")
        except socket.timeout:
            got.append("timeout")
        # #102: the retired attach is told why, its own class: no retry
        ok = got == [(0x13, TAKEN), "eof"]
        check("a newer attach retires the older: one 0x13 \"taken\", then "
              "EOF, no 0x12", ok)
        if not ok:
            print("       %r" % got)

        # --- list rows: meta allowlisted and re-encoded -------------------
        meta = {"prs": {"merged": 2, "open": [
            {"url": "https://github.com/o/r/pull/5", "title": "T"},
            {"url": "https://evil.example/o/r/pull/6", "title": "X"},
            {"url": "https://github.com/o/r/pull/7/files", "title": "Y"}]}}
        r = subprocess.run([DCH, "--meta", "svc-ret", json.dumps(meta)],
                           env=env, capture_output=True, timeout=10)
        c = conn(port)
        send(c, 0x03)
        t, pl = frame(c)
        c.close()
        rows = {x.get("name"): x for x in json.loads(pl)}
        row = rows.get("svc-ret", {})
        check("0x11 rows: meta keeps only github pull urls",
              r.returncode == 0 and row.get("meta") == {"prs": {
                  "merged": 2, "open": [{"num": 5, "repo": "o/r",
                                         "title": "T", "url":
                                         "https://github.com/o/r/pull/5"}]}})
        check("0x11 rows: keys sorted, compact",
              pl == json.dumps(json.loads(pl), sort_keys=True,
                               separators=(",", ":"),
                               ensure_ascii=False).encode())

        # --- 0x09 from the VT mirror -------------------------------------
        send(b, 0x10, b"echo serve-snap-mark\n")
        time.sleep(0.6)
        send(b, 0x09)
        t, pl = frame_not(b)
        if mirror:
            check("0x09 answers the screen plus the caret",
                  t == 0x19 and b"serve-snap-mark" in pl
                  and re.search(rb"\x1b\[\d+;\d+H$", pl))
        else:
            check("0x09 is empty with no mirror (lite)", t == 0x19 and pl == b"")
        b.close()
        a.close()

        # --- #101: 0x01 "scrollback" reaches the spawned client ----------
        r = subprocess.run([DCH, "--spawn", "svc-sb", "--size", "40x10", "--",
                            "sh", "-c", "seq 1 39; echo END; exec sh"],
                           env=env, capture_output=True, timeout=30)
        check("spawned a session with history", r.returncode == 0)

        end = time.time() + 10
        while time.time() < end and "END" not in subprocess.run(
                [DCH, "--read", "svc-sb"], env=env, capture_output=True,
                text=True, timeout=10).stdout and mirror:
            time.sleep(0.1)

        def sb_attach(extra):
            """The 0x10 bytes an attach with these extra keys gets."""
            s = conn(port)
            send(s, 0x01, dict({"name": "svc-sb", "cols": 40, "rows": 10},
                               **extra))
            send(s, 0x10, b"echo SB-$((6*7))\n")  # lite: no replay
            out, end = b"", time.time() + 10
            try:
                while time.time() < end and b"SB-42\r\n" not in out:
                    t, pl = frame(s)
                    if t == 0x10:
                        out += pl
            except (EOFError, socket.timeout):
                pass
            s.close()
            time.sleep(0.3)  # the retired client's exit, before the next
            return out

        with5 = sb_attach({"scrollback": 5})
        plain = sb_attach({})
        junk = sb_attach({"scrollback": "lots"})
        if mirror:
            m = re.search(rb"\x1b\[H\x1b\[J" + rb"(\d+)\r\n" * 4 +
                          rb"(\d+)\x1b\[0m\x1b\[999H\n{5}\x1b\[H\x1b\[J", with5)
            n = [int(x) for x in m.groups()] if m else []
            check("0x01 scrollback 5: 5 history rows above the screen",
                  n and n == list(range(n[0], n[0] + 5)) and n[0] > 20
                  and b"END" in with5)
            check("0x01 without scrollback: the screen alone",
                  b"\x1b[999H" not in plain and b"END" in plain)
            check("0x01 scrollback not a number: ignored",
                  b"\x1b[999H" not in junk and b"END" in junk)
        else:
            check("0x01 scrollback on lite: attaches, input round-trips",
                  b"SB-42" in with5 and b"SB-42" in plain)
        subprocess.run([DCH, "-k", "svc-sb"], env=env, capture_output=True,
                       timeout=15)

        # --- SIGTERM: exit 0, the session lives on -----------------------
        time.sleep(0.3)
        check("SIGTERM exits 0", stop(p) == 0)
        check("sessions outlive the server", "svc-ret" in sessions())
        subprocess.run([DCH, "-k", "svc-ret"], env=env, capture_output=True,
                       timeout=15)

        # --- create off by default; device_id stable ---------------------
        p, port, log = serve()
        s = conn(port)
        send(s, 0x08)
        info = json.loads(frame(s)[1])
        check("no create in ops without --allow-create (ctl, xray are)",
              info.get("ops") == ["ctl", "ctl-cancel", "watch", "xray"])
        check("device_id is stable across runs", info["device_id"] == dev1)
        send(s, 0x02, {"name": "svc-nope"})
        t, pl = frame(s)
        check("create refused without --allow-create",
              t == 0x13 and "--allow-create" in json.loads(pl)["message"])
        s.close()
        check("no session created", "svc-nope" not in sessions())
        # create off: attach only to a listed session, never start one
        s = conn(port)
        send(s, 0x01, {"name": "svc-new", "cols": 80, "rows": 24})
        try:
            t, pl = frame(s)
            msg = json.loads(pl).get("message", "") if t == 0x13 else ""
            check("create off: attach to an unknown name gets 0x13",
                  "no session named svc-new" in msg
                  and "does not allow create" in msg)
            check("create off: then the FIN", s.recv(1) == b"")
        except (EOFError, socket.timeout):
            check("create off: attach to an unknown name gets 0x13", False)
        s.close()
        check("create off: no session started by attach",
              "svc-new" not in sessions())
        r = subprocess.run([DCH, "--spawn", "svc-have", "--size", "80x24",
                            "sh"], env=env, capture_output=True, timeout=30)
        s = conn(port)
        send(s, 0x01, {"name": "svc-have", "cols": 80, "rows": 24})
        send(s, 0x10, b"\n")  # lite: no replay; make the idle sh print
        t, _ = frame(s)
        check("create off: attach to a listed session streams 0x10",
              r.returncode == 0 and t == 0x10)
        s.close()
        xray_checks(port, log)
        stop(p)
        follow_cap_checks()

        # --- the gate, per connection (whois runs per peer) --------------
        # A refusal on tailscaled's word is remembered by the parent for
        # 30 s, so each case runs against its own server.
        # Each refused peer gets one 0x13 {"code","message"} and the close
        # (#094), whether it sent its hello first or nothing at all.
        def gate_case(msg, why, code, *args, start=OWNER, **kw):
            ts(status=start)
            p, port, log = serve(*args)
            ts(**kw)
            got = [reply(port, hello) for hello in (True, False)]
            time.sleep(0.1)
            ok = re.search(r"refused: .*" + re.escape(why), text(log))
            check("refused: %s" % msg, ok)
            check("  told %s, exactly, then the close (#094)" % code,
                  got == [refusal(code)] * 2)
            if not ok or got != [refusal(code)] * 2:
                print(got, text(log)[-600:])
            stop(p)

        gate_case("another user's node", "another user's node", "not_owner",
                  who=whois(7, "other@example.com"))
        gate_case("a tagged host, a user not on --allow-user",
                  "not on --allow-user", "not_allowed", "--allow-user", "8",
                  start=TAGGED, status=TAGGED,
                  who=whois(7, "other@example.com"))
        gate_case("a tagged peer", "tagged node", "tagged_peer",
                  who=whois(42, tags=["tag:ci"]))
        gate_case("whois and status down (fail closed)", "fail closed",
                  "whois_unavailable", status=None, who=None)
        gate_case("this host's own node, by whois (F2)", "this host itself",
                  "self", who=whois(42, node="1001", nid=1001))
        # F1: --whois-optional relaxes only a tailscaled that is not answering
        gate_case("whois failing while status works, --whois-optional (F1)",
                  "status works", "whois_failed", "--whois-optional",
                  who=None)
        gate_case("a whois with a string UserProfile.ID, --whois-optional (F1)",
                  "unusable", "whois_failed", "--whois-optional",
                  who='{"UserProfile":{"ID":"42"},'
                      '"Node":{"ID":2002,"StableID":"2002"}}')
        gate_case("a whois over the 256 KiB cap, --whois-optional (F1)",
                  "status works", "whois_failed", "--whois-optional",
                  who=json.dumps(whois(42)) + " " * (300 << 10))
        ts()
        p, port, log = serve("--whois-optional")
        ts(status=None, who=None)
        check("accepted: whois and status down with --whois-optional (F1)",
              reply(port) == "accept")
        check("--whois-optional logs a warning",
              "tailscaled not answering, accepted" in text(log))
        stop(p)
        ts()
        p, port, log = serve()
        check("accepted: the owner's node, its hello answered as before",
              reply(port) == "accept")
        check("tailscale runs without the test env var (F7)",
              calls() and all(c.endswith(" unset") for c in calls()))
        stop(p)

        # --- F8: a refused peer is refused again before fork --------------
        ts()
        p, port, log = serve()
        ts(who=whois(7, "other@example.com"))
        cached = False
        for _ in range(40):
            refused(port)
            if "refused before fork" in text(log):
                cached = True
                break
            time.sleep(0.1)
        n0 = len([c for c in calls() if c.startswith("whois")])
        rs = [reply(port, i % 2 == 0) for i in range(6)]
        time.sleep(0.2)
        n1 = len([c for c in calls() if c.startswith("whois")])
        check("a refused peer is cached by the parent (F8)", cached)
        check("the cached refusal tells the same code, hello or not (#094)",
              rs == [refusal("not_owner")] * 6)
        check("its reconnects are refused with no fork and no whois (F8)",
              n1 == n0 and text(log).count(
                  "dch serve[%d]: 127.0.0.1: refused before fork" % p.pid)
              == 1)
        stop(p)

        # --- #095: past 16 per peer, a transient busy, never cached ------
        ts()
        p, port, log = serve()
        held = []
        for _ in range(16):
            s = conn(port)
            send(s, 0x08)
            if frame_not(s)[0] == 0x18:
                held.append(s)
            else:
                s.close()
        busy = json.dumps({"code": "busy", "message": "this host is at its "
                           "connection limit: try again shortly"},
                          separators=(",", ":")).encode()
        got = [reply(port, hello) for hello in (True, False)]
        check("16 connections from one peer are held", len(held) == 16)
        check("the 17th is told busy, exactly, then the close (#095)",
              got == [busy] * 2)
        if got != [busy] * 2:
            print(got, text(log)[-600:])
        check("the per-peer limit keeps its log line",
              "refused: too many connections from it" in text(log))
        for s in held:
            s.close()
        back = False
        for _ in range(50):
            r = reply(port)
            if r == "accept":
                back = True
                break
            time.sleep(0.1)
        check("busy is not cached: once they close, the peer is accepted "
              "(#095)", back and "refused before fork" not in text(log))
        stop(p)

        # --- #122: warm ctl connections idle out sooner at half the slots
        # (DCH_CTL_IDLE_MS 20 s here; under pressure a tenth, 2 s)
        def warm(port, src=None):
            s = conn(port, src)
            send(s, 0x30, {"argv": ["--history", "--json"]})
            return s if frame_not(s, (0x31, 0x32))[0] == 0x33 else None

        def gone(s):
            s.settimeout(0.1)
            try:
                return s.recv(1) == b""
            except socket.timeout:
                return False
            except OSError:
                return True

        def all_gone(w, secs):
            end = time.time() + secs
            while time.time() < end and not all(s and gone(s) for s in w):
                time.sleep(0.2)
            return all(s and gone(s) for s in w)

        def edge(srcs, what):
            # srcs[:-1] warm and idle past 2 s with none closed; the last
            # one tips it: all close after 2 s idle, not 20 s
            p, port, log = serve(e=dict(benv, DCH_CTL_IDLE_MS="20000"))
            w = [warm(port, a) for a in srcs[:-1]]
            time.sleep(3)
            check("at %d %s, warm ctl connections outlive the pressure "
                  "limit (2 s)" % (len(w), what),
                  all(w) and not any(gone(s) for s in w))
            w.append(warm(port, srcs[-1]))
            check("at %d %s, they close after 2 s idle, not 20 s (#122)"
                  % (len(w), what), all_gone(w, 6))
            for s in w:
                if s:
                    s.close()
            stop(p)

        # per peer: 7 of 16 is no pressure, the 8th is
        edge([None] * 8, "of the peer's 16 slots")
        # overall: 31 of 64 from five peers of at most 7 each is none, the
        # 32nd is; needs 127.0.0.2-6 (Linux has 127/8, macOS lo0 has
        # only 127.0.0.1 unless aliased)
        peers = ["127.0.0.%d" % i for i in range(2, 7)]
        try:
            for a in peers:
                with socket.socket() as t:
                    t.bind((a, 0))
        except OSError:
            print("skip at 32 of 64 slots overall (no 127.0.0.2-6 here)")
        else:
            edge([a for a in peers[:4] for _ in range(7)] + [peers[4]] * 4,
                 "of 64 slots overall")

        # --- F3: peer strings reach the log escaped ----------------------
        ts()
        p, port, log = serve()
        s = conn(port)
        send(s, 0x2e, {"device_id": "ph\u009bone\u202e\x1b]"})
        t, _ = frame(s)
        s.close()
        s = conn(port)
        send(s, 0x01, {"name": "bad\u009bname", "cols": 80, "rows": 24})
        t2, _ = frame(s)
        s.close()
        time.sleep(0.2)
        raw = open(log, "rb").read()
        check("a device id is logged with C1, bidi and ESC escaped (F3)",
              t == 0x11 and b"device ph\\u{009b}one\\u{202e}" in raw
              and b"\xc2\x9b" not in raw and b"\xe2\x80\xae" not in raw
              and b"\x1b" not in raw)
        check("a session name with a C1 control is refused (F3)", t2 == 0x13)
        stop(p)

        # --- no bypass: a loopback bind admits nobody (Q13) --------------
        ts()
        p, port, log = serve(e=env)
        check("without the bypass a loopback peer is refused, told nothing "
              "(not a tailnet peer, #094)",
              all(r == "closed" or r.startswith("reset")
                  for r in (reply(port, h) for h in (True, False))))
        check("a non-tailnet --bind warns that it admits nobody",
              "admits nobody" in text(log))
        check("an out-of-range peer is refused before fork (by the parent)",
              "dch serve[%d]: 127.0.0.1: refused: not an IPv4" % p.pid
              in text(log))
        stop(p)

        # --- --check-peer and --allow-user -------------------------------
        def peer(ip, *args):
            r = subprocess.run([DCH, "serve", "--check-peer", ip] + list(args),
                               env=env, capture_output=True, text=True,
                               timeout=20)
            return r.returncode, r.stdout

        check("--check-peer refuses a non-tailnet address, no code",
              peer("192.168.1.5") == (1, "refuse 192.168.1.5: not an IPv4 "
                                         "address in 100.64.0.0/10\n"))
        check("--check-peer accepts the owner's node",
              peer("100.100.2.2")[0] == 0)
        # F2: this host itself, by address and by node id
        rc, out = peer("100.100.1.1")
        check("a peer on this host's tailnet address is refused (F2)",
              rc == 1 and "this host itself" in out and "(self)" in out)
        rc, out = peer("100.100.3.3", "--bind", "100.100.3.3")
        check("a peer on the bound address is refused (F2)",
              rc == 1 and "this host itself" in out)
        ts(who=whois(42, node="1001", nid=1001))
        rc, out = peer("100.100.2.2")
        check("a whois whose Node.StableID is this host's Self.ID is refused "
              "(F2)",
              rc == 1 and "this host itself" in out)
        # F5: odd but valid JSON fails closed
        nd = '"Node":{"ID":2002,"StableID":"2002"'
        for who, why, msg in (
                ('{"UserProfile":{"ID":42},' + nd + ',"Tags":"tag:x"}}',
                 "tagged node", "Tags that is not an array"),
                ('{"UserProfile":{"ID":42},' + nd + ',"Tags":[],'
                 '"Tags":["tag:x"]}}', "tagged node", "a duplicated Tags"),
                ('{"UserProfile":{"ID":42},' + nd + '},' + nd +
                 ',"Tags":["tag:x"]}}', "unusable", "a duplicated Node"),
                ('{"UserProfile":{"ID":42},"UserProfile":{"ID":7},' + nd +
                 '}}', "unusable", "a duplicated UserProfile"),
                ('{"UserProfile":{"ID":42.0},' + nd + '}}', "unusable",
                 "a float UserProfile.ID"),
                ('{"UserProfile":{"ID":42}}', "unusable", "no Node"),
                ('{"UserProfile":{"ID":42},"Node":{"ID":2002}}', "unusable",
                 "a Node with an ID but no StableID"),
                ('{"UserProfile":{"ID":42},"Node":{"ID":2002,'
                 '"StableID":2002}}', "unusable", "a numeric Node.StableID"),
                ('{"UserProfile":{"ID":42},"Node":{"ID":2002,'
                 '"StableID":"n\\u001bx"}}', "unusable",
                 "a control character in Node.StableID"),
                ('{"UserProfile":{"ID":42},"Node":{"ID":2002,'
                 '"StableID":"%s"}}' % ("n" * 64), "unusable",
                 "an over-long Node.StableID")):
            ts(who=who)
            rc, out = peer("100.100.2.2", "--whois-optional")
            check("whois with %s is refused (F5)" % msg,
                  rc == 1 and why in out)
        # Self.ID unknown: no node check, but a Node is still required
        ts(status={"Self": {"UserID": 42}},
           who='{"UserProfile":{"ID":42},"Node":{"ID":2002}}')
        check("with no Self.ID, a whois Node without StableID is accepted",
              peer("100.100.2.2")[0] == 0)
        ts(status={"Self": {"UserID": 42}}, who='{"UserProfile":{"ID":42}}')
        rc, out = peer("100.100.2.2")
        check("with no Self.ID, a whois with no Node is refused",
              rc == 1 and "unusable" in out)
        ts(status={"Self": {"ID": "n x", "UserID": 42}})
        rc, err = run_fail()
        check("a status with a junk Self.ID refuses to start",
              rc == 1 and "one Self" in err)

        # --- #088: tailscale.com-shaped ids ("n...CNTRL") -----------------
        saas_peer = whois(42, node="nPeer456CNTRL", nid=123456789)
        ts(status=SAAS_OWNER, who=saas_peer)
        p, port, log = serve()
        check("serve starts on a tailscale.com-shaped status (#088)",
              port is not None)
        check("accepted: the owner's other node, tailscale.com ids (#088)",
              port is not None and not refused(port))
        stop(p)
        check("--check-peer accepts it too (#088)",
              peer("100.100.2.2")[0] == 0)
        ts(status=SAAS_OWNER,
           who=whois(42, node="nXyZ123CNTRL", nid=123456789))
        rc, out = peer("100.100.2.2")
        check("a whois whose StableID is this host's Self.ID is refused as "
              "self, tailscale.com ids (#088)",
              rc == 1 and "this host itself" in out)
        for who, msg in (
                ('{"UserProfile":{"ID":42},"Node":{"ID":123456789}}',
                 "no StableID"),
                ('{"UserProfile":{"ID":42},"Node":{"ID":123456789,'
                 '"StableID":123456789}}', "a numeric StableID"),
                ('{"UserProfile":{"ID":42},"Node":{"ID":123456789,'
                 '"StableID":["nPeer456CNTRL"]}}', "an array StableID")):
            ts(status=SAAS_OWNER, who=who)
            rc, out = peer("100.100.2.2")
            check("a whois with %s is refused as unusable, tailscale.com "
                  "ids (#088)" % msg, rc == 1 and "unusable" in out)

        ts(status='{"Self":{"ID":"1001","UserID":42},"Self":{"UserID":7}}')
        rc, err = run_fail()
        check("a status with a duplicated Self refuses to start (F5)",
              rc == 1 and "one Self" in err)
        ts(status={"Self": {"ID": "1001", "UserID": 42, "Tags": "tag:x"},
                   "User": {}})
        rc, err = run_fail()
        check("a host whose Self.Tags is not an array counts as tagged (F5)",
              rc == 1 and "--allow-user" in err)
        ts()
        ts(status=TAGGED, who=whois(7, "Friend@Example.com"))
        rc, err = run_fail()
        check("a tagged host refuses to start without --allow-user",
              rc == 1 and "--allow-user" in err)
        check("--allow-user by id", peer("100.100.2.2", "--allow-user",
                                         "7")[0] == 0)
        rc, out = peer("100.100.2.2", "--allow-user", "8")
        check("--allow-user by another id refuses, the code printed (#094)",
              rc == 1 and out == "refuse 100.100.2.2: a user not on "
              "--allow-user (tagged host) (not_allowed)\n")
        check("--allow-user by login, case-insensitive (Q15)",
              peer("100.100.2.2", "--allow-user", "friend@example.COM")[0] == 0)
        check("--allow-user by another login refuses",
              peer("100.100.2.2", "--allow-user", "x@example.com")[0] == 1)
        ts(status=TAGGED, who=whois(7, tags=["tag:ci"]))
        check("--allow-user never admits a tagged peer",
              peer("100.100.2.2", "--allow-user", "7")[0] == 1)
        ts()
        rc, err = run_fail("--allow-user", "7")
        check("--allow-user on an untagged host refuses to start (Q14)",
              rc == 1 and "only for tagged hosts" in err)

        # --- startup refusals ---------------------------------------------
        ts(status=None)
        rc, err = run_fail()
        check("tailscale status failing refuses to start", rc == 1)
        rc, err = run_fail("--whois-optional")
        check("--whois-optional with status failing refuses to start",
              rc == 1 and "owner is unknown" in err)
        if not any(os.path.exists(x) for x in (
                "/usr/bin/tailscale", "/usr/local/bin/tailscale",
                "/opt/homebrew/bin/tailscale",
                "/Applications/Tailscale.app/Contents/MacOS/Tailscale")):
            rc, err = run_fail("--whois-optional",
                               e=dict(benv, PATH="/usr/bin:/bin"))
            check("no tailscale CLI refuses to start, --whois-optional or "
                  "not", rc == 1 and "not on PATH" in err)
        # F4: an empty or relative PATH entry is never searched
        ts()
        bad_ts = "#!/bin/sh\ntouch %s/planted-ran; exit 1\n" % tmp
        os.makedirs(os.path.join(tmp, "rel"))
        for d in (tmp, os.path.join(tmp, "rel")):
            with open(os.path.join(d, "tailscale"), "w") as f:
                f.write(bad_ts)
            os.chmod(os.path.join(d, "tailscale"), 0o755)
        p, port, log = serve(e=dict(benv, PATH="rel::%s:/usr/bin:/bin" % fake),
                             cwd=tmp)
        check("the tailscale CLI comes from an absolute PATH entry (F4)",
              port is not None and "tailscale CLI: %s/tailscale" % fake
              in text(log)
              and not os.path.exists(os.path.join(tmp, "planted-ran")))
        stop(p)
        ts(ip=None)
        rc, err = run_fail(bind=False)
        check("tailscale ip -4 failing refuses to start (Q11)",
              rc == 1 and "reported no address" in err)
        ts(ip="")
        rc, err = run_fail(bind=False)
        check("tailscale ip -4 printing nothing refuses to start (Q11)",
              rc == 1 and "reported no address" in err)
        ts()
        for bad in ("0.0.0.0", "224.0.0.1", "255.255.255.255"):
            p2 = subprocess.run([DCH, "serve", "--bind", bad, "--port", "0"],
                                env=benv, capture_output=True, text=True,
                                timeout=20)
            check("--bind %s refused (Q13)" % bad,
                  p2.returncode == 2 and "unicast" in p2.stderr)
        hold = socket.socket()
        hold.bind(("127.0.0.1", 0))
        hold.listen(1)
        busy = hold.getsockname()[1]
        p2 = subprocess.run([DCH, "serve", "--bind", "127.0.0.1", "--port",
                             str(busy)], env=benv, capture_output=True,
                            text=True, timeout=20)
        hold.close()
        check("a port in use refuses to start, naming Prosper (Q12)",
              p2.returncode == 1 and "Prosper" in p2.stderr
              and "another dch serve" in p2.stderr)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        for n in sessions():
            subprocess.run([DCH, "-k", n], env=env, capture_output=True,
                           timeout=15)
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAILED: %d" % len(fails))
        sys.exit(1)
    print("all ok")


if __name__ == "__main__":
    main()
