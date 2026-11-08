/*
** dch serve: a server for Prosper's "remote frames" protocol 1.0, so the
** Prosper iPhone app can drive dch sessions on a host with no Prosper.
** Spec: ventsislav-georgiev/prosper docs/protocol/remote-frames.md; its
** conformance set runs from tests/remote-frames/.
**
** Process model. The parent owns the listening socket and never reads a
** byte from a peer. It drops a peer outside 100.64.0.0/10 at accept, then
** forks one handler per connection. The handler runs the owner check
** (tailscale whois) before it reads anything, then serves the connection
** with one poll loop. A slow subprocess stalls only its own connection, and
** a crash costs one connection. Handlers report "attached to <name>" and
** "watching for <device>" up a pipe. The parent then retires the older
** handlers (the newest attach wins; at most 4 streams per device).
**
** An attached connection runs a real `dch -E -f -n <name>` client in a pty,
** as Prosper does. The master repaints for a new client, so the first 0x10
** bytes are a full screen. A resize is TIOCSWINSZ, which the client forwards
** as MSG_WINCH. Every other verb runs dch itself: --ls-json, -k, -m, and
** --read --ansi --cursor. Nothing goes through a shell: argv vectors, execv,
** timeouts and capped output only.
*/
#include "dtach.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <dirent.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdarg.h>
#include <stdint.h>
#include <strings.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/sysctl.h>
#endif

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0	/* SIGPIPE is ignored anyway */
#endif

#define SV_PORT 8771
#define RF_HDR 5
#define RF_GUARD (256u << 20)	/* spec receive guard: a longer length closes */
#define RF_SEND_MAX (8u << 20)	/* spec send limit, the phone's maxFrame */
/* Our guard for the frames we buffer whole: the JSON control requests of
** the core subset, none of which needs more than a few hundred bytes. 0x10
** streams into the pty and every other type streams to nowhere, so no frame
** is ever held whole past this, whatever its length (up to RF_GUARD). */
#define RF_CTRL_MAX 65536
#define PTY_CHUNK 32768		/* a pty 0x10 frame, as Prosper's */
#define OUT_HIGH 65536		/* past this unsent, stop making more output */
#define PIN_MAX 65536		/* client bytes waiting for the pty */
#define SB_LIMIT (16u << 20)	/* any one growable buffer */
#define STALL_MS 20000		/* output untaken this long closes (spec) */
#define IDLE_MS 30000		/* no whole frame this long, not attached or
				** watching: closes (slowloris) */
#define LINGER_MS 2000
#define WATCH_POLL_MS 2000
#define WATCH_BEAT_MS 25000	/* spec heartbeat */
#define MAX_CONN 64
#define MAX_PER_PEER 16
#define NEG_N 64		/* refused peers remembered by the parent */
#define NEG_MS 30000		/* for this long, refused with no fork */
#define MAX_SELF 16		/* `tailscale ip -4` lines kept */
#define MAX_WATCH_PER_DEV 4	/* spec */
#define NAME_CAP 255
#define DEV_CAP 128
#define MAX_ALLOW 16
#define MAX_CMD 256
#define TS_MS 3000		/* each tailscale CLI call */
#define DASH_MSG "a session name starting with - can't be passed to dch"

/* ---- logging and time ------------------------------------------------ */

static volatile sig_atomic_t g_stop;

static long long
now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* The UTF-8 sequence at s: its length with the code point in *cp, or 0 when
** it is not one (a stray, overlong or surrogate byte run). */
static int
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
** overrides and isolates (U+202A-U+202E, U+2066-U+2069). */
static int
cp_ctl(unsigned cp)
{
	return cp < 0x20 || (cp >= 0x7f && cp < 0xa0) ||
	       (cp >= 0x202a && cp <= 0x202e) || (cp >= 0x2066 && cp <= 0x2069);
}

/* s into out[cap] with every control escaped, \xNN for a byte (also one
** that is not UTF-8) and \u{NNNN} for a code point, so that nothing a peer
** sends (a session name, a device id) can drive the operator's terminal. */
static void
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

static void slog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* One line to stderr, "<UTC time> dch serve[pid]: <text>", as one write so
** handlers' lines do not interleave. The text goes through logsafe: it
** carries peer-supplied strings. */
static void
slog(const char *fmt, ...)
{
	char line[2048], text[1024];
	struct timespec ts;
	struct tm tm;
	va_list ap;
	size_t n;
	int k;

	clock_gettime(CLOCK_REALTIME, &ts);
	gmtime_r(&ts.tv_sec, &tm);
	n = strftime(line, sizeof line, "%Y-%m-%dT%H:%M:%S", &tm);
	k = snprintf(line + n, sizeof line - n, ".%03ldZ dch serve[%ld]: ",
	             (long)(ts.tv_nsec / 1000000), (long)getpid());
	if (k < 0 || (size_t)k >= sizeof line - n)
		return;
	n += (size_t)k;
	va_start(ap, fmt);
	k = vsnprintf(text, sizeof text, fmt, ap);
	va_end(ap);
	if (k < 0)
		return;
	logsafe(line + n, sizeof line - n - 1, text);
	n += strlen(line + n);
	line[n++] = '\n';
	(void)!write(2, line, n);
}

/* src into dst[cap], cut to fit. */
static void
copy_str(char *dst, size_t cap, const char *src)
{
	size_t n = strlen(src);

	if (n >= cap)
		n = cap - 1;
	memcpy(dst, src, n);
	dst[n] = '\0';
}

/* ---- the frame parser (pure; tests/fuzz/fuzz_serve.c) ---------------- */

enum { RF_NEED, RF_FRAME, RF_CHUNK, RF_CLOSE };

struct rf_state
{
	uint32_t left;		/* payload bytes still to come, streamed frame */
	unsigned char type;	/* its type */
};

struct rf_frame
{
	unsigned char type;
	const unsigned char *data;
	size_t len;
	int end;		/* the frame is complete with this piece */
};

/* The core subset's requests, buffered whole; everything else streams. */
static int
rf_control(unsigned int t)
{
	return (t >= 0x01 && t <= 0x09) || t == 0x2e;
}

/* The next piece of a client's byte stream b[0..n). *used is how much of b
** it took. RF_NEED: more bytes first (nothing taken). RF_CLOSE: a length
** over a guard, so the connection closes. RF_FRAME: a whole control frame.
** RF_CHUNK: part of a streamed frame (0x10, 0x0a, any type we do not
** serve), possibly empty when only its header was taken. A 0x10 chunk is
** at most `room` bytes, the space its sink has; 0 room is RF_NEED. Pure:
** no I/O, and *st is the only state carried between calls. */
static int
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
	if (len > RF_GUARD)
		return RF_CLOSE;
	f->type = b[0];
	f->data = b;
	f->len = 0;
	if (rf_control(b[0]))
	{
		if (len > RF_CTRL_MAX)
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

struct sb
{
	char *p;
	size_t n, cap;
	int bad;		/* out of memory or past SB_LIMIT */
};

static int
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

static void
sb_add(struct sb *b, const void *d, size_t k)
{
	if (sb_room(b, k) < 0)
		return;
	if (k)
		memcpy(b->p + b->n, d, k);
	b->n += k;
	b->p[b->n] = '\0';
}

static void
sb_s(struct sb *b, const char *s)
{
	sb_add(b, s, strlen(s));
}

/* s as a JSON string (bytes that are not UTF-8 read as U+FFFD). */
static void
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

static void
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
static int
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
static int
jstr(const struct json_parser *jp, int obj, const char *key, char *out,
     size_t cap)
{
	int t = dch_json_field(jp, obj, key);

	if (t == -1)
		return 0;
	return t >= 0 && jp->tok[t].kind == JSON_STRING &&
	       dch_json_decode(jp, t, out, cap) == 0 ? 1 : -1;
}

static int
jint(const struct json_parser *jp, int obj, const char *key, long long *v)
{
	int t = dch_json_field(jp, obj, key);

	if (t == -1)
		return 0;
	return tok_int(jp, t, v) == 0 ? 1 : -1;
}

/* Field `key` of `obj` when it is a container of `kind`, else -1. */
static int
jcont(const struct json_parser *jp, int obj, const char *key,
      enum json_kind kind)
{
	int t = obj >= 0 ? dch_json_field(jp, obj, key) : -1;

	return t >= 0 && jp->tok[t].kind == kind ? t : -1;
}

/* A request payload p[0..n) as a JSON object, empty read as {}: 0 with *jp
** parsed over text (dch_json_free it), -1 when it is anything else. */
static int
rq_json(struct json_parser *jp, char *text, const unsigned char *p, size_t n)
{
	if (!n)
		memcpy(text, "{}", 3);
	else
	{
		if (memchr(p, 0, n))
			return -1;
		memcpy(text, p, n);
		text[n] = '\0';
	}
	if (dch_json_parse(jp, text) < 0 || jp->tok[0].kind != JSON_OBJECT)
	{
		dch_json_free(jp);
		return -1;
	}
	return 0;
}

/* cols/rows of a request: 80x24 for a missing (or useless) key. */
static void
rq_size(const struct json_parser *jp, int *cols, int *rows)
{
	long long v;

	*cols = 80;
	*rows = 24;
	if (jint(jp, 0, "cols", &v) == 1 && v > 0)
		*cols = v > 1000 ? 1000 : (int)v;
	if (jint(jp, 0, "rows", &v) == 1 && v > 0)
		*rows = v > 1000 ? 1000 : (int)v;
}

/* A session name dch can be handed as one argv word: UTF-8 with no '/' and
** no control logsafe would escape. The leading '-' is checked apart (it has
** its own spec message). */
static int
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

/* ---- listResp rows (pure) -------------------------------------------- */

static int
gh_seg(const char *s, size_t n, size_t max)
{
	size_t i;

	if (!n || n > max || (n == 1 && s[0] == '.') ||
	    (n == 2 && s[0] == '.' && s[1] == '.'))
		return 0;
	for (i = 0; i < n; i++)
		if (!isalnum((unsigned char)s[i]) && !strchr("-_.", s[i]))
			return 0;
	return 1;
}

/* url when it is exactly https://github.com/<owner>/<repo>/pull/<digits>,
** as Prosper allowlists it: "owner/repo" (cut to 100) into repo, the number
** into *num. 0, or -1 for any other url. */
static int
gh_pull(const char *url, char *repo, size_t rcap, long long *num)
{
	static const char pre[] = "https://github.com/";
	const char *o = url + sizeof pre - 1, *r, *pull, *d;
	size_t k;

	if (strncmp(url, pre, sizeof pre - 1) || !(r = strchr(o, '/')) ||
	    !gh_seg(o, (size_t)(r - o), 39) || !(pull = strchr(++r, '/')) ||
	    !gh_seg(r, (size_t)(pull - r), 100) || strncmp(pull, "/pull/", 6))
		return -1;
	d = pull + 6;
	k = strspn(d, "0123456789");
	if (!k || k > 9 || d[k])
		return -1;
	*num = strtoll(d, NULL, 10);
	k = (size_t)(pull - o);
	if (k > 100)
		k = 100;
	if (k >= rcap)
		return -1;
	memcpy(repo, o, k);
	repo[k] = '\0';
	return 0;
}

/* s cut to its first `max` code points. */
static void
cut_scalars(char *s, size_t max)
{
	size_t k = 0;

	for (; *s; s++)
		if (((unsigned char)*s & 0xc0) != 0x80 && k++ == max)
		{
			*s = '\0';
			return;
		}
}

/* `"key":` into an object being written from b->p[base] on, after a comma
** when a key is already there. */
static void
sb_key(struct sb *b, size_t base, const char *key)
{
	sb_s(b, b->n > base ? ",\"" : "\"");
	sb_s(b, key);
	sb_s(b, "\":");
}

/* A row's "meta" for the phone, re-encoded from the decoded fields (never
** dch's raw JSON): only allowlisted pull urls, repo and title cut, at most
** 50 considered, as Prosper's DchListRow.metaWire. Nothing when nothing
** survives; meta_epoch only beside a meta. */
static void
meta_wire(struct sb *b, size_t base, const struct json_parser *jp, int meta,
          long long ep)
{
	int prs = jcont(jp, meta, "prs", JSON_OBJECT), open, i, seen = 0;
	long long merged = -1, v;
	struct sb o = {0};

	if (prs < 0)
		return;
	if (jint(jp, prs, "merged", &v) == 1)
		merged = v < 0 ? 0 : v;
	open = jcont(jp, prs, "open", JSON_ARRAY);
	sb_s(&o, "[");
	for (i = open + 1; open >= 0 && i < jp->tok[open].next && seen < 50;
	     i = jp->tok[i].next, seen++)
	{
		char url[600], title[1024], repo[128];
		long long num;

		if (jp->tok[i].kind != JSON_OBJECT ||
		    jstr(jp, i, "url", url, sizeof url) != 1 ||
		    gh_pull(url, repo, sizeof repo, &num) < 0)
			continue;
		if (jstr(jp, i, "title", title, sizeof title) != 1)
			title[0] = '\0';
		cut_scalars(title, 200);
		sb_s(&o, o.n > 1 ? ",{\"num\":" : "{\"num\":");
		sb_ll(&o, num);
		sb_s(&o, ",\"repo\":");
		sb_q(&o, repo);
		sb_s(&o, ",\"title\":");
		sb_q(&o, title);
		sb_s(&o, ",\"url\":");
		sb_q(&o, url);
		sb_s(&o, "}");
	}
	sb_s(&o, "]");
	if (!o.bad && (o.n > 2 || merged >= 0))
	{
		sb_key(b, base, "meta");
		sb_s(b, "{\"prs\":{");
		if (merged >= 0)
		{
			sb_s(b, "\"merged\":");
			sb_ll(b, merged);
			sb_s(b, ",");
		}
		sb_s(b, "\"open\":");
		sb_add(b, o.p, o.n);
		sb_s(b, "}}");
		if (ep > 0)
		{
			sb_key(b, base, "meta_epoch");
			sb_ll(b, ep);
		}
	}
	free(o.p);
}

/* `dch --ls-json` as listResp's payload into *b: per row name; alias, else
** the harness name; state when one of the four; agents when > 0; meta. Keys
** sorted, so equal rows are equal bytes (the watch stream compares). Rows
** that would take the payload past the send limit are left out. 0, or -1
** when ls is not a JSON array. */
static int
rows_wire(const char *ls, struct sb *b)
{
	struct json_parser jp;
	int i;

	if (dch_json_parse(&jp, ls) < 0 || jp.tok[0].kind != JSON_ARRAY)
	{
		dch_json_free(&jp);
		return -1;
	}
	sb_s(b, "[");
	for (i = 1; i < jp.tok[0].next; i = jp.tok[i].next)
	{
		char name[1024], alias[2048], state[16];
		long long agents = 0, ep = 0;
		struct sb r = {0};
		size_t base;

		if (jp.tok[i].kind != JSON_OBJECT ||
		    jstr(&jp, i, "name", name, sizeof name) != 1 || !name[0])
			continue;
		if (jstr(&jp, i, "alias", alias, sizeof alias) != 1 ||
		    !alias[0])
			if (jstr(&jp, i, "harness", alias, sizeof alias) != 1)
				alias[0] = '\0';
		if (jstr(&jp, i, "state", state, sizeof state) != 1 ||
		    (strcmp(state, "working") && strcmp(state, "idle") &&
		     strcmp(state, "blocked") && strcmp(state, "done")))
			state[0] = '\0';
		if (jint(&jp, i, "agents", &agents) != 1)
			agents = 0;
		if (jint(&jp, i, "meta_epoch", &ep) != 1)
			ep = 0;
		sb_s(&r, b->n > 1 ? ",{" : "{");
		base = r.n;
		if (agents > 0)
		{
			sb_key(&r, base, "agents");
			sb_ll(&r, agents);
		}
		if (alias[0])
		{
			sb_key(&r, base, "alias");
			sb_q(&r, alias);
		}
		meta_wire(&r, base, &jp, jcont(&jp, i, "meta", JSON_OBJECT), ep);
		sb_key(&r, base, "name");
		sb_q(&r, name);
		if (state[0])
		{
			sb_key(&r, base, "state");
			sb_q(&r, state);
		}
		sb_s(&r, "}");
		if (!r.bad && b->n + r.n + 1 <= RF_SEND_MAX)
			sb_add(b, r.p, r.n);
		free(r.p);
	}
	sb_s(b, "]");
	dch_json_free(&jp);
	return b->bad ? -1 : 0;
}

/* ---- the peer gate (pure) -------------------------------------------- */

/* A tailcfg.StableNodeID: "963" on Headscale, "nAbCdEf1CNTRL" on
** tailscale.com. Short; anything longer is junk. */
#define NODE_ID_MAX 64

struct gate
{
	long long self_uid;	/* the host's owner; -1 on a tagged host */
	char self_node[NODE_ID_MAX];	/* the host's StableNodeID (Self.ID);
					** "" unknown */
	const char *allow[MAX_ALLOW];	/* --allow-user: id or login */
	int nallow;
	int whois_optional;
	int test_loopback;	/* test-only bypass, armed (see serve_main) */
};

/* Verdicts: G_OK and G_OK_NOWHOIS accept, every other one refuses. */
enum { G_OK, G_OK_NOWHOIS, G_RANGE, G_SELF, G_NOWHOIS, G_NOTNODE,
       G_BADWHOIS, G_TAGGED, G_OWNER };

static const char *
gate_why(int v)
{
	switch (v)
	{
	case G_OK:		return "same owner";
	case G_OK_NOWHOIS:	return "tailscaled not answering, accepted "
				       "(--whois-optional)";
	case G_RANGE:		return "not an IPv4 address in 100.64.0.0/10";
	case G_SELF:		return "this host itself (any local user could "
				       "connect from it)";
	case G_NOWHOIS:		return "tailscaled not answering (fail closed)";
	case G_NOTNODE:		return "tailscale whois failed while tailscale "
				       "status works (not a node it knows)";
	case G_BADWHOIS:	return "tailscale whois answer unusable";
	case G_TAGGED:		return "tagged node";
	default:		return "another user's node";
	}
}

static int
ip_cgnat(uint32_t ip)
{
	return (ip & 0xffc00000u) == 0x64400000u;	/* 100.64.0.0/10 */
}

static int
ip_loop(uint32_t ip)
{
	return ip >> 24 == 127;
}

/* An address one host can own: not 0/8, multicast, reserved or broadcast. */
static int
unicast(uint32_t ip)
{
	return ip >> 24 != 0 && ip >> 28 < 14;
}

static int
gate_addr(const struct gate *g, uint32_t ip)
{
	return ip_cgnat(ip) || (g->test_loopback && ip_loop(ip)) ? G_OK
	                                                         : G_RANGE;
}

static int
all_digits(const char *s)
{
	return *s && s[strspn(s, "0123456789")] == '\0';
}

/* Field `key` of `obj` as a StableNodeID (status Self.ID, whois
** Node.StableID) into out[NODE_ID_MAX]: a non-empty string of ASCII
** letters, digits, '-', '_' or '.'. 1, 0 when absent ("" in out), -1
** otherwise (a number, junk, too long). */
static int
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
static int
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

/* `tailscale whois --json`: UserProfile.ID into *uid, LoginName into login,
** Node.StableID into node[NODE_ID_MAX] ("" when absent), *tagged when the
** node has tags (or an odd Tags) or is the tagged-devices identity. 0, or
** -1 when the document is unusable: no single UserProfile with an integer
** ID, no single Node, a StableID that is not a node id. Node.ID (an int64
** on both control planes) is not used. */
static int
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

/* The decision for a peer, right after accept and before a byte is read.
** ip: its address. self: it is one of this host's own addresses (a local
** process can connect from those). whois: `tailscale whois --json`, NULL
** when it did not exit cleanly. ts_up: when whois is NULL, whether
** `tailscale status` still answers, so tailscaled is up and the peer is
** simply not a node it knows.
** A tagged node is refused, and so is this host's own node (whois
** Node.StableID equal to status Self.ID; when Self.ID is known, a whois
** with no StableID is unusable). Else the host's
** owner is accepted, compared by numeric user id only, and on a tagged host
** (no owner) each --allow-user: a number against UserProfile.ID exactly, a
** login against UserProfile.LoginName case-insensitively (ASCII). serve_main
** lets --allow-user only onto a tagged host, so the two never mix. Every
** doubt refuses; --whois-optional accepts only when tailscaled itself is not
** answering (whois and status both fail). */
static int
serve_gate(const struct gate *g, uint32_t ip, int self, const char *whois,
           int ts_up)
{
	char login[256], node[NODE_ID_MAX];
	long long uid;
	int tagged, i;

	if (gate_addr(g, ip) != G_OK)
		return G_RANGE;
	if (self)
		return G_SELF;
	if (!whois)
		return ts_up ? G_NOTNODE
		             : g->whois_optional ? G_OK_NOWHOIS : G_NOWHOIS;
	if (whois_user(whois, &uid, node, login, sizeof login, &tagged) < 0)
		return G_BADWHOIS;
	if (tagged)
		return G_TAGGED;
	if (g->self_node[0] && !node[0])
		return G_BADWHOIS;
	if (g->self_node[0] && !strcmp(node, g->self_node))
		return G_SELF;
	if (g->self_uid >= 0 && uid == g->self_uid)
		return G_OK;
	for (i = 0; i < g->nallow; i++)
		if (all_digits(g->allow[i])
		        ? strtoll(g->allow[i], NULL, 10) == uid
		        : login[0] && !strcasecmp(g->allow[i], login))
			return G_OK;
	return G_OWNER;
}

/* `tailscale status --json`: Self.UserID into *uid, Self.ID (the host's
** StableNodeID) into node[NODE_ID_MAX] ("" when absent), *tagged when Self
** has tags (or an odd Tags) or its user is the tagged-devices identity. 0,
** or -1 (also for a duplicate Self or a Self.ID that is not a node id). */
static int
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

/* ---- subprocesses ---------------------------------------------------- */

static int
cloexec(int fd)
{
	return fcntl(fd, F_SETFD, FD_CLOEXEC);
}

static int
nonblock(int fd)
{
	int fl = fcntl(fd, F_GETFL, 0);

	return fl < 0 ? -1 : fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

static int
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
static void
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

/* Run argv (argv[0] an absolute path; no shell) with stdin on /dev/null.
** Its stdout goes to *out (malloc'd, NUL-terminated, *outn bytes, at most
** cap) when out is set, else /dev/null; its stderr to err[errcap] when err
** is set, else /dev/null. Past timeout_ms or cap it is SIGKILLed and
** reaped. The wait status, or -1 when it could not run, timed out or
** overflowed. */
static int
run(char *const argv[], char **out, size_t *outn, size_t cap, char *err,
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
				r = on < cap ? read(po[0], ob + on, cap - on)
				             : read(po[0], junk, 1);
			else
				r = en < errcap - 1
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

/* ---- tailscale ------------------------------------------------------- */

static char g_ts[PATH_MAX];	/* the tailscale CLI; "" when not found */
static uint32_t g_self[MAX_SELF];	/* `tailscale ip -4`, and --bind */
static int g_nself;

/* `tailscale <a...>` exiting 0, stdout into *out (malloc'd): 0, or -1. */
static int
ts_run(char *a1, char *a2, char *a3, char *a4, char **out, size_t cap)
{
	char *av[] = { g_ts, a1, a2, a3, a4, NULL };
	int st;

	*out = NULL;
	if (!g_ts[0])
		return -1;
	st = run(av, out, NULL, cap, NULL, 0, TS_MS);
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
** decides every peer. */
static void
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

/* Whether tailscaled answers at all (`tailscale status` exits 0). */
static int
ts_up(void)
{
	char *out;
	int r = ts_run("status", "--json", "--peers=false", NULL, &out,
	               4u << 20);

	free(out);
	return r == 0;
}

/* Whether ip is in g_self (`tailscale ip -4`, and --bind once bound). */
static int
ip_tailnet(uint32_t ip)
{
	int k;

	for (k = 0; k < g_nself; k++)
		if (g_self[k] == ip)
			return 1;
	return 0;
}

/* Whether ip is one of this host's own addresses: its tailnet ones, the
** bound one, local (the accepted socket's own address, 0 for none), or any
** interface's. A process of any local user can connect from those, and
** whois would name this node, so the owner. */
static int
ip_self(uint32_t ip, uint32_t local)
{
	struct ifaddrs *ifa, *i;
	int hit = ip == local || ip_tailnet(ip);

	if (!hit && getifaddrs(&ifa) == 0)
	{
		for (i = ifa; i && !hit; i = i->ifa_next)
			hit = i->ifa_addr && i->ifa_addr->sa_family == AF_INET &&
			      ntohl(((struct sockaddr_in *)(void *)i->ifa_addr)
			            ->sin_addr.s_addr) == ip;
		freeifaddrs(ifa);
	}
	return hit;
}

/* The peer's whois document, NULL when whois cannot answer. */
static char *
ts_whois(const char *ipstr)
{
	char ip[INET_ADDRSTRLEN], *out;

	copy_str(ip, sizeof ip, ipstr);
	return ts_run("whois", "--json", ip, NULL, &out, 256 << 10) == 0 ? out
	                                                                : NULL;
}

/* ---- machine info (0x18) --------------------------------------------- */

static struct sb g_hello;	/* the 0x18 payload, built once */
static int g_allow_create;
static struct gate g_gate;
static int g_notify = -1;	/* handlers -> parent */

/* A stable id that is never an IP: SHA-256 of /etc/machine-id (salted, as
** systemd asks of an app-specific id), else of a random UUID kept in dch's
** state dir. */
static int
device_id(char out[65])
{
	char buf[128], dir[PATH_MAX], path[PATH_MAX + 16], salted[160];
	unsigned char r[16];
	ssize_t n = -1;
	int fd, k;

	if ((fd = open("/etc/machine-id", O_RDONLY | O_CLOEXEC)) >= 0)
	{
		n = read(fd, buf, sizeof buf - 1);
		close(fd);
	}
	if (n < 32)
	{
		char *sl;

		if (dch_history_dir(dir, sizeof dir, NULL, 1) < 0 ||
		    !(sl = strrchr(dir, '/')))
			return -1;
		*sl = '\0';	/* <state>/dch/sessions -> <state>/dch */
		snprintf(path, sizeof path, "%s/device-id", dir);
		n = -1;
		if ((fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)) >= 0)
		{
			n = read(fd, buf, sizeof buf - 1);
			close(fd);
		}
		if (n < 32)
		{
			if (dch_random_bytes(r, sizeof r) < 0)
				return -1;
			r[6] = (unsigned char)((r[6] & 0x0f) | 0x40);
			r[8] = (unsigned char)((r[8] & 0x3f) | 0x80);
			k = snprintf(buf, sizeof buf,
			             "%02x%02x%02x%02x-%02x%02x-%02x%02x-"
			             "%02x%02x-%02x%02x%02x%02x%02x%02x\n",
			             r[0], r[1], r[2], r[3], r[4], r[5], r[6],
			             r[7], r[8], r[9], r[10], r[11], r[12],
			             r[13], r[14], r[15]);
			fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC |
			          O_NOFOLLOW, 0600);
			if (fd >= 0)
			{
				n = write(fd, buf, (size_t)k);
				close(fd);
			}
			if (n != k)
				return -1;
		}
	}
	buf[n] = '\0';
	buf[strcspn(buf, " \t\r\n")] = '\0';
	if (strlen(buf) < 32)
		return -1;
	k = snprintf(salted, sizeof salted, "dch-serve:%s", buf);
	if (k < 0 || (size_t)k >= sizeof salted)
		return -1;
	dch_sha256_hex(salted, (size_t)k, out);
	return 0;
}

/* "Linux 6.8", "macOS 26.1". */
static void
os_label(char *out, size_t cap)
{
	struct utsname u;
	size_t k = 0;
	int dots = 0;

#ifdef __APPLE__
	{
		char v[64];
		size_t vn = sizeof v;

		if (sysctlbyname("kern.osproductversion", v, &vn, NULL, 0) == 0)
		{
			snprintf(out, cap, "macOS %.*s", (int)vn, v);
			return;
		}
	}
#endif
	if (uname(&u) < 0)
	{
		snprintf(out, cap, "unknown");
		return;
	}
	while (u.release[k] && (isdigit((unsigned char)u.release[k]) ||
	                        (u.release[k] == '.' && ++dots < 2)))
		k++;
	snprintf(out, cap, "%.64s %.*s", u.sysname, (int)k, u.release);
}

static int
build_hello(void)
{
	char id[65], host[256], os[160];

	if (device_id(id) < 0)
	{
		slog("cannot make a device id (no /etc/machine-id, and the "
		     "state dir is not writable)");
		return -1;
	}
	if (gethostname(host, sizeof host) < 0)
		copy_str(host, sizeof host, "localhost");
	host[sizeof host - 1] = '\0';
	os_label(os, sizeof os);
	sb_s(&g_hello, "{\"device_id\":");
	sb_q(&g_hello, id);
	sb_s(&g_hello, ",\"hostname\":");
	sb_q(&g_hello, host);
	sb_s(&g_hello, ",\"name\":");
	sb_q(&g_hello, host);
	sb_s(&g_hello, g_allow_create ? ",\"ops\":[\"create\",\"watch\"]"
	                              : ",\"ops\":[\"watch\"]");
	sb_s(&g_hello, ",\"os\":");
	sb_q(&g_hello, os);
	sb_s(&g_hello, ",\"proto\":\"1.0\",\"server\":\"dch\"}");
	return g_hello.bad ? -1 : 0;
}

/* ---- one connection -------------------------------------------------- */

struct conn
{
	int fd, pty;
	uint32_t ip;
	char ipstr[INET_ADDRSTRLEN];
	unsigned char in[RF_HDR + RF_CTRL_MAX];
	size_t inlen;
	struct rf_state rs;
	int parse_need;		/* the input holds no frame we can take now */
	struct sb out;
	size_t outoff;
	unsigned char pin[PIN_MAX];	/* client bytes for the pty */
	size_t pinlen;
	pid_t kid;		/* the dch client, once attached */
	int kid_status, kid_done, pty_eof;
	int attached, cols, rows;
	long long jiggle_at;
	char name[NAME_CAP + 1];	/* "" after a create dch named */
	int watching;
	char *last_list;
	long long next_poll, last_sent;
	int eof, closing, guard_close;
	long long last_frame, last_write, eof_at;
};

static struct conn C;	/* one connection per handler process */
static char rq_text[RF_CTRL_MAX + 3];

static size_t
pending(const struct conn *c)
{
	return c->out.n - c->outoff;
}

static int
out_frame(struct conn *c, unsigned int type, const void *p, size_t n)
{
	unsigned char h[RF_HDR];

	if (n > RF_SEND_MAX || sb_room(&c->out, RF_HDR + n) < 0)
	{
		c->closing = 1;	/* never a partial frame on the wire */
		return -1;
	}
	if (!pending(c))
		c->last_write = now_ms();
	h[0] = (unsigned char)type;
	h[1] = (unsigned char)(n >> 24);
	h[2] = (unsigned char)(n >> 16);
	h[3] = (unsigned char)(n >> 8);
	h[4] = (unsigned char)n;
	sb_add(&c->out, h, sizeof h);
	sb_add(&c->out, p, n);
	return 0;
}

static void
out_err(struct conn *c, const char *msg)
{
	struct sb b = {0};

	sb_s(&b, "{\"message\":");
	sb_q(&b, msg);
	sb_s(&b, "}");
	if (!b.bad)
		out_frame(c, 0x13, b.p, b.n);
	free(b.p);
}

/* Write what the socket takes now: 0, or -1 when the peer is gone. */
static int
out_flush(struct conn *c)
{
	while (pending(c))
	{
		ssize_t w = send(c->fd, c->out.p + c->outoff, pending(c),
		                 MSG_NOSIGNAL);

		if (w > 0)
		{
			c->outoff += (size_t)w;
			c->last_write = now_ms();
		}
		else if (w < 0 && errno == EINTR)
			continue;
		else if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
			break;
		else
			return -1;
	}
	if (c->outoff == c->out.n)
		c->out.n = c->outoff = 0;
	else if (c->outoff > (1u << 20))
	{
		memmove(c->out.p, c->out.p + c->outoff, pending(c));
		c->out.n -= c->outoff;
		c->outoff = 0;
	}
	return 0;
}

/* Tell the parent: 'A' attached to s, 'W' watching for device s. */
static void
notify(char kind, const char *s)
{
	char m[400];
	int k = snprintf(m, sizeof m, "%c %ld %.300s\n", kind, (long)getpid(),
	                 s);

	if (g_notify >= 0 && k > 0 && (size_t)k < sizeof m)
		(void)!write(g_notify, m, (size_t)k);
}

/* The listResp payload into *b from a fresh `dch --ls-json`: 0, or -1. */
static int
list_payload(struct sb *b)
{
	char *av[] = { dch_exe, "--ls-json", NULL }, *ls;
	int st = run(av, &ls, NULL, SB_LIMIT, NULL, 0, 10000);
	int rc = -1;

	if (st >= 0 && WIFEXITED(st) && WEXITSTATUS(st) == 0)
		rc = rows_wire(ls, b);
	if (st >= 0)
		free(ls);
	return rc;
}

/* Whether `name` is in the list 0x03 would answer: 1, 0, or -1 when the
** list is unavailable. */
static int
session_listed(const char *name)
{
	struct json_parser jp;
	struct sb b = {0};
	char got[NAME_CAP + 2];
	int i, hit = 0;

	if (list_payload(&b) < 0 || dch_json_parse(&jp, b.p) < 0)
	{
		free(b.p);
		return -1;
	}
	for (i = 1; i < jp.tok[0].next && !hit; i = jp.tok[i].next)
		hit = jstr(&jp, i, "name", got, sizeof got) == 1 &&
		      !strcmp(got, name);
	dch_json_free(&jp);
	free(b.p);
	return hit;
}

/* The name dch picked for a nameless create: its client's pidfile in the
** socket dir is <name>.sock.client.<pid>. 0, or -1 while it is not there. */
static int
created_name(struct conn *c)
{
	char tail[48];
	struct dirent *e;
	DIR *d;
	int k = snprintf(tail, sizeof tail, ".sock.client.%ld", (long)c->kid);

	if (k < 0 || (size_t)k >= sizeof tail || !(d = opendir(dch_sock_dir())))
		return -1;
	while ((e = readdir(d)))
	{
		size_t n = strlen(e->d_name);

		if (n > (size_t)k && n - (size_t)k <= NAME_CAP &&
		    !strcmp(e->d_name + n - (size_t)k, tail))
		{
			memcpy(c->name, e->d_name, n - (size_t)k);
			c->name[n - (size_t)k] = '\0';
			break;
		}
	}
	closedir(d);
	return c->name[0] ? 0 : -1;
}

static void
set_ws(struct conn *c, int rows)
{
	struct winsize ws;

	memset(&ws, 0, sizeof ws);
	ws.ws_col = (unsigned short)c->cols;
	ws.ws_row = (unsigned short)rows;
	if (c->pty >= 0)
		ioctl(c->pty, TIOCSWINSZ, &ws);
}

/* 0x01 attach / 0x02 create: a real dch client in a pty. */
static void
on_attach(struct conn *c, int create, const unsigned char *p, size_t n)
{
	struct json_parser jp;
	char name[NAME_CAP + 2], msg[200], *av[8 + MAX_CMD + 1], **cmd = NULL;
	int r, k = 0, ncmd = 0, t, m;
	struct winsize ws;
	pid_t pid;

	if (c->watching)
	{
		out_err(c, "watch connections cannot attach or create");
		return;
	}
	if (c->attached)
		return;		/* a second attach or create is ignored */
	if (create && !g_allow_create)
	{
		out_err(c, "creating sessions is off on this host "
		           "(dch serve --allow-create)");
		return;
	}
	if (rq_json(&jp, rq_text, p, n) < 0)
	{
		out_err(c, create ? "malformed create request"
		                  : "malformed attach request");
		return;
	}
	r = jstr(&jp, 0, "name", name, sizeof name);
	if (r < 0 || (!create && (r == 0 || !name[0])))
	{
		dch_json_free(&jp);
		out_err(c, "attach needs a session name");
		return;
	}
	if (r == 0)
		name[0] = '\0';
	if (name[0] == '-' || (name[0] && !name_ok(name)))
	{
		dch_json_free(&jp);
		out_err(c, name[0] == '-' ? DASH_MSG : "invalid session name");
		return;
	}
	/* `dch -n` attaches or creates. With create off, a phone must not
	** start a shell under a name of its choosing: attach only to a
	** session that is listed. ponytail: a session ending between this
	** check and the exec is started again as a shell; closing that needs
	** an attach-only dch flag. */
	if (!create && !g_allow_create && session_listed(name) != 1)
	{
		dch_json_free(&jp);
		snprintf(msg, sizeof msg, "no session named %.80s (this host "
		         "does not allow create)", name);
		out_err(c, msg);
		c->closing = 1;	/* the 0x13 is the last frame, as a failed spawn */
		return;
	}
	rq_size(&jp, &c->cols, &c->rows);
	if (create && (t = dch_json_field(&jp, 0, "command")) != -1)
	{
		if (t < 0 || jp.tok[t].kind != JSON_ARRAY ||
		    (jp.tok[t].next > t + 1 &&
		     !(cmd = dch_json_strv(rq_text, NULL, "command", &ncmd))) ||
		    ncmd > MAX_CMD)
		{
			free(cmd);
			dch_json_free(&jp);
			out_err(c, "malformed create request: command must be "
			           "an array of strings");
			return;
		}
	}
	dch_json_free(&jp);
	av[k++] = dch_exe;
	av[k++] = "-E";
	if (!create)
		av[k++] = "-f";
	if (name[0])
	{
		av[k++] = "-n";
		av[k++] = name;
	}
	if (ncmd)
	{
		av[k++] = "--";
		for (t = 0; t < ncmd; t++)
			av[k++] = cmd[t];
	}
	av[k] = NULL;
	memset(&ws, 0, sizeof ws);
	ws.ws_col = (unsigned short)c->cols;
	ws.ws_row = (unsigned short)c->rows;
	pid = forkpty(&m, NULL, NULL, &ws);
	if (pid == 0)
	{
		const char *home = getenv("HOME");

		exec_prep();
		setenv("TERM", "xterm-256color", 1);
		if (!home || chdir(home) < 0)
			(void)!chdir("/");
		execv(av[0], av);
		_exit(127);
	}
	free(cmd);
	if (pid < 0)
	{
		snprintf(msg, sizeof msg, "Could not start the session: %s",
		         strerror(errno));
		slog("%s: %s", c->ipstr, msg);
		out_err(c, msg);
		c->closing = 1;	/* the 0x13 is the last frame */
		return;
	}
	cloexec(m);
	nonblock(m);
	c->pty = m;
	c->kid = pid;
	c->attached = 1;
	copy_str(c->name, sizeof c->name, name);
	slog("%s: %s %s (%dx%d)", c->ipstr, create ? "create" : "attach",
	     name[0] ? name : "(dch names it)", c->cols, c->rows);
	if (name[0])
		notify('A', name);
}

/* 0x09: dch's VT mirror (what `dch --read --ansi` prints) plus the caret as
** ESC[row;colH. Empty when unattached or with no mirror (a lite build); never
** an error. */
static void
on_snapshot(struct conn *c)
{
	char err[256], *scr = NULL, *cur;
	char *av[] = { dch_exe, "--read", c->name, "--ansi", "--cursor", NULL };
	struct sb b = {0};
	size_t sn = 0;
	long row, col;
	int st;

	if (c->attached && !c->name[0])
		(void)created_name(c);
	if (!c->attached || !c->name[0] ||
	    (st = run(av, &scr, &sn, 4u << 20, err, sizeof err, 5000)) < 0)
	{
		out_frame(c, 0x19, "", 0);
		return;
	}
	if (WIFEXITED(st) && WEXITSTATUS(st) == 0 && sn)
	{
		sb_add(&b, scr, sn);
		if ((cur = strstr(err, "cursor ")) &&
		    sscanf(cur, "cursor %ld %ld", &row, &col) == 2 && row > 0 &&
		    col > 0 && row < 100000 && col < 100000)
		{
			char cup[32];
			int k = snprintf(cup, sizeof cup, "\033[%ld;%ldH", row,
			                 col);

			if (k > 0 && (size_t)k < sizeof cup)
				sb_add(&b, cup, (size_t)k);
		}
	}
	if (b.bad || b.n > RF_SEND_MAX)
		b.n = 0;	/* too big to send: say "no mirror" */
	out_frame(c, 0x19, b.n ? b.p : "", b.n);
	free(scr);
	free(b.p);
}

/* 0x04 kill, 0x06 rename: always an empty 0x14 (dch's result is not
** reported), but for a name starting with '-'. */
static void
on_kill_rename(struct conn *c, int kill_, const unsigned char *p, size_t n)
{
	struct json_parser jp;
	char name[NAME_CAP + 2], alias[1024];
	int r = -1, ra = 0;

	if (rq_json(&jp, rq_text, p, n) == 0)
	{
		r = jstr(&jp, 0, "name", name, sizeof name);
		if (!kill_)
			ra = jstr(&jp, 0, "alias", alias, sizeof alias);
		dch_json_free(&jp);
	}
	if (r == 1 && name[0] == '-')
	{
		out_err(c, DASH_MSG);
		return;
	}
	if (r == 1 && name_ok(name) && ra >= 0)
	{
		char *kv[] = { dch_exe, "-k", name, NULL };
		char *mv[] = { dch_exe, "-m", name, alias, NULL };

		if (ra == 0)
			alias[0] = '\0';
		slog("%s: %s %s", c->ipstr, kill_ ? "kill" : "rename", name);
		run(kill_ ? kv : mv, NULL, NULL, 0, NULL, 0, 15000);
	}
	out_frame(c, 0x14, "", 0);
}

static void
on_hello(struct conn *c, const unsigned char *p, size_t n)
{
	struct json_parser jp;
	char pv[32], msg[96];

	if (n && rq_json(&jp, rq_text, p, n) == 0)
	{
		int r = jstr(&jp, 0, "proto", pv, sizeof pv);

		dch_json_free(&jp);
		if (r == 1 && strtol(pv, NULL, 10) > 1)
		{
			snprintf(msg, sizeof msg, "protocol %.20s is newer than "
			         "this server's 1.0", pv);
			out_err(c, msg);
			return;
		}
	}
	out_frame(c, 0x18, g_hello.p, g_hello.n);
}

static void
on_list(struct conn *c)
{
	struct sb b = {0};

	if (list_payload(&b) < 0)
	{
		slog("%s: dch --ls-json failed; answering []", c->ipstr);
		free(b.p);
		out_frame(c, 0x11, "[]", 2);
		return;
	}
	out_frame(c, 0x11, b.p, b.n);
	free(b.p);
}

static void
on_watch(struct conn *c, const unsigned char *p, size_t n)
{
	struct json_parser jp;
	char dev[DEV_CAP + 1], *q;
	struct sb b = {0};
	int r;

	if (rq_json(&jp, rq_text, p, n) < 0)
	{
		out_err(c, "malformed watch request: send {}");
		return;
	}
	r = jstr(&jp, 0, "device_id", dev, sizeof dev);
	dch_json_free(&jp);
	if (c->attached)
	{
		out_err(c, "watch needs its own connection");
		return;
	}
	if (c->watching)
		return;		/* a second 0x2e is ignored */
	if (list_payload(&b) < 0)
	{
		free(b.p);
		out_err(c, "the session list is unavailable");
		return;
	}
	out_frame(c, 0x11, b.p, b.n);
	c->last_list = b.p;
	c->watching = 1;
	c->last_sent = now_ms();
	c->next_poll = c->last_sent + WATCH_POLL_MS;
	if (r != 1 || !dev[0])
		copy_str(dev, sizeof dev, c->ipstr);
	for (q = dev; *q; q++)
		if ((unsigned char)*q <= ' ' || *q == 0x7f)
			*q = '_';
	slog("%s: watch (device %s)", c->ipstr, dev);
	notify('W', dev);
}

/* The stream after its snapshot: a 0x11 when the list changed, polled
** every 2 s (so at most one per 2 s), a 0x14 {} heartbeat after 25 s of
** nothing. Nothing new while the client has not taken the last frame. */
static void
watch_tick(struct conn *c, long long now)
{
	struct sb b = {0};

	if (pending(c))
		return;
	if (now >= c->next_poll)
	{
		c->next_poll = now + WATCH_POLL_MS;
		if (list_payload(&b) == 0 && strcmp(b.p, c->last_list))
		{
			out_frame(c, 0x11, b.p, b.n);
			free(c->last_list);
			c->last_list = b.p;
			c->last_sent = now;
			return;
		}
		free(b.p);
	}
	if (now - c->last_sent >= WATCH_BEAT_MS)
	{
		out_frame(c, 0x14, "{}", 2);
		c->last_sent = now;
	}
}

static void
on_frame(struct conn *c, unsigned int type, const unsigned char *p,
         size_t n)
{
	struct json_parser jp;

	switch (type)
	{
	case 0x08:
		on_hello(c, p, n);
		break;
	case 0x03:
		on_list(c);
		break;
	case 0x2e:
		on_watch(c, p, n);
		break;
	case 0x01:
	case 0x02:
		on_attach(c, type == 0x02, p, n);
		break;
	case 0x04:
	case 0x06:
		on_kill_rename(c, type == 0x04, p, n);
		break;
	case 0x09:
		on_snapshot(c);
		break;
	case 0x05:		/* resize: no reply */
		if (c->pty >= 0 && rq_json(&jp, rq_text, p, n) == 0)
		{
			rq_size(&jp, &c->cols, &c->rows);
			dch_json_free(&jp);
			set_ws(c, c->rows);
		}
		break;
	case 0x07:
		/* redraw: rows-1, then back 120 ms later, as Prosper does. A
		** same-size SIGWINCH is not enough: Node/Ink apps repaint only
		** on a real size change. */
		if (c->pty >= 0 && !c->jiggle_at && c->rows > 2)
		{
			set_ws(c, c->rows - 1);
			c->jiggle_at = now_ms() + 120;
		}
		break;
	default:
		break;
	}
}

/* Take every frame the input holds, while the output is not backed up. */
static void
parse_input(struct conn *c)
{
	size_t off = 0, used;
	struct rf_frame f;

	c->parse_need = 0;
	while (!c->closing && pending(c) <= OUT_HIGH)
	{
		size_t room = c->pty >= 0 ? sizeof c->pin - c->pinlen
		                          : (size_t)-1;
		int r = rf_parse(&c->rs, c->in + off, c->inlen - off, room, &f,
		                 &used);

		if (r == RF_CLOSE)
		{
			slog("%s: frame length over the guard; closing",
			     c->ipstr);
			out_err(c, "frame too large");
			c->closing = c->guard_close = 1;
			break;
		}
		if (r == RF_NEED)
		{
			c->parse_need = 1;
			break;
		}
		off += used;
		if (f.end)
			c->last_frame = now_ms();
		if (r == RF_FRAME)
			on_frame(c, f.type, f.data, f.len);
		else if (f.type == 0x10 && c->pty >= 0 && f.len)
		{
			/* 0x10 with no session is dropped */
			memcpy(c->pin + c->pinlen, f.data, f.len);
			c->pinlen += f.len;
		}
	}
	memmove(c->in, c->in + off, c->inlen - off);
	c->inlen -= off;
}

/* SIGHUP the dch client (its own detach: the session lives), SIGKILL after
** 300 ms: one stuck writing to an undrained pty ignores SIGHUP. */
static void
kid_end(struct conn *c)
{
	long long end;
	pid_t w;

	if (c->kid <= 0 || c->kid_done)
		return;
	kill(c->kid, SIGHUP);
	end = now_ms() + 300;
	while ((w = waitpid(c->kid, &c->kid_status, WNOHANG)) == 0 ||
	       (w < 0 && errno == EINTR))
	{
		if (w == 0 && now_ms() >= end)
		{
			kill(c->kid, SIGKILL);
			while (waitpid(c->kid, &c->kid_status, 0) < 0 &&
			       errno == EINTR)
				;
			break;
		}
		poll(NULL, 0, 10);
	}
	c->kid_done = 1;
}

static void
pty_read(struct conn *c, size_t budget)
{
	unsigned char buf[PTY_CHUNK];

	while (c->pty >= 0 && !c->pty_eof && budget)
	{
		ssize_t r = read(c->pty, buf, sizeof buf);

		if (r > 0)
		{
			out_frame(c, 0x10, buf, (size_t)r);
			budget = (size_t)r < budget ? budget - (size_t)r : 0;
		}
		else if (r < 0 && errno == EINTR)
			continue;
		else
		{
			if (r == 0 || (errno != EAGAIN && errno != EWOULDBLOCK))
				c->pty_eof = 1;	/* EIO: the client is gone */
			break;
		}
	}
}

/* Close: the dch client first, then the socket. A graceful close sends what
** is queued and the FIN, and reads off what the client still sends for a
** moment, so the close is a FIN and not a reset that could eat the reply. */
static void
conn_end(struct conn *c, int graceful)
{
	kid_end(c);
	if (c->pty >= 0)
		close(c->pty);
	c->pty = -1;
	if (graceful)
	{
		shutdown(c->fd, SHUT_WR);
		if (!c->eof && !c->guard_close)
		{
			long long end = now_ms() + LINGER_MS;
			char junk[4096];
			struct pollfd pf;

			pf.fd = c->fd;
			pf.events = POLLIN;
			while (now_ms() < end && !g_stop &&
			       poll(&pf, 1, (int)(end - now_ms())) > 0 &&
			       read(c->fd, junk, sizeof junk) > 0)
				;
		}
	}
	close(c->fd);
	free(c->last_list);
	free(c->out.p);
}

/* One connection, in the forked handler. Returns the handler's exit
** status: 3 when the gate refused the peer on tailscaled's own word (the
** parent then refuses that address for NEG_MS without forking), else 0. */
static int
serve_conn(int fd, uint32_t ip)
{
	struct conn *c = &C;
	struct in_addr a;
	char *whois = NULL;
	int v, graceful = 0;

	c->fd = fd;
	c->pty = -1;
	c->ip = ip;
	a.s_addr = htonl(ip);
	inet_ntop(AF_INET, &a, c->ipstr, sizeof c->ipstr);
	/* The gate, before a byte is read. A refused peer gets no frame.
	** The parent already refused this host's own addresses. */
	whois = ts_whois(c->ipstr);
	v = serve_gate(&g_gate, ip, 0, whois, !whois && ts_up());
	free(whois);
	if (v >= G_RANGE)
	{
		slog("%s: refused: %s", c->ipstr, gate_why(v));
		close(fd);
		return v == G_NOWHOIS ? 0 : 3;
	}
	if (v == G_OK_NOWHOIS)
		slog("%s: warning: %s", c->ipstr, gate_why(v));
	if (ip_loop(ip))
		slog("%s: WARNING: loopback peer let in by the test-only "
		     "bypass", c->ipstr);
	nonblock(fd);
	c->last_frame = c->last_write = now_ms();
	for (;;)
	{
		struct pollfd pf[2];
		int np = 0, si, pi = -1, tmo = 500;
		long long now = now_ms();

		if (g_stop)
			break;	/* retired, or the server stops: no frame */
		if (c->jiggle_at && now >= c->jiggle_at)
		{
			c->jiggle_at = 0;
			set_ws(c, c->rows);
		}
		if (c->watching && !c->closing)
			watch_tick(c, now);
		if (!c->closing)
			parse_input(c);
		if (c->kid > 0 && !c->kid_done &&
		    waitpid(c->kid, &c->kid_status, WNOHANG) == c->kid)
			c->kid_done = 1;
		if (c->kid_done && c->attached && !c->closing)
		{
			/* the session's client exited: its last output, then
			** 0x12 and the FIN */
			struct sb b = {0};
			int st = c->kid_status;

			pty_read(c, 1u << 20);
			sb_s(&b, "{\"code\":");
			sb_ll(&b, WIFEXITED(st) ? WEXITSTATUS(st)
			                        : 128 + WTERMSIG(st));
			sb_s(&b, "}");
			out_frame(c, 0x12, b.p, b.n);
			free(b.p);
			slog("%s: %s exited", c->ipstr,
			     c->name[0] ? c->name : "session");
			c->closing = 1;
		}
		/* The client's FIN: answer every frame it sent first. */
		if (c->eof && !c->closing && c->parse_need &&
		    (!c->pinlen || now - c->eof_at > LINGER_MS))
			c->closing = 1;
		if (out_flush(c) < 0)
			break;		/* the peer is gone */
		if (c->closing && !pending(c))
		{
			graceful = 1;
			break;
		}
		if (pending(c) && now - c->last_write >= STALL_MS)
		{
			slog("%s: output not taken for %d s; closing",
			     c->ipstr, STALL_MS / 1000);
			break;
		}
		if (!c->attached && !c->watching && !c->closing &&
		    now - c->last_frame >= IDLE_MS)
		{
			slog("%s: idle; closing", c->ipstr);
			break;
		}
		si = np;
		pf[np].fd = fd;
		pf[np].events = (short)((!c->eof && !c->closing &&
		                         c->inlen < sizeof c->in ? POLLIN : 0) |
		                        (pending(c) ? POLLOUT : 0));
		pf[np++].revents = 0;
		if (c->pty >= 0 && !c->pty_eof && !c->closing)
		{
			pi = np;
			pf[np].fd = c->pty;
			pf[np].events =
			    (short)((pending(c) <= OUT_HIGH ? POLLIN : 0) |
			            (c->pinlen ? POLLOUT : 0));
			pf[np++].revents = 0;
		}
		if (c->kid > 0 && !c->kid_done)
			tmo = 100;
		if (c->jiggle_at && c->jiggle_at - now < tmo)
			tmo = (int)(c->jiggle_at - now) + 1;
		if (c->eof && !c->closing)
			tmo = 50;
		if (poll(pf, (nfds_t)np, tmo) < 0)
		{
			if (errno == EINTR)
				continue;
			break;
		}
		if (pf[si].revents & (POLLIN | POLLHUP | POLLERR) &&
		    pf[si].events & POLLIN)
		{
			ssize_t r = read(fd, c->in + c->inlen,
			                 sizeof c->in - c->inlen);

			if (r > 0)
				c->inlen += (size_t)r;
			else if (r == 0)
			{
				c->eof = 1;
				c->eof_at = now_ms();
			}
			else if (errno != EAGAIN && errno != EWOULDBLOCK &&
			         errno != EINTR)
				break;	/* reset */
		}
		if (pi >= 0 && pf[pi].revents & POLLOUT && c->pinlen)
		{
			ssize_t w = write(c->pty, c->pin, c->pinlen);

			if (w > 0)
			{
				memmove(c->pin, c->pin + w, c->pinlen - (size_t)w);
				c->pinlen -= (size_t)w;
			}
		}
		if (pi >= 0 && pf[pi].revents & (POLLIN | POLLHUP | POLLERR))
			pty_read(c, PTY_CHUNK);
	}
	if (c->eof && !c->closing)
		slog("%s: closed by the client", c->ipstr);
	conn_end(c, graceful);
	return 0;
}

/* ---- the parent ------------------------------------------------------ */

struct kid
{
	pid_t pid;
	uint32_t ip;
	long long seq;
	char name[NAME_CAP + 1];	/* attached to */
	char dev[DEV_CAP + 1];		/* watching for */
};

static struct kid kids[MAX_CONN];
static int nkids;
static long long kid_seq;

/* Peers a handler refused on tailscaled's word (exit status 3), refused
** again before fork until `until`: no fork, no whois, no kid slot. Full,
** the entry that expires first goes. An accepted peer never lands here,
** so neither does the owner. */
static struct neg
{
	uint32_t ip;
	long long until;
	int told;	/* its first cached refusal is logged, no more */
} neg[NEG_N];

static void
neg_add(uint32_t ip)
{
	struct neg *n = &neg[0];
	int i;

	for (i = 0; i < NEG_N; i++)
		if (neg[i].ip == ip)
		{
			n = &neg[i];
			break;
		}
		else if (neg[i].until < n->until)
			n = &neg[i];
	n->ip = ip;
	n->until = now_ms() + NEG_MS;
	n->told = 0;
}

static struct neg *
neg_hit(uint32_t ip)
{
	long long now = now_ms();
	int i;

	for (i = 0; i < NEG_N; i++)
		if (neg[i].ip == ip && neg[i].until > now)
			return &neg[i];
	return NULL;
}

static void
on_signal(int sig)
{
	if (sig != SIGCHLD)
		g_stop = 1;
}

static void
kids_reap(void)
{
	pid_t p;
	int st, i;

	while ((p = waitpid(-1, &st, WNOHANG)) > 0)
		for (i = 0; i < nkids; i++)
			if (kids[i].pid == p)
			{
				if (WIFEXITED(st) && WEXITSTATUS(st) == 3)
					neg_add(kids[i].ip);
				kids[i] = kids[--nkids];
				break;
			}
}

/* "A <pid> <name>": the newest attach to a session retires the older
** connections attached to it. "W <pid> <dev>": a device keeps at most 4
** watch streams, the oldest goes. Only ever our own children. */
static void
kids_note(const char *line)
{
	struct kid *k = NULL, *old;
	char *end;
	long pid = strtol(line + 1, &end, 10);
	int i, n;

	for (i = 0; i < nkids; i++)
		if (kids[i].pid == (pid_t)pid)
			k = &kids[i];
	if (!k || *end != ' ' || (line[0] != 'A' && line[0] != 'W'))
		return;
	if (line[0] == 'A')
	{
		copy_str(k->name, sizeof k->name, end + 1);
		for (i = 0; i < nkids; i++)
			if (&kids[i] != k && !strcmp(kids[i].name, k->name))
			{
				slog("retiring pid %ld on %s: a newer attach "
				     "took it", (long)kids[i].pid, k->name);
				kill(kids[i].pid, SIGTERM);
				kids[i].name[0] = '\0';
			}
		return;
	}
	copy_str(k->dev, sizeof k->dev, end + 1);
	for (;;)
	{
		for (old = NULL, n = 0, i = 0; i < nkids; i++)
			if (!strcmp(kids[i].dev, k->dev))
			{
				n++;
				if (&kids[i] != k && (!old || kids[i].seq < old->seq))
					old = &kids[i];
			}
		if (n <= MAX_WATCH_PER_DEV || !old)
			break;
		slog("closing the oldest watch of device %s", k->dev);
		kill(old->pid, SIGTERM);
		old->dev[0] = '\0';
	}
}

static void
kids_read(int fd)
{
	static char buf[4096];
	static size_t len;
	ssize_t r;
	char *nl;

	while ((r = read(fd, buf + len, sizeof buf - 1 - len)) > 0)
	{
		len += (size_t)r;
		buf[len] = '\0';
		while ((nl = strchr(buf, '\n')))
		{
			*nl = '\0';
			kids_note(buf);
			len -= (size_t)(nl + 1 - buf);
			memmove(buf, nl + 1, len + 1);
		}
		if (len == sizeof buf - 1)
			len = 0;	/* no newline in 4 KiB: drop it */
	}
}

static void
accept_one(int lfd, int nr)
{
	struct sockaddr_in sa;
	socklen_t sl = sizeof sa;
	char ipstr[INET_ADDRSTRLEN];
	uint32_t ip, local = 0;
	int fd, i, same = 0;
	struct neg *n;
	pid_t pid;

	fd = accept(lfd, (struct sockaddr *)&sa, &sl);
	if (fd < 0)
		return;
	cloexec(fd);
	if (sa.sin_family != AF_INET)
	{
		close(fd);
		return;
	}
	ip = ntohl(sa.sin_addr.s_addr);
	inet_ntop(AF_INET, &sa.sin_addr, ipstr, sizeof ipstr);
	/* The range check costs no fork and no kid slot; the handler runs
	** the whole gate (whois) again before it reads a byte. */
	if (gate_addr(&g_gate, ip) != G_OK)
	{
		slog("%s: refused: %s", ipstr, gate_why(G_RANGE));
		close(fd);
		return;
	}
	/* This host's own addresses: any local user's process. Loopback
	** under the armed test bypass is its own path (it is never a
	** tailnet peer). */
	sl = sizeof sa;
	if (getsockname(fd, (struct sockaddr *)&sa, &sl) == 0 &&
	    sa.sin_family == AF_INET)
		local = ntohl(sa.sin_addr.s_addr);
	if (!(g_gate.test_loopback && ip_loop(ip)) && ip_self(ip, local))
	{
		slog("%s: refused: %s", ipstr, gate_why(G_SELF));
		close(fd);
		return;
	}
	if ((n = neg_hit(ip)))
	{
		if (!n->told++)
			slog("%s: refused before fork for %d s: refused by the "
			     "gate already", ipstr, NEG_MS / 1000);
		close(fd);
		return;
	}
	for (i = 0; i < nkids; i++)
		same += kids[i].ip == ip;
	if (nkids >= MAX_CONN || same >= MAX_PER_PEER)
	{
		slog("%s: refused: %s", ipstr, nkids >= MAX_CONN
		     ? "too many connections" : "too many connections from it");
		close(fd);
		return;
	}
	pid = fork();
	if (pid == 0)
	{
		close(lfd);
		close(nr);
		_exit(serve_conn(fd, ip));
	}
	close(fd);
	if (pid < 0)
	{
		slog("fork: %s", strerror(errno));
		return;
	}
	kids[nkids].pid = pid;
	kids[nkids].ip = ip;
	kids[nkids].seq = ++kid_seq;
	kids[nkids].name[0] = kids[nkids].dev[0] = '\0';
	nkids++;
}

/* ---- startup --------------------------------------------------------- */

static void
serve_usage(FILE *f)
{
	fprintf(f,
	    "Usage: dch serve [--bind <ipv4>] [--port <n>] [--allow-create]\n"
	    "                 [--allow-user <login|id>]... [--whois-optional]\n"
	    "       dch serve --check-peer <ipv4> [flags as above]\n"
	    "Serve this host's dch sessions to the Prosper iPhone app\n"
	    "(remote frames 1.0, TCP port 8771), in the foreground.\n"
	    "Only peers on your tailnet that you own get in.\n"
	    "  --bind <ipv4>       listen there, not on this host's Tailscale\n"
	    "                      IPv4 (warns when it is not a tailnet one)\n"
	    "  --port <n>          another port, 0 for any (tests; the phone\n"
	    "                      dials 8771)\n"
	    "  --allow-create      let the phone start sessions with its own\n"
	    "                      command (0x02)\n"
	    "  --allow-user <u>    on a tagged host (no owner), accept this\n"
	    "                      user's nodes: a numeric Tailscale user id or\n"
	    "                      a login; required there, refused on an\n"
	    "                      untagged host (repeatable)\n"
	    "  --whois-optional    accept a tailnet peer while tailscaled is\n"
	    "                      not answering (whois and status both fail;\n"
	    "                      default: refuse it); the owner must still\n"
	    "                      be known at startup\n"
	    "  --check-peer <ip>   print whether a peer would get in, and exit\n");
}

/* This host's tailnet addresses, `tailscale ip -4`, into g_self. */
static void
ts_ips(void)
{
	char *out, *line, *save = NULL;
	struct in_addr a;

	if (ts_run("ip", "-4", NULL, NULL, &out, 4096) < 0)
		return;
	for (line = strtok_r(out, "\r\n", &save); line && g_nself < MAX_SELF;
	     line = strtok_r(NULL, "\r\n", &save))
		if (inet_pton(AF_INET, line, &a) == 1)
			g_self[g_nself++] = ntohl(a.s_addr);
	free(out);
}

static int
want_arg(int argc, char **argv, int i)
{
	if (i + 1 < argc)
		return 1;
	fprintf(stderr, "dch serve: %s needs a value\n", argv[i]);
	return 0;
}

int
serve_main(int argc, char **argv)
{
	const char *bind_s = NULL, *check = NULL, *e;
	struct sockaddr_in sa;
	socklen_t sl;
	struct sigaction act;
	struct in_addr addr = { 0 };
	char ipstr[INET_ADDRSTRLEN], *status;
	long port = SV_PORT;
	int i, lfd, one = 1, np[2], tagged = 0, fd, test_lo;

	memset(&g_gate, 0, sizeof g_gate);
	for (i = 0; i < argc; i++)
	{
		const char *a = argv[i];

		if (!strcmp(a, "-h") || !strcmp(a, "--help"))
		{
			serve_usage(stdout);
			return 0;
		}
		else if (!strcmp(a, "--allow-create"))
			g_allow_create = 1;
		else if (!strcmp(a, "--whois-optional"))
			g_gate.whois_optional = 1;
		else if (!strcmp(a, "--bind") && want_arg(argc, argv, i))
			bind_s = argv[++i];
		else if (!strcmp(a, "--check-peer") && want_arg(argc, argv, i))
			check = argv[++i];
		else if (!strcmp(a, "--port") && want_arg(argc, argv, i))
		{
			char *end;

			port = strtol(argv[++i], &end, 10);
			if (*end || port < 0 || port > 65535)
			{
				fprintf(stderr, "dch serve: bad port %s\n",
				        argv[i]);
				return 2;
			}
		}
		else if (!strcmp(a, "--allow-user") && want_arg(argc, argv, i))
		{
			if (!argv[++i][0] || g_gate.nallow == MAX_ALLOW)
			{
				fprintf(stderr, "dch serve: --allow-user takes "
				        "a login or an id, at most %d\n",
				        MAX_ALLOW);
				return 2;
			}
			g_gate.allow[g_gate.nallow++] = argv[i];
		}
		else
		{
			if (strncmp(a, "--", 2) || i + 1 >= argc ||
			    (strcmp(a, "--bind") && strcmp(a, "--port") &&
			     strcmp(a, "--check-peer") &&
			     strcmp(a, "--allow-user")))
				fprintf(stderr, "dch serve: unknown option "
				        "%s\n", a);
			serve_usage(stderr);
			return 2;
		}
	}

	/* fds 0-2 stay taken, so no pipe or socket lands on them */
	while ((fd = open("/dev/null", O_RDWR)) >= 0 && fd <= 2)
		;
	if (fd > 2)
		close(fd);
	memset(&act, 0, sizeof act);
	act.sa_handler = SIG_IGN;
	sigaction(SIGPIPE, &act, NULL);
	act.sa_handler = on_signal;	/* no SA_RESTART: poll wakes */
	sigemptyset(&act.sa_mask);
	sigaction(SIGINT, &act, NULL);
	sigaction(SIGTERM, &act, NULL);
	sigaction(SIGHUP, &act, NULL);
	sigaction(SIGCHLD, &act, NULL);
	/* Run from inside a dch session, the clients we spawn would refuse
	** to nest. */
	unsetenv("DCH_SESSION");
	if (!dch_exe || dch_exe[0] != '/')
	{
		fprintf(stderr, "dch serve: cannot find dch's own path; run it "
		        "by its full path\n");
		return 1;
	}

	/* The test-only loopback bypass, read once and gone from the
	** environment, so no child (dch, a session's shell) inherits it. */
	e = getenv("DCH_SERVE_TEST_LOOPBACK");
	test_lo = e && !strcmp(e, "1");
	unsetenv("DCH_SERVE_TEST_LOOPBACK");

	/* The tailscale CLI, absolute, for execv. */
	ts_find();
	if (g_ts[0])
		slog("tailscale CLI: %s", g_ts);

	/* The host's owner and node, cached for every peer. */
	g_gate.self_uid = -1;
	if (ts_run("status", "--json", "--peers=false", NULL, &status,
	           4u << 20) == 0 &&
	    status_self(status, &g_gate.self_uid, g_gate.self_node,
	                &tagged) == 0)
	{
		if (tagged)
			g_gate.self_uid = -1;
	}
	else
	{
		/* No owner known, nobody to serve. --whois-optional only
		** relaxes one peer's failed whois, never this. */
		fprintf(stderr, "dch serve: %s, so this host's owner is unknown "
		        "and no peer could be checked. Start tailscale and log "
		        "in (or put its CLI on PATH).\n",
		        !g_ts[0] ? "the tailscale CLI is not on PATH (nor in "
		                   "/usr/bin, /usr/local/bin, /opt/homebrew/bin "
		                   "or /Applications/Tailscale.app)"
		        : status ? "tailscale status --json is not a document "
		                   "with one Self"
		                 : "tailscale status --json failed");
		free(status);
		return 1;
	}
	if (g_gate.nallow && !tagged)
	{
		fprintf(stderr, "dch serve: --allow-user is only for tagged "
		        "hosts; an untagged host serves its owner\n");
		free(status);
		return 1;
	}
	if (tagged && !g_gate.nallow)
	{
		fprintf(stderr, "dch serve: this host is a tagged node, so no "
		        "user owns it. Name who may connect with --allow-user "
		        "<login or numeric user id>.\n");
		free(status);
		return 1;
	}
	free(status);

	/* The test-only loopback bypass: the env var AND an explicit loopback
	** --bind, for loopback peers only, never by accident. */
	if (test_lo)
	{
		if (bind_s && inet_pton(AF_INET, bind_s, &addr) == 1 &&
		    ip_loop(ntohl(addr.s_addr)))
		{
			g_gate.test_loopback = 1;
			slog("WARNING: DCH_SERVE_TEST_LOOPBACK=1: loopback peers "
			     "skip the 100.64.0.0/10 check. Test-only: never "
			     "set it on a real host.");
		}
		else
			slog("warning: DCH_SERVE_TEST_LOOPBACK ignored: it "
			     "needs a loopback --bind");
	}

	/* The address: --bind, else the first of `tailscale ip -4`. Never a
	** wildcard, and never an interface scan. */
	ts_ips();
	if (bind_s)
	{
		uint32_t b;

		if (inet_pton(AF_INET, bind_s, &addr) != 1)
		{
			fprintf(stderr, "dch serve: --bind %s is not an IPv4 "
			        "address\n", bind_s);
			return 2;
		}
		b = ntohl(addr.s_addr);
		if (!unicast(b))
		{
			fprintf(stderr, "dch serve: refusing --bind %s: not a "
			        "unicast address. dch serve never listens on a "
			        "wildcard; name one address of this host.\n",
			        bind_s);
			return 2;
		}
		if (!ip_tailnet(b) && !g_gate.test_loopback)
			slog("warning: --bind %s is not this host's tailnet "
			     "address. --bind relaxes nothing: only your own "
			     "tailnet nodes (100.64.0.0/10, owner-checked) get "
			     "in, so this bind admits nobody.", bind_s);
		if (g_nself < MAX_SELF)
			g_self[g_nself++] = b;
	}

	if (check)
	{
		struct in_addr ca;
		char *w = NULL;
		uint32_t ip;
		int v, self;

		if (inet_pton(AF_INET, check, &ca) != 1)
		{
			fprintf(stderr, "dch serve: --check-peer %s is not an "
			        "IPv4 address\n", check);
			return 2;
		}
		ip = ntohl(ca.s_addr);
		v = gate_addr(&g_gate, ip);
		if (v == G_OK)
		{
			self = !(g_gate.test_loopback && ip_loop(ip)) &&
			       ip_self(ip, 0);
			w = self ? NULL : ts_whois(check);
			v = serve_gate(&g_gate, ip, self, w,
			               !self && !w && ts_up());
			free(w);
		}
		printf("%s %s: %s\n", v < G_RANGE ? "accept" : "refuse", check,
		       gate_why(v));
		return v < G_RANGE ? 0 : 1;
	}

	if (!bind_s)
	{
		if (!g_nself)
		{
			fprintf(stderr, "dch serve: tailscale reported no address "
			        "(stopped or logged out: `tailscale ip -4` failed "
			        "or printed nothing). Not falling back to any other "
			        "address; pass --bind <ipv4> to choose one.\n");
			return 1;
		}
		addr.s_addr = htonl(g_self[0]);
	}
	inet_ntop(AF_INET, &addr, ipstr, sizeof ipstr);

	if (build_hello() < 0)
		return 1;
	if ((lfd = socket(AF_INET, SOCK_STREAM, 0)) < 0)
	{
		fprintf(stderr, "dch serve: socket: %s\n", strerror(errno));
		return 1;
	}
	cloexec(lfd);
	setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
	memset(&sa, 0, sizeof sa);
	sa.sin_family = AF_INET;
	sa.sin_port = htons((unsigned short)port);
	sa.sin_addr = addr;
	if (bind(lfd, (struct sockaddr *)&sa, sizeof sa) < 0)
	{
		if (errno == EADDRINUSE)
			fprintf(stderr, "dch serve: %s:%ld is already in use, "
			        "likely by Prosper (its Remote Terminal on a Mac) or "
			        "another dch serve. Stop that one first: dch serve "
			        "1.0 takes no other port, the phone dials %d.\n",
			        ipstr, port, SV_PORT);
		else
			fprintf(stderr, "dch serve: bind %s:%ld: %s\n", ipstr,
			        port, strerror(errno));
		close(lfd);
		return 1;
	}
	if (listen(lfd, 16) < 0 || pipe_ce(np) < 0)
	{
		fprintf(stderr, "dch serve: listen: %s\n", strerror(errno));
		close(lfd);
		return 1;
	}
	sl = sizeof sa;	/* --port 0: the kernel's pick, logged below */
	if (getsockname(lfd, (struct sockaddr *)&sa, &sl) == 0)
		port = ntohs(sa.sin_port);
	nonblock(lfd);
	nonblock(np[0]);
	nonblock(np[1]);
	g_notify = np[1];
	slog("listening on %s:%ld (dch %s, proto 1.0, ops: %s)", ipstr, port,
	     DCH_VERSION, g_allow_create ? "create, watch" : "watch");
	if (g_gate.self_uid >= 0)
		slog("accepting nodes of user %lld%s", g_gate.self_uid,
		     g_gate.nallow ? " and --allow-user" : "");
	else if (g_gate.nallow)
		slog("accepting nodes of --allow-user only");

	while (!g_stop)
	{
		struct pollfd pf[2];

		kids_reap();
		pf[0].fd = lfd;
		pf[0].events = POLLIN;
		pf[1].fd = np[0];
		pf[1].events = POLLIN;
		pf[0].revents = pf[1].revents = 0;
		if (poll(pf, 2, 1000) < 0)
		{
			if (errno == EINTR)
				continue;
			slog("poll: %s", strerror(errno));
			break;
		}
		if (pf[1].revents)
			kids_read(np[0]);
		if (pf[0].revents & POLLIN)
			accept_one(lfd, np[0]);
	}

	/* Stop: every handler detaches its client (the sessions live on). */
	slog("stopping; sessions keep running");
	close(lfd);
	for (i = 0; i < nkids; i++)
		kill(kids[i].pid, SIGTERM);
	{
		long long end = now_ms() + 3000;

		while (nkids && now_ms() < end)
		{
			kids_reap();
			poll(NULL, 0, 20);
		}
	}
	for (i = 0; i < nkids; i++)
	{
		kill(kids[i].pid, SIGKILL);
		waitpid(kids[i].pid, NULL, 0);
	}
	return 0;
}
