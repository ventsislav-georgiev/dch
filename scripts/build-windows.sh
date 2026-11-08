#!/bin/sh
# Build dch.exe, the Windows client (#077: lists and attaches to the
# sessions of other hosts; hosts none), with a mingw-w64 C compiler.
#
#   scripts/build-windows.sh [-o OUT] [EXTRA-CFLAGS...]
#
# CC (default x86_64-w64-mingw32-gcc; gcc in an MSYS2 MINGW64/UCRT64
# shell) must target Windows. OUT defaults to ./dch.exe. Only the client's
# sources: no configure, no libghostty, no pty.
set -eu
cd "$(dirname "$0")/.."
out=dch.exe
if [ "${1:-}" = -o ]; then out=$2; shift 2; fi
CC=${CC:-x86_64-w64-mingw32-gcc}
ver=$(sed -n 's/^AC_INIT(dch, *\([^,]*\),.*/\1/p' configure.ac)
# shellcheck disable=SC2086 # CC may carry its own flags
$CC -std=gnu11 -O2 -W -Wall -D__USE_MINGW_ANSI_STDIO=1 \
	-DDCH_VERSION="\"$ver\"" -I. "$@" -o "$out" \
	win/main.c win/plat.c picker.c remote.c rframe.c json.c \
	-lws2_32 -lshell32 -static
