"""Test-only client for a master's X-ray shell ring (#119).

Speaks MSG_RING_READ / MSG_RING_CLEAR over a session's control socket and
dumps what comes back as .ring.ndjson: one JSON object per line,
{"seq","kind","flags","t","text"}, marks also carrying "exit". This is the
"test-only way to dump the ring": it lives in tests/, never in dch or its help.

    python3 tests/xray_ring.py <session> [<from-seq>]   # ndjson on stdout
"""
import json, os, socket, struct, sys

MSG_READ_DATA, MSG_READ_END, MSG_ACK = 8, 9, 11
MSG_RING_READ, MSG_RING_HDR, MSG_RING_WAKE, MSG_RING_CLEAR = 18, 19, 20, 21
MSG_RESTART, MSG_RESTART_CAPS, F_RING = 13, 22, 0x01
F_SUB, F_SCREEN = 0x01, 0x04
S_RANGE, S_CHAPTERS, S_PAUSED, S_OFF, S_SUB, S_SUBFULL, S_LATCHED = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40)
KINDS = {1: "prompt", 2: "input", 3: "output", 4: "mark", 5: "gap"}
F_CONT, F_EXIT, F_TRUNC, F_INPUT, F_ALT = 0x01, 0x02, 0x04, 0x08, 0x10


def sock_path(name):
    d = os.environ.get("DCH_SOCKET_DIR")
    if not d:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        d = ("%s/dch-%d" % (xdg, os.getuid()) if xdg
             else "/tmp/dch-%d" % os.getuid())
    return os.path.join(d, name + ".sock")


def connect(name):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sock_path(name))
    return s


def recv_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise EOFError("master closed the connection")
        b += c
    return b


def recv_frame(s):
    t, n = struct.unpack("<BH", recv_exact(s, 3))
    if t in (MSG_READ_END, MSG_ACK, MSG_RING_WAKE):
        return t, n, b""          # len is the status (no payload)
    return t, n, recv_exact(s, n)


def parse_hdr(p):
    st, head, tail, first, count, epoch = struct.unpack_from("<BQQQII", p)
    return {"status": st, "head": head, "tail": tail, "first": first,
            "count": count, "epoch": epoch,
            "session_id": p[33:50].decode()}


def parse_records(data, first):
    out, off, seq = [], 0, first
    while off < len(data):
        n, kind, flags, t = struct.unpack_from("<HBBI", data, off)
        text = data[off + 8:off + 8 + n]
        assert len(text) == n, "torn record"
        out.append({"seq": seq, "kind": kind, "flags": flags, "t": t,
                    "text": text})
        off += 8 + n
        seq += 1
    return out


def read(sock, seq=0, maxb=1 << 20, sub=False, wakes=None, flags=0):
    """One MSG_RING_READ on an open connection -> (hdr, records)."""
    sock.sendall(struct.pack("<BHQIB", MSG_RING_READ, 13, seq, maxb,
                             (F_SUB if sub else 0) | flags))
    hdr, data = None, b""
    while True:
        t, n, p = recv_frame(sock)
        if t == MSG_RING_WAKE:          # a wake from before this read
            if wakes is not None:
                wakes.append(1)
            continue
        if t == MSG_RING_HDR:
            hdr = parse_hdr(p)
        elif t == MSG_READ_DATA:
            data += p
        elif t == MSG_READ_END:
            hdr = hdr or {}
            hdr["end"] = n
            break
        else:
            raise AssertionError("unexpected frame type %d" % t)
    return hdr, parse_records(data, hdr.get("first", 0)) if data else []


def read_all(name, seq=0):
    """Every record from seq on, following replies until the tail."""
    s = connect(name)
    try:
        recs, h, st = [], None, 0
        while True:
            h, r = read(s, seq)
            st |= h.get("status", 0)
            h["status_any"] = st          # range on any reply
            recs += r
            if not r or h["end"] != 0:
                return h, recs
            seq = r[-1]["seq"] + 1
            if seq >= h["tail"]:
                return h, recs
    finally:
        s.close()


def clear(name):
    s = connect(name)
    try:
        s.sendall(struct.pack("<BH", MSG_RING_CLEAR, 0))
        t, n, _ = recv_frame(s)
        return t == MSG_ACK and n == 0
    finally:
        s.close()


def ndjson_line(r):
    o = {"seq": r["seq"], "kind": KINDS.get(r["kind"], r["kind"]),
         "flags": r["flags"], "t": r["t"],
         "text": r["text"].decode("utf-8", "replace")}
    if r["kind"] == 4 and r["flags"] & F_EXIT:
        o["exit"] = int(o["text"].split(";", 2)[1])
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def texts(recs, kinds=(3,)):
    return [r["text"].decode("utf-8", "replace") for r in recs
            if r["kind"] in kinds]


if __name__ == "__main__":
    h, recs = read_all(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2
                       else 0)
    sys.stderr.write(json.dumps(h) + "\n")
    for r in recs:
        print(ndjson_line(r))
