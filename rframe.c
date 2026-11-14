/*
** Remote frames, the parts dch serve (serve.c) and the dch client for remote
** hosts (remote.c) share: the frame parser, growable buffers and JSON
** helpers, escaping a peer's text for a terminal, subprocesses with
** timeouts, and the tailscale CLI. See rframe.h. The Windows client (win/,
** #077) builds it too: there win/plat.c has the subprocesses and the
** tailscale lookup.
*/
#ifdef _WIN32
#include "win/win.h"
#else
#include "dtach.h"
#endif
#include "rframe.h"

#include <ctype.h>
#include <stdarg.h>
#ifndef _WIN32
#include <poll.h>
#endif
#include <stdint.h>
#include <time.h>
#ifndef _WIN32
#include <unistd.h>
#endif

#define TS_MS 3000		/* each tailscale CLI call */

/* ---- logging and time ------------------------------------------------ */

volatile sig_atomic_t g_stop;

long long
now_ms(void)
{
#ifdef _WIN32
	return (long long)GetTickCount64();
#else
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
#endif
}

/* The UTF-8 sequence at s: its length with the code point in *cp, or 0 when
** it is not one (a stray, overlong or surrogate byte run). */
int
utf8_at(const unsigned char *s, unsigned *cp)
{
	static const unsigned min[] = { 0, 0, 0x80, 0x800, 0x10000 };
	int n = *s < 0x80 ? 1 : *s >= 0xc2 && *s < 0xe0 ? 2
	      : *s >= 0xe0 && *s < 0xf0 ? 3 : *s >= 0xf0 && *s < 0xf5 ? 4 : 0;
	int i;

	*cp = *s;
	if (n < 2)
		return n;
	*cp = *s & (0x7f >> n);
	for (i = 1; i < n; i++)
	{
		if ((s[i] & 0xc0) != 0x80)
			return 0;
		*cp = *cp << 6 | (s[i] & 0x3f);
	}
	return *cp < min[n] || *cp > 0x10ffff ||
	       (*cp >= 0xd800 && *cp < 0xe000) ? 0 : n;
}

/* A code point a terminal acts on: C0, DEL, C1, and the bidi embeddings,
** overrides and isolates (U+202A-U+202E, U+2066-U+2069); also the bidi
** marks (U+200E, U+200F, U+061C) and the line and paragraph separators
** (U+2028, U+2029), which reorder text or cut it into lines (#167). */
int
cp_ctl(unsigned cp)
{
	return cp < 0x20 || (cp >= 0x7f && cp < 0xa0) ||
	       (cp >= 0x202a && cp <= 0x202e) || (cp >= 0x2066 && cp <= 0x2069) ||
	       cp == 0x200e || cp == 0x200f || cp == 0x061c || cp == 0x2028 ||
	       cp == 0x2029;
}

/* s into out[cap] with every control escaped, \xNN for a byte (also one
** that is not UTF-8) and \u{NNNN} for a code point, so that nothing a peer
** sends (a session name, a device id) can drive the operator's terminal. */
void
logsafe(char *out, size_t cap, const char *s)
{
	const unsigned char *p = (const unsigned char *)s;
	size_t o = 0;
	unsigned cp;
	int n;

	while (*p && o + 10 < cap)
	{
		n = utf8_at(p, &cp);
		if (n && !cp_ctl(cp))
		{
			memcpy(out + o, p, n);
			o += n;
		}
		else if (n > 1)
			o += snprintf(out + o, cap - o, "\\u{%04x}", cp);
		else
		{
			o += snprintf(out + o, cap - o, "\\x%02x", *p);
			n = 1;
		}
		p += n;
	}
	out[o] = '\0';
}

/* src into dst[cap], cut to fit. */
void
copy_str(char *dst, size_t cap, const char *src)
{
	size_t n = strlen(src);

	if (n >= cap)
		n = cap - 1;
	memcpy(dst, src, n);
	dst[n] = '\0';
}

/* ---- the frame parser (pure; tests/fuzz/fuzz_serve.c) ---------------- */

/* The frames buffered whole: from a client, the core subset's requests and
** 1.1's ctl (0x30, and #118's 0x34 cancel); from a server, the replies a
** dch client reads (hello, list, exit, error, ok, a ctl's exit 0x33).
** Everything else streams (a ctl's output, 0x31 and 0x32, too). */
static int
rf_control(const struct rf_state *st, unsigned int t)
{
	if (st->from_server)
		return t == 0x11 || t == 0x12 || t == 0x13 || t == 0x14 ||
		       t == 0x18 || t == 0x33;
	return (t >= 0x01 && t <= 0x09) || t == 0x2e || t == 0x30 ||
	       t == 0x34;
}

/* The next piece of a peer's byte stream b[0..n): a client's, or with
** st->from_server a server's (the dch client's side). *used is how much of b
** it took. RF_NEED: more bytes first (nothing taken). RF_CLOSE: a length
** over a guard, so the connection closes: from a client RF_GUARD, and
** RF_CTRL_MAX for a frame held whole; from a server the spec's send limit,
** and RF_LIST_MAX for a frame held whole. RF_FRAME: a whole control frame.
** RF_CHUNK: part of a streamed frame (0x10, 0x0a, any type we do not
** serve), possibly empty when only its header was taken. A 0x10 chunk is
** at most `room` bytes, the space its sink has; 0 room is RF_NEED. Pure:
** no I/O, and *st is the only state carried between calls. */
int
rf_parse(struct rf_state *st, const unsigned char *b, size_t n, size_t room,
         struct rf_frame *f, size_t *used)
{
	uint32_t len;

	*used = 0;
	if (st->left)
	{
		size_t k = st->left;

		if (k > n)
			k = n;
		if (st->type == 0x10 && k > room)
			k = room;
		if (!k)
			return RF_NEED;
		f->type = st->type;
		f->data = b;
		f->len = k;
		st->left -= (uint32_t)k;
		f->end = st->left == 0;
		*used = k;
		return RF_CHUNK;
	}
	if (n < RF_HDR)
		return RF_NEED;
	len = (uint32_t)b[1] << 24 | (uint32_t)b[2] << 16 |
	      (uint32_t)b[3] << 8 | (uint32_t)b[4];
	if (len > (st->from_server ? RF_SEND_MAX : RF_GUARD))
		return RF_CLOSE;
	f->type = b[0];
	f->data = b;
	f->len = 0;
	if (rf_control(st, b[0]))
	{
		if (len > (st->from_server ? RF_LIST_MAX : RF_CTRL_MAX))
			return RF_CLOSE;
		if (n - RF_HDR < len)
			return RF_NEED;
		f->data = b + RF_HDR;
		f->len = len;
		f->end = 1;
		*used = RF_HDR + (size_t)len;
		return RF_FRAME;
	}
	st->type = b[0];
	st->left = len;
	f->end = len == 0;
	*used = RF_HDR;
	return RF_CHUNK;
}

/* ---- growable buffers and JSON out ----------------------------------- */

int
sb_room(struct sb *b, size_t k)
{
	size_t want, c;
	char *q;

	if (b->bad)
		return -1;
	if (k > SB_LIMIT - b->n)
	{
		b->bad = 1;
		return -1;
	}
	want = b->n + k + 1;
	if (want <= b->cap)
		return 0;
	for (c = b->cap ? b->cap : 256; c < want; c *= 2)
		;
	if (!(q = realloc(b->p, c)))
	{
		b->bad = 1;
		return -1;
	}
	b->p = q;
	b->cap = c;
	return 0;
}

void
sb_add(struct sb *b, const void *d, size_t k)
{
	if (sb_room(b, k) < 0)
		return;
	if (k)
		memcpy(b->p + b->n, d, k);
	b->n += k;
	b->p[b->n] = '\0';
}

void
sb_s(struct sb *b, const char *s)
{
	sb_add(b, s, strlen(s));
}

/* s as a JSON string (bytes that are not UTF-8 read as U+FFFD). */
void
sb_q(struct sb *b, const char *s)
{
	size_t k = strlen(s), r;

	if (k > SB_LIMIT / 6 || sb_room(b, k * 6 + 3) < 0)
	{
		b->bad = 1;
		return;
	}
	r = dch_json_quote(b->p + b->n, b->cap - b->n, s);
	if (r == SIZE_MAX)
		b->bad = 1;
	else
		b->n += r;
}

void
sb_ll(struct sb *b, long long v)
{
	char d[24];
	int k = snprintf(d, sizeof d, "%lld", v);

	if (k > 0 && (size_t)k < sizeof d)
		sb_add(b, d, (size_t)k);
}

/* ---- JSON in --------------------------------------------------------- */

/* A JSON integer token into *v: -1 when it is not one (a fraction, an
** exponent, a bool, a string). */
int
tok_int(const struct json_parser *jp, int t, long long *v)
{
	const char *s;
	char *end;

	if (t < 0 || jp->tok[t].kind != JSON_PRIMITIVE)
		return -1;
	s = jp->text + jp->tok[t].start;
	if (*s != '-' && !isdigit((unsigned char)*s))
		return -1;
	errno = 0;
	*v = strtoll(s, &end, 10);
	return errno || end != jp->text + jp->tok[t].end ? -1 : 0;
}

/* Field `key` of object token `obj`: 1 and the value in out / *v, 0 when it
** is absent, -1 when it is the wrong type, too long or duplicated. */
int
jstr(const struct json_parser *jp, int obj, const char *key, char *out,
     size_t cap)
{
	int t = dch_json_field(jp, obj, key);

	if (t == -1)
		return 0;
	return t >= 0 && jp->tok[t].kind == JSON_STRING &&
	       dch_json_decode(jp, t, out, cap) == 0 ? 1 : -1;
}

int
jint(const struct json_parser *jp, int obj, const char *key, long long *v)
{
	int t = dch_json_field(jp, obj, key);

	if (t == -1)
		return 0;
	return tok_int(jp, t, v) == 0 ? 1 : -1;
}

/* Field `key` of `obj` when it is a container of `kind`, else -1. */
int
jcont(const struct json_parser *jp, int obj, const char *key,
      enum json_kind kind)
{
	int t = obj >= 0 ? dch_json_field(jp, obj, key) : -1;

	return t >= 0 && jp->tok[t].kind == kind ? t : -1;
}

/* Field `key` of `obj` as a StableNodeID (status Self.ID, whois
** Node.StableID) into out[NODE_ID_MAX]: a non-empty string of ASCII
** letters, digits, '-', '_' or '.'. 1, 0 when absent ("" in out), -1
** otherwise (a number, junk, too long). */
int
jnode(const struct json_parser *jp, int obj, const char *key, char *out)
{
	int r = jstr(jp, obj, key, out, NODE_ID_MAX);

	if (r == 1 && (!*out || out[strspn(out, "abcdefghijklmnopqrstuvwxyz"
	                                   "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
	                                   "0123456789-_.")]))
		r = -1;
	if (r != 1)
		*out = '\0';
	return r;
}

/* Whether object `obj` carries tags: 0 for no Tags key, null or [], else
** 1, and so for anything odd (a duplicate key, a string): fail closed. */
int
has_tags(const struct json_parser *jp, int obj)
{
	int t = dch_json_field(jp, obj, "Tags");

	if (t == -1)
		return 0;
	if (t >= 0 && jp->tok[t].kind == JSON_ARRAY)
		return jp->tok[t].next > t + 1;
	return !(t >= 0 && jp->tok[t].kind == JSON_PRIMITIVE &&
	         jp->tok[t].end - jp->tok[t].start == 4 &&
	         !memcmp(jp->text + jp->tok[t].start, "null", 4));
}

/* `tailscale status --json`: Self.UserID into *uid, Self.ID (the host's
** StableNodeID) into node[NODE_ID_MAX] ("" when absent), *tagged when Self
** has tags (or an odd Tags) or its user is the tagged-devices identity. 0,
** or -1 (also for a duplicate Self or a Self.ID that is not a node id). */
int
status_self(const char *js, long long *uid, char *node, int *tagged)
{
	struct json_parser jp;
	int self, user, rc = -1;
	char key[24], login[256];

	*tagged = 0;
	*node = '\0';
	if (dch_json_parse(&jp, js) == 0 && jp.tok[0].kind == JSON_OBJECT &&
	    (self = jcont(&jp, 0, "Self", JSON_OBJECT)) >= 0 &&
	    jint(&jp, self, "UserID", uid) == 1 &&
	    jnode(&jp, self, "ID", node) >= 0)
	{
		*tagged = has_tags(&jp, self);
		snprintf(key, sizeof key, "%lld", *uid);
		user = jcont(&jp, jcont(&jp, 0, "User", JSON_OBJECT), key,
		             JSON_OBJECT);
		if (user >= 0 &&
		    jstr(&jp, user, "LoginName", login, sizeof login) == 1 &&
		    !strcmp(login, "tagged-devices"))
			*tagged = 1;
		rc = 0;
	}
	dch_json_free(&jp);
	return rc;
}

/* `tailscale whois --json`: UserProfile.ID into *uid, LoginName into login,
** Node.StableID into node[NODE_ID_MAX] ("" when absent), *tagged when the
** node has tags (or an odd Tags) or is the tagged-devices identity. 0, or
** -1 when the document is unusable: no single UserProfile with an integer
** ID, no single Node, a StableID that is not a node id. Node.ID (an int64
** on both control planes) is not used. */
int
whois_user(const char *js, long long *uid, char *node, char *login,
           size_t lcap, int *tagged)
{
	struct json_parser jp;
	int up, nd, rc = -1;

	*tagged = 0;
	*node = '\0';
	login[0] = '\0';
	if (dch_json_parse(&jp, js) == 0 && jp.tok[0].kind == JSON_OBJECT &&
	    (up = jcont(&jp, 0, "UserProfile", JSON_OBJECT)) >= 0 &&
	    jint(&jp, up, "ID", uid) == 1 &&
	    (nd = jcont(&jp, 0, "Node", JSON_OBJECT)) >= 0 &&
	    jnode(&jp, nd, "StableID", node) >= 0)
	{
		if (jstr(&jp, up, "LoginName", login, lcap) != 1)
			login[0] = '\0';
		*tagged = has_tags(&jp, nd) || !strcmp(login, "tagged-devices");
		rc = 0;
	}
	dch_json_free(&jp);
	return rc;
}

/* ---- ctl: the verbs dch serve runs (pure; tests/fuzz) --------------- */

/* A session name dch can be handed as one argv word: UTF-8 with no '/' and
** no control logsafe would escape. The leading '-' is checked apart (it has
** its own spec message). */
int
name_ok(const char *s)
{
	const unsigned char *p = (const unsigned char *)s;
	size_t n = strlen(s);
	unsigned cp;
	int k;

	if (!n || n > NAME_CAP || s[0] == '-')
		return 0;
	for (; *p; p += k)
		if (!(k = utf8_at(p, &cp)) || cp_ctl(cp) || cp == '/')
			return 0;
	return 1;
}

enum { CN_NONE, CN_ONE, CN_MANY };	/* the session names a verb takes */
enum { CT_NONE, CT_TEXT, CT_ALIAS, CT_SAY };	/* free text after them */
#define CTL_JSON 8	/* --json, required */
#define CTL_ALL 16	/* --all instead of the names (--forget only) */

/* The allowlist (remote frames 1.1, #114). Each verb's shape is dch's own
** parser's (dch.c main), narrowed: the names right after the verb (CN_ONE)
** or anywhere among the flags (CN_MANY); then only the flags in `opts`;
** then, for CT_TEXT, an optional `--` and free text (--send's words,
** --keys' keys, --spawn's command), for CT_SAY a required `--` and the
** text, for CT_ALIAS exactly one word. --recent takes an optional count,
** --size COLSxROWS, --as a session name. Nothing else ever runs. */
static const struct ctl_verb
{
	const char *verb;
	unsigned char names, tail;
	int flags;
	const char *opts;	/* " --a --b ": the flags it takes */
} ctl_verbs[] = {
	/* verb         names    tail      flags                 opts */
	{ "--read",     CN_ONE,  CT_NONE,  0, " --ansi --recent --cursor " },
	{ "--send",     CN_ONE,  CT_TEXT,  0, "" },
	{ "--run",      CN_ONE,  CT_TEXT,  CTL_LONG, "" },	/* ag_type, #112 */
	{ "--keys",     CN_ONE,  CT_TEXT,  0, "" },
	{ "--follow",   CN_ONE,  CT_NONE,  CTL_FOREVER, "" },
	{ "--status",   CN_ONE,  CT_NONE,  0, "" },
	{ "-k",         CN_ONE,  CT_NONE,  0, "" },
	{ "-m",         CN_ONE,  CT_ALIAS, 0, "" },
	{ "-d",         CN_ONE,  CT_NONE,  0, "" },
	{ "--spawn",    CN_ONE,  CT_TEXT,  CTL_CREATE, " --size " },
	{ "--history",  CN_NONE, CT_NONE,  CTL_JSON, "" },
	{ "--revive",   CN_ONE,  CT_NONE,  CTL_CREATE | CTL_LONG | CTL_JSON,
	                " --as --no-message " },
	{ "--pin",      CN_MANY, CT_NONE,  CTL_JSON, "" },
	{ "--unpin",    CN_MANY, CT_NONE,  CTL_JSON, "" },
	{ "--forget",   CN_MANY, CT_NONE,  CTL_JSON | CTL_ALL, "" },
	{ "--stop",     CN_MANY, CT_NONE,  CTL_LONG | CTL_JSON, "" },
	{ "--say",      CN_ONE,  CT_SAY,   CTL_LONG | CTL_JSON, "" },
};

static const struct ctl_verb *
ctl_find(const char *verb)
{
	size_t i;

	for (i = 0; i < sizeof ctl_verbs / sizeof *ctl_verbs; i++)
		if (!strcmp(verb, ctl_verbs[i].verb))
			return &ctl_verbs[i];
	return NULL;
}

int
ctl_known(const char *verb)
{
	return ctl_find(verb) != NULL;
}

/* Never in a ctl's argv, but in free text: dch's other modes and help, the
** forced forget. */
static int
ctl_never(const char *a)
{
	return !strcmp(a, "--config") || !strcmp(a, "serve") ||
	       !strcmp(a, "-h") || !strcmp(a, "--help") || !strcmp(a, "-f");
}

/* All digits, lo..hi. */
static int
ctl_count(const char *s, long lo, long hi)
{
	size_t k = strspn(s, "0123456789");
	long v;

	if (!k || k > 5 || s[k])
		return 0;
	v = strtol(s, NULL, 10);
	return v >= lo && v <= hi;
}

static int
ctl_size(const char *s)
{
	const char *x = strchr(s, 'x');
	char c[8];

	if (!x || (size_t)(x - s) >= sizeof c)
		return 0;
	memcpy(c, s, (size_t)(x - s));
	c[x - s] = '\0';
	return ctl_count(c, 1, 4096) && ctl_count(x + 1, 1, 4096);
}

static int ctl_no(char *why, size_t cap, const char *fmt, ...)
	__attribute__((format(printf, 3, 4)));

static int
ctl_no(char *why, size_t cap, const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(why, cap, fmt, ap);
	va_end(ap);
	return -1;
}

/* A session name in a ctl: 0, or -1 with why. */
static int
ctl_name(const char *s, char *why, size_t cap)
{
	if (s[0] == '-')
		return ctl_never(s) || !strcmp(s, "--all")
		       ? ctl_no(why, cap, "%.60s is not allowed", s)
		       : ctl_no(why, cap, "%s", DASH_MSG);
	if (!name_ok(s) || ctl_never(s))
		return ctl_no(why, cap, "invalid session name");
	return 0;
}

int
xray_scope_ok(const char *s)
{
	return !strcmp(s, "session") || !strcmp(s, "project") ||
	       !strcmp(s, "all");
}

int
xray_anchor_ok(const char *s)
{
	size_t n = strspn(s, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
	                     "0123456789:_.-");

	return n >= 1 && n <= 128 && !s[n];
}

/* --search's text (#297): 2 to 200 bytes of UTF-8 with no control
** character (cp_ctl's), so one line a client typed. */
int
xray_search_ok(const char *s)
{
	const unsigned char *p = (const unsigned char *)s;
	size_t n = strlen(s);
	unsigned cp;
	int k;

	if (n < 2 || n > 200)
		return 0;
	for (; *p; p += k)
		if (!(k = utf8_at(p, &cp)) || cp_ctl(cp))
			return 0;
	return 1;
}

int
xray_budget(const char *s)
{
	int k, v = 0;

	/* digits only, no strtoul: no sign, no 0x, no wrap */
	for (k = 0; s[k] >= '0' && s[k] <= '9'; k++)
		if ((v = v * 10 + s[k] - '0') > 1000)
			v = 1000;
	return !s[k] && k && v >= 50 && v <= 300 ? v : -1;
}

/* --xray (spec #832 v3.2.1 4.4): the name right after the verb, then 3.1's
** flags in any order, each once: --json, required (the view is the
** client's own, as --requests is); one of --follow, --ref <text>,
** --search <text> (#297), --chapter <anchor> and --clear; --scope, --full
** and --budget-ms only with --ref. A value is the next element even when it
** starts with '-'. --ref's and --search's text and the anchor are CK_TEXT
** (an anchor may hold ':'). The --ref
** text's byte and grammar rules are xray.c's, which the Windows client does
** not build: dch serve checks them (ctl_request), the dch client before
** it sends. --follow has no timeout and no cap. */
static int
ctl_xray(int argc, char *const argv[], unsigned char *kind, char *why,
         size_t cap)
{
	static const char *const opt[] = { "--json", "--follow", "--clear",
	                                   "--full", "--ref", "--chapter",
	                                   "--scope", "--budget-ms", "--search" };
	enum { X_JSON = 1, X_FOLLOW = 2, X_CLEAR = 4, X_FULL = 8, X_REF = 16,
	       X_CHAPTER = 32, X_SCOPE = 64, X_BUDGET = 128, X_SEARCH = 256 };
	const char *val[9] = { NULL };
	unsigned seen = 0, k, one;
	int i;

	if (ctl_name(argv[1], why, cap) < 0)
		return -1;
	kind[0] = CK_VERB;
	kind[1] = CK_NAME;
	for (i = 2; i < argc; i++)
	{
		const char *a = argv[i];

		for (k = 0; k < 9 && strcmp(a, opt[k]); k++)
			;
		if (k == 9)
			return ctl_never(a) || !strcmp(a, "--all")
			       ? ctl_no(why, cap, "%.60s is not allowed", a)
			       : a[0] == '-'
			       ? ctl_no(why, cap, "%.60s is not allowed with --xray",
			                a)
			       : ctl_no(why, cap, "unexpected %.60s after --xray", a);
		if (seen & 1u << k)
			return ctl_no(why, cap, "%s twice", opt[k]);
		seen |= 1u << k;
		kind[i] = CK_FLAG;
		if (k >= 4)
		{
			if (++i >= argc)
				return ctl_no(why, cap, "%s needs a value", opt[k]);
			val[k] = argv[i];
			kind[i] = k == 6 || k == 7 ? CK_VALUE : CK_TEXT;
		}
	}
	one = seen & (X_FOLLOW | X_CLEAR | X_REF | X_CHAPTER | X_SEARCH);
	if (!(seen & X_JSON))
		return ctl_no(why, cap, "--xray needs --json (the view is "
		              "client-side only)");
	if (one & (one - 1))
		return ctl_no(why, cap, "--ref, --search, --follow, --chapter and "
		              "--clear exclude each other");
	if (seen & (X_FULL | X_SCOPE | X_BUDGET) && !(seen & X_REF))
		return ctl_no(why, cap, "--scope, --full and --budget-ms come "
		              "with --ref");
	if (val[5] && !xray_anchor_ok(val[5]))
		return ctl_no(why, cap, "bad anchor");
	if (val[6] && !xray_scope_ok(val[6]))
		return ctl_no(why, cap, "bad scope");
	if (val[7] && xray_budget(val[7]) < 0)
		return ctl_no(why, cap, "bad budget");
	if (val[8] && !xray_search_ok(val[8]))
		return ctl_no(why, cap, "bad search: 2 to 200 bytes, no control "
		              "character");
	return seen & X_FOLLOW ? CTL_FOREVER | CTL_NOCAP : 0;
}

int
ctl_check(int argc, char *const argv[], unsigned char *kind, char *why,
          size_t cap)
{
	const struct ctl_verb *v;
	int i = 1, names = 0, json = 0, all = 0, dd = 0;

	if (argc < 2)
		return ctl_no(why, cap, "a ctl request takes a verb and a "
		              "session name");
	if (argc > CTL_ARGV_MAX)
		return ctl_no(why, cap, "too many arguments");
	memset(kind, 0, (size_t)argc);
	if (!strcmp(argv[0], "--xray"))
		return ctl_xray(argc, argv, kind, why, cap);
	if (!(v = ctl_find(argv[0])))
		return ctl_no(why, cap, "%.60s is not allowed", argv[0]);
	kind[0] = CK_VERB;
	if (v->names == CN_ONE && v->tail != CT_SAY)
	{
		if (ctl_name(argv[1], why, cap) < 0)
			return -1;
		kind[i++] = CK_NAME;
		names = 1;
	}
	if (v->tail == CT_ALIAS)
	{
		if (argc != 3)
			return ctl_no(why, cap, "%s takes a session name and an "
			              "alias", v->verb);
		kind[2] = CK_TEXT;
		return v->flags & (CTL_CREATE | CTL_LONG | CTL_FOREVER);
	}
	for (; i < argc; i++)
	{
		const char *a = argv[i];
		char sp[80];

		if (v->tail >= CT_TEXT && !strcmp(a, "--"))
		{
			kind[i++] = CK_FLAG;
			dd = 1;
			break;
		}
		/* dch's parser: "", "-" or a word start the command */
		if (v->tail == CT_TEXT && (a[0] != '-' || !a[1]))
			break;
		if ((v->flags & CTL_JSON && !strcmp(a, "--json")) ||
		    (v->flags & CTL_ALL && !strcmp(a, "--all")))
		{
			*(a[2] == 'j' ? &json : &all) = 1;
			kind[i] = CK_FLAG;
			continue;
		}
		if ((v->names == CN_MANY || v->tail == CT_SAY) && a[0] &&
		    a[0] != '-')
		{
			if (ctl_name(a, why, cap) < 0)
				return -1;
			kind[i] = CK_NAME;
			names++;
			continue;
		}
		snprintf(sp, sizeof sp, " %.70s ", a);
		if (a[0] != '-' || strchr(a, ' ') || !strstr(v->opts, sp))
			return ctl_never(a) || !strcmp(a, "--all")
			       ? ctl_no(why, cap, "%.60s is not allowed", a)
			       : a[0] == '-'
			       ? ctl_no(why, cap, "%.60s is not allowed with %s",
			                a, v->verb)
			       : ctl_no(why, cap, "unexpected %.60s after %s", a,
			                v->verb);
		kind[i] = CK_FLAG;
		if (!strcmp(a, "--recent") && i + 1 < argc &&
		    argv[i + 1][0] >= '0' && argv[i + 1][0] <= '9')
		{
			if (!ctl_count(argv[++i], 1, 65535))
				return ctl_no(why, cap, "--recent wants 1..65535 "
				              "lines");
			kind[i] = CK_VALUE;
		}
		else if (!strcmp(a, "--size"))
		{
			if (++i >= argc || !ctl_size(argv[i]))
				return ctl_no(why, cap, "--size wants COLSxROWS");
			kind[i] = CK_VALUE;
		}
		else if (!strcmp(a, "--as"))
		{
			if (++i >= argc)
				return ctl_no(why, cap, "--as needs a session "
				              "name");
			if (ctl_name(argv[i], why, cap) < 0)
				return -1;
			kind[i] = CK_VALUE;
		}
	}
	for (; i < argc; i++)
		kind[i] = CK_TEXT;	/* the loop ends here only for text */
	if (v->tail == CT_SAY && !dd)
		return ctl_no(why, cap, "%s needs -- before its text", v->verb);
	if (v->tail == CT_SAY && names != 1)
		return ctl_no(why, cap, "%s takes one session name", v->verb);
	if (v->names == CN_MANY && !!names == all)
		return ctl_no(why, cap, "%s takes session names%s", v->verb,
		              v->flags & CTL_ALL ? ", or --all" : "");
	if (v->flags & CTL_JSON && !json)
		return ctl_no(why, cap, "%s needs --json", v->verb);
	return v->flags & (CTL_CREATE | CTL_LONG | CTL_FOREVER);
}

/* ---- subprocesses ---------------------------------------------------- */

#ifndef _WIN32	/* win/plat.c: CreateProcess; cloexec/nonblock on sockets */
int
cloexec(int fd)
{
	return fcntl(fd, F_SETFD, FD_CLOEXEC);
}

int
nonblock(int fd)
{
	int fl = fcntl(fd, F_GETFL, 0);

	return fl < 0 ? -1 : fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

int
pipe_ce(int p[2])
{
	if (pipe(p) < 0)
		return -1;
	cloexec(p[0]);
	cloexec(p[1]);
	return 0;
}

/* In a forked child, just before execv: default signal dispositions (execve
** keeps SIG_IGN, and SIGPIPE is ignored here), nothing blocked, and no fd
** but 0-2. Every fd we open is close-on-exec too; this is the second net. */
void
exec_prep(void)
{
	static const int sigs[] = { SIGPIPE, SIGCHLD, SIGINT, SIGTERM, SIGHUP };
	struct sigaction sa;
	sigset_t none;
	long fd, max = sysconf(_SC_OPEN_MAX);
	size_t i;

	memset(&sa, 0, sizeof sa);
	sa.sa_handler = SIG_DFL;
	sigemptyset(&sa.sa_mask);
	for (i = 0; i < sizeof sigs / sizeof *sigs; i++)
		sigaction(sigs[i], &sa, NULL);
	sigemptyset(&none);
	sigprocmask(SIG_SETMASK, &none, NULL);
	/* ponytail: a 4096 cap on the sweep; CLOEXEC covers ours past it */
	if (max < 0 || max > 4096)
		max = 4096;
	for (fd = 3; fd < max; fd++)
		close((int)fd);
}

/* --search's or --ref's text as dch serve hands it to its ctl child, on
** stdin with "-" in the argv (LOW-XN1): fd read to its end into b, at most
** 256 bytes (--ref's limit, the larger of the two) and no NUL; b, or NULL
** (overlong, a NUL, a read error). The flag's own rules come after. */
char *
ctl_text_read(int fd, char b[257])
{
	size_t n = 0;
	ssize_t r;

	while (n < 257 && (r = read(fd, b + n, 257 - n)) != 0)
		if (r > 0)
			n += (size_t)r;
		else if (errno != EINTR)
			return NULL;
	if (n > 256 || memchr(b, '\0', n))
		return NULL;
	b[n] = '\0';
	return b;
}

/* Run argv (argv[0] an absolute path; no shell) with stdin on /dev/null.
** Its stdout goes to *out (malloc'd, NUL-terminated, *outn bytes, at most
** cap) when out is set, else /dev/null; its stderr to err[errcap] when err
** is set, else /dev/null. Past timeout_ms or cap it is SIGKILLed and
** reaped. The wait status, or -1 when it could not run, timed out or
** overflowed. */
int
rf_run(char *const argv[], char **out, size_t *outn, size_t cap, char *err,
    size_t errcap, int timeout_ms)
{
	int po[2] = { -1, -1 }, pe[2] = { -1, -1 }, st = -1, bad = 0;
	size_t on = 0, en = 0;
	long long end = now_ms() + timeout_ms;
	char *ob = NULL;
	pid_t pid;
	pid_t w;

	if (out)
	{
		*out = NULL;
		if (!(ob = malloc(cap + 1)) || pipe_ce(po) < 0)
			goto fail;
	}
	if (err && (errcap < 2 || pipe_ce(pe) < 0))
		goto fail;
	if ((pid = fork()) < 0)
		goto fail;
	if (pid == 0)
	{
		int nul = open("/dev/null", O_RDWR);

		if (nul < 0)
			_exit(127);
		dup2(nul, 0);
		dup2(out ? po[1] : nul, 1);
		dup2(err ? pe[1] : nul, 2);
		exec_prep();
		execv(argv[0], argv);
		_exit(127);
	}
	if (po[1] >= 0)
		close(po[1]);
	if (pe[1] >= 0)
		close(pe[1]);
	po[1] = pe[1] = -1;
	while ((po[0] >= 0 || pe[0] >= 0) && !bad)
	{
		struct pollfd pf[2];
		int np = 0, left = (int)(end - now_ms()), j;

		if (left <= 0 || g_stop)
		{
			bad = 1;
			break;
		}
		if (po[0] >= 0)
			pf[np++] = (struct pollfd){ po[0], POLLIN, 0 };
		if (pe[0] >= 0)
			pf[np++] = (struct pollfd){ pe[0], POLLIN, 0 };
		if (poll(pf, (nfds_t)np, left) < 0)
		{
			if (errno == EINTR)
				continue;
			bad = 1;
			break;
		}
		for (j = 0; j < np; j++)
		{
			char junk[512];
			int isout = pf[j].fd == po[0];
			ssize_t r;

			if (!pf[j].revents)
				continue;
			if (isout)
				r = ob && on < cap ? read(po[0], ob + on, cap - on)
				                   : read(po[0], junk, 1);
			else
				r = err && en < errcap - 1
				        ? read(pe[0], err + en, errcap - 1 - en)
				        : read(pe[0], junk, sizeof junk);
			if (r < 0 && (errno == EINTR || errno == EAGAIN))
				continue;
			if (r <= 0)
			{
				close(pf[j].fd);
				*(isout ? &po[0] : &pe[0]) = -1;
			}
			else if (isout && on >= cap)
				bad = 1;	/* over the cap */
			else if (isout)
				on += (size_t)r;
			else if (en < errcap - 1)
				en += (size_t)r;
		}
	}
	if (bad)
		kill(pid, SIGKILL);
	while ((w = waitpid(pid, &st, WNOHANG)) == 0 ||
	       (w < 0 && errno == EINTR))
	{
		if (w == 0 && (now_ms() >= end || g_stop))
		{
			kill(pid, SIGKILL);
			while (waitpid(pid, &st, 0) < 0 && errno == EINTR)
				;
			bad = 1;
			break;
		}
		poll(NULL, 0, 5);
	}
	if (w < 0)
		bad = 1;
fail:
	if (po[0] >= 0)
		close(po[0]);
	if (po[1] >= 0)
		close(po[1]);
	if (pe[0] >= 0)
		close(pe[0]);
	if (pe[1] >= 0)
		close(pe[1]);
	if (err)
		err[errcap ? (en < errcap ? en : errcap - 1) : 0] = '\0';
	if (bad || st < 0)
	{
		free(ob);
		return -1;
	}
	if (out)
	{
		ob[on] = '\0';
		*out = ob;
		if (outn)
			*outn = on;
	}
	return st;
}
#endif

/* ---- tailscale ------------------------------------------------------- */

char g_ts[PATH_MAX];	/* the tailscale CLI; "" when not found */

/* `tailscale <a...>` exiting 0, stdout into *out (malloc'd): 0, or -1. */
int
ts_run(char *a1, char *a2, char *a3, char *a4, char **out, size_t cap)
{
	char *av[] = { g_ts, a1, a2, a3, a4, NULL };
	int st;

	*out = NULL;
	if (!g_ts[0])
		return -1;
	st = rf_run(av, out, NULL, cap, NULL, 0, TS_MS);
	if (st < 0 || !WIFEXITED(st) || WEXITSTATUS(st) != 0)
	{
		free(*out);
		*out = NULL;
		return -1;
	}
	return 0;
}

/* The tailscale CLI into g_ts: the first absolute PATH entry holding one,
** else a fixed install location (on macOS the CLI is often only a shell
** alias for the app's binary). An empty or relative PATH entry is skipped:
** it names whatever directory dch serve was started from, and this binary
** decides every peer. win/plat.c has the Windows one. */
#ifndef _WIN32
void
ts_find(void)
{
	static const char app[] =
		"/Applications/Tailscale.app/Contents/MacOS/Tailscale";
	char path[4096];
	const char *e = getenv("PATH"), *d, *c;
	size_t o = 0, l;
	struct stat st;

	for (d = e; d && *d; d = c ? c + 1 : NULL)
	{
		c = strchr(d, ':');
		l = c ? (size_t)(c - d) : strlen(d);
		if (d[0] == '/' && o + l + 2 < sizeof path)
		{
			memcpy(path + o, d, l);
			o += l;
			path[o++] = ':';
		}
	}
	copy_str(path + o, sizeof path - o,
	         "/usr/bin:/usr/local/bin:/opt/homebrew/bin");
	if (path_lookup("tailscale", path, "/", g_ts, sizeof g_ts) < 0 &&
	    stat(app, &st) == 0 && S_ISREG(st.st_mode) && access(app, X_OK) == 0)
		copy_str(g_ts, sizeof g_ts, app);
}
#endif
