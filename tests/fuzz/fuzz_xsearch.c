/* libFuzzer target: X-ray --search (#297), the matcher and its two readers.
** Every byte is hostile: the corpus is whatever the session wrote.
**
** Input: byte 0 picks the path and the needle's length, byte 1 on is the
** needle (2 + byte 0's bits 3-7 bytes), the rest is the body.
**   bit 0 clear: the body as a transcript, read backward by xr_search_file
**      from a file; bit 1 reads it as a Codex rollout; bit 2 shrinks the
**      window (xr_lim.line 64) and --json's range (head 100, tail 200);
**   bit 0 set: the body as ring records ({u16 len, u8 kind, u8 flags,
**      u32 time, text}) through xr_shell_feed and xr_search_rec; a record
**      of kind 0xFE draws the ring/screen seam (#171), every record after it
**      a screen row.
** xr_find always agrees with a naive folded search over the body. The
** printed object is one line of JSON with at most 50 hits, newest first
** (offsets never rising), each inside the body; `more` only with 50 hits.
** For a needle the CLI takes (xray_search_ok), every hit's snippet holds
** the needle, ASCII letters folded, its space runs as one space and its
** ends trimmed, as the snippet writes them. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "../../ring.h"
#include "../../xray.h"
#include "../../bridge.h"
#include "../../rframe.h"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static int fd = -1;

static int
fold(int c)
{
	return c >= 'A' && c <= 'Z' ? c + 32 : c;
}

static size_t
naive(const char *h, size_t hn, const char *t, size_t tn)
{
	size_t i, k;

	for (i = 0; tn && i + tn <= hn; i++)
	{
		for (k = 0; k < tn && fold((unsigned char)h[i + k]) ==
		     fold((unsigned char)t[k]); k++)
			;
		if (k == tn)
			return i;
	}
	return SIZE_MAX;
}

/* the needle as a snippet writes it: space runs as one, the ends trimmed */
static size_t
norm(const char *t, char *o)
{
	size_t l = 0;

	for (; *t; t++)
		if (*t != ' ' || (l && o[l - 1] != ' '))
			o[l++] = *t;
	while (l && o[l - 1] == ' ')
		l--;
	o[l] = '\0';
	return l;
}

static void
check(const char *out, size_t ol, const char *needle, uint64_t body)
{
	struct json_parser p;
	char want[XR_SEARCH_MAX + 1], snip[4 * XR_SNIPPET + 8 * XR_SEARCH_MAX];
	size_t wl = norm(needle, want);
	int ok = xray_search_ok(needle), h, i, f, hits = 0;
	long long last = -1;

	if (!ol || out[ol - 1] != '\n' || memchr(out, '\n', ol - 1))
		abort();	/* one line */
	memset(&p, 0, sizeof p);
	if (dch_json_parse(&p, out) < 0 || p.tok[0].kind != JSON_OBJECT ||
	    (h = dch_json_field(&p, 0, "hits")) < 0 ||
	    p.tok[h].kind != JSON_ARRAY)
		abort();
	for (i = h + 1; i < p.tok[h].next; i = p.tok[i].next)
	{
		long long off;

		hits++;
		if ((f = dch_json_field(&p, i, "offset")) < 0 ||
		    p.tok[f].kind != JSON_PRIMITIVE)
			abort();
		off = strtoll(out + p.tok[f].start, NULL, 10);
		if (off < 0 || (body && (uint64_t)off >= body) ||
		    (last >= 0 && off > last))
			abort();	/* inside the body, newest first */
		last = off;
		if ((f = dch_json_field(&p, i, "snippet")) < 0 ||
		    p.tok[f].kind != JSON_STRING ||
		    dch_json_decode(&p, f, snip, sizeof snip) < 0)
			abort();
		if (ok && naive(snip, strlen(snip), want, wl) == SIZE_MAX && wl)
			abort();	/* a hit that does not hold the needle */
	}
	if (hits > XR_SEARCH_HITS ||
	    ((f = dch_json_field(&p, 0, "more")) >= 0 &&
	     !strncmp(out + p.tok[f].start, "true", 4) &&
	     hits != XR_SEARCH_HITS))
		abort();
	dch_json_free(&p);
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct xr_limits l0 = xr_lim;
	char needle[XR_SEARCH_MAX + 1], *out = NULL;
	size_t nl, ol = 0, at;
	const char *body;
	struct xr_search *s;
	FILE *f;

	if (n < 3)
		return 0;
	nl = 2 + (d[0] >> 3);
	if (1 + nl > n)
		return 0;
	memcpy(needle, d + 1, nl);
	needle[nl] = '\0';
	body = (const char *)d + 1 + nl;
	n -= 1 + nl;
	/* the matcher against a naive one, the needle as given (a NUL in it
	** cuts it, as argv would) */
	nl = strlen(needle);
	if (nl && xr_find(body, n, needle, nl) != naive(body, n, needle, nl))
		abort();
	if (!(s = xr_search_new(needle, xr_now_ms())))
		return 0;
	if (!(d[0] & 1))
	{
		if (fd < 0)
		{
			char path[] = "/tmp/fuzz_xsearch.XXXXXX";

			if ((fd = mkstemp(path)) < 0)
				abort();
			unlink(path);
		}
		if (ftruncate(fd, 0) != 0 ||
		    (n && pwrite(fd, body, n, 0) != (ssize_t)n))
			abort();
		if (d[0] & 4)
		{
			xr_lim.line = 64;
			xr_lim.head = 100;
			xr_lim.tail = 200;
		}
		if (xr_search_file(s, fd, n, 0x1f, d[0] & 2) != 0)
			abort();	/* only an allocation or a read fails */
		xr_lim = l0;
	}
	else
	{
		struct xr_chapters c;
		struct xr_shell sh;
		uint64_t seq = 1000;
		int screen = 0;

		xr_shell_begin(&sh, &c);
		for (at = 0; at + 8 <= n;)
		{
			size_t l = (size_t)(unsigned char)body[at] |
			           (size_t)(unsigned char)body[at + 1] << 8;
			struct xr_rec r;

			if (at + 8 + l > n)
				l = n - at - 8;
			if ((unsigned char)body[at + 2] == 0xFE && !screen)
			{
				xr_shell_screen(&sh);
				screen = 1;
			}
			r.seq = seq++;
			r.kind = screen ? RING_K_OUTPUT : (unsigned char)body[at + 2] % 7;
			r.flags = screen ? 0 : (unsigned char)body[at + 3];
			r.t = (uint32_t)(unsigned char)body[at + 4] |
			      (uint32_t)(unsigned char)body[at + 5] << 8;
			r.text = body + at + 8;
			r.len = l;
			xr_shell_feed(&sh, &r);
			xr_search_rec(s, &sh, &r);
			at += 8 + l;
		}
		xr_shell_end(&sh);
		xr_chapters_free(&c);
		xr_search_ring_end(s);
		n = 0;		/* offsets are seqs, not bytes */
	}
	if (!(f = open_memstream(&out, &ol)))
		abort();
	if (xr_search_print(f, s, "s", "id", d[0] & 1 ? "shell" : "claude",
	                    NULL) != 0)
		abort();
	fclose(f);
	check(out, ol, needle, n);
	free(out);
	xr_search_free(s);
	return 0;
}
