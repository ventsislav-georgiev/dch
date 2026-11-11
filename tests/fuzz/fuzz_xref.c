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
**      xr_shell_feed with xr_shell_refs into an index, then xr_print;
**   3: xr_ref_ok and xr_ref_parse over the bytes as a C string.
** Path 1 with byte 0's bit 2 set is --follow's instead (#136): the transcript
** read whole, and read again in pieces at cut points the input seeds, a delta
** batch after each; the pieces end where the whole read does, with the same
** object printed, every delta line one schema-1 chapter or ref object, and a
** second diff of the last moment empty.
** Every hit handed to a sink lies inside its line; a definition's title is
** valid UTF-8, its passage within XR_PASSAGE scalars' bytes; the printed object is one line within the cap. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../../xray.h"
#include "../../bridge.h"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

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
};

static void
tee_hit(void *ctx, const struct xr_hit *h)
{
	struct tee *t = ctx;

	if (!h->sp || h->sp->s >= h->sp->e || h->sp->e > h->ll || !h->line)
		abort();
	check_key(h->sp);
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

/* A full --ref over the same transcript for key; its stream must hold
** first, done last, one line each. */
static void
scan(const uint8_t *d, size_t n, const char *key)
{
	struct xr_claude *x = xr_claude_new();
	struct xr_chapters c = {0};
	struct xr_scan *s;
	struct xr_sink k;
	char *buf = NULL;
	size_t bl = 0, i, st = 1;
	FILE *f;

	if (!x)
		return;
	if (!(f = open_memstream(&buf, &bl)))
	{
		xr_claude_free(x);
		return;
	}
	if (!(s = xr_scan_new(f, key, "fuzz", "", 0, 1, 0, 300, xr_now_ms())))
	{
		fclose(f);
		free(buf);
		xr_claude_free(x);
		return;
	}
	if (xr_scan_first(s, "11111111-2222-4333-8444-555555555555",
	                  XR_ISSUE, key) == 0)
	{
		xr_scan_sink(s, &k);
		xr_claude_refs(x, &k, 0x1234, xr_scan_tick, s);
		for (i = st; i <= n; i++)
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
		(void)xr_scan_done(s);
	}
	xr_scan_free(s);
	xr_claude_free(x);
	fclose(f);
	if (bl && (buf[bl - 1] != '\n' || !strstr(buf, "{\"schema\":1,\"first\"")))
		abort();
	free(buf);
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
	struct xr_claude *one = xr_claude_new(), *inc = xr_claude_new();
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

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct tee t;
	struct xr_index *ix;

	if (!n)
		return 0;
	memset(&t, 0, sizeof t);
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
		if (xr_ref_ok(s) && xr_ref_parse(s, "/w", &sp))
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
		struct xr_claude *x = xr_claude_new();
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
			scan(d, n, t.key);
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
				r.seq = seq++;
				r.kind = d[off + 2] % 7;
				r.flags = d[off + 3];
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
