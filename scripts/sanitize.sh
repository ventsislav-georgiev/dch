#!/bin/sh
# Build dch with AddressSanitizer + UndefinedBehaviorSanitizer and run the
# full suite under it. Extra arguments go to ./configure (e.g.
# --without-libghostty for the lite build). Rebuilds the tree in place.
#
# Reports go to files, not stderr: masters are detached with stderr on
# /dev/null, so a report on stderr would be lost, and the suite compares
# stderr text. Any report file fails the run, as does a failing test.
#
# detect_leaks=0: dch's verbs are short-lived processes that exit without
# freeing (by design), and LeakSanitizer does not run on macOS at all, so
# leak reports would differ per platform and say nothing about memory safety.
set -eu
cd "$(dirname "$0")/.."
SAN="-fsanitize=address,undefined -fno-omit-frame-pointer"
./configure --disable-hardening "$@" CFLAGS="$SAN -g -O1" LDFLAGS="$SAN" >/dev/null
make clean >/dev/null
make

logs=$(mktemp -d)
trap 'rm -rf "$logs"' EXIT
export ASAN_OPTIONS="halt_on_error=1:detect_leaks=0:log_path=$logs/asan"
# Linux (gcc links libasan dynamically): a test LD_PRELOADs a link() shim,
# and ASan refuses to start unless its runtime comes first in the library
# list. The shim interposes nothing ASan intercepts, so the check is moot.
[ "$(uname)" = Linux ] && ASAN_OPTIONS="$ASAN_OPTIONS:verify_asan_link_order=0"
export UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1:log_path=$logs/ubsan"
# macOS: MallocStackLogging makes every forked child print a malloc warning
# under ASan's allocator (and breaks gh); it carries no signal here.
unset MallocStackLogging MallocStackLoggingNoCompact

# ASan slows every verb several times over; skip the wall-clock budgets.
export DCH_TEST_NO_BUDGET=1

rc=0
sh tests/run.sh || rc=1
if [ -n "$(ls "$logs")" ]; then
	cat "$logs"/*
	echo "sanitizer reports above" >&2
	rc=1
fi
exit $rc
