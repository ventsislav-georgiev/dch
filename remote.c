/*
** The dch client for remote hosts (#087): the sessions of the user's other
** machines, served over Prosper's remote frames 1.0 (TCP 8771) by `dch
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
** ponytail: no 0x2e watch stream (the picker re-probes every RM_EVERY_MS),
** and no remote kill, rename or create: those need ops-gated frames, and a
** confirmation in the picker the way the local ones have one.
*/
#ifdef _WIN32
#include "win/win.h"	/* Winsock, and the sock_* spellings below */
#else
#include "dtach.h"
#endif
#include "rframe.h"

#ifndef _WIN32
#include <arpa/inet.h>
#endif
#include <ctype.h>
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
#define RM_EVERY_MS 2000	/* the picker re-probes this often */
#define RM_FRESH_S 10		/* the picker: older rows show as stale */
#define RM_TTL_S 120		/* a silent host's cached rows, this long */
#define RM_TS_MS 1500		/* `tailscale status --json` */
#define RM_CACHE_MAX (8u << 20)
#define RM_HIGH 262144		/* relay: past this unsent, stop reading */
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
** first IPv4 in 100.64.0.0/10. None on a tagged host (no user owns it), and
** none unless BackendState is "Running": a stopped or logged-out tailscaled
** still lists its peers, and 100.64.0.0/10 off the tailnet is any ISP's
** carrier-grade NAT. Pure. */
static int
peers_from(const char *js, struct rhost *v, int cap, long long *me)
{
	struct json_parser jp;
	char node[NODE_ID_MAX], bs[32];
	long long uid;
	int tagged, peers, i, n = 0;

	*me = -1;
	if (status_self(js, me, node, &tagged) < 0 || tagged)
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

/* The cached rows, each its host's and when that host answered (now: the
** time, against which a row's is checked). */
static void
cache_load(struct rlist *l, long long now)
{
	char path[PATH_MAX + 32], *text;
	struct stat st;
	ssize_t k;
	int fd;

	if (cache_path(path, sizeof path, ".remote-cache") < 0 ||
	    (fd = open(path, O_RDONLY | O_BINARY | O_CLOEXEC | O_NOFOLLOW)) < 0)
		return;
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) || !file_mine(&st) ||
	    (size_t)st.st_size > RM_CACHE_MAX ||
	    !(text = malloc((size_t)st.st_size + 1)))
	{
		close(fd);
		return;
	}
	k = read(fd, text, (size_t)st.st_size);
	close(fd);
	if (k == st.st_size && !memchr(text, 0, (size_t)k))
	{
		text[k] = '\0';
		(void)rows_doc(text, NULL, now, l);
	}
	free(text);
}

/* l as the cache: up to RM_CACHE_MAX less a row's worst case, so the
** loader never refuses the file (a host past that is left out). The write
** is to a new mkstemp file (created exclusively, here and on Windows)
** renamed over the old, SIGTERM held meanwhile (dch_remote_stop's), so no
** half-written file is ever left behind. */
static void
cache_save(const struct rlist *l)
{
	char path[PATH_MAX + 32], tmp[PATH_MAX + 32];
	struct sb b = {0};
#ifndef _WIN32
	sigset_t term, old;
#endif
	int i, fd;
	ssize_t w;

	if (cache_path(path, sizeof path, ".remote-cache") < 0 ||
	    cache_path(tmp, sizeof tmp, ".remote-cache.XXXXXX") < 0)
		return;
	sb_s(&b, "[");
	for (i = 0; i < l->n && b.n + 6 * (DCH_RHOST_MAX + 2 * 256 + 16) + 64 <
	     RM_CACHE_MAX; i++)
	{
		const struct dch_rrow *r = &l->v[i];

		sb_s(&b, i ? ",\n{\"alias\":" : "{\"alias\":");
		sb_q(&b, r->alias);
		sb_s(&b, ",\"at\":");
		sb_ll(&b, r->at);
		sb_s(&b, ",\"host\":");
		sb_q(&b, r->host);
		sb_s(&b, ",\"name\":");
		sb_q(&b, r->name);
		sb_s(&b, ",\"state\":");
		sb_q(&b, r->state);
		sb_s(&b, "}");
	}
	sb_s(&b, "]\n");
#ifndef _WIN32
	sigemptyset(&term);
	sigaddset(&term, SIGTERM);
	sigprocmask(SIG_BLOCK, &term, &old);
#endif
	if (!b.bad && (fd = mkstemp(tmp)) >= 0)
	{
		w = write(fd, b.p, b.n);
		if (close(fd) < 0 || w != (ssize_t)b.n || rename_over(tmp, path) < 0)
			unlink(tmp);
	}
#ifndef _WIN32
	sigprocmask(SIG_SETMASK, &old, NULL);
#endif
	free(b.p);
}

/* ---- probes ---------------------------------------------------------- */

/* PR_REFUSED: the server's gate turned us away with a 0x13 that has a
** "code" (#094), not a host that is down: why holds its message. */
enum { PR_CONN, PR_WAIT, PR_OK, PR_FAIL, PR_NEWER, PR_REFUSED };

struct probe
{
	struct rhost h;
	struct sockaddr_in sa;
	int have;		/* sa is h's address */
	int fd, st, hello;
	int keep;		/* PR_OK leaves fd open, in holding what followed */
	struct rf_state rs;
	struct sb in;
	char *list;		/* the 0x11 payload, NUL-terminated */
	char why[300];		/* PR_FAIL, PR_NEWER, PR_REFUSED: printable */
};

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
		         "dch's 1.0", proto);
		pr_end(p, PR_NEWER, why);
	}
	else
		p->hello = 1;
	free(t);
	if (p->st == PR_WAIT && p->hello && p->list)
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
	while (p->st == PR_WAIT &&
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

/* Every probe at once, all done within ms, name lookups included. Each
** ends PR_OK (hello and list), PR_NEWER or PR_FAIL, its socket closed (but
** a kept PR_OK's). */
static void
probe_all(struct probe *v, int n, int ms)
{
	static const unsigned char rq[] = { 0x08, 0, 0, 0, 0, 0x03, 0, 0, 0, 0 };
	long long end = now_ms() + ms;
	struct pollfd pf[RM_HOSTS];
	int map[RM_HOSTS], i, np;

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
		{
			struct probe *p = &v[map[i]];
			socklen_t l = sizeof(int);
			int e = 0;

			if (!pf[i].revents)
				continue;
			if (p->st == PR_WAIT)
				pr_read(p);
			else if (getsockopt(p->fd, SOL_SOCKET, SO_ERROR,
			                    (void *)&e, &l) < 0 || e)
				pr_end(p, PR_FAIL, sock_str(e ? e : sock_err()));
			else if (send(p->fd, (const void *)rq, sizeof rq,
			              MSG_NOSIGNAL) != (ssize_t)sizeof rq)
				pr_end(p, PR_FAIL, sock_str(sock_err()));
			else
				p->st = PR_WAIT;
		}
	}
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

static void
probes_free(struct probe *v, int n)
{
	int i;

	for (i = 0; i < n; i++)
	{
		if (v[i].fd >= 0)
			sock_close(v[i].fd);
		free(v[i].list);
		free(v[i].in.p);
	}
	free(v);
}

/* Probe every host within ms, the tailscale call and name lookups
** included; the rows of those that answered, then the cached rows (stale)
** of those that did not, for RM_TTL_S. Saves the cache, and sets the
** candidate labels (written to lfd too, then closed, when lfd >= 0). loud:
** a host that speaks a newer protocol is said on stderr. */
static void
rows_now(struct rlist *l, int ms, int loud, int lfd)
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
	rows_now(&bg_new, RM_BG_MS, 0, -1);
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
		/* DCH_REMOTE_BOX_MS: a slower link (or a sanitizer build) */
		const char *e = getenv("DCH_REMOTE_BOX_MS");
		long box = e ? atol(e) : 0;

#ifdef _WIN32
		bg_stop();	/* one probe at a time: they share rows_now's state */
#endif
		rows_now(&l, box > 0 && box <= 60000 ? (int)box : RM_BOX_MS, 1,
		         -1);
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
			bg_at = now_ms();
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
				rows_now(&bl, RM_BG_MS, 0, lp[1]);
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
#endif
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

/* The server's frames in tin as the relay takes them: 0x10 onto to_sp,
** 0x12 sets *exited, the first 0x13's message into msg[cap] (escaped, kept
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
			*exited = 1;
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
/* The relay child: the attach client's packets on sp as frames on tcp,
** 0x10 back raw; tin and rs hold what followed the list on tcp. 0 when the
** session ended (0x12) or the attach side closed (a detach: the TCP close
** is all, nothing is killed); else dch -a's exit status (remote.h), with
** why the connection ended written to `why`. */
static int
relay(int tcp, int sp, int why, const char *label, struct sb tin,
      struct rf_state rs)
{
	struct sb to_tcp = {0}, to_sp = {0};
	unsigned char pin[PKT_HDR + PKT_MAX], buf[RM_READ];
	size_t pn = 0, toff = 0, soff = 0;
	int redraws = 0, eof = 0, exited = 0, code = 1;
	long long drain = 0;
	char msg[400] = "";

	nonblock(tcp);
	nonblock(sp);
	if (relay_in(&rs, &tin, &to_sp, &exited, &code, msg, sizeof msg) < 0)
	{
		eof = 1;
		drain = now_ms() + 2000;
	}
	for (;;)
	{
		size_t qt = to_tcp.n - toff, qs = to_sp.n - soff;
		struct pollfd pf[2];
		ssize_t k;

		if (eof && (!qs || now_ms() >= drain))
			break;
		pf[0].fd = eof ? -1 : tcp;
		pf[0].events = (short)((qs < RM_HIGH ? POLLIN : 0) |
		                       (qt ? POLLOUT : 0));
		pf[1].fd = sp;
		pf[1].events = (short)((qt < RM_HIGH && !eof ? POLLIN : 0) |
		                       (qs ? POLLOUT : 0));
		pf[0].revents = pf[1].revents = 0;
		if (poll(pf, 2, eof ? 100 : -1) < 0)
		{
			if (errno == EINTR)
				continue;
			snprintf(msg, sizeof msg, "poll: %s", strerror(errno));
			break;
		}
		/* to the terminal */
		if (qs && pf[1].revents & (POLLOUT | POLLHUP | POLLERR))
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
				sb_add(&tin, buf, (size_t)k);
				if (relay_in(&rs, &tin, &to_sp, &exited, &code,
				             msg, sizeof msg) < 0)
					eof = 1;
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
	if (exited)
		return 0;	/* the session ended, whatever came after */
	{
		char line[800];

		end_line(line, sizeof line, code, label, msg);
		(void)!write(why, line, strlen(line));
	}
	return code;
}

static pid_t relay_pid = -1;
static int why_fd = -1;

/* attach.c's hook, when the relay closed the session socket: NULL for a
** session that ended, else what to say once the terminal is back, with the
** exit status in *code (the relay's; 1 for one that died). */
static const char *
remote_lost(int *code)
{
	static char msg[900];
	ssize_t k = -1;
	int st = 0;

	if (relay_pid <= 0)
		return NULL;
	while (waitpid(relay_pid, &st, 0) < 0 && errno == EINTR)
		;
	relay_pid = -1;
	if (WIFEXITED(st) && WEXITSTATUS(st) == 0)
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
** half a minute rather than never. */
static void
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

/* host:name as a connection that has listed name, for the 0x01 to follow
** on it: its socket, the host in *h, and what followed the list in *rest,
** the parser's state in *rs; or -1, why said on stderr (-2: refused). */
static int
attach_open(const char *host, const char *name, struct rhost *h,
            struct sb *rest, struct rf_state *rs)
{
	struct rhost hv[RM_HOSTS];
	char sh[300], sn[1100];
	int n = candidates(hv, RM_HOSTS, RM_TS_MS, 1), i;

	logsafe(sh, sizeof sh, host);
	logsafe(sn, sizeof sn, name);
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
	return open_listed(&hv[i], name, sh, sn, rest, rs);
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
	dch_trace("remote attach %s relay=%d", h.label, (int)relay_pid);
	rc = attach_main(0);	/* returns only to switch sessions */
	attach_sock = -1;
	attach_lost = NULL;
	if (relay_pid > 0)
		while (waitpid(relay_pid, NULL, 0) < 0 && errno == EINTR)
			;
	relay_pid = -1;
	close(why_fd);
	why_fd = -1;
	return rc;
}
#endif
