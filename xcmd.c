/*
 * xcmd.c - X-ray's command source (#321): run the user's `tasks_command` or
 * `memory_command`, read its stdout as JSON lines, hand each valid row on as
 * the contract's row line while the rest is still arriving. See xcmd.h.
 *
 * The run follows xray.c's git resolver (res_run): stdin /dev/null, its own
 * process group, signals reset, no other fd of ours, a group kill when the
 * querying process is ended, the leader reaped only after the group kill.
 * It forks rather than posix_spawn()s: the cwd needs addchdir, which is
 * _np on glibc and deprecated on macOS 26.
 */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#include "bridge.h"
#include "xcmd.h"

extern char **environ;

/* ---- the argv split ----------------------------------------------------- */

int
xcmd_argv(const char *s, const char *home, char *buf, size_t cap, char **argv,
          int max)
{
	size_t o = 0, hl;
	int argc = 0;

	if (!cap || max < 2)
		return -1;
	for (;;)
	{
		while (*s == ' ' || *s == '\t')
			s++;
		if (!*s)
			break;
		if (argc + 1 >= max)
			return -1;
		argv[argc++] = buf + o;
		if (s[0] == '~' && s[1] == '/')
		{
			hl = home ? strlen(home) : 0;
			if (!hl || hl >= cap - o)
				return -1;
			memcpy(buf + o, home, hl);
			o += hl;
			s++;	/* the '/' is copied as a plain byte */
		}
		while (*s && *s != ' ' && *s != '\t')
		{
			char q = *s;

			if (q == '\'' || q == '"')
			{
				for (s++; *s && *s != q; s++)
				{
					if (o + 1 >= cap)
						return -1;
					buf[o++] = *s;
				}
				if (!*s++)
					return -1;	/* an open quote */
				continue;
			}
			if (o + 1 >= cap)
				return -1;
			buf[o++] = *s++;
		}
		buf[o++] = '\0';
	}
	argv[argc] = NULL;
	return argc ? argc : -1;
}

/* ---- the line validator (pure) ------------------------------------------ */

struct xc_out
{
	char *o;
	size_t n, cap;
	int bad;
};

static void
put(struct xc_out *w, const char *s)
{
	size_t l = strlen(s);

	if (w->bad || l >= w->cap - w->n)
	{
		w->bad = 1;
		return;
	}
	memcpy(w->o + w->n, s, l + 1);
	w->n += l;
}

/* A JSON string, or null for NULL */
static void
putq(struct xc_out *w, const char *s)
{
	size_t l;

	if (!s)
	{
		put(w, "null");
		return;
	}
	if (w->bad || (l = dch_json_quote(w->o + w->n, w->cap - w->n, s)) ==
	              SIZE_MAX)
	{
		w->bad = 1;
		return;
	}
	w->n += l;
}

/* `,"key":` */
static void
key(struct xc_out *w, const char *k)
{
	/* n is 0 when the first put did not fit: never o[-1] */
	put(w, w->n && w->o[w->n - 1] == '{' ? "\"" : ",\"");
	put(w, k);
	put(w, "\":");
}

/* Token t as text into tmp[XCMD_LINE_MAX + 1]: a string decoded, a number
** (num) as written. NULL for anything else, a duplicated key (-2) too. */
static const char *
text(const struct json_parser *p, int t, int num, char *tmp)
{
	size_t l;

	if (t < 0)
		return NULL;
	if (p->tok[t].kind == JSON_STRING)
		return dch_json_decode(p, t, tmp, XCMD_LINE_MAX + 1) == 0 ? tmp
		                                                          : NULL;
	if (!num || p->tok[t].kind != JSON_PRIMITIVE)
		return NULL;
	l = (size_t)(p->tok[t].end - p->tok[t].start);
	if (!l || l > XCMD_LINE_MAX ||
	    !(p->text[p->tok[t].start] == '-' ||
	      (p->text[p->tok[t].start] >= '0' && p->text[p->tok[t].start] <= '9')))
		return NULL;
	memcpy(tmp, p->text + p->tok[t].start, l);
	tmp[l] = '\0';
	return tmp;
}

/* Token t cut like a title (xray.c's rule: its first non-blank line, blanks
** collapsed, 120 scalars) into out; NULL when absent, not text or blank. */
static const char *
cut(const struct json_parser *p, int t, int num, char *tmp,
    char out[XR_TITLE_MAX * 4 + 1])
{
	const char *s = text(p, t, num, tmp);

	if (!s)
		return NULL;
	xr_title(s, strlen(s), out, XR_TITLE_MAX * 4 + 1);
	return *out ? out : NULL;
}

/* A url or path: kept whole up to XCMD_URL_MAX bytes, no control bytes
** (spaces too when !spaces), else NULL. */
static const char *
link_text(const struct json_parser *p, int t, int spaces, char *tmp)
{
	const char *s = text(p, t, 0, tmp);
	size_t i;

	if (!s || !*s || strlen(s) > XCMD_URL_MAX)
		return NULL;
	for (i = 0; s[i]; i++)
		if ((unsigned char)s[i] < 32 || s[i] == 0x7f ||
		    (!spaces && s[i] == ' '))
			return NULL;
	return s;
}

/* The task status words (the contract's mapping) and the four the view
** groups by; matched without case, ' ' and '-' as '_' */
static const char *const xc_status[][2] = {
	{"pending", "pending"}, {"active", "active"}, {"done", "done"},
	{"dropped", "dropped"}, {"in_progress", "active"},
	{"completed", "done"}, {"deleted", "dropped"},
};

static const char *
status_of(const char *w)
{
	char b[16];
	size_t i, k;

	for (i = 0; w[i] && i < sizeof b - 1; i++)
	{
		char c = w[i];

		b[i] = c == ' ' || c == '-' ? '_' : c >= 'A' && c <= 'Z'
		       ? (char)(c - 'A' + 'a') : c;
	}
	if (w[i])
		return NULL;
	b[i] = '\0';
	for (k = 0; k < sizeof xc_status / sizeof *xc_status; k++)
		if (!strcmp(b, xc_status[k][0]))
			return xc_status[k][1];
	return NULL;
}

static void
row_task(struct xc_out *w, const struct json_parser *p, int r, char *tmp,
         const char *id, const char *title)
{
	char b[XR_TITLE_MAX * 4 + 1], lb[XR_TITLE_MAX * 4 + 1];
	const char *st, *word, *label;
	int t, i;

	word = cut(p, dch_json_field(p, r, "status"), 0, tmp, b);
	st = word ? status_of(word) : NULL;
	label = cut(p, dch_json_field(p, r, "label"), 0, tmp, lb);
	put(w, "{\"task\":{");
	key(w, "id");
	putq(w, id);
	key(w, "title");
	putq(w, title);
	key(w, "status");
	putq(w, st ? st : "pending");
	key(w, "label");
	putq(w, label ? label : word);
	key(w, "active");
	putq(w, cut(p, dch_json_field(p, r, "active"), 0, tmp, b));
	key(w, "note");
	putq(w, cut(p, dch_json_field(p, r, "note"), 0, tmp, b));
	key(w, "created");
	putq(w, cut(p, dch_json_field(p, r, "created"), 0, tmp, b));
	key(w, "updated");
	putq(w, cut(p, dch_json_field(p, r, "updated"), 0, tmp, b));
	key(w, "chapter");
	put(w, "null");
	key(w, "created_chapter");
	put(w, "null");
	key(w, "blocked_by");
	put(w, "[");
	if ((t = dch_json_field(p, r, "blocked_by")) >= 0 &&
	    p->tok[t].kind == JSON_ARRAY)
		for (i = t + 1; i < p->tok[t].next; i = p->tok[i].next)
			if (cut(p, i, 1, tmp, b))
			{
				if (w->n && w->o[w->n - 1] != '[')
					put(w, ",");
				putq(w, b);
			}
	put(w, "]");
	key(w, "url");
	putq(w, link_text(p, dch_json_field(p, r, "url"), 0, tmp));
	key(w, "group");
	putq(w, cut(p, dch_json_field(p, r, "group"), 0, tmp, b));
	put(w, "}}");
}

static void
row_memory(struct xc_out *w, const struct json_parser *p, int r, char *tmp,
           const char *id, const char *title)
{
	char b[XR_TITLE_MAX * 4 + 1];
	const char *s;
	size_t l;
	int bodycut = 0;

	put(w, "{\"memory\":{");
	key(w, "id");
	putq(w, id);
	key(w, "title");
	putq(w, title);
	key(w, "note");
	putq(w, cut(p, dch_json_field(p, r, "note"), 0, tmp, b));
	key(w, "kind");
	putq(w, cut(p, dch_json_field(p, r, "kind"), 0, tmp, b));
	key(w, "scope");
	putq(w, cut(p, dch_json_field(p, r, "scope"), 0, tmp, b));
	key(w, "updated");
	putq(w, cut(p, dch_json_field(p, r, "updated"), 0, tmp, b));
	key(w, "path");
	putq(w, link_text(p, dch_json_field(p, r, "path"), 1, tmp));
	key(w, "session");
	s = cut(p, dch_json_field(p, r, "session"), 0, tmp, b);
	putq(w, s && (!strcmp(s, "wrote") || !strcmp(s, "loaded")) ? s : NULL);
	key(w, "chapter");
	put(w, "null");
	key(w, "body");
	if ((s = text(p, dch_json_field(p, r, "body"), 0, tmp)) &&
	    strlen(s) > XCMD_BODY_MAX)
	{
		/* decoded text is valid UTF-8: back off continuation bytes */
		for (l = XCMD_BODY_MAX; ((unsigned char)tmp[l] & 0xc0) == 0x80; l--)
			;
		tmp[l] = '\0';
		bodycut = 1;
	}
	putq(w, s);
	if (bodycut)
	{
		key(w, "cut");
		put(w, "true");
	}
	put(w, "}}");
}

enum xcmd_line_kind
xcmd_line(enum xcmd_kind kind, const char *s, size_t n, int first,
          unsigned long pos, char *out, size_t cap)
{
	char line[XCMD_LINE_MAX + 1], tmp[XCMD_LINE_MAX + 1];
	char idb[XR_TITLE_MAX * 4 + 1], tb[XR_TITLE_MAX * 4 + 1], pb[24];
	struct xc_out w = { out, 0, cap < XCMD_LINE_MAX + 1 ? cap
	                                                     : XCMD_LINE_MAX + 1,
	                    0 };
	struct json_parser p;
	const char *id, *title;
	enum xcmd_line_kind k = XCMD_SKIP;
	size_t i;
	int r;

	if (cap)
		out[0] = '\0';
	for (i = 0; i < n && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r'); i++)
		;
	if (i == n)
		return XCMD_BLANK;
	if (n > XCMD_LINE_MAX || memchr(s, '\0', n) || cap < 2)
		return XCMD_SKIP;
	memcpy(line, s, n);
	line[n] = '\0';
	if (dch_json_parse(&p, line) != 0 || p.tok[0].kind != JSON_OBJECT)
		goto done;
	/* {"title":"..."} alone, first: the list's title */
	if (first && p.ntok == 3 && dch_json_field(&p, 0, "title") == 2 &&
	    p.tok[2].kind == JSON_STRING)
	{
		if (dch_json_decode(&p, 2, tmp, sizeof tmp) == 0 &&
		    cap >= XR_TITLE_MAX * 4 + 1)
		{
			xr_title(tmp, strlen(tmp), out, cap);
			k = XCMD_TITLE;
		}
		goto done;
	}
	/* {"task":{...}} / {"memory":{...}}, else the bare row */
	r = dch_json_field(&p, 0, kind == XCMD_TASK ? "task" : "memory");
	if (r < 0 || p.tok[r].kind != JSON_OBJECT)
		r = 0;
	if (!(title = cut(&p, dch_json_field(&p, r, "title"), 0, tmp, tb)))
		goto done;	/* no title: skipped */
	if (!(id = cut(&p, dch_json_field(&p, r, "id"), 1, tmp, idb)))
	{
		snprintf(pb, sizeof pb, "%lu", pos);
		id = pb;
	}
	if (kind == XCMD_TASK)
		row_task(&w, &p, r, tmp, id, title);
	else
		row_memory(&w, &p, r, tmp, id, title);
	if (!w.bad)
		k = XCMD_ROW;
	else
		out[0] = '\0';
done:
	dch_json_free(&p);
	return k;
}

/* ---- the run ------------------------------------------------------------ */

/* The running command's group, for the signal handler. */
static volatile sig_atomic_t xc_pg;
static const int xc_sigs[] = {SIGTERM, SIGINT, SIGHUP};

/* The querying process is ending: the command's group goes with it. */
static void
xc_sig(int sig)
{
	if (xc_pg > 0)
		kill(-(pid_t)xc_pg, SIGKILL);
	signal(sig, SIG_DFL);
	raise(sig);
}

/* 1 pid has exited, 0 not yet, -1 not ours; never reaps it: an unreaped
** leader keeps its group id from being reused */
static int
xc_done(pid_t pid)
{
	siginfo_t si;

	memset(&si, 0, sizeof si);
	if (waitid(P_PID, (id_t)pid, &si, WEXITED | WNOHANG | WNOWAIT) != 0)
		return errno == EINTR ? 0 : -1;
	return si.si_pid == pid;
}

/* End the group: SIGTERM and XCMD_GRACE_MS first when term (the leader
** still runs), then SIGKILL, which also ends what the leader left behind;
** only then reap the leader, at most 1 s (one in uninterruptible I/O is
** left to init, as xray.c's res_reap does). The wait status, or -1. */
static int
xc_end(pid_t pid, int term)
{
	long long g;
	int st = 0;
	pid_t w;

	if (term)
	{
		kill(-pid, SIGTERM);
		for (g = xr_now_ms() + XCMD_GRACE_MS;
		     !xc_done(pid) && xr_now_ms() < g;)
			poll(NULL, 0, 10);
	}
	kill(-pid, SIGKILL);
	xc_pg = 0;
	for (g = xr_now_ms() + 1000;
	     ((w = waitpid(pid, &st, WNOHANG)) == 0 ||
	      (w < 0 && errno == EINTR)) && xr_now_ms() < g;)
		poll(NULL, 0, 10);
	return w == pid ? st : -1;
}

/* "NAME=value", malloc'd */
static char *
xc_env(const char *name, const char *v)
{
	size_t l = strlen(name) + strlen(v ? v : "") + 2;
	char *e = malloc(l);

	if (e)
		snprintf(e, l, "%s=%s", name, v ? v : "");
	return e;
}

static const char *const xc_names[] = {"DCH_SESSION", "DCH_SESSION_CWD",
                                       "DCH_AGENT", "DCH_AGENT_SESSION_ID",
                                       "DCH_ROWS_MAX"};
#define XC_NAMES (sizeof xc_names / sizeof *xc_names)

/* environ minus our names, then ours; NULL when out of memory */
static char **
xc_envp(const struct xcmd_ctx *c, char *mine[XC_NAMES])
{
	const char *v[XC_NAMES];
	char rows[16], **e;
	size_t n = 0, k, j;

	snprintf(rows, sizeof rows, "%d", XCMD_ROWS_MAX);
	v[0] = c->session;
	v[1] = c->cwd;
	v[2] = c->agent;
	v[3] = c->agent_sid;
	v[4] = rows;
	for (k = 0; k < XC_NAMES; k++)
		if (!(mine[k] = xc_env(xc_names[k], v[k])))
			return NULL;
	while (environ[n])
		n++;
	if (!(e = malloc((n + XC_NAMES + 1) * sizeof *e)))
		return NULL;
	for (k = j = 0; k < n; k++)
	{
		size_t m;

		for (m = 0; m < XC_NAMES; m++)
		{
			size_t l = strlen(xc_names[m]);

			if (!strncmp(environ[k], xc_names[m], l) &&
			    environ[k][l] == '=')
				break;
		}
		if (m == XC_NAMES)
			e[j++] = environ[k];
	}
	for (k = 0; k < XC_NAMES; k++)
		e[j++] = mine[k];
	e[j] = NULL;
	return e;
}

/* The child, after fork: only async-signal-safe calls before the exec
** (execvp's PATH walk aside, as every fork-exec has). Never returns. */
static void xc_child(char **, char **, const char *, int, int, int, int, int)
	__attribute__((noreturn));

static void
xc_child(char **argv, char **envp, const char *cwd, int in, int out, int err,
         int ep, int maxfd)
{
	struct sigaction sa;
	sigset_t none;
	int e, fd, s;

	setpgid(0, 0);
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = SIG_DFL;
	for (s = 1; s < NSIG; s++)
		if (s != SIGKILL && s != SIGSTOP)
			sigaction(s, &sa, NULL);
	sigemptyset(&none);
	sigprocmask(SIG_SETMASK, &none, NULL);
	if ((cwd && *cwd && chdir(cwd) != 0) || dup2(in, 0) < 0 ||
	    dup2(out, 1) < 0 || dup2(err, 2) < 0)
		goto fail;
	for (fd = 3; fd < maxfd; fd++)
		if (fd != ep)
			close(fd);
	environ = envp;
	execvp(argv[0], argv);
fail:
	e = errno;
	if (write(ep, &e, sizeof e) < 0)
		e = 0;
	_exit(127);
}

/* One past the highest open fd, for the child's close loop: from the fd
** directory (a loop to the limit costs 0.5 s at 64K), else the soft limit
** held to 64K. */
static int
xc_maxfd(void)
{
	struct rlimit rl;
	struct dirent *e;
	DIR *d = opendir("/dev/fd");	/* Linux: a link to /proc/self/fd */
	int fd, max = 2;

	if (d)
	{
		while ((e = readdir(d)))
			if ((fd = atoi(e->d_name)) > max && fd != dirfd(d))
				max = fd;
		closedir(d);
		return max + 1;
	}
	if (getrlimit(RLIMIT_NOFILE, &rl) != 0 || rl.rlim_cur == RLIM_INFINITY ||
	    rl.rlim_cur > 65536)
		return 65536;
	return (int)rl.rlim_cur;
}

/* What the reading loop carries. */
struct xc_run
{
	enum xcmd_kind kind;
	xcmd_row_fn fn;
	void *arg;
	struct xcmd_result *res;
	char *line, *row;	/* XCMD_LINE_MAX + 1 each */
	size_t ln;
	int over;		/* inside a line past XCMD_LINE_MAX */
	int first;
	int stop;		/* XC_* */
};

enum { XC_RUN, XC_CLOCK, XC_BYTES, XC_ROWS, XC_CALLER };

static void
xc_one(struct xc_run *r, const char *s, size_t n)
{
	enum xcmd_line_kind k;

	if (r->res->rows >= XCMD_ROWS_MAX)
	{
		/* one more line past the cap: the command has more */
		if (xcmd_line(r->kind, s, n, 0, 0, r->row, 0) != XCMD_BLANK)
		{
			r->stop = XC_ROWS;
			r->res->skipped++;	/* left out: the done line's dropped */
		}
		return;
	}
	k = xcmd_line(r->kind, s, n, r->first, r->res->rows + 1, r->row,
	              XCMD_LINE_MAX + 1);
	if (k == XCMD_BLANK)
		return;
	r->first = 0;
	if (k == XCMD_TITLE)
		snprintf(r->res->title, sizeof r->res->title, "%s", r->row);
	else if (k == XCMD_SKIP)
		r->res->skipped++;
	else
	{
		r->res->rows++;
		if (r->fn && r->fn(r->row, r->arg))
			r->stop = XC_CALLER;
	}
}

/* stdout bytes into lines; a line past XCMD_LINE_MAX is dropped as it
** goes (never held) and counted once at its end */
static void
xc_feed(struct xc_run *r, const char *b, size_t n)
{
	while (n && r->stop == XC_RUN)
	{
		const char *nl = memchr(b, '\n', n);
		size_t seg = nl ? (size_t)(nl - b) : n;

		if (!r->over && seg > XCMD_LINE_MAX - r->ln)
		{
			r->over = 1;
			r->ln = 0;
		}
		if (!r->over)
		{
			memcpy(r->line + r->ln, b, seg);
			r->ln += seg;
		}
		if (!nl)
			return;
		if (r->over)
		{
			r->res->skipped++;
			r->first = 0;
		}
		else
			xc_one(r, r->line, r->ln);
		r->over = 0;
		r->ln = 0;
		b += seg + 1;
		n -= seg + 1;
	}
}

static void
xc_warn(struct xcmd_result *res, const char *what, const char *fmt, ...)
	__attribute__((format(printf, 3, 4)));

static void
xc_warn(struct xcmd_result *res, const char *what, const char *fmt, ...)
{
	va_list ap;
	int l = snprintf(res->warn, sizeof res->warn, "%s: ", what);

	va_start(ap, fmt);
	if (l > 0 && (size_t)l < sizeof res->warn)
		vsnprintf(res->warn + l, sizeof res->warn - (size_t)l, fmt, ap);
	va_end(ap);
}

int
xcmd_rows(enum xcmd_kind kind, const struct xcmd_ctx *ctx, xcmd_row_fn fn,
          void *arg, struct xcmd_result *res)
{
	const char *what = kind == XCMD_TASK ? "tasks_command" : "memory_command";
	char buf[4096], *argv[XCMD_ARGS_MAX + 1], *mine[XC_NAMES] = {0}, **envp;
	char chunk[XCMD_LINE_MAX], eb[1024], prog[XR_TITLE_MAX * 4 + 1];
	struct xc_run r;
	struct sigaction sa, old[3];
	struct pollfd p[2];
	sigset_t set, omask;
	size_t en = 0, total = 0;
	int po[2] = {-1, -1}, pe[2] = {-1, -1}, ep[2] = {-1, -1}, in = -1;
	int k, st, e = 0, maxfd;
	long long until;
	pid_t pid = -1;
	ssize_t n;

	memset(res, 0, sizeof *res);
	res->code = -1;
	for (k = 0; k < 3; k++)
		old[k].sa_handler = SIG_IGN;	/* nothing to restore yet */
	if (!ctx->command || !*ctx->command)
		return 0;
	if (xcmd_argv(ctx->command, getenv("HOME"), buf, sizeof buf, argv,
	              XCMD_ARGS_MAX + 1) < 0)
	{
		xc_warn(res, what, "not a command line (an open quote, too long, "
		        "or ~/ with no HOME)");
		return 0;
	}
	xr_title(argv[0], strlen(argv[0]), prog, sizeof prog);
	memset(&r, 0, sizeof r);
	r.kind = kind;
	r.fn = fn;
	r.arg = arg;
	r.res = res;
	r.first = 1;
	if (!(envp = xc_envp(ctx, mine)) ||
	    !(r.line = malloc(XCMD_LINE_MAX + 1)) ||
	    !(r.row = malloc(XCMD_LINE_MAX + 1)) || pipe(po) != 0 ||
	    pipe(pe) != 0 || pipe(ep) != 0 ||
	    (in = open("/dev/null", O_RDONLY)) < 0)
	{
		xc_warn(res, what, "cannot run %s: %s", prog, strerror(errno));
		goto out;
	}
	for (k = 0; k < 2; k++)
	{
		fcntl(po[k], F_SETFD, FD_CLOEXEC);
		fcntl(pe[k], F_SETFD, FD_CLOEXEC);
		fcntl(ep[k], F_SETFD, FD_CLOEXEC);
	}
	fcntl(in, F_SETFD, FD_CLOEXEC);
	maxfd = xc_maxfd();
	sigemptyset(&set);
	for (k = 0; k < 3; k++)
		sigaddset(&set, xc_sigs[k]);
	/* no window where the command runs and a SIGTERM would miss its group */
	sigprocmask(SIG_BLOCK, &set, &omask);
	if ((pid = fork()) == 0)
		xc_child(argv, envp, ctx->cwd, in, po[1], pe[1], ep[1], maxfd);
	if (pid > 0)
	{
		setpgid(pid, pid);	/* both sides: no race with the child's */
		xc_pg = pid;
		memset(&sa, 0, sizeof sa);
		sa.sa_handler = xc_sig;
		for (k = 0; k < 3; k++)
			if (sigaction(xc_sigs[k], NULL, &old[k]) != 0)
				old[k].sa_handler = SIG_IGN;	/* left alone */
			else if (old[k].sa_handler != SIG_IGN)
				sigaction(xc_sigs[k], &sa, NULL);
	}
	else
		e = errno;
	sigprocmask(SIG_SETMASK, &omask, NULL);
	close(po[1]);
	close(pe[1]);
	close(ep[1]);
	close(in);
	po[1] = pe[1] = ep[1] = in = -1;
	if (pid < 0)
	{
		xc_warn(res, what, "cannot run %s: %s", prog, strerror(e));
		goto out;
	}
	/* the exec closes ep; an errno on it is a chdir or exec that failed */
	while ((n = read(ep[0], &e, sizeof e)) < 0 && errno == EINTR)
		;
	if (n == sizeof e)
	{
		st = xc_end(pid, 0);
		xc_warn(res, what, "cannot run %s: %s", prog, strerror(e));
		goto restore;
	}
	res->ran = 1;
	until = xr_now_ms() + XCMD_WALL_MS;
	fcntl(po[0], F_SETFL, O_NONBLOCK);
	fcntl(pe[0], F_SETFL, O_NONBLOCK);
	p[0].fd = po[0];
	p[1].fd = pe[0];
	p[0].events = p[1].events = POLLIN;
	while (r.stop == XC_RUN && (p[0].fd >= 0 || p[1].fd >= 0))
	{
		long long left = until - xr_now_ms();

		if (left <= 0)
		{
			r.stop = XC_CLOCK;
			break;
		}
		if (poll(p, 2, (int)left) < 0)
		{
			if (errno == EINTR)
				continue;
			break;
		}
		for (k = 0; k < 2 && r.stop == XC_RUN; k++)
		{
			if (p[k].fd < 0 || !p[k].revents)
				continue;
			/* stderr: its first bytes kept, the rest drained */
			if (k && en < sizeof eb)
				n = read(p[k].fd, eb + en, sizeof eb - en);
			else
				n = read(p[k].fd, chunk, sizeof chunk);
			if (n > 0 && k)
				en += en < sizeof eb ? (size_t)n : 0;
			else if (n > 0 && !k)
			{
				size_t take = (size_t)n < XCMD_OUT_MAX - total
				              ? (size_t)n : XCMD_OUT_MAX - total;

				total += take;
				xc_feed(&r, chunk, take);
				if (r.stop == XC_RUN && take < (size_t)n)
					r.stop = XC_BYTES;
			}
			else if (n == 0 ||
			         (n < 0 && errno != EINTR && errno != EAGAIN))
			{
				close(p[k].fd);
				p[k].fd = -1;
			}
		}
	}
	/* the last line, no newline after it */
	if (r.stop == XC_RUN && p[0].fd < 0)
	{
		if (r.over)
			res->skipped++;	/* the last line: first no longer matters */
		else if (r.ln)
			xc_one(&r, r.line, r.ln);
	}
	/* closed first: a writer past here gets EPIPE, most end on it */
	for (k = 0; k < 2; k++)
		if (p[k].fd >= 0)
			close(p[k].fd);
	po[0] = pe[0] = -1;
	if (r.stop == XC_RUN)
		while (!(k = xc_done(pid)) && xr_now_ms() < until)
			poll(NULL, 0, 5);
	else
		k = 0;
	if (r.stop == XC_RUN && !k)
		r.stop = XC_CLOCK;	/* closed its output, still running */
	st = xc_end(pid, r.stop != XC_RUN);
	res->stopped = r.stop != XC_RUN && r.stop != XC_CALLER;
	if (st != -1 && WIFEXITED(st))
		res->code = WEXITSTATUS(st);
	xr_title(eb, en, res->err, sizeof res->err);
	if (r.stop == XC_CLOCK)
		xc_warn(res, what, "stopped early after %d s", XCMD_WALL_MS / 1000);
	else if (r.stop == XC_BYTES)
		xc_warn(res, what, "stopped early at %u MB of output",
		        XCMD_OUT_MAX >> 20);
	else if (r.stop == XC_ROWS)
		xc_warn(res, what, "stopped early at %d rows", XCMD_ROWS_MAX);
	else if (r.stop == XC_CALLER)
		;
	else if (st != -1 && WIFSIGNALED(st))
		xc_warn(res, what, "ended by signal %d%s%s", WTERMSIG(st),
		        *res->err ? ": " : "", res->err);
	else if (res->code > 0)
		xc_warn(res, what, "exit %d%s%s", res->code, *res->err ? ": " : "",
		        res->err);
	else if (res->code < 0)
		xc_warn(res, what, "not reaped");
	else if (!res->rows && res->skipped)
		xc_warn(res, what, "no valid row (%lu lines skipped)",
		        res->skipped);
restore:
	(void)st;
	sigprocmask(SIG_BLOCK, &set, &omask);
	for (k = 0; k < 3; k++)
		if (old[k].sa_handler != SIG_IGN)
			sigaction(xc_sigs[k], &old[k], NULL);
	sigprocmask(SIG_SETMASK, &omask, NULL);
out:
	for (k = 0; k < 2; k++)
	{
		if (po[k] >= 0)
			close(po[k]);
		if (pe[k] >= 0)
			close(pe[k]);
		if (ep[k] >= 0)
			close(ep[k]);
	}
	if (in >= 0)
		close(in);
	for (k = 0; k < (int)XC_NAMES; k++)
		free(mine[k]);
	free(envp);
	free(r.line);
	free(r.row);
	return (int)res->rows;
}
