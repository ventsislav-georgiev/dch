#ifndef cargv_h
#define cargv_h

#include <stddef.h>

/* cargv.c: a Claude Code argv and its one-shot prompt (#138). Pure. */

/* Index of the argv entry whose basename is `claude`, argc when none. */
int dch_claude_tok(char **v, int argc);
/* in[0..argc) without claude's one-shot prompt, into out (argc + 1
** entries, NULL-terminated, not in): every operand (the prompt, or more of
** it) and a bare `--` with everything after it go, every option and its
** values stay. All of in when there is no claude token, the first operand
** is a claude command, or an option the table does not know may own what
** follows. The new count. */
int dch_claude_strip(char **in, int argc, char **out, int agent);
/* The same, and *nm the out index of the entry holding the value of
** claude's session name (-n X, --name X, --name=X, -cn X, -nX; the last
** one wins), the value at out[*nm] + *off; -1 when there is none, the
** walk kept everything, or claude does not run (#398). */
int dch_claude_strip_nm(char **in, int argc, char **out, int agent, int *nm,
                        size_t *off);
/* #655: s is a Codex thread id (a UUID). 1 or 0. */
int dch_codex_id_ok(const char *s);
/* The relaunch of a Codex argv on thread id in cwd: in[0..at] (at: the
** codex word), then `resume <id> -C <cwd>`, then in's options with their
** values, less a subcommand, positionals (resume/fork, a SESSION_ID, the
** PROMPT, everything after `--`), --last and a -C of its own; msg, when
** non-NULL and not empty, last as the PROMPT. #696: drop leaves out each
** -c/--config whose key is model_provider or model_providers.*; cfg's
** ncfg K=V go as `-c K=V` after the options, before the PROMPT. out holds
** argc + 7 + 2 * ncfg entries, NULL-terminated; its count. */
int dch_codex_resume(char **in, int argc, int at, char *id, char *cwd,
                     char *msg, char **out, int drop, char *const *cfg,
                     int ncfg);

#endif
