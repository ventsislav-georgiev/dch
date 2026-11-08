/*
** The dch client for remote hosts (#087): the sessions of the user's other
** machines, served over Prosper's remote frames 1.0 (TCP 8771) by `dch
** serve` (serve.c) or by Prosper's DchSessionServer on a Mac.
**
** Hosts. --hosts (or the config's remote_hosts), else discovery: the online
** peers of `tailscale status --json` owned by this host's own Tailscale
** user (numeric UserID), untagged, at their 100.64.0.0/10 address. Nothing
** else is ever dialled. No tailscale CLI, no peers, DCH_NO_REMOTE=1 or
** remote_hosts = off: no remote rows, never an error.
**
** Listing. Every host is probed at once, non-blocking, within one time box:
** connect, 0x08 and 0x03 pipelined (both read-only; the spec lets them
** share a connection), then 0x18 (a newer major refuses the host) and 0x11.
** The rows go to a cache in dch's state dir; a host that did not answer in
** time shows its cached rows, marked stale, for RM_TTL_S. `--ls` probes in
** the foreground (RM_BOX_MS); the picker reads the cache and re-probes in a
** forked child, so nothing stalls it.
**
** Attach. A relay child bridges attach.c's packets on a socketpair to the
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
** ponytail: no 0x2e watch stream (the picker re-probes every RM_EVERY_MS),
** and no remote kill, rename or create: those need ops-gated frames, and a
** confirmation in the picker the way the local ones have one.
*/
#include "dtach.h"
#include "rframe.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <strings.h>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0	/* SIGPIPE is ignored */
#endif

#define RM_PORT 8771
#define RM_HOSTS 64		/* hosts probed, at most */
#define RM_ROWS 1000		/* rows kept per host, at most */
#define RM_BOX_MS 300		/* --ls: every probe, in parallel, within */
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
};

static const char *g_hosts;	/* --hosts */

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
** untagged and owned by Self's user (by numeric id), each at its first
** IPv4 in 100.64.0.0/10. None on a tagged host: no user owns it. Pure. */
static int
peers_from(const char *js, struct rhost *v, int cap)
{
	struct json_parser jp;
	char node[NODE_ID_MAX];
	long long me, uid;
	int tagged, peers, i, n = 0;

	if (status_self(js, &me, node, &tagged) < 0 || tagged)
		return 0;
	if (dch_json_parse(&jp, js) < 0)
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
		    jint(&jp, p, "UserID", &uid) != 1 || uid != me ||
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

/* The hosts to dial: --hosts, else remote_hosts, else discovery. */
static int
candidates(struct rhost *v, int cap)
{
	char cfg[1024], bad[400], *out = NULL;
	char *av[] = { NULL, "status", "--json", NULL };
	int st, n = 0;

	if (!remote_on())
		return 0;
	if (g_hosts)
		return hosts_parse(g_hosts, v, cap, bad, sizeof bad);
	if (dch_config_get("remote_hosts", cfg, sizeof cfg) && cfg[0])
	{
		if ((n = hosts_parse(cfg, v, cap, bad, sizeof bad)) < 0)
			fprintf(stderr, "dch: remote_hosts: bad entry %s\n", bad);
		return n < 0 ? 0 : n;
	}
	if (!g_ts[0])
		ts_find();
	if (!g_ts[0])
		return 0;
	av[0] = g_ts;
	st = rf_run(av, &out, NULL, RM_CACHE_MAX, NULL, 0, RM_TS_MS);
	if (st >= 0 && WIFEXITED(st) && WEXITSTATUS(st) == 0 && out)
		n = peers_from(out, v, cap);
	free(out);
	return n;
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
** answered at `at`), or the cache's (host NULL: each row names its host
** and when it answered). A row without a usable name is skipped; the
** alias kept only when it fits, the state only when it is one of the
** four. At most RM_ROWS. Pure. */
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
		         !r->host[0] || jint(jp, i, "at", &a) != 1)
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

static int
cache_path(char *out, size_t cap)
{
	char dir[PATH_MAX], *sl;

	if (dch_history_dir(dir, sizeof dir, NULL, 1) < 0 ||
	    !(sl = strrchr(dir, '/')))
		return -1;
	*sl = '\0';	/* <state>/dch/sessions: <state>/dch */
	return (size_t)snprintf(out, cap, "%s/remote-cache.json", dir) < cap
	       ? 0 : -1;
}

/* The cached rows, each its host's and when that host answered. */
static void
cache_load(struct rlist *l)
{
	char path[PATH_MAX + 32], *text;
	struct stat st;
	ssize_t k;
	int fd;

	if (cache_path(path, sizeof path) < 0 ||
	    (fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)) < 0)
		return;
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
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
		(void)rows_doc(text, NULL, 0, l);
	}
	free(text);
}

static void
cache_save(const struct rlist *l)
{
	char path[PATH_MAX + 32], tmp[PATH_MAX + 64];
	struct sb b = {0};
	int i, fd;
	ssize_t w;

	if (cache_path(path, sizeof path) < 0)
		return;
	sb_s(&b, "[");
	for (i = 0; i < l->n; i++)
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
	snprintf(tmp, sizeof tmp, "%s.%ld.tmp", path, (long)getpid());
	if (!b.bad && (fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC |
	                         O_CLOEXEC | O_NOFOLLOW, 0600)) >= 0)
	{
		w = write(fd, b.p, b.n);
		if (close(fd) < 0 || w != (ssize_t)b.n || rename(tmp, path) < 0)
			unlink(tmp);
	}
	free(b.p);
}

/* ---- probes ---------------------------------------------------------- */

enum { PR_CONN, PR_WAIT, PR_OK, PR_FAIL, PR_NEWER };

struct probe
{
	struct rhost h;
	int fd, st, hello;
	struct rf_state rs;
	struct sb in;
	char *list;		/* the 0x11 payload, NUL-terminated */
	char why[300];		/* PR_FAIL, PR_NEWER: printable */
};

/* h's address: an IPv4 literal, else the system resolver (a --hosts name;
** MagicDNS answers a tailnet one). ponytail: getaddrinfo blocks outside
** the time box; only a name the user typed gets here. */
static int
resolve(const struct rhost *h, struct sockaddr_in *sa)
{
	struct addrinfo hint, *res;

	memset(sa, 0, sizeof *sa);
	sa->sin_family = AF_INET;
	sa->sin_port = htons(h->port);
	if (inet_pton(AF_INET, h->addr, &sa->sin_addr) == 1)
		return 0;
	memset(&hint, 0, sizeof hint);
	hint.ai_family = AF_INET;
	hint.ai_socktype = SOCK_STREAM;
	if (getaddrinfo(h->addr, NULL, &hint, &res) != 0)
		return -1;
	sa->sin_addr = ((struct sockaddr_in *)(void *)res->ai_addr)->sin_addr;
	freeaddrinfo(res);
	return 0;
}

static void
pr_end(struct probe *p, int st, const char *why)
{
	if (p->fd >= 0)
		close(p->fd);
	p->fd = -1;
	p->st = st;
	if (why)
		logsafe(p->why, sizeof p->why, why);
}

/* A non-blocking connect to h: the socket, or -1 with errno. */
static int
tcp_dial(const struct rhost *h)
{
	struct sockaddr_in sa;
	int fd, e;

	if (resolve(h, &sa) < 0)
	{
		errno = EHOSTUNREACH;
		return -1;
	}
	if ((fd = socket(AF_INET, SOCK_STREAM, 0)) < 0)
		return -1;
	cloexec(fd);
	nonblock(fd);
	if (connect(fd, (struct sockaddr *)&sa, sizeof sa) < 0 &&
	    errno != EINPROGRESS)
	{
		e = errno;
		close(fd);
		errno = e;
		return -1;
	}
	return fd;
}

/* The "message" of a 0x13 payload, escaped, into out[cap]. */
static void
err_text(const unsigned char *d, size_t n, char *out, size_t cap)
{
	struct json_parser jp;
	char t[1024], m[256] = "";

	if (n < sizeof t && !memchr(d, 0, n))
	{
		memcpy(t, d, n);
		t[n] = '\0';
		if (dch_json_parse(&jp, t) == 0 && jp.ntok > 0 &&
		    jp.tok[0].kind == JSON_OBJECT)
			(void)jstr(&jp, 0, "message", m, sizeof m);
		dch_json_free(&jp);
	}
	logsafe(out, cap, m[0] ? m : "error");
}

static void
pr_frame(struct probe *p, const struct rf_frame *f)
{
	char *t, proto[32], why[400];
	int major;

	if (f->type == 0x13)
	{
		err_text(f->data, f->len, why + 6, sizeof why - 6);
		memcpy(why, "says: ", 6);
		pr_end(p, PR_FAIL, why);
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
		pr_end(p, PR_OK, NULL);
}

static void
pr_read(struct probe *p)
{
	unsigned char buf[RM_READ];
	struct rf_frame f;
	size_t off = 0, used;
	ssize_t k = read(p->fd, buf, sizeof buf);
	int r;

	if (k < 0 && (errno == EINTR || errno == EAGAIN))
		return;
	if (k <= 0)
	{
		pr_end(p, PR_FAIL, k ? strerror(errno) : "closed the connection");
		return;
	}
	sb_add(&p->in, buf, (size_t)k);
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

/* Every probe at once, all done within ms. Each ends PR_OK (hello and
** list), PR_NEWER or PR_FAIL, its socket closed. */
static void
probe_all(struct probe *v, int n, int ms)
{
	static const unsigned char rq[] = { 0x08, 0, 0, 0, 0, 0x03, 0, 0, 0, 0 };
	long long end = now_ms() + ms;
	struct pollfd pf[RM_HOSTS];
	int map[RM_HOSTS], i, np;

	for (i = 0; i < n; i++)
	{
		v[i].rs.from_server = 1;
		v[i].fd = -1;
		if ((v[i].fd = tcp_dial(&v[i].h)) < 0)
			pr_end(&v[i], PR_FAIL, strerror(errno));
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
		if (!np || left <= 0)
			break;
		if (poll(pf, (nfds_t)np, (int)left) < 0)
		{
			if (errno == EINTR)
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
			else if (getsockopt(p->fd, SOL_SOCKET, SO_ERROR, &e,
			                    &l) < 0 || e)
				pr_end(p, PR_FAIL, strerror(e ? e : errno));
			else if (send(p->fd, rq, sizeof rq, MSG_NOSIGNAL) !=
			         (ssize_t)sizeof rq)
				pr_end(p, PR_FAIL, strerror(errno));
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
		free(v[i].in.p);
		v[i].in.p = NULL;
	}
}

static void
probes_free(struct probe *v, int n)
{
	int i;

	for (i = 0; i < n; i++)
		free(v[i].list);
	free(v);
}

/* Probe every host within ms; the rows of those that answered, then the
** cached rows (stale) of those that did not, for RM_TTL_S. Saves the
** cache. loud: a host that speaks a newer protocol is said on stderr. */
static void
rows_now(struct rlist *l, int ms, int loud)
{
	struct rhost hv[RM_HOSTS];
	struct probe *pv;
	struct rlist old = {0};
	long long now = (long long)time(NULL);
	int n = candidates(hv, RM_HOSTS), i, k;

	if (n <= 0 || !(pv = calloc((size_t)n, sizeof *pv)))
		return;
	for (i = 0; i < n; i++)
		pv[i].h = hv[i];
	probe_all(pv, n, ms);
	cache_load(&old);
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
		for (k = 0; k < old.n; k++)
		{
			struct dch_rrow *r;

			if (strcmp(old.v[k].host, hv[i].label) ||
			    now - old.v[k].at > RM_TTL_S || !(r = rl_new(l)))
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

static pid_t bg_pid = -1;
static long long bg_at = -RM_EVERY_MS;

int
dch_remote_rows(struct dch_rrow **out, int now)
{
	struct rlist l = {0};
	long long t = (long long)time(NULL);
	int i, k;

	*out = NULL;
	if (!remote_on())
		return 0;
	if (now)
		rows_now(&l, RM_BOX_MS, 1);
	else
	{
		/* the picker: the cache, re-probed behind it */
		if (bg_pid > 0 && waitpid(bg_pid, NULL, WNOHANG) != 0)
			bg_pid = -1;
		if (bg_pid < 0 && now_ms() - bg_at >= RM_EVERY_MS)
		{
			bg_at = now_ms();
			if ((bg_pid = fork()) == 0)
			{
				struct rlist bl = {0};
				int nul = open("/dev/null", O_RDWR);

				signal(SIGINT, SIG_DFL);
				signal(SIGTERM, SIG_DFL);
				signal(SIGHUP, SIG_DFL);
				signal(SIGWINCH, SIG_DFL);
				if (nul >= 0)
				{
					dup2(nul, 0);
					dup2(nul, 1);
					dup2(nul, 2);
				}
				rows_now(&bl, RM_BG_MS, 0);
				_exit(0);
			}
		}
		cache_load(&l);
		for (i = k = 0; i < l.n; i++)
			if (t - l.v[i].at <= RM_TTL_S)
			{
				l.v[k] = l.v[i];
				l.v[k++].stale = t - l.v[i].at > RM_FRESH_S;
			}
		l.n = k;
	}
	if (l.n > 1)
		qsort(l.v, (size_t)l.n, sizeof *l.v, row_cmp);
	*out = l.v;
	return l.n;
}

void
dch_remote_esc(char *out, size_t cap, const char *s)
{
	logsafe(out, cap, s);
}

/* ---- attach ---------------------------------------------------------- */

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
	sb_s(&j, "}");
	if (j.bad)
		b->bad = 1;
	else
		put_frame(b, type, j.p, j.n);
	free(j.p);
}

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

/* The relay child: the attach client's packets on sp as frames on tcp,
** 0x10 back raw. 0 when the session ended (0x12) or the attach side
** closed (a detach: the TCP close is all, nothing is killed); else 2, with
** why the connection ended written to `why`. */
static int
relay(int tcp, int sp, int why, const char *label)
{
	struct sb tin = {0}, to_tcp = {0}, to_sp = {0};
	struct rf_state rs = { 0, 0, 1 };
	unsigned char pin[PKT_HDR + PKT_MAX], buf[RM_READ];
	size_t pn = 0, toff = 0, soff = 0;
	int redraws = 0, eof = 0, exited = 0;
	long long drain = 0;
	char msg[400] = "";

	signal(SIGPIPE, SIG_IGN);
	nonblock(tcp);
	nonblock(sp);
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
				snprintf(msg, sizeof msg, "%s", strerror(errno));
				eof = 1;
				drain = now_ms() + 2000;
			}
			sb_drop(&to_tcp, &toff);
		}
		/* from the server */
		if (!eof && pf[0].revents & (POLLIN | POLLHUP | POLLERR))
		{
			struct rf_frame f;
			size_t used = 0, off = 0;
			int r;

			k = read(tcp, buf, sizeof buf);
			if (k < 0 && (errno == EINTR || errno == EAGAIN))
				continue;
			if (k <= 0)
			{
				if (k < 0)
					snprintf(msg, sizeof msg, "%s",
					         strerror(errno));
				eof = 1;
			}
			else
				sb_add(&tin, buf, (size_t)k);
			while (!eof && !tin.bad &&
			       (r = rf_parse(&rs, (const unsigned char *)tin.p +
			                     off, tin.n - off, (size_t)-1, &f,
			                     &used)) != RF_NEED)
			{
				off += used;
				if (r == RF_CLOSE)
				{
					snprintf(msg, sizeof msg, "it sent a frame "
					         "over the size limit");
					eof = 1;
				}
				else if (f.type == 0x10 && f.len)
					sb_add(&to_sp, f.data, f.len);
				else if (f.type == 0x12 && r == RF_FRAME)
					exited = 1;
				else if (f.type == 0x13 && r == RF_FRAME &&
				         !msg[0])
				{
					char m[300];

					err_text(f.data, f.len, m, sizeof m);
					snprintf(msg, sizeof msg, "it says: %s", m);
				}
			}
			if (tin.bad || to_sp.bad || to_tcp.bad)
			{
				snprintf(msg, sizeof msg, "out of memory");
				eof = 1;
			}
			if (off)
			{
				memmove(tin.p, tin.p + off, tin.n - off);
				tin.n -= off;
			}
			if (eof)
				drain = now_ms() + 2000;
		}
	}
	if (exited)
		return 0;	/* the session ended, whatever came after */
	{
		char line[800], safe[500];

		logsafe(safe, sizeof safe, msg);
		snprintf(line, sizeof line, "dch: lost the connection to %s%s%s",
		         label, safe[0] ? ": " : "", safe);
		(void)!write(why, line, strlen(line));
	}
	return 2;
}

static pid_t relay_pid = -1;
static int why_fd = -1;

/* attach.c's hook, when the relay closed the session socket: NULL for a
** session that ended, else what to say once the terminal is back. */
static const char *
remote_lost(void)
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
	if (why_fd >= 0)
		k = read(why_fd, msg, sizeof msg - 1);
	if (k <= 0)
		snprintf(msg, sizeof msg, "dch: lost the connection");
	else
		msg[k] = '\0';
	return msg;
}

/* A connect with a deadline: the socket, or -1 with errno. */
static int
tcp_connect(const struct rhost *h, int ms)
{
	struct pollfd pf;
	socklen_t l = sizeof(int);
	int fd = tcp_dial(h), e = 0;

	if (fd < 0)
		return -1;
	pf.fd = fd;
	pf.events = POLLOUT;
	pf.revents = 0;
	while (poll(&pf, 1, ms) < 0 && errno == EINTR)
		;
	if (!pf.revents)
		e = ETIMEDOUT;
	else if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &e, &l) < 0)
		e = errno;
	if (e)
	{
		close(fd);
		errno = e;
		return -1;
	}
	return fd;
}

/* TCP keepalive, so a link that died silently ends the attach in about
** half a minute rather than never. */
static void
keepalive(int fd)
{
	int on = 1, idle = 15, intvl = 5, cnt = 3;

	setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof on);
	setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &on, sizeof on);
#ifdef TCP_KEEPIDLE
	setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof idle);
#elif defined(TCP_KEEPALIVE)
	setsockopt(fd, IPPROTO_TCP, TCP_KEEPALIVE, &idle, sizeof idle);
#endif
#ifdef TCP_KEEPINTVL
	setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof intvl);
#endif
#ifdef TCP_KEEPCNT
	setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof cnt);
#endif
	(void)idle;
	(void)intvl;
	(void)cnt;
}

/* Is name listed on h? 1, or 0 with why said on stderr. A 0x01 for a name
** it does not have would start one there (Prosper's 0x01 creates). */
static int
listed(const struct rhost *h, const char *name, const char *sh,
       const char *sn)
{
	struct probe *p = calloc(1, sizeof *p);
	struct rlist rows = {0};
	int k, found = 0;

	if (!p)
		return 0;
	p->h = *h;
	probe_all(p, 1, RM_ATTACH_MS);
	if (p->st == PR_OK && rows_doc(p->list, h->label, 0, &rows) == 0)
		for (k = 0; k < rows.n && !found; k++)
			found = !strcmp(rows.v[k].name, name);
	if (p->st != PR_OK)
		fprintf(stderr, "dch: %s: %s\n", sh, p->why);
	else if (!found)
		fprintf(stderr, "dch: no session %s on %s\n", sn, sh);
	free(rows.v);
	probes_free(p, 1);
	return found;
}

int
dch_remote_attach(const char *host, const char *name)
{
	struct rhost hv[RM_HOSTS];
	struct winsize ws;
	struct sb out = {0};
	char sh[300], sn[1100];
	int n = candidates(hv, RM_HOSTS), i, fd, sp[2], wp[2], rc;

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
		return 1;
	}
	if (!listed(&hv[i], name, sh, sn))
		return 1;
	if (!isatty(0) || tcgetattr(0, &orig_term) < 0)
	{
		fprintf(stderr, "dch: attaching requires a terminal\n");
		return 1;
	}
	if ((fd = tcp_connect(&hv[i], RM_ATTACH_MS)) < 0)
	{
		fprintf(stderr, "dch: %s: %s\n", sh, strerror(errno));
		return 1;
	}
	keepalive(fd);
	memset(&ws, 0, sizeof ws);
	ioctl(0, TIOCGWINSZ, &ws);
	put_size(&out, 0x01, name, &ws);
	sp[0] = sp[1] = wp[0] = wp[1] = -1;
	if (out.bad || send(fd, out.p, out.n, MSG_NOSIGNAL) != (ssize_t)out.n ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, sp) < 0 || pipe_ce(wp) < 0 ||
	    (relay_pid = fork()) < 0)
	{
		fprintf(stderr, "dch: %s: %s\n", sh, strerror(errno));
		free(out.p);
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
		close(sp[0]);
		close(wp[0]);
		_exit(relay(fd, sp[1], wp[1], hv[i].label));
	}
	close(fd);
	close(sp[1]);
	close(wp[1]);
	cloexec(sp[0]);
	why_fd = wp[0];
	attach_sock = sp[0];
	attach_lost = remote_lost;
	dch_trace("remote attach %s relay=%d", hv[i].label, (int)relay_pid);
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
