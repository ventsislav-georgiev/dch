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
	long at;		/* when the host answered, epoch seconds */
	int stale;		/* the host did not answer this time */
};
int dch_remote_set_hosts(const char *list);
int dch_remote_rows(struct dch_rrow **out, int now);
void dch_remote_stop(void);
int dch_remote_attach(const char *host, const char *name);
void dch_remote_esc(char *out, size_t cap, const char *s);

#endif
