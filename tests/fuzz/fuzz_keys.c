/* libFuzzer target: the attaching client's keyboard scan. detach_hits reads
** what the user's terminal sends (raw ^\ and Kitty CSI-u key reports) in
** PKT_MAX chunks, as attach_main does. focus_filter (#100) gets the same
** bytes cut in two reads: forwarding, it must hand every byte back in order;
** the output scan must survive anything. Linked with the rest of dch minus
** attach.o, and dch.c built with -Dmain=dch_main (detach_char is ^\).
** The X-ray chord's x decode (xray_x, #187) reads what follows a press
** and a whole read. The picker's sequence reader (pk_csi, #132) gets the
** bytes after ESC [ as pk_key collects them, at most 16. */
#include "../../attach.c"
#include "../../picker.h"
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

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
		(void)xray_x(d, (int)n);
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
	return 0;
}
