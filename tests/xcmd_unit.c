/* Units for X-ray's command source, xcmd.c (#321): the argv split (quotes,
** ~/, no shell words) and the line validator (bare and wrapped rows, the
** title line, status and id rules, cuts, escapes, caps). Also the driver
** tests/xcmd_test.py runs the runner through:
**
**   xcmd_unit                                     the units
**   xcmd_unit run task|memory CMD [CWD [SESSION [AGENT [SID]]]]
**       rows to stdout as they arrive, then one JSON result line to stderr;
**       XCMD_STOP_AFTER=n: the callback ends the run after n rows;
**       XCMD_QUIET=1: rows counted, not printed; XCMD_EINTR=1: a 1 ms
**       interval timer interrupts every blocking call
**
**   cc -I. -o xcmd_unit tests/xcmd_unit.c xcmd.c xray.c json.c && ./xcmd_unit
*/
#include <errno.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>
#include "xcmd.h"
#include "bridge.h"

static int fails;

void dch_trace(const char *fmt, ...);
void
dch_trace(const char *fmt, ...)
{
	(void)fmt;
}

#define CHECK(msg, cond) do { if (cond) printf("  ok   %s\n", msg); \
	else { printf("  FAIL %s (line %d)\n", msg, __LINE__); fails++; } } while (0)

static char buf[4096], *av[XCMD_ARGS_MAX + 1];

static int
split(const char *s, const char *home)
{
	return xcmd_argv(s, home, buf, sizeof buf, av, XCMD_ARGS_MAX + 1);
}

static void
t_argv(void)
{
	char big[200];
	int k;

	CHECK("argv: blanks separate, runs collapse",
	      split("  a\tb   c ", NULL) == 3 && !strcmp(av[0], "a") &&
	      !strcmp(av[1], "b") && !strcmp(av[2], "c") && !av[3]);
	CHECK("argv: single and double quotes, parts join, no escapes inside",
	      split("'a b' \"c 'd\" e'f'\"g\" 'x\\'", NULL) == 4 &&
	      !strcmp(av[0], "a b") && !strcmp(av[1], "c 'd") &&
	      !strcmp(av[2], "efg") && !strcmp(av[3], "x\\"));
	CHECK("argv: an empty quoted word stays a word",
	      split("p \"\" ''", NULL) == 3 && !*av[1] && !*av[2]);
	CHECK("argv: a leading unquoted ~/ is home, nowhere else",
	      split("~/bin/t ~/ '~/q' a~/b ~x ~", "/h") == 6 &&
	      !strcmp(av[0], "/h/bin/t") && !strcmp(av[1], "/h/") &&
	      !strcmp(av[2], "~/q") && !strcmp(av[3], "a~/b") &&
	      !strcmp(av[4], "~x") && !strcmp(av[5], "~"));
	CHECK("argv: ~/ with no home or an empty one is refused",
	      split("~/t", NULL) == -1 && split("~/t", "") == -1);
	CHECK("argv: shell words are plain bytes ($HOME ; `id` * | > &&)",
	      split("t $HOME ; `id` * | >o && $(x)", "/h") == 9 &&
	      !strcmp(av[1], "$HOME") && !strcmp(av[2], ";") &&
	      !strcmp(av[3], "`id`") && !strcmp(av[4], "*") &&
	      !strcmp(av[8], "$(x)"));
	CHECK("argv: an open quote is refused",
	      split("t 'a", NULL) == -1 && split("t \"a", NULL) == -1);
	CHECK("argv: empty and blank lines are refused",
	      split("", NULL) == -1 && split(" \t ", NULL) == -1);
	for (k = 0; k < 65; k++)
		memcpy(big + k * 2, "a ", 2);
	big[130] = '\0';
	CHECK("argv: 65 words are refused, 64 taken",
	      split(big, NULL) == -1 && (big[128] = '\0', split(big, NULL) == 64));
	CHECK("argv: a buffer too small is refused, never overrun",
	      xcmd_argv("abcdef", NULL, buf, 6, av, 4) == -1 &&
	      xcmd_argv("abcde", NULL, buf, 6, av, 4) == 1 &&
	      xcmd_argv("~/abc", "/home", buf, 8, av, 4) == -1 &&
	      xcmd_argv("~/ab", "/home", buf, 9, av, 4) == 1 &&
	      !strcmp(av[0], "/home/ab"));
}

static char out[XCMD_LINE_MAX + 1];

static enum xcmd_line_kind
line(enum xcmd_kind k, const char *s, int first)
{
	return xcmd_line(k, s, strlen(s), first, 3, out, sizeof out);
}

#define NULLS "\"active\":null,\"note\":null,\"created\":null," \
	"\"updated\":null,\"chapter\":null,\"created_chapter\":null"

static void
t_task(void)
{
	char *big;
	size_t k;

	CHECK("task: a bare row, every contract field, in order",
	      line(XCMD_TASK, "{\"id\":7,\"title\":\"Fix\",\"status\":"
	           "\"in_progress\",\"x\":{\"y\":1}}", 0) == XCMD_ROW &&
	      !strcmp(out, "{\"task\":{\"id\":\"7\",\"title\":\"Fix\",\"status\":"
	              "\"active\",\"label\":\"in_progress\"," NULLS
	              ",\"blocked_by\":[],\"url\":null,\"group\":null}}"));
	CHECK("task: the wrapped row reads the same",
	      line(XCMD_TASK, " {\"task\":{\"id\":\"7\",\"title\":\"Fix\","
	           "\"status\":\"in_progress\"}}\r", 0) == XCMD_ROW &&
	      !strcmp(out, "{\"task\":{\"id\":\"7\",\"title\":\"Fix\",\"status\":"
	              "\"active\",\"label\":\"in_progress\"," NULLS
	              ",\"blocked_by\":[],\"url\":null,\"group\":null}}"));
	CHECK("task: every field kept, label wins over the status word",
	      line(XCMD_TASK, "{\"id\":\"a\",\"title\":\"T\",\"status\":\"done\","
	           "\"label\":\"PARKED\",\"active\":\"Doing\",\"note\":\"n\","
	           "\"created\":\"2026-11-14T07:01:02Z\",\"updated\":\"u\","
	           "\"chapter\":\"x\",\"blocked_by\":[1,\"b\",true,null,{},[]],"
	           "\"url\":\"https://e.x/1?a=b\",\"group\":\"g\"}", 0) == XCMD_ROW &&
	      !strcmp(out, "{\"task\":{\"id\":\"a\",\"title\":\"T\",\"status\":"
	              "\"done\",\"label\":\"PARKED\",\"active\":\"Doing\",\"note\":"
	              "\"n\",\"created\":\"2026-11-14T07:01:02Z\",\"updated\":"
	              "\"u\",\"chapter\":null,\"created_chapter\":null,"
	              "\"blocked_by\":[\"1\",\"b\"],\"url\":\"https://e.x/1?a=b\","
	              "\"group\":\"g\"}}"));
	CHECK("task: no status is pending, label null",
	      line(XCMD_TASK, "{\"title\":\"T\",\"id\":1}", 0) == XCMD_ROW &&
	      strstr(out, "\"status\":\"pending\",\"label\":null"));
	CHECK("task: an unknown status is pending, the word kept in label",
	      line(XCMD_TASK, "{\"title\":\"T\",\"status\":\"PARKED\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"status\":\"pending\",\"label\":\"PARKED\""));
	CHECK("task: status words without case, ' ' and '-' as '_'",
	      line(XCMD_TASK, "{\"title\":\"T\",\"status\":\"DONE\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"status\":\"done\",\"label\":\"DONE\"") &&
	      line(XCMD_TASK, "{\"title\":\"T\",\"status\":\"In Progress\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"status\":\"active\"") &&
	      line(XCMD_TASK, "{\"title\":\"T\",\"status\":\"in-progress\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"status\":\"active\"") &&
	      line(XCMD_TASK, "{\"title\":\"T\",\"status\":\"deleted\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"status\":\"dropped\"") &&
	      line(XCMD_TASK, "{\"title\":\"T\",\"status\":\"completed\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"status\":\"done\""));
	CHECK("task: a status that is no string is pending, label null",
	      line(XCMD_TASK, "{\"title\":\"T\",\"status\":5}", 0) == XCMD_ROW &&
	      strstr(out, "\"status\":\"pending\",\"label\":null"));
	CHECK("task: no id, or one that is no string or number: the row's place",
	      line(XCMD_TASK, "{\"title\":\"T\"}", 0) == XCMD_ROW &&
	      !strncmp(out, "{\"task\":{\"id\":\"3\",", 18) &&
	      line(XCMD_TASK, "{\"title\":\"T\",\"id\":true}", 0) == XCMD_ROW &&
	      !strncmp(out, "{\"task\":{\"id\":\"3\",", 18) &&
	      line(XCMD_TASK, "{\"title\":\"T\",\"id\":-1.5e3}", 0) == XCMD_ROW &&
	      !strncmp(out, "{\"task\":{\"id\":\"-1.5e3\",", 23));
	CHECK("task: no title, a blank one or one that is no string: skipped",
	      line(XCMD_TASK, "{\"id\":1}", 0) == XCMD_SKIP &&
	      line(XCMD_TASK, "{\"title\":\" \\n\\t\"}", 0) == XCMD_SKIP &&
	      line(XCMD_TASK, "{\"title\":3}", 0) == XCMD_SKIP && !*out);
	CHECK("task: a duplicated title is no title",
	      line(XCMD_TASK, "{\"title\":\"a\",\"title\":\"b\"}", 0) ==
	      XCMD_SKIP);
	CHECK("task: not an object, broken JSON, bad UTF-8: skipped",
	      line(XCMD_TASK, "[{\"title\":\"a\"}]", 0) == XCMD_SKIP &&
	      line(XCMD_TASK, "\"title\"", 0) == XCMD_SKIP &&
	      line(XCMD_TASK, "{\"title\":\"a\"", 0) == XCMD_SKIP &&
	      line(XCMD_TASK, "{\"title\":\"a\"} x", 0) == XCMD_SKIP &&
	      line(XCMD_TASK, "{\"title\":\"a\xff\"}", 0) == XCMD_SKIP &&
	      line(XCMD_TASK, "{\"title\":\"\\u0000a\"}", 0) == XCMD_SKIP);
	CHECK("task: a NUL inside the line: skipped",
	      xcmd_line(XCMD_TASK, "{\"title\":\"a\"}\0x", 15, 0, 1, out,
	                sizeof out) == XCMD_SKIP);
	CHECK("task: blanks only is blank, not skipped",
	      line(XCMD_TASK, "", 0) == XCMD_BLANK &&
	      line(XCMD_TASK, " \t\r", 1) == XCMD_BLANK);
	CHECK("task: another kind's wrapper is a row with no title",
	      line(XCMD_TASK, "{\"memory\":{\"title\":\"a\"}}", 0) == XCMD_SKIP);
	CHECK("task: the title cut like an engine title (first line, 120)",
	      line(XCMD_TASK, "{\"title\":\"\\n  one\\ttwo  \\nthree\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"title\":\"one two\""));
	big = malloc(XCMD_LINE_MAX + 64);
	strcpy(big, "{\"title\":\"");
	for (k = 0; k < 300; k++)
		strcat(big, "\xc3\xa9");
	strcat(big, "\",\"note\":\"");
	for (k = 0; k < 300; k++)
		strcat(big, "n");
	strcat(big, "\"}");
	CHECK("task: title and note cut at 120 scalars",
	      line(XCMD_TASK, big, 0) == XCMD_ROW && strstr(out, "\xc3\xa9\",") &&
	      strlen(strstr(out, "\"note\":\"")) == 8 + 120 + 1 +
	      strlen(",\"created\":null,\"updated\":null,\"chapter\":null,"
	             "\"created_chapter\":null,\"blocked_by\":[],\"url\":null,"
	             "\"group\":null}}"));
	CHECK("task: control characters and bidi marks written as \\u",
	      line(XCMD_TASK, "{\"title\":\"a\\u0001b\\u202ec\\u001bd\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"title\":\"a\\u0001b\\u202ec\\u001bd\""));
	CHECK("task: a url with a space or a control byte is null",
	      line(XCMD_TASK, "{\"title\":\"T\",\"url\":\"a b\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"url\":null") &&
	      line(XCMD_TASK, "{\"title\":\"T\",\"url\":\"a\\u0007\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"url\":null"));
	/* a line at the cap */
	memset(big, ' ', XCMD_LINE_MAX + 1);
	memcpy(big, "{\"title\":\"T\"}", 13);
	CHECK("task: a line of XCMD_LINE_MAX is read, one byte more skipped",
	      xcmd_line(XCMD_TASK, big, XCMD_LINE_MAX, 0, 1, out, sizeof out) ==
	      XCMD_ROW &&
	      xcmd_line(XCMD_TASK, big, XCMD_LINE_MAX + 1, 0, 1, out,
	                sizeof out) == XCMD_SKIP);
	/* 1, ids grow to "1", past the row line cap */
	strcpy(big, "{\"title\":\"T\",\"blocked_by\":[");
	for (k = strlen(big); k < XCMD_LINE_MAX - 8; k += 2)
		memcpy(big + k, "1,", 2);
	strcpy(big + k, "1]}");
	CHECK("task: a row line past XCMD_LINE_MAX is skipped",
	      line(XCMD_TASK, big, 0) == XCMD_SKIP && !*out);
	CHECK("task: a small out is refused, never overrun",
	      xcmd_line(XCMD_TASK, "{\"title\":\"T\"}", 13, 0, 1, out, 40) ==
	      XCMD_SKIP && !*out);
	free(big);
}

static void
t_title(void)
{
	CHECK("title: {\"title\":...} alone on the first line names the list",
	      line(XCMD_TASK, "{\"title\":\"My  list\\nx\"}", 1) == XCMD_TITLE &&
	      !strcmp(out, "My list"));
	CHECK("title: not first, it is a row",
	      line(XCMD_TASK, "{\"title\":\"My list\"}", 0) == XCMD_ROW);
	CHECK("title: with another field, it is a row",
	      line(XCMD_TASK, "{\"title\":\"a\",\"id\":1}", 1) == XCMD_ROW);
	CHECK("title: memory rows take it too",
	      line(XCMD_MEMORY, "{\"title\":\"m\"}", 1) == XCMD_TITLE);
}

static void
t_memory(void)
{
	char *big;
	size_t k;

	CHECK("memory: a bare row, every contract field, in order",
	      line(XCMD_MEMORY, "{\"id\":\"dch-stays-c\",\"title\":\"dch stays C\","
	           "\"note\":\"no rewrite\",\"kind\":\"feedback\",\"scope\":"
	           "\"project\",\"updated\":\"2026-11-10T09:00:00Z\",\"path\":"
	           "\"/m/a b.md\",\"session\":\"wrote\",\"chapter\":\"c\","
	           "\"body\":\"line1\\nline2\",\"zz\":1}", 0) == XCMD_ROW &&
	      !strcmp(out, "{\"memory\":{\"id\":\"dch-stays-c\",\"title\":"
	              "\"dch stays C\",\"note\":\"no rewrite\",\"kind\":\"feedback\","
	              "\"scope\":\"project\",\"updated\":\"2026-11-10T09:00:00Z\","
	              "\"path\":\"/m/a b.md\",\"session\":\"wrote\",\"chapter\":null,"
	              "\"body\":\"line1\\nline2\"}}"));
	CHECK("memory: wrapped, session only wrote or loaded, no body is null",
	      line(XCMD_MEMORY, "{\"memory\":{\"title\":\"t\",\"session\":\"x\"}}",
	           0) == XCMD_ROW &&
	      strstr(out, "\"session\":null,\"chapter\":null,\"body\":null}}") &&
	      line(XCMD_MEMORY, "{\"title\":\"t\",\"session\":\"loaded\"}", 0) ==
	      XCMD_ROW && strstr(out, "\"session\":\"loaded\""));
	CHECK("memory: a task wrapper is no memory row",
	      line(XCMD_MEMORY, "{\"task\":{\"title\":\"a\"}}", 0) == XCMD_SKIP);
	big = malloc(XCMD_LINE_MAX);
	strcpy(big, "{\"title\":\"t\",\"body\":\"");
	k = strlen(big);
	memset(big + k, 'b', XCMD_BODY_MAX - 1);
	k += XCMD_BODY_MAX - 1;
	memcpy(big + k, "\xc3\xa9zz\"}", 7);
	CHECK("memory: a body past 16 KB cut on a character, cut: true",
	      line(XCMD_MEMORY, big, 0) == XCMD_ROW &&
	      strstr(out, "b\",\"cut\":true}}") &&
	      strlen(strstr(out, "\"body\":\"")) ==
	      8 + XCMD_BODY_MAX - 1 + strlen("\",\"cut\":true}}"));
	free(big);
}

/* ---- the driver --------------------------------------------------------- */

static long stop_after, seen;
static int quiet;
static long long t0, first_ms = -1;

static int
row(const char *l, void *arg)
{
	(void)arg;
	if (first_ms < 0)
		first_ms = xr_now_ms() - t0;
	seen++;
	if (!quiet)
	{
		fputs(l, stdout);
		fputc('\n', stdout);
		fflush(stdout);
	}
	return stop_after && seen >= stop_after;
}

static void
tick(int s)
{
	(void)s;
}

static void
q(const char *k, const char *v)
{
	static char b[4 * XCMD_WARN_MAX];

	dch_json_quote(b, sizeof b, v);
	fprintf(stderr, ",\"%s\":%s", k, b);
}

static int
drive(int argc, char **argv)
{
	struct xcmd_ctx c = { argv[1], argc > 3 ? argv[3] : "",
	                      argc > 2 ? argv[2] : NULL,
	                      argc > 4 ? argv[4] : "", argc > 5 ? argv[5] : "" };
	struct xcmd_result r;
	struct rusage ru;
	const char *e;
	int ret, left;
	long kb;

	if ((e = getenv("XCMD_STOP_AFTER")))
		stop_after = atol(e);
	quiet = getenv("XCMD_QUIET") != NULL;
	if (getenv("XCMD_EINTR"))
	{
		struct sigaction sa;
		struct itimerval it = { { 0, 1000 }, { 0, 1000 } };

		memset(&sa, 0, sizeof sa);
		sa.sa_handler = tick;	/* no SA_RESTART: every call sees EINTR */
		sigaction(SIGALRM, &sa, NULL);
		setitimer(ITIMER_REAL, &it, NULL);
	}
	t0 = xr_now_ms();
	ret = xcmd_rows(!strcmp(argv[0], "memory") ? XCMD_MEMORY : XCMD_TASK, &c,
	                row, NULL, &r);
	{
		struct itimerval off = { { 0, 0 }, { 0, 0 } };

		setitimer(ITIMER_REAL, &off, NULL);
	}
	/* no child left unreaped: a zombie would answer here */
	left = waitpid(-1, NULL, WNOHANG) != -1 || errno != ECHILD;
	getrusage(RUSAGE_SELF, &ru);
	kb = (long)ru.ru_maxrss;
#ifdef __APPLE__
	kb /= 1024;
#endif
	fprintf(stderr, "{\"ret\":%d,\"rows\":%lu,\"skipped\":%lu,\"ran\":%d,"
	        "\"stopped\":%d,\"code\":%d,\"seen\":%ld,\"first_ms\":%lld,"
	        "\"total_ms\":%lld,\"maxrss_kb\":%ld,\"children\":%d", ret, r.rows,
	        r.skipped, r.ran, r.stopped, r.code, seen, first_ms,
	        xr_now_ms() - t0, kb, left);
	q("err", r.err);
	q("title", r.title);
	q("warn", r.warn);
	fprintf(stderr, "}\n");
	return 0;
}

int
main(int argc, char **argv)
{
	if (argc >= 4 && !strcmp(argv[1], "run"))
		return drive(argc - 2, argv + 2);
	t_argv();
	t_task();
	t_title();
	t_memory();
	printf(fails ? "%d FAILED\n" : "all ok\n", fails);
	return fails != 0;
}
