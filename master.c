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
#include <dirent.h>
#include <limits.h>
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

/* How long the master's select() waits before running a self-heal pass, in
** milliseconds. Hourly is plenty for what it repairs: macOS's daily
** /usr/libexec/tmp_cleaner sweep. DCH_HEAL_MS overrides it (resolved once at
** master start) so the test suite doesn't have to sleep an hour. */
static long heal_ms = 3600L * 1000L;

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
** child-owned too: forcing it for every session turns terminal wheel events
** into cursor keys for inline apps such as Codex. */
static const int dec_modes[] = {
	1000, 1002, 1003, 1004, 1006, 1015, 1049, 2004,
};
#define N_DEC_MODES ((int)(sizeof(dec_modes) / sizeof(dec_modes[0])))
static unsigned char dec_on[N_DEC_MODES];

/* Respawn (MSG_RESPAWN) state; see the respawn section below. A master with
** no child ("held") is the_pty.fd < 0. */
static int respawn_armed;		/* consumed by the next child exit */
static char **respawn_argv;		/* argv to start then; NULL = hold */
static struct timespec respawn_at;	/* when armed (TTL) / when held */
/* ponytail: 5 min arm TTL, 2 min hold. Long enough for an orchestrator to
** make the child exit / read the exit tail; short enough that a crashed one
** leaves no trap and no childless zombie session. Env-overridable. */
static long respawn_ttl_ms = 300L * 1000L;	/* DCH_RESPAWN_TTL_MS */
static long hold_ms = 120L * 1000L;		/* DCH_HOLD_MS */
/* Raw bytes the child wrote from the arm until it was reaped: #022 reads the
** `claude --resume <id>` line from here, never from the mirror, which is full
** of older look-alikes. Last EXIT_TAIL_MAX bytes only. */
#define EXIT_TAIL_MAX 8192
static unsigned char exit_tail[EXIT_TAIL_MAX];
static size_t exit_tail_len;
static int exit_code = -1, exit_signal;	/* how the last reaped child ended */
/* argv of the current (or, while held, the last) child, for MSG_CMD. Starts
** as the master's own argv; after a respawn it is a malloc'd block. */
static char **cur_argv;
static int cur_argv_owned, dch_argv_owned;
/* dch_argv[0..argv_prefix) is "<exe> --master-of <sock> --": the part a
** respawn keeps when it rewrites the inner command for the next live
** restart. 0 = could not tell, so a restart reverts to the launch argv. */
static int argv_prefix;
static int respawn_live(void);

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

/* Device+inode of the socket node THIS master bound, so unlink_socket() can
** tell "still mine" from "a replacement master took the name" before
** deleting anything. Recorded by record_sock_identity() at every point this
** master (re)binds sockname: the cold-start and heal re-binds inside
** bind_socket(), and the live-restart resume path in master_process (the fd
** rode across the exec, so the node on disk is still the one we bound
** before). Stays 0 only when we die() before ever binding -- unlink_socket()
** then falls back to the unconditional pre-#005 behavior, so an early-exit
** path still cleans up after itself. */
static dev_t sock_dev;
static ino_t sock_ino;
static int sock_ino_valid;

/* Stat sockname and remember it as ours. Best-effort: if the stat fails
** right after we just bound/adopted the node, we simply don't get to verify
** ownership later -- unlink_socket() treats that the same as "never
** recorded" (see above), which is the same behavior this file shipped with
** before #005. */
static void
record_sock_identity(void)
{
	struct stat st;

	if (stat(sockname, &st) < 0)
		return;
	sock_dev = st.st_dev;
	sock_ino = st.st_ino;
	sock_ino_valid = 1;
}

/* Unlink the socket and every sidecar. NOT called across a restart: the
** re-exec keeps the socket (and the .resume blob the new image reads).
**
** Runs at exit (atexit) and from die(), by which point the node on disk may
** no longer be the one this master bound: if this master went invisible
** (its node vanished) and a replacement took the name before either of us
** could heal, deleting that node -- and the sidecars, which live at the same
** path -- would strand the winner until ITS OWN next heal tick. So verify
** ownership by device+inode first and only clean up what is provably ours. */
static void
unlink_socket(void)
{
	static const char *suffix[] = { ".act", ".state", ".ver", ".resume",
	                                ".meta", ".killed" };
	struct stat st;
	char side[1100];
	size_t i;
	int unlink_sock = 1;	/* no identity recorded: keep the old, unconditional behavior */

	if (sock_ino_valid)
	{
		if (stat(sockname, &st) == 0)
		{
			/* A node is there. Ours -> clean up below, same as
			** always. Someone else's -> a replacement master owns
			** the name (and, at this same path, its sidecars too):
			** touch nothing. */
			if (st.st_dev != sock_dev || st.st_ino != sock_ino)
				return;
		}
		else
			/* Gone (ENOENT), or an unexpected stat error: no
			** confirmed replacement, but nothing to unlink()
			** either way. The sidecars are still ours. */
			unlink_sock = 0;
	}

	if (unlink_sock)
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

/* ---- session history record (#042) ----------------------------------
**
** One JSON line per session in dch_history_dir()/<name>.json, replaced
** (tmp + rename) at start, every rec_ms and at exit, so a session that dies
** (out of memory, a full disk, a reboot) leaves what `dch --revive` needs:
** where it ran, what it ran, and the Claude Code session inside. Never the
** environment. The exit write runs from atexit, also out of die()'s signal
** handler, so it only write()s the buffers the last refresh built. */
static char rec_path[1200], rec_name[512], rec_cwd[PATH_MAX];
static char rec_dir[1024], rec_sdir[1024];	/* record dir, socket dir */
static char *rec_head;		/* {"name":…,"started":N, */
static char *rec_agent;		/* the agent object, or null */
static struct dch_agent rec_ag;	/* last agent seen; pid 0 = none */
static long rec_started;
static char rec_end[32];	/* how the master ended, set before exit */
static struct timespec rec_at;	/* last refresh */
static long rec_ms = 60L * 1000L;	/* DCH_RECORD_MS */

static void
rec_str(FILE *f, const char *s)
{
	size_t cap = strlen(s) * 6 + 3;
	char *q = malloc(cap);

	fputs(q && dch_json_quote(q, cap, s) != (size_t)-1 ? q : "\"\"", f);
	free(q);
}

/* v as a JSON array, v[0] written as v0 when that is given. */
static void
rec_strv(FILE *f, char **v, const char *v0)
{
	int k;

	fputc('[', f);
	for (k = 0; v && v[k]; k++)
	{
		if (k)
			fputc(',', f);
		rec_str(f, k == 0 && v0 ? v0 : v[k]);
	}
	fputc(']', f);
}

static int
rec_put(int fd, const char *s)
{
	size_t n = strlen(s);

	return write(fd, s, n) == (ssize_t)n;
}

/* Replace the record with the last refresh plus updated/ended/end. */
static void
rec_write(const char *end)
{
	char tmp[1300], mid[200];
	time_t now = time(NULL);
	int fd, ok;

	if (!rec_head || !rec_agent ||
	    snprintf(tmp, sizeof tmp, "%s.%d.tmp", rec_path, (int)getpid()) >=
	        (int)sizeof tmp)
		return;
	snprintf(mid, sizeof mid, "\"updated\":%ld,\"ended\":%ld,\"end\":\"%s\","
	         "\"agent\":", (long)now, end[0] ? (long)now : 0L, end);
	fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
	/* another socket dir's master pruned our dir while it was empty */
	if (fd < 0 && errno == ENOENT && mkdir(rec_dir, 0700) == 0)
		fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
	if (fd < 0)
		return;
	ok = rec_put(fd, rec_head) && rec_put(fd, mid) &&
	     rec_put(fd, rec_agent) && rec_put(fd, "}\n");
	if (close(fd) != 0 || !ok || rename(tmp, rec_path) != 0)
		unlink(tmp);
}

/* Rebuild the record from the live session and write it. The agent sticks
** while its claude's session file is still there: a claude killed under a
** shell (out of memory) leaves it, so a revive still brings it back; one
** that quit cleanly removed it. */
static void
rec_refresh(void)
{
	struct dch_agent a;
	char alias[600] = "", ap[1100], *hb = NULL, *ab = NULL, *old;
	char exe[PATH_MAX];
	const char *path = getenv("PATH");
	size_t hl, al;
	FILE *f;
	int fd;

	if (!rec_path[0])
		return;
	if (the_pty.fd >= 0 && dch_agent_probe(the_pty.pid, &a) == 0)
	{
		free(rec_ag.launch);
		rec_ag = a;
	}
	else if (rec_ag.pid && dch_claude_gone(rec_ag.pid))
	{
		free(rec_ag.launch);
		memset(&rec_ag, 0, sizeof rec_ag);
	}
	if (sidecar_path(".alias", ap, sizeof ap) == 0 &&
	    (fd = open(ap, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)) >= 0)
	{
		ssize_t r = read(fd, alias, sizeof alias - 1);

		close(fd);
		alias[r > 0 ? r : 0] = '\0';
		alias[strcspn(alias, "\r\n")] = '\0';
	}
	if (!(f = open_memstream(&hb, &hl)))
		return;
	fputs("{\"name\":", f);
	rec_str(f, rec_name);
	fputs(",\"sock_dir\":", f);
	rec_str(f, rec_sdir);
	fputs(",\"alias\":", f);
	rec_str(f, alias);
	fputs(",\"cwd\":", f);
	rec_str(f, rec_cwd);
	/* A bare argv[0] as the path execvp() found it on (our PATH and
	** cwd are the child's), so a revive under a thinner PATH (launchd)
	** still finds it. Not realpath()ed: a symlink keeps its basename. */
	if (!cur_argv || !cur_argv[0] || strchr(cur_argv[0], '/') || !path ||
	    path_lookup(cur_argv[0], path, rec_cwd, exe, sizeof exe) < 0)
		exe[0] = '\0';
	fputs(",\"argv\":", f);
	rec_strv(f, cur_argv, exe[0] ? exe : NULL);
	fprintf(f, ",\"started\":%ld,", rec_started);
	if (fclose(f) != 0 || !(f = open_memstream(&ab, &al)))
	{
		free(hb);
		return;
	}
	if (!rec_ag.pid)
		fputs("null", f);
	else
	{
		fputs("{\"kind\":\"claude\",\"session_id\":", f);
		rec_str(f, rec_ag.sid);
		fputs(",\"name\":", f);
		rec_str(f, rec_ag.name);
		fputs(",\"cwd\":", f);
		rec_str(f, rec_ag.cwd);
		fputs(",\"launch\":", f);
		rec_strv(f, rec_ag.launch, NULL);
		fputc('}', f);
	}
	if (fclose(f) != 0)
	{
		free(hb);
		free(ab);
		return;
	}
	/* Swap in whole buffers: a signal landing mid-refresh must find the
	** previous pair intact for its exit write. */
	old = rec_head;
	rec_head = hb;
	free(old);
	old = rec_agent;
	rec_agent = ab;
	free(old);
	rec_write("");
}

/* At exit: how the session ended. `dch -k` leaves <sock>.killed before it
** signals us; else die() named the signal, else the child's exit status. */
static void
rec_exit(void)
{
	char kp[1100];

	if (!rec_path[0])
		return;
	if (sidecar_path(".killed", kp, sizeof kp) == 0 && access(kp, F_OK) == 0)
		snprintf(rec_end, sizeof rec_end, "killed");
	else if (!rec_end[0] && exit_signal > 0)
		snprintf(rec_end, sizeof rec_end, "signal %d", exit_signal);
	else if (!rec_end[0])
		snprintf(rec_end, sizeof rec_end, "exit %d",
		         exit_code >= 0 ? exit_code : 1);
	rec_write(rec_end);
}

/* Drop *.json idle for 30 days, and *.tmp a master killed mid-write left
** for a day, from one record dir. */
static void
rec_prune(int dfd)
{
	DIR *d = fdopendir(dfd);
	struct dirent *de;
	struct stat st;

	if (!d)
	{
		close(dfd);
		return;
	}
	while ((de = readdir(d)))
	{
		size_t n = strlen(de->d_name);
		long age = n > 5 && strcmp(de->d_name + n - 5, ".json") == 0 ?
		           30L * 86400L :
		           n > 4 && strcmp(de->d_name + n - 4, ".tmp") == 0 ?
		           86400L : 0;

		if (age &&
		    fstatat(dirfd(d), de->d_name, &st, AT_SYMLINK_NOFOLLOW) == 0 &&
		    S_ISREG(st.st_mode) && st.st_mtime < time(NULL) - age)
			unlinkat(dirfd(d), de->d_name, 0);
	}
	closedir(d);
}

/* Start the record: a cold start prunes records idle for 30 days, from
** every socket dir's subdir, and the subdirs that leaves empty, then drops
** a .killed marker a SIGKILLed master left behind; a live restart keeps
** the started time. */
static void
rec_init(int resumed)
{
	char root[1024], kp[1100], body[256];
	const char *base = strrchr(sockname, '/'), *e = getenv("DCH_SESSION");
	const char *off;
	size_t bl;
	long v;
	int fd;

	if (!base)
		return;		/* a relative socket path keeps no record */
	snprintf(rec_sdir, sizeof rec_sdir, "%.*s", (int)(base - sockname),
	         sockname);
	base++;
	bl = strlen(base);
	if (bl > 5 && strcmp(base + bl - 5, ".sock") == 0)
		bl -= 5;
	/* DCH_NO_HISTORY: no record, no prune, nothing under the state dir */
	off = getenv("DCH_NO_HISTORY");
	if (off && *off && strcmp(off, "0") != 0)
	{
		if (!resumed && sidecar_path(".killed", kp, sizeof kp) == 0)
			unlink(kp);
		return;
	}
	if (!resumed && dch_history_dir(root, sizeof root, NULL, 0) == 0 &&
	    (fd = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)) >= 0)
	{
		DIR *d = fdopendir(fd);
		struct dirent *de;
		int sub;

		while (d && (de = readdir(d)))
			if (de->d_name[0] != '.' &&
			    (sub = openat(dirfd(d), de->d_name,
			                  O_RDONLY | O_DIRECTORY | O_NOFOLLOW)) >= 0)
			{
				rec_prune(sub);
				unlinkat(dirfd(d), de->d_name, AT_REMOVEDIR);
			}
		if (d)
			closedir(d);
		else
			close(fd);
	}
	if (dch_history_dir(rec_dir, sizeof rec_dir, rec_sdir, 1) < 0 ||
	    !getcwd(rec_cwd, sizeof rec_cwd) ||
	    snprintf(rec_path, sizeof rec_path, "%s/%.*s.json", rec_dir, (int)bl,
	             base) >= (int)sizeof rec_path)
	{
		rec_path[0] = '\0';
		return;
	}
	snprintf(rec_name, sizeof rec_name, "%s", e && *e ? e : "");
	if (!rec_name[0])
		snprintf(rec_name, sizeof rec_name, "%.*s", (int)bl, base);
	e = getenv("DCH_RECORD_MS");
	if (e && atol(e) > 0)
		rec_ms = atol(e);
	rec_started = (long)time(NULL);
	if (resumed && (fd = open(rec_path, O_RDONLY | O_NOFOLLOW)) >= 0)
	{
		/* "started" sits in the head, well inside the first bytes */
		ssize_t r = read(fd, body, sizeof body - 1);
		char *p;

		close(fd);
		body[r > 0 ? r : 0] = '\0';
		if ((p = strstr(body, "\"started\":")) && (v = atol(p + 10)) > 0)
			rec_started = v;
	}
	else if (!resumed && sidecar_path(".killed", kp, sizeof kp) == 0)
		unlink(kp);
	rec_refresh();
	clock_gettime(CLOCK_MONOTONIC, &rec_at);
	atexit(rec_exit);
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
	snprintf(rec_end, sizeof rec_end, "signal %d", sig);
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
	/* Use the original terminal's settings, and the_pty.ws as it stands:
	** the cold start seeds it, a respawn inherits the live size. */
	int ep[2] = {-1, -1}, err = 0;
	ssize_t r;

	the_pty.term = orig_term;

	/* A spawning client waits on statusfd: give the child a close-on-exec
	** pipe, so a failed exec comes back as its errno and a good one as
	** EOF, whatever the timing. */
	if (statusfd != -1 &&
	    (pipe(ep) < 0 || fcntl(ep[0], F_SETFD, FD_CLOEXEC) < 0 ||
	     fcntl(ep[1], F_SETFD, FD_CLOEXEC) < 0))
		return -1;

	/* Create the pty process */
	if (!dont_have_tty)
		the_pty.pid = forkpty(&the_pty.fd, NULL, &the_pty.term,
		    &the_pty.ws);
	else
		the_pty.pid = forkpty(&the_pty.fd, NULL, NULL, &the_pty.ws);
	if (the_pty.pid < 0)
		return -1;
	dch_trace("init_pty child=%d fd=%d notty=%d", (int)the_pty.pid,
		the_pty.fd, dont_have_tty);
	if (the_pty.pid == 0)
	{
		struct client *p;
		sigset_t none;

		/* Child.. Execute the program. A respawned child is forked
		** while clients are connected; their sockets are left open
		** across exec on purpose (a live restart hands them to the
		** next image), so they must be closed here. */
		for (p = clients; p; p = p->next)
			close(p->fd);
		/* execve keeps SIG_IGN and the signal mask. A respawn forks
		** after master_process ignored these, and a child ignoring
		** SIGHUP would outlive `dch -k`. Reset them so every child,
		** cold or respawned, starts from the defaults. */
		signal(SIGPIPE, SIG_DFL);
		signal(SIGXFSZ, SIG_DFL);
		signal(SIGHUP, SIG_DFL);
		signal(SIGTTIN, SIG_DFL);
		signal(SIGTTOU, SIG_DFL);
		sigemptyset(&none);
		sigprocmask(SIG_SETMASK, &none, NULL);
		execvp(*argv, argv);

		/* Hand the errno to the master, or (a respawn: nobody waits)
		** say it on the pty. */
		err = errno;
		if (ep[1] != -1)
		{
			r = write(ep[1], &err, sizeof err);
			_exit(1);
		}
		printf(EOS "\r\n%s: could not execute %s: %s\r\n", progname,
		       *argv, strerror(err));
		fflush(stdout);
		_exit(1);
	}
	/* Parent.. Finish up and return */
	if (ep[0] != -1)
	{
		close(ep[1]);
		while ((r = read(ep[0], &err, sizeof err)) < 0 && errno == EINTR)
			;
		close(ep[0]);
		if (r == sizeof err)
		{
			/* the client prints "<session>: " before this */
			dprintf(statusfd, "%s: %s\n", *argv, strerror(err));
			exit(1);
		}
	}
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

/* Bind sockname, shortening the path via chdir when the full one overflows
** sun_path (104-108 bytes, far below PATH_MAX). Both callers depend on the
** invariant this restores before returning: sockname is a full path and the
** cwd is the one we came in with — only bind() ever sees the short name. */
static int
bind_socket(void)
{
	int s = create_socket(sockname);
	char *slash;
	int dirfd;

	if (s >= 0 || errno != ENAMETOOLONG)
	{
		/* sockname is untouched on this path -- still a full path. */
		if (s >= 0)
			record_sock_identity();
		return s;
	}

	slash = strrchr(sockname, '/');
	if (!slash)
		return -1;
	dirfd = open(".", O_RDONLY);
	if (dirfd < 0)
		return -1;
	*slash = '\0';
	if (chdir(sockname) >= 0)
	{
		s = create_socket(slash + 1);
		/* Restore the cwd whether or not the bind worked: a failed
		** heal re-bind must not leave the master in the socket dir, or
		** a respawned child would start there instead of the launch
		** cwd. Bound but stuck in the wrong cwd is worse than not
		** bound: every other path we use is relative to nothing. */
		if (fchdir(dirfd) < 0 && s >= 0)
		{
			close(s);
			s = -1;
		}
	}
	*slash = '/';
	close(dirfd);
	/* sockname is a full path again (restored above) and fchdir already
	** put the cwd back, so record_sock_identity()'s stat() sees exactly
	** what a caller would. */
	if (s >= 0)
		record_sock_identity();
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

/* Recreate the directory holding sockname, if a cleaner took it. macOS's
** /tmp sweep deletes emptied directories too, so losing the last session in
** /tmp/dch-<uid> can lose the directory a moment later. */
static void
ensure_sock_dir(void)
{
	char dir[1100];
	char *slash;
	struct stat st;

	if (strlen(sockname) >= sizeof dir)
		return;
	strcpy(dir, sockname);
	slash = strrchr(dir, '/');
	if (!slash || slash == dir)
		return;			/* no directory part, or "/" itself */
	*slash = '\0';
	if (stat(dir, &st) == 0)
		return;
	if (mkdir(dir, 0700) < 0)
		dch_trace("heal mkdir %s: %s", dir, strerror(errno));
}

/* Refresh atime (and, unavoidably, ctime) on the sidecars that are still
** there. mtime is deliberately left alone: <sockname>.act's mtime IS the
** last-activity signal --ls-json reports as activity_epoch, and bumping it
** would claim work that never happened. A cleaner needs atime AND mtime AND
** ctime all older than its threshold, so refreshing two of the three is
** enough to keep an idle session's files alive. Best-effort throughout: a
** sidecar that isn't there simply isn't touched. */
static void
touch_sidecars(void)
{
	static const char *suffix[] = { ".act", ".state", ".alias", ".meta" };
	struct timespec times[2];
	char path[1100];
	size_t i;

	times[0].tv_sec = times[1].tv_sec = 0;
	times[0].tv_nsec = UTIME_NOW;	/* atime := now */
	times[1].tv_nsec = UTIME_OMIT;	/* mtime := unchanged */

	for (i = 0; i < sizeof suffix / sizeof suffix[0]; i++)
		if (sidecar_path(suffix[i], path, sizeof path) == 0)
			utimensat(AT_FDCWD, path, times, 0);
}

/* One self-heal pass, run whenever the master's select() times out.
**
** A master whose socket node was unlinked keeps running and keeps serving
** whoever is already connected, but is invisible: --status says "no session"
** and an attach falls through the dead-socket path and starts a SECOND master
** on the same name, orphaning this one forever. So we re-bind.
**
** Everything here is best-effort and traced, never fatal — a session with a
** broken socket is still a session, and exiting would lose the work inside it.
** Returns a new listening fd (already stored in listen_fd) when the socket had
** to be re-bound, or -1 when the caller's fd still stands. */
static int
heal_tick(int s, int has_attached_client)
{
	struct stat st;
	char ver[1100];
	int ns = -1, rebind = 0;

	if (stat(sockname, &st) < 0)
	{
		if (errno != ENOENT)
			dch_trace("heal stat %s: %s", sockname,
			          strerror(errno));
		else
		{
			dch_trace("heal: %s vanished, re-binding", sockname);
			ensure_sock_dir();
			rebind = 1;
		}
	}
	else if (!S_ISSOCK(st.st_mode))
	{
		/* Something that is not a socket took the name. It cannot be
		** a session, so it is ours to clear out of the way. */
		dch_trace("heal: %s is not a socket, replacing", sockname);
		if (unlink(sockname) == 0)
			rebind = 1;
		else
			dch_trace("heal unlink: %s", strerror(errno));
	}
	/* A live socket node is left strictly alone — no stat-and-rebind, no
	** unlink. By the time we notice, that node may belong to a replacement
	** master, and taking it would strand a session that is doing fine. */

	if (rebind)
	{
		ns = bind_socket();
		if (ns < 0)
			dch_trace("heal bind %s: %s", sockname,
			          strerror(errno));
		else
		{
#if defined(F_SETFD) && defined(FD_CLOEXEC)
			/* Same deal as the cold start: the session's own
			** children must not inherit the listen socket. */
			fcntl(ns, F_SETFD, FD_CLOEXEC);
#endif
			close(s);
			listen_fd = ns;
			/* The fresh node comes back 0600; an attached session
			** advertises itself with S_IXUSR, so put that back. */
			update_socket_modes(has_attached_client);
			dch_trace("heal: re-bound %s fd=%d", sockname, ns);
		}
	}

	/* The sidecars are plain files, which is exactly what a /tmp cleaner
	** deletes. Re-stamp the one carrying real information (a missing .ver
	** makes --restart claim this master predates live restart) and keep
	** the survivors young. */
	if (sidecar_path(".ver", ver, sizeof ver) == 0 && access(ver, F_OK) != 0)
		write_version_sidecar();
	touch_sidecars();

	return ns;
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

	/* Armed: keep the raw tail of what the child says on its way out. */
	if (respawn_armed)
	{
		if (len >= EXIT_TAIL_MAX)
		{
			memcpy(exit_tail, buf + len - EXIT_TAIL_MAX, EXIT_TAIL_MAX);
			exit_tail_len = EXIT_TAIL_MAX;
		}
		else
		{
			if (exit_tail_len + len > EXIT_TAIL_MAX)
			{
				size_t drop = exit_tail_len + len - EXIT_TAIL_MAX;

				memmove(exit_tail, exit_tail + drop,
				        exit_tail_len - drop);
				exit_tail_len -= drop;
			}
			memcpy(exit_tail + exit_tail_len, buf, len);
			exit_tail_len += len;
		}
	}

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

	if (the_pty.fd < 0)
		return;		/* held: no child, nothing pending */
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
	/* Held (no child): dropped, like any other input. */
	if (the_pty.fd >= 0)
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
** The re-fed screen also lands on the mirror's PRIMARY buffer; tracked DEC
** mode 1049 restores the child's active buffer for attached terminals.
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

	/* A pending arm or a hold is not in the resume blob: the new image
	** would silently drop the orchestrator's respawn (or, held, have no
	** pty to adopt). Refuse; the caller retries once the respawn is done. */
	if (the_pty.fd < 0 || respawn_live())
		return queue_status(p, MSG_ACK, DCH_ST_BUSY);

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

/* Turn "arg0\0arg1\0...\0" into a NULL-terminated vector in ONE malloc block
** (pointers first, then the strings), so a single free() releases it. NULL on
** an empty argv[0], a payload not ending in NUL, or no memory. */
static char **
argv_from_wire(const unsigned char *buf, size_t len)
{
	size_t i, n = 0;
	char **v, *s;

	if (len == 0 || buf[0] == '\0' || buf[len - 1] != '\0')
		return NULL;
	for (i = 0; i < len; i++)
		if (buf[i] == '\0')
			n++;
	v = malloc((n + 1) * sizeof(char *) + len);
	if (!v)
		return NULL;
	s = (char *)(v + n + 1);
	memcpy(s, buf, len);
	for (i = 0; i < n; i++)
	{
		v[i] = s;
		s += strlen(s) + 1;
	}
	v[n] = NULL;
	return v;
}

static long
ms_since(const struct timespec *t)
{
	struct timespec now;

	clock_gettime(CLOCK_MONOTONIC, &now);
	return (long)(now.tv_sec - t->tv_sec) * 1000L +
	       (now.tv_nsec - t->tv_nsec) / 1000000L;
}

/* Drop an arm (cancel, TTL expiry): no respawn, and nothing captured for it. */
static void
respawn_disarm(void)
{
	free(respawn_argv);
	respawn_argv = NULL;
	respawn_armed = 0;
	exit_tail_len = 0;
}

/* Is an arm in force? Expires it first when it has outlived its TTL: an
** orchestrator that armed and then could not make the child exit must not
** leave a trap that a manual quit springs hours later. */
static int
respawn_live(void)
{
	if (respawn_armed && ms_since(&respawn_at) > respawn_ttl_ms)
	{
		dch_trace("respawn arm expired");
		respawn_disarm();
	}
	return respawn_armed;
}

/* Clear the dead child's terminal modes before the new one prints anything,
** on the mirror (so --read and attach replay describe the new child) and on
** attached terminals alike: kitty keyboard flags (popped on the alt screen
** and again on the primary), the alt screen itself (only if it is on —
** 1049l when off would still restore a saved cursor), every tracked
** input-reporting mode, DECCKM/keypad (the mirror encodes --keys arrows from
** them), cursor visibility and SGR. The screen contents stay: the old
** child's exit output (its resume line) remains readable above the new
** child's first frame. Routed through process_pty_buf, so dec_on/kbd_u are
** cleared by the same trackers that set them. */
static void
reset_child_modes(void)
{
	char seq[256];
	int n, k;

	n = snprintf(seq, sizeof seq, "\033[<99u");
	for (k = 0; k < N_DEC_MODES; k++)
		if (dec_modes[k] == 1049 && dec_on[k])
			n += snprintf(seq + n, sizeof seq - (size_t)n,
			              "\033[?1049l\033[<99u");
	for (k = 0; k < N_DEC_MODES; k++)
		if (dec_modes[k] != 1049)
			n += snprintf(seq + n, sizeof seq - (size_t)n,
			              "\033[?%dl", dec_modes[k]);
	n += snprintf(seq + n, sizeof seq - (size_t)n,
	              "\033[?1l\033>\033[?25h\033[0m%s",
	              kbd_m_len ? "\033[>4m" : "");
	process_pty_buf((const unsigned char *)seq, (size_t)n);
	kbd_m_len = 0;	/* the ESC[>4m reset was just "remembered" as a set */
}

/* Start v on a fresh pty in place of the child that went away. It gets the
** live window size (the_pty.ws, kept current while held), the master's
** environment and the master's cwd, which is the launch cwd (bind_socket
** always restores it). Takes ownership of v. A pty we cannot even create
** ends the session like an unarmed exit; an argv that fails to exec dies in
** the child and ends it the same way, since the arm that started it is
** already consumed — no respawn loop. */
static void
respawn_start(char **v)
{
	int n;

	if (init_pty(v, -1) < 0)
	{
		dch_trace("respawn: init_pty: %s", strerror(errno));
		exit(1);
	}
	dch_trace("respawn child=%d fd=%d", (int)the_pty.pid, the_pty.fd);
	reset_child_modes();

	if (cur_argv_owned)
		free(cur_argv);
	cur_argv = v;
	cur_argv_owned = 1;

	/* MSG_RESTART re-execs dch_argv and the new image takes its inner
	** command from there, so rewriting it is what carries the respawned
	** argv across a live restart (and into `ps`). */
	for (n = 0; v[n]; n++)
		;
	if (argv_prefix > 0)
	{
		char **nd = malloc((size_t)(argv_prefix + n + 1) *
		                   sizeof(char *));

		if (!nd)
			dch_trace("respawn: no memory to rewrite dch_argv");
		else
		{
			memcpy(nd, dch_argv, (size_t)argv_prefix * sizeof(char *));
			memcpy(nd + argv_prefix, v,
			       (size_t)(n + 1) * sizeof(char *));
			if (dch_argv_owned)
				free(dch_argv);
			dch_argv = nd;
			dch_argv_owned = 1;
		}
	}
}

/* The child was reaped while an arm was in force: keep the session. Close
** the old pty first — anything still holding its slave (a grandchild) gets
** EIO/hangup instead of holding up the respawn — then start the armed argv,
** or hold childless until one arrives (or hold_ms runs out). */
static void
respawn_consume(int status)
{
	char **v = respawn_argv;
	char meta[1100];

	/* Metadata describes the program that just went away. */
	if (sidecar_path(".meta", meta, sizeof meta) == 0)
		unlink(meta);
	exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
	exit_signal = WIFSIGNALED(status) ? WTERMSIG(status) : 0;
	respawn_argv = NULL;
	respawn_armed = 0;
	close(the_pty.fd);
#ifdef BROKEN_MASTER
	close(the_pty.slave);
	the_pty.slave = -1;
#endif
	the_pty.fd = -1;
	the_pty.pid = 0;
	dch_trace("respawn: child reaped code=%d sig=%d -> %s", exit_code,
	          exit_signal, v ? "respawn" : "hold");
	if (v)
		respawn_start(v);
	else
		clock_gettime(CLOCK_MONOTONIC, &respawn_at);
}

/* Serve MSG_RESPAWN: op 'a' + argv, 'h' (hold) or 'c' (cancel). */
static int
do_respawn(struct client *p, const unsigned char *req, unsigned int len)
{
	char **v = NULL;
	int op = len ? req[0] : 0;

	if (op == 'a')
	{
		v = argv_from_wire(req + 1, len - 1);
		if (!v)
			return queue_status(p, MSG_ACK, DCH_ST_ERR);
	}
	else if ((op != 'h' && op != 'c') || len != 1)
		return queue_status(p, MSG_ACK, DCH_ST_ERR);

	if (op == 'c')
	{
		respawn_disarm();
		if (the_pty.fd >= 0)
			return queue_status(p, MSG_ACK, DCH_ST_OK);
		/* Held: the child is already gone, so cancelling ends the
		** session the way the unarmed exit would have. Answer first. */
		dch_trace("respawn cancelled while held -> master exit");
		if (queue_status(p, MSG_ACK, DCH_ST_OK) == 0)
			flush_client(p);
		exit(exit_code >= 0 ? exit_code : 1);
	}

	if (the_pty.fd < 0)
	{
		/* Held: nothing left to wait for. 'h' again is a no-op. */
		if (v)
			respawn_start(v);
		return queue_status(p, MSG_ACK, DCH_ST_OK);
	}

	/* A new arm replaces an earlier one and starts afresh: TTL, exit
	** tail and exit status all describe THIS arm's child exit. */
	free(respawn_argv);
	respawn_argv = v;
	respawn_armed = 1;
	exit_tail_len = 0;
	exit_code = -1;
	exit_signal = 0;
	clock_gettime(CLOCK_MONOTONIC, &respawn_at);
	return queue_status(p, MSG_ACK, DCH_ST_OK);
}

/* Serve MSG_CMD: pid, state, last exit status, current argv, exit tail. */
static int
do_cmd(struct client *p)
{
	const char *state = the_pty.fd < 0 ? "held"
	                  : respawn_live() ? "armed" : "running";
	char head[128];
	unsigned char *buf;
	size_t total, off, l;
	int argc = 0, hl, rc = 0;

	for (total = 0; cur_argv && cur_argv[argc]; argc++)
		total += strlen(cur_argv[argc]) + 1;
	hl = snprintf(head, sizeof head, "%ld%c%s%c%d%c%d%c%d%c",
	              (long)the_pty.pid, 0, state, 0, exit_code, 0,
	              exit_signal, 0, argc, 0);
	if (hl < 0 || (size_t)hl >= sizeof head)
		return queue_status(p, MSG_READ_END, DCH_ST_ERR);
	total += (size_t)hl + exit_tail_len;
	buf = malloc(total);
	if (!buf)
		return queue_status(p, MSG_READ_END, DCH_ST_ERR);
	memcpy(buf, head, (size_t)hl);
	off = (size_t)hl;
	for (argc = 0; cur_argv && cur_argv[argc]; argc++)
	{
		l = strlen(cur_argv[argc]) + 1;
		memcpy(buf + off, cur_argv[argc], l);
		off += l;
	}
	memcpy(buf + off, exit_tail, exit_tail_len);
	for (off = 0; off < total && rc == 0; off += PKT_MAX)
	{
		l = total - off > PKT_MAX ? PKT_MAX : total - off;
		rc = queue_frame(p, MSG_READ_DATA, (unsigned int)l, buf + off, l);
	}
	free(buf);
	if (rc < 0)
		return -1;
	return queue_status(p, MSG_READ_END, DCH_ST_OK);
}

/* Process activity on the pty - Input and terminal changes are queued out to
** the attached clients (flushed by the main loop). If the pty goes away, we
** die — unless a respawn is armed. */
static void
pty_activity(void)
{
	unsigned char buf[BUFSIZE];
	struct pollfd pfd = { the_pty.fd, POLLIN, 0 };
	ssize_t len;

	/* A control verb this pass may have drain_pty()'d what select() saw:
	** read() on the blocking pty would then wedge the master until the
	** child writes again. */
	if (poll(&pfd, 1, 0) == 0)
		return;

	/* Read the pty activity */
	len = read(the_pty.fd, buf, sizeof(buf));
	dch_trace("pty read len=%zd", len);

	/* Error -> die */
	if (len <= 0)
	{
		int status;
		pid_t waited;

		dch_trace("pty eof len=%zd errno=%d -> master exit", len, errno);

		do
			waited = waitpid(the_pty.pid, &status, 0);
		while (waited < 0 && errno == EINTR);
		if (waited >= 0 && respawn_live())
		{
			respawn_consume(status);
			return;
		}
		/* for the history record's end (rec_exit) */
		exit_code = waited >= 0 && WIFEXITED(status) ? WEXITSTATUS(status)
		                                              : -1;
		exit_signal = waited >= 0 && WIFSIGNALED(status) ? WTERMSIG(status)
		                                                  : 0;
		if (waited >= 0)
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
	else if (type == MSG_KEYS || type == MSG_READ || type == MSG_WAIT ||
	         type == MSG_RESPAWN || type == MSG_CMD)
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
		** against current keyboard modes) instead of last wake's. An
		** armed RESPAWN needs the gate open too: the child's exit is
		** only seen by reading the pty / reaping it. */
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
		/* Held (no child): input has nowhere to go; drop it. */
		if (the_pty.fd >= 0)
			write_buf_or_fail(the_pty.fd, payload, len);
	}

	else if (type == MSG_KEYS)
		return do_keys(p, payload, len);
	else if (type == MSG_RESPAWN)
		return do_respawn(p, payload, len);
	else if (type == MSG_CMD)
		return do_cmd(p);
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
		int rearm = dch_vt_enabled() || !pty_gated;

		/* Someone showed up: open the --spawn pty gate and pull what the
		** child already printed into the mirror, exactly as a control
		** client does. Leaving it for the next select() wake to notice
		** is a race we lose on macOS, where arming the fd after the write
		** has landed does not make it readable — the output then sits in
		** the pty until the child writes again, and a session that prints
		** once and waits replays blank. With a mirror, drain before
		** p->attached so replay keeps its mode-before-screen ordering. The
		** first gated lite attach forwards startup output live, including its
		** modes, so it must not re-arm those same modes below. */
		pty_gated = 0;
		if (!rearm)
			p->attached = 1;
		drain_pty();

		p->attached = 1;
		dch_trace("attach client fd=%d", p->fd);

		/* Everything below goes through the client's ORDERED out-queue,
		** never a direct write(): the screen replay has to land after the
		** mode re-arms and before whatever the child prints next, and a
		** direct write on a nonblocking socket cannot promise that. */

		/* Re-arm the terminal modes the child enabled for this client; a
		** previous detach reset them, and the child only sets them once at
		** startup. No-op for anything the child never enabled. DEC modes
		** first, keyboard protocol after: Ghostty/kitty keep the kitty flag
		** stack per screen, so the push has to follow 1049h to land on the
		** alt screen the child actually reads keys from — pushed before it,
		** the flags sit on the primary screen, the child gets legacy keys
		** and the detaching client's pop misses them. */
		bad = flush_client(p) < 0;
		for (k = 0; rearm && !bad && k < N_DEC_MODES; k++)
			if (dec_on[k])
		{
			unsigned char s[16];
			int sl = snprintf((char *)s, sizeof s,
			    "\033[?%dh", dec_modes[k]);
			if (sl > 0 && (size_t)sl <= sizeof(s))
				bad = queue_to_client(p, s, (size_t)sl) < 0;
		}
		if (rearm && !bad && kbd_u_len)
			bad = queue_to_client(p, kbd_u, kbd_u_len) < 0;
		if (rearm && !bad && kbd_m_len)
			bad = queue_to_client(p, kbd_m, kbd_m_len) < 0;

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
		/* Held: the size is still recorded; the respawn applies it. */
		memcpy(&the_pty.ws, payload, sizeof(struct winsize));
		if (the_pty.fd >= 0)
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
		/* Held: the size is still recorded; the respawn applies it. */
		memcpy(&the_pty.ws, payload, sizeof(struct winsize));
		if (the_pty.fd >= 0)
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

		/* Held: no child to poke (killpty's kill(-pid) fallback
		** would hit pid 0 = our own process group). */
		if (the_pty.fd < 0)
			return 0;

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
	struct timeval tv;
	int highest_fd, nready;
	int nullfd;

	int has_attached_client = 0;
	int pfd;
	long wait_ms;
	struct timespec heal_at;	/* last heal pass (or master start) */

	/* Okay, disassociate ourselves from the original terminal, as we
	** don't care what happens to it. */
	setsid();

	/* Resolve the trace sink now (before any signal handler can call
	** dch_trace), so die() only ever fprintf()s, never fopen()s. */
	dch_trace("master start pid=%d waitattach=%d", (int)getpid(), waitattach);

	listen_fd = s;
	no_replay = getenv("DCH_NO_REPLAY") != NULL;
	{
		const char *e = getenv("DCH_HEAL_MS");
		long v = e ? atol(e) : 0;

		if (v > 0)
			heal_ms = v;
		e = getenv("DCH_HOLD_MS");
		v = e ? atol(e) : 0;
		if (v > 0)
			hold_ms = v;
		e = getenv("DCH_RESPAWN_TTL_MS");
		v = e ? atol(e) : 0;
		if (v > 0)
			respawn_ttl_ms = v;
	}
	/* The inner argv is a tail of dch_argv (run_master_of hands us a
	** pointer into it, on a cold start and after a live restart alike). */
	cur_argv = argv;
	if (dch_argv && argv > dch_argv && argv - dch_argv < 8)
		argv_prefix = (int)(argv - dch_argv);

	/* Set a trap to unlink the socket when we die. */
	atexit(unlink_socket);

	signal(SIGCHLD, die);
	dch_bridge_prepare(argv, resume.valid);

	if (resume.valid)
	{
		master_adopt();
		/* The fd rode across the exec; bind_socket() never ran in
		** this image, so nothing has recorded the node as ours yet.
		** It is still the one the previous image bound (a restart
		** never rebinds), so a plain stat() here is exactly right. */
		record_sock_identity();
	}
	else
	{
		pty_gated = waitattach;

		/* Start at a sane 80x24 instead of 0x0 so headless-spawned
		** programs (and the terminal mirror) see a real grid; the
		** attacher's WINCH corrects it. */
		memset(&the_pty.ws, 0, sizeof(struct winsize));
		the_pty.ws.ws_row = 24;
		the_pty.ws.ws_col = 80;

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
	dch_bridge_start(argv, resume.valid, the_pty.fd, s, statusfd);
	rec_init(resume.valid);

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
	clock_gettime(CLOCK_MONOTONIC, &heal_at);
	while (1)
	{
		int new_has_attached_client = 0;
		int have_writes = 0;
		dch_bridge_reap();

		/* Armed: watch the child itself, not only pty EOF — a grandchild
		** still holding the slave would otherwise keep EOF (and so the
		** respawn) from ever coming. Held: the hold is bounded. */
		wait_ms = heal_ms;
		/* The history record's refresh tick, due even while busy. */
		if (ms_since(&rec_at) >= rec_ms)
		{
			rec_refresh();
			clock_gettime(CLOCK_MONOTONIC, &rec_at);
		}
		if (rec_ms - ms_since(&rec_at) < wait_ms)
			wait_ms = rec_ms - ms_since(&rec_at);
		if (the_pty.fd >= 0 && respawn_live())
		{
			int st;

			if (waitpid(the_pty.pid, &st, WNOHANG) == the_pty.pid)
			{
				drain_pty();	/* its last words, into tail+mirror */
				check_waiters(NULL);
				respawn_consume(st);
			}
			/* ponytail: 200 ms poll while armed covers a SIGCHLD that
			** lands between this check and select(); a self-pipe
			** would make it exact. */
			else if (wait_ms > 200)
				wait_ms = 200;
		}
		if (the_pty.fd < 0)
		{
			long left = hold_ms - ms_since(&respawn_at);

			if (left <= 0)
			{
				dch_trace("hold expired -> master exit");
				exit(exit_code >= 0 ? exit_code : 1);
			}
			if (left < wait_ms)
				wait_ms = left;
		}
		pfd = the_pty.fd;

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
		if (!pty_gated && pfd >= 0)
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

		/* Wait for something to happen. The timeout is the self-heal
		** tick (shortened while armed/held, see above); Linux writes
		** back the remaining time, so tv is set fresh on every pass. */
		tv.tv_sec = wait_ms / 1000;
		tv.tv_usec = (wait_ms % 1000) * 1000;
		nready = select(highest_fd + 1, &readfds,
		                have_writes ? &writefds : NULL, NULL, &tv);
		if (nready < 0)
		{
			if (errno == EINTR || errno == EAGAIN)
				continue;
			exit(1);
		}
		if (nready == 0)
		{
			/* Nothing to serve: heal instead. The fd sets are
			** empty now, so there is nothing else to do this pass.
			** A shortened (armed/held) timeout is not a heal tick
			** by itself, but one that finds a heal overdue runs it:
			** while armed is when an orchestrator needs the socket. */
			int ns = -1;

			if (wait_ms == heal_ms || ms_since(&heal_at) >= heal_ms)
			{
				ns = heal_tick(s, has_attached_client);
				clock_gettime(CLOCK_MONOTONIC, &heal_at);
			}
			if (ns >= 0)
				s = ns;
			continue;
		}

		dch_trace("mloop wake wa=%d ctl=%d pty=%d", pty_gated,
		          FD_ISSET(s, &readfds),
		          pfd >= 0 && FD_ISSET(pfd, &readfds));
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
		/* Only the pty we selected on: a client's MSG_RESPAWN may have
		** replaced it this pass, and the new fd was never in the set. */
		if (pfd >= 0 && the_pty.fd == pfd && FD_ISSET(pfd, &readfds))
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
	s = bind_socket();
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
		/* stdout: spawn_master's status pipe, read to EOF */
		fd[1] = dup(1);
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
