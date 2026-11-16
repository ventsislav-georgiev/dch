/* ptyfail: a preload shim for tests/run.sh (#399). forkpty fails with
** ENXIO, as on a full pty table, for the first $DCH_PTYFAIL calls of each
** process, then works. macOS: DYLD_INSERT_LIBRARIES; else LD_PRELOAD. */
#define _GNU_SOURCE
#include <errno.h>
#include <stdlib.h>
#include <sys/types.h>
#include <termios.h>
#include <sys/ioctl.h>
#ifdef __APPLE__
#include <util.h>
#else
#include <dlfcn.h>
#include <pty.h>
#endif

static int calls;

static int
fail_now(void)
{
	const char *n = getenv("DCH_PTYFAIL");

	if (n && calls++ < atoi(n))
	{
		errno = ENXIO;
		return 1;
	}
	return 0;
}

#ifdef __APPLE__
static pid_t
my_forkpty(int *m, char *name, struct termios *t, struct winsize *w)
{
	return fail_now() ? -1 : forkpty(m, name, t, w);
}

__attribute__((used, section("__DATA,__interpose")))
static const struct { const void *with, *orig; } ptyfail_ip[] = {
	{ (const void *)my_forkpty, (const void *)forkpty },
};
#else
pid_t
forkpty(int *m, char *name, const struct termios *t, const struct winsize *w)
{
	pid_t (*real)(int *, char *, const struct termios *,
	              const struct winsize *);

	if (fail_now())
		return -1;
	*(void **)&real = dlsym(RTLD_NEXT, "forkpty");
	return real(m, name, t, w);
}
#endif
