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
#ifdef __APPLE__
#include <libproc.h>
#include <sys/sysctl.h>
#endif

/* DCH_VERSION lives in dtach.h — the master needs it for the .ver sidecar. */

/* Shared globals (declared in dtach.h, used by attach.c/master.c). */
const char copyright[] = "dch " DCH_VERSION " - based on dtach"
                         " (C)Copyright 2004-2016 Ned T. Crigler";
char *progname;
char *sockname;
int detach_char = '\\' - 64;
int no_suspend;
int redraw_method = REDRAW_UNSPEC;
struct termios orig_term;
int dont_have_tty;
char *dch_exe;
char **dch_argv;

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
** PUSH/KEYS/READ/WAIT/RESTART/RESPAWN = pkt->len bytes; WINCH/REDRAW = a
** winsize; ATTACH/DETACH/CMD = none. */
size_t
packet_payload_len(const struct packet *pkt)
{
	if (pkt->type == MSG_PUSH || pkt->type == MSG_KEYS ||
	    pkt->type == MSG_READ || pkt->type == MSG_WAIT ||
	    pkt->type == MSG_RESTART || pkt->type == MSG_RESPAWN)
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
	/* EEXIST is the common case, so the directory we just accepted may be
	** one somebody else created. Owning it means owning the names in it:
	** the socket, and now the resume blob a live restart hands to the next
	** image. Sockets and blobs are 0600 and validated, but a name an
	** attacker can unlink between save and exec still kills the session. */
	{
		struct stat st;

		/* Write access is the whole threat: the files in here are 0600
		** and validated on read, so being able to list the directory
		** costs nothing. Being able to create or unlink names in it is
		** what turns a restart into a lost session. */
		if (lstat(sock_dir, &st) < 0 || !S_ISDIR(st.st_mode) ||
		    st.st_uid != uid || (st.st_mode & (S_IWGRP | S_IWOTH)))
		{
			fprintf(stderr, "dch: %s must be a directory owned by "
			        "uid %u and not writable by group or other\n",
			        sock_dir, (unsigned)uid);
			return -1;
		}
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
	/* A session switch calls this again in the same process: drop the
	** previous session's marker, or live_clients_on() counts us as still
	** attached there forever. */
	if (client_pidfile[0])
		unlink(client_pidfile);
	snprintf(client_pidfile, sizeof(client_pidfile),
	         "%s/%s.client.%d", sock_dir, bn, (int)getpid());
	int fd = open(client_pidfile, O_CREAT | O_WRONLY | O_TRUNC, 0600);
	if (fd >= 0)
		close(fd);
	{
		static int trapped;

		if (!trapped)
		{
			trapped = 1;
			atexit(client_pidfile_unlink);
		}
	}
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
	char **v;       /* real session names (socket basenames) */
	char **alias;   /* display alias or NULL; parallel to v */
	char **harness; /* live Claude Code or Codex session name, or NULL */
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
		l->harness = realloc(l->harness, l->cap * sizeof(char *));
	}
	l->alias[l->n] = NULL;
	l->harness[l->n] = NULL;
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
		free(l->harness[i]);
	}
	free(l->v);
	free(l->alias);
	free(l->harness);
	l->v = NULL;
	l->alias = NULL;
	l->harness = NULL;
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

/* dch version of the master serving this session, from the `<sock>.ver`
** sidecar it stamps at startup. Returns NULL when the file is missing or
** unreadable — i.e. the master predates the sidecar, so "older than us" is the
** only safe reading. Static buffer; valid until the next call. */
static const char *
session_version(const char *name)
{
	static char ver[64];
	char p[1300], cb[256];
	int fd, r, n;

	n = snprintf(p, sizeof(p), "%s/%s.sock.ver", sock_dir,
	             canon_name(name, cb, sizeof(cb)));
	if (n < 0 || (size_t)n >= sizeof(p))
		return NULL;
	/* NOFOLLOW+NONBLOCK+S_ISREG: the runtime dir is shared, and a symlink
	** or a FIFO planted at this name would otherwise redirect the read or
	** hang it forever. */
	fd = open(p, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
	if (fd < 0)
		return NULL;
	{
		struct stat st;

		if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
		    st.st_uid != getuid())
		{
			close(fd);
			return NULL;
		}
	}
	r = (int)read(fd, ver, sizeof(ver) - 1);
	close(fd);
	if (r <= 0)
		return NULL;
	ver[r] = '\0';
	r = (int)strcspn(ver, "\r\n");
	ver[r] = '\0';
	/* Reject anything that isn't a plain version token: this string ends up
	** in --ls-json, and the sidecar lives in a world-listable runtime dir. */
	if (r == 0 || (size_t)r != strspn(ver, "0123456789.-abcdefghijklmnop"
	                                       "qrstuvwxyz"))
		return NULL;
	return ver;
}

static const char *
version_or_empty(const char *name)
{
	const char *v = session_version(name);

	return v ? v : "";
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

/* ---- harness session names ------------------------------------------ */

/* Fetch one environment value from another same-uid process. macOS uses
** KERN_PROCARGS2; elsewhere /proc/<pid>/environ. */
static int
proc_env_value(long pid, const char *key, char *out, size_t outsz)
{
	size_t keylen = strlen(key);
#ifdef __APPLE__
	static char *buf;
	static size_t bufsz;
	int mib[3] = { CTL_KERN, KERN_PROCARGS2, (int)pid };
	size_t len, i;

	if (!buf)
	{
		int am[2] = { CTL_KERN, KERN_ARGMAX };
		int argmax = 0;
		len = sizeof(argmax);
		if (sysctl(am, 2, &argmax, &len, NULL, 0) != 0 || argmax <= 0)
			argmax = 256 * 1024;
		bufsz = (size_t)argmax;
		buf = malloc(bufsz);
		if (!buf)
			return -1;
	}
	len = bufsz;
	if (sysctl(mib, 3, buf, &len, NULL, 0) != 0 || len == 0)
		return -1;
	buf[len - 1] = '\0';
	/* Layout: argc int, exec path, NULs, argv[], env[]. Entries are
	** NUL-separated; scan each entry start for our prefix. */
	for (i = sizeof(int); i < len; )
	{
		if (len - i > keylen + 1 && memcmp(buf + i, key, keylen) == 0 &&
		    buf[i + keylen] == '=')
		{
			snprintf(out, outsz, "%s", buf + i + keylen + 1);
			return 0;
		}
		while (i < len && buf[i] != '\0')
			i++;
		while (i < len && buf[i] == '\0')
			i++;
	}
	return -1;
#else
	char p[64], buf[65536];
	int fd, r;
	size_t off = 0, i;

	snprintf(p, sizeof(p), "/proc/%ld/environ", pid);
	fd = open(p, O_RDONLY);
	if (fd < 0)
		return -1;
	while (off < sizeof(buf) - 1 &&
	       (r = (int)read(fd, buf + off, sizeof(buf) - 1 - off)) > 0)
		off += (size_t)r;
	close(fd);
	buf[off] = '\0';
	for (i = 0; i < off; i += strlen(buf + i) + 1)
	{
		if (strncmp(buf + i, key, keylen) == 0 && buf[i + keylen] == '=')
		{
			snprintf(out, outsz, "%s", buf + i + keylen + 1);
			return 0;
		}
	}
	return -1;
#endif
}

static int
proc_dch_session(long pid, char *out, size_t outsz)
{
	return proc_env_value(pid, "DCH_SESSION", out, outsz);
}

static int
proc_is_codex(long pid)
{
	char path[4096];
	const char *base;
#ifdef __APPLE__
	if (proc_pidpath((int)pid, path, sizeof(path)) <= 0)
		return 0;
#else
	char proc[64];
	ssize_t n;

	snprintf(proc, sizeof(proc), "/proc/%ld/exe", pid);
	n = readlink(proc, path, sizeof(path) - 1);
	if (n <= 0)
		return 0;
	path[n] = '\0';
#endif
	base = strrchr(path, '/');
	base = base ? base + 1 : path;
	return strcmp(base, "codex") == 0 ||
	       strcmp(base, "codex-code-mode-host") == 0;
}

int
dch_visit_claude_records(dch_claude_record_fn visit, void *arg)
{
	const char *base = getenv("CLAUDE_CONFIG_DIR"), *home = getenv("HOME");
	char dir[1100];
	DIR *d;
	struct dirent *de;

	if ((!base || !*base) && (!home || !*home))
		return 0;
	if (snprintf(dir, sizeof dir, "%s%s/sessions", base && *base ? base : home,
	             base && *base ? "" : "/.claude") >= (int)sizeof dir ||
	    !(d = opendir(dir)))
		return 0;
	while ((de = readdir(d)))
	{
		char path[1400], body[8192], extra;
		struct stat before, after;
		size_t len = 0, n = strlen(de->d_name);
		int fd;
		if (n < 6 || strcmp(de->d_name + n - 5, ".json") ||
		    snprintf(path, sizeof path, "%s/%s", dir, de->d_name) >=
		        (int)sizeof path)
			continue;
		fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
		if (fd < 0 || fstat(fd, &before) < 0 || !S_ISREG(before.st_mode) ||
		    before.st_uid != getuid() || (before.st_mode & 022) ||
		    before.st_size <= 0 || before.st_size >= (off_t)sizeof body)
		{
			if (fd >= 0) close(fd);
			continue;
		}
		while (len < (size_t)before.st_size)
		{
			ssize_t r = read(fd, body + len, (size_t)before.st_size - len);
			if (r > 0) len += (size_t)r;
			else if (r < 0 && errno == EINTR) continue;
			else break;
		}
		if (len != (size_t)before.st_size || read(fd, &extra, 1) != 0 ||
		    fstat(fd, &after) < 0 || after.st_size != before.st_size ||
		    after.st_dev != before.st_dev || after.st_ino != before.st_ino ||
		    memchr(body, '\0', len))
		{
			close(fd);
			continue;
		}
		close(fd);
		body[len] = '\0';
		if (visit(path, body, len, arg) < 0)
		{
			closedir(d);
			return -1;
		}
	}
	closedir(d);
	return 0;
}

/* Best-effort overlay of the Claude Code session name running inside each
** dch session. The harness writes ~/.claude/sessions/<pid>.json for every
** live claude process; that process's DCH_SESSION env names the dch session
** exactly, so the join needs no cwd heuristics. Auto-titled harness names
** ("nameSource":"derived") are skipped — "dch-28" beats no one. Explicit
** dch aliases still win in the picker. */
static int
load_harness_record(const char *path, const char *body, size_t body_len,
                    void *arg)
{
	struct slist *sl = arg;
	char jb[8192], sess[600], cb[256], *nm, *q, *endp;
	const char *base = strrchr(path, '/'), *cn;
	long pid;
	int i;
	if (body_len >= sizeof jb) return 0;
	memcpy(jb, body, body_len + 1);
	base = base ? base + 1 : path;
	pid = strtol(base, &endp, 10);
	if (pid <= 0 || strcmp(endp, ".json") ||
	    strstr(jb, "\"nameSource\":\"derived\"")) return 0;
	nm = strstr(jb, "\"name\":\"");
	if (!nm || !(q = strchr(nm += 8, '"')) || q == nm) return 0;
	*q = '\0';
	for (char *c = nm; *c; c++)
		if (*c == '\t' || *c == '\n' || *c == '\r') *c = ' ';
	if (proc_dch_session(pid, sess, sizeof sess) != 0) return 0;
	cn = canon_name(sess, cb, sizeof cb);
	for (i = 0; i < sl->n; i++)
		if (!sl->harness[i] && strcmp(sl->v[i], cn) == 0)
		{
			sl->harness[i] = strdup(nm);
			break;
		}
	return 0;
}

static void
load_harness_names(struct slist *sl)
{
	if (sl->n) (void)dch_visit_claude_records(load_harness_record, sl);
}

/* Look up a Codex thread's current user-visible name. session_index.jsonl
** has no PID, so callers establish liveness separately and obtain the ID
** from the process environment or its shell snapshot. Codex does not record
** whether a title was auto-derived; only empty/malformed titles can be
** safely skipped. */
int
dch_codex_thread_name(const char *home, const char *id, char *out, size_t outsz)
{
	char path[1200], buf[65536], *line, *next;
	struct stat st;
	int fd, r, tail = 0;

	snprintf(path, sizeof(path), "%s/session_index.jsonl", home);
	fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
	if (fd < 0)
		return -1;
	if (fstat(fd, &st) == 0 && st.st_size >= (off_t)sizeof(buf) - 1)
	{
		(void)lseek(fd, -((off_t)sizeof(buf) - 1), SEEK_END);
		tail = 1;
	}
	r = (int)read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (r <= 0)
		return -1;
	buf[r] = '\0';
	out[0] = '\0';
	line = tail ? strchr(buf, '\n') : buf;
	if (tail && !line)
		return -1;
	if (tail)
		line++;
	for (; line && *line; line = next)
	{
		char *p, *q, *nm, *end;
		next = strchr(line, '\n');
		if (next)
			*next++ = '\0';
		p = strstr(line, "\"id\":\"");
		if (!p)
			continue;
		p += 6;
		q = strchr(p, '"');
		if (!q || (size_t)(q - p) != strlen(id) || strncmp(p, id, q - p))
			continue;
		nm = strstr(q, "\"thread_name\":\"");
		if (!nm)
			continue;
		nm += 15;
		end = strchr(nm, '"');
		if (!end || end == nm || strchr(nm, '\\') ||
		    (size_t)(end - nm) >= outsz)
			continue;
		memcpy(out, nm, (size_t)(end - nm));
		out[end - nm] = '\0';
	}
	return out[0] ? 0 : -1;
}

/* Current Codex releases no longer export the thread ID to the long-lived
** process. Its shell snapshot still joins that ID (the filename prefix) to
** the inherited DCH_SESSION. Prefer the newest exact match. */
int
dch_codex_snapshot_id(const char *home, const char *sess, const char *marker,
                      char *out, size_t outsz, int strict)
{
	char dir[1200], want[700], mark[700];
	struct dirent *de;
	struct stat best = {0};
	DIR *d;
	int ambiguous = 0;
	char strict_id[128] = "";

	if (snprintf(dir, sizeof(dir), "%s/shell_snapshots", home) >=
	    (int)sizeof(dir))
		return -1;
	snprintf(want, sizeof(want), "\nexport DCH_SESSION=%s\n", sess);
	if (marker)
		snprintf(mark, sizeof(mark), "\nexport %s=%s\n",
		         "DCH_NATIVE_BRIDGE_ID", marker);
	d = opendir(dir);
	if (!d)
		return -1;
	while ((de = readdir(d)))
	{
		char path[1500], buf[262144], *dot;
		struct stat st;
		int fd, r;
		size_t n = strlen(de->d_name);

		dot = strchr(de->d_name, '.');
		if (n < 4 || !dot || (size_t)(dot - de->d_name) >= outsz ||
		    strcmp(de->d_name + n - 3, ".sh") != 0)
			continue;
		if (strict)
		{
			int valid = (size_t)(dot - de->d_name) == 36;
			for (int j = 0; valid && j < 36; j++)
				if (j == 8 || j == 13 || j == 18 || j == 23)
					valid = de->d_name[j] == '-';
				else
					valid = isxdigit((unsigned char)de->d_name[j]);
			if (!valid)
				continue;
		}
		snprintf(path, sizeof(path), "%s/%s", dir, de->d_name);
		fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
		if (fd < 0 || fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) ||
		    st.st_uid != getuid() || (strict && (st.st_mode & 022)) ||
		    st.st_size <= 0 || st.st_size >= (off_t)sizeof(buf))
		{
			if (fd >= 0) close(fd);
			continue;
		}
		if (strict)
		{
			struct stat after;
			char extra;
			size_t off = 0;
			while (off < (size_t)st.st_size)
			{
				ssize_t got = read(fd, buf + off, (size_t)st.st_size - off);
				if (got > 0) off += (size_t)got;
				else if (got < 0 && errno == EINTR) continue;
				else break;
			}
			r = (int)off;
			if (off != (size_t)st.st_size || read(fd, &extra, 1) != 0 ||
			    fstat(fd, &after) < 0 || after.st_size != st.st_size ||
			    after.st_dev != st.st_dev || after.st_ino != st.st_ino ||
			    memchr(buf, '\0', off)) r = -1;
		}
		else
			r = (int)read(fd, buf, sizeof(buf) - 1);
		close(fd);
		if (r <= 0)
			continue;
		buf[r] = '\0';
		if (!strstr(buf, want) || (marker && !strstr(buf, mark)))
			continue;
		if (strict)
		{
			size_t idlen = (size_t)(dot - de->d_name);
			if (!strict_id[0])
			{
				memcpy(strict_id, de->d_name, idlen);
				strict_id[idlen] = '\0';
				memcpy(out, strict_id, idlen + 1);
				best = st;
			}
			else if (strlen(strict_id) != idlen ||
			         strncmp(strict_id, de->d_name, idlen) != 0)
				ambiguous = 1;
			continue;
		}
		if (best.st_mtime)
		{
#ifdef __APPLE__
			if (st.st_mtimespec.tv_sec < best.st_mtimespec.tv_sec ||
			    (st.st_mtimespec.tv_sec == best.st_mtimespec.tv_sec &&
			     st.st_mtimespec.tv_nsec < best.st_mtimespec.tv_nsec))
				continue;
			ambiguous = st.st_mtimespec.tv_sec == best.st_mtimespec.tv_sec &&
			            st.st_mtimespec.tv_nsec == best.st_mtimespec.tv_nsec;
#else
			if (st.st_mtim.tv_sec < best.st_mtim.tv_sec ||
			    (st.st_mtim.tv_sec == best.st_mtim.tv_sec &&
			     st.st_mtim.tv_nsec < best.st_mtim.tv_nsec))
				continue;
			ambiguous = st.st_mtim.tv_sec == best.st_mtim.tv_sec &&
			            st.st_mtim.tv_nsec == best.st_mtim.tv_nsec;
#endif
		}
		else
			ambiguous = 0;
		memcpy(out, de->d_name, (size_t)(dot - de->d_name));
		out[dot - de->d_name] = '\0';
		best = st;
	}
	closedir(d);
	return best.st_mtime && !(strict && ambiguous) ? 0 : -1;
}

static void
load_codex_pid(struct slist *sl, const char *home, long pid)
{
	char id[128], sess[600], title[600], cb[256], codexdir[1200];
	const char *cn;
	int i, target = -1;

	if (!proc_is_codex(pid) ||
	    proc_dch_session(pid, sess, sizeof(sess)) != 0)
		return;
	cn = canon_name(sess, cb, sizeof(cb));
	for (i = 0; i < sl->n; i++)
		if (!sl->harness[i] && strcmp(sl->v[i], cn) == 0)
		{
			target = i;
			break;
		}
	if (getenv("CODEX_HOME") && getenv("CODEX_HOME")[0])
		snprintf(codexdir, sizeof codexdir, "%s", getenv("CODEX_HOME"));
	else
		snprintf(codexdir, sizeof codexdir, "%s/.codex", home);
	if (target < 0 ||
	    (proc_env_value(pid, "CODEX_THREAD_ID", id, sizeof(id)) != 0 &&
	     proc_env_value(pid, "CODEX_SESSION_ID", id, sizeof(id)) != 0 &&
	     dch_codex_snapshot_id(codexdir, sess, NULL, id, sizeof(id), 0) != 0) ||
	    dch_codex_thread_name(codexdir, id, title, sizeof(title)) != 0)
		return;
	for (char *c = title; *c; c++)
		if (*c == '\t' || *c == '\n' || *c == '\r')
			*c = ' ';
	sl->harness[target] = strdup(title);
}

/* A genuine live Codex process supplies DCH_SESSION; its environment or
** matching shell snapshot supplies the thread ID. The title index alone is
** never a liveness signal. */
static void
load_codex_names(struct slist *sl)
{
	const char *home = getenv("HOME");

	if (sl->n == 0 || !home || !home[0])
		return;
#ifdef __APPLE__
	int pids[4096], n, i;
	n = proc_listpids(PROC_ALL_PIDS, 0, pids, sizeof(pids));
	if (n <= 0)
		return;
	for (i = 0; i < n / (int)sizeof(pids[0]); i++)
		if (pids[i] > 0)
			load_codex_pid(sl, home, pids[i]);
#else
	DIR *d = opendir("/proc");
	struct dirent *de;
	if (!d)
		return;
	while ((de = readdir(d)))
	{
		char *end;
		long pid = strtol(de->d_name, &end, 10);
		if (pid > 0 && *end == '\0')
			load_codex_pid(sl, home, pid);
	}
	closedir(d);
#endif
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

static void
session_display_name(struct slist *sl, int i, char *out, size_t outsz)
{
	const char *dn = sl->alias[i] ? sl->alias[i] : sl->harness[i];

	if (dn)
		snprintf(out, outsz, "%s (%s)", dn, sl->v[i]);
	else
		snprintf(out, outsz, "%s", sl->v[i]);
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
		session_display_name(sl, i, row, sizeof(row));
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
	load_harness_names(&sl);
	load_codex_names(&sl);

	int fd = open("/dev/tty", O_RDWR);
	if (fd < 0)
	{
		/* Headless: list names so the caller can still see them. */
		for (int i = 0; i < sl.n; i++)
		{
			char row[1300];
			session_display_name(&sl, i, row, sizeof(row));
			puts(row);
		}
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
	    "  dch --read <name> --cursor                 screen on stdout, plus\n"
	    "                                             `cursor <row> <col>\n"
	    "                                             <visible> <wrap>` (1-based)\n"
	    "                                             on stderr\n"
	    "  dch --restart <name>|--all [-f]            re-exec the session's\n"
	    "                                             master onto this dch\n"
	    "                                             binary, in place; the\n"
	    "                                             program keeps running.\n"
	    "                                             --all skips sessions\n"
	    "                                             already current (-f\n"
	    "                                             restarts them anyway)\n"
	    "  dch --respawn <name> [-- cmd...]           when the program exits,\n"
	    "                                             keep the session and\n"
	    "                                             start cmd in its place;\n"
	    "                                             no cmd = hold childless\n"
	    "                                             until a second --respawn\n"
	    "                                             brings one (one-shot)\n"
	    "  dch --respawn <name> --cancel              drop a pending respawn\n"
	    "  dch --cmd <name>                           JSON: child pid, state,\n"
	    "                                             argv, last exit status,\n"
	    "                                             output since the arm\n"
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
	    "  dch --agent-list [--json]                  list native Claude peers\n"
	    "  dch --agent-send <name> <message...>       message one native peer\n"
	    "Env: DCH_NO_DETECT=1 disables screen-content state detection.\n"
	    "     DCH_DOUBLE_TAP_MS sets the switch double-press window\n"
	    "     (default 300; 0 disables switching, detach becomes instant).\n"
	    "Detach: Ctrl-\\  (or `dch -d` if the host swallows it).\n"
	    "Switch: Ctrl-\\ twice quickly — detach and pick another session.\n"
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
		/* Returns only if connect() failed, or the user asked to
		** switch sessions — the latter is a live session, not a dead
		** socket, so it must not fall through to the respawn. */
		if (attach_main(1) == ATTACH_SWITCH)
			return ATTACH_SWITCH;
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

/* do_attach(), plus the double-tap-detach session switcher: when the attached
** client returns ATTACH_SWITCH, show the same picker as `dch -l` and attach to
** whatever the user selects. Quitting the picker just ends the loop, leaving
** the previous session detached and running. */
static int
attach_with_switch(char *exe, int forced_name, int force, char **inner_argv,
                   int inner_argc)
{
	for (;;)
	{
		char chosen[600];
		int rc = do_attach(exe, forced_name, force, inner_argv,
		                   inner_argc);

		if (rc != ATTACH_SWITCH)
			return rc;
		if (pick_session("switch to session:", chosen,
		                 sizeof(chosen)) != 0)
			return 0;
		snprintf(session_name, sizeof(session_name), "%s", chosen);
		forced_name = 1;
		force = 1;
	}
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
/* A master that explicitly answers DCH_ST_UNSUP is provably old — it spoke
** the control protocol well enough to say "I don't know that verb". */
static const char old_master_msg[] =
    "dch: session master is older than this dch and does not support that\n"
    "     command — try `dch --restart <session>`; if that fails too, the\n"
    "     master predates live restart and the session must be recreated\n";
/* ctl_dead_exit's got_any==false case: total silence within CTL_IDLE_MS.
** That does NOT prove an old master — connect() succeeds via the kernel
** listen backlog even when the master never accept()s, so a wedged or
** stopped (SIGSTOP/App Nap) master looks identical on the wire. Name both
** causes instead of guessing. */
static const char no_reply_msg[] =
    "dch: session master is not answering — it may be wedged or stopped\n"
    "     (SIGSTOP, App Nap), or too old to speak this dch's control\n"
    "     protocol. Check whether its process is alive or stuck (e.g. ps)\n"
    "     before recreating anything; try `dch --restart <session>` first\n";

static int
control_connect_path(const char *path)
{
	struct sockaddr_un sa;
	int s;

	if (strlen(path) > sizeof(sa.sun_path) - 1)
		return -1;
	s = socket(PF_UNIX, SOCK_STREAM, 0);
	if (s < 0)
		return -1;
	memset(&sa, 0, sizeof(sa));
	sa.sun_family = AF_UNIX;
	strcpy(sa.sun_path, path);
	if (connect(s, (struct sockaddr *)&sa, sizeof(sa)) < 0)
	{
		close(s);
		return -1;
	}
	return s;
}

static int
control_connect(const char *name, int quiet)
{
	int s;

	if (make_sock_path(name) < 0)
		return -1;
	s = control_connect_path(sock_path);
	if (s < 0 && !quiet)
		fprintf(stderr, "dch: no session: %s\n", name);
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
			     r->buf[0] == MSG_WAIT_HIT ||
			     r->buf[0] == MSG_READ_CURSOR) ? flen : 0;

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
	case DCH_ST_UNSUP:
		fputs(old_master_msg, stderr);
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
		fputs(no_reply_msg, stderr);
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

/* Fetch the visible screen (plain, or with SGR when ansi) into buf (cap
** bytes + NUL). Keeps the TAIL when the stream exceeds cap — detection
** reads bottom lines (the master itself tail-caps at 2MB). The deadline is wall-clock:
** ctl_next_frame's timeout is inactivity-based, and a master trickling
** bytes must not stall --ls-json. Returns content length, -1 on any
** failure. */
static int
fetch_screen(const char *path, char *buf, size_t cap, int deadline_ms,
             int ansi)
{
	unsigned char req[4], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	struct timeval t0, now;
	unsigned int len;
	size_t used = 0;
	int s, rc;

	s = control_connect_path(path);
	if (s < 0)
		return -1;
	req[0] = ansi ? DCH_READ_ANSI : DCH_READ_PLAIN;
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
			if (len == DCH_ST_NOVT)
				return -2;
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

/* The last DET_LINES non-empty lines of screen text raw[0..n), folded to
** lowercase ASCII (UTF-8 bytes untouched), or NULL. Caller frees. */
static char *
tail_fold(const char *raw, size_t n)
{
	const char *end, *p;
	char *fold;
	size_t rl, k;
	int lines;

	end = raw + n;
	p = end;
	lines = 0;
	while (p > raw && lines < DET_LINES)
	{
		const char *ls = p, *q;
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
		return NULL;
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
	return fold;
}

/* The last DET_LINES non-empty visible lines, folded (tail_fold), or NULL
** when there is no screen. *novt is set when the master has no terminal
** mirror. Caller frees. */
static char *
screen_tail(const char *path, int *novt)
{
	char *raw, *fold;
	int n;

	*novt = 0;
	raw = malloc(DET_CAP + 1);
	if (!raw)
		return NULL;
	n = fetch_screen(path, raw, DET_CAP, DET_DEADLINE_MS, 0);
	if (n <= 0)
	{
		*novt = n == -2;
		free(raw);
		return NULL;
	}
	fold = tail_fold(raw, (size_t)n);
	free(raw);
	return fold;
}

/* Classify the visible screen: "blocked", "working", or NULL (no signal
** or no screen — caller falls back). Detected "working" additionally
** requires recent pty output: the covered harnesses animate their
** spinner/status line while working (continuous repaint = fresh .act),
** so a frozen frame with a stale footer must read idle, not working. */
static const char *
detect_state(const char *name)
{
	const char *res = NULL;
	char *fold;
	int novt;

	if (getenv("DCH_NO_DETECT") || make_sock_path(name) < 0)
		return NULL;
	fold = screen_tail(sock_path, &novt);
	if (!fold)
		return NULL;

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
	         det_spinner(fold))
	{
		long ep = activity_epoch(name);
		if (ep && time(NULL) - ep <= DET_WORK_ACT_SECS)
			res = "working";
	}
	free(fold);
	return res;
}

/* Byte length of a leading non-ASCII UTF-8 char at p (a status glyph), 0
** for ASCII or a sequence cut short by the NUL. */
static size_t
det_glyph(const char *p)
{
	unsigned char c = (unsigned char)p[0];
	size_t g = c >= 0xF0 ? 4 : c >= 0xE0 ? 3 : c >= 0xC0 ? 2 : 0, k;

	for (k = 1; k < g; k++)
		if (!p[k])
			return 0;
	return g;
}

/* Live subagents, counted off Claude Code's agent panel: a "<glyph> main"
** row, then one "<glyph> <agent-type> ..." row per running subagent, at
** the bottom of the screen. The panel exists only while subagents do, so
** no panel reads 0 — as does no screen or DCH_NO_DETECT.
** ponytail: screen-scraped, so the count is what the panel shows — a
** prompt covering it reads 0 until answered. Exact counts need
** SubagentStart/SubagentStop hooks reporting into a sidecar. */
static int
detect_agents(const char *name)
{
	char *fold, *line, *next;
	int novt, in = 0, n = 0;

	if (getenv("DCH_NO_DETECT") || make_sock_path(name) < 0)
		return 0;
	fold = screen_tail(sock_path, &novt);
	if (!fold)
		return 0;
	for (line = fold; line; line = next)
	{
		char *p = line, *q;
		size_t g;

		next = strchr(line, '\n');
		if (next)
			*next++ = '\0';
		while (*p == ' ')
			p++;
		g = det_glyph(p);
		if (!g || p[g] != ' ')
		{
			in = 0; /* prose or footer: not a panel row */
			continue;
		}
		p += g + 1;
		for (q = p + strlen(p); q > p && q[-1] == ' '; q--)
			;
		if (q - p == 4 && strncmp(p, "main", 4) == 0)
			in = 1, n = 0; /* the last panel on screen wins */
		else if (in)
			n++;
	}
	free(fold);
	return n;
}

/* Bridge sidecar gate for typing into the Codex TUI at socket `path`:
** 1 = the composer shows its empty placeholder, safe to type; 0 = an
** approval prompt, form or pager owns the keyboard; 2 = anything else (a
** draft in the composer, an unknown screen, a failed read); -1 = no
** terminal mirror or DCH_NO_DETECT, so there is no view at all. */
int
dch_pty_gate(const char *path)
{
	char *fold;
	int novt, res;

	if (getenv("DCH_NO_DETECT"))
		return -1;
	fold = screen_tail(path, &novt);
	if (!fold)
		return novt ? -1 : 2;
	if (det_match(det_suppress,
	              sizeof(det_suppress) / sizeof(det_suppress[0]), fold) ||
	    det_match(det_blocked,
	              sizeof(det_blocked) / sizeof(det_blocked[0]), fold))
		res = 0;
	else
		res = strstr(fold, "ask codex to do anything") ||
		      strstr(fold, "ask a follow-up question") ? 1 : 2;
	free(fold);
	return res;
}

/* Type `text` into the session at socket `path` as one bracketed paste, or
** press Enter when text is NULL. Codex folds a CR that arrives in the same
** burst as pasted text into the paste, so Enter is a separate, later call. */
int
dch_pty_push(const char *path, const char *text)
{
	int s = control_connect_path(path);
	size_t l, off;

	if (s < 0)
		return -1;
	if (!text)
		send_ctl(s, MSG_PUSH, "\r", 1);
	else
	{
		send_ctl(s, MSG_PUSH, "\033[200~", 6);
		for (l = strlen(text), off = 0; off < l; off += PKT_MAX)
			send_ctl(s, MSG_PUSH, text + off,
			         l - off > PKT_MAX ? PKT_MAX : l - off);
		send_ctl(s, MSG_PUSH, "\033[201~", 6);
	}
	close(s);
	return 0;
}

static int
do_read_verb(const char *name, int ansi, int recent_lines, int want_cursor)
{
	unsigned char req[5], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	int s, rc, got_cursor = 0;

	s = control_connect(name, 0);
	if (s < 0)
		return 1;

	req[0] = ansi ? DCH_READ_ANSI : DCH_READ_PLAIN;
	req[1] = recent_lines >= 0 ? DCH_READ_RECENT : DCH_READ_VISIBLE;
	req[2] = recent_lines >= 0 ? (recent_lines & 0xff) : 0;
	req[3] = recent_lines >= 0 ? ((recent_lines >> 8) & 0xff) : 0;
	req[4] = want_cursor ? DCH_READ_F_CURSOR : 0;
	/* Stay on the 4-byte request unless a flag is actually set: a master
	** built before flags existed then sees the exact bytes it always did. */
	send_ctl(s, MSG_READ, req, want_cursor ? sizeof(req) : 4);

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
		else if (type == MSG_READ_CURSOR && len == DCH_READ_CURSOR_LEN)
		{
			/* stderr keeps the stdout screen dump byte-exact; 1-based
			** to match CUP (\e[row;colH) so callers can paste it in. */
			unsigned int row = (unsigned int)payload[0] |
			                   ((unsigned int)payload[1] << 8);
			unsigned int col = (unsigned int)payload[2] |
			                   ((unsigned int)payload[3] << 8);

			fprintf(stderr, "cursor %u %u %u %u\n", row + 1,
			        col + 1, payload[4], payload[5]);
			got_cursor = 1;
		}
		else if (type == MSG_READ_END)
		{
			close(s);
			/* end the screen dump on a newline for shells/agents */
			if (r.got_any)
				fputc('\n', stdout);
			rc = ctl_status_exit(len);
			/* No cursor frame despite asking: either the screen was
			** tail-cut (the row would point at the wrong line, so
			** the master withholds it) or the master predates the
			** flag and ignored it. Never let a caller that asked for
			** the caret mistake either case for "row 1, column 1". */
			if (rc == 0 && want_cursor && !got_cursor)
			{
				fputs(len == DCH_ST_TRUNC
				    ? "dch: screen too big to place the cursor "
				      "in the kept tail; caret not reported\n"
				    : "dch: session master predates --cursor; "
				      "restart the session to enable it\n",
				    stderr);
				return 1;
			}
			return rc;
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

/* Ask the master to re-exec itself onto the current binary, in place. The ACK
** arrives on this very socket from the NEW image (the fd survives execvp), so
** an OK here means the new binary is serving; DCH_ST_ERR means the exec failed
** and the OLD one rolled back and is still serving. Either way the session,
** its child process and its scrollback are untouched. */
static int
do_restart_verb(const char *name, int no_version, int quiet)
{
	unsigned char payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	int s, rc;

	/* Returns 0 restarted, 1 tried and failed, 2 nothing answering (a dead
	** socket), 3 sidecar missing (age unknown, treated as not restartable
	** this run). --all treats 2 and 3 as "not this command's problem" — 3 is
	** the state of every session on the first upgrade to a dch that has
	** live restart, or any session whose sidecar tmp_cleaner has removed. */
	s = control_connect(name, quiet);
	if (s < 0)
		return 2;
	/* No version sidecar is UNKNOWN age, not proven old: macOS's
	** tmp_cleaner deletes idle sidecars from /tmp after 3 days (plain
	** files, unlike the socket node) even off a live, current master,
	** and the master's self-heal tick (default hourly, DCH_HEAL_MS
	** override) re-stamps a missing .ver on its own. Say so instead of
	** waiting out the idle timeout for silence. */
	if (no_version)
	{
		close(s);
		fprintf(stderr, "dch: %s: version sidecar missing — age unknown,"
		        " not\n     proven old (idle sidecars get cleaned from"
		        " /tmp; the master\n     re-stamps its own within a heal"
		        " tick). Retry shortly; if\n     restart keeps failing,"
		        " recreate the session\n", name);
		return 3;
	}
	/* Tell the master which binary to land on: its own dch_exe was
	** resolved when the session started, and on a versioned prefix
	** (Homebrew Cellar, Nix store) that path still points at the old
	** build after an upgrade — re-execing it would report success and
	** change nothing. dch_exe here is this process's own, i.e. the new
	** one. Over PKT_MAX it is dropped; the master falls back to its own. */
	{
		size_t elen = dch_exe ? strlen(dch_exe) : 0;

		if (elen > PKT_MAX)
			elen = 0;
		send_ctl(s, MSG_RESTART, (const unsigned char *)dch_exe,
		         (unsigned int)elen);
	}

	/* CTL_IDLE_MS is generous for this: the master answers after it has
	** re-execed and re-read its whole state, which is still microseconds. */
	rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
	if (rc <= 0)
	{
		close(s);
		return ctl_dead_exit(&r, "restart");
	}
	close(s);
	if (type != MSG_ACK)
		return 1;
	if (len == DCH_ST_OK)
	{
		/* The ACK proves a new image is serving, not that it is THIS
		** version — the master may have fallen back to its own path.
		** The sidecar is written by whoever is serving now, so it is
		** the only honest answer. */
		const char *v = session_version(name);

		if (v && strcmp(v, DCH_VERSION) != 0)
		{
			fprintf(stderr, "dch: %s: restarted, but still on dch "
			        "%s (expected %s)\n", name, v, DCH_VERSION);
			return 1;
		}
		printf("%s: restarted on dch %s\n", name, v ? v : DCH_VERSION);
		return 0;
	}
	if (len == DCH_ST_ERR)
	{
		fprintf(stderr, "dch: %s: re-exec failed; the session is still "
		        "running on its old master\n", name);
		return 1;
	}
	if (len == DCH_ST_BUSY)
	{
		/* The resume blob does not carry an arm or a hold. */
		fprintf(stderr, "dch: %s: a respawn is pending (armed or held);"
		        " retry once it is done, or `dch --respawn %s --cancel`"
		        "\n", name, name);
		return 1;
	}
	return ctl_status_exit(len);
}

/* MSG_RESPAWN to the master at `path`: op 'a' with cmd, 'h' (hold) or 'c'
** (cancel). The MSG_ACK status (an old master answers DCH_ST_UNSUP), -3 when
** the argv does not fit one frame, -4 when nothing listens, else -1/-2 for a
** master that went quiet mid-answer / never answered (ctl_dead_exit's two
** cases). */
static int
respawn_req(const char *path, int op, char **cmd, int ncmd)
{
	unsigned char req[PKT_MAX], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	size_t fill = 1, need = 1;
	int s, rc, k;

	req[0] = (unsigned char)op;
	for (k = 0; k < ncmd; k++)
		need += strlen(cmd[k]) + 1;
	/* One frame carries the whole argv; nothing reassembles more. */
	if (need > sizeof req)
		return -3;
	for (k = 0; k < ncmd; k++)
	{
		size_t l = strlen(cmd[k]) + 1;

		memcpy(req + fill, cmd[k], l);
		fill += l;
	}
	s = control_connect_path(path);
	if (s < 0)
		return -4;
	send_ctl(s, MSG_RESPAWN, req, fill);
	rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
	close(s);
	if (rc <= 0)
		return r.got_any ? -1 : -2;
	return type == MSG_ACK ? (int)len : DCH_ST_ERR;
}

/* Arm a respawn (or, on a held session, perform it now); no command = hold,
** cancel = drop the arm (and end a held session). */
static int
do_respawn_verb(const char *name, char **cmd, int ncmd, int cancel)
{
	struct ctl_reader dead = {0};
	size_t need = 1;
	int k, st;

	if (ncmd && !cmd[0][0])
	{
		fprintf(stderr, "dch: --respawn: empty command\n");
		return 1;
	}
	if (make_sock_path(name) < 0)
		return 1;
	st = respawn_req(sock_path, cancel ? 'c' : ncmd ? 'a' : 'h', cmd, ncmd);
	if (st == -3)
	{
		for (k = 0; k < ncmd; k++)
			need += strlen(cmd[k]) + 1;
		fprintf(stderr, "dch: --respawn: command is %zu bytes, the "
		        "limit is %d\n", need - 1, PKT_MAX - 1);
		return 1;
	}
	if (st == -4)
	{
		fprintf(stderr, "dch: no session: %s\n", name);
		return 1;
	}
	if (st < 0)
	{
		dead.got_any = st == -1;
		return ctl_dead_exit(&dead, "respawn");
	}
	if (st == DCH_ST_ERR)
	{
		fprintf(stderr, "dch: %s: respawn request rejected\n", name);
		return 1;
	}
	return ctl_status_exit((unsigned int)st);
}

/* Print s[0..n) as a JSON string. Invalid UTF-8 becomes U+FFFD so the output
** always parses: the exit tail is raw pty bytes, maybe cut mid-character.
** ponytail: lead/continuation check only; overlongs past C0/C1 and encoded
** surrogates pass through. */
static void
json_bytes(const unsigned char *s, size_t n)
{
	size_t i = 0, k, l;

	putchar('"');
	while (i < n)
	{
		unsigned char c = s[i];

		if (c == '"' || c == '\\')
		{
			printf("\\%c", c);
			i++;
			continue;
		}
		if (c < 0x20 || c == 0x7f)
		{
			printf("\\u%04x", c);
			i++;
			continue;
		}
		l = c < 0x80 ? 1 : (c >= 0xc2 && c <= 0xdf) ? 2
		  : (c & 0xf0) == 0xe0 ? 3 : (c >= 0xf0 && c <= 0xf4) ? 4 : 0;
		for (k = 1; l && k < l; k++)
			if (i + k >= n || (s[i + k] & 0xc0) != 0x80)
				l = 0;
		if (!l)
		{
			fputs("\\ufffd", stdout);
			i++;
			continue;
		}
		fwrite(s + i, 1, l, stdout);
		i += l;
	}
	putchar('"');
}

/* Next NUL-terminated field of a MSG_CMD reply, or NULL when it runs out. */
static const char *
cmd_field(const unsigned char **p, const unsigned char *end)
{
	const unsigned char *f = *p, *z = memchr(f, '\0', (size_t)(end - f));

	if (!z)
		return NULL;
	*p = z + 1;
	return (const char *)f;
}

/* A parsed MSG_CMD reply. buf owns the strings; argv is malloc'd. */
struct cmd_info
{
	unsigned char *buf;
	long pid;			/* 0 while held */
	const char *state;		/* running | armed | held */
	int code, sig;			/* -1 / 0 until a child is reaped under an arm */
	int argc;
	char **argv;			/* argc entries + NULL */
	const unsigned char *tail;	/* raw output since the arm */
	size_t tail_len;
};

static void
cmd_info_free(struct cmd_info *ci)
{
	free(ci->buf);
	free(ci->argv);
	memset(ci, 0, sizeof *ci);
}

/* MSG_CMD to the master at `path`. 0 = *ci filled; a positive master status
** (DCH_ST_UNSUP from an old one); -4 nothing listens; -1/-2 the master went
** quiet mid-answer / never answered; -3 a malformed reply. */
static int
cmd_query(const char *path, struct cmd_info *ci)
{
	unsigned char payload[PKT_MAX], type, *buf = NULL;
	const unsigned char *q, *end;
	const char *pid, *code, *sig, *ac;
	struct ctl_reader r = {0};
	unsigned int len;
	size_t fill = 0;
	int s, rc, k;

	memset(ci, 0, sizeof *ci);
	s = control_connect_path(path);
	if (s < 0)
		return -4;
	send_ctl(s, MSG_CMD, NULL, 0);
	for (;;)
	{
		rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
		if (rc <= 0)
		{
			close(s);
			free(buf);
			return r.got_any ? -1 : -2;
		}
		if (type != MSG_READ_DATA)
			break;
		{
			unsigned char *nb = realloc(buf, fill + len);

			if (!nb)
			{
				close(s);
				free(buf);
				return -3;
			}
			buf = nb;
			memcpy(buf + fill, payload, len);
			fill += len;
		}
	}
	close(s);
	if (type != MSG_READ_END || len != DCH_ST_OK)
	{
		free(buf);
		return type == MSG_READ_END || type == MSG_ACK ? (int)len
		                                               : DCH_ST_ERR;
	}

	q = buf;
	end = buf + fill;
	pid = buf ? cmd_field(&q, end) : NULL;
	ci->state = pid ? cmd_field(&q, end) : NULL;
	code = ci->state ? cmd_field(&q, end) : NULL;
	sig = code ? cmd_field(&q, end) : NULL;
	ac = sig ? cmd_field(&q, end) : NULL;
	ci->buf = buf;
	if (!ac || atoi(ac) < 0 ||
	    !(ci->argv = calloc((size_t)atoi(ac) + 1, sizeof(char *))))
	{
		cmd_info_free(ci);
		return -3;
	}
	ci->pid = atol(pid);
	ci->code = atoi(code);
	ci->sig = atoi(sig);
	for (k = 0; k < atoi(ac); k++)
		if (!(ci->argv[k] = (char *)cmd_field(&q, end)))
		{
			cmd_info_free(ci);
			return -3;
		}
	ci->argc = k;
	ci->tail = q;
	ci->tail_len = (size_t)(end - q);
	return 0;
}

/* `dch --cmd`: what the session is running right now, as one JSON object:
** {"pid","state","argv","exit_code","exit_signal","tail"}. pid is null while
** held; exit_* are null until a child is reaped under an arm. */
static int
do_cmd_verb(const char *name)
{
	struct cmd_info ci;
	struct ctl_reader dead = {0};
	int k, st;

	if (make_sock_path(name) < 0)
		return 1;
	st = cmd_query(sock_path, &ci);
	if (st == -4)
	{
		fprintf(stderr, "dch: no session: %s\n", name);
		return 1;
	}
	if (st == -1 || st == -2)
	{
		dead.got_any = st == -1;
		return ctl_dead_exit(&dead, "cmd");
	}
	if (st == -3)
	{
		fprintf(stderr, "dch: malformed cmd response\n");
		return 1;
	}
	if (st > 0)
		return ctl_status_exit((unsigned int)st);
	if (ci.pid > 0)
		printf("{\"pid\":%ld,\"state\":", ci.pid);
	else
		fputs("{\"pid\":null,\"state\":", stdout);
	json_bytes((const unsigned char *)ci.state, strlen(ci.state));
	fputs(",\"argv\":[", stdout);
	for (k = 0; k < ci.argc; k++)
	{
		if (k)
			putchar(',');
		json_bytes((const unsigned char *)ci.argv[k],
		           strlen(ci.argv[k]));
	}
	fputs("],\"exit_code\":", stdout);
	if (ci.code >= 0)
		printf("%d", ci.code);
	else
		fputs("null", stdout);
	fputs(",\"exit_signal\":", stdout);
	if (ci.sig > 0)
		printf("%d", ci.sig);
	else
		fputs("null", stdout);
	fputs(",\"tail\":", stdout);
	json_bytes(ci.tail, ci.tail_len);
	puts("}");
	cmd_info_free(&ci);
	return 0;
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
	/* Caller env first; DCH_SESSION is force-set after, so --env cannot
	** spoof session identity. (TERM below is only defaulted when absent
	** or dumb — a caller-supplied TERM legitimately wins.) */
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

/* ---- agent-aware --restart --all (Claude Code) ----------------------
**
** After the master pass, `--restart --all` restarts the Claude Code agents
** that run AS a session's command once the installed CLI is newer than the
** running one (-f: all of them). Per agent: type a pause message, wait until
** it is provably idle, arm a #021 hold and type /exit, read the session id
** from the `claude --resume <id>` line in the exit tail, respawn the same
** argv with --resume <id>, wait for the new composer, type a resume message.
** Every keystroke goes through a composer gate that wants the empty composer
** on screen — Enter on a permission prompt would approve a tool call — so
** the whole step needs the terminal mirror and dch-lite skips it. */

/* Index of the argv entry whose basename is `claude`, argc when none. */
static int
claude_tok(char **v, int argc)
{
	int k;

	for (k = 0; k < argc; k++)
	{
		const char *b = strrchr(v[k], '/');

		if (strcmp(b ? b + 1 : v[k], "claude") == 0)
			break;
	}
	return k;
}

#ifdef HAVE_LIBGHOSTTY_VT

#define AG_POLL_MS  250
/* Paste, then Enter: Enter in the same burst can fold into the paste. */
#define AG_ENTER_MS 300
/* How long typed text may take to show in the composer: a Claude that has
** just resumed a long conversation took over a second to paint a paste. */
#define AG_LAND_MS  3000

static const char ag_pause_msg[] =
    "pause all work, i need to restart you. Paused means: stop every "
    "background shell and monitor (TaskStop), stop or finish your "
    "subagents, cancel any /loop, ScheduleWakeup and session crons, and "
    "note what you stopped so it can be re-armed after the restart. Then "
    "say that all work is paused and you are ready to be restarted.";
static const char ag_resume_msg[] =
    "resume all work. You were restarted: re-arm the shells, monitors, "
    "subagents, loops, wakeups and crons you stopped before the restart, "
    "then carry on where you left off.";

/* What the screen lets us type into (claude_gate). */
enum { CG_NOVT = -1, CG_EMPTY, CG_DIALOG, CG_DRAFT, CG_QUEUED, CG_UNKNOWN };
static const char *const cg_why[] = {
	"the composer is empty", "a prompt or dialog is waiting for you",
	"the composer holds an unsent draft", "a queued message is pending",
	"the composer is not on screen",
};

/* One Claude session record (~/.claude/sessions/<pid>.json). */
struct crec
{
	long pid, upd;		/* upd = statusUpdatedAt, epoch ms */
	int depth;		/* generations below the scan root */
	char sid[64], ver[64], status[32], cwd[1024];
};

struct crec_scan
{
	long root;
	int verify, n;		/* n = live records under root */
	struct crec best;	/* the shallowest of them */
};

struct agent
{
	char name[600], path[1100];
	char **argv;		/* the session's argv (owned by ci) */
	struct cmd_info ci;
	int tok, argc, saw_busy;
	long cpid;
	char ver[64], want[64], sid[64], id[64], note[200], why[120];
	enum { AG_WAIT, AG_EXIT, AG_START, AG_DONE } step;
	long long t_sent, ok_since, deadline;
};

static long long
wall_ms(void)
{
	struct timeval tv;

	gettimeofday(&tv, NULL);
	return (long long)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

static long
env_ms(const char *key, long dflt)
{
	const char *e = getenv(key);
	long v = e ? atol(e) : 0;

	return v > 0 ? v : dflt;
}

/* Parent pid from the kernel's process table — never the env, which macOS
** redacts for platform binaries. -1 when the process is gone. */
static long
proc_ppid(long pid)
{
#ifdef __APPLE__
	struct kinfo_proc kp;
	size_t len = sizeof kp;
	int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PID, (int)pid };

	if (sysctl(mib, 4, &kp, &len, NULL, 0) != 0 || len == 0)
		return -1;
	return kp.kp_eproc.e_ppid;
#else
	char p[64], buf[512], *rp;
	FILE *f;
	long pp = -1;

	snprintf(p, sizeof p, "/proc/%ld/stat", pid);
	if (!(f = fopen(p, "r")))
		return -1;
	/* the comm field may hold spaces and parens: parse after the last ')' */
	if (fgets(buf, sizeof buf, f) && (rp = strrchr(buf, ')')))
		sscanf(rp + 1, " %*c %ld", &pp);
	fclose(f);
	return pp;
#endif
}

/* Generations from `anc` down to `pid` (0 = the same process), -1 when pid
** does not descend from it. */
static int
proc_depth(long pid, long anc)
{
	int d;

	for (d = 0; d < 64 && pid > 1; d++)
	{
		if (pid == anc)
			return d;
		pid = proc_ppid(pid);
	}
	return -1;
}

static int
proc_cwd(long pid, char *out, size_t outsz)
{
#ifdef __APPLE__
	struct proc_vnodepathinfo vi;

	if (proc_pidinfo((int)pid, PROC_PIDVNODEPATHINFO, 0, &vi, sizeof vi)
	    != (int)sizeof vi)
		return -1;
	snprintf(out, outsz, "%s", vi.pvi_cdir.vip_path);
#else
	char p[64];
	ssize_t n;

	snprintf(p, sizeof p, "/proc/%ld/cwd", pid);
	n = readlink(p, out, outsz - 1);
	if (n <= 0)
		return -1;
	out[n] = '\0';
#endif
	return 0;
}

/* dch_visit_claude_records callback: count the live interactive records
** whose pid descends from cs->root, keep the shallowest. With verify, the
** record's procStart must match the process's start time, so a record left
** by a dead Claude whose pid was recycled never joins. */
static int
crec_visit(const char *path, const char *body, size_t len, void *arg)
{
	struct crec_scan *cs = arg;
	struct crec c;
	const char *base = strrchr(path, '/');
	char kind[32], start[128], actual[128], *endp;
	long jpid;

	(void)len;
	memset(&c, 0, sizeof c);
	base = base ? base + 1 : path;
	c.pid = strtol(base, &endp, 10);
	if (c.pid <= 0 || strcmp(endp, ".json") ||
	    dch_json_long(body, "pid", &jpid) < 0 || jpid != c.pid ||
	    dch_json_string(body, "kind", kind, sizeof kind) < 0 ||
	    strcmp(kind, "interactive") || kill((pid_t)c.pid, 0) < 0 ||
	    (c.depth = proc_depth(c.pid, cs->root)) < 0 ||
	    dch_json_string(body, "sessionId", c.sid, sizeof c.sid) < 0 ||
	    dch_json_string(body, "version", c.ver, sizeof c.ver) < 0 ||
	    dch_json_string(body, "status", c.status, sizeof c.status) < 0)
		return 0;
	if (dch_json_string(body, "cwd", c.cwd, sizeof c.cwd) < 0)
		c.cwd[0] = '\0';
	if (dch_json_long(body, "statusUpdatedAt", &c.upd) < 0)
		c.upd = 0;
	if (cs->verify &&
	    (dch_json_string(body, "procStart", start, sizeof start) < 0 ||
	     dch_process_start(c.pid, actual, sizeof actual) < 0 ||
	     strcmp(start, actual)))
		return 0;
	if (cs->n++ == 0 || c.depth < cs->best.depth)
		cs->best = c;
	return 0;
}

/* Live Claude records at or below `root`; *best = the shallowest. */
static int
claude_find(long root, int verify, struct crec *best)
{
	struct crec_scan cs;

	memset(&cs, 0, sizeof cs);
	cs.root = root;
	cs.verify = verify;
	(void)dch_visit_claude_records(crec_visit, &cs);
	*best = cs.best;
	return cs.n;
}

/* Length of the escape sequence at p (p[0] == ESC): CSI up to its final
** byte, OSC up to BEL or ST, else ESC + intermediates + one final byte. */
static size_t
esc_len(const unsigned char *p, size_t n)
{
	size_t i = 2;

	if (n < 2)
		return n;
	if (p[1] == '[')
	{
		while (i < n && (p[i] < 0x40 || p[i] > 0x7e))
			i++;
		return i < n ? i + 1 : n;
	}
	if (p[1] == ']')
	{
		for (; i < n; i++)
			if (p[i] == '\a')
				return i + 1;
			else if (p[i] == 0x1b && i + 1 < n && p[i + 1] == '\\')
				return i + 2;
		return n;
	}
	for (i = 1; i < n && p[i] >= 0x20 && p[i] <= 0x2f; i++)
		;
	return i < n ? i + 1 : n;
}

/* Faint (SGR 2) after the SGR parameters p[0..n), starting from `dim`. The
** extended colour forms are skipped whole: the 2 in 38;2;r;g;b is not dim. */
static int
sgr_dim(const unsigned char *p, size_t n, int dim)
{
	size_t i = 0;
	int skip = 0;

	if (n == 0)
		return 0;
	while (i <= n)
	{
		long v = 0;
		size_t j = i;

		while (j < n && p[j] != ';')
		{
			if (p[j] == ':')	/* 38:2:r:g:b is one parameter */
				v = -1;
			else if (v >= 0 && p[j] >= '0' && p[j] <= '9')
				v = v * 10 + (p[j] - '0');
			j++;
		}
		if (skip > 0)
			skip--;
		else if (v == 38 || v == 48 || v == 58)
			skip = j + 1 < n && p[j + 1] == '5' ? 2 : 4;
		else if (v == 0 || v == 22)
			dim = 0;
		else if (v == 2)
			dim = 1;
		i = j + 1;
	}
	return dim;
}

/* The screen at `path` as plain text plus a per-byte faint flag in *dimp
** (both malloc'd), CRs dropped. NULL on failure, *err = CG_NOVT when the
** session has no mirror, else CG_UNKNOWN. */
static char *
screen_faint(const char *path, char **dimp, int *err)
{
	char *raw, *txt, *dim;
	size_t i, k = 0;
	int n, d = 0;

	*err = CG_UNKNOWN;
	if (getenv("DCH_NO_DETECT"))
	{
		*err = CG_NOVT;
		return NULL;
	}
	raw = malloc(DET_CAP + 1);
	if (!raw)
		return NULL;
	n = fetch_screen(path, raw, DET_CAP, DET_DEADLINE_MS, 1);
	if (n <= 0)
	{
		free(raw);
		if (n == -2)
			*err = CG_NOVT;
		return NULL;
	}
	txt = malloc((size_t)n + 1);
	dim = malloc((size_t)n + 1);
	if (!txt || !dim)
	{
		free(raw);
		free(txt);
		free(dim);
		return NULL;
	}
	for (i = 0; i < (size_t)n; )
	{
		const unsigned char *u = (const unsigned char *)raw + i;

		if (*u == 0x1b)
		{
			size_t l = esc_len(u, (size_t)n - i);

			if (l > 2 && u[1] == '[' && u[l - 1] == 'm')
				d = sgr_dim(u + 2, l - 3, d);
			i += l;
			continue;
		}
		if (*u != '\r')
		{
			txt[k] = (char)*u;
			dim[k++] = (char)d;
		}
		i++;
	}
	txt[k] = '\0';
	free(raw);
	*dimp = dim;
	return txt;
}

/* A prompt or form (CG_DIALOG) or a queued message (CG_QUEUED) on the
** bottom lines of txt, else CG_UNKNOWN. */
static int
screen_modal(const char *txt)
{
	char *fold = tail_fold(txt, strlen(txt));
	int res = CG_UNKNOWN;

	if (fold && (det_match(det_suppress, sizeof(det_suppress) /
	                       sizeof(det_suppress[0]), fold) ||
	             det_match(det_blocked, sizeof(det_blocked) /
	                       sizeof(det_blocked[0]), fold)))
		res = CG_DIALOG;
	else if (fold && (strstr(fold, "press up to edit queued messages") ||
	                  strstr(fold, "ctrl+enter to send now")))
		res = CG_QUEUED;
	free(fold);
	return res;
}

static int
is_rule(const char *l)
{
	while (*l == ' ')
		l++;
	return strncmp(l, "\xe2\x94\x80", 3) == 0;
}

/* The composer: the bottom-most row that starts with "❯" (then a space, an
** NBSP or the end of the row) right under a "─" rule and running down to
** the next rule, its wrapped rows indented. Returns that row, *end = the
** closing rule; NULL when there is none. A permission prompt's "❯ 1. Yes"
** is indented and has no closing rule, so it never reads as one. */
static char *
composer_find(char *txt, char **end)
{
	char *ls, *nx, *comp = NULL, *prev = NULL;

	for (ls = txt; ls; prev = ls, ls = nx)
	{
		char *l;

		nx = strchr(ls, '\n');
		nx = nx ? nx + 1 : NULL;
		if (!prev || !is_rule(prev) || strncmp(ls, "\xe2\x9d\xaf", 3) ||
		    !(ls[3] == ' ' || ls[3] == '\n' || ls[3] == '\0' ||
		      strncmp(ls + 3, "\xc2\xa0", 2) == 0))
			continue;
		for (l = nx; l && !is_rule(l) && (*l == ' ' || *l == '\n'); )
		{
			l = strchr(l, '\n');
			l = l ? l + 1 : NULL;
		}
		if (l && is_rule(l))
		{
			comp = ls;
			*end = l;
		}
	}
	return comp;
}

/* Is the Claude Code screen at `path` safe to type into? Positive evidence
** only: the composer must be on screen and empty (nothing after the glyph,
** or only Claude's dim placeholder: a typed draft is not dim), with no
** prompt or form up and no queued message. Anything else is reported, never
** typed over: a trust dialog or release notes before the composer read
** UNKNOWN. */
static int
claude_gate(const char *path)
{
	char *txt, *dim, *comp, *end, *c;
	int res;

	if (!(txt = screen_faint(path, &dim, &res)))
		return res;
	if ((res = screen_modal(txt)) == CG_UNKNOWN &&
	    (comp = composer_find(txt, &end)))
	{
		for (c = comp + 3; ; )
			if (*c == ' ')
				c++;
			else if (strncmp(c, "\xc2\xa0", 2) == 0)
				c += 2;
			else
				break;
		res = (*c == '\n' || *c == '\0' || dim[c - txt]) &&
		      end == strchr(comp, '\n') + 1 ? CG_EMPTY : CG_DRAFT;
	}
	free(txt);
	free(dim);
	return res;
}

/* s without spaces, NBSPs and line breaks (wrapping moves them), at most
** cap bytes. */
static char *
squeeze(const char *s, size_t n, size_t cap)
{
	char *o = malloc(n + 1);
	size_t i, k = 0;

	if (!o)
		return NULL;
	for (i = 0; i < n && k < cap; i++)
		if (strncmp(s + i, "\xc2\xa0", 2) == 0)
			i++;
		else if (s[i] != ' ' && s[i] != '\n' && s[i] != '\t')
			o[k++] = s[i];
	o[k] = '\0';
	return o;
}

/* Did `text` land in the composer? Evidence counts only inside the composer
** rows — the transcript above routinely shows "[Pasted text #1 +3 lines]"
** and earlier messages: the start of our text, or the placeholder Claude
** Code collapses a long paste into. */
static int
composer_holds(const char *path, const char *text)
{
	char *txt, *dim, *comp, *end, *have = NULL, *want = NULL;
	int err, ok = 0;

	if (!(txt = screen_faint(path, &dim, &err)))
		return 0;
	if (screen_modal(txt) != CG_DIALOG && (comp = composer_find(txt, &end)) &&
	    (have = squeeze(comp + 3, (size_t)(end - comp - 3), (size_t)-1)) &&
	    (want = squeeze(text, strlen(text), 32)))
		ok = *want && (strstr(have, want) ||
		               strstr(have, "[Pastedtext#"));
	free(have);
	free(want);
	free(txt);
	free(dim);
	return ok;
}

/* Type text (paste = bracketed paste for messages; raw keystrokes for a
** slash command, which Claude's menu must see typed), then press Enter —
** only once the composer shows it. A prompt raised in between would take
** the Enter as an answer. Returns 0, -1 when nothing could be typed, -2
** when the text did not land (no Enter pressed). */
static int
ag_type(const char *path, const char *text, int paste)
{
	int s, t;

	if (paste)
	{
		if (dch_pty_push(path, text) < 0)
			return -1;
	}
	else
	{
		if ((s = control_connect_path(path)) < 0)
			return -1;
		send_ctl(s, MSG_PUSH, text, strlen(text));
		close(s);
	}
	for (t = AG_ENTER_MS; ; t += AG_POLL_MS / 2)
	{
		poll(NULL, 0, t == AG_ENTER_MS ? AG_ENTER_MS : AG_POLL_MS / 2);
		if (composer_holds(path, text))
			return dch_pty_push(path, NULL);
		if (t >= AG_LAND_MS)
			return -2;
	}
}

static int
is_uuid(const char *u)
{
	int i;

	for (i = 0; i < 36; i++)
		if (i == 8 || i == 13 || i == 18 || i == 23 ? u[i] != '-'
		                                            : !isxdigit((unsigned char)u[i]))
			return 0;
	return 1;
}

/* The id from the LAST `--resume <uuid>` in the exit tail. The tail is raw
** pty bytes: Ink dims the line and may style the id apart from the command,
** and a narrow pane hard-wraps it, so escapes and control bytes (CR/LF) go
** first and a split or wrapped id reads whole. */
static int
resume_id(const unsigned char *t, size_t n, char *out, size_t outsz)
{
	char *p = malloc(n + 1), *q, *hit = NULL;
	size_t i = 0, k = 0;

	if (!p)
		return -1;
	while (i < n)
		if (t[i] == 0x1b)
			i += esc_len(t + i, n - i);
		else if (t[i] < 0x20 || t[i] == 0x7f)
			i++;
		else
			p[k++] = (char)t[i++];
	p[k] = '\0';
	for (q = p; (q = strstr(q, "--resume")); q += 8)
	{
		char *u = q + 8;

		while (*u == ' ')
			u++;
		if (is_uuid(u))
			hit = u;
	}
	if (hit)
		snprintf(out, outsz, "%.36s", hit);
	free(p);
	return hit ? 0 : -1;
}

/* argv for the relaunch: up to the claude token, then --resume <id>, then
** the rest minus any earlier session choice (--resume/-r [id], --resume=,
** --session-id, --continue/-c, --fork-session). Inserted after the token,
** not appended: a trailing `-- <prompt>` would swallow it. --resume/-r take
** an OPTIONAL value, so only a non-flag next token is theirs. */
static char **
resume_argv(char **v, int argc, int tok, char *id, int *outc)
{
	char **o = calloc((size_t)argc + 3, sizeof(char *));
	int k, n = 0, opts = 1;

	if (!o)
		return NULL;
	for (k = 0; k <= tok; k++)
		o[n++] = v[k];
	o[n++] = "--resume";
	o[n++] = id;
	for (k = tok + 1; k < argc; k++)
	{
		const char *a = v[k];

		if (opts && strcmp(a, "--") == 0)
			opts = 0;
		else if (opts && (strcmp(a, "--resume") == 0 ||
		                  strcmp(a, "-r") == 0))
		{
			if (k + 1 < argc && v[k + 1][0] != '-')
				k++;
			continue;
		}
		else if (opts && strcmp(a, "--session-id") == 0)
		{
			if (k + 1 < argc)
				k++;
			continue;
		}
		else if (opts && (strncmp(a, "--resume=", 9) == 0 ||
		                  strncmp(a, "--session-id=", 13) == 0 ||
		                  strcmp(a, "--continue") == 0 ||
		                  strcmp(a, "-c") == 0 ||
		                  strcmp(a, "--fork-session") == 0))
			continue;
		o[n++] = v[k];
	}
	*outc = n;
	return o;
}

/* `claude --version` of the binary the relaunch will run: the token itself
** when it has a '/' (relative to the session's cwd), else the first match on
** the MASTER's PATH, which is the env the respawned argv gets. */
static int
claude_installed(const char *tok, const char *mpath, const char *mcwd,
                 char *out, size_t outsz)
{
	char exe[2048], buf[256], *sp;
	const char *dir = mpath, *e;
	struct stat st;
	struct pollfd pfd;
	long long end;
	size_t got = 0;
	int fd[2], status;
	pid_t pid;

	exe[0] = '\0';
	if (strchr(tok, '/'))
		snprintf(exe, sizeof exe, "%s%s%s", tok[0] == '/' ? "" : mcwd,
		         tok[0] == '/' ? "" : "/", tok);
	else
		for (; dir; dir = e ? e + 1 : NULL)
		{
			int l;

			e = strchr(dir, ':');
			l = e ? (int)(e - dir) : (int)strlen(dir);
			snprintf(exe, sizeof exe, "%.*s/%s", l ? l : (int)strlen(mcwd),
			         l ? dir : mcwd, tok);
			if (stat(exe, &st) == 0 && S_ISREG(st.st_mode) &&
			    access(exe, X_OK) == 0)
				break;
			exe[0] = '\0';
		}
	if (!exe[0] || pipe(fd) < 0)
		return -1;
	pid = fork();
	if (pid < 0)
	{
		close(fd[0]);
		close(fd[1]);
		return -1;
	}
	if (pid == 0)
	{
		char *av[] = { exe, "--version", NULL };
		int nul = open("/dev/null", O_RDWR);

		dup2(fd[1], 1);
		if (nul >= 0)
		{
			dup2(nul, 0);
			dup2(nul, 2);
		}
		close(fd[0]);
		close(fd[1]);
		setenv("PATH", mpath, 1);
		if (chdir(mcwd) < 0)
			_exit(127);
		execv(exe, av);
		_exit(127);
	}
	close(fd[1]);
	/* ponytail: a 10 s cap; a --version that hangs is not one to trust */
	end = wall_ms() + 10000;
	pfd.fd = fd[0];
	pfd.events = POLLIN;
	while (got < sizeof buf - 1 && wall_ms() < end &&
	       poll(&pfd, 1, 200) >= 0)
	{
		ssize_t r;

		if (!(pfd.revents & (POLLIN | POLLHUP)))
			continue;
		r = read(fd[0], buf + got, sizeof buf - 1 - got);
		if (r <= 0)
			break;
		got += (size_t)r;
	}
	close(fd[0]);
	while (waitpid(pid, &status, WNOHANG) == 0)
	{
		if (wall_ms() >= end)
		{
			kill(pid, SIGKILL);
			waitpid(pid, &status, 0);
			return -1;
		}
		poll(NULL, 0, 20);
	}
	buf[got] = '\0';
	/* "2.1.299 (Claude Code)" */
	sp = buf + strspn(buf, " \t\r\n");
	sp[strcspn(sp, " \t\r\n")] = '\0';
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0 ||
	    !isdigit((unsigned char)sp[0]))
		return -1;
	snprintf(out, outsz, "%s", sp);
	return 0;
}

/* One report line per agent; done = it reached a terminal state. */
static void
ag_say(struct agent *a, int failed, const char *fmt, ...)
{
	va_list ap;

	printf("%s: %s", a->name, failed ? "NOT restarted — " : "");
	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	putchar('\n');
	fflush(stdout);
	a->step = AG_DONE;
}

/* Type the resume message into an agent we give up on, if the composer
** lets us. Returns how it went, for the report line. */
static const char *
ag_resume(struct agent *a, const char *resume)
{
	if (claude_gate(a->path) != CG_EMPTY)
		return "NOT typed";
	switch (ag_type(a->path, resume, 1))
	{
	case 0:
		return "typed";
	case -2:
		return "typed but NOT sent (it did not land in the composer)";
	default:
		return "NOT typed";
	}
}

/* The last `want` non-blank lines of raw pty output, escapes dropped,
** joined with " / " into out: what a crashed claude said last. */
static void
tail_lines(const unsigned char *t, size_t n, int want, char *out,
           size_t outsz)
{
	char *p = malloc(n + 1), *l, *keep[8];
	size_t i = 0, k = 0;
	int c = 0, j;

	out[0] = '\0';
	if (!p || want > 8)
	{
		free(p);
		return;
	}
	while (i < n)
		if (t[i] == 0x1b)
		{
			size_t l = esc_len(t + i, n - i);

			/* cursor moves and clears end a line; SGR does not */
			if (t[i + l - 1] != 'm')
				p[k++] = '\n';
			i += l;
		}
		else if (t[i] == '\r' || t[i] == '\n')
			p[k++] = '\n', i++;
		else if (t[i] < 0x20 || t[i] == 0x7f)
			i++;
		else
			p[k++] = (char)t[i++];
	p[k] = '\0';
	for (l = strtok(p, "\n"); l; l = strtok(NULL, "\n"))
	{
		l += strspn(l, " ");
		if (*l)
			keep[c++ % want] = l;
	}
	for (j = c > want ? c - want : 0; j < c; j++)
		snprintf(out + strlen(out), outsz - strlen(out), "%s%s",
		         out[0] ? " / " : "", keep[j % want]);
	free(p);
}

/* The hold is armed and /exit typed: wait for the exit, then relaunch. */
static int
ag_exited(struct agent *a, const char *step_s)
{
	struct cmd_info ci;
	char tid[64], **nv;
	int nc, st, fail = 0;

	st = cmd_query(a->path, &ci);
	if (st != 0)
	{
		ag_say(a, 1, "the session went away during /exit");
		return 1;
	}
	if (strcmp(ci.state, "held") != 0)
	{
		if (strcmp(ci.state, "running") == 0)
			ag_say(a, 1, "the hold was dropped (expired or cancelled) "
			       "before claude exited");
		else if (wall_ms() >= a->deadline)
		{
			respawn_req(a->path, 'c', NULL, 0);
			ag_say(a, 1, "/exit did not end claude within %s; hold "
			       "cancelled, claude left running", step_s);
		}
		cmd_info_free(&ci);
		return a->step == AG_DONE;
	}
	/* Crashed instead of a clean /exit: nothing to resume into. The
	** cancel ends the session, as the exit would have unarmed. */
	if (ci.sig > 0 || ci.code != 0)
	{
		char last[300];

		/* The session, and its screen, go with the cancel: keep what
		** claude said last in the report. */
		tail_lines(ci.tail, ci.tail_len, 3, last, sizeof last);
		respawn_req(a->path, 'c', NULL, 0);
		if (ci.sig > 0)
			ag_say(a, 1, "claude died on signal %d instead of exiting;"
			       " not relaunched, session ended%s%s%s", ci.sig,
			       last[0] ? " (last output: " : "", last,
			       last[0] ? ")" : "");
		else
			ag_say(a, 1, "claude exited with status %d instead of 0;"
			       " not relaunched, session ended%s%s%s", ci.code,
			       last[0] ? " (last output: " : "", last,
			       last[0] ? ")" : "");
		cmd_info_free(&ci);
		return 1;
	}
	/* The exit line is primary; the record's id (read right before /exit,
	** as /clear rotates it) is the fallback and the cross-check. */
	if (resume_id(ci.tail, ci.tail_len, tid, sizeof tid) < 0)
	{
		snprintf(a->id, sizeof a->id, "%s", a->sid);
		snprintf(a->note, sizeof a->note, " (no resume line in the "
		         "exit output; used the session record's id)");
	}
	else
	{
		snprintf(a->id, sizeof a->id, "%s", tid);
		if (strcmp(tid, a->sid) != 0)
			snprintf(a->note, sizeof a->note, " (exit line said %s, "
			         "session record said %s; used the exit line)",
			         tid, a->sid);
	}
	cmd_info_free(&ci);
	nv = resume_argv(a->argv, a->argc, a->tok, a->id, &nc);
	st = nv ? respawn_req(a->path, 'a', nv, nc) : -3;
	free(nv);
	if (st != DCH_ST_OK)
	{
		respawn_req(a->path, 'c', NULL, 0);
		ag_say(a, 1, "relaunch with --resume %s was refused (%d); "
		       "session ended", a->id, st);
		fail = 1;
	}
	else
	{
		a->step = AG_START;
		a->deadline = wall_ms() + env_ms("DCH_AGENT_STEP_MS", 60000);
	}
	return fail;
}

/* One poll of one agent. Returns 1 when it ended in failure. */
static int
ag_tick(struct agent *a, long tmo, long settle, const char *resume)
{
	long step = env_ms("DCH_AGENT_STEP_MS", 60000);
	long long now = wall_ms();
	char step_s[32];
	struct crec r;
	int g, ready;

	snprintf(step_s, sizeof step_s, "%lds", step / 1000);
	if (a->step == AG_EXIT)
		return ag_exited(a, step_s);
	if (a->step == AG_START)
	{
		struct cmd_info ci;

		if (cmd_query(a->path, &ci) != 0)
		{
			ag_say(a, 1, "relaunched with --resume %s, but it exited "
			       "and the session ended", a->id);
			return 1;
		}
		g = CG_UNKNOWN;
		if (ci.pid > 0 && claude_find(ci.pid, 1, &r) > 0 &&
		    strcmp(r.status, "idle") == 0 &&
		    (g = claude_gate(a->path)) == CG_EMPTY)
		{
			int old = strcmp(r.ver, a->want) != 0;

			cmd_info_free(&ci);
			if (ag_type(a->path, resume, 1) != 0)
			{
				ag_say(a, 1, "relaunched with --resume %s on claude %s, "
				       "but the resume message did not land in the "
				       "composer; Enter not pressed", a->id, r.ver);
				return 1;
			}
			if (strcmp(r.sid, a->id) != 0)
				snprintf(a->note + strlen(a->note),
				         sizeof a->note - strlen(a->note),
				         " (the new record says session %s)", r.sid);
			if (old)
				ag_say(a, 1, "relaunched with --resume %s, but still "
				       "on claude %s (expected %s)%s", a->id, r.ver,
				       a->want, a->note);
			else
				ag_say(a, 0, "claude %s -> %s, resumed %s%s", a->ver,
				       r.ver, a->id, a->note);
			return old;
		}
		cmd_info_free(&ci);
		if (now < a->deadline)
			return 0;
		ag_say(a, 1, "relaunched with --resume %s, but no empty composer "
		       "within %s (%s); resume message not typed", a->id, step_s,
		       g == CG_NOVT ? "no mirror" : cg_why[g]);
		return 1;
	}

	/* AG_WAIT: is the pause consumed and the agent idle, for a while? */
	if (claude_find(a->cpid, 0, &r) == 0 || r.pid != a->cpid)
	{
		ag_say(a, 1, "claude exited while pausing");
		return 1;
	}
	if (strcmp(r.status, "busy") == 0 && r.upd >= a->t_sent)
		a->saw_busy = 1;
	g = claude_gate(a->path);
	/* Busy after the push proves the message was taken; an idle stamp
	** newer than the push does too, unless the message sits queued —
	** a turn that ends with one queued is idle for a moment first. */
	ready = (a->saw_busy || (r.upd > a->t_sent && g != CG_QUEUED)) &&
	        strcmp(r.status, "idle") == 0 && g == CG_EMPTY;
	if (!ready && g == CG_EMPTY)
		snprintf(a->why, sizeof a->why, "status %s", r.status);
	else if (!ready)
		snprintf(a->why, sizeof a->why, "status %s, %s", r.status,
		         g == CG_NOVT ? "no mirror" : cg_why[g]);
	else if ((g = detect_agents(a->name)) > 0)
	{
		ready = 0;
		snprintf(a->why, sizeof a->why, "%d subagent%s running", g,
		         g == 1 ? "" : "s");
	}
	else
		snprintf(a->why, sizeof a->why, "idle, settling");
	if (!ready)
		a->ok_since = 0;
	else if (!a->ok_since)
		a->ok_since = now;
	if (ready && now - a->ok_since >= settle)
	{
		int st;

		/* Taken now, not at pause time: /clear rotates it. */
		snprintf(a->sid, sizeof a->sid, "%s", r.sid);
		st = respawn_req(a->path, 'h', NULL, 0);
		if (st != DCH_ST_OK)
		{
			ag_say(a, 1, "could not arm the hold (%d); resume message "
			       "%s", st, ag_resume(a, resume));
			return 1;
		}
		if ((st = ag_type(a->path, "/exit", 0)) != 0)
		{
			respawn_req(a->path, 'c', NULL, 0);
			ag_say(a, 1, "%s; hold cancelled", st == -2
			       ? "/exit did not land in the composer, Enter not "
			         "pressed" : "could not type /exit");
			return 1;
		}
		a->step = AG_EXIT;
		a->deadline = now + step;
		return 0;
	}
	if (now < a->deadline)
		return 0;
	ag_say(a, 1, "not ready within %lds (%s); resume message %s", tmo / 1000,
	       a->why, ag_resume(a, resume));
	return 1;
}

/* SIGINT/SIGTERM/SIGHUP during the agent step: the first one is noted, so
** agents still waiting to pause get the resume message instead of staying
** paused; a second one exits at once. */
static volatile sig_atomic_t ag_sig;

static void
ag_on_sig(int sig)
{
	if (ag_sig)
		_exit(128 + sig);
	ag_sig = sig;
}

/* The agent step of `--restart --all`. Returns the number of agents that
** were attempted and failed. */
static int
restart_agents(int force)
{
	struct slist sl = {0}, cur = {0};
	struct agent *ag;
	const char *pmsg = getenv("DCH_PAUSE_MSG"), *rmsg = getenv("DCH_RESUME_MSG");
	const char *self = getenv("DCH_SESSION");
	long tmo = env_ms("DCH_AGENT_TIMEOUT_MS", 600000);
	long settle = env_ms("DCH_AGENT_SETTLE_MS", 3000);
	char selfb[256], want[64] = "";
	int na = 0, k, fails = 0, said = 0, live;
	static const int sigs[] = { SIGINT, SIGTERM, SIGHUP };
	struct sigaction sa, old[3], oldpipe;

	if (!pmsg || !*pmsg)
		pmsg = ag_pause_msg;
	if (!rmsg || !*rmsg)
		rmsg = ag_resume_msg;
	if (self && *self)
		self = canon_name(self, selfb, sizeof selfb);
	list_sessions(&sl);
	ag = calloc((size_t)sl.n + 1, sizeof *ag);
	if (!ag)
	{
		slist_free(&sl);
		return 1;
	}
	for (k = 0; k < sl.n; k++)
	{
		struct agent *a = &ag[na];
		char mpath[8192], mcwd[PATH_MAX], jcwd[PATH_MAX], rc[PATH_MAX];
		const char *skip = NULL;
		struct crec r;
		long mpid;
		int st, n;

		snprintf(a->name, sizeof a->name, "%s", sl.v[k]);
		session_sock_path(sl.v[k], a->path, sizeof a->path);
		st = cmd_query(a->path, &a->ci);
		if (st == DCH_ST_UNSUP)
		{
			printf("%s: skipped — its master predates agent restart "
			       "(--respawn/--cmd)\n", a->name);
			said++;
			continue;
		}
		if (st != 0)
			continue;	/* dead socket: nothing to restart */
		a->argv = a->ci.argv;
		a->argc = a->ci.argc;
		a->tok = claude_tok(a->argv, a->argc);
		if (a->tok < a->argc && strcmp(a->ci.state, "running") != 0)
		{
			/* Ours to arm: someone else's respawn owns the exit. */
			printf("%s: skipped — a respawn is pending (%s)\n", a->name,
			       a->ci.state);
			said++;
			cmd_info_free(&a->ci);
			continue;
		}
		n = a->ci.pid > 0 && strcmp(a->ci.state, "running") == 0
		    ? claude_find(a->ci.pid, 1, &r) : 0;
		mpid = a->ci.pid > 0 ? proc_ppid(a->ci.pid) : -1;
		if (n == 0 && a->tok == a->argc)
		{
			cmd_info_free(&a->ci);
			continue;	/* no Claude in this session */
		}
		said++;
		mpath[0] = mcwd[0] = '\0';
		if (mpid > 1 && proc_env_value(mpid, "PATH", mpath,
		                               sizeof mpath) < 0)
			snprintf(mpath, sizeof mpath, "%s",
			         getenv("PATH") ? getenv("PATH") : "");
		if (mpid > 1 && proc_cwd(mpid, rc, sizeof rc) == 0 &&
		    !realpath(rc, mcwd))
			mcwd[0] = '\0';
		if (n == 0)
			skip = "claude is its command, but no live claude "
			       "session record joins it";
		else if (a->tok == a->argc)
			skip = "claude was started inside the session's shell, "
			       "not as its command";
		else if (proc_depth((long)getpid(), r.pid) >= 0 ||
		         (self && strcmp(self, a->name) == 0))
			skip = "run from outside dch to restart this one";
		else if (!mcwd[0] || !realpath(r.cwd, jcwd) ||
		         strcmp(mcwd, jcwd) != 0)
			skip = "claude's cwd is not the session's launch cwd";
		else if (claude_installed(a->argv[a->tok], mpath, mcwd, a->want,
		                          sizeof a->want) < 0)
			skip = "cannot run `claude --version` as the session "
			       "would (its PATH and cwd)";
		if (skip)
		{
			printf("%s: skipped — %s\n", a->name, skip);
			cmd_info_free(&a->ci);
			continue;
		}
		a->cpid = r.pid;
		snprintf(a->ver, sizeof a->ver, "%s", r.ver);
		if (n > 1)
			printf("%s: %d claude processes in the session; "
			       "restarting the outermost (pid %ld)\n", a->name, n,
			       a->cpid);
		if (force || strcmp(a->ver, a->want) != 0)
		{
			na++;
			continue;
		}
		snprintf(rc, sizeof rc, "%s: already on claude %s", a->name,
		         a->want);
		slist_push(&cur, rc);
		snprintf(want, sizeof want, "%s", a->want);
		said -= n == 1;
		cmd_info_free(&a->ci);
	}
	slist_free(&sl);
	if (cur.n && !na && !said)
		printf("all claude agents already on claude %s\n", want);
	else
		for (k = 0; k < cur.n; k++)
			puts(cur.v[k]);
	slist_free(&cur);
	fflush(stdout);

	/* From the first pause on, a signal must not leave agents paused. */
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = ag_on_sig;
	sa.sa_flags = SA_RESTART;
	sigemptyset(&sa.sa_mask);
	ag_sig = 0;
	for (k = 0; k < 3; k++)
		sigaction(sigs[k], &sa, &old[k]);
	/* A caller that stopped reading (a timed-out Bash tool, `| head`)
	** must not kill us mid-report, leaving the next agents paused. */
	sa.sa_handler = SIG_IGN;
	sigaction(SIGPIPE, &sa, &oldpipe);

	/* Pause every stale agent first, then wait on them all at once: one
	** slow agent must not hold up the rest. */
	for (k = 0; k < na; k++)
	{
		struct agent *a = &ag[k];
		int g = ag_sig ? CG_UNKNOWN : claude_gate(a->path);

		if (ag_sig)
		{
			ag_say(a, 1, "interrupted (signal %d) before its pause; "
			       "nothing typed", (int)ag_sig);
			fails++;
			continue;
		}
		if (g != CG_EMPTY)
		{
			ag_say(a, g != CG_NOVT, g == CG_NOVT ? "skipped — the session "
			       "has no terminal mirror" : "%s; nothing typed",
			       g == CG_NOVT ? "" : cg_why[g]);
			fails += g != CG_NOVT;
			continue;
		}
		/* Stamped before the push: a status written after it proves
		** the agent has seen the message. */
		a->t_sent = wall_ms();
		a->deadline = a->t_sent + tmo;
		a->step = AG_WAIT;
		snprintf(a->why, sizeof a->why, "no status yet");
		if ((g = ag_type(a->path, pmsg, 1)) != 0)
		{
			ag_say(a, 1, g == -2 ? "the pause message did not land in the "
			       "composer; Enter not pressed" : "could not type the "
			       "pause message");
			fails++;
			continue;
		}
		printf("%s: pause message sent; waiting for claude %s to go "
		       "idle\n", a->name, a->ver);
		fflush(stdout);
	}
	do
	{
		live = 0;
		for (k = 0; k < na; k++)
			if (ag[k].step == AG_WAIT && ag_sig)
			{
				/* Past AG_WAIT the hold is armed: those finish, bounded
				** by DCH_AGENT_STEP_MS. */
				ag_say(&ag[k], 1, "interrupted (signal %d) while waiting "
				       "for it to pause; resume message %s", (int)ag_sig,
				       ag_resume(&ag[k], rmsg));
				fails++;
			}
			else if (ag[k].step != AG_DONE)
			{
				fails += ag_tick(&ag[k], tmo, settle, rmsg);
				live += ag[k].step != AG_DONE;
			}
		if (live)
			poll(NULL, 0, AG_POLL_MS);
	} while (live);
	for (k = 0; k < 3; k++)
		sigaction(sigs[k], &old[k], NULL);
	sigaction(SIGPIPE, &oldpipe, NULL);
	for (k = 0; k < na; k++)
		cmd_info_free(&ag[k].ci);
	free(ag);
	return fails;
}

#else /* !HAVE_LIBGHOSTTY_VT */

static int
restart_agents(int force)
{
	struct slist sl = {0};
	struct cmd_info ci;
	char path[1100];
	int k, hit = 0;

	(void)force;
	/* Only worth a word when some session runs claude. */
	list_sessions(&sl);
	for (k = 0; k < sl.n && !hit; k++)
	{
		session_sock_path(sl.v[k], path, sizeof path);
		if (cmd_query(path, &ci) == 0)
		{
			hit = claude_tok(ci.argv, ci.argc) < ci.argc;
			cmd_info_free(&ci);
		}
	}
	slist_free(&sl);
	if (hit)
		puts("agents: not restarted — dch-lite has no terminal mirror to "
		     "watch them with");
	return 0;
}

#endif

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
	       A_REPORT, A_RESTART, A_AGENT_LIST, A_AGENT_SEND,
	       A_RESPAWN, A_CMD }
	    action = A_ATTACH;
	char alias_arg[600] = "";
	int opt_ansi = 0, opt_recent = -1, opt_timeout = 10000, opt_cursor = 0;
	int opt_all = 0, opt_cancel = 0;
	int opt_agent_json = 0;
	int opt_cols = 0, opt_rows = 0;
	char opt_match[DCH_WAIT_MAX + 1] = "";
	char opt_state[40] = "";

	progname = argv[0];

	/* Start with nothing blocked. A signal mask survives fork AND exec, so a
	** parent that blocks a signal hands us one we can never receive: the
	** handler installs fine and simply never runs. Prosper spawns the attach
	** client with forkpty()+execve() from a Swift runtime thread that has
	** SIGWINCH blocked, which silently disabled every window-size change —
	** rotating the phone into landscape left the session at the portrait
	** width, and the resize was pending, undeliverable, forever. Runs before
	** the --master-of dispatch so the session daemon is covered too. */
	{
		sigset_t none;
		sigemptyset(&none);
		sigprocmask(SIG_SETMASK, &none, NULL);
	}

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

	/* Publish them for the master: MSG_RESTART re-execs this same image in
	** place, and after execvp() there is no second chance to look them up. */
	dch_exe = exe;
	dch_argv = argv;

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
		else if (strcmp(a, "--agent-list") == 0)
		{
			action = A_AGENT_LIST;
			i++;
			if (i < argc && strcmp(argv[i], "--json") == 0)
			{
				opt_agent_json = 1;
				i++;
			}
		}
		else if (strcmp(a, "--agent-send") == 0)
		{
			action = A_AGENT_SEND;
			i++;
			if (i >= argc || !argv[i][0])
			{
				fprintf(stderr, "dch: --agent-send needs a peer name and message\n");
				return 1;
			}
			if (strlen(argv[i]) >= sizeof session_name)
			{
				fprintf(stderr, "dch: peer name is too long\n");
				return 1;
			}
			snprintf(session_name, sizeof session_name, "%s", argv[i++]);
			break;
		}
		else if (strcmp(a, "--spawn") == 0 ||
		         strcmp(a, "--send") == 0 ||
		         strcmp(a, "--run") == 0 ||
		         strcmp(a, "--keys") == 0 ||
		         strcmp(a, "--read") == 0 ||
		         strcmp(a, "--wait") == 0 ||
		         strcmp(a, "--status") == 0 ||
		         strcmp(a, "--report") == 0 ||
		         strcmp(a, "--respawn") == 0 ||
		         strcmp(a, "--cmd") == 0)
		{
			/* Control verbs: all take a session name next. */
			action = strcmp(a, "--spawn") == 0 ? A_SPAWN
			       : strcmp(a, "--send") == 0  ? A_SEND
			       : strcmp(a, "--run") == 0   ? A_RUN
			       : strcmp(a, "--keys") == 0  ? A_KEYS
			       : strcmp(a, "--read") == 0  ? A_READ
			       : strcmp(a, "--wait") == 0  ? A_WAIT
			       : strcmp(a, "--status") == 0 ? A_STATUS
			       : strcmp(a, "--respawn") == 0 ? A_RESPAWN
			       : strcmp(a, "--cmd") == 0   ? A_CMD
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
		else if (strcmp(a, "--restart") == 0)
		{
			/* Not folded into the control-verb block above: that
			** block always eats the next arg as a session name, and
			** `--restart --all` has none. */
			action = A_RESTART;
			i++;
			if (i < argc && strcmp(argv[i], "--all") == 0)
			{
				opt_all = 1;
				i++;
			}
			else if (i < argc && argv[i][0] != '\0' &&
			         argv[i][0] != '-')
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				i++;
			}
			else
			{
				fprintf(stderr, "dch: --restart needs a session "
				        "name or --all\n");
				return 1;
			}
		}
		else if (strcmp(a, "--cancel") == 0)
		{
			opt_cancel = 1;
			i++;
		}
		else if (strcmp(a, "--ansi") == 0)
		{
			opt_ansi = 1;
			i++;
		}
		else if (strcmp(a, "--cursor") == 0)
		{
			opt_cursor = 1;
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

	/* --cancel only means something to --respawn; elsewhere it would be
	** silently ignored, so refuse it. */
	if (opt_cancel && action != A_RESPAWN)
	{
		fprintf(stderr, "dch: --cancel only works with --respawn\n");
		return 1;
	}

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
	case A_AGENT_LIST:
		if (inner_argc)
		{
			fprintf(stderr, "dch: --agent-list takes only optional --json\n");
			return 1;
		}
		return dch_bridge_agent_list(opt_agent_json);
	case A_AGENT_SEND:
		if (!inner_argc)
		{
			fprintf(stderr, "dch: --agent-send needs a non-empty message\n");
			return 1;
		}
		return dch_bridge_agent_send(session_name, inner_argc, inner_argv);
	case A_RESTART:
	{
		struct slist sl = {0};
		int k, rc, bad = 0, did = 0, old = 0;

		if (!opt_all)
		{
			rc = do_restart_verb(session_name,
			                     session_version(session_name)
			                         == NULL, 0);
			return rc == 0 ? 0 : 1;
		}

		/* --all restarts only what is actually stale: a restart is
		** cheap but not free (the app repaints, scrollback in the
		** mirror is dropped), so don't spend it on sessions already
		** serving this binary. -f overrides. */
		list_sessions(&sl);
		for (k = 0; k < sl.n; k++)
		{
			const char *v = session_version(sl.v[k]);

			if (!force && v && strcmp(v, DCH_VERSION) == 0)
				continue;
			/* No sidecar at all: age unknown — a master that predates
			** live restart looks identical to a current one whose
			** sidecar tmp_cleaner removed. Either way MSG_RESTART risks
			** the full idle timeout before saying nothing, so skip the
			** wait — but only once we know something is actually there;
			** a stale socket file has no sidecar either. */
			rc = do_restart_verb(sl.v[k], v == NULL, 1);
			if (rc == 2)
				continue;	/* dead socket; nothing to do */
			if (rc == 3)
			{
				old++;	/* already explained on stderr */
				continue;
			}
			did++;
			if (rc != 0)
				bad++;
		}
		slist_free(&sl);
		if (!did && !old)
			printf("all sessions already on dch %s\n", DCH_VERSION);
		else if (!did)
			printf("nothing to restart: %d session%s too old\n",
			       old, old == 1 ? " is" : "s are");
		fflush(stdout);
		/* Then the agents, on masters that now speak --respawn. */
		if (restart_agents(force))
			bad++;
		return bad ? 1 : 0;
	}
	case A_LISTRAW:
	{
		struct slist sl = {0};
		list_sessions(&sl);
		load_aliases(&sl);
		load_harness_names(&sl);
		load_codex_names(&sl);
		int k;
		for (k = 0; k < sl.n; k++)
		{
			char row[1300];
			session_display_name(&sl, k, row, sizeof(row));
			puts(row);
		}
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
		load_harness_names(&sl);
		load_codex_names(&sl);
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
			printf("\",\"harness\":\"");
			for (f = sl.harness[k] ? sl.harness[k] : ""; *f; f++)
				printf(*f == '"' || *f == '\\' ? "\\%c" : "%c", *f);
			/* version is "" when the master predates the sidecar —
			** i.e. definitely older than us, which is exactly what
			** a caller deciding whether to --restart needs. */
			printf("\",\"activity_epoch\":%ld,\"state\":\"%s\""
			       ",\"version\":\"%s\",\"agents\":%d}",
			       activity_epoch(sl.v[k]),
			       session_state(sl.v[k]),
			       version_or_empty(sl.v[k]),
			       detect_agents(sl.v[k]));
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
			return attach_with_switch(exe, 1, 1, inner_argv,
			                          inner_argc);
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
		if (opt_cursor && opt_recent >= 0)
		{
			fprintf(stderr, "dch: --cursor needs the visible screen; "
			        "not valid with --recent\n");
			return 1;
		}
		return do_read_verb(session_name, opt_ansi, opt_recent,
		                    opt_cursor);
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
	case A_RESPAWN:
		if (opt_cancel && inner_argc)
		{
			fprintf(stderr, "dch: --respawn --cancel takes no "
			        "command\n");
			return 1;
		}
		return do_respawn_verb(session_name, inner_argv, inner_argc,
		                       opt_cancel);
	case A_CMD:
		return do_cmd_verb(session_name);
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
		return attach_with_switch(exe, forced_name, force,
		                          inner_argv, inner_argc);
	}
	return 0;
}
