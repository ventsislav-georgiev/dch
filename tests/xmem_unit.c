/* Units for X-ray --memory's parsers, xmem.c (#322): the frontmatter block
** (keys, metadata.type, quotes, block scalars, a fence never closed), the
** MEMORY.md index line (bullets, dashes, refused targets) and the shell
** rule (a redirect or an editing verb writes); xm_print over a rules tree
** of XM_MANY files that the walk reaches twice (#344), and XM_CALLS
** shell commands read against it (#350).
**
**   cc -I. -o xmem_unit tests/xmem_unit.c xmem.c xray.c json.c && ./xmem_unit
*/
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "xmem.h"

static int fails;

void dch_trace(const char *fmt, ...);
void
dch_trace(const char *fmt, ...)
{
	(void)fmt;
}

#define CHECK(msg, cond) do { if (cond) printf("  ok   %s\n", msg); \
	else { printf("  FAIL %s (line %d)\n", msg, __LINE__); fails++; } } while (0)

static int
is(const char *s, struct xm_span v, const char *want)
{
	if (!want)
		return v.n == 0;
	return v.n == strlen(want) && !memcmp(s + v.s, want, v.n);
}

static int
front(const char *s, const char *name, const char *desc, const char *type)
{
	struct xm_front f;

	return xm_front(s, strlen(s), &f) == 1 && is(s, f.name, name) &&
	       is(s, f.desc, desc) && is(s, f.type, type);
}

static int
lnk(const char *s, const char *title, const char *file, const char *hook)
{
	struct xm_link l;

	return xm_link(s, strlen(s), &l) == 1 && is(s, l.title, title) &&
	       is(s, l.file, file) && is(s, l.hook, hook);
}

static int
nolink(const char *s)
{
	struct xm_link l;

	return xm_link(s, strlen(s), &l) == 0;
}

static int
wrote(const char *s)
{
	return xm_wrote(s, strlen(s));
}

static void
t_front(void)
{
	struct xm_front f;

	CHECK("front: name, description, top-level type",
	      front("---\nname: n\ndescription: d e\ntype: user\n---\nbody\n",
	            "n", "d e", "user"));
	CHECK("front: metadata.type",
	      front("---\nname: n\nmetadata:\n  type: feedback\n---\n", "n",
	            NULL, "feedback"));
	CHECK("front: quotes stripped, CRLF",
	      front("---\r\nname: 'q n'\r\ndescription: \"q d\"\r\n---\r\n",
	            "q n", "q d", NULL));
	CHECK("front: a block scalar gives no value",
	      front("---\ndescription: |\n  long\n  text\nname: n\n---\n", "n",
	            NULL, NULL));
	CHECK("front: the first value wins",
	      front("---\nname: a\nname: b\n---\n", "a", NULL, NULL));
	CHECK("front: an indented key is not top level",
	      front("---\n  name: x\nother:\n  description: y\n---\n", NULL, NULL,
	            NULL));
	CHECK("front: a BOM before the fence",
	      front("\xef\xbb\xbf---\nname: b\n---\n", "b", NULL, NULL));
	CHECK("front: no fence: 0", xm_front("name: x\n", 8, &f) == 0);
	CHECK("front: a fence never closed: 0",
	      xm_front("---\nname: x\n", 12, &f) == 0);
	CHECK("front: empty input: 0", xm_front("", 0, &f) == 0);
	CHECK("front: '----' is no fence", xm_front("----\nname: x\n---\n", 17,
	                                             &f) == 0);
}

static void
t_link(void)
{
	CHECK("link: - [T](f.md) \xe2\x80\x94 hook",
	      lnk("- [Title](file.md) \xe2\x80\x94 the hook", "Title",
	           "file.md", "the hook"));
	CHECK("link: * and ' - '", lnk("* [B](b.md) - hb", "B", "b.md", "hb"));
	CHECK("link: + and ':'", lnk("+ [G](g.md): hg", "G", "g.md", "hg"));
	CHECK("link: an en dash", lnk("- [E](e.md) \xe2\x80\x93 he", "E",
	                               "e.md", "he"));
	CHECK("link: no hook", lnk("- [N](n.md)", "N", "n.md", NULL));
	CHECK("link: indented", lnk("  - [I](i.md) - x", "I", "i.md", "x"));
	CHECK("link: a path: refused", nolink("- [x](../s.md) - h"));
	CHECK("link: a subdirectory: refused", nolink("- [x](d/s.md)"));
	CHECK("link: a URL: refused", nolink("- [x](https://a.b/s.md)"));
	CHECK("link: a dot file: refused", nolink("- [x](.s.md)"));
	CHECK("link: not .md: refused", nolink("- [x](s.txt)"));
	CHECK("link: a backslash: refused", nolink("- [x](a\\b.md)"));
	CHECK("link: a control character: refused", nolink("- [x](a\x01.md)"));
	CHECK("link: no bullet: refused", nolink("[x](a.md)"));
	CHECK("link: a heading: refused", nolink("# Memory"));
	CHECK("link: '.md' alone: refused", nolink("- [x](.md)"));
}

static void
t_wrote(void)
{
	CHECK("wrote: '>'", wrote("echo x > /m/a.md"));
	CHECK("wrote: '>>'", wrote("echo x >> /m/a.md"));
	CHECK("wrote: tee", wrote("echo x | tee /m/a.md"));
	CHECK("wrote: sed -i", wrote("sed -i s/a/b/ /m/a.md"));
	CHECK("wrote: perl -pi", wrote("perl -pi -e s/a/b/ /m/a.md"));
	CHECK("wrote: rm, mv, touch", wrote("rm /m/a.md") &&
	      wrote("mv /m/a.md /m/b.md") && wrote("touch /m/a.md"));
	CHECK("loaded: cat", !wrote("cat /m/a.md"));
	CHECK("loaded: 2>/dev/null reads",
	      !wrote("cat /m/a.md 2>/dev/null"));
	CHECK("loaded: >&2 is no file", !wrote("cat /m/a.md >&2"));
	CHECK("loaded: sed without -i", !wrote("sed -n 1p /m/a.md"));
	CHECK("loaded: 'rm' inside a word", !wrote("grep -r firmware /m/a.md"));
	CHECK("loaded: empty", !xm_wrote("", 0));
}

#ifndef XM_MANY
#define XM_MANY 3000
#endif

static double
now(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

#ifndef XM_CALLS
#define XM_CALLS 8000
#endif

/* The shell rule over the XM_MANY rules (#350): a command names a row by
** its full path followed by a terminator, so r00007.md is loaded and
** r00008.md (named as r00008.mdx) is not. Each command was matched against
** every row, rows x bytes: XM_CALLS 2 KB commands full of '/' took 37.6 s
** past the rows' own cost (base, t_many's run without a transcript); one
** pass over each, narrowing the rows by path, takes 1.0 s on a loaded Mac.
** The bound is 25 times the rows' own cost in the same run (base, 0.4 s
** there: the old code took 90 times it, the new takes 3), so it holds on a
** slow runner as well. */
static void
t_shell(const char *home, struct xm_req *q, double base)
{
	char tp[256], cmd[2048], *buf = NULL, *l, *e;
	size_t len = 0, k, hit = 0, miss = 0;
	struct stat st;
	double t;
	FILE *f;

	snprintf(tp, sizeof tp, "%s/t.jsonl", home);
	if (!(f = fopen(tp, "w")))
	{
		CHECK("shell: a transcript", 0);
		return;
	}
	for (k = 0; k + 2 < sizeof cmd; k += 2)
		memcpy(cmd + k, "/q", 2);
	cmd[k] = '\0';
	fprintf(f, "{\"type\":\"assistant\",\"message\":{\"role\":"
	        "\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":"
	        "\"toolu_0\",\"name\":\"Bash\",\"input\":{\"command\":"
	        "\"cat %s/.claude/rules/r00007.md %s/.claude/rules/r00008.mdx"
	        "\"}}]}}\n", home, home);
	for (k = 1; k <= XM_CALLS; k++)
		fprintf(f, "{\"type\":\"assistant\",\"message\":{\"role\":"
		        "\"assistant\",\"content\":[{\"type\":\"tool_use\","
		        "\"id\":\"toolu_%zu\",\"name\":\"Bash\",\"input\":"
		        "{\"command\":\"ls %s\"}}]}}\n", k, cmd);
	fclose(f);
	stat(tp, &st);
	q->path = tp;
	q->st = &st;
	f = open_memstream(&buf, &len);
	t = now();
	xm_print(f, q);
	t = now() - t - base;
	fclose(f);
	q->path = NULL;
	unlink(tp);
	for (l = buf; l && *l; l = e ? e + 1 : NULL)
	{
		if ((e = strchr(l, '\n')))
			*e = '\0';
		hit += strstr(l, "/r00007.md\"") && strstr(l, "\"session\":\"loaded\"");
		miss += strstr(l, "/r00008.md\"") && strstr(l, "\"session\":\"");
	}
	CHECK("shell: a full path loads its row, a longer name does not",
	      hit == 1 && miss == 0);
	printf("       %d calls over %d rows: %.2f s more\n", XM_CALLS, XM_MANY,
	       t);
	CHECK("shell: one pass per command, not rows x bytes (under 25 times "
	      "the rows' own cost)", t < 25 * (base > 0.2 ? base : 0.2));
	free(buf);
}

/* cwd = home, cfg = home/.claude: its CLAUDE.md and rules come twice and
** are listed once. The dedupe was N^2 (#344); a 3000-file tree does not
** show it (its open and stat calls dominate), -DXM_MANY=50000 does: 25.4 s
** before, 16.5 s after on a loaded Mac, the rest is open(). The 30 s bound
** is about 50x the 3000-file run, so only a hang trips it. */
static void
t_many(void)
{
	char home[] = "/tmp/xmem_unit.XXXXXX", p[256], *buf = NULL, *l, *e;
	struct xm_req q = { "claude", NULL, NULL, NULL, NULL, NULL };
	size_t len = 0, rows = 0, mine = 0;
	FILE *f;
	double t;
	int k;

	if (!mkdtemp(home))
	{
		CHECK("many: a temp dir", 0);
		return;
	}
	snprintf(p, sizeof p, "%s/.claude", home);
	mkdir(p, 0700);
	snprintf(p, sizeof p, "%s/.claude/rules", home);
	mkdir(p, 0700);
	snprintf(p, sizeof p, "%s/.claude/CLAUDE.md", home);
	if ((f = fopen(p, "w")))
		fclose(f);
	for (k = 0; k < XM_MANY; k++)
	{
		snprintf(p, sizeof p, "%s/.claude/rules/r%05d.md", home, k);
		if ((f = fopen(p, "w")))
			fclose(f);
	}
	snprintf(p, sizeof p, "%s/.claude", home);
	q.cfg = p;
	q.cwd = home;
	f = open_memstream(&buf, &len);
	t = now();
	xm_print(f, &q);
	t = now() - t;
	fclose(f);
	for (l = buf; l && *l; l = e ? e + 1 : NULL)
	{
		if ((e = strchr(l, '\n')))
			*e = '\0';
		if (!strncmp(l, "{\"memory\":", 10) && ++rows)
			mine += strstr(l, "/.claude/rules/r") ||
			        strstr(l, "/.claude/CLAUDE.md");
	}
	CHECK("many: each instruction file once", mine == XM_MANY + 1);
	printf("       %zu rows in %.2f s\n", rows, t);
	CHECK("many: n log n, not N^2 (30 s bound)", t < 30);
	free(buf);
	t_shell(home, &q, t);
	for (k = 0; k < XM_MANY; k++)
	{
		snprintf(p, sizeof p, "%s/.claude/rules/r%05d.md", home, k);
		unlink(p);
	}
	snprintf(p, sizeof p, "%s/.claude/rules", home);
	rmdir(p);
	snprintf(p, sizeof p, "%s/.claude/CLAUDE.md", home);
	unlink(p);
	snprintf(p, sizeof p, "%s/.claude", home);
	rmdir(p);
	rmdir(home);
}

int
main(void)
{
	t_front();
	t_link();
	t_wrote();
	t_many();
	printf("%s: %d failed\n", fails ? "FAIL" : "ok", fails);
	return fails != 0;
}
