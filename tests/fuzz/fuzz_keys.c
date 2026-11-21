/* libFuzzer target: the attaching client's keyboard scan. detach_hits reads
** what the user's terminal sends (raw ^\ and Kitty CSI-u key reports) in
** PKT_MAX chunks, as attach_main does. focus_filter (#100) gets the same
** bytes cut in two reads: forwarding, it must hand every byte back in order;
** the output scan must survive anything. Linked with the rest of dch minus
** attach.o, and dch.c built with -Dmain=dch_main (detach_char is ^\).
** The X-ray chord's x decode (xray_x, #187) reads what follows a press
** and a whole read; a read of key releases only (#196) is -1. The picker's sequence reader (pk_csi, #132) gets the
** bytes after ESC [ as pk_key collects them, at most 16. The handover
** scan (switch_tail, #591) reads the session's output through the rolling
** tail: a hit is the input's end, its name printable and inside the cap,
** and never with a key the bytes do not carry. switch_hold (#591) is the
** start of a handover still to come: held bytes are a suffix, and the
** read loop's hold + tail logic never emits a handover byte, and emits all
** else in order. */
#include "../../attach.c"
#include "../../picker.h"
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static const char hkey[] = "0123456789abcdef0123456789abcdef";

/* The read loop's hold + tail logic over two reads: what it writes into
** out (cap: n), 1 when it switched. Held bytes at the end are flushed. */
static int
hold_sim(const uint8_t *d, size_t n, size_t cut, uint8_t *out, size_t *on)
{
	uint8_t comb[sizeof swhold + 2 * 4096];
	size_t at, c, total, keep, h, o = 0;
	char name[sizeof attach_to];

	swtail_n = swhold_n = 0;
	for (c = 0; c < 2; c++)
	{
		const uint8_t *b = d + (c ? cut : 0);
		size_t len = c ? n - cut : cut;

		memcpy(comb, swhold, swhold_n);
		memcpy(comb + swhold_n, b, len);
		total = swhold_n + len;
		swhold_n = 0;
		swtail_add(b, len);
		h = switch_tail(swtail, swtail_n, hkey, name, sizeof name);
		if (h)
		{
			memcpy(out + o, comb, total - h);
			*on = o + total - h;
			return 1;
		}
		keep = switch_hold(swtail, swtail_n, hkey);
		if (keep > total)
			abort();
		memcpy(swhold, comb + total - keep, keep);
		swhold_n = keep;
		at = total - keep;
		memcpy(out + o, comb, at);
		o += at;
	}
	memcpy(out + o, swhold, swhold_n);	/* restore_term's flush */
	*on = o + swhold_n;
	swhold_n = 0;
	return 0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct packet pkt;

	if (n > sizeof pkt.u.buf)
		return 0;
	memset(&pkt, 0, sizeof pkt);
	memcpy(pkt.u.buf, d, n);
	pkt.len = (unsigned short)n;
	{
		int h = detach_hits(&pkt);

		if (h > 2)
			abort();	/* the scan stops at two */
		/* the X-ray chord's x (#187): after the press, and a read */
		if (h == 1 && (detach_end < 1 || detach_end > (int)n))
			abort();
		if (h == 1)
			(void)xray_x(pkt.u.buf + detach_end, (int)n - detach_end);
		{
			int x = xray_x(d, (int)n);

			/* -1, releases only (#196): never for an empty read */
			if (x < -1 || x > 1 || (x < 0 && n == 0))
				abort();
		}
	}
	{
		int k = pk_csi(d, n < 16 ? (int)n : 16);

		if (k < 0 || k > PK_SENTER)
			abort();
	}

	{
		unsigned char b[sizeof pkt.u.buf + 2], got[sizeof pkt.u.buf + 2];
		size_t cut = n ? d[0] % (n + 1) : 0, gl = 0;
		int in = 0, l;

		(void)focus_scan(d, (ssize_t)n);
		inner_focus = 1;	/* forwarding: identity */
		fpend_n = dq_n = 0;
		memcpy(b, d, cut);
		l = focus_filter(b, (int)cut, &in, 0);
		memcpy(got, b, l);
		gl = l;
		/* the delay line (#117) goes out on its timer, before the
		** next read's bytes */
		memcpy(got + gl, dq, dq_n);
		gl += dq_n;
		dq_n = 0;
		memcpy(b, d + cut, n - cut);
		l = focus_filter(b, (int)(n - cut), &in, 1);
		memcpy(got + gl, b, l);
		gl += l;
		memcpy(got + gl, dq, dq_n);
		gl += dq_n;
		dq_n = 0;
		if (gl != n || memcmp(got, d, n) != 0 || fpend_n)
			abort();
	}
	{
		const char *key = hkey;
		char name[sizeof attach_to];
		size_t h, i;

		swtail_n = 0;
		swtail_add(d, n / 2);
		swtail_add(d + n / 2, n - n / 2);
		h = switch_tail(swtail, swtail_n, key, name, sizeof name);
		if (h > swtail_n || (h && (!name[0] ||
		    strlen(name) >= sizeof name ||
		    memcmp(swtail + swtail_n - h, DCH_SWITCH_OSC,
		           sizeof DCH_SWITCH_OSC - 1) != 0 ||
		    memcmp(swtail + swtail_n - h + sizeof DCH_SWITCH_OSC - 1, key,
		           strlen(key)) != 0)))
			abort();
		for (i = 0; h && name[i]; i++)
			if ((unsigned char)name[i] < 0x20 || name[i] == 0x7f)
				abort();
	}
	{
		/* the hold is a suffix that starts at an ESC */
		size_t k = switch_hold(d, n, hkey);

		if (k > n || k >= sizeof swtail || (k && d[n - k] != 033))
			abort();
	}
	if (n < 2048)
	{
		/* random input, cut anywhere: emitted whole and unchanged */
		uint8_t out[4096];
		size_t on = 0;

		if (!hold_sim(d, n, n ? d[0] % (n + 1) : 0, out, &on) &&
		    (on != n || memcmp(out, d, n) != 0))
			abort();
	}
	{
		/* a handover after d, split at every offset: none of it emitted */
		uint8_t st[2048 + 128], out[2048 + 128];
		size_t sl = n < 2048 ? n : 2048, hl, on = 0, i;
		char tail[128];

		memcpy(st, d, sl);
		for (i = 0; i < sl; i++)	/* no ESC of d can start a handover */
			if (st[i] == 033)
				st[i] = 'e';
		hl = (size_t)snprintf(tail, sizeof tail, "%s%s;nm%zu\a",
		                      DCH_SWITCH_OSC, hkey, sl);
		memcpy(st + sl, tail, hl);
		for (i = 0; i <= sl + hl; i++)
			if (!hold_sim(st, sl + hl, i, out, &on) || on != sl ||
			    memcmp(out, st, sl) != 0)
				abort();
	}
	return 0;
}
