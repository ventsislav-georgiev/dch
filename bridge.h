#ifndef DCH_BRIDGE_H
#define DCH_BRIDGE_H
#include <stddef.h>

typedef int (*dch_claude_record_fn)(const char *, const char *, size_t, void *);
int dch_visit_claude_records(dch_claude_record_fn visit, void *arg);

/* Both build variants link bridge.c; only the terminal mirror is optional. */
void dch_bridge_prepare(char **argv, int resumed);
void dch_bridge_start(char **argv, int resumed, int ptyfd, int listenfd,
                      int statusfd);
void dch_bridge_reap(void);
int dch_bridge_agent_list(int json);
int dch_bridge_agent_send(const char *name, int argc, char **argv);
int dch_codex_snapshot_id(const char *home, const char *sess,
                          const char *marker, char *out, size_t outsz,
                          int strict);
int dch_codex_thread_name(const char *home, const char *id, char *out,
                          size_t outsz);
int dch_pty_gate(const char *path);
int dch_pty_push(const char *path, const char *text);

#endif
