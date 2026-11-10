/* libFuzzer target: the X-ray shell ring (#119). Arbitrary bytes go through
** the terminal mirror with the capture (and its OSC 133 / RIS callbacks)
** installed, cut into feeds at fuzzer-chosen points, with resizes and the
** master's clear message mixed in; the ring's invariants are checked after
** every feed. Then reads from fuzzer-chosen seqs must return whole records.
**
** Input: byte 0 = geometry/size choice, then ops: 0xF0 next-byte = feed the
** next (byte) bytes as one slice, 0xF1 = resize, 0xF2 = ED 3 forms split at
** a fuzzer-chosen point, 0xF3 = clear message; any other byte is fed
** one-by-one in runs. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "../../ring.h"
#include "../../vt.h"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static struct dch_ring R;
static unsigned char rb[RING_MAX + 64];

static void
check(void)
{
	if (ring_check(&R) != 0)
		abort();
}

static void
reads(uint64_t from)
{
	uint64_t first, cnt, i;
	size_t got, off = 0;
	int st;

	got = ring_read(&R, from, rb, (size_t)(from % 3 ? 70000 : sizeof rb),
	                &first, &cnt, &st);
	for (i = 0; i < cnt; i++)
	{
		size_t l = (size_t)rb[off] | (size_t)rb[off + 1] << 8;

		if (rb[off + 2] < RING_K_PROMPT || rb[off + 2] > RING_K_GAP)
			abort();
		off += RING_HDR + l;
	}
	if (off != got || first < R.head_seq || first + cnt > R.tail_seq)
		abort();
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	static const char *ed3[] = { "\033[3J", "\033[03J", "\033[?3J",
	                             "\033[3\bJ", "\033[H\033[2J\033[3J",
	                             "\033c" };
	static const int geo[][3] = { { 80, 24, 2000 }, { 80, 24, 0 },
	                              { 20, 5, 0 }, { 200, 50, 100 },
	                              { 1024, 30, 0 }, { 3, 2, 10 } };
	uint64_t last_tail;
	size_t i = 1;
	int g;

	if (n < 1)
		return 0;
	g = d[0] % 6;
	ring_init(&R, d[0] & 0x80 ? RING_CHUNK : RING_DEFAULT,
	          (uint64_t)d[0] << 40);
	dch_vt_capture(&R);
	if (dch_vt_init(geo[g][0], geo[g][1], geo[g][2]) < 0)
		abort();
	last_tail = R.tail_seq;
	while (i < n)
	{
		uint8_t op = d[i++];
		size_t k;

		if (op == 0xF0 && i < n)
		{
			k = d[i++];
			k = k > n - i ? n - i : k;
			dch_vt_feed(d + i, k);
			i += k;
		}
		else if (op == 0xF1 && i + 1 < n)
		{
			dch_vt_resize(1 + d[i] % 200, 1 + d[i + 1] % 60);
			i += 2;
		}
		else if (op == 0xF2 && i + 1 < n)
		{
			const char *s = ed3[d[i] % 6];
			size_t l = strlen(s), cut = d[i + 1] % (l + 1);

			dch_vt_feed((const unsigned char *)s, cut);
			check();
			dch_vt_feed((const unsigned char *)s + cut, l - cut);
			i += 2;
		}
		else if (op == 0xF3)
		{
			dch_vt_clear();
			if (R.tail_seq != R.head_seq)
				abort();	/* any clear empties it */
		}
		else
		{
			/* a run up to the next op byte, fed whole */
			for (k = i; k < n && d[k] < 0xF0; k++)
				;
			dch_vt_feed(d + i - 1, k - i + 1);
			i = k;
		}
		check();
		if (R.tail_seq < last_tail)
			abort();	/* seqs never go back */
		last_tail = R.tail_seq;
	}
	dch_vt_capture_flush();
	check();
	reads(R.head_seq);
	reads(R.head_seq + (n % 7));
	reads(R.tail_seq);
	dch_vt_feed((const unsigned char *)"\033c", 2);
	if (R.tail_seq != R.head_seq)
		abort();		/* RIS empties it, whatever came before */
	dch_vt_free();
	ring_free(&R);
	return 0;
}
