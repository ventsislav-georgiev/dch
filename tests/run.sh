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

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
