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

	/* Input reassembly (client->master). The socket is nonblocking and
	** frames are variable length, so a read may split one frame or carry
	** several; buffer raw bytes and parse complete frames out. */
	unsigned char inbuf[PKT_HDR + PKT_MAX];
	size_t inlen;

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

/* Unlink the socket (and its activity sidecar). */
static void
unlink_socket(void)
{
	char act[1100];
	int n = snprintf(act, sizeof act, "%s.act", sockname);

	unlink(sockname);
	if (n > 0 && (size_t)n < sizeof act)
		unlink(act);
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
	/* Use the original terminal's settings. We don't have to set the
	** window size here, because the attacher will send it in a packet. */
	the_pty.term = orig_term;
	memset(&the_pty.ws, 0, sizeof(struct winsize));

	/* Create the pty process */
	if (!dont_have_tty)
		the_pty.pid = forkpty(&the_pty.fd, NULL, &the_pty.term, NULL);
	else
		the_pty.pid = forkpty(&the_pty.fd, NULL, NULL, NULL);
	if (the_pty.pid < 0)
		return -1;
	else if (the_pty.pid == 0)
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

/* Best-effort short write to a (nonblocking) client fd. Used for the tiny
** kitty re-arm string; a partial/failed write just means that client misses
** the re-arm, which the next redraw or keypress will not corrupt. */
static void
write_client_seq(struct client *p, const unsigned char *buf, size_t len)
{
	size_t off = 0;

	while (off < len)
	{
		ssize_t n = write(p->fd, buf + off, len - off);

		if (n > 0)
			off += (size_t)n;
		else if (n < 0 && errno == EINTR)
			continue;
		else
			break;
	}
}

/* Unlink a client from the list and free it (socket + output queue). */
static void
remove_client(struct client *p)
{
	dch_trace("remove client fd=%d attached=%d outlen=%zu", p->fd,
	          p->attached, p->outlen);
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

/* Process activity on the pty - Input and terminal changes are queued out to
** the attached clients (flushed by the main loop). If the pty goes away, we
** die. */
static void
pty_activity(void)
{
	unsigned char buf[BUFSIZE];
	ssize_t len;
	struct client *p, *next;

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
	track_keymode(buf, (size_t)len);
	track_decmode(buf, (size_t)len);

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
		    queue_to_client(p, buf, (size_t)len) < 0 ||
		    flush_client(p) < 0)
			remove_client(p);
	}
}

/* Process activity on the control socket */
static void
control_activity(int s)
{
	int fd;
	struct client *p;

	/* Accept the new client and link it in. */
	fd = accept(s, NULL, NULL);
	if (fd < 0)
		return;
	else if (setnonblocking(fd) < 0)
	{
		close(fd);
		return;
	}

	/* Link it in. calloc fail → drop client; existing sessions stay live.
	** calloc zeroes the reassembly + output-queue state (inlen/out/...). */
	p = calloc(1, sizeof(struct client));
	if (!p)
	{
		close(fd);
		return;
	}
	p->fd = fd;
	p->attached = 0;
	p->pprev = &clients;
	p->next = *(p->pprev);
	if (p->next)
		p->next->pprev = &p->next;
	*(p->pprev) = p;
}

/* Act on one fully-reassembled frame. `len` is the header length field
** (PUSH = payload bytes, REDRAW = method); `payload` points at the frame's
** payload bytes (len for PUSH, a struct winsize for WINCH/REDRAW). */
static void
handle_packet(struct client *p, unsigned int type, unsigned int len,
              const unsigned char *payload)
{
	/* Push out data to the program. */
	if (type == MSG_PUSH)
	{
		write_buf_or_fail(the_pty.fd, payload, len);
	}

	/* Attach or detach from the program. */
	else if (type == MSG_ATTACH)
	{
		p->attached = 1;
		dch_trace("attach client fd=%d", p->fd);
		/* Re-arm the terminal modes the child enabled for this client; a
		** previous detach reset them, and the child only sets them once at
		** startup. No-op for anything the child never enabled. */
		if (kbd_u_len)
			write_client_seq(p, kbd_u, kbd_u_len);
		if (kbd_m_len)
			write_client_seq(p, kbd_m, kbd_m_len);
		{
			int k;
			for (k = 0; k < N_DEC_MODES; k++)
				if (dec_on[k])
				{
					unsigned char s[16];
					int sl = snprintf((char *)s, sizeof s,
					    "\033[?%dh", dec_modes[k]);
					if (sl > 0 && (size_t)sl <= sizeof(s))
						write_client_seq(p, s, (size_t)sl);
				}
		}
	}
	else if (type == MSG_DETACH)
		p->attached = 0;

	/* Window size change request, without a forced redraw. */
	else if (type == MSG_WINCH)
	{
		memcpy(&the_pty.ws, payload, sizeof(struct winsize));
		ioctl(the_pty.fd, TIOCSWINSZ, &the_pty.ws);
	}

	/* Force a redraw using a particular method. */
	else if (type == MSG_REDRAW)
	{
		int method = len;

		/* If the client didn't specify a particular method, use
		** whatever we had on startup. */
		if (method == REDRAW_UNSPEC)
			method = redraw_method;
		if (method == REDRAW_NONE)
			return;

		/* Set the window size. */
		memcpy(&the_pty.ws, payload, sizeof(struct winsize));
		ioctl(the_pty.fd, TIOCSWINSZ, &the_pty.ws);

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
		size_t plen = (type == MSG_PUSH) ? len
		    : (type == MSG_WINCH || type == MSG_REDRAW)
		        ? sizeof(struct winsize)
		    : 0;

		/* A PUSH length past PKT_MAX is not one of ours -> corrupt. */
		if (plen > PKT_MAX)
		{
			dch_trace("bad frame type=%u len=%u plen=%zu -> drop fd=%d",
			          type, len, plen, p->fd);
			remove_client(p);
			return;
		}
		if (p->inlen - off < PKT_HDR + plen)
			break; /* rest of this frame hasn't arrived yet */

		handle_packet(p, type, len, f + PKT_HDR);
		off += PKT_HDR + plen;
	}

	/* Shift any partial trailing frame back to the front. */
	if (off)
	{
		memmove(p->inbuf, p->inbuf + off, p->inlen - off);
		p->inlen -= off;
	}
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

	/* Set a trap to unlink the socket when we die. */
	atexit(unlink_socket);

	/* Create a pty in which the process is running. */
	signal(SIGCHLD, die);
	if (init_pty(argv, statusfd) < 0)
	{
		if (statusfd != -1)
			dup2(statusfd, 1);
		if (errno == ENOENT)
			printf("%s: Could not find a pty.\n", progname);
		else
			printf("%s: init_pty: %s\n", progname, strerror(errno));
		exit(1);
	}

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
		if (waitattach && clients && clients->attached)
			waitattach = 0;
		if (!waitattach)
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

		dch_trace("mloop wake wa=%d ctl=%d pty=%d", waitattach,
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

	/* Use a default redraw method if one hasn't been specified yet. */
	if (redraw_method == REDRAW_UNSPEC)
		redraw_method = REDRAW_CTRL_L;

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
