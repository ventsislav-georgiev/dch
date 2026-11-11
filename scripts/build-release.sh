#!/bin/sh
# Build one prebuilt release binary for this host and package it (#134).
#
#   scripts/build-release.sh full|lite
#
# Runs configure and make, smoke-tests the binary, and writes to dist/:
#   dch[-lite]-VER-OS-ARCH.tar.gz         dch, dch.1, COPYING, README.md
#   dch[-lite]-VER.TAG.bottle.tar.gz      the Homebrew bottle (TAG below)
# and a .sha256 beside each. The release-assets workflow runs this on each
# native runner; on Linux it runs inside an old-glibc container.
set -eu
cd "$(dirname "$0")/.."

variant=${1:?usage: scripts/build-release.sh full|lite}
ver=$(sed -n 's/^AC_INIT(dch, *\([^,]*\),.*/\1/p' configure.ac)
os=$(uname -s | tr '[:upper:]' '[:lower:]')
arch=$(uname -m)

# Homebrew bottle tag per target. macOS builds target 14.0 (Sonoma), so
# their bottles pour on 14 and later; brew builds older macOS from source.
case "$os-$arch" in
darwin-arm64)  tag=arm64_sonoma ;;
darwin-x86_64) tag=sonoma ;;
linux-x86_64)  tag=x86_64_linux ;;
linux-aarch64) tag=arm64_linux ;;
*) echo "error: no release target for $os-$arch" >&2; exit 1 ;;
esac

case "$variant" in
full) formula=dch;      cfg= ;;
lite) formula=dch-lite; cfg=--without-libghostty ;;
*) echo "error: variant must be full or lite, got $variant" >&2; exit 1 ;;
esac

# Downstream pins check minos 14.0; the SDK default is the build host's OS.
# COPYFILE_DISABLE: no AppleDouble files in the tarballs.
[ "$os" = darwin ] && export MACOSX_DEPLOYMENT_TARGET=14.0 COPYFILE_DISABLE=1

[ -f Makefile ] && make distclean >/dev/null
# shellcheck disable=SC2086 # cfg is empty or one flag
./configure $cfg
make

./dch --version
./dch --version | grep -qF "dch $ver" || {
	echo "error: dch --version does not report $ver" >&2; exit 1; }
[ "$(./dch --build)" = "$variant" ] || {
	echo "error: dch --build is not $variant" >&2; exit 1; }
if [ "$variant" = full ]; then
	./dch -h | grep -q 'detachable terminal session' || {
		echo "error: dch -h did not print the usage" >&2; exit 1; }
fi

if [ "$os" = darwin ]; then
	minos=$(vtool -show-build dch | awk '$1 == "minos" { print $2 }')
	[ "$minos" = 14.0 ] || {
		echo "error: minos is '$minos', want 14.0" >&2; exit 1; }
	# The linker ad-hoc signs arm64 output; arm64 macOS kills unsigned code.
	[ "$arch" = arm64 ] && codesign -v dch
	echo "minos $minos"
else
	# The minimum glibc this binary needs: the newest symbol version it binds.
	objdump -T dch | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1
fi

sha() {
	if command -v sha256sum >/dev/null; then
		(cd dist && sha256sum "$1" > "$1.sha256")
	else
		(cd dist && shasum -a 256 "$1" > "$1.sha256")
	fi
	cat "dist/$1.sha256"
}

mkdir -p dist
name=$formula-$ver-$os-$arch
rm -rf "dist/$name" "dist/bottle-$formula"
mkdir "dist/$name"
cp dch dch.1 COPYING README.md "dist/$name/"
# The full binary embeds libghostty-vt; MIT asks for its notice in copies.
if [ "$variant" = full ]; then
	cp vendor/libghostty-vt/LICENSE "dist/$name/LICENSE.libghostty-vt"
fi
tar --no-xattrs -czf "dist/$name.tar.gz" -C dist "$name"
sha "$name.tar.gz"

# Bottle layout as brew pours it into the Cellar: NAME/VERSION/... . brew
# fetches ROOT_URL/NAME-VERSION.TAG.bottle.tar.gz (one dash) for any
# root_url that is not ghcr.io. No INSTALL_RECEIPT.json: brew pours with an
# empty receipt and writes its own.
keg=dist/bottle-$formula/$formula/$ver
mkdir -p "$keg/bin" "$keg/share/man/man1"
cp dch "$keg/bin/"
cp dch.1 "$keg/share/man/man1/"
cp COPYING README.md "$keg/"
[ "$variant" = full ] && cp vendor/libghostty-vt/LICENSE "$keg/LICENSE.libghostty-vt"
bottle=$formula-$ver.$tag.bottle.tar.gz
tar --no-xattrs -czf "dist/$bottle" -C "dist/bottle-$formula" "$formula"
sha "$bottle"
rm -rf "dist/$name" "dist/bottle-$formula"
