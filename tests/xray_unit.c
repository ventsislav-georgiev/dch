/* Units for the X-ray engine, xray.c (#120, XR1b): titles, the project key,
** the identity shapes, the Claude decoder's turn rules and caps, the bounded
** JSONL reader, the shell chapter machine and the output cap.
**
**   cc -I. -o xray_unit tests/xray_unit.c xray.c json.c && ./xray_unit
*/
#include <fcntl.h>
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
	printf("%s: %d failed\n", fails ? "FAIL" : "ok", fails);
	return fails != 0;
}
