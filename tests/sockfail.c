/* Test-only interposer for tests/chord_fork_exit_test.py (#25, LOW-25).
** Loaded into an attach client (DYLD_INSERT_LIBRARIES on macOS, LD_PRELOAD
** on Linux). In any process forked from the loading one, a write() to a
** socket fails with EPIPE, as on a master that died under the write: every
** query child of the in-attach X-ray view takes its exit(1) path. The
** loading process itself is untouched.
**
** SOCKFAIL_MARK names a file that gets one line per event, so the test needs
** no sleep: "l <pid>" the shim loaded, "w <pid>" a child's first failed
** write, "x <pid> raw|cooked|notty" that child at exit, with the state of
** the terminal on its stdin. The exit line comes from an atexit handler
** registered at load, so before every handler the client registered later
** (restore_term, client_pidfile_unlink): by the time it is written, those
** have run in the child, and the terminal shows what they did to it.
**
** Built by the test with the system cc:
**   macOS: cc -dynamiclib -o sockfail.dylib sockfail.c
**   Linux: cc -shared -fPIC -o sockfail.so sockfail.c -ldl */
#ifndef __APPLE__
#define _GNU_SOURCE	/* RTLD_NEXT */
#include <dlfcn.h>
#endif
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <termios.h>
#include <unistd.h>

static pid_t first, failed;
static char mark[1024];

#ifdef __APPLE__
/* dyld does not apply an image's own interposing to that image: write()
** here is the real one */
#define real_write write
#else
static ssize_t
real_write(int fd, const void *b, size_t n)
{
	static ssize_t (*w)(int, const void *, size_t);

	if (!w)
		*(void **)&w = dlsym(RTLD_NEXT, "write");
	if (!w)
	{
		errno = ENOSYS;
		return -1;
	}
	return w(fd, b, n);
}
#endif

static void
note(char c, const char *what)
{
	char line[48];
	int fd, n;

	if (!mark[0])
		return;
	n = snprintf(line, sizeof line, "%c %d %s\n", c, (int)getpid(), what);
	fd = open(mark, O_WRONLY | O_CREAT | O_APPEND, 0600);
	if (fd < 0)
		return;
	(void)real_write(fd, line, (size_t)n);
	close(fd);
}

static void
at_exit(void)
{
	struct termios t;

	if (failed && failed == getpid())
		note('x', tcgetattr(0, &t) < 0 ? "notty"
		     : t.c_lflag & ICANON ? "cooked" : "raw");
}

__attribute__((constructor)) static void
init(void)
{
	const char *p = getenv("SOCKFAIL_MARK");

	first = getpid();
	if (p && strlen(p) < sizeof mark)
		strcpy(mark, p);
	atexit(at_exit);
	note('l', "");
}

static ssize_t
sockfail_write(int fd, const void *b, size_t n)
{
	struct stat st;

	if (getpid() == first || fstat(fd, &st) < 0 || !S_ISSOCK(st.st_mode))
		return real_write(fd, b, n);
	if (failed != getpid())
	{
		failed = getpid();
		note('w', "");
	}
	errno = EPIPE;
	return -1;
}

#ifdef __APPLE__
__attribute__((used, section("__DATA,__interpose")))
static struct { const void *n, *o; } ip[] = {
	{ (const void *)sockfail_write, (const void *)write } };
#else
ssize_t
write(int fd, const void *b, size_t n)
{
	return sockfail_write(fd, b, n);
}
#endif
