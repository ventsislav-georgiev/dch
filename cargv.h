#ifndef cargv_h
#define cargv_h

/* cargv.c: a Claude Code argv and its one-shot prompt (#138). Pure. */

/* Index of the argv entry whose basename is `claude`, argc when none. */
int dch_claude_tok(char **v, int argc);
/* How many entries of v[0..argc) to keep so that claude's one-shot prompt
** is gone: argc when there is none, no claude token, or an option the table
** does not know may own what follows. */
int dch_claude_keep(char **v, int argc);

#endif
