/*
** The X-ray view (#175, XR5a): `dch --xray <n>` with no --json, and the
** picker's X. Three tabs over one session, Chapters, Refs and On screen; a
** ref's card; a chapter's reader. Drawn with the picker's renderer, on the
** terminal its host (dch.c) made raw and put on the alternate screen.
**
** Every answer is the engine's own --json: dch.c's xv_spawn forks this
** process and runs xray_main in the child with --follow, --ref or --chapter
** and --json, stdout into a pipe, and the view reads the NDJSON with json.c.
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
#define XV_ALPHA     "asdfghjklzxcvbnmwertyuiop"	/* hint labels: no q */

enum { XV_LIST, XV_CARD, XV_READ };
enum { XV_FK, XV_CK, XV_RK, XV_SK, XV_NKIDS };	/* follow, card, reader,
						** a remote screen (#188) */

static char xv_none[1];	/* "": a field absent, null, or out of memory */
static volatile pid_t xv_pids[XV_NKIDS];	/* for xv_kill */
int xv_tab;	/* the tab a view opens on (the in-attach chord's: On screen) */

struct xv;
struct xv_kid
{
	pid_t pid;
	int fd, efd, skip, rc;	/* skip: a line past XV_LINE_MAX */
	char *buf;
	size_t len, cap;
	char err[300];
	void (*line)(struct xv *v, const struct json_parser *p);
};

struct xv_ch
{
	char *anchor, *title, *kind;
	long long at, offset;
	int error;
};

struct xv_ref
{
	char *key, *shape, *text, *title;
	long long count;
	int defined, underline;
};

struct xv_def
{
	char *session, *title, *passage, *rule;
	long long at;
};

struct xv_men
{
	char *session, *line, *chapter;
	long long at;
};

struct xv_ent
{
	char *role, *tool, *text;
	int error;
};

struct xv_rl		/* a reader line: bytes [s, s + n) of its text */
{
	const char *s;
	size_t n;
	int kind;		/* 0 an entry's head, 1 text, 2 output */
};

struct xv_hint
{
	int row;
	size_t s, e;		/* the span's bytes on its row */
	char *key, *text;
	char label[3];
};

struct xv
{
	int fd, color, mode, back, tab, win, filtering;
	int hl;			/* a remote one's "host:" length, else 0 */
	char n[700];		/* the session as given */
	char fail[400];		/* the host's refusal, in place of the tabs */
	char status[400], filt[256];
	/* the index: --follow's snapshot and its deltas */
	int have, ended, live, mentions_only;
	long long lines;
	char *session, *sid, *corpus, *reason, *why, *label;
	struct xv_ch *ch;
	struct xv_ref *rf;
	int nch, ach, nrf, arf;
	struct xv_kid kid[XV_NKIDS];
	int sel[3], top[3], *vis, nvis, avis;
	/* the card */
	char *ckey, *ctext;
	int full, scope, cdone, cpartial, csel, ctop;
	struct xv_def *d;
	struct xv_men *m;
	int nd, ad, nm, am;
	long long cnt[5];	/* session, project, project_sessions, all,
				** all_sessions; -1 null */
	/* the reader */
	char *rtitle, *rwant;
	long long rat;
	int rtrunc, rtop, rw, rloaded;
	struct xv_ent *e;
	struct xv_rl *rl;
	int ne, ae, nrl, arl;
	/* On screen */
	char *scr;
	int scr_rc, nh, ah, nlab;
	long long scr_at;
	struct xv_hint *h;
	char typed[3];
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

/* Does s[0..n) hold w? */
static int
xv_in(const char *s, size_t n, const char *w)
{
	size_t l = strlen(w), k;

	for (k = 0; l && k + l <= n; k++)
		if (!memcmp(s + k, w, l))
			return 1;
	return 0;
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
	int l, cw = 0;

	while (k < n && s[k])
	{
		char c[5] = "";

		/* pk_cps: as wide as pk_txt draws it */
		l = 1;
		(void)ap_cp(s + k, &l);
		if (l < 1 || k + (size_t)l > n)
			l = 1;
		memcpy(c, s + k, (size_t)l);
		cw += pk_cps(c);
		if (cw > w)
			break;
		k += (size_t)l;
	}
	return k;
}

/* ---- the children ---------------------------------------------------- */

static void
xv_stop(struct xv_kid *k, int slot)
{
	if (k->pid > 0)
	{
		kill(k->pid, SIGTERM);
		xv_pids[slot] = 0;
		while (waitpid(k->pid, NULL, 0) < 0 && errno == EINTR)
			;
	}
	if (k->fd >= 0)
		close(k->fd);
	if (k->efd >= 0)
		close(k->efd);
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
		if (!k->skip)
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
	if (k->len >= XV_LINE_MAX)
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
	}
	if (k->pid <= 0 || k->fd >= 0 || k->efd >= 0)
		return 0;
	xv_pids[slot] = 0;
	while (waitpid(k->pid, &st, 0) < 0 && errno == EINTR)
		;
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
}

static void
xv_ref_free(struct xv_ref *r)
{
	xv_sfree(r->key);
	xv_sfree(r->shape);
	xv_sfree(r->text);
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
	int k;

	c.anchor = xv_str(p, o, "anchor");
	c.title = xv_str(p, o, "title");
	c.kind = xv_str(p, o, "kind");
	c.at = xv_num(p, o, "at", -1);
	c.offset = xv_num(p, o, "offset", 0);
	c.error = xv_bool(p, o, "error");
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
	r.title = xv_str(p, o, "title");
	r.count = xv_num(p, o, "count", 0);
	r.defined = xv_bool(p, o, "defined");
	r.underline = xv_bool(p, o, "underline");
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
	if ((a = xv_obj(p, o, "chapters", JSON_ARRAY)) >= 0)
		for (t = a + 1; t < p->tok[a].next; t = p->tok[t].next)
			if (p->tok[t].kind == JSON_OBJECT)
				xv_chapter(v, p, t);
	if ((a = xv_obj(p, o, "refs", JSON_ARRAY)) >= 0)
		for (t = a + 1; t < p->tok[a].next; t = p->tok[t].next)
			if (p->tok[t].kind == JSON_OBJECT)
				xv_ref(v, p, t);
	if (!v->have)
		v->sel[0] = v->nch - 1;	/* the newest chapter */
	v->have = 1;
}

static void xv_back(struct xv *v);

/* One --follow line (3.3). */
static void
xv_fline(struct xv *v, const struct json_parser *p)
{
	int o, k, j;

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
		** included; full and evicted: kept until the snapshot */
		if (strcmp(why, "full") && strcmp(why, "evicted"))
		{
			xv_drop(v);
			while (v->mode != XV_LIST)
				xv_back(v);
			snprintf(v->status, sizeof v->status, "X-ray reset (%s)", why);
		}
		xv_sfree(why);
	}
}

/* ---- the card (--ref, spec 3.4) ------------------------------------- */

static void
xv_card_free(struct xv *v)
{
	int k;

	for (k = 0; k < v->nd; k++)
	{
		xv_sfree(v->d[k].session);
		xv_sfree(v->d[k].title);
		xv_sfree(v->d[k].passage);
		xv_sfree(v->d[k].rule);
	}
	for (k = 0; k < v->nm; k++)
	{
		xv_sfree(v->m[k].session);
		xv_sfree(v->m[k].line);
		xv_sfree(v->m[k].chapter);
	}
	v->nd = v->nm = 0;
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
	int o;

	if ((o = xv_obj(p, 0, "definition", JSON_OBJECT)) >= 0)
	{
		struct xv_def d;

		if (xv_grow(&v->d, &v->ad, v->nd, sizeof d) < 0)
			return;
		d.session = xv_str(p, o, "session");
		d.title = xv_str(p, o, "title");
		d.passage = xv_str(p, o, "passage");
		d.rule = xv_str(p, o, "rule");
		d.at = xv_num(p, o, "at", -1);
		if (!xv_lines_ok(v, d.session))
		{
			xv_sfree(d.passage);
			d.passage = xv_none;
		}
		v->d[v->nd++] = d;
	}
	else if ((o = xv_obj(p, 0, "mention", JSON_OBJECT)) >= 0)
	{
		struct xv_men m;

		if (v->nm >= XV_MENTIONS ||
		    xv_grow(&v->m, &v->am, v->nm, sizeof m) < 0)
			return;
		m.session = xv_str(p, o, "session");
		if (!xv_lines_ok(v, m.session))
		{
			xv_sfree(m.session);
			return;
		}
		m.line = xv_str(p, o, "line");
		m.chapter = xv_str(p, o, "chapter");
		m.at = xv_num(p, o, "at", -1);
		v->m[v->nm++] = m;
	}
	else if ((o = xv_obj(p, 0, "progress", JSON_OBJECT)) >= 0)
		xv_counts(v, p, o);
	else if (xv_bool(p, 0, "done"))
	{
		xv_counts(v, p, 0);
		v->cdone = 1;
		v->cpartial = xv_bool(p, 0, "partial");
	}
}

/* The card of ref text (key: its index key), scanned light for the
** project, or full (scope 1 project, 2 all). */
static void
xv_card(struct xv *v, const char *key, const char *text, int full,
        int scope)
{
	char *av[8];
	int ac = 0, k;

	if (key != v->ckey)
	{
		xv_sfree(v->ckey);
		xv_sfree(v->ctext);
		v->ckey = strdup(key);
		v->ctext = strdup(text);
		if (!v->ckey || !v->ctext)
		{
			free(v->ckey);
			free(v->ctext);
			v->ckey = v->ctext = NULL;
			snprintf(v->status, sizeof v->status, "X-ray: out of memory");
			return;
		}
		v->csel = v->ctop = 0;
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
	av[ac++] = v->ctext;
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
	xv_sfree(v->rtitle);
	v->rtitle = NULL;
	v->rloaded = v->rtop = 0;
}

/* The entries as lines of w cells: a head per entry, its text wrapped. */
static void
xv_wrap(struct xv *v, int w)
{
	struct xv_rl l;
	int k;

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

			do
			{
				f = xv_fitlen(s, ll, w);
				if (!f && ll)
					f = 1;
				if (xv_grow(&v->rl, &v->arl, v->nrl, sizeof l) < 0)
					return;
				l.s = s;
				l.n = f;
				l.kind = out ? 2 : 1;
				v->rl[v->nrl++] = l;
				s += f;
				ll -= f;
			} while (ll);
			if (!*s)
				break;
			s++;
		}
	}
	/* opened from a mention: at the ref's first line */
	if (v->rwant && !v->rloaded)
		for (k = 0; k < v->nrl; k++)
			if (v->rl[k].kind && xv_in(v->rl[k].s, v->rl[k].n, v->rwant))
			{
				v->rtop = k > 2 ? k - 2 : 0;
				break;
			}
	v->rloaded = 1;
}

static void
xv_rline(struct xv *v, const struct json_parser *p)
{
	int a, t;
	char *c;

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
		/* a tab or CR as a blank: the reader lays text out itself */
		for (c = e.text; *c; c++)
			if (*c == '\t' || *c == '\r')
				*c = ' ';
		v->e[v->ne++] = e;
	}
	xv_wrap(v, pk_w - 2);
}

/* The reader on chapter anchor; want: the text to open at, or NULL. */
static void
xv_read(struct xv *v, const char *anchor, const char *want)
{
	char *av[5], a[200];

	snprintf(a, sizeof a, "%s", anchor);
	xv_read_free(v);
	xv_sfree(v->rwant);
	v->rwant = want ? strdup(want) : NULL;
	v->back = v->mode;
	v->mode = XV_READ;
	av[0] = v->n;
	av[1] = "--chapter";
	av[2] = a;
	av[3] = "--json";
	av[4] = NULL;
	(void)xv_start(v, XV_RK, av, 4, xv_rline);
}

/* Esc: the reader to where it came from, the card to the list. */
static void
xv_back(struct xv *v)
{
	if (v->mode == XV_READ)
	{
		xv_stop(&v->kid[XV_RK], XV_RK);
		xv_read_free(v);
		v->mode = v->back == XV_CARD ? XV_CARD : XV_LIST;
	}
	else if (v->mode == XV_CARD)
	{
		xv_stop(&v->kid[XV_CK], XV_CK);
		xv_card_free(v);
		v->mode = XV_LIST;
	}
}

/* ---- On screen (K6) -------------------------------------------------- */

static void
xv_hints_free(struct xv *v)
{
	int k;

	for (k = 0; k < v->nh; k++)
	{
		free(v->h[k].key);
		free(v->h[k].text);
	}
	v->nh = v->nlab = 0;
}

struct xv_scan
{
	struct xv *v;
	const char *line;
	int row;
};

/* A span on the screen: a hint when the index underlines its key (K6). */
static int
xv_span(void *ctx, const struct xr_span *sp)
{
	struct xv_scan *c = ctx;
	struct xv *v = c->v;
	struct xv_hint h;
	int k;

	for (k = 0; k < v->nrf && strcmp(v->rf[k].key, sp->key); k++)
		;
	if (k == v->nrf || !v->rf[k].underline ||
	    xv_grow(&v->h, &v->ah, v->nh, sizeof h) < 0)
		return 0;
	memset(&h, 0, sizeof h);
	h.row = c->row;
	h.s = sp->s;
	h.e = sp->e;
	h.key = strdup(sp->key);
	h.text = strndup(c->line + sp->s, sp->e - sp->s);
	if (!h.key || !h.text)
	{
		free(h.key);
		free(h.text);
		return 0;
	}
	v->h[v->nh++] = h;
	return 0;
}

/* v->scr's spans found by the engine's grammar, one label per key. */
static void
xv_screen_scan(struct xv *v)
{
	struct xv_scan c;
	char *s, *nl;
	int k, j, row = 0;
	int code = v->corpus && !strcmp(v->corpus, "shell");
	const char *cwd = v->label && v->label[0] == '/' ? v->label : NULL;

	c.v = v;
	for (s = v->scr; *s; s = nl + 1, row++)
	{
		if (!(nl = strchr(s, '\n')))
			nl = s + strlen(s);
		c.line = s;
		c.row = row;
		(void)xr_grammar(s, (size_t)(nl - s), code ? XR_CODE : XR_PROSE,
		                 cwd, xv_span, &c);
		if (!*nl)
			break;
	}
	/* the labels, one per key, in screen order; two letters past 25 */
	for (k = 0; k < v->nh; k++)
	{
		for (j = 0; j < k && strcmp(v->h[j].key, v->h[k].key); j++)
			;
		if (j < k)
			memcpy(v->h[k].label, v->h[j].label, 3);
		else
			v->nlab++;
	}
	for (k = 0, j = 0; k < v->nh; k++)
	{
		int i;

		if (v->h[k].label[0])
			continue;
		i = j++;
		if (v->nlab <= 25)
			v->h[k].label[0] = XV_ALPHA[i];
		else if (i < 25 * 25)
		{
			v->h[k].label[0] = XV_ALPHA[i / 25];
			v->h[k].label[1] = XV_ALPHA[i % 25];
		}
		for (i = k + 1; i < v->nh; i++)
			if (!strcmp(v->h[i].key, v->h[k].key))
				memcpy(v->h[i].label, v->h[k].label, 3);
	}
}

/* The session's screen from its mirror, then its hints. A remote one's
** (#188) is ctl's `--read host:name --ansi` in a child, on its own
** connection; xv_screen_done takes its answer. No --cursor: the view draws
** none, and it fails a screen over 2 MB or an older master. */
static void
xv_screen_load(struct xv *v)
{
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
	xv_hints_free(v);
	free(v->scr);
	v->scr = NULL;
	if (!v->have || !v->live)
	{
		v->scr_rc = -1;
		return;
	}
	if (!(v->scr = malloc(128 * 1024 + 1)))
	{
		v->scr_rc = -1;
		return;
	}
	v->scr_rc = xv_screen(v->session, v->scr, 128 * 1024 + 1);
	if (v->scr_rc < 0)
	{
		free(v->scr);
		v->scr = NULL;
		return;
	}
	xv_screen_scan(v);
}

/* A remote screen's child has ended: its bytes as plain text, or why not
** (a dch-lite host's "no terminal mirror", else its line on the status). */
static void
xv_screen_done(struct xv *v)
{
	static const char lite[] = "dch: this session has no terminal mirror";
	struct xv_kid *k = &v->kid[XV_SK];
	char *s = k->rc ? NULL : xv_plain(k->buf ? k->buf : "", k->len);

	xv_hints_free(v);
	free(v->scr);
	v->scr = s;
	if (s)
	{
		v->scr_rc = (int)strlen(s);
		xv_screen_scan(v);
		return;
	}
	v->scr_rc = k->rc == 3 && !strncmp(k->err, lite, sizeof lite - 1) ? -2
	            : -1;
	if (v->scr_rc == -1 && (!k->rc || k->err[0]))
		snprintf(v->status, sizeof v->status, "%s", k->rc ? k->err
		         : "X-ray: the screen is too big");
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
	if (!v->have || !strcmp(c, "claude"))
		return;
	if (!strcmp(c, "codex"))
		snprintf(b, n, "Codex%smentions only", d);
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
	else if (!strcmp(c, "none"))
	{
		if (!strcmp(r, "ended"))
			snprintf(b, n, "Shell%sended%snot kept", d, d);
		else
			snprintf(b, n, "%s", !strcmp(r, "lite")
			         ? "X-ray needs the full dch build" : !strcmp(r, "off")
			         ? "X-ray is off for shells (xray_ring = 0)"
			         : "No X-ray corpus for this session");
	}
	else
		snprintf(b, n, "X-ray corpus not supported by this dch");
}

/* The rows a list tab shows (Chapters, Refs): their indexes, filtered. */
static void
xv_vis(struct xv *v)
{
	int k, n = v->tab == 0 ? v->nch : v->nrf;

	v->nvis = 0;
	for (k = 0; k < n; k++)
	{
		if (v->filt[0] &&
		    !(v->tab == 0 ? xv_has(v->ch[k].title, v->filt)
		      : xv_has(v->rf[k].text, v->filt) ||
		      xv_has(v->rf[k].title, v->filt)))
			continue;
		if (xv_grow(&v->vis, &v->avis, v->nvis, sizeof *v->vis) < 0)
			return;
		v->vis[v->nvis++] = k;
	}
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
	pk_set(!strcmp(c->kind, "compact") ? "2" : "");
	pk_txt(c->title);
	if (on)
		pk_pad(pk_w);
}

static void
xv_row_ref(const struct xv_ref *r, int on)
{
	char b[40];
	int tw = pk_w / 3 > 20 ? pk_w / 3 : 20;

	xv_lead(on);
	snprintf(b, sizeof b, "%5lld ", r->count);
	pk_set("2");
	pk_txt(b);
	pk_set(pk_p(PK_ACC));
	pk_txt(r->defined ? (pk_utf ? "\xe2\x97\x8f " : "* ") : "  ");
	pk_set("2");
	pk_fit(r->shape, 7);
	pk_txt(" ");
	pk_set(r->underline ? "1" : "");
	pk_fit(r->text, tw);
	pk_txt("  ");
	pk_set("2");
	pk_txt(r->title);
	if (on)
		pk_pad(pk_w);
}

static void
xv_row_screen(const struct xv *v, const char *s, size_t n, int row)
{
	size_t at = 0;
	int k;

	pk_txt(" ");
	for (k = 0; k < v->nh; k++)
	{
		const struct xv_hint *h = &v->h[k];

		if (h->row != row || h->s < at || h->e > n)
			continue;
		pk_set("");
		xv_txtn(s + at, h->s - at);
		if (h->label[0])
		{
			int typed = v->typed[0] && v->typed[0] == h->label[0];

			pk_bg = pk_p(PK_CHIP);
			pk_set(pk_p(typed ? PK_WARN : PK_ACCB));
			pk_txt(pk_p(PK_CHIP)[0] ? "" : "[");
			pk_txt(h->label);
			pk_txt(pk_p(PK_CHIP)[0] ? "" : "]");
			pk_bg = "";
		}
		pk_set("1;4");
		xv_txtn(s + h->s, h->e - h->s);
		at = h->e;
	}
	pk_set("");
	xv_txtn(s + at, n - at);
}

/* The card: what the ref is, its definitions, counts and mentions. */
static void
xv_draw_card(struct xv *v, int win)
{
	char b[600], w[40];
	const char *ge = v->cdone && !v->cpartial ? "" : "\xe2\x89\xa5";
	int k, rows = 0, first = -1, last = -1, mw;
	int running = v->kid[XV_CK].pid > 0;

	if (!pk_utf)
		ge = v->cdone && !v->cpartial ? "" : ">=";
	/* the definitions: two or more are counted, never one picked */
	pk_line();
	pk_set("1");
	if (!v->nd)
	{
		pk_set("2");
		pk_txt(running ? "Definitions: searching" : "No definition found");
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
		if (d->passage[0] && rows < win / 2)
		{
			const char *s = d->passage;
			int l;

			for (l = 0; l < 3 && *s && rows < win / 2; l++, rows++)
			{
				size_t f = xv_fitlen(s, strlen(s), pk_w - 4);

				if (!f)
					f = 1;
				pk_line();
				pk_set("2");
				pk_txt("    ");
				xv_txtn(s, f);
				pk_eol();
				s += f;
			}
		}
	}
	/* the counts by scope */
	pk_line();
	snprintf(b, sizeof b, "Mentions: this session %s%lld", ge,
	         v->cnt[0] > 0 ? v->cnt[0] : 0);
	if (v->cnt[1] >= 0)
		snprintf(b + strlen(b), sizeof b - strlen(b), "%sthis project "
		         "%s%lld in %lld sessions", xv_dot(), ge, v->cnt[1],
		         v->cnt[2] > 0 ? v->cnt[2] : 0);
	if (v->cnt[3] >= 0)
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
	for (k = 0; k < v->nm; k++)
		if (v->m[k].at >= 0)
		{
			if (first < 0 || v->m[k].at < v->m[first].at)
				first = k;
			if (last < 0 || v->m[k].at > v->m[last].at)
				last = k;
		}
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
	/* the mentions, a list */
	mw = win - rows;
	if (mw < 1)
		mw = 1;
	xv_clamp(&v->csel, &v->ctop, v->nm, mw);
	for (k = v->ctop; rows < win; k++, rows++)
	{
		pk_line();
		if (k < v->nm)
		{
			const struct xv_men *m = &v->m[k];

			xv_lead(k == v->csel);
			xv_when(m->at, w, sizeof w);
			pk_set("2");
			pk_fit(w, 13);
			if (strcmp(m->session, v->session))
			{
				pk_fit(m->session, 16);
				pk_txt(" ");
			}
			pk_set("");
			pk_txt(m->line);
			if (k == v->csel)
				pk_pad(pk_w);
		}
		else if (k == 0)
		{
			pk_set("2");
			pk_txt(running ? "  " : !xv_lines_ok(v, v->session)
			       ? "  Lines need the full scan: f" : "  No mention lines");
		}
		pk_eol();
	}
}

static void
xv_draw_read(struct xv *v, int win)
{
	int k;

	if (v->rloaded && v->rw != pk_w - 2)
		xv_wrap(v, pk_w - 2);
	if (v->rtop > v->nrl - win)
		v->rtop = v->nrl - win;
	if (v->rtop < 0)
		v->rtop = 0;
	for (k = v->rtop; k < v->rtop + win; k++)
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
			xv_txtn(v->rl[k].s, v->rl[k].n);
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
}

static void
xv_draw(struct xv *v, int force)
{
	static const char *const tabn[3] = { "Chapters", "Refs", "On screen" };
	static const char spin[] = "\xe2\xa0\x8b\xe2\xa0\x99\xe2\xa0\xb9"
		"\xe2\xa0\xb8\xe2\xa0\xbc\xe2\xa0\xb4\xe2\xa0\xa6\xe2\xa0\xa7"
		"\xe2\xa0\x87\xe2\xa0\x8f", ascii[] = "|/-\\";
	struct pkkey keys[12];
	const char *ret = pk_utf ? "\xe2\x86\xb5" : "enter";
	char b[1200];
	int nk = 0, kl, cap, k, n, busy;
	unsigned drop;

	pk_begin(v->fd, v->color);
	if (pk_w < 24 || pk_h < 8)
	{
		pk_line();
		pk_txt("X-ray: the window is too small");
		pk_eol();
		pk_fend(v->fd, force);
		return;
	}
#define XV_K(g, k, l) (keys[nk++] = (struct pkkey){ (k), (l), NULL, (g), 0 })
	if (v->filtering)
	{
		XV_K(0, ret, "done");
		XV_K(3, "esc", "clear");
	}
	else if (v->mode == XV_CARD)
	{
		XV_K(0, ret, "read");
		XV_K(0, "f", "full scan");
		XV_K(0, "o", "other projects");
		XV_K(0, "y", "copy");
		XV_K(3, "esc", "back");
	}
	else if (v->mode == XV_READ)
	{
		XV_K(3, pk_utf ? "\xe2\x86\x91\xe2\x86\x93" : "up/down", "scroll");
		XV_K(3, "esc", "back");
	}
	else if (v->fail[0])
		XV_K(3, "q", "quit");
	else
	{
		if (v->tab < 2)
		{
			XV_K(0, ret, v->tab == 0 ? "read" : "card");
			XV_K(3, "/", "filter");
		}
		else
			XV_K(0, "a-z", "label: card");
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
	else if (v->mode == XV_READ)
	{
		pk_set(pk_p(PK_ACCB));
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
	else
		for (k = 0; k < 3; k++)
			pk_tab(tabn[k], k == 0 ? v->nch : k == 1 ? v->nrf : v->nlab,
			       k == v->tab);
	pk_eol();

	pk_line();
	xv_label(v, b, sizeof b);
	pk_set("2");
	pk_txt(b);
	pk_eol();

	if (v->mode == XV_CARD)
		xv_draw_card(v, v->win);
	else if (v->mode == XV_READ)
		xv_draw_read(v, v->win);
	else if (v->fail[0])
		for (k = 0; k < v->win; k++)
		{
			pk_line();
			pk_eol();
		}
	else if (v->tab < 2)
	{
		int *sel = &v->sel[v->tab], *top = &v->top[v->tab];

		xv_vis(v);
		n = v->nvis;
		xv_clamp(sel, top, n, v->win);
		for (k = *top; k < *top + v->win; k++)
		{
			pk_line();
			if (k < n && v->tab == 0)
				xv_row_ch(&v->ch[v->vis[k]], v->vis[k], k == *sel);
			else if (k < n)
				xv_row_ref(&v->rf[v->vis[k]], k == *sel);
			else if (k == 0)
			{
				pk_set("2");
				pk_txt(!v->have ? "    Reading" : v->filt[0]
				       ? "    No match" : v->tab == 0 ? "    No chapters"
				       : "    No refs");
				if (!v->have)
					pk_txt(xv_ell());
			}
			pk_eol();
		}
	}
	else
	{
		const char *s = v->scr;
		int row = 0;

		for (k = 0; k < v->win; k++)
		{
			pk_line();
			if (s && *s)
			{
				size_t l = strcspn(s, "\n");

				xv_row_screen(v, s, l, row++);
				s += l + (s[l] == '\n');
			}
			else if (k == 0 && !s)
			{
				int rd = !v->have || v->kid[XV_SK].pid > 0;

				pk_set("2");
				if (rd)
					pk_txt("    Reading");
				pk_txt(rd ? xv_ell() : !v->live
				       ? "    No screen: the session has ended"
				       : v->scr_rc == -2
				       ? "    No screen: dch-lite keeps no mirror"
				       : "    No screen");
			}
			pk_eol();
		}
	}

	pk_rule();
	pk_line();
	if (v->filtering || (v->mode == XV_LIST && v->tab < 2 && v->filt[0]))
	{
		pk_set(pk_p(PK_ACC));
		pk_txt("/");
		pk_set("");
		pk_txt(v->filt);
		if (v->filtering)
			pk_txt(pk_utf ? "\xe2\x96\x88" : "_");
		pk_txt("  ");
	}
	busy = v->kid[XV_CK].pid > 0 || v->kid[XV_RK].pid > 0 ||
	       (!v->have && !v->fail[0]);
	if (busy)
	{
		int f = (int)(now_ms() / 100 % (pk_utf ? 10 : 4));

		snprintf(b, sizeof b, "%.*s ", pk_utf ? 3 : 1,
		         pk_utf ? spin + f * 3 : ascii + f);
		pk_set(pk_p(PK_ACC));
		pk_txt(b);
	}
	if (v->typed[0])
	{
		pk_set(pk_p(PK_ACC));
		pk_txt(v->typed);
		pk_txt("  ");
	}
	pk_set("");
	pk_txt(v->status);
	pk_eol();
	pk_keys(keys, nk, drop, cap, 1);
	pk_fend(v->fd, force);
}

/* ---- keys ------------------------------------------------------------ */

/* The filter's prompt, as the picker's (#096). 1: c was the filter's. */
static int
xv_filt(struct xv *v, int c)
{
	size_t l = strlen(v->filt);

	if (!v->filtering)
	{
		if (c != '/' || v->tab > 1)
			return 0;
		v->filtering = 1;
		v->status[0] = '\0';
		return 1;
	}
	switch (c)
	{
	case '\r': case '\n': case PK_UP: case PK_DOWN: case '\t': case PK_BTAB:
		v->filtering = 0;	/* and on to the list, as the picker's */
		return 0;
	case PK_ESC:
		v->filt[0] = '\0';
		v->filtering = 0;
		break;
	case 0x7f: case 8:
		if (!l)
		{
			v->filtering = 0;
			return 1;
		}
		while (l && ((unsigned char)v->filt[--l] & 0xC0) == 0x80)
			;
		v->filt[l] = '\0';
		break;
	case 0x15:	/* Ctrl-U */
		v->filt[0] = '\0';
		break;
	default:
		if (c < 0x20 || c > 0xff)
			return 0;	/* the arrows: the list's */
		if (l + 1 < sizeof v->filt)
		{
			v->filt[l] = (char)c;
			v->filt[l + 1] = '\0';
		}
	}
	v->sel[0] = v->sel[1] = 0;
	return 1;
}

/* A hint label typed (On screen): its card when it names one. */
static void
xv_hint_key(struct xv *v, int c)
{
	size_t l = strlen(v->typed);
	int k;

	if (l >= 2)
		l = 0;
	v->typed[l] = (char)c;
	v->typed[l + 1] = '\0';
	for (k = 0; k < v->nh; k++)
		if (!strcmp(v->h[k].label, v->typed))
		{
			v->typed[0] = '\0';
			xv_card(v, v->h[k].key, v->h[k].text, 0, 1);
			return;
		}
	for (k = 0; k < v->nh && strncmp(v->h[k].label, v->typed, l + 1); k++)
		;
	if (k == v->nh)
	{
		snprintf(v->status, sizeof v->status, "no label %s", v->typed);
		v->typed[0] = '\0';
	}
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

/* One key. 1: leave the view. */
static int
xv_key(struct xv *v, int c)
{
	int *sel, n, k;

	if (c == 3 || c == 4)
		return 1;
	if (v->fail[0])
		return c == 'q' || c == PK_ESC;
	if (v->mode == XV_LIST && xv_filt(v, c))
		return 0;
	v->status[0] = '\0';
	if (v->mode == XV_READ)
	{
		switch (c)
		{
		case 'j': case PK_DOWN: v->rtop++; break;
		case 'k': case PK_UP: v->rtop--; break;
		case ' ': case PK_PGDN: v->rtop += v->win; break;
		case 'b': case PK_PGUP: v->rtop -= v->win; break;
		case 'g': case PK_HOME: v->rtop = 0; break;
		case 'G': case PK_END: v->rtop = v->nrl; break;
		case 'q': case PK_ESC: case 'h': case PK_LEFT: case 0x7f:
			xv_back(v);
			break;
		}
		return 0;
	}
	if (v->mode == XV_CARD)
	{
		const struct xv_men *m = v->csel < v->nm ? &v->m[v->csel] : NULL;

		switch (c)
		{
		case 'j': case PK_DOWN: v->csel++; break;
		case 'k': case PK_UP: v->csel--; break;
		case PK_PGDN: v->csel += v->win / 2; break;
		case PK_PGUP: v->csel -= v->win / 2; break;
		case 'g': case PK_HOME: v->csel = 0; break;
		case 'G': case PK_END: v->csel = v->nm - 1; break;
		case 'f':
			xv_card(v, v->ckey, v->ctext, 1, v->scope);
			break;
		case 'o':
			xv_card(v, v->ckey, v->ctext, 1, 2);
			break;
		case 'y':
			xv_copy(v);
			break;
		case '\r': case '\n':
			if (m && m->chapter[0] && !strcmp(m->session, v->session))
				xv_read(v, m->chapter, v->ctext);
			else if (m)
				snprintf(v->status, sizeof v->status, "%s",
				         m->chapter[0] ? "another session's chapter: "
				         "open its X-ray" : "no chapter for that line");
			break;
		case 'q': case PK_ESC: case 'h': case PK_LEFT: case 0x7f:
			xv_back(v);
			break;
		}
		return 0;
	}
	/* the list */
	if (c == '\t' || c == PK_RIGHT || c == PK_BTAB || c == PK_LEFT ||
	    (v->tab < 2 && (c == 'l' || c == 'h')))
	{
		v->tab = (v->tab + (c == '\t' || c == PK_RIGHT || c == 'l' ? 1 : 2))
		         % 3;
		v->typed[0] = '\0';
		if (v->tab == 2)
			xv_screen_load(v);
		return 0;
	}
	if (c == 'q')
		return 1;
	if (c == PK_ESC)
	{
		if (v->typed[0])
			v->typed[0] = '\0';
		else if (v->filt[0] && v->tab < 2)
			v->filt[0] = '\0';
		else
			return 1;
		return 0;
	}
	if (v->tab == 2)
	{
		if (c > 0 && c < 128 && strchr(XV_ALPHA, c))
			xv_hint_key(v, c);
		else if (c == PK_DOWN || c == PK_UP)
			v->status[0] = '\0';
		return 0;
	}
	sel = &v->sel[v->tab];
	xv_vis(v);
	n = v->nvis;
	switch (c)
	{
	case 'j': case PK_DOWN: (*sel)++; break;
	case 'k': case PK_UP: (*sel)--; break;
	case PK_PGDN: *sel += v->win; break;
	case PK_PGUP: *sel -= v->win; break;
	case 'g': case PK_HOME: *sel = 0; break;
	case 'G': case PK_END: *sel = n - 1; break;
	case '\r': case '\n':
		if (*sel < 0 || *sel >= n)
			break;
		k = v->vis[*sel];
		if (v->tab == 0)
			xv_read(v, v->ch[k].anchor, NULL);
		else
			xv_card(v, v->rf[k].key, v->rf[k].text, 0, 1);
		break;
	}
	return 0;
}

/* ---- the loop -------------------------------------------------------- */

static void
xv_free(struct xv *v)
{
	int k;

	for (k = 0; k < XV_NKIDS; k++)
		xv_stop(&v->kid[k], k);
	xv_drop(v);
	xv_card_free(v);
	xv_read_free(v);
	xv_hints_free(v);
	xv_sfree(v->session);
	xv_sfree(v->sid);
	xv_sfree(v->corpus);
	xv_sfree(v->reason);
	xv_sfree(v->why);
	xv_sfree(v->label);
	xv_sfree(v->ckey);
	xv_sfree(v->ctext);
	xv_sfree(v->rwant);
	free(v->ch);
	free(v->rf);
	free(v->vis);
	free(v->d);
	free(v->m);
	free(v->e);
	free(v->rl);
	free(v->h);
	free(v->scr);
	free(v);
}

int
xv_run(int fd, const char *n, char *err, size_t errsz)
{
	struct xv *v = calloc(1, sizeof *v);
	char *av[4];
	int k, rc = 0;

	err[0] = '\0';
	if (!v)
	{
		snprintf(err, errsz, "dch: X-ray failed on this session");
		return 3;
	}
	v->fd = fd;
	v->tab = xv_tab;
	v->color = pk_colors(0);
	snprintf(v->n, sizeof v->n, "%s", n);
	v->hl = xv_remote(n) ? (int)strcspn(n, ":") + 1 : 0;
	for (k = 0; k < XV_NKIDS; k++)
	{
		v->kid[k].fd = v->kid[k].efd = -1;
		v->kid[k].rc = -1;
	}
	av[0] = v->n;
	av[1] = "--follow";
	av[2] = "--json";
	av[3] = NULL;
	if (xv_start(v, XV_FK, av, 3, xv_fline) < 0)
	{
		snprintf(err, errsz, "dch: %s", v->status);
		xv_free(v);
		return 3;
	}
	pk_pn = 0;
	for (;;)
	{
		struct timeval tv;
		fd_set rs;
		int mx = fd, force = pk_winch, c, busy;

		pk_winch = 0;
		if (v->mode == XV_LIST && v->tab == 2 && now_ms() - v->scr_at >= 1000)
			xv_screen_load(v);
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
					snprintf(v->status, sizeof v->status, "live "
					         "updates stopped: %s", f->err);
				else if (v->live)
					snprintf(v->status, sizeof v->status, "the "
					         "session ended; this is its last state");
			}
		}
		if (xv_pump(v, XV_CK, &rs) && v->kid[XV_CK].rc)
			snprintf(v->status, sizeof v->status, "%s",
			         v->kid[XV_CK].err[0] ? v->kid[XV_CK].err
			         : "X-ray: the scan failed");
		if (xv_pump(v, XV_SK, &rs))
			xv_screen_done(v);
		if (xv_pump(v, XV_RK, &rs) && !v->rloaded && v->mode == XV_READ)
		{
			snprintf(v->status, sizeof v->status, "%s",
			         v->kid[XV_RK].err[0] ? v->kid[XV_RK].err
			         : "X-ray: no such chapter");
			xv_back(v);
		}
		if (!FD_ISSET(fd, &rs))
			continue;
		if ((c = pk_key(fd)) < 0 || xv_key(v, c))
			break;
	}
	xv_free(v);
	pk_pn = 0;
	return rc;
}
