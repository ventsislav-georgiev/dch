/* A stand-in `codex` for the X-ray Codex tests (#160; spec 7.4): dch takes a
** process for a Codex by its executable's name, and reads its environment,
** so this is a native binary built as `codex` (a script's name is its
** interpreter's; a copied platform binary hides its environment).
**
** `--help` prints no --no-daemon (dch leaves the argv alone); otherwise it
** appends "<pid> <DCH_NATIVE_BRIDGE_ID or -> <DCH_SESSION or ->" to
** $FAKE_CODEX_LOG and waits for a signal. It reads and runs nothing.
**
**   cc -o <dir>/codex tests/fake_codex.c
*/
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int
main(int argc, char **argv)
{
	const char *log = getenv("FAKE_CODEX_LOG");
	const char *m = getenv("DCH_NATIVE_BRIDGE_ID");
	const char *s = getenv("DCH_SESSION");
	FILE *f;

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
	for (;;)
		pause();
}
