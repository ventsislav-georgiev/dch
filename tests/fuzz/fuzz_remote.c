/* libFuzzer target: the dch client for remote hosts' pure parts (#087).
** The first byte picks a parser, the rest is its input: the --hosts list,
** `tailscale status --json` (discovery's peer filter), a 0x18 hello, a 0x11
** list or the cache, attach.c's packet stream (as frames, which must parse
** back as a client's), and a server's frame stream into the probe. Every
** peer string, once escaped, must hold no control byte. Linked with the rest
** of dch minus remote.o, and dch.c built with -Dmain=dch_main. */
#include "../../remote.c"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static void
printable(const char *s)
{
	char e[2048];

	dch_remote_esc(e, sizeof e, s);
	for (s = e; *s; s++)
		if ((unsigned char)*s < 0x20 || *s == 0x7f)
			abort();
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
	switch (d[0] % 6)
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
		k = peers_from(t, hv, RM_HOSTS);
		for (i = 0; i < k; i++)
		{
			label_ok(hv[i].label, DCH_RHOST_MAX, "-");
			if (!ip_cgnat4(hv[i].addr) || hv[i].port != RM_PORT)
				abort();
		}
		break;
	case 2:		/* 0x18 */
		k = hello_major(t, proto, sizeof proto);
		if (k < -1 || strlen(proto) >= sizeof proto)
			abort();
		break;
	case 3:		/* 0x11, and the cache */
	{
		struct rlist l = {0};

		(void)rows_doc(t, "h", 1, &l);
		(void)rows_doc(t, NULL, 0, &l);
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
			printable(r->host);
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
	case 5:		/* a server's frames into a probe */
	{
		struct probe *p = calloc(1, sizeof *p);
		struct rf_frame f;
		size_t off = 0, used;
		int r;

		if (!p)
			break;
		p->fd = -1;
		p->st = PR_WAIT;
		p->rs.from_server = 1;
		while (p->st == PR_WAIT &&
		       (r = rf_parse(&p->rs, d + 1 + off, n - 1 - off,
		                     (size_t)-1, &f, &used)) != RF_NEED)
		{
			off += used;
			if (r == RF_CLOSE)
				break;
			if (r == RF_FRAME)
				pr_frame(p, &f);
		}
		printable(p->why);
		if (p->st == PR_OK && !p->list)
			abort();
		probes_free(p, 1);
		break;
	}
	}
	free(t);
	return 0;
}
