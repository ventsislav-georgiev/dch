"""Shared by tests/serve_test.py and tests/serve_timing_test.py: the fake
tailscale, the dch serve starter, the frame helpers. setup() builds a temp
tree and returns its locals (names in NAMES); cleanup(L) kills what is left; verdict(L)
prints the verdict and exits 1 on a failure.
"""
import calendar, json, os, re, shutil, signal, socket, struct, subprocess, sys
import tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
RUNPY = os.path.join(HERE, "remote-frames", "golden", "run.py")

# Builtins only, no fork: serve gives each call 3 s (TS_MS), and under load
# a slower fake turns into a whois_failed refusal cached 30 s (#246). The
# fallback of tests/fake_tailscale.c, which keeps the same contract (#277).
FAKE_TS = """#!/bin/sh
d=${0%/*}
echo "$1 ${DCH_SERVE_TEST_LOOPBACK:-unset}" >> "$d/calls"
case "$1" in
status) f=status.json ;; whois) f=whois.json ;; ip) f=ip.txt ;; *) exit 1 ;;
esac
[ -f "$d/$f" ] || exit 1
while IFS= read -r l; do printf '%s\\n' "$l"; done < "$d/$f"
printf '%s' "$l"
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



NAMES = 'tmp fake home env benv procs fails whois_fail_ok slow_whois check put ts log_n serve stop calls run_fail text conn send recv_exact frame frame_not reply refused refusal sessions ctl1'.split()


def setup():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchsv", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    fake = os.path.join(tmp, "fake")
    os.makedirs(fake)
    # the compiled fake: one exec per call and no shell start (#277); the
    # shell one only when no compiler builds it
    try:
        built = subprocess.call(os.environ.get("CC", "cc").split() + [
            "-O1", "-o", os.path.join(fake, "tailscale"),
            os.path.join(HERE, "fake_tailscale.c")]) == 0
    except OSError:
        built = False
    if not built:
        print("  note: tests/fake_tailscale.c did not build, the shell fake")
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
    whois_fail_ok = set()	# serve logs whose whois_failed is the case
    slow_whois = []

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)
            if not slow_whois:
                slow_whois.extend(
                    n for n in sorted(os.listdir(tmp))
                    if n.startswith("serve") and n.endswith(".log")
                    and os.path.join(tmp, n) not in whois_fail_ok
                    and "refused: tailscale whois failed while" in
                    text(os.path.join(tmp, n)))
                if slow_whois:
                    print("  setup: whois_failed, the fake whois was too "
                          "slow (over serve's 3 s, refusal cached 30 s; "
                          "%s): checks failing on it are not serve bugs"
                          % ", ".join(slow_whois))

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
    return locals()


def cleanup(L):
    procs, env, tmp, sessions = (
        L[k] for k in ("procs", "env", "tmp", "sessions"))
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()
    for n in sessions():
        subprocess.run([DCH, "-k", n], env=env, capture_output=True,
                       timeout=15)
    shutil.rmtree(tmp, ignore_errors=True)


def verdict(L):
    fails, slow_whois = L["fails"], L["slow_whois"]
    if fails:
        print("FAILED: %d%s" % (len(fails), slow_whois and
                                " (setup: whois_failed, the fake whois was "
                                "too slow, see above)" or ""))
        sys.exit(1)
    print("all ok")
