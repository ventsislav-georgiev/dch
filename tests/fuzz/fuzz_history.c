/* libFuzzer target: history record parsing. hrec_load reads <name>.json
** records out of the state dir (what --history, --revive and the picker's
** History tab list), so the input is written to a record file first. Built
** with -Dmain=dch_main so dch.c's statics are reachable. */
#include "../../dch.c"
#include <stdint.h>

int LLVMFuzzerInitialize(int *argc, char ***argv);
int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static char dir[] = "/tmp/dch-fuzz-history.XXXXXX";
static int dfd = -1;

static void
cleanup(void)
{
	unlinkat(dfd, "r.json", 0);
	rmdir(dir);
}

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
	(void)argc;
	(void)argv;
	if (!mkdtemp(dir) || (dfd = open(dir, O_RDONLY | O_DIRECTORY)) < 0)
		abort();
	atexit(cleanup);
	/* a record only loads for the socket dir it names: the seeds use "/s",
	** which also leaves name_live() nothing to connect to */
	snprintf(sock_dir, sizeof sock_dir, "/s");
	return 0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	struct hrec r;
	int fd = openat(dfd, "r.json", O_WRONLY | O_CREAT | O_TRUNC, 0600);

	if (fd < 0)
		abort();
	if (n && write(fd, d, n) != (ssize_t)n)
		abort();
	close(fd);
	if (hrec_load(dfd, "r.json", &r) == 0)
	{
		free(r.body);
		free(r.argv);
		free(r.launch);
	}
	return 0;
}
