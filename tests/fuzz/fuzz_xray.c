/* libFuzzer target: the X-ray chapter decoders (#120, XR1b). They run only
** in the querying process (K3), over a transcript or a ring the session
** controls, so every byte here is hostile.
**
** Input: byte 0 picks the decoder, the rest is its input.
**   even: a Claude JSONL transcript, cut at '\n' as xr_claude_read cuts it
**         (a line past XR_LINE_MAX goes in as oversized), then the
**         chapters printed through xr_print;
**   odd:  ring records as MSG_RING_READ carries them ({u16 len, u8 kind,
**         u8 flags, u32 time, text}), through xr_shell_chapters, then
**         printed; byte 0's high bits pick an output cap squeeze.
** Every chapter's title must be valid UTF-8 of at most XR_TITLE_MAX
** scalars, and the printed object one line. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
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
	    bl > XR_OUT_MAX)
		abort();
	free(buf);
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct xr_chapters c;

	if (!n)
		return 0;
	if (!(d[0] & 1))
	{
		struct xr_claude *x = xr_claude_new();
		size_t i = 1, s = 1;

		if (!x)
			return 0;
		for (; i <= n; i++)
			if (i == n || d[i] == '\n')
			{
				int big = i - s > XR_LINE_MAX;
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
