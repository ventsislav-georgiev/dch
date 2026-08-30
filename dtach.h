/*
    dtach - A simple program that emulates the detach feature of screen.
    Copyright (C) 2001, 2004-2016 Ned T. Crigler

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/
#ifndef dtach_h
#define dtach_h

#if defined(__has_attribute)
#if __has_attribute(unused)
#define ATTRIBUTE_UNUSED __attribute__((__unused__))
#else
#define ATTRIBUTE_UNUSED
#endif
#elif defined(__GNUC__) || defined(__clang__)
#define ATTRIBUTE_UNUSED __attribute__((__unused__))
#else
#define ATTRIBUTE_UNUSED
#endif

#include <config.h>

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if TIME_WITH_SYS_TIME
#include <sys/time.h>
#include <time.h>
#else
#if HAVE_SYS_TIME_H
#include <sys/time.h>
#else
#include <time.h>
#endif
#endif

#ifdef HAVE_PTY_H
#include <pty.h>
#endif

#ifdef HAVE_UTIL_H
#include <util.h>
#endif

#ifdef HAVE_LIBUTIL_H
#include <libutil.h>
#endif

#ifdef HAVE_STROPTS_H
#include <stropts.h>
#endif

#ifdef HAVE_UNISTD_H
#include <unistd.h>
#endif

#ifdef HAVE_SYS_IOCTL_H
#include <sys/ioctl.h>
#endif

#ifdef HAVE_SYS_RESOURCE_H
#include <sys/resource.h>
#endif

#include <termios.h>
#include <sys/select.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>

#ifndef S_ISREG
#define S_ISREG(m) (((m) & S_IFMT) == S_IFREG)
#endif

#ifndef S_ISSOCK
#define S_ISSOCK(m) (((m) & S_IFMT) == S_IFSOCK)
#endif

extern char *progname, *sockname;
extern int detach_char, no_suspend, redraw_method;
extern struct termios orig_term;
extern int dont_have_tty;

/* argv[0] as resolved at startup, and the master's own argv. The master needs
** both to re-exec itself in place on MSG_RESTART. */
extern char *dch_exe;
extern char **dch_argv;

/* dch's own version, independent of the dtach base (PACKAGE_VERSION). Lives
** here, not in dch.c, because the master stamps it into the `<sock>.ver`
** sidecar so a client can tell which binary a running session is serving. */
#define DCH_VERSION "1.12.0"

enum
{
	MSG_PUSH	= 0,
	MSG_ATTACH	= 1,
	MSG_DETACH	= 2,
	MSG_WINCH	= 3,
	MSG_REDRAW	= 4,
	/* Control verbs (client->master). A connection that sends one of
	** these takes the CONTROL role and can never attach (see the role
	** latch in master.c) — responses are framed, attached output is raw,
	** and one socket must never carry both. */
	MSG_KEYS	= 5,	/* payload: NUL-separated key combos */
	MSG_READ	= 6,	/* payload: {u8 format; u8 source; u16 lines LE;
				** u8 flags (optional 5th byte)} */
	MSG_WAIT	= 7,	/* payload: literal substring (<= 512 bytes) */
	/* Responses (master->control-client), same [type][len:2 LE] framing. */
	MSG_READ_DATA	= 8,	/* len bytes of screen snapshot */
	MSG_READ_END	= 9,	/* len = status, no payload */
	MSG_WAIT_HIT	= 10,	/* payload: the matching line */
	MSG_ACK		= 11,	/* len = status; reply to MSG_KEYS */
	MSG_READ_CURSOR	= 12,	/* payload: {u16 row LE; u16 col LE; u8 visible;
				** u8 wrap} — 0-based, active-area relative.
				** ONLY sent when the request set
				** DCH_READ_F_CURSOR, so a client built before
				** this frame existed never has to know it. */
	MSG_RESTART	= 13,	/* payload: absolute path of the binary to
				** re-exec (the client's own, so the session
				** lands on the dch that asked, not on the one
				** it was started from — a versioned Homebrew
				** or Nix prefix makes those different paths).
				** Empty payload means "whatever you started
				** from". The ACK comes from the
				** NEW master over this same connection (the fd
				** is carried across the exec), so an ACK is
				** proof the new image is serving; a rolled-back
				** exec answers DCH_ST_ERR from the old one. */
};

/* MSG_READ_END / MSG_ACK statuses */
enum
{
	DCH_ST_OK	= 0,
	DCH_ST_ERR	= 1,	/* internal error (mirror latched off) */
	DCH_ST_NOVT	= 2,	/* master has no terminal mirror (lite/DCH_NO_VT) */
	DCH_ST_TRUNC	= 3,	/* response truncated (head dropped, tail kept) */
	DCH_ST_BUSY	= 4,	/* out-queue too full; retry */
	DCH_ST_UNSUP	= 5,	/* master does not know this verb (it is older
				** than the client). Masters built before this
				** status existed answer silence instead — the
				** client's inactivity deadline covers both. */
};

/* MSG_READ payload fields */
#define DCH_READ_PLAIN   0
#define DCH_READ_ANSI    1
#define DCH_READ_VISIBLE 0
#define DCH_READ_RECENT  1
/* flags byte (payload[4], absent in a 4-byte request = no flags) */
#define DCH_READ_F_CURSOR 0x01	/* also send MSG_READ_CURSOR */
#define DCH_READ_F_ALL    (DCH_READ_F_CURSOR)	/* every flag we understand */
#define DCH_READ_CURSOR_LEN 6	/* MSG_READ_CURSOR payload size */

#define DCH_WAIT_MAX 512	/* MSG_WAIT pattern cap */

enum
{
	REDRAW_UNSPEC	= 0,
	REDRAW_NONE	= 1,
	REDRAW_CTRL_L	= 2,
	REDRAW_WINCH	= 3,
};

/*
** The client to master protocol.
**
** Wire frame is variable length: [type:1][len:2 host-order][payload:len].
** Keystrokes stay tiny on the wire (good for slow links); paste/bulk input
** fills up to PKT_MAX per frame, so a big paste costs ~len/PKT_MAX frames
** instead of one frame per 8 bytes (the old fixed-frame cap).
**
** `len` is overloaded by type, exactly as before: PUSH = payload byte count,
** REDRAW = redraw method. WINCH/REDRAW carry a struct winsize payload (sized
** from the type, not len); ATTACH/DETACH carry no payload.
**
** NOTE: this framing is NOT wire-compatible with the old fixed-size packet.
** A new client cannot attach to a master built before this change — kill old
** sessions and start fresh.
*/
#define PKT_HDR 3
/* ponytail: 4 KB paste chunk — fewer frames per paste, tiny typing frames.
** Bump if huge pastes over fast links ever dominate; costs PKT_HDR+PKT_MAX
** per-client reassembly buffer in the master. */
#define PKT_MAX 4096

struct packet
{
	unsigned char type;
	unsigned short len;
	union
	{
		unsigned char buf[PKT_MAX];
		struct winsize ws;
	} u;
};

/*
** Stream buffer size for master->client redraw. Bumped from upstream 4 KB
** so reattach replays more recent terminal output (vim/fzf full redraw is
** unaffected; pagers like `less`/`cat` benefit). 16 KB is a sane sweet spot:
** big enough for two 200x80 screens of dense ANSI, small enough to never
** matter for memory.
*/
#define BUFSIZE 16384

/* This hopefully moves to the bottom of the screen */
#define EOS "\033[999H"

/*
** Opt-in debug tracing. No-op unless the DCH_DEBUG env var is set:
**   DCH_DEBUG=<path>  append trace lines to <path>
**   DCH_DEBUG=1       append to /tmp/dch.<uid>.trace
** The env is inherited across the master re-exec, so client and master both
** trace to the same sink. The master daemonizes with stderr -> /dev/null, so a
** file sink is the only thing that survives there. Cheap when disabled (one
** cached flag check); see dch_trace() in dch.c.
*/
void dch_trace(const char *fmt, ...)
#if defined(__GNUC__) || defined(__clang__)
	__attribute__((format(printf, 1, 2)))
#endif
	;

void write_buf_or_fail(int fd, const void *buf, size_t count);
void write_packet_or_fail(int fd, const struct packet *pkt);
size_t packet_payload_len(const struct packet *pkt);

/* attach_main() normally never returns while a master is live: it exits on
** detach. It returns 1 when the connect failed, and ATTACH_SWITCH when the
** user double-tapped the detach key to ask for the session picker. */
#define ATTACH_SWITCH 2

int attach_main(int noerror);
int master_main(char **argv, int waitattach, int dontfork);
int push_main(void);

#ifdef sun
#define BROKEN_MASTER
#endif
#endif
