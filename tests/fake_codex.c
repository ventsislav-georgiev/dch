/* A stand-in `codex` for the X-ray Codex tests (#160; spec 7.4): dch takes a
** process for a Codex by its executable's name, and reads its environment,
** so this is a native binary built as `codex` (a script's name is its
** interpreter's; a copied platform binary hides its environment).
**
** `--help` prints no --no-daemon (dch leaves the argv alone); otherwise it
** appends "<pid> <DCH_NATIVE_BRIDGE_ID or -> <DCH_SESSION or ->" to
** $FAKE_CODEX_LOG and waits for a signal. It reads and runs nothing.
** With $FAKE_CODEX_TUI set it runs that script with python3 as its child
** on the same terminal (the --say tests' painted Codex screens, #658) and
** exits when it does; with $FAKE_CODEX_DIR set it first appends its argv,
** \x1f-joined, to $FAKE_CODEX_DIR/argv.log (the --stop tests, #659).
** $FAKE_CODEX_SNAP_TID: a shell snapshot for that thread in
** $CODEX_HOME/shell_snapshots, as Codex makes at its first turn (spec D3).
**
**   cc -o <dir>/codex tests/fake_codex.c
*/
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

int
main(int argc, char **argv)
{
	const char *log = getenv("FAKE_CODEX_LOG");
	const char *m = getenv("DCH_NATIVE_BRIDGE_ID");
	const char *s = getenv("DCH_SESSION");
	const char *dir = getenv("FAKE_CODEX_DIR");
	char ap[4096];
	FILE *f;
	int k;

	if (argc > 1 && !strcmp(argv[1], "--help"))
	{
		puts("Usage: codex [OPTIONS]");
		return 0;
	}
	if (log && (f = fopen(log, "a")))
	{
		fprintf(f, "%ld %s %s\n", (long)getpid(), m && *m ? m : "-",
		        s && *s ? s : "-");
		fclose(f);
	}
	if ((m = getenv("FAKE_CODEX_SNAP_TID")) && (dir = getenv("CODEX_HOME")) &&
	    s && *s && snprintf(ap, sizeof ap, "%s/shell_snapshots", dir) <
	    (int)sizeof ap)
	{
		const char *mk = getenv("DCH_NATIVE_BRIDGE_ID");

		mkdir(dir, 0700);
		mkdir(ap, 0700);
		if (snprintf(ap + strlen(ap), sizeof ap - strlen(ap), "/%s.1.sh", m)
		    < (int)(sizeof ap - strlen(ap)) && (f = fopen(ap, "w")))
		{
			fprintf(f, "# Snapshot file\nexport DCH_SESSION=%s\n", s);
			if (mk && *mk)
				fprintf(f, "export DCH_NATIVE_BRIDGE_ID=%s\n", mk);
			fclose(f);
		}
	}
	dir = getenv("FAKE_CODEX_DIR");
	if (dir && snprintf(ap, sizeof ap, "%s/argv.log", dir) < (int)sizeof ap &&
	    (f = fopen(ap, "a")))
	{
		for (k = 0; k < argc; k++)
			fprintf(f, "%s%s", k ? "\x1f" : "", argv[k]);
		fputc('\n', f);
		fclose(f);
	}
	if ((m = getenv("FAKE_CODEX_TUI")) && *m)
	{
		int st;
		pid_t p = fork();

		if (p == 0)
		{
			execlp("python3", "python3", m, (char *)NULL);
			_exit(127);
		}
		while (p > 0 && waitpid(p, &st, 0) < 0)
			;
		return 0;
	}
	for (;;)
		pause();
}
