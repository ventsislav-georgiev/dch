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
dch_vt_snapshot(int format, int lines, char **out, size_t *outlen)
{
	(void)format;
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
