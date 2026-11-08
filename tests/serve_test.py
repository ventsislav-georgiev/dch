"""dch serve (#073): Prosper's remote frames 1.0 over TCP.

The server runs on 127.0.0.1 with a fake `tailscale` first on PATH and the
test-only loopback bypass (DCH_SERVE_TEST_LOOPBACK=1 plus a loopback --bind);
it never binds a tailnet address. Ports are the kernel's pick (--port 0).
Covers: the golden fixtures (tests/remote-frames, --mutating); the peer gate
(owner mismatch, tagged peer, whois down closed and open, no bypass); startup
refusals (tagged host without --allow-user, --allow-user on an untagged host,
tailscale ip -4 failing or empty, wildcard and non-unicast binds, the port in
use); --allow-user by id and by login (case-insensitive); create off without
--allow-create; the newest attach retiring the older; 0x09 from the mirror;
a newer proto major; an oversize control frame; a client half-close; SIGTERM.
Security review F1-F8: --whois-optional only while tailscaled is down; this
host's own address and node refused; log escaping; the CLI from absolute
PATH entries; odd whois/status JSON failing closed; the test variable unset
for children; refused peers cached before fork.

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

# Self.ID is a string in status, Node.ID a number in whois (as tailscale).
OWNER = {"Self": {"ID": "1001", "UserID": 42, "Tags": []},
         "User": {"42": {"LoginName": "me@example.com"}}}
TAGGED = {"Self": {"ID": "1001", "UserID": 9, "Tags": ["tag:server"]},
          "User": {"9": {"LoginName": "tagged-devices"}}}


def whois(uid, login="me@example.com", tags=(), node=2002):
    return {"UserProfile": {"ID": uid, "LoginName": login},
            "Node": {"ID": node, "Tags": list(tags)}}


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

    def serve(*args, e=benv, bind=True, cwd=None):
        """Start dch serve: (proc, port, log path); port None when it exited."""
        log_n[0] += 1
        log = os.path.join(tmp, "serve%d.log" % log_n[0])
        argv = [DCH, "serve"] + (["--bind", "127.0.0.1"] if bind else []) + \
            ["--port", "0"] + list(args)
        with open(log, "w") as lf:
            p = subprocess.Popen(argv, env=e, stdin=subprocess.DEVNULL,
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

    def conn(port):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
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

    def refused(port):
        """Connected, sent a hello, got EOF with no byte at all."""
        try:
            s = conn(port)
            send(s, 0x08)
            got = s.recv(1)
            s.close()
            return got == b""
        except (ConnectionResetError, BrokenPipeError):
            return True
        except socket.timeout:
            return False

    def sessions():
        r = subprocess.run([DCH, "-ls"], env=env, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.split()

    try:
        mirror = subprocess.run([DCH, "--build"], env=env, capture_output=True,
                                text=True).stdout.strip() == "full"

        # --- the goldens, every fixture, --mutating ----------------------
        ts()
        p, port, log = serve("--allow-create")
        check("serves on a loopback test port", port is not None)
        r = subprocess.run([sys.executable, RUNPY, "127.0.0.1:%d" % port,
                            "--mutating"], capture_output=True, text=True,
                           timeout=180)
        check("golden fixtures 01-15 pass (--mutating)",
              r.returncode == 0 and r.stdout.count("PASS ") == 15)
        if r.returncode:
            print(r.stdout + r.stderr)
        check("the bypass is logged loudly",
              "WARNING: DCH_SERVE_TEST_LOOPBACK=1" in text(log))

        # --- 0x18 ---------------------------------------------------------
        s = conn(port)
        send(s, 0x08, {"proto": "1.0"})
        t, pl = frame(s)
        info = json.loads(pl)
        check("0x18 hello keys", t == 0x18 and info.get("server") == "dch"
              and info.get("proto") == "1.0" and info.get("hostname")
              and info.get("name") and info.get("os")
              and re.fullmatch(r"[0-9a-f]{64}", info.get("device_id", "")))
        check("ops lists create with --allow-create",
              sorted(info.get("ops", [])) == ["create", "watch"])
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
        got12 = False
        try:
            while True:
                t, _ = frame(a)
                got12 |= t == 0x12
        except (EOFError, ConnectionResetError):
            pass
        except socket.timeout:
            got12 = True
        check("a newer attach retires the older: EOF, no 0x12", not got12)

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
        check("no create in ops without --allow-create",
              info.get("ops") == ["watch"])
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
        t, _ = frame(s)
        check("create off: attach to a listed session streams 0x10",
              r.returncode == 0 and t == 0x10)
        s.close()

        stop(p)

        # --- the gate, per connection (whois runs per peer) --------------
        # A refusal on tailscaled's word is remembered by the parent for
        # 30 s, so each case runs against its own server.
        def gate_case(msg, why, *args, **kw):
            ts()
            p, port, log = serve(*args)
            ts(**kw)
            got = refused(port)
            time.sleep(0.1)
            ok = got and re.search(r"refused: .*" + re.escape(why), text(log))
            check("refused: %s" % msg, ok)
            if not ok:
                print(text(log)[-600:])
            stop(p)

        gate_case("another user's node", "another user's node",
                  who=whois(7, "other@example.com"))
        gate_case("a tagged peer", "tagged node", who=whois(42, tags=["tag:ci"]))
        gate_case("whois and status down (fail closed)", "fail closed",
                  status=None, who=None)
        # F1: --whois-optional relaxes only a tailscaled that is not answering
        gate_case("whois failing while status works, --whois-optional (F1)",
                  "status works", "--whois-optional", who=None)
        gate_case("a whois with a string UserProfile.ID, --whois-optional (F1)",
                  "unusable", "--whois-optional",
                  who='{"UserProfile":{"ID":"42"},"Node":{"ID":2002}}')
        gate_case("a whois over the 256 KiB cap, --whois-optional (F1)",
                  "status works", "--whois-optional",
                  who=json.dumps(whois(42)) + " " * (300 << 10))
        ts()
        p, port, log = serve("--whois-optional")
        ts(status=None, who=None)
        check("accepted: whois and status down with --whois-optional (F1)",
              not refused(port))
        check("--whois-optional logs a warning",
              "tailscaled not answering, accepted" in text(log))
        stop(p)
        ts()
        p, port, log = serve()
        check("accepted: the owner's node", not refused(port))
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
        rs = [refused(port) for _ in range(5)]
        time.sleep(0.2)
        n1 = len([c for c in calls() if c.startswith("whois")])
        check("a refused peer is cached by the parent (F8)", cached)
        check("its reconnects are refused with no fork and no whois (F8)",
              all(rs) and n1 == n0 and text(log).count(
                  "dch serve[%d]: 127.0.0.1: refused before fork" % p.pid)
              == 1)
        stop(p)

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
        check("without the bypass a loopback peer is refused", refused(port))
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

        check("--check-peer refuses a non-tailnet address",
              peer("192.168.1.5")[0] == 1)
        check("--check-peer accepts the owner's node",
              peer("100.100.2.2")[0] == 0)
        # F2: this host itself, by address and by node id
        rc, out = peer("100.100.1.1")
        check("a peer on this host's tailnet address is refused (F2)",
              rc == 1 and "this host itself" in out)
        rc, out = peer("100.100.3.3", "--bind", "100.100.3.3")
        check("a peer on the bound address is refused (F2)",
              rc == 1 and "this host itself" in out)
        ts(who=whois(42, node=1001))
        rc, out = peer("100.100.2.2")
        check("a whois whose Node.ID is this host's Self.ID is refused (F2)",
              rc == 1 and "this host itself" in out)
        # F5: odd but valid JSON fails closed
        for who, why, msg in (
                ('{"UserProfile":{"ID":42},"Node":{"ID":2002,"Tags":"tag:x"}}',
                 "tagged node", "Tags that is not an array"),
                ('{"UserProfile":{"ID":42},"Node":{"ID":2002,"Tags":[],'
                 '"Tags":["tag:x"]}}', "tagged node", "a duplicated Tags"),
                ('{"UserProfile":{"ID":42},"Node":{"ID":2002},'
                 '"Node":{"ID":2002,"Tags":["tag:x"]}}', "unusable",
                 "a duplicated Node"),
                ('{"UserProfile":{"ID":42},"UserProfile":{"ID":7},'
                 '"Node":{"ID":2002}}', "unusable", "a duplicated UserProfile"),
                ('{"UserProfile":{"ID":42.0},"Node":{"ID":2002}}', "unusable",
                 "a float UserProfile.ID"),
                ('{"UserProfile":{"ID":42},"Node":{}}', "unusable",
                 "a Node with no ID")):
            ts(who=who)
            rc, out = peer("100.100.2.2", "--whois-optional")
            check("whois with %s is refused (F5)" % msg,
                  rc == 1 and why in out)
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
        check("--allow-user by another id refuses",
              peer("100.100.2.2", "--allow-user", "8")[0] == 1)
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
