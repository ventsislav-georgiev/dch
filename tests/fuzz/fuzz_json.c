/* libFuzzer target: bridge.c's JSON helpers (dch_json_*) over an arbitrary
** document, plus a quote -> parse round trip that must give the input back. */
#include "dtach.h"
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	char *s = malloc(n + 1), *fx, *q, *doc, *back, out[256], tiny[4];
	char **v;
	long l;
	int argc;
	size_t qn;

	if (!s)
		return 0;
	memcpy(s, d, n);
	s[n] = '\0';

	/* as a document */
	(void)dch_json_is_object(s);
	(void)dch_json_string(s, "name", out, sizeof out);
	(void)dch_json_string(s, "name", tiny, sizeof tiny);
	(void)dch_json_long(s, "pid", &l);
	(void)dch_json_nested_string(s, "agent", "session_id", out, sizeof out);
	if ((v = dch_json_strv(s, NULL, "argv", &argc)))
		free(v);
	if ((v = dch_json_strv(s, "agent", "launch", &argc)))
		free(v);
	(void)dch_meta_prs(s, out, sizeof out, &l);
	(void)dch_meta_prs(s, tiny, sizeof tiny, &l);
	(void)dch_utf8_trim(s, strlen(s));
	free(dch_utf8_fix(s));

	/* as a string value: quoting it must round-trip (valid UTF-8 only:
	** anything else is written as U+FFFD by design) */
	(void)dch_json_quote(tiny, sizeof tiny, s);
	fx = dch_utf8_fix(s);
	qn = strlen(s) * 6 + 3;	/* worst case: every byte as \u00XX */
	q = malloc(qn);
	doc = malloc(qn + 16);
	back = malloc(strlen(s) + 1);
	if (!fx && q && doc && back)
	{
		/* SIZE_MAX: did not fit, which the bound above rules out */
		if (dch_json_quote(q, qn, s) == SIZE_MAX)
			abort();
		snprintf(doc, qn + 16, "{\"k\":%s}", q);
		if (!dch_json_is_object(doc) ||
		    dch_json_string(doc, "k", back, strlen(s) + 1) < 0 ||
		    strcmp(back, s) != 0)
			abort();
	}
	free(fx);
	free(q);
	free(doc);
	free(back);
	free(s);
	return 0;
}
