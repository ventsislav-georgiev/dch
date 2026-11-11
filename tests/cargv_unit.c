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
static int agent;	/* dch_claude_strip's agent flag for the next checks */

/* words: space-separated argv, '_' inside a word is a space. want: what
** dch_claude_strip keeps, joined by spaces. */
static void
check(const char *name, const char *words, const char *want)
{
	char buf[1024], got[1024] = "", *v[64], *out[65], *w;
	int n = 0, k, keep;

	snprintf(buf, sizeof buf, "%s", words);
	for (w = strtok(buf, " "); w && n < 63; w = strtok(NULL, " "))
		v[n++] = w;
	for (k = 0; k < n; k++)
		for (w = v[k]; *w; w++)
			if (*w == '_')
				*w = ' ';
	v[n] = NULL;
	memset(out, 0xa5, sizeof out);	/* a missing NULL would show */
	keep = dch_claude_strip(v, n, out, agent);
	for (k = 0; k < keep; k++)
		snprintf(got + strlen(got), sizeof got - strlen(got), "%s%s",
		         k ? " " : "", out[k]);
	if (out[keep] != NULL)
		snprintf(got, sizeof got, "(out not NULL-terminated)");
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
	check("an option after the prompt stays", "claude fix --model opus",
	      "claude --model opus");
	check("options around the prompt stay",
	      "claude --model opus fix --effort high",
	      "claude --model opus --effort high");
	check("an operand then --: the rest goes", "claude fix -- more",
	      "claude");
	check("options before a later -- stay",
	      "claude fix --effort high -- x", "claude --effort high");
	check("-c fix -n foo", "claude -c fix -n foo", "claude -c -n foo");
	check("variadic after the prompt keeps its values",
	      "claude fix --add-dir a b", "claude --add-dir a b");
	check("excess operands go, options between stay",
	      "claude fix -p the --verbose bug", "claude -p --verbose");
	check("unknown after the prompt then a word: keep all",
	      "claude fix --frob x", "claude fix --frob x");
	check("a command word as a later operand is prompt",
	      "claude fix update", "claude");
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
	check("unknown --no-x then a word: cut", "claude --no-frob hi",
	      "claude --no-frob");

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
	/* #141: claude is not the program run: kept whole */
	check("git -C .../claude", "git -C x/claude log --oneline",
	      "git -C x/claude log --oneline");
	check("sh -c with a claude dir arg",
	      "sh -c echo_$* sh x/claude extra",
	      "sh -c echo $* sh x/claude extra");
	check("--directory /srv/claude",
	      "python3 -m http.server --directory /srv/claude 8000",
	      "python3 -m http.server --directory /srv/claude 8000");
	check("docker --name claude", "docker run --name claude img cmd",
	      "docker run --name claude img cmd");
	check("ssh host claude", "ssh host claude p", "ssh host claude p");
	check("env -u X claude: a valued env option", "env -u X claude p",
	      "env -u X claude p");
	check("env -C dir: claude is the dir", "env -C /src/claude ls a",
	      "env -C /src/claude ls a");
	/* #141: claude run after env: cut */
	check("env FOO=1 claude", "env FOO=1 claude p --model m",
	      "env FOO=1 claude --model m");
	check("env -i claude", "env -i claude p", "env -i claude");
	check("/usr/bin/env A=1 B=2 /x/claude --",
	      "/usr/bin/env A=1 B=2 /x/claude -- p",
	      "/usr/bin/env A=1 B=2 /x/claude");
	check("env =x is not an assignment", "env =x claude p",
	      "env =x claude p");

	/* #141: dch saw Claude run in the session: a wrapper's claude cuts */
	agent = 1;
	check("agent: wrap claude", "wrap claude fix_it --model opus",
	      "wrap claude --model opus");
	check("agent: headroom wrap claude",
	      "/u/.local/bin/headroom wrap claude --chrome p --no-rtk",
	      "/u/.local/bin/headroom wrap claude --chrome --no-rtk");
	check("agent: headroom wrap claude, --resume",
	      "headroom wrap claude --resume 1234 --chrome -- p",
	      "headroom wrap claude --resume 1234 --chrome");
	/* an unknown --no-x never takes a value (commander negation) */
	check("agent: unknown --no-rtk then a word: the word is the prompt",
	      "headroom wrap claude --chrome --no-rtk p",
	      "headroom wrap claude --chrome --no-rtk");
	check("agent: unknown --no-rtk=1 then a word: cut",
	      "headroom wrap claude --no-rtk=1 p",
	      "headroom wrap claude --no-rtk=1");
	check("agent: other unknown option then a word keeps all",
	      "headroom wrap claude --chrome --rtk p",
	      "headroom wrap claude --chrome --rtk p");
	check("agent: claude argv[0] still cuts", "claude --name x hi",
	      "claude --name x");
	check("agent: no claude token", "zsh -l", "zsh -l");
	agent = 0;

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
