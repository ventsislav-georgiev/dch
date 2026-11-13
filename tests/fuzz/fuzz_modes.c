/* libFuzzer target: the pty-output mode trackers across read boundaries.
** track_modes fed a stream in two or three reads must leave kbd_u, kbd_m and
** dec_on[] exactly as track_keymode + track_decmode leave them over the
** unsplit stream, unless a sequence crossing a cut is longer than the carry
** (dropped by design). Once, every tracked sequence is checked at every one
** and every pair of split points. Linked like fuzz_frames: includes
** master.c, dch.c built with -Dmain=dch_main. */
#include "../../master.c"
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

struct modes
{
	unsigned char u[sizeof kbd_u], m[sizeof kbd_m], dec[N_DEC_MODES];
	size_t ul, ml;
};

static void
modes_clear(void)
{
	kbd_u_len = kbd_m_len = mode_carry_len = 0;
	memset(dec_on, 0, sizeof dec_on);
}

static void
modes_get(struct modes *s)
{
	memset(s, 0, sizeof *s);
	memcpy(s->u, kbd_u, kbd_u_len);
	memcpy(s->m, kbd_m, kbd_m_len);
	memcpy(s->dec, dec_on, sizeof dec_on);
	s->ul = kbd_u_len;
	s->ml = kbd_m_len;
}

/* The unsplit reference: today's single-read scan. */
static void
modes_whole(const uint8_t *d, size_t n, struct modes *s)
{
	modes_clear();
	track_keymode(d, n);
	track_decmode(d, n);
	modes_get(s);
}

/* d in three reads, cut at a <= b. */
static void
modes_split(const uint8_t *d, size_t n, size_t a, size_t b, struct modes *s)
{
	modes_clear();
	track_modes(d, a);
	track_modes(d + a, b - a);
	track_modes(d + b, n - b);
	modes_get(s);
}

/* A cut at c inside a candidate sequence longer than the carry. */
static int
too_long(const uint8_t *d, size_t n, size_t c)
{
	size_t p = c, q = c;

	while (p > 0 && mode_cont(d[p - 1]))
		p--;
	if (p == 0 || d[p - 1] != '\033')
		return 0;
	while (q < n && mode_cont(d[q]))
		q++;
	return (q < n ? q + 1 : q) - (p - 1) > sizeof mode_carry;
}

static void
modes_check(const uint8_t *d, size_t n, size_t a, size_t b,
    const struct modes *want)
{
	struct modes got;

	if (too_long(d, n, a) || too_long(d, n, b))
		return;
	modes_split(d, n, a, b, &got);
	if (memcmp(&got, want, sizeof got) != 0)
		abort();
}

/* Every tracked sequence, set and reset, in context, at every split. */
static void
every_split(void)
{
	static const char *const seq[] = {
		"\033[?1049h", "\033[?1000;1006h", "\033[?2004h", "\033[>1u",
		"\033[<u", "\033[>4;2m", "\033[?1000l", "\033[?1002;1003;1015h",
		"\033[>5:1u", "\033[?1006;1000l",
	};
	static const char pre[] = "\033[>3u\033[?1000hab\033";
	unsigned char buf[128];
	struct modes want, set;
	size_t k, n, a, b;

	for (k = 0; k < sizeof seq / sizeof seq[0]; k++)
	{
		n = (size_t)snprintf((char *)buf, sizeof buf, "%sx%s\033[",
		    pre, seq[k]);
		modes_whole(buf, n, &want);
		/* the sequence really changed something */
		modes_whole(buf, sizeof pre, &set);
		if (memcmp(&set, &want, sizeof set) == 0)
			abort();
		for (a = 0; a <= n; a++)
			for (b = a; b <= n; b++)
				modes_check(buf, n, a, b, &want);
	}
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	static int once;
	struct modes want;
	size_t a, b, c;

	if (!once)
	{
		every_split();
		once = 1;
	}
	if (n < 4)
		return 0;
	a = ((size_t)d[0] | (size_t)d[1] << 8) % (n - 1);
	b = ((size_t)d[2] | (size_t)d[3] << 8) % (n - 1);
	d += 4;
	n -= 4;
	if (a > n)
		a = n;
	if (b > n)
		b = n;
	modes_whole(d, n, &want);
	modes_check(d, n, a < b ? a : b, a < b ? b : a, &want);
	for (c = 0; n <= 512 && c <= n; c++)
		modes_check(d, n, c, c, &want);
	return 0;
}
