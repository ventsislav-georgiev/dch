/*
 * xray.c - the X-ray engine's one-shot half (#120, XR1b). See xray.h.
 *
 * Claude chapters follow Prosper's TranscriptDecoder, TranscriptAssembler and
 * TranscriptVerbosity.turns() rule for rule (spec 5.3, 9.3 parity): the same
 * key aliases, the same hidden noise, the same prompt/step/image split, the
 * same tool pairing (256 open calls) and sidechain threads. Only what decides
 * a chapter's start, kind, title, offset and error is kept; block bodies are
 * never built, except one chapter's entries for --chapter (#156), clipped.
 */
#include <config.h>

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <sys/wait.h>
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
                           XR_OUT_MAX, XR_FILES, XR_BYTES, XR_WALL_MS};

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

/* ---- refs: the grammar (v2.1 2.2-2.5; #135, XR2a) ---------------------- */

#include "xray_uni.h"

const char *const xr_shape_name[] = { "ticket", "issue", "pull", "commit",
                                      "file", "url" };

#define A_DIG(c) ((c) >= '0' && (c) <= '9')
#define A_UP(c) ((c) >= 'A' && (c) <= 'Z')
#define A_LO(c) ((c) >= 'a' && (c) <= 'z')
#define A_AL(c) (A_UP(c) || A_LO(c))
#define A_ALNUM(c) (A_AL(c) || A_DIG(c))
#define A_HEX(c) (A_DIG(c) || ((c) >= 'a' && (c) <= 'f'))

/* \p{L}, \p{N} or '_': the boundary class, (?<![\p{L}\p{N}_]) and
** (?![\p{L}\p{N}_]) (v2.1 2.2; QA F1.4: `#831é` is no `#831`). */
static int
u_word(uint32_t c)
{
	size_t lo = 0, hi = sizeof xr_uni_ln / sizeof xr_uni_ln[0];

	if (c < 0x80)
		return A_ALNUM(c) || c == '_';
	while (lo < hi)
	{
		size_t mid = (lo + hi) / 2;

		if (c < xr_uni_ln[mid][0])
			hi = mid;
		else if (c > xr_uni_ln[mid][1])
			lo = mid + 1;
		else
			return 1;
	}
	return 0;
}

/* The scalar ending at i (0 at the start); U+FFFD when the bytes before i
** are no whole scalar. */
static uint32_t
prev_scalar(const char *s, size_t i)
{
	size_t k, a;
	uint32_t c;

	if (!i)
		return 0;
	k = i - 1;
	while (k > 0 && i - k < 4 && ((unsigned char)s[k] & 0xc0) == 0x80)
		k--;
	c = u8_next((const unsigned char *)s + k, i - k, &a);
	return k + a == i ? c : 0xfffd;
}

static int
after_ok(const char *s, size_t n, size_t e)
{
	size_t a;

	return e >= n || !u_word(u8_next((const unsigned char *)s + e, n - e, &a));
}

/* The first `cap` scalars of s[0..n): their byte length (R3: scalars, never
** bytes or graphemes). */
static size_t
clip(const char *s, size_t n, size_t cap)
{
	size_t i = 0, a, k = 0;

	while (i < n && k < cap)
	{
		(void)u8_next((const unsigned char *)s + i, n - i, &a);
		i += a;
		k++;
	}
	return i;
}

enum { RU = 1, RR = 2, RF = 4, RT = 8, Rr = 16, RS = 32, RH = 64 };
#define R_PROSE (RU | RR | RF | RT | Rr | RS | RH)
#define R_CODE (RU | RR | RF | RS)		/* tool fields, code spans */
#define R_ANCHOR (RR | RT | Rr | RH)		/* definition anchors (2.4) */
#define R_D3 (RU | RR | RT | Rr | RH)		/* D3: no file, no commit */

static int
span_key(struct xr_span *sp, int shape, const char *t, size_t tl, int lower)
{
	size_t pl = strlen(xr_shape_name[shape]), k;

	if (!tl || tl > XR_KEY_MAX)
		return 0;
	memcpy(sp->key, xr_shape_name[shape], pl);
	sp->key[pl] = ':';
	for (k = 0; k < tl; k++)
		sp->key[pl + 1 + k] = lower && A_UP((unsigned char)t[k])
		                      ? (char)(t[k] + 32) : t[k];
	sp->key[pl + 1 + tl] = '\0';
	sp->shape = shape;
	return 1;
}

const char *
xr_span_text(const struct xr_span *sp)
{
	return sp->key + strlen(xr_shape_name[sp->shape]) + 1;
}

/* A GitHub owner or repo name: [A-Za-z0-9._-], not "." or "..". */
static int
gh_name(const char *s, size_t n, size_t max)
{
	size_t k;

	if (!n || n > max || (n == 1 && s[0] == '.') ||
	    (n == 2 && s[0] == '.' && s[1] == '.'))
		return 0;
	for (k = 0; k < n; k++)
		if (!A_ALNUM((unsigned char)s[k]) && s[k] != '-' && s[k] != '_' &&
		    s[k] != '.')
			return 0;
	return 1;
}

/* U's kind (v2.1 2.3), validated like DchSessionMeta.PR.pullURL: exactly
** https://github.com/<o>/<r>/(pull|issues)/<1-9 digits> is a pull,
** .../commit/<7-40 lowercase hex> a commit; anything else a url. */
static void
url_kind(const char *s, size_t n, struct xr_span *sp)
{
	static const char gh[] = "https://github.com/";
	const char *p[4];
	size_t pl[4], k, j = sizeof gh - 1, np = 0, d;
	char t[200];

	if (n > j && !memcmp(s, gh, j))
	{
		while (np < 4)
		{
			p[np] = s + j;
			for (k = j; k < n && s[k] != '/'; k++)
				;
			pl[np++] = k - j;
			if (k >= n)
				break;
			j = k + 1;
		}
		if (np == 4 && p[3] + pl[3] == s + n && gh_name(p[0], pl[0], 39) &&
		    gh_name(p[1], pl[1], 100))
		{
			for (d = 0; d < pl[3] && A_DIG((unsigned char)p[3][d]); d++)
				;
			if (((pl[2] == 4 && !memcmp(p[2], "pull", 4)) ||
			     (pl[2] == 6 && !memcmp(p[2], "issues", 6))) &&
			    d == pl[3] && d >= 1 && d <= 9)
			{
				k = (size_t)snprintf(t, sizeof t, "%.*s/%.*s#%.*s",
				                     (int)pl[0], p[0], (int)pl[1], p[1],
				                     (int)pl[3], p[3]);
				(void)span_key(sp, XR_PULL, t, k, 1);
				return;
			}
			for (d = 0; d < pl[3] && A_HEX((unsigned char)p[3][d]); d++)
				;
			if (pl[2] == 6 && !memcmp(p[2], "commit", 6) && d == pl[3] &&
			    d >= 7 && d <= 40)
			{
				(void)span_key(sp, XR_COMMIT, p[3], 7, 0);
				memcpy(sp->full, p[3], d);
				sp->full[d] = '\0';
				return;
			}
		}
	}
	(void)span_key(sp, XR_URL, s, n, 0);
}

/* U: http(s)://, then anything but space, controls and <>"'`{}|\^; a
** trailing .,;:!? and an unbalanced ) or ] trimmed. */
static size_t
r_url(const char *s, size_t n, size_t i, struct xr_span *sp)
{
	size_t j, e, k, a;
	int o, c;

	sp->full[0] = '\0';	/* inline_run calls this directly (#147) */
	if (n - i >= 8 && !strncasecmp(s + i, "https://", 8))
		j = i + 8;
	else if (n - i >= 7 && !strncasecmp(s + i, "http://", 7))
		j = i + 7;
	else
		return 0;
	for (e = j; e < n; e += a)
	{
		unsigned char b = (unsigned char)s[e];
		uint32_t u;

		if (e - i > XR_KEY_MAX)
			return 0;
		if (b < 0x80)
		{
			if (b <= 0x20 || b == 0x7f || strchr("<>\"'`{}|\\^", b))
				break;
			a = 1;
			continue;
		}
		u = u8_next((const unsigned char *)s + e, n - e, &a);
		if (u_space(u) || u == 0xfffd || u < 0xa0 || u == 0x200b ||
		    (u >= 0x200e && u <= 0x200f) || (u >= 0x202a && u <= 0x202e) ||
		    (u >= 0x2066 && u <= 0x2069))
			break;
	}
	for (;;)
	{
		if (e > j && strchr(".,;:!?", s[e - 1]))
		{
			e--;
			continue;
		}
		if (e > j && (s[e - 1] == ')' || s[e - 1] == ']'))
		{
			char cl = s[e - 1], op = cl == ')' ? '(' : '[';

			for (k = i, o = c = 0; k < e; k++)
				o += s[k] == op, c += s[k] == cl;
			if (c > o)
			{
				e--;
				continue;
			}
		}
		break;
	}
	if (e == j || e - i > XR_KEY_MAX || !A_ALNUM((unsigned char)s[j]) ||
	    !after_ok(s, n, e))
		return 0;
	url_kind(s + i, e - i, sp);
	return e;
}

/* r's stoplist (v3.2.2 Q3), case-insensitive: glue words and standards. */
static int
r_stop(const char *s, size_t k)
{
	static const char *const stop[] = {
		"issue", "issues", "pull", "pulls", "item", "items", "step",
		"steps", "page", "pages", "line", "lines", "test", "tests", "case",
		"cases", "task", "tasks", "note", "notes", "part", "parts", "entry",
		"round", "phase", "build", "ticket", "number", "version", "link",
		"repo", "pkcs",
	};
	size_t m;

	for (m = 0; m < sizeof stop / sizeof stop[0]; m++)
		if (strlen(stop[m]) == k && !strncasecmp(stop[m], s, k))
			return 1;
	return 0;
}

/* R (owner) and r (no owner): {repo}#{n}. r's repo (v3.2.2 Q3): 3+ chars,
** a letter first, a letter or digit last, not in the stoplist. */
static size_t
r_repo(const char *s, size_t n, size_t i, uint32_t prev, int owner,
       struct xr_span *sp)
{
	size_t j = i, k, d;

	/* both forms: not after \w (the caller's boundary) or / . - */
	if (prev == '/' || prev == '.' || prev == '-')
		return 0;
	if (owner)
	{
		for (k = 0; j < n && k < 40 && (A_ALNUM((unsigned char)s[j]) ||
		                                s[j] == '-'); j++)
			k++;
		if (k < 1 || k > 39 || j >= n || s[j] != '/')
			return 0;
		j++;
	}
	else if (!A_AL((unsigned char)s[i]))
		return 0;
	for (k = 0; j < n && k <= 100 && (A_ALNUM((unsigned char)s[j]) ||
	                                  s[j] == '_' || s[j] == '.' ||
	                                  s[j] == '-'); j++)
		k++;
	if (k < 1 || k > 100 || j >= n || s[j] != '#' ||
	    (!owner && (k < 3 || !A_ALNUM((unsigned char)s[j - 1]) ||
	                r_stop(s + i, k))))
		return 0;
	for (d = 0, j++; j < n && d < 8 && A_DIG((unsigned char)s[j]); j++)
		d++;
	if (d < 1 || d > 7 || !after_ok(s, n, j) ||
	    !span_key(sp, owner ? XR_PULL : XR_ISSUE, s + i, j - i, 1))
		return 0;
	return j;
}

static int
path_c(unsigned char c)
{
	return A_ALNUM(c) || c == '_' || c == '.' || c == '-' || c == '/' ||
	       c == '~' || c == '+' || c == '@';
}

/* :line, :line:col (col 0: without) or #L<line>; the end, 0 for none. */
static size_t
line_sfx(const char *s, size_t n, size_t e, int col)
{
	size_t j = e, d, k;

	if (j < n && s[j] == ':')
	{
		for (d = 0, j++; j < n && d < 8 && A_DIG((unsigned char)s[j]); j++)
			d++;
		if (d < 1 || d > 7)
			return 0;
		if (col && j + 1 < n && s[j] == ':' && A_DIG((unsigned char)s[j + 1]))
		{
			for (d = 0, k = j + 1; k < n && d < 8 &&
			     A_DIG((unsigned char)s[k]); k++)
				d++;
			if (d <= 7)
				j = k;
		}
		return j;
	}
	if (j + 2 < n && s[j] == '#' && s[j + 1] == 'L' &&
	    A_DIG((unsigned char)s[j + 2]))
	{
		for (d = 0, j += 2; j < n && d < 8 && A_DIG((unsigned char)s[j]); j++)
			d++;
		return d <= 7 ? j : 0;
	}
	return 0;
}

/* Lexically absolute (2.5): "." and ".." and "//" folded, a relative path
** joined to cwd when that is absolute; "~/" kept, ".." at "~" dropped (Q4).
** 0 when it does not fit, or for "~user": no path (Q4). */
static size_t
path_norm(const char *p, size_t pl, const char *cwd, char *out, size_t outsz)
{
	char src[XR_PATH_MAX + XR_KEY_MAX + 4];
	size_t sl, k, cs, o = 0, nst = 0, st[XR_PATH_MAX / 2 + XR_KEY_MAX / 2 + 4];
	int root = p[0] == '/' ? 1 : p[0] == '~' ? 2 : 0;

	if (root == 2 && pl > 1 && p[1] != '/')
		return 0;

	if (!root && cwd && cwd[0] == '/' && strlen(cwd) <= XR_PATH_MAX)
	{
		sl = (size_t)snprintf(src, sizeof src, "%s/%.*s", cwd, (int)pl, p);
		root = 1;
	}
	else
		sl = (size_t)snprintf(src, sizeof src, "%.*s",
		                      (int)(pl - (root == 2)), p + (root == 2));
	if (sl >= sizeof src)
		return 0;
	for (k = 0; k <= sl; k = cs + 1)
	{
		size_t cl;

		for (cs = k; cs < sl && src[cs] != '/'; cs++)
			;
		cl = cs - k;
		if (!cl || (cl == 1 && src[k] == '.'))
			continue;
		if (cl == 2 && src[k] == '.' && src[k + 1] == '.')
		{
			if (nst && !(src[st[nst - 1]] == '.' &&
			             src[st[nst - 1] + 1] == '.' &&
			             (st[nst - 1] + 2 == sl ||
			              src[st[nst - 1] + 2] == '/')))
			{
				nst--;
				continue;
			}
			if (root)
				continue;	/* above the root: stays there */
		}
		st[nst++] = k;
	}
	if (root == 2)
		out[o++] = '~';
	for (k = 0; k < nst; k++)
	{
		size_t cl = strcspn(src + st[k], "/");

		if (o + cl + 2 >= outsz)
			return 0;
		if (root || k)
			out[o++] = '/';
		memcpy(out + o, src + st[k], cl);
		o += cl;
	}
	if (!o)
		out[o++] = root == 1 ? '/' : '.';
	out[o] = '\0';
	return o;
}

/* F: {path}, its line optional in prose and required in code and for a
** bare Name.ext. Not 1/2, and/or, 2026/11/09, Node.js, e.g., v1.2.3. */
static size_t
r_path(const char *s, size_t n, size_t i, uint32_t prev, int code,
       const char *cwd, struct xr_span *sp)
{
	size_t j = i, e, k, ls = 0, comps = 0, x, a, sfx, pl;
	unsigned char c = (unsigned char)s[i];
	int ext = 0, bare;
	char norm[XR_KEY_MAX + 1];

	if (prev == '/' || prev == '.' || prev == '-' || prev == '~' ||
	    prev == '+' || prev == '@' || c == '-' || c == '+' || c == '@' ||
	    (c == '~' && (i + 1 >= n || s[i + 1] != '/')))
		return 0;
	while (j < n)
	{
		unsigned char b = (unsigned char)s[j];

		if (j - i > XR_KEY_MAX)
			return 0;
		if (b < 0x80)
		{
			if (!path_c(b))
				break;
			j++;
		}
		else if (u_word(u8_next((const unsigned char *)s + j, n - j, &a)))
			j += a;
		else
			break;
	}
	for (e = j; e > i && s[e - 1] == '.'; e--)
		;
	if (e == i)
		return 0;
	bare = 1;
	ls = i;		/* the last component's start */
	for (k = x = i; k <= e; k++)
		if (k == e || s[k] == '/')
		{
			comps += k > x;
			if (k < e)
			{
				bare = 0;
				ls = k + 1;
			}
			x = k + 1;
		}
	comps -= s[i] == '~';	/* "~" names no part */
	/* the extension: 1-10 alnum after the last component's last dot, from
	** a letter, with a name before it */
	for (x = e; x > ls && s[x - 1] != '.'; x--)
		;
	if (x > ls + 1 && e - x >= 1 && e - x <= 10 && A_AL((unsigned char)s[x]))
	{
		ext = 1;
		for (k = x; k < e; k++)
			if (!A_ALNUM((unsigned char)s[k]))
				ext = 0;
	}
	/* bare or relative: an extension; absolute or ~/: that or 2 parts */
	if ((bare || !(s[i] == '/' || s[i] == '~')) ? !ext : (!ext && comps < 2))
		return 0;
	sfx = e == j ? line_sfx(s, n, e, 1) : 0;
	if (sfx && !after_ok(s, n, sfx))
		sfx = line_sfx(s, n, e, 0);
	if (sfx && !after_ok(s, n, sfx))
		sfx = 0;
	if (!sfx && (code || bare || !after_ok(s, n, e)))
		return 0;
	if (!(pl = path_norm(s + i, e - i, cwd, norm, sizeof norm)) ||
	    !span_key(sp, XR_FILE, norm, pl, 0))
		return 0;
	return sfx ? sfx : e;
}

static const char *const t_stop[] = {
	"UTF", "ISO", "SHA", "MD", "RFC", "CVE", "TLS", "SSL", "HTTP", "IPV",
	"AES", "RSA", "GPT", "PEP", "ES", "ECMA", "WCAG", "ARM", "X86", "COVID",
	"PM", "AM", 0
};

/* T: {KEY}-{n}, KEY [A-Z][A-Z0-9]{1,9} past the stoplist, n 1-6 digits. */
static size_t
r_ticket(const char *s, size_t n, size_t i, struct xr_span *sp)
{
	size_t j = i + 1, d, k;

	if (!A_UP((unsigned char)s[i]))
		return 0;
	while (j < n && j - i < 11 && (A_UP((unsigned char)s[j]) ||
	                               A_DIG((unsigned char)s[j])))
		j++;
	if (j - i < 2 || j - i > 10 || j >= n || s[j] != '-')
		return 0;
	for (k = 0; t_stop[k]; k++)
		if (strlen(t_stop[k]) == j - i && !memcmp(t_stop[k], s + i, j - i))
			return 0;
	for (d = 0, j++; j < n && d < 7 && A_DIG((unsigned char)s[j]); j++)
		d++;
	if (d < 1 || d > 6 || !after_ok(s, n, j) ||
	    !span_key(sp, XR_TICKET, s + i, j - i, 0))
		return 0;
	return j;
}

/* S: 7-40 lowercase hex, a letter and a digit; not after # 0x -, not before
** - or \w. */
static size_t
r_sha(const char *s, size_t n, size_t i, uint32_t prev, struct xr_span *sp)
{
	size_t j = i;
	int let = 0, dig = 0;

	if (prev == '#' || prev == '-')
		return 0;
	for (; j < n && j - i <= 40 && A_HEX((unsigned char)s[j]); j++)
		if (A_DIG((unsigned char)s[j]))
			dig = 1;
		else
			let = 1;
	if (j - i < 7 || j - i > 40 || !let || !dig ||
	    (j < n && s[j] == '-') || !after_ok(s, n, j) ||
	    !span_key(sp, XR_COMMIT, s + i, 7, 0))
		return 0;
	memcpy(sp->full, s + i, j - i);
	sp->full[j - i] = '\0';
	return j;
}

/* H: #{n}, n 2-6 digits; not after \w & # /, not before \w -. */
static size_t
r_hash(const char *s, size_t n, size_t i, uint32_t prev, struct xr_span *sp)
{
	size_t j = i + 1, d;

	if (s[i] != '#' || prev == '&' || prev == '#' || prev == '/')
		return 0;
	for (d = 0; j < n && d < 7 && A_DIG((unsigned char)s[j]); j++)
		d++;
	if (d < 2 || d > 6 || (j < n && s[j] == '-') || !after_ok(s, n, j) ||
	    !span_key(sp, XR_ISSUE, s + i, j - i, 0))
		return 0;
	return j;
}

/* The first rule (U, R, F, T, r, S, H) matching at i: the span's end, 0 for
** none. The caller has checked the boundary before i. */
static size_t
try_rules(const char *s, size_t n, size_t i, uint32_t prev, int rules,
          int code, const char *cwd, struct xr_span *sp)
{
	unsigned char c = (unsigned char)s[i];
	size_t e = 0;

	sp->full[0] = '\0';
	if ((rules & RU) && (c == 'h' || c == 'H') && (e = r_url(s, n, i, sp)))
		sp->rule = 'U';
	else if ((rules & RR) && (A_ALNUM(c) || c == '-') &&
	         (e = r_repo(s, n, i, prev, 1, sp)))
		sp->rule = 'R';
	else if ((rules & RF) && (path_c(c) || c >= 0x80) &&
	         (e = r_path(s, n, i, prev, code, cwd, sp)))
		sp->rule = 'F';
	else if ((rules & RT) && (e = r_ticket(s, n, i, sp)))
		sp->rule = 'T';
	else if ((rules & Rr) && A_AL(c) && (e = r_repo(s, n, i, prev, 0, sp)))
		sp->rule = 'r';
	else if ((rules & RS) && A_HEX(c) && (e = r_sha(s, n, i, prev, sp)))
		sp->rule = 'S';
	else if ((rules & RH) && c == '#' && (e = r_hash(s, n, i, prev, sp)))
		sp->rule = 'H';
	if (e)
	{
		sp->s = i;
		sp->e = e;
	}
	return e;
}

struct gscan
{
	const char *s;
	const char *cwd;
	xr_span_fn fn;
	void *ctx;
	size_t count;
	int stop;
};

static void
g_emit(struct gscan *g, const struct xr_span *sp)
{
	g->count++;
	if (g->fn && g->fn(g->ctx, sp))
		g->stop = 1;
}

/* Greedy by start, then rule order (v2.1 2.3): a span accepted at i hides
** every candidate starting inside it. whole: a code span, where only U may
** match short of the whole trimmed run. */
static void
scan_run(struct gscan *g, size_t a, size_t b, int rules, int code, int whole)
{
	const char *s = g->s;
	uint32_t prev = prev_scalar(s, a), c;
	size_t i = a, adv;
	struct xr_span sp;

	while (i < b && !g->stop)
	{
		c = u8_next((const unsigned char *)s + i, b - i, &adv);
		if (!u_word(prev) && try_rules(s, b, i, prev, rules, code, g->cwd,
		                               &sp))
		{
			if (!whole || sp.rule == 'U' || (sp.s == a && sp.e == b))
				g_emit(g, &sp);
			prev = prev_scalar(s, sp.e);
			i = sp.e;
			continue;
		}
		prev = c;
		i += adv;
	}
}

/* A fence line (CommonMark): up to 3 spaces, then 3+ backticks or tildes. */
static int
fence_open(const char *s, size_t n, size_t ls, char *fc, size_t *fl)
{
	size_t i = ls, k = 0;

	while (i < n && s[i] == ' ' && k < 3)
		i++, k++;
	if (i >= n || (s[i] != '`' && s[i] != '~'))
		return 0;
	*fc = s[i];
	for (k = 0; i < n && s[i] == *fc; i++)
		k++;
	if (k < 3)
		return 0;
	if (*fc == '`')
		for (; i < n && s[i] != '\n'; i++)
			if (s[i] == '`')
				return 0;
	*fl = k;
	return 1;
}

static int
fence_close(const char *s, size_t n, size_t ls, char fc, size_t fl)
{
	size_t i = ls, k = 0;

	while (i < n && s[i] == ' ' && k < 3)
		i++, k++;
	for (k = 0; i < n && s[i] == fc; i++)
		k++;
	if (k < fl)
		return 0;
	while (i < n && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r'))
		i++;
	return i >= n || s[i] == '\n';
}

static size_t
line_end(const char *s, size_t n, size_t i)
{
	const char *nl = memchr(s + i, '\n', n - i);

	return nl ? (size_t)(nl - s) : n;
}

/* A code span's content: nothing when it holds a newline (2.4), else the
** whole-span rules on its trimmed text. */
static void
code_span(struct gscan *g, size_t a, size_t b)
{
	size_t n = b - a;
	const char *t;

	if (memchr(g->s + a, '\n', n))
		return;
	t = trim(g->s + a, &n);
	if (n)
		scan_run(g, (size_t)(t - g->s), (size_t)(t - g->s) + n, R_CODE, 1, 1);
}

/* [label](url) at i in [i, b): 1 with the label's end m and the ')' at q. */
static int
md_link(const char *s, size_t i, size_t b, size_t *m, size_t *q)
{
	size_t k;

	for (k = i + 1; k < b && k - i < 4096 && s[k] != ']'; k++)
		if (s[k] == '[' || s[k] == '\n')
			return 0;
	if (k + 1 >= b || s[k] != ']' || s[k + 1] != '(')
		return 0;
	*m = k;
	for (k += 2; k < b && k - *m < XR_KEY_MAX + 4 && s[k] != ')'; k++)
		if ((unsigned char)s[k] <= ' ')
			return 0;
	if (k >= b || s[k] != ')')
		return 0;
	*q = k;
	return 1;
}

/* Inline code spans and (Markdown) links over the prose run [a, b). */
static void
inline_run(struct gscan *g, size_t a, size_t b, int md)
{
	const char *s = g->s;
	size_t i = a, seg = a, k, j, r, m, q;

	while (i < b && !g->stop)
	{
		if (s[i] == '`')
		{
			for (k = 0; i + k < b && s[i + k] == '`'; k++)
				;
			for (j = i + k; j < b; j += r ? r : 1)
			{
				for (r = 0; j + r < b && s[j + r] == '`'; r++)
					;
				if (r == k)
					break;
			}
			if (j < b)
			{
				scan_run(g, seg, i, R_PROSE, 0, 0);
				code_span(g, i + k, j);
				i = seg = j + k;
			}
			else
				i += k;	/* no closer: literal backticks */
			continue;
		}
		if (md && s[i] == '[' && md_link(s, i, b, &m, &q))
		{
			struct xr_span sp;

			scan_run(g, seg, i, R_PROSE, 0, 0);
			/* already linked: no span in the label; the URL is a url
			** span on the label's range (2.3) */
			if (!g->stop && r_url(s, q, m + 2, &sp) == q)
			{
				sp.rule = 'U';
				sp.s = i + 1;
				sp.e = m;
				g_emit(g, &sp);
			}
			i = seg = q + 1;
			continue;
		}
		i++;
	}
	if (!g->stop)
		scan_run(g, seg, b, R_PROSE, 0, 0);
}

/* Plain or Markdown prose: fences (an unclosed one makes the rest code),
** then inline spans (v2.1 2.4, v1 4.3's small scanner). */
static void
scan_prose(struct gscan *g, size_t n, int md)
{
	const char *s = g->s;
	size_t ls = 0, ps = 0, fl;
	char fc;

	while (ls < n && !g->stop)
	{
		size_t le = line_end(s, n, ls);

		if (fence_open(s, n, ls, &fc, &fl))
		{
			size_t k = le < n ? le + 1 : n;

			inline_run(g, ps, ls, md);
			while (k < n && !fence_close(s, n, k, fc, fl))
			{
				size_t ke = line_end(s, n, k);

				k = ke < n ? ke + 1 : n;
			}
			if (k >= n)
				return;
			le = line_end(s, n, k);
			ls = ps = le < n ? le + 1 : n;
			continue;
		}
		ls = le < n ? le + 1 : n;
	}
	if (!g->stop)
		inline_run(g, ps, n, md);
}

static size_t
grammar(const char *s, size_t n, int mode, int rules, const char *cwd,
        xr_span_fn fn, void *ctx)
{
	struct gscan g = { s, cwd, fn, ctx, 0, 0 };

	if (mode == XR_PROSE || mode == XR_MD)
		scan_prose(&g, n, mode == XR_MD);
	else
		scan_run(&g, 0, n, rules, mode == XR_CODE, 0);
	return g.count;
}

size_t
xr_grammar(const char *s, size_t n, int mode, const char *cwd, xr_span_fn fn,
           void *ctx)
{
	return grammar(s, n, mode, mode == XR_CODE ? R_CODE : R_PROSE, cwd, fn,
	               ctx);
}

/* The span at exactly i under `rules` (prose: no code scan), 1 with *sp. */
static int
anchor_at(const char *s, size_t n, size_t i, int rules, const char *cwd,
          struct xr_span *sp)
{
	return i < n && !u_word(prev_scalar(s, i)) &&
	       try_rules(s, n, i, prev_scalar(s, i), rules, 0, cwd, sp) != 0;
}

int
xr_ref_ok(const char *text)
{
	size_t n = strlen(text), i, a;

	if (!n || n > 256)
		return 0;
	for (i = 0; i < n; i += a)
	{
		uint32_t c;

		if (!(a = dch_utf8_len((const unsigned char *)text + i, n - i)))
			return 0;
		c = u8_next((const unsigned char *)text + i, n - i, &a);
		if (c < 0x20 || (c >= 0x7f && c <= 0x9f) || c == 0x200e ||
		    c == 0x200f || (c >= 0x202a && c <= 0x202e) ||
		    (c >= 0x2066 && c <= 0x2069))
			return 0;
	}
	return 1;
}

struct one_span
{
	struct xr_span sp;
	int n;
};

static int
one_span(void *ctx, const struct xr_span *sp)
{
	struct one_span *o = ctx;

	if (!o->n++)
		o->sp = *sp;
	return o->n > 1;
}

int
xr_ref_parse(const char *text, const char *cwd, struct xr_span *out)
{
	size_t n = strlen(text);
	const char *t = trim(text, &n);
	struct one_span o;

	o.n = 0;
	/* the trimmed text as its own string: boundaries end where it does */
	{
		char *c = malloc(n + 1);

		if (!c)
			return 0;
		memcpy(c, t, n);
		c[n] = '\0';
		grammar(c, n, XR_PROSE, R_PROSE, cwd, one_span, &o);
		free(c);
	}
	if (o.n != 1 || o.sp.s != 0 || o.sp.e != n)
		return 0;
	*out = o.sp;
	return 1;
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

/* A subdir of dfd, never through a symlink; -1 none. */
static DIR *
cx_dir(int dfd, const char *name)
{
	/* dirfd() failing is -1; openat would only say EBADF, the analyzer
	** wants it not passed at all */
	int fd = dfd == -1 ? -1 : openat(dfd, name, O_RDONLY | O_DIRECTORY |
	                                 O_NOFOLLOW | O_CLOEXEC);
	DIR *d = fd >= 0 ? fdopendir(fd) : NULL;

	if (fd >= 0 && !d)
		close(fd);
	return d;
}

/* d's entries named by exactly w digits, as a bitmap (names only: no stat,
** and a bound by construction: 10^w names at most). */
static void
cx_names(DIR *d, size_t w, unsigned char *bits)
{
	struct dirent *de;
	size_t k;
	unsigned v;

	while ((de = readdir(d)))
	{
		for (k = 0, v = 0; k < w && isdigit((unsigned char)de->d_name[k]);
		     k++)
			v = v * 10 + (unsigned)(de->d_name[k] - '0');
		if (k == w && !de->d_name[w])
			bits[v / 8] |= (unsigned char)(1u << (v % 8));
	}
}

#define CX_HAS(b, v) ((b)[(v) / 8] & (1u << ((v) % 8)))

int
xr_codex_lookup(const char *home, const char *tid, char *path, size_t pathsz,
                struct stat *st)
{
	char dir[XR_PATH_MAX + 16], ys[12], ms[12], ds[12];
	unsigned char yb[10000 / 8 + 1] = {0};
	DIR *sd, *yd, *md, *dd;
	struct dirent *de;
	struct stat s;
	int y, m, day, days = 0, ups = 0, hits = 0;
	size_t tl = strlen(tid);

	if (!xr_config_ok(home) || !xr_uuid_shaped(tid) ||
	    snprintf(dir, sizeof dir, "%s/sessions", home) >= (int)sizeof dir ||
	    !(sd = cx_dir(AT_FDCWD, dir)))
		return -1;
	cx_names(sd, 4, yb);
	/* year and month dirs opened count too, against a bound of their own:
	** empty ones open no day dir (QA 1.55.0 LOW-11) */
	for (y = 9999; y >= 0 && days < XR_CODEX_DAYS && ups < XR_CODEX_DAYS &&
	     hits < 2; y--)
	{
		unsigned char mb[13] = {0};

		snprintf(ys, sizeof ys, "%04d", y);
		if (!CX_HAS(yb, (unsigned)y) || !(yd = cx_dir(dirfd(sd), ys)))
			continue;
		ups++;
		cx_names(yd, 2, mb);
		for (m = 99; m >= 0 && days < XR_CODEX_DAYS && ups < XR_CODEX_DAYS &&
		     hits < 2; m--)
		{
			unsigned char db[13] = {0};

			snprintf(ms, sizeof ms, "%02d", m);
			if (!CX_HAS(mb, (unsigned)m) || !(md = cx_dir(dirfd(yd), ms)))
				continue;
			ups++;
			cx_names(md, 2, db);
			for (day = 99; day >= 0 && days < XR_CODEX_DAYS && hits < 2;
			     day--)
			{
				snprintf(ds, sizeof ds, "%02d", day);
				if (!CX_HAS(db, (unsigned)day) ||
				    !(dd = cx_dir(dirfd(md), ds)))
					continue;
				days++;
				/* "<any>-<tid>.jsonl", a regular file, not a link; ours and
				** not group- or world-writable, as a snapshot (QA 1.55.0
				** NIT-6) */
				while ((de = readdir(dd)) && hits < 2)
				{
					size_t n = strlen(de->d_name);
					int dfd = dirfd(dd);

					/* dfd < 0: dirfd() failing, as in cx_dir */
					if (n < tl + 7 || de->d_name[n - tl - 7] != '-' ||
					    memcmp(de->d_name + n - tl - 6, tid, tl) ||
					    strcmp(de->d_name + n - 6, ".jsonl") || dfd < 0 ||
					    fstatat(dfd, de->d_name, &s,
					            AT_SYMLINK_NOFOLLOW) < 0 ||
					    !S_ISREG(s.st_mode) || s.st_uid != getuid() ||
					    (s.st_mode & 022))
						continue;
					if (hits++ == 0 &&
					    snprintf(path, pathsz, "%s/%s/%s/%s/%s", dir, ys,
					             ms, ds, de->d_name) >= (int)pathsz)
						hits = 2;	/* cut short: no path */
					*st = s;
				}
				closedir(dd);
			}
			closedir(md);
		}
		closedir(yd);
	}
	closedir(sd);
	dch_trace("xray codex lookup: %d hit%s, %d day dirs", hits,
	          hits == 1 ? "" : "s", days);
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

/* harness(): `<task-notification>…</task-notification>` and
** `<system-reminder>…</system-reminder>` blocks and nothing else, the text
** Claude Code writes as a user record (#192). 1 with the first notification's
** <summary>, else its <event>, as a title in sum[] ("" for neither), else 0. */
static int
harness(const char *s, size_t n, char *sum, size_t sumsz)
{
	static const char *const H[] = { "task-notification", "system-reminder" };
	const char *t, *e, *v = NULL;
	size_t tl = n, i, a, k, ol = 0, vl = 0;
	char o[24], c[24];
	int first = 1;

	sum[0] = '\0';
	t = trim(s, &tl);
	if (!tl)
		return 0;
	for (i = 0; i < tl; i += a)
	{
		for (k = 0; k < 2; k++)
		{
			ol = (size_t)snprintf(o, sizeof o, "<%s>", H[k]);
			if (i + ol <= tl && !memcmp(t + i, o, ol))
				break;
		}
		if (k < 2)
		{
			snprintf(c, sizeof c, "</%s>", H[k]);
			if (!(e = find(t, tl, c, i + ol)))
				return 0;
			if (!k && first)
			{
				first = 0;
				if (!(v = tag(t + i + ol, (size_t)(e - t) - i - ol,
				              "summary", &vl)) || !vl)
					v = tag(t + i + ol, (size_t)(e - t) - i - ol, "event",
					        &vl);
				if (v && !vl)
					v = NULL;
			}
			i = (size_t)(e - t) + strlen(c);
			a = 0;
			continue;
		}
		if (!u_space(u8_next((const unsigned char *)t + i, tl - i, &a)))
			return 0;
	}
	if (v)
		xr_title(v, vl | XR_WHOLE, sum, sumsz);
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
unwrap(char *s, size_t n, size_t cap)
{
	static const char open[] = "<pasted_content id=\"";
	char close[96];
	size_t i = 0, k = 0, j, cs, ce, t = 0, nb = 0, a, sc = 0;
	const char *e;
	int miss = 0;

	/* cap 0: until the title is settled; else until cap scalars are out
	** (the refs' clip, 3.1), after at most 16 openers with no closer */
	while (i < n && (cap ? sc < cap : !settled(s, k, &t, &nb)))
	{
		if (s[i] == '<' && i + sizeof open - 1 <= n && miss < 16 &&
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
					if (cap)
						sc += scalars(s + k, ce - cs);
					k += ce - cs;
					i = (size_t)(e - s) + strlen(close);
					continue;
				}
				miss += cap != 0;
			}
		}
		/* a whole scalar, so settled() never sees half of one */
		(void)u8_next((const unsigned char *)s + i, n - i, &a);
		memmove(s + k, s + i, a);
		k += a;
		i += a;
		sc++;
	}
	return k;
}

/* ---- refs: emitting mentions and definitions ------------------------------ */

static void put_or_null(FILE *f, const char *s);
static size_t tokens_max(const char *s, size_t n);

/* s[0..n) less a scalar cut short at its end (bridge.c's dch_utf8_trim,
** kept here so the engine links alone: units, fuzz). */
static size_t
u8_cut(const char *s, size_t n)
{
	size_t k = n, want;
	unsigned char c;

	while (k > 0 && n - k < 3 && ((unsigned char)s[k - 1] & 0xc0) == 0x80)
		k--;
	if (!k || (c = (unsigned char)s[k - 1]) < 0xc0)
		return n;
	want = c >= 0xf0 ? 4 : c >= 0xe0 ? 3 : 2;
	return n - (k - 1) < want ? k - 1 : n;
}

static uint64_t
fnv(const char *s, size_t n)
{
	uint64_t h = 0xcbf29ce484222325ULL;

	while (n--)
		h = (h ^ (unsigned char)*s++) * 0x100000001b3ULL;
	return h;
}

/* A definition's identity (v2.1 6.3): its title, whitespace collapsed and
** case-folded (shortcut: ASCII folding only). */
static uint64_t
title_hash(const char *t)
{
	uint64_t h = 0xcbf29ce484222325ULL;
	const unsigned char *u = (const unsigned char *)t;

	for (; *u; u++)
		h = (h ^ (A_UP(*u) ? *u + 32 : *u)) * 0x100000001b3ULL;
	return h;
}

/* Where a field's mentions go: the sink plus what each hit carries. */
struct remit
{
	const struct xr_sink *k;
	const char *cwd;
	const char *text;	/* the field scanned */
	uint64_t off;		/* Claude: the line's offset; shell: the seq */
	long long at;
	const char *chapter;
	uint64_t *ctr;		/* Claude: each mention its own unit */
	uint64_t unit, row;	/* shell: the block and the row */
	int left;		/* spans left in the block, -1 no cap */
	/* the field's code spans, for a definition anchor's one mention */
	struct cov { size_t s, e; uint64_t h; } *cov;
	size_t ncov, ccov;
	/* shell: a block's output, its lines' starts and seqs (offset, row) */
	const size_t *lpos;
	const uint64_t *lseq;
	size_t nl;
	/* the last hit's line [cls, cle) in ctext: hits arrive sorted, so a
	** line's bounds are found once, not once per span */
	const char *ctext;
	size_t cls, cle;
};

static void
rm_hit(struct remit *r, const struct xr_span *sp, const char *title,
       const char *passage, const char *rule)
{
	struct xr_hit h;
	struct xr_span c = *sp;
	size_t ls, le, from = 0;

	if (r->ctext == r->text && r->cls <= sp->s && sp->e <= r->cle)
		ls = r->cls, le = r->cle;
	else
	{
		/* text[cle] is the '\n' ending the cached line */
		if (r->ctext == r->text && sp->s > r->cle)
			from = r->cle + 1;
		for (ls = sp->s; ls > from && r->text[ls - 1] != '\n'; ls--)
			;
		for (le = sp->e; r->text[le] && r->text[le] != '\n'; le++)
			;
		r->ctext = r->text;
		r->cls = ls;
		r->cle = le;
	}
	c.s -= ls;
	c.e -= ls;
	memset(&h, 0, sizeof h);
	h.sp = &c;
	h.line = r->text + ls;
	h.ll = le - ls;
	h.off = r->off;
	h.at = r->at;
	h.chapter = r->chapter;
	if (title)
	{
		h.title = title;
		h.passage = passage;
		h.rule = rule;
	}
	if (r->ctr)
		h.unit = h.row = ++*r->ctr;
	else
	{
		h.unit = r->unit;
		h.row = r->row;
	}
	if (r->nl)
	{
		size_t lo = 0, hi = r->nl;

		while (hi - lo > 1)
			if (r->lpos[(lo + hi) / 2] <= sp->s)
				lo = (lo + hi) / 2;
			else
				hi = (lo + hi) / 2;
		h.off = h.row = r->lseq[lo];
	}
	r->k->hit(r->k->ctx, &h);
}

static void
cov_add(struct remit *r, const struct xr_span *sp)
{
	if (r->ncov == r->ccov)
	{
		size_t nc = r->ccov ? r->ccov * 2 : 16;
		struct cov *v = realloc(r->cov, nc * sizeof *v);

		if (v)
		{
			r->cov = v;
			r->ccov = nc;
		}
	}
	if (r->ncov < r->ccov)
	{
		r->cov[r->ncov].s = sp->s;
		r->cov[r->ncov].e = sp->e;
		r->cov[r->ncov++].h = fnv(sp->key, strlen(sp->key));
	}
}

/* A span recorded for coverage only (a command scanned again at its result). */
static int
cov_span(void *ctx, const struct xr_span *sp)
{
	cov_add(ctx, sp);
	return 0;
}

/* A file key still relative: seen before any line carried a cwd (LOW-7),
** so no --ref can name it. Dropped, not held: a cwd seen later may not be
** the one it was written in. */
static int
rel_file(const struct xr_span *sp)
{
	const char *t = xr_span_text(sp);

	return sp->shape == XR_FILE && t[0] != '/' && t[0] != '~';
}

static int
rm_span(void *ctx, const struct xr_span *sp)
{
	struct remit *r = ctx;

	if (!r->left)
		return 1;
	if (rel_file(sp))
		return 0;
	if (r->left > 0)
		r->left--;
	cov_add(r, sp);
	rm_hit(r, sp, NULL, NULL, NULL);
	return 0;
}

/* Every span of text[0..n) in mode to the sink; text NUL-terminated. */
static void
rm_scan(struct remit *r, const char *text, size_t n, int mode)
{
	r->text = text;
	r->ctext = NULL;
	r->ncov = 0;
	(void)xr_grammar(text, n, mode, r->cwd, rm_span, r);
}

/* ---- refs: definitions (v2.1 4.2-4.3) ------------------------------------- */

/* A command's words, unquoted (a small sh reading: '…', "…", \x), with
** their raw byte range; operators (| ; & < > ( ) and an unquoted newline)
** are words of their own. \ newline is a blank, or nothing inside a word;
** a # starting a word is a comment to the line's end; each here-doc's body,
** in order, is skipped to its delimiter line (a trailing CR ignored). hx:
** a body sh expands ($(…) or ` under an unquoted delimiter). */
#define SW_MAX 256	/* one word more marks a command past the cap */
struct sw
{
	char *b;
	size_t n;
	size_t off[SW_MAX + 1], rs[SW_MAX + 1], re[SW_MAX + 1];
	char op[SW_MAX + 1];
	int hx;
};

static void
shell_words(const char *c, size_t cl, struct sw *w)
{
	size_t i = 0, o = 0, hd = 0, j, dl, b0, k, dk;
	const char *d;
	int bl, dash, uq;

	w->n = 0;
	w->hx = 0;
	if (!(w->b = malloc(cl + SW_MAX + 2)))
		return;	/* a word adds at most its NUL to the bytes it reads */
	while (i < cl && w->n <= SW_MAX)
	{
		for (b0 = i, bl = 0;
		     i < cl && (c[i] == ' ' || c[i] == '\t' || c[i] == '\r' ||
		                (c[i] == '\\' && i + 1 < cl && c[i + 1] == '\n'));
		     i += 1 + (c[i] == '\\'))
			bl |= c[i] == ' ' || c[i] == '\t';
		if (i >= cl)
			break;
		if (c[i] == '#' && (bl || !w->n || w->op[w->n - 1] ||
		                    w->re[w->n - 1] != b0))
		{
			/* a comment: not after a word's CR or \ newline (sh reads
			** that # as the word's) */
			while (i < cl && c[i] != '\n')
				i++;
			continue;
		}
		if (c[i] == '\n')
		{
			/* a newline ends the command, like ';' (one after an operator
			** or at the start does not); each here-doc's body follows it,
			** in the order of their << */
			w->rs[w->n] = i++;
			for (k = hd ? hd - 1 : w->n; k < w->n; k++)
			{
				if (w->op[k] || !w->op[k - 1] ||
				    strcmp(w->b + w->off[k - 1], "<<"))
					continue;
				i += k >= hd && i < cl && c[i] == '\n';	/* a later body */
				/* <<-EOF and <<- EOF (not << -EOF or <<'-EOF') */
				dash = w->re[k - 1] == w->rs[k] && c[w->rs[k]] == '-';
				dk = k + (dash && !w->b[w->off[k] + 1] && k + 1 < w->n &&
				          !w->op[k + 1]);
				d = w->b + w->off[dk] + (dk == k && dash);
				for (j = w->rs[dk];
				     j < w->re[dk] && !strchr("'\"\\", c[j]); j++)
					;
				uq = j == w->re[dk];	/* sh expands the body */
				for (b0 = i, dl = strlen(d); i < cl; i = j + 1)
				{
					for (; dash && i < cl && c[i] == '\t'; i++)
						;	/* <<- strips leading tabs */
					for (j = i; j < cl && c[j] != '\n'; j++)
						;
					if (j - i - (j > i && c[j - 1] == '\r') == dl &&
					    !memcmp(c + i, d, dl))
					{
						i = j;	/* its own newline follows an operator */
						break;
					}
				}
				if (i > cl)
					i = cl;	/* no delimiter line: c[cl] is not the body's */
				for (; uq && b0 < i; b0++)
					w->hx |= c[b0] == '`' ||
					         (c[b0] == '$' && b0 + 1 < i && c[b0 + 1] == '(');
			}
			hd = 0;
			for (j = i; j < cl && strchr(" \t\r\n", c[j]); j++)
				;
			if (j >= cl)
				break;	/* a trailing newline ends nothing */
			if (!w->n || w->op[w->n - 1])
				continue;
			w->off[w->n] = o;
			w->op[w->n] = 1;
			w->b[o++] = '\n';
			w->b[o++] = '\0';
			w->re[w->n] = w->rs[w->n] + 1;
			w->n++;
			continue;
		}
		w->rs[w->n] = i;
		w->off[w->n] = o;
		w->op[w->n] = strchr("|;&<>()", c[i]) != NULL && c[i];
		if (w->op[w->n])
		{
			w->b[o++] = c[i++];
			if (i < cl && c[i] == c[i - 1] && strchr("&|<>", c[i]))
				w->b[o++] = c[i++];
		}
		else
			while (i < cl && c[i] != ' ' && c[i] != '\t' && c[i] != '\n' &&
			       c[i] != '\r' && !strchr("|;&<>()", c[i]))
			{
				char q = c[i];

				if (q == '\'' || q == '"')
				{
					for (i++; i < cl && c[i] != q; i++)
					{
						if (q == '"' && c[i] == '\\' && i + 1 < cl &&
						    c[++i] == '\n')
							continue;	/* \ newline: nothing */
						w->b[o++] = c[i];
					}
					i += i < cl;
				}
				else if (q == '\\' && i + 1 < cl)
				{
					if (c[i + 1] != '\n')
						w->b[o++] = c[i + 1];	/* \ newline: nothing */
					i += 2;
				}
				else
					w->b[o++] = c[i++];
			}
		w->b[o++] = '\0';
		if (!hd && !w->op[w->n] && w->n && w->op[w->n - 1] &&
		    !strcmp(w->b + w->off[w->n - 1], "<<"))
			hd = w->n + 1;	/* the line's first here-doc delimiter word */
		w->re[w->n++] = i;
	}
}

#define SWD(w, k) ((w)->b + (w)->off[k])

static int
is_assign(const char *s)
{
	const char *p = s;

	if (!A_AL((unsigned char)*p) && *p != '_')
		return 0;
	while (A_ALNUM((unsigned char)*p) || *p == '_')
		p++;
	return *p == '=';
}

/* Words that only run the command after them, the letters of their
** options that take a value (`sudo -u root cat`, `xargs -n 1 rg`), and
** the words they take before it (timeout's duration); -1: they run the
** rest as one shell line (`eval`, `watch`). */
static const struct { const char *w, *argopt; int pos; } wraps[] = {
	{ "sudo", "ugCDhprTt", 0 }, { "env", "uSCP", 0 }, { "command", "", 0 },
	{ "xargs", "IJLnPsEdaR", 0 }, { "nice", "n", 0 }, { "time", "", 0 },
	{ "nohup", "", 0 }, { "doas", "auC", 0 }, { "exec", "a", 0 },
	{ "stdbuf", "ioe", 0 }, { "timeout", "sk", 1 }, { "eval", "", -1 },
	{ "watch", "nq", -1 }, { 0, 0, 0 }
};

static const char *base(const char *s);
static int in_list(const char *s, const char *const *v);

/* The first command word of the simple command w[k..e): past env
** assignments, redirects, reserved words and the wraps with their
** options. e when there is none. *line: the words from it on, *line - 1
** bytes into it, are one shell line (`eval …`, `watch …`, `env -S '…'`,
** `env -S'…'`, `env --split-string=…`). */
static size_t
cmd_head(const struct sw *w, size_t k, size_t e, int *line)
{
	static const char *const resv[] = { "{", "!", "if", "then", "elif",
	                                    "else", "do", "while", "until", 0 };
	const char *o, *a;
	size_t j;
	int p;

	*line = 0;
	while (k < e)
	{
		o = SWD(w, k);
		if (w->op[k] || (k + 1 < e && w->op[k + 1] && o[0] >= '0' &&
		                 o[0] <= '9' && !o[strspn(o, "0123456789")] &&
		                 w->re[k] == w->rs[k + 1]))
		{
			/* < f cat, 2>/dev/null cat: the redirect and its target */
			k += 1 + !w->op[k];
			k += k < e && !w->op[k];
			continue;
		}
		if (is_assign(SWD(w, k)) || in_list(SWD(w, k), resv))
		{
			k++;
			continue;
		}
		for (j = 0; wraps[j].w && strcmp(base(SWD(w, k)), wraps[j].w); j++)
			;
		if (!wraps[j].w)
			return k;
		for (k++; k < e && (o = SWD(w, k))[0] == '-'; k++)
		{
			if (!strcmp(o, "--"))
			{
				k++;
				break;
			}
			/* env -S, -iS'…', --split-string[=…]: the rest is a line */
			a = strcmp(wraps[j].w, "env") ? NULL :
			    !strncmp(o, "--split-string", 14) && (!o[14] || o[14] == '=') ?
			    o + 14 + !!o[14] :
			    o[1] != '-' && o[1 + strcspn(o + 1, "uCPS")] == 'S' ?
			    o + 2 + strcspn(o + 1, "uCPS") : NULL;
			if (a && *a)
			{
				*line = 1 + (int)(a - o);
				return k;
			}
			if (a)
			{
				*line = k + 1 < e;
				return k + 1;
			}
			if (o[1] && !o[2] && strchr(wraps[j].argopt, o[1]))
				k++;
		}
		for (p = wraps[j].pos; p > 0 && k < e; p--)
			k++;
		if (wraps[j].pos < 0)
		{
			*line = k < e;
			return k;
		}
	}
	return e;
}

static const char *
base(const char *s)
{
	const char *b = strrchr(s, '/');

	return b ? b + 1 : s;
}

static int
in_list(const char *s, const char *const *v)
{
	for (; *v; v++)
		if (!strcmp(s, *v))
			return 1;
	return 0;
}

/* An absolute or ~ path outside cwd (exclusion #1); no cwd: outside. */
static int
outside(const char *p, const char *cwd)
{
	char a[XR_KEY_MAX + 1], b[XR_KEY_MAX + 1], t[XR_PATH_MAX + 8];
	const char *h = getenv("HOME");
	size_t al, bl;

	if (p[0] == '~')
	{
		if (!h || h[0] != '/' || (p[1] && p[1] != '/') ||
		    snprintf(t, sizeof t, "%s%s", h, p + 1) >= (int)sizeof t)
			return 1;
		p = t;
	}
	if (!cwd || cwd[0] != '/' ||
	    !(al = path_norm(p, strlen(p), NULL, a, sizeof a)) ||
	    !(bl = path_norm(cwd, strlen(cwd), NULL, b, sizeof b)))
		return 1;
	if (bl == 1)
		return 0;	/* cwd "/" */
	return !(al >= bl && !memcmp(a, b, bl) && (a[bl] == '\0' || a[bl] == '/'));
}

/* A printed file argument from outside cwd (exclusion #1): one whose value
** only the shell knows ($, `), or a path that leaves cwd once made absolute
** against cur, the directory after any cd (lost: a cd went somewhere
** unknown). With no cwd at all, only a leading .. leaves it. */
static int
arg_outside(const char *a, const char *cur, int lost, const char *cwd)
{
	char n[XR_KEY_MAX + 1];

	if (strpbrk(a, "$`"))
		return 1;
	if (a[0] == '/' || a[0] == '~')
		return outside(a, cwd);
	if (lost || !path_norm(a, strlen(a), cur, n, sizeof n))
		return 1;
	if (n[0] == '/')
		return outside(n, cwd);
	return !strcmp(n, "..") || !strncmp(n, "../", 3);
}

/* X_SUB: found only inside $(…) or backquotes, whose output is the outer
** command's input: D3 (argv) still applies */
enum { X_NONE, X_QUOTE = 1, X_DIFF = 3, X_LIST = 4, X_SUB = 8 };

/* excluded() on cmd run in cur (lost: 1 unknown, 2 under xargs, whose
** files come from its input), depth sh -c levels in. */
static int
excl_in(const char *cmd, size_t cl, const char *cwd, const char *cur,
        int lost, int depth)
{
	static const char *const list[] = { "rg", "grep", "egrep", "fgrep", "ag",
	                                    "ack", "fd", "find", "ls", 0 };
	static const char *const print[] = { "cat", "head", "tail", "bat",
	                                     "less", "awk", "nl", "more", "sed",
	                                     "jq", "tac", "sort", "cut", "zcat",
	                                     "gzcat", "batcat", "glow", "yq",
	                                     "uniq", "rev", "column", "fold",
	                                     "fmt", 0 };
	static const char *const shells[] = { "sh", "bash", "zsh", "dash", "ksh",
	                                      "fish", 0 };
	static const char *const closers[] = { "done", "fi", "esac", "}", 0 };
	struct sw w;
	char cdb[XR_KEY_MAX + 1], q;
	size_t s, e, h, k, sub, i, j, t, end;
	int r = X_NONE, sr = X_NONE, x, skip, line, rd, xa;
	const char *c0, *a;

	shell_words(cmd, cl, &w);
	if (!w.b || w.n > SW_MAX)
	{
		free(w.b);
		return X_LIST;	/* no memory, or words past SW_MAX unseen */
	}
	if (w.hx)
		r = X_LIST;	/* $(…) in a here-doc body sh expands: not read */
	/* every simple command: split on ; & && | || ( ) and newlines */
	for (s = 0; s < w.n && !r; s = e + 1)
	{
		for (e = s; e < w.n && !(w.op[e] && !strchr("<>", SWD(&w, e)[0]));
		     e++)
			;
		/* $(…) and `…` in quotes or a word run too: their commands,
		** classified alike (an unquoted $( is split at the ( already) */
		end = e > s ? w.re[e - 1] : 0;
		for (i = e > s ? w.rs[s] : 0, q = 0; i < end && !sr; i++)
			if (cmd[i] == '\\' && q != '\'')
				i++;
			else if ((cmd[i] == '\'' && q != '"') ||
			         (cmd[i] == '"' && q != '\''))
				q = q ? 0 : cmd[i];
			else if (q != '\'' && (cmd[i] == '`' ||
			         (cmd[i] == '$' && i + 1 < end && cmd[i + 1] == '(')))
			{
				t = i + 1 + (cmd[i] == '$');
				for (j = t; cmd[i] == '`' && j < end && cmd[j] != '`';
				     j += 1 + (cmd[j] == '\\'))
					;
				j = cmd[i] == '$' || j > end ? end : j;
				x = depth >= 3 ? X_LIST :
				    excl_in(cmd + t, j - t, cwd, cur, lost, depth + 1);
				sr = x & ~X_SUB;
				i = j;
			}
		/* a < source is a printed file of a printer, of a compound
		** command (done < f, } < f) and with no command ($(< f), < f) */
		for (k = s, rd = 0; k + 1 < e; k++)
			if (w.op[k] && !strcmp(SWD(&w, k), "<") && !w.op[k + 1] &&
			    arg_outside(SWD(&w, k + 1), cur, lost, cwd))
				rd = 1;
		if ((h = cmd_head(&w, s, e, &line)) >= e ||
		    in_list(SWD(&w, h), closers))
		{
			r = rd ? X_QUOTE : r;
			continue;
		}
		for (k = s, xa = lost & 2; k < h; k++)
			if (!strcmp(base(SWD(&w, k)), "xargs"))
				xa = 2;
		if (line)
		{
			/* eval/watch/env -S: the words from h on, re-read as a line */
			for (k = h; k + 1 < e; k++)
				w.b[w.off[k + 1] - 1] = ' ';
			a = SWD(&w, h) + line - 1;
			x = depth >= 3 ? X_LIST :
			    excl_in(a, strlen(a), cwd, cur, lost | xa, depth + 1);
			if (x & X_SUB)
				sr = sr ? sr : x & ~X_SUB;
			else
				r = x;
			continue;
		}
		c0 = base(SWD(&w, h));
		if (!strcmp(c0, "cd") || !strcmp(c0, "pushd"))
		{
			/* later relative arguments resolve where cd went */
			for (k = h + 1; k < e && SWD(&w, k)[0] == '-' && SWD(&w, k)[1];
			     k++)
				;
			a = k < e && !w.op[k] ? SWD(&w, k) : "";
			lost = (lost & 2) | (!a[0] || a[0] == '~' || a[0] == '-' ||
			       a[0] == '+' || strpbrk(a, "$`") ||
			       !path_norm(a, strlen(a), lost ? NULL : cur, cdb,
			                  sizeof cdb) || cdb[0] != '/');
			cur = lost ? NULL : cdb;
			continue;
		}
		if (in_list(c0, shells))
		{
			/* sh -c 'cmd' (or -lc, -ec): its command, classified alike */
			for (k = h + 1; k + 1 < e && !w.op[k]; k++)
				if (SWD(&w, k)[0] == '-' && SWD(&w, k)[1] != '-' &&
				    strchr(SWD(&w, k), 'c'))
				{
					x = depth >= 3 || w.op[k + 1] ? X_LIST :
					    excl_in(SWD(&w, k + 1), strlen(SWD(&w, k + 1)), cwd,
					            cur, lost | xa, depth + 1);
					if (x & X_SUB)
						sr = sr ? sr : x & ~X_SUB;
					else
						r = x;
					break;
				}
			continue;
		}
		/* git's subcommand: past its global options */
		for (sub = h + 1; sub < e && SWD(&w, sub)[0] == '-'; sub++)
			if (!strcmp(SWD(&w, sub), "-C") || !strcmp(SWD(&w, sub), "-c"))
				sub++;
		if (sub >= e || w.op[sub])
			sub = 0;
		if (in_list(c0, list) ||
		    (!strcmp(c0, "git") && sub && !strcmp(SWD(&w, sub), "grep")))
			r = X_LIST;
		else if (!strcmp(c0, "diff") || !strcmp(c0, "patch") ||
		         (!strcmp(c0, "git") && sub &&
		          (!strcmp(SWD(&w, sub), "diff") ||
		           !strcmp(SWD(&w, sub), "show"))))
			r = X_DIFF;
		else if (!strcmp(c0, "git") && sub && !strcmp(SWD(&w, sub), "log"))
		{
			for (k = sub + 1; k < e; k++)
				if (!strcmp(SWD(&w, k), "-p") || !strcmp(SWD(&w, k), "-u") ||
				    !strcmp(SWD(&w, k), "--patch"))
					r = X_DIFF;
		}
		else
		{
			if (!in_list(c0, print))
				continue;
			if (rd || xa)
				r = X_QUOTE;	/* a < source outside, files from xargs */
			/* the file arguments: not options, not the script or filter
			** of sed, awk, jq or yq, not a > target or a << delimiter; a <
			** source is one */
			skip = !strcmp(c0, "sed") || !strcmp(c0, "awk") ||
			       !strcmp(c0, "jq") || !strcmp(c0, "yq");
			for (k = h + 1; k < e; k++)
			{
				a = SWD(&w, k);
				if (w.op[k])
					k += a[0] == '>' || !strcmp(a, "<<");
				else if (a[0] == '-' && a[1])
					;
				else if (skip)
					skip = 0;
				else if (arg_outside(a, cur, lost, cwd))
					r = X_QUOTE;
			}
		}
	}
	free(w.b);
	return r ? r : sr ? sr | X_SUB : X_NONE;
}

/* Exclusions #1, #3 and #4 on the paired command (v2.1 4.3), over every
** simple command of it. */
static int
excluded(const char *cmd, size_t cl, const char *cwd)
{
	return excl_in(cmd, cl, cwd, cwd, 0, 0);
}

/* One definition found in a block. */
struct dres
{
	struct xr_span sp;	/* the key; s/e its anchor in out (or cmd) */
	int incmd;
	int ind2;		/* D2: s/e in dsrc.d2out when that is set */
	const char *rule;
	char title[XR_TITLE_MAX * 4 + 1];
	char *passage;
};

struct dset
{
	struct dres *v;
	size_t n, cap;
};

static struct dres *
dpush(struct dset *d)
{
	if (d->n == d->cap)
	{
		size_t nc = d->cap ? d->cap * 2 : 8;
		struct dres *v = realloc(d->v, nc * sizeof *v);

		if (!v)
			return NULL;
		d->v = v;
		d->cap = nc;
	}
	memset(&d->v[d->n], 0, sizeof d->v[d->n]);
	return &d->v[d->n++];
}

static void
dfree(struct dset *d)
{
	size_t k;

	for (k = 0; k < d->n; k++)
		free(d->v[k].passage);
	free(d->v);
	memset(d, 0, sizeof *d);
}

/* A passage: the scalars of s[0..n), at most XR_PASSAGE, malloc'd. */
static char *
passage_of(const char *s, size_t n)
{
	size_t l;
	const char *t = trim(s, &n);
	char *p;

	l = clip(t, n, XR_PASSAGE);
	if (!l || !(p = malloc(l + 1)))
		return NULL;
	memcpy(p, t, l);
	p[l] = '\0';
	return p;
}

/* The id part of a key (D2, D4): n for #n / repo#n / owner/repo#n, the
** whole text for KEY-N. */
static const char *
id_part(const struct xr_span *sp)
{
	const char *t = xr_span_text(sp), *h = strrchr(t, '#');

	return sp->shape == XR_TICKET ? t : h ? h + 1 : t;
}

struct idm
{
	const char *v;
	size_t vl;
	struct xr_span sp;
	int found;
};

static int
idm_span(void *ctx, const struct xr_span *sp)
{
	struct idm *m = ctx;
	const char *ip = id_part(sp);

	if ((sp->shape == XR_TICKET || sp->shape == XR_ISSUE ||
	     sp->shape == XR_PULL) && strlen(ip) == m->vl &&
	    !memcmp(ip, m->v, m->vl))
	{
		m->sp = *sp;
		m->found = 1;
		return 1;
	}
	return 0;
}

/* D2/D4's key for an id value v: a ref on the command line whose id part
** equals it; else #v for digits; else v itself when rule T matches it. */
static int
id_resolve(const char *v, size_t vl, const char *cmd, size_t cl,
           const char *cwd, struct xr_span *sp)
{
	struct idm m = { v, vl, { 0 }, 0 };
	char t[16];
	size_t k;

	if (!vl || vl > 64)
		return 0;
	sp->full[0] = '\0';	/* the r_ticket path sets no full (#147) */
	if (cmd)
		(void)grammar(cmd, cl, XR_PLAIN, R_ANCHOR, cwd, idm_span, &m);
	if (m.found)
	{
		*sp = m.sp;
		return 1;
	}
	for (k = 0; k < vl && A_DIG((unsigned char)v[k]); k++)
		;
	if (k == vl && vl <= 7)
	{
		t[0] = '#';
		memcpy(t + 1, v, vl);
		sp->rule = 'H';
		return span_key(sp, XR_ISSUE, t, vl + 1, 0);
	}
	return r_ticket(v, vl, 0, sp) == vl;
}

/* The output's lines: [ls, le) each, '\r' off, and whether a fence holds it
** (exclusion #2). */
struct olines
{
	size_t n;
	size_t *ls, *le;
	char *fence;
};

static int
olines(const char *s, size_t n, struct olines *o)
{
	size_t k = 0, i = 0, cnt = 1, fl = 0;
	char fc = 0;
	int in = 0;

	for (i = 0; i < n; i++)
		cnt += s[i] == '\n';
	o->ls = malloc(cnt * sizeof *o->ls);
	o->le = malloc(cnt * sizeof *o->le);
	o->fence = malloc(cnt);
	if (!o->ls || !o->le || !o->fence)
		return -1;
	for (i = 0; i <= n && k < cnt; k++)
	{
		size_t e = line_end(s, n, i);

		o->ls[k] = i;
		o->le[k] = e > i && s[e - 1] == '\r' ? e - 1 : e;
		if (in)
		{
			o->fence[k] = 1;
			if (fence_close(s, n, i, fc, fl))
				in = 0;
		}
		else
			o->fence[k] = in = fence_open(s, n, i, &fc, &fl);
		i = e + 1;
	}
	o->n = k;
	return 0;
}

static void
olines_free(struct olines *o)
{
	free(o->ls);
	free(o->le);
	free(o->fence);
}

/* The rest of a heading or a title value as a title. */
static void
title_of(const char *s, size_t n, char *out)
{
	xr_title(s, n | XR_WHOLE, out, XR_TITLE_MAX * 4 + 1);
}

/* D1 at line k: `^#{1,6} ` and the ref as the first token. */
static int
d1(const char *s, const struct olines *o, size_t k, const char *cwd,
   struct dres *d)
{
	size_t p = o->ls[k], le = o->le[k], h = 0, e, t, j, got = 0, pl = 0;
	char pb[XR_PASSAGE * 4 + 8];

	while (p < le && s[p] == '#' && h < 7)
		p++, h++;
	if (h < 1 || h > 6 || p >= le || s[p] != ' ' ||
	    !anchor_at(s, le, ++p, R_ANCHOR, cwd, &d->sp))
		return 0;
	e = d->sp.e;
	if (e == le)
		t = le;
	else if (s[e] == ':')
		t = e + 1;
	else if (le - e >= 3 && !memcmp(s + e, " - ", 3))
		t = e + 3;
	else if (le - e >= 5 && !memcmp(s + e, " \xe2\x80\x94 ", 5))
		t = e + 5;
	else if (s[e] == ' ')
		t = e + 1;
	else
		return 0;
	title_of(s + t, le - t, d->title);
	if (!d->title[0])
		return 0;	/* `## #831` alone: an empty title never defines */
	/* the passage: up to 6 following non-blank lines */
	for (j = k + 1; j < o->n && got < 6 && pl < sizeof pb - 8; j++)
	{
		size_t l = o->le[j] - o->ls[j];

		if (o->fence[j] || blank(s + o->ls[j], l))
			continue;
		if (l > sizeof pb - 8 - pl)
			l = sizeof pb - 8 - pl;
		if (pl)
			pb[pl++] = '\n';
		memcpy(pb + pl, s + o->ls[j], l);
		pl += l;
		got++;
	}
	pl = u8_cut(pb, pl);
	d->passage = passage_of(pb, pl);
	d->rule = "D1";
	return 1;
}

/* ^\s*<word>\s*: then the value; its start, or 0. */
static size_t
field_line(const char *s, size_t ls, size_t le, const char *word)
{
	size_t p = ls, wl = strlen(word);

	while (p < le && (s[p] == ' ' || s[p] == '\t'))
		p++;
	if (le - p < wl + 1 || strncasecmp(s + p, word, wl))
		return 0;
	p += wl;
	while (p < le && (s[p] == ' ' || s[p] == '\t') && strcmp(word, "title"))
		p++;
	if (p >= le || s[p] != ':')
		return 0;
	for (p++; p < le && (s[p] == ' ' || s[p] == '\t'); p++)
		;
	return p < le ? p : 0;
}

struct same_shape
{
	int shape;
	const char *key;
	int other;
};

static int
same_shape(void *ctx, const struct xr_span *sp)
{
	struct same_shape *x = ctx;

	if (sp->shape == x->shape && strcmp(sp->key, x->key))
		x->other = 1;
	return x->other;
}

/* D4 at line k: the ref as the line's first token (or a number:/id: line's
** value), and a `title:` line within 3 either way, with no other ref of its
** shape in the window. */
static int
d4(const char *s, const struct olines *o, size_t k, const char *cmd,
   size_t cl, const char *cwd, struct dres *d)
{
	size_t p = o->ls[k], le = o->le[k], v, j, lo, hi, tl = 0, pl = 0, tv = 0;
	char pb[XR_PASSAGE * 4 + 8];
	struct same_shape ss;

	while (p < le && (s[p] == ' ' || s[p] == '\t'))
		p++;
	if (anchor_at(s, le, p, R_ANCHOR, cwd, &d->sp) &&
	    (d->sp.e == le || s[d->sp.e] == ' ' || s[d->sp.e] == '\t' ||
	     s[d->sp.e] == ':'))
		;
	else if ((v = field_line(s, o->ls[k], le, "number")) ||
	         (v = field_line(s, o->ls[k], le, "id")))
	{
		size_t ve = le;

		while (ve > v && (u_space((unsigned char)s[ve - 1]) ||
		                  s[ve - 1] == ','))
			ve--;
		if (ve - v >= 2 && (s[v] == '"' || s[v] == '\'') &&
		    s[ve - 1] == s[v])
			v++, ve--;
		if (!id_resolve(s + v, ve - v, cmd, cl, cwd, &d->sp))
			return 0;
		d->sp.s = v;
		d->sp.e = ve;
	}
	else
		return 0;
	lo = k >= 3 ? k - 3 : 0;
	hi = k + 3 < o->n ? k + 3 : o->n - 1;
	for (j = lo; j <= hi && !tl; j++)
		if (j != k && !o->fence[j] &&
		    (tv = field_line(s, o->ls[j], o->le[j], "title")))
			tl = j + 1;
	if (!tl)
		return 0;
	ss.shape = d->sp.shape;
	ss.key = d->sp.key;
	ss.other = 0;
	for (j = lo; j <= hi && !ss.other; j++)
		(void)grammar(s + o->ls[j], o->le[j] - o->ls[j], XR_PLAIN, R_PROSE,
		              cwd, same_shape, &ss);
	if (ss.other)
		return 0;
	title_of(s + tv, o->le[tl - 1] - tv, d->title);
	for (j = lo; j <= hi && pl < sizeof pb - 8; j++)
	{
		size_t l = o->le[j] - o->ls[j];

		if (j == k || j == tl - 1 || blank(s + o->ls[j], l))
			continue;
		if (l > sizeof pb - 8 - pl)
			l = sizeof pb - 8 - pl;
		if (pl)
			pb[pl++] = '\n';
		memcpy(pb + pl, s + o->ls[j], l);
		pl += l;
	}
	pl = u8_cut(pb, pl);
	d->passage = passage_of(pb, pl);
	d->rule = "D4";
	return 1;
}

static const char *const A_IDF[] = { "id", "number", "entry_id", "key", "iid",
                                     0 };
static const char *const A_TITLEF[] = { "title", "heading", "summary", "name",
                                        0 };

/* D2: the trimmed output one JSON object, or an array of at most 200, within
** XR_D2_MAX. */
static void
d2(const char *out, size_t ol, const char *cmd, size_t cl, const char *cwd,
   struct dset *ds)
{
	size_t n = ol, k;
	const char *t = trim(out, &n);
	struct json_parser p;
	char *buf;
	int objs[200], no = 0, i;

	if (!n || n > XR_D2_MAX || (t[0] != '{' && t[0] != '[') ||
	    memchr(t, '\0', n) || !(buf = malloc(n + 1)))
		return;
	memcpy(buf, t, n);
	buf[n] = '\0';
	memset(&p, 0, sizeof p);
	if (tokens_max(buf, n) > XR_LINE_TOKENS || dch_json_parse(&p, buf) < 0)
		goto out;
	if (p.tok[0].kind == JSON_OBJECT)
		objs[no++] = 0;
	else if (p.tok[0].kind == JSON_ARRAY)
		for (i = 1; i < p.tok[0].next; i = p.tok[i].next)
		{
			if (no == 200 || p.tok[i].kind != JSON_OBJECT)
				goto out;
			objs[no++] = i;
		}
	for (k = 0; k < (size_t)no && ds->n < XR_DEFS; k++)
	{
		int iv = -1, tv = -1, bv, a;
		char *v, *ti, *bd;
		size_t vl, tl, bl, x;
		struct dres *d;
		struct xr_span sp;
		const char *rt, *tp;

		for (a = 0; A_IDF[a] && iv < 0; a++)
			if ((iv = j_field(&p, objs[k], A_IDF[a])) >= 0 &&
			    p.tok[iv].kind != JSON_STRING &&
			    p.tok[iv].kind != JSON_PRIMITIVE)
				iv = -1;
		for (a = 0; A_TITLEF[a] && tv < 0; a++)
			if ((tv = j_field(&p, objs[k], A_TITLEF[a])) >= 0)
			{
				size_t l = 0;
				char *s = j_str(&p, tv, &l);

				if (!s || blank(s, l))
					tv = -1;
				free(s);
			}
		if (iv < 0 || tv < 0)
			continue;
		if (p.tok[iv].kind == JSON_STRING)
			v = j_str(&p, iv, &vl);
		else if ((v = malloc((size_t)(p.tok[iv].end - p.tok[iv].start) + 1)))
		{
			vl = (size_t)(p.tok[iv].end - p.tok[iv].start);
			memcpy(v, buf + p.tok[iv].start, vl);
			v[vl] = '\0';
		}
		if (!v)
			continue;
		if (!id_resolve(v, vl, cmd, cl, cwd, &sp) || !(d = dpush(ds)))
		{
			free(v);
			continue;
		}
		free(v);
		d->sp = sp;
		d->sp.s = (size_t)(t - out) + (size_t)p.tok[iv].start;
		d->sp.e = (size_t)(t - out) + (size_t)p.tok[iv].end;
		d->rule = "D2";
		d->ind2 = 1;
		/* the title, leading # markup and the ref itself stripped */
		ti = j_str(&p, tv, &tl);
		tp = ti ? ti : "";
		for (x = 0; tp[x] == '#'; x++)
			;
		if (x && (tp[x] == ' ' || tp[x] == '\t'))
			for (tp += x; *tp == ' ' || *tp == '\t'; tp++)
				;	/* heading markup, then the ref */
		rt = xr_span_text(&sp);
		x = strlen(rt);
		if (!strncasecmp(tp, rt, x) && after_ok(tp, strlen(tp), x))
		{
			tp += x;
			while (*tp == ' ' || *tp == ':' || *tp == '-' || *tp == '\t')
				tp++;
			if (!strncmp(tp, "\xe2\x80\x94", 3))
				for (tp += 3; *tp == ' '; tp++)
					;
		}
		title_of(tp, strlen(tp), d->title);
		free(ti);
		bv = j_field(&p, objs[k], "body");
		if (bv < 0 || p.tok[bv].kind != JSON_STRING)
			bv = j_field(&p, objs[k], "description");
		if (bv >= 0 && (bd = j_str(&p, bv, &bl)))
		{
			d->passage = passage_of(bd, bl);
			free(bd);
		}
	}
out:
	dch_json_free(&p);
	free(buf);
}

/* A block's command and output, as the definition rules read them. */
struct dsrc
{
	const char *out;	/* NUL-terminated */
	size_t ol;
	const char *cmd;	/* NULL: a generic card (no command) */
	size_t cl;
	int bash, failed, noexit, d2ok;
	const char *cwd;
	/* D2's source when out is a clip of it: the whole result (Q5) */
	const char *d2out;	/* NUL-terminated; NULL: out */
	size_t d2l;
};

struct d3m
{
	size_t xs, xe;		/* the --title word: skipped */
	struct xr_span sp;
	int n, multi;
	struct xr_span one[6];
	int cnt[6];
};

static int
d3_cmd(void *ctx, const struct xr_span *sp)
{
	struct d3m *m = ctx;

	if (sp->e > m->xs && sp->s < m->xe)
		return 0;
	if (!m->n++)
		m->sp = *sp;
	else if (strcmp(m->sp.key, sp->key))
		m->multi = 1;
	return 0;
}

static int
d3_out(void *ctx, const struct xr_span *sp)
{
	struct d3m *m = ctx;

	if (!m->cnt[sp->shape]++)
		m->one[sp->shape] = *sp;
	else if (!strcmp(m->one[sp->shape].key, sp->key))
		m->cnt[sp->shape]--;	/* the same key again */
	return 0;
}

/* D3: --title on a bash command that did not fail. */
static void
d3(const struct dsrc *s, struct dset *ds)
{
	struct sw w;
	struct d3m m;
	const char *val = NULL;
	size_t k;
	struct dres *d;
	char title[XR_TITLE_MAX * 4 + 1];

	shell_words(s->cmd, s->cl, &w);
	if (!w.b)
		return;
	memset(&m, 0, sizeof m);
	for (k = 0; k < w.n && !val; k++)
		if (w.op[k])
			continue;
		else if (!strcmp(SWD(&w, k), "--title") && k + 1 < w.n &&
		         !w.op[k + 1])
		{
			val = SWD(&w, k + 1);
			m.xs = w.rs[k];
			m.xe = w.re[k + 1];
		}
		else if (!strncmp(SWD(&w, k), "--title=", 8))
		{
			val = SWD(&w, k) + 8;
			m.xs = w.rs[k];
			m.xe = w.re[k];
		}
	if (val)
		title_of(val, strlen(val), title);
	free(w.b);
	if (!val || !title[0])
		return;
	(void)grammar(s->cmd, s->cl, XR_PLAIN, R_D3, s->cwd, d3_cmd, &m);
	if (m.n && !m.multi)
	{
		if ((d = dpush(ds)))
		{
			d->sp = m.sp;
			d->incmd = 1;
			d->rule = "D3";
			memcpy(d->title, title, sizeof title);
		}
		return;
	}
	if (m.n)
		return;
	(void)grammar(s->out, s->ol, XR_PLAIN, R_D3, s->cwd, d3_out, &m);
	for (k = 0; k < 6; k++)
		if (m.cnt[k] == 1 && ds->n < XR_DEFS && (d = dpush(ds)))
		{
			d->sp = m.one[k];
			d->rule = "D3";
			memcpy(d->title, title, sizeof title);
		}
}

/* Every definition of one paired block, at most XR_DEFS (v2.1 3.6). D1/D4
** are decided while streaming: a 4th distinct key of one shape is a listing
** (#4) and ends the scan with none, so a block costs O(lines) and at most
** XR_DEFS kept results whatever its size. */
static void
defs_find(const struct dsrc *s, struct dset *ds)
{
	struct olines o;
	size_t k;
	char seen[6][3][XR_KEY_MAX + 16];
	int nseen[6] = {0}, x;

	memset(ds, 0, sizeof *ds);
	if (s->failed || ((x = s->cmd ? excluded(s->cmd, s->cl, s->cwd) : X_NONE) &&
	                  !(x & X_SUB)))
		return;		/* #5; #1, #3, #4 (only in a $(…): D3 still runs) */
	memset(&o, 0, sizeof o);
	if (!x && olines(s->out, s->ol, &o) == 0)
	{
		for (k = 0; k < o.n; k++)
		{
			struct dres t, *d;
			int sh, i;

			memset(&t, 0, sizeof t);
			if (o.fence[k] || (!d1(s->out, &o, k, s->cwd, &t) &&
			    !d4(s->out, &o, k, s->cmd, s->cl, s->cwd, &t)))
				continue;	/* #2: nothing in a fence */
			sh = t.sp.shape;
			for (i = 0; i < nseen[sh] && strcmp(seen[sh][i], t.sp.key); i++)
				;
			if (i == nseen[sh] && nseen[sh] == 3)
			{
				/* #4: more than 3 distinct D1/D4 keys of one shape;
				** D2 and D3 still apply (ruling Q7) */
				free(t.passage);
				dfree(ds);
				break;
			}
			if (i == nseen[sh])
				memcpy(seen[sh][nseen[sh]++], t.sp.key, sizeof t.sp.key);
			if (ds->n < XR_DEFS && (d = dpush(ds)))
				*d = t;
			else
				free(t.passage);
		}
	}
	olines_free(&o);
	if (!x && s->d2ok && ds->n < XR_DEFS)
		d2(s->d2out ? s->d2out : s->out, s->d2out ? s->d2l : s->ol,
		   s->cmd, s->cl, s->cwd, ds);
	if (s->bash && s->cmd && !s->noexit && ds->n < XR_DEFS)
		d3(s, ds);
}

/* A block's definitions to the sink, each with its anchor as one mention
** unless a code span of the same key already covers it (4.2). out_r and
** cmd_r: the remits the block's output and command were scanned with. */
static void
defs_emit(const struct dsrc *s, struct remit *out_r, struct remit *cmd_r)
{
	struct dset ds;
	size_t k, c;

	defs_find(s, &ds);
	for (k = 0; k < ds.n; k++)
	{
		struct dres *d = &ds.v[k];
		struct remit *r = d->incmd ? cmd_r : out_r;
		uint64_t h = fnv(d->sp.key, strlen(d->sp.key));
		int covered = 0;

		if (!r)
			continue;
		r->text = d->incmd ? s->cmd :
		          d->ind2 && s->d2out ? s->d2out : s->out;
		r->ctext = NULL;
		rm_hit(r, &d->sp, d->title, d->passage ? d->passage : NULL, d->rule);
		for (c = 0; c < r->ncov && !covered; c++)
			covered = r->cov[c].h == h && r->cov[c].s < d->sp.e &&
			          d->sp.s < r->cov[c].e;
		if (!covered)
			rm_hit(r, &d->sp, NULL, NULL, NULL);
	}
	dfree(&ds);
}

/* ---- refs: the index (3.2) ------------------------------------------------- */

struct xr_ent
{
	char *key;
	int shape;
	char full[41];
	uint64_t nunit, lastunit, nrow, lastrow, first;
	int ndefs;
	uint64_t defh[XR_DEFS];
	char *title;
};

#define XR_HASH 4096		/* slots, > 2 x XR_KEYS */
#define XR_DROPSET 65536

struct xr_index
{
	struct xr_ent *v;
	size_t n;
	uint32_t slot[XR_HASH];	/* entry + 1 */
	uint64_t *drop;		/* dropped keys' hashes, open addressing */
	uint64_t dropped;
	uint64_t ord;
	int per_row;
};

unsigned long xr_index_live;

struct xr_index *
xr_index_new(void)
{
	struct xr_index *x = calloc(1, sizeof *x);

	if (x && !(x->v = calloc(XR_KEYS, sizeof *x->v)))
	{
		free(x);
		return NULL;
	}
	if (x)
		xr_index_live++;
	return x;
}

void
xr_index_free(struct xr_index *x)
{
	size_t k;

	if (!x)
		return;
	for (k = 0; k < x->n; k++)
	{
		free(x->v[k].key);
		free(x->v[k].title);
	}
	free(x->v);
	free(x->drop);
	free(x);
	xr_index_live--;
}

void
xr_index_per_row(struct xr_index *x, int per_row)
{
	x->per_row = per_row;
}

size_t
xr_index_keys(const struct xr_index *x)
{
	return x->n;
}

static void
ix_drop(struct xr_index *x, uint64_t h)
{
	size_t i;

	if (!h)
		h = 1;
	if (!x->drop && !(x->drop = calloc(XR_DROPSET, sizeof *x->drop)))
	{
		x->dropped = x->dropped ? x->dropped : 1;	/* a lower bound */
		return;
	}
	for (i = h % XR_DROPSET; x->drop[i]; i = (i + 1) % XR_DROPSET)
		if (x->drop[i] == h)
			return;
	/* the set stops at half load, so a probe stays short: past 32768
	** distinct dropped keys, dropped_keys is a lower bound */
	if (x->dropped < XR_DROPSET / 2)
	{
		x->drop[i] = h;
		x->dropped++;
	}
}

static struct xr_ent *
ix_get(struct xr_index *x, const struct xr_span *sp)
{
	uint64_t h = fnv(sp->key, strlen(sp->key));
	size_t i;
	struct xr_ent *e;

	for (i = h % XR_HASH; x->slot[i]; i = (i + 1) % XR_HASH)
		if (!strcmp(x->v[x->slot[i] - 1].key, sp->key))
			return &x->v[x->slot[i] - 1];
	if (x->n == XR_KEYS)
	{
		ix_drop(x, h);
		return NULL;
	}
	e = &x->v[x->n];
	memset(e, 0, sizeof *e);
	if (!(e->key = strdup(sp->key)))
		return NULL;
	e->shape = sp->shape;
	e->first = x->ord++;
	x->slot[i] = (uint32_t)++x->n;
	return e;
}

static void
ix_hit(void *ctx, const struct xr_hit *h)
{
	struct xr_index *x = ctx;
	struct xr_ent *e = ix_get(x, h->sp);
	int k;

	if (!e)
		return;
	if (strlen(h->sp->full) > strlen(e->full))
		snprintf(e->full, sizeof e->full, "%s", h->sp->full);
	if (h->title)
	{
		uint64_t th = title_hash(h->title);
		char *t;

		for (k = 0; k < e->ndefs && e->defh[k] != th; k++)
			;
		if (k == e->ndefs && e->ndefs < XR_DEFS)
			e->defh[e->ndefs++] = th;
		if (!e->ndefs)
			e->ndefs = 1;	/* defined: never 0 past the cap */
		if ((t = strdup(h->title)))
		{
			free(e->title);
			e->title = t;	/* the newest definition's */
		}
		return;
	}
	if (!e->nunit || e->lastunit != h->unit)
	{
		e->nunit++;
		e->lastunit = h->unit;
	}
	if (!e->nrow || e->lastrow != h->row)
	{
		e->nrow++;
		e->lastrow = h->row;
	}
}

void
xr_index_sink(struct xr_index *x, struct xr_sink *k)
{
	k->hit = ix_hit;
	k->ctx = x;
}

static uint64_t
ix_count(const struct xr_index *x, const struct xr_ent *e)
{
	return x->per_row ? e->nrow : e->nunit;
}

/* ---- refs: the --ref scan's lines (3.4) ------------------------------------ */

#define XR_HELD 199		/* a file's newest mentions, after its first */

struct xr_scan
{
	FILE *f;
	char key[XR_KEY_MAX + 16];
	const char *session, *project;	/* this file's session */
	int live, full, shell, budget;
	int scope, other, same;	/* --scope; this file another session's, in
				** <n>'s project dir */
	long long t0;
	uint64_t written;
	size_t room;		/* kept under the cap for the tally and done */
	/* this file */
	uint64_t nunit, lastunit, nrow, lastrow, lines, lastoff, lasth, foff;
	int found, ndefs, mentioned;
	uint64_t defh[XR_DEFS];
	char *held[XR_HELD];
	size_t hn, h0;
	uint64_t dropped;	/* mention lines the XR_HELD bound left out */
	/* the scan */
	uint64_t files, bytes, base, too_long;
	uint64_t cs, cp, cps, ca, cas;	/* 3.4's counts, files done */
	int stop, out_full;
	int cancel;		/* a write failed: 1 EPIPE (the reader left), 2 other */
	const char *why;
	long long last;
};

long long
xr_now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* A file's tally line (3.4); mentions_dropped: the mention lines past the
** first and the newest 199, counted in count but not written. */
static char *
tally_line(const struct xr_scan *x, uint64_t count, uint64_t dropped,
           int partial)
{
	char *b = NULL;
	size_t bl;
	FILE *m = open_memstream(&b, &bl);

	if (!m)
		return NULL;
	fputs("{\"schema\":1,\"tally\":{\"session\":", m);
	xr_put_str(m, x->session);
	fprintf(m, ",\"live\":%s,\"project\":", x->live ? "true" : "false");
	xr_put_str(m, x->project);
	/* partial: the per-file range cap cut it (3.4); a stop says so in done */
	fprintf(m, ",\"count\":%llu,\"definitions\":%d,\"partial\":%s,"
	        "\"mentions_dropped\":%llu}}\n", (unsigned long long)count,
	        x->ndefs, partial ? "true" : "false",
	        (unsigned long long)dropped);
	if (fclose(m) != 0)
	{
		free(b);
		return NULL;
	}
	return b;
}

struct xr_scan *
xr_scan_new(FILE *f, const char *key, const char *session,
            const char *project, int live, int full, int shell, int budget,
            long long t0)
{
	struct xr_scan *x = calloc(1, sizeof *x);
	char *t;

	if (!x)
		return NULL;
	x->f = f;
	snprintf(x->key, sizeof x->key, "%s", key);
	x->session = session;
	x->project = project;
	x->live = live;
	x->full = full;
	x->shell = shell;
	x->budget = budget;
	x->t0 = x->last = t0;
	/* room for done and the widest tally: both always go (3.4) */
	if (!(t = tally_line(x, UINT64_MAX, UINT64_MAX, 1)))
	{
		free(x);
		return NULL;
	}
	x->room = 1024 + strlen(t);
	free(t);
	return x;
}

void
xr_scan_free(struct xr_scan *x)
{
	size_t k;

	if (!x)
		return;
	for (k = 0; k < x->hn; k++)
		free(x->held[(x->h0 + k) % XR_HELD]);
	free(x);
}

/* A failed write ends the scan as a cancel (FROZEN 3.4): nothing more is
** written, `done` included. */
static int
sc_fail(struct xr_scan *x, int epipe)
{
	if (!x->cancel)
		x->cancel = epipe ? 1 : 2;
	x->stop = 1;
	x->why = "cancel";
	return -1;
}

/* stdio's verdict on x->f after a write or flush that returned ok. */
static int
sc_check(struct xr_scan *x, int ok)
{
	return ok && !ferror(x->f) ? 0 : sc_fail(x, errno == EPIPE);
}

static int
sc_put(struct xr_scan *x, const char *l, size_t n)
{
	if (sc_check(x, fwrite(l, 1, n, x->f) == n) != 0)
		return -1;
	x->written += n;
	return 0;
}

/* One line under the 4 MiB cap, with room kept for the tally and `done`
** (3.4). */
static int
sc_line(struct xr_scan *x, const char *l, size_t n)
{
	if (x->out_full || x->cancel)
		return -1;
	if (x->written + n + x->room > xr_lim.out)
	{
		x->out_full = x->stop = 1;
		x->why = "output";
		return -1;
	}
	return sc_put(x, l, n);
}

/* A line built on a memstream, then sent. */
static FILE *
ln_open(char **b, size_t *bl)
{
	*b = NULL;
	return open_memstream(b, bl);
}

/* *b is the memstream's buffer: valid only once m is closed. */
static void
ln_send(struct xr_scan *x, FILE *m, char **b, size_t *bl)
{
	if (fclose(m) == 0)
		(void)sc_line(x, *b, *bl);
	free(*b);
}

static void
put_at(FILE *m, long long at)
{
	if (at >= 0)
		fprintf(m, "%lld", at);
	else
		fputs("null", m);
}

/* The line around the span, at most 200 scalars (3.4). */
static char *
cut_line(const char *l, size_t ll, size_t s, size_t e)
{
	size_t tot = scalars(l, ll), ss = scalars(l, s), sl = scalars(l + s, e - s);
	size_t from = 0, a, b, k, i;
	char *o;

	if (tot > 200)
	{
		if (sl >= 200)
			from = ss;
		else
		{
			from = ss > (200 - sl) / 2 ? ss - (200 - sl) / 2 : 0;
			if (from + 200 > tot)
				from = tot - 200;
		}
	}
	for (i = k = 0; i < ll && k < from; k++)
		(void)u8_next((const unsigned char *)l + i, ll - i, &a), i += a;
	b = i + clip(l + i, ll - i, 200);
	if (!(o = malloc(b - i + 1)))
		return NULL;
	memcpy(o, l + i, b - i);
	o[b - i] = '\0';
	return o;
}

static char *
mention_line(struct xr_scan *x, const struct xr_hit *h)
{
	char *b, *line = cut_line(h->line, h->ll, h->sp->s, h->sp->e);
	size_t bl;
	FILE *m = ln_open(&b, &bl);

	if (!m || !line)
	{
		if (m)
			fclose(m);
		free(line);
		free(m ? b : NULL);
		return NULL;
	}
	fputs("{\"schema\":1,\"mention\":{\"session\":", m);
	xr_put_str(m, x->session);
	fputs(",\"chapter\":", m);
	put_or_null(m, x->other ? NULL : h->chapter);	/* <n>'s only */
	fputs(",\"at\":", m);
	put_at(m, h->at);
	fputs(",\"line\":", m);
	xr_put_str(m, line);
	fprintf(m, ",\"offset\":%llu,\"live\":%s,\"project\":",
	        (unsigned long long)h->off, x->live ? "true" : "false");
	xr_put_str(m, x->project);
	fputs("}}\n", m);
	free(line);
	if (fclose(m) != 0)
	{
		free(b);
		return NULL;
	}
	return b;
}

static void
sc_hit(void *ctx, const struct xr_hit *h)
{
	struct xr_scan *x = ctx;
	char *b;
	size_t bl;
	FILE *m;

	if (!x->stop && x->found && x->shell && h->off != x->foff)
	{
		x->stop = 1;	/* a shell's first result: its row is done */
		x->why = "first";
	}
	if (x->stop || strcmp(h->sp->key, x->key))
		return;
	if (h->title)
	{
		uint64_t th = title_hash(h->title);
		int k;

		for (k = 0; k < x->ndefs && x->defh[k] != th; k++)
			;
		/* a line per distinct title, at most XR_DEFS (3.8) */
		if (k < x->ndefs || x->ndefs == XR_DEFS)
			goto anchor;
		x->defh[x->ndefs++] = th;
		if (!(m = ln_open(&b, &bl)))
			return;
		fputs("{\"schema\":1,\"definition\":{\"session\":", m);
		xr_put_str(m, x->session);
		fprintf(m, ",\"live\":%s,\"project\":", x->live ? "true" : "false");
		xr_put_str(m, x->project);
		fputs(",\"at\":", m);
		put_at(m, h->at);
		fputs(",\"title\":", m);
		xr_put_str(m, h->title);
		/* a shell <n> and every other session in light mode: titles and
		** counts only (S18, K4.2) */
		if (x->full || (!x->shell && !x->other))
		{
			fputs(",\"passage\":", m);
			put_or_null(m, h->passage);
		}
		fprintf(m, ",\"rule\":\"%s\",\"offset\":%llu,\"chapter\":", h->rule,
		        (unsigned long long)h->off);
		put_or_null(m, x->other ? NULL : h->chapter);
		fputs("}}\n", m);
		ln_send(x, m, &b, &bl);
	}
	else
	{
		int newrow = !x->nrow || x->lastrow != h->row;
		uint64_t lh = fnv(h->line, h->ll);
		int newline = !x->lines++ || x->lastoff != h->off ||
		              x->lasth != lh;

		if (!x->nunit || x->lastunit != h->unit)
		{
			x->nunit++;
			x->lastunit = h->unit;
		}
		if (newrow)
		{
			x->nrow++;
			x->lastrow = h->row;
		}
		/* one line per source line (a Claude entry, a shell row); never
		** in light mode for a shell <n> (S18) or another session (K4.2) */
		x->lastoff = h->off;
		x->lasth = lh;
		if (newline && (x->full || (!x->shell && !x->other)) &&
		    (b = mention_line(x, h)))
		{
			if (!x->mentioned++)
			{
				(void)sc_line(x, b, strlen(b));
				free(b);
			}
			else
			{
				if (x->hn == XR_HELD)
				{
					free(x->held[x->h0]);
					x->h0 = (x->h0 + 1) % XR_HELD;
					x->hn--;
					x->dropped++;
				}
				x->held[(x->h0 + x->hn++) % XR_HELD] = b;
			}
		}
	}
	/* light: the first result (a wider scope: another session's, R5) ends
	** the scan once its line (a shell: its row) is done, so a definition
	** keeps its anchor's mention */
anchor:
	if (!x->full && !x->found && (!x->scope || x->other))
	{
		x->found = 1;
		x->foff = h->off;
	}
}

void
xr_scan_sink(struct xr_scan *x, struct xr_sink *k)
{
	k->hit = sc_hit;
	k->ctx = x;
}

/* 3.4's counts: the files done, plus cur, this file's so far */
static void
put_counts(FILE *m, const struct xr_scan *x, uint64_t cur)
{
	uint64_t s = x->cs, p = x->cp, ps = x->cps, a = x->ca, as = x->cas;

	if (!x->other)
		s += cur;
	else if (x->same)
		p += cur, ps += cur > 0;
	else
		a += cur, as += cur > 0;
	fprintf(m, "\"counts\":{\"session\":%llu,\"project\":%llu,"
	        "\"project_sessions\":%llu,\"all\":", (unsigned long long)s,
	        (unsigned long long)p, (unsigned long long)ps);
	if (x->scope == 2)
		fprintf(m, "%llu,\"all_sessions\":%llu}", (unsigned long long)a,
		        (unsigned long long)as);
	else
		fputs("null,\"all_sessions\":null}", m);
}

int
xr_scan_tick(void *ctx, uint64_t bytes)
{
	struct xr_scan *x = ctx;
	long long now = xr_now_ms();

	x->bytes = x->base + bytes;
	if (!x->stop && x->found)
	{
		if (!x->shell)	/* a shell's: its row ends the scan (sc_hit) */
		{
			x->stop = 1;
			x->why = "first";
		}
	}
	else if (!x->stop && !x->full && now - x->t0 >= x->budget)
	{
		x->stop = 1;
		x->why = "budget";
	}
	else if (!x->stop && x->full && now - x->t0 >= xr_lim.ms)
	{
		x->stop = 1;
		x->why = "time";
	}
	else if (!x->stop && x->bytes > xr_lim.bytes)
	{
		x->stop = 1;
		x->why = "bytes";
	}
	/* one progress line per 250 ms window while the scan runs (R6) */
	if (!x->stop && now - x->last >= 250)
	{
		char *b;
		size_t bl;
		FILE *m = ln_open(&b, &bl);

		x->last += (now - x->last) / 250 * 250;
		if (m)
		{
			fprintf(m, "{\"schema\":1,\"progress\":{\"files\":%llu,"
			        "\"bytes\":%llu,\"partial\":true,",
			        (unsigned long long)x->files + 1,
			        (unsigned long long)x->bytes);
			put_counts(m, x, x->nunit);
			fputs("}}\n", m);
			ln_send(x, m, &b, &bl);
			if (!x->cancel)
				(void)sc_check(x, fflush(x->f) == 0);
		}
	}
	return x->stop;
}

void
xr_scan_file(struct xr_scan *x, uint64_t bytes, uint64_t too_long,
             int partial, int per_row)
{
	char *b;
	size_t bl, k;
	uint64_t count = per_row ? x->nrow : x->nunit;

	x->files++;
	if (x->found && !x->stop)
	{
		x->stop = 1;	/* a shell's first result, its row the last read */
		x->why = "first";
	}
	if (x->stop && !x->shell && x->bytes >= x->base)
		bytes = x->bytes - x->base;	/* stopped: what the ticks saw */
	x->base += bytes;
	x->bytes = x->base;
	x->too_long += too_long;
	for (k = 0; k < x->hn; k++)
	{
		char *l = x->held[(x->h0 + k) % XR_HELD];

		(void)sc_line(x, l, strlen(l));
		free(l);
	}
	x->hn = x->h0 = 0;
	if (!x->other)
		x->cs = count;
	else if (x->same)
		x->cp += count, x->cps += count > 0;
	else
		x->ca += count, x->cas += count > 0;
	b = tally_line(x, count, x->dropped, partial);
	/* the next file starts from nothing; found and the stop carry on */
	x->nunit = x->lastunit = x->nrow = x->lastrow = x->lines = 0;
	x->lastoff = x->lasth = 0;
	x->ndefs = x->mentioned = 0;
	x->dropped = 0;
	if (!b)
		return;
	/* the tally goes past a full output: sc_line kept its room */
	bl = strlen(b);
	if (!x->cancel && x->written + bl + 1024 <= xr_lim.out)
		(void)sc_put(x, b, bl);
	free(b);
	if (partial && !x->why)
		x->why = "range";
}

int
xr_scan_first(struct xr_scan *x, const char *session_id, int shape,
              const char *text)
{
	char *b;
	size_t bl;
	FILE *m = ln_open(&b, &bl);
	int rc;

	if (!m)
		return -1;
	fputs("{\"schema\":1,\"first\":{\"session\":", m);
	xr_put_str(m, x->session);
	fputs(",\"session_id\":", m);
	xr_put_str(m, session_id);
	fputs(",\"key\":", m);
	xr_put_str(m, x->key);
	fprintf(m, ",\"shape\":\"%s\",\"text\":", xr_shape_name[shape]);
	xr_put_str(m, text);
	fputs(",\"project\":", m);
	xr_put_str(m, x->project);
	fprintf(m, ",\"mode\":\"%s\",\"scope\":\"%s\",\"budget_ms\":",
	        x->full ? "full" : "light",
	        x->scope == 2 ? "all" : x->scope ? "project" : "session");
	if (x->full)
		fputs("null", m);
	else
		fprintf(m, "%d", x->budget);
	fprintf(m, ",\"elapsed_ms\":%lld}}\n", xr_now_ms() - x->t0);
	if (fclose(m) != 0)
	{
		free(b);
		return -1;
	}
	rc = sc_line(x, b, strlen(b));
	free(b);
	return rc == 0 ? sc_check(x, fflush(x->f) == 0) : -1;
}

int
xr_scan_done(struct xr_scan *x)
{
	int partial = x->stop || x->why != NULL;

	if (x->cancel)
		return -1;	/* a cancel has no done line */
	fprintf(x->f, "{\"schema\":1,\"done\":true,\"partial\":%s,",
	        partial ? "true" : "false");
	put_counts(x->f, x, 0);
	fputs(",\"why\":", x->f);
	put_or_null(x->f, partial ? x->why : NULL);
	fprintf(x->f, ",\"files\":%llu,\"bytes\":%llu,\"lines_too_long\":%llu}\n",
	        (unsigned long long)x->files, (unsigned long long)x->bytes,
	        (unsigned long long)x->too_long);
	return sc_check(x, fflush(x->f) == 0);
}

int
xr_scan_cancelled(const struct xr_scan *x)
{
	return x->cancel;
}

void
xr_scan_scope(struct xr_scan *x, int scope)
{
	x->scope = scope;
}

int
xr_scan_next(struct xr_scan *x, const char *session, const char *project,
             int live, int same)
{
	if (!x->stop && (x->files >= xr_lim.files || x->bytes >= xr_lim.bytes))
	{
		x->stop = 1;	/* a cap used up: the next file is not opened */
		x->why = x->files >= xr_lim.files ? "files" : "bytes";
	}
	if (xr_scan_tick(x, 0))	/* the budget, 10 s, a result, progress */
		return 1;
	x->session = session;
	x->project = project;
	x->live = live;
	x->same = same;
	x->other = 1;
	x->shell = 0;	/* a Claude file: <n>'s shell rules end with its ring */
	return 0;
}

/* ---- Claude: the assembler ------------------------------------------------- */

#define XR_THREADS 1024
#define XR_ID_MAX 64

/* An open call as the refs see it: its tier and, for Bash, its command. */
enum { RC_BASH, RC_OTHER, RC_SEARCH, RC_CARD };
struct rcall
{
	char id[XR_ID_MAX + 1];
	int kind;
	char *cmd;		/* RC_BASH: <= XR_BASH_MAX bytes */
	size_t cl;
	char chapter[48];	/* the call's chapter anchor, "" for none */
	uint64_t off;
	long long at;
};

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
	const char *harness;	/* the prompt being blocked is harness text:
				** its summary ("" for none), else NULL */
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
	/* refs (#135): rk.hit NULL for chapters only */
	struct xr_sink rk;
	uint64_t ino, unit, too_long;
	int (*tick)(void *ctx, uint64_t bytes);
	void *tctx;
	int halt;
	struct rcall *calls;	/* the refs ring: XR_CALLS open calls, oldest
				** first, apart from pend (3.6, QA A2) */
	int ncalls;
	uint64_t next;		/* the last read's resume offset: the start of
				** the line it did not finish (#136) */
	struct xr_entries *want;	/* --chapter (#156), else NULL */
	int codex;		/* a Codex rollout (7.4) */
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
	for (k = 0; k < d->ncalls; k++)
		free(d->calls[k].cmd);
	free(d->calls);
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

static void ch_anchor(struct xr_claude *d, char out[48]);
static int ent_hit(struct xr_entries *e, const char *anchor, uint64_t ord);

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
		if (d->want)
		{
			char a[48];

			ch_anchor(d, a);
			(void)ent_hit(d->want, a, d->ch.total - 1);
		}
	}
	x = cur(d);
	if (x && b == B_PROMPT && !d->titled)
	{
		/* a partial first chapter takes the prompt riding in its lead */
		x->kind = kind;
		snprintf(x->title, sizeof x->title, "%s", title);
		if ((x->harness = d->harness != NULL))
			snprintf(x->summary, sizeof x->summary, "%s", d->harness);
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

/* ---- Claude: refs (v2.1 3.1, 3.6; spec 7.2) -------------------------------- */

/* The open chapter's anchor, as xr_claude_end will write it ("" for none). */
static void
ch_anchor(struct xr_claude *d, char out[48])
{
	struct xr_chapter *x = cur(d);

	out[0] = '\0';
	if (x)
		snprintf(out, 48, "f%llx:%llu", (unsigned long long)d->ino,
		         (unsigned long long)x->offset);
}

static void
rm_claude(struct xr_claude *d, struct remit *r, const char *chapter)
{
	memset(r, 0, sizeof *r);
	r->k = &d->rk;
	r->cwd = d->cwd[0] ? d->cwd : NULL;
	r->off = d->off;
	r->at = d->at;
	r->chapter = chapter && chapter[0] ? chapter : NULL;
	r->ctr = &d->unit;
	r->left = XR_SPANS;	/* shortcut: 3.6's cap, counted per field group */
}

/* s[0..n) clipped to cap scalars, scanned in mode under r (a NUL-ended copy:
** rm_hit reads to the line's end). */
static void
rm_clip(struct remit *r, const char *s, size_t n, size_t cap, int mode)
{
	size_t l = cap ? clip(s, n, cap) : n;
	char *b = malloc(l + 1);

	if (!b)
		return;
	memcpy(b, s, l);
	b[l] = '\0';
	rm_scan(r, b, l, mode);
	free(b);
}

/* One block's text to the sink, in the open chapter. */
static void
refs_text(struct xr_claude *d, const char *s, size_t n, size_t cap, int mode)
{
	struct remit r;
	char a[48];

	if (!d->rk.hit || !s)
		return;
	ch_anchor(d, a);
	rm_claude(d, &r, a);
	rm_clip(&r, s, n, cap, mode);
	free(r.cov);
}

/* A notice's text: stripWrapperTags (</?[a-z][a-z_-]*>), in place. */
static size_t
strip_tags(char *s, size_t n)
{
	size_t i = 0, k = 0, j;

	while (i < n)
	{
		j = i + 1 + (i + 1 < n && s[i + 1] == '/');
		if (s[i] == '<' && j < n && A_LO((unsigned char)s[j]))
		{
			for (j++; j < n && (A_LO((unsigned char)s[j]) || s[j] == '_' ||
			                    s[j] == '-'); j++)
				;
			if (j < n && s[j] == '>')
			{
				i = j + 1;
				continue;
			}
		}
		s[k++] = s[i++];
	}
	return k;
}

static struct rcall *
rc_find(struct xr_claude *d, const char *id)
{
	int k;

	for (k = 0; k < d->ncalls; k++)
		if (!strcmp(d->calls[k].id, id))
			return &d->calls[k];
	return NULL;
}

static void
rc_drop(struct xr_claude *d, struct rcall *c)
{
	int k = (int)(c - d->calls);

	free(c->cmd);
	memmove(c, c + 1, (size_t)(d->ncalls - k - 1) * sizeof *c);
	d->ncalls--;
}

/* A call joins the refs ring (a repeated id replaces its older call; past
** XR_CALLS the oldest goes). */
static struct rcall *
rc_push(struct xr_claude *d, const char *id, int kind)
{
	char kb[XR_ID_MAX + 1];
	struct rcall *c;

	id = id_key(id, kb);
	if ((c = rc_find(d, id)))
		rc_drop(d, c);
	if (!d->calls && !(d->calls = calloc(XR_CALLS, sizeof *d->calls)))
		return NULL;
	if (d->ncalls == XR_CALLS)
		rc_drop(d, &d->calls[0]);
	c = &d->calls[d->ncalls++];
	memset(c, 0, sizeof *c);
	snprintf(c->id, sizeof c->id, "%s", id);
	c->kind = kind;
	c->off = d->off;
	c->at = d->at;
	ch_anchor(d, c->chapter);
	return c;
}

static int
str_cmp(const void *a, const void *b)
{
	return strcmp(*(char *const *)a, *(char *const *)b);
}

/* A generic card's keys: the input's, once each, at most 64, sorted
** (malloc'd). The count. */
static int
card_keys(const struct json_parser *p, int in, char **keys)
{
	int nk = 0, i, k;

	for (i = in + 1; i < p->tok[in].next && i + 1 < p->ntok && nk < 64;
	     i = p->tok[i + 1].next)
	{
		size_t l;
		char *s = j_str(p, i, &l);

		for (k = 0; s && k < nk && strcmp(keys[k], s); k++)
			;
		if (s && k == nk)
			keys[nk++] = s;
		else
			free(s);
	}
	qsort(keys, (size_t)nk, sizeof *keys, str_cmp);
	return nk;
}

/* A generic card's fields: the input's keys sorted, the first 8, each value
** (its last, as a dictionary keeps it) prefixed to 240 scalars in code.
** shortcut: a nested value is its raw JSON text, not Prosper's rendering. */
static void
refs_card(struct xr_claude *d, const struct json_parser *p, int in)
{
	char *keys[64], a[48];
	int nk, k;
	struct remit r;

	if (in < 0)
		return;
	nk = card_keys(p, in, keys);
	ch_anchor(d, a);
	rm_claude(d, &r, a);
	for (k = 0; k < nk; k++)
	{
		int v = k < 8 ? j_field(p, in, keys[k]) : -1;

		if (v >= 0 && p->tok[v].kind == JSON_STRING)
		{
			size_t l;
			char *s = j_str(p, v, &l);

			if (s)
				rm_clip(&r, s, l, XR_FIELD_CAP, XR_CODE);
			free(s);
		}
		else if (v >= 0 && !j_null(p, v))
			rm_clip(&r, p->text + p->tok[v].start,
			        (size_t)(p->tok[v].end - p->tok[v].start),
			        XR_FIELD_CAP, XR_CODE);
		free(keys[k]);
	}
	free(r.cov);
}

/* A tool_use or server_tool_use (3.6's tiers): Bash shows its command, an
** edit/read its path, a search its rows, anything else a generic card. */
static void
refs_call(struct xr_claude *d, const struct json_parser *p, int i)
{
	char *id = k_str(p, i, A_ID, NULL), *name = k_str(p, i, A_NAME, NULL);
	int in = k_obj(p, i, A_INPUT), v;
	char *s = NULL, a[48];
	size_t l = 0;
	struct rcall *c;
	struct remit r;

	if (!name)
		name = strdup("");
	if (name && !strcmp(name, "Bash") &&
	    (v = j_field(p, in, "command")) >= 0 &&
	    p->tok[v].kind == JSON_STRING && (s = j_str(p, v, &l)) &&
	    !blank(s, l))
	{
		ch_anchor(d, a);
		rm_claude(d, &r, a);
		rm_clip(&r, s, l, XR_TEXT_CAP, XR_CODE);
		free(r.cov);
		if (id && (c = rc_push(d, id, RC_BASH)))
		{
			c->cl = u8_cut(s, l > XR_BASH_MAX ? XR_BASH_MAX : l);
			if ((c->cmd = malloc(c->cl + 1)))
			{
				memcpy(c->cmd, s, c->cl);
				c->cmd[c->cl] = '\0';
			}
		}
	}
	else if (name && (!strcmp(name, "Edit") || !strcmp(name, "MultiEdit") ||
	                  !strcmp(name, "Write") || !strcmp(name, "Read")) &&
	         (v = j_field(p, in, "file_path")) >= 0 &&
	         p->tok[v].kind == JSON_STRING && (s = j_str(p, v, &l)) &&
	         !blank(s, l) &&
	         (strcmp(name, "Write") || j_field(p, in, "content") >= 0) &&
	         (strcmp(name, "Edit") || (j_field(p, in, "old_string") >= 0 &&
	                                   j_field(p, in, "new_string") >= 0)))
	{
		/* the header's path: one file ref (2.5) */
		struct xr_span sp;
		char norm[XR_KEY_MAX + 1];
		size_t nl;

		memset(&sp, 0, sizeof sp);
		ch_anchor(d, a);
		rm_claude(d, &r, a);
		r.text = s;
		sp.rule = 'F';
		sp.e = l;
		if ((nl = path_norm(s, l, r.cwd, norm, sizeof norm)) &&
		    span_key(&sp, XR_FILE, norm, nl, 0) && !rel_file(&sp))
			rm_hit(&r, &sp, NULL, NULL, NULL);
		if (id)
			(void)rc_push(d, id, RC_OTHER);
	}
	else if (name && (!strcmp(name, "Grep") || !strcmp(name, "Glob")) &&
	         (v = j_field(p, in, "pattern")) >= 0 &&
	         p->tok[v].kind == JSON_STRING &&
	         p->tok[v].end > p->tok[v].start)
	{
		if (id)
			(void)rc_push(d, id, RC_SEARCH);
	}
	else
	{
		refs_card(d, p, in);
		if (id)
			(void)rc_push(d, id, RC_CARD);
	}
	free(s);
	free(id);
	free(name);
}

/* outputText: toolUseResult's stdout (+ stderr, + [interrupted]) before the
** tool_result's own text (QA A1). malloc'd, NULL for none. */
static char *
output_text(const struct json_parser *p, int i, int extra, size_t *n)
{
	int c, v;
	char *o, *e = NULL;
	size_t ol, el = 0;

	*n = 0;
	if ((v = j_field(p, extra, "stdout")) >= 0 &&
	    p->tok[v].kind == JSON_STRING && (o = j_str(p, v, &ol)))
	{
		int intr = (v = j_field(p, extra, "interrupted")) >= 0 &&
		           p->tok[v].kind == JSON_PRIMITIVE &&
		           p->text[p->tok[v].start] == 't';
		char *no;

		if ((v = j_field(p, extra, "stderr")) >= 0 &&
		    p->tok[v].kind == JSON_STRING)
			e = j_str(p, v, &el);
		if ((no = realloc(o, ol + el + 16)))
		{
			o = no;
			if (e && el)
			{
				o[ol++] = '\n';
				memcpy(o + ol, e, el);
				ol += el;
			}
			if (intr)
			{
				memcpy(o + ol, "\n[interrupted]", 14);
				ol += 14;
			}
			o[ol] = '\0';
		}
		free(e);
		*n = ol;
		return o;
	}
	c = j_get(p, i, A_CONTENT);
	if (c >= 0 && p->tok[c].kind == JSON_STRING)
		return j_str(p, c, n);
	if (c >= 0 && p->tok[c].kind == JSON_ARRAY)
		return texts(p, c, n);
	return NULL;
}

/* A search card's rows: extra.filenames, else extra.content or the text
** split at "\n", empties dropped; the first 20, each prefixed to 240. */
static void
refs_rows(struct xr_claude *d, struct remit *r, const struct json_parser *p,
          int extra, const char *text, size_t tl)
{
	int f = j_field(p, extra, "filenames"), v, rows = 0;
	char *s = NULL;
	size_t l = 0, i, e;

	(void)d;
	if (f >= 0 && p->tok[f].kind == JSON_ARRAY)
	{
		for (v = f + 1; v < p->tok[f].next && rows < 20; v = p->tok[v].next)
			if (p->tok[v].kind == JSON_STRING && (s = j_str(p, v, &l)))
			{
				if (l)
				{
					rm_clip(r, s, l, XR_FIELD_CAP, XR_CODE);
					rows++;
				}
				free(s);
			}
		return;
	}
	if ((v = j_field(p, extra, "content")) >= 0 &&
	    p->tok[v].kind == JSON_STRING && (s = j_str(p, v, &l)))
	{
		text = s;
		tl = l;
	}
	for (i = 0; text && i < tl && rows < 20; i = e + 1)
	{
		e = line_end(text, tl, i);
		if (e > i)
		{
			rm_clip(r, text + i, e - i, XR_FIELD_CAP, XR_CODE);
			rows++;
		}
	}
	free(s);
}

/* A tool_result (3.6): paired, its call's tier decides what is read and
** what may define, in the call's chapter; an orphan is a "Tool result"
** card, mentions only. Before tool_result(): the pairing is still open. */
struct rres
{
	struct rcall c;		/* a copy; c.cmd owned */
	int paired;
};

static void
refs_pair(struct xr_claude *d, const char *id, struct rres *rr)
{
	char kb[XR_ID_MAX + 1];
	struct rcall *c;

	memset(rr, 0, sizeof *rr);
	if (!d->rk.hit || !id || !(c = rc_find(d, id_key(id, kb))))
		return;
	rr->c = *c;
	c->cmd = NULL;
	rc_drop(d, c);
	rr->paired = 1;
}

static void
refs_result(struct xr_claude *d, const struct json_parser *p, int i, int err,
            struct rres *rr)
{
	int extra = k_obj(p, 0, (const char *const[]){ "toolUseResult", 0 });
	size_t n, bl;
	char *t = output_text(p, i, extra, &n), a[48], *body = NULL;
	struct remit r, cr;
	struct dsrc ds;

	memset(&r, 0, sizeof r);
	if (!d->rk.hit)
		goto out;
	if (!rr->paired)
		ch_anchor(d, a);
	rm_claude(d, &r, rr->paired ? rr->c.chapter : a);
	if (rr->paired && rr->c.kind == RC_OTHER)
		goto out;
	if (rr->paired && rr->c.kind == RC_SEARCH)
	{
		refs_rows(d, &r, p, extra, t, t ? n : 0);
		goto out;
	}
	if (!t)
		goto out;
	bl = clip(t, n, XR_OUTPUT_CAP);
	/* Q5: D2 parses the whole result (<= XR_D2_MAX), all else the clip */
	if (bl < n && rr->paired && n <= XR_D2_MAX)
	{
		if (!(body = malloc(bl + 1)))
			goto out;
		memcpy(body, t, bl);
		body[bl] = '\0';
	}
	else
	{
		t[bl] = '\0';
		body = t;
	}
	rm_scan(&r, body, bl, XR_CODE);
	if (!rr->paired)
		goto out;
	memset(&ds, 0, sizeof ds);
	ds.out = body;
	ds.ol = bl;
	ds.failed = err;
	ds.d2ok = n <= XR_D2_MAX;
	ds.d2out = body != t ? t : NULL;
	ds.d2l = n;
	ds.cwd = r.cwd;
	memset(&cr, 0, sizeof cr);
	if (rr->c.kind == RC_BASH && rr->c.cmd)
	{
		ds.bash = 1;
		ds.cmd = rr->c.cmd;
		ds.cl = rr->c.cl;
		/* the command's code spans, for a D3 anchor's one mention */
		rm_claude(d, &cr, rr->c.chapter);
		cr.off = rr->c.off;
		cr.at = rr->c.at;
		cr.left = 0;
		cr.text = ds.cmd;
		(void)grammar(ds.cmd, ds.cl, XR_CODE, R_CODE, cr.cwd, cov_span, &cr);
	}
	defs_emit(&ds, &r, ds.bash ? &cr : NULL);
	free(cr.cov);
out:
	free(r.cov);
	if (body != t)
		free(body);
	free(t);
	free(rr->c.cmd);
}

/* ---- --chapter: one chapter's entries (3.5; #156, XR3b) --------------------- */

struct xr_ent_call
{
	char id[XR_ID_MAX + 1];
	char *name;
};

/* The chapter anchored `anchor` (ordinal ord) is the wanted one only when the
** two strings are equal, byte for byte (K14.2). 1 when it is hit now. */
static int
ent_hit(struct xr_entries *e, const char *anchor, uint64_t ord)
{
	if (!e || e->hit || strcmp(anchor, e->anchor))
		return 0;
	e->hit = 1;
	e->ord = ord;
	return 1;
}

/* One entry (3.5): s[0..n) clipped to cap scalars, escaped as --ls-json
** escapes; dropped, and truncated set, once the object would pass its cap. */
static void
ent_put(struct xr_entries *e, const char *role, const char *tool,
        const char *s, size_t n, size_t cap, int err)
{
	size_t l = s ? clip(s, n, cap) : 0, tl = tool ? strlen(tool) : 0;
	size_t qc = (l + tl) * 6 + 96, k;
	char *t, *q, *nb;

	if (e->truncated || e->failed)
		return;
	if (!(t = malloc(l + 1)) || !(q = malloc(qc)))
	{
		e->failed = 1;
		free(t);
		return;
	}
	if (l)
		memcpy(t, s, l);
	t[l] = '\0';
	k = (size_t)snprintf(q, qc, "%s{\"role\":\"%s\",\"tool\":",
	                     e->len ? "," : "", role);
	if (tool)
		k += dch_json_quote(q + k, qc - k, tool);
	else
		k += (size_t)snprintf(q + k, qc - k, "null");
	k += (size_t)snprintf(q + k, qc - k, ",\"text\":");
	k += dch_json_quote(q + k, qc - k, t);
	k += (size_t)snprintf(q + k, qc - k, ",\"error\":%s}",
	                      err ? "true" : "false");
	if (e->len + k > e->cap)
		e->truncated = 1;	/* cut here: no later entry either */
	else if (!(nb = realloc(e->buf, e->len + k + 1)))
		e->failed = 1;
	else
	{
		memcpy(nb + e->len, q, k + 1);
		e->buf = nb;
		e->len += k;
	}
	free(t);
	free(q);
}

/* An entry of the wanted chapter, on the main thread (a subagent's own
** blocks are its thread's, not the turn's entries). */
static int
ent_here(const struct xr_claude *d, const char *agent)
{
	return !agent && d->want && d->want->hit && d->turn &&
	       d->ch.total - 1 == d->want->ord;
}

/* A generic call's input as its card shows it (refs_card): "key: value"
** lines, the first 8 keys sorted, each value prefixed to 240 scalars, a
** nested value as its raw JSON text. malloc'd, NULL for none. */
static char *
card_text(const struct json_parser *p, int in, size_t *n)
{
	char *keys[64], *o = NULL;
	size_t ol = 0;
	int nk = in < 0 ? 0 : card_keys(p, in, keys), k, any = 0;
	FILE *m = nk ? open_memstream(&o, &ol) : NULL;

	*n = 0;
	for (k = 0; k < nk; k++)
	{
		int v = m && k < 8 ? j_field(p, in, keys[k]) : -1;
		size_t l = 0;
		char *s = j_str(p, v, &l);
		const char *t = s;
		int raw = !s && v >= 0 && !j_null(p, v);

		if (raw)
		{
			t = p->text + p->tok[v].start;
			l = (size_t)(p->tok[v].end - p->tok[v].start);
		}
		/* v, not t: the analyzer reads "if (t)" as p->text maybe NULL,
		** and v >= 0 is what ties m (non-NULL) to a field */
		if (v >= 0 && (s || raw))
		{
			fprintf(m, "%s%s: ", any++ ? "\n" : "", keys[k]);
			fwrite(t, 1, clip(t, l, XR_FIELD_CAP), m);
		}
		free(s);
		free(keys[k]);
	}
	if (m && fclose(m) == 0)
		*n = ol;
	else
	{
		free(o);
		o = NULL;
	}
	return o;
}

/* A call of the wanted chapter, kept so that its result names its tool. */
static void
ent_keep(struct xr_entries *e, const char *id, const char *name)
{
	struct xr_ent_call *c;
	char kb[XR_ID_MAX + 1];

	if (!id)
		return;
	if (!e->calls && !(e->calls = calloc(XR_PENDING, sizeof *e->calls)))
	{
		e->failed = 1;
		return;
	}
	if (e->ncalls == XR_PENDING)	/* the oldest goes, as pending's does */
	{
		free(e->calls[0].name);
		memmove(e->calls, e->calls + 1, (size_t)--e->ncalls *
		        sizeof *e->calls);
	}
	c = &e->calls[e->ncalls++];
	snprintf(c->id, sizeof c->id, "%s", id_key(id, kb));
	c->name = name ? strdup(name) : NULL;
}

/* The tool name of the kept call with this id, else NULL. */
static const char *
ent_tool(const struct xr_entries *e, const char *id)
{
	char kb[XR_ID_MAX + 1];
	int k;

	for (k = e->ncalls - 1; id && k >= 0; k--)
		if (!strcmp(e->calls[k].id, id_key(id, kb)))
			return e->calls[k].name;
	return NULL;
}

/* A call's tool entry: Bash its command, an edit or a read its path,
** anything else its card's fields; the call is kept so that its result
** names its tool. */
static void
ent_call(struct xr_claude *d, const struct json_parser *p, int i,
         const char *id, const char *name)
{
	struct xr_entries *e = d->want;
	int in = k_obj(p, i, A_INPUT), v = -1;
	char *s;
	size_t l = 0;

	if (name && !strcmp(name, "Bash"))
		v = j_field(p, in, "command");
	else if (name && (!strcmp(name, "Edit") || !strcmp(name, "MultiEdit") ||
	                  !strcmp(name, "Write") || !strcmp(name, "Read")))
		v = j_field(p, in, "file_path");
	if (!(s = j_str(p, v, &l)) || blank(s, l))
	{
		free(s);
		s = card_text(p, in, &l);
	}
	ent_put(e, "tool", name, s, l, XR_TEXT_CAP, 0);
	free(s);
	ent_keep(e, id, name);
}

/* A result's chapter, before tool_result() closes its pairing: its call's
** (*quiet: a search's, whose error does not show), or -2 for an orphan,
** placed where it lands. */
static long long
ent_paired(struct xr_claude *d, const char *id, int *quiet)
{
	char kb[XR_ID_MAX + 1];
	int k = id ? pend_find(d, id_key(id, kb)) : -1;

	*quiet = k >= 0 && d->pend[k].quiet;
	return k >= 0 ? d->pend[k].ord : -2;
}

/* A result's output entry (after tool_result()): its call's tool name, the
** clipped output (v2.1 3.1: toolUseResult's stdout first, 3000 scalars). */
static void
ent_result(struct xr_claude *d, const struct json_parser *p, int i,
           const char *agent, const char *id, long long ord, int err)
{
	struct xr_entries *e = d->want;
	const char *name = NULL;
	char *t;
	size_t n;

	if (ord == -2 ? !ent_here(d, agent) : !e->hit || ord != (long long)e->ord)
		return;
	if (ord != -2)
		name = ent_tool(e, id);
	t = output_text(p, i, k_obj(p, 0, (const char *const[]){
	                    "toolUseResult", 0 }), &n);
	ent_put(e, "output", name, t, n, XR_OUTPUT_CAP, err);
	free(t);
}

/* A slash command's user entry: "/name args", the args as typed (trimmed). */
static void
ent_command(struct xr_entries *e, const char *t, size_t tl)
{
	size_t nl = 0, al = 0;
	const char *name = tag(t, tl, "command-name", &nl);
	const char *args = tag(t, tl, "command-args", &al);
	char *b = malloc(nl + al + 2);

	if (!b)
	{
		e->failed = 1;
		return;
	}
	memcpy(b, name, nl);
	if (args && al)
	{
		b[nl++] = ' ';
		memcpy(b + nl, args, al);
		nl += al;
	}
	ent_put(e, "user", NULL, b, nl, XR_TEXT_CAP, 0);
	free(b);
}

/* A user text block: a slash command, else a prompt; blank is hidden. */
static void
user_text(struct xr_claude *d, const char *agent, char *s, size_t n)
{
	char title[XR_TITLE_MAX * 4 + 1], sum[XR_TITLE_MAX * 4 + 1], *u = NULL;
	size_t ul = 0, tl = n;
	const char *t, *args;
	int h;

	if (command(s, n, title, sizeof title))
	{
		block(d, agent, B_PROMPT, "command", title);
		/* its args, prefix(240), as prose (3.6) */
		t = trim(s, &tl);
		if (d->rk.hit && (args = tag(t, tl, "command-args", &ul)))
			refs_text(d, args, ul, XR_FIELD_CAP, XR_PROSE);
		if (ent_here(d, agent))
			ent_command(d->want, t, tl);
		return;
	}
	if (blank(s, n))
		return;
	h = harness(s, n, sum, sizeof sum);
	if ((d->rk.hit || d->want) && (u = malloc(n + 1)))
	{
		memcpy(u, s, n);
		ul = unwrap(u, n, XR_TEXT_CAP);
	}
	xr_title(s, unwrap(s, n, 0), title, sizeof title);	/* s is spent */
	d->harness = h ? sum : NULL;
	block(d, agent, B_PROMPT, "prompt", title);
	d->harness = NULL;
	refs_text(d, u, ul, XR_TEXT_CAP, XR_PROSE);
	if (ent_here(d, agent))
	{
		d->want->failed |= !u;
		ent_put(d->want, "user", NULL, u, ul, XR_TEXT_CAP, 0);
	}
	free(u);
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
		compaction(d, ag, n > 0);
		refs_text(d, s, n, XR_TEXT_CAP, XR_PROSE);
		if (n && ent_here(d, ag))
			ent_put(d->want, "user", NULL, s, n, XR_TEXT_CAP, 0);
		free(s);
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
			struct rres rr;
			int quiet = 0;
			long long ord = d->want ? ent_paired(d, id, &quiet) : -2;

			refs_pair(d, id, &rr);
			tool_result(d, ag, id, k_bool(p, i, A_ERR));
			if (d->rk.hit)
				refs_result(d, p, i, k_bool(p, i, A_ERR), &rr);
			if (d->want)
				ent_result(d, p, i, ag, id, ord,
				           k_bool(p, i, A_ERR) && !quiet);
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
		{
			block(d, ag, B_STEP, NULL, NULL);
			refs_text(d, s, n, XR_TEXT_CAP, XR_MD);
			if (ent_here(d, ag))
				ent_put(d->want, "assistant", NULL, s, n, XR_TEXT_CAP, 0);
		}
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
			{
				block(d, ag, B_STEP, NULL, NULL);
				refs_text(d, s, n, XR_TEXT_CAP, XR_MD);
				if (ent_here(d, ag))
					ent_put(d->want, "assistant", NULL, s, n, XR_TEXT_CAP,
					        0);
			}
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
			if (d->rk.hit)
				refs_call(d, p, i);
			if (ent_here(d, ag))
				ent_call(d, p, i, id, name);
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

/* ---- Codex: the rollout (7.4, K9) ------------------------------------------- */

/* Rollout lines {"timestamp","type","payload"} as Codex 0.151-0.167 write
** them (field names checked against real rollouts, 2026-11-11). Only
** response_item, compacted, session_meta and turn_context are read; every
** event_msg and any other type gives nothing, so a user turn Codex records
** twice (the item and its event) counts once. */
static const char *const A_PAYLOAD[] = { "payload", 0 };
static const char *const A_ROLE[] = { "role", 0 };
static const char *const A_ARGS[] = { "arguments", 0 };
static const char *const A_OUTPUT[] = { "output", 0 };
static const char *const A_ACTION[] = { "action", 0 };
static const char *const A_CALLID[] = { "call_id", 0 };

void
xr_claude_codex(struct xr_claude *d)
{
	d->codex = 1;
}

/* A user-message part Codex adds itself, never typed: its AGENTS.md
** instructions, or one tag-wrapped part (<environment_context>, an <image>
** placeholder, ...). */
static int
cx_context(const char *s, size_t n)
{
	static const char ag[] = "# AGENTS.md instructions";
	const char *t = trim(s, &n);

	if (n >= sizeof ag - 1 && !memcmp(t, ag, sizeof ag - 1))
		return 1;
	return n >= 3 && t[0] == '<' && t[n - 1] == '>' &&
	       (A_LO((unsigned char)t[1]) || t[1] == '_' || t[1] == '/');
}

/* A user message: its typed parts joined as Codex joins them (no separator)
** make the prompt (5.3's rule, as a Claude user text); its images ride with
** it. A message of Codex's own parts only is no turn. */
static void
cx_user(struct xr_claude *d, const struct json_parser *p, int pl)
{
	int c = j_get(p, pl, A_CONTENT), i, img = 0;
	char *text = NULL, *nt, *s;
	size_t tl = 0, l;

	if (c < 0 || p->tok[c].kind != JSON_ARRAY)
		return;
	for (i = c + 1; i < p->tok[c].next; i = p->tok[i].next)
	{
		if (p->tok[i].kind != JSON_OBJECT)
			continue;
		if (k_streq(p, i, A_TYPE, "input_image"))
			img = 1;
		else if (k_streq(p, i, A_TYPE, "input_text") &&
		         (s = k_str(p, i, A_TEXT, &l)))
		{
			if (!cx_context(s, l) && (nt = realloc(text, tl + l + 1)))
			{
				text = nt;
				memcpy(text + tl, s, l);
				tl += l;
				text[tl] = '\0';
			}
			free(s);
		}
	}
	if (text)
		user_text(d, NULL, text, tl);
	if (img)
		main_block(d, B_IMAGE, NULL, NULL);
	free(text);
}

/* function_call's arguments: a JSON object as a generic card (3.6), else
** the string as one field. */
static void
cx_args(struct xr_claude *d, const struct json_parser *p, int pl)
{
	struct json_parser q;
	size_t n;
	char *s = k_str(p, pl, A_ARGS, &n);

	if (!s)
		return;
	memset(&q, 0, sizeof q);
	if (tokens_max(s, n) <= XR_LINE_TOKENS && dch_json_parse(&q, s) >= 0 &&
	    q.tok[0].kind == JSON_OBJECT)
		refs_card(d, &q, 0);
	else
		refs_text(d, s, n, XR_FIELD_CAP, XR_CODE);
	dch_json_free(&q);
	free(s);
}

/* --chapter's entry for one Codex step of the wanted chapter (3.5): a call's
** tool entry (function_call its arguments' card, else their text; a custom
** tool its input; a local shell call its action's card), or an output entry
** named by its call_id's call. error is false: a rollout has no error flag
** of its own (7.4: never a guess). */
static void
cx_ent(struct xr_claude *d, const struct json_parser *p, int pl,
       const char *t)
{
	struct xr_entries *e = d->want;
	struct json_parser q;
	char *id = k_str(p, pl, A_CALLID, NULL), *name = NULL, *s = NULL, *a;
	size_t n = 0, al;
	int c;

	if (!strcmp(t, "function_call_output") ||
	    !strcmp(t, "custom_tool_call_output"))
	{
		c = j_get(p, pl, A_OUTPUT);
		s = c >= 0 && p->tok[c].kind == JSON_STRING ? j_str(p, c, &n)
		    : c >= 0 && p->tok[c].kind == JSON_ARRAY ? texts(p, c, &n)
		    : NULL;
		ent_put(e, "output", ent_tool(e, id), s, n, XR_OUTPUT_CAP, 0);
	}
	else if (!strcmp(t, "function_call") || !strcmp(t, "custom_tool_call") ||
	         !strcmp(t, "local_shell_call"))
	{
		name = k_str(p, pl, A_NAME, NULL);
		if (!strcmp(t, "function_call"))
		{
			if ((a = k_str(p, pl, A_ARGS, &al)))
			{
				memset(&q, 0, sizeof q);
				if (tokens_max(a, al) <= XR_LINE_TOKENS &&
				    dch_json_parse(&q, a) >= 0 &&
				    q.tok[0].kind == JSON_OBJECT)
					s = card_text(&q, 0, &n);
				dch_json_free(&q);
				if (!s)
				{
					s = a;
					n = al;
				}
				else
					free(a);
			}
		}
		else if (!strcmp(t, "custom_tool_call"))
			s = k_str(p, pl, A_INPUT, &n);
		else
			s = card_text(p, k_obj(p, pl, A_ACTION), &n);
		ent_put(e, "tool", name, s, n, XR_TEXT_CAP, 0);
		ent_keep(e, id, name);
	}
	free(s);
	free(name);
	free(id);
}

/* One response_item: a user or assistant message, else a step whose tool
** fields are scanned under the Claude tool-field rules (code context; a
** command-like input 6000 scalars, an output 3000, a card's fields 240). */
static void
cx_item(struct xr_claude *d, const struct json_parser *p, int pl)
{
	char *t = k_str(p, pl, A_TYPE, NULL), *r, *s;
	int c, i;
	size_t n = 0;

	if (t && !strcmp(t, "message"))
	{
		r = k_str(p, pl, A_ROLE, NULL);
		c = j_get(p, pl, A_CONTENT);
		if (r && !strcmp(r, "user"))
			cx_user(d, p, pl);
		else if (r && !strcmp(r, "assistant") && c >= 0 &&
		         p->tok[c].kind == JSON_ARRAY)
			for (i = c + 1; i < p->tok[c].next; i = p->tok[i].next)
			{
				s = p->tok[i].kind == JSON_OBJECT
				    ? k_str(p, i, A_TEXT, &n) : NULL;
				if (s && !blank(s, n))
				{
					main_block(d, B_STEP, NULL, NULL);
					refs_text(d, s, n, XR_TEXT_CAP, XR_MD);
					if (ent_here(d, NULL))
						ent_put(d->want, "assistant", NULL, s, n,
						        XR_TEXT_CAP, 0);
				}
				free(s);
			}
		/* developer and system messages: Codex's own context */
		free(r);
		free(t);
		return;
	}
	main_block(d, B_STEP, NULL, NULL);
	if (!d->rk.hit || !t)
		;
	else if (!strcmp(t, "function_call"))
		cx_args(d, p, pl);
	else if (!strcmp(t, "custom_tool_call") &&
	         (s = k_str(p, pl, A_INPUT, &n)))
	{
		refs_text(d, s, n, XR_TEXT_CAP, XR_CODE);
		free(s);
	}
	else if (!strcmp(t, "local_shell_call"))
		refs_card(d, p, k_obj(p, pl, A_ACTION));
	else if (!strcmp(t, "function_call_output") ||
	         !strcmp(t, "custom_tool_call_output"))
	{
		c = j_get(p, pl, A_OUTPUT);
		s = c >= 0 && p->tok[c].kind == JSON_STRING ? j_str(p, c, &n)
		    : c >= 0 && p->tok[c].kind == JSON_ARRAY ? texts(p, c, &n)
		    : NULL;
		refs_text(d, s, n, XR_OUTPUT_CAP, XR_CODE);
		free(s);
	}
	/* reasoning, and any type not listed: a step, no ref (7.4) */
	if (t && ent_here(d, NULL))
		cx_ent(d, p, pl, t);
	free(t);
}

static void
cx_line(struct xr_claude *d, const struct json_parser *p)
{
	int pl = k_obj(p, 0, A_PAYLOAD);
	char *t = pl >= 0 ? k_str(p, 0, A_TYPE, NULL) : NULL, *s;
	size_t n;

	if (!t)
		;
	else if (!strcmp(t, "response_item"))
		cx_item(d, p, pl);
	else if (!strcmp(t, "compacted"))
	{
		/* its summary, as a Claude compact summary's (3.6) */
		main_block(d, B_PROMPT, "compact", "Conversation compacted");
		if ((s = k_str(p, pl, A_MESSAGE, &n)))
			refs_text(d, s, n, XR_TEXT_CAP, XR_PROSE);
		/* as a Claude compact summary's user entry */
		if (s && ent_here(d, NULL))
			ent_put(d->want, "user", NULL, s, n, XR_TEXT_CAP, 0);
		free(s);
		/* its lead ends here: the prompt after it is a turn of its own */
		main_block(d, B_STEP, NULL, NULL);
	}
	else if ((!strcmp(t, "session_meta") || !strcmp(t, "turn_context")) &&
	         (s = k_str(p, pl, A_CWD, &n)))
	{
		if (s[0] == '/' && scalars(s, n) <= XR_PATH_MAX &&
		    n < sizeof d->cwd)
			memcpy(d->cwd, s, n + 1);
		free(s);
	}
	free(t);
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
	if (d->codex)
	{
		cx_line(d, &p);
		dch_json_free(&p);
		return;
	}
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
		{
			block(d, ag, B_STEP, NULL, NULL);	/* notice or card */
			/* a notice: wrapper tags out, trimmed, prefix(6000) */
			if (d->rk.hit && (s = k_str(&p, 0, A_CONTENT, &n)))
			{
				const char *t;

				n = strip_tags(s, n);
				t = trim(s, &n);
				refs_text(d, t, n, XR_TEXT_CAP, XR_PROSE);
				free(s);
			}
		}
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

static void
ch_anchors(struct xr_claude *d, uint64_t ino)
{
	size_t k;

	for (k = 0; k < d->ch.n; k++)
	{
		struct xr_chapter *x = &d->ch.v[(d->ch.start + k) % XR_CHAPTERS];

		snprintf(x->anchor, sizeof x->anchor, "f%llx:%llu",
		         (unsigned long long)ino, (unsigned long long)x->offset);
	}
}

void
xr_claude_end(struct xr_claude *d, uint64_t ino, struct xr_chapters *out)
{
	ch_anchors(d, ino);
	*out = d->ch;
	memset(&d->ch, 0, sizeof d->ch);
	d->turn = 0;
}

const struct xr_chapters *
xr_claude_chapters(struct xr_claude *d)
{
	ch_anchors(d, d->ino);
	return &d->ch;
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
		{
			d->next = start;
			return got < 0 ? -1 : 1;
		}
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
			{
				xr_claude_line(d, "", 0, start, 1);
				d->too_long++;
			}
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
			/* --ref: the budget, the output cap, light's first result */
			if (d->tick && (d->halt = d->tick(d->tctx, start - from +
			                                     (mid ? xr_lim.head : 0))))
			{
				d->next = start;
				return 0;
			}
		}
		pos += (uint64_t)got;
	}
	/* a line still open at the range's end (the head's cut, or one the
	** agent is writing) waits, as LineBuffer's pending does: dropped,
	** and the next read (--follow) starts there */
	d->next = start;
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
			if (rc == 0 && !d->halt)
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

void
xr_claude_refs(struct xr_claude *d, const struct xr_sink *k, uint64_t ino,
               int (*tick)(void *ctx, uint64_t bytes), void *tctx)
{
	d->rk = *k;
	d->ino = ino;
	d->tick = tick;
	d->tctx = tctx;
}

uint64_t
xr_claude_too_long(const struct xr_claude *d)
{
	return d->too_long;
}

int
xr_claude_tail(struct xr_claude *d, int fd, uint64_t from, uint64_t to)
{
	char *buf = malloc(XR_CHUNK), *acc = malloc(XR_LINE_MAX + 1);
	int rc = -1;

	d->halt = 0;
	d->next = from;
	if (buf && acc)
		rc = from < to ? read_range(d, fd, from, to, 0, buf, acc) : 0;
	free(buf);
	free(acc);
	return rc;
}

uint64_t
xr_claude_next(const struct xr_claude *d)
{
	return d->next;
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

static void
sh_feed(struct xr_shell *s, const struct xr_rec *x)
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
		/* --chapter: the block's command (B..C) is its tool entry */
		if (ent_hit(s->want, ch->anchor, (uint64_t)s->ord))
			ent_put(s->want, "tool", NULL, c, l, XR_TEXT_CAP, 0);
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

static void sh_refs_pre(struct xr_shell *s, const struct xr_rec *x);
static void sh_refs_post(struct xr_shell *s, const struct xr_rec *x);

/* --chapter: the wanted block's output entry, once (its C..D rows, the
** error its D gave). */
static void
sh_ent_close(struct xr_shell *s)
{
	struct xr_entries *e = s->want;
	const struct xr_chapter *c;

	if (!e || !e->hit || e->done)
		return;
	e->done = 1;
	c = ch_ord(s->out, e->ord);
	ent_put(e, "output", NULL, e->out, e->ol, XR_OUTPUT_CAP, c && c->error);
}

/* --chapter: the wanted block's output rows while it is open, joined at
** soft wraps, kept to 4 bytes a scalar of the clip (3.5); closed by the
** first record after it. */
static void
sh_ent(struct xr_shell *s, const struct xr_rec *x)
{
	struct xr_entries *e = s->want;
	size_t room, l;

	if (!e->hit || e->done)
		return;
	if (s->st != S_OUTPUT || s->ord < 0 || (uint64_t)s->ord != e->ord)
	{
		sh_ent_close(s);
		return;
	}
	if (x->kind != RING_K_OUTPUT)
		return;
	if (!e->out && !(e->out = malloc(XR_OUTPUT_CAP * 4)))
	{
		e->failed = 1;
		return;
	}
	room = XR_OUTPUT_CAP * 4 - e->ol;
	if (e->ol && room && !(x->flags & RING_F_CONT))
	{
		e->out[e->ol++] = '\n';
		room--;
	}
	l = x->len < room ? x->len : room;
	memcpy(e->out + e->ol, x->text, l);
	e->ol += l;
}

void
xr_shell_feed(struct xr_shell *s, const struct xr_rec *x)
{
	if (s->refs)
		sh_refs_pre(s, x);
	sh_feed(s, x);
	if (s->refs)
		sh_refs_post(s, x);
	if (s->want)
		sh_ent(s, x);
}

/* ---- shell: refs (7.3) ----------------------------------------------------- */

struct xr_shref
{
	struct xr_sink k;
	char *cwd;
	uint64_t unit;		/* the block: bumped at a primary A and a gap */
	/* the open logical line: rows joined through RING_F_CONT */
	char *l;
	size_t ll;
	uint64_t lseq;
	uint32_t lt;
	int lkind, open;
	/* the paired block's output (<= XR_D2_MAX) and its lines */
	char *ob;
	size_t obl;
	int over;
	size_t *opos;
	uint64_t *oseq;
	size_t on, ocap;
	char cmd[XR_CMD_MAX + 1];
	size_t cl;
	uint64_t cseq;
	char chapter[32];
	/* the newest block's first prompt row, for the screen's rows */
	char prompt[256];
	size_t pl;
	int screen;		/* past xr_shell_screen */
};

int
xr_shell_refs(struct xr_shell *s, const struct xr_sink *k, const char *cwd)
{
	struct xr_shref *r = calloc(1, sizeof *r);

	if (!r || !(r->l = malloc(XR_SH_LINE + 1)) ||
	    !(r->ob = malloc(XR_D2_MAX + 1)) ||
	    !(r->cwd = strdup(cwd && cwd[0] == '/' ? cwd : "")))
	{
		if (r)
		{
			free(r->l);
			free(r->ob);
		}
		free(r);
		return -1;
	}
	r->k = *k;
	s->refs = r;
	return 0;
}

static void
sh_block_reset(struct xr_shref *r)
{
	r->obl = r->on = 0;
	r->over = 0;
	r->cl = 0;
	r->chapter[0] = '\0';
}

/* The open line out: its mentions in code, and a paired block's output
** row into the block buffer. */
static void
sh_flush(struct xr_shell *s)
{
	struct xr_shref *r = s->refs;
	struct remit m;
	int inblock = s->st == S_OUTPUT && s->ord >= 0;

	if (!r->open)
		return;
	r->open = 0;
	r->l[r->ll] = '\0';
	memset(&m, 0, sizeof m);
	m.k = &r->k;
	m.cwd = r->cwd[0] ? r->cwd : NULL;
	m.off = m.row = r->lseq;
	m.at = r->lt;
	m.unit = r->unit;
	m.left = XR_SPANS;	/* 3.8: per logical line */
	/* shortcut: a mention's chapter is its block's for output rows of a
	** paired block, else null */
	m.chapter = inblock && r->lkind == RING_K_OUTPUT && r->chapter[0]
	            ? r->chapter : NULL;
	rm_scan(&m, r->l, r->ll, XR_CODE);
	free(m.cov);
	if (!inblock || r->lkind != RING_K_OUTPUT || r->over)
		return;
	if (r->obl + r->ll + 1 > XR_D2_MAX)
	{
		r->over = 1;	/* past 256 KB: what is kept stands, D2 off */
		return;
	}
	if (r->on == r->ocap)
	{
		size_t nc = r->ocap ? r->ocap * 2 : 64;
		size_t *np = realloc(r->opos, nc * sizeof *np);
		uint64_t *ns;

		if (np)
			r->opos = np;
		if (!np || !(ns = realloc(r->oseq, nc * sizeof *ns)))
		{
			r->over = 1;
			return;
		}
		r->oseq = ns;
		r->ocap = nc;
	}
	r->opos[r->on] = r->obl;
	r->oseq[r->on++] = r->lseq;
	memcpy(r->ob + r->obl, r->l, r->ll);
	r->obl += r->ll;
	r->ob[r->obl++] = '\n';
}

/* A row record joins the open line (RING_F_CONT) or starts one. */
static void
sh_row(struct xr_shell *s, const struct xr_rec *x)
{
	struct xr_shref *r = s->refs;
	size_t l = x->len;

	if (!(x->flags & RING_F_CONT) || !r->open || r->ll + l > XR_SH_LINE)
	{
		sh_flush(s);	/* shortcut: a row past the 64 KiB line starts the
				** next, it is not split */
		r->open = 1;
		r->ll = 0;
		r->lseq = x->seq;
		r->lt = x->t;
		r->lkind = x->kind;
	}
	if (l > XR_SH_LINE - r->ll)
		l = u8_cut(x->text, XR_SH_LINE - r->ll);
	memcpy(r->l + r->ll, x->text, l);
	r->ll += l;
}

/* D on a paired block: its definitions (v2.1 4.2-4.3 over 7.3's block). */
static void
sh_defs(struct xr_shell *s, const struct xr_rec *x)
{
	struct xr_shref *r = s->refs;
	struct dsrc ds;
	struct remit o, c;
	int ex = (x->flags & RING_F_EXIT) && x->len > 2;

	if (!r->cl || blank(r->cmd, r->cl))
		return;
	r->ob[r->obl] = '\0';
	r->cmd[r->cl] = '\0';
	memset(&ds, 0, sizeof ds);
	ds.out = r->ob;
	ds.ol = r->obl;
	ds.cmd = r->cmd;
	ds.cl = r->cl;
	ds.bash = 1;
	ds.failed = ex && mark_exit(x) != 0;
	ds.noexit = !(x->flags & RING_F_EXIT);
	ds.d2ok = !r->over;
	ds.cwd = r->cwd[0] ? r->cwd : NULL;
	memset(&o, 0, sizeof o);
	o.k = &r->k;
	o.cwd = ds.cwd;
	o.at = s->at;
	o.chapter = r->chapter[0] ? r->chapter : NULL;
	o.unit = r->unit;
	o.left = XR_SPANS;
	o.lpos = r->opos;
	o.lseq = r->oseq;
	o.nl = r->on;
	c = o;
	c.nl = 0;
	c.off = c.row = r->cseq;
	/* the code spans the rows already counted, for the anchors' one mention */
	o.text = ds.out;
	(void)grammar(ds.out, ds.ol, XR_CODE, R_CODE, ds.cwd, cov_span, &o);
	c.text = ds.cmd;
	(void)grammar(ds.cmd, ds.cl, XR_CODE, R_CODE, ds.cwd, cov_span, &c);
	defs_emit(&ds, &o, &c);
	free(o.cov);
	free(c.cov);
}

/* Before the state machine: rows to lines, marks and gaps flush; after it
** (post), the block's bookkeeping. */
static void
sh_refs_pre(struct xr_shell *s, const struct xr_rec *x)
{
	struct xr_shref *r = s->refs;

	/* shortcut: a prompt past 255 bytes is not kept, so the screen's rows
	** after it stay in one unit (a count that can only be lower) */
	if (x->kind == RING_K_PROMPT && s->st == S_PROMPT && !s->havep)
	{
		if ((r->pl = x->len < sizeof r->prompt ? x->len : 0))
			memcpy(r->prompt, x->text, r->pl);
	}
	/* a screen row has no marks: one that starts with the prompt is the
	** next block's, as its A would make it */
	if (r->screen && r->pl && !(x->flags & RING_F_CONT) && x->len >= r->pl &&
	    !memcmp(x->text, r->prompt, r->pl) &&
	    (x->len == r->pl || x->text[r->pl] == ' '))
	{
		sh_flush(s);
		r->unit++;
	}
	if (x->kind == RING_K_PROMPT || x->kind == RING_K_INPUT ||
	    x->kind == RING_K_OUTPUT)
	{
		sh_row(s, x);
		return;
	}
	sh_flush(s);
	if (x->kind == RING_K_GAP)
	{
		r->unit++;
		sh_block_reset(r);
	}
	else if (x->kind == RING_K_MARK && x->len == 1 && x->text[0] == 'A' &&
	         s->st != S_PROMPT)
	{
		r->unit++;	/* a new primary prompt, not a redraw */
		sh_block_reset(r);
	}
	else if (x->kind == RING_K_MARK && x->len && x->text[0] == 'D' &&
	         (x->len == 1 || x->text[1] == ';') && s->st == S_OUTPUT &&
	         s->ord >= 0)
		sh_defs(s, x);
}

static void
sh_refs_post(struct xr_shell *s, const struct xr_rec *x)
{
	struct xr_shref *r = s->refs;

	if (x->kind != RING_K_MARK || !x->len)
		return;
	if (x->text[0] == 'C' && s->st == S_OUTPUT && s->ord >= 0)
	{
		const char *c = x->len > 2 ? x->text + 2 : s->cmd;
		size_t l = x->len > 2 ? x->len - 2 : s->cl;

		if (x->len > 2 && blank(c, l))
		{
			c = s->cmd;
			l = s->cl;
		}
		if (l > XR_CMD_MAX)
			l = u8_cut(c, XR_CMD_MAX);
		memcpy(r->cmd, c, l);
		r->cl = l;
		r->cseq = x->seq;
		r->obl = r->on = 0;
		r->over = 0;
		snprintf(r->chapter, sizeof r->chapter, "s%llu",
		         (unsigned long long)s->aseq);
	}
	else if (s->st == S_NONE)
		sh_block_reset(r);	/* an orphan, a forged C, a D: nothing held */
}

void
xr_shell_screen(struct xr_shell *s)
{
	if (s->refs)
	{
		sh_flush(s);	/* the ring's last line, in its own block */
		s->refs->screen = 1;
	}
	s->st = S_NONE;
	s->ord = -1;
}

void
xr_shell_end(struct xr_shell *s)
{
	struct xr_shref *r = s->refs;

	sh_ent_close(s);	/* a block still open at the read's end */
	if (!r)
		return;
	sh_flush(s);
	free(r->l);
	free(r->ob);
	free(r->opos);
	free(r->oseq);
	free(r->cwd);
	free(r);
	s->refs = NULL;
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

/* #192: additive, so a chapter that is not auto prints neither key. */
static void
put_auto(FILE *m, const struct xr_chapter *c)
{
	if (!c->harness)
		return;
	fputs(",\"auto\":true", m);
	if (c->summary[0])
	{
		fputs(",\"summary\":", m);
		xr_put_str(m, c->summary);
	}
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
	fprintf(m, ",\"kind\":\"%s\",\"partial\":%s,\"error\":%s",
	        c->kind, c->partial ? "true" : "false",
	        c->error ? "true" : "false");
	put_auto(m, c);
	fputc('}', m);
}

static const struct xr_index *ref_sort_x;

static int
ref_cmp(const void *pa, const void *pb)
{
	const struct xr_ent *a = &ref_sort_x->v[*(const size_t *)pa];
	const struct xr_ent *b = &ref_sort_x->v[*(const size_t *)pb];
	uint64_t ca = ix_count(ref_sort_x, a), cb = ix_count(ref_sort_x, b);

	if (ca != cb)
		return ca > cb ? -1 : 1;
	return a->first < b->first ? -1 : a->first > b->first;
}

/* One ref (3.2): count descending, then first appearance. */
/* count 0 with no definitions: the removal line of a follow (3.3) */
static void
put_ref_n(FILE *m, const struct xr_ent *e, uint64_t c, const char *project,
          int comma)
{
	fputs(comma ? ",{\"key\":" : "{\"key\":", m);
	xr_put_str(m, e->key);
	fprintf(m, ",\"shape\":\"%s\",\"text\":", xr_shape_name[e->shape]);
	xr_put_str(m, strchr(e->key, ':') + 1);
	if (e->shape == XR_COMMIT)
	{
		fputs(",\"full\":", m);
		xr_put_str(m, e->full[0] ? e->full : strchr(e->key, ':') + 1);
	}
	fputs(",\"project\":", m);
	xr_put_str(m, project);
	fprintf(m, ",\"count\":%llu,\"defined\":%s,\"definitions\":%d,"
	        "\"title\":", (unsigned long long)c, e->ndefs ? "true" : "false",
	        e->ndefs);
	put_or_null(m, e->title);
	fprintf(m, ",\"underline\":%s}",
	        e->ndefs || c >= 2 ? "true" : "false");
}

static void
put_ref(FILE *m, const struct xr_index *x, const struct xr_ent *e,
        const char *project, int comma)
{
	put_ref_n(m, e, x ? ix_count(x, e) : 0, project, comma);
}

static int sent_cut(struct xr_sent *s, const size_t *ix, size_t n);

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
	const char *proj = a->project ? a->project : "";
	size_t nr = a->refs ? a->refs->n : 0, keep, *ord = NULL, *rat = NULL;
	uint64_t dropped = a->refs ? a->refs->dropped : 0;

	if (!(rat = calloc(nr + 1, sizeof *rat)) ||
	    (nr && !(ord = malloc(nr * sizeof *ord))))
	{
		free(rat);
		free(ord);
		free(at);
		return -1;
	}
	for (k = 0; k < nr; k++)
		ord[k] = k;
	if (nr)
	{
		ref_sort_x = a->refs;
		qsort(ord, nr, sizeof *ord, ref_cmp);
	}
	/* each ref's size, as chapters' below */
	if (nr && !(m = open_memstream(&body, &bl)))
		goto fail;
	for (k = 0; k < nr && fseek(m, 0, SEEK_SET) == 0; k++)
	{
		put_ref(m, a->refs, &a->refs->v[ord[k]], proj, k > 0);
		rat[k + 1] = rat[k] + (size_t)ftell(m);
	}
	if (nr && (fclose(m) != 0 || k < nr))
		goto fail;
	free(body);
	body = NULL;
	if (!at || !(m = open_memstream(&body, &bl)))
	{
fail:
		free(body);
		free(ord);
		free(rat);
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
		free(ord);
		free(rat);
		free(at);
		return -1;
	}
	free(body);
	body = NULL;
	bl = at[n];
	/* 4 MiB: refs go first, from the end (each a dropped key), then the
	** oldest chapters */
	slack = 4096 + 6 * (strlen(a->session) + strlen(a->session_id) +
	                    (a->project ? strlen(a->project) : 0) +
	                    (a->project_label ? strlen(a->project_label) : 0));
	for (keep = nr; keep && bl + rat[keep] + slack > xr_lim.out; keep--)
		;
	while (cut < n && bl - at[cut] + rat[keep] + slack > xr_lim.out)
		cut++;
	if (cut || keep < nr)
	{
		complete = 0;
		why = "output";
		dropped += nr - keep;
	}
	if (a->sent && sent_cut(a->sent, ord + keep, nr - keep) != 0)
		goto fail;
	if (!(m = open_memstream(&head, &hl)))
	{
		body = NULL;
		goto fail;
	}
	fputs(a->snapshot ? "{\"schema\":1,\"snapshot\":{\"schema\":1,\"session\":"
	      : "{\"schema\":1,\"session\":", m);
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
	fprintf(m, ",\"other_projects\":%lld,\"chapters\":[", a->other_projects);
	if (fclose(m) != 0)
		rc = -1;
	if (rc == 0)
	{
		fputs(head, f);
		for (k = cut; k < n; k++)	/* the first kept one: no comma */
			put_chapter(f, xr_chapter_at(a->ch, k), first + k + 1,
			            k > cut);
		fprintf(f, "],\"chapters_evicted\":%s,\"refs\":[",
		        a->chapters_evicted ? "true" : "false");
		for (k = 0; k < keep; k++)
			put_ref(f, a->refs, &a->refs->v[ord[k]], proj, k > 0);
		fprintf(f, "],\"dropped_keys\":%llu}%s\n",
		        (unsigned long long)dropped, a->snapshot ? "}" : "");
		if (fflush(f) != 0 || ferror(f))
			rc = -1;
	}
	free(head);
	free(at);
	free(ord);
	free(rat);
	return rc;
}

/* ---- --follow's deltas (3.3; #136, XR2b) ------------------------------------ */

/* What a follower last wrote, per ref key and per chapter anchor: the hash of
** the object's bytes. A delta is the diff of the answer now against it, so
** the deltas applied to the snapshot are the --json of the same moment. */
struct sref
{
	uint64_t kh, vh;
	char *key;
	int shape;
	int cut;		/* left out by a snapshot's cap: never sent */
	size_t ix;		/* the entry, in the answer it was built from */
};
struct sch
{
	uint64_t kh, vh, offset;
	char anchor[40];
	size_t ix;
};
struct stab
{
	struct sref *r;
	struct sch *c;
	size_t nr, nc;
	int own;		/* keys strdup'ed (a table kept past its answer) */
};
struct xr_sent
{
	struct stab sent, pend;
	int have;		/* pend holds the last xr_delta's diff */
	size_t *cut, ncut;	/* the last xr_print's left-out refs (entries) */
};

static int
sent_cut(struct xr_sent *s, const size_t *ix, size_t n)
{
	free(s->cut);
	s->cut = NULL;
	s->ncut = 0;
	if (n && !(s->cut = malloc(n * sizeof *s->cut)))
		return -1;
	if (n)
		memcpy(s->cut, ix, n * sizeof *s->cut);
	s->ncut = n;
	return 0;
}

static void
stab_free(struct stab *t)
{
	size_t k;

	for (k = 0; t->own && k < t->nr; k++)
		free(t->r[k].key);
	free(t->r);
	free(t->c);
	memset(t, 0, sizeof *t);
}

struct xr_sent *
xr_sent_new(void)
{
	return calloc(1, sizeof(struct xr_sent));
}

void
xr_sent_free(struct xr_sent *s)
{
	if (!s)
		return;
	stab_free(&s->sent);
	stab_free(&s->pend);
	free(s->cut);
	free(s);
}

static int
sref_cmp(const void *pa, const void *pb)
{
	const struct sref *a = pa, *b = pb;

	if (a->kh != b->kh)
		return a->kh < b->kh ? -1 : 1;
	return strcmp(a->key, b->key);
}

static int
sch_cmp(const void *pa, const void *pb)
{
	const struct sch *a = pa, *b = pb;

	if (a->kh != b->kh)
		return a->kh < b->kh ? -1 : 1;
	return strcmp(a->anchor, b->anchor);
}

/* The answer's refs and chapters as a table, keys borrowed from a. */
static int
stab_build(const struct xr_answer *a, struct stab *t)
{
	size_t nr = a->refs ? a->refs->n : 0, nc = a->ch ? a->ch->n : 0, k;
	const char *proj = a->project ? a->project : "";
	char *b = NULL;
	size_t bl = 0;
	FILE *m;

	memset(t, 0, sizeof *t);
	if (!(t->r = calloc(nr + 1, sizeof *t->r)) ||
	    !(t->c = calloc(nc + 1, sizeof *t->c)) ||
	    !(m = open_memstream(&b, &bl)))
	{
		stab_free(t);
		return -1;
	}
	for (k = 0; k < nr && fseek(m, 0, SEEK_SET) == 0; k++)
	{
		const struct xr_ent *e = &a->refs->v[k];
		struct sref *r = &t->r[k];

		put_ref(m, a->refs, e, proj, 0);
		if (fflush(m) != 0)
			break;
		r->kh = fnv(e->key, strlen(e->key));
		r->vh = fnv(b, (size_t)ftell(m));
		r->key = e->key;
		r->shape = e->shape;
		r->ix = k;
	}
	t->nr = k;
	for (k = 0; t->nr == nr && k < nc && fseek(m, 0, SEEK_SET) == 0; k++)
	{
		const struct xr_chapter *x = xr_chapter_at(a->ch, k);
		struct sch *c = &t->c[k];

		put_chapter(m, x, 0, 0);	/* i is display only: not compared */
		if (fflush(m) != 0)
			break;
		snprintf(c->anchor, sizeof c->anchor, "%s", x->anchor);
		c->kh = fnv(c->anchor, strlen(c->anchor));
		c->vh = fnv(b, (size_t)ftell(m));
		c->offset = x->offset;
		c->ix = k;
	}
	t->nc = k;
	if (fclose(m) != 0 || t->nr != nr || t->nc != nc)
	{
		free(b);
		stab_free(t);
		return -1;
	}
	free(b);
	qsort(t->r, t->nr, sizeof *t->r, sref_cmp);
	qsort(t->c, t->nc, sizeof *t->c, sch_cmp);
	return 0;
}

/* pend becomes sent, its keys copied (the answer goes before the next). */
static int
stab_keep(struct xr_sent *s)
{
	size_t k;

	for (k = 0; k < s->pend.nr; k++)
		if (!(s->pend.r[k].key = strdup(s->pend.r[k].key)))
		{
			s->pend.nr = k;	/* the copies so far are freed */
			s->pend.own = 1;
			stab_free(&s->pend);
			stab_free(&s->sent);
			return -1;
		}
	s->pend.own = 1;
	stab_free(&s->sent);
	s->sent = s->pend;
	memset(&s->pend, 0, sizeof s->pend);
	s->have = 0;
	return 0;
}

int
xr_sent_take(struct xr_sent *s, const struct xr_answer *a)
{
	unsigned char *m;
	size_t k;

	stab_free(&s->pend);
	if (stab_build(a, &s->pend) != 0 ||
	    !(m = calloc(s->pend.nr + 1, 1)))
		return -1;
	/* the refs a's snapshot left out (LOW-9): a later change to one is
	** no line, as --json does not show it */
	for (k = 0; k < s->ncut; k++)
		if (s->cut[k] < s->pend.nr)
			m[s->cut[k]] = 1;
	for (k = 0; k < s->pend.nr; k++)
		s->pend.r[k].cut = m[s->pend.r[k].ix];
	free(m);
	(void)sent_cut(s, NULL, 0);
	return stab_keep(s);
}

int
xr_delta(struct xr_sent *s, const struct xr_answer *a, uint64_t head,
         size_t *nref, int *lost)
{
	size_t k;

	*nref = 0;
	*lost = 0;
	stab_free(&s->pend);
	s->have = 0;
	if (stab_build(a, &s->pend) != 0)
		return -1;
	s->have = 1;
	for (k = 0; k < s->pend.nr; k++)
	{
		const struct sref *o = bsearch(&s->pend.r[k], s->sent.r,
		                               s->sent.nr, sizeof *o, sref_cmp);

		/* left out still: it stays out (LOW-9) */
		if ((s->pend.r[k].cut = o && o->cut))
			continue;
		*nref += !o || o->vh != s->pend.r[k].vh;
	}
	for (k = 0; k < s->sent.nr; k++)
		*nref += !s->sent.r[k].cut &&
		         !bsearch(&s->sent.r[k], s->pend.r, s->pend.nr,
		                  sizeof *s->pend.r, sref_cmp);
	/* a chapter gone with its prompt record still in the ring: no line
	** removes it, so the caller resets (an evicted one the client drops at
	** the evict line) */
	for (k = 0; k < s->sent.nc; k++)
		if (s->sent.c[k].offset >= head &&
		    !bsearch(&s->sent.c[k], s->pend.c, s->pend.nc,
		             sizeof *s->pend.c, sch_cmp))
			*lost = 1;
	return 0;
}

int
xr_delta_write(FILE *f, struct xr_sent *s, const struct xr_answer *a)
{
	size_t nr = a->refs ? a->refs->n : 0, nc = a->ch ? a->ch->n : 0, k;
	const char *proj = a->project ? a->project : "";
	uint64_t first = a->ch ? a->ch->total - a->ch->n : 0;
	unsigned char *chg;

	if (!s->have || s->pend.nr != nr || s->pend.nc != nc ||
	    !(chg = calloc(nr + nc + 1, 1)))
		return -1;
	for (k = 0; k < nr; k++)
	{
		const struct sref *p = &s->pend.r[k], *o = bsearch(p, s->sent.r,
		                                                    s->sent.nr,
		                                                    sizeof *o,
		                                                    sref_cmp);

		chg[p->ix] = !p->cut && (!o || o->vh != p->vh);
	}
	for (k = 0; k < nc; k++)
	{
		const struct sch *p = &s->pend.c[k], *o = bsearch(p, s->sent.c,
		                                                   s->sent.nc,
		                                                   sizeof *o,
		                                                   sch_cmp);

		chg[nr + p->ix] = !o || o->vh != p->vh;
	}
	for (k = 0; k < nc; k++)	/* oldest first, as a snapshot's */
		if (chg[nr + k])
		{
			fputs("{\"schema\":1,\"chapter\":", f);
			put_chapter(f, xr_chapter_at(a->ch, k), first + k + 1, 0);
			fputs("}\n", f);
		}
	for (k = 0; k < nr; k++)
		if (chg[k])
		{
			fputs("{\"schema\":1,\"ref\":", f);
			put_ref(f, a->refs, &a->refs->v[k], proj, 0);
			fputs("}\n", f);
		}
	for (k = 0; k < s->sent.nr; k++)
		if (!s->sent.r[k].cut &&
		    !bsearch(&s->sent.r[k], s->pend.r, s->pend.nr,
		             sizeof *s->pend.r, sref_cmp))
		{
			struct xr_ent e;

			/* gone: count 0 removes the key (3.3) */
			memset(&e, 0, sizeof e);
			e.key = s->sent.r[k].key;
			e.shape = s->sent.r[k].shape;
			fputs("{\"schema\":1,\"ref\":", f);
			put_ref_n(f, &e, 0, proj, 0);
			fputs("}\n", f);
		}
	free(chg);
	if (stab_keep(s) != 0)
		return -1;
	return ferror(f) ? -1 : 0;
}

/* ---- --chapter (3.5; #156, XR3b) ------------------------------------------- */

int
xr_anchor_ok(const char *s)
{
	size_t n = strspn(s, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
	                     "0123456789:_.-");

	return n >= 1 && n <= 128 && !s[n];
}

void
xr_entries_begin(struct xr_entries *e, const char *anchor,
                 const char *session, size_t out)
{
	/* the head's room: the session and its id escaped (6 bytes a byte at
	** worst), a 128-byte anchor, a 120-scalar title and summary, the keys */
	size_t head = 6 * (strlen(session) + 64) + 2048;

	memset(e, 0, sizeof *e);
	e->anchor = anchor;
	e->cap = out > head ? out - head : 0;
}

void
xr_entries_free(struct xr_entries *e)
{
	int k;

	for (k = 0; k < e->ncalls; k++)
		free(e->calls[k].name);
	free(e->calls);
	free(e->buf);
	free(e->out);
	e->calls = NULL;
	e->buf = e->out = NULL;
	e->ncalls = 0;
}

void
xr_claude_entries(struct xr_claude *d, struct xr_entries *e, uint64_t ino)
{
	d->want = e;
	d->ino = ino;
}

int
xr_chapter_print(FILE *f, const char *session, const char *session_id,
                 const struct xr_chapter *c, const struct xr_entries *e)
{
	fputs("{\"schema\":1,\"session\":", f);
	xr_put_str(f, session);
	fputs(",\"session_id\":", f);
	xr_put_str(f, session_id);
	fputs(",\"anchor\":", f);
	xr_put_str(f, c->anchor);
	fputs(",\"title\":", f);
	xr_put_str(f, c->title);
	fputs(",\"at\":", f);
	if (c->at >= 0)
		fprintf(f, "%lld", c->at);
	else
		fputs("null", f);
	fprintf(f, ",\"kind\":\"%s\"", c->kind);
	put_auto(f, c);
	fputs(",\"entries\":[", f);
	if (e->len)
		fwrite(e->buf, 1, e->len, f);
	fprintf(f, "],\"truncated\":%s}\n", e->truncated ? "true" : "false");
	return fflush(f) != 0 || ferror(f) ? -1 : 0;
}

/* ---- resolvers (XR6, K13; spec 3.7, 8.3) ------------------------------- */

const char *const *xr_git_probe;
unsigned long xr_res_spawned;

#define RES_OUT_MAX (64u << 10)	/* git's stdout kept; the rest drained */
#define RES_ERR_MAX 4096

/* s[0..n) into o (cap * 4 + 1 bytes): at most cap scalars, invalid UTF-8
** and NUL as U+FFFD; the JSON writer escapes the controls. */
static void
res_text(char *o, const char *s, size_t n, size_t cap)
{
	size_t i = 0, a, k = 0, w = 0;

	while (i < n && k++ < cap)
	{
		uint32_t c = u8_next((const unsigned char *)s + i, n - i, &a);

		w += u8_put(o + w, c ? c : 0xfffd);
		i += a;
	}
	o[w] = '\0';
}

static void
res_set(struct xr_resolved *r, const char *state, const char *title)
{
	r->state = state;
	snprintf(r->title, sizeof r->title, "%s", title);
}

/* "<git root>/.git" exists, walking up from cwd by its text alone: root,
** else cwd itself (v2.1 7.1's project boundary). 1 when a root was found. */
static int
res_root(const char *cwd, char *root, size_t rootsz)
{
	char p[PATH_MAX + 8];
	struct stat st;
	size_t l = strlen(cwd);

	snprintf(root, rootsz, "%s", cwd);
	if (cwd[0] != '/' || l >= PATH_MAX)
		return 0;
	memcpy(p, cwd, l + 1);
	while (l > 1 && p[l - 1] == '/')
		l--;
	for (;;)	/* p[0..l) the dir; l 0 is "/" */
	{
		memcpy(p + l, "/.git", 6);
		if (lstat(p, &st) == 0)
		{
			snprintf(root, rootsz, "%.*s", (int)(l ? l : 1), l ? p : "/");
			return 1;
		}
		if (!l)
			return 0;
		while (l && p[l - 1] != '/')
			l--;
		if (l)
			l--;
	}
}

/* p inside root (lexically): the part after it, "" for root itself. */
static const char *
res_under(const char *p, const char *root)
{
	size_t rl = strlen(root);

	if (rl == 1 && root[0] == '/')
		return p[0] == '/' ? p + 1 : NULL;
	if (strncmp(p, root, rl) || (p[rl] && p[rl] != '/'))
		return NULL;
	return p[rl] ? p + rl + 1 : p + rl;
}

/* root is "/", $HOME or an ancestor of it, by real path (HOME and the
** passwd entry's): too wide to be a project, so none (#178). So is
** ~/.config or ~/Library, or a root inside one that is no git root below
** it: their apps keep tokens under any name (QA 1.55.0 LOW-14, #184). */
static int
res_wide(const char *root, int git)
{
	const struct passwd *pw = getpwuid(getuid());
	const char *h[2] = { getenv("HOME"), pw ? pw->pw_dir : NULL };
	static const char *const app[2] = { ".config", "Library" };
	char rr[PATH_MAX], hr[PATH_MAX], d[PATH_MAX + 16];
	size_t l;
	int k, a;

	if (!realpath(root, rr))
		snprintf(rr, sizeof rr, "%s", root);
	if (!strcmp(rr, "/"))
		return 1;
	for (k = 0; k < 2; k++)
		if (h[k] && h[k][0] == '/')
		{
			const char *hb = realpath(h[k], hr) ? hr : h[k];

			if (res_under(hb, rr))
				return 1;
			for (a = 0; a < 2; a++)
			{
				l = (size_t)snprintf(d, sizeof d, "%s%s%s", hb,
				                     hb[strlen(hb) - 1] == '/' ? "" : "/",
				                     app[a]);
				if (!strncasecmp(rr, d, l) && (!rr[l] ||
				    (rr[l] == '/' && (!git || !rr[l + 1]))))
					return 1;
			}
		}
	return 0;
}

/* ---- git (8.3) ---- */

static int
res_exec(const char *p)
{
	struct stat st;

	return stat(p, &st) == 0 && S_ISREG(st.st_mode) && access(p, X_OK) == 0;
}

/* 8.3's probe, no process run. On macOS /usr/bin/git is never a pick: the
** shim opens the install dialog without a developer dir, and with one, that
** dir's own git is found first. */
const char *
xr_git_bin(char *buf, size_t bufsz)
{
#ifdef __APPLE__
	static const char *const def[] = {
		"/Library/Developer/CommandLineTools/usr/bin/git",
		"/var/db/xcode_select_link",
		"/Applications/Xcode.app/Contents/Developer/usr/bin/git",
		"/opt/homebrew/bin/git", "/usr/local/bin/git", NULL
	};
#else
	/* 13.2 item 4 */
	static const char *const def[] = {"/usr/bin/git", "/usr/local/bin/git",
	                                  NULL};
#endif
	const char *const *v = xr_git_probe ? xr_git_probe : def;
	size_t k;

	for (k = 0; v[k]; k++)
	{
		char d[PATH_MAX];
		ssize_t l;

		if (!strcmp(v[k], "/var/db/xcode_select_link"))
		{
			/* step 2: usr/bin/git under the dir that link names */
			if ((l = readlink(v[k], d, sizeof d - 1)) <= 0)
				continue;
			d[l] = '\0';
			if (d[0] != '/' || snprintf(buf, bufsz, "%s/usr/bin/git", d)
			    >= (int)bufsz || !res_exec(buf))
				continue;
			return buf;
		}
		if (res_exec(v[k]))
			return snprintf(buf, bufsz, "%s", v[k]) < (int)bufsz ? buf
			       : NULL;
	}
	return NULL;
}

/* glibc 2.34+ closes the rest in the child; it is declared without
** _GNU_SOURCE (__USE_MISC, the default), so no configure check. */
#if defined(__GLIBC__) && defined(__USE_MISC) && \
    (__GLIBC__ > 2 || (__GLIBC__ == 2 && __GLIBC_MINOR__ >= 34))
#define RES_CLOSEFROM 1
#else
#define RES_CLOSEFROM 0
#endif

#if !defined(POSIX_SPAWN_CLOEXEC_DEFAULT) && !RES_CLOSEFROM
/* Neither a spawn flag nor an action closes the rest: every fd >= 3 is
** marked close-on-exec first. The querying process is one-shot and execs
** nothing else. -1 when /proc/self/fd cannot be listed: git is not run. */
static int
res_cloexec(void)
{
	DIR *d = opendir("/proc/self/fd");
	struct dirent *e;
	int fd, f;

	if (!d)
		return -1;
	while ((e = readdir(d)))
		if ((fd = atoi(e->d_name)) >= 3 && fd != dirfd(d) &&
		    (f = fcntl(fd, F_GETFD)) >= 0)
			fcntl(fd, F_SETFD, f | FD_CLOEXEC);
	closedir(d);
	return 0;
}
#endif

struct res_run
{
	char out[RES_OUT_MAX + 1], err[RES_ERR_MAX + 1];
	size_t on, en;
	int status;
};

/* The running git's group, for the signal handler (3.7). */
static volatile sig_atomic_t res_pg;
static const int res_sigs[] = {SIGTERM, SIGINT, SIGHUP};

/* The querying process is ending: the group goes with it (3.7, #833). */
static void
res_sig(int sig)
{
	if (res_pg > 0)
		kill(-(pid_t)res_pg, SIGKILL);
	signal(sig, SIG_DFL);
	raise(sig);
}

static void
res_keep(char *b, size_t *bn, size_t cap, const char *s, ssize_t n)
{
	size_t k = (size_t)n < cap - *bn ? (size_t)n : cap - *bn;

	memcpy(b + *bn, s, k);
	*bn += k;
	b[*bn] = '\0';
}

/* Reap pid, by `until` (ms), else end its group: SIGTERM, a 1 s grace,
** SIGKILL, at most 1 s more (#833). 0 exited (*st), 1 timed out. */
static int
res_reap(pid_t pid, long long until, int *st)
{
	long long grace;
	pid_t w;

	while ((w = waitpid(pid, st, WNOHANG)) == 0 && xr_now_ms() < until)
		poll(NULL, 0, 10);
	if (w == pid)
		return 0;
	kill(-pid, SIGTERM);
	for (grace = xr_now_ms() + 1000;
	     (w = waitpid(pid, st, WNOHANG)) == 0 && xr_now_ms() < grace;)
		poll(NULL, 0, 20);
	kill(-pid, SIGKILL);	/* what ignored SIGTERM, the leader's kids */
	/* a git in uninterruptible I/O (a stalled mount) outlives SIGKILL: it is
	** left unreaped, to init when this one-shot process ends (QA 1.55.0
	** LOW-3) */
	for (grace = xr_now_ms() + 1000; w == 0 &&
	     (w = waitpid(pid, st, WNOHANG)) == 0 && xr_now_ms() < grace;)
		poll(NULL, 0, 20);
	return 1;
}

/* One git run: argv as built, env exactly envp, stdin /dev/null, its own
** group (3.7), stdout and stderr kept to their caps and drained past them
** so git never blocks on a pipe. 0 ran (r->status), 1 timed out (the group
** ended), -1 not run. */
static int
res_run(char *const argv[], char *const envp[], long long until,
        struct res_run *r)
{
	posix_spawn_file_actions_t fa;
	posix_spawnattr_t at;
	struct sigaction sa, old[3];
	struct pollfd p[2];
	sigset_t set, omask;
	int po[2] = {-1, -1}, pe[2] = {-1, -1}, k, st = 0, timed = 0, rc;
	short fl = POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGMASK |
	           POSIX_SPAWN_SETSIGDEF;
	pid_t pid;

	r->on = r->en = 0;
	r->out[0] = r->err[0] = '\0';
	r->status = -1;
#if !defined(POSIX_SPAWN_CLOEXEC_DEFAULT) && !RES_CLOSEFROM
	if (res_cloexec() != 0)
		return -1;
#endif
	if (pipe(po) != 0 || pipe(pe) != 0)
	{
		for (k = 0; k < 2; k++)
			if (po[k] >= 0)
				close(po[k]);
		return -1;
	}
	for (k = 0; k < 2; k++)
	{
		fcntl(po[k], F_SETFD, FD_CLOEXEC);
		fcntl(pe[k], F_SETFD, FD_CLOEXEC);
	}
	posix_spawn_file_actions_init(&fa);
	posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
	posix_spawn_file_actions_adddup2(&fa, po[1], 1);
	posix_spawn_file_actions_adddup2(&fa, pe[1], 2);
	posix_spawnattr_init(&at);
	/* no other fd of ours reaches git (macOS; glibc; else res_cloexec) */
#ifdef POSIX_SPAWN_CLOEXEC_DEFAULT
	fl |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#elif RES_CLOSEFROM
	posix_spawn_file_actions_addclosefrom_np(&fa, 3);
#endif
	posix_spawnattr_setflags(&at, fl);
	posix_spawnattr_setpgroup(&at, 0);
	sigemptyset(&set);
	posix_spawnattr_setsigmask(&at, &set);
	sigaddset(&set, SIGPIPE);
	for (k = 0; k < 3; k++)
		sigaddset(&set, res_sigs[k]);
	posix_spawnattr_setsigdefault(&at, &set);
	/* no window where git runs and a SIGTERM would miss its group */
	sigprocmask(SIG_BLOCK, &set, &omask);
	rc = posix_spawn(&pid, argv[0], &fa, &at, argv, envp);
	if (rc == 0)
	{
		xr_res_spawned++;
		res_pg = pid;
		memset(&sa, 0, sizeof sa);
		sa.sa_handler = res_sig;
		for (k = 0; k < 3; k++)
			if (sigaction(res_sigs[k], NULL, &old[k]) != 0)
				old[k].sa_handler = SIG_IGN;	/* left alone */
			else if (old[k].sa_handler != SIG_IGN)
				sigaction(res_sigs[k], &sa, NULL);
	}
	sigprocmask(SIG_SETMASK, &omask, NULL);
	posix_spawn_file_actions_destroy(&fa);
	posix_spawnattr_destroy(&at);
	close(po[1]);
	close(pe[1]);
	p[0].fd = po[0];
	p[1].fd = pe[0];
	p[0].events = p[1].events = POLLIN;
	while (rc == 0 && (p[0].fd >= 0 || p[1].fd >= 0))
	{
		long long left = until - xr_now_ms();
		char b[4096];
		ssize_t n;

		if (left <= 0)
		{
			timed = 1;
			break;
		}
		if (poll(p, 2, (int)left) < 0)
		{
			if (errno == EINTR)
				continue;
			break;
		}
		for (k = 0; k < 2; k++)
		{
			if (p[k].fd < 0 || !p[k].revents)
				continue;
			if ((n = read(p[k].fd, b, sizeof b)) > 0)
			{
				if (k == 0)
					res_keep(r->out, &r->on, RES_OUT_MAX, b, n);
				else
					res_keep(r->err, &r->en, RES_ERR_MAX, b, n);
			}
			else if (n == 0 || errno != EINTR)
			{
				close(p[k].fd);
				p[k].fd = -1;
			}
		}
	}
	for (k = 0; k < 2; k++)
		if (p[k].fd >= 0)
			close(p[k].fd);
	if (rc != 0)
		return -1;
	timed = res_reap(pid, timed ? 0 : until, &st) || timed;
	sigprocmask(SIG_BLOCK, &set, &omask);
	res_pg = 0;
	for (k = 0; k < 3; k++)
		if (old[k].sa_handler != SIG_IGN)
			sigaction(res_sigs[k], &old[k], NULL);
	sigprocmask(SIG_SETMASK, &omask, NULL);
	r->status = WIFEXITED(st) ? WEXITSTATUS(st) : -1;
	return timed;
}

static int
res_hex(const char *s, size_t n)
{
	size_t k;

	for (k = 0; k < n; k++)
		if (!A_HEX((unsigned char)s[k]))
			return 0;
	return 1;
}

int
xr_git_parse(const char *s, size_t n, long long now, struct xr_resolved *r)
{
	const char *f[4], *ls[64];
	size_t fl[4], ll[64], i = 0, k, nl = 0;
	char who[1200], age[32];
	long long at = 0, d;

	for (k = 0; k < 3; k++)
	{
		const char *z = memchr(s + i, '\0', n - i);

		if (!z)
			return 0;
		f[k] = s + i;
		fl[k] = (size_t)(z - f[k]);
		i += fl[k] + 1;
	}
	f[3] = s + i;
	for (fl[3] = 0; i + fl[3] < n && s[i + fl[3]] != '\n'; fl[3]++)
		;
	i += fl[3];
	if ((fl[0] != 40 && fl[0] != 64) || !res_hex(f[0], fl[0]) ||
	    !fl[2] || fl[2] > 15)
		return 0;
	for (k = 0; k < fl[2]; k++)
	{
		if (!A_DIG((unsigned char)f[2][k]))
			return 0;
		at = at * 10 + (f[2][k] - '0');
	}
	xr_title(f[3], fl[3], r->title, sizeof r->title);
	/* the picker's short age: now, 22m, 3h, 2d */
	d = now > at ? now - at : 0;
	if (d < 60)
		snprintf(age, sizeof age, "now");
	else
		snprintf(age, sizeof age, "%lld%s ago", d < 3600 ? d / 60 : d < 86400
		         ? d / 3600 : d / 86400, d < 3600 ? "m" : d < 86400 ? "h"
		         : "d");
	k = (size_t)snprintf(who, sizeof who, "%.*s \xc2\xb7 %s \xc2\xb7 %.7s",
	                     (int)(fl[1] < 800 ? fl[1] : 800), f[1], age, f[0]);
	res_text(r->subtitle, who, k < sizeof who ? k : sizeof who - 1,
	         XR_RES_LINE);
	/* the stat: its rows, then the summary; past 14, the first 13 and it */
	while (i < n)
	{
		size_t e = i;

		while (e < n && s[e] != '\n')
			e++;
		if (e > i && nl < 64)
		{
			ls[nl] = s + i;
			ll[nl++] = e - i;
		}
		i = e + 1;
	}
	for (k = 0; k < nl && k < XR_RES_LINES; k++)
	{
		size_t j = nl > XR_RES_LINES && k == XR_RES_LINES - 1 ? nl - 1 : k;

		res_text(r->line[k], ls[j], ll[j], XR_RES_LINE);
	}
	r->nlines = (int)k;
	r->kind = "commit";
	r->state = "ok";
	return 1;
}

int
xr_git_remote(const char *s, size_t n, const char *want)
{
	static const char host[] = "github.com";
	size_t wl = strlen(want), hl = sizeof host - 1, i, e, k, l;
	const char *t;

	for (i = 0; i < n; i = e + 1)
	{
		for (e = i; e < n && s[e] != '\n'; e++)
			;
		for (k = i; k < e && s[k] != ' '; k++)	/* the key, then the url */
			;
		for (k++; k + hl < e; k++)
		{
			/* "@github.com:" or "//github.com/", then exactly o/r */
			if ((s[k - 1] != '@' &&
			     (k < i + 2 || s[k - 2] != '/' || s[k - 1] != '/')) ||
			    strncasecmp(s + k, host, hl) ||
			    (s[k + hl] != ':' && s[k + hl] != '/'))
				continue;
			t = s + k + hl + 1;
			l = (size_t)(s + e - t);
			if (l < wl || strncasecmp(t, want, wl))
				continue;
			t += wl;
			l -= wl;
			if (l >= 4 && !strncasecmp(t, ".git", 4))
				t += 4, l -= 4;
			if (!l || (l == 1 && *t == '/'))
				return 1;
		}
	}
	return 0;
}

/* GitCommitResolver (8.3): rev-parse, then show, under one 3 s deadline.
** gh, a commit URL's "<owner>/<repo>" when its SHA is short: config's
** remote URLs run between, and a repo none of them names is not that
** commit's (QA 1.55.0 LOW-1). */
static int
res_commit(const char *sha, const char *cwd, int live, const char *gh,
           struct xr_resolved *r)
{
	static struct res_run run;	/* 68 KB: off the stack */
	char git[PATH_MAX], dir[PATH_MAX], rev[64], home[PATH_MAX + 8];
	char root[PATH_MAX], name[300], *av[40], *env[9];
	const char *h = getenv("HOME"), *b;
	size_t n = strlen(sha), k, e = 0, a = 0;
	long long until;
	int rc, foreign = 0;

	/* 8.3: checked again, 7-40 lowercase hex, so it is never an option */
	if (n < 7 || n > 40 || !res_hex(sha, n))
		return 0;
	r->kind = "commit";
	snprintf(r->subtitle, sizeof r->subtitle, "%.7s", sha);
	if (!live)
	{
		res_set(r, "ended", "Session ended");	/* v2.1 7.1 */
		return 1;
	}
	if (!xr_git_bin(git, sizeof git))
	{
		res_set(r, "no_git", "git is not installed");
		return 1;
	}
	if (cwd[0] != '/' || snprintf(dir, sizeof dir, "%s", cwd)
	    >= (int)sizeof dir)
	{
		res_set(r, "not_repo", "Not in a git repository");
		return 1;
	}
	snprintf(rev, sizeof rev, "%s^{commit}", sha);
	/* exactly 8.3's list; HOME as the querying process has it */
	{
		static char e0[] = "GIT_CONFIG_NOSYSTEM=1",
		            e1[] = "GIT_CONFIG_GLOBAL=/dev/null",
		            e2[] = "GIT_OPTIONAL_LOCKS=0",
		            e3[] = "GIT_TERMINAL_PROMPT=0",
		            e4[] = "GIT_NO_LAZY_FETCH=1", e5[] = "PATH=/usr/bin:/bin",
		            e6[] = "LANG=en_US.UTF-8";
		char *v[] = {e0, e1, e2, e3, e4, e5, e6};

		for (k = 0; k < 7; k++)
			env[e++] = v[k];
		if (h && snprintf(home, sizeof home, "HOME=%s", h)
		    < (int)sizeof home)
			env[e++] = home;
		env[e] = NULL;
	}
	{
		/* 8.3's argv verbatim, plus log.showSignature=false: a repo's
		** log.showSignature and gpg.program would run that program on a
		** signed commit (#159's sentinel test) */
		static char g0[] = "--no-pager", c[] = "-c",
		            g1[] = "core.fsmonitor=", g2[] = "core.hooksPath=/dev/null",
		            g3[] = "protocol.allow=never", g4[] = "diff.external=",
		            g5[] = "color.ui=never", g6[] = "log.showSignature=false",
		            g7[] = "-C";
		char *v[] = {g0, c, g1, c, g2, c, g3, c, g4, c, g5, c, g6, g7};

		av[a++] = git;
		for (k = 0; k < sizeof v / sizeof *v; k++)
			av[a++] = v[k];
		av[a++] = dir;
	}
	until = xr_now_ms() + XR_GIT_MS;
	{
		static char s0[] = "rev-parse", s1[] = "--is-inside-work-tree";

		av[a] = s0;
		av[a + 1] = s1;
		av[a + 2] = NULL;
	}
	if ((rc = res_run(av, env, until, &run)) < 0)
		return 0;
	if (rc == 0 && (run.status != 0 || strcmp(run.out, "true\n")))
	{
		/* only git's own "not a git repository" (or a cwd gone) says so;
		** any other git error gives no line (QA 1.55.0 LOW-4) */
		if (run.status != 0 && !strstr(run.err, "not a git repository") &&
		    !strstr(run.err, "cannot change to"))
			return 0;
		res_set(r, "not_repo", "Not in a git repository");
		return 1;
	}
	if (rc == 0 && gh)
	{
		static char s0[] = "config", s1[] = "--get-regexp",
		            s2[] = "^remote\\..*\\.url$";

		av[a] = s0;
		av[a + 1] = s1;
		av[a + 2] = s2;
		av[a + 3] = NULL;
		/* 1: no remote at all; a git error or the deadline: no line, never
		** not_commit or timeout. run.out (remote URLs can carry a token)
		** is only matched, never written anywhere. */
		if (res_run(av, env, until, &run) != 0 ||
		    (run.status != 0 && run.status != 1))
			return 0;
		foreign = !xr_git_remote(run.out, run.on, gh);
	}
	if (rc == 0 && !foreign)
	{
		static char s0[] = "show", s1[] = "--no-color", s2[] = "--no-ext-diff",
		            s3[] = "--no-textconv", s4[] = "--stat=80",
		            s5[] = "--stat-count=14",
		            s6[] = "--format=%H%x00%an%x00%at%x00%s",
		            s7[] = "--end-of-options";
		char *v[] = {s0, s1, s2, s3, s4, s5, s6, s7};

		for (k = 0; k < sizeof v / sizeof *v; k++)
			av[a + k] = v[k];
		av[a + k] = rev;	/* after --end-of-options: never an option */
		av[a + k + 1] = NULL;
		if ((rc = res_run(av, env, until, &run)) < 0)
			return 0;
	}
	if (rc == 1)
	{
		res_set(r, "timeout", "git timed out after 3 s");
		return 1;
	}
	if (!foreign)
	{
		if (run.status == 0 &&
		    xr_git_parse(run.out, run.on, (long long)time(NULL), r))
			return 1;
		if (strstr(run.err, " is ambiguous"))
		{
			res_set(r, "ambiguous", "Ambiguous short SHA");
			return 1;
		}
		/* git's own "unknown revision" or "expected commit type" (a tree,
		** a blob, a tag of one) only; a git that failed otherwise (a bad
		** .gitmodules, a missing blob) gives no line (LOW-4) */
		if (!strstr(run.err, "unknown revision") &&
		    !strstr(run.err, "expected commit type"))
			return 0;
	}
	(void)res_root(dir, root, sizeof root);
	b = strrchr(root, '/');
	res_text(name, b && b[1] ? b + 1 : root, strlen(b && b[1] ? b + 1 : root),
	         60);
	r->state = "not_commit";
	snprintf(r->title, sizeof r->title, "Not a commit in %s", name);
	return 1;
}

/* ---- files (v2.1 7.1, v1 7.3) ---- */

long
xr_ref_line(const char *text)
{
	const char *p = text + strcspn(text, ":#");
	long v = 0;
	int d;

	if (p[0] == ':')
		p++;
	else if (p[0] == '#' && p[1] == 'L')
		p += 2;
	else
		return 0;
	for (d = 0; d < 7 && A_DIG((unsigned char)p[d]); d++)
		v = v * 10 + (p[d] - '0');
	return v;
}

/* The deny list (#178), ASCII case-insensitive. 'p': a part of the path
** starts with it; 'd': a part (or parts, "a/b") equals it; on the name
** only, 'n': equals it, 's': ends in it, 'i': starts with it and has no
** dot. A guard against the common credential files, not a guarantee.
** res_file matches 'd' only below a git root whose own path lies in such
** a directory, and the home's own always (#184). */
static const struct { char how; const char *s; } xr_deny[] = {
	/* env files, ssh, gpg, aws, the package managers' tokens (.env*) */
	{ 'p', ".env" }, { 'p', ".ssh" }, { 'p', ".gnupg" }, { 'p', ".aws" },
	{ 'p', ".netrc" }, { 'p', ".npmrc" }, { 'p', ".pypirc" },
	{ 'p', ".git-credentials" },
	/* git internals: .git/config can hold https://user:token@ remotes */
	{ 'd', ".git" },
	/* cloud, container, agent and password stores, whole directories */
	{ 'd', ".kube" }, { 'd', ".docker" }, { 'd', ".azure" }, { 'd', ".oci" },
	{ 'd', ".claude" }, { 'd', ".codex" }, { 'd', ".config/gh" },
	{ 'd', ".config/gcloud" }, { 'd', ".gcloud" },
	{ 'd', ".password-store" },
	/* QA 1.55.0 LOW-14: maven, hub, 1Password, macOS shell sessions */
	{ 'd', ".m2" }, { 'd', ".config/hub" }, { 'd', ".config/op" },
	{ 'd', ".bash_sessions" },
	/* single token and password files */
	{ 'n', ".claude.json" }, { 'n', ".pgpass" }, { 'n', ".my.cnf" },
	{ 'n', ".vault-token" }, { 'n', ".htpasswd" }, { 'n', ".s3cfg" },
	{ 'n', "credentials" }, { 'n', "credentials.json" },
	{ 'n', "credentials.toml" }, { 'n', "credentials.db" },
	{ 'n', "application_default_credentials.json" },
	{ 'n', "secrets.yaml" }, { 'n', "secrets.yml" }, { 'n', "secrets.json" },
	/* LOW-14: terraform, yarn, gradle, rclone, boto, docker, X11 */
	{ 'n', "credentials.tfrc.json" }, { 'n', ".yarnrc.yml" },
	{ 'n', "gradle.properties" }, { 'n', "rclone.conf" }, { 'n', ".boto" },
	{ 'n', ".dockercfg" }, { 'n', ".Xauthority" },
	/* keys, certificates, keystores, keychains, terraform state */
	{ 's', ".pem" }, { 's', ".key" }, { 's', ".p12" }, { 's', ".pfx" },
	{ 's', ".p8" }, { 's', ".ppk" }, { 's', ".jks" }, { 's', ".keystore" },
	{ 's', ".keychain" }, { 's', ".keychain-db" }, { 's', ".tfstate" },
	{ 's', ".tfstate.backup" }, { 's', ".keyring" },
	/* shell and REPL history: .zsh_history, .psql_history, fish_history */
	{ 's', "history" },
	/* ssh keys: id_rsa, id_ed25519, never id_rsa.pub */
	{ 'i', "id_" },
};

/* path on the deny list, its 'd' entries only on the parts from dirs on
** (a pointer into path; path itself for the whole of it) */
static int
xr_secret_at(const char *path, const char *dirs)
{
	const char *c = path, *e, *s;
	size_t l, k, n;

	for (; *c; c = *e ? e + 1 : e)
	{
		e = c + strcspn(c, "/");
		l = (size_t)(e - c);
		for (k = 0; k < sizeof xr_deny / sizeof *xr_deny; k++)
		{
			s = xr_deny[k].s;
			n = strlen(s);
			if (xr_deny[k].how == 'p' ? l >= n && !strncasecmp(c, s, n)
			    : xr_deny[k].how == 'd' ? c >= dirs &&
			      !strncasecmp(c, s, n) && (!c[n] || c[n] == '/')
			    : *e ? 0	/* the rest: the name only */
			    : xr_deny[k].how == 'n' ? l == n && !strncasecmp(c, s, n)
			    : xr_deny[k].how == 's' ? l >= n && !strncasecmp(e - n, s, n)
			    : l > n && !strncasecmp(c, s, n) && !memchr(c, '.', l))
				return 1;
		}
	}
	return 0;
}

int
xr_secret_name(const char *path)
{
	return xr_secret_at(path, path);
}

/* a 'd' entry starts at c, or with every at any part of it */
static int
xr_deny_dir(const char *c, int every)
{
	size_t k, n;

	for (; c; c = every && (c = strchr(c, '/')) ? c + 1 : NULL)
		for (k = 0; k < sizeof xr_deny / sizeof *xr_deny; k++)
		{
			n = strlen(xr_deny[k].s);
			if (xr_deny[k].how == 'd' &&
			    !strncasecmp(c, xr_deny[k].s, n) && (!c[n] || c[n] == '/'))
				return 1;
		}
	return 0;
}

/* path in a denied directory of the home (~/.claude, ~/.config/gh, ...),
** whatever the project root: HOME and the passwd entry's home, each as
** given and as its real path, without regard to case (#184) */
static int
res_home_deny(const char *path)
{
	const struct passwd *pw = getpwuid(getuid());
	const char *h[2] = { getenv("HOME"), pw ? pw->pw_dir : NULL }, *c;
	char hr[PATH_MAX];
	size_t l;
	int k;

	for (k = 0; k < 4; k++)
	{
		c = h[k / 2];
		if (!c || c[0] != '/' || ((k & 1) && !(c = realpath(c, hr))))
			continue;
		for (l = strlen(c); l && c[l - 1] == '/'; l--)
			;
		if (!strncasecmp(path, c, l) && path[l] == '/' &&
		    xr_deny_dir(path + l + 1, 0))
			return 1;
	}
	return 0;
}

void
xr_file_lines(const char *s, size_t n, long line, struct xr_resolved *r)
{
	size_t i, total = 0, start = 0, ln = 0;

	for (i = 0; i < n; i++)
		total += s[i] == '\n';
	total += n && s[n - 1] != '\n';
	/* line N seventh, the window kept whole at either end */
	if (line > 7)
		start = (size_t)line - 7;
	if (total > XR_RES_LINES && start > total - XR_RES_LINES)
		start = total - XR_RES_LINES;
	if (total <= XR_RES_LINES)
		start = 0;
	r->nlines = 0;
	r->first = (long)start + 1;
	for (i = 0; i < n && r->nlines < XR_RES_LINES; ln++)
	{
		size_t e = i;

		while (e < n && s[e] != '\n')
			e++;
		if (ln >= start)
			res_text(r->line[r->nlines++], s + i,
			         e - i - (e > i && s[e - 1] == '\r'), XR_RES_LINE);
		i = e + 1;
	}
}

/* FileResolver: the key's path, inside the project (the cwd's git root,
** else the cwd; none when that is /, $HOME or above, or ~/.config or
** ~/Library) by its text and again by realpath; never a denied name, on
** either whole path, nor a denied dir of the home; lstat a regular file
** with one link, at most 2 MB, opened through the seam. Outside the
** project nothing is touched, so no TCC prompt (QA D), unless a symlinked
** dir inside points out: realpath follows it, then refuses. A git root
** whose typed path lies in a denied dir (a worktree in .claude/worktrees)
** has the denied dirs matched below it only (#184). */
static int
res_file(const char *key, const char *text, const char *cwd,
         struct xr_resolved *r)
{
	char p[PATH_MAX], root[PATH_MAX], rp[PATH_MAX], rr[PATH_MAX];
	char shown[PATH_MAX + 32], *buf;
	const char *h = getenv("HOME"), *rel, *base;
	long line = xr_ref_line(text);
	struct stat st;
	ssize_t got;
	size_t n = 0;
	int fd, git, below;

	if (cwd[0] != '/' || strlen(cwd) >= PATH_MAX)
		return 0;
	if (key[0] == '~')
	{
		if (!h || h[0] != '/' || snprintf(p, sizeof p, "%s%s", h, key + 1)
		    >= (int)sizeof p)
			return 0;
	}
	else if (key[0] != '/' || snprintf(p, sizeof p, "%s", key)
	         >= (int)sizeof p)
		return 0;
	r->kind = "file";
	git = res_root(cwd, root, sizeof root);
	below = git && xr_deny_dir(root, 1);
	/* shown: relative to the cwd, else the root, else whole */
	rel = res_under(p, cwd);
	snprintf(shown, sizeof shown, "%s", rel && *rel ? rel
	         : (rel = res_under(p, root)) && *rel ? rel : p);
	res_text(r->subtitle, shown, strlen(shown), XR_RES_LINE);
	if (res_wide(root, git) || !res_under(p, root))
	{
		res_set(r, "outside_project", "Outside this session's project");
		return 1;
	}
	if (xr_secret_at(p, below ? res_under(p, root) : p) || res_home_deny(p))
	{
		res_set(r, "secret_name", "Not previewed: may hold secrets");
		return 1;
	}
	if (lstat(p, &st) != 0 || !S_ISREG(st.st_mode))
		return 0;	/* gone, a symlink, a FIFO, a dir: no block */
	if (!realpath(p, rp) || !realpath(root, rr))
		return 0;
	/* a hard link's other names can lie outside: the boundary's answer */
	if (!res_under(rp, rr) || st.st_nlink > 1)
	{
		res_set(r, "outside_project", "Outside this session's project");
		return 1;
	}
	if (xr_secret_at(rp, below ? res_under(rp, rr) : rp) ||
	    res_home_deny(rp))
	{
		res_set(r, "secret_name", "Not previewed: may hold secrets");
		return 1;
	}
	if ((uint64_t)st.st_size > XR_RES_FILE)
	{
		res_set(r, "too_large", "Not previewed: over 2 MB");
		return 1;
	}
	/* the real path, and only the inode lstat saw (the seam checks) */
	if ((fd = xr_seam_open(rp, &st)) < 0)
		return 0;
	if (!(buf = malloc(XR_RES_FILE + 1)))
	{
		close(fd);
		return 0;
	}
	while (n <= XR_RES_FILE &&
	       ((got = read(fd, buf + n, XR_RES_FILE + 1 - n)) > 0 ||
	        (got < 0 && errno == EINTR)))
		n += got > 0 ? (size_t)got : 0;
	close(fd);
	if (n > XR_RES_FILE)
		res_set(r, "too_large", "Not previewed: over 2 MB");
	else
	{
		/* a NUL in the first 8000 bytes (git's test): binary, said in the
		** subtitle, no lines of U+FFFD (QA 1.55.0 NIT-2) */
		int bin = memchr(buf, '\0', n < 8000 ? n : 8000) != NULL;

		base = strrchr(p, '/') + 1;
		r->state = "ok";
		res_text(r->title, base, strlen(base), XR_TITLE_MAX);
		if (bin)
			snprintf(shown + strlen(shown), sizeof shown - strlen(shown),
			         " \xc2\xb7 binary");
		else if (line > 0)
			snprintf(shown + strlen(shown), sizeof shown - strlen(shown),
			         " \xc2\xb7 line %ld", line);
		if (bin || line > 0)
			res_text(r->subtitle, shown, strlen(shown), XR_RES_LINE);
		if (bin)
			r->first = 1;
		else
			xr_file_lines(buf, n, line, r);
	}
	free(buf);
	return 1;
}

int
xr_res_ref(const struct xr_span *sp, const char *text, const char *cwd,
           int live, struct xr_resolved *r)
{
	memset(r, 0, sizeof *r);
	if (sp->shape == XR_COMMIT)
	{
		/* url_kind made it: https://github.com/<o>/<r>/commit/<sha>. A full
		** 40-hex name is that commit in any repo: only a short one can
		** match an unrelated local commit, so only it is checked (LOW-1
		** ruling). */
		const char *u = strstr(text, "https://github.com/");
		const char *o = u ? u + 19 : "", *e = strchr(o, '/');
		char gh[160];

		e = e ? strchr(e + 1, '/') : NULL;
		if (u && (!e || snprintf(gh, sizeof gh, "%.*s", (int)(e - o), o)
		          >= (int)sizeof gh))
			return 0;
		return res_commit(sp->full, cwd ? cwd : "", live,
		                  u && strlen(sp->full) < 40 ? gh : NULL, r);
	}
	if (sp->shape == XR_FILE)
		return res_file(xr_span_text(sp), text, cwd ? cwd : "", r);
	return 0;
}

void
xr_scan_resolved(struct xr_scan *x, const struct xr_span *sp,
                 const char *text, const char *cwd, int live)
{
	struct xr_resolved *r;
	long long t = xr_now_ms();
	unsigned long sp0 = xr_res_spawned;
	char *b;
	size_t bl;
	FILE *m;
	int k;

	if (!x->full || x->cancel || x->out_full || !(r = malloc(sizeof *r)))
		return;
	if (xr_res_ref(sp, text, cwd, live, r) && (m = ln_open(&b, &bl)))
	{
		fputs("{\"schema\":1,\"resolved\":{\"kind\":", m);
		xr_put_str(m, r->kind);
		fputs(",\"state\":", m);
		xr_put_str(m, r->state);
		fputs(",\"title\":", m);
		xr_put_str(m, r->title);
		fputs(",\"subtitle\":", m);
		xr_put_str(m, r->subtitle);
		/* which row is line N, even clamped at the end; only a file shown */
		if (!strcmp(r->kind, "file") && !strcmp(r->state, "ok"))
			fprintf(m, ",\"first\":%ld", r->first);
		fputs(",\"lines\":[", m);
		for (k = 0; k < r->nlines; k++)
		{
			if (k)
				fputc(',', m);
			xr_put_str(m, r->line[k]);
		}
		fputs("]}}\n", m);
		ln_send(x, m, &b, &bl);
		/* K12: the kind, the state, counts; never a path or a line */
		dch_trace("xray resolve: kind=%s state=%s spawned=%lu ms=%lld",
		          r->kind, r->state, xr_res_spawned - sp0, xr_now_ms() - t);
	}
	free(r);
}
