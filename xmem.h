/*
 * xmem.h - X-ray --memory (#322): `dch --xray <n> --memory --json`, a
 * session's memories and the instruction files in force, as JSON lines.
 *
 * Pure engine, as xray.c: dch.c resolves the session; this file lists,
 * reads and prints. Nothing is written, no symlink is followed, a file
 * that is not regular is never read.
 */
#ifndef DCH_XMEM_H
#define DCH_XMEM_H

#include <stddef.h>
#include <stdio.h>
#include <sys/stat.h>

#define XM_ROWS   50000		/* rows printed, the rest counted dropped */
#define XM_LINE   (64u << 10)	/* one row line */
#define XM_BODY   (16u << 10)	/* a body, then "cut":true */
#define XM_FILE   (1u << 20)	/* a bigger file is listed with no body */
#define XM_LEVELS 32		/* directories walked up from the cwd */

struct xm_req
{
	const char *agent;	/* "claude", "codex", NULL (a shell: none) */
	const char *cfg;	/* Claude's config dir or CODEX_HOME, "" none */
	const char *path;	/* the transcript or rollout, NULL none */
	const struct stat *st;	/* its lookup's stat */
	const char *cwd;	/* the session's, "" unknown */
};

/* The header, a {"memory":{…}} line per row, the done line. 0, or -1 a
** write failed. */
int xm_print(FILE *f, const struct xm_req *q);

/* The parsers (the units and fuzz_xmem). A span is bytes [s, s + n) of
** the input; n 0 for none. */
struct xm_span
{
	size_t s, n;
};
/* A leading `---` block: name, description and type (top level, or
** metadata.type); quotes stripped. 1 with f filled, 0 no block. */
struct xm_front
{
	struct xm_span name, desc, type;
};
int xm_front(const char *s, size_t n, struct xm_front *f);
/* One MEMORY.md line, `- [Title](file.md) — hook`: 1 with l filled when
** the target is one file name ending in .md (no '/', not dot-led), else 0. */
struct xm_link
{
	struct xm_span title, file, hook;
};
int xm_link(const char *s, size_t n, struct xm_link *l);
/* A shell command that writes what it names: a '>' redirect not led by a
** digit (2>/dev/null reads), or tee, rm, mv, touch, truncate, sd, sed -i,
** perl -i or perl -pi as a word. */
int xm_wrote(const char *s, size_t n);

#endif /* DCH_XMEM_H */
