# Process this file with autoconf to produce a configure script.
# dch is a fork of dtach (C)Copyright 2004-2016 Ned T. Crigler, GPL-2.0-or-later.
# Original attribution preserved in source headers, README, and COPYING.
AC_INIT(dch, 1.40.2, https://github.com/ventsislav-georgiev/dch/issues)
AC_PREREQ(2.60)
AC_CONFIG_SRCDIR(dch.c)
AC_CONFIG_HEADER(config.h)

# libghostty-vt terminal mirror (default on; --without-libghostty = dch-lite)
AC_ARG_WITH([libghostty],
	[AS_HELP_STRING([--without-libghostty],
		[build dch-lite: no terminal mirror, no --read or --wait --match])],
	[], [with_libghostty=yes])

VT_OBJS=vt_stub.o
VT_LIBS=
VT_CPPFLAGS=
if test "x$with_libghostty" != xno; then
	vt_platform=`uname -s | tr 'A-Z' 'a-z'`-`uname -m`
	vt_lib="$srcdir/vendor/libghostty-vt/lib/$vt_platform/libghostty-vt.a"
	if test -f "$vt_lib"; then
		AC_DEFINE([HAVE_LIBGHOSTTY_VT], [1],
			[Define if the libghostty-vt terminal mirror is built in])
		VT_OBJS=vt.o
		VT_LIBS=$vt_lib
		VT_CPPFLAGS="-I\$(srcdir)/vendor/libghostty-vt/include"
	else
		AC_MSG_ERROR([no vendored libghostty-vt.a for $vt_platform.
Run scripts/build-libghostty.sh (needs zig 0.17.x), or configure --without-libghostty for dch-lite.])
	fi
fi
AC_SUBST([VT_OBJS])
AC_SUBST([VT_LIBS])
AC_SUBST([VT_CPPFLAGS])

# Checks for programs.
AC_PROG_CC
AC_PROG_GCC_TRADITIONAL

if test "$GCC" = yes; then
	CFLAGS="$CFLAGS -W -Wall";
fi

# Hardened build flags, each probed so a toolchain that lacks one skips it.
# --disable-hardening is for sanitizer and fuzz builds (FORTIFY's checked
# memcpy hides accesses from ASan).
AC_ARG_ENABLE([hardening],
	[AS_HELP_STRING([--disable-hardening],
		[skip FORTIFY, stack protector, PIE and RELRO flags])],
	[], [enable_hardening=yes])

# DCH_TRY_FLAG(VAR, FLAG): append FLAG to VAR (CFLAGS or LDFLAGS) when a
# test program links with it under -Werror: clang only warns about a flag
# it ignores, and an ignored flag hardens nothing.
AC_DEFUN([DCH_TRY_FLAG], [
	AC_MSG_CHECKING([whether $CC accepts $2])
	dch_try_old=$$1
	$1="$$1 $2"
	dch_try_cflags=$CFLAGS
	CFLAGS="$CFLAGS -Werror"
	AC_LINK_IFELSE([AC_LANG_PROGRAM([], [])], [dch_try=yes], [dch_try=no])
	CFLAGS=$dch_try_cflags
	if test $dch_try = no; then
		$1=$dch_try_old
	fi
	AC_MSG_RESULT([$dch_try])
])

if test "x$enable_hardening" != xno; then
	# FORTIFY needs optimization; a toolchain that already sets it (Ubuntu
	# gcc defaults to 3) keeps its own level.
	AC_MSG_CHECKING([whether to define _FORTIFY_SOURCE=2])
	AC_COMPILE_IFELSE([AC_LANG_PROGRAM([[
#if !defined(__OPTIMIZE__) || defined(_FORTIFY_SOURCE)
#error not optimized, or already set
#endif
]], [])], [CFLAGS="$CFLAGS -D_FORTIFY_SOURCE=2"; AC_MSG_RESULT([yes])],
	[AC_MSG_RESULT([no])])
	DCH_TRY_FLAG([CFLAGS], [-fstack-protector-strong])
	DCH_TRY_FLAG([CFLAGS], [-fstack-clash-protection])
	DCH_TRY_FLAG([CFLAGS], [-fPIE])
	DCH_TRY_FLAG([LDFLAGS], [-pie])
	# ELF only: Mach-O's ld rejects -z, so the probe skips these on macOS.
	DCH_TRY_FLAG([LDFLAGS], [-Wl,-z,relro])
	DCH_TRY_FLAG([LDFLAGS], [-Wl,-z,now])
fi

# Checks for libraries.
AC_CHECK_LIB(util, openpty)
AC_CHECK_LIB(socket, socket)

# Checks for header files.
AC_CHECK_HEADERS(fcntl.h sys/select.h sys/socket.h sys/time.h)
AC_CHECK_HEADERS(sys/ioctl.h sys/resource.h pty.h termios.h util.h)
AC_CHECK_HEADERS(libutil.h stropts.h)
AC_HEADER_TIME

# Checks for typedefs, structures, and compiler characteristics.
AC_C_CONST
AC_TYPE_PID_T
AC_TYPE_SSIZE_T

# Checks for library functions.
AC_TYPE_SIGNAL
AC_CHECK_FUNCS(atexit dup2 memset)
AC_CHECK_FUNCS(select socket strerror)
AC_CHECK_FUNCS(openpty forkpty ptsname grantpt unlockpt)

AC_CONFIG_FILES(Makefile)
AC_OUTPUT
