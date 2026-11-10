/*
** dch serve: a server for Prosper's "remote frames" protocol 1.1, so the
** Prosper iPhone app can drive dch sessions on a host with no Prosper.
** Spec: ventsislav-georgiev/prosper docs/protocol/remote-frames.md; its
** conformance set runs from tests/remote-frames/.
**
** Process model. The parent owns the listening socket and never parses a
** byte from a peer (one it refused and told why is read off and dropped,
** so the close is a FIN). It drops a peer outside 100.64.0.0/10 at accept,
** then forks one handler per connection. The handler runs the owner check
** (tailscale whois) before it reads anything, then serves the connection
** with one poll loop. A slow subprocess stalls only its own connection, and
** a crash costs one connection. Handlers report "attached to <name>" and
** "watching for <device>" up a pipe. The parent then retires the older
** handlers (the newest attach wins, the older told so with a 0x13 "taken";
** at most 4 streams per device).
**
** An attached connection runs a real `dch -E -f -n <name>` client in a pty,
** as Prosper does. The master repaints for a new client, so the first 0x10
** bytes are a full screen. A resize is TIOCSWINSZ, which the client forwards
** as MSG_WINCH. Every other verb runs dch itself: --ls-json, -k, -m, and
** --read --ansi --cursor. Nothing goes through a shell: argv vectors, execv,
** timeouts and capped output only.
**
** ctl (1.1, #114): a 0x30 runs one allowlisted dch verb (rframe.c's
** ctl_check) as a child of the handler, its pipes in the same poll loop:
** its output streams out as 0x31/0x32 while the client keeps up, its exit
** comes as 0x33. A cap, a timeout, a 0x34 (cancel, #118) or the client's
** close ends it (SIGTERM, then SIGKILL). Each 0x30 that runs gets exactly
** one 0x33; after it the connection stays open (warm) for the next 0x30,
** until CTL_IDLE_MS without a frame. A 0x30 while one runs is refused with
** a 0x13 "busy" and the running one goes on; any other refused 0x30 is a
** 0x13, then the FIN. A 0x34 with no ctl running is ignored.
*/
#include "dtach.h"
#include "rframe.h"
#include "xray.h"

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
/* A retired attach's last frame (#102): its own class, not a refusal.
** Ended by another client; do not auto-retry. */
#define TAKEN_JSON "{\"code\":\"taken\",\"message\":\"another client " \
                   "attached to this session\"}"
/* A 0x30 while this connection's ctl runs (#118): refused, nothing closes */
#define BUSY_JSON "{\"code\":\"busy\",\"message\":\"a ctl is already " \
                  "running on this connection\"}"
#define PTY_CHUNK 32768		/* a pty 0x10 frame, as Prosper's */
#define OUT_HIGH 65536		/* past this unsent, stop making more output */
#define PIN_MAX 65536		/* client bytes waiting for the pty */
#define STALL_MS 20000		/* output untaken this long closes (spec) */
#define IDLE_MS 30000		/* no whole frame this long, not attached or
				** watching: closes (slowloris) */
#define LINGER_MS 2000
#define REFUSE_MS 1000		/* a refused peer, told why: closed when it is
				** done, or after this long */
#define REFUSE_N 16		/* refused peers the parent waits on so */
#define WATCH_POLL_MS 2000
#define WATCH_BEAT_MS 25000	/* spec heartbeat */
#define MAX_CONN 64
#define MAX_PER_PEER 16
#define NEG_N 64		/* refused peers remembered by the parent */
#define NEG_MS 30000		/* for this long, refused with no fork */
#define MAX_SELF 16		/* `tailscale ip -4` lines kept */
#define MAX_WATCH_PER_DEV 4	/* spec */
#define DEV_CAP 128
#define MAX_ALLOW 16
#define MAX_CMD 256
#define CTL_MS 60000		/* a ctl's timeout (#114) */
#define CTL_LONG_MS 660000	/* --say, --stop, --revive, --run: dch
				** waits up to 600 s for an agent itself */
#define CTL_CAP (8u << 20)	/* a ctl's stdout and stderr together */
#define CTL_GRACE_MS 2000	/* a cut ctl: SIGTERM, then SIGKILL */
#define CTL_IDLE_MS 300000	/* a warm ctl connection, no frame: closes */
#define CTL_CHUNK 65536

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

/* ---- JSON in --------------------------------------------------------- */

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

/* An attach's optional "scrollback" (#101): rows of history the client's
** replay sends first, clamped to 0..DCH_SCROLLBACK_MAX; 0 when missing or
** not a number. */
static int
rq_scrollback(const struct json_parser *jp)
{
	long long v;

	if (jint(jp, 0, "scrollback", &v) != 1 || v <= 0)
		return 0;
	return v > DCH_SCROLLBACK_MAX ? DCH_SCROLLBACK_MAX : (int)v;
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

/* Verdicts: G_OK and G_OK_NOWHOIS accept, every other one refuses.
** G_OWNER: another user's node on an untagged host; G_NOTALLOWED: a user
** not on --allow-user, on a tagged host. G_BUSY is no gate verdict: the
** parent's connection limits (#095); serve_gate never returns it, no
** handler exits with it, the neg cache never holds it. */
enum { G_OK, G_OK_NOWHOIS, G_RANGE, G_SELF, G_NOWHOIS, G_NOTNODE,
       G_BADWHOIS, G_TAGGED, G_OWNER, G_NOTALLOWED, G_BUSY, G_N };

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
	case G_NOTALLOWED:	return "a user not on --allow-user (tagged host)";
	case G_BUSY:		return "too many connections";
	default:		return "another user's node";
	}
}

/* What a refused tailnet peer is told (#094): one 0x13 {"message","code"},
** the code a stable contract (README, dch.1). NULL for an accept and for
** G_RANGE: an address off the tailnet is told nothing. Never names the
** owner, a user, a node or the allow-list. */
static const char *
gate_code(int v, const char **msg)
{
	static const char *const t[G_N][2] = {
		[G_SELF] = { "self", "this host does not serve connections "
		             "from itself" },
		[G_NOWHOIS] = { "whois_unavailable", "this host could not "
		                "verify you: tailscaled is not answering" },
		[G_NOTNODE] = { "whois_failed", "this host could not verify "
		                "you (tailscale whois failed)" },
		[G_BADWHOIS] = { "whois_failed", "this host could not verify "
		                 "you (tailscale whois failed)" },
		[G_TAGGED] = { "tagged_peer", "this host does not serve tagged "
		               "nodes" },
		[G_OWNER] = { "not_owner", "this host serves only its owner's "
		              "devices" },
		[G_NOTALLOWED] = { "not_allowed", "this host is tagged and "
		                   "serves only the users it names: start dch "
		                   "serve there with --allow-user <your login "
		                   "or id>" },
		/* the one transient code: try again later */
		[G_BUSY] = { "busy", "this host is at its connection limit: "
		             "try again shortly" },
	};

	if (v < 0 || v >= G_N || !t[v][0])
		return NULL;
	*msg = t[v][1];
	return t[v][0];
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
	return g->self_uid >= 0 ? G_OWNER : G_NOTALLOWED;
}

/* ---- tailscale ------------------------------------------------------- */

static uint32_t g_self[MAX_SELF];	/* `tailscale ip -4`, and --bind */
static int g_nself;

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
	sb_s(&g_hello, g_allow_create
	     ? ",\"ops\":[\"create\",\"ctl\",\"ctl-cancel\",\"watch\"]"
	     : ",\"ops\":[\"ctl\",\"ctl-cancel\",\"watch\"]");
	sb_s(&g_hello, ",\"os\":");
	sb_q(&g_hello, os);
	sb_s(&g_hello, ",\"proto\":\"1.1\",\"server\":\"dch\"}");
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
	/* a ctl (0x30, #114): the dch it runs (0 none yet, -1 done: warm),
	** its stdout and stderr (-1 at EOF), what went out of them, its
	** timeout (0 none) and, once cut ("cap", "timeout", "cancel"), when
	** SIGKILL follows the SIGTERM */
	pid_t ctl;
	int ctl_fd[2], ctl_st, ctl_reaped;
	size_t ctl_sent;
	long long ctl_end, ctl_kill;
	const char *ctl_why;
};

static struct conn C;	/* one connection per handler process */
static long g_ctl_ms;		/* DCH_CTL_TIMEOUT_MS (tests): every ctl's */
static size_t g_ctl_cap = CTL_CAP;	/* DCH_CTL_CAP (tests), at most 8 MiB */
static long g_ctl_idle = CTL_IDLE_MS;	/* DCH_CTL_IDLE_MS (tests) */
/* SIGUSR1 from the parent: a newer attach took this handler's session */
static volatile sig_atomic_t g_taken;
/* SIGUSR2 from the parent (or set at fork): slots are short, so a warm ctl
** connection idles a tenth of g_ctl_idle (30 s), not 5 min (#122). Sticky. */
static volatile sig_atomic_t g_press;
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
	int st = rf_run(av, &ls, NULL, SB_LIMIT, NULL, 0, 10000);
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
	char sbn[16];
	int r, k = 0, ncmd = 0, t, m, sb;
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
	/* INFO-2 (#120): a new session never takes a session_id's shape */
	if (name[0] && xr_id_shaped(name) && session_listed(name) != 1)
	{
		dch_json_free(&jp);
		out_err(c, "name looks like a session_id");
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
	sb = rq_scrollback(&jp);
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
	if (sb > 0)
	{
		snprintf(sbn, sizeof sbn, "%d", sb);
		av[k++] = "--scrollback";
		av[k++] = sbn;
	}
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
	    (st = rf_run(av, &scr, &sn, 4u << 20, err, sizeof err, 5000)) < 0)
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
** reported), but for a name starting with '-' or an alias shaped like a
** session_id (INFO-2). */
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
	if (!kill_ && ra == 1 && xr_label_id_shaped(alias, strlen(alias)))
	{
		out_err(c, "name looks like a session_id");	/* INFO-2 */
		return;
	}
	if (r == 1 && name_ok(name) && ra >= 0)
	{
		char *kv[] = { dch_exe, "-k", name, NULL };
		char *mv[] = { dch_exe, "-m", name, alias, NULL };

		if (ra == 0)
			alias[0] = '\0';
		slog("%s: %s %s", c->ipstr, kill_ ? "kill" : "rename", name);
		rf_run(kill_ ? kv : mv, NULL, NULL, 0, NULL, 0, 15000);
	}
	out_frame(c, 0x14, "", 0);
}

/* ---- ctl (0x30, remote frames 1.1, #114) ---------------------------- */

/* A 0x30's payload: its argv (dch_json_strv's block: free it) into *av and
** *ac, and "cwd" ("" when absent) into cwd[PATH_MAX], checked against the
** allowlist (ctl_check) and the cwd for being absolute. The CTL_* flags, or
** -1 with why. Pure. */
static int
ctl_request(const unsigned char *p, size_t n, char ***av, int *ac, char *cwd,
            char *why, size_t cap)
{
	static unsigned char kind[CTL_ARGV_MAX];
	struct json_parser jp;
	int t, r, any, arr;

	*av = NULL;
	*ac = 0;
	cwd[0] = '\0';
	if (rq_json(&jp, rq_text, p, n) < 0)
	{
		snprintf(why, cap, "malformed ctl request: send "
		         "{\"argv\":[...]}");
		return -1;
	}
	r = jstr(&jp, 0, "cwd", cwd, PATH_MAX);
	t = dch_json_field(&jp, 0, "argv");
	arr = t >= 0 && jp.tok[t].kind == JSON_ARRAY;
	any = arr && jp.tok[t].next > t + 1;
	dch_json_free(&jp);
	if (r < 0 || t == -2 || (t >= 0 && !arr) ||
	    (any && !(*av = dch_json_strv(rq_text, NULL, "argv", ac))))
	{
		cwd[0] = '\0';
		snprintf(why, cap, "malformed ctl request: argv must be an "
		         "array of strings, cwd a string");
		return -1;
	}
	if ((t = ctl_check(*ac, *av, kind, why, cap)) >= 0 && r == 1 &&
	    cwd[0] != '/')
	{
		snprintf(why, cap, "no such directory");
		t = -1;
	}
	/* The child's dch reads a name with ':' and no local socket as
	** host:name and dials on with this host's identity: a relay. */
	for (int i = 0; t >= 0 && i < *ac; i++)
		if ((kind[i] == CK_NAME || kind[i] == CK_VALUE) &&
		    strchr((*av)[i], ':'))
		{
			snprintf(why, cap, "a session name with ':' is local "
			         "only");
			t = -1;
		}
	if (t < 0)
	{
		free(*av);
		*av = NULL;
	}
	return t;
}

/* Stop forwarding the ctl's output and SIGTERM it (SIGKILL CTL_GRACE_MS
** later, ctl_tick); its 0x33 says why once it is reaped. */
static void
ctl_cut(struct conn *c, const char *why)
{
	int k;

	c->ctl_why = why;
	for (k = 0; k < 2; k++)
		if (c->ctl_fd[k] >= 0)
		{
			close(c->ctl_fd[k]);
			c->ctl_fd[k] = -1;
		}
	if (!c->ctl_reaped)
	{
		kill(c->ctl, SIGTERM);
		c->ctl_kill = now_ms() + CTL_GRACE_MS;
	}
	slog("%s: ctl cut short: %s", c->ipstr, why);
}

/* The ctl's stdout (k 0) or stderr (1) as 0x31 / 0x32 frames, as it
** comes, while the client keeps up; up to the cap, then cut. Once the ctl
** is reaped an empty pipe is done: what it wrote is in the pipe, and a
** leftover writer (a process it started) is not waited for. */
static void
ctl_read(struct conn *c, int k)
{
	unsigned char buf[CTL_CHUNK];
	ssize_t r;

	while (c->ctl_fd[k] >= 0 && pending(c) <= OUT_HIGH)
	{
		if ((r = read(c->ctl_fd[k], buf, sizeof buf)) < 0 &&
		    errno == EINTR)
			continue;
		if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) &&
		    !c->ctl_reaped)
			return;
		if (r <= 0)
		{
			close(c->ctl_fd[k]);
			c->ctl_fd[k] = -1;
			return;
		}
		if ((size_t)r > g_ctl_cap - c->ctl_sent)
			r = (ssize_t)(g_ctl_cap - c->ctl_sent);
		if (r)
			out_frame(c, 0x31 + (unsigned)k, buf, (size_t)r);
		c->ctl_sent += (size_t)r;
		if (c->ctl_sent >= g_ctl_cap)
		{
			ctl_cut(c, "cap");
			return;
		}
	}
}

/* What a client's 0x30 or 0x34 does (#118), by the connection's ctl (0
** none yet, > 0 running, -1 done) and why it was cut (NULL not cut). Pure,
** so fuzz_serve runs frame sequences through it. */
enum { CA_IGNORE, CA_RUN, CA_BUSY, CA_CANCEL };

static int
ctl_action(unsigned int type, pid_t ctl, const char *why)
{
	if (type == 0x30)
		return ctl > 0 ? CA_BUSY : CA_RUN;
	return type == 0x34 && ctl > 0 && !why ? CA_CANCEL : CA_IGNORE;
}

/* 0x30: dch itself with the request's argv, no shell, stdin /dev/null, in
** its cwd or else HOME. Refused (a 0x13, nothing runs): anything off the
** allowlist, a create verb with create off, a cwd that is no directory;
** then the FIN. Not called while a ctl runs (ctl_action: a 0x13 "busy").
** After its 0x33 the connection takes the next 0x30 (#118). */
static void
on_ctl(struct conn *c, const unsigned char *p, size_t n)
{
	char why[200], cwd[PATH_MAX], **av = NULL, *argv[CTL_ARGV_MAX + 2];
	struct stat st;
	int ac, fl, po[2] = { -1, -1 }, pe[2] = { -1, -1 }, k;
	pid_t pid = -1;

	if (c->attached || c->watching)
	{
		out_err(c, "ctl needs its own connection");
		return;
	}
	c->closing = 1;		/* until the ctl runs: the 0x13, then the FIN */
	if ((fl = ctl_request(p, n, &av, &ac, cwd, why, sizeof why)) < 0 ||
	    !av)
	{
		slog("%s: ctl refused: %s", c->ipstr, why);
		out_err(c, why);
		return;
	}
	if (fl & CTL_CREATE && !g_allow_create)
		snprintf(why, sizeof why, "creating sessions is off on this "
		         "host (dch serve --allow-create)");
	else if (cwd[0] && (stat(cwd, &st) < 0 || !S_ISDIR(st.st_mode)))
		snprintf(why, sizeof why, "no such directory");
	else
		why[0] = '\0';
	if (!why[0])
	{
		argv[0] = dch_exe;
		memcpy(argv + 1, av, (size_t)ac * sizeof *av);
		argv[ac + 1] = NULL;
		if (pipe_ce(po) == 0 && pipe_ce(pe) == 0 && (pid = fork()) == 0)
		{
			const char *home = getenv("HOME");
			int nul = open("/dev/null", O_RDWR);

			if (nul < 0)
				_exit(127);
			dup2(nul, 0);
			dup2(po[1], 1);
			dup2(pe[1], 2);
			if (cwd[0] ? chdir(cwd) < 0
			           : !home || chdir(home) < 0)
				if (cwd[0] || chdir("/") < 0)
					_exit(127);
			exec_prep();
			execv(argv[0], argv);
			_exit(127);
		}
		if (pid < 0)
			snprintf(why, sizeof why, "could not run dch: %s",
			         strerror(errno));
	}
	for (k = 0; k < 2; k++)
	{
		if (po[k] >= 0 && (pid < 0 || k))
			close(po[k]);
		if (pe[k] >= 0 && (pid < 0 || k))
			close(pe[k]);
	}
	if (why[0])
	{
		slog("%s: ctl %s refused: %s", c->ipstr, av[0], why);
		out_err(c, why);
		free(av);
		return;
	}
	nonblock(po[0]);
	nonblock(pe[0]);
	c->closing = 0;
	c->ctl = pid;
	c->ctl_reaped = 0;
	c->ctl_sent = 0;
	c->ctl_kill = 0;
	c->ctl_why = NULL;
	c->ctl_fd[0] = po[0];
	c->ctl_fd[1] = pe[0];
	c->ctl_end = g_ctl_ms > 0 ? now_ms() + g_ctl_ms	/* --follow's too */
	             : fl & CTL_FOREVER ? 0
	             : now_ms() + (fl & CTL_LONG ? CTL_LONG_MS : CTL_MS);
	slog("%s: ctl %s %s (pid %ld)", c->ipstr, av[0], av[1], (long)pid);
	free(av);
}

/* A running ctl, each turn of the loop: reaped, timed out, killed after
** its grace; once reaped and its pipes drained, the 0x33, and the
** connection is warm (its idle clock starts). The
** exit is dch's status, 128 + the signal when one ended it, or 2 with a
** "why" when the server cut it. */
static void
ctl_tick(struct conn *c, long long now)
{
	struct sb b = {0};
	int st;

	if (c->ctl <= 0)
		return;
	if (!c->ctl_reaped && waitpid(c->ctl, &c->ctl_st, WNOHANG) == c->ctl)
		c->ctl_reaped = 1;
	if (!c->ctl_reaped && !c->ctl_why && c->ctl_end && now >= c->ctl_end)
		ctl_cut(c, "timeout");
	if (!c->ctl_reaped && c->ctl_kill && now >= c->ctl_kill)
	{
		kill(c->ctl, SIGKILL);
		c->ctl_kill = 0;
	}
	if (!c->ctl_reaped)
		return;
	ctl_read(c, 0);
	ctl_read(c, 1);
	if (c->ctl_fd[0] >= 0 || c->ctl_fd[1] >= 0)
		return;
	st = c->ctl_st;
	sb_s(&b, "{\"exit\":");
	sb_ll(&b, c->ctl_why ? 2 : WIFEXITED(st) ? WEXITSTATUS(st)
	                              : 128 + WTERMSIG(st));
	if (c->ctl_why)
	{
		sb_s(&b, ",\"why\":");
		sb_q(&b, c->ctl_why);
	}
	sb_s(&b, "}");
	out_frame(c, 0x33, b.p, b.n);
	slog("%s: ctl done: %s", c->ipstr, b.bad ? "?" : b.p);
	free(b.p);
	c->ctl = -1;
	c->last_frame = now;
}

/* The ctl on a connection that ends: SIGTERM (--stop and --say finish
** safely on one), SIGKILL after CTL_GRACE_MS. */
static void
ctl_end(struct conn *c)
{
	long long end;
	pid_t w;
	int k;

	for (k = 0; k < 2; k++)
		if (c->ctl_fd[k] >= 0)
			close(c->ctl_fd[k]);
	c->ctl_fd[0] = c->ctl_fd[1] = -1;
	if (c->ctl <= 0 || c->ctl_reaped)
		return;
	kill(c->ctl, SIGTERM);
	end = now_ms() + CTL_GRACE_MS;
	while ((w = waitpid(c->ctl, &c->ctl_st, WNOHANG)) == 0 ||
	       (w < 0 && errno == EINTR))
	{
		if (w == 0 && now_ms() >= end)
		{
			kill(c->ctl, SIGKILL);
			while (waitpid(c->ctl, &c->ctl_st, 0) < 0 &&
			       errno == EINTR)
				;
			break;
		}
		poll(NULL, 0, 10);
	}
	c->ctl_reaped = 1;
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
			         "this server's 1.1", pv);
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

	/* A ctl connection (running or warm, #118) stays one: frames are read
	** while its ctl runs, so an attach, watch, kill or rename there would
	** mix a pty or a request into the ctl's stream. Hello, list, 0x30 and
	** 0x34 only; anything else is refused and closes. */
	if (c->ctl && type != 0x08 && type != 0x03 && type != 0x30 &&
	    type != 0x34)
	{
		out_err(c, "a ctl connection takes only ctl frames");
		c->closing = 1;
		return;
	}
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
	case 0x30:
	case 0x34:		/* cancel: ignored with no ctl running, or once cut */
		switch (ctl_action(type, c->ctl, c->ctl_why))
		{
		case CA_RUN:
			on_ctl(c, p, n);
			break;
		case CA_BUSY:	/* the running one goes on: nothing closes */
			out_frame(c, 0x13, BUSY_JSON, sizeof BUSY_JSON - 1);
			break;
		case CA_CANCEL:
			ctl_cut(c, "cancel");
			break;
		}
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
	ctl_end(c);
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

/* Tell fd's peer why the gate refused it (verdict v): the one 0x13 that
** gate_code gives (none for G_RANGE), in one non-blocking send that is
** never retried, so a peer that does not read costs nothing; then the FIN.
** The caller closes fd once the peer is done (refuse_drain): closing with
** the peer's hello unread would send a reset, and a reset can eat the 0x13
** (macOS drops it). */
static void
refuse(int fd, int v)
{
	const char *msg, *code = gate_code(v, &msg);
	struct sb b = {0};
	unsigned char f[RF_HDR + 1024];

	nonblock(fd);
	if (!code)
		return;
	sb_s(&b, "{\"code\":");
	sb_q(&b, code);
	sb_s(&b, ",\"message\":");
	sb_q(&b, msg);
	sb_s(&b, "}");
	if (!b.bad && b.n <= sizeof f - RF_HDR)
	{
		f[0] = 0x13;
		f[1] = f[2] = 0;
		f[3] = (unsigned char)(b.n >> 8);
		f[4] = (unsigned char)b.n;
		memcpy(f + RF_HDR, b.p, b.n);
		(void)!send(fd, f, RF_HDR + b.n, MSG_NOSIGNAL);
	}
	free(b.p);
	shutdown(fd, SHUT_WR);
}

/* Read off and drop (never parse) what a refused peer sent, without
** waiting: 1 once it is done (EOF or an error), 0 while it may send more. */
static int
refuse_drain(int fd)
{
	char junk[4096];
	ssize_t r;
	int k;

	for (k = 0; k < 16; k++)
		if ((r = recv(fd, junk, sizeof junk, MSG_DONTWAIT)) <= 0)
			return r == 0 || (errno != EAGAIN && errno != EWOULDBLOCK &&
			                  errno != EINTR);
	return 0;
}

/* The handler's exit status for a refusal the parent caches: NEG_EXIT plus
** the verdict, so the cached refusal says the same (see kids_reap). */
#define NEG_EXIT 16

/* One connection, in the forked handler. Returns the handler's exit
** status: NEG_EXIT + the verdict when the gate refused the peer on
** tailscaled's own word (the parent then refuses that address for NEG_MS
** without forking, with the same 0x13), else 0. */
static int
serve_conn(int fd, uint32_t ip)
{
	struct conn *c = &C;
	struct in_addr a;
	char *whois = NULL;
	int v, graceful = 0;

	g_taken = 0;	/* only the parent's SIGUSR1 to this handler */
	c->fd = fd;
	c->pty = -1;
	c->ctl_fd[0] = c->ctl_fd[1] = -1;
	c->ip = ip;
	a.s_addr = htonl(ip);
	inet_ntop(AF_INET, &a, c->ipstr, sizeof c->ipstr);
	/* The gate, before a byte is read. A refused peer gets one 0x13 that
	** says why, and the close. The parent already refused this host's
	** own addresses. */
	whois = ts_whois(c->ipstr);
	v = serve_gate(&g_gate, ip, 0, whois, !whois && ts_up());
	free(whois);
	if (v >= G_RANGE)
	{
		long long end = now_ms() + REFUSE_MS;
		struct pollfd pf;

		slog("%s: refused: %s", c->ipstr, gate_why(v));
		refuse(fd, v);
		pf.fd = fd;
		pf.events = POLLIN;
		while (!refuse_drain(fd) && now_ms() < end &&
		       poll(&pf, 1, (int)(end - now_ms())) > 0)
			;
		close(fd);
		return v == G_NOWHOIS ? 0 : NEG_EXIT + v;
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
		struct pollfd pf[4];
		int np = 0, si, pi = -1, ci = -1, tmo = 500, k;
		long long now = now_ms();

		if (g_stop)
			break;	/* the server stops: no frame */
		if (g_taken && c->attached && !c->closing)
		{
			/* retired (#102): say why, then the FIN. Nothing more
			** from the pty: the 0x13 is the last frame. */
			slog("%s: %s taken by a newer attach", c->ipstr, c->name);
			out_frame(c, 0x13, TAKEN_JSON, sizeof TAKEN_JSON - 1);
			c->closing = 1;
		}
		if (c->jiggle_at && now >= c->jiggle_at)
		{
			c->jiggle_at = 0;
			set_ws(c, c->rows);
		}
		if (c->watching && !c->closing)
			watch_tick(c, now);
		if (!c->closing)
			parse_input(c);
		ctl_tick(c, now);
		if (c->eof && c->ctl > 0 && !c->ctl_why)
		{
			/* a ctl's client is gone (a FIN is a close here); one
			** already cut (a 0x34 before the FIN) still gets its 0x33 */
			slog("%s: closed by the client; stopping its ctl",
			     c->ipstr);
			break;
		}
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
		if (c->eof && !c->closing && c->ctl <= 0 && c->parse_need &&
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
		if (!c->attached && !c->watching && !c->closing && c->ctl <= 0 &&
		    now - c->last_frame >= (!c->ctl ? IDLE_MS : g_press
		                            ? g_ctl_idle / 10 : g_ctl_idle))
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
		if (c->ctl > 0 && !c->closing && pending(c) <= OUT_HIGH)
			for (ci = np, k = 0; k < 2; k++)
				if (c->ctl_fd[k] >= 0)
				{
					pf[np].fd = c->ctl_fd[k];
					pf[np].events = POLLIN;
					pf[np++].revents = 0;
				}
		if ((c->kid > 0 && !c->kid_done) || c->ctl > 0)
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
		for (k = ci; k >= 0 && k < np; k++)
			if (pf[k].revents & (POLLIN | POLLHUP | POLLERR))
				ctl_read(c, pf[k].fd == c->ctl_fd[0] ? 0 : 1);
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

/* Peers a handler refused on tailscaled's word (exit status NEG_EXIT + the
** verdict), refused again before fork until `until`, with that verdict's
** 0x13: no fork, no whois, no kid slot. Full, the entry that expires first
** goes. An accepted peer never lands here, so neither does the owner. */
static struct neg
{
	uint32_t ip;
	long long until;
	int told;	/* its first cached refusal is logged, no more */
	int v;		/* the handler's verdict */
} neg[NEG_N];

static void
neg_add(uint32_t ip, int v)
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
	n->v = v;
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

/* Peers the parent refused and told why, waited on for REFUSE_MS until
** they are done (refuse_drain) so the close is a FIN; never blocking. Full,
** a refused peer is closed at once. */
static struct { int fd; long long until; } rfd[REFUSE_N];
static int nrfd;

static void
refused_wait(int fd)
{
	if (nrfd == REFUSE_N)
	{
		close(fd);	/* ponytail: a reset may eat its 0x13 */
		return;
	}
	rfd[nrfd].fd = fd;
	rfd[nrfd++].until = now_ms() + REFUSE_MS;
}

/* After a poll over pf (one per rfd, in order): close those done or due. */
static void
refused_tick(const struct pollfd *pf)
{
	long long now = now_ms();
	int i;

	for (i = nrfd - 1; i >= 0; i--)	/* the last moves to i: seen */
		if ((pf[i].revents && refuse_drain(rfd[i].fd)) ||
		    now >= rfd[i].until)
		{
			close(rfd[i].fd);
			rfd[i] = rfd[--nrfd];
		}
}

static void
on_signal(int sig)
{
	if (sig == SIGUSR1)
		g_taken = 1;
	else if (sig == SIGUSR2)
		g_press = 1;
	else if (sig != SIGCHLD)
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
				if (WIFEXITED(st) &&
				    WEXITSTATUS(st) > NEG_EXIT + G_RANGE &&
				    WEXITSTATUS(st) < NEG_EXIT + G_BUSY)
					neg_add(kids[i].ip,
					        WEXITSTATUS(st) - NEG_EXIT);
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
				kill(kids[i].pid, SIGUSR1);
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
		refuse(fd, G_SELF);
		refused_wait(fd);
		return;
	}
	if ((n = neg_hit(ip)))
	{
		if (!n->told++)
			slog("%s: refused before fork for %d s: refused by the "
			     "gate already", ipstr, NEG_MS / 1000);
		refuse(fd, n->v);
		refused_wait(fd);
		return;
	}
	for (i = 0; i < nkids; i++)
		same += kids[i].ip == ip;
	if (nkids >= MAX_CONN || same >= MAX_PER_PEER)
	{
		slog("%s: refused: %s", ipstr, nkids >= MAX_CONN
		     ? "too many connections" : "too many connections from it");
		refuse(fd, G_BUSY);	/* transient: never in the neg cache */
		refused_wait(fd);
		return;
	}
	/* Half the slots taken, the peer's or all: its handlers (all of them)
	** and this one idle out sooner, so warm ctl connections cannot hold
	** the slots for CTL_IDLE_MS. */
	g_press = same + 1 >= MAX_PER_PEER / 2 || nkids + 1 >= MAX_CONN / 2;
	for (i = 0; g_press && i < nkids; i++)
		if (kids[i].ip == ip || nkids + 1 >= MAX_CONN / 2)
			kill(kids[i].pid, SIGUSR2);
	pid = fork();
	if (pid == 0)
	{
		close(lfd);
		close(nr);
		while (nrfd)
			close(rfd[--nrfd].fd);
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
	sigaction(SIGUSR1, &act, NULL);	/* the handler's retire */
	act.sa_flags = SA_RESTART;	/* a handler's pressure: no EINTR */
	sigaction(SIGUSR2, &act, NULL);
	act.sa_flags = 0;
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
	/* ctl's limits for tests (#114), read once and gone, as above */
	if ((e = getenv("DCH_CTL_TIMEOUT_MS")) && atol(e) > 0)
		g_ctl_ms = atol(e);
	if ((e = getenv("DCH_CTL_CAP")) && atol(e) > 0 &&
	    (unsigned long)atol(e) < CTL_CAP)
		g_ctl_cap = (size_t)atol(e);
	if ((e = getenv("DCH_CTL_IDLE_MS")) && atol(e) > 0)
		g_ctl_idle = atol(e);
	unsetenv("DCH_CTL_TIMEOUT_MS");
	unsetenv("DCH_CTL_CAP");
	unsetenv("DCH_CTL_IDLE_MS");

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
		{
			const char *m, *code = gate_code(v, &m);

			printf("%s %s: %s%s%s%s\n", v < G_RANGE ? "accept"
			       : "refuse", check, gate_why(v), code ? " (" : "",
			       code ? code : "", code ? ")" : "");
		}
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
	slog("listening on %s:%ld (dch %s, proto 1.1, ops: %s)", ipstr, port,
	     DCH_VERSION, g_allow_create ? "create, ctl, ctl-cancel, watch"
	                                 : "ctl, ctl-cancel, watch");
	if (g_gate.self_uid >= 0)
		slog("accepting nodes of user %lld%s", g_gate.self_uid,
		     g_gate.nallow ? " and --allow-user" : "");
	else if (g_gate.nallow)
		slog("accepting nodes of --allow-user only");

	while (!g_stop)
	{
		struct pollfd pf[2 + REFUSE_N];
		int tmo = 1000;

		kids_reap();
		pf[0].fd = lfd;
		pf[0].events = POLLIN;
		pf[1].fd = np[0];
		pf[1].events = POLLIN;
		pf[0].revents = pf[1].revents = 0;
		for (i = 0; i < nrfd; i++)
		{
			long long left = rfd[i].until - now_ms();

			pf[2 + i].fd = rfd[i].fd;
			pf[2 + i].events = POLLIN;
			pf[2 + i].revents = 0;
			if (left < tmo)
				tmo = left > 0 ? (int)left : 0;
		}
		if (poll(pf, (nfds_t)(2 + nrfd), tmo) < 0)
		{
			if (errno == EINTR)
				continue;
			slog("poll: %s", strerror(errno));
			break;
		}
		refused_tick(pf + 2);
		if (pf[1].revents)
			kids_read(np[0]);
		if (pf[0].revents & POLLIN)
			accept_one(lfd, np[0]);
	}
	while (nrfd)
		close(rfd[--nrfd].fd);

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
