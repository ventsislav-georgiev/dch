/* libFuzzer target: the attaching client's keyboard scan. detach_hits reads
** what the user's terminal sends (raw ^\ and Kitty CSI-u key reports) in
** PKT_MAX chunks, as attach_main does. Linked with the rest of dch minus
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
	return 0;
}
