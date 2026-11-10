/*
 * ring.h - X-ray shell output ring (#119, XR1a).
 *
 * The master copies each primary-screen row into this ring as it scrolls
 * into history (vt.c does the capture), plus OSC 133 marks and gap records.
 * The querying side reads it over the control socket (MSG_RING_READ).
 *
 * Storage: a logical byte ring of `cap` bytes over 64 KiB chunks, allocated
 * as the tail first reaches them. Records are whole and may straddle chunks:
 *
 *   u16 len LE | u8 kind | u8 flags | u32 time LE (epoch seconds) | len bytes
 *
 * Every record has a seq (head_seq .. tail_seq-1, consecutive, never on the
 * wire per record). Eviction drops whole records, oldest first. Plain C, no
 * libghostty: the lite build compiles it, and nothing ever appends there.
 */
#ifndef DCH_RING_H
#define DCH_RING_H

#include <stddef.h>
#include <stdint.h>

#define RING_CHUNK    65536u
#define RING_MAX      (16u << 20)	/* xray_ring ceiling */
#define RING_DEFAULT  (2u << 20)
#define RING_HDR      8u		/* record header bytes */
#define RING_TEXT_MAX 65535u

/* kinds */
enum
{
	RING_K_PROMPT = 1,
	RING_K_INPUT = 2,
	RING_K_OUTPUT = 3,
	RING_K_MARK = 4,	/* text: "A" "A;k" "B" "C" "C;<cmd>" "D" "D;<exit>" */
	RING_K_GAP = 5,		/* text: the reason ("agent", "latch", ...) */
};
/* flags */
#define RING_F_CONT  0x01	/* continues the previous record (soft wrap) */
#define RING_F_EXIT  0x02	/* a D mark that carries an exit code */
#define RING_F_TRUNC 0x04	/* text cut at the last UTF-8 boundary <= 64K */
#define RING_F_INPUT 0x08	/* a C mark: client input reached the pty
				** after this block's B */
#define RING_F_ALT   0x10	/* a mark seen on the alternate screen */

/* ring_read status bits */
#define RING_R_RANGE 0x01	/* `from` was stale (or a bad record ended the
				** read): data starts at the head instead */

struct dch_ring
{
	unsigned char **chunk;	/* cap / RING_CHUNK slots, NULL until used */
	size_t cap;		/* bytes; 0 = off */
	uint64_t head, tail;	/* logical byte offsets, tail - head <= cap */
	uint64_t head_seq, tail_seq;
	uint64_t cache_seq, cache_off;	/* last read position, a hint */
	unsigned long gen;	/* moves on every append and clear */
	uint32_t epoch;		/* clears so far; a reader's reset trigger */
	int chapters_evicted;	/* a prompt (or A mark) was evicted */
};

/* Size from the xray_ring config value ("2M", "512K", "0", bytes); -1 when
** the value is malformed (callers keep the default). Clamped to RING_MAX and
** rounded up to whole chunks. */
long ring_parse_size(const char *v);
/* A fresh random 48-bit seq base, and a session id "m" + 16 hex. */
uint64_t ring_seq_base(void);
void ring_session_id(char out[18]);

/* 0, or -1 (no memory: the ring stays off). cap 0 = off. */
int ring_init(struct dch_ring *r, size_t cap, uint64_t seq_base);
void ring_free(struct dch_ring *r);
/* Empty it (ED 3, RIS, a lost tracked ref). seqs continue; epoch moves. */
void ring_clear(struct dch_ring *r);
/* Append one record, evicting the oldest as needed. Text past 65535 bytes
** (or past what the ring holds) is cut on a UTF-8 boundary and flagged.
** Returns the record's seq, or UINT64_MAX when the ring is off. */
uint64_t ring_append(struct dch_ring *r, int kind, int flags, uint32_t t,
                     const char *text, size_t len);
/* Copy whole records from seq `from` into out (at most max bytes). *first is
** the seq of the first record copied, *n the record count; returns the byte
** count. *status gets RING_R_* bits. */
size_t ring_read(struct dch_ring *r, uint64_t from, unsigned char *out,
                 size_t max, uint64_t *first, uint64_t *n, int *status);

/* Handover across --restart (§13.2): a self-describing linear copy. */
size_t ring_export_size(const struct dch_ring *r);
void ring_export(const struct dch_ring *r, unsigned char *out);
/* Validate buf and load it into r (already ring_init'ed with its own cap).
** 0, or -1 (r is left empty). */
int ring_import(struct dch_ring *r, const unsigned char *buf, size_t len);

/* Invariant check for the fuzz target and the unit test: 0 when sound. */
int ring_check(const struct dch_ring *r);

#endif /* DCH_RING_H */
