/*
** The session picker (#075): Agent Island in a terminal. picker.h says
** what is here and what the host (dch.c, win/) brings.
**
** `dch -l`, `dch -k`/`-d` without a name, and the double-tap switch. Tabs:
** Sessions; Sidekicks (n), the live sessions whose name, alias or harness
** title matches the config's sidekick_pattern (default "-qa"), shown only when there are
** some; History (n), the ended records, one row per name (its newest
** record, pinned when any of the name's is), pinned first, by name, at
** most 50; and other hosts' as host:name (#115), at most 50 per host,
** never pinned here, which only resume (there). "/"
** filters every tab, as vim's search (#096). Rows carry
** --ls-json's data (state, agents, meta.prs) and --history's, reloaded about
** once a second. Enter hands the session back to the caller; every other
** action forks this same dch with its CLI verb, the output into an unlinked
** file (never over the screen, and no pipe to break when the picker quits
** first), and its last line is the result. Drawn on the alternate screen
** with autowrap off, each line overwritten in place and cleared to its end,
** and only the lines that changed: no frame clears the screen and no row
** wraps. On Windows (#090) only the Sessions tab, of remote rows, which
** only attach: no actions, no History, no settings view.
*/
#ifdef _WIN32
#include "win/win.h"
#else
#include "dtach.h"
#endif
#include "picker.h"
#include "rframe.h"

#include <ctype.h>

volatile sig_atomic_t pk_winch;
static int pk_sortname;

/* Code point at p (UTF-8), its byte length in *len. A broken sequence (a
** stray, overlong, surrogate, truncated or past U+10FFFF one) is its first
** byte alone, a code point >= 0x80 of length 1, which pk_txtto draws as '?':
** a remote host's screen is raw bytes (QA 1.57.0 LOW-R1). */
unsigned
ap_cp(const char *p, int *len)
{
	unsigned c;

	if (!(*len = utf8_at((const unsigned char *)p, &c)))
	{
		*len = 1;
		c = (unsigned char)*p;
	}
	return c;
}

/* Terminal cells of a code point: 2 for East Asian wide and emoji.
** ponytail: a hand list, as Prosper's; a miss only fails the title rule. */
int
ap_cells(unsigned u)
{
	return (u >= 0x1100 && u <= 0x115F) || (u >= 0x2E80 && u <= 0x303E) ||
	       (u >= 0x3041 && u <= 0x33FF) || (u >= 0x3400 && u <= 0x4DBF) ||
	       (u >= 0x4E00 && u <= 0x9FFF) || (u >= 0xA000 && u <= 0xA4CF) ||
	       (u >= 0xA960 && u <= 0xA97F) || (u >= 0xAC00 && u <= 0xD7A3) ||
	       (u >= 0xF900 && u <= 0xFAFF) || (u >= 0xFE30 && u <= 0xFE6F) ||
	       (u >= 0xFF00 && u <= 0xFF60) || (u >= 0xFFE0 && u <= 0xFFE6) ||
	       (u >= 0x1F200 && u <= 0x1FAFF) || (u >= 0x20000 && u <= 0x3FFFD)
	       ? 2 : 1;
}

/* Case-insensitive, digit runs by value: "a2" before "a10". */
static int
pk_natcmp(const char *a, const char *b)
{
	while (*a && *b)
	{
		int x = tolower((unsigned char)*a), y = tolower((unsigned char)*b);

		if (isdigit(x) && isdigit(y))
		{
			size_t la = 0, lb = 0;
			int c;

			while (a[0] == '0' && isdigit((unsigned char)a[1]))
				a++;
			while (b[0] == '0' && isdigit((unsigned char)b[1]))
				b++;
			while (isdigit((unsigned char)a[la]))
				la++;
			while (isdigit((unsigned char)b[lb]))
				lb++;
			if (la != lb)
				return la < lb ? -1 : 1;
			if ((c = strncmp(a, b, la)))
				return c;
			a += la;
			b += lb;
			continue;
		}
		if (x != y)
			return x - y;
		a++;
		b++;
	}
	return (unsigned char)*a - (unsigned char)*b;
}

static int
pk_rank(const char *s)
{
	return !strcmp(s, "blocked") ? 0 : !strcmp(s, "working") ? 1
	       : !strcmp(s, "done") ? 2 : 3;
}

/* By activity: blocked, working, done, idle, newest output first; or by
** the shown title, numeric-aware. Ties on the name. */
static int
pk_cmp(const void *x, const void *y)
{
	const struct pkrow *a = x, *b = y;
	int c;

	if (!pk_sortname)
	{
		if ((c = pk_rank(a->state) - pk_rank(b->state)))
			return c;
		if (a->when != b->when)
			return a->when > b->when ? -1 : 1;
	}
	else if ((c = pk_natcmp(a->title, b->title)))
		return c;
	return strcmp(a->name, b->name);
}

/* History (#144): pinned first, then by the shown title, numeric-aware;
** ties the newest first, then the dch name. */
static int
pk_hcmp(const void *x, const void *y)
{
	const struct pkrow *a = x, *b = y;
	int c;

	if (a->pinned != b->pinned)
		return a->pinned ? -1 : 1;
	if ((c = pk_natcmp(a->title, b->title)))
		return c;
	if (a->when != b->when)
		return a->when > b->when ? -1 : 1;
	return strcmp(a->sname, b->sname);
}

int
pk_find(const struct pk *p, int t, const char *name)
{
	int k;

	for (k = 0; k < p->n[t]; k++)
		if (strcmp(p->r[t][k].name, name) == 0)
			return k;
	return -1;
}

/* ---- the "/" filter (#096): a tab shows only the rows whose title, dch
** name or directory holds the text, ASCII case folded. sel stays a row's
** index (the hosts' pk_do read it) and moves only among the rows kept;
** with none kept it is n (no row: the row actions do nothing). ---- */

static int
pk_lc(int c)
{
	return c >= 'A' && c <= 'Z' ? c + 32 : c;
}

/* f in s, ASCII case folded; any other byte (UTF-8) as it is. */
static int
pk_has(const char *s, const char *f)
{
	size_t k;

	for (;; s++)
	{
		for (k = 0; f[k] && pk_lc((unsigned char)s[k]) ==
		     pk_lc((unsigned char)f[k]); k++)
			;
		if (!f[k])
			return 1;
		if (!*s)
			return 0;
	}
}

/* Row k of tab t kept by the filter? History's dch name is its sname. */
static int
pk_vis(const struct pk *p, int t, int k)
{
	const struct pkrow *w = &p->r[t][k];

	return !p->filt[0] || pk_has(w->title, p->filt) ||
	       pk_has(t == 2 ? w->sname : w->name, p->filt) ||
	       pk_has(w->cwd, p->filt);
}

/* The first kept row from k on; n[t] when none. */
static int
pk_next(const struct pk *p, int t, int k)
{
	while (k < p->n[t] && !pk_vis(p, t, k))
		k++;
	return k;
}

/* The kept rows before row k: its place in the list drawn. pk_pos(p, t,
** p->n[t]) is the tab's count. */
static int
pk_pos(const struct pk *p, int t, int k)
{
	int j, n = 0;

	for (j = 0; j < k && j < p->n[t]; j++)
		n += pk_vis(p, t, j);
	return n;
}

/* The kept row at place pos; n[t] when none. */
static int
pk_nth(const struct pk *p, int t, int pos)
{
	int k = pk_next(p, t, 0);

	while (pos-- > 0 && k < p->n[t])
		k = pk_next(p, t, k + 1);
	return k;
}

/* The selection on a kept row: its own, else the next kept one, else the
** one before; n[t] when none is kept. */
static void
pk_fix(struct pk *p, int t)
{
	int k = p->sel[t] < p->n[t] ? p->sel[t] : p->n[t] - 1, j;

	if (k < 0)
		k = 0;
	if ((j = pk_next(p, t, k)) == p->n[t])
		for (j = k - 1; j >= 0 && !pk_vis(p, t, j); j--)
			;
	p->sel[t] = j < 0 ? p->n[t] : j;
}

/* Keep each tab's selection on the same row (by name, and remote or not:
** a local srv:gone is not host srv's gone, #167) when rows come, go or
** move; a row that went, or that the filter no longer keeps, leaves it on
** the nearest kept one. */
static void
pk_reselect(struct pk *p, char sel[3][600])
{
	int t, k, r;

	for (t = 0; t < 3; t++)
	{
		r = sel[t][0] == '\1';
		for (k = 0; sel[t][r] && k < p->n[t] &&
		     (!p->r[t][k].remote != !r ||
		      strcmp(p->r[t][k].name, sel[t] + r)); k++)
			;
		if (sel[t][r] && k < p->n[t])
			p->sel[t] = k;
		pk_fix(p, t);
	}
}

/* Each tab's selected row for pk_reselect: its name, after a \1 when it
** is a remote row's. */
void
pk_selnames(const struct pk *p, char sel[3][600])
{
	int t;

	for (t = 0; t < 3; t++)
		snprintf(sel[t], 600, "%s%.598s", p->sel[t] < p->n[t] &&
		         p->r[t][p->sel[t]].remote ? "\1" : "", p->sel[t] < p->n[t]
		         ? p->r[t][p->sel[t]].name : "");
}

/* Sort, then select the rows named in sel (pk_selnames before the rows
** changed). */
void
pk_sort(struct pk *p, char sel[3][600])
{
	int t;

	pk_sortname = p->byname;
	for (t = 0; t < 2; t++)
		if (p->n[t] > 1)
			qsort(p->r[t], (size_t)p->n[t], sizeof *p->r[t], pk_cmp);
	if (p->n[2] > 1)
		qsort(p->r[2], (size_t)p->n[2], sizeof *p->r[2], pk_hcmp);
	pk_reselect(p, sel);
}

static int
pk_shown(const struct pk *p, int t)
{
	return t == 0 || p->n[t] > 0;
}

/* The sort the config asks for. */
int
pk_cfbyname(void)
{
	char v[16];

	return dch_config_get("picker_sort", v, sizeof v) && !strcmp(v, "name");
}

/* ---- drawing: one frame into pk_fb, written when it changed ---- */
/*
** The look (#086), after Apple's Human Interface Guidelines, in a terminal:
** hierarchy by weight rather than colour, the titles in the terminal's own
** foreground and all that is secondary faint; one accent, Claude's clay
** orange, for the active tab, the keys and the working state; red, green
** and faint for the other states, and nothing else in colour. Every list has
** its column headers, every column one alignment (ages and counts right,
** words left), and nothing wraps. The selection is a quiet tinted bar the
** width of the screen with an accent edge, every cell on it keeping its own
** colour. Four color levels: none (NO_COLOR, TERM=dumb: the layout, a '>'
** and brackets carry it), the basic eight (reverse for the bar, which would
** turn a colour into a block, so the bar's cells drop theirs), the 256 cube
** and truecolor (COLORTERM). The two backgrounds, the bar and the key chips,
** have a pair for light terminals, picked by COLORFGBG.
*/

#define PK_LINES 1024
static char pk_fb[1 << 18], pk_prev[1 << 18], pk_out[1 << 18];
static size_t pk_fn;
size_t pk_pn;
static size_t pk_lo[PK_LINES + 1], pk_plo[PK_LINES + 1];	/* line starts */
static int pk_nl, pk_pnl;
int pk_col, pk_w, pk_h, pk_utf;
static int pk_y, pk_off, pk_color, pk_light;
const char *pk_bg = "";	/* the background under what is drawn */
static char pk_cur[112];	/* the SGR the frame last wrote */

/* [color level][light terminal][entry] */
static const char *const pk_pal[4][2][PK_NPAL] = {
	{ { "", "", "", "", "", "", "" }, { "", "", "", "", "", "", "" } },
	{ { "33", "1;33", "1;31", "32", "1;33", "7", "7" },
	  { "33", "1;33", "1;31", "32", "1;33", "7", "7" } },
	{ { "38;5;173", "1;38;5;173", "1;38;5;203", "38;5;71", "1;38;5;179",
	    "48;5;236", "48;5;238" },
	  { "38;5;166", "1;38;5;166", "1;38;5;160", "38;5;28", "1;38;5;130",
	    "48;5;254", "48;5;252" } },
	{ { "38;2;217;119;87", "1;38;2;217;119;87", "1;38;2;240;96;96",
	    "38;2;96;184;120", "1;38;2;229;181;103", "48;2;38;44;56",
	    "48;2;58;64;78" },
	  { "38;2;190;90;56", "1;38;2;190;90;56", "1;38;2;200;40;40",
	    "38;2;30;130;60", "1;38;2;160;100;0", "48;2;226;232;242",
	    "48;2;212;218;228" } },
};
#define PK_P(i) pk_pal[pk_color][pk_light][i]

/* A palette entry at this frame's colour level (the X-ray view's, #175). */
const char *
pk_p(int i)
{
	return PK_P(i);
}

void
pk_rawn(const char *s, size_t n)
{
	if (!pk_off && pk_fn + n < sizeof pk_fb)
	{
		memcpy(pk_fb + pk_fn, s, n);
		pk_fn += n;
	}
}

void
pk_raw(const char *s)
{
	pk_rawn(s, strlen(s));
}

/* SGR parameters ("0;1;33"); nothing without color, nor what the frame
** already set. */
static void
pk_sgr(const char *s)
{
	char b[128];

	if (!pk_color || pk_off || !strcmp(s, pk_cur))
		return;
	/* a cut escape would leave the terminal mid-sequence */
	if ((size_t)snprintf(b, sizeof b, "\033[%sm", s) < sizeof b)
	{
		pk_raw(b);
		snprintf(pk_cur, sizeof pk_cur, "%s", s);
	}
}

/* Draw on in a ("2" faint, a palette entry, "" the plain foreground) over
** pk_bg. On the basic bar, reverse video, a colour would read as a block:
** there only bold stays. */
void
pk_set(const char *a)
{
	char b[112];

	if (pk_color == 1 && *pk_bg)
		a = a[0] == '1' && (!a[1] || a[1] == ';') ? "1" : "";
	snprintf(b, sizeof b, "0%s%s%s%s", *a ? ";" : "", a, *pk_bg ? ";" : "",
	         pk_bg);
	pk_sgr(b);
}

/* A bidi control (U+061C, U+200E, U+200F, U+2028-U+202E, U+2066-U+2069,
** the set dch's JSON quoting escapes): drawn as
** '?', as a control is (K10.6, #175: titles are hostile). */
static int
pk_bidi(unsigned u)
{
	return u == 0x061C || u == 0x200E || u == 0x200F ||
	       (u >= 0x2028 && u <= 0x202E) || (u >= 0x2066 && u <= 0x2069);
}

/* Cells of code point u: two for the wide (ap_cells), none for a combining
** mark, a joiner, an invisible operator, a BOM or a variation selector; one
** for a bidi control's '?'. */
static int
pk_cw(unsigned u)
{
	return pk_bidi(u) ? 1 : (u >= 0x300 && u <= 0x36F) ||
	       (u >= 0x200B && u <= 0x200F) || (u >= 0x2060 && u <= 0x2064) ||
	       (u >= 0xFE00 && u <= 0xFE0F) || u == 0xFEFF
	       ? 0 : ap_cells(u);
}

/* Characters (UTF-8), each as wide as it draws, up to column lim and the
** edge. A control (C0, DEL or C1), a bidi control or a broken sequence
** shows as '?'. */
void
pk_txtto(const char *s, int lim)
{
	unsigned u;
	int l, w;

	if (lim > pk_w)
		lim = pk_w;
	for (; *s; s += l)
	{
		unsigned char c = (unsigned char)*s;

		u = ap_cp(s, &l);
		w = pk_cw(u);
		if (pk_col + w > lim)
			break;
		if (u < 0x20 || (u >= 0x7f && u < 0xa0) || (c >= 0x80 && l == 1) ||
		    pk_bidi(u))
			pk_rawn("?", 1);
		else
			pk_rawn(s, (size_t)l);
		pk_col += w;
	}
}

void
pk_txt(const char *s)
{
	pk_txtto(s, pk_w);
}

/* Display width: the cells s takes. */
int
pk_cps(const char *s)
{
	int n = 0, l;

	for (; *s; s += l)
		n += pk_cw(ap_cp(s, &l));
	return n;
}

/* Blanks up to column end. */
void
pk_pad(int end)
{
	while (pk_col < end && pk_col < pk_w)
	{
		pk_rawn(" ", 1);
		pk_col++;
	}
}

static const char *
pk_ell(void)
{
	return pk_utf ? "\xe2\x80\xa6" : "~";
}

/* s in exactly w columns: cut with an ellipsis, or padded. */
void
pk_fit(const char *s, int w)
{
	int end = pk_col + w;

	if (w <= 0)
		return;
	if (pk_cps(s) > w)
	{
		pk_txtto(s, end - 1);
		pk_txt(pk_ell());
	}
	else
		pk_txt(s);
	pk_pad(end);
}

/* s right-aligned in w columns. */
static void
pk_rfit(const char *s, int w)
{
	int n = pk_cps(s);

	if (n < w)
		pk_pad(pk_col + w - n);
	pk_fit(s, n < w ? n : w);
}

/* s in w columns, cut from the left: a path's tail says the most. */
static void
pk_tail(const char *s, int w)
{
	int n = pk_cps(s), l;

	if (n > w && w > 1)
	{
		for (; *s && n > w - 1; s += l)
			n -= pk_cw(ap_cp(s, &l));
		pk_txt(pk_ell());
		w--;
	}
	pk_fit(s, w);
}

/* The next screen line; lines past the bottom are dropped. */
void
pk_line(void)
{
	char b[32];

	pk_off = 0;
	if (pk_nl < PK_LINES)
		pk_lo[pk_nl] = pk_fn;
	pk_nl++;
	if (++pk_y > pk_h)
	{
		pk_off = 1;
		return;
	}
	snprintf(b, sizeof b, "\033[%d;1H", pk_y);
	pk_raw(b);
	pk_col = 0;
}

/* The rest of the line cleared; not at the edge, where the cursor sits on
** the last cell drawn and an erase would take it. */
void
pk_eol(void)
{
	pk_bg = "";
	pk_set("");
	if (pk_col < pk_w)
		pk_raw("\033[K");
}

/* A faint rule across the screen: the list above, the controls below. */
void
pk_rule(void)
{
	pk_line();
	pk_set("2");
	while (pk_col < pk_w)
		pk_txt(pk_utf ? "\xe2\x94\x80" : "-");
	pk_eol();
}

/* A key hint's width (struct pkkey: picker.h) */
static int
pk_kw(const struct pkkey *k)
{
	return pk_cps(k->key) + (pk_color ? 2 : 0) + 1 + pk_cps(k->label) +
	       (k->val ? 1 + pk_cps(k->val) : 0);
}

/* The key as a chip, the accent (red for a destructive key) on a tint,
** reverse on the basic eight; its label faint. */
static void
pk_chip(const struct pkkey *k)
{
	if (pk_color)
	{
		pk_bg = PK_P(PK_CHIP);
		pk_set(PK_P(k->warn ? PK_RED : PK_ACC));
		pk_txt(" ");
		pk_txt(k->key);
		pk_txt(" ");
		pk_bg = "";
	}
	else
		pk_txt(k->key);
	pk_set("2");
	pk_txt(" ");
	pk_txt(k->label);
	if (k->val)
	{
		pk_txt(" ");
		pk_set(PK_P(PK_ACC));
		pk_txt(k->val);
	}
}

/* The key hints in at most cap lines: a group whole on one line when it
** fits, " │ " between two groups on a line, two spaces between hints; the
** groups in drop (bits) left out. The lines used, or -1 when a hint did not
** fit: it is never cut, only left for a wider window. draw 0 only counts. */
int
pk_keys(const struct pkkey *it, int n, unsigned drop, int cap, int draw)
{
	int lines = 0, col = 0, g = -1, k, j;

	for (k = 0; k < n; k++)
	{
		int w = pk_kw(&it[k]), need = w, sep;

		if (drop >> it[k].group & 1)
			continue;
		if (it[k].group != g)
			for (j = k + 1; j < n && it[j].group == it[k].group; j++)
				need += 2 + pk_kw(&it[j]);
		if (need > pk_w)
			need = w;
		sep = !col ? 0 : it[k].group != g ? 3 : 2;
		if (!lines || col + sep + need > pk_w)
		{
			if (lines == cap || w > pk_w)
			{
				if (draw && lines)
					pk_eol();
				return -1;
			}
			if (draw && lines)
				pk_eol();
			if (draw)
				pk_line();
			lines++;
			col = sep = 0;
		}
		if (draw && sep == 3)
		{
			pk_set("2");
			pk_txt(pk_utf ? " \xe2\x94\x82 " : " | ");
		}
		else if (draw && sep)
			pk_txt("  ");
		if (draw)
			pk_chip(&it[k]);
		col += sep + w;
		g = it[k].group;
	}
	if (draw && lines)
		pk_eol();
	return lines;
}

static const char *
pk_label(const char *s)
{
	return !strcmp(s, "blocked") ? "Needs input" : !strcmp(s, "working")
	       ? "Working" : !strcmp(s, "done") ? "Done" : "Idle";
}

/* A state's colour: needs input the loudest, red and bold; working the
** accent; done green; idle faint. */
static const char *
pk_hue(const char *s)
{
	return !strcmp(s, "blocked") ? PK_P(PK_RED) : !strcmp(s, "working")
	       ? PK_P(PK_ACC) : !strcmp(s, "done") ? PK_P(PK_GRN) : "2";
}

/* A state's glyph: a shape apiece, so colour is never all it says. */
static const char *
pk_glyph(const char *s)
{
	int b = !strcmp(s, "blocked"), w = !strcmp(s, "working");
	int d = !strcmp(s, "done");

	if (!pk_utf)
		return b ? "!" : w ? "*" : d ? "+" : "-";
	return b ? "\xe2\x97\x8f" : w ? "\xe2\x9c\xb3" : d ? "\xe2\x9c\x93"
	       : "\xe2\x97\x8b";
}

/* An end in the island's neutral words: nothing done on purpose reads as
** a crash. */
static const char *
pk_end(const char *e, char *b, size_t n)
{
	if (!*e || !strcmp(e, "vanished"))
		return "vanished";
	if (!strcmp(e, "killed"))
		return "closed";
	if (!strcmp(e, "exit 0"))
		return "exited";
	if (!strcmp(e, "signal 15") || !strcmp(e, "signal 1") ||
	    !strcmp(e, "signal 2"))
		snprintf(b, n, "ended (%s)", e);
	else if (!strncmp(e, "signal ", 7))
		snprintf(b, n, "crashed (%s)", e);
	else if (!strncmp(e, "exit ", 5))
		snprintf(b, n, "exited (%s)", e + 5);
	else
		return e;
	return b;
}

/* An age, short: now, 22m, 3h, 2d. */
static void
pk_age(long when, char *b, size_t n)
{
	long s = (long)time(NULL) - when;

	if (s < 0)
		s = 0;
	if (when <= 0)
		b[0] = '\0';
	else if (s < 60)
		snprintf(b, n, "now");
	else if (s < 3600)
		snprintf(b, n, "%ldm", s / 60);
	else if (s < 86400)
		snprintf(b, n, "%ldh", s / 3600);
	else
		snprintf(b, n, "%ldd", s / 86400);
}

/* The columns of the rows on screen, their widths from those rows (0: not
** shown); the settings view's values, read once a frame. */
static struct
{
	int name, state, age, agents, merged, last, gap;
	int mw;		/* the widest merged text: its ✓ marks line up */
} pk_c;

/* "✓ n", n right-aligned so the whole text is w columns wide (0: as is). */
static void
pk_mergedtxt(long m, int w, char *b, size_t n)
{
	int pre = pk_utf ? 2 : 0;

	snprintf(b, n, "%s%*ld", pk_utf ? "\xe2\x9c\x93 " : "",
	         w > pre ? w - pre : 0, m);
}

#ifndef _WIN32	/* the settings view: dch.c's config, POSIX only */
static char pk_cfv[NCONF][1024];
static int pk_cfis[NCONF];

/* Key k's value as the settings view shows it: a bool as ● on / ○ off, a
** choice as all of them with the set one in the accent (in brackets
** without colour), a line as itself; then (default) when the file has
** none. Its width; draw 0 only measures. */
static int
pk_cfval(int k, int draw)
{
	const char *sv = pk_cfv[k], *c = config_keys[k].choices;
	char one[128];
	int n = 0, on = !strcmp(sv, "true"), cur;
	size_t l;

#define PK_V(a, s) (n += pk_cps(s), draw ? (pk_set(a), pk_txt(s)) : (void)0)
	if (config_keys[k].is_bool)
	{
		if (pk_utf)
			PK_V(on ? PK_P(PK_ACC) : "2", on ? "\xe2\x97\x8f " :
			     "\xe2\x97\x8b ");
		PK_V("", on ? "on" : "off");
	}
	else if (c)
		for (;;)
		{
			l = strcspn(c, "|");
			snprintf(one, sizeof one, "%.*s", (int)l, c);
			cur = !strcmp(one, sv);
			if (cur && !pk_color)
				PK_V("", "[");
			PK_V(cur ? PK_P(PK_ACCB) : "2", one);
			if (cur && !pk_color)
				PK_V("", "]");
			if (!c[l])
				break;
			PK_V("2", pk_utf && pk_color ? " \xc2\xb7 " : " ");
			c += l + 1;
		}
	else
		PK_V("", *sv ? sv : "\"\"");
	if (!pk_cfis[k])
		PK_V("2", "  (default)");
#undef PK_V
	return n;
}
#endif

/* Lay out the columns for the cnt rows on screen from row k. The title
** takes at most three tenths of a wide screen; as the screen narrows the
** last column goes first, then merged, agents and the age, and the title
** shrinks last. */
static void
pk_cols(const struct pk *p, int k, int cnt)
{
	char b[1400];
	int used, cap, w, hist = !p->settings && p->tab == 2, last = 0;

	memset(&pk_c, 0, sizeof pk_c);
	pk_c.gap = pk_w >= 120 ? 3 : 2;
	pk_c.name = p->settings ? 7 : 4;
	pk_c.state = 5;
	for (; cnt > 0; cnt--, k = p->settings ? k + 1
	     : pk_next(p, p->tab, k + 1))
	{
		const struct pkrow *r;

#ifndef _WIN32
		if (p->settings)
		{
			int set;

			snprintf(pk_cfv[k], sizeof pk_cfv[k], "%.1023s",
			         config_show(k, b, sizeof b, &set));
			pk_cfis[k] = set;
			if ((w = pk_cps(config_keys[k].key)) > pk_c.name)
				pk_c.name = w;
			if ((w = pk_cfval(k, 0)) > pk_c.state)
				pk_c.state = w;
			last = 1;
			continue;
		}
#endif
		r = &p->r[p->tab][k];
		if ((w = pk_cps(r->title)) > pk_c.name)
			pk_c.name = w;
		w = pk_cps(hist ? pk_end(r->state, b, sizeof b) : pk_label(r->state));
		if (w > pk_c.state)
			pk_c.state = w;
		pk_c.age |= r->when > 0;
		if (hist)
		{
			last |= !!r->cwd[0];
			continue;
		}
		pk_c.agents |= r->agents > 0;
		if (r->merged > 0)
		{
			pk_mergedtxt(r->merged, 0, b, sizeof b);
			if ((w = pk_cps(b)) > pk_c.merged)
				pk_c.merged = pk_c.mw = w;
		}
		last |= !!r->prs[0];
	}
	cap = pk_w * 3 / 10;
	if (cap < 16)
		cap = 16;
	if (cap > 40)
		cap = 40;
	if (!p->settings && pk_c.name > cap)
		pk_c.name = cap;
	used = 4 + pk_c.name + pk_c.gap + pk_c.state;
	if (used > pk_w)
	{
		pk_c.name -= used - pk_w;
		if (pk_c.name < 4)
			pk_c.name = 4;
		used = 4 + pk_c.name + pk_c.gap + pk_c.state;
	}
#define PK_COL(f, w) \
	if (pk_c.f && used + pk_c.gap + (w) <= pk_w) \
		used += pk_c.gap + (pk_c.f = (w)); \
	else \
		pk_c.f = 0;
	PK_COL(age, hist ? 4 : 6)
	PK_COL(agents, 6)
	PK_COL(merged, pk_c.merged > 6 ? pk_c.merged : 6)
#undef PK_COL
	if (last && used + pk_c.gap + 8 <= pk_w)
		pk_c.last = pk_w - used - pk_c.gap;
}

static void
pk_gap(void)
{
	pk_pad(pk_col + pk_c.gap);
}

/* The column headers, faint: the same widths and alignments as the rows. */
static void
pk_heads(const struct pk *p)
{
	int hist = !p->settings && p->tab == 2;

	pk_line();
	pk_set("2");
	pk_txt("    ");
	pk_fit(p->settings ? "SETTING" : "NAME", pk_c.name);
	pk_gap();
	pk_fit(p->settings ? "VALUE" : hist ? "ENDED" : "STATE", pk_c.state);
	if (pk_c.age)
	{
		pk_gap();
		pk_rfit(hist ? "WHEN" : "ACTIVE", pk_c.age);
	}
	if (pk_c.agents)
	{
		pk_gap();
		pk_rfit("AGENTS", pk_c.agents);
	}
	if (pk_c.merged)
	{
		pk_gap();
		pk_rfit("MERGED", pk_c.merged);
	}
	if (pk_c.last)
	{
		pk_gap();
		pk_fit(p->settings ? "ABOUT" : hist ? "DIRECTORY"
		       : "PULL REQUESTS", pk_c.last);
	}
	pk_eol();
}

/* A row's first four columns: the selection's accent edge and its bar,
** then the glyph. */
static void
pk_lead(int sel, const char *hue, const char *glyph)
{
	pk_bg = sel ? PK_P(PK_SEL) : "";
	pk_set(PK_P(PK_ACC));
	pk_txt(!sel ? " " : pk_utf ? "\xe2\x96\x8c" : ">");
	pk_txt(" ");
	pk_set(hue);
	pk_txt(glyph);
	pk_pad(4);
	pk_set(sel ? "1" : "");
}

/* A row's open PRs (dch_meta_prs: "owner/repo#N ...") in w columns, the
** repo faint and without its owner, said once when they all share it
** ("solution #1 #2"), left out when not even the first fits with it; as
** many as fit, then +N for the rest. */
static void
pk_prs(const char *s, int w)
{
	const char *t, *h, *r0 = NULL, *repo;
	char b[480];
	size_t l, rl = 0;
	int n = 0, same = 1, bare, k, need, end = pk_col + w;

	for (t = s; *t; t += l + (t[l] == ' '))
	{
		l = strcspn(t, " ");
		if (!(h = memchr(t, '#', l)))
			h = t + l;
		if (!r0)
			r0 = t, rl = (size_t)(h - t);
		else if ((size_t)(h - t) != rl || strncmp(t, r0, rl))
			same = 0;
		n++;
	}
	if (!n)
	{
		pk_pad(end);
		return;
	}
	same = same && n > 1;
	for (repo = r0 + rl; repo > r0 && repo[-1] != '/'; repo--)
		;
	/* the first: its repo, a blank when it is said once, #N, room for +N */
	bare = (int)(r0 + rl - repo) + same + (int)(strcspn(r0, " ") - rl) +
	       (n > 1 ? 4 : 0) > w;
	for (k = 0, t = s; *t; k++, t += l + (t[l] == ' '))
	{
		l = strcspn(t, " ");
		if (!(h = memchr(t, '#', l)))
			h = t + l;
		for (repo = h; repo > t && repo[-1] != '/'; repo--)
			;
		snprintf(b, sizeof b, "%.*s", bare || (same && k) ? 0
		         : (int)(h - repo), repo);
		need = (k ? 1 : 0) + pk_cps(b) + (!bare && same && !k) +
		       (int)(t + l - h) + (k + 1 < n ? 4 : 0);
		if (pk_col + need > end)
		{
			snprintf(b, sizeof b, "%s+%d", k ? " " : "", n - k);
			pk_set("2");
			pk_txtto(b, end);
			break;
		}
		if (k)
			pk_txt(" ");
		pk_set("2");
		pk_txt(b);
		if (!bare && same && !k)
			pk_txt(" ");
		snprintf(b, sizeof b, "%.*s", (int)(t + l - h), h);
		pk_set("");
		pk_txt(b);
	}
	pk_pad(end);
}

/* One row of Sessions, Sidekicks or History, in pk_c's columns. */
static void
pk_row(const struct pk *p, const struct pkrow *w, int sel)
{
	char b[1400], age[32];
	int hist = p->tab == 2;

	if (hist)
		pk_lead(sel, PK_P(PK_ACC), !w->pinned ? " " : pk_utf
		        ? "\xe2\x97\x86" : "*");
	else
		pk_lead(sel, w->remote == 2 ? "2" : pk_hue(w->state),
		        pk_glyph(w->state));
	if (w->remote)
	{
		/* host: faint, the name as a local one; all faint when stale.
		** The host is a plain label, so its ':' is the first. */
		const char *c = strchr(w->title, ':');
		int end = pk_col + pk_c.name;

		snprintf(b, sizeof b, "%.*s", c ? (int)(c - w->title + 1) : 0,
		         w->title);
		pk_set("2");
		pk_txtto(b, end);
		pk_set(w->remote == 2 ? "2" : sel ? "1" : "");
		pk_fit(c ? c + 1 : w->title, end - pk_col);
	}
	else
		pk_fit(w->title, pk_c.name);
	pk_gap();
	if (w->remote == 2 && !hist)
	{
		pk_set("2");
		pk_fit("Stale", pk_c.state);
	}
	else if (hist)
	{
		const char *e = pk_end(w->state, b, sizeof b);

		/* running (#254): the accent, as a working session is */
		pk_set(w->live ? PK_P(PK_ACC) : !strncmp(e, "crashed", 7)
		       ? PK_P(PK_RED) : "2");
		pk_fit(e, pk_c.state);
	}
	else
	{
		pk_set(pk_hue(w->state));
		pk_fit(pk_label(w->state), pk_c.state);
	}
	pk_age(w->when, age, sizeof age);
	if (pk_c.age)
	{
		pk_gap();
		pk_set("2");
		pk_rfit(age, pk_c.age);
	}
	if (pk_c.agents)
	{
		pk_gap();
		snprintf(b, sizeof b, "%d", w->agents);
		pk_set("");
		pk_rfit(w->agents > 0 ? b : "", pk_c.agents);
	}
	if (pk_c.merged)
	{
		pk_gap();
		pk_mergedtxt(w->merged, pk_c.mw, b, sizeof b);
		pk_set("2");
		pk_rfit(w->merged > 0 ? b : "", pk_c.merged);
	}
	if (pk_c.last && hist)
	{
		pk_gap();
		pk_set("2");
		pk_tail(w->cwd, pk_c.last);
	}
	else if (pk_c.last)
	{
		pk_gap();
		pk_prs(w->prs, pk_c.last);
	}
	if (sel)
		pk_pad(pk_w);	/* the bar the whole width: one unit */
}

#ifndef _WIN32
/* A settings row: the key, its value, what it is for. */
static void
pk_cfrow(int k, int sel)
{
	pk_lead(sel, "", " ");
	pk_fit(config_keys[k].key, pk_c.name);
	pk_gap();
	pk_cfval(k, 1);
	pk_pad(4 + pk_c.name + pk_c.gap + pk_c.state);
	if (pk_c.last)
	{
		pk_gap();
		pk_set("2");
		pk_fit(config_keys[k].desc, pk_c.last);
	}
	if (sel)
		pk_pad(pk_w);
}
#endif

/* A segment of the tab bar: the active one in the accent on a tint (in
** brackets without colour), its count faint. */
void
pk_tab(const char *name, int n, int on)
{
	char b[24];

	if (pk_col)
		pk_txt(" ");
	pk_bg = on ? PK_P(PK_CHIP) : "";
	pk_set(on ? PK_P(PK_ACCB) : "");
	pk_txt(on && !pk_color ? "[" : " ");
	pk_txt(name);
	if (n >= 0)
	{
		snprintf(b, sizeof b, " %d", n);
		pk_set("2");
		pk_txt(b);
	}
	pk_set(on ? PK_P(PK_ACCB) : "");
	pk_txt(on && !pk_color ? "]" : " ");
	pk_bg = "";
	pk_set("");
}

/* Write the frame: only the lines that changed since the last one, each
** placing itself and ending in the plain attributes, in one write; all of
** it when forced, after a prompt drew over it (pk_pn 0), or when the lines
** moved. Nothing when nothing changed. */
void
pk_flush(int fd, int force)
{
	size_t o, n = 0;
	int k;

	if (pk_nl > PK_LINES)
		force = 1;
	if (pk_nl <= PK_LINES)
		pk_lo[pk_nl] = pk_fn;
	if (force || !pk_pn || pk_nl != pk_pnl || pk_lo[0] != pk_plo[0] ||
	    memcmp(pk_fb, pk_prev, pk_lo[0]))
	{
		pk_write(fd, pk_fb, pk_fn);
		n = 1;
	}
	else
	{
		for (k = 0; k < pk_nl; k++)
		{
			o = pk_lo[k + 1] - pk_lo[k];
			if (o == pk_plo[k + 1] - pk_plo[k] &&
			    !memcmp(pk_fb + pk_lo[k], pk_prev + pk_plo[k], o))
				continue;
			memcpy(pk_out + n, pk_fb + pk_lo[k], o);
			n += o;
		}
		if (n)
			pk_write(fd, pk_out, n);
	}
	if (!n)
		return;
	memcpy(pk_prev, pk_fb, pk_fn);
	memcpy(pk_plo, pk_lo, sizeof pk_lo[0] *
	       (size_t)((pk_nl < PK_LINES ? pk_nl : PK_LINES) + 1));
	pk_pn = pk_fn;
	pk_pnl = pk_nl;
}

/* A new frame on fd's terminal, at colour level color: the picker's and
** the X-ray view's (#175). */
void
pk_begin(int fd, int color)
{
	int cols = 0, rows = 0;

	pk_size(fd, &cols, &rows);
	if (cols <= 0 || rows <= 0)
	{
		cols = 80;
		rows = 24;
	}
	pk_w = cols > 1000 ? 1000 : cols;
	pk_h = rows;
	pk_y = pk_fn = 0;
	pk_off = pk_nl = 0;
	pk_color = color;
	pk_bg = "";
	snprintf(pk_cur, sizeof pk_cur, "0");
	if (pk_color)
		pk_raw("\033[0m");
}

/* The frame done: below it cleared, written (pk_flush). */
void
pk_fend(int fd, int force)
{
	pk_off = 0;
	pk_raw("\033[J");
	pk_flush(fd, force);
}

/* s right-aligned and faint, when it fits with room to spare: the
** picker's first row gives it up first at narrow widths. */
static void
pk_aside(const char *s)
{
	if (pk_w - pk_col >= pk_cps(s) + 4)
	{
		pk_pad(pk_w - pk_cps(s));
		pk_set("2");
		pk_txt(s);
	}
}

static void
pk_draw(struct pk *p, int fd, const char *header, int force)
{
	static const char *const spin[2] = { "|/-\\",
		"\xe2\xa0\x8b\xe2\xa0\x99\xe2\xa0\xb9\xe2\xa0\xb8\xe2\xa0\xbc"
		"\xe2\xa0\xb4\xe2\xa0\xa6\xe2\xa0\xa7\xe2\xa0\x87\xe2\xa0\x8f" };
	static const char *const tabn[3] = { "Sessions", "Sidekicks", "History" };
	struct pkkey keys[24];
	char b[96], go[16];
	const char *ret;
	int nk = 0, k, t, rows, kl, cap, tabs = 0, pos, ri;
	/* the rows drawn: the kept ones (pk_pos), or the settings */
#ifdef _WIN32
	int n = pk_pos(p, p->tab, p->n[p->tab]);
#else
	int n = p->settings ? (int)NCONF : pk_pos(p, p->tab, p->n[p->tab]);
	const struct pkrow *cur;
	char intr[16];
#endif
	int *sel = p->settings ? &p->cfsel : &p->sel[p->tab];
	int *top = p->settings ? &p->cftop : &p->top[p->tab];
	unsigned drop;

	pk_begin(fd, p->color);
	rows = pk_h;
	if (!p->settings)
		pk_fix(p, p->tab);
#ifndef _WIN32
	cur = !p->settings && *sel < p->n[p->tab] ? &p->r[p->tab][*sel] : NULL;
#endif
	ret = pk_utf ? "\xe2\x86\xb5" : "enter";
	/* what Enter does to a live row: attach, or the verb of the -k and
	** -d pickers, the first word of their header ("kill session:") */
	snprintf(go, sizeof go, "%s", p->opens ? "attach" : header);
	go[strcspn(go, " ")] = '\0';
	for (t = 0; t < 3; t++)
		tabs += pk_shown(p, t);

	/* the hints for this view and row: actions, the agents', the view,
	** moving about */
#define PK_K(g, k, l, v, x) \
	(keys[nk++] = (struct pkkey){ (k), (l), (v), (g), (x) })
	if (p->filtering)
	{
		PK_K(0, ret, p->tab == 2 ? "resume" : go, NULL, 0);
		PK_K(3, pk_utf ? "\xe2\x86\x91\xe2\x86\x93" : "up/down", "keep",
		     NULL, 0);
		PK_K(3, "esc", "clear", NULL, 0);
	}
	else if (p->settings)
	{
		PK_K(0, ret, "change", NULL, 0);
		PK_K(0, "u", "default", NULL, 0);
		PK_K(3, "esc", "back", NULL, 0);
	}
#ifdef _WIN32
	else	/* remote rows (#090), which only attach */
		PK_K(0, ret, "attach", NULL, 0);
#else
	else if (p->tab < 2)
	{
		PK_K(0, ret, go, NULL, 0);
		PK_K(0, "r", "rename", NULL, 0);
		PK_K(0, "X", "kill", NULL, 1);
		PK_K(0, "x", "x-ray", NULL, 0);
		PK_K(1, "R", "restart", NULL, 0);
		PK_K(1, "S", "stop", NULL, 1);
		if (cur && !strcmp(cur->state, "blocked"))
			PK_K(1, "a", "allow", NULL, 0);
		PK_K(1, "b", "stale", NULL, 0);
		PK_K(1, "B", "force all", NULL, 1);
		PK_K(2, "s", "sort", p->byname ? "name" : "activity", 0);
	}
	else
	{
		/* a running name's row (#254): it attaches, nothing resumes
		** or removes it */
		int run = cur && cur->live;

		if (!run || p->opens)
			PK_K(0, ret, run ? "attach" : "resume", NULL, 0);
		if (p->opens && !run)
			PK_K(0, pk_utf ? "\xe2\x87\xa7\xe2\x86\xb5/o"
			     : "S-enter/o", "open", NULL, 0);
		if (!run)
			PK_K(0, "m", "no message", NULL, 0);
		if (p->interrupted)
		{
			snprintf(intr, sizeof intr, "%d", p->interrupted);
			PK_K(0, "I", "interrupted", intr, 0);
		}
		PK_K(0, "p", cur && cur->pinned ? "unpin" : "pin", NULL, 0);
		if (!run)
			PK_K(0, "d", "remove", NULL, 1);
		PK_K(0, "x", "x-ray", NULL, 0);
	}
	if (!p->settings && !p->filtering)
		PK_K(2, ",", "settings", NULL, 0);
#endif
	if (!p->settings)
	{
		if (!p->filtering)
			PK_K(3, "/", "filter", NULL, 0);
		if (tabs > 1)
			PK_K(3, pk_utf ? "\xe2\x87\xa5" : "tab", "tabs", NULL, 0);
		if (!p->filtering)
			PK_K(3, "q", "quit", NULL, 0);
	}
#undef PK_K
	/* too wide: the agents' group goes, then the view's */
	cap = rows >= 16 ? 2 : 1;
	for (drop = 0; (kl = pk_keys(keys, nk, drop, cap, 0)) < 0 && drop < 6;
	     drop = drop * 2 + 2)
		;
	if (kl < 0)
		kl = cap;

	/* the title (the -k and -d pickers' only, #252), tabs, column headers,
	** rule and status around the list */
	p->win = rows - (*header ? 5 : 4) - kl;
	if (p->win < 1)
		p->win = 1;
	if (p->settings && *sel >= n)
		*sel = n - 1;
	if (*sel < 0)
		*sel = 0;
	/* top and pos: places in the list drawn, not row indexes */
	pos = p->settings ? *sel : pk_pos(p, p->tab, *sel);
	if (*top > pos)
		*top = pos;
	if (pos >= *top + p->win)
		*top = pos - p->win + 1;
	if (*top > 0 && *top + p->win > n)
		*top = n - p->win > 0 ? n - p->win : 0;
	ri = p->settings ? *top : pk_nth(p, p->tab, *top);
	pk_cols(p, ri, (*top + p->win < n ? *top + p->win : n) - *top);

	/* the title, quiet, and what there is on the right, faint: on the
	** tabs row when there is no title (the switch picker's, #252) */
	k = p->n[0] + p->n[1];
	snprintf(b, sizeof b, "%d session%s %s dch %s", k, k == 1 ? "" : "s",
	         pk_utf ? "\xc2\xb7" : "-", DCH_VERSION);
	if (*header)
	{
		pk_line();
		pk_set("1");
		pk_txt(header);
		pk_aside(b);
		pk_eol();
	}

	pk_line();
	if (p->settings)
		pk_tab("Settings", -1, 1);
	for (t = 0; t < 3 && !p->settings; t++)
		if (pk_shown(p, t))
			pk_tab(tabn[t], pk_pos(p, t, p->n[t]), t == p->tab);
	if (!*header)
		pk_aside(b);
	pk_eol();

	pk_heads(p);
	for (k = *top; k < *top + p->win; k++)
	{
		pk_line();
#ifndef _WIN32
		if (k < n && p->settings)
			pk_cfrow(k, k == *sel);
		else
#endif
		if (k < n)
		{
			pk_row(p, &p->r[p->tab][ri], ri == *sel);
			ri = pk_next(p, p->tab, ri + 1);
		}
		else if (k == 0)
		{
			pk_set("2");
			pk_txt(p->filt[0] ? "    No match" : p->tab == 2
			       ? "    No history" : "    No sessions");
		}
		pk_eol();
	}

	pk_rule();
	pk_line();
	p->status_row = pk_y;
	if (p->asking)
	{
		pk_set(PK_P(PK_WARN));
		pk_txt(p->status);
		pk_txt(" [y/N]");
	}
	else
	{
		if (!p->settings && (p->filtering || p->filt[0]))
		{
			/* the filter, a block for the caret while it is typed */
			pk_set(PK_P(PK_ACC));
			pk_txt("/");
			pk_set("");
			pk_txt(p->filt);
			if (p->filtering)
				pk_txt(pk_utf ? "\xe2\x96\x88" : "_");
			pk_txt("  ");
		}
		if (p->pid > 0)
		{
			/* the busy spinner: a frame a tenth of a second */
			int f = (int)(now_ms() / 100 % (pk_utf ? 10 : 4));

			snprintf(b, sizeof b, "%.*s ", pk_utf ? 3 : 1,
			         spin[pk_utf] + f * (pk_utf ? 3 : 1));
			pk_set(PK_P(PK_ACC));
			pk_txt(b);
			pk_set("2");
			pk_txt(p->run.doing);
			pk_txt(pk_utf ? "\xe2\x80\xa6" : "...");
			if (p->status[0])
				pk_txt("  ");
		}
		pk_set("");
		pk_txt(p->status);
	}
	pk_eol();
	pk_keys(keys, nk, drop, cap, 1);
	pk_fend(fd, force);
}

/* Is another byte of an escape sequence there within 50ms? */
static int
pk_more(int fd)
{
	return pk_wait(fd, 50) > 0;
}

/* The next key: a byte, PK_* for a sequence we know, 0 for one we do not,
** -1 at the end of input. */
int
pk_key(int fd)
{
	unsigned char c, s[16];
	int n = 0;

	if (pk_rd(fd, &c) < 0)
		return -1;
	if (c != 27)
		return c;
	if (!pk_more(fd) || pk_rd(fd, &c) < 0)
		return PK_ESC;
	if (c != '[' && c != 'O')
		return 0;
	while (n < (int)sizeof s && pk_more(fd) && pk_rd(fd, &s[n]) == 0)
		if (s[n++] >= 0x40)	/* the final byte */
			break;
	return pk_csi(s, n);
}

/* The key a CSI (or SS3) sequence's bytes after ESC [ name, s[n - 1] its
** final byte; 0 for one we do not know. The kitty keyboard protocol's
** CSI code[;mods]u (#132; the picker pushes its disambiguate flag), lock
** bits ignored (#137): Esc, Ctrl-letter and Shift+Tab as their legacy
** keys, Shift+Enter as PK_SENTER, as is xterm modifyOtherKeys' CSI
** 27;2;13~. */
int
pk_csi(const unsigned char *s, int n)
{
	int code = 0, mods = 1, k = 0;

	if (n <= 0)
		return 0;
	switch (s[n - 1])
	{
	case 'u':
		while (k < n - 1 && isdigit(s[k]) && code < 0x110000)
			code = code * 10 + (s[k++] - '0');
		if (k < n - 1 && s[k] == ';')
			for (mods = 0, k++; k < n - 1 && isdigit(s[k]) &&
			     mods < 1000; k++)
				mods = mods * 10 + (s[k] - '0');
		if (mods > 1)	/* drop Caps Lock (64) and Num Lock (128) */
			mods = ((mods - 1) & ~(64 | 128)) + 1;
		if (mods == 2)	/* Shift */
			return code == 13 ? PK_SENTER : code == 9 ? PK_BTAB : 0;
		if (mods == 5 && code >= 'a' && code <= 'z')	/* Ctrl */
			return code & 0x1f;
		return mods > 1 ? 0 : code == 27 ? PK_ESC : code > 0 &&
		       code < 0x80 ? code : 0;
	case 'A': return PK_UP;
	case 'B': return PK_DOWN;
	case 'C': return PK_RIGHT;
	case 'D': return PK_LEFT;
	case 'Z': return PK_BTAB;
	case 'H': return PK_HOME;
	case 'F': return PK_END;
	case '~':
		if (n == 8 && !memcmp(s, "27;2;13~", 8))
			return PK_SENTER;
		return s[0] == '5' ? PK_PGUP : s[0] == '6' ? PK_PGDN
		       : s[0] == '1' || s[0] == '7' ? PK_HOME
		       : s[0] == '4' || s[0] == '8' ? PK_END : 0;
	}
	return 0;
}

/* ---- what every host's picker shares past the drawing ---- */

/* A remote row (#087), host:name as that host last answered; its fields as
** the peer sent them, so the shown title is escaped. */
void
pk_rrow(struct pkrow *w, const struct dch_rrow *r)
{
	char e[sizeof w->title - DCH_RHOST_MAX];

	snprintf(w->name, sizeof w->name, "%s:%s", r->host, r->name);
	dch_remote_esc(e, sizeof e, r->name);
	snprintf(w->title, sizeof w->title, "%s:%s", r->host, e);
	snprintf(w->state, sizeof w->state, "%s", r->state);
	w->remote = r->stale ? 2 : 1;
}

/* The keys every picker has: quit, and moving about the rows (the kept
** ones) and the tabs. -1: quit; 1: c was one of them; 0: it is the host's. */
int
pk_nav(struct pk *p, int c)
{
	int t = p->tab, k, pos = pk_pos(p, t, p->sel[t]);
	int cnt = pk_pos(p, t, p->n[t]);

	switch (c)
	{
	case 'q': case 3: case 4: case PK_ESC:
		return -1;
	case 'j': case PK_DOWN:
		pos++;
		break;
	case 'k': case PK_UP:
		pos--;
		break;
	case PK_PGDN:
		pos += p->win;
		break;
	case PK_PGUP:
		pos -= p->win;
		break;
	case 'g': case PK_HOME:
		pos = 0;
		break;
	case 'G': case PK_END:
		pos = cnt - 1;
		break;
	case '\t': case 'l': case PK_RIGHT:
	case PK_BTAB: case 'h': case PK_LEFT:
		k = c == '\t' || c == 'l' || c == PK_RIGHT ? 1 : 2;
		do
			t = (t + k) % 3;
		while (!pk_shown(p, t));
		p->tab = t;
		return 1;
	default:
		return 0;
	}
	if (pos >= cnt)
		pos = cnt - 1;
	if (pos < 0)
		pos = 0;
	p->sel[t] = pk_nth(p, t, pos);
	return 1;
}

/* The filter off: every row back, each selection where it was (the top on
** a tab that had none kept). */
static void
pk_unfilt(struct pk *p)
{
	int t;

	p->filt[0] = '\0';
	p->filtering = 0;
	for (t = 0; t < 3; t++)
		if (p->sel[t] >= p->n[t])
			p->sel[t] = 0;
}

/* The filter's keys (#096). "/" opens its prompt; there printable keys,
** UTF-8 as typed, go to the text, Backspace takes a character back (on
** nothing it closes the prompt), Ctrl-U clears it and Esc clears it and
** closes; Enter closes the prompt and goes on to the host, which acts on
** the selected row; Up and Down (the arrows, not j/k) close it too and go
** on to the host, which moves among the kept rows, so the letter actions
** work on a filtered row (#108); the text stays until Esc. A changed text selects the
** top match of every tab. 1: c was the filter's. */
static int
pk_filt(struct pk *p, int fd, int c)
{
	size_t l = strlen(p->filt);
	unsigned char b;
	int t, more;

	if (!p->filtering)
	{
		if (c == '/')
		{
			p->filtering = 1;
			p->status[0] = '\0';
			return 1;
		}
		if (c != PK_ESC || !p->filt[0])
			return 0;
		pk_unfilt(p);
		return 1;
	}
	switch (c)
	{
	case '\r': case '\n': case PK_SENTER: case PK_UP: case PK_DOWN:
		p->filtering = 0;
		return 0;
	case PK_ESC:
		pk_unfilt(p);
		return 1;
	case 0x7f: case 8:
		if (!l)
		{
			p->filtering = 0;
			return 1;
		}
		while (l && ((unsigned char)p->filt[--l] & 0xC0) == 0x80)
			;
		p->filt[l] = '\0';
		break;
	case 0x15:	/* Ctrl-U */
		p->filt[0] = '\0';
		break;
	default:
		if (c < 0x20 || c > 0xff)
			return 0;	/* Tab, the arrows, Ctrl-C: the host's */
		/* a character's bytes together: no frame shows half of one */
		more = c >= 0xf0 ? 3 : c >= 0xe0 ? 2 : c >= 0xc0;
		b = (unsigned char)c;
		do
		{
			if (l + 1 < sizeof p->filt)
				p->filt[l++] = (char)b;
		} while (more-- > 0 && pk_more(fd) && pk_rd(fd, &b) == 0);
		p->filt[l] = '\0';
	}
	for (t = 0; t < 3; t++)
	{
		p->sel[t] = 0;
		pk_fix(p, t);
	}
	return 1;
}

/* The picker's place (#098), kept across the pickers of one process (the
** switch reopens it): the last one's tab, sort, filter and selections, and
** the session the host says was just left, selected in the next one. */
static struct
{
	int set, tab, byname;
	char filt[sizeof ((struct pk *)0)->filt], sel[3][600], focus[600];
	int fhist;	/* the focus is a History record's (#133) */
} pk_mem;

void
pk_focus(const char *name)
{
	snprintf(pk_mem.focus, sizeof pk_mem.focus, "%s", name ? name : "");
	pk_mem.fhist = 0;
}

void
pk_focus_history(const char *file)
{
	pk_focus(file);
	pk_mem.fhist = 1;
}

/* p's place, for the next picker; its filter's prompt closed. */
static void
pk_keep(const struct pk *p)
{
	pk_mem.set = 1;
	pk_mem.tab = p->tab;
	pk_mem.byname = p->byname;
	snprintf(pk_mem.filt, sizeof pk_mem.filt, "%s", p->filt);
	pk_selnames(p, pk_mem.sel);
}

/* The color level from the environment, pk_utf and pk_light set (vt: a VT
** console whatever TERM says, Windows'): none, the basic eight, the 256
** cube, truecolor; TERM=dumb draws ASCII too. A Windows console has no
** TERM, and since Windows 10 does truecolor. The picker's and the X-ray
** view's (#175). */
int
pk_colors(int vt)
{
	const char *nc = getenv("NO_COLOR"), *term = getenv("TERM");
	const char *ct = getenv("COLORTERM"), *fgbg = getenv("COLORFGBG");

	pk_utf = term && *term ? strcmp(term, "dumb") != 0 : vt;
	/* COLORFGBG "fg;bg": a white or light grey background */
	pk_light = fgbg && (fgbg = strrchr(fgbg, ';')) &&
	           (atoi(fgbg + 1) == 7 || atoi(fgbg + 1) == 15);
	return !pk_utf || (nc && *nc) ? 0 : ct && (!strcmp(ct, "truecolor") ||
	       !strcmp(ct, "24bit")) ? 3 : term && strstr(term, "256color")
	       ? 2 : vt ? 3 : 1;
}

/* A new picker in p, its colours from the environment (vt: a VT console
** whatever TERM says, Windows'), its rows loaded; with remote set and none
** here nor cached, the remote hosts probed once, in the box. In the last
** picker's place, when there was one, on the row pk_focus named when it is
** there: its tab, the nearest row the filter keeps. 0, or -1 when there
** are no rows at all ("no sessions" said). */
int
pk_open(struct pk *p, int remote, int vt)
{
	int t, k, j;

	memset(p, 0, sizeof *p);
	p->pid = -1;
	p->color = pk_colors(vt);
	p->byname = pk_mem.set ? pk_mem.byname : pk_cfbyname();
	snprintf(p->filt, sizeof p->filt, "%s", pk_mem.filt);
	pk_load(p);
	if (remote && !p->n[0] && !p->n[1] && !p->n[2])
	{
		/* nothing here and nothing cached: probe once, in the box */
		struct dch_rrow *rr;

		(void)dch_remote_rows(&rr, 1);
		free(rr);
		pk_load(p);
	}
	p->loaded = now_ms();
	if (!p->n[0] && !p->n[1] && !p->n[2])
	{
		fprintf(stderr, "no sessions\n");
		return -1;
	}
	p->tab = p->n[0] ? 0 : p->n[1] ? 1 : 2;
	if (pk_mem.set)
	{
		pk_reselect(p, pk_mem.sel);
		if (pk_shown(p, pk_mem.tab))
			p->tab = pk_mem.tab;
	}
	/* a session that ended: History, its record or none (#133) */
	if (pk_mem.fhist && pk_shown(p, 2))
		p->tab = 2;
	/* the session left: on its tab, this one first */
	for (j = 0; pk_mem.focus[0] && j < (pk_mem.fhist ? p->tab == 2 : 3); j++)
	{
		t = (p->tab + j) % 3;
		if (pk_shown(p, t) && (k = pk_find(p, t, pk_mem.focus)) >= 0)
		{
			p->tab = t;
			p->sel[t] = k;
			pk_fix(p, t);
			break;
		}
	}
	pk_mem.focus[0] = '\0';
	pk_mem.fhist = 0;
	return 0;
}

/* The picker on the terminal the host has made raw, until a key picks a
** session (0, its name in out) or quits (1). Rows reload once a second. */
int
pk_run(struct pk *p, int fd, const char *header, char *out, size_t outsz)
{
	int k;

	pk_pn = 0;
	for (;;)
	{
		long long wait;
		int c, force = pk_winch;

		pk_winch = 0;
		if (now_ms() - p->loaded >= 1000)
		{
			pk_load(p);
			p->loaded = now_ms();
			if (!pk_shown(p, p->tab))
				p->tab = 0;
		}
		pk_draw(p, fd, header, force);
		wait = 1000 - (now_ms() - p->loaded);
		if (p->pid > 0 && wait > 100)
			wait = 100;	/* the running action's end shows soon */
		k = pk_wait(fd, wait < 10 ? 10 : (long)wait);
		pk_reap(p);
		/* a revive asked to attach (#132) is up; a cut name is
		** another session's */
		if (p->go[0] && (size_t)snprintf(out, outsz, "%s", p->go) < outsz)
		{
			pk_keep(p);
			return 0;
		}
		p->go[0] = '\0';
		if (k <= 0)
			continue;	/* the tick, or a resize */
		if ((c = pk_key(fd)) < 0)
			k = -1;
		else if (!p->asking && !p->settings && pk_filt(p, fd, c))
			continue;
		else
			k = pk_do(p, fd, c, out, outsz);
		if (k)
		{
			pk_keep(p);
			return k > 0 ? 0 : 1;
		}
	}
}
