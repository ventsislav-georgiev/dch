/*
 * vt_stub.c - dch-lite build: no terminal mirror.
 * Same symbols as vt.c; dch_vt_enabled() == 0 tells callers the truth.
 */
#include <stdlib.h>
#include "vt.h"

int
dch_vt_enabled(void)
{
	return 0;
}

int
dch_vt_init(int cols, int rows, int scrollback)
{
	(void)cols;
	(void)rows;
	(void)scrollback;
	return -1;
}

void
dch_vt_free(void)
{
}

void
dch_vt_feed(const unsigned char *buf, size_t len)
{
	(void)buf;
	(void)len;
}

void
dch_vt_resize(int cols, int rows)
{
	(void)cols;
	(void)rows;
}

int
dch_vt_size(int *cols, int *rows, unsigned long *g)
{
	(void)cols;
	(void)rows;
	(void)g;
	return -1;
}

int
dch_vt_alt(void)
{
	return 0;
}

int
dch_vt_snapshot(int format, int lines, char **out, size_t *outlen)
{
	(void)format;
	(void)lines;
	*out = NULL;
	*outlen = 0;
	return -1;
}

int
dch_vt_history(int lines, char **out, size_t *outlen)
{
	(void)lines;
	*out = NULL;
	*outlen = 0;
	return -1;
}

int
dch_vt_cursor(int *row, int *col, int *visible, int *wrap)
{
	(void)row;
	(void)col;
	(void)visible;
	(void)wrap;
	return -1;
}

int
dch_vt_encode_keys(const char *spec, char **out, size_t *outlen)
{
	(void)spec;
	*out = NULL;
	*outlen = 0;
	return -1;
}

void
dch_vt_buf_free(char *buf)
{
	free(buf);
}

/* X-ray capture: no mirror, no rows (ring stays empty). */
void
dch_vt_capture(struct dch_ring *r)
{
	(void)r;
}

void
dch_vt_capture_rebase(void)
{
}

void
dch_vt_capture_pause(int on)
{
	(void)on;
}

int
dch_vt_capture_paused(void)
{
	return 0;
}

void
dch_vt_input_seen(void)
{
}

void
dch_vt_capture_flush(void)
{
}

void
dch_vt_clear(void)
{
}
