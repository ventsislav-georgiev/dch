/*
** The Windows client (#077): what rframe.c and remote.c get from dtach.h
** on POSIX, for a mingw-w64 build of dch.exe that lists and attaches to
** the sessions of other hosts and hosts none itself. win/plat.c is the
** platform layer, win/main.c the verbs; scripts/build-windows.sh builds it.
*/
#ifndef DCH_WIN_H
#define DCH_WIN_H

#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00	/* Windows 10: VT consoles */
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <errno.h>
#include <fcntl.h>
#include <io.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>

#include "remote.h"

#ifndef DCH_VERSION
#error "DCH_VERSION: scripts/build-windows.sh passes configure.ac's"
#endif

/* dtach.h's: dch_remote_attach's code for the detach key's switch to the
** picker (#090) */
#define ATTACH_SWITCH 2

/* rf_run's status is the exit code, or -1 when it could not run */
#define WIFEXITED(s) ((s) >= 0)
#define WEXITSTATUS(s) (s)

struct winsize
{
	unsigned short ws_row, ws_col, ws_xpixel, ws_ypixel;
};

/* Winsock's spellings of what remote.c does with a socket (POSIX ones in
** remote.c). A SOCKET is kept in an int: Windows handle values fit 32
** bits, and INVALID_SOCKET comes out as -1. */
#define sock_close(s) closesocket((SOCKET)(s))
#define sock_read(s, b, n) recv((SOCKET)(s), (char *)(b), (int)(n), 0)
#define sock_err() WSAGetLastError()
#define sock_seterr(e) WSASetLastError(e)
#define SE_INTR WSAEINTR
#define SE_AGAIN WSAEWOULDBLOCK
#define SE_INPROGRESS WSAEWOULDBLOCK
#define SE_UNREACH WSAEHOSTUNREACH
typedef unsigned long nfds_t;
#define poll(p, n, t) WSAPoll((p), (n), (t))
const char *sock_str(int e);

/* The cache (remote.c) lives in %LOCALAPPDATA%\dch: the user's profile,
** whose ACL lets no other user in, so it is this user's own file there;
** dch_history_dir refuses a directory that is a reparse point. */
#define file_mine(st) 1
#define O_CLOEXEC _O_NOINHERIT
#define O_NOFOLLOW 0
#define rename_over(a, b) \
	(MoveFileExA((a), (b), MOVEFILE_REPLACE_EXISTING) ? 0 : -1)

/* dch.c's, here from win/plat.c: the same config format and switches. */
int dch_config_get(const char *key, char *out, size_t cap);
int dch_env_on(const char *key);
int dch_history_dir(char *out, size_t outsz, const char *sockdir,
                    int create);

/* The console (win/plat.c). con_raw: VT output, raw VT input, the modes
** restored at exit and on Ctrl-Break or a closed window; 0, -1 when stdin
** or stdout is not a console, -2 when it has no VT mode. con_keys: the
** keys typed so far as UTF-8, how many bytes (0 none), -1 when the console
** is gone. */
int con_raw(void);
void con_restore(void);
void con_size(struct winsize *ws);
HANDLE con_in(void);
int con_keys(unsigned char *buf, size_t cap);
int con_write(const void *p, size_t n);

#endif
