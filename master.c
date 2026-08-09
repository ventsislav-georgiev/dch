/*
    dtach - A simple program that emulates the detach feature of screen.
    Copyright (C) 2004-2016 Ned T. Crigler

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
#include "dtach.h"
#include "vt.h"
#include <poll.h>

/* The pty struct - The pty information is stored here. */
struct pty
{
	/* File descriptor of the pty */
	int fd;
#ifdef BROKEN_MASTER
	/* File descriptor of the slave side of the pty. For broken systems. */
	int slave;
#endif
	/* Process id of the child. */
	pid_t pid;
	/* The terminal parameters of the pty. Old and new for comparision
	** purposes. */
	struct termios term;
	/* The current window size of the pty. */
	struct winsize ws;
};

/* A connected client */
struct client
{
	/* The next client in the linked list. */
	struct client *next;
	/* The previous client in the linked list. */
	struct client **pprev;
	/* File descriptor of the client. */
	int fd;
	/* Whether or not the client is attached. */
	int attached;

	/* Connection role latch. A control connection (MSG_KEYS/READ/WAIT)
	** receives framed responses on the same socket attached clients
	** receive raw pty bytes on — one connection must never be both, or
	** a response frame would interleave with terminal output. First
	** role-defining message wins; a frame from the other class drops
	** the client. */
#define ROLE_NONE     0
#define ROLE_ATTACHED 1
#define ROLE_CONTROL  2
	int role;

	/* Pending --wait pattern (empty = no waiter). NUL-terminated. */
	char wait_pat[DCH_WAIT_MAX + 1];

	/* Input reassembly (client->master). The socket is nonblocking and
	** frames are variable length, so a read may split one frame or carry
	** several; buffer raw bytes and parse complete frames out. */
	unsigned char inbuf[PKT_HDR + PKT_MAX];
	size_t inlen;

	/* Set by MSG_ATTACH, consumed by the MSG_REDRAW/MSG_WINCH that follows
	** it: the screen repaint waits until the mirror knows this client's
	** window size. See the MSG_ATTACH branch in handle_packet(). */
	int want_replay;

	/* Output queue (master->client). The master never blocks on a slow
	** client: pty output is queued here and flushed when the socket is
	** writable. A client that backs up past OUT_CAP is dropped instead of
	** stalling everyone (head-of-line). */
	unsigned char *out;
	size_t outlen, outcap, outoff;
};

/* ponytail: 4 MB per-client output backlog before we give up on a slow
** client and drop it. Bump if a legitimately bursty-but-slow link needs
** more slack; the cost is up to OUT_CAP heap per stalled client. */
#define OUT_CAP (4u * 1024u * 1024u)

/* The list of connected clients. */
static struct client *clients;
/* The pseudo-terminal created for the child process. */
static struct pty the_pty;
/* The listening socket, at file scope so do_restart() can hand it to the next
** image (it is the one descriptor we mark close-on-exec). -1 until bound. */
static int listen_fd = -1;

/* While set, the select loop leaves the pty out of readfds (the session
** was started with -w/--spawn and nothing has attached yet). File-scope so
** handle_packet can open the gate when a control client shows up; see
** drain_pty() for why opening it late needs an explicit drain. */
static int pty_gated;
/* Number of clients with a pending wait_pat; skips the post-batch
** snapshot entirely in the common no-waiter case. */
static int n_waiters;
/* Set once the mirror hits a runtime error and is freed; distinguishes
** "never had a mirror" (DCH_ST_NOVT) from "mirror died" (DCH_ST_ERR). */
static int vt_latched_off;
/* DCH_NO_REPLAY=1: don't repaint an attaching client from the mirror. Escape
** hatch for a program whose own WINCH repaint is enough and that dislikes
** seeing its screen handed back to it. Resolved once at master start. */
static int no_replay;

/* Enhanced-keyboard mode state. TUIs (Claude Code, nvim, ...) turn on keyboard
** protocols the terminal must stay in for their keys to work:
**   ESC[>...u  kitty keyboard protocol (push; ESC[<...u pops)
**   ESC[>...m  xterm modifyOtherKeys / XTMODKEYS
** A detaching client resets the terminal to a sane default (otherwise the bare
** shell it returns to emits CSI-u / `;N~` garbage for shifted keys), so we
** remember the last sequence of each kind the child emitted and replay it to
** every client that attaches afterwards — the child only sets these once, at
** startup, and this fork keeps no scrollback to re-send them. Bounded buffers:
** real sequences are a handful of bytes. */
static unsigned char kbd_u[32];   /* last ESC[>...u (kitty), empty after a pop */
static size_t kbd_u_len;
static unsigned char kbd_m[32];   /* last ESC[>...m (modifyOtherKeys) */
static size_t kbd_m_len;

/* DEC private modes (CSI ? Pm h/l) the child may enable that leave the outer
** terminal emitting stdin garbage if not torn down on detach, and that the
** child only sets once at startup. We remember each mode's current on/off so a
** freshly attaching client can be re-armed. Mouse tracking + ext-coords, focus
** events (1004), and bracketed paste (2004) — the same input-reporting modes
** restore_term resets and Claude Code's App.tsx toggles. Alt-screen (1049) is
** intentionally excluded: the attach client manages it itself. */
static const int dec_modes[] = {
	1000, 1002, 1003, 1004, 1006, 1015, 2004,
};
#define N_DEC_MODES ((int)(sizeof(dec_modes) / sizeof(dec_modes[0])))
static unsigned char dec_on[N_DEC_MODES];

#ifndef HAVE_FORKPTY
pid_t forkpty(int *amaster, char *name, struct termios *termp,
	      struct winsize *winp);
#endif

/* Build "<sockname><suffix>". Returns 0 on success, -1 on truncation. */
static int
sidecar_path(const char *suffix, char *buf, size_t bufsz)
{
	int n = snprintf(buf, bufsz, "%s%s", sockname, suffix);

	return (n < 0 || (size_t)n >= bufsz) ? -1 : 0;
}

/* Is DCH_RESUME pointing at the blob WE would have written? A stray value
** inherited from some parent shell is not a failed resume, it is not a resume
** at all — and a failed one deliberately refuses to start the session. */
static int
resume_is_ours(const char *path)
{
	char mine[1100];

	return path && *path &&
	       sidecar_path(".resume", mine, sizeof mine) == 0 &&
	       strcmp(path, mine) == 0;
}

/* Unlink the socket and every sidecar. NOT called across a restart: the
** re-exec keeps the socket (and the .resume blob the new image reads). */
static void
unlink_socket(void)
{
	static const char *suffix[] = { ".act", ".state", ".ver", ".resume" };
	char side[1100];
	size_t i;

	unlink(sockname);
	for (i = 0; i < sizeof suffix / sizeof suffix[0]; i++)
		if (sidecar_path(suffix[i], side, sizeof side) == 0)
			unlink(side);
}

/* Stamp `<sockname>.ver` with the version of the binary now serving this
** session. Without it a client can only infer a master's age from how it
** fails to answer; with it, --ls-json can just say. Rewritten (not appended)
** on every start AND every restart, so it always names the live image. */
static void
write_version_sidecar(void)
{
	char path[1100];
	int fd;

	if (sidecar_path(".ver", path, sizeof path) < 0)
		return;
	unlink(path);
	fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
	if (fd < 0)
		return;			/* best-effort: absence just means "unknown" */
	/* Plain write(), never write_buf_or_fail(): that exits the process on
	** error, and a full disk must not take the session down over a hint. */
	{
		ssize_t w = write(fd, DCH_VERSION "\n",
		                  sizeof(DCH_VERSION "\n") - 1);
		(void)w;
	}
	close(fd);
}

/* Stamp the per-session activity sidecar `<sockname>.act` with the current time
** so a watcher (Prosper's keep-awake) can tell a detached session is still doing
** work and must not sleep. Throttled to once a second — pty output is bursty and
** we only need second-granularity freshness. The file's mtime IS the signal; the
** body stays empty. Best-effort: a failed stamp just means one missed second. */
static void
touch_activity(void)
{
	static time_t last;
	time_t now = time(NULL);
	char path[1100];
	int n, fd;

	if (now == last)
		return;			/* ponytail: 1s throttle, plenty for a sleep guard */
	last = now;

	n = snprintf(path, sizeof path, "%s.act", sockname);
	if (n < 0 || (size_t)n >= sizeof path)
		return;
	fd = open(path, O_WRONLY | O_CREAT, 0600);
	if (fd < 0)
		return;
	futimens(fd, NULL);		/* NULL => set atime+mtime to now */
	close(fd);
}

/* Signal */
static RETSIGTYPE
die(int sig)
{
	/* Well, the child died. */
	if (sig == SIGCHLD)
	{
#ifdef BROKEN_MASTER
		/* Damn you Solaris! */
		close(the_pty.fd);
#endif
		return;
	}
	/* Trace is opt-in and the sink fd is resolved at master startup, so this
	** only does fprintf() here, not fopen() — good enough for a debug build to
	** catch "who killed the master" (the bug this whole trace facility exists
	** to diagnose). Not strictly async-signal-safe; debug-only. */
	dch_trace("master die sig=%d", sig);
	exit(1);
}

/* Sets a file descriptor to non-blocking mode. */
static int
setnonblocking(int fd)
{
	int flags;

#if defined(O_NONBLOCK)
	flags = fcntl(fd, F_GETFL);
	if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
		return -1;
	return 0;
#elif defined(FIONBIO)
	flags = 1;
	if (ioctl(fd, FIONBIO, &flags) < 0)
		return -1;
	return 0;
#else
#warning Do not know how to set non-blocking mode.
	return 0;
#endif
}

/* Initialize the pty structure. */
static int
init_pty(char **argv, int statusfd)
{
	/* Use the original terminal's settings. Start at a sane 80x24
	** instead of 0x0 so headless-spawned programs (and the terminal
	** mirror) see a real grid; the attacher's WINCH corrects it. */
	the_pty.term = orig_term;
	memset(&the_pty.ws, 0, sizeof(struct winsize));
	the_pty.ws.ws_row = 24;
	the_pty.ws.ws_col = 80;

	/* Create the pty process */
	if (!dont_have_tty)
		the_pty.pid = forkpty(&the_pty.fd, NULL, &the_pty.term,
		    &the_pty.ws);
	else
		the_pty.pid = forkpty(&the_pty.fd, NULL, NULL, &the_pty.ws);
	if (the_pty.pid < 0)
		return -1;
	dch_trace("init_pty pid=%d fd=%d notty=%d ispeed=%lu ospeed=%lu "
		"iflag=%lx oflag=%lx cflag=%lx lflag=%lx",
		(int)the_pty.pid, the_pty.fd, dont_have_tty,
		(unsigned long)cfgetispeed(&the_pty.term),
		(unsigned long)cfgetospeed(&the_pty.term),
		(unsigned long)the_pty.term.c_iflag,
		(unsigned long)the_pty.term.c_oflag,
		(unsigned long)the_pty.term.c_cflag,
		(unsigned long)the_pty.term.c_lflag);
	if (the_pty.pid == 0)
	{
		/* Child.. Execute the program. */
		execvp(*argv, argv);

		/* Report the error to statusfd if we can, or stdout if we
		** can't. */
		if (statusfd != -1)
			dup2(statusfd, 1);
		else
			printf(EOS "\r\n");

		printf("%s: could not execute %s: %s\r\n", progname,
		       *argv, strerror(errno));
		fflush(stdout);
		_exit(1);
	}
	/* Parent.. Finish up and return */
#ifdef BROKEN_MASTER
	{
		char *buf;

		buf = ptsname(the_pty.fd);
		the_pty.slave = open(buf, O_RDWR|O_NOCTTY);
	}
#endif
	return 0;
}

/* Send a signal to the slave side of a pseudo-terminal. */
static void
killpty(struct pty *pty, int sig)
{
	pid_t pgrp = -1;

#ifdef TIOCSIGNAL
	if (ioctl(pty->fd, TIOCSIGNAL, sig) >= 0)
		return;
#endif
#ifdef TIOCSIG
	if (ioctl(pty->fd, TIOCSIG, sig) >= 0)
		return;
#endif
#ifdef TIOCGPGRP
#ifdef BROKEN_MASTER
	if (ioctl(pty->slave, TIOCGPGRP, &pgrp) >= 0 && pgrp != -1 &&
	    kill(-pgrp, sig) >= 0)
		return;
#endif
	if (ioctl(pty->fd, TIOCGPGRP, &pgrp) >= 0 && pgrp != -1 &&
	    kill(-pgrp, sig) >= 0)
		return;
#endif

	/* Fallback using the child's pid. */
	kill(-pty->pid, sig);
}

/* Creates a new unix domain socket. */
static int
create_socket(char *name)
{
	int s;
	struct sockaddr_un sockun;
	mode_t omask;

	if (strlen(name) > sizeof(sockun.sun_path) - 1)
	{
		errno = ENAMETOOLONG;
		return -1;
	}

	omask = umask(077);
	s = socket(PF_UNIX, SOCK_STREAM, 0);
	if (s < 0)
	{
		umask(omask); /* umask always succeeds, errno is untouched. */
		return -1;
	}
	sockun.sun_family = AF_UNIX;
	strcpy(sockun.sun_path, name);
	if (bind(s, (struct sockaddr *)&sockun, sizeof(sockun)) < 0)
	{
		umask(omask); /* umask always succeeds, errno is untouched. */
		close(s);
		return -1;
	}
	umask(omask); /* umask always succeeds, errno is untouched. */
	if (listen(s, 128) < 0)
	{
		close(s);
		return -1;
	}
	if (setnonblocking(s) < 0)
	{
		close(s);
		return -1;
	}
	/* chmod it to prevent any surprises */
	if (chmod(name, 0600) < 0)
	{
		close(s);
		return -1;
	}
	return s;
}

/* Update the modes on the socket. */
static void
update_socket_modes(int exec)
{
	struct stat st;
	mode_t newmode;

	if (stat(sockname, &st) < 0)
		return;

	if (exec)
		newmode = st.st_mode | S_IXUSR;
	else
		newmode = st.st_mode & ~S_IXUSR;

	if (st.st_mode != newmode)
		chmod(sockname, newmode);
}

/* Scan a chunk of child output for enhanced-keyboard mode sequences and update
** the remembered enable strings:
**   ESC[>...u  kitty push   -> remember verbatim in kbd_u
**   ESC[<...u  kitty pop     -> clear kbd_u
**   ESC[>...m  modifyOtherKeys-> remember verbatim in kbd_m
** Only digits/';'/':' are allowed between the marker and the terminator, so
** this never matches ordinary SGR (ESC[...m without the '>'). Sequences split
** across two reads are not stitched — they are emitted atomically in practice. */
static void
track_keymode(const unsigned char *buf, size_t len)
{
	size_t i;

	for (i = 0; i + 2 < len; i++)
	{
		size_t j;
		int is_gt;

		if (buf[i] != '\033' || buf[i + 1] != '[')
			continue;
		if (buf[i + 2] != '>' && buf[i + 2] != '<')
			continue;
		is_gt = (buf[i + 2] == '>');

		for (j = i + 3; j < len; j++)
		{
			unsigned char b = buf[j];
			size_t n = j - i + 1;

			if (b == 'u')
			{
				if (!is_gt)            /* ESC[<...u : kitty pop */
					kbd_u_len = 0;
				else if (n <= sizeof(kbd_u))
				{
					memcpy(kbd_u, buf + i, n);
					kbd_u_len = n;
				}
				break;
			}
			if (is_gt && b == 'm')         /* ESC[>...m : modifyOtherKeys */
			{
				if (n <= sizeof(kbd_m))
				{
					memcpy(kbd_m, buf + i, n);
					kbd_m_len = n;
				}
				break;
			}
			if (!((b >= '0' && b <= '9') || b == ';' || b == ':'))
				break;
		}
	}
}

/* Scan a chunk of child output for DEC private mode set/reset (CSI ? Pm... h/l)
** and update dec_on[] for any tracked mode. Handles grouped params
** (e.g. ESC[?1000;1006h sets both). The terminator h/l applies to every param
** in that one sequence. Sequences split across reads are not stitched. */
static void
track_decmode(const unsigned char *buf, size_t len)
{
	size_t i;

	for (i = 0; i + 2 < len; i++)
	{
		size_t j;
		int params[16], np = 0, val = 0, have = 0;

		if (buf[i] != '\033' || buf[i + 1] != '[' || buf[i + 2] != '?')
			continue;

		for (j = i + 3; j < len; j++)
		{
			unsigned char b = buf[j];

			if (b >= '0' && b <= '9')
			{
				val = val * 10 + (b - '0');
				have = 1;
				continue;
			}
			if (b == ';')
			{
				if (have && np < 16)
					params[np++] = val;
				val = 0;
				have = 0;
				continue;
			}
			if (b == 'h' || b == 'l')
			{
				int on = (b == 'h'), p, k;

				if (have && np < 16)
					params[np++] = val;
				for (p = 0; p < np; p++)
					for (k = 0; k < N_DEC_MODES; k++)
						if (dec_modes[k] == params[p])
							dec_on[k] = (unsigned char)on;
			}
			break; /* h/l handled above; any other byte ends this CSI */
		}
	}
}

/* Unlink a client from the list and free it (socket + output queue). */
static void
remove_client(struct client *p)
{
	dch_trace("remove client fd=%d attached=%d outlen=%zu", p->fd,
	          p->attached, p->outlen);
	if (p->wait_pat[0])
		n_waiters--;
	close(p->fd);
	if (p->next)
		p->next->pprev = p->pprev;
	*(p->pprev) = p->next;
	free(p->out);
	free(p);
}

/* Append output for a client's send queue. Returns -1 if the client has
** backed up past OUT_CAP (or allocation fails) and should be dropped. */
static int
queue_to_client(struct client *p, const unsigned char *buf, size_t len)
{
	/* Reclaim the already-sent prefix before measuring/growing. */
	if (p->outoff)
	{
		memmove(p->out, p->out + p->outoff, p->outlen - p->outoff);
		p->outlen -= p->outoff;
		p->outoff = 0;
	}
	if (p->outlen + len > OUT_CAP)
		return -1;
	if (p->outlen + len > p->outcap)
	{
		size_t ncap = p->outcap ? p->outcap : BUFSIZE;
		unsigned char *nb;

		/* Doubling can't wrap: outlen+len <= OUT_CAP (4 MB) is enforced
		** above, so ncap tops out near 4 MB. If OUT_CAP is ever bumped
		** anywhere near SIZE_MAX/2, this needs an overflow guard. */
		while (ncap < p->outlen + len)
			ncap *= 2;
		nb = realloc(p->out, ncap);
		if (!nb)
			return -1;
		p->out = nb;
		p->outcap = ncap;
	}
	memcpy(p->out + p->outlen, buf, len);
	p->outlen += len;
	return 0;
}

/* Flush as much queued output as the socket accepts (nonblocking). Returns
** -1 on a hard write error (drop the client), 0 otherwise (EAGAIN just means
** try again when writable). */
static int
flush_client(struct client *p)
{
	while (p->outoff < p->outlen)
	{
		ssize_t n = write(p->fd, p->out + p->outoff,
		                  p->outlen - p->outoff);

		if (n > 0)
		{
			p->outoff += (size_t)n;
			continue;
		}
		if (n < 0 && errno == EINTR)
			continue;
		if (n < 0 && errno == EAGAIN)
			return 0;
		return -1;
	}
	p->outoff = p->outlen = 0;
	return 0;
}

/* One pty output buffer: track modes, queue to attached clients, feed the
** mirror. Shared by pty_activity (select wake) and drain_pty (gate-open /
** pre-read sync). */
static void
process_pty_buf(const unsigned char *buf, size_t len)
{
	struct client *p, *next;

	/* Real output happened — refresh the activity stamp (throttled). */
	touch_activity();

#ifdef BROKEN_MASTER
	/* Get the current terminal settings. */
	if (tcgetattr(the_pty.slave, &the_pty.term) < 0)
		exit(1);
#else
	/* Get the current terminal settings. */
	if (tcgetattr(the_pty.fd, &the_pty.term) < 0)
		exit(1);
#endif

	/* Remember any enhanced-keyboard mode toggles so we can re-arm a
	** client that attaches later (see kbd_u / kbd_m). */
	track_keymode(buf, len);
	track_decmode(buf, len);

	/* Queue to every attached client. A slow client that overflows its
	** queue is dropped rather than stalling the pty and the other clients. */
	for (p = clients; p; p = next)
	{
		next = p->next;
		if (!p->attached)
			continue;
		/* Drain any existing backlog FIRST: a client momentarily at OUT_CAP
		** but recovering would otherwise be dropped by queue_to_client before
		** it got a chance to flush. flush on an empty queue is a no-op; the
		** trailing flush pushes what we just queued out the same iteration. */
		if (flush_client(p) < 0 ||
		    queue_to_client(p, buf, len) < 0 ||
		    flush_client(p) < 0)
			remove_client(p);
	}

	/* Feed the terminal mirror AFTER the flush loop: attached clients
	** keep first claim on the wake's time budget. */
	dch_vt_feed(buf, len);
}

/* Pull any pty output that is pending RIGHT NOW into the mirror (and to
** attached clients). Needed before servicing MSG_READ/MSG_WAIT: the pty may
** have been gated out of this iteration's readfds (--spawn, nothing attached
** yet), or simply have bytes queued behind this control frame's wake.
**
** the_pty.fd is BLOCKING (and must stay so: pty writes go through
** write_buf_or_fail, which exits on EAGAIN), so each read is gated by a
** zero-timeout poll instead of looping until EAGAIN.
**
** BOUNDED: a child producing output nonstop (yes, tail -f, chatty build)
** keeps the pty readable forever; an unbounded loop here would wedge the
** whole single-threaded master. 256 buffers (4 MB) swallows any startup
** screen; whatever is still pending drains through the normal select-loop
** pty_activity wakes. */
#define DRAIN_MAX_BUFS 256
static void
drain_pty(void)
{
	unsigned char buf[BUFSIZE];
	struct pollfd pfd;
	ssize_t len;
	int i;

	pfd.fd = the_pty.fd;
	pfd.events = POLLIN;
	for (i = 0; i < DRAIN_MAX_BUFS; i++)
	{
		if (poll(&pfd, 1, 0) <= 0 || !(pfd.revents & POLLIN))
			return;
		len = read(the_pty.fd, buf, sizeof(buf));
		if (len <= 0)
			return; /* child died; the main loop's wake handles it */
		process_pty_buf(buf, (size_t)len);
	}
}

/* Queue one hand-built [type][len:2 LE][payload] response frame on a control
** client. NEVER write_packet_or_fail here — that exits the whole master on a
** transient write error. Returns -1 if the client was dropped. */
static int
queue_frame(struct client *p, unsigned char type, unsigned int len,
            const unsigned char *payload, size_t plen)
{
	unsigned char hdr[PKT_HDR];

	hdr[0] = type;
	hdr[1] = len & 0xff;
	hdr[2] = (len >> 8) & 0xff;
	if (queue_to_client(p, hdr, sizeof(hdr)) < 0 ||
	    (plen && queue_to_client(p, payload, plen) < 0))
	{
		remove_client(p);
		return -1;
	}
	return 0;
}

/* Response frame with no payload; the length field carries the status. */
static int
queue_status(struct client *p, unsigned char type, unsigned int status)
{
	return queue_frame(p, type, status, NULL, 0);
}

/* The mirror returned a runtime error: free it and answer DCH_ST_ERR from
** now on (degradation for returned errors; a Zig panic still aborts). */
static void
vt_latch_off(void)
{
	dch_trace("vt mirror error -> latched off");
	dch_vt_free();
	vt_latched_off = 1;
}

static unsigned int
vt_missing_status(void)
{
	return vt_latched_off ? DCH_ST_ERR : DCH_ST_NOVT;
}

/* Build the byte string that repaints the mirror's current visible screen
** onto a freshly cleared terminal and leaves the caret where the child thinks
** it is.
**
** dtach's contract was "clear the screen, poke the program with SIGWINCH,
** trust it to repaint". That only holds for programs that repaint from
** scratch. A differential renderer (Claude Code / Ink, and anything else that
** diffs against its own model of the screen) sees a WINCH at an UNCHANGED size,
** concludes nothing needs redrawing, and emits only the rows it happens to
** rewrite next — so the attaching human stares at a blank screen until they
** resize the window and force a real dimension change. We hold the whole
** screen in the mirror already; send it.
**
** Cursor is read BEFORE the snapshot and no pty read happens in between, so
** the caret belongs to the screen it is placed on. Returns 0 and a malloc'd
** buffer (free with free()), or -1 when there is no usable mirror. */
#define HOME_ERASE "\033[H\033[J"

static int
vt_restore_blob(char **out, size_t *outlen)
{
	char *snap, *buf, tail[32];
	size_t snaplen;
	int row, col, vis, wrap, tl;

	if (!dch_vt_enabled())
		return -1;
	if (dch_vt_cursor(&row, &col, &vis, &wrap) < 0)
		return -1;
	(void)wrap;	/* pending soft-wrap can't be expressed with CUP */
	if (dch_vt_snapshot(DCH_VT_FMT_ANSI, 0, &snap, &snaplen) < 0)
	{
		vt_latch_off();
		return -1;
	}
	/* SGR reset so the dump's last cell attributes don't bleed into what
	** the child prints next, then park the caret. Hidden cursor is
	** re-hidden; a visible one needs nothing (both a fresh alt screen and
	** a fresh mirror start visible). */
	tl = snprintf(tail, sizeof tail, "\033[0m\033[%d;%dH%s", row + 1,
	              col + 1, vis ? "" : "\033[?25l");
	if (tl < 0 || (size_t)tl >= sizeof tail)
	{
		dch_vt_buf_free(snap);
		return -1;
	}
	/* Home+erase leads the blob. An attaching client cleared its own
	** screen already, so there it is a harmless duplicate — but a client
	** carried across a restart did not, and the snapshot is trimmed (no
	** trailing blanks), so without this the old frame's bottom rows
	** survive and the repaint lands wherever the caret happened to be. */
	buf = malloc(sizeof HOME_ERASE - 1 + snaplen + (size_t)tl);
	if (!buf)
	{
		dch_vt_buf_free(snap);
		return -1;
	}
	memcpy(buf, HOME_ERASE, sizeof HOME_ERASE - 1);
	memcpy(buf + sizeof HOME_ERASE - 1, snap, snaplen);
	memcpy(buf + sizeof HOME_ERASE - 1 + snaplen, tail, (size_t)tl);
	dch_vt_buf_free(snap);
	*out = buf;
	*outlen = sizeof HOME_ERASE - 1 + snaplen + (size_t)tl;
	return 0;
}

/* Queue a repaint of the mirrored screen onto p's out-queue. Returns -1 only
** when the queue actually rejected the bytes.
**
** The size guard is the point: a 1024x1024 mirror painted with per-cell SGR
** renders to ~24 MB, six times OUT_CAP, and an overflow costs the client its
** connection — the attach would fail outright instead of merely looking stale.
** Skipping the repaint degrades to the historical clear-and-SIGWINCH
** behaviour, which is the same path DCH_NO_REPLAY=1 selects. Half of OUT_CAP,
** not all of it, because the mode re-arms are already queued ahead of us. */
static int
queue_replay(struct client *p)
{
	char *blob;
	size_t bloblen;
	int rc = 0;

	p->want_replay = 0;
	if (vt_restore_blob(&blob, &bloblen) < 0)
		return 0;
	if (bloblen <= OUT_CAP / 2)
		rc = queue_to_client(p, (unsigned char *)blob, bloblen);
	else
		dch_trace("replay: snapshot %zu over cap, skipping", bloblen);
	free(blob);
	return rc;
}

/* Serve MSG_READ. payload: {u8 format; u8 source; u16 lines LE} plus an
** optional 5th flags byte (`flags`; 0 when the client sent only 4).
** Returns -1 if the client was dropped. */
#define READ_RESP_CAP (2u * 1024u * 1024u)
#define READ_HEADROOM (64u * 1024u)
static int
do_read(struct client *p, const unsigned char *payload, unsigned int flags)
{
	unsigned int format = payload[0];
	unsigned int source = payload[1];
	unsigned int lines = (unsigned int)payload[2] |
	                     ((unsigned int)payload[3] << 8);
	unsigned int status = DCH_ST_OK;
	unsigned char cur[DCH_READ_CURSOR_LEN];
	int crow, ccol, cvis, cwrap, send_cursor = 0;
	char *snap, *body;
	size_t snaplen, off;

	if (!dch_vt_enabled())
		return queue_status(p, MSG_READ_END, vt_missing_status());

	/* One outstanding response per control connection: bytes still
	** queued means the previous response hasn't drained — protocol
	** abuse, not a wait state. */
	if (p->outlen - p->outoff > 0)
	{
		dch_trace("read while response pending -> drop fd=%d", p->fd);
		remove_client(p);
		return -1;
	}

	if (format > DCH_READ_ANSI || source > DCH_READ_RECENT ||
	    (flags & ~(unsigned int)DCH_READ_F_ALL))
		return queue_status(p, MSG_READ_END, DCH_ST_ERR);
	/* Recent-N is a scrollback tail; the live cursor sits on the visible
	** screen and means nothing against it. Refuse rather than answer with
	** a coordinate the caller would misplace. */
	if ((flags & DCH_READ_F_CURSOR) && source == DCH_READ_RECENT)
		return queue_status(p, MSG_READ_END, DCH_ST_ERR);
	if (source == DCH_READ_RECENT && lines == 0)
		lines = 100;

	if (dch_vt_snapshot(format == DCH_READ_ANSI ? DCH_VT_FMT_ANSI
	    : DCH_VT_FMT_PLAIN,
	    source == DCH_READ_RECENT ? (int)lines : 0,
	    &snap, &snaplen) < 0)
	{
		vt_latch_off();
		return queue_status(p, MSG_READ_END, DCH_ST_ERR);
	}

	/* Bound the response: keep the TAIL (most recent output is what a
	** controlling agent needs), report the cut. */
	body = snap;
	if (snaplen > READ_RESP_CAP)
	{
		body += snaplen - READ_RESP_CAP;
		snaplen = READ_RESP_CAP;
		status = DCH_ST_TRUNC;
	}

	/* Cursor comes from the SAME servicing of this frame as the snapshot
	** above — no pty read happens in between, so the caret the caller
	** paints belongs to the screen it paints. Queried before anything is
	** queued so a failure still answers with a clean status-only reply.
	**
	** NOT sent once the dump was tail-cut: the row is relative to the top
	** of the active area, and the cut threw that top away (byte-wise, so
	** even counting the dropped newlines wouldn't recover the alignment).
	** The client turns the missing frame into an explicit error — better
	** than a coordinate that points at the wrong row. */
	if ((flags & DCH_READ_F_CURSOR) && status != DCH_ST_TRUNC)
	{
		if (dch_vt_cursor(&crow, &ccol, &cvis, &cwrap) < 0)
		{
			/* The snapshot above just worked, so the mirror is
			** alive — answer this one request badly, don't latch
			** the whole session off for every other reader. */
			dch_vt_buf_free(snap);
			return queue_status(p, MSG_READ_END, DCH_ST_ERR);
		}
		/* ponytail: u16 on the wire — the mirror grid is clamped to
		** 1024x1024 in dch_vt_resize, and the source values are
		** uint16_t, so neither coordinate can overflow the field. */
		cur[0] = (unsigned char)(crow & 0xff);
		cur[1] = (unsigned char)((crow >> 8) & 0xff);
		cur[2] = (unsigned char)(ccol & 0xff);
		cur[3] = (unsigned char)((ccol >> 8) & 0xff);
		cur[4] = (unsigned char)(cvis ? 1 : 0);
		cur[5] = (unsigned char)(cwrap ? 1 : 0);
		send_cursor = 1;
	}

	/* Admission check: never let a response ride into the OUT_CAP
	** overflow drop (that kills the client without an END frame). */
	if (p->outlen - p->outoff + snaplen + READ_HEADROOM > OUT_CAP)
	{
		dch_vt_buf_free(snap);
		return queue_status(p, MSG_READ_END, DCH_ST_BUSY);
	}

	/* Cursor first: the caller knows where the caret goes before it has
	** finished reassembling the screen. */
	if (send_cursor &&
	    queue_frame(p, MSG_READ_CURSOR, DCH_READ_CURSOR_LEN, cur,
	    sizeof(cur)) < 0)
	{
		dch_vt_buf_free(snap);
		return -1;
	}

	for (off = 0; off < snaplen; off += PKT_MAX)
	{
		size_t chunk = snaplen - off > PKT_MAX ? PKT_MAX
		    : snaplen - off;

		if (queue_frame(p, MSG_READ_DATA, (unsigned int)chunk,
		    (const unsigned char *)body + off, chunk) < 0)
		{
			dch_vt_buf_free(snap);
			return -1;
		}
	}
	dch_vt_buf_free(snap);
	return queue_status(p, MSG_READ_END, status);
}

/* Find the line containing the first occurrence of pat in hay; out gets the
** line's bounds. Returns 1 on hit. */
static int
find_match_line(const char *hay, const char *pat, const char **line,
                size_t *linelen)
{
	const char *hit = strstr(hay, pat);
	const char *start, *end;

	if (!hit)
		return 0;
	for (start = hit; start > hay && start[-1] != '\n'; start--)
		;
	for (end = hit; *end && *end != '\n'; end++)
		;
	*line = start;
	*linelen = (size_t)(end - start);
	return 1;
}

/* Rows of recent scrollback a wait pattern is matched against. One 16 KB
** pty burst can scroll a line clean past the visible grid before we get to
** look, so match deeper than the screen. */
#define WAIT_SCAN_ROWS 256

/* Check every pending waiter against the current mirror state. `exclude`
** guards the caller's own client pointer (handle_packet still holds it).
** Runs after each pty batch; skipped entirely while no waiter exists. */
static void
check_waiters(struct client *exclude)
{
	struct client *p, *next;
	char *snap;
	size_t snaplen;

	if (n_waiters <= 0 || !dch_vt_enabled())
		return;

	if (dch_vt_snapshot(DCH_VT_FMT_PLAIN, WAIT_SCAN_ROWS, &snap,
	    &snaplen) < 0)
	{
		vt_latch_off();
		/* Tell every waiter the mirror is gone rather than hanging
		** them until their timeout. */
		for (p = clients; p; p = next)
		{
			next = p->next;
			if (!p->wait_pat[0] || p == exclude)
				continue;
			p->wait_pat[0] = '\0';
			n_waiters--;
			queue_status(p, MSG_READ_END, DCH_ST_ERR);
		}
		return;
	}

	for (p = clients; p; p = next)
	{
		const char *line;
		size_t linelen;

		next = p->next;
		if (!p->wait_pat[0] || p == exclude)
			continue;
		if (!find_match_line(snap, p->wait_pat, &line, &linelen))
			continue;
		p->wait_pat[0] = '\0';
		n_waiters--;
		if (linelen > PKT_MAX)
			linelen = PKT_MAX;
		queue_frame(p, MSG_WAIT_HIT, (unsigned int)linelen,
		    (const unsigned char *)line, linelen);
	}
	dch_vt_buf_free(snap);
}

/* Serve MSG_WAIT: answer immediately when the pattern is already on screen,
** otherwise park it on the client. Returns -1 if the client was dropped. */
static int
do_wait(struct client *p, const unsigned char *payload, unsigned int len)
{
	char *snap;
	size_t snaplen;
	const char *line;
	size_t linelen;

	if (!dch_vt_enabled())
		return queue_status(p, MSG_READ_END, vt_missing_status());
	if (len == 0 || len > DCH_WAIT_MAX)
	{
		dch_trace("bad wait pattern len=%u -> drop fd=%d", len, p->fd);
		remove_client(p);
		return -1;
	}

	if (p->wait_pat[0])
		n_waiters--; /* replacing an existing pattern */
	memcpy(p->wait_pat, payload, len);
	p->wait_pat[len] = '\0';

	if (dch_vt_snapshot(DCH_VT_FMT_PLAIN, WAIT_SCAN_ROWS, &snap,
	    &snaplen) < 0)
	{
		p->wait_pat[0] = '\0';
		vt_latch_off();
		return queue_status(p, MSG_READ_END, DCH_ST_ERR);
	}
	if (find_match_line(snap, p->wait_pat, &line, &linelen))
	{
		p->wait_pat[0] = '\0';
		if (linelen > PKT_MAX)
			linelen = PKT_MAX;
		if (queue_frame(p, MSG_WAIT_HIT, (unsigned int)linelen,
		    (const unsigned char *)line, linelen) < 0)
		{
			dch_vt_buf_free(snap);
			return -1;
		}
	}
	else
		n_waiters++;
	dch_vt_buf_free(snap);
	return 0;
}

/* Serve MSG_KEYS: encode the combos against the mirror's LIVE keyboard modes
** and write to the pty. Returns -1 if the client was dropped. */
static int
do_keys(struct client *p, const unsigned char *payload, unsigned int len)
{
	char spec[PKT_MAX + 1];
	char *bytes;
	size_t nbytes, i;

	if (!dch_vt_enabled())
		return queue_status(p, MSG_ACK, vt_missing_status());

	/* Wire format is NUL-separated; the shim takes whitespace-separated. */
	memcpy(spec, payload, len);
	spec[len] = '\0';
	for (i = 0; i < len; i++)
		if (spec[i] == '\0')
			spec[i] = ' ';

	if (dch_vt_encode_keys(spec, &bytes, &nbytes) < 0)
		return queue_status(p, MSG_ACK, DCH_ST_ERR);

	/* The pty fd is blocking and write_buf_or_fail loops until done: a
	** batch bigger than one kernel pty buffer could stall the whole
	** select loop if the child isn't draining. Real key batches are tiny
	** (a few hundred bytes); refuse anything that could block. */
	if (nbytes > BUFSIZE)
	{
		dch_vt_buf_free(bytes);
		return queue_status(p, MSG_ACK, DCH_ST_ERR);
	}
	write_buf_or_fail(the_pty.fd, bytes, nbytes);
	dch_vt_buf_free(bytes);
	return queue_status(p, MSG_ACK, DCH_ST_OK);
}

/* ---- live restart (MSG_RESTART) --------------------------------------- */
/*
** Swap the code serving a live session without touching the child.
**
** The master is a single-process select loop that owns the pty master fd and
** is the child's real parent, so the whole job is execv() on ourselves. Every
** descriptor survives an exec unless it is FD_CLOEXEC (only the listen socket
** is, and we clear it), the pid does not change, and the child therefore stays
** OUR child — SIGCHLD and the exit-status path keep working across the swap.
**
** That is less machinery than handing the pty to a second process over
** SCM_RIGHTS, and better: a fresh process cannot inherit parenthood, so a
** two-server hand-off loses waitpid() on the shell it adopted.
**
** Rollback is free. If execv() returns, nothing has changed: we fall back
** into the select loop and answer DCH_ST_ERR on the same connection.
**
** Two signal details the exec depends on. SIGPIPE was set to SIG_IGN, and
** execve preserves SIG_IGN while resetting handlers — which is why the new
** image can write to carried clients before it reinstalls anything. Do not
** convert that to a handler. SIGCHLD's handler IS reset, so a child that
** exits inside the exec window signals nothing; the backstop is the pty read
** returning EOF, which kills the master the same way.
**
** What does NOT survive: control connections other than the requester (they
** are one-shot RPCs; the client sees EOF and can retry) and the mirror's
** scrollback — only the visible screen is carried over, re-fed as ANSI.
** The re-fed screen also lands on the mirror's PRIMARY buffer even when the
** child is on the alt screen (1049 is not in dec_modes, so it is not carried):
** the pixels are right, but a later \033[?1049l restores a primary buffer
** holding what the alt screen showed, until the child repaints.
** ponytail: visible screen only. Carrying scrollback means replaying N
** screens of ANSI through a fresh grid; add it if --read --recent right after
** a restart ever matters more than the complexity.
*/
#define RESUME_MAGIC   "dch-resume 1\n"
#define RESUME_MAX_CLI 64
/* Blob cap. A visible screen of dense ANSI is tens of KB; the mirror grid is
** clamped to 1024x1024, so this is generous headroom, not a real limit. */
#define RESUME_MAX_SNAP (8u * 1024u * 1024u)

struct resume_state
{
	int valid;
	int lfd, pfd, ackfd;
	pid_t cpid;
	struct winsize ws;
	int gate, redraw;
	unsigned char dec[N_DEC_MODES];
	unsigned char ku[sizeof kbd_u];
	size_t kulen;
	unsigned char km[sizeof kbd_m];
	size_t kmlen;
	int cfd[RESUME_MAX_CLI];
	/* The partial frame each carried client had already read off its
	** socket, restored into the new image's reassembly buffer. */
	unsigned char cin[RESUME_MAX_CLI][PKT_HDR + PKT_MAX];
	size_t cinlen[RESUME_MAX_CLI];
	int ncfd;
	unsigned char *snap;
	size_t snaplen;
};
static struct resume_state resume;

static void
hex_encode(const unsigned char *in, size_t len, char *out)
{
	static const char d[] = "0123456789abcdef";
	size_t i;

	for (i = 0; i < len; i++)
	{
		out[i * 2] = d[in[i] >> 4];
		out[i * 2 + 1] = d[in[i] & 0xf];
	}
	out[len * 2] = '\0';
}

/* Decode `hex` into out (cap bytes). Returns the byte count, or -1 on a bad
** digit / odd length / overflow. */
static int
hex_decode(const char *hex, unsigned char *out, size_t cap)
{
	size_t n = strlen(hex), i;

	if (n % 2)
		return -1;
	if (n / 2 > cap)
		return -1;
	for (i = 0; i < n; i += 2)
	{
		int hi, lo, k;

		hi = lo = 0;
		for (k = 0; k < 2; k++)
		{
			char c = hex[i + k];
			int v = (c >= '0' && c <= '9') ? c - '0'
			      : (c >= 'a' && c <= 'f') ? c - 'a' + 10
			      : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : -1;

			if (v < 0)
				return -1;
			if (k == 0)
				hi = v;
			else
				lo = v;
		}
		out[i / 2] = (unsigned char)((hi << 4) | lo);
	}
	return (int)(n / 2);
}

/* Serialize everything the next image needs into `path`. Text header lines
** followed by `snap <len>` and that many raw bytes. Returns 0 on success. */
static int
restart_save(const char *path, int lfd, int ackfd)
{
	char *blob = NULL, hex[sizeof ((struct client *)0)->inbuf * 2 + 1];
	size_t bloblen = 0;
	struct client *p, *pnext;
	FILE *f;
	int fd, i, n = 0;

	unlink(path);			/* a stale blob from a failed restart */
	fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
	if (fd < 0)
		return -1;
	f = fdopen(fd, "w");
	if (!f)
	{
		close(fd);
		unlink(path);
		return -1;
	}

	fputs(RESUME_MAGIC, f);
	fprintf(f, "lfd %d\n", lfd);
	fprintf(f, "pfd %d\n", the_pty.fd);
	fprintf(f, "cpid %ld\n", (long)the_pty.pid);
	fprintf(f, "ack %d\n", ackfd);
	fprintf(f, "ws %u %u %u %u\n", (unsigned)the_pty.ws.ws_row,
	        (unsigned)the_pty.ws.ws_col, (unsigned)the_pty.ws.ws_xpixel,
	        (unsigned)the_pty.ws.ws_ypixel);
	fprintf(f, "gate %d\n", pty_gated);
	fprintf(f, "redraw %d\n", redraw_method);
	fputs("dec ", f);
	for (i = 0; i < N_DEC_MODES; i++)
		fputc(dec_on[i] ? '1' : '0', f);
	fputc('\n', f);
	hex_encode(kbd_u, kbd_u_len, hex);
	fprintf(f, "ku %s\n", hex);
	hex_encode(kbd_m, kbd_m_len, hex);
	fprintf(f, "km %s\n", hex);
	/* Attached clients ride across as bare fds. fd > 2 because the daemon
	** re-points 0/1/2 at /dev/null on the way back up.
	**
	** The half-frame in p->inbuf rides with them. Those bytes are already
	** off the socket, so dropping them would leave the next image reading
	** mid-frame and reinterpreting the tail as a header — a leading 0x00 is
	** MSG_PUSH, which would type the rest of a keystroke burst straight
	** into the pty. */
	for (p = clients; p; p = pnext)
	{
		pnext = p->next;
		if (!p->attached || p->fd == ackfd || p->fd <= 2)
			continue;
		if (n++ >= RESUME_MAX_CLI)
		{
			/* Past the cap the fd would survive the exec owned by
			** nobody. Drop the client whole — this write can still
			** fail below, and execv can still return, and either
			** rollback would leave a live struct client pointing at
			** a closed fd, which select() answers with EBADF. */
			remove_client(p);
			continue;
		}
		hex_encode(p->inbuf, p->inlen, hex);
		fprintf(f, "cli %d %s\n", p->fd, hex);
	}

	/* Drop the snapshot rather than write one the loader will reject: past
	** the exec there is no way back, so a blob that fails to load costs the
	** whole session, while a missing snapshot costs one repaint. A 1024x1024
	** grid of dense per-cell SGR renders well past the cap. */
	if (vt_restore_blob(&blob, &bloblen) < 0 || bloblen > RESUME_MAX_SNAP)
	{
		if (bloblen > RESUME_MAX_SNAP)
			dch_trace("restart: snapshot %zu > cap, dropping",
			          bloblen);
		free(blob);
		blob = NULL;
		bloblen = 0;
	}
	fprintf(f, "snap %zu\n", bloblen);
	if (bloblen)
		fwrite(blob, 1, bloblen, f);
	free(blob);

	if (ferror(f) || fclose(f) != 0)
	{
		unlink(path);
		return -1;
	}
	return 0;
}

/* Read the blob named by DCH_RESUME into `resume`. Always consumes the file
** and the env var, so a malformed blob can never be re-read and a crashed
** restart leaves nothing behind. Sets resume.valid on success. */
static void
restart_load(void)
{
	const char *path = getenv("DCH_RESUME");
	struct stat st;
	unsigned char *buf, *body;
	char *line, *save;
	size_t len;
	ssize_t got;
	int fd, seen_snap = 0;

	if (!path || !*path)
		return;
	if (!resume_is_ours(path))
		return;
	fd = open(path, O_RDONLY | O_NOFOLLOW);
	/* Unset immediately: whatever happens next, the child we exec into must
	** never inherit this and try to resume a session that isn't its own. */
	unsetenv("DCH_RESUME");
	if (fd < 0)
		return;
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) || st.st_uid != getuid()
	    || (size_t)st.st_size > RESUME_MAX_SNAP + 64 * 1024)
	{
		close(fd);
		return;
	}
	/* Only now: a DCH_RESUME we did not write is not ours to delete, and
	** unlinking on the way in made this a file-removal primitive for
	** anything that could plant the variable in a dch launch. */
	unlink(path);
	len = (size_t)st.st_size;
	buf = malloc(len + 1);
	if (!buf)
	{
		close(fd);
		return;
	}
	for (got = 0; (size_t)got < len; )
	{
		ssize_t n = read(fd, buf + got, len - (size_t)got);

		if (n <= 0)
			break;
		got += n;
	}
	close(fd);
	if ((size_t)got != len || len < sizeof RESUME_MAGIC - 1 ||
	    memcmp(buf, RESUME_MAGIC, sizeof RESUME_MAGIC - 1) != 0)
	{
		free(buf);
		return;
	}
	buf[len] = '\0';

	/* Header lines up to `snap`, then `snaplen` raw bytes. strtok_r over
	** the NUL-terminated copy is safe: the raw tail is never tokenized
	** because we stop at the snap line. */
	memset(&resume, 0, sizeof resume);
	resume.lfd = resume.pfd = resume.ackfd = -1;
	body = buf + sizeof RESUME_MAGIC - 1;
	line = strtok_r((char *)body, "\n", &save);
	for (; line; line = strtok_r(NULL, "\n", &save))
	{
		unsigned r, c, xp, yp;
		long v;
		int cfd, off = 0;

		if (sscanf(line, "lfd %d", &resume.lfd) == 1)
			continue;
		if (sscanf(line, "pfd %d", &resume.pfd) == 1)
			continue;
		if (sscanf(line, "ack %d", &resume.ackfd) == 1)
			continue;
		if (sscanf(line, "cpid %ld", &v) == 1)
		{
			resume.cpid = (pid_t)v;
			continue;
		}
		if (sscanf(line, "ws %u %u %u %u", &r, &c, &xp, &yp) == 4)
		{
			resume.ws.ws_row = (unsigned short)r;
			resume.ws.ws_col = (unsigned short)c;
			resume.ws.ws_xpixel = (unsigned short)xp;
			resume.ws.ws_ypixel = (unsigned short)yp;
			continue;
		}
		if (sscanf(line, "gate %d", &resume.gate) == 1)
			continue;
		if (sscanf(line, "redraw %d", &resume.redraw) == 1)
			continue;
		if (sscanf(line, "cli %d%n", &cfd, &off) == 1)
		{
			const char *hex = line + off;
			int n;

			if (resume.ncfd >= RESUME_MAX_CLI || cfd <= 2)
				continue;
			while (*hex == ' ')
				hex++;
			/* A client that had a half-read frame pending carries it
			** here; a bad field costs only that client's pending
			** bytes, so drop them rather than the whole resume. */
			n = hex_decode(hex, resume.cin[resume.ncfd],
			               sizeof resume.cin[0]);
			resume.cinlen[resume.ncfd] = n > 0 ? (size_t)n : 0;
			resume.cfd[resume.ncfd++] = cfd;
			continue;
		}
		if (strncmp(line, "dec ", 4) == 0)
		{
			int i;

			for (i = 0; i < N_DEC_MODES && line[4 + i]; i++)
				resume.dec[i] = (line[4 + i] == '1');
			continue;
		}
		if (strncmp(line, "ku ", 3) == 0)
		{
			int n = hex_decode(line + 3, resume.ku,
			                   sizeof resume.ku);
			resume.kulen = n > 0 ? (size_t)n : 0;
			continue;
		}
		if (strncmp(line, "km ", 3) == 0)
		{
			int n = hex_decode(line + 3, resume.km,
			                   sizeof resume.km);
			resume.kmlen = n > 0 ? (size_t)n : 0;
			continue;
		}
		if (strncmp(line, "snap ", 5) == 0)
		{
			unsigned long sl = strtoul(line + 5, NULL, 10);
			/* strtok_r replaced this line's '\n' with NUL, so the
			** raw tail starts one byte past the terminator. */
			unsigned char *tail = (unsigned char *)line +
			                      strlen(line) + 1;

			seen_snap = 1;
			if (sl && sl <= RESUME_MAX_SNAP &&
			    tail + sl <= buf + len)
			{
				resume.snap = malloc(sl);
				if (resume.snap)
				{
					memcpy(resume.snap, tail, sl);
					resume.snaplen = sl;
				}
			}
			/* MUST break, not continue: another strtok_r() would
			** write NULs into the raw tail we just copied past. */
			break;
		}
	}
	free(buf);

	/* A carried fd at 0/1/2 would be clobbered by the daemon's
	** dup2(/dev/null); the saver refuses those, so this only rejects a
	** corrupt blob. */
	if (!seen_snap || resume.lfd <= 2 || resume.pfd <= 2 ||
	    resume.cpid <= 0)
	{
		free(resume.snap);
		memset(&resume, 0, sizeof resume);
		return;
	}
	resume.valid = 1;
}

/* Serve MSG_RESTART. Returns -1 if the client is gone; on success it does not
** return at all (the process is replaced). */
static int
do_restart(struct client *p, const unsigned char *req, unsigned int reqlen)
{
	char path[1100], exe[PKT_MAX + 1];
	const char *target = dch_exe;
	struct client *q, *next;
	int lfd = listen_fd;

	/* The requester names the binary to land on (its own). Anyone who can
	** reach this socket can already drive the session's keyboard, so this
	** is not a new trust boundary — but exec is, so take the path only if
	** it is a plain file this same uid owns and may execute. Anything
	** else falls back to the path we were started from. */
	if (reqlen > 0 && reqlen < sizeof exe)
	{
		struct stat st;

		memcpy(exe, req, reqlen);
		exe[reqlen] = '\0';
		if (exe[0] == '/' && !memchr(exe, '\0', reqlen) &&
		    stat(exe, &st) == 0 && S_ISREG(st.st_mode) &&
		    st.st_uid == getuid() && access(exe, X_OK) == 0)
			target = exe;
		else
			dch_trace("restart: rejecting requested exe");
	}

	if (!target || !dch_argv || lfd < 0)
		return queue_status(p, MSG_ACK, DCH_ST_ERR);
	/* Every fd we carry must be >2: master_process points 0/1/2 at
	** /dev/null on the way back up and would clobber them. */
	if (lfd <= 2 || the_pty.fd <= 2 || p->fd <= 2)
		return queue_status(p, MSG_ACK, DCH_ST_ERR);
	if (sidecar_path(".resume", path, sizeof path) < 0)
		return queue_status(p, MSG_ACK, DCH_ST_ERR);

	/* Pull anything the child has already written into the mirror, so the
	** screen we carry over is the screen as of right now. */
	drain_pty();

	if (restart_save(path, lfd, p->fd) < 0)
		return queue_status(p, MSG_ACK, DCH_ST_ERR);

	/* Drop every connection except the requester's. Other control conns are
	** one-shot RPCs mid-flight; they get a clean EOF and their caller
	** retries. This happens even if the exec below rolls back — a lost RPC
	** is recoverable, a carried-but-unowned fd is not. Attached clients are
	** in the blob and must NOT be closed here. */
	for (q = clients; q; q = next)
	{
		next = q->next;
		if (q != p && !q->attached)
			remove_client(q);
	}

#if defined(F_SETFD) && defined(FD_CLOEXEC)
	/* The listen socket is the only descriptor we deliberately marked
	** close-on-exec; the pty and the client sockets never were. */
	if (fcntl(lfd, F_SETFD, 0) < 0)
	{
		unlink(path);
		return queue_status(p, MSG_ACK, DCH_ST_ERR);
	}
#endif
	if (setenv("DCH_RESUME", path, 1) < 0)
	{
		unlink(path);
		return queue_status(p, MSG_ACK, DCH_ST_ERR);
	}

	dch_trace("restart exec %s", target);
	execv(target, dch_argv);

	/* Rollback. Nothing was handed over, nothing was signalled: put the
	** close-on-exec flag back, drop the blob, and keep serving. */
	dch_trace("restart exec failed errno=%d", errno);
	unsetenv("DCH_RESUME");
	unlink(path);
#if defined(F_SETFD) && defined(FD_CLOEXEC)
	fcntl(lfd, F_SETFD, FD_CLOEXEC);
#endif
	return queue_status(p, MSG_ACK, DCH_ST_ERR);
}

/* Process activity on the pty - Input and terminal changes are queued out to
** the attached clients (flushed by the main loop). If the pty goes away, we
** die. */
static void
pty_activity(void)
{
	unsigned char buf[BUFSIZE];
	ssize_t len;

	/* Read the pty activity */
	len = read(the_pty.fd, buf, sizeof(buf));
	dch_trace("pty read len=%zd", len);

	/* Error -> die */
	if (len <= 0)
	{
		int status;

		dch_trace("pty eof len=%zd errno=%d -> master exit", len, errno);

		if (wait(&status) >= 0)
		{
			if (WIFEXITED(status))
				exit(WEXITSTATUS(status));
		}
		exit(1);
	}

	process_pty_buf(buf, (size_t)len);
	check_waiters(NULL);
}

/* Wrap an already-connected fd in a client and link it in. calloc fail → drop
** the client; existing sessions stay live. calloc zeroes the reassembly and
** output-queue state (inlen/out/...). Returns NULL after closing fd on
** failure. Also used by the resume path, which adopts fds across a re-exec
** rather than accept()ing them. */
static struct client *
link_client(int fd)
{
	struct client *p;

	if (setnonblocking(fd) < 0)
	{
		close(fd);
		return NULL;
	}
	p = calloc(1, sizeof(struct client));
	if (!p)
	{
		close(fd);
		return NULL;
	}
	p->fd = fd;
	p->pprev = &clients;
	p->next = *(p->pprev);
	if (p->next)
		p->next->pprev = &p->next;
	*(p->pprev) = p;
	return p;
}

/* Process activity on the control socket */
static void
control_activity(int s)
{
	int fd = accept(s, NULL, NULL);

	if (fd >= 0)
		link_client(fd);
}

/* Act on one fully-reassembled frame. `len` is the header length field
** (PUSH = payload bytes, REDRAW = method); `payload` points at the frame's
** payload bytes (len for PUSH, a struct winsize for WINCH/REDRAW).
** Returns -1 when the client was removed (caller must stop touching p). */
static int
handle_packet(struct client *p, unsigned int type, unsigned int len,
              const unsigned char *payload)
{
	/* Enforce the connection role latch (see struct client). PUSH, WINCH
	** and DETACH stay role-neutral. */
	if (type == MSG_ATTACH)
	{
		if (p->role == ROLE_CONTROL)
		{
			dch_trace("attach on control conn -> drop fd=%d", p->fd);
			remove_client(p);
			return -1;
		}
		p->role = ROLE_ATTACHED;
	}
	else if (type == MSG_RESTART)
	{
		/* Control role, but NOT "someone showed up": a restart must not
		** open a --spawn session's pty gate, because the gate state is
		** one of the things it carries across. */
		if (p->role == ROLE_ATTACHED)
		{
			dch_trace("restart on attached conn -> drop fd=%d",
			          p->fd);
			remove_client(p);
			return -1;
		}
		p->role = ROLE_CONTROL;
		return do_restart(p, payload, len);
	}
	else if (type == MSG_KEYS || type == MSG_READ || type == MSG_WAIT)
	{
		if (p->role == ROLE_ATTACHED)
		{
			dch_trace("control verb on attached conn -> drop fd=%d",
			          p->fd);
			remove_client(p);
			return -1;
		}
		p->role = ROLE_CONTROL;

		/* A control client counts as "someone showed up": open the
		** --spawn pty gate, then pull pending pty output into the
		** mirror so READ/WAIT see current state (and KEYS encodes
		** against current keyboard modes) instead of last wake's. */
		pty_gated = 0;
		drain_pty();
		check_waiters(p);
	}

	/* A redraw from a control client would repaint every attached
	** human's screen — reading must never perturb the session. */
	if (type == MSG_REDRAW && p->role == ROLE_CONTROL)
		return 0;

	/* Push out data to the program. */
	if (type == MSG_PUSH)
	{
		write_buf_or_fail(the_pty.fd, payload, len);
	}

	else if (type == MSG_KEYS)
		return do_keys(p, payload, len);
	else if (type == MSG_READ)
	{
		/* 4-byte payload {format,source,lines LE}; shorter is a
		** malformed client — same treatment as a bad WAIT. A 5th byte
		** carries request flags; older clients send none. */
		if (len < 4)
		{
			remove_client(p);
			return -1;
		}
		return do_read(p, payload, len >= 5 ? payload[4] : 0u);
	}
	else if (type == MSG_WAIT)
		return do_wait(p, payload, len);

	/* Attach or detach from the program. */
	else if (type == MSG_ATTACH)
	{
		int k, bad;

		p->attached = 1;
		dch_trace("attach client fd=%d", p->fd);

		/* Everything below goes through the client's ORDERED out-queue,
		** never a direct write(): the screen replay has to land after the
		** mode re-arms and before whatever the child prints next, and a
		** direct write on a nonblocking socket cannot promise that. */

		/* Re-arm the terminal modes the child enabled for this client; a
		** previous detach reset them, and the child only sets them once at
		** startup. No-op for anything the child never enabled. */
		bad = flush_client(p) < 0;
		if (!bad && kbd_u_len)
			bad = queue_to_client(p, kbd_u, kbd_u_len) < 0;
		if (!bad && kbd_m_len)
			bad = queue_to_client(p, kbd_m, kbd_m_len) < 0;
		for (k = 0; !bad && k < N_DEC_MODES; k++)
			if (dec_on[k])
			{
				unsigned char s[16];
				int sl = snprintf((char *)s, sizeof s,
				    "\033[?%dh", dec_modes[k]);
				if (sl > 0 && (size_t)sl <= sizeof(s))
					bad = queue_to_client(p, s,
					    (size_t)sl) < 0;
			}

		/* The screen repaint does NOT happen here: MSG_ATTACH carries no
		** winsize, so the mirror is still at the previous client's
		** geometry and a snapshot taken now would be wrapped for the
		** wrong width. The MSG_REDRAW that every client sends straight
		** after this frame carries the size — replay from there, once
		** the mirror has been resized. (Adding a payload to MSG_ATTACH
		** would desync every master built before it.) */
		p->want_replay = !no_replay;
		if (bad || flush_client(p) < 0)
		{
			remove_client(p);
			return -1;
		}
	}
	else if (type == MSG_DETACH)
	{
		p->attached = 0;
		/* Detaching before the sizing frame arrived: the repaint has
		** no one to land on any more. */
		p->want_replay = 0;
	}

	/* Window size change request, without a forced redraw. */
	else if (type == MSG_WINCH)
	{
		memcpy(&the_pty.ws, payload, sizeof(struct winsize));
		ioctl(the_pty.fd, TIOCSWINSZ, &the_pty.ws);
		dch_vt_resize(the_pty.ws.ws_col, the_pty.ws.ws_row);
		if (p->want_replay && (queue_replay(p) < 0 ||
		                       flush_client(p) < 0))
		{
			remove_client(p);
			return -1;
		}
	}

	/* Force a redraw using a particular method. */
	else if (type == MSG_REDRAW)
	{
		int method = len;

		/* If the client didn't specify a particular method, use
		** whatever we had on startup. */
		if (method == REDRAW_UNSPEC)
			method = redraw_method;
		/* REDRAW_NONE asked for no repaint; the replay is a repaint. */
		if (method == REDRAW_NONE)
		{
			p->want_replay = 0;
			return 0;
		}

		/* Set the window size. */
		memcpy(&the_pty.ws, payload, sizeof(struct winsize));
		ioctl(the_pty.fd, TIOCSWINSZ, &the_pty.ws);
		dch_vt_resize(the_pty.ws.ws_col, the_pty.ws.ws_row);

		/* Now that the mirror is at this client's size, hand it the
		** screen it attached for. */
		if (p->want_replay && (queue_replay(p) < 0 ||
		                       flush_client(p) < 0))
		{
			remove_client(p);
			return -1;
		}

		/* Send a ^L character if the terminal is in no-echo and
		** character-at-a-time mode. */
		if (method == REDRAW_CTRL_L)
		{
			char c = '\f';

			if (((the_pty.term.c_lflag & (ECHO|ICANON)) == 0) &&
			    (the_pty.term.c_cc[VMIN] == 1))
			{
				write_buf_or_fail(the_pty.fd, &c, 1);
			}
		}
		/* Send a WINCH signal to the program. */
		else if (method == REDRAW_WINCH)
		{
			killpty(&the_pty, SIGWINCH);
		}
	}

	/* A verb this master does not know — i.e. the client is NEWER than the
	** session. Say so instead of going silent: silence costs the client its
	** whole inactivity deadline and can only be guessed at afterwards. Never
	** answered on an attached connection, where a response frame would be
	** rendered as terminal output. */
	else if (p->role != ROLE_ATTACHED)
	{
		dch_trace("unsupported verb type=%u fd=%d", type, p->fd);
		return queue_status(p, MSG_ACK, DCH_ST_UNSUP);
	}
	return 0;
}

/* Process activity from a client. Reassembles variable-length frames from
** the (nonblocking) socket: a read may split one frame or carry several. */
static void
client_activity(struct client *p)
{
	ssize_t n;
	size_t off;

	n = read(p->fd, p->inbuf + p->inlen, sizeof(p->inbuf) - p->inlen);
	if (n < 0 && (errno == EAGAIN || errno == EINTR))
		return;
	if (n <= 0) /* EOF or hard error -> drop the client */
	{
		remove_client(p);
		return;
	}
	p->inlen += (size_t)n;

	/* Parse out every complete frame currently buffered. */
	off = 0;
	while (p->inlen - off >= PKT_HDR)
	{
		unsigned char *f = p->inbuf + off;
		unsigned int type = f[0];
		unsigned int len  = (unsigned int)f[1] |
		                    ((unsigned int)f[2] << 8);
		/* Unknown types take `len` as their payload size so a verb
		** added by a newer dch can be skipped whole and answered with
		** DCH_ST_UNSUP instead of desyncing the stream. That is the
		** contract for every future verb: if it carries a payload, the
		** header length field must be its byte count. */
		size_t plen = (type == MSG_WINCH || type == MSG_REDRAW)
		        ? sizeof(struct winsize)
		    : (type == MSG_ATTACH || type == MSG_DETACH) ? 0
		    : len;

		/* A PUSH length past PKT_MAX is not one of ours -> corrupt.
		** This also bounds the skip-unknown-verbs contract above: a
		** future verb's payload must be <= PKT_MAX to be skippable. */
		if (plen > PKT_MAX)
		{
			dch_trace("bad frame type=%u len=%u plen=%zu -> drop fd=%d",
			          type, len, plen, p->fd);
			remove_client(p);
			return;
		}
		if (p->inlen - off < PKT_HDR + plen)
			break; /* rest of this frame hasn't arrived yet */

		if (handle_packet(p, type, len, f + PKT_HDR) < 0)
			return; /* client removed; p is gone */
		off += PKT_HDR + plen;
	}

	/* Shift any partial trailing frame back to the front. */
	if (off)
	{
		memmove(p->inbuf, p->inbuf + off, p->inlen - off);
		p->inlen -= off;
	}
}


/* Rebuild the session from `resume` instead of starting one. The pty, the
** child, the listen socket and every attached client's socket are the SAME
** descriptors the previous image had — execvp() kept them open and kept our
** pid, so the child is still our child and waitpid()/SIGCHLD still work. */
/* A descriptor number out of the resume blob is only a number: check it really
** is a connected stream socket and not the pty, the listener, or a repeat of a
** client we already took. Adopting the pty here would let the master write
** protocol frames into the child's terminal input and later close it. */
static int
adoptable_client_fd(int fd, const int *taken, int ntaken)
{
	struct stat st;
	socklen_t tl = sizeof(int);
	int type, i;

	if (fd <= 2 || fd == resume.pfd || fd == resume.lfd)
		return 0;
	for (i = 0; i < ntaken; i++)
		if (taken[i] == fd)
			return 0;
	if (fstat(fd, &st) < 0 || !S_ISSOCK(st.st_mode))
		return 0;
	if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &tl) < 0 ||
	    type != SOCK_STREAM)
		return 0;
	return 1;
}

static void
master_adopt(void)
{
	int taken[RESUME_MAX_CLI + 1], ntaken = 0;
	char *blob = NULL;
	size_t bloblen = 0;
	int i;

	the_pty.fd = resume.pfd;
#ifdef BROKEN_MASTER
	/* The slave fd number is not carried in the resume blob. Say "none"
	** rather than leave a zero that killpty() would ioctl against stdin. */
	the_pty.slave = -1;
#endif
	the_pty.pid = resume.cpid;
	the_pty.ws = resume.ws;
	pty_gated = resume.gate;
	redraw_method = resume.redraw;
	memcpy(dec_on, resume.dec, sizeof dec_on);
	memcpy(kbd_u, resume.ku, resume.kulen);
	kbd_u_len = resume.kulen;
	memcpy(kbd_m, resume.km, resume.kmlen);
	kbd_m_len = resume.kmlen;
	if (tcgetattr(the_pty.fd, &the_pty.term) < 0)
		memset(&the_pty.term, 0, sizeof the_pty.term);

	/* Re-seed the mirror at the size we left at, by feeding it the screen
	** we rendered before the exec. Scrollback does not come across. */
	if (!getenv("DCH_NO_VT"))
	{
		char *sb = getenv("DCH_SCROLLBACK");
		int scrollback = sb ? atoi(sb) : 2000;
		int cols = the_pty.ws.ws_col ? the_pty.ws.ws_col : 80;
		int rows = the_pty.ws.ws_row ? the_pty.ws.ws_row : 24;

		if (scrollback < 0)
			scrollback = 0;
		if (scrollback > 100000)
			scrollback = 100000;
		if (dch_vt_init(cols, rows, scrollback) < 0)
			dch_trace("vt mirror unavailable after restart");
		else if (resume.snap)
			dch_vt_feed(resume.snap, resume.snaplen);
	}
	free(resume.snap);
	resume.snap = NULL;
	resume.snaplen = 0;

	/* One repaint for every carried client. Same OUT_CAP guard queue_replay
	** applies: an oversized dump would cost each client its connection,
	** which is worse than a stale screen. */
	if (!no_replay && vt_restore_blob(&blob, &bloblen) == 0 &&
	    bloblen > OUT_CAP / 2)
	{
		dch_trace("restart: snapshot %zu over cap, skipping", bloblen);
		free(blob);
		blob = NULL;
	}

	for (i = 0; i < resume.ncfd; i++)
	{
		struct client *p;

		if (!adoptable_client_fd(resume.cfd[i], taken, ntaken))
		{
			dch_trace("resume: rejecting client fd=%d",
			          resume.cfd[i]);
			continue;
		}
		taken[ntaken++] = resume.cfd[i];
		p = link_client(resume.cfd[i]);
		if (!p)
			continue;
		p->attached = 1;
		p->role = ROLE_ATTACHED;
		/* Half-read frame from before the exec. Without it the next
		** read() lands mid-packet and the stream desyncs for good. */
		if (resume.cinlen[i])
		{
			memcpy(p->inbuf, resume.cin[i], resume.cinlen[i]);
			p->inlen = resume.cinlen[i];
		}
		/* Whatever the old image had queued died with it, and a
		** diff-based app repaints nothing on its own. Same repaint
		** attach uses, for the same reason — but rendered once for
		** everyone: nothing between iterations can change the mirror,
		** and one render is a full-grid ANSI dump. */
		p->want_replay = 0;
		if (blob && (queue_to_client(p, (unsigned char *)blob,
		                             bloblen) < 0 ||
		             flush_client(p) < 0))
			remove_client(p);
	}
	free(blob);

	/* The requester is still waiting on its MSG_RESTART. Answering from
	** HERE — after the new image is fully wired up — is what makes the ACK
	** mean "the new binary is serving", not "the exec was attempted". */
	if (adoptable_client_fd(resume.ackfd, taken, ntaken))
	{
		struct client *p = link_client(resume.ackfd);

		if (p)
		{
			p->role = ROLE_CONTROL;
			/* Queued, not flushed — deliberately. The select loop
			** flushes it, and write_version_sidecar() runs before
			** that loop starts, so the requester can never observe
			** the ACK before the new version is on disk. Its
			** post-ACK version check depends on that ordering. */
			queue_status(p, MSG_ACK, DCH_ST_OK);
		}
	}
	dch_trace("master resumed pid=%d clients=%d", (int)getpid(),
	          resume.ncfd);
}

/* The master process - It watches over the pty process and the attached */
/* clients. */
static void
master_process(int s, char **argv, int waitattach, int statusfd)
{
	struct client *p, *next;
	fd_set readfds, writefds;
	int highest_fd;
	int nullfd;

	int has_attached_client = 0;

	/* Okay, disassociate ourselves from the original terminal, as we
	** don't care what happens to it. */
	setsid();

	/* Resolve the trace sink now (before any signal handler can call
	** dch_trace), so die() only ever fprintf()s, never fopen()s. */
	dch_trace("master start pid=%d waitattach=%d", (int)getpid(), waitattach);

	listen_fd = s;
	no_replay = getenv("DCH_NO_REPLAY") != NULL;

	/* Set a trap to unlink the socket when we die. */
	atexit(unlink_socket);

	signal(SIGCHLD, die);

	if (resume.valid)
		master_adopt();
	else
	{
		pty_gated = waitattach;

		/* Create a pty in which the process is running. */
		if (init_pty(argv, statusfd) < 0)
		{
			if (statusfd != -1)
				dup2(statusfd, 1);
			if (errno == ENOENT)
				printf("%s: Could not find a pty.\n", progname);
			else
				printf("%s: init_pty: %s\n", progname,
				       strerror(errno));
			exit(1);
		}

		/* Start the terminal mirror at the pty's initial 80x24;
		** MSG_WINCH / MSG_REDRAW keep it in sync. Failure (or
		** DCH_NO_VT) just leaves --read/--wait/--keys unavailable;
		** the session is unaffected. */
		if (!getenv("DCH_NO_VT"))
		{
			char *sb = getenv("DCH_SCROLLBACK");
			int scrollback = sb ? atoi(sb) : 2000;

			if (scrollback < 0)
				scrollback = 0;
			if (scrollback > 100000)
				scrollback = 100000;
			if (dch_vt_init(80, 24, scrollback) < 0)
				dch_trace("vt mirror unavailable");
		}
	}

	/* Stamp the version LAST, once this image is actually serving: a client
	** that reads the sidecar is asking "what is running", not "what was
	** launched". Rewritten on every start, including a resume. */
	write_version_sidecar();

	/* Set up some signals. */
	signal(SIGPIPE, SIG_IGN);
	signal(SIGXFSZ, SIG_IGN);
	signal(SIGHUP, SIG_IGN);
	signal(SIGTTIN, SIG_IGN);
	signal(SIGTTOU, SIG_IGN);
	signal(SIGINT, die);
	signal(SIGTERM, die);

	/* Close statusfd, since we don't need it anymore. */
	if (statusfd != -1)
		close(statusfd);

	/* Make sure stdin/stdout/stderr point to /dev/null. We are now a
	** daemon. */
	nullfd = open("/dev/null", O_RDWR);
	if (nullfd >= 0)
	{
		dup2(nullfd, 0);
		dup2(nullfd, 1);
		dup2(nullfd, 2);
		if (nullfd > 2)
			close(nullfd);
	}

	/* Loop forever. */
	while (1)
	{
		int new_has_attached_client = 0;
		int have_writes = 0;

		/* Re-initialize the file descriptor set for select. */
		FD_ZERO(&readfds);
		FD_ZERO(&writefds);
		FD_SET(s, &readfds);
		highest_fd = s;

		/*
		** When waitattach is set, wait until the client attaches
		** before trying to read from the pty.
		*/
		/*
		** When waitattach is set, don't read the pty until a client has
		** attached. Flip the gate AND arm the pty in the same iteration:
		** the pty may already hold output (inner printed before attach), so
		** waiting another loop to arm it would block in select() with no
		** follow-up wake (our reassembly drains ATTACH+REDRAW in one read).
		*/
		if (pty_gated)
		{
			/* Scan the FULL list: a control client connecting first
			** would otherwise hide an attached one behind it (clients
			** are head-inserted). handle_packet also clears the gate
			** directly on any control verb. */
			for (p = clients; p; p = p->next)
				if (p->attached)
				{
					pty_gated = 0;
					break;
				}
		}
		if (!pty_gated)
		{
			FD_SET(the_pty.fd, &readfds);
			if (the_pty.fd > highest_fd)
				highest_fd = the_pty.fd;
		}

		for (p = clients; p; p = p->next)
		{
			FD_SET(p->fd, &readfds);
			if (p->fd > highest_fd)
				highest_fd = p->fd;

			/* Watch for writability only while output is queued, so a
			** backed-up client wakes us when it can take more. */
			if (p->outoff < p->outlen)
			{
				FD_SET(p->fd, &writefds);
				have_writes = 1;
			}

			if (p->attached)
				new_has_attached_client = 1;
		}

		/* chmod the socket if necessary. */
		if (has_attached_client != new_has_attached_client)
		{
			update_socket_modes(new_has_attached_client);
			has_attached_client = new_has_attached_client;
		}

		/* Wait for something to happen. */
		if (select(highest_fd + 1, &readfds,
		           have_writes ? &writefds : NULL, NULL, NULL) < 0)
		{
			if (errno == EINTR || errno == EAGAIN)
				continue;
			exit(1);
		}

		dch_trace("mloop wake wa=%d ctl=%d pty=%d", pty_gated,
		          FD_ISSET(s, &readfds), FD_ISSET(the_pty.fd, &readfds));
		/* New client? */
		if (FD_ISSET(s, &readfds))
			control_activity(s);
		/* Activity on a client? */
		for (p = clients; p; p = next)
		{
			next = p->next;
			if (FD_ISSET(p->fd, &readfds))
				client_activity(p);
		}
		/* pty activity? Read it and queue to attached clients. */
		if (FD_ISSET(the_pty.fd, &readfds))
			pty_activity();
		/* Flush queued output. Attempt every client with a backlog (the
		** write() just EAGAINs if its socket isn't ready); drop on a hard
		** error so one dead client can't wedge the master. */
		for (p = clients; p; p = next)
		{
			next = p->next;
			if (p->outoff < p->outlen && flush_client(p) < 0)
				remove_client(p);
		}
	}
}

int
master_main(char **argv, int waitattach, int dontfork)
{
	int fd[2] = {-1, -1};
	int s;
	pid_t pid;

	/* Use a default redraw method if one hasn't been specified yet.
	** WINCH, not CTRL_L: modern TUIs bind ^L to actions (Claude Code
	** >=2.1.94 maps it to clear-input — a stray ^L on every attach showed
	** "press ctrl+l again to clear" and could wipe the session). */
	if (redraw_method == REDRAW_UNSPEC)
		redraw_method = REDRAW_WINCH;

	/* A re-exec from MSG_RESTART: the socket is already bound and listening
	** on a carried fd. Re-binding it would fail (or worse, unlink a live
	** session's socket), and we must not fork: our pid IS the session's,
	** and the child on the pty is ours only as long as we stay us. */
	{
		int was_resume = resume_is_ours(getenv("DCH_RESUME"));

		restart_load();
		if (resume.valid)
		{
#if defined(F_SETFD) && defined(FD_CLOEXEC)
			/* Cleared before the exec so the fd would survive it;
			** put it back, or the session's own children inherit
			** the listen socket. */
			fcntl(resume.lfd, F_SETFD, FD_CLOEXEC);
#endif
			master_process(resume.lfd, argv, waitattach, -1);
			return 0;
		}
		/* Asked to resume and couldn't. Do NOT fall through to the cold
		** start: the previous image's socket is still on disk and still
		** bound to descriptors we are holding but can no longer name, so
		** create_socket() would fail with EADDRINUSE and, worse, a
		** success would mean two masters on one session. Nothing here
		** can save the session — say so and get out of the way. */
		if (was_resume)
		{
			dch_trace("resume blob unusable; session cannot be "
			          "rebuilt");
			/* atexit(unlink_socket) is registered inside
			** master_process, which we never reach — so clean up
			** by hand or the session's socket outlives it and
			** `dch -ls` keeps advertising a session nobody can
			** connect to. */
			unlink_socket();
			return 1;
		}
	}

	/* Create the unix domain socket. */
	s = create_socket(sockname);
	if (s < 0 && errno == ENAMETOOLONG)
	{
		char *slash = strrchr(sockname, '/');

		/* Try to shorten the socket's path name by using chdir. */
		if (slash)
		{
			int dirfd = open(".", O_RDONLY);

			if (dirfd >= 0)
			{
				*slash = '\0';
				if (chdir(sockname) >= 0)
				{
					s = create_socket(slash + 1);
					if (s >= 0 && fchdir(dirfd) < 0)
					{
						close(s);
						s = -1;
					}
				}
				*slash = '/';
				close(dirfd);
			}
		}
	}
	if (s < 0)
	{
		printf("%s: %s: %s\n", progname, sockname, strerror(errno));
		return 1;
	}

#if defined(F_SETFD) && defined(FD_CLOEXEC)
	fcntl(s, F_SETFD, FD_CLOEXEC);

	/* If FD_CLOEXEC works, create a pipe and use it to report any errors
	** that occur while trying to execute the program. */
	if (dontfork)
	{
		fd[1] = dup(2);
		if (fcntl(fd[1], F_SETFD, FD_CLOEXEC) < 0)
		{
			close(fd[1]);
			fd[1] = -1;
		}
	}
	else if (pipe(fd) >= 0)
	{
		if (fcntl(fd[0], F_SETFD, FD_CLOEXEC) < 0 ||
		    fcntl(fd[1], F_SETFD, FD_CLOEXEC) < 0)
		{
			close(fd[0]);
			close(fd[1]);
			fd[0] = fd[1] = -1;
		}
	}
#endif

	if (dontfork)
	{
		master_process(s, argv, waitattach, fd[1]);
		return 0;
	}

	/* Fork off so we can daemonize and such */
	pid = fork();
	if (pid < 0)
	{
		printf("%s: fork: %s\n", progname, strerror(errno));
		unlink_socket();
		return 1;
	}
	else if (pid == 0)
	{
		/* Child - this becomes the master */
		if (fd[0] != -1)
			close(fd[0]);
		master_process(s, argv, waitattach, fd[1]);
		return 0;
	}
	/* Parent - just return. */

#if defined(F_SETFD) && defined(FD_CLOEXEC)
	/* Check if an error occurred while trying to execute the program. */
	if (fd[0] != -1)
	{
		char buf[1024];
		ssize_t len;

		close(fd[1]);
		len = read(fd[0], buf, sizeof(buf));
		if (len > 0)
		{
			do
			{
				write_buf_or_fail(2, buf, len);
				len = read(fd[0], buf, sizeof(buf));
			} while (len > 0);

			kill(pid, SIGTERM);
			return 1;
		}
		close(fd[0]);
	}
#endif
	close(s);
	return 0;
}

/* BSDish functions for systems that don't have them. */
#ifndef HAVE_OPENPTY
#define HAVE_OPENPTY
/* openpty: Use /dev/ptmx and Unix98 if we have it. */
#if defined(HAVE_PTSNAME) && defined(HAVE_GRANTPT) && defined(HAVE_UNLOCKPT)
int
openpty(int *amaster, int *aslave, char *name, struct termios *termp,
	struct winsize *winp)
{
	int master, slave;
	char *buf;

#ifdef _AIX
	master = open("/dev/ptc", O_RDWR|O_NOCTTY);
	if (master < 0)
		return -1;
	buf = ttyname(master);
	if (!buf)
		return -1;

	slave = open(buf, O_RDWR|O_NOCTTY);
	if (slave < 0)
		return -1;
#else
	master = open("/dev/ptmx", O_RDWR);
	if (master < 0)
		return -1;
	if (grantpt(master) < 0)
		return -1;
	if (unlockpt(master) < 0)
		return -1;
	buf = ptsname(master);
	if (!buf)
		return -1;

	slave = open(buf, O_RDWR|O_NOCTTY);
	if (slave < 0)
		return -1;

#ifdef I_PUSH
	if (ioctl(slave, I_PUSH, "ptem") < 0)
		return -1;
	if (ioctl(slave, I_PUSH, "ldterm") < 0)
		return -1;
#endif
#endif

	*amaster = master;
	*aslave = slave;
	if (name)
		strcpy(name, buf);
	if (termp)
		tcsetattr(slave, TCSAFLUSH, termp);
	if (winp)
		ioctl(slave, TIOCSWINSZ, winp);
	return 0;
}
#else
#error Do not know how to define openpty.
#endif
#endif

#ifndef HAVE_FORKPTY
#if defined(HAVE_OPENPTY)
pid_t
forkpty(int *amaster, char *name, struct termios *termp,
	struct winsize *winp)
{
	pid_t pid;
	int master, slave;

	if (openpty(&master, &slave, name, termp, winp) < 0)
		return -1;
	*amaster = master;

	/* Fork off... */
	pid = fork();
	if (pid < 0)
		return -1;
	else if (pid == 0)
	{
		char *buf;
		int fd;

		setsid();
#ifdef TIOCSCTTY
		buf = NULL;
		if (ioctl(slave, TIOCSCTTY, NULL) < 0)
			_exit(1);
#elif defined(_AIX)
		fd = open("/dev/tty", O_RDWR|O_NOCTTY);
		if (fd >= 0)
		{
			ioctl(fd, TIOCNOTTY, NULL);
			close(fd);
		}

		buf = ttyname(master);
		fd = open(buf, O_RDWR);
		close(fd);

		fd = open("/dev/tty", O_WRONLY);
		if (fd < 0)
			_exit(1);
		close(fd);

		if (termp && tcsetattr(slave, TCSAFLUSH, termp) == -1)
			_exit(1);
		if (ioctl(slave, TIOCSWINSZ, winp) == -1)
			_exit(1);
#else
		buf = ptsname(master);
		fd = open(buf, O_RDWR);
		close(fd);
#endif
		dup2(slave, 0);
		dup2(slave, 1);
		dup2(slave, 2);

		if (slave > 2)
			close(slave);
		close(master);
		return 0;
	}
	else
	{
		close(slave);
		return pid;
	}
}
#else
#error Do not know how to define forkpty.
#endif
#endif
