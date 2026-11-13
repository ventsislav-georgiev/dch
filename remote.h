/*
** The dch client for remote hosts (#087), remote.c: what dch.c and the
** Windows client (win/, #077) call. A row is one session on another host,
** every field as the peer sent it: print it only through dch_remote_esc.
*/
#ifndef DCH_REMOTE_H
#define DCH_REMOTE_H

#include <stddef.h>

#define DCH_RHOST_MAX 64
struct dch_rrow
{
	char host[DCH_RHOST_MAX];	/* its host's label */
	char name[256], alias[256];
	char state[16];		/* working|idle|blocked|done, or "" */
	char cwd[512];		/* its directory, "" unknown (#115) */
	long at;		/* when the host answered, epoch seconds */
	int stale;		/* the host did not answer this time */
};
int dch_remote_set_hosts(const char *list);
const char *dch_remote_hosts(void);	/* --hosts, or NULL */
int dch_remote_rows(struct dch_rrow **out, int now);
/* A remote host's ended session (#115): one record of its `dch --history
** --json`, the fields as the peer sent them (print them only through
** dch_remote_esc); json is the whole record, a JSON object with every C1,
** DEL and bidi control written as a \u escape, never a "host" key. */
struct dch_rrec
{
	char host[DCH_RHOST_MAX];
	char name[256], file[300], alias[256], aname[256], end[64];
	char cwd[512], argv[512], sid[64];
	long when;		/* it ended, else last updated; 0 unknown */
	long at;		/* when the host answered */
	int pinned, stale;
	char *json;
};
/* Every host's records, each host's in its order (newest first): now,
** fetched in the foreground within the --ls --remote box; else the
** picker's, from the cache the background probe keeps. */
int dch_remote_history(struct dch_rrec **out, int now);
void dch_remote_hfree(struct dch_rrec *v, int n);
void dch_remote_stop(void);
/* The picker's History tab shows (1) or not: its remote records are
** re-fetched behind it only then, after one fetch at its open (#163). */
void dch_remote_hist_tab(int shown);
/* dch -a host:name's exit status (#102): 0 detached or the session ended,
** 1 the link was lost or the host is down or busy (a retry may work), else
** one of these, where a retry will not help: */
#define DCH_EXIT_REFUSED 4	/* the host's gate refused us (a coded 0x13) */
#define DCH_EXIT_TAKEN 5	/* another client attached ("taken") */
#define DCH_EXIT_UNSUPPORTED 6	/* a ctl verb the host's serve lacks (#114) */
#define DCH_EXIT_UNREACHABLE 7	/* a ctl's host could not be reached (#114) */
/* host is a named host (--hosts, remote_hosts) or a cached row's. */
int dch_remote_on(void);
int dch_remote_known(const char *host);
int dch_remote_attach(const char *host, const char *name);
/* A control verb on a remote host (#114): argv is the verb's own (argv[0] is
** the verb), its names host:name when host is NULL. Runs it there through
** dch serve's ctl op, its output onto ours: the remote dch's exit status
** (0-3), else 1 (bad request, refused, unknown host), 4, 6 or 7. */
int dch_remote_ctl(const char *host, int argc, char **argv, const char *cwd);
/* dch --xray host:name --requests --json (#158, spec #832 4.3): one warm
** ctl connection to host, X-ray requests on name read off stdin as NDJSON,
** each answer wrapped on stdout. 0 at stdin EOF; else no connection: 1, 4,
** 6 or 7, said on stderr and as one {"schema":1,"exit":N,...} line. */
int dch_remote_requests(char *n);
/* dch --ls-json [--remote] --follow (#103): the list, then again on each
** change, until SIGINT, SIGTERM or our stdout closes: -1 then (exit 0), -2
** out of memory (exit 1). Each array is printed by a forked child, in
** which this returns the remote rows' count and *out the rows (with
** remote): print the --ls-json array to stdout and exit. POSIX only. */
int dch_remote_follow(int remote, struct dch_rrow **out);
void dch_remote_esc(char *out, size_t cap, const char *s);
void keepalive(int fd);	/* TCP keepalive (about 30 s) and no Nagle */

/* Attach replay scrollback (#101): rows of the session's history sent above
** the screen. --scrollback sets dch_replay_sb (-1: not given, then
** DCH_REPLAY_SCROLLBACK); dch_scrollback_num reads one value, -1 if it is
** not a count. Both clamp to 0..DCH_SCROLLBACK_MAX. */
#define DCH_SCROLLBACK_MAX 10000
extern int dch_replay_sb;
int dch_scrollback_num(const char *s);
int dch_replay_scrollback(void);

#endif
