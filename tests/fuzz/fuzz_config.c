/* libFuzzer target: the config parser. dch_config_get reads $DCH_CONFIG
** fresh on every call, so the input is written there first; every key
** dch --config knows is looked up, plus the bool reading. Built with
** -Dmain=dch_main so dch.c's statics are reachable. */
#include "../../dch.c"
#include <stdint.h>

int LLVMFuzzerInitialize(int *argc, char ***argv);
int LLVMFuzzerTestOneInput(const uint8_t *d, size_t n);

static char path[] = "/tmp/dch-fuzz-config.XXXXXX";

static void
cleanup(void)
{
	unlink(path);
}

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
	int fd = mkstemp(path);

	(void)argc;
	(void)argv;
	if (fd < 0)
		abort();
	close(fd);
	atexit(cleanup);
	setenv("DCH_CONFIG", path, 1);
	return 0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *d, size_t n)
{
	char out[1024], tiny[4];
	size_t i;
	FILE *f = fopen(path, "w");

	if (!f)
		abort();
	if (n)
		fwrite(d, 1, n, f);
	fclose(f);
	for (i = 0; i < sizeof config_keys / sizeof *config_keys; i++)
	{
		(void)dch_config_get(config_keys[i].key, out, sizeof out);
		(void)dch_config_get(config_keys[i].key, tiny, sizeof tiny);
		if (config_keys[i].is_bool)
			(void)dch_config_bool(config_keys[i].key);
	}
	return 0;
}
