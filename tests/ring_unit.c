/*
 * ring_unit.c - unit tests for the X-ray shell ring (#119), plus the
 * test-only .ring.ndjson loader and dumper (never built into dch).
 *
 *   cc -I. tests/ring_unit.c ring.c -o ring_unit                  (ring only)
 *   cc -DWITH_VT -I. -I<ghostty include> tests/ring_unit.c ring.c vt.c <.a>
 *                                                     (+ capture through vt.c)
 *   ring_unit                       run every test; exit 1 on a failure
 *   ring_unit roundtrip <in.ndjson> load the file into a ring, dump it back
 *                                   to stdout (the golden round-trip)
 *
 * tests/run.sh builds and runs it. Test numbers are the proposal's §11.11.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "ring.h"
#ifdef WITH_VT
#include "vt.h"
#endif

static int fails;

#define CHECK(name, cond) do { \
	if (cond) \
		printf("  ok   %s\n", name); \
	else \
	{ \
		printf("  FAIL %s (%s:%d)\n", name, __FILE__, __LINE__); \
		fails++; \
	} \
} while (0)

/* ---- the record list a ring holds, via ring_read ---------------------- */

struct rec
{
	uint64_t seq;
	int kind, flags;
	uint32_t t;
	char *text;
	size_t len;
};

static struct rec *
recs(struct dch_ring *r, uint64_t from, size_t *n, int *status)
{
	static unsigned char buf[RING_MAX + 64];
	struct rec *v;
	uint64_t first, cnt, i;
	size_t got, off = 0;

	got = ring_read(r, from, buf, sizeof buf, &first, &cnt, status);
	v = calloc(cnt + 1, sizeof *v);
	for (i = 0; i < cnt; i++)
	{
		size_t l = (size_t)buf[off] | (size_t)buf[off + 1] << 8;

		v[i].seq = first + i;
		v[i].kind = buf[off + 2];
		v[i].flags = buf[off + 3];
		v[i].t = (uint32_t)buf[off + 4] | (uint32_t)buf[off + 5] << 8 |
		         (uint32_t)buf[off + 6] << 16 |
		         (uint32_t)buf[off + 7] << 24;
		v[i].text = malloc(l + 1);
		memcpy(v[i].text, buf + off + RING_HDR, l);
		v[i].text[l] = '\0';
		v[i].len = l;
		off += RING_HDR + l;
	}
	if (off != got)
		v[0].kind = -1;		/* torn: a test fails on it */
	*n = (size_t)cnt;
	return v;
}

static void
recs_free(struct rec *v, size_t n)
{
	size_t i;

	for (i = 0; i < n; i++)
		free(v[i].text);
	free(v);
}

/* ---- .ring.ndjson: {"seq","kind","flags","t","text"} (+ "exit") -------- */

static const char *kname[] = { "?", "prompt", "input", "output", "mark",
                               "gap" };
static const char esc_from[] = "\"\\\b\f\n\r\t", esc_to[] = "\"\\bfnrt";

static void
dump_json_str(FILE *f, const char *s, size_t n)
{
	size_t i;

	fputc('"', f);
	for (i = 0; i < n; i++)
	{
		unsigned char c = (unsigned char)s[i];

		const char *e = strchr(esc_from, c);

		/* python's json.dumps(ensure_ascii=False) escapes */
		if (c && e)
			fprintf(f, "\\%c", esc_to[e - esc_from]);
		else if (c < 0x20)
			fprintf(f, "\\u%04x", c);
		else
			fputc(c, f);
	}
	fputc('"', f);
}

static void
dump_ndjson(FILE *f, const struct rec *v, size_t n)
{
	size_t i;

	for (i = 0; i < n; i++)
	{
		fprintf(f, "{\"seq\":%llu,\"kind\":\"%s\",\"flags\":%d,\"t\":%u,"
		        "\"text\":", (unsigned long long)v[i].seq,
		        kname[v[i].kind >= 1 && v[i].kind <= 5 ? v[i].kind : 0],
		        v[i].flags, v[i].t);
		dump_json_str(f, v[i].text, v[i].len);
		if (v[i].kind == RING_K_MARK && (v[i].flags & RING_F_EXIT))
			fprintf(f, ",\"exit\":%ld", strtol(v[i].text + 2, NULL,
			                                   10));
		fputs("}\n", f);
	}
}

/* The loader only reads what the dumper writes: fixed key order, the
** escapes above (\uXXXX for a control byte only). Returns -1 on anything
** else. */
static int
load_line(const char *l, uint64_t *seq, int *kind, int *flags, uint32_t *t,
          char *text, size_t *len)
{
	unsigned long long sq;
	unsigned tt;
	char kn[16];
	int fl, k, used = 0;
	const char *p;
	size_t n = 0;

	if (sscanf(l, "{\"seq\":%llu,\"kind\":\"%15[a-z]\",\"flags\":%d,"
	           "\"t\":%u,\"text\":\"%n", &sq, kn, &fl, &tt, &used) != 4 ||
	    !used)
		return -1;
	for (k = 1; k <= 5 && strcmp(kname[k], kn); k++)
		;
	if (k > 5)
		return -1;
	for (p = l + used; *p && *p != '"'; p++)
	{
		if (*p != '\\')
		{
			text[n++] = *p;
			continue;
		}
		p++;
		if (*p && strchr(esc_to, *p))
			text[n++] = esc_from[strchr(esc_to, *p) - esc_to];
		else if (*p == 'u')
		{
			unsigned c;

			if (sscanf(p + 1, "%4x", &c) != 1 || c > 0x7f)
				return -1;
			text[n++] = (char)c;
			p += 4;
		}
		else
			return -1;
	}
	if (*p != '"')
		return -1;
	*seq = sq;
	*kind = k;
	*flags = fl;
	*t = tt;
	*len = n;
	return 0;
}

static int
roundtrip(const char *path)
{
	static char line[1 << 18], text[1 << 17];
	struct dch_ring r;
	struct rec *v;
	FILE *f = fopen(path, "r");
	size_t n, len;
	uint64_t seq, base = 0;
	uint32_t t;
	int kind, flags, st, first = 1;

	if (!f)
		return 2;
	if (ring_init(&r, RING_DEFAULT, 0) < 0)
	{
		fclose(f);
		return 2;
	}
	while (fgets(line, sizeof line, f))
	{
		if (load_line(line, &seq, &kind, &flags, &t, text, &len) < 0)
		{
			fprintf(stderr, "bad line: %s", line);
			fclose(f);
			ring_free(&r);
			return 1;
		}
		if (first)
		{
			r.head_seq = r.tail_seq = base = seq;
			first = 0;
		}
		if (seq != r.tail_seq || ring_append(&r, kind, flags, t, text,
		    len) == UINT64_MAX)
		{
			fprintf(stderr, "seq %llu out of order\n",
			        (unsigned long long)seq);
			fclose(f);
			ring_free(&r);
			return 1;
		}
	}
	fclose(f);
	v = recs(&r, base, &n, &st);
	dump_ndjson(stdout, v, n);
	recs_free(v, n);
	ring_free(&r);
	return 0;
}

/* ---- ring.c ------------------------------------------------------------ */

static void
test_parse(void)
{
	CHECK("parse: 2M", ring_parse_size("2M") == 2 << 20);
	CHECK("parse: 512KiB", ring_parse_size("512KiB") == 512 << 10);
	CHECK("parse: 0 = off", ring_parse_size("0") == 0);
	CHECK("parse: 100 rounds up to a chunk",
	      ring_parse_size("100") == RING_CHUNK);
	CHECK("parse: 64M clamps to 16M", ring_parse_size("64M") == RING_MAX);
	CHECK("parse: junk refused", ring_parse_size("2X") == -1 &&
	      ring_parse_size("") == -1 && ring_parse_size("-1") == -1 &&
	      ring_parse_size(" 2M") == -1);
}

static void
test_ids(void)
{
	char a[18], b[18];
	uint64_t x = ring_seq_base(), y = ring_seq_base();
	int i, hex = 1;

	ring_session_id(a);
	ring_session_id(b);
	for (i = 1; i < 17; i++)
		hex &= !!strchr("0123456789abcdef", a[i]) && a[i];
	CHECK("session_id: m + 16 lowercase hex", a[0] == 'm' && hex &&
	      !a[17]);
	CHECK("session_id: fresh each time", strcmp(a, b) != 0);
	CHECK("seq base: under 2^48, fresh each time",
	      x < (1ull << 48) && y < (1ull << 48) && x != y);
}

/* 1: wrap-around at exactly 2 MiB, a record straddling the end */
static void
test_wrap(void)
{
	struct dch_ring r;
	struct rec *v;
	static char text[4096];	/* the boundary case appends 4088 */
	size_t n, i, total = 0;
	int st, ok = 1;

	ring_init(&r, RING_DEFAULT, 1000);
	memset(text, 'x', sizeof text);
	/* 2 MiB / 2008 is not whole: some record straddles the end */
	for (i = 0; i < 3000; i++)
	{
		text[0] = (char)('a' + i % 26);
		ring_append(&r, RING_K_OUTPUT, 0, (uint32_t)i, text, 2000);
		total += 2008;
		if (ring_check(&r))
			ok = 0;
	}
	CHECK("1: invariants hold across the wrap", ok);
	CHECK("1: the ring is full, never over", r.tail - r.head <= r.cap &&
	      r.tail - r.head > r.cap - 2008);
	CHECK("1: the logical tail passed the end", r.tail > r.cap &&
	      total == r.tail);
	v = recs(&r, r.head_seq, &n, &st);
	for (i = 0; i < n; i++)
		if (v[i].len != 2000 || v[i].text[0] !=
		    (char)('a' + (v[i].seq - 1000) % 26) || v[i].text[1] != 'x')
			ok = 0;
	CHECK("1: every record reads back whole, straddlers too",
	      ok && n == (r.tail - r.head) / 2008 && v[0].kind ==
	      RING_K_OUTPUT && st == 0);
	recs_free(v, n);
	ring_free(&r);

	/* a record ending exactly at the 2 MiB boundary */
	ring_init(&r, RING_DEFAULT, 0);
	for (i = 0; i < (RING_DEFAULT / 4096); i++)
		ring_append(&r, RING_K_OUTPUT, 0, 0, text, 4096 - RING_HDR);
	CHECK("1: exactly full at 2 MiB, nothing evicted",
	      r.tail == RING_DEFAULT && r.head == 0 && r.head_seq == 0);
	ring_append(&r, RING_K_OUTPUT, 0, 0, "y", 1);
	CHECK("1: the next one evicts one whole record",
	      r.head == 4096 && r.head_seq == 1 && !ring_check(&r));
	ring_free(&r);
}

/* A read from any seq lands on that record (the per-chunk index across
** laps, evictions and a clear), and a far-back read after a tail read
** costs one chunk of records, not the whole ring (the master thread). */
static void
test_index(void)
{
	static unsigned char buf[600];
	struct dch_ring r;
	char text[400];
	uint64_t s, from, first, cnt;
	size_t i, l;
	int st, k, ok = 1;
	clock_t t0;

	ring_init(&r, RING_DEFAULT, 5000);
	memset(text, '.', sizeof text);
	srand(119);
	for (k = 0; k < 3; k++)
	{
		for (i = 0; i < 30000; i++)
		{
			s = r.tail_seq;
			l = (size_t)snprintf(text, 24, "%llu", (unsigned long long)s);
			text[l] = '.';
			ring_append(&r, RING_K_OUTPUT, 0, 0, text,
			            l + 1 + (size_t)(rand() % 300));
		}
		for (i = 0; i < 3000; i++)
		{
			char want[24];

			from = r.head_seq + (uint64_t)rand() %
			       (r.tail_seq - r.head_seq);
			if (i % 2)	/* a follower's tail read moves the cache */
				ring_read(&r, r.tail_seq - 1, buf, sizeof buf,
				          &first, &cnt, &st);
			ring_read(&r, from, buf, sizeof buf, &first, &cnt, &st);
			l = (size_t)snprintf(want, sizeof want, "%llu",
			                     (unsigned long long)from);
			if (st || first != from || !cnt ||
			    memcmp(buf + RING_HDR, want, l) ||
			    buf[RING_HDR + l] != '.')
				ok = 0;
		}
		if (k == 1)
			ring_clear(&r);
	}
	CHECK("index: a read from any seq lands on it, laps and a clear",
	      ok && r.tail > 2 * r.cap && !ring_check(&r));
	ring_free(&r);

	/* 2M empty records in 16 MiB: the worst case for a walk */
	ring_init(&r, RING_MAX, 0);
	while (r.tail - r.head + RING_HDR <= r.cap)
		ring_append(&r, RING_K_OUTPUT, 0, 0, "", 0);
	ring_append(&r, RING_K_OUTPUT, 0, 0, "", 0);
	t0 = clock();
	for (i = 0, ok = 1; i < 2000; i++)
	{
		ring_read(&r, r.tail_seq - 1, buf, 64, &first, &cnt, &st);
		ring_read(&r, r.head_seq + 1, buf, 64, &first, &cnt, &st);
		if (st || first != r.head_seq + 1 || cnt != 8)
			ok = 0;
	}
	CHECK("index: 2000 head reads after tail reads, 2M records, < 2 s cpu",
	      ok && r.tail_seq - r.head_seq == RING_MAX / RING_HDR &&
	      (double)(clock() - t0) / CLOCKS_PER_SEC < 2.0);
	ring_free(&r);
}

/* 2: eviction is whole records, oldest first; chapters_evicted */
static void
test_evict(void)
{
	struct dch_ring r;
	static char text[60000];
	int i;

	ring_init(&r, RING_CHUNK * 2, 7);
	ring_append(&r, RING_K_MARK, 0, 0, "B", 1);
	ring_append(&r, RING_K_OUTPUT, 0, 0, "o", 1);
	memset(text, 'z', sizeof text);
	ring_append(&r, RING_K_OUTPUT, 0, 0, text, 60000);
	ring_append(&r, RING_K_OUTPUT, 0, 0, text, 60000);
	ring_append(&r, RING_K_OUTPUT, 0, 0, text, 60000);
	CHECK("2: whole records go, oldest first; no prompt, no flag",
	      !r.chapters_evicted && r.head_seq == 10 && !ring_check(&r));
	ring_append(&r, RING_K_PROMPT, 0, 0, "$ ", 2);
	ring_append(&r, RING_K_MARK, 0, 0, "A", 1);
	for (i = 0; i < 3; i++)
		ring_append(&r, RING_K_OUTPUT, 0, 0, text, 60000);
	CHECK("2: an evicted prompt sets chapters_evicted",
	      r.chapters_evicted && !ring_check(&r));
	ring_clear(&r);
	CHECK("clear: empty, seqs continue, epoch moves, flag reset",
	      r.head_seq == r.tail_seq && r.tail_seq == 17 && r.epoch == 1 &&
	      !r.chapters_evicted && !ring_check(&r));
	ring_free(&r);
}

/* F7 / 11.8: a row over 65535 bytes is cut on a UTF-8 boundary */
static void
test_trunc(void)
{
	struct dch_ring r;
	struct rec *v;
	static char text[70000];
	size_t n, i;
	int st, ok = 1;

	ring_init(&r, RING_DEFAULT, 0);
	for (i = 0; i + 3 <= sizeof text; i += 3)
		memcpy(text + i, "\xe4\xb8\xad", 3);	/* 3-byte chars */
	ring_append(&r, RING_K_OUTPUT, 0, 0, text, 69999);
	v = recs(&r, 0, &n, &st);
	for (i = 0; n == 1 && i < v[0].len; i += 3)
		if (memcmp(v[0].text + i, "\xe4\xb8\xad", 3))
			ok = 0;
	CHECK("11.8: cut at the last boundary <= 65535, flagged",
	      n == 1 && v[0].len == 65535 && ok &&
	      (v[0].flags & RING_F_TRUNC));
	recs_free(v, n);
	ring_free(&r);
}

/* 12: a read races an append that evicts its seq: range, no torn record */
static void
test_race(void)
{
	struct dch_ring r;
	struct rec *v;
	static char text[30000];
	size_t n;
	uint64_t s;
	int st;

	ring_init(&r, RING_CHUNK, 50);
	memset(text, 'q', sizeof text);
	s = ring_append(&r, RING_K_OUTPUT, 0, 0, text, 30000);
	ring_append(&r, RING_K_OUTPUT, 0, 0, text, 30000);
	v = recs(&r, s + 1, &n, &st);	/* a follower parks at s+1 */
	recs_free(v, n);
	ring_append(&r, RING_K_OUTPUT, 0, 0, text, 30000);	/* evicts s */
	ring_append(&r, RING_K_OUTPUT, 0, 0, text, 30000);	/* and s+1 */
	v = recs(&r, s + 1, &n, &st);
	CHECK("12: an evicted seq answers range from the head, whole records",
	      (st & RING_R_RANGE) && n == 2 && v[0].seq == s + 2 &&
	      v[0].kind == RING_K_OUTPUT && v[0].len == 30000 &&
	      v[1].len == 30000);
	recs_free(v, n);
	v = recs(&r, r.tail_seq + 5, &n, &st);
	CHECK("12: a future seq answers range too", (st & RING_R_RANGE) &&
	      n == 2);
	recs_free(v, n);
	v = recs(&r, r.tail_seq, &n, &st);
	CHECK("read at the tail: nothing, no range", !st && n == 0);
	recs_free(v, n);
	ring_free(&r);
}

/* 10 (unit half): the handover format validates before it loads */
static void
test_export(void)
{
	struct dch_ring a, b;
	unsigned char *x;
	size_t n, i;
	int ok;

	ring_init(&a, RING_DEFAULT, 123456789);
	for (i = 0; i < 1000; i++)
		ring_append(&a, i % 7 ? RING_K_OUTPUT : RING_K_PROMPT, 0,
		            (uint32_t)i, "hello world", 5 + i % 6);
	a.chapters_evicted = 1;
	a.epoch = 4;
	n = ring_export_size(&a);
	x = malloc(n);
	ring_export(&a, x);
	ring_init(&b, RING_DEFAULT, 1);
	ok = ring_import(&b, x, n) == 0;
	CHECK("10: export/import keeps seqs, records, flags, epoch",
	      ok && b.head_seq == a.head_seq && b.tail_seq == a.tail_seq &&
	      b.tail - b.head == a.tail - a.head && b.chapters_evicted &&
	      b.epoch == 4 && !ring_check(&b));
	ring_free(&b);
	ring_init(&b, RING_DEFAULT, 1);
	x[48 + 2] = 9;			/* a bad kind in the first record */
	CHECK("10: a bad record loads nothing", ring_import(&b, x, n) < 0 &&
	      b.tail == b.head);
	x[48 + 2] = RING_K_OUTPUT;
	CHECK("10: a short blob loads nothing",
	      ring_import(&b, x, n - 1) < 0 && ring_import(&b, x, 20) < 0);
	x[0] = 'X';
	CHECK("10: a bad magic loads nothing", ring_import(&b, x, n) < 0);
	x[0] = 'd';
	ring_free(&b);
	ring_init(&b, RING_CHUNK, 1);
	CHECK("10: a smaller ring keeps the newest records",
	      ring_import(&b, x, n) == 0 && b.tail_seq == a.tail_seq &&
	      !ring_check(&b));
	ring_free(&b);
	ring_free(&a);
	free(x);
}

#ifdef WITH_VT
/* ---- capture through vt.c (the mirror, libghostty) --------------------- */

static struct dch_ring R;

static void
feed(const char *s)
{
	dch_vt_feed((const unsigned char *)s, strlen(s));
}

static void
start(int cols, int rows, int sb)
{
	dch_vt_free();
	ring_free(&R);
	ring_init(&R, RING_DEFAULT, 1000);
	dch_vt_capture(&R);
	if (dch_vt_init(cols, rows, sb) < 0)
	{
		printf("  FAIL vt init\n");
		exit(1);
	}
}

/* Output records' texts joined with '\n' */
static char *
outs(int kinds_mask)
{
	struct rec *v;
	size_t n, i, cap = 1 << 20, w = 0;
	char *o = malloc(cap);
	int st;

	v = recs(&R, R.head_seq, &n, &st);
	for (i = 0; i < n; i++)
	{
		if (!(kinds_mask & 1 << v[i].kind))
			continue;
		while (w + v[i].len + 2 > cap)
			o = realloc(o, cap *= 2);
		memcpy(o + w, v[i].text, v[i].len);
		w += v[i].len;
		o[w++] = '\n';
	}
	o[w] = '\0';
	recs_free(v, n);
	return o;
}

static int
count_kind(int kind, const char *text)
{
	struct rec *v;
	size_t n, i;
	int st, c = 0;

	v = recs(&R, R.head_seq, &n, &st);
	for (i = 0; i < n; i++)
		if (v[i].kind == kind && (!text ||
		    !strcmp(v[i].text + strspn(v[i].text, " "), text)))
			c++;
	recs_free(v, n);
	return c;
}

/* Lines 1..N in order, each once, as output records: the first absent one
** (0 = all there) */
static long
seq_hole(long from, long to)
{
	char *o = outs(1 << RING_K_OUTPUT), *p = o;
	long want = from;

	while (*p && want <= to)
	{
		char *e = strchr(p, '\n');

		*e = '\0';
		if (atol(p) == want && strspn(p, "0123456789") == strlen(p))
			want++;
		else if (want > from)
			break;		/* a hole, or a duplicate */
		p = e + 1;
	}
	free(o);
	return want > to ? 0 : want;
}

static void
seq_feed(long from, long to)
{
	char b[32];
	long i;

	for (i = from; i <= to; i++)
	{
		snprintf(b, sizeof b, "%ld\r\n", i);
		feed(b);
	}
}

/* One big buffer, as a pty read would bring it */
static void
seq_feed_bulk(long from, long to)
{
	size_t cap = (size_t)(to - from + 1) * 24 + 1, w = 0;
	char *b = malloc(cap);
	long i;

	for (i = from; i <= to; i++)
		w += (size_t)snprintf(b + w, cap - w, "%ld\r\n", i);
	dch_vt_feed((unsigned char *)b, w);
	free(b);
}

static void
test_capture_basic(void)
{
	start(80, 24, 2000);
	seq_feed_bulk(1, 100000);
	CHECK("seq 1 100000 at 80 cols (one feed): every row, in order, once",
	      seq_hole(1, 100000 - 24) == 0);
	CHECK("seq 1 100000: no gap record", count_kind(RING_K_GAP, NULL) == 0);
	CHECK("seq 1 100000: the screen rows are not in the ring yet",
	      seq_hole(100000 - 22, 100000) != 0);
	start(80, 24, 0);
	seq_feed_bulk(1, 100000);
	CHECK("DCH_SCROLLBACK=0: every row still captured, no gap",
	      seq_hole(1, 100000 - 24) == 0 && !count_kind(RING_K_GAP, NULL));
	{
		char *h = NULL;
		size_t hl = 0;

		dch_vt_history(1000, &h, &hl);
		CHECK("DCH_SCROLLBACK=0: the capture-only history never shows",
		      h && hl == 0);
		free(h);
		dch_vt_snapshot(DCH_VT_FMT_PLAIN, 1000, &h, &hl);
		CHECK("DCH_SCROLLBACK=0: --recent sees the screen only",
		      h && !strstr(h, "99970\n") && strstr(h, "99990"));
		free(h);
	}
	start(80, 24, 100);
	seq_feed_bulk(1, 5000);
	{
		char *h = NULL;
		size_t hl = 0, lines = 0, i;

		dch_vt_history(100000, &h, &hl);
		for (i = 0; i < hl; i++)
			lines += h[i] == '\n';
		CHECK("DCH_SCROLLBACK=100: history shows 100..100+page rows",
		      lines >= 100 && lines <= 100 + 49152 / 81);
		free(h);
	}
}

static void
test_wrap_join(void)
{
	char line[300], *o;
	int i;

	start(80, 24, 2000);
	for (i = 0; i < 250; i++)
		line[i] = (char)('a' + i % 26);
	memcpy(line + 250, "\r\n", 3);
	feed(line);
	feed("tail   \r\n");
	seq_feed(1, 30);
	{
		struct rec *v;
		size_t n, i, w = 0;
		char j[400];
		int st, flags_ok = 1;

		v = recs(&R, R.head_seq, &n, &st);
		for (i = 0; i < n && w + v[i].len < sizeof j; i++)
		{
			if (i && !(v[i].flags & RING_F_CONT))
				break;
			if (!i && (v[i].flags & RING_F_CONT))
				flags_ok = 0;
			memcpy(j + w, v[i].text, v[i].len);
			w += v[i].len;
		}
		CHECK("soft wraps: continuation records (CONT) join to the line",
		      flags_ok && w == 250 && !memcmp(j, line, 250) &&
		      i < n && !strcmp(v[i].text, "tail"));
		recs_free(v, n);
	}
	/* rows that scroll in together join in the formatter: one record */
	start(80, 24, 2000);
	{
		char big[400];

		memcpy(big, line, 252);
		memcpy(big + 252, "tail   \r\n", 10);
		strcpy(big + 261, "");
		feed(big);
		feed("\033[30S");
		o = outs(1 << RING_K_OUTPUT);
		CHECK("soft wraps scrolled in at once: one record, trimmed",
		      !strncmp(o, line, 250) && o[250] == '\n' &&
		      !strncmp(o + 251, "tail\n", 5));
		free(o);
	}
}

/* 9a/9b: rows on screen when ED 3 comes never enter, either clear order */
static void
test_clear_orders(void)
{
	static const char *forms[] = {
		"\033[H\033[2J\033[3J", "\033[3J\033[H\033[2J", "\033[3J",
		"\033[03J", "\033[?3J", "\033[3\bJ", "\033c",
	};
	static const char *names[] = {
		"9a: ED 2 then ED 3", "9b: ED 3 then ED 2", "ED 3",
		"ED 3 spelled ESC[03J", "ED 3 spelled ESC[?3J",
		"ED 3 with a C0 inside", "RIS",
	};
	size_t f;

	for (f = 0; f < sizeof forms / sizeof *forms; f++)
	{
		char name[96], *o;
		uint32_t e;
		size_t cut, len = strlen(forms[f]);

		/* every split point of the clear across two feeds */
		for (cut = 0; cut <= len; cut++)
		{
			int ok;

			start(80, 24, 2000);
			seq_feed(1, 200);
			feed("onscreen-a\r\nonscreen-b\r\n");
			e = R.epoch;
			dch_vt_feed((const unsigned char *)forms[f], cut);
			dch_vt_feed((const unsigned char *)forms[f] + cut,
			            len - cut);
			seq_feed(1000, 1100);
			o = outs(1 << RING_K_OUTPUT);
			/* ED 3 alone leaves the screen: those rows scroll
			** into history afterwards, as the user sees them */
			ok = R.epoch != e && (!strstr(o, "onscreen-") ||
			     !strstr(forms[f], "2J")) &&
			     (!strstr(o, "onscreen-") || f != 6) &&
			     !strstr(o, "\n100\n") && strncmp(o, "100\n", 4) &&
			     seq_hole(1000, 1100 - 24) == 0;
			free(o);
			if (!ok || cut == len)
			{
				snprintf(name, sizeof name,
				         "%s empties the ring (split at %zu/%zu)",
				         names[f], cut, len);
				CHECK(name, ok);
				if (!ok)
					break;
			}
		}
	}
}

/* §14.1: SU and ED 2 floods at the history cap never empty, never gap */
static void
test_flood(void)
{
	static char buf[1 << 20];
	size_t w = 0;
	uint32_t e;
	int i, sb;

	for (sb = 0; sb <= 2000; sb += 2000)
	{
		start(80, 24, sb);
		seq_feed_bulk(1, 5000);		/* at the cap */
		e = R.epoch;
		w = 0;
		for (i = 0; i < 4000; i++)
			w += (size_t)snprintf(buf + w, sizeof buf - w,
			    "m%d\033[999S\033[2J", i);
		dch_vt_feed((unsigned char *)buf, w);
		CHECK(sb ? "flood ESC[999S ESC[2J at the cap: no clear, no gap"
		         : "same flood at DCH_SCROLLBACK=0",
		      R.epoch == e && !count_kind(RING_K_GAP, NULL) &&
		      count_kind(RING_K_OUTPUT, "m3998") == 1 &&
		      count_kind(RING_K_OUTPUT, "m0") == 1);
	}
	start(80, 24, 2000);
	seq_feed_bulk(1, 5000);
	e = R.epoch;
	feed("big\033[4294967295Sx\033[99999999999999999999S");
	seq_feed(1, 30);
	CHECK("CSI 4294967295 S (and a 20-digit one): a screen, no clear",
	      R.epoch == e && count_kind(RING_K_OUTPUT, "big") == 1 &&
	      !count_kind(RING_K_GAP, NULL));
	/* a 4000-digit param: the slice still holds the final byte */
	start(1024, 1024, 0);
	seq_feed_bulk(1, 3000);
	e = R.epoch;
	w = 0;
	for (i = 0; i < 40; i++)
	{
		w += (size_t)snprintf(buf + w, sizeof buf - w, "p%d\033[", i);
		memset(buf + w, '9', 4000);
		w += 4000;
		buf[w++] = 'S';
	}
	dch_vt_feed((unsigned char *)buf, w);
	CHECK("a 4000-digit CSI S flood: no clear, no gap",
	      R.epoch == e && !count_kind(RING_K_GAP, NULL) &&
	      count_kind(RING_K_OUTPUT, "p38") == 1);
	/* a 1024x1024 grid: one ESC costs more than the budget */
	start(1024, 1024, 0);
	seq_feed_bulk(1, 3000);
	e = R.epoch;
	w = 0;
	for (i = 0; i < 50; i++)
		w += (size_t)snprintf(buf + w, sizeof buf - w,
		    "w%d\033[999S", i);
	dch_vt_feed((unsigned char *)buf, w);
	CHECK("1024x1024: SU floods keep the ref (one sequence per slice)",
	      R.epoch == e && !count_kind(RING_K_GAP, NULL) &&
	      count_kind(RING_K_OUTPUT, "w48") == 1);
	/* REP (CSI n b) pushes up to 65535 cells, wide ones two each: one
	** sequence is ~1640 rows at 80 cols, more than a screen */
	for (sb = 0; sb <= 2000; sb += 2000)
	{
		start(80, 24, sb);
		seq_feed_bulk(1, 3000);
		e = R.epoch;
		w = 0;
		for (i = 0; i < 30; i++)
			w += (size_t)snprintf(buf + w, sizeof buf - w,
			    "r%d\r\n%s\033[65535b\r\n", i,
			    i & 1 ? "\xe4\xb8\xad" : "x");
		dch_vt_feed((unsigned char *)buf, w);
		seq_feed(1, 30);
		CHECK(sb ? "REP 65535 flood (wide too): no clear, no gap"
		         : "REP 65535 flood at DCH_SCROLLBACK=0",
		      R.epoch == e && !count_kind(RING_K_GAP, NULL) &&
		      count_kind(RING_K_OUTPUT, "r0") == 1 &&
		      count_kind(RING_K_OUTPUT, "r29") == 1 && !ring_check(&R));
	}
	/* top-anchored DECSTBM region scrolls reach history */
	start(80, 24, 2000);
	seq_feed(1, 30);
	feed("\033[1;20r\033[20;1Hregion-a\n\n\n\n\n\n\n\n\n\n\033[r"
	     "\033[24;1H\r\n");
	seq_feed(31, 80);
	CHECK("DECSTBM 1;20 scrolls enter the ring once",
	      count_kind(RING_K_OUTPUT, "region-a") == 1);
}

/* T9i: the ref is never set on the alt screen; any clear empties the ring */
static void
test_alt(void)
{
	char *h = NULL, *o;
	size_t hl;
	long before;

	start(80, 24, 2000);
	seq_feed(1, 100);
	before = (long)(R.tail_seq - R.head_seq);
	feed("\033[?1049h");
	seq_feed(5000, 5300);
	feed("\033[?1049l");
	o = outs(1 << RING_K_OUTPUT);
	CHECK("T9i: alt-screen rows never enter the ring",
	      !strstr(o, "5000") && (long)(R.tail_seq - R.head_seq) == before);
	free(o);
	seq_feed(101, 130);
	CHECK("T9i: capture resumes on the primary, nothing lost",
	      seq_hole(1, 130 - 24) == 0);

	/* in-band ED 3 on the alt screen: libghostty leaves the primary
	** history alone. Oracle: the mirror's own history, not the ref. */
	start(80, 24, 2000);
	seq_feed(1, 100);
	before = (long)(R.tail_seq - R.head_seq);
	feed("\033[?1049h\033[3J\033[?1049l");
	dch_vt_history(1000, &h, &hl);
	CHECK("T9i: ED 3 on alt keeps the primary history (oracle: history)",
	      h && strstr(h, "\n50\r\n") != NULL);
	CHECK("T9i: ... and so the ring, which mirrors it",
	      (long)(R.tail_seq - R.head_seq) == before);
	free(h);

	start(80, 24, 2000);
	seq_feed(1, 100);
	feed("\033[?1049h");
	feed("\033c");
	CHECK("T9i: RIS on the alt screen empties the ring",
	      R.tail_seq == R.head_seq && R.epoch == 1);
	start(80, 24, 2000);
	seq_feed(1, 100);
	feed("\033[?1049h\033]133;A\007alt-prompt\033[?1049l");
	dch_vt_clear();
	CHECK("clear message (H, 2J, 3J into the mirror) empties the ring",
	      R.tail_seq == R.head_seq && R.epoch == 1);
	start(80, 24, 2000);
	seq_feed(1, 100);
	feed("\033[?1049h");
	dch_vt_clear();
	CHECK("T9i: the clear message on the alt screen empties it too",
	      R.tail_seq == R.head_seq && R.epoch == 1);
	feed("\033[?1049l");
	seq_feed(200, 260);
	CHECK("T9i: after it, new primary rows are captured",
	      seq_hole(200, 260 - 24) == 0);
}

/* T9j: resize and reflow re-anchor, never clear, never duplicate */
static void
test_resize(void)
{
	char line[200], *o;
	int i, ok = 1;

	start(80, 24, 2000);
	seq_feed(1, 300);
	for (i = 0; i < 20; i++)
	{
		snprintf(line, sizeof line, "keep-%02d\r\n", i);
		feed(line);
	}
	dch_vt_resize(80, 10);		/* pushes screen rows into history */
	dch_vt_resize(80, 40);		/* pulls them back */
	dch_vt_resize(40, 40);		/* reflow narrower */
	dch_vt_resize(120, 24);		/* and wider */
	seq_feed(1000, 1100);
	o = outs(1 << RING_K_OUTPUT);
	for (i = 0; i < 20; i++)
	{
		char *p;

		snprintf(line, sizeof line, "\nkeep-%02d\n", i);
		p = strstr(o, line);
		if (!p || strstr(p + 1, line))
			ok = 0;
	}
	CHECK("T9j: rows a resize pushes are captured once, pulls add none",
	      ok);
	CHECK("T9j: no clear, no gap, the sequence intact",
	      R.epoch == 0 && !count_kind(RING_K_GAP, NULL) &&
	      seq_hole(1, 300) == 0 && seq_hole(1000, 1100 - 24) == 0);
	free(o);

	/* width-only reflow of long wrapped lines */
	start(80, 24, 2000);
	for (i = 0; i < 40; i++)
	{
		snprintf(line, sizeof line, "rz%03d-%s\r\n", i,
		         "abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmnopqr"
		         "stuvwxyz0123456789abcdefghijklmnopqrstuvwxyz");
		feed(line);
	}
	dch_vt_resize(40, 24);
	dch_vt_resize(130, 24);
	dch_vt_resize(80, 24);
	seq_feed(1, 60);
	o = outs(1 << RING_K_OUTPUT);
	for (ok = 1, i = 0; i < 40; i++)
	{
		char *p;

		snprintf(line, sizeof line, "rz%03d-", i);
		p = strstr(o, line);
		if (!p || strstr(p + 1, line))
			ok = 0;
	}
	CHECK("T9j: width-only reflows: every wrapped line once, whole",
	      ok && R.epoch == 0 && !count_kind(RING_K_GAP, NULL));
	free(o);
	/* resize while alt: re-anchored when the primary comes back */
	start(80, 24, 2000);
	seq_feed(1, 100);
	feed("\033[?1049h");
	dch_vt_resize(60, 12);
	feed("\033[?1049l");
	seq_feed(101, 200);
	CHECK("T9j: a resize on the alt screen re-anchors on return",
	      R.epoch == 0 && !count_kind(RING_K_GAP, NULL) &&
	      seq_hole(101, 200 - 12) == 0);
}

/* 5: OSC 133 marks land between the right records; C's input flag */
static void
test_marks(void)
{
	struct rec *v;
	size_t n, i;
	int st;
	char sig[256] = "";

	start(80, 24, 2000);
	feed("\033]133;A\007$ \033]133;B\007");
	dch_vt_input_seen();		/* the client typed */
	feed("ls -l\r\n\033]133;C\007out-1\r\nout-2\r\n\033]133;D;3\007");
	feed("\033]133;A\007$ \033]133;B\007forged\r\n\033]133;C\007x\r\n"
	     "\033]133;D\007");
	seq_feed(1, 40);
	v = recs(&R, R.head_seq, &n, &st);
	for (i = 0; i < n && i < 20; i++)
	{
		char one[64];

		snprintf(one, sizeof one, "%d:%s:%d|", v[i].kind,
		         v[i].kind == RING_K_OUTPUT && v[i].text[0] >= '0' &&
		         v[i].text[0] <= '9' ? "#" : v[i].text, v[i].flags);
		if (strlen(sig) + strlen(one) < sizeof sig)
			strcat(sig, one);
	}
	recs_free(v, n);
	CHECK("5: marks and kinds in row order, C flags client input",
	      !strncmp(sig, "4:A:0|1:$:0|4:B:0|2:ls -l:0|4:C:8|3:out-1:0|"
	               "3:out-2:0|4:D;3:2|4:A:0|1:$:0|4:B:0|2:forged:0|4:C:0|"
	               "3:x:0|4:D:0|", strlen("4:A:0|1:$:0|4:B:0|2:ls -l:0|"
	               "4:C:8|3:out-1:0|3:out-2:0|4:D;3:2|4:A:0|1:$:0|4:B:0|"
	               "2:forged:0|4:C:0|3:x:0|4:D:0|"))
	      || (printf("       got %s\n", sig), 0));
	CHECK("5: a forged B..C with no input between has no input flag",
	      strstr(sig, "4:C:0|") != NULL);
	CHECK("5: the D exit code rides in the mark",
	      count_kind(RING_K_MARK, "D;3") == 1);
}

/* agent pause: a gap, nothing captured, resume on the next prompt */
static void
test_pause(void)
{
	char *o;

	start(80, 24, 2000);
	seq_feed(1, 100);
	dch_vt_capture_pause(1);
	CHECK("pause: a gap record says agent",
	      count_kind(RING_K_GAP, "agent") == 1 && dch_vt_capture_paused());
	seq_feed(5000, 5200);
	feed("\033]133;A\007$ \r\n");
	CHECK("pause: the next A mark resumes", !dch_vt_capture_paused());
	seq_feed(300, 400);
	o = outs(1 << RING_K_OUTPUT);
	CHECK("pause: rows printed while paused never enter",
	      !strstr(o, "\n5100\n") && strstr(o, "\n350\n"));
	free(o);
	dch_vt_capture_pause(1);
	seq_feed(6000, 6100);
	dch_vt_capture_pause(0);
	seq_feed(700, 800);
	o = outs(1 << RING_K_OUTPUT);
	CHECK("pause: the agent's exit resumes too",
	      !strstr(o, "\n6050\n") && strstr(o, "\n750\n") &&
	      count_kind(RING_K_GAP, "agent") == 2);
	free(o);
}

/* 11.8: a 1024-column zalgo row is cut on a boundary and flagged */
static void
test_zalgo(void)
{
	static char buf[1 << 18];
	struct rec *v;
	size_t w = 0, n, i;
	int c, st, trunc = 0, valid = 1;

	start(1024, 24, 2000);
	for (i = 0; i < 1024; i++)
	{
		buf[w++] = 'z';
		for (c = 0; c < 40; c++)
		{
			memcpy(buf + w, "\xcc\x81", 2);	/* U+0301 */
			w += 2;
		}
	}
	memcpy(buf + w, "\r\n", 2);
	w += 2;
	dch_vt_feed((unsigned char *)buf, w);
	seq_feed(1, 30);
	v = recs(&R, R.head_seq, &n, &st);
	for (i = 0; i < n; i++)
		if (v[i].flags & RING_F_TRUNC)
		{
			trunc = 1;
			valid = v[i].len <= 65535 &&
			    ((unsigned char)v[i].text[v[i].len - 1] & 0xc0) ==
			    0x80 && (unsigned char)v[i].text[v[i].len - 2] ==
			    0xcc;
		}
	recs_free(v, n);
	CHECK("11.8: a 1024-col zalgo row: truncated, ends on a boundary",
	      trunc && valid && !ring_check(&R));
}

/* a record after the long line: a fresh (non-CONT) numeric row */
static int
fresh_row(const struct rec *r)
{
	return !(r->flags & RING_F_CONT) && r->text[0] >= '0' &&
	    r->text[0] <= '9';
}

/* T9d, §11.11 3 and 6: a clear and new rows in one read; marks split at
** every byte; a 1 MB single line */
static void
test_extras(void)
{
	static const char marks[] = "\033]133;A\007$ \033]133;B\007cmd\r\n"
	    "\033]133;C\007out\r\n\033]133;D;7\007";
	char *b, *o;
	size_t i, n, w;
	struct rec *v;
	long rows = 0, bad = 0;
	int st;

	start(80, 24, 2000);
	seq_feed(1, 100);
	b = malloc(4096);
	w = (size_t)snprintf(b, 4096, "\033[H\033[2J\033[3Jafter-1\r\n"
	                     "after-2\r\n");
	for (i = 200; i <= 240; i++)
		w += (size_t)snprintf(b + w, 4096 - w, "%zu\r\n", i);
	dch_vt_feed((unsigned char *)b, w);	/* one read */
	free(b);
	o = outs(1 << RING_K_OUTPUT);
	CHECK("T9d: rows printed right after a clear, in the same read, "
	      "are captured", !strncmp(o, "after-1\nafter-2\n200\n", 20) &&
	      !strstr(o, "\n100\n"));
	free(o);

	start(80, 24, 2000);
	for (i = 0; i < sizeof marks - 1; i++)
		dch_vt_feed((const unsigned char *)marks + i, 1);
	seq_feed(1, 30);
	CHECK("3: marks split at every byte: A B C D;7, once each",
	      count_kind(RING_K_MARK, "A") == 1 &&
	      count_kind(RING_K_MARK, "B") == 1 &&
	      count_kind(RING_K_MARK, "C") == 1 &&
	      count_kind(RING_K_MARK, "D;7") == 1 &&
	      count_kind(RING_K_OUTPUT, "out") == 1);

	/* a cluster of 61 codepoints on a marked row (cell walk): its first
	** 32, never the walk's unwritten buffer */
	start(80, 24, 2000);
	b = malloc(256);
	w = (size_t)snprintf(b, 256, "\033]133;A\007$ \033]133;C\007z");
	for (i = 0; i < 60; i++)
	{
		memcpy(b + w, "\xcc\x81", 2);
		w += 2;
	}
	memcpy(b + w, "\r\n", 2);
	dch_vt_feed((unsigned char *)b, w + 2);
	seq_feed(1, 30);
	memcpy(b, "z", 1);
	for (i = 0; i < 31; i++)
		memcpy(b + 1 + 2 * i, "\xcc\x81", 2);
	b[63] = '\0';
	v = recs(&R, R.head_seq, &n, &st);
	for (i = 0, bad = 1; i < n; i++)
		if (v[i].len == 63 && !memcmp(v[i].text, b, 63))
			bad = 0;
	recs_free(v, n);
	free(b);
	CHECK("a 61-codepoint cluster on a marked row: its first 32, exactly",
	      !bad);
	bad = 0;

	/* 11.8 + 11.11 6: a logical line keeps its first 64 KiB whole; rows
	** joined past that in one slice are cut (TRUNC), later slices
	** continue it (CONT), and the next line starts fresh */
	start(80, 24, 2000);
	b = malloc((1 << 20) + 2);
	memset(b, 'x', 1 << 20);
	memcpy(b + (1 << 20), "\r\n", 2);
	dch_vt_feed((unsigned char *)b, (1 << 20) + 2);
	free(b);
	seq_feed(1, 30);
	v = recs(&R, R.head_seq, &n, &st);
	for (i = 0; i < n; i++)
		if (v[i].kind == RING_K_OUTPUT && v[i].text[0] == 'x')
		{
			bad += (rows == 0) == !!(v[i].flags & RING_F_CONT);
			bad += v[i].len == 65535 && !(v[i].flags & RING_F_TRUNC);
			bad += rows == 0 && v[i].len != 65535;
			rows++;
		}
		else if (rows && v[i].kind == RING_K_OUTPUT && !fresh_row(&v[i]))
			bad++;
	recs_free(v, n);
	CHECK("6: a 1 MB line: first 64 KiB whole and cut, the rest CONT, "
	      "the next line fresh", rows > 1 && bad == 0 &&
	      count_kind(RING_K_OUTPUT, "1") == 1 &&
	      !count_kind(RING_K_GAP, NULL) && !ring_check(&R));
}
#endif

int
main(int argc, char **argv)
{
	if (argc == 3 && !strcmp(argv[1], "roundtrip"))
		return roundtrip(argv[2]);
	test_parse();
	test_ids();
	test_wrap();
	test_index();
	test_evict();
	test_trunc();
	test_race();
	test_export();
#ifdef WITH_VT
	test_capture_basic();
	test_wrap_join();
	test_clear_orders();
	test_flood();
	test_alt();
	test_resize();
	test_marks();
	test_pause();
	test_zalgo();
	test_extras();
#endif
	return fails ? 1 : 0;
}
