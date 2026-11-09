/* libFuzzer target: the master's control-packet handling. The input is a
** client's byte stream: frame_size() splits it exactly as client_activity
** does, and each frame's payload goes through the pure parsers that
** handle_packet hands it to (the RESPAWN argv, the WAIT pattern scan, the
** READ request incl. --follow's meta/since flags, ATTACH's scrollback) plus
** the resume blob's hex decoder; the same bytes as pty output go through
** track_decmode. Linked with the rest of dch minus master.o, and
** dch.c built with -Dmain=dch_main. */
#include "../../master.c"
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	static const char hay[] = "$ make\nok: built\n$ dch --wait\n";
	unsigned char hex[PKT_MAX + 1], out[PKT_MAX / 2];
	char pat[DCH_WAIT_MAX + 1];
	const char *line;
	size_t off = 0, linelen;
	long fs;

	/* the other direction: the child's pty output, scanned for DEC modes */
	track_decmode(d, n);
	while ((fs = frame_size(d + off, n - off)) > 0)
	{
		const unsigned char *f = d + off, *payload = f + PKT_HDR;
		unsigned int type = f[0];
		unsigned int len = (unsigned int)f[1] | ((unsigned int)f[2] << 8);
		char **v;

		/* the contract client_activity relies on: a whole frame is
		** buffered and fits the inbuf */
		if ((size_t)fs > n - off || (size_t)fs > PKT_HDR + PKT_MAX)
			abort();
		if (type == MSG_READ)
		{
			struct read_req q;

			/* a request it accepts never asks for more than the
			** wire carried, nor a scrollback tail with the caret */
			if (read_req_parse(payload, len, &q) == 0 &&
			    (((q.flags & DCH_READ_F_SINCE) &&
			      len < DCH_READ_REQ_MAX) ||
			     (q.flags & ~(unsigned int)DCH_READ_F_ALL) ||
			     ((q.flags & DCH_READ_F_CURSOR) &&
			      q.source == DCH_READ_RECENT)))
				abort();
		}
		else if (type == MSG_ATTACH)
		{
			/* #101: len is the scrollback ask, never past the cap */
			if (attach_scrollback(len) > DCH_SCROLLBACK_MAX)
				abort();
		}
		else if (type == MSG_RESPAWN && len && payload[0] == 'a' &&
		    (v = argv_from_wire(payload + 1, len - 1)))
			free(v);
		else if (type == MSG_WAIT && len && len <= DCH_WAIT_MAX)
		{
			memcpy(pat, payload, len);
			pat[len] = '\0';
			(void)find_match_line(hay, pat, &line, &linelen);
		}
		else if (type == MSG_RESTART && len < sizeof hex)
		{
			/* a hex string here exercises the resume blob decoder */
			memcpy(hex, payload, len);
			hex[len] = '\0';
			(void)hex_decode((const char *)hex, out, sizeof out);
		}
		off += (size_t)fs;
	}
	return 0;
}
