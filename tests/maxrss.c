/* maxrss ARGV...: run ARGV in a fresh fork()ed child, wait4() it, and print
 * "maxrss <bytes>" on stderr; exit with the child's status (128+sig if it
 * died on a signal, 124 on the 60 s watchdog).
 *
 * Why not Python's subprocess + os.wait4: on Linux CPython spawns with
 * vfork/posix_spawn, and execve folds the old mm's RSS high-water mark into
 * the child's signal->maxrss (exec_mmap -> setmax_mm_hiwater_rss), so the
 * child's ru_maxrss starts at the Python parent's ~18 MB. A plain fork() of
 * this tiny process copies a ~1 MB mm instead, so the number is the target's.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile pid_t child;
static volatile sig_atomic_t timed_out;

static void on_alarm(int sig)
{
	(void)sig;
	timed_out = 1;
	if (child > 0)
		kill(child, SIGKILL);
}

int main(int argc, char **argv)
{
	struct rusage ru;
	int status;

	if (argc < 2) {
		fprintf(stderr, "usage: maxrss cmd [arg...]\n");
		return 2;
	}
	child = fork();
	if (child < 0) {
		perror("fork");
		return 2;
	}
	if (child == 0) {
		execvp(argv[1], argv + 1);
		perror(argv[1]);
		_exit(127);
	}
	signal(SIGALRM, on_alarm);
	alarm(60);
	while (wait4(child, &status, 0, &ru) < 0)
		if (errno != EINTR) {
			perror("wait4");
			return 2;
		}
	alarm(0);
#ifdef __APPLE__
	long long bytes = (long long)ru.ru_maxrss;          /* bytes */
#else
	long long bytes = (long long)ru.ru_maxrss * 1024;   /* KiB */
#endif
	fprintf(stderr, "maxrss %lld\n", bytes);
	if (WIFSIGNALED(status))
		return timed_out ? 124 : 128 + WTERMSIG(status);
	return WEXITSTATUS(status);
}
