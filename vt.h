/*
 * vt.h - terminal mirror shim around libghostty-vt.
 *
 * The master process keeps a headless terminal emulator fed with every
 * byte written to the pty, so control clients can read the rendered
 * screen (--read), wait for output (--wait), and send encoded keys
 * (--keys) without attaching.
 *
 * Boundary rule: this interface uses plain buffers and scalars only —
 * no libghostty types leak out. The lite build (vt_stub.c) provides the
 * same symbols with dch_vt_enabled() == 0 and no-op bodies, so callers
 * never need #ifdefs.
 *
 * Buffers returned via *out are plain malloc() memory; free with
 * dch_vt_buf_free() (== free()). No ghostty-allocated pointer escapes.
 */
#ifndef DCH_VT_H
#define DCH_VT_H

#include <stddef.h>

/* Snapshot formats */
#define DCH_VT_FMT_PLAIN 0
#define DCH_VT_FMT_ANSI  1

/* Returns 1 when a live mirror exists (init succeeded, not disabled). */
int dch_vt_enabled(void);

/*
 * Create the mirror. scrollback is in rows (0 disables scrollback).
 * Returns 0 on success, -1 on failure; failure latches the shim off
 * (all other calls become no-ops), the session keeps running.
 */
int dch_vt_init(int cols, int rows, int scrollback);

void dch_vt_free(void);

/* Feed raw pty output bytes into the mirror. */
void dch_vt_feed(const unsigned char *buf, size_t len);

/* Resize the mirror grid (call adjacent to TIOCSWINSZ). */
void dch_vt_resize(int cols, int rows);

/*
 * Mirror grid size (after clamping) and a change generation that moves on
 * every feed and resize, so an unchanged value means the screen cannot have
 * changed. Returns 0, or -1 when disabled (outputs untouched).
 */
int dch_vt_size(int *cols, int *rows, unsigned long *gen);

/*
 * Render the screen. lines == 0: visible screen only; lines > 0: the
 * last `lines` rows including scrollback. format is DCH_VT_FMT_*.
 * On success *out is a malloc'd NUL-terminated buffer (*outlen excludes
 * the NUL) and 0 is returned; -1 on failure or when disabled.
 */
int dch_vt_snapshot(int format, int lines, char **out, size_t *outlen);

/* The last `lines` grid rows of scrollback above the visible screen, ANSI,
** one line per row ("" when there are none). For the attach replay (#101). */
int dch_vt_history(int lines, char **out, size_t *outlen);

/*
 * Cursor position in the mirror, as of right now. row/col are 0-based
 * and relative to the ACTIVE AREA (the visible screen a dch_vt_snapshot()
 * with lines == 0 renders), visible is DEC mode 25, wrap is the pending
 * soft-wrap flag: col is still the LAST column, and the next printed cell
 * lands on the next row. Returns 0 on success, -1 on failure or when
 * disabled; on failure the outputs are untouched.
 */
int dch_vt_cursor(int *row, int *col, int *visible, int *wrap);

/*
 * Encode a key chord specification into the byte sequence the
 * foreground program expects (respects the mirror's current keyboard
 * modes: kitty, application cursor keys, ...).
 *
 * spec is whitespace-separated tokens: a single character ("q", "/"),
 * a named key (enter, esc, escape, tab, space, backspace, delete,
 * insert, up, down, left, right, home, end, pgup, pgdn, f1..f12), or
 * either with modifier prefixes ("ctrl+c", "alt+enter",
 * "ctrl+shift+f5").
 *
 * On success *out is malloc'd (*outlen bytes, not NUL-terminated) and
 * 0 is returned. Unknown token or disabled shim: -1.
 */
int dch_vt_encode_keys(const char *spec, char **out, size_t *outlen);

/* Free a buffer returned by dch_vt_snapshot / dch_vt_encode_keys. */
void dch_vt_buf_free(char *buf);

/*
 * X-ray capture (#119). With a ring set (before dch_vt_init), every row the
 * PRIMARY screen scrolls into history is appended to it, ANSI-free, after
 * each feed slice; OSC 133 marks are placed at their row. Any erase of the
 * history (ED 3 in any spelling, RIS) empties the ring. The lite build's
 * stubs do nothing, so a ring there stays empty.
 */
struct dch_ring;
void dch_vt_capture(struct dch_ring *r);
/* The rows in history now count as captured: after a --restart re-feed. */
void dch_vt_capture_rebase(void);
/* on: an agent runs in the shell; rows are skipped and a gap is written.
** off: resume (rows printed while paused never enter). */
void dch_vt_capture_pause(int on);
int dch_vt_capture_paused(void);
/* The master forwarded client input to the pty (OSC 133 B..C fact). */
void dch_vt_input_seen(void);
/* Append marks still waiting for their row (before a --restart). */
void dch_vt_capture_flush(void);
/* Clear the mirror's history like ⌘K (ESC[H ESC[2J ESC[3J into the mirror
** only, never the pty) and empty the ring whatever the active screen. */
void dch_vt_clear(void);

#endif /* DCH_VT_H */
