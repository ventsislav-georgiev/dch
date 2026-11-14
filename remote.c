/*
** The dch client for remote hosts (#087): the sessions of the user's other
** machines, served over Prosper's remote frames 1.0/1.1 (TCP 8771) by `dch
** serve` (serve.c) or by Prosper's DchSessionServer on a Mac.
**
** Hosts. --hosts (or the config's remote_hosts), else discovery: the online
** peers of `tailscale status --json` owned by this host's own Tailscale
** user (numeric UserID), untagged, at their 100.64.0.0/10 address, and only
** while tailscaled says BackendState "Running". Nothing else is ever
** dialled. No tailscale CLI, no peers, DCH_NO_REMOTE=1 or remote_hosts =
** off: no remote rows, never an error. Before an attach to a discovered
** host, `tailscale whois` on its address must name the same user, untagged
** (a host the user named in --hosts or remote_hosts is taken as given).
**
** Listing. Every host is probed at once, non-blocking, within one time box:
** connect, 0x08 and 0x03 pipelined (both read-only; the spec lets them
** share a connection), then 0x18 (a newer major refuses the host) and 0x11.
** The rows go to a cache in dch's state dir; a host that did not answer in
** time shows its cached rows, marked stale, for RM_TTL_S. `--ls --remote`
** probes in the foreground, all of it (the tailscale call, name lookups,
** every probe) within RM_BOX_MS; the picker reads the cache and re-probes in
** a forked child, so nothing stalls it, and shows only the rows of hosts
** that are candidates now.
**
** Attach. The list check and the 0x01 share one connection (Prosper's 0x01
** creates a session it does not have). A relay child bridges attach.c's packets on a socketpair to the
** frames: the attach client is the local one, so the detach key, the
** double tap and the redraw on resume all work the same. The TCP close is
** the detach; the remote session lives on.
**
** Every byte from a peer is hostile: frames are bounded (rf_parse in its
** server-frames mode), every JSON field is copied into a fixed buffer or
** dropped, and every name a peer sent is escaped (logsafe) before it is
** printed. Only the 0x10 bytes of an attach reach the terminal raw, as a
** local session's screen does.
**
** Windows (#077): the same file builds the native client, win/ the rest.
** Listing and the attach's checks are shared; the name lookups run in a
** thread, not a child; the picker's background probe is not built (#090);
** and an attach is one loop over the console and the socket (win_relay),
** not attach.c and a relay child.
**
** Control verbs (#114): dch_remote_ctl sends one 0x30 (the verb's argv,
** the host: off its names) on a connection of its own once the 0x18 lists
** "ctl", and writes the 0x31/0x32 bytes out as they come; 0x33's exit is
** its own. SIGINT, SIGTERM or a closed stdout (EPIPE) cancels it (#118):
** a 0x34 when the host lists "ctl-cancel", then its 0x33 for a moment;
** else the close, which the host takes as one. dch --xray host:name
** (#157) comes here too, its values checked first by dch --xray's own
** rules; a host whose hello lacks "xray" is exit 6, nothing sent. dch
** --xray host:name --requests --json (#158) holds one such connection warm
** for many requests, one at a time, each read off stdin (spec #832 4.3).
**
** dch --ls-json [--remote] --follow (#103) is the one client of the 0x2e
** watch stream; the picker re-probes every RM_EVERY_MS.
** ponytail: no remote kill, rename or create from the picker: those need a
** confirmation the way the local ones have one (the CLI verbs are ctl's).
*/
#ifdef _WIN32
#include "win/win.h"	/* Winsock, and the sock_* spellings below */
#else
#include "dtach.h"
#include "xray.h"	/* --requests checks a --ref as dch --xray does */
#endif
#include "rframe.h"

#ifndef _WIN32
#include <arpa/inet.h>
#endif
#include <ctype.h>
#include <float.h>
#ifndef _WIN32
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <strings.h>

/* The socket calls Winsock spells otherwise (win/win.h has those). */
#define sock_close close
#define sock_read read
#define sock_err() errno
#define sock_seterr(e) (errno = (e))
#define sock_str strerror
#define SE_INTR EINTR
#define SE_AGAIN EAGAIN
#define SE_INPROGRESS EINPROGRESS
#define SE_UNREACH EHOSTUNREACH
/* the cache: this user's file, that nobody else can write */
#define file_mine(st) ((st)->st_uid == getuid() && !((st)->st_mode & 022))
#define rename_over rename
#define O_BINARY 0
#endif

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0	/* SIGPIPE is ignored */
#endif

#define RM_PORT 8771
#define RM_HOSTS 64		/* hosts probed, at most */
#define RM_ROWS 1000		/* rows kept per host, at most */
#ifdef _WIN32
/* --ls on Windows lists only remote rows, and a process start (the
** tailscale call) costs more there */
#define RM_BOX_MS 1000
#else
#define RM_BOX_MS 300		/* --ls: every probe, in parallel, within */
#endif
#define RM_BG_MS 2000		/* the picker's background probe */
#define RM_ATTACH_MS 5000	/* -a: connect, hello and list */
#define RM_CANCEL_MS 3000	/* a ctl's 0x34 sent: its 0x33 within this
				** (the server's SIGKILL comes at 2 s) */
#define RM_EVERY_MS 2000	/* the picker re-probes this often */
#define RM_FRESH_S 10		/* the picker: older rows show as stale */
#define RM_TTL_S 120		/* a silent host's cached rows, this long */
#define RM_TS_MS 1500		/* `tailscale status --json` */
#define RM_CACHE_MAX (8u << 20)
#define RM_HIST_MAX (2u << 20)	/* a host's --history --json, at most */
#define RM_HRECS 500		/* records kept per host, at most (newest first) */
#define RM_HIST_EVERY_MS 10000	/* the picker re-fetches history this often */
#define RM_HIST_FRESH_S 30	/* the picker: older history shows as stale */
#define RM_HIGH 262144		/* relay: past this unsent, stop reading */
#define RM_REARM_MS 1000	/* relay: a re-arm awaited this long (#231) */
#define RM_READ 65536

struct rhost
{
	char label[DCH_RHOST_MAX];	/* what host:name names */
	char addr[256];		/* an IPv4 literal, or a name (--hosts) */
	unsigned short port;
	int found;		/* by discovery: whois it before an attach */
};

static const char *g_hosts;	/* --hosts */
static long long g_me = -1;	/* discovery: this host's Tailscale user */

#ifndef _WIN32
/* A forked child's signals: dch's handlers (attach.c's die, the picker's)
** act on the shared terminal and the parent's files, so none of them run
** here. SIGPIPE stays ignored: a peer that hangs up is an error return. */
static void
child_signals(void)
{
	static const int sigs[] = { SIGHUP, SIGINT, SIGTERM, SIGQUIT, SIGUSR1,
	                            SIGUSR2, SIGWINCH, SIGCHLD, SIGALRM };
	sigset_t none;
	size_t i;

	for (i = 0; i < sizeof sigs / sizeof *sigs; i++)
		signal(sigs[i], SIG_DFL);
	signal(SIGPIPE, SIG_IGN);
	sigemptyset(&none);
	sigprocmask(SIG_SETMASK, &none, NULL);
}
#endif

/* ---- hosts ----------------------------------------------------------- */

static int
plain(const char *s, size_t n, const char *extra)
{
	size_t i;

	for (i = 0; i < n; i++)
		if (!s[i] || (!isalnum((unsigned char)s[i]) &&
		              !strchr(extra, s[i])) || (unsigned char)s[i] >= 0x80)
			return 0;
	return 1;
}

/* One entry, [label=]host[:port], into *h: 0, or -1. A label and a host
** are plain ASCII, so neither carries a ':' or anything a terminal acts
** on. The label is the host when not given. Pure. */
static int
host_entry(const char *s, size_t n, struct rhost *h)
{
	const char *eq = memchr(s, '=', n), *hs = eq ? eq + 1 : s, *colon;
	size_t hn = (size_t)(s + n - hs), ln;
	long port = RM_PORT;

	memset(h, 0, sizeof *h);
	if ((colon = memchr(hs, ':', hn)))
	{
		size_t pn = (size_t)(hs + hn - colon - 1), i;

		if (!pn || pn > 5)
			return -1;
		for (i = 0, port = 0; i < pn; i++)
		{
			if (colon[1 + i] < '0' || colon[1 + i] > '9')
				return -1;
			port = port * 10 + (colon[1 + i] - '0');
		}
		if (port < 1 || port > 65535)
			return -1;
		hn = (size_t)(colon - hs);
	}
	ln = eq ? (size_t)(eq - s) : hn;
	if (!hn || hn >= sizeof h->addr || !plain(hs, hn, "-.") || !ln ||
	    ln >= sizeof h->label || !plain(eq ? s : hs, ln, "-._"))
		return -1;
	memcpy(h->addr, hs, hn);
	h->addr[hn] = '\0';
	memcpy(h->label, eq ? s : hs, ln);
	h->label[ln] = '\0';
	h->port = (unsigned short)port;
	return 0;
}

static int
host_find(const struct rhost *v, int n, const char *label)
{
	int i;

	for (i = 0; i < n; i++)
		if (!strcasecmp(v[i].label, label))
			return i;
	return -1;
}

/* A comma (or space) separated list of entries into v[cap]: how many, or
** -1 at the first bad one (its text in bad[badcap], escaped). A repeated
** label counts once. Pure. */
static int
hosts_parse(const char *list, struct rhost *v, int cap, char *bad,
            size_t badcap)
{
	int n = 0;

	while (*list)
	{
		size_t k = strcspn(list, ", \t");
		struct rhost h;

		if (k && host_entry(list, k, &h) < 0)
		{
			char t[96];

			snprintf(t, sizeof t, "%.*s", (int)(k > 80 ? 80 : k), list);
			logsafe(bad, badcap, t);
			return -1;
		}
		if (k && n < cap && host_find(v, n, h.label) < 0)
			v[n++] = h;
		list += k + (list[k] != '\0');
	}
	return n;
}

static int
ip_cgnat4(const char *s)
{
	struct in_addr a;

	return inet_pton(AF_INET, s, &a) == 1 &&
	       (ntohl(a.s_addr) & 0xffc00000u) == 0x64400000u;
}

/* A peer's label: its MagicDNS name's first label, else its HostName, in
** lower case, anything but [a-z0-9-] as '-'. 0, or -1 when none. */
static int
peer_label(const struct json_parser *jp, int p, char *out)
{
	char dns[256], *dot;
	size_t i;

	if (jstr(jp, p, "DNSName", dns, sizeof dns) != 1 || !dns[0] ||
	    dns[0] == '.')
	{
		if (jstr(jp, p, "HostName", dns, sizeof dns) != 1)
			return -1;
	}
	else if ((dot = strchr(dns, '.')))
		*dot = '\0';
	dns[DCH_RHOST_MAX - 1] = '\0';
	for (i = 0; dns[i]; i++)
	{
		unsigned char c = (unsigned char)dns[i];

		dns[i] = c < 0x80 && isalnum(c) ? (char)tolower(c) : '-';
	}
	if (!dns[0] || strspn(dns, "-") == i)
		return -1;
	memcpy(out, dns, i + 1);
	return 0;
}

/* `tailscale status --json` into v[cap]: the peers worth probing, online,
** untagged and owned by Self's user (by numeric id, into *me), each at its
** first IPv4 in 100.64.0.0/10. None on a tagged host (no user owns it) or
** for a negative Self.UserID (-1 is "unknown" to peer_is_mine and dch
** serve's gate), and none unless BackendState is "Running": a stopped or
** logged-out tailscaled still lists its peers, and 100.64.0.0/10 off the
** tailnet is any ISP's carrier-grade NAT. Pure. */
static int
peers_from(const char *js, struct rhost *v, int cap, long long *me)
{
	struct json_parser jp;
	char node[NODE_ID_MAX], bs[32];
	long long uid;
	int tagged, peers, i, n = 0;

	*me = -1;
	if (status_self(js, me, node, &tagged) < 0 || tagged || *me < 0)
		return 0;
	if (dch_json_parse(&jp, js) < 0 ||
	    jstr(&jp, 0, "BackendState", bs, sizeof bs) != 1 ||
	    strcmp(bs, "Running"))
	{
		dch_json_free(&jp);
		return 0;
	}
	peers = jcont(&jp, 0, "Peer", JSON_OBJECT);
	for (i = peers + 1; peers >= 0 && i + 1 < jp.ntok &&
	     i < jp.tok[peers].next && n < cap; i = jp.tok[i + 1].next)
	{
		int p = i + 1, ips, on, k;
		struct rhost h;

		if (jp.tok[p].kind != JSON_OBJECT)
			continue;
		on = dch_json_field(&jp, p, "Online");
		if (on < 0 || jp.tok[on].kind != JSON_PRIMITIVE ||
		    jp.tok[on].end - jp.tok[on].start != 4 ||
		    memcmp(jp.text + jp.tok[on].start, "true", 4) ||
		    jint(&jp, p, "UserID", &uid) != 1 || uid != *me ||
		    has_tags(&jp, p) || peer_label(&jp, p, h.label) < 0 ||
		    host_find(v, n, h.label) >= 0 ||
		    (ips = jcont(&jp, p, "TailscaleIPs", JSON_ARRAY)) < 0)
			continue;
		h.addr[0] = '\0';
		for (k = ips + 1; k < jp.tok[ips].next && !h.addr[0];
		     k = jp.tok[k].next)
			if (jp.tok[k].kind != JSON_STRING ||
			    dch_json_decode(&jp, k, h.addr, 64) < 0 ||
			    !ip_cgnat4(h.addr))
				h.addr[0] = '\0';
		if (!h.addr[0])
			continue;
		h.port = RM_PORT;
		h.found = 1;
		v[n++] = h;
	}
	dch_json_free(&jp);
	return n;
}

/* Remote rows at all: --hosts, else not with DCH_NO_REMOTE=1 or
** remote_hosts = off. */
static int
remote_on(void)
{
	char c[16];

	if (g_hosts)
		return 1;
	return !dch_env_on("DCH_NO_REMOTE") &&
	       !(dch_config_get("remote_hosts", c, sizeof c) &&
	         !strcmp(c, "off"));
}

/* The hosts the user named: --hosts, else remote_hosts (a bad one said on
** stderr when loud, and then none). How many, or -1 when neither is set. */
static int
named_hosts(struct rhost *v, int cap, int loud)
{
	char cfg[1024], bad[400];
	int n;

	if (g_hosts)
		return hosts_parse(g_hosts, v, cap, bad, sizeof bad);
	if (!dch_config_get("remote_hosts", cfg, sizeof cfg) || !cfg[0])
		return -1;
	if ((n = hosts_parse(cfg, v, cap, bad, sizeof bad)) < 0 && loud)
		fprintf(stderr, "dch: remote_hosts: bad entry %s\n", bad);
	return n < 0 ? 0 : n;
}

/* The hosts to dial: --hosts, else remote_hosts (a bad entry said when
** loud), else discovery, its `tailscale status` given ts_ms. How many, or
** -1 when discovery could not tell (tailscale failed or ran out of time). */
static int
candidates(struct rhost *v, int cap, int ts_ms, int loud)
{
	char *out = NULL;
	char *av[] = { NULL, "status", "--json", NULL };
	int st, n;

	if (!remote_on())
		return 0;
	if ((n = named_hosts(v, cap, loud)) >= 0)
		return n;
	if (!g_ts[0])
		ts_find();
	if (!g_ts[0])
		return 0;
	av[0] = g_ts;
	st = rf_run(av, &out, NULL, RM_CACHE_MAX, NULL, 0, ts_ms);
	n = st >= 0 && WIFEXITED(st) && WEXITSTATUS(st) == 0 && out
	    ? peers_from(out, v, cap, &g_me) : -1;
	free(out);
	return n;
}

/* The candidate labels the picker shows rows of, "\n"-separated with one
** at each end: the last probe round's, this process's or the background
** child's (sent on a pipe). A cache row of any other host is not shown. */
static char g_lab[RM_HOSTS * DCH_RHOST_MAX + 2];
static int g_lab_ok;

static void
labels_set(const struct rhost *v, int n)
{
	size_t o = 1;
	int i;

	g_lab[0] = '\n';
	for (i = 0; i < n; i++)
		o += (size_t)snprintf(g_lab + o, sizeof g_lab - o, "%s\n",
		                      v[i].label);
	g_lab[o] = '\0';
	g_lab_ok = 1;
}

#ifndef _WIN32
static int
label_in(const char *host)
{
	char nd[DCH_RHOST_MAX + 3];

	snprintf(nd, sizeof nd, "\n%s\n", host);
	return g_lab_ok && strstr(g_lab, nd) != NULL;
}
#endif

const char *
dch_remote_hosts(void)
{
	return g_hosts;
}

int
dch_remote_set_hosts(const char *list)
{
	struct rhost v[RM_HOSTS];
	char bad[400];

	if (hosts_parse(list, v, RM_HOSTS, bad, sizeof bad) < 0)
	{
		fprintf(stderr, "dch: --hosts: bad entry %s (want "
		        "[label=]host[:port], comma-separated)\n", bad);
		return -1;
	}
	g_hosts = list;
	return 0;
}

/* ---- rows ------------------------------------------------------------ */

struct rlist
{
	struct dch_rrow *v;
	int n, cap;
};

static struct dch_rrow *
rl_new(struct rlist *l)
{
	if (l->n == l->cap)
	{
		int c = l->cap ? l->cap * 2 : 32;
		struct dch_rrow *q;

		if (c > RM_HOSTS * RM_ROWS * 2 ||
		    !(q = realloc(l->v, (size_t)c * sizeof *q)))
			return NULL;
		l->v = q;
		l->cap = c;
	}
	memset(&l->v[l->n], 0, sizeof *l->v);
	return &l->v[l->n];
}

/* The rows of array token arr: a listResp's (host given, every row
** answered at `at`), or the cache's (host NULL: each row names its host, a
** plain label, and when it answered, after 0 and at most a minute past
** `at`, the time now). A row without a usable name is skipped; the alias
** kept only when it fits, the state only when it is one of the four. At
** most RM_ROWS. Pure. */
static void
rows_from(const struct json_parser *jp, int arr, const char *host,
          long long at, struct rlist *l)
{
	int i, took = 0;

	for (i = arr + 1; i < jp->tok[arr].next && took < RM_ROWS * (host ? 1 :
	     RM_HOSTS); i = jp->tok[i].next)
	{
		struct dch_rrow *r;
		long long a = at;

		if (jp->tok[i].kind != JSON_OBJECT || !(r = rl_new(l)))
			continue;
		if (jstr(jp, i, "name", r->name, sizeof r->name) != 1 ||
		    !r->name[0])
			continue;
		if (host)
			copy_str(r->host, sizeof r->host, host);
		else if (jstr(jp, i, "host", r->host, sizeof r->host) != 1 ||
		         !r->host[0] ||
		         !plain(r->host, strlen(r->host), "-._") ||
		         jint(jp, i, "at", &a) != 1 || a <= 0 || a > at + 60)
			continue;
		if (jstr(jp, i, "alias", r->alias, sizeof r->alias) != 1)
			r->alias[0] = '\0';
		if (jstr(jp, i, "cwd", r->cwd, sizeof r->cwd) != 1)
			r->cwd[0] = '\0';
		if (jstr(jp, i, "state", r->state, sizeof r->state) != 1 ||
		    (strcmp(r->state, "working") && strcmp(r->state, "idle") &&
		     strcmp(r->state, "blocked") && strcmp(r->state, "done")))
			r->state[0] = '\0';
		r->at = (long)a;
		l->n++;
		took++;
	}
}

/* A listResp (or cache) document's rows: 0, or -1 when it is not a JSON
** array. */
static int
rows_doc(const char *js, const char *host, long long at, struct rlist *l)
{
	struct json_parser jp;
	int rc = -1;

	if (dch_json_parse(&jp, js) == 0 && jp.ntok > 0 &&
	    jp.tok[0].kind == JSON_ARRAY)
	{
		rows_from(&jp, 0, host, at, l);
		rc = 0;
	}
	dch_json_free(&jp);
	return rc;
}

/* The major of 0x18's "proto" ("<major>.<minor>"; none is a pre-#776
** Prosper, so 1), or -1 for a hello that is not an object or a proto not
** of that shape. Pure. */
static int
hello_major(const char *js, char *proto, size_t cap)
{
	struct json_parser jp;
	int r = -1, k;
	size_t d, m;

	copy_str(proto, cap, "1.0");
	if (dch_json_parse(&jp, js) == 0 && jp.ntok > 0 &&
	    jp.tok[0].kind == JSON_OBJECT &&
	    (k = jstr(&jp, 0, "proto", proto, cap)) >= 0)
	{
		d = strspn(proto, "0123456789");
		m = d && proto[d] == '.' ? strspn(proto + d + 1, "0123456789")
		                         : 0;
		if (k == 0)
			r = 1;
		else if (d && d < 5 && m && m < 5 && !proto[d + 1 + m])
			r = atoi(proto);
	}
	if (r < 0)
		copy_str(proto, cap, "?");
	dch_json_free(&jp);
	return r;
}

static int
row_cmp(const void *x, const void *y)
{
	const struct dch_rrow *a = x, *b = y;
	int c = strcmp(a->host, b->host);

	return c ? c : strcmp(a->name, b->name);
}

/* ---- remote history (#115) ------------------------------------------ */

struct hlist
{
	struct dch_rrec *v;
	int n, cap;
};

void
dch_remote_hfree(struct dch_rrec *v, int n)
{
	int i;

	for (i = 0; v && i < n; i++)
		free(v[i].json);
	free(v);
}

static struct dch_rrec *
hl_new(struct hlist *l)
{
	if (l->n == l->cap)
	{
		int c = l->cap ? l->cap * 2 : 64;
		struct dch_rrec *q;

		if (c > RM_HOSTS * RM_HRECS * 2 ||
		    !(q = realloc(l->v, (size_t)c * sizeof *q)))
			return NULL;
		l->v = q;
		l->cap = c;
	}
	memset(&l->v[l->n], 0, sizeof *l->v);
	return &l->v[l->n];
}

/* Object token o as text, malloc'd: every C1, DEL and bidi control (which
** a valid document holds raw only inside a string) as a \u escape, the way
** --ls-json writes them, and any whitespace byte as a space. NULL when out
** of memory. Pure. */
static char *
obj_safe(const struct json_parser *jp, int o)
{
	const unsigned char *s = (const unsigned char *)jp->text +
	                         jp->tok[o].start;
	size_t n = (size_t)(jp->tok[o].end - jp->tok[o].start), i;
	struct sb b = {0};
	char e[8];
	unsigned cp;
	int k;

	for (i = 0; i < n; i += (size_t)k)
	{
		if (!(k = utf8_at(s + i, &cp)))
		{
			k = 1;	/* never: the parser took only UTF-8 */
			cp = s[i];
		}
		if (cp < 0x20)
			sb_s(&b, " ");
		else if (cp_ctl(cp))
		{
			snprintf(e, sizeof e, "\\u%04x", cp);
			sb_s(&b, e);
		}
		else
			sb_add(&b, s + i, (size_t)k);
	}
	if (b.bad || !b.p)
	{
		free(b.p);
		return NULL;
	}
	return b.p;
}

/* Object token o as a strict JSON reader takes it (#167): no lone
** surrogate escape, no number past a double's range, no object or array
** nested over 64 deep in it (keys do not count, QA 1.55.0 NIT-8: serde_json
** takes 128). Python, serde_json and the like reject a whole --history
** --json --remote over one such record. Pure. */
static int
hist_strict(const struct json_parser *jp, int o)
{
	char x[5] = "";
	unsigned long u;
	int t, q, d, i, n;

	for (t = o + 1; t < jp->tok[o].next; t++)
	{
		const char *s = jp->text + jp->tok[t].start;

		n = jp->tok[t].end - jp->tok[t].start;
		if (jp->tok[t].kind == JSON_OBJECT || jp->tok[t].kind == JSON_ARRAY)
		{
			for (q = t, d = 0; q != o && d <= 64; q = jp->tok[q].parent)
				d += jp->tok[q].kind == JSON_OBJECT ||
				     jp->tok[q].kind == JSON_ARRAY;
			if (d > 64)
				return 0;
		}
		else if (jp->tok[t].kind == JSON_PRIMITIVE)
		{
			double v = strtod(s, NULL);	/* true, false, null: 0 */

			if (v > DBL_MAX || v < -DBL_MAX)
				return 0;
		}
		else	/* the parser checked each \u has 4 hex digits */
			for (i = 0; i < n; i++)
				if (s[i] == '\\' && s[++i] == 'u')
				{
					memcpy(x, s + i + 1, 4);
					u = strtoul(x, NULL, 16);
					if (u >= 0xdc00 && u < 0xe000)
						return 0;
					if (u >= 0xd800 && u < 0xdc00)
					{
						if (i + 10 >= n || s[i + 5] != '\\' ||
						    s[i + 6] != 'u')
							return 0;
						memcpy(x, s + i + 7, 4);
						u = strtoul(x, NULL, 16);
						if (u < 0xdc00 || u >= 0xe000)
							return 0;
						i += 6;
					}
					i += 4;
				}
	}
	return 1;
}

struct hkey
{
	int par;		/* its object's token */
	const char *s;		/* the key, decoded */
};

static int
hkey_cmp(const void *a, const void *b)
{
	const struct hkey *x = a, *y = b;

	return x->par != y->par ? (x->par < y->par ? -1 : 1) : strcmp(x->s, y->s);
}

/* A key given twice in one object of record o, by its decoded text: text
** and the picker would show a read one empty, a JSON reader the last value
** (#167), and a reader that refuses duplicates the whole document over any
** (QA 1.55.0 NIT-9). 1 too when out of memory. Pure. */
static int
hist_dup(const struct json_parser *jp, int o)
{
	int cap = jp->tok[o].next - o, n = 0, t, dup = 0;
	size_t at = 0, k;
	struct hkey *v = malloc((size_t)cap * sizeof *v);
	/* each key decoded is no longer than as written, plus its NUL */
	char *b = malloc((size_t)(jp->tok[o].end - jp->tok[o].start) +
	                 (size_t)cap);

	for (t = o + 1; v && b && !dup && t < jp->tok[o].next; t++)
	{
		if (jp->tok[t].kind != JSON_STRING ||
		    jp->tok[jp->tok[t].parent].kind != JSON_OBJECT)
			continue;	/* a value, not a key */
		k = (size_t)(jp->tok[t].end - jp->tok[t].start) + 1;
		dup = dch_json_decode(jp, t, b + at, k) < 0;
		v[n].par = jp->tok[t].parent;
		v[n++].s = b + at;
		at += k;
	}
	if (!v || !b)
		dup = 1;
	else if (!dup && n > 1)
	{
		qsort(v, (size_t)n, sizeof *v, hkey_cmp);
		for (t = 1; t < n && !dup; t++)
			dup = !hkey_cmp(&v[t - 1], &v[t]);
	}
	free(v);
	free(b);
	return dup;
}

/* The records of array token arr, host's --history --json answered at at:
** each an object with a "name", at most RM_HRECS. One without a name, or
** with a "host" or "stale" key of its own (dch adds those), or one
** hist_strict rejects or with a key hist_dup finds twice, is skipped;
** every other field kept only when it fits, the record whole in json.
** Pure. */
static void
hist_from(const struct json_parser *jp, int arr, const char *host,
          long long at, struct hlist *l)
{
	int i, took = 0;

	for (i = arr + 1; i < jp->tok[arr].next && took < RM_HRECS;
	     i = jp->tok[i].next)
	{
		struct dch_rrec *r;
		char w[512];
		long long v;
		size_t o = 0;
		int a, t;

		if (jp->tok[i].kind != JSON_OBJECT ||
		    dch_json_field(jp, i, "host") != -1 ||
		    dch_json_field(jp, i, "stale") != -1 ||
		    !hist_strict(jp, i) || hist_dup(jp, i) || !(r = hl_new(l)) ||
		    jstr(jp, i, "name", r->name, sizeof r->name) != 1 ||
		    !r->name[0])
			continue;
		if (jstr(jp, i, "file", r->file, sizeof r->file) != 1 ||
		    !r->file[0])
			copy_str(r->file, sizeof r->file, r->name);
		if (jstr(jp, i, "alias", r->alias, sizeof r->alias) != 1)
			r->alias[0] = '\0';
		if (jstr(jp, i, "end", r->end, sizeof r->end) != 1)
			r->end[0] = '\0';
		if (jstr(jp, i, "cwd", r->cwd, sizeof r->cwd) != 1)
			r->cwd[0] = '\0';
		a = jcont(jp, i, "argv", JSON_ARRAY);
		for (t = a + 1; a >= 0 && t < jp->tok[a].next && o < sizeof r->argv;
		     t = jp->tok[t].next)
			if (jp->tok[t].kind == JSON_STRING &&
			    dch_json_decode(jp, t, w, sizeof w) == 0)
				o += (size_t)snprintf(r->argv + o, sizeof r->argv - o,
				                      "%s%s", o ? " " : "", w);
		if ((a = jcont(jp, i, "agent", JSON_OBJECT)) >= 0)
		{
			if (jstr(jp, a, "session_id", r->sid, sizeof r->sid) != 1)
				r->sid[0] = '\0';
			if (jstr(jp, a, "name", r->aname, sizeof r->aname) != 1)
				r->aname[0] = '\0';
		}
		/* a long that fits 32 bits too (Windows) */
		if ((jint(jp, i, "ended", &v) == 1 && v > 0 && v < 0x7fffffffLL) ||
		    (jint(jp, i, "updated", &v) == 1 && v > 0 &&
		     v < 0x7fffffffLL))
			r->when = (long)v;
		t = dch_json_field(jp, i, "pinned");
		r->pinned = t >= 0 && jp->tok[t].kind == JSON_PRIMITIVE &&
		            jp->tok[t].end - jp->tok[t].start == 4 &&
		            !memcmp(jp->text + jp->tok[t].start, "true", 4);
		copy_str(r->host, sizeof r->host, host);
		r->at = (long)at;
		if (!(r->json = obj_safe(jp, i)))
			continue;
		l->n++;
		took++;
	}
}

/* A host's --history --json reply js: its records, 0; or -1 when it is not
** a JSON array. Pure. */
static int
hist_doc(const char *js, const char *host, long long at, struct hlist *l)
{
	struct json_parser jp;
	int rc = -1;

	if (dch_json_parse(&jp, js) == 0 && jp.ntok > 0 &&
	    jp.tok[0].kind == JSON_ARRAY)
	{
		hist_from(&jp, 0, host, at, l);
		rc = 0;
	}
	dch_json_free(&jp);
	return rc;
}

/* The history cache js: [{"at":N,"host":"label","recs":[...]}...], a
** host a plain label that answered after 0 and at most a minute past now.
** 0, or -1 when it is not a JSON array. Pure. */
static int
hist_cdoc(const char *js, long long now, struct hlist *l)
{
	struct json_parser jp;
	char host[DCH_RHOST_MAX];
	long long at;
	int i, recs, nh = 0, rc = -1;

	if (dch_json_parse(&jp, js) == 0 && jp.ntok > 0 &&
	    jp.tok[0].kind == JSON_ARRAY)
	{
		for (i = 1; i < jp.tok[0].next && nh < RM_HOSTS;
		     i = jp.tok[i].next)
			if (jp.tok[i].kind == JSON_OBJECT &&
			    jstr(&jp, i, "host", host, sizeof host) == 1 &&
			    host[0] && plain(host, strlen(host), "-._") &&
			    jint(&jp, i, "at", &at) == 1 && at > 0 &&
			    at <= now + 60 &&
			    (recs = jcont(&jp, i, "recs", JSON_ARRAY)) >= 0)
			{
				hist_from(&jp, recs, host, at, l);
				nh++;
			}
		rc = 0;
	}
	dch_json_free(&jp);
	return rc;
}

/* ---- the cache ------------------------------------------------------- */

/* In <state>/dch/sessions, which dch_history_dir has checked is a
** directory of this user's that nobody else can write. A dot file: master.c
** and the history loader skip it there. On Windows %LOCALAPPDATA%\dch
** (win/plat.c), in the user's profile, whose ACL keeps every other user
** out: that is the owner check there. */
static int
cache_path(char *out, size_t cap, const char *leaf)
{
	char dir[PATH_MAX];

	if (dch_history_dir(dir, sizeof dir, NULL, 1) < 0)
		return -1;
	return (size_t)snprintf(out, cap, "%s/%s", dir, leaf) < cap ? 0 : -1;
}

/* Cache file leaf, this user's and at most RM_CACHE_MAX: its text, or
** NULL. */
static char *
cache_read(const char *leaf)
{
	char path[PATH_MAX + 32], *text;
	struct stat st;
	ssize_t k;
	int fd;

	if (cache_path(path, sizeof path, leaf) < 0 ||
	    (fd = open(path, O_RDONLY | O_BINARY | O_CLOEXEC | O_NOFOLLOW)) < 0)
		return NULL;
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) || !file_mine(&st) ||
	    (size_t)st.st_size > RM_CACHE_MAX ||
	    !(text = malloc((size_t)st.st_size + 1)))
	{
		close(fd);
		return NULL;
	}
	k = read(fd, text, (size_t)st.st_size);
	close(fd);
	if (k == st.st_size && !memchr(text, 0, (size_t)k))
	{
		text[k] = '\0';
		return text;
	}
	free(text);
	return NULL;
}

/* The cached rows, each its host's and when that host answered (now: the
** time, against which a row's is checked). */
static void
cache_load(struct rlist *l, long long now)
{
	char *text = cache_read(".remote-cache");

	if (text)
		(void)rows_doc(text, NULL, now, l);
	free(text);
}

/* The cached history records (#115), as cache_load's rows. */
static void
hist_cload(struct hlist *l, long long now)
{
	char *text = cache_read(".remote-history");

	if (text)
		(void)hist_cdoc(text, now, l);
	free(text);
}

int
dch_remote_on(void)
{
	return remote_on();
}

/* The host this process is attached to (#189): the in-attach X-ray chord's
** view routes its host:name by it, a host only discovery found too. */
static char g_attached[DCH_RHOST_MAX];

/* host is one dch knows without a probe: a named host (--hosts,
** remote_hosts), a cached row's or the attach's; never discovery (no
** tailscale run). */
int
dch_remote_known(const char *host)
{
	struct rhost v[RM_HOSTS];
	struct rlist l = {0};
	int n, k, hit = 0;

	if (!remote_on())
		return 0;
	if (g_attached[0] && !strcasecmp(g_attached, host))
		return 1;
	n = named_hosts(v, RM_HOSTS, 0);
	for (k = 0; k < n && !hit; k++)
		hit = !strcasecmp(v[k].label, host);	/* as host_find */
	if (!hit)
	{
		cache_load(&l, (long long)time(NULL));
		for (k = 0; k < l.n && !hit; k++)
			hit = !strcasecmp(l.v[k].host, host);
		free(l.v);
	}
	if (!hit)
	{
		/* a host whose History rows were listed (#115) */
		struct hlist hl = {0};

		hist_cload(&hl, (long long)time(NULL));
		for (k = 0; k < hl.n && !hit; k++)
			hit = !strcasecmp(hl.v[k].host, host);
		dch_remote_hfree(hl.v, hl.n);
	}
	return hit;
}

/* b as the cache file leaf: written to a new mkstemp file (created
** exclusively, here and on Windows) renamed over the old, SIGTERM held
** meanwhile (dch_remote_stop's), so no half-written file is ever left
** behind. */
static void
cache_write(const char *leaf, struct sb *b)
{
	char path[PATH_MAX + 32], tmp[PATH_MAX + 48], tl[64];
#ifndef _WIN32
	sigset_t term, old;
#endif
	int fd;
	ssize_t w;

	snprintf(tl, sizeof tl, "%s.XXXXXX", leaf);
	if (b->bad || cache_path(path, sizeof path, leaf) < 0 ||
	    cache_path(tmp, sizeof tmp, tl) < 0)
		return;
#ifndef _WIN32
	sigemptyset(&term);
	sigaddset(&term, SIGTERM);
	sigprocmask(SIG_BLOCK, &term, &old);
#endif
	if ((fd = mkstemp(tmp)) >= 0)
	{
		w = write(fd, b->p, b->n);
		if (close(fd) < 0 || w != (ssize_t)b->n || rename_over(tmp, path) < 0)
			unlink(tmp);
	}
#ifndef _WIN32
	sigprocmask(SIG_SETMASK, &old, NULL);
#endif
}

/* l as the cache: up to RM_CACHE_MAX less a row's worst case, so the
** loader never refuses the file (a host past that is left out). */
static void
cache_save(const struct rlist *l)
{
	struct sb b = {0};
	int i;

	sb_s(&b, "[");
	for (i = 0; i < l->n && b.n + 6 * (DCH_RHOST_MAX + 3 * 256 + 512) + 64 <
	     RM_CACHE_MAX; i++)
	{
		const struct dch_rrow *r = &l->v[i];

		sb_s(&b, i ? ",\n{\"alias\":" : "{\"alias\":");
		sb_q(&b, r->alias);
		sb_s(&b, ",\"at\":");
		sb_ll(&b, r->at);
		sb_s(&b, ",\"cwd\":");
		sb_q(&b, r->cwd);
		sb_s(&b, ",\"host\":");
		sb_q(&b, r->host);
		sb_s(&b, ",\"name\":");
		sb_q(&b, r->name);
		sb_s(&b, ",\"state\":");
		sb_q(&b, r->state);
		sb_s(&b, "}");
	}
	sb_s(&b, "]\n");
	cache_write(".remote-cache", &b);
	free(b.p);
}

/* l (each host's records together) as the history cache, hist_cdoc's
** shape: up to RM_CACHE_MAX, a host past that left out. */
static void
hist_save(const struct hlist *l)
{
	struct sb b = {0};
	size_t sz;
	int i, j, k;

	sb_s(&b, "[");
	for (i = 0; i < l->n; i = j)
	{
		for (j = i, sz = 0; j < l->n && !strcmp(l->v[j].host,
		     l->v[i].host); j++)
			sz += strlen(l->v[j].json) + 2;
		if (b.n + sz + 6 * DCH_RHOST_MAX + 64 >= RM_CACHE_MAX)
			continue;
		sb_s(&b, b.n > 1 ? ",\n{\"at\":" : "{\"at\":");
		sb_ll(&b, l->v[i].at);
		sb_s(&b, ",\"host\":");
		sb_q(&b, l->v[i].host);
		sb_s(&b, ",\"recs\":[");
		for (k = i; k < j; k++)
		{
			sb_s(&b, k > i ? ",\n" : "");
			sb_s(&b, l->v[k].json);
		}
		sb_s(&b, "]}");
	}
	sb_s(&b, "]\n");
	cache_write(".remote-history", &b);
	free(b.p);
}

/* ---- probes ---------------------------------------------------------- */

/* PR_REFUSED: the server's gate turned us away with a 0x13 that has a
** "code" (#094), not a host that is down: why holds its message. PR_OLD: a
** history probe's host has no ctl op (#115). */
enum { PR_CONN, PR_WAIT, PR_OK, PR_FAIL, PR_NEWER, PR_REFUSED, PR_OLD };

struct probe
{
	struct rhost h;
	struct sockaddr_in sa;
	int have;		/* sa is h's address */
	int fd, st, hello;
	int keep;		/* PR_OK leaves fd open, in holding what followed */
	int hello_only;		/* a ctl's (#114): 0x08 alone, PR_OK on 0x18 */
	struct rf_state rs;
	struct sb in;
	char *list;		/* the 0x11 payload, NUL-terminated */
	char *hello_js;		/* hello_only, keep: the 0x18 payload */
	char why[300];		/* PR_FAIL, PR_NEWER, PR_REFUSED: printable */
	/* hist (#115): once the hello lists "ctl", a 0x30 of --history --json,
	** its 0x31 bytes into hout (RM_HIST_MAX at most); PR_OK on exit 0 */
	int hist;
	struct sb hout, herr;
};

static int hello_op(const char *js, const char *op);
static void ctl_payload(struct sb *b, int argc, char **argv, const char *cwd);
static int ctl_in(struct rf_state *rs, struct sb *tin, struct sb *out,
                  struct sb *err, int *xit, char *why, size_t wcap, char *msg,
                  size_t cap, int *code, int split);

/* Every probe's sa, its address when it is an IPv4 literal: how many are
** names, not literals. */
static int
addr_literals(struct probe *v, int n)
{
	int i, names = 0;

	for (i = 0; i < n; i++)
	{
		memset(&v[i].sa, 0, sizeof v[i].sa);
		v[i].sa.sin_family = AF_INET;
		v[i].sa.sin_port = htons(v[i].h.port);
		v[i].have = inet_pton(AF_INET, v[i].h.addr,
		                      &v[i].sa.sin_addr) == 1;
		names += !v[i].have;
	}
	return names;
}

#ifdef _WIN32
/* Windows: the lookups in a thread. One still running at `end` is left to
** finish on its own; its job is freed by whichever side lets go last. */
struct rjob
{
	volatile LONG refs;
	int n, want[RM_HOSTS];
	char addr[RM_HOSTS][256];
	uint32_t got[RM_HOSTS];
};

static DWORD WINAPI
rjob_run(void *arg)
{
	struct rjob *j = arg;
	struct addrinfo hint, *res;
	int i;

	memset(&hint, 0, sizeof hint);
	hint.ai_family = AF_INET;
	hint.ai_socktype = SOCK_STREAM;
	for (i = 0; i < j->n; i++)
		if (j->want[i] && getaddrinfo(j->addr[i], NULL, &hint, &res) == 0)
		{
			j->got[i] = ((struct sockaddr_in *)(void *)
			             res->ai_addr)->sin_addr.s_addr;
			freeaddrinfo(res);
		}
	if (InterlockedDecrement(&j->refs) == 0)
		free(j);
	return 0;
}

static void
resolve_all(struct probe *v, int n, long long end)
{
	struct rjob *j;
	HANDLE th;
	long long left;
	int i;

	if (!addr_literals(v, n) || !(j = calloc(1, sizeof *j)))
		return;
	j->refs = 2;
	j->n = n;
	for (i = 0; i < n; i++)
	{
		j->want[i] = !v[i].have;
		copy_str(j->addr[i], sizeof j->addr[i], v[i].h.addr);
	}
	if (!(th = CreateThread(NULL, 0, rjob_run, j, 0, NULL)))
	{
		free(j);
		return;
	}
	left = end - now_ms();
	if (WaitForSingleObject(th, left > 0 ? (DWORD)left : 0) ==
	    WAIT_OBJECT_0)
		for (i = 0; i < n; i++)
			if (!v[i].have && j->got[i])
			{
				v[i].sa.sin_addr.s_addr = j->got[i];
				v[i].have = 1;
			}
	CloseHandle(th);
	if (InterlockedDecrement(&j->refs) == 0)
		free(j);
}
#else
/* Every probe's address into its sa, by `end` (now_ms): an IPv4 literal at
** once; the names (--hosts, remote_hosts; MagicDNS answers a tailnet one)
** in one forked child, since getaddrinfo has no timeout of its own: a slow
** resolver costs what is left of the time box, and a name not resolved by
** then fails its probe. */
static void
resolve_all(struct probe *v, int n, long long end)
{
	uint32_t got[RM_HOSTS];
	size_t want = (size_t)n * sizeof *got, have = 0;
	int i, p[2];
	pid_t pid;

	if (!addr_literals(v, n) || pipe_ce(p) < 0)
		return;
	if ((pid = fork()) == 0)
	{
		struct addrinfo hint, *res;

		child_signals();
		memset(&hint, 0, sizeof hint);
		hint.ai_family = AF_INET;
		hint.ai_socktype = SOCK_STREAM;
		for (i = 0; i < n; i++)
		{
			got[i] = 0;
			if (!v[i].have &&
			    getaddrinfo(v[i].h.addr, NULL, &hint, &res) == 0)
			{
				got[i] = ((struct sockaddr_in *)(void *)
				          res->ai_addr)->sin_addr.s_addr;
				freeaddrinfo(res);
			}
		}
		/* at most 256 bytes: one atomic write */
		(void)!write(p[1], got, want);
		_exit(0);
	}
	close(p[1]);
	while (pid > 0 && have < want)
	{
		struct pollfd pf;
		long long left = end - now_ms();
		ssize_t k;

		pf.fd = p[0];
		pf.events = POLLIN;
		pf.revents = 0;
		if (left <= 0 || (poll(&pf, 1, (int)left) < 0 && errno != EINTR))
			break;
		if (!pf.revents)
			continue;
		if ((k = read(p[0], (char *)got + have, want - have)) <= 0)
		{
			if (k < 0 && errno == EINTR)
				continue;
			break;
		}
		have += (size_t)k;
	}
	if (pid > 0)
	{
		kill(pid, SIGKILL);
		while (waitpid(pid, NULL, 0) < 0 && errno == EINTR)
			;
	}
	close(p[0]);
	for (i = 0; have == want && i < n; i++)
		if (!v[i].have && got[i])
		{
			v[i].sa.sin_addr.s_addr = got[i];
			v[i].have = 1;
		}
}
#endif

static void
pr_end(struct probe *p, int st, const char *why)
{
	if (p->fd >= 0)
		sock_close(p->fd);
	p->fd = -1;
	p->st = st;
	if (why)
		logsafe(p->why, sizeof p->why, why);
}

/* A non-blocking connect to p's address: the socket, or -1 with
** sock_err(). */
static int
tcp_dial(const struct probe *p)
{
	int fd, e;

	if (!p->have)
	{
		sock_seterr(SE_UNREACH);
		return -1;
	}
	/* a SOCKET in an int: Windows handle values fit 32 bits */
	if ((fd = (int)socket(AF_INET, SOCK_STREAM, 0)) < 0)
		return -1;
	cloexec(fd);
	nonblock(fd);
	if (connect(fd, (const struct sockaddr *)&p->sa, sizeof p->sa) < 0 &&
	    sock_err() != SE_INPROGRESS)
	{
		e = sock_err();
		sock_close(fd);
		sock_seterr(e);
		return -1;
	}
	return fd;
}

/* The "message" of a 0x13 payload, escaped, into out[cap]. What its
** "code" makes it, as dch -a's exit status: DCH_EXIT_TAKEN for "taken"
** (#102), DCH_EXIT_REFUSED for any other (a dch serve refusal, #094), 1 for
** none or "busy": the one transient code (#095), a failure like a host that
** is down. */
static int
err_text(const unsigned char *d, size_t n, char *out, size_t cap)
{
	struct json_parser jp;
	char t[1024], m[256] = "", code[64];
	int coded = 1;

	if (n < sizeof t && !memchr(d, 0, n))
	{
		memcpy(t, d, n);
		t[n] = '\0';
		if (dch_json_parse(&jp, t) == 0 && jp.ntok > 0 &&
		    jp.tok[0].kind == JSON_OBJECT)
		{
			(void)jstr(&jp, 0, "message", m, sizeof m);
			if (jstr(&jp, 0, "code", code, sizeof code) == 1 &&
			    code[0] && strcmp(code, "busy"))
				coded = strcmp(code, "taken") ? DCH_EXIT_REFUSED
				                               : DCH_EXIT_TAKEN;
		}
		dch_json_free(&jp);
	}
	logsafe(out, cap, m[0] ? m : "error");
	return coded;
}

/* A history probe's host said hello (js): the 0x30 for its --history
** --json when it lists "ctl"; else PR_OLD. */
static void
hist_ask(struct probe *p, const char *js)
{
	static char *av[] = { "--history", "--json", NULL };
	struct sb rq = {0};

	if (!hello_op(js, "ctl"))
	{
		pr_end(p, PR_OLD, "does not support remote history (needs dch "
		       "serve 1.41+)");
		return;
	}
	/* a few dozen bytes into an idle socket: sent whole */
	ctl_payload(&rq, 2, av, NULL);
	if (rq.bad || send(p->fd, (const void *)rq.p, rq.n, MSG_NOSIGNAL) !=
	    (ssize_t)rq.n)
		pr_end(p, PR_FAIL, rq.bad ? "out of memory"
		       : sock_str(sock_err()));
	free(rq.p);
}

/* A history probe's ctl frames in p->in, as far as they have come: PR_OK
** once its dch exited 0, else PR_FAIL (or PR_REFUSED for a coded 0x13). */
static void
hist_in(struct probe *p)
{
	char why[100] = "", msg[300] = "";
	int xit = 1, code = 1;
	int r = ctl_in(&p->rs, &p->in, &p->hout, &p->herr, &xit, why,
	               sizeof why, msg, sizeof msg, &code, 0);

	p->herr.n = 0;		/* its stderr: not ours to show */
	if (p->hout.bad || p->herr.bad || p->hout.n > RM_HIST_MAX)
		pr_end(p, PR_FAIL, "sent too much");
	else if (r < 0)
		pr_end(p, PR_FAIL, msg);
	else if (r && msg[0])
		pr_end(p, code == 1 ? PR_FAIL : PR_REFUSED, msg);
	else if (r && (xit || why[0]))
		pr_end(p, PR_FAIL, "its dch --history --json failed");
	else if (r)
		pr_end(p, PR_OK, NULL);
}

static void
pr_frame(struct probe *p, const struct rf_frame *f)
{
	char *t, proto[32], why[400];
	int major;

	if (f->type == 0x13)
	{
		if (err_text(f->data, f->len, why + 6, sizeof why - 6) ==
		    DCH_EXIT_REFUSED)
			pr_end(p, PR_REFUSED, why + 6);
		else
		{
			memcpy(why, "says: ", 6);
			pr_end(p, PR_FAIL, why);
		}
		return;
	}
	if ((f->type != 0x18 || p->hello) && (f->type != 0x11 || p->list))
		return;		/* 0x14, 0x12, a repeat: not ours */
	if (memchr(f->data, 0, f->len) || !(t = malloc(f->len + 1)))
	{
		pr_end(p, PR_FAIL, "sent an unreadable reply");
		return;
	}
	memcpy(t, f->data, f->len);
	t[f->len] = '\0';
	if (f->type == 0x11)
	{
		p->list = t;
		t = NULL;
	}
	else if ((major = hello_major(t, proto, sizeof proto)) < 0)
		pr_end(p, PR_FAIL, "sent an unreadable hello (0x18)");
	else if (major > 1)
	{
		snprintf(why, sizeof why, "speaks protocol %s, newer than this "
		         "dch's 1.1", proto);
		pr_end(p, PR_NEWER, why);
	}
	else
	{
		p->hello = 1;
		if (p->hist)
			hist_ask(p, t);
		else if (p->hello_only || p->keep)
		{
			p->hello_js = t;
			t = NULL;
		}
	}
	free(t);
	if (p->st == PR_WAIT && p->hello && !p->hist &&
	    (p->list || p->hello_only))
	{
		if (p->keep)
			p->st = PR_OK;
		else
			pr_end(p, PR_OK, NULL);
	}
}

/* k more bytes from p's server: its frames, as far as they have come. */
static void
pr_feed(struct probe *p, const unsigned char *buf, size_t k)
{
	struct rf_frame f;
	size_t off = 0, used;
	int r;

	sb_add(&p->in, buf, k);
	if (p->in.bad)
	{
		pr_end(p, PR_FAIL, "sent too much");
		return;
	}
	/* a history probe past its hello: ctl frames, hist_in's */
	while (p->st == PR_WAIT && !(p->hist && p->hello) &&
	       (r = rf_parse(&p->rs, (const unsigned char *)p->in.p + off,
	                     p->in.n - off, (size_t)-1, &f, &used)) != RF_NEED)
	{
		off += used;
		if (r == RF_CLOSE)
			pr_end(p, PR_FAIL, "sent a frame over the size limit");
		else if (r == RF_FRAME)
			pr_frame(p, &f);
	}
	if (off)
	{
		memmove(p->in.p, p->in.p + off, p->in.n - off);
		p->in.n -= off;
	}
	if (p->st == PR_WAIT && p->hist && p->hello)
		hist_in(p);
}

static void
pr_read(struct probe *p)
{
	unsigned char buf[RM_READ];
	ssize_t k = sock_read(p->fd, buf, sizeof buf);
	int e = sock_err();

	if (k < 0 && (e == SE_INTR || e == SE_AGAIN))
		return;
	if (k <= 0)
		pr_end(p, PR_FAIL, k ? sock_str(e) : "closed the connection");
	else
		pr_feed(p, buf, (size_t)k);
}

/* Each probe's address (names by end) and its non-blocking connect:
** PR_CONN, else PR_FAIL. */
static void
pr_start(struct probe *v, int n, long long end)
{
	int i;

	resolve_all(v, n, end);
	for (i = 0; i < n; i++)
	{
		v[i].rs.from_server = 1;
		v[i].fd = -1;
		if ((v[i].fd = tcp_dial(&v[i])) < 0)
			pr_end(&v[i], PR_FAIL, sock_str(sock_err()));
		else
			v[i].st = PR_CONN;
	}
}

/* p's socket is ready: PR_CONN connected (its hello asked), PR_WAIT its
** reply. */
static void
pr_io(struct probe *p)
{
	static const unsigned char rq[] = { 0x08, 0, 0, 0, 0, 0x03, 0, 0, 0, 0 };
	size_t rqn = p->hello_only ? RF_HDR : sizeof rq;	/* one or all */
	socklen_t l = sizeof(int);
	int e = 0;

	if (p->st == PR_WAIT)
		pr_read(p);
	else if (getsockopt(p->fd, SOL_SOCKET, SO_ERROR, (void *)&e, &l) < 0
	         || e)
		pr_end(p, PR_FAIL, sock_str(e ? e : sock_err()));
	else if (send(p->fd, (const void *)rq, rqn, MSG_NOSIGNAL) !=
	         (ssize_t)rqn)
		pr_end(p, PR_FAIL, sock_str(sock_err()));
	else
		p->st = PR_WAIT;
}

/* The probes still going after ms: PR_FAIL; what each not kept read,
** freed. */
static void
pr_finish(struct probe *v, int n, int ms)
{
	int i;

	for (i = 0; i < n; i++)
	{
		if (v[i].st == PR_CONN || v[i].st == PR_WAIT)
		{
			char why[64];

			snprintf(why, sizeof why, "no answer within %d ms", ms);
			pr_end(&v[i], PR_FAIL, why);
		}
		if (v[i].st != PR_OK || !v[i].keep)
		{
			free(v[i].in.p);
			memset(&v[i].in, 0, sizeof v[i].in);
		}
	}
}

/* Every probe at once, all done within ms, name lookups included. Each
** ends PR_OK (hello and list), PR_NEWER or PR_FAIL, its socket closed (but
** a kept PR_OK's). */
static void
probe_all(struct probe *v, int n, int ms)
{
	long long end = now_ms() + ms;
	struct pollfd pf[RM_HOSTS];
	int map[RM_HOSTS], i, np;

	pr_start(v, n, end);
	for (;;)
	{
		long long left = end - now_ms();

		for (i = np = 0; i < n; i++)
			if (v[i].st == PR_CONN || v[i].st == PR_WAIT)
			{
				pf[np].fd = v[i].fd;
				pf[np].events = v[i].st == PR_CONN ? POLLOUT
				                                    : POLLIN;
				pf[np].revents = 0;
				map[np++] = i;
			}
		/* g_stop: the Windows picker's probe thread is cut short */
		if (!np || left <= 0 || g_stop)
			break;
		if (poll(pf, (nfds_t)np, left > 100 ? 100 : (int)left) < 0)
		{
			if (sock_err() == SE_INTR)
				continue;
			break;
		}
		for (i = 0; i < np; i++)
			if (pf[i].revents)
				pr_io(&v[map[i]]);
	}
	pr_finish(v, n, ms);
}

static void
probes_free(struct probe *v, int n)
{
	int i;

	for (i = 0; i < n; i++)
	{
		if (v[i].fd >= 0)
			sock_close(v[i].fd);
		free(v[i].list);
		free(v[i].hello_js);
		free(v[i].in.p);
		free(v[i].hout.p);
		free(v[i].herr.p);
	}
	free(v);
}

static int
probe_cmp(const void *x, const void *y)
{
	return strcmp(((const struct probe *)x)->h.label,
	              ((const struct probe *)y)->h.label);
}

/* Every host's history (#115) within ms, in label order: the records of
** those that answered, then the cached ones (stale) of those that did not,
** for RM_TTL_S, as rows_now's rows. Saves the history cache. loud: a host
** that speaks a newer protocol, refuses us or has no ctl is said on
** stderr, and shows nothing. */
static void
hist_now(const struct rhost *hv, int n, int ms, struct hlist *l, int loud)
{
	struct probe *pv;
	struct hlist old = {0};
	struct dch_rrec *r;
	long long now = (long long)time(NULL);
	int i, k;

	if (n <= 0 || !(pv = calloc((size_t)n, sizeof *pv)))
		return;
	for (i = 0; i < n; i++)
	{
		pv[i].h = hv[i];
		pv[i].hello_only = pv[i].hist = 1;
	}
	qsort(pv, (size_t)n, sizeof *pv, probe_cmp);
	probe_all(pv, n, ms);
	hist_cload(&old, now);
	for (i = 0; i < n; i++)
	{
		const char *lab = pv[i].h.label;

		if (pv[i].st == PR_OK && pv[i].hout.p &&
		    hist_doc(pv[i].hout.p, lab, now, l) == 0)
			continue;
		if (pv[i].st == PR_NEWER || pv[i].st == PR_REFUSED ||
		    pv[i].st == PR_OLD)
		{
			if (loud)
				fprintf(stderr, pv[i].st == PR_NEWER ? "dch: %s %s; "
				        "skipped\n" : pv[i].st == PR_REFUSED ? "dch: %s "
				        "refused: %s\n" : "dch: %s: %s\n", lab,
				        pv[i].why);
			continue;
		}
		/* 0 < at <= now + 60: no overflow */
		for (k = 0; k < old.n; k++)
			if (!strcmp(old.v[k].host, lab) &&
			    old.v[k].at >= now - RM_TTL_S && (r = hl_new(l)))
			{
				*r = old.v[k];
				r->stale = 1;
				old.v[k].json = NULL;	/* l's now */
				l->n++;
			}
	}
	dch_remote_hfree(old.v, old.n);
	probes_free(pv, n);
	hist_save(l);
}

/* Probe every host within ms, the tailscale call and name lookups
** included; the rows of those that answered, then the cached rows (stale)
** of those that did not, for RM_TTL_S. Saves the cache, and sets the
** candidate labels (written to lfd too, then closed, when lfd >= 0). loud:
** a host that speaks a newer protocol is said on stderr. hist: then their
** history too, in RM_BG_MS more (the picker's background probe, #115). */
static void
rows_now(struct rlist *l, int ms, int loud, int lfd, int hist)
{
	struct rhost hv[RM_HOSTS];
	struct probe *pv;
	struct rlist old = {0};
	long long now = (long long)time(NULL), end = now_ms() + ms, left;
	int n = candidates(hv, RM_HOSTS, ms < RM_TS_MS ? ms : RM_TS_MS, loud);
	int i, k;

	if (n >= 0)
		labels_set(hv, n);
	if (lfd >= 0)
	{
		if (n >= 0)
			(void)!write(lfd, g_lab, strlen(g_lab));
		close(lfd);
	}
	if (n <= 0 || !(pv = calloc((size_t)n, sizeof *pv)))
		return;
	for (i = 0; i < n; i++)
		pv[i].h = hv[i];
	left = end - now_ms();
	probe_all(pv, n, left > 0 ? (int)left : 0);
	cache_load(&old, now);
	for (i = 0; i < n; i++)
	{
		if (pv[i].st == PR_OK &&
		    rows_doc(pv[i].list, hv[i].label, now, l) == 0)
			continue;
		if (pv[i].st == PR_NEWER)
		{
			if (loud)
				fprintf(stderr, "dch: %s %s; skipped\n",
				        hv[i].label, pv[i].why);
			continue;
		}
		if (pv[i].st == PR_REFUSED)
		{
			/* not down: its cached rows are no use to us */
			if (loud)
				fprintf(stderr, "dch: %s refused: %s\n",
				        hv[i].label, pv[i].why);
			continue;
		}
		for (k = 0; k < old.n; k++)
		{
			struct dch_rrow *r;

			/* 0 < at <= now + 60: no overflow */
			if (strcmp(old.v[k].host, hv[i].label) ||
			    old.v[k].at < now - RM_TTL_S || !(r = rl_new(l)))
				continue;
			*r = old.v[k];
			r->stale = 1;
			l->n++;
		}
	}
	free(old.v);
	probes_free(pv, n);
	cache_save(l);
	if (hist)
	{
		struct hlist hl = {0};

		hist_now(hv, n, RM_BG_MS, &hl, 0);
		dch_remote_hfree(hl.v, hl.n);
	}
}

#ifdef _WIN32
/* The picker's background probe on Windows (#090): a thread, where POSIX
** forks a child. While it runs it owns what rows_now touches (g_lab, g_me,
** g_ts, the cache's writes, bg_new); the picker's thread takes its rows
** once it has ended, and touches none of that before: it shows no cached
** rows by label, so never reads g_lab. */
static HANDLE bg_th;
static struct rlist bg_new, bg_last;
static int bg_got;		/* bg_last is a probe's */
static long long bg_at = -RM_EVERY_MS;

static DWORD WINAPI
bg_run(void *arg)
{
	(void)arg;
	rows_now(&bg_new, RM_BG_MS, 0, -1, 0);
	return 0;
}

/* The probe thread, cut short (g_stop: rf_run and probe_all give up at
** once) and joined; its rows dropped. */
static void
bg_stop(void)
{
	if (!bg_th)
		return;
	g_stop = 1;
	WaitForSingleObject(bg_th, INFINITE);
	CloseHandle(bg_th);
	bg_th = NULL;
	g_stop = 0;
	free(bg_new.v);
	memset(&bg_new, 0, sizeof bg_new);
}

/* The picker's rows into l: the last probe's (a silent host's stale), re-
** probed behind it every RM_EVERY_MS; until one has ended, the cache's
** within RM_TTL_S, stale past RM_FRESH_S, whatever host they are of.
** ponytail: a name lookup (--hosts with names) is not cut short, so the
** join can take up to RM_BG_MS; literal addresses (discovery) do not. */
static void
bg_rows(struct rlist *l, long long t)
{
	int i, k;

	if (bg_th && WaitForSingleObject(bg_th, 0) == WAIT_OBJECT_0)
	{
		CloseHandle(bg_th);
		bg_th = NULL;
		free(bg_last.v);
		bg_last = bg_new;
		memset(&bg_new, 0, sizeof bg_new);
		bg_got = 1;
	}
	if (!bg_th && now_ms() - bg_at >= RM_EVERY_MS)
	{
		bg_at = now_ms();
		bg_th = CreateThread(NULL, 0, bg_run, NULL, 0, NULL);
	}
	if (bg_got)
	{
		if (bg_last.n && (l->v = malloc((size_t)bg_last.n * sizeof *l->v)))
		{
			memcpy(l->v, bg_last.v, (size_t)bg_last.n * sizeof *l->v);
			l->n = l->cap = bg_last.n;
		}
		return;
	}
	cache_load(l, t);
	for (i = k = 0; i < l->n; i++)
		if (l->v[i].at >= t - RM_TTL_S)
		{
			l->v[k] = l->v[i];
			l->v[k++].stale = l->v[i].at < t - RM_FRESH_S;
		}
	l->n = k;
}
#else
static pid_t bg_pid = -1;
static long long bg_at = -RM_EVERY_MS;
static long long bg_hist_at = -RM_HIST_EVERY_MS;	/* < 0: none yet */
static int lab_fd = -1;		/* the bg child's labels, read end */
static char lab_in[sizeof g_lab];
static size_t lab_n;

/* The bg child's labels, as far as they have come; at EOF they are the
** candidates (none sent: it could not tell, the old ones stand). */
static void
labels_poll(void)
{
	ssize_t k;

	while (lab_fd >= 0 && lab_n < sizeof lab_in - 1)
	{
		if ((k = read(lab_fd, lab_in + lab_n, sizeof lab_in - 1 - lab_n))
		    > 0)
		{
			lab_n += (size_t)k;
			continue;
		}
		if (k < 0 && errno == EINTR)
			continue;
		if (k < 0 && errno == EAGAIN)
			return;
		break;
	}
	if (lab_fd < 0)
		return;
	if (lab_n && lab_in[0] == '\n' && lab_in[lab_n - 1] == '\n')
	{
		memcpy(g_lab, lab_in, lab_n);
		g_lab[lab_n] = '\0';
		g_lab_ok = 1;
	}
	close(lab_fd);
	lab_fd = -1;
	lab_n = 0;
}
#endif

/* --ls's time box: DCH_REMOTE_BOX_MS for a slower link (or a sanitizer
** build), else RM_BOX_MS. */
static int
box_ms(void)
{
	const char *e = getenv("DCH_REMOTE_BOX_MS");
	long box = e ? atol(e) : 0;

	return box > 0 && box <= 60000 ? (int)box : RM_BOX_MS;
}

static int bg_hist_tab;	/* the picker's History tab shows */

/* The rows: now, probed in the foreground (--ls); else the picker's, from
** the cache, re-probed behind it (on Windows bg_rows'). */
int
dch_remote_rows(struct dch_rrow **out, int now)
{
	struct rlist l = {0};
	long long t = (long long)time(NULL);
#ifndef _WIN32
	int i, k, lp[2];
#endif

	*out = NULL;
	if (!remote_on())
		return 0;
	if (now)
	{
#ifdef _WIN32
		bg_stop();	/* one probe at a time: they share rows_now's state */
#endif
		rows_now(&l, box_ms(), 1, -1, 0);
	}
#ifdef _WIN32
	else
		bg_rows(&l, t);
#else
	else
	{
		/* the picker: the cache, re-probed behind it */
		if (bg_pid > 0 && waitpid(bg_pid, NULL, WNOHANG) != 0)
			bg_pid = -1;
		labels_poll();
		if (bg_pid < 0 && lab_fd < 0 && now_ms() - bg_at >= RM_EVERY_MS &&
		    pipe_ce(lp) == 0)
		{
			/* the history less often: each is a dch run there;
			** once at the picker's open (the tab's count), then
			** only while its History tab shows (#163) */
			int hist = now_ms() - bg_hist_at >= RM_HIST_EVERY_MS &&
			           (bg_hist_tab || bg_hist_at < 0);

			bg_at = now_ms();
			if (hist)
				bg_hist_at = bg_at;
			if ((bg_pid = fork()) == 0)
			{
				struct rlist bl = {0};
				int nul = open("/dev/null", O_RDWR);

				child_signals();
				close(lp[0]);
				if (nul >= 0)
				{
					dup2(nul, 0);
					dup2(nul, 1);
					dup2(nul, 2);
				}
				rows_now(&bl, RM_BG_MS, 0, lp[1], hist);
				_exit(0);
			}
			close(lp[1]);
			if (bg_pid < 0)
				close(lp[0]);
			else
			{
				lab_fd = lp[0];
				nonblock(lab_fd);
			}
		}
		/* hosts the user named are known at once; discovery's come
		** from the bg child */
		if (!g_lab_ok)
		{
			struct rhost hv[RM_HOSTS];
			int n = named_hosts(hv, RM_HOSTS, 0);

			if (n >= 0)
				labels_set(hv, n);
		}
		cache_load(&l, t);
		for (i = k = 0; i < l.n; i++)
			if (label_in(l.v[i].host) && l.v[i].at >= t - RM_TTL_S)
			{
				l.v[k] = l.v[i];
				l.v[k++].stale = l.v[i].at < t - RM_FRESH_S;
			}
		l.n = k;
	}
#endif
	if (l.n > 1)
		qsort(l.v, (size_t)l.n, sizeof *l.v, row_cmp);
	*out = l.v;
	return l.n;
}

int
dch_remote_history(struct dch_rrec **out, int now)
{
	struct hlist l = {0};

	*out = NULL;
	if (!remote_on())
		return 0;
	if (now)
	{
		/* --ls --remote's box, the tailscale call included */
		struct rhost hv[RM_HOSTS];
		int ms = box_ms(), n;
		long long end = now_ms() + ms, left;

#ifdef _WIN32
		bg_stop();	/* rows_now's state is the probe thread's */
#endif
		n = candidates(hv, RM_HOSTS, ms < RM_TS_MS ? ms : RM_TS_MS, 1);
		left = end - now_ms();
		hist_now(hv, n, left > 0 ? (int)left : 0, &l, 1);
	}
#ifndef _WIN32
	else
	{
		/* the picker's: the cache the bg child keeps, of the hosts
		** that are candidates now (dch_remote_rows' labels) */
		long long t = (long long)time(NULL);
		int i, k;

		hist_cload(&l, t);
		for (i = k = 0; i < l.n; i++)
			if (label_in(l.v[i].host) && l.v[i].at >= t - RM_TTL_S)
			{
				l.v[k] = l.v[i];
				l.v[k++].stale = l.v[i].at < t - RM_HIST_FRESH_S;
			}
			else
				free(l.v[i].json);
		l.n = k;
	}
#endif
	*out = l.v;
	return l.n;
}

/* The picker is done: its bg child goes (SIGTERM waits out a cache write)
** and is reaped, so none outlives it as a zombie; on Windows its thread is
** joined, before an attach runs rows_now's discovery itself. */
void
dch_remote_stop(void)
{
#ifdef _WIN32
	bg_stop();
	free(bg_last.v);
	memset(&bg_last, 0, sizeof bg_last);
	bg_got = 0;
#else
	if (bg_pid > 0)
	{
		kill(bg_pid, SIGTERM);
		while (waitpid(bg_pid, NULL, 0) < 0 && errno == EINTR)
			;
	}
	bg_pid = -1;
	if (lab_fd >= 0)
		close(lab_fd);
	lab_fd = -1;
	lab_n = 0;
	bg_hist_at = -RM_HIST_EVERY_MS;	/* the next picker fetches once */
#endif
}

void
dch_remote_hist_tab(int shown)
{
	bg_hist_tab = shown;
}

void
dch_remote_esc(char *out, size_t cap, const char *s)
{
	logsafe(out, cap, s);
}

/* ---- attach ---------------------------------------------------------- */

int dch_replay_sb = -1;

int
dch_scrollback_num(const char *s)
{
	char *tail;
	long v;

	if (!s || *s < '0' || *s > '9')
		return -1;
	errno = 0;
	v = strtol(s, &tail, 10);
	if (*tail)
		return -1;
	return errno == ERANGE || v > DCH_SCROLLBACK_MAX ? DCH_SCROLLBACK_MAX
	                                                 : (int)v;
}

int
dch_replay_scrollback(void)
{
	int v;

	if (dch_replay_sb >= 0)
		return dch_replay_sb;
	v = dch_scrollback_num(getenv("DCH_REPLAY_SCROLLBACK"));
	return v < 0 ? 0 : v;
}

static void
put_frame(struct sb *b, unsigned int type, const void *p, size_t n)
{
	unsigned char h[RF_HDR];

	h[0] = (unsigned char)type;
	h[1] = (unsigned char)(n >> 24);
	h[2] = (unsigned char)(n >> 16);
	h[3] = (unsigned char)(n >> 8);
	h[4] = (unsigned char)n;
	sb_add(b, h, sizeof h);
	sb_add(b, p, n);
}

static void
put_size(struct sb *b, unsigned int type, const char *name,
         const struct winsize *ws)
{
	struct sb j = {0};

	sb_s(&j, "{\"cols\":");
	sb_ll(&j, ws->ws_col ? ws->ws_col : 80);
	if (name)
	{
		sb_s(&j, ",\"name\":");
		sb_q(&j, name);
	}
	sb_s(&j, ",\"rows\":");
	sb_ll(&j, ws->ws_row ? ws->ws_row : 24);
	/* #101: optional, an older server ignores it */
	if (type == 0x01 && dch_replay_scrollback() > 0)
	{
		sb_s(&j, ",\"scrollback\":");
		sb_ll(&j, dch_replay_scrollback());
	}
	sb_s(&j, "}");
	if (j.bad)
		b->bad = 1;
	else
		put_frame(b, type, j.p, j.n);
	free(j.p);
}

#ifndef _WIN32
/* The next packet attach.c wrote in b[0..n) (type, u16 little-endian len,
** the payload packet_payload_len sizes): 1 with *pk and *used, 0 for more
** bytes first, -1 for one no attach client sends. Pure. */
static int
pkt_next(const unsigned char *b, size_t n, struct packet *pk, size_t *used)
{
	size_t k;

	if (n < PKT_HDR)
		return 0;
	pk->type = b[0];
	pk->len = (unsigned short)(b[1] | b[2] << 8);
	if (pk->type > MSG_REDRAW)
		return -1;
	if ((k = packet_payload_len(pk)) > sizeof pk->u.buf)
		return -1;
	if (n - PKT_HDR < k)
		return 0;
	memcpy(pk->u.buf, b + PKT_HDR, k);
	*used = PKT_HDR + k;
	return 1;
}

/* An attach packet as frames: keys as 0x10, a size as 0x05, a redraw as
** 0x05 then 0x07 (but the first: the attach itself paints). */
static void
pkt_frames(const struct packet *pk, int *redraws, struct sb *out)
{
	if (pk->type == MSG_PUSH && pk->len)
		put_frame(out, 0x10, pk->u.buf, pk->len);
	else if (pk->type == MSG_WINCH || pk->type == MSG_REDRAW)
		put_size(out, 0x05, NULL, &pk->u.ws);
	if (pk->type == MSG_REDRAW && (*redraws)++)
		put_frame(out, 0x07, "", 0);
}
#endif

/* Forget the sent head of b, off bytes, when it is all of b or big. */
static void
sb_drop(struct sb *b, size_t *off)
{
	if (*off == b->n)
		b->n = *off = 0;
	else if (*off > (1u << 20))
	{
		memmove(b->p, b->p + *off, b->n - *off);
		b->n -= *off;
		*off = 0;
	}
}

/* A 0x12's payload: 1 when it says "why":"detach" (#217: the host's dch -d
** detached its attach client, the session lives on), else 0. Pure. */
static int
end_detach(const unsigned char *d, size_t n)
{
	struct json_parser jp;
	char t[256], why[16];
	int k = 0;

	if (n >= sizeof t || memchr(d, 0, n))
		return 0;
	memcpy(t, d, n);
	t[n] = '\0';
	if (dch_json_parse(&jp, t) == 0 && jp.ntok > 0 &&
	    jp.tok[0].kind == JSON_OBJECT)
		k = jstr(&jp, 0, "why", why, sizeof why) == 1 &&
		    !strcmp(why, "detach");
	dch_json_free(&jp);
	return k;
}

/* The server's frames in tin as the relay takes them: 0x10 onto to_sp,
** 0x12 sets *exited (2 when it says "why":"detach", #217), the first 0x13's message into msg[cap] (escaped, kept
** whatever ends the link after it) and what its code makes the exit status
** into *code (err_text). What it parsed leaves tin. -1 when the link must
** end: a frame over the limit. Pure. */
static int
relay_in(struct rf_state *rs, struct sb *tin, struct sb *to_sp, int *exited,
         int *code, char *msg, size_t cap)
{
	struct rf_frame f;
	size_t used = 0, off = 0;
	int r, rc = 0;

	while (tin->n && !tin->bad &&
	       (r = rf_parse(rs, (const unsigned char *)tin->p + off,
	                     tin->n - off, (size_t)-1, &f, &used)) != RF_NEED)
	{
		off += used;
		if (r == RF_CLOSE)
		{
			snprintf(msg, cap, "it sent a frame over the size limit");
			rc = -1;
			break;
		}
		if (f.type == 0x10 && f.len)
			sb_add(to_sp, f.data, f.len);
		else if (f.type == 0x12 && r == RF_FRAME)
			*exited = 1 + end_detach(f.data, f.len);
		else if (f.type == 0x13 && r == RF_FRAME && !msg[0])
		{
			char m[300];

			*code = err_text(f.data, f.len, m, sizeof m);
			snprintf(msg, cap, "%s%s", *code == 1 ? "it says: " : "",
			         m);
		}
	}
	if (off)
	{
		memmove(tin->p, tin->p + off, tin->n - off);
		tin->n -= off;
	}
	return rc;
}

/* The line that says why a remote attach to label ended without the
** session ending, for exit status code (relay_in): taken, refused, or the
** link lost. */
static void
end_line(char *line, size_t cap, int code, const char *label, const char *msg)
{
	char safe[500];

	logsafe(safe, sizeof safe, msg);
	if (code == DCH_EXIT_TAKEN)
		snprintf(line, cap, "dch: %s: %s", label, safe);
	else if (code == DCH_EXIT_REFUSED)
		snprintf(line, cap, "dch: %s refused: %s", label, safe);
	else
		snprintf(line, cap, "dch: lost the connection to %s%s%s",
		         label, safe[0] ? ": " : "", safe);
}

#ifndef _WIN32
#define RELAY_DETACHED 3	/* not a dch -a status: the relay's own */

/* The session's screen as the host's stream sets it (#231): b's bytes from
** n0 on go through *st (how much of ESC[?1049 matched so far) into *alt (1
** on the alternate screen). Where the first ESC[?1049h starts, backed over
** the ESC[?<n>h sets right before it (a re-arm's modes, master.c dec_modes
** order), or (size_t)-1 when there is none. Pure. */
static size_t
alt_scan(const struct sb *b, size_t n0, int *st, int *alt)
{
	static const char pre[] = "\033[?1049";
	size_t i, at = (size_t)-1;

	for (i = n0; i < b->n; i++)
	{
		unsigned char c = (unsigned char)b->p[i];

		if (*st == 7 && (c == 'h' || c == 'l'))
		{
			*st = 0;
			*alt = c == 'h';
			if (*alt && at == (size_t)-1)
			{
				size_t k;

				at = i >= 7 ? i - 7 : 0;
				while (at > 0 && b->p[at - 1] == 'h')
				{
					for (k = at - 1; k > 0 &&
					     isdigit((unsigned char)b->p[k - 1]); k--)
						;
					if (k == at - 1 || k < 3 ||
					    memcmp(b->p + k - 3, "\033[?", 3))
						break;
					at = k - 3;
				}
			}
		}
		else if (*st < 7 && c == (unsigned char)pre[*st])
			(*st)++;
		else
			*st = c == '\033';
	}
	return at;
}

/* The relay child: the attach client's packets on sp as frames on tcp,
** 0x10 back raw; tin and rs hold what followed the list on tcp. 0 when the
** session ended (0x12) or the attach side closed (a detach: the TCP close
** is all, nothing is killed); RELAY_DETACHED when the host detached its
** client (a 0x12 with "why":"detach"); else dch -a's exit status
** (remote.h), with why the connection ended written to `why`. */
static int
relay(int tcp, int sp, int why, const char *label, struct sb tin,
      struct rf_state rs)
{
	struct sb to_tcp = {0}, to_sp = {0};
	unsigned char pin[PKT_HDR + PKT_MAX], buf[RM_READ];
	size_t pn = 0, toff = 0, soff = 0;
	int redraws = 0, eof = 0, exited = 0, code = 1, held = 0;
	int st = 0, alt = 0, attaches = 0;
	long long drain = 0, rearm = 0;
	char msg[400] = "";

	nonblock(tcp);
	nonblock(sp);
	if (relay_in(&rs, &tin, &to_sp, &exited, &code, msg, sizeof msg) < 0)
	{
		eof = 1;
		drain = now_ms() + 2000;
	}
	(void)alt_scan(&to_sp, 0, &st, &alt);
	for (;;)
	{
		size_t qt = to_tcp.n - toff, qs = to_sp.n - soff;
		struct pollfd pf[2];
		ssize_t k;
		long long now = now_ms();
		int hold, tmo = -1;

		if (eof && (!qs || now >= drain))
			break;
		/* a host that never re-arms (before 1.58.0, or the 0x07
		** lost): what it sent meanwhile goes out after all (#231) */
		if (rearm && now >= rearm)
			rearm = 0;
		hold = rearm && !eof;
		if (hold)
			tmo = (int)(rearm - now);
		pf[0].fd = eof ? -1 : tcp;
		pf[0].events = (short)((qs < RM_HIGH ? POLLIN : 0) |
		                       (qt ? POLLOUT : 0));
		pf[1].fd = sp;
		pf[1].events = (short)((qt < RM_HIGH && !eof ? POLLIN : 0) |
		                       (qs && !hold ? POLLOUT : 0));
		pf[0].revents = pf[1].revents = 0;
		if (poll(pf, 2, eof ? 100 : tmo) < 0)
		{
			if (errno == EINTR)
				continue;
			snprintf(msg, sizeof msg, "poll: %s", strerror(errno));
			break;
		}
		/* to the terminal */
		if (qs && !hold && pf[1].revents & (POLLOUT | POLLHUP | POLLERR))
		{
			k = write(sp, to_sp.p + soff, qs);
			if (k < 0 && errno != EINTR && errno != EAGAIN)
				return 0;	/* the attach side is gone */
			if (k > 0)
				soff += (size_t)k;
			sb_drop(&to_sp, &soff);
		}
		/* from the keyboard */
		if (!eof && pf[1].revents & (POLLIN | POLLHUP | POLLERR))
		{
			struct packet pk;
			size_t used = 0, off = 0;
			int r;

			k = read(sp, pin + pn, sizeof pin - pn);
			if (k == 0 || (k < 0 && errno != EINTR &&
			               errno != EAGAIN))
				return 0;	/* detached */
			if (k > 0)
				pn += (size_t)k;
			while ((r = pkt_next(pin + off, pn - off, &pk, &used)) > 0)
			{
				/* a ctrl-z suspend (#211): the client reset its
				** terminal off the session's screen, so the host's
				** output until the resume is dropped (the link is
				** still read); the resume's 0x07 repaints */
				if (pk.type == MSG_DETACH || pk.type == MSG_ATTACH)
					held = pk.type == MSG_DETACH;
				if (held)
					to_sp.n = soff = 0;
				/* a resume, or the X-ray overlay's return (not the
				** first attach): until the host's re-arm comes back
				** an RTT later, what it sends is for the screen the
				** client's reset left: hold it, the re-arm's 1049h
				** drops it (#231) */
				if (pk.type == MSG_ATTACH && attaches++ && alt)
					rearm = now_ms() + RM_REARM_MS;
				pkt_frames(&pk, &redraws, &to_tcp);
				off += used;
			}
			if (r < 0)
				return 0;	/* not attach.c: drop the link */
			memmove(pin, pin + off, pn - off);
			pn -= off;
		}
		/* to the server */
		if (!eof && qt && pf[0].revents & (POLLOUT | POLLHUP | POLLERR))
		{
			k = send(tcp, to_tcp.p + toff, qt, MSG_NOSIGNAL);
			if (k > 0)
				toff += (size_t)k;
			else if (k < 0 && errno != EINTR && errno != EAGAIN)
			{
				if (!msg[0])
					snprintf(msg, sizeof msg, "%s",
					         strerror(errno));
				eof = 1;
				drain = now_ms() + 2000;
			}
			sb_drop(&to_tcp, &toff);
		}
		/* from the server */
		if (!eof && pf[0].revents & (POLLIN | POLLHUP | POLLERR))
		{
			k = read(tcp, buf, sizeof buf);
			if (k < 0 && (errno == EINTR || errno == EAGAIN))
				continue;
			if (k <= 0)
			{
				/* a 0x13 then a reset: the 0x13 says why */
				if (k < 0 && !msg[0])
					snprintf(msg, sizeof msg, "%s",
					         strerror(errno));
				eof = 1;
			}
			else
			{
				size_t n0 = to_sp.n, at;

				sb_add(&tin, buf, (size_t)k);
				if (relay_in(&rs, &tin, &to_sp, &exited, &code,
				             msg, sizeof msg) < 0)
					eof = 1;
				at = alt_scan(&to_sp, n0, &st, &alt);
				if (held)
					to_sp.n = soff = 0;
				else if (rearm && at != (size_t)-1)
				{
					rearm = 0;	/* the re-arm: from it on */
					if (at > soff)
						soff = at;
				}
				else if (rearm && to_sp.n - soff > RM_HIGH / 2)
				{
					/* stale either way: keep a tail a
					** re-arm's start may be in */
					soff = to_sp.n - 128;
					sb_drop(&to_sp, &soff);
				}
			}
			if (tin.bad || to_sp.bad || to_tcp.bad)
			{
				snprintf(msg, sizeof msg, "out of memory");
				eof = 1;
			}
			if (eof)
				drain = now_ms() + 2000;
		}
	}
	if (exited)	/* the session ended, whatever came after */
		return exited == 2 ? RELAY_DETACHED : 0;
	{
		char line[800];

		end_line(line, sizeof line, code, label, msg);
		(void)!write(why, line, strlen(line));
	}
	return code;
}

static pid_t relay_pid = -1;
static int why_fd = -1, relay_st = -1;

/* The relay's wait status once it closed the session socket, reaped once
** (the X-ray overlay may ask before the main loop does); -1: none. */
static int
relay_status(void)
{
	if (relay_pid > 0)
	{
		while (waitpid(relay_pid, &relay_st, 0) < 0 && errno == EINTR)
			;
		relay_pid = -1;
	}
	return relay_st;
}

/* attach.c's hook, when the relay closed the session socket: whether the
** host detached its client (#217), so the X-ray overlay leaves too. */
static int
remote_detached(void)
{
	int st = relay_status();

	return st != -1 && WIFEXITED(st) && WEXITSTATUS(st) == RELAY_DETACHED;
}

/* attach.c's hook, when the relay closed the session socket: NULL for a
** session that ended or a detach, else what to say once the terminal is
** back, with the exit status in *code (the relay's; 1 for one that died). */
static const char *
remote_lost(int *code)
{
	static char msg[900];
	ssize_t k = -1;
	int st = relay_status();

	if (st == -1 || (WIFEXITED(st) && (WEXITSTATUS(st) == 0 ||
	                                   WEXITSTATUS(st) == RELAY_DETACHED)))
		return NULL;
	*code = WIFEXITED(st) ? WEXITSTATUS(st) : 1;
	if (why_fd >= 0)
		k = read(why_fd, msg, sizeof msg - 1);
	if (k <= 0)
		snprintf(msg, sizeof msg, "dch: lost the connection");
	else
		msg[k] = '\0';
	return msg;
}
#endif

/* TCP keepalive, so a link that died silently ends the attach in about
** half a minute rather than never; dch serve's connections too, so a dead
** peer's idle --follow lets its session's slots go (QA 1.55.0 LOW-5). */
void
keepalive(int fd)
{
	int on = 1, idle = 15, intvl = 5, cnt = 3;

	/* (const void *): Winsock takes a const char * */
	setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, (const void *)&on, sizeof on);
	setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, (const void *)&on, sizeof on);
#ifdef TCP_KEEPIDLE
	setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, (const void *)&idle,
	           sizeof idle);
#elif defined(TCP_KEEPALIVE)
	setsockopt(fd, IPPROTO_TCP, TCP_KEEPALIVE, (const void *)&idle,
	           sizeof idle);
#endif
#ifdef TCP_KEEPINTVL
	setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, (const void *)&intvl,
	           sizeof intvl);
#endif
#ifdef TCP_KEEPCNT
	setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, (const void *)&cnt,
	           sizeof cnt);
#endif
	(void)idle;
	(void)intvl;
	(void)cnt;
}

/* For a host discovery found: does `tailscale whois` on its address name
** this host's own Tailscale user, untagged? Fails closed. */
static int
peer_is_mine(const struct rhost *h)
{
	char ip[sizeof h->addr], node[NODE_ID_MAX], login[256], *out;
	long long uid;
	int tagged, ok;

	copy_str(ip, sizeof ip, h->addr);
	if (g_me < 0 || ts_run("whois", "--json", ip, NULL, &out, 256 << 10) < 0)
		return 0;
	ok = whois_user(out, &uid, node, login, sizeof login, &tagged) == 0 &&
	     uid == g_me && !tagged;
	free(out);
	return ok;
}

/* A connection to h that has listed name: its socket, with what followed
** the list in *rest and the parser's state in *rs, for the 0x01 to follow
** on it; or -1, why said on stderr (-2: the host's gate refused us). Prosper's 0x01 creates a session it
** does not have, so the check and the attach share the connection: no
** session can end between them unseen. */
static int
open_listed(const struct rhost *h, const char *name, const char *sh,
            const char *sn, struct sb *rest, struct rf_state *rs)
{
	struct probe *p = calloc(1, sizeof *p);
	struct rlist rows = {0};
	int k, found = 0, fd = -1;

	if (!p)
		return -1;
	p->h = *h;
	p->keep = 1;
	probe_all(p, 1, RM_ATTACH_MS);
	if (p->st == PR_OK && rows_doc(p->list, h->label, 0, &rows) == 0)
		for (k = 0; k < rows.n && !found; k++)
			found = !strcmp(rows.v[k].name, name);
	if (p->st == PR_REFUSED)
	{
		fprintf(stderr, "dch: %s refused: %s\n", sh, p->why);
		fd = -2;
	}
	else if (p->st != PR_OK)
		fprintf(stderr, "dch: %s: %s\n", sh, p->why);
	else if (!found)
		fprintf(stderr, "dch: no session %s on %s\n", sn, sh);
	else
	{
		fd = p->fd;
		p->fd = -1;
		*rest = p->in;
		memset(&p->in, 0, sizeof p->in);
		*rs = p->rs;
	}
	free(rows.v);
	probes_free(p, 1);
	return fd;
}

#ifdef _WIN32
/* attach.c's double-press window, in ms: DCH_DOUBLE_TAP_MS (0 to 5000; 0:
** none, one press acts at once), else 300. */
static long
tap_ms(void)
{
	const char *e = getenv("DCH_DOUBLE_TAP_MS");
	char *tail;
	long v;

	if (e && *e && (v = strtol(e, &tail, 10), !*tail) && v >= 0 && v <= 5000)
		return v;
	return 300;
}

/* The Windows attach (#077): no attach.c and no relay child, one loop. It
** waits on the socket's event and the console's input: keys go out as
** 0x10, a new window size as 0x05, the server's 0x10 to the console raw.
** The detach key (Ctrl-\, 0x1c) ends it here as attach.c's does: one
** press detaches, two within tap_ms switch to the picker (#090), or the
** other way round with detach_key = switch; none of a burst with a press
** in it goes. The TCP close is the detach, the session lives on. 0 for a
** detach or a session that ended (0x12); ATTACH_SWITCH for the picker;
** else dch -a's exit status (remote.h: 1 a lost link, or taken, refused),
** said once the console is restored (win/plat.c restores it on every other
** way out too). */
static int
win_relay(int fd, const char *label, const char *name, struct sb tin,
          struct rf_state rs)
{
	struct sb to_tcp = {0}, to_con = {0};
	struct winsize ws, now;
	unsigned char buf[RM_READ], kb[4096];
	size_t toff = 0;
	int exited = 0, eof = 0, gone = 0, sw = 0, hits, k, r, code = 1;
	char msg[400] = "", line[800], dk[16];
	WSAEVENT ev = WSA_INVALID_EVENT;
	long dbl = tap_ms();
	long long tap = 0;	/* a lone press stands at this now_ms; 0 none */
	int swap = dch_config_get("detach_key", dk, sizeof dk) &&
	           !strcmp(dk, "switch");

	if ((r = con_raw()) < 0)
	{
		fprintf(stderr, r == -2 ? "dch: this console has no VT mode "
		        "(Windows 10 or later)\n" : "dch: attaching requires a "
		        "terminal\n");
		free(tin.p);
		return 1;
	}
	con_size(&ws);
	put_size(&to_tcp, 0x01, name, &ws);
	if ((ev = WSACreateEvent()) == WSA_INVALID_EVENT ||
	    WSAEventSelect((SOCKET)fd, ev, FD_READ | FD_WRITE | FD_CLOSE))
	{
		snprintf(msg, sizeof msg, "%s", sock_str(WSAGetLastError()));
		eof = 1;
	}
	else if (relay_in(&rs, &tin, &to_con, &exited, &code, msg,
	                  sizeof msg) < 0)
		eof = 1;
	while (!eof && !exited && !gone)
	{
		HANDLE hv[2];
		WSANETWORKEVENTS ne;
		DWORD nh = 1, w;
		long long left;

		/* to the console, whole, before more is read */
		if (to_con.n && con_write(to_con.p, to_con.n) < 0)
			gone = 1;	/* the console went: nobody to tell */
		to_con.n = 0;
		/* to the server, as far as it takes it now */
		while (!gone && toff < to_tcp.n)
		{
			k = send((SOCKET)fd, to_tcp.p + toff,
			         (int)(to_tcp.n - toff), 0);
			if (k > 0)
			{
				toff += (size_t)k;
				continue;
			}
			if (WSAGetLastError() != WSAEWOULDBLOCK)
			{
				snprintf(msg, sizeof msg, "%s",
				         sock_str(WSAGetLastError()));
				eof = 1;
			}
			break;
		}
		sb_drop(&to_tcp, &toff);
		if (!eof && (tin.bad || to_con.bad || to_tcp.bad))
		{
			snprintf(msg, sizeof msg, "out of memory");
			eof = 1;
		}
		if (eof || gone)
			break;
		hv[0] = ev;
		if (to_tcp.n - toff < RM_HIGH)
			hv[nh++] = con_in();
		/* a quarter second: a window resize is noticed by polling; a
		** lone detach press, at its time */
		left = tap ? tap - now_ms() : 250;
		w = WaitForMultipleObjects(nh, hv, FALSE, left <= 0 ? 0 : left >
		                           250 ? 250 : (DWORD)left);
		if (w == WAIT_FAILED)
		{
			snprintf(msg, sizeof msg, "wait: error %lu",
			         (unsigned long)GetLastError());
			break;
		}
		con_size(&now);
		if (now.ws_row != ws.ws_row || now.ws_col != ws.ws_col)
		{
			ws = now;
			put_size(&to_tcp, 0x05, NULL, &ws);
		}
		if (tap && now_ms() >= tap)
		{
			gone = 1;	/* one press, the window over */
			sw = swap;
		}
		else if (nh == 2 && w == WAIT_OBJECT_0 + 1)
		{
			const unsigned char *c = kb;

			if ((k = con_keys(kb, sizeof kb)) < 0)
				gone = 1;	/* the console went */
			for (hits = 0; k > 0 && hits < 2 &&
			     (c = memchr(c, 0x1c, (size_t)(kb + k - c))); c++)
				hits++;
			/* two presses: in one burst, or in the first one's window.
			** One with no window, or anything else in its window: that
			** press stands alone. */
			if (hits >= 2 || (hits && tap))
			{
				gone = 1;
				sw = !swap;
			}
			else if ((hits && !dbl) || (k > 0 && tap))
			{
				gone = 1;
				sw = swap;
			}
			else if (hits)
				tap = now_ms() + dbl;
			else if (k > 0)
				put_frame(&to_tcp, 0x10, kb, (size_t)k);
		}
		if (gone || WSAEnumNetworkEvents((SOCKET)fd, ev, &ne) ||
		    !(ne.lNetworkEvents & (FD_READ | FD_CLOSE)))
			continue;
		/* from the server, until it would block or a console's worth:
		** a recv that leaves bytes behind sets the event again */
		while (!eof && to_con.n < RM_HIGH)
		{
			k = recv((SOCKET)fd, (char *)buf, (int)sizeof buf, 0);
			if (k > 0)
			{
				sb_add(&tin, buf, (size_t)k);
				if (relay_in(&rs, &tin, &to_con, &exited, &code,
				             msg, sizeof msg) < 0)
					eof = 1;
				continue;
			}
			/* a 0x13 then a reset: the 0x13 says why */
			if (k < 0 && WSAGetLastError() == WSAEWOULDBLOCK)
				break;
			if (k < 0 && !msg[0])
				snprintf(msg, sizeof msg, "%s",
				         sock_str(WSAGetLastError()));
			eof = 1;
		}
	}
	/* what the server sent before the end */
	if (to_con.n && !gone)
		(void)con_write(to_con.p, to_con.n);
	con_restore();
	if (ev != WSA_INVALID_EVENT)
	{
		WSAEventSelect((SOCKET)fd, ev, 0);
		WSACloseEvent(ev);
	}
	free(tin.p);
	free(to_con.p);
	free(to_tcp.p);
	if (exited || gone)
		return sw && !exited ? ATTACH_SWITCH : 0;
	end_line(line, sizeof line, code, label, msg);
	fprintf(stderr, "%s\n", line);
	return code;
}
#endif

/* The host labelled `host` into *h: one the user named, or one discovery
** found that `tailscale whois` says is the user's own. 0, or -1 with why
** said on stderr. */
static int
host_pick(const char *host, struct rhost *h)
{
	struct rhost hv[RM_HOSTS];
	char sh[300];
	int n = candidates(hv, RM_HOSTS, RM_TS_MS, 1), i;

	logsafe(sh, sizeof sh, host);
	if ((i = n > 0 ? host_find(hv, n, host) : -1) < 0)
	{
		fprintf(stderr, "dch: %s is not a known host (", sh);
		for (i = 0; i < n; i++)
			fprintf(stderr, "%s%s", i ? ", " : "known: ", hv[i].label);
		fprintf(stderr, "%s)\n", n > 0 ? "" : !remote_on()
		        ? "remote hosts are off: DCH_NO_REMOTE or remote_hosts"
		          " = off" : "none: no --hosts, no remote_hosts, no "
		          "online tailnet peers of yours");
		return -1;
	}
	/* before a byte goes to it: a discovered peer is the user's own */
	if (hv[i].found && !peer_is_mine(&hv[i]))
	{
		fprintf(stderr, "dch: %s: tailscale whois does not show it as "
		        "your own untagged machine; not connecting\n", sh);
		return -1;
	}
	*h = hv[i];
	return 0;
}

/* host:name as a connection that has listed name, for the 0x01 to follow
** on it: its socket, the host in *h, and what followed the list in *rest,
** the parser's state in *rs; or -1, why said on stderr (-2: refused). */
static int
attach_open(const char *host, const char *name, struct rhost *h,
            struct sb *rest, struct rf_state *rs)
{
	char sh[300], sn[1100];

	if (host_pick(host, h) < 0)
		return -1;
	logsafe(sh, sizeof sh, host);
	logsafe(sn, sizeof sn, name);
	return open_listed(h, name, sh, sn, rest, rs);
}

#ifdef _WIN32
int
dch_remote_attach(const char *host, const char *name)
{
	struct rhost h;
	struct sb rest = {0};
	struct rf_state rs;
	int fd, rc;

	if ((fd = attach_open(host, name, &h, &rest, &rs)) < 0)
		return fd == -2 ? DCH_EXIT_REFUSED : 1;
	keepalive(fd);
	rc = win_relay(fd, h.label, name, rest, rs);
	sock_close(fd);
	return rc;
}
#else
int
dch_remote_attach(const char *host, const char *name)
{
	struct rhost h;
	struct winsize ws;
	struct sb out = {0}, rest = {0};
	struct rf_state rs;
	char sh[300];
	int i, fd, sp[2], wp[2], rc;

	if ((fd = attach_open(host, name, &h, &rest, &rs)) < 0)
		return fd == -2 ? DCH_EXIT_REFUSED : 1;
	logsafe(sh, sizeof sh, host);
	if (!isatty(0) || tcgetattr(0, &orig_term) < 0)
	{
		fprintf(stderr, "dch: attaching requires a terminal\n");
		close(fd);
		free(rest.p);
		return 1;
	}
	keepalive(fd);
	memset(&ws, 0, sizeof ws);
	ioctl(0, TIOCGWINSZ, &ws);
	put_size(&out, 0x01, name, &ws);
	sp[0] = sp[1] = wp[0] = wp[1] = -1;
	/* a few dozen bytes into an idle socket: sent whole */
	if (out.bad || send(fd, out.p, out.n, MSG_NOSIGNAL) != (ssize_t)out.n ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, sp) < 0 || pipe_ce(wp) < 0 ||
	    (relay_pid = fork()) < 0)
	{
		fprintf(stderr, "dch: %s: %s\n", sh, strerror(errno));
		free(out.p);
		free(rest.p);
		close(fd);
		for (i = 0; i < 2; i++)
		{
			if (sp[i] >= 0)
				close(sp[i]);
			if (wp[i] >= 0)
				close(wp[i]);
		}
		return 1;
	}
	free(out.p);
	relay_st = -1;
	if (relay_pid == 0)
	{
		child_signals();
		close(sp[0]);
		close(wp[0]);
		_exit(relay(fd, sp[1], wp[1], h.label, rest, rs));
	}
	free(rest.p);
	close(fd);
	close(sp[1]);
	close(wp[1]);
	cloexec(sp[0]);
	why_fd = wp[0];
	attach_sock = sp[0];
	attach_lost = remote_lost;
	attach_detached = remote_detached;
	snprintf(g_attached, sizeof g_attached, "%s", h.label);
	dch_trace("remote attach %s relay=%d", h.label, (int)relay_pid);
	rc = attach_main(0);	/* returns only to switch sessions */
	attach_sock = -1;
	attach_lost = NULL;
	attach_detached = NULL;
	g_attached[0] = '\0';
	if (relay_pid > 0)
		while (waitpid(relay_pid, NULL, 0) < 0 && errno == EINTR)
			;
	relay_pid = -1;
	close(why_fd);
	why_fd = -1;
	return rc;
}
#endif

/* ---- ctl: a dch verb on a remote host (#114) ------------------------ */

/* Whether the 0x18 hello js lists `op` in its "ops". Pure. */
static int
hello_op(const char *js, const char *op)
{
	struct json_parser jp;
	char got[32];
	int ops, i, hit = 0;

	if (dch_json_parse(&jp, js) == 0 && jp.ntok > 0 &&
	    jp.tok[0].kind == JSON_OBJECT &&
	    (ops = jcont(&jp, 0, "ops", JSON_ARRAY)) >= 0)
		for (i = ops + 1; i < jp.tok[ops].next && !hit;
		     i = jp.tok[i].next)
			hit = jp.tok[i].kind == JSON_STRING &&
			      dch_json_decode(&jp, i, got, sizeof got) == 0 &&
			      !strcmp(got, op);
	dch_json_free(&jp);
	return hit;
}

/* The server's frames in tin as a ctl client takes them: 0x31 onto out,
** 0x32 onto err; 0x33's exit into *xit (0..255; a malformed one is 1) and
** its "why", escaped, into why[wcap] ("unreadable" for a why or a 0x33
** we cannot read); a 0x13's message, escaped, into
** msg[cap] and what its code makes the exit status into *code (err_text: 1
** uncoded). What it parsed leaves tin. 1 once the ctl is over (a 0x33 or a
** 0x13), 0 for more, -1 when the link must end (a frame over the limit).
** With split, 2 when output of the other stream is next, left in tin: the
** caller takes what came so far, then calls again (the host's order of
** stdout and stderr kept, QA 1.55.0 NIT-4). Pure. */
static int
ctl_in(struct rf_state *rs, struct sb *tin, struct sb *out, struct sb *err,
       int *xit, char *why, size_t wcap, char *msg, size_t cap, int *code,
       int split)
{
	struct rf_state was = *rs;
	struct rf_frame f;
	size_t off = 0, used;
	int r, rc = 0, last = 0;

	while (!rc && (r = rf_parse(rs, (const unsigned char *)tin->p + off,
	                            tin->n - off, (size_t)-1, &f, &used))
	       != RF_NEED)
	{
		if (split && r == RF_CHUNK && (f.type == 0x31 ||
		    f.type == 0x32) && f.len && last && f.type != last)
		{
			*rs = was;	/* not taken: the next call's */
			rc = 2;
			break;
		}
		was = *rs;
		off += used;
		if (r == RF_CLOSE)
		{
			snprintf(msg, cap, "it sent a frame over the size limit");
			rc = -1;
		}
		else if ((f.type == 0x31 || f.type == 0x32) && f.len)
		{
			sb_add(f.type == 0x31 ? out : err, f.data, f.len);
			last = f.type;
		}
		else if (f.type == 0x13 && r == RF_FRAME)
		{
			*code = err_text(f.data, f.len, msg, cap);
			rc = 1;
		}
		else if (f.type == 0x33 && r == RF_FRAME)
		{
			struct json_parser jp;
			char t[512], w[64];
			long long v = -1;

			*xit = 1;
			/* a 0x33 or a why we cannot read still says so (#130) */
			snprintf(why, wcap, "unreadable");
			if (f.len < sizeof t && !memchr(f.data, 0, f.len))
			{
				memcpy(t, f.data, f.len);
				t[f.len] = '\0';
				if (dch_json_parse(&jp, t) == 0 && jp.ntok > 0 &&
				    jp.tok[0].kind == JSON_OBJECT)
				{
					int wr;

					if (jint(&jp, 0, "exit", &v) == 1 && v >= 0 &&
					    v <= 255)
						*xit = (int)v;
					if ((wr = jstr(&jp, 0, "why", w, sizeof w)) == 1)
						logsafe(why, wcap, w);
					else if (!wr)
						why[0] = '\0';	/* no why: a plain end */
				}
				dch_json_free(&jp);
			}
			rc = 1;
		}
	}
	if (off)
	{
		memmove(tin->p, tin->p + off, tin->n - off);
		tin->n -= off;
	}
	return rc;
}

/* s[0..n) onto b with every code point a terminal acts on (cp_ctl) but a
** newline or a tab as a \u escape, a byte that is not UTF-8 as \ufffd: a
** JSON line stays the same JSON (a valid one holds them raw only inside a
** string), and a host's bytes cannot drive the terminal (QA 1.55.0 LOW-6).
** A CR outside a string is JSON space and is dropped, so a CRLF line stays
** JSON too (QA 1.56.0 NIT-R-A). Pure. */
static void
ctl_esc(struct sb *b, const char *s, size_t n)
{
	const unsigned char *u = (const unsigned char *)s;
	char e[8];
	unsigned cp;
	size_t i;
	int k, q = 0;	/* in a string: 1, 2 just after its backslash */

	for (i = 0; i < n; i += (size_t)k)
	{
		if (!(k = utf8_at(u + i, &cp)) || (size_t)k > n - i)
		{
			k = 1;
			cp = 0xfffd;
			sb_s(b, "\\ufffd");
		}
		else if (cp == '\r' && !q)
			;
		else if (cp_ctl(cp) && cp != '\n' && cp != '\t')
		{
			snprintf(e, sizeof e, "\\u%04x", cp);
			sb_s(b, e);
		}
		else
			sb_add(b, s + i, (size_t)k);
		q = cp == '\n' ? 0 : q == 2 ? 1 : cp == '"' ? !q
		    : cp == '\\' && q ? 2 : q;
	}
}

static int g_ctl_errmid;	/* the host's stderr written ends mid-line */

/* One stream ctl_in gathered onto f: all of b as it comes, or with esc (a
** host's --xray --json) its whole lines, all of it when last, through
** ctl_esc; the rest stays in b. 0, -1 when the write failed, or -2 (none
** of it written) when the lines escaped are past SB_LIMIT. */
static int
ctl_out(FILE *f, struct sb *b, int esc, int last)
{
	struct sb e = {0};
	size_t k = b->n;
	int rc = 0;

	while (esc && !last && k && b->p[k - 1] != '\n')
		k--;
	if (!k)
		return 0;
	if (esc)
		ctl_esc(&e, b->p, k);
	if (e.bad)
		rc = -2;
	else if (fwrite(esc ? e.p : b->p, 1, esc ? e.n : k, f) !=
	         (esc ? e.n : k) || fflush(f))
		rc = -1;
	else if (f == stderr && (esc ? e.n : k))
		g_ctl_errmid = (esc ? e.p[e.n - 1] : b->p[k - 1]) != '\n';
	free(e.p);
	memmove(b->p, b->p + k, b->n - k);
	b->n -= k;
	b->p[b->n] = '\0';
	return rc;
}

/* What ctl_in gathered, onto stdout and stderr as it comes (esc, last: as
** ctl_out's): 0, -1 when stdout is gone (a reader that closed the pipe),
** or -2 when a line on either is past SB_LIMIT escaped (QA 1.56.0 LOW-R2). */
static int
ctl_put(struct sb *out, struct sb *err, int esc, int last)
{
	int e = ctl_out(stderr, err, esc, last), o = ctl_out(stdout, out, esc,
	                                                    last);

	return e == -2 && !o ? -2 : o;
}

/* argv as a 0x30 request (cwd's too when set) into *b. */
static void
ctl_payload(struct sb *b, int argc, char **argv, const char *cwd)
{
	struct sb j = {0};
	int i;

	sb_s(&j, "{\"argv\":[");
	for (i = 0; i < argc; i++)
	{
		sb_s(&j, i ? "," : "");
		sb_q(&j, argv[i]);
	}
	sb_s(&j, "]");
	if (cwd)
	{
		sb_s(&j, ",\"cwd\":");
		sb_q(&j, cwd);
	}
	sb_s(&j, "}");
	if (j.bad || j.n > RF_CTRL_MAX)
		b->bad = 1;
	else
		put_frame(b, 0x30, j.p, j.n);
	free(j.p);
}

/* A host's refusal as said: one of an X-ray flag this dch's own allowlist
** took is an older dch's there (#297; #263 for a ref kind). */
static const char *
ctl_older(const char *msg, char *buf, size_t cap)
{
	static const char tail[] = " is not allowed with --xray";
	size_t n = strlen(msg), t = sizeof tail - 1;

	if (n <= t || msg[0] != '-' || strcmp(msg + n - t, tail))
		return msg;
	snprintf(buf, cap, "the host's dch does not know %.*s", (int)(n - t),
	         msg);
	return buf;
}

static volatile sig_atomic_t g_ctl_sig;	/* SIGINT or SIGTERM: cancel */

static void
ctl_sig(int sig)
{
	g_ctl_sig = sig;
	signal(sig, SIG_DFL);	/* a second one ends us at once */
}

/* How long an X-ray request may hear nothing from the host before it is a
** lost link: the host's own ctl timeout (CTL_MS; DCH_CTL_TIMEOUT_MS, as dch
** serve reads it, in tests), then RM_ATTACH_MS (QA 1.55.0 LOW-10). */
static long long
ctl_quiet(void)
{
	const char *e = getenv("DCH_CTL_TIMEOUT_MS");

	return (e && atol(e) > 0 ? atol(e) : CTL_MS) + RM_ATTACH_MS;
}

/* The ctl's request out on fd, then its frames until 0x33 or 0x13: the
** exit status (remote.h), what went wrong said on stderr. Cancelled (a
** signal, or our stdout gone): a 0x34 when the host takes one (cancel_ok),
** its 0x33 for up to RM_CANCEL_MS; 128 + the signal, or 0 for EPIPE. esc:
** the output through ctl_esc, line by line. quiet: nothing from the host
** this long (ms, 0 never) is a lost link. */
static int
ctl_run(int fd, const char *sh, const char *verb, struct sb *rq,
        struct sb tin, struct rf_state rs, int cancel_ok, int esc,
        long long quiet)
{
	static const unsigned char c34[] = { 0x34, 0, 0, 0, 0 };
	struct sb out = {0}, err = {0};
	unsigned char buf[RM_READ];
	char msg[400] = "", why[100] = "", old[450];
	size_t off = 0;
	long long end = now_ms() + RM_ATTACH_MS, cend = 0, heard, t;
	int r = 0, xit = 1, code = 1, cx = 0, w;	/* cx: the signal, -1 EPIPE */
	ssize_t k;

	g_ctl_errmid = 0;
	while (off < rq->n)
	{
		struct pollfd pf;
		long long left = end - now_ms();

		pf.fd = fd;
		pf.events = POLLOUT;
		pf.revents = 0;
		k = send(fd, (const void *)(rq->p + off), rq->n - off,
		         MSG_NOSIGNAL);
		if (k > 0)
			off += (size_t)k;
		else if (left <= 0 || k == 0 || (sock_err() != SE_AGAIN &&
		                                 sock_err() != SE_INTR))
		{
			snprintf(msg, sizeof msg, "%s", left <= 0 ? "timed out"
			         : k == 0 ? "connection closed"
			         : sock_str(sock_err()));
			r = -1;
			break;
		}
		else
			(void)poll(&pf, 1, (int)left);
	}
	heard = now_ms();
	while (!r)
	{
		struct pollfd pf;

		r = ctl_in(&rs, &tin, &out, &err, &xit, why, sizeof why, msg,
		           sizeof msg, &code, 0);
		if (tin.bad || out.bad || err.bad)
		{
			/* out, err hold at most one partial line: past
			** SB_LIMIT raw, so past it escaped (QA 1.58.0 NIT-R3) */
			if (tin.bad)
				snprintf(msg, sizeof msg, "out of memory");
			else
				snprintf(msg, sizeof msg, "it sent a line over "
				         "%u MiB escaped", SB_LIMIT >> 20);
			r = -1;
		}
		if (cx < 0)
			out.n = 0;	/* our reader is gone */
		t = now_ms();
		/* last: only a plain end's; a cut's partial line is no line,
		** nor is one of a host dch a signal ended (QA 1.58.0 LOW-R7) */
		if ((w = ctl_put(&out, &err, esc, r > 0 && !why[0] && !cx &&
		                 xit < 128)) ==
		    -2)
		{
			snprintf(msg, sizeof msg, "it sent a line over %u MiB "
			         "escaped", SB_LIMIT >> 20);
			r = -1;
		}
		else if (w < 0 && !cx)
			cx = -1;
		/* time blocked on our stdout is not the host's silence (QA
		** 1.56.0 MED-R1) */
		heard += now_ms() - t;
		if (!cx && g_ctl_sig)
			cx = g_ctl_sig;
		if (r)
			break;
		if (cx && !cend)
		{
			if (!cancel_ok || send(fd, (const void *)c34, sizeof c34,
			                       MSG_NOSIGNAL) != (ssize_t)sizeof c34)
				break;	/* the close: the host's cancel */
			cend = now_ms() + RM_CANCEL_MS;
		}
		if (cend && now_ms() >= cend)
			break;
		if (quiet && !cend && now_ms() - heard >= quiet)
		{
			/* a host that never answers (QA 1.55.0 LOW-10) */
			snprintf(msg, sizeof msg, "no answer within %lld ms", quiet);
			r = -1;
			break;
		}
		pf.fd = fd;
		pf.events = POLLIN;
		pf.revents = 0;
		/* bounded: a signal between the check and the poll (or on
		** Windows, where it does not interrupt one) is seen soon */
		if ((k = poll(&pf, 1, 200)) <= 0)
		{
			if (!k || sock_err() == SE_INTR)
				continue;
			snprintf(msg, sizeof msg, "poll: %s",
			         sock_str(sock_err()));
			r = -1;
			break;
		}
		k = sock_read(fd, buf, sizeof buf);
		if (k < 0 && (sock_err() == SE_INTR || sock_err() == SE_AGAIN))
			continue;
		if (k <= 0)
		{
			snprintf(msg, sizeof msg, "%s", k ? sock_str(sock_err())
			         : "the connection was lost");
			r = -1;
		}
		else
		{
			sb_add(&tin, buf, (size_t)k);
			heard = now_ms();
		}
	}
	/* a lost link, a cancel or the host's cut (a why) drops the stdout
	** line esc held: a cut JSON line is no line (QA 1.55.0 NIT-5,
	** Prosper's ruling; QA 1.56.0 NIT-R-B) */
	if (cx || r < 0 || why[0] || xit >= 128)
		out.n = 0;
	(void)ctl_put(&out, &err, esc, 1);	/* a line esc held, cut short */
	free(tin.p);
	free(out.p);
	free(err.p);
	if (cx)
		return cx < 0 ? 0 : 128 + cx;
	if (r < 0)
	{
		fprintf(stderr, "%sdch: %s: unreachable: %s\n",
		        g_ctl_errmid ? "\n" : "", sh, msg);
		return DCH_EXIT_UNREACHABLE;
	}
	if (msg[0])
	{
		/* a 0x13: the host refused this request, nothing ran */
		if (code == 1)
			fprintf(stderr, "dch: %s: %s\n", sh,
			        ctl_older(msg, old, sizeof old));
		else
			fprintf(stderr, "dch: %s refused: %s\n", sh, msg);
		return code == 1 ? 1 : DCH_EXIT_REFUSED;
	}
	if (why[0])
		fprintf(stderr, "%sdch: %s: %s stopped by the host (%s)\n",
		        g_ctl_errmid ? "\n" : "", sh, verb, why);
	else if (xit >= 128)	/* on its own line (QA 1.60.0 NIT-R5) */
		fprintf(stderr, "%sdch: %s: %s on the host ended by signal "
		        "%d\n", g_ctl_errmid ? "\n" : "", sh, verb, xit - 128);
	return xit;
}

int
dch_remote_ctl(const char *host, int argc, char **argv, const char *cwd)
{
	static unsigned char kind[CTL_ARGV_MAX];
	char why[300], hb[DCH_RHOST_MAX], sh[300], **av = NULL;
	struct sb rq = {0};
	struct rhost h;
	struct probe *p = NULL;
	int fl, i, rc = 1, xray = argc > 0 && !strcmp(argv[0], "--xray");

	if ((fl = ctl_check(argc, argv, kind, why, sizeof why)) < 0)
	{
		fprintf(stderr, "dch: can't run on a remote host: %s\n", why);
		return 1;
	}
#ifndef _WIN32
	/* a reader that closed our stdout ends us with 0, as --follow's does */
	signal(SIGPIPE, SIG_IGN);
#endif
	if (!(av = malloc(((size_t)argc + 1) * sizeof *av)))
		return 1;
	memcpy(av, argv, ((size_t)argc + 1) * sizeof *av);
	/* host:name: every name on the one host, the prefix off */
	hb[0] = '\0';
	for (i = 0; !host && i < argc; i++)
	{
		const char *c = strchr(argv[i], ':');
		size_t n = c ? (size_t)(c - argv[i]) : 0;

		if (kind[i] != CK_NAME)
			continue;
		logsafe(sh, sizeof sh, argv[i]);
		if (!n || n >= sizeof hb)
		{
			fprintf(stderr, "dch: %s is not host:name\n", sh);
			goto out;
		}
		if (!hb[0])
		{
			memcpy(hb, argv[i], n);
			hb[n] = '\0';
		}
		else if (strncasecmp(hb, argv[i], n) || hb[n])
		{
			fprintf(stderr, "dch: %s: the names are on different "
			        "hosts\n", sh);
			goto out;
		}
		av[i] = argv[i] + n + 1;
	}
	if (!host)
	{
		host = hb;
		if (ctl_check(argc, av, kind, why, sizeof why) < 0)
		{
			fprintf(stderr, "dch: can't run on a remote host: %s\n", why);
			goto out;
		}
	}
	logsafe(sh, sizeof sh, host);
	ctl_payload(&rq, argc, av, cwd);
	if (rq.bad)
	{
		fprintf(stderr, "dch: the request is over %u KiB\n",
		        RF_CTRL_MAX >> 10);
		goto out;
	}
	if (host_pick(host, &h) < 0 || !(p = calloc(1, sizeof *p)))
		goto out;
	p->h = h;
	p->keep = p->hello_only = 1;
	probe_all(p, 1, RM_ATTACH_MS);
	if (p->st == PR_REFUSED)
	{
		fprintf(stderr, "dch: %s refused: %s\n", sh, p->why);
		rc = DCH_EXIT_REFUSED;
	}
	else if (p->st == PR_NEWER)
		fprintf(stderr, "dch: %s %s\n", sh, p->why);
	else if (p->st != PR_OK)
	{
		fprintf(stderr, "dch: %s: unreachable: %s\n", sh, p->why);
		rc = DCH_EXIT_UNREACHABLE;
	}
	else if (!hello_op(p->hello_js, "ctl") ||
	         (fl & CTL_CREATE && !hello_op(p->hello_js, "create")) ||
	         (xray && !hello_op(p->hello_js, "xray")))
	{
		/* the request is not sent (spec #832 4.2: tokens, never proto) */
		fprintf(stderr, "dch: %s: does not support %s (needs dch serve "
		        "%s)\n", sh, argv[0], xray ? "1.55.0+"
		        : hello_op(p->hello_js, "ctl") ? "--allow-create" : "1.41+");
		rc = DCH_EXIT_UNSUPPORTED;
	}
	else
	{
		keepalive(p->fd);
		signal(SIGINT, ctl_sig);
		signal(SIGTERM, ctl_sig);
		rc = ctl_run(p->fd, sh, argv[0], &rq, p->in, p->rs,
		             hello_op(p->hello_js, "ctl-cancel"), xray,
		             xray && !(fl & CTL_FOREVER) ? ctl_quiet() : 0);
		memset(&p->in, 0, sizeof p->in);
	}
out:
	if (p)
		probes_free(p, 1);
	free(rq.p);
	free(av);
#ifndef _WIN32
	if (g_ctl_sig && rc == 128 + g_ctl_sig)
		raise(g_ctl_sig);	/* the shell sees the signal (now SIG_DFL) */
#endif
	return rc;
}

#ifndef _WIN32
/* ---- dch --xray host:name --requests --json (#158, spec #832 4.3) ---- */

/* One warm ctl connection, many requests. Each stdin line {"req":N,
** "argv":[...]} (the argv after --xray <name>) is checked here first, by
** 3.1's value rules and in 4.3's request forms only, then sent as one 0x30;
** its 0x31 lines come back as {"req":N,"line":<the host's line>}, its 0x32
** lines as {"req":N,"stderr":"..."}, its end as one {"req":N,"exit":E,
** "why":...}. One at a time ("busy"). {"cancel":N} is a 0x34, or the
** close and a quiet re-dial when the host lacks "ctl-cancel". An idle close
** or a lost link re-dials on the next request; the one in flight then ends
** exit 7 "lost". A host line too long to carry ends it exit 2 "cap", the
** close and a quiet re-dial. stdin EOF: the close, exit 0. */

#define RQ_LINE_MAX RF_CTRL_MAX	/* a stdin line, at most */
/* a host line held, at most: a stock host's own cap (8 MiB); one past it,
** or one o cannot take escaped (sb_q's SB_LIMIT / 6), ends its request
** exit 2 "cap" */
#define RQ_HOST_MAX (SB_LIMIT / 2)

enum { RQ_BAD, RQ_REQ, RQ_CANCEL };

/* One stdin line s: RQ_REQ for {"req":N,"argv":[strings]} (*av
** dch_json_strv's block to free, NULL for [], *ac its count), RQ_CANCEL
** for {"cancel":N}, else RQ_BAD; *req is N when it is 1..2^31-1, else 0.
** Any other key, a key twice or a wrong type is RQ_BAD: input is never
** additive (3.1). Pure. */
static int
rq_parse(const char *s, long long *req, char ***av, int *ac)
{
	struct json_parser jp;
	long long v = 0;
	int keys = 0, i, t, a = -1, rc = RQ_BAD;

	*req = 0;
	*av = NULL;
	*ac = 0;
	if (dch_json_parse(&jp, s) < 0 || jp.ntok < 1 ||
	    jp.tok[0].kind != JSON_OBJECT)
		goto out;
	/* key i, its value i + 1, the next key after the value */
	for (i = 1; i + 1 < jp.ntok && i < jp.tok[0].next;
	     i = jp.tok[i + 1].next)
		keys++;
	if ((t = dch_json_field(&jp, 0, "cancel")) != -1)
	{
		if (keys == 1 && tok_int(&jp, t, &v) == 0 && v >= 1 &&
		    v <= 0x7fffffff)
		{
			*req = v;
			rc = RQ_CANCEL;
		}
		goto out;
	}
	if (jint(&jp, 0, "req", &v) == 1 && v >= 1 && v <= 0x7fffffff)
		*req = v;
	if (!*req || keys != 2 || (a = jcont(&jp, 0, "argv", JSON_ARRAY)) < 0)
		goto out;
	rc = RQ_REQ;
	for (i = a + 1; i < jp.tok[a].next; i = jp.tok[i].next)
		if (jp.tok[i].kind != JSON_STRING)
			rc = RQ_BAD;
	if (rc == RQ_REQ && jp.tok[a].next > a + 1 &&
	    !(*av = dch_json_strv(s, NULL, "argv", ac)))
		rc = RQ_BAD;
out:
	dch_json_free(&jp);
	return rc;
}

/* --xray, name, then a request's argv: a block to free, or NULL. */
static char **
rq_argv(char *name, int ac, char **av)
{
	char **xv = calloc((size_t)ac + 3, sizeof *xv);

	if (!xv)
		return NULL;
	xv[0] = "--xray";
	xv[1] = name;
	if (ac)
		memcpy(xv + 2, av, (size_t)ac * sizeof *av);
	return xv;
}

/* A request's argv (after --xray <name>) by 3.1's value rules, in 4.3's
** request forms only: --json, --ref <text> [--scope] [--full]
** [--budget-ms], --chapter <anchor>, --clear. 0, else -1 with its stderr
** line in msg[cap], worded as dch --xray's for a value rule. Pure. */
static int
rq_check(char *name, int ac, char **av, char *msg, size_t cap)
{
	static unsigned char kind[CTL_ARGV_MAX];
	struct xr_span sp;
	char why[300], **xv;
	int fl, i;

	if (ac > CTL_ARGV_MAX - 2)
	{
		snprintf(msg, cap, "dch: bad usage: too many arguments");
		return -1;
	}
	if (!(xv = rq_argv(name, ac, av)))
	{
		snprintf(msg, cap, "dch: out of memory");
		return -1;
	}
	/* the ctl allowlist's --xray entries (4.4), less --follow */
	if ((fl = ctl_check(ac + 2, xv, kind, why, sizeof why)) < 0)
		snprintf(msg, cap, "dch: %s%s", strncmp(why, "bad ", 4)
		         ? "bad usage: " : "", why);
	else if (fl & CTL_FOREVER)
	{
		snprintf(msg, cap, "dch: bad usage: --follow is not a request");
		fl = -1;
	}
	for (i = 2; fl >= 0 && i + 1 < ac + 2; i++)
		if (kind[i] == CK_FLAG && !strcmp(xv[i], "--ref"))
		{
			if (!xr_ref_ok(xv[i + 1]))
				snprintf(msg, cap, "dch: bad ref");
			else if (!xr_ref_parse(xv[i + 1], NULL, &sp))
				snprintf(msg, cap, "dch: not a ref");
			else
				continue;
			fl = -1;
		}
	free(xv);
	return fl < 0 ? -1 : 0;
}

static void
rq_head(struct sb *o, long long req)
{
	sb_s(o, "{\"req\":");
	if (req)
		sb_ll(o, req);
	else
		sb_s(o, "null");	/* a line with no req of its own */
}

/* s[0..k) (s[k] a NUL) as one JSON string: sb_q's escapes, and a NUL,
** where sb_q would stop, as \u0000 (QA 1.55.0 LOW-9). Pure. */
static void
rq_q(struct sb *o, const char *s, size_t k)
{
	size_t i = 0, n, at;

	sb_s(o, "\"");
	for (;;)
	{
		n = strlen(s + i);
		at = o->n;
		sb_q(o, s + i);		/* "..." with s[i + n] a NUL or the end */
		if (o->bad)
			return;
		memmove(o->p + at, o->p + at + 1, o->n - at - 2);
		o->p[o->n -= 2] = '\0';	/* its quotes off */
		if ((i += n) >= k)
			break;
		sb_s(o, "\\u0000");
		i++;
	}
	sb_s(o, "\"");
}

/* req's stderr line s, escaped (C0, C1, bidi; U+FFFD for bad UTF-8) */
static void
rq_err(struct sb *o, long long req, const char *s)
{
	rq_head(o, req);
	sb_s(o, ",\"stderr\":");
	sb_q(o, s);
	sb_s(o, "}\n");
}

/* req's one end line */
static void
rq_exit(struct sb *o, long long req, int code, const char *why)
{
	rq_head(o, req);
	sb_s(o, ",\"exit\":");
	sb_ll(o, code);
	sb_s(o, ",\"why\":");
	if (why)
		sb_q(o, why);
	else
		sb_s(o, "null");
	sb_s(o, "}\n");
}

/* s (k bytes) may go out byte for byte as a "line": one JSON object by
** json.c's RFC 8259 grammar (valid UTF-8, nothing after it, no NUL), with
** no raw code point dch's own quoting escapes (C0, DEL, C1, bidi), so it is
** as safe to print as a line dch wrote. Pure. */
static int
rq_obj(const char *s, size_t k)
{
	struct json_parser jp;
	const unsigned char *u = (const unsigned char *)s;
	unsigned cp;
	size_t i;
	int n, ok;

	if (strlen(s) != k)
		return 0;
	for (i = 0; i < k; i += (size_t)n)
		if (!(n = utf8_at(u + i, &cp)) || cp_ctl(cp) ||
		    cp == 0x200e || cp == 0x200f)
			return 0;
	ok = dch_json_parse(&jp, s) == 0 && jp.ntok > 0 &&
	     jp.tok[0].kind == JSON_OBJECT;
	dch_json_free(&jp);
	return ok;
}

/* The complete lines of buf (all of it when last) onto o, each one line of
** req: stderr's escaped; stdout's byte for byte when rq_obj passes it and
** the wrapped line still parses, else as a JSON string (rq_q: a NUL kept
** as \u0000), so a host never writes a line of its own here. What is left
** stays in buf: 0. -1 for a line over RQ_HOST_MAX (held or whole) or one o
** cannot take: the lines before it are on o, nothing of it, and o is not
** bad. Pure. */
static int
rq_wrap(struct sb *o, long long req, int err, struct sb *buf, int last)
{
	struct json_parser jp;
	size_t off = 0, k, at;
	char *s, *nl;
	int obj;

	while (off < buf->n)
	{
		s = buf->p + off;
		if (!(nl = memchr(s, '\n', buf->n - off)) && !last)
			break;
		k = nl ? (size_t)(nl - s) : buf->n - off;
		if (k > RQ_HOST_MAX)
			return -1;
		s[k] = '\0';	/* the newline, or the sb's own NUL */
		obj = 0;
		at = o->n;
		if (!err && rq_obj(s, k))
		{
			rq_head(o, req);
			sb_s(o, ",\"line\":");
			sb_add(o, s, k);
			sb_s(o, "}\n");
			/* wrapped, the object sits a level deeper: past json.c's
			** depth limit the line dch wrote would not parse */
			if (!o->bad)
			{
				obj = dch_json_parse(&jp, o->p + at) == 0;
				dch_json_free(&jp);
				if (!obj)
					o->p[o->n = at] = '\0';
			}
		}
		if (err || !obj)
		{
			rq_head(o, req);
			sb_s(o, err ? ",\"stderr\":" : ",\"line\":");
			rq_q(o, s, k);
			sb_s(o, "}\n");
		}
		if (o->bad)
		{
			o->bad = 0;	/* o as it was: the line is not there */
			o->n = at;
			if (o->p)
				o->p[at] = '\0';
			return -1;
		}
		off += k + 1;
	}
	if (off)
	{
		off = off < buf->n ? off : buf->n;
		memmove(buf->p, buf->p + off, buf->n - off);
		buf->n -= off;
		buf->p[buf->n] = '\0';
	}
	return buf->n > RQ_HOST_MAX ? -1 : 0;
}

/* The ready line: the ops of hello js, each quoted anew. Pure. */
static void
rq_ready(struct sb *o, const char *js)
{
	struct json_parser jp;
	char got[64];
	int ops, i, n = 0;

	sb_s(o, "{\"schema\":1,\"ready\":{\"ops\":[");
	if (dch_json_parse(&jp, js) == 0 && jp.ntok > 0 &&
	    jp.tok[0].kind == JSON_OBJECT &&
	    (ops = jcont(&jp, 0, "ops", JSON_ARRAY)) >= 0)
		for (i = ops + 1; i < jp.tok[ops].next; i = jp.tok[i].next)
			if (jp.tok[i].kind == JSON_STRING &&
			    dch_json_decode(&jp, i, got, sizeof got) == 0)
			{
				sb_s(o, n++ ? "," : "");
				sb_q(o, got);
			}
	dch_json_free(&jp);
	sb_s(o, "]}}\n");
}

struct rq
{
	struct rhost h;
	char sh[300], *name;	/* the host, printable; the host-local name */
	struct probe *p;	/* the warm connection, NULL: none */
	struct probe *dp;	/* else a dial on the way (#226), until dend */
	long long dend;
	struct sb pend;		/* the 0x30 of the request waiting on it */
	struct sb wait;		/* stdin lines queued behind that request */
	int cancel_ok;		/* its hello lists "ctl-cancel" */
	struct rf_state rs;
	struct sb tin, lo, le;	/* frames in; stdout, stderr to a newline */
	long long req, cend;	/* the one running (0 none); its 0x34's end */
	long long heard;	/* its request sent, or the host's last bytes */
	long long dark;		/* a failed dial stands until then: */
	int dcode;		/* its status */
	char dmsg[800];		/* and its stderr line */
};

static void
rq_close(struct rq *q)
{
	if (q->p)
		probes_free(q->p, 1);
	if (q->dp)
		probes_free(q->dp, 1);
	q->p = q->dp = NULL;
	free(q->tin.p);
	free(q->lo.p);
	free(q->le.p);
	free(q->pend.p);
	memset(&q->tin, 0, sizeof q->tin);
	memset(&q->lo, 0, sizeof q->lo);
	memset(&q->le, 0, sizeof q->le);
	memset(&q->pend, 0, sizeof q->pend);
}

/* q's dial p is over: 0 and p q's warm connection, else (p freed) the
** exit status (4 refused, 6 no "ctl" or "xray", 7 unreachable, 1 a newer
** protocol) and its stderr line in msg[cap]. A failure stands for
** RM_ATTACH_MS: the requests queued behind a dark host's dial get it at
** once, not a dial each (QA 1.55.0 LOW-10). */
static int
rq_hello(struct rq *q, struct probe *p, char *msg, size_t cap)
{
	int rc = 0;

	if (p->st == PR_REFUSED)
	{
		snprintf(msg, cap, "dch: %s refused: %s", q->sh, p->why);
		rc = DCH_EXIT_REFUSED;
	}
	else if (p->st == PR_NEWER)
	{
		snprintf(msg, cap, "dch: %s %s", q->sh, p->why);
		rc = 1;
	}
	else if (p->st != PR_OK)
	{
		snprintf(msg, cap, "dch: %s: unreachable: %s", q->sh, p->why);
		rc = DCH_EXIT_UNREACHABLE;
	}
	else if (!hello_op(p->hello_js, "ctl") || !hello_op(p->hello_js, "xray"))
	{
		snprintf(msg, cap, "dch: %s: does not support --xray (needs dch "
		         "serve 1.55.0+)", q->sh);
		rc = DCH_EXIT_UNSUPPORTED;
	}
	if (rc)
	{
		probes_free(p, 1);
		q->dark = now_ms() + RM_ATTACH_MS;
		q->dcode = rc;
		snprintf(q->dmsg, sizeof q->dmsg, "%s", msg);
		return rc;
	}
	keepalive(p->fd);
	q->p = p;
	q->tin = p->in;
	memset(&p->in, 0, sizeof p->in);
	q->rs = p->rs;
	q->cancel_ok = hello_op(p->hello_js, "ctl-cancel");
	return 0;
}

/* A new connection to q's host: as rq_hello says; with wait 0 the dial
** goes on in q->dp (0), driven by dch_remote_requests's poll, so stdin is
** read while it dials (#226). */
static int
rq_dial(struct rq *q, char *msg, size_t cap, int wait)
{
	struct probe *p;

	rq_close(q);
	if (now_ms() < q->dark)
	{
		snprintf(msg, cap, "%s", q->dmsg);
		return q->dcode;
	}
	if (!(p = calloc(1, sizeof *p)))
	{
		snprintf(msg, cap, "dch: out of memory");
		return 1;
	}
	p->h = q->h;
	p->keep = p->hello_only = 1;
	if (wait)
		probe_all(p, 1, RM_ATTACH_MS);
	else
	{
		q->dend = now_ms() + RM_ATTACH_MS;
		pr_start(p, 1, q->dend);
		if (p->st == PR_CONN)
		{
			q->dp = p;
			return 0;
		}
	}
	return rq_hello(q, p, msg, cap);
}

/* req's dial failed (e, msg): its stderr line and its end */
static void
rq_undialled(struct sb *o, long long req, int e, const char *msg)
{
	rq_err(o, req, msg);
	rq_exit(o, req, e, e == DCH_EXIT_UNREACHABLE ? "lost" : "refused");
}

/* n bytes out on fd within RM_ATTACH_MS: 0, else -1 and why in msg. */
static int
rq_send(int fd, const void *d, size_t n, char *msg, size_t cap)
{
	const char *b = d;
	long long end = now_ms() + RM_ATTACH_MS;
	size_t off = 0;
	ssize_t k;

	while (off < n)
	{
		struct pollfd pf;
		long long left = end - now_ms();

		if ((k = send(fd, b + off, n - off, MSG_NOSIGNAL)) > 0)
		{
			off += (size_t)k;
			continue;
		}
		if (left <= 0 || !k || (errno != EAGAIN && errno != EINTR))
		{
			snprintf(msg, cap, "%s", left <= 0 ? "timed out"
			         : !k ? "connection closed" : strerror(errno));
			return -1;
		}
		pf.fd = fd;
		pf.events = POLLOUT;
		pf.revents = 0;
		(void)poll(&pf, 1, (int)left);
	}
	return 0;
}

/* The link is gone (why): closed, and the request in flight ends exit 7
** "lost"; the next request dials anew. */
static void
rq_lost(struct rq *q, struct sb *o, const char *why)
{
	char m[800];

	if (q->req)
	{
		snprintf(m, sizeof m, "dch: %s: unreachable: %s", q->sh, why);
		rq_err(o, q->req, m);
		rq_exit(o, q->req, DCH_EXIT_UNREACHABLE, "lost");
	}
	q->req = q->cend = 0;
	rq_close(q);
}

/* Ended by the close: "cancel" (no "ctl-cancel", or its 0x33 is late), as
** the host's 0x34 would end it, or "cap" (a host line past what dch
** carries), as the host's cap would. What is held is dropped, and a new
** connection is dialled, quietly (a failure: the next request's). */
static void
rq_drop(struct rq *q, struct sb *o, const char *why)
{
	char m[800];

	rq_exit(o, q->req, 2, why);
	q->req = q->cend = 0;
	rq_close(q);
	(void)rq_dial(q, m, sizeof m, 0);
}

/* The request waiting on q's dial ends "cancel": nothing of it was sent;
** the dial goes on, quietly (#226). */
static void
rq_unsent(struct rq *q, struct sb *o)
{
	rq_exit(o, q->req, 2, "cancel");
	q->req = 0;
	free(q->pend.p);
	memset(&q->pend, 0, sizeof q->pend);
}

/* q's dial, once over or past dend: the request waiting on it sent, or
** ended as the dial failed. */
static void
rq_dialled(struct rq *q, struct sb *o)
{
	struct probe *p = q->dp;
	char m[800];
	int e;

	if ((p->st == PR_CONN || p->st == PR_WAIT) && now_ms() < q->dend)
		return;
	q->dp = NULL;
	pr_finish(p, 1, RM_ATTACH_MS);
	if ((e = rq_hello(q, p, m, sizeof m)) && q->req)
		rq_undialled(o, q->req, e, m);
	if (e)
		q->req = 0;
	else if (q->req)
	{
		q->heard = now_ms();
		if (rq_send(q->p->fd, q->pend.p, q->pend.n, m, sizeof m) < 0)
			rq_lost(q, o, m);
	}
	free(q->pend.p);
	memset(&q->pend, 0, sizeof q->pend);
}

/* q's socket is readable: its frames, the request's lines onto o. */
static void
rq_net(struct rq *q, struct sb *o)
{
	unsigned char buf[RM_READ];
	char msg[400], why[100], m[800], old[450];
	int r, xit, code;
	ssize_t k = read(q->p->fd, buf, sizeof buf);

	if (k < 0 && (errno == EINTR || errno == EAGAIN))
		return;
	if (k <= 0)
	{
		/* idle: the host's idle close, quietly */
		rq_lost(q, o, k ? strerror(errno) : "the connection was lost");
		return;
	}
	sb_add(&q->tin, buf, (size_t)k);
	q->heard = now_ms();
	do
	{
		msg[0] = why[0] = '\0';
		xit = code = 1;
		r = ctl_in(&q->rs, &q->tin, &q->lo, &q->le, &xit, why,
		           sizeof why, msg, sizeof msg, &code, 1);
		if (r >= 0 && (q->tin.bad || q->lo.bad || q->le.bad))
		{
			snprintf(msg, sizeof msg, "it sent too much");
			r = -1;
		}
		if (r < 0)
		{
			rq_lost(q, o, msg);
			return;
		}
		if (!q->req)
			q->lo.n = q->le.n = 0;	/* no request of ours: dropped */
		else if (rq_wrap(o, q->req, 1, &q->le, r == 1) < 0 ||
		         rq_wrap(o, q->req, 0, &q->lo, r == 1) < 0)
		{
			rq_drop(q, o, "cap");
			return;
		}
		if (r == 2)
			continue;	/* the other stream's output next */
		if (r && q->req && msg[0])
		{
			/* a 0x13: the host refused it, nothing ran */
			snprintf(m, sizeof m, code == 1 ? "dch: %s: %s"
			         : "dch: %s refused: %s", q->sh,
			         code == 1 ? ctl_older(msg, old, sizeof old) : msg);
			rq_err(o, q->req, m);
			rq_exit(o, q->req, code == 1 ? 1 : DCH_EXIT_REFUSED,
			        "refused");
		}
		else if (r && q->req)
			rq_exit(o, q->req, xit, !strcmp(why, "cancel") ||
			        !strcmp(why, "cap") || !strcmp(why, "timeout")
			        ? why : NULL);
		if (r)
			q->req = q->cend = 0;
		if (r && msg[0])
			rq_close(q);	/* its FIN follows */
	} while (r && q->p && q->tin.n);
}

/* {"cancel":N}: a 0x34 for N while it runs, else nothing (as for 0x34). */
static void
rq_cancel(struct rq *q, struct sb *o, long long req)
{
	static const unsigned char c34[] = { 0x34, 0, 0, 0, 0 };
	char m[400];

	if (!q->req || req != q->req || q->cend)
		return;
	if (q->dp)
		rq_unsent(q, o);
	else if (q->cancel_ok &&
	    rq_send(q->p->fd, c34, sizeof c34, m, sizeof m) == 0)
		q->cend = now_ms() + RM_CANCEL_MS;
	else
		rq_drop(q, o, "cancel");
}

/* A checked request out as a 0x30 on the warm connection, dialled first
** when there is none; while a dial is on the way it waits on it. */
static void
rq_start(struct rq *q, struct sb *o, long long req, int ac, char **av)
{
	struct sb b = {0};
	char **xv, m[800];
	int e;

	if (!q->p && !q->dp && (e = rq_dial(q, m, sizeof m, 0)))
	{
		rq_undialled(o, req, e, m);
		return;
	}
	if ((xv = rq_argv(q->name, ac, av)))
		ctl_payload(&b, ac + 2, xv, NULL);
	free(xv);
	if (!xv || b.bad)
	{
		snprintf(m, sizeof m, "dch: the request is over %u KiB",
		         RF_CTRL_MAX >> 10);
		rq_err(o, req, m);
		rq_exit(o, req, 2, "refused");
	}
	else
	{
		q->req = req;
		q->heard = now_ms();
		if (q->dp)
		{
			q->pend = b;	/* sent once dialled */
			return;
		}
		if (rq_send(q->p->fd, b.p, b.n, m, sizeof m) < 0)
			rq_lost(q, o, m);
	}
	free(b.p);
}

/* One stdin line s[0..k) (NUL-terminated there). */
static void
rq_line(struct rq *q, struct sb *o, const char *s, size_t k)
{
	long long req = 0;
	char **av = NULL, m[400];
	int ac = 0, kind;

	if (strspn(s, " \t\r") == k)
		return;		/* a blank line */
	if (k > RQ_LINE_MAX)
	{
		rq_err(o, 0, "dch: bad request: a line over 64 KiB");
		rq_exit(o, 0, 2, "refused");
		return;
	}
	kind = strlen(s) != k ? RQ_BAD : rq_parse(s, &req, &av, &ac);
	if (kind == RQ_CANCEL)
		rq_cancel(q, o, req);
	else if (kind == RQ_BAD)
	{
		rq_err(o, req, "dch: bad request: send {\"req\":N,\"argv\":"
		       "[...]} or {\"cancel\":N}, one per line");
		rq_exit(o, req, 2, "refused");
	}
	else if (q->req)
		/* ctl's own rule (4.2); the running one's own id is not this
		** line's to end (QA 1.55.0 LOW-8) */
		rq_exit(o, req == q->req ? 0 : req, 1, "busy");
	else if (rq_check(q->name, ac, av, m, sizeof m) < 0)
	{
		rq_err(o, req, m);
		rq_exit(o, req, 2, "refused");
	}
	else
		rq_start(q, o, req, ac, av);
	free(av);
}

/* The lines queued behind a request that was dialling, in order, while
** none is; a failed alloc's buffer is left for the caller's exit. */
static void
rq_drain(struct rq *q, struct sb *o)
{
	char *nl;

	while (q->wait.n && !q->wait.bad && !(q->req && q->dp) &&
	       (nl = memchr(q->wait.p, '\n', q->wait.n)))
	{
		size_t k = (size_t)(nl - q->wait.p);

		*nl = '\0';
		rq_line(q, o, q->wait.p, k);
		memmove(q->wait.p, nl + 1, q->wait.n - k - 1);
		q->wait.n -= k + 1;
	}
}

/* One stdin line s[0..k): a {"cancel":N} at once; any other behind a
** request still dialling waits, after what is queued, as when the dial
** blocked (#226). */
static void
rq_take(struct rq *q, struct sb *o, const char *s, size_t k)
{
	long long req;
	char **av = NULL;
	int ac;

	rq_drain(q, o);
	if (q->req && q->dp && (k > RQ_LINE_MAX || strlen(s) != k ||
	                        rq_parse(s, &req, &av, &ac) != RQ_CANCEL))
	{
		free(av);
		sb_add(&q->wait, s, k);
		sb_add(&q->wait, "\n", 1);
		return;
	}
	free(av);
	rq_line(q, o, s, k);
	rq_drain(q, o);
}

/* stdout gets o, flushed: 0, else -1 (our reader is gone). */
static int
rq_put(struct sb *o)
{
	int rc = o->bad ? -1 : 0;

	if (!rc && o->n && (fwrite(o->p, 1, o->n, stdout) != o->n ||
	                    fflush(stdout)))
		rc = -1;
	o->n = 0;
	return rc;
}

/* No warm connection to start with: code, said on stderr and as the one
** stdout line {"schema":1,"exit":code,"stderr":msg}. */
static int
rq_fail(int code, const char *msg)
{
	struct sb o = {0};

	if (msg)
		fprintf(stderr, "%s\n", msg);
	sb_s(&o, "{\"schema\":1,\"exit\":");
	sb_ll(&o, code);
	if (msg)
	{
		sb_s(&o, ",\"stderr\":");
		sb_q(&o, msg);
	}
	sb_s(&o, "}\n");
	(void)rq_put(&o);
	free(o.p);
	return code;
}

int
dch_remote_requests(char *n)
{
	unsigned char buf[RM_READ];
	struct sb in = {0}, o = {0};
	struct rq q;
	char hb[DCH_RHOST_MAX], msg[800];
	size_t hl = strcspn(n, ":"), off;
	long long quiet = ctl_quiet();
	int rc, skip = 0;
	ssize_t k;

	memset(&q, 0, sizeof q);
	if (!n[hl] || !hl || hl >= sizeof hb)
		return rq_fail(2, "dch: bad session");
	memcpy(hb, n, hl);
	hb[hl] = '\0';
	q.name = n + hl + 1;
	logsafe(q.sh, sizeof q.sh, hb);
	/* a closed stdout is a write that fails: exit 0, as --follow's */
	signal(SIGPIPE, SIG_IGN);
	if (host_pick(hb, &q.h) < 0)
		return rq_fail(1, NULL);
	if ((rc = rq_dial(&q, msg, sizeof msg, 1)))
		return rq_fail(rc, msg);
	rq_ready(&o, q.p->hello_js);
	while (rq_put(&o) == 0)
	{
		struct pollfd pf[2];
		long long due = 0;
		int np = 1, tmo = -1;

		/* past 64 KiB queued behind a dial, stdin waits for it */
		pf[0].fd = q.wait.n > RQ_LINE_MAX ? -1 : 0;
		pf[0].events = POLLIN;
		pf[0].revents = 0;
		if (q.p || q.dp)
		{
			pf[1].fd = q.p ? q.p->fd : q.dp->fd;
			pf[1].events = q.dp && q.dp->st == PR_CONN ? POLLOUT
			                                           : POLLIN;
			pf[1].revents = 0;
			np = 2;
		}
		/* a 0x34's 0x33, else the host's answer within ctl_quiet */
		if (q.req && q.p)
			due = q.cend ? q.cend : q.heard + quiet;
		if (q.dp)
			due = q.dend;
		if (due)
			tmo = due > now_ms() ? (int)(due - now_ms()) : 0;
		if (poll(pf, (nfds_t)np, tmo) < 0)
		{
			if (errno == EINTR)
				continue;
			break;
		}
		if (np > 1 && pf[1].revents && q.dp)
			pr_io(q.dp);
		else if (np > 1 && pf[1].revents && q.p)
			rq_net(&q, &o);
		if (q.dp)
		{
			rq_dialled(&q, &o);
			rq_drain(&q, &o);
		}
		if (q.cend && now_ms() >= q.cend)
			rq_drop(&q, &o, "cancel");	/* its 0x33 never came */
		else if (q.req && q.p && !q.cend && now_ms() - q.heard >= quiet)
		{
			snprintf(msg, sizeof msg, "no answer within %lld ms", quiet);
			rq_lost(&q, &o, msg);	/* QA 1.55.0 LOW-10 */
		}
		if (!pf[0].revents)
			continue;
		if ((k = read(0, buf, sizeof buf)) < 0 &&
		    (errno == EINTR || errno == EAGAIN))
			continue;
		if (k <= 0)
		{
			/* the close cancels a request in flight on the host,
			** and one dialling, and those queued behind it; an
			** unterminated last line is no request */
			while (q.req && q.dp)
			{
				rq_unsent(&q, &o);
				rq_drain(&q, &o);
			}
			if (q.req)
				rq_exit(&o, q.req, 2, "cancel");
			(void)rq_put(&o);
			break;
		}
		sb_add(&in, buf, (size_t)k);
		if (in.bad)
			break;
		for (off = 0;;)
		{
			char *s = in.p + off, *nl = memchr(s, '\n', in.n - off);

			if (!nl)
				break;
			*nl = '\0';
			if (!skip)
				rq_take(&q, &o, s, (size_t)(nl - s));
			skip = 0;
			off += (size_t)(nl - s) + 1;
		}
		if (q.wait.bad)
			break;	/* rq_take's failed alloc */
		if (!skip && in.n - off > RQ_LINE_MAX)
		{
			rq_err(&o, 0, "dch: bad request: a line over 64 KiB");
			rq_exit(&o, 0, 2, "refused");
			skip = 1;	/* the rest of it, to its newline */
		}
		if (skip)
			off = in.n;
		memmove(in.p, in.p + off, in.n - off);
		in.n -= off;
	}
	rq_close(&q);
	rc = in.bad || o.bad || q.wait.bad;	/* never a silent end: only a
						** failed alloc */
	if (rc)
		fprintf(stderr, "dch: out of memory\n");
	free(in.p);
	free(o.p);
	free(q.wait.p);
	return rc;
}

/* ---- dch --ls-json [--remote] --follow (#103) ------------------------ */

/*
** --ls-json's array, then the same again whenever it changes. Each host
** keeps one probe: one whose hello lists "watch" is held on that
** connection, a 0x2e sent after its list, and re-read off its 0x11s; any
** other is probed again every RM_EVERY_MS, as the picker does, and the
** local rows are re-read on the same tick. A host that fails (no answer in
** RM_BG_MS, a drop, a stream silent RM_QUIET_MS, a frame it should not
** send) keeps its rows, stale, for RM_TTL_S after it last answered, as
** --ls-json --remote shows them, and is dialled again after RM_EVERY_MS,
** doubled per failure up to 16 times that. The array is printed by
** --ls-json's own code in a forked child, its stdout a pipe to us: it goes
** out only when it differs from the last one.
*/
#define RM_QUIET_MS 35000	/* a watch stream's 0x14 comes after 25 s */
#define FL_GAP_MS 200		/* least time between two looks: a host that
				** floods 0x11s costs one per gap (QA 1.62.0
				** LOW-R2) */

struct fhost
{
	struct probe p;		/* the dial, then (live) its 0x2e stream */
	int live;		/* p.fd is the stream */
	int idle;		/* nothing in flight: dialled again at due */
	int ok;			/* its rows are fresh */
	int gone;		/* refused us or newer: no rows */
	int fails;		/* in a row */
	long long due, end;	/* the next dial; the dial's or stream's limit */
	struct rlist rows;
	long long at;		/* when it last answered, epoch seconds */
};

/* DCH_REMOTE_EVERY_MS, DCH_REMOTE_QUIET_MS (tests): 100..600000, else dflt */
static long long
env_ms(const char *k, long long dflt)
{
	const char *e = getenv(k);
	long v = e ? atol(e) : 0;

	return v >= 100 && v <= 600000 ? v : dflt;
}

/* h's probe closed and emptied, its host kept */
static void
fh_close(struct fhost *h)
{
	struct rhost hh = h->p.h;

	if (h->p.fd >= 0)
		sock_close(h->p.fd);
	free(h->p.list);
	free(h->p.hello_js);
	free(h->p.in.p);
	memset(&h->p, 0, sizeof h->p);
	h->p.h = hh;
	h->p.fd = -1;
	h->live = 0;
}

/* a and b list the same sessions, the same way: their at aside */
static int
rows_same(const struct rlist *a, const struct rlist *b)
{
	int i;

	for (i = 0; a->n == b->n && i < a->n; i++)
		if (strcmp(a->v[i].name, b->v[i].name) ||
		    strcmp(a->v[i].alias, b->v[i].alias) ||
		    strcmp(a->v[i].state, b->v[i].state) ||
		    strcmp(a->v[i].cwd, b->v[i].cwd))
			return 0;
	return a->n == b->n;
}

/* The frames of h's 0x2e stream in h->p.in, as far as they have come: a
** 0x11 is its rows, a 0x14 a beat, a streamed type skipped. A 0x13, a
** frame over the limit or a list that is not an array ends the stream
** (p.st PR_REFUSED for a coded 0x13, else PR_FAIL; why said). 1 when its
** rows changed (the same rows again are a beat) or it ended. */
static int
fh_frames(struct fhost *h, long long now, long long quiet)
{
	struct rf_frame f;
	size_t off = 0, used;
	char why[300];
	int r, rc = 0, bad;

	while (h->live && (r = rf_parse(&h->p.rs, (const unsigned char *)
	       h->p.in.p + off, h->p.in.n - off, (size_t)-1, &f, &used)) !=
	       RF_NEED)
	{
		struct rlist l = {0};
		char *t = NULL;

		off += used;
		if (r == RF_CLOSE)
			pr_end(&h->p, PR_FAIL, "sent a frame over the size limit");
		else if (r != RF_FRAME || (f.type != 0x11 && f.type != 0x13 &&
		                           f.type != 0x14))
			continue;	/* 0x12, 0x18, a streamed type: not ours */
		else if (f.type == 0x13)
			pr_end(&h->p, err_text(f.data, f.len, why, sizeof why) ==
			       DCH_EXIT_REFUSED ? PR_REFUSED : PR_FAIL, why);
		else if (f.type == 0x11 && (memchr(f.data, 0, f.len) ||
		         !(t = malloc(f.len + 1))))
			pr_end(&h->p, PR_FAIL, "sent an unreadable list");
		else
		{
			h->end = now + quiet;
			h->at = (long long)time(NULL);
			if (f.type == 0x14)
				continue;
			memcpy(t, f.data, f.len);
			t[f.len] = '\0';
			bad = rows_doc(t, h->p.h.label, h->at, &l) < 0;
			free(t);
			if (bad)
			{
				free(l.v);
				pr_end(&h->p, PR_FAIL, "sent an unreadable list");
			}
			else
			{
				rc |= !rows_same(&l, &h->rows);
				free(h->rows.v);
				h->rows = l;
				continue;
			}
		}
		h->live = 0;	/* ended: fh_settle's */
		rc = 1;
	}
	if (off)
	{
		memmove(h->p.in.p, h->p.in.p + off, h->p.in.n - off);
		h->p.in.n -= off;
	}
	return rc;
}

/* h's stream is readable. As fh_frames. */
static int
fh_read(struct fhost *h, long long now, long long quiet)
{
	unsigned char buf[RM_READ];
	ssize_t k = sock_read(h->p.fd, buf, sizeof buf);
	int e = sock_err();

	if (k < 0 && (e == SE_INTR || e == SE_AGAIN))
		return 0;
	if (k > 0)
		sb_add(&h->p.in, buf, (size_t)k);
	if (k > 0 && !h->p.in.bad)
		return fh_frames(h, now, quiet);
	pr_end(&h->p, PR_FAIL, k > 0 ? "sent too much" : k ? sock_str(e)
	       : "closed the connection");
	h->live = 0;
	return 1;
}

/* h's dial, once it is over: its rows (then its stream, when its hello
** lists "watch"), else the failure, its next dial after the backoff. 1
** when it changed what h shows. */
static int
fh_settle(struct fhost *h, long long now, long long every, long long quiet)
{
	struct rlist l = {0};
	struct sb b = {0};
	char dev[64];

	if (h->live || h->idle || h->p.st == PR_CONN || h->p.st == PR_WAIT)
		return 0;
	h->idle = 1;
	if (h->p.st == PR_OK && rows_doc(h->p.list, h->p.h.label,
	                                 (long long)time(NULL), &l) == 0)
	{
		free(h->rows.v);
		h->rows = l;
		h->at = (long long)time(NULL);
		h->ok = 1;
		h->gone = h->fails = 0;
		h->due = now + every;
		if (!hello_op(h->p.hello_js, "watch"))
		{
			pr_end(&h->p, PR_OK, NULL);
			return 1;
		}
		/* one per process: the host keeps 4 streams of a device */
		snprintf(dev, sizeof dev, "{\"device_id\":\"dch-%ld\"}",
		         (long)getpid());
		put_frame(&b, 0x2e, dev, strlen(dev));
		/* a few dozen bytes into an idle socket: sent whole */
		if (!b.bad && send(h->p.fd, (const void *)b.p, b.n, MSG_NOSIGNAL)
		    == (ssize_t)b.n)
		{
			h->live = 1;
			h->idle = 0;
			h->end = now + quiet;
			(void)fh_frames(h, now, quiet);	/* what came after it */
		}
		else
			pr_end(&h->p, PR_OK, NULL);	/* probed, as one without */
		free(b.p);
		return 1;
	}
	free(l.v);
	if (h->p.st == PR_OK)
		pr_end(&h->p, PR_FAIL, "sent an unreadable list");
	if ((h->p.st == PR_NEWER || h->p.st == PR_REFUSED) && !h->gone)
		fprintf(stderr, h->p.st == PR_NEWER ? "dch: %s %s; skipped\n"
		        : "dch: %s refused: %s\n", h->p.h.label, h->p.why);
	if (h->p.st == PR_NEWER || h->p.st == PR_REFUSED)
	{
		h->gone = 1;	/* as --ls-json --remote: none of its rows */
		h->rows.n = 0;
	}
	h->ok = 0;
	h->due = now + (every << (h->fails < 4 ? h->fails : 4));
	h->fails++;
	return 1;
}

/* h dialled again: its hello and list asked, RM_BG_MS for the answer */
static void
fh_dial(struct fhost *h, int box)
{
	fh_close(h);
	h->p.keep = 1;
	pr_start(&h->p, 1, now_ms() + box);
	h->idle = 0;
	h->end = now_ms() + RM_BG_MS;
}

/* The hosts --ls-json --remote would dial now (discovery's tailscale call
** given ms): one no longer there closed and dropped, a new one dialled at
** once, its cached rows (stale) shown until it answers. When discovery
** cannot tell, they stand. 1 when they changed. */
static int
fl_hosts(struct fhost *v, int *n, long long now, int ms, int loud)
{
	struct rhost hv[RM_HOSTS];
	struct rlist old = {0};
	long long t = (long long)time(NULL);
	int k = candidates(hv, RM_HOSTS, ms < RM_TS_MS ? ms : RM_TS_MS, loud);
	int i, j, ch = 0, got = 0;

#define FH_SAME(a, b) (!strcmp((a)->label, (b)->label) && \
                       !strcmp((a)->addr, (b)->addr) && (a)->port == (b)->port)
	for (i = 0; k >= 0 && i < *n;)
	{
		for (j = 0; j < k && !FH_SAME(&v[i].p.h, &hv[j]); j++)
			;
		if (j < k)
		{
			i++;
			continue;
		}
		fh_close(&v[i]);
		free(v[i].rows.v);
		v[i] = v[--*n];
		ch = 1;
	}
	for (j = 0; j < k && *n < RM_HOSTS; j++)
	{
		struct fhost *h = &v[*n];

		for (i = 0; i < *n && !FH_SAME(&v[i].p.h, &hv[j]); i++)
			;
		if (i < *n)
			continue;
		if (!got++)
			cache_load(&old, t);
		memset(h, 0, sizeof *h);
		h->p.h = hv[j];
		h->p.fd = -1;
		h->idle = 1;
		h->due = now;
		for (i = 0; i < old.n; i++)
		{
			struct dch_rrow *r;

			/* 0 < at <= t + 60: no overflow */
			if (strcmp(old.v[i].host, hv[j].label) ||
			    old.v[i].at < t - RM_TTL_S || !(r = rl_new(&h->rows)))
				continue;
			*r = old.v[i];
			h->rows.n++;
			if (r->at > h->at)
				h->at = r->at;
		}
		(*n)++;
		ch = 1;
	}
#undef FH_SAME
	free(old.v);
	return ch;
}

/* What every host shows now: a fresh one's rows; else its last, stale,
** for RM_TTL_S after it last answered; in dch_remote_rows' order. */
static void
fl_rows(const struct fhost *v, int n, struct rlist *l)
{
	long long t = (long long)time(NULL);
	struct dch_rrow *r;
	int i, k;

	for (i = 0; i < n; i++)
		for (k = 0; !v[i].gone && (v[i].ok || v[i].at >= t - RM_TTL_S) &&
		     k < v[i].rows.n && (r = rl_new(l)); k++)
		{
			*r = v[i].rows.v[k];
			r->stale = !v[i].ok;
			l->n++;
		}
	if (l->n > 1)
		qsort(l->v, (size_t)l->n, sizeof *l->v, row_cmp);
}

static void
fl_free(struct fhost *v, int n)
{
	int i;

	for (i = 0; i < n; i++)
	{
		fh_close(&v[i]);
		free(v[i].rows.v);
	}
	free(v);
}

/* One look at the list: --ls-json's code prints it in a child, which
** returns 1 here with the remote rows in *out, *nout. The parent reads it
** (RM_ATTACH_MS at most, then SIGKILLs it) and prints it when it is whole
** and not *last: 0, or -1 once our stdout is gone. save: the rows to the
** cache too. */
static int
fl_look(const struct fhost *v, int n, struct sb *last, int save,
        struct dch_rrow **out, int *nout)
{
	struct rlist l = {0};
	struct sb b = {0};
	long long end = now_ms() + RM_ATTACH_MS;
	int p[2], st = 1, rc = 0;
	pid_t pid;

	fl_rows(v, n, &l);
	if (save)
		cache_save(&l);
	fflush(stdout);
	if (pipe_ce(p) < 0)
	{
		free(l.v);
		return 0;
	}
	if ((pid = fork()) == 0)
	{
		child_signals();
		dup2(p[1], 1);
		close(p[0]);
		close(p[1]);
		*out = l.v;
		*nout = l.n;
		return 1;
	}
	close(p[1]);
	free(l.v);
	while (pid > 0)
	{
		unsigned char buf[RM_READ];
		struct pollfd pf = { p[0], POLLIN, 0 };
		long long left = end - now_ms();
		ssize_t k;

		if (left <= 0 || g_ctl_sig ||
		    (poll(&pf, 1, (int)left) < 0 && errno != EINTR))
		{
			kill(pid, SIGKILL);	/* a wedged master: next tick */
			break;
		}
		if (!pf.revents)
			continue;
		if ((k = read(p[0], buf, sizeof buf)) < 0 && errno == EINTR)
			continue;
		if (k <= 0)
			break;
		sb_add(&b, buf, (size_t)k);
	}
	close(p[0]);
	while (pid > 0 && waitpid(pid, &st, 0) < 0 && errno == EINTR)
		;
	if (pid > 0 && !b.bad && WIFEXITED(st) && !WEXITSTATUS(st) &&
	    b.n > 2 && b.p[0] == '[' && b.p[b.n - 1] == '\n' &&
	    (b.n != last->n || memcmp(b.p, last->p, b.n)))
	{
		size_t off = 0;
		ssize_t k;

		/* write(2), not stdio: a SIGINT or SIGTERM while a reader that
		** stopped reading holds the pipe full ends the write (no
		** SA_RESTART) and leaves nothing buffered for exit to flush;
		** a line cut there is the last output (QA 1.62.0 MED-R1) */
		while (off < b.n && !g_ctl_sig)
			if ((k = write(1, b.p + off, b.n - off)) > 0)
				off += (size_t)k;
			else if (k == 0 || errno != EINTR)
				break;
		if (off < b.n)
			rc = -1;	/* the reader is gone, or a signal */
		free(last->p);
		*last = b;
		return rc;
	}
	free(b.p);
	return 0;
}

int
dch_remote_follow(int remote, struct dch_rrow **out)
{
	struct fhost *v = calloc(RM_HOSTS, sizeof *v);
	struct sb last = {0};
	long long every = env_ms("DCH_REMOTE_EVERY_MS", RM_EVERY_MS);
	long long quiet = env_ms("DCH_REMOTE_QUIET_MS", RM_QUIET_MS);
	long long start = now_ms(), tick = start, looked = 0, now;
	int box = box_ms(), n = 0, i, dirty = 1, save = 0, first = 1, r;
	struct sigaction sa;

	*out = NULL;
	if (!v)
	{
		fprintf(stderr, "dch: out of memory\n");
		return -2;
	}
	/* a closed stdout is a write that fails, or a POLLERR/POLLHUP: exit 0 */
	signal(SIGPIPE, SIG_IGN);
	/* no SA_RESTART: a write blocked on a full pipe ends (fl_look) */
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = ctl_sig;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	while (!g_ctl_sig)
	{
		struct pollfd pf[RM_HOSTS + 1];
		int map[RM_HOSTS + 1], np = 0, pend = 0;
		long long wake;

		now = now_ms();
		if (now >= tick)
		{
			/* macOS answers POLLHUP to a POLLOUT poll only, which
			** would spin on a writable pipe: once a tick */
			struct pollfd o = { 1, POLLOUT, 0 };

			/* not POLLNVAL: macOS says it of /dev/null (QA 1.62.0
			** NIT-R1); a closed fd 1 shows in the poll below */
			if (poll(&o, 1, 0) > 0 &&
			    (o.revents & (POLLERR | POLLHUP)))
				break;
			tick = now + every;
			dirty = 1;
			if (remote && fl_hosts(v, &n, now, box, first))
				save = 1;
			now = now_ms();
		}
		for (i = 0; i < n; i++)
		{
			struct fhost *h = &v[i];

			if (h->idle && now >= h->due)
				fh_dial(h, box);
			else if (!h->idle && now >= h->end)
			{
				char why[64];

				snprintf(why, sizeof why, h->live ? "silent for %lld "
				         "ms" : "no answer within %lld ms", h->live
				         ? quiet : (long long)RM_BG_MS);
				pr_end(&h->p, PR_FAIL, why);
				h->live = 0;
			}
			if (fh_settle(h, now_ms(), every, quiet))
				dirty = save = 1;
			pend |= !h->idle && !h->live;
		}
		/* the first array: every host answered, or --ls-json's box */
		if (dirty && (!first || !pend || now_ms() >= start + box) &&
		    now_ms() >= looked + FL_GAP_MS)
		{
			int nr = 0;

			if ((r = fl_look(v, n, &last, save, out, &nr)) == 1)
			{
				fl_free(v, n);	/* the child: it prints */
				free(last.p);
				return nr;
			}
			if (r < 0)
				break;
			dirty = save = first = 0;
			looked = now_ms();
		}
		wake = first && start + box < tick ? start + box : tick;
		if (dirty && !first && looked + FL_GAP_MS < wake)
			wake = looked + FL_GAP_MS;	/* the look the gap held */
		for (i = 0; i < n; i++)
		{
			struct fhost *h = &v[i];
			long long w = h->idle ? h->due : h->end;

			if (w < wake)
				wake = w;
			if (h->idle || h->p.fd < 0)
				continue;
			pf[np].fd = h->p.fd;
			pf[np].events = h->p.st == PR_CONN ? POLLOUT : POLLIN;
			pf[np].revents = 0;
			map[np++] = i;
		}
		/* Linux flags a reader-less pipe with POLLERR, asked or not */
		pf[np].fd = 1;
		pf[np].events = 0;
		pf[np].revents = 0;
		map[np++] = -1;
		/* a SIGINT or SIGTERM during the refresh: not a tick late.
		** shortcut: one landing between this test and the poll still
		** waits for the wake, a pselect-style wait if it is ever seen */
		if (g_ctl_sig)
			break;
		now = now_ms();
		if (poll(pf, (nfds_t)np, wake > now ? (int)(wake - now) : 0) < 0)
		{
			if (errno == EINTR)
				continue;
			break;
		}
		if (pf[np - 1].revents & (POLLERR | POLLHUP | POLLNVAL))
			break;
		for (i = 0; i < np - 1; i++)
			if (pf[i].revents && v[map[i]].live)
			{
				if (fh_read(&v[map[i]], now_ms(), quiet))
					dirty = save = 1;
			}
			else if (pf[i].revents)
				pr_io(&v[map[i]].p);
	}
	fl_free(v, n);
	free(last.p);
	return -1;
}
#endif
