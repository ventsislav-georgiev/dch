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
** still in raw mode. Pop alt-screen LAST so the user's pre-attach view is
** what stays on screen.
*/
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
	** that attaches, so reattach keeps the inner app's modes. */
	printf("\033[?25h"
	       "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"
	       "\033[?1004l"
	       "\033[?2004l\033[?7h"
	       "\033[<u"
	       "\033[>4;0m"
	       "\033[?1049l");
	fflush(stdout);
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
		close(s);

		/* ECONNREFUSED is also returned for regular files, so make
		** sure we are trying to connect to a socket. */
		if (errno == ECONNREFUSED)
		{
			struct stat st;

			if (stat(name, &st) < 0)
				return -1;
			else if (!S_ISSOCK(st.st_mode) || S_ISREG(st.st_mode))
				errno = ENOTSOCK;
		}
		return -1;
	}
	return s;
}

/* Signal — silent exit; restore_term handles cleanup. Upstream printed
** "[detached]"/"[got signal N - dying]" banners; we suppress them so the
** parent shell prompt reappears clean. */
static RETSIGTYPE
die(ATTRIBUTE_UNUSED int sig)
{
	exit(0);
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

/* Handles input from the keyboard. Scans the WHOLE buffer for the detach
** char (upstream only checked buf[0], which loses the keypress if it
** arrives bundled with other bytes — common when the terminal sends a
** burst, or when the user types fast after pasted input). */
static void
process_kbd(int s, struct packet *pkt)
{
	int i;

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
		/* Tell the master that we are suspending. */
		pkt->type = MSG_DETACH;
		write_packet_or_fail(s, pkt);

		/* And suspend... */
		tcsetattr(0, TCSADRAIN, &orig_term);
		kill(getpid(), SIGTSTP);
		tcsetattr(0, TCSADRAIN, &cur_term);

		/* Tell the master that we are returning. */
		pkt->type = MSG_ATTACH;
		write_packet_or_fail(s, pkt);

		/* We would like a redraw, too. */
		pkt->type = MSG_REDRAW;
		pkt->len = redraw_method;
		ioctl(0, TIOCGWINSZ, &pkt->u.ws);
		write_packet_or_fail(s, pkt);
		return;
	}
	/* Detach char anywhere in the buffer? */
	if (detach_char != -1)
	{
		for (i = 0; i < pkt->len; i++)
		{
			if (pkt->u.buf[i] == detach_char)
				exit(0);
		}
	}
	/* Kitty keyboard protocol: when the inner program turns on progressive
	** keyboard enhancement (it emits ESC[>...u — Claude Code, recent nvim,
	** etc. do this), the terminal stops sending the detach char as a raw
	** control byte and instead reports it as a CSI escape:
	**     CSI <codepoint> ; <modifiers> u
	** where <codepoint> is the BASE key, not the control code (Ctrl-\ is
	** reported as '\' = 0x5c, NOT 0x1c). Without matching this form the raw
	** scan above never fires and detach silently stops working inside such
	** apps. Recover the base codepoint by undoing the control mask
	** (Ctrl+X == X & 0x1f, so base == detach_char | 0x40) and look for that
	** CSI ... u sequence anywhere in the buffer. */
	if (detach_char >= 0 && detach_char < 0x20)
	{
		char want[8];
		int wlen = snprintf(want, sizeof want, "\033[%d;",
			detach_char | 0x40);

		for (i = 0; i + wlen <= pkt->len; i++)
		{
			int j;

			if (memcmp(pkt->u.buf + i, want, wlen) != 0)
				continue;
			/* Matched "ESC [ <cp> ;" — consume the modifier/event-type
			** field ([0-9;:] run) and require a terminating 'u'. */
			for (j = i + wlen; j < pkt->len; j++)
			{
				unsigned char b = pkt->u.buf[j];

				if (b == 'u')
					exit(0);
				if (!((b >= '0' && b <= '9') || b == ';' || b == ':'))
					break;
			}
		}
	}
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

	/* Push it out */
	write_packet_or_fail(s, pkt);
}

int
attach_main(int noerror)
{
	struct packet pkt;
	unsigned char buf[BUFSIZE];
	fd_set readfds;
	int s;

	/* Attempt to open the socket. Don't display an error if noerror is
	** set. */
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
		if (!noerror)
			printf("%s: %s: %s\n", progname, sockname,
			       strerror(errno));
		return 1;
	}

	/* The current terminal settings are equal to the original terminal
	** settings at this point. */
	cur_term = orig_term;

	/* Set a trap to restore the terminal when we die. */
	atexit(restore_term);

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

	/* Enter alt-screen + home cursor. On detach, restore_term pops back
	** to primary screen so the user's pre-attach scrollback is intact. */
	write_buf_or_fail(1, "\033[?1049h\033[H\033[J", 11);

	/* Tell the master that we want to attach. */
	memset(&pkt, 0, sizeof(struct packet));
	pkt.type = MSG_ATTACH;
	write_packet_or_fail(s, &pkt);

	/* We would like a redraw, too. */
	pkt.type = MSG_REDRAW;
	pkt.len = redraw_method;
	ioctl(0, TIOCGWINSZ, &pkt.u.ws);
	write_packet_or_fail(s, &pkt);
	dch_trace("client attached fd=%d sock=%s", s, sockname);

	/* Wait for things to happen */
	while (1)
	{
		int n;

		/* SIGUSR1 detach — main-loop check avoids exit() inside the
		** signal handler. */
		if (want_detach)
			exit(0);

		FD_ZERO(&readfds);
		FD_SET(0, &readfds);
		FD_SET(s, &readfds);
		n = select(s + 1, &readfds, NULL, NULL, NULL);
		if (n < 0 && errno != EINTR && errno != EAGAIN)
			exit(1);

		/* Pty activity */
		if (n > 0 && FD_ISSET(s, &readfds))
		{
			ssize_t len = read(s, buf, sizeof(buf));

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
			/* Send the data to the terminal. */
			write_buf_or_fail(1, buf, len);
			n--;
		}
		/* stdin activity */
		if (n > 0 && FD_ISSET(0, &readfds))
		{
			ssize_t len;

			pkt.type = MSG_PUSH;
			len = read(0, pkt.u.buf, sizeof(pkt.u.buf));

			if (len <= 0)
				exit(1);

			pkt.len = len;
			process_kbd(s, &pkt);
			n--;
		}

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
push_main()
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
