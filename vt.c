/*
 * vt.c - terminal mirror implementation backed by libghostty-vt.
 * See vt.h for the interface contract.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <time.h>
#include <ghostty/vt.h>
#include "vt.h"
#include "ring.h"

static GhosttyTerminal the_term; /* NULL when disabled/failed */
static GhosttyKeyEncoder the_encoder;
static int cur_cols, cur_rows;
static size_t sb_lines; /* history kept: sb_user, or sb_eff with the ring on */
static size_t sb_user; /* DCH_SCROLLBACK; 0 = no scrollback the user sees */
static unsigned long gen; /* bumped on every feed/resize: see dch_vt_size */

/* Arbitrary cell pixel size; only image protocols and size reports care. */
#define CELL_W 8
#define CELL_H 16

int
dch_vt_enabled(void)
{
	return the_term != NULL;
}

/* The grid allocation scales with cols*rows: a crafted 65535x65535 geometry
** measured 14.5 GB RSS. Real terminals top out around 700x400 on a 4K display;
** clamp the MIRROR only (the real pty winsize is untouched, apps still see what
** the client sent). Applied at init as well as resize: a restart re-inits the
** mirror at whatever winsize the session was last told, which a client picks. */
static void
clamp_grid(int *cols, int *rows)
{
	if (*cols < 1)
		*cols = 80;
	if (*rows < 1)
		*rows = 24;
	if (*cols > 1024)
		*cols = 1024;
	if (*rows > 1024)
		*rows = 1024;
}

/* libghostty drops history a whole page at a time, and only once the line
** limit is exceeded: with a limit of N, history falls to N minus a page right
** after a prune (at N=100 and 80 cols, down to one line). Ask for N plus one
** page so at least N lines are always kept, and at most a page more. A
** standard page holds a fixed ~383 KB grid (ghostty page.zig std_capacity),
** 8 bytes per cell plus an 8-byte row header, so ~47900/(cols+1) rows: 591 at
** 80 cols, 238 at 200. 49152 = 384 KB / 8 rounds that up a little.
** ponytail: the page size is ghostty-internal, not in the C API; the depth
** check in tests/run.sh fails if an upgrade changes it. */
static size_t
page_rows(void)
{
	return 49152 / ((size_t)cur_cols + 1);
}

static int
set_scrollback_lines(void)
{
	size_t n = sb_lines + page_rows();

	return ghostty_terminal_set(the_term,
	    GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES, &n) == GHOSTTY_SUCCESS
	    ? 0 : -1;
}

#define FMT_UNWRAP DCH_VT_FMT_UNWRAP
static int format_sel(int format, const GhosttySelection *sel, char **out,
                      size_t *outlen);
static int format_unwrap(const GhosttySelection *sel, char **out,
                         size_t *outlen);
static int select_rows(size_t y0, size_t y1, GhosttySelection *sel);

/* ---- X-ray capture (#119) -------------------------------------------------
** Rows are copied when they scroll into the primary screen's history. A
** tracked grid ref sits on the newest row captured so far: rows below it in
** history are new, and when an erase of history (ED 3 in any spelling, RIS)
** discards its row the ring empties (§13.3). The feed is cut into slices
** that cannot scroll more rows than the history keeps (§14.1), so a prune
** never takes the ref's row and its loss can only mean an erase. */
static struct dch_ring *xr;		/* NULL: capture off */
static GhosttyTrackedGridRef xref;	/* newest captured history row */
/* With no history row tracked, the screen's top row over one slice: rows
** plus an erase in the same slice only show as its loss (#125) */
static GhosttyTrackedGridRef xsent;
static int xpaused, xresume, xreanchor;
static int xris;			/* a full reset (RIS) came */
static int xb_open, xinput;		/* inside OSC 133 B..C; input seen */
static int xlast_kind = RING_K_OUTPUT;	/* what a CONT record continues */
static size_t slice_b = 1;		/* row budget of one feed slice */

/* A mark waits on its row (a ref at the cursor when it came) until that row
** is captured, so it lands between the right records. */
#define PM_MAX 64
static struct pmark
{
	GhosttyTrackedGridRef ref;
	char *text;
	size_t len;
	uint32_t t;
	int flags;
} pm[PM_MAX];
static int npm;

static char *wb;	/* walk_row's line buffer */
static size_t wcap;
static char *ubuf;	/* format_unwrap's reused output */
static size_t ucap;

static int
xon(void)
{
	return the_term && xr && xr->cap;
}

static uint32_t
now32(void)
{
	return (uint32_t)time(NULL);
}

/* §14.1: sb_eff = max(DCH_SCROLLBACK, a page, one REP plus a screen) while
** the ring is on, and a slice may cost at most half of it in rows. With the
** REP term a single-byte slice (the meter's floor) also fits, and a prune,
** which keeps sb_eff rows, never looks like an erase (capture()). */
static size_t seq_max(void);

static void
xr_budget(void)
{
	size_t eff = sb_user;

	if (xr && xr->cap)
	{
		if (eff < page_rows())
			eff = page_rows();
		if (eff < seq_max() + (size_t)cur_rows + 2)
			eff = seq_max() + (size_t)cur_rows + 2;
	}
	sb_lines = eff;
	slice_b = eff / 2 ? eff / 2 : 1;
}

/* History rows a reader may show: the capture-only extra never does. */
static size_t
user_history(size_t h)
{
	size_t vis;

	if (sb_lines == sb_user)
		return h;
	vis = sb_user ? sb_user + page_rows() : 0;
	return h < vis ? h : vis;
}

static int
alt_active(void)
{
	GhosttyTerminalScreen s = GHOSTTY_TERMINAL_SCREEN_PRIMARY;

	ghostty_terminal_get(the_term, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &s);
	return s != GHOSTTY_TERMINAL_SCREEN_PRIMARY;
}

static size_t
hist_rows(void)
{
	size_t h = 0;

	ghostty_terminal_get(the_term, GHOSTTY_TERMINAL_DATA_SCROLLBACK_ROWS,
	    &h);
	return h;
}

/* (Re)point r at (x, y) in `tag` coordinates; NULL when that fails. */
static GhosttyTrackedGridRef
track(GhosttyTrackedGridRef r, GhosttyPointTag tag, size_t x, size_t y)
{
	GhosttyPoint p;

	memset(&p, 0, sizeof p);
	p.tag = tag;
	p.value.coordinate.x = (uint16_t)x;
	p.value.coordinate.y = (uint32_t)y;
	if (r)
	{
		if (ghostty_tracked_grid_ref_set(r, the_term, p) ==
		    GHOSTTY_SUCCESS)
			return r;
		ghostty_tracked_grid_ref_free(r);
		return NULL;
	}
	if (ghostty_terminal_grid_ref_track(the_term, p, &r) !=
	    GHOSTTY_SUCCESS)
		return NULL;
	return r;
}

/* Screen (history-inclusive) position of a tracked ref; -1 once erased. */
static int
ref_pos(GhosttyTrackedGridRef r, size_t *x, size_t *y)
{
	GhosttyPointCoordinate c;

	if (!r || ghostty_tracked_grid_ref_point(r, GHOSTTY_POINT_TAG_SCREEN,
	    &c) != GHOSTTY_SUCCESS)
		return -1;
	if (x)
		*x = c.x;
	*y = c.y;
	return 0;
}

/* Everything in history now counts as captured. Primary screen only. */
static void
anchor(void)
{
	size_t h = hist_rows();

	if (h)
		xref = track(xref, GHOSTTY_POINT_TAG_SCREEN, 0, h - 1);
	else if (xref)
	{
		ghostty_tracked_grid_ref_free(xref);
		xref = NULL;
	}
}

static void
pm_drop(int i)
{
	ghostty_tracked_grid_ref_free(pm[i].ref);
	free(pm[i].text);
	memmove(pm + i, pm + i + 1, (size_t)(npm - i - 1) * sizeof *pm);
	npm--;
}

static void
pm_emit(int i)
{
	ring_append(xr, RING_K_MARK, pm[i].flags, pm[i].t, pm[i].text,
	    pm[i].len);
	pm_drop(i);
}

/* GHOSTTY_TERMINAL_OPT_SEMANTIC_PROMPT: the mirror's own (fragment-safe)
** OSC 133 parse reports a step; record it as a mark (§11.7). */
static void
on_prompt(GhosttyTerminal t, void *ud, const GhosttyTerminalSemanticPrompt *ev)
{
	char head[24], *text;
	size_t hl, cl = 0;
	uint16_t cx = 0, cy = 0;
	int flags = 0;

	(void)t;
	(void)ud;
	if (!xon())
		return;
	switch (ev->kind)
	{
	case GHOSTTY_SEMANTIC_PROMPT_PROMPT_START:
		if (ev->prompt_kind == GHOSTTY_SEMANTIC_PROMPT_PROMPT_PRIMARY)
		{
			snprintf(head, sizeof head, "A");
			if (xpaused && !xresume)
			{
				xresume = 1;	/* the shell's prompt is back */
				ring_touch(xr);	/* paused flips: wake readers */
			}
		}
		else
			snprintf(head, sizeof head, "A;%c",
			    ev->prompt_kind == GHOSTTY_SEMANTIC_PROMPT_PROMPT_RIGHT
			    ? 'r' : ev->prompt_kind ==
			    GHOSTTY_SEMANTIC_PROMPT_PROMPT_CONTINUATION ? 'c'
			    : 's');
		break;
	case GHOSTTY_SEMANTIC_PROMPT_INPUT_START:
		snprintf(head, sizeof head, "B");
		xb_open = 1;
		xinput = 0;
		break;
	case GHOSTTY_SEMANTIC_PROMPT_OUTPUT_START:
		snprintf(head, sizeof head, "C");
		if (xb_open && xinput)
			flags |= RING_F_INPUT;
		xb_open = 0;
		cl = ev->command.ptr ? ev->command.len : 0;
		break;
	case GHOSTTY_SEMANTIC_PROMPT_COMMAND_END:
		if (ev->has_exit_code)
		{
			snprintf(head, sizeof head, "D;%ld", (long)ev->exit_code);
			flags |= RING_F_EXIT;
		}
		else
			snprintf(head, sizeof head, "D");
		xb_open = 0;
		break;
	default:
		return;
	}
	if (xpaused && !xresume)
		return;
	if (cl > RING_TEXT_MAX)
		cl = RING_TEXT_MAX;	/* ring_append cuts it on a boundary */
	hl = strlen(head);
	if (!(text = malloc(hl + 1 + cl)))
		return;
	memcpy(text, head, hl);
	if (cl)
	{
		text[hl] = ';';
		memcpy(text + hl + 1, ev->command.ptr, cl);
		hl += 1 + cl;
	}
	/* No primary row to wait on: on the alt screen it lands now. */
	if (alt_active())
	{
		ring_append(xr, RING_K_MARK, flags | RING_F_ALT, now32(), text,
		    hl);
		free(text);
		return;
	}
	if (npm == PM_MAX)
		pm_emit(0);	/* shortcut: the oldest lands unplaced; 64 is
				** many screens of prompts */
	ghostty_terminal_get(the_term, GHOSTTY_TERMINAL_DATA_CURSOR_X, &cx);
	ghostty_terminal_get(the_term, GHOSTTY_TERMINAL_DATA_CURSOR_Y, &cy);
	pm[npm].ref = track(NULL, GHOSTTY_POINT_TAG_ACTIVE, cx, cy);
	if (!pm[npm].ref)
	{
		ring_append(xr, RING_K_MARK, flags, now32(), text, hl);
		free(text);
		return;
	}
	pm[npm].text = text;
	pm[npm].len = hl;
	pm[npm].t = now32();
	pm[npm].flags = flags;
	npm++;
}

/* GHOSTTY_TERMINAL_OPT_RESET: RIS erases history even when no row of it was
** tracked (alt-screen marks may be all the ring holds). */
static void
on_reset(GhosttyTerminal t, void *ud)
{
	(void)t;
	(void)ud;
	xris = 1;
}

/* Emit marks queued at a row before y, or at row y up to column x. */
static void
marks_upto(size_t x, size_t y, void (*flush)(void))
{
	size_t mx, my;

	while (npm)
	{
		if (ref_pos(pm[0].ref, &mx, &my) < 0)
		{
			pm_drop(0);	/* its row was erased */
			continue;
		}
		if (my > y || (my == y && mx > x))
			return;
		if (flush)
			flush();
		pm_emit(0);
	}
}

static int
wb_room(size_t n, size_t more)
{
	char *nb;
	size_t nc;

	if (n + more <= wcap)
		return 0;
	nc = wcap ? wcap * 2 : 1024;
	while (nc < n + more)
		nc *= 2;
	if (!(nb = realloc(wb, nc)))
		return -1;
	wb = nb;
	wcap = nc;
	return 0;
}

static size_t
utf8_put(char *o, uint32_t c)
{
	if (c < 0x80)
		return o[0] = (char)c, 1;
	if (c < 0x800)
		return o[0] = (char)(0xc0 | c >> 6),
		    o[1] = (char)(0x80 | (c & 0x3f)), 2;
	if (c < 0x10000)
		return o[0] = (char)(0xe0 | c >> 12),
		    o[1] = (char)(0x80 | (c >> 6 & 0x3f)),
		    o[2] = (char)(0x80 | (c & 0x3f)), 3;
	if (c > 0x10ffff)
		c = 0xfffd;
	return o[0] = (char)(0xf0 | c >> 18),
	    o[1] = (char)(0x80 | (c >> 12 & 0x3f)),
	    o[2] = (char)(0x80 | (c >> 6 & 0x3f)),
	    o[3] = (char)(0x80 | (c & 0x3f)), 4;
}

/* walk_row's current run of one semantic class */
static size_t wn;
static int wcls = -1, wfirst, wcont;

static void
run_flush(void)
{
	int k;

	while (wn && wb[wn - 1] == ' ')
		wn--;
	if (wn)
	{
		k = wcls == GHOSTTY_CELL_SEMANTIC_PROMPT ? RING_K_PROMPT
		    : wcls == GHOSTTY_CELL_SEMANTIC_INPUT ? RING_K_INPUT
		    : RING_K_OUTPUT;
		ring_append(xr, k, wfirst && wcont ? RING_F_CONT : 0, now32(),
		    wb, wn);
		xlast_kind = k;
		wfirst = 0;
	}
	wn = 0;
	wcls = -1;
}

/* The first max codepoints of a cell's need-long cluster into cps; on
** failure one U+FFFD, never the unwritten buffer. Returns the count. */
static size_t
graphemes_head(const GhosttyGridRef *g, size_t need, uint32_t *cps,
               size_t max)
{
	uint32_t *all = malloc(need * sizeof *all);
	size_t got = 0;

	if (all && ghostty_grid_ref_graphemes(g, all, need, &got) ==
	    GHOSTTY_SUCCESS && got >= max)
		memcpy(cps, all, max * sizeof *cps);
	else
	{
		cps[0] = 0xfffd;
		max = 1;
	}
	free(all);
	return max;
}

/* A row that carries marks, cell by cell: runs of prompt, input and output
** text become records, and each mark lands before the cell it was set at.
** Only rows with marks pay for this; the rest go through the formatter. */
static void
walk_row(size_t y)
{
	GhosttyPoint p;
	GhosttyGridRef g;
	GhosttyRow row;
	bool cont = false;
	uint32_t cps[32];
	size_t x;

	memset(&p, 0, sizeof p);
	p.tag = GHOSTTY_POINT_TAG_SCREEN;
	p.value.coordinate.y = (uint32_t)y;
	memset(&g, 0, sizeof g);
	g.size = sizeof g;
	wn = 0;
	wcls = -1;
	wfirst = 1;
	if (ghostty_terminal_grid_ref(the_term, p, &g) != GHOSTTY_SUCCESS)
	{
		marks_upto((size_t)-1, y, NULL);
		return;
	}
	if (ghostty_grid_ref_row(&g, &row) == GHOSTTY_SUCCESS)
		ghostty_row_get(row, GHOSTTY_ROW_DATA_WRAP_CONTINUATION, &cont);
	wcont = cont;
	for (x = 0; x < (size_t)cur_cols; x++)
	{
		GhosttyCell c;
		GhosttyCellWide w = GHOSTTY_CELL_WIDE_NARROW;
		GhosttyCellSemanticContent sem = GHOSTTY_CELL_SEMANTIC_OUTPUT;
		size_t n = 0, i;

		marks_upto(x, y, run_flush);
		g.x = (uint16_t)x;
		if (ghostty_grid_ref_cell(&g, &c) != GHOSTTY_SUCCESS)
			break;
		ghostty_cell_get(c, GHOSTTY_CELL_DATA_WIDE, &w);
		if (w == GHOSTTY_CELL_WIDE_SPACER_TAIL ||
		    w == GHOSTTY_CELL_WIDE_SPACER_HEAD)
			continue;
		ghostty_cell_get(c, GHOSTTY_CELL_DATA_SEMANTIC_CONTENT, &sem);
		/* shortcut: a cluster past 32 codepoints keeps its first 32.
		** OUT_OF_SPACE writes nothing into cps, only the size it needs,
		** so read it whole into a heap buffer first. */
		if (ghostty_grid_ref_graphemes(&g, cps, 32, &n) !=
		    GHOSTTY_SUCCESS)
			n = n > 32 ? graphemes_head(&g, n, cps, 32) : 0;
		if (n && wcls >= 0 && (int)sem != wcls)
			run_flush();
		if (wcls < 0)
			wcls = (int)sem;
		if (wb_room(wn, n ? n * 4 : 1) < 0)
			break;
		if (!n)
			wb[wn++] = ' ';
		for (i = 0; i < n; i++)
			wn += utf8_put(wb + wn, cps[i]);
	}
	run_flush();
	marks_upto((size_t)-1, y, NULL);
}

/* Rows y0..y1 without marks: one formatter pass, one record per logical
** line (soft wraps joined), blank lines dropped. */
static void
emit_seg(size_t y0, size_t y1)
{
	GhosttySelection sel;
	GhosttyPoint p;
	GhosttyGridRef g;
	GhosttyRow row;
	bool cont = false;
	char *buf;
	size_t len, i, s;
	int first = 1;

	memset(&p, 0, sizeof p);
	p.tag = GHOSTTY_POINT_TAG_SCREEN;
	p.value.coordinate.y = (uint32_t)y0;
	memset(&g, 0, sizeof g);
	g.size = sizeof g;
	if (ghostty_terminal_grid_ref(the_term, p, &g) == GHOSTTY_SUCCESS &&
	    ghostty_grid_ref_row(&g, &row) == GHOSTTY_SUCCESS)
		ghostty_row_get(row, GHOSTTY_ROW_DATA_WRAP_CONTINUATION, &cont);
	if (select_rows(y0, y1, &sel) < 0 ||
	    format_unwrap(&sel, &buf, &len) < 0)
		return;
	for (s = i = 0; i <= len; i++)
	{
		int k = first && cont ? xlast_kind : RING_K_OUTPUT;

		if (i < len && buf[i] != '\n')
			continue;
		if (i > s)
		{
			ring_append(xr, k, first && cont ? RING_F_CONT : 0,
			    now32(), buf + s, i - s);
			xlast_kind = k;
		}
		first = 0;
		s = i + 1;
	}
}

static void
emit_rows(size_t y0, size_t y1)
{
	size_t y = y0, mx, my;

	while (y <= y1)
	{
		size_t next = y1 + 1;

		while (npm)
		{
			if (ref_pos(pm[0].ref, &mx, &my) < 0)
				pm_drop(0);
			else if (my < y)
				pm_emit(0);	/* its row is behind us */
			else
			{
				if (my <= y1)
					next = my;
				break;
			}
		}
		if (next == y)
		{
			walk_row(y++);
			continue;
		}
		emit_seg(y, next - 1);
		y = next;
	}
}

/* After each feed slice: an erase empties the ring; new history rows are
** captured; the ref moves to the newest. */
static void
capture(void)
{
	size_t h, y;
	int i;

	if (xris)
	{
		xris = 0;
		ring_clear(xr);
		ghostty_tracked_grid_ref_free(xref);	/* NULL-safe */
		xref = NULL;	/* rows after the reset are all new */
		for (i = 0; i < npm; )
			if (!ghostty_tracked_grid_ref_has_value(pm[i].ref))
				pm_drop(i);
			else
				i++;
	}
	else if ((xref && !ghostty_tracked_grid_ref_has_value(xref)) ||
	    (xsent && !ghostty_tracked_grid_ref_has_value(xsent)))
	{
		/* Any clear, whatever screen is active (RIS on alt too). Unless
		** more rows are in history than one slice can have pushed since
		** an erase: then a prune took the row (the meter undercounted),
		** and the backstop writes a gap instead (§11.4). */
		if (hist_rows() > (slice_b > seq_max() ? slice_b : seq_max()) +
		    (size_t)cur_rows + 1)
		{
			ring_append(xr, RING_K_GAP, 0, now32(), "lost", 4);
			xreanchor = 1;	/* which rows are new is unknown */
		}
		else
			ring_clear(xr);
		ghostty_tracked_grid_ref_free(xref);
		xref = NULL;
		for (i = 0; i < npm; )
			if (!ghostty_tracked_grid_ref_has_value(pm[i].ref))
				pm_drop(i);
			else
				i++;
	}
	if (alt_active())
		return;		/* never set on, or read from, the alt screen */
	if (xreanchor || xresume)
	{
		anchor();
		if (xresume)
			xpaused = 0;
		xreanchor = xresume = 0;
		return;
	}
	h = hist_rows();
	if (xref)
	{
		/* At or past h: its row was pulled back on screen by a resize. */
		if (ref_pos(xref, NULL, &y) < 0 || y + 1 >= h)
			return;
		y++;
	}
	else if (!h)
		return;
	else
		y = 0;
	if (!xpaused)
		emit_rows(y, h - 1);
	anchor();
}

/* §14.1's row-cost meter: a conservative bound on the rows the next bytes
** can push into history, never a decision about what they mean. LF/VT/FF
** cost a row, text a row per cols bytes, and an escape sequence costs at its
** final byte, the only byte that runs it (probes on the vendored .a, report
** §6): REP (CSI n b) repeats the last cell up to 65535 times, so
** seq_max(); SU, ED 2 (at an OSC 133 prompt it pushes rows+1), SD, DECSTBM
** and ESC c/D/E/M a screen and a row; anything else nothing. Params are never
** parsed, so CSI 4294967295 S costs the same as CSI S. The slice ends before
** the byte that would pass the budget (floor: one byte). Cutting inside a
** sequence is harmless: nothing has run yet, and the slice that gets the
** final byte pays for it. xesc follows the parser across slices and feeds,
** the same way the mirror's parser does. */
enum { XE_GROUND, XE_ESC, XE_ESCINT, XE_CSI };
static int xesc;

/* The most rows one byte can push: a REP of a wide (2-cell) character. */
static size_t
seq_max(void)
{
	size_t rep = 131070 / (size_t)cur_cols + 2, scr = (size_t)cur_rows + 1;

	return rep > scr ? rep : scr;
}

static size_t
slice_len(const unsigned char *b, size_t n)
{
	size_t cost = 0, p = 0, i, scr = (size_t)cur_rows + 1;

	for (i = 0; i < n; i++)
	{
		unsigned char ch = b[i];
		size_t c = 0;
		int st = xesc;

		if (ch == 0x1b)
			st = XE_ESC;			/* starts or restarts */
		else if (ch == 0x18 || ch == 0x1a)
			st = XE_GROUND;			/* CAN, SUB cancel */
		else if (ch < 0x20)
			c = ch == '\n' || ch == '\v' || ch == '\f';	/* runs */
		else if (st == XE_CSI)
		{
			if (ch >= 0x40 && ch <= 0x7e)	/* the final byte */
			{
				c = ch == 'b' ? seq_max() :
				    ch == 'S' || ch == 'J' || ch == 'T' ||
				    ch == 'r' ? scr : 0;
				st = XE_GROUND;
			}				/* else ignored or a param */
		}
		else if (st == XE_ESC || st == XE_ESCINT)
		{
			if (ch == 0x7f)
				;			/* ignored */
			else if (st == XE_ESC && ch == '[')
				st = XE_CSI;
			else if (ch <= 0x2f)
				st = XE_ESCINT;		/* intermediate */
			else
			{
				c = st == XE_ESC && (ch == 'c' || ch == 'D' ||
				    ch == 'E' || ch == 'M') ? scr : 0;
				st = XE_GROUND;		/* text from 0x80 on */
				if (ch >= 0x80 && ++p >= (size_t)cur_cols)
				{
					c = 1;
					p = 0;
				}
			}
		}
		else if (++p >= (size_t)cur_cols)
		{
			c = 1;
			p = 0;
		}
		if (i && cost + c > slice_b)
			return i;
		cost += c;
		xesc = st;
	}
	return n;
}

void
dch_vt_capture(struct dch_ring *r)
{
	xr = r;
}

void
dch_vt_capture_rebase(void)
{
	if (!xon())
		return;
	if (alt_active())
		xreanchor = 1;
	else
		anchor();
}

void
dch_vt_capture_pause(int on)
{
	if (!xon())
		return;
	if (on && !xpaused)
	{
		while (npm)
			pm_emit(0);
		ring_append(xr, RING_K_GAP, 0, now32(), "agent", 5);
		xpaused = 1;
		xresume = 0;
	}
	else if (!on && xpaused)
	{
		if (!xresume)
			ring_touch(xr);	/* paused flips, no record: wake readers */
		xresume = 1;
		capture();
	}
}

int
dch_vt_capture_paused(void)
{
	return xpaused && !xresume;
}

void
dch_vt_input_seen(void)
{
	if (xb_open)
		xinput = 1;
}

void
dch_vt_capture_flush(void)
{
	while (xr && npm)
		pm_emit(0);
}

int
dch_vt_init(int cols, int rows, int scrollback)
{
	size_t zero = 0;

	clamp_grid(&cols, &rows);
	if (ghostty_terminal_new(NULL, &the_term, cols,
	    rows) != GHOSTTY_SUCCESS) {
		the_term = NULL;
		return -1;
	}
	cur_cols = cols;
	cur_rows = rows;
	xesc = XE_GROUND;	/* a fresh parser */
	sb_user = scrollback > 0 ? (size_t)scrollback : 0;
	xr_budget();
	/* Lines govern, so drop the byte limit (the library's 10 KB default
	** would undercut them). 0 = none: only a zero byte limit disables
	** scrollback, a line limit always keeps one page. */
	if (ghostty_terminal_set(the_term,
	    GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES,
	    sb_lines ? NULL : &zero) != GHOSTTY_SUCCESS ||
	    (sb_lines && set_scrollback_lines() < 0) ||
	    (xr && xr->cap && (ghostty_terminal_set(the_term,
	    GHOSTTY_TERMINAL_OPT_SEMANTIC_PROMPT,
	    (const void *)on_prompt) != GHOSTTY_SUCCESS ||
	    ghostty_terminal_set(the_term, GHOSTTY_TERMINAL_OPT_RESET,
	    (const void *)on_reset) != GHOSTTY_SUCCESS))) {
		ghostty_terminal_free(the_term);
		the_term = NULL;
		return -1;
	}
	if (ghostty_key_encoder_new(NULL, &the_encoder) != GHOSTTY_SUCCESS) {
		ghostty_terminal_free(the_term);
		the_term = NULL;
		return -1;
	}
	return 0;
}

void
dch_vt_free(void)
{
	if (!the_term)
		return;
	while (npm)
		pm_drop(0);
	ghostty_tracked_grid_ref_free(xref);
	xref = NULL;
	xpaused = xresume = xreanchor = xris = xb_open = xinput = 0;
	free(wb);
	wb = NULL;
	wcap = 0;
	free(ubuf);
	ubuf = NULL;
	ucap = 0;
	ghostty_key_encoder_free(the_encoder);
	ghostty_terminal_free(the_term);
	the_term = NULL;
}

void
dch_vt_feed(const unsigned char *buf, size_t len)
{
	if (!the_term)
		return;
	if (!xon())
		ghostty_terminal_vt_write(the_term, buf, len);
	else
		while (len)
		{
			size_t n = slice_len(buf, len);

			/* Only records the rows cannot vouch for (marks, gaps)
			** need it: an empty ring has nothing to clear. */
			if (!xref && xr->tail_seq != xr->head_seq &&
			    !alt_active())
				xsent = track(NULL, GHOSTTY_POINT_TAG_ACTIVE, 0,
				    0);
			ghostty_terminal_vt_write(the_term, buf, n);
			capture();
			ghostty_tracked_grid_ref_free(xsent);
			xsent = NULL;
			buf += n;
			len -= n;
		}
	gen++;
}

void
dch_vt_clear(void)
{
	static const unsigned char seq[] = "\033[H\033[2J\033[3J";
	uint32_t e;

	if (!the_term)
		return;
	e = xr ? xr->epoch : 0;
	dch_vt_feed(seq, sizeof seq - 1);
	/* On the alt screen ED 3 leaves the primary history alone: the ring
	** empties anyway (§14.3 H2). */
	if (xon() && xr->epoch == e)
		ring_clear(xr);
}

int
dch_vt_size(int *cols, int *rows, unsigned long *g)
{
	if (!the_term)
		return -1;
	*cols = cur_cols;
	*rows = cur_rows;
	*g = gen;
	return 0;
}

void
dch_vt_resize(int cols, int rows)
{
	GhosttyTrackedGridRef top = NULL;

	if (!the_term || cols <= 0 || rows <= 0)
		return;
	clamp_grid(&cols, &rows);
	/* The screen's old top row: a shrink pushes it and the rows below it
	** into history, never captured yet. */
	if (xon() && !alt_active())
		top = track(NULL, GHOSTTY_POINT_TAG_ACTIVE, 0, 0);
	if (ghostty_terminal_resize(the_term, cols, rows, CELL_W,
	    CELL_H) == GHOSTTY_SUCCESS) {
		cur_cols = cols;
		cur_rows = rows;
		gen++;
		xr_budget();
		/* A page holds more rows when narrower: re-size the slack. */
		if (sb_lines)
			set_scrollback_lines();
		if (xon() && alt_active())
			xreanchor = 1;
		else if (xon())
		{
			/* §14.3 H3/T9j: a reflow is not a clear. Capture what
			** the resize pushed into history, then re-anchor on the
			** newest history row. A ref pulled back on screen (the
			** window grew) stays: those rows are captured already. */
			size_t h = hist_rows(), start = xref ? h : 0, ty, ry;
			int on_screen = 0;

			if (xref && !ghostty_tracked_grid_ref_has_value(xref))
			{
				/* A reflow is never an erase: a prune took it */
				ring_append(xr, RING_K_GAP, 0, now32(), "lost", 4);
				ghostty_tracked_grid_ref_free(xref);
				xref = NULL;
				start = h;
			}
			if (ref_pos(top, NULL, &ty) == 0 && ty < start)
				start = ty;
			if (ref_pos(xref, NULL, &ry) == 0)
			{
				if (ry + 1 > start)
					start = ry + 1;
				on_screen = ry >= h;
			}
			if (!xpaused && start < h)
				emit_rows(start, h - 1);
			if (!on_screen)
				anchor();
		}
	}
	ghostty_tracked_grid_ref_free(top);
}

/* Length of the escape sequence at s (n bytes left): OSC up to BEL/ST, CSI
** up to its final byte, anything else two bytes. */
static size_t
esc_len(const char *s, size_t n)
{
	size_t i = 2;

	if (n < 2)
		return n;
	if (s[1] == ']') {
		for (; i < n; i++)
			if (s[i] == '\a')
				return i + 1;
			else if (s[i] == '\033' && i + 1 < n && s[i + 1] == '\\')
				return i + 2;
		return n;
	}
	if (s[1] == '[') {
		while (i < n && ((unsigned char)s[i] < 0x40 ||
		    (unsigned char)s[i] > 0x7e))
			i++;
		return i < n ? i + 1 : n;
	}
	return 2;
}

/* The VT formatter emits per-cell OSC 8 links but carries an open one across
** row breaks, so a line taken on its own (the recent-N tail-cut below, a
** reader showing the last rows) loses the open or keeps a stray close. Close
** the link before each line break and reopen it before the next printed byte,
** dropping the formatter's own close when it lands there. Writes to o (NULL:
** count only), returns the length. */
#define OSC8_CLOSE "\033]8;;\033\\"
static size_t
osc8_per_line(const char *s, size_t n, char *o)
{
	const char *open = NULL;
	size_t i = 0, k = 0, l, olen = 0;
	int pending = 0;

#define PUT(p, len) do { if (o) memcpy(o + k, (p), (len)); k += (len); } while (0)
	while (i < n) {
		if (s[i] == '\033') {
			l = esc_len(s + i, n - i);
			if (l > 5 && memcmp(s + i, "\033]8;", 4) == 0) {
				const char *semi = memchr(s + i + 4, ';', l - 4);
				size_t term = s[i + l - 1] == '\a' ? 1 : 2;
				int close = !semi || semi + 1 == s + i + l - term;

				if (!(close && pending))
					PUT(s + i, l);
				open = close ? NULL : s + i;
				olen = l;
				pending = 0;
			} else
				PUT(s + i, l);
			i += l;
			continue;
		}
		if (open && (s[i] == '\n' ||
		    (s[i] == '\r' && i + 1 < n && s[i + 1] == '\n'))) {
			if (!pending)
				PUT(OSC8_CLOSE, sizeof OSC8_CLOSE - 1);
			pending = 1;
		} else if (pending) {
			PUT(open, olen);
			pending = 0;
		}
		PUT(s + i, 1);
		i++;
	}
	if (open && !pending)
		PUT(OSC8_CLOSE, sizeof OSC8_CLOSE - 1);
#undef PUT
	return k;
}

/* Whole grid rows y0..y1 (screen coordinates, history included) as sel. */
static int
select_rows(size_t y0, size_t y1, GhosttySelection *sel)
{
	GhosttyPoint p_start, p_end;
	GhosttyGridRef ref_start, ref_end;

	memset(&p_start, 0, sizeof(p_start));
	p_start.tag = GHOSTTY_POINT_TAG_SCREEN;
	p_start.value.coordinate.x = 0;
	p_start.value.coordinate.y = y0;
	memset(&p_end, 0, sizeof(p_end));
	p_end.tag = GHOSTTY_POINT_TAG_SCREEN;
	p_end.value.coordinate.x = cur_cols > 0 ? cur_cols - 1 : 0;
	p_end.value.coordinate.y = y1;
	if (ghostty_terminal_grid_ref(the_term, p_start,
	    &ref_start) != GHOSTTY_SUCCESS ||
	    ghostty_terminal_grid_ref(the_term, p_end,
	    &ref_end) != GHOSTTY_SUCCESS)
		return -1;
	memset(sel, 0, sizeof(*sel));
	sel->size = sizeof(*sel);
	sel->start = ref_start;
	sel->end = ref_end;
	return 0;
}

/* Format sel (NULL: the whole screen and its history) into a malloc'd,
** NUL-terminated *out; *outlen excludes the NUL. */
static int
fmt_new(int format, const GhosttySelection *sel, GhosttyFormatter *fmt)
{
	GhosttyFormatterTerminalOptions opts;

	memset(&opts, 0, sizeof(opts));
	opts.size = sizeof(opts);
	opts.emit = format == DCH_VT_FMT_ANSI ? GHOSTTY_FORMATTER_FORMAT_VT
	    : GHOSTTY_FORMATTER_FORMAT_PLAIN;
	opts.trim = 1;
	opts.unwrap = format == FMT_UNWRAP;
	opts.extra.size = sizeof(opts.extra);
	opts.extra.screen.size = sizeof(opts.extra.screen);
	opts.selection = sel;
	return ghostty_formatter_terminal_new(NULL, fmt, the_term,
	    opts) == GHOSTTY_SUCCESS ? 0 : -1;
}

/* Capture's formatter run: one pass into a reused buffer, a second only
** when the buffer has to grow (format_sel always makes two). *out stays
** valid until the next call. */

static int
format_unwrap(const GhosttySelection *sel, char **out, size_t *outlen)
{
	GhosttyFormatter fmt;
	GhosttyResult r;
	size_t need = 0;

	if (fmt_new(FMT_UNWRAP, sel, &fmt) < 0)
		return -1;
	r = ghostty_formatter_format_buf(fmt, (uint8_t *)ubuf, ucap, &need);
	if (r == GHOSTTY_OUT_OF_SPACE && need)
	{
		char *nb = realloc(ubuf, need);

		if (nb)
		{
			ubuf = nb;
			ucap = need;
			r = ghostty_formatter_format_buf(fmt, (uint8_t *)ubuf,
			    ucap, &need);
		}
	}
	ghostty_formatter_free(fmt);
	if (r != GHOSTTY_SUCCESS && !(r == GHOSTTY_OUT_OF_SPACE && !need))
		return -1;
	*out = ubuf;
	*outlen = r == GHOSTTY_SUCCESS ? need : 0;
	return 0;
}

static int
format_sel(int format, const GhosttySelection *sel, char **out,
           size_t *outlen)
{
	GhosttyFormatter fmt;
	size_t need, written;
	char *buf;

	if (fmt_new(format, sel, &fmt) < 0)
		return -1;

	need = 0;
	if (ghostty_formatter_format_buf(fmt, NULL, 0,
	    &need) != GHOSTTY_OUT_OF_SPACE) {
		/* empty snapshot is legal (blank screen) */
		ghostty_formatter_free(fmt);
		*out = malloc(1);
		if (!*out)
			return -1;
		(*out)[0] = '\0';
		*outlen = 0;
		return 0;
	}
	buf = malloc(need + 1);
	if (!buf) {
		ghostty_formatter_free(fmt);
		return -1;
	}
	written = 0;
	if (ghostty_formatter_format_buf(fmt, (uint8_t *)buf, need,
	    &written) != GHOSTTY_SUCCESS) {
		free(buf);
		ghostty_formatter_free(fmt);
		return -1;
	}
	ghostty_formatter_free(fmt);
	buf[written] = '\0';

	/* Fast path: no link anywhere costs one scan. */
	if (format == DCH_VT_FMT_ANSI && strstr(buf, "\033]8;")) {
		size_t n = osc8_per_line(buf, written, NULL);
		char *nb = malloc(n + 1);

		if (!nb) {
			free(buf);
			return -1;
		}
		osc8_per_line(buf, written, nb);
		nb[n] = '\0';
		free(buf);
		buf = nb;
		written = n;
	}
	*out = buf;
	*outlen = written;
	return 0;
}

int
dch_vt_alt(void)
{
	return the_term && alt_active();
}

int
dch_vt_snapshot(int format, int lines, char **out, size_t *outlen)
{
	GhosttySelection sel;
	size_t total, first, written, y;
	char *buf;

	*out = NULL;
	*outlen = 0;
	if (!the_term)
		return -1;

	if (lines > 0) {
		/* Recent-N: the bottom of the grid is usually blank viewport
		** rows, so selecting the last N grid rows yields nothing.
		** Format the whole history (NULL selection trims trailing
		** blanks) and tail-cut N text lines afterwards. */
		size_t h = hist_rows(), vis = user_history(h);

		if (vis == h) {
			if (format_sel(format, NULL, &buf, &written) < 0)
				return -1;
		} else {
			/* The ring keeps capture-only history above what the
			** user may see: start below it, and drop the blank
			** screen rows the explicit selection brings along. */
			if (ghostty_terminal_get(the_term,
			    GHOSTTY_TERMINAL_DATA_TOTAL_ROWS,
			    &total) != GHOSTTY_SUCCESS || total == 0 ||
			    select_rows(h - vis, total - 1, &sel) < 0 ||
			    format_sel(format, &sel, &buf, &written) < 0)
				return -1;
			while (written > 1 && buf[written - 1] == '\n' &&
			    buf[written - 2] == '\n')
				buf[--written] = '\0';
		}
	} else {
		/* Visible screen: the bottom cur_rows grid rows. */
		if (ghostty_terminal_get(the_term,
		    GHOSTTY_TERMINAL_DATA_TOTAL_ROWS,
		    &total) != GHOSTTY_SUCCESS || total == 0)
			return -1;
		first = total > (size_t)cur_rows ? total - (size_t)cur_rows : 0;
		/* X-ray's rows (#171): not the ones a grow pulled back on
		** screen, in the ring already */
		if (format == FMT_UNWRAP && ref_pos(xref, NULL, &y) == 0 &&
		    y >= first)
			first = y + 1;
		if (first >= total) {
			if (!(buf = malloc(1)))
				return -1;
			buf[0] = '\0';
			written = 0;
		} else if (select_rows(first, total - 1, &sel) < 0 ||
		    format_sel(format, &sel, &buf, &written) < 0)
			return -1;
	}

	if (lines > 0 && written > 0) {
		/* Keep only the last `lines` text lines. ANSI note: SGR state
		** set before the cut is lost; each kept line still carries its
		** own leading attributes from the formatter, so in practice
		** colors survive. */
		size_t i = written;
		int seen = 0;

		if (buf[i - 1] == '\n')
			i--; /* trailing newline isn't a line boundary */
		while (i > 0) {
			if (buf[i - 1] == '\n' && ++seen >= lines)
				break;
			i--;
		}
		if (i > 0) {
			memmove(buf, buf + i, written - i + 1);
			written -= i;
		}
	}

	*out = buf;
	*outlen = written;
	return 0;
}

/* The last `lines` grid rows above the visible screen, ANSI, one text line
** per row (soft wraps stay breaks: the client re-wraps nothing). Empty when
** there is no history, as on the alternate screen. */
int
dch_vt_history(int lines, char **out, size_t *outlen)
{
	GhosttySelection sel;
	size_t total, first;

	*out = NULL;
	*outlen = 0;
	if (!the_term || lines < 1 || ghostty_terminal_get(the_term,
	    GHOSTTY_TERMINAL_DATA_TOTAL_ROWS, &total) != GHOSTTY_SUCCESS)
		return -1;
	first = total > (size_t)cur_rows ? total - (size_t)cur_rows : 0;
	if ((size_t)lines > user_history(first))
		lines = (int)user_history(first);
	if (!first || !lines) {
		*out = calloc(1, 1);
		return *out ? 0 : -1;
	}
	if (select_rows(first > (size_t)lines ? first - (size_t)lines : 0,
	    first - 1, &sel) < 0)
		return -1;
	return format_sel(DCH_VT_FMT_ANSI, &sel, out, outlen);
}

int
dch_vt_cursor(int *row, int *col, int *visible, int *wrap)
{
	uint16_t x, y;
	bool vis, pw;

	if (!the_term)
		return -1;
	if (ghostty_terminal_get(the_term, GHOSTTY_TERMINAL_DATA_CURSOR_X,
	    &x) != GHOSTTY_SUCCESS ||
	    ghostty_terminal_get(the_term, GHOSTTY_TERMINAL_DATA_CURSOR_Y,
	    &y) != GHOSTTY_SUCCESS ||
	    ghostty_terminal_get(the_term, GHOSTTY_TERMINAL_DATA_CURSOR_VISIBLE,
	    &vis) != GHOSTTY_SUCCESS ||
	    ghostty_terminal_get(the_term,
	    GHOSTTY_TERMINAL_DATA_CURSOR_PENDING_WRAP,
	    &pw) != GHOSTTY_SUCCESS)
		return -1;
	*row = (int)y;
	*col = (int)x;
	*visible = vis ? 1 : 0;
	*wrap = pw ? 1 : 0;
	return 0;
}

/* --keys token table: name, physical key, utf8 byte (0 = none) */
static const struct keyname {
	const char *name;
	GhosttyKey key;
	char utf8;
} keynames[] = {
	{"enter",     GHOSTTY_KEY_ENTER,      '\r'},
	{"escape",    GHOSTTY_KEY_ESCAPE,     0x1b},
	{"esc",       GHOSTTY_KEY_ESCAPE,     0x1b},
	{"tab",       GHOSTTY_KEY_TAB,        '\t'},
	{"space",     GHOSTTY_KEY_SPACE,      ' '},
	{"backspace", GHOSTTY_KEY_BACKSPACE,  0x7f},
	{"delete",    GHOSTTY_KEY_DELETE,     0},
	{"insert",    GHOSTTY_KEY_INSERT,     0},
	{"up",        GHOSTTY_KEY_ARROW_UP,   0},
	{"down",      GHOSTTY_KEY_ARROW_DOWN, 0},
	{"left",      GHOSTTY_KEY_ARROW_LEFT, 0},
	{"right",     GHOSTTY_KEY_ARROW_RIGHT,0},
	{"home",      GHOSTTY_KEY_HOME,       0},
	{"end",       GHOSTTY_KEY_END,        0},
	{"pgup",      GHOSTTY_KEY_PAGE_UP,    0},
	{"pgdn",      GHOSTTY_KEY_PAGE_DOWN,  0},
	{"f1",  GHOSTTY_KEY_F1,  0}, {"f2",  GHOSTTY_KEY_F2,  0},
	{"f3",  GHOSTTY_KEY_F3,  0}, {"f4",  GHOSTTY_KEY_F4,  0},
	{"f5",  GHOSTTY_KEY_F5,  0}, {"f6",  GHOSTTY_KEY_F6,  0},
	{"f7",  GHOSTTY_KEY_F7,  0}, {"f8",  GHOSTTY_KEY_F8,  0},
	{"f9",  GHOSTTY_KEY_F9,  0}, {"f10", GHOSTTY_KEY_F10, 0},
	{"f11", GHOSTTY_KEY_F11, 0}, {"f12", GHOSTTY_KEY_F12, 0},
};

/* punctuation on a US layout; --send exists for arbitrary text */
static const struct keyname punct[] = {
	{"-",  GHOSTTY_KEY_MINUS,         '-'},
	{"=",  GHOSTTY_KEY_EQUAL,         '='},
	{"[",  GHOSTTY_KEY_BRACKET_LEFT,  '['},
	{"]",  GHOSTTY_KEY_BRACKET_RIGHT, ']'},
	{"\\", GHOSTTY_KEY_BACKSLASH,     '\\'},
	{";",  GHOSTTY_KEY_SEMICOLON,     ';'},
	{"'",  GHOSTTY_KEY_QUOTE,         '\''},
	{",",  GHOSTTY_KEY_COMMA,         ','},
	{".",  GHOSTTY_KEY_PERIOD,        '.'},
	{"/",  GHOSTTY_KEY_SLASH,         '/'},
	{"`",  GHOSTTY_KEY_BACKQUOTE,     '`'},
};

/* US-layout shift pairs: glyph <-> physical key + base char. Lets both
** `--keys :` and `--keys shift+;` produce ":". ponytail: US layout only —
** the mirror has no idea what layout the human's keyboard uses anyway. */
static const struct shiftpair {
	char glyph, base;
	GhosttyKey key;
} shifted[] = {
	{'!', '1', GHOSTTY_KEY_DIGIT_1}, {'@', '2', GHOSTTY_KEY_DIGIT_2},
	{'#', '3', GHOSTTY_KEY_DIGIT_3}, {'$', '4', GHOSTTY_KEY_DIGIT_4},
	{'%', '5', GHOSTTY_KEY_DIGIT_5}, {'^', '6', GHOSTTY_KEY_DIGIT_6},
	{'&', '7', GHOSTTY_KEY_DIGIT_7}, {'*', '8', GHOSTTY_KEY_DIGIT_8},
	{'(', '9', GHOSTTY_KEY_DIGIT_9}, {')', '0', GHOSTTY_KEY_DIGIT_0},
	{'_', '-', GHOSTTY_KEY_MINUS},   {'+', '=', GHOSTTY_KEY_EQUAL},
	{'{', '[', GHOSTTY_KEY_BRACKET_LEFT},
	{'}', ']', GHOSTTY_KEY_BRACKET_RIGHT},
	{'|', '\\', GHOSTTY_KEY_BACKSLASH},
	{':', ';', GHOSTTY_KEY_SEMICOLON}, {'"', '\'', GHOSTTY_KEY_QUOTE},
	{'<', ',', GHOSTTY_KEY_COMMA},     {'>', '.', GHOSTTY_KEY_PERIOD},
	{'?', '/', GHOSTTY_KEY_SLASH},     {'~', '`', GHOSTTY_KEY_BACKQUOTE},
};

/*
 * Encode one token ("ctrl+shift+f5", "q", "enter") into ev + encoder
 * output appended at dst (capacity cap, current fill *fill).
 * Returns 0 ok, -1 bad token or overflow.
 */
static int
encode_token(GhosttyKeyEvent ev, const char *tok, size_t toklen,
	     char *dst, size_t cap, size_t *fill)
{
	GhosttyMods mods = 0;
	char utf8 = 0;
	char text; /* set_utf8 stores the POINTER; must outlive encode below */
	GhosttyKey key = GHOSTTY_KEY_UNIDENTIFIED;
	size_t i, n;

	/* strip modifier prefixes */
	for (;;) {
		if (toklen > 5 && strncasecmp(tok, "ctrl+", 5) == 0) {
			mods |= GHOSTTY_MODS_CTRL; tok += 5; toklen -= 5;
		} else if (toklen > 4 && strncasecmp(tok, "alt+", 4) == 0) {
			mods |= GHOSTTY_MODS_ALT; tok += 4; toklen -= 4;
		} else if (toklen > 6 && strncasecmp(tok, "shift+", 6) == 0) {
			mods |= GHOSTTY_MODS_SHIFT; tok += 6; toklen -= 6;
		} else
			break;
	}

	if (toklen == 1) {
		char c = tok[0];
		if (c >= 'A' && c <= 'Z') {
			mods |= GHOSTTY_MODS_SHIFT;
			c = c - 'A' + 'a';
		}
		if (c >= 'a' && c <= 'z') {
			key = GHOSTTY_KEY_A + (c - 'a');
			utf8 = c;
		} else if (c >= '0' && c <= '9') {
			key = GHOSTTY_KEY_DIGIT_0 + (c - '0');
			utf8 = c;
		} else {
			for (i = 0; i < sizeof(punct) / sizeof(punct[0]); i++)
				if (punct[i].name[0] == c) {
					key = punct[i].key;
					utf8 = punct[i].utf8;
					break;
				}
			/* shifted glyph typed directly (":", "?", "{"...) */
			if (key == GHOSTTY_KEY_UNIDENTIFIED)
				for (i = 0; i < sizeof(shifted) /
				     sizeof(shifted[0]); i++)
					if (shifted[i].glyph == c) {
						key = shifted[i].key;
						utf8 = shifted[i].base;
						mods |= GHOSTTY_MODS_SHIFT;
						break;
					}
		}
	}
	if (key == GHOSTTY_KEY_UNIDENTIFIED) {
		for (i = 0; i < sizeof(keynames) / sizeof(keynames[0]); i++)
			if (strlen(keynames[i].name) == toklen &&
			    strncasecmp(keynames[i].name, tok, toklen) == 0) {
				key = keynames[i].key;
				utf8 = keynames[i].utf8;
				break;
			}
	}
	if (key == GHOSTTY_KEY_UNIDENTIFIED)
		return -1;

	ghostty_key_event_set_action(ev, GHOSTTY_KEY_ACTION_PRESS);
	ghostty_key_event_set_key(ev, key);
	ghostty_key_event_set_mods(ev, mods);
	/* ctrl/alt chords: mods are not consumed into the text */
	ghostty_key_event_set_consumed_mods(ev,
	    mods & GHOSTTY_MODS_SHIFT);
	if (utf8 && !(mods & (GHOSTTY_MODS_CTRL | GHOSTTY_MODS_ALT))) {
		/* SHIFT resolves to the glyph the app actually receives;
		** the unshifted codepoint below stays the base char. */
		text = utf8;
		if (mods & GHOSTTY_MODS_SHIFT) {
			if (utf8 >= 'a' && utf8 <= 'z')
				text = utf8 - 'a' + 'A';
			else
				for (i = 0; i < sizeof(shifted) /
				     sizeof(shifted[0]); i++)
					if (shifted[i].base == utf8) {
						text = shifted[i].glyph;
						break;
					}
		}
		ghostty_key_event_set_utf8(ev, &text, 1);
	} else
		ghostty_key_event_set_utf8(ev, NULL, 0);
	/* kitty CSI-u needs the unshifted codepoint (spike finding #4) */
	ghostty_key_event_set_unshifted_codepoint(ev,
	    utf8 ? (uint32_t)(unsigned char)utf8 : 0);

	n = 0;
	if (ghostty_key_encoder_encode(the_encoder, ev, dst + *fill,
	    cap - *fill, &n) != GHOSTTY_SUCCESS)
		return -1;
	*fill += n;
	return 0;
}

int
dch_vt_encode_keys(const char *spec, char **out, size_t *outlen)
{
	GhosttyKeyEvent ev;
	const char *p = spec, *tok;
	char *buf;
	size_t cap, fill = 0;
	int rc = 0;

	*out = NULL;
	*outlen = 0;
	if (!the_term || !spec)
		return -1;

	/* worst case ~16 bytes per encoded key; tokens >= 1 char each */
	cap = strlen(spec) * 16 + 64;
	buf = malloc(cap);
	if (!buf)
		return -1;

	/* modes (kitty, DECCKM, ...) may have changed since last call */
	ghostty_key_encoder_setopt_from_terminal(the_encoder, the_term);

	if (ghostty_key_event_new(NULL, &ev) != GHOSTTY_SUCCESS) {
		free(buf);
		return -1;
	}
	while (*p) {
		while (*p && isspace((unsigned char)*p))
			p++;
		if (!*p)
			break;
		tok = p;
		while (*p && !isspace((unsigned char)*p))
			p++;
		if (encode_token(ev, tok, p - tok, buf, cap, &fill) < 0) {
			rc = -1;
			break;
		}
	}
	ghostty_key_event_free(ev);

	if (rc < 0 || fill == 0) {
		free(buf);
		return -1;
	}
	*out = buf;
	*outlen = fill;
	return 0;
}

void
dch_vt_buf_free(char *buf)
{
	free(buf);
}
