/*
** The X-ray view (#175, XR5a): `dch --xray <n>` with no --json, and the
** picker's x. Tabs over one session: Prompts (Commands for a shell,
** #295: the engine's chapters), Responses (each turn's answer, newest first,
** #290; none for a shell), Refs (the refs on the session's screen now
** first, #294), Search (where a word or phrase was said, #291) and Tasks
** (the session's task list, #320: only when it has one), Memory (its
** memories and instruction files, #323: the same), Schedules (its crons,
** /loop wake-up and Codex heartbeats, #687: the same); a ref's card, a
** task's, memory's or schedule's card; a chapter's reader. Drawn with the picker's renderer,
** on the terminal its host (dch.c) made raw and put on the alternate
** screen.
**
** Every answer is the engine's own --json: dch.c's xv_spawn forks this
** process and runs xray_main in the child with --follow, --ref, --chapter
** --search, --tasks, --memory or --schedules and --json, stdout into a pipe, and the
** view reads the NDJSON with json.c.
** No second parser of any corpus, no exec, nothing written to disk. The
** index lives as --follow's does (spec 3.1): a snapshot, then deltas, for as
** long as the view is open; every child is ended with it.
**
** Corpus text is hostile. It reaches the terminal only through pk_txt,
** which draws a control (ESC included) or a bidi control as '?' (K10.6).
*/
#include "dtach.h"
#include "picker.h"
#include "rframe.h"
#include "xray.h"

#include <ctype.h>

#ifndef XV_LINE_MAX
#define XV_LINE_MAX  (8u << 20)	/* an NDJSON line past it is dropped (the
				** engine's are under 4 MiB) */
#endif
#define XV_MENTIONS  10000	/* a card keeps at most these */
#define XV_STOP_MS   50	/* a child's grace after its SIGTERM (#204) */
#define XV_KILL_MS   250	/* after its SIGKILL, then kept (#212) */
#define XV_LATE      16	/* kept: SIGKILLed, not dead yet */

enum { XV_LIST, XV_CARD, XV_READ, XV_TCARD };
/* the tabs as drawn and cycled: 0 Prompts, 2 Responses (#290), 1 Refs (the
** chord's xv_tab), 3 Search (#291), 4 Tasks (#320), 5 Memory (#323), 6
** Schedules (#687) */
static const int xv_ord[] = { 0, 2, 1, 3, 4, 5, 6 };
#define XV_NTAB  ((int)(sizeof xv_ord / sizeof *xv_ord))
#define XV_ALINES 8	/* a response's lines on Responses; the reader has all */
/* follow, card, reader, a remote screen (#188), a search (#291), the task
** list (#320), the memory list (#323: XV_TK + 1, as tq[1] is tq[0] + 1),
** the schedules (#687: XV_TK + 2) */
enum { XV_FK, XV_CK, XV_RK, XV_SK, XV_QK, XV_TK, XV_MK, XV_HK, XV_NKIDS };
#define XV_TASKS   50000	/* the task or memory rows kept (XR_TASKS) */
#define XV_TLINE   (64u << 10)	/* a --tasks or --memory line past it is
				** dropped */
#define XV_MBODY   (16u << 20)	/* the memory text kept in all (#323) */
#define XV_MEVERY  30000	/* Memory's re-read, while shown */
#define XV_TEVERY  5000		/* a live session's list re-read at most so
				** often while shown (6 times that while not),
				** when --follow said something */

static char xv_none[1];	/* "": a field absent, null, or out of memory */
static volatile pid_t xv_pids[XV_NKIDS];	/* for xv_kill */
static volatile sig_atomic_t xv_cont;	/* SIGCONT: the shell had the tty */
int xv_tab;	/* the tab a view opens on (the in-attach chord's: Refs) */
char xv_go[512];	/* o (#194): the session the host attaches once the
			** view is left, as session_name holds it; "" none */

struct xv;
struct xv_kid
{
	pid_t pid;
	int fd, efd, skip, rc;	/* skip: a line past XV_LINE_MAX */
	char *buf;
	size_t len, cap, lmax;	/* lmax: its own line limit, 0 XV_LINE_MAX */
	char err[300];
	void (*line)(struct xv *v, const struct json_parser *p);
};

struct xv_ch
{
	char *anchor, *title, *kind, *summary;
	long long at, offset;
	int error, aut;	/* aut: "auto", a prompt the harness wrote (#192) */
	/* "peer" (#359): another agent's prompt, its sender's name ("" none
	** readable); xv_none when absent (an older engine or host) */
	char *peer;
	/* the turn's answer and tool line (#296), "" when absent (an older
	** host's); roff -1 none, rcut: the engine cut it */
	char *resp, *tools;
	long long roff;
	int rcut;
};

struct xv_ref
{
	char *key, *shape, *text, *title;
	char *full;		/* a commit's fullest SHA, else "" */
	long long count, seen;	/* seen: later is more recent */
	long long last;		/* last_offset (#262): -1 from an older host */
	int defined, underline;
	int onscr;		/* in the on-screen or last-reply group (xv_vis) */
};

struct xv_def
{
	char *session, *title, *passage, *rule;
	char *url, *status;	/* a define_command's (#405) */
	int cmd;		/* "source":"command" */
	int lead;		/* #575: a "lead" line, until a definition comes */
	long long at;
};

struct xv_men
{
	char *session, *line, *chapter, *who, *tool;	/* who: "" none */
	char *sent;		/* #478: the sentence, "" from an older host */
	int ss, se;		/* the ref's bytes in sent, ss -1 none */
	long long at, off;	/* off: -1 from a host that sends none */
	/* #479: a line of this project's other session (its last_active
	** came: seen, the epoch s, -1 null), its prompt number (0 null), the
	** turn's title and the session's id; same 0 for any other line */
	int same, live;
	long long seen, turn;
	char *ttitle, *sid;
	int read;		/* #517: in a read-type call's output */
};

struct xv_ent
{
	char *role, *tool, *text;
	int error, cut;	/* cut: the engine cut the middle (a marker line) */
	long long off;	/* its record's, -1 from a host that sends none */
};

struct xv_hit		/* a search's hit (#291) */
{
	char *chapter, *who, *tool, *snippet;
	long long at, off;
	int head;		/* a You hit repeating its turn's title: the
				** group's head line, no line of its own (#457) */
};

struct xv_task		/* a --tasks row (#320), a --memory row (#323) */
{
	char *id, *title, *label, *active, *note, *created, *chapter, *url;
	char *group, *blocked;	/* blocked: blocked_by's ids, ", " between */
	char *path, *scope, *body;	/* a memory's; label is its kind */
	long long updated;	/* -1 none */
	int st, seq;		/* st: 0 active, 1 pending, 2 done, 3 dropped;
				** a memory's 0 this session's, 1 the rest, 2
				** instructions; seq: the answer's order */
	int ses, cut;		/* ses: 1 this session wrote it, 2 loaded it;
				** cut: 1 the engine cut the text, 2 not kept */
	/* a --schedules row's (#687): title is its prompt, label its human
	** schedule, note its reason; st 0 pending, 1 fired, 2 the rest
	** (stw says which); times epoch s, -1 none */
	char *cron, *kind, *stw;
	long long next, lastf, expires, fires, noop;
	int recur;
};

/* One --tasks answer: its rows, their order (grouped, Done newest first),
** its header and end. A list a later tab can keep the same way. */
struct xv_tl
{
	struct xv_task *t;
	int n, a, *ord, aord, nord, nst[4];	/* nst: the rows of each st */
	char *title, *warn;
	int partial, done, dropped;	/* done: its done line came */
	int hi, lo;	/* the next row's seq at the end, before the first */
	size_t bytes;			/* a memory list's text kept */
};

/* A list's query: Tasks' (tq[0]), Memory's (tq[1]) or Schedules' (tq[2]). The list shown, a
** re-read's until its end (re: the answer goes there); the order is made
** again (sort) at most every 100 ms while rows stream; no: never asked
** again; ci: the card's row; sel: the selected row's id */
struct xv_tq
{
	struct xv_tl tl, tn;
	int re, sort, gen, fol, no, ci;
	long long at, sat;	/* the query's start, the last sort */
	char *sel;
};

struct xv_rl		/* a reader line: bytes [s, s + n) of its text */
{
	const char *s;
	size_t n;
	int kind;		/* 0 an entry's head, 1 text, 2 output */
};

struct xv
{
	int fd, color, mode, back, tab, win, filtering;
	int hl;			/* a remote one's "host:" length, else 0 */
	char n[700];		/* the session as given */
	char fail[400];		/* the host's refusal, in place of the tabs */
	char status[400], filt[256];
	/* Search (#291): the text typed, the one searched (as big as filt:
	** xv_filt edits either); the list's one line in place of hits (an
	** older host, no corpus) */
	char q[256], sq[256], sline[400];
	struct xv_hit *hit;
	int nh, ah, sdone, smore, spart;	/* sdone: its answer came */
	char *swhy;
	char stop[400];		/* the follow's end: the status row when it is
				** empty, never replaced (QA 1.57.0 LOW-R3) */
	/* the index: --follow's snapshot and its deltas */
	int have, ended, live, mentions_only;
	long long lines, claudes;	/* claudes: -1 when not said (#256) */
	char *session, *sid, *corpus, *reason, *why, *label;
	struct xv_ch *ch;
	struct xv_ref *rf;
	int nch, ach, nrf, arf;
	int alltmp, ntmp;	/* Refs: temp files shown; their rows */
	int allk, nk;		/* Refs: files, commits and URLs shown; their
				** rows (#262) */
	int allauto, nauto;	/* Chapters: auto chapters shown; their rows */
	int hidepeer, npeer, peernote;	/* Prompts, Responses: cross-session rows
				** hidden (a, #360); their rows */
	long long rseq;
	struct xv_kid kid[XV_NKIDS];
	int sel[XV_NTAB], top[XV_NTAB], *vis, nvis, avis;	/* Responses: items */
	int ngrp, ngrp2;	/* Refs: vis's first ngrp rows are on screen, the
				** next up to ngrp2 are in the last reply */
	char *selk;		/* Refs: the selected row's key, followed when
				** the rows change */
	char *sela;		/* Responses: the selected item's anchor, so */
	char *selp;		/* Prompts: the same, its own (#358) */
	int old;		/* Responses: no chapter with an answer field */
	/* the card */
	char *ckey, *ctext;
	char *cref;		/* --ref's text: the fullest form known (#415) */
	char *cwarn;		/* done's define_warn (#405), else NULL */
	int full, scope, cdone, cpartial, csel, ctop;
	int chdr;		/* #478: on the header of csel's turn */
	/* the selected mention's session and offset, followed as lines
	** arrive (#304); cses NULL on the first row */
	char *cses;
	long long coff;
	struct xv_def *d;
	struct xv_men *m;
	int nd, ad, nm, am;
	long long cnt[5];	/* session, project, project_sessions, all,
				** all_sessions; -1 null */
	char *vkind, *vtitle, *vsub;	/* --full's resolved line */
	/* the reader */
	char *rtitle, *rwant;
	char *rans;		/* opened on an answer: its start (#290) */
	char *rsnip;		/* opened on a Search hit: its snippet */
	long long roff;		/* and its record's offset (#356), -1 none */
	int rskip;		/* the entries before it holding it */
	int rtwins;		/* its twins (itself too) when offsets came */
	int rwho;		/* its who's first letter */
	char *rtool;		/* and its tool */
	char *ranchor, *rturn;	/* the chapter shown; one being turned to */
	char *rn;		/* #479: the reader's session when not v's */
	long long rat;
	int rtrunc, rtop, rw, rloaded;
	int rend, redge;	/* open at its end; the hint said at that end */
	struct xv_ent *e;
	struct xv_rl *rl;
	int ne, ae, nrl, arl;
	/* the screen's refs (#294): one per key, the lowest row's first; not
	** the index's, so count 0 and no title */
	struct xv_ref *sr;
	int nsr, asr;
	struct xv_ref *lr;	/* the newest response's refs, document order */
	int nlr, alr;
	long long scr_at;
	/* r (#454): the refs in the lines shown, the lowest first, by the
	** screen's grammar; strip: their picker is open, ssel and stp its
	** selection and top; sback: the reader's back while r's card is up */
	struct xv_ref *rr;
	int nrr, arr, strip, ssel, stp, sback;
	/* / in the reader (#454): the text found (as big as filt: xv_filt
	** edits it), the match line n and N go on from (-1 none) */
	char rq[256];
	int rcur;
	/* Tasks (#320), Memory (#323), Schedules (#687): vis holds their rows
	** while vtab is 4, 5 or 6 and the list's gen (tvgen) and tvf say they are current */
	struct xv_tq tq[3];
	int tvgen, vtab, mtop;	/* mtop: a memory card's text scrolled */
	char tvf[256];		/* the filter vis was made with */
	int tg[4];		/* vis's first row of each st, -1 none */
	/* the follow carries Tasks, Memory (#448) and Schedules (#687): blk,
	** the block it is sending (0 tasks, 1 memory, 2 schedules, -1 none);
	** poll, a host that refused that follow (an older dch): 1 the follow
	** with Tasks and Memory, Schedules one own query; 2 the follow alone,
	** the lists' own children */
	int blk, poll;
};

void
xv_kill(void)
{
	int k;

	for (k = 0; k < XV_NKIDS; k++)
		if (xv_pids[k] > 0)
			kill(xv_pids[k], SIGTERM);
}

/* ---- small things ---------------------------------------------------- */

/* What the view calls a chapter (#295): one prompt with what followed it
** in a Claude or Codex session (several Claudes too), else one command.
** --json and --chapter keep "chapter". */
static const char *
xv_noun(const struct xv *v)
{
	const char *c = v->corpus ? v->corpus : "claude";

	return strcmp(c, "claude") && strcmp(c, "codex") &&
	       (strcmp(c, "none") || v->claudes < 2) ? "command" : "prompt";
}

static void
xv_sfree(char *s)
{
	if (s && s != xv_none)
		free(s);
}

/* Room for one more of *n elements of sz bytes in *pp. 0, -1 no memory. */
static int
xv_grow(void *pp, int *cap, int n, size_t sz)
{
	void **p = pp, *q;
	int c = *cap ? *cap * 2 : 64;

	if (n < *cap)
		return 0;
	if (!(q = realloc(*p, (size_t)c * sz)))
		return -1;
	*p = q;
	*cap = c;
	return 0;
}

/* Field k of object o: a string, decoded (xv_none when absent, null or
** out of memory; free with xv_sfree). */
static char *
xv_str(const struct json_parser *p, int o, const char *k)
{
	int t = o >= 0 ? dch_json_field(p, o, k) : -1;
	size_t n;
	char *s;

	if (t < 0 || p->tok[t].kind != JSON_STRING)
		return xv_none;
	/* decoded, a string is never longer than its escaped form */
	n = (size_t)(p->tok[t].end - p->tok[t].start) + 1;
	if (!(s = malloc(n)))
		return xv_none;
	if (dch_json_decode(p, t, s, n) < 0)
		s[0] = '\0';
	return s;
}

/* A number field, def when absent, null or not a number. */
static long long
xv_num(const struct json_parser *p, int o, const char *k, long long def)
{
	int t = o >= 0 ? dch_json_field(p, o, k) : -1;
	const char *s;

	if (t < 0 || p->tok[t].kind != JSON_PRIMITIVE)
		return def;
	s = p->text + p->tok[t].start;
	return *s == '-' || isdigit((unsigned char)*s) ? strtoll(s, NULL, 10)
	                                                : def;
}

static int
xv_bool(const struct json_parser *p, int o, const char *k)
{
	int t = o >= 0 ? dch_json_field(p, o, k) : -1;

	return t >= 0 && p->tok[t].kind == JSON_PRIMITIVE &&
	       p->text[p->tok[t].start] == 't';
}

/* Field k of o, when it is an object (or kind). */
static int
xv_obj(const struct json_parser *p, int o, const char *k, int kind)
{
	int t = o >= 0 ? dch_json_field(p, o, k) : -1;

	return t >= 0 && (int)p->tok[t].kind == kind ? t : -1;
}

static void
xv_when(long long at, char *b, size_t n)
{
	time_t t = (time_t)at;
	struct tm tm;

	if (at < 0 || !localtime_r(&t, &tm) || !strftime(b, n, "%m-%d %H:%M",
	                                                 &tm))
		snprintf(b, n, "%s", "");
}

/* ASCII case folded: does s hold f? */
static int
xv_has(const char *s, const char *f)
{
	size_t k, fl = strlen(f);

	for (; *s; s++)
	{
		for (k = 0; k < fl && s[k] &&
		     tolower((unsigned char)s[k]) == tolower((unsigned char)f[k]);
		     k++)
			;
		if (k == fl)
			return 1;
	}
	return !fl;
}

/* Does s start with w, ASCII letters folded as the engine's --search
** (#291)? */
static int
xv_at(const char *s, const char *w)
{
	for (; *w; s++, w++)
		if ((*s >= 'A' && *s <= 'Z' ? *s + 32 : *s) !=
		    (*w >= 'A' && *w <= 'Z' ? *w + 32 : *w))
			return 0;
	return 1;
}

/* Does w start in s[0..n)? s goes on past n (a reader line's text), so a
** match may end on a later line; ASCII letters folded (#291). */
static int
xv_in(const char *s, size_t n, const char *w)
{
	size_t k;

	for (k = 0; *w && k < n; k++)
		if (xv_at(s + k, w))
			return 1;
	return 0;
}

/* Where in s a search hit's snippet w starts (MED-V21), w as the engine
** cuts it: a run of blanks and controls one space. NULL: not in s. */
static const char *
xv_snipat(const char *s, const char *w)
{
	const unsigned char *a, *b;

	if (!strncmp(w, "\xe2\x80\xa6 ", 4))	/* the engine's lead: not text */
		w += 4;
	for (; *w && *s; s++)
	{
		for (a = (const unsigned char *)s, b = (const unsigned char *)w;
		     *b; b++)
			if (*b != ' ')
			{
				if (*a++ != *b)
					break;
			}
			else if (*a > ' ' && *a != 0x7f)
				break;
			else
				while (*a && (*a <= ' ' || *a == 0x7f))
					a++;
		if (!*b)
			return s;
	}
	return NULL;
}

/* n bytes of s, drawn (hostile text: pk_txt). */
static void
xv_txtn(const char *s, size_t n)
{
	char b[4100];

	if (n >= sizeof b)
		n = sizeof b - 1;
	memcpy(b, s, n);
	b[n] = '\0';
	pk_txt(b);
}

/* The bytes of s[0..n) that fit in w cells, at a character's end. */
static size_t
xv_fitlen(const char *s, size_t n, int w)
{
	size_t k = 0;
	int l, cw = 0, st = 0;

	while (k < n && s[k])
	{
		char c[5] = "";

		/* pk_cps: as wide as pk_txt draws it */
		l = 1;
		(void)ap_cp(s + k, &l);
		if (l < 1 || k + (size_t)l > n)
			l = 1;
		memcpy(c, s + k, (size_t)l);
		cw += pk_cwx(ap_cp(c, &(int){1}), &st);
		if (cw > w)
			break;
		k += (size_t)l;
	}
	return k;
}

/* The bytes of s[0..n) for one row of w cells: at the last space that
** fits (the row ends where the next byte is a space); a hard break, at a
** character's end, only for a word wider than the row. */
static size_t
xv_brk(const char *s, size_t n, int w)
{
	size_t f = xv_fitlen(s, n, w), j;

	if (f >= n || s[f] == ' ')
		return f;
	for (j = f; j > 0; j--)
		if (s[j] == ' ')
			return j;
	return f;
}

/* ---- the children ---------------------------------------------------- */

/* Reap the children xv_stop kept (#212), then keep pid (> 0) too; 0 when
** there is no room. Main context only: xv_kill never sees these, a
** SIGKILLed child needs no SIGTERM, and a kept pid is never signalled. */
static int
xv_late(pid_t pid)
{
	static pid_t late[XV_LATE];
	int i, room = -1;
	pid_t w;

	for (i = 0; i < XV_LATE; i++)
	{
		if (late[i] > 0 && ((w = waitpid(late[i], NULL, WNOHANG)) > 0 ||
		                    (w < 0 && errno != EINTR)))
			late[i] = 0;
		if (!late[i] && room < 0)
			room = i;
	}
	if (pid > 0 && room >= 0)
		late[room] = pid;
	return pid <= 0 || room >= 0;
}

/* A child ended. Its pipes close first: a write it is blocked in fails
** (EPIPE) instead of waiting for a reader that is gone (QA 1.57.0 HIGH-R1).
** SIGTERM lets a remote one send the host its 0x34; after XV_STOP_MS it is
** SIGKILLed, so the view never sits out a silent host's RM_CANCEL_MS
** (MED-R2): the host takes the closed connection as the cancel. The
** signals that run xv_kill wait until the pid is reaped (or kept) and its
** slot cleared, so xv_kill never signals a reaped pid. One still alive
** XV_KILL_MS after its SIGKILL is in uninterruptible I/O (a hung mount):
** SIGKILL waits for that call to return, so it is kept and reaped later,
** and the view goes on (#212). */
static void
xv_stop(struct xv_kid *k, int slot)
{
	if (k->fd >= 0)
		close(k->fd);
	if (k->efd >= 0)
		close(k->efd);
	if (k->pid > 0)
	{
		struct timespec ts = { 0, 5000000 };
		long long end = now_ms() + XV_STOP_MS;
		sigset_t s, o;
		pid_t w;

		sigemptyset(&s);
		sigaddset(&s, SIGINT);
		sigaddset(&s, SIGTERM);
		sigaddset(&s, SIGHUP);
		sigprocmask(SIG_BLOCK, &s, &o);
		kill(k->pid, SIGTERM);
		while ((w = waitpid(k->pid, NULL, WNOHANG)) == 0 && now_ms() < end)
			nanosleep(&ts, NULL);
		if (w == 0)
		{
			kill(k->pid, SIGKILL);
			end = now_ms() + XV_KILL_MS;
			while ((w = waitpid(k->pid, NULL, WNOHANG)) == 0 &&
			       now_ms() < end)
				nanosleep(&ts, NULL);
		}
		/* no room to keep it: the wait for good, as before */
		if (w == 0 && !xv_late(k->pid))
			while (waitpid(k->pid, NULL, 0) < 0 && errno == EINTR)
				;
		xv_pids[slot] = 0;
		sigprocmask(SIG_SETMASK, &o, NULL);
	}
	free(k->buf);
	memset(k, 0, sizeof *k);
	k->fd = k->efd = -1;
	k->rc = -1;
}

/* `dch --xray <av...>` in a child of this process (dch.c's xv_spawn); its
** lines to fn. 0, or -1 with the status said. */
static int
xv_start(struct xv *v, int slot, char **av, int ac,
         void (*fn)(struct xv *v, const struct json_parser *p))
{
	struct xv_kid *k = &v->kid[slot];

	xv_stop(k, slot);
	if ((k->pid = xv_spawn(v->fd, ac, av, &k->fd, &k->efd)) < 0)
	{
		k->pid = 0;
		k->fd = k->efd = -1;
		snprintf(v->status, sizeof v->status, "X-ray: %s",
		         strerror(errno));
		return -1;
	}
	xv_pids[slot] = k->pid;
	(void)fcntl(k->fd, F_SETFL, O_NONBLOCK);
	(void)fcntl(k->efd, F_SETFL, O_NONBLOCK);
	k->line = fn;
	return 0;
}

static void
xv_lines(struct xv *v, struct xv_kid *k, int eof)
{
	size_t at = 0;
	char *nl;

	if (!k->line)
	{
		/* a remote screen's bytes, whole: the tail xv_plain keeps */
		if (k->len > 128 * 1024)
		{
			memmove(k->buf, k->buf + k->len - 128 * 1024, 128 * 1024);
			k->len = 128 * 1024;
		}
		return;
	}
	while (at < k->len)
	{
		struct json_parser p;

		nl = memchr(k->buf + at, '\n', k->len - at);
		if (!nl && !eof)
			break;
		if (nl)
			*nl = '\0';
		else
			k->buf[k->len] = '\0';
		/* the tail of a dropped over-long line: nothing parsed, nothing
		** to free */
		if (!k->skip && (!k->lmax || (nl ? (size_t)(nl - k->buf) : k->len)
		                 - at <= k->lmax))
		{
			if (dch_json_parse(&p, k->buf + at) == 0 &&
			    p.tok[0].kind == JSON_OBJECT)
				k->line(v, &p);
			dch_json_free(&p);
		}
		k->skip = 0;
		at = nl ? (size_t)(nl - k->buf) + 1 : k->len;
	}
	memmove(k->buf, k->buf + at, k->len - at);
	k->len -= at;
	if (k->len >= (k->lmax ? k->lmax : XV_LINE_MAX))
	{
		k->len = 0;	/* the rest of that line goes too */
		k->skip = 1;
	}
}

/* What a child has written, its lines handed over; 1 when it has ended
** (rc set), else 0. */
static int
xv_pump(struct xv *v, int slot, fd_set *rs)
{
	struct xv_kid *k = &v->kid[slot];
	ssize_t r;
	int st;
	size_t el;
	pid_t w;
	long long t0 = now_ms();

	if (k->efd >= 0 && FD_ISSET(k->efd, rs))
	{
		char b[512];

		el = strlen(k->err);
		r = read(k->efd, b, sizeof b);
		if (r == 0 || (r < 0 && errno != EAGAIN && errno != EINTR))
		{
			close(k->efd);
			k->efd = -1;
		}
		else if (r > 0)
			snprintf(k->err + el, sizeof k->err - el, "%.*s", (int)r, b);
	}
	while (k->fd >= 0 && FD_ISSET(k->fd, rs))
	{
		if (k->len + 65537 > k->cap)
		{
			size_t c = k->cap ? k->cap * 2 : 1u << 17;
			char *b = realloc(k->buf, c);

			if (!b)
			{
				/* no memory: the child's answer is lost, it is ended */
				snprintf(k->err, sizeof k->err, "X-ray: out of memory");
				close(k->fd);
				k->fd = -1;
				break;
			}
			k->buf = b;
			k->cap = c;
		}
		r = read(k->fd, k->buf + k->len, 65536);
		if (r < 0 && (errno == EAGAIN || errno == EINTR))
			break;
		if (r <= 0)
		{
			xv_lines(v, k, 1);
			close(k->fd);
			k->fd = -1;
			break;
		}
		k->len += (size_t)r;
		xv_lines(v, k, 0);
		/* a long task or memory list: drawn as it comes (#320), the rest
		** next tick */
		if ((slot >= XV_TK || v->blk >= 0) && now_ms() - t0 >= 50)
			break;
	}
	if (k->pid <= 0 || k->fd >= 0 || k->efd >= 0)
		return 0;
	xv_pids[slot] = 0;
	while ((w = waitpid(k->pid, &st, WNOHANG)) < 0 && errno == EINTR)
		;
	if (w == 0)
	{
		/* its pipes closed as it exits, its exit not done: a later
		** tick reaps it (#212) */
		xv_pids[slot] = k->pid;
		return 0;
	}
	k->pid = 0;
	k->rc = WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
	/* its last line, without the newline */
	if ((el = strlen(k->err)) > 0 && k->err[el - 1] == '\n')
		k->err[el - 1] = '\0';
	return 1;
}

/* ---- the index (--follow, spec 3.2-3.3) ----------------------------- */

static void
xv_ch_free(struct xv_ch *c)
{
	xv_sfree(c->anchor);
	xv_sfree(c->title);
	xv_sfree(c->kind);
	xv_sfree(c->summary);
	xv_sfree(c->peer);
	xv_sfree(c->resp);
	xv_sfree(c->tools);
}

static void
xv_ref_free(struct xv_ref *r)
{
	xv_sfree(r->key);
	xv_sfree(r->shape);
	xv_sfree(r->text);
	xv_sfree(r->full);
	xv_sfree(r->title);
}

static void
xv_drop(struct xv *v)
{
	int k;

	for (k = 0; k < v->nch; k++)
		xv_ch_free(&v->ch[k]);
	for (k = 0; k < v->nrf; k++)
		xv_ref_free(&v->rf[k]);
	v->nch = v->nrf = 0;
}

static void
xv_chapter(struct xv *v, const struct json_parser *p, int o)
{
	struct xv_ch c;
	char *s;
	int k;

	c.anchor = xv_str(p, o, "anchor");
	c.title = xv_str(p, o, "title");
	c.kind = xv_str(p, o, "kind");
	c.at = xv_num(p, o, "at", -1);
	c.offset = xv_num(p, o, "offset", 0);
	c.error = xv_bool(p, o, "error");
	/* absent from an older engine or host: every chapter shown */
	c.aut = xv_bool(p, o, "auto");
	c.summary = xv_str(p, o, "summary");
	c.peer = xv_str(p, o, "peer");
	c.resp = xv_str(p, o, "response");
	c.tools = xv_str(p, o, "tools");
	c.roff = xv_num(p, o, "response_offset", -1);
	c.rcut = xv_bool(p, o, "response_cut");
	/* a tab or CR as a blank, as the reader's */
	for (s = c.resp; *s; s++)
		if (*s == '\t' || *s == '\r')
			*s = ' ';
	/* an anchor already held is replaced (3.3) */
	for (k = 0; k < v->nch && strcmp(v->ch[k].anchor, c.anchor); k++)
		;
	if (k == v->nch && xv_grow(&v->ch, &v->ach, v->nch, sizeof c) < 0)
	{
		xv_ch_free(&c);
		return;
	}
	if (k < v->nch)
		xv_ch_free(&v->ch[k]);
	else
		v->nch++;
	v->ch[k] = c;
}

static void
xv_ref(struct xv *v, const struct json_parser *p, int o)
{
	struct xv_ref r;
	int k;

	r.key = xv_str(p, o, "key");
	r.shape = xv_str(p, o, "shape");
	r.text = xv_str(p, o, "text");
	r.full = xv_str(p, o, "full");
	r.title = xv_str(p, o, "title");
	r.count = xv_num(p, o, "count", 0);
	r.last = xv_num(p, o, "last_offset", -1);
	r.defined = xv_bool(p, o, "defined");
	r.underline = xv_bool(p, o, "underline");
	r.onscr = 0;
	/* no recency in a ref line: its arrival (a snapshot's rows in their
	** order, a delta now) */
	r.seen = ++v->rseq;
	for (k = 0; k < v->nrf && strcmp(v->rf[k].key, r.key); k++)
		;
	if (k < v->nrf)
		xv_ref_free(&v->rf[k]);
	/* count 0 removes the key (3.3) */
	if (!r.count)
	{
		xv_ref_free(&r);
		if (k < v->nrf)
			memmove(&v->rf[k], &v->rf[k + 1],
			        (size_t)(--v->nrf - k) * sizeof r);
		return;
	}
	if (k == v->nrf && xv_grow(&v->rf, &v->arf, v->nrf, sizeof r) < 0)
	{
		xv_ref_free(&r);
		return;
	}
	if (k == v->nrf)
		v->nrf++;
	v->rf[k] = r;
}

static void
xv_snapshot(struct xv *v, const struct json_parser *p, int o)
{
	int a, t;

	xv_drop(v);
	xv_sfree(v->session);
	xv_sfree(v->sid);
	xv_sfree(v->corpus);
	xv_sfree(v->reason);
	xv_sfree(v->why);
	xv_sfree(v->label);
	v->session = xv_str(p, o, "session");
	v->sid = xv_str(p, o, "session_id");
	v->corpus = xv_str(p, o, "corpus");
	v->reason = xv_str(p, o, "reason");
	v->why = xv_str(p, o, "why");
	v->label = xv_str(p, o, "project_label");
	v->live = xv_bool(p, o, "live");
	v->mentions_only = xv_bool(p, o, "mentions_only");
	v->lines = xv_num(p, o, "lines", -1);
	v->claudes = xv_num(p, o, "claudes", -1);
	if ((a = xv_obj(p, o, "chapters", JSON_ARRAY)) >= 0)
		for (t = a + 1; t < p->tok[a].next; t = p->tok[t].next)
			if (p->tok[t].kind == JSON_OBJECT)
				xv_chapter(v, p, t);
	if ((a = xv_obj(p, o, "refs", JSON_ARRAY)) >= 0)
		for (t = a + 1; t < p->tok[a].next; t = p->tok[t].next)
			if (p->tok[t].kind == JSON_OBJECT)
				xv_ref(v, p, t);
	v->have = 1;
}

static void xv_back(struct xv *v);
static int xv_tfline(struct xv *v, const struct json_parser *p);

/* One --follow line (3.3). */
static void
xv_fline(struct xv *v, const struct json_parser *p)
{
	int o, k, j;

	/* Tasks', Memory's and Schedules' cue to re-read (#320, #323, #687) */
	v->tq[0].fol = v->tq[1].fol = 1;
	if ((o = xv_obj(p, 0, "snapshot", JSON_OBJECT)) >= 0)
		xv_snapshot(v, p, o);
	else if ((o = xv_obj(p, 0, "chapter", JSON_OBJECT)) >= 0)
		xv_chapter(v, p, o);
	else if ((o = xv_obj(p, 0, "ref", JSON_OBJECT)) >= 0)
		xv_ref(v, p, o);
	else if ((o = xv_obj(p, 0, "evict", JSON_OBJECT)) >= 0)
	{
		long long s = xv_num(p, o, "seq", 0);

		for (k = j = 0; k < v->nch; k++)
			if (v->ch[k].offset < s)
				xv_ch_free(&v->ch[k]);
			else
				v->ch[j++] = v->ch[k];
		v->nch = j;
	}
	else if ((o = xv_obj(p, 0, "reset", JSON_OBJECT)) >= 0)
	{
		char *why = xv_str(p, o, "why");

		/* rotated, shrunk, cleared: everything held goes now, cards
		** included; full, evicted and restarted (the same session
		** under a new master image): kept until the snapshot */
		if (strcmp(why, "full") && strcmp(why, "evicted") &&
		    strcmp(why, "restarted"))
		{
			xv_drop(v);
			while (v->mode != XV_LIST)
				xv_back(v);
			snprintf(v->status, sizeof v->status, "X-ray reset (%s)", why);
		}
		xv_sfree(why);
	}
	else
		(void)xv_tfline(v, p);
}

/* ---- the card (--ref, spec 3.4) ------------------------------------- */

static void
xv_def_free(struct xv_def *d)
{
	xv_sfree(d->session);
	xv_sfree(d->title);
	xv_sfree(d->passage);
	xv_sfree(d->rule);
	xv_sfree(d->url);
	xv_sfree(d->status);
}

static void
xv_card_free(struct xv *v)
{
	int k;

	for (k = 0; k < v->nd; k++)
		xv_def_free(&v->d[k]);
	for (k = 0; k < v->nm; k++)
	{
		xv_sfree(v->m[k].session);
		xv_sfree(v->m[k].line);
		xv_sfree(v->m[k].chapter);
		xv_sfree(v->m[k].who);
		xv_sfree(v->m[k].tool);
		xv_sfree(v->m[k].sent);
		xv_sfree(v->m[k].ttitle);
		xv_sfree(v->m[k].sid);
	}
	v->nd = v->nm = 0;
	xv_sfree(v->vkind);
	xv_sfree(v->vtitle);
	xv_sfree(v->vsub);
	xv_sfree(v->cwarn);
	v->vkind = v->vtitle = v->vsub = v->cwarn = NULL;
}

/* One session: the same id, or with no id the same name (#493) */
static int
xv_ses(const struct xv_men *a, const struct xv_men *b)
{
	if (a->sid[0] && b->sid[0])
		return !strcmp(a->sid, b->sid);
	return !strcmp(a->session, b->session);
}

/* A shell session's light card shows titles and counts only (S18), and a
** light card no other session's line (K4.2): the engine sends none, and
** the view keeps none either. */
static int
xv_lines_ok(const struct xv *v, const char *session)
{
	return v->full || (strcmp(v->corpus ? v->corpus : "", "shell") &&
	                   session && v->session && !strcmp(session, v->session));
}

static void
xv_counts(struct xv *v, const struct json_parser *p, int o)
{
	static const char *const f[5] = { "session", "project",
		"project_sessions", "all", "all_sessions" };
	int c = xv_obj(p, o, "counts", JSON_OBJECT), k;

	for (k = 0; c >= 0 && k < 5; k++)
		v->cnt[k] = xv_num(p, c, f[k], -1);
}

static void
xv_cline(struct xv *v, const struct json_parser *p)
{
	char *s;
	int o, lead = 0;

	if ((o = xv_obj(p, 0, "definition", JSON_OBJECT)) >= 0 ||
	    (lead = (o = xv_obj(p, 0, "lead", JSON_OBJECT)) >= 0))
	{
		struct xv_def d;

		/* #575: the lead-in shown while define_command runs; the first
		** definition after it (the command's, else the lead-in's) replaces
		** it */
		if (v->nd && v->d[v->nd - 1].lead)
			xv_def_free(&v->d[--v->nd]);
		if (xv_grow(&v->d, &v->ad, v->nd, sizeof d) < 0)
			return;
		d.session = xv_str(p, o, "session");
		d.title = xv_str(p, o, "title");
		d.passage = xv_str(p, o, "passage");
		d.rule = xv_str(p, o, "rule");
		d.at = xv_num(p, o, "at", -1);
		d.url = xv_str(p, o, "url");
		d.status = xv_str(p, o, "status");
		d.lead = lead;
		d.cmd = !strcmp((s = xv_str(p, o, "source")), "command");
		xv_sfree(s);
		/* a define_command's body is no transcript line: always shown */
		if (!d.cmd && !xv_lines_ok(v, d.session))
		{
			xv_sfree(d.passage);
			d.passage = xv_none;
		}
		v->d[v->nd++] = d;
	}
	else if ((o = xv_obj(p, 0, "mention", JSON_OBJECT)) >= 0)
	{
		struct xv_men m;
		long long off = xv_num(p, o, "offset", -1);
		int i;

		if (v->nm >= XV_MENTIONS ||
		    xv_grow(&v->m, &v->am, v->nm, sizeof m) < 0)
			return;
		m.session = xv_str(p, o, "session");
		/* #479: this project's other sessions' lines, light too; a
		** remote view keeps titles and counts only for them */
		m.same = !v->hl && dch_json_field(p, o, "last_active") >= 0 &&
		         strcmp(v->corpus ? v->corpus : "", "shell");
		if (!m.same && !xv_lines_ok(v, m.session))
		{
			xv_sfree(m.session);
			return;
		}
		m.line = xv_str(p, o, "line");
		m.chapter = xv_str(p, o, "chapter");
		m.who = xv_str(p, o, "who");
		m.tool = xv_str(p, o, "tool");
		m.read = (i = dch_json_field(p, o, "read")) >= 0 &&
		         p->tok[i].kind == JSON_STRING;
		m.sent = xv_str(p, o, "sentence");
		m.ss = m.se = -1;
		if ((i = xv_obj(p, o, "span", JSON_ARRAY)) >= 0 && i + 2 < p->ntok &&
		    p->tok[i + 1].parent == i && p->tok[i + 2].parent == i &&
		    isdigit((unsigned char)p->text[p->tok[i + 1].start]) &&
		    isdigit((unsigned char)p->text[p->tok[i + 2].start]))
		{
			long long a = strtoll(p->text + p->tok[i + 1].start, NULL, 10);
			long long e = strtoll(p->text + p->tok[i + 2].start, NULL, 10);

			/* whole bytes of sent only: a hostile host's span is no index */
			if (a >= 0 && a <= e && e <= (long long)strlen(m.sent))
				m.ss = (int)a, m.se = (int)e;
		}
		/* light reads newest first, a window at a time (#262): a line
		** before its window's first prompt has no chapter; this
		** session's is the last one that starts before it */
		if (!m.chapter[0] && v->session && !strcmp(m.session, v->session))
		{
			int c, best = -1;
			char *t;

			for (c = 0; c < v->nch; c++)
				if (v->ch[c].offset >= 0 &&
				    v->ch[c].offset <= off && (best < 0 ||
				    v->ch[c].offset > v->ch[best].offset))
					best = c;
			if (best >= 0 && (t = strdup(v->ch[best].anchor)))
			{
				xv_sfree(m.chapter);
				m.chapter = t;
			}
		}
		m.at = xv_num(p, o, "at", -1);
		m.off = off;
		m.seen = xv_num(p, o, "last_active", -1);
		m.turn = xv_num(p, o, "turn", 0);
		m.ttitle = xv_str(p, o, "turn_title");
		m.sid = xv_str(p, o, "session_id");
		m.live = xv_bool(p, o, "live");
		/* newest first within a file (#304): above its file's older
		** lines, so light's windows (newest first, each in file order)
		** and --full (oldest first) both list newest first; files keep
		** the order sent, a host that sends no offset arrival order */
		for (i = v->nm; i > 0 && off >= 0 && v->m[i - 1].off >= 0 &&
		     v->m[i - 1].off < off && xv_ses(&v->m[i - 1], &m);
		     i--)
			;
		memmove(v->m + i + 1, v->m + i, (size_t)(v->nm - i) * sizeof m);
		v->m[i] = m;
		v->nm++;
		if (v->cses && v->coff == off && !strcmp(v->cses, m.session))
			v->csel = i;
		else if (v->cses && i <= v->csel)
			v->csel++;
	}
	else if ((o = xv_obj(p, 0, "resolved", JSON_OBJECT)) >= 0 &&
	         !v->vkind)
	{
		v->vkind = xv_str(p, o, "kind");
		v->vtitle = xv_str(p, o, "title");
		v->vsub = xv_str(p, o, "subtitle");
	}
	else if ((o = xv_obj(p, 0, "progress", JSON_OBJECT)) >= 0)
		xv_counts(v, p, o);
	else if (xv_bool(p, 0, "done"))
	{
		xv_counts(v, p, 0);
		v->cdone = 1;
		v->cpartial = xv_bool(p, 0, "partial");
		xv_sfree(v->cwarn);
		v->cwarn = xv_str(p, 0, "define_warn");
	}
}

/* The card's selection follows its mention as lines arrive (#304), also
** through f and p's new scan; on the first row it stays there (the newest
** under it), as Refs' does. */
static void
xv_cpin(struct xv *v)
{
	if (v->csel >= v->nm)
		v->csel = v->nm - 1;
	if (v->csel < 0)
		v->csel = 0;
	free(v->cses);
	v->cses = v->csel > 0 && v->m[v->csel].off >= 0
	          ? strdup(v->m[v->csel].session) : NULL;
	v->coff = v->cses ? v->m[v->csel].off : -1;
}

/* The card of ref text (key: its index key), scanned light for the
** project, or full (scope 1 project, 2 all). ref, when not NULL or "", is
** what --ref sends (define_command's ref as written, #415); text stays the
** card's header, copy and highlight. key == v->ckey (a re-scan) keeps
** all three, so v->cref may be passed back in. */
static void
xv_card(struct xv *v, const char *key, const char *text, const char *ref,
        int full, int scope)
{
	char *av[8];
	int ac = 0, k;

	if (key != v->ckey)
	{
		xv_sfree(v->ckey);
		xv_sfree(v->ctext);
		xv_sfree(v->cref);
		v->ckey = strdup(key);
		v->ctext = strdup(text);
		/* a term asks by its key: a bare name or bold phrase is no ref */
		v->cref = strdup(ref && ref[0] ? ref
		                 : !strncmp(key, "term:", 5) ? key : text);
		if (!v->ckey || !v->ctext || !v->cref)
		{
			free(v->ckey);
			free(v->ctext);
			free(v->cref);
			v->ckey = v->ctext = v->cref = NULL;
			snprintf(v->status, sizeof v->status, "X-ray: out of memory");
			return;
		}
		v->csel = v->ctop = v->chdr = 0;
		free(v->cses);
		v->cses = NULL;
	}
	xv_card_free(v);
	for (k = 0; k < 5; k++)
		v->cnt[k] = -1;
	v->full = full;
	v->scope = scope;
	v->cdone = v->cpartial = 0;
	if (v->mode != XV_CARD)
	{
		v->back = v->mode;
		v->mode = XV_CARD;
	}
	av[ac++] = v->n;
	av[ac++] = "--ref";
	av[ac++] = v->cref;
	if (scope == 2)
	{
		av[ac++] = "--scope";
		av[ac++] = "all";
	}
	if (full)
		av[ac++] = "--full";
	av[ac++] = "--json";
	av[ac] = NULL;
	(void)xv_start(v, XV_CK, av, ac, xv_cline);
}

/* The filter's text is a ref (#263) by --ref's own value rules (3.1), so
** what passes here passes there. 1 with t its text, outer blanks cut. */
static int
xv_fref(const struct xv *v, char *t, size_t sz)
{
	struct xr_span sp;
	size_t n;

	if (!v->filt[0] || !xr_ref_ok(v->filt) ||
	    !xr_ref_parse(v->filt, NULL, &sp))
		return 0;
	snprintf(t, sz, "%s", v->filt + strspn(v->filt, " "));
	for (n = strlen(t); n && t[n - 1] == ' ';)
		t[--n] = '\0';
	return 1;
}

/* ---- Search (--search, #291) --------------------------------------- */

static void
xv_hits_free(struct xv *v)
{
	int k;

	for (k = 0; k < v->nh; k++)
	{
		xv_sfree(v->hit[k].chapter);
		xv_sfree(v->hit[k].who);
		xv_sfree(v->hit[k].tool);
		xv_sfree(v->hit[k].snippet);
	}
	v->nh = v->sdone = v->smore = v->spart = 0;
	xv_sfree(v->swhy);
	v->swhy = NULL;
	v->sline[0] = '\0';
}

static int xv_rch(const struct xv *v, const char *a);

/* Two hits under one turn: a hit under no chapter is a turn of its own */
static int
xv_hsame(const struct xv_hit *a, const struct xv_hit *b)
{
	return a->chapter[0] && !strcmp(a->chapter, b->chapter);
}

/* A turn's title as its rows show it */
static const char *
xv_ctitle(const struct xv_ch *c)
{
	return c->aut && c->summary[0] ? c->summary : c->title;
}

/* The hits by turn (#457): the turns newest first, each one's hits
** together, a You hit that is its title first as the head, the rest as
** sent. Once per answer: the chapters shown then name the titles.
** shortcut: a turn the index has not sent yet gets no head; fine while the
** index comes before the first search. */
static void
xv_hgroup(struct xv *v)
{
	struct xv_hit *o;
	int k, j, pass, n = 0, c;

	for (k = 0; k < v->nh; k++)
	{
		const struct xv_hit *h = &v->hit[k];

		v->hit[k].head = !strcmp(h->who, "user") &&
		                 (c = xv_rch(v, h->chapter)) >= 0 &&
		                 !strcmp(h->snippet, xv_ctitle(&v->ch[c]));
	}
	if (v->nh < 2 || !(o = malloc((size_t)v->nh * sizeof *o)))
		return;
	for (k = 0; k < v->nh; k++)
	{
		for (j = 0; j < k && !xv_hsame(&v->hit[j], &v->hit[k]); j++)
			;
		for (pass = j < k ? 2 : 0; pass < 2; pass++)
			for (j = k; j < v->nh; j++)
				if ((j == k || xv_hsame(&v->hit[j], &v->hit[k])) &&
				    v->hit[j].head == !pass)
					o[n++] = v->hit[j];
	}
	memcpy(v->hit, o, (size_t)n * sizeof *o);
	free(o);
}

/* The turns of the search's hits */
static int
xv_hturns(const struct xv *v)
{
	int k, n = 0;

	for (k = 0; k < v->nh; k++)
		n += !k || !xv_hsame(&v->hit[k], &v->hit[k - 1]);
	return n;
}

/* The search's one answer: its hits, newest first as sent */
static void
xv_qline(struct xv *v, const struct json_parser *p)
{
	int a, t;

	if (v->sdone || (a = xv_obj(p, 0, "hits", JSON_ARRAY)) < 0)
		return;
	for (t = a + 1; t < p->tok[a].next; t = p->tok[t].next)
	{
		struct xv_hit h;

		if (p->tok[t].kind != JSON_OBJECT ||
		    xv_grow(&v->hit, &v->ah, v->nh, sizeof h) < 0)
			continue;
		h.chapter = xv_str(p, t, "chapter");
		h.who = xv_str(p, t, "who");
		h.tool = xv_str(p, t, "tool");
		h.snippet = xv_str(p, t, "snippet");
		h.at = xv_num(p, t, "at", -1);
		h.off = xv_num(p, t, "offset", -1);
		v->hit[v->nh++] = h;
	}
	xv_hgroup(v);
	v->smore = xv_bool(p, 0, "more");
	v->spart = xv_bool(p, 0, "partial");
	v->swhy = xv_str(p, 0, "why");
	v->sdone = 1;
}

/* Search text t on the Search tab, the last hits gone; a text the engine
** would refuse is said, nothing spawned */
static void
xv_search(struct xv *v, const char *t)
{
	char *av[5];

	if (!xray_search_ok(t))
	{
		snprintf(v->status, sizeof v->status, "bad search: 2 to 200 "
		         "bytes, no control character");
		return;
	}
	v->status[0] = '\0';	/* an earlier refusal's (LOW-V18) */
	snprintf(v->sq, sizeof v->sq, "%s", t);
	snprintf(v->q, sizeof v->q, "%s", v->sq);
	xv_hits_free(v);
	v->sel[3] = v->top[3] = 0;
	v->tab = 3;
	v->filtering = 0;
	av[0] = v->n;
	av[1] = "--search";
	av[2] = v->sq;
	av[3] = "--json";
	av[4] = NULL;
	(void)xv_start(v, XV_QK, av, 4, xv_qline);
}

/* The search's child ended. An older host's refusal and a session with no
** corpus are the list's line, any other failure the status's. */
static void
xv_qend(struct xv *v)
{
	const struct xv_kid *k = &v->kid[XV_QK];
	const char *e = k->err[0] ? k->err : "X-ray: the search failed";

	if (!k->rc && v->sdone)
		return;
	if (strstr(e, "does not know --search") || strstr(e, "no x-ray corpus"))
		snprintf(v->sline, sizeof v->sline, "%s", e);
	else
		snprintf(v->status, sizeof v->status, "%s", e);
}

/* ^R: the card of the ref typed in the filter, held or not, its kind shown
** or hidden (#263); words: Search for them (#291) */
static void
xv_lookup(struct xv *v)
{
	char t[sizeof v->filt];

	if (!xv_fref(v, t, sizeof t))
	{
		if (v->filt[0])
			xv_search(v, v->filt);
		else
			snprintf(v->status, sizeof v->status, "^R looks up the "
			         "ref or searches the words typed after /");
		return;
	}
	v->filtering = 0;
	xv_card(v, t, t, NULL, 0, 1);
}

/* ---- Tasks (--tasks, #320) ------------------------------------------- */

static int xv_rtab(const struct xv *v);

static void
xv_task_free(struct xv_task *t)
{
	xv_sfree(t->id);
	xv_sfree(t->title);
	xv_sfree(t->label);
	xv_sfree(t->active);
	xv_sfree(t->note);
	xv_sfree(t->created);
	xv_sfree(t->chapter);
	xv_sfree(t->url);
	xv_sfree(t->group);
	xv_sfree(t->blocked);
	xv_sfree(t->path);
	xv_sfree(t->scope);
	xv_sfree(t->body);
	xv_sfree(t->cron);
	xv_sfree(t->kind);
	xv_sfree(t->stw);
}

static void
xv_tl_free(struct xv_tl *l)
{
	int k;

	for (k = 0; k < l->n; k++)
		xv_task_free(&l->t[k]);
	free(l->t);
	free(l->ord);
	xv_sfree(l->title);
	xv_sfree(l->warn);
	memset(l, 0, sizeof *l);
}

/* "2026-11-14T07:09:40Z" (UTC) to epoch seconds; -1 */
static long long
xv_iso(const char *s)
{
	struct tm tm;

	memset(&tm, 0, sizeof tm);
	if (sscanf(s, "%4d-%2d-%2dT%2d:%2d:%2d", &tm.tm_year, &tm.tm_mon,
	           &tm.tm_mday, &tm.tm_hour, &tm.tm_min, &tm.tm_sec) != 6)
		return -1;
	tm.tm_year -= 1900;
	tm.tm_mon--;
	return (long long)timegm(&tm);
}

/* blocked_by's ids, ", " between them (a long run cut), or xv_none */
static char *
xv_ids(const struct json_parser *p, int o)
{
	int a = xv_obj(p, o, "blocked_by", JSON_ARRAY), t;
	char b[200], one[64], *s;
	size_t l = 0;

	b[0] = '\0';
	for (t = a + 1; a >= 0 && t < p->tok[a].next; t = p->tok[t].next)
	{
		if (p->tok[t].kind == JSON_STRING)
		{
			if (dch_json_decode(p, t, one, sizeof one) < 0)
				continue;
		}
		else if (p->tok[t].kind == JSON_PRIMITIVE)
			snprintf(one, sizeof one, "%.*s",
			         p->tok[t].end - p->tok[t].start,
			         p->text + p->tok[t].start);
		else
			continue;
		l += (size_t)snprintf(b + l, sizeof b - l, "%s%s", l ? ", " : "",
		                      one);
		if (l >= sizeof b - 1)
			break;
	}
	return b[0] && (s = strdup(b)) ? s : xv_none;
}

/* "2026-11-14T07:09:40Z" field k of o to epoch seconds; -1 none */
static long long
xv_isof(const struct json_parser *p, int o, const char *k)
{
	char *s = xv_str(p, o, k);
	long long t = s[0] ? xv_iso(s) : -1;

	xv_sfree(s);
	return t;
}

/* Row o of list l (i: 0 a task's, 1 a memory's, 2 a schedule's), its text
** counted in l->bytes; t->seq unset */
static void
xv_trow(struct xv_tl *l, const struct json_parser *p, int o, int i,
        struct xv_task *t0)
{
	struct xv_task t;
	char *s;
	int mem = i == 1;

	t.id = xv_str(p, o, "id");
	t.title = xv_str(p, o, i == 2 ? "prompt" : "title");
	t.note = xv_str(p, o, i == 2 ? "reason" : "note");
	t.chapter = xv_str(p, o, "chapter");
	t.label = xv_str(p, o, mem ? "kind" : i == 2 ? "human" : "label");
	t.active = t.created = t.url = t.group = t.blocked = xv_none;
	t.path = t.scope = t.body = xv_none;
	t.cron = t.kind = t.stw = xv_none;
	t.next = t.lastf = t.expires = -1;
	t.fires = t.noop = 0;
	t.ses = t.cut = t.recur = 0;
	if (i == 2)
	{
		t.created = xv_str(p, o, "created");
		t.cron = xv_str(p, o, "cron");
		t.kind = xv_str(p, o, "kind");
		t.stw = xv_str(p, o, "status");
		t.next = xv_isof(p, o, "next");
		t.lastf = xv_isof(p, o, "last_fired");
		t.expires = xv_isof(p, o, "expires");
		t.fires = xv_num(p, o, "fires", 0);
		t.noop = xv_num(p, o, "noop_streak", 0);
		t.recur = xv_bool(p, o, "recurring");
		t.st = !strcmp(t.stw, "pending") ? 0 : !strcmp(t.stw, "fired")
		       ? 1 : 2;
	}
	else if (mem)
	{
		t.path = xv_str(p, o, "path");
		t.scope = xv_str(p, o, "scope");
		t.body = xv_str(p, o, "body");
		t.cut = xv_bool(p, o, "cut");
		if (l->bytes + strlen(t.body) > XV_MBODY)
		{
			xv_sfree(t.body);
			t.body = xv_none;
			t.cut = 2;
		}
		l->bytes += strlen(t.body);
		s = xv_str(p, o, "session");
		t.ses = !strcmp(s, "wrote") ? 1 : !strcmp(s, "loaded") ? 2 : 0;
		xv_sfree(s);
		t.st = t.ses ? 0 : !strcmp(t.label, "instructions") ? 2 : 1;
	}
	else
	{
		t.active = xv_str(p, o, "active");
		t.created = xv_str(p, o, "created");
		t.url = xv_str(p, o, "url");
		t.group = xv_str(p, o, "group");
		t.blocked = xv_ids(p, o);
		s = xv_str(p, o, "status");
		t.st = !strcmp(s, "active") ? 0 : !strcmp(s, "done") ? 2
		       : !strcmp(s, "dropped") ? 3 : 1;
		xv_sfree(s);
	}
	t.updated = xv_isof(p, o, "updated");
	*t0 = t;
}

/* One --tasks (tq[0]), --memory (tq[1]) or --schedules (tq[2]) line: the
** header, a row, the end (its warn too, #321). Rows past XV_TASKS are
** counted as dropped; a memory's text past XV_MBODY in all is not kept. */
static void
xv_lline(struct xv *v, struct xv_tq *q, const struct json_parser *p)
{
	static const char *const key[3] = { "task", "memory", "schedule" };
	struct xv_tl *l = q->re ? &q->tn : &q->tl;
	int i = (int)(q - v->tq);
	struct xv_task t;
	char *s;
	int o;

	if ((o = xv_obj(p, 0, key[i], JSON_OBJECT)) < 0)
	{
		if (xv_num(p, 0, "schema", -1) >= 0)
		{
			xv_sfree(l->title);
			xv_sfree(l->warn);
			l->title = xv_str(p, 0, "title");
			l->warn = xv_str(p, 0, "warn");
			l->partial = xv_bool(p, 0, "partial");
		}
		else if (xv_bool(p, 0, "done"))
		{
			l->done = 1;
			l->dropped += (int)xv_num(p, 0, "dropped", 0);
			if ((s = xv_str(p, 0, "warn"))[0])
			{
				xv_sfree(l->warn);
				l->warn = s;
			}
			else
				xv_sfree(s);
		}
		return;
	}
	if (l->n >= XV_TASKS)
	{
		l->dropped++;
		return;
	}
	if (xv_grow(&l->t, &l->a, l->n, sizeof t) < 0)
		return;
	xv_trow(l, p, o, i, &t);
	t.seq = l->hi++;
	l->t[l->n++] = t;
	l->nst[t.st]++;
	if (l == &q->tl)
		q->sort = 1;
}

static void
xv_tline(struct xv *v, const struct json_parser *p)
{
	xv_lline(v, &v->tq[0], p);
}

static void
xv_mline(struct xv *v, const struct json_parser *p)
{
	xv_lline(v, &v->tq[1], p);
}

static void
xv_hline(struct xv *v, const struct json_parser *p)
{
	xv_lline(v, &v->tq[2], p);
}

static const struct xv_task *xv_sort_tk;
static int xv_sort_mem;

/* In progress, Pending, Done, Dropped; the first two as sent, the last two
** newest first (a row with no time after the dated ones, as sent). A
** memory list: its groups, each as sent (the engine's order) */
static int
xv_task_cmp(const void *pa, const void *pb)
{
	const struct xv_task *a = &xv_sort_tk[*(const int *)pa];
	const struct xv_task *b = &xv_sort_tk[*(const int *)pb];

	if (a->st != b->st)
		return a->st - b->st;
	if (!xv_sort_mem && a->st >= 2 && a->updated != b->updated)
		return a->updated > b->updated ? -1 : 1;
	return a->seq - b->seq;
}

/* l's order, made once per answer (and while it streams, every 100 ms):
** one sort. 0, -1 no memory (the last order kept). */
static int
xv_tord(struct xv_tl *l, int mem)
{
	int k, *o;

	if (l->n > l->aord)
	{
		if (!(o = realloc(l->ord, (size_t)l->a * sizeof *o)))
			return -1;
		l->ord = o;
		l->aord = l->a;
	}
	for (k = 0; k < l->n; k++)
		l->ord[k] = k;
	xv_sort_tk = l->t;
	xv_sort_mem = mem;
	qsort(l->ord, (size_t)l->n, sizeof *l->ord, xv_task_cmp);
	l->nord = l->n;
	return 0;
}

/* Tasks (i 0), Memory (1) or Schedules (2) is a tab: its list has a row */
static int
xv_ttab(const struct xv *v, int i)
{
	return v->tq[i].tl.n > 0;
}

/* List i is still coming: its child runs, or the follow is sending it */
static int
xv_tbusy(const struct xv *v, int i)
{
	return v->poll >= (i == 2 ? 1 : 2) ? v->kid[XV_TK + i].pid > 0
	               : v->blk == i && v->kid[XV_FK].pid > 0;
}

/* A whole answer for list i begins: the first straight into the list shown
** (rows drawn as they come), a later one into tn until its end */
static void
xv_tbeg(struct xv *v, int i)
{
	struct xv_tq *q = &v->tq[i];

	xv_tl_free(&q->tn);
	if (!(q->re = xv_ttab(v, i)))
		xv_tl_free(&q->tl);
}

/* `--tasks --json` (i 0) or `--memory --json` (1) in XV_TK + i, for a host
** whose follow carries neither; `--schedules --json` (2) once, for a host
** whose follow carries no schedules (#687) */
static void
xv_tasks(struct xv *v, int i)
{
	struct xv_tq *q = &v->tq[i];
	char *av[4];

	if (v->kid[XV_TK + i].pid > 0 || q->no)
		return;
	xv_tbeg(v, i);
	q->at = now_ms();
	q->fol = 0;
	av[0] = v->n;
	av[1] = i == 2 ? "--schedules" : i ? "--memory" : "--tasks";
	av[2] = "--json";
	av[3] = NULL;
	if (xv_start(v, XV_TK + i, av, 3, i == 2 ? xv_hline : i ? xv_mline
	             : xv_tline) == 0)
		v->kid[XV_TK + i].lmax = XV_TLINE;
}

/* List i's answer ended (rc, err: its child's; the follow's block 0, "").
** A dch or host that knows no --tasks, --memory or --schedules (a usage
** refusal, an older host's): never asked again, and no tab; any other
** failure keeps what is shown. A later whole answer replaces the list; a card (or the
** reader opened from it) stays on its row, or goes when the row has. */
static void
xv_tend(struct xv *v, int i, int rc, const char *err)
{
	struct xv_tq *q = &v->tq[i];
	int j;

	if (rc == 2 || strstr(err, "does not know"))
		q->no = 1;
	q->sort = 1;
	if (!q->re)
		return;
	q->re = 0;
	if (rc || !q->tn.done)
	{
		xv_tl_free(&q->tn);
		return;
	}
	xv_tl_free(&q->tl);
	q->tl = q->tn;
	memset(&q->tn, 0, sizeof q->tn);
	if (v->tab != 4 + i || (v->mode != XV_TCARD && v->back != XV_TCARD))
		return;
	for (j = 0; q->sel && j < q->tl.n && strcmp(q->tl.t[j].id, q->sel);
	     j++)
		;
	if (!q->sel || j == q->tl.n)
	{
		if (v->mode == XV_TCARD)
			v->mode = XV_LIST;
		else
			v->back = XV_LIST;
	}
	q->ci = j;
}

/* The follow's change to list i (#448): row o, keyed by id, replaces its
** row or is added; "gone" removes it. A changed memory is the newest of its
** group, as the engine orders them; a task keeps its place. Sorted at once:
** vis and the card's row hold indexes into the rows. */
static void
xv_tdelta(struct xv *v, int i, const struct json_parser *p, int o)
{
	struct xv_tq *q = &v->tq[i];
	struct xv_tl *l = &q->tl;
	struct xv_task t;
	char *id = xv_str(p, o, "id");
	int k, seq;

	for (k = 0; k < l->n && strcmp(l->t[k].id, id); k++)
		;
	xv_sfree(id);
	seq = k < l->n ? l->t[k].seq : l->hi;
	if (xv_bool(p, o, "gone"))
	{
		if (k == l->n)
			return;
		l->nst[l->t[k].st]--;
		l->bytes -= strlen(l->t[k].body);
		xv_task_free(&l->t[k]);
		memmove(&l->t[k], &l->t[k + 1], (size_t)(--l->n - k) * sizeof t);
		/* the card on it goes back to the list (or the reader opened
		** from it, back to the list) */
		if (q->ci == k && v->tab == 4 + i && v->mode == XV_TCARD)
			v->mode = XV_LIST;
		else if (q->ci == k && v->tab == 4 + i && v->back == XV_TCARD)
			v->back = XV_LIST;
		else if (q->ci > k)
			q->ci--;
	}
	else
	{
		if (k == l->n && (l->n >= XV_TASKS ||
		                  xv_grow(&l->t, &l->a, l->n, sizeof t) < 0))
			return;
		if (k < l->n)
		{
			l->nst[l->t[k].st]--;
			l->bytes -= strlen(l->t[k].body);
			xv_task_free(&l->t[k]);
		}
		else
		{
			l->n++;
			l->hi++;
		}
		xv_trow(l, p, o, i, &t);
		t.seq = i == 1 ? --l->lo : seq;
		l->t[k] = t;
		l->nst[t.st]++;
	}
	q->sort = 0;
	q->sat = now_ms();
	q->gen++;
	if (xv_tord(l, i) < 0)
		l->nord = 0;	/* no order: no row shown until the next */
}

/* A --follow line of Tasks or Memory (#448): a block's header, rows and
** done line, as the one-shot's, or a change between blocks. 1 when it was
** one. */
static int
xv_tfline(struct xv *v, const struct json_parser *p)
{
	int o = -1, i, end = 0;
	char *b;

	if (xv_num(p, 0, "schema", -1) >= 0)
	{
		/* a tasks header says its session; a memory one never; a
		** schedules one its block (#686) */
		b = xv_str(p, 0, "block");
		i = v->blk = !strcmp(b, "schedules") ? 2
		             : dch_json_field(p, 0, "session") >= 0 ? 0 : 1;
		xv_sfree(b);
		xv_tbeg(v, i);
	}
	else if (v->blk >= 0 && xv_bool(p, 0, "done"))
	{
		i = v->blk;
		v->blk = -1;
		end = 1;
	}
	else if ((o = xv_obj(p, 0, "task", JSON_OBJECT)) >= 0)
		i = 0;
	else if ((o = xv_obj(p, 0, "memory", JSON_OBJECT)) >= 0)
		i = 1;
	else if ((o = xv_obj(p, 0, "schedule", JSON_OBJECT)) >= 0)
		i = 2;
	else
		return 0;
	/* applied on a shell's snapshot too: the block after it replaces what
	** a Claude that left it showed (live false: its crons ended), and a
	** shell that never had one gets empty blocks, so no tab (MED-XA1) */
	if (o >= 0 && v->blk < 0)
		xv_tdelta(v, i, p, o);
	else if (i == v->blk || end)
		xv_lline(v, &v->tq[i], p);
	if (end)
		xv_tend(v, i, 0, "");
	return 1;
}

/* Tasks' or Memory's rows (vis), filtered over id, title, active text,
** note, label, and a memory's path and text: made again only when the
** order or the filter changed; the selection kept on its row once a key
** has moved it. tg: each group's first row. */
static void
xv_tvis(struct xv *v)
{
	struct xv_tq *q = &v->tq[v->tab - 4];
	const struct xv_tl *l = &q->tl;
	const char *f = v->filt;
	int k, g;

	v->ntmp = v->nauto = v->npeer = v->nk = 0;
	if (q->sort && (!xv_tbusy(v, v->tab - 4) ||
	                now_ms() - q->sat >= 100) &&
	    !xv_tord(&q->tl, v->tab - 4))
	{
		q->sort = 0;
		q->sat = now_ms();
		q->gen++;
	}
	if (v->vtab == v->tab && v->tvgen == q->gen && !strcmp(v->tvf, f))
		return;
	v->vtab = v->tab;
	v->tvgen = q->gen;
	snprintf(v->tvf, sizeof v->tvf, "%s", f);
	v->nvis = 0;
	for (g = 0; g < 4; g++)
		v->tg[g] = -1;
	for (k = 0; k < l->nord; k++)
	{
		const struct xv_task *t = &l->t[l->ord[k]];

		if (f[0] && !xv_has(t->id, f) && !xv_has(t->title, f) &&
		    !xv_has(t->active, f) && !xv_has(t->note, f) &&
		    !xv_has(t->label, f) && !xv_has(t->path, f) &&
		    !xv_has(t->body, f) && !xv_has(t->cron, f))
			continue;
		if (xv_grow(&v->vis, &v->avis, v->nvis, sizeof *v->vis) < 0)
			break;
		if (v->tg[t->st] < 0)
			v->tg[t->st] = v->nvis;
		v->vis[v->nvis++] = l->ord[k];
	}
	for (k = 0; q->sel && k < v->nvis; k++)
		if (!strcmp(l->t[v->vis[k]].id, q->sel))
		{
			v->sel[v->tab] = k;
			break;
		}
}

/* ---- the reader (--chapter, spec 3.5) ------------------------------- */

static void
xv_read_free(struct xv *v)
{
	int k;

	for (k = 0; k < v->ne; k++)
	{
		xv_sfree(v->e[k].role);
		xv_sfree(v->e[k].tool);
		xv_sfree(v->e[k].text);
	}
	v->ne = v->nrl = 0;
	v->rcur = -1;
	xv_sfree(v->rtitle);
	v->rtitle = NULL;
	v->rloaded = v->rtop = 0;
}

/* Entry n is of the Search hit's kind: its who's role, a tool's call or
** output. */
static int
xv_kind(const struct xv *v, int n)
{
	const char *r = v->e[n].role;

	return v->rwho == 't' ? !strcmp(r, "tool") || !strcmp(r, "output")
	       : r[0] == v->rwho;
}

/* Entry n could hold the Search hit's twin past its cut (LOW-V33): of the
** hit's kind, its text at the engine's cap (xray.h). */
static int
xv_cut(const struct xv *v, int n)
{
	const struct xv_ent *e = &v->e[n];
	const unsigned char *c;
	size_t k = 0;
	int out = !strcmp(e->role, "output");

	if (!xv_kind(v, n))
		return 0;
	if (v->rtool && *v->rtool && *e->tool && strcmp(v->rtool, e->tool))
		return 0;
	for (c = (const unsigned char *)e->text; *c; c++)
		k += (*c & 0xc0) != 0x80;
	return k >= (out ? XR_OUTPUT_CAP : XR_TEXT_CAP);
}

/* The entries as lines of w cells: a head per entry, its text wrapped. */
static void
xv_wrap(struct xv *v, int w)
{
	struct xv_rl l;
	const char *p;
	int k, at = -1, skip, held, cut, c, te, n;

	v->nrl = 0;
	v->rw = w;
	if (w < 4)
		return;
	for (k = 0; k < v->ne; k++)
	{
		const char *s = v->e[k].text;
		int out = !strcmp(v->e[k].role, "output");

		l.s = v->e[k].role;
		l.n = 0;
		l.kind = 0;
		if (xv_grow(&v->rl, &v->arl, v->nrl, sizeof l) < 0)
			return;
		l.n = (size_t)k;	/* a head: n is its entry */
		v->rl[v->nrl++] = l;
		for (;;)
		{
			size_t ll = strcspn(s, "\n"), f;
			int mark = v->e[k].cut && !strncmp(s, "\xe2\x80\xa6 ", 4) &&
			           ll > 23 && !strncmp(s + ll - 19, " characters cut \xe2\x80\xa6", 19);

			do
			{
				f = xv_brk(s, ll, w);
				if (!f && ll)
					f = 1;
				if (xv_grow(&v->rl, &v->arl, v->nrl, sizeof l) < 0)
					return;
				l.s = s;
				l.n = f;
				l.kind = out || mark ? 2 : 1;
				v->rl[v->nrl++] = l;
				s += f;
				ll -= f;
				if (ll && *s == ' ')	/* the break's space */
					s++, ll--;
			} while (ll);
			if (!*s)
				break;
			s++;
		}
	}
	/* opened on a Search hit (MED-V21): in the entry holding its snippet
	** (an older host's entries carry no offset), at the first line from
	** there on holding the text, else the snippet's own line */
	/* a twin whose snippet lies past an entry's cut is not held
	** (LOW-V33): when the cut entries of the hit's kind are exactly the
	** missing twins, they count as twins too, in entry order */
	for (k = held = cut = 0; v->rsnip && !v->rloaded && k < v->ne; k++)
		if (xv_snipat(v->e[k].text, v->rsnip))
			held++;
		else
			cut += xv_cut(v, k);
	cut = held < v->rtwins && cut == v->rtwins - held;
	skip = v->rskip;
	/* entries that say their record's offset (#356): the hit's is the
	** entry of its kind at its offset, one holding the snippet first; the
	** count above is for a host that sends none */
	for (k = 0, te = -1; v->rsnip && !v->rloaded && v->roff >= 0 &&
	     k < v->ne; k++)
		if (v->e[k].off == v->roff && xv_kind(v, k) &&
		    !(v->rtool && *v->rtool && *v->e[k].tool &&
		      strcmp(v->rtool, v->e[k].tool)) &&
		    (te < 0 || (!xv_snipat(v->e[te].text, v->rsnip) &&
		                xv_snipat(v->e[k].text, v->rsnip))))
			te = k;
	for (k = 0, p = NULL; v->rsnip && !v->rloaded && k < v->nrl; k++)
		if (!v->rl[k].kind && at >= 0)
			break;
		else if (!v->rl[k].kind)
		{
			n = (int)v->rl[k].n;
			p = te < 0 || n == te ? xv_snipat(v->e[n].text, v->rsnip)
			    : NULL;
			c = te < 0 ? !p && cut && xv_cut(v, n) : n == te && !p;
			if (te < 0 && (p || c) && skip-- > 0)
				p = NULL;	/* an earlier hit's (LOW-V29) */
			else if (c)
				at = k;	/* the hit's, cut before it: its start */
		}
		else if (p && v->rl[k].s + v->rl[k].n > p &&
		         (at < 0 || xv_in(v->rl[k].s, v->rl[k].n, v->rwant)))
		{
			at = k;
			if (xv_in(v->rl[k].s, v->rl[k].n, v->rwant))
				break;
		}
	if (at >= 0)
		v->rtop = at > 2 ? at - 2 : 0;
	/* opened from a mention: at the ref's first line */
	else if (v->rwant && !v->rloaded)
		for (k = 0; k < v->nrl; k++)
			if (v->rl[k].kind && xv_in(v->rl[k].s, v->rl[k].n, v->rwant))
			{
				v->rtop = k > 2 ? k - 2 : 0;
				break;
			}
	if (v->rend && !v->rloaded)
		v->rtop = v->nrl;	/* xv_draw_read clamps */
	/* opened on an answer (#290): at the last reply holding its start
	** (entries carry no offset), else at the end */
	for (k = v->nrl - 1; v->rans && !v->rloaded && k >= 0; k--)
		if (!v->rl[k].kind && !strcmp(v->e[v->rl[k].n].role, "assistant")
		    && strstr(v->e[v->rl[k].n].text, v->rans))
		{
			v->rtop = k;
			break;
		}
	v->rloaded = 1;
}

static void
xv_rline(struct xv *v, const struct json_parser *p)
{
	int a, t;
	char *c;

	if (v->rturn)
	{
		/* a turn's chapter came: it is the one shown, from its edge */
		free(v->ranchor);
		v->ranchor = v->rturn;
		v->rturn = NULL;
		xv_sfree(v->rwant);
		v->rwant = NULL;
		free(v->rans);
		v->rans = NULL;
		free(v->rsnip);
		v->rsnip = NULL;
		free(v->rtool);
		v->rtool = NULL;
	}
	xv_read_free(v);
	v->rtitle = xv_str(p, 0, "title");
	v->rat = xv_num(p, 0, "at", -1);
	v->rtrunc = xv_bool(p, 0, "truncated");
	if ((a = xv_obj(p, 0, "entries", JSON_ARRAY)) < 0)
		return;
	for (t = a + 1; t < p->tok[a].next; t = p->tok[t].next)
	{
		struct xv_ent e;

		if (p->tok[t].kind != JSON_OBJECT ||
		    xv_grow(&v->e, &v->ae, v->ne, sizeof e) < 0)
			continue;
		e.role = xv_str(p, t, "role");
		e.tool = xv_str(p, t, "tool");
		e.text = xv_str(p, t, "text");
		e.error = xv_bool(p, t, "error");
		e.cut = xv_num(p, t, "cut", 0) > 0;
		e.off = xv_num(p, t, "offset", -1);
		/* a tab or CR as a blank: the reader lays text out itself */
		for (c = e.text; *c; c++)
			if (*c == '\t' || *c == '\r')
				*c = ' ';
		v->e[v->ne++] = e;
	}
	xv_wrap(v, pk_w - 2);
}

/* --chapter anchor, its answer to xv_rline */
static void
xv_rquery(struct xv *v, const char *anchor)
{
	char *av[5], a[200];

	snprintf(a, sizeof a, "%s", anchor);
	av[0] = v->rn ? v->rn : v->n;
	av[1] = "--chapter";
	av[2] = a;
	av[3] = "--json";
	av[4] = NULL;
	(void)xv_start(v, XV_RK, av, 4, xv_rline);
}

/* The reader on chapter anchor; want: the text to open at, or NULL. */
static void
xv_read(struct xv *v, const char *anchor, const char *want)
{
	xv_read_free(v);
	xv_sfree(v->rwant);
	v->rwant = want ? strdup(want) : NULL;
	free(v->rans);
	v->rans = NULL;
	free(v->rsnip);
	v->rsnip = NULL;
	free(v->rtool);
	v->rtool = NULL;
	free(v->ranchor);
	v->ranchor = strdup(anchor);
	free(v->rturn);
	v->rturn = NULL;
	v->rend = v->redge = v->strip = 0;
	v->rq[0] = '\0';
	v->back = v->mode;
	v->mode = XV_READ;
	xv_rquery(v, anchor);
}

/* The index of the chapter with anchor a, or -1. */
static int
xv_rch(const struct xv *v, const char *a)
{
	int k;

	for (k = 0; a && k < v->nch; k++)
		if (!strcmp(v->ch[k].anchor, a))
			return k;
	return -1;
}

static int xv_hid(const struct xv_ch *c);

/* A message another agent session sent (#359): a peer that is not auto
** (an auto one is a subagent's hand-back) */
static int
xv_xs(const struct xv_ch *c)
{
	return c->peer != xv_none && !c->aut;
}

/* The chapter d (1 next, -1 previous) from the reader's, or the one it is
** turning to: the Chapters list's order, skipping the auto rows while n
** hides them there. -1: none. */
static int
xv_next(const struct xv *v, int d)
{
	int k = xv_rch(v, v->rturn ? v->rturn : v->ranchor);

	if (k < 0)
		return -1;
	for (k += d; k >= 0 && k < v->nch; k += d)
		if ((v->allauto || !xv_hid(&v->ch[k])) &&
		    !(v->hidepeer && xv_xs(&v->ch[k])))
			return k;
	return -1;
}

/* ] and [, and a scroll past an end: the reader to the next chapter, at
** its top, or the previous one, at its top (end: at its end). The chapter
** shown stays until the new one comes, and stays if it does not. */
static void
xv_turn(struct xv *v, int d, int end)
{
	int k = xv_next(v, d);
	char *a;

	if (k < 0)
	{
		snprintf(v->status, sizeof v->status, "no %s %s", d > 0
		         ? "next" : "previous", xv_noun(v));
		return;
	}
	if (!(a = strdup(v->ch[k].anchor)))
		return;
	free(v->rturn);
	v->rturn = a;
	v->rend = end;
	xv_rquery(v, a);
}

/* A scroll key, d rows (+ down, - up). At the chapter's end that way the
** first press says what comes next, the same way again turns to it; e: the
** end the last key said. */
static void
xv_scroll(struct xv *v, int d, int e)
{
	int max = v->nrl - v->win, k;
	const struct xv_ch *c;

	if (max < 0)
		max = 0;
	if (!v->rloaded || (d > 0 ? v->rtop < max : v->rtop > 0))
	{
		v->rtop += d;
		return;
	}
	if (e == (d > 0 ? 1 : -1))
	{
		xv_turn(v, e, e < 0);
		return;
	}
	if ((k = xv_next(v, d > 0 ? 1 : -1)) < 0)
	{
		snprintf(v->status, sizeof v->status, "%s %s", d > 0
		         ? "end of the last" : "top of the first", xv_noun(v));
		return;
	}
	c = &v->ch[k];
	v->redge = d > 0 ? 1 : -1;
	snprintf(v->status, sizeof v->status, "%s; again for the %s %s "
	         "(%d of %d): %s", d > 0 ? "end" : "top", d > 0 ? "next"
	         : "previous", xv_noun(v), k + 1, v->nch,
	         c->aut && c->summary[0] ? c->summary : c->title);
}

/* Esc: the reader to where it came from, the card to the list. */
static void
xv_back(struct xv *v)
{
	if (v->mode == XV_READ)
	{
		xv_stop(&v->kid[XV_RK], XV_RK);
		xv_read_free(v);
		free(v->rturn);
		v->rturn = NULL;
		free(v->rn);
		v->rn = NULL;
		v->strip = v->filtering = 0;
		v->rq[0] = '\0';
		/* a memory's card opens the reader too (#323) */
		v->mode = v->back == XV_CARD || v->back == XV_TCARD ? v->back
		          : XV_LIST;
	}
	else if (v->mode == XV_CARD)
	{
		xv_stop(&v->kid[XV_CK], XV_CK);
		xv_card_free(v);
		/* a card r opened in the reader (#454): back to it, at its line */
		if (v->back == XV_READ)
		{
			v->mode = XV_READ;
			v->back = v->sback;
		}
		else
			v->mode = XV_LIST;
	}
	else if (v->mode == XV_TCARD)
		v->mode = XV_LIST;
}

/* ---- the screen's refs (K6, #294) --------------------------------- */

static void
xv_sr_free(struct xv *v)
{
	int k;

	for (k = 0; k < v->nsr; k++)
		xv_ref_free(&v->sr[k]);
	v->nsr = 0;
}

struct xv_scan
{
	struct xv *v;
	const char *line;
	int rep;		/* the last reply: every span, into lr */
};

/* A span on the screen, its key once (#262: the index may not hold it,
** nor underline a ref of the last turns). */
static int
xv_span(void *ctx, const struct xr_span *sp)
{
	struct xv_scan *c = ctx;
	struct xv *v = c->v;
	struct xv_ref r;
	int k;

	struct xv_ref **a = c->rep ? &v->lr : &v->sr;
	int *n = c->rep ? &v->nlr : &v->nsr, *cap = c->rep ? &v->alr : &v->asr;

	/* the reply keeps every place, xv_reply_scan drops the earlier ones */
	for (k = 0; !c->rep && k < *n && strcmp((*a)[k].key, sp->key); k++)
		;
	if ((!c->rep && k < *n) || xv_grow(a, cap, *n, sizeof r) < 0)
		return 0;
	memset(&r, 0, sizeof r);
	r.key = strdup(sp->key);
	r.shape = strdup(xr_shape_name[sp->shape]);
	r.text = strndup(c->line + sp->s, sp->e - sp->s);
	r.title = xv_none;
	r.last = -1;
	if (!r.key || !r.shape || !r.text)
	{
		free(r.key);
		free(r.shape);
		free(r.text);
		return 0;
	}
	(*a)[(*n)++] = r;
	return 0;
}

/* The screen's refs by the engine's grammar, from its lowest row up: the
** latest first. The screen itself is not kept. */
static void
xv_screen_scan(struct xv *v, const char *scr)
{
	const char *s, *e = scr + strlen(scr);
	struct xv_scan c;
	int code = v->corpus && !strcmp(v->corpus, "shell");
	const char *cwd = v->label && v->label[0] == '/' ? v->label : NULL;
	int mode = code ? XR_CODE : XR_PROSE | XR_TERMS;

	xv_sr_free(v);
	c.v = v;
	c.rep = 0;
	for (;; e = s - 1)
	{
		for (s = e; s > scr && s[-1] != '\n'; s--)
			;
		c.line = s;
		(void)xr_grammar(s, (size_t)(e - s), mode,
		                 cwd, xv_span, &c);
		if (s == scr)
			break;
	}
}

/* The newest response's refs, one each at its last place, in document
** order (xv_vis lists them from the end). Whatever text the view holds. */
static void
xv_reply_scan(struct xv *v)
{
	const char *s, *e, *t = v->nch ? v->ch[v->nch - 1].resp : "";
	struct xv_scan c;
	int code = v->corpus && !strcmp(v->corpus, "shell");
	const char *cwd = v->label && v->label[0] == '/' ? v->label : NULL;
	int i, j, mode = code ? XR_CODE : XR_PROSE | XR_TERMS;

	for (i = 0; i < v->nlr; i++)
		xv_ref_free(&v->lr[i]);
	v->nlr = 0;
	c.v = v;
	c.rep = 1;
	for (s = t; *s; s = *e ? e + 1 : e)
	{
		e = strchr(s, '\n');
		if (!e)
			e = s + strlen(s);
		c.line = s;
		(void)xr_grammar(s, (size_t)(e - s), mode,
		                 cwd, xv_span, &c);
	}
	for (i = 0; i < v->nlr; i++)
		for (j = i + 1; j < v->nlr; j++)
			if (!strcmp(v->lr[i].key, v->lr[j].key))
			{
				xv_ref_free(&v->lr[i]);
				v->lr[i].key = NULL;
				break;
			}
	for (i = j = 0; i < v->nlr; i++)
		if (v->lr[i].key)
			v->lr[j++] = v->lr[i];
	v->nlr = j;
}

/* The session's screen from its mirror, then its refs. A remote one's
** (#188) is ctl's `--read host:name --ansi` in a child, on its own
** connection; xv_screen_done takes its answer. No --cursor: the view draws
** none, and it fails a screen over 2 MB or an older master. */
static void
xv_screen_load(struct xv *v)
{
	char *scr;

	v->scr_at = now_ms();
	if (v->hl && v->have && v->live)
	{
		char hn[sizeof v->n + 300], *av[4];

		if (v->kid[XV_SK].pid > 0)
			return;
		snprintf(hn, sizeof hn, "%.*s%s", v->hl, v->n, v->session);
		av[0] = "--read";
		av[1] = hn;
		av[2] = "--ansi";
		av[3] = NULL;
		(void)xv_start(v, XV_SK, av, 3, NULL);
		return;
	}
	xv_sr_free(v);
	if (!v->have || !v->live || !(scr = malloc(128 * 1024 + 1)))
		return;
	if (xv_screen(v->session, scr, 128 * 1024 + 1) >= 0)
		xv_screen_scan(v, scr);
	free(scr);
}

/* A remote screen's child has ended: its refs, or none (a dch-lite host
** keeps no mirror; a failed read is not said: the group just goes). */
static void
xv_screen_done(struct xv *v)
{
	struct xv_kid *k = &v->kid[XV_SK];
	char *s = k->rc ? NULL : xv_plain(k->buf ? k->buf : "", k->len);

	xv_sr_free(v);
	if (s)
		xv_screen_scan(v, s);
	free(s);
}

/* ---- drawing --------------------------------------------------------- */

static const char *
xv_dot(void)
{
	return pk_utf ? " \xc2\xb7 " : " - ";
}

static const char *
xv_ell(void)
{
	return pk_utf ? "\xe2\x80\xa6" : "...";
}

/* Spec 3.9's label for the corpus, "" for none. */
static void
xv_label(const struct xv *v, char *b, size_t n)
{
	const char *c = v->corpus, *r = v->reason, *d = xv_dot();

	b[0] = '\0';
	/* a Claude followed on while others run: the count (#314) */
	if (!v->have || (!strcmp(c, "claude") && v->claudes < 2))
		return;
	if (!strcmp(c, "codex") && v->mentions_only)
		snprintf(b, n, "Codex%smentions only", d);
	else if (!strcmp(c, "codex"))	/* #657: a rollout defines */
		snprintf(b, n, "Codex");
	else if (!strcmp(c, "shell"))
	{
		int alt = !strcmp(v->why, "alt");
		size_t l;

		if (v->mentions_only)
			snprintf(b, n, "Shell%sno shell integration%smentions only",
			         d, d);
		else
			snprintf(b, n, "Shell%slast %lld rows", d,
			         v->lines > 0 ? v->lines : 0);
		/* every shell label carries the clear note (3.9) */
		l = strlen(b);
		snprintf(b + l, n - l, "%s%s%sa terminal's own clear-screen key "
		         "may not clear X-ray; run `clear`", alt ? d : "",
		         alt ? "waiting for the shell prompt" : "", d);
	}
	else if (!strcmp(c, "screen"))
		snprintf(b, n, "Screen%slast %lld lines%smentions only", d,
		         v->lines > 0 ? v->lines : 0, d);
	else if (!strcmp(c, "none") || !strcmp(c, "claude"))
	{
		/* several Claudes: so with the ring off too, a Claude's corpus
		** is never the ring (#256) */
		if (v->claudes > 1)
			snprintf(b, n, "%lld Claude sessions run here; X-ray reads one",
			         v->claudes);
		else if (!strcmp(r, "ended"))
			snprintf(b, n, "Shell%sended%snot kept", d, d);
		else if (!r[0] && v->claudes == 0)
			snprintf(b, n, "No Claude session found here");
		else
			snprintf(b, n, "%s", !strcmp(r, "lite")
			         ? "X-ray needs the full dch build" : !strcmp(r, "off")
			         ? "X-ray is off for shells (xray_ring = 0)"
			         : "No X-ray corpus for this session");
	}
	else
		snprintf(b, n, "X-ray corpus not supported by this dch");
}

/* s under directory d (absolute, not "/"): d's length without a trailing
** '/', else 0. */
static size_t
xv_under(const char *s, const char *d)
{
	size_t l = d && d[0] == '/' ? strlen(d) : 0;

	while (l > 1 && d[l - 1] == '/')
		l--;
	return l > 1 && !strncmp(s, d, l) && s[l] == '/' ? l : 0;
}

/* s under a temp directory by its text; $TMPDIR only for a local view: a
** remote view's $TMPDIR is this machine's, not its host's. */
static int
xv_tmpdir(const char *s, int local)
{
	static const char *const d[] = { "/tmp", "/private/tmp", "/var/folders",
		"/private/var/folders" };
	size_t k;

	for (k = 0; k < sizeof d / sizeof *d; k++)
		if (xv_under(s, d[k]))
			return 1;
	return local && xv_under(s, getenv("TMPDIR"));
}

/* A file under a temp directory: a scratch file, hidden by default. A
** file of the session's own project is never scratch (#220): by text, and
** in a local view by realpath too (/tmp is /private/tmp on macOS), the
** rows resolved only when the project's own realpath is in a temp dir. A
** ".." component is scratch, never resolved: transcript text could walk it
** into an autofs map (/net, /home), a lookup per row per filter pass. */
static int
xv_temp(const struct xv *v, const struct xv_ref *r)
{
	/* the label's realpath, once per label: under /home it is an autofs
	** lookup, too slow for every row on every filter pass */
	static char lk[PATH_MAX], lr[PATH_MAX];
	static int lok;
	char rr[PATH_MAX];
	const char *t = r->text;
	size_t l = strlen(t);

	if (strcmp(r->shape, "file") || !xv_tmpdir(t, !v->hl))
		return 0;
	if (strstr(t, "/../") || (l >= 3 && !strcmp(t + l - 3, "/..")))
		return 1;
	if (xv_under(t, v->label))
		return 0;
	if (v->hl || !v->label || v->label[0] != '/')
		return 1;
	if (strcmp(lk, v->label))
	{
		snprintf(lk, sizeof lk, "%s", v->label);
		lok = realpath(v->label, lr) && xv_tmpdir(lr, 1);
	}
	return !lok || !(xv_under(t, lr) ||
	         (realpath(t, rr) && xv_under(rr, lr)));
}

static const struct xv_ref *xv_sort_rf;

/* A term (#458): shown by default, after the machine refs of its group */
static int
xv_term(const struct xv_ref *r)
{
	return !strcmp(r->shape, "term");
}

/* Refs: the machine refs before the terms (#459), the latest mentioned
** first (#262; an older host sends no last_offset), then the highest
** count, then the most recently seen. The offset is a record's line: only
** refs of one record tie, and only the engine could tell their places
** apart (#452 puts the last answer's by place in its own group). */
static int
xv_ref_cmp(const void *pa, const void *pb)
{
	const struct xv_ref *a = &xv_sort_rf[*(const int *)pa];
	const struct xv_ref *b = &xv_sort_rf[*(const int *)pb];

	if (xv_term(a) != xv_term(b))
		return xv_term(a) - xv_term(b);
	if (a->last != b->last)
		return a->last > b->last ? -1 : 1;
	if (a->count != b->count)
		return a->count > b->count ? -1 : 1;
	return a->seen < b->seen ? 1 : a->seen > b->seen ? -1 : 0;
}

/* A chapter hidden by default, n shows it: one the harness wrote (#192) or
** a compaction (#223) */
static int
xv_hid(const struct xv_ch *c)
{
	return c->aut || !strcmp(c->kind, "compact");
}

static int xv_live(const struct xv *v);

/* Chapter k's turn is still running (#290): the newest, no answer yet */
static int
xv_working(const struct xv *v, int k)
{
	return k == v->nch - 1 && !v->ch[k].resp[0] && xv_live(v);
}

/* Chapter k is an item of Responses: an answer, a tool line, an answer the
** cap dropped (its offset), or a turn still running */
static int
xv_item(const struct xv *v, int k)
{
	const struct xv_ch *c = &v->ch[k];

	return c->resp[0] || c->tools[0] || c->roff >= 0 || xv_working(v, k);
}

/* No chapter carries any of #296's fields though a turn has ended: a remote
** host before 1.63.0, or one whose turns have no answer and no tool yet.
** Nothing the host sends tells the two apart (LOW-V19): Responses lists
** nothing, a last turn's working line included, and says none yet. */
static int
xv_old(const struct xv *v)
{
	int k;

	if (!v->hl || v->nch - xv_live(v) < 1)
		return 0;
	for (k = 0; k < v->nch; k++)
		if (v->ch[k].resp[0] || v->ch[k].tools[0] || v->ch[k].roff >= 0)
			return 0;
	return 1;
}

/* Responses is a tab: the snapshot says prompts, not commands */
static int
xv_rtab(const struct xv *v)
{
	return v->have && !strcmp(xv_noun(v), "prompt");
}

/* Refs row k of xv_vis: the index's, or past it the screen's, then the
** last reply's. */
static struct xv_ref *
xv_rfat(struct xv *v, int k)
{
	k -= v->nrf;
	return k < 0 ? &v->rf[k + v->nrf] : k < v->nsr ? &v->sr[k]
	       : &v->lr[k - v->nsr];
}

/* Row k when the filter and the hidden kinds show it (those counted). */
static void
xv_vis1(struct xv *v, int k)
{
	const struct xv_ref *r = v->tab == 1 ? xv_rfat(v, k) : NULL;

	if (v->tab == 2 && (v->old || !xv_item(v, k)))
		return;
	if (v->filt[0] &&
	    !(!r ? xv_has(v->ch[k].title, v->filt) ||
	      xv_has(v->ch[k].summary, v->filt) ||
	      (v->tab == 2 && xv_has(v->ch[k].resp, v->filt))
	      : xv_has(r->text, v->filt) || xv_has(r->title, v->filt)))
		return;
	/* Refs opens on pulls, issues, tickets and terms; a shows the rest */
	if (r && strcmp(r->shape, "pull") && strcmp(r->shape, "issue") &&
	    strcmp(r->shape, "ticket") && !xv_term(r) && (v->nk++, !v->allk))
		return;
	if (r && xv_temp(v, r) && (v->ntmp++, !v->alltmp))
		return;
	if (!r && xv_hid(&v->ch[k]) && (v->nauto++, !v->allauto))
		return;
	if (!r && xv_xs(&v->ch[k]) && (v->npeer++, v->hidepeer))
		return;
	if (xv_grow(&v->vis, &v->avis, v->nvis, sizeof *v->vis) < 0)
		return;
	v->vis[v->nvis++] = k;
}

/* The rows a list tab shows (Prompts, Responses, Refs): their indexes,
** filtered. Prompts, Responses: the newest first (#290, #358). Refs: the
** refs on the screen now first (#294), the index's own row when it holds
** the key, then the rest, never one twice. Each group's terms come after
** its machine refs (#459), and only the index's: it reads prose alone, the
** screen and a reply may hold a tool's output. */
static void
xv_vis(struct xv *v)
{
	int k, j, p, n = v->tab == 1 ? v->nrf : v->nch;
	const char *sk = v->tab == 1 ? v->selk : v->tab ? v->sela : v->selp;

	if (v->tab >= 4)
	{
		xv_tvis(v);
		return;
	}
	v->vtab = v->tab;	/* Tasks' or Memory's rows are gone from vis */
	v->nvis = v->ntmp = v->nauto = v->npeer = v->nk = 0;
	v->old = xv_old(v);
	/* Search: every hit, by turn (#291, #457: xv_hgroup) */
	for (k = 0; v->tab == 3 && k < v->nh &&
	     !xv_grow(&v->vis, &v->avis, v->nvis, sizeof *v->vis); k++)
		v->vis[v->nvis++] = k;
	if (v->tab == 3)
		return;
	for (k = 0; k < v->nrf; k++)
		v->rf[k].onscr = 0;
	for (p = 0; p < 2; p++)
	for (j = 0; v->tab == 1 && j < v->nsr; j++)
	{
		if (xv_term(&v->sr[j]) != p)
			continue;
		for (k = 0; k < v->nrf && strcmp(v->rf[k].key, v->sr[j].key);
		     k++)
			;
		if (p && k == v->nrf)
			continue;
		if (k < v->nrf)
			v->rf[k].onscr = 1;
		xv_vis1(v, k < v->nrf ? k : v->nrf + j);
	}
	v->ngrp = v->nvis;
	/* the newest answer's refs, its last place on top; a turn with no
	** answer has none */
	if (v->tab == 1)
		xv_reply_scan(v);
	for (p = 0; p < 2; p++)
	for (j = v->nlr - 1; v->tab == 1 && j >= 0; j--)
	{
		if (xv_term(&v->lr[j]) != p)
			continue;
		for (k = 0; k < v->nsr && strcmp(v->sr[k].key, v->lr[j].key); k++)
			;
		if (k < v->nsr)
			continue;
		for (k = 0; k < v->nrf && strcmp(v->rf[k].key, v->lr[j].key);
		     k++)
			;
		if (p && k == v->nrf)
			continue;
		if (k < v->nrf && v->rf[k].onscr)
			continue;
		if (k < v->nrf)
			v->rf[k].onscr = 1;
		xv_vis1(v, k < v->nrf ? k : v->nrf + v->nsr + j);
	}
	v->ngrp2 = v->nvis;
	for (k = 0; k < n; k++)
		if (v->tab != 1 || !v->rf[k].onscr)
			xv_vis1(v, v->tab == 1 ? k : n - 1 - k);
	if (v->tab == 1 && v->nvis - v->ngrp2 > 1)
	{
		xv_sort_rf = v->rf;
		qsort(v->vis + v->ngrp2, (size_t)(v->nvis - v->ngrp2),
		      sizeof *v->vis, xv_ref_cmp);
	}
	/* Refs: the selection stays on its ref while the rows change under
	** it, the screen's each second; on the first row it stays there (the
	** newest under it); a key gone keeps the index. Prompts, Responses: on
	** its prompt, as --follow re-sends them (#290, #358) */
	for (k = 0; v->sel[v->tab] > 0 && sk && k < v->nvis; k++)
		if (!strcmp(v->tab == 1 ? xv_rfat(v, v->vis[k])->key
		            : v->ch[v->vis[k]].anchor, sk))
		{
			v->sel[v->tab] = k;
			break;
		}
}

/* Prompts, Refs, Responses: remember the selected row's key for xv_vis */
static void
xv_selk(struct xv *v)
{
	int s = v->sel[v->tab];
	char **k = v->tab == 1 ? &v->selk : v->tab ? &v->sela : &v->selp;

	if (v->tab == 3)
		return;	/* Search's hits change only with a new search */
	if (v->tab >= 4)
		k = &v->tq[v->tab - 4].sel;	/* Tasks, Memory: by id (#320) */
	free(*k);
	*k = s < 0 || s >= v->nvis ? NULL : strdup(v->tab == 1
	     ? xv_rfat(v, v->vis[s])->key : v->tab >= 4
	     ? v->tq[v->tab - 4].tl.t[v->vis[s]].id : v->ch[v->vis[s]].anchor);
}

/* sel and top inside n rows of a window of win */
static void
xv_clamp(int *sel, int *top, int n, int win)
{
	if (*sel >= n)
		*sel = n - 1;
	if (*sel < 0)
		*sel = 0;
	if (*top > *sel)
		*top = *sel;
	if (*sel >= *top + win)
		*top = *sel - win + 1;
	if (*top > 0 && *top + win > n)
		*top = n - win > 0 ? n - win : 0;
}

/* A row's lead: the selection's accent edge on its tint. */
static void
xv_lead(int on)
{
	pk_bg = on ? pk_p(PK_SEL) : "";
	pk_set(on ? pk_p(PK_ACC) : "");
	pk_txt(on ? (pk_utf ? "\xe2\x96\x8c" : ">") : " ");
	pk_set("");
}

/* Another agent's prompt (#360): a robot for a cross-session message, an
** inbox for a subagent's hand-back, then its sender; a bell for another
** auto row. 1 when the title, not the summary, follows (a hand-back's
** summary repeats the sender). Each mark one code point, 2 cells. */
static int
xv_mark(const struct xv_ch *c)
{
	int pe = c->peer != xv_none, w;

	if (!pe && !c->aut)
		return 0;
	pk_txt(!pk_utf ? (pe && !c->aut ? "@ " : pe ? "< " : "* ")
	       : pe && !c->aut ? "\xf0\x9f\xa4\x96 " : pe
	       ? "\xf0\x9f\x93\xa5 " : "\xf0\x9f\x94\x94 ");
	if (pe && c->peer[0])
	{
		/* at most a third of the room left, the title after it (#383) */
		w = pk_cps(c->peer);
		pk_fit(c->peer, w < (pk_w - pk_col) / 3 ? w : (pk_w - pk_col) / 3);
		pk_txt("  ");
	}
	return pe;
}

static void
xv_row_ch(const struct xv_ch *c, int i, int on)
{
	char b[40];

	xv_lead(on);
	snprintf(b, sizeof b, "%4d  ", i + 1);
	pk_set("2");
	pk_txt(b);
	xv_when(c->at, b, sizeof b);
	pk_fit(b, 13);
	pk_set(c->error ? pk_p(PK_RED) : "2");
	pk_txt(c->error ? "! " : !strcmp(c->kind, "command") ? "/ "
	       : !strcmp(c->kind, "compact") ? "= " : "  ");
	/* an auto chapter, shown: faint, by what it says it is */
	pk_set(xv_hid(c) ? "2" : "");
	pk_txt(!xv_mark(c) && c->aut && c->summary[0] ? c->summary
	       : c->title);
	if (on)
		pk_pad(pk_w);
}

/* The next line of s in w cells (a newline ends one, a long one wraps):
** its n bytes; returns where the one after it starts. */
static const char *
xv_wline(const char *s, int w, size_t *n)
{
	size_t ll = strcspn(s, "\n"), f = xv_brk(s, ll, w);

	if (!f && ll)
		f = 1;
	*n = f;
	if (f < ll && s[f] == ' ')	/* the break's space */
		return s + f + 1;
	return s[f] == '\n' ? s + f + 1 : s + f;
}

/* One more line of an item: 1 when it is drawn (*h below room), begun
** under the row's lead and faint when said */
static int
xv_fline1(int *h, int room, int faint)
{
	if ((*h)++ >= room)
		return 0;
	pk_line();
	xv_lead(0);
	pk_set(faint ? "2" : "");
	pk_txt("      ");
	return 1;
}

/* Search hit i (#291) in its turn's group (#457): the group's first hit
** draws the head (time, prompt number, title), a head hit is that line,
** any other its place: who said it, then its snippet in at most 3 lines,
** the text searched standing out; the group's last a blank under it */
static int
xv_hit1(struct xv *v, int i, int room)
{
	const struct xv_hit *t = &v->hit[v->vis[i]];
	const char *s = t->snippet, *w, *s0 = s, *who;
	char sn[3 * XR_SNIPPET + 16];
	int h = 0, l, on, c = xv_rch(v, t->chapter), sel = i == v->sel[3];
	int you = !strcmp(t->who, "user"), ind, k;
	int first = !i || !xv_hsame(t, &v->hit[v->vis[i - 1]]);
	int last = i + 1 >= v->nvis || !xv_hsame(t, &v->hit[v->vis[i + 1]]);
	int hd = first && t->head;
	unsigned char mk[1024] = { 0 };	/* a snippet's bytes in a match: 1 */
	size_t n, a, e, j, ql = strlen(v->sq);
	char b[300];

	if (first && h++ < room)
	{
		pk_line();
		xv_lead(hd && sel);
		xv_when(c >= 0 ? v->ch[c].at : t->at, b, sizeof b);
		pk_set("2");
		pk_txt(" ");
		pk_fit(b, 13);
		if (c >= 0)
		{
			snprintf(b, sizeof b, "#%d  ", c + 1);
			pk_txt(b);
		}
		pk_set(c < 0 ? "2" : "");
		snprintf(b, sizeof b, "(outside the %ss shown)", xv_noun(v));
		pk_txt(c < 0 ? b : xv_ctitle(&v->ch[c]));
		if (hd && sel)
			pk_pad(pk_w);
		pk_eol();
	}
	if (hd)
		goto end;
	/* the reader's role names; a tool hit by its tool */
	who = you ? "You" : !strcmp(t->who, "assistant") ? "Reply"
	      : t->tool[0] ? t->tool : strcmp(xv_noun(v), "prompt")
	      ? "Output" : "Tool";
	ind = pk_cps(who) + 2;
	if (!pk_utf && !strncmp(s, "\xe2\x80\xa6", 3))	/* the ASCII ellipsis */
	{
		snprintf(sn, sizeof sn, "...%s", s + 3);
		s = s0 = sn;
	}
	for (j = e = 0; s[j] && j < sizeof mk; j++)
	{
		if (ql && xv_at(s + j, v->sq))
			e = j + ql;
		mk[j] = j < e;
	}
	for (l = 0; *s && l < 3; l++)
	{
		w = s;
		s = xv_wline(w, pk_w - 8 - ind, &n);
		if (l == 0 && h++ < room)
		{
			/* the place's line: its who in the indent's place */
			pk_line();
			xv_lead(sel);
			pk_txt("      ");
			pk_set(you ? pk_p(PK_ACCB) : "1");
			pk_txt(who);
			pk_set("");
			pk_txt("  ");
		}
		else if (l == 0 || !xv_fline1(&h, room, 0))
			continue;
		else
			for (k = 0; k < ind; k++)
				pk_txt(" ");
		/* runs in a match and out of one; a match ends on a character's
		** end, the text searched being whole characters */
		for (a = 0; a < n; a = e)
		{
			j = (size_t)(w - s0) + a;
			on = j < sizeof mk && mk[j];
			for (e = a + 1; e < n && (j + e - a < sizeof mk &&
			     mk[j + e - a]) == on; e++)
				;
			pk_set(on ? pk_p(PK_ACCB) : "");
			xv_txtn(w + a, e - a);
		}
		if (l == 0 && sel)
			pk_pad(pk_w);
		pk_eol();
	}
end:
	if (last && h++ < room)
	{
		pk_line();
		pk_eol();
	}
	return h;
}

/* Responses item i of vis (#290), its lines drawn while fewer than room
** (0: measured only): the prompt as its Prompts row, the tool line faint,
** the answer wrapped to XV_ALINES lines (an ellipsis when there is more:
** the reader has it), or what stands in for it, then a blank. Returns its
** height, the blank included. */
static int
xv_feed1(struct xv *v, int i, int room)
{
	int k = v->vis[i], h = 0, w = pk_w - 8, l, tail = 0;
	const struct xv_ch *c;
	const char *s, *tl;
	size_t n;

	if (v->tab == 3)
		return xv_hit1(v, i, room);
	c = &v->ch[k];
	s = c->resp;
	if (h++ < room)
	{
		pk_line();
		xv_row_ch(c, k, i == v->sel[2]);
		pk_eol();
	}
	if (c->tools[0] && xv_fline1(&h, room, 1))
	{
		pk_txt(c->tools);
		pk_eol();
	}
	/* the newest item holds up to half the window, so a cut answer's
	** tail shows: the head's first lines, a faint ellipsis, the tail's
	** last (the engine joins them with "\n...\n") */
	if (i == 0 && c->rcut && (tl = strstr(s, "\n\xe2\x80\xa6\n")) != NULL)
	{
		const char *hs = s, *t;
		int nt, nh, hl, tk, rl = v->win / 2 > XV_ALINES ? v->win / 2 : XV_ALINES;

		tl += 5;
		for (nt = 0, t = tl; *t; nt++)
			t = xv_wline(t, w, &n);
		for (nh = 0, t = s; t < tl - 5; nh++)
			t = xv_wline(t, w, &n);
		tk = nt < rl / 2 ? nt : rl / 2;
		hl = rl - 1 - tk;
		if (nh + 1 + nt <= rl)
			hl = nh, tk = nt;
		for (l = 0; l < hl && hs < tl - 5; l++)
		{
			t = hs;
			hs = xv_wline(t, w, &n);
			if (xv_fline1(&h, room, 0))
			{
				xv_txtn(t, n);
				pk_eol();
			}
		}
		if (xv_fline1(&h, room, 1))
		{
			pk_txt(xv_ell());
			pk_eol();
		}
		for (t = tl, l = nt - tk; l-- > 0;)
			t = xv_wline(t, w, &n);
		while (*t)
		{
			const char *u = t;

			t = xv_wline(u, w, &n);
			if (xv_fline1(&h, room, 0))
			{
				xv_txtn(u, n);
				pk_eol();
			}
		}
		s = t;
		tail = 1;
	}
	else
		for (l = 0; *s && l < XV_ALINES; l++)
		{
			const char *t = s;

			s = xv_wline(t, w, &n);
			if (xv_fline1(&h, room, 0))
			{
				xv_txtn(t, n);
				pk_eol();
			}
		}
	if ((*s || (c->rcut && !tail) || (!c->resp[0] && (xv_working(v, k) ||
	     c->roff >= 0))) && xv_fline1(&h, room, 1))
	{
		pk_txt(c->resp[0] ? xv_ell() : xv_working(v, k) ? "working"
		       : "Enter reads it");
		if (!c->resp[0] && xv_working(v, k))
			pk_txt(xv_ell());
		pk_eol();
	}
	if (h++ < room)
	{
		pk_line();
		pk_eol();
	}
	return h;
}

/* Responses (#290), Search (#291): the items from top down, the selected
** one whole on screen when it fits, else at the window's top; the window
** full when items above the top fit in it. */
static void
xv_draw_feed(struct xv *v, int win)
{
	int *sel = &v->sel[v->tab], *top = &v->top[v->tab], n = v->nvis, t, h;

	if (*sel >= n)
		*sel = n - 1;
	if (*sel < 0)
		*sel = 0;
	if (*top > *sel)
		*top = *sel;
	/* h: the lines from t to the selected one's last (its blank not) */
	for (t = *sel, h = xv_feed1(v, t, 0) - 1;
	     t > *top && h + xv_feed1(v, t - 1, 0) <= win;)
		h += xv_feed1(v, --t, 0);
	*top = t;
	for (h = -1; t < n && h <= win; t++)
		h += xv_feed1(v, t, 0);
	while (t == n && *top > 0 && h + xv_feed1(v, *top - 1, 0) <= win)
		h += xv_feed1(v, --*top, 0);
	xv_selk(v);
	for (t = *top, h = 0; t < n && h < win; t++)
		h += xv_feed1(v, t, win - h);
	for (; h < win; h++)
	{
		pk_line();
		pk_eol();
	}
}

/* PgDn (d 1), PgUp (-1) on Responses and Search: the items a window holds
** from the selected one that way, at least one */
static int
xv_page(struct xv *v, int d)
{
	int k = v->sel[v->tab] < v->nvis ? v->sel[v->tab] : v->nvis - 1, s = 0;
	int k0;

	if (k < 0)
		return 0;
	for (k0 = k; k + d >= 0 && k + d < v->nvis &&
	     (s += xv_feed1(v, d > 0 ? k : k + d, 0)) <= v->win;)
		k += d;
	return k == k0 ? d : k - k0;
}

/* s in w cells; one that does not fit keeps its first keep bytes, then an
** ellipsis and the tail that fits: a path's end tells it apart. */
static void
xv_tail(const char *s, size_t keep, int w)
{
	const char *e = xv_ell();
	int end = pk_col + w, n, l;
	char c[5];

	if (pk_cps(s) <= w || w <= pk_cps(e))
	{
		pk_fit(s, w);
		return;
	}
	xv_txtn(s, keep);
	for (s += keep, n = pk_cps(s); *s && n > end - pk_col - pk_cps(e); s += l)
	{
		(void)ap_cp(s, &l);
		memset(c, 0, sizeof c);
		memcpy(c, s, (size_t)l);
		n -= pk_cps(c);
	}
	pk_txt(e);
	pk_fit(s, end - pk_col);
}

/* A ref's text in w cells: a file under the project relative to it, else
** under HOME from ~; a URL cut keeps its host's start and its end, not its
** scheme. */
static void
xv_reftext(const struct xv *v, const struct xv_ref *r, int w)
{
	const char *s = r->text, *u;
	char b[4200];
	size_t l, keep = 0;
	int file = !strcmp(r->shape, "file");

	if (file && (l = xv_under(s, v->label)))
		s += l + 1;
	else if (file && !v->hl && (l = xv_under(s, getenv("HOME"))))
	{
		snprintf(b, sizeof b, "~%s", s + l);
		s = b;
	}
	else if (!strcmp(r->shape, "url") && pk_cps(s) > w &&
	         (u = strstr(s, "://")))
	{
		s = u + 3;
		keep = strcspn(s, "/");
		/* #220: the first cells are always the host's start, so a path
		** can never pose as the host. A host wider than the row is cut
		** at its end; one wider than half keeps its first w/2 cells;
		** either way the ellipsis then the URL's tail. */
		if (xv_fitlen(s, keep, w) < keep)
		{
			pk_fit(s, w);
			return;
		}
		keep = xv_fitlen(s, keep, w / 2);
	}
	xv_tail(s, keep, w);
}

static void
xv_row_ref(const struct xv *v, const struct xv_ref *r, int on)
{
	char b[40];
	int tw = pk_w / 3 > 20 ? pk_w / 3 : 20;

	xv_lead(on);
	snprintf(b, sizeof b, "%5lld ", r->count);
	if (!r->count)
		b[4] = ' ';	/* only on the screen: no count (#294) */
	pk_set("2");
	pk_txt(b);
	pk_set(pk_p(PK_ACC));
	pk_txt(r->defined ? (pk_utf ? "\xe2\x97\x8f " : "* ") : "  ");
	pk_set("2");
	pk_fit(r->shape, 7);
	pk_txt(" ");
	pk_set(r->underline ? "1" : "");
	/* no title: the whole row's rest */
	xv_reftext(v, r, r->title[0] ? tw : pk_w - pk_col - 1);
	pk_txt("  ");
	pk_set("2");
	pk_txt(r->title);
	if (on)
		pk_pad(pk_w);
}

/* The card: what the ref is, its definitions, counts and mentions. */
/* #478: the card groups its mentions by turn. They come newest first per
** session (#304), so a turn's are one run: m[k] opens one when its session
** or chapter differs from the one above. */
static int
xv_run0(const struct xv *v, int k)
{
	return k == 0 || !xv_ses(&v->m[k], &v->m[k - 1]) ||
	       strcmp(v->m[k].chapter, v->m[k - 1].chapter);
}

/* The header rows above mention k: its turn's (1), and over that (2) its
** session's when m[k] opens a run of this project's other session (#479) */
static int
xv_hdrs(const struct xv *v, int k)
{
	return xv_run0(v, k) + (v->m[k].same && (k == 0 ||
	       !xv_ses(&v->m[k], &v->m[k - 1])));
}

/* The card's list row of mention k, or of its header hdr (1 the turn's,
** 2 the session's) */
static int
xv_crow(const struct xv *v, int k, int hdr)
{
	int j, r = 0;

	for (j = 0; j <= k && j < v->nm; j++)
		r += 1 + xv_hdrs(v, j);
	if (k < v->nm && hdr > xv_hdrs(v, k))
		hdr = xv_hdrs(v, k);
	return r - 1 - hdr;
}

/* List row r back to its mention, and which header (0 the mention) */
static void
xv_cat(const struct xv *v, int r, int *k, int *hdr)
{
	int j, h;

	*k = v->nm > 0 ? v->nm - 1 : 0;
	*hdr = 0;
	for (j = 0; j < v->nm; j++)
	{
		for (h = xv_hdrs(v, j); h > 0; h--)
			if (r-- == 0)
			{
				*k = j, *hdr = h;
				return;
			}
		if (r-- == 0)
		{
			*k = j;
			return;
		}
	}
}

static int
xv_crows(const struct xv *v)
{
	return v->nm ? xv_crow(v, v->nm - 1, 0) + 1 : 0;
}

/* The selection d list rows on (headers too), or to row d when abs */
static void
xv_cmove(struct xv *v, int d, int abs)
{
	int r = abs ? d : xv_crow(v, v->csel, v->chdr) + d, n = xv_crows(v);

	if (r >= n)
		r = n - 1;
	xv_cat(v, r < 0 ? 0 : r, &v->csel, &v->chdr);
}

/* A mention's sentence, its ref standing out as a search match does; on a
** narrow row the words before it give way so the ref shows. An older
** host's mention: its line. */
static void
xv_csent(const struct xv_men *m)
{
	const char *s = m->sent;
	size_t a = 0, p, ss = (size_t)m->ss, se = (size_t)m->se;
	int room = pk_w - pk_col - 2, snap = 1;

	if (!s[0] || m->ss < 0)
	{
		pk_txt(s[0] ? s : m->line);
		return;
	}
	while (a < ss && xv_fitlen(s + a, se - a, room) < se - a)
		while (++a < ss && ((unsigned char)s[a] & 0xc0) == 0x80)
			;
	/* NIT-UH2: on to the next word, never past the ref; the cut stays when
	** there is none or it drops over half the context shown (#483's rule);
	** a mark after the spaces is skipped too (#494). The word snap is the
	** first pass's only: judged again against the snapped cut, #483's half
	** would drop a chain of mark-led words whole (#504) */
	do
	{
		size_t k;

		p = a;
		while (a < ss && (k = xr_u8_cont(s + a, ss - a)))
			a += k;
		a += xr_ri2(s, a, ss);
		if (snap && a && (unsigned char)s[a - 1] > ' ')
		{
			size_t b = a;

			while (b < ss && (unsigned char)s[b] > ' ')
				b++;
			if (b < ss && b - a <= (ss - a) / 2)
				a = b;
		}
		snap = 0;
		while (a && a < ss && (unsigned char)s[a] <= ' ')
			a++;
	} while (a != p);
	if (a)
		pk_txt(xv_ell());
	xv_txtn(s + a, ss - a);
	pk_set(pk_p(PK_ACCB));
	xv_txtn(s + ss, se - ss);
	pk_set("");
	pk_txt(s + se);
}

static void
xv_draw_card(struct xv *v, int win)
{
	char b[600], w[40];
	const char *ge = v->cdone && !v->cpartial ? "" : "\xe2\x89\xa5";
	int k, rows = 0, first = -1, last = -1, mw, sel, n, r;
	int running = v->kid[XV_CK].pid > 0;

	if (!pk_utf)
		ge = v->cdone && !v->cpartial ? "" : ">=";
	for (k = 0; k < v->nm; k++)
		if (v->m[k].at >= 0)
		{
			if (first < 0 || v->m[k].at < v->m[first].at)
				first = k;
			if (last < 0 || v->m[k].at > v->m[last].at)
				last = k;
		}
	/* the definitions: two or more are counted, never one picked */
	pk_line();
	pk_set("1");
	if (!v->nd && !running && first >= 0 && v->m[first].line[0] &&
	    !(v->cwarn && v->cwarn[0]))
	{
		/* no definition: the first mention's line says what it is */
		const struct xv_men *m = &v->m[first];
		int c = xv_rch(v, m->chapter);

		pk_set("2");
		if (c >= 0)
		{
			snprintf(b, sizeof b, "First said (%s %d, ", xv_noun(v),
			         c + 1);
			pk_txt(b);
			pk_txtto(v->ch[c].title, pk_col + pk_w / 3);
			pk_txt("): ");
		}
		else
			pk_txt("First said: ");
		pk_set("");
		pk_txtto(m->line, pk_w);
	}
	else if (!v->nd)
	{
		pk_set("2");
		pk_txt(running ? "Definitions: searching" : "No definition found");
		if (!running && v->cwarn && v->cwarn[0])	/* #405: why not */
		{
			pk_txt(xv_dot());
			pk_txt(v->cwarn);
		}
	}
	else
	{
		snprintf(b, sizeof b, v->nd == 1 ? "Definition" : "%d definitions",
		         v->nd);
		pk_txt(b);
	}
	pk_eol();
	rows++;
	for (k = 0; k < v->nd && rows < win / 2; k++)
	{
		const struct xv_def *d = &v->d[k];

		pk_line();
		pk_txt("  ");
		if (d->cmd)	/* #405: the user's own command, marked so */
		{
			/* the mark's room first: a long title or status is cut,
			** the mark stays (LOW-AV5) */
			int lim = pk_w - pk_cps(xv_dot()) -
			          pk_cps("your define_command"),
			    sw = d->status[0] ? 2 + pk_cps(d->status) : 0;

			/* up to 16 cells of the status kept beside a long title */
			pk_txtto(d->title, lim - (sw < 18 ? sw : 18));
			pk_set("2");
			if (d->status[0] && pk_col + 2 < lim)
			{
				pk_txt("  ");
				pk_txtto(d->status, lim);
			}
			pk_txt(xv_dot());
			pk_txt("your define_command");
			pk_eol();
			rows++;
			if (d->url[0] && rows < win / 2)
			{
				pk_line();
				pk_set("2");
				pk_txt("    ");
				pk_txt(d->url);
				pk_eol();
				rows++;
			}
		}
		else
		{
			pk_set(pk_p(PK_ACC));
			pk_txt(d->rule);
			pk_set("");
			pk_txt(" ");
			pk_txt(d->title);
			xv_when(d->at, w, sizeof w);
			pk_set("2");
			snprintf(b, sizeof b, "  %s %s", w, d->session);
			pk_txt(b);
			pk_eol();
			rows++;
		}
		/* a term's first mention (#459): its title is the passage */
		if (d->passage[0] && strcmp(d->passage, d->title) &&
		    rows < win / 2)
		{
			const char *s = d->passage;
			int l;

			for (l = 0; l < 3 && *s && rows < win / 2; l++, rows++)
			{
				size_t f = xv_fitlen(s, d->cmd ? strcspn(s, "\n")
				                     : strlen(s), pk_w - 4);

				if (!f && !(d->cmd && *s == '\n'))
					f = 1;
				pk_line();
				pk_set("2");
				pk_txt("    ");
				xv_txtn(s, f);
				pk_eol();
				s += f;
				if (d->cmd && *s == '\n')	/* its own lines */
					s++;
			}
		}
	}
	/* the resolver's one line (f): the commit or file, or why not */
	if (v->vkind)
	{
		pk_line();
		pk_set(pk_p(PK_ACC));
		pk_txt(v->vkind);
		pk_set("");
		pk_txt(" ");
		pk_txt(v->vtitle);
		pk_set("2");
		pk_txt("  ");
		pk_txt(v->vsub);
		pk_eol();
		rows++;
	}
	/* the counts by scope */
	pk_line();
	snprintf(b, sizeof b, "Mentions: this session %s%lld", ge,
	         v->cnt[0] > 0 ? v->cnt[0] : 0);
	/* light has not scanned them: never "0 in 0 sessions" before f */
	if (v->cnt[1] >= 0 && !v->full && !v->cnt[1] && v->cnt[2] <= 0)
		snprintf(b + strlen(b), sizeof b - strlen(b),
		         "%sthis project: f scans", xv_dot());
	else if (v->cnt[1] >= 0)
		snprintf(b + strlen(b), sizeof b - strlen(b), "%sthis project "
		         "%s%lld in %lld sessions", xv_dot(), ge, v->cnt[1],
		         v->cnt[2] > 0 ? v->cnt[2] : 0);
	if (v->cnt[3] >= 0 && !v->full && !v->cnt[3] && v->cnt[4] <= 0)
		snprintf(b + strlen(b), sizeof b - strlen(b),
		         "%sother projects: f scans", xv_dot());
	else if (v->cnt[3] >= 0)
		snprintf(b + strlen(b), sizeof b - strlen(b), "%sother projects "
		         "%s%lld in %lld sessions", xv_dot(), ge, v->cnt[3],
		         v->cnt[4] > 0 ? v->cnt[4] : 0);
	pk_txt(b);
	pk_set("2");
	pk_txt(running ? (v->full ? "  searching more" : "  searching")
	       : v->cdone && v->cpartial && !v->full ? "  (f for more)" : "");
	if (running)
		pk_txt(xv_ell());
	pk_eol();
	rows++;
	pk_line();
	pk_set("2");
	if (first >= 0)
	{
		xv_when(v->m[first].at, w, sizeof w);
		snprintf(b, sizeof b, "First mention %s %s", w, v->m[first].session);
		pk_txt(b);
		xv_when(v->m[last].at, w, sizeof w);
		snprintf(b, sizeof b, "%slatest %s %s", xv_dot(), w,
		         v->m[last].session);
		pk_txt(b);
	}
	pk_eol();
	rows++;
	/* the mentions, a list: a header per turn, newest first (#478) */
	mw = win - rows;
	if (mw < 1)
		mw = 1;
	if (v->csel >= v->nm)
		v->csel = v->nm - 1, v->chdr = 0;
	if (v->csel < 0)
		v->csel = 0;
	sel = xv_crow(v, v->csel, v->chdr);
	n = xv_crows(v);
	xv_clamp(&sel, &v->ctop, n, mw);
	if (n)
		xv_cat(v, sel, &v->csel, &v->chdr);
	for (r = v->ctop; rows < win; r++, rows++)
	{
		pk_line();
		if (r < n)
		{
			const struct xv_men *m;
			int hdr, c, other, nw, aw;

			xv_cat(v, r, &k, &hdr);
			m = &v->m[k];
			c = xv_rch(v, m->chapter);
			other = strcmp(m->session, v->session) != 0;
			xv_lead(r == sel);
			if (hdr == 2)
			{
				/* #479: the project's other session, its last activity */
				pk_age((long)m->seen, w, sizeof w);
				pk_set("2");
				pk_txt("also discussed in ");
				pk_set("1");
				/* the age's real width; the name keeps the room, the
				** age goes first, a cut name ends in an ellipsis */
				nw = pk_cps(m->session);
				aw = w[0] ? 2 + pk_cps(w) + (strcmp(w, "now") ? 4 : 0) : 0;
				if (pk_col + nw + aw > pk_w)
					aw = 0;
				if (pk_col + nw > pk_w)
				{
					pk_txtto(m->session, pk_w - 1);
					pk_txt(pk_utf ? "\xe2\x80\xa6" : "~");
				}
				else
					pk_txtto(m->session, pk_w);
				pk_set("2");
				if (aw)
				{
					pk_txt(", ");
					pk_txt(w);
					pk_txt(strcmp(w, "now") ? " ago" : "");
				}
			}
			else if (hdr && m->same)
			{
				/* its turn: pN, its title, its newest mention's time */
				pk_set("1");
				if (m->turn > 0)
				{
					snprintf(w, sizeof w, "%c%lld ", xv_noun(v)[0],
					         m->turn);
					pk_txt(w);
				}
				pk_txtto(m->ttitle[0] ? m->ttitle : xv_noun(v),
				         pk_w - 14);
				xv_when(m->at, w, sizeof w);
				pk_set("2");
				pk_txt("  ");
				pk_txt(w);
			}
			else if (hdr)
			{
				/* the turn: pN, its title, its time */
				pk_set("1");
				if (c >= 0)
				{
					snprintf(w, sizeof w, "%c%d ", xv_noun(v)[0], c + 1);
					pk_txt(w);
					pk_txtto(v->ch[c].title, pk_w - 14);
					xv_when(v->ch[c].at, w, sizeof w);
					pk_set("2");
					pk_txt("  ");
					pk_txt(w);
				}
				else
				{
					pk_set("2");
					pk_txt(other ? m->session : "no prompt");
				}
			}
			else
			{
				pk_txt("  ");
				pk_set("2");
				if (c < 0 && !m->same)	/* no header time: its own */
				{
					xv_when(m->at, w, sizeof w);
					pk_fit(w, 13);
				}
				/* #517: what the agent read, apart from what was said */
				pk_fit(m->read ? "read"
				       : !strcmp(m->who, "user") ? "You"
				       : !strcmp(m->who, "assistant") ? "Reply"
				       : m->tool[0] ? m->tool
				       : !strcmp(m->who, "tool") ? "Tool" : "", 6);
				pk_set("");
				xv_csent(m);
			}
			if (r == sel)
				pk_pad(pk_w);
		}
		else if (r == 0)
		{
			pk_set("2");
			pk_txt(running ? "  " : !xv_lines_ok(v, v->session)
			       ? "  Lines need the full scan: f" : "  No mention lines");
		}
		pk_eol();
	}
}

/* Row k of r's strip (#454): the index's ref with its key (count, title),
** else the scan's */
static const struct xv_ref *
xv_sref(const struct xv *v, int k)
{
	int j;

	for (j = 0; j < v->nrf; j++)
		if (!strcmp(v->rf[j].key, v->rr[k].key))
			return &v->rf[j];
	return &v->rr[k];
}

/* The strip's height in the window, 0 closed: up to half of it */
static int
xv_strip_h(const struct xv *v)
{
	int h = v->win / 2 > 2 ? v->win / 2 : 2;

	if (!v->strip)
		return 0;
	if (h > v->nrr + 1)
		h = v->nrr + 1;
	return h < v->win ? h : v->win;
}

/* r's strip in rows lines: a faint head, then its refs as Refs' rows */
static void
xv_draw_strip(struct xv *v, int rows)
{
	int k;

	if (rows > 1)
	{
		pk_line();
		pk_set("2");
		pk_txt("  refs in view, the lowest first");
		pk_eol();
		rows--;
	}
	xv_clamp(&v->ssel, &v->stp, v->nrr, rows);
	for (k = v->stp; k < v->stp + rows; k++)
	{
		pk_line();
		if (k < v->nrr)
			xv_row_ref(v, xv_sref(v, k), k == v->ssel);
		pk_eol();
	}
}

/* n bytes of reader text s on base, the text found (/, #454) standing out */
static void
xv_rhl(const struct xv *v, const char *s, size_t n, const char *base)
{
	size_t a = 0, e, ql = strlen(v->rq);

	while (a < n)
	{
		for (e = a; e < n && !(ql && xv_at(s + e, v->rq)); e++)
			;
		pk_set(base);
		xv_txtn(s + a, e - a);
		if (e == n)
			break;
		a = e + ql < n ? e + ql : n;
		pk_set(pk_p(PK_ACCB));
		xv_txtn(s + e, a - e);
	}
}

static void
xv_draw_read(struct xv *v, int win)
{
	int k, h = xv_strip_h(v);

	if (v->rloaded && v->rw != pk_w - 2)
		xv_wrap(v, pk_w - 2);
	if (v->rtop > v->nrl - win)
		v->rtop = v->nrl - win;
	if (v->rtop < 0)
		v->rtop = 0;
	/* r's strip under the text: the lowest lines stay in view */
	for (k = v->rtop + h; k < v->rtop + win; k++)
	{
		pk_line();
		if (k < v->nrl && v->rl[k].kind == 0)
		{
			const struct xv_ent *e = &v->e[v->rl[k].n];
			int you = !strcmp(e->role, "user");

			pk_set(e->error ? pk_p(PK_RED) : you ? pk_p(PK_ACCB) : "1");
			pk_txt(you ? "You" : !strcmp(e->role, "assistant") ? "Reply"
			       : !strcmp(e->role, "tool") ? "Tool" : "Output");
			if (e->tool[0])
			{
				pk_set("2");
				pk_txt(" ");
				pk_txt(e->tool);
			}
		}
		else if (k < v->nrl)
		{
			pk_set(v->rl[k].kind == 2 ? "2" : "");
			pk_txt("  ");
			xv_rhl(v, v->rl[k].s, v->rl[k].n, v->rl[k].kind == 2 ? "2" : "");
		}
		else if (k == 0)
		{
			pk_set("2");
			pk_txt(v->kid[XV_RK].pid > 0 ? "  Reading" : "  No entries");
			if (v->kid[XV_RK].pid > 0)
				pk_txt(xv_ell());
		}
		pk_eol();
	}
	if (h)
		xv_draw_strip(v, h);
}

/* Search (#291): the text's line, then the hits as a feed, or the one
** line in their place; faint under them, more and partial */
static void
xv_draw_search(struct xv *v, int win)
{
	char b[600], m[60];
	const char *y = v->swhy ? v->swhy : "";
	int k, more = v->sdone && v->smore && v->nh;

	pk_line();
	pk_set(pk_p(PK_ACC));
	pk_txt(" Search: ");
	pk_set("");
	pk_txt(v->q);
	if (v->filtering)
		pk_txt(pk_utf ? "\xe2\x96\x88" : "_");
	pk_eol();
	snprintf(b, sizeof b, "  the search stopped early (%s); the hits shown "
	         "stand", !strcmp(y, "time") ? "it took too long"
	         : !strcmp(y, "bytes") ? "too much to read"
	         : !strcmp(y, "range") ? "the history changed or was cut as it "
	         "read" : y);
	win -= 1 + more + v->spart;
	if (win < 1)
		win = 1;
	if ((xv_vis(v), v->nvis))
		xv_draw_feed(v, win);
	else
	{
		for (k = 0; k < win; k++)
		{
			pk_line();
			pk_set("2");
			if (k == 0 && v->kid[XV_QK].pid > 0)
			{
				pk_txt("  Searching");
				pk_txt(xv_ell());
			}
			else if (k == 0 && v->sline[0])
			{
				pk_txt("  ");
				pk_txt(v->sline);
			}
			else if (k == 0 && v->sdone)
			{
				pk_txt("  No match for \"");
				pk_txt(v->sq);
				pk_txt("\"");
			}
			else if (k == 0)
				pk_txt("  Type a word or phrase; Enter finds "
				       "where this session said it");
			pk_eol();
		}
	}
	if (more)
	{
		pk_line();
		pk_set("2");
		snprintf(m, sizeof m, "  the %d newest %ss are shown",
		         xv_hturns(v), xv_noun(v));
		pk_txt(m);
		pk_eol();
	}
	if (v->spart)
	{
		pk_line();
		pk_set("2");
		pk_txt(b);
		pk_eol();
	}
}

/* ---- Tasks' drawing (#320) ------------------------------------------- */

static const char *const xv_tgrp[4] = { "In progress", "Pending", "Done",
	"Dropped" };
static const char *const xv_mgrp[3] = { "This session", "Memories",
	"Instructions" };
static const char *const xv_hgrp[3] = { "Pending", "Fired", "Ended" };

static int xv_live(const struct xv *v);

/* A task's label says more than its status (a ledger's PARKED) */
static int
xv_tlabel(const struct xv_task *t)
{
	static const char *const w[] = { "", "pending", "in_progress",
		"in progress", "completed", "deleted", "active", "done",
		"dropped", "open" };
	size_t k;

	for (k = 0; k < sizeof w / sizeof *w; k++)
		if (!strcasecmp(t->label, w[k]))
			return 0;
	return 1;
}

/* Tasks' line L, the group lines among the rows: a row of vis (>= 0), or
** group g's line (-1 - g) */
static int
xv_tat(const struct xv *v, int L)
{
	int g, h = 0;

	for (g = 0; g < 4; g++)
		if (v->tg[g] >= 0)
		{
			if (L == v->tg[g] + h)
				return -1 - g;
			if (L < v->tg[g] + h)
				break;
			h++;
		}
	return L - h;
}

/* A task's row: its mark, id, title (an active one's text while it runs),
** and at the right a label that says more, what blocks it and its age */
static void
xv_row_task(const struct xv_task *t, int on)
{
	static const char *const mk[2][4] = { { "> ", "o ", "x ", "- " },
		{ "\xe2\x96\xb6 ", "\xe2\x97\x8b ", "\xe2\x9c\x93 ",
		  "\xe2\x9c\x97 " } };
	char x[300], age[16];
	int xw, aw;

	xv_lead(on);
	pk_set(t->st == 0 ? pk_p(PK_ACC) : "2");
	pk_txt(mk[pk_utf][t->st]);
	pk_set("2");
	pk_fit(t->id, 6);
	pk_txt(" ");
	snprintf(x, sizeof x, "%s%s%s%s", xv_tlabel(t) ? t->label : "",
	         xv_tlabel(t) && t->blocked[0] ? "  " : "",
	         t->blocked[0] ? "blocked by " : "", t->blocked);
	pk_age((long)t->updated, age, sizeof age);
	aw = age[0] ? (int)strlen(age) + 2 : 0;
	xw = pk_cps(x);
	if (xw > (pk_w - pk_col) / 3)
		xw = (pk_w - pk_col) / 3;
	pk_set(t->st == 3 ? "2" : "");
	pk_fit(t->st == 0 && t->active[0] ? t->active : t->title,
	       pk_w - pk_col - aw - (xw ? xw + 2 : 0) - 1);
	pk_set("2");
	if (xw)
	{
		pk_txt("  ");
		pk_fit(x, xw);
	}
	if (aw)
	{
		pk_txt("  ");
		pk_txt(age);
	}
	if (on)
		pk_pad(pk_w);
}

/* A memory's row (#323): its title (an instruction file's path, short), a
** kind that is one word and its note, faint, as room allows; at the right
** whether this session wrote or loaded it and its age */
static void
xv_row_mem(const struct xv *v, const struct xv_task *t, int on)
{
	char x[40], age[16];
	int xw, end;

	xv_lead(on);
	pk_txt(" ");
	pk_age((long)t->updated, age, sizeof age);
	snprintf(x, sizeof x, "%s%s%s", t->ses == 1 ? "wrote" : t->ses == 2
	         ? "loaded" : "", t->ses && age[0] ? "  " : "", age);
	xw = x[0] ? pk_cps(x) + 2 : 0;
	end = pk_w - xw - 1;
	if (!strcmp(t->label, "instructions"))
	{
		struct xv_ref r = { 0 };

		r.shape = "file";
		r.text = t->path[0] ? t->path : t->title;
		xv_reftext(v, &r, end - pk_col);
	}
	else
	{
		xw = pk_cps(t->title);
		pk_fit(t->title, xw < end - pk_col ? xw : end - pk_col);
		pk_set("2");
		if (t->label[0] && !strpbrk(t->label, " \t") &&
		    end - pk_col > pk_cps(t->label) + 2)
		{
			pk_txt("  ");
			pk_txt(t->label);
		}
		if (t->note[0] && end - pk_col > 8)
		{
			pk_txt("  ");
			pk_fit(t->note, end - pk_col);
		}
		pk_pad(end);
	}
	pk_set("2");
	if (x[0])
	{
		pk_txt("  ");
		pk_txt(x);
	}
	if (on)
		pk_pad(pk_w);
}

/* s seconds, short: 45s, 4m, 3h, 2d */
static void
xv_dur(long long s, char *b, size_t n)
{
	if (s < 60)
		snprintf(b, n, "%llds", s < 0 ? 0 : s);
	else if (s < 3600)
		snprintf(b, n, "%lldm", s / 60);
	else if (s < 86400)
		snprintf(b, n, "%lldh", s / 3600);
	else
		snprintf(b, n, "%lldd", s / 86400);
}

/* A schedule's row (#687): its mark, id and prompt; at the right its
** human schedule, when (next 4m counted down from the clock at each frame,
** due once next has passed in a live session, fired 2m ago; an ended
** one's status), its fires and its no-op streak */
static void
xv_row_sched(const struct xv *v, const struct xv_task *t, int on)
{
	static const char *const mk[2][3] = { { "o ", "x ", "- " },
		{ "\xe2\x97\x8b ", "\xe2\x9c\x93 ", "\xe2\x9c\x97 " } };
	long long now = (long long)time(NULL);
	char x[300], w[40], d[24];
	int xw;

	w[0] = '\0';
	if (t->st == 2)
		snprintf(w, sizeof w, "%s", t->stw);
	else if (t->st == 0 && t->next > now)
	{
		xv_dur(t->next - now, d, sizeof d);
		snprintf(w, sizeof w, "next %s", d);
	}
	else if (t->st == 0 && t->next >= 0 && xv_live(v))
		snprintf(w, sizeof w, "due");
	else if (t->lastf >= 0)
	{
		xv_dur(now - t->lastf, d, sizeof d);
		snprintf(w, sizeof w, "fired %s ago", d);
	}
	snprintf(x, sizeof x, "%s%s%s", t->label, t->label[0] && w[0] ? "  "
	         : "", w);
	if (t->fires > 0)
		snprintf(x + strlen(x), sizeof x - strlen(x), "%s%s%lld",
		         x[0] ? "  " : "", pk_utf ? "\xc3\x97" : "x", t->fires);
	if (t->noop > 0)
		snprintf(x + strlen(x), sizeof x - strlen(x), "%snoop %lld",
		         x[0] ? "  " : "", t->noop);
	xv_lead(on);
	pk_set(t->st == 0 ? pk_p(PK_ACC) : "2");
	pk_txt(mk[pk_utf][t->st]);
	pk_set("2");
	pk_fit(t->id, 8);
	pk_txt(" ");
	xw = pk_cps(x);
	if (xw > (pk_w - pk_col) / 2)
		xw = (pk_w - pk_col) / 2;
	pk_set(t->st == 2 ? "2" : "");
	pk_fit(t->title, pk_w - pk_col - (xw ? xw + 2 : 0) - 1);
	pk_set("2");
	if (xw)
	{
		pk_txt("  ");
		pk_fit(x, xw);
	}
	if (on)
		pk_pad(pk_w);
}

/* Tasks, Memory: a faint title line when a task list has a name (a memory
** list's names its group), the rows under their group lines (the window
** only: nothing drawn or measured off it), faint lines under them for a
** warning, a partial read and what was cut */
static void
xv_draw_tasks(struct xv *v, int win)
{
	const struct xv_tl *l = &v->tq[v->tab - 4].tl;
	int *sel = &v->sel[v->tab], *top = &v->top[v->tab], n, ng = 0, g, k, i;
	int nf = 0, mem = v->tab == 5, sch = v->tab == 6;
	const char *what = mem ? "memories" : sch ? "schedules" : "tasks";
	char b[300], f[3][300];

	xv_vis(v);
	n = v->nvis;
	if (!mem && l->title && l->title[0] && win > 1)
	{
		pk_line();
		pk_set("2");
		pk_txt("  ");
		pk_txt(l->title);
		pk_eol();
		win--;
	}
	if (l->warn && l->warn[0])
		snprintf(f[nf++], sizeof *f, "  %s", l->warn);
	if (l->partial)
		snprintf(f[nf++], sizeof *f, "  the history was too long to read "
		         "whole; %s may be missing", mem ? "what this session "
		         "wrote or loaded" : sch ? "older schedules"
		         : "older tasks");
	if (l->dropped)
		snprintf(f[nf++], sizeof *f, "  the %d %s are shown", l->n,
		         mem ? "first" : "newest");
	else if (!l->done && !xv_tbusy(v, v->tab - 4))
		snprintf(f[nf++], sizeof *f, "  the list stopped early; the %s "
		         "shown stand", what);
	while (nf && win - nf < 1)
		nf--;
	win -= nf;
	for (g = 0; g < 4; g++)
		ng += v->tg[g] >= 0;
	if (*sel >= n)
		*sel = n - 1;
	if (*sel < 0)
		*sel = 0;
	/* the selection's line, its group's line in view on its first row */
	for (i = *sel, g = 0; g < 4; g++)
		i += v->tg[g] >= 0 && v->tg[g] <= *sel;
	xv_clamp(&i, top, n + ng, win);
	if (*top == i && *top && xv_tat(v, i - 1) < 0)
		(*top)--;
	for (k = *top; k < *top + win; k++)
	{
		pk_line();
		if (k < n + ng && (i = xv_tat(v, k)) < 0)
		{
			/* the group's line, its rows counted */
			int e = n;

			for (g = -i; g < 4; g++)
				if (v->tg[g] >= 0)
				{
					e = v->tg[g];
					break;
				}
			pk_set("2");
			snprintf(b, sizeof b, "  %s %d", sch ? xv_hgrp[-1 - i]
			         : !mem ? xv_tgrp[-1 - i]
			         : i == -2 && l->title && l->title[0] ? l->title
			         : xv_mgrp[-1 - i], e - v->tg[-1 - i]);
			pk_txt(b);
		}
		else if (k < n + ng && mem)
			xv_row_mem(v, &l->t[v->vis[i]], i == *sel);
		else if (k < n + ng && sch)
			xv_row_sched(v, &l->t[v->vis[i]], i == *sel);
		else if (k < n + ng)
			xv_row_task(&l->t[v->vis[i]], i == *sel);
		else if (k == 0)
		{
			pk_set("2");
			pk_txt(xv_fref(v, b, sizeof b) ? "    No match: ^R looks it up"
			       : "    No match; ^R searches the session for it");
		}
		pk_eol();
	}
	for (k = 0; k < nf; k++)
	{
		pk_line();
		pk_set("2");
		pk_txt(f[k]);
		pk_eol();
	}
}

/* One line of a task's card: its name faint, then its value */
static int
xv_tfield(const char *k, const char *val, int *rows, int win)
{
	if (!val[0] || *rows >= win)
		return 0;
	pk_line();
	pk_set("2");
	pk_txt("  ");
	pk_fit(k, 12);
	pk_set("");
	pk_fit(val, pk_w - pk_col);	/* cut with an ellipsis (NIT-V11) */
	pk_eol();
	return ++*rows;
}

/* A path line of a memory's card: its name faint, then lead and the path,
** its tail kept */
static void
xv_tpath(const char *k, const char *lead, const char *path, int *rows,
         int win)
{
	if (*rows >= win)
		return;
	pk_line();
	pk_set("2");
	pk_txt("  ");
	pk_fit(k, 12);
	pk_set("");
	pk_txt(lead);
	xv_tail(path, 0, pk_w - pk_col);
	pk_eol();
	++*rows;
}

/* A memory's card (#323): its title whole, a line per field (this
** session's line says Enter reads that turn), then its text under them,
** scrolled by mtop */
static int
xv_draw_mcard(struct xv *v, const struct xv_task *t, int win, int rows)
{
	const char *s, *w;
	char x[300], u[40];
	size_t nb;
	int k, n;

	xv_when(t->updated, u, sizeof u);
	(void)xv_tfield("Note", t->note, &rows, win);
	if (t->path[0])
		xv_tpath("Path", "", t->path, &rows, win);
	(void)xv_tfield("Kind", t->label, &rows, win);
	(void)xv_tfield("Scope", t->scope, &rows, win);
	(void)xv_tfield("Updated", u, &rows, win);
	snprintf(x, sizeof x, "this session %s it%s%s", t->ses == 1 ? "wrote"
	         : "loaded", t->chapter[0] ? "; enter reads that " : "",
	         t->chapter[0] ? xv_noun(v) : "");
	if (t->ses)
		(void)xv_tfield("Session", x, &rows, win);
	snprintf(x, sizeof x, "not kept (the list keeps %u MB of text)%s",
	         XV_MBODY >> 20, t->path[0] ? ": read " : "");
	if (t->cut && !t->path[0])	/* no file to name (LOW-V31) */
		(void)xv_tfield("Text", t->cut == 2 ? x : "cut at 16 KB", &rows,
		                win);
	else if (t->cut)
		xv_tpath("Text", t->cut == 2 ? x : "cut at 16 KB: the whole "
		         "text is in ", t->path, &rows, win);
	else if (!t->body[0])
		(void)xv_tfield("Text", "none", &rows, win);
	if (!t->body[0] || rows + 1 >= win)
		return rows;
	pk_line();
	pk_eol();
	rows++;
	/* the text's lines, wrapped; the window stops at its end */
	for (n = 0, s = t->body; *s; n++)
		s = xv_wline(s, pk_w - 4, &nb);
	if (v->mtop > n - (win - rows))
		v->mtop = n - (win - rows);
	if (v->mtop < 0)
		v->mtop = 0;
	for (k = 0, s = t->body; *s && rows < win; k++)
	{
		s = xv_wline(w = s, pk_w - 4, &nb);
		if (k < v->mtop)
			continue;
		pk_line();
		pk_txt("  ");
		xv_txtn(w, nb);
		pk_eol();
		rows++;
	}
	return rows;
}

/* A task's card (#320, one with no turn to read): what is known of it,
** built as the ref's card: the title whole, then a line per field; a
** memory's (#323) the same way */
static void
xv_draw_tcard(struct xv *v, int win)
{
	const struct xv_tq *q = &v->tq[v->tab - 4];
	const struct xv_task *t = q->ci >= 0 && q->ci < q->tl.n
	                          ? &q->tl.t[q->ci] : NULL;
	const char *s;
	char st[300], c[40], u[40];
	size_t nb;
	int rows = 0;

	if (t)
	{
		for (s = t->title; *s && rows < win / 2; rows++)
		{
			const char *w = s;

			s = xv_wline(w, pk_w - 4, &nb);
			pk_line();
			pk_set("1");
			pk_txt("  ");
			xv_txtn(w, nb);
			pk_eol();
		}
	}
	if (t && v->tab == 5)
		rows = xv_draw_mcard(v, t, win, rows);
	else if (t && v->tab == 6)
	{
		/* a schedule's (#687): the whole cron and its times */
		char nx[40], ex[40], lf[40], fi[60];

		snprintf(st, sizeof st, "%s  %s%s", t->stw, t->kind,
		         t->recur ? ", recurring" : "");
		xv_when(xv_iso(t->created), c, sizeof c);
		xv_when(t->next, nx, sizeof nx);
		xv_when(t->expires, ex, sizeof ex);
		xv_when(t->lastf, lf, sizeof lf);
		snprintf(fi, sizeof fi, t->noop > 0 ? "%lld (the last %lld no-op)"
		         : "%lld", t->fires, t->noop);
		(void)xv_tfield("Id", t->id, &rows, win);
		(void)xv_tfield("Status", st, &rows, win);
		(void)xv_tfield("Cron", t->cron, &rows, win);
		(void)xv_tfield("Schedule", t->label, &rows, win);
		(void)xv_tfield("Next", nx, &rows, win);
		(void)xv_tfield("Created", c, &rows, win);
		(void)xv_tfield("Expires", ex, &rows, win);
		(void)xv_tfield("Last fired", lf, &rows, win);
		(void)xv_tfield("Fires", fi, &rows, win);
		(void)xv_tfield("Reason", t->note, &rows, win);
		(void)xv_tfield("Turn", t->chapter[0] ? "enter reads it"
		                : "none to read", &rows, win);
	}
	else if (t)
	{
		snprintf(st, sizeof st, "%s%s%s", xv_tgrp[t->st],
		         xv_tlabel(t) ? "  " : "", xv_tlabel(t) ? t->label : "");
		xv_when(xv_iso(t->created), c, sizeof c);
		xv_when(t->updated, u, sizeof u);
		(void)xv_tfield("Id", t->id, &rows, win);
		(void)xv_tfield("Status", st, &rows, win);
		(void)xv_tfield("Doing", t->active, &rows, win);
		(void)xv_tfield("Note", t->note, &rows, win);
		(void)xv_tfield("Created", c, &rows, win);
		(void)xv_tfield("Updated", u, &rows, win);
		(void)xv_tfield("Blocked by", t->blocked, &rows, win);
		(void)xv_tfield("Group", t->group, &rows, win);
		(void)xv_tfield("URL", t->url, &rows, win);
		(void)xv_tfield("Turn", "none to read", &rows, win);
	}
	for (; rows < win; rows++)
	{
		pk_line();
		pk_eol();
	}
}

/* Memory's re-read interval; DCH_XRAY_MEVERY=<ms> lowers it (tests only) */
static long long
xv_mevery(void)
{
	const char *e = getenv("DCH_XRAY_MEVERY");
	long long ms = e ? atoll(e) : 0;

	return ms > 0 && ms < XV_MEVERY ? ms : XV_MEVERY;
}

/* The session runs: o has something to attach. */
static int
xv_live(const struct xv *v)
{
	return v->have && v->live && !(v->ended && !v->kid[XV_FK].rc);
}

/* The bar's cells for tabs lo..hi of w's nt (each its cells, the space
** before it not counted), a mark at the side with more. */
static int
xv_tabsw(const int *w, int nt, int lo, int hi)
{
	int k, s = lo > 0 ? 2 : 0;	/* the mark, the space after it */

	/* k < nt: hi is below nt already, said for the static analyzer */
	for (k = lo; k <= hi && k < nt; k++)
		s += w[k] + (k > lo);
	return s + (hi < nt - 1 ? 2 : 0);
}

/* The tab bar (NIT-V9): nt tabs, name[k] with count c[k] (-1 none), tab on
** current, no word ever cut: all of them with their counts, else all
** without (a "Search 50+" its first word), else tab on and as many whole
** neighbours as fit, a < or > at the side that has more. */
static void
xv_tabs(const char *const *name, const int *c, int nt, int on)
{
	char b[XV_NTAB][24];
	int w[XV_NTAB], lo = on, hi = on, k, cn;

	for (cn = 1; cn >= 0; cn--)
	{
		for (k = 0; k < nt; k++)
		{
			if (cn)
				w[k] = pk_cps(name[k]) + 2 +
				       (c[k] >= 0 ? snprintf(b[k], sizeof b[k], " %d",
				                             c[k]) : 0);
			else
				w[k] = snprintf(b[k], sizeof b[k], "%.*s",
				                (int)strcspn(name[k], " "), name[k]) + 2;
		}
		if (xv_tabsw(w, nt, 0, nt - 1) <= pk_w)
		{
			lo = 0;
			hi = nt - 1;
			break;
		}
	}
	/* cn -1: tab on, then a neighbour at a time, right and left in turn */
	for (k = 0; cn < 0 && k < 2 * nt; k++)
		if (k % 2 && lo > 0 && xv_tabsw(w, nt, lo - 1, hi) <= pk_w)
			lo--;
		else if (!(k % 2) && hi < nt - 1 &&
		         xv_tabsw(w, nt, lo, hi + 1) <= pk_w)
			hi++;
	if (lo > 0)
	{
		pk_set("2");
		pk_txt(pk_utf ? "\xe2\x80\xb9" : "<");
	}
	for (k = lo; k <= hi; k++)
		pk_tab(cn > 0 ? name[k] : b[k], cn > 0 ? c[k] : -1, k == on);
	if (hi < nt - 1)
	{
		pk_set("2");
		pk_txt(pk_utf ? " \xe2\x80\xba" : " >");
		pk_set("");
	}
}

static void
xv_draw(struct xv *v, int force)
{
	const char *tabn[XV_NTAB] = { "Prompts", "Refs", "Responses",
		"Search", "Tasks", "Memory", "Schedules" };
	static const char spin[] = "\xe2\xa0\x8b\xe2\xa0\x99\xe2\xa0\xb9"
		"\xe2\xa0\xb8\xe2\xa0\xbc\xe2\xa0\xb4\xe2\xa0\xa6\xe2\xa0\xa7"
		"\xe2\xa0\x87\xe2\xa0\x8f", ascii[] = "|/-\\";
	struct pkkey keys[12];
	const char *ret = pk_utf ? "\xe2\x86\xb5" : "enter", *ent;
	char b[1200], tmp[32], kt[32], sn[40];
	const char *tn[XV_NTAB];
	int nk = 0, kl, cap, k, n, busy, isref, cnt[XV_NTAB], tc[XV_NTAB], on = 0;
	unsigned drop;

	pk_begin(v->fd, v->color);
	if (pk_w < 24 || pk_h < 8)
	{
		pk_line();
		pk_fit(pk_w >= 30 ? "X-ray: the window is too small" : pk_w >= 16
		       ? "X-ray: too small" : "too small", pk_w);
		pk_eol();
		pk_fend(v->fd, force);
		return;
	}
#define XV_K(g, k, l) (keys[nk++] = (struct pkkey){ (k), (l), NULL, (g), 0 })
	isref = v->mode == XV_LIST && v->tab != 3 && xv_fref(v, b, sizeof b);
	if (v->filtering && v->mode == XV_READ)
	{
		XV_K(0, ret, "find");
		XV_K(3, "esc", "clear");
	}
	else if (v->filtering && v->tab == 3)
	{
		XV_K(0, ret, "search");
		XV_K(3, "esc", "cancel");
	}
	else if (v->filtering)
	{
		XV_K(0, ret, "done");
		if (isref || v->filt[0])
			XV_K(0, "^R", isref ? "look up" : "search");
		XV_K(3, "esc", "clear");
	}
	else if (v->strip && (v->mode == XV_READ || (v->mode == XV_LIST &&
	                                             v->tab == 2)))
	{
		XV_K(0, ret, "card");
		XV_K(3, pk_utf ? "\xe2\x86\x91\xe2\x86\x93" : "up/down", "move");
		XV_K(3, "esc", "close");
	}
	else if (v->mode == XV_CARD)
	{
		XV_K(0, ret, "read");
		XV_K(0, "f", "full scan");
		if (xv_live(v))
			XV_K(0, "o", "open session");
		XV_K(0, "p", v->scope == 2 ? "this project" : "other projects");
		XV_K(0, "y", "copy");
		XV_K(3, "esc", "back");
	}
	else if (v->mode == XV_TCARD)
	{
		const struct xv_tq *q = &v->tq[v->tab - 4];

		/* a memory's: its text scrolls, Enter reads its turn; a
		** schedule's: its turn */
		if (q->ci < q->tl.n && q->tl.t[q->ci].chapter[0] &&
		    (v->tab == 6 || (v->tab == 5 && q->tl.t[q->ci].ses)))
			XV_K(0, ret, "read");
		if (xv_live(v))
			XV_K(0, "o", "open session");
		if (v->tab == 5)
			XV_K(3, pk_utf ? "\xe2\x86\x91\xe2\x86\x93" : "up/down",
			     "scroll");
		XV_K(3, "esc", "back");
	}
	else if (v->mode == XV_READ)
	{
		if (xv_live(v))
			XV_K(0, "o", "open session");
		XV_K(0, "]", strcmp(xv_noun(v), "prompt") ? "next command"
		     : "next prompt");
		XV_K(0, "[", "previous");
		XV_K(0, "r", "refs");
		XV_K(3, "/", "find");
		if (v->rq[0])
			XV_K(0, "n/N", "next/previous match");
		XV_K(3, pk_utf ? "\xe2\x86\x91\xe2\x86\x93" : "up/down", "scroll");
		XV_K(3, "esc", "back");
	}
	else if (v->fail[0])
		XV_K(3, "q", "quit");
	else
	{
		/* Tasks, Schedules: a row with no turn opens its card (c too
		** on Schedules, #687); Memory: a card */
		ent = v->tab == 1 || v->tab == 5 ? "card" : "read";
		if ((v->tab == 4 || v->tab == 6) && (xv_vis(v), v->sel[v->tab] >= 0)
		    && v->sel[v->tab] < v->nvis &&
		    !v->tq[v->tab - 4].tl.t[v->vis[v->sel[v->tab]]].chapter[0])
			ent = "details";
		XV_K(0, ret, ent);
		if (v->tab == 6 && v->nvis)
			XV_K(0, "c", "card");
		if (v->tab != 1 && xv_live(v))
			XV_K(0, "o", "open session");
		if (v->tab == 2)
			XV_K(0, "r", "refs");
		XV_K(3, "/", v->tab == 3 ? "search" : "filter");
		if (isref || (v->tab != 3 && v->filt[0]))
			XV_K(0, "^R", isref ? "look up" : "search");
		if (v->tab != 1 && (xv_vis(v), v->nauto))
		{
			snprintf(tmp, sizeof tmp, "%d hidden", v->nauto);
			keys[nk++] = (struct pkkey){ "n", v->allauto
				? "hide auto rows" : "auto rows",
				v->allauto ? NULL : tmp, 2, 0 };
		}
		if ((v->tab == 0 || v->tab == 2) && (xv_vis(v), v->npeer))
		{
			snprintf(kt, sizeof kt, "%d hidden", v->npeer);
			keys[nk++] = (struct pkkey){ "a", v->hidepeer
				? "agent rows" : "hide agent rows",
				v->hidepeer ? kt : NULL, 2, 0 };
		}
		if (v->tab == 1 && (xv_vis(v), v->nk))
		{
			snprintf(kt, sizeof kt, "%d hidden", v->nk);
			keys[nk++] = (struct pkkey){ "a", v->allk
				? "hide files, commits, URLs"
				: "files, commits, URLs",
				v->allk ? NULL : kt, 2, 0 };
		}
		if (v->tab == 1 && v->ntmp)
		{
			snprintf(tmp, sizeof tmp, "%d hidden", v->ntmp);
			keys[nk++] = (struct pkkey){ "t", v->alltmp
				? "hide temp files" : "temp files",
				v->alltmp ? NULL : tmp, 2, 0 };
		}
		XV_K(3, pk_utf ? "\xe2\x87\xa5" : "tab", "tabs");
		XV_K(3, "q", "quit");
	}
#undef XV_K
	cap = pk_h >= 16 ? 2 : 1;
	for (drop = 0; (kl = pk_keys(keys, nk, drop, cap, 0)) < 0 && drop < 6;
	     drop = drop * 2 + 2)
		;
	if (kl < 0)
		kl = cap;
	v->win = pk_h - 5 - kl;
	if (v->win < 1)
		v->win = 1;

	/* the title, quiet; the session's directory faint on the right */
	pk_line();
	pk_set("1");
	pk_txt("X-ray  ");
	pk_set("");
	if (v->have && v->hl)
	{
		snprintf(b, sizeof b, "%.*s", v->hl, v->n);	/* "host:" */
		pk_txt(b);
	}
	pk_txt(v->have ? v->session : v->n);
	if (v->have && !v->live)
	{
		pk_set("2");
		pk_txt("  (ended)");
	}
	if (v->have && v->label[0] && pk_w - pk_col >= pk_cps(v->label) + 4)
	{
		pk_pad(pk_w - pk_cps(v->label));
		pk_set("2");
		pk_txt(v->label);
	}
	pk_eol();

	pk_line();
	if (v->mode == XV_CARD)
	{
		pk_set(pk_p(PK_ACCB));
		pk_txt(v->ctext);
		pk_set("2");
		snprintf(b, sizeof b, "  %s%s%s", v->full ? "full scan" : "light",
		         v->scope == 2 ? xv_dot() : "",
			         v->scope == 2 ? "other projects" : "");
		pk_txt(b);
	}
	else if (v->mode == XV_TCARD)
	{
		const struct xv_tq *q = &v->tq[v->tab - 4];

		pk_set(pk_p(PK_ACCB));
		pk_txt(v->tab == 6 ? "Schedule " : v->tab == 5 ? "Memory "
		       : "Task ");
		pk_txt(q->ci < q->tl.n ? q->tl.t[q->ci].id : "");
	}
	else if (v->mode == XV_READ)
	{
		/* where a turn has brought the reader: the list's number */
		if ((k = xv_rch(v, v->ranchor)) >= 0)
		{
			snprintf(b, sizeof b, "%d of %d  ", k + 1, v->nch);
			pk_set("2");
			pk_txt(b);
		}
		pk_set(pk_p(PK_ACCB));
		if (k >= 0)
			(void)xv_mark(&v->ch[k]);
		pk_txt(v->rtitle ? v->rtitle : "");
		xv_when(v->rat, b, sizeof b);
		pk_set("2");
		pk_txt("  ");
		pk_txt(b);
		if (v->rtrunc)
			pk_txt("  (truncated)");
	}
	else if (v->fail[0])
	{
		/* a host that refused us, lacks --xray or is gone (#188) */
		pk_set(pk_p(PK_RED));
		pk_txt(v->fail);
	}
	else if (v->have)
	{
		/* no tabs before the snapshot says Prompts or Commands (#295),
		** so with Responses or without (#290); Prompts, Responses: the
		** ones shown by default */
		if (strcmp(xv_noun(v), "prompt"))
			tabn[0] = "Commands";
		if ((v->tab == 2 && !xv_rtab(v)) ||
		    (v->tab >= 4 && !xv_ttab(v, v->tab - 4)))
			v->tab = 0;	/* a snapshot that says commands now; a
					** re-read with no row (#320) */
		n = xv_old(v);
		for (k = cnt[0] = cnt[2] = 0; k < v->nch; k++)
		{
			if (v->hidepeer && xv_xs(&v->ch[k]))
				continue;
			cnt[0] += (v->allauto || !xv_hid(&v->ch[k]));
			cnt[2] += !n && (v->allauto || !xv_hid(&v->ch[k])) && xv_item(v, k);
		}
		cnt[1] = v->nrf;
		/* Search: its turns once a search has answered (#457), + past
		** the cap (as a name: pk_tab counts with a number only) */
		cnt[3] = v->sdone && !v->smore ? xv_hturns(v) : -1;
		if (v->sdone && v->smore)
		{
			snprintf(sn, sizeof sn, "Search %d+", xv_hturns(v));
			tabn[3] = sn;
		}
		/* Tasks: the open ones, in progress and pending (#320) */
		cnt[4] = v->tq[0].tl.nst[0] + v->tq[0].tl.nst[1];
		cnt[5] = v->tq[1].tl.n;	/* Memory: every row (#323) */
		cnt[6] = v->tq[2].tl.nst[0];	/* Schedules: pending (#687) */
		for (k = n = 0; k < XV_NTAB; k++)
			if ((xv_ord[k] != 2 || xv_rtab(v)) &&
			    (xv_ord[k] < 4 || xv_ttab(v, xv_ord[k] - 4)))
			{
				if (xv_ord[k] == v->tab)
					on = n;
				tn[n] = tabn[xv_ord[k]];
				tc[n++] = cnt[xv_ord[k]];
			}
		xv_tabs(tn, tc, n, on);
	}
	pk_eol();

	pk_line();
	xv_label(v, b, sizeof b);
	pk_set("2");
	pk_txt(b);
	pk_eol();

	if (v->mode == XV_CARD)
		xv_draw_card(v, v->win);
	else if (v->mode == XV_TCARD)
		xv_draw_tcard(v, v->win);
	else if (v->mode == XV_READ)
		xv_draw_read(v, v->win);
	else if (v->fail[0])
		for (k = 0; k < v->win; k++)
		{
			pk_line();
			pk_eol();
		}
	else if (v->tab == 3)
		xv_draw_search(v, v->win);
	else if (v->tab >= 4)
		xv_draw_tasks(v, v->win);
	else if (v->tab == 2 && v->have && (xv_vis(v), v->nvis))
	{
		k = xv_strip_h(v);
		xv_draw_feed(v, v->win - k);
		if (k)
			xv_draw_strip(v, k);
	}
	else
	{
		int *sel = &v->sel[v->tab], *top = &v->top[v->tab], win = v->win;
		int g, g2, h1, h2, h3, i;

		xv_vis(v);
		n = v->nvis;
		/* Refs: what a ref is, one faint line over the rows */
		if (v->tab == 1 && v->nrf && win > 1)
		{
			pk_line();
			pk_set("2");
			snprintf(b, sizeof b, "  %s this session touched%s; %s opens a "
			         "card", v->allk ? "Every ref" : "Pull requests, issues, "
			         "tickets and terms", v->nrf && v->rf[0].last >= 0 ?
			         ", latest first" : "", ret);
			pk_txt(b);
			pk_eol();
			win--;
		}
		/* Refs: a faint line over the on-screen group, one over the
		** last reply's, one over the rest under them (#294); the
		** selection's row keeps its line in view */
		g = v->tab == 1 ? v->ngrp : 0;
		g2 = v->tab == 1 ? v->ngrp2 : 0;
		h1 = g > 0;
		h3 = g2 > g;
		h2 = (g > 0 || h3) && n > g2;
		if (*sel >= n)
			*sel = n - 1;
		if (*sel < 0)
			*sel = 0;
		i = *sel + h1 + (h3 && *sel >= g) + (h2 && *sel >= g2);
		xv_clamp(&i, top, n + h1 + h2 + h3, win);
		if (*top == i && *top &&
		    (*sel ? (h3 && *sel == g) || (h2 && *sel == g2) : h1 || h3))
			(*top)--;
		if (v->tab < 2)
			xv_selk(v);
		for (k = *top; k < *top + win; k++)
		{
			int p3 = g + h1, p2 = g2 + h1 + h3;

			i = k - h1 - (h3 && k > p3) - (h2 && k > p2);
			pk_line();
			if ((h1 && !k) || (h3 && k == p3) || (h2 && k == p2))
			{
				pk_set("2");
				pk_txt(h1 && !k ? "  on screen now" : h3 && k == p3
				       ? "  in the last reply" : "  the rest");
			}
			else if (i < n && v->tab == 0)
				xv_row_ch(&v->ch[v->vis[i]], v->vis[i],
				          i == *sel);
			else if (i < n)
				xv_row_ref(v, xv_rfat(v, v->vis[i]), i == *sel);
			else if (k == 0)
			{
				pk_set("2");
				pk_txt(!v->have ? "    Reading" : v->tab == 1 && v->nk &&
				       !v->allk ? "    Only files, commits and URLs: a "
				       "shows them" : v->tab == 1 && v->ntmp &&
				       !v->alltmp ? "    Only temp files: t shows them"
				       : v->tab != 1 && v->nauto && !v->allauto &&
				       v->npeer && v->hidepeer ? "    Only auto "
				       "and agent rows: n and a show them"
				       : v->tab != 1 && v->nauto && !v->allauto
				       ? "    Only auto rows: n shows them"
				       : v->tab != 1 && v->npeer && v->hidepeer
				       ? "    Only agent rows: a shows them"
				       : isref ? "    No match: ^R looks it up"
				       : v->filt[0] ? "    No match; ^R searches the "
				       "session for it" : v->tab == 0
				       ? strcmp(xv_noun(v), "prompt")
				       ? "    No commands" : "    No prompts"
				       : v->tab == 2 ? "    No responses yet: what the "
				       "agent answers at the end of each turn shows here"
				       : "    No refs yet: the files, "
				       "commits, pull requests, issues and URLs this session "
				       "touches show here");
				if (!v->have)
					pk_txt(xv_ell());
			}
			pk_eol();
		}
	}

	pk_rule();
	pk_line();
	/* Search's text has its own line (#291); the reader's find, its own */
	if (v->mode == XV_READ ? v->filtering || v->rq[0] : v->tab != 3 &&
	    (v->filtering || (v->mode == XV_LIST && v->filt[0])))
	{
		pk_set(pk_p(PK_ACC));
		pk_txt("/");
		pk_set("");
		pk_txt(v->mode == XV_READ ? v->rq : v->filt);
		if (v->filtering)
			pk_txt(pk_utf ? "\xe2\x96\x88" : "_");
		pk_txt("  ");
	}
	busy = v->kid[XV_CK].pid > 0 || v->kid[XV_RK].pid > 0 ||
	       v->kid[XV_QK].pid > 0 ||
	       (v->tab >= 4 && xv_tbusy(v, v->tab - 4) &&
	        !v->tq[v->tab - 4].re) ||
	       (!v->have && !v->fail[0]);
	if (busy)
	{
		int f = (int)(now_ms() / 100 % (pk_utf ? 10 : 4));

		snprintf(b, sizeof b, "%.*s ", pk_utf ? 3 : 1,
		         pk_utf ? spin + f * 3 : ascii + f);
		pk_set(pk_p(PK_ACC));
		pk_txt(b);
	}
	pk_set(!strcmp(v->status, "no refs in view") ? "2" : "");
	pk_txt(v->status[0] ? v->status : v->stop);
	pk_eol();
	pk_keys(keys, nk, drop, cap, 1);
	pk_fend(v->fd, force);
}

/* ---- keys ------------------------------------------------------------ */

/* A bare number typed as the filter (#454): Prompts and Responses select
** that prompt, the filter gone, so Enter reads it. 1 when it was one. */
static int
xv_jump(struct xv *v)
{
	size_t l = strlen(v->filt);
	int n, k;

	if (!l || l > 6 || strspn(v->filt, "0123456789") != l)
		return 0;
	n = atoi(v->filt);
	v->filt[0] = '\0';
	xv_vis(v);
	for (k = 0; k < v->nvis && v->vis[k] != n - 1; k++)
		;
	if (k < v->nvis)
	{
		v->sel[v->tab] = k;
		xv_selk(v);
	}
	else
		snprintf(v->status, sizeof v->status, "no %s %d in this list",
		         xv_noun(v), n);
	return 1;
}

/* The filter's prompt, as the picker's (#096); on Search the text to
** search, Enter searching it, Esc keeping the last one (#291); in the
** reader the text to find (#454). 1: c was the filter's. */
static int
xv_filt(struct xv *v, int c)
{
	int rd = v->mode == XV_READ, k;
	char *f = rd ? v->rq : v->tab == 3 ? v->q : v->filt;
	size_t l = strlen(f);

	if (!v->filtering)
	{
		if (c != '/')
			return 0;
		v->filtering = 1;
		v->status[0] = '\0';
		return 1;
	}
	if (!rd && v->tab == 3 && (c == '\r' || c == '\n'))
	{
		xv_search(v, v->q);
		return 1;
	}
	switch (c)
	{
	case '\r': case '\n': case PK_UP: case PK_DOWN: case '\t': case PK_BTAB:
		v->filtering = 0;	/* and on to the list, as the picker's */
		return c == '\r' || c == '\n' ? !rd && (v->tab == 0 ||
		       v->tab == 2) && xv_jump(v) : 0;
	case PK_ESC:
		snprintf(f, sizeof v->q, "%s", !rd && v->tab == 3 ? v->sq : "");
		v->filtering = 0;
		break;
	case 0x7f: case 8:
		if (!l)
		{
			v->filtering = 0;
			return 1;
		}
		while (l && ((unsigned char)f[--l] & 0xC0) == 0x80)
			;
		f[l] = '\0';
		break;
	case 0x15:	/* Ctrl-U */
		f[0] = '\0';
		break;
	default:
		if (c < 0x20 || c > 0xff)
			return 0;	/* the arrows: the list's */
		if (l + 1 < sizeof v->filt)
		{
			f[l] = (char)c;
			f[l + 1] = '\0';
		}
	}
	if (!rd && v->tab != 3)	/* Search's hits stay until it runs */
	{
		memset(v->sel, 0, sizeof v->sel);
		for (k = 0; k < 3; k++)
		{
			free(v->tq[k].sel);
			v->tq[k].sel = NULL;
		}
	}
	return 1;
}

/* OSC 52: the ref's text to the terminal's clipboard. */
static void
xv_copy(struct xv *v)
{
	static const char b64[] =
	    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
	const unsigned char *s = (const unsigned char *)v->ctext;
	size_t n = strlen(v->ctext), k, o = 0;
	char out[2200];

	o = (size_t)snprintf(out, sizeof out, "\033]52;c;");
	for (k = 0; k < n && o + 5 < sizeof out - 2; k += 3)
	{
		unsigned x = (unsigned)s[k] << 16 | (k + 1 < n ? s[k + 1] << 8 : 0)
		             | (k + 2 < n ? s[k + 2] : 0);

		out[o++] = b64[x >> 18 & 63];
		out[o++] = b64[x >> 12 & 63];
		out[o++] = k + 1 < n ? b64[x >> 6 & 63] : '=';
		out[o++] = k + 2 < n ? b64[x & 63] : '=';
	}
	out[o++] = '\a';
	pk_write(v->fd, out, o);
	snprintf(v->status, sizeof v->status, "copied %s", v->ctext);
}

/* o: leave the view for an attach to the session, xv_go naming it for the
** host (dch.c: dch --xray, the picker's x; the in-attach chord's view is
** already on it, so leaving is all). 1: leave. Ended: said, kept. */
static int
xv_open(struct xv *v)
{
	const char *in = getenv("DCH_SESSION");

	if (!v->have)
		return 0;
	/* a local one asked again: its end may not have reached the view */
	if (!xv_live(v) || (!v->hl && !name_live(v->session)))
		snprintf(v->status, sizeof v->status, "%s has ended: no session "
		         "to open", v->session);
	else if (in && *in)
		snprintf(v->status, sizeof v->status, "already inside session "
		         "%s; nesting disabled", in);
	else if ((size_t)snprintf(xv_go, sizeof xv_go, "%.*s%s", v->hl, v->n,
	                          v->session) >= sizeof xv_go)
	{
		xv_go[0] = '\0';
		snprintf(v->status, sizeof v->status, "name too long");
	}
	else
		return 1;
	return 0;
}

/* The a key's choice outlives the view (#406): one line, hide_agents=0|1, in
** dch's state dir (the one the session records live under). Hidden unless
** the file says 0; a failed write is dropped, the view goes on. */
static int
xv_peerfile(char *out, size_t sz, int create)
{
	char *s;

	if (dch_history_dir(out, sz, NULL, create) < 0 ||
	    !(s = strrchr(out, '/')) ||
	    snprintf(s, sz - (size_t)(s - out), "/xray-view") >=
	        (int)(sz - (size_t)(s - out)))
		return -1;
	return 0;
}

static int
xv_peerload(void)
{
	char p[PATH_MAX], b[32], *s;
	struct stat st;
	ssize_t n;
	int fd;

	/* a FIFO or a symlink there is the default, not a hang (LOW-AV2); a
	** dch/ others can write to is not believed either (LOW-VW1) */
	if (xv_peerfile(p, sizeof p, 0) < 0 || !(s = strrchr(p, '/')) ||
	    (*s = '\0', dch_dir_private(p) < 0) || (*s = '/') != '/' ||
	    (fd = open(p, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)) < 0)
		return 1;
	n = fstat(fd, &st) == 0 && S_ISREG(st.st_mode)
	    ? read(fd, b, sizeof b - 1) : -1;
	close(fd);
	if (n < 0)
		return 1;
	b[n] = '\0';
	b[strcspn(b, "\n")] = '\0';	/* first line, CRLF too (LOW-AV3) */
	n = (ssize_t)strlen(b);
	if (n && b[n - 1] == '\r')
		b[n - 1] = '\0';
	return strcmp(b, "hide_agents=0") != 0;
}

/* 0 saved or a write error (dropped); else why it is not kept (#418): -1 a
** state dir failed the owner / no group-or-other-write check (dch/ holds the
** file and its temp, sessions/ is checked by dch_history_dir), -2 the path
** is too long, -3 something is in the way of the temp file. */
static int
xv_peersave(int hide)
{
	char p[PATH_MAX] = "", t[PATH_MAX + 8], d[PATH_MAX], b[32], *s;
	int fd, n, ok;

	errno = 0;
	if (xv_peerfile(p, sizeof p, 1) < 0)
		return !p[0] ? 0 : strlen(p) >= sizeof p - 1 ||
		       errno == ENAMETOOLONG ? -2 : -1;
	if (snprintf(t, sizeof t, "%s.%ld", p, (long)getpid()) >= PATH_MAX)
		return -2;
	snprintf(d, sizeof d, "%s", p);
	if (!(s = strrchr(d, '/')))
		return 0;
	*s = '\0';
	errno = 0;
	if (dch_dir_private(d) < 0)
		return errno == ENAMETOOLONG ? -2 : -1;
	/* never through a planted symlink, 0600 as the other state files
	** (LOW-AV1, NIT-AV7) */
	unlink(t);
	if ((fd = open(t, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC,
	               0600)) < 0)
		return errno == ENAMETOOLONG ? -2 :
		       errno == EEXIST || errno == EISDIR || errno == ELOOP
		       ? -3 : 0;
	n = snprintf(b, sizeof b, "hide_agents=%d\n", hide);
	ok = write(fd, b, (size_t)n) == n;
	if (close(fd) != 0 || !ok || rename(t, p) < 0)
		unlink(t);
	return 0;
}

/* r (#454): the refs in text t (the lines shown) into rr by the screen's
** grammar, the lowest first, and their strip opened; none: said faint */
static void
xv_rrefs(struct xv *v, const char *t)
{
	struct xv_ref *a = v->sr;
	int n = v->nsr, c = v->asr;

	v->sr = v->rr;
	v->nsr = v->nrr;
	v->asr = v->arr;
	xv_screen_scan(v, t ? t : "");
	v->rr = v->sr;
	v->nrr = v->nsr;
	v->arr = v->asr;
	v->sr = a;
	v->nsr = n;
	v->asr = c;
	v->ssel = v->stp = 0;
	v->strip = v->nrr > 0;
	if (!v->nrr)
		snprintf(v->status, sizeof v->status, "no refs in view");
}

/* r in the reader: the text lines in the window */
static void
xv_rshown(struct xv *v)
{
	size_t n = 1;
	int k, e;
	char *t, *p;

	if (v->rtop > v->nrl - v->win)
		v->rtop = v->nrl - v->win;
	if (v->rtop < 0)
		v->rtop = 0;
	e = v->rtop + v->win < v->nrl ? v->rtop + v->win : v->nrl;
	for (k = v->rtop; k < e; k++)
		n += v->rl[k].n + 1;
	if ((p = t = malloc(n)) != NULL)
	{
		for (k = v->rtop; k < e; k++)
			if (v->rl[k].kind)
			{
				memcpy(p, v->rl[k].s, v->rl[k].n);
				p += v->rl[k].n;
				*p++ = '\n';
			}
		*p = '\0';
	}
	xv_rrefs(v, t);
	free(t);
}

/* r on a Responses item: its answer's lines as xv_feed1 shows them.
** shortcut: the newest cut item's whole held text (its head may show only
** in part); exact when its head and tail always fit */
static void
xv_ishown(struct xv *v)
{
	int i = v->sel[2], l;
	const char *s, *e;
	size_t n;
	char *t;

	xv_vis(v);
	if (i < 0 || i >= v->nvis)
	{
		xv_rrefs(v, "");
		return;
	}
	s = e = v->ch[v->vis[i]].resp;
	if (i == 0 && v->ch[v->vis[i]].rcut)
		e += strlen(e);
	else
		for (l = 0; *e && l < XV_ALINES; l++)
			e = xv_wline(e, pk_w - 8, &n);
	t = strndup(s, (size_t)(e - s));
	xv_rrefs(v, t);
	free(t);
}

/* / in the reader (#454): the next line holding the text found from line
** k on, d 1 down, -1 up; -1 none */
static int
xv_rfind(const struct xv *v, int k, int d)
{
	for (; v->rq[0] && k >= 0 && k < v->nrl; k += d)
		if (v->rl[k].kind && xv_in(v->rl[k].s, v->rl[k].n, v->rq))
			return k;
	return -1;
}

/* The reader to match line k, near the window's top, said with its place
** among the matches; none (k -1): why */
static void
xv_rgo(struct xv *v, int k, const char *none)
{
	int i, n = 0, at = 0;

	if (k < 0)
	{
		snprintf(v->status, sizeof v->status, "%s: %s", v->rq, none);
		return;
	}
	for (i = 0; (i = xv_rfind(v, i, 1)) >= 0; i++)
	{
		n++;
		at += i <= k;
	}
	v->rcur = k;
	v->rtop = k > 2 ? k - 2 : 0;
	snprintf(v->status, sizeof v->status, "%s: %d of %d", v->rq, at, n);
}

/* One key. 1: leave the view. */
static int
xv_key(struct xv *v, int c)
{
	int *sel, n, k, i;
	int st = v->strip && (v->mode == XV_READ || (v->mode == XV_LIST &&
	                                             v->tab == 2));

	if (c == 3 || c == 4)
		return 1;
	if (v->fail[0])
		return c == 'q' || c == PK_ESC;
	if (!st && (v->mode == XV_LIST || v->mode == XV_READ) && xv_filt(v, c))
		return 0;
	v->status[0] = '\0';
	/* r's strip (#454): j k move, Enter opens the card (q comes back to
	** it), q Esc close it; any other key closes it and is its own */
	if (st)
	{
		const struct xv_ref *r;

		if (v->ssel >= v->nrr)
			v->ssel = v->nrr - 1;
		if (v->ssel < 0)
			v->ssel = 0;
		switch (c)
		{
		case 'j': case PK_DOWN: v->ssel++; return 0;
		case 'k': case PK_UP: v->ssel--; return 0;
		case 'q': case PK_ESC: v->strip = 0; return 0;
		case '\r': case '\n':
			r = xv_sref(v, v->ssel);
			k = v->back;
			xv_card(v, r->key, r->text, r->full, 0, 1);
			/* the reader's own back waits under the card's */
			if (v->mode == XV_CARD && v->back == XV_READ)
				v->sback = k == XV_CARD ? XV_LIST : k;
			return 0;
		}
		v->strip = 0;
		if (xv_filt(v, c))
			return 0;
	}
	if (v->mode == XV_READ)
	{
		k = v->redge;
		v->redge = 0;
		switch (c)
		{
		case 'r': xv_rshown(v); break;
		case '\r': case '\n':
			v->rcur = -1;
			if (v->rq[0])
				xv_rgo(v, xv_rfind(v, v->rtop + 1, 1),
				       "no match below the top line");
			break;
		case 'n':
			if (v->rq[0])
				xv_rgo(v, xv_rfind(v, v->rcur < 0 ? v->rtop + 1
				                   : v->rcur + 1, 1), "no more below");
			break;
		case 'N':
			if (v->rq[0])
				xv_rgo(v, xv_rfind(v, v->rcur < 0 ? v->rtop - 1
				                   : v->rcur - 1, -1), "no more above");
			break;
		case PK_ESC:
			if (v->rq[0])
			{
				v->rq[0] = '\0';
				v->rcur = -1;
			}
			else
				xv_back(v);
			break;
		case 'j': case PK_DOWN: xv_scroll(v, 1, k); break;
		case 'k': case PK_UP: xv_scroll(v, -1, k); break;
		case ' ': case PK_PGDN: xv_scroll(v, v->win, k); break;
		case 'b': case PK_PGUP: xv_scroll(v, -v->win, k); break;
		case 'g': case PK_HOME: xv_scroll(v, -v->nrl - 1, k); break;
		case 'G': case PK_END: xv_scroll(v, v->nrl + 1, k); break;
		case ']': xv_turn(v, 1, 0); break;
		case '[': xv_turn(v, -1, 0); break;
		case 'o':
			return xv_open(v);
		case 'q': case 'h': case PK_LEFT: case 0x7f:
			xv_back(v);
			break;
		}
		return 0;
	}
	if (v->mode == XV_TCARD)
	{
		struct xv_tq *q = &v->tq[v->tab - 4];
		const struct xv_task *t = q->ci < q->tl.n ? &q->tl.t[q->ci]
		                          : NULL;

		if (c == 'o')
			return xv_open(v);
		if (c == 'q' || c == PK_ESC || c == 'h' || c == PK_LEFT ||
		    c == 0x7f)
		{
			xv_back(v);
			return 0;
		}
		/* a schedule's (#687): Enter reads its turn */
		if (v->tab == 6 && t && t->chapter[0] &&
		    (c == '\r' || c == '\n'))
			xv_read(v, t->chapter, NULL);
		if (v->tab != 5 || !t)
			return 0;
		/* a memory's (#323): its text scrolls (xv_draw_mcard clamps);
		** Enter reads the turn this session wrote or loaded it in */
		switch (c)
		{
		case 'j': case PK_DOWN: v->mtop++; break;
		case 'k': case PK_UP: v->mtop--; break;
		case ' ': case PK_PGDN: v->mtop += v->win; break;
		case 'b': case PK_PGUP: v->mtop -= v->win; break;
		case 'g': case PK_HOME: v->mtop = 0; break;
		case 'G': case PK_END: v->mtop = 1 << 30; break;
		case '\r': case '\n':
			if (t->ses && t->chapter[0])
				xv_read(v, t->chapter, NULL);
			else
				snprintf(v->status, sizeof v->status,
				         "%sno %s to read", t->ses ? "" :	/* LOW-V30 */
				         "this session did not write or load "
				         "it: ", xv_noun(v));
			break;
		}
		return 0;
	}
	if (v->mode == XV_CARD)
	{
		const struct xv_men *m;

		/* xv_draw_card clamps too, but a too-small window skips it */
		if (v->csel >= v->nm)
			v->csel = v->nm - 1;
		if (v->csel < 0)
			v->csel = 0;
		m = v->csel < v->nm ? &v->m[v->csel] : NULL;
		switch (c)
		{
		case 'j': case PK_DOWN: xv_cmove(v, 1, 0); xv_cpin(v); break;
		case 'k': case PK_UP: xv_cmove(v, -1, 0); xv_cpin(v); break;
		case PK_PGDN: xv_cmove(v, v->win / 2, 0); xv_cpin(v); break;
		case PK_PGUP: xv_cmove(v, -(v->win / 2), 0); xv_cpin(v); break;
		case 'g': case PK_HOME: xv_cmove(v, 0, 1); xv_cpin(v); break;
		case 'G': case PK_END: xv_cmove(v, INT_MAX, 1); xv_cpin(v); break;
		case 'f':
			xv_card(v, v->ckey, v->ctext, v->cref, 1, v->scope);
			break;
		case 'o':
			return xv_open(v);
		case 'p':	/* o before 1.61 (#227); o opens, as everywhere */
			xv_card(v, v->ckey, v->ctext, v->cref, 1, v->scope == 2 ? 1 : 2);
			break;
		case 'y':
			xv_copy(v);
			break;
		case '\r': case '\n':
			/* a turn's header opens the reader at the turn (#478); this
			** project's other session's: in its reader (#479), by
			** name while it runs, else by its id */
			free(v->rn);
			v->rn = NULL;
			if (m && m->chapter[0] && m->same &&
			    !(v->rn = strdup(m->sid[0] && !m->live ? m->sid
			                     : m->session)))
				snprintf(v->status, sizeof v->status,
				         "X-ray: out of memory");
			else if (m && m->chapter[0] && (m->same ||
			         !strcmp(m->session, v->session)))
			{
				xv_read(v, m->chapter, v->chdr ? NULL : v->ctext);
				if (m->same)
					snprintf(v->status, sizeof v->status, "%s, %s",
					         m->session, m->ttitle[0] ? m->ttitle
					         : "its prompt");
			}
			else if (m)
				snprintf(v->status, sizeof v->status, "%s",
				         m->chapter[0] ? "another session's prompt: "
				         "open its X-ray" : "no prompt for that line");
			break;
		case 'q': case PK_ESC: case 'h': case PK_LEFT: case 0x7f:
			xv_back(v);
			break;
		}
		return 0;
	}
	/* the list */
	if (c == '\t' || c == PK_RIGHT || c == PK_BTAB || c == PK_LEFT ||
	    c == 'l' || c == 'h')
	{
		/* the next tab as drawn, or the previous one; Responses only
		** where it is a tab (#290) */
		n = c == '\t' || c == PK_RIGHT || c == 'l' ? 1 : XV_NTAB - 1;
		for (k = 0; k < XV_NTAB - 1 && xv_ord[k] != v->tab; k++)
			;
		do
			k = (k + n) % XV_NTAB;
		while ((xv_ord[k] == 2 && !xv_rtab(v)) ||
		       (xv_ord[k] >= 4 && !xv_ttab(v, xv_ord[k] - 4)));
		/* Search opens typing until a search is made (#291); typing
		** there ends on leaving it */
		if (v->tab == 3 || xv_ord[k] == 3)
			v->filtering = xv_ord[k] == 3 && !v->sq[0];
		v->tab = xv_ord[k];
		if (v->tab == 1)
			xv_screen_load(v);
		/* a running session's list, re-read (the follow's is current) */
		if (v->poll > 1 && v->tab == 4 && xv_live(v))
			xv_tasks(v, 0);
		return 0;
	}
	if (c == 'q')
		return 1;
	if (c == 0x12)	/* Ctrl-R */
	{
		if (v->tab == 3)
			xv_search(v, v->q);
		else
			xv_lookup(v);
		return 0;
	}
	if (c == PK_ESC)
	{
		if (v->filt[0] && v->tab != 3)
			v->filt[0] = '\0';
		else
			return 1;
		return 0;
	}
	if (v->tab == 1 && c == 't')
	{
		v->alltmp = !v->alltmp;
		return 0;
	}
	if (v->tab == 1 && c == 'a')
	{
		v->allk = !v->allk;
		return 0;
	}
	if ((v->tab == 0 || v->tab == 2) && (c == 'n' || c == 'a'))
	{
		/* Prompts, Responses: the selection stays on its chapter, or
		** the next older one shown (newest first, #358), on the first
		** row too (#383); none shown before: the newest */
		xv_vis(v);
		k = v->sel[v->tab] >= 0 && v->sel[v->tab] < v->nvis
		    ? v->vis[v->sel[v->tab]] : v->nch;
		if (c == 'n')
			v->allauto = !v->allauto;
		else
		{
			v->hidepeer = !v->hidepeer;	/* #360 */
			if ((n = xv_peersave(v->hidepeer)) < 0 && !v->peernote++)
				snprintf(v->status, sizeof v->status, "a not saved: "
				         "%s", n == -1 ? "dir unsafe" : n == -2 ?
				         "path long" : "temp taken");
		}
		xv_vis(v);
		for (n = 0; n < v->nvis && v->vis[n] > k; n++)
			;
		v->sel[v->tab] = n;
		xv_selk(v);
		return 0;
	}
	if (v->tab != 1 && c == 'o')
		return xv_open(v);
	if (v->tab == 2 && c == 'r')
	{
		xv_ishown(v);
		return 0;
	}
	sel = &v->sel[v->tab];
	xv_vis(v);
	n = v->nvis;
	/* Schedules (#687): Enter reads a row's turn, so c opens its card */
	if (v->tab == 6 && c == 'c' && *sel >= 0 && *sel < n)
	{
		v->tq[2].ci = v->vis[*sel];
		v->back = v->mode;
		v->mode = XV_TCARD;
		return 0;
	}
	switch (c)
	{
	case 'j': case PK_DOWN: (*sel)++; break;
	case 'k': case PK_UP: (*sel)--; break;
	case PK_PGDN: *sel += v->tab == 2 || v->tab == 3 ? xv_page(v, 1)
	                      : v->win; break;
	case PK_PGUP: *sel += v->tab == 2 || v->tab == 3 ? xv_page(v, -1)
	                      : -v->win; break;
	case 'g': case PK_HOME: *sel = 0; break;
	case 'G': case PK_END: *sel = n - 1; break;
	case '\r': case '\n':
		if (*sel < 0 || *sel >= n)
			break;
		k = v->vis[*sel];
		if (v->tab == 3 && v->hit[k].chapter[0])
		{
			xv_read(v, v->hit[k].chapter, v->sq);	/* at it */
			if (v->hit[k].snippet[0])
				v->rsnip = strdup(v->hit[k].snippet);
			/* an earlier hit of the turn with its snippet is in an
			** entry before it (LOW-V29) */
			for (v->rskip = v->rtwins = 0, i = 0; i < v->nh; i++)
				if (v->hit[k].off >= 0 &&
				    !strcmp(v->hit[i].chapter,
				            v->hit[k].chapter) &&
				    !strcmp(v->hit[i].snippet,
				            v->hit[k].snippet))
				{
					v->rtwins++;
					v->rskip += v->hit[i].off <
					            v->hit[k].off;
				}
			v->roff = v->hit[k].off;
			v->rwho = v->hit[k].who[0];
			v->rtool = strdup(v->hit[k].tool);
		}
		else if (v->tab == 3)
			snprintf(v->status, sizeof v->status, "this place is "
			         "outside the %ss X-ray can open", xv_noun(v));
		else if (v->tab == 0)
			xv_read(v, v->ch[k].anchor, NULL);
		else if (v->tab == 4 && v->tq[0].tl.t[k].chapter[0])
			/* the turn that last changed it, at its title if said */
			xv_read(v, v->tq[0].tl.t[k].chapter,
			        v->tq[0].tl.t[k].title);
		else if (v->tab == 6 && v->tq[2].tl.t[k].chapter[0])
			/* a schedule's last turn (#687) */
			xv_read(v, v->tq[2].tl.t[k].chapter, NULL);
		else if (v->tab >= 4)
		{
			/* a task with no turn, a memory (#323): its card */
			v->tq[v->tab - 4].ci = k;
			v->mtop = 0;
			v->back = v->mode;
			v->mode = XV_TCARD;
		}
		else if (v->tab == 2)
		{
			/* at the answer: its start (entries carry no offset); a
			** dropped one, or none yet, at the turn's end */
			n = (int)strcspn(v->ch[k].resp, "\n");
			xv_read(v, v->ch[k].anchor, NULL);
			v->rend = 1;
			if (v->ch[k].resp[0])
				v->rans = strndup(v->ch[k].resp, n < 60 ? (size_t)n : 60);
		}
		else
		{
			const struct xv_ref *r = xv_rfat(v, k);

			xv_card(v, r->key, r->text, r->full, 0, 1);
		}
		break;
	}
	xv_selk(v);
	return 0;
}

/* ---- the loop -------------------------------------------------------- */

static void
xv_on_cont(int sig)
{
	(void)sig;
	xv_cont = 1;
	tty_conts++;
}

static void
xv_free(struct xv *v)
{
	int k;

	for (k = 0; k < XV_NKIDS; k++)
		xv_stop(&v->kid[k], k);
	xv_drop(v);
	xv_card_free(v);
	xv_read_free(v);
	xv_sr_free(v);
	xv_hits_free(v);
	free(v->hit);
	for (k = 0; k < 3; k++)
	{
		xv_tl_free(&v->tq[k].tl);
		xv_tl_free(&v->tq[k].tn);
		free(v->tq[k].sel);
	}
	xv_sfree(v->session);
	xv_sfree(v->sid);
	xv_sfree(v->corpus);
	xv_sfree(v->reason);
	xv_sfree(v->why);
	xv_sfree(v->label);
	xv_sfree(v->ckey);
	xv_sfree(v->ctext);
	xv_sfree(v->cref);
	xv_sfree(v->rwant);
	free(v->ranchor);
	free(v->rturn);
	free(v->rn);
	free(v->ch);
	free(v->rf);
	free(v->vis);
	free(v->d);
	free(v->m);
	free(v->e);
	free(v->rl);
	free(v->sr);
	for (k = 0; k < v->nrr; k++)
		xv_ref_free(&v->rr[k]);
	free(v->rr);
	for (k = 0; k < v->nlr; k++)
		xv_ref_free(&v->lr[k]);
	free(v->lr);
	free(v->selk);
	free(v->sela);
	free(v->selp);
	free(v->cses);
	free(v->rans);
	free(v->rsnip);
	free(v->rtool);
	free(v);
}

/* Inside a bracketed paste after the n bytes at c (#230): in, then the last
** ESC[200~ (1) or ESC[201~ (0) in them. Pure. */
int
xv_paste_in(const unsigned char *c, size_t n, int in)
{
	size_t i;

	for (i = 0; i + 6 <= n; i++)
		if (!memcmp(c + i, "\033[20", 4) && c[i + 5] == '~' &&
		    (c[i + 4] == '0' || c[i + 4] == '1'))
			in = c[i + 4] == '0';
	return in;
}

/* Keys on fd typed at the view, never for the session (#230, MED-V20):
** what comes until a 100 ms pause (1 s inside a bracketed paste, to its
** end) is read and dropped: an arrow key, a UTF-8 character, a paste of any
** size, a second or held o. any: the first read waits (the failed chord
** view's any key). The last 5 bytes carry over for a marker cut across two
** reads. select, not poll: macOS's poll says POLLNVAL on /dev/tty, the
** view's fd. shortcut: input paused longer than that, or still coming 2 s
** on, leaves its rest to the session; a clock-free end needs the paste's
** own end marker, which an unbracketed paste does not have */
void
xv_drop_keys(int fd, int any)
{
	unsigned char c[4096 + 5];
	long long t0 = any ? 0 : xr_now_ms();
	size_t k = 0;
	ssize_t r;
	int in = 0;

	for (;; any = 0)
	{
		fd_set rs;
		struct timeval tv = { in, in ? 0 : 100000 };

		FD_ZERO(&rs);
		FD_SET(fd, &rs);
		if ((!any && select(fd + 1, &rs, NULL, NULL, &tv) <= 0) ||
		    (r = read(fd, c + k, sizeof c - k)) <= 0)
			break;
		if (!t0)
			t0 = xr_now_ms();
		k += (size_t)r;
		in = xv_paste_in(c, k, in);
		if (k > 5)
		{
			memmove(c, c + k - 5, 5);
			k = 5;
		}
		if (xr_now_ms() - t0 > 2000)
			break;
	}
}

/* The view's follow (#448): its lines carry Tasks, Memory and Schedules
** (#687), but for a host that refused that (v->poll: then without
** Schedules, then alone). 0, -1 with the status said. */
static int
xv_follow(struct xv *v)
{
	char *av[] = { v->n, "--follow", "--tasks", "--memory", "--schedules",
	               "--json", NULL };
	static const int na[] = { 6, 5, 3 };	/* by v->poll */
	int n = na[v->poll];

	av[n - 1] = "--json";
	av[n] = NULL;
	return xv_start(v, XV_FK, av, n, xv_fline);
}

int
xv_run(int fd, const char *n, char *err, size_t errsz)
{
	struct xv *v = calloc(1, sizeof *v);
	struct sigaction sa, ocont;
	struct termios tio;
	int k, rc = 0, tio_ok;

	err[0] = xv_go[0] = '\0';
	if (!v)
	{
		snprintf(err, errsz, "dch: X-ray failed on this session");
		return 3;
	}
	v->fd = fd;
	v->hidepeer = xv_peerload();
	v->tab = xv_tab;
	v->color = pk_colors(0);
	snprintf(v->n, sizeof v->n, "%s", n);
	v->hl = xv_remote(n) ? (int)strcspn(n, ":") + 1 : 0;
	for (k = 0; k < XV_NKIDS; k++)
	{
		v->kid[k].fd = v->kid[k].efd = -1;
		v->kid[k].rc = -1;
	}
	v->blk = -1;
	if (xv_follow(v) < 0)
	{
		snprintf(err, errsz, "dch: %s", v->status);
		xv_free(v);
		return 3;
	}
	pk_pn = 0;
	/* stopped and continued (#255): the view's modes back, a whole frame
	** over the shell's lines. No SA_RESTART: the wait wakes at once */
	tio_ok = tcgetattr(fd, &tio) == 0;
	xv_cont = 0;
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = xv_on_cont;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGCONT, &sa, &ocont);
	for (;;)
	{
		struct timeval tv;
		fd_set rs;
		int mx = fd, force = pk_winch, c, busy, r;

		(void)xv_late(0);
		pk_winch = 0;
		/* a stop in the tcsetattr returns EINTR on the fg (macOS); from
		** the background it stops on SIGTTOU until the fg, so the frame
		** waits for the terminal. In the chord's child the attach sets
		** the same modes (attach.c xray_overlay) */
		if (xv_cont)
		{
			force = 1;
			do
			{
				xv_cont = 0;
				r = tio_ok ? tcsetattr(fd, TCSADRAIN, &tio) : 0;
			} while ((r < 0 && errno == EINTR) || xv_cont);
		}
		if (v->mode == XV_LIST && v->tab == 1 && now_ms() - v->scr_at >= 1000)
			xv_screen_load(v);
		/* the lists' own children, for a host whose --follow carries
		** neither (#448). Tasks (#320): asked once the snapshot says
		** prompts, so the first paint never waits; a running session's
		** again after --follow has said something, at most every
		** XV_TEVERY while shown (a whole transcript read each time),
		** else less often */
		if (v->poll > 1 && xv_rtab(v) && !v->tq[0].no &&
		    v->kid[XV_TK].pid <= 0 &&
		    (!v->tq[0].at || (v->tq[0].fol && xv_live(v) && now_ms() -
		     v->tq[0].at >= (v->tab == 4 ? 1 : 6) * XV_TEVERY)))
			xv_tasks(v, 0);
		/* Memory (#323): the same start; again every XV_MEVERY while
		** shown, a transcript line or not: memory files change apart
		** from it (LOW-V28) */
		if (v->poll > 1 && xv_rtab(v) && !v->tq[1].no &&
		    v->kid[XV_MK].pid <= 0 &&
		    (!v->tq[1].at || (xv_live(v) &&
		     v->tab == 5 && now_ms() - v->tq[1].at >= xv_mevery())))
			xv_tasks(v, 1);
		/* Schedules (#687): one query, for a host whose follow carries
		** none (its live changes need a dch that streams them) */
		if (v->poll && xv_rtab(v) && !v->tq[2].at)
			xv_tasks(v, 2);
		xv_draw(v, force);
		FD_ZERO(&rs);
		FD_SET(fd, &rs);
		busy = !v->have && !v->fail[0];
		for (k = 0; k < XV_NKIDS; k++)
		{
			if (v->kid[k].fd >= 0)
				FD_SET(v->kid[k].fd, &rs);
			if (v->kid[k].efd >= 0)
				FD_SET(v->kid[k].efd, &rs);
			mx = v->kid[k].fd > mx ? v->kid[k].fd : mx;
			mx = v->kid[k].efd > mx ? v->kid[k].efd : mx;
			busy |= k != XV_FK && v->kid[k].pid > 0;
		}
		busy |= v->blk >= 0;
		/* the spinner's frames while a query runs, else a tick for the
		** screen */
		tv.tv_sec = 0;
		tv.tv_usec = busy ? 100000 : 500000;
		if (select(mx + 1, &rs, NULL, NULL, &tv) < 0)
		{
			if (errno == EINTR)
				continue;	/* a resize */
			snprintf(err, errsz, "dch: X-ray: %s", strerror(errno));
			rc = 3;
			break;
		}
		if (xv_pump(v, XV_FK, &rs))
		{
			struct xv_kid *f = &v->kid[XV_FK];

			/* a block cut short: what came of it stands as a child's
			** failed answer's would */
			if (v->blk >= 0)
				xv_tend(v, v->blk, 1, "");
			v->blk = -1;
			/* a host whose dch refused the follow with the lists (an
			** older dch: "--schedules and --tasks", "--tasks and
			** --follow", or it does not know them): the follow with
			** fewer, then alone, and the lists' own children */
			if (!v->have && v->poll < 2 &&
			    (strstr(f->err, "--follow") ||
			     strstr(f->err, "--schedules") ||
			     strstr(f->err, "does not know")))
			{
				v->poll++;
				if (xv_follow(v) == 0)
					continue;
				snprintf(err, errsz, "dch: %s", v->status);
				rc = 3;
				break;
			}
			if (!v->have)
			{
				/* no snapshot: the session's answer is the view's */
				snprintf(err, errsz, "%s", f->err[0] ? f->err
				         : "dch: X-ray failed on this session");
				rc = f->rc ? f->rc : 3;
				/* ctl's own exits, a remote view's only: the host's
				** gate refused us, it lacks --xray, it is gone. Said
				** in place of the tabs until the user leaves (#188) */
				if (rc != DCH_EXIT_REFUSED && rc != DCH_EXIT_UNSUPPORTED &&
				    rc != DCH_EXIT_UNREACHABLE)
					break;
				snprintf(v->fail, sizeof v->fail, "%s", err);
			}
			else
			{
				v->ended = 1;
				if (f->rc)
					snprintf(v->stop, sizeof v->stop, "live updates "
					         "stopped (reopen the view for them): %s",
					         f->err);
				else if (v->live)
					snprintf(v->stop, sizeof v->stop, "the "
					         "session ended; this is its last state");
			}
		}
		/* a host's usage refusal: the view checked the ref with this
		** dch's grammar, so the host's dch is another (#263) */
		if (xv_pump(v, XV_CK, &rs) && v->kid[XV_CK].rc)
			snprintf(v->status, sizeof v->status, "%s%s",
			         v->hl && v->kid[XV_CK].rc == 2 ? "the host's dch "
			         "takes no such ref (an older dch?): " : "",
			         v->kid[XV_CK].err[0] ? v->kid[XV_CK].err
			         : "X-ray: the scan failed");
		if (xv_pump(v, XV_SK, &rs))
			xv_screen_done(v);
		if (xv_pump(v, XV_QK, &rs))
			xv_qend(v);
		if (xv_pump(v, XV_TK, &rs))
			xv_tend(v, 0, v->kid[XV_TK].rc, v->kid[XV_TK].err);
		if (xv_pump(v, XV_MK, &rs))
			xv_tend(v, 1, v->kid[XV_MK].rc, v->kid[XV_MK].err);
		if (xv_pump(v, XV_HK, &rs))
			xv_tend(v, 2, v->kid[XV_HK].rc, v->kid[XV_HK].err);
		if (xv_pump(v, XV_RK, &rs) && v->mode == XV_READ)
		{
			const char *e = v->kid[XV_RK].err, *w = xv_noun(v);

			/* the engine's raw refusal is no sentence for the status
			** line (#493) */
			if (strstr(e, "no x-ray corpus"))
				e = "that session's transcript is gone";

			/* the engine's "chapter gone" stays for --json clients;
			** the view says it in its own word (#295) */
			if (strstr(e, "chapter gone") && (v->rturn || !v->rloaded))
			{
				snprintf(v->status, sizeof v->status,
				         "X-ray: that %s is gone", w);
				if (!v->rturn)
					xv_back(v);
			}
			/* a turn that got no chapter: said, the reader stays */
			else if (v->rturn)
				snprintf(v->status, sizeof v->status, e[0]
				         ? "could not read that %s: %s"
				         : "could not read that %s: no such %s", w,
				         e[0] ? e : w);
			else if (!v->rloaded)
			{
				if (e[0])
					snprintf(v->status, sizeof v->status, "%s", e);
				else
					snprintf(v->status, sizeof v->status,
					         "X-ray: no such %s", w);
				xv_back(v);
			}
			free(v->rturn);
			v->rturn = NULL;
		}
		if (!FD_ISSET(fd, &rs))
			continue;
		if ((c = pk_key(fd)) < 0 || xv_key(v, c))
			break;
	}
	sigaction(SIGCONT, &ocont, NULL);
	/* o (LOW-V12, MED-V20): what is unread on the terminal or still coming
	** (a second o, a held one, Enter) was typed at the view. Before
	** xv_free: its children's stop can take 300 ms each, and the pause
	** counts from the o, not from after it (a key typed then was lost) */
	if (xv_go[0])
		xv_drop_keys(fd, 0);
	xv_free(v);
	pk_pn = 0;
	return rc;
}
