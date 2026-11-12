/* libFuzzer target: `--chapter <anchor>` (#156, XR3b; spec 3.5): the anchor
** rule and a chapter's entries, read from a transcript or a ring the
** session controls, so every byte here is hostile.
**
** Input: byte 0's bit 0 picks the corpus, bit 1 a small object cap (4 KiB
** of entries) so a small input reaches the cut, bit 2 a JSONL corpus read
** as a Codex rollout (#169); byte 1 picks the chapter
** (bit 7: an anchor no chapter has); bytes 2..33 are a candidate anchor
** (up to its first NUL); the rest is the corpus:
**   0: a Claude JSONL transcript, cut at '\n' here;
**   1: ring records ({u16 len, u8 kind, u8 flags, u32 time, text}), a short
**      tail taken as what is left, through xr_shell_feed.
** xr_anchor_ok must agree with the rule written out here. The corpus is
** decoded once for its chapters, then again with the picked anchor wanted,
** as dch.c does: the chapters must be the same both times; an anchor of the
** index is hit, one of no chapter keeps nothing; the entries stay within
** their cap, and the printed object is one line of valid JSON within it. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
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
	xr_entries_free(&e);
	xr_chapters_free(&c1);
	xr_chapters_free(&c2);
	return 0;
}
