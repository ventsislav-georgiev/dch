/*
 * xray.h - the X-ray engine's one-shot half (#120, XR1b): `dch --xray <n>
 * --json` chapters for Claude transcripts and shell rings.
 *
 * Pure engine: no sockets, no master, no dch.c state. It runs only in the
 * querying process (K3); master.o links none of it. dch.c resolves the
 * session and fetches the ring; this file decodes, builds chapters and
 * prints the schema-1 object (spec #832 v3.2.1 sections 3.2, 5.3, 7.1-7.3,
 * 8.1).
 */
#ifndef DCH_XRAY_H
#define DCH_XRAY_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/stat.h>

#define XR_TITLE_MAX   120		/* scalars (3.2, ruling R3) */
#define XR_CHAPTERS    5000		/* 13.2 item 6: the newest kept */
#define XR_HEAD_RANGE  (16ull << 20)	/* 3.8 per-file range: head ... */
#define XR_TAIL_RANGE  (256ull << 20)	/* ... plus the newest 256 MB */
#define XR_CHUNK       (1u << 20)	/* read size (QA 1.44 LOW-8: 4 MiB
				** put a near-cap query over 16 MB) */
#define XR_LINE_MAX    (1u << 20)	/* longer lines are one failed step */
#define XR_LINE_TOKENS 100000		/* so are lines of more JSON tokens
					** (about 20 B each: 3.8's 16 MB) */
#define XR_OUT_MAX     (4u << 20)	/* the --json object cap */
#define XR_PENDING     256		/* open tool calls, Prosper's bound */
#define XR_PATH_MAX    1024		/* config_dir and cwd bound (8.1) */

/* The limits above as the code reads them. Only fuzz_xray lowers them, so
** a small input reaches every cap; never above the defaults (the buffers are
** sized by the macros). */
struct xr_limits
{
	uint64_t head, tail;		/* XR_HEAD_RANGE, XR_TAIL_RANGE */
	size_t chunk, line, out;	/* XR_CHUNK, XR_LINE_MAX, XR_OUT_MAX */
};
extern struct xr_limits xr_lim;

/* One chapter (3.2). at < 0 prints null. */
struct xr_chapter
{
	uint64_t offset;
	long long at;
	char anchor[40];
	char title[XR_TITLE_MAX * 4 + 1];
	const char *kind;		/* "prompt", "command", "compact" */
	int partial, error;
};

/* The newest XR_CHAPTERS, oldest first from `start` (a ring). `total`
** counts every chapter started, so total - n were dropped. */
struct xr_chapters
{
	struct xr_chapter *v;
	size_t start, n;
	uint64_t total;
};
void xr_chapters_free(struct xr_chapters *c);
const struct xr_chapter *xr_chapter_at(const struct xr_chapters *c,
                                       size_t i);

/* ---- identity (3.1) ---------------------------------------------------- */
/* A live shell's session_id: "m" + 16 lowercase hex, nothing else. Also the
** name rule (INFO-2): no create path takes a name of this shape. */
int xr_id_shaped(const char *s);
/* The first n bytes of a label, ASCII whitespace trimmed as the listing
** shows it, are id-shaped (INFO-2 for aliases). */
int xr_label_id_shaped(const char *s, size_t n);
/* 3.1's Claude UUID form: 36 chars of [0-9a-f-]. */
int xr_uuid_form(const char *s);
/* 8-4-4-4-12 lowercase-or-upper hex: the shape a path join may take. */
int xr_uuid_shaped(const char *s);
/* A History record name: "<file>.json", one segment, no '/', no "..". */
int xr_record_form(const char *s);

/* ---- the open seam (8.1) ----------------------------------------------- */
/* O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC, fstat S_ISREG and the lookup's
** (st_dev, st_ino), else closed unread; then F_NOCACHE / FADV_DONTNEED.
** The fd, or -1. Every call is counted (and traced, with no path). */
int xr_seam_open(const char *path, const struct stat *want);
extern unsigned long xr_seam_opened, xr_seam_refused;

/* <cfg>/projects/<one dir>/<sid>.jsonl, 7.2's checks: cfg absolute, no
** "..", <= 1024 bytes; sid uuid-shaped; one names-only readdir; an lstat per
** entry; exactly one regular hit. 0 with path, st and project (the dir
** name) filled, else -1. */
int xr_config_ok(const char *cfg);
int xr_claude_lookup(const char *cfg, const char *sid, char *path,
                     size_t pathsz, struct stat *st, char *project,
                     size_t projectsz);

/* ---- Claude (7.2, 5.3) ------------------------------------------------- */
struct xr_claude;
struct xr_claude *xr_claude_new(void);
void xr_claude_free(struct xr_claude *d);
/* One JSONL line (no '\n', and line[len] == '\0'), its byte offset; a line
** over XR_LINE_MAX is passed as len 0 with oversized 1. */
void xr_claude_line(struct xr_claude *d, const char *line, size_t len,
                    uint64_t off, int oversized);
/* Close the open turn and hand the chapters over (d keeps none). */
void xr_claude_end(struct xr_claude *d, uint64_t ino,
                   struct xr_chapters *out);
/* The newest absolute cwd an entry carried, "" for none. */
const char *xr_claude_cwd(const struct xr_claude *d);
/* Read fd under 3.8's range (head 16 MB + newest 256 MB, 1 MB chunks,
** 1 MB lines): 0, *capped set when bytes were skipped or the file shrank
** mid-read; -1 on a read error. */
int xr_claude_read(struct xr_claude *d, int fd, uint64_t size, int *capped);

/* ---- shell ring (7.3) -------------------------------------------------- */
struct xr_rec
{
	uint64_t seq;
	int kind, flags;		/* RING_K_*, RING_F_* (ring.h) */
	uint32_t t;
	const char *text;
	size_t len;
};
/* Chapters from the ring's marks: one per primary A followed by B, then C
** with the client-input flag (else forged: 7.3) and non-blank command text, anchored on the block's first prompt-kind
** record (the A mark when it has none). *paired: some block was paired (else
** the corpus is mentions only); *alt: the newest A came on the alt screen. */
void xr_shell_chapters(const struct xr_rec *r, size_t n,
                       struct xr_chapters *out, int *paired, int *alt);
/* The same, one record at a time, so a ring is never held whole (3.8):
** xr_shell_begin, xr_shell_feed per record oldest first (x is not kept),
** then paired = npaired != 0 and alt. */
#define XR_CMD_MAX 4096	/* command text kept per block; the title is 120 */
struct xr_shell
{
	struct xr_chapters *out;
	int st, havep, alt;
	uint64_t aseq, npaired;	/* npaired: chapters still standing */
	uint32_t at;
	long long ord;		/* the open block's chapter, -1 for none */
	size_t cl;
	char cmd[XR_CMD_MAX + 1];
};
void xr_shell_begin(struct xr_shell *s, struct xr_chapters *out);
void xr_shell_feed(struct xr_shell *s, const struct xr_rec *x);
/* One MSG_RING_READ reply's records ({u16 len, u8 kind, u8 flags, u32 time,
** text}, seqs from first), bounds-checked, to fn one at a time (x and its
** text live until fn returns). The record bytes walked, or -1 when the
** reply is torn: a record past bl, bytes left over, or not `count` records.
** Records before the tear were handed over. */
long long xr_ring_walk(const unsigned char *buf, size_t bl, uint64_t first,
                       uint32_t count,
                       void (*fn)(void *ctx, const struct xr_rec *x),
                       void *ctx);

/* ---- helpers ----------------------------------------------------------- */
/* Claude Code's project-dir rule over cwd (7.1): each UTF-16 unit outside
** [A-Za-z0-9] becomes '-'; past 200, the first 200 + "-" + base36 hash. */
void xr_project_key(const char *cwd, char *out, size_t outsz);
/* s[0..n) as a title: invalid UTF-8 as U+FFFD, the first non-blank line,
** whitespace runs collapsed to one space, trimmed, <= 120 scalars. */
void xr_title(const char *s, size_t n, char *out, size_t outsz);

/* ---- the answer (3.2) -------------------------------------------------- */
struct xr_answer
{
	const char *session, *session_id, *corpus, *reason, *why;
	const char *project, *project_label;
	int live, available, mentions_only, complete, chapters_evicted;
	long long lines;		/* < 0 prints null */
	const struct xr_chapters *ch;
};
/* Write the object and a newline; past XR_OUT_MAX the oldest chapters go,
** with complete:false, why:"output". 0, or -1 when the write failed. */
int xr_print(FILE *f, const struct xr_answer *a);
/* A JSON string as --ls-json writes it (dch_json_quote). */
void xr_put_str(FILE *f, const char *s);

#endif /* DCH_XRAY_H */
