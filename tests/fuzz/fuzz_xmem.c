/* libFuzzer target: X-ray --memory (#322), its parsers and the whole answer.
** Every byte is hostile: the memory files and the transcript are whatever
** the session wrote.
**
** Input: byte 0 picks the path.
**   even: the rest through xm_front, xm_link and xm_wrote; every span they
**      give lies inside the input, a link's file is 4..255 bytes ending in
**      .md with no '/', and xm_front's 1 means a closed block;
**   odd: the rest split at the first two "\n@@\n" into MEMORY.md, one memory
**      file (a.md) and a transcript (every "@M" in it the memory directory's
**      path, so its calls can name the files; bit 1 reads it as a Codex
**      rollout, the file is then CODEX_HOME's AGENTS.md and "@M" that
**      directory), laid out under a temp directory as a Claude config, then
**      xm_print: every line is JSON under XM_LINE + 1 bytes, the header
**      first and the done line last, never more than XM_ROWS rows. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "../../bridge.h"
#include "../../xmem.h"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static char root[256];

static void
span_in(struct xm_span v, size_t n)
{
	if (v.s > n || v.n > n - v.s)
		abort();
}

static void
parsers(const char *s, size_t n)
{
	struct xm_front f;
	struct xm_link l;
	size_t k;

	if (xm_front(s, n, &f))
	{
		span_in(f.name, n);
		span_in(f.desc, n);
		span_in(f.type, n);
	}
	else if (f.name.n || f.desc.n || f.type.n)
		abort();
	if (xm_link(s, n, &l))
	{
		span_in(l.title, n);
		span_in(l.file, n);
		span_in(l.hook, n);
		if (l.file.n < 4 || l.file.n > 255 || s[l.file.s] == '.' ||
		    memcmp(s + l.file.s + l.file.n - 3, ".md", 3))
			abort();
		for (k = 0; k < l.file.n; k++)
			if (s[l.file.s + k] == '/' || s[l.file.s + k] == '\\' ||
			    (unsigned char)s[l.file.s + k] < 0x20)
				abort();
	}
	(void)xm_wrote(s, n);
}

static void
put(const char *path, const char *s, size_t n)
{
	FILE *f = fopen(path, "wb");

	if (!f)
		abort();
	if (n)
		fwrite(s, 1, n, f);
	fclose(f);
}

/* s with every "@M" as rep */
static char *
subst(const char *s, size_t n, const char *rep, size_t *out)
{
	size_t rl = strlen(rep), k, o = 0, c = 0;
	char *b;

	for (k = 0; k + 1 < n; k++)
		c += s[k] == '@' && s[k + 1] == 'M';
	if (!(b = malloc(n + c * rl + 1)))
		abort();
	for (k = 0; k < n; k++)
		if (k + 1 < n && s[k] == '@' && s[k + 1] == 'M')
		{
			memcpy(b + o, rep, rl);
			o += rl;
			k++;
		}
		else
			b[o++] = s[k];
	*out = o;
	return b;
}

static void
answer(const char *s, size_t n, int codex)
{
	static const char sep[] = "\n@@\n";
	const char *p[3] = { s, NULL, NULL }, *e;
	size_t len[3] = { n, 0, 0 }, tn, ll = 0, rows = 0;
	char path[512], mem[400], tr[400], *t, *out = NULL, *line, *nl;
	struct json_parser q;
	struct stat st;
	FILE *m;
	int k, last = 0;
	struct xm_req r;

	for (k = 0; k < 2; k++)
		if ((e = memmem(p[k], len[k], sep, 4)))
		{
			p[k + 1] = e + 4;
			len[k + 1] = len[k] - (size_t)(e + 4 - p[k]);
			len[k] = (size_t)(e - p[k]);
		}
		else
			break;
	snprintf(mem, sizeof mem, "%s/cfg/projects/-p/memory", root);
	snprintf(tr, sizeof tr, "%s/cfg/projects/-p/s.jsonl", root);
	snprintf(path, sizeof path, "%s/MEMORY.md", mem);
	put(path, p[0], len[0]);
	snprintf(path, sizeof path, codex ? "%s/cfg/AGENTS.md" : "%s/a.md",
	         codex ? root : mem);
	put(path, p[1] ? p[1] : "", len[1]);
	snprintf(path, sizeof path, "%s/cfg", root);
	t = subst(p[2] ? p[2] : "", len[2], codex ? path : mem, &tn);
	put(tr, t, tn);
	free(t);
	if (stat(tr, &st) != 0)
		abort();
	memset(&r, 0, sizeof r);
	r.agent = codex ? "codex" : "claude";
	r.cfg = path;
	r.path = tr;
	r.st = &st;
	r.cwd = mem;
	if (!(m = open_memstream(&out, &ll)))
		abort();
	if (xm_print(m, &r) != 0)
		abort();
	fclose(m);
	for (line = out; line < out + ll; line = nl + 1)
	{
		if (!(nl = memchr(line, '\n', (size_t)(out + ll - line))) ||
		    (size_t)(nl - line) > XM_LINE)
			abort();
		*nl = '\0';
		memset(&q, 0, sizeof q);
		if (dch_json_parse(&q, line) < 0)
			abort();
		dch_json_free(&q);
		if ((line == out) != !strncmp(line, "{\"schema\":1,", 12))
			abort();
		last = !strncmp(line, "{\"done\":true,", 13);
		rows += !strncmp(line, "{\"memory\":", 10);
	}
	if (!last || rows > XM_ROWS)
		abort();
	free(out);
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	char path[300];

	if (n < 1)
		return 0;
	if (!(d[0] & 1))
	{
		parsers((const char *)d + 1, n - 1);
		return 0;
	}
	if (!root[0])
	{
		const char *tmp = getenv("TMPDIR");

		snprintf(root, sizeof root, "%s/fuzz_xmem.XXXXXX",
		         tmp && *tmp ? tmp : "/tmp");
		if (!mkdtemp(root))
			abort();
		snprintf(path, sizeof path, "%s/cfg", root);
		mkdir(path, 0700);
		snprintf(path, sizeof path, "%s/cfg/projects", root);
		mkdir(path, 0700);
		snprintf(path, sizeof path, "%s/cfg/projects/-p", root);
		mkdir(path, 0700);
		snprintf(path, sizeof path, "%s/cfg/projects/-p/memory", root);
		mkdir(path, 0700);
	}
	answer((const char *)d + 1, n - 1, d[0] & 2);
	return 0;
}
