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
#include <poll.h>
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

/* Path of the metadata sidecar (written by --meta, e.g. from a status line).
** Returns 0 on success, -1 on truncation. */
static int
meta_file_path(const char *name, char *buf, size_t bufsz)
{
	char cb[256];
	int n = snprintf(buf, bufsz, "%s/%s.sock.meta", sock_dir,
	                 canon_name(name, cb, sizeof(cb)));
	return (n < 0 || (size_t)n >= bufsz) ? -1 : 0;
}

/* Session metadata is one JSON object, at most this many bytes, no byte
** below 0x20 (keeps --ls-json one line per call). */
#define DCH_META_MAX 16384

static int
meta_bytes_ok(const char *s, size_t n)
{
	size_t i;
	if (n == 0 || n > DCH_META_MAX || s[0] != '{')
		return 0;
	for (i = 0; i < n; i++)
		if ((unsigned char)s[i] < 0x20)
			return 0;
	return 1;
}

/* Stored metadata, or NULL; *epoch gets the sidecar mtime (when the content
** last changed: --meta skips identical pushes) or 0. The file is validated
** in full again on read: a truncated, hand-edited or third-party-written
** sidecar printed verbatim would break the whole --ls-json array, not just
** this row. NOFOLLOW + NONBLOCK + S_ISREG + uid, as for .ver: the runtime dir
** is shared. Static buffer; valid until the next call. */
static const char *
session_meta(const char *name, long *epoch)
{
	static char buf[DCH_META_MAX + 1];
	char p[1300];
	struct stat st;
	ssize_t r = -1;
	int fd;

	*epoch = 0;
	if (meta_file_path(name, p, sizeof(p)) != 0 ||
	    (fd = open(p, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)) < 0)
		return NULL;
	if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) &&
	    st.st_uid == getuid() && st.st_size <= DCH_META_MAX)
		r = read(fd, buf, DCH_META_MAX + 1);
	close(fd);
	if (r <= 0 || !meta_bytes_ok(buf, (size_t)r))
		return NULL;
	buf[r] = '\0';
	if (!dch_json_is_object(buf))
		return NULL;
	*epoch = (long)st.st_mtime;
	return buf;
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

#ifdef __APPLE__
/* KERN_PROCARGS2 of a same-uid process: argc int, exec path, NULs, argv[],
** env[]. Static buffer, NUL-terminated; NULL on failure. */
static char *
procargs(long pid, size_t *lenp)
{
	static char *buf;
	static size_t bufsz;
	int mib[3] = { CTL_KERN, KERN_PROCARGS2, (int)pid };
	size_t len;

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
			return NULL;
	}
	len = bufsz;
	if (sysctl(mib, 3, buf, &len, NULL, 0) != 0 || len <= sizeof(int))
		return NULL;
	buf[len - 1] = '\0';
	*lenp = len;
	return buf;
}
#endif

/* Fetch one environment value from another same-uid process. macOS uses
** KERN_PROCARGS2; elsewhere /proc/<pid>/environ. */
static int
proc_env_value(long pid, const char *key, char *out, size_t outsz)
{
	size_t keylen = strlen(key);
#ifdef __APPLE__
	size_t len, i;
	char *buf = procargs(pid, &len);

	if (!buf)
		return -1;
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

/* argv of a same-uid process as one malloc'd NULL-terminated block, or NULL.
** A script runs as `<interpreter> <script> args`: when argv[1] is an
** executable "#!" file the interpreter is dropped, so a shell-launched
** `headroom wrap claude` reads back as `/…/headroom wrap claude`, which
** retypes the same, not as `python3 /…/headroom wrap claude`. */
static char **
proc_argv(long pid)
{
	char **v, *s, hb[2];
	size_t len, i, start;
	int argc, k, fd;
#ifdef __APPLE__
	char *buf = procargs(pid, &len);

	if (!buf)
		return NULL;
	memcpy(&argc, buf, sizeof argc);
	for (i = sizeof(int); i < len && buf[i]; i++)
		;	/* the exec path */
	while (i < len && !buf[i])
		i++;
#else
	static char buf[65536];
	char p[64];
	ssize_t r;

	snprintf(p, sizeof p, "/proc/%ld/cmdline", pid);
	if ((fd = open(p, O_RDONLY)) < 0)
		return NULL;
	for (len = 0; len < sizeof buf - 1 &&
	     (r = read(fd, buf + len, sizeof buf - 1 - len)) > 0; )
		len += (size_t)r;
	close(fd);
	buf[len] = '\0';
	for (argc = 0, i = 0; i < len; i++)
		argc += buf[i] == '\0';
	i = 0;
#endif
	start = i;
	for (k = 0; k < argc && i < len; k++)
		i += strlen(buf + i) + 1;
	if (argc < 1 || k < argc ||
	    !(v = malloc((size_t)(argc + 1) * sizeof *v + (i - start))))
		return NULL;
	s = memcpy(v + argc + 1, buf + start, i - start);
	for (k = 0; k < argc; k++, s += strlen(s) + 1)
		v[k] = s;
	v[argc] = NULL;
	if (argc > 1 && v[1][0] == '/' && access(v[1], X_OK) == 0 &&
	    (fd = open(v[1], O_RDONLY | O_NONBLOCK)) >= 0)
	{
		int bang = read(fd, hb, 2) == 2 && hb[0] == '#' && hb[1] == '!';

		close(fd);
		if (bang)
			memmove(v, v + 1, (size_t)argc * sizeof *v);
	}
	return v;
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

/* Claude Code's live-session records: $CLAUDE_CONFIG_DIR/sessions, else
** ~/.claude/sessions. */
static int
claude_sessions_dir(char *dir, size_t dirsz)
{
	const char *base = getenv("CLAUDE_CONFIG_DIR"), *home = getenv("HOME");

	if ((!base || !*base) && (!home || !*home))
		return -1;
	return snprintf(dir, dirsz, "%s%s/sessions", base && *base ? base : home,
	                base && *base ? "" : "/.claude") >= (int)dirsz ? -1 : 0;
}

/* Did claude `pid` end cleanly? Claude Code removes its sessions/<pid>.json
** on a clean exit; a SIGKILLed one (out of memory) leaves it behind. */
int
dch_claude_gone(long pid)
{
	char dir[1100], p[1200];

	return claude_sessions_dir(dir, sizeof dir) < 0 ||
	       snprintf(p, sizeof p, "%s/%ld.json", dir, pid) >= (int)sizeof p ||
	       access(p, F_OK) != 0;
}

int
dch_visit_claude_records(dch_claude_record_fn visit, void *arg)
{
	char dir[1100];
	DIR *d;
	struct dirent *de;

	if (claude_sessions_dir(dir, sizeof dir) < 0 || !(d = opendir(dir)))
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

/* mark: ".killed" (-k) or ".stopped" (--stop, which wrote it already). */
static int
end_session(const char *name, const char *mark)
{
	char sp[1100];
	session_sock_path(name, sp, sizeof(sp));
	struct stat st;
	if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
	{
		fprintf(stderr, "no session: %s\n", name);
		return 1;
	}

	/* Mark the end as deliberate before the master hears of it: its
	** history record then ends "killed" or "stopped", which --revive
	** --all skips. */
	char pat[1200];
	int kfd;
	snprintf(pat, sizeof(pat), "%s%s", sp, mark);
	if ((kfd = open(pat, O_WRONLY | O_CREAT | O_NOFOLLOW, 0600)) >= 0)
		close(kfd);

	/* Term master (its sentinel argv contains the sock path). */
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
	if (meta_file_path(name, side, sizeof(side)) == 0)
		unlink(side);
	return 0;
}

static int
kill_session(const char *name)
{
	if (end_session(name, ".killed") != 0)
		return 1;
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
	    "  dch --restart <name>|--all --include-agents [-f]\n"
	    "                                             then also restart stale\n"
	    "                                             Claude Code agents (-f:\n"
	    "                                             current ones too); off\n"
	    "                                             by default, --all too\n"
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
	    "  dch --meta <name> '<json object>'          attach metadata (status\n"
	    "                                             lines, hooks); `clear`\n"
	    "                                             drops it; shown by\n"
	    "                                             --ls-json as \"meta\"\n"
	    "  dch --history [--json]                     sessions that ended:\n"
	    "                                             name, when, how, cwd,\n"
	    "                                             command, claude session\n"
	    "  dch --revive <name> [--as <new>]           start an ended session\n"
	    "                                             again: its cwd, its\n"
	    "                                             command, claude resumed\n"
	    "                                             and told to carry on\n"
	    "  dch --revive --all                         every session that died\n"
	    "                                             (not exit 0, -k or\n"
	    "                                             --stop)\n"
	    "                                             in the last 3 days\n"
	    "                                             after 5+ minutes;\n"
	    "                                             others by name\n"
	    "  dch --say <name>...|--all [--json] -- <text...>\n"
	    "                                             type a message into\n"
	    "                                             each session's Claude\n"
	    "                                             Code, only when it is\n"
	    "                                             idle at an empty\n"
	    "                                             composer; one result\n"
	    "                                             per session: sent,\n"
	    "                                             skipped (busy, prompt,\n"
	    "                                             draft, ...) or failed\n"
	    "  dch --stop <name>...|--all [--json]        pause each session's\n"
	    "                                             Claude Code, /exit it,\n"
	    "                                             keep its resume id and\n"
	    "                                             end the session as\n"
	    "                                             \"stopped\"; dch --revive\n"
	    "                                             <name> wakes it resumed\n"
	    "  dch --ls-json    like -lj but JSON, adds \"state\"\n"
	    "  dch --agent-list [--json]                  list native Claude peers\n"
	    "  dch --agent-send <name> <message...>       message one native peer\n"
	    "Env: DCH_NO_DETECT=1 disables screen-content state detection.\n"
	    "     DCH_NO_HISTORY=1 at session start keeps no history record.\n"
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
static int control_connect_path(const char *path);

static long long
wall_ms(void)
{
	struct timeval tv;

	gettimeofday(&tv, NULL);
	return (long long)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

static int
spawn_master(char *exe, const char *name, const char *sockp,
             char **inner_argv)
{
	char msg[1024];
	size_t n = 0;
	ssize_t got;
	int fd[2];
	pid_t pid;

	if (pipe(fd) < 0)
	{
		fprintf(stderr, "dch: pipe: %s\n", strerror(errno));
		return -1;
	}
	pid = fork();
	if (pid < 0)
	{
		fprintf(stderr, "dch: fork: %s\n", strerror(errno));
		close(fd[0]);
		close(fd[1]);
		return -1;
	}
	if (pid == 0)
	{
		/* Child: become session leader, drop tty, exec sentinel. Its
		** stdout is the status pipe: the master holds it until the
		** program is exec'd, and prints there whatever stops it. */
		setsid();
		close(fd[0]);
		dup2(fd[1], 1);
		if (fd[1] > 2)
			close(fd[1]);
		int nullfd = open("/dev/null", O_RDWR);
		if (nullfd >= 0)
		{
			dup2(nullfd, 0);
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
		/* exe is absolute after main() resolved argv[0]; execvp only
		** matters for the bare-name fallback, when nothing on PATH
		** matched and exe is still the original argv[0]. */
		execvp(av[0], av);
		dprintf(1, "%s: %s\n", exe, strerror(errno));
		_exit(127);
	}
	dch_trace("spawn_master child=%d sock=%s", (int)pid, sockp);
	/* EOF on the status pipe: the program exec'd (or the master died,
	** which the probe below sees). Any text is why it did not start. */
	close(fd[1]);
	{
		long long left, end = wall_ms() + 10000;
		struct pollfd pf = { fd[0], POLLIN, 0 };

		for (;;)
		{
			if ((left = end - wall_ms()) <= 0)
			{
				fprintf(stderr, "dch: master did not accept "
				        "within 10s\n");
				close(fd[0]);
				return -1;
			}
			if (poll(&pf, 1, (int)left) <= 0)
				continue;
			got = read(fd[0], msg + n, sizeof msg - 1 - n);
			if (got < 0 && errno == EINTR)
				continue;
			if (got <= 0 || (n += (size_t)got) == sizeof msg - 1)
				break;
		}
	}
	close(fd[0]);
	if (n)
	{
		msg[n] = '\0';
		chomp(msg);
		fprintf(stderr, "dch: %s: %s\n", name, msg);
		return -1;
	}
	/* Parent: wait until the master accepts, so the caller's own connect
	** cannot land before bind() or between bind() and listen(). Poll on a
	** fine 5ms quantum against a 10s wall-clock cap (an iteration count
	** overran it under load): the master binds in a few ms, so a coarse
	** quantum just adds dead time to every cold start, while a slow runner
	** may need seconds to exec. The probe is a connect+close the master
	** drops as a plain EOF. Out of time is a failure (-1) with its own
	** message; the still-starting master is left alone. */
	int status = 0;
	long long end = wall_ms() + 10000;
	for (;;)
	{
		int probe = control_connect_path(sockp);
		if (probe >= 0)
		{
			close(probe);
			return 0;
		}
		if (wall_ms() >= end)
			break;
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
	fprintf(stderr, "dch: master did not accept within 10s\n");
	return -1;
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
	if (spawn_master(exe, session_name, sock_path, inner_argv) < 0)
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
** is prior art for the approach; nothing is ported from it — AGPL).
** A leading '^' on s1 makes a line rule: only a short line that starts
** with s1 (after leading blanks) and contains s2 matches — the footer
** itself, not prose that quotes it. */
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
	{ "^esc to cancel", "tab to amend" },              /* claude perms  */
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

/* Longest footer line a line rule accepts, blanks trimmed (bytes;
** the real one with its optional extra hints is ~50). */
#define DET_LINE_MAX 64

/* Line rule r ('^' s1) against the lines of hay. */
static int
det_line(const struct det_rule *r, const char *hay)
{
	const char *p = hay, *e, *q, *s1 = r->s1 + 1;

	while (*p)
	{
		e = strchr(p, '\n');
		if (!e)
			e = p + strlen(p);
		q = e;
		while (p < e && (*p == ' ' || *p == '\t'))
			p++;
		while (q > p && (q[-1] == ' ' || q[-1] == '\t' ||
		                 q[-1] == '\r'))
			q--;
		if (q - p <= DET_LINE_MAX &&
		    !strncmp(p, s1, strlen(s1)))
			for (q = p; q < e; q++)
				if (!strncmp(q, r->s2, strlen(r->s2)))
					return 1;
		p = *e ? e + 1 : e;
	}
	return 0;
}

static int
det_match(const struct det_rule *t, size_t n, const char *hay)
{
	size_t i;
	for (i = 0; i < n; i++)
		if (t[i].s1[0] == '^' ? det_line(&t[i], hay) :
		    strstr(hay, t[i].s1) &&
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
              char **inner_argv, int inner_argc, int quiet)
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
	if (spawn_master(exe, name, sock_path, inner_argv) < 0)
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

	/* No READ_END is not a failure: spawn_master saw the program exec,
	** and a master gone before it answered means it already exited
	** (`--spawn s true`). Its end is in the history record. */
	if (!quiet)
		printf("%s\n", name);
	return 0;
}

/* ---- agent-aware --restart --all (Claude Code) ----------------------
**
** After the master pass, `--restart --all` restarts the Claude Code agents
** that run AS a session's command once the installed CLI differs from the
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

/* First regular, executable <dir>/<name> along path, the way execvp()
** searches it: an empty component means cwd. Leaves the hit in out and
** returns 0, or returns -1 with out empty. Shared with the master's record. */
int
path_lookup(const char *name, const char *path, const char *cwd,
            char *out, size_t outsz)
{
	const char *dir = path, *e;
	struct stat st;

	for (; dir; dir = e ? e + 1 : NULL)
	{
		int l;

		e = strchr(dir, ':');
		l = e ? (int)(e - dir) : (int)strlen(dir);
		snprintf(out, outsz, "%.*s/%s", l ? l : (int)strlen(cwd),
		         l ? dir : cwd, name);
		if (stat(out, &st) == 0 && S_ISREG(st.st_mode) &&
		    access(out, X_OK) == 0)
			return 0;
	}
	out[0] = '\0';
	return -1;
}

/* One Claude session record (~/.claude/sessions/<pid>.json). */
struct crec
{
	long pid, upd;		/* upd = statusUpdatedAt, epoch ms */
	int depth;		/* generations below the scan root */
	char sid[64], ver[64], status[32], cwd[1024], name[256];
};

struct crec_scan
{
	long root;
	int verify, n;		/* n = live records under root */
	struct crec best;	/* the shallowest of them */
};

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
	if (dch_json_string(body, "name", c.name, sizeof c.name) < 0)
		c.name[0] = '\0';
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

/* ---- session history (#042) ----------------------------------------- */

/* Where the masters keep one record per session:
** $XDG_STATE_HOME/dch/sessions/<slug>, else ~/.local/state/dch/sessions/<slug>
** — not the socket dir, the records must outlive a reboot. <slug> is the
** socket dir with '/' as '-' (/tmp/dch-502 -> -tmp-dch-502): a test run
** with its own runtime dir must never write, or revive, the real dir's
** sessions. sockdir NULL = the sessions/ root; a relative sockdir keeps no
** records (the slug would depend on the cwd). create = make what is
** missing, 0700. Like the socket dir it must be ours and closed to others:
** a planted record would be revived as a command. 0 ok, -1 not usable. */
int
dch_history_dir(char *out, size_t outsz, const char *sockdir, int create)
{
	const char *x = getenv("XDG_STATE_HOME"), *h = getenv("HOME");
	struct stat st;
	char *p;
	int n;

	if (sockdir && *sockdir != '/')
		return -1;
	if (x && *x == '/')
		n = snprintf(out, outsz, "%s/dch/sessions/", x);
	else if (h && *h)
		n = snprintf(out, outsz, "%s/.local/state/dch/sessions/", h);
	else
		return -1;
	if (n < 0 || (size_t)n >= outsz ||
	    snprintf(out + n, outsz - (size_t)n, "%s", sockdir ? sockdir : "") >=
	        (int)(outsz - (size_t)n))
		return -1;
	for (p = out + n; *p; p++)
		if (*p == '/')
			*p = '-';
	if (!sockdir)
		out[n - 1] = '\0';
	for (p = out; create && p; )
	{
		p = strchr(p + 1, '/');
		if (p)
			*p = '\0';
		if (mkdir(out, 0700) < 0 && errno != EEXIST &&
		    (stat(out, &st) < 0 || !S_ISDIR(st.st_mode)))
			create = 0;
		if (p)
			*p = '/';
	}
	return lstat(out, &st) == 0 && S_ISDIR(st.st_mode) &&
	       st.st_uid == getuid() && !(st.st_mode & 022) ? 0 : -1;
}

/* The Claude Code agent under a session's child, for the master's record:
** the shallowest live claude below `child`, and its launch argv — the
** outermost process on the way down to it, below the child: what was typed
** into a shell (`headroom wrap claude --chrome`), else claude itself. 0 and
** a filled *a (a->launch malloc'd), or -1 when there is none. */
int
dch_agent_probe(long child, struct dch_agent *a)
{
	struct crec r;
	long p, pp;

	memset(a, 0, sizeof *a);
	if (child <= 0 || claude_find(child, 1, &r) == 0)
		return -1;
	for (p = r.pid; p != child && (pp = proc_ppid(p)) > 1 && pp != child;
	     p = pp)
		;
	a->pid = r.pid;
	snprintf(a->sid, sizeof a->sid, "%s", r.sid);
	snprintf(a->name, sizeof a->name, "%s", r.name);
	snprintf(a->cwd, sizeof a->cwd, "%s", r.cwd);
	a->launch = proc_argv(p);
	return 0;
}

/* ---- --stop: pause, /exit, end the session (#046) --------------------
**
** `--stop <name>...|--all` ends sessions for --revive to bring back: the
** session's Claude Code (its command, or typed into its shell) is paused
** and /exit'ed the way --restart --include-agents does it, its resume id
** read from the exit line; then the session ends as `dch -k` ends one, but
** marked <sock>.stopped, holding the agent: the history record ends
** "stopped" with that agent even though claude quit cleanly first. A
** session without a live Claude ends at once. */

static void
put_json_str(const char *s)
{
	size_t cap = strlen(s) * 6 + 3;
	char *q = malloc(cap);

	fputs(q && dch_json_quote(q, cap, s) != (size_t)-1 ? q : "\"\"", stdout);
	free(q);
}

/* --json: {"name","phase"} as a session reaches pausing|exiting|ending. */
static void
stop_phase(const char *name, const char *phase, int json)
{
	if (!json)
		return;
	fputs("{\"name\":", stdout);
	put_json_str(name);
	printf(",\"phase\":\"%s\"}\n", phase);
	fflush(stdout);
}

/* The one result line per session; res ok|skipped|failed|interrupted. */
static void
stop_print(const char *name, const char *res, const char *code,
           const char *detail, const char *aname, const char *sid, int json)
{
	if (!json && strcmp(res, "ok") != 0)
		printf("%s: %s — %s\n", name, res, detail);
	else if (!json)
		printf("%s: stopped (%s%s%s%s%srevive with dch --revive %s)%s%s\n",
		       name, *sid ? "claude " : "", aname, *sid && *aname ? " " : "",
		       sid, *sid ? "; " : "", name, *detail ? " " : "", detail);
	else
	{
		fputs("{\"name\":", stdout);
		put_json_str(name);
		printf(",\"result\":\"%s\",\"reason\":\"%s\",\"detail\":", res,
		       code);
		put_json_str(detail);
		fputs(",\"session_id\":", stdout);
		put_json_str(sid);
		fputs("}\n", stdout);
	}
	fflush(stdout);
}

/* <path>.stopped: the agent object (ag), or empty; ag NULL and drop =
** remove it. From the moment it is there the master's record keeps that
** agent. 0, or -1. */
static int
stop_mark(const char *path, const struct dch_agent *ag, int drop)
{
	char mk[1200], tmp[1300];
	FILE *f = NULL;
	int fd, ok;

	snprintf(mk, sizeof mk, "%s.stopped", path);
	if (drop)
		return unlink(mk);
	snprintf(tmp, sizeof tmp, "%s.%d", mk, (int)getpid());
	fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
	if (fd >= 0 && !(f = fdopen(fd, "w")))
		close(fd);
	ok = f != NULL;
	if (f && ag)
		dch_agent_json(f, ag);
	if (f && fclose(f) != 0)
		ok = 0;
	if (!ok || rename(tmp, mk) != 0)
	{
		unlink(tmp);
		return -1;
	}
	return 0;
}

/* Mark the session stopped (stop_mark), end it and wait for its master
** (mpid) to write the record. NULL, or why it did not end. */
static const char *
stop_end(const char *name, const char *path, long mpid,
         const struct dch_agent *ag)
{
	long long end;

	if (stop_mark(path, ag, 0) < 0)
	{
		stop_mark(path, NULL, 1);
		return "could not write its .stopped marker";
	}
	if (end_session(name, ".stopped") != 0)
	{
		stop_mark(path, NULL, 1);
		return "the session went away";
	}
	/* ponytail: 10 s for the master's exit, so the record is final when
	** we say stopped; a --revive right after reads "stopped". */
	for (end = wall_ms() + 10000; mpid > 1 && kill((pid_t)mpid, 0) == 0;
	     poll(NULL, 0, 50))
		if (wall_ms() >= end)
			return "its master did not exit within 10s";
	return NULL;
}

#ifdef HAVE_LIBGHOSTTY_VT

#define AG_POLL_MS  250
/* Paste, then Enter: Enter in the same burst can fold into the paste. */
#define AG_ENTER_MS 300
#define AG_REENTER  5	/* Enters after the first one that did not take */

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
/* --stop's (#046); the resume one goes to an agent it gives up on. */
static const char stop_pause_msg[] =
    "pause all work, this session is being stopped by dch --stop and will "
    "be resumed later with --resume. Paused means: stop every background "
    "shell and monitor (TaskStop), stop or finish your subagents, cancel "
    "any /loop, ScheduleWakeup and session crons, and note what you "
    "stopped so it can be re-armed after the resume. Then say that all "
    "work is paused and you are ready to be stopped.";
static const char stop_resume_msg[] =
    "resume all work. dch --stop gave up and left this session running: "
    "re-arm the shells, monitors, subagents, loops, wakeups and crons you "
    "stopped for the pause, then carry on where you left off.";

/* What the screen lets us type into (claude_gate). */
enum { CG_NOVT = -1, CG_EMPTY, CG_DIALOG, CG_DRAFT, CG_QUEUED, CG_UNKNOWN };
static const char *const cg_why[] = {
	"the composer is empty", "a prompt or dialog is waiting for you",
	"the composer holds an unsent draft", "a queued message is pending",
	"the composer is not on screen",
};
static const char *const cg_code[] = {	/* --stop's reasons, as --say's */
	"", "prompt", "draft", "queued", "no_screen",
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
	/* --stop: no relaunch; the session ends instead */
	int stop, json;
	long mpid;		/* the master, to wait out its exit */
	struct dch_agent ag;	/* probed right before /exit */
	const char *res, *code;	/* the result, when not by failed/step */
};

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
** when the text did not land (no Enter pressed), -3 when it landed but
** stayed in the composer through every Enter. */
static int
ag_type(const char *path, const char *text, int paste)
{
	/* How long typed text may take to show in the composer: a Claude that
	** has just resumed a long conversation took over a second to paint a
	** paste. */
	long land = env_ms("DCH_AGENT_LAND_MS", 3000);
	/* An Enter may not take: right after --resume, Claude Code 2.1.309
	** showed the paste but ignored an Enter and another ~6 s later; one
	** ~20 s after the spawn submitted. So Enter again while the text still
	** sits in the composer, AG_REENTER times at most, this far apart. A
	** dialog never gets one: composer_holds() reads it as not holding. */
	long gap = env_ms("DCH_AGENT_REENTER_MS", 5000);
	long long next;
	int s, t, n;

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
			break;
		if (t >= land)
			return -2;
	}
	for (n = 0; ; n++)
	{
		if (dch_pty_push(path, NULL) < 0)
			return -1;
		for (next = wall_ms() + gap; wall_ms() < next; )
		{
			poll(NULL, 0, AG_POLL_MS);
			if (!composer_holds(path, text))
				return 0;
		}
		if (n == AG_REENTER)
			return -3;
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

/* `claude --version` of the binary the relaunch will run: the token itself
** when it has a '/' (relative to the session's cwd), else the first match on
** the MASTER's PATH, which is the env the respawned argv gets. */
static int
claude_installed(const char *tok, const char *mpath, const char *mcwd,
                 char *out, size_t outsz)
{
	char exe[2048], buf[256], *sp;
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
		path_lookup(tok, mpath, mcwd, exe, sizeof exe);
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
	char d[2048];

	va_start(ap, fmt);
	if (a->stop)
	{
		vsnprintf(d, sizeof d, fmt, ap);
		stop_print(a->name, a->res ? a->res : failed ? "failed" : "ok",
		           a->code ? a->code : a->step == AG_WAIT ? "not_idle"
		                                                  : "exit_failed",
		           d, a->ag.name, failed ? "" : a->id, a->json);
	}
	else
	{
		printf("%s: %s", a->name, failed ? "NOT restarted — " : "");
		vprintf(fmt, ap);
		putchar('\n');
		fflush(stdout);
	}
	va_end(ap);
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
	case -3:
		return "typed but NOT sent (it stayed in the composer through "
		       "every Enter)";
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

/* A master that does not answer is not a gone session: -1/-2 it went
** quiet, -4 with the socket still there is a refused connect (a full
** listen backlog). Only a missing socket means the session ended. */
static int
ag_quiet(const struct agent *a, int st)
{
	struct stat sb;

	return st == -1 || st == -2 ||
	       (st == -4 && (stat(a->path, &sb) == 0 || errno != ENOENT));
}

/* Respawn the held session as claude --resume <id>. A master that did not
** answer may or may not have taken it: AG_START sends it again while the
** session still reads held. Only a refusal cancels, which ends the
** session. Returns 1 when it failed. */
static int
ag_relaunch(struct agent *a)
{
	char **nv;
	int nc, st;

	nv = resume_argv(a->argv, a->argc, a->tok, a->id, &nc);
	st = nv ? respawn_req(a->path, 'a', nv, nc) : -3;
	free(nv);
	if (st == DCH_ST_OK || ag_quiet(a, st))
	{
		a->step = AG_START;
		return 0;
	}
	respawn_req(a->path, 'c', NULL, 0);
	ag_say(a, 1, "relaunch with --resume %s was refused (%d); "
	       "session ended", a->id, st);
	return 1;
}

/* The id to resume into: the exit line's is primary; the record's (read
** right before /exit, as /clear rotates it) is the fallback and the
** cross-check. */
static void
ag_read_id(struct agent *a, const struct cmd_info *ci)
{
	char tid[64];

	if (resume_id(ci->tail, ci->tail_len, tid, sizeof tid) < 0)
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
}

/* The hold is armed and /exit typed: wait for the exit, then relaunch. */
static int
ag_exited(struct agent *a, const char *step_s)
{
	struct cmd_info ci;
	int st;

	st = cmd_query(a->path, &ci);
	if (ag_quiet(a, st))
	{
		if (wall_ms() < a->deadline)
			return 0;
		/* No cancel: to a held master it would end the session. */
		ag_say(a, 1, "the master stopped answering after /exit (silent "
		       "at the %s limit); hold left as is, it ends after "
		       "DCH_HOLD_MS; resume by hand: claude --resume %s",
		       step_s, a->sid);
		return 1;
	}
	if (st != 0)
	{
		ag_say(a, 1, "the session went away during /exit; resume by "
		       "hand: claude --resume %s", a->sid);
		return 1;
	}
	if (strcmp(ci.state, "held") != 0)
	{
		if (strcmp(ci.state, "running") == 0)
			ag_say(a, 1, "the hold was dropped (expired or cancelled) "
			       "before claude exited (session %s)", a->sid);
		else if (wall_ms() >= a->deadline)
		{
			respawn_req(a->path, 'c', NULL, 0);
			ag_say(a, 1, "/exit did not end claude within %s; hold "
			       "cancelled, claude left running (session %s)",
			       step_s, a->sid);
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
			       " not relaunched, session ended%s%s%s; resume by "
			       "hand: claude --resume %s", ci.sig,
			       last[0] ? " (last output: " : "", last,
			       last[0] ? ")" : "", a->sid);
		else
			ag_say(a, 1, "claude exited with status %d instead of 0;"
			       " not relaunched, session ended%s%s%s; resume by "
			       "hand: claude --resume %s", ci.code,
			       last[0] ? " (last output: " : "", last,
			       last[0] ? ")" : "", a->sid);
		cmd_info_free(&ci);
		return 1;
	}
	ag_read_id(a, &ci);
	cmd_info_free(&ci);
	a->deadline = wall_ms() + env_ms("DCH_AGENT_STEP_MS", 60000);
	return ag_relaunch(a);
}

/* --stop: the hold is armed and /exit typed. Once claude is gone — the
** session held when claude was its command, the shell left when it was
** typed into one — the exit line's id goes into the marker and the session
** ends. Not gone in time: disarmed, claude told to carry on. */
static int
stop_exited(struct agent *a, const char *step_s, const char *resume)
{
	struct cmd_info ci;
	const char *why;
	int st = cmd_query(a->path, &ci), held = 0, gone = 0;

	if (st == 0)
	{
		held = strcmp(ci.state, "held") == 0;
		gone = held || kill((pid_t)a->cpid, 0) < 0;
		if (!gone)
			cmd_info_free(&ci);
	}
	if (!gone && (st == 0 || ag_quiet(a, st)))
	{
		if (wall_ms() < a->deadline)
			return 0;
		stop_mark(a->path, NULL, 1);
		if (st == 0)	/* not held: this only disarms */
			respawn_req(a->path, 'c', NULL, 0);
		ag_say(a, 1, "%s within %s; resume message %s", st == 0
		       ? "/exit did not end claude" : "the master stopped "
		       "answering after /exit", step_s, ag_resume(a, resume));
		return 1;
	}
	if (st != 0)
	{
		stop_mark(a->path, NULL, 1);
		ag_say(a, 1, "the session went away during /exit");
		return 1;
	}
	/* From a shell, the exit line may still sit in the pty: give the
	** master a tick to read it into the tail. */
	if (!held && !a->ok_since)
	{
		cmd_info_free(&ci);
		a->ok_since = wall_ms();
		return 0;
	}
	ag_read_id(a, &ci);
	cmd_info_free(&ci);
	snprintf(a->ag.sid, sizeof a->ag.sid, "%s", a->id);
	stop_phase(a->name, "ending", a->json);
	if ((why = stop_end(a->name, a->path, a->mpid, &a->ag)))
	{
		a->code = "end_failed";
		ag_say(a, 1, "claude quit (claude --resume %s), but %s", a->id,
		       why);
		return 1;
	}
	a->res = "ok";
	a->code = "";
	ag_say(a, 0, "%s", a->note[0] ? a->note + 1 : "");
	return 0;
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
		return a->stop ? stop_exited(a, step_s, resume)
		               : ag_exited(a, step_s);
	if (a->step == AG_START)
	{
		struct cmd_info ci;
		int st = cmd_query(a->path, &ci);

		if (ag_quiet(a, st))
		{
			if (now < a->deadline)
				return 0;
			ag_say(a, 1, "relaunched with --resume %s, but the master "
			       "stopped answering (silent at the %s limit); resume "
			       "message not typed", a->id, step_s);
			return 1;
		}
		if (st != 0)
		{
			ag_say(a, 1, "relaunched with --resume %s, but it exited "
			       "and the session ended", a->id);
			return 1;
		}
		/* Still held: the relaunch went to a stalled master and never
		** applied (the relaunched claude is not armed, so it never reads
		** held). Send it again, within the same step limit. */
		if (strcmp(ci.state, "held") == 0)
		{
			cmd_info_free(&ci);
			if (now < a->deadline)
				return ag_relaunch(a);
			ag_say(a, 1, "relaunched with --resume %s, but claude did not "
			       "start within %s; hold left as is, it ends after "
			       "DCH_HOLD_MS", a->id, step_s);
			return 1;
		}
		/* dch never arms the relaunched claude, so "armed" here is a late
		** duplicate 'a': a relaunch that timed out, was re-sent above while
		** the session read held, then applied after all. Disarm it once,
		** or claude's next exit would relaunch it again. Not held, so 'c'
		** only disarms (a held session would end on it). */
		if (strcmp(ci.state, "armed") == 0)
			respawn_req(a->path, 'c', NULL, 0);
		g = CG_UNKNOWN;
		if (ci.pid > 0 && claude_find(ci.pid, 1, &r) > 0 &&
		    strcmp(r.status, "idle") == 0 &&
		    (g = claude_gate(a->path)) == CG_EMPTY)
		{
			int old = strcmp(r.ver, a->want) != 0;
			int t;

			cmd_info_free(&ci);
			t = ag_type(a->path, resume, 1);
			if (t == -1)
			{
				ag_say(a, 1, "relaunched with --resume %s on claude %s, "
				       "but could not type the resume message",
				       a->id, r.ver);
				return 1;
			}
			if (t != 0)
			{
				ag_say(a, 1, "relaunched with --resume %s on claude %s, "
				       "but the resume message %s", a->id, r.ver,
				       t == -3 ? "stayed in the composer through every "
				       "Enter" : "did not land in the composer; Enter "
				       "not pressed");
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
		a->code = "claude_exited";
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

		a->code = "exit_failed";
		/* --stop keeps the agent for --revive: its launch command can
		** only be read while claude runs. */
		if (a->stop && dch_agent_probe(a->ci.pid, &a->ag) < 0)
		{
			ag_say(a, 1, "could not read claude's launch command; "
			       "resume message %s", ag_resume(a, resume));
			return 1;
		}
		stop_phase(a->name, "exiting", a->json);
		/* Taken now, not at pause time: /clear rotates it. */
		snprintf(a->sid, sizeof a->sid, "%s", r.sid);
		st = respawn_req(a->path, 'h', NULL, 0);
		if (st != DCH_ST_OK)
		{
			ag_say(a, 1, "could not arm the hold (%d); resume message "
			       "%s", st, ag_resume(a, resume));
			return 1;
		}
		/* --stop: a record refresh after /exit must keep the agent */
		if (a->stop && stop_mark(a->path, &a->ag, 0) < 0)
		{
			stop_mark(a->path, NULL, 1);
			respawn_req(a->path, 'c', NULL, 0);
			ag_say(a, 1, "could not write its .stopped marker; resume "
			       "message %s", ag_resume(a, resume));
			return 1;
		}
		/* -3 (still in the composer) goes on to AG_EXIT: claude may be
		** gone already, and a cancel would end a held session; AG_EXIT
		** cancels safely if it never exits. */
		if ((st = ag_type(a->path, "/exit", 0)) != 0 && st != -3)
		{
			if (a->stop)
				stop_mark(a->path, NULL, 1);
			respawn_req(a->path, 'c', NULL, 0);
			ag_say(a, 1, "%s; hold cancelled%s%s", st == -2
			       ? "/exit did not land in the composer, Enter not "
			         "pressed" : "could not type /exit",
			       a->stop ? "; resume message " : "",
			       a->stop ? ag_resume(a, resume) : "");
			return 1;
		}
		a->step = AG_EXIT;
		a->deadline = now + step;
		a->ok_since = 0;
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

/* The agent step of `--restart --include-agents`: every session, or only
** `only` when non-NULL. Returns the number of agents that were attempted
** and failed. */
static int ag_run(struct agent *ag, int na, const char *pmsg,
                  const char *rmsg);

static int
restart_agents(int force, const char *only)
{
	struct slist sl = {0}, cur = {0};
	struct agent *ag;
	const char *pmsg = getenv("DCH_PAUSE_MSG"), *rmsg = getenv("DCH_RESUME_MSG");
	const char *self = getenv("DCH_SESSION");
	char selfb[256], onlyb[256], want[64] = "";
	int na = 0, k, fails, said = 0;

	if (!pmsg || !*pmsg)
		pmsg = ag_pause_msg;
	if (!rmsg || !*rmsg)
		rmsg = ag_resume_msg;
	if (self && *self)
		self = canon_name(self, selfb, sizeof selfb);
	if (only)
		only = canon_name(only, onlyb, sizeof onlyb);
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

		if (only && strcmp(sl.v[k], only) != 0)
			continue;
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
	if (cur.n && !na && !said && !only)
		printf("all claude agents already on claude %s\n", want);
	else
		for (k = 0; k < cur.n; k++)
			puts(cur.v[k]);
	slist_free(&cur);
	fflush(stdout);
	fails = ag_run(ag, na, pmsg, rmsg);
	for (k = 0; k < na; k++)
		cmd_info_free(&ag[k].ci);
	free(ag);
	return fails;
}

/* Pause every agent in ag[0..na), then wait on them all at once: one slow
** agent must not hold up the rest. Returns how many failed. */
static int
ag_run(struct agent *ag, int na, const char *pmsg, const char *rmsg)
{
	long tmo = env_ms("DCH_AGENT_TIMEOUT_MS", 600000);
	long settle = env_ms("DCH_AGENT_SETTLE_MS", 3000);
	int k, fails = 0, live;
	static const int sigs[] = { SIGINT, SIGTERM, SIGHUP };
	struct sigaction sa, old[3], oldpipe;

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

	for (k = 0; k < na; k++)
	{
		struct agent *a = &ag[k];
		int g = ag_sig ? CG_UNKNOWN : claude_gate(a->path);

		if (ag_sig)
		{
			a->res = "interrupted";
			a->code = "signal";
			ag_say(a, 1, "interrupted (signal %d) before its pause; "
			       "nothing typed", (int)ag_sig);
			fails++;
			continue;
		}
		if (g != CG_EMPTY)
		{
			a->res = g == CG_NOVT ? "skipped" : NULL;
			a->code = cg_code[g == CG_NOVT ? CG_UNKNOWN : g];
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
		stop_phase(a->name, "pausing", a->json);
		if ((g = ag_type(a->path, pmsg, 1)) != 0)
		{
			a->code = "not_typed";
			ag_say(a, 1, g == -2 ? "the pause message did not land in the "
			       "composer; Enter not pressed" : g == -3 ? "the pause "
			       "message stayed in the composer through every Enter"
			       : "could not type the pause message");
			fails++;
			continue;
		}
		if (a->stop)
			continue;
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
				ag[k].res = "interrupted";
				ag[k].code = "signal";
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
	return fails;
}

#else /* !HAVE_LIBGHOSTTY_VT */

static int
restart_agents(int force, const char *only)
{
	struct slist sl = {0};
	struct cmd_info ci;
	char path[1100], onlyb[256];
	int k, hit = 0;

	(void)force;
	if (only)
		only = canon_name(only, onlyb, sizeof onlyb);
	/* Only worth a word when some session runs claude. */
	list_sessions(&sl);
	for (k = 0; k < sl.n && !hit; k++)
	{
		if (only && strcmp(sl.v[k], only) != 0)
			continue;
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

/* ---- --say: one message to the Claude sessions (#045) --------------
**
** `--say <name>...|--all -- <text>` types text into each session's Claude
** Code under the --include-agents gate, at the moment of the call: never
** into a busy Claude, a dialog, a draft or a queue — those are skipped and
** the caller retries. Every session types in a child of its own, so --all
** costs the slowest session, not the sum (ag_type alone may take ~25 s). */

/* Outcomes; from SAY_NO_SESSION on they are failures. */
enum { SAY_OK, SAY_NOT_CLAUDE, SAY_SELF, SAY_BUSY, SAY_PROMPT, SAY_DRAFT,
       SAY_QUEUED, SAY_NO_SCREEN, SAY_NO_SESSION, SAY_NOT_SUBMITTED,
       SAY_NOT_TYPED, SAY_OLD_MASTER, SAY_N };
static const char *const say_code[] = {
	"", "not_claude", "self", "busy", "prompt", "draft", "queued",
	"no_screen", "no_session", "not_submitted", "not_typed", "old_master",
};
static const char *const say_why[] = {
	"", "no claude runs in the session", "the session you are in",
	"claude is busy", "a prompt or dialog is waiting for you",
	"the composer holds an unsent draft", "a queued message is pending",
	"the composer is not on screen", "no such session",
	"the message stayed in the composer through every Enter",
	"the message could not be typed",
	"its master predates --cmd (dch --restart it)",
};
/* A child's exit status carries its outcome past exit(1) and the like. */
#define SAY_EXIT 100

static void
say_print(const char *name, int c, int json)
{
	const char *res = c == SAY_OK ? "sent" : c < SAY_NO_SESSION ? "skipped"
	                                                           : "failed";
	size_t cap = strlen(name) * 6 + 3;
	char *q;

	if (!json)
		printf(c == SAY_OK ? "%s: %s\n" : "%s: %s — %s\n", name, res,
		       say_why[c]);
	else if ((q = malloc(cap)))
	{
		if (dch_json_quote(q, cap, name) == (size_t)-1)
			snprintf(q, cap, "\"\"");
		printf("{\"name\":%s,\"result\":\"%s\",\"reason\":\"%s\","
		       "\"detail\":\"%s\"}\n", q, c == SAY_OK ? "ok" : res,
		       say_code[c], say_why[c]);
		free(q);
	}
	fflush(stdout);
}

#ifdef HAVE_LIBGHOSTTY_VT
/* The gate and the typing, in the child. */
static int
say_type(const char *path, const char *text)
{
	switch (claude_gate(path))
	{
	case CG_EMPTY:
		break;
	case CG_DIALOG:
		return SAY_PROMPT;
	case CG_DRAFT:
		return SAY_DRAFT;
	case CG_QUEUED:
		return SAY_QUEUED;
	default:
		return SAY_NO_SCREEN;
	}
	switch (ag_type(path, text, 1))
	{
	case 0:
		return SAY_OK;
	case -3:
		return SAY_NOT_SUBMITTED;
	default:
		return SAY_NOT_TYPED;
	}
}
#endif

/* names NULL = every session. Exit status: 1 when any session failed. */
static int
do_say_verb(char **names, int nnames, char **words, int nwords, int json)
{
	struct slist sl = {0};
	const char *self = getenv("DCH_SESSION");
	char selfb[256], cb[256], path[1100], *text;
	pid_t *kids;
	size_t len = 1;
	int k, c, st, live = 0, bad = 0, ws;

	for (k = 0; k < nwords; k++)
		len += strlen(words[k]) + 1;
	if (!(text = malloc(len)))
		return 1;
	for (text[0] = '\0', k = 0; k < nwords; k++)
		strcat(strcat(text, k ? " " : ""), words[k]);
	if (!text[strspn(text, " \t\r\n")])
	{
		fprintf(stderr, "dch: --say needs text after --\n");
		free(text);
		return 1;
	}
	if (self && *self)
		self = canon_name(self, selfb, sizeof selfb);
	if (!names)
	{
		list_sessions(&sl);
		names = sl.v;
		nnames = sl.n;
	}
	kids = calloc((size_t)nnames + 1, sizeof *kids);
	for (k = 0; kids && k < nnames; k++)
	{
		struct cmd_info ci;
		struct crec r;

		session_sock_path(names[k], path, sizeof path);
		if (self && *self &&
		    strcmp(self, canon_name(names[k], cb, sizeof cb)) == 0)
			c = SAY_SELF;
		else if ((st = cmd_query(path, &ci)) == -4 && sl.v)
			continue;	/* --all: a stale socket, no session */
		else if (st == DCH_ST_UNSUP)
			c = SAY_OLD_MASTER;
		else if (st != 0)
			c = SAY_NO_SESSION;
		else
		{
			c = ci.pid <= 0 || claude_find(ci.pid, 1, &r) == 0
			    ? SAY_NOT_CLAUDE
			    : strcmp(r.status, "idle") != 0 ? SAY_BUSY : -1;
			cmd_info_free(&ci);
		}
#ifdef HAVE_LIBGHOSTTY_VT
		if (c < 0 && (kids[k] = fork()) == 0)
			_exit(SAY_EXIT + say_type(path, text));
		if (c < 0 && kids[k] > 0)
		{
			live++;
			continue;
		}
		if (c < 0)
			c = SAY_NOT_TYPED;
#else
		if (c < 0)
			c = SAY_NO_SCREEN;	/* dch-lite: no mirror to gate on */
#endif
		say_print(names[k], c, json);
		bad |= c >= SAY_NO_SESSION;
	}
	for (; live > 0; live--)
	{
		pid_t p = wait(&ws);

		if (p < 0)
			break;
		for (k = 0; k < nnames && kids[k] != p; k++)
			;
		if (k == nnames)
		{
			live++;		/* not one of ours */
			continue;
		}
		c = WIFEXITED(ws) && WEXITSTATUS(ws) >= SAY_EXIT &&
		    WEXITSTATUS(ws) < SAY_EXIT + SAY_N
		    ? WEXITSTATUS(ws) - SAY_EXIT : SAY_NOT_TYPED;
		say_print(names[k], c, json);
		bad |= c >= SAY_NO_SESSION;
	}
	bad |= !kids;
	free(kids);
	free(text);
	slist_free(&sl);
	return bad ? 1 : 0;
}

/* `--stop <name>...|--all [--json]` (names NULL = every session). Exit
** status 1 when any session failed or was interrupted. */
static int
do_stop_verb(char **names, int nnames, int json)
{
	struct slist sl = {0};
	const char *self = getenv("DCH_SESSION");
	char selfb[256], cb[256], path[1100];
	int k, bad = 0;
#ifdef HAVE_LIBGHOSTTY_VT
	const char *pmsg = getenv("DCH_PAUSE_MSG"), *rmsg = getenv("DCH_RESUME_MSG");
	struct agent *ag;
	int na = 0;
#endif

	if (self && *self)
		self = canon_name(self, selfb, sizeof selfb);
	if (!names)
	{
		list_sessions(&sl);
		names = sl.v;
		nnames = sl.n;
	}
#ifdef HAVE_LIBGHOSTTY_VT
	if (!(ag = calloc((size_t)nnames + 1, sizeof *ag)))
	{
		slist_free(&sl);
		return 1;
	}
#endif
	for (k = 0; k < nnames; k++)
	{
		struct cmd_info ci;
		struct crec r;
		const char *why;
		long mpid;
		int c = -1, st = -1;

		session_sock_path(names[k], path, sizeof path);
		if (self && *self &&
		    strcmp(self, canon_name(names[k], cb, sizeof cb)) == 0)
			c = SAY_SELF;
		else if ((st = cmd_query(path, &ci)) == -4 && sl.v)
			continue;	/* --all: a stale socket, no session */
		else if (st == DCH_ST_UNSUP)
			c = SAY_OLD_MASTER;
		else if (st != 0)
			c = SAY_NO_SESSION;
		else if (strcmp(ci.state, "running") != 0)
		{
			/* --stop arms its own hold: someone else's owns the exit */
			stop_print(names[k], "skipped", "respawn_pending",
			           "a respawn is pending", "", "", json);
			cmd_info_free(&ci);
			continue;
		}
		else if (proc_depth((long)getpid(), ci.pid) >= 0)
			c = SAY_SELF;
		if (c == SAY_SELF && st == 0)
			cmd_info_free(&ci);
		if (c >= 0)
		{
			stop_print(names[k], c < SAY_NO_SESSION ? "skipped" : "failed",
			           say_code[c], say_why[c], "", "", json);
			bad |= c >= SAY_NO_SESSION;
			continue;
		}
		mpid = proc_ppid(ci.pid);
		if (claude_find(ci.pid, 1, &r) == 0)
		{
			cmd_info_free(&ci);
			stop_phase(names[k], "ending", json);
			why = stop_end(names[k], path, mpid, NULL);
			stop_print(names[k], why ? "failed" : "ok",
			           why ? "end_failed" : "", why ? why : "", "", "",
			           json);
			bad |= why != NULL;
			continue;
		}
#ifdef HAVE_LIBGHOSTTY_VT
		if (claude_gate(path) != CG_NOVT)
		{
			struct agent *a = &ag[na++];

			snprintf(a->name, sizeof a->name, "%s", names[k]);
			snprintf(a->path, sizeof a->path, "%s", path);
			a->ci = ci;
			a->argv = ci.argv;
			a->argc = ci.argc;
			a->tok = claude_tok(a->argv, a->argc);
			a->cpid = r.pid;
			snprintf(a->ver, sizeof a->ver, "%s", r.ver);
			a->mpid = mpid;
			a->stop = 1;
			a->json = json;
			continue;
		}
#endif
		/* nothing is typed without the mirror to gate on */
		cmd_info_free(&ci);
		stop_print(names[k], "skipped", say_code[SAY_NO_SCREEN],
		           say_why[SAY_NO_SCREEN], "", "", json);
	}
#ifdef HAVE_LIBGHOSTTY_VT
	if (na)
		bad |= ag_run(ag, na, pmsg && *pmsg ? pmsg : stop_pause_msg,
		              rmsg && *rmsg ? rmsg : stop_resume_msg) > 0;
	for (k = 0; k < na; k++)
	{
		cmd_info_free(&ag[k].ci);
		free(ag[k].ag.launch);
	}
	free(ag);
#endif
	slist_free(&sl);
	return bad ? 1 : 0;
}

/* ---- session history: --history, --revive (#042) -------------------- */

/* One record from dch_history_dir() (written by master.c's rec_*). */
struct hrec
{
	char file[300];		/* record basename without .json */
	char *body;		/* as stored, one line */
	char name[512], alias[600], cwd[PATH_MAX], end[32];
	char sid[64], aname[256], acwd[PATH_MAX];
	long started, updated, ended;
	char **argv, **launch;	/* malloc'd blocks */
	int argc, launchc, live;
	/* --revive */
	char as[512];
	int mode;		/* RV_* */
	long long deadline;
};

enum { RV_SKIP, RV_PLAIN, RV_ARGV, RV_SHELL };

static void
hrec_free(struct hrec *r, int n)
{
	int k;

	for (k = 0; k < n; k++)
	{
		free(r[k].body);
		free(r[k].argv);
		free(r[k].launch);
	}
	free(r);
}

/* Does a master answer on `name`? */
static int
name_live(const char *name)
{
	char sp[1100];
	int s;

	session_sock_path(name, sp, sizeof sp);
	if ((s = control_connect_path(sp)) < 0)
		return 0;
	close(s);
	return 1;
}

static int
hrec_load(int dfd, const char *fn, struct hrec *r)
{
	struct stat st;
	size_t n = strlen(fn);
	ssize_t got;
	char sd[sizeof sock_dir];
	int fd;

	memset(r, 0, sizeof *r);
	if (n < 6 || n - 5 >= sizeof r->file || strcmp(fn + n - 5, ".json"))
		return -1;
	snprintf(r->file, sizeof r->file, "%.*s", (int)(n - 5), fn);
	fd = openat(dfd, fn, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
	if (fd < 0)
		return -1;
	/* ours and nobody else's: a record is a command we would run */
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
	    st.st_uid != getuid() || (st.st_mode & 022) ||
	    st.st_size <= 0 || st.st_size > 1024 * 1024 ||
	    !(r->body = malloc((size_t)st.st_size + 1)))
	{
		close(fd);
		return -1;
	}
	got = read(fd, r->body, (size_t)st.st_size);
	close(fd);
	r->body[got > 0 ? got : 0] = '\0';
	chomp(r->body);
	/* the slug folds '/' and '-' together: only this socket dir's own */
	if (!dch_json_is_object(r->body) ||
	    dch_json_string(r->body, "sock_dir", sd, sizeof sd) < 0 ||
	    strcmp(sd, sock_dir) != 0 ||
	    dch_json_string(r->body, "name", r->name, sizeof r->name) < 0 ||
	    !r->name[0] ||
	    dch_json_string(r->body, "cwd", r->cwd, sizeof r->cwd) < 0 ||
	    dch_json_string(r->body, "end", r->end, sizeof r->end) < 0 ||
	    !(r->argv = dch_json_strv(r->body, NULL, "argv", &r->argc)))
	{
		free(r->body);
		r->body = NULL;
		return -1;
	}
	(void)dch_json_string(r->body, "alias", r->alias, sizeof r->alias);
	(void)dch_json_long(r->body, "started", &r->started);
	(void)dch_json_long(r->body, "updated", &r->updated);
	(void)dch_json_long(r->body, "ended", &r->ended);
	if (dch_json_nested_string(r->body, "agent", "session_id", r->sid,
	                           sizeof r->sid) == 0 && r->sid[0])
	{
		(void)dch_json_nested_string(r->body, "agent", "name", r->aname,
		                             sizeof r->aname);
		(void)dch_json_nested_string(r->body, "agent", "cwd", r->acwd,
		                             sizeof r->acwd);
		r->launch = dch_json_strv(r->body, "agent", "launch",
		                          &r->launchc);
	}
	else
		r->sid[0] = '\0';
	/* A record with an end is over even if a new session took its name. */
	r->live = !r->end[0] && name_live(r->name);
	if (!r->end[0] && !r->live)
		snprintf(r->end, sizeof r->end, "vanished");
	return 0;
}

static long
hrec_when(const struct hrec *r)
{
	return r->ended ? r->ended : r->updated;
}

static int
hrec_cmp(const void *a, const void *b)
{
	long x = hrec_when(a), y = hrec_when(b);

	return x < y ? 1 : x > y ? -1 : 0;
}

/* Every readable record, newest end first. */
static int
history_load(struct hrec **out)
{
	char dir[1024];
	struct dirent *de;
	struct hrec *v = NULL, *nv;
	int n = 0, cap = 0;
	DIR *d;

	*out = NULL;
	if (dch_history_dir(dir, sizeof dir, sock_dir, 0) < 0 ||
	    !(d = opendir(dir)))
		return 0;
	while ((de = readdir(d)))
	{
		if (n == cap)
		{
			cap = cap ? cap * 2 : 16;
			if (!(nv = realloc(v, (size_t)cap * sizeof *v)))
				break;
			v = nv;
		}
		if (hrec_load(dirfd(d), de->d_name, &v[n]) == 0)
			n++;
	}
	closedir(d);
	if (n)
		qsort(v, (size_t)n, sizeof *v, hrec_cmp);
	*out = v;
	return n;
}

static void
put_argv(char **v)
{
	int k;

	for (k = 0; v && v[k]; k++)
		printf("%s%s", k ? " " : "", v[k]);
}

static int
do_history_verb(int json)
{
	struct hrec *r;
	int n = history_load(&r), k, first = 1;

	if (json)
		putchar('[');
	for (k = 0; k < n; k++)
	{
		char when[32] = "-";
		time_t t = (time_t)hrec_when(&r[k]);
		const char *v;

		if (r[k].live)
			continue;
		if (json)
		{
			/* As stored, one line each; a dead end "" reads vanished. */
			v = strstr(r[k].body, "\"end\":\"\"");
			if (v)
				printf("%s%.*s\"end\":\"vanished\"%s", first ? "" : ",",
				       (int)(v - r[k].body), r[k].body, v + 8);
			else
				printf("%s%s", first ? "" : ",", r[k].body);
			first = 0;
			continue;
		}
		if (t)
			strftime(when, sizeof when, "%Y-%m-%d %H:%M", localtime(&t));
		printf("%s%s%s%s  %s  %s  %s  $ ", r[k].name,
		       r[k].alias[0] ? " (" : "", r[k].alias,
		       r[k].alias[0] ? ")" : "", when, r[k].end, r[k].cwd);
		put_argv(r[k].argv);
		if (r[k].sid[0])
			printf("  [claude %s%s%s]", r[k].aname,
			       r[k].aname[0] ? " " : "", r[k].sid);
		putchar('\n');
	}
	if (json)
		puts("]");
	hrec_free(r, n);
	return 0;
}

/* What --revive --all takes: a session that died (not exit 0, not dch -k
** or --stop),
** recently, after a real run. A test session that ends on a signal on
** purpose lives seconds; one from last week is history, not work in
** flight. Both stay in --history and revive by name. */
#define RV_ALL_MAX_AGE	(3L * 86400L)
#define RV_ALL_MIN_LIFE	(5L * 60L)

static int
rv_all_takes(const struct hrec *h)
{
	long seen = hrec_when(h);

	return !h->live && strcmp(h->end, "killed") != 0 &&
	       strcmp(h->end, "stopped") != 0 &&
	       strcmp(h->end, "exit 0") != 0 &&
	       seen >= (long)time(NULL) - RV_ALL_MAX_AGE &&
	       seen - h->started >= RV_ALL_MIN_LIFE;
}

/* v[0..n) as one shell command line, each word quoted when it needs it. */
static char *
shell_line(const char *cd, char **v, int n)
{
	size_t cap = cd ? strlen(cd) * 4 + 16 : 1;
	char *o, *p;
	int k;

	for (k = 0; k < n; k++)
		cap += strlen(v[k]) * 4 + 3;
	if (!(o = p = malloc(cap)))
		return NULL;
	for (k = cd ? -1 : 0; k < n; k++)
	{
		const char *w = k < 0 ? cd : v[k], *c;

		if (p > o)
			*p++ = ' ';
		if (k < 0)
			p += sprintf(p, "cd ");
		/* a leading '=' is zsh's =cmd expansion: quote it */
		if (*w && *w != '=' && strspn(w, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
		                 "0123456789_./:=@%+,-") == strlen(w))
			p += sprintf(p, "%s", w);
		else
		{
			*p++ = '\'';
			for (c = w; *c; c++)
				if (*c == '\'')
					p += sprintf(p, "'\\''");
				else
					*p++ = *c;
			*p++ = '\'';
		}
		if (k < 0)
			p += sprintf(p, " &&");
	}
	*p = '\0';
	return o;
}

/* The calling Claude Code session's identity, never handed to a revived
** one: --revive is often run from inside another agent. */
static const char *const rv_strip[] = {
	"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
	"CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
	"CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
	"CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID", "CLAUDE_EFFORT",
};

static int do_spawn_verb(char *exe, const char *name, int cols, int rows,
                         char **inner_argv, int inner_argc, int quiet);

/* Spawn one record's session in its cwd, as its argv — with --resume <id>
** when claude is its command (RV_ARGV); a shell that had claude typed into
** it comes back as the shell (RV_SHELL, the command is typed later). 0, or
** -1 with the reason printed. */
static int
revive_spawn(char *exe, struct hrec *r, int cols, int rows)
{
	char **nv = NULL, **v = r->argv;
	int tok = claude_tok(r->argv, r->argc), nc = r->argc, rc;

	if (chdir(r->cwd) < 0)
	{
		printf("%s: NOT revived — its directory %s is gone (%s)\n",
		       r->name, r->cwd, strerror(errno));
		return -1;
	}
	r->mode = RV_PLAIN;
	if (r->sid[0] && tok < r->argc)
	{
		if (!(nv = resume_argv(r->argv, r->argc, tok, r->sid, &nc)))
			return -1;
		v = nv;
		r->mode = RV_ARGV;
	}
	else if (r->sid[0] && r->launch)
		r->mode = RV_SHELL;
	{
		/* do_spawn_verb wants a NULL-terminated argv */
		char **av = calloc((size_t)nc + 1, sizeof *av);

		rc = -1;
		if (av)
		{
			memcpy(av, v, (size_t)nc * sizeof *av);
			rc = do_spawn_verb(exe, r->as, cols, rows, av, nc, 1);
			free(av);
		}
	}
	free(nv);
	if (rc != 0)
	{
		printf("%s: NOT revived — the session did not start\n", r->name);
		return -1;
	}
	if (r->alias[0])
		write_alias(r->as, r->alias);
	return 0;
}

/* Type the agent's launch command, with --resume <id>, into the revived
** shell once it has printed something (its prompt): no mirror needed. */
static int
revive_type_launch(struct hrec *r)
{
	char **nv, *line, *cmd[1];
	int tok = claude_tok(r->launch, r->launchc), nc, rc;

	if (tok == r->launchc)
		tok = r->launchc - 1;	/* no claude token: --resume goes last */
	while (activity_epoch(r->as) == 0 && wall_ms() < r->deadline)
		poll(NULL, 0, 100);
	poll(NULL, 0, 300);
	if (!(nv = resume_argv(r->launch, r->launchc, tok, r->sid, &nc)))
		return -1;
	line = shell_line(r->acwd[0] && strcmp(r->acwd, r->cwd) ? r->acwd : NULL,
	                  nv, nc);
	free(nv);
	if (!line)
		return -1;
	cmd[0] = line;
	rc = do_send_verb(r->as, cmd, 1, 1);
	free(line);
	return rc;
}

#ifdef HAVE_LIBGHOSTTY_VT
/* Wait for the resumed claude's empty composer, then type the revive
** message. Returns NULL when it was sent, else why not. */
static const char *
revive_message(struct hrec *r, const char *msg)
{
	static char why[160];
	char path[1100];
	struct cmd_info ci;
	struct crec c;
	int g = CG_UNKNOWN, t;

	session_sock_path(r->as, path, sizeof path);
	for (;;)
	{
		int ok = cmd_query(path, &ci) == 0;

		if (ok && ci.pid > 0 && claude_find(ci.pid, 1, &c) > 0 &&
		    strcmp(c.status, "idle") == 0 &&
		    (g = claude_gate(path)) == CG_EMPTY)
		{
			cmd_info_free(&ci);
			break;
		}
		if (ok)
			cmd_info_free(&ci);
		if (g == CG_NOVT)
			return "the session has no terminal mirror";
		if (wall_ms() >= r->deadline)
		{
			snprintf(why, sizeof why, "no empty composer within %lds (%s)",
			         env_ms("DCH_AGENT_STEP_MS", 60000) / 1000, cg_why[g]);
			return why;
		}
		poll(NULL, 0, AG_POLL_MS);
	}
	t = ag_type(path, msg, 1);
	return t == 0 ? NULL : t == -2 ? "it did not land in the composer"
	       : t == -3 ? "it stayed in the composer through every Enter"
	       : "it could not be typed";
}
#endif

/* `--revive <name> [--as <new>]` / `--revive --all`. */
static int
do_revive_verb(char *exe, const char *name, const char *as, int all,
               int cols, int rows)
{
	struct hrec *r;
	int n = history_load(&r), k, picked = 0, bad = 0;
	long step = env_ms("DCH_AGENT_STEP_MS", 60000);
	char dir[1024], cb[256];

	for (k = 0; k < (int)(sizeof rv_strip / sizeof rv_strip[0]); k++)
		unsetenv(rv_strip[k]);
	(void)dch_history_dir(dir, sizeof dir, sock_dir, 0);
	for (k = 0; k < n; k++)
	{
		struct hrec *h = &r[k];
		int i;

		h->mode = RV_SKIP;
		if (all ? !rv_all_takes(h)
		        : strcmp(h->name, name) != 0 &&
		          strcmp(h->file, canon_name(name, cb, sizeof cb)) != 0)
			continue;
		if (!all && picked)
			continue;	/* two records claim it: the newest wins */
		picked++;
		snprintf(h->as, sizeof h->as, "%s", as ? as : h->name);
		if (!all && name_live(h->as))
		{
			fprintf(stderr, "dch: session %s is live; revive it under "
			        "another name with --revive %s --as <new>\n", h->as,
			        name);
			bad++;
			continue;
		}
		/* --all: the next free <name>-N, one that no record claims
		** either — its master would overwrite that record. */
		if (all && name_live(h->as))
			for (i = 2; i <= 999; i++)
			{
				char rp[1400];

				snprintf(h->as, sizeof h->as, "%s-%d", h->name, i);
				snprintf(rp, sizeof rp, "%s/%s.json", dir,
				         canon_name(h->as, cb, sizeof cb));
				if (!name_live(h->as) && access(rp, F_OK) != 0)
					break;
			}
		if (revive_spawn(exe, h, cols, rows) < 0)
		{
			h->mode = RV_SKIP;
			bad++;
			continue;
		}
		h->deadline = wall_ms() + step;
		/* revived under a new name: the old record is now history the
		** new one carries; left alone, --all would revive it again */
		if (strcmp(h->as, h->name) != 0 && !h->live)
		{
			char rp[1400];

			snprintf(rp, sizeof rp, "%s/%s.json", dir, h->file);
			unlink(rp);
		}
		if (h->mode == RV_PLAIN)
		{
			printf("%s: revived as %s in %s", h->name, h->as, h->cwd);
			if (h->sid[0])
				printf("; claude %s NOT resumed (its launch command is "
				       "unknown): claude --resume %s", h->aname, h->sid);
			putchar('\n');
			fflush(stdout);
			bad += h->sid[0] != 0;
		}
	}
	if (!picked)
	{
		if (all)
			puts("nothing to revive (sessions that ended over 3 days "
			     "ago or ran under 5 minutes are in dch --history; "
			     "revive one by name)");
		else
			fprintf(stderr, "dch: no history for session %s\n", name);
		hrec_free(r, n);
		return all ? 0 : 1;
	}
	/* Then the agents: every session is already starting, so waiting on
	** them one by one costs about the slowest one. Shells get their claude
	** command first; its clock starts when it is typed. */
	for (k = 0; k < n; k++)
		if (r[k].mode == RV_SHELL)
		{
			if (revive_type_launch(&r[k]) == 0)
			{
				r[k].deadline = wall_ms() + step;
				continue;
			}
			printf("%s: revived as %s in %s, but claude %s could not be "
			       "started; resume by hand: claude --resume %s\n",
			       r[k].name, r[k].as, r[k].cwd, r[k].aname, r[k].sid);
			r[k].mode = RV_PLAIN;
			bad++;
		}
	for (k = 0; k < n; k++)
	{
		struct hrec *h = &r[k];
		const char *why = NULL, *msg = getenv("DCH_REVIVE_MSG");
		char text[1200], when[32] = "an unknown time";
		time_t t = (time_t)hrec_when(h);

		if (h->mode != RV_ARGV && h->mode != RV_SHELL)
			continue;
		if (t)
			strftime(when, sizeof when, "%Y-%m-%d %H:%M", localtime(&t));
		if (strcmp(h->end, "stopped") == 0)
			snprintf(text, sizeof text, "resume all work. This session "
			         "was stopped by dch --stop (%s) and dch revived it "
			         "with claude --resume. Re-arm the monitors, "
			         "background shells, subagents, loops, wakeups and "
			         "crons you stopped before the pause, then carry on "
			         "where you left off.", when);
		else
			snprintf(text, sizeof text, "resume all work. This session "
			         "ended (%s, last seen %s) and dch revived it with "
			         "claude --resume. Re-read your ledger and task list, "
			         "re-arm the monitors, background shells, loops, "
			         "wakeups and crons that died with it, re-dispatch "
			         "any unfinished workers, then carry on where you "
			         "left off.", h->end, when);
#ifdef HAVE_LIBGHOSTTY_VT
		why = revive_message(h, msg && *msg ? msg : text);
		bad += why != NULL;
#else
		(void)msg;
		why = "dch-lite has no terminal mirror to see the composer";
#endif
		printf("%s: revived as %s in %s, claude %s%sresumed %s; resume "
		       "message %s%s%s\n", h->name, h->as, h->cwd, h->aname,
		       h->aname[0] ? " " : "", h->sid, why ? "NOT sent (" : "sent",
		       why ? why : "", why ? ")" : "");
		fflush(stdout);
	}
	hrec_free(r, n);
	return bad ? 1 : 0;
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
	       A_REPORT, A_META, A_RESTART, A_AGENT_LIST, A_AGENT_SEND,
	       A_RESPAWN, A_CMD, A_HISTORY, A_REVIVE, A_SAY, A_STOP }
	    action = A_ATTACH;
	char alias_arg[600] = "";
	int opt_ansi = 0, opt_recent = -1, opt_timeout = 10000, opt_cursor = 0;
	int opt_all = 0, opt_cancel = 0, opt_agents = 0;
	int opt_agent_json = 0, opt_hist_json = 0, opt_say_json = 0;
	int say_at = 0, say_n = 0;
	char opt_as[512] = "";
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

	/* Resolve exe for the master re-exec. It must end up absolute: --restart
	** sends it to the master as the binary to land on, the master accepts
	** only an absolute path, and its own fallback re-execs with execv(),
	** which does no PATH search — a bare "dch" there resolves against the
	** master's cwd and fails (or runs whatever ./dch sits there).
	** A path (has a '/') is realpath()ed. A bare name like "dch" came from a
	** PATH lookup by the shell, so repeat that lookup the way execvp() does.
	** Never realpath() the bare name itself: that resolves RELATIVE TO CWD
	** and silently picks up an unrelated "./dch" (e.g. a `dch/` source dir),
	** making the master exec a directory ("failed to spawn"). An absolute
	** PATH hit is kept as is, so /opt/homebrew/bin/dch stays the symlink and
	** follows upgrades; a relative hit (empty or relative PATH entry) is
	** realpath()ed. No hit keeps the bare name for execvp() to search. */
	char *exe = argv[0];
	if (strchr(argv[0], '/'))
	{
		char *rp = realpath(argv[0], NULL);
		if (rp)
			exe = rp;
	}
	else
	{
		char hit[PATH_MAX];
		const char *path = getenv("PATH");

		if (path && path_lookup(argv[0], path, ".", hit, sizeof hit) == 0)
			exe = hit[0] == '/' ? strdup(hit) : realpath(hit, NULL);
		if (!exe)
			exe = argv[0];
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
		         strcmp(a, "--meta") == 0 ||
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
			       : strcmp(a, "--meta") == 0  ? A_META
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
		else if (strcmp(a, "--say") == 0)
		{
			/* <name>... or --all, maybe --json, then a required
			** `--` and the text; the names are packed into
			** argv[say_at...] in place. */
			action = A_SAY;
			for (say_at = ++i; i < argc && strcmp(argv[i], "--") != 0; i++)
				if (strcmp(argv[i], "--all") == 0)
					opt_all = 1;
				else if (strcmp(argv[i], "--json") == 0)
					opt_say_json = 1;
				else if (argv[i][0] == '-' || argv[i][0] == '\0')
				{
					fprintf(stderr, "dch: --say: unexpected "
					        "%s\n", argv[i][0] ? argv[i] : "''");
					return 1;
				}
				else
					argv[say_at + say_n++] = argv[i];
			if (i == argc || opt_all == !!say_n)
			{
				fprintf(stderr, "dch: usage: dch --say <name>... "
				        "| --all [--json] -- <text...>\n");
				return 1;
			}
			i++;
			break;
		}
		else if (strcmp(a, "--stop") == 0)
		{
			/* <name>... or --all, maybe --json: the rest of the
			** line, names packed as --say packs them */
			action = A_STOP;
			for (say_at = ++i; i < argc; i++)
				if (strcmp(argv[i], "--all") == 0)
					opt_all = 1;
				else if (strcmp(argv[i], "--json") == 0)
					opt_say_json = 1;
				else if (argv[i][0] == '-' || argv[i][0] == '\0')
				{
					fprintf(stderr, "dch: --stop: unexpected "
					        "%s\n", argv[i][0] ? argv[i] : "''");
					return 1;
				}
				else
					argv[say_at + say_n++] = argv[i];
			if (opt_all == !!say_n)
			{
				fprintf(stderr, "dch: usage: dch --stop <name>... "
				        "| --all [--json]\n");
				return 1;
			}
			break;
		}
		else if (strcmp(a, "--history") == 0)
		{
			action = A_HISTORY;
			i++;
			if (i < argc && strcmp(argv[i], "--json") == 0)
			{
				opt_hist_json = 1;
				i++;
			}
		}
		else if (strcmp(a, "--revive") == 0)
		{
			/* <name> or --all, like --restart */
			action = A_REVIVE;
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
				fprintf(stderr, "dch: --revive needs a session "
				        "name or --all\n");
				return 1;
			}
		}
		else if (strcmp(a, "--as") == 0)
		{
			i++;
			if (i >= argc || argv[i][0] == '\0' || argv[i][0] == '-')
			{
				fprintf(stderr, "dch: --as needs a session name\n");
				return 1;
			}
			snprintf(opt_as, sizeof(opt_as), "%s", argv[i]);
			i++;
		}
		else if (strcmp(a, "--cancel") == 0)
		{
			opt_cancel = 1;
			i++;
		}
		else if (strcmp(a, "--include-agents") == 0)
		{
			opt_agents = 1;
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
	if (opt_as[0] && (action != A_REVIVE || opt_all))
	{
		fprintf(stderr, "dch: --as only works with --revive <name>\n");
		return 1;
	}
	if (opt_agents && action != A_RESTART)
	{
		fprintf(stderr, "dch: --include-agents only works with "
		        "--restart\n");
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
			if (rc != 0)
				return 1;
			fflush(stdout);
			return opt_agents && restart_agents(force, session_name)
			       ? 1 : 0;
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
		if (opt_agents && restart_agents(force, NULL))
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
			long mep;
			const char *meta = session_meta(sl.v[k], &mep);
			printf("\",\"activity_epoch\":%ld,\"state\":\"%s\""
			       ",\"version\":\"%s\",\"agents\":%d"
			       ",\"meta\":%s,\"meta_epoch\":%ld}",
			       activity_epoch(sl.v[k]),
			       session_state(sl.v[k]),
			       version_or_empty(sl.v[k]),
			       detect_agents(sl.v[k]),
			       meta ? meta : "null", mep);
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
	case A_META:
	{
		/* Free-form JSON object a program in the session publishes about
		** itself (tasks, open PRs, ...); --ls-json passes it through. */
		char sp[1100], mp[1300], tp[1320];
		struct stat st;
		const char *js = inner_argc == 1 ? inner_argv[0] : "";
		size_t n;
		while (*js == ' ')
			js++;
		n = strlen(js);
		while (n && js[n - 1] == ' ')
			n--;
		if (strcmp(js, "clear") != 0 &&
		    (!meta_bytes_ok(js, n) || !dch_json_is_object(js)))
		{
			fprintf(stderr, "dch: --meta needs one single-line JSON "
			        "object (at most %d bytes, 1024 tokens), or "
			        "`clear`\n", DCH_META_MAX);
			return 1;
		}
		session_sock_path(session_name, sp, sizeof(sp));
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode) ||
		    meta_file_path(session_name, mp, sizeof(mp)) != 0)
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		if (strcmp(js, "clear") == 0)
		{
			unlink(mp);
			return 0;
		}
		/* Unchanged content is a no-op, so meta_epoch (the mtime) stays
		** the time the content last changed and writers may re-push
		** freely. */
		{
			long ep;
			const char *cur = session_meta(session_name, &ep);
			if (cur && strlen(cur) == n && memcmp(cur, js, n) == 0)
				return 0;
		}
		/* Atomic replace, as for --report: readers never see a torn
		** object; last writer wins. */
		snprintf(tp, sizeof(tp), "%s.%d.tmp", mp, (int)getpid());
		int fd = open(tp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
		int wrote = fd >= 0 && write(fd, js, n) == (ssize_t)n;
		if (fd >= 0 && close(fd) != 0)
			wrote = 0;
		if (!wrote || rename(tp, mp) != 0)
		{
			if (fd >= 0)
				unlink(tp);
			fprintf(stderr, "dch: cannot write meta for %s\n",
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
	case A_HISTORY:
		return do_history_verb(opt_hist_json);
	case A_SAY:
		return do_say_verb(opt_all ? NULL : argv + say_at, say_n,
		                   inner_argv, inner_argc, opt_say_json);
	case A_STOP:
		return do_stop_verb(opt_all ? NULL : argv + say_at, say_n,
		                    opt_say_json);
	case A_REVIVE:
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
		if (action == A_REVIVE)
			return do_revive_verb(exe, session_name,
			                      opt_as[0] ? opt_as : NULL, opt_all,
			                      opt_cols, opt_rows);
		return do_spawn_verb(exe, session_name, opt_cols, opt_rows,
		                     inner_argv, inner_argc, 0);
	}
	case A_ATTACH:
		return attach_with_switch(exe, forced_name, force,
		                          inner_argv, inner_argc);
	}
	return 0;
}
