/*
** A Claude Code argv and its one-shot prompt (#138). `claude [options]
** [prompt]` sends the prompt as the session's first message; dch keeps it
** out of every relaunch and every History record, so a revive, a restart or
** a resume never sends it again. Pure: no I/O.
*/
#include <string.h>

#include "cargv.h"

/* claude's options, from `claude --help` of Claude Code 2.1.318.
** b: a flag; r: takes a value, the next token whatever it is; o: takes an
** OPTIONAL value, the next token only when it does not start with '-';
** v: variadic, takes the next token and then every one up to the next that
** starts with '-'. commander.js parses them so. */
static const struct copt
{
	const char *name;
	char kind;
} copts[] = {
	{ "--add-dir", 'v' },
	{ "--agent", 'r' },
	{ "--agents", 'r' },
	{ "--allow-dangerously-skip-permissions", 'b' },
	{ "--allowedTools", 'v' }, { "--allowed-tools", 'v' },
	{ "--append-system-prompt", 'r' },
	{ "--autocompact", 'r' },
	{ "--ax-screen-reader", 'b' },
	{ "--bg", 'b' }, { "--background", 'b' },
	{ "--bare", 'b' },
	{ "--betas", 'v' },
	{ "--brief", 'b' },
	{ "--chrome", 'b' },
	{ "--cloud", 'o' },
	{ "-c", 'b' }, { "--continue", 'b' },
	{ "--dangerously-skip-permissions", 'b' },
	{ "-d", 'o' }, { "--debug", 'o' },
	{ "--debug-file", 'r' },
	{ "--desktop", 'b' },
	{ "--disable-slash-commands", 'b' },
	{ "--disallowedTools", 'v' }, { "--disallowed-tools", 'v' },
	{ "--effort", 'r' },
	{ "--environment", 'r' },
	{ "--exclude-dynamic-system-prompt-sections", 'b' },
	{ "--fallback-model", 'r' },
	{ "--file", 'v' },
	{ "--fork-session", 'b' },
	{ "--forward-subagent-text", 'b' },
	{ "--from-pr", 'o' },
	{ "-h", 'b' }, { "--help", 'b' },
	{ "--ide", 'b' },
	{ "--include-hook-events", 'b' },
	{ "--include-partial-messages", 'b' },
	{ "--input-format", 'r' },
	{ "--json-schema", 'r' },
	{ "--max-budget-usd", 'r' },
	{ "--mcp-config", 'v' },
	{ "--model", 'r' },
	{ "-n", 'r' }, { "--name", 'r' },
	{ "--no-chrome", 'b' },
	{ "--no-session-persistence", 'b' },
	{ "--output-format", 'r' },
	{ "--permission-mode", 'r' },
	{ "--permission-prompts", 'r' },
	{ "--plugin-dir", 'r' },
	{ "--plugin-url", 'r' },
	{ "-p", 'b' }, { "--print", 'b' },
	{ "--prompt-suggestions", 'o' },
	{ "--remote-control", 'o' },
	{ "--remote-control-session-name-prefix", 'r' },
	{ "--replay-user-messages", 'b' },
	{ "--restricted", 'b' },
	{ "-r", 'o' }, { "--resume", 'o' },
	{ "--safe-mode", 'b' },
	{ "--session-id", 'r' },
	{ "--setting-sources", 'r' },
	{ "--settings", 'r' },
	{ "--strict-mcp-config", 'b' },
	{ "--system-prompt", 'r' },
	{ "--system-prompt-snapshot", 'r' },
	{ "--teleport", 'o' },
	{ "--tmux", 'b' },
	{ "--tools", 'v' },
	{ "--verbose", 'b' },
	{ "-v", 'b' }, { "--version", 'b' },
	{ "-w", 'o' }, { "--worktree", 'o' },
};

/* claude's commands and aliases (2.1.318: `claude --help`, commander's
** `help`, and the hidden ones in its bundle). As the first operand one is
** dispatched, so it is a command, never a prompt. */
static const char *const csubs[] = {
	"agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import",
	"install", "logs", "mcp", "plugin", "plugins", "purge", "respawn", "rm",
	"setup-token", "stop", "kill", "ultrareview", "update", "upgrade",
	"help", "remote-control", "rc", "sandbox", "import-conversations",
};

int
dch_claude_tok(char **v, int argc)
{
	int k;

	for (k = 0; k < argc; k++)
	{
		const char *b = strrchr(v[k], '/');

		if (strcmp(b ? b + 1 : v[k], "claude") == 0)
			break;
	}
	return k;
}

/* commander's test for an option-looking token: "-" alone is an operand */
static int
dashed(const char *s)
{
	return s[0] == '-' && s[1];
}

/* The option named s[0..n), NULL when the table has none. */
static const struct copt *
copt_find(const char *s, size_t n)
{
	size_t k;

	for (k = 0; k < sizeof copts / sizeof copts[0]; k++)
		if (strlen(copts[k].name) == n && !strncmp(copts[k].name, s, n))
			return &copts[k];
	return NULL;
}

int
dch_claude_keep(char **v, int argc)
{
	int k = dch_claude_tok(v, argc), var = 0;
	size_t j;

	for (k++; k < argc; k++)
	{
		const char *a = v[k], *p;
		const struct copt *o = NULL;

		if (strcmp(a, "--") == 0)
			return k;	/* the rest is the prompt; the `--` goes too */
		if (var && !dashed(a))
			continue;	/* one more value of a variadic option */
		var = 0;
		if (!dashed(a))
		{
			for (j = 0; j < sizeof csubs / sizeof csubs[0]; j++)
				if (strcmp(a, csubs[j]) == 0)
					return argc;
			return k;	/* the first operand: the prompt and the rest */
		}
		if (a[1] == '-' && strchr(a, '='))
			continue;	/* --opt=value never takes the next token */
		if (!(o = copt_find(a, strlen(a))) && a[1] != '-')
		{
			/* a cluster: -cp is -c -p, -nfoo is -n foo, and -cn
			** takes n's value from the next token, as -n would */
			for (p = a + 1; *p; p++)
			{
				char s[3] = { '-', *p, '\0' };

				if (!(o = copt_find(s, 2)) || o->kind != 'b')
					break;
			}
			if (o && (o->kind == 'b' || p[1]))
				continue;	/* all flags, or the value is the rest */
		}
		if (!o)
		{
			/* Unknown: what follows may be its value, and dropping
			** that would break the relaunch. Keep everything. */
			if (k + 1 < argc && !dashed(v[k + 1]))
				return argc;
			continue;
		}
		if (o->kind == 'r' || o->kind == 'v')
		{
			if (++k >= argc)
				return argc;
		}
		else if (o->kind == 'o' && k + 1 < argc && !dashed(v[k + 1]))
			k++;
		var = o->kind == 'v';
	}
	return argc;
}
