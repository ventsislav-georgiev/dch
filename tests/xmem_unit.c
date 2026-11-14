/* Units for X-ray --memory's parsers, xmem.c (#322): the frontmatter block
** (keys, metadata.type, quotes, block scalars, a fence never closed), the
** MEMORY.md index line (bullets, dashes, refused targets) and the shell
** rule (a redirect or an editing verb writes).
**
**   cc -I. -o xmem_unit tests/xmem_unit.c xmem.c xray.c json.c && ./xmem_unit
*/
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
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

int
main(void)
{
	t_front();
	t_link();
	t_wrote();
	printf("%s: %d failed\n", fails ? "FAIL" : "ok", fails);
	return fails != 0;
}
