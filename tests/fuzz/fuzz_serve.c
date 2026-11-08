/* libFuzzer target: dch serve's pure parts (#073). The input is a client's
** TCP byte stream. rf_parse takes it whole, then again in pieces whose sizes
** come from the input, through a buffer the size of the handler's: both
** passes must see the same frames and the same streamed bytes, and a full
** buffer must never stall. The same bytes as text go through rows_wire
** (its output must parse as JSON), the whois/status readers and the peer
** gate, the pull-url allowlist and the name check. Linked with the rest of
** dch minus serve.o, and dch.c built with -Dmain=dch_main. */
#include "../../serve.c"

int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

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

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct sb a = {0}, b = {0}, rows = {0};
	struct json_parser jp;
	struct gate g;
	char *text, repo[128], login[64];
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
	(void)whois_user(text, &uid, login, sizeof login, &tagged);
	(void)status_self(text, &uid, &tagged);
	memset(&g, 0, sizeof g);
	g.self_uid = n ? d[0] : -1;
	g.allow[g.nallow++] = "7";
	g.allow[g.nallow++] = "me@example.com";
	g.whois_optional = n > 1 && d[1] & 1;
	if (serve_gate(&g, 0x64400001u, text) == G_RANGE ||
	    serve_gate(&g, 0x0a000001u, text) != G_RANGE)
		abort();
	if (gh_pull(text, repo, sizeof repo, &num) == 0 &&
	    (num < 0 || num > 999999999 || strlen(repo) > 100))
		abort();
	if (name_ok(text) && (text[0] == '-' || strchr(text, '/')))
		abort();
	cut_scalars(text, 3);
	free(text);
	return 0;
}
