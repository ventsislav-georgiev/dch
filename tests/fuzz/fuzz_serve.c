/* libFuzzer target: dch serve's pure parts (#073). The input is a client's
** TCP byte stream. rf_parse takes it whole, then again in pieces whose sizes
** come from the input, through a buffer the size of the handler's: both
** passes must see the same frames and the same streamed bytes, and a full
** buffer must never stall. The same bytes as text go through rows_wire
** (its output must parse as JSON), the whois/status readers and the peer
** gate, the pull-url allowlist, the name check and an attach request's
** JSON (size and #101's scrollback); #114's ctl allowlist, over a 0x30's
** JSON (ctl_request) and over the input's lines as an argv (ctl_check):
** what passes starts with an allowlisted verb, its names pass name_ok and
** no forbidden word sits outside free text. #118: the input's frames as one
** connection's sequence through ctl_action (0x30, 0x34, and a streamed 0x33
** or 0x35 standing for the running ctl's exit or timeout): each 0x30 that
** runs gets exactly one 0x33, busy changes nothing, a 0x34 cancels only a
** running ctl not yet cut; each cut's grace (ctl_grace, #124/#126) is the
** long one only for a CTL_LONG verb cut by a 0x34 or the close, never
** while the server stops. LOW-XN1: an accepted --xray's --search or --ref
** text is the one ctl_secret moves to the child's stdin, and it comes back
** whole through a pipe and ctl_text_read; the input's own bytes through
** that pipe come back exactly when 256 or fewer with no NUL, else NULL.
** Linked with the rest of dch minus
** serve.o, and dch.c built with -Dmain=dch_main. */
#include "../../serve.c"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

#define NODE_OK "abcdefghijklmnopqrstuvwxyz" \
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."

/* Every event of a parse, as bytes: two parses of one stream must match. */
static struct sb ev;

static void
note(int r, const struct rf_frame *f)
{
	unsigned char h[6];

	if (r == RF_FRAME || (r == RF_CHUNK && f->data && !f->len))
	{
		h[0] = r == RF_FRAME ? 'F' : 'S';
		h[1] = f->type;
		h[2] = (unsigned char)(f->len >> 24);
		h[3] = (unsigned char)(f->len >> 16);
		h[4] = (unsigned char)(f->len >> 8);
		h[5] = (unsigned char)f->len;
		sb_add(&ev, h, sizeof h);
	}
	if (f->len)
		sb_add(&ev, f->data, f->len);
	if (r == RF_CHUNK && f->end)
		sb_add(&ev, "E", 1);
	if (r == RF_CLOSE)
		sb_add(&ev, "X", 1);
}

/* The whole stream at once: its event log in *out. */
static void
parse_whole(const uint8_t *d, size_t n, struct sb *out)
{
	struct rf_state st = {0, 0};
	struct rf_frame f;
	size_t off = 0, used;
	int r;

	ev.n = 0;
	for (;;)
	{
		memset(&f, 0, sizeof f);
		r = rf_parse(&st, d + off, n - off, (size_t)-1, &f, &used);
		if (used > n - off || (r == RF_NEED && used))
			abort();
		if (r == RF_NEED)
			break;
		note(r, &f);
		if (r == RF_CLOSE)
			break;
		if (!used)
			abort();	/* no progress on a frame or chunk */
		off += used;
	}
	*out = ev;
	memset(&ev, 0, sizeof ev);
}

/* In pieces, through a handler-sized buffer, 0x10 limited to `room`. */
static void
parse_pieces(const uint8_t *d, size_t n, size_t room, struct sb *out)
{
	static unsigned char buf[RF_HDR + RF_CTRL_MAX];
	struct rf_state st = {0, 0};
	struct rf_frame f;
	size_t in = 0, len = 0, used, step = 1;
	int r = RF_NEED;

	ev.n = 0;
	while (r != RF_CLOSE)
	{
		size_t off = 0, k = n - in;

		if (k > sizeof buf - len)
			k = sizeof buf - len;
		if (k > step)
			k = step;
		memcpy(buf + len, d + in, k);
		len += k;
		in += k;
		step = step * 3 + 1;	/* 1, 4, 13, ... then wraps */
		if (step > 70000)
			step = in % 7 + 1;
		for (;;)
		{
			memset(&f, 0, sizeof f);
			r = rf_parse(&st, buf + off, len - off, room, &f, &used);
			if (r == RF_NEED || r == RF_CLOSE)
				break;
			note(r, &f);
			off += used;
		}
		if (r == RF_CLOSE)
			note(r, &f);
		memmove(buf, buf + off, len - off);
		len -= off;
		if (r == RF_NEED && len == sizeof buf)
			abort();	/* a full buffer that takes nothing stalls */
		if (in == n && !k)
			break;
	}
	*out = ev;
	memset(&ev, 0, sizeof ev);
}

/* An --xray ctl_check let through (#157, spec #832 4.4): the name right
** after the verb; 3.1's flags only, each once, --json among them, at most
** one of --follow, --ref, --search (#297), --chapter, --tasks (#319),
** --clear; --scope,
** --full, --budget-ms only with --ref; each value by its rule; no timeout
** and no cap for --follow alone. */
static void
xray_inv(int ac, char **av, const unsigned char *kind, int fl)
{
	static const char *const opt[] = { "--json", "--follow", "--clear",
	                                   "--full", "--ref", "--chapter",
	                                   "--scope", "--budget-ms", "--search",
	                                   "--tasks" };
	unsigned seen = 0, k, one;
	int i;

	if (kind[1] != CK_NAME)
		abort();
	for (i = 2; i < ac; i++)
	{
		for (k = 0; k < 10 && strcmp(av[i], opt[k]); k++)
			;
		if (k == 10 || kind[i] != CK_FLAG || seen & 1u << k)
			abort();
		seen |= 1u << k;
		if (k < 4 || k == 9)
			continue;
		if (++i >= ac || kind[i] != (k == 6 || k == 7 ? CK_VALUE : CK_TEXT) ||
		    (k == 5 && !xray_anchor_ok(av[i])) ||
		    (k == 8 && !xray_search_ok(av[i])) ||
		    (k == 6 && !xray_scope_ok(av[i])) ||
		    (k == 7 && xray_budget(av[i]) < 50))
			abort();
	}
	one = seen & (2 | 4 | 16 | 32 | 256 | 512);
	if (!(seen & 1) || one & (one - 1) || (seen & (8 | 64 | 128) &&
	                                       !(seen & 16)) ||
	    fl != (seen & 2 ? CTL_FOREVER | CTL_NOCAP : 0))
		abort();
}

/* d[0..n) (n <= 512: a pipe takes it whole) through a pipe and
** ctl_text_read, as a ctl child reads its --search or --ref text. */
static void
text_rt(const void *d, size_t n)
{
	char b[257], *r;
	int p[2];

	if (pipe(p) < 0)
		abort();
	if (write(p[1], d, n) != (ssize_t)n)
		abort();
	close(p[1]);
	r = ctl_text_read(p[0], b);
	close(p[0]);
	if (!r != (n > 256 || memchr(d, '\0', n) != NULL) ||
	    (r && (strlen(r) != n || memcmp(r, d, n))))
		abort();
}

/* LOW-XN1: ctl_secret names the text of an --xray's --search or --ref
** flag, the only text it names, and that text survives the pipe. */
static void
secret_inv(int ac, char **av, const unsigned char *kind)
{
	int s = ctl_secret(ac, av), i;

	for (i = 1; i + 1 < ac; i++)
		if (kind[i] == CK_FLAG && !strcmp(av[0], "--xray") &&
		    (!strcmp(av[i], "--search") || !strcmp(av[i], "--ref")) &&
		    s != i + 1)
			abort();
	if (s >= 0 && (s < 3 || s >= ac || kind[s] != CK_TEXT ||
	               (strcmp(av[s - 1], "--search") &&
	                strcmp(av[s - 1], "--ref")) ||
	               strlen(av[s]) > 256))
		abort();
	if (s >= 0)
		text_rt(av[s], strlen(av[s]));
}

/* What ctl_check let through (fl >= 0) is on the allowlist. */
static void
ctl_inv(int ac, char **av, const unsigned char *kind, int fl)
{
	int i;

	if (fl < 0)
		return;
	if (ac < 2 || ac > CTL_ARGV_MAX ||
	    (!ctl_known(av[0]) && strcmp(av[0], "--xray")) ||
	    kind[0] != CK_VERB ||
	    (fl & ~(CTL_CREATE | CTL_LONG | CTL_FOREVER | CTL_NOCAP)) ||
	    (fl & CTL_NOCAP && strcmp(av[0], "--xray")))
		abort();
	if (!strcmp(av[0], "--xray"))
		xray_inv(ac, av, kind, fl);
	/* the verbs that wait on an agent get the long timeout (#122: --run) */
	if (!(fl & CTL_LONG) &&
	    (!strcmp(av[0], "--say") || !strcmp(av[0], "--stop") ||
	     !strcmp(av[0], "--revive") || !strcmp(av[0], "--run")))
		abort();
	for (i = 1; i < ac; i++)
	{
		const char *a = av[i];

		if (kind[i] < CK_FLAG || kind[i] > CK_TEXT ||
		    (kind[i] == CK_NAME && !name_ok(a)))
			abort();
		if (kind[i] != CK_TEXT &&
		    (!strcmp(a, "--config") || !strcmp(a, "serve") ||
		     !strcmp(a, "-h") || !strcmp(a, "--help") || !strcmp(a, "-f") ||
		     (!strcmp(a, "--all") && strcmp(av[0], "--forget"))))
			abort();
	}
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct sb a = {0}, b = {0}, rows = {0};
	struct json_parser jp;
	struct gate g;
	char *text, repo[128], login[64];
	char node[NODE_ID_MAX];
	long long num, uid;
	int tagged;

	parse_whole(d, n, &a);
	parse_pieces(d, n, n ? (size_t)d[0] % 9 + 1 : 1, &b);
	if (a.bad || b.bad || a.n != b.n || (a.n && memcmp(a.p, b.p, a.n)))
		abort();
	free(a.p);
	free(b.p);

	if (!(text = malloc(n + 1)))
		return 0;
	memcpy(text, d, n);
	text[n] = '\0';
	if (rows_wire(text, &rows) == 0)
	{
		if (dch_json_parse(&jp, rows.p) < 0 ||
		    jp.tok[0].kind != JSON_ARRAY)
			abort();
		dch_json_free(&jp);
	}
	free(rows.p);
	/* a node id, when there is one, is plain (ASan checks it fits) */
	if (whois_user(text, &uid, node, login, sizeof login, &tagged) == 0 &&
	    node[strspn(node, NODE_OK)])
		abort();
	if (status_self(text, &uid, node, &tagged) == 0 &&
	    node[strspn(node, NODE_OK)])
		abort();
	memset(&g, 0, sizeof g);
	g.self_uid = n ? d[0] : -1;
	if (n > 2 && d[2] & 1)
		snprintf(g.self_node, sizeof g.self_node, "n%uCNTRL",
		         (unsigned)d[2]);
	g.allow[g.nallow++] = "7";
	g.allow[g.nallow++] = "me@example.com";
	g.whois_optional = n > 1 && d[1] & 1;
	if (serve_gate(&g, 0x64400001u, 0, text, 0) == G_RANGE ||
	    serve_gate(&g, 0x0a000001u, 0, text, 0) != G_RANGE ||
	    serve_gate(&g, 0x64400001u, 1, text, 0) != G_SELF ||
	    serve_gate(&g, 0x64400001u, 0, NULL, 1) != G_NOTNODE)
		abort();
	{
		/* #094: every refusal of a tailnet peer has a code and a
		** message, an accept none; the owner/allow-list split follows
		** the host's taggedness */
		const char *m = NULL, *code;
		int v = serve_gate(&g, 0x64400001u, 0, text, 0);

		code = gate_code(v, &m);
		if ((v > G_RANGE) != (code != NULL) || (code && !m) ||
		    gate_code(G_RANGE, &m) ||
		    (v == G_OWNER && g.self_uid < 0) ||
		    (v == G_NOTALLOWED && g.self_uid >= 0) ||
		    /* #095: busy is the parent's limits, never a verdict */
		    v == G_BUSY || !gate_code(G_BUSY, &m) ||
		    strcmp(gate_code(G_BUSY, &m), "busy"))
			abort();
	}
	{
		/* logsafe leaves no byte a terminal acts on */
		char out[512];
		const unsigned char *q;
		unsigned cp;
		int k;

		logsafe(out, sizeof out, text);
		for (q = (const unsigned char *)out; *q; q += k)
			if (!(k = utf8_at(q, &cp)) || cp_ctl(cp))
				abort();
		/* a name that passes name_ok is logged as it is */
		if (name_ok(text) && strcmp(out, text))
			abort();
	}
	if (gh_pull(text, repo, sizeof repo, &num) == 0 &&
	    (num < 0 || num > 999999999 || strlen(repo) > 100))
		abort();
	if (name_ok(text) && (text[0] == '-' || strchr(text, '/')))
		abort();
	if (n <= RF_CTRL_MAX && rq_json(&jp, rq_text, d, n) == 0)
	{
		/* an attach request (0x01) as on_attach reads it: the size and
		** #101's optional scrollback always land in range */
		int cols, rows, sb = rq_scrollback(&jp);

		rq_size(&jp, &cols, &rows);
		dch_json_free(&jp);
		if (cols < 1 || cols > 1000 || rows < 1 || rows > 1000 ||
		    sb < 0 || sb > DCH_SCROLLBACK_MAX)
			abort();
	}
	if (n <= RF_CTRL_MAX)
	{
		/* #114: a 0x30 as on_ctl reads it; ctl_check agrees on argv */
		static unsigned char kind[CTL_ARGV_MAX];
		char **av, cwd[PATH_MAX], why[200];
		int ac, fl = ctl_request(d, n, &av, &ac, cwd, why, sizeof why);

		if (fl < 0 ? av != NULL || !why[0]
		           : !av || (cwd[0] && cwd[0] != '/') ||
		             ctl_check(ac, av, kind, why, sizeof why) != fl)
			abort();
		if (fl >= 0)
			ctl_inv(ac, av, kind, fl);
		/* #157: an --xray --ref that serve runs has 3.1's text */
		for (int i = 2; fl >= 0 && !strcmp(av[0], "--xray") && i + 1 < ac;
		     i++)
		{
			struct xr_span sp;

			if (kind[i] == CK_FLAG && !strcmp(av[i], "--ref") &&
			    (!xr_ref_ok(av[i + 1]) ||
			     !xr_ref_parse(av[i + 1], NULL, &sp)))
				abort();
		}
		if (fl >= 0)
			secret_inv(ac, av, kind);
		free(av);
	}
	text_rt(d, n < 512 ? n : 512);
	{
		/* the input's lines as an argv */
		unsigned char kind[64];
		char *v[64], *c = strdup(text), *q = c, why[200];
		int ac = 0, fl;

		while (q && ac < 64)
		{
			v[ac++] = q;
			if ((q = strchr(q, '\n')))
				*q++ = '\0';
		}
		fl = c ? ctl_check(ac, v, kind, why, sizeof why) : -1;
		if (c && fl < 0 && !why[0])
			abort();
		if (c)
			ctl_inv(ac, v, kind, fl);
		free(c);
	}
	{
		/* #118: the frames as on_frame and ctl_tick take them */
		struct rf_state st = {0, 0};
		struct rf_frame f;
		size_t off = 0, used;
		pid_t ctl = 0;		/* a stand-in: never signalled */
		const char *why = NULL;
		int r, a, ac, runs = 0, ends = 0, lg = n && d[0] & 1;
		char cwd[PATH_MAX], w[200], **av;

		while ((r = rf_parse(&st, d + off, n - off, (size_t)-1, &f,
		                     &used)) != RF_NEED && r != RF_CLOSE)
		{
			off += used;
			if (r == RF_CHUNK && f.end && ctl > 0 &&
			    (f.type == 0x33 || (f.type == 0x35 && !why)))
			{
				if (f.type == 0x35)
					why = "timeout";	/* ctl_cut */
				else
				{
					ctl = -1;	/* reaped: its one 0x33 */
					ends++;
				}
				continue;
			}
			if (r != RF_FRAME || (f.type != 0x30 && f.type != 0x34))
				continue;
			a = ctl_action(f.type, ctl, why);
			if ((f.type == 0x30) != (a == CA_RUN || a == CA_BUSY) ||
			    (a == CA_RUN && ctl > 0) ||
			    ((a == CA_BUSY || a == CA_CANCEL) && ctl <= 0) ||
			    (a == CA_CANCEL && why) ||
			    (f.type == 0x34 && ctl > 0 && !why && a != CA_CANCEL))
				abort();
			if (a == CA_CANCEL)
				why = "cancel";
			else if (a == CA_RUN)
			{
				if (ctl_request(f.data, f.len, &av, &ac, cwd, w,
				                sizeof w) < 0 || !av)
					break;	/* refused: the 0x13, the FIN */
				free(av);
				ctl = ++runs;
				why = NULL;
			}
			if (why && (ctl_grace(lg, why, 1) != CTL_GRACE_MS ||
			            ctl_grace(lg, why, 0) != (lg && (!strcmp(why,
			            "cancel") || !strcmp(why, "closed"))
			            ? CTL_LONG_MS : CTL_GRACE_MS) ||
			            ctl_grace(lg, "closed", 0) != (lg ? CTL_LONG_MS
			            : CTL_GRACE_MS) ||
			            ctl_grace(lg, "cap", 0) != CTL_GRACE_MS))
				abort();
		}
		if (runs != ends + (ctl > 0))
			abort();
	}
	cut_scalars(text, 3);
	free(text);
	return 0;
}
