/*
 * xray.c - the X-ray engine's one-shot half (#120, XR1b). See xray.h.
 *
 * Claude chapters follow Prosper's TranscriptDecoder, TranscriptAssembler and
 * TranscriptVerbosity.turns() rule for rule (spec 5.3, 9.3 parity): the same
 * key aliases, the same hidden noise, the same prompt/step/image split, the
 * same tool pairing (256 open calls) and sidechain threads. Only what decides
 * a chapter's start, kind, title, offset and error is kept; block bodies are
 * never built.
 */
#include <config.h>

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "bridge.h"
#include "ring.h"
#include "xray.h"

void dch_trace(const char *fmt, ...)
#if defined(__GNUC__) || defined(__clang__)
	__attribute__((format(printf, 1, 2)))
#endif
	;

unsigned long xr_seam_opened, xr_seam_refused;
struct xr_limits xr_lim = {XR_HEAD_RANGE, XR_TAIL_RANGE, XR_CHUNK, XR_LINE_MAX,
                           XR_OUT_MAX};

/* ---- UTF-8 and Unicode classes ----------------------------------------- */

/* The scalar at s (n > 0 bytes left); invalid bytes are one U+FFFD each. */
static uint32_t
u8_next(const unsigned char *s, size_t n, size_t *adv)
{
	size_t l = dch_utf8_len(s, n);
	uint32_t c;

	if (!l)
	{
		*adv = 1;
		return 0xfffd;
	}
	*adv = l;
	if (l == 1)
		return s[0];
	c = s[0] & (l == 2 ? 0x1f : l == 3 ? 0x0f : 0x07);
	for (size_t k = 1; k < l; k++)
		c = c << 6 | (s[k] & 0x3f);
	return c;
}

static size_t
u8_put(char *o, uint32_t c)
{
	if (c < 0x80)
		return (void)(o[0] = (char)c), 1;
	if (c < 0x800)
	{
		o[0] = (char)(0xc0 | c >> 6);
		o[1] = (char)(0x80 | (c & 63));
		return 2;
	}
	if (c < 0x10000)
	{
		o[0] = (char)(0xe0 | c >> 12);
		o[1] = (char)(0x80 | (c >> 6 & 63));
		o[2] = (char)(0x80 | (c & 63));
		return 3;
	}
	o[0] = (char)(0xf0 | c >> 18);
	o[1] = (char)(0x80 | (c >> 12 & 63));
	o[2] = (char)(0x80 | (c >> 6 & 63));
	o[3] = (char)(0x80 | (c & 63));
	return 4;
}

/* Unicode White_Space: Swift's Character.isWhitespace and Foundation's
** .whitespacesAndNewlines agree on it. */
static int
u_space(uint32_t c)
{
	return (c >= 0x09 && c <= 0x0d) || c == 0x20 || c == 0x85 ||
	       c == 0xa0 || c == 0x1680 || (c >= 0x2000 && c <= 0x200a) ||
	       c == 0x2028 || c == 0x2029 || c == 0x202f || c == 0x205f ||
	       c == 0x3000;
}

/* Character.isNewline */
static int
u_newline(uint32_t c)
{
	return (c >= 0x0a && c <= 0x0d) || c == 0x85 || c == 0x2028 ||
	       c == 0x2029;
}

static int
blank(const char *s, size_t n)
{
	size_t i, a;

	for (i = 0; i < n; i += a)
		if (!u_space(u8_next((const unsigned char *)s + i, n - i, &a)))
			return 0;
	return 1;
}

static size_t
scalars(const char *s, size_t n)
{
	size_t i, a, k = 0;

	for (i = 0; i < n; i += a, k++)
		(void)u8_next((const unsigned char *)s + i, n - i, &a);
	return k;
}

/* Trim White_Space from both ends of s[0..*n): the new start. */
static const char *
trim(const char *s, size_t *n)
{
	size_t i = 0, a, e = *n, last = 0;

	while (i < *n && u_space(u8_next((const unsigned char *)s + i, *n - i,
	                                 &a)))
		i += a;
	for (size_t j = i; j < e; j += a)
		if (!u_space(u8_next((const unsigned char *)s + j, e - j, &a)))
			last = j + a;
	*n = last > i ? last - i : 0;
	return s + i;
}

#define XR_WHOLE ((size_t)1 << (sizeof(size_t) * 8 - 1))

/* n | XR_WHOLE: the whole text, newlines collapsed too (a slash command's
** "/name args"); else the first line holding a non-blank scalar. */
void
xr_title(const char *s, size_t n, char *out, size_t outsz)
{
	const unsigned char *u = (const unsigned char *)s;
	size_t i, a, o = 0, k = 0, ls = 0, le;
	int gap = 0, found = 0;
	uint32_t c;

	if (outsz < XR_TITLE_MAX * 4 + 1)
	{
		if (outsz)
			out[0] = '\0';
		return;
	}
	if (n & XR_WHOLE)
	{
		n &= ~XR_WHOLE;
		le = n;
		found = 1;
	}
	else
		for (i = 0, le = n; i <= n; i += a)
		{
			if (i == n)
			{
				c = '\n';
				a = 1;
			}
			else
				c = u8_next(u + i, n - i, &a);
			if (!u_newline(c))
				continue;
			if (!blank(s + ls, i - ls))
			{
				le = i;
				found = 1;
				break;
			}
			ls = i + a;
		}
	if (!found)
		ls = le = 0;
	for (i = ls; i < le && k < XR_TITLE_MAX; i += a)
	{
		c = u8_next(u + i, le - i, &a);
		if (u_space(c))
		{
			gap = 1;
			continue;
		}
		if (gap && o)
		{
			out[o++] = ' ';
			if (++k == XR_TITLE_MAX)
				break;
		}
		gap = 0;
		o += u8_put(out + o, c ? c : 0xfffd);
		k++;
	}
	out[o] = '\0';
}

/* ---- identity ----------------------------------------------------------- */

int
xr_id_shaped(const char *s)
{
	int k;

	if (s[0] != 'm')
		return 0;
	for (k = 1; k <= 16; k++)
		if (!((s[k] >= '0' && s[k] <= '9') || (s[k] >= 'a' && s[k] <= 'f')))
			return 0;
	return s[17] == '\0';
}

int
xr_label_id_shaped(const char *s, size_t n)
{
	char b[18];

	while (n && (*s == ' ' || (*s >= '\t' && *s <= '\r')))
		s++, n--;
	while (n && (s[n - 1] == ' ' || (s[n - 1] >= '\t' && s[n - 1] <= '\r')))
		n--;
	if (n != 17)
		return 0;
	/* any case (QA 1.45.1 LOW-1): on a case-insensitive file system the
	** name's socket also answers to the id */
	for (n = 0; n < 17; n++)
		b[n] = (char)tolower((unsigned char)s[n]);
	b[17] = '\0';
	return xr_id_shaped(b);
}

int
xr_uuid_form(const char *s)
{
	int k;

	for (k = 0; k < 36; k++)
		if (!((s[k] >= '0' && s[k] <= '9') || (s[k] >= 'a' && s[k] <= 'f') ||
		      s[k] == '-'))
			return 0;
	return s[36] == '\0';
}

int
xr_uuid_shaped(const char *s)
{
	int k;

	for (k = 0; k < 36; k++)
		if (k == 8 || k == 13 || k == 18 || k == 23 ? s[k] != '-'
		    : !isxdigit((unsigned char)s[k]))
			return 0;
	return s[36] == '\0';
}

int
xr_record_form(const char *s)
{
	size_t n = strlen(s);

	return n > 5 && strcmp(s + n - 5, ".json") == 0 && !strchr(s, '/') &&
	       !strstr(s, "..");
}

/* ---- the seam ------------------------------------------------------------ */

int
xr_seam_open(const char *path, const struct stat *want)
{
	struct stat st;
	int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);

	if (fd < 0)
	{
		int e = errno;

		xr_seam_refused++;
		dch_trace("xray seam: refused (open errno %d)", e);
		errno = e;
		return -1;
	}
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
	    st.st_dev != want->st_dev || st.st_ino != want->st_ino)
	{
		close(fd);
		xr_seam_refused++;
		dch_trace("xray seam: refused (not the file looked up)");
		errno = 0;	/* swapped, not unreadable */
		return -1;
	}
#ifdef F_NOCACHE
	(void)fcntl(fd, F_NOCACHE, 1);
#elif defined(POSIX_FADV_DONTNEED)
	(void)posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
#endif
	xr_seam_opened++;
	dch_trace("xray seam: open ino=%llx", (unsigned long long)st.st_ino);
	return fd;
}

int
xr_config_ok(const char *cfg)
{
	size_t n = strlen(cfg);
	const char *p;

	if (cfg[0] != '/' || n > XR_PATH_MAX)
		return 0;
	for (p = cfg; (p = strstr(p, "..")); p += 2)
		if ((p == cfg || p[-1] == '/') && (p[2] == '/' || p[2] == '\0'))
			return 0;
	return 1;
}

int
xr_claude_lookup(const char *cfg, const char *sid, char *path, size_t pathsz,
                 struct stat *st, char *project, size_t projectsz)
{
	char dir[XR_PATH_MAX + 16], p[XR_PATH_MAX + 400];
	struct dirent *de;
	struct stat s;
	DIR *d;
	int hits = 0;

	if (!xr_config_ok(cfg) || !xr_uuid_shaped(sid) ||
	    snprintf(dir, sizeof dir, "%s/projects", cfg) >= (int)sizeof dir ||
	    !(d = opendir(dir)))
		return -1;
	while ((de = readdir(d)) && hits < 2)
	{
		/* the project dir is lstat'ed too: a symlinked one is not
		** followed (QA 1.44 LOW-7) */
		if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..") ||
		    snprintf(p, sizeof p, "%s/%s", dir, de->d_name)
		    >= (int)sizeof p || lstat(p, &s) < 0 || !S_ISDIR(s.st_mode) ||
		    snprintf(p, sizeof p, "%s/%s/%s.jsonl", dir, de->d_name, sid)
		    >= (int)sizeof p ||
		    lstat(p, &s) < 0 || !S_ISREG(s.st_mode))
			continue;
		if (hits++ == 0 &&
		    (snprintf(path, pathsz, "%s", p) >= (int)pathsz ||
		     snprintf(project, projectsz, "%s", de->d_name)
		     >= (int)projectsz))
			hits = 2;	/* cut short: no path beats a wrong one */
		*st = s;
	}
	closedir(d);
	if (hits != 1 && projectsz)
		project[0] = '\0';	/* ambiguous: no one hit's dir */
	dch_trace("xray lookup: %d hit%s", hits, hits == 1 ? "" : "s");
	return hits == 1 ? 0 : -1;
}

/* ---- chapters ------------------------------------------------------------ */

void
xr_chapters_free(struct xr_chapters *c)
{
	free(c->v);
	memset(c, 0, sizeof *c);
}

const struct xr_chapter *
xr_chapter_at(const struct xr_chapters *c, size_t i)
{
	return i < c->n ? &c->v[(c->start + i) % XR_CHAPTERS] : NULL;
}

/* A new newest chapter, the oldest going past XR_CHAPTERS; NULL without
** memory. */
static struct xr_chapter *
ch_push(struct xr_chapters *c)
{
	struct xr_chapter *x;

	if (!c->v && !(c->v = calloc(XR_CHAPTERS, sizeof *c->v)))
		return NULL;
	if (c->n == XR_CHAPTERS)
	{
		x = &c->v[c->start];
		c->start = (c->start + 1) % XR_CHAPTERS;
	}
	else
		x = &c->v[(c->start + c->n++) % XR_CHAPTERS];
	c->total++;
	memset(x, 0, sizeof *x);
	x->at = -1;
	x->kind = "prompt";
	return x;
}

/* Chapter number `ord` (0-based over every one started), if still held. */
static struct xr_chapter *
ch_ord(struct xr_chapters *c, uint64_t ord)
{
	uint64_t first = c->total - c->n;

	if (ord < first || ord >= c->total)
		return NULL;
	return &c->v[(c->start + (size_t)(ord - first)) % XR_CHAPTERS];
}

/* ---- JSON over dch's tokenizer --------------------------------------------- */

static int
j_null(const struct json_parser *p, int t)
{
	return p->tok[t].kind == JSON_PRIMITIVE &&
	       p->tok[t].end - p->tok[t].start == 4 &&
	       !memcmp(p->text + p->tok[t].start, "null", 4);
}

/* A string token decoded, lone surrogates and \u0000 as U+FFFD; malloc'd,
** NUL-terminated, *n its length. NULL for a non-string. */
static char *
j_str(const struct json_parser *p, int t, size_t *n)
{
	const char *s;
	char *o;
	size_t k = 0;
	int i, e;

	if (t < 0 || p->tok[t].kind != JSON_STRING)
		return NULL;
	s = p->text;
	i = p->tok[t].start;
	e = p->tok[t].end;
	/* calloc: the analyzer cannot see sscanf's %n bound in iso_time() */
	if (!(o = calloc(1, (size_t)(e - i) + 1)))
		return NULL;
	while (i < e)
	{
		uint32_t c = (unsigned char)s[i], lo;

		if (c != '\\')
		{
			o[k++] = (char)c;
			i++;
			continue;
		}
		c = (unsigned char)s[i + 1];
		i += 2;
		switch (c)
		{
		case 'b': o[k++] = '\b'; continue;
		case 'f': o[k++] = '\f'; continue;
		case 'n': o[k++] = '\n'; continue;
		case 'r': o[k++] = '\r'; continue;
		case 't': o[k++] = '\t'; continue;
		case 'u': break;
		default: o[k++] = (char)c; continue;
		}
		c = (uint32_t)strtoul((char[5]){ s[i], s[i + 1], s[i + 2], s[i + 3],
		                                 0 }, NULL, 16);
		i += 4;
		if (c >= 0xd800 && c <= 0xdbff && i + 6 <= e && s[i] == '\\' &&
		    s[i + 1] == 'u' &&
		    (lo = (uint32_t)strtoul((char[5]){ s[i + 2], s[i + 3],
		                                       s[i + 4], s[i + 5], 0 },
		                            NULL, 16)) >= 0xdc00 && lo <= 0xdfff)
		{
			c = 0x10000 + ((c - 0xd800) << 10) + (lo - 0xdc00);
			i += 6;
		}
		else if ((c >= 0xd800 && c <= 0xdfff) || c == 0)
			c = 0xfffd;
		/* an escape is 6 bytes, its UTF-8 at most 4 (a pair: 12 -> 4) */
		k += u8_put(o + k, c);
	}
	o[k] = '\0';
	*n = k;
	return o;
}

/* The value of `key` in object `obj`, the last one when repeated (as a
** dictionary keeps it); -1 when absent. */
static int
j_field(const struct json_parser *p, int obj, const char *key)
{
	size_t kl = strlen(key), n;
	int found = -1, i;

	if (obj < 0 || p->tok[obj].kind != JSON_OBJECT)
		return -1;
	for (i = obj + 1; i < p->tok[obj].next && i + 1 < p->ntok;
	     i = p->tok[i + 1].next)
	{
		const struct json_token *t = &p->tok[i];

		if ((size_t)(t->end - t->start) == kl &&
		    !memcmp(p->text + t->start, key, kl))
			found = i + 1;
		else if (memchr(p->text + t->start, '\\',
		                (size_t)(t->end - t->start)))
		{
			char *d = j_str(p, i, &n);

			if (d && n == kl && !memcmp(d, key, kl))
				found = i + 1;
			free(d);
		}
	}
	return found;
}

/* TranscriptKeys.get: the first alias whose value is not null. */
static int
j_get(const struct json_parser *p, int obj, const char *const *alias)
{
	int v;

	for (; *alias; alias++)
		if ((v = j_field(p, obj, *alias)) >= 0 && !j_null(p, v))
			return v;
	return -1;
}

static const char *const A_TYPE[] = { "type", "entryType", "entry_type", 0 };
static const char *const A_SIDE[] = { "isSidechain", "is_sidechain", 0 };
static const char *const A_AGENT[] = { "agentId", "agent_id", 0 };
static const char *const A_META[] = { "isMeta", "is_meta", 0 };
static const char *const A_SUMMARY[] = { "isCompactSummary",
                                         "is_compact_summary", 0 };
static const char *const A_TUID[] = { "tool_use_id", "toolUseId",
                                      "tool_use_ID", 0 };
static const char *const A_ERR[] = { "is_error", "isError", 0 };
static const char *const A_SUB[] = { "subtype", "sub_type", 0 };
static const char *const A_CWD[] = { "cwd", "workingDirectory",
                                     "working_directory", 0 };
static const char *const A_MESSAGE[] = { "message", 0 };
static const char *const A_CONTENT[] = { "content", 0 };
static const char *const A_TEXT[] = { "text", 0 };
static const char *const A_THINK[] = { "thinking", 0 };
static const char *const A_ID[] = { "id", 0 };
static const char *const A_NAME[] = { "name", 0 };
static const char *const A_INPUT[] = { "input", 0 };
static const char *const A_PATTERN[] = { "pattern", 0 };
static const char *const A_ATTACH[] = { "attachment", 0 };
static const char *const A_PROMPT[] = { "prompt", 0 };
static const char *const A_TS[] = { "timestamp", 0 };

/* K.str: a string value or NULL (malloc'd). */
static char *
k_str(const struct json_parser *p, int obj, const char *const *alias,
      size_t *n)
{
	size_t ignored;

	return j_str(p, j_get(p, obj, alias), n ? n : &ignored);
}

static int
k_obj(const struct json_parser *p, int obj, const char *const *alias)
{
	int v = j_get(p, obj, alias);

	return v >= 0 && p->tok[v].kind == JSON_OBJECT ? v : -1;
}

/* K.bool: a JSON Bool, as NSNumber bridges it (true, or the number 1). */
static int
k_bool(const struct json_parser *p, int obj, const char *const *alias)
{
	int v = j_get(p, obj, alias);
	const char *s;
	int n;

	if (v < 0 || p->tok[v].kind != JSON_PRIMITIVE)
		return 0;
	s = p->text + p->tok[v].start;
	n = p->tok[v].end - p->tok[v].start;
	if (n == 4 && !memcmp(s, "true", 4))
		return 1;
	return s[0] != 'f' && s[0] != 'n' && strtod(s, NULL) == 1.0;
}

static int
k_streq(const struct json_parser *p, int obj, const char *const *alias,
        const char *want)
{
	char *s = k_str(p, obj, alias, NULL);
	int r = s && !strcmp(s, want);

	free(s);
	return r;
}

/* ISO 8601 "2026-01-02T03:04:05.678Z" (or +hh:mm) to epoch seconds; -1. */
static long long
iso_time(const char *s)
{
	struct tm tm;
	int y, mo, d, h, mi, se, n = 0, off = 0;
	const char *p;

	if (!s || sscanf(s, "%4d-%2d-%2dT%2d:%2d:%2d%n", &y, &mo, &d, &h, &mi,
	                 &se, &n) != 6 || n != 19)
		return -1;
	p = s + 19;
	if (*p == '.')
		for (p++; *p >= '0' && *p <= '9'; p++)
			;
	if (*p == '+' || *p == '-')
	{
		/* exactly [+-]HH:MM, byte by byte: p += 6 must stay in bounds */
		if (!isdigit((unsigned char)p[1]) || !isdigit((unsigned char)p[2]) ||
		    p[3] != ':' || !isdigit((unsigned char)p[4]) ||
		    !isdigit((unsigned char)p[5]))
			return -1;
		off = (((p[1] - '0') * 10 + p[2] - '0') * 60 +
		       (p[4] - '0') * 10 + p[5] - '0') * 60 * (*p == '-' ? -1 : 1);
		p += 6;
	}
	else if (*p == 'Z')
		p++;
	if (*p || mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 ||
	    se > 60)
		return -1;
	memset(&tm, 0, sizeof tm);
	tm.tm_year = y - 1900;
	tm.tm_mon = mo - 1;
	tm.tm_mday = d;
	tm.tm_hour = h;
	tm.tm_min = mi;
	tm.tm_sec = se;
	return (long long)timegm(&tm) - off;
}

/* ---- Claude: Prosper's text rules ----------------------------------------- */

static const char *
find(const char *s, size_t n, const char *needle, size_t from)
{
	size_t l = strlen(needle);

	for (size_t i = from; i + l <= n; i++)
		if (s[i] == needle[0] && !memcmp(s + i, needle, l))
			return s + i;
	return NULL;
}

/* tag(): the trimmed text between the first <name> and the </name> after. */
static const char *
tag(const char *s, size_t n, const char *name, size_t *len)
{
	char o[64], c[64];
	const char *a, *b;

	snprintf(o, sizeof o, "<%s>", name);
	snprintf(c, sizeof c, "</%s>", name);
	if (!(a = find(s, n, o, 0)))
		return NULL;
	a += strlen(o);
	if (!(b = find(s, n, c, (size_t)(a - s))))
		return NULL;
	*len = (size_t)(b - a);
	return trim(a, len);
}

/* command(): `<command-name>/x</command-name><command-args>…` and nothing
** else. 1 with the title ("/name args") in title[], else 0. */
static int
command(const char *s, size_t n, char *title, size_t titlesz)
{
	const char *t, *name, *args;
	size_t tl = n, nl, al = 0, i, a, k;
	char *buf;

	t = trim(s, &tl);
	if (tl < 9 || memcmp(t, "<command-", 9) ||
	    !(name = tag(t, tl, "command-name", &nl)) || !nl || name[0] != '/' ||
	    scalars(name, nl) > 80)
		return 0;
	for (i = 0; i < nl; i += a)
		if (u_newline(u8_next((const unsigned char *)name + i, nl - i, &a)))
			return 0;
	/* every <(command-[a-z-]+)>…</\1> pair out, the rest blank */
	for (i = 0; i < tl; i += a)
	{
		if (t[i] == '<' && i + 9 < tl && !memcmp(t + i, "<command-", 9))
		{
			char close[96];
			const char *e;

			for (k = i + 9; k < tl && ((t[k] >= 'a' && t[k] <= 'z') ||
			                           t[k] == '-'); k++)
				;
			if (k > i + 9 && k < tl && t[k] == '>' && k - i < 80)
			{
				snprintf(close, sizeof close, "</%.*s>",
				         (int)(k - i - 1), t + i + 1);
				if ((e = find(t, tl, close, k + 1)))
				{
					i = (size_t)(e - t) + strlen(close);
					a = 0;
					continue;
				}
			}
		}
		if (!u_space(u8_next((const unsigned char *)t + i, tl - i, &a)))
			return 0;
	}
	if ((args = tag(t, tl, "command-args", &al)))
	{
		/* prefix(240) */
		for (i = k = 0; i < al && k < 240; i += a, k++)
			(void)u8_next((const unsigned char *)args + i, al - i, &a);
		al = i;
		if (blank(args, al))
			args = NULL;
	}
	if (!(buf = malloc(nl + al + 2)))
		return 0;
	memcpy(buf, name, nl);
	if (args)
	{
		buf[nl] = ' ';
		memcpy(buf + nl + 1, args, al);
	}
	xr_title(buf, (nl + (args ? al + 1 : 0)) | XR_WHOLE, title, titlesz);
	free(buf);
	return 1;
}

/* The title (xr_title) is settled once the first non-blank line has ended or
** holds XR_TITLE_MAX non-blank scalars: each of those is one title scalar. */
static int
settled(const char *o, size_t k, size_t *t, size_t *nb)
{
	size_t a;
	uint32_t c;

	while (*t < k)
	{
		c = u8_next((const unsigned char *)o + *t, k - *t, &a);
		*t += a;
		if (u_newline(c))
		{
			if (*nb)
				return 1;
		}
		else if (!u_space(c) && ++*nb >= XR_TITLE_MAX)
			return 1;
	}
	return 0;
}

/* unwrapPasted: each <pasted_content id="k">\n?…\n?</pasted_content id="k">
** becomes its text; in place (the output never passes the input), the new
** length returned. It stops once the title is settled: an opener with no
** close scans to the end, and only a few fit before that (QA 1.44 MED-3). */
static size_t
unwrap(char *s, size_t n)
{
	static const char open[] = "<pasted_content id=\"";
	char close[96];
	size_t i = 0, k = 0, j, cs, ce, t = 0, nb = 0, a;
	const char *e;

	while (i < n && !settled(s, k, &t, &nb))
	{
		if (s[i] == '<' && i + sizeof open - 1 <= n &&
		    !memcmp(s + i, open, sizeof open - 1))
		{
			j = i + sizeof open - 1;
			cs = j;
			while (j < n && j - cs < 65 &&
			       (isalnum((unsigned char)s[j]) || s[j] == '_' ||
			        s[j] == '-'))
				j++;
			if (j > cs && j - cs <= 64 && j + 1 < n && s[j] == '"' &&
			    s[j + 1] == '>')
			{
				snprintf(close, sizeof close, "</pasted_content id=\"%.*s\">",
				         (int)(j - cs), s + cs);
				cs = j + 2;
				if (cs < n && s[cs] == '\n')
					cs++;
				if ((e = find(s, n, close, cs)))
				{
					ce = (size_t)(e - s);
					if (ce > cs && s[ce - 1] == '\n')
						ce--;
					memmove(s + k, s + cs, ce - cs);
					k += ce - cs;
					i = (size_t)(e - s) + strlen(close);
					continue;
				}
			}
		}
		/* a whole scalar, so settled() never sees half of one */
		(void)u8_next((const unsigned char *)s + i, n - i, &a);
		memmove(s + k, s + i, a);
		k += a;
		i += a;
	}
	return k;
}

/* ---- Claude: the assembler ------------------------------------------------- */

#define XR_THREADS 1024

struct pend
{
	char *id;
	long long ord;		/* the chapter its block is in; -1: a thread's */
	int quiet;		/* Grep/Glob with a pattern: the search card
				** shows no error (TranscriptVerbosity.isError) */
};

struct xr_claude
{
	struct xr_chapters ch;
	int turn, lead, bare;	/* bare: the last main block is a compaction
				** with no summary (mergeSummary's target) */
	int titled;		/* the open chapter has its prompt */
	struct pend pend[XR_PENDING + 1];
	int npend;
	char *order[XR_PENDING + 2];	/* pendingOrder, ids may repeat */
	int norder;
	char **threads;
	int nthreads, threads_full;
	char cwd[XR_PATH_MAX + 1];
	/* the line being decoded */
	uint64_t off;
	long long at;
};

enum { B_PROMPT, B_IMAGE, B_STEP };

struct xr_claude *
xr_claude_new(void)
{
	return calloc(1, sizeof(struct xr_claude));
}

void
xr_claude_free(struct xr_claude *d)
{
	int k;

	if (!d)
		return;
	for (k = 0; k < d->npend; k++)
		free(d->pend[k].id);
	for (k = 0; k < d->norder; k++)
		free(d->order[k]);
	for (k = 0; k < d->nthreads; k++)
		free(d->threads[k]);
	free(d->threads);
	xr_chapters_free(&d->ch);
	free(d);
}

const char *
xr_claude_cwd(const struct xr_claude *d)
{
	return d->cwd;
}

static struct xr_chapter *
cur(struct xr_claude *d)
{
	return d->turn ? ch_ord(&d->ch, d->ch.total - 1) : NULL;
}

/* A block on the main thread: turns()'s rule. kind/title for a prompt. */
static void
main_block(struct xr_claude *d, int b, const char *kind, const char *title)
{
	struct xr_chapter *x;

	if (!d->turn || (b == B_PROMPT && !d->lead))
	{
		if (!(x = ch_push(&d->ch)))
			return;
		d->turn = 1;
		d->lead = 1;
		d->titled = 0;
		x->offset = d->off;
		x->at = d->at;
		x->partial = b != B_PROMPT;
	}
	x = cur(d);
	if (x && b == B_PROMPT && !d->titled)
	{
		/* a partial first chapter takes the prompt riding in its lead */
		x->kind = kind;
		snprintf(x->title, sizeof x->title, "%s", title);
		d->titled = 1;
	}
	if (b != B_PROMPT && b != B_IMAGE)
		d->lead = 0;
	d->bare = 0;
}

/* An id as kept: itself up to XR_ID_MAX bytes, else 0xff (never in UTF-8,
** so never a short id: dch_json_parse refuses a line that is not UTF-8 and
** the decode is checked again, QA 1.45.1 NIT-2) and its 64-bit FNV-1a in
** hex. Pairing and thread identity need only equality; a long id is never
** held whole (QA 1.44 MED-2). */
#define XR_ID_MAX 64

static const char *
id_key(const char *id, char key[XR_ID_MAX + 1])
{
	uint64_t h = 0xcbf29ce484222325ULL;
	const unsigned char *u = (const unsigned char *)id;

	if (strlen(id) <= XR_ID_MAX)
		return id;
	for (; *u; u++)
		h = (h ^ *u) * 0x100000001b3ULL;
	snprintf(key, XR_ID_MAX + 1, "\xff%016llx", (unsigned long long)h);
	return key;
}

/* A block on a sidechain: its thread's first one adds the thread (a step)
** to the main thread. */
static void
side_block(struct xr_claude *d, const char *agent)
{
	char kb[XR_ID_MAX + 1];
	int k;

	agent = id_key(agent, kb);

	for (k = 0; k < d->nthreads; k++)
		if (!strcmp(d->threads[k], agent))
			return;
	if (d->nthreads < XR_THREADS)
	{
		char **nt = realloc(d->threads, (size_t)(d->nthreads + 1) *
		                    sizeof *nt);

		if (nt && (nt[d->nthreads] = strdup(agent)))
		{
			d->threads = nt;
			d->nthreads++;
		}
		else if (nt)
			d->threads = nt;
	}
	else
		d->threads_full = 1;	/* shortcut: past 1024 subagents each
					** sidechain block reads as a new thread */
	main_block(d, B_STEP, NULL, NULL);
}

static void
block(struct xr_claude *d, const char *agent, int b, const char *kind,
      const char *title)
{
	if (agent)
		side_block(d, agent);
	else
		main_block(d, b, kind, title);
}

static void
mark_error(struct xr_claude *d, long long ord)
{
	struct xr_chapter *x = ord >= 0 ? ch_ord(&d->ch, (uint64_t)ord) : NULL;

	if (x)
		x->error = 1;
}

static int
pend_find(struct xr_claude *d, const char *id)
{
	int k;

	for (k = 0; k < d->npend; k++)
		if (!strcmp(d->pend[k].id, id))
			return k;
	return -1;
}

static void
pend_drop(struct xr_claude *d, int k)
{
	free(d->pend[k].id);
	d->pend[k] = d->pend[--d->npend];
}

static void
tool_use(struct xr_claude *d, const char *agent, const char *id, int quiet)
{
	char kb[XR_ID_MAX + 1], *c;
	int k;

	block(d, agent, B_STEP, NULL, NULL);
	if (!id)
		return;
	if ((k = pend_find(d, id = id_key(id, kb))) < 0)
	{
		if (!(c = strdup(id)))
			return;
		k = d->npend++;
		d->pend[k].id = c;
	}
	d->pend[k].ord = agent ? -1 : (long long)d->ch.total - 1;
	d->pend[k].quiet = quiet;
	if (!(d->order[d->norder] = strdup(d->pend[k].id)))
		return;
	d->norder++;
	if (d->norder > XR_PENDING)
	{
		/* the oldest open call goes (pending[removeFirst()] = nil) */
		if ((k = pend_find(d, d->order[0])) >= 0)
			pend_drop(d, k);
		free(d->order[0]);
		memmove(d->order, d->order + 1, (size_t)--d->norder *
		        sizeof *d->order);
	}
}

static void
tool_result(struct xr_claude *d, const char *agent, const char *id, int err)
{
	char kb[XR_ID_MAX + 1];
	int k, j, w;

	if (id && (k = pend_find(d, id = id_key(id, kb))) >= 0)
	{
		if (err && !d->pend[k].quiet)
			mark_error(d, d->pend[k].ord);
		pend_drop(d, k);
		for (j = w = 0; j < d->norder; j++)
			if (strcmp(d->order[j], id))
				d->order[w++] = d->order[j];
			else
				free(d->order[j]);
		d->norder = w;
		return;
	}
	/* an orphan: a generic "Tool result" card, a step with its isError */
	block(d, agent, B_STEP, NULL, NULL);
	if (err && !agent)
		mark_error(d, (long long)d->ch.total - 1);
}

/* texts(): each part's text, or "[type]"; joined by "\n"; NULL for none. */
static char *
texts(const struct json_parser *p, int arr, size_t *n)
{
	char *o = NULL, *s, *no;
	size_t k = 0, l;
	int i, any = 0;

	for (i = arr + 1; i < p->tok[arr].next; i = p->tok[i].next)
	{
		if (p->tok[i].kind != JSON_OBJECT)
			continue;
		if (!(s = k_str(p, i, A_TEXT, &l)))
		{
			char *t = k_str(p, i, A_TYPE, &l);

			if (!t)
				continue;
			s = malloc(l + 3);
			if (s)
				snprintf(s, l + 3, "[%s]", t);
			free(t);
			if (!s)
				continue;
			l += 2;
		}
		if (!(no = realloc(o, k + l + 2)))
		{
			free(s);
			break;
		}
		o = no;
		if (any)
			o[k++] = '\n';
		memcpy(o + k, s, l);
		k += l;
		o[k] = '\0';
		any = 1;
		free(s);
	}
	*n = k;
	return o;
}

/* A user text block: a slash command, else a prompt; blank is hidden. */
static void
user_text(struct xr_claude *d, const char *agent, char *s, size_t n)
{
	char title[XR_TITLE_MAX * 4 + 1];

	if (command(s, n, title, sizeof title))
	{
		block(d, agent, B_PROMPT, "command", title);
		return;
	}
	if (blank(s, n))
		return;
	xr_title(s, unwrap(s, n), title, sizeof title);	/* s is spent */
	block(d, agent, B_PROMPT, "prompt", title);
}

static void
compaction(struct xr_claude *d, const char *agent, int summary)
{
	if (summary && !agent && d->bare)
	{
		d->bare = 0;	/* mergeSummary: into the boundary before it */
		return;
	}
	block(d, agent, B_PROMPT, "compact", "Conversation compacted");
	if (!agent && !summary)
		d->bare = 1;
}

static void
entry_user(struct xr_claude *d, const struct json_parser *p, const char *ag)
{
	int m, c, i;
	size_t n;
	char *s;

	if (k_bool(p, 0, A_META))
		return;
	m = k_obj(p, 0, A_MESSAGE);
	c = m >= 0 ? j_get(p, m, A_CONTENT) : -1;
	if (k_bool(p, 0, A_SUMMARY))
	{
		n = 0;
		s = c >= 0 && p->tok[c].kind == JSON_STRING ? j_str(p, c, &n)
		    : c >= 0 && p->tok[c].kind == JSON_ARRAY ? texts(p, c, &n)
		    : NULL;
		free(s);
		compaction(d, ag, n > 0);
		return;
	}
	if (c >= 0 && p->tok[c].kind == JSON_STRING)
	{
		if ((s = j_str(p, c, &n)))
			user_text(d, ag, s, n);
		free(s);
		return;
	}
	if (c < 0 || p->tok[c].kind != JSON_ARRAY)
	{
		block(d, ag, B_STEP, NULL, NULL);	/* "user without content" */
		return;
	}
	{
		/* the text parts joined, the prompt first, then the rest */
		char *text = NULL, *nt;
		size_t tl = 0;

		for (i = c + 1; i < p->tok[c].next; i = p->tok[i].next)
		{
			char *t;
			size_t l = 0;

			if (p->tok[i].kind != JSON_OBJECT ||
			    !k_streq(p, i, A_TYPE, "text"))
				continue;
			t = k_str(p, i, A_TEXT, &l);
			if ((nt = realloc(text, tl + l + 2)))
			{
				text = nt;
				if (tl)
					text[tl++] = '\n';
				if (t)
					memcpy(text + tl, t, l);
				tl += l;
				text[tl] = '\0';
			}
			free(t);
		}
		if (text)
			user_text(d, ag, text, tl);
		free(text);
	}
	for (i = c + 1; i < p->tok[c].next; i = p->tok[i].next)
	{
		char *t;

		if (p->tok[i].kind != JSON_OBJECT)
			continue;
		t = k_str(p, i, A_TYPE, NULL);
		if (t && !strcmp(t, "text"))
			;
		else if (t && !strcmp(t, "image"))
			block(d, ag, B_IMAGE, NULL, NULL);
		else if (t && !strcmp(t, "tool_result"))
		{
			char *id = k_str(p, i, A_TUID, NULL);

			tool_result(d, ag, id, k_bool(p, i, A_ERR));
			free(id);
		}
		else
			block(d, ag, B_STEP, NULL, NULL);	/* unrecognized */
		free(t);
	}
}

static void
entry_assistant(struct xr_claude *d, const struct json_parser *p,
                const char *ag)
{
	int m = k_obj(p, 0, A_MESSAGE), c, i;
	size_t n;
	char *s;

	c = m >= 0 ? j_get(p, m, A_CONTENT) : -1;
	if (c >= 0 && p->tok[c].kind == JSON_STRING)
	{
		if ((s = j_str(p, c, &n)) && !blank(s, n))
			block(d, ag, B_STEP, NULL, NULL);
		free(s);
		return;
	}
	if (c < 0 || p->tok[c].kind != JSON_ARRAY)
	{
		block(d, ag, B_STEP, NULL, NULL);
		return;
	}
	for (i = c + 1; i < p->tok[c].next; i = p->tok[i].next)
	{
		char *t, *id, *name;
		int in, pat;

		if (p->tok[i].kind != JSON_OBJECT)
			continue;
		t = k_str(p, i, A_TYPE, NULL);
		if (t && !strcmp(t, "text"))
		{
			if ((s = k_str(p, i, A_TEXT, &n)) && !blank(s, n))
				block(d, ag, B_STEP, NULL, NULL);
			free(s);
		}
		else if (t && (!strcmp(t, "thinking") ||
		               !strcmp(t, "redacted_thinking")))
		{
			if ((s = k_str(p, i, A_THINK, &n)) && !blank(s, n))
				block(d, ag, B_STEP, NULL, NULL);
			free(s);
		}
		else if (t && (!strcmp(t, "tool_use") ||
		               !strcmp(t, "server_tool_use")))
		{
			int quiet = 0;

			id = k_str(p, i, A_ID, NULL);
			name = k_str(p, i, A_NAME, NULL);
			in = k_obj(p, i, A_INPUT);
			if (name && (!strcmp(name, "Grep") || !strcmp(name, "Glob")) &&
			    in >= 0 && (pat = j_get(p, in, A_PATTERN)) >= 0 &&
			    p->tok[pat].kind == JSON_STRING &&
			    p->tok[pat].end > p->tok[pat].start)
				quiet = 1;
			tool_use(d, ag, id, quiet);
			free(id);
			free(name);
		}
		else
			block(d, ag, B_STEP, NULL, NULL);
		free(t);
	}
}

static const char *const noise_types[] = {
	"queue-operation", "atis-latch", "last-prompt", "mode",
	"permission-mode", "ai-title", "custom-title", "agent-name",
	"api-request", "api-request-blob", "api-request-shape",
	"file-history-snapshot", "file-history-delta", "cost-state",
	"bridge-session", "session-owner", "pr-link", "frame-link", "tools-used",
	"artifact-autoreact-ledger", "artifact-comment-monitor", "summary", 0
};
static const char *const noise_sub[] = {
	"turn_duration", "stop_hook_summary", "scheduled_task_fire",
	"agents_killed", 0
};

static int
listed(const char *const *v, const char *s)
{
	for (; *v; v++)
		if (!strcmp(*v, s))
			return 1;
	return 0;
}

/* An upper bound on the line's JSON tokens: one per value and key, each
** after an opener, ',' or ':' outside strings. */
static size_t
tokens_max(const char *s, size_t n)
{
	size_t k, c = 1;
	int in = 0;

	for (k = 0; k < n; k++)
		if (in)
		{
			if (s[k] == '\\')
				k++;
			else if (s[k] == '"')
				in = 0;
		}
		else if (s[k] == '"')
			in = 1;
		else if (s[k] == ',' || s[k] == ':' || s[k] == '[' || s[k] == '{')
			c++;
	return c;
}

void
xr_claude_line(struct xr_claude *d, const char *line, size_t len,
               uint64_t off, int oversized)
{
	struct json_parser p;
	char *type = NULL, *agent = NULL, *s;
	const char *ag = NULL;
	size_t n;

	d->off = off;
	d->at = -1;
	memset(&p, 0, sizeof p);
	/* shortcut: a line past XR_LINE_TOKENS is one failed step, as an
	** oversized one; upgrade to a streaming tokenizer if real lines do */
	if (oversized || memchr(line, '\0', len) ||
	    tokens_max(line, len) > XR_LINE_TOKENS ||
	    dch_json_parse(&p, line) < 0 || p.tok[0].kind != JSON_OBJECT)
	{
		dch_json_free(&p);
		main_block(d, B_STEP, NULL, NULL);	/* a failed block */
		return;
	}
	if ((s = k_str(&p, 0, A_TS, NULL)))
		d->at = iso_time(s);
	free(s);
	if ((s = k_str(&p, 0, A_CWD, &n)) && s[0] == '/' &&
	    scalars(s, n) <= XR_PATH_MAX && n < sizeof d->cwd)
		memcpy(d->cwd, s, n + 1);
	free(s);
	if (k_bool(&p, 0, A_SIDE))
		ag = (agent = k_str(&p, 0, A_AGENT, NULL)) ? agent : "subagent";
	type = k_str(&p, 0, A_TYPE, NULL);
	if (type && listed(noise_types, type))
		;
	else if (type && !strcmp(type, "user"))
		entry_user(d, &p, ag);
	else if (type && !strcmp(type, "assistant"))
		entry_assistant(d, &p, ag);
	else if (type && !strcmp(type, "system"))
	{
		char *sub = k_str(&p, 0, A_SUB, NULL);

		if (sub && !strcmp(sub, "compact_boundary"))
			compaction(d, ag, 0);
		else if (!sub || !listed(noise_sub, sub))
			block(d, ag, B_STEP, NULL, NULL);	/* notice or card */
		free(sub);
	}
	else if (type && !strcmp(type, "attachment"))
	{
		int a = k_obj(&p, 0, A_ATTACH);

		/* a prompt typed while the agent worked (no line source in
		** Prosper's clip, but the block's source is the line) */
		if (a >= 0 && k_streq(&p, a, A_TYPE, "queued_command") &&
		    (s = k_str(&p, a, A_PROMPT, &n)))
		{
			if (!blank(s, n))
				user_text(d, ag, s, n);
			free(s);
		}
	}
	else
		block(d, ag, B_STEP, NULL, NULL);	/* unrecognized entry */
	free(type);
	free(agent);
	dch_json_free(&p);
}

void
xr_claude_end(struct xr_claude *d, uint64_t ino, struct xr_chapters *out)
{
	size_t k;

	for (k = 0; k < d->ch.n; k++)
	{
		struct xr_chapter *x = &d->ch.v[(d->ch.start + k) % XR_CHAPTERS];

		snprintf(x->anchor, sizeof x->anchor, "f%llx:%llu",
		         (unsigned long long)ino, (unsigned long long)x->offset);
	}
	*out = d->ch;
	memset(&d->ch, 0, sizeof d->ch);
	d->turn = 0;
}

/* One range [from, to) of fd through the line splitter (Prosper's
** LineBuffer, with 3.8's 1 MB line cap). `mid`: from is not a line start
** unless the byte before it is '\n', so the bytes up to the first '\n' go. */
static int
read_range(struct xr_claude *d, int fd, uint64_t from, uint64_t to, int mid,
           char *buf, char *acc)
{
	uint64_t pos = from, start = from;
	size_t fill = 0;
	int skipping = 0, drop = 0;

	if (mid && from > 0)
	{
		char c;

		if (pread(fd, &c, 1, (off_t)(from - 1)) != 1)
			return -1;
		drop = c != '\n';
	}
	while (pos < to)
	{
		size_t want = to - pos > xr_lim.chunk ? xr_lim.chunk
		              : (size_t)(to - pos);
		ssize_t got = pread(fd, buf, want, (off_t)pos);
		size_t i = 0;

		if (got < 0 && errno == EINTR)
			continue;
		if (got <= 0)	/* shrank: what was read stands, incomplete */
			return got < 0 ? -1 : 1;
		while (i < (size_t)got)
		{
			char *nl = memchr(buf + i, '\n', (size_t)got - i);
			size_t seg = nl ? (size_t)(nl - (buf + i)) : (size_t)got - i;

			if (!drop && !skipping)
			{
				if (fill + seg > xr_lim.line)
				{
					skipping = 1;
					fill = 0;
				}
				else
				{
					memcpy(acc + fill, buf + i, seg);
					fill += seg;
				}
			}
			if (!nl)
				break;
			if (drop)
				drop = 0;
			else if (skipping)
				xr_claude_line(d, "", 0, start, 1);
			else
			{
				size_t k;

				for (k = 0; k < fill && (acc[k] == ' ' || acc[k] == '\t' ||
				                         acc[k] == '\r'); k++)
					;
				acc[fill] = '\0';
				if (k < fill)
					xr_claude_line(d, acc, fill, start, 0);
			}
			skipping = 0;
			fill = 0;
			i += seg + 1;
			start = pos + i;
		}
		pos += (uint64_t)got;
	}
	/* a line still open at the range's end (the head's cut, or one the
	** agent is writing) waits, as LineBuffer's pending does: dropped */
	return 0;
}

int
xr_claude_read(struct xr_claude *d, int fd, uint64_t size, int *capped)
{
	char *buf = malloc(XR_CHUNK), *acc = malloc(XR_LINE_MAX + 1);
	int rc = -1;

	*capped = 0;
	if (buf && acc)
	{
		if (size <= xr_lim.head + xr_lim.tail)
			rc = read_range(d, fd, 0, size, 0, buf, acc);
		else
		{
			*capped = 1;
			rc = read_range(d, fd, 0, xr_lim.head, 0, buf, acc);
			if (rc == 0)
				rc = read_range(d, fd, size - xr_lim.tail, size, 1,
				                buf, acc);
		}
		if (rc == 1)
		{
			*capped = 1;
			rc = 0;
		}
	}
	free(buf);
	free(acc);
	return rc;
}

/* ---- shell ring ------------------------------------------------------------ */

long long
xr_ring_walk(const unsigned char *buf, size_t bl, uint64_t first,
             uint32_t count, void (*fn)(void *ctx, const struct xr_rec *x),
             void *ctx)
{
	size_t off = 0;
	uint32_t got = 0;

	while (off + 8 <= bl && got < count)
	{
		size_t l = (size_t)buf[off] | (size_t)buf[off + 1] << 8;
		struct xr_rec x;

		if (l > bl - off - 8)
			return -1;
		x.seq = first + got;
		x.kind = buf[off + 2];
		x.flags = buf[off + 3];
		x.t = (uint32_t)buf[off + 4] | (uint32_t)buf[off + 5] << 8 |
		      (uint32_t)buf[off + 6] << 16 | (uint32_t)buf[off + 7] << 24;
		x.text = (const char *)buf + off + 8;
		x.len = l;
		fn(ctx, &x);
		got++;
		off += 8 + l;
	}
	return off == bl && got == count ? (long long)off : -1;
}

/* D;<n>'s exit status. Record text is not NUL-terminated: copy first. */
static long
mark_exit(const struct xr_rec *x)
{
	char eb[24];
	size_t el = x->len - 2 < sizeof eb - 1 ? x->len - 2 : sizeof eb - 1;

	memcpy(eb, x->text + 2, el);
	eb[el] = '\0';
	return strtol(eb, NULL, 10);
}

enum { S_NONE, S_PROMPT, S_INPUT, S_OUTPUT };

/* A B or C inside the open C..D: that block is an orphan (7.3), so the
** chapter pushed at its C goes again, as a forged C never gets one. */
static void
sh_orphan(struct xr_shell *s)
{
	if (s->st == S_OUTPUT && s->ord >= 0 &&
	    (uint64_t)s->ord + 1 == s->out->total && s->out->n)
	{
		s->out->n--;
		s->out->total--;
		s->npaired--;
	}
	s->st = S_NONE;
	s->ord = -1;
}

void
xr_shell_begin(struct xr_shell *s, struct xr_chapters *out)
{
	memset(s, 0, sizeof *s);
	memset(out, 0, sizeof *out);
	s->out = out;
	s->st = S_NONE;
	s->ord = -1;
}

void
xr_shell_feed(struct xr_shell *s, const struct xr_rec *x)
{
	char m;

	if (x->kind == RING_K_GAP)
	{
		s->st = S_NONE;	/* capture lost or paused: the block is gone */
		return;
	}
	if (x->kind == RING_K_PROMPT && s->st == S_PROMPT && !s->havep)
	{
		s->aseq = x->seq;
		s->at = x->t;
		s->havep = 1;
		return;
	}
	if (x->kind == RING_K_INPUT && s->st == S_INPUT)
	{
		size_t l = x->len;

		if (s->cl && !(x->flags & RING_F_CONT) && s->cl < XR_CMD_MAX)
			s->cmd[s->cl++] = '\n';
		if (l > XR_CMD_MAX - s->cl)
			l = XR_CMD_MAX - s->cl;
		memcpy(s->cmd + s->cl, x->text, l);
		s->cl += l;
		return;
	}
	if (x->kind != RING_K_MARK || !x->len ||
	    (x->len > 1 && x->text[1] != ';'))
		return;
	m = x->text[0];
	if (m == 'A')
	{
		if (x->len > 1)
			return;	/* A;k: a secondary prompt */
		s->alt = (x->flags & RING_F_ALT) != 0;
		if (s->st == S_PROMPT)
			return;	/* a redraw before B */
		s->st = S_PROMPT;
		s->aseq = x->seq;
		s->at = x->t;
		s->havep = 0;
		s->ord = -1;
	}
	else if (m == 'B')
	{
		if (s->st == S_PROMPT)
		{
			s->st = S_INPUT;
			s->cl = 0;
		}
		else
			sh_orphan(s);	/* B outside A..B: an orphan */
	}
	else if (m == 'C')
	{
		const char *c = x->len > 2 ? x->text + 2 : s->cmd;
		size_t l = x->len > 2 ? x->len - 2 : s->cl;
		struct xr_chapter *ch;

		if (s->st != S_INPUT)
		{
			sh_orphan(s);	/* C without B */
			return;
		}
		if (!(x->flags & RING_F_INPUT))
		{
			/* no client input between B and C: forged by output
			** (7.3, QA677 F4), an orphan */
			s->st = S_NONE;
			return;
		}
		if (x->len > 2 && blank(c, l))
		{
			c = s->cmd;
			l = s->cl;
		}
		s->st = S_OUTPUT;
		if (blank(c, l) || !(ch = ch_push(s->out)))
			return;	/* an empty command is an orphan */
		s->ord = (long long)s->out->total - 1;
		ch->offset = s->aseq;
		ch->at = s->at;
		ch->kind = "command";
		snprintf(ch->anchor, sizeof ch->anchor, "s%llu",
		         (unsigned long long)s->aseq);
		xr_title(c, l, ch->title, sizeof ch->title);
		s->npaired++;
	}
	else if (m == 'D')
	{
		struct xr_chapter *ch;

		if (s->st == S_OUTPUT && s->ord >= 0 &&
		    (ch = ch_ord(s->out, (uint64_t)s->ord)) &&
		    (x->flags & RING_F_EXIT) && x->len > 2 && mark_exit(x) != 0)
			ch->error = 1;
		s->st = S_NONE;
		s->ord = -1;
	}
}

void
xr_shell_chapters(const struct xr_rec *r, size_t n, struct xr_chapters *out,
                  int *paired, int *alt)
{
	struct xr_shell s;
	size_t k;

	xr_shell_begin(&s, out);
	for (k = 0; k < n; k++)
		xr_shell_feed(&s, &r[k]);
	*paired = s.npaired != 0;
	*alt = s.alt;
}

/* ---- the project key -------------------------------------------------------- */

void
xr_project_key(const char *cwd, char *out, size_t outsz)
{
	const unsigned char *u = (const unsigned char *)cwd;
	size_t n = strlen(cwd), i, a, o = 0, units = 0;
	int32_t h = 0;
	char tail[16];

	if (!outsz)
		return;
	for (i = 0; i < n; i += a)
	{
		uint32_t c = u8_next(u + i, n - i, &a);
		int w = c >= 0x10000 ? 2 : 1;
		uint32_t cu[2];

		/* the int32 Java hash over UTF-16 units, as Claude's Cbe() */
		if (w == 2)
		{
			cu[0] = 0xd800 + ((c - 0x10000) >> 10);
			cu[1] = 0xdc00 + ((c - 0x10000) & 0x3ff);
		}
		else
			cu[0] = c;
		for (int j = 0; j < w; j++)
		{
			h = (int32_t)((uint32_t)h * 31u + cu[j]);
			if (units < 200 && o + 1 < outsz)
				out[o++] = c < 0x80 && isalnum((int)c) ? (char)c : '-';
			units++;
		}
	}
	out[o] = '\0';
	if (units > 200)
	{
		/* Math.abs(h).toString(36) */
		uint32_t v = h < 0 ? (uint32_t)0 - (uint32_t)h : (uint32_t)h;
		char *p = tail + sizeof tail;

		*--p = '\0';
		do
			*--p = "0123456789abcdefghijklmnopqrstuvwxyz"[v % 36];
		while (v /= 36);
		if (o + 1 + strlen(p) < outsz)
			snprintf(out + o, outsz - o, "-%s", p);
	}
}

/* ---- output ---------------------------------------------------------------- */

void
xr_put_str(FILE *f, const char *s)
{
	size_t cap = strlen(s) * 6 + 3;
	char *b = malloc(cap);

	if (b && dch_json_quote(b, cap, s) != SIZE_MAX)
		fputs(b, f);
	else
		fputs("\"\"", f);
	free(b);
}

static void
put_or_null(FILE *f, const char *s)
{
	if (s)
		xr_put_str(f, s);
	else
		fputs("null", f);
}

static void
put_chapter(FILE *m, const struct xr_chapter *c, uint64_t i, int comma)
{
	fputs(comma ? ",{\"anchor\":" : "{\"anchor\":", m);
	xr_put_str(m, c->anchor);
	fprintf(m, ",\"offset\":%llu,\"i\":%llu,\"at\":",
	        (unsigned long long)c->offset, (unsigned long long)i);
	if (c->at >= 0)
		fprintf(m, "%lld", c->at);
	else
		fputs("null", m);
	fputs(",\"title\":", m);
	xr_put_str(m, c->title);
	fprintf(m, ",\"kind\":\"%s\",\"partial\":%s,\"error\":%s}",
	        c->kind, c->partial ? "true" : "false",
	        c->error ? "true" : "false");
}

/* Two passes: each chapter's size first (one at a time, so no 4 MiB body is
** held: QA 1.44 LOW-8), then the kept ones straight to f. */
int
xr_print(FILE *f, const struct xr_answer *a)
{
	char *body = NULL, *head = NULL;
	size_t bl = 0, hl = 0, k, cut = 0, n = a->ch ? a->ch->n : 0, slack;
	size_t *at = calloc(n + 1, sizeof *at);
	uint64_t first = a->ch ? a->ch->total - a->ch->n : 0;
	FILE *m;
	int complete = a->complete, rc = 0;
	const char *why = a->why;

	if (!at || !(m = open_memstream(&body, &bl)))
	{
		free(at);
		return -1;
	}
	for (k = 0; k < n && fseek(m, 0, SEEK_SET) == 0; k++)
	{
		put_chapter(m, xr_chapter_at(a->ch, k), first + k + 1, k > 0);
		at[k + 1] = at[k] + (size_t)ftell(m);
	}
	if (fclose(m) != 0 || k < n)
	{
		free(body);
		free(at);
		return -1;
	}
	free(body);
	body = NULL;
	bl = at[n];
	/* 4 MiB: the oldest chapters go first (refs are empty in XR1) */
	slack = 4096 + 6 * (strlen(a->session) + strlen(a->session_id) +
	                    (a->project ? strlen(a->project) : 0) +
	                    (a->project_label ? strlen(a->project_label) : 0));
	while (cut < n && bl - at[cut] + slack > xr_lim.out)
		cut++;
	if (cut)
	{
		complete = 0;
		why = "output";
	}
	if (!(m = open_memstream(&head, &hl)))
	{
		free(body);
		free(at);
		return -1;
	}
	fputs("{\"schema\":1,\"session\":", m);
	xr_put_str(m, a->session);
	fputs(",\"session_id\":", m);
	xr_put_str(m, a->session_id);
	fprintf(m, ",\"live\":%s,\"corpus\":\"%s\",\"reason\":",
	        a->live ? "true" : "false", a->corpus);
	put_or_null(m, a->reason);
	fprintf(m, ",\"available\":%s,\"mentions_only\":%s,\"lines\":",
	        a->available ? "true" : "false",
	        a->mentions_only ? "true" : "false");
	if (a->lines >= 0)
		fprintf(m, "%lld", a->lines);
	else
		fputs("null", m);
	fprintf(m, ",\"complete\":%s,\"why\":", complete ? "true" : "false");
	put_or_null(m, complete ? NULL : why);
	fputs(",\"project\":", m);
	xr_put_str(m, a->project ? a->project : "");
	fputs(",\"project_label\":", m);
	xr_put_str(m, a->project_label ? a->project_label : "");
	fputs(",\"other_projects\":0,\"chapters\":[", m);
	if (fclose(m) != 0)
		rc = -1;
	if (rc == 0)
	{
		fputs(head, f);
		for (k = cut; k < n; k++)	/* the first kept one: no comma */
			put_chapter(f, xr_chapter_at(a->ch, k), first + k + 1,
			            k > cut);
		fprintf(f, "],\"chapters_evicted\":%s,\"refs\":[],"
		        "\"dropped_keys\":0}\n",
		        a->chapters_evicted ? "true" : "false");
		if (fflush(f) != 0 || ferror(f))
			rc = -1;
	}
	free(head);
	free(at);
	return rc;
}
