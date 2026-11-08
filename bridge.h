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
int dch_codex_shared(const char *sess, const char *marker);
char **dch_codex_argv(char **argv);
int dch_pty_gate(const char *path);
int dch_pty_push(const char *path, const char *text);
int dch_json_string(const char *json, const char *key, char *out, size_t cap);
int dch_json_long(const char *json, const char *key, long *out);
int dch_json_is_object(const char *json);
int dch_json_nested_string(const char *json, const char *object,
                           const char *key, char *out, size_t cap);
size_t dch_json_quote(char *out, size_t cap, const char *s);
/* json.c: the length of the valid UTF-8 sequence at s (n bytes left), 0 for
** none; whether s[0..n) is all UTF-8 with no NUL. */
size_t dch_utf8_len(const unsigned char *s, size_t n);
int dch_utf8_ok(const unsigned char *s, size_t n);
size_t dch_utf8_trim(const char *s, size_t n);
char *dch_utf8_fix(const char *s);
char **dch_json_strv(const char *json, const char *object, const char *key,
                     int *argc);
int dch_process_start(long pid, char *out, size_t cap);

/* The JSON tokenizer (json.c), for callers that walk a document themselves.
** dch_json_parse() needs NUL-terminated UTF-8; dch_json_free() after every
** parse, even a failed one. Token 0 is the root; a container's children run
** from its index + 1 to its `next`, each child's `next` the one after it.
** dch_json_field(): the value token of `key` in an object, -1 absent, -2
** duplicated. dch_json_decode(): a string token, unescaped, into out[cap]. */
enum json_kind { JSON_STRING, JSON_OBJECT, JSON_ARRAY, JSON_PRIMITIVE };
struct json_token {
  enum json_kind kind;
  int start, end, parent, next;
};
struct json_parser {
  const char *text;
  size_t len, pos;
  struct json_token *tok;
  int ntok, cap, depth;
};
int dch_json_parse(struct json_parser *p, const char *text);
void dch_json_free(struct json_parser *p);
int dch_json_field(const struct json_parser *p, int object, const char *key);
int dch_json_decode(const struct json_parser *p, int token, char *out,
                    size_t cap);
void dch_sha256_hex(const void *data, size_t n, char out[65]);
int dch_random_bytes(unsigned char *out, size_t len);
int dch_meta_prs(const char *json, char *out, size_t cap, long *merged);

#endif
