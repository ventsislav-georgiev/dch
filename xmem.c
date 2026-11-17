/*
 * xmem.c - X-ray --memory (#322): a session's memories and the instruction
 * files in force, as JSON lines (see xmem.h).
 *
 * A Claude session: the memory directory beside its transcript (the index
 * MEMORY.md, one file per memory) and its instruction files; a Codex: its
 * AGENTS.md files. The transcript or rollout is decoded once (xray.c) for
 * the tool calls that touch a row's file: `session` and `chapter`.
 */
#include <config.h>

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "bridge.h"
#include "xmem.h"
#include "xray.h"

enum { XM_LOADED = 1, XM_WROTE = 2 };

struct xm_row
{
	char *id, *path, *title, *note;	/* title, note: the index's, or NULL */
	const char *scope;
	int instr, sess, stok;		/* stok: st holds its stat */
	unsigned long ord;		/* the order it was added in */
	unsigned long long seq;		/* the newest touch's call */
	char chapter[48];
	struct stat st;
};

struct xm_set
{
	struct xm_row *v;
	size_t n, cap, nmem;		/* the memory rows first, nmem of them */
	char memdir[PATH_MAX];		/* "" for none */
	int dfd;			/* memdir's, else -1 */
	struct xm_row **byp;		/* the rows by path, while the scan runs */
	unsigned long long seq;
	struct xm_live *live;		/* --follow's: touches kept by path */
};

/* ---- the parsers --------------------------------------------------------- */

static size_t
xm_eol(const char *s, size_t n, size_t i)
{
	while (i < n && s[i] != '\n')
		i++;
	return i;
}

static int
xm_blank(char c)
{
	return c == ' ' || c == '\t' || c == '\r';
}

static void
xm_trim(const char *s, size_t *a, size_t *b)
{
	while (*a < *b && xm_blank(s[*a]))
		(*a)++;
	while (*b > *a && xm_blank(s[*b - 1]))
		(*b)--;
}

static int
xm_fence(const char *s, size_t a, size_t b)
{
	xm_trim(s, &a, &b);
	return b - a == 3 && !memcmp(s + a, "---", 3);
}

/* a frontmatter value: quotes stripped; a block scalar (| or >) is none */
static void
xm_val(const char *s, size_t a, size_t b, struct xm_span *v)
{
	xm_trim(s, &a, &b);
	if (b - a >= 2 && (s[a] == '"' || s[a] == '\'') && s[b - 1] == s[a])
	{
		a++;
		b--;
	}
	else if (a < b && (s[a] == '|' || s[a] == '>'))
		return;
	if (!v->n && b > a)
	{
		v->s = a;
		v->n = b - a;
	}
}

static int
xm_key(const char *s, size_t a, size_t b, const char *k)
{
	return b - a == strlen(k) && !memcmp(s + a, k, b - a);
}

int
xm_front(const char *s, size_t n, struct xm_front *f)
{
	size_t i = 0, e, a, c;
	int meta = 0;

	memset(f, 0, sizeof *f);
	if (n >= 3 && !memcmp(s, "\xef\xbb\xbf", 3))
		i = 3;
	e = xm_eol(s, n, i);
	if (!xm_fence(s, i, e))
		return 0;
	while (e < n)
	{
		i = e + 1;
		e = xm_eol(s, n, i);
		if (xm_fence(s, i, e))
			return 1;
		for (a = i; a < e && (s[a] == ' ' || s[a] == '\t'); a++)
			;
		for (c = a; c < e && s[c] != ':'; c++)
			;
		if (c == e)
			continue;
		if (a == i)
		{
			meta = xm_key(s, a, c, "metadata");
			if (xm_key(s, a, c, "name"))
				xm_val(s, c + 1, e, &f->name);
			else if (xm_key(s, a, c, "description"))
				xm_val(s, c + 1, e, &f->desc);
			else if (xm_key(s, a, c, "type"))
				xm_val(s, c + 1, e, &f->type);
		}
		else if (meta && xm_key(s, a, c, "type"))
			xm_val(s, c + 1, e, &f->type);
	}
	memset(f, 0, sizeof *f);	/* never closed: no block */
	return 0;
}

int
xm_link(const char *s, size_t n, struct xm_link *l)
{
	size_t i = 0, t, te, fs, fe, h, he, k;

	memset(l, 0, sizeof *l);
	while (i < n && (s[i] == ' ' || s[i] == '\t'))
		i++;
	if (i + 2 >= n || (s[i] != '-' && s[i] != '*' && s[i] != '+') ||
	    (s[i + 1] != ' ' && s[i + 1] != '\t'))
		return 0;
	for (i += 2; i < n && (s[i] == ' ' || s[i] == '\t'); i++)
		;
	if (i >= n || s[i] != '[')
		return 0;
	t = ++i;
	for (te = t; te + 1 < n && s[te] != '\n' &&
	     !(s[te] == ']' && s[te + 1] == '('); te++)
		;
	if (te + 1 >= n || s[te] != ']')
		return 0;
	for (fs = fe = te + 2; fe < n && s[fe] != ')' && s[fe] != '\n'; fe++)
		;
	if (fe >= n || s[fe] != ')' || fe - fs < 4 || fe - fs > 255 ||
	    s[fs] == '.' || memcmp(s + fe - 3, ".md", 3))
		return 0;
	for (k = fs; k < fe; k++)
		if (s[k] == '/' || s[k] == '\\' || (unsigned char)s[k] < 0x20)
			return 0;
	xm_trim(s, &t, &te);
	l->title.s = t;
	l->title.n = te - t;
	l->file.s = fs;
	l->file.n = fe - fs;
	h = fe + 1;
	he = xm_eol(s, n, h);
	xm_trim(s, &h, &he);
	if (he - h >= 3 && (!memcmp(s + h, "\xe2\x80\x94", 3) ||
	                    !memcmp(s + h, "\xe2\x80\x93", 3)))
		h += 3;
	else if (h < he && (s[h] == '-' || s[h] == ':'))
		h++;
	xm_trim(s, &h, &he);
	l->hook.s = h;
	l->hook.n = he - h;
	return 1;
}

int
xm_wrote(const char *s, size_t n)
{
	static const char *const w[] = { "tee", "rm", "mv", "touch", "truncate",
	                                 "sd", "sed -i", "perl -i", "perl -pi",
	                                 NULL };
	size_t i, k, l;

	for (i = 0; i < n; i++)
	{
		if (s[i] == '>' && (!i || s[i - 1] < '0' || s[i - 1] > '9') &&
		    (i + 1 >= n || s[i + 1] != '&'))
			return 1;
		if (i && !strchr(" \t\n;|&(`", s[i - 1]))
			continue;
		for (k = 0; w[k]; k++)
			if ((l = strlen(w[k])) <= n - i && !memcmp(s + i, w[k], l) &&
			    (strchr(w[k], ' ') || i + l == n ||
			     strchr(" \t\n", s[i + l])))
				return 1;
	}
	return 0;
}

/* ---- gathering ----------------------------------------------------------- */

static char *
xm_dup(const char *s, size_t n)
{
	char *d = malloc(n + 1);

	if (d)
	{
		memcpy(d, s, n);
		d[n] = '\0';
	}
	return d;
}

static struct xm_row *
xm_add(struct xm_set *s, const char *id, size_t idn, const char *path,
       const char *scope)
{
	struct xm_row *r, *v;

	if (s->n == s->cap)
	{
		size_t c = s->cap ? s->cap * 2 : 64;

		if (!(v = realloc(s->v, c * sizeof *v)))
			return NULL;
		s->v = v;
		s->cap = c;
	}
	r = &s->v[s->n];
	memset(r, 0, sizeof *r);
	if (!(r->id = xm_dup(id, idn)) || !(r->path = strdup(path)))
	{
		free(r->id);
		return NULL;
	}
	r->scope = scope;
	r->ord = (unsigned long)s->n++;
	return r;
}

static void
xm_free_row(struct xm_row *r)
{
	free(r->id);
	free(r->path);
	free(r->title);
	free(r->note);
}

/* name under dfd, opened never blocking and with no symlink followed;
** AT_FDCWD: an instruction file's path, a symlink followed (a ruling of
** #322's round 2); read when a regular file of at most XM_FILE bytes: up to
** cap bytes, NUL-ended, *len. NULL when not read; *stok when *st is its
** fstat. */
static char *
xm_read(int dfd, const char *name, size_t cap, size_t *len, struct stat *st,
        int *stok)
{
	int fd = openat(dfd, name, O_RDONLY | O_NONBLOCK | O_CLOEXEC |
	                (dfd == AT_FDCWD ? 0 : O_NOFOLLOW));
	char *b = NULL;
	size_t n = 0;
	ssize_t r;

	*stok = 0;
	*len = 0;
	if (fd < 0)
		return NULL;
	*stok = fstat(fd, st) == 0;
	if (*stok && S_ISREG(st->st_mode) && st->st_size <= XM_FILE &&
	    (b = malloc(cap + 1)))
		while (n < cap && ((r = read(fd, b + n, cap - n)) > 0 ||
		                   (r < 0 && errno == EINTR)))
			if (r > 0)
				n += (size_t)r;
	close(fd);
	if (b)
	{
		b[n] = '\0';
		*len = n;
	}
	return b;
}

/* MEMORY.md's links: a row each, its title and hook */
static void
xm_index(struct xm_set *s)
{
	struct xm_link l;
	struct xm_row *r;
	struct stat st;
	char p[PATH_MAX], *b;
	size_t n, i, e;
	int stok;

	if (!(b = xm_read(s->dfd, "MEMORY.md", XM_FILE, &n, &st, &stok)))
		return;
	for (i = 0; i < n; i = e + 1)
	{
		e = xm_eol(b, n, i);
		if (!xm_link(b + i, e - i, &l) ||
		    snprintf(p, sizeof p, "%s/%.*s", s->memdir, (int)l.file.n,
		             b + i + l.file.s) >= (int)sizeof p ||
		    !(r = xm_add(s, b + i + l.file.s, l.file.n - 3, p, "project")))
			continue;
		if (l.title.n)
			r->title = xm_dup(b + i + l.title.s, l.title.n);
		if (l.hook.n)
			r->note = xm_dup(b + i + l.hook.s, l.hook.n);
	}
	free(b);
}

static int
xm_byid(const void *a, const void *b)
{
	const struct xm_row *x = a, *y = b;
	int c = strcmp(x->id, y->id);

	return c ? c : x->ord < y->ord ? -1 : x->ord > y->ord;
}

/* the memory directory beside the transcript: the index's rows, then every
** other *.md in it (never a dot file, a directory or MEMORY.md); one row an
** id, the index's title and hook, the directory's lstat */
static void
xm_memdir(struct xm_set *s, const char *transcript)
{
	const char *sl = strrchr(transcript, '/');
	struct dirent *e;
	struct xm_row *r;
	struct stat st;
	char p[PATH_MAX];
	size_t i, k, l;
	DIR *dp;
	int fd;

	if (!sl || snprintf(s->memdir, sizeof s->memdir, "%.*s/memory",
	                    (int)(sl - transcript), transcript)
	           >= (int)sizeof s->memdir ||
	    (s->dfd = open(s->memdir, O_RDONLY | O_DIRECTORY | O_NOFOLLOW |
	                   O_CLOEXEC)) < 0)
	{
		s->memdir[0] = '\0';
		return;
	}
	xm_index(s);
	if ((fd = dup(s->dfd)) >= 0 && !(dp = fdopendir(fd)))
		close(fd);
	else if (fd >= 0)
	{
		while ((e = readdir(dp)))
		{
			l = strlen(e->d_name);
			if (e->d_name[0] == '.' || l < 4 ||
			    strcmp(e->d_name + l - 3, ".md") ||
			    !strcmp(e->d_name, "MEMORY.md") ||
			    fstatat(s->dfd, e->d_name, &st, AT_SYMLINK_NOFOLLOW) < 0 ||
			    S_ISDIR(st.st_mode) ||
			    snprintf(p, sizeof p, "%s/%s", s->memdir, e->d_name)
			    >= (int)sizeof p ||
			    !(r = xm_add(s, e->d_name, l - 3, p, "project")))
				continue;
			r->st = st;
			r->stok = 1;
		}
		closedir(dp);
	}
	/* one row an id: the index's first line, the directory's stat */
	if (s->n)
		qsort(s->v, s->n, sizeof *s->v, xm_byid);
	for (i = k = 0; i < s->n; i++)
	{
		if (k && !strcmp(s->v[k - 1].id, s->v[i].id))
		{
			if (s->v[i].stok && !s->v[k - 1].stok)
			{
				s->v[k - 1].st = s->v[i].st;
				s->v[k - 1].stok = 1;
			}
			xm_free_row(&s->v[i]);
			continue;
		}
		s->v[k++] = s->v[i];
	}
	s->n = k;
	for (i = 0; i < s->n; i++)
		if (!s->v[i].stok)
			s->v[i].stok = fstatat(s->dfd, strrchr(s->v[i].path, '/') + 1,
			                       &s->v[i].st, AT_SYMLINK_NOFOLLOW) == 0 &&
			               !S_ISDIR(s->v[i].st.st_mode);
}

/* dir/name when it exists and is no directory, a symlink by its target
** (a dangling one by itself): an instruction row under its own path, once
** (the walk can reach the config dir again: xm_undup); 1 */
static int
xm_instr(struct xm_set *s, const char *dir, const char *name,
         const char *scope)
{
	struct xm_row *r;
	struct stat st;
	char p[PATH_MAX];

	if (snprintf(p, sizeof p, "%s%s%s", dir, strcmp(dir, "/") ? "/" : "",
	             name) >= (int)sizeof p ||
	    (stat(p, &st) < 0 && lstat(p, &st) < 0) || S_ISDIR(st.st_mode))
		return 0;
	if (!(r = xm_add(s, p, strlen(p), p, scope)))
		return 0;
	r->instr = r->stok = 1;
	r->st = st;
	return 1;
}

static int
xm_strcmp(const void *a, const void *b)
{
	return strcmp(*(char *const *)a, *(char *const *)b);
}

static int
xm_pathord(const void *a, const void *b)
{
	const struct xm_row *x = *(struct xm_row *const *)a,
	                    *y = *(struct xm_row *const *)b;
	int c = strcmp(x->path, y->path);

	return c ? c : x->ord < y->ord ? -1 : x->ord > y->ord;
}

/* each instruction path once, the first added kept, in place: n log n,
** a rules tree can hold any number of files */
static void
xm_undup(struct xm_set *s)
{
	size_t n = s->n - s->nmem, k, j;
	struct xm_row **p;

	if (n < 2 || !(p = malloc(n * sizeof *p)))
		return;
	for (k = 0; k < n; k++)
		p[k] = &s->v[s->nmem + k];
	qsort(p, n, sizeof *p, xm_pathord);
	for (k = 1; k < n; k++)
		if (!strcmp(p[k]->path, p[k - 1]->path))
			p[k]->instr = 2;	/* a repeat */
	free(p);
	for (j = k = s->nmem; k < s->n; k++)
		if (s->v[k].instr == 2)
			xm_free_row(&s->v[k]);
		else
			s->v[j++] = s->v[k];
	s->n = j;
}

/* a rules directory's *.md, by name, its subdirectories 3 deep; no
** symlinked directory is entered */
static void
xm_rules(struct xm_set *s, const char *dir, const char *scope, int depth)
{
	int fd = open(dir, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
	char **nm = NULL, **v, p[PATH_MAX];
	size_t n = 0, cap = 0, k, l;
	struct dirent *e;
	struct stat st;
	DIR *dp;

	if (fd < 0)
		return;
	if (!(dp = fdopendir(fd)))
	{
		close(fd);
		return;
	}
	while ((e = readdir(dp)))
	{
		if (e->d_name[0] == '.')
			continue;
		if (n == cap)
		{
			cap = cap ? cap * 2 : 16;
			if (!(v = realloc(nm, cap * sizeof *nm)))
				break;
			nm = v;
		}
		if (!(nm[n] = strdup(e->d_name)))
			break;
		n++;
	}
	closedir(dp);
	if (n)
		qsort(nm, n, sizeof *nm, xm_strcmp);
	for (k = 0; k < n; k++)
	{
		l = strlen(nm[k]);
		if (snprintf(p, sizeof p, "%s/%s", dir, nm[k]) < (int)sizeof p &&
		    lstat(p, &st) == 0)
		{
			if (S_ISDIR(st.st_mode) && depth < 3)
				xm_rules(s, p, scope, depth + 1);
			else if (!S_ISDIR(st.st_mode) && l > 3 &&
			         !strcmp(nm[k] + l - 3, ".md"))
				xm_instr(s, dir, nm[k], scope);
		}
		free(nm[k]);
	}
	free(nm);
}

/* The project directories from the cwd up, as prefix lengths of cwd,
** XM_LEVELS at most: Claude every ancestor to "/" (its docs); Codex to the
** first that holds .git, the cwd only when none does (agents_md.rs). The
** count. */
static size_t
xm_walk(const char *cwd, int codex, size_t *lens)
{
	char p[PATH_MAX];
	size_t l = strlen(cwd), k = 0;
	struct stat st;

	if (cwd[0] != '/' || l + 6 >= sizeof p)
		return 0;
	while (l > 1 && cwd[l - 1] == '/')
		l--;
	for (;;)
	{
		lens[k++] = l;
		snprintf(p, sizeof p, "%.*s/.git", (int)(l > 1 ? l : 0), cwd);
		if (codex && lstat(p, &st) == 0)
			return k;
		if (l <= 1 || k == XM_LEVELS)
			break;
		while (l > 1 && cwd[l - 1] != '/')
			l--;
		if (l > 1)
			l--;
	}
	return codex ? 1 : k;
}

static void
xm_instrs(struct xm_set *s, const struct xm_req *q)
{
	size_t lens[XM_LEVELS], k, n = 0;
	char d[PATH_MAX];
	int codex = !strcmp(q->agent, "codex"), any = 0;

	if (codex && xr_config_ok(q->cfg))
	{
		if (!xm_instr(s, q->cfg, "AGENTS.override.md", "user"))
			xm_instr(s, q->cfg, "AGENTS.md", "user");
	}
	else if (!codex)
	{
#ifdef __APPLE__
		xm_instr(s, "/Library/Application Support/ClaudeCode", "CLAUDE.md",
		         "managed");
#else
		xm_instr(s, "/etc/claude-code", "CLAUDE.md", "managed");
#endif
		if (xr_config_ok(q->cfg) &&
		    snprintf(d, sizeof d, "%s/rules", q->cfg) < (int)sizeof d)
		{
			xm_instr(s, q->cfg, "CLAUDE.md", "user");
			xm_rules(s, d, "user", 0);
		}
	}
	if (q->cwd)
		n = xm_walk(q->cwd, codex, lens);
	for (k = n; k-- > 0;)	/* root first: the load order */
	{
		snprintf(d, sizeof d, "%.*s", (int)lens[k], q->cwd);
		if (codex)
		{
			if (!xm_instr(s, d, "AGENTS.override.md", "project"))
				xm_instr(s, d, "AGENTS.md", "project");
			continue;
		}
		any |= xm_instr(s, d, "CLAUDE.md", "project");
		any |= xm_instr(s, d, ".claude/CLAUDE.md", "project");
		any |= xm_instr(s, d, "CLAUDE.local.md", "project");
		if (strlen(d) + 15 < sizeof d)
		{
			strcat(d, strcmp(d, "/") ? "/.claude/rules" : ".claude/rules");
			xm_rules(s, d, "project", 0);
		}
	}
	/* Claude Code reads AGENTS.md when no CLAUDE.md is in force */
	for (k = n; !codex && !any && k-- > 0;)
	{
		snprintf(d, sizeof d, "%.*s", (int)lens[k], q->cwd);
		xm_instr(s, d, "AGENTS.md", "project");
		xm_instr(s, d, ".claude/AGENTS.md", "project");
	}
	xm_undup(s);
}

/* ---- the transcript's tool calls ----------------------------------------- */

static int
xm_bypath(const void *a, const void *b)
{
	return strcmp((*(struct xm_row *const *)a)->path,
	              (*(struct xm_row *const *)b)->path);
}

/* --follow --memory (#446): every touch of a .md path, by path, so a row
** listed later (a memory written before the directory shows it) still gets
** its session and chapter */
struct xm_tch
{
	char *path;
	int how;
	unsigned long long seq;
	char chapter[48];
};

struct xm_live
{
	struct xm_set s;
	struct xm_tch *t;		/* sorted by path */
	size_t tn, tcap;
	unsigned long touched;
	char *agent, *cfg, *path, *cwd;
};

/* shortcut: only .md paths that can be rows, the first XM_TOUCHES of
** them kept; upgrade if a row's file can be named otherwise */
static void
xm_ltouch(struct xm_live *l, char *p, int how, unsigned long long seq,
          const char *chapter)
{
	size_t n = strlen(p), lo = 0, hi = l->tn, m;
	struct xm_tch *v;
	int c = 1;

	if (n < 3 || strcmp(p + n - 3, ".md"))
		return;
	/* only a path that can become a row: under the session's memory
	** directory (a row listed later), or an instruction row by path (the
	** one-shot's xm_bypath match) */
	{
		struct xm_row key, *kp = &key;
		const char *sl = l->path ? strrchr(l->path, '/') : NULL;
		size_t dl = sl ? (size_t)(sl - l->path) : 0;

		key.path = p;
		if (!(dl && !strncmp(p, l->path, dl) &&
		      !strncmp(p + dl, "/memory/", 8)) &&
		    !(l->s.byp &&
		      bsearch(&kp, l->s.byp, l->s.n, sizeof *l->s.byp, xm_bypath)))
			return;
	}
	while (lo < hi)
	{
		m = lo + (hi - lo) / 2;
		if (!(c = strcmp(l->t[m].path, p)))
		{
			lo = m;
			break;
		}
		if (c < 0)
			lo = m + 1;
		else
			hi = m;
	}
	if (c)
	{
		if (l->tn >= XM_TOUCHES)
			return;
		if (l->tn == l->tcap)
		{
			size_t cap = l->tcap ? l->tcap * 2 : 64;

			if (!(v = realloc(l->t, cap * sizeof *v)))
				return;
			l->t = v;
			l->tcap = cap;
		}
		memmove(&l->t[lo + 1], &l->t[lo], (l->tn - lo) * sizeof *l->t);
		memset(&l->t[lo], 0, sizeof *l->t);
		if (!(l->t[lo].path = strdup(p)))
		{
			memmove(&l->t[lo], &l->t[lo + 1], (l->tn - lo) * sizeof *l->t);
			return;
		}
		l->tn++;
	}
	if (how < l->t[lo].how)
		return;
	l->t[lo].how = how;
	l->t[lo].seq = seq;
	snprintf(l->t[lo].chapter, sizeof l->t[lo].chapter, "%s", chapter);
	l->touched++;
}

static void
xm_touch(struct xm_set *s, const char *path, size_t n, int how,
         const char *chapter)
{
	struct xm_row key, *kp = &key, **hit, *r;
	char p[PATH_MAX];

	if (n >= sizeof p)
		return;
	memcpy(p, path, n);
	p[n] = '\0';
	if (s->live)
	{
		xm_ltouch(s->live, p, how, s->seq, chapter);
		return;
	}
	key.path = p;
	if (!(hit = bsearch(&kp, s->byp, s->n, sizeof *s->byp, xm_bypath)) ||
	    how < (r = *hit)->sess)
		return;
	r->sess = how;
	r->seq = s->seq;
	snprintf(r->chapter, sizeof r->chapter, "%s", chapter);
}

static int
xm_term(char c)
{
	return strchr(" \t\r\n'\"`;|&<>()$\\,", c) != NULL;	/* NUL too */
}

/* the first of the rows by path lo..hi (all longer than d, sorted by
** their byte at d) whose byte at d is above c */
static size_t
xm_above(const struct xm_set *s, size_t lo, size_t hi, size_t d, size_t c)
{
	size_t m;

	while (lo < hi)
		if ((unsigned char)s->byp[m = lo + (hi - lo) / 2]->path[d] > c)
			hi = m;
		else
			lo = m + 1;
	return lo;
}

/* xm_above, galloping in from lo (top 0) or from hi (top 1): the cost is
** the log of the rows it drops, so a long shared prefix that sheds a row
** a byte costs O(1) per byte, not two full searches (#355) */
static size_t
xm_gallop(const struct xm_set *s, size_t lo, size_t hi, size_t d, size_t c,
          int top)
{
	size_t g = 1;

	if (top)
	{
		while (g <= hi - lo &&
		       (unsigned char)s->byp[hi - g]->path[d] > c)
			g *= 2;
		return xm_above(s, g <= hi - lo ? hi - g + 1 : lo, hi - g / 2, d,
		                c);
	}
	while (g <= hi - lo && (unsigned char)s->byp[lo + g - 1]->path[d] <= c)
		g *= 2;
	return xm_above(s, lo + g / 2, g <= hi - lo ? lo + g - 1 : hi, d, c);
}

/* a shell command: each row whose full path it names, followed by a
** terminator; wrote or loaded by xm_wrote */
static void
xm_shell(struct xm_set *s, const char *t, size_t n, const char *chapter)
{
	int how = xm_wrote(t, n) ? XM_WROTE : XM_LOADED;
	size_t ml = strlen(s->memdir), i, j, k, pl, lo, hi;

	/* a memory row: the directory, '/', a name up to a terminator */
	for (i = 0; ml && i + ml < n; i++)
		if (t[i] == s->memdir[0] && !memcmp(t + i, s->memdir, ml) &&
		    t[i + ml] == '/')
		{
			for (j = i + ml + 1; j < n && !xm_term(t[j]); j++)
				;
			xm_touch(s, t + i, j - i, how, chapter);
			i = j - 1;
		}
	/* an instruction row: from each '/', the rows by path narrowed byte
	** by byte to those the text goes on with; a path that ends there
	** followed by a terminator is named. Length x log rows, not rows x
	** length (#350) */
	for (i = 0; i < n; i++)
		for (lo = 0, hi = t[i] == '/' && s->byp ? s->n : 0, pl = 0;
		     lo < hi; pl++)
		{
			for (; lo < hi && !s->byp[lo]->path[pl]; lo++)
				if ((size_t)(s->byp[lo] - s->v) >= s->nmem &&
				    (i + pl == n || xm_term(t[i + pl])))
					xm_touch(s, t + i, pl, how, chapter);
			if (i + pl == n)
				break;
			k = (unsigned char)t[i + pl];
			lo = xm_gallop(s, lo, hi, pl, k - 1, 0);
			hi = xm_gallop(s, lo, hi, pl, k, 1);
		}
}

/* an apply_patch body: its files, wrote; relative ones against cwd */
static void
xm_patch(struct xm_set *s, const char *t, size_t n, const char *cwd,
         const char *chapter)
{
	static const char *const h[] = { "*** Add File: ", "*** Update File: ",
	                                 "*** Delete File: ", "*** Move to: ",
	                                 NULL };
	char p[PATH_MAX];
	size_t i, e, a, l;
	int k;

	for (i = 0; i < n; i = e + 1)
	{
		e = xm_eol(t, n, i);
		for (k = 0; h[k]; k++)
			if (e - i > (l = strlen(h[k])) && !memcmp(t + i, h[k], l))
				break;
		if (!h[k])
			continue;
		a = i + l;
		l = e;
		xm_trim(t, &a, &l);
		if (a < l && t[a] == '/')
			xm_touch(s, t + a, l - a, XM_WROTE, chapter);
		else if (a < l && cwd[0] == '/' &&
		         snprintf(p, sizeof p, "%s/%.*s", cwd, (int)(l - a), t + a)
		         < (int)sizeof p)
			xm_touch(s, p, strlen(p), XM_WROTE, chapter);
	}
}

/* a string token decoded (malloc'd, NUL-ended), else NULL */
static char *
xm_str(const struct json_parser *p, int t)
{
	char *b;

	if (t < 0 || p->tok[t].kind != JSON_STRING ||
	    !(b = malloc((size_t)(p->tok[t].end - p->tok[t].start) + 1)))
		return NULL;
	if (dch_json_decode(p, t, b, (size_t)(p->tok[t].end - p->tok[t].start)
	                    + 1) < 0)
	{
		free(b);
		return NULL;
	}
	return b;
}

/* every string token in (from, to), decoded, '\n'-joined; NULL for none
** (the 1-byte "" would hide from the analyzer that strlen() bounds it) */
static char *
xm_strings(const struct json_parser *p, int from, int to)
{
	size_t n = 1, l = 0, sl;
	char *b, *s;
	int k;

	for (k = from + 1; k < to; k++)
		if (p->tok[k].kind == JSON_STRING)
			n += (size_t)(p->tok[k].end - p->tok[k].start) + 1;
	if (!(b = malloc(n)))
		return NULL;
	for (k = from + 1; k < to; k++)
		if ((s = xm_str(p, k)))
		{
			sl = strlen(s);
			memcpy(b + l, s, sl);
			l += sl;
			b[l++] = '\n';
			free(s);
		}
	if (!l)
	{
		free(b);
		return NULL;
	}
	b[l] = '\0';
	return b;
}

/* a Codex call's text: a custom tool's input; a function call's arguments
** string, its strings when it is JSON; a local shell's action strings */
static char *
xm_codex(const struct json_parser *p, const char *type)
{
	struct json_parser q;
	char *a, *t;
	int v;

	if (!strcmp(type, "custom_tool_call"))
		return xm_str(p, dch_json_field(p, 0, "input"));
	if (!strcmp(type, "local_shell_call"))
		return (v = dch_json_field(p, 0, "action")) >= 0
		       ? xm_strings(p, v, p->tok[v].next) : NULL;
	if (!(a = xm_str(p, dch_json_field(p, 0, "arguments"))))
		return NULL;
	memset(&q, 0, sizeof q);
	if (dch_json_parse(&q, a) < 0 || !(t = xm_strings(&q, -1, q.ntok)))
		t = NULL;
	dch_json_free(&q);
	if (!t)
		return a;
	free(a);
	return t;
}

static void
xm_call(void *ctx, const char *name, const char *json, size_t n,
        const char *chapter, const char *cwd)
{
	struct xm_set *s = ctx;
	struct json_parser p;
	char *b = xm_dup(json, n), *t = NULL;
	int w;

	memset(&p, 0, sizeof p);
	if (!b || !name || dch_json_parse(&p, b) < 0 ||
	    p.tok[0].kind != JSON_OBJECT)
		goto out;
	s->seq++;
	w = !strcmp(name, "Write") || !strcmp(name, "Edit") ||
	    !strcmp(name, "MultiEdit");
	if (w || !strcmp(name, "Read"))
	{
		if ((t = xm_str(&p, dch_json_field(&p, 0, "file_path"))))
			xm_touch(s, t, strlen(t), w ? XM_WROTE : XM_LOADED, chapter);
	}
	else if (!strcmp(name, "Bash"))
	{
		if ((t = xm_str(&p, dch_json_field(&p, 0, "command"))))
			xm_shell(s, t, strlen(t), chapter);
	}
	else if ((!strcmp(name, "function_call") ||
	          !strcmp(name, "custom_tool_call") ||
	          !strcmp(name, "local_shell_call")) && (t = xm_codex(&p, name)))
	{
		if (strstr(t, "*** Begin Patch"))
			xm_patch(s, t, strlen(t), cwd, chapter);
		else
			xm_shell(s, t, strlen(t), chapter);
	}
out:
	free(t);
	dch_json_free(&p);
	free(b);
}

/* s->byp: the rows by path again (none: NULL, no row matched by path) */
static int
xm_bypaths(struct xm_set *s)
{
	size_t k;

	free(s->byp);
	if (!(s->byp = malloc((s->n ? s->n : 1) * sizeof *s->byp)))
		return -1;
	for (k = 0; k < s->n; k++)
		s->byp[k] = &s->v[k];
	qsort(s->byp, s->n, sizeof *s->byp, xm_bypath);
	return 0;
}

/* the transcript decoded for the rows' touches: 1 when its read range may
** have dropped records (or it could not be read), else 0 */
static int
xm_scan(struct xm_set *s, const struct xm_req *q)
{
	struct xr_chapters ch = {0};
	struct xr_claude *d;
	int fd, capped = 0, rc;

	if (xm_bypaths(s) < 0)
		return 1;
	if ((fd = xr_seam_open(q->path, q->st)) < 0)
		return 1;
	if (!(d = xr_claude_new()))
	{
		close(fd);
		return 1;
	}
	if (!strcmp(q->agent, "codex"))
		xr_claude_codex(d);
	xr_claude_calls(d, xm_call, s, (uint64_t)q->st->st_ino);
	rc = xr_claude_read(d, fd, (uint64_t)q->st->st_size, &capped);
	close(fd);
	xr_claude_end(d, (uint64_t)q->st->st_ino, &ch);
	xr_chapters_free(&ch);
	xr_claude_free(d);
	return rc < 0 || capped;
}

/* ---- the answer ---------------------------------------------------------- */

/* session rows first, newest touch first; then newest mtime first */
static int
xm_order(const void *a, const void *b)
{
	const struct xm_row *x = a, *y = b;

	if (!x->sess != !y->sess)
		return x->sess ? -1 : 1;
	if (x->seq != y->seq)
		return x->seq > y->seq ? -1 : 1;
	if (x->stok != y->stok)
		return x->stok ? -1 : 1;
	if (x->stok && x->st.st_mtime != y->st.st_mtime)
		return x->st.st_mtime > y->st.st_mtime ? -1 : 1;
	return strcmp(x->id, y->id);
}

static void
xm_title(FILE *m, const char *s, size_t n)
{
	char t[XR_TITLE_MAX * 4 + 1];

	xr_title(s, n, t, sizeof t);
	xr_put_str(m, t);
}

/* one row's line, under XM_LINE: the body halved (cut) until it fits */
static int
xm_row_line(FILE *f, struct xm_set *s, struct xm_row *r)
{
	static const char *const kinds[] = { "user", "feedback", "project",
	                                     "reference", NULL };
	char t[XR_TITLE_MAX * 4 + 1], iso[32], *b = NULL, *line = NULL;
	const char *kind = r->instr ? "instructions" : NULL;
	struct xm_front fm;
	struct stat st;
	struct tm tm;
	size_t n = 0, ll = 0;
	int stok = 0, cut = 0, k, rc;
	FILE *m;

	/* a file the listing saw as not regular is never opened */
	if ((!r->stok || S_ISREG(r->st.st_mode)) && (r->instr || s->dfd >= 0))
		b = xm_read(r->instr ? AT_FDCWD : s->dfd,
		            r->instr ? r->path : strrchr(r->path, '/') + 1,
		            XM_BODY + 1, &n, &st, &stok);
	if (stok)
	{
		r->st = st;
		r->stok = 1;
	}
	if (b && n > XM_BODY)
	{
		for (n = XM_BODY; n && ((unsigned char)b[n] & 0xc0) == 0x80; n--)
			;
		b[n] = '\0';
		cut = 1;
	}
	memset(&fm, 0, sizeof fm);
	if (b && !r->instr)
		(void)xm_front(b, n, &fm);
	for (k = 0; fm.type.n && kinds[k]; k++)
		if (strlen(kinds[k]) == fm.type.n &&
		    !memcmp(b + fm.type.s, kinds[k], fm.type.n))
			kind = kinds[k];
	for (;;)
	{
		if (!(m = open_memstream(&line, &ll)))
		{
			free(b);
			return -1;
		}
		fputs("{\"memory\":{\"id\":", m);
		xr_put_str(m, r->id);
		fputs(",\"title\":", m);
		t[0] = '\0';
		if (r->instr)
			xr_title(r->path, strlen(r->path), t, sizeof t);
		if (!t[0] && r->title)
			xr_title(r->title, strlen(r->title), t, sizeof t);
		if (!t[0] && fm.name.n)
			xr_title(b + fm.name.s, fm.name.n, t, sizeof t);
		if (!t[0])
			xr_title(r->id, strlen(r->id), t, sizeof t);
		xr_put_str(m, t);
		fputs(",\"note\":", m);
		if (r->note)
			xm_title(m, r->note, strlen(r->note));
		else if (fm.desc.n)
			xm_title(m, b + fm.desc.s, fm.desc.n);
		else
			fputs("null", m);
		fputs(",\"kind\":", m);
		if (kind)
			xr_put_str(m, kind);
		else
			fputs("null", m);
		fputs(",\"scope\":", m);
		xr_put_str(m, r->scope);
		if (r->stok && gmtime_r(&r->st.st_mtime, &tm) &&
		    strftime(iso, sizeof iso, "%Y-%m-%dT%H:%M:%SZ", &tm))
			fprintf(m, ",\"updated\":\"%s\"", iso);
		else
			fputs(",\"updated\":null", m);
		fputs(",\"path\":", m);
		xr_put_str(m, r->path);
		if (r->sess)
		{
			fprintf(m, ",\"session\":\"%s\",\"chapter\":",
			        r->sess == XM_WROTE ? "wrote" : "loaded");
			if (r->chapter[0])
				xr_put_str(m, r->chapter);
			else
				fputs("null", m);
		}
		else
			fputs(",\"session\":null,\"chapter\":null", m);
		fputs(",\"body\":", m);
		if (b)
			xr_put_str(m, b);
		else
			fputs("null", m);
		fputs(cut ? ",\"cut\":true}}\n" : "}}\n", m);
		if (fclose(m) != 0)
		{
			free(line);
			free(b);
			return -1;
		}
		if (ll <= XM_LINE || !b)
			break;
		free(line);
		line = NULL;
		/* the line holds escapes: halve the body; nothing left is null */
		for (n /= 2; n && ((unsigned char)b[n] & 0xc0) == 0x80; n--)
			;
		b[n] = '\0';
		cut = 1;
		if (!n)
		{
			free(b);
			b = NULL;
		}
	}
	rc = fwrite(line, 1, ll, f) == ll ? 0 : -1;
	free(line);
	free(b);
	return rc;
}

/* the rows: a Claude's memory directory, then the instruction files */
static void
xm_rows(struct xm_set *s, const struct xm_req *q)
{
	if (!q->agent)
		return;
	if (q->path && !strcmp(q->agent, "claude"))
		xm_memdir(s, q->path);
	s->nmem = s->n;
	xm_instrs(s, q);
}

/* the header, the rows in their order, the done line */
static int
xm_emit(FILE *f, struct xm_set *s, const struct xm_req *q, int partial)
{
	size_t k, cnt = 0;
	int rc = 0;

	if (s->nmem)
		qsort(s->v, s->nmem, sizeof *s->v, xm_order);
	fprintf(f, "{\"schema\":1,\"source\":\"%s\",\"title\":null,"
	        "\"partial\":%s", s->n ? q->agent : "none",
	        partial ? "true" : "false");
	if (q->warn && q->warn[0])
	{
		fputs(",\"warn\":", f);
		xr_put_str(f, q->warn);
	}
	fputs("}\n", f);
	for (k = 0; k < s->n && k < XM_ROWS && rc == 0; k++, cnt++)
		rc = xm_row_line(f, s, &s->v[k]);
	if (rc == 0)
		fprintf(f, "{\"done\":true,\"count\":%zu,\"dropped\":%zu}\n", cnt,
		        s->n - cnt);
	if (fflush(f) != 0 || ferror(f))
		rc = -1;
	return rc;
}

/* the rows gone; the touches' count (seq) and live kept */
static void
xm_clear(struct xm_set *s)
{
	size_t k;

	for (k = 0; k < s->n; k++)
		xm_free_row(&s->v[k]);
	free(s->v);
	free(s->byp);
	if (s->dfd >= 0)
		close(s->dfd);
	s->v = NULL;
	s->byp = NULL;
	s->n = s->cap = s->nmem = 0;
	s->dfd = -1;
	s->memdir[0] = '\0';
}

int
xm_print(FILE *f, const struct xm_req *q)
{
	struct xm_set s;
	int partial = 0, rc;

	memset(&s, 0, sizeof s);
	s.dfd = -1;
	xm_rows(&s, q);
	if (s.n && q->path)
		partial = xm_scan(&s, q);
	rc = xm_emit(f, &s, q, partial);
	xm_clear(&s);
	return rc;
}

/* ---- --follow --memory (#446) -------------------------------------------- */

static void
xm_live_rows(struct xm_live *l)
{
	struct xm_req q = { l->agent, l->cfg, l->path, NULL, l->cwd, NULL };

	xm_clear(&l->s);
	xm_rows(&l->s, &q);
	(void)xm_bypaths(&l->s);	/* none: no instruction row by a shell */
}

struct xm_live *
xm_live_new(const struct xm_req *q)
{
	struct xm_live *l = calloc(1, sizeof *l);

	if (!l)
		return NULL;
	l->s.dfd = -1;
	l->s.live = l;
	if ((q->agent && !(l->agent = strdup(q->agent))) ||
	    !(l->cfg = strdup(q->cfg ? q->cfg : "")) ||
	    (q->path && !(l->path = strdup(q->path))) ||
	    !(l->cwd = strdup(q->cwd ? q->cwd : "")))
	{
		xm_live_free(l);
		return NULL;
	}
	xm_live_rows(l);
	return l;
}

void
xm_live_free(struct xm_live *l)
{
	size_t k;

	if (!l)
		return;
	xm_clear(&l->s);
	for (k = 0; k < l->tn; k++)
		free(l->t[k].path);
	free(l->t);
	free(l->agent);
	free(l->cfg);
	free(l->path);
	free(l->cwd);
	free(l);
}

void
xm_live_attach(struct xm_live *l, struct xr_claude *d, uint64_t ino)
{
	xr_claude_calls(d, xm_call, &l->s, ino);
}

unsigned long
xm_live_touched(const struct xm_live *l)
{
	return l ? l->touched : 0;
}

static int
xm_tchpath(const void *k, const void *e)
{
	return strcmp(k, ((const struct xm_tch *)e)->path);
}

int
xm_live_print(FILE *f, struct xm_live *l, int partial, const char *warn)
{
	struct xm_req q = { l ? l->agent : NULL, "", NULL, NULL, "", warn };
	const struct xm_tch *h;
	struct xm_set none;
	size_t k;
	int rc;

	if (!l)
	{
		memset(&none, 0, sizeof none);
		none.dfd = -1;
		return xm_emit(f, &none, &q, 0);
	}
	xm_live_rows(l);
	for (k = 0; k < l->s.n; k++)
		if (l->tn && (h = bsearch(l->s.v[k].path, l->t, l->tn, sizeof *l->t,
		                          xm_tchpath)))
		{
			l->s.v[k].sess = h->how;
			l->s.v[k].seq = h->seq;
			snprintf(l->s.v[k].chapter, sizeof l->s.v[k].chapter, "%s",
			         h->chapter);
		}
	rc = xm_emit(f, &l->s, &q, partial && l->s.n);
	(void)xm_bypaths(&l->s);	/* xm_emit's order moved the rows */
	return rc;
}
