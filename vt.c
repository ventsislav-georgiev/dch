/*
 * vt.c - terminal mirror implementation backed by libghostty-vt.
 * See vt.h for the interface contract.
 */
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <ghostty/vt.h>
#include "vt.h"

static GhosttyTerminal the_term; /* NULL when disabled/failed */
static GhosttyKeyEncoder the_encoder;
static int cur_cols, cur_rows;
static size_t sb_lines; /* DCH_SCROLLBACK; 0 = no scrollback */
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
static int
set_scrollback_lines(void)
{
	size_t n = sb_lines + 49152 / ((size_t)cur_cols + 1);

	return ghostty_terminal_set(the_term,
	    GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES, &n) == GHOSTTY_SUCCESS
	    ? 0 : -1;
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
	sb_lines = scrollback > 0 ? (size_t)scrollback : 0;
	/* Lines govern, so drop the byte limit (the library's 10 KB default
	** would undercut them). 0 = none: only a zero byte limit disables
	** scrollback, a line limit always keeps one page. */
	if (ghostty_terminal_set(the_term,
	    GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES,
	    sb_lines ? NULL : &zero) != GHOSTTY_SUCCESS ||
	    (sb_lines && set_scrollback_lines() < 0)) {
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
	ghostty_key_encoder_free(the_encoder);
	ghostty_terminal_free(the_term);
	the_term = NULL;
}

void
dch_vt_feed(const unsigned char *buf, size_t len)
{
	if (the_term) {
		ghostty_terminal_vt_write(the_term, buf, len);
		gen++;
	}
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
	if (!the_term || cols <= 0 || rows <= 0)
		return;
	clamp_grid(&cols, &rows);
	if (ghostty_terminal_resize(the_term, cols, rows, CELL_W,
	    CELL_H) == GHOSTTY_SUCCESS) {
		cur_cols = cols;
		cur_rows = rows;
		gen++;
		/* A page holds more rows when narrower: re-size the slack. */
		if (sb_lines)
			set_scrollback_lines();
	}
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
format_sel(int format, const GhosttySelection *sel, char **out,
           size_t *outlen)
{
	GhosttyFormatterTerminalOptions opts;
	GhosttyFormatter fmt;
	size_t need, written;
	char *buf;

	memset(&opts, 0, sizeof(opts));
	opts.size = sizeof(opts);
	opts.emit = format == DCH_VT_FMT_ANSI ? GHOSTTY_FORMATTER_FORMAT_VT
	    : GHOSTTY_FORMATTER_FORMAT_PLAIN;
	opts.trim = 1;
	opts.extra.size = sizeof(opts.extra);
	opts.extra.screen.size = sizeof(opts.extra.screen);
	opts.selection = sel;

	if (ghostty_formatter_terminal_new(NULL, &fmt, the_term,
	    opts) != GHOSTTY_SUCCESS)
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
dch_vt_snapshot(int format, int lines, char **out, size_t *outlen)
{
	GhosttySelection sel;
	size_t total, first, written;
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
		if (format_sel(format, NULL, &buf, &written) < 0)
			return -1;
	} else {
		/* Visible screen: the bottom cur_rows grid rows. */
		if (ghostty_terminal_get(the_term,
		    GHOSTTY_TERMINAL_DATA_TOTAL_ROWS,
		    &total) != GHOSTTY_SUCCESS || total == 0)
			return -1;
		first = total > (size_t)cur_rows ? total - (size_t)cur_rows : 0;
		if (select_rows(first, total - 1, &sel) < 0 ||
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
	if (!first) {
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
