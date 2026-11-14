/*
** Remote frames (Prosper's protocol 1.0): what dch serve (serve.c) and the
** dch client for remote hosts (remote.c) share. rframe.c. Spec:
** ventsislav-georgiev/prosper docs/protocol/remote-frames.md.
*/
#ifndef DCH_RFRAME_H
#define DCH_RFRAME_H

#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include "bridge.h"

#define RF_HDR 5		/* type, then a u32 big-endian length */
#define RF_GUARD (256u << 20)	/* spec receive guard: a longer length closes */
#define RF_SEND_MAX (8u << 20)	/* spec send limit, the phone's maxFrame */
/* Our guard for the frames we buffer whole: the JSON control requests of
** the core subset, none of which needs more than a few hundred bytes. 0x10
** streams into the pty and every other type streams to nowhere, so no frame
** is ever held whole past this, whatever its length (up to RF_GUARD). */
#define RF_CTRL_MAX 65536
/* The client's guard for a server reply held whole (0x11 list, 0x18 hello,
** 0x12, 0x13, 0x14): a list of thousands of sessions. A server may send up
** to RF_SEND_MAX; past this one the client drops the host.
** ponytail: 1 MiB, raise toward RF_SEND_MAX if a real list ever nears it. */
#define RF_LIST_MAX (1u << 20)
#define SB_LIMIT (16u << 20)	/* any one growable buffer */
#define NODE_ID_MAX 64		/* a tailcfg.StableNodeID; longer is junk */
#define NAME_CAP 255		/* a session name a peer may send */
#define DASH_MSG "a session name starting with - can't be passed to dch"

enum { RF_NEED, RF_FRAME, RF_CHUNK, RF_CLOSE };

struct rf_state
{
	uint32_t left;		/* payload bytes still to come, streamed frame */
	unsigned char type;	/* its type */
	unsigned char from_server;	/* set: parse a server's frames */
};

struct rf_frame
{
	unsigned char type;
	const unsigned char *data;
	size_t len;
	int end;		/* the frame is complete with this piece */
};

int rf_parse(struct rf_state *st, const unsigned char *b, size_t n,
             size_t room, struct rf_frame *f, size_t *used);

struct sb
{
	char *p;
	size_t n, cap;
	int bad;		/* out of memory or past SB_LIMIT */
};

int sb_room(struct sb *b, size_t k);
void sb_add(struct sb *b, const void *d, size_t k);
void sb_s(struct sb *b, const char *s);
void sb_q(struct sb *b, const char *s);
void sb_ll(struct sb *b, long long v);

int tok_int(const struct json_parser *jp, int t, long long *v);
int jstr(const struct json_parser *jp, int obj, const char *key, char *out,
         size_t cap);
int jint(const struct json_parser *jp, int obj, const char *key,
         long long *v);
int jcont(const struct json_parser *jp, int obj, const char *key,
          enum json_kind kind);
int jnode(const struct json_parser *jp, int obj, const char *key, char *out);
int has_tags(const struct json_parser *jp, int obj);
int status_self(const char *js, long long *uid, char *node, int *tagged);
int whois_user(const char *js, long long *uid, char *node, char *login,
               size_t lcap, int *tagged);

extern volatile sig_atomic_t g_stop;	/* set: subprocesses are cut short */
long long now_ms(void);
int utf8_at(const unsigned char *s, unsigned *cp);
int cp_ctl(unsigned cp);
void logsafe(char *out, size_t cap, const char *s);
void copy_str(char *dst, size_t cap, const char *src);

int cloexec(int fd);
int nonblock(int fd);
int pipe_ce(int p[2]);
void exec_prep(void);
char *ctl_text_read(int fd, char b[257]);
int rf_run(char *const argv[], char **out, size_t *outn, size_t cap,
           char *err, size_t errcap, int timeout_ms);

/* ctl (remote frames 1.1, #114): the dch verbs dch serve runs for a 0x30,
** one allowlist (rframe.c) that the server refuses by and the clients check
** before they send. ctl_check: the CTL_* flags of argv[0..argc) (dch's argv
** without the program, host-local names), each element's CK_* in kind[argc];
** or -1 with why in why[cap]. Pure. */
#define CTL_CREATE 1		/* needs "create" in ops (--allow-create) */
#define CTL_LONG 2		/* the 660 s timeout, not 60 s */
#define CTL_MS 60000		/* a ctl's timeout (#114); a client waits on a
				** silent host this long and RM_ATTACH_MS */
#define CTL_FOREVER 4		/* no timeout (--follow) */
#define CTL_NOCAP 32		/* no output cap (--xray --follow, 4.4) */
#define CTL_ARGV_MAX 1024
enum { CK_VERB = 1, CK_FLAG, CK_VALUE, CK_NAME, CK_TEXT };
int ctl_check(int argc, char *const argv[], unsigned char *kind, char *why,
              size_t cap);
int ctl_known(const char *verb);
/* X-ray's value rules (spec #832 3.1), one copy for dch --xray and the ctl
** allowlist: --scope's enum; a --chapter anchor, [A-Za-z0-9:_.-]{1,128};
** --budget-ms as ASCII digits only (no sign, no 0x, no wrap), its value
** 50..300, else -1. */
int xray_scope_ok(const char *s);
int xray_anchor_ok(const char *s);
int xray_budget(const char *s);
int xray_search_ok(const char *s);
int name_ok(const char *s);

extern char g_ts[];	/* the tailscale CLI, after ts_find; "" none */
void ts_find(void);
int ts_run(char *a1, char *a2, char *a3, char *a4, char **out, size_t cap);

#endif
