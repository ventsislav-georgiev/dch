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
};

enum { PK_UP = 256, PK_DOWN, PK_RIGHT, PK_LEFT, PK_BTAB, PK_PGUP, PK_PGDN,
       PK_HOME, PK_END, PK_ESC };

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
int pk_open(struct pk *p, int remote, int vt);
int pk_run(struct pk *p, int fd, const char *header, char *out,
           size_t outsz);

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
#define NCONF 5
extern const struct dch_conf config_keys[];
const char *config_show(int k, char *v, size_t cap, int *set);
#endif

#endif
