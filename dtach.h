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
#include <limits.h>
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
#include "bridge.h"

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

/* dch's version, from AC_INIT in configure.ac (the one version source). Lives
** here, not in dch.c, because the master stamps it into the `<sock>.ver`
** sidecar so a client can tell which binary a running session is serving. */
#define DCH_VERSION PACKAGE_VERSION

enum
{
	MSG_PUSH	= 0,
	MSG_ATTACH	= 1,	/* no payload; len = rows of scrollback the
				** replay sends above the screen (#101, 0 =
				** none; masters before it ignore len) */
	MSG_DETACH	= 2,	/* no payload; len 1: the client's X-ray view
				** is up, it stays attached (a restart carries
				** it) and gets no output (#311; masters before
				** it detach it) */
	MSG_WINCH	= 3,
	MSG_REDRAW	= 4,
	/* Control verbs (client->master). A connection that sends one of
	** these takes the CONTROL role and can never attach (see the role
	** latch in master.c) — responses are framed, attached output is raw,
	** and one socket must never carry both. */
	MSG_KEYS	= 5,	/* payload: NUL-separated key combos */
	MSG_READ	= 6,	/* payload: {u8 format; u8 source; u16 lines LE;
				** u8 flags (optional 5th byte); u32 gen LE
				** (bytes 5..8, only with DCH_READ_F_SINCE)} */
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
	MSG_RESPAWN	= 14,	/* payload: op byte, then for 'a' the argv as
				** NUL-terminated strings (whole payload <=
				** PKT_MAX). 'a' = when the child exits, start
				** this argv in its place (or now, if held);
				** 'h' = hold: keep the session childless after
				** the exit until an 'a' arrives; 'c' = cancel
				** an arm (ends a held session). MSG_ACK status
				** reply. One-shot: the next child exit eats
				** the arm. */
	MSG_CMD		= 15,	/* payload: optional u8 flags (DCH_CMD_F_*;
				** none = 0). Reply: MSG_READ_DATA chunks
				** of "<pid>\0<state>\0<exit code>\0<exit
				** signal>\0<argc>\0" + argc NUL-terminated
				** args + the raw exit tail (to the end), then
				** MSG_READ_END. pid 0 = no child (held); code
				** -1 / signal 0 = not reaped or not that. */
	MSG_READ_META	= 16,	/* payload: {u32 gen LE; u16 rows LE; u16 cols
				** LE} — the mirror's change generation and
				** grid size. ONLY sent when the request set
				** DCH_READ_F_META (dch --follow). */
	MSG_CMD_STAT	= 17,	/* payload: {u16 rows LE; u16 cols LE; u16
				** attached clients LE} — the pty winsize the
				** master holds. ONLY sent, ahead of the
				** MSG_CMD reply, when the request set
				** DCH_CMD_F_STAT (dch --ls-json). */
	/* X-ray shell ring (#119). Control verbs; the master protocol only,
	** never a ctl verb (S19). */
	MSG_RING_READ	= 18,	/* payload: {u64 seq LE; u32 max LE; u8 flags
				** (DCH_RING_F_*, optional)}. Reply:
				** MSG_RING_HDR, MSG_READ_DATA chunks of whole
				** records from seq (u16 len LE, u8 kind, u8
				** flags, u32 time LE, text; ring.h), then
				** MSG_READ_END. max is clamped to 64K+8 ..
				** DCH_RING_READ_MAX. */
	MSG_RING_HDR	= 19,	/* payload: DCH_RING_HDR_LEN bytes: {u8
				** status (DCH_RING_S_*); u64 head_seq; u64
				** tail_seq; u64 first (seq of the first
				** record sent); u32 count; u32 epoch (moves
				** on every clear); char session_id[17]}, LE */
	MSG_RING_WAKE	= 20,	/* no payload: the ring moved since this
				** subscriber's last read. Edge-triggered: one
				** until its next MSG_RING_READ. */
	MSG_RING_CLEAR	= 21,	/* no payload: ESC[H ESC[2J ESC[3J into the
				** mirror only (never the pty), and the ring
				** empties whatever the active screen.
				** MSG_ACK reply. With no ring, or while an
				** agent runs, ACKed and nothing cleared. */
	MSG_RESTART_CAPS = 22,	/* payload: u8 flags (DCH_RESTART_F_*): what
				** the requester's image takes over on the
				** MSG_RESTART that follows on this connection.
				** MSG_ACK reply (a master older than this
				** answers DCH_ST_UNSUP). With no CAPS, the
				** target is taken to be pre-1.43 (#121): no
				** ring handover, core limit restored. */
	MSG_PUSH_END	= 23,	/* no payload: the MSG_PUSHes before it on this
				** connection are all there is (dch --send,
				** #307). MSG_ACK reply: DCH_ST_OK, or
				** DCH_ST_BUSY when some of them were dropped
				** (the program is not reading and 4 MB wait).
				** A master before it answers DCH_ST_UNSUP. */
};
#define DCH_RESTART_F_RING 0x01	/* adopts the blob's ringfd and core lines */

/* MSG_RING_READ flags */
#define DCH_RING_F_SUB    0x01	/* subscribe this connection to wakes */
#define DCH_RING_F_HDR    0x02	/* the header only: no records (max is
				** ignored). A 1.43.0 master ignores the flag
				** and sends one reply of records as usual */
#define DCH_RING_F_SCREEN 0x04	/* with SUB: wake on a mirror change too, as
				** the follower reads the screen (#140). A
				** master before #140 ignores it */
#define DCH_RING_READ_MAX (1u << 20)	/* S10: one reply's record bytes */
#define DCH_RING_SUBS     4	/* S10: subscribers per master */
#define DCH_RING_STALE_MS 3000	/* a subscriber whose wake waits this long
				** loses its slot to a new one when all are
				** taken (none is free otherwise) */
#define DCH_RING_HDR_LEN  50
/* MSG_RING_HDR status bits */
#define DCH_RING_S_RANGE   0x01	/* seq was evicted (or never was): data
				** starts at the head; why:"range" */
#define DCH_RING_S_CHAPTERS 0x02	/* a prompt was evicted */
#define DCH_RING_S_PAUSED  0x04	/* an agent runs: capture paused (with
				** S_OFF: the agent alone) */
#define DCH_RING_S_OFF     0x08	/* no ring (xray_ring 0, lite, no mirror) */
#define DCH_RING_S_SUB     0x10	/* this connection is subscribed */
#define DCH_RING_S_SUBFULL 0x20	/* subscribe refused: DCH_RING_SUBS taken */
#define DCH_RING_S_LATCHED 0x40	/* the mirror latched off: capture ended */
#define DCH_RING_S_EVICTED 0x80	/* rows were evicted since the last clear
				** (why:"range"). A 1.43.0 master never sets
				** it: no bit is no evidence either way */

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
	DCH_ST_SAME	= 6,	/* DCH_READ_F_SINCE: the mirror generation still
				** equals the one asked about, nothing rendered */
	DCH_ST_ALT	= 7,	/* DCH_READ_F_PRIMARY: the alt screen is active
				** or capture is paused for an agent, nothing
				** rendered */
};

/* MSG_READ payload fields */
#define DCH_READ_PLAIN   0
#define DCH_READ_ANSI    1
#define DCH_READ_VISIBLE 0
#define DCH_READ_RECENT  1
/* flags byte (payload[4], absent in a 4-byte request = no flags) */
#define DCH_READ_F_CURSOR 0x01	/* also send MSG_READ_CURSOR */
#define DCH_READ_F_META   0x02	/* also send MSG_READ_META */
#define DCH_READ_F_SINCE  0x04	/* bytes 5..8 = gen: DCH_ST_SAME if unchanged */
#define DCH_READ_F_PRIMARY 0x08	/* X-ray (#140): the visible rows, soft wraps
				** joined, only while the primary screen is
				** active and capture not paused, else
				** DCH_ST_ALT and no data. Plain,
				** visible and alone; a master before #140
				** answers DCH_ST_ERR (the client reads the
				** ring alone) */
#define DCH_READ_F_ALL    (DCH_READ_F_CURSOR | DCH_READ_F_META | \
                           DCH_READ_F_SINCE | DCH_READ_F_PRIMARY)	/* every
				** flag we understand */
#define DCH_READ_CURSOR_LEN 6	/* MSG_READ_CURSOR payload size */
#define DCH_READ_META_LEN   8	/* MSG_READ_META payload size */
#define DCH_READ_REQ_MAX    9	/* longest MSG_READ payload (with F_SINCE) */

/* MSG_CMD flags byte (payload[0]; masters before it ignore the payload) */
#define DCH_CMD_F_STAT    0x01	/* also send MSG_CMD_STAT first */
#define DCH_CMD_STAT_LEN  6	/* MSG_CMD_STAT payload size */

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
** REDRAW = redraw method, ATTACH = scrollback rows. WINCH/REDRAW carry a
** struct winsize payload (sized from the type, not len); ATTACH/DETACH carry
** no payload.
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
/* the trace sink's fd once resolved, else -1: for a signal handler, which
** must write(2) a line itself, never take stdio's lock (QA 1.63.0 OOA-1) */
extern int dch_trace_fd;

void write_buf_or_fail(int fd, const void *buf, size_t count);
void write_packet_or_fail(int fd, const struct packet *pkt);
size_t packet_payload_len(const struct packet *pkt);

/* attach_main() normally never returns while a master is live: it exits on
** detach. It returns 1 when the connect failed, and ATTACH_SWITCH when the
** user asked for the session picker with the detach key: two presses, or one
** with detach_key = switch. */
#define ATTACH_SWITCH 2
/* ... and ATTACH_ENDED when the session ended (the master closed the socket)
** under attach_end_back, set by the host for an attach the picker made
** (#133): back to the picker, not the shell. Local attaches only. */
#define ATTACH_ENDED 3
extern int attach_end_back;

/* Session history (#042): each master keeps a JSON record of its session in
** dch_history_dir(); `--history` lists the dead ones, `--revive` restarts
** them. dch_agent_probe() finds the Claude Code agent under the child. */
struct dch_agent
{
	long pid;		/* claude's pid */
	char sid[64], name[256], cwd[PATH_MAX];	/* = hrec.acwd: no cut */
	char rec[1400];		/* its Claude session record's path */
	char cfg[1400];		/* its config dir, "" = our ~/.claude (#065) */
	char **launch;		/* malloc'd block, NULL-terminated; may be NULL */
	int n;			/* the live Claudes under the child (#314) */
};
int dch_history_dir(char *out, size_t outsz, const char *sockdir,
                    int create);
/* The shallowest Codex at or below child (X-ray 7.3/7.4); 0 none. */
long dch_codex_below(long child);
int dch_agent_probe(long child, struct dch_agent *a);
/* 1: the Claude at pid is in front of its terminal, through any wrapper
** pty up to the session child root (#282, #312); 0 not; -1 unreadable. */
int proc_fg(long root, long pid);
void dch_agent_json(FILE *f, const struct dch_agent *a);
int dch_claude_gone(const char *rec);
int path_lookup(const char *name, const char *path, const char *cwd,
                char *out, size_t outsz);

/* The session socket dir, once main() has computed it. */
const char *dch_sock_dir(void);
/* `dch serve` (#073): Prosper's remote frames 1.0, serve.c. */
int serve_main(int argc, char **argv);

int dch_config_path(char *out, size_t cap);
int dch_config_get(const char *key, char *out, size_t cap);
int dch_config_bool(const char *key);
int dch_env_on(const char *key);
int dch_allow_prompt(const char *screen, char **ident, char **title);
/* A connect to a master's socket (dch.c): the fd, or -1 with errno, EAGAIN
** when its backlog stayed full for 500 ms: a live master, stuck (#265). */
int control_connect_path(const char *path);

int attach_main(int noerror);
/* A remote attach (remote.c) hands attach_main an already connected
** socket instead of sockname's, and a hook it asks, when that socket
** closes, what to say: NULL for a session that ended, else the message to
** print once the terminal is restored, exiting with *code (remote.h). */
extern int attach_sock;
extern const char *(*attach_lost)(int *code);
/* And, asked by the X-ray overlay then, 1 when the host detached its
** client (dch -d there, #217): the view leaves as on a local dch -d. */
extern int (*attach_detached)(void);
/* The in-attach X-ray chord (#187), the detach key then x: the host's view
** of the attached session, run in a child attach_main forks; its exit code.
** A remote attach's too (#189). NULL: no chord, the bytes go on as before. */
extern int (*attach_xray)(void);

#include "remote.h"
void client_pidfile_unlink(void);
int master_main(char **argv, int waitattach, int dontfork);
int push_main(void);

#ifdef sun
#define BROKEN_MASTER
#endif
#endif
