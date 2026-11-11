#ifndef cargv_h
#define cargv_h

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

#endif
