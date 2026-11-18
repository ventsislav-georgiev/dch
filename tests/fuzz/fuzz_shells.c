/* libFuzzer target: who started a shell (#482). shell_owner reads Claude
** Code transcripts (untrusted bytes, a line possibly still being written):
** the main one by a binary search on line timestamps and a window around
** it, then a subagent's and its meta. The input is all three files; an
** input whose first byte is odd is repeated past 64 KB so the binary
** search runs. Built with -Dmain=dch_main so dch.c's statics are
** reachable. */
#include "../../dch.c"
#include <stdint.h>

int LLVMFuzzerInitialize(int *argc, char ***argv);
int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static char dir[] = "/tmp/dch-fuzz-shells.XXXXXX";
static char tp[sizeof dir + 16], sub[sizeof dir + 32];

static void
put(const char *path, const uint8_t *d, size_t n, size_t total)
{
	int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
	size_t w = 0;

	if (fd < 0)
		abort();
	do
		if (n && write(fd, d, n) != (ssize_t)n)
			abort();
	while (n && (w += n) < total);
	close(fd);
}

static void
cleanup(void)
{
	char f[sizeof sub + 32];

	snprintf(f, sizeof f, "%s/agent-f.jsonl", sub);
	unlink(f);
	snprintf(f, sizeof f, "%s/agent-f.meta.json", sub);
	unlink(f);
	rmdir(sub);
	snprintf(f, sizeof f, "%s/m", dir);
	rmdir(f);
	unlink(tp);
	rmdir(dir);
}

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
	char f[sizeof dir + 8];

	(void)argc;
	(void)argv;
	if (!mkdtemp(dir))
		abort();
	snprintf(tp, sizeof tp, "%s/m.jsonl", dir);
	snprintf(f, sizeof f, "%s/m", dir);
	snprintf(sub, sizeof sub, "%s/m/subagents", dir);
	if (mkdir(f, 0700) || mkdir(sub, 0700))
		abort();
	atexit(cleanup);
	return 0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	char f[sizeof sub + 32], agent[64], adesc[201], desc[201];
	struct sub sv[256];
	int ns = -1;
	/* the seeds' "timestamp"s are at 2026-11-17T00:00:00Z */
	time_t t = 1794873600;

	put(tp, d, n, n && (d[0] & 1) ? 70000 : n);
	snprintf(f, sizeof f, "%s/agent-f.jsonl", sub);
	put(f, d, n, n);
	snprintf(f, sizeof f, "%s/agent-f.meta.json", sub);
	put(f, d, n, n);
	(void)shell_owner(tp, "bfuzz", t, t, sv, &ns, agent, adesc, sizeof adesc,
	                  desc, sizeof desc);
	return 0;
}
