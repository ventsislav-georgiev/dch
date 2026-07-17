# Process this file with autoconf to produce a configure script.
# dch is a fork of dtach (C)Copyright 2004-2016 Ned T. Crigler, GPL-2.0-or-later.
# Original attribution preserved in source headers, README, and COPYING.
AC_INIT(dch, 0.14, https://github.com/ventsislav-georgiev/dch/issues)
AC_PREREQ(2.60)
AC_CONFIG_SRCDIR(dch.c)
AC_CONFIG_HEADER(config.h)

# libghostty-vt terminal mirror (default on; --without-libghostty = dch-lite)
AC_ARG_WITH([libghostty],
	[AS_HELP_STRING([--without-libghostty],
		[build dch-lite: no terminal mirror, no --read/--wait (~100KB binary)])],
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
Run scripts/build-libghostty.sh (needs zig 0.15.x), or configure --without-libghostty for dch-lite.])
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
