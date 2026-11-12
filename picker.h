/*
** The session picker (#075), picker.c: its rows, its drawing, its keys and
** its loop, one implementation for dch (dch.c hosts it on a POSIX tty) and
** dch.exe (win/ hosts it on the Windows console, #090). What differs is the
** host's: where the rows come from, what a key does past moving about, and
** the terminal underneath.
*/
#ifndef DCH_PICKER_H
#define DCH_PICKER_H

#include <signal.h>
#include <stdio.h>
#include <sys/types.h>

#include "remote.h"

struct pkrow
{
	char name[600];		/* the session; History: the record's file */
	char sname[512];	/* History: the record's session name */
	char title[600];	/* alias, else harness name, else name */
	char state[32];		/* blocked|working|done|idle; History: its end */
	char prs[400], cwd[300];
	long when, merged;	/* last output; History: when it ended */
	int agents, pinned;
	int remote;		/* host:name (#087): 1, 2 when stale */
};

struct pkact
{
	char *argv[12];		/* dch's argv, into buf */
	char buf[1600];
	char doing[700];	/* "killing alpha", while it runs */
	char target[600];	/* the row it is about; "" = a bulk action */
	int tab, revive, nomsg;
	int go;			/* a revive: attach to it once it is up (#132) */
	char host[DCH_RHOST_MAX];	/* a revive on this host (#115) */
};

struct pk
{
	struct pkrow *r[3];	/* Sessions, Sidekicks, History */
	int n[3], sel[3], top[3];
	int tab, byname, color, interrupted, win, status_row, asking;
	int settings, cfsel, cftop;	/* the settings view (#082) */
	int askcfg;		/* asking about config key askcfg - 1 */
	long long loaded;	/* now_ms of the last load; 0 = load now */
	struct pkact ask, run;	/* the action asked about, the one running */
	pid_t pid;
	FILE *out;		/* the running one's stdout and stderr */
	char status[800];
	char filt[256];		/* the "/" filter (#096); "" = every row */
	int filtering;		/* its prompt is open: keys type into it */
	char go[600];		/* a revive's session, up: pk_run attaches it */
	int opens;		/* the host attaches the pick (dch -l, the
				** switch): History's o / Shift+Enter may */
};

enum { PK_UP = 256, PK_DOWN, PK_RIGHT, PK_LEFT, PK_BTAB, PK_PGUP, PK_PGDN,
       PK_HOME, PK_END, PK_ESC, PK_SENTER /* Shift+Enter (#132) */ };

/* picker.c */
extern volatile sig_atomic_t pk_winch;	/* the host saw a resize */
extern size_t pk_pn;	/* 0: the next frame is written whole */
unsigned ap_cp(const char *p, int *len);
int ap_cells(unsigned u);
int pk_find(const struct pk *p, int t, const char *name);
void pk_selnames(const struct pk *p, char sel[3][600]);
void pk_sort(struct pk *p, char sel[3][600]);
void pk_rrow(struct pkrow *w, const struct dch_rrow *r);
int pk_cfbyname(void);
int pk_nav(struct pk *p, int c);
int pk_csi(const unsigned char *s, int n);
/* The session just left (host:name for a remote), selected on its tab by
** the next pk_open, which also keeps the last picker's place (#098). */
void pk_focus(const char *name);
/* Or a History record's file (#133): the next pk_open opens on History,
** that row selected when it is there ("" for none). */
void pk_focus_history(const char *file);
int pk_open(struct pk *p, int remote, int vt);
int pk_run(struct pk *p, int fd, const char *header, char *out,
           size_t outsz);

/* The renderer (picker.c), shared with the X-ray view (#175, xview.c): a
** frame is pk_begin, lines (pk_line ... pk_eol) and pk_fend, which writes
** only what changed. pk_txt draws a control or bidi code point as '?'. */
enum { PK_ACC, PK_ACCB, PK_RED, PK_GRN, PK_WARN, PK_SEL, PK_CHIP, PK_NPAL };
/* A key hint: the key, what it does, and a value in the accent ("sort
** name"). group: 0 the actions, 1 the agents', 2 the view, 3 moving about;
** warn: the key is a destructive one's. */
struct pkkey
{
	const char *key, *label, *val;
	int group, warn;
};
extern int pk_col, pk_w, pk_h, pk_utf;
extern const char *pk_bg;
const char *pk_p(int i);
int pk_colors(int vt);
void pk_tab(const char *name, int n, int on);
void pk_begin(int fd, int color);
void pk_fend(int fd, int force);
void pk_flush(int fd, int force);
void pk_rawn(const char *s, size_t n);
void pk_raw(const char *s);
void pk_set(const char *a);
void pk_txtto(const char *s, int lim);
void pk_txt(const char *s);
int pk_cps(const char *s);
void pk_pad(int end);
void pk_fit(const char *s, int w);
void pk_line(void);
void pk_eol(void);
void pk_rule(void);
int pk_keys(const struct pkkey *it, int n, unsigned drop, int cap, int draw);
int pk_key(int fd);

/* The host's: dch.c, or win/. pk_load: every tab's rows afresh. pk_do: one
** key, 1 when out holds the session to attach, -1 to quit, else 0.
** pk_reap: a running action's end. The terminal: pk_wait, input within ms
** (1), none (0), cut short by a resize (-1); pk_rd, the next byte (0), or
** -1 at its end; pk_size, its columns and rows (0 when unknown); pk_write,
** a frame. */
void pk_load(struct pk *p);
int pk_do(struct pk *p, int fd, int c, char *out, size_t outsz);
void pk_reap(struct pk *p);
int pk_wait(int fd, long ms);
int pk_rd(int fd, unsigned char *c);
void pk_size(int fd, int *cols, int *rows);
void pk_write(int fd, const void *b, size_t n);

#ifndef _WIN32
/* dch.c's config keys: `dch --config`'s and the settings view's (#082),
** NCONF of them (dch.c asserts it). */
struct dch_conf
{
	const char *key;
	int is_bool;
	const char *def;		/* the value when the key is not set */
	const char *choices;	/* "a|b": all it takes; NULL: any one line */
	const char *desc;	/* the picker's settings view says it (#086) */
};
#define NCONF 7
extern const struct dch_conf config_keys[];
const char *config_show(int k, char *v, size_t cap, int *set);

/* The X-ray view (#175), xview.c: `dch --xray <n>` and the picker's X, on
** fd, a terminal the caller made raw and put on the alternate screen. 0 when
** the user left it, else dch's exit code with err set (the session's
** answer: "dch: no such session"). xv_kill: its children signalled, from a
** signal handler too. */
int xv_run(int fd, const char *n, char *err, size_t errsz);
void xv_kill(void);
extern int xv_tab;	/* the tab it opens on: 0 Chapters, 2 On screen */
/* dch.c's, for it: xray_main(ac, av) in a forked child with stdout and
** stderr into *out and *err (never exec, never the terminal fd; av[0]
** "--read": ctl's --read host:name in its place, #188); its pid, or -1. A
** live session's visible screen, plain, into buf (cap bytes and a
** NUL): its length, -1 none, -2 no mirror (dch-lite). */
pid_t xv_spawn(int fd, int ac, char **av, int *out, int *err);
int xv_screen(const char *name, char *buf, size_t cap);
/* A remote view (#188): xv_remote, n is host:name; xv_plain, the host's
** `--read <name> --ansi` bytes as plain text, malloc'd, or NULL. */
int xv_remote(const char *n);
char *xv_plain(const char *raw, size_t n);
#endif

#endif
