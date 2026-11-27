/*
** A Claude Code argv and its one-shot prompt (#138). `claude [options]
** [prompt]` sends the prompt as the session's first message; dch keeps it
** out of every relaunch and every History record, so a revive, a restart or
** a resume never sends it again. Pure: no I/O.
*/
#include <ctype.h>
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

/* claude is the program run: argv[0], or after `env` with only NAME=value
** and value-less env flags between. A `claude` anywhere else (a directory,
** an option value, an ssh or sh -c argument) belongs to another command,
** whose words are not a prompt (#141), unless dch saw Claude run in the
** session (agent: `headroom wrap claude ...`). An env option with a value
** (-u N, -C dir, -S str, -P path) leaves a plain word between: kept whole. */
static int
claude_runs(char **v, int tok)
{
	static const char *const eflags[] = {
		"-i", "-0", "-v", "--ignore-environment", "--null", "--debug",
	};
	const char *b;
	int k;
	size_t j;

	if (tok == 0)
		return 1;
	b = strrchr(v[0], '/');
	if (strcmp(b ? b + 1 : v[0], "env") != 0)
		return 0;
	for (k = 1; k < tok; k++)
	{
		const char *a = v[k], *p = a;

		if ((*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') ||
		    *p == '_')
			while ((*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') ||
			       (*p >= '0' && *p <= '9') || *p == '_')
				p++;
		if (p > a && *p == '=')
			continue;	/* NAME=value */
		for (j = 0; j < sizeof eflags / sizeof eflags[0]; j++)
			if (strcmp(a, eflags[j]) == 0)
				break;
		if (j == sizeof eflags / sizeof eflags[0])
			return 0;
	}
	return 1;
}

/* commander parses options wherever they sit, operands among them: the root
** program has positional options (they only stop it at a command) and no
** pass-through. So every operand is the prompt or more of it, and every
** option before a bare `--` stays. */
int
dch_claude_strip(char **in, int argc, char **out, int agent)
{
	int nm;
	size_t off;

	return dch_claude_strip_nm(in, argc, out, agent, &nm, &off);
}

static int
isname(const struct copt *o)
{
	return !strcmp(o->name, "-n") || !strcmp(o->name, "--name");
}

int
dch_claude_strip_nm(char **in, int argc, char **out, int agent, int *nm,
                    size_t *off)
{
	int tok = dch_claude_tok(in, argc), k, n, var = 0, ops = 0;
	size_t j;

	*nm = -1;
	*off = 0;

	if (tok < argc && !agent && !claude_runs(in, tok))
		goto whole;
	for (n = 0; n <= tok && n < argc; n++)
		out[n] = in[n];
	for (k = tok + 1; k < argc; k++)
	{
		const char *a = in[k], *p;
		const struct copt *o;

		if (strcmp(a, "--") == 0)
			break;	/* the rest is the prompt; the `--` goes too */
		if (var && !dashed(a))
		{
			out[n++] = in[k];	/* one more variadic value */
			continue;
		}
		var = 0;
		if (!dashed(a))
		{
			for (j = 0; !ops && j < sizeof csubs / sizeof csubs[0]; j++)
				if (strcmp(a, csubs[j]) == 0)
					goto whole;	/* a command, not a prompt */
			ops++;
			continue;	/* an operand: the prompt, or more of it */
		}
		out[n++] = in[k];
		if (a[1] == '-' && strchr(a, '='))
		{
			if (strncmp(a, "--name=", 7) == 0)
				*nm = n - 1, *off = 7;
			continue;	/* --opt=value never takes the next token */
		}
		if (!(o = copt_find(a, strlen(a))) && a[1] != '-')
		{
			/* a cluster: -cp is -c -p, -nfoo is -n foo, and -cn
			** takes n's value from the next token, as -n would */
			for (p = a + 1; *p; p++)
			{
				char sh[3] = { '-', *p, '\0' };

				if (!(o = copt_find(sh, 2)) || o->kind != 'b')
					break;
			}
			if (o && p[1] && isname(o))
				*nm = n - 1, *off = (size_t)(p + 1 - a);
			if (o && (o->kind == 'b' || p[1]))
				continue;	/* all flags, or the value is the rest */
		}
		if (!o)
		{
			/* Unknown: what follows may be its value, and dropping
			** that would break the relaunch. Keep everything. A
			** --no-x never takes one (commander's negatable option). */
			if (strncmp(a, "--no-", 5) != 0 && k + 1 < argc &&
			    !dashed(in[k + 1]))
				goto whole;
			continue;
		}
		if ((o->kind == 'r' || o->kind == 'v') && k + 1 < argc)
		{
			if (isname(o))
				*nm = n, *off = 0;
			out[n++] = in[++k];
		}
		else if (o->kind == 'o' && k + 1 < argc && !dashed(in[k + 1]))
			out[n++] = in[++k];
		var = o->kind == 'v';
	}
	out[n] = NULL;
	/* the name only of a claude that runs: a `claude` word elsewhere
	** (agent) is not its argv */
	if (!claude_runs(in, tok))
		*nm = -1;
	return n;
whole:
	*nm = -1;	/* unsure what owns what: no name */
	for (n = 0; n < argc; n++)
		out[n] = in[n];
	out[n] = NULL;
	return n;
}

/* A Codex thread id as dch records and passes it (#655): a UUID, 36
** characters with dashes at 8, 13, 18 and 23, hex elsewhere. 1 or 0. */
int
dch_codex_id_ok(const char *s)
{
	int k;

	for (k = 0; k < 36; k++)
		if (k == 8 || k == 13 || k == 18 || k == 23 ? s[k] != '-'
		    : !((s[k] >= '0' && s[k] <= '9') ||
		        (s[k] >= 'a' && s[k] <= 'f') ||
		        (s[k] >= 'A' && s[k] <= 'F')))
			return 0;
	return s[36] == '\0';
}

/* codex's options that take the next token as their value (clap, Codex
** 0.167 `codex --help` and `codex resume --help`); every other option is
** a flag. shortcut: -i/--image takes one value here, clap's 1.. would
** take more words up to the next option; upgrade if images matter. */
static const char *const cxval[] = {
	"-c", "--config", "--enable", "--disable", "--remote",
	"--remote-auth-token-env", "-i", "--image", "-m", "--model",
	"--local-provider", "-p", "--profile", "-s", "--sandbox", "-a",
	"--ask-for-approval", "-C", "--cd", "--add-dir", NULL
};

/* #696: a -c/--config value whose key (before the first '=', trimmed,
** a quote around it dropped) is model_provider or model_providers.*. */
static int
cx_provider(const char *kv)
{
	const char *e = strchr(kv, '=');
	size_t l;

	if (!e)
		return 0;
	while (kv < e && isspace((unsigned char)*kv))
		kv++;
	while (e > kv && isspace((unsigned char)e[-1]))
		e--;
	if (kv < e && (*kv == '"' || *kv == '\''))
		kv++;
	if (e > kv && (e[-1] == '"' || e[-1] == '\''))
		e--;
	l = (size_t)(e - kv);
	return (l == 14 && strncmp(kv, "model_provider", 14) == 0) ||
	       (l > 16 && strncmp(kv, "model_providers.", 16) == 0);
}

int
dch_codex_resume(char **in, int argc, int at, char *id, char *cwd,
                 char *msg, char **out, int drop, char *const *cfg, int ncfg)
{
	int k, n = 0, val, i;

	for (k = 0; k <= at; k++)
		out[n++] = in[k];
	out[n++] = "resume";
	out[n++] = id;
	out[n++] = "-C";
	out[n++] = cwd;
	for (k = at + 1; k < argc; k++)
	{
		const char *a = in[k];

		if (strcmp(a, "--") == 0)
			break;		/* the rest is the prompt */
		/* resume or fork, its SESSION_ID, or the PROMPT */
		if (a[0] != '-' || !a[1])
			continue;
		for (val = 0, i = 0; cxval[i] && !val; i++)
			val = strcmp(a, cxval[i]) == 0;
		/* a session choice, and a -C of its own: ours stands */
		if (strcmp(a, "--last") == 0 || strncmp(a, "--cd=", 5) == 0 ||
		    (a[1] == 'C' && a[2]))
			continue;
		if (strcmp(a, "-C") == 0 || strcmp(a, "--cd") == 0)
		{
			k++;
			continue;
		}
		if (drop && (((!strcmp(a, "-c") || !strcmp(a, "--config")) &&
		              k + 1 < argc && cx_provider(in[k + 1]) && ++k) ||
		             (a[1] == 'c' && a[2] && cx_provider(a + 2)) ||
		             (!strncmp(a, "--config=", 9) && cx_provider(a + 9))))
			continue;
		out[n++] = in[k];
		if (val && k + 1 < argc)
			out[n++] = in[++k];
	}
	for (i = 0; i < ncfg; i++)
	{
		out[n++] = "-c";
		out[n++] = cfg[i];
	}
	if (msg && *msg)
	{
		if (msg[0] == '-')
			out[n++] = "--";	/* a prompt, never an option */
		out[n++] = msg;
	}
	out[n] = NULL;
	return n;
}
