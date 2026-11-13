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
# Run a python test script; on failure print its output so CI logs show
# which sub-check died instead of a bare FAIL line.
# A test still running after DCH_TEST_TIMEOUT seconds (whole seconds, default
# 600: the slowest python test takes a few minutes on a macOS runner under
# ASan; 0 = no watchdog; empty, negative or not a number = 600) gets every
# thread's traceback printed by faulthandler's in-process watchdog thread (no
# sleeper process to reap), exits 1, and pyreap ends what it left. The test
# runs in its own session (os.setsid), in the background so that a trapped ^C
# ends it too. Output goes to a file, not $(...): a leftover child holding
# that pipe open would hang the suite.
pypid=
pyrun(){
    pyt=${DCH_TEST_TIMEOUT:-600}
    case $pyt in *[!0-9]*|??????????*) pyt=600 ;; esac
    pyls=$(ls "$SOCKDIR")
    # An async list starts with SIGINT and SIGQUIT ignored: restore them.
    DCH="$DCH" python3 -c 'import faulthandler, os, runpy, signal, sys
os.setsid()
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGQUIT, signal.SIG_DFL)
if int(sys.argv[1]):
    faulthandler.dump_traceback_later(int(sys.argv[1]), exit=True)
sys.argv = sys.argv[2:]
sys.path[0] = os.path.dirname(os.path.abspath(sys.argv[0]))
runpy.run_path(sys.argv[0], run_name="__main__")' \
        "$pyt" "$(dirname "$0")/$2" </dev/null >"$TMP/pyout" 2>&1 &
    pypid=$!
    wait "$pypid" && ok "$1" \
        || { bad "$1"; sed 's/^/       /' "$TMP/pyout"
             grep -q '^Timeout (' "$TMP/pyout" && pyreap; }
    pypid=
}
# End what test $pypid left: its process group, then the sessions it started
# in this run's socket dir. setsid made the group id its pid, and a group id
# is never reused while a member lives, so group $pypid is the test's. Masters
# setsid themselves: dch -k ends each by the pid on its socket. Sessions and
# dirs a test made under /tmp itself are left: no safe rule finds them from
# here. Only these rows print, never another process's argv.
pyreap(){
    me=$(ps -o pgid= -p $$ | tr -d ' ')
    # never kill -- -'', -0, -1 or our own group: $! is none of them
    case $pypid in ''|0*|1|*[!0-9]*) return ;; esac
    case $me in ''|0*|*[!0-9]*) return ;; esac
    [ "$pypid" != "$me" ] || return
    left=$(ps -A -o pid= -o pgid= -o stat= -o etime= -o command= |
           awk -v g="$pypid" '$2 == g { print "       " $0 }')
    if [ -n "$left" ]; then
        printf '       killed, left in its process group:\n%s\n' "$left"
        kill -s KILL -- "-$pypid"
    fi
    for s in "$SOCKDIR"/*.sock; do
        n=${s##*/}
        [ -S "$s" ] && ! printf '%s\n' "$pyls" | grep -qxF "$n" || continue
        printf '       ended its session %s with dch -k\n' "${n%.sock}"
        "$DCH" -k "${n%.sock}" >/dev/null 2>&1
    done
}

# Isolate session dir: dch uses $XDG_RUNTIME_DIR/dch-$UID.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
trap 'pyreap; exit 130' INT TERM HUP
export XDG_RUNTIME_DIR="$TMP"
# DCH_SOCKET_DIR wins over XDG_RUNTIME_DIR: an inherited one would point the
# suite at the caller's live sessions.
unset DCH_SOCKET_DIR DCH_SESSION
# --revive would run the developer's login shell (#057): revive_test.py covers it
export DCH_LOGIN_ENV=0
# Session records go to $XDG_STATE_HOME/dch/sessions: keep them out of ~.
export XDG_STATE_HOME="$TMP/state"
# The developer's config (auto_allow = true) must not answer the fakes' prompts.
export DCH_CONFIG="$TMP/config"
# -ls and the picker would probe the developer's tailnet peers (#087):
# remote_client_test.py points dch at a loopback dch serve with --hosts.
export DCH_NO_REMOTE=1
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
ls_out=$("$DCH" -ls 2>/dev/null | LC_ALL=C sort | tr '\n' ',')
check "-ls lists sessions" "$ls_out" "alpha,beta,"

# --- alias sidecar is display-only: -ls still shows real name --------------
printf 'My Alpha\n' > "$SOCKDIR/alpha.sock.alias"
ls_out=$("$DCH" -ls 2>/dev/null | LC_ALL=C sort | tr '\n' ',')
check "-ls shows alias and real name" "$ls_out" "My Alpha (alpha),beta,"

# --- -lj exposes name<TAB>alias<TAB>activity_epoch -------------------------
# alpha (with alias) gets a fresh activity stamp; beta has none → epoch 0.
: > "$SOCKDIR/alpha.sock.act"
lj_alpha=$("$DCH" -lj 2>/dev/null | awk -F'\t' '$1=="alpha"{print NF":"($3>0)}')
check "-lj alpha: 3 fields, recent epoch" "$lj_alpha" "3:1"
lj_beta=$("$DCH" -lj 2>/dev/null | awk -F'\t' '$1=="beta"{print NF":"$3}')
check "-lj beta: 3 fields, epoch 0" "$lj_beta" "3:0"

# --- -m: a live session takes the alias; a missing one is refused (#085) ---
"$DCH" -m beta 'Bee' >/dev/null 2>&1; check "-m on a live session exits 0" "$?" "0"
check "-m writes the alias sidecar" "$(cat "$SOCKDIR/beta.sock.alias" 2>/dev/null)" "Bee"
"$DCH" -m beta >/dev/null 2>&1
[ ! -s "$SOCKDIR/beta.sock.alias" ] && ok "-m with no alias clears it" || bad "-m with no alias clears it"
m_err=$("$DCH" -m ghost 'Boo' 2>&1 >/dev/null); m_rc=$?
check "-m on a missing session exits 1" "$m_rc" "1"
check "-m on a missing session: -k's wording" "$m_err" "$("$DCH" -k ghost 2>&1 >/dev/null)"
[ ! -e "$SOCKDIR/ghost.sock.alias" ] && ok "-m on a missing session writes nothing" || bad "-m on a missing session writes nothing"

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

# --- -ls and the headless -l picker sort by shown label, case-insensitive ---
SORTDIR="$TMP/sort-dch"
mkdir -p "$SORTDIR"
for s in zeta Alpha mid zz; do mksock "$SORTDIR/$s.sock"; done
printf 'aardvark\n' > "$SORTDIR/zz.sock.alias"
want="aardvark (zz),Alpha,mid,zeta,"
srt_out=$(DCH_SOCKET_DIR="$SORTDIR" "$DCH" -ls 2>/dev/null | tr '\n' ',')
check "-ls sorted by label" "$srt_out" "$want"
# setsid drops the controlling tty so pick_session takes its headless path.
srt_out=$(DCH_SOCKET_DIR="$SORTDIR" python3 -c \
	'import os,sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \
	"$DCH" -l </dev/null 2>/dev/null | tr '\n' ',')
check "headless -l sorted by label" "$srt_out" "$want"

# --- does this build have the terminal mirror? -----------------------------
# A lite build (configure --without-libghostty) answers exit 3 to --read, so
# every check that needs --read/--wait is gated on this instead of failing.
MIRROR=0
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    "$DCH" --spawn mirrorprobe --size 80x24 sh >/dev/null 2>&1
    "$DCH" --read mirrorprobe >/dev/null 2>&1
    [ "$?" = "3" ] || MIRROR=1
    "$DCH" -k mirrorprobe >/dev/null 2>&1
fi
skipmirror(){ printf '  skip %s (build has no terminal mirror)\n' "$1"; }
# Wall-clock budgets mean nothing under ASan (scripts/sanitize.sh sets this).
nobudget(){ [ -n "${DCH_TEST_NO_BUDGET:-}" ] && printf '  skip %s (timing budget, sanitizer build)\n' "$1"; }
# Latency budget: the fastest of 20 runs of "$@" under 100 ms wall. A sleep or
# a deadline-driven read slows every run; a loaded box only some (#154).
fast20(){ python3 -c '
import subprocess, sys, time
def one():
    t = time.perf_counter()
    subprocess.run(sys.argv[1:], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    return time.perf_counter() - t
sys.exit(0 if min(one() for _ in range(20)) < 0.1 else 1)
' "$@"; }

# --- spawn-vs-attach: leftover sockets + hot-path attach -------------------
# Real forkpty spawn, so it needs an executable dch + python3. Skip gracefully.
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
	pyrun "child owns alternate-screen mode" alt_screen_test.py
	pyrun "kitty keyboard pop reaches both screens" kbd_altscreen_test.py
	pyrun "mode sequences split across pty reads are re-armed" mode_split_test.py
	pyrun "spawn over leftover socket + hot-path attach" spawn_test.py
fi

# --- hot-path performance budget (spawn-vs-attach decision) ----------------
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    pyrun "hot-path perf budget" perf_test.py
    pyrun "picker resolves harness session names" harness_names_test.py
    pyrun "--meta stores session metadata for --ls-json" meta_test.py
    pyrun "--ls-json claude_session_id and transcript_path" transcript_test.py
    pyrun "--ls-json cols, rows and attached clients" ls_json_size_test.py
    pyrun "master self-heals a deleted socket" socket_heal_test.py
    pyrun "native agent bridge public CLI and wire protocol" agent_bridge_test.py
    pyrun "shared Codex daemon never binds or names a session; --no-daemon" codex_daemon_test.py
fi

# --- on-demand redraw (SIGUSR2 → MSG_REDRAW(REDRAW_WINCH)) ------------------
# Real forkpty spawn, so it needs an executable dch + python3. Skip gracefully.
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    pyrun "SIGUSR2 redraw reaches inner program" redraw_test.py
fi

# --- attach: WINCH repaint, and NEVER a typed ^L ----------------------------
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    pyrun "attach repaints via WINCH, types no ^L" attach_test.py
    pyrun "attach is raw again after bg/fg, a startup ^Z, an outside SIGTSTP (LOW-V9)" attach_cont_test.py
    pyrun "size follows focus (DEC 1004, #100)" focus_test.py
    pyrun "attach replay with --scrollback (#101)" scrollback_test.py
fi

if [ "$MIRROR" = "1" ]; then
    pyrun "double-tap detach opens the session picker" switch_test.py
    pyrun "session picker: tabs, sort, actions, history, color, width" picker_test.py
    pyrun "the picker keeps its place across a switch" picker_place_test.py
    pyrun "the picker lines up its merged counts" picker_merged_test.py
    pyrun "the picker's History: one row per name, by name" picker_history_test.py
else
    skipmirror "double-tap detach opens the session picker"
    skipmirror "session picker: tabs, sort, actions, history, color, width"
    skipmirror "the picker keeps its place across a switch"
    skipmirror "the picker's History: one row per name, by name"
fi

# --- attach replays the mirrored screen (blank-attach regression) -----------
if [ "$MIRROR" = "1" ]; then
    pyrun "attach replays the screen" replay_test.py
    pyrun "OSC 8 links survive --read and the redraw" osc8_test.py
else
    skipmirror "attach replays the screen"
    skipmirror "OSC 8 links survive --read and the redraw"
fi

# --- an attached client survives a live restart -----------------------------
if [ "$MIRROR" = "1" ]; then
    pyrun "attached client survives --restart" restart_attach_test.py
    pyrun "half-written packet survives --restart" restart_partial_test.py
else
    skipmirror "attached client survives --restart"
    skipmirror "half-written packet survives --restart"
fi

# --- resize reaches an idle session, even with SIGWINCH blocked at spawn ----
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    pyrun "idle resize reaches the session" resize_idle_test.py
    pyrun "--follow streams whole frames on change, read-only" follow_test.py
    pyrun "respawn keeps the session across a child exit" respawn_test.py
    pyrun "--restart --include-agents restarts stale Claude agents" agent_restart_test.py
    pyrun "--history/--revive bring dead sessions back" revive_test.py
    pyrun "--revive never starts two claudes on one id" revive_sid_test.py
    pyrun "claude's one-shot prompt is never sent again (#138)" oneshot_prompt_test.py
    pyrun "--pin/--unpin/--forget keep and clean history" history_pin_test.py
    pyrun "a local a:b session stays local, remote hosts off or not (#122)" colon_name_test.py
    pyrun "new sessions: statusline links, no OSC 8 into pipes (#122)" hyperlink_env_test.py
    pyrun "--say messages Claude sessions (busy too), gated" say_test.py
    pyrun "--run into a Claude session goes through the gate (#112)" run_agent_test.py
    pyrun "--say/--run/--revive stop safely on a signal (#126)" verb_signal_test.py
    pyrun "auto-allow answers plain permission prompts; --config" auto_allow_test.py
    # --stop's own 3 s deadlines expire on the ASan-slowed macOS CI runner
    # (a different check each run, no sanitizer report); ASan on Linux and
    # every plain build still run it.
    if [ "$(uname)" = Darwin ] && nobudget "--stop pauses Claude, ends the session as stopped"; then :
    else
        pyrun "--stop pauses Claude, ends the session as stopped" stop_test.py
    fi
    pyrun "-k/--stop signal exactly the session's master" kill_test.py
    pyrun "attached client exits cleanly on SIGHUP/SIGTERM" hangup_test.py
    pyrun "records with odd bytes, long aliases and long argv read back" record_utf8_test.py
    pyrun "a signalled master's history end names the sender" signal_sender_test.py
    pyrun "--cmd survives empty reply frames" cmd_frames_test.py
    pyrun "dch serve: remote frames goldens, peer gate, startup refusals" serve_test.py
    pyrun "remote hosts: -ls, --remote, -a host:name, hostile peers" remote_client_test.py
    pyrun "remote history: --history --remote, picker History rows, --ls-json cwd" remote_history_test.py
    pyrun "the Windows client's checks, against this dch" windows_client_test.py
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

    # --- exec failure is an error, an instant exit is not (#044, #026) ----
    err=$("$DCH" --spawn "nx$$" dch-no-such-prog-$$ 2>&1 >/dev/null)
    check "--spawn of a missing program exits 1" "$?" "1"
    case "$err" in
    *"nx$$: dch-no-such-prog-$$: No such file or directory"*)
        ok "--spawn of a missing program names it and the error" ;;
    *) bad "--spawn of a missing program names it and the error ($err)" ;;
    esac
    [ ! -e "$SOCKDIR/nx$$.sock" ] && ok "a failed exec leaves no session" \
                                  || bad "a failed exec leaves no session"
    out=$("$DCH" --spawn "nx$$" sleep 60 2>/dev/null)
    check "a later --spawn of a real program works" "$out" "nx$$"
    "$DCH" -k "nx$$" >/dev/null 2>&1
    n=0 f=0
    while [ "$n" -lt 20 ]; do
        "$DCH" --spawn "tr$$-$n" true >/dev/null 2>&1 || f=$((f + 1))
        "$DCH" --spawn "tx$$-$n" sh -c 'exit 3' >/dev/null 2>&1 || f=$((f + 1))
        n=$((n + 1))
    done
    check "--spawn of a program that exits at once: exit 0 (40 runs)" "$f" "0"

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

        # DCH_SCROLLBACK=N keeps at least N lines of history above the
        # screen, and at most one ghostty page more (pages are pruned whole:
        # 591 rows at 80 cols, 238 at 200; vt.c asks for N + 606 / N + 244).
        # 0 keeps none. Before 1.28.2 N was the line limit itself, so history
        # fell up to a page below N (2000 kept ~1600); before 1.26.1 it was
        # a byte budget (~290 lines at 200 cols whatever the setting).
        # N + 2928 lines: several prunes deep, and for N=100 it lands just
        # after a limit-of-N build pruned to ~50 lines, so that build fails.
        for sbcase in 0:80x24:0 100:80x24:606 2000:80x24:606 2000:200x50:244; do
            sbn=${sbcase%%:*}; sbsz=${sbcase#*:}; sbpg=${sbsz#*:}; sbsz=${sbsz%:*}
            SBN="sb$$-$sbn-$sbpg"; sbt=$((sbn + 2928))
            DCH_SCROLLBACK=$sbn "$DCH" --spawn "$SBN" --size "$sbsz" -- \
                sh -c "seq 1 $sbt; echo SBDONE; sleep 30" >/dev/null 2>&1
            "$DCH" --wait "$SBN" --match SBDONE --timeout 20000 >/dev/null 2>&1
            sbl=$("$DCH" --read "$SBN" --recent 65535 2>/dev/null | grep -E '^[0-9]+$')
            sbs=$("$DCH" --read "$SBN" 2>/dev/null | grep -cE '^[0-9]+$')
            sbold=$(printf '%s\n' "$sbl" | head -1)
            # History = numbered lines kept above the screen's numbered lines.
            sbh=$(( $(printf '%s\n' "$sbl" | grep -c .) - sbs ))
            [ "$(printf '%s\n' "$sbl" | tail -1)" = "$sbt" ] \
                && [ "$sbold" -eq $((sbt - sbh - sbs + 1)) ] \
                && [ "$sbh" -ge "$sbn" ] && [ "$sbh" -le $((sbn + sbpg)) ] \
                && ok "DCH_SCROLLBACK=$sbn at $sbsz keeps $sbn..$((sbn + sbpg)) lines" \
                || bad "DCH_SCROLLBACK=$sbn at $sbsz keeps $sbn..$((sbn + sbpg)) lines (got $sbh, oldest $sbold of $sbt)"
            "$DCH" -k "$SBN" >/dev/null 2>&1
        done

        # Ghost text (a TUI's own placeholder/argument hint) is only
        # distinguishable from typed input by its styling, so --ansi must
        # round-trip the faint attribute and the exact fg color, and must
        # leave default-fg text unstyled. See docs/ghost-text.md.
        AN="ansi$$"
        "$DCH" --spawn "$AN" --size 40x6 sh -c 'printf "\033[2J\033[1;1Hreal\033[2m ghost\033[0m\033[3;1H\033[38;2;153;153;153mhint\033[0m"; sleep 60' \
            >/dev/null 2>&1
        "$DCH" --wait "$AN" --match ghost --timeout 5000 >/dev/null 2>&1
        ansi=$("$DCH" --read "$AN" --ansi 2>/dev/null | tr -d '\r')
        case $ansi in
        *"$(printf '\033')[2m ghost"*) ok "--read --ansi keeps faint (SGR 2)" ;;
        *) bad "--read --ansi keeps faint (SGR 2)" ;;
        esac
        case $ansi in
        *"$(printf '\033')[38;2;153;153;153mhint"*)
            ok "--read --ansi keeps the exact fg color" ;;
        *) bad "--read --ansi keeps the exact fg color" ;;
        esac
        # "real" is default-fg: it must arrive with no SGR run of its own,
        # which is what lets a watcher treat "styled" as "not typed".
        case $ansi in
        real*) ok "--read --ansi leaves default-fg text unstyled" ;;
        *) bad "--read --ansi leaves default-fg text unstyled" ;;
        esac
        "$DCH" -k "$AN" >/dev/null 2>&1

        # The caret must be REPORTED, not inferred from the byte stream:
        # typing 5 characters moves the column by exactly 5 and leaves the
        # row alone. Fails loudly if the row/col offset convention drifts.
        # Every failure mode (no mirror, old master, BUSY) puts PROSE on
        # stderr, so pick the cursor line out and refuse to do arithmetic on
        # anything else — `$(( ))` on a word aborts the whole suite.
        curline() {
            "$DCH" --read "$1" --cursor 2>&1 >/dev/null \
                | grep '^cursor ' | head -1
        }
        cur0=$(curline "$SN")
        "$DCH" --send "$SN" "abcde" >/dev/null 2>&1
        "$DCH" --wait "$SN" --match abcde --timeout 5000 >/dev/null 2>&1
        cur1=$(curline "$SN")
        r0=$(echo "$cur0" | cut -d' ' -f2); c0=$(echo "$cur0" | cut -d' ' -f3)
        r1=$(echo "$cur1" | cut -d' ' -f2); c1=$(echo "$cur1" | cut -d' ' -f3)
        case "$r0$c0$r1$c1" in
        '' | *[!0-9]*)
            bad "--read --cursor tracks typed columns ([$cur0] -> [$cur1])" ;;
        *)
            [ "$r0" = "$r1" ] && [ "$((c1 - c0))" -eq 5 ] \
                && ok "--read --cursor tracks typed columns" \
                || bad "--read --cursor tracks typed columns ([$cur0] -> [$cur1])" ;;
        esac
        # --cursor must not silently pollute the screen dump.
        "$DCH" --read "$SN" --cursor 2>/dev/null | grep -q '^cursor ' \
            && bad "--read --cursor keeps stdout clean" \
            || ok "--read --cursor keeps stdout clean"
        # Caret is a visible-screen property; refuse the scrollback tail.
        "$DCH" --read "$SN" --cursor --recent 5 >/dev/null 2>&1
        check "--read --cursor rejects --recent" "$?" "1"
        # Drop the half-typed line so later markers start from a clean prompt.
        "$DCH" --keys "$SN" ctrl+c >/dev/null 2>&1

        # Absolute-addressed caret: CUP to row 5 col 3, print 4 chars, so the
        # answer is pinned to "5 7" with no prompt-width guessing. Also pins
        # row N of the report to line N of the screen dump.
        CN="cur$$"
        "$DCH" --spawn "$CN" --size 40x10 \
            sh -c 'printf "\033[2J\033[5;3Hmark"; sleep 60' >/dev/null 2>&1
        "$DCH" --wait "$CN" --match mark --timeout 5000 >/dev/null 2>&1
        check "--read --cursor reports absolute position" \
              "$(curline "$CN")" "cursor 5 7 1 0"
        "$DCH" --read "$CN" 2>/dev/null | sed -n '5p' | grep -q '^  mark' \
            && ok "--read --cursor row N is dump line N" \
            || bad "--read --cursor row N is dump line N"
        "$DCH" -k "$CN" >/dev/null 2>&1

        # Pending soft-wrap: exactly $cols characters leaves the caret ON the
        # last column with wrap=1, NOT on column cols+1 or the next row.
        WN="wrap$$"
        "$DCH" --spawn "$WN" --size 20x8 \
            sh -c 'printf "\033[2J\033[1;1Hwwwwwwwwwwwwwwwwwwww"; sleep 60' \
            >/dev/null 2>&1
        "$DCH" --wait "$WN" --match wwwwwwwwwwwwwwwwwwww --timeout 5000 \
            >/dev/null 2>&1
        check "--read --cursor reports pending soft-wrap" \
              "$(curline "$WN")" "cursor 1 20 1 1"
        "$DCH" -k "$WN" >/dev/null 2>&1

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

    # A child producing output nonstop must not wedge the master: the drain
    # before each control verb is bounded (DRAIN_MAX_BUFS), so --read and
    # --keys stay responsive against a flooding pty.
    if [ "$mirror" -eq 0 ]; then
        "$DCH" --run "$SN" "yes flooding" >/dev/null 2>&1
        sleep 0.5
        t0=$(python3 -c 'import time; print(time.time())')
        "$DCH" --read "$SN" >/dev/null 2>&1
        rc=$?
        fast=$(python3 -c "import time; print(0 if time.time()-$t0 < 5.0 else 1)")
        [ "$rc" -eq 0 ] && [ "$fast" -eq 0 ] \
            && ok "--read responsive under flooding child" \
            || bad "--read responsive under flooding child (rc=$rc fast=$fast)"
        "$DCH" --keys "$SN" ctrl+c >/dev/null 2>&1
        "$DCH" --run "$SN" "echo flood_over" >/dev/null 2>&1
        "$DCH" --wait "$SN" --match flood_over --timeout 5000 >/dev/null 2>&1 \
            && ok "--keys ctrl+c stops flooding child" \
            || bad "--keys ctrl+c stops flooding child"
    fi

    # Control-verb latency budget: a --read round trip within 100 ms (generous
    # CI headroom over the ~3 ms local reality; catches accidental sleeps or
    # timeout-driven reads).
    if [ "$mirror" -eq 0 ] && ! nobudget "--read latency budget (fastest of 20 < 100ms)"; then
        fast20 "$DCH" --read "$SN" \
            && ok "--read latency budget (fastest of 20 < 100ms)" \
            || bad "--read latency budget (fastest of 20 < 100ms)"
    fi

    "$DCH" --ls-json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert any(s["name"] == sys.argv[1] for s in d), d
assert all(s["state"] in ("working", "idle", "blocked", "done") for s in d), d
' "$SN" && ok "--ls-json valid JSON with session + state" \
       || bad "--ls-json valid JSON with session + state"

    # --status: fresh output = working; stale .act sidecar = idle.
    "$DCH" --run "$SN" "echo status_ping" >/dev/null 2>&1
    sleep 1
    check "--status recent output is working" \
          "$("$DCH" --status "$SN" 2>/dev/null)" "working"
    touch -t 200001010000 "$SOCKDIR/$SN.sock.act"
    check "--status stale output is idle" \
          "$("$DCH" --status "$SN" 2>/dev/null)" "idle"
    "$DCH" --status no_such_session_zz >/dev/null 2>&1
    check "--status missing session exits 1" "$?" "1"

    # --report: pushed semantic states override the output heuristic.
    "$DCH" --report "$SN" blocked >/dev/null 2>&1
    check "--report state overrides heuristic" \
          "$("$DCH" --status "$SN" 2>/dev/null)" "blocked"
    "$DCH" --ls-json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
s = next(s for s in d if s["name"] == sys.argv[1])
assert s["state"] == "blocked", s
' "$SN" && ok "--ls-json surfaces reported state" \
       || bad "--ls-json surfaces reported state"
    check "--wait --state comma list matches" \
          "$("$DCH" --wait "$SN" --state idle,blocked,done --timeout 2000 2>/dev/null)" \
          "blocked"
    "$DCH" --wait "$SN" --state done --timeout 300 >/dev/null 2>&1
    check "--wait --state times out with exit 2" "$?" "2"
    "$DCH" --report "$SN" nonsense >/dev/null 2>&1
    check "--report rejects unknown state" "$?" "1"
    "$DCH" --report no_such_session_zz working >/dev/null 2>&1
    check "--report missing session exits 1" "$?" "1"
    "$DCH" --report "$SN" clear >/dev/null 2>&1
    touch -t 200001010000 "$SOCKDIR/$SN.sock.act"
    check "--report clear reverts to heuristic" \
          "$("$DCH" --status "$SN" 2>/dev/null)" "idle"
    printf 'gar"bage' > "$SOCKDIR/$SN.sock.state"
    check "--status ignores corrupt state sidecar" \
          "$("$DCH" --status "$SN" 2>/dev/null)" "idle"
    "$DCH" --ls-json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
s = next(s for s in d if s["name"] == sys.argv[1])
assert s["state"] in ("idle", "working", "blocked", "done"), s
' "$SN" && ok "--ls-json valid despite corrupt sidecar" \
       || bad "--ls-json valid despite corrupt sidecar"
    "$DCH" --wait no_such_session_zz --state idle --timeout 500 >/dev/null 2>&1
    check "--wait --state missing session exits 1" "$?" "1"
    "$DCH" --report "$SN" working >/dev/null 2>&1  # left for -k to clean

    "$DCH" -k "$SN" >/dev/null 2>&1
    [ ! -e "$SOCKDIR/$SN.sock.state" ] \
        && ok "kill removes state sidecar" \
        || bad "kill removes state sidecar"

    # Long names get hashed socket filenames; sidecars must derive from
    # the same normalization or --status/--ls-json read the wrong files.
    LN="$(printf 'l%.0s' $(seq 1 200))$$"
    "$DCH" --spawn "$LN" sh >/dev/null 2>&1
    n=0
    while [ "$("$DCH" --status "$LN" 2>/dev/null)" != "working" ] \
          && [ "$n" -lt 100 ]; do
        "$DCH" --run "$LN" "echo long_ping" >/dev/null 2>&1
        sleep 0.1; n=$((n + 1))
    done
    check "long-name --status sees activity" \
          "$("$DCH" --status "$LN" 2>/dev/null)" "working"
    "$DCH" --report "$LN" blocked >/dev/null 2>&1
    check "long-name --report round-trips" \
          "$("$DCH" --status "$LN" 2>/dev/null)" "blocked"
    "$DCH" --ls-json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert any(s["state"] == "blocked" for s in d), d
' && ok "long-name --ls-json surfaces reported state" \
   || bad "long-name --ls-json surfaces reported state"
    "$DCH" -k "$LN" >/dev/null 2>&1
    n=$(find "$SOCKDIR" -name '*.sock.state' 2>/dev/null | wc -l)
    check "long-name kill removes hashed sidecars" "$((n + 0))" "0"

    # --- built-in state detection: screen-content rules ---------------------
    # Fake harness: paint agent-UI footer/prompt strings into a quiet shell
    # (echo off so the painted commands themselves never reach the grid),
    # then assert what --status infers. Every assertion follows a --wait
    # --match barrier on a per-paint sentinel, so the grid is provably
    # repainted before we look. Needs the mirror; the lite master path is
    # covered by the heuristic tests above (detection falls back silently).
    if [ "$mirror" -eq 0 ]; then
        DN="det$$"
        "$DCH" --spawn "$DN" --size 80x24 sh >/dev/null 2>&1
        "$DCH" --run "$DN" "stty -echo" >/dev/null 2>&1
        paint() { # paint $2 (printf %b, so \n splits lines) + sentinel $1
            # \033c (RIS) resets the screen without the external `clear`
            # binary, which stripped containers may lack.
            "$DCH" --run "$DN" "printf '\033c'; printf '%b\n' \"$2\" 'sent_$1'" >/dev/null 2>&1
            "$DCH" --wait "$DN" --match "sent_$1" --timeout 5000 >/dev/null 2>&1
            # The master throttles .act touches to 1/s; consecutive paints
            # inside one second would keep a backdated stamp. Re-stamp so
            # "fresh output" is deterministic regardless of test pacing.
            touch "$SOCKDIR/$DN.sock.act"
        }
        stale() { touch -t 200001010000 "$SOCKDIR/$DN.sock.act"; }

        paint w1 'Esc to interrupt'
        check "detect working from footer" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "working"
        stale
        check "detect working needs fresh output" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "idle"

        paint w2 '\342\240\213 Thinking...'
        check "detect working from spinner" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "working"

        # blocked needs no fresh output: a prompt can sit for hours.
        paint b1 'Do you want to proceed?'
        stale
        check "detect blocked overrides idle heuristic" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"

        paint b2 'esc to interrupt\nDo you want to proceed?'
        check "blocked beats working on the same screen" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"

        # blocker-override chain: detected blocked beats a stale report,
        # reported done beats everything, clear re-exposes the screen.
        "$DCH" --report "$DN" working >/dev/null 2>&1
        check "detected blocked overrides reported working" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"
        "$DCH" --report "$DN" done >/dev/null 2>&1
        check "reported done beats detected blocked" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "done"
        "$DCH" --report "$DN" clear >/dev/null 2>&1
        check "report clear re-exposes detected blocked" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"

        paint r1 'esc to interrupt'
        "$DCH" --report "$DN" idle >/dev/null 2>&1
        check "reported idle beats detected working" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "idle"
        "$DCH" --report "$DN" clear >/dev/null 2>&1

        # transcript viewers replay old prompts; suppression must win.
        paint s1 'Showing detailed transcript\nDo you want to proceed?'
        check "transcript view suppresses blocked" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "working"

        # two-string rules need both halves on screen.
        paint f1 'esc to cancel'
        check "half of a paired rule does not block" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "working"

        # Claude Write/Edit permission dialogs: matched on the shared
        # footer, not the question (\302\267 is the middle dot).
        paint c1 ' Create file\n notes.txt\n  1 hello\n Do you want to create notes.txt?\n \342\235\257 1. Yes\n   3. No\n Esc to cancel \302\267 Tab to amend'
        stale
        check "detect claude write prompt as blocked" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"

        paint c2 ' Edit file\n Do you want to make this edit to foo.py?\n \342\235\257 1. Yes\n   3. No\n Esc to cancel \302\267 Tab to amend'
        stale
        check "detect claude edit prompt as blocked" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"

        # the footer rule is anchored to the footer line: prose quoting
        # both phrases (split, or mid-sentence) is not a dialog.
        paint c4 'The dialog footer says Esc to cancel\nand then Tab to amend.'
        stale
        check "footer phrases on separate lines do not block" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "idle"

        paint c5 'commit: the Write and Edit dialogs share the footer Esc to cancel \302\267 Tab to amend so match on it'
        stale
        check "footer quoted mid-sentence does not block" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "idle"

        # Claude Code's footer runs to the screen width while background
        # agents run; the permission footer still matches.
        paint c6 ' Edit file\n Do you want to make this edit to foo.py?\n \342\235\257 1. Yes\n   3. No\n Esc to cancel \302\267 Tab to amend \302\267 ctrl+x ctrl+k twice to stop background agents'
        stale
        check "long permission footer is blocked" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"

        # with the composer on screen, a prompt phrase above it is
        # transcript (a reply quoting it), not a prompt (Prosper #757)
        paint c7 '  The prompt reads \342\200\234Do you want to proceed?\342\200\235 with Yes / No.\n\n\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\n\342\235\257 \n\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\n  ctrl+x ctrl+k twice to stop background agents\n\342\217\272 main\n\342\227\257 worker  #1 task'
        stale
        check "prompt phrase above the composer does not block" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "idle"

        # rows under the composer's closing rule still count
        paint c8 '\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\n\342\235\257 \n\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\342\224\200\n Do you want to proceed?\n \342\235\257 1. Yes'
        stale
        check "prompt phrase below the composer blocks" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"

        # Claude Code's plan approval (#092) draws no esc footer: its
        # question's start blocks, wrapped under ~90 columns too
        paint p1 ' Claude has written up a plan and is ready to execute. Would you like to\n proceed?\n\n \342\235\257 1. Yes, and use auto mode\n   2. Yes, manually approve edits\n   3. Tell Claude what to change\n      shift+tab to approve with this feedback\n\n ctrl+g to edit in VS Code \302\267 ~/.claude/plans/example-plan.md'
        stale
        check "detect claude plan approval as blocked" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"

        paint p2 'Claude Code said: Claude has written up a plan and is ready.'
        stale
        check "plan question quoted mid-line does not block" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "idle"

        paint c3 'Do you want to create a file?'
        stale
        check "prose 'do you want to create' does not block" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "idle"

        paint q1 'nothing to see here'
        stale
        check "no signals, stale output is idle" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "idle"

        paint n1 'Allow command?'
        check "detection on: blocked" \
              "$("$DCH" --status "$DN" 2>/dev/null)" "blocked"
        check "DCH_NO_DETECT=1 disables detection" \
              "$(DCH_NO_DETECT=1 "$DCH" --status "$DN" 2>/dev/null)" "working"
        check "DCH_NO_DETECT=0 leaves detection on" \
              "$(DCH_NO_DETECT=0 "$DCH" --status "$DN" 2>/dev/null)" "blocked"
        check "DCH_NO_DETECT= (empty) leaves detection on" \
              "$(DCH_NO_DETECT= "$DCH" --status "$DN" 2>/dev/null)" "blocked"

        paint ww 'esc to interrupt'
        check "--wait --state sees detected working" \
              "$("$DCH" --wait "$DN" --state working --timeout 3000 2>/dev/null)" \
              "working"
        check "--wait --state active is an alias for working" \
              "$("$DCH" --wait "$DN" --state active --timeout 3000 2>/dev/null)" \
              "working"

        paint lj 'Allow command?'
        "$DCH" --ls-json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
s = next(s for s in d if s["name"] == sys.argv[1])
assert s["state"] == "blocked", s
' "$DN" && ok "--ls-json carries detected state" \
       || bad "--ls-json carries detected state"

        # Claude Code's agent panel: "⏺ main" then one row per subagent.
        agents() {
            "$DCH" --ls-json 2>/dev/null | python3 -c '
import json, sys
print(next(s for s in json.load(sys.stdin) if s["name"] == sys.argv[1])["agents"])
' "$DN"
        }
        check "no agent panel reads 0 agents" "$(agents)" "0"
        paint a1 'footer\n\n  \342\217\272 main\n  \342\227\257 worker-opus-high  Running… 5m\n  \342\227\257 worker-opus-medium  Drafting… 1m'
        check "--ls-json counts agent panel rows" "$(agents)" "2"
        check "DCH_NO_DETECT=1 reads 0 agents" \
              "$(export DCH_NO_DETECT=1; agents)" "0"
        check "DCH_NO_DETECT=0 still counts agents" \
              "$(export DCH_NO_DETECT=0; agents)" "2"
        paint a2 '\342\217\272 main\n\342\227\257 worker-opus-high  Running…\nsome prose\n\342\227\257 not a row'
        check "agent rows stop at the first non-row line" "$(agents)" "1"
        paint a3 '\342\217\272 main is great\n\342\227\257 worker-opus-high  Running…'
        check "a line merely starting with main is no panel" "$(agents)" "0"

        # Hot-path budget: every --status runs detection (a control connect
        # + full screen fetch), and --ls-json runs it per session. A call
        # within 100 ms (generous CI headroom over the ~4 ms local reality;
        # catches accidental sleeps or deadline-driven reads sneaking into
        # the resolve path).
        if ! nobudget "--status latency budget (fastest of 20 < 100ms)"; then
            fast20 "$DCH" --status "$DN" \
                && ok "--status latency budget (fastest of 20 < 100ms)" \
                || bad "--status latency budget (fastest of 20 < 100ms)"
        fi
        if ! nobudget "--ls-json latency budget (fastest of 20 < 100ms)"; then
            fast20 "$DCH" --ls-json \
                && ok "--ls-json latency budget (fastest of 20 < 100ms)" \
                || bad "--ls-json latency budget (fastest of 20 < 100ms)"
        fi

        "$DCH" -k "$DN" >/dev/null 2>&1
    fi

    # --- --spawn --env: caller variables, reserved keys win ------------------
    EN="env$$"
    if [ "$mirror" -eq 0 ]; then
        "$DCH" --spawn "$EN" --env CREW_ROLE=x --env V=a=b \
               --env DCH_SESSION=fake sh >/dev/null 2>&1
        check "--spawn --env accepted" "$?" "0"
        # Matching the expanded output (not the typed line: that shows the
        # unexpanded $names) proves the values reached the child env.
        "$DCH" --run "$EN" 'echo env_out $CREW_ROLE $V' >/dev/null 2>&1
        "$DCH" --wait "$EN" --match "env_out x a=b" --timeout 5000 >/dev/null 2>&1
        check "--env values reach child (first = splits)" "$?" "0"
        "$DCH" --run "$EN" 'echo sess_out $DCH_SESSION' >/dev/null 2>&1
        "$DCH" --wait "$EN" --match "sess_out $EN" --timeout 5000 >/dev/null 2>&1
        check "--env cannot spoof DCH_SESSION" "$?" "0"
        "$DCH" -k "$EN" >/dev/null 2>&1
    fi
    "$DCH" --spawn "${EN}b" --env FOO sh >/dev/null 2>&1
    check "--env without = exits 1" "$?" "1"
    "$DCH" --spawn "${EN}b" --env =VAL sh >/dev/null 2>&1
    check "--env empty key exits 1" "$?" "1"
    set --
    n=0
    while [ "$n" -lt 33 ]; do set -- "$@" --env "K$n=v"; n=$((n + 1)); done
    "$DCH" --spawn "${EN}b" "$@" sh >/dev/null 2>&1
    check "--env 33rd is a hard error" "$?" "1"

    # Real alt-screen TUI end-to-end: drive vim without attaching. Covers
    # mode-aware --keys (esc, ":" as shift+; chord) and screen rendering.
    if [ "$mirror" -eq 0 ] && command -v vim >/dev/null 2>&1; then
        VOUT="$TMP/vim_out"
        "$DCH" --spawn "vim$$" --size 80x24 vim -u NONE >/dev/null 2>&1
        "$DCH" --wait "vim$$" --match "IMproved" --timeout 15000 >/dev/null 2>&1
        "$DCH" --keys "vim$$" i >/dev/null 2>&1
        "$DCH" --send "vim$$" "tui marker line" >/dev/null 2>&1
        "$DCH" --keys "vim$$" esc >/dev/null 2>&1
        if "$DCH" --wait "vim$$" --match "tui marker line" --timeout 10000 \
               >/dev/null 2>&1; then
            ok "vim e2e: typed text renders"
        else
            bad "vim e2e: typed text renders"
            "$DCH" --read "vim$$" 2>&1 | sed 's/^/       | /'
        fi
        "$DCH" --keys "vim$$" : >/dev/null 2>&1
        "$DCH" --send "vim$$" "wq! $VOUT" >/dev/null 2>&1
        "$DCH" --keys "vim$$" enter >/dev/null 2>&1
        n=0
        while [ ! -f "$VOUT" ] && [ "$n" -lt 100 ]; do
            sleep 0.1; n=$((n + 1))
        done
        if grep -q "tui marker line" "$VOUT" 2>/dev/null; then
            ok "vim e2e: ':' keys reach command mode, file saved"
        else
            bad "vim e2e: ':' keys reach command mode, file saved"
            "$DCH" --read "vim$$" 2>&1 | sed 's/^/       | /'
        fi
        "$DCH" -k "vim$$" >/dev/null 2>&1
    fi

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

    # DCH_NO_VT=0 is off, like every DCH_NO_* (#085): the mirror stays.
    if [ "$mirror" -eq 0 ]; then
        DCH_NO_VT=0 "$DCH" --spawn "vt0$$" sh >/dev/null 2>&1
        "$DCH" --read "vt0$$" >/dev/null 2>&1
        check "DCH_NO_VT=0 keeps the mirror: --read exits 0" "$?" "0"
        "$DCH" -k "vt0$$" >/dev/null 2>&1
    fi

    # Live restart: the master re-execs itself in place. The session's child,
    # its pid, its screen and its scrollback all have to survive, and the
    # session must still take input afterwards.
    RN="restart$$"
    "$DCH" --spawn "$RN" --size 80x24 sh >/dev/null 2>&1
    "$DCH" --send "$RN" "echo pre-mark" >/dev/null 2>&1
    "$DCH" --wait "$RN" --match "pre-mark" --timeout 10000 \
        >/dev/null 2>&1
    RPID_BEFORE=$("$DCH" --ls-json 2>/dev/null | tr ',' '\n' |
        grep -c "\"version\":\"[0-9]" || true)
    check "--ls-json carries a version field" \
        "$([ "$RPID_BEFORE" -ge 1 ] && echo yes || echo no)" "yes"

    "$DCH" --restart "$RN" >/dev/null 2>&1
    check "--restart exits 0" "$?" "0"

    # The screen the old master rendered is still there: proof the mirror was
    # carried across the exec rather than rebuilt empty. Both this and the
    # input check below read through the mirror, so a lite build skips them.
    if [ "$MIRROR" = "1" ]; then
        if "$DCH" --read "$RN" 2>/dev/null | grep -q "pre-mark"; then
            ok "--restart preserves the mirrored screen"
        else
            bad "--restart preserves the mirrored screen"
            "$DCH" --read "$RN" 2>&1 | sed 's/^/       | /'
        fi

        # ...and the shell is the same live process, still reading its pty.
        "$DCH" --send "$RN" "echo post-mark" >/dev/null 2>&1
        "$DCH" --wait "$RN" --match "post-mark" --timeout 10000 \
            >/dev/null 2>&1
        check "session still takes input after --restart" "$?" "0"
    else
        skipmirror "--restart preserves the mirrored screen"
        skipmirror "session still takes input after --restart"
    fi

    "$DCH" --restart --all >/dev/null 2>&1
    check "--restart --all exits 0" "$?" "0"

    "$DCH" -k "$RN" >/dev/null 2>&1
    "$DCH" --restart "no-such-session-$$" >/dev/null 2>&1
    check "--restart on a missing session exits 1" "$?" "1"

    # Bare-name dch (#038): run as "dch" off PATH from a cwd with no ./dch.
    # The client must send an absolute binary; a bare "dch" made the master
    # execv() "dch" against its cwd and answer "re-exec failed".
    real="$(cd "$(dirname "$DCH")" && pwd)/$(basename "$DCH")"
    mkdir -p "$TMP/bare/bin" "$TMP/bare/cwd"
    ln -sf "$real" "$TMP/bare/bin/dch"
    BN="bare$$"
    out=$(cd "$TMP/bare/cwd" && export PATH="$TMP/bare/bin:$PATH" &&
        dch --spawn "$BN" sleep 60 >/dev/null 2>&1 &&
        dch --restart "$BN" 2>&1)
    case "$out" in
        *"re-exec failed"*) bad "bare-name --restart re-execs ($out)" ;;
        *"restarted on dch"*) ok "bare-name --restart re-execs" ;;
        *) bad "bare-name --restart re-execs ($out)" ;;
    esac
    "$DCH" -k "$BN" >/dev/null 2>&1
fi

# --- --spawn waits until the master accepts (#035) --------------------------
if [ -x "$DCH" ]; then
    # Slow master start: a bare-name `dch` re-execs itself via PATH, so a
    # wrapper there delays only the master by 2s (past the old 1s cap).
    if [ -x /bin/bash ]; then
        real="$(cd "$(dirname "$DCH")" && pwd)/$(basename "$DCH")"
        mkdir -p "$TMP/slow"
        printf '#!/bin/bash\n[ "$1" = --master-of ] && sleep 2\nexec -a dch "%s" "$@"\n' \
            "$real" >"$TMP/slow/dch"
        chmod +x "$TMP/slow/dch"
        PATH="$TMP/slow:$PATH" dch --spawn "slow$$" sleep 60 >/dev/null 2>&1
        check "--spawn waits for a slow-starting master" "$?" "0"
        "$DCH" -k "slow$$" >/dev/null 2>&1
    fi
fi

# --- claude's one-shot prompt (#138): cargv.c units ---------------------------
csrc=$(cd "$(dirname "$0")/.." && pwd)
ccc=$(sed -n 's/^CC = //p' Makefile 2>/dev/null)
# shellcheck disable=SC2086 # CC may carry flags
if ${ccc:-cc} -O1 -I"$csrc" -o "$TMP/cargv_unit" "$csrc/tests/cargv_unit.c" \
    "$csrc/cargv.c" >"$TMP/cargv_unit.log" 2>&1; then
    if "$TMP/cargv_unit" >"$TMP/cargv_unit.log" 2>&1; then
        ok "claude argv units: the one-shot prompt"
    else
        bad "claude argv units"; grep -v '^  ok' "$TMP/cargv_unit.log" | sed 's/^/       /'
    fi
else
    bad "cargv_unit builds"; sed 's/^/       /' "$TMP/cargv_unit.log"
fi

# --- X-ray shell ring (#119): ring.c units, capture through the mirror, the
# .ring.ndjson golden round-trip, then the master over its socket ----------
xsrc=$(cd "$(dirname "$0")/.." && pwd)
xcc=$(sed -n 's/^CC = //p' Makefile 2>/dev/null)
xcc=${xcc:-cc}
if command -v "${xcc%% *}" >/dev/null 2>&1; then  # CC may carry flags
    xlibs=$(sed -n 's/^VT_LIBS = //p' Makefile 2>/dev/null)
    xvt=
    [ "$MIRROR" = "1" ] && [ -n "$xlibs" ] &&
        xvt="-DWITH_VT -I$xsrc/vendor/libghostty-vt/include $xsrc/vt.c $xlibs"
    # shellcheck disable=SC2086 # xcc and xvt are word lists
    if $xcc -O1 -I"$xsrc" -I. -o "$TMP/ring_unit" "$xsrc/tests/ring_unit.c" \
        "$xsrc/ring.c" $xvt >"$TMP/ring_unit.log" 2>&1; then
        if "$TMP/ring_unit" >"$TMP/ring_unit.log" 2>&1; then
            ok "ring units$([ -n "$xvt" ] && echo ' + capture through the mirror')"
        else
            bad "ring units"; grep -v '^  ok' "$TMP/ring_unit.log" | sed 's/^/       /'
        fi
        "$TMP/ring_unit" roundtrip "$xsrc/tests/xray/golden.ring.ndjson" |
            cmp -s - "$xsrc/tests/xray/golden.ring.ndjson"
        check ".ring.ndjson golden: load, dump, byte-identical" "$?" "0"
        xrt=0 xrn=0
        for f in "$xsrc"/tests/xray/fixtures/*/shell/*.ring.ndjson; do
            xrn=$((xrn + 1))
            "$TMP/ring_unit" roundtrip "$f" | cmp -s - "$f" || xrt=$((xrt + 1))
        done
        check "fixture rings ($xrn): load, dump, byte-identical" "$xrt" "0"
    else
        bad "ring_unit builds"; sed 's/^/       /' "$TMP/ring_unit.log"
    fi
else
    bad "ring units: no compiler ($xcc)"
fi
if [ "$MIRROR" = "1" ]; then
    pyrun "X-ray ring: read op, wakes, clears, --restart handover, traces" xray_ring_test.py
else
    skipmirror "X-ray ring: read op, wakes, clears, --restart handover, traces"
fi

# --- dch --xray (#120): the engine's units, the goldens, real masters -------
# shellcheck disable=SC2086 # xcc is a word list
if $xcc -O1 -I"$xsrc" -I. -o "$TMP/xray_unit" "$xsrc/tests/xray_unit.c" \
    "$xsrc/xray.c" "$xsrc/json.c" >"$TMP/xray_unit.log" 2>&1; then
    if "$TMP/xray_unit" >"$TMP/xray_unit.log" 2>&1; then
        ok "xray units: titles, project key, ids, the seam, decoder caps, resolvers"
    else
        bad "xray units"; grep -v '^  ok' "$TMP/xray_unit.log" | sed 's/^/       /'
    fi
else
    bad "xray_unit builds"; sed 's/^/       /' "$TMP/xray_unit.log"
fi
# spec 2.1: the engine is client-only; the master never links an xr_ symbol
if [ -f master.o ] && command -v nm >/dev/null 2>&1; then
    xrsym=$(nm master.o 2>/dev/null | grep -cE ' _?xr_')
    check "the master links no x-ray engine symbol (nm master.o)" "$xrsym" "0"
else
    printf '  skip the master links no x-ray engine symbol (%s)\n' \
        "$( [ -f master.o ] && echo 'no nm' || echo 'no master.o')"
fi
# spec 9.5 "0 network": the view (#175) opens no socket and runs no program;
# it forks dch itself for the engine's answers
if [ -f xview.o ] && command -v nm >/dev/null 2>&1; then
    xvsym=$(nm xview.o 2>/dev/null |
        grep -cE ' U _?(socket|connect|exec[lv]p?e?|posix_spawnp?|popen|system)$')
    check "the X-ray view links no socket or exec call (nm xview.o)" "$xvsym" "0"
else
    printf '  skip the X-ray view links no socket or exec call (%s)\n' \
        "$( [ -f xview.o ] && echo 'no nm' || echo 'no xview.o')"
fi
pyrun "xray goldens: chapters, identity, corpus labels, MANIFEST" xray_golden.py
pyrun "the --xray view: tabs, card, reader, hostile text, signals, picker X" xray_view_test.py
pyrun "the --xray view: a query child stopped right after its fork leaves the terminal alone, direct and chord" xray_fork_sig_test.py
pyrun "the --xray view repaints after a stop and fg, direct and chord (#255)" xray_view_cont_test.py
# #25: a socket-write interposer (tests/sockfail.c) makes every query child
# of the chord's view exit(1); pyrun shows a passing test's skip line only here
pyrun "the chord's query child exit()s: the client's terminal and marker stay" \
    chord_fork_exit_test.py
grep '^  skip ' "$TMP/pyout"
pyrun "the --xray view of host:name over a loopback dch serve, picker X, the chord over dch -a host:name" xray_remote_view_test.py
pyrun "the --xray view's o: a local host:name that has ended, a host that answers another name (#232)" xray_view_o_test.py
pyrun "the --xray answer's claudes and the view's line for it, local and over a loopback dch serve (#256)" xray_claudes_test.py
# xray_cli_test.py's RSS budget: a fork()+exec+wait4 helper, because a child
# spawned by Python inherits Python's RSS high-water mark on Linux (maxrss.c)
# shellcheck disable=SC2086 # xcc is a word list
if $xcc -O1 -o "$TMP/maxrss" "$xsrc/tests/maxrss.c" >"$TMP/maxrss.log" 2>&1; then
    export DCH_MAXRSS="$TMP/maxrss"
else
    bad "maxrss helper builds"; sed 's/^/       /' "$TMP/maxrss.log"
fi
if [ "$MIRROR" = "1" ]; then
    pyrun "xray: a typed shell block, --clear, a live Claude, the seam, names" xray_cli_test.py
    # pyrun shows a passing test's own skip lines only on failure
    [ -n "${ASAN_OPTIONS:-}" ] &&
        printf '  skip xray: the 16 MiB RSS budget (sanitizer build)\n'
    pyrun "xray resolvers: commit and file, the hostile-repo sentinel" xray_resolve_test.py
else
    skipmirror "xray: a typed shell block, --clear, a live Claude, the seam, names"
    skipmirror "xray resolvers: commit and file, the hostile-repo sentinel"
fi
# a live Codex's rollout (#160, 7.4): the one-UUID mapping, the seam, --ref,
# --follow; its ring-pause part skips itself without the mirror
pyrun "xray: a live Codex's rollout, the mapping, the seam, --follow" xray_codex_test.py
# the file window's guard (#178): QA's repro with FAKE files, local and over
# a loopback dch serve, through ended sessions so lite runs it too
pyrun "xray file windows: no wide root, the deny list, hard links" xray_deny_test.py

# --- the watchdog ends a hung test, its child and its session, only them ----
# watchdog_hang.py records its pid, a child's and a session master's, then
# hangs past a 3 s watchdog. The sleep is a bystander in run.sh's own group.
# The subshell keeps the hung test's FAIL out of the counts.
if [ -x "$DCH" ] && command -v python3 >/dev/null 2>&1; then
    wdf=$fail wdpy='' wdkid='' wdm='' wdname=''
    sleep 30 & wdby=$!
    (DCH_TEST_TIMEOUT=3 DCH_WD_PIDS="$TMP/wd.pids"; export DCH_WD_PIDS
     pyrun "hung" watchdog_hang.py) >"$TMP/wd.out" 2>&1
    [ -f "$TMP/wd.pids" ] && read -r wdpy wdkid wdm wdname <"$TMP/wd.pids"
    wdgone(){ for p; do
        case $p in ''|*[!0-9]*) return 1 ;; esac
        ! kill -0 "$p" 2>/dev/null || return 1
    done; }
    n=0
    while [ "$n" -lt 50 ] && ! wdgone "$wdpy" "$wdkid" "$wdm"; do
        sleep 0.1; n=$((n + 1))
    done
    grep -q '^  FAIL hung$' "$TMP/wd.out" && grep -q 'Timeout (' "$TMP/wd.out" \
        && ok "watchdog: a hung test fails, with its traceback" \
        || bad "watchdog: a hung test fails, with its traceback"
    wdgone "$wdpy" "$wdkid" && ok "watchdog: the hung test and its child end" \
        || bad "watchdog: the hung test and its child end"
    wdgone "$wdm" && [ -n "$wdname" ] && [ ! -e "$SOCKDIR/$wdname.sock" ] \
        && ok "watchdog: the session it started in the socket dir ends" \
        || bad "watchdog: the session it started in the socket dir ends"
    kill -0 "$wdby" 2>/dev/null && ok "watchdog: a process outside its group lives" \
        || bad "watchdog: a process outside its group lives"
    kill "$wdby"
    [ "$fail" = "$wdf" ] || sed 's/^/       /' "$TMP/wd.out"
    [ -S "$SOCKDIR/$wdname.sock" ] && "$DCH" -k "$wdname" >/dev/null 2>&1
    # DCH_TEST_TIMEOUT 0 (no watchdog) and invalid values (600) pass a test
    n=0
    for v in 0 '' -1 abc; do
        (DCH_TEST_TIMEOUT=$v; pyrun "t" watchdog_hang.py) | grep -q '^  ok   t$' &&
            n=$((n + 1))
    done
    check "DCH_TEST_TIMEOUT 0, empty, -1 or abc: a test still passes" "$n" "4"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
