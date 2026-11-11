/* Units for the X-ray engine, xray.c (#120, XR1b): titles, the project key,
** the identity shapes, the Claude decoder's turn rules and caps, the bounded
** JSONL reader, the shell chapter machine and the output cap; refs (#135):
** the grammar rows, --ref's parse and value rule, the index's lifetime and
** caps (keys, spans, definitions, the dropped set) and the config_dir check.
**
**   cc -I. -o xray_unit tests/xray_unit.c xray.c json.c && ./xray_unit
*/
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
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

int
main(void)
{
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
	t_cancel();
	t_scan_caps();
	t_scan_scope();
	t_lookup();
	printf("%s: %d failed\n", fails ? "FAIL" : "ok", fails);
	return fails != 0;
}
