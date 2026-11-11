#!/bin/sh
# Build the libFuzzer targets under tests/fuzz/ (with ASan + UBSan) and run
# each one for SECONDS (default 60) over its seed corpus.
#
#   scripts/fuzz.sh [SECONDS] [TARGET...] [-- CONFIGURE-ARGS]
#   TARGET: json meta history config detect frames keys serve remote ring
#           xray xref
#           (default: all)
#
# Needs a clang with the libFuzzer runtime: CC=clang on Linux; on macOS
# Apple's clang has none, use Homebrew's (CC=$(brew --prefix llvm)/bin/clang).
# Builds the full variant (the Claude composer gate only exists there;
# -- --without-libghostty for lite) in a scratch copy, so the tree is left
# alone and its seeds are never rewritten (libFuzzer adds to the corpus dir
# it runs on). A crashing input lands in $FUZZ_ARTIFACTS (default ./fuzz-artifacts).
set -eu
src=$(cd "$(dirname "$0")/.." && pwd)
secs=${1:-60}
[ $# -gt 0 ] && shift
targets=
while [ $# -gt 0 ] && [ "$1" != -- ]; do targets="$targets $1"; shift; done
[ $# -gt 0 ] && shift
targets=${targets:-json meta history config detect frames keys serve remote ring xray xref}
CC=${CC:-clang}
art=${FUZZ_ARTIFACTS:-$PWD/fuzz-artifacts}

b=$(mktemp -d)
trap 'rm -rf "$b"' EXIT
mkdir -p "$b/tests/fuzz"
cp "$src"/*.c "$src"/*.h "$src"/configure "$src"/Makefile.in \
   "$src"/config.h.in "$b"/
cp "$src"/tests/fuzz/*.c "$b/tests/fuzz/"
[ -d "$src/vendor" ] && ln -s "$src/vendor" "$b/vendor"
cd "$b"

# -fno-sanitize-recover: a UBSan finding must stop the fuzzer like a crash.
# -ftrivial-auto-var-init=pattern: an unset stack read fails, not reads a
# leftover 0 (#147); every clang with libFuzzer here has it (clang >= 8).
SAN="-fsanitize=address,undefined -fno-sanitize-recover=undefined -ftrivial-auto-var-init=pattern"
CF="-g -O1 -fno-omit-frame-pointer $SAN -fsanitize=fuzzer-no-link"
./configure --disable-hardening CC="$CC" CFLAGS="$CF" "$@" >/dev/null
vt_objs=$(sed -n 's/^VT_OBJS = //p' Makefile)
vt_libs=$(sed -n 's/^VT_LIBS = //p' Makefile)
CF="$CF -I. -Ivendor/libghostty-vt/include"
make attach.o master.o serve.o rframe.o remote.o bridge.o json.o picker.o ring.o xray.o cargv.o $vt_objs >/dev/null
# dch.c has main(); the targets bring libFuzzer's.
$CC $CF -Dmain=dch_main -c dch.c -o dch.o

mkdir -p "$art"
# fuzz_ring makes a terminal per input: on Linux its mmap64 wrapper keeps
# libghostty's mappings from drifting through ASan shadow (fuzz_ring.c).
ringld=
[ "$(uname -s)" = Linux ] && ringld=-Wl,--wrap=mmap64
rc=0
for t in $targets; do
	ld=
	case $t in
	json)  objs="attach.o master.o dch.o serve.o remote.o" ;;  # the API
	frames) objs="attach.o dch.o serve.o remote.o" ;;  # includes master.c
	keys)  objs="master.o dch.o serve.o remote.o" ;;   # includes attach.c
	serve) objs="attach.o master.o dch.o remote.o" ;;  # includes serve.c
	remote) objs="attach.o master.o dch.o serve.o" ;;  # includes remote.c
	ring)  objs="attach.o master.o dch.o serve.o remote.o"  # the API
	       ld=$ringld ;;
	xray|xref) objs="attach.o master.o dch.o serve.o remote.o" ;;  # the API
	*)     objs="attach.o master.o serve.o remote.o" ;; # includes dch.c
	esac
	$CC $CF -Dmain=dch_main -fsanitize=fuzzer -o "fuzz_$t" \
		"tests/fuzz/fuzz_$t.c" $objs rframe.o bridge.o json.o picker.o ring.o xray.o cargv.o \
		$vt_objs $vt_libs $ld
	mkdir -p "corpus/$t"
	cp "$src/tests/fuzz/corpus/$t"/* "corpus/$t/" 2>/dev/null || :
	echo "fuzz_$t: ${secs}s"
	"./fuzz_$t" -max_total_time="$secs" -artifact_prefix="$art/$t-" \
		-print_final_stats=1 "corpus/$t" 2>"$t.log" || {
		tail -n 40 "$t.log"
		echo "fuzz_$t FAILED: input in $art/" >&2
		rc=1
	}
	grep -E '^stat::(number_of_executed_units|new_units_added)' \
		"$t.log" || :
done
rmdir "$art" 2>/dev/null || :
exit $rc
