/* libFuzzer target: X-ray refs (#135, XR2a): the grammar and its normalizer,
** --ref's value rule and parse, and the Claude and shell decoders with the
** refs sinks installed (D1-D4, the index, a full --ref scan). Every byte is
** hostile: the corpus is whatever the session wrote.
**
** Input: byte 0's low 2 bits pick the path, the rest is its input.
**   0: the grammar over the bytes, in the context byte 0's bits 2-3 pick;
**      every span inside the text, in order, not overlapping, its key
**      "<shape>:<text>" of valid UTF-8 within XR_KEY_MAX;
**   1: a Claude JSONL transcript (cut at '\n') into an index and a --ref
**      scan for the first key the index met, then xr_print with the refs;
**   2: ring records ({u16 len, u8 kind, u8 flags, u32 time, text}) through
**      xr_shell_feed with xr_shell_refs into an index, then xr_print; a
**      record of kind 0xFE draws the ring/screen seam (#171), and every
**      record after it goes in as a screen row (output, no flags): none of
**      their hits may carry a chapter;
**   3: xr_ref_ok and xr_ref_parse over the bytes as a C string; with byte
**      0's bit 2 set, the resolvers' parsers instead (#159): git show's
**      output, a file's 14-line window around the bytes' own :line, the
**      secret-name list, a .git file's "gitdir: " test (#205), the remote
**      URL matcher against its rule read from the other end (#206); every
**      block line valid UTF-8 within 200 scalars.
** Path 1's scan: bit 3 reads the transcript twice, the second time as
** another session's (#155), bit 4 makes it light.
** Path 1 with byte 0's bit 2 set is --follow's instead (#136): the transcript
** read whole, and read again in pieces at cut points the input seeds, a delta
** batch after each; the pieces end where the whole read does, with the same
** object printed, every delta line one schema-1 chapter or ref object, and a
** second diff of the last moment empty.
** Paths 1 and 5 with byte 0's bit 5 set read the bytes as a Codex rollout
** instead (#160, 7.4): mentions only, so no hit may carry a definition.
** Path 1 also reads the transcript as a file through light --ref's windows,
** newest first, with its prefilter on and off (#293): the same lines, first
** and progress aside, or abort. Windows of 16 bytes (bit 6) or 509; bit 7 a
** line cap of 64 and a byte cap of half the file; bit 3 --scope project, so
** no first result ends the read.
** Every hit handed to a sink lies inside its line; a definition's title is
** valid UTF-8, its passage within XR_PASSAGE scalars' bytes; the printed object is one line within the cap. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include "../../ring.h"
#include "../../xray.h"
#include "../../bridge.h"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static int cx;			/* this input is a Codex rollout */

static struct xr_claude *
decoder(void)
{
	struct xr_claude *x = xr_claude_new();

	if (x && cx)
		xr_claude_codex(x);
	return x;
}

static void
check_utf8(const char *t, size_t n)
{
	const unsigned char *u = (const unsigned char *)t;
	size_t i = 0;

	while (i < n)
	{
		size_t a = u[i] < 0x80 ? 1 : (u[i] & 0xe0) == 0xc0 ? 2
		           : (u[i] & 0xf0) == 0xe0 ? 3 : (u[i] & 0xf8) == 0xf0
		           ? 4 : 0;
		size_t k;

		if (!a || i + a > n)
			abort();
		for (k = 1; k < a; k++)
			if ((u[i + k] & 0xc0) != 0x80)
				abort();
		i += a;
	}
}

static void
check_key(const struct xr_span *sp)
{
	const char *c = strchr(sp->key, ':');
	size_t kl = strlen(sp->key);

	if (sp->shape < XR_TICKET || sp->shape > XR_URL || !c ||
	    strncmp(sp->key, xr_shape_name[sp->shape], (size_t)(c - sp->key)) ||
	    strlen(xr_shape_name[sp->shape]) != (size_t)(c - sp->key) ||
	    xr_span_text(sp) != c + 1 || kl - (size_t)(c + 1 - sp->key) == 0 ||
	    kl - (size_t)(c + 1 - sp->key) > XR_KEY_MAX)
		abort();
	check_utf8(sp->key, kl);
}

struct gram
{
	size_t n, last;
};

static int
gram_span(void *ctx, const struct xr_span *sp)
{
	struct gram *g = ctx;

	if (sp->s >= sp->e || sp->e > g->n || sp->s < g->last)
		abort();
	g->last = sp->e;
	check_key(sp);
	return 0;
}

/* the sink between a decoder and its consumers: checks, then forwards */
struct tee
{
	struct xr_sink ix, sc;
	char key[XR_KEY_MAX + 16];
	int screen;		/* past the seam: no chapter (#171) */
};

static void
tee_hit(void *ctx, const struct xr_hit *h)
{
	struct tee *t = ctx;

	if (!h->sp || h->sp->s >= h->sp->e || h->sp->e > h->ll || !h->line)
		abort();
	check_key(h->sp);
	if (cx && (h->title || h->passage || h->rule))
		abort();	/* a Codex corpus defines nothing (7.4) */
	if (t->screen && h->chapter)
		abort();	/* a screen row has no marks, so no block */
	if (h->title)
		check_utf8(h->title, strlen(h->title));
	/* a passage is the corpus's own bytes: xr_put_str writes U+FFFD */
	if (h->passage && strlen(h->passage) > XR_PASSAGE * 4)
		abort();
	if (!t->key[0])
		snprintf(t->key, sizeof t->key, "%s", h->sp->key);
	t->ix.hit(t->ix.ctx, h);
	if (t->sc.hit)
		t->sc.hit(t->sc.ctx, h);
}

static void
emit(struct xr_index *ix, int shell)
{
	struct xr_chapters c = {0};
	struct xr_answer a;
	char *buf = NULL;
	size_t bl = 0;
	FILE *f;

	memset(&a, 0, sizeof a);
	a.session = "fuzz";
	a.session_id = "m0123456789abcdef";
	a.corpus = shell ? "shell" : "claude";
	a.project = a.project_label = "";
	a.available = a.complete = 1;
	a.lines = shell ? 1 : -1;
	a.ch = &c;
	a.refs = ix;
	if (!(f = open_memstream(&buf, &bl)))
		return;
	if (xr_print(f, &a) != 0)
		abort();
	fclose(f);
	if (!bl || buf[bl - 1] != '\n' || memchr(buf, '\n', bl - 1) ||
	    bl > xr_lim.out)
		abort();
	free(buf);
}

/* A --ref over the same transcript for key, full, or light with byte 0's
** bit 4; with bit 3, read again as another session's file (#155, --scope
** all). Its stream must hold first, done last, one line each; light, the
** other session's lines carry no mention and no passage (K4.2). */
static void
scan(const uint8_t *d, size_t n, const char *key)
{
	struct xr_scan *s;
	struct xr_sink k;
	char *buf = NULL, *o;
	size_t bl = 0, i, st;
	int light = d[0] & 16, pass;
	FILE *f;

	if (!(f = open_memstream(&buf, &bl)))
		return;
	if (!(s = xr_scan_new(f, key, "fuzz", "", 0, !light, 0, 300,
	                      xr_now_ms())))
	{
		fclose(f);
		free(buf);
		return;
	}
	xr_scan_scope(s, d[0] & 8 ? 2 : 0);
	if (xr_scan_first(s, "11111111-2222-4333-8444-555555555555",
	                  XR_ISSUE, key) == 0)
	{
		for (pass = 0; pass < (d[0] & 8 ? 2 : 1); pass++)
		{
			struct xr_claude *x;
			struct xr_chapters c = {0};

			if ((pass && xr_scan_next(s, "other", "q", 0, 0)) ||
			    !(x = decoder()))
				break;
			xr_scan_sink(s, &k);
			xr_claude_refs(x, &k, 0x1234, xr_scan_tick, s);
			for (i = st = 1; i <= n; i++)
				if (i == n || d[i] == '\n')
				{
					char *l = i > st ? malloc(i - st + 1) : NULL;

					if (l)
					{
						memcpy(l, d + st, i - st);
						l[i - st] = '\0';
						xr_claude_line(x, l, i - st, st, 0);
						free(l);
					}
					st = i + 1;
				}
			xr_claude_end(x, 0x1234, &c);
			xr_chapters_free(&c);
			xr_scan_file(s, n, xr_claude_too_long(x), 0, 0);
			xr_claude_free(x);
		}
		(void)xr_scan_done(s);
	}
	xr_scan_free(s);
	fclose(f);
	if (bl && (buf[bl - 1] != '\n' || !strstr(buf, "{\"schema\":1,\"first\"")))
		abort();
	for (o = buf; light && o && (o = strstr(o, "\"session\":\"other\"")); o++)
		if (!strncmp(o - 11, "\"mention\":{", 11) ||
		    (!strncmp(o - 14, "\"definition\":{", 14) &&
		     strstr(o, "\"passage\"") &&
		     strstr(o, "\"passage\"") < strchr(o, '\n')))
			abort();
	free(buf);
}

/* light --ref over fd for key, the prefilter off (noskip) or on: its lines
** but first and progress, malloc'd; NULL when out of memory */
static char *
light(int fd, size_t n, const char *key, int scope, int noskip)
{
	struct xr_scan *s = NULL;
	struct xr_claude *x = NULL;
	struct xr_chapters c = {0};
	struct xr_sink k;
	char *buf = NULL, *p, *q, *w;
	size_t bl = 0;
	int capped = 0, ok = 0;
	FILE *f = open_memstream(&buf, &bl);

	xr_noskip = noskip;
	if (f && (s = xr_scan_new(f, key, "fuzz", "", 0, 0, 0, 300,
	                          xr_now_ms())))
		xr_scan_scope(s, scope);
	if (s && xr_scan_first(s, "11111111-2222-4333-8444-555555555555",
	                       XR_ISSUE, key) == 0 && (x = decoder()))
	{
		xr_scan_sink(s, &k);
		xr_claude_refs(x, &k, 0x1234, xr_scan_tick, s);
		(void)xr_claude_newest(x, fd, n, &capped);
		xr_claude_end(x, 0x1234, &c);
		xr_chapters_free(&c);
		xr_scan_file(s, n, xr_claude_too_long(x), capped, 0);
		ok = xr_scan_done(s) == 0;
	}
	xr_noskip = 0;
	xr_claude_free(x);
	xr_scan_free(s);
	if (f)
		fclose(f);
	if (!ok)
	{
		free(buf);
		return NULL;
	}
	for (p = w = buf; *p; p = q)
	{
		q = strchr(p, '\n');
		q = q ? q + 1 : p + strlen(p);
		if (strncmp(p, "{\"schema\":1,\"first\"", 19) &&
		    strncmp(p, "{\"schema\":1,\"progress\"", 22))
		{
			memmove(w, p, (size_t)(q - p));
			w += q - p;
		}
	}
	*w = '\0';
	return buf;
}

/* #293's differential: the transcript d[1..n) as a file, light --ref's
** windows with the prefilter on and off must print the same */
static void
differ(const uint8_t *d, size_t n, const char *key)
{
	static int fd = -1;
	char path[] = "/tmp/fuzz_xref.XXXXXX", *a, *b;
	struct xr_limits l0 = xr_lim;

	if (fd < 0 && (fd = mkstemp(path)) >= 0)
		unlink(path);
	if (fd < 0 || ftruncate(fd, 0) != 0 ||
	    pwrite(fd, d + 1, n - 1, 0) != (ssize_t)(n - 1))
		return;
	xr_lim.chunk = d[0] & 64 ? 16 : 509;
	if (d[0] & 128)
	{
		xr_lim.line = 64;
		xr_lim.bytes = n / 2 + 1;
	}
	xr_noclock = 1;	/* only the bytes decide where a read stops */
	a = light(fd, n - 1, key, d[0] & 8 ? 1 : 0, 0);
	b = light(fd, n - 1, key, d[0] & 8 ? 1 : 0, 1);
	xr_noclock = 0;
	xr_lim = l0;
	if (a && b && strcmp(a, b))
	{
		fprintf(stderr, "prefilter on:\n%s\noff:\n%s\n", a, b);
		abort();
	}
	free(a);
	free(b);
}

static void
claude_answer(struct xr_answer *a, struct xr_claude *x, struct xr_index *ix)
{
	memset(a, 0, sizeof *a);
	a->session = "fuzz";
	a->session_id = "11111111-2222-4333-8444-555555555555";
	a->corpus = "claude";
	a->project = a->project_label = "";
	a->available = a->live = a->complete = 1;
	a->lines = -1;
	a->ch = xr_claude_chapters(x);
	a->refs = ix;
}

static char *
printed(struct xr_answer *a, size_t *bl)
{
	char *buf = NULL;
	FILE *f = open_memstream(&buf, bl);

	if (!f)
		return NULL;
	if (xr_print(f, a) != 0)
		abort();
	fclose(f);
	return buf;
}

/* Each line of the deltas: {"schema":1,"chapter"|"ref":{...}}, valid JSON. */
#define CH_LINE "{\"schema\":1,\"chapter\":{"
#define REF_LINE "{\"schema\":1,\"ref\":{"
static void
check_deltas(char *b, size_t n)
{
	char *l = b, *e;

	while (l < b + n)
	{
		struct json_parser p;

		if (!(e = memchr(l, '\n', (size_t)(b + n - l))))
			abort();
		*e = '\0';
		if (strncmp(l, CH_LINE, sizeof CH_LINE - 1) &&
		    strncmp(l, REF_LINE, sizeof REF_LINE - 1))
			abort();
		memset(&p, 0, sizeof p);
		if (dch_json_parse(&p, l) < 0 || p.tok[0].kind != JSON_OBJECT ||
		    p.tok[0].end != (int)(e - l))
			abort();
		dch_json_free(&p);
		l = e + 1;
	}
}

static void
follow(const uint8_t *d, size_t n)
{
	FILE *tf = tmpfile(), *m = NULL;
	struct xr_claude *one = decoder(), *inc = decoder();
	struct xr_index *ix1 = xr_index_new(), *ix2 = xr_index_new();
	struct xr_sent *s = xr_sent_new();
	struct xr_sink k1, k2;
	struct xr_answer a;
	uint64_t to = 0, size = n - 1;
	uint32_t r = d[0] * 2654435761u + (uint32_t)n;
	char *dl = NULL, *p1 = NULL, *p2 = NULL;
	size_t dll = 0, l1 = 0, l2 = 0, nref, mark;
	int lost, fd;

	if (!tf || !one || !inc || !ix1 || !ix2 || !s ||
	    fwrite(d + 1, 1, size, tf) != size || fflush(tf) != 0 ||
	    !(m = open_memstream(&dl, &dll)))
		goto out;
	fd = fileno(tf);
	xr_index_sink(ix1, &k1);
	xr_claude_refs(one, &k1, 0x1234, NULL, NULL);
	xr_index_sink(ix2, &k2);
	xr_claude_refs(inc, &k2, 0x1234, NULL, NULL);
	if (xr_claude_tail(one, fd, 0, size) != 0)
		abort();
	claude_answer(&a, inc, ix2);	/* the empty snapshot */
	if (xr_sent_take(s, &a) != 0)
		goto out;
	while (to < size)
	{
		r = r * 1103515245u + 12345u;
		to += 1 + (r >> 16) % 97;
		if (to > size)
			to = size;
		if (xr_claude_tail(inc, fd, xr_claude_next(inc), to) != 0)
			abort();
		if (xr_claude_next(inc) > to)
			abort();
		claude_answer(&a, inc, ix2);
		if (xr_delta(s, &a, 0, &nref, &lost) != 0 ||
		    xr_delta_write(m, s, &a) != 0)
			goto out;
	}
	if (xr_claude_next(inc) != xr_claude_next(one))
		abort();
	claude_answer(&a, inc, ix2);
	if (xr_delta(s, &a, 0, &nref, &lost) != 0)
		goto out;
	fflush(m);
	mark = dll;
	if (nref || lost || xr_delta_write(m, s, &a) != 0 || fflush(m) ||
	    dll != mark)
		abort();	/* everything was written already */
	fclose(m);
	m = NULL;
	check_deltas(dl, dll);
	p2 = printed(&a, &l2);
	claude_answer(&a, one, ix1);
	p1 = printed(&a, &l1);
	if (p1 && p2 && (l1 != l2 || memcmp(p1, p2, l1)))
		abort();	/* pieces or whole: one answer */
out:
	if (m)
		fclose(m);
	free(dl);
	free(p1);
	free(p2);
	xr_sent_free(s);
	xr_claude_free(one);
	xr_claude_free(inc);
	xr_index_free(ix1);
	xr_index_free(ix2);
	if (tf)
		fclose(tf);
}

/* A resolved block (#159): at most 14 lines, each of them, the title and
** the subtitle valid UTF-8 and within their scalar caps. */
static void
check_res(const struct xr_resolved *r)
{
	const char *const t[2] = {r->title, r->subtitle};
	int k;

	if (r->nlines < 0 || r->nlines > XR_RES_LINES)
		abort();
	for (k = 0; k < r->nlines + 2; k++)
	{
		const char *l = k < 2 ? t[k] : r->line[k - 2];
		size_t ln = strlen(l), sc = 0, i;

		check_utf8(l, ln);
		for (i = 0; i < ln; i++)
			sc += ((unsigned char)l[i] & 0xc0) != 0x80;
		if (sc > (k == 0 ? XR_TITLE_MAX : XR_RES_LINE))
			abort();
	}
}

/* xr_git_remote's rule for one URL v[0..l), read from the end: it ends
** "o/r" then "", "/", ".git" or ".git/"; before it "github.com" then '/'
** (after an optional ":<digits>", digits maybe none) under a scheme
** https/http/ssh/git/git+ssh/ssh+git://, ':' with none; before that, an
** optional "user@" where user has no '@', byte <= ' ', '/', and '?', '#'
** (scheme) or ':' (none) (QA 1.57.0 NIT-D1, 1.59.0 LOW-X7, 1.61.0 NIT-X2) */
static int
url_rule(const char *v, size_t l)
{
	static const char *const tail[4] = {"", "/", ".git", ".git/"};
	static const char *const sch[6] = {"https://", "http://", "ssh://",
	                                   "git://", "git+ssh://", "ssh+git://"};
	char suf[32];
	size_t k, j, m, p, q, c;

	{
		for (k = 0; k < 8; k++)
		{
			m = (size_t)snprintf(suf, sizeof suf, "%co/r%s",
			                     k < 4 ? '/' : ':', tail[k % 4]);
			if (l < m || strncasecmp(v + l - m, suf, m))
				continue;
			q = l - m;	/* v[0..q): ...github.com[:port] */
			for (j = q; k < 4 && j > 0 && v[j - 1] >= '0' &&
			     v[j - 1] <= '9'; j--)
				;
			if (k < 4 && j > 0 && v[j - 1] == ':')
				q = j - 1;
			if (q < 10 || strncasecmp(v + q - 10, "github.com", 10))
				continue;
			q -= 10;	/* v[0..q): [scheme][user@] */
			for (p = 0, c = 0; k < 4 && c < 6; c++)
				if (!strncasecmp(v, sch[c], strlen(sch[c])))
				{
					p = strlen(sch[c]);
					break;
				}
			if ((k < 4 && !p) || p > q)
				continue;
			if (p == q)
				return 1;
			if (v[q - 1] != '@')
				continue;
			for (j = p; j < q - 1 && v[j] != '@' && v[j] != '/' &&
			     (unsigned char)v[j] > ' ' &&
			     !strchr(k < 4 ? "?#" : ":", v[j]); j++)
				;
			if (j == q - 1)
				return 1;
		}
	}
	return 0;
}

/* xr_git_remote's rule: an entry "<key>\n<value>\0" whose key is not
** url.<base>.insteadof has a value that, rewritten as git's remote.c
** alias_url does, passes url_rule: walk the bases in the order each is
** first named, each one's values in order, and keep the first value
** strictly longer than the kept one (none kept: any, "" too) that starts
** the URL; past 64 insteadof entries, none (QA 1.61.0 LOW-X15, 1.63.0
** LOW-X20) */
static int
remote_rule(const char *s, size_t n)
{
	static char u[1 << 17];
	const char *z, *v, *y, *w, *b, *y2, *w2;
	size_t i, j, j2, p, l, bl, kl, ni = 0;
	int first;

#define INSTEADOF(k, kn) ((kn) >= 14 && !strncmp((k), "url.", 4) && \
	                  !strncmp((k) + (kn) - 10, ".insteadof", 10))
#define NEXT(x, yy) ((yy) = memchr(s + (x), '\0', n - (x)))
	for (j = 0; j < n && NEXT(j, y); j = (size_t)(y - s) + 1)
		if ((w = memchr(s + j, '\n', (size_t)(y - s) - j)) &&
		    INSTEADOF(s + j, (size_t)(w - s) - j) && ++ni > 64)
			return 0;
	for (i = 0; i < n && NEXT(i, z); i = (size_t)(z - s) + 1)
	{
		if (!(v = memchr(s + i, '\n', (size_t)(z - s) - i)) ||
		    INSTEADOF(s + i, (size_t)(v - s) - i))
			continue;
		v++;
		l = (size_t)(z - v);
		b = NULL;
		bl = kl = 0;
		for (j = 0; j < n && NEXT(j, y); j = (size_t)(y - s) + 1)
		{
			if (!(w = memchr(s + j, '\n', (size_t)(y - s) - j)) ||
			    !INSTEADOF(s + j, (size_t)(w - s) - j))
				continue;
			for (first = 1, p = 0; first && p < j && NEXT(p, y2);
			     p = (size_t)(y2 - s) + 1)
				first = !((w2 = memchr(s + p, '\n',
				                       (size_t)(y2 - s) - p)) &&
				          w2 - (s + p) == w - (s + j) &&
				          !memcmp(s + p, s + j, (size_t)(w - s) - j));
			if (!first)
				continue;	/* this base was walked already */
			for (j2 = j; j2 < n && NEXT(j2, y2);
			     j2 = (size_t)(y2 - s) + 1)
				if ((w2 = memchr(s + j2, '\n', (size_t)(y2 - s) - j2)) &&
				    w2 - (s + j2) == w - (s + j) &&
				    !memcmp(s + j2, s + j, (size_t)(w - s) - j) &&
				    (!b || (size_t)(y2 - w2 - 1) > bl) &&
				    (size_t)(y2 - w2 - 1) <= l &&
				    !memcmp(w2 + 1, v, (size_t)(y2 - w2 - 1)))
				{
					bl = (size_t)(y2 - w2 - 1);
					b = s + j + 4;
					kl = (size_t)(w - b) - 10;
				}
		}
		if (!b ? url_rule(v, l) : kl + l - bl <= 4096 &&
		    (memcpy(u, b, kl), memcpy(u + kl, v + bl, l - bl),
		     url_rule(u, kl + l - bl)))
			return 1;
	}
#undef NEXT
#undef INSTEADOF
	return 0;
}

/* s[0..n) (s[n] == '\0') as git's output, a file and a path */
static void
resolvers(const char *s, size_t n)
{
	static struct xr_resolved r;

	memset(&r, 0, sizeof r);
	if (xr_git_parse(s, n, (long long)n * 977, &r))
		check_res(&r);
	if (r.first)	/* a file's field only */
		abort();
	memset(&r, 0, sizeof r);
	xr_file_lines(s, n, xr_ref_line(s), &r);
	check_res(&r);
	/* line[0]'s number: from 1, the window inside the file, the asked
	** line in it when the file has that line */
	{
		long L = xr_ref_line(s), total = 0;
		size_t i;

		for (i = 0; i < n; i++)
			total += s[i] == '\n';
		total += n && s[n - 1] != '\n';
		if (r.first < 1 || r.nlines != (total < XR_RES_LINES ? total
		    : XR_RES_LINES) || r.first + r.nlines - 1 > total ||
		    (L >= 1 && L <= total && (L < r.first ||
		     L > r.first + r.nlines - 1)))
			abort();
	}
	if (xr_ref_line(s) < 0 || xr_ref_line(s) > 9999999)
		abort();
	(void)xr_secret_name(s);
	if (xr_gitfile(s, n) != (n > 8 && !memcmp(s, "gitdir: ", 8) &&
	    !strchr("\r\n", s[8])))
		abort();
	if (xr_git_remote(s, n, "o/r") != remote_rule(s, n))
		abort();
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct tee t;
	struct xr_index *ix;

	if (!n)
		return 0;
	memset(&t, 0, sizeof t);
	cx = (d[0] & 3) == 1 && (d[0] & 32);
	if ((d[0] & 3) == 0)
	{
		static const int modes[] = {XR_PROSE, XR_MD, XR_CODE, XR_PLAIN};
		struct gram g = {n - 1, 0};
		char *s = malloc(n);	/* an exact block: no read past it */

		if (!s)
			return 0;
		memcpy(s, d + 1, n - 1);
		(void)xr_grammar(s, n - 1, modes[(d[0] >> 2) & 3],
		                 d[0] & 16 ? "/home/demo/proj" : NULL, gram_span, &g);
		free(s);
		return 0;
	}
	if ((d[0] & 3) == 3)
	{
		char *s = malloc(n);
		struct xr_span sp;

		if (!s)
			return 0;
		memcpy(s, d + 1, n - 1);
		s[n - 1] = '\0';
		if (d[0] & 4)
			resolvers(s, n - 1);
		else if (xr_ref_ok(s) && xr_ref_parse(s, "/w", &sp))
			check_key(&sp);
		free(s);
		return 0;
	}
	if ((d[0] & 7) == 5)
	{
		follow(d, n);
		if (xr_index_live)
			abort();
		return 0;
	}
	if (!(ix = xr_index_new()))
		return 0;
	xr_index_sink(ix, &t.ix);
	if ((d[0] & 3) == 1)
	{
		struct xr_claude *x = decoder();
		struct xr_chapters c = {0};
		struct xr_sink k = {tee_hit, &t};
		size_t i, s = 1;

		if (!x)
		{
			xr_index_free(ix);
			return 0;
		}
		xr_claude_refs(x, &k, 0x1234, NULL, NULL);
		for (i = 1; i <= n; i++)
			if (i == n || d[i] == '\n')
			{
				char *l = i > s ? malloc(i - s + 1) : NULL;

				if (l)
				{
					memcpy(l, d + s, i - s);
					l[i - s] = '\0';
					xr_claude_line(x, l, i - s, s, 0);
					free(l);
				}
				s = i + 1;
			}
		xr_claude_end(x, 0x1234, &c);
		xr_chapters_free(&c);
		xr_claude_free(x);
		emit(ix, 0);
		if (t.key[0])
		{
			scan(d, n, t.key);
			differ(d, n, t.key);
		}
	}
	else
	{
		struct xr_chapters c = {0};
		struct xr_shell sh;
		struct xr_sink k = {tee_hit, &t};
		size_t off = 1;
		uint64_t seq = 1000;

		xr_shell_begin(&sh, &c);
		if (xr_shell_refs(&sh, &k, d[0] & 4 ? "/home/demo" : "") == 0)
		{
			while (off + 8 <= n)
			{
				size_t l = (size_t)d[off] | (size_t)d[off + 1] << 8;
				struct xr_rec r;
				char *tx;

				if (off + 8 + l > n)
					l = n - off - 8;
				if (!(tx = malloc(l ? l : 1)))
					break;
				memcpy(tx, d + off + 8, l);
				if (d[off + 2] == 0xFE && !t.screen)
				{
					xr_shell_screen(&sh);
					t.screen = 1;
				}
				r.seq = seq++;
				r.kind = t.screen ? RING_K_OUTPUT : d[off + 2] % 7;
				r.flags = t.screen ? 0 : d[off + 3];
				r.t = (uint32_t)d[off + 4] | (uint32_t)d[off + 5] << 8;
				r.text = tx;
				r.len = l;
				xr_shell_feed(&sh, &r);
				free(tx);
				off += 8 + l;
			}
			xr_index_per_row(ix, !sh.npaired);
		}
		xr_shell_end(&sh);
		xr_chapters_free(&c);
		emit(ix, 1);
	}
	xr_index_free(ix);
	if (xr_index_live)
		abort();	/* K3: nothing outlives its query */
	return 0;
}
