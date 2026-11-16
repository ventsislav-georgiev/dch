/*
 * xcmd.h - X-ray's command source (#321): the user's own `tasks_command` /
 * `memory_command` / `define_command` (#405) from the dch config, run
 * read-only, its stdout read as JSON lines (define: or one plain-text first
 * line) and turned into the contract's row lines, escapes and controls out.
 *
 * Client-only, like xray.c: the querying process runs it, the master links
 * none of it. The command line comes only from the user's own config, never
 * from a peer; dch splits it (no shell). define_command's last argv word is
 * the ref, which a transcript or (through dch serve) a peer chose:
 * untrusted, never run when it starts with '-'.
 */
#ifndef DCH_XCMD_H
#define DCH_XCMD_H

#include <stddef.h>

#include "xray.h"

#define XCMD_WALL_MS  5000		/* the whole run, then the group ends */
#define XCMD_GRACE_MS 250		/* SIGTERM to SIGKILL */
#define XCMD_OUT_MAX  (32u << 20)	/* stdout bytes read */
#define XCMD_ROWS_MAX 50000		/* rows kept: the first */
#define XCMD_LINE_MAX (64u << 10)	/* a stdout line and a row line */
#define XCMD_BODY_MAX (16u << 10)	/* a memory row's body */
#define XCMD_URL_MAX  2048		/* a url or path field */
#define XCMD_ARGS_MAX 64		/* argv words */
#define XCMD_ERR_MAX  (XR_TITLE_MAX * 4 + 1)	/* stderr's first line */
#define XCMD_WARN_MAX 512
/* define_command (#405): smaller output limits, one card's few definitions */
/* 5 s as tasks: a just-written script's first exec can wait ~3 s on
** endpoint security */
#define XCMD_DEF_WALL_MS 5000
#define XCMD_DEF_OUT_MAX (1u << 20)
#define XCMD_DEF_BODY    2048		/* a definition's body: bytes, */
#define XCMD_DEF_LINES   20		/* and lines */

enum xcmd_kind { XCMD_TASK, XCMD_MEMORY, XCMD_DEFINE };

/* What the query knows about the session; NULL reads as "". command is the
** config value (dch_config_get("tasks_command", ...)), unsplit. XCMD_DEFINE:
** ref (as written), the last argv word and DCH_REF; its shape DCH_REF_SHAPE;
** key, X-ray's normalized text, DCH_REF_KEY. Not run when ref or key starts
** with '-' (an option to the user's program). */
struct xcmd_ctx
{
	const char *command;
	const char *session, *cwd, *agent, *agent_sid;
	const char *ref, *shape, *key;
};

struct xcmd_result
{
	unsigned long rows, skipped;	/* rows handed over; lines left out
				** (invalid, over 64 KB, the one seen past the
				** row cap) */
	int ran;		/* the program started */
	int stopped;		/* ended early: the clock, a cap or the callback */
	int code;		/* exit code, -1 when signalled or not run */
	char err[XCMD_ERR_MAX];		/* stderr's first line, one line */
	char title[XR_TITLE_MAX * 4 + 1];	/* the {"title":...} line */
	char warn[XCMD_WARN_MAX];	/* one line for the header, or "" */
};

/* Called with each canonical row line (no newline) as it is read; nonzero
** ends the command (res->stopped stays 0: the caller asked). */
typedef int (*xcmd_row_fn)(const char *line, void *arg);

/* Run ctx->command for kind, rows to fn as they arrive. XCMD_DEFINE (#412):
** rows held until the command ends, handed over only after a clean exit 0
** (or a 9th definition line's stop, dch's cap: the 8 stand; any other
** line past the 8th is read and ignored, #417); a non-zero exit, the
** clock, the 1 MB cap or no memory for the rows hands none over (res->rows 0) and leaves res->warn. Returns res->rows:
** > 0, the command is the source; 0, the caller uses its built-in source.
** res->warn is the header's warn either way: set when the command failed,
** could not run, timed out or hit a cap, "" for a clean answer (an empty
** one too) and when no command is configured. */
int xcmd_rows(enum xcmd_kind kind, const struct xcmd_ctx *ctx, xcmd_row_fn fn,
              void *arg, struct xcmd_result *res);

/* Pure parts, for the units and the fuzz target. */

/* The command line into argv[max] (NULL-terminated), words in buf[cap]:
** blanks separate, '...' and "..." quote (no escapes inside), a word's
** leading unquoted ~/ is home. argc, or -1 (empty, an open quote, too many
** words, too long, ~/ with no home). */
int xcmd_argv(const char *line, const char *home, char *buf, size_t cap,
              char **argv, int max);

enum xcmd_line_kind { XCMD_SKIP, XCMD_ROW, XCMD_TITLE, XCMD_BLANK };

/* One stdout line s[n] (no newline) of kind. first: no line before it was a
** row or the title; pos: the row's number, its id when it has none.
** XCMD_DEFINE: a row is {"title","passage","url","status"}, from an object
** with a title, or from a first line that is no JSON object and does not
** start with '{' (its text the title). A leading BOM and escapes go
** first, so a colour-wrapped JSON line is JSON and one they empty is
** XCMD_BLANK (#412, #417). XCMD_ROW: out holds the row line; XCMD_TITLE: out holds the list title
** (cut like a title); XCMD_SKIP: not a row (counted); XCMD_BLANK: only
** blanks (not counted). out[cap] needs XCMD_LINE_MAX + 1. */
enum xcmd_line_kind xcmd_line(enum xcmd_kind kind, const char *s, size_t n,
                              int first, unsigned long pos, char *out,
                              size_t cap);

#endif
