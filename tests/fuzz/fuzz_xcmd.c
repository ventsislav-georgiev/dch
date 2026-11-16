/* libFuzzer target: X-ray's command source (#321), xcmd.c's pure parts.
** The line validator reads the stdout of a user's command (a script that
** may print anything); the argv split reads the user's config line.
**
** Input: byte 0 picks.
**   bit 0 clear: the rest, cut at '\n', line by line through xcmd_line,
**     the kind from bit 1 (bit 3 set: define, #405), `first` from bit 2
**     for the first line only.
**     A row line must be one line of valid UTF-8 within XCMD_LINE_MAX that
**     parses as {"task"|"memory":{...}} with an id and a title; a title
**     must be valid UTF-8 of at most XR_TITLE_MAX scalars; anything else
**     leaves out empty. A define row: {"title","passage","url","status"},
**     a title, no control byte decoded but a passage's '\n' and '\t'.
**   bit 0 set: the rest up to a NUL through xcmd_argv, home "/h" or none
**     (bit 1), a buffer of 64 or 4096 bytes (bit 2): at most XCMD_ARGS_MAX
**     words, every word inside the buffer and NUL-terminated.
*/
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../../xcmd.h"
#include "../../bridge.h"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static void
check_title(const char *t)
{
	const unsigned char *u = (const unsigned char *)t;
	size_t i = 0, sc = 0;

	if (!dch_utf8_ok(u, strlen(t)))
		abort();
	for (; u[i]; i++)
		sc += (u[i] & 0xc0) != 0x80;
	if (sc > XR_TITLE_MAX)
		abort();
}

static void
check_row(enum xcmd_kind kind, const char *o)
{
	struct json_parser p;
	size_t l = strlen(o);

	if (!l || l > XCMD_LINE_MAX || strchr(o, '\n') ||
	    !dch_utf8_ok((const unsigned char *)o, l))
		abort();
	if (dch_json_parse(&p, o) != 0 || p.tok[0].kind != JSON_OBJECT ||
	    dch_json_field(&p, 0, kind == XCMD_TASK ? "task" : "memory") != 2 ||
	    p.tok[2].kind != JSON_OBJECT ||
	    dch_json_field(&p, 2, "title") < 0 || dch_json_field(&p, 2, "id") < 0)
		abort();
	dch_json_free(&p);
}

/* a define row's text field k: absent never, null or text with no control
** byte (nl: '\n' allowed) */
static void
check_def_text(const struct json_parser *p, const char *k, int nl, int need)
{
	static char t[XCMD_LINE_MAX + 1];
	int f = dch_json_field(p, 0, k);
	size_t i;

	if (f < 0)
		abort();
	if (p->tok[f].kind != JSON_STRING)
	{
		if (need || p->text[p->tok[f].start] != 'n')
			abort();
		return;
	}
	if (dch_json_decode(p, f, t, sizeof t) != 0 || (need && !t[0]))
		abort();
	for (i = 0; t[i]; i++)
		if (((unsigned char)t[i] < 0x20 &&
		     !(nl && (t[i] == '\n' || t[i] == '\t'))) ||
		    t[i] == 0x7f)
			abort();
}

static void
check_def(const char *o)
{
	struct json_parser p;
	size_t l = strlen(o);

	if (!l || l > XCMD_LINE_MAX || strchr(o, '\n') ||
	    !dch_utf8_ok((const unsigned char *)o, l) ||
	    dch_json_parse(&p, o) != 0 || p.tok[0].kind != JSON_OBJECT)
		abort();
	check_def_text(&p, "title", 0, 1);
	check_def_text(&p, "passage", 1, 0);
	check_def_text(&p, "url", 0, 0);
	check_def_text(&p, "status", 0, 0);
	dch_json_free(&p);
}

static char out[XCMD_LINE_MAX + 1];

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	enum xcmd_kind kind;
	const char *s;
	size_t i, e;
	int first;

	if (!n)
		return 0;
	if (d[0] & 1)
	{
		char *line = malloc(n), *buf, *av[XCMD_ARGS_MAX + 1];
		size_t cap = d[0] & 4 ? 4096 : 64;
		int argc, k;

		/* exactly cap bytes on the heap: ASan sees a write past it */
		buf = malloc(cap);
		if (!line || !buf)
		{
			free(line);
			free(buf);
			return 0;
		}
		memcpy(line, d + 1, n - 1);
		line[n - 1] = '\0';
		argc = xcmd_argv(line, d[0] & 2 ? "/h" : NULL, buf, cap, av,
		                 XCMD_ARGS_MAX + 1);
		if (argc > XCMD_ARGS_MAX || (argc >= 0 && av[argc]))
			abort();
		for (k = 0; k < argc; k++)
			if (av[k] < buf || av[k] >= buf + cap ||
			    !memchr(av[k], '\0', (size_t)(buf + cap - av[k])))
				abort();
		free(line);
		free(buf);
		return 0;
	}
	kind = d[0] & 8 ? XCMD_DEFINE : d[0] & 2 ? XCMD_MEMORY : XCMD_TASK;
	first = (d[0] & 4) != 0;
	s = (const char *)d + 1;
	n--;
	for (i = 0; i <= n; i = e + 1)
	{
		enum xcmd_line_kind k;
		const char *nl = memchr(s + i, '\n', n - i);

		e = nl ? (size_t)(nl - s) : n;
		k = xcmd_line(kind, s + i, e - i, first, 7, out, sizeof out);
		if (k == XCMD_ROW && kind == XCMD_DEFINE)
			check_def(out);
		else if (k == XCMD_ROW)
			check_row(kind, out);
		else if (k == XCMD_TITLE)
			check_title(out);
		else if (*out)
			abort();
		first = 0;
	}
	return 0;
}
