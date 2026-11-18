/* libFuzzer target: the X-ray excerpt cuts (#492, #488). ref_sentence
** (xray.c) cuts a mention's sentence around a ref's span, xv_csent
** (xview.c) cuts that sentence again for a narrow card row. Both are
** static, so xray.c, picker.c and xview.c are included (-Dmain=dch_main);
** the frame buffer pk_fb is then readable. Every byte of the line is
** hostile: combining marks, ZWJ, invalid bytes.
**
** Input: byte 0 the ref's length minus 1 (1-256 bytes), byte 1 the card's
** left column (0-7), byte 2 which offset the card rows are drawn for, the
** rest the line (at most 1200 bytes).
** ref_sentence runs with the span starting at EVERY offset of the line:
** its output is at most XR_SENT bytes of valid UTF-8, and the span's
** place in it holds the ref's bytes when the ref is valid text without
** controls (else the same count of bytes). The scalar after the leading
** ellipsis is not a continuation (xr_u8_cont) unless the line has an
** ellipsis of its own (the classes: marks, variation selectors, keycap,
** skin tones; and not the second RI of a pair), nor when the ref itself starts on a mark. For the
** selected span xv_csent draws the sentence at EVERY width 8-130: valid UTF-8, within the width, the ref whole when
** xv_fitlen says it fits, no continuation scalar after its ellipsis. */
#include "../../xray.c"
#include "../../picker.c"
#include "../../xview.c"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

/* the line says which invariant */
#define FAIL() (fprintf(stderr, "fuzz_xcut: invariant at line %d\n", __LINE__), abort())

static int
valid(const unsigned char *u, size_t n)
{
	for (size_t i = 0, a; i < n; i += a)
	{
		a = dch_utf8_len(u + i, n - i);
		if (!a)
			return 0;
	}
	return 1;
}

/* a regional indicator U+1F1E6-1F1FF at p (4 readable bytes) */
static int
isri(const char *p)
{
	return !memcmp(p, "\xf0\x9f\x87", 3) && (unsigned char)p[3] >= 0xa6 &&
	       (unsigned char)p[3] <= 0xbf;
}

/* valid UTF-8 with no control the cut would blank */
static int
clean(const unsigned char *u, size_t n)
{
	if (!valid(u, n))
		return 0;
	for (size_t i = 0; i < n; i++)
		if (u[i] < ' ' || u[i] == 0x7f ||
		    (u[i] == 0xc2 && i + 1 < n && u[i + 1] < 0xa0))
			return 0;
	return 1;
}

static void
draw(const char *o, size_t ss, size_t se, size_t k0, int own)
{
	struct xv_men m = { 0 };
	const char *ref = o + ss;
	size_t rl = se - ss;

	m.sent = (char *)o;
	m.ss = (int)ss;
	m.se = (int)se;
	pk_utf = 1;
	pk_color = 0;
	for (int w = 8; w <= 130; w++)
	{
		int room;
		size_t i = 0;

		pk_w = w;
		pk_col = (int)k0;
		pk_fn = 0;
		room = pk_w - pk_col - 2;
		xv_csent(&m);
		if (pk_col > pk_w || !valid((unsigned char *)pk_fb, pk_fn))
			FAIL();
		if (!own && !xr_u8_cont(ref, rl) && pk_fn >= 3 && !memcmp(pk_fb, "\xe2\x80\xa6", 3) &&
		    memcmp(o, "\xe2\x80\xa6", 3) &&
		    xr_u8_cont(pk_fb + 3, pk_fn - 3))
			FAIL();
		if (room >= 1 && clean((const unsigned char *)ref, rl) &&
		    xv_fitlen(ref, rl, room) == rl)
		{
			while (i + rl <= pk_fn && memcmp(pk_fb + i, ref, rl))
				i++;
			if (i + rl > pk_fn)
				FAIL();
		}
	}
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	char *l, *o = malloc(XR_SENT + 1);
	size_t rl, sel, ll;
	int own;

	if (n < 4 || !o)
		return free(o), 0;
	rl = (size_t)d[0] + 1;
	ll = n - 3 > 1200 ? 1200 : n - 3;
	l = malloc(ll);
	memcpy(l, d + 3, ll);
	sel = d[2] % ll;
	own = memmem(l, ll, "\xe2\x80\xa6", 3) != NULL;
	for (size_t s = 0; s < ll; s++)
	{
		size_t e = s + rl > ll ? ll : s + rl, ss, se, ol;

		size_t tn = e - s;

		/* a ref is a match of text: no blank (White_Space) ends */
		if (trim(l + s, &tn) != l + s || tn != e - s ||
		    (unsigned char)l[s] <= ' ' || (unsigned char)l[e - 1] <= ' ' ||
		    l[s] == 0x7f || l[e - 1] == 0x7f)
			continue;
		/* and starts and ends on a scalar, not inside one */
		if (((unsigned char)l[s] & 0xc0) == 0x80 ||
		    (e < ll && ((unsigned char)l[e] & 0xc0) == 0x80))
			continue;
		ref_sentence(l, ll, s, e, o, &ss, &se);
		ol = strlen(o);
		if (ol > XR_SENT || !valid((unsigned char *)o, ol))
			FAIL();
		if (ss == SIZE_MAX)
		{
			if (se != SIZE_MAX)
				FAIL();
			continue;
		}
		if (ss > se || se > ol || se - ss != e - s)
			FAIL();
		if (clean((unsigned char *)l + s, e - s) &&
		    memcmp(o + ss, l + s, e - s))
			FAIL();
		if (!own && ss > 4 && !memcmp(o, "\xe2\x80\xa6 ", 4) &&
		    xr_u8_cont(o + 4, ss - 4))
			FAIL();
		/* the excerpt body o[4..ss) is l[lo..s) when untouched: it does
		** not start on the second RI of a pair (#495) */
		if (!own && ss > 8 && !memcmp(o, "\xe2\x80\xa6 ", 4) && s >= ss - 4 &&
		    !memcmp(l + s - (ss - 4), o + 4, ss - 4) && isri(o + 4))
		{
			size_t lo = s - (ss - 4);
			int odd = 0;

			for (; lo >= 4 && isri(l + lo - 4); lo -= 4)
				odd ^= 1;
			if (odd)
				FAIL();
		}
		if (s == sel)
			draw(o, ss, se, d[1] & 7, own);
	}
	free(l);
	free(o);
	return 0;
}
