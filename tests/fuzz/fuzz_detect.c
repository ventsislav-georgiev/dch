/* libFuzzer target: state detection on screen text a session's program
** controls. The input is a fetched screen as the master returns it: plain
** text for --status/--ls-json (tail_fold, det_blocker, composer_find), with
** SGR for the Claude composer gate (faint_text, gate_screen), and the
** auto-allow parse (dch_allow_prompt). Built with -Dmain=dch_main so dch.c's
** statics are reachable. */
#include "../../dch.c"
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	const char *raw = (const char *)d;
	char *fold, *ident, *title, *end, *z;

	if (n > DET_CAP)
		return 0;
	/* --status: the folded tail, as detect_state reads it */
	if ((fold = tail_fold(raw, n)))
	{
		(void)det_match(det_suppress, sizeof det_suppress /
		                sizeof det_suppress[0], fold);
		(void)det_blocker(fold);
		(void)det_match(det_working, sizeof det_working /
		                sizeof det_working[0], fold);
		(void)det_spinner(fold);
		free(fold);
	}
#ifdef HAVE_LIBGHOSTTY_VT
	{
		/* the composer gate (full build only): SGR-carrying screen to
		** text + faint flags, as screen_faint does */
		size_t k = faint_text(raw, n, NULL, NULL);
		char *txt = calloc(k + 1, 1), *dim = calloc(k + 1, 1), *comp, *sq;

		if (txt && dim && faint_text(raw, n, txt, dim) == k)
		{
			(void)gate_screen(txt, dim, 0);
			(void)gate_screen(txt, dim, 1);
			/* --say's delivery check squeezes the composer row */
			if ((comp = composer_find(txt, &end)) &&
			    (sq = squeeze(comp + 3, (size_t)(end - comp - 3),
			                  (size_t)-1)))
				free(sq);
		}
		free(txt);
		free(dim);
	}
#endif
	/* auto-allow parses the plain screen, NUL-terminated as fetched */
	if ((z = malloc(n + 1)))
	{
		memcpy(z, raw, n);
		z[n] = '\0';
		(void)composer_find(z, &end);
		if (dch_allow_prompt(z, &ident, &title))
		{
			free(ident);
			free(title);
		}
		free(z);
	}
	return 0;
}
