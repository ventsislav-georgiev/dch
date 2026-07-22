/*
    dch — detachable terminal session manager. Built on dtach core
    (attach_main/master_main/push_main from upstream) with the bash-wrapper
    features merged in: auto-named sessions, static-TUI picker for -l/-k/-d,
    nesting refuse, orphan reap, server/client split so closing the VSCode
    terminal kills only the client and the session survives.

    Originally GPLv2 from dtach by Ned T. Crigler. This file is the dch
    entry point that replaces upstream main.c.
*/
#include "dtach.h"

#include <ctype.h>
#include <dirent.h>
#include <stdarg.h>
#include <sys/time.h>
#include <pwd.h>
#include <sys/types.h>
#include <sys/un.h>

/* dch's own version, independent of the dtach base (PACKAGE_VERSION). */
#define DCH_VERSION "1.4.0"

/* Shared globals (declared in dtach.h, used by attach.c/master.c). */
const char copyright[] = "dch - based on dtach " PACKAGE_VERSION
                         " (C)Copyright 2004-2016 Ned T. Crigler";
char *progname;
char *sockname;
int detach_char = '\\' - 64;
int no_suspend;
int redraw_method = REDRAW_UNSPEC;
struct termios orig_term;
int dont_have_tty;

/* dch-local state. */
static char sock_dir[1024];
static char sock_path[1100];
static char session_name[512];
static char client_pidfile[1200];

/* Write a buffer or exit — used by attach.c and master.c (declared in
** dtach.h). Lives here to keep main.c-equivalent self-contained. */
void
write_buf_or_fail(int fd, const void *buf, size_t count)
{
	while (count != 0)
	{
		ssize_t ret = write(fd, buf, count);

		if (ret >= 0)
		{
			buf = (const char *)buf + ret;
			count -= ret;
		}
		else if (ret < 0 && errno == EINTR)
			continue;
		else
			exit(1);
	}
}

/* Payload byte count carried on the wire for a packet, derived from type.
** PUSH/KEYS/READ/WAIT = pkt->len bytes; WINCH/REDRAW = a winsize;
** ATTACH/DETACH = none. */
size_t
packet_payload_len(const struct packet *pkt)
{
	if (pkt->type == MSG_PUSH || pkt->type == MSG_KEYS ||
	    pkt->type == MSG_READ || pkt->type == MSG_WAIT)
		return pkt->len;
	if (pkt->type == MSG_WINCH || pkt->type == MSG_REDRAW)
		return sizeof(struct winsize);
	return 0;
}

void
write_packet_or_fail(int fd, const struct packet *pkt)
{
	/* Serialize [type:1][len:2 host-order][payload]. Single writer per
	** socket, so two write_buf_or_fail calls can't interleave a frame. */
	unsigned char hdr[PKT_HDR];

	hdr[0] = pkt->type;
	hdr[1] = (unsigned char)(pkt->len & 0xff);
	hdr[2] = (unsigned char)((pkt->len >> 8) & 0xff);
	write_buf_or_fail(fd, hdr, PKT_HDR);

	{
		size_t plen = packet_payload_len(pkt);

		if (plen)
			write_buf_or_fail(fd, pkt->u.buf, plen);
	}
}

/* ---- helpers --------------------------------------------------------- */

static void
chomp(char *s)
{
	size_t n = strlen(s);
	while (n > 0 && (s[n - 1] == '\n' || s[n - 1] == '\r' ||
	                 s[n - 1] == ' '  || s[n - 1] == '\t'))
		s[--n] = '\0';
}

/* In-place sanitize: '/' and ' ' → '_'; keep [A-Za-z0-9._-]. */
static void
sanitize(char *s)
{
	char *r = s, *w = s;
	while (*r)
	{
		char c = *r++;
		if (c == '/' || c == ' ')
			c = '_';
		if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
		    (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')
			*w++ = c;
	}
	*w = '\0';
}

/* popen `cmd`, capture first line of stdout (trimmed) into out. Returns 0
** on success and non-empty output, -1 otherwise. */
static int
run_capture(const char *cmd, char *out, size_t outsz)
{
	FILE *fp = popen(cmd, "r");
	if (!fp)
		return -1;
	if (!fgets(out, (int)outsz, fp))
	{
		out[0] = '\0';
		pclose(fp);
		return -1;
	}
	chomp(out);
	pclose(fp);
	return out[0] ? 0 : -1;
}

/* SOCK_DIR = $DCH_SOCKET_DIR, else $XDG_RUNTIME_DIR/dch-$UID, fallback
** /tmp/dch-$UID. mkdir 0700. DCH_SOCKET_DIR (when set) is used verbatim — a host
** process (e.g. a GUI app spawning sessions) can pin one deterministic dir so it
** and a shell-launched dch agree, regardless of the GUI-vs-shell environment
** split that otherwise leaves XDG_RUNTIME_DIR set in one but not the other. Only
** the leaf is created, same as the XDG path; the caller owns the parent. */
static int
compute_sock_dir(void)
{
	const char *dir = getenv("DCH_SOCKET_DIR");
	const char *xdg = getenv("XDG_RUNTIME_DIR");
	uid_t uid = getuid();
	if (dir && *dir)
		snprintf(sock_dir, sizeof(sock_dir), "%s", dir);
	else if (xdg && *xdg)
		snprintf(sock_dir, sizeof(sock_dir), "%s/dch-%u",
		         xdg, (unsigned)uid);
	else
		snprintf(sock_dir, sizeof(sock_dir), "/tmp/dch-%u",
		         (unsigned)uid);
	if (mkdir(sock_dir, 0700) < 0 && errno != EEXIST)
	{
		fprintf(stderr, "dch: mkdir %s: %s\n", sock_dir,
		        strerror(errno));
		return -1;
	}
	return 0;
}

/* ---- debug tracing -------------------------------------------------- */

/* Opt-in trace sink, resolved once from DCH_DEBUG (see dtach.h). Off by
** default: when DCH_DEBUG is unset, the first call caches trace_fp = NULL and
** every call is a cheap early return. Each line is prefixed with a wall-clock
** timestamp and the pid so client/master interleaving is readable. */
void
dch_trace(const char *fmt, ...)
{
	static FILE *trace_fp;
	static int resolved;
	va_list ap;
	struct timeval tv;

	if (!resolved)
	{
		const char *e = getenv("DCH_DEBUG");
		char path[64];

		resolved = 1;
		if (e && *e)
		{
			if (strcmp(e, "1") == 0)
			{
				snprintf(path, sizeof(path),
				         "/tmp/dch.%u.trace",
				         (unsigned)getuid());
				e = path;
			}
			trace_fp = fopen(e, "a");
			if (trace_fp)
			{
				/* Don't leak the sink across the master re-exec
				** into the inner program. ("e"/O_CLOEXEC fopen
				** mode is a GNU extension, unreliable on macOS.) */
				fcntl(fileno(trace_fp), F_SETFD, FD_CLOEXEC);
				setvbuf(trace_fp, NULL, _IOLBF, 0);
			}
		}
	}
	if (!trace_fp)
		return;

	gettimeofday(&tv, NULL);
	fprintf(trace_fp, "%ld.%06ld pid=%d ", (long)tv.tv_sec,
	        (long)tv.tv_usec, (int)getpid());
	va_start(ap, fmt);
	vfprintf(trace_fp, fmt, ap);
	va_end(ap);
	fputc('\n', trace_fp);
}

/* Normalize a session name to the on-disk basename. Long names that would
** overflow AF_UNIX sun_path are deterministically shortened (prefix + djb2
** hash) so create and attach agree. Every sidecar path (.act/.state/.alias)
** must derive from this same normalization or long-named sessions read the
** wrong sidecars (the master writes them under the hashed name). Returns
** `name` unchanged when it fits, else `buf`. */
static const char *
canon_name(const char *name, char *buf, size_t bufsz)
{
	struct sockaddr_un sa;

	if (strlen(sock_dir) + 7 >= sizeof(sa.sun_path))
		return name; /* pathological sock_dir; make_sock_path errors */
	/* Chars available for the name itself: "<sock_dir>/" + name + ".sock\0". */
	size_t budget = sizeof(sa.sun_path) - strlen(sock_dir) - 1 - 5 - 1;
	if (strlen(name) <= budget || budget <= 8)
		return name;
	/* djb2 hash of the full name -> 6 hex chars for uniqueness. */
	unsigned long h = 5381;
	for (const char *p = name; *p; p++)
		h = ((h << 5) + h) + (unsigned char)*p;
	int keep = (int)budget - 7; /* '-' + 6 hex */
	snprintf(buf, bufsz, "%.*s-%06lx", keep, name, h & 0xffffff);
	return buf;
}

/* Build sock_path. Long names are shortened via canon_name so it "just
** works" regardless of cwd/branch length. */
static int
make_sock_path(const char *name)
{
	struct sockaddr_un sa;
	char shortened[256];
	/* Guard before canon_name's subtraction underflows (size_t) on a
	** pathological sock_dir — the final snprintf check would still catch
	** it, but this is clearer and fails fast. */
	if (strlen(sock_dir) + 7 >= sizeof(sa.sun_path))
	{
		fprintf(stderr, "dch: socket directory path too long\n");
		return -1;
	}
	name = canon_name(name, shortened, sizeof(shortened));
	int n = snprintf(sock_path, sizeof(sock_path),
	                 "%s/%s.sock", sock_dir, name);
	if (n < 0 || (size_t)n >= sizeof(sock_path) ||
	    (size_t)n >= sizeof(sa.sun_path))
	{
		fprintf(stderr, "dch: socket path too long\n");
		return -1;
	}
	sockname = sock_path;
	return 0;
}

/* Build <sock_dir>/<canon(name)>.sock into buf (for existence checks that
** don't want to touch the global sock_path). */
static void
session_sock_path(const char *name, char *buf, size_t bufsz)
{
	char cb[256];
	snprintf(buf, bufsz, "%s/%s.sock", sock_dir,
	         canon_name(name, cb, sizeof(cb)));
}

/* Read first line of `path` (NUL-terminated, newline stripped) into out.
** Returns 0 on success and non-empty content. */
static int
read_first_line(const char *path, char *out, size_t outsz)
{
	int fd = open(path, O_RDONLY);
	if (fd < 0)
		return -1;
	ssize_t r;
	do {
		r = read(fd, out, outsz - 1);
	} while (r < 0 && errno == EINTR);
	close(fd);
	if (r <= 0)
		return -1;
	out[r] = '\0';
	char *nl = strchr(out, '\n');
	if (nl)
		*nl = '\0';
	chomp(out);
	return out[0] ? 0 : -1;
}

/* Walk parents from cwd looking for a .git directory or file (worktree).
** On hit, fills `root` with the repo toplevel (parent of .git) and `gitdir`
** with the actual git dir (resolves "gitdir: <path>" indirection). */
static int
find_git(char *root, size_t rootsz, char *gitdir, size_t gitdirsz)
{
	char cur[2048];
	if (!getcwd(cur, sizeof(cur)))
		return -1;

	while (1)
	{
		char dot[2200];
		int n = snprintf(dot, sizeof(dot), "%s/.git", cur);
		if (n < 0 || (size_t)n >= sizeof(dot))
			return -1;
		struct stat st;
		if (stat(dot, &st) == 0)
		{
			snprintf(root, rootsz, "%s", cur);
			if (S_ISDIR(st.st_mode))
			{
				snprintf(gitdir, gitdirsz, "%s", dot);
				return 0;
			}
			/* .git file → "gitdir: <path>" (worktree/submodule). */
			char line[2048];
			if (read_first_line(dot, line, sizeof(line)) == 0 &&
			    strncmp(line, "gitdir: ", 8) == 0)
			{
				const char *p = line + 8;
				if (p[0] == '/')
					snprintf(gitdir, gitdirsz, "%s", p);
				else
					snprintf(gitdir, gitdirsz,
					         "%s/%s", cur, p);
				return 0;
			}
			return -1;
		}
		char *slash = strrchr(cur, '/');
		if (!slash || slash == cur)
			return -1;
		*slash = '\0';
	}
}

/* Auto-name: <repo>-<branch> if cwd is a git repo, else cwd basename.
** Native: stat + open on .git/HEAD — no fork/popen. */
static void
auto_name(char *out, size_t outsz)
{
	char root[2048], gitdir[2048], head[512], base[512], branch[512];

	if (find_git(root, sizeof(root), gitdir, sizeof(gitdir)) == 0)
	{
		const char *slash = strrchr(root, '/');
		const char *bn = slash ? slash + 1 : root;
		snprintf(base, sizeof(base), "%s", bn);
		sanitize(base);

		char headpath[2200];
		snprintf(headpath, sizeof(headpath), "%s/HEAD", gitdir);
		if (read_first_line(headpath, head, sizeof(head)) == 0)
		{
			if (strncmp(head, "ref: refs/heads/", 16) == 0)
				snprintf(branch, sizeof(branch),
				         "%s", head + 16);
			else if (strncmp(head, "ref: ", 5) == 0)
			{
				/* Non-branch ref (tag, packed-ref, …). */
				const char *r = head + 5;
				const char *s = strrchr(r, '/');
				snprintf(branch, sizeof(branch),
				         "%s", s ? s + 1 : r);
			}
			else
			{
				/* Detached HEAD: bare hash, use 7-char short. */
				snprintf(branch, sizeof(branch), "%.7s", head);
			}
		}
		else
			snprintf(branch, sizeof(branch), "detached");

		sanitize(branch);
		snprintf(out, outsz, "%s-%s", base, branch);
		return;
	}

	char cwd[2048];
	if (!getcwd(cwd, sizeof(cwd)))
		snprintf(cwd, sizeof(cwd), "session");
	const char *slash = strrchr(cwd, '/');
	const char *bn = slash ? slash + 1 : cwd;
	snprintf(out, outsz, "%s", bn);
	sanitize(out);
}

/* ---- client tracking (PID files) ------------------------------------ */
/*
** Bash dch used `pgrep -f 'dtach -a +<sock>'` to find live clients. We've
** merged dtach and dch into one binary, so pgrep can't distinguish client
** from server. Instead each client touches <sockdir>/<base>.client.<pid>
** at attach time and unlinks at exit. Liveness = file exists + kill(pid,0)
** + not orphaned (PPID=1 with no TTY = VSCode-helper-closed).
*/

static void
client_pidfile_unlink(void)
{
	if (client_pidfile[0])
		unlink(client_pidfile);
}

static void
client_pidfile_create(const char *sockp)
{
	const char *slash = strrchr(sockp, '/');
	const char *bn = slash ? slash + 1 : sockp;
	snprintf(client_pidfile, sizeof(client_pidfile),
	         "%s/%s.client.%d", sock_dir, bn, (int)getpid());
	int fd = open(client_pidfile, O_CREAT | O_WRONLY | O_TRUNC, 0600);
	if (fd >= 0)
		close(fd);
	atexit(client_pidfile_unlink);
}

/* Return 1 if pid has PPID=1 and no controlling TTY (VSCode-helper-orphan
** signature). Cross-platform via ps. */
static int
pid_is_orphan(int pid)
{
	char pscmd[128], psout[256];
	snprintf(pscmd, sizeof(pscmd),
	         "ps -o ppid=,tty= -p %d 2>/dev/null", pid);
	if (run_capture(pscmd, psout, sizeof(psout)) != 0)
		return 0;
	char *p = psout;
	while (*p == ' ')
		p++;
	long ppid = strtol(p, &p, 10);
	while (*p == ' ' || *p == '\t')
		p++;
	chomp(p);
	int no_tty = (*p == '\0' || strcmp(p, "??") == 0 ||
	              strcmp(p, "?") == 0 || strcmp(p, "-") == 0);
	return ppid == 1 && no_tty;
}

/* Live (non-orphan) client count for `<base>.sock`. Reaps stale entries. */
static int
live_clients_on(const char *sockp)
{
	DIR *d = opendir(sock_dir);
	if (!d)
		return 0;

	const char *slash = strrchr(sockp, '/');
	const char *bn = slash ? slash + 1 : sockp;
	char prefix[600];
	snprintf(prefix, sizeof(prefix), "%s.client.", bn);
	size_t plen = strlen(prefix);

	int count = 0;
	struct dirent *de;
	while ((de = readdir(d)))
	{
		if (strncmp(de->d_name, prefix, plen) != 0)
			continue;
		int pid = atoi(de->d_name + plen);
		char path[2200];
		snprintf(path, sizeof(path), "%s/%s", sock_dir, de->d_name);

		if (pid <= 0 || kill(pid, 0) != 0)
		{
			unlink(path); /* dead */
			continue;
		}
		if (pid_is_orphan(pid))
		{
			kill(pid, SIGTERM);
			unlink(path);
			continue;
		}
		count++;
	}
	closedir(d);
	return count;
}

/* Send SIGUSR1 to all live clients of `<base>.sock` (clean detach). */
static int
detach_all_clients_on(const char *sockp)
{
	DIR *d = opendir(sock_dir);
	if (!d)
		return 0;
	const char *slash = strrchr(sockp, '/');
	const char *bn = slash ? slash + 1 : sockp;
	char prefix[600];
	snprintf(prefix, sizeof(prefix), "%s.client.", bn);
	size_t plen = strlen(prefix);

	int n = 0;
	struct dirent *de;
	while ((de = readdir(d)))
	{
		if (strncmp(de->d_name, prefix, plen) != 0)
			continue;
		int pid = atoi(de->d_name + plen);
		if (pid <= 0)
			continue;
		/* Liveness check first so we don't SIGUSR1 a recycled PID. */
		if (kill(pid, 0) != 0)
		{
			char path[2200];
			snprintf(path, sizeof(path), "%s/%s",
			         sock_dir, de->d_name);
			unlink(path);
			continue;
		}
		if (kill(pid, SIGUSR1) == 0)
			n++;
	}
	closedir(d);
	return n;
}

/* ---- session listing / picker --------------------------------------- */

struct slist
{
	char **v;     /* real session names (socket basenames) */
	char **alias; /* display alias or NULL; parallel to v */
	int n, cap;
};

static void
slist_push(struct slist *l, const char *s)
{
	if (l->n == l->cap)
	{
		l->cap = l->cap ? l->cap * 2 : 8;
		l->v = realloc(l->v, l->cap * sizeof(char *));
		l->alias = realloc(l->alias, l->cap * sizeof(char *));
	}
	l->alias[l->n] = NULL;
	l->v[l->n++] = strdup(s);
}

static void
slist_free(struct slist *l)
{
	int i;
	for (i = 0; i < l->n; i++)
	{
		free(l->v[i]);
		free(l->alias[i]);
	}
	free(l->v);
	free(l->alias);
	l->v = NULL;
	l->alias = NULL;
	l->n = l->cap = 0;
}

/* List active session names (sockets in sock_dir). */
static int
list_sessions(struct slist *out)
{
	DIR *d = opendir(sock_dir);
	if (!d)
		return -1;
	struct dirent *de;
	while ((de = readdir(d)))
	{
		size_t n = strlen(de->d_name);
		if (n <= 5 || strcmp(de->d_name + n - 5, ".sock") != 0)
			continue;
		char nm[600];
		snprintf(nm, sizeof(nm), "%.*s", (int)(n - 5), de->d_name);
		slist_push(out, nm);
	}
	closedir(d);
	return 0;
}

/* Build the alias-sidecar path: <sock_dir>/<name>.sock.alias */
static int
alias_path(const char *name, char *out, size_t outsz)
{
	char cb[256];
	int n = snprintf(out, outsz, "%s/%s.sock.alias", sock_dir,
	                 canon_name(name, cb, sizeof(cb)));
	return (n < 0 || (size_t)n >= outsz) ? -1 : 0;
}

/* Last-activity epoch (mtime of <sock_dir>/<name>.sock.act, written by the
** master on pty output). 0 when the session has produced no output yet / no
** sidecar. Lets a watcher tell a detached-but-working session from an idle one. */
static long
activity_epoch(const char *name)
{
	char ap[1300], cb[256];
	struct stat st;
	int n = snprintf(ap, sizeof ap, "%s/%s.sock.act", sock_dir,
	                 canon_name(name, cb, sizeof(cb)));

	if (n < 0 || (size_t)n >= sizeof ap || stat(ap, &st) != 0)
		return 0;
	return (long)st.st_mtime;
}

/* A session is "working" when the master stamped pty output within this
** window, "idle" otherwise. The master throttles stamps to 1/s, so 5s
** gives headroom without hiding real activity. (v1.3 printed "active"
** here; collapsed into "working" so the busy concept has one name across
** heuristic, detection and reports. --wait keeps "active" as an alias.) */
#define DCH_ACTIVE_SECS 5

static const char *
state_of(long ep)
{
	return (ep && time(NULL) - ep <= DCH_ACTIVE_SECS) ? "working"
	                                                  : "idle";
}

/* Path of the reported-state sidecar (written by --report, e.g. from a
** harness hook). Returns 0 on success, -1 on truncation. */
static int
state_file_path(const char *name, char *buf, size_t bufsz)
{
	char cb[256];
	int n = snprintf(buf, bufsz, "%s/%s.sock.state", sock_dir,
	                 canon_name(name, cb, sizeof(cb)));
	return (n < 0 || (size_t)n >= bufsz) ? -1 : 0;
}

/* The closed set of reportable states (herdr's, plus done). A closed set
** keeps --status output a stable contract and --ls-json trivially valid. */
static int
valid_state_token(const char *tok)
{
	static const char *known[] = { "working", "idle", "blocked", "done" };
	size_t k;
	for (k = 0; k < sizeof(known) / sizeof(known[0]); k++)
		if (strcmp(tok, known[k]) == 0)
			return 1;
	return 0;
}

static const char *detect_state(const char *name);

/* Resolve a session's state. An explicit "done" report always wins; a
** detected on-screen blocker beats any other report (herdr's blocker-
** override — a stale "working" from a crashed harness hook must not
** stick until session death); then the reported state; then screen
** detection; then the output heuristic. Sidecar content is re-validated
** on read so a hand-mangled file never leaks into --status/--ls-json. */
static const char *
session_state(const char *name)
{
	static char st[40];
	char p[1300];
	const char *rep = NULL, *det;

	if (state_file_path(name, p, sizeof(p)) == 0)
	{
		int fd = open(p, O_RDONLY);
		if (fd >= 0)
		{
			int r = (int)read(fd, st, sizeof(st) - 1);
			close(fd);
			while (r > 0 && (st[r - 1] == '\n' || st[r - 1] == '\r'))
				r--;
			st[r > 0 ? r : 0] = '\0';
			if (r > 0 && valid_state_token(st))
				rep = st;
		}
	}
	if (rep && strcmp(rep, "done") == 0)
		return rep;
	det = detect_state(name);
	if (det && strcmp(det, "blocked") == 0)
		return det;
	if (rep)
		return rep;
	if (det)
		return det;
	return state_of(activity_epoch(name));
}

/* Fill sl->alias[i] from each session's sidecar file (NULL if none). */
static void
load_aliases(struct slist *sl)
{
	int i;
	for (i = 0; i < sl->n; i++)
	{
		char ap[1200], line[600];
		if (alias_path(sl->v[i], ap, sizeof(ap)) == 0 &&
		    read_first_line(ap, line, sizeof(line)) == 0)
		{
			free(sl->alias[i]);
			sl->alias[i] = strdup(line);
		}
	}
}

/* Write (or clear, when alias is empty) the sidecar for `name`. */
static int
write_alias(const char *name, const char *alias)
{
	char ap[1200];
	if (alias_path(name, ap, sizeof(ap)) < 0)
		return -1;
	if (!alias || !alias[0])
	{
		unlink(ap);
		return 0;
	}
	int fd = open(ap, O_WRONLY | O_CREAT | O_TRUNC, 0600);
	if (fd < 0)
		return -1;
	(void)!write(fd, alias, strlen(alias));
	(void)!write(fd, "\n", 1);
	close(fd);
	return 0;
}

/* ---- static TUI picker --------------------------------------------- */
/*
** Self-contained arrow-key picker over /dev/tty. No external deps. Keys:
**   up/down or k/j        move
**   enter                 select
**   q / esc / Ctrl-C      cancel
*/

static struct termios picker_saved_term;
static int picker_tty_fd = -1;
static int picker_term_saved;

static void
picker_restore(void)
{
	if (picker_tty_fd >= 0)
	{
		if (picker_term_saved)
			tcsetattr(picker_tty_fd, TCSANOW, &picker_saved_term);
		/* Cursor on + disable mouse/bracketed-paste/alt-screen we may
		** have inherited. dch itself never enables them but a previous
		** in-shell app could've. Safe to send unconditionally. */
		(void)!write(picker_tty_fd,
		    "\033[?25h"
		    "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"
		    "\033[?2004l",
		    6 + 5 * 8 + 8);
		close(picker_tty_fd);
		picker_tty_fd = -1;
	}
}

static void
picker_sigint(int sig)
{
	(void)sig;
	picker_restore();
	_exit(130);
}

/* Write fixed string; no errno fuss. */
static void
tw(int fd, const char *s)
{
	(void)!write(fd, s, strlen(s));
}

/* Erase the (header + N entries) block we drew at the bottom of the screen.
** Caller has previously written exactly `lines` lines ending with \r\n; cursor
** is on the line *after* the last entry. */
static void
picker_erase(int fd, int lines)
{
	char up[32];
	int n = snprintf(up, sizeof(up), "\033[%dA\r", lines);
	(void)!write(fd, up, n);
	for (int i = 0; i < lines; i++)
		tw(fd, "\033[2K\n");
	n = snprintf(up, sizeof(up), "\033[%dA\r", lines);
	(void)!write(fd, up, n);
}

/* Visible window: [top, top+win). Caller maintains top so sel stays in view. */
static void
picker_draw(int fd, const char *header, struct slist *sl, int sel,
            int top, int win)
{
	dprintf(fd, "\033[2K\033[1m%s\033[0m\r\n", header);
	int end = top + win;
	if (end > sl->n)
		end = sl->n;
	for (int i = top; i < end; i++)
	{
		char row[1300];
		if (sl->alias && sl->alias[i])
			snprintf(row, sizeof(row), "%s (%s)",
			         sl->alias[i], sl->v[i]);
		else
			snprintf(row, sizeof(row), "%s", sl->v[i]);
		if (i == sel)
			dprintf(fd, "\033[2K\033[7m> %s\033[0m\r\n", row);
		else
			dprintf(fd, "\033[2K  %s\r\n", row);
	}
}

static int
tty_rows(int fd)
{
	struct winsize ws;
	if (ioctl(fd, TIOCGWINSZ, &ws) == 0 && ws.ws_row > 0)
		return ws.ws_row;
	return 24;
}

/* Interactive picker. Returns 0 on success (out filled), 1 if no sessions,
** no TTY, or user canceled. */
static int
pick_session(const char *header, char *out, size_t outsz)
{
	struct slist sl = {0};
	int rc = 1;

	if (list_sessions(&sl) < 0 || sl.n == 0)
	{
		fprintf(stderr, "no sessions\n");
		slist_free(&sl);
		return 1;
	}
	load_aliases(&sl);

	int fd = open("/dev/tty", O_RDWR);
	if (fd < 0)
	{
		/* Headless: list names so the caller can still see them. */
		for (int i = 0; i < sl.n; i++)
			puts(sl.v[i]);
		slist_free(&sl);
		return 1;
	}

	struct termios raw;
	if (tcgetattr(fd, &picker_saved_term) < 0)
	{
		close(fd);
		slist_free(&sl);
		return 1;
	}
	raw = picker_saved_term;
	raw.c_lflag &= ~(ICANON | ECHO | ISIG | IEXTEN);
	raw.c_iflag &= ~(IXON | ICRNL | INLCR | IGNCR);
	raw.c_cc[VMIN] = 1;
	raw.c_cc[VTIME] = 0;
	if (tcsetattr(fd, TCSANOW, &raw) < 0)
	{
		close(fd);
		slist_free(&sl);
		return 1;
	}
	picker_tty_fd = fd;
	picker_term_saved = 1;
	signal(SIGINT, picker_sigint);
	signal(SIGTERM, picker_sigint);
	signal(SIGHUP, picker_sigint);

	/* Hide cursor + force mouse tracking off so stray mouse motion can't
	** inject CSI bytes into our keypress read. */
	tw(fd, "\033[?25l"
	       "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"
	       "\033[?2004l");

	int sel = 0, top = 0, drawn = 0;
	int rows = tty_rows(fd);
	/* Reserve 2 rows: 1 for header, 1 to keep prompt below picker. */
	int win = rows - 2;
	if (win < 1)
		win = 1;
	if (win > sl.n)
		win = sl.n;
	int lines = 1 + win; /* header + visible entries */

	for (;;)
	{
		/* Keep sel inside [top, top+win). */
		if (sel < top)
			top = sel;
		else if (sel >= top + win)
			top = sel - win + 1;

		if (drawn)
			picker_erase(fd, lines);
		picker_draw(fd, header, &sl, sel, top, win);
		drawn = 1;

		unsigned char c;
		ssize_t r;
		do {
			r = read(fd, &c, 1);
		} while (r < 0 && errno == EINTR);
		if (r <= 0)
			break; /* cancel */

		if (c == '\r' || c == '\n')
		{
			snprintf(out, outsz, "%s", sl.v[sel]);
			rc = 0;
			break;
		}
		if (c == 'q' || c == 3 /*Ctrl-C*/ || c == 4 /*Ctrl-D*/)
			break;
		if (c == 'j')
		{
			if (sel < sl.n - 1)
				sel++;
			continue;
		}
		if (c == 'k')
		{
			if (sel > 0)
				sel--;
			continue;
		}
		if (c == 27) /* ESC or CSI sequence */
		{
			fd_set rs;
			struct timeval tv = {0, 50 * 1000};
			FD_ZERO(&rs);
			FD_SET(fd, &rs);
			if (select(fd + 1, &rs, NULL, NULL, &tv) <= 0)
				break; /* bare ESC = cancel */
			unsigned char b1, b2;
			if (read(fd, &b1, 1) != 1 || b1 != '[')
				break;
			if (read(fd, &b2, 1) != 1)
				break;
			if (b2 == 'A' && sel > 0)
				sel--;
			else if (b2 == 'B' && sel < sl.n - 1)
				sel++;
			continue;
		}
	}

	/* Erase our drawing so the parent shell prompt isn't shoved down. */
	if (drawn)
		picker_erase(fd, lines);
	picker_restore();
	signal(SIGINT, SIG_DFL);
	signal(SIGTERM, SIG_DFL);
	signal(SIGHUP, SIG_DFL);
	slist_free(&sl);
	return rc;
}

/* ---- interactive rename (alias sidecar) ---------------------------- */

/* Put the tty into the same raw mode the picker uses + hide cursor. */
static void
picker_raw_on(int fd)
{
	struct termios raw = picker_saved_term;
	raw.c_lflag &= ~(ICANON | ECHO | ISIG | IEXTEN);
	raw.c_iflag &= ~(IXON | ICRNL | INLCR | IGNCR);
	raw.c_cc[VMIN] = 1;
	raw.c_cc[VTIME] = 0;
	tcsetattr(fd, TCSANOW, &raw);
	tw(fd, "\033[?25l"
	       "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"
	       "\033[?2004l");
}

/* Read one cooked line from the tty (echo on). Returns 0 with the trimmed
** line in `out` (possibly empty), -1 on EOF/error. Caller is responsible
** for re-entering raw mode afterwards. */
static int
prompt_line(int fd, const char *prompt, char *out, size_t outsz)
{
	tcsetattr(fd, TCSANOW, &picker_saved_term); /* cooked + echo */
	tw(fd, "\033[?25h\033[2K\r");
	tw(fd, prompt);
	ssize_t r;
	do {
		r = read(fd, out, outsz - 1);
	} while (r < 0 && errno == EINTR);
	if (r <= 0)
	{
		out[0] = '\0';
		return -1;
	}
	out[r] = '\0';
	chomp(out);
	return 0;
}

/* Interactive rename: pick a session, type a new alias, repeat until quit.
** Empty input clears the alias (reverts to the real name). */
static int
rename_sessions_interactive(void)
{
	struct slist sl = {0};
	if (list_sessions(&sl) < 0 || sl.n == 0)
	{
		fprintf(stderr, "no sessions\n");
		slist_free(&sl);
		return 0;
	}
	load_aliases(&sl);

	int fd = open("/dev/tty", O_RDWR);
	if (fd < 0)
	{
		fprintf(stderr, "dch: rename needs a terminal\n");
		slist_free(&sl);
		return 1;
	}
	if (tcgetattr(fd, &picker_saved_term) < 0)
	{
		close(fd);
		slist_free(&sl);
		return 1;
	}
	picker_tty_fd = fd;
	picker_term_saved = 1;
	signal(SIGINT, picker_sigint);
	signal(SIGTERM, picker_sigint);
	signal(SIGHUP, picker_sigint);
	picker_raw_on(fd);

	const char *header = "rename session (enter=edit, q=quit):";
	int sel = 0, top = 0, drawn = 0, prev_lines = 0;

	for (;;)
	{
		int rows = tty_rows(fd);
		int win = rows - 2;
		if (win < 1)
			win = 1;
		if (win > sl.n)
			win = sl.n;
		int lines = 1 + win;

		if (sel >= sl.n)
			sel = sl.n - 1;
		if (sel < top)
			top = sel;
		else if (sel >= top + win)
			top = sel - win + 1;

		if (drawn)
			picker_erase(fd, prev_lines);
		picker_draw(fd, header, &sl, sel, top, win);
		drawn = 1;
		prev_lines = lines;

		unsigned char c;
		ssize_t r;
		do {
			r = read(fd, &c, 1);
		} while (r < 0 && errno == EINTR);
		if (r <= 0)
			break;

		if (c == 'q' || c == 3 /*^C*/ || c == 4 /*^D*/)
			break;
		if (c == 'j')
		{
			if (sel < sl.n - 1)
				sel++;
			continue;
		}
		if (c == 'k')
		{
			if (sel > 0)
				sel--;
			continue;
		}
		if (c == 27) /* ESC / arrows */
		{
			fd_set rs;
			struct timeval tv = {0, 50 * 1000};
			FD_ZERO(&rs);
			FD_SET(fd, &rs);
			if (select(fd + 1, &rs, NULL, NULL, &tv) <= 0)
				break;
			unsigned char b1, b2;
			if (read(fd, &b1, 1) != 1 || b1 != '[')
				break;
			if (read(fd, &b2, 1) != 1)
				break;
			if (b2 == 'A' && sel > 0)
				sel--;
			else if (b2 == 'B' && sel < sl.n - 1)
				sel++;
			continue;
		}
		if (c == '\r' || c == '\n')
		{
			char prompt[700], in[600];
			picker_erase(fd, prev_lines);
			snprintf(prompt, sizeof(prompt),
			         "new name for '%s' (empty=clear): ", sl.v[sel]);
			if (prompt_line(fd, prompt, in, sizeof(in)) == 0)
			{
				sanitize(in);
				write_alias(sl.v[sel], in);
				free(sl.alias[sel]);
				sl.alias[sel] = in[0] ? strdup(in) : NULL;
			}
			picker_raw_on(fd);
			tw(fd, "\033[2K\r");
			drawn = 0; /* redraw fresh, nothing to erase */
			continue;
		}
	}

	if (drawn)
		picker_erase(fd, prev_lines);
	picker_restore();
	signal(SIGINT, SIG_DFL);
	signal(SIGTERM, SIG_DFL);
	signal(SIGHUP, SIG_DFL);
	slist_free(&sl);
	return 0;
}

/* ---- session kill --------------------------------------------------- */

/*
** Kill = SIGTERM master process(es) for socket + unlink socket file. The
** master daemonizes via re-exec as `dch --master-of <sock> -- <cmd...>` so
** its argv contains the sock path; we pkill -f on that sentinel.
*/

static int
pkill_pattern(const char *pat)
{
	char cmd[2048];
	snprintf(cmd, sizeof(cmd),
	         "pkill -TERM -f '%s' 2>/dev/null; exit 0", pat);
	return system(cmd);
}

static int
kill_session(const char *name)
{
	char sp[1100];
	session_sock_path(name, sp, sizeof(sp));
	struct stat st;
	if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
	{
		fprintf(stderr, "no session: %s\n", name);
		return 1;
	}

	/* Term master (its sentinel argv contains the sock path). */
	char pat[1200];
	snprintf(pat, sizeof(pat), "dch --master-of %s", sp);
	pkill_pattern(pat);

	/* Term any clients still attached. */
	DIR *d = opendir(sock_dir);
	if (d)
	{
		char prefix[600];
		snprintf(prefix, sizeof(prefix), "%s.sock.client.", name);
		size_t plen = strlen(prefix);
		struct dirent *de;
		while ((de = readdir(d)))
		{
			if (strncmp(de->d_name, prefix, plen) != 0)
				continue;
			int pid = atoi(de->d_name + plen);
			if (pid > 0)
				kill(pid, SIGTERM);
			char path[2200];
			snprintf(path, sizeof(path), "%s/%s",
			         sock_dir, de->d_name);
			unlink(path);
		}
		closedir(d);
	}

	unlink(sp);
	char ap[1200];
	if (alias_path(name, ap, sizeof(ap)) == 0)
		unlink(ap);
	/* Drop the activity + reported-state sidecars too (the master unlinks
	** them on a clean exit, but a SIGKILLed master never runs atexit). */
	char side[1300];
	if (snprintf(side, sizeof side, "%s/%s.sock.act", sock_dir, name) > 0)
		unlink(side);
	if (state_file_path(name, side, sizeof(side)) == 0)
		unlink(side);
	printf("killed: %s\n", name);
	return 0;
}

static int
kill_all(void)
{
	struct slist sl = {0};
	int i;
	if (list_sessions(&sl) < 0 || sl.n == 0)
	{
		printf("no sessions\n");
		slist_free(&sl);
		return 0;
	}
	for (i = 0; i < sl.n; i++)
	{
		/* Skip entries that vanished between list and kill so we
		** don't spam "no session: X" on a race. */
		char sp[1100];
		snprintf(sp, sizeof(sp), "%s/%s.sock", sock_dir, sl.v[i]);
		struct stat st;
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
			continue;
		kill_session(sl.v[i]);
	}
	slist_free(&sl);
	return 0;
}

/* ---- usage ---------------------------------------------------------- */

static void
usage(void)
{
	printf(
	    "dch " DCH_VERSION " — detachable terminal session\n"
	    "Usage:\n"
	    "  dch              attach free session, else spawn new\n"
	    "  dch <cmd...>     attach-or-create, run cmd\n"
	    "  dch -l           pick a session and attach (mirror)\n"
	    "  dch -k [name]    kill session; without name, pick interactively\n"
	    "  dch -kl          kill ALL sessions\n"
	    "  dch -ls          list sessions (one per line)\n"
	    "  dch -lj          list sessions as name<TAB>alias<TAB>activity_epoch per line\n"
	    "  dch -rl          interactively rename (alias) sessions\n"
	    "  dch -m <name> [alias]  set alias non-interactively (empty clears)\n"
	    "  dch -d [name]    detach all clients of session (sends SIGUSR1)\n"
	    "  dch -n <name>    override auto-name\n"
	    "  dch -f           force attach even if busy (mirror)\n"
	    "  dch -E           disable detach escape (Ctrl-\\); use -d instead\n"
	    "  dch -e <c>       set detach character (default ^\\)\n"
	    "  dch -h           this help\n"
	    "  dch -V           print version\n"
	    "Agent/control verbs (no attach; safe to script):\n"
	    "  dch --spawn <name> [--size CxR] [--env K=V]... [cmd...]\n"
	    "                                             start headless session\n"
	    "  dch --send <name> <text...>                type text into session\n"
	    "  dch --run <name> <text...>                 type text, press enter\n"
	    "  dch --keys <name> <key...>                 send keys (ctrl+c, up, f2, ...)\n"
	    "  dch --read <name> [--ansi] [--recent [N]]  print session screen\n"
	    "  dch --wait <name> --match <str> [--timeout ms]  wait for output\n"
	    "  dch --wait <name> --state <s> [--timeout ms]    wait for state\n"
	    "  dch --status <name>                        print session state:\n"
	    "                                             working|idle|blocked|done\n"
	    "                                             (reported, else detected\n"
	    "                                             from screen, else output\n"
	    "                                             heuristic)\n"
	    "  dch --report <name> <state>                set state (optional hooks;\n"
	    "                                             `clear` reverts to auto)\n"
	    "  dch --ls-json    like -lj but JSON, adds \"state\"\n"
	    "Env: DCH_NO_DETECT=1 disables screen-content state detection.\n"
	    "Detach: Ctrl-\\  (or `dch -d` if the host swallows it).\n"
	    "Nesting refused inside an existing dch session.\n");
}

/* ---- subcommand: server-only sentinel ------------------------------- */
/*
** When we daemonize the master, we re-exec dch with `--master-of <sock>
** -- <cmd...>` so the running process's argv is grep-able by kill_session.
*/

static int
run_master_of(int argc, char **argv)
{
	if (argc < 1)
		return 1;
	sockname = argv[0];
	argc--; argv++;
	/* Optional "--" separator from caller's argv. */
	if (argc >= 1 && strcmp(argv[0], "--") == 0)
	{
		argc--; argv++;
	}
	if (argc < 1)
		return 1;
	if (tcgetattr(0, &orig_term) < 0)
	{
		memset(&orig_term, 0, sizeof(struct termios));
		dont_have_tty = 1;
	}
	/* Disable the default detach char; clients set their own. */
	return master_main(argv, 1, 1); /* waitattach=1, dontfork=1 */
}

/* ---- attach action -------------------------------------------------- */

/* Build a daemon-launching command vector. We fork+setsid here so the
** master survives the originating terminal closing. Returns 0 on parent
** success. */
static int
spawn_master(char *exe, const char *sockp, char **inner_argv)
{
	pid_t pid = fork();
	if (pid < 0)
	{
		fprintf(stderr, "dch: fork: %s\n", strerror(errno));
		return -1;
	}
	if (pid == 0)
	{
		/* Child: become session leader, drop tty, exec sentinel. */
		setsid();
		int nullfd = open("/dev/null", O_RDWR);
		if (nullfd >= 0)
		{
			dup2(nullfd, 0);
			dup2(nullfd, 1);
			dup2(nullfd, 2);
			if (nullfd > 2)
				close(nullfd);
		}
		/* Build argv: exe --master-of <sock> -- inner_argv... */
		int ic = 0;
		while (inner_argv[ic])
			ic++;
		char **av = malloc(sizeof(char *) * (ic + 5));
		int j = 0;
		av[j++] = exe;
		av[j++] = (char *)"--master-of";
		av[j++] = (char *)sockp;
		av[j++] = (char *)"--";
		int k;
		for (k = 0; k < ic; k++)
			av[j++] = inner_argv[k];
		av[j] = NULL;
		/* execvp handles both absolute paths (when exe = realpath
		** result) and bare names (when realpath failed and exe is
		** the original argv[0] = "dch" found via PATH). */
		execvp(av[0], av);
		_exit(127);
	}
	dch_trace("spawn_master child=%d sock=%s", (int)pid, sockp);
	/* Parent: wait briefly so child can fail loudly before we attach. Poll on
	** a fine 5ms quantum (200 * 5ms = 1s cap): the master binds in a few ms,
	** so a coarse quantum just adds dead time to every cold start. Cost is a
	** cheap stat()/waitpid() spin that exits the instant the socket appears. */
	int status = 0;
	for (int i = 0; i < 200; i++)
	{
		struct stat st;
		if (stat(sockp, &st) == 0 && S_ISSOCK(st.st_mode))
			return 0;
		struct timespec ts = {0, 5 * 1000 * 1000}; /* 5ms */
		nanosleep(&ts, NULL);
		pid_t r = waitpid(pid, &status, WNOHANG);
		if (r == pid)
		{
			dch_trace("spawn_master child=%d died early status=%d",
			          (int)pid, status);
			return -1;
		}
	}
	return 0;
}

static int
do_attach(char *exe, int forced_name, int force, char **inner_argv,
          int inner_argc)
{
	/* Nesting refuse. */
	const char *nested = getenv("DCH_SESSION");
	if (nested && *nested)
	{
		fprintf(stderr,
		    "dch: already inside session '%s'; nesting disabled\n",
		    nested);
		return 1;
	}

	/* If auto-attaching and base is busy, advance to next free slot. */
	if (!forced_name && !force)
	{
		char base[512];
		snprintf(base, sizeof(base), "%s", session_name);
		int i;
		for (i = 1; i <= 999; i++)
		{
			char cand[600];
			if (i == 1)
				snprintf(cand, sizeof(cand), "%s", base);
			else
				snprintf(cand, sizeof(cand), "%s-%d",
				         base, i);
			char sp[1100];
			snprintf(sp, sizeof(sp), "%s/%s.sock",
			         sock_dir, cand);
			struct stat st;
			if (stat(sp, &st) != 0 || !S_ISSOCK(st.st_mode) ||
			    live_clients_on(sp) == 0)
			{
				snprintf(session_name,
				         sizeof(session_name), "%s", cand);
				break;
			}
		}
		if (i > 999)
		{
			fprintf(stderr,
			    "dch: 999 attached sessions for base '%s' — refusing\n",
			    base);
			return 1;
		}
	}

	if (make_sock_path(session_name) < 0)
		return 1;

	if (tcgetattr(0, &orig_term) < 0)
	{
		fprintf(stderr, "dch: attaching requires a terminal\n");
		return 1;
	}

	/* Hot path: if a socket node is present, attach to it directly — a single
	** connect(), no probe. attach_main() only RETURNS when connect() fails; a
	** live master never returns (it runs the session and exit()s). So a return
	** here means the node is a dead socket left by a crashed master whose
	** atexit unlink never ran — fall through and respawn. A stray non-socket
	** file (S_ISSOCK false) skips straight to the spawn path. This decides
	** spawn-vs-attach by LIVENESS (does connect() succeed?), which stat() alone
	** cannot — without it a dead socket looks attachable and a stray file makes
	** the master's bind() hit EADDRINUSE ("failed to spawn"). */
	{
	struct stat st;
	if (stat(sock_path, &st) == 0 && S_ISSOCK(st.st_mode))
	{
		dch_trace("attach hot-path sock=%s", sock_path);
		client_pidfile_create(sock_path);
		attach_main(1);		/* returns only if connect() failed */
		dch_trace("hot-path connect failed; dead socket, respawning");
	}
	}

	/* No node, a stray non-socket file, or a dead socket: clear whatever is
	** there so bind() succeeds, then spawn a fresh master and attach. */
	unlink(sock_path);
	setenv("DCH_SESSION", session_name, 1);
	{
	char *default_argv[] = { NULL, NULL };
	if (inner_argc == 0)
	{
		const char *sh = getenv("SHELL");
		default_argv[0] = (char *)(sh && *sh ? sh : "/bin/sh");
		inner_argv = default_argv;
	}
	if (spawn_master(exe, sock_path, inner_argv) < 0)
	{
		fprintf(stderr, "dch: failed to spawn session\n");
		return 1;
	}
	}
	client_pidfile_create(sock_path);
	return attach_main(0);
}

/* ---- control verbs: --read --wait --keys --send --run --spawn ------- */
/*
** These open a CONTROL connection to the session master: request frames go
** out with the normal packet writer, responses come back framed as
** [type][len:2 LE][payload] (MSG_READ_DATA/READ_END/WAIT_HIT/ACK). A control
** connection never attaches, so reading a session can't perturb it.
*/

#include <poll.h>

/* Inactivity deadline for --read/--keys responses. Generous: the master
** answers in microseconds; this only bounds a dead/ancient master. */
#define CTL_IDLE_MS 2000

static const char no_vt_msg[] =
    "dch: this session has no terminal mirror (dch-lite build or DCH_NO_VT)\n"
    "     — install full dch and restart the session to use this command\n";
static const char old_master_msg[] =
    "dch: no reply — session master predates terminal-mirror support;\n"
    "     restart the session with current dch\n";

static int
control_connect(const char *name, int quiet)
{
	struct sockaddr_un sa;
	int s;

	if (make_sock_path(name) < 0)
		return -1;
	s = socket(PF_UNIX, SOCK_STREAM, 0);
	if (s < 0)
		return -1;
	if (strlen(sock_path) > sizeof(sa.sun_path) - 1)
	{
		close(s);
		return -1;
	}
	memset(&sa, 0, sizeof(sa));
	sa.sun_family = AF_UNIX;
	strcpy(sa.sun_path, sock_path);
	if (connect(s, (struct sockaddr *)&sa, sizeof(sa)) < 0)
	{
		close(s);
		if (!quiet)
			fprintf(stderr, "dch: no session: %s\n", name);
		return -1;
	}
	return s;
}

static void
send_ctl(int s, unsigned char type, const void *payload, size_t len)
{
	struct packet pkt;

	memset(&pkt, 0, sizeof(pkt));
	pkt.type = type;
	pkt.len = (unsigned short)len;
	if (len)
		memcpy(pkt.u.buf, payload, len);
	write_packet_or_fail(s, &pkt);
}

/* Reassembly state for master->client response frames. */
struct ctl_reader
{
	unsigned char buf[PKT_HDR + PKT_MAX];
	size_t fill;
	int got_any; /* any bytes ever (old-master detection) */
};

/* Next response frame. Returns 1 (frame), 0 (idle_ms of silence), -1 (EOF /
** error). The deadline is INACTIVITY-based: every received byte resets it,
** so a large response streaming slowly never false-trips. */
static int
ctl_next_frame(int s, struct ctl_reader *r, int idle_ms,
               unsigned char *type, unsigned char *payload, unsigned int *len)
{
	for (;;)
	{
		if (r->fill >= PKT_HDR)
		{
			unsigned int flen = (unsigned int)r->buf[1] |
			                    ((unsigned int)r->buf[2] << 8);
			unsigned int plen =
			    (r->buf[0] == MSG_READ_DATA ||
			     r->buf[0] == MSG_WAIT_HIT) ? flen : 0;

			if (plen > PKT_MAX)
				return -1; /* corrupt */
			if (r->fill >= PKT_HDR + plen)
			{
				*type = r->buf[0];
				*len = flen;
				if (plen)
					memcpy(payload, r->buf + PKT_HDR, plen);
				memmove(r->buf, r->buf + PKT_HDR + plen,
				        r->fill - PKT_HDR - plen);
				r->fill -= PKT_HDR + plen;
				return 1;
			}
		}

		struct pollfd pfd = { s, POLLIN, 0 };
		int rc = poll(&pfd, 1, idle_ms);
		if (rc == 0)
			return 0;
		if (rc < 0)
		{
			if (errno == EINTR)
				continue;
			return -1;
		}
		ssize_t n = read(s, r->buf + r->fill,
		                 sizeof(r->buf) - r->fill);
		if (n <= 0)
			return -1;
		r->fill += (size_t)n;
		r->got_any = 1;
	}
}

/* Map a READ_END/ACK status to an exit code + message. */
static int
ctl_status_exit(unsigned int status)
{
	switch (status)
	{
	case DCH_ST_OK:
		return 0;
	case DCH_ST_NOVT:
		fputs(no_vt_msg, stderr);
		return 3;
	case DCH_ST_TRUNC:
		fprintf(stderr, "dch: response truncated (over 2MB), "
		        "tail kept\n");
		return 0;
	case DCH_ST_BUSY:
		fprintf(stderr, "dch: session output queue busy, retry\n");
		return 1;
	default:
		fprintf(stderr, "dch: session terminal mirror error\n");
		return 1;
	}
}

static int
ctl_dead_exit(struct ctl_reader *r, const char *verb)
{
	if (!r->got_any)
		fputs(old_master_msg, stderr);
	else
		fprintf(stderr, "dch: truncated %s response\n", verb);
	return 1;
}

/* ---- Built-in agent-state detection (--status/--wait/--ls-json) ----
**
** Classifies the session's visible screen against a small table of UI
** strings the popular agent harnesses paint (permission prompts, "esc to
** interrupt" footers, spinner glyphs). Runs client-side over the existing
** MSG_READ protocol: zero master changes, so running sessions gain
** detection on a plain binary update. Any failure — lite/no-mirror
** master, busy, timeout, truncation, old master — silently falls back to
** the activity heuristic. DCH_NO_DETECT=1 disables detection. */

#define DET_CAP           (128 * 1024) /* tail-kept screen buffer */
#define DET_LINES         20           /* bottom non-empty lines scanned */
#define DET_DEADLINE_MS   500          /* wall-clock cap per fetch */
#define DET_WORK_ACT_SECS 30           /* "working" needs output this new */

/* Fetch the plain visible screen into buf (cap bytes + NUL). Keeps the
** TAIL when the stream exceeds cap — detection reads bottom lines (the
** master itself tail-caps at 2MB). The deadline is wall-clock:
** ctl_next_frame's timeout is inactivity-based, and a master trickling
** bytes must not stall --ls-json. Returns content length, -1 on any
** failure. */
static int
fetch_screen(const char *name, char *buf, size_t cap, int deadline_ms)
{
	unsigned char req[4], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	struct timeval t0, now;
	unsigned int len;
	size_t used = 0;
	int s, rc;

	s = control_connect(name, 1);
	if (s < 0)
		return -1;
	req[0] = DCH_READ_PLAIN;
	req[1] = DCH_READ_VISIBLE;
	req[2] = req[3] = 0;
	send_ctl(s, MSG_READ, req, sizeof(req));
	gettimeofday(&t0, NULL);
	for (;;)
	{
		int left;

		gettimeofday(&now, NULL);
		left = deadline_ms -
		       (int)((now.tv_sec - t0.tv_sec) * 1000 +
		             (now.tv_usec - t0.tv_usec) / 1000);
		if (left <= 0)
			break;
		rc = ctl_next_frame(s, &r, left, &type, payload, &len);
		if (rc <= 0)
			break;
		if (type == MSG_READ_DATA)
		{
			if (len >= cap)
			{
				memcpy(buf, payload + len - cap, cap);
				used = cap;
			}
			else
			{
				if (used + len > cap)
				{
					size_t drop = used + len - cap;
					memmove(buf, buf + drop, used - drop);
					used -= drop;
				}
				memcpy(buf + used, payload, len);
				used += len;
			}
		}
		else if (type == MSG_READ_END)
		{
			close(s);
			if (len != DCH_ST_OK && len != DCH_ST_TRUNC)
				return -1;
			buf[used] = '\0';
			return (int)used;
		}
		else
			break; /* protocol violation */
	}
	close(s);
	return -1;
}

/* One rule: ANDed lowercase substrings (s2 optional). OR variants are
** separate rows. Strings are facts about the harnesses' own UIs (herdr
** is prior art for the approach; nothing is ported from it — AGPL). */
struct det_rule
{
	const char *s1, *s2;
};

/* Screen shows history, not live state — abort detection entirely. */
static const struct det_rule det_suppress[] = {
	{ "showing detailed transcript", 0 },              /* claude ctrl-o */
	{ "pgup/pgdn to", "q to quit" },                   /* codex pager   */
};
/* Waiting on a human. Checked before working: a stale spinner must not
** mask a live permission prompt. */
static const struct det_rule det_blocked[] = {
	{ "do you want to proceed?", 0 },                  /* claude,gemini */
	{ "enter to select", "esc to cancel" },            /* claude forms  */
	{ "press enter to confirm or esc to cancel", 0 },  /* codex         */
	{ "enter to submit answer", 0 },                   /* codex forms   */
	{ "allow command?", 0 },                           /* codex exec    */
	{ "permission required", 0 },                      /* opencode      */
	{ "waiting for approval", 0 },                     /* cursor        */
	{ "waiting for user confirmation", 0 },            /* gemini        */
	{ "allow execution", 0 },                          /* gemini        */
	{ "run this command?", 0 },                        /* cursor        */
	{ "proceed (y)", 0 },                              /* cursor        */
};
/* Busy right now (footer hints; spinner handled separately). */
static const struct det_rule det_working[] = {
	{ "esc to interrupt", 0 },                 /* claude,codex,opencode */
	{ "ctrl+c to interrupt", 0 },                      /* opencode      */
};

static int
det_match(const struct det_rule *t, size_t n, const char *hay)
{
	size_t i;
	for (i = 0; i < n; i++)
		if (strstr(hay, t[i].s1) &&
		    (!t[i].s2 || strstr(hay, t[i].s2)))
			return 1;
	return 0;
}

/* TUI spinner glyphs (the 10-frame braille set harnesses animate; the
** whole U+2800 block would false-positive on progress bars/sparklines).
** Counts only at the start of a line — after whitespace/box-drawing —
** and followed by space or line end. Scans raw bytes; the ASCII fold
** never touches multibyte sequences. */
static int
det_spinner(const char *scr)
{
	/* U+280B U+2819 U+2839 U+2838 U+283C U+2834 U+2826 U+2827 U+2807
	** U+280F -> UTF-8 E2 A0 <third byte>: */
	static const unsigned char third[] = {
		0x8B, 0x99, 0xB9, 0xB8, 0xBC, 0xB4, 0xA6, 0xA7, 0x87, 0x8F
	};
	const unsigned char *p = (const unsigned char *)scr;
	int at_start = 1;

	while (*p)
	{
		if (*p == '\n')
		{
			at_start = 1;
			p++;
			continue;
		}
		if (at_start)
		{
			if (*p == ' ' || *p == '\t')
			{
				p++;
				continue;
			}
			/* skip box-drawing U+2500-U+257F (e.g. table edges) */
			if (p[0] == 0xE2 && (p[1] == 0x94 || p[1] == 0x95) &&
			    p[2])
			{
				p += 3;
				continue;
			}
			if (p[0] == 0xE2 && p[1] == 0xA0 && p[2])
			{
				size_t k;
				for (k = 0; k < sizeof(third); k++)
					if (p[2] == third[k] &&
					    (p[3] == ' ' || p[3] == '\n' ||
					     p[3] == '\0'))
						return 1;
			}
			at_start = 0;
		}
		p++;
	}
	return 0;
}

/* Classify the visible screen: "blocked", "working", or NULL (no signal
** or no screen — caller falls back). Detected "working" additionally
** requires recent pty output: the covered harnesses animate their
** spinner/status line while working (continuous repaint = fresh .act),
** so a frozen frame with a stale footer must read idle, not working. */
static const char *
detect_state(const char *name)
{
	char *raw, *fold, *end, *p;
	const char *res = NULL;
	size_t rl, k;
	int n, lines;

	if (getenv("DCH_NO_DETECT"))
		return NULL;
	raw = malloc(DET_CAP + 1);
	if (!raw)
		return NULL;
	n = fetch_screen(name, raw, DET_CAP, DET_DEADLINE_MS);
	if (n <= 0)
	{
		free(raw);
		return NULL;
	}
	/* isolate the last DET_LINES non-empty lines */
	end = raw + n;
	p = end;
	lines = 0;
	while (p > raw && lines < DET_LINES)
	{
		char *ls = p, *q;
		int nonblank = 0;

		while (ls > raw && ls[-1] != '\n')
			ls--;
		for (q = ls; q < p; q++)
			if (*q != ' ' && *q != '\t' && *q != '\n')
			{
				nonblank = 1;
				break;
			}
		if (nonblank)
			lines++;
		p = ls > raw ? ls - 1 : raw;
	}
	rl = (size_t)(end - p);
	fold = malloc(rl + 1);
	if (!fold)
	{
		free(raw);
		return NULL;
	}
	for (k = 0; k < rl; k++)
	{
		char c = p[k];
		if (c == '\0')
			c = ' ';
		/* explicit ASCII fold: tolower() is locale-dependent and UB
		** on negative char; this leaves UTF-8 bytes untouched */
		fold[k] = (c >= 'A' && c <= 'Z') ? c + 32 : c;
	}
	fold[rl] = '\0';

	if (det_match(det_suppress,
	              sizeof(det_suppress) / sizeof(det_suppress[0]), fold))
		res = NULL;
	else if (det_match(det_blocked,
	                   sizeof(det_blocked) / sizeof(det_blocked[0]),
	                   fold))
		res = "blocked";
	else if (det_match(det_working,
	                   sizeof(det_working) / sizeof(det_working[0]),
	                   fold) ||
	         det_spinner(p))
	{
		long ep = activity_epoch(name);
		if (ep && time(NULL) - ep <= DET_WORK_ACT_SECS)
			res = "working";
	}
	free(fold);
	free(raw);
	return res;
}

static int
do_read_verb(const char *name, int ansi, int recent_lines)
{
	unsigned char req[4], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	int s, rc;

	s = control_connect(name, 0);
	if (s < 0)
		return 1;

	req[0] = ansi ? DCH_READ_ANSI : DCH_READ_PLAIN;
	req[1] = recent_lines >= 0 ? DCH_READ_RECENT : DCH_READ_VISIBLE;
	req[2] = recent_lines >= 0 ? (recent_lines & 0xff) : 0;
	req[3] = recent_lines >= 0 ? ((recent_lines >> 8) & 0xff) : 0;
	send_ctl(s, MSG_READ, req, sizeof(req));

	for (;;)
	{
		rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
		if (rc <= 0)
		{
			close(s);
			return ctl_dead_exit(&r, "read");
		}
		if (type == MSG_READ_DATA)
			fwrite(payload, 1, len, stdout);
		else if (type == MSG_READ_END)
		{
			close(s);
			/* end the screen dump on a newline for shells/agents */
			if (r.got_any)
				fputc('\n', stdout);
			return ctl_status_exit(len);
		}
		else
		{
			close(s);
			return 1; /* protocol violation */
		}
	}
}

static int
do_wait_verb(const char *name, const char *pattern, int timeout_ms)
{
	unsigned char payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	size_t plen = strlen(pattern);
	int s, rc;
	struct timeval start, now;

	if (plen == 0 || plen > DCH_WAIT_MAX)
	{
		fprintf(stderr, "dch: --match must be 1..%d bytes\n",
		        DCH_WAIT_MAX);
		return 1;
	}
	s = control_connect(name, 0);
	if (s < 0)
		return 1;
	send_ctl(s, MSG_WAIT, pattern, plen);

	gettimeofday(&start, NULL);
	for (;;)
	{
		int elapsed, left;

		gettimeofday(&now, NULL);
		elapsed = (int)((now.tv_sec - start.tv_sec) * 1000 +
		                (now.tv_usec - start.tv_usec) / 1000);
		left = timeout_ms - elapsed;
		if (left <= 0)
		{
			close(s);
			fprintf(stderr, "dch: wait timed out after %dms\n",
			        timeout_ms);
			return 2;
		}
		rc = ctl_next_frame(s, &r, left, &type, payload, &len);
		if (rc == 0)
			continue; /* loop re-checks the wall clock */
		if (rc < 0)
		{
			close(s);
			return ctl_dead_exit(&r, "wait");
		}
		if (type == MSG_WAIT_HIT)
		{
			fwrite(payload, 1, len, stdout);
			fputc('\n', stdout);
			close(s);
			return 0;
		}
		if (type == MSG_READ_END)
		{
			int e = ctl_status_exit(len);

			close(s);
			return e ? e : 1; /* a status frame here is never a hit */
		}
	}
}

/* Legacy fallback encoding for --keys against a mirror-less session: a
** fixed xterm-default table, blind to kitty/application modes. */
static int
legacy_keys(const char *combo, char *out, size_t outsz)
{
	static const struct { const char *name; const char *seq; } tab[] = {
		{"enter", "\r"}, {"esc", "\x1b"}, {"escape", "\x1b"},
		{"tab", "\t"}, {"space", " "}, {"backspace", "\x7f"},
		{"up", "\x1b[A"}, {"down", "\x1b[B"}, {"right", "\x1b[C"},
		{"left", "\x1b[D"}, {"home", "\x1b[H"}, {"end", "\x1b[F"},
		{"pgup", "\x1b[5~"}, {"pgdn", "\x1b[6~"},
		{"delete", "\x1b[3~"}, {"insert", "\x1b[2~"},
		{"f1", "\x1bOP"}, {"f2", "\x1bOQ"}, {"f3", "\x1bOR"},
		{"f4", "\x1bOS"}, {"f5", "\x1b[15~"}, {"f6", "\x1b[17~"},
		{"f7", "\x1b[18~"}, {"f8", "\x1b[19~"}, {"f9", "\x1b[20~"},
		{"f10", "\x1b[21~"}, {"f11", "\x1b[23~"}, {"f12", "\x1b[24~"},
	};
	size_t k;
	int alt = 0, ctrl = 0;
	char pos = 0;

	for (;;)
	{
		if (strncasecmp(combo, "ctrl+", 5) == 0)
		{
			ctrl = 1;
			combo += 5;
		}
		else if (strncasecmp(combo, "alt+", 4) == 0)
		{
			alt = 1;
			combo += 4;
		}
		else if (strncasecmp(combo, "shift+", 6) == 0)
			combo += 6; /* legacy table can't encode shift chords */
		else
			break;
	}
	if (ctrl && combo[0] && !combo[1])
	{
		char c = combo[0];
		if (c >= 'A' && c <= 'Z')
			c += 32;
		if (c >= 'a' && c <= 'z')
			pos = c & 037;
		else if (c == '[')
			pos = 0x1b;
		else
			return -1;
		snprintf(out, outsz, "%s%c", alt ? "\x1b" : "", pos);
		return 0;
	}
	if (combo[0] && !combo[1])
	{
		snprintf(out, outsz, "%s%c", alt ? "\x1b" : "", combo[0]);
		return 0;
	}
	for (k = 0; k < sizeof(tab) / sizeof(tab[0]); k++)
		if (strcasecmp(tab[k].name, combo) == 0)
		{
			snprintf(out, outsz, "%s%s", alt ? "\x1b" : "",
			         tab[k].seq);
			return 0;
		}
	return -1;
}

static int
do_keys_verb(const char *name, char **combos, int ncombos)
{
	unsigned char spec[PKT_MAX], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	size_t fill = 0;
	int s, rc, k;

	if (ncombos == 0)
	{
		fprintf(stderr, "dch: --keys needs at least one key\n");
		return 1;
	}
	for (k = 0; k < ncombos; k++)
	{
		size_t l = strlen(combos[k]);
		if (fill + l + 1 > sizeof(spec))
		{
			fprintf(stderr, "dch: too many keys in one call\n");
			return 1;
		}
		memcpy(spec + fill, combos[k], l);
		fill += l;
		spec[fill++] = '\0'; /* NUL-separated wire format */
	}

	s = control_connect(name, 0);
	if (s < 0)
		return 1;
	send_ctl(s, MSG_KEYS, spec, fill - 1); /* drop trailing NUL */

	rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
	if (rc <= 0)
	{
		close(s);
		return ctl_dead_exit(&r, "keys");
	}
	if (type != MSG_ACK)
	{
		close(s);
		return 1;
	}
	if (len == DCH_ST_NOVT || len == DCH_ST_ERR)
	{
		/* Mirror-less session: fall back to a fixed legacy table so
		** the common chords still land; warn that mode-aware apps
		** (kitty protocol, application cursor keys) may see the
		** wrong encoding. */
		char one[16];
		char bytes[PKT_MAX];
		size_t bfill = 0;

		for (k = 0; k < ncombos; k++)
		{
			if (legacy_keys(combos[k], one, sizeof(one)) < 0)
			{
				fprintf(stderr, "dch: unknown key: %s\n",
				        combos[k]);
				close(s);
				return 1;
			}
			size_t l = strlen(one);
			if (bfill + l > sizeof(bytes))
				break;
			memcpy(bytes + bfill, one, l);
			bfill += l;
		}
		send_ctl(s, MSG_PUSH, bytes, bfill);
		fprintf(stderr, "dch: session has no terminal mirror; sent "
		        "legacy key encoding (mode-aware apps may "
		        "misread modified keys)\n");
		close(s);
		return 0;
	}
	close(s);
	if (len != DCH_ST_OK)
		fprintf(stderr, "dch: unknown key in: %s...\n", combos[0]);
	return len == DCH_ST_OK ? 0 : 1;
}

static int
do_send_verb(const char *name, char **words, int nwords, int press_enter)
{
	int s, k;

	if (nwords == 0 && !press_enter)
	{
		fprintf(stderr, "dch: --send needs text\n");
		return 1;
	}
	s = control_connect(name, 0);
	if (s < 0)
		return 1;
	for (k = 0; k < nwords; k++)
	{
		const char *w = words[k];
		size_t l = strlen(w), off;

		for (off = 0; off < l; off += PKT_MAX)
		{
			size_t chunk = l - off > PKT_MAX ? PKT_MAX : l - off;
			send_ctl(s, MSG_PUSH, w + off, chunk);
		}
		if (k + 1 < nwords)
			send_ctl(s, MSG_PUSH, " ", 1);
	}
	if (press_enter)
		send_ctl(s, MSG_PUSH, "\r", 1);
	close(s);
	return 0;
}

/* --spawn --env KEY=VAL collected from argv (strings persist for putenv). */
#define SPAWN_ENV_MAX 32
static char *spawn_env[SPAWN_ENV_MAX];
static int spawn_env_n;

static int
do_spawn_verb(char *exe, const char *name, int cols, int rows,
              char **inner_argv, int inner_argc)
{
	unsigned char payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	struct winsize ws;
	unsigned char req[4] = { DCH_READ_PLAIN, DCH_READ_VISIBLE, 0, 0 };
	unsigned int len;
	struct packet pkt;
	char *default_argv[] = { NULL, NULL };
	struct stat st;
	int s, rc;

	if (make_sock_path(name) < 0)
		return 1;
	if (stat(sock_path, &st) == 0 && S_ISSOCK(st.st_mode))
	{
		/* Live master answers a connect; refuse to clobber it. A
		** stale socket (dead master) just gets unlinked below. */
		int probe = control_connect(name, 1);
		if (probe >= 0)
		{
			close(probe);
			fprintf(stderr, "dch: session already exists: %s\n",
			        name);
			return 1;
		}
	}
	unlink(sock_path);

	if (inner_argc == 0)
	{
		const char *sh = getenv("SHELL");

		default_argv[0] = (char *)(sh && *sh ? sh : "/bin/sh");
		inner_argv = default_argv;
	}
	/* Caller env first; DCH_SESSION/TERM are set after, so reserved keys
	** always win — --env DCH_SESSION=x cannot spoof session identity. */
	for (rc = 0; rc < spawn_env_n; rc++)
		putenv(spawn_env[rc]);
	setenv("DCH_SESSION", name, 1);
	/* Headless spawns often run where TERM is unset or "dumb" (cron, CI,
	** daemons); the mirror is xterm-compatible, so give TUIs a terminal
	** they will actually draw on. */
	{
		const char *term = getenv("TERM");

		if (!term || !*term || strcmp(term, "dumb") == 0)
			setenv("TERM", "xterm-256color", 1);
	}
	if (spawn_master(exe, sock_path, inner_argv) < 0)
	{
		fprintf(stderr, "dch: failed to spawn session\n");
		return 1;
	}

	s = control_connect(name, 0);
	if (s < 0)
		return 1;

	/* Give the child a real grid immediately (headless: no attacher will
	** send a WINCH later). */
	memset(&ws, 0, sizeof(ws));
	ws.ws_col = cols;
	ws.ws_row = rows;
	memset(&pkt, 0, sizeof(pkt));
	pkt.type = MSG_WINCH;
	memcpy(&pkt.u.ws, &ws, sizeof(ws));
	write_packet_or_fail(s, &pkt);

	/* Throwaway read: a control verb opens the -w pty gate, so the
	** child's startup output flows into the mirror instead of blocking
	** on a full kernel pty buffer. The response doubles as "master is
	** alive and answering". */
	send_ctl(s, MSG_READ, req, sizeof(req));
	do
		rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
	while (rc == 1 && type == MSG_READ_DATA);
	close(s);

	if (rc != 1 || type != MSG_READ_END)
	{
		fprintf(stderr, "dch: spawned but no master reply\n");
		return 1;
	}
	printf("%s\n", name);
	return 0;
}

/* ---- main ----------------------------------------------------------- */

int
main(int argc, char **argv)
{
	int forced_name = 0;
	int force = 0;
	int kill_explicit = 0;
	enum { A_ATTACH, A_LIST, A_KILL, A_KILLALL, A_DETACH, A_LISTRAW,
	       A_RENAME, A_LISTJSON, A_LISTJSON2, A_SETALIAS,
	       A_SPAWN, A_SEND, A_RUN, A_KEYS, A_READ, A_WAIT, A_STATUS,
	       A_REPORT }
	    action = A_ATTACH;
	char alias_arg[600] = "";
	int opt_ansi = 0, opt_recent = -1, opt_timeout = 10000;
	int opt_cols = 0, opt_rows = 0;
	char opt_match[DCH_WAIT_MAX + 1] = "";
	char opt_state[40] = "";

	progname = argv[0];
	/* Resolve exe for the master re-exec. Only realpath() when argv[0] is a
	** path (has a '/'): a bare name like "dch" came from a PATH lookup by the
	** shell, and realpath()ing it resolves RELATIVE TO CWD — which silently
	** picks up an unrelated "./dch" (e.g. a `dch/` source dir) and makes the
	** master execvp() a directory ("failed to spawn"). Leaving a bare name as
	** is lets the child's execvp() re-search PATH, which is what we want. */
	char *exe = argv[0];
	if (strchr(argv[0], '/'))
	{
		char *rp = realpath(argv[0], NULL);
		if (rp)
			exe = rp;
	}

	/* Internal sentinel: master-of <sock> -- <cmd...> */
	if (argc >= 3 && strcmp(argv[1], "--master-of") == 0)
		return run_master_of(argc - 2, argv + 2);

	if (compute_sock_dir() < 0)
		return 1;

	int i = 1;
	while (i < argc)
	{
		const char *a = argv[i];
		if (strcmp(a, "--") == 0)
		{
			i++;
			break;
		}
		else if (strcmp(a, "-h") == 0 || strcmp(a, "--help") == 0)
		{
			usage();
			return 0;
		}
		else if (strcmp(a, "-V") == 0 || strcmp(a, "--version") == 0)
		{
			printf("dch %s\n", DCH_VERSION);
			return 0;
		}
		else if (strcmp(a, "-l") == 0)
		{
			action = A_LIST;
			i++;
		}
		else if (strcmp(a, "-ls") == 0)
		{
			action = A_LISTRAW;
			i++;
		}
		else if (strcmp(a, "-lj") == 0)
		{
			action = A_LISTJSON;
			i++;
		}
		else if (strcmp(a, "-rl") == 0)
		{
			action = A_RENAME;
			i++;
		}
		else if (strcmp(a, "-m") == 0)
		{
			/* Non-interactive set/clear alias: -m <name> [alias]. */
			action = A_SETALIAS;
			i++;
			if (i < argc)
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				i++;
			}
			if (i < argc)
			{
				snprintf(alias_arg, sizeof(alias_arg), "%s",
				         argv[i]);
				i++;
			}
		}
		else if (strcmp(a, "-kl") == 0 || strcmp(a, "-lk") == 0)
		{
			action = A_KILLALL;
			i++;
		}
		else if (strcmp(a, "-k") == 0)
		{
			action = A_KILL;
			i++;
			if (i < argc && argv[i][0] != '-')
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				kill_explicit = 1;
				i++;
			}
		}
		else if (strcmp(a, "-d") == 0)
		{
			action = A_DETACH;
			i++;
			if (i < argc && argv[i][0] != '-')
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				kill_explicit = 1;
				i++;
			}
		}
		else if (strcmp(a, "-n") == 0)
		{
			i++;
			if (i >= argc)
			{
				fprintf(stderr, "dch: -n needs a name\n");
				return 1;
			}
			snprintf(session_name, sizeof(session_name),
			         "%s", argv[i]);
			forced_name = 1;
			i++;
		}
		else if (strcmp(a, "-f") == 0)
		{
			force = 1;
			i++;
		}
		else if (strcmp(a, "-E") == 0)
		{
			detach_char = -1;
			i++;
		}
		else if (strcmp(a, "-e") == 0)
		{
			i++;
			if (i >= argc)
			{
				fprintf(stderr, "dch: -e needs a char\n");
				return 1;
			}
			const char *c = argv[i];
			if (c[0] == '^' && c[1])
				detach_char = (c[1] == '?') ? 0177
				                            : (c[1] & 037);
			else
				detach_char = c[0];
			i++;
		}
		else if (strcmp(a, "--ls-json") == 0)
		{
			action = A_LISTJSON2;
			i++;
		}
		else if (strcmp(a, "--spawn") == 0 ||
		         strcmp(a, "--send") == 0 ||
		         strcmp(a, "--run") == 0 ||
		         strcmp(a, "--keys") == 0 ||
		         strcmp(a, "--read") == 0 ||
		         strcmp(a, "--wait") == 0 ||
		         strcmp(a, "--status") == 0 ||
		         strcmp(a, "--report") == 0)
		{
			/* Control verbs: all take a session name next. */
			action = strcmp(a, "--spawn") == 0 ? A_SPAWN
			       : strcmp(a, "--send") == 0  ? A_SEND
			       : strcmp(a, "--run") == 0   ? A_RUN
			       : strcmp(a, "--keys") == 0  ? A_KEYS
			       : strcmp(a, "--read") == 0  ? A_READ
			       : strcmp(a, "--wait") == 0  ? A_WAIT
			       : strcmp(a, "--status") == 0 ? A_STATUS
			                                    : A_REPORT;
			i++;
			if (i >= argc || argv[i][0] == '\0')
			{
				fprintf(stderr,
				        "dch: %s needs a session name\n", a);
				return 1;
			}
			snprintf(session_name, sizeof(session_name),
			         "%s", argv[i]);
			i++;
		}
		else if (strcmp(a, "--ansi") == 0)
		{
			opt_ansi = 1;
			i++;
		}
		else if (strcmp(a, "--recent") == 0)
		{
			/* Optional line count; bare --recent = master default. */
			opt_recent = 0;
			i++;
			if (i < argc && argv[i][0] >= '0' && argv[i][0] <= '9')
			{
				opt_recent = atoi(argv[i]);
				if (opt_recent < 1 || opt_recent > 65535)
				{
					fprintf(stderr, "dch: --recent wants "
					        "1..65535 lines\n");
					return 1;
				}
				i++;
			}
		}
		else if (strcmp(a, "--match") == 0)
		{
			i++;
			if (i >= argc)
			{
				fprintf(stderr,
				        "dch: --match needs a string\n");
				return 1;
			}
			snprintf(opt_match, sizeof(opt_match), "%s", argv[i]);
			i++;
		}
		else if (strcmp(a, "--state") == 0)
		{
			i++;
			if (i >= argc || argv[i][0] == '\0')
			{
				fprintf(stderr,
				        "dch: --state needs a state name\n");
				return 1;
			}
			snprintf(opt_state, sizeof(opt_state), "%s", argv[i]);
			i++;
		}
		else if (strcmp(a, "--timeout") == 0)
		{
			i++;
			if (i >= argc || atoi(argv[i]) < 1)
			{
				fprintf(stderr,
				        "dch: --timeout needs milliseconds\n");
				return 1;
			}
			opt_timeout = atoi(argv[i]);
			i++;
		}
		else if (strcmp(a, "--size") == 0)
		{
			i++;
			if (i >= argc || sscanf(argv[i], "%dx%d",
			    &opt_cols, &opt_rows) != 2 ||
			    opt_cols < 1 || opt_cols > 4096 ||
			    opt_rows < 1 || opt_rows > 4096)
			{
				fprintf(stderr,
				        "dch: --size wants COLSxROWS\n");
				return 1;
			}
			i++;
		}
		else if (strcmp(a, "--env") == 0)
		{
			const char *eq;

			i++;
			if (i >= argc || !(eq = strchr(argv[i], '=')) ||
			    eq == argv[i])
			{
				fprintf(stderr,
				        "dch: --env wants KEY=VAL\n");
				return 1;
			}
			if (spawn_env_n >= SPAWN_ENV_MAX)
			{
				fprintf(stderr,
				        "dch: --env: too many (max %d)\n",
				        SPAWN_ENV_MAX);
				return 1;
			}
			spawn_env[spawn_env_n++] = argv[i];
			i++;
		}
		else if (strcmp(a, "--") == 0)
		{
			i++;
			break;
		}
		else if (a[0] == '-' && a[1] != '\0')
		{
			fprintf(stderr, "dch: unknown flag %s\n", a);
			return 1;
		}
		else
			break;
	}

	/* Inner command after flags. argv is already NULL-terminated by libc. */
	int inner_argc = argc - i;
	char **inner_argv = argv + i;

	/* Auto-name only where the action needs a specific session and the
	** caller didn't already supply one. */
	if (session_name[0] == '\0')
	{
		if (action == A_ATTACH ||
		    (action == A_KILL && kill_explicit) ||
		    (action == A_DETACH && kill_explicit))
		{
			auto_name(session_name, sizeof(session_name));
		}
	}

	switch (action)
	{
	case A_LISTRAW:
	{
		struct slist sl = {0};
		list_sessions(&sl);
		int k;
		for (k = 0; k < sl.n; k++)
			puts(sl.v[k]);
		slist_free(&sl);
		return 0;
	}
	case A_LISTJSON2:
	{
		/* Real JSON for agents. Names/aliases are tab- and
		** newline-free (enforced at creation), so escaping only has
		** to cover quotes and backslashes. */
		struct slist sl = {0};
		list_sessions(&sl);
		load_aliases(&sl);
		int k;
		putchar('[');
		for (k = 0; k < sl.n; k++)
		{
			const char *f;
			printf("%s{\"name\":\"", k ? "," : "");
			for (f = sl.v[k]; *f; f++)
				printf(*f == '"' || *f == '\\' ? "\\%c" : "%c", *f);
			printf("\",\"alias\":\"");
			for (f = sl.alias[k] ? sl.alias[k] : ""; *f; f++)
				printf(*f == '"' || *f == '\\' ? "\\%c" : "%c", *f);
			printf("\",\"activity_epoch\":%ld,\"state\":\"%s\"}",
			       activity_epoch(sl.v[k]),
			       session_state(sl.v[k]));
		}
		puts("]");
		slist_free(&sl);
		return 0;
	}
	case A_LISTJSON:
	{
		/* Machine list: one "name\talias\tactivity_epoch" per line (alias
		** empty when none, epoch 0 when no output yet). Tab-separated, not
		** JSON — no escaping needed since names can't contain tabs and
		** set-alias strips them. */
		struct slist sl = {0};
		list_sessions(&sl);
		load_aliases(&sl);
		int k;
		for (k = 0; k < sl.n; k++)
			printf("%s\t%s\t%ld\n", sl.v[k],
			       sl.alias[k] ? sl.alias[k] : "",
			       activity_epoch(sl.v[k]));
		slist_free(&sl);
		return 0;
	}
	case A_SETALIAS:
	{
		if (session_name[0] == '\0')
		{
			fprintf(stderr, "dch: -m needs a session name\n");
			return 1;
		}
		/* Keep aliases single-line and tab-free so -lj stays parseable. */
		for (char *p = alias_arg; *p; p++)
			if (*p == '\t' || *p == '\n' || *p == '\r')
				*p = ' ';
		return write_alias(session_name, alias_arg) == 0 ? 0 : 1;
	}
	case A_LIST:
	{
		char chosen[600];
		int rc = pick_session("attach session (mirror):",
		                      chosen, sizeof(chosen));
		if (rc == 0)
		{
			snprintf(session_name, sizeof(session_name),
			         "%s", chosen);
			return do_attach(exe, 1, 1, inner_argv, inner_argc);
		}
		return 0;
	}
	case A_KILL:
	{
		if (!kill_explicit && !forced_name)
		{
			char chosen[600];
			int rc = pick_session("kill session:", chosen,
			                      sizeof(chosen));
			if (rc != 0)
				return 0;
			snprintf(session_name, sizeof(session_name),
			         "%s", chosen);
		}
		return kill_session(session_name);
	}
	case A_RENAME:
		return rename_sessions_interactive();
	case A_KILLALL:
		return kill_all();
	case A_DETACH:
	{
		if (!kill_explicit && !forced_name)
		{
			char chosen[600];
			int rc = pick_session("detach session:", chosen,
			                      sizeof(chosen));
			if (rc != 0)
				return 0;
			snprintf(session_name, sizeof(session_name),
			         "%s", chosen);
		}
		char sp[1100];
		session_sock_path(session_name, sp, sizeof(sp));
		struct stat st;
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		int n = detach_all_clients_on(sp);
		printf("detached %d client%s of: %s\n",
		       n, n == 1 ? "" : "s", session_name);
		return 0;
	}
	case A_READ:
		return do_read_verb(session_name, opt_ansi, opt_recent);
	case A_STATUS:
	{
		char sp[1100];
		struct stat st;
		session_sock_path(session_name, sp, sizeof(sp));
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		puts(session_state(session_name));
		return 0;
	}
	case A_REPORT:
	{
		char sp[1100], pp[1300];
		struct stat st;
		const char *tok = inner_argc == 1 ? inner_argv[0] : "";
		/* Closed set, rejected at the boundary (herdr-style): an
		** unknown token writes nothing and fails loudly here, so
		** consumers of --status never see a surprise state. */
		if (!valid_state_token(tok) && strcmp(tok, "clear") != 0)
		{
			fprintf(stderr, "dch: --report needs one state: "
			        "working|idle|blocked|done, or `clear`\n");
			return 1;
		}
		session_sock_path(session_name, sp, sizeof(sp));
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		if (state_file_path(session_name, pp, sizeof(pp)) != 0)
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		if (strcmp(tok, "clear") == 0)
		{
			unlink(pp); /* back to the output heuristic */
			return 0;
		}
		/* Atomic replace (write temp + rename): a concurrent --status
		** never reads a torn file; last writer wins, which is the
		** right semantic for ordered harness hooks. */
		char tp[1320];
		size_t tl = strlen(tok);
		snprintf(tp, sizeof(tp), "%s.%d.tmp", pp, (int)getpid());
		int fd = open(tp, O_WRONLY | O_CREAT | O_TRUNC, 0600);
		int wrote = fd >= 0 && write(fd, tok, tl) == (ssize_t)tl;
		if (fd >= 0 && close(fd) != 0)
			wrote = 0;
		if (!wrote || rename(tp, pp) != 0)
		{
			if (fd >= 0)
				unlink(tp);
			fprintf(stderr, "dch: cannot write state for %s\n",
			        session_name);
			return 1;
		}
		return 0;
	}
	case A_WAIT:
		if (opt_state[0] != '\0')
		{
			/* State wait: poll the sidecar-backed state every 100ms
			** (herdr's cadence). --state takes a comma list and
			** matches any of it, e.g. --state idle,blocked,done =
			** "the turn is over". Works in both variants (no mirror
			** needed). The session must exist on every poll — a
			** killed session ends the wait with "no session"
			** instead of burning the timeout. */
			char sp[1100];
			struct stat st;
			struct timeval w0, wn;
			int ntok = 0, t;
			char *want[8], *tok, *rest;
			for (tok = strtok_r(opt_state, ",", &rest);
			     tok && ntok < 8;
			     tok = strtok_r(NULL, ",", &rest))
			{
				/* "active" is the v1.3 name for the busy
				** state; map it in the compare, not just the
				** validator, or waiters on it never fire. */
				if (strcmp(tok, "active") == 0)
					tok = (char *)"working";
				else if (!valid_state_token(tok))
				{
					fprintf(stderr, "dch: --state wants "
					        "active|working|idle|blocked|"
					        "done (comma list ok)\n");
					return 1;
				}
				want[ntok++] = tok;
			}
			if (ntok == 0)
			{
				fprintf(stderr,
				        "dch: --state needs a state name\n");
				return 1;
			}
			gettimeofday(&w0, NULL);
			for (;;)
			{
				session_sock_path(session_name, sp,
				                  sizeof(sp));
				if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
				{
					fprintf(stderr, "no session: %s\n",
					        session_name);
					return 1;
				}
				const char *cur =
				    session_state(session_name);
				for (t = 0; t < ntok; t++)
					if (strcmp(cur, want[t]) == 0)
					{
						puts(cur);
						return 0;
					}
				/* wall-clock, not loop-count: detection
				** adds up to DET_DEADLINE_MS per poll and a
				** fixed increment would blow the budget */
				gettimeofday(&wn, NULL);
				if ((wn.tv_sec - w0.tv_sec) * 1000 +
				    (wn.tv_usec - w0.tv_usec) / 1000 >=
				    opt_timeout)
					return 2;
				poll(NULL, 0, 100);
			}
		}
		if (opt_match[0] == '\0')
		{
			fprintf(stderr,
			        "dch: --wait needs --match or --state\n");
			return 1;
		}
		return do_wait_verb(session_name, opt_match, opt_timeout);
	case A_KEYS:
		return do_keys_verb(session_name, inner_argv, inner_argc);
	case A_SEND:
		return do_send_verb(session_name, inner_argv, inner_argc, 0);
	case A_RUN:
		return do_send_verb(session_name, inner_argv, inner_argc, 1);
	case A_SPAWN:
	{
		if (opt_cols == 0)
		{
			/* Headless spawn: caller's env is the best size hint. */
			const char *c = getenv("COLUMNS"), *l = getenv("LINES");

			opt_cols = c ? atoi(c) : 0;
			opt_rows = l ? atoi(l) : 0;
			if (opt_cols < 1 || opt_cols > 4096 ||
			    opt_rows < 1 || opt_rows > 4096)
			{
				opt_cols = 80;
				opt_rows = 24;
			}
		}
		return do_spawn_verb(exe, session_name, opt_cols, opt_rows,
		                     inner_argv, inner_argc);
	}
	case A_ATTACH:
		return do_attach(exe, forced_name, force,
		                 inner_argv, inner_argc);
	}
	return 0;
}
