/*
 * cargv_unit.c - unit tests for cargv.c: claude's one-shot prompt in an
 * argv (#138).
 *
 *   cc -I. tests/cargv_unit.c cargv.c -o cargv_unit && ./cargv_unit
 *
 * tests/run.sh builds and runs it.
 */
#include <stdio.h>
#include <string.h>
#include "cargv.h"

static int fails;

/* words: space-separated argv, '_' inside a word is a space. want: what
** dch_claude_keep keeps, joined the same way. */
static void
check(const char *name, const char *words, const char *want)
{
	char buf[1024], got[1024] = "", *v[64], *w;
	int n = 0, k, keep;

	snprintf(buf, sizeof buf, "%s", words);
	for (w = strtok(buf, " "); w && n < 63; w = strtok(NULL, " "))
		v[n++] = w;
	for (k = 0; k < n; k++)
		for (w = v[k]; *w; w++)
			if (*w == '_')
				*w = ' ';
	v[n] = NULL;
	keep = dch_claude_keep(v, n);
	for (k = 0; k < keep; k++)
		snprintf(got + strlen(got), sizeof got - strlen(got), "%s%s",
		         k ? " " : "", v[k]);
	if (strcmp(got, want) == 0)
		printf("  ok   %s\n", name);
	else
	{
		printf("  FAIL %s: want [%s] got [%s]\n", name, want, got);
		fails++;
	}
}

int
main(void)
{
	/* `--` forms */
	check("-- then a prompt", "claude --name x -- fix_the_bug",
	      "claude --name x");
	check("-- then a multi-word prompt", "claude -- fix the login bug",
	      "claude");
	check("-- a prompt that looks like an option", "claude -- --model",
	      "claude");
	check("dangling --", "claude --model opus --", "claude --model opus");
	check("-- a subcommand word is a prompt", "claude --name x -- update",
	      "claude --name x");

	/* positional forms */
	check("bare prompt", "claude fix_the_bug", "claude");
	check("prompt after --name", "claude --name api fix_the_login_bug",
	      "claude --name api");
	check("unquoted multi-word prompt", "claude --model opus fix the bug",
	      "claude --model opus");
	check("everything after the prompt goes",
	      "claude fix_it --model opus", "claude");
	check("- alone is an operand", "claude -", "claude");
	check("full path to claude", "/opt/homebrew/bin/claude -n x hi",
	      "/opt/homebrew/bin/claude -n x");
	check("wrapped in env", "env A=1 claude --verbose hi",
	      "env A=1 claude --verbose");

	/* --opt=value */
	check("--model=opus", "claude --model=opus hi", "claude --model=opus");
	check("--add-dir=a is not variadic after", "claude --add-dir=a hi",
	      "claude --add-dir=a");
	check("--tmux=classic", "claude --tmux=classic hi",
	      "claude --tmux=classic");
	check("unknown --x=y takes nothing", "claude --x=y hi", "claude --x=y");

	/* value-taking options before the prompt */
	check("required value", "claude --permission-mode plan hi",
	      "claude --permission-mode plan");
	check("required value starting with -",
	      "claude --append-system-prompt -x hi",
	      "claude --append-system-prompt -x");
	check("required value --", "claude --system-prompt -- hi",
	      "claude --system-prompt --");
	check("required value missing", "claude --model", "claude --model");
	check("optional value taken", "claude --debug hooks hi",
	      "claude --debug hooks");
	check("optional value not taken from a dash",
	      "claude -d --verbose hi", "claude -d --verbose");
	check("-n short", "claude -n api hi", "claude -n api");
	check("-nfoo attached", "claude -napi hi", "claude -napi");
	check("cluster -cp", "claude -cp hi", "claude -cp");
	check("cluster -cn takes the next", "claude -cn api hi",
	      "claude -cn api");
	check("cluster -dhooks", "claude -dhooks hi", "claude -dhooks");

	/* variadic options: their values are not a prompt */
	check("--add-dir values", "claude --add-dir a b c",
	      "claude --add-dir a b c");
	check("--allowedTools then an option then a prompt",
	      "claude --allowedTools Bash Edit --model opus hi",
	      "claude --allowedTools Bash Edit --model opus");
	check("--mcp-config then --", "claude --mcp-config a.json -- hi",
	      "claude --mcp-config a.json");
	check("--disallowed-tools values", "claude --disallowed-tools Bash",
	      "claude --disallowed-tools Bash");

	/* unknown options */
	check("unknown then a word: keep all", "claude --frob x hi",
	      "claude --frob x hi");
	check("unknown short then a word: keep all", "claude -z x",
	      "claude -z x");
	check("unknown cluster then a word: keep all", "claude -cz x",
	      "claude -cz x");
	check("unknown then a dash: a flag", "claude --frob --model o hi",
	      "claude --frob --model o");
	check("unknown then --", "claude --frob -- hi", "claude --frob");

	/* subcommands */
	check("claude update is a command", "claude update", "claude update");
	check("claude mcp serve is a command", "claude mcp serve",
	      "claude mcp serve");
	check("a command after options", "claude --verbose doctor",
	      "claude --verbose doctor");

	/* --resume and friends: kept here, resume_argv drops them */
	check("--resume id then prompt",
	      "claude --resume 123e4567 --name x hi",
	      "claude --resume 123e4567 --name x");
	check("-r without id then prompt after --", "claude -r -- hi",
	      "claude -r");
	check("-c then prompt", "claude -c hi", "claude -c");

	/* nothing to strip */
	check("no claude token", "zsh -l", "zsh -l");
	check("not claude: a prompt-looking word stays",
	      "codex fix_the_bug", "codex fix the bug");
	check("claude-ish name is not claude", "claude-code hi",
	      "claude-code hi");
	check("claude alone", "claude", "claude");
	check("claude with options only", "claude --name x --model opus",
	      "claude --name x --model opus");
	check("empty", "", "");

	printf("%s\n", fails ? "FAIL" : "ok");
	return fails != 0;
}
