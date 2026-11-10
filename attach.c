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
#include <strings.h>

#ifndef VDISABLE
#ifdef _POSIX_VDISABLE
#define VDISABLE _POSIX_VDISABLE
#else
#define VDISABLE 0377
#endif
#endif

/*
** The current terminal settings. After coming back from a suspend, we
** restore this.
*/
static struct termios cur_term;
/* 1 if the window size changed */
static int win_changed;
/* SIGUSR1 → clean detach. Set by handler, checked in main loop. */
static volatile sig_atomic_t want_detach;
/* SIGUSR2 → force a redraw (MSG_REDRAW). Set by handler, checked in main loop. */
static volatile sig_atomic_t want_redraw;

/*
** Restore terminal + reset modes inner program may have left on (mouse
** tracking, bracketed paste, alt-screen, cursor hide, no-wrap). dtach itself
** never touches these, but vim/fzf/less inside the session do — without this
** the parent shell ends up with mouse codes printing on movement and stdin
** still in raw mode. Pop alt-screen LAST in case the child enabled it; inline
** programs remain on the primary screen so native terminal scrollback works.
*/
#define TERM_RESET "\033[?25h" \
	"\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l" \
	"\033[?1004l" \
	"\033[?2004l\033[?7h" \
	"\033[<8u" \
	"\033[>4;0m" \
	"\033[?1049l" \
	"\033[<8u"

static void
restore_term(void)
{
	tcsetattr(0, TCSADRAIN, &orig_term);

	/* \e[?25h cursor on; \e[?100[0236]l + \e[?101[56]l mouse tracking off;
	** \e[?1004l focus-event reporting off; \e[?2004l bracketed paste off;
	** \e[?7h auto-wrap on; \e[<u pop the kitty keyboard protocol; \e[>4;0m
	** turn off xterm modifyOtherKeys. TUIs (Claude Code etc.) enable these
	** and only reset them on a clean exit — on a detach the child keeps
	** running, so without this the bare shell the user returns to emits
	** CSI-u / `;N~` garbage for shifted keys (modifyOtherKeys) and `[I`/`[O`
	** on focus changes (1004). This mirrors Claude Code's own teardown
	** (App.tsx: DISABLE_MODIFY_OTHER_KEYS, DISABLE_KITTY_KEYBOARD, focus
	** off, bracketed-paste off). \e[?1049l leave alt-screen and restore
	** primary. The master replays whatever the child set to the next client
	** that attaches, so reattach keeps the inner app's modes.
	**
	** The kitty pop runs twice, once on each side of the alt-screen exit:
	** Ghostty (and kitty) keep one flag stack PER SCREEN and do not copy it
	** on 1049h/l. A push that landed on the primary screen (the master's
	** re-arm, an inline app) is invisible to a pop issued from the alt
	** screen, and vice versa — with a single pop the bare shell came back
	** with ctrl-d typing `0;5u`. \e[<8u rather than \e[<u: Ghostty's stack
	** is 8 deep and a pop of >= 8 empties it outright, so nested pushes
	** (a TUI launched from inside the session) can't leave a level behind. */
	fputs(TERM_RESET, stdout);
	fflush(stdout);
}

int attach_sock = -1;
const char *(*attach_lost)(int *code);

/* A remote attach's socket closed: when the link (not the session) ended,
** say why on a restored terminal and exit with the hook's code. */
static void
lost(void)
{
	int code = 1;
	const char *why = attach_lost(&code);

	if (why)
	{
		restore_term();
		fprintf(stderr, "%s\n", why);
		exit(code);
	}
}

/* Connects to a unix domain socket */
static int
connect_socket(char *name)
{
	int s;
	struct sockaddr_un sockun;

	if (strlen(name) > sizeof(sockun.sun_path) - 1)
	{
		errno = ENAMETOOLONG;
		return -1;
	}

	s = socket(PF_UNIX, SOCK_STREAM, 0);
	if (s < 0)
		return -1;
	sockun.sun_family = AF_UNIX;
	strcpy(sockun.sun_path, name);
	if (connect(s, (struct sockaddr *)&sockun, sizeof(sockun)) < 0)
	{
		int e = errno;	/* connect's: close may overwrite it */

		close(s);
		errno = e;
		/* ECONNREFUSED is also returned for regular files, so make
		** sure we are trying to connect to a socket. */
		if (e == ECONNREFUSED)
		{
			struct stat st;

			if (stat(name, &st) < 0)
				return -1;
			/* a successful stat may still change errno */
			errno = (!S_ISSOCK(st.st_mode) || S_ISREG(st.st_mode))
				? ENOTSOCK : e;
		}
		return -1;
	}
	return s;
}

/* Signal — silent exit. Upstream printed "[detached]"/"[got signal N -
** dying]" banners; we suppress them so the parent shell prompt reappears
** clean. Async-signal-safe, so no exit(): its atexit handlers (restore_term,
** client_pidfile_unlink, done here by hand) ran restore_term's stdio
** and a TCSADRAIN that blocks forever while nobody reads the terminal, as
** after a hangup (#029). A hung-up terminal gets no reset sequence; a live
** one gets it by write(), and its modes back at once (TCSANOW). */
static RETSIGTYPE
die(int sig)
{
	if (sig != SIGHUP)
		(void)!write(1, TERM_RESET, sizeof TERM_RESET - 1);
	tcsetattr(0, TCSANOW, &orig_term);
	client_pidfile_unlink();
	_exit(0);
}

/* SIGUSR1 → request clean detach. Used when Ctrl-\ is unreachable (some
** terminal hosts bind it). Send via `dch -d <name>` or `kill -USR1 <pid>`. */
static RETSIGTYPE
on_sigusr1(ATTRIBUTE_UNUSED int sig)
{
	signal(SIGUSR1, on_sigusr1);
	want_detach = 1;
}

/* Window size change. */
static RETSIGTYPE
win_change(ATTRIBUTE_UNUSED int sig)
{
	signal(SIGWINCH, win_change);
	win_changed = 1;
}

/* SIGUSR2 → force a full redraw of the inner program without reattaching.
** Sends MSG_REDRAW(REDRAW_WINCH) so the master raises SIGWINCH at the program
** unconditionally (even when the size is unchanged), making it repaint. Used by
** hosts (DchTerm) to recover after a local relayout — e.g. the soft keyboard
** showing/hiding — where no byte stream change would otherwise repaint. */
static RETSIGTYPE
on_sigusr2(ATTRIBUTE_UNUSED int sig)
{
	signal(SIGUSR2, on_sigusr2);
	want_redraw = 1;
}

/* Counts detach-key presses in a keyboard buffer. Scans the WHOLE buffer
** (upstream only checked buf[0], which loses the keypress if it arrives
** bundled with other bytes — common when the terminal sends a burst, or when
** the user types fast after pasted input).
**
** Two forms are counted:
**   - the raw control byte (Ctrl-\ == 0x1c);
**   - the Kitty keyboard protocol report. When the inner program turns on
**     progressive keyboard enhancement (it emits ESC[>...u — Claude Code,
**     recent nvim, etc. do this), the terminal stops sending the detach char
**     as a control byte and reports it as
**         CSI <codepoint> ; <modifiers>[:<event>] [; <text>] u
**     where <codepoint> is the BASE key, not the control code (Ctrl-\ is
**     reported as '\' == 0x5c, NOT 0x1c). Recover the base codepoint by
**     undoing the control mask (Ctrl+X == X & 0x1f, so base ==
**     detach_char | 0x40). Only event type 1 (press) counts: once the inner
**     app asks for repeat (2) and release (3) events, counting those would
**     turn a single keypress into two hits. */
static int
detach_hits(struct packet *pkt)
{
	const unsigned char *p, *end = pkt->u.buf + pkt->len;
	int hits = 0;

	if (detach_char == -1)
		return 0;

	/* Two hits is all the caller can act on, and every scan below runs on
	** each keystroke — including multi-kilobyte pastes — so stop at two and
	** let memchr do the walking. */
	p = pkt->u.buf;
	while (hits < 2 && (p = memchr(p, detach_char, end - p)) != NULL)
	{
		hits++;
		p++;
	}
	if (hits < 2 && detach_char < 0x20)
	{
		char want[8];
		int wlen = snprintf(want, sizeof want, "\033[%d;",
			detach_char | 0x40);

		p = pkt->u.buf;
		while (hits < 2 && (p = memchr(p, '\033', end - p)) != NULL)
		{
			const unsigned char *q;
			int ev = 1, field = 0, closed = 0;

			if (end - p < wlen || memcmp(p, want, wlen) != 0)
			{
				p++;
				continue;
			}
			/* Sitting on the <modifiers> field now. */
			for (q = p + wlen; q < end; q++)
			{
				unsigned char b = *q;

				if (b == 'u')
				{
					closed = 1;
					break;
				}
				if (b == ':')
				{
					if (field == 0)
					{
						field = 1;	/* <event> */
						ev = 0;
					}
					continue;
				}
				if (b == ';')
				{
					field = 2;	/* trailing params */
					continue;
				}
				if (b < '0' || b > '9')
					break;
				if (field == 1)
					ev = ev < 1000 ? ev * 10 + (b - '0') : ev;
			}
			if (closed && ev == 1)
				hits++;
			p++;
		}
	}
	return hits;
}

/* How long a single detach press waits for a second one before it commits to
** its own action (detaching, unless detach_key = switch). Long enough for a
** comfortable double-tap, short enough that a plain detach still feels
** instant — but "comfortable" is a property of the person typing, so
** DCH_DOUBLE_TAP_MS overrides it. 0 disables the double press entirely and
** makes the single press act at once. */
#define DETACH_DOUBLE_MS	300

/* Monotonic "now", so an NTP step mid-window can neither fire the detach early
** nor strand it. Falls back to the wall clock only where CLOCK_MONOTONIC is
** absent. */
static void
now_us(struct timeval *tv)
{
#ifdef CLOCK_MONOTONIC
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0)
	{
		tv->tv_sec = ts.tv_sec;
		tv->tv_usec = ts.tv_nsec / 1000;
		return;
	}
#endif
	gettimeofday(tv, NULL);
}

static long
detach_double_us(void)
{
	static long us = -1;

	if (us < 0)
	{
		const char *e = getenv("DCH_DOUBLE_TAP_MS");
		char *tail;
		long ms = DETACH_DOUBLE_MS;

		if (e && *e)
		{
			long v = strtol(e, &tail, 10);

			if (*tail == '\0' && v >= 0 && v <= 5000)
				ms = v;
		}
		us = ms * 1000L;
	}
	return us;
}

/* Size follows focus (#100). Two clients can share one session (a terminal
** and an app pane); the one the user focuses should own the pty size. So the
** client turns on focus reporting (DEC 1004) on its own terminal, and a
** focus-in (ESC [ I) re-sends this window's size as a MSG_WINCH. The master
** sets it with TIOCSWINSZ, which compares against the pty's CURRENT size, so
** a size the other client changed is taken back even though this window's
** size never moved. The reports themselves reach the inner program only while
** it has 1004 on (Claude Code does); otherwise they are swallowed.
** DCH_FOCUS_RESIZE=0, or focus_resize = false in the config, turns all of it
** off: no 1004 from the client, nothing intercepted. */
static int focus_on;		/* this attach turned 1004 on */
static int inner_focus;		/* the inner program has 1004 on */
static int fo_st, fo_val, fo_hit;	/* focus_scan's parser state */
static unsigned char fpend[2];	/* a trailing ESC / ESC [ held back */
static int fpend_n;
static long long fpend_t;	/* when the held prefix came in (mono_us) */
static unsigned char dq[PKT_MAX];	/* keys a cut-off Esc's gap delays */
static int dq_n;
static long long dq_at;		/* when dq goes out */

/* ponytail: a held ESC waits this long for the rest of a split report — the
** lone Esc key pays it as latency (tmux's escape-time, same trade). Only a
** read that ENDS in ESC or ESC [ waits; a kitty-keyboard Esc (CSI 27 u) never
** does. 50 ms (#117): a loaded host or an ssh hop can space a report's halves
** well past the 5 ms #107 had, and the report then leaked in as typed text.
** A key typed inside the hold no longer needs a short one: the Esc goes out
** at once, the key after the gap it had (focus_filter), so the program never
** reads the two as one Meta-key. 50 ms is the hold before #107, whose only
** complaint was that merge. */
#define FOCUS_HOLD_MS	50

static long long
mono_us(void)
{
	struct timeval tv;

	now_us(&tv);
	return tv.tv_sec * 1000000LL + tv.tv_usec;
}

static int
focus_wanted(void)
{
	const char *e = getenv("DCH_FOCUS_RESIZE");
	char v[16];

	/* the env var takes the config's off words too (#107) */
	if (!(e && *e) && dch_config_get("focus_resize", v, sizeof v))
		e = v;
	return !(e && (!strcasecmp(e, "false") || !strcmp(e, "0") ||
	               !strcasecmp(e, "no") || !strcasecmp(e, "off")));
}

/* Track the inner program's own 1004 in the session's output, a byte at a
** time so a sequence split across reads still counts: CSI ? Pm h/l with 1004
** anywhere in Pm, and RIS (ESC c), which resets it. 1 when the outer
** terminal's 1004 was just turned off by it and has to be turned back on. */
static int
focus_scan(const unsigned char *b, ssize_t n)
{
	ssize_t i;
	int rearm = 0;

	for (i = 0; i < n; i++)
	{
		unsigned char c = b[i];

		if (fo_st == 1)		/* ESC */
		{
			fo_st = c == '[' ? 2 : 0;
			if (c == 'c')
			{
				inner_focus = 0;
				rearm = 1;
			}
		}
		else if (fo_st == 2)	/* ESC [ */
		{
			fo_st = c == '?' ? 3 : 0;
			fo_val = fo_hit = 0;
		}
		else if (fo_st == 3)	/* ESC [ ? Pm */
		{
			if (c >= '0' && c <= '9')
			{
				fo_val = fo_val < 100000 ? fo_val * 10 + (c - '0')
				                         : fo_val;
				continue;
			}
			fo_hit |= fo_val == 1004;
			fo_val = 0;
			if (c == ';')
				continue;
			if ((c == 'h' || c == 'l') && fo_hit)
			{
				inner_focus = c == 'h';
				rearm |= c == 'l';
			}
			fo_st = 0;
		}
		if (fo_st == 0 && c == '\033')
			fo_st = 1;
	}
	return rearm;
}

/* Take the outer terminal's focus reports out of a keyboard read of len
** bytes in b (room for 2 more): put back what the last read held, note a
** focus-in in *in, keep a report only while the inner program wants them,
** and hold a trailing ESC or ESC [ that may begin one (unless flush). Every
** other byte stays, in order. The new length: what goes out now; what has to
** wait is moved to dq. */
static int
focus_filter(unsigned char *b, int len, int *in, int flush)
{
	long long now = mono_us(), gap = now - fpend_t;
	int i, o = 0, cut = 0;

	if (fpend_n)
	{
		/* A held lone ESC the read does not continue with '[' was the
		** Esc key: it goes out now as its own packet, and what follows
		** it the gap it had later, so the program reads the two apart,
		** not as Meta-<key> (#107, #117). A held ESC [ goes out with
		** the byte after it, as today: a CSI the link split (an arrow
		** key over ssh) stays one sequence. */
		cut = fpend_n == 1 && len && b[0] != '[';
		memmove(b + fpend_n, b, len);
		memcpy(b, fpend, fpend_n);
		len += fpend_n;
		fpend_n = 0;
	}
	for (i = 0; i < len; i++)
	{
		if (b[i] == '\033' && i + 2 < len && b[i + 1] == '[' &&
		    (b[i + 2] == 'I' || b[i + 2] == 'O'))
		{
			*in |= b[i + 2] == 'I';
			if (inner_focus)
			{
				b[o++] = b[i];
				b[o++] = b[i + 1];
				b[o++] = b[i + 2];
			}
			i += 2;
			continue;
		}
		if (!flush && b[i] == '\033' &&
		    (i + 1 == len || (i + 2 == len && b[i + 1] == '[')))
		{
			fpend_n = len - i;
			memcpy(fpend, b + i, fpend_n);
			fpend_t = now;
			break;
		}
		b[o++] = b[i];
	}
	/* The delay line: past the cut, or all of it behind keys already
	** waiting (order first). shortcut: what joins a waiting dq goes out
	** in one packet with it, so Esc + key typed again inside that gap
	** can merge; a per-chunk queue if anyone types that fast. */
	if (dq_n)
		cut = 0;
	else if (!cut)
		return o;
	if (o > cut)
	{
		if (!dq_n)
			dq_at = now + gap;
		memcpy(dq + dq_n, b + cut, o - cut);
		dq_n += o - cut;
		o = cut;
	}
	return o;
}

/* process_kbd return values. */
#define KBD_OK		0
#define KBD_DETACH	1	/* one detach-key press */
#define KBD_SWITCH	2	/* two presses (detach_key_done says what each does) */

/* What the detach key's presses mean: one detaches and two open the picker,
** or with detach_key = switch (#082), read once per attach, the other way
** round. Detaching exits; the picker returns ATTACH_SWITCH to the caller,
** the same way from either. */
static int detach_swap;

static int
detach_key_done(int s, int twice)
{
	if (twice == detach_swap)
		exit(0);
	dch_trace("client switch requested");
	restore_term();
	close(s);
	return ATTACH_SWITCH;
}

/* Handles input from the keyboard. Returns one of KBD_*; on anything but
** KBD_OK the buffer is swallowed rather than forwarded. */
static int
process_kbd(int s, struct packet *pkt)
{
	int i, hits;

	/* Suspend? Scan whole buffer — VMIN=1 usually delivers VSUSP as buf[0],
	** but bracketed/pasted input or fast typing can bundle it with other
	** bytes. Same class of bug as the detach_char scan below. */
	int suspend_hit = 0;
	if (!no_suspend && cur_term.c_cc[VSUSP] != VDISABLE)
	{
		for (i = 0; i < pkt->len; i++)
		{
			if (pkt->u.buf[i] == cur_term.c_cc[VSUSP])
			{
				suspend_hit = 1;
				break;
			}
		}
	}
	if (suspend_hit)
	{
		/* Tell the master that we are suspending. pkt is the PUSH
		** buffer and its len still holds the last keystroke count.
		** Neither DETACH nor ATTACH carries a payload, and masters now
		** read len as the payload size for types they do not know, so
		** leave nothing behind for one to misread. */
		pkt->type = MSG_DETACH;
		pkt->len = 0;
		write_packet_or_fail(s, pkt);

		/* And suspend... with our focus reporting off meanwhile, so
		** the shell does not read [I/[O on focus changes. */
		if (focus_on)
			write_buf_or_fail(1, "\033[?1004l", 8);
		tcsetattr(0, TCSADRAIN, &orig_term);
		kill(getpid(), SIGTSTP);
		tcsetattr(0, TCSADRAIN, &cur_term);
		if (focus_on)
			write_buf_or_fail(1, "\033[?1004h", 8);

		/* Tell the master that we are returning. */
		pkt->type = MSG_ATTACH;
		pkt->len = 0;
		write_packet_or_fail(s, pkt);

		/* We would like a redraw, too. Ask for WINCH explicitly so even
		** an older master (whose UNSPEC default was CTRL_L) never types
		** a stray ^L into the program. */
		pkt->type = MSG_REDRAW;
		pkt->len = redraw_method == REDRAW_UNSPEC ? REDRAW_WINCH : redraw_method;
		ioctl(0, TIOCGWINSZ, &pkt->u.ws);
		write_packet_or_fail(s, pkt);
		return KBD_OK;
	}
	/* Detach key. Two presses in one burst (key repeat, or a fast
	** double-tap that arrives in a single read) mean "switch session". */
	hits = detach_hits(pkt);
	if (hits >= 2)
		return KBD_SWITCH;
	if (hits == 1)
		return KBD_DETACH;

	/* Force-redraw bookkeeping: if user pressed Ctrl-L, master will echo
	** it back and the pty will redraw — but mark the window as changed so
	** our next iteration also nudges WINSZ in case the terminal resized
	** silently. */
	for (i = 0; i < pkt->len; i++)
	{
		if (pkt->u.buf[i] == '\f')
		{
			win_changed = 1;
			break;
		}
	}

	/* Push it out. */
	write_packet_or_fail(s, pkt);
	return KBD_OK;
}

int
attach_main(int noerror)
{
	struct packet pkt;
	unsigned char buf[BUFSIZE];
	fd_set readfds;
	int s;
	int detach_pending = 0;
	struct timeval detach_at = {0, 0};

	/* Attempt to open the socket. Don't display an error if noerror is
	** set. */
	s = attach_sock >= 0 ? attach_sock : connect_socket(sockname);
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
					s = connect_socket(slash + 1);
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
		if (!noerror)
			printf("%s: %s: %s\n", progname, sockname,
			       strerror(errno));
		return 1;
	}

	/* The current terminal settings are equal to the original terminal
	** settings at this point. */
	cur_term = orig_term;
	{
		char v[16];

		detach_swap = dch_config_get("detach_key", v, sizeof v) &&
		              !strcmp(v, "switch");
	}

	/* Set a trap to restore the terminal when we die. Once only: a session
	** switch re-enters attach_main in the same process, and atexit slots
	** are a finite resource. */
	{
		static int trapped;

		if (!trapped)
		{
			trapped = 1;
			atexit(restore_term);
		}
	}

	/* Set some signals. */
	signal(SIGPIPE, SIG_IGN);
	signal(SIGXFSZ, SIG_IGN);
	signal(SIGHUP, die);
	signal(SIGTERM, die);
	signal(SIGINT, die);
	signal(SIGQUIT, die);
	signal(SIGWINCH, win_change);
	signal(SIGUSR1, on_sigusr1);
	signal(SIGUSR2, on_sigusr2);

	/* Set raw mode. */
	cur_term.c_iflag &= ~(IGNBRK|BRKINT|PARMRK|ISTRIP|INLCR|IGNCR|ICRNL);
	cur_term.c_iflag &= ~(IXON|IXOFF);
	cur_term.c_oflag &= ~(OPOST);
	cur_term.c_lflag &= ~(ECHO|ECHONL|ICANON|ISIG|IEXTEN);
	cur_term.c_cflag &= ~(CSIZE|PARENB);
	cur_term.c_cflag |= CS8;
	cur_term.c_cc[VLNEXT] = VDISABLE;
	cur_term.c_cc[VMIN] = 1;
	cur_term.c_cc[VTIME] = 0;
	tcsetattr(0, TCSADRAIN, &cur_term);

	/* Focus reporting on, before the attach: the master's mode re-arm
	** (ESC[?1004h when the inner program has it) arrives in the replay
	** and focus_scan picks it up from there, so it starts off. */
	focus_on = focus_wanted();
	inner_focus = fo_st = fpend_n = dq_n = 0;
	if (focus_on)
		write_buf_or_fail(1, "\033[?1004h", 8);

	/* Tell the master that we want to attach. len asks for that many
	** rows of scrollback above the replayed screen (#101); the frame has
	** no payload whatever len says, and a master before it ignores len. */
	memset(&pkt, 0, sizeof(struct packet));
	pkt.type = MSG_ATTACH;
	pkt.len = (unsigned short)dch_replay_scrollback();
	write_packet_or_fail(s, &pkt);

	/* We would like a redraw, too. WINCH when unspecified — see the
	** resume path above; a ^L redraw double-types into modern TUIs. */
	pkt.type = MSG_REDRAW;
	pkt.len = redraw_method == REDRAW_UNSPEC ? REDRAW_WINCH : redraw_method;
	ioctl(0, TIOCGWINSZ, &pkt.u.ws);
	write_packet_or_fail(s, &pkt);
	dch_trace("client attached fd=%d sock=%s", s,
	          attach_sock >= 0 ? "(remote)" : sockname);

	/* Wait for things to happen */
	while (1)
	{
		int n, in, flush = 0, dq_due = 0;
		long wait_us = -1;
		struct timeval tv, *tvp = NULL;

		/* SIGUSR1 detach — main-loop check avoids exit() inside the
		** signal handler. */
		if (want_detach)
			exit(0);

		/* One detach press seen: hold it for a beat so a second press
		** can turn it into a session switch instead. A real deadline,
		** not a select timeout — a chatty session would otherwise keep
		** waking us and the press would never land. */
		if (detach_pending)
		{
			struct timeval now;
			long us;

			now_us(&now);
			us = (detach_at.tv_sec - now.tv_sec) * 1000000L
			   + (detach_at.tv_usec - now.tv_usec);
			if (us <= 0)	/* window expired — one press */
				return detach_key_done(s, 0);
			wait_us = us;
		}
		/* A held ESC prefix: wake when it is due to go out as typed;
		** delayed keys: wake when their gap is up. Timers, so pty
		** output never waits on them. */
		if (fpend_n || dq_n)
		{
			long long now = mono_us(), us = LLONG_MAX, d;

			if (fpend_n)
			{
				d = fpend_t + FOCUS_HOLD_MS * 1000LL - now;
				flush = d <= 0;
				us = d;
			}
			if (dq_n)
			{
				d = dq_at - now;
				dq_due = d <= 0;
				us = d < us ? d : us;
			}
			if (us < 0)
				us = 0;
			if (wait_us < 0 || us < wait_us)
				wait_us = us;
		}
		if (wait_us >= 0)
		{
			tv.tv_sec = wait_us / 1000000;
			tv.tv_usec = wait_us % 1000000;
			tvp = &tv;
		}

		FD_ZERO(&readfds);
		/* a full delay line reads no more keys until it goes out */
		if (dq_n <= (int)sizeof(dq) - 3)
			FD_SET(0, &readfds);
		FD_SET(s, &readfds);
		n = select(s + 1, &readfds, NULL, NULL, tvp);
		if (n < 0 && errno != EINTR && errno != EAGAIN)
			exit(1);

		/* Pty activity */
		if (n > 0 && FD_ISSET(s, &readfds))
		{
			ssize_t len = read(s, buf, sizeof(buf));

			if (len <= 0 && attach_lost)
				lost();
			if (len == 0)
			{
				dch_trace("client exit: master closed socket");
				exit(0);
			}
			else if (len < 0)
			{
				dch_trace("client exit: read(s) errno=%d", errno);
				exit(1);
			}
			/* Send the data to the terminal. The inner program
			** turning its 1004 off turns ours off too: back on. */
			write_buf_or_fail(1, buf, len);
			if (focus_on && focus_scan(buf, len))
				write_buf_or_fail(1, "\033[?1004h", 8);
			n--;
		}
		/* stdin activity, or a held ESC prefix that nothing completed
		** (flush: it was due before this select, and no byte came), or
		** delayed keys whose time came (first: they are older; stdin
		** stays readable for the next turn). */
		in = n > 0 && FD_ISSET(0, &readfds) && !dq_due;
		if (in || (flush && fpend_n) || dq_due)
		{
			ssize_t len = 0;
			int focus_in = 0;

			pkt.type = MSG_PUSH;
			if (dq_due)
			{
				memcpy(pkt.u.buf, dq, dq_n);
				len = dq_n;
				dq_n = 0;
			}
			else
			{
				if (in)
				{
					/* room for a held prefix, and for dq */
					len = read(0, pkt.u.buf, sizeof(pkt.u.buf) -
					           (focus_on ? 2 + dq_n : 0));
					if (len <= 0)
						exit(1);
					n--;
				}
				if (focus_on)
				{
					len = focus_filter(pkt.u.buf, len, &focus_in,
					                   !in);
					win_changed |= focus_in;
					/* all of it a report, held or delayed: no
					** keystroke now, so the detach window stays
					** as it was */
					if (len == 0)
						goto keys_done;
				}
			}

			pkt.len = len;
			int k = process_kbd(s, &pkt);
			long w = detach_double_us();

			/* Two presses — one burst, or one inside the window the
			** first press opened. */
			if (k == KBD_SWITCH || (k == KBD_DETACH && detach_pending))
				return detach_key_done(s, 1);
			if (k == KBD_DETACH)
			{
				if (w == 0)	/* no double press: one, at once */
					return detach_key_done(s, 0);
				detach_pending = 1;
				now_us(&detach_at);
				detach_at.tv_usec += w;
				detach_at.tv_sec += detach_at.tv_usec / 1000000;
				detach_at.tv_usec %= 1000000;
			}
			/* Anything else ends the window: the first press stands
			** on its own. */
			else if (detach_pending)
				return detach_key_done(s, 0);
		}
keys_done:

		/* Window size changed? */
		if (win_changed)
		{
			win_changed = 0;

			pkt.type = MSG_WINCH;
			ioctl(0, TIOCGWINSZ, &pkt.u.ws);
			write_packet_or_fail(s, &pkt);
		}

		/* Redraw requested (SIGUSR2)? Force the program to repaint via an
		** unconditional WINCH at the master, regardless of size change. */
		if (want_redraw)
		{
			want_redraw = 0;

			pkt.type = MSG_REDRAW;
			pkt.len = REDRAW_WINCH;
			ioctl(0, TIOCGWINSZ, &pkt.u.ws);
			write_packet_or_fail(s, &pkt);
		}
	}
	return 0;
}

int
push_main(void)
{
	struct packet pkt;
	int s;

	/* Attempt to open the socket. */
	s = connect_socket(sockname);
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
					s = connect_socket(slash + 1);
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

	/* Set some signals. */
	signal(SIGPIPE, SIG_IGN);

	/* Push the contents of standard input to the socket. */
	pkt.type = MSG_PUSH;
	for (;;)
	{
		ssize_t len;

		len = read(0, pkt.u.buf, sizeof(pkt.u.buf));

		if (len == 0)
			return 0;
		else if (len < 0)
		{
			printf("%s: %s: %s\n", progname, sockname,
			       strerror(errno));
			return 1;
		}

		pkt.len = len;
		write_packet_or_fail(s, &pkt);
	}
}
