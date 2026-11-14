/* libFuzzer target: `--chapter <anchor>` (#156, XR3b; spec 3.5): the anchor
** rule and a chapter's entries, read from a transcript or a ring the
** session controls, so every byte here is hostile.
**
** Input: byte 0's bit 0 picks the corpus, bit 1 a small object cap (4 KiB
** of entries) so a small input reaches the cut, bit 2 a JSONL corpus read
** as a Codex rollout (#169), bits 3-4 the seek's window (#300: 16, 64, 509
** or 4096 bytes) and bit 5 a range of a quarter head and a third tail;
** byte 1 picks the chapter
** (bit 7: an anchor no chapter has); bytes 2..33 are a candidate anchor
** (up to its first NUL); the rest is the corpus:
**   0: a Claude JSONL transcript, cut at '\n' here;
**   1: ring records ({u16 len, u8 kind, u8 flags, u32 time, text}), a short
**      tail taken as what is left, through xr_shell_feed.
** xr_anchor_ok must agree with the rule written out here. The corpus is
** decoded once for its chapters, then again with the picked anchor wanted,
** as dch.c does: the chapters must be the same both times; an anchor of the
** index is hit, one of no chapter keeps nothing; the entries stay within
** their cap, and the printed object is one line of valid JSON within it.
** #300: a JSONL corpus is also written to a file and asked by the seek
** (xr_chapter_seek, the whole read when it is not sure, as dch.c), for the
** picked chapter's anchor on the file's inode and for an offset from
** bytes 2-3: the answer must be the whole read's, byte for byte. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "../../xray.h"
#include "../../bridge.h"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static int
anchor_ref(const char *s)
{
	size_t i = 0;

	for (; s[i]; i++)
		if (!((s[i] >= 'A' && s[i] <= 'Z') || (s[i] >= 'a' && s[i] <= 'z') ||
		      (s[i] >= '0' && s[i] <= '9') || s[i] == ':' || s[i] == '_' ||
		      s[i] == '.' || s[i] == '-'))
			return 0;
	return i >= 1 && i <= 128;
}

static void
decode(const uint8_t *d, size_t n, int shell, int codex,
       struct xr_entries *e, struct xr_chapters *c)
{
	size_t i, s;

	if (!shell)
	{
		struct xr_claude *x = xr_claude_new();

		if (!x)
			return;
		if (codex)
			xr_claude_codex(x);
		if (e)
			xr_claude_entries(x, e, 0x1234);
		for (i = s = 0; i <= n; i++)
			if (i == n || d[i] == '\n')
			{
				char *l = i > s ? malloc(i - s + 1) : NULL;

				if (l)
				{
					memcpy(l, d + s, i - s);
					l[i - s] = '\0';
					xr_claude_line(x, l, i - s, s, 0);
				}
				free(l);
				s = i + 1;
			}
		xr_claude_end(x, 0x1234, c);
		xr_claude_free(x);
		return;
	}
	{
		struct xr_shell sh;
		struct xr_rec r;
		size_t off = 0, k = 0;

		xr_shell_begin(&sh, c);
		sh.want = e;
		while (off + 8 <= n)
		{
			size_t l = (size_t)d[off] | (size_t)d[off + 1] << 8;
			char *t;

			if (off + 8 + l > n)
				l = n - off - 8;	/* a short tail: what is left */
			/* its own exact block: record text is not NUL-terminated */
			if (!(t = malloc(l ? l : 1)))
				break;
			memcpy(t, d + off + 8, l);
			r.seq = 1000 + k++;
			r.kind = d[off + 2] % 7;
			r.flags = d[off + 3];
			r.t = (uint32_t)d[off + 4] | (uint32_t)d[off + 5] << 8;
			r.text = t;
			r.len = l;
			xr_shell_feed(&sh, &r);
			free(t);
			off += 8 + l;
		}
		xr_shell_end(&sh);
	}
}

/* --chapter on fd as dch.c answers it (seek: the seek first), printed into
** a malloc'd string: the object, else "gone" or "failed". */
static char *
answer(int fd, const struct stat *st, int codex, const char *anchor, int seek)
{
	struct xr_chapters ch = {0};
	struct xr_entries e;
	struct xr_claude *x;
	char *o = NULL;
	size_t ol = 0, k;
	int rc = 0, capped, any = 0;
	FILE *m;

	xr_entries_begin(&e, anchor, "fuzz", XR_CH_OUT);
	if (seek)
		rc = xr_chapter_seek(fd, st, codex, &e, &ch);
	if (rc == 0 && (x = xr_claude_new()))
	{
		if (codex)
			xr_claude_codex(x);
		xr_claude_entries(x, &e, (uint64_t)st->st_ino);
		rc = xr_claude_read(x, fd, (uint64_t)st->st_size, &capped);
		xr_claude_end(x, (uint64_t)st->st_ino, &ch);
		xr_claude_free(x);
	}
	if ((m = open_memstream(&o, &ol)))
	{
		for (k = 0; rc >= 0 && !e.failed && !any && k < ch.n; k++)
			if (!strcmp(xr_chapter_at(&ch, k)->anchor, anchor) && e.hit &&
			    e.ord == ch.total - ch.n + k)
				any = xr_chapter_print(m, "fuzz", "s",
				                       xr_chapter_at(&ch, k), &e) == 0;
		if (!any)
			fputs(rc < 0 || e.failed ? "failed" : "gone", m);
		fclose(m);
	}
	xr_chapters_free(&ch);
	xr_entries_free(&e);
	return o;
}

/* #300: the seek against the whole read, over the corpus as a file. */
static void
seek_same(const uint8_t *d, size_t n, int codex, const char *picked)
{
	static const size_t win[4] = { 16, 64, 509, 4096 };
	static char path[] = "/tmp/fuzz_xchapter.XXXXXX";
	static int fd = -1;
	struct xr_limits l0 = xr_lim;
	struct stat st;
	char a[2][64];
	const char *off;
	int k;

	if (fd < 0 && (fd = mkstemp(path)) >= 0)
		unlink(path);
	if (fd < 0 || ftruncate(fd, 0) != 0 ||
	    pwrite(fd, d + 34, n - 34, 0) != (ssize_t)(n - 34) ||
	    fstat(fd, &st) != 0)
		return;
	xr_lim.chunk = win[d[0] >> 3 & 3];
	if (d[0] & 32)
	{
		xr_lim.head = (n - 34) / 4;
		xr_lim.tail = (n - 34) / 3;
	}
	/* the picked chapter's offset (decode() anchors on inode 0x1234) */
	off = picked[0] == 'f' ? strchr(picked, ':') : NULL;
	snprintf(a[0], sizeof a[0], "f%llx:%s", (unsigned long long)st.st_ino,
	         off ? off + 1 : "0");
	snprintf(a[1], sizeof a[1], "f%llx:%zu", (unsigned long long)st.st_ino,
	         ((size_t)d[2] | (size_t)d[3] << 8) % (n - 34 + 1));
	for (k = 0; k < 2; k++)
	{
		char *x = answer(fd, &st, codex, a[k], 1);
		char *y = answer(fd, &st, codex, a[k], 0);

		if (!x || !y || strcmp(x, y))
			abort();	/* the seek answers as the whole read */
		free(x);
		free(y);
	}
	xr_lim = l0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct xr_chapters c1 = {0}, c2 = {0};
	struct xr_entries e;
	struct json_parser jp;
	char cand[33], want[160];
	char *buf = NULL;
	size_t bl = 0, out, i, k = 0;
	int shell, in_index = 0;
	FILE *f;

	if (n < 34)
		return 0;
	memcpy(cand, d + 2, 32);
	cand[32] = '\0';
	if (xr_anchor_ok(cand) != anchor_ref(cand))
		abort();
	shell = d[0] & 1;
	out = d[0] & 2 ? 6 * (4 + 64) + 2048 + 4096 : XR_CH_OUT;
	decode(d + 34, n - 34, shell, d[0] & 4, NULL, &c1);
	if (c1.n && !(d[1] & 0x80))
	{
		k = d[1] % c1.n;
		snprintf(want, sizeof want, "%s", xr_chapter_at(&c1, k)->anchor);
	}
	else	/* chapters' anchors start with f or s, never x */
		snprintf(want, sizeof want, "x%s", c1.n ?
		         xr_chapter_at(&c1, 0)->anchor : cand);
	xr_entries_begin(&e, want, "fuzz", out);
	decode(d + 34, n - 34, shell, d[0] & 4, &e, &c2);
	if (c2.n != c1.n || c2.total != c1.total)
		abort();	/* wanting a chapter never changes the index */
	for (i = 0; i < c1.n; i++)
		if (strcmp(xr_chapter_at(&c1, i)->anchor,
		           xr_chapter_at(&c2, i)->anchor))
			abort();
	if (want[0] == 'x' && (e.hit || e.len))
		abort();	/* no chapter, no entries */
	if (want[0] != 'x' && !e.hit)
		abort();	/* the index's anchor was met on the way */
	if (e.len > e.cap)
		abort();
	for (i = 0; i < c2.n; i++)
		if (!strcmp(xr_chapter_at(&c2, i)->anchor, want) && e.hit &&
		    e.ord == c2.total - c2.n + i)
		{
			in_index = 1;
			k = i;
		}
	if (in_index && !e.failed && (f = open_memstream(&buf, &bl)))
	{
		if (xr_chapter_print(f, "fuzz", "m0123456789abcdef",
		                     xr_chapter_at(&c2, k), &e) != 0)
			abort();
		fclose(f);
		if (!bl || buf[bl - 1] != '\n' || memchr(buf, '\n', bl - 1) ||
		    bl > out)
			abort();
		buf[bl - 1] = '\0';
		if (dch_json_parse(&jp, buf) != 0)
			abort();	/* one valid JSON object */
		dch_json_free(&jp);
		free(buf);
	}
	if (!shell)
		seek_same(d, n, d[0] & 4, want);
	xr_entries_free(&e);
	xr_chapters_free(&c1);
	xr_chapters_free(&c2);
	return 0;
}
