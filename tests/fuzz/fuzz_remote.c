/* libFuzzer target: the dch client for remote hosts' pure parts (#087).
** The first byte picks a parser, the rest is its input: the --hosts list,
** `tailscale status --json` (discovery's peer filter), a 0x18 hello, a 0x11
** list or the cache, attach.c's packet stream (as frames, which must parse
** back as a client's), a server's frame stream into the probe and into the
** relay (each cut into reads of a size the input picks), and logsafe at a
** small cap. Every peer string, once escaped, must hold no control byte:
** C0, DEL, C1 or a bidi control. Linked with the rest of dch minus
** remote.o, and dch.c built with -Dmain=dch_main. */
#include "../../remote.c"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

#define NOW 1700000000LL	/* the cache's "now" */

/* Valid UTF-8 (logsafe writes any other byte as \xNN) holding nothing a
** terminal acts on: C0, DEL, C1 (U+0080-009F), the bidi embeddings,
** overrides and isolates (U+202A-202E, U+2066-2069). The ranges are
** spelled out here, not cp_ctl's, so a regression there shows. */
static void
inert(const char *s)
{
	const unsigned char *u = (const unsigned char *)s;
	unsigned cp;
	int k;

	for (; *u; u += k)
		if (!(k = utf8_at(u, &cp)) || cp < 0x20 ||
		    (cp >= 0x7f && cp < 0xa0) || (cp >= 0x202a && cp <= 0x202e) ||
		    (cp >= 0x2066 && cp <= 0x2069))
			abort();
}

static void
printable(const char *s)
{
	char e[2048];

	dch_remote_esc(e, sizeof e, s);
	inert(e);
}

static void
label_ok(const char *l, size_t cap, const char *extra)
{
	if (!l[0] || strlen(l) >= cap || !plain(l, strlen(l), extra))
		abort();
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct rhost hv[RM_HOSTS];
	char *t, bad[400], proto[32];
	int k, i, j;

	if (n < 1 || !(t = malloc(n)))
		return 0;
	memcpy(t, d + 1, n - 1);
	t[n - 1] = '\0';
	switch (d[0] % 8)
	{
	case 0:		/* --hosts */
		k = hosts_parse(t, hv, RM_HOSTS, bad, sizeof bad);
		if (k < 0)
			printable(bad);
		for (i = 0; i < k; i++)
		{
			label_ok(hv[i].label, DCH_RHOST_MAX, "-._");
			label_ok(hv[i].addr, sizeof hv[i].addr, "-.");
			if (!hv[i].port)
				abort();
			for (j = 0; j < i; j++)
				if (!strcasecmp(hv[i].label, hv[j].label))
					abort();
		}
		break;
	case 1:		/* discovery */
	{
		long long me;

		k = peers_from(t, hv, RM_HOSTS, &me);
		for (i = 0; i < k; i++)
		{
			label_ok(hv[i].label, DCH_RHOST_MAX, "-");
			if (!ip_cgnat4(hv[i].addr) || hv[i].port != RM_PORT ||
			    !hv[i].found || me < 0 ||
			    !strstr(t, "\"Running\""))
				abort();
		}
		break;
	}
	case 2:		/* 0x18 */
		k = hello_major(t, proto, sizeof proto);
		if (k < -1 || strlen(proto) >= sizeof proto)
			abort();
		break;
	case 3:		/* 0x11, and the cache */
	{
		struct rlist l = {0};

		(void)rows_doc(t, "h", 1, &l);
		k = l.n;
		(void)rows_doc(t, NULL, NOW, &l);
		for (i = 0; i < l.n; i++)
		{
			struct dch_rrow *r = &l.v[i];

			if (!r->name[0] || !r->host[0] ||
			    (r->state[0] && strcmp(r->state, "working") &&
			     strcmp(r->state, "idle") && strcmp(r->state, "blocked") &&
			     strcmp(r->state, "done")))
				abort();
			printable(r->name);
			printable(r->alias);
			/* the picker prints a cached host raw: a plain label,
			** answered in (0, now + 60], so the TTL sums cannot
			** overflow (UBSan) */
			if (i >= k && (!plain(r->host, strlen(r->host), "-._") ||
			               r->at <= 0 || r->at > NOW + 60 ||
			               (r->at < NOW - RM_TTL_S) !=
			               (NOW - r->at > RM_TTL_S)))
				abort();
		}
		free(l.v);
		break;
	}
	case 4:		/* attach.c's packets as frames */
	{
		struct sb out = {0};
		struct packet pk;
		struct rf_state rs = {0, 0, 0};
		struct rf_frame f;
		size_t off = 0, used = 0;
		int redraws = 0, r;

		while ((r = pkt_next(d + 1 + off, n - 1 - off, &pk, &used)) > 0)
		{
			if (!used || used > n - 1 - off)
				abort();
			pkt_frames(&pk, &redraws, &out);
			off += used;
		}
		for (off = 0; !out.bad && off < out.n; off += used)
		{
			r = rf_parse(&rs, (const unsigned char *)out.p + off,
			             out.n - off, (size_t)-1, &f, &used);
			if (r == RF_CLOSE || r == RF_NEED || !used)
				abort();	/* what we send, whole and in bounds */
		}
		free(out.p);
		break;
	}
	case 5:		/* a server's frames into a probe, in reads of c */
	{
		struct probe *p = calloc(1, sizeof *p);
		size_t c = n > 1 ? (size_t)d[1] % 64 + 1 : 1, off;

		if (!p)
			break;
		p->fd = -1;
		p->st = PR_WAIT;
		p->keep = n > 1 && d[1] & 0x80;
		p->rs.from_server = 1;
		for (off = 2; off < n && p->st == PR_WAIT; off += c)
			pr_feed(p, d + off, n - off < c ? n - off : c);
		inert(p->why);
		if (p->st == PR_OK && !p->list)
			abort();
		if (p->in.n > n)
			abort();	/* what is held came from the input */
		probes_free(p, 1);
		break;
	}
	case 6:		/* the relay's server side, in reads of c */
	{
		struct rf_state rs = { 0, 0, 1 };
		struct sb tin = {0}, to_sp = {0};
		size_t c = n > 1 ? (size_t)d[1] % 64 + 1 : 1, off;
		char msg[400] = "";
		int exited = 0, r = 0;

		for (off = 2; off < n && r == 0; off += c)
		{
			sb_add(&tin, d + off, n - off < c ? n - off : c);
			r = relay_in(&rs, &tin, &to_sp, &exited, msg, sizeof msg);
		}
		inert(msg);
		if (tin.n >= RF_HDR + RF_LIST_MAX && r == 0)
			abort();	/* a held frame past its cap closes */
		free(tin.p);
		free(to_sp.p);
		break;
	}
	case 7:		/* logsafe cut short */
	{
		char out[16];
		size_t cap = n > 1 ? (size_t)d[1] % sizeof out + 1 : 1;

		memset(out, 'Z', sizeof out);
		logsafe(out, cap, n > 2 ? t + 1 : "");
		if (strlen(out) >= cap || (cap < sizeof out && out[cap] != 'Z'))
			abort();
		inert(out);
		break;
	}
	}
	free(t);
	return 0;
}
