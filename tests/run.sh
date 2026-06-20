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

# --- kill removes both socket and its alias sidecar ------------------------
"$DCH" -k alpha >/dev/null 2>&1
[ ! -e "$SOCKDIR/alpha.sock" ]       && ok "kill removes socket"        || bad "kill removes socket"
[ ! -e "$SOCKDIR/alpha.sock.alias" ] && ok "kill removes alias sidecar" || bad "kill removes alias sidecar"
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

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
