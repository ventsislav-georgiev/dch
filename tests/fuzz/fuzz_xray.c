/* libFuzzer target: the X-ray chapter decoders (#120, XR1b). They run only
** in the querying process (K3), over a transcript or a ring the session
** controls, so every byte here is hostile.
**
** Input: byte 0's low 2 bits pick the decoder, the rest is its input.
**   0: a Claude JSONL transcript, cut at '\n' here (a line past xr_lim.line
**      goes in as oversized);
**   1: ring records ({u16 len, u8 kind, u8 flags, u32 time, text}), a short
**      tail taken as what is left, through xr_shell_chapters;
**   2: a Claude transcript as a file, through xr_claude_read (read_range:
**      the chunking, the line cap, the head and tail ranges);
**   3: one MSG_RING_READ reply: u32 count LE, then its record bytes, through
**      xr_ring_walk (dch.c's reply walker) into xr_shell_feed.
** Byte 0's high bits squeeze xr_lim (fuzz only): the output cap to 8-64K,
** the read chunk to 16 B or 4093 B, the line cap to 64 B, the ranges to a
** 128 B head and a 256 B tail, so a small input reaches every cap.
** Then the chapters are printed through xr_print. Every chapter's title must
** be valid UTF-8 of at most XR_TITLE_MAX scalars, and the printed object one
** line, within the output cap. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "../../xray.h"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static void
check_title(const char *t)
{
	const unsigned char *u = (const unsigned char *)t;
	size_t i = 0, sc = 0;

	while (u[i])
	{
		size_t a = u[i] < 0x80 ? 1 : (u[i] & 0xe0) == 0xc0 ? 2
		           : (u[i] & 0xf0) == 0xe0 ? 3 : (u[i] & 0xf8) == 0xf0
		           ? 4 : 0;
		size_t k;

		if (!a)
			abort();
		for (k = 1; k < a; k++)
			if ((u[i + k] & 0xc0) != 0x80)
				abort();
		i += a;
		sc++;
	}
	if (sc > XR_TITLE_MAX)
		abort();
}

static void
emit(struct xr_chapters *c, int shell)
{
	struct xr_answer a;
	char *buf = NULL;
	size_t bl = 0, i;
	FILE *f;

	for (i = 0; i < c->n; i++)
		check_title(xr_chapter_at(c, i)->title);
	memset(&a, 0, sizeof a);
	a.session = "fuzz";
	a.session_id = "m0123456789abcdef";
	a.corpus = shell ? "shell" : "claude";
	a.project = a.project_label = "";
	a.available = a.complete = 1;
	a.lines = shell ? 1 : -1;
	a.ch = c;
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

struct walk
{
	struct xr_shell sh;
	uint32_t got;
	uint64_t next;
};

static void
walk_rec(void *ctx, const struct xr_rec *x)
{
	struct walk *w = ctx;

	if (x->seq != w->next++)
		abort();	/* seqs run from first, one per record */
	w->got++;
	xr_shell_feed(&w->sh, x);
}

/* The transcript file mode 2 reads: one per process, rewritten per input. */
static int
tfile(void)
{
	static int fd = -1;
	char path[] = "/tmp/fuzz_xray.XXXXXX";

	if (fd < 0 && (fd = mkstemp(path)) >= 0)
		unlink(path);
	return fd;
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	static const size_t outs[] = {XR_OUT_MAX, 8192, 16384, 65536};
	struct xr_chapters c = {0};
	unsigned sq;

	if (!n)
		return 0;
	sq = d[0] >> 2;
	xr_lim.out = outs[sq & 3];
	xr_lim.head = sq & 4 ? 128 : XR_HEAD_RANGE;
	xr_lim.tail = sq & 4 ? 256 : XR_TAIL_RANGE;
	xr_lim.chunk = sq & 8 ? 16 : sq & 16 ? 4093 : XR_CHUNK;
	xr_lim.line = sq & 32 ? 64 : XR_LINE_MAX;
	if ((d[0] & 3) == 2)
	{
		struct xr_claude *x = xr_claude_new();
		int fd = tfile(), capped;

		if (!x)
			return 0;
		if (fd >= 0 && ftruncate(fd, 0) == 0 &&
		    pwrite(fd, d + 1, n - 1, 0) == (ssize_t)(n - 1) &&
		    xr_claude_read(x, fd, n - 1, &capped) == 0 &&
		    capped != (n - 1 > xr_lim.head + xr_lim.tail))
			abort();	/* capped is exactly "past the ranges" */
		xr_claude_end(x, 0x1234, &c);
		xr_claude_free(x);
	}
	else if ((d[0] & 3) == 3)
	{
		struct walk w;
		uint32_t count;
		unsigned char *b;
		long long r;

		if (n < 5)
			return 0;
		count = (uint32_t)d[1] | (uint32_t)d[2] << 8 |
		        (uint32_t)d[3] << 16 | (uint32_t)d[4] << 24;
		/* an exact block: ASan must see a read past the reply */
		if (!(b = malloc(n - 5 ? n - 5 : 1)))
			return 0;
		memcpy(b, d + 5, n - 5);
		memset(&w, 0, sizeof w);
		w.next = 1000;
		xr_shell_begin(&w.sh, &c);
		r = xr_ring_walk(b, n - 5, 1000, count, walk_rec, &w);
		if (r >= 0 && ((size_t)r != n - 5 || w.got != count))
			abort();	/* whole means every byte, every record */
		if (w.got > count)
			abort();
		free(b);
	}
	else if (!(d[0] & 1))
	{
		struct xr_claude *x = xr_claude_new();
		size_t i = 1, s = 1;

		if (!x)
			return 0;
		for (; i <= n; i++)
			if (i == n || d[i] == '\n')
			{
				int big = i - s > xr_lim.line;
				char *l = big ? NULL : malloc(i - s + 1);

				if (i > s && (big || l))
				{
					if (l)
					{
						memcpy(l, d + s, i - s);
						l[i - s] = '\0';
					}
					xr_claude_line(x, l ? l : "", big ? 0 : i - s,
					               s, big);
				}
				free(l);
				s = i + 1;
			}
		xr_claude_end(x, 0x1234, &c);
		xr_claude_free(x);
	}
	else
	{
		struct xr_rec *r = NULL;
		size_t off = 1, k = 0;
		int paired, alt;

		while (off + 8 <= n)
		{
			size_t l = (size_t)d[off] | (size_t)d[off + 1] << 8;
			struct xr_rec *nr;
			char *t;

			if (off + 8 + l > n)
				l = n - off - 8;	/* a short tail: what is left */
			if (!(nr = realloc(r, (k + 1) * sizeof *r)))
				break;
			r = nr;
			r[k].seq = 1000 + k;
			r[k].kind = d[off + 2] % 7;
			r[k].flags = d[off + 3];
			r[k].t = (uint32_t)d[off + 4] | (uint32_t)d[off + 5] << 8;
			/* each text in its own exact block: record text is not
			** NUL-terminated, and ASan must see a read past it */
			if (!(t = malloc(l ? l : 1)))
				break;
			memcpy(t, d + off + 8, l);
			r[k].text = t;
			r[k].len = l;
			k++;
			off += 8 + l;
		}
		xr_shell_chapters(r, k, &c, &paired, &alt);
		if (paired && !c.n && !c.total)
			abort();	/* paired means a chapter was made */
		while (k)
			free((void *)r[--k].text);
		free(r);
	}
	emit(&c, d[0] & 1);
	xr_chapters_free(&c);
	return 0;
}
