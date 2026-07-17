#!/bin/sh
# dch test suite. Exercises CLI surface + alias-sidecar lifecycle without a
# controlling tty (the interactive picker itself can't be driven headlessly).
set -u

DCH=${DCH:-./dch}
fail=0
pass=0

ok()   { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad()  { fail=$((fail + 1)); printf '  FAIL %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want [$3] got [$2])"; fi; }

# Isolate session dir: dch uses $XDG_RUNTIME_DIR/dch-$UID.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export XDG_RUNTIME_DIR="$TMP"
SOCKDIR="$TMP/dch-$(id -u)"
mkdir -p "$SOCKDIR"

# --- usage advertises -rl --------------------------------------------------
"$DCH" -h 2>&1 | grep -q -- '-rl' && ok "usage lists -rl" || bad "usage lists -rl"

# --- unknown flag still errors --------------------------------------------
"$DCH" -zz >/dev/null 2>&1; check "unknown flag exits 1" "$?" "1"

# --- -ls on empty dir ------------------------------------------------------
out=$("$DCH" -ls 2>/dev/null); check "empty -ls is empty" "$out" ""

# --- fake sessions: real listening unix sockets ----------------------------
mksock() {
	python3 - "$1" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sys.argv[1])
s.listen(1)
# Hold the socket open just long enough for dch to stat it, then exit;
# the file persists on disk as a socket node after the fd closes.
PY
}
mksock "$SOCKDIR/alpha.sock"
mksock "$SOCKDIR/beta.sock"

# -ls reports both real names (order-independent)
ls_out=$("$DCH" -ls 2>/dev/null | sort | tr '\n' ',')
check "-ls lists sessions" "$ls_out" "alpha,beta,"

# --- alias sidecar is display-only: -ls still shows real name --------------
printf 'My Alpha\n' > "$SOCKDIR/alpha.sock.alias"
ls_out=$("$DCH" -ls 2>/dev/null | sort | tr '\n' ',')
check "-ls ignores alias" "$ls_out" "alpha,beta,"

# --- -lj exposes name<TAB>alias<TAB>activity_epoch -------------------------
# alpha (with alias) gets a fresh activity stamp; beta has none → epoch 0.
: > "$SOCKDIR/alpha.sock.act"
lj_alpha=$("$DCH" -lj 2>/dev/null | awk -F'\t' '$1=="alpha"{print NF":"($3>0)}')
check "-lj alpha: 3 fields, recent epoch" "$lj_alpha" "3:1"
lj_beta=$("$DCH" -lj 2>/dev/null | awk -F'\t' '$1=="beta"{print NF":"$3}')
check "-lj beta: 3 fields, epoch 0" "$lj_beta" "3:0"

# --- kill removes socket, alias, and activity sidecars ---------------------
"$DCH" -k alpha >/dev/null 2>&1
[ ! -e "$SOCKDIR/alpha.sock" ]       && ok "kill removes socket"        || bad "kill removes socket"
[ ! -e "$SOCKDIR/alpha.sock.alias" ] && ok "kill removes alias sidecar" || bad "kill removes alias sidecar"
[ ! -e "$SOCKDIR/alpha.sock.act" ]   && ok "kill removes act sidecar"   || bad "kill removes act sidecar"
[ -e "$SOCKDIR/beta.sock" ]          && ok "kill leaves other session"  || bad "kill leaves other session"

# --- -rl needs a tty when sessions exist (headless = graceful error) -------
"$DCH" -rl </dev/null >/dev/null 2>&1; check "-rl headless exits 1" "$?" "1"

# --- DCH_SOCKET_DIR override: used verbatim, and wins over XDG_RUNTIME_DIR ---
DCHDIR="$TMP/custom-dch"
mkdir -p "$DCHDIR"
mksock "$DCHDIR/gamma.sock"
# XDG dir still has beta.sock; the override must list gamma, NOT beta.
ovr_out=$(DCH_SOCKET_DIR="$DCHDIR" "$DCH" -ls 2>/dev/null | sort | tr '\n' ',')
check "DCH_SOCKET_DIR overrides XDG" "$ovr_out" "gamma,"
# Override creates the leaf dir if missing (mkdir 0700), then lists empty.
NEWDIR="$TMP/made-by-dch"
empty_out=$(DCH_SOCKET_DIR="$NEWDIR" "$DCH" -ls 2>/dev/null)
check "DCH_SOCKET_DIR empty -ls" "$empty_out" ""
[ -d "$NEWDIR" ] && ok "DCH_SOCKET_DIR creates leaf dir" || bad "DCH_SOCKET_DIR creates leaf dir"
# Unset → unchanged behavior: still sees the XDG sessions (beta).
unset_out=$("$DCH" -ls 2>/dev/null | sort | tr '\n' ',')
check "unset DCH_SOCKET_DIR = XDG default" "$unset_out" "beta,"

# --- spawn-vs-attach: leftover sockets + hot-path attach -------------------
# Real forkpty spawn, so it needs an executable dch + python3. Skip gracefully.
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    if DCH="$DCH" python3 "$(dirname "$0")/spawn_test.py" >/dev/null 2>&1; then
        ok "spawn over leftover socket + hot-path attach"
    else
        bad "spawn over leftover socket + hot-path attach"
    fi
fi

# --- hot-path performance budget (spawn-vs-attach decision) ----------------
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    if DCH="$DCH" python3 "$(dirname "$0")/perf_test.py" >/dev/null 2>&1; then
        ok "hot-path perf budget"
    else
        bad "hot-path perf budget"
    fi
fi

# --- on-demand redraw (SIGUSR2 → MSG_REDRAW(REDRAW_WINCH)) ------------------
# Real forkpty spawn, so it needs an executable dch + python3. Skip gracefully.
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    if DCH="$DCH" python3 "$(dirname "$0")/redraw_test.py" >/dev/null 2>&1; then
        ok "SIGUSR2 redraw reaches inner program"
    else
        bad "SIGUSR2 redraw reaches inner program"
    fi
fi

# --- attach: WINCH repaint, and NEVER a typed ^L ----------------------------
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    if DCH="$DCH" python3 "$(dirname "$0")/attach_test.py" >/dev/null 2>&1; then
        ok "attach repaints via WINCH, types no ^L"
    else
        bad "attach repaints via WINCH, types no ^L"
    fi
fi

# --- control verbs: --spawn --run --read --wait --keys ---------------------
# Headless by design, so plain shell drives them. `sh` as the inner command
# keeps prompts boring; markers make matching deterministic.
if [ -x "$DCH" ]; then
    SN="ctl$$"

    out=$("$DCH" --spawn "$SN" --size 80x24 sh 2>/dev/null)
    check "--spawn prints session name" "$out" "$SN"
    [ -S "$SOCKDIR/$SN.sock" ] && ok "--spawn creates socket" \
                               || bad "--spawn creates socket"

    "$DCH" --spawn "$SN" sh >/dev/null 2>&1
    check "--spawn refuses live duplicate" "$?" "1"

    # Full build has the terminal mirror; dch-lite refuses --read/--wait with
    # exit 3 by design. Probe once and branch the expectations.
    "$DCH" --read "$SN" >/dev/null 2>&1
    mirror=$?

    if [ "$mirror" -eq 0 ]; then
        "$DCH" --run "$SN" "echo dch_marker_one" >/dev/null 2>&1
        hit=$("$DCH" --wait "$SN" --match dch_marker_one --timeout 5000)
        st=$?
        check "--wait finds marker (exit 0)" "$st" "0"
        case "$hit" in
        *dch_marker_one*) ok "--wait prints matching line" ;;
        *) bad "--wait prints matching line (got [$hit])" ;;
        esac

        "$DCH" --read "$SN" 2>/dev/null | grep -q dch_marker_one \
            && ok "--read shows session screen" \
            || bad "--read shows session screen"

        "$DCH" --read "$SN" --recent 200 2>/dev/null | grep -q dch_marker_one \
            && ok "--read --recent shows history" \
            || bad "--read --recent shows history"

        "$DCH" --wait "$SN" --match never_printed_zz --timeout 200 >/dev/null 2>&1
        check "--wait timeout exits 2" "$?" "2"

        # ctrl+c must interrupt a foreground sleep; the next prompt marker
        # proves the shell is answering again.
        "$DCH" --run "$SN" "sleep 30" >/dev/null 2>&1
        "$DCH" --keys "$SN" ctrl+c >/dev/null 2>&1
        check "--keys ctrl+c accepted" "$?" "0"
        "$DCH" --run "$SN" "echo dch_marker_two" >/dev/null 2>&1
        "$DCH" --wait "$SN" --match dch_marker_two --timeout 5000 >/dev/null 2>&1
        check "--keys ctrl+c interrupts sleep" "$?" "0"
    else
        check "lite --read exits 3" "$mirror" "3"
        "$DCH" --wait "$SN" --match x --timeout 200 >/dev/null 2>&1
        check "lite --wait exits 3" "$?" "3"
        "$DCH" --keys "$SN" enter >/dev/null 2>&1
        check "lite --keys legacy fallback exits 0" "$?" "0"
        # --send/--run are mirror-independent: plain pty writes.
        "$DCH" --run "$SN" "echo lite_marker" >/dev/null 2>&1
        check "lite --run accepted" "$?" "0"
    fi

    "$DCH" --read no_such_session_zz >/dev/null 2>&1
    check "--read missing session exits 1" "$?" "1"

    "$DCH" --ls-json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert any(s["name"] == sys.argv[1] for s in d), d
' "$SN" && ok "--ls-json valid JSON with session" \
       || bad "--ls-json valid JSON with session"

    "$DCH" -k "$SN" >/dev/null 2>&1

    # Mirror-less master (DCH_NO_VT parity with dch-lite): read/wait refuse
    # with exit 3, keys fall back to legacy encoding.
    LN="lite$$"
    DCH_NO_VT=1 "$DCH" --spawn "$LN" sh >/dev/null 2>&1
    "$DCH" --read "$LN" >/dev/null 2>&1
    check "no-mirror --read exits 3" "$?" "3"
    "$DCH" --wait "$LN" --match x --timeout 200 >/dev/null 2>&1
    check "no-mirror --wait exits 3" "$?" "3"
    "$DCH" --keys "$LN" enter >/dev/null 2>&1
    check "no-mirror --keys legacy fallback exits 0" "$?" "0"
    "$DCH" -k "$LN" >/dev/null 2>&1
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
