/*
 * ring.c - X-ray shell output ring (#119). See ring.h for the format.
 *
 * Traces from here carry lengths and seqs only, never record text (S3).
 */
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/mman.h>
#include "ring.h"

#ifndef MAP_ANONYMOUS
#define MAP_ANONYMOUS MAP_ANON
#endif

#define EXPORT_MAGIC "dchring1"
#define EXPORT_HDR   48u
#define SEQ_LIMIT    (1ull << 53)	/* exact in a JSON double */

long
ring_parse_size(const char *v)
{
	unsigned long long n = 0;
	const char *p = v;

	if (!v || *v < '0' || *v > '9')
		return -1;
	for (; *p >= '0' && *p <= '9'; p++)
		if ((n = n * 10 + (unsigned)(*p - '0')) > (1ull << 40))
			return -1;
	if (*p == 'k' || *p == 'K')
		n <<= 10, p++;
	else if (*p == 'm' || *p == 'M')
		n <<= 20, p++;
	if (*p == 'i' && p[1] == 'B')
		p += 2;
	else if (*p == 'B')
		p++;
	if (*p)
		return -1;
	if (n > RING_MAX)
		n = RING_MAX;
	return (long)((n + RING_CHUNK - 1) / RING_CHUNK * RING_CHUNK);
}

static void
rand_bytes(void *buf, size_t n)
{
#if defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__)
	arc4random_buf(buf, n);
#else
	int fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
	ssize_t got = fd >= 0 ? read(fd, buf, n) : -1;

	if (fd >= 0)
		close(fd);
	if (got != (ssize_t)n)
	{
		/* shortcut: time^pid when /dev/urandom is unreadable; the base
		** only has to differ between images, not resist a guess */
		unsigned long long s = (unsigned long long)time(NULL) ^
		                       ((unsigned long long)getpid() << 20) ^
		                       (unsigned long long)clock();
		unsigned char *b = buf;
		size_t i;

		for (i = 0; i < n; i++)
		{
			s = s * 6364136223846793005ull + 1442695040888963407ull;
			b[i] = (unsigned char)(s >> 56);
		}
	}
#endif
}

uint64_t
ring_seq_base(void)
{
	uint64_t v = 0;

	rand_bytes(&v, sizeof v);
	return v & ((1ull << 48) - 1);
}

void
ring_session_id(char out[18])
{
	static const char d[] = "0123456789abcdef";
	unsigned char b[8];
	int i;

	rand_bytes(b, sizeof b);
	out[0] = 'm';
	for (i = 0; i < 8; i++)
	{
		out[1 + i * 2] = d[b[i] >> 4];
		out[2 + i * 2] = d[b[i] & 15];
	}
	out[17] = '\0';
}

static unsigned char *
chunk_new(void)
{
	void *p = mmap(NULL, RING_CHUNK, PROT_READ | PROT_WRITE,
	               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);

	if (p == MAP_FAILED)
		return NULL;
#ifdef MADV_DONTDUMP
	/* S5: keeps the ring chunks out of a core; the soft RLIMIT_CORE 0
	** (master.c core_off) covers the heap copies */
	(void)madvise(p, RING_CHUNK, MADV_DONTDUMP);
#endif
	return p;
}

/* No chunk has a first record yet: UINT64_MAX is past every tail. */
static void
marks_reset(struct dch_ring *r)
{
	size_t i;

	for (i = 0; i < r->cap / RING_CHUNK; i++)
		r->mark[i].off = UINT64_MAX;
}

int
ring_init(struct dch_ring *r, size_t cap, uint64_t seq_base)
{
	memset(r, 0, sizeof *r);
	r->head_seq = r->tail_seq = seq_base;
	cap = (cap + RING_CHUNK - 1) / RING_CHUNK * RING_CHUNK;
	if (cap > RING_MAX)
		cap = RING_MAX;
	if (!cap)
		return 0;
	r->chunk = calloc(cap / RING_CHUNK, sizeof *r->chunk);
	r->mark = malloc(cap / RING_CHUNK * sizeof *r->mark);
	if (!r->chunk || !r->mark)
	{
		free(r->chunk);
		free(r->mark);
		r->chunk = NULL;
		r->mark = NULL;
		return -1;
	}
	r->cap = cap;
	marks_reset(r);
	return 0;
}

void
ring_free(struct dch_ring *r)
{
	size_t i;

	for (i = 0; r->chunk && i < r->cap / RING_CHUNK; i++)
		if (r->chunk[i])
			munmap(r->chunk[i], RING_CHUNK);
	free(r->chunk);
	free(r->mark);
	r->chunk = NULL;
	r->mark = NULL;
	r->cap = 0;
	r->head = r->tail = 0;
	r->head_seq = r->tail_seq;
}

void
ring_clear(struct dch_ring *r)
{
	r->head = r->tail;
	r->head_seq = r->tail_seq;
	r->chapters_evicted = 0;
	r->epoch++;
	r->gen++;
}

/* Copy n bytes between the ring at logical offset off and a flat buffer.
** Chunks are cap-aligned, so a wrap is just another chunk boundary. */
static void
ring_get(const struct dch_ring *r, uint64_t off, unsigned char *dst,
         size_t n)
{
	while (n)
	{
		size_t p = (size_t)(off % r->cap), co = p % RING_CHUNK;
		size_t k = RING_CHUNK - co < n ? RING_CHUNK - co : n;
		const unsigned char *c = r->chunk[p / RING_CHUNK];

		if (c)
			memcpy(dst, c + co, k);
		else
			memset(dst, 0, k);	/* never written: reads as junk */
		dst += k;
		off += k;
		n -= k;
	}
}

static void
ring_put(struct dch_ring *r, uint64_t off, const unsigned char *src,
         size_t n)
{
	while (n)
	{
		size_t p = (size_t)(off % r->cap), co = p % RING_CHUNK;
		size_t k = RING_CHUNK - co < n ? RING_CHUNK - co : n;

		memcpy(r->chunk[p / RING_CHUNK] + co, src, k);
		src += k;
		off += k;
		n -= k;
	}
}

/* Every chunk [off, off+n) touches exists afterwards, or -1. */
static int
ring_reserve(struct dch_ring *r, uint64_t off, size_t n)
{
	uint64_t end = off + n;

	off -= off % RING_CHUNK;
	for (; off < end; off += RING_CHUNK)
	{
		size_t ci = (size_t)(off % r->cap) / RING_CHUNK;

		if (!r->chunk[ci] && !(r->chunk[ci] = chunk_new()))
			return -1;
	}
	return 0;
}

/* The record at off, if it lies whole inside [head, tail): its text length,
** else -1 (a bad record ends a read; it is never skipped over). */
static long
rec_at(const struct dch_ring *r, uint64_t off, unsigned char *hdr)
{
	size_t len;

	if (off < r->head || off + RING_HDR > r->tail)
		return -1;
	ring_get(r, off, hdr, RING_HDR);
	len = (size_t)hdr[0] | (size_t)hdr[1] << 8;
	if (hdr[2] < RING_K_PROMPT || hdr[2] > RING_K_GAP ||
	    off + RING_HDR + len > r->tail)
		return -1;
	return (long)len;
}

static void
evict_one(struct dch_ring *r)
{
	unsigned char h[RING_HDR], c = 0;
	size_t len;

	ring_get(r, r->head, h, RING_HDR);
	len = (size_t)h[0] | (size_t)h[1] << 8;
	if (h[2] == RING_K_MARK && len)
		ring_get(r, r->head + RING_HDR, &c, 1);
	if (h[2] == RING_K_PROMPT || c == 'A')
		r->chapters_evicted = 1;
	r->head += RING_HDR + len;
	r->head_seq++;
}

uint64_t
ring_append(struct dch_ring *r, int kind, int flags, uint32_t t,
            const char *text, size_t len)
{
	unsigned char h[RING_HDR];
	size_t room, need;

	if (!r->cap)
		return UINT64_MAX;
	room = r->cap - RING_HDR < RING_TEXT_MAX ? r->cap - RING_HDR
	                                         : RING_TEXT_MAX;
	if (len > room)
	{
		/* the first byte left out must not be a continuation byte */
		len = room;
		while (len && ((unsigned char)text[len] & 0xc0) == 0x80)
			len--;
		flags |= RING_F_TRUNC;
	}
	need = RING_HDR + len;
	while (r->tail - r->head + need > r->cap)
		evict_one(r);
	if (ring_reserve(r, r->tail, need) < 0)
		return UINT64_MAX;
	h[0] = (unsigned char)(len & 0xff);
	h[1] = (unsigned char)(len >> 8);
	h[2] = (unsigned char)kind;
	h[3] = (unsigned char)flags;
	h[4] = (unsigned char)(t & 0xff);
	h[5] = (unsigned char)(t >> 8 & 0xff);
	h[6] = (unsigned char)(t >> 16 & 0xff);
	h[7] = (unsigned char)(t >> 24 & 0xff);
	ring_put(r, r->tail, h, RING_HDR);
	{
		struct ring_mark *m = &r->mark[(size_t)(r->tail % r->cap) /
		                               RING_CHUNK];

		/* the chunk's first record: its slot holds an older lap's, or
		** one evicted or cleared */
		if (m->off / RING_CHUNK != r->tail / RING_CHUNK ||
		    m->off < r->head)
		{
			m->seq = r->tail_seq;
			m->off = r->tail;
		}
	}
	if (len)
		ring_put(r, r->tail + RING_HDR, (const unsigned char *)text,
		         len);
	r->tail += need;
	r->gen++;
	return r->tail_seq++;
}

size_t
ring_read(struct dch_ring *r, uint64_t from, unsigned char *out, size_t max,
          uint64_t *first, uint64_t *n, int *status)
{
	unsigned char h[RING_HDR];
	uint64_t off = r->head, seq = r->head_seq;
	size_t w = 0;
	long len;

	*status = 0;
	*n = 0;
	if (from < r->head_seq || from > r->tail_seq)
	{
		*status |= RING_R_RANGE;
		from = r->head_seq;
	}
	/* Start at the nearest known record at or before from: the last
	** chunk whose first record is not past it (bounding the walk to one
	** chunk of records, S6: the master thread), or the last stop. */
	if (r->tail > r->head)
	{
		uint64_t c = (r->tail - 1) / RING_CHUNK, h0 = r->head / RING_CHUNK;

		for (;; c--)
		{
			const struct ring_mark *m =
			    &r->mark[(size_t)(c % (r->cap / RING_CHUNK))];

			if (m->off / RING_CHUNK == c && m->off >= r->head &&
			    m->off < r->tail && m->seq <= from)
			{
				off = m->off;
				seq = m->seq;
				break;
			}
			if (c == h0)
				break;
		}
	}
	if (r->cache_seq >= seq && r->cache_seq <= from &&
	    r->cache_off >= r->head && r->cache_off <= r->tail)
	{
		off = r->cache_off;
		seq = r->cache_seq;
	}
	for (; seq < from; seq++)
	{
		if ((len = rec_at(r, off, h)) < 0)
		{
			*status |= RING_R_RANGE;
			*first = seq;
			return 0;
		}
		off += RING_HDR + (size_t)len;
	}
	*first = seq;
	for (; seq < r->tail_seq; seq++)
	{
		if ((len = rec_at(r, off, h)) < 0)
		{
			*status |= RING_R_RANGE;
			break;
		}
		if (w + RING_HDR + (size_t)len > max)
			break;
		ring_get(r, off, out + w, RING_HDR + (size_t)len);
		w += RING_HDR + (size_t)len;
		off += RING_HDR + (size_t)len;
		(*n)++;
	}
	r->cache_seq = seq;
	r->cache_off = off;
	return w;
}

static void
le_put(unsigned char *p, uint64_t v, int n)
{
	int i;

	for (i = 0; i < n; i++)
		p[i] = (unsigned char)(v >> (8 * i));
}

static uint64_t
le_get(const unsigned char *p, int n)
{
	uint64_t v = 0;
	int i;

	for (i = n - 1; i >= 0; i--)
		v = v << 8 | p[i];
	return v;
}

size_t
ring_export_size(const struct dch_ring *r)
{
	return EXPORT_HDR + (size_t)(r->tail - r->head);
}

/* magic[8] | u32 flags | u64 cap | u64 head_seq | u64 count | u64 used |
** u32 epoch | used bytes of records, oldest first */
void
ring_export(const struct dch_ring *r, unsigned char *out)
{
	memcpy(out, EXPORT_MAGIC, 8);
	le_put(out + 8, r->chapters_evicted ? 1 : 0, 4);
	le_put(out + 12, r->cap, 8);
	le_put(out + 20, r->head_seq, 8);
	le_put(out + 28, r->tail_seq - r->head_seq, 8);
	le_put(out + 36, r->tail - r->head, 8);
	le_put(out + 44, r->epoch, 4);
	if (r->cap && r->tail > r->head)
		ring_get(r, r->head, out + EXPORT_HDR,
		         (size_t)(r->tail - r->head));
}

int
ring_import(struct dch_ring *r, const unsigned char *buf, size_t len)
{
	uint64_t head_seq, count, used, i;
	size_t off;

	if (!r->cap || len < EXPORT_HDR || memcmp(buf, EXPORT_MAGIC, 8))
		return -1;
	head_seq = le_get(buf + 20, 8);
	count = le_get(buf + 28, 8);
	used = le_get(buf + 36, 8);
	if (le_get(buf + 12, 8) > RING_MAX || used > RING_MAX ||
	    used != len - EXPORT_HDR || count > used / RING_HDR ||
	    head_seq >= SEQ_LIMIT || head_seq + count >= SEQ_LIMIT)
		return -1;
	/* walk every record before touching r: a torn blob loads nothing */
	for (off = EXPORT_HDR, i = 0; off < len; i++)
	{
		size_t l;

		if (off + RING_HDR > len)
			return -1;
		l = (size_t)buf[off] | (size_t)buf[off + 1] << 8;
		if (buf[off + 2] < RING_K_PROMPT || buf[off + 2] > RING_K_GAP ||
		    off + RING_HDR + l > len)
			return -1;
		off += RING_HDR + l;
	}
	if (i != count)
		return -1;
	r->head = r->tail = 0;
	r->head_seq = r->tail_seq = head_seq;
	marks_reset(r);
	for (off = EXPORT_HDR; off < len; )
	{
		const unsigned char *h = buf + off;
		size_t l = off + RING_HDR <= len ?
		    (size_t)h[0] | (size_t)h[1] << 8 : len;

		/* again: buf may be a shared mapping */
		if (off + RING_HDR + l > len ||
		    ring_append(r, h[2], h[3], (uint32_t)le_get(h + 4, 4),
		                (const char *)h + RING_HDR, l) == UINT64_MAX)
		{
			ring_clear(r);
			return -1;
		}
		off += RING_HDR + l;
	}
	if (le_get(buf + 8, 4) & 1)
		r->chapters_evicted = 1;
	r->epoch = (uint32_t)le_get(buf + 44, 4);
	r->cache_seq = r->cache_off = 0;
	return 0;
}

int
ring_check(const struct dch_ring *r)
{
	unsigned char h[RING_HDR];
	uint64_t off = r->head, seq;
	long len;

	if (r->head > r->tail || r->tail - r->head > r->cap ||
	    r->head_seq > r->tail_seq || r->cap % RING_CHUNK ||
	    r->cap > RING_MAX)
		return -1;
	if (!r->cap)
		return r->head == r->tail && r->head_seq == r->tail_seq ? 0 : -1;
	for (seq = r->head_seq; seq < r->tail_seq; seq++)
	{
		if ((len = rec_at(r, off, h)) < 0 || (size_t)len > RING_TEXT_MAX)
			return -1;
		off += RING_HDR + (size_t)len;
	}
	return off == r->tail ? 0 : -1;
}
