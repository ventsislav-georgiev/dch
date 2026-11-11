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
#define XR_FILES       64		/* 3.8: files per --ref */
#define XR_BYTES       (1ull << 30)	/* 3.8: bytes read per --ref */
#define XR_WALL_MS     10000		/* 3.8: --ref --full's wall time */

/* The limits above as the code reads them. Only fuzz_xray and dch's
** DCH_XRAY_CAPS (tests) lower them, so a small input reaches every cap;
** never above the defaults (the buffers are sized by the macros). */
struct xr_limits
{
	uint64_t head, tail;		/* XR_HEAD_RANGE, XR_TAIL_RANGE */
	size_t chunk, line, out;	/* XR_CHUNK, XR_LINE_MAX, XR_OUT_MAX */
	uint64_t files, bytes;		/* XR_FILES, XR_BYTES */
	long long ms;			/* XR_WALL_MS */
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
** shows it, are id-shaped in any case (INFO-2: the name rule, aliases). */
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

/* ---- refs (XR2a, #135): grammar, normalization, definitions ------------ */
#define XR_KEY_MAX     1024		/* bytes of normalized text; a longer
					** candidate is no ref */
#define XR_KEYS        2000		/* index keys (3.2), the rest counted */
#define XR_SPANS       128		/* mentions per entry (3.8) */
#define XR_DEFS        8		/* definitions per entry (3.8) */
#define XR_TEXT_CAP    6000		/* scalars: prose clips (v2.1 3.1) */
#define XR_OUTPUT_CAP  3000		/* scalars: tool output clips */
#define XR_FIELD_CAP   240		/* scalars: card fields, search rows */
#define XR_CALLS       512		/* the tool_use ring (3.8) */
#define XR_BASH_MAX    4096		/* a Bash command kept in the ring */
#define XR_PASSAGE     600		/* scalars */
#define XR_D2_MAX      (256u << 10)	/* D2's JSON bound */
#define XR_SH_LINE     (64u << 10)	/* a WRAP-joined shell line (3.8) */

enum { XR_TICKET, XR_ISSUE, XR_PULL, XR_COMMIT, XR_FILE, XR_URL };
extern const char *const xr_shape_name[];

/* Grammar contexts (v2.1 2.4): plain prose (user text: fences and inline
** code spans found by the small scanner), Markdown prose (assistant text:
** the same, plus already-linked labels), a tool field (code: U, R, S and
** F-with-a-line anywhere), and prose with no code scan (the D3 command). */
enum { XR_PROSE, XR_MD, XR_CODE, XR_PLAIN };

/* One ref span: bytes [s, e) of the text, its rule letter, shape, and key
** "<shape>:<normalized text>" (text = key + its shape name + 1). full: a
** commit's hex as written, lowercase; "" otherwise. */
struct xr_span
{
	size_t s, e;
	int shape;
	char rule;
	char key[XR_KEY_MAX + 16];
	char full[41];
};
const char *xr_span_text(const struct xr_span *sp);
/* Every span of s[0..n) in context `mode`, in order, to fn (nonzero stops);
** cwd absolute or NULL, for file keys (2.5). The span count. */
typedef int (*xr_span_fn)(void *ctx, const struct xr_span *sp);
size_t xr_grammar(const char *s, size_t n, int mode, const char *cwd,
                  xr_span_fn fn, void *ctx);
/* --ref's parse (3.1): the grammar in prose context finds exactly one span
** covering the whole trimmed text. 1 with *out, else 0. */
int xr_ref_parse(const char *text, const char *cwd, struct xr_span *out);
/* --ref's value rule (3.1): 1..256 bytes of UTF-8, no C0, DEL, C1 or bidi
** control. */
int xr_ref_ok(const char *text);

/* What the decoders hand a refs consumer: one mention, or one definition
** (title set). line/ll: the text the span is in, its span at sp->s..sp->e;
** off: the JSONL line's byte offset, or the shell record's seq; unit: the
** counting unit (a shell block, or a row without marks; a Claude mention is
** its own unit); chapter: the anchor, or NULL. */
struct xr_hit
{
	const struct xr_span *sp;
	const char *line;
	size_t ll;
	uint64_t off, unit, row;
	long long at;
	const char *chapter;
	const char *title, *passage, *rule;	/* a definition */
};
struct xr_sink
{
	void (*hit)(void *ctx, const struct xr_hit *h);
	void *ctx;
};

/* The index (3.2's refs): every key, counted, defined, titled. It lives in
** the querying process exactly as long as its query (K3); xr_index_live
** counts the ones not yet freed (a test seam). */
struct xr_index;
struct xr_index *xr_index_new(void);
void xr_index_free(struct xr_index *x);
extern unsigned long xr_index_live;
void xr_index_sink(struct xr_index *x, struct xr_sink *k);
/* per_row: a shell without marks counts rows, not blocks (7.3) */
void xr_index_per_row(struct xr_index *x, int per_row);
size_t xr_index_keys(const struct xr_index *x);

/* --ref (3.4): one key's NDJSON lines (first, definition, mention, tally,
** progress, done), all under xr_lim.out with room kept for the tally and
** done. */
long long xr_now_ms(void);	/* CLOCK_MONOTONIC */
struct xr_scan;
struct xr_scan *xr_scan_new(FILE *f, const char *key, const char *session,
                            const char *project, int live, int full,
                            int shell, int budget, long long t0);
void xr_scan_free(struct xr_scan *x);
void xr_scan_sink(struct xr_scan *x, struct xr_sink *k);
/* line 1, before any record is read */
int xr_scan_first(struct xr_scan *x, const char *session_id, int shape,
                  const char *text);
/* --scope (3.4), before xr_scan_first: 0 session (the default here), 1
** project, 2 all */
void xr_scan_scope(struct xr_scan *x, int scope);
/* The next file after <n>'s (3.4's order): another session's, same: in
** <n>'s project dir. 0 read it; nonzero the scan stopped (a cap, the
** budget, a result in light mode), so no more files. */
int xr_scan_next(struct xr_scan *x, const char *session, const char *project,
                 int live, int same);
/* the --ref tick: bytes so far; nonzero once the scan should stop */
int xr_scan_tick(void *ctx, uint64_t bytes);
/* one file read: its held mentions and its tally (partial: range-capped) */
void xr_scan_file(struct xr_scan *x, uint64_t bytes, uint64_t too_long,
                  int partial, int per_row);
int xr_scan_done(struct xr_scan *x);
/* a write failed (3.4's cancel: no more lines, no done): 1 EPIPE, the
** reader left; 2 any other error; 0 none */
int xr_scan_cancelled(const struct xr_scan *x);

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
/* Refs too: every entry's mentions and definitions go to k (copied), with
** chapter anchors on inode ino. tick, when set, runs after each line with
** the bytes read so far; nonzero stops the read (a --ref budget or cap). */
void xr_claude_refs(struct xr_claude *d, const struct xr_sink *k,
                    uint64_t ino, int (*tick)(void *ctx, uint64_t bytes),
                    void *tctx);
/* Lines over xr_lim.line seen so far (--ref's lines_too_long). */
uint64_t xr_claude_too_long(const struct xr_claude *d);
/* --follow (#136): the tail. Lines of fd in [from, to), from a line start;
** a line still open at `to` is left for the next call, which starts at
** xr_claude_next(d) (also set by xr_claude_read). The refs' tick may stop it
** early at a line end (the full pass's slices). 0, 1 the file shrank under
** the read, -1 a read error. The decoder (its tool_use ring, its open turn)
** carries on across calls (QA A2). */
int xr_claude_tail(struct xr_claude *d, int fd, uint64_t from, uint64_t to);
uint64_t xr_claude_next(const struct xr_claude *d);
/* The chapters so far, anchored, the open turn included; d keeps them. */
const struct xr_chapters *xr_claude_chapters(struct xr_claude *d);

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
struct xr_shref;
struct xr_entries;
struct xr_shell
{
	struct xr_chapters *out;
	int st, havep, alt;
	uint64_t aseq, npaired;	/* npaired: chapters still standing */
	uint32_t at;
	long long ord;		/* the open block's chapter, -1 for none */
	size_t cl;
	char cmd[XR_CMD_MAX + 1];
	struct xr_shref *refs;	/* xr_shell_refs, else NULL */
	struct xr_entries *want;	/* --chapter: set after xr_shell_begin */
};
void xr_shell_begin(struct xr_shell *s, struct xr_chapters *out);
void xr_shell_feed(struct xr_shell *s, const struct xr_rec *x);
/* Refs over the ring too (7.3): rows WRAP-joined into logical lines of at
** most XR_SH_LINE bytes, tool-field rules, D1-D4 over paired blocks only.
** cwd: the pane's, for exclusion #1 and file keys. 0, or -1 out of memory.
** xr_shell_end flushes the last line and frees what refs held. */
int xr_shell_refs(struct xr_shell *s, const struct xr_sink *k,
                  const char *cwd);
void xr_shell_end(struct xr_shell *s);
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
	int snapshot;			/* --follow: {"schema":1,"snapshot":{…}} */
	long long lines;		/* < 0 prints null */
	long long other_projects;	/* 3.2: counted from names only */
	const struct xr_chapters *ch;
	struct xr_index *refs;		/* NULL prints [] */
};
/* Write the object and a newline; past XR_OUT_MAX the oldest chapters go,
** with complete:false, why:"output". 0, or -1 when the write failed. */
int xr_print(FILE *f, const struct xr_answer *a);
/* A JSON string as --ls-json writes it (dch_json_quote). */
void xr_put_str(FILE *f, const char *s);

/* ---- --follow's deltas (3.3; #136) ------------------------------------- */
/* What a follower last wrote: per ref key and chapter anchor, the hash of the
** object's bytes (a chapter's i, display only, is left out). */
struct xr_sent;
struct xr_sent *xr_sent_new(void);
void xr_sent_free(struct xr_sent *s);
/* After a snapshot of a: all of it counts as written. 0, -1 out of memory. */
int xr_sent_take(struct xr_sent *s, const struct xr_answer *a);
/* The diff of a against what was written, held for xr_delta_write: *nref,
** the ref keys new, changed or gone; *lost, a written chapter gone whose
** offset is at or past head (a shell's ring head: below it, the evict line
** drops it; pass 0 for a transcript). Nothing written. 0, -1 no memory. */
int xr_delta(struct xr_sent *s, const struct xr_answer *a, uint64_t head,
             size_t *nref, int *lost);
/* Write the held diff for the same a: a chapter line per new or changed
** chapter (oldest first), a ref line per new or changed key, a count-0 ref
** per gone key; then that is what was written. 0, -1 a write failed. */
int xr_delta_write(FILE *f, struct xr_sent *s, const struct xr_answer *a);

/* ---- --chapter (3.5; #156, XR3b) --------------------------------------- */
#define XR_CH_OUT      (1u << 20)	/* the --chapter object cap (3.8) */

/* --chapter's value rule (3.1): 1..128 bytes of [A-Za-z0-9:_.-]. */
int xr_anchor_ok(const char *s);

/* One chapter's entries (3.5), gathered while a decoder builds the
** chapters-only index: those of the chapter whose anchor equals `anchor`
** byte for byte, and nothing else (K14.2: no offset is ever taken on the
** anchor's say). Each entry is a JSON object, comma-joined in buf, cut at an
** entry once the whole object would pass its cap (truncated). */
struct xr_ent_call;
struct xr_entries
{
	const char *anchor;
	size_t cap;			/* buf's bound: the object's less its head */
	char *buf;
	size_t len;
	uint64_t ord;			/* the chapter's, once hit */
	int hit, truncated, done, failed;	/* failed: out of memory */
	struct xr_ent_call *calls;	/* Claude: the chapter's calls */
	int ncalls;
	char *out;			/* shell: the block's C..D rows */
	size_t ol;
};
/* Zeroed, for anchor, under out bytes of output for this session (its
** session_id at most 64 bytes: a UUID or a ring's id). */
void xr_entries_begin(struct xr_entries *e, const char *anchor,
                      const char *session, size_t out);
void xr_entries_free(struct xr_entries *e);
/* Claude: before the read; ino is the transcript's. A shell sets s->want
** after xr_shell_begin: its block's output entry is written when the block
** closes, or at xr_shell_end. */
void xr_claude_entries(struct xr_claude *d, struct xr_entries *e,
                       uint64_t ino);
/* {"schema":1,"session","session_id","anchor","title","at","kind",
** "entries":[…],"truncated"} and a newline. 0, or -1 the write failed. */
int xr_chapter_print(FILE *f, const char *session, const char *session_id,
                     const struct xr_chapter *c, const struct xr_entries *e);

#endif /* DCH_XRAY_H */
