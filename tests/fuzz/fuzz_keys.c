/* libFuzzer target: the attaching client's keyboard scan. detach_hits reads
** what the user's terminal sends (raw ^\ and Kitty CSI-u key reports) in
** PKT_MAX chunks, as attach_main does. focus_filter (#100) gets the same
** bytes cut in two reads: forwarding, it must hand every byte back in order;
** the output scan must survive anything. Linked with the rest of dch minus
** attach.o, and dch.c built with -Dmain=dch_main (detach_char is ^\). */
#include "../../attach.c"
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
	if (detach_hits(&pkt) > 2)
		abort();	/* the scan stops at two */

	{
		unsigned char b[sizeof pkt.u.buf + 2], got[sizeof pkt.u.buf + 2];
		size_t cut = n ? d[0] % (n + 1) : 0, gl = 0;
		int in = 0, l;

		(void)focus_scan(d, (ssize_t)n);
		inner_focus = 1;	/* forwarding: identity */
		fpend_n = 0;
		memcpy(b, d, cut);
		l = focus_filter(b, (int)cut, &in, 0);
		memcpy(got, b, l);
		gl = l;
		memcpy(b, d + cut, n - cut);
		l = focus_filter(b, (int)(n - cut), &in, 1);
		memcpy(got + gl, b, l);
		gl += l;
		if (gl != n || memcmp(got, d, n) != 0 || fpend_n)
			abort();
	}
	return 0;
}
