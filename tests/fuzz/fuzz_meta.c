/* libFuzzer target: the --meta validator (what `dch --meta` accepts and
** session_meta re-checks on read) and the picker's "prs" reader over it.
** Built with -Dmain=dch_main so dch.c's statics are reachable. */
#include "../../dch.c"
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	char *s = malloc(n + 1), out[512];
	long merged;

	if (!s)
		return 0;
	memcpy(s, d, n);
	s[n] = '\0';
	/* the --meta gate, as do_meta applies it */
	if (meta_bytes_ok(s, n) && dch_json_is_object(s))
		(void)dch_meta_prs(s, out, sizeof out, &merged);
	free(s);
	return 0;
}
