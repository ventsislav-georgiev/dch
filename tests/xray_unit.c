/* Units for the X-ray engine, xray.c (#120, XR1b): titles, the project key,
** the identity shapes, the Claude decoder's turn rules and caps, the bounded
** JSONL reader, the shell chapter machine and the output cap; refs (#135):
** the grammar rows, --ref's parse and value rule, the index's lifetime and
** caps (keys, spans, definitions, the dropped set) and the config_dir check;
** --chapter (#156): the anchor rule and a chapter's entries; resolvers
** (#159, XR6): the file rules and every state, git's argv, env, probe,
** states, timeout and group kill through a fake git.
**
**   cc -I. -o xray_unit tests/xray_unit.c xray.c json.c && ./xray_unit
*/
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "xray.h"
#include "ring.h"

static int fails;

void dch_trace(const char *fmt, ...);
void
dch_trace(const char *fmt, ...)
{
	(void)fmt;
}

#define CHECK(msg, cond) do { if (cond) printf("  ok   %s\n", msg); \
	else { printf("  FAIL %s (line %d)\n", msg, __LINE__); fails++; } } while (0)

static const char *
title(const char *s)
{
	static char b[XR_TITLE_MAX * 4 + 1];

	xr_title(s, strlen(s), b, sizeof b);
	return b;
}

static size_t
scalars(const char *s)
{
	size_t n = 0;

	for (; *s; s++)
		n += ((unsigned char)*s & 0xc0) != 0x80;
	return n;
}

static void
t_title(void)
{
	char big[1000];
	int k;

	CHECK("title: first non-blank line, runs collapsed",
	      !strcmp(title("\n  \n  fix   the\tbuild  \nsecond"), "fix the build"));
	CHECK("title: Unicode spaces collapse (NBSP, U+3000)",
	      !strcmp(title("a\xc2\xa0\xc2\xa0" "b\xe3\x80\x80" "c"), "a b c"));
	CHECK("title: invalid UTF-8 is U+FFFD",
	      !strcmp(title("a\xff" "b"), "a\xef\xbf\xbd" "b"));
	CHECK("title: blank is empty", !strcmp(title(" \n\t "), ""));
	for (k = 0; k < 140; k++)
		memcpy(big + k * 3, "e\xcc\x81", 3);	/* e + U+0301 */
	big[420] = '\0';
	CHECK("title: cut at 120 scalars, not graphemes or bytes",
	      scalars(title(big)) == 120 && strlen(title(big)) == 180);
}

static void
t_key(void)
{
	char out[400], p[300];

	xr_project_key("/home/demo/proj", out, sizeof out);
	CHECK("key: /home/demo/proj", !strcmp(out, "-home-demo-proj"));
	xr_project_key("/home/d\xc3\xa9/\xf0\x9f\x98\x80", out, sizeof out);
	CHECK("key: one '-' per UTF-16 unit (BMP 1, astral 2)",
	      !strcmp(out, "-home-d----"));
	memcpy(p, "/home/", 6);
	memset(p + 6, 'x', 250);
	p[256] = '\0';
	xr_project_key(p, out, sizeof out);
	CHECK("key: past 200 units, 200 + '-' + base36(|java hash|)",
	      strlen(out) == 207 && !strcmp(out + 195, "xxxxx-rs2lzz"));
}

static void
t_ids(void)
{
	CHECK("id: m + 16 lowercase hex", xr_id_shaped("m0123456789abcdef"));
	CHECK("id: uppercase hex is not", !xr_id_shaped("m0123456789ABCDEF"));
	CHECK("id: 15 or 17 hex is not", !xr_id_shaped("m0123456789abcde") &&
	      !xr_id_shaped("m0123456789abcdef0"));
	CHECK("id: other first letter is not", !xr_id_shaped("n0123456789abcdef"));
	CHECK("label id: padding trimmed as the listing shows it",
	      xr_label_id_shaped("  m0123456789abcdef", 19) &&
	      xr_label_id_shaped("m0123456789abcdef \n", 19) &&
	      xr_label_id_shaped("\tm0123456789abcdef\r", 19));
	CHECK("label id: only the first n bytes, and inner text is a label",
	      xr_label_id_shaped("m0123456789abcdefXX", 17) &&
	      !xr_label_id_shaped("m0123456789abcdef x", 19) &&
	      !xr_label_id_shaped("   ", 3) && !xr_label_id_shaped("", 0));
	CHECK("uuid form", xr_uuid_form("0b1f6a52-4c1e-4d7a-9a51-2f6d8c3e7a10"));
	CHECK("uuid form: uppercase is a name",
	      !xr_uuid_form("0B1F6A52-4C1E-4D7A-9A51-2F6D8C3E7A10"));
	CHECK("record form", xr_record_form("demo.1794300000.json"));
	CHECK("record form: no '/', no '..', not bare",
	      !xr_record_form("a/b.json") && !xr_record_form("../x.json") &&
	      !xr_record_form(".json") && !xr_record_form("demo"));
	CHECK("config: absolute only", xr_config_ok("/home/demo/.claude") &&
	      !xr_config_ok("rel/.claude") && !xr_config_ok("/a/../b") &&
	      !xr_config_ok(""));
}

/* ---- Claude --------------------------------------------------------------- */

static struct xr_claude *D;
static uint64_t OFF;

static void
line(const char *s)
{
	char *b = strdup(s);

	xr_claude_line(D, b, strlen(b), OFF, 0);
	OFF += strlen(b) + 1;
	free(b);
}

static void
begin(void)
{
	D = xr_claude_new();
	OFF = 0;
}

static void
end(struct xr_chapters *c)
{
	xr_claude_end(D, 0xabc, c);
	xr_claude_free(D);
}

#define U(t) "{\"type\":\"user\",\"message\":{\"content\":\"" t "\"}}"
#define A(t) "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":" \
             "\"text\",\"text\":\"" t "\"}]}}"

/* iso_time through a chapter's `at`: offsets are exactly [+-]HH:MM; a
** short form is unparsed (-1), never read past the string's end (ASan) */
static long long
at_of(const char *ts)
{
	char l[256];
	struct xr_chapters c;
	long long at;

	snprintf(l, sizeof l, "{\"type\":\"user\",\"message\":{\"content\":"
	         "\"hi\"},\"timestamp\":\"%s\"}", ts);
	begin();
	line(l);
	end(&c);
	at = c.n ? xr_chapter_at(&c, 0)->at : -2;
	xr_chapters_free(&c);
	return at;
}

static void
t_time(void)
{
	CHECK("time: Z", at_of("2026-01-02T03:04:05Z") == 1767323045);
	CHECK("time: fraction and +01:00",
	      at_of("2026-01-02T03:04:05.678+01:00") == 1767323045 - 3600);
	CHECK("time: -02:30", at_of("2026-01-02T03:04:05-02:30") ==
	      1767323045 + 9000);
	CHECK("time: short offsets are unparsed (+1:2, +01:0, +01, -)",
	      at_of("2026-01-02T03:04:05+1:2") == -1 &&
	      at_of("2026-01-02T03:04:05+01:0") == -1 &&
	      at_of("2026-01-02T03:04:05+01") == -1 &&
	      at_of("2026-01-02T03:04:05-") == -1);
}

static void
t_claude(void)
{
	struct xr_chapters c;
	char b[300];
	int k;

	begin();
	line("{\"type\":null,\"entryType\":\"user\",\"message\":{\"content\":"
	     "\"alias\"}}");
	line(A("x"));
	line("{\"type\":\"assistant\",\"type\":\"user\",\"message\":{\"content\":"
	     "\"last key wins\"}}");
	line(A("x"));
	line("{\"type\":\"user\",\"message\":{\"content\":\"<pasted_content "
	     "id=\\\"p1\\\">\\npasted words\\n</pasted_content id=\\\"p1\\\">\"}}");
	line(A("x"));
	line(U("<command-name>/x</command-name> trailing words"));
	end(&c);
	CHECK("claude: a null key falls to the next alias",
	      c.n == 4 && !strcmp(xr_chapter_at(&c, 0)->title, "alias"));
	CHECK("claude: the last duplicate key wins",
	      !strcmp(xr_chapter_at(&c, 1)->title, "last key wins"));
	CHECK("claude: pasted content unwrapped",
	      !strcmp(xr_chapter_at(&c, 2)->title, "pasted words"));
	CHECK("claude: a command with other text is a prompt",
	      !strcmp(xr_chapter_at(&c, 3)->kind, "prompt"));
	xr_chapters_free(&c);

	/* QA 1.44 MED-3: unwrap stops once the title is settled; the title
	** is what a whole unwrap gives: an unclosed opener stays text, a
	** later closed one is still unwrapped, blank pastes before it go */
	begin();
	line(U("<pasted_content id=\\\"b\\\">\\n \\n</pasted_content "
	       "id=\\\"b\\\">\\n<pasted_content id=\\\"a\\\">"
	       "<pasted_content id=\\\"c\\\">C</pasted_content "
	       "id=\\\"c\\\"> tail\\nnext line"));
	end(&c);
	CHECK("claude: unwrap: an unclosed opener is text, a closed one after "
	      "it unwrapped",
	      c.n == 1 && !strcmp(xr_chapter_at(&c, 0)->title,
	                          "<pasted_content id=\"a\">C tail"));
	xr_chapters_free(&c);
	{
		char *big = malloc(40000 * 30 + 64);
		size_t k2 = 0;

		if (big)
		{
			k2 = (size_t)sprintf(big, "{\"type\":\"user\",\"message\":"
			                     "{\"content\":\"");
			for (k = 0; k < 40000; k++)
				k2 += (size_t)sprintf(big + k2,
				                      "<pasted_content id=\\\"a%d\\\">", k);
			memcpy(big + k2, "\"}}", 4);
			begin();
			line(big);
			end(&c);
			CHECK("claude: unwrap: 40000 unclosed openers cut at 120 "
			      "scalars",
			      c.n == 1 && !strncmp(xr_chapter_at(&c, 0)->title,
			                           "<pasted_content id=\"a0\">"
			                           "<pasted_content id=\"a1\">", 48) &&
			      strlen(xr_chapter_at(&c, 0)->title) == XR_TITLE_MAX);
			xr_chapters_free(&c);
			free(big);
		}
	}

	/* the 256 open tool calls: an evicted Grep's error result is an
	** orphan (an error); a tracked Grep's is quiet */
	begin();
	line(U("evicted"));
	line("{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":"
	     "\"tool_use\",\"id\":\"g0\",\"name\":\"Grep\",\"input\":"
	     "{\"pattern\":\"x\"}}]}}");
	for (k = 0; k < XR_PENDING; k++)
	{
		snprintf(b, sizeof b, "{\"type\":\"assistant\",\"message\":"
		         "{\"content\":[{\"type\":\"tool_use\",\"id\":\"t%d\","
		         "\"name\":\"Bash\",\"input\":{}}]}}", k);
		line(b);
	}
	line("{\"type\":\"user\",\"message\":{\"content\":[{\"type\":"
	     "\"tool_result\",\"tool_use_id\":\"g0\",\"is_error\":true}]}}");
	line(U("tracked"));
	line("{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":"
	     "\"tool_use\",\"id\":\"g1\",\"name\":\"Grep\",\"input\":"
	     "{\"pattern\":\"x\"}}]}}");
	line("{\"type\":\"user\",\"message\":{\"content\":[{\"type\":"
	     "\"tool_result\",\"tool_use_id\":\"g1\",\"is_error\":true}]}}");
	end(&c);
	CHECK("claude: past 256 open calls the oldest is dropped (orphan error)",
	      c.n == 2 && xr_chapter_at(&c, 0)->error);
	CHECK("claude: a tracked Grep with a pattern errors quietly",
	      c.n == 2 && !xr_chapter_at(&c, 1)->error);
	xr_chapters_free(&c);

	/* NIT-2 (QA 1.45.1): a short id made to look like a long one's key
	** (0xff + its FNV-1a) never reaches id_key: a raw 0xff is not UTF-8,
	** so its whole line is refused and the long Grep stays quiet */
	{
		char lid[100], fake[20];
		unsigned long long h = 0xcbf29ce484222325ULL;
		const unsigned char *u;

		memset(lid, 'L', 80);
		lid[80] = '\0';
		for (u = (const unsigned char *)lid; *u; u++)
			h = (h ^ *u) * 0x100000001b3ULL;
		snprintf(fake, sizeof fake, "\xff%016llx", h);
		begin();
		line(U("long"));
		snprintf(b, sizeof b, "{\"type\":\"assistant\",\"message\":"
		         "{\"content\":[{\"type\":\"tool_use\",\"id\":\"%s\","
		         "\"name\":\"Grep\",\"input\":{\"pattern\":\"x\"}}]}}",
		         lid);
		line(b);
		line(U("crafted"));
		snprintf(b, sizeof b, "{\"type\":\"user\",\"message\":{"
		         "\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":"
		         "\"%s\",\"is_error\":true}]}}", fake);
		line(b);
		end(&c);
		CHECK("claude: a crafted 0xff id never pairs with a long id's key",
		      c.n == 2 && !xr_chapter_at(&c, 0)->error &&
		      !xr_chapter_at(&c, 1)->error);
		xr_chapters_free(&c);
	}

	/* an oversized line is a failed step: here it opens a partial */
	begin();
	xr_claude_line(D, "", 0, 0, 1);
	OFF = 2000000;
	line(U("after"));
	end(&c);
	CHECK("claude: an oversized line is a step (partial first chapter)",
	      c.n == 2 && xr_chapter_at(&c, 0)->partial &&
	      !strcmp(xr_chapter_at(&c, 1)->title, "after") &&
	      xr_chapter_at(&c, 1)->offset == 2000000 &&
	      !strcmp(xr_chapter_at(&c, 1)->anchor, "fabc:2000000"));
	xr_chapters_free(&c);

	/* past XR_LINE_TOKENS a line is a failed step, as an oversized one;
	** commas inside a string count for nothing */
	{
		size_t n = 2 * XR_LINE_TOKENS + 256, k2;
		char *big = malloc(n + 64);

		if (big)
		{
			k2 = (size_t)snprintf(big, n, "{\"type\":\"user\",\"message\":"
			                     "{\"content\":\"big\"},\"a\":[");
			while (k2 < 2 * XR_LINE_TOKENS + 200)
				big[k2++] = '0', big[k2++] = ',';
			memcpy(big + k2, "0]}", 4);
			begin();
			line(big);
			line(U("after"));
			end(&c);
			CHECK("claude: a line past XR_LINE_TOKENS is a failed step",
			      c.n == 2 && xr_chapter_at(&c, 0)->partial &&
			      !strcmp(xr_chapter_at(&c, 1)->title, "after"));
			xr_chapters_free(&c);
			k2 = (size_t)snprintf(big, n, "{\"type\":\"user\",\"message\":"
			                     "{\"content\":\"x");
			while (k2 < 2 * XR_LINE_TOKENS + 200)
				big[k2++] = ',';
			memcpy(big + k2, "\"}}", 4);
			begin();
			line(big);
			end(&c);
			CHECK("claude: commas in a string are no tokens",
			      c.n == 1 && !xr_chapter_at(&c, 0)->partial &&
			      xr_chapter_at(&c, 0)->title[0] == 'x');
			xr_chapters_free(&c);
			free(big);
		}
	}

	/* 5000 chapters: the newest kept, total counts all */
	begin();
	for (k = 0; k < XR_CHAPTERS + 100; k++)
	{
		snprintf(b, sizeof b, U("p%d"), k);
		line(b);
		line(A("x"));
	}
	end(&c);
	CHECK("claude: 5000 chapters kept, the newest, total counts all",
	      c.n == XR_CHAPTERS && c.total == XR_CHAPTERS + 100 &&
	      !strcmp(xr_chapter_at(&c, 0)->title, "p100"));
	xr_chapters_free(&c);
}

/* the 3.8 range over a sparse file: head 16 MB + the newest 256 MB */
static void
t_read(void)
{
	char path[] = "/tmp/xray_unit.XXXXXX";
	const char head[] = U("head prompt") "\n" A("x") "\n";
	const char tail[] = "partial line\n" U("tail prompt") "\n";
	uint64_t size = XR_HEAD_RANGE + XR_TAIL_RANGE + (8u << 20);
	struct xr_chapters c;
	int fd = mkstemp(path), capped = 0, rc;

	if (fd < 0)
	{
		CHECK("read: temp file", 0);
		return;
	}
	unlink(path);
	(void)!write(fd, head, sizeof head - 1);
	(void)!pwrite(fd, tail, sizeof tail - 1,
	              (off_t)(size - (sizeof tail - 1)));
	D = xr_claude_new();
	rc = xr_claude_read(D, fd, size, &capped);
	xr_claude_end(D, 1, &c);
	xr_claude_free(D);
	close(fd);
	CHECK("read: past the range, capped", rc == 0 && capped);
	CHECK("read: the head range and the tail range both read",
	      c.n == 2 && !strcmp(xr_chapter_at(&c, 0)->title, "head prompt") &&
	      !strcmp(xr_chapter_at(&c, 1)->title, "tail prompt"));
	xr_chapters_free(&c);

	/* QA 1.44 LOW-5: the file shrank under the read (it is shorter than
	** the size the lookup saw): what was read stands, capped */
	char path2[] = "/tmp/xray_unit.XXXXXX";

	fd = mkstemp(path2);
	if (fd < 0)
	{
		CHECK("read: shrink temp file", 0);
		return;
	}
	unlink(path2);
	(void)!write(fd, head, sizeof head - 1);
	D = xr_claude_new();
	rc = xr_claude_read(D, fd, sizeof head - 1 + 4096, &capped);
	xr_claude_end(D, 1, &c);
	xr_claude_free(D);
	close(fd);
	CHECK("read: a file that shrank mid-read is capped (complete:false)",
	      rc == 0 && capped && c.n == 1 &&
	      !strcmp(xr_chapter_at(&c, 0)->title, "head prompt"));
	xr_chapters_free(&c);
}

/* ---- shell ---------------------------------------------------------------- */

static struct xr_rec R[64];
static size_t RN;

static void
rec(int kind, int flags, const char *text)
{
	R[RN].seq = 100 + RN;
	R[RN].kind = kind;
	R[RN].flags = flags;
	R[RN].t = 1000 + (uint32_t)RN;
	R[RN].text = text;
	R[RN].len = strlen(text);
	RN++;
}

static int walked;
static uint64_t walk_seq;

static void
walk_one(void *ctx, const struct xr_rec *x)
{
	(void)ctx;
	walk_seq = x->seq;
	walked++;
}

/* dch.c's reply walker: whole replies only (F2) */
static void
t_walk(void)
{
	/* two records: "ab" (output) and "" (mark) */
	static const unsigned char b[] = {2, 0, 3, 0, 1, 0, 0, 0, 'a', 'b',
	                                  0, 0, 4, 0, 2, 0, 0, 0};

	walked = 0;
	CHECK("walk: a whole reply is every byte, seqs from first",
	      xr_ring_walk(b, sizeof b, 50, 2, walk_one, NULL) == (long long)
	      sizeof b && walked == 2 && walk_seq == 51);
	walked = 0;
	CHECK("walk: a record past the reply is torn, never read",
	      xr_ring_walk(b, sizeof b - 1, 50, 2, walk_one, NULL) == -1 &&
	      walked == 1);
	CHECK("walk: fewer records than count is torn",
	      xr_ring_walk(b, 10, 50, 2, walk_one, NULL) == -1);
	CHECK("walk: bytes past count's records are torn",
	      xr_ring_walk(b, sizeof b, 50, 1, walk_one, NULL) == -1);
	CHECK("walk: a length past the buffer is torn",
	      xr_ring_walk((const unsigned char *)"\xff\xff\3\0\0\0\0\0", 8,
	                   1, 1, walk_one, NULL) == -1);
	CHECK("walk: an empty reply of count 0 is whole",
	      xr_ring_walk(b, 0, 1, 0, walk_one, NULL) == 0);
}

static void
t_shell(void)
{
	struct xr_chapters c;
	int paired, alt;

	RN = 0;
	rec(RING_K_MARK, 0, "A");
	rec(RING_K_PROMPT, 0, "$");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "ls");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_MARK, RING_F_EXIT, "D;3");
	rec(RING_K_MARK, 0, "A");		/* forged: C without input */
	rec(RING_K_PROMPT, 0, "$");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "cat notes");
	rec(RING_K_MARK, 0, "C");
	rec(RING_K_MARK, RING_F_EXIT, "D;0");
	xr_shell_chapters(R, RN, &c, &paired, &alt);
	CHECK("shell: anchored on the prompt record, error on D exit != 0",
	      c.n == 1 && xr_chapter_at(&c, 0)->offset == 101 &&
	      !strcmp(xr_chapter_at(&c, 0)->anchor, "s101") &&
	      xr_chapter_at(&c, 0)->at == 1001 && xr_chapter_at(&c, 0)->error);
	CHECK("shell: a forged block (no input flag on C) is no chapter",
	      c.n == 1 && paired && !alt);
	xr_chapters_free(&c);

	RN = 0;
	rec(RING_K_MARK, 0, "A");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "make");
	rec(RING_K_GAP, 0, "slice");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_MARK, RING_F_ALT, "A");
	xr_shell_chapters(R, RN, &c, &paired, &alt);
	CHECK("shell: a gap orphans its block; no prompt record: the A's seq",
	      c.n == 0 && !paired && alt);
	xr_chapters_free(&c);
	RN = 0;
	rec(RING_K_MARK, RING_F_ALT, "A");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "make");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_MARK, 0, "A");
	xr_shell_chapters(R, RN, &c, &paired, &alt);
	CHECK("shell: no prompt record anchors on the A; the next primary A "
	      "clears alt", c.n == 1 && xr_chapter_at(&c, 0)->offset == 100 &&
	      !alt);
	xr_chapters_free(&c);

	/* record text is not NUL-terminated: "D;0" followed by the next
	** record's bytes ("5...") is exit 0, never 5 */
	RN = 0;
	rec(RING_K_MARK, 0, "A");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "true");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_MARK, RING_F_EXIT, "D;05");
	R[RN - 1].len = 3;
	xr_shell_chapters(R, RN, &c, &paired, &alt);
	CHECK("shell: the D exit is read within the record's length",
	      c.n == 1 && !xr_chapter_at(&c, 0)->error);
	xr_chapters_free(&c);

	/* a B or a C inside the open C..D orphans that block, as a forged C
	** does: its chapter goes and it pairs nothing; the block before stays */
	RN = 0;
	rec(RING_K_MARK, 0, "A");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "make");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_MARK, 0, "B");
	xr_shell_chapters(R, RN, &c, &paired, &alt);
	CHECK("shell: a B inside C..D orphans the block (no chapter, unpaired)",
	      c.n == 0 && c.total == 0 && !paired);
	xr_chapters_free(&c);
	RN = 0;
	rec(RING_K_MARK, 0, "A");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "ls");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_MARK, 0, "D;0");
	rec(RING_K_MARK, 0, "A");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "make");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_MARK, RING_F_EXIT, "D;2");
	xr_shell_chapters(R, RN, &c, &paired, &alt);
	CHECK("shell: a C inside C..D orphans only that block",
	      c.n == 1 && c.total == 1 && paired &&
	      !strcmp(xr_chapter_at(&c, 0)->title, "ls") &&
	      !xr_chapter_at(&c, 0)->error);
	xr_chapters_free(&c);
}

/* ---- the output cap ------------------------------------------------------- */

static void
t_print(void)
{
	struct xr_chapters c = {0};
	struct xr_answer a;
	char *buf = NULL;
	size_t bl = 0, k;
	FILE *f;

	c.v = calloc(XR_CHAPTERS, sizeof *c.v);
	if (!c.v)
		return;
	for (k = 0; k < XR_CHAPTERS; k++)
	{
		struct xr_chapter *x = &c.v[k];

		x->offset = k;
		x->at = -1;
		x->kind = "prompt";
		snprintf(x->anchor, sizeof x->anchor, "f1:%zu", k);
		memset(x->title, 1, 200);	/* 200 x \u0001: 1200 bytes */
	}
	c.n = c.total = XR_CHAPTERS;
	memset(&a, 0, sizeof a);
	a.session = a.session_id = "s";
	a.corpus = "claude";
	a.project = a.project_label = "";
	a.available = a.complete = 1;
	a.lines = -1;
	a.ch = &c;
	f = open_memstream(&buf, &bl);
	CHECK("print: written", f && xr_print(f, &a) == 0);
	if (f)
		fclose(f);
	CHECK("print: under 4 MiB, one line", buf && bl <= XR_OUT_MAX &&
	      bl > XR_OUT_MAX - 8192 && !memchr(buf, '\n', bl - 1));
	CHECK("print: the oldest went, why output",
	      buf && strstr(buf, "\"complete\":false,\"why\":\"output\"") &&
	      strstr(buf, "\"anchor\":\"f1:4999\"") &&
	      !strstr(buf, "\"anchor\":\"f1:0\","));
	free(buf);
	xr_chapters_free(&c);
}

/* ---- refs (XR2a, #135) --------------------------------------------------- */

struct keys
{
	char b[2048];
	size_t n;
};

static int
key_cat(void *ctx, const struct xr_span *sp)
{
	struct keys *k = ctx;

	k->n += (size_t)snprintf(k->b + k->n, sizeof k->b - k->n, "%s%s",
	                         k->n ? " " : "", sp->key);
	return k->n >= sizeof k->b - 1;
}

/* every span's key in s, space-joined */
static const char *
spans(const char *s, int mode)
{
	static struct keys k;

	k.n = 0;
	k.b[0] = '\0';
	(void)xr_grammar(s, strlen(s), mode, "/home/demo/proj", key_cat, &k);
	return k.b;
}

/* the grammar rows the mutants turn on (v2.1 2.2-2.4, 9.7) */
static void
t_grammar(void)
{
	static const struct { const char *s; int mode; const char *want; } g[] = {
		/* mutant 6: Unicode boundaries, not ASCII ones */
		{ "#831\xc3\xa9", XR_PROSE, "" },
		{ "\xc3\xa9#838", XR_PROSE, "" },
		{ "\xe4\xb8\xad#840", XR_PROSE, "" },
		{ "#841\xe3\x80\x82", XR_PROSE, "issue:#841" },
		{ "PROJ-1\xc3\xa9", XR_PROSE, "" },
		/* mutant 16: the ticket stoplist */
		{ "UTF-8 SHA-256 ISO-8601 PROJ-12", XR_PROSE, "ticket:PROJ-12" },
		/* mutant 15: H and T are prose rules, never code ones */
		{ "#831 PROJ-12", XR_CODE, "" },
		{ "#831", XR_PROSE, "issue:#831" },
		{ "see `#831` and #832", XR_PROSE, "issue:#832" },
		{ "o/r#7 https://x.org/y", XR_CODE, "pull:o/r#7 url:https://x.org/y" },
		{ "src/a.c:3 and src/b.c", XR_CODE, "file:/home/demo/proj/src/a.c" },
		{ "x #831_", XR_PROSE, "" },
		/* L2: r has R's left guard, no repo#n tail of a path or host */
		{ "x/acme/widgets#12", XR_PROSE, "" },
		{ "example.com/page#12", XR_PROSE, "" },
		{ "widgets#12", XR_PROSE, "issue:widgets#12" },
		/* Q3 (v3.2.2): r's repo is 3+ chars, ends alnum, not a stopword */
		{ "a#12", XR_PROSE, "" },
		{ "PR#831", XR_PROSE, "" },
		{ "C#10", XR_PROSE, "" },
		{ "issue#12", XR_PROSE, "" },
		{ "PKCS#12", XR_PROSE, "" },
		{ "Steps#4 tests#5", XR_PROSE, "" },
		{ "abc#831", XR_PROSE, "issue:abc#831" },
		{ "pre-#831", XR_PROSE, "issue:#831" },
		{ "post-#20338 x_#12", XR_PROSE, "issue:#20338" },
		{ "issuer#12", XR_PROSE, "issue:issuer#12" },
		/* Q4 (v3.2.2): ~/ kept as written, folded lexically, .. at ~
		** dropped; ~user/ is no path */
		{ "~/demo/../x.md", XR_PROSE, "file:~/x.md" },
		{ "~/../x.md", XR_PROSE, "file:~/x.md" },
		{ "~/a/./b//c.md", XR_PROSE, "file:~/a/b/c.md" },
		{ "~user/x.md", XR_PROSE, "" },
		{ "`~user/x.md`", XR_PROSE, "" },
	};
	size_t k;

	for (k = 0; k < sizeof g / sizeof g[0]; k++)
	{
		const char *got = spans(g[k].s, g[k].mode);
		char m[160];

		snprintf(m, sizeof m, "grammar: %s in %s -> [%s]", g[k].s,
		         g[k].mode == XR_CODE ? "code" : "prose", g[k].want);
		CHECK(m, !strcmp(got, g[k].want));
		if (strcmp(got, g[k].want))
			printf("       got [%s]\n", got);
	}
}

/* Q15: the progress write checks its flush, so a reader gone before it
** is a cancel at that write, not at the one after (review M2, mutant R5) */
static void
t_cancel(void)
{
	int fd[2];
	FILE *f;
	struct xr_scan *x;

	signal(SIGPIPE, SIG_IGN);
	if (pipe(fd) || !(f = fdopen(fd[1], "w")))
	{
		CHECK("cancel: a pipe", 0);
		return;
	}
	/* t0 a second ago: the first tick is due a progress line */
	x = xr_scan_new(f, "issue:#831", "s", "", 0, 1, 0, 300,
	                xr_now_ms() - 1000);
	CHECK("cancel: first written", x && xr_scan_first(x, "id", XR_ISSUE,
	                                                   "#831") == 0);
	close(fd[0]);
	if (x)
		(void)xr_scan_tick(x, 1);
	CHECK("cancel: EPIPE at the progress write is a cancel (1)",
	      x && xr_scan_cancelled(x) == 1);
	CHECK("cancel: no done after it", x && xr_scan_done(x) != 0);
	xr_scan_free(x);
	fclose(f);
}

/* #149: a scan's lines from hits fed straight to its sink; the output in
** *out (freed by the caller), the scan's done rc in *rc. */
static size_t
scan_run(int full, int shell, int ndefs, int ntitles, int nment,
         size_t pad, char **out, int *rc)
{
	struct xr_span sp;
	struct xr_hit h;
	struct xr_sink k;
	struct xr_scan *x;
	char line[2048], title_[64], *b = NULL;
	size_t bl = 0;
	FILE *f = open_memstream(&b, &bl);
	int i;

	*rc = -1;
	*out = NULL;
	if (!f || !xr_ref_parse("#831", NULL, &sp) ||
	    !(x = xr_scan_new(f, sp.key, "s", "p", 1, full, shell, 300,
	                      xr_now_ms())))
	{
		if (f)
			fclose(f);
		free(b);
		return 0;
	}
	(void)xr_scan_first(x, "id", sp.shape, xr_span_text(&sp));
	xr_scan_sink(x, &k);
	memset(line, 'w', pad);
	memcpy(line, "#831 ", 5);
	line[pad] = '\0';
	sp.s = 0;
	sp.e = 4;
	for (i = 0; i < ndefs + nment; i++)
	{
		memset(&h, 0, sizeof h);
		h.sp = &sp;
		h.line = line;
		h.ll = pad;
		h.off = h.unit = h.row = (uint64_t)i + 1;
		h.at = -1;
		if (i < ndefs)
		{
			snprintf(title_, sizeof title_, "title %d", i % ntitles);
			h.title = title_;
			h.passage = line;
			h.rule = "D1";
		}
		k.hit(k.ctx, &h);
	}
	xr_scan_file(x, 1, 0, 0, 0);
	*rc = xr_scan_done(x);
	xr_scan_free(x);
	fclose(f);
	*out = b;
	return bl;
}

static int
count_of(const char *s, const char *needle)
{
	int n = 0;

	for (; (s = strstr(s, needle)); s++)
		n++;
	return n;
}

/* #149: the --ref stream's caps (3.4): first and the tally always go,
** definition lines once per title and at most 8, the mention lines the
** 200-per-file bound left out counted, and a shell's scan ticked. */
static void
t_scan_caps(void)
{
	size_t out0 = xr_lim.out, n;
	char *o, *d;
	int rc;
	struct xr_scan *x;

	n = scan_run(1, 1, 21, 4, 0, 40, &o, &rc);
	CHECK("scan: 21 definitions of 4 titles write 4 lines",
	      o && rc == 0 && count_of(o, "\"definition\"") == 4 &&
	      strstr(o, "\"definitions\":4,"));
	free(o);
	n = scan_run(1, 1, 12, 12, 0, 40, &o, &rc);
	CHECK("scan: 12 distinct titles write 8 lines",
	      o && rc == 0 && count_of(o, "\"definition\"") == 8);
	free(o);
	n = scan_run(1, 0, 0, 1, 300, 40, &o, &rc);
	CHECK("scan: 300 mention lines write 200, tally mentions_dropped 100",
	      o && rc == 0 && count_of(o, "\"mention\"") == 200 &&
	      strstr(o, "\"count\":300,") &&
	      strstr(o, "\"mentions_dropped\":100}") &&
	      strstr(o, "\"partial\":false,\"counts\""));
	free(o);
	/* the cap reached by held mentions: first ... tally, done */
	xr_lim.out = 64 << 10;
	n = scan_run(1, 1, 8, 8, 400, 1000, &o, &rc);
	d = o ? strstr(o, "{\"schema\":1,\"done\"") : NULL;
	CHECK("scan: past the cap, first, then the tally, then done "
	      "why output, all under the cap",
	      o && rc == 0 && n <= xr_lim.out &&
	      !strncmp(o, "{\"schema\":1,\"first\"", 19) &&
	      strstr(o, "\"tally\"") && d && strstr(o, "\"tally\"") < d &&
	      strstr(d, "\"why\":\"output\"") && d[strlen(d) - 1] == '\n' &&
	      !strchr(d, '\n')[1]);
	free(o);
	xr_lim.out = out0;

	/* the shell's tick (LOW-4): budget, 10 s, a first result left to its
	** row */
	x = xr_scan_new(stdout, "issue:#831", "s", "", 1, 1, 1, 300,
	                xr_now_ms() - 10001);
	CHECK("scan: a full shell scan past 10 s stops",
	      x && xr_scan_tick(x, 1) != 0);
	xr_scan_free(x);
	x = xr_scan_new(stdout, "issue:#831", "s", "", 1, 0, 1, 50,
	                xr_now_ms() - 60);
	CHECK("scan: a light shell scan past its budget stops",
	      x && xr_scan_tick(x, 1) != 0);
	xr_scan_free(x);
	(void)n;
}

/* #155: one hit of #831 to k (a definition when title is set) */
static void
hit831(const struct xr_sink *k, uint64_t off, const char *title)
{
	static const char line[] = "#831 here";
	struct xr_span sp;
	struct xr_hit h;

	if (!xr_ref_parse("#831", NULL, &sp))
		return;
	sp.s = 0;
	sp.e = 4;
	memset(&h, 0, sizeof h);
	h.sp = &sp;
	h.line = line;
	h.ll = sizeof line - 1;
	h.off = h.unit = h.row = off;
	h.at = -1;
	h.chapter = "f1:0";
	if (title)
	{
		h.title = title;
		h.passage = "a passage";
		h.rule = "D1";
	}
	k->hit(k->ctx, &h);
}

/* #155: --scope project|all over several files (3.4): <n>'s own results
** never stop a light scan, another session's first does, with no passage,
** no mention and chapter null; the counts per scope; the files and bytes
** caps stop before the next file with their why. */
static void
t_scan_scope(void)
{
	struct xr_limits l0 = xr_lim;
	struct xr_scan *x;
	struct xr_sink k;
	char *b = NULL;
	size_t bl = 0;
	FILE *f = open_memstream(&b, &bl);

	x = f ? xr_scan_new(f, "issue:#831", "n", "p", 1, 0, 0, 300,
	                    xr_now_ms()) : NULL;
	if (!x)
	{
		CHECK("scope: a scan", 0);
		if (f)
			fclose(f);
		free(b);
		return;
	}
	xr_scan_scope(x, 1);
	(void)xr_scan_first(x, "id", XR_ISSUE, "#831");
	xr_scan_sink(x, &k);
	hit831(&k, 1, NULL);
	CHECK("scope: light, <n>'s own result does not stop it",
	      xr_scan_tick(x, 10) == 0);
	xr_scan_file(x, 10, 0, 0, 0);
	CHECK("scope: the next file is read", xr_scan_next(x, "o", "p", 0, 1) == 0);
	hit831(&k, 1, "Title");
	hit831(&k, 1, NULL);
	CHECK("scope: another session's first result stops it",
	      xr_scan_tick(x, 10) != 0);
	xr_scan_file(x, 10, 0, 0, 0);
	CHECK("scope: no file after the stop", xr_scan_next(x, "o2", "p", 0, 1));
	(void)xr_scan_done(x);
	xr_scan_free(x);
	fclose(f);
	CHECK("scope: first says project; <n>'s line carries its chapter",
	      b && strstr(b, "\"scope\":\"project\"") &&
	      strstr(b, "\"mention\":{\"session\":\"n\",\"chapter\":\"f1:0\""));
	CHECK("scope: light, another session's definition has no passage, "
	      "chapter null, and no mention line",
	      b && strstr(b, "\"definition\":{\"session\":\"o\"") &&
	      !strstr(b, "\"passage\"") && strstr(b, "\"chapter\":null") &&
	      !strstr(b, "\"mention\":{\"session\":\"o\""));
	CHECK("scope: done why first, counts per scope, all null",
	      b && strstr(b, "\"why\":\"first\"") &&
	      strstr(b, "\"counts\":{\"session\":1,\"project\":1,"
	                "\"project_sessions\":1,\"all\":null,"
	                "\"all_sessions\":null}"));
	free(b);

	/* full, all: the files cap is checked before the next file opens */
	b = NULL;
	f = open_memstream(&b, &bl);
	xr_lim.files = 3;
	x = f ? xr_scan_new(f, "issue:#831", "n", "p", 1, 1, 0, 300,
	                    xr_now_ms()) : NULL;
	if (x)
	{
		xr_scan_scope(x, 2);
		(void)xr_scan_first(x, "id", XR_ISSUE, "#831");
		xr_scan_sink(x, &k);
		xr_scan_file(x, 10, 0, 0, 0);
		CHECK("scope: file 2 of 3", xr_scan_next(x, "a", "p", 0, 1) == 0);
		hit831(&k, 1, NULL);
		hit831(&k, 2, NULL);
		xr_scan_file(x, 10, 0, 0, 0);
		CHECK("scope: file 3 of 3", xr_scan_next(x, "b", "q", 0, 0) == 0);
		hit831(&k, 1, "Other");
		hit831(&k, 1, NULL);
		xr_scan_file(x, 10, 0, 0, 0);
		CHECK("scope: a 4th file is not opened (64 in production)",
		      xr_scan_next(x, "c", "q", 0, 0) != 0);
		(void)xr_scan_done(x);
		xr_scan_free(x);
	}
	fclose(f);
	CHECK("scope: full, other sessions' lines carry the passage",
	      b && strstr(b, "\"passage\":\"a passage\"") &&
	      strstr(b, "\"mention\":{\"session\":\"a\",\"chapter\":null"));
	CHECK("scope: done why files, all counted",
	      b && strstr(b, "\"partial\":true") && strstr(b, "\"why\":\"files\"") &&
	      strstr(b, "\"counts\":{\"session\":0,\"project\":2,"
	                "\"project_sessions\":1,\"all\":1,\"all_sessions\":1}"));
	free(b);
	xr_lim = l0;

	/* the bytes cap: mid-file at the tick, and before the next file */
	xr_lim.bytes = 100;
	f = fopen("/dev/null", "w");
	x = f ? xr_scan_new(f, "issue:#831", "n", "p", 1, 1, 0, 300,
	                    xr_now_ms()) : NULL;
	CHECK("scope: past the bytes cap the tick stops",
	      x && xr_scan_tick(x, 50) == 0 && xr_scan_tick(x, 101) != 0);
	xr_scan_free(x);
	x = f ? xr_scan_new(f, "issue:#831", "n", "p", 1, 1, 0, 300,
	                    xr_now_ms()) : NULL;
	if (x)
		xr_scan_file(x, 100, 0, 0, 0);
	CHECK("scope: a used-up bytes cap opens no next file",
	      x && xr_scan_next(x, "a", "p", 0, 1) != 0);
	xr_scan_free(x);
	if (f)
		fclose(f);
	xr_lim = l0;
}

static void
t_ref(void)
{
	struct xr_span sp;
	char big[300];

	CHECK("ref: #831 parses to issue:#831",
	      xr_ref_parse("#831", NULL, &sp) && !strcmp(sp.key, "issue:#831") &&
	      !strcmp(xr_span_text(&sp), "#831"));
	CHECK("ref: surrounding blanks are trimmed",
	      xr_ref_parse("  o/r#7 ", NULL, &sp) && !strcmp(sp.key, "pull:o/r#7"));
	CHECK("ref: a file ref keys on the cwd, the line dropped",
	      xr_ref_parse("src/main.c:12", "/w", &sp) &&
	      !strcmp(sp.key, "file:/w/src/main.c"));
	CHECK("ref: a relative file ref still parses with no cwd (dch's shape "
	      "check; #148 drops such keys only in the corpus)",
	      xr_ref_parse("src/main.c:12", NULL, &sp) &&
	      !strcmp(sp.key, "file:src/main.c"));
	CHECK("ref: ~user is no path, .. at ~ dropped (Q4, #148 LOW-6)",
	      !xr_ref_parse("~bob/x.c:3", "/w", &sp) &&
	      xr_ref_parse("~/../x.c:3", "/w", &sp) &&
	      !strcmp(sp.key, "file:~/x.c"));
	CHECK("ref: two refs, or text around one, are no ref",
	      !xr_ref_parse("#831 #832", NULL, &sp) &&
	      !xr_ref_parse("see #831", NULL, &sp) &&
	      !xr_ref_parse("UTF-8", NULL, &sp) && !xr_ref_parse("", NULL, &sp));
	/* README's number lengths (#150 NIT-1): #n 2-6, repo#n 1-7, ticket
	** 1-6, a GitHub URL 1-9; a number keys as written */
	CHECK("ref: number lengths as README says (#150)",
	      !xr_ref_parse("#5", NULL, &sp) && !xr_ref_parse("#0", NULL, &sp) &&
	      xr_ref_parse("#123456", NULL, &sp) &&
	      !xr_ref_parse("#1234567", NULL, &sp) &&
	      xr_ref_parse("#00012", NULL, &sp) &&
	      !strcmp(sp.key, "issue:#00012") &&
	      xr_ref_parse("o/r#0", NULL, &sp) && !strcmp(sp.key, "pull:o/r#0") &&
	      xr_ref_parse("dch#1234567", NULL, &sp) &&
	      !xr_ref_parse("dch#12345678", NULL, &sp) &&
	      xr_ref_parse("PROJ-123456", NULL, &sp) &&
	      !xr_ref_parse("PROJ-1234567", NULL, &sp) &&
	      xr_ref_parse("https://github.com/o/r/pull/123456789", NULL, &sp) &&
	      !strcmp(sp.key, "pull:o/r#123456789") &&
	      xr_ref_parse("https://github.com/o/r/pull/1234567890", NULL, &sp) &&
	      sp.shape == XR_URL);
	/* 2.3's U is validated like Prosper's pullURL: host exactly
	** github.com (#150 NIT-8, spec over QA) */
	CHECK("ref: a GitHub.com URL stays a url",
	      xr_ref_parse("https://GitHub.com/o/r/pull/7", NULL, &sp) &&
	      sp.shape == XR_URL);
	memset(big, 'a', sizeof big);
	big[256] = '\0';
	CHECK("ref value: 256 bytes ok, 257 refused", xr_ref_ok(big) &&
	      (big[256] = 'a', big[257] = '\0', !xr_ref_ok(big)));
	CHECK("ref value: empty, C0, DEL, C1 and bidi controls refused",
	      !xr_ref_ok("") && !xr_ref_ok("a\x01") && !xr_ref_ok("a\x7f") &&
	      !xr_ref_ok("a\xc2\x85") && !xr_ref_ok("a\xe2\x80\xae") &&
	      !xr_ref_ok("a\xe2\x81\xa6") && !xr_ref_ok("a\xe2\x80\x8f") &&
	      !xr_ref_ok("a\xff"));
	CHECK("ref value: a dash-led value is accepted", xr_ref_ok("-831"));
	/* 3.1 lists the bidi controls; U+061C is not one of them (#150 NIT-5),
	** so it is no "bad ref", only no ref */
	CHECK("ref value: U+061C passes the value check, then is no ref",
	      xr_ref_ok("#831\xd8\x9c") && !xr_ref_parse("#831\xd8\x9c", NULL, &sp));
}

/* K3 (mutant 3's unit half): an index is counted live until freed */
static void
t_index(void)
{
	unsigned long l0 = xr_index_live;
	struct xr_index *a = xr_index_new(), *b = xr_index_new();

	CHECK("index: two live", a && b && xr_index_live == l0 + 2);
	xr_index_free(a);
	xr_index_free(b);
	xr_index_free(NULL);
	CHECK("index: none live after free", xr_index_live == l0);
}

/* ---- the caps (3.2, 3.8; review #135 H1, M1, M4, L1) ---------------------- */

struct cnt
{
	size_t m, d;
};

static void
cnt_hit(void *ctx, const struct xr_hit *h)
{
	struct cnt *c = ctx;

	if (h->title)
		c->d++;
	else
		c->m++;
}

/* records through the shell's refs into k */
static void
shell_refs(const struct xr_rec *r, size_t n, const struct xr_sink *k)
{
	struct xr_chapters c;
	struct xr_shell s;
	size_t i;

	xr_shell_begin(&s, &c);
	if (xr_shell_refs(&s, k, "/w") == 0)
		for (i = 0; i < n; i++)
			xr_shell_feed(&s, &r[i]);
	xr_shell_end(&s);
	xr_chapters_free(&c);
}

/* one paired block (cat notes.md, D;0) whose output is the rows of out */
static size_t
block(struct xr_rec *r, char *out)
{
	static const struct { int kind, flags; const char *t; } h[] = {
		{ RING_K_MARK, 0, "A" }, { RING_K_PROMPT, 0, "$" },
		{ RING_K_MARK, 0, "B" }, { RING_K_INPUT, 0, "cat notes.md" },
		{ RING_K_MARK, RING_F_INPUT, "C" },
	};
	size_t n = 0, k;
	char *l, *nl;

	for (k = 0; k < sizeof h / sizeof h[0]; k++, n++)
	{
		r[n].seq = n + 1;
		r[n].kind = h[k].kind;
		r[n].flags = h[k].flags;
		r[n].t = 1000;
		r[n].text = h[k].t;
		r[n].len = strlen(h[k].t);
	}
	for (l = out; *l; l = nl + 1, n++)
	{
		nl = strchr(l, '\n');
		r[n].seq = n + 1;
		r[n].kind = RING_K_OUTPUT;
		r[n].flags = 0;
		r[n].t = 1000;
		r[n].text = l;
		r[n].len = (size_t)(nl - l);
	}
	r[n].seq = n + 1;
	r[n].kind = RING_K_MARK;
	r[n].flags = RING_F_EXIT;
	r[n].t = 1000;
	r[n].text = "D;0";
	r[n].len = 3;
	return n + 1;
}

/* the definitions of one block of n heading rows "# #<base + k % keys> t<k>" */
static struct cnt
headings(size_t n, int keys, long long *ms)
{
	struct xr_rec *r = calloc(n + 8, sizeof *r);
	char *out = malloc(n * 32 + 1), *o = out;
	struct cnt c = {0, 0};
	struct xr_sink k = { cnt_hit, &c };
	long long t0;
	size_t i;

	if (!r || !out)
	{
		free(r);
		free(out);
		c.d = (size_t)-1;
		return c;
	}
	for (i = 0; i < n; i++)
		o += sprintf(o, "# #%d t%zu\n", 831 + (int)(i % (size_t)keys), i);
	t0 = xr_now_ms();
	shell_refs(r, block(r, out), &k);
	*ms = xr_now_ms() - t0;
	free(r);
	free(out);
	return c;
}

static void
t_caps(void)
{
	/* ASan and UBSan slow the scan many times over: the times are
	** reported, not held to a budget, under a sanitizer */
	int timed = !getenv("ASAN_OPTIONS");
	struct cnt c;
	struct xr_sink k = { cnt_hit, &c };
	struct xr_rec r1;
	char *row = malloc(70000), *b;
	struct xr_rec *rows;
	struct xr_index *ix;
	struct xr_answer a;
	size_t i, bl, o;
	long long ms, t0;
	FILE *f;
	char m[160];

	if (!row)
		return;
	/* 200 shas on one shell line: 128 mentions (3.8) */
	for (i = o = 0; i < 200; i++)
		o += (size_t)sprintf(row + o, "a%06zx ", i);
	memset(&r1, 0, sizeof r1);
	r1.seq = 1;
	r1.kind = RING_K_OUTPUT;
	r1.text = row;
	r1.len = o;
	memset(&c, 0, sizeof c);
	shell_refs(&r1, 1, &k);
	CHECK("caps: 200 shas on one shell line give 128 mentions",
	      c.m == XR_SPANS && c.d == 0);

	/* 2 MiB of 64 KiB sha rows: linear, well under 1 s (M1) */
	for (i = o = 0; o + 8 < 65000; i++)
		o += (size_t)sprintf(row + o, "a%06zx ", i);
	rows = calloc(32, sizeof *rows);
	if (rows)
	{
		for (i = 0; i < 32; i++)
		{
			rows[i].seq = i + 1;
			rows[i].kind = RING_K_OUTPUT;
			rows[i].text = row;
			rows[i].len = o;
		}
		memset(&c, 0, sizeof c);
		t0 = xr_now_ms();
		shell_refs(rows, 32, &k);
		ms = xr_now_ms() - t0;
		snprintf(m, sizeof m, "caps: 32 x 64 KiB sha rows: 128 mentions a "
		         "row, %lld ms (< 1000)", ms);
		CHECK(m, c.m == 32 * XR_SPANS && (!timed || ms < 1000));
		free(rows);
	}

	/* 9 D1 headings of one key: 8 definitions; 4 keys: a listing (#4) */
	c = headings(9, 1, &ms);
	CHECK("caps: 9 headings of one key give 8 definitions",
	      c.d == XR_DEFS);
	c = headings(3, 3, &ms);
	CHECK("caps: 3 distinct headings are 3 definitions", c.d == 3);
	c = headings(4, 4, &ms);
	CHECK("caps: 4 distinct headings are a listing, 0 definitions",
	      c.d == 0);
	/* H1: a 256 KB block of headings is O(n), not O(n^2) */
	c = headings(32000, 1, &ms);
	snprintf(m, sizeof m, "caps: 32000 heading rows of one key: 8 "
	         "definitions, %lld ms (< 1000)", ms);
	CHECK(m, c.d == XR_DEFS && (!timed || ms < 1000));
	c = headings(32000, 32000, &ms);
	snprintf(m, sizeof m, "caps: 32000 distinct heading rows: a listing, "
	         "%lld ms (< 1000)", ms);
	CHECK(m, c.d == 0 && (!timed || ms < 1000));

	/* 2100 distinct shas over a Claude transcript: 2000 keys, 100 dropped */
	if ((ix = xr_index_new()))
	{
		struct xr_chapters ch;

		xr_index_sink(ix, &k);
		begin();
		xr_claude_refs(D, &k, 1, NULL, NULL);
		for (i = 0; i < 21; i++)
		{
			size_t j;

			o = (size_t)sprintf(row, "{\"type\":\"user\",\"message\":"
			                    "{\"content\":\"");
			for (j = 0; j < 100; j++)
				o += (size_t)sprintf(row + o, "a%06zx ", i * 100 + j);
			strcpy(row + o, "\"}}");
			line(row);
		}
		end(&ch);
		memset(&a, 0, sizeof a);
		a.session = a.session_id = "s";
		a.corpus = "claude";
		a.project = a.project_label = "";
		a.available = a.complete = 1;
		a.lines = -1;
		a.ch = &ch;
		a.refs = ix;
		b = NULL;
		f = open_memstream(&b, &bl);
		CHECK("caps: 2100 distinct shas give 2000 keys, dropped_keys 100",
		      xr_index_keys(ix) == XR_KEYS && f && xr_print(f, &a) == 0 &&
		      fclose(f) == 0 && strstr(b, "\"dropped_keys\":100}"));
		free(b);
		xr_chapters_free(&ch);
		xr_index_free(ix);
	}

	/* a Bash command is clipped like any text field: 6000 scalars */
	{
		static const char pre[] = "{\"type\":\"assistant\",\"message\":"
		                          "{\"content\":[{\"type\":\"tool_use\","
		                          "\"id\":\"b1\",\"name\":\"Bash\","
		                          "\"input\":{\"command\":\"o/r#1 ";
		struct xr_chapters ch;

		o = strlen(pre);
		memcpy(row, pre, o);
		memset(row + o, 'x', 6000);
		o += 6000;
		strcpy(row + o, " o/r#2\"}}]}}");
		memset(&c, 0, sizeof c);
		k.hit = cnt_hit;
		k.ctx = &c;
		begin();
		xr_claude_refs(D, &k, 1, NULL, NULL);
		line(row);
		end(&ch);
		xr_chapters_free(&ch);
		CHECK("caps: a Bash command's ref past 6000 scalars is not scanned",
		      c.m == 1);
	}

	/* L1: past 32768 distinct dropped keys the count is a lower bound,
	** and the set stays fast: 320 rows of 128 new shas, 40960 keys */
	if ((ix = xr_index_new()) && (rows = calloc(320, sizeof *rows)))
	{
		char *big = malloc(320 * 1040);

		xr_index_sink(ix, &k);
		for (i = 0; big && i < 320; i++)
		{
			size_t j;
			char *p = big + i * 1040;

			for (j = o = 0; j < 128; j++)
				o += (size_t)sprintf(p + o, "a%06zx ", i * 128 + j);
			rows[i].seq = i + 1;
			rows[i].kind = RING_K_OUTPUT;
			rows[i].text = p;
			rows[i].len = o;
		}
		t0 = xr_now_ms();
		if (big)
			shell_refs(rows, 320, &k);
		ms = xr_now_ms() - t0;
		memset(&a, 0, sizeof a);
		a.session = a.session_id = "s";
		a.corpus = "shell";
		a.project = a.project_label = "";
		a.available = a.complete = 1;
		a.lines = -1;
		a.refs = ix;
		b = NULL;
		f = open_memstream(&b, &bl);
		snprintf(m, sizeof m, "caps: 40960 keys: 2000 kept, dropped_keys "
		         "the lower bound 32768, %lld ms (< 1000)", ms);
		CHECK(m, big && xr_index_keys(ix) == XR_KEYS && f &&
		      xr_print(f, &a) == 0 && fclose(f) == 0 &&
		      strstr(b, "\"dropped_keys\":32768}") && (!timed || ms < 1000));
		free(b);
		free(big);
		free(rows);
	}
	xr_index_free(ix);
	free(row);
}

/* ---- Codex (7.4, K9) ---------------------------------------------------- */

struct cxkeys
{
	char b[4096];
	size_t d;
};

static void
key_hit(void *ctx, const struct xr_hit *h)
{
	struct cxkeys *k = ctx;
	size_t n = strlen(k->b);

	if (h->title)
		k->d++;
	snprintf(k->b + n, sizeof k->b - n, "%s|", h->sp->key);
}

#define CXI(p) "{\"timestamp\":\"2026-11-10T10:00:00.000Z\",\"type\":" \
               "\"response_item\",\"payload\":" p "}"

/* the decoder: user turns are chapters (Codex's own parts left out), tool
** fields and assistant text mentions; developer, reasoning, events and
** unknown types nothing; never a definition */
static void
t_codex(void)
{
	struct cxkeys k = { "", 0 };
	struct xr_sink sk = { key_hit, &k };
	struct xr_chapters c;

	begin();
	xr_claude_codex(D);
	xr_claude_refs(D, &sk, 1, NULL, NULL);
	line("{\"timestamp\":\"2026-11-10T10:00:00.000Z\",\"type\":"
	     "\"session_meta\",\"payload\":{\"id\":\"x\",\"cwd\":\"/w\"}}");
	line(CXI("{\"type\":\"message\",\"role\":\"developer\",\"content\":"
	         "[{\"type\":\"input_text\",\"text\":\"see devonly.c\"}]}"));
	line(CXI("{\"type\":\"message\",\"role\":\"user\",\"content\":["
	         "{\"type\":\"input_text\",\"text\":\"# AGENTS.md instructions "
	         "for /w\\n<INSTRUCTIONS>agents.c</INSTRUCTIONS>\"},"
	         "{\"type\":\"input_text\",\"text\":\"<environment_context>"
	         "<cwd>/w</cwd></environment_context>\"}]}"));
	line(CXI("{\"type\":\"message\",\"role\":\"user\",\"content\":["
	         "{\"type\":\"input_text\",\"text\":\"<image name=[Image #1]>\"},"
	         "{\"type\":\"input_image\",\"image_url\":\"data:x\"},"
	         "{\"type\":\"input_text\",\"text\":\"</image>\"},"
	         "{\"type\":\"input_text\",\"text\":\"fix src/main.c \"},"
	         "{\"type\":\"input_text\",\"text\":\"please\"}]}"));
	line("{\"type\":\"event_msg\",\"payload\":{\"type\":\"user_message\","
	     "\"message\":\"fix src/main.c please\"}}");
	line(CXI("{\"type\":\"reasoning\",\"summary\":[{\"type\":"
	         "\"summary_text\",\"text\":\"src/reason.c:1\"}]}"));
	line(CXI("{\"type\":\"function_call\",\"name\":\"exec_command\","
	         "\"arguments\":\"{\\\"cmd\\\":\\\"cat src/args.c:3\\\"}\"}"));
	line(CXI("{\"type\":\"function_call_output\",\"output\":"
	         "\"src/out.c:9: warning\"}"));
	line(CXI("{\"type\":\"custom_tool_call\",\"name\":\"exec\",\"input\":"
	         "\"sed -n 1p src/custom.c:4\"}"));
	line(CXI("{\"type\":\"custom_tool_call_output\",\"output\":[{\"type\":"
	         "\"input_text\",\"text\":\"src/arr.c:2\"}]}"));
	line(CXI("{\"type\":\"message\",\"role\":\"assistant\",\"content\":["
	         "{\"type\":\"output_text\",\"text\":\"done in src/util.c\"}]}"));
	line(CXI("{\"type\":\"some_new_item\",\"text\":\"src/unknown.c:1\"}"));
	line("{\"type\":\"world_state\",\"payload\":{\"text\":\"src/ws.c:1\"}}");
	line("{\"type\":\"compacted\",\"payload\":{\"message\":\"summary of "
	     "src/comp.c\"}}");
	line("not json src/bad.c");
	line(CXI("{\"type\":\"message\",\"role\":\"user\",\"content\":["
	         "{\"type\":\"input_text\",\"text\":\"second turn\"}]}"));
	CHECK("codex: the session's cwd", !strcmp(xr_claude_cwd(D), "/w"));
	end(&c);
	CHECK("codex: chapters are the typed turns and the compaction",
	      c.n == 3 && !strcmp(xr_chapter_at(&c, 0)->title,
	                          "fix src/main.c please") &&
	      !strcmp(xr_chapter_at(&c, 1)->kind, "compact") &&
	      !strcmp(xr_chapter_at(&c, 2)->title, "second turn"));
	CHECK("codex: a chapter's time is its line's",
	      c.n && xr_chapter_at(&c, 0)->at == 1794304800);
	CHECK("codex: tool fields, outputs and assistant text mention",
	      strstr(k.b, "main.c") && strstr(k.b, "args.c") &&
	      strstr(k.b, "out.c") && strstr(k.b, "custom.c") &&
	      strstr(k.b, "arr.c") && strstr(k.b, "util.c") &&
	      strstr(k.b, "comp.c"));
	CHECK("codex: Codex's own parts, developer text, reasoning, events, "
	      "unknown and bad lines give no ref",
	      !strstr(k.b, "agents.c") && !strstr(k.b, "devonly.c") &&
	      !strstr(k.b, "reason.c") && !strstr(k.b, "unknown.c") &&
	      !strstr(k.b, "ws.c") && !strstr(k.b, "bad.c"));
	CHECK("codex: the user turn once (its event_msg twin is not read)",
	      strstr(k.b, "main.c") && !strstr(strstr(k.b, "main.c") + 1,
	                                       "main.c"));
	CHECK("codex: mentions only, never a definition", k.d == 0);
	xr_chapters_free(&c);
}

static void
mkfile(const char *p)
{
	close(open(p, O_WRONLY | O_CREAT, 0600));
}

/* the lookup (7.4, mutant 36): names only, depth 3, newest first, at most
** XR_CODEX_DAYS day dirs; exactly one regular file, never a link */
static void
t_codex_lookup(void)
{
	char d[] = "/tmp/xray_cx.XXXXXX", p[512], q[512], path[1100], cwd[1100];
	const char *tid = "019a0000-1111-7222-8333-444455556666";
	struct stat st;
	int m, day;

	if (!mkdtemp(d))
	{
		CHECK("codex lookup: temp dir", 0);
		return;
	}
	snprintf(p, sizeof p, "%s/sessions", d);
	mkdir(p, 0700);
	snprintf(p, sizeof p, "%s/sessions/2000", d);
	mkdir(p, 0700);
	/* 401 day dirs: 2000/01..40 x 01..10, and 2000/41/01 */
	for (m = 1; m <= 41; m++)
	{
		snprintf(p, sizeof p, "%s/sessions/2000/%02d", d, m);
		mkdir(p, 0700);
		for (day = 1; day <= (m == 41 ? 1 : 10); day++)
		{
			snprintf(p, sizeof p, "%s/sessions/2000/%02d/%02d", d, m, day);
			mkdir(p, 0700);
		}
	}
	snprintf(p, sizeof p, "%s/sessions/2000/01/01/rollout-2000-01-01T00-00"
	         "-00-%s.jsonl", d, tid);
	mkfile(p);
	CHECK("codex lookup: the 401st newest day dir is past the bound",
	      xr_codex_lookup(d, tid, path, sizeof path, &st) < 0);
	unlink(p);
	snprintf(p, sizeof p, "%s/sessions/2000/01/02/rollout-2000-01-02T00-00"
	         "-00-%s.jsonl", d, tid);
	mkfile(p);
	CHECK("codex lookup: the 400th newest is read, its path whole",
	      xr_codex_lookup(d, tid, path, sizeof path, &st) == 0 &&
	      !strcmp(path, p) && S_ISREG(st.st_mode));
	CHECK("codex lookup: another id's file is no hit",
	      xr_codex_lookup(d, "019a0000-1111-7222-8333-444455556667", path,
	                      sizeof path, &st) < 0);
	CHECK("codex lookup: a non-UUID id is refused",
	      xr_codex_lookup(d, "../../x", path, sizeof path, &st) < 0);
	snprintf(q, sizeof q, "%s/sessions/../", d);
	CHECK("codex lookup: a CODEX_HOME with .. is refused",
	      xr_codex_lookup(q, tid, path, sizeof path, &st) < 0);
	if (getcwd(cwd, sizeof cwd) && chdir(d) == 0)
	{
		CHECK("codex lookup: a relative CODEX_HOME is refused",
		      xr_codex_lookup(".", tid, path, sizeof path, &st) < 0);
		(void)!chdir(cwd);
	}
	/* two files for one id: none, never the newest */
	snprintf(q, sizeof q, "%s/sessions/2000/41/01/rollout-2000-02-11T00-00"
	         "-00-%s.jsonl", d, tid);
	mkfile(q);
	CHECK("codex lookup: two rollouts for one id are no hit",
	      xr_codex_lookup(d, tid, path, sizeof path, &st) < 0);
	unlink(q);
	/* a symlink in place of the file, or of a day dir: refused */
	(void)!symlink(p, q);
	unlink(p);
	CHECK("codex lookup: a symlinked rollout is refused",
	      xr_codex_lookup(d, tid, path, sizeof path, &st) < 0);
	unlink(q);
	snprintf(q, sizeof q, "%s/real", d);
	mkdir(q, 0700);
	snprintf(path, sizeof path, "%s/real/rollout-x-%s.jsonl", d, tid);
	mkfile(path);
	snprintf(p, sizeof p, "%s/sessions/2000/41/02", d);
	(void)!symlink(q, p);
	CHECK("codex lookup: a symlinked day dir is not entered",
	      xr_codex_lookup(d, tid, cwd, sizeof cwd, &st) < 0);
	unlink(p);
	unlink(path);
	rmdir(q);
	snprintf(p, sizeof p, "rm -rf '%s'", d);
	(void)!system(p);
}

/* mutant 8: config_dir is validated before any readdir (7.2) */
static void
t_lookup(void)
{
	char d[] = "/tmp/xray_look.XXXXXX", p[256], q[256], path[1100];
	char proj[256], cwd[1100];
	const char *sid = "11111111-2222-4333-8444-555555555555";
	struct stat st;

	if (!mkdtemp(d))
	{
		CHECK("lookup: temp dir", 0);
		return;
	}
	snprintf(p, sizeof p, "%s/projects", d);
	mkdir(p, 0700);
	snprintf(p, sizeof p, "%s/projects/-w", d);
	mkdir(p, 0700);
	snprintf(p, sizeof p, "%s/projects/-w/%s.jsonl", d, sid);
	close(open(p, O_WRONLY | O_CREAT, 0600));
	CHECK("lookup: the absolute config_dir finds the transcript",
	      xr_claude_lookup(d, sid, path, sizeof path, &st, proj,
	                       sizeof proj) == 0 && !strcmp(proj, "-w"));
	snprintf(q, sizeof q, "%s/projects/../", d);
	CHECK("lookup: a config_dir with .. is refused (same dir)",
	      !xr_config_ok(q) && xr_claude_lookup(q, sid, path, sizeof path,
	                                           &st, proj, sizeof proj) < 0);
	if (getcwd(cwd, sizeof cwd) && chdir(d) == 0)
	{
		CHECK("lookup: a relative config_dir is refused",
		      !xr_config_ok(".") &&
		      xr_claude_lookup(".", sid, path, sizeof path, &st, proj,
		                       sizeof proj) < 0);
		(void)!chdir(cwd);
	}
	CHECK("lookup: a non-UUID sid is refused",
	      xr_claude_lookup(d, "../x", path, sizeof path, &st, proj,
	                       sizeof proj) < 0);
	CHECK("lookup: a name with .. inside it is fine",
	      xr_config_ok("/a/b..c/"));
	unlink(p);
	snprintf(p, sizeof p, "%s/projects/-w", d);
	rmdir(p);
	snprintf(p, sizeof p, "%s/projects", d);
	rmdir(p);
	rmdir(d);
}

/* the open seam (8.1): every open counted, refusals closed unread */
static void
t_seam(void)
{
	char d[] = "/tmp/xray_seam.XXXXXX", a[64], b[64], l[64];
	struct stat sa, sb;
	unsigned long o0, r0;
	int fd;

	if (!mkdtemp(d))
	{
		CHECK("seam: temp dir", 0);
		return;
	}
	snprintf(a, sizeof a, "%s/a.jsonl", d);
	snprintf(b, sizeof b, "%s/b.jsonl", d);
	snprintf(l, sizeof l, "%s/l.jsonl", d);
	close(open(a, O_WRONLY | O_CREAT, 0600));
	close(open(b, O_WRONLY | O_CREAT, 0600));
	(void)!symlink(a, l);
	stat(a, &sa);
	stat(b, &sb);
	o0 = xr_seam_opened;
	r0 = xr_seam_refused;
	fd = xr_seam_open(a, &sa);
	CHECK("seam: the looked-up file opens, counted",
	      fd >= 0 && xr_seam_opened == o0 + 1 && xr_seam_refused == r0);
	if (fd >= 0)
		close(fd);
	CHECK("seam: another file than the lookup's (dev, ino) is refused",
	      xr_seam_open(a, &sb) < 0 && xr_seam_refused == r0 + 1);
	CHECK("seam: a symlink is refused (O_NOFOLLOW)",
	      xr_seam_open(l, &sa) < 0 && xr_seam_refused == r0 + 2);
	CHECK("seam: every call counted, opened or refused",
	      xr_seam_opened + xr_seam_refused == o0 + r0 + 3);
	unlink(a);
	unlink(b);
	unlink(l);
	rmdir(d);
}

/* ---- --chapter (3.5; #156) ----------------------------------------------- */

#define TU(id, name, in) "{\"type\":\"assistant\",\"message\":{\"content\":" \
	"[{\"type\":\"tool_use\",\"id\":\"" id "\",\"name\":\"" name "\"," \
	"\"input\":" in "}]}}"
#define TR(id, err, t) "{\"type\":\"user\",\"message\":{\"content\":[{" \
	"\"type\":\"tool_result\",\"tool_use_id\":\"" id "\",\"is_error\":" \
	err ",\"content\":\"" t "\"}]}}"

/* one decode of l with the wanted anchor, as dch.c runs it */
static void
ch_run(const char *const *l, int n, const char *anchor, size_t out,
       struct xr_chapters *c, struct xr_entries *e)
{
	int k;

	xr_entries_begin(e, anchor, "s", out);
	begin();
	xr_claude_entries(D, e, 0xabc);
	for (k = 0; k < n; k++)
		line(l[k]);
	end(c);
}

static const char *
ch_buf(const struct xr_entries *e)
{
	return e->buf ? e->buf : "";
}

static void
t_chapter(void)
{
	static const char *const L[] = {
		U("first"),
		TU("t1", "Bash", "{\"command\":\"ls\"}"),
		A("one"),
		U("second"),
		TR("t1", "false", "late"),	/* the call's chapter, not this */
		TU("g1", "Grep", "{\"pattern\":\"x\"}"),
		TR("g1", "true", "none"),	/* a quiet tool: no error */
		TR("zz", "true", "orphan"),	/* no call: this chapter's */
		TU("f1", "Foo", "{\"b\":\"2\",\"a\":{\"x\":1}}"),
		"{\"type\":\"assistant\",\"isSidechain\":true,\"agentId\":\"a1\","
		"\"message\":{\"content\":\"thread\"}}",
		U("<command-name>/clear</command-name><command-args>all"
		  "</command-args>"),
	};
	static char big[5000], ok[130];
	struct xr_chapters c;
	struct xr_entries e;
	char a1[48], a2[48];
	char *mb = NULL;
	size_t ml = 0;
	FILE *f;
	int k;

	/* the value rule (3.1): [A-Za-z0-9:_.-], 1..128 bytes */
	memset(ok, 'a', 128);
	CHECK("anchor: charset and 1..128 bytes",
	      xr_anchor_ok("f1a:0") && xr_anchor_ok("s12") &&
	      xr_anchor_ok("A-Z_a.z:9") && xr_anchor_ok("-") && xr_anchor_ok(ok));
	ok[128] = 'a';
	CHECK("anchor: empty, 129 bytes, a space, '/', UTF-8, a newline are "
	      "not", !xr_anchor_ok("") && !xr_anchor_ok(ok) &&
	      !xr_anchor_ok("a b") && !xr_anchor_ok("a/b") &&
	      !xr_anchor_ok("\xc3\xa9") && !xr_anchor_ok("a\n"));

	ch_run(L, (int)(sizeof L / sizeof *L), "-", 1 << 20, &c, &e);
	CHECK("chapter: three chapters", c.n == 3);
	snprintf(a1, sizeof a1, "%s", c.n ? xr_chapter_at(&c, 0)->anchor : "");
	snprintf(a2, sizeof a2, "%s", c.n > 1 ? xr_chapter_at(&c, 1)->anchor : "");
	CHECK("chapter: an anchor in no chapter hits nothing, keeps nothing",
	      !e.hit && !e.len && !e.truncated);
	xr_chapters_free(&c);
	xr_entries_free(&e);

	ch_run(L, (int)(sizeof L / sizeof *L), a1, 1 << 20, &c, &e);
	CHECK("chapter: the first: its prompt, its call, its text, and the "
	      "result paired back from the next chapter",
	      e.hit && e.ord == 0 && !strcmp(ch_buf(&e),
	      "{\"role\":\"user\",\"tool\":null,\"text\":\"first\",\"error\":false},"
	      "{\"role\":\"tool\",\"tool\":\"Bash\",\"text\":\"ls\",\"error\":false},"
	      "{\"role\":\"assistant\",\"tool\":null,\"text\":\"one\","
	      "\"error\":false},"
	      "{\"role\":\"output\",\"tool\":\"Bash\",\"text\":\"late\","
	      "\"error\":false}"));
	xr_chapters_free(&c);
	xr_entries_free(&e);

	ch_run(L, (int)(sizeof L / sizeof *L), a2, 1 << 20, &c, &e);
	CHECK("chapter: the second: a quiet tool's error is none, an orphan "
	      "is tool null, a card is its sorted keys, no subagent text",
	      e.hit && e.ord == 1 && !strcmp(ch_buf(&e),
	      "{\"role\":\"user\",\"tool\":null,\"text\":\"second\","
	      "\"error\":false},"
	      "{\"role\":\"tool\",\"tool\":\"Grep\",\"text\":\"pattern: x\","
	      "\"error\":false},"
	      "{\"role\":\"output\",\"tool\":\"Grep\",\"text\":\"none\","
	      "\"error\":false},"
	      "{\"role\":\"output\",\"tool\":null,\"text\":\"orphan\","
	      "\"error\":true},"
	      "{\"role\":\"tool\",\"tool\":\"Foo\",\"text\":\"a: {\\\"x\\\":1}\\n"
	      "b: 2\",\"error\":false}"));
	CHECK("chapter: the second's anchor is the index's",
	      c.n == 3 && !strcmp(xr_chapter_at(&c, 1)->anchor, a2));
	xr_chapters_free(&c);
	xr_entries_free(&e);

	ch_run(L, (int)(sizeof L / sizeof *L), "fabc:", 1 << 20, &c, &e);
	snprintf(a1, sizeof a1, "%s", c.n == 3 ? xr_chapter_at(&c, 2)->anchor
	                                       : "");
	xr_chapters_free(&c);
	xr_entries_free(&e);
	ch_run(L, (int)(sizeof L / sizeof *L), a1, 1 << 20, &c, &e);
	CHECK("chapter: a slash command is /name args", e.hit &&
	      !strcmp(ch_buf(&e), "{\"role\":\"user\",\"tool\":null,\"text\":"
	              "\"/clear all\",\"error\":false}"));
	xr_chapters_free(&c);
	xr_entries_free(&e);
	snprintf(a1, sizeof a1, "%s", "fabc:0");

	/* K14.2 (mutant 10): an offset, inode or form not exactly the index's
	** is no chapter, never a seek */
	{
		static const char *const bad[] = {"fabc:1", "fabd:0", "fabc:00",
		                                  "fABC:0", "f0abc:0", "s0"};

		for (k = 0; k < (int)(sizeof bad / sizeof *bad); k++)
		{
			ch_run(L, (int)(sizeof L / sizeof *L), bad[k], 1 << 20, &c, &e);
			CHECK(bad[k], !e.hit && !e.len);
			xr_chapters_free(&c);
			xr_entries_free(&e);
		}
	}

	/* the object cap: entries cut whole, truncated set, nothing after */
	ch_run(L, (int)(sizeof L / sizeof *L), a1, 6 * (1 + 64) + 2048 + 100,
	       &c, &e);
	CHECK("chapter: a small cap keeps whole entries only, truncated",
	      e.hit && e.truncated && e.len <= e.cap && e.cap == 100 &&
	      !strcmp(ch_buf(&e), "{\"role\":\"user\",\"tool\":null,\"text\":"
	              "\"first\",\"error\":false}"));
	xr_chapters_free(&c);
	xr_entries_free(&e);

	/* print: the object, at null when the corpus has none */
	begin();
	xr_entries_begin(&e, "fabc:0", "s", 1 << 20);
	xr_claude_entries(D, &e, 0xabc);
	line(U("hi \\u0007"));
	end(&c);
	f = open_memstream(&mb, &ml);
	CHECK("print: the object, one line, escaped (K14.5)",
	      c.n == 1 && f && xr_chapter_print(f, "s", "id", xr_chapter_at(&c, 0),
	                                        &e) == 0 &&
	      fclose(f) == 0 && !strcmp(mb, "{\"schema\":1,\"session\":\"s\","
	      "\"session_id\":\"id\",\"anchor\":\"fabc:0\",\"title\":"
	      "\"hi \\u0007\",\"at\":null,\"kind\":\"prompt\",\"entries\":["
	      "{\"role\":\"user\",\"tool\":null,\"text\":\"hi \\u0007\","
	      "\"error\":false}],\"truncated\":false}\n"));
	free(mb);
	xr_chapters_free(&c);
	xr_entries_free(&e);

	/* shell: the command, then C..D joined at soft wraps, D's exit */
	memset(big, 'o', 4000);
	RN = 0;
	rec(RING_K_MARK, 0, "A");
	rec(RING_K_PROMPT, 0, "$");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "make");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_OUTPUT, 0, "ab");
	rec(RING_K_OUTPUT, RING_F_CONT, "cd");
	rec(RING_K_OUTPUT, 0, "ef");
	rec(RING_K_MARK, RING_F_EXIT, "D;2");
	rec(RING_K_OUTPUT, 0, "not the block's");
	rec(RING_K_MARK, 0, "A");
	rec(RING_K_PROMPT, 0, "$");
	rec(RING_K_MARK, 0, "B");
	rec(RING_K_INPUT, 0, "cat");
	rec(RING_K_MARK, RING_F_INPUT, "C");
	rec(RING_K_OUTPUT, 0, big);
	for (k = 0; k < 2; k++)
	{
		struct xr_shell s;
		size_t i;

		xr_shell_begin(&s, &c);
		xr_entries_begin(&e, k ? "s111" : "s101", "s", 1 << 20);
		s.want = &e;
		for (i = 0; i < RN; i++)
			xr_shell_feed(&s, &R[i]);
		xr_shell_end(&s);
		if (!k)
			CHECK("shell chapter: the command, rows WRAP-joined, error "
			      "from D;2", e.hit && c.n == 2 && !strcmp(ch_buf(&e),
			      "{\"role\":\"tool\",\"tool\":null,\"text\":\"make\","
			      "\"error\":false},{\"role\":\"output\",\"tool\":null,"
			      "\"text\":\"abcd\\nef\",\"error\":true}"));
		else
			CHECK("shell chapter: an open block's output at the end, "
			      "clipped to 3000 scalars", e.hit && e.len ==
			      strlen("{\"role\":\"tool\",\"tool\":null,\"text\":\"cat\","
			             "\"error\":false},{\"role\":\"output\",\"tool\":"
			             "null,\"text\":\"\",\"error\":false}") + 3000);
		xr_chapters_free(&c);
		xr_entries_free(&e);
	}
}

/* ---- resolvers (#159, XR6) ---------------------------------------------- */

static char self[PATH_MAX];	/* this binary, for the fake git's fd probe */
static char rd[256];	/* the resolver tests' temp dir (short: no snprintf cuts) */

static void
put(const char *rel, const char *body, size_t n)
{
	char p[PATH_MAX + 64];
	FILE *f;

	snprintf(p, sizeof p, "%s/%s", rd, rel);
	if ((f = fopen(p, "w")))
	{
		fwrite(body, 1, n, f);
		fclose(f);
	}
}

static void
dir(const char *rel)
{
	char p[PATH_MAX + 64];

	snprintf(p, sizeof p, "%s/%s", rd, rel);
	mkdir(p, 0700);
}

/* --ref text against cwd (relative to rd): 1 with *r */
static int
res(const char *text, const char *cwd, int live, struct xr_resolved *r)
{
	char c[PATH_MAX + 64];
	struct xr_span sp;

	snprintf(c, sizeof c, "%s%s%s", rd, *cwd ? "/" : "", cwd);
	memset(r, 0, sizeof *r);
	if (!xr_ref_parse(text, c, &sp))
		return -1;
	return xr_res_ref(&sp, text, c, live, r);
}

static int
is(const struct xr_resolved *r, const char *kind, const char *state)
{
	return r->kind && r->state && !strcmp(r->kind, kind) &&
	       !strcmp(r->state, state);
}

static void
t_res_file(void)
{
	struct xr_resolved r;
	char b[2000];
	static const char *const secret[] = {
		".env", ".env.local", ".ssh/config", ".gnupg/k", ".aws/credentials",
		".netrc", ".npmrc", ".pypirc", ".git-credentials", "a/k.pem",
		"k.key", "c.p12", "login.keychain", "login.keychain-db", "id_rsa",
		"keys/id_ed25519", NULL
	};
	char *buf;
	size_t k, n = 0;
	int ok;

	dir("proj");
	dir("proj/.git");
	dir("proj/src");
	dir("out");
	for (k = 1; k <= 40; k++)
		n += (size_t)snprintf(b + n, sizeof b - n, "line %zu\n", k);
	put("proj/src/a.c", b, n);
	CHECK("file: ok, title the name, subtitle path and line",
	      res("src/a.c:20", "proj", 1, &r) == 1 && is(&r, "file", "ok") &&
	      !strcmp(r.title, "a.c") &&
	      !strcmp(r.subtitle, "src/a.c \xc2\xb7 line 20"));
	CHECK("file: 14 lines around the line, it the seventh, first 14",
	      r.nlines == 14 && !strcmp(r.line[0], "line 14") &&
	      !strcmp(r.line[6], "line 20") && !strcmp(r.line[13], "line 27") &&
	      r.first == 14);
	CHECK("file: line 1 gives the first 14, first 1",
	      res("src/a.c:1", "proj", 1, &r) == 1 && r.nlines == 14 &&
	      !strcmp(r.line[0], "line 1") && r.first == 1);
	CHECK("file: line 5 (start clamp) gives the first 14, first 1",
	      res("src/a.c:5", "proj", 1, &r) == 1 && r.nlines == 14 &&
	      !strcmp(r.line[4], "line 5") && r.first == 1);
	CHECK("file: the last line gives the last 14, first 27",
	      res("src/a.c:40", "proj", 1, &r) == 1 && r.nlines == 14 &&
	      !strcmp(r.line[0], "line 27") && !strcmp(r.line[13], "line 40") &&
	      r.first == 27);
	CHECK("file: a line past the end gives the last 14, first 27",
	      res("src/a.c#L999", "proj", 1, &r) == 1 &&
	      !strcmp(r.line[13], "line 40") && r.first == 27);
	snprintf(b, sizeof b, "%s/proj/src/a.c", rd);
	CHECK("file: an absolute path without a line: the first 14, first 1",
	      res(b, "proj", 1, &r) == 1 && is(&r, "file", "ok") &&
	      !strcmp(r.line[0], "line 1") && !strcmp(r.subtitle, "src/a.c") &&
	      r.first == 1);
	put("proj/s.c", "s1\ns2\ns3\ns4\ns5\n", 15);
	CHECK("file: shorter than 14: all of it, first 1 whatever the line",
	      res("s.c:4", "proj", 1, &r) == 1 && r.nlines == 5 &&
	      !strcmp(r.line[3], "s4") && r.first == 1 &&
	      res("s.c:99", "proj", 1, &r) == 1 && r.first == 1);
	CHECK("file: :line:col reads the line",
	      res("src/a.c:20:3", "proj", 1, &r) == 1 &&
	      !strcmp(r.line[6], "line 20"));
	CHECK("file: from a subdir cwd, inside the git root: relative to the cwd",
	      res("a.c:2", "proj/src", 1, &r) == 1 && is(&r, "file", "ok") &&
	      !strcmp(r.subtitle, "a.c \xc2\xb7 line 2"));
	put("proj/top.c", "x\n", 2);
	CHECK("file: in the root, above the cwd: inside, relative to the root",
	      res("../top.c:1", "proj/src", 1, &r) == 1 && is(&r, "file", "ok") &&
	      !strcmp(r.subtitle, "top.c \xc2\xb7 line 1"));
	put("out/y.c", "y\n", 2);
	CHECK("file: outside the project: outside_project, path only, no first",
	      res("../out/y.c:1", "proj", 1, &r) == 1 &&
	      is(&r, "file", "outside_project") && r.nlines == 0 && !r.first);
	CHECK("file: no git root: the cwd is the boundary",
	      res("../y.c:1", "out", 1, &r) == 1 &&
	      is(&r, "file", "outside_project") &&
	      res("y.c:1", "out", 1, &r) == 1 && is(&r, "file", "ok"));
	/* a symlinked dir inside out of the project; a symlink as the name */
	snprintf(b, sizeof b, "%s/out", rd);
	{
		char l[PATH_MAX];

		snprintf(l, sizeof l, "%s/proj/sub", rd);
		ok = symlink(b, l) == 0;
		snprintf(l, sizeof l, "%s/proj/ln.c", rd);
		ok = ok && symlink("src/a.c", l) == 0;
		snprintf(l, sizeof l, "%s/proj/fifo.c", rd);
		ok = ok && mkfifo(l, 0600) == 0;
	}
	CHECK("file: a symlinked dir out of the project: outside_project",
	      ok && res("sub/y.c:1", "proj", 1, &r) == 1 &&
	      is(&r, "file", "outside_project") && r.nlines == 0);
	CHECK("file: a symlink as the name is no block (lstat: not regular)",
	      res("ln.c:1", "proj", 1, &r) == 0);
	CHECK("file: a FIFO is no block, and never opened (no hang)",
	      res("fifo.c:1", "proj", 1, &r) == 0);
	dir("proj/dir.c");
	CHECK("file: a directory is no block", res("dir.c:1", "proj", 1, &r) == 0);
	CHECK("file: a missing file is no block",
	      res("nope.c:1", "proj", 1, &r) == 0);
	for (k = 0; secret[k]; k++)
	{
		char t[400];

		snprintf(t, sizeof t, "%s/proj/%s:1", rd, secret[k]);
		snprintf(b, sizeof b, "file: secret name %s: secret_name, unread",
		         secret[k]);
		CHECK(b, xr_secret_name(secret[k]) && res(t, "proj", 1, &r) == 1 &&
		      is(&r, "file", "secret_name") && r.nlines == 0);
	}
	CHECK("file: id_rsa.pub, id_x.txt, env.c, a.key.c are not secret names",
	      !xr_secret_name("id_rsa.pub") && !xr_secret_name("id_x.txt") &&
	      !xr_secret_name("env.c") && !xr_secret_name("a.key.c") &&
	      !xr_secret_name("src/keys.c"));
	if ((buf = malloc(XR_RES_FILE + 1)))
	{
		memset(buf, 'a', XR_RES_FILE + 1);
		buf[10] = '\n';
		put("proj/big.c", buf, XR_RES_FILE + 1);
		CHECK("file: 2 MB + 1 is too_large, unread",
		      res("big.c:1", "proj", 1, &r) == 1 &&
		      is(&r, "file", "too_large") && r.nlines == 0);
		put("proj/big.c", buf, XR_RES_FILE);
		CHECK("file: exactly 2 MB is read",
		      res("big.c:1", "proj", 1, &r) == 1 && is(&r, "file", "ok"));
		free(buf);
	}
	put("proj/u.c", "ok \xff\xfe end\r\nnul\0here\n", 20);
	CHECK("file: invalid UTF-8 and NUL as U+FFFD, CR dropped",
	      res("u.c:1", "proj", 1, &r) == 1 && r.nlines == 2 &&
	      !strcmp(r.line[0], "ok \xef\xbf\xbd\xef\xbf\xbd end") &&
	      !strcmp(r.line[1], "nul\xef\xbf\xbdhere"));
	memset(b, 'w', 900);
	b[900] = '\n';
	put("proj/w.c", b, 901);
	CHECK("file: a line cut to 200 scalars",
	      res("w.c:1", "proj", 1, &r) == 1 && strlen(r.line[0]) == 200);
	{
		const char *h = getenv("HOME");
		char hb[PATH_MAX], sv[PATH_MAX];

		snprintf(sv, sizeof sv, "%s", h ? h : "");
		snprintf(hb, sizeof hb, "%s/proj", rd);
		setenv("HOME", hb, 1);
		CHECK("file: ~/ is the querying process's HOME",
		      res("~/src/a.c:3", "proj", 1, &r) == 1 && is(&r, "file", "ok") &&
		      !strcmp(r.line[2], "line 3"));
		if (h)
			setenv("HOME", sv, 1);
		else
			unsetenv("HOME");
	}
	CHECK("file: an ended session still resolves files",
	      res("src/a.c:5", "proj", 0, &r) == 1 && is(&r, "file", "ok"));
	CHECK("ref line: :12, :12:3, #L7, none",
	      xr_ref_line("a.c:12") == 12 && xr_ref_line("a.c:12:3") == 12 &&
	      xr_ref_line(" a.c#L7 ") == 7 && xr_ref_line("a/b.c") == 0);
}

/* A fake git: records its argv and env, then acts as $D/mode says. */
static void
fake_git(void)
{
	char s[4000];
	int n = snprintf(s, sizeof s,
	    "#!/bin/sh\n"
	    "D='%s'\n"
	    "printf '%%s\\n' \"$@\" >> \"$D/argv\"; echo -- >> \"$D/argv\"\n"
	    "/usr/bin/env > \"$D/env\"\n"
	    "m=$(/bin/cat \"$D/mode\")\n"
	    "case \"$*\" in *rev-parse*)\n"
	    "  [ \"$m\" = notrepo ] && { echo false; exit 0; }\n"
	    "  [ \"$m\" = slowrev ] || { echo true; exit 0; } ;; esac\n"
	    "case \"$m\" in\n"
	    "ok) printf '0123456789abcdef0123456789abcdef01234567\\000Ann B\\0001700000000"
	    "\\000a subject\\n\\n f | 1 +\\n 1 file changed\\n'; exit 0 ;;\n"
	    "amb) echo 'error: short object ID abcdef1 is ambiguous' >&2; exit 128 ;;\n"
	    "bad) echo \"fatal: bad object\" >&2; exit 128 ;;\n"
	    "flood) /usr/bin/yes | /usr/bin/head -c 10000000; "
	    "/usr/bin/yes e | /usr/bin/head -c 10000000 >&2; exit 0 ;;\n"
	    "slow|slowrev) echo $$ > \"$D/pid\"; /bin/sleep 30 & /bin/sleep 30 ;;\n"
	    "fds) '%s' --fds > \"$D/fds\"; exit 1 ;;\n"
	    "esac\n", rd, self);

	put("git", s, (size_t)n);
	snprintf(s, sizeof s, "%s/git", rd);
	chmod(s, 0700);
}

static void
mode(const char *m)
{
	put("mode", m, strlen(m));
	put("argv", "", 0);
}

static char *
slurp(const char *rel)
{
	static char b[8192];
	char p[PATH_MAX + 64];
	FILE *f;
	size_t n = 0;

	snprintf(p, sizeof p, "%s/%s", rd, rel);
	if ((f = fopen(p, "r")))
	{
		n = fread(b, 1, sizeof b - 1, f);
		fclose(f);
	}
	b[n] = '\0';
	return b;
}

static void
t_res_git(void)
{
	char git[PATH_MAX], cwd[PATH_MAX], want[4000], gb[PATH_MAX], *e;
	const char *probe[3] = {"/nonexistent/git", git, NULL};
	const char *none[2] = {"/nonexistent/git", NULL};
	struct xr_resolved r;
	struct xr_span sp;
	unsigned long s0;
	long long t0;
	pid_t pid;
	int k;

	snprintf(git, sizeof git, "%s/git", rd);
	snprintf(cwd, sizeof cwd, "%s/proj", rd);
	fake_git();
#ifdef __APPLE__
	CHECK("git probe: never /usr/bin/git on macOS",
	      !xr_git_bin(gb, sizeof gb) || strcmp(gb, "/usr/bin/git"));
#endif
	xr_git_probe = none;
	s0 = xr_res_spawned;
	CHECK("git: none found: no_git, \"git is not installed\", 0 spawns",
	      res("abcdef1", "proj", 1, &r) == 1 && is(&r, "commit", "no_git") &&
	      !strcmp(r.title, "git is not installed") && xr_res_spawned == s0);
	xr_git_probe = probe;
	CHECK("git probe: the first executable in order",
	      xr_git_bin(gb, sizeof gb) && !strcmp(gb, git));
	mode("ok");
	s0 = xr_res_spawned;
	CHECK("git: an ended session: ended, \"Session ended\", 0 spawns",
	      res("abcdef1", "proj", 0, &r) == 1 && is(&r, "commit", "ended") &&
	      !strcmp(r.title, "Session ended") &&
	      !strcmp(r.subtitle, "abcdef1") && xr_res_spawned == s0);
	memset(&sp, 0, sizeof sp);
	sp.shape = XR_COMMIT;
	snprintf(sp.key, sizeof sp.key, "commit:abcdef1");
	for (k = 0; k < 4; k++)
	{
		static const char *const bad[] = {"ABCDEF1", "-cfoo=1", "abc",
		                                  "abcdef1 x"};

		snprintf(sp.full, sizeof sp.full, "%s", bad[k]);
		CHECK("git: a SHA not 7-40 lowercase hex never spawns",
		      xr_res_ref(&sp, bad[k], cwd, 1, &r) == 0 &&
		      xr_res_spawned == s0);
	}
	CHECK("git: ok: subject, \"author \xc2\xb7 age \xc2\xb7 short\", the stat",
	      res("abcdef1234", "proj", 1, &r) == 1 && is(&r, "commit", "ok") &&
	      !strcmp(r.title, "a subject") && r.nlines == 2 &&
	      !strcmp(r.line[0], " f | 1 +") &&
	      !strncmp(r.subtitle, "Ann B \xc2\xb7 ", 8) &&
	      strstr(r.subtitle, "d ago \xc2\xb7 0123456") &&
	      xr_res_spawned == s0 + 2);
	snprintf(want, sizeof want,
	         "--no-pager\n-c\ncore.fsmonitor=\n-c\ncore.hooksPath=/dev/null\n"
	         "-c\nprotocol.allow=never\n-c\ndiff.external=\n-c\ncolor.ui=never\n"
	         "-c\nlog.showSignature=false\n-C\n%s\nrev-parse\n"
	         "--is-inside-work-tree\n--\n"
	         "--no-pager\n-c\ncore.fsmonitor=\n-c\ncore.hooksPath=/dev/null\n"
	         "-c\nprotocol.allow=never\n-c\ndiff.external=\n-c\ncolor.ui=never\n"
	         "-c\nlog.showSignature=false\n-C\n%s\nshow\n--no-color\n"
	         "--no-ext-diff\n--no-textconv\n--stat=80\n--stat-count=14\n"
	         "--format=%%H%%x00%%an%%x00%%at%%x00%%s\n--end-of-options\n"
	         "abcdef1234^{commit}\n--\n", cwd, cwd);
	CHECK("git: argv exactly 8.3's (plus log.showSignature=false), the SHA "
	      "after --end-of-options", !strcmp(slurp("argv"), want));
	e = slurp("env");
	{
		static const char *const env[] = {
			"GIT_CONFIG_NOSYSTEM=1\n", "GIT_CONFIG_GLOBAL=/dev/null\n",
			"GIT_OPTIONAL_LOCKS=0\n", "GIT_TERMINAL_PROMPT=0\n",
			"GIT_NO_LAZY_FETCH=1\n", "PATH=/usr/bin:/bin\n",
			"LANG=en_US.UTF-8\n", "HOME=", NULL
		};
		int all = 1, extra = 0;
		char *l;

		for (k = 0; env[k]; k++)
			all &= strstr(e, env[k]) != NULL;
		/* what /bin/sh adds itself */
		for (l = e; *l; l = strchr(l, '\n') ? strchr(l, '\n') + 1 : l + strlen(l))
			if (strncmp(l, "GIT_", 4) && strncmp(l, "PATH=", 5) &&
			    strncmp(l, "LANG=", 5) && strncmp(l, "HOME=", 5) &&
			    strncmp(l, "PWD=", 4) && strncmp(l, "SHLVL=", 6) &&
			    strncmp(l, "_=", 2) && strncmp(l, "OLDPWD=", 7) &&
			    strncmp(l, "__CF_USER_TEXT_ENCODING=", 24))
				extra = 1;
		CHECK("git: env exactly 8.3's list, nothing else inherited",
		      all && !extra && !strstr(e, "GIT_CONFIG_PARAMETERS"));
		if (!all || extra)
			printf("%s\n", e);
	}
	CHECK("git: a 40-hex SHA reaches git whole, ^{commit} after it",
	      res("0123456789abcdef0123456789abcdef01234567", "proj", 1, &r) == 1 &&
	      is(&r, "commit", "ok") &&
	      strstr(slurp("argv"), "\n--end-of-options\n"
	             "0123456789abcdef0123456789abcdef01234567^{commit}\n--\n"));
	mode("notrepo");
	CHECK("git: rev-parse says false: not_repo, show never runs",
	      res("abcdef1", "proj", 1, &r) == 1 && is(&r, "commit", "not_repo") &&
	      !strcmp(r.title, "Not in a git repository") &&
	      !strstr(slurp("argv"), "show\n"));
	mode("amb");
	CHECK("git: \"is ambiguous\": ambiguous",
	      res("abcdef1", "proj", 1, &r) == 1 && is(&r, "commit", "ambiguous") &&
	      !strcmp(r.title, "Ambiguous short SHA"));
	mode("bad");
	CHECK("git: anything else: not_commit, named by the git root",
	      res("abcdef1", "proj/src", 1, &r) == 1 &&
	      is(&r, "commit", "not_commit") &&
	      !strcmp(r.title, "Not a commit in proj"));
	CHECK("git: a relative or empty cwd: not_repo, no spawn",
	      (s0 = xr_res_spawned, 1) &&
	      xr_ref_parse("abcdef1", NULL, &sp) &&
	      xr_res_ref(&sp, "abcdef1", "", 1, &r) == 1 &&
	      is(&r, "commit", "not_repo") && xr_res_spawned == s0);
	mode("fds");
	put("fds", "", 0);
	{
		/* an fd of ours without CLOEXEC; the probe execs, so the shell's
		** own close-on-exec fds are gone there: what it sees came from us */
		int x = open("/dev/null", O_RDONLY);

		CHECK("git: no fd of the querying process reaches git (one without "
		      "CLOEXEC open), only 0 1 2",
		      x >= 3 && self[0] && res("abcdef1", "proj", 1, &r) == 1 &&
		      !strcmp(slurp("fds"), "0 1 2\n"));
		if (strcmp(slurp("fds"), "0 1 2\n"))
			printf("  fds: [%s] fd %d\n", slurp("fds"), x);
		if (x >= 0)
			close(x);
	}
	mode("flood");
	t0 = xr_now_ms();
	CHECK("git: a 10 MB flood on both pipes is drained, git exits",
	      res("abcdef1", "proj", 1, &r) == 1 &&
	      is(&r, "commit", "not_commit") && xr_now_ms() - t0 < XR_GIT_MS);
	for (k = 0; k < 2; k++)
	{
		mode(k ? "slowrev" : "slow");
		put("pid", "", 0);
		t0 = xr_now_ms();
		CHECK(k ? "git: a slow rev-parse: timeout" :
		      "git: a slow show: timeout, \"git timed out after 3 s\"",
		      res("abcdef1", "proj", 1, &r) == 1 &&
		      is(&r, "commit", "timeout") &&
		      !strcmp(r.title, "git timed out after 3 s"));
		t0 = xr_now_ms() - t0;
		CHECK("git: the timeout is 3 s (one deadline for both runs)",
		      t0 >= XR_GIT_MS - 50 && t0 < XR_GIT_MS + 1500);
		pid = (pid_t)atoi(slurp("pid"));
		t0 = xr_now_ms();
		while (pid > 0 && !(kill(-pid, 0) == -1 && errno == ESRCH) &&
		       xr_now_ms() - t0 < 5000)
			usleep(10000);
		CHECK("git: the timeout ends git's whole group (kill(-pgid, 0): ESRCH)",
		      pid > 0 && kill(-pid, 0) == -1 && errno == ESRCH);
	}
	/* 3.7: SIGTERM to the querying process takes git's group with it */
	mode("slow");
	put("pid", "", 0);
	if ((pid = fork()) == 0)
	{
		(void)res("abcdef1", "proj", 1, &r);
		_exit(0);
	}
	{
		pid_t g = 0;
		int st = 0;

		t0 = xr_now_ms();
		while (pid > 0 && !(g = (pid_t)atoi(slurp("pid"))) &&
		       xr_now_ms() - t0 < 2000)
			usleep(10000);
		usleep(50000);	/* the slow show itself, past the pid write */
		if (pid > 0)
			kill(pid, SIGTERM);
		if (pid > 0)
			waitpid(pid, &st, 0);
		/* killed at once; EPERM while init holds the zombies, then gone */
		t0 = xr_now_ms();
		while (g > 0 && !(kill(-g, 0) == -1 && errno == ESRCH) &&
		       xr_now_ms() - t0 < 5000)
			usleep(10000);
		CHECK("git: SIGTERM ends the querying process, git's group with it",
		      g > 0 && WIFSIGNALED(st) && WTERMSIG(st) == SIGTERM &&
		      kill(-g, 0) == -1 && errno == ESRCH);
	}
	xr_git_probe = NULL;
}

static void
t_res_parse(void)
{
	struct xr_resolved r;
	char b[4000];
	size_t n, k;

	n = (size_t)snprintf(b, sizeof b, "%s", "0123456789abcdef0123456789abcdef"
	                     "01234567");
	memcpy(b + n, "\0A\0" "100\0sub\xff\n\n", 13);
	n += 13;
	for (k = 0; k < 16; k++)
		n += (size_t)snprintf(b + n, sizeof b - n, " f%zu | 1 +\n", k);
	n += (size_t)snprintf(b + n, sizeof b - n, " 16 files changed\n");
	CHECK("git parse: past 14 stat lines, the first 13 and the summary",
	      xr_git_parse(b, n, 100 + 7200, &r) == 1 && r.nlines == 14 &&
	      !strcmp(r.line[12], " f12 | 1 +") &&
	      !strcmp(r.line[13], " 16 files changed") &&
	      !strcmp(r.title, "sub\xef\xbf\xbd") &&
	      !strcmp(r.subtitle, "A \xc2\xb7 2h ago \xc2\xb7 0123456"));
	CHECK("git parse: age now, minutes, days",
	      xr_git_parse(b, n, 100, &r) && strstr(r.subtitle, " now ") &&
	      xr_git_parse(b, n, 100 + 300, &r) && strstr(r.subtitle, "5m ago") &&
	      xr_git_parse(b, n, 100 + 3 * 86400, &r) &&
	      strstr(r.subtitle, "3d ago"));
	CHECK("git parse: a short hash, a missing field, a non-digit time: no",
	      !xr_git_parse("abc\0A\0" "1\0s\n", 10, 0, &r) &&
	      !xr_git_parse(b, 45, 0, &r) &&
	      !xr_git_parse("0123456789abcdef0123456789abcdef01234567\0A\0x\0s",
	                    46, 0, &r));
}

static void
t_resolve(void)
{
	char d[] = "/tmp/xray_res.XXXXXX", cmd[PATH_MAX + 16], real[PATH_MAX];

	if (!mkdtemp(d) || !realpath(d, real) ||
	    snprintf(rd, sizeof rd, "%s", real) >= (int)sizeof rd)
	{
		CHECK("resolve: temp dir", 0);
		return;
	}
	t_res_parse();
	t_res_file();
	t_res_git();
	snprintf(cmd, sizeof cmd, "rm -rf '%s'", rd);
	(void)!system(cmd);
}

/* ---- definition exclusions on the command (#1, #3, #4; #164) ------------- */

static void
rule_hit(void *ctx, const struct xr_hit *h)
{
	if (h->rule && h->rule[0] == 'D' && h->rule[1] >= '1' && h->rule[1] <= '4')
		*(int *)ctx |= 1 << (h->rule[1] - '1');
}

/* The rules (D1 1, D2 2, D3 4, D4 8) a paired Bash call of cmd in
** /home/demo/proj defines by, its output out (JSON-escaped; NULL: the
** heading "# #831 Title") */
static int
defines_out(const char *cmd, const char *out)
{
	char res[400], *b = malloc(strlen(cmd) * 2 + 200), *o;
	int rules = 0;
	struct xr_sink k = { rule_hit, &rules };
	struct xr_chapters ch;

	if (!b)
		return -1;
	snprintf(res, sizeof res, "{\"type\":\"user\",\"cwd\":\"/home/demo/proj\","
	         "\"message\":{\"content\":[{\"type\":\"tool_result\","
	         "\"tool_use_id\":\"b1\",\"content\":\"%s\"}]}}",
	         out ? out : "# #831 Title\\nbody");
	o = b + sprintf(b, "{\"type\":\"assistant\",\"cwd\":\"/home/demo/proj\","
	                "\"message\":{\"content\":[{\"type\":\"tool_use\","
	                "\"id\":\"b1\",\"name\":\"Bash\",\"input\":{\"command\":\"");
	for (; *cmd; cmd++)
	{
		if (strchr("\"\\\n\t\r", *cmd))
			*o++ = '\\';
		*o++ = *cmd == '\n' ? 'n' : *cmd == '\t' ? 't' : *cmd == '\r' ? 'r' :
		       *cmd;
	}
	strcpy(o, "\"}}]}}");
	begin();
	xr_claude_refs(D, &k, 1, NULL, NULL);
	line(b);
	line(res);
	end(&ch);
	xr_chapters_free(&ch);
	free(b);
	return rules;
}

static int
defines(const char *cmd)
{
	return defines_out(cmd, NULL);
}

static void
t_excl(void)
{
	static const char *const stop[] = {
		/* MED-1: a newline ends a command, like ';' */
		"echo x\ncat ../x", "echo x\nrg 831", "cd /etc\ncat notes.md",
		"true\ngit diff", "true\r\ngit diff", "cd /etc && \\\n  cat notes.md",
		"echo x; \\\nrg 831",
		/* MED-1: a wrapper by its basename */
		"/usr/bin/env rg x", "/usr/bin/env cat /etc/x", "/usr/bin/xargs rg x",
		"/usr/bin/sudo cat /etc/x",
		/* LOW-5: wrappers */
		"nohup cat ../x", "timeout 5 cat ../x", "timeout -s KILL 5 rg x",
		"doas cat ../x", "doas -u root rg x", "exec cat ../x",
		"exec -a n rg x", "eval cat ../x", "eval 'rg x'",
		"eval 'cd /etc; cat notes.md'", "stdbuf -oL cat ../x",
		"stdbuf -o L rg x", "watch cat ../x", "watch -n 1 git diff",
		"env -S 'rg x'", "env -S 'cat /etc/x'",
		/* LOW-5: reserved words, a { } group */
		"{ cat ../x; }", "! cat ../x", "if true; then cat ../x; fi",
		"for f in a; do cat ../x; done", "while true; do rg x; done",
		/* LOW-5: command substitution in quotes, backticks */
		"echo \"$(cat ../x)\"", "echo \"x $(rg 831) y\"", "echo `cat ../x`",
		"echo \"`rg x`\"", "cd /etc && echo \"$(cat notes.md)\"",
		"sh -c 'echo \"$(cat ../x)\"'",
		/* LOW-5: pushd is followed like cd */
		"pushd /etc && cat notes.md", "pushd /etc >/dev/null; cat notes.md",
		"pushd +1 && cat notes.md",
		/* a here-doc's body is skipped, the command after it is not */
		"cat <<EOF\nbody\nEOF\nrg x", "cat <<-EOF\n\tbody\n\tEOF\ngit diff",
		/* ruling 1: printers of #1 beyond v2.1's list, a file outside */
		"sed 's/a/b/' ../x.md", "sed -e 's/a/b/' /etc/x", "jq . ../x.json",
		"jq -r .title /etc/x.json", "tac ../x", "sort ../x", "cut -f1 ../x",
		"zcat /etc/x.gz", "gzcat ../x.gz", "batcat /etc/x",
		"glow ../README.md", "yq '.a' /etc/x.yaml", "uniq ../x", "rev ../x",
		"column -t /etc/x", "fold -w 80 ../x", "fmt ../x",
		0
	};
	static const char *const keep[] = {
		/* quoted newlines and substitutions stay one word */
		"echo \"a\nrg x\"", "echo 'x\ncat ../x'", "echo '$(cat ../x)'",
		"echo \"\\$(cat ../x)\"", "echo \"$(true)\" ok",
		/* the shipped reading (#148) is unchanged */
		"cat notes.md", "cd sub && cat ../n.md", "sed -n '/pat/p' notes.md",
		"cat sub/../x", "time -p cat notes.md", "\n\ncat notes.md\n\n",
		"gh issue view 12 \\\n  --json title",
		/* a here-doc's body is no command; its delimiter is no file */
		"gh pr create --title Fix --body \"$(cat <<'EOF'\n## Summary\n"
		"rg is used\ngit diff shows\ncat /etc/x\nEOF\n)\"",
		"cat <<EOF | gh issue create --title X --body-file -\nls a\nEOF",
		"cd $X && cat <<EOF\nbody\nEOF",
		/* wrappers with nothing to run, a here-string */
		"timeout 5", "eval", "env -S", "cat <<< \"x\"",
		/* ruling 1: a printer of a file inside the cwd still defines */
		"sed 's/a/b/' notes.md", "jq . x.json", "sort notes.md",
		0
	};
	/* change (a): a reader or search only inside $(…) or backquotes
	** leaves D3 (argv) to the outer command; D1, D2, D4 stay off */
	static const char *const d3[] = {
		"gh pr create --title \"T\" --body \"$(cat /abs/outside/body.md)\"",
		"gh pr create --title \"T\" --body \"`cat /etc/body.md`\"",
		"gh pr create --title \"T\" --body \"$(rg -l x)\"",
		"gh pr create --title T --body \"$(git diff)\"",
		0
	};
	static const char *const d3no[] = {
		"cat /etc/x; gh pr create --title \"T\" --body \"$(cat /etc/b.md)\"",
		"gh pr create --title T --body \"$(cat /etc/b.md)\" && rg x",
		"gh pr create --title T --body \"`rg x`\"\ncat ../x",
		0
	};
	static const char url[] = "https://github.com/o/r/pull/12";
	char m[200], *p, *big = malloc(257 * 4 + 2);
	size_t i;

	for (i = 0; stop[i]; i++)
	{
		snprintf(m, sizeof m, "excl: stops defining: %s", stop[i]);
		for (p = m; (p = strpbrk(p, "\n\t\r")); )
			*p = '|';
		CHECK(m, defines(stop[i]) == 0);
	}
	for (i = 0; keep[i]; i++)
	{
		snprintf(m, sizeof m, "excl: still defines: %s", keep[i]);
		for (p = m; (p = strpbrk(p, "\n\t\r")); )
			*p = '|';
		CHECK(m, defines(keep[i]) & 1);
	}
	for (i = 0; d3[i]; i++)
	{
		snprintf(m, sizeof m, "excl: D3 only: %s", d3[i]);
		CHECK(m, defines_out(d3[i], url) == 4 && defines(d3[i]) == 4);
	}
	for (i = 0; d3no[i]; i++)
	{
		snprintf(m, sizeof m, "excl: no D3 with an outer reader: %s", d3no[i]);
		for (p = m; (p = strpbrk(p, "\n\t\r")); )
			*p = '|';
		CHECK(m, defines_out(d3no[i], url) == 0);
	}
	CHECK("excl: `todo.py get … | jq .` (no file) defines by D2",
	      defines_out("todo.py get dch 831 | jq .",
	                  "{\\\"number\\\": 831, \\\"title\\\": \\\"Fix\\\"}") == 2);
	/* change (b): 256 words are read, a 257th excludes the command whole */
	if (big)
	{
		strcpy(big, "cat");
		for (i = 1; i < 256; i++)
			strcat(big, " a");
		CHECK("excl: 256 words are read (cat a … a)", defines(big) == 1);
		strcat(big, "\n");
		CHECK("excl: 256 words and a trailing newline are read",
		      defines(big) == 1);
		big[strlen(big) - 1] = '\0';
		strcat(big, " a");
		CHECK("excl: 257 words exclude the command whole", defines(big) == 0);
		free(big);
	}
}

int
main(int argc, char **argv)
{
	/* the fake git's fd probe: the fds this process was started with */
	if (argc == 2 && !strcmp(argv[1], "--fds"))
	{
		int fd, n = 0;

		for (fd = 0; fd < 1024; fd++)
			if (fcntl(fd, F_GETFD) != -1)
				printf("%s%d", n++ ? " " : "", fd);
		printf("\n");
		return 0;
	}
	if (!realpath(argv[0], self))
		self[0] = '\0';
	t_seam();
	t_title();
	t_key();
	t_ids();
	t_claude();
	t_time();
	t_read();
	t_shell();
	t_walk();
	t_print();
	t_grammar();
	t_ref();
	t_index();
	t_caps();
	t_excl();
	t_cancel();
	t_scan_caps();
	t_scan_scope();
	t_lookup();
	t_chapter();
	t_resolve();
	t_codex();
	t_codex_lookup();
	printf("%s: %d failed\n", fails ? "FAIL" : "ok", fails);
	return fails != 0;
}
