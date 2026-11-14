/*
    dch — detachable terminal session manager. Built on dtach core
    (attach_main/master_main/push_main from upstream) with the bash-wrapper
    features merged in: auto-named sessions, static-TUI picker for -l/-k/-d,
    nesting refuse, orphan reap, server/client split so closing the VSCode
    terminal kills only the client and the session survives.

    Originally GPLv2 from dtach by Ned T. Crigler. This file is the dch
    entry point that replaces upstream main.c.
*/
#include "cargv.h"
#include "dtach.h"
#include "picker.h"
#include "rframe.h"
#include "ring.h"
#include "xray.h"

#include <ctype.h>
#include <dirent.h>
#include <poll.h>
#include <regex.h>
#include <stdarg.h>
#include <sys/time.h>
#include <pwd.h>
#include <sys/types.h>
#include <sys/un.h>
#ifdef __APPLE__
#include <libproc.h>
#include <sys/sysctl.h>
#endif
#ifdef __linux__
#include <sys/inotify.h>	/* --xray --follow's tail (#136) */
#else
#include <sys/event.h>
#endif

/* DCH_VERSION lives in dtach.h — the master needs it for the .ver sidecar. */

/* printf-style checking for our variadic formatters (fmt at f, args at a). */
#define DCH_PRINTF(f, a) __attribute__((format(printf, f, a)))

/* Shared globals (declared in dtach.h, used by attach.c/master.c). */
const char copyright[] = "dch " DCH_VERSION " - based on dtach"
                         " (C)Copyright 2004-2016 Ned T. Crigler";
char *progname;
char *sockname;
int detach_char = '\\' - 64;
int no_suspend;
int redraw_method = REDRAW_UNSPEC;
struct termios orig_term;
int dont_have_tty;
char *dch_exe;
char **dch_argv;

/* dch-local state. */
static char sock_dir[1024];
static char sock_path[1100];
static char session_name[512];
static char client_pidfile[1200];
static pid_t client_pid;	/* the process that file names */
/* Remote hosts (#087), remote.c. remote_ok: an attach may name host:name
** (-a, and a pick from the attach pickers); attach_only: -a, which never
** creates; remote_host: --host; pk_remote: the picker lists remote rows. */
static int remote_ok, attach_only, pk_remote;
static const char *remote_host;
/* the picker's header: "kill session:" or "detach session:" where it does
** not attach (opens 0), what its Enter does there */
static const char *pk_header = "";
/* the host of an a:b name that stayed local only because the host is not
** known (#122): a failed verb names it in one hint line */
static char g_rhint[DCH_RHOST_MAX];
/* --xray: the stderr line a failed write exits 1 with (SIGPIPE ignored) */
static const char *write_fail_line;

/* Write a buffer or exit — used by attach.c and master.c (declared in
** dtach.h). Lives here to keep main.c-equivalent self-contained. */
void
write_buf_or_fail(int fd, const void *buf, size_t count)
{
	while (count != 0)
	{
		ssize_t ret = write(fd, buf, count);

		if (ret >= 0)
		{
			buf = (const char *)buf + ret;
			count -= ret;
		}
		else if (ret < 0 && errno == EINTR)
			continue;
		else
		{
			if (write_fail_line)
				fputs(write_fail_line, stderr);
			exit(1);
		}
	}
}

/* Payload byte count carried on the wire for a packet, derived from type.
** PUSH/KEYS/READ/WAIT/RESTART/RESPAWN/CMD/RING_READ = pkt->len bytes; WINCH/REDRAW = a
** winsize; ATTACH/DETACH/CMD = none. */
size_t
packet_payload_len(const struct packet *pkt)
{
	if (pkt->type == MSG_PUSH || pkt->type == MSG_KEYS ||
	    pkt->type == MSG_READ || pkt->type == MSG_WAIT ||
	    pkt->type == MSG_RESTART || pkt->type == MSG_RESPAWN ||
	    pkt->type == MSG_CMD || pkt->type == MSG_RING_READ ||
	    pkt->type == MSG_RESTART_CAPS)
		return pkt->len;
	if (pkt->type == MSG_WINCH || pkt->type == MSG_REDRAW)
		return sizeof(struct winsize);
	return 0;
}

void
write_packet_or_fail(int fd, const struct packet *pkt)
{
	/* Serialize [type:1][len:2 host-order][payload]. Single writer per
	** socket, so two write_buf_or_fail calls can't interleave a frame. */
	unsigned char hdr[PKT_HDR];

	hdr[0] = pkt->type;
	hdr[1] = (unsigned char)(pkt->len & 0xff);
	hdr[2] = (unsigned char)((pkt->len >> 8) & 0xff);
	write_buf_or_fail(fd, hdr, PKT_HDR);

	{
		size_t plen = packet_payload_len(pkt);

		if (plen)
			write_buf_or_fail(fd, pkt->u.buf, plen);
	}
}

/* ---- helpers --------------------------------------------------------- */

static void
chomp(char *s)
{
	size_t n = strlen(s);
	while (n > 0 && (s[n - 1] == '\n' || s[n - 1] == '\r' ||
	                 s[n - 1] == ' '  || s[n - 1] == '\t'))
		s[--n] = '\0';
}

/* In-place sanitize: '/' and ' ' → '_'; keep [A-Za-z0-9._-]. */
static void
sanitize(char *s)
{
	char *r = s, *w = s;
	while (*r)
	{
		char c = *r++;
		if (c == '/' || c == ' ')
			c = '_';
		if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
		    (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')
			*w++ = c;
	}
	*w = '\0';
}

/* popen `cmd`, capture first line of stdout (trimmed) into out. Returns 0
** on success and non-empty output, -1 otherwise. */
static int
run_capture(const char *cmd, char *out, size_t outsz)
{
	FILE *fp = popen(cmd, "r");
	if (!fp)
		return -1;
	if (!fgets(out, (int)outsz, fp))
	{
		out[0] = '\0';
		pclose(fp);
		return -1;
	}
	chomp(out);
	pclose(fp);
	return out[0] ? 0 : -1;
}

/* The user config: $DCH_CONFIG, else $XDG_CONFIG_HOME/dch/config, else
** ~/.config/dch/config. Into out[cap]; 0 when no path can be formed. */
int
dch_config_path(char *out, size_t cap)
{
	const char *e = getenv("DCH_CONFIG"), *x = getenv("XDG_CONFIG_HOME"),
	           *h = getenv("HOME");

	if (e && *e)
		return snprintf(out, cap, "%s", e) < (int)cap;
	if (x && *x)
		return snprintf(out, cap, "%s/dch/config", x) < (int)cap;
	return h && *h && snprintf(out, cap, "%s/.config/dch/config", h) <
	                  (int)cap;
}

/* The value of a config line that sets `key`, cut in place, else NULL. */
static char *
config_line(char *line, const char *key)
{
	char *p = line, *v, *e;
	size_t kl = strlen(key);

	while (*p == ' ' || *p == '\t')
		p++;
	if (strncmp(p, key, kl))
		return NULL;
	v = p + kl;
	while (*v == ' ' || *v == '\t')
		v++;
	if (*v++ != '=')
		return NULL;
	while (*v == ' ' || *v == '\t')
		v++;
	e = v + strlen(v);
	while (e > v && (e[-1] == '\n' || e[-1] == '\r' ||
	                 e[-1] == ' ' || e[-1] == '\t'))
		e--;
	*e = '\0';
	return v;
}

/* Value of `key` in the config ("key = value" lines, '#' comments, blanks
** around both trimmed, last one wins) into out[cap]. 1 when set, else 0.
** Read fresh on every call: the file is tiny, and an edit (by hand,
** `dch --config`, or Prosper) takes effect on the next read. */
int
dch_config_get(const char *key, char *out, size_t cap)
{
	char path[1024], line[1024], *v;
	int found = 0;
	FILE *f;

	if (!dch_config_path(path, sizeof(path)) || !(f = fopen(path, "r")))
		return 0;
	while (fgets(line, sizeof(line), f))
		if ((v = config_line(line, key)))
		{
			snprintf(out, cap, "%s", v);
			found = 1;
		}
	fclose(f);
	return found;
}

/* The keys `dch --config` and the picker's settings view know (picker.h
** has their struct and NCONF); bool ones take dch_config_bool's words,
** choice ones only their own. */
const struct dch_conf config_keys[] = {
	/* the master answers permission prompts */
	{ "auto_allow", 1, "false", NULL,
	  "Answer plain permission prompts with Yes" },
	/* the picker's sidekick sessions; empty: no Sidekicks tab */
	{ "sidekick_pattern", 0, "-qa", NULL,
	  "Sessions listed under Sidekicks (a regex)" },
	/* the picker's sort, its s key */
	{ "picker_sort", 0, "activity", "activity|name",
	  "How the session list is ordered" },
	/* what one Ctrl-\ press does; two do the other (#082) */
	{ "detach_key", 0, "detach", "detach|switch",
	  "What one Ctrl-\\ press does; two do the other" },
	/* the remote hosts the picker (and --remote) lists (#087) */
	{ "remote_hosts", 0, "", NULL,
	  "Remote hosts: [label=]host[:port],...; empty: your tailnet "
	  "peers; off: none" },
	/* the attach client takes the session's size on focus-in (#100) */
	{ "focus_resize", 1, "true", NULL,
	  "The focused client sets the session's size" },
	/* the X-ray shell ring's size (#119), read when a session starts */
	{ "xray_ring", 0, "2M", NULL,
	  "Shell output kept for X-ray: 0 (off) to 16M" },
};
_Static_assert(sizeof config_keys / sizeof *config_keys == NCONF,
               "picker.h's NCONF counts config_keys");

static int
config_bool_word(const char *v)
{
	static const char *w[] = { "true", "false", "1", "0", "yes", "no",
	                           "on", "off" };
	size_t i;

	for (i = 0; i < sizeof w / sizeof *w; i++)
		if (!strcasecmp(v, w[i]))
			return 1;
	return 0;
}

/* v's place in choices ("a|b|c"), or NULL when it is not one of them. */
static const char *
config_choice(const char *choices, const char *v)
{
	size_t n = strlen(v), l;

	for (;;)
	{
		l = strcspn(choices, "|");
		if (l == n && !strncmp(choices, v, n))
			return choices;
		if (!choices[l])
			return NULL;
		choices += l + 1;
	}
}

/* The index of key in config_keys, or -1. */
static int
config_key(const char *key)
{
	size_t k;

	for (k = 0; k < NCONF; k++)
		if (!strcmp(key, config_keys[k].key))
			return (int)k;
	return -1;
}

/* Key k's value as `dch --config` shows it: a bool as true or false, the
** default when unset. *set: whether the file has it. */
const char *
config_show(int k, char *v, size_t cap, int *set)
{
	*set = dch_config_get(config_keys[k].key, v, cap);
	if (config_keys[k].is_bool)
		return (*set ? dch_config_bool(config_keys[k].key)
		             : !strcmp(config_keys[k].def, "true"))
		       ? "true" : "false";
	return *set ? v : config_keys[k].def;
}

/* Set key k to s, or remove it (s NULL): `dch --config` and the picker's
** settings view both write here. Every other line stays (comments too), the
** key's first line is replaced and its later ones dropped, and it lands
** whole: a temp file renamed over the old (the target of a symlinked one).
** 0, or -1 with errno; EINVAL for a value the key does not take. */
static int
config_set(int k, const char *s)
{
	char path[1024], tmp[1100], *rp;
	const char *key = config_keys[k].key;
	int fd, done = 0, ok, e, mode = 0644;
	struct stat st;
	FILE *in, *out;

	if (s && (strpbrk(s, "\r\n") || strlen(s) > 900 ||
	          (config_keys[k].is_bool && !config_bool_word(s)) ||
	          (config_keys[k].choices &&
	           !config_choice(config_keys[k].choices, s))))
	{
		errno = EINVAL;
		return -1;
	}
	if (!dch_config_path(path, sizeof path))
	{
		errno = ENOENT;
		return -1;
	}
	if ((rp = realpath(path, NULL)))
		snprintf(path, sizeof path, "%s", rp);
	free(rp);
	/* mkdir -p the parent; a failure shows as the temp file's */
	for (rp = strchr(path + 1, '/'); rp; rp = strchr(rp + 1, '/'))
	{
		*rp = '\0';
		(void)mkdir(path, 0700);
		*rp = '/';
	}
	if (stat(path, &st) == 0)
		mode = st.st_mode & 07777;
	snprintf(tmp, sizeof tmp, "%s.%ld.tmp", path, (long)getpid());
	if ((fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW,
	               0600)) < 0)
		return -1;
	if (!(out = fdopen(fd, "w")))
	{
		e = errno;
		close(fd);
		unlink(tmp);
		errno = e;
		return -1;
	}
	fchmod(fd, (mode_t)mode);
	if ((in = fopen(path, "r")))
	{
		char *line = NULL, *cut;
		size_t lcap = 0;
		ssize_t ll;

		while ((ll = getline(&line, &lcap, in)) > 0)
		{
			if (!(cut = strdup(line)) || !config_line(cut, key))
			{
				fputs(line, out);
				if (line[ll - 1] != '\n')
					fputc('\n', out);
			}
			else if (!done++ && s)
				fprintf(out, "%s = %s\n", key, s);
			free(cut);
		}
		free(line);
		fclose(in);
	}
	if (!done && s)
		fprintf(out, "%s = %s\n", key, s);
	ok = fflush(out) == 0 && fsync(fd) == 0;
	e = errno;
	if (fclose(out) != 0 || !ok || rename(tmp, path) < 0)
	{
		if (ok)
			e = errno;	/* fclose's or rename's */
		unlink(tmp);
		errno = e;
		return -1;
	}
	return 0;
}

/* dch --shell-init zsh|bash|fish (#119): the OSC 133 A/B/C/D hooks for an
** rc file, so a shell inside dch marks its prompts, commands and exit codes
** for X-ray. Idempotent (sourcing twice adds nothing); a shell or terminal
** integration that already emits OSC 133 needs none of it. */
static const char shell_init_zsh[] =
"# dch --shell-init zsh: OSC 133 prompt marks for dch's X-ray\n"
"if [[ -z ${__dch_osc133-} ]]; then\n"
"  typeset -g __dch_osc133=1 __dch_ran=\n"
"  autoload -Uz add-zsh-hook\n"
"  __dch_precmd() {\n"
"    local ret=$?\n"
"    [[ -n $__dch_ran ]] && printf '\\033]133;D;%s\\007' $ret\n"
"    __dch_ran=\n"
"    printf '\\033]133;A\\007'\n"
"    [[ $PS1 == *'133;B'* ]] || PS1=$PS1$'%{\\e]133;B\\a%}'\n"
"    return $ret\n"
"  }\n"
"  __dch_preexec() { printf '\\033]133;C\\007'; __dch_ran=1; }\n"
"  add-zsh-hook precmd __dch_precmd\n"
"  add-zsh-hook preexec __dch_preexec\n"
"fi\n";
static const char shell_init_bash[] =
"# dch --shell-init bash: OSC 133 prompt marks for dch's X-ray (bash 4.4+)\n"
"if [[ -z ${__dch_osc133-} ]]; then\n"
"  __dch_osc133=1 __dch_ran=\n"
"  __dch_prompt() {\n"
"    local ret=$?\n"
"    [[ -n $__dch_ran ]] && printf '\\033]133;D;%s\\007' \"$ret\"\n"
"    __dch_ran=\n"
"    printf '\\033]133;A\\007'\n"
"    return $ret\n"
"  }\n"
"  __dch_ps1() {\n"
"    local ret=$?\n"
"    [[ $PS1 == *'133;B'* ]] || PS1=$PS1'\\[\\e]133;B\\a\\]'\n"
"    return $ret\n"
"  }\n"
"  PROMPT_COMMAND=\"__dch_prompt${PROMPT_COMMAND:+;$PROMPT_COMMAND};__dch_ps1\"\n"
"  # PS0 runs as a command starts; the subscript sets __dch_ran here (the\n"
"  # '-' keeps set -u quiet: an unset element is unbound, declared or not)\n"
"  PS0=${PS0-}'\\e]133;C\\a${__dch_nil[__dch_ran=1]-}'\n"
"fi\n";
static const char shell_init_fish[] =
"# dch --shell-init fish: OSC 133 prompt marks for dch's X-ray (fish 4\n"
"# emits them itself)\n"
"if not set -q __dch_osc133; and test (string split . $version)[1] -lt 4\n"
"  set -g __dch_osc133 1\n"
"  function __dch_prompt_start --on-event fish_prompt\n"
"    printf '\\033]133;A\\007'\n"
"  end\n"
"  functions -q fish_prompt; and functions -c fish_prompt __dch_fish_prompt\n"
"  function fish_prompt\n"
"    functions -q __dch_fish_prompt; and __dch_fish_prompt\n"
"    printf '\\033]133;B\\007'\n"
"  end\n"
"  function __dch_preexec --on-event fish_preexec\n"
"    printf '\\033]133;C\\007'\n"
"  end\n"
"  function __dch_postexec --on-event fish_postexec\n"
"    printf '\\033]133;D;%s\\007' $status\n"
"  end\n"
"end\n";

static int
shell_init_main(int argc, char **argv)
{
	const char *sh = argc == 1 ? argv[0] : "";

	if (!strcmp(sh, "zsh"))
		fputs(shell_init_zsh, stdout);
	else if (!strcmp(sh, "bash"))
		fputs(shell_init_bash, stdout);
	else if (!strcmp(sh, "fish"))
		fputs(shell_init_fish, stdout);
	else
	{
		fprintf(stderr, "usage: dch --shell-init zsh|bash|fish\n");
		return 2;
	}
	return 0;
}

/* dch --config [<key> [<value>|--unset]]: show the config, one key, or set
** or remove one (config_set). */
static int
config_main(int argc, char **argv)
{
	char path[1024], v[1024];
	const char *key = argc ? argv[0] : NULL, *sv;
	int k, set;

	if (!dch_config_path(path, sizeof path))
	{
		fprintf(stderr, "dch: no config path (set HOME or DCH_CONFIG)\n");
		return 1;
	}
	if (!key)
	{
		printf("%s\n", path);
		for (k = 0; k < (int)NCONF; k++)
		{
			sv = config_show(k, v, sizeof v, &set);
			printf("%s = %s\n", config_keys[k].key, sv);
		}
		return 0;
	}
	if ((k = config_key(key)) < 0 || argc > 2)
	{
		fprintf(stderr, argc > 2 ? "usage: dch --config [<key> "
		        "[<value>|--unset]]\n" : "dch: unknown config key: %s\n",
		        key);
		return 1;
	}
	if (argc == 1)
	{
		if (!dch_config_get(key, v, sizeof v))
			return 1;
		printf("%s\n", v);
		return 0;
	}
	if (config_set(k, strcmp(argv[1], "--unset") ? argv[1] : NULL) == 0)
		return 0;
	if (errno == EINVAL && config_keys[k].is_bool)
		fprintf(stderr, "dch: %s takes true or false\n", key);
	else if (errno == EINVAL && config_keys[k].choices)
		fprintf(stderr, "dch: %s takes %s\n", key,
		        config_keys[k].choices);
	else if (errno == EINVAL)
		fprintf(stderr, "dch: %s: one line, at most 900 bytes\n", key);
	else
		fprintf(stderr, "dch: write %s: %s\n", path, strerror(errno));
	return 1;
}

/* A boolean config key: "true"/"1"/"yes"/"on" is 1, anything else 0. */
int
dch_config_bool(const char *key)
{
	char v[16];

	return dch_config_get(key, v, sizeof(v)) &&
	       (!strcasecmp(v, "true") || !strcmp(v, "1") ||
	        !strcasecmp(v, "yes") || !strcasecmp(v, "on"));
}

/* A DCH_NO_* switch: unset, empty or "0" is off, anything else on. */
int
dch_env_on(const char *key)
{
	const char *e = getenv(key);

	return e && *e && strcmp(e, "0") != 0;
}

/* The session socket dir, for serve.c (once main() has computed it). */
const char *
dch_sock_dir(void)
{
	return sock_dir;
}

/* SOCK_DIR = $DCH_SOCKET_DIR, else $XDG_RUNTIME_DIR/dch-$UID, fallback
** /tmp/dch-$UID. mkdir 0700. DCH_SOCKET_DIR (when set) is used verbatim — a host
** process (e.g. a GUI app spawning sessions) can pin one deterministic dir so it
** and a shell-launched dch agree, regardless of the GUI-vs-shell environment
** split that otherwise leaves XDG_RUNTIME_DIR set in one but not the other. Only
** the leaf is created, same as the XDG path; the caller owns the parent. */
static int
compute_sock_dir(void)
{
	const char *dir = getenv("DCH_SOCKET_DIR");
	const char *xdg = getenv("XDG_RUNTIME_DIR");
	uid_t uid = getuid();
	if (dir && *dir)
		snprintf(sock_dir, sizeof(sock_dir), "%s", dir);
	else if (xdg && *xdg)
		snprintf(sock_dir, sizeof(sock_dir), "%s/dch-%u",
		         xdg, (unsigned)uid);
	else
		snprintf(sock_dir, sizeof(sock_dir), "/tmp/dch-%u",
		         (unsigned)uid);
	if (mkdir(sock_dir, 0700) < 0 && errno != EEXIST)
	{
		fprintf(stderr, "dch: mkdir %s: %s\n", sock_dir,
		        strerror(errno));
		return -1;
	}
	/* EEXIST is the common case, so the directory we just accepted may be
	** one somebody else created. Owning it means owning the names in it:
	** the socket, and now the resume blob a live restart hands to the next
	** image. Sockets and blobs are 0600 and validated, but a name an
	** attacker can unlink between save and exec still kills the session. */
	{
		struct stat st;

		/* Write access is the whole threat: the files in here are 0600
		** and validated on read, so being able to list the directory
		** costs nothing. Being able to create or unlink names in it is
		** what turns a restart into a lost session. */
		if (lstat(sock_dir, &st) < 0 || !S_ISDIR(st.st_mode) ||
		    st.st_uid != uid || (st.st_mode & (S_IWGRP | S_IWOTH)))
		{
			fprintf(stderr, "dch: %s must be a directory owned by "
			        "uid %u and not writable by group or other\n",
			        sock_dir, (unsigned)uid);
			return -1;
		}
	}
	return 0;
}

/* ---- debug tracing -------------------------------------------------- */

/* Opt-in trace sink, resolved once from DCH_DEBUG (see dtach.h). Off by
** default: when DCH_DEBUG is unset, the first call caches trace_fp = NULL and
** every call is a cheap early return. Each line is prefixed with a wall-clock
** timestamp and the pid so client/master interleaving is readable. */
void
dch_trace(const char *fmt, ...)
{
	static FILE *trace_fp;
	static int resolved;
	va_list ap;
	struct timeval tv;

	if (!resolved)
	{
		const char *e = getenv("DCH_DEBUG");
		char path[64];

		resolved = 1;
		if (e && *e)
		{
			if (strcmp(e, "1") == 0)
			{
				snprintf(path, sizeof(path),
				         "/tmp/dch.%u.trace",
				         (unsigned)getuid());
				e = path;
			}
			trace_fp = fopen(e, "a");
			if (trace_fp)
			{
				/* Don't leak the sink across the master re-exec
				** into the inner program. ("e"/O_CLOEXEC fopen
				** mode is a GNU extension, unreliable on macOS.) */
				fcntl(fileno(trace_fp), F_SETFD, FD_CLOEXEC);
				setvbuf(trace_fp, NULL, _IOLBF, 0);
			}
		}
	}
	if (!trace_fp)
		return;

	gettimeofday(&tv, NULL);
	fprintf(trace_fp, "%ld.%06ld pid=%d ", (long)tv.tv_sec,
	        (long)tv.tv_usec, (int)getpid());
	va_start(ap, fmt);
	vfprintf(trace_fp, fmt, ap);
	va_end(ap);
	fputc('\n', trace_fp);
}

/* Normalize a session name to the on-disk basename. Long names that would
** overflow AF_UNIX sun_path are deterministically shortened (prefix + djb2
** hash) so create and attach agree. Every sidecar path (.act/.state/.alias)
** must derive from this same normalization or long-named sessions read the
** wrong sidecars (the master writes them under the hashed name). Returns
** `name` unchanged when it fits, else `buf`. */
static const char *
canon_name(const char *name, char *buf, size_t bufsz)
{
	struct sockaddr_un sa;

	if (strlen(sock_dir) + 7 >= sizeof(sa.sun_path))
		return name; /* pathological sock_dir; make_sock_path errors */
	/* Chars available for the name itself: "<sock_dir>/" + name + ".sock\0". */
	size_t budget = sizeof(sa.sun_path) - strlen(sock_dir) - 1 - 5 - 1;
	if (strlen(name) <= budget || budget <= 8)
		return name;
	/* djb2 hash of the full name -> 6 hex chars for uniqueness. */
	unsigned long h = 5381;
	for (const char *p = name; *p; p++)
		h = ((h << 5) + h) + (unsigned char)*p;
	int keep = (int)budget - 7; /* '-' + 6 hex */
	snprintf(buf, bufsz, "%.*s-%06lx", keep, name, h & 0xffffff);
	return buf;
}

/* Build sock_path. Long names are shortened via canon_name so it "just
** works" regardless of cwd/branch length. */
static int
make_sock_path(const char *name)
{
	struct sockaddr_un sa;
	char shortened[256];
	/* Guard before canon_name's subtraction underflows (size_t) on a
	** pathological sock_dir — the final snprintf check would still catch
	** it, but this is clearer and fails fast. */
	if (strlen(sock_dir) + 7 >= sizeof(sa.sun_path))
	{
		fprintf(stderr, "dch: socket directory path too long\n");
		return -1;
	}
	name = canon_name(name, shortened, sizeof(shortened));
	int n = snprintf(sock_path, sizeof(sock_path),
	                 "%s/%s.sock", sock_dir, name);
	if (n < 0 || (size_t)n >= sizeof(sock_path) ||
	    (size_t)n >= sizeof(sa.sun_path))
	{
		fprintf(stderr, "dch: socket path too long\n");
		return -1;
	}
	sockname = sock_path;
	return 0;
}

/* Build <sock_dir>/<canon(name)>.sock into buf (for existence checks that
** don't want to touch the global sock_path). */
static void
session_sock_path(const char *name, char *buf, size_t bufsz)
{
	char cb[256];
	snprintf(buf, bufsz, "%s/%s.sock", sock_dir,
	         canon_name(name, cb, sizeof(cb)));
}

/* host:name and no local session of that exact name: a remote one (#087). */
static int
name_is_remote(const char *name)
{
	char sp[1100];
	struct stat st;

	session_sock_path(name, sp, sizeof sp);
	return strchr(name, ':') && (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode));
}

/* Read first line of `path` (NUL-terminated, newline stripped) into out.
** Returns 0 on success and non-empty content. */
static int
read_first_line(const char *path, char *out, size_t outsz)
{
	int fd = open(path, O_RDONLY);
	if (fd < 0)
		return -1;
	ssize_t r;
	do {
		r = read(fd, out, outsz - 1);
	} while (r < 0 && errno == EINTR);
	close(fd);
	if (r <= 0)
		return -1;
	out[r] = '\0';
	char *nl = strchr(out, '\n');
	if (nl)
		*nl = '\0';
	chomp(out);
	return out[0] ? 0 : -1;
}

/* Walk parents from cwd looking for a .git directory or file (worktree).
** On hit, fills `root` with the repo toplevel (parent of .git) and `gitdir`
** with the actual git dir (resolves "gitdir: <path>" indirection). */
static int
find_git(char *root, size_t rootsz, char *gitdir, size_t gitdirsz)
{
	char cur[2048];
	if (!getcwd(cur, sizeof(cur)))
		return -1;

	while (1)
	{
		char dot[2200];
		int n = snprintf(dot, sizeof(dot), "%s/.git", cur);
		if (n < 0 || (size_t)n >= sizeof(dot))
			return -1;
		struct stat st;
		if (stat(dot, &st) == 0)
		{
			/* A cut path names another directory: fail instead. */
			if ((size_t)snprintf(root, rootsz, "%s", cur) >= rootsz)
				return -1;
			if (S_ISDIR(st.st_mode))
				return (size_t)snprintf(gitdir, gitdirsz, "%s",
				                        dot) < gitdirsz ? 0 : -1;
			/* .git file → "gitdir: <path>" (worktree/submodule). */
			char line[2048];
			if (read_first_line(dot, line, sizeof(line)) == 0 &&
			    strncmp(line, "gitdir: ", 8) == 0)
			{
				const char *p = line + 8;
				if (p[0] == '/')
					n = snprintf(gitdir, gitdirsz, "%s", p);
				else
					n = snprintf(gitdir, gitdirsz,
					             "%s/%s", cur, p);
				return (size_t)n < gitdirsz ? 0 : -1;
			}
			return -1;
		}
		char *slash = strrchr(cur, '/');
		if (!slash || slash == cur)
			return -1;
		*slash = '\0';
	}
}

/* Auto-name: <repo>-<branch> if cwd is a git repo, else cwd basename.
** Native: stat + open on .git/HEAD — no fork/popen. */
static void
auto_name(char *out, size_t outsz)
{
	/* base + "-" + branch fits a session name: a name, cut, is still a name */
	char root[2048], gitdir[2048], head[512], base[256], branch[256];

	if (find_git(root, sizeof(root), gitdir, sizeof(gitdir)) == 0)
	{
		const char *slash = strrchr(root, '/');
		const char *bn = slash ? slash + 1 : root;
		snprintf(base, sizeof(base), "%.*s", (int)sizeof base - 1, bn);
		sanitize(base);

		char headpath[2200];
		snprintf(headpath, sizeof(headpath), "%s/HEAD", gitdir);
		if (read_first_line(headpath, head, sizeof(head)) == 0)
		{
			if (strncmp(head, "ref: refs/heads/", 16) == 0)
				snprintf(branch, sizeof(branch), "%.*s",
				         (int)sizeof branch - 1, head + 16);
			else if (strncmp(head, "ref: ", 5) == 0)
			{
				/* Non-branch ref (tag, packed-ref, …). */
				const char *r = head + 5;
				const char *s = strrchr(r, '/');
				snprintf(branch, sizeof(branch), "%.*s",
				         (int)sizeof branch - 1, s ? s + 1 : r);
			}
			else
			{
				/* Detached HEAD: bare hash, use 7-char short. */
				snprintf(branch, sizeof(branch), "%.7s", head);
			}
		}
		else
			snprintf(branch, sizeof(branch), "detached");

		sanitize(branch);
		snprintf(out, outsz, "%s-%s", base, branch);
		if (out[0] == '-')	/* a dir named -x: no name starts so (#162) */
			out[0] = '_';
		return;
	}

	char cwd[2048];
	if (!getcwd(cwd, sizeof(cwd)))
		snprintf(cwd, sizeof(cwd), "session");
	const char *slash = strrchr(cwd, '/');
	const char *bn = slash ? slash + 1 : cwd;
	snprintf(out, outsz, "%.*s", (int)outsz - 1, bn);
	sanitize(out);
	if (out[0] == '-')
		out[0] = '_';
}

/* ---- client tracking (PID files) ------------------------------------ */
/*
** Bash dch used `pgrep -f 'dtach -a +<sock>'` to find live clients. We've
** merged dtach and dch into one binary, so pgrep can't distinguish client
** from server. Instead each client touches <sockdir>/<base>.client.<pid>
** at attach time and unlinks at exit. Liveness = file exists + kill(pid,0)
** + not orphaned (PPID=1 with no TTY = VSCode-helper-closed).
*/

/* Also from attach.c's die(), in a signal handler: unlink only. Not in a
** fork that exit()s (the file is its parent's). */
void
client_pidfile_unlink(void)
{
	if (client_pidfile[0] && getpid() == client_pid)
		unlink(client_pidfile);
}

static void
client_pidfile_create(const char *sockp)
{
	const char *slash = strrchr(sockp, '/');
	const char *bn = slash ? slash + 1 : sockp;
	/* A session switch calls this again in the same process: drop the
	** previous session's marker, or live_clients_on() counts us as still
	** attached there forever. */
	if (client_pidfile[0])
		unlink(client_pidfile);
	client_pid = getpid();
	if ((size_t)snprintf(client_pidfile, sizeof(client_pidfile),
	                     "%s/%s.client.%d", sock_dir, bn, (int)getpid()) >=
	    sizeof(client_pidfile))
	{
		client_pidfile[0] = '\0';	/* a cut name is another file */
		return;
	}
	int fd = open(client_pidfile, O_CREAT | O_WRONLY | O_TRUNC, 0600);
	if (fd >= 0)
		close(fd);
	{
		static int trapped;

		if (!trapped)
		{
			trapped = 1;
			atexit(client_pidfile_unlink);
		}
	}
}

/* Return 1 if pid has PPID=1 and no controlling TTY (VSCode-helper-orphan
** signature). Cross-platform via ps. */
static int
pid_is_orphan(int pid)
{
	char pscmd[128], psout[256];
	snprintf(pscmd, sizeof(pscmd),
	         "ps -o ppid=,tty= -p %d 2>/dev/null", pid);
	if (run_capture(pscmd, psout, sizeof(psout)) != 0)
		return 0;
	char *p = psout;
	while (*p == ' ')
		p++;
	long ppid = strtol(p, &p, 10);
	while (*p == ' ' || *p == '\t')
		p++;
	chomp(p);
	int no_tty = (*p == '\0' || strcmp(p, "??") == 0 ||
	              strcmp(p, "?") == 0 || strcmp(p, "-") == 0);
	return ppid == 1 && no_tty;
}

/* Live (non-orphan) client count for `<base>.sock`. Reaps stale entries. */
static int
live_clients_on(const char *sockp)
{
	const char *slash = strrchr(sockp, '/');
	const char *bn = slash ? slash + 1 : sockp;
	char prefix[600];
	/* a cut prefix would match (and reap) another session's clients */
	if ((size_t)snprintf(prefix, sizeof(prefix), "%s.client.", bn) >=
	    sizeof(prefix))
		return 0;
	size_t plen = strlen(prefix);
	DIR *d = opendir(sock_dir);
	if (!d)
		return 0;

	int count = 0;
	struct dirent *de;
	while ((de = readdir(d)))
	{
		if (strncmp(de->d_name, prefix, plen) != 0)
			continue;
		int pid = atoi(de->d_name + plen);
		char path[2200];
		snprintf(path, sizeof(path), "%s/%s", sock_dir, de->d_name);

		if (pid <= 0 || kill(pid, 0) != 0)
		{
			unlink(path); /* dead */
			continue;
		}
		if (pid_is_orphan(pid))
		{
			kill(pid, SIGTERM);
			unlink(path);
			continue;
		}
		count++;
	}
	closedir(d);
	return count;
}

/* Send SIGUSR1 to all live clients of `<base>.sock` (clean detach). */
static int
detach_all_clients_on(const char *sockp)
{
	const char *slash = strrchr(sockp, '/');
	const char *bn = slash ? slash + 1 : sockp;
	char prefix[600];
	if ((size_t)snprintf(prefix, sizeof(prefix), "%s.client.", bn) >=
	    sizeof(prefix))
		return 0;
	size_t plen = strlen(prefix);
	DIR *d = opendir(sock_dir);
	if (!d)
		return 0;

	int n = 0;
	struct dirent *de;
	while ((de = readdir(d)))
	{
		if (strncmp(de->d_name, prefix, plen) != 0)
			continue;
		int pid = atoi(de->d_name + plen);
		if (pid <= 0)
			continue;
		/* Liveness check first so we don't SIGUSR1 a recycled PID. */
		if (kill(pid, 0) != 0)
		{
			char path[2200];
			snprintf(path, sizeof(path), "%s/%s",
			         sock_dir, de->d_name);
			unlink(path);
			continue;
		}
		if (kill(pid, SIGUSR1) == 0)
			n++;
	}
	closedir(d);
	return n;
}

/* ---- session listing / picker --------------------------------------- */

struct slist
{
	char **v;       /* real session names (socket basenames) */
	char **alias;   /* display alias or NULL; parallel to v */
	char **harness; /* live Claude Code or Codex session name, or NULL */
	int n, cap;
};

static void
slist_push(struct slist *l, const char *s)
{
	if (l->n == l->cap)
	{
		l->cap = l->cap ? l->cap * 2 : 8;
		l->v = realloc(l->v, l->cap * sizeof(char *));
		l->alias = realloc(l->alias, l->cap * sizeof(char *));
		l->harness = realloc(l->harness, l->cap * sizeof(char *));
	}
	l->alias[l->n] = NULL;
	l->harness[l->n] = NULL;
	l->v[l->n++] = strdup(s);
}

static void
slist_free(struct slist *l)
{
	int i;
	for (i = 0; i < l->n; i++)
	{
		free(l->v[i]);
		free(l->alias[i]);
		free(l->harness[i]);
	}
	free(l->v);
	free(l->alias);
	free(l->harness);
	l->v = NULL;
	l->alias = NULL;
	l->harness = NULL;
	l->n = l->cap = 0;
}

/* Order a list for a person to read: by the shown label (alias, else
** harness name, else session name), case-insensitive, ties by name. */
static const char *
slist_label(struct slist *l, int i)
{
	return l->alias[i] ? l->alias[i] : l->harness[i] ? l->harness[i] : l->v[i];
}

static void
slist_sort(struct slist *l)
{
	/* ponytail: insertion sort, O(n^2); fine for a screenful of sessions */
	for (int i = 1; i < l->n; i++)
		for (int j = i; j > 0; j--)
		{
			int c = strcasecmp(slist_label(l, j - 1), slist_label(l, j));
			if (c < 0 || (c == 0 && strcmp(l->v[j - 1], l->v[j]) <= 0))
				break;
			char *t = l->v[j]; l->v[j] = l->v[j - 1]; l->v[j - 1] = t;
			t = l->alias[j]; l->alias[j] = l->alias[j - 1]; l->alias[j - 1] = t;
			t = l->harness[j]; l->harness[j] = l->harness[j - 1]; l->harness[j - 1] = t;
		}
}

/* List active session names (sockets in sock_dir). */
static int
list_sessions(struct slist *out)
{
	DIR *d = opendir(sock_dir);
	if (!d)
		return -1;
	struct dirent *de;
	while ((de = readdir(d)))
	{
		size_t n = strlen(de->d_name);
		if (n <= 5 || strcmp(de->d_name + n - 5, ".sock") != 0)
			continue;
		char nm[600];
		snprintf(nm, sizeof(nm), "%.*s", (int)(n - 5), de->d_name);
		slist_push(out, nm);
	}
	closedir(d);
	return 0;
}

/* Build the alias-sidecar path: <sock_dir>/<name>.sock.alias */
static int
alias_path(const char *name, char *out, size_t outsz)
{
	char cb[256];
	int n = snprintf(out, outsz, "%s/%s.sock.alias", sock_dir,
	                 canon_name(name, cb, sizeof(cb)));
	return (n < 0 || (size_t)n >= outsz) ? -1 : 0;
}

/* Last-activity epoch (mtime of <sock_dir>/<name>.sock.act, written by the
** master on pty output). 0 when the session has produced no output yet / no
** sidecar. Lets a watcher tell a detached-but-working session from an idle one. */
static long
activity_epoch(const char *name)
{
	char ap[1300], cb[256];
	struct stat st;
	int n = snprintf(ap, sizeof ap, "%s/%s.sock.act", sock_dir,
	                 canon_name(name, cb, sizeof(cb)));

	if (n < 0 || (size_t)n >= sizeof ap || stat(ap, &st) != 0)
		return 0;
	return (long)st.st_mtime;
}

/* A session is "working" when the master stamped pty output within this
** window, "idle" otherwise. The master throttles stamps to 1/s, so 5s
** gives headroom without hiding real activity. (v1.3 printed "active"
** here; collapsed into "working" so the busy concept has one name across
** heuristic, detection and reports. --wait keeps "active" as an alias.) */
#define DCH_ACTIVE_SECS 5

static const char *
state_of(long ep)
{
	return (ep && time(NULL) - ep <= DCH_ACTIVE_SECS) ? "working"
	                                                  : "idle";
}

/* Path of the reported-state sidecar (written by --report, e.g. from a
** harness hook). Returns 0 on success, -1 on truncation. */
static int
state_file_path(const char *name, char *buf, size_t bufsz)
{
	char cb[256];
	int n = snprintf(buf, bufsz, "%s/%s.sock.state", sock_dir,
	                 canon_name(name, cb, sizeof(cb)));
	return (n < 0 || (size_t)n >= bufsz) ? -1 : 0;
}

/* Path of the metadata sidecar (written by --meta, e.g. from a status line).
** Returns 0 on success, -1 on truncation. */
static int
meta_file_path(const char *name, char *buf, size_t bufsz)
{
	char cb[256];
	int n = snprintf(buf, bufsz, "%s/%s.sock.meta", sock_dir,
	                 canon_name(name, cb, sizeof(cb)));
	return (n < 0 || (size_t)n >= bufsz) ? -1 : 0;
}

/* Session metadata is one JSON object, at most this many bytes, no byte
** below 0x20 (keeps --ls-json one line per call). */
#define DCH_META_MAX 16384

static int
meta_bytes_ok(const char *s, size_t n)
{
	size_t i;
	if (n == 0 || n > DCH_META_MAX || s[0] != '{')
		return 0;
	for (i = 0; i < n; i++)
		if ((unsigned char)s[i] < 0x20)
			return 0;
	return 1;
}

/* Stored metadata, or NULL; *epoch gets the sidecar mtime (when the content
** last changed: --meta skips identical pushes) or 0. The file is validated
** in full again on read: a truncated, hand-edited or third-party-written
** sidecar printed verbatim would break the whole --ls-json array, not just
** this row. NOFOLLOW + NONBLOCK + S_ISREG + uid, as for .ver: the runtime dir
** is shared. Static buffer; valid until the next call. */
static const char *
session_meta(const char *name, long *epoch)
{
	static char buf[DCH_META_MAX + 1];
	char p[1300];
	struct stat st;
	ssize_t r = -1;
	int fd;

	*epoch = 0;
	if (meta_file_path(name, p, sizeof(p)) != 0 ||
	    (fd = open(p, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)) < 0)
		return NULL;
	if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) &&
	    st.st_uid == getuid() && st.st_size <= DCH_META_MAX)
		r = read(fd, buf, DCH_META_MAX + 1);
	close(fd);
	if (r <= 0 || !meta_bytes_ok(buf, (size_t)r))
		return NULL;
	buf[r] = '\0';
	if (!dch_json_is_object(buf))
		return NULL;
	*epoch = (long)st.st_mtime;
	return buf;
}

/* The prompt the master auto-allowed last (#074): <sock>.allowed holds its
** title, *epoch gets the mtime (when). NULL when there is none. Opened as
** session_meta opens .meta. Static buffer; valid until the next call. */
static const char *
session_allowed(const char *name, long *epoch)
{
	static char buf[1100];
	char p[1300], cb[256];
	struct stat st;
	ssize_t r = -1;
	int fd;

	*epoch = 0;
	if (snprintf(p, sizeof p, "%s/%s.sock.allowed", sock_dir,
	             canon_name(name, cb, sizeof cb)) >= (int)sizeof p ||
	    (fd = open(p, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)) < 0)
		return NULL;
	if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) &&
	    st.st_uid == getuid())
		r = read(fd, buf, sizeof buf - 1);
	close(fd);
	if (r <= 0)
		return NULL;
	buf[r] = '\0';
	buf[strcspn(buf, "\r\n")] = '\0';
	*epoch = (long)st.st_mtime;
	return buf;
}

/* dch version of the master serving this session, from the `<sock>.ver`
** sidecar it stamps at startup. Returns NULL when the file is missing or
** unreadable — i.e. the master predates the sidecar, so "older than us" is the
** only safe reading. Static buffer; valid until the next call. */
static const char *
session_version(const char *name)
{
	static char ver[64];
	char p[1300], cb[256];
	int fd, r, n;

	n = snprintf(p, sizeof(p), "%s/%s.sock.ver", sock_dir,
	             canon_name(name, cb, sizeof(cb)));
	if (n < 0 || (size_t)n >= sizeof(p))
		return NULL;
	/* NOFOLLOW+NONBLOCK+S_ISREG: the runtime dir is shared, and a symlink
	** or a FIFO planted at this name would otherwise redirect the read or
	** hang it forever. */
	fd = open(p, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
	if (fd < 0)
		return NULL;
	{
		struct stat st;

		if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
		    st.st_uid != getuid())
		{
			close(fd);
			return NULL;
		}
	}
	r = (int)read(fd, ver, sizeof(ver) - 1);
	close(fd);
	if (r <= 0)
		return NULL;
	ver[r] = '\0';
	r = (int)strcspn(ver, "\r\n");
	ver[r] = '\0';
	/* Reject anything that isn't a plain version token: this string ends up
	** in --ls-json, and the sidecar lives in a world-listable runtime dir. */
	if (r == 0 || (size_t)r != strspn(ver, "0123456789.-abcdefghijklmnop"
	                                       "qrstuvwxyz"))
		return NULL;
	return ver;
}

static const char *
version_or_empty(const char *name)
{
	const char *v = session_version(name);

	return v ? v : "";
}

/* The closed set of reportable states (herdr's, plus done). A closed set
** keeps --status output a stable contract and --ls-json trivially valid. */
static int
valid_state_token(const char *tok)
{
	static const char *known[] = { "working", "idle", "blocked", "done" };
	size_t k;
	for (k = 0; k < sizeof(known) / sizeof(known[0]); k++)
		if (strcmp(tok, known[k]) == 0)
			return 1;
	return 0;
}

static const char *detect_state(const char *name);

/* Resolve a session's state. An explicit "done" report always wins; a
** detected on-screen blocker beats any other report (herdr's blocker-
** override — a stale "working" from a crashed harness hook must not
** stick until session death); then the reported state; then screen
** detection; then the output heuristic. Sidecar content is re-validated
** on read so a hand-mangled file never leaks into --status/--ls-json. */
static const char *
session_state(const char *name)
{
	static char st[40];
	char p[1300];
	const char *rep = NULL, *det;

	if (state_file_path(name, p, sizeof(p)) == 0)
	{
		int fd = open(p, O_RDONLY);
		if (fd >= 0)
		{
			int r = (int)read(fd, st, sizeof(st) - 1);
			close(fd);
			while (r > 0 && (st[r - 1] == '\n' || st[r - 1] == '\r'))
				r--;
			st[r > 0 ? r : 0] = '\0';
			if (r > 0 && valid_state_token(st))
				rep = st;
		}
	}
	if (rep && strcmp(rep, "done") == 0)
		return rep;
	det = detect_state(name);
	if (det && strcmp(det, "blocked") == 0)
		return det;
	if (rep)
		return rep;
	if (det)
		return det;
	return state_of(activity_epoch(name));
}

/* Fill sl->alias[i] from each session's sidecar file (NULL if none). */
static void
load_aliases(struct slist *sl)
{
	int i;
	for (i = 0; i < sl->n; i++)
	{
		char ap[1200], line[600];
		if (alias_path(sl->v[i], ap, sizeof(ap)) == 0 &&
		    read_first_line(ap, line, sizeof(line)) == 0)
		{
			free(sl->alias[i]);
			sl->alias[i] = strdup(line);
		}
	}
}

/* ---- harness session names ------------------------------------------ */

#ifdef __APPLE__
/* KERN_PROCARGS2 of a same-uid process: argc int, exec path, NULs, argv[],
** env[]. Static buffer, NUL-terminated; NULL on failure. */
static char *
procargs(long pid, size_t *lenp)
{
	static char *buf;
	static size_t bufsz;
	int mib[3] = { CTL_KERN, KERN_PROCARGS2, (int)pid };
	size_t len;

	if (!buf)
	{
		int am[2] = { CTL_KERN, KERN_ARGMAX };
		int argmax = 0;
		len = sizeof(argmax);
		if (sysctl(am, 2, &argmax, &len, NULL, 0) != 0 || argmax <= 0)
			argmax = 256 * 1024;
		bufsz = (size_t)argmax;
		buf = malloc(bufsz);
		if (!buf)
			return NULL;
	}
	len = bufsz;
	if (sysctl(mib, 3, buf, &len, NULL, 0) != 0 || len <= sizeof(int))
		return NULL;
	buf[len - 1] = '\0';
	*lenp = len;
	return buf;
}
#endif

/* Fetch one environment value from another same-uid process. macOS uses
** KERN_PROCARGS2; elsewhere /proc/<pid>/environ. */
static int
proc_env_value(long pid, const char *key, char *out, size_t outsz)
{
	size_t keylen = strlen(key);
#ifdef __APPLE__
	size_t len, i;
	char *buf = procargs(pid, &len);

	if (!buf)
		return -1;
	/* Layout: argc int, exec path, NULs, argv[], env[]. Entries are
	** NUL-separated; scan each entry start for our prefix. */
	for (i = sizeof(int); i < len; )
	{
		if (len - i > keylen + 1 && memcmp(buf + i, key, keylen) == 0 &&
		    buf[i + keylen] == '=')
		{
			snprintf(out, outsz, "%s", buf + i + keylen + 1);
			return 0;
		}
		while (i < len && buf[i] != '\0')
			i++;
		while (i < len && buf[i] == '\0')
			i++;
	}
	return -1;
#else
	char p[64], buf[65536];
	int fd, r;
	size_t off = 0, i;

	snprintf(p, sizeof(p), "/proc/%ld/environ", pid);
	fd = open(p, O_RDONLY);
	if (fd < 0)
		return -1;
	while (off < sizeof(buf) - 1 &&
	       (r = (int)read(fd, buf + off, sizeof(buf) - 1 - off)) > 0)
		off += (size_t)r;
	close(fd);
	buf[off] = '\0';
	for (i = 0; i < off; i += strlen(buf + i) + 1)
	{
		if (strncmp(buf + i, key, keylen) == 0 && buf[i + keylen] == '=')
		{
			snprintf(out, outsz, "%s", buf + i + keylen + 1);
			return 0;
		}
	}
	return -1;
#endif
}

static int
proc_dch_session(long pid, char *out, size_t outsz)
{
	return proc_env_value(pid, "DCH_SESSION", out, outsz);
}

/* argv of a same-uid process as one malloc'd NULL-terminated block, or NULL.
** A script runs as `<interpreter> <script> args`: when argv[1] is an
** executable "#!" file the interpreter is dropped, so a shell-launched
** `headroom wrap claude` reads back as `/…/headroom wrap claude`, which
** retypes the same, not as `python3 /…/headroom wrap claude`. */
static char **
proc_argv(long pid)
{
	char **v, *s, hb[2];
	size_t len, i, start;
	int argc, k, fd;
#ifdef __APPLE__
	char *buf = procargs(pid, &len);

	if (!buf)
		return NULL;
	memcpy(&argc, buf, sizeof argc);
	for (i = sizeof(int); i < len && buf[i]; i++)
		;	/* the exec path */
	while (i < len && !buf[i])
		i++;
#else
	static char buf[65536];
	char p[64];
	ssize_t r;

	snprintf(p, sizeof p, "/proc/%ld/cmdline", pid);
	if ((fd = open(p, O_RDONLY)) < 0)
		return NULL;
	for (len = 0; len < sizeof buf - 1 &&
	     (r = read(fd, buf + len, sizeof buf - 1 - len)) > 0; )
		len += (size_t)r;
	close(fd);
	buf[len] = '\0';
	for (argc = 0, i = 0; i < len; i++)
		argc += buf[i] == '\0';
	i = 0;
#endif
	start = i;
	for (k = 0; k < argc && i < len; k++)
		i += strlen(buf + i) + 1;
	if (argc < 1 || k < argc ||
	    !(v = malloc((size_t)(argc + 1) * sizeof *v + (i - start))))
		return NULL;
	s = memcpy(v + argc + 1, buf + start, i - start);
	for (k = 0; k < argc; k++, s += strlen(s) + 1)
		v[k] = s;
	v[argc] = NULL;
	if (argc > 1 && v[1][0] == '/' && access(v[1], X_OK) == 0 &&
	    (fd = open(v[1], O_RDONLY | O_NONBLOCK)) >= 0)
	{
		int bang = read(fd, hb, 2) == 2 && hb[0] == '#' && hb[1] == '!';

		close(fd);
		if (bang)
			memmove(v, v + 1, (size_t)argc * sizeof *v);
	}
	return v;
}

static int
proc_is_codex(long pid)
{
	char path[4096];
	const char *base;
#ifdef __APPLE__
	if (proc_pidpath((int)pid, path, sizeof(path)) <= 0)
		return 0;
#else
	char proc[64];
	ssize_t n;

	snprintf(proc, sizeof(proc), "/proc/%ld/exe", pid);
	n = readlink(proc, path, sizeof(path) - 1);
	if (n <= 0)
		return 0;
	path[n] = '\0';
#endif
	base = strrchr(path, '/');
	base = base ? base + 1 : path;
	return strcmp(base, "codex") == 0 ||
	       strcmp(base, "codex-code-mode-host") == 0;
}

/* Claude Code's live-session records: $CLAUDE_CONFIG_DIR/sessions, else
** ~/.claude/sessions — as process `pid` sees them when its env can be read
** (a Claude under its own config dir or HOME, #062), else as we do. pid 0
** = ours. */
static int
claude_sessions_dir(long pid, char *dir, size_t dirsz)
{
	static char eb[1024], hb[1024];
	const char *base = getenv("CLAUDE_CONFIG_DIR"), *home = getenv("HOME");

	if (pid > 0 && proc_env_value(pid, "CLAUDE_CONFIG_DIR", eb,
	                              sizeof eb) == 0 && *eb)
		base = eb;
	else if (pid > 0 && proc_env_value(pid, "HOME", hb, sizeof hb) == 0 &&
	         *hb)
		base = NULL, home = hb;
	if ((!base || !*base) && (!home || !*home))
		return -1;
	return snprintf(dir, dirsz, "%s%s/sessions", base && *base ? base : home,
	                base && *base ? "" : "/.claude") >= (int)dirsz ? -1 : 0;
}

/* Did the claude whose record the probe found at `rec` end cleanly? Claude
** Code removes its sessions/<pid>.json on a clean exit; a SIGKILLed one (out
** of memory) leaves it behind. The path, not the pid: the dir came from that
** claude's env (#062), gone with it. */
int
dch_claude_gone(const char *rec)
{
	return !*rec || access(rec, F_OK) != 0;
}

/* When process pid started, from dch_process_start's "%a %b %e %T %Y" in
** UTC; -1 when unknown. */
static time_t
proc_started(long pid)
{
	static const char mon[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
	char s[128], m[4];
	const char *p;
	struct tm tm;

	memset(&tm, 0, sizeof tm);
	if (dch_process_start(pid, s, sizeof s) < 0 ||
	    sscanf(s, "%*s %3s %d %d:%d:%d %d", m, &tm.tm_mday, &tm.tm_hour,
	           &tm.tm_min, &tm.tm_sec, &tm.tm_year) != 6 ||
	    strlen(m) != 3 || !(p = strstr(mon, m)) || (p - mon) % 3)
		return -1;
	tm.tm_mon = (int)(p - mon) / 3;
	tm.tm_year -= 1900;
	return timegm(&tm);
}

/* Every regular record in one sessions dir, to visit(). Claude Code
** rewrites its record in place (writeFile: truncate, then write), so a read
** in between finds it empty or changing: such a record is read again, at
** most 10 waits of 5ms per dir, so one unlucky read never drops a live
** Claude (#253). Empty, only a live <pid>.json's whose process started by
** the record's last write (2s for the clocks' rounding): one left empty by
** a Claude killed mid-write stays empty, and would cost every scan 50ms,
** also once its pid is reused (#274). One still empty or changing after
** the waits goes to visit() as "", len 0: a Claude there, id unknown. */
static int
visit_records_in(const char *dir, dch_claude_record_fn visit, void *arg)
{
	DIR *d;
	struct dirent *de;
	int waits = 0;

	if (!(d = opendir(dir)))
		return 0;
	while ((de = readdir(d)))
	{
		char path[1400], body[8192], extra, *e;
		struct stat before, after;
		size_t len, n = strlen(de->d_name);
		long rp;
		time_t t;
		int fd;
		if (n < 6 || strcmp(de->d_name + n - 5, ".json") ||
		    snprintf(path, sizeof path, "%s/%s", dir, de->d_name) >=
		        (int)sizeof path)
			continue;
	again:
		len = 0;
		memset(&before, 0, sizeof before);
		fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
		if (fd < 0 || fstat(fd, &before) < 0 || !S_ISREG(before.st_mode) ||
		    before.st_uid != getuid() || (before.st_mode & 022) ||
		    before.st_size <= 0 || before.st_size >= (off_t)sizeof body)
		{
			if (fd >= 0) close(fd);
			if (fd >= 0 && S_ISREG(before.st_mode) &&
			    before.st_uid == getuid() && !(before.st_mode & 022) &&
			    before.st_size == 0 &&
			    (rp = strtol(de->d_name, &e, 10)) > 0 &&
			    !strcmp(e, ".json") &&
			    (kill((pid_t)rp, 0) == 0 || errno == EPERM) &&
			    (t = proc_started(rp)) >= 0 && t <= before.st_mtime + 2)
			{
				if (waits++ >= 10)
					goto found;
				dch_trace("claude record %s: empty, read again", path);
				usleep(5000);
				goto again;
			}
			continue;
		}
		while (len < (size_t)before.st_size)
		{
			ssize_t r = read(fd, body + len, (size_t)before.st_size - len);
			if (r > 0) len += (size_t)r;
			else if (r < 0 && errno == EINTR) continue;
			else break;
		}
		if (len != (size_t)before.st_size || read(fd, &extra, 1) != 0 ||
		    fstat(fd, &after) < 0 || after.st_size != before.st_size ||
		    after.st_dev != before.st_dev || after.st_ino != before.st_ino ||
		    memchr(body, '\0', len))
		{
			close(fd);
			if (!memchr(body, '\0', len))
			{
				if (waits++ >= 10)
				{
					len = 0;
					goto found;
				}
				dch_trace("claude record %s: changed, read again", path);
				usleep(5000);
				goto again;
			}
			continue;
		}
		close(fd);
	found:
		/* len 0: still empty or changing after the waits. Visited, not
		** skipped: skipped, another Claude would answer for it (#274) */
		if (!len)
			dch_trace("claude record %s: unread, id unknown", path);
		body[len] = '\0';
		if (visit(path, body, len, arg) < 0)
		{
			closedir(d);
			return -1;
		}
	}
	closedir(d);
	return 0;
}

int
dch_visit_claude_records(dch_claude_record_fn visit, void *arg)
{
	char dir[1100];

	return claude_sessions_dir(0, dir, sizeof dir) < 0 ? 0
	       : visit_records_in(dir, visit, arg);
}

/* Best-effort overlay of the Claude Code session name running inside each
** dch session. The harness writes ~/.claude/sessions/<pid>.json for every
** live claude process; that process's DCH_SESSION env names the dch session
** exactly, so the join needs no cwd heuristics. Auto-titled harness names
** ("nameSource":"derived") are skipped — "dch-28" beats no one. Explicit
** dch aliases still win in the picker. */
static int
load_harness_record(const char *path, const char *body, size_t body_len,
                    void *arg)
{
	struct slist *sl = arg;
	char jb[8192], sess[600], cb[256], *nm, *q, *endp;
	const char *base = strrchr(path, '/'), *cn;
	long pid;
	int i;
	if (body_len >= sizeof jb) return 0;
	memcpy(jb, body, body_len + 1);
	base = base ? base + 1 : path;
	pid = strtol(base, &endp, 10);
	if (pid <= 0 || strcmp(endp, ".json") ||
	    strstr(jb, "\"nameSource\":\"derived\"")) return 0;
	nm = strstr(jb, "\"name\":\"");
	if (!nm || !(q = strchr(nm += 8, '"')) || q == nm) return 0;
	*q = '\0';
	for (char *c = nm; *c; c++)
		if (*c == '\t' || *c == '\n' || *c == '\r') *c = ' ';
	if (proc_dch_session(pid, sess, sizeof sess) != 0) return 0;
	cn = canon_name(sess, cb, sizeof cb);
	for (i = 0; i < sl->n; i++)
		if (!sl->harness[i] && strcmp(sl->v[i], cn) == 0)
		{
			sl->harness[i] = strdup(nm);
			break;
		}
	return 0;
}

static void
load_harness_names(struct slist *sl)
{
	if (sl->n) (void)dch_visit_claude_records(load_harness_record, sl);
}

/* Look up a Codex thread's current user-visible name. session_index.jsonl
** has no PID, so callers establish liveness separately and obtain the ID
** from the process environment or its shell snapshot. Codex does not record
** whether a title was auto-derived; only empty/malformed titles can be
** safely skipped. */
int
dch_codex_thread_name(const char *home, const char *id, char *out, size_t outsz)
{
	char path[1200], buf[65536], *line, *next;
	struct stat st;
	int fd, r, tail = 0;

	snprintf(path, sizeof(path), "%s/session_index.jsonl", home);
	fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
	if (fd < 0)
		return -1;
	if (fstat(fd, &st) == 0 && st.st_size >= (off_t)sizeof(buf) - 1)
	{
		(void)lseek(fd, -((off_t)sizeof(buf) - 1), SEEK_END);
		tail = 1;
	}
	r = (int)read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (r <= 0)
		return -1;
	buf[r] = '\0';
	out[0] = '\0';
	line = tail ? strchr(buf, '\n') : buf;
	if (tail && !line)
		return -1;
	if (tail)
		line++;
	for (; line && *line; line = next)
	{
		char *p, *q, *nm, *end;
		next = strchr(line, '\n');
		if (next)
			*next++ = '\0';
		p = strstr(line, "\"id\":\"");
		if (!p)
			continue;
		p += 6;
		q = strchr(p, '"');
		if (!q || (size_t)(q - p) != strlen(id) || strncmp(p, id, q - p))
			continue;
		nm = strstr(q, "\"thread_name\":\"");
		if (!nm)
			continue;
		nm += 15;
		end = strchr(nm, '"');
		if (!end || end == nm || strchr(nm, '\\') ||
		    (size_t)(end - nm) >= outsz)
			continue;
		memcpy(out, nm, (size_t)(end - nm));
		out[end - nm] = '\0';
	}
	return out[0] ? 0 : -1;
}

/* Every live pid, to fn until it returns nonzero; that value, else 0. */
static int
each_pid(int (*fn)(long, void *), void *arg)
{
	int r = 0;
#ifdef __APPLE__
	int pids[4096], n, i;
	n = proc_listpids(PROC_ALL_PIDS, 0, pids, sizeof(pids));
	for (i = 0; n > 0 && !r && i < n / (int)sizeof(pids[0]); i++)
		if (pids[i] > 0)
			r = fn(pids[i], arg);
#else
	DIR *d = opendir("/proc");
	struct dirent *de;
	if (!d)
		return 0;
	while (!r && (de = readdir(d)))
	{
		char *end;
		long pid = strtol(de->d_name, &end, 10);
		if (pid > 0 && *end == '\0')
			r = fn(pid, arg);
	}
	closedir(d);
#endif
	return r;
}

static long proc_ppid(long pid);

struct codex_carrier
{
	const char *sess, *marker;
};

/* A Codex binary that carries the session (and the marker, when given) but
** whose nearest dch master above it is not that session's — or that has no
** master above it at all, like the app-server daemon (ppid 1) a TUI without
** --no-daemon starts. Its shells snapshot the session's env for threads of
** whichever TUI asked, so their snapshots cannot be told from the TUI's. */
static int
codex_detached(long pid, void *arg)
{
	const struct codex_carrier *c = arg;
	char v[700];
	long up = pid;
	int d;

	if (!proc_is_codex(pid) || proc_dch_session(pid, v, sizeof v) != 0 ||
	    strcmp(v, c->sess) != 0 ||
	    (c->marker &&
	     (proc_env_value(pid, "DCH_NATIVE_BRIDGE_ID", v, sizeof v) != 0 ||
	      strcmp(v, c->marker) != 0)))
		return 0;
	for (d = 0; d < 64 && (up = proc_ppid(up)) > 1; d++)
	{
		char **a = proc_argv(up);
		int master = a && a[0] && a[1] && strcmp(a[1], "--master-of") == 0;

		free(a);
		if (master)
			return proc_dch_session(up, v, sizeof v) != 0 ||
			       strcmp(v, c->sess) != 0;
	}
	return 1;
}

/* 1 when a detached Codex (above) carries sess/marker: its snapshots would
** bind or name a thread this session's TUI may not be running, so callers
** refuse instead (#083). Sticky per marker, since the snapshots it wrote
** outlive it; one-shot callers (--ls) pass no marker or exit anyway. */
int
dch_codex_shared(const char *sess, const char *marker)
{
	static char seen[700];
	struct codex_carrier c = { sess, marker };

	if (marker && strcmp(seen, marker) == 0)
		return 1;
	if (!each_pid(codex_detached, &c))
		return 0;
	if (marker)
		snprintf(seen, sizeof seen, "%s", marker);
	return 1;
}

/* Current Codex releases no longer export the thread ID to the long-lived
** process. Its shell snapshot still joins that ID (the filename prefix) to
** the inherited DCH_SESSION. Prefer the newest exact match. None at all
** while a detached Codex (the shared app-server daemon) carries the same
** session: then a match may be another TUI's thread. */
int
dch_codex_snapshot_id(const char *home, const char *sess, const char *marker,
                      char *out, size_t outsz, int strict)
{
	char dir[1200], want[700], mark[700];
	struct dirent *de;
	struct stat best = {0};
	DIR *d;
	int ambiguous = 0;
	char strict_id[128] = "";

	if (snprintf(dir, sizeof(dir), "%s/shell_snapshots", home) >=
	    (int)sizeof(dir))
		return -1;
	snprintf(want, sizeof(want), "\nexport DCH_SESSION=%s\n", sess);
	if (marker)
		snprintf(mark, sizeof(mark), "\nexport %s=%s\n",
		         "DCH_NATIVE_BRIDGE_ID", marker);
	d = opendir(dir);
	if (!d)
		return -1;
	while ((de = readdir(d)))
	{
		char path[1500], buf[262144], *dot;
		struct stat st;
		int fd, r;
		size_t n = strlen(de->d_name);

		dot = strchr(de->d_name, '.');
		if (n < 4 || !dot || (size_t)(dot - de->d_name) >= outsz ||
		    strcmp(de->d_name + n - 3, ".sh") != 0)
			continue;
		if (strict)
		{
			int valid = (size_t)(dot - de->d_name) == 36;
			for (int j = 0; valid && j < 36; j++)
				if (j == 8 || j == 13 || j == 18 || j == 23)
					valid = de->d_name[j] == '-';
				else
					valid = isxdigit((unsigned char)de->d_name[j]);
			if (!valid)
				continue;
		}
		snprintf(path, sizeof(path), "%s/%s", dir, de->d_name);
		fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
		if (fd < 0 || fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) ||
		    st.st_uid != getuid() || (strict && (st.st_mode & 022)) ||
		    st.st_size <= 0 || st.st_size >= (off_t)sizeof(buf))
		{
			if (fd >= 0) close(fd);
			continue;
		}
		if (strict)
		{
			struct stat after;
			char extra;
			size_t off = 0;
			while (off < (size_t)st.st_size)
			{
				ssize_t got = read(fd, buf + off, (size_t)st.st_size - off);
				if (got > 0) off += (size_t)got;
				else if (got < 0 && errno == EINTR) continue;
				else break;
			}
			r = (int)off;
			if (off != (size_t)st.st_size || read(fd, &extra, 1) != 0 ||
			    fstat(fd, &after) < 0 || after.st_size != st.st_size ||
			    after.st_dev != st.st_dev || after.st_ino != st.st_ino ||
			    memchr(buf, '\0', off)) r = -1;
		}
		else
			r = (int)read(fd, buf, sizeof(buf) - 1);
		close(fd);
		if (r <= 0)
			continue;
		buf[r] = '\0';
		if (!strstr(buf, want) || (marker && !strstr(buf, mark)))
			continue;
		if (strict)
		{
			size_t idlen = (size_t)(dot - de->d_name);
			if (!strict_id[0])
			{
				memcpy(strict_id, de->d_name, idlen);
				strict_id[idlen] = '\0';
				memcpy(out, strict_id, idlen + 1);
				best = st;
			}
			else if (strlen(strict_id) != idlen ||
			         strncmp(strict_id, de->d_name, idlen) != 0)
				ambiguous = 1;
			continue;
		}
		if (best.st_mtime)
		{
#ifdef __APPLE__
			if (st.st_mtimespec.tv_sec < best.st_mtimespec.tv_sec ||
			    (st.st_mtimespec.tv_sec == best.st_mtimespec.tv_sec &&
			     st.st_mtimespec.tv_nsec < best.st_mtimespec.tv_nsec))
				continue;
			ambiguous = st.st_mtimespec.tv_sec == best.st_mtimespec.tv_sec &&
			            st.st_mtimespec.tv_nsec == best.st_mtimespec.tv_nsec;
#else
			if (st.st_mtim.tv_sec < best.st_mtim.tv_sec ||
			    (st.st_mtim.tv_sec == best.st_mtim.tv_sec &&
			     st.st_mtim.tv_nsec < best.st_mtim.tv_nsec))
				continue;
			ambiguous = st.st_mtim.tv_sec == best.st_mtim.tv_sec &&
			            st.st_mtim.tv_nsec == best.st_mtim.tv_nsec;
#endif
		}
		else
			ambiguous = 0;
		memcpy(out, de->d_name, (size_t)(dot - de->d_name));
		out[dot - de->d_name] = '\0';
		best = st;
	}
	closedir(d);
	return best.st_mtime && !(strict && ambiguous) &&
	       !dch_codex_shared(sess, marker) ? 0 : -1;
}

static int
load_codex_pid(long pid, void *arg)
{
	struct slist *sl = arg;
	char id[128], sess[600], title[600], cb[256], codexdir[1200], mk[700];
	const char *cn, *ch, *home = getenv("HOME"), *marker = NULL;
	int i, target = -1;

	if (!proc_is_codex(pid) ||
	    proc_dch_session(pid, sess, sizeof(sess)) != 0)
		return 0;
	cn = canon_name(sess, cb, sizeof(cb));
	for (i = 0; i < sl->n; i++)
		if (!sl->harness[i] && strcmp(sl->v[i], cn) == 0)
		{
			target = i;
			break;
		}
	if ((ch = getenv("CODEX_HOME")) && *ch)
		snprintf(codexdir, sizeof codexdir, "%s", ch);
	else
		snprintf(codexdir, sizeof codexdir, "%s/.codex", home);
	/* a bridged TUI's own snapshots also carry its bridge marker */
	if (proc_env_value(pid, "DCH_NATIVE_BRIDGE_ID", mk, sizeof mk) == 0 &&
	    mk[0])
		marker = mk;
	if (target < 0 ||
	    (proc_env_value(pid, "CODEX_THREAD_ID", id, sizeof(id)) != 0 &&
	     proc_env_value(pid, "CODEX_SESSION_ID", id, sizeof(id)) != 0 &&
	     dch_codex_snapshot_id(codexdir, sess, marker, id, sizeof(id), 0) !=
	         0) ||
	    dch_codex_thread_name(codexdir, id, title, sizeof(title)) != 0)
		return 0;
	for (char *c = title; *c; c++)
		if (*c == '\t' || *c == '\n' || *c == '\r')
			*c = ' ';
	sl->harness[target] = strdup(title);
	return 0;
}

/* A genuine live Codex process supplies DCH_SESSION; its environment or
** matching shell snapshot supplies the thread ID. The title index alone is
** never a liveness signal. */
static void
load_codex_names(struct slist *sl)
{
	const char *home = getenv("HOME");

	if (sl->n && home && home[0])
		(void)each_pid(load_codex_pid, sl);
}

/* A session listed under exactly this name: on a case-insensitive file
** system (macOS) its socket path answers to every case variant too. */
static int
name_listed(const char *name)
{
	struct slist sl = {0};
	int k, hit = 0;

	(void)list_sessions(&sl);
	for (k = 0; k < sl.n && !hit; k++)
		hit = strcmp(sl.v[k], name) == 0;
	slist_free(&sl);
	return hit;
}

/* INFO-2 (#120): no new session or rename takes a name shaped like a live
** shell's session_id ("m" + 16 hex, in any case: QA 1.45.1 LOW-1), so
** `--xray <id> --clear` never meets a name that shadows an id. 1, the line
** printed, when refused. #162: nor a leading '-', which every verb would
** read as a flag. */
static int
refuse_id_name(const char *name)
{
	if (name[0] == '-')
	{
		fputs("dch: a session name cannot start with '-'\n", stderr);
		return 1;
	}
	if (!xr_label_id_shaped(name, strlen(name)))	/* the listing trims */
		return 0;
	fputs("dch: name looks like a session_id\n", stderr);
	return 1;
}

/* #203: a name the caller gives a new session (--spawn, --revive --as, -n) is
** one the store can hold: no '/' (a socket outside the socket dir), no
** control, at most NAME_CAP bytes. A leading '-' is refuse_id_name's. 1,
** the line printed, when refused. */
static int
refuse_path_name(const char *name)
{
	if (name[0] == '-' || name_ok(name))
		return 0;
	fputs("dch: bad session name (no '/' or control characters, at most "
	      "255 bytes)\n", stderr);
	return 1;
}

/* Write (or clear, when alias is empty) the sidecar for `name`. At most
** the 599 bytes the master reads into its record, cut on a character
** boundary (callers cut by bytes). */
static int
write_alias(const char *name, const char *alias)
{
	char ap[1200];
	size_t n = alias ? strlen(alias) : 0;

	n = dch_utf8_trim(alias, n > 599 ? 599 : n);
	if (n && xr_label_id_shaped(alias, n))
		return -2;
	if (alias_path(name, ap, sizeof(ap)) < 0)
		return -1;
	if (!n)
	{
		unlink(ap);
		return 0;
	}
	int fd = open(ap, O_WRONLY | O_CREAT | O_TRUNC, 0600);
	if (fd < 0)
		return -1;
	(void)!write(fd, alias, n);
	(void)!write(fd, "\n", 1);
	close(fd);
	return 0;
}

/* ---- static TUI picker --------------------------------------------- */
/*
** Self-contained arrow-key picker over /dev/tty. No external deps. Keys:
**   up/down or k/j        move
**   enter                 select
**   q / esc / Ctrl-C      cancel
*/

static struct termios picker_saved_term;
static int picker_tty_fd = -1;
static int picker_term_saved;
static int picker_alt;	/* the picker (#075) is on the alternate screen */

static void
picker_restore(void)
{
	if (picker_tty_fd >= 0)
	{
		if (picker_term_saved)
			tcsetattr(picker_tty_fd, TCSANOW, &picker_saved_term);
		/* Cursor on + disable mouse/bracketed-paste/alt-screen we may
		** have inherited. dch itself never enables them but a previous
		** in-shell app could've. Safe to send unconditionally. Autowrap
		** back on (the picker turns it off); the alternate screen left
		** only when the picker entered it: leaving restores the saved
		** cursor, which would jump -rl's inline prompt. */
		(void)!write(picker_tty_fd,
		    "\033[?25h"
		    "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"
		    "\033[?2004l\033[?7h",
		    6 + 5 * 8 + 8 + 5);
		/* the kitty flags pushed on the alternate screen popped there:
		** Ghostty keeps a stack per screen (#132) */
		if (picker_alt)
			(void)!write(picker_tty_fd, "\033[<u\033[0m\033[?1049l", 16);
		picker_alt = 0;
		close(picker_tty_fd);
		picker_tty_fd = -1;
	}
}

static void
picker_sigint(int sig)
{
	(void)sig;
	xv_kill();	/* the X-ray view's children (#175) end with it */
	picker_restore();
	_exit(130);
}

/* Write fixed string; no errno fuss. */
static void
tw(int fd, const char *s)
{
	(void)!write(fd, s, strlen(s));
}

/* Erase the (header + N entries) block we drew at the bottom of the screen.
** Caller has previously written exactly `lines` lines ending with \r\n; cursor
** is on the line *after* the last entry. */
static void
picker_erase(int fd, int lines)
{
	char up[32];
	int n = snprintf(up, sizeof(up), "\033[%dA\r", lines);
	(void)!write(fd, up, n);
	for (int i = 0; i < lines; i++)
		tw(fd, "\033[2K\n");
	n = snprintf(up, sizeof(up), "\033[%dA\r", lines);
	(void)!write(fd, up, n);
}

static void
session_display_name(struct slist *sl, int i, char *out, size_t outsz)
{
	const char *dn = sl->alias[i] ? sl->alias[i] : sl->harness[i];

	if (dn)
		snprintf(out, outsz, "%s (%s)", dn, sl->v[i]);
	else
		snprintf(out, outsz, "%s", sl->v[i]);
}

/* Visible window: [top, top+win). Caller maintains top so sel stays in view. */
static void
picker_draw(int fd, const char *header, struct slist *sl, int sel,
            int top, int win)
{
	dprintf(fd, "\033[2K\033[1m%s\033[0m\r\n", header);
	int end = top + win;
	if (end > sl->n)
		end = sl->n;
	for (int i = top; i < end; i++)
	{
		char row[1300];
		session_display_name(sl, i, row, sizeof(row));
		if (i == sel)
			dprintf(fd, "\033[2K\033[7m> %s\033[0m\r\n", row);
		else
			dprintf(fd, "\033[2K  %s\r\n", row);
	}
}

static int
tty_rows(int fd)
{
	struct winsize ws;
	if (ioctl(fd, TIOCGWINSZ, &ws) == 0 && ws.ws_row > 0)
		return ws.ws_row;
	return 24;
}

/* The interactive picker (#075), defined after the history code it reads.
** 0 with the chosen session in out; 1 when none, no tty or cancelled. */
static int pick_session(const char *header, char *out, size_t outsz,
                        int opens);
static void pk_ended(const char *name, long since);

/* ---- interactive rename (alias sidecar) ---------------------------- */

/* Put the tty into the same raw mode the picker uses + hide cursor. */
static void
picker_raw_on(int fd)
{
	struct termios raw = picker_saved_term;
	raw.c_lflag &= ~(ICANON | ECHO | ISIG | IEXTEN);
	raw.c_iflag &= ~(IXON | ICRNL | INLCR | IGNCR);
	raw.c_cc[VMIN] = 1;
	raw.c_cc[VTIME] = 0;
	tcsetattr(fd, TCSANOW, &raw);
	tw(fd, "\033[?25l"
	       "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"
	       "\033[?2004l");
	if (picker_alt)	/* back from prompt_line: Shift+Enter told apart */
		tw(fd, "\033[>1u");
}

/* Read one cooked line from the tty (echo on). Returns 0 with the trimmed
** line in `out` (possibly empty), -1 on EOF/error. Caller is responsible
** for re-entering raw mode afterwards. */
static int
prompt_line(int fd, const char *prompt, char *out, size_t outsz)
{
	tcsetattr(fd, TCSANOW, &picker_saved_term); /* cooked + echo */
	if (picker_alt)	/* the kitty flags off: ^D, ^U as the line editor's */
		tw(fd, "\033[<u");
	tw(fd, "\033[?25h\033[2K\r");
	tw(fd, prompt);
	ssize_t r;
	do {
		r = read(fd, out, outsz - 1);
	} while (r < 0 && errno == EINTR);
	if (r <= 0)
	{
		out[0] = '\0';
		return -1;
	}
	out[r] = '\0';
	chomp(out);
	return 0;
}

/* Interactive rename: pick a session, type a new alias, repeat until quit.
** Empty input clears the alias (reverts to the real name). */
static int
rename_sessions_interactive(void)
{
	struct slist sl = {0};
	if (list_sessions(&sl) < 0 || sl.n == 0)
	{
		fprintf(stderr, "no sessions\n");
		slist_free(&sl);
		return 0;
	}
	load_aliases(&sl);
	slist_sort(&sl);

	int fd = open("/dev/tty", O_RDWR);
	if (fd < 0)
	{
		fprintf(stderr, "dch: rename needs a terminal\n");
		slist_free(&sl);
		return 1;
	}
	if (tcgetattr(fd, &picker_saved_term) < 0)
	{
		close(fd);
		slist_free(&sl);
		return 1;
	}
	picker_tty_fd = fd;
	picker_term_saved = 1;
	signal(SIGINT, picker_sigint);
	signal(SIGTERM, picker_sigint);
	signal(SIGHUP, picker_sigint);
	picker_raw_on(fd);

	const char *header = "rename session (enter=edit, q=quit):";
	int sel = 0, top = 0, drawn = 0, prev_lines = 0;

	for (;;)
	{
		int rows = tty_rows(fd);
		int win = rows - 2;
		if (win < 1)
			win = 1;
		if (win > sl.n)
			win = sl.n;
		int lines = 1 + win;

		if (sel >= sl.n)
			sel = sl.n - 1;
		if (sel < top)
			top = sel;
		else if (sel >= top + win)
			top = sel - win + 1;

		if (drawn)
			picker_erase(fd, prev_lines);
		picker_draw(fd, header, &sl, sel, top, win);
		drawn = 1;
		prev_lines = lines;

		unsigned char c;
		ssize_t r;
		do {
			r = read(fd, &c, 1);
		} while (r < 0 && errno == EINTR);
		if (r <= 0)
			break;

		if (c == 'q' || c == 3 /*^C*/ || c == 4 /*^D*/)
			break;
		if (c == 'j')
		{
			if (sel < sl.n - 1)
				sel++;
			continue;
		}
		if (c == 'k')
		{
			if (sel > 0)
				sel--;
			continue;
		}
		if (c == 27) /* ESC / arrows */
		{
			fd_set rs;
			struct timeval tv = {0, 50 * 1000};
			FD_ZERO(&rs);
			FD_SET(fd, &rs);
			if (select(fd + 1, &rs, NULL, NULL, &tv) <= 0)
				break;
			unsigned char b1, b2;
			if (read(fd, &b1, 1) != 1 || b1 != '[')
				break;
			if (read(fd, &b2, 1) != 1)
				break;
			if (b2 == 'A' && sel > 0)
				sel--;
			else if (b2 == 'B' && sel < sl.n - 1)
				sel++;
			continue;
		}
		if (c == '\r' || c == '\n')
		{
			char prompt[700], in[600];
			int refused = 0;

			picker_erase(fd, prev_lines);
			snprintf(prompt, sizeof(prompt),
			         "new name for '%s' (empty=clear): ", sl.v[sel]);
			if (prompt_line(fd, prompt, in, sizeof(in)) == 0)
			{
				sanitize(in);
				refused = write_alias(sl.v[sel], in) == -2;
				if (!refused)
				{
					free(sl.alias[sel]);
					sl.alias[sel] = in[0] ? strdup(in) : NULL;
				}
			}
			picker_raw_on(fd);
			tw(fd, refused ? "\033[2K\rdch: name looks like a "
			   "session_id\r\n" : "\033[2K\r");
			drawn = 0; /* redraw fresh, nothing to erase */
			continue;
		}
	}

	if (drawn)
		picker_erase(fd, prev_lines);
	picker_restore();
	signal(SIGINT, SIG_DFL);
	signal(SIGTERM, SIG_DFL);
	signal(SIGHUP, SIG_DFL);
	slist_free(&sl);
	return 0;
}

/* ---- session kill --------------------------------------------------- */

/*
** Kill = SIGTERM the session's master + unlink socket file. The master runs
** as `<exe> --master-of <sock> -- <cmd...>`; a pid is signalled only when
** its argv[1..2] are exactly that, whatever the binary is called (#027) and
** whatever characters the name holds (#060) — or that socket by another
** spelling of its dir (/tmp vs /private/tmp, a symlink: #067), compared by
** realpath when argv[2] is absolute.
*/

static int
is_master_of(long pid, const char *sp)
{
	char **v = pid > 1 ? proc_argv(pid) : NULL, ra[PATH_MAX], rb[PATH_MAX];
	int ok = v && v[0] && v[1] && v[2] &&
	         strcmp(v[1], "--master-of") == 0 &&
	         (strcmp(v[2], sp) == 0 ||
	          (v[2][0] == '/' && realpath(v[2], ra) && realpath(sp, rb) &&
	           strcmp(ra, rb) == 0));

	free(v);
	return ok;
}

/* The pid listening on sp, as the kernel recorded it, or -1. */
static long
sock_peer_pid(const char *sp)
{
	int s = control_connect_path(sp);
	long pid = -1;
#if defined(LOCAL_PEERPID)
	pid_t p;
	socklen_t l = sizeof p;

	if (s >= 0 && getsockopt(s, SOL_LOCAL, LOCAL_PEERPID, &p, &l) == 0)
		pid = p;
#elif defined(SO_PEERCRED)
	/* struct ucred needs _GNU_SOURCE; its ABI is { pid, uid, gid }. */
	unsigned int cr[3];
	socklen_t l = sizeof cr;

	if (s >= 0 && getsockopt(s, SOL_SOCKET, SO_PEERCRED, cr, &l) == 0)
		pid = (long)cr[0];
#endif
	if (s >= 0)
		close(s);
	return pid;
}

/* Every process whose argv says it is the master of sp gets sig (0: none);
** how many there are. A zombie's argv is unreadable: it is not counted. */
static int
masters_of(const char *sp, int sig)
{
	int k = 0;
#ifdef __APPLE__
	int pids[4096], n, i;

	n = proc_listpids(PROC_ALL_PIDS, 0, pids, sizeof(pids));
	for (i = 0; i < n / (int)sizeof(pids[0]); i++)
		if (is_master_of(pids[i], sp))
		{
			k++;
			if (sig)
				kill(pids[i], sig);
		}
#else
	DIR *d = opendir("/proc");
	struct dirent *de;
	char *end;
	long pid;

	while (d && (de = readdir(d)))
		if ((pid = strtol(de->d_name, &end, 10)) > 0 && !*end &&
		    is_master_of(pid, sp))
		{
			k++;
			if (sig)
				kill((pid_t)pid, sig);
		}
	if (d)
		closedir(d);
#endif
	return k;
}

/* SIGTERM the master of sp: the socket's listener, or, when that is not one
** (dead socket, unreachable path, a stranger on it), every process whose
** argv says it is. */
static void
term_master(const char *sp)
{
	long pid = sock_peer_pid(sp);

	if (is_master_of(pid, sp))
	{
		kill((pid_t)pid, SIGTERM);
		return;
	}
	(void)masters_of(sp, SIGTERM);
}

/* mark: ".killed" (-k) or ".stopped" (--stop, which wrote it already). */
static int
end_session(const char *name, const char *mark)
{
	char sp[1100];
	session_sock_path(name, sp, sizeof(sp));
	struct stat st;
	if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
	{
		fprintf(stderr, "no session: %s\n", name);
		return 1;
	}

	/* Mark the end as deliberate before the master hears of it: its
	** history record then ends "killed" or "stopped", which --revive
	** --all skips. */
	char pat[1200];
	int kfd;
	snprintf(pat, sizeof(pat), "%s%s", sp, mark);
	if ((kfd = open(pat, O_WRONLY | O_CREAT | O_NOFOLLOW, 0600)) >= 0)
		close(kfd);

	term_master(sp);

	/* Term any clients still attached. */
	DIR *d = opendir(sock_dir);
	if (d)
	{
		char prefix[600];
		snprintf(prefix, sizeof(prefix), "%s.sock.client.", name);
		size_t plen = strlen(prefix);
		struct dirent *de;
		while ((de = readdir(d)))
		{
			if (strncmp(de->d_name, prefix, plen) != 0)
				continue;
			int pid = atoi(de->d_name + plen);
			if (pid > 0)
				kill(pid, SIGTERM);
			char path[2200];
			snprintf(path, sizeof(path), "%s/%s",
			         sock_dir, de->d_name);
			unlink(path);
		}
		closedir(d);
	}

	unlink(sp);
	char ap[1200];
	if (alias_path(name, ap, sizeof(ap)) == 0)
		unlink(ap);
	/* Drop the activity + reported-state sidecars too (the master unlinks
	** them on a clean exit, but a SIGKILLed master never runs atexit). */
	char side[1300];
	if (snprintf(side, sizeof side, "%s/%s.sock.act", sock_dir, name) > 0)
		unlink(side);
	if (state_file_path(name, side, sizeof(side)) == 0)
		unlink(side);
	if (meta_file_path(name, side, sizeof(side)) == 0)
		unlink(side);
	return 0;
}

static int
kill_session(const char *name)
{
	if (end_session(name, ".killed") != 0)
		return 1;
	printf("killed: %s\n", name);
	return 0;
}

static int
kill_all(void)
{
	struct slist sl = {0};
	int i;
	if (list_sessions(&sl) < 0 || sl.n == 0)
	{
		printf("no sessions\n");
		slist_free(&sl);
		return 0;
	}
	for (i = 0; i < sl.n; i++)
	{
		/* Skip entries that vanished between list and kill so we
		** don't spam "no session: X" on a race. */
		char sp[1100];
		snprintf(sp, sizeof(sp), "%s/%s.sock", sock_dir, sl.v[i]);
		struct stat st;
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
			continue;
		kill_session(sl.v[i]);
	}
	slist_free(&sl);
	return 0;
}

/* ---- usage ---------------------------------------------------------- */

static void
usage(void)
{
	printf(
	    "dch " DCH_VERSION " — detachable terminal session\n"
	    "Usage:\n"
	    "  dch              attach free session, else spawn new\n"
	    "  dch <cmd...>     attach-or-create, run cmd\n"
	    "  dch -l           pick a session and attach (mirror)\n"
	    "  dch -k [name]    kill session; without name, pick interactively\n"
	    "  dch -kl          kill ALL sessions\n"
	    "  dch -ls, --ls    list local sessions (one per line)\n"
	    "  dch -lj          list sessions as name<TAB>alias<TAB>activity_epoch per line\n"
	    "  dch -a <name>    attach to a session, never create one;\n"
	    "                   host:name attaches to one on a remote host\n"
	    "  dch --host <h> -a <name>  attach to <name> on host h\n"
	    "  dch <verb> host:name ...  a control verb on a remote host (its\n"
	    "                   dch serve 1.41+): --read --send --run --keys\n"
	    "                   --follow --status -k -m -d --spawn --history\n"
	    "                   --revive --pin --unpin --forget --stop --say;\n"
	    "                   --host <h> also works (--history needs it);\n"
	    "                   exits 6 if the host lacks it, 7 if unreachable\n"
	    "  dch --cwd <dir> --spawn ...  the spawned session's start dir\n"
	    "  dch --scrollback <n>  with an attach: the last n lines of the\n"
	    "                   session's scrollback first (DCH_REPLAY_SCROLLBACK)\n"
	    "  dch --hosts <h1,h2,...>   the remote hosts, [label=]host[:port],\n"
	    "                   instead of your online tailnet peers\n"
	    "  dch --remote     with -ls, -lj, --ls-json, --history: remote rows\n"
	    "                   too, as host:name (the picker always shows them)\n"
	    "  dch -rl          interactively rename (alias) sessions\n"
	    "  dch -m <name> [alias]  set alias non-interactively (empty clears)\n"
	    "  dch -d [name]    detach all clients of session (sends SIGUSR1)\n"
	    "  dch -n <name>    override auto-name\n"
	    "  dch -f           force attach even if busy (mirror)\n"
	    "  dch -E           disable detach escape (Ctrl-\\); use -d instead\n"
	    "  dch -e <c>       set detach character (default ^\\)\n"
	    "  dch -h           this help\n"
	    "  dch -V           print version\n"
	    "  dch --build      print the build: full, or lite (no terminal mirror)\n"
	    "Agent/control verbs (no attach; safe to script):\n"
	    "  dch --spawn <name> [--size CxR] [--env K=V]... [cmd...]\n"
	    "                                             start headless session\n"
	    "  dch --send <name> <text...>                type text into session\n"
	    "  dch --run <name> <text...>                 type text, press enter\n"
	    "  dch --keys <name> <key...>                 send keys (ctrl+c, up, f2, ...)\n"
	    "  dch --read <name> [--ansi] [--recent [N]]  print session screen\n"
	    "  dch --read <name> --cursor                 screen on stdout, plus\n"
	    "                                             `cursor <row> <col>\n"
	    "                                             <visible> <wrap>` (1-based)\n"
	    "                                             on stderr\n"
	    "  dch --follow <name>                        stream the screen on\n"
	    "                                             change (read-only):\n"
	    "                                             `dch-frame <len>\n"
	    "                                             <rows>x<cols> <crow>\n"
	    "                                             <ccol> <cvis>` + <len>\n"
	    "                                             bytes of --read --ansi\n"
	    "  dch --restart <name>|--all [-f]            re-exec the session's\n"
	    "                                             master onto this dch\n"
	    "                                             binary, in place; the\n"
	    "                                             program keeps running.\n"
	    "                                             --all skips sessions\n"
	    "                                             already current (-f\n"
	    "                                             restarts them anyway)\n"
	    "  dch --restart <name>|--all --include-agents [-f] [--json]\n"
	    "                                             then also restart stale\n"
	    "                                             Claude Code agents (-f:\n"
	    "                                             current ones too) run\n"
	    "                                             as the command, not\n"
	    "                                             typed into the shell;\n"
	    "                                             off by default, --all\n"
	    "                                             too; --json: NDJSON\n"
	    "  dch --respawn <name> [-- cmd...]           when the program exits,\n"
	    "                                             keep the session and\n"
	    "                                             start cmd in its place;\n"
	    "                                             no cmd = hold childless\n"
	    "                                             until a second --respawn\n"
	    "                                             brings one (one-shot)\n"
	    "  dch --respawn <name> --cancel              drop a pending respawn\n"
	    "  dch --cmd <name>                           JSON: child pid, state,\n"
	    "                                             argv, last exit status,\n"
	    "                                             output since the arm\n"
	    "  dch --wait <name> --match <str> [--timeout ms]  wait for output\n"
	    "  dch --wait <name> --state <s> [--timeout ms]    wait for state\n"
	    "  dch --status <name>                        print session state:\n"
	    "                                             working|idle|blocked|done\n"
	    "                                             (reported, else detected\n"
	    "                                             from screen, else output\n"
	    "                                             heuristic)\n"
	    "  dch --report <name> <state>                set state (optional hooks;\n"
	    "                                             `clear` reverts to auto)\n"
	    "  dch --meta <name> '<json object>'          attach metadata (status\n"
	    "                                             lines, hooks); `clear`\n"
	    "                                             drops it; shown by\n"
	    "                                             --ls-json as \"meta\"\n"
	    "  dch --history [--json]                     sessions that ended:\n"
	    "                                             name, when, how, cwd,\n"
	    "                                             command, claude session,\n"
	    "                                             pinned\n"
	    "  dch --pin|--unpin <name>... [--json]       keep a session's record\n"
	    "                                             past the 30-day prune\n"
	    "                                             (the pin follows the\n"
	    "                                             name), or stop keeping it\n"
	    "  dch --forget <name>... [-f] [--json]       delete ended sessions'\n"
	    "                                             records; never a live\n"
	    "                                             one, a pinned one only\n"
	    "                                             with -f\n"
	    "  dch --forget --all [--json]                delete every record\n"
	    "                                             neither live nor pinned\n"
	    "  dch --revive <name> [--as <new>] [--json]  start an ended session\n"
	    "                                             again: its cwd, its\n"
	    "                                             command, claude resumed\n"
	    "                                             and told to carry on\n"
	    "                                             (--no-message: resumed\n"
	    "                                             only, nothing typed)\n"
	    "  dch --revive --all [--json]                every session that died\n"
	    "                                             (not exit 0, -k or\n"
	    "                                             --stop)\n"
	    "                                             in the last 3 days\n"
	    "                                             after 5+ minutes;\n"
	    "                                             others by name\n"
	    "  dch --say <name>...|--all [--json] -- <text...>\n"
	    "                                             type a message into\n"
	    "                                             each session's Claude\n"
	    "                                             Code at an empty\n"
	    "                                             composer (busy: it is\n"
	    "                                             queued); one result\n"
	    "                                             per session: sent,\n"
	    "                                             skipped (prompt,\n"
	    "                                             draft, ...) or failed\n"
	    "  dch --stop <name>...|--all [--json]        pause each session's\n"
	    "                                             Claude Code, /exit it\n"
	    "                                             (as the command; typed\n"
	    "                                             into the shell it goes\n"
	    "                                             with the session), keep\n"
	    "                                             its resume id and\n"
	    "                                             end the session as\n"
	    "                                             \"stopped\"; dch --revive\n"
	    "                                             <name> wakes it resumed\n"
	    "  dch --ls-json    like -lj but JSON, adds \"state\"; --remote\n"
	    "                   adds remote rows, each with a \"host\";\n"
	    "                   --follow: again, one line, on each change\n"
	    "  dch --config [<key> [<value>|--unset]]     show the config file\n"
	    "                                             (path, then each key),\n"
	    "                                             one key, or set/remove\n"
	    "                                             it. Keys: auto_allow\n"
	    "                                             (the master answers\n"
	    "                                             Claude Code's plain\n"
	    "                                             permission prompts\n"
	    "                                             with Yes; not in lite),\n"
	    "                                             sidekick_pattern,\n"
	    "                                             picker_sort\n"
	    "                                             (activity|name),\n"
	    "                                             detach_key\n"
	    "                                             (detach|switch: what\n"
	    "                                             one Ctrl-\\ does),\n"
	    "                                             remote_hosts (off,\n"
	    "                                             or a --hosts list),\n"
	    "                                             xray_ring (shell output\n"
	    "                                             kept for X-ray, 0 to\n"
	    "                                             16M, default 2M); the\n"
	    "                                             picker's , key edits\n"
	    "                                             them too\n"
	    "  dch --shell-init zsh|bash|fish             print OSC 133 prompt\n"
	    "                                             hooks for your rc file\n"
	    "                                             (X-ray's command marks)\n"
	    "  dch --xray <n>                             the X-ray view: chapters,\n"
	    "                                             refs, on-screen hints\n"
	    "  dch --xray <n> [--clear] --json            a session's chapters as\n"
	    "                                             one JSON object; --clear\n"
	    "                                             empties a shell's ring\n"
	    "  dch --xray <n> --follow --json             that object, then its\n"
	    "                                             deltas as NDJSON\n"
	    "  dch --xray <n> --ref <ref> [--scope session|project|all] [--full]\n"
	    "       [--budget-ms <ms>] --json             one ref's mentions and\n"
	    "                                             definitions as NDJSON\n"
	    "  dch --xray <n> --search <text> --json      the newest 50 places a\n"
	    "                                             word or phrase was said\n"
	    "  dch --xray <n> --chapter <anchor> --json   one chapter's entries\n"
	    "                                             as one JSON object;\n"
	    "                                             <n> as host:name runs\n"
	    "                                             on that host's dch serve\n"
	    "  dch --xray host:name --requests --json     one warm connection: X-ray\n"
	    "                                             requests as NDJSON on\n"
	    "                                             stdin, answers on stdout\n"
	    "  dch --agent-list [--json]                 list native Claude peers\n"
	    "  dch --agent-send <name> <message...>       message one native peer\n"
	    "  dch serve [--allow-create] [--allow-user <u>]...  serve sessions to\n"
	    "                                             the Prosper iPhone app\n"
	    "                                             over Tailscale (port\n"
	    "                                             8771; dch serve -h)\n"
	    "Env: DCH_NO_DETECT=1 disables screen-content state detection.\n"
	    "     DCH_CONFIG names the config file (else\n"
	    "     $XDG_CONFIG_HOME/dch/config, else ~/.config/dch/config).\n"
	    "     DCH_NO_HISTORY=1 at session start keeps no history record.\n"
	    "     DCH_NO_REMOTE=1 lists no remote hosts (but --hosts).\n"
	    "     DCH_NO_*: empty or 0 is off.\n"
	    "     DCH_DOUBLE_TAP_MS sets the switch double-press window\n"
	    "     (default 300; 0 disables it, one press acts at once).\n"
	    "Detach: Ctrl-\\  (or `dch -d` if the host swallows it).\n"
	    "Switch: Ctrl-\\ twice quickly — detach and pick another session.\n"
	    "        detach_key = switch swaps the two (one press: the picker).\n"
	    "Nesting refused inside an existing dch session.\n");
}

/* ---- subcommand: server-only sentinel ------------------------------- */
/*
** When we daemonize the master, we re-exec dch with `--master-of <sock>
** -- <cmd...>` so end_session can tell the master by its argv.
*/

static int
run_master_of(int argc, char **argv)
{
	if (argc < 1)
		return 1;
	sockname = argv[0];
	argc--; argv++;
	/* Optional "--" separator from caller's argv. */
	if (argc >= 1 && strcmp(argv[0], "--") == 0)
	{
		argc--; argv++;
	}
	if (argc < 1)
		return 1;
	if (tcgetattr(0, &orig_term) < 0)
	{
		memset(&orig_term, 0, sizeof(struct termios));
		dont_have_tty = 1;
	}
	/* Disable the default detach char; clients set their own. */
	return master_main(argv, 1, 1); /* waitattach=1, dontfork=1 */
}

/* ---- attach action -------------------------------------------------- */

/* Build a daemon-launching command vector. We fork+setsid here so the
** master survives the originating terminal closing. Returns 0 on parent
** success. */
static long long
wall_ms(void)
{
	struct timeval tv;

	gettimeofday(&tv, NULL);
	return (long long)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

static void strip_caller_agent(void);

static int
spawn_master(char *exe, const char *name, char *sockp,
             char **inner_argv)
{
	char msg[1024];
	size_t n = 0;
	ssize_t got;
	int fd[2];
	pid_t pid;

	if (pipe(fd) < 0)
	{
		fprintf(stderr, "dch: pipe: %s\n", strerror(errno));
		return -1;
	}
	pid = fork();
	if (pid < 0)
	{
		fprintf(stderr, "dch: fork: %s\n", strerror(errno));
		close(fd[0]);
		close(fd[1]);
		return -1;
	}
	if (pid == 0)
	{
		/* Child: become session leader, drop tty, exec sentinel. Its
		** stdout is the status pipe: the master holds it until the
		** program is exec'd, and prints there whatever stops it. */
		setsid();
		close(fd[0]);
		dup2(fd[1], 1);
		if (fd[1] > 2)
			close(fd[1]);
		int nullfd = open("/dev/null", O_RDWR);
		if (nullfd >= 0)
		{
			dup2(nullfd, 0);
			dup2(nullfd, 2);
			if (nullfd > 2)
				close(nullfd);
		}
		/* Build argv: exe --master-of <sock> -- inner_argv... */
		int ic = 0;
		while (inner_argv[ic])
			ic++;
		char **av = malloc(sizeof(char *) * (ic + 5));
		int j = 0;
		av[j++] = exe;
		av[j++] = "--master-of";
		av[j++] = sockp;
		av[j++] = "--";
		int k;
		for (k = 0; k < ic; k++)
			av[j++] = inner_argv[k];
		av[j] = NULL;
		/* exe is absolute after main() resolved argv[0]; execvp only
		** matters for the bare-name fallback, when nothing on PATH
		** matched and exe is still the original argv[0]. */
		execvp(av[0], av);
		dprintf(1, "%s: %s\n", exe, strerror(errno));
		_exit(127);
	}
	dch_trace("spawn_master child=%d sock=%s", (int)pid, sockp);
	/* EOF on the status pipe: the program exec'd (or the master died,
	** which the probe below sees). Any text is why it did not start. */
	close(fd[1]);
	{
		long long left, end = wall_ms() + 10000;
		struct pollfd pf = { fd[0], POLLIN, 0 };

		for (;;)
		{
			if ((left = end - wall_ms()) <= 0)
			{
				fprintf(stderr, "dch: master did not accept "
				        "within 10s\n");
				close(fd[0]);
				return -1;
			}
			if (poll(&pf, 1, (int)left) <= 0)
				continue;
			got = read(fd[0], msg + n, sizeof msg - 1 - n);
			if (got < 0 && errno == EINTR)
				continue;
			if (got <= 0 || (n += (size_t)got) == sizeof msg - 1)
				break;
		}
	}
	close(fd[0]);
	if (n)
	{
		msg[n] = '\0';
		chomp(msg);
		fprintf(stderr, "dch: %s: %s\n", name, msg);
		return -1;
	}
	/* Parent: wait until the master accepts, so the caller's own connect
	** cannot land before bind() or between bind() and listen(). Poll on a
	** fine 5ms quantum against a 10s wall-clock cap (an iteration count
	** overran it under load): the master binds in a few ms, so a coarse
	** quantum just adds dead time to every cold start, while a slow runner
	** may need seconds to exec. The probe is a connect+close the master
	** drops as a plain EOF. Out of time is a failure (-1) with its own
	** message; the still-starting master is left alone. */
	int status = 0;
	long long end = wall_ms() + 10000;
	for (;;)
	{
		int probe = control_connect_path(sockp);
		if (probe >= 0)
		{
			close(probe);
			return 0;
		}
		if (wall_ms() >= end)
			break;
		struct timespec ts = {0, 5 * 1000 * 1000}; /* 5ms */
		nanosleep(&ts, NULL);
		pid_t r = waitpid(pid, &status, WNOHANG);
		if (r == pid)
		{
			dch_trace("spawn_master child=%d died early status=%d",
			          (int)pid, status);
			return -1;
		}
	}
	fprintf(stderr, "dch: master did not accept within 10s\n");
	return -1;
}

/* a control_connect_path EAGAIN: said by the verbs, the attach and the
** picker's status line */
#define STUCK_MSG "session %s is not accepting connections — its master " \
                  "may be stopped or wedged"
static const char stuck_fmt[] = "dch: " STUCK_MSG "\n";

static int attach_xray_view(void);
static int attach_remote;	/* this attach is a host's: so is its chord's view */
/* The next attach's route as the picked row or the view is (#210), not as
** its name routes: 1 the host's, -1 local, 0 by the name. One attach. */
static int attach_route;

static int
do_attach(char *exe, int forced_name, int force, char **inner_argv,
          int inner_argc)
{
	/* Nesting refuse. */
	const char *nested = getenv("DCH_SESSION");
	int route = attach_route;

	attach_route = 0;
	if (nested && *nested)
	{
		fprintf(stderr,
		    "dch: already inside session '%s'; nesting disabled\n",
		    nested);
		return 1;
	}

	attach_xray = attach_xray_view;	/* local and remote (#187, #189) */
	/* Remote (#087): --host names the host; else an exact local name
	** wins, and host:name is remote. */
	attach_remote = 1;	/* until the name turns out local, below */
	if (remote_host)
		return dch_remote_attach(remote_host, session_name);
	/* never with remote hosts off (#122); discovery still names hosts */
	if (remote_ok && dch_remote_on() &&
	    (route ? route > 0 && strchr(session_name, ':')
	     : name_is_remote(session_name)))
	{
		char host[512];
		char *colon = strchr(session_name, ':');

		snprintf(host, sizeof host, "%.*s", (int)(colon - session_name),
		         session_name);
		return dch_remote_attach(host, colon + 1);
	}
	attach_remote = 0;

	/* If auto-attaching and base is busy, advance to next free slot. */
	if (!forced_name && !force)
	{
		char base[512];
		snprintf(base, sizeof(base), "%s", session_name);
		int i;
		for (i = 1; i <= 999; i++)
		{
			char cand[600];
			if (i == 1)
				snprintf(cand, sizeof(cand), "%s", base);
			else
				snprintf(cand, sizeof(cand), "%s-%d",
				         base, i);
			char sp[1100];
			/* a candidate that does not fit is not free */
			if (strlen(cand) >= sizeof(session_name) ||
			    (size_t)snprintf(sp, sizeof(sp), "%s/%s.sock",
			                     sock_dir, cand) >= sizeof(sp))
				continue;
			struct stat st;
			if (stat(sp, &st) != 0 || !S_ISSOCK(st.st_mode) ||
			    live_clients_on(sp) == 0)
			{
				memcpy(session_name, cand, strlen(cand) + 1);
				break;
			}
		}
		if (i > 999)
		{
			fprintf(stderr,
			    "dch: 999 attached sessions for base '%s' — refusing\n",
			    base);
			return 1;
		}
	}

	if (make_sock_path(session_name) < 0)
		return 1;
	/* #203: a given name (-n, a pick) that fails name_ok creates
	** nothing; an auto-name is the caller's own and stays as it is */
	if (!attach_only && (session_name[0] == '-' ||
	                     xr_label_id_shaped(session_name,
	                                        strlen(session_name)) ||
	                     (forced_name && !name_ok(session_name))))
	{
		int probe = control_connect_path(sock_path);

		/* a session already named so is attached, by its exact name */
		if ((probe < 0 || !name_listed(session_name)) &&
		    (refuse_id_name(session_name) ||
		     refuse_path_name(session_name)))
			exit(2);	/* not 2 returned: that is ATTACH_SWITCH */
		close(probe);
	}
	{
		struct stat st;

		if (attach_only && (stat(sock_path, &st) < 0 ||
		                    !S_ISSOCK(st.st_mode)))
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
	}

	if (tcgetattr(0, &orig_term) < 0)
	{
		fprintf(stderr, "dch: attaching requires a terminal\n");
		return 1;
	}

	/* Hot path: if a socket node is present, attach to it directly — a single
	** connect(), no probe. attach_main() only RETURNS when connect() fails; a
	** live master never returns (it runs the session and exit()s). So a return
	** here means the node is a dead socket left by a crashed master whose
	** atexit unlink never ran — fall through and respawn. A stray non-socket
	** file (S_ISSOCK false) skips straight to the spawn path. This decides
	** spawn-vs-attach by LIVENESS (does connect() succeed?), which stat() alone
	** cannot — without it a dead socket looks attachable and a stray file makes
	** the master's bind() hit EADDRINUSE ("failed to spawn"). */
	{
	struct stat st;
	if (stat(sock_path, &st) == 0 && S_ISSOCK(st.st_mode))
	{
		dch_trace("attach hot-path sock=%s", sock_path);
		client_pidfile_create(sock_path);
		/* Returns only if connect() failed, or the user asked to
		** switch sessions — the latter is a live session, not a dead
		** socket, so it must not fall through to the respawn. */
		int rc = attach_main(1);

		if (rc == ATTACH_SWITCH || rc == ATTACH_ENDED)
			return rc;
		/* live but stuck: never respawned over (#269) */
		if (rc == 1 && errno == EAGAIN)
		{
			fprintf(stderr, stuck_fmt, session_name);
			return 1;
		}
		dch_trace("hot-path connect failed; dead socket, respawning");
	}
	}
	if (attach_only)
	{
		fprintf(stderr, "no session: %s\n", session_name);
		return 1;
	}

	/* No node, a stray non-socket file, or a dead socket: clear whatever is
	** there so bind() succeeds, then spawn a fresh master and attach. */
	unlink(sock_path);
	strip_caller_agent();
	setenv("DCH_SESSION", session_name, 1);
	{
	char *default_argv[] = { NULL, NULL };
	int rc;

	if (inner_argc == 0)
	{
		char *sh = getenv("SHELL");
		default_argv[0] = sh && *sh ? sh : "/bin/sh";
		inner_argv = default_argv;
	}
	rc = spawn_master(exe, session_name, sock_path, inner_argv);
	/* The master took DCH_SESSION with it; this client is not inside the
	** session, and a switch from it would hit the nesting refusal (#109). */
	unsetenv("DCH_SESSION");
	if (rc < 0)
	{
		fprintf(stderr, "dch: failed to spawn session\n");
		return 1;
	}
	}
	client_pidfile_create(sock_path);
	return attach_main(0);
}

/* do_attach(), plus the double-tap-detach session switcher: when the attached
** client returns ATTACH_SWITCH, show the same picker as `dch -l` and attach to
** whatever the user selects. Quitting the picker just ends the loop, leaving
** the previous session detached and running. picked: the first attach is
** the picker's (dch -l); a session the picker opened that ends goes back to
** it, on History (#133), where any other's end exits to the shell. */
static int
attach_with_switch(char *exe, int forced_name, int force, char **inner_argv,
                   int inner_argc, int picked)
{
	for (;;)
	{
		char chosen[sizeof session_name];
		long since = (long)time(NULL);
		int rc;

		attach_end_back = picked;
		rc = do_attach(exe, forced_name, force, inner_argv, inner_argc);
		attach_end_back = 0;
		if (rc == ATTACH_ENDED)
			pk_ended(session_name, since);
		else if (rc != ATTACH_SWITCH)
			return rc;
		/* the picker opens on the session just left (#098) */
		else if (!remote_host)
			pk_focus(session_name);
		else if ((size_t)snprintf(chosen, sizeof chosen, "%s:%s",
		                          remote_host, session_name) < sizeof chosen)
			pk_focus(chosen);
		pk_remote = 1;
		attach_only = 0;	/* before the pick: a view's o sets it */
		if (pick_session("", chosen,
		                 sizeof(chosen), 1) != 0)
			return 0;
		snprintf(session_name, sizeof(session_name), "%s", chosen);
		picked = 1;
		forced_name = 1;
		force = 1;
		remote_ok = 1;
		remote_host = NULL;
	}
}

/* ---- control verbs: --read --wait --keys --send --run --spawn ------- */
/*
** These open a CONTROL connection to the session master: request frames go
** out with the normal packet writer, responses come back framed as
** [type][len:2 LE][payload] (MSG_READ_DATA/READ_END/WAIT_HIT/ACK). A control
** connection never attaches, so reading a session can't perturb it.
*/

/* Inactivity deadline for --read/--keys responses. Generous: the master
** answers in microseconds; this only bounds a dead/ancient master. */
#define CTL_IDLE_MS 2000

static const char no_vt_msg[] =
    "dch: this session has no terminal mirror (dch-lite build or DCH_NO_VT)\n"
    "     — install full dch and restart the session to use this command\n";
/* A master that explicitly answers DCH_ST_UNSUP is provably old — it spoke
** the control protocol well enough to say "I don't know that verb". */
static const char old_master_msg[] =
    "dch: session master is older than this dch and does not support that\n"
    "     command — try `dch --restart <session>`; if that fails too, the\n"
    "     master predates live restart and the session must be recreated\n";
/* ctl_dead_exit's got_any==false case: total silence within CTL_IDLE_MS.
** That does NOT prove an old master — connect() succeeds via the kernel
** listen backlog even when the master never accept()s, so a wedged or
** stopped (SIGSTOP/App Nap) master looks identical on the wire. Name both
** causes instead of guessing. */
static const char no_reply_msg[] =
    "dch: session master is not answering — it may be wedged or stopped\n"
    "     (SIGSTOP, App Nap), or too old to speak this dch's control\n"
    "     protocol. Check whether its process is alive or stuck (e.g. ps)\n"
    "     before recreating anything; try `dch --restart <session>` first\n";

/* How long a connect waits on a full listen backlog: --ls-json's budget
** for a whole screen fetch (DET_DEADLINE_MS). A pending connect to a master
** that accepts, however loaded, connects at once; only a stopped or wedged
** master's full backlog waits (#265). */
#define CTL_CONNECT_MS 500

/* -1 with errno EAGAIN: a listener is there, but its backlog stayed full
** for CTL_CONNECT_MS (Linux). macOS refuses that connect at once, as it
** does a dead master's socket: there a refused socket node whose master's
** argv still names it is the same stuck master (#268). That is a live,
** stuck master, never a dead socket to unlink or revive over. */
int
control_connect_path(const char *path)
{
	struct sockaddr_un sa;
	/* Linux bounds a unix connect's wait for backlog room by SO_SNDTIMEO */
	struct timeval tv = {0, CTL_CONNECT_MS * 1000}, t0 = {0, 0};
	int s, e;

	if (strlen(path) > sizeof(sa.sun_path) - 1)
	{
		errno = ENAMETOOLONG;	/* never a stale EAGAIN */
		return -1;
	}
	s = socket(PF_UNIX, SOCK_STREAM, 0);
	if (s < 0)
		return -1;
	memset(&sa, 0, sizeof(sa));
	sa.sun_family = AF_UNIX;
	strcpy(sa.sun_path, path);
	(void)setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
	if (connect(s, (struct sockaddr *)&sa, sizeof(sa)) < 0)
	{
		/* EINTR comes only from that wait: a full backlog too */
		e = errno == EINTR ? EAGAIN : errno;
#ifdef __APPLE__
		struct stat st;

		/* only after a refusal of a socket node: off the common path */
		if (e == ECONNREFUSED && lstat(path, &st) == 0 &&
		    S_ISSOCK(st.st_mode) && masters_of(path, 0) > 0)
			e = EAGAIN;
#endif
		close(s);
		errno = e;
		return -1;
	}
	(void)setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &t0, sizeof t0);
	return s;
}

static int
control_connect(const char *name, int quiet)
{
	int s, e;

	if (make_sock_path(name) < 0)
		return -1;
	s = control_connect_path(sock_path);
	e = errno;
	if (s < 0 && e == EAGAIN)
		fprintf(stderr, stuck_fmt, name);
	else if (s < 0 && !quiet)
		fprintf(stderr, "dch: no session: %s\n", name);
	errno = e;
	return s;
}

static void
send_ctl(int s, unsigned char type, const void *payload, size_t len)
{
	struct packet pkt;

	memset(&pkt, 0, sizeof(pkt));
	pkt.type = type;
	pkt.len = (unsigned short)len;
	if (len)
		memcpy(pkt.u.buf, payload, len);
	write_packet_or_fail(s, &pkt);
}

/* Reassembly state for master->client response frames. */
struct ctl_reader
{
	unsigned char buf[PKT_HDR + PKT_MAX];
	size_t fill;
	int got_any; /* any bytes ever (old-master detection) */
	int eof;     /* the peer hung up (EOF or a read error), not silence */
};

/* Next response frame. Returns 1 (frame), 0 (idle_ms of silence), -1 (EOF /
** error). The deadline is INACTIVITY-based: every received byte resets it,
** so a large response streaming slowly never false-trips. */
static int
ctl_next_frame(int s, struct ctl_reader *r, int idle_ms,
               unsigned char *type, unsigned char *payload, unsigned int *len)
{
	for (;;)
	{
		if (r->fill >= PKT_HDR)
		{
			unsigned int flen = (unsigned int)r->buf[1] |
			                    ((unsigned int)r->buf[2] << 8);
			unsigned int plen =
			    (r->buf[0] == MSG_READ_DATA ||
			     r->buf[0] == MSG_WAIT_HIT ||
			     r->buf[0] == MSG_READ_CURSOR ||
			     r->buf[0] == MSG_READ_META ||
			     r->buf[0] == MSG_CMD_STAT ||
			     r->buf[0] == MSG_RING_HDR) ? flen : 0;

			if (plen > PKT_MAX)
				return -1; /* corrupt */
			if (r->fill >= PKT_HDR + plen)
			{
				*type = r->buf[0];
				*len = flen;
				if (plen)
					memcpy(payload, r->buf + PKT_HDR, plen);
				memmove(r->buf, r->buf + PKT_HDR + plen,
				        r->fill - PKT_HDR - plen);
				r->fill -= PKT_HDR + plen;
				return 1;
			}
		}

		struct pollfd pfd = { s, POLLIN, 0 };
		int rc = poll(&pfd, 1, idle_ms);
		if (rc == 0)
			return 0;
		if (rc < 0)
		{
			if (errno == EINTR)
				continue;
			return -1;
		}
		ssize_t n = read(s, r->buf + r->fill,
		                 sizeof(r->buf) - r->fill);
		if (n <= 0)
		{
			r->eof = 1;
			return -1;
		}
		r->fill += (size_t)n;
		r->got_any = 1;
	}
}

/* Map a READ_END/ACK status to an exit code + message. */
static int
ctl_status_exit(unsigned int status)
{
	switch (status)
	{
	case DCH_ST_OK:
		return 0;
	case DCH_ST_NOVT:
		fputs(no_vt_msg, stderr);
		return 3;
	case DCH_ST_TRUNC:
		fprintf(stderr, "dch: response truncated (over 2MB), "
		        "tail kept\n");
		return 0;
	case DCH_ST_BUSY:
		fprintf(stderr, "dch: session output queue busy, retry\n");
		return 1;
	case DCH_ST_UNSUP:
		fputs(old_master_msg, stderr);
		return 1;
	default:
		fprintf(stderr, "dch: session terminal mirror error\n");
		return 1;
	}
}

static int
ctl_dead_exit(struct ctl_reader *r, const char *verb)
{
	if (!r->got_any)
		fputs(no_reply_msg, stderr);
	else
		fprintf(stderr, "dch: truncated %s response\n", verb);
	return 1;
}

/* ---- Built-in agent-state detection (--status/--wait/--ls-json) ----
**
** Classifies the session's visible screen against a small table of UI
** strings the popular agent harnesses paint (permission prompts, "esc to
** interrupt" footers, spinner glyphs). Runs client-side over the existing
** MSG_READ protocol: zero master changes, so running sessions gain
** detection on a plain binary update. Any failure — lite/no-mirror
** master, busy, timeout, truncation, old master — silently falls back to
** the activity heuristic. DCH_NO_DETECT=1 disables detection. */

#define DET_CAP           (128 * 1024) /* tail-kept screen buffer */
#define DET_LINES         20           /* bottom non-empty lines scanned */
#define DET_DEADLINE_MS   500          /* wall-clock cap per fetch */
#define DET_WORK_ACT_SECS 30           /* "working" needs output this new */

/* Fetch the visible screen (plain, or with SGR when ansi) into buf (cap
** bytes + NUL). Keeps the TAIL when the stream exceeds cap — detection
** reads bottom lines (the master itself tail-caps at 2MB). The deadline is wall-clock:
** ctl_next_frame's timeout is inactivity-based, and a master trickling
** bytes must not stall --ls-json. flags: DCH_READ_F_* (0: a 4-byte request).
** st, when set: the reply's DCH_ST_*, -1 when none came (timeout, hang-up).
** Returns content length, -1 on any failure, -2 no mirror. */
static int
fetch_screen(const char *path, char *buf, size_t cap, int deadline_ms,
             int ansi, unsigned int flags, int *st)
{
	unsigned char req[5], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	struct timeval t0, now;
	unsigned int len;
	size_t used = 0;
	int s, rc;

	if (st)
		*st = -1;
	if (cap < PKT_MAX)
		return -1;
	s = control_connect_path(path);
	if (s < 0)
		return -1;
	req[0] = ansi ? DCH_READ_ANSI : DCH_READ_PLAIN;
	req[1] = DCH_READ_VISIBLE;
	req[2] = req[3] = 0;
	req[4] = (unsigned char)flags;
	send_ctl(s, MSG_READ, req, flags ? 5 : 4);
	gettimeofday(&t0, NULL);
	for (;;)
	{
		int left;

		gettimeofday(&now, NULL);
		left = deadline_ms -
		       (int)((now.tv_sec - t0.tv_sec) * 1000 +
		             (now.tv_usec - t0.tv_usec) / 1000);
		if (left <= 0)
			break;
		rc = ctl_next_frame(s, &r, left, &type, payload, &len);
		if (rc <= 0)
			break;
		if (type == MSG_READ_DATA)
		{
			/* len <= PKT_MAX <= cap: one frame never overflows buf */
			if (used + len > cap)
			{
				size_t drop = used + len - cap;
				memmove(buf, buf + drop, used - drop);
				used -= drop;
			}
			memcpy(buf + used, payload, len);
			used += len;
		}
		else if (type == MSG_READ_END)
		{
			close(s);
			if (st)
				*st = (int)len;
			if (len == DCH_ST_NOVT)
				return -2;
			if (len != DCH_ST_OK && len != DCH_ST_TRUNC)
				return -1;
			buf[used] = '\0';
			return (int)used;
		}
		else
			break; /* protocol violation */
	}
	close(s);
	return -1;
}

/* One rule: ANDed lowercase substrings (s2 optional). OR variants are
** separate rows. Strings are facts about the harnesses' own UIs (herdr
** is prior art for the approach; nothing is ported from it — AGPL).
** A leading '^' on s1 makes a line rule: only a line that starts with s1
** (after leading blanks) and contains s2 (if any) matches — the footer
** itself, not prose that quotes it. Any length: while background agents run, the
** footer runs to the screen width. */
struct det_rule
{
	const char *s1, *s2;
};

/* Screen shows history, not live state — abort detection entirely. */
static const struct det_rule det_suppress[] = {
	{ "showing detailed transcript", 0 },              /* claude ctrl-o */
	{ "pgup/pgdn to", "q to quit" },                   /* codex pager   */
};
/* Waiting on a human. Checked before working: a stale spinner must not
** mask a live permission prompt. */
static const struct det_rule det_blocked[] = {
	{ "do you want to proceed?", 0 },                  /* claude,gemini */
	{ "enter to select", "esc to cancel" },            /* claude forms  */
	{ "^esc to cancel", "tab to amend" },              /* claude perms  */
	/* plan approval ("Ready to code?"): no esc footer; the question's
	** start, as it wraps at its end under ~90 columns */
	{ "^claude has written up a plan", 0 },            /* claude plan   */
	{ "^background work is running", 0 },              /* claude /exit  */
	{ "press enter to confirm or esc to cancel", 0 },  /* codex         */
	{ "enter to submit answer", 0 },                   /* codex forms   */
	{ "allow command?", 0 },                           /* codex exec    */
	{ "permission required", 0 },                      /* opencode      */
	{ "waiting for approval", 0 },                     /* cursor        */
	{ "waiting for user confirmation", 0 },            /* gemini        */
	{ "allow execution", 0 },                          /* gemini        */
	{ "run this command?", 0 },                        /* cursor        */
	{ "proceed (y)", 0 },                              /* cursor        */
};
/* Busy right now (footer hints; spinner handled separately). */
static const struct det_rule det_working[] = {
	{ "esc to interrupt", 0 },                 /* claude,codex,opencode */
	{ "ctrl+c to interrupt", 0 },                      /* opencode      */
};

/* Line rule r ('^' s1) against the lines of hay. */
static int
det_line(const struct det_rule *r, const char *hay)
{
	const char *p = hay, *e, *nl, *q, *s1 = r->s1 + 1;

	while (*p)
	{
		/* the loop ends on strchr's NULL, not on *e: the analyzer
		** cannot see that p + strlen(p) points at a NUL */
		nl = strchr(p, '\n');
		e = nl ? nl : p + strlen(p);
		q = e;
		while (p < e && (*p == ' ' || *p == '\t'))
			p++;
		while (q > p && (q[-1] == ' ' || q[-1] == '\t' ||
		                 q[-1] == '\r'))
			q--;
		if (!strncmp(p, s1, strlen(s1)) && !r->s2)
			return 1;
		if (!strncmp(p, s1, strlen(s1)))
			for (q = p; q < e; q++)
				if (!strncmp(q, r->s2, strlen(r->s2)))
					return 1;
		if (!nl)
			break;
		p = nl + 1;
	}
	return 0;
}

static int
det_match(const struct det_rule *t, size_t n, const char *hay)
{
	size_t i;
	for (i = 0; i < n; i++)
		if (t[i].s1[0] == '^' ? det_line(&t[i], hay) :
		    strstr(hay, t[i].s1) &&
		    (!t[i].s2 || strstr(hay, t[i].s2)))
			return 1;
	return 0;
}

/* TUI spinner glyphs (the 10-frame braille set harnesses animate; the
** whole U+2800 block would false-positive on progress bars/sparklines).
** Counts only at the start of a line — after whitespace/box-drawing —
** and followed by space or line end. Scans raw bytes; the ASCII fold
** never touches multibyte sequences. */
static int
det_spinner(const char *scr)
{
	/* U+280B U+2819 U+2839 U+2838 U+283C U+2834 U+2826 U+2827 U+2807
	** U+280F -> UTF-8 E2 A0 <third byte>: */
	static const unsigned char third[] = {
		0x8B, 0x99, 0xB9, 0xB8, 0xBC, 0xB4, 0xA6, 0xA7, 0x87, 0x8F
	};
	const unsigned char *p = (const unsigned char *)scr;
	int at_start = 1;

	while (*p)
	{
		if (*p == '\n')
		{
			at_start = 1;
			p++;
			continue;
		}
		if (at_start)
		{
			if (*p == ' ' || *p == '\t')
			{
				p++;
				continue;
			}
			/* skip box-drawing U+2500-U+257F (e.g. table edges) */
			if (p[0] == 0xE2 && (p[1] == 0x94 || p[1] == 0x95) &&
			    p[2])
			{
				p += 3;
				continue;
			}
			if (p[0] == 0xE2 && p[1] == 0xA0 && p[2])
			{
				size_t k;
				for (k = 0; k < sizeof(third); k++)
					if (p[2] == third[k] &&
					    (p[3] == ' ' || p[3] == '\n' ||
					     p[3] == '\0'))
						return 1;
			}
			at_start = 0;
		}
		p++;
	}
	return 0;
}

/* The last DET_LINES non-empty lines of screen text raw[0..n), folded to
** lowercase ASCII (UTF-8 bytes untouched), or NULL. Caller frees. */
static char *
tail_fold(const char *raw, size_t n)
{
	const char *end, *p;
	char *fold;
	size_t rl, k;
	int lines;

	end = raw + n;
	p = end;
	lines = 0;
	while (p > raw && lines < DET_LINES)
	{
		const char *ls = p, *q;
		int nonblank = 0;

		while (ls > raw && ls[-1] != '\n')
			ls--;
		for (q = ls; q < p; q++)
			if (*q != ' ' && *q != '\t' && *q != '\n')
			{
				nonblank = 1;
				break;
			}
		if (nonblank)
			lines++;
		p = ls > raw ? ls - 1 : raw;
	}
	rl = (size_t)(end - p);
	fold = malloc(rl + 1);
	if (!fold)
		return NULL;
	for (k = 0; k < rl; k++)
	{
		char c = p[k];
		if (c == '\0')
			c = ' ';
		/* explicit ASCII fold: tolower() is locale-dependent and UB
		** on negative char; this leaves UTF-8 bytes untouched */
		fold[k] = (c >= 'A' && c <= 'Z') ? c + 32 : c;
	}
	fold[rl] = '\0';
	return fold;
}

/* The last DET_LINES non-empty visible lines, folded (tail_fold), or NULL
** when there is no screen. *novt is set when the master has no terminal
** mirror. Caller frees. */
static char *
screen_tail(const char *path, int *novt)
{
	char *raw, *fold;
	int n;

	*novt = 0;
	raw = malloc(DET_CAP + 1);
	if (!raw)
		return NULL;
	n = fetch_screen(path, raw, DET_CAP, DET_DEADLINE_MS, 0, 0, NULL);
	if (n <= 0)
	{
		*novt = n == -2;
		free(raw);
		return NULL;
	}
	fold = tail_fold(raw, (size_t)n);
	free(raw);
	return fold;
}

static int
is_rule(const char *l)
{
	while (*l == ' ')
		l++;
	return strncmp(l, "\xe2\x94\x80", 3) == 0;
}

/* The composer: the bottom-most row that starts with "❯" (then a space, an
** NBSP or the end of the row) right under a "─" rule and running down to
** the next rule, its wrapped rows indented. Returns that row, *end = the
** closing rule; NULL when there is none. A permission prompt's "❯ 1. Yes"
** is indented and has no closing rule, so it never reads as one. */
static char *
composer_find(char *txt, char **end)
{
	char *ls, *nx, *comp = NULL, *prev = NULL;

	for (ls = txt; ls; prev = ls, ls = nx)
	{
		char *l;

		nx = strchr(ls, '\n');
		nx = nx ? nx + 1 : NULL;
		if (!prev || !is_rule(prev) || strncmp(ls, "\xe2\x9d\xaf", 3) ||
		    !(ls[3] == ' ' || ls[3] == '\n' || ls[3] == '\0' ||
		      strncmp(ls + 3, "\xc2\xa0", 2) == 0))
			continue;
		for (l = nx; l && !is_rule(l) && (*l == ' ' || *l == '\n'); )
		{
			l = strchr(l, '\n');
			l = l ? l + 1 : NULL;
		}
		if (l && is_rule(l))
		{
			comp = ls;
			*end = l;
		}
	}
	return comp;
}

/* A blocker phrase on folded screen text. Claude Code's composer gives
** way to a live prompt or form, so with a composer on screen only the
** rows below it count: text above it is transcript, and a reply quoting
** "Do you want to proceed?" is not a prompt. No composer (a dialog,
** another harness): the whole text counts. */
static int
det_blocker(char *fold)
{
	char *end, *from = composer_find(fold, &end) ? end : fold;

	return det_match(det_blocked,
	                 sizeof(det_blocked) / sizeof(det_blocked[0]), from);
}

/* ---- auto-allow: Claude Code's plain permission prompt (#074) ---------
**
** A port of Prosper's Always allow rule (AgentIslandPrompt.swift: parse,
** allowYes, isDontAskAgain): the master answers "1" into a prompt that is
** exactly Claude Code's "Do you want to proceed?" over "Yes", at most one
** "Yes, and don't ask again …" (never picked: it writes the user's
** settings; a curly ‘ or ’ reads as "'", #783) and a last "No…"; nothing
** else. The parse reads the plain
** screen bottom-up: the bottom-most numbered option, walked up through 1
** (at most AP_GAP other lines between two), exactly one "❯" caret, no
** checkboxes, the dialog the last thing on the screen (below it only rules
** and one "Esc to cancel" footer, then Claude Code's session-title rule).
** As in Prosper, a head off the screen top and hidden characters do not
** stop it: they guard what a human reads, and this approves any plain
** permission prompt. Prosper's preview forms are not parsed: never one.
** Never hook- or ask-rule-raised prompts either (Prosper #790, ap_hook:
** "Hook … requires confirmation for this", "Permission rule … requires
** confirmation for this", "Ask rule … overrides auto mode for this" and
** their footers); those wait for the human. */

#define AP_GAP 4	/* non-option lines allowed between two options */
#define AP_SLACK 2	/* a frame rule may end this many cells short */

/* Byte length of a blank (space, tab, NBSP) at p, else 0. */
static int
ap_ws(const char *p)
{
	return *p == ' ' || *p == '\t' ? 1 : !strncmp(p, "\xc2\xa0", 2) ? 2 : 0;
}

static const char *
ap_skip(const char *p)
{
	int w;

	while ((w = ap_ws(p)))
		p += w;
	return p;
}

/* Length of s with trailing blanks off. */
static size_t
ap_rtrim(const char *s, size_t n)
{
	while (n && (s[n - 1] == ' ' || s[n - 1] == '\t' ||
	             (n >= 2 && !strncmp(s + n - 2, "\xc2\xa0", 2))))
		n -= s[n - 1] == '\xa0' ? 2 : 1;
	return n;
}

/* Code points in s[0..n). */
static int
ap_count(const char *s, size_t n)
{
	int c = 0;
	size_t i;

	for (i = 0; i < n; i++)
		c += ((unsigned char)s[i] & 0xC0) != 0x80;
	return c;
}

/* Is the code point at p one of the UTF-8 glyphs in set (concatenated)? */
static int
ap_in(const char *p, const char *set)
{
	int n, m;
	unsigned c = ap_cp(p, &n);

	for (; *set; set += m)
		if (ap_cp(set, &m) == c)
			return n;
	return 0;
}

static int
ap_blank(const char *l)
{
	return !*ap_skip(l);
}

/* Only rule glyphs, blanks around them: a frame or a rule. */
static int
ap_rule_of(const char *l, const char *glyphs)
{
	const char *p = ap_skip(l);
	int n, any = 0;

	while (*p && !ap_blank(p))
	{
		if (!(n = ap_in(p, glyphs)))
			return 0;
		p += n;
		any = 1;
	}
	return any && ap_blank(p);
}

#define AP_RULES "─━═╌┄╭╮╰╯┌┐└┘"

static int
ap_rule(const char *l)
{
	return ap_rule_of(l, AP_RULES);
}

static int
ap_blank_or_rule(const char *l)
{
	return ap_blank(l) || ap_rule(l);
}

/* "Esc to cancel · Tab to amend": one that starts with it at any length
** (Claude Code appends hints and cuts at the width), else at most 64. */
static int
ap_footer(const char *l)
{
	const char *p = ap_skip(l);
	size_t n = ap_rtrim(p, strlen(p)), i;
	char low[512];

	if (n >= sizeof low)
		n = sizeof low - 1;
	for (i = 0; i < n; i++)
		low[i] = p[i] >= 'A' && p[i] <= 'Z' ? p[i] + 32 : p[i];
	low[n] = '\0';
	const char *f = strstr(low, "esc to cancel");

	return f == low || (f && ap_count(low, n) <= 64);
}

/* An AskUserQuestion header-chip line at column 0 or 1. */
static int
ap_chip(const char *l)
{
	const char *p = l;

	while (*p == ' ')
		p++;
	if (p - l > 1 || !*p)
		return 0;
	if (ap_in(p, "☐☒✔"))
		return 1;
	if (!ap_in(p, "←"))
		return 0;
	for (; *p; p++)
		if (ap_in(p, "☐☒✔"))
			return 1;
	return 0;
}

/* "  ❯ 2. Yes, and …" -> 2, *label (trimmed, NUL-cut in place), *caret;
** 0 when not an option line. */
static int
ap_option(char *l, char **label, int *caret)
{
	char *p = l + (ap_skip(l) - l), *e;

	*caret = 0;
	if (!strncmp(p, "❯", 3))
	{
		*caret = 1;
		p += ap_skip(p + 3) - p;
	}
	if (*p < '1' || *p > '9' || p[1] != '.' || !ap_ws(p + 2))
		return 0;
	*label = p + (ap_skip(p + 2) - p);
	e = *label + ap_rtrim(*label, strlen(*label));
	if (e == *label)
		return 0;
	*e = '\0';
	return *p - '0';
}

/* An option label's trailing key hint, "(esc)", "(shift+tab)": its open
** paren, else NULL. Only esc, tab, enter or modifier+key, as Prosper. */
static const char *
ap_hint(const char *label)
{
	size_t n = strlen(label), k, kl;
	const char *o = strrchr(label, '('), *plus, *key;
	static const char *mods[] = { "ctrl", "shift", "alt", "cmd", "option" };
	char in[32];

	if (!n || label[n - 1] != ')' || !o || (size_t)(label + n - 1 - o - 1) >=
	    sizeof in)
		return NULL;
	for (k = 0; o + 1 + k < label + n - 1; k++)
		in[k] = o[1 + k] >= 'A' && o[1 + k] <= 'Z' ? o[1 + k] + 32
		                                            : o[1 + k];
	in[k] = '\0';
	if (!(plus = strchr(in, '+')))
		return !strcmp(in, "esc") || !strcmp(in, "tab") ||
		       !strcmp(in, "enter") ? o : NULL;
	key = plus + 1;
	if (strchr(key, '+') || (kl = strlen(key)) < 1 || kl > 5)
		return NULL;
	for (k = 0; k < kl; k++)
		if (!isascii((unsigned char)key[k]) ||
		    !isalnum((unsigned char)key[k]))
			return NULL;
	for (k = 0; k < sizeof mods / sizeof *mods; k++)
		if (strlen(mods[k]) == (size_t)(plus - in) &&
		    !strncmp(in, mods[k], (size_t)(plus - in)))
			return o;
	return NULL;
}

/* Leading characters of a raw row that are blanks or "│". */
static int
ap_lead(const char *r)
{
	int n = 0, w;

	for (;;)
		if ((w = ap_ws(r)))
			r += w, n++;
		else if (!strncmp(r, "│", 3))
			r += 3, n++;
		else
			return n;
}

/* The label without its key hint (ap_hint), into out[cap]; the hint's
** open paren in *hint, or NULL. */
static void
ap_unhint(const char *label, char *out, size_t cap, const char **hint)
{
	*hint = ap_hint(label);
	snprintf(out, cap, "%.*s", (int)(*hint ? ap_rtrim(label,
	         (size_t)(*hint - label)) : strlen(label)), label);
}

struct ap_screen
{
	int nl, width;
	char **raw;	/* the rows */
	char **ln;	/* the rows unframed (a 1.x "│ … │" box row loses its
			** bars), trailing blanks off */
};

/* Row r is a frame: a rule at column 0 or 1, as wide as the widest rule
** on the screen (AP_SLACK cells short still counts). */
static int
ap_frame(const struct ap_screen *s, int r)
{
	return ap_rule(s->ln[r]) && ap_lead(s->raw[r]) <= 1 &&
	       ap_count(s->raw[r], ap_rtrim(s->raw[r], strlen(s->raw[r]))) +
	       AP_SLACK >= s->width;
}

/* Claude Code's session-title rule under a dialog: "─"s, " <title> ", "─",
** from column 0 across the widest rule. */
static int
ap_title_rule(const char *t, int width)
{
	size_t len = strlen(t);
	const char *lbl = t, *h, *e = t + len - 4;
	int cells = 0, n;

	if (strncmp(t, "─", 3) || len < 4 || strcmp(e, " ─"))
		return 0;
	while (!strncmp(lbl, "─", 3))
		lbl += 3;
	if (lbl >= e || *lbl != ' ')
		return 0;
	for (h = lbl; h < e && ap_ws(h); h += ap_ws(h))
		;
	if (h >= e)
		return 0;	/* a blank label */
	for (h = t; *h; h += n)
		cells += ap_cells(ap_cp(h, &n));
	return cells + AP_SLACK >= width;
}

/* Claude Code's ticking "⚠ Claude Code will automatically block this
** request in 1:35, to avoid holding up an unattended session" over the
** question (Prosper #784): its M:SS becomes "m:ss" in s, in place, only
** where whole lines read exactly that (words split by spaces or wraps), so
** the loop guard sees one prompt, not a new one each second. */
static void
ap_countdown(char *s)
{
	static regex_t re;
	static int st;		/* 0 not compiled yet, 1 ok, -1 failed */
	regmatch_t m[4];
	int fl = 0;
	char *d, *t;

	if (!st)
		st = regcomp(&re, "^( *\xe2\x9a\xa0(\xef\xb8\x8f)?[ \n]+Claude"
		    "[ \n]+Code[ \n]+will[ \n]+automatically[ \n]+block[ \n]+this"
		    "[ \n]+request[ \n]+in[ \n]+)[0-9]{1,2}:[0-9]{2}(,[ \n]+to"
		    "[ \n]+avoid[ \n]+holding[ \n]+up[ \n]+an[ \n]+unattended"
		    "[ \n]+session)$", REG_EXTENDED | REG_NEWLINE) ? -1 : 1;
	while (st > 0 && regexec(&re, s, 4, m, fl) == 0)
	{
		d = s + m[1].rm_eo;
		t = s + m[3].rm_so;
		memcpy(d, "m:ss", 4);	/* "M:SS" or "MM:SS": never longer */
		memmove(d + 4, t, strlen(t) + 1);
		s = d + 4 + (m[3].rm_eo - m[3].rm_so);
		fl = REG_NOTBOL;
	}
}

/* A prompt raised by a hook or an ask rule (Prosper #790): rows lo..hi-1,
** frames aside, joined with every blank removed (a wrap anywhere still
** matches), hold one of ap_marks: a literal (b NULL), or a, then b
** starting 1..n characters after a ends (every a tried; n 0: any
** distance, as a managed-policy rule may be any length and draws no
** "/permissions" line to back it, Prosper 6fe8094). A hook is always backed
** by "to update hooks", so its bound stays. Claude Code
** 2.1.318 draws "Hook PreToolUse:Bash requires confirmation for this
** command:" over "settings.json to update hooks", "Permission rule <rule>
** requires confirmation for this <t>." (over "/permissions to update
** rules", missing for managed policy) and, in auto mode, "Ask rule <rule>
** overrides auto mode for this <t>." over "/permissions to let auto mode
** decide". "/permissions to update rules" alone is no marker: it is drawn
** under a working-directory reason too. Classifier asks are answered. */
static const struct { const char *a, *b; int n; } ap_marks[] = {
	{ "toupdatehooks", NULL, 0 },
	{ "/permissionstoletautomodedecide", NULL, 0 },
	{ "Hook", "requiresconfirmationforthis", 200 },
	{ "Permissionrule", "requiresconfirmationforthis", 0 },
	{ "Askrule", "overridesautomodeforthis", 0 },
};

static int
ap_hook(const struct ap_screen *s, int lo, int hi)
{
	char *b = NULL, *p, *q;
	const char *t;
	size_t n, al, m;
	int k, w, hit = 0;
	FILE *f;

	if (!(f = open_memstream(&b, &n)))
		return 1;	/* unsure: leave it to the human */
	for (k = lo; k < hi; k++)
		if (!ap_frame(s, k))
			for (t = s->ln[k]; *t; t += w ? w : 1)
				if (!(w = ap_ws(t)))
					fputc(*t, f);
	if (fclose(f) != 0)
		return free(b), 1;
	for (m = 0; !hit && m < sizeof ap_marks / sizeof ap_marks[0]; m++)
	{
		al = strlen(ap_marks[m].a);
		for (p = b; !hit && (p = strstr(p, ap_marks[m].a)); p++)
			if (!ap_marks[m].b)
				hit = 1;
			else if (p[al] && (q = strstr(p + al + 1, ap_marks[m].b)))
			{
				/* characters, as Prosper's regex */
				for (n = 0, t = p + al; t < q; t++)
					n += ((unsigned char)*t & 0xc0) != 0x80;
				hit = !ap_marks[m].n || n <= (size_t)ap_marks[m].n;
			}
	}
	free(b);
	return hit;
}

/* Is the screen (plain text, rows by '\n') showing a Claude Code
** permission prompt the auto-allow rule may answer with "1"? Never a
** hook- or ask-rule-raised one (ap_hook): those wait for the human. 1 with
** *ident (the whole dialog: question, context, options and their detail,
** the caret aside, a countdown masked (ap_countdown): the loop guard
** compares it) and *title (its first line,
** e.g. "Bash command"), both malloc'd; else 0. The screen must also read
** blocked as --status reads it (det_blocker: under a composer only). */
int
dch_allow_prompt(const char *screen, char **ident, char **title)
{
	struct ap_screen s = { 0, 0, NULL, NULL };
	char *buf, *p, *lab[9], *lb, word[512], sq[1024], *ctx = NULL;
	const char *h;
	int ro[9], hi[9], li[9], nf = 0, carets = 0, ok = 0;
	int i, j, k, d, c, col, last = -1, tail, gap, seen, end, a1 = -1, a2 = -1;
	int na = 0, edge[2] = { 0, 0 };
	size_t len, tl;
	FILE *f;

	*ident = *title = NULL;
	if (!(p = tail_fold(screen, strlen(screen))))
		return 0;
	k = !det_match(det_suppress, sizeof det_suppress /
	               sizeof det_suppress[0], p) && det_blocker(p);
	free(p);
	if (!k || !(buf = strdup(screen)))
		return 0;
	for (p = buf, i = 0; p; p = strchr(p, '\n'), p = p ? p + 1 : p)
		i++;
	s.raw = calloc((size_t)i, sizeof *s.raw);
	s.ln = calloc((size_t)i, sizeof *s.ln);
	if (!s.raw || !s.ln)
		goto out;
	/* s.nl counts the rows as they are filled (the same i rows) */
	for (p = buf; p; s.nl++)
	{
		s.raw[s.nl] = p;
		if ((p = strchr(p, '\n')))
			*p++ = '\0';
	}
	for (i = 0; i < s.nl; i++)
	{
		col = (int)strspn(s.raw[i], " ");
		if (col <= 1 && ap_in(s.raw[i] + col, "╭╰┌└") &&
		    ap_rule(s.raw[i]))
			edge[col] = 1;
	}
	for (i = 0; i < s.nl; i++)
	{
		char *l;

		if (!(l = s.ln[i] = strdup(s.raw[i])))
			goto out;
		l[ap_rtrim(l, strlen(l))] = '\0';
		col = (int)strspn(l, " ");
		len = strlen(l);
		if (col <= 1 && edge[col] && !strncmp(l + col, "│", 3) &&
		    len >= (size_t)col + 6 && !strcmp(l + len - 3, "│"))
		{
			l[len - 3] = '\0';
			memmove(l, l + col + 3, len - (size_t)col - 5);
			l[ap_rtrim(l, strlen(l))] = '\0';
			if (l[0] == ' ')
				memmove(l, l + 1, strlen(l));
		}
		if (ap_rule(l) && (k = ap_count(s.raw[i], ap_rtrim(s.raw[i],
		                   strlen(s.raw[i])))) > s.width)
			s.width = k;
	}
	for (i = s.nl - 1; i >= 0 && last < 0; i--)
		if (ap_option(s.ln[i], &lb, &c))
			last = i;
	if (last < 0)
		goto out;
	/* Under the last option: its detail (up to a blank or a rule), then
	** only rules and, last, a footer or a rule, then at most the
	** session-title rule, right under the footer. */
	for (tail = last + 1; tail < s.nl && !ap_blank_or_rule(s.ln[tail]);
	     tail++)
		;
	for (i = tail; i < s.nl; i++)
		if (!ap_blank(s.ln[i]))
			a2 = a1, a1 = i, na++;
	end = a1;
	if (na >= 2 && ap_title_rule(s.ln[a1], s.width) && ap_footer(s.ln[a2]))
		end = a2;
	for (i = tail; i < end; i++)
		if (!ap_blank(s.ln[i]) && !ap_rule(s.ln[i]))
			goto out;
	if (end >= 0 ? !ap_rule(s.ln[end]) && !ap_footer(s.ln[end])
	             : tail > last + 1)
		goto out;
	/* Up through option 1: consecutive digits, at most AP_GAP other rows
	** between two. An option's detail: its non-blank, non-rule rows down
	** to the next option (the last one's: down to `tail`). */
	for (i = last, gap = 0; i >= 0 && (!nf || li[nf - 1] != 1); i--)
		if ((d = ap_option(s.ln[i], &lb, &c)))
		{
			if (nf && d != li[nf - 1] - 1)
				goto out;
			hi[nf] = nf ? ro[nf - 1] : tail;
			ro[nf] = i;
			li[nf] = d;
			lab[nf++] = lb;
			carets += c;
			gap = 0;
		}
		else if (++gap > AP_GAP)
			goto out;
	if (!nf || li[nf - 1] != 1 || nf < 2 || carets != 1)
		goto out;
	for (k = 0; k < nf; k++)
	{
		int n;

		ap_cp(lab[k] + 1, &n);
		if (ap_in(lab[k], "☐☑☒") ||
		    (lab[k][0] == '[' && lab[k][1] && lab[k][1 + n] == ']'))
			goto out;	/* a checkbox: a digit toggles it */
	}
	/* Above option 1, past blanks and frames: the question. */
	while (i >= 0 && (ap_blank(s.ln[i]) || ap_frame(&s, i)))
		i--;
	if (i < 0 || strcmp(ap_skip(s.ln[i]), "Do you want to proceed?"))
		goto out;
	/* Above it up to the dialog's top: the first solid frame with
	** something under it (dashed inner frames are passed). The context is
	** every non-frame row between; a header chip makes it a question
	** form. j < 0: the top is off the screen, as Prosper allows. */
	for (j = i - 1, seen = 0; j >= 0; j--)
		if (!ap_frame(&s, j))
			seen |= !ap_blank(s.ln[j]);
		else if (!ap_rule_of(s.ln[j], "╌┄") && seen)
			break;
	for (k = j + 1; k < i; k++)
		if (!ap_frame(&s, k) && ap_chip(s.ln[k]))
			goto out;
	if (ap_hook(&s, j + 1, i))
		goto out;
	/* allowYes: 2 or 3 options; option 1 a bare "Yes"; the last a "No…";
	** neither with detail rows; a middle one only "Yes, and don't ask
	** again …" (never picked), and never a mode change. lab[0] is the
	** bottom option, lab[nf - 1] option 1. */
	if (nf > 3 || strcmp(lab[nf - 1], "Yes"))
		goto out;
	for (k = 0; k < nf; k += nf - 1)
		for (d = ro[k] + 1; d < hi[k]; d++)
			if (!ap_blank_or_rule(s.ln[d]))
				goto out;
	ap_unhint(lab[0], word, sizeof word, &h);
	if (strcmp(word, "No") && strncmp(word, "No,", 3) &&
	    strncmp(word, "No ", 3))
		goto out;
	if (nf == 3)
	{
		size_t n = 0;

		ap_unhint(lab[1], word, sizeof word, &h);
		if (h && !strcmp(h, "(shift+tab)"))
			goto out;
		for (d = ro[1]; d < hi[1]; d++)
		{
			const char *t = d == ro[1] ? word : s.ln[d];

			if (d > ro[1] && ap_blank_or_rule(t))
				continue;
			while (*t && n + 1 < sizeof sq)
				if ((c = ap_ws(t)))
					t += c;
				else if (!strncmp(t, "\xe2\x80\x98", 3) ||
				         !strncmp(t, "\xe2\x80\x99", 3))
					sq[n++] = '\'', t += 3;	/* ‘ ’ */
				else
					sq[n++] = *t++;
		}
		sq[n] = '\0';
		if (strncmp(sq, "Yes,anddon'taskagain", 20) ||
		    strstr(sq, "(shift+tab)"))
			goto out;
	}
	/* identity: context and question (a countdown masked), then the
	** options with detail */
	if (!(f = open_memstream(&ctx, &tl)))
		goto out;
	for (k = j + 1; k <= i; k++)
		if (!ap_frame(&s, k))
			fprintf(f, "%s\n", s.ln[k]);
	if (fclose(f) != 0)
		goto out;
	ap_countdown(ctx);
	if (!(f = open_memstream(ident, &tl)))
		goto out;
	fputs(ctx, f);
	for (k = nf - 1; k >= 0; k--)
	{
		fprintf(f, "%d. %s\n", li[k], lab[k]);
		for (d = ro[k] + 1; d < hi[k]; d++)
			if (!ap_blank_or_rule(s.ln[d]))
				fprintf(f, "\t%s\n", ap_skip(s.ln[d]));
	}
	if (fclose(f) != 0)
		goto out;
	/* the title: the context's first line, else the question */
	for (k = j + 1; ap_frame(&s, k) || ap_blank(s.ln[k]); k++)
		;
	ok = (*title = strdup(ap_skip(s.ln[k]))) != NULL;
out:
	if (!ok)
	{
		free(*ident);
		*ident = NULL;
	}
	for (i = 0; s.ln && i < s.nl; i++)
		free(s.ln[i]);
	free(s.ln);
	free(s.raw);
	free(buf);
	free(ctx);
	return ok;
}

/* Classify the visible screen: "blocked", "working", or NULL (no signal
** or no screen — caller falls back). Detected "working" additionally
** requires recent pty output: the covered harnesses animate their
** spinner/status line while working (continuous repaint = fresh .act),
** so a frozen frame with a stale footer must read idle, not working. */
static const char *
detect_state(const char *name)
{
	const char *res = NULL;
	char *fold;
	int novt;

	if (dch_env_on("DCH_NO_DETECT") || make_sock_path(name) < 0)
		return NULL;
	fold = screen_tail(sock_path, &novt);
	if (!fold)
		return NULL;

	if (det_match(det_suppress,
	              sizeof(det_suppress) / sizeof(det_suppress[0]), fold))
		res = NULL;
	else if (det_blocker(fold))
		res = "blocked";
	else if (det_match(det_working,
	                   sizeof(det_working) / sizeof(det_working[0]),
	                   fold) ||
	         det_spinner(fold))
	{
		long ep = activity_epoch(name);
		if (ep && time(NULL) - ep <= DET_WORK_ACT_SECS)
			res = "working";
	}
	free(fold);
	return res;
}

/* Byte length of a leading non-ASCII UTF-8 char at p (a status glyph), 0
** for ASCII or a sequence cut short by the NUL. */
static size_t
det_glyph(const char *p)
{
	unsigned char c = (unsigned char)p[0];
	size_t g = c >= 0xF0 ? 4 : c >= 0xE0 ? 3 : c >= 0xC0 ? 2 : 0, k;

	for (k = 1; k < g; k++)
		if (!p[k])
			return 0;
	return g;
}

/* Live subagents, counted off Claude Code's agent panel: a "<glyph> main"
** row, then one "<glyph> <agent-type> ..." row per running subagent, at
** the bottom of the screen. The panel exists only while subagents do, so
** no panel reads 0 — as does no screen or DCH_NO_DETECT.
** ponytail: screen-scraped, so the count is what the panel shows — a
** prompt covering it reads 0 until answered. Exact counts need
** SubagentStart/SubagentStop hooks reporting into a sidecar. */
static int
detect_agents(const char *name)
{
	char *fold, *line, *next;
	int novt, in = 0, n = 0;

	if (dch_env_on("DCH_NO_DETECT") || make_sock_path(name) < 0)
		return 0;
	fold = screen_tail(sock_path, &novt);
	if (!fold)
		return 0;
	for (line = fold; line; line = next)
	{
		char *p = line, *q;
		size_t g;

		next = strchr(line, '\n');
		if (next)
			*next++ = '\0';
		while (*p == ' ')
			p++;
		g = det_glyph(p);
		if (!g || p[g] != ' ')
		{
			in = 0; /* prose or footer: not a panel row */
			continue;
		}
		p += g + 1;
		for (q = p + strlen(p); q > p && q[-1] == ' '; q--)
			;
		if (q - p == 4 && strncmp(p, "main", 4) == 0)
			in = 1, n = 0; /* the last panel on screen wins */
		else if (in)
			n++;
	}
	free(fold);
	return n;
}

/* Bridge sidecar gate for typing into the Codex TUI at socket `path`:
** 1 = the composer shows its empty placeholder, safe to type; 0 = an
** approval prompt, form or pager owns the keyboard; 2 = anything else (a
** draft in the composer, an unknown screen, a failed read); -1 = no
** terminal mirror or DCH_NO_DETECT, so there is no view at all. */
int
dch_pty_gate(const char *path)
{
	char *fold;
	int novt, res;

	if (dch_env_on("DCH_NO_DETECT"))
		return -1;
	fold = screen_tail(path, &novt);
	if (!fold)
		return novt ? -1 : 2;
	if (det_match(det_suppress,
	              sizeof(det_suppress) / sizeof(det_suppress[0]), fold) ||
	    det_blocker(fold))
		res = 0;
	else
		res = strstr(fold, "ask codex to do anything") ||
		      strstr(fold, "ask a follow-up question") ? 1 : 2;
	free(fold);
	return res;
}

/* Type `text` into the session at socket `path` as one bracketed paste, or
** press Enter when text is NULL. Codex folds a CR that arrives in the same
** burst as pasted text into the paste, so Enter is a separate, later call. */
int
dch_pty_push(const char *path, const char *text)
{
	int s = control_connect_path(path);
	size_t l, off;

	if (s < 0)
		return -1;
	if (!text)
		send_ctl(s, MSG_PUSH, "\r", 1);
	else
	{
		send_ctl(s, MSG_PUSH, "\033[200~", 6);
		for (l = strlen(text), off = 0; off < l; off += PKT_MAX)
			send_ctl(s, MSG_PUSH, text + off,
			         l - off > PKT_MAX ? PKT_MAX : l - off);
		send_ctl(s, MSG_PUSH, "\033[201~", 6);
	}
	close(s);
	return 0;
}

static int
do_read_verb(const char *name, int ansi, int recent_lines, int want_cursor)
{
	unsigned char req[5], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	int s, rc, got_cursor = 0;

	s = control_connect(name, 0);
	if (s < 0)
		return 1;

	req[0] = ansi ? DCH_READ_ANSI : DCH_READ_PLAIN;
	req[1] = recent_lines >= 0 ? DCH_READ_RECENT : DCH_READ_VISIBLE;
	req[2] = recent_lines >= 0 ? (recent_lines & 0xff) : 0;
	req[3] = recent_lines >= 0 ? ((recent_lines >> 8) & 0xff) : 0;
	req[4] = want_cursor ? DCH_READ_F_CURSOR : 0;
	/* Stay on the 4-byte request unless a flag is actually set: a master
	** built before flags existed then sees the exact bytes it always did. */
	send_ctl(s, MSG_READ, req, want_cursor ? sizeof(req) : 4);

	for (;;)
	{
		rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
		if (rc <= 0)
		{
			close(s);
			return ctl_dead_exit(&r, "read");
		}
		if (type == MSG_READ_DATA)
			fwrite(payload, 1, len, stdout);
		else if (type == MSG_READ_CURSOR && len == DCH_READ_CURSOR_LEN)
		{
			/* stderr keeps the stdout screen dump byte-exact; 1-based
			** to match CUP (\e[row;colH) so callers can paste it in. */
			unsigned int row = (unsigned int)payload[0] |
			                   ((unsigned int)payload[1] << 8);
			unsigned int col = (unsigned int)payload[2] |
			                   ((unsigned int)payload[3] << 8);

			fprintf(stderr, "cursor %u %u %u %u\n", row + 1,
			        col + 1, payload[4], payload[5]);
			got_cursor = 1;
		}
		else if (type == MSG_READ_END)
		{
			close(s);
			/* end the screen dump on a newline for shells/agents */
			if (r.got_any)
				fputc('\n', stdout);
			rc = ctl_status_exit(len);
			/* No cursor frame despite asking: either the screen was
			** tail-cut (the row would point at the wrong line, so
			** the master withholds it) or the master predates the
			** flag and ignored it. Never let a caller that asked for
			** the caret mistake either case for "row 1, column 1". */
			if (rc == 0 && want_cursor && !got_cursor)
			{
				fputs(len == DCH_ST_TRUNC
				    ? "dch: screen too big to place the cursor "
				      "in the kept tail; caret not reported\n"
				    : "dch: session master predates --cursor; "
				      "restart the session to enable it\n",
				    stderr);
				return 1;
			}
			return rc;
		}
		else
		{
			close(s);
			return 1; /* protocol violation */
		}
	}
}

/* ---- --follow: the screen as a stream of self-delimited frames ----
**
**   dch-frame <len> <rows>x<cols> <crow> <ccol> <cvis>\n<len payload bytes>
**
** The payload is byte-for-byte what `dch --read <name> --ansi` prints, its
** trailing newline included. crow/ccol are 1-based like --cursor's (0 0 when
** the screen rendered over 2 MB and was tail-cut, so the caret has no row);
** cvis is 1/0. Every frame is whole: a caret-only change still carries the
** size and the full screen. A future incompatible format changes the first
** token. One MSG_READ per interval carries the generation of the last screen
** received; an idle session answers DCH_ST_SAME without rendering, so
** following costs a few syscalls per tick. A control connection never
** attaches: nothing is written to the pty, no size is sent. */
#define FOLLOW_MS_DEFAULT 150

struct follow_frame
{
	char *buf;
	size_t len, cap;
	unsigned int rows, cols, crow, ccol, cvis;
	unsigned long gen;
};

/* One round trip on control socket s. since = generation of the last screen
** received, or -1 for "render regardless". Returns 1 (new screen in *f), 0
** (unchanged or busy), -1 (connection lost or silent), 2 (refused, *status
** set). */
static int
follow_fetch(int s, struct ctl_reader *r, long long since,
             struct follow_frame *f, unsigned int *status)
{
	unsigned char req[PKT_HDR + DCH_READ_REQ_MAX], payload[PKT_MAX], type;
	unsigned int len, plen = since < 0 ? 5 : DCH_READ_REQ_MAX;
	int got_meta = 0;

	req[0] = MSG_READ;
	req[1] = (unsigned char)plen;
	req[2] = 0;
	req[3] = DCH_READ_ANSI;
	req[4] = DCH_READ_VISIBLE;
	req[5] = req[6] = 0;
	req[7] = DCH_READ_F_CURSOR | DCH_READ_F_META;
	if (since >= 0)
	{
		req[7] |= DCH_READ_F_SINCE;
		req[8] = (unsigned char)(since & 0xff);
		req[9] = (unsigned char)((since >> 8) & 0xff);
		req[10] = (unsigned char)((since >> 16) & 0xff);
		req[11] = (unsigned char)((since >> 24) & 0xff);
	}
	/* Not send_ctl: write_packet_or_fail exits 1 on a dead master, and a
	** session ending under us is a clean exit 0. One small write on a
	** stream socket is never split. */
	if (write(s, req, PKT_HDR + plen) != (ssize_t)(PKT_HDR + plen))
		return -1;
	f->len = 0;
	f->crow = f->ccol = f->cvis = 0;
	for (;;)
	{
		if (ctl_next_frame(s, r, CTL_IDLE_MS, &type, payload, &len) <= 0)
			return -1;
		if (type == MSG_READ_META && len == DCH_READ_META_LEN)
		{
			f->gen = (unsigned long)payload[0] |
			         ((unsigned long)payload[1] << 8) |
			         ((unsigned long)payload[2] << 16) |
			         ((unsigned long)payload[3] << 24);
			f->rows = (unsigned int)payload[4] |
			          ((unsigned int)payload[5] << 8);
			f->cols = (unsigned int)payload[6] |
			          ((unsigned int)payload[7] << 8);
			got_meta = 1;
		}
		else if (type == MSG_READ_CURSOR && len == DCH_READ_CURSOR_LEN)
		{
			f->crow = ((unsigned int)payload[0] |
			           ((unsigned int)payload[1] << 8)) + 1;
			f->ccol = ((unsigned int)payload[2] |
			           ((unsigned int)payload[3] << 8)) + 1;
			f->cvis = payload[4] ? 1 : 0;
		}
		else if (type == MSG_READ_DATA)
		{
			/* +1: room for --read's closing newline */
			if (f->len + len + 1 > f->cap)
			{
				size_t ncap = (f->len + len + 1) * 2;
				char *nb = realloc(f->buf, ncap);

				if (!nb)
					return -1;
				f->buf = nb;
				f->cap = ncap;
			}
			memcpy(f->buf + f->len, payload, len);
			f->len += len;
		}
		else if (type == MSG_READ_END)
		{
			/* busy: our own queue is drained, so this is the
			** screen outgrowing it — try again next tick */
			if ((len == DCH_ST_SAME && since >= 0) ||
			    len == DCH_ST_BUSY)
				return 0;
			if (len != DCH_ST_OK && len != DCH_ST_TRUNC)
			{
				*status = len;
				return 2;
			}
			if (!got_meta)
			{
				/* a master from before request flags ignores
				** them and answers a plain read */
				*status = DCH_ST_UNSUP;
				return 2;
			}
			if (!f->buf && !(f->buf = malloc(f->cap = 64)))
				return -1;
			f->buf[f->len++] = '\n';
			return 1;
		}
		else
			return -1; /* protocol violation */
	}
}

/* --follow's refusal: the --read statuses, except that a master which
** rejects the new request flags (DCH_ST_ERR) or ignores them (no meta frame,
** DCH_ST_UNSUP here) but still serves a plain read is simply too old. */
static int
follow_refused(unsigned int status)
{
	static char probe[PKT_MAX + 1];

	if ((status == DCH_ST_ERR &&
	     fetch_screen(sock_path, probe, PKT_MAX, CTL_IDLE_MS, 0, 0,
	                  NULL) >= 0) ||
	    status == DCH_ST_UNSUP)
	{
		fputs("dch: session master predates --follow; restart the "
		      "session (dch --restart <name>)\n", stderr);
		return 1;
	}
	return ctl_status_exit(status);
}

static int
do_follow_verb(const char *name)
{
	struct follow_frame a = {0}, b = {0}, *last = &a, *cur = &b, *t;
	struct ctl_reader r = {0};
	const char *e = getenv("DCH_FOLLOW_MS");
	long iv = e ? atol(e) : 0;
	long long since = -1, next;
	unsigned int status = 0;
	int s, rc, sent = 0, fails = 0, ret = 0;
	struct sigaction sa;

	if (iv <= 0)
		iv = FOLLOW_MS_DEFAULT;
	if (iv < 20)
		iv = 20;
	if (iv > 10000)
		iv = 10000;
	/* A reader closing the pipe ends us with exit 0: EPIPE, not a signal. */
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = SIG_IGN;
	sigaction(SIGPIPE, &sa, NULL);

	s = control_connect(name, 0);
	if (s < 0)
		return 1;
	for (;;)
	{
		next = wall_ms() + iv;
		rc = follow_fetch(s, &r, since, cur, &status);
		if (rc < 0)
		{
			close(s);
			s = -1;
			/* Never answered at all, or keeps failing after
			** reconnects: a dead or wedged master. */
			if (!sent || ++fails > 3)
			{
				ret = ctl_dead_exit(&r, "follow");
				break;
			}
			/* Lost mid-stream: the session ended (connect fails:
			** exit 0) or `--restart` dropped control connections
			** (reconnect and render afresh). */
			memset(&r, 0, sizeof r);
			s = control_connect(name, 1);
			if (s < 0)
				break;
			since = -1;
			continue;
		}
		if (rc == 2)
		{
			ret = follow_refused(status);
			break;
		}
		fails = 0;
		if (rc == 1)
		{
			since = (long long)cur->gen;
			if (!sent || cur->len != last->len ||
			    cur->rows != last->rows || cur->cols != last->cols ||
			    cur->crow != last->crow || cur->ccol != last->ccol ||
			    cur->cvis != last->cvis ||
			    memcmp(cur->buf, last->buf, cur->len) != 0)
			{
				printf("dch-frame %zu %ux%u %u %u %u\n", cur->len,
				       cur->rows, cur->cols, cur->crow, cur->ccol,
				       cur->cvis);
				fwrite(cur->buf, 1, cur->len, stdout);
				if (fflush(stdout) != 0 || ferror(stdout))
					break; /* reader went away: exit 0 */
				sent = 1;
				t = last;
				last = cur;
				cur = t;
			}
		}
		/* Sleep out the tick, watching stdout: a reader that closes
		** the pipe while the session is idle must still end us. Linux
		** flags a reader-less pipe with POLLERR whatever was asked;
		** macOS only answers POLLHUP to a POLLOUT poll, which would
		** spin on a writable pipe, so that one is a per-tick probe. */
		for (;;)
		{
			struct pollfd pfd = { 1, 0, 0 };
			long long left = next - wall_ms();

			if (left <= 0)
			{
				pfd.events = POLLOUT;
				left = 0;
			}
			if (poll(&pfd, 1, (int)left) > 0 &&
			    (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)))
				goto out;
			if (pfd.events)
				break;
		}
	}
out:
	if (s >= 0)
		close(s);
	free(a.buf);
	free(b.buf);
	return ret;
}

static int
do_wait_verb(const char *name, const char *pattern, int timeout_ms)
{
	unsigned char payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	size_t plen = strlen(pattern);
	int s, rc;
	struct timeval start, now;

	if (plen == 0 || plen > DCH_WAIT_MAX)
	{
		fprintf(stderr, "dch: --match must be 1..%d bytes\n",
		        DCH_WAIT_MAX);
		return 1;
	}
	s = control_connect(name, 0);
	if (s < 0)
		return 1;
	send_ctl(s, MSG_WAIT, pattern, plen);

	gettimeofday(&start, NULL);
	for (;;)
	{
		int elapsed, left;

		gettimeofday(&now, NULL);
		elapsed = (int)((now.tv_sec - start.tv_sec) * 1000 +
		                (now.tv_usec - start.tv_usec) / 1000);
		left = timeout_ms - elapsed;
		if (left <= 0)
		{
			close(s);
			fprintf(stderr, "dch: wait timed out after %dms\n",
			        timeout_ms);
			return 2;
		}
		rc = ctl_next_frame(s, &r, left, &type, payload, &len);
		if (rc == 0)
			continue; /* loop re-checks the wall clock */
		if (rc < 0)
		{
			close(s);
			return ctl_dead_exit(&r, "wait");
		}
		if (type == MSG_WAIT_HIT)
		{
			fwrite(payload, 1, len, stdout);
			fputc('\n', stdout);
			close(s);
			return 0;
		}
		if (type == MSG_READ_END)
		{
			int e = ctl_status_exit(len);

			close(s);
			return e ? e : 1; /* a status frame here is never a hit */
		}
	}
}

/* Legacy fallback encoding for --keys against a mirror-less session: a
** fixed xterm-default table, blind to kitty/application modes. */
static int
legacy_keys(const char *combo, char *out, size_t outsz)
{
	static const struct { const char *name; const char *seq; } tab[] = {
		{"enter", "\r"}, {"esc", "\x1b"}, {"escape", "\x1b"},
		{"tab", "\t"}, {"space", " "}, {"backspace", "\x7f"},
		{"up", "\x1b[A"}, {"down", "\x1b[B"}, {"right", "\x1b[C"},
		{"left", "\x1b[D"}, {"home", "\x1b[H"}, {"end", "\x1b[F"},
		{"pgup", "\x1b[5~"}, {"pgdn", "\x1b[6~"},
		{"delete", "\x1b[3~"}, {"insert", "\x1b[2~"},
		{"f1", "\x1bOP"}, {"f2", "\x1bOQ"}, {"f3", "\x1bOR"},
		{"f4", "\x1bOS"}, {"f5", "\x1b[15~"}, {"f6", "\x1b[17~"},
		{"f7", "\x1b[18~"}, {"f8", "\x1b[19~"}, {"f9", "\x1b[20~"},
		{"f10", "\x1b[21~"}, {"f11", "\x1b[23~"}, {"f12", "\x1b[24~"},
	};
	size_t k;
	int alt = 0, ctrl = 0;
	char pos = 0;

	for (;;)
	{
		if (strncasecmp(combo, "ctrl+", 5) == 0)
		{
			ctrl = 1;
			combo += 5;
		}
		else if (strncasecmp(combo, "alt+", 4) == 0)
		{
			alt = 1;
			combo += 4;
		}
		else if (strncasecmp(combo, "shift+", 6) == 0)
			combo += 6; /* legacy table can't encode shift chords */
		else
			break;
	}
	if (ctrl && combo[0] && !combo[1])
	{
		char c = combo[0];
		if (c >= 'A' && c <= 'Z')
			c += 32;
		if (c >= 'a' && c <= 'z')
			pos = c & 037;
		else if (c == '[')
			pos = 0x1b;
		else
			return -1;
		snprintf(out, outsz, "%s%c", alt ? "\x1b" : "", pos);
		return 0;
	}
	if (combo[0] && !combo[1])
	{
		snprintf(out, outsz, "%s%c", alt ? "\x1b" : "", combo[0]);
		return 0;
	}
	for (k = 0; k < sizeof(tab) / sizeof(tab[0]); k++)
		if (strcasecmp(tab[k].name, combo) == 0)
		{
			snprintf(out, outsz, "%s%s", alt ? "\x1b" : "",
			         tab[k].seq);
			return 0;
		}
	return -1;
}

/* Ask the master to re-exec itself onto the current binary, in place. The ACK
** arrives on this very socket from the NEW image (the fd survives execvp), so
** an OK here means the new binary is serving; DCH_ST_ERR means the exec failed
** and the OLD one rolled back and is still serving. Either way the session,
** its child process and its scrollback are untouched. */
static int
do_restart_verb(const char *name, int no_version, int quiet)
{
	unsigned char payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	int s, rc;

	/* Returns 0 restarted, 1 tried and failed, 2 nothing answering (a dead
	** socket), 3 sidecar missing (age unknown, treated as not restartable
	** this run). --all treats 2 and 3 as "not this command's problem" — 3 is
	** the state of every session on the first upgrade to a dch that has
	** live restart, or any session whose sidecar tmp_cleaner has removed. */
	s = control_connect(name, quiet);
	if (s < 0)
		return 2;
	/* No version sidecar is UNKNOWN age, not proven old: macOS's
	** tmp_cleaner deletes idle sidecars from /tmp after 3 days (plain
	** files, unlike the socket node) even off a live, current master,
	** and the master's self-heal tick (default hourly, DCH_HEAL_MS
	** override) re-stamps a missing .ver on its own. Say so instead of
	** waiting out the idle timeout for silence. */
	if (no_version)
	{
		close(s);
		fprintf(stderr, "dch: %s: version sidecar missing — age unknown,"
		        " not\n     proven old (idle sidecars get cleaned from"
		        " /tmp; the master\n     re-stamps its own within a heal"
		        " tick). Retry shortly; if\n     restart keeps failing,"
		        " recreate the session\n", name);
		return 3;
	}
	/* First say this image takes the ring handover over (#121): a master
	** that hears no caps hands none over, as to a pre-1.43 dch. An older
	** master answers DCH_ST_UNSUP; any ACK will do. */
	{
		unsigned char caps = DCH_RESTART_F_RING;

		send_ctl(s, MSG_RESTART_CAPS, &caps, 1);
		rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
		if (rc <= 0)
		{
			close(s);
			return ctl_dead_exit(&r, "restart");
		}
	}
	/* Tell the master which binary to land on: its own dch_exe was
	** resolved when the session started, and on a versioned prefix
	** (Homebrew Cellar, Nix store) that path still points at the old
	** build after an upgrade — re-execing it would report success and
	** change nothing. dch_exe here is this process's own, i.e. the new
	** one. Over PKT_MAX it is dropped; the master falls back to its own. */
	{
		size_t elen = dch_exe ? strlen(dch_exe) : 0;

		if (elen > PKT_MAX)
			elen = 0;
		send_ctl(s, MSG_RESTART, (const unsigned char *)dch_exe,
		         (unsigned int)elen);
	}

	/* CTL_IDLE_MS is generous for this: the master answers after it has
	** re-execed and re-read its whole state, which is still microseconds. */
	rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
	if (rc <= 0)
	{
		close(s);
		return ctl_dead_exit(&r, "restart");
	}
	close(s);
	if (type != MSG_ACK)
		return 1;
	if (len == DCH_ST_OK)
	{
		/* The ACK proves a new image is serving, not that it is THIS
		** version — the master may have fallen back to its own path.
		** The sidecar is written by whoever is serving now, so it is
		** the only honest answer. */
		const char *v = session_version(name);

		if (v && strcmp(v, DCH_VERSION) != 0)
		{
			fprintf(stderr, "dch: %s: restarted, but still on dch "
			        "%s (expected %s)\n", name, v, DCH_VERSION);
			return 1;
		}
		return 0;	/* master_print() says so */
	}
	if (len == DCH_ST_ERR)
	{
		fprintf(stderr, "dch: %s: re-exec failed; the session is still "
		        "running on its old master\n", name);
		return 1;
	}
	if (len == DCH_ST_BUSY)
	{
		/* The resume blob does not carry an arm or a hold. */
		fprintf(stderr, "dch: %s: a respawn is pending (armed or held);"
		        " retry once it is done, or `dch --respawn %s --cancel`"
		        "\n", name, name);
		return 1;
	}
	return ctl_status_exit(len);
}

/* MSG_RESPAWN to the master at `path`: op 'a' with cmd, 'h' (hold) or 'c'
** (cancel). The MSG_ACK status (an old master answers DCH_ST_UNSUP), -3 when
** the argv does not fit one frame, -4 when nothing listens, else -1/-2 for a
** master that went quiet mid-answer / never answered (ctl_dead_exit's two
** cases). */
static int
respawn_req(const char *path, int op, char **cmd, int ncmd)
{
	unsigned char req[PKT_MAX], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	size_t fill = 1, need = 1;
	int s, rc, k;

	req[0] = (unsigned char)op;
	for (k = 0; k < ncmd; k++)
		need += strlen(cmd[k]) + 1;
	/* One frame carries the whole argv; nothing reassembles more. */
	if (need > sizeof req)
		return -3;
	for (k = 0; k < ncmd; k++)
	{
		size_t l = strlen(cmd[k]) + 1;

		memcpy(req + fill, cmd[k], l);
		fill += l;
	}
	s = control_connect_path(path);
	if (s < 0)
		return -4;
	send_ctl(s, MSG_RESPAWN, req, fill);
	rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
	close(s);
	if (rc <= 0)
		return r.got_any ? -1 : -2;
	/* the status is a 16-bit wire field: never one of the codes above */
	return type == MSG_ACK && len <= 0xffff ? (int)len : DCH_ST_ERR;
}

/* Arm a respawn (or, on a held session, perform it now); no command = hold,
** cancel = drop the arm (and end a held session). */
static int
do_respawn_verb(const char *name, char **cmd, int ncmd, int cancel)
{
	struct ctl_reader dead = {0};
	size_t need = 1;
	int k, st;

	if (ncmd && !cmd[0][0])
	{
		fprintf(stderr, "dch: --respawn: empty command\n");
		return 1;
	}
	if (make_sock_path(name) < 0)
		return 1;
	st = respawn_req(sock_path, cancel ? 'c' : ncmd ? 'a' : 'h', cmd, ncmd);
	if (st == -3)
	{
		for (k = 0; k < ncmd; k++)
			need += strlen(cmd[k]) + 1;
		fprintf(stderr, "dch: --respawn: command is %zu bytes, the "
		        "limit is %d\n", need - 1, PKT_MAX - 1);
		return 1;
	}
	if (st == -4 && errno == EAGAIN)
	{
		fprintf(stderr, stuck_fmt, name);
		return 1;
	}
	if (st == -4)
	{
		fprintf(stderr, "dch: no session: %s\n", name);
		return 1;
	}
	if (st < 0)
	{
		dead.got_any = st == -1;
		return ctl_dead_exit(&dead, "respawn");
	}
	if (st == DCH_ST_ERR)
	{
		fprintf(stderr, "dch: %s: respawn request rejected\n", name);
		return 1;
	}
	return ctl_status_exit((unsigned int)st);
}

/* Print s[0..n) as a JSON string. Invalid UTF-8 becomes U+FFFD so the output
** always parses: the exit tail is raw pty bytes, maybe cut mid-character.
** ponytail: lead/continuation check only; overlongs past C0/C1 and encoded
** surrogates pass through. */
static void
json_bytes(const unsigned char *s, size_t n)
{
	size_t i = 0, k, l;

	putchar('"');
	while (i < n)
	{
		unsigned char c = s[i];

		if (c == '"' || c == '\\')
		{
			printf("\\%c", c);
			i++;
			continue;
		}
		if (c < 0x20 || c == 0x7f)
		{
			printf("\\u%04x", c);
			i++;
			continue;
		}
		l = c < 0x80 ? 1 : (c >= 0xc2 && c <= 0xdf) ? 2
		  : (c & 0xf0) == 0xe0 ? 3 : (c >= 0xf0 && c <= 0xf4) ? 4 : 0;
		for (k = 1; l && k < l; k++)
			if (i + k >= n || (s[i + k] & 0xc0) != 0x80)
				l = 0;
		if (!l)
		{
			fputs("\\ufffd", stdout);
			i++;
			continue;
		}
		fwrite(s + i, 1, l, stdout);
		i += l;
	}
	putchar('"');
}

/* Next NUL-terminated field of a MSG_CMD reply, or NULL when it runs out. */
static char *
cmd_field(unsigned char **p, const unsigned char *end)
{
	unsigned char *f = *p, *z = memchr(f, '\0', (size_t)(end - f));

	if (!z)
		return NULL;
	*p = z + 1;
	return (char *)f;
}

/* A parsed MSG_CMD reply. buf owns the strings; argv is malloc'd. */
struct cmd_info
{
	unsigned char *buf;
	long pid;			/* 0 while held */
	const char *state;		/* running | armed | held */
	int code, sig;			/* -1 / 0 until a child is reaped under an arm */
	int argc;
	char **argv;			/* argc entries + NULL */
	const unsigned char *tail;	/* raw output since the arm */
	size_t tail_len;
	int stat;			/* 1: the master sent MSG_CMD_STAT */
	unsigned int rows, cols, clients;	/* pty winsize, attached */
};

static void
cmd_info_free(struct cmd_info *ci)
{
	free(ci->buf);
	free(ci->argv);
	memset(ci, 0, sizeof *ci);
}

/* MSG_CMD to the master at `path`. 0 = *ci filled; a positive master status
** (DCH_ST_UNSUP from an old one); -4 nothing listens; -1/-2 the master went
** quiet mid-answer / never answered; -3 a malformed reply. `flags` are
** DCH_CMD_F_*: with DCH_CMD_F_STAT a master that knows it fills ci->stat and
** the size fields; an older one ignores the byte. */
static int
cmd_query_f(const char *path, struct cmd_info *ci, unsigned char flags)
{
	unsigned char payload[PKT_MAX], type, *buf = NULL, *q;
	const unsigned char *end;
	const char *pid, *code, *sig, *ac;
	struct ctl_reader r = {0};
	unsigned int len;
	size_t fill = 0;
	int s, rc, k;

	memset(ci, 0, sizeof *ci);
	s = control_connect_path(path);
	if (s < 0)
		return -4;
	send_ctl(s, MSG_CMD, &flags, flags ? 1 : 0);
	for (;;)
	{
		rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
		if (rc <= 0)
		{
			close(s);
			free(buf);
			return r.got_any ? -1 : -2;
		}
		if (type == MSG_CMD_STAT)
		{
			if (len == DCH_CMD_STAT_LEN)
			{
				ci->stat = 1;
				ci->rows = payload[0] | (unsigned int)payload[1] << 8;
				ci->cols = payload[2] | (unsigned int)payload[3] << 8;
				ci->clients = payload[4] |
				              (unsigned int)payload[5] << 8;
			}
			continue;
		}
		if (type != MSG_READ_DATA)
			break;
		if (!len)	/* realloc(buf, 0) may free buf and return NULL */
			continue;
		{
			unsigned char *nb = realloc(buf, fill + len);

			if (!nb)
			{
				close(s);
				free(buf);
				return -3;
			}
			buf = nb;
			memcpy(buf + fill, payload, len);
			fill += len;
		}
	}
	close(s);
	if (type != MSG_READ_END || len != DCH_ST_OK)
	{
		free(buf);
		return type == MSG_READ_END || type == MSG_ACK ? (int)len
		                                               : DCH_ST_ERR;
	}

	q = buf;
	end = buf + fill;
	pid = buf ? cmd_field(&q, end) : NULL;
	ci->state = pid ? cmd_field(&q, end) : NULL;
	code = ci->state ? cmd_field(&q, end) : NULL;
	sig = code ? cmd_field(&q, end) : NULL;
	ac = sig ? cmd_field(&q, end) : NULL;
	ci->buf = buf;
	if (!ac || atoi(ac) < 0 ||
	    !(ci->argv = calloc((size_t)atoi(ac) + 1, sizeof(char *))))
	{
		cmd_info_free(ci);
		return -3;
	}
	ci->pid = atol(pid);
	ci->code = atoi(code);
	ci->sig = atoi(sig);
	for (k = 0; k < atoi(ac); k++)
		if (!(ci->argv[k] = cmd_field(&q, end)))
		{
			cmd_info_free(ci);
			return -3;
		}
	ci->argc = k;
	ci->tail = q;
	ci->tail_len = (size_t)(end - q);
	return 0;
}

static int
cmd_query(const char *path, struct cmd_info *ci)
{
	return cmd_query_f(path, ci, 0);
}

/* `dch --cmd`: what the session is running right now, as one JSON object:
** {"pid","state","argv","exit_code","exit_signal","tail"}. pid is null while
** held; exit_* are null until a child is reaped under an arm. */
static int
do_cmd_verb(const char *name)
{
	struct cmd_info ci;
	struct ctl_reader dead = {0};
	int k, st;

	if (make_sock_path(name) < 0)
		return 1;
	st = cmd_query(sock_path, &ci);
	if (st == -4 && errno == EAGAIN)
	{
		fprintf(stderr, stuck_fmt, name);
		return 1;
	}
	if (st == -4)
	{
		fprintf(stderr, "dch: no session: %s\n", name);
		return 1;
	}
	if (st == -1 || st == -2)
	{
		dead.got_any = st == -1;
		return ctl_dead_exit(&dead, "cmd");
	}
	if (st == -3)
	{
		fprintf(stderr, "dch: malformed cmd response\n");
		return 1;
	}
	if (st > 0)
		return ctl_status_exit((unsigned int)st);
	if (ci.pid > 0)
		printf("{\"pid\":%ld,\"state\":", ci.pid);
	else
		fputs("{\"pid\":null,\"state\":", stdout);
	json_bytes((const unsigned char *)ci.state, strlen(ci.state));
	fputs(",\"argv\":[", stdout);
	for (k = 0; k < ci.argc; k++)
	{
		if (k)
			putchar(',');
		json_bytes((const unsigned char *)ci.argv[k],
		           strlen(ci.argv[k]));
	}
	fputs("],\"exit_code\":", stdout);
	if (ci.code >= 0)
		printf("%d", ci.code);
	else
		fputs("null", stdout);
	fputs(",\"exit_signal\":", stdout);
	if (ci.sig > 0)
		printf("%d", ci.sig);
	else
		fputs("null", stdout);
	fputs(",\"tail\":", stdout);
	json_bytes(ci.tail, ci.tail_len);
	puts("}");
	cmd_info_free(&ci);
	return 0;
}

static int
do_keys_verb(const char *name, char **combos, int ncombos)
{
	unsigned char spec[PKT_MAX], payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	unsigned int len;
	size_t fill = 0;
	int s, rc, k;

	if (ncombos <= 0)
	{
		fprintf(stderr, "dch: --keys needs at least one key\n");
		return 1;
	}
	for (k = 0; k < ncombos; k++)
	{
		size_t l = strlen(combos[k]);
		if (fill + l + 1 > sizeof(spec))
		{
			fprintf(stderr, "dch: too many keys in one call\n");
			return 1;
		}
		memcpy(spec + fill, combos[k], l);
		fill += l;
		spec[fill++] = '\0'; /* NUL-separated wire format */
	}

	s = control_connect(name, 0);
	if (s < 0)
		return 1;
	send_ctl(s, MSG_KEYS, spec, fill - 1); /* drop trailing NUL */

	rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
	if (rc <= 0)
	{
		close(s);
		return ctl_dead_exit(&r, "keys");
	}
	if (type != MSG_ACK)
	{
		close(s);
		return 1;
	}
	if (len == DCH_ST_NOVT || len == DCH_ST_ERR)
	{
		/* Mirror-less session: fall back to a fixed legacy table so
		** the common chords still land; warn that mode-aware apps
		** (kitty protocol, application cursor keys) may see the
		** wrong encoding. */
		char one[16];
		char bytes[PKT_MAX];
		size_t bfill = 0;

		for (k = 0; k < ncombos; k++)
		{
			if (legacy_keys(combos[k], one, sizeof(one)) < 0)
			{
				fprintf(stderr, "dch: unknown key: %s\n",
				        combos[k]);
				close(s);
				return 1;
			}
			size_t l = strlen(one);
			if (bfill + l > sizeof(bytes))
				break;
			memcpy(bytes + bfill, one, l);
			bfill += l;
		}
		send_ctl(s, MSG_PUSH, bytes, bfill);
		fprintf(stderr, "dch: session has no terminal mirror; sent "
		        "legacy key encoding (mode-aware apps may "
		        "misread modified keys)\n");
		close(s);
		return 0;
	}
	close(s);
	if (len != DCH_ST_OK)
		fprintf(stderr, "dch: unknown key in: %s...\n", combos[0]);
	return len == DCH_ST_OK ? 0 : 1;
}

static int
do_send_verb(const char *name, char **words, int nwords, int press_enter)
{
	int s, k;

	if (nwords == 0 && !press_enter)
	{
		fprintf(stderr, "dch: --send needs text\n");
		return 1;
	}
	s = control_connect(name, 0);
	if (s < 0)
		return 1;
	for (k = 0; k < nwords; k++)
	{
		const char *w = words[k];
		size_t l = strlen(w), off;

		for (off = 0; off < l; off += PKT_MAX)
		{
			size_t chunk = l - off > PKT_MAX ? PKT_MAX : l - off;
			send_ctl(s, MSG_PUSH, w + off, chunk);
		}
		if (k + 1 < nwords)
			send_ctl(s, MSG_PUSH, " ", 1);
	}
	if (press_enter)
		send_ctl(s, MSG_PUSH, "\r", 1);
	close(s);
	return 0;
}

/* --spawn --env KEY=VAL collected from argv (strings persist for putenv). */
#define SPAWN_ENV_MAX 32
static char *spawn_env[SPAWN_ENV_MAX];
static int spawn_env_n;

static int
do_spawn_verb(char *exe, const char *name, int cols, int rows,
              char **inner_argv, int inner_argc, int quiet)
{
	unsigned char payload[PKT_MAX], type;
	struct ctl_reader r = {0};
	struct winsize ws;
	unsigned char req[4] = { DCH_READ_PLAIN, DCH_READ_VISIBLE, 0, 0 };
	unsigned int len;
	struct packet pkt;
	char *default_argv[] = { NULL, NULL };
	struct stat st;
	int s, rc;

	if (refuse_id_name(name))
		return 2;
	if (make_sock_path(name) < 0)
		return 1;
	if (stat(sock_path, &st) == 0 && S_ISSOCK(st.st_mode))
	{
		/* Live master answers a connect; refuse to clobber it. A
		** stale socket (dead master) just gets unlinked below. */
		int probe = control_connect(name, 1);
		if (probe < 0 && errno == EAGAIN)
			return 1;	/* stuck, not dead: never unlinked (#265) */
		if (probe >= 0)
		{
			close(probe);
			fprintf(stderr, "dch: session already exists: %s\n",
			        name);
			return 1;
		}
	}
	unlink(sock_path);

	if (inner_argc == 0)
	{
		char *sh = getenv("SHELL");

		default_argv[0] = sh && *sh ? sh : "/bin/sh";
		inner_argv = default_argv;
	}
	/* Caller env first; DCH_SESSION is force-set after, so --env cannot
	** spoof session identity. (TERM below is only defaulted when absent
	** or dumb — a caller-supplied TERM legitimately wins.) */
	strip_caller_agent();
	for (rc = 0; rc < spawn_env_n; rc++)
		putenv(spawn_env[rc]);
	setenv("DCH_SESSION", name, 1);
	/* Headless spawns often run where TERM is unset or "dumb" (cron, CI,
	** daemons); the mirror is xterm-compatible, so give TUIs a terminal
	** they will actually draw on. */
	{
		const char *term = getenv("TERM");

		if (!term || !*term || strcmp(term, "dumb") == 0)
			setenv("TERM", "xterm-256color", 1);
	}
	{
		char sz[32];

		snprintf(sz, sizeof sz, "%dx%d", cols, rows);
		setenv("DCH_SPAWN_SIZE", sz, 1);
	}
	rc = spawn_master(exe, name, sock_path, inner_argv);
	unsetenv("DCH_SPAWN_SIZE");
	if (rc < 0)
	{
		fprintf(stderr, "dch: failed to spawn session\n");
		return 1;
	}

	s = control_connect(name, 0);
	if (s < 0)
		return 1;

	/* Give the child a real grid immediately (headless: no attacher will
	** send a WINCH later). */
	memset(&ws, 0, sizeof(ws));
	ws.ws_col = cols;
	ws.ws_row = rows;
	memset(&pkt, 0, sizeof(pkt));
	pkt.type = MSG_WINCH;
	memcpy(&pkt.u.ws, &ws, sizeof(ws));
	write_packet_or_fail(s, &pkt);

	/* Throwaway read: a control verb opens the -w pty gate, so the
	** child's startup output flows into the mirror instead of blocking
	** on a full kernel pty buffer. The response doubles as "master is
	** alive and answering". */
	send_ctl(s, MSG_READ, req, sizeof(req));
	do
		rc = ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len);
	while (rc == 1 && type == MSG_READ_DATA);
	close(s);

	/* No READ_END is not a failure: spawn_master saw the program exec,
	** and a master gone before it answered means it already exited
	** (`--spawn s true`). Its end is in the history record. */
	if (!quiet)
		printf("%s\n", name);
	return 0;
}

/* ---- agent-aware --restart --all (Claude Code) ----------------------
**
** After the master pass, `--restart --all` restarts the Claude Code agents
** that run AS a session's command once the installed CLI differs from the
** running one (-f: all of them). Per agent: type a pause message, wait until
** it is provably idle, arm a #021 hold and type /exit, read the session id
** from the `claude --resume <id>` line in the exit tail, respawn the same
** argv with --resume <id>, wait for the new composer, type a resume message.
** Every keystroke goes through a composer gate that wants the empty composer
** on screen — Enter on a permission prompt would approve a tool call — so
** the whole step needs the terminal mirror and dch-lite skips it. */

/* First regular, executable <dir>/<name> along path, the way execvp()
** searches it: an empty component means cwd. Leaves the hit in out and
** returns 0, or returns -1 with out empty. Shared with the master's record. */
int
path_lookup(const char *name, const char *path, const char *cwd,
            char *out, size_t outsz)
{
	const char *dir = path, *e;
	struct stat st;

	for (; dir; dir = e ? e + 1 : NULL)
	{
		int l;

		e = strchr(dir, ':');
		l = e ? (int)(e - dir) : (int)strlen(dir);
		snprintf(out, outsz, "%.*s/%s", l ? l : (int)strlen(cwd),
		         l ? dir : cwd, name);
		if (stat(out, &st) == 0 && S_ISREG(st.st_mode) &&
		    access(out, X_OK) == 0)
			return 0;
	}
	out[0] = '\0';
	return -1;
}

/* One Claude session record (~/.claude/sessions/<pid>.json). */
struct crec
{
	long pid, upd;		/* upd = statusUpdatedAt, epoch ms */
	int depth;		/* generations below the scan root */
	char sid[64], ver[64], status[32], cwd[1024], name[256];
	char rec[1400];		/* the record's path */
};

struct crec_scan
{
	long root;
	int verify, n;		/* n = live Claudes under root, as joined */
	int all;		/* every live record under root */
	long pid[16];		/* the first 16 of them */
	struct crec best;	/* the shallowest of them */
};

static long
env_ms(const char *key, long dflt)
{
	const char *e = getenv(key);
	long v = e ? atol(e) : 0;

	return v > 0 ? v : dflt;
}

/* Parent pid from the kernel's process table — never the env, which macOS
** redacts for platform binaries. -1 when the process is gone. With tpgid,
** also the foreground process group of its controlling terminal (<= 0:
** none). */
static long
proc_stat(long pid, long *tpgid)
{
#ifdef __APPLE__
	struct kinfo_proc kp;
	size_t len = sizeof kp;
	int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PID, (int)pid };

	if (sysctl(mib, 4, &kp, &len, NULL, 0) != 0 || len == 0)
		return -1;
	if (tpgid)
		*tpgid = kp.kp_eproc.e_tpgid;
	return kp.kp_eproc.e_ppid;
#else
	char p[64], buf[512], *rp;
	FILE *f;
	long pp = -1, tp = -1;

	snprintf(p, sizeof p, "/proc/%ld/stat", pid);
	if (!(f = fopen(p, "r")))
		return -1;
	/* the comm field may hold spaces and parens: parse after the last ')' */
	if (fgets(buf, sizeof buf, f) && (rp = strrchr(buf, ')')))
		sscanf(rp + 1, " %*c %ld %*d %*d %*d %ld", &pp, &tp);
	fclose(f);
	if (tpgid)
		*tpgid = tp;
	return pp;
#endif
}

static long
proc_ppid(long pid)
{
	return proc_stat(pid, NULL);
}

/* 1 when the Claude at `pid` is in the foreground process group of the
** terminal that `root` (a session's child, which the master made that
** pty's controlling process) controls, 0 when it is not — suspended with
** Ctrl-Z or in the background, a shell or another job in front (#282) —
** -1 when either cannot be read. The kernel's own view, so an old master
** answers it as well as a new one. */
static int
proc_fg(long root, long pid)
{
	long fg = -1;
	pid_t g = getpgid((pid_t)pid);

	if (proc_stat(root, &fg) < 0 || fg <= 0 || g < 0)
		return -1;
	return g == (pid_t)fg;
}

/* Generations from `anc` down to `pid` (0 = the same process), -1 when pid
** does not descend from it. */
static int
proc_depth(long pid, long anc)
{
	int d;

	for (d = 0; d < 64 && pid > 1; d++)
	{
		if (pid == anc)
			return d;
		pid = proc_ppid(pid);
	}
	return -1;
}

/* dch_visit_claude_records callback: count the live interactive records
** whose pid descends from cs->root, keep the shallowest. With verify, the
** record's procStart must match the process's start time, so a record left
** by a dead Claude whose pid was recycled never joins. Claudes below
** another (a test's fake Claude under a supervisor's tool call, #253) count
** as that one: n is 1 while the shallowest is the ancestor of all. */
static int
crec_visit(const char *path, const char *body, size_t len, void *arg)
{
	struct crec_scan *cs = arg;
	struct crec c;
	const char *base = strrchr(path, '/');
	char kind[32], start[128], actual[128], *endp;
	long jpid;
	int i, self, nested;

	memset(&c, 0, sizeof c);
	base = base ? base + 1 : path;
	c.pid = strtol(base, &endp, 10);
	/* len 0, a record caught mid-rewrite (visit_records_in): its live
	** pid's Claude, sid "" (#274) */
	if (c.pid <= 0 || strcmp(endp, ".json") ||
	    (len && (dch_json_long(body, "pid", &jpid) < 0 || jpid != c.pid ||
	             dch_json_string(body, "kind", kind, sizeof kind) < 0 ||
	             strcmp(kind, "interactive"))) ||
	    kill((pid_t)c.pid, 0) < 0 ||
	    (c.depth = proc_depth(c.pid, cs->root)) < 0 ||
	    (len && (dch_json_string(body, "sessionId", c.sid,
	                             sizeof c.sid) < 0 ||
	             dch_json_string(body, "version", c.ver, sizeof c.ver) < 0 ||
	             dch_json_string(body, "status", c.status,
	                             sizeof c.status) < 0)))
		return 0;
	if (dch_json_string(body, "cwd", c.cwd, sizeof c.cwd) < 0)
		c.cwd[0] = '\0';
	if (dch_json_string(body, "name", c.name, sizeof c.name) < 0)
		c.name[0] = '\0';
	if (dch_json_long(body, "statusUpdatedAt", &c.upd) < 0)
		c.upd = 0;
	if (cs->verify && len &&
	    (dch_json_string(body, "procStart", start, sizeof start) < 0 ||
	     dch_process_start(c.pid, actual, sizeof actual) < 0 ||
	     strcmp(start, actual)))
		return 0;
	snprintf(c.rec, sizeof c.rec, "%s", path);
	if (cs->all < 16)
		cs->pid[cs->all] = c.pid;
	if (cs->all++ == 0 || c.depth < cs->best.depth)
		cs->best = c;
	/* over all of them, every time: records come in readdir order. The
	** best's pid twice (two config dirs) is two answers, not a nesting. */
	for (i = self = 0, nested = cs->all <= 16; nested && i < cs->all &&
	     i < 16; i++)
		nested = cs->pid[i] == cs->best.pid ? ++self == 1
		         : proc_depth(cs->pid[i], cs->best.pid) > 0;
	cs->n = nested ? 1 : cs->all;
	return 0;
}

/* 1 opens a batch of proc_tree calls that share one process snapshot (the
** first call takes it, 2 then reuses it); 0 closes the batch. */
static int proc_tree_keep;

/* `root` and its descendants, breadth first, at most max: the pids. */
static int
proc_tree(long root, long *out, int max)
{
	int n = 1, i;
#ifdef __APPLE__
	int kids[512], c, k;

	out[0] = root;
	for (i = 0; i < n && n < max; i++)
	{
		c = proc_listpids(PROC_PPID_ONLY, (uint32_t)out[i], kids,
		                  sizeof kids) / (int)sizeof *kids;
		for (k = 0; k < c && n < max; k++)
			if (kids[k] > 0)
				out[n++] = kids[k];
	}
#else
	/* A pass reads every process's stat; in a batch (one --ls-json, a tree
	** per row) the first pass serves the rest. */
	static long pid[4096], pp[4096];
	static int np;
	int k;
	DIR *d;
	struct dirent *de;
	char *end;

	out[0] = root;
	if (proc_tree_keep != 2)
	{
		np = 0;
		if ((d = opendir("/proc")))
		{
			while (np < 4096 && (de = readdir(d)))
				if ((pid[np] = strtol(de->d_name, &end, 10)) > 0 &&
				    !*end)
					pp[np] = proc_ppid(pid[np]), np++;
			closedir(d);
		}
		if (proc_tree_keep)
			proc_tree_keep = 2;
	}
	for (i = 0; i < n && n < max; i++)
		for (k = 0; k < np && n < max; k++)
			if (pp[k] == out[i])
				out[n++] = pid[k];
#endif
	return n;
}

/* The sessions dirs to search for Claudes at or below `root`, added to
** dirs[nd..max) once each: ours first, then each process's in the tree,
** which may run under its own CLAUDE_CONFIG_DIR or HOME
** (`CLAUDE_CONFIG_DIR=~/.claude-work claude`, #062). Once by inode, not by
** string: `~/.claude/` and `~/.claude` read twice would count every record
** twice. The new count. */
static int
claude_dirs(long root, char (*dirs)[1100], int nd, int max)
{
	struct stat st[16];
	long tree[64];
	int n = proc_tree(root, tree, 64), i, j;

	for (j = 0; j < nd && j < 16; j++)
		if (stat(dirs[j], &st[j]) < 0)
			memset(&st[j], 0, sizeof st[j]);
	for (i = -1; i < n && nd < max && nd < 16; i++)
	{
		if (claude_sessions_dir(i < 0 ? 0 : tree[i], dirs[nd],
		                        sizeof dirs[nd]) < 0 ||
		    stat(dirs[nd], &st[nd]) < 0 || !S_ISDIR(st[nd].st_mode))
			continue;
		for (j = 0; j < nd && (st[j].st_dev != st[nd].st_dev ||
		                       st[j].st_ino != st[nd].st_ino); j++)
			;
		if (j == nd)
			nd++;
	}
	return nd;
}

/* Live Claudes at or below `root`, those below another counted as it (see
** crec_visit); *best = the shallowest. */
static int
claude_find(long root, int verify, struct crec *best)
{
	struct crec_scan cs;
	char dirs[8][1100];
	int nd = claude_dirs(root, dirs, 0, 8), i;

	memset(&cs, 0, sizeof cs);
	cs.root = root;
	cs.verify = verify;
	for (i = 0; i < nd; i++)
		(void)visit_records_in(dirs[i], crec_visit, &cs);
	*best = cs.best;
	return cs.n;
}

/* --ls-json's Claude join (#079): one crec_scan per row, each record read
** once for all of them. */
struct crec_rows
{
	struct crec_scan *v;
	int n;
};

static int
crec_visit_rows(const char *path, const char *body, size_t len, void *arg)
{
	struct crec_rows *m = arg;
	int i;

	for (i = 0; i < m->n; i++)
		if (m->v[i].root > 0)
			(void)crec_visit(path, body, len, &m->v[i]);
	return 0;
}

/* A canonical UUID: 8-4-4-4-12 hex. A sessionId comes from a file and goes
** into a path. */
static int
uuid_shaped(const char *s)
{
	int k;

	for (k = 0; k < 36; k++)
		if (k == 8 || k == 13 || k == 18 || k == 23 ? s[k] != '-'
		                                            : !isxdigit((unsigned char)s[k]))
			return 0;
	return s[36] == '\0';
}

/* The transcript of the Claude whose record is c: the one regular
** <config>/projects/<any>/<sid>.jsonl, <config> being the record's
** <config>/sessions/<pid>.json less two components. 0 and out filled, -1
** when there is none or more than one: no guessing between them. */
static int
claude_transcript(const struct crec *c, char *out, size_t outsz)
{
	char cfg[1400], dir[1500], p[2000];
	struct dirent *de;
	struct stat st;
	DIR *d;
	char *s;
	int k, hits = 0;

	snprintf(cfg, sizeof cfg, "%s", c->rec);
	for (k = 0; k < 2 && (s = strrchr(cfg, '/')); k++)
		*s = '\0';
	if (k < 2 || cfg[0] != '/' || !uuid_shaped(c->sid) ||
	    snprintf(dir, sizeof dir, "%s/projects", cfg) >= (int)sizeof dir ||
	    !(d = opendir(dir)))
		return -1;
	while ((de = readdir(d)) && hits < 2)
	{
		if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..") ||
		    snprintf(p, sizeof p, "%s/%s/%s.jsonl", dir, de->d_name,
		             c->sid) >= (int)sizeof p ||
		    stat(p, &st) < 0 || !S_ISREG(st.st_mode))
			continue;
		if (hits++ == 0 && snprintf(out, outsz, "%s", p) >= (int)outsz)
			hits = 2;	/* cut short: no path beats a wrong one */
	}
	closedir(d);
	return hits == 1 ? 0 : -1;
}

/* claude's arguments (after argv[0]) say it never opens a composer:
** -p/--print before any `--`, or a subcommand first (`claude mcp serve`,
** `claude update`). By argv, not by a record kind: crec_visit takes only
** "interactive" records, so a batch run never has one to find, and a
** record that cannot be read is the very case this decides (#067). */
static int
claude_batch(char **v)
{
	static const char *const sub[] = {
		"mcp", "plugin", "plugins", "update", "upgrade", "doctor",
		"install", "setup-token", "migrate-installer", "config", "auth",
	};
	int k;

	for (k = 0; v[k] && strcmp(v[k], "--") != 0; k++)
		if (strcmp(v[k], "-p") == 0 || strcmp(v[k], "--print") == 0)
			return 1;
	for (k = 0; v[0] && k < (int)(sizeof sub / sizeof sub[0]); k++)
		if (strcmp(v[0], sub[k]) == 0)
			return 1;
	return 0;
}

/* A process at or below `root` that is plainly an interactive Claude Code
** — argv[0]'s basename is `claude` (a #! script's interpreter dropped),
** not a batch run (claude_batch) — whether or not its record can be found.
** Its pid, or 0. */
static long
claude_proc(long root)
{
	long tree[64];
	int n = proc_tree(root, tree, 64), i, hit;

	for (i = 0; i < n; i++)
	{
		char **v = proc_argv(tree[i]);

		hit = v && dch_claude_tok(v, 1) == 0 && !claude_batch(v + 1);
		free(v);
		if (hit)
			return tree[i];
	}
	return 0;
}

/* argv for the relaunch: up to the claude token, then --resume <id>, then
** the rest minus any earlier session choice (--resume/-r [id], --resume=,
** --session-id, --continue/-c, --fork-session) and minus the one-shot
** prompt, which was the first message only (#138). Inserted after the
** token, not appended: a trailing `-- <prompt>` would swallow it.
** --resume/-r take an OPTIONAL value, so only a non-flag next token is
** theirs. */
static char **
resume_argv(char **v, int argc, int tok, char *id, int *outc)
{
	char **o = calloc((size_t)argc + 3, sizeof(char *));
	char **s = calloc((size_t)argc + 1, sizeof(char *));
	int k, n = 0, opts = 1;

	if (!o || !s)
	{
		free(o);
		free(s);
		return NULL;
	}
	/* only entries past the claude token go: tok still indexes it */
	argc = dch_claude_strip(v, argc, s, 1);
	v = s;
	for (k = 0; k <= tok; k++)
		o[n++] = v[k];
	o[n++] = "--resume";
	o[n++] = id;
	for (k = tok + 1; k < argc; k++)
	{
		const char *a = v[k];

		if (opts && strcmp(a, "--") == 0)
			opts = 0;
		else if (opts && (strcmp(a, "--resume") == 0 ||
		                  strcmp(a, "-r") == 0))
		{
			if (k + 1 < argc && v[k + 1][0] != '-')
				k++;
			continue;
		}
		else if (opts && strcmp(a, "--session-id") == 0)
		{
			if (k + 1 < argc)
				k++;
			continue;
		}
		else if (opts && (strncmp(a, "--resume=", 9) == 0 ||
		                  strncmp(a, "--session-id=", 13) == 0 ||
		                  strcmp(a, "--continue") == 0 ||
		                  strcmp(a, "-c") == 0 ||
		                  strcmp(a, "--fork-session") == 0))
			continue;
		o[n++] = v[k];
	}
	free(s);
	*outc = n;
	return o;
}

/* ---- session history (#042) ----------------------------------------- */

/* Where the masters keep one record per session:
** $XDG_STATE_HOME/dch/sessions/<slug>, else ~/.local/state/dch/sessions/<slug>
** — not the socket dir, the records must outlive a reboot. <slug> is the
** socket dir with '/' as '-' (/tmp/dch-502 -> -tmp-dch-502): a test run
** with its own runtime dir must never write, or revive, the real dir's
** sessions. sockdir NULL = the sessions/ root; a relative sockdir keeps no
** records (the slug would depend on the cwd). create = make what is
** missing, 0700. Like the socket dir it must be ours and closed to others:
** a planted record would be revived as a command. 0 ok, -1 not usable. */
int
dch_history_dir(char *out, size_t outsz, const char *sockdir, int create)
{
	const char *x = getenv("XDG_STATE_HOME"), *h = getenv("HOME");
	struct stat st;
	char *p;
	int n;

	if (sockdir && *sockdir != '/')
		return -1;
	if (x && *x == '/')
		n = snprintf(out, outsz, "%s/dch/sessions/", x);
	else if (h && *h)
		n = snprintf(out, outsz, "%s/.local/state/dch/sessions/", h);
	else
		return -1;
	if (n < 0 || (size_t)n >= outsz ||
	    snprintf(out + n, outsz - (size_t)n, "%s", sockdir ? sockdir : "") >=
	        (int)(outsz - (size_t)n))
		return -1;
	for (p = out + n; *p; p++)
		if (*p == '/')
			*p = '-';
	if (!sockdir)
		out[n - 1] = '\0';
	for (p = out; create && p; )
	{
		p = strchr(p + 1, '/');
		if (p)
			*p = '\0';
		if (mkdir(out, 0700) < 0 && errno != EEXIST &&
		    (stat(out, &st) < 0 || !S_ISDIR(st.st_mode)))
			create = 0;
		if (p)
			*p = '/';
	}
	return lstat(out, &st) == 0 && S_ISDIR(st.st_mode) &&
	       st.st_uid == getuid() && !(st.st_mode & 022) ? 0 : -1;
}

/* The Claude Code agent under a session's child, for the master's record:
** the shallowest live claude below `child`, and its launch argv — the
** outermost process on the way down to it, below the child: what was typed
** into a shell (`headroom wrap claude --chrome`), else claude itself. 0 and
** a filled *a (a->launch malloc'd), or -1 when there is none. */
int
dch_agent_probe(long child, struct dch_agent *a)
{
	struct crec r;
	long p, pp;
	const char *h = getenv("HOME");
	char def[1100], *s;
	int k;

	memset(a, 0, sizeof *a);
	/* no sid: its record caught mid-rewrite, the caller keeps what it
	** had (#274) */
	if (child <= 0 || claude_find(child, 1, &r) == 0 || !r.sid[0])
		return -1;
	for (p = r.pid; p != child && (pp = proc_ppid(p)) > 1 && pp != child;
	     p = pp)
		;
	a->pid = r.pid;
	snprintf(a->sid, sizeof a->sid, "%s", r.sid);
	snprintf(a->name, sizeof a->name, "%s", r.name);
	snprintf(a->cwd, sizeof a->cwd, "%s", r.cwd);
	snprintf(a->rec, sizeof a->rec, "%s", r.rec);
	/* Its config dir is the record's <dir>/sessions/<pid>.json less two
	** components, kept when it is not our ~/.claude: its own
	** CLAUDE_CONFIG_DIR or HOME, for --revive to resume under (#065). */
	snprintf(a->cfg, sizeof a->cfg, "%s", r.rec);
	for (k = 0; k < 2 && (s = strrchr(a->cfg, '/')); k++)
		*s = '\0';
	if (h && *h && snprintf(def, sizeof def, "%s/.claude", h) <
	    (int)sizeof def && strcmp(def, a->cfg) == 0)
		a->cfg[0] = '\0';
	a->launch = proc_argv(p);
	return 0;
}

/* The shallowest live Codex below (or at) `child`: the agent whose corpus
** is its rollout (7.4), so the ring pauses for it as for a Claude (7.3).
** 0 none. */
long
dch_codex_below(long child)
{
	long pids[64];
	int n, k;

	if (child <= 0)
		return 0;
	n = proc_tree(child, pids, 64);
	for (k = 0; k < n; k++)
		if (proc_is_codex(pids[k]))
			return pids[k];
	return 0;
}

/* ---- --stop: pause, /exit, end the session (#046) --------------------
**
** `--stop <name>...|--all` ends sessions for --revive to bring back: the
** session's Claude Code is paused the way --restart --include-agents does
** it. As the session's command it is /exit'ed, its resume id read from the
** exit line; typed into the shell it is never /exit'ed (the shell would run
** what followed it on the line), its id read from its session record. Then
** the session ends as `dch -k` ends one, but marked <sock>.stopped, holding
** the agent: the history record ends "stopped" with that agent. A session
** without a live Claude ends at once. */

static void
put_json_str(const char *s)
{
	size_t cap = strlen(s) * 6 + 3;
	char *q = malloc(cap);

	fputs(q && dch_json_quote(q, cap, s) != (size_t)-1 ? q : "\"\"", stdout);
	free(q);
}

/* --json (--stop, --restart, --revive): {"name","phase"} as a session
** reaches each step. */
static void
json_phase(const char *name, const char *phase, int json)
{
	if (!json)
		return;
	fputs("{\"name\":", stdout);
	put_json_str(name);
	printf(",\"phase\":\"%s\"}\n", phase);
	fflush(stdout);
}

/* --json's result object; xk/xv (when xk) one more string field after
** "name": --revive's "as", the master pass's "phase":"master". Left open
** for a caller that appends keys (--revive's "running"). */
static void
json_final_open(const char *name, const char *xk, const char *xv,
                const char *res, const char *code, const char *detail,
                const char *sid)
{
	fputs("{\"name\":", stdout);
	put_json_str(name);
	if (xk)
	{
		printf(",\"%s\":", xk);
		put_json_str(xv);
	}
	printf(",\"result\":\"%s\",\"reason\":\"%s\",\"detail\":", res, code);
	put_json_str(detail);
	fputs(",\"session_id\":", stdout);
	put_json_str(sid);
}

static void
json_final(const char *name, const char *xk, const char *xv,
           const char *res, const char *code, const char *detail,
           const char *sid)
{
	json_final_open(name, xk, xv, res, code, detail, sid);
	fputs("}\n", stdout);
	fflush(stdout);
}

/* A --stop that ended `name` and found no record of it (DCH_NO_HISTORY,
** a failed write) promises no --revive: its ok line says so instead (L1,
** #067). 1 when it printed that line. */
static int
stop_ok_norec(const char *name, const char *sid, int json)
{
	char dir[1024], p[1400], cb[256], d[600];
	struct stat st;

	if (dch_history_dir(dir, sizeof dir, sock_dir, 0) == 0 &&
	    snprintf(p, sizeof p, "%s/%s.json", dir,
	             canon_name(name, cb, sizeof cb)) < (int)sizeof p &&
	    lstat(p, &st) == 0)
		return 0;
	snprintf(d, sizeof d, "no session record kept, so dch --revive cannot "
	         "bring it back%s%s", *sid ? "; resume by hand: claude --resume "
	         : "", sid);
	if (json)
		json_final(name, NULL, NULL, "ok", "", d, sid);
	else
		printf("%s: stopped (%s)\n", name, d);
	fflush(stdout);
	return 1;
}

/* The one result line per session; res ok|skipped|failed|interrupted.
** Other than ok the text is `<name>: <res> — <detail>`, --restart's too. */
static void
stop_print(const char *name, const char *res, const char *code,
           const char *detail, const char *aname, const char *sid, int json)
{
	char hint[700];

	/* #162: no session takes a name starting with '-', not even back */
	if (name[0] == '-')
		snprintf(hint, sizeof hint, "a name starting with '-' is not "
		         "revived");
	else
		snprintf(hint, sizeof hint, "revive with dch --revive %s", name);
	if (json)
		json_final(name, NULL, NULL, res, code, detail, sid);
	else if (strcmp(res, "ok") != 0)
		/* the id, once known, on every line: it may be all that is left */
		printf("%s: %s — %s%s%s%s\n", name, res, detail,
		       *sid && !strstr(detail, sid) ? " (claude " : "",
		       *sid && !strstr(detail, sid) ? sid : "",
		       *sid && !strstr(detail, sid) ? ")" : "");
	else
		printf("%s: stopped (%s%s%s%s%s%s)%s%s\n",
		       name, *sid ? "claude " : "", aname, *sid && *aname ? " " : "",
		       sid, *sid ? "; " : "", hint, *detail ? " " : "", detail);
	fflush(stdout);
}

/* <path>.stopped: the agent object (ag), or empty; ag NULL and drop =
** remove it. From the moment it is there the master's record keeps that
** agent. 0, or -1. */
static int
stop_mark(const char *path, const struct dch_agent *ag, int drop)
{
	char mk[1200], tmp[1300];
	FILE *f = NULL;
	int fd, ok;

	snprintf(mk, sizeof mk, "%s.stopped", path);
	if (drop)
		return unlink(mk);
	snprintf(tmp, sizeof tmp, "%s.%d", mk, (int)getpid());
	fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
	if (fd >= 0 && !(f = fdopen(fd, "w")))
		close(fd);
	ok = f != NULL;
	if (f && ag)
		dch_agent_json(f, ag);
	if (f && fclose(f) != 0)
		ok = 0;
	if (!ok || rename(tmp, mk) != 0)
	{
		unlink(tmp);
		return -1;
	}
	return 0;
}

/* Mark the session stopped (stop_mark), end it and wait for its master
** (mpid) to write the record. NULL, or why it did not end. */
static const char *
stop_end(const char *name, const char *path, long mpid,
         const struct dch_agent *ag)
{
	long long end;

	if (stop_mark(path, ag, 0) < 0)
	{
		stop_mark(path, NULL, 1);
		return "could not write its .stopped marker";
	}
	if (end_session(name, ".stopped") != 0)
	{
		stop_mark(path, NULL, 1);
		return "the session went away";
	}
	/* ponytail: 10 s for the master's exit, so the record is final when
	** we say stopped; a --revive right after reads "stopped". */
	for (end = wall_ms() + 10000; mpid > 1 && kill((pid_t)mpid, 0) == 0;
	     poll(NULL, 0, 50))
		if (wall_ms() >= end)
			return "its master did not exit within 10s";
	return NULL;
}

/* A process's cwd (the agent verbs, and --xray's live-shell label) */
static int
proc_cwd(long pid, char *out, size_t outsz)
{
#ifdef __APPLE__
	struct proc_vnodepathinfo vi;

	if (proc_pidinfo((int)pid, PROC_PIDVNODEPATHINFO, 0, &vi, sizeof vi)
	    != (int)sizeof vi)
		return -1;
	snprintf(out, outsz, "%s", vi.pvi_cdir.vip_path);
#else
	char p[64];
	ssize_t n;

	snprintf(p, sizeof p, "/proc/%ld/cwd", pid);
	n = readlink(p, out, outsz - 1);
	if (n <= 0)
		return -1;
	out[n] = '\0';
#endif
	return 0;
}

/* --say, --run and --revive under SIGINT, SIGTERM or SIGHUP (#126): the
** first one stops the verb at its next safe point. Nothing more is typed,
** text already typed is taken back out of the composer (never an Enter),
** and the verb says so. A second one exits at once. A --say parent forwards
** the first to its children as SIGUSR1, which only stops them: a child
** under a Ctrl-C to its whole group gets the tty's SIGINT and the forward,
** and must not read them as two (si_pid cannot tell them apart: macOS
** gives 0 for both when they are pending together). SIGPIPE is ignored: a
** cut dch serve ctl's reader may be gone. */
static volatile sig_atomic_t ty_sig;	/* stop: any of them, or SIGUSR1 */
static volatile sig_atomic_t ty_own;	/* an INT, TERM or HUP came */
static pid_t *ty_kids;		/* in a --say parent: its children */
static int ty_nkids;

static void
ty_set(sigset_t *set)
{
	sigemptyset(set);
	sigaddset(set, SIGINT);
	sigaddset(set, SIGTERM);
	sigaddset(set, SIGHUP);
	sigaddset(set, SIGUSR1);
}

static void
ty_on_sig(int sig)
{
	int k;

	if (sig == SIGUSR1)
	{
		ty_sig = 1;
		return;
	}
	if (ty_own)
		_exit(128 + sig);
	ty_own = ty_sig = 1;
	for (k = 0; k < ty_nkids; k++)
		if (ty_kids[k] > 0)
			kill(ty_kids[k], SIGUSR1);
}

static void
ty_arm(void)
{
	static const int sigs[] = { SIGINT, SIGTERM, SIGHUP, SIGUSR1 };
	struct sigaction sa;
	int k;

	memset(&sa, 0, sizeof sa);
	sa.sa_handler = ty_on_sig;
	sa.sa_flags = SA_RESTART;
	ty_set(&sa.sa_mask);	/* never nested: one body at a time */
	for (k = 0; k < 4; k++)
		sigaction(sigs[k], &sa, NULL);
	signal(SIGPIPE, SIG_IGN);
}

#ifdef HAVE_LIBGHOSTTY_VT

#define AG_POLL_MS  250
/* Paste, then Enter: Enter in the same burst can fold into the paste. */
#define AG_ENTER_MS 300
#define AG_REENTER  5	/* Enters after the first one that did not take */

static const char ag_pause_msg[] =
    "pause all work, i need to restart you. Paused means: stop every "
    "background shell and monitor (TaskStop), stop or finish your "
    "subagents, cancel any /loop, ScheduleWakeup and session crons, and "
    "note what you stopped so it can be re-armed after the restart. Then "
    "say that all work is paused and you are ready to be restarted.";
static const char ag_resume_msg[] =
    "resume all work. You were restarted: re-arm the shells, monitors, "
    "subagents, loops, wakeups and crons you stopped before the restart, "
    "then carry on where you left off.";
/* --stop's (#046); the resume one goes to an agent it gives up on. */
static const char stop_pause_msg[] =
    "pause all work, this session is being stopped by dch --stop and will "
    "be resumed later with --resume. Paused means: stop every background "
    "shell and monitor (TaskStop), stop or finish your subagents, cancel "
    "any /loop, ScheduleWakeup and session crons, and note what you "
    "stopped so it can be re-armed after the resume. Then say that all "
    "work is paused and you are ready to be stopped.";
static const char stop_resume_msg[] =
    "resume all work. dch --stop gave up and left this session running: "
    "re-arm the shells, monitors, subagents, loops, wakeups and crons you "
    "stopped for the pause, then carry on where you left off.";

/* What the screen lets us type into (claude_gate). */
enum { CG_NOVT = -1, CG_EMPTY, CG_DIALOG, CG_DRAFT, CG_QUEUED, CG_UNKNOWN };
static const char *const cg_why[] = {
	"the composer is empty", "a prompt or dialog is waiting for you",
	"the composer holds an unsent draft", "a queued message is pending",
	"the composer is not on screen",
};
static const char *const cg_code[] = {	/* --stop's reasons, as --say's */
	"", "prompt", "draft", "queued", "no_screen",
};

struct agent
{
	char name[600], path[1100];
	char **argv;		/* the session's argv (owned by ci) */
	struct cmd_info ci;
	int tok, argc, saw_busy, exit_ans;	/* exit_ans: exit prompt answered */
	char work[200];		/* what that prompt listed (exit_prompt) */
	long cpid;
	char ver[64], want[64], sid[64], id[64], note[480], why[120];
	enum { AG_WAIT, AG_EXIT, AG_START, AG_DONE } step;
	long long t_sent, ok_since, deadline;
	/* --stop: no relaunch; the session ends instead */
	int stop, json;
	long mpid;		/* the master, to wait out its exit */
	struct dch_agent ag;	/* probed right before /exit */
	const char *res, *code;	/* the result, when not by failed/step */
};

#endif /* HAVE_LIBGHOSTTY_VT */

/* esc_len, sgr_dim and faint_text serve dch-lite too: the X-ray view reads
** a remote session's screen with them (#188, xv_plain) */

/* Length of the escape sequence at p (p[0] == ESC): CSI up to its final
** byte, OSC up to BEL or ST, else ESC + intermediates + one final byte. */
static size_t
esc_len(const unsigned char *p, size_t n)
{
	size_t i = 2;

	if (n < 2)
		return n;
	if (p[1] == '[')
	{
		while (i < n && (p[i] < 0x40 || p[i] > 0x7e))
			i++;
		return i < n ? i + 1 : n;
	}
	if (p[1] == ']')
	{
		for (; i < n; i++)
			if (p[i] == '\a')
				return i + 1;
			else if (p[i] == 0x1b && i + 1 < n && p[i + 1] == '\\')
				return i + 2;
		return n;
	}
	for (i = 1; i < n && p[i] >= 0x20 && p[i] <= 0x2f; i++)
		;
	return i < n ? i + 1 : n;
}

/* Faint (SGR 2) after the SGR parameters p[0..n), starting from `dim`. The
** extended colour forms are skipped whole: the 2 in 38;2;r;g;b is not dim. */
static int
sgr_dim(const unsigned char *p, size_t n, int dim)
{
	size_t i = 0;
	int skip = 0;

	if (n == 0)
		return 0;
	while (i <= n)
	{
		long v = 0;
		size_t j = i;

		while (j < n && p[j] != ';')
		{
			if (p[j] == ':')	/* 38:2:r:g:b is one parameter */
				v = -1;
			else if (v >= 0 && p[j] >= '0' && p[j] <= '9')
				/* capped: no SGR code is past 107 */
				v = v < 1000 ? v * 10 + (p[j] - '0') : v;
			j++;
		}
		if (skip > 0)
			skip--;
		else if (v == 38 || v == 48 || v == 58)
			skip = j + 1 < n && p[j + 1] == '5' ? 2 : 4;
		else if (v == 0 || v == 22)
			dim = 0;
		else if (v == 2)
			dim = 1;
		i = j + 1;
	}
	return dim;
}

/* raw[0..n) as plain text into txt with a per-byte faint flag in dim (both
** NULL: just count), CRs dropped. Cursor-forward (CSI n C) becomes n spaces:
** ghostty 1.4's formatter writes a run of blank cells that way, and Claude
** Code's composer is mostly blank cells — its wrapped rows start with one. */
static size_t
faint_text(const char *raw, size_t n, char *txt, char *dim)
{
	size_t i, k = 0;
	int d = 0;

	for (i = 0; i < n; )
	{
		const unsigned char *u = (const unsigned char *)raw + i;

		if (*u == 0x1b)
		{
			size_t l = esc_len(u, n - i), c = 0, j;

			if (l > 2 && u[1] == '[' && u[l - 1] == 'm')
				d = sgr_dim(u + 2, l - 3, d);
			else if (l > 2 && u[1] == '[' && u[l - 1] == 'C')
			{
				/* capped well past any row width: a huge
				** count must not wrap k or the buffers */
				for (j = 2; j < l - 1 && isdigit(u[j]); j++)
					c = c < 10000 ? c * 10 + (u[j] - '0') : c;
				c = j == l - 1 ? (c ? c : 1) : 0;
			}
			for (; c > 0; c--, k++)
				if (txt)
				{
					txt[k] = ' ';
					dim[k] = (char)d;
				}
			i += l;
			continue;
		}
		if (*u != '\r')
		{
			if (txt)
			{
				txt[k] = (char)*u;
				dim[k] = (char)d;
			}
			k++;
		}
		i++;
	}
	return k;
}

/* A remote session's screen, ctl `--read <name> --ansi`'s stdout
** (the host's bytes, hostile), as plain text the way faint_text reads a
** local one: escapes dropped, CSI n C as n blanks, CRs dropped, a NUL as
** '?'. Its tail
** as fetch_screen keeps one, the text within 8 times that. malloc'd, NULL
** when too big or out of memory (the X-ray view's screen refs, #188). */
char *
xv_plain(const char *raw, size_t n)
{
	char *txt, *dim;
	size_t k, i;

	if (n > DET_CAP)
	{
		raw += n - DET_CAP;
		n = DET_CAP;
	}
	k = faint_text(raw, n, NULL, NULL);
	if (k > 8 * DET_CAP || !(txt = malloc(k + 1)))
		return NULL;
	if (!(dim = malloc(k + 1)))
	{
		free(txt);
		return NULL;
	}
	(void)faint_text(raw, n, txt, dim);
	free(dim);
	/* a NUL is an invalid byte, '?': the rows after it still show (NIT-R1) */
	for (i = 0; i < k; i++)
		if (!txt[i])
			txt[i] = '?';
	txt[k] = '\0';
	return txt;
}

#ifdef HAVE_LIBGHOSTTY_VT

/* The screen at `path` as plain text plus a per-byte faint flag in *dimp
** (both malloc'd), CRs dropped. NULL on failure, *err = CG_NOVT when the
** session has no mirror, else CG_UNKNOWN. */
static char *
screen_faint(const char *path, char **dimp, int *err)
{
	char *raw, *txt, *dim;
	size_t k;
	int n;

	*err = CG_UNKNOWN;
	if (dch_env_on("DCH_NO_DETECT"))
	{
		*err = CG_NOVT;
		return NULL;
	}
	raw = malloc(DET_CAP + 1);
	if (!raw)
		return NULL;
	n = fetch_screen(path, raw, DET_CAP, DET_DEADLINE_MS, 1, 0, NULL);
	if (n <= 0)
	{
		free(raw);
		if (n == -2)
			*err = CG_NOVT;
		return NULL;
	}
	k = faint_text(raw, (size_t)n, NULL, NULL);
	/* calloc: the analyzer cannot pair the counting pass with the filling
	** one, and zeroed bytes cost nothing here */
	txt = calloc(k + 1, 1);
	dim = calloc(k + 1, 1);
	if (!txt || !dim)
	{
		free(raw);
		free(txt);
		free(dim);
		return NULL;
	}
	faint_text(raw, (size_t)n, txt, dim);
	txt[k] = '\0';
	free(raw);
	*dimp = dim;
	return txt;
}

/* A prompt or form (CG_DIALOG) or a queued message (CG_QUEUED) on the
** bottom lines of txt, else CG_UNKNOWN. */
static int
screen_modal(const char *txt)
{
	char *fold = tail_fold(txt, strlen(txt));
	int res = CG_UNKNOWN;

	if (fold && (det_match(det_suppress, sizeof(det_suppress) /
	                       sizeof(det_suppress[0]), fold) ||
	             det_blocker(fold)))
		res = CG_DIALOG;
	else if (fold && (strstr(fold, "press up to edit queued messages") ||
	                  strstr(fold, "ctrl+enter to send now")))
		res = CG_QUEUED;
	free(fold);
	return res;
}

/* Is the Claude Code screen at `path` safe to type into? Positive evidence
** only: the composer must be on screen and empty (nothing after the glyph,
** or only Claude's dim placeholder: a typed draft is not dim), with no
** prompt or form up and — unless queue_ok — no queued message. Anything
** else is reported, never typed over: a trust dialog or release notes
** before the composer read UNKNOWN. queue_ok (--say): a message typed while
** others wait in Claude Code's queue joins them, so only the composer
** under them counts. gate_screen is the verdict on a fetched screen (pure,
** for the fuzzer); claude_gate_q fetches it. */
static int
gate_screen(char *txt, const char *dim, int queue_ok)
{
	char *comp, *end, *c;
	int res;

	if ((res = screen_modal(txt)) == CG_QUEUED && queue_ok)
		res = CG_UNKNOWN;
	if (res == CG_UNKNOWN && (comp = composer_find(txt, &end)))
	{
		for (c = comp + 3; ; )
			if (*c == ' ')
				c++;
			else if (strncmp(c, "\xc2\xa0", 2) == 0)
				c += 2;
			else
				break;
		res = (*c == '\n' || *c == '\0' || dim[c - txt]) &&
		      end == strchr(comp, '\n') + 1 ? CG_EMPTY : CG_DRAFT;
	}
	return res;
}

static int
claude_gate_q(const char *path, int queue_ok)
{
	char *txt, *dim;
	int res;

	if (!(txt = screen_faint(path, &dim, &res)))
		return res;
	res = gate_screen(txt, dim, queue_ok);
	free(txt);
	free(dim);
	return res;
}

static int
claude_gate(const char *path)
{
	return claude_gate_q(path, 0);
}

/* s without spaces, NBSPs and line breaks (wrapping moves them), at most
** cap bytes. */
static char *
squeeze(const char *s, size_t n, size_t cap)
{
	char *o = malloc(n + 1);
	size_t i, k = 0;

	if (!o)
		return NULL;
	for (i = 0; i < n && k < cap; i++)
		if (strncmp(s + i, "\xc2\xa0", 2) == 0)
			i++;
		else if (s[i] != ' ' && s[i] != '\n' && s[i] != '\t')
			o[k++] = s[i];
	o[k] = '\0';
	return o;
}

/* Did `text` land in the composer? Evidence counts only inside the composer
** rows — the transcript above routinely shows "[Pasted text #1 +3 lines]"
** and earlier messages: our text, or the placeholder Claude Code collapses
** a long paste into, leading the composer. Anything typed ahead of it is
** not ours, and an Enter would send it. */
static int
composer_holds(const char *path, const char *text)
{
	char *txt, *dim, *comp, *end, *have = NULL, *want = NULL;
	int err, ok = 0;

	if (!(txt = screen_faint(path, &dim, &err)))
		return 0;
	if (screen_modal(txt) != CG_DIALOG && (comp = composer_find(txt, &end)) &&
	    (have = squeeze(comp + 3, (size_t)(end - comp - 3), (size_t)-1)) &&
	    (want = squeeze(text, strlen(text), 32)))
		ok = *want && (strncmp(have, want, strlen(want)) == 0 ||
		               strncmp(have, "[Pastedtext#", 12) == 0);
	free(have);
	free(want);
	free(txt);
	free(dim);
	return ok;
}

/* What is left of `text` in the composer, for ty_take_back: 0 nothing
** (only the placeholder), 1 a start of it (the rows Ctrl-U has not reached
** yet, or the "[Pasted text #" chip), -1 anything else: a dialog, no
** composer, an unreadable screen, or text that is not a start of ours.
** Not composer_holds(): its 32-byte prefix misses a first row shorter
** than that (#128), which is all that is left before the last Ctrl-U. */
static int
composer_left(const char *path, const char *text)
{
	char *txt, *dim, *comp, *end, *have = NULL, *want = NULL;
	int err, res = -1, g;

	if (!(txt = screen_faint(path, &dim, &err)))
		return -1;
	if ((g = gate_screen(txt, dim, 1)) == CG_EMPTY)
		res = 0;
	else if (g == CG_DRAFT && (comp = composer_find(txt, &end)) &&
	         (have = squeeze(comp + 3, (size_t)(end - comp - 3), (size_t)-1)) &&
	         (want = squeeze(text, strlen(text), (size_t)-1)) &&
	         (strncmp(want, have, strlen(have)) == 0 ||
	          strncmp(have, "[Pastedtext#", 12) == 0))
		res = 1;
	free(have);
	free(want);
	free(txt);
	free(dim);
	return res;
}

/* A signal once our text is in the composer (#126): take it back out,
** never an Enter. Claude Code's Ctrl-U (2.1.318: deleteToLineStart) kills
** to the start of the cursor's visual (wrapped) row, and at its column 0
** the newline or row before it; on an empty composer it does nothing. So
** one Ctrl-U at a time, the screen re-read after each, while what is left
** is a start of the text, until the composer is empty: -5. -6 when it
** holds anything else (a dialog over it: the keys went to it and the text
** sits under it), or text is still there at the deadline (land ms) or
** after strlen + 2 of them (each kills at least a byte).
** shortcut: an Enter Claude takes only after the signal still sends the
** text, read here as taken back; upgrade by watching for the turn. */
static int
ty_take_back(const char *path, const char *text, long land)
{
	long long end = wall_ms() + land;
	size_t n, max = strlen(text) + 2;
	int s;

	for (n = 0; ; n++)
	{
		if ((s = composer_left(path, text)) <= 0)
			return s == 0 ? -5 : -6;
		if (n == max || wall_ms() >= end ||
		    (s = control_connect_path(path)) < 0)
			return -6;
		send_ctl(s, MSG_PUSH, "\x15", 1);
		close(s);
		poll(NULL, 0, AG_POLL_MS / 5);
	}
}

/* Push text (NULL: an Enter) with INT, TERM, HUP and the forward blocked
** across the ty_sig check (#126): -4 once a signal was handled, so nothing
** is ever typed after one. Else 0, or -1 when it could not be pushed. */
static int
ty_push(const char *path, const char *text, int paste)
{
	sigset_t set, old;
	int s, r = -4;

	ty_set(&set);
	sigprocmask(SIG_BLOCK, &set, &old);
	if (!ty_sig && (paste || !text))
		r = dch_pty_push(path, text) < 0 ? -1 : 0;
	else if (!ty_sig)
	{
		r = (s = control_connect_path(path)) < 0 ? -1 : 0;
		if (s >= 0)
		{
			send_ctl(s, MSG_PUSH, text, strlen(text));
			close(s);
		}
	}
	sigprocmask(SIG_SETMASK, &old, NULL);
	return r;
}

/* Type text (paste = bracketed paste for messages; raw keystrokes for a
** slash command, which Claude's menu must see typed), then press Enter —
** only once the composer shows it. A prompt raised in between would take
** the Enter as an answer. Returns 0, -1 when nothing could be typed, -2
** when the text did not land (no Enter pressed), -3 when it landed but
** stayed in the composer through every Enter. Once ty_sig is set (#126):
** -4 before anything is typed; text typed waits to land, then
** ty_take_back's -5 or -6 instead of the next Enter; -6 too when it never
** landed (it may still paint). */
static int
ag_type(const char *path, const char *text, int paste)
{
	/* How long typed text may take to show in the composer: a Claude that
	** has just resumed a long conversation took over a second to paint a
	** paste, one resumed at 230k context under load well over 3 s. Text
	** that shows after we gave up sits in the composer unsent for good, so
	** the wait is long: it ends the moment the text shows. */
	long land = env_ms("DCH_AGENT_LAND_MS", 30000);
	/* An Enter may not take: right after --resume, Claude Code 2.1.309
	** showed the paste but ignored an Enter and another ~6 s later; one
	** ~20 s after the spawn submitted. So Enter again while the text still
	** sits in the composer, AG_REENTER times at most, this far apart. A
	** dialog never gets one: composer_holds() reads it as not holding. */
	long gap = env_ms("DCH_AGENT_REENTER_MS", 5000);
	long long next;
	int t, n;

	if ((t = ty_push(path, text, paste)) < 0)
		return t;
	for (t = AG_ENTER_MS; ; t += AG_POLL_MS / 2)
	{
		poll(NULL, 0, t == AG_ENTER_MS ? AG_ENTER_MS : AG_POLL_MS / 2);
		if (composer_holds(path, text))
			break;
		/* a prompt the paste raised: nothing lands until a human
		** answers it, so stop waiting */
		if (t >= land || claude_gate(path) == CG_DIALOG)
			return ty_sig ? -6 : -2;	/* it may paint later */
	}
	for (n = 0; ; n++)
	{
		if ((t = ty_push(path, NULL, 1)) == -4)
			return ty_take_back(path, text, land);
		if (t < 0)
			return -1;
		for (next = wall_ms() + gap; wall_ms() < next; )
		{
			poll(NULL, 0, AG_POLL_MS);
			if (!composer_holds(path, text))
				return 0;
			if (ty_sig)
				return ty_take_back(path, text, land);
		}
		if (n == AG_REENTER)
			return -3;
	}
}

/* Claude Code's prompt when /exit finds background work still running
** (seen on 2.1.280 and 2.1.322): "Background work is running", "The
** following will stop when you exit:", the work, then "1. Exit and stop
** tasks", "2. Move to background and exit", "3. Stay". 1 when it is on the
** screen at `path` with option 1 reading so; the work it lists goes to out,
** "; "-joined. */
static int
exit_prompt(const char *path, char *out, size_t outsz)
{
	char *raw = malloc(DET_CAP + 1), *l, *nx;
	size_t k = 0, w;
	int n, st = 0, ok = 0;

	out[0] = '\0';
	if (!raw)
		return 0;
	n = fetch_screen(path, raw, DET_CAP, DET_DEADLINE_MS, 0, 0, NULL);
	raw[n > 0 ? n : 0] = '\0';
	for (l = raw; l && !ok; l = nx)
	{
		nx = strchr(l, '\n');
		if (nx)
			*nx++ = '\0';
		l += ap_skip(l) - l;
		w = ap_rtrim(l, strlen(l));
		if (w && l[w - 1] == '\r')
			w--;
		l[w] = '\0';
		if (strncasecmp(l, "Background work is running", 26) == 0)
			st = 1, k = 0, out[0] = '\0';
		else if (st == 1 && strncasecmp(l, "The following will stop when "
		                                "you exit:", 38) == 0)
			st = 2;
		else if (st == 2 && strstr(l, "1. Exit and stop tasks"))
			ok = 1;
		else if (st == 2 && *l && k + 3 < outsz)
		{
			const unsigned char *u = (const unsigned char *)l;

			if (k)
				out[k++] = ';', out[k++] = ' ';
			/* screen text, later pasted into claude: no control byte
			** (ESC, so no paste end; C0, DEL, UTF-8 C1) gets through */
			for (; *u && k + 1 < outsz; u++)
				if (*u == 0xc2 && u[1] >= 0x80 && u[1] <= 0x9f)
					u++;
				else if (*u >= 0x20 && *u != 0x7f)
					out[k++] = (char)*u;
			/* cut at the cap: end on a character boundary; k stays
			** at the cap, so no later item is joined on */
			out[*u ? dch_utf8_trim(out, k) : k] = '\0';
		}
	}
	free(raw);
	return ok;
}

static int
is_uuid(const char *u)
{
	int i;

	for (i = 0; i < 36; i++)
		if (i == 8 || i == 13 || i == 18 || i == 23 ? u[i] != '-'
		                                            : !isxdigit((unsigned char)u[i]))
			return 0;
	return 1;
}

/* The id from the LAST `--resume <uuid>` in the exit tail. The tail is raw
** pty bytes: Ink dims the line and may style the id apart from the command,
** and a narrow pane hard-wraps it, so escapes and control bytes (CR/LF) go
** first and a split or wrapped id reads whole. */
static int
resume_id(const unsigned char *t, size_t n, char *out, size_t outsz)
{
	char *p = malloc(n + 1), *q, *hit = NULL;
	size_t i = 0, k = 0;

	if (!p)
		return -1;
	while (i < n)
		if (t[i] == 0x1b)
			i += esc_len(t + i, n - i);
		else if (t[i] < 0x20 || t[i] == 0x7f)
			i++;
		else
			p[k++] = (char)t[i++];
	p[k] = '\0';
	for (q = p; (q = strstr(q, "--resume")); q += 8)
	{
		char *u = q + 8;

		while (*u == ' ')
			u++;
		if (is_uuid(u))
			hit = u;
	}
	if (hit)
		snprintf(out, outsz, "%.36s", hit);
	free(p);
	return hit ? 0 : -1;
}

/* `claude --version` of the binary the relaunch will run: the token itself
** when it has a '/' (relative to the session's cwd), else the first match on
** the MASTER's PATH, which is the env the respawned argv gets. */
static int
claude_installed(const char *tok, const char *mpath, const char *mcwd,
                 char *out, size_t outsz)
{
	char exe[2048], buf[256], *sp;
	struct pollfd pfd;
	long long end;
	size_t got = 0;
	int fd[2], status;
	pid_t pid;

	exe[0] = '\0';
	if (strchr(tok, '/'))
		snprintf(exe, sizeof exe, "%s%s%s", tok[0] == '/' ? "" : mcwd,
		         tok[0] == '/' ? "" : "/", tok);
	else
		path_lookup(tok, mpath, mcwd, exe, sizeof exe);
	if (!exe[0] || pipe(fd) < 0)
		return -1;
	pid = fork();
	if (pid < 0)
	{
		close(fd[0]);
		close(fd[1]);
		return -1;
	}
	if (pid == 0)
	{
		char *av[] = { exe, "--version", NULL };
		int nul = open("/dev/null", O_RDWR);

		dup2(fd[1], 1);
		if (nul >= 0)
		{
			dup2(nul, 0);
			dup2(nul, 2);
		}
		close(fd[0]);
		close(fd[1]);
		setenv("PATH", mpath, 1);
		if (chdir(mcwd) < 0)
			_exit(127);
		execv(exe, av);
		_exit(127);
	}
	close(fd[1]);
	/* ponytail: a 10 s cap; a --version that hangs is not one to trust */
	end = wall_ms() + 10000;
	pfd.fd = fd[0];
	pfd.events = POLLIN;
	while (got < sizeof buf - 1 && wall_ms() < end &&
	       poll(&pfd, 1, 200) >= 0)
	{
		ssize_t r;

		if (!(pfd.revents & (POLLIN | POLLHUP)))
			continue;
		r = read(fd[0], buf + got, sizeof buf - 1 - got);
		if (r <= 0)
			break;
		got += (size_t)r;
	}
	close(fd[0]);
	while (waitpid(pid, &status, WNOHANG) == 0)
	{
		if (wall_ms() >= end)
		{
			kill(pid, SIGKILL);
			waitpid(pid, &status, 0);
			return -1;
		}
		poll(NULL, 0, 20);
	}
	buf[got] = '\0';
	/* "2.1.299 (Claude Code)" */
	sp = buf + strspn(buf, " \t\r\n");
	sp[strcspn(sp, " \t\r\n")] = '\0';
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0 ||
	    !isdigit((unsigned char)sp[0]))
		return -1;
	snprintf(out, outsz, "%s", sp);
	return 0;
}

/* One report line per agent; done = it reached a terminal state. */
static DCH_PRINTF(3, 4) void
ag_say(struct agent *a, int failed, const char *fmt, ...)
{
	va_list ap;
	char d[2048];
	const char *res = a->res ? a->res : failed ? "failed" : "ok";

	va_start(ap, fmt);
	vsnprintf(d, sizeof d, fmt, ap);
	va_end(ap);
	if (a->stop && strcmp(res, "ok") == 0 &&
	    stop_ok_norec(a->name, a->id[0] ? a->id : a->sid, a->json))
		;
	else if (a->stop || a->json)
		stop_print(a->name, res, strcmp(res, "ok") == 0 ? ""
		           : a->code ? a->code : a->step == AG_WAIT ? "not_idle"
		                                                  : "exit_failed",
		           d, a->ag.name, a->id[0] || !a->stop ? a->id : a->sid,
		           a->json);
	else
	{
		printf("%s: %s%s\n", a->name, failed ? "NOT restarted — " : "", d);
		fflush(stdout);
	}
	a->step = AG_DONE;
}

/* Type the resume message. After /exit's prompt stopped background work
** (exit_ans) it says what, so the agent can re-arm it. */
static int
ag_type_resume(struct agent *a, const char *resume)
{
	char *m;
	int t;

	if (!a->exit_ans)
		return ag_type(a->path, resume, 1);
	if (!(m = malloc(strlen(resume) + sizeof a->work + 120)))
		return -1;
	sprintf(m, "%s At exit, Claude Code stopped background work that was "
	        "still running%s%s. Re-arm what is still needed.", resume,
	        a->work[0] ? ": " : "", a->work);
	t = ag_type(a->path, m, 1);
	free(m);
	return t;
}

/* Type the resume message into an agent we give up on, if the composer
** lets us. Returns how it went, for the report line. */
static const char *
ag_resume(struct agent *a, const char *resume)
{
	if (claude_gate(a->path) != CG_EMPTY)
		return "NOT typed";
	switch (ag_type_resume(a, resume))
	{
	case 0:
		return "typed";
	case -2:
		return "typed but NOT sent (it did not land in the composer)";
	case -3:
		return "typed but NOT sent (it stayed in the composer through "
		       "every Enter)";
	default:
		return "NOT typed";
	}
}

/* The last `want` non-blank lines of raw pty output, escapes dropped,
** joined with " / " into out: what a crashed claude said last. */
static void
tail_lines(const unsigned char *t, size_t n, int want, char *out,
           size_t outsz)
{
	char *p = malloc(n + 1), *l, *keep[8];
	size_t i = 0, k = 0;
	int c = 0, j;

	out[0] = '\0';
	if (!p || want > 8)
	{
		free(p);
		return;
	}
	while (i < n)
		if (t[i] == 0x1b)
		{
			size_t el = esc_len(t + i, n - i);

			/* cursor moves and clears end a line; SGR does not */
			if (t[i + el - 1] != 'm')
				p[k++] = '\n';
			i += el;
		}
		else if (t[i] == '\r' || t[i] == '\n')
			p[k++] = '\n', i++;
		else if (t[i] < 0x20 || t[i] == 0x7f)
			i++;
		else
			p[k++] = (char)t[i++];
	p[k] = '\0';
	for (l = strtok(p, "\n"); l; l = strtok(NULL, "\n"))
	{
		l += strspn(l, " ");
		if (*l)
			keep[c++ % want] = l;
	}
	for (j = c > want ? c - want : 0; j < c; j++)
		snprintf(out + strlen(out), outsz - strlen(out), "%s%s",
		         out[0] ? " / " : "", keep[j % want]);
	free(p);
}

/* A master that does not answer is not a gone session: -1/-2 it went
** quiet, -4 with the socket still there is a refused connect (a full
** listen backlog). Only a missing socket means the session ended. */
static int
ag_quiet(const struct agent *a, int st)
{
	struct stat sb;

	return st == -1 || st == -2 ||
	       (st == -4 && (stat(a->path, &sb) == 0 || errno != ENOENT));
}

/* Respawn the held session as claude --resume <id>. A master that did not
** answer may or may not have taken it: AG_START sends it again while the
** session still reads held. Only a refusal cancels, which ends the
** session. Returns 1 when it failed. */
static int
ag_relaunch(struct agent *a)
{
	char **nv;
	int nc, st;

	nv = resume_argv(a->argv, a->argc, a->tok, a->id, &nc);
	st = nv ? respawn_req(a->path, 'a', nv, nc) : -3;
	free(nv);
	if (st == DCH_ST_OK || ag_quiet(a, st))
	{
		a->step = AG_START;
		return 0;
	}
	respawn_req(a->path, 'c', NULL, 0);
	ag_say(a, 1, "relaunch with --resume %s was refused (%d); "
	       "session ended", a->id, st);
	return 1;
}

/* /exit left unsent in a composer Claude stays in: a later Enter there
** would end the session with no hold, so take it back out. The report's
** suffix. */
static const char *
exit_back(const char *path)
{
	if (!composer_holds(path, "/exit"))
		return "";
	return ty_take_back(path, "/exit", env_ms("DCH_AGENT_LAND_MS", 30000))
	       == -5 ? "; /exit taken back out of the composer"
	             : "; /exit may still sit in the composer";
}

/* The id to resume into: the exit line's is primary; the record's (read
** right before /exit, as /clear rotates it) is the fallback and the
** cross-check. */
static void
ag_read_id(struct agent *a, const struct cmd_info *ci)
{
	char tid[64];

	if (resume_id(ci->tail, ci->tail_len, tid, sizeof tid) < 0)
	{
		snprintf(a->id, sizeof a->id, "%s", a->sid);
		snprintf(a->note + strlen(a->note), sizeof a->note -
		         strlen(a->note), " (no resume line in the exit output; "
		         "used the session record's id)");
	}
	else
	{
		snprintf(a->id, sizeof a->id, "%s", tid);
		if (strcmp(tid, a->sid) != 0)
			snprintf(a->note + strlen(a->note), sizeof a->note -
			         strlen(a->note), " (exit line said %s, session "
			         "record said %s; used the exit line)", tid, a->sid);
	}
}

/* The hold is armed and /exit typed: wait for the exit, then relaunch. */
static int
ag_exited(struct agent *a, const char *step_s)
{
	struct cmd_info ci;
	int st;

	st = cmd_query(a->path, &ci);
	if (ag_quiet(a, st))
	{
		if (wall_ms() < a->deadline)
			return 0;
		/* No cancel: to a held master it would end the session. */
		ag_say(a, 1, "the master stopped answering after /exit (silent "
		       "at the %s limit); hold left as is, it ends after "
		       "DCH_HOLD_MS; resume by hand: claude --resume %s",
		       step_s, a->sid);
		return 1;
	}
	if (st != 0)
	{
		ag_say(a, 1, "the session went away during /exit; resume by "
		       "hand: claude --resume %s", a->sid);
		return 1;
	}
	if (strcmp(ci.state, "held") != 0)
	{
		if (strcmp(ci.state, "running") == 0)
			ag_say(a, 1, "the hold was dropped (expired or cancelled) "
			       "before claude exited (session %s)", a->sid);
		else if (wall_ms() >= a->deadline)
		{
			respawn_req(a->path, 'c', NULL, 0);
			ag_say(a, 1, "/exit did not end claude within %s; hold "
			       "cancelled, claude left running (session %s)%s%s",
			       step_s, a->sid, a->note, exit_back(a->path));
		}
		cmd_info_free(&ci);
		return a->step == AG_DONE;
	}
	/* Crashed instead of a clean /exit: nothing to resume into. The
	** cancel ends the session, as the exit would have unarmed. */
	if (ci.sig > 0 || ci.code != 0)
	{
		char last[300];

		/* The session, and its screen, go with the cancel: keep what
		** claude said last in the report. */
		tail_lines(ci.tail, ci.tail_len, 3, last, sizeof last);
		respawn_req(a->path, 'c', NULL, 0);
		if (ci.sig > 0)
			ag_say(a, 1, "claude died on signal %d instead of exiting;"
			       " not relaunched, session ended%s%s%s; resume by "
			       "hand: claude --resume %s", ci.sig,
			       last[0] ? " (last output: " : "", last,
			       last[0] ? ")" : "", a->sid);
		else
			ag_say(a, 1, "claude exited with status %d instead of 0;"
			       " not relaunched, session ended%s%s%s; resume by "
			       "hand: claude --resume %s", ci.code,
			       last[0] ? " (last output: " : "", last,
			       last[0] ? ")" : "", a->sid);
		cmd_info_free(&ci);
		return 1;
	}
	ag_read_id(a, &ci);
	cmd_info_free(&ci);
	a->deadline = wall_ms() + env_ms("DCH_AGENT_STEP_MS", 60000);
	json_phase(a->name, "relaunching", a->json);
	a->code = "relaunch_failed";
	return ag_relaunch(a);
}

static int stop_recorded(const char *path, long since, char *sid,
                         size_t sidsz);

/* --stop, claude the session's command: the hold is armed and /exit
** typed. Once claude is gone — the session held, or claude gone under a
** wrapper that still runs — the exit line's id goes into the marker and the
** session ends. Not gone in time: disarmed, claude told to carry on. */
static int
stop_exited(struct agent *a, const char *step_s, const char *resume)
{
	struct cmd_info ci;
	const char *why;
	int st = cmd_query(a->path, &ci), held = 0, gone = 0;

	if (st == 0)
	{
		held = strcmp(ci.state, "held") == 0;
		gone = held || kill((pid_t)a->cpid, 0) < 0;
		if (!gone)
			cmd_info_free(&ci);
	}
	if (!gone && st == 0)
	{
		if (wall_ms() < a->deadline)
			return 0;
		/* Not held: this only disarms. Then the marker goes, but only
		** while claude provably runs: its session file stays, so the
		** record keeps tracking it (a /clear's new id too). One that quit
		** right after the check keeps the marker, and with it the id. */
		respawn_req(a->path, 'c', NULL, 0);
		if (kill((pid_t)a->cpid, 0) == 0)
			stop_mark(a->path, NULL, 1);
		why = exit_back(a->path);	/* before the resume message */
		ag_say(a, 1, "/exit did not end claude within %s%s%s; resume "
		       "message %s", step_s, a->note, why, ag_resume(a, resume));
		return 1;
	}
	if (!gone && ag_quiet(a, st))
	{
		if (wall_ms() < a->deadline)
			return 0;
		/* Claude may be gone already: the marker is all that keeps its
		** id, so it stays and the held master's end records "stopped"
		** with it. A claude still running gets a later --stop. */
		ag_say(a, 1, "the master stopped answering after /exit within %s;"
		       " its .stopped marker stays, so the record ends stopped "
		       "with the id; resume message %s; resume by hand: claude "
		       "--resume %s; if claude is still running, run dch --stop "
		       "%s again", step_s, ag_resume(a, resume), a->sid, a->name);
		return 1;
	}
	if (st != 0)
	{
		/* The master read the marker on its way out (it went first in
		** its atexit order): a record that ended stopped with an id is
		** a stop, whatever took the session down. */
		if (stop_recorded(a->path, (long)(a->t_sent / 1000), a->id,
		                  sizeof a->id) == 0)
		{
			a->res = "ok";
			a->code = "";
			ag_say(a, 0, "(the session ended on its own during /exit; "
			       "its record kept the id)");
			return 0;
		}
		ag_say(a, 1, "the session went away during /exit; resume by "
		       "hand: claude --resume %s", a->sid);
		return 1;
	}
	/* Under a wrapper, the exit line may still sit in the pty: give the
	** master a tick to read it into the tail. */
	if (!held && !a->ok_since)
	{
		cmd_info_free(&ci);
		a->ok_since = wall_ms();
		return 0;
	}
	ag_read_id(a, &ci);
	cmd_info_free(&ci);
	snprintf(a->ag.sid, sizeof a->ag.sid, "%s", a->id);
	json_phase(a->name, "ending", a->json);
	if ((why = stop_end(a->name, a->path, a->mpid, &a->ag)))
	{
		a->code = "end_failed";
		ag_say(a, 1, "claude quit (claude --resume %s), but %s", a->id,
		       why);
		return 1;
	}
	a->res = "ok";
	a->code = "";
	ag_say(a, 0, "%s", a->note[0] ? a->note + 1 : "");
	return 0;
}

/* One poll of one agent. Returns 1 when it ended in failure. */
static int
ag_tick(struct agent *a, long tmo, long settle, const char *resume)
{
	long step = env_ms("DCH_AGENT_STEP_MS", 60000);
	long long now = wall_ms();
	char step_s[32];
	struct crec r;
	int g, ready, idle, busy;

	snprintf(step_s, sizeof step_s, "%lds", step / 1000);
	if (a->step == AG_EXIT)
	{
		/* Our /exit asked Claude to quit; with background work left it
		** asks first. The pause already told the agent to stop it all,
		** so confirm the exit, as /exit did before the prompt existed. */
		if (!a->exit_ans && exit_prompt(a->path, a->work, sizeof a->work) &&
		    ty_push(a->path, "1", 0) == 0)
		{
			a->exit_ans = 1;
			snprintf(a->note + strlen(a->note),
			         sizeof a->note - strlen(a->note), " (at /exit claude "
			         "stopped the background work it still ran: %s)",
			         a->work[0] ? a->work : "unlisted");
		}
		return a->stop ? stop_exited(a, step_s, resume)
		               : ag_exited(a, step_s);
	}
	if (a->step == AG_START)
	{
		struct cmd_info ci;
		int st = cmd_query(a->path, &ci);

		if (ag_quiet(a, st))
		{
			if (now < a->deadline)
				return 0;
			ag_say(a, 1, "relaunched with --resume %s, but the master "
			       "stopped answering (silent at the %s limit); resume "
			       "message not typed", a->id, step_s);
			return 1;
		}
		if (st != 0)
		{
			ag_say(a, 1, "relaunched with --resume %s, but it exited "
			       "and the session ended", a->id);
			return 1;
		}
		/* Still held: the relaunch went to a stalled master and never
		** applied (the relaunched claude is not armed, so it never reads
		** held). Send it again, within the same step limit. */
		if (strcmp(ci.state, "held") == 0)
		{
			cmd_info_free(&ci);
			if (now < a->deadline)
				return ag_relaunch(a);
			ag_say(a, 1, "relaunched with --resume %s, but claude did not "
			       "start within %s; hold left as is, it ends after "
			       "DCH_HOLD_MS", a->id, step_s);
			return 1;
		}
		/* dch never arms the relaunched claude, so "armed" here is a late
		** duplicate 'a': a relaunch that timed out, was re-sent above while
		** the session read held, then applied after all. Disarm it once,
		** or claude's next exit would relaunch it again. Not held, so 'c'
		** only disarms (a held session would end on it). */
		if (strcmp(ci.state, "armed") == 0)
			respawn_req(a->path, 'c', NULL, 0);
		g = CG_UNKNOWN;
		busy = ci.pid > 0 && claude_find(ci.pid, 1, &r) > 0;
		if (busy && !(busy = strcmp(r.status, "idle") != 0) &&
		    (g = claude_gate(a->path)) == CG_EMPTY)
		{
			int old = strcmp(r.ver, a->want) != 0;
			int t;

			cmd_info_free(&ci);
			json_phase(a->name, "resuming", a->json);
			t = ag_type_resume(a, resume);
			a->code = t == -3 ? "not_submitted" : "not_typed";
			if (t == -1)
			{
				ag_say(a, 1, "relaunched with --resume %s on claude %s, "
				       "but could not type the resume message",
				       a->id, r.ver);
				return 1;
			}
			if (t != 0)
			{
				ag_say(a, 1, "relaunched with --resume %s on claude %s, "
				       "but the resume message %s", a->id, r.ver,
				       t == -3 ? "stayed in the composer through every "
				       "Enter" : "did not land in the composer; Enter "
				       "not pressed");
				return 1;
			}
			if (strcmp(r.sid, a->id) != 0)
				snprintf(a->note + strlen(a->note),
				         sizeof a->note - strlen(a->note),
				         " (the new record says session %s)", r.sid);
			a->code = "not_updated";
			if (old)
				ag_say(a, 1, "relaunched with --resume %s, but still "
				       "on claude %s (expected %s)%s", a->id, r.ver,
				       a->want, a->note);
			else
				ag_say(a, 0, "claude %s -> %s, resumed %s%s", a->ver,
				       r.ver, a->id, a->note);
			return old;
		}
		cmd_info_free(&ci);
		if (now < a->deadline)
			return 0;
		/* busy: the gate was never read, cg_why[g] would blame the screen */
		ag_say(a, 1, "relaunched with --resume %s, but no empty composer "
		       "within %s (%s%s); resume message not typed", a->id, step_s,
		       busy ? "claude is not idle, status " : g == CG_NOVT
		       ? "no mirror" : cg_why[g], busy ? r.status : "");
		return 1;
	}

	/* AG_WAIT: is the pause consumed and the agent idle, for a while? */
	if (claude_find(a->cpid, 0, &r) == 0 || r.pid != a->cpid)
	{
		a->code = "claude_exited";
		ag_say(a, 1, "claude exited while pausing");
		return 1;
	}
	if (strcmp(r.status, "busy") == 0 && r.upd >= a->t_sent)
		a->saw_busy = 1;
	g = claude_gate(a->path);
	/* Busy after the push proves the message was taken; an idle stamp
	** newer than the push does too, unless the message sits queued —
	** a turn that ends with one queued is idle for a moment first. */
	/* "shell": idle, a background shell still running. The pause told the
	** agent to stop them; one still there DCH_AGENT_SHELL_MS after its
	** status says so is one it does not see (civ2-qa: "no background
	** shells" over Claude's "2 shells still running"), and /exit's prompt
	** stops it (ag_tick). */
	idle = strcmp(r.status, "idle") == 0 ||
	       (strcmp(r.status, "shell") == 0 && r.upd > a->t_sent &&
	        now - r.upd >= env_ms("DCH_AGENT_SHELL_MS", 60000));
	ready = (a->saw_busy || (r.upd > a->t_sent && g != CG_QUEUED)) &&
	        idle && g == CG_EMPTY;
	if (!ready && g == CG_EMPTY)
		snprintf(a->why, sizeof a->why, "status %s", r.status);
	else if (!ready)
		snprintf(a->why, sizeof a->why, "status %s, %s", r.status,
		         g == CG_NOVT ? "no mirror" : cg_why[g]);
	else if ((g = detect_agents(a->name)) > 0)
	{
		ready = 0;
		snprintf(a->why, sizeof a->why, "%d subagent%s running", g,
		         g == 1 ? "" : "s");
	}
	else
		snprintf(a->why, sizeof a->why, "idle, settling");
	if (!ready)
		a->ok_since = 0;
	else if (!a->ok_since)
		a->ok_since = now;
	if (ready && now - a->ok_since >= settle)
	{
		const char *why;
		int st;

		a->code = "exit_failed";
		/* --stop keeps the agent for --revive: its launch command can
		** only be read while claude runs. */
		if (a->stop && dch_agent_probe(a->ci.pid, &a->ag) < 0)
		{
			ag_say(a, 1, "could not read claude's launch command; "
			       "resume message %s", ag_resume(a, resume));
			return 1;
		}
		/* Taken now, not at pause time: /clear rotates it. */
		snprintf(a->sid, sizeof a->sid, "%s", r.sid);
		/* --stop, claude typed into the shell: no /exit, the shell would
		** run whatever followed claude on its line. The record's id goes
		** into the marker and the hangup ends shell and claude together. */
		if (a->stop && a->tok == a->argc)
		{
			snprintf(a->id, sizeof a->id, "%s", r.sid);
			snprintf(a->ag.sid, sizeof a->ag.sid, "%s", r.sid);
			json_phase(a->name, "ending", a->json);
			if ((why = stop_end(a->name, a->path, a->mpid, &a->ag)))
			{
				a->code = "end_failed";
				ag_say(a, 1, "%s; resume message %s", why,
				       ag_resume(a, resume));
				return 1;
			}
			a->res = "ok";
			a->code = "";
			ag_say(a, 0, "%s", "");
			return 0;
		}
		json_phase(a->name, "exiting", a->json);
		st = respawn_req(a->path, 'h', NULL, 0);
		if (st != DCH_ST_OK)
		{
			ag_say(a, 1, "could not arm the hold (%d); resume message "
			       "%s", st, ag_resume(a, resume));
			return 1;
		}
		/* --stop: a record refresh after /exit must keep the agent */
		if (a->stop && stop_mark(a->path, &a->ag, 0) < 0)
		{
			stop_mark(a->path, NULL, 1);
			respawn_req(a->path, 'c', NULL, 0);
			ag_say(a, 1, "could not write its .stopped marker; resume "
			       "message %s", ag_resume(a, resume));
			return 1;
		}
		/* -3 (still in the composer) goes on to AG_EXIT: claude may be
		** gone already, and a cancel would end a held session; AG_EXIT
		** cancels safely if it never exits. */
		if ((st = ag_type(a->path, "/exit", 0)) != 0 && st != -3)
		{
			if (a->stop)
				stop_mark(a->path, NULL, 1);
			respawn_req(a->path, 'c', NULL, 0);
			ag_say(a, 1, "%s; hold cancelled%s%s", st == -2
			       ? "/exit did not land in the composer, Enter not "
			         "pressed" : "could not type /exit",
			       a->stop ? "; resume message " : "",
			       a->stop ? ag_resume(a, resume) : "");
			return 1;
		}
		a->step = AG_EXIT;
		a->deadline = now + step;
		a->ok_since = 0;
		return 0;
	}
	if (now < a->deadline)
		return 0;
	ag_say(a, 1, "not ready within %lds (%s); resume message %s", tmo / 1000,
	       a->why, ag_resume(a, resume));
	return 1;
}

/* SIGINT/SIGTERM/SIGHUP during the agent step: the first one is noted, so
** agents still waiting to pause get the resume message instead of staying
** paused; a second one stops the wait for the rest once the step in hand
** is through (an armed hold gets its /exit, a marker stays), each reported
** with its id; a third exits at once. */
static volatile sig_atomic_t ag_sig, ag_sig2;

static void
ag_on_sig(int sig)
{
	if (ag_sig2)
		_exit(128 + sig);
	if (ag_sig)
		ag_sig2 = sig;
	else
		ag_sig = sig;
}

/* The agent step of `--restart --include-agents`: every session, or only
** `only` when non-NULL; json = --json's NDJSON. Returns the number of
** agents that were attempted and failed. */
static int ag_run(struct agent *ag, int na, const char *pmsg,
                  const char *rmsg);

static int
restart_agents(int force, const char *only, int json)
{
	struct slist sl = {0}, cur = {0};
	struct agent *ag;
	const char *pmsg = getenv("DCH_PAUSE_MSG"), *rmsg = getenv("DCH_RESUME_MSG");
	const char *self = getenv("DCH_SESSION");
	char selfb[256], onlyb[256], want[64] = "";
	int na = 0, k, fails, said = 0;

	if (!pmsg || !*pmsg)
		pmsg = ag_pause_msg;
	if (!rmsg || !*rmsg)
		rmsg = ag_resume_msg;
	if (self && *self)
		self = canon_name(self, selfb, sizeof selfb);
	if (only)
		only = canon_name(only, onlyb, sizeof onlyb);
	list_sessions(&sl);
	ag = calloc((size_t)sl.n + 1, sizeof *ag);
	if (!ag)
	{
		slist_free(&sl);
		return 1;
	}
	for (k = 0; k < sl.n; k++)
	{
		struct agent *a = &ag[na];
		char mpath[8192], mcwd[PATH_MAX], jcwd[PATH_MAX], rc[PATH_MAX];
		const char *skip = NULL, *code = NULL;
		struct crec r;
		long mpid;
		int st, n;

		if (only && strcmp(sl.v[k], only) != 0)
			continue;
		snprintf(a->name, sizeof a->name, "%s", sl.v[k]);
		session_sock_path(sl.v[k], a->path, sizeof a->path);
		a->json = json;
		st = cmd_query(a->path, &a->ci);
		if (st == DCH_ST_UNSUP)
		{
			stop_print(a->name, "skipped", "old_master", "its master "
			           "predates agent restart (--respawn/--cmd)", "", "",
			           json);
			said++;
			continue;
		}
		if (st != 0)
			continue;	/* dead socket: nothing to restart */
		a->argv = a->ci.argv;
		a->argc = a->ci.argc;
		a->tok = dch_claude_tok(a->argv, a->argc);
		n = a->ci.pid > 0 && strcmp(a->ci.state, "held") != 0
		    ? claude_find(a->ci.pid, 1, &r) : 0;
		mpid = a->ci.pid > 0 ? proc_ppid(a->ci.pid) : -1;
		if (n == 0 && a->tok == a->argc)
		{
			if (json)
				stop_print(a->name, "skipped", "not_claude", "no claude "
				           "runs in the session", "", "", 1);
			cmd_info_free(&a->ci);
			continue;	/* no Claude in this session */
		}
		if (strcmp(a->ci.state, "running") != 0)
		{
			/* Someone else's respawn owns the session's next exit. */
			snprintf(a->why, sizeof a->why, "a respawn is pending (%s)",
			         a->ci.state);
			stop_print(a->name, "skipped", "respawn_pending", a->why, "",
			           "", json);
			said++;
			cmd_info_free(&a->ci);
			continue;
		}
		said++;
		mpath[0] = mcwd[0] = '\0';
		if (mpid > 1 && proc_env_value(mpid, "PATH", mpath,
		                               sizeof mpath) < 0)
			snprintf(mpath, sizeof mpath, "%s",
			         getenv("PATH") ? getenv("PATH") : "");
		if (mpid > 1 && proc_cwd(mpid, rc, sizeof rc) == 0 &&
		    !realpath(rc, mcwd))
			mcwd[0] = '\0';
		if (n == 0)
			skip = "claude is its command, but no live claude "
			       "session record joins it", code = "no_record";
		/* No /exit there: the shell would run whatever followed claude
		** on its line (`claude && git push`, a loop). */
		else if (a->tok == a->argc)
			skip = "claude was started inside the session's shell, "
			       "not as its command", code = "in_shell";
		else if (proc_depth((long)getpid(), r.pid) >= 0 ||
		         (self && strcmp(self, a->name) == 0))
			skip = "run from outside dch to restart this one",
			code = "self";
		else if (!mcwd[0] || !realpath(r.cwd, jcwd) ||
		         strcmp(mcwd, jcwd) != 0)
			skip = "claude's cwd is not the session's launch cwd",
			code = "cwd_mismatch";
		else if (claude_installed(a->argv[a->tok], mpath, mcwd, a->want,
		                          sizeof a->want) < 0)
			skip = "cannot run `claude --version` as the session "
			       "would (its PATH and cwd)", code = "version_unknown";
		if (skip)
		{
			stop_print(a->name, "skipped", code, skip, "", "", json);
			cmd_info_free(&a->ci);
			continue;
		}
		a->cpid = r.pid;
		snprintf(a->ver, sizeof a->ver, "%s", r.ver);
		if (n > 1 && !json)
			printf("%s: %d claude processes in the session; "
			       "restarting the outermost (pid %ld)\n", a->name, n,
			       a->cpid);
		if (force || strcmp(a->ver, a->want) != 0)
		{
			na++;
			continue;
		}
		snprintf(rc, sizeof rc, "%s%salready on claude %s",
		         json ? "" : a->name, json ? "" : ": ", a->want);
		if (json)	/* no summary line: cur stays empty */
			stop_print(a->name, "skipped", "current", rc, "", "", 1);
		else
			slist_push(&cur, rc);
		snprintf(want, sizeof want, "%s", a->want);
		said -= n == 1;
		cmd_info_free(&a->ci);
	}
	slist_free(&sl);
	if (cur.n && !na && !said && !only)
		printf("all claude agents already on claude %s\n", want);
	else
		for (k = 0; k < cur.n; k++)
			puts(cur.v[k]);
	slist_free(&cur);
	fflush(stdout);
	fails = ag_run(ag, na, pmsg, rmsg);
	for (k = 0; k < na; k++)
		cmd_info_free(&ag[k].ci);
	free(ag);
	return fails;
}

/* Pause every agent in ag[0..na), then wait on them all at once: one slow
** agent must not hold up the rest. Returns how many failed. */
static int
ag_run(struct agent *ag, int na, const char *pmsg, const char *rmsg)
{
	long tmo = env_ms("DCH_AGENT_TIMEOUT_MS", 600000);
	long settle = env_ms("DCH_AGENT_SETTLE_MS", 3000);
	int k, fails = 0, live;
	static const int sigs[] = { SIGINT, SIGTERM, SIGHUP };
	struct sigaction sa, old[3], oldpipe;

	/* From the first pause on, a signal must not leave agents paused. */
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = ag_on_sig;
	sa.sa_flags = SA_RESTART;
	sigemptyset(&sa.sa_mask);
	ag_sig = ag_sig2 = 0;
	for (k = 0; k < 3; k++)
		sigaction(sigs[k], &sa, &old[k]);
	/* A caller that stopped reading (a timed-out Bash tool, `| head`)
	** must not kill us mid-report, leaving the next agents paused. */
	sa.sa_handler = SIG_IGN;
	sigaction(SIGPIPE, &sa, &oldpipe);

	for (k = 0; k < na; k++)
	{
		struct agent *a = &ag[k];
		int g = ag_sig ? CG_UNKNOWN : claude_gate(a->path);

		if (ag_sig)
		{
			a->res = "interrupted";
			a->code = "signal";
			ag_say(a, 1, "interrupted (signal %d) before its pause; "
			       "nothing typed", (int)ag_sig);
			fails++;
			continue;
		}
		if (g != CG_EMPTY)
		{
			a->res = g == CG_NOVT ? "skipped" : NULL;
			a->code = cg_code[g == CG_NOVT ? CG_UNKNOWN : g];
			ag_say(a, g != CG_NOVT, g == CG_NOVT ? "skipped — the session "
			       "has no terminal mirror" : "%s; nothing typed",
			       g == CG_NOVT ? "" : cg_why[g]);
			fails += g != CG_NOVT;
			continue;
		}
		/* Stamped before the push: a status written after it proves
		** the agent has seen the message. */
		a->t_sent = wall_ms();
		a->deadline = a->t_sent + tmo;
		a->step = AG_WAIT;
		snprintf(a->why, sizeof a->why, "no status yet");
		json_phase(a->name, "pausing", a->json);
		if ((g = ag_type(a->path, pmsg, 1)) != 0)
		{
			a->code = "not_typed";
			ag_say(a, 1, g == -2 ? "the pause message did not land in the "
			       "composer; Enter not pressed" : g == -3 ? "the pause "
			       "message stayed in the composer through every Enter"
			       : "could not type the pause message");
			fails++;
			continue;
		}
		if (a->stop || a->json)
			continue;
		printf("%s: pause message sent; waiting for claude %s to go "
		       "idle\n", a->name, a->ver);
		fflush(stdout);
	}
	do
	{
		live = 0;
		for (k = 0; k < na; k++)
			if (ag[k].step == AG_WAIT && ag_sig)
			{
				/* Past AG_WAIT the hold is armed: those finish, bounded
				** by DCH_AGENT_STEP_MS. */
				ag[k].res = "interrupted";
				ag[k].code = "signal";
				ag_say(&ag[k], 1, "interrupted (signal %d) while waiting "
				       "for it to pause; resume message %s", (int)ag_sig,
				       ag_resume(&ag[k], rmsg));
				fails++;
			}
			else if (ag[k].step != AG_DONE && ag_sig2)
			{
				ag[k].res = "interrupted";
				ag[k].code = "signal";
				/* restart only: claude already runs again on the id */
				if (ag[k].step == AG_START)
					ag_say(&ag[k], 1, "interrupted (signal %d) after the "
					       "relaunch: claude --resume %s is started in the "
					       "session; resume message not typed", (int)ag_sig2,
					       ag[k].id);
				/* /exit is typed: a --stop's marker stays, so a claude
				** that quits ends the record "stopped" with the id; a
				** restart's armed hold runs out instead. */
				else
					ag_say(&ag[k], 1, "interrupted (signal %d) after /exit;"
					       " %s; resume by hand: claude --resume %s; if "
					       "claude is still running, %s %s%s", (int)ag_sig2,
					       ag[k].stop ? "hold and .stopped marker left as "
					       "they are, the record ends stopped with the id "
					       "once claude has quit" : "hold left armed, once "
					       "claude has quit the session stays held until "
					       "DCH_HOLD_MS runs out, then ends",
					       ag[k].id[0] ? ag[k].id : ag[k].sid, ag[k].stop
					       ? "run dch --stop" : "dch --respawn", ag[k].name,
					       ag[k].stop ? " again" : " --cancel disarms the "
					       "hold");
				fails++;
			}
			else if (ag[k].step != AG_DONE)
			{
				fails += ag_tick(&ag[k], tmo, settle, rmsg);
				live += ag[k].step != AG_DONE;
			}
		if (live)
			poll(NULL, 0, AG_POLL_MS);
	} while (live);
	for (k = 0; k < 3; k++)
		sigaction(sigs[k], &old[k], NULL);
	sigaction(SIGPIPE, &oldpipe, NULL);
	return fails;
}

#else /* !HAVE_LIBGHOSTTY_VT */

static int
restart_agents(int force, const char *only, int json)
{
	struct slist sl = {0};
	struct cmd_info ci;
	char path[1100], onlyb[256];
	int k, hit = 0, h;

	(void)force;
	if (only)
		only = canon_name(only, onlyb, sizeof onlyb);
	/* Only worth a word when some session runs claude; --json: a line
	** for each one that does. */
	list_sessions(&sl);
	for (k = 0; k < sl.n && (json || !hit); k++)
	{
		if (only && strcmp(sl.v[k], only) != 0)
			continue;
		session_sock_path(sl.v[k], path, sizeof path);
		if (cmd_query(path, &ci) == 0)
		{
			h = dch_claude_tok(ci.argv, ci.argc) < ci.argc;
			cmd_info_free(&ci);
			if (h && json)
				stop_print(sl.v[k], "skipped", "no_screen", "dch-lite "
				           "has no terminal mirror to watch it with", "",
				           "", 1);
			hit |= h;
		}
	}
	slist_free(&sl);
	if (hit && !json)
		puts("agents: not restarted — dch-lite has no terminal mirror to "
		     "watch them with");
	return 0;
}

#endif

/* ---- --say: one message to the Claude sessions (#045) --------------
**
** `--say <name>...|--all -- <text>` types text into each session's Claude
** Code at the moment of the call, busy or not: Enter in a busy Claude
** queues the message, it does not interrupt the turn. Never over a dialog
** (Enter would answer it) or a draft (ours would join it) — those are
** skipped and the caller retries. Every session types in a child of its
** own, so --all costs the slowest session, not the sum (ag_type alone may
** take ~25 s). */

/* Outcomes; from SAY_NO_SESSION on they are failures. */
enum { SAY_OK, SAY_NOT_CLAUDE, SAY_SELF, SAY_PROMPT, SAY_DRAFT,
       SAY_NO_SCREEN, SAY_NO_SESSION, SAY_NOT_SUBMITTED,
       SAY_NOT_TYPED, SAY_OLD_MASTER, SAY_INTERRUPTED, SAY_WITHDRAWN,
       SAY_DRAFT_LEFT, SAY_NOT_FG, SAY_N };
static const char *const say_code[] = {
	"", "not_claude", "self", "prompt", "draft", "no_screen", "no_session",
	"not_submitted", "not_typed", "old_master", "interrupted", "withdrawn",
	"draft_left", "not_foreground",
};
static const char *const say_why[] = {
	"", "no claude runs in the session", "the session you are in",
	"a prompt or dialog is waiting for you",
	"the composer holds an unsent draft", "the composer is not on screen",
	"no such session",
	"the message stayed in the composer through every Enter",
	"the message could not be typed",
	"its master predates --cmd (dch --restart it)",
	"a signal came before anything was typed",
	"a signal came once it was typed: taken back out of the composer, "
	"not sent",
	"a signal came once it was typed: it may still sit in the composer, "
	"not sent",
	"claude is not the terminal's foreground job (suspended with Ctrl-Z?): "
	"fg it first",
};
/* A child's exit status carries its outcome past exit(1) and the like. */
#define SAY_EXIT 100

static void
say_print(const char *name, int c, int json)
{
	const char *res = c == SAY_OK ? "sent" : c < SAY_NO_SESSION ? "skipped"
	                                                           : "failed";
	size_t cap = strlen(name) * 6 + 3;
	char *q;

	if (!json)
		printf(c == SAY_OK ? "%s: %s\n" : "%s: %s — %s\n", name, res,
		       say_why[c]);
	else if ((q = malloc(cap)))
	{
		if (dch_json_quote(q, cap, name) == (size_t)-1)
			snprintf(q, cap, "\"\"");
		printf("{\"name\":%s,\"result\":\"%s\",\"reason\":\"%s\","
		       "\"detail\":\"%s\"}\n", q, c == SAY_OK ? "ok" : res,
		       say_code[c], say_why[c]);
		free(q);
	}
	fflush(stdout);
}

#ifdef HAVE_LIBGHOSTTY_VT
/* The gate and the typing, in the child. Busy or with messages queued,
** Claude Code queues ours: ag_type counts the text leaving the composer as
** sent, whether it started a turn or joined the queue above it. */
static int
say_type(const char *path, const char *text, int paste)
{
	switch (claude_gate_q(path, 1))
	{
	case CG_EMPTY:
		break;
	case CG_DIALOG:
		return SAY_PROMPT;
	case CG_DRAFT:
		return SAY_DRAFT;
	default:
		return SAY_NO_SCREEN;
	}
	switch (ag_type(path, text, paste))
	{
	case 0:
		return SAY_OK;
	case -3:
		return SAY_NOT_SUBMITTED;
	case -4:
		return SAY_INTERRUPTED;
	case -5:
		return SAY_WITHDRAWN;
	case -6:
		return SAY_DRAFT_LEFT;
	default:
		return SAY_NOT_TYPED;
	}
}
#endif

/* names NULL = every session. Exit status: 1 when any session failed. */
static int
do_say_verb(char **names, int nnames, char **words, int nwords, int json)
{
	struct slist sl = {0};
	const char *self = getenv("DCH_SESSION");
	char selfb[256], cb[256], path[1100], *text;
	pid_t *kids;
#ifdef HAVE_LIBGHOSTTY_VT
	sigset_t term, old;
#endif
	size_t len = 1;
	int k, c, st, live = 0, bad = 0, ws;

	for (k = 0; k < nwords; k++)
		len += strlen(words[k]) + 1;
	if (!(text = malloc(len)))
		return 1;
	for (text[0] = '\0', k = 0; k < nwords; k++)
		strcat(strcat(text, k ? " " : ""), words[k]);
	if (!text[strspn(text, " \t\r\n")])
	{
		fprintf(stderr, "dch: --say needs text after --\n");
		free(text);
		return 1;
	}
	ty_arm();
#ifdef HAVE_LIBGHOSTTY_VT
	ty_set(&term);
#endif
	if (self && *self)
		self = canon_name(self, selfb, sizeof selfb);
	if (!names)
	{
		list_sessions(&sl);
		names = sl.v;
		nnames = sl.n;
	}
	kids = calloc((size_t)nnames + 1, sizeof *kids);
	ty_kids = kids;
	ty_nkids = kids ? nnames : 0;
	for (k = 0; kids && k < nnames; k++)
	{
		struct cmd_info ci;
		struct crec r;

		session_sock_path(names[k], path, sizeof path);
		if (self && *self &&
		    strcmp(self, canon_name(names[k], cb, sizeof cb)) == 0)
			c = SAY_SELF;
		else if ((st = cmd_query(path, &ci)) == -4 && sl.v)
			continue;	/* --all: a stale socket, no session */
		else if (st == DCH_ST_UNSUP)
			c = SAY_OLD_MASTER;
		else if (st != 0)
			c = SAY_NO_SESSION;
		else
		{
			/* any status: a busy Claude queues the message. A
			** suspended one reads nothing: the shell in front would
			** (#282) */
			c = ci.pid <= 0 || claude_find(ci.pid, 1, &r) == 0
			    ? SAY_NOT_CLAUDE
			    : proc_fg(ci.pid, r.pid) == 0 ? SAY_NOT_FG : -1;
			cmd_info_free(&ci);
		}
#ifdef HAVE_LIBGHOSTTY_VT
		/* blocked over the fork: a signal now is forwarded to this
		** child too, once kids[k] holds it */
		if (c < 0)
			sigprocmask(SIG_BLOCK, &term, &old);
		if (c < 0 && (kids[k] = fork()) == 0)
		{
			ty_kids = NULL;
			ty_nkids = 0;
			sigprocmask(SIG_SETMASK, &old, NULL);
			_exit(SAY_EXIT + say_type(path, text, 1));
		}
		if (c < 0)
			sigprocmask(SIG_SETMASK, &old, NULL);
		if (c < 0 && kids[k] > 0)
		{
			live++;
			continue;
		}
		if (c < 0)
			c = SAY_NOT_TYPED;
#else
		if (c < 0)
			c = SAY_NO_SCREEN;	/* dch-lite: no mirror to gate on */
#endif
		say_print(names[k], c, json);
		bad |= c >= SAY_NO_SESSION;
	}
	for (; live > 0; live--)
	{
		pid_t p = wait(&ws);

		if (p < 0 && errno == EINTR)
		{
			live++;
			continue;
		}
		if (p < 0)
			break;
		for (k = 0; k < nnames && kids[k] != p; k++)
			;
		if (k == nnames)
		{
			live++;		/* not one of ours */
			continue;
		}
		c = WIFEXITED(ws) && WEXITSTATUS(ws) >= SAY_EXIT &&
		    WEXITSTATUS(ws) < SAY_EXIT + SAY_N
		    ? WEXITSTATUS(ws) - SAY_EXIT : SAY_NOT_TYPED;
		say_print(names[k], c, json);
		bad |= c >= SAY_NO_SESSION;
	}
	bad |= !kids;
	ty_nkids = 0;
	free(kids);
	free(text);
	slist_free(&sl);
	return bad ? 1 : 0;
}

/* `--run <n> <text...>` (#112): into a Claude Code session, text and Enter in
** one burst fold the Enter into the paste, so the text goes through --say's
** gate (empty composer, typed, Enter once it shows, re-Entered). A slash
** command is typed raw, as Claude's menu must see it. Anything else — a
** shell, another program, a master without a mirror, dch-lite, your own
** session — keeps the plain burst. Exit 1 with `dch: <n>: <reason> — <why>` when the gate fails. */
static int
do_run_verb(const char *name, char **words, int nwords)
{
#ifdef HAVE_LIBGHOSTTY_VT
	struct cmd_info ci;
	struct crec r;
	const char *self = getenv("DCH_SESSION");
	char selfb[256], cb[256], path[1100], *text;
	size_t len = 1;
	int k, c;

	/* Your own session keeps the burst: the gate would wait on the composer
	** of the agent that is running this very command. */
	if (self && *self && strcmp(canon_name(self, selfb, sizeof selfb),
	    canon_name(name, cb, sizeof cb)) == 0)
		goto burst;
	session_sock_path(name, path, sizeof path);
	if (nwords == 0 || cmd_query(path, &ci) != 0)
		goto burst;
	/* a suspended Claude (#282): what is in front gets the burst */
	c = ci.pid > 0 && claude_find(ci.pid, 1, &r) != 0 &&
	    proc_fg(ci.pid, r.pid) != 0;
	cmd_info_free(&ci);
	if (!c || claude_gate_q(path, 1) == CG_NOVT)
		goto burst;
	for (k = 0; k < nwords; k++)
		len += strlen(words[k]) + 1;
	if (!(text = malloc(len)))
		return 1;
	for (text[0] = '\0', k = 0; k < nwords; k++)
		strcat(strcat(text, k ? " " : ""), words[k]);
	ty_arm();
	c = say_type(path, text, text[0] != '/');
	free(text);
	if (c == SAY_OK)
		return 0;
	fprintf(stderr, "dch: %s: %s — %s\n", name, say_code[c], say_why[c]);
	return 1;
burst:
#endif
	return do_send_verb(name, words, nwords, 1);
}

/* --stop needs a 1.24.0 master: an older one (or one without a version
** sidecar) does not know <sock>.stopped and records the end as a death,
** which --revive --all would bring back. */
static int
stop_old_master(const char *name)
{
	const char *v = session_version(name);
	int a, b;

	return !v || sscanf(v, "%d.%d", &a, &b) != 2 ||
	       a < 1 || (a == 1 && b < 24);
}

/* `--stop <name>...|--all [--json]` (names NULL = every session). Exit
** status 1 when any session failed or was interrupted. */
static int
do_stop_verb(char **names, int nnames, int json)
{
	struct slist sl = {0};
	const char *self = getenv("DCH_SESSION");
	char selfb[256], cb[256], path[1100];
	int k, bad = 0;
#ifdef HAVE_LIBGHOSTTY_VT
	const char *pmsg = getenv("DCH_PAUSE_MSG"), *rmsg = getenv("DCH_RESUME_MSG");
	struct agent *ag;
	int na = 0;
#endif

	if (self && *self)
		self = canon_name(self, selfb, sizeof selfb);
	if (!names)
	{
		list_sessions(&sl);
		names = sl.v;
		nnames = sl.n;
	}
#ifdef HAVE_LIBGHOSTTY_VT
	if (!(ag = calloc((size_t)nnames + 1, sizeof *ag)))
	{
		slist_free(&sl);
		return 1;
	}
#endif
	for (k = 0; k < nnames; k++)
	{
		struct cmd_info ci;
		struct crec r;
		const char *why;
		long mpid;
		int c = -1, st = -1;

		session_sock_path(names[k], path, sizeof path);
		if (self && *self &&
		    strcmp(self, canon_name(names[k], cb, sizeof cb)) == 0)
			c = SAY_SELF;
		else if ((st = cmd_query(path, &ci)) == -4 && sl.v)
			continue;	/* --all: a stale socket, no session */
		else if (st == DCH_ST_UNSUP)
			c = SAY_OLD_MASTER;
		else if (st != 0)
			c = SAY_NO_SESSION;
		else if (stop_old_master(names[k]))
		{
			stop_print(names[k], "failed", "old_master", "its master "
			           "predates --stop (dch --restart it)", "", "", json);
			cmd_info_free(&ci);
			bad = 1;
			continue;
		}
		else if (!ci.state || strcmp(ci.state, "running") != 0)
		{
			/* --stop arms its own hold: someone else's owns the exit */
			stop_print(names[k], "skipped", "respawn_pending",
			           "a respawn is pending", "", "", json);
			cmd_info_free(&ci);
			continue;
		}
		else if (proc_depth((long)getpid(), ci.pid) >= 0)
			c = SAY_SELF;
		if (c == SAY_SELF && st == 0)
			cmd_info_free(&ci);
		if (c >= 0)
		{
			stop_print(names[k], c < SAY_NO_SESSION ? "skipped" : "failed",
			           say_code[c], say_why[c], "", "", json);
			bad |= c >= SAY_NO_SESSION;
			continue;
		}
		mpid = proc_ppid(ci.pid);
		if (claude_find(ci.pid, 1, &r) == 0)
		{
			int cp = claude_proc(ci.pid) > 0;

			cmd_info_free(&ci);
			if (cp)
			{
				/* ending it would hang up a Claude unpaused, its id
				** unknown: leave it */
				stop_print(names[k], "failed", "claude_unreadable", "a "
				           "claude runs in it, but no session record of "
				           "it can be read; left running", "", "", json);
				bad = 1;
				continue;
			}
			json_phase(names[k], "ending", json);
			why = stop_end(names[k], path, mpid, NULL);
			if (!why && stop_ok_norec(names[k], "", json))
				continue;
			stop_print(names[k], why ? "failed" : "ok",
			           why ? "end_failed" : "", why ? why : "", "", "",
			           json);
			bad |= why != NULL;
			continue;
		}
		if (proc_fg(ci.pid, r.pid) == 0)
		{
			/* the pause message and /exit would go to the shell in
			** front (#282) */
			cmd_info_free(&ci);
			stop_print(names[k], "failed", say_code[SAY_NOT_FG],
			           say_why[SAY_NOT_FG], "", "", json);
			bad = 1;
			continue;
		}
#ifdef HAVE_LIBGHOSTTY_VT
		if (claude_gate(path) != CG_NOVT)
		{
			struct agent *a = &ag[na++];

			snprintf(a->name, sizeof a->name, "%s", names[k]);
			snprintf(a->path, sizeof a->path, "%s", path);
			a->ci = ci;
			a->argv = ci.argv;
			a->argc = ci.argc;
			a->tok = dch_claude_tok(a->argv, a->argc);
			a->cpid = r.pid;
			snprintf(a->ver, sizeof a->ver, "%s", r.ver);
			a->mpid = mpid;
			a->stop = 1;
			a->json = json;
			continue;
		}
#endif
		/* nothing is typed without the mirror to gate on */
		cmd_info_free(&ci);
		stop_print(names[k], "skipped", say_code[SAY_NO_SCREEN],
		           say_why[SAY_NO_SCREEN], "", "", json);
	}
#ifdef HAVE_LIBGHOSTTY_VT
	if (na)
		bad |= ag_run(ag, na, pmsg && *pmsg ? pmsg : stop_pause_msg,
		              rmsg && *rmsg ? rmsg : stop_resume_msg) > 0;
	for (k = 0; k < na; k++)
	{
		cmd_info_free(&ag[k].ci);
		free(ag[k].ag.launch);
	}
	free(ag);
#endif
	slist_free(&sl);
	return bad ? 1 : 0;
}

/* ---- session history: --history, --revive (#042) -------------------- */

/* One record from dch_history_dir() (written by master.c's rec_*). */
struct hrec
{
	char file[300];		/* record basename without .json */
	char *body;		/* as stored, one line */
	char name[512], alias[600], cwd[PATH_MAX], end[192];
	char sid[64], aname[256], acwd[PATH_MAX];
	char acfg[1400];	/* agent's config_dir, "" = the default (#065) */
	long started, updated, ended;
	char **argv, **launch;	/* malloc'd blocks */
	int argc, launchc, live;
	int pinned;		/* <file>.pin beside it (dch --pin) */
	/* --revive */
	char as[512];
	int mode;		/* RV_* */
	long long deadline;
};

enum { RV_SKIP, RV_PLAIN, RV_ARGV, RV_SHELL };

static void
hrec_free(struct hrec *r, int n)
{
	int k;

	for (k = 0; k < n; k++)
	{
		free(r[k].body);
		free(r[k].argv);
		free(r[k].launch);
	}
	free(r);
}

/* Does a master answer on `name`? The view's o asks it too (#232). 1 with
** errno EAGAIN: it is there but stuck (#265), else errno 0 (#269). */
int
name_live(const char *name)
{
	char sp[1100];
	int s;

	session_sock_path(name, sp, sizeof sp);
	if ((s = control_connect_path(sp)) < 0)
		return errno == EAGAIN;	/* stuck is live (#265) */
	close(s);
	errno = 0;
	return 1;
}

/* An older master recorded claude's one-shot prompt (#138): write v[0..n)
** over the string array `key` (in `object`, else at the top) of *body, so
** --history --json does not show what a revive would not send. Left as it
** was when anything fails: hrec_load's arrays are cut either way. */
static void
hrec_put(char **body, const char *object, const char *key, char **v, int n)
{
	struct json_parser p;
	char *a = NULL, *nb = NULL, *q;
	size_t al = 0, cap;
	FILE *f;
	int o = 0, fl, k, ok = 1;

	if (dch_json_parse(&p, *body) < 0 ||
	    (object && ((o = dch_json_field(&p, 0, object)) < 0 ||
	                p.tok[o].kind != JSON_OBJECT)) ||
	    (fl = dch_json_field(&p, o, key)) < 0 ||
	    p.tok[fl].kind != JSON_ARRAY || !(f = open_memstream(&a, &al)))
	{
		dch_json_free(&p);
		return;
	}
	fputc('[', f);
	for (k = 0; k < n && ok; k++)
	{
		cap = strlen(v[k]) * 6 + 3;
		ok = (q = malloc(cap)) &&
		     dch_json_quote(q, cap, v[k]) != (size_t)-1;
		if (ok)
			fprintf(f, "%s%s", k ? "," : "", q);
		free(q);
	}
	fputc(']', f);
	ok = fclose(f) == 0 && ok;	/* al holds the size from here */
	cap = strlen(*body) + al + 1;
	if (ok && (nb = malloc(cap)))
	{
		/* the array's token runs from its '[' to just past its ']' */
		snprintf(nb, cap, "%.*s%s%s", p.tok[fl].start, *body, a,
		         *body + p.tok[fl].end);
		free(*body);
		*body = nb;
	}
	free(a);
	dch_json_free(&p);
}

/* Drop claude's one-shot prompt from a record's argv (and body). */
static void
hrec_strip(struct hrec *r, char **v, int *argc, const char *object,
           const char *key)
{
	char **s = v ? malloc(((size_t)*argc + 1) * sizeof *s) : NULL;
	char kind[16];
	int n, ag;

	/* a Claude agent was seen: a wrapped claude's prompt goes too (#141) */
	ag = dch_json_nested_string(r->body, "agent", "kind", kind,
	                            sizeof kind) == 0 && !strcmp(kind, "claude");
	if (s && (n = dch_claude_strip(v, *argc, s, ag)) < *argc)
	{
		/* v (dch_json_strv's block) has room: n < *argc */
		memcpy(v, s, ((size_t)n + 1) * sizeof *s);
		*argc = n;
		hrec_put(&r->body, object, key, v, n);
	}
	free(s);
}

static int
hrec_load(int dfd, const char *fn, struct hrec *r)
{
	struct stat st;
	size_t n = strlen(fn);
	ssize_t got;
	char sd[sizeof sock_dir], pin[sizeof r->file + 4], *fx;
	int fd;

	memset(r, 0, sizeof *r);
	/* dfd: dirfd's, which can fail */
	if (dfd < 0 || n < 6 || n - 5 >= sizeof r->file ||
	    strcmp(fn + n - 5, ".json"))
		return -1;
	snprintf(r->file, sizeof r->file, "%.*s", (int)(n - 5), fn);
	fd = openat(dfd, fn, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
	if (fd < 0)
		return -1;
	/* ours and nobody else's: a record is a command we would run */
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
	    st.st_uid != getuid() || (st.st_mode & 022) ||
	    st.st_size <= 0 || st.st_size > 1024 * 1024 ||
	    !(r->body = malloc((size_t)st.st_size + 1)))
	{
		close(fd);
		return -1;
	}
	got = read(fd, r->body, (size_t)st.st_size);
	close(fd);
	r->body[got > 0 ? got : 0] = '\0';
	chomp(r->body);
	/* an older master wrote bytes that are not UTF-8 as they came */
	if ((fx = dch_utf8_fix(r->body)))
	{
		free(r->body);
		r->body = fx;
	}
	/* the slug folds '/' and '-' together: only this socket dir's own */
	if (!dch_json_is_object(r->body) ||
	    dch_json_string(r->body, "sock_dir", sd, sizeof sd) < 0 ||
	    strcmp(sd, sock_dir) != 0 ||
	    dch_json_string(r->body, "name", r->name, sizeof r->name) < 0 ||
	    /* #203's name_ok, a leading '-' apart as there: an older dch's
	    ** -x is still listed, pinned and forgotten, never revived */
	    !name_ok(r->name + strspn(r->name, "-")) ||
	    dch_json_string(r->body, "cwd", r->cwd, sizeof r->cwd) < 0 ||
	    dch_json_string(r->body, "end", r->end, sizeof r->end) < 0 ||
	    !(r->argv = dch_json_strv(r->body, NULL, "argv", &r->argc)))
	{
		free(r->body);
		r->body = NULL;
		return -1;
	}
	(void)dch_json_string(r->body, "alias", r->alias, sizeof r->alias);
	(void)dch_json_long(r->body, "started", &r->started);
	(void)dch_json_long(r->body, "updated", &r->updated);
	(void)dch_json_long(r->body, "ended", &r->ended);
	if (dch_json_nested_string(r->body, "agent", "session_id", r->sid,
	                           sizeof r->sid) == 0 && r->sid[0])
	{
		(void)dch_json_nested_string(r->body, "agent", "name", r->aname,
		                             sizeof r->aname);
		(void)dch_json_nested_string(r->body, "agent", "cwd", r->acwd,
		                             sizeof r->acwd);
		if (dch_json_nested_string(r->body, "agent", "config_dir",
		                           r->acfg, sizeof r->acfg) < 0)
			r->acfg[0] = '\0';
		r->launch = dch_json_strv(r->body, "agent", "launch",
		                          &r->launchc);
		hrec_strip(r, r->launch, &r->launchc, "agent", "launch");
	}
	else
		r->sid[0] = '\0';
	hrec_strip(r, r->argv, &r->argc, NULL, "argv");
	/* A record with an end is over even if a new session took its name;
	** live means its own socket answers, so a record a cold start moved
	** aside (<name>.<ended>.json) is history beside the name's live one. */
	r->live = !r->end[0] && name_live(r->file);
	if (!r->end[0] && !r->live)
		snprintf(r->end, sizeof r->end, "vanished");
	snprintf(pin, sizeof pin, "%s.pin", r->file);
	r->pinned = fstatat(dfd, pin, &st, AT_SYMLINK_NOFOLLOW) == 0 &&
	            S_ISREG(st.st_mode);
	return 0;
}

#ifdef HAVE_LIBGHOSTTY_VT
/* --stop: did the session at `path` (its socket) leave a record that ended
** "stopped", at `since` or later, with an agent id? Into sid: 0, or -1. */
static int
stop_recorded(const char *path, long since, char *sid, size_t sidsz)
{
	const char *b = strrchr(path, '/');
	char dir[1024], fn[300];
	struct hrec h;
	size_t n;
	int dfd, ok;

	b = b ? b + 1 : path;
	n = strlen(b);
	if (n < 6 || strcmp(b + n - 5, ".sock") != 0 ||
	    snprintf(fn, sizeof fn, "%.*s.json", (int)(n - 5), b)
	    >= (int)sizeof fn ||
	    dch_history_dir(dir, sizeof dir, sock_dir, 0) < 0 ||
	    (dfd = open(dir, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)) < 0)
		return -1;
	ok = hrec_load(dfd, fn, &h) == 0 && strcmp(h.end, "stopped") == 0 &&
	     h.sid[0] && h.ended >= since;
	close(dfd);
	if (ok)
		snprintf(sid, sidsz, "%s", h.sid);
	free(h.body);
	free(h.argv);
	free(h.launch);
	return ok ? 0 : -1;
}
#endif

static long
hrec_when(const struct hrec *r)
{
	return r->ended ? r->ended : r->updated;
}

static int
hrec_cmp(const void *a, const void *b)
{
	long x = hrec_when(a), y = hrec_when(b);

	return x < y ? 1 : x > y ? -1 : 0;
}

/* Every readable record, newest end first. */
static int
history_load(struct hrec **out)
{
	char dir[1024];
	struct dirent *de;
	struct hrec *v = NULL, *nv;
	int n = 0, cap = 0;
	DIR *d;

	*out = NULL;
	if (dch_history_dir(dir, sizeof dir, sock_dir, 0) < 0 ||
	    !(d = opendir(dir)))
		return 0;
	while ((de = readdir(d)))
	{
		if (n == cap)
		{
			cap = cap ? cap * 2 : 16;
			if (!(nv = realloc(v, (size_t)cap * sizeof *v)))
				break;
			v = nv;
		}
		if (hrec_load(dirfd(d), de->d_name, &v[n]) == 0)
			n++;
	}
	closedir(d);
	if (n)
		qsort(v, (size_t)n, sizeof *v, hrec_cmp);
	*out = v;
	return n;
}

static void
put_argv(char **v)
{
	int k;

	for (k = 0; v && v[k]; k++)
		printf("%s%s", k ? " " : "", v[k]);
}

/* --history's remote records (#115), after the local ones: every field a
** peer sent escaped in text; in JSON the record as it sent it (C1, DEL and
** bidi controls as \u escapes) with "host" and "stale" added. */
static void
history_remote(int json, int first)
{
	struct dch_rrec *v;
	int n = dch_remote_history(&v, 1), k;
	char en[1100], ea[1100], ee[300], ec[1600], eg[1600], es[300], ef[1000];

	for (k = 0; k < n; k++)
	{
		const struct dch_rrec *r = &v[k];
		char when[32] = "-";
		time_t t = (time_t)r->when;
		size_t jl = strlen(r->json);

		if (json)
		{
			printf("%s%.*s,\"host\":", first ? "" : ",", (int)(jl - 1),
			       r->json);
			put_json_str(r->host);
			printf(",\"stale\":%s}", r->stale ? "true" : "false");
			first = 0;
			continue;
		}
		dch_remote_esc(en, sizeof en, r->name);
		dch_remote_esc(ea, sizeof ea, r->alias);
		dch_remote_esc(ee, sizeof ee, r->end[0] ? r->end : "vanished");
		dch_remote_esc(ec, sizeof ec, r->cwd);
		dch_remote_esc(eg, sizeof eg, r->argv);
		if (t)
			strftime(when, sizeof when, "%Y-%m-%d %H:%M",
			         localtime(&t));
		printf("%s:%s%s%s%s  %s  %s  %s  $ %s", r->host, en,
		       ea[0] ? " (" : "", ea, ea[0] ? ")" : "", when, ee, ec, eg);
		if (r->sid[0])
		{
			dch_remote_esc(ea, sizeof ea, r->aname);
			dch_remote_esc(es, sizeof es, r->sid);
			printf("  [claude %s%s%s]", ea, ea[0] ? " " : "", es);
		}
		if (r->pinned)
			fputs("  [pinned]", stdout);
		if (strcmp(r->file, r->name))
		{
			dch_remote_esc(ef, sizeof ef, r->file);
			printf("  [record %s]", ef);
		}
		puts(r->stale ? "  [stale]" : "");
	}
	dch_remote_hfree(v, n);
}

static int
do_history_verb(int json, int remote)
{
	struct hrec *r;
	int n = history_load(&r), k, first = 1;

	if (json)
		putchar('[');
	for (k = 0; k < n; k++)
	{
		char when[32] = "-", cb[256], fq[2048];
		time_t t = (time_t)hrec_when(&r[k]);
		const char *v;
		/* moved aside by a cold start under its name: the text output
		** names its file (--json names every record's file) */
		int aside = strcmp(r[k].file, canon_name(r[k].name, cb,
		                                         sizeof cb)) != 0, hasf;

		if (r[k].live)
			continue;
		if (json)
		{
			/* As stored, one line each; a dead end "" reads vanished,
			** a pinned one gains "pinned":true before its '}', every
			** one "file". */
			size_t bl;

			v = strstr(r[k].body, "\"end\":\"\"");
			fputs(first ? "" : ",", stdout);
			if (v)
				printf("%.*s\"end\":\"vanished\"",
				       (int)(v - r[k].body), r[k].body);
			v = v ? v + 8 : r[k].body;
			bl = strlen(v);
			hasf = dch_json_quote(fq, sizeof fq, r[k].file)
			       != (size_t)-1;
			if ((r[k].pinned || hasf) && bl && v[bl - 1] == '}')
				printf("%.*s%s%s%s}", (int)(bl - 1), v, r[k].pinned
				       ? ",\"pinned\":true" : "", hasf ? ",\"file\":"
				       : "", hasf ? fq : "");
			else
				fputs(v, stdout);
			first = 0;
			continue;
		}
		if (t)
			strftime(when, sizeof when, "%Y-%m-%d %H:%M", localtime(&t));
		printf("%s%s%s%s  %s  %s  %s  $ ", r[k].name,
		       r[k].alias[0] ? " (" : "", r[k].alias,
		       r[k].alias[0] ? ")" : "", when, r[k].end, r[k].cwd);
		put_argv(r[k].argv);
		if (r[k].sid[0])
			printf("  [claude %s%s%s]", r[k].aname,
			       r[k].aname[0] ? " " : "", r[k].sid);
		if (r[k].pinned)
			fputs("  [pinned]", stdout);
		if (aside)
			printf("  [record %s]", r[k].file);
		putchar('\n');
	}
	if (remote)
	{
		fflush(stdout);	/* the local lines first, a stderr note after */
		history_remote(json, first);
	}
	if (json)
		puts("]");
	hrec_free(r, n);
	return 0;
}

/* What --revive --all takes: a session that died (not exit 0, not dch -k
** or --stop),
** recently, after a real run. A test session that ends on a signal on
** purpose lives seconds; one from last week is history, not work in
** flight. Both stay in --history and revive by name. */
#define RV_ALL_MAX_AGE	(3L * 86400L)
#define RV_ALL_MIN_LIFE	(5L * 60L)

static int
rv_all_takes(const struct hrec *h)
{
	long seen = hrec_when(h);

	return !h->live && strcmp(h->end, "killed") != 0 &&
	       strcmp(h->end, "stopped") != 0 &&
	       strcmp(h->end, "exit 0") != 0 &&
	       seen >= (long)time(NULL) - RV_ALL_MAX_AGE &&
	       seen - h->started >= RV_ALL_MIN_LIFE;
}

/* ---- the session picker (#075): picker.c, hosted on a tty ----------- */
/*
** picker.c draws it and moves about it; here are its rows (pk_load), its
** actions (pk_do: every key past moving about) and the tty under it.
*/

#define PK_HIST_MAX 50

static void
pk_on_winch(int sig)
{
	(void)sig;
	pk_winch = 1;
}

/* Does s match the sidekick pattern: as the regex (mode 1), or as text,
** ignoring case (mode 2)? */
static int
pk_sidekick(int mode, regex_t *re, const char *pat, const char *s)
{
	size_t n = strlen(pat), k;

	if (!s || !mode)
		return 0;
	if (mode == 1)
		return regexec(re, s, 0, NULL, 0) == 0;
	for (; *s; s++)
	{
		for (k = 0; k < n && s[k] && tolower((unsigned char)s[k]) ==
		     tolower((unsigned char)pat[k]); k++)
			;
		if (k == n)
			return 1;
	}
	return 0;
}

/* The session named `name` on Sessions or Sidekicks, this host's; NULL
** when none runs. Their rows are named as the sockets are: canon_name's. */
static const struct pkrow *
pk_running(const struct pk *p, const char *name)
{
	char cb[256];
	const char *f = canon_name(name, cb, sizeof cb);
	int t, k;

	for (t = 0; t < 2; t++)
		for (k = 0; k < p->n[t]; k++)
			if (!p->r[t][k].remote && !strcmp(p->r[t][k].name, f))
				return &p->r[t][k];
	return NULL;
}

/* Every tab's rows afresh: the live sessions as --ls-json reads them, the
** ended records as --history does. */
void
pk_load(struct pk *p)
{
	struct slist sl = {0};
	struct hrec *h;
	char pat[256] = "-qa", sel[3][600];
	const char *home = getenv("HOME");
	size_t hl = home ? strlen(home) : 0;
	regex_t re;
	struct dch_rrow *rr = NULL;
	struct dch_rrec *rh = NULL;
	int mode = 0, k, j, t, nh, pin, nr, nrh, base = 0, cnt = 0;

	pk_selnames(p, sel);
	for (t = 0; t < 3; t++)
	{
		free(p->r[t]);
		p->r[t] = NULL;
		p->n[t] = 0;
	}
	/* set to nothing: no Sidekicks tab */
	(void)dch_config_get("sidekick_pattern", pat, sizeof pat);
	if (pat[0])
	{
		mode = regcomp(&re, pat, REG_EXTENDED | REG_ICASE | REG_NOSUB)
		       == 0 ? 1 : 2;
		/* a regex every name matches splits nothing: text, as an
		** invalid one is */
		if (mode == 1 && regexec(&re, "", 0, NULL, 0) == 0)
		{
			regfree(&re);
			mode = 2;
		}
	}
	list_sessions(&sl);
	load_aliases(&sl);
	load_harness_names(&sl);
	load_codex_names(&sl);
	dch_remote_hist_tab(!p->settings && p->tab == 2);
	nr = pk_remote ? dch_remote_rows(&rr, 0) : 0;
	p->r[0] = calloc((size_t)(sl.n + nr) + 1, sizeof **p->r);
	p->r[1] = calloc((size_t)sl.n + 1, sizeof **p->r);
	for (k = 0; p->r[0] && p->r[1] && k < sl.n; k++)
	{
		/* the harness title too: a "-qa" is often only Claude's name
		** for the session, the one the row shows (#086) */
		int side = pk_sidekick(mode, &re, pat, sl.v[k]) ||
		           pk_sidekick(mode, &re, pat, sl.alias[k]) ||
		           pk_sidekick(mode, &re, pat, sl.harness[k]);
		struct pkrow *w = &p->r[side][p->n[side]++];
		const char *meta;
		long mep;

		snprintf(w->name, sizeof w->name, "%s", sl.v[k]);
		snprintf(w->title, sizeof w->title, "%s", slist_label(&sl, k));
		snprintf(w->state, sizeof w->state, "%s", session_state(sl.v[k]));
		w->when = activity_epoch(sl.v[k]);
		w->agents = detect_agents(sl.v[k]);
		if ((meta = session_meta(sl.v[k], &mep)))
			(void)dch_meta_prs(meta, w->prs, sizeof w->prs, &w->merged);
	}
	if (mode == 1)
		regfree(&re);
	slist_free(&sl);
	/* the other hosts' sessions, by their host:name, as they last
	** answered (remote.c re-probes them behind the picker) */
	for (k = 0; p->r[0] && k < nr; k++)
		pk_rrow(&p->r[0][p->n[0]++], &rr[k]);
	free(rr);

	nh = history_load(&h);
	nrh = pk_remote ? dch_remote_history(&rh, 0) : 0;
	p->r[2] = calloc((size_t)(nh + nrh) + 1, sizeof **p->r);
	p->interrupted = 0;
	for (k = 0; k < nh; k++)
		p->interrupted += rv_all_takes(&h[k]);
	/* one row per name (#144): its newest record, pinned when any of
	** the name's is, live when any of the name's is; h is newest first */
	for (k = 0; p->r[2] && k < nh; k++)
	{
		struct pkrow *w;

		for (j = 0; j < p->n[2] && strcmp(p->r[2][j].sname,
		     h[k].name); j++)
			;
		if (j < p->n[2])
		{
			p->r[2][j].pinned |= h[k].pinned;
			p->r[2][j].live |= h[k].live;
			continue;
		}
		w = &p->r[2][p->n[2]++];
		snprintf(w->name, sizeof w->name, "%s", h[k].file);
		snprintf(w->sname, sizeof w->sname, "%s", h[k].name);
		/* alias, else the agent's session name, else the dch
		 * name: slist_label's order, and Prosper's (#097) */
		snprintf(w->title, sizeof w->title, "%s", h[k].alias[0]
		         ? h[k].alias : h[k].aname[0] ? h[k].aname
		         : h[k].name);
		snprintf(w->state, sizeof w->state, "%s", h[k].end);
		if (hl && strncmp(h[k].cwd, home, hl) == 0 &&
		    (h[k].cwd[hl] == '/' || !h[k].cwd[hl]))
			snprintf(w->cwd, sizeof w->cwd, "~%s", h[k].cwd + hl);
		else
			snprintf(w->cwd, sizeof w->cwd, "%s", h[k].cwd);
		w->when = hrec_when(&h[k]);
		w->pinned = h[k].pinned;
		w->live = h[k].live;
	}
	/* a name decides (#254): one with a session running (its row on
	** Sessions or Sidekicks, or a live record) says "running" and when
	** that session last had output; shown only when pinned, else it is
	** that session's tab's */
	for (k = pin = 0; k < p->n[2]; k++)
	{
		struct pkrow *w = &p->r[2][k];
		const struct pkrow *s = pk_running(p, w->sname);

		if (s || w->live)
		{
			w->live = 1;
			snprintf(w->state, sizeof w->state, "running");
			w->when = s ? s->when : w->when;
		}
		pin += w->pinned;
	}
	/* every pinned name, then the newest of the rest while the total is
	** under the cap */
	for (k = j = 0; k < p->n[2]; k++)
		if (p->r[2][k].pinned || (!p->r[2][k].live && pin < PK_HIST_MAX))
		{
			pin += !p->r[2][k].pinned;
			p->r[2][j++] = p->r[2][k];
		}
	p->n[2] = j;
	hrec_free(h, nh);
	/* the other hosts' (#115), as their host last answered: per host the
	** first PK_HIST_MAX names, each its newest record (the host's order),
	** never pinned here; host:file the row, host:name its name */
	for (k = 0; p->r[2] && k < nrh; k++)
	{
		const struct dch_rrec *x = &rh[k];
		char e[sizeof p->r[2]->title - DCH_RHOST_MAX];
		struct pkrow *w = &p->r[2][p->n[2]];

		if (!k || strcmp(x->host, rh[k - 1].host))
		{
			base = p->n[2];
			cnt = 0;
		}
		memset(w, 0, sizeof *w);	/* a slot the cap left over */
		snprintf(w->sname, sizeof w->sname, "%s:%s", x->host, x->name);
		for (j = base; j < p->n[2] && strcmp(p->r[2][j].sname, w->sname);
		     j++)
			;
		if (j < p->n[2] || cnt >= PK_HIST_MAX)
			continue;
		cnt++;
		p->n[2]++;
		snprintf(w->name, sizeof w->name, "%s:%s", x->host, x->file);
		dch_remote_esc(e, sizeof e, x->alias[0] ? x->alias : x->aname[0]
		               ? x->aname : x->name);
		snprintf(w->title, sizeof w->title, "%s:%s", x->host, e);
		dch_remote_esc(w->state, sizeof w->state, x->end[0] ? x->end
		               : "vanished");
		dch_remote_esc(w->cwd, sizeof w->cwd, x->cwd);
		w->when = x->when;
		w->remote = x->stale ? 2 : 1;
	}
	dch_remote_hfree(rh, nrh);
	pk_sort(p, sel);
}

/* The session the picker opened ended (#133): the next picker opens on
** History, on the record that end wrote: this name's, ended at `since` or
** later (an older one of the name ended before the attach), the newest.
** The master writes it as it exits, so it is waited for, up to 2s; none
** by then: History as it is, or the session's own row when it still
** answers (the master dropped the client, it did not end). */
static void
pk_ended(const char *name, long since)
{
	char f[sizeof ((struct hrec *)0)->file] = "";
	struct hrec *h;
	long best = 0;
	int k, n, tries;

	for (tries = 0; !f[0] && tries < 40; tries++)
	{
		if (tries)
			usleep(50 * 1000);
		n = history_load(&h);
		for (k = 0; k < n; k++)
			if (!h[k].live && h[k].ended >= since &&
			    h[k].ended > best && !strcmp(h[k].name, name))
			{
				best = h[k].ended;
				snprintf(f, sizeof f, "%s", h[k].file);
			}
		hrec_free(h, n);
	}
	if (!f[0] && name_live(name))
		pk_focus(name);
	else
		pk_focus_history(f);
}

/* ---- actions ---- */

/* An action: the dch words after argv[0] (NULL-terminated), what the
** status line says while it runs, the row it is about. */
/* One more word on a's argv: 0, or -1 when it does not fit. */
static int
pk_arg(struct pkact *a, const char *w)
{
	size_t off = 0, l = strlen(w) + 1;
	int n = 1;

	while (a->argv[n])
		n++;
	if (n > 1)
		off = (size_t)(a->argv[n - 1] - a->buf) + strlen(a->argv[n - 1]) + 1;
	if (n >= 11 || off + l > sizeof a->buf)
		return -1;
	memcpy(a->buf + off, w, l);
	a->argv[n] = a->buf + off;
	a->argv[n + 1] = NULL;
	return 0;
}

static void
pk_act(struct pkact *a, int tab, const char *target, const char *doing, ...)
{
	va_list ap;
	const char *w;

	memset(a, 0, sizeof *a);
	a->tab = tab;
	snprintf(a->target, sizeof a->target, "%s", target);
	snprintf(a->doing, sizeof a->doing, "%s", doing);
	a->argv[0] = dch_exe;
	va_start(ap, doing);
	while ((w = va_arg(ap, const char *)) && pk_arg(a, w) == 0)
		;
	va_end(ap);
}

static void
pk_start(struct pk *p, struct pkact *a)
{
	FILE *f;
	pid_t pid;

	/* gone since it was asked about: say so, run nothing */
	if (a->target[0] && pk_find(p, a->tab, a->target) < 0 &&
	    (a->tab == 2 || pk_find(p, !a->tab, a->target) < 0))
	{
		snprintf(p->status, sizeof p->status, "%s: no longer there",
		         a->target);
		return;
	}
	if (!(f = tmpfile()) || (pid = fork()) < 0)
	{
		snprintf(p->status, sizeof p->status, "%s: %s", a->doing,
		         strerror(errno));
		if (f)
			fclose(f);
		return;
	}
	if (pid == 0)
	{
		int nul = open("/dev/null", O_RDWR);

		if (nul >= 0)
			dup2(nul, 0);
		dup2(fileno(f), 1);
		dup2(fileno(f), 2);
		execvp(a->argv[0], a->argv);
		dprintf(2, "cannot run %s: %s\n", a->argv[0], strerror(errno));
		_exit(127);
	}
	p->run = *a;	/* its argv points into a; only exec needed it */
	p->pid = pid;
	p->out = f;
	p->status[0] = '\0';
}

/* The running action ended? Its last line is the result; --revive's, from
** its --json line, reads "Resumed as <name>". */
void
pk_reap(struct pk *p)
{
	char buf[8192], res[32] = "", as[600] = "", det[700] = "", *line, *next;
	const char *last = NULL;
	struct stat sb;
	off_t off = 0;
	ssize_t n;
	int st;

	if (p->pid <= 0 || waitpid(p->pid, &st, WNOHANG) != p->pid)
		return;
	p->pid = -1;
	if (fstat(fileno(p->out), &sb) == 0 &&
	    sb.st_size > (off_t)sizeof buf - 1)
		off = sb.st_size - (off_t)(sizeof buf - 1);
	n = pread(fileno(p->out), buf, sizeof buf - 1, off);
	fclose(p->out);
	p->out = NULL;
	buf[n > 0 ? n : 0] = '\0';
	for (line = buf; line; line = next)
	{
		if ((next = strchr(line, '\n')))
			*next++ = '\0';
		chomp(line);
		if (!*line)
			continue;
		last = line;
		if (p->run.revive && *line == '{' &&
		    dch_json_string(line, "result", res, sizeof res) == 0)
		{
			(void)dch_json_string(line, "as", as, sizeof as);
			(void)dch_json_string(line, "detail", det, sizeof det);
		}
	}
	if (res[0] && strcmp(res, "ok") == 0)
		snprintf(p->status, sizeof p->status, "Resumed as %s%s%s%s",
		         p->run.host, p->run.host[0] ? ":" : "", as,
		         p->run.nomsg ? " without the message" : "");
	else if (res[0])
		snprintf(p->status, sizeof p->status, "%s: %s", p->run.target,
		         det[0] ? det : res);
	else if (last)
		snprintf(p->status, sizeof p->status, "%.*s",
		         (int)sizeof p->status - 1, last);	/* display: cut ok */
	else
		snprintf(p->status, sizeof p->status, "%s: %s", p->run.doing,
		         WIFEXITED(st) && WEXITSTATUS(st) == 0 ? "done"
		         : "failed");
	/* o, Shift+Enter (#132): the session this revive made, by the name
	** it answered, attached by pk_run; gone already, the line stays */
	if (p->run.host[0])
	{
		/* on another host (#115): what it said, escaped; attached as
		** host:name, the way Enter on a Sessions row of it is */
		char e[sizeof p->status];

		dch_remote_esc(e, sizeof e, p->status);
		memcpy(p->status, e, sizeof e);
		if (res[0] && !strcmp(res, "ok") && p->run.go && as[0])
		{
			snprintf(p->go, sizeof p->go, "%s:%s", p->run.host, as);
			attach_route = 1;	/* a local host:name too (#248) */
		}
	}
	else if (res[0] && !strcmp(res, "ok") && p->run.go && as[0] &&
	         name_live(as))
		snprintf(p->go, sizeof p->go, "%s", as);
	p->loaded = 0;
}

static DCH_PRINTF(2, 3) void
pk_ask(struct pk *p, const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(p->status, sizeof p->status, fmt, ap);
	va_end(ap);
	p->asking = 1;
}

/* Rename: one cooked line on the status row, then raw again. */
static void
pk_rename(struct pk *p, int fd, const struct pkrow *w)
{
	char q[700], in[600], pos[32], *c;
	int rc;

	snprintf(pos, sizeof pos, "\033[%d;1H", p->status_row);
	tw(fd, pos);
	snprintf(q, sizeof q, "new name for %s (empty clears): ", w->title);
	if (prompt_line(fd, q, in, sizeof in) == 0)
	{
		for (c = in; *c; c++)
			if ((unsigned char)*c < 0x20 || *c == 0x7f)
				*c = ' ';
		if ((rc = write_alias(w->name, in)) == -2)
			snprintf(p->status, sizeof p->status, "%s: name looks like "
			         "a session_id", w->name);
		else if (rc < 0)
			snprintf(p->status, sizeof p->status, "%s: rename failed",
			         w->name);
		else
			snprintf(p->status, sizeof p->status, in[0]
			         ? "%s renamed" : "%s: alias cleared", w->name);
	}
	picker_raw_on(fd);
	pk_pn = 0;	/* the prompt drew over the frame: all of it again */
	p->loaded = 0;
}

/* Write config key k (s NULL: back to its default) and apply it at once:
** the sort, and a reload for the sidekick split. */
static void
pk_cfset(struct pk *p, int k, const char *s)
{
	const char *key = config_keys[k].key;

	if (config_set(k, s) < 0)
		snprintf(p->status, sizeof p->status, "%s: %s", key,
		         errno == EINVAL ? "one line, at most 900 bytes"
		         : strerror(errno));
	else if (s)
		snprintf(p->status, sizeof p->status, "%s = %s", key, s);
	else
		snprintf(p->status, sizeof p->status, "%s: default", key);
	p->byname = pk_cfbyname();
	p->loaded = 0;
}

/* The settings view's keys (#082), driven by config_keys: Enter toggles a
** bool (auto_allow on asks first), cycles a choice, or prompts for a line;
** u goes back to the default. 0: go on; -1: quit. */
static int
pk_cfdo(struct pk *p, int fd, int c)
{
	int k = p->cfsel, set;
	const char *ch = config_keys[k].choices, *sv, *m;
	char v[1024], nv[1024], q[1100], pos[32];

	switch (c)
	{
	case 3: case 4:
		return -1;
	case 'q': case ',': case PK_ESC:
		p->settings = 0;
		return 0;
	case 'j': case PK_DOWN:
		p->cfsel += p->cfsel < (int)NCONF - 1;
		return 0;
	case 'k': case PK_UP:
		p->cfsel -= p->cfsel > 0;
		return 0;
	case 'g': case PK_HOME:
		p->cfsel = 0;
		return 0;
	case 'G': case PK_END:
		p->cfsel = (int)NCONF - 1;
		return 0;
	case 'u':
		pk_cfset(p, k, NULL);
		return 0;
	case '\r': case '\n':
		break;
	default:
		return 0;
	}
	sv = config_show(k, v, sizeof v, &set);
	if (config_keys[k].is_bool && !strcmp(sv, "true"))
		pk_cfset(p, k, "false");
	else if (config_keys[k].is_bool && !strcmp(config_keys[k].key,
	                                           "auto_allow"))
	{
		p->askcfg = k + 1;
		pk_ask(p, "Turn auto_allow on? The master answers Claude Code's "
		       "plain permission prompts with Yes.");
	}
	else if (config_keys[k].is_bool)
		pk_cfset(p, k, "true");
	else if (ch)
	{
		/* the next choice, round to the first */
		m = config_choice(ch, sv);
		m = m && m[strcspn(m, "|")] ? m + strcspn(m, "|") + 1 : ch;
		snprintf(nv, sizeof nv, "%.*s", (int)strcspn(m, "|"), m);
		pk_cfset(p, k, nv);
	}
	else
	{
		/* one cooked line on the status row, as rename */
		snprintf(pos, sizeof pos, "\033[%d;1H", p->status_row);
		tw(fd, pos);
		snprintf(q, sizeof q, "new %s (^D keeps it): ",
		         config_keys[k].key);
		if (prompt_line(fd, q, nv, sizeof nv) == 0)
			pk_cfset(p, k, nv);
		picker_raw_on(fd);
		pk_pn = 0;
	}
	return 0;
}

/* The first free <name>-2, -3, ... when name is live; "" when it is not. */
static void
pk_free_name(const struct pk *p, const char *name, char *as, size_t asz)
{
	int k, j;

	as[0] = '\0';
	if (!name_live(name))
		return;
	for (k = 2; k < 1000; k++)
	{
		snprintf(as, asz, "%s-%d", name, k);
		for (j = 0; j < p->n[2] && strcmp(p->r[2][j].sname, as); j++)
			;
		if (j == p->n[2] && !name_live(as))
			return;
	}
}

/* What p and d act on: a History row is its name's (#144), so every record
** of the name; f, the shown record's file, when the name would read as a
** record file. */
static const char *
pk_recs(const struct pkrow *w, const char *f)
{
	size_t n = strlen(w->sname);

	return n && !(n > 5 && !strcmp(w->sname + n - 5, ".json"))
	       ? w->sname : f;
}

/* Enter, m or o on another host's History row (#115): --revive of its
** record there, by --host (a host only discovery knows has no host:name
** route) and the picker's --hosts. No --as: a live name there is that
** host's dch to refuse, and its line is the status. */
static void
pk_rrevive(struct pk *p, const struct pkrow *w, int c)
{
	const char *f = strchr(w->name, ':'), *hl = dch_remote_hosts();
	char d[800], rf[620];
	struct pkact *a = &p->ask;

	if (!f)
		return;
	snprintf(rf, sizeof rf, "%s.json", f + 1);
	snprintf(d, sizeof d, "resuming %s", w->title);
	pk_act(a, 2, w->name, d, "--revive", rf, "--json", NULL);
	snprintf(a->host, sizeof a->host, "%.*s", (int)(f - w->name), w->name);
	if (pk_arg(a, "--host") < 0 || pk_arg(a, a->host) < 0 ||
	    (hl && (pk_arg(a, "--hosts") < 0 || pk_arg(a, hl) < 0)) ||
	    (c == 'm' && pk_arg(a, "--no-message") < 0))
	{
		snprintf(p->status, sizeof p->status, "%s: too long for the "
		         "picker; dch --revive on the command line", w->title);
		return;
	}
	a->revive = 1;
	a->nomsg = c == 'm';
	a->go = c == 'o';
	pk_start(p, a);
}

static int pk_xray(struct pk *p, int fd, const char *name, int record,
                   int remote,
                   char *out, size_t outsz);

/* One key. 1: out holds the session to attach; -1: quit; 0: go on. */
int
pk_do(struct pk *p, int fd, int c, char *out, size_t outsz)
{
	int t = p->tab, *sel = &p->sel[t], k;
	const struct pkrow *w = *sel < p->n[t] ? &p->r[t][*sel] : NULL;
	char d[800], f[620], as[600];
	const char *nm;

	attach_route = 0;	/* a revive's, its name cut by pk_run (#248) */
	if (p->asking)
	{
		p->asking = 0;
		if (c != 'y' && c != 'Y')
			snprintf(p->status, sizeof p->status, "cancelled");
		else if (p->askcfg)
			pk_cfset(p, p->askcfg - 1, "true");
		else
			pk_start(p, &p->ask);
		p->askcfg = 0;
		return 0;
	}
	if (p->settings)
		return pk_cfdo(p, fd, c);
	if ((k = pk_nav(p, c)))
		return k < 0 ? -1 : 0;
	/* Shift+Enter: Enter; in History, o, only where the pick is attached:
	** -k and -d would kill or detach what it revived */
	if (c == PK_SENTER)
		c = t < 2 ? '\r' : 'o';
	if (c == 'o' && t == 2 && !p->opens)
	{
		snprintf(p->status, sizeof p->status, "o opens a session: from "
		         "dch -l or the switch");
		return 0;
	}
	switch (c)
	{
	case ',':
		p->settings = 1;
		p->status[0] = '\0';
		return 0;
	case 's':
		if (t < 2)
		{
			char names[3][600];

			pk_selnames(p, names);
			p->byname = !p->byname;
			pk_sort(p, names);
			if (config_set(config_key("picker_sort"), p->byname
			               ? "name" : "activity") < 0)
				snprintf(p->status, sizeof p->status, "sort: not "
				         "saved (%s)", strerror(errno));
		}
		return 0;
	}
	if (c != '\r' && c != '\n' && c != 'r' && c > 0 && c < 256 &&
	    strchr("RSXabBmIpdo", c) && p->pid > 0)
	{
		snprintf(p->status, sizeof p->status, "busy, one at a time");
		return 0;
	}
	if (t < 2 && (c == 'b' || c == 'B'))
	{
		pk_act(&p->ask, t, "", c == 'b' ? "restarting stale agents"
		       : "restarting all agents", "--restart", "--all",
		       "--include-agents", c == 'B' ? "-f" : NULL, NULL);
		pk_ask(p, c == 'b' ? "Restart stale agents? Each pauses for up "
		       "to minutes.%s" : "Restart all agents (force)? Each "
		       "pauses for up to minutes.%s", "");
		return 0;
	}
	if (t == 2 && c == 'I')
	{
		if (!p->interrupted)
			snprintf(p->status, sizeof p->status, "nothing "
			         "interrupted to resume");
		else
		{
			snprintf(d, sizeof d, "%d", p->interrupted);
			pk_act(&p->ask, 2, "", "resuming interrupted",
			       "--revive", "--all", NULL);
			pk_ask(p, "Resume interrupted (%s)?", d);
		}
		return 0;
	}
	if (!w || (c > 0 && c < 256 && !strchr("\r\nrRSxXapmdo", c)))
		return 0;
	/* dch would read a name that starts with '-' as a flag: Enter, X, p
	** and d pass it after `--` or need no flag; no such name is made or
	** resumed (#162) */
	if (w->name[0] == '-' && !(t == 2 ? c == 'p' || c == 'd' : c == 'X' ||
	                           c == '\r' || c == '\n'))
	{
		if (t == 2)
			snprintf(p->status, sizeof p->status, "%s: a name "
			         "starting with '-' can't be resumed; p pins, d "
			         "removes", w->name);
		else
			snprintf(p->status, sizeof p->status, "%s: a name "
			         "starting with '-'; Enter %s", w->name, p->opens
			         ? "attaches, X kills" : *pk_header == 'k' ?
			         "kills, X asks first" : "detaches, X kills");
		return 0;
	}
	if (c == 'x')
	{
		/* the X-ray view (#175): a live session by name, a History row
		** by its record, a running one's (#254) by the session; a remote
		** one's on its host (#188) */
		return pk_xray(p, fd, t == 2 && w->live ? w->sname : w->name,
		               t == 2 && !w->live, w->remote, out, outsz);
	}
	/* ponytail: a remote row only attaches; kill, rename, restart and
	** the rest need ops-gated frames and the same confirmation (#087).
	** A remote History row (#115) only resumes, there: Enter, m, o. */
	if (w->remote)
	{
		if (t < 2 && (c == '\r' || c == '\n') &&
		    (size_t)snprintf(out, outsz, "%s", w->name) < outsz)
		{
			attach_route = 1;	/* a local host:name too (#210) */
			return 1;
		}
		if (t == 2 && (c == '\r' || c == '\n' || c == 'm' || c == 'o'))
			pk_rrevive(p, w, c);
		else
			snprintf(p->status, sizeof p->status, t == 2 ? "%s: on "
			         "another host; Enter resumes it there, p and d "
			         "are for this host's" : "%s: a remote session; "
			         "Enter attaches and x opens its X-ray, nothing "
			         "else", w->title);
		return 0;
	}
	/* a running name's History row (#254): Enter and o attach the
	** session, as its Sessions row does; nothing resumes or removes it */
	if (t == 2 && w->live && c > 0 && c < 256 && strchr("\r\nomd", c))
	{
		int go = c != 'm' && c != 'd' && p->opens;

		if (go && !name_live(w->sname))
		{
			snprintf(p->status, sizeof p->status, "%s: no longer "
			         "running", w->title);
			p->loaded = 0;
		}
		else if (go && errno == EAGAIN)	/* stuck: no attach (#269) */
			snprintf(p->status, sizeof p->status, STUCK_MSG,
			         w->title);
		else if (go && (size_t)snprintf(out, outsz, "%s", w->sname) <
		         outsz)
		{
			attach_route = -1;	/* this host's, as the row is */
			return 1;
		}
		else
			snprintf(p->status, sizeof p->status, "%s is running; %s",
			         w->title, c == 'd' ? "d removes it once it has "
			         "ended" : p->opens ? "Enter attaches it" : "attach "
			         "it from dch -l or the switch");
		return 0;
	}
	if (t < 2)
		switch (c)
		{
		case '\r': case '\n':
			if (!name_live(w->name))
			{
				snprintf(p->status, sizeof p->status,
				         "%s: no longer there", w->title);
				p->loaded = 0;
				return 0;
			}
			/* stuck: no attach; -k and -d still act (#269) */
			if (p->opens && errno == EAGAIN)
			{
				snprintf(p->status, sizeof p->status, STUCK_MSG,
				         w->title);
				return 0;
			}
			/* a cut name is another session's */
			if ((size_t)snprintf(out, outsz, "%s", w->name) < outsz)
				return 1;
			snprintf(p->status, sizeof p->status, "name too long");
			return 0;
		case 'r':
			pk_rename(p, fd, w);
			return 0;
		case 'R':
			snprintf(d, sizeof d, "restarting %s", w->title);
			pk_act(&p->ask, t, w->name, d, "--restart", w->name,
			       "--include-agents", "-f", NULL);
			pk_ask(p, "Restart the agent in %s?", w->title);
			return 0;
		case 'S':
			snprintf(d, sizeof d, "stopping %s", w->title);
			pk_act(&p->ask, t, w->name, d, "--stop", w->name, NULL);
			pk_ask(p, "Stop %s? Claude Code exits; resume it from "
			       "History.", w->title);
			return 0;
		case 'X':
			snprintf(d, sizeof d, "killing %s", w->title);
			pk_act(&p->ask, t, w->name, d, "-k", "--", w->name, NULL);
			pk_ask(p, "Kill %s?", w->title);
			return 0;
		case 'a':
			if (strcmp(w->state, "blocked") != 0)
			{
				snprintf(p->status, sizeof p->status, "%s is not "
				         "waiting for input", w->title);
				return 0;
			}
			/* "1" only on a plain permission prompt, by the
			** auto-allow rule: on a question form it would pick
			** an answer the user never saw */
			{
				char *raw = malloc(DET_CAP + 1), *id = NULL,
				     *pt = NULL;
				int n = raw && make_sock_path(w->name) == 0 ?
				        fetch_screen(sock_path, raw, DET_CAP,
				                     DET_DEADLINE_MS, 0, 0, NULL) : -1;
				int ok = n > 0 &&
				         (raw[n] = '\0', dch_allow_prompt(raw,
				                                            &id,
				                                            &pt));

				if (ok)
					snprintf(d, sizeof d, "%s", pt);
				free(raw);
				free(id);
				free(pt);
				if (!ok)
				{
					/* -k and -d: their Enter kills or
					** detaches, it answers nothing */
					snprintf(p->status, sizeof p->status,
					         "%s: not a plain Yes/No permission "
					         "prompt; %s", w->title, p->opens ?
					         "attach (Enter) to answer" :
					         "answer it from dch -l");
					return 0;
				}
			}
			pk_ask(p, "Allow %s (presses 1, Yes)?", d);
			snprintf(d, sizeof d, "allowing %s", w->title);
			pk_act(&p->ask, t, w->name, d, "--keys", w->name, "1",
			       NULL);
			return 0;
		}
	if (t < 2)
		return 0;
	snprintf(f, sizeof f, "%s.json", w->name);	/* this one record */
	switch (c)
	{
	case '\r': case '\n': case 'm': case 'o':
		pk_free_name(p, w->sname, as, sizeof as);
		nm = c == 'm' ? "--no-message" : NULL;
		snprintf(d, sizeof d, "resuming %s", w->title);
		if (as[0])
			pk_act(&p->ask, 2, w->name, d, "--revive", f, "--as", as,
			       "--json", nm, NULL);
		else
			pk_act(&p->ask, 2, w->name, d, "--revive", f, "--json", nm,
			       NULL);
		p->ask.revive = 1;
		p->ask.nomsg = c == 'm';
		p->ask.go = c == 'o';	/* and attach once it is up (#132) */
		pk_start(p, &p->ask);
		return 0;
	case 'p':
		snprintf(d, sizeof d, "%s %s", w->pinned ? "unpinning"
		         : "pinning", w->title);
		pk_act(&p->ask, 2, w->name, d, w->pinned ? "--unpin" : "--pin",
		       "--", pk_recs(w, f), NULL);
		pk_start(p, &p->ask);
		return 0;
	case 'd':
		if (w->pinned)
		{
			snprintf(p->status, sizeof p->status, "%s is pinned; "
			         "unpin it first", w->title);
			return 0;
		}
		snprintf(d, sizeof d, "removing %s", w->title);
		pk_act(&p->ask, 2, w->name, d, "--forget", "--", pk_recs(w, f),
		       NULL);
		/* a live session of the name: its record is not History's, and
		** --forget <name> would fail on it; the ended records' files */
		if (pk_recs(w, f) == w->sname && name_live(w->sname))
		{
			struct hrec *h;
			int nh = history_load(&h);

			pk_act(&p->ask, 2, w->name, d, "--forget", "--", NULL);
			/* shortcut: as many as an action's argv holds (8); the
			** rest stay a row for the next d */
			for (k = 0; k < nh; k++)
				if (!name_live(h[k].file) &&
				    !strcmp(h[k].name, w->sname) &&
				    snprintf(f, sizeof f, "%s.json", h[k].file) <
				    (int)sizeof f && pk_arg(&p->ask, f) < 0)
					break;
			hrec_free(h, nh);
		}
		pk_ask(p, "Remove %s from History?", w->title);
		return 0;
	}
	return 0;
}

/* ---- the X-ray view's host (#175; xview.c) ------------------------- */

static int xray_main(int argc, char **argv);
static int name_routes_remote(const char *n);
/* An X-ray view of a picker row or an attach: 1 remote, -1 local, as the
** row is, not as its name routes (a local name may be host:name; QA 1.57.0
** LOW-R2). The view's children inherit it. 0: by the name. */
static int xv_route;

/* xview.c's engine call: xray_main in a forked child, as `dch --xray <av>
** --json` runs it (av[0] "--read": `dch --read host:name ...` over ctl),
** stdout and stderr into pipes. No exec: the same image,
** so the view and --json share every line of the engine. The child keeps
** no handler of the view's and lets go of the terminal. */
pid_t
xv_spawn(int fd, int ac, char **av, int *out, int *err)
{
	int po[2], pe[2];
	sigset_t s, o;
	pid_t pid;

	if (pipe(po) < 0)
		return -1;
	if (pipe(pe) < 0)
	{
		close(po[0]);
		close(po[1]);
		return -1;
	}
	fflush(stdout);
	fflush(stderr);
	/* blocked over the fork: a SIGTERM from xv_stop before the resets
	** would run picker_sigint here, which restores the view's terminal
	** and xv_kills its siblings (QA 1.58.0 MED-V4); after them, one
	** pending kills the child by its default action */
	sigemptyset(&s);
	sigaddset(&s, SIGINT);
	sigaddset(&s, SIGTERM);
	sigaddset(&s, SIGHUP);
	sigprocmask(SIG_BLOCK, &s, &o);
	if ((pid = fork()) == 0)
	{
		int rc;

		signal(SIGINT, SIG_DFL);
		signal(SIGTERM, SIG_DFL);
		signal(SIGHUP, SIG_DFL);
		signal(SIGWINCH, SIG_DFL);
		signal(SIGCONT, SIG_DFL);	/* xv_run's (#255) */
		sigprocmask(SIG_SETMASK, &o, NULL);
		picker_tty_fd = -1;
		close(fd);
		if (dup2(po[1], STDOUT_FILENO) < 0 || dup2(pe[1], STDERR_FILENO) < 0)
			_exit(3);
		close(po[0]);
		close(po[1]);
		close(pe[0]);
		close(pe[1]);
		/* a remote screen (#188, #294): ctl --read host:name --ansi,
		** its own connection, as the view's queries */
		rc = !strcmp(av[0], "--read") ? dch_remote_ctl(NULL, ac, av, NULL)
		     : xray_main(ac, av);
		fflush(stdout);
		_exit(rc);
	}
	sigprocmask(SIG_SETMASK, &o, NULL);
	close(po[1]);
	close(pe[1]);
	if (pid < 0)
	{
		close(po[0]);
		close(pe[0]);
		return -1;
	}
	*out = po[0];
	*err = pe[0];
	return pid;
}

/* n names a remote session (host:name, #188): the view reads its screen
** over ctl. */
int
xv_remote(const char *n)
{
	return name_routes_remote(n);
}

/* A live session's visible screen, plain (Refs' on screen now). sock_path
** is kept: the switch's picker runs inside an attach. */
int
xv_screen(const char *name, char *buf, size_t cap)
{
	char keep[sizeof sock_path];
	int n = -1;

	if (cap < DET_CAP + 1 || !name_ok(name))
		return -1;
	memcpy(keep, sock_path, sizeof keep);
	if (make_sock_path(name) == 0)
		n = fetch_screen(sock_path, buf, DET_CAP, DET_DEADLINE_MS, 0, 0, NULL);
	memcpy(sock_path, keep, sizeof keep);
	if (n >= 0)
		buf[n] = '\0';
	return n;
}

/* The picker's x: the view on its terminal, as it is (raw, alternate
** screen); the picker drawn whole after. The view's o (#194): 1, out
** holds the session to attach, as Enter's; not in the -k and -d pickers. */
static int
pk_xray(struct pk *p, int fd, const char *name, int record, int remote,
        char *out, size_t outsz)
{
	char n[620], err[400];
	int rc;

	if (record)
		snprintf(n, sizeof n, "%s.json", name);
	else
		snprintf(n, sizeof n, "%s", name);
	xv_route = remote ? 1 : -1;
	rc = xv_run(fd, n, err, sizeof err);
	xv_route = 0;
	pk_pn = 0;
	if (rc != 0)
		snprintf(p->status, sizeof p->status, "%s", err);
	else if (xv_go[0] && !p->opens)
		snprintf(p->status, sizeof p->status, "o opens a session: from "
		         "dch -l or the switch");
	else if (xv_go[0] &&
	         (size_t)snprintf(out, outsz, "%s", xv_go) < outsz)
	{
		attach_route = remote ? 1 : -1;	/* the view's own (#210) */
		attach_only = 1;	/* ended since o: refused, never a new one */
		return 1;
	}
	return 0;
}

/* Input on fd within ms? select, not poll: macOS poll() answers POLLNVAL
** for a tty. -1 when a signal (a resize) cut it short. */
int
pk_wait(int fd, long ms)
{
	struct timeval tv = { ms / 1000, (int)(ms % 1000) * 1000 };
	fd_set rs;

	FD_ZERO(&rs);
	FD_SET(fd, &rs);
	return select(fd + 1, &rs, NULL, NULL, &tv);
}

int
pk_rd(int fd, unsigned char *c)
{
	ssize_t r;

	do
		r = read(fd, c, 1);
	while (r < 0 && errno == EINTR);
	return r == 1 ? 0 : -1;
}

void
pk_size(int fd, int *cols, int *rows)
{
	struct winsize ws;

	*cols = *rows = 0;
	if (ioctl(fd, TIOCGWINSZ, &ws) == 0)
	{
		*cols = ws.ws_col;
		*rows = ws.ws_row;
	}
}

void
pk_write(int fd, const void *b, size_t n)
{
	(void)!write(fd, b, n);
}

/* opens: the caller attaches what is picked, so History's o and
** Shift+Enter may revive and hand it back (#132); -k and -d must not. */
static int
pick_session(const char *header, char *out, size_t outsz, int opens)
{
	struct sigaction sa, owinch;
	struct pk p;
	int fd = open("/dev/tty", O_RDWR | O_CLOEXEC), rc = 1, k;

	if (fd < 0)
	{
		/* Headless: list names so the caller can still see them. */
		struct slist sl = {0};

		list_sessions(&sl);
		if (!sl.n)
			fprintf(stderr, "no sessions\n");
		load_aliases(&sl);
		load_harness_names(&sl);
		load_codex_names(&sl);
		slist_sort(&sl);
		for (k = 0; k < sl.n; k++)
		{
			char row[1300];

			session_display_name(&sl, k, row, sizeof(row));
			puts(row);
		}
		slist_free(&sl);
		return 1;
	}
	if (pk_open(&p, pk_remote, 0) < 0 ||
	    tcgetattr(fd, &picker_saved_term) < 0)
	{
		close(fd);
		goto done;
	}
	p.opens = opens;
	pk_header = header;
	picker_tty_fd = fd;
	picker_term_saved = 1;
	signal(SIGINT, picker_sigint);
	signal(SIGTERM, picker_sigint);
	signal(SIGHUP, picker_sigint);
	/* no SA_RESTART: a resize wakes the poll below at once */
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = pk_on_winch;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGWINCH, &sa, &owinch);
	picker_raw_on(fd);
	picker_alt = 1;
	/* the kitty protocol's disambiguate flag, pushed on the alternate
	** screen and popped there (picker_restore): Shift+Enter is CSI 13;2u,
	** Enter stays \r (#132) */
	tw(fd, "\033[?1049h\033[?7l\033[H\033[2J\033[>1u");
	rc = pk_run(&p, fd, header, out, outsz);
	picker_restore();
	signal(SIGINT, SIG_DFL);
	signal(SIGTERM, SIG_DFL);
	signal(SIGHUP, SIG_DFL);
	sigaction(SIGWINCH, &owinch, NULL);
done:
	if (pk_remote)
		dch_remote_stop();	/* its background probe, reaped */
	/* an action still running carries on; only its result goes unread */
	if (p.out)
		fclose(p.out);
	for (k = 0; k < 3; k++)
		free(p.r[k]);
	return rc;
}

/* v[0..n) as one shell command line, each word quoted when it needs it. */
static char *
shell_line(const char *cd, char **v, int n)
{
	size_t cap = cd ? strlen(cd) * 4 + 16 : 1;
	char *o, *p;
	int k;

	for (k = 0; k < n; k++)
		cap += strlen(v[k]) * 4 + 3;
	if (!(o = p = malloc(cap)))
		return NULL;
	for (k = cd ? -1 : 0; k < n; k++)
	{
		const char *w = k < 0 ? cd : v[k], *c;

		if (p > o)
			*p++ = ' ';
		if (k < 0)
			p += snprintf(p, cap - (size_t)(p - o), "cd ");
		/* a leading '=' is zsh's =cmd expansion: quote it */
		if (*w && *w != '=' && strspn(w, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
		                 "0123456789_./:=@%+,-") == strlen(w))
			p += snprintf(p, cap - (size_t)(p - o), "%s", w);
		else
		{
			*p++ = '\'';
			for (c = w; *c; c++)
				if (*c == '\'')
					p += snprintf(p, cap - (size_t)(p - o), "'\\''");
				else
					*p++ = *c;
			*p++ = '\'';
		}
		if (k < 0)
			p += snprintf(p, cap - (size_t)(p - o), " &&");
	}
	*p = '\0';
	return o;
}

/* The calling Claude Code session's identity, never handed to a revived
** one: --revive is often run from inside another agent. */
static const char *const rv_strip[] = {
	"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
	"CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
	"CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
	"CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID", "CLAUDE_EFFORT",
	/* the caller's IDE link and its hook files (E1, #067) */
	"CLAUDE_CODE_SSE_PORT", "CLAUDE_ENV_FILE", "CLAUDE_PROJECT_DIR",
};

/* What Claude Code sets for its own tool commands, never for a session of
** the user's (E1, #067): dropped when the caller is Claude Code
** (CLAUDECODE), and the login shell (login_env) puts back any the user's
** rc really sets. GIT_EDITOR=true would make a later `git commit` with no
** -m abort. */
static const char *const rv_harness[] = {
	"GIT_EDITOR", "AI_AGENT", "COREPACK_ENABLE_AUTO_PIN",
	"NoDefaultCurrentDirectoryInExePath",
};

/* Drop the calling Claude Code session's identity (and, when the caller is
** Claude Code, its harness vars), and default LC_TERMINAL, before a
** client starts a new master:
** --revive, --spawn and a plain `dch <new>` are often run from inside an
** agent (#067, #111). Callers apply --env after, so an explicit one wins. */
static void
strip_caller_agent(void)
{
	size_t k;

	if (getenv("CLAUDECODE"))
		for (k = 0; k < sizeof rv_harness / sizeof rv_harness[0]; k++)
			unsetenv(rv_harness[k]);
	for (k = 0; k < sizeof rv_strip / sizeof rv_strip[0]; k++)
		unsetenv(rv_strip[k]);
	/* dch's mirror keeps OSC 8 (#069), but under TERM=xterm-256color with
	** no TERM_PROGRAM, Claude Code's hyperlink check fails and it strips the
	** statusline's links. Claude Code also takes LC_TERMINAL=ghostty (its
	** check's last resort, past isTTY); supports-hyperlinks and the like
	** never read it, so Node CLIs keep OSC 8 out of pipes and files, as
	** FORCE_HYPERLINK=1 did not (#122). A caller's own value wins. */
	setenv("LC_TERMINAL", "ghostty", 0);
}

/* Take the environment the user's login shell builds (~/.zshrc and the
** rest) on top of ours: a revive run by a GUI app or a daemon would hand its
** sessions that app's PATH, without what the rc files add (gcloud, kubectl
** wrappers). DCH_* and the shell's own bookkeeping stay ours. Best effort:
** a shell that fails or hangs (DCH_LOGIN_ENV_MS) changes nothing;
** DCH_LOGIN_ENV=0 skips it. */
static void
login_env(void)
{
	const char *sh = getenv("SHELL"), *off = getenv("DCH_LOGIN_ENV");
	const char *td = getenv("TMPDIR");
	long long until = wall_ms() + env_ms("DCH_LOGIN_ENV_MS", 10000);
	char tmp[1024], *buf = NULL, *e;
	struct stat sb;
	int fd, st = -1;
	pid_t pid, w = 0;

	if (!sh || *sh != '/' || (off && strcmp(off, "0") == 0))
		return;
	snprintf(tmp, sizeof tmp, "%s/dch-env.XXXXXX", td && *td ? td : "/tmp");
	if ((fd = mkstemp(tmp)) < 0)
		return;
	/* The shell writes the file and exits; waiting on the exit, not on
	** EOF, so a daemon the rc files start cannot hold us. */
	if ((pid = fork()) == 0)
	{
		int nul = open("/dev/null", O_RDWR);

		/* no controlling terminal: an interactive shell must not take
		** over the caller's job control */
		setsid();
		if (nul >= 0)
		{
			dup2(nul, 0);
			dup2(nul, 1);
			dup2(nul, 2);
		}
		execl(sh, sh, "-l", "-i", "-c", "env -0 >| \"$1\"", sh, tmp,
		      (char *)NULL);
		_exit(127);
	}
	while (pid > 0 && (w = waitpid(pid, &st, WNOHANG)) == 0 &&
	       wall_ms() < until)
		poll(NULL, 0, 20);
	if (pid > 0 && w == 0)
	{
		kill(-pid, SIGKILL);
		kill(pid, SIGKILL);
		waitpid(pid, &st, 0);
		st = -1;
	}
	if (w > 0 && WIFEXITED(st) && WEXITSTATUS(st) == 0 &&
	    fstat(fd, &sb) == 0 && sb.st_size > 0 &&
	    (buf = malloc((size_t)sb.st_size + 1)) &&
	    pread(fd, buf, (size_t)sb.st_size, 0) == sb.st_size)
	{
		buf[sb.st_size] = '\0';
		for (e = buf; e < buf + sb.st_size; e += strlen(e) + 1)
		{
			char *eq = strchr(e, '=');

			if (!eq || eq == e || strncmp(e, "DCH_", 4) == 0)
				continue;
			*eq = '\0';
			if (strcmp(e, "PWD") && strcmp(e, "OLDPWD") &&
			    strcmp(e, "SHLVL") && strcmp(e, "_"))
				setenv(e, eq + 1, 1);
		}
	}
	free(buf);
	close(fd);
	unlink(tmp);
}

static int do_spawn_verb(char *exe, const char *name, int cols, int rows,
                         char **inner_argv, int inner_argc, int quiet);

/* --revive --json's "running" (#182): does session `as` run its agent
** now? Its master answers and a live Claude record sits under its child,
** as revive_message and the #064 guard probe it. A child started as the
** agent itself (argv) counts before Claude has written that record. */
static int
rv_running(const char *as, int argv)
{
	char path[1100];
	struct cmd_info ci;
	struct crec c;
	int on;

	if (!name_ok(as))
		return 0;	/* a path-like name: no socket outside the store */
	session_sock_path(as, path, sizeof path);
	if (cmd_query(path, &ci) != 0)
		return 0;
	on = ci.pid > 0 && (argv || claude_find(ci.pid, 1, &c) > 0);
	cmd_info_free(&ci);
	return on;
}

/* --revive's final --json line without a record to revive (#182). */
static void
rv_json_final(const char *name, const char *as, const char *res,
              const char *code, const char *detail, const char *sid,
              int argv)
{
	json_final_open(name, "as", as, res, code, detail, sid);
	printf(",\"running\":%s}\n", rv_running(as, argv) ? "true" : "false");
	fflush(stdout);
}

/* --revive's line for one record: `<name>: <detail>`, or under --json
** {"name","as","result","reason","detail","session_id","running"}. */
static DCH_PRINTF(5, 6) void
rv_say(const struct hrec *r, int json, const char *res, const char *code,
       const char *fmt, ...)
{
	va_list ap;
	char d[2048];

	va_start(ap, fmt);
	vsnprintf(d, sizeof d, fmt, ap);
	va_end(ap);
	if (json)
		rv_json_final(r->name, r->as, res, code, d, r->sid,
		              r->mode == RV_ARGV);
	else
		printf("%s: %s\n", r->name, d);
	fflush(stdout);
}

/* What a revive would run that holds U+FFFD, NULL for none: json_quote's
** stand-in for bytes that were not UTF-8, so not what ran. */
static const char *
hrec_lossy(const struct hrec *r)
{
	static const char rep[] = "\xef\xbf\xbd";
	int k;

	if (strstr(r->cwd, rep) || strstr(r->acwd, rep) || strstr(r->acfg, rep))
		return "directory";
	for (k = 0; k < r->argc; k++)
		if (strstr(r->argv[k], rep))
			return "command";
	for (k = 0; r->launch && r->launch[k]; k++)
		if (strstr(r->launch[k], rep))
			return "claude command";
	return NULL;
}

/* The config dir a revived claude resumes under (#065): its record's,
** unless the command up to the claude token, v[0..tok), sets one itself
** (`env CLAUDE_CONFIG_DIR=… claude`). NULL = leave the env alone. */
static const char *
rv_cfg(const char *cfg, char **v, int tok)
{
	int k;

	for (k = 0; *cfg && v && k < tok; k++)
		if (strncmp(v[k], "CLAUDE_CONFIG_DIR=", 18) == 0)
			return NULL;
	return *cfg ? cfg : NULL;
}

/* Spawn one record's session in its cwd, as its argv — with --resume <id>
** when claude is its command (RV_ARGV); a shell that had claude typed into
** it comes back as the shell (RV_SHELL, the command is typed later). 0, or
** -1 with the reason printed. */
static int
revive_spawn(char *exe, struct hrec *r, int cols, int rows, int json)
{
	char **nv = NULL, **v = r->argv;
	int tok = dch_claude_tok(r->argv, r->argc), nc = r->argc, rc;
	const char *lossy = hrec_lossy(r), *cfg = NULL, *o;
	char *was = NULL;

	if (lossy)
	{
		rv_say(r, json, "failed", "not_utf8", "NOT revived — its %s had "
		       "bytes that are not UTF-8, recorded as U+FFFD: it would run "
		       "something else%s%s", lossy, r->sid[0] ? "; resume by "
		       "hand: claude --resume " : "", r->sid);
		return -1;
	}
	if (chdir(r->cwd) < 0)
	{
		rv_say(r, json, "failed", "cwd_gone", "NOT revived — its directory "
		       "%s is gone (%s)", r->cwd, strerror(errno));
		return -1;
	}
	r->mode = RV_PLAIN;
	if (r->sid[0] && tok < r->argc)
	{
		if (!(nv = resume_argv(r->argv, r->argc, tok, r->sid, &nc)))
			return -1;
		v = nv;
		r->mode = RV_ARGV;
		/* claude is the child: its config dir goes in the env the new
		** master hands it, ours put back after (--all spawns more) */
		if ((cfg = rv_cfg(r->acfg, r->argv, tok)))
		{
			was = (o = getenv("CLAUDE_CONFIG_DIR")) ? strdup(o) : NULL;
			setenv("CLAUDE_CONFIG_DIR", cfg, 1);
		}
	}
	else if (r->sid[0] && r->launch)
		r->mode = RV_SHELL;
	/* The new master's record starts with this one's agent and keeps it
	** until a probe finds the resumed claude: a session that ends sooner
	** must not lose the id. Its env only — never the child's. */
	if (r->sid[0])
	{
		struct dch_agent a;
		char *aj = NULL;
		size_t al;
		FILE *f;

		memset(&a, 0, sizeof a);
		snprintf(a.sid, sizeof a.sid, "%s", r->sid);
		snprintf(a.name, sizeof a.name, "%s", r->aname);
		snprintf(a.cwd, sizeof a.cwd, "%s", r->acwd);
		snprintf(a.cfg, sizeof a.cfg, "%s", r->acfg);
		a.launch = r->launch;
		if ((f = open_memstream(&aj, &al)))
		{
			dch_agent_json(f, &a);
			if (fclose(f) == 0)
				setenv("DCH_REVIVE_AGENT", aj, 1);
		}
		free(aj);
	}
	{
		/* do_spawn_verb wants a NULL-terminated argv */
		char **av = calloc((size_t)nc + 1, sizeof *av);

		rc = -1;
		if (av)
		{
			memcpy(av, v, (size_t)nc * sizeof *av);
			rc = do_spawn_verb(exe, r->as, cols, rows, av, nc, 1);
			free(av);
		}
	}
	unsetenv("DCH_REVIVE_AGENT");
	if (cfg && was)
		setenv("CLAUDE_CONFIG_DIR", was, 1);
	else if (cfg)
		unsetenv("CLAUDE_CONFIG_DIR");
	free(was);
	free(nv);
	if (rc != 0)
	{
		rv_say(r, json, "failed", "not_started", "NOT revived — the "
		       "session did not start");
		return -1;
	}
	if (r->alias[0])
		write_alias(r->as, r->alias);
	return 0;
}

/* Type launch (NULL-terminated), with --resume <id>, into the shell of
** session `name`, after `cd <cd> &&` when cd is non-NULL, as `env
** CLAUDE_CONFIG_DIR=<cfg> …` when rv_cfg() says so: env, not an assignment
** word, so every shell takes it and shell_line() quotes it (#065). 0, or
** -1. */
static int
shell_type_launch(const char *name, char **launch, char *id, const char *cd,
                  const char *cfg)
{
	char **nv, *line, *cmd[1], *ev = NULL;
	int n, tok, nc, rc;

	for (n = 0; launch && launch[n]; n++)
		;
	tok = dch_claude_tok(launch, n);
	if (tok == n)
		tok = n - 1;	/* no claude token: --resume goes last */
	if (!(nv = resume_argv(launch, n, tok, id, &nc)))
		return -1;
	if ((cfg = rv_cfg(cfg, launch, tok)))
	{
		/* two slots ahead of the relaunch for `env <ev>` */
		size_t el = strlen(cfg) + 19;
		char **pv = calloc((size_t)nc + 2, sizeof *pv);

		if (!pv || !(ev = malloc(el)))
		{
			free(pv);
			free(nv);
			return -1;
		}
		snprintf(ev, el, "CLAUDE_CONFIG_DIR=%s", cfg);
		pv[0] = "env";
		pv[1] = ev;
		memcpy(pv + 2, nv, (size_t)nc * sizeof *nv);
		free(nv);
		nv = pv;
		nc += 2;
	}
	line = shell_line(cd, nv, nc);
	free(nv);
	free(ev);
	if (!line)
		return -1;
	cmd[0] = line;
	rc = do_send_verb(name, cmd, 1, 1);
	free(line);
	return rc;
}

/* Type the agent's launch command, with --resume <id>, into the revived
** shell once it has printed something (its prompt): no mirror needed. */
static int
revive_type_launch(struct hrec *r)
{
	while (!ty_sig && activity_epoch(r->as) == 0 && wall_ms() < r->deadline)
		poll(NULL, 0, 100);
	if (!ty_sig)
		poll(NULL, 0, 300);
	if (ty_sig)
		return -2;	/* #126: nothing typed */
	return shell_type_launch(r->as, r->launch, r->sid,
	                         r->acwd[0] && strcmp(r->acwd, r->cwd)
	                         ? r->acwd : NULL, r->acfg);
}

#ifdef HAVE_LIBGHOSTTY_VT
/* Wait for the resumed claude's empty composer, then type the revive
** message. Returns NULL when it was sent, else why not (*code: its
** --json reason). */
static const char *
revive_message(struct hrec *r, const char *msg, const char **code)
{
	static char why[160];
	char path[1100];
	struct cmd_info ci;
	struct crec c;
	int g = CG_UNKNOWN, t;

	session_sock_path(r->as, path, sizeof path);
	for (;;)
	{
		int ok;

		if (ty_sig)
		{
			*code = say_code[SAY_INTERRUPTED];
			return say_why[SAY_INTERRUPTED];
		}
		ok = cmd_query(path, &ci) == 0;

		if (ok && ci.pid > 0 && claude_find(ci.pid, 1, &c) > 0 &&
		    strcmp(c.status, "idle") == 0 &&
		    (g = claude_gate(path)) == CG_EMPTY)
		{
			cmd_info_free(&ci);
			break;
		}
		if (ok)
			cmd_info_free(&ci);
		*code = cg_code[CG_UNKNOWN];
		if (g == CG_NOVT)
			return "the session has no terminal mirror";
		if (wall_ms() >= r->deadline)
		{
			snprintf(why, sizeof why, "no empty composer within %lds (%s)",
			         env_ms("DCH_AGENT_STEP_MS", 60000) / 1000, cg_why[g]);
			*code = g == CG_EMPTY ? "not_idle" : cg_code[g];
			return why;
		}
		poll(NULL, 0, AG_POLL_MS);
	}
	t = ag_type(path, msg, 1);
	if (t <= -4)
	{
		t = t == -4 ? SAY_INTERRUPTED : t == -5 ? SAY_WITHDRAWN
		    : SAY_DRAFT_LEFT;
		*code = say_code[t];
		return say_why[t];
	}
	*code = t == 0 ? "" : t == -3 ? "not_submitted" : "not_typed";
	return t == 0 ? NULL : t == -2 ? "it did not land in the composer"
	       : t == -3 ? "it stayed in the composer through every Enter"
	       : "it could not be typed";
}
#endif

/* --revive's guard against two Claudes on one conversation (#064): the
** Claude ids running now, and where. A live session is probed the way
** --say probes it (its child, then the live Claude records below it, each
** read through that process's own config dir, or before a Claude there
** has written one, the --resume id in its argv), not read from its history
** record: the record is up to DCH_RECORD_MS behind (it misses a Claude
** resumed by hand a moment ago) and keeps a killed Claude's id on purpose.
** Then our Claude sessions dir, for a Claude no session probe found: one
** that outlived its master, or runs in a plain terminal. A revive adds the
** id it brings back (run). */
struct sidhold
{
	char sid[64], who[700];
	int run;
};

struct sidset
{
	struct sidhold *v;
	int n, cap;
};

static const struct sidhold *
sid_held(const struct sidset *s, const char *sid)
{
	int k;

	for (k = 0; k < s->n; k++)
		if (strcmp(s->v[k].sid, sid) == 0)
			return &s->v[k];
	return NULL;
}

static DCH_PRINTF(4, 5) void
sid_add(struct sidset *s, const char *sid, int run, const char *fmt, ...)
{
	struct sidhold *nv;
	va_list ap;

	if (!*sid || sid_held(s, sid))
		return;
	if (s->n == s->cap)
	{
		if (!(nv = realloc(s->v, (size_t)(s->cap * 2 + 8) * sizeof *nv)))
			return;
		s->v = nv;
		s->cap = s->cap * 2 + 8;
	}
	snprintf(s->v[s->n].sid, sizeof s->v[s->n].sid, "%s", sid);
	va_start(ap, fmt);
	vsnprintf(s->v[s->n].who, sizeof s->v[s->n].who, fmt, ap);
	va_end(ap);
	s->v[s->n++].run = run;
}

/* dch_visit_claude_records callback: every live interactive Claude, its
** record's procStart matching the process (a recycled pid never joins). */
static int
sid_visit(const char *path, const char *body, size_t len, void *arg)
{
	struct sidset *s = arg;
	char kind[32], sid[64], start[128], actual[128], sn[600];
	long pid;

	(void)path;
	(void)len;
	if (dch_json_long(body, "pid", &pid) < 0 || pid <= 0 ||
	    dch_json_string(body, "kind", kind, sizeof kind) < 0 ||
	    strcmp(kind, "interactive") ||
	    dch_json_string(body, "sessionId", sid, sizeof sid) < 0 ||
	    !*sid || sid_held(s, sid) || kill((pid_t)pid, 0) < 0 ||
	    dch_json_string(body, "procStart", start, sizeof start) < 0 ||
	    dch_process_start(pid, actual, sizeof actual) < 0 ||
	    strcmp(start, actual))
		return 0;
	if (proc_dch_session(pid, sn, sizeof sn) == 0 && *sn && name_live(sn))
		sid_add(s, sid, 0, "live session %s (claude pid %ld)", sn, pid);
	else
		sid_add(s, sid, 0, "claude pid %ld, outside any live session",
		        pid);
	return 0;
}

/* The conversation a Claude at or below `root` was started on, read from
** its live argv (`--resume <id>`, `--resume=<id>`, `-r <id>`, the last one;
** none with --fork-session, which writes a new id): until that Claude
** writes its sessions record, the only sign of its id (#281). The kernel's
** argv, not the master's MSG_CMD one, which outlives the child while held
** and is the shell for a claude typed into it: a Claude that exited is out
** of the tree and holds nothing. 0 and *sid, or -1. */
static int
claude_argv_sid(long root, char *sid, size_t sidsz)
{
	long tree[64];
	int n = proc_tree(root, tree, 64), i, k, argc;
	char **v;

	for (i = 0; i < n; i++)
	{
		const char *id = NULL;

		if (!(v = proc_argv(tree[i])))
			continue;
		for (argc = 0; v[argc]; argc++)
			;
		for (k = dch_claude_tok(v, argc) + 1; k < argc &&
		     strcmp(v[k], "--"); k++)
			if (!strcmp(v[k], "--fork-session"))
				break;
			else if ((!strcmp(v[k], "--resume") || !strcmp(v[k], "-r")) &&
			         k + 1 < argc && xr_uuid_shaped(v[k + 1]))
				id = v[++k];
			else if (!strncmp(v[k], "--resume=", 9) &&
			         xr_uuid_shaped(v[k] + 9))
				id = v[k] + 9;
		if (id && (k == argc || strcmp(v[k], "--fork-session")))
		{
			snprintf(sid, sidsz, "%s", id);
			free(v);
			return 0;
		}
		free(v);
	}
	return -1;
}

static void
sid_live(struct sidset *s, const struct hrec *r, int n)
{
	struct slist sl;
	char sp[1100];
	int k, j;

	memset(&sl, 0, sizeof sl);
	(void)list_sessions(&sl);
	for (k = 0; k < sl.n; k++)
	{
		struct cmd_info ci;
		struct crec c;

		session_sock_path(sl.v[k], sp, sizeof sp);
		if (cmd_query(sp, &ci) != 0)
			continue;
		/* a record wins (a /resume switch moves it off the argv); none
		** yet, or an empty one (#274): the id the Claude started on */
		if (ci.pid > 0 && claude_find(ci.pid, 1, &c) > 0 && c.sid[0])
			sid_add(s, c.sid, 0, "live session %s", sl.v[k]);
		else if (ci.pid > 0 &&
		         claude_argv_sid(ci.pid, c.sid, sizeof c.sid) == 0)
			sid_add(s, c.sid, 0, "live session %s", sl.v[k]);
		cmd_info_free(&ci);
	}
	slist_free(&sl);
	(void)dch_visit_claude_records(sid_visit, s);
	/* and each record's own config dir (#065): a Claude outside any
	** session runs a conversation only from the dir that holds it */
	for (k = 0; k < n; k++)
	{
		for (j = 0; j < k && strcmp(r[j].acfg, r[k].acfg); j++)
			;
		if (r[k].acfg[0] && j == k &&
		    snprintf(sp, sizeof sp, "%s/sessions", r[k].acfg) <
		    (int)sizeof sp)
			(void)visit_records_in(sp, sid_visit, s);
	}
}

/* Which records an argument names, for --revive and --pin/--unpin/--forget
** (#067): `<file>.json` exactly that one record file (the primary
** <name>.json or one a cold start moved aside); else every record of that
** session name, its canonical file, or the file as given (--history's
** [record <file>], which canon_name would hash again when long). Sets
** *stem and *byfile; NULL, or the reason a refusal reports: not_found for
** a stem with a '/' (it never leaves the history dir), ambiguous for a
** .json argument that is also a session's name. */
static const char *
rec_arg(const char *arg, const struct hrec *r, int n, char *cb, size_t cbsz,
        const char **stem, int *byfile)
{
	size_t al = strlen(arg);
	int k, j;

	*byfile = al > 5 && strcmp(arg + al - 5, ".json") == 0;
	if (*byfile)
	{
		snprintf(cb, cbsz, "%.*s", (int)(al - 5), arg);
		*stem = cb;
	}
	else
		*stem = canon_name(arg, cb, cbsz);
	if (strchr(*stem, '/'))
		return "not_found";
	if (*byfile)
	{
		for (k = 0; k < n; k++)
			if (strcmp(r[k].name, arg) == 0)
				return "ambiguous";
		return NULL;
	}
	/* a long argument as given that is one record's file (moved aside
	** from a long name) while it is another's name or hashes to its file:
	** which one is meant is anyone's guess */
	for (k = 0; k < n; k++)
		if (strcmp(r[k].file, arg) == 0 && strcmp(r[k].file, *stem) &&
		    strcmp(r[k].name, arg))
			break;
	for (j = 0; k < n && j < n; j++)
		if (strcmp(r[j].name, arg) == 0 || strcmp(r[j].file, *stem) == 0)
			return "ambiguous";
	return NULL;
}

static int
rec_hit(const struct hrec *h, const char *arg, const char *stem, int byfile)
{
	if (byfile)
		return strcmp(h->file, stem) == 0;
	return strcmp(h->name, arg) == 0 || strcmp(h->file, stem) == 0 ||
	       strcmp(h->file, arg) == 0;
}

#define AMBIGUOUS_FMT "ambiguous: %s matches more than one session's " \
                      "records; pass the one meant as <file>.json, its " \
                      "\"file\" in dch --history --json (session a.json: " \
                      "a.json.json)"

/* `--revive <name> [--as <new>]` / `--revive --all`; nomsg: --no-message,
** claude is resumed and nothing is typed into it. */
static int
do_revive_verb(char *exe, const char *name, const char *as, int all,
               int cols, int rows, int json, int nomsg)
{
	struct hrec *r;
	int n = history_load(&r), k, picked = 0, bad = 0, seenlive = 0;
	long step = env_ms("DCH_AGENT_STEP_MS", 60000);
	char dir[1024], cb[256], ab[256];
	const char *stem = NULL, *why;
	const struct sidhold *sh;
	struct sidset held;
	int byfile = 0;

	memset(&held, 0, sizeof held);
	if (!all && (why = rec_arg(name, r, n, ab, sizeof ab, &stem, &byfile)))
	{
		char d[1200];

		if (*why == 'a')
			snprintf(d, sizeof d, AMBIGUOUS_FMT, name);
		else
			snprintf(d, sizeof d, "no history for session %s", name);
		fprintf(stderr, "dch: %s\n", d);
		if (json)
			rv_json_final(name, as ? as : name, "failed", *why == 'a'
			              ? "ambiguous" : "no_history", d, "", 0);
		hrec_free(r, n);
		return 1;
	}
	ty_arm();
	strip_caller_agent();
	login_env();
	(void)dch_history_dir(dir, sizeof dir, sock_dir, 0);
	if (n)
		sid_live(&held, r, n);
	for (k = 0; k < n; k++)
	{
		struct hrec *h = &r[k];
		int i;

		h->mode = RV_SKIP;
		if (all && !rv_all_takes(h))
		{
			/* the newest record of an id ended on purpose: an older
			** crash record of that id stays down too (newest first) */
			if (!h->live && (!strcmp(h->end, "stopped") ||
			                 !strcmp(h->end, "killed") ||
			                 !strcmp(h->end, "exit 0")))
				sid_add(&held, h->sid, 2, "%s", h->end);
			continue;
		}
		if (!all && !rec_hit(h, name, stem, byfile))
			continue;
		if (!all && (picked || h->live))
		{
			seenlive |= h->live;
			continue;	/* the newest ended record wins */
		}
		picked++;
		snprintf(h->as, sizeof h->as, "%s", as ? as : h->name);
		if (!all && name_live(h->as))
		{
			fprintf(stderr, "dch: session %s is live; revive it under "
			        "another name with --revive %s --as <new>\n", h->as,
			        name);
			if (json)
				rv_say(h, 1, "failed", "live", "session %s is live",
				       h->as);
			bad++;
			continue;
		}
		/* never a second Claude on a conversation: not one a live
		** session runs, not one this run already brought back from a
		** newer record of it (the newest end comes first) */
		sh = h->sid[0] ? sid_held(&held, h->sid) : NULL;
		if (sh && all)
		{
			rv_say(h, json, "skipped", sh->run ? "dup_sid" : "live_sid",
			       "skipped — claude %s %s%s (record %s)", h->sid,
			       sh->run == 2 ? "ended in a newer record: "
			       : sh->run ? "is revived from a newer record as "
			       : "already runs in ", sh->who, h->file);
			continue;
		}
		if (sh)
		{
			fprintf(stderr, "dch: claude %s already runs in %s; not "
			        "reviving %s: two claudes would write one "
			        "conversation\n", h->sid, sh->who, name);
			if (json)
				rv_say(h, 1, "failed", "live_sid", "claude %s already "
				       "runs in %s", h->sid, sh->who);
			bad++;
			continue;
		}
		/* --all: the next free <name>-N, one that no record claims
		** either — its master would overwrite that record. */
		if (all && name_live(h->as))
			for (i = 2; i <= 999; i++)
			{
				char rp[1400];

				/* a cut <name>-N is not that name: no free one */
				if ((size_t)snprintf(h->as, sizeof h->as, "%s-%d",
				                     h->name, i) >= sizeof h->as ||
				    (size_t)snprintf(rp, sizeof rp, "%s/%s.json", dir,
				                     canon_name(h->as, cb, sizeof cb)) >=
				        sizeof rp)
					continue;
				if (!name_live(h->as) && access(rp, F_OK) != 0)
					break;
			}
		if (ty_sig)	/* #126: a signal stops before the next start */
		{
			rv_say(h, json, "failed", say_code[SAY_INTERRUPTED], "NOT "
			       "revived — %s%s%s", say_why[SAY_INTERRUPTED],
			       h->sid[0] ? "; resume by hand: claude --resume " : "",
			       h->sid);
			bad++;
			continue;
		}
		json_phase(h->name, "starting", json);
		if (revive_spawn(exe, h, cols, rows, json) < 0)
		{
			h->mode = RV_SKIP;
			bad++;
			continue;
		}
		sid_add(&held, h->sid, 1, "%s", h->as);
		h->deadline = wall_ms() + step;
		/* revived into another file (a new name, or a record moved
		** aside): the old record is now history the new one carries;
		** left alone, --all would revive it again */
		if (strcmp(h->file, canon_name(h->as, cb, sizeof cb)) != 0 &&
		    !h->live)
		{
			char rp[1400], np[1400];

			/* not when the new master keeps no record: it has our env
			** (login_env never takes DCH_*), so master.c's own test */
			snprintf(rp, sizeof rp, "%s/%s.json", dir, h->file);
			if (!dch_env_on("DCH_NO_HISTORY") &&
			    unlink(rp) == 0)
			{
				/* and its pin moves with it */
				snprintf(rp, sizeof rp, "%s/%s.pin", dir, h->file);
				snprintf(np, sizeof np, "%s/%s.pin", dir,
				         canon_name(h->as, cb, sizeof cb));
				if (h->pinned)
					(void)rename(rp, np);
			}
		}
		if (h->mode == RV_PLAIN)
		{
			rv_say(h, json, h->sid[0] ? "failed" : "ok",
			       h->sid[0] ? "launch_unknown" : "", "revived as %s in "
			       "%s%s%s%s%s", h->as, h->cwd, h->sid[0] ? "; claude "
			       : "", h->sid[0] ? h->aname : "", h->sid[0]
			       ? " NOT resumed (its launch "
			       "command is unknown): claude --resume " : "", h->sid);
			bad += h->sid[0] != 0;
		}
	}
	if (!picked)
	{
		if (all && !json)
			puts("nothing to revive (sessions that ended over 3 days "
			     "ago or ran under 5 minutes are in dch --history; "
			     "revive one by name)");
		else if (!all && seenlive)
			fprintf(stderr, "dch: session %s is live and no ended record "
			        "of it is left to revive\n", name);
		else if (!all)
			fprintf(stderr, "dch: no history for session %s\n", name);
		if (!all && json)
			rv_json_final(name, as ? as : name, "failed",
			              seenlive ? "live" : "no_history", seenlive
			              ? "session is live"
			              : "no history for this session", "", 0);
		hrec_free(r, n);
		free(held.v);
		return all ? 0 : 1;
	}
	/* Then the agents: every session is already starting, so waiting on
	** them one by one costs about the slowest one. Shells get their claude
	** command first; its clock starts when it is typed. */
	for (k = 0; k < n; k++)
		if (r[k].mode == RV_SHELL)
		{
			int tl = revive_type_launch(&r[k]);

			if (tl == 0)
			{
				r[k].deadline = wall_ms() + step;
				continue;
			}
			rv_say(&r[k], json, "failed", tl == -2 ? say_code[SAY_INTERRUPTED]
			       : "relaunch_failed", "revived as %s in %s, but claude %s "
			       "%s; resume by hand: claude --resume %s", r[k].as,
			       r[k].cwd, r[k].aname, tl == -2 ? "was not started: a "
			       "signal came first" : "could not be started", r[k].sid);
			r[k].mode = RV_PLAIN;
			bad++;
		}
	for (k = 0; k < n; k++)
	{
		struct hrec *h = &r[k];
		const char *msg = getenv("DCH_REVIVE_MSG");
		const char *code, *res;
		char text[1200], when[32] = "an unknown time";
		time_t t = (time_t)hrec_when(h);

		why = NULL;
		if (h->mode != RV_ARGV && h->mode != RV_SHELL)
			continue;
		if (nomsg)
		{
			rv_say(h, json, "ok", "", "revived as %s in %s, claude %s%s"
			       "resumed %s; resume message not sent (--no-message)",
			       h->as, h->cwd, h->aname, h->aname[0] ? " " : "",
			       h->sid);
			continue;
		}
		if (t)
			strftime(when, sizeof when, "%Y-%m-%d %H:%M", localtime(&t));
		if (strcmp(h->end, "stopped") == 0)
			snprintf(text, sizeof text, "resume all work. This session "
			         "was stopped by dch --stop (%s) and dch revived it "
			         "with claude --resume. Re-arm the monitors, "
			         "background shells, subagents, loops, wakeups and "
			         "crons you stopped before the pause, then carry on "
			         "where you left off.", when);
		else
			snprintf(text, sizeof text, "resume all work. This session "
			         "ended (%s, last seen %s) and dch revived it with "
			         "claude --resume. Re-read your ledger and task list, "
			         "re-arm the monitors, background shells, loops, "
			         "wakeups and crons that died with it, re-dispatch "
			         "any unfinished workers, then carry on where you "
			         "left off.", h->end, when);
#ifdef HAVE_LIBGHOSTTY_VT
		json_phase(h->name, "resuming", json);
		code = "";
		why = revive_message(h, msg && *msg ? msg : text, &code);
		bad += why != NULL;
		res = why ? "failed" : "ok";
#else
		(void)msg;
		why = "dch-lite has no terminal mirror to see the composer";
		code = "no_screen";	/* revived and resumed: still ok */
		res = "ok";
#endif
		rv_say(h, json, res, code,
		       "revived as %s in %s, claude %s%sresumed %s; resume "
		       "message %s%s%s", h->as, h->cwd, h->aname,
		       h->aname[0] ? " " : "", h->sid, why ? "NOT sent (" : "sent",
		       why ? why : "", why ? ")" : "");
	}
	hrec_free(r, n);
	free(held.v);
	return bad ? 1 : 0;
}

/* ---- --pin, --unpin, --forget (#052) ------------------------------- */

enum { HV_PIN, HV_UNPIN, HV_FORGET };

/* One result line; c counts ok, skipped, failed for the --json summary.
** Text: `<name>: <detail>` on ok, `<name>: failed — <detail>`; a skip
** (--forget --all) says nothing, its count goes in the closing line. */
static void
hv_say(const char *name, const char *res, const char *code,
       const char *detail, const char *sid, int json, int *c)
{
	c[*res == 'o' ? 0 : *res == 's' ? 1 : 2]++;
	if (json)
		json_final(name, NULL, NULL, res, code, detail, sid);
	else if (*res == 'o')
		printf("%s: %s\n", name, detail);
	else if (*res == 'f')
		printf("%s: failed — %s\n", name, detail);
}

/* Remove one record and its pin. 0, or -1 with errno. */
static int
hv_forget(const char *dir, const struct hrec *r)
{
	char p[1400];

	snprintf(p, sizeof p, "%s/%s.pin", dir, r->file);
	if (unlink(p) < 0 && errno != ENOENT)
		return -1;
	snprintf(p, sizeof p, "%s/%s.json", dir, r->file);
	return unlink(p) < 0 && errno != ENOENT ? -1 : 0;
}

/* `--pin|--unpin <name>...`, `--forget <name>... [-f]`, `--forget --all`
** (names NULL). A pin is an empty <file>.pin beside the record, keyed by
** the name, never touched by the master: it outlives every rewrite, a
** record that has one is never pruned, and --forget refuses it without
** -f. A live session's record is never forgotten: its master would write
** it back. Matching is --revive's (rec_arg): an ambiguous argument
** touches nothing, one that matches nothing is never ok. */
static int
do_pin_verb(char **names, int nn, int mode, int force, int json)
{
	struct hrec *r;
	int n = history_load(&r), j, k, f, fd, perr, hit, c[3] = {0, 0, 0};
	int kp = 0, kl = 0;
	char dir[1024], cb[256], p[1400];
	const char *cn;

	if (dch_history_dir(dir, sizeof dir, sock_dir, 0) < 0)
		dir[0] = '\0';	/* no records: history_load found none */
	for (k = 0; !names && k < n; k++)
		if (name_live(r[k].file))
		{
			kl++;
			hv_say(r[k].name, "skipped", "live", "session is live",
			       r[k].sid, json, c);
		}
		else if (r[k].pinned)
		{
			kp++;
			hv_say(r[k].name, "skipped", "pinned", "pinned", r[k].sid,
			       json, c);
		}
		else if (hv_forget(dir, &r[k]) < 0)
			hv_say(r[k].name, "failed", "io_error", strerror(errno),
			       r[k].sid, json, c);
		else
			hv_say(r[k].name, "ok", "", "forgotten", r[k].sid, json, c);
	for (j = 0; names && j < nn; j++)
	{
		int byfile;
		const char *why = rec_arg(names[j], r, n, cb, sizeof cb, &cn,
		                          &byfile);

		/* ambiguous: nothing touched, not even a stray pin */
		if (why)
		{
			snprintf(p, sizeof p, AMBIGUOUS_FMT, names[j]);
			hv_say(names[j], "failed", why, *why == 'a' ? p : "no "
			       "history for this session", "", json, c);
			continue;
		}
		for (hit = perr = 0, f = -1, k = 0; k < n; k++)
		{
			if (!rec_hit(&r[k], names[j], cn, byfile))
				continue;
			hit++;
			if (f < 0)
				f = k;	/* the newest record: its sid */
			/* every record of the name, the ones a cold start moved
			** aside too: the pin goes beside each */
			snprintf(p, sizeof p, "%s/%s.pin", dir, r[k].file);
			if (mode == HV_PIN && (fd = open(p, O_WRONLY | O_CREAT |
			                                 O_NOFOLLOW, 0600)) >= 0)
				close(fd);
			else if (mode == HV_PIN)
				perr = errno;
			else if (mode == HV_UNPIN && unlink(p) < 0 && errno != ENOENT)
				perr = errno;
			if (mode != HV_FORGET)
				continue;
			if (name_live(r[k].file))
				hv_say(r[k].name, "failed", "live", "session is live",
				       r[k].sid, json, c);
			else if (r[k].pinned && !force)
				hv_say(r[k].name, "failed", "pinned", "pinned (-f "
				       "forgets it anyway)", r[k].sid, json, c);
			else if (hv_forget(dir, &r[k]) < 0)
				hv_say(r[k].name, "failed", "io_error",
				       strerror(errno), r[k].sid, json, c);
			else
				hv_say(r[k].name, "ok", "", "forgotten", r[k].sid,
				       json, c);
		}
		/* a pin left without its record: unpinning it is a hit */
		snprintf(p, sizeof p, "%s/%s.pin", dir, cn);
		if (mode == HV_UNPIN && dir[0] && unlink(p) == 0)
			hit += !hit;
		else if (mode == HV_UNPIN && dir[0] && errno != ENOENT)
			perr = errno;
		if ((mode == HV_UNPIN && (hit || perr)) || (mode == HV_PIN && hit))
		{
			if (perr)
				hv_say(names[j], "failed", "io_error", strerror(perr),
				       f >= 0 ? r[f].sid : "", json, c);
			else
				hv_say(names[j], "ok", "", mode == HV_PIN ? "pinned"
				       : "unpinned", f >= 0 ? r[f].sid : "", json, c);
		}
		else if (!hit)
			hv_say(names[j], "failed", "not_found", "no history for "
			       "this session", "", json, c);
	}
	if (json)
		printf("{\"ok\":%d,\"skipped\":%d,\"failed\":%d}\n", c[0], c[1],
		       c[2]);
	else if (!names)
		printf("forgot %d (%d pinned, %d live kept)\n", c[0], kp, kl);
	hrec_free(r, n);
	return c[2] ? 1 : 0;
}

/* The master pass's word on one session: rc is do_restart_verb's, or -1
** for one --all leaves alone (already current). Text says only a restart,
** as it always has (the failures are on stderr); --json says every one,
** as {"name","phase":"master","result","reason","detail","session_id":""}.
** all: --all, where a missing sidecar is skipped, not failed. */
static void
master_print(const char *name, int rc, int all, int json)
{
	char d[128];

	if (!json)
	{
		if (rc == 0)
			printf("%s: restarted on dch %s\n", name, DCH_VERSION);
		return;
	}
	snprintf(d, sizeof d, "%s dch %s", rc == 0 ? "restarted on"
	         : "already on", DCH_VERSION);
	json_final(name, "phase", "master", rc == 0 ? "ok"
	           : rc < 0 || (rc == 3 && all) ? "skipped" : "failed",
	           rc == 0 ? "" : rc < 0 ? "current" : rc == 2 ? "no_session"
	           : rc == 3 ? "no_sidecar" : "restart_failed",
	           rc <= 0 ? d : rc == 2 ? "no such session" : rc == 3
	           ? "version sidecar missing, age unknown (see stderr)"
	           : "not restarted (see stderr)", "");
}

/* ---- dch --xray (#120, XR1b) ----------------------------------------- */

/* The one ring client (XR1a's MSG_RING_READ, dtach.h): every record from the
** head to the tail as it stood at the first reply, in DCH_RING_READ_MAX
** replies, handed to a callback one at a time. Only one reply is ever held
** (3.8's 16 MB, whatever xray_ring is). */
#define RING_BUSY_TRIES 50
/* Replies per walk: a real master fills each but the last to within one
** record of DCH_RING_READ_MAX, so RING_MAX (plus the last reply's overshoot)
** takes about 19; a peer that trickles a record at a time is cut off. */
#define RING_REPLIES_MAX 64

struct ring_snap
{
	unsigned status;	/* DCH_RING_S_* of every reply, or'ed */
	uint32_t epoch;
	char sid[18];
	int seqgap;		/* a later reply did not start where we asked */
	uint64_t bytes;		/* record bytes handed over, headers included */
	uint64_t head;		/* the first reply's head_seq (#136) */
	uint64_t tail;		/* and its tail_seq (#140) */
	uint64_t end;		/* past the last record read (#173) */
	int woke;		/* a MSG_RING_WAKE came in mid-read (#136) */
};

/* x and its text live until the call returns. */
typedef void (*ring_rec_fn)(void *ctx, const struct xr_rec *x);

static uint64_t
le64(const unsigned char *p)
{
	uint64_t v = 0;
	int k;

	for (k = 7; k >= 0; k--)
		v = v << 8 | p[k];
	return v;
}

/* Over an open control connection s. fn NULL: the header only
** (DCH_RING_F_HDR; a 1.43.0 master's records are dropped unread). 0, rs
** filled; -1 the master hung up (EOF or a reset at any point: the session
** is gone, as when nothing listens); -2 the master does not answer the op
** (it predates #119: silence, or DCH_ST_UNSUP); -3 a
** malformed or failed reply, one over DCH_RING_READ_MAX record bytes, or a
** later one that does not move past seq (a hostile master never holds the
** read); -4 the ring was cleared mid-read (the caller starts over).
** ring_fetch_rd: on the caller's reader, which keeps frames that came in
** behind the reply (a follower's next wake), with DCH_RING_F_* flags.
** at: NULL from the head; else on from at's end, every reply held to at's
** epoch, one that does not start there a seqgap (#173). */
static int
ring_fetch_rd(int s, struct ctl_reader *rd, int flags,
              const struct ring_snap *at, struct ring_snap *rs,
              ring_rec_fn fn, void *ctx)
{
	unsigned char req[13], payload[PKT_MAX], type, hdr[DCH_RING_HDR_LEN];
	unsigned char *buf = NULL;
	size_t bl, bcap = 0;
	unsigned int len;
	uint64_t seq = 0, first, tail0 = 0;
	uint32_t count, max = DCH_RING_READ_MAX;
	long long wl;
	int rc, k, tries = 0, replies = 0, have_hdr, ret = 0;

	memset(rs, 0, sizeof *rs);
	if (at)
	{
		seq = rs->end = at->end;
		rs->epoch = at->epoch;
	}
	for (;;)
	{
		for (k = 0; k < 8; k++)
			req[k] = (unsigned char)(seq >> (8 * k));
		for (k = 0; k < 4; k++)
			req[8 + k] = (unsigned char)(max >> (8 * k));
		req[12] = (unsigned char)((fn ? 0 : DCH_RING_F_HDR) | flags);
		send_ctl(s, MSG_RING_READ, req, sizeof req);
		have_hdr = 0;
		bl = 0;
		for (;;)
		{
			rc = ctl_next_frame(s, rd, CTL_IDLE_MS, &type, payload, &len);
			if (rc <= 0)
			{
				ret = rd->eof ? -1 : replies || rd->got_any ? -3 : -2;
				goto out;
			}
			if (type == MSG_RING_HDR && len == DCH_RING_HDR_LEN)
			{
				memcpy(hdr, payload, sizeof hdr);
				have_hdr = 1;
			}
			else if (type == MSG_READ_DATA && have_hdr && len)
			{
				if (bl + len > DCH_RING_READ_MAX)
				{
					ret = -3;	/* past S10's reply bound */
					goto out;
				}
				if (!fn)
				{
					bl += len;	/* header only: never kept */
					continue;
				}
				if (bl + len > bcap)
				{
					size_t nc = bcap ? bcap : 65536;
					unsigned char *nb;

					while (nc < bl + len)
						nc *= 2;
					if (!(nb = realloc(buf, nc)))
					{
						ret = -3;
						goto out;
					}
					buf = nb;
					bcap = nc;
				}
				memcpy(buf + bl, payload, len);
				bl += len;
			}
			else if (type == MSG_READ_END || type == MSG_ACK)
				break;
			else if (type == MSG_RING_WAKE)
				rs->woke = 1;	/* the ring moved past this read */
			else
			{
				ret = -3;
				goto out;
			}
		}
		if (type == MSG_ACK || len == DCH_ST_UNSUP)
		{
			ret = -2;
			goto out;
		}
		if (len == DCH_ST_BUSY && ++tries < RING_BUSY_TRIES)
		{
			poll(NULL, 0, 20);
			continue;
		}
		if (len != DCH_ST_OK || !have_hdr)
		{
			ret = -3;
			goto out;
		}
		if (++replies > RING_REPLIES_MAX)
		{
			ret = -3;
			goto out;
		}
		first = le64(hdr + 17);
		count = (uint32_t)hdr[25] | (uint32_t)hdr[26] << 8 |
		        (uint32_t)hdr[27] << 16 | (uint32_t)hdr[28] << 24;
		if (replies == 1)
		{
			if (!at)
				rs->epoch = le64(hdr + 29) & 0xffffffffu;
			memcpy(rs->sid, hdr + 33, 17);
			rs->sid[17] = '\0';
			rs->tail = tail0 = le64(hdr + 9);
			rs->head = le64(hdr + 1);
		}
		if (replies > 1 || at)
		{
			if ((le64(hdr + 29) & 0xffffffffu) != rs->epoch)
			{
				ret = -4;	/* cleared mid-read: none of it stands */
				goto out;
			}
			/* the first read asks from 0 and lands on the head; a
			** later one that lands elsewhere lost records */
			if ((hdr[0] & DCH_RING_S_RANGE) || first != seq)
				rs->seqgap = 1;
			if (count && first + count <= seq)
			{
				ret = -3;	/* no progress: never asked again */
				goto out;
			}
		}
		rs->status |= hdr[0] & (unsigned)~DCH_RING_S_RANGE;
		if (!fn || (hdr[0] & DCH_RING_S_OFF))
			break;
		/* this reply's records, bounds-checked, one at a time */
		if ((wl = xr_ring_walk(buf, bl, first, count, fn, ctx)) < 0)
		{
			ret = -3;	/* a torn record: never skipped */
			goto out;
		}
		rs->bytes += (uint64_t)wl;
		rs->end = first + count;
		if (rs->bytes > (uint64_t)RING_MAX + DCH_RING_READ_MAX)
		{
			ret = -3;	/* more than any ring holds, plus a reply */
			goto out;
		}
		/* up to the first reply's tail: a flooding shell is never
		** chased, so the read ends within about one ring's bytes */
		if (!count || first + count >= tail0)
			break;
		seq = first + count;
	}
out:
	free(buf);
	return ret;
}

static int
ring_fetch_fd(int s, struct ring_snap *rs, ring_rec_fn fn, void *ctx)
{
	struct ctl_reader r = {0};

	return ring_fetch_rd(s, &r, 0, NULL, rs, fn, ctx);
}

/* ring_fetch_fd on a connection of its own; -1 nothing listens, or the
** master hung up. */
static int
ring_fetch(const char *path, struct ring_snap *rs, ring_rec_fn fn,
           void *ctx)
{
	int s, rc;

	memset(rs, 0, sizeof *rs);
	if ((s = control_connect_path(path)) < 0)
		return -1;
	rc = ring_fetch_fd(s, rs, fn, ctx);
	close(s);
	return rc;
}

/* After a ring read, the current primary screen (7.3, #140): its rows, soft
** wraps joined, to fn as output records with seqs from the ring's tail on,
** past the seam xr_shell_screen draws in sh (#171). The alt screen
** (DCH_ST_ALT) and a master before #140 (DCH_ST_ERR) give nothing: the
** answer is the ring's alone, never an error. 1 when screen rows were lost:
** no answer within ms, a hang-up, busy, or a reply past the master's cap,
** whose kept tail starts inside a row (maybe inside a character): that row
** is dropped. Rows that scroll into the ring between its read and the
** screen's are on neither (QA 1.54.0 LOW-2): after each screen read the
** ring is read on from where it ended, those rows to fn, and the screen
** again, until the ring stood still across one; XR_SEAM_TRIES times, then
** a header check alone, and rows lost when it moved still (#173). */
#define XR_SCREEN_CAP (2u << 20)	/* the master's MSG_READ cap */
#define XR_SEAM_TRIES 3
static int
ring_screen(const char *path, const struct ring_snap *rs, struct xr_shell *sh,
            ring_rec_fn fn, void *ctx, int ms)
{
	struct ring_snap at = *rs, cu;
	struct ctl_reader r;
	struct xr_rec x;
	char *buf, *l, *nl;
	int n, st, s, fr, tries, seam = 0;

	if (rs->status & (DCH_RING_S_OFF | DCH_RING_S_PAUSED))
		return 0;
	if (ms <= 0 || !(buf = malloc(XR_SCREEN_CAP + 1)))
		return 1;
	for (tries = 0;; tries++)
	{
		n = fetch_screen(path, buf, XR_SCREEN_CAP, ms, 0,
		                 DCH_READ_F_PRIMARY, &st);
		if (st != DCH_ST_OK && st != DCH_ST_TRUNC)
			break;	/* no rows (alt, a master before #140) or lost */
		fr = -1;
		if ((s = control_connect_path(path)) >= 0)
		{
			memset(&r, 0, sizeof r);
			fr = ring_fetch_rd(s, &r, 0, &at, &cu,
			                   tries < XR_SEAM_TRIES ? fn : NULL, ctx);
			close(s);
		}
		if (fr == 0 && !cu.seqgap && cu.tail == at.end)
			break;
		if (fr != 0 || cu.seqgap || tries == XR_SEAM_TRIES)
		{
			seam = 1;
			break;
		}
		at.end = cu.end;
	}
	xr_shell_screen(sh);
	memset(&x, 0, sizeof x);
	x.seq = at.end;
	x.kind = RING_K_OUTPUT;
	x.t = (uint32_t)time(NULL);
	l = buf;
	if (st == DCH_ST_TRUNC && n > 0)
		l = (nl = memchr(buf, '\n', (size_t)n)) ? nl + 1 : buf + n;
	for (; n > 0 && l < buf + n; l = nl + 1)
	{
		if (!(nl = memchr(l, '\n', (size_t)(buf + n - l))))
			nl = buf + n;
		if (nl == l)
			continue;	/* blank rows never enter the ring either */
		x.text = l;
		x.len = (size_t)(nl - l);
		fn(ctx, &x);
		x.seq++;
	}
	free(buf);
	return seam || st < 0 || st == DCH_ST_BUSY || st == DCH_ST_TRUNC;
}

/* The live Claude under a session's child, as --ls-json joins it: exactly
** one record, uuid-shaped. 1 with *c, else 0. ci: the MSG_CMD answer. */
static int
xr_live_claude(const struct cmd_info *ci, struct crec *c)
{
	return ci->pid > 0 && strcmp(ci->state, "held") != 0 &&
	       claude_find(ci->pid, 1, c) == 1 && xr_uuid_shaped(c->sid);
}

/* <cfg> of a live Claude: its record's <cfg>/sessions/<pid>.json. */
static void
xr_crec_cfg(const struct crec *c, char *cfg, size_t cfgsz)
{
	char *s;
	int k;

	snprintf(cfg, cfgsz, "%s", c->rec);
	for (k = 0; k < 2 && (s = strrchr(cfg, '/')); k++)
		*s = '\0';
	if (k < 2)
		cfg[0] = '\0';
}

/* The live Codex under a session's child (7.4, K9): the shallowest codex,
** its CODEX_HOME (validated before any read: mutant 36) and the one thread
** id dch_codex_snapshot_id's strict rule gives (ambiguous or absent: no
** corpus, never the newest rollout: mutant 35). 1 mapped, -1 a Codex
** with no id, 0 none. Nothing in the Codex dir is ever run. */
struct xr_cx
{
	long pid;
	char home[1400], tid[64], cwd[PATH_MAX];
};

static int
xr_live_codex(const struct cmd_info *ci, struct xr_cx *x)
{
	char sess[600], mk[700], h[1200];
	const char *e;

	memset(x, 0, sizeof *x);
	if (ci->pid <= 0 || strcmp(ci->state, "held") == 0 ||
	    (x->pid = dch_codex_below(ci->pid)) <= 0)
		return 0;
	(void)proc_cwd(x->pid, x->cwd, sizeof x->cwd);
	/* its own CODEX_HOME, else its HOME's .codex, as codex itself reads */
	if (proc_env_value(x->pid, "CODEX_HOME", x->home, sizeof x->home) != 0 ||
	    !x->home[0])
	{
		e = proc_env_value(x->pid, "HOME", h, sizeof h) == 0 && h[0] ? h
		    : getenv("HOME");
		if (!e || snprintf(x->home, sizeof x->home, "%s/.codex", e)
		    >= (int)sizeof x->home)
			x->home[0] = '\0';
	}
	if (!xr_config_ok(x->home) ||
	    proc_dch_session(x->pid, sess, sizeof sess) != 0 ||
	    dch_codex_snapshot_id(x->home, sess,
	                          proc_env_value(x->pid, "DCH_NATIVE_BRIDGE_ID",
	                                         mk, sizeof mk) == 0 && mk[0]
	                          ? mk : NULL, x->tid, sizeof x->tid, 1) != 0 ||
	    !xr_uuid_shaped(x->tid))
	{
		x->tid[0] = '\0';
		return -1;
	}
	return 1;
}

enum { XR_NONE, XR_LIVE, XR_ENDED };

struct xr_target
{
	int kind;
	char name[600];		/* the session, host-local */
	char sock[1100];
	char sid[18];		/* resolved by ring session_id: the one to clear */
	const struct hrec *rec;	/* XR_ENDED */
};

static int
xr_no_session(void)
{
	fputs("dch: no such session\n", stderr);
	return 1;
}

/* The live session whose Claude has this id; 1 with t filled. */
static int
xr_by_agent(const char *id, struct xr_target *t)
{
	struct slist sl = {0};
	int k, hit = 0;

	list_sessions(&sl);
	for (k = 0; k < sl.n && !hit; k++)
	{
		struct cmd_info ci;
		struct crec c;

		session_sock_path(sl.v[k], t->sock, sizeof t->sock);
		if (cmd_query(t->sock, &ci) != 0)
			continue;
		if (xr_live_claude(&ci, &c) && strcmp(c.sid, id) == 0)
		{
			snprintf(t->name, sizeof t->name, "%s", sl.v[k]);
			t->kind = XR_LIVE;
			hit = 1;
		}
		cmd_info_free(&ci);
	}
	slist_free(&sl);
	return hit;
}

/* The live session whose ring session_id this is (--clear only): each
** session's header, never its records (7.3: a ring is read only for its
** own <n>). */
static int
xr_by_session_id(const char *id, struct xr_target *t)
{
	struct slist sl = {0};
	int k, hit = 0;

	list_sessions(&sl);
	for (k = 0; k < sl.n && !hit; k++)
	{
		struct ring_snap rs;

		session_sock_path(sl.v[k], t->sock, sizeof t->sock);
		if (ring_fetch(t->sock, &rs, NULL, NULL) == 0 &&
		    strcmp(rs.sid, id) == 0)
		{
			snprintf(t->name, sizeof t->name, "%s", sl.v[k]);
			snprintf(t->sid, sizeof t->sid, "%s", rs.sid);
			t->kind = XR_LIVE;
			hit = 1;
		}
	}
	slist_free(&sl);
	return hit;
}

/* 3.1's resolution. 0 with t; else the exit code, its line printed.
** clear: --clear's, which takes only a live session. */
static int
xr_resolve(const char *n, const struct hrec *r, int nr, int clear,
           struct xr_target *t)
{
	char cb[256];
	const char *stem, *why;
	int byfile, k;

	memset(t, 0, sizeof *t);
	/* INFO-2: an exact live session_id first, then names, so a legacy
	** session named like an id never shadows a live id */
	if (clear && xr_id_shaped(n) && xr_by_session_id(n, t))
		return 0;
	if (xr_uuid_form(n))
	{
		if (xr_by_agent(n, t))
			return 0;
		for (k = 0; !clear && k < nr; k++)
			if (!r[k].live && strcmp(r[k].sid, n) == 0)
			{
				t->kind = XR_ENDED;
				t->rec = &r[k];
				snprintf(t->name, sizeof t->name, "%s", r[k].name);
				return 0;
			}
		return xr_no_session();
	}
	if (xr_record_form(n))
	{
		/* a .json value that is also a session's name: refused */
		if (name_live(n) || ((why = rec_arg(n, r, nr, cb, sizeof cb,
		                                    &stem, &byfile)) &&
		                     *why == 'a'))
		{
			fputs("dch: ambiguous session\n", stderr);
			return 1;
		}
		if (why)
			return xr_no_session();
		/* a record form is History only, so an Ended pane never reads
		** a new ring: a plain shell's <name>.json, which the reused
		** name's session rewrites, answers corpus "none", "ended"
		** (3.1, 7.5, T11); --clear takes no record */
		for (k = 0; k < nr; k++)
			if (rec_hit(&r[k], n, stem, 1))
			{
				if (clear || (r[k].live && r[k].sid[0]))
					break;
				t->kind = XR_ENDED;
				t->rec = &r[k];
				snprintf(t->name, sizeof t->name, "%s", r[k].name);
				return 0;
			}
		return xr_no_session();
	}
	/* an id-shaped name only as listed: a case variant's socket answers
	** for it on macOS (QA 1.45.1 LOW-1) */
	if (name_live(n) && (!xr_label_id_shaped(n, strlen(n)) ||
	                     name_listed(n)))
	{
		t->kind = XR_LIVE;
		snprintf(t->name, sizeof t->name, "%s", n);
		session_sock_path(n, t->sock, sizeof t->sock);
		return 0;
	}
	if (clear)
		return xr_no_session();
	stem = canon_name(n, cb, sizeof cb);
	for (k = 0; k < nr; k++)	/* newest first: the newest ended one */
		if (!r[k].live && rec_hit(&r[k], n, stem, 0))
		{
			t->kind = XR_ENDED;
			t->rec = &r[k];
			snprintf(t->name, sizeof t->name, "%s", r[k].name);
			return 0;
		}
	return xr_no_session();
}

/* Claude chapters for (cfg, sid) into a; 0, -1 no transcript, -2 a read
** failure (exit 3). */
static int
xr_claude_answer(const char *cfg, const char *sid, struct xr_answer *a,
                 struct xr_chapters *ch, char *project, size_t projectsz,
                 char *label, size_t labelsz, struct xr_index *refs)
{
	char path[2048], home[1400];
	struct xr_claude *d;
	struct stat st;
	int fd, capped = 0, rc;

	if (!cfg[0])
	{
		const char *h = getenv("HOME");

		if (!h || snprintf(home, sizeof home, "%s/.claude", h)
		    >= (int)sizeof home)
			return -1;
		cfg = home;
	}
	if (xr_claude_lookup(cfg, sid, path, sizeof path, &st, project,
	                     projectsz) < 0)
		return -1;
	if ((fd = xr_seam_open(path, &st)) < 0)	/* there but unreadable: 3 */
		return errno == EACCES || errno == EPERM || errno == EIO ? -2 : -1;
	if (!(d = xr_claude_new()))
	{
		close(fd);
		return -2;
	}
	if (refs)
	{
		struct xr_sink k;

		xr_index_sink(refs, &k);
		xr_claude_refs(d, &k, (uint64_t)st.st_ino, NULL, NULL);
	}
	rc = xr_claude_read(d, fd, (uint64_t)st.st_size, &capped);
	close(fd);
	xr_claude_end(d, (uint64_t)st.st_ino, ch);
	if (xr_claude_cwd(d)[0])
		snprintf(label, labelsz, "%s", xr_claude_cwd(d));
	xr_claude_free(d);
	if (rc < 0)
	{
		xr_chapters_free(ch);
		return -2;
	}
	a->corpus = "claude";
	a->available = 1;
	a->complete = !capped && ch->total == ch->n;
	a->why = a->complete ? NULL : "range";
	a->chapters_evicted = 0;	/* 3.2: shell prompt records only */
	a->ch = ch;
	return 0;
}

/* --ref's request (3.4): the text, light or full, the light budget. */
static long long xr_t0;		/* dch's start, CLOCK_MONOTONIC ms */

struct xr_refq
{
	const char *text;
	int full, budget, scope;	/* scope: xr_scan_scope's */
	int search;			/* text is --search's (#297) */
};

static int
xr_no_corpus(void)
{
	fputs("dch: no x-ray corpus\n", stderr);
	return 1;
}

static int
xr_failed(void)
{
	fputs("dch: X-ray failed on this session\n", stderr);
	return 3;
}

/* a relative path that outgrows the key only against the session's cwd
** fails here, not in main's cwd-less check: still 3.1's "not a ref" */
static int
xr_not_ref(void)
{
	fputs("dch: not a ref\n", stderr);
	return 2;
}

/* A one-shot answer's exit: written (ok), or the reader left (EPIPE, a
** cancel: 3.4), else a failure. */
static int
xr_out_exit(int ok)
{
	if (ok)
		return 0;
	return ferror(stdout) && errno == EPIPE ? 0 : xr_failed();
}

/* --ref's exit once sc is done with (freed here): a reader that left (EPIPE)
** is a cancel (3.4), exit 0 with no done line; any other write error, or
** rc, a failure. */
static int
xr_ref_exit(struct xr_scan *sc, int rc)
{
	int c = xr_scan_cancelled(sc);

	xr_scan_free(sc);
	if (c == 1)
		return 0;
	return c || rc ? xr_failed() : 0;
}

/* One transcript (codex: a Codex rollout, 7.4) through --ref's scan (3.4),
** the per-file reader for <n>'s file and every other session's: its lines
** and its tally. 0; -1 a read failure; -2 not opened (gone, or not the file
** looked up). */
static int
xr_ref_file(struct xr_scan *sc, const char *path, const struct stat *st,
            int codex)
{
	struct xr_chapters ch = {0};
	struct xr_claude *d;
	struct xr_sink k;
	int fd, capped = 0, rc;
	uint64_t span = (uint64_t)st->st_size;

	if ((fd = xr_seam_open(path, st)) < 0)
		return -2;
	if (!(d = xr_claude_new()))
	{
		close(fd);
		return -1;
	}
	if (codex)
		xr_claude_codex(d);
	xr_scan_sink(sc, &k);
	xr_claude_refs(d, &k, (uint64_t)st->st_ino, xr_scan_tick, sc);
	/* light reads newest first: a ref of the last turns inside the budget */
	rc = xr_scan_full(sc) ? xr_claude_read(d, fd, span, &capped)
	     : xr_claude_newest(d, fd, span, &capped);
	close(fd);
	xr_claude_end(d, (uint64_t)st->st_ino, &ch);
	xr_chapters_free(&ch);
	if (capped && span > xr_lim.head + xr_lim.tail)
		span = xr_lim.head + xr_lim.tail;
	xr_scan_file(sc, span, xr_claude_too_long(d), capped, 0);
	xr_claude_free(d);
	return rc < 0 ? -1 : 0;
}

/* A Claude session of this store other than <n> (7.6): a live row's (its
** master's MSG_CMD, never its ring) or a History record's; or a live
** Codex's (codex: sid its thread, cfg its CODEX_HOME, project its cwd's key;
** History records only Claude, 7.4). */
struct xr_cand
{
	char name[600], sid[64], cfg[1400];
	long long seen, dseen;	/* lastSeen; its project dir's newest */
	int live, same, codex;
	char path[2048], project[1200];	/* the lookup's */
	struct stat st;
};

/* This store's Claude and live Codex sessions but self (<n>'s id, or ""),
** one per id: the live rows first, then the History records, newest first.
** Names only: nothing is opened. The count, *out to free; 0 with *out
** NULL. */
static int
xr_corpus(const char *self, struct xr_cand **out)
{
	struct slist sl = {0};
	struct xr_cand *v = NULL, *nv, c;
	struct hrec *r;
	const char *h = getenv("HOME");
	int nr = history_load(&r), n = 0, cap = 0, k, j;

	list_sessions(&sl);
	for (k = 0; k < sl.n + nr; k++)
	{
		memset(&c, 0, sizeof c);
		if (k < sl.n)
		{
			struct cmd_info ci;
			struct crec cr;
			struct xr_cx xc;
			char sock[1100];
			int ok;

			session_sock_path(sl.v[k], sock, sizeof sock);
			if (cmd_query(sock, &ci) != 0)
				continue;
			ok = xr_live_claude(&ci, &cr);
			/* else a live Codex, keyed by its cwd as for <n> (7.4) */
			c.codex = !ok && xr_live_codex(&ci, &xc) > 0;
			if (c.codex && !xc.cwd[0])
				(void)proc_cwd(ci.pid, xc.cwd, sizeof xc.cwd);
			cmd_info_free(&ci);
			if (!ok && !c.codex)
				continue;
			snprintf(c.name, sizeof c.name, "%s", sl.v[k]);
			if (c.codex)
			{
				snprintf(c.sid, sizeof c.sid, "%s", xc.tid);
				snprintf(c.cfg, sizeof c.cfg, "%s", xc.home);
				xr_project_key(xc.cwd, c.project, sizeof c.project);
			}
			else
			{
				snprintf(c.sid, sizeof c.sid, "%s", cr.sid);
				xr_crec_cfg(&cr, c.cfg, sizeof c.cfg);
				if (!c.cfg[0])
					continue;	/* no corpus, as for <n> */
				c.seen = cr.upd / 1000;
			}
			c.live = 1;
		}
		else
		{
			const struct hrec *x = &r[k - sl.n];

			snprintf(c.name, sizeof c.name, "%s", x->name);
			snprintf(c.sid, sizeof c.sid, "%s", x->sid);
			if (x->acfg[0])
				snprintf(c.cfg, sizeof c.cfg, "%s", x->acfg);
			else if (!h || snprintf(c.cfg, sizeof c.cfg, "%s/.claude", h)
			         >= (int)sizeof c.cfg)
				continue;
			c.seen = x->updated;
			c.live = x->live;
		}
		if (!xr_uuid_shaped(c.sid) || !strcmp(c.sid, self))
			continue;
		for (j = 0; j < n && strcmp(v[j].sid, c.sid); j++)
			;
		if (j < n)
			continue;	/* a live row's id: its record is older */
		if (n == cap)
		{
			cap = cap ? cap * 2 : 16;
			if (!(nv = realloc(v, (size_t)cap * sizeof *v)))
				break;
			v = nv;
		}
		v[n++] = c;
	}
	slist_free(&sl);
	hrec_free(r, nr);
	*out = v;
	return n;
}

static int
xr_cand_cmp(const void *a, const void *b)
{
	const struct xr_cand *x = a, *y = b;
	int c;

	if (x->same != y->same)
		return y->same - x->same;
	if (x->dseen != y->dseen)
		return x->dseen < y->dseen ? 1 : -1;
	if ((c = strcmp(x->project, y->project)))
		return c;
	if (x->seen != y->seen)
		return x->seen < y->seen ? 1 : -1;
	return (c = strcmp(x->name, y->name)) ? c : strcmp(x->sid, y->sid);
}

/* The other sessions' Claude and Codex files (7.6), each mapped like <n>'s
** by names only (xr_claude_lookup: a uuid, a valid config_dir, one regular
** <sid>.jsonl, no symlink; xr_codex_lookup for a Codex; nothing opened), in
** 3.4's order: <n>'s project dir newest lastSeen first, then (all) the
** other dirs, newest first. The count, *out to free. */
static int
xr_mapped(const char *self, const char *project, int all,
          struct xr_cand **out)
{
	struct xr_cand *v;
	int n = xr_corpus(self, &v), m = 0, k, j;

	for (k = 0; k < n; k++)
	{
		if (v[k].codex ? xr_codex_lookup(v[k].cfg, v[k].sid, v[k].path,
		                                 sizeof v[k].path, &v[k].st) < 0
		    : xr_claude_lookup(v[k].cfg, v[k].sid, v[k].path,
		                       sizeof v[k].path, &v[k].st, v[k].project,
		                       sizeof v[k].project) < 0)
			continue;	/* unmapped: never opened */
		if (v[k].codex)	/* lastSeen: the rollout's last write */
			v[k].seen = (long long)v[k].st.st_mtime;
		v[k].same = !strcmp(v[k].project, project);
		if (!v[k].same && !all)
			continue;
		if (m != k)
			v[m] = v[k];
		m++;
	}
	for (k = 0; k < m; k++)
		for (j = 0; j < m && !v[k].same; j++)
			if (!strcmp(v[j].project, v[k].project) &&
			    v[j].seen > v[k].dseen)
				v[k].dseen = v[j].seen;
	if (m)
		qsort(v, (size_t)m, sizeof *v, xr_cand_cmp);
	*out = v;
	return m;
}

/* 3.2's other_projects: the sessions --scope all would add, from names
** only (K4.5): nothing opened, nothing scanned. */
static long long
xr_other_projects(const char *self, const char *project)
{
	struct xr_cand *v;
	int n = xr_mapped(self, project, 1, &v), k;
	long long c = 0;

	for (k = 0; k < n; k++)
		c += !v[k].same;
	free(v);
	return c;
}

/* --scope project|all after <n>'s own file or ring (3.4, 7.6): the other
** sessions' Claude and Codex files in xr_mapped's order, never a ring. 0,
** or -1 a read failure. */
static int
xr_ref_others(struct xr_scan *sc, int scope, const char *self,
              const char *project)
{
	struct xr_cand *v;
	int m, k, rc = 0;

	if (!scope)
		return 0;
	m = xr_mapped(self, project, scope == 2, &v);
	for (k = 0; k < m && rc == 0; k++)
	{
		if (xr_scan_next(sc, v[k].name, v[k].project, v[k].live, v[k].same))
			break;
		if ((rc = xr_ref_file(sc, v[k].path, &v[k].st, v[k].codex)) == -2)
			rc = 0;	/* gone since the lookup: not read */
	}
	/* sc's session names point into v: only done (no names) follows */
	free(v);
	return rc;
}

/* --search over one transcript or rollout (#297): its newest hits, one
** object. No other session's file is read. */
static int
xr_search_one(const char *path, const struct stat *st, int codex,
              const char *session, const char *sid, const char *text)
{
	struct xr_search *s = xr_search_new(text, xr_t0);
	int fd, rc;

	if (!s)
		return xr_failed();
	if ((fd = xr_seam_open(path, st)) < 0)
	{
		xr_search_free(s);
		return xr_failed();
	}
	rc = xr_search_file(s, fd, (uint64_t)st->st_size, (uint64_t)st->st_ino,
	                    codex);
	close(fd);
	rc = rc < 0 ? xr_failed()
	     : xr_out_exit(xr_search_print(stdout, s, session, sid,
	                                   codex ? "codex" : "claude", NULL) == 0);
	xr_search_free(s);
	return rc;
}

/* --ref over (cfg, sid)'s transcript (3.4): `first` once the file is
** found, before it is opened; then its lines, its tally, `done`. */
static int
xr_claude_ref(const char *cfg, const char *sid, const char *session,
              int live, const char *label, const struct xr_refq *q)
{
	char path[2048], home[1400], project[1200] = "";
	struct xr_scan *sc;
	struct xr_span sp;
	struct stat st;
	int rc;

	if (!cfg[0])
	{
		const char *h = getenv("HOME");

		if (!h || snprintf(home, sizeof home, "%s/.claude", h)
		    >= (int)sizeof home)
			return xr_no_corpus();
		cfg = home;
	}
	if (xr_claude_lookup(cfg, sid, path, sizeof path, &st, project,
	                     sizeof project) < 0)
		return xr_no_corpus();
	if (q->search)
		return xr_search_one(path, &st, 0, session, sid, q->text);
	if (!project[0])
		xr_project_key(label, project, sizeof project);
	if (!xr_ref_parse(q->text, label, &sp))
		return xr_not_ref();
	if (!(sc = xr_scan_new(stdout, sp.key, session, project, live, q->full,
	                       0, q->budget, xr_t0)))
		return xr_failed();
	xr_scan_scope(sc, q->scope);
	if (xr_scan_first(sc, sid, sp.shape, xr_span_text(&sp)) != 0)
		return xr_ref_exit(sc, -1);
	rc = xr_ref_file(sc, path, &st, 0);
	/* XR6, --full: right after <n>'s own file, before the others (#176):
	** it reads only the ref and <n>'s cwd, so a cap or a cut keeps it */
	if (rc == 0)
		xr_scan_resolved(sc, &sp, q->text, label, live);
	if (rc == 0)
		rc = xr_ref_others(sc, q->scope, sid, project);
	/* a cancel stopped the read: no done */
	return xr_ref_exit(sc, rc != 0 || xr_scan_done(sc) != 0);
}

/* x's rollout, decoded (7.4): *d with refs into k, *st its stat. 0, -1 no
** rollout, -2 a read failure. One open, through the seam. */
static int
xr_codex_read(const struct xr_cx *x, struct xr_claude **d, struct stat *st,
              struct xr_sink *k, int (*tick)(void *, uint64_t), void *ctx,
              int *capped)
{
	char path[2048];
	int fd, rc;

	*d = NULL;
	if (xr_codex_lookup(x->home, x->tid, path, sizeof path, st) < 0)
		return -1;
	if ((fd = xr_seam_open(path, st)) < 0)	/* there but unreadable: 3 */
		return errno == EACCES || errno == EPERM || errno == EIO ? -2 : -1;
	if (!(*d = xr_claude_new()))
	{
		close(fd);
		return -2;
	}
	xr_claude_codex(*d);
	xr_claude_refs(*d, k, (uint64_t)st->st_ino, tick, ctx);
	rc = xr_claude_read(*d, fd, (uint64_t)st->st_size, capped);
	close(fd);
	return rc < 0 ? -2 : 0;
}

/* A live Codex's answer (7.4): corpus "codex", mentions only, chapters its
** user turns. 0, -1 no rollout, -2 a read failure (exit 3). */
static int
xr_codex_answer(const struct xr_cx *x, struct xr_answer *a,
                struct xr_chapters *ch, char *label, size_t labelsz,
                struct xr_index *refs)
{
	struct xr_claude *d;
	struct xr_sink k;
	struct stat st;
	int capped = 0, rc;

	xr_index_sink(refs, &k);
	rc = xr_codex_read(x, &d, &st, &k, NULL, NULL, &capped);
	if (d)
	{
		/* the label is the live Codex's cwd, as --ref keys it; the
		** rollout's only when that is unreadable */
		xr_claude_end(d, (uint64_t)st.st_ino, ch);
		if (!label[0] && xr_claude_cwd(d)[0])
			snprintf(label, labelsz, "%s", xr_claude_cwd(d));
		xr_claude_free(d);
	}
	if (rc < 0)
	{
		xr_chapters_free(ch);
		return rc;
	}
	a->corpus = "codex";
	a->available = 1;
	a->mentions_only = 1;
	a->refs = refs;
	a->complete = !capped && ch->total == ch->n;
	a->why = a->complete ? NULL : "range";
	a->ch = ch;
	return 0;
}

/* --ref over a live Codex's rollout (3.4, 7.4): as over a transcript, then
** (--scope project|all) the other sessions' Claude and Codex files. */
static int
xr_codex_ref(const struct xr_cx *x, const char *session, const char *label,
             const struct xr_refq *q)
{
	char path[2048], project[1200];
	struct xr_scan *sc;
	struct xr_span sp;
	struct stat st;
	int rc;

	if (xr_codex_lookup(x->home, x->tid, path, sizeof path, &st) < 0)
		return xr_no_corpus();
	if (q->search)
		return xr_search_one(path, &st, 1, session, x->tid, q->text);
	xr_project_key(label, project, sizeof project);
	if (!xr_ref_parse(q->text, label, &sp))
		return xr_not_ref();
	if (!(sc = xr_scan_new(stdout, sp.key, session, project, 1, q->full,
	                       0, q->budget, xr_t0)))
		return xr_failed();
	xr_scan_scope(sc, q->scope);
	if (xr_scan_first(sc, x->tid, sp.shape, xr_span_text(&sp)) != 0)
		return xr_ref_exit(sc, -1);
	rc = xr_ref_file(sc, path, &st, 1);
	if (rc == 0)	/* XR6, --full: as over a transcript */
		xr_scan_resolved(sc, &sp, q->text, label, 1);
	if (rc == 0)
		rc = xr_ref_others(sc, q->scope, x->tid, project);
	return xr_ref_exit(sc, rc != 0 || xr_scan_done(sc) != 0);
}

/* The shell answer, built as the records stream past. */
struct xr_ring_acc
{
	struct xr_shell sh;
	long long rows;
	uint64_t bytes;		/* record text, for --ref's done */
	int gap, cont, seen;
	int lost;		/* ring_screen's: screen rows lost */
};

static void
xr_ring_rec(void *ctx, const struct xr_rec *x)
{
	struct xr_ring_acc *a = ctx;

	xr_shell_feed(&a->sh, x);
	a->bytes += x->len;
	if (x->kind == RING_K_GAP)
		a->gap = 1;
	else if (x->kind != RING_K_MARK)
	{
		/* the oldest row a soft-wrap continuation: its start is gone */
		if (!a->seen++)
			a->cont = (x->flags & RING_F_CONT) != 0;
		a->rows++;
	}
}

/* The live Claudes under a session's child, as xr_live_claude counts them;
** -1 when the session does not answer (#256). */
static int
xr_claudes(const char *sock)
{
	struct cmd_info ci;
	struct crec c;
	int n;

	if (cmd_query(sock, &ci) != 0)
		return -1;
	n = ci.pid > 0 ? claude_find(ci.pid, 1, &c) : 0;
	cmd_info_free(&ci);
	return n;
}

/* A shell's answer from one ring read and its screen rows (3.2, 7.3), as
** --json and every --follow batch give it: corpus none for a ring that is
** off or paused. Paused for an agent none joined: how many Claudes run
** there, so the answer can say why (#256). */
static void
xr_shell_fill(struct xr_answer *a, const struct ring_snap *rs,
              const struct xr_ring_acc *acc, const struct xr_chapters *ch,
              struct xr_index *refs, const char *sock)
{
	if (rs->status & DCH_RING_S_PAUSED)
		a->agent = (a->claudes = xr_claudes(sock)) >= 0;
	if (rs->status & DCH_RING_S_OFF)
	{
		int novt = 0;

		free(screen_tail(sock, &novt));
		a->reason = novt ? "lite" : "off";
	}
	else if (rs->status & DCH_RING_S_PAUSED)
		;	/* another agent's corpus (Codex: XR3c) */
	else
	{
		int gap = rs->seqgap || acc->gap || acc->lost;

		if (refs)
			xr_index_per_row(refs, !acc->sh.npaired);
		a->refs = refs;
		a->corpus = "shell";
		a->available = 1;
		a->mentions_only = !acc->sh.npaired;
		/* 3.2 (Q16): the rows read, the screen's only when it was */
		a->lines = acc->rows;
		a->chapters_evicted = (rs->status & DCH_RING_S_CHAPTERS) ||
		                      ch->total != ch->n;
		a->ch = ch;
		if (acc->sh.alt)
			a->why = "alt";
		else if (gap || acc->cont || a->chapters_evicted ||
		         (rs->status & DCH_RING_S_EVICTED))
			a->why = "range";	/* rows gone or lost: a 1.43.0
					** master never sets EVICTED, so
					** only this evidence speaks there */
		a->complete = a->why == NULL;
	}
}

/* --ref's record sink over a shell: the scan's tick before each record, as
** over a transcript (3.4: the budget, 10 s, progress, a closed reader); a
** stopped scan reads on but feeds nothing. */
struct xr_ref_ring
{
	struct xr_ring_acc *acc;
	struct xr_scan *sc;
};

static void
xr_ref_rec(void *ctx, const struct xr_rec *x)
{
	struct xr_ref_ring *f = ctx;

	if (!xr_scan_tick(f->sc, f->acc->bytes))
		xr_ring_rec(f->acc, x);
}

/* --search over a live shell's ring and screen (#297), as --ref reads them:
** the ring's newest hits, one object; partial when rows were lost. */
struct xr_search_ring
{
	struct xr_ring_acc *acc;
	struct xr_search *s;
};

static void
xr_search_ring_rec(void *ctx, const struct xr_rec *x)
{
	struct xr_search_ring *f = ctx;

	xr_ring_rec(f->acc, x);
	xr_search_rec(f->s, &f->acc->sh, x);
}

static int
xr_shell_search(const struct xr_target *t, const char *text)
{
	struct xr_chapters ch = {0};
	struct xr_search_ring f;
	struct ring_snap rs;
	int fr = ring_fetch(t->sock, &rs, NULL, NULL), rc, partial;

	if (fr == -3)
		return xr_failed();
	if (fr == -1)
		return xr_no_session();
	if (fr != 0 || (rs.status & (DCH_RING_S_OFF | DCH_RING_S_PAUSED)))
		return xr_no_corpus();
	f.acc = calloc(1, sizeof *f.acc);
	f.s = xr_search_new(text, xr_t0);
	if (!f.acc || !f.s)
	{
		free(f.acc);
		xr_search_free(f.s);
		return xr_failed();
	}
	xr_shell_begin(&f.acc->sh, &ch);
	fr = ring_fetch(t->sock, &rs, xr_search_ring_rec, &f);
	if (fr == 0)
		f.acc->lost = ring_screen(t->sock, &rs, &f.acc->sh,
		                          xr_search_ring_rec, &f, CTL_IDLE_MS);
	xr_shell_end(&f.acc->sh);
	xr_search_ring_end(f.s);
	partial = fr == -4 || rs.seqgap || f.acc->gap || f.acc->cont ||
	          f.acc->lost || (rs.status & DCH_RING_S_EVICTED);
	rc = fr == -1 ? xr_no_session() : fr != 0 && fr != -4 ? xr_failed()
	     : xr_out_exit(xr_search_print(stdout, f.s, t->name, rs.sid, "shell",
	                                   partial ? "range" : NULL) == 0);
	xr_chapters_free(&ch);
	xr_search_free(f.s);
	free(f.acc);
	return rc;
}

/* --ref over a live shell's ring (3.4): the ring's header names it, so
** `first` is line 1 before a record is read; then its lines, its tally,
** `done`. A ring cleared mid-read ends it there, partial: the lines
** already out stand. */
static int
xr_shell_ref(const struct xr_target *t, const struct xr_refq *q,
             const char *label)
{
	char project[1200] = "";
	struct xr_chapters ch = {0};
	struct xr_ref_ring f;
	struct ring_snap rs;
	struct xr_span sp;
	struct xr_sink k;
	int fr, rc;

	if (q->search)
		return xr_shell_search(t, q->text);
	if ((fr = ring_fetch(t->sock, &rs, NULL, NULL)) == -3)
		return xr_failed();
	if (fr == -1)
		return xr_no_session();
	/* a master before #119, a ring off or paused: no corpus */
	if (fr != 0 || (rs.status & (DCH_RING_S_OFF | DCH_RING_S_PAUSED)))
		return xr_no_corpus();
	xr_project_key(label, project, sizeof project);
	if (!xr_ref_parse(q->text, label, &sp))
		return xr_not_ref();
	if (!(f.acc = calloc(1, sizeof *f.acc)))
		return xr_failed();
	if (!(f.sc = xr_scan_new(stdout, sp.key, t->name, project, 1, q->full,
	                         1, q->budget, xr_t0)))
	{
		free(f.acc);
		return xr_failed();
	}
	xr_scan_scope(f.sc, q->scope);
	rc = xr_scan_first(f.sc, rs.sid, sp.shape, xr_span_text(&sp));
	xr_shell_begin(&f.acc->sh, &ch);
	xr_scan_sink(f.sc, &k);
	if (rc == 0 && xr_shell_refs(&f.acc->sh, &k, label) == 0)
	{
		fr = ring_fetch(t->sock, &rs, xr_ref_rec, &f);
		/* light: the screen read gets what is left of the budget, +2 ms
		** so a read it cut ends past it and the tick says "budget";
		** DCH_XRAY_NOCLOCK (tests): --full's 2 s, as the clock is off */
		if (fr == 0 && (f.acc->lost = ring_screen(t->sock, &rs, &f.acc->sh,
		    xr_ref_rec, &f, q->full || xr_noclock ? CTL_IDLE_MS
		    : (int)(q->budget + 2 - (xr_now_ms() - xr_t0)))))
			(void)xr_scan_tick(f.sc, f.acc->bytes);
		xr_shell_end(&f.acc->sh);
		rc = fr != 0 && fr != -4 && fr != -1;
	}
	else
		rc = -1;
	if (rc == 0 && fr != -1)
	{
		int partial = fr == -4 || rs.seqgap || f.acc->gap || f.acc->cont ||
		              f.acc->lost ||
		              (rs.status & (DCH_RING_S_EVICTED | DCH_RING_S_OFF |
		                            DCH_RING_S_PAUSED));

		xr_scan_file(f.sc, f.acc->bytes, 0, partial, !f.acc->sh.npaired);
		xr_scan_resolved(f.sc, &sp, q->text, label, 1);	/* XR6: --full */
		/* then the project's Claude files, never another ring (S17) */
		rc = xr_ref_others(f.sc, q->scope, "", project);
		if (rc == 0)
			rc = xr_scan_done(f.sc);
	}
	xr_shell_end(&f.acc->sh);
	xr_chapters_free(&ch);
	free(f.acc);
	/* hung up after `first`: the lines out stand, no done, as a cancel */
	if (fr == -1 && xr_scan_cancelled(f.sc) == 0)
	{
		xr_scan_free(f.sc);
		return xr_no_session();
	}
	return xr_ref_exit(f.sc, rc);
}

/* snap: --follow's History form, the answer as its one snapshot line */
static int
xr_json(const struct xr_target *t, const struct xr_refq *q, int snap)
{
	struct xr_answer a;
	struct xr_chapters ch = {0};
	char project[1200] = "", label[PATH_MAX] = "", sid[64] = "", cfg[1400];
	char recname[400];
	int rc = 0;
	struct xr_index *refs = NULL;
	struct xr_sink k;

	memset(&a, 0, sizeof a);
	a.snapshot = snap;
	a.session = t->name;
	a.session_id = sid;
	a.corpus = "none";
	a.lines = -1;
	a.complete = 1;
	a.project = project;
	a.project_label = label;
	if (t->kind == XR_ENDED)
	{
		const struct hrec *h = t->rec;

		snprintf(label, sizeof label, "%s", h->acwd[0] ? h->acwd : h->cwd);
		if (q)
			return h->sid[0] ? xr_claude_ref(h->acfg, h->sid, t->name, 0,
			                                 label, q)
			                 : xr_no_corpus();
		if (h->sid[0])
		{
			snprintf(sid, sizeof sid, "%s", h->sid);
			if (!(refs = xr_index_new()))
				return xr_failed();
			rc = xr_claude_answer(h->acfg, h->sid, &a, &ch, project,
			                      sizeof project, label, sizeof label, refs);
		}
		else
		{
			snprintf(recname, sizeof recname, "%s.json", h->file);
			a.session_id = recname;
			a.reason = "ended";
		}
		if (!project[0])
			xr_project_key(label, project, sizeof project);
	}
	else
	{
		struct cmd_info ci;
		struct crec c;
		struct ring_snap rs;
		struct xr_ring_acc *acc = NULL;
		struct xr_cx xc;
		int qc = cmd_query(t->sock, &ci), fr = -3;
		int tries, cx;

		a.live = 1;
		if (qc == -4)
			return xr_no_session();
		if (qc == 0 && ci.pid > 0)
			(void)proc_cwd(ci.pid, label, sizeof label);
		if (qc == 0 && xr_live_claude(&ci, &c))
		{
			snprintf(sid, sizeof sid, "%s", c.sid);
			if (!label[0] || c.cwd[0])
				snprintf(label, sizeof label, "%s", c.cwd);
			xr_crec_cfg(&c, cfg, sizeof cfg);
			if (q)
			{
				cmd_info_free(&ci);
				return cfg[0] ? xr_claude_ref(cfg, c.sid, t->name, 1, label,
				                              q) : xr_no_corpus();
			}
			if (!(refs = xr_index_new()))
				rc = -2;
			else
				rc = cfg[0] ? xr_claude_answer(cfg, c.sid, &a, &ch, project,
				                               sizeof project, label,
				                               sizeof label, refs) : -1;
		}
		else if (qc == 0 && (cx = xr_live_codex(&ci, &xc)) != 0)
		{
			/* a Codex's corpus is its rollout, never the ring (7.4) */
			if (xc.cwd[0])
				snprintf(label, sizeof label, "%s", xc.cwd);
			snprintf(sid, sizeof sid, "%s", xc.tid);
			if (q)
			{
				cmd_info_free(&ci);
				return cx > 0 ? xr_codex_ref(&xc, t->name, label, q)
				              : xr_no_corpus();
			}
			if (cx > 0 && !(refs = xr_index_new()))
				rc = -2;
			else if (cx > 0)
				rc = xr_codex_answer(&xc, &a, &ch, label, sizeof label,
				                     refs);
		}
		else if (q)
		{
			if (qc == 0)
				cmd_info_free(&ci);
			return xr_shell_ref(t, q, label);
		}
		else if (!(acc = malloc(sizeof *acc)))
			rc = -2;
		else
		{
			/* cleared mid-read: start over, the old records dropped */
			for (tries = 0; tries < 3; tries++)
			{
				xr_chapters_free(&ch);
				xr_index_free(refs);
				refs = NULL;
				memset(acc, 0, sizeof *acc);
				xr_shell_begin(&acc->sh, &ch);
				/* refs over the ring, keyed by the pane's cwd (7.3) */
				if ((refs = xr_index_new()))
					xr_index_sink(refs, &k);
				if (refs && xr_shell_refs(&acc->sh, &k, label) != 0)
					break;
				fr = ring_fetch(t->sock, &rs, xr_ring_rec, acc);
				if (fr == 0)
					acc->lost = ring_screen(t->sock, &rs, &acc->sh,
					                        xr_ring_rec, acc, CTL_IDLE_MS);
				xr_shell_end(&acc->sh);
				if (fr != -4)
					break;
			}
			xr_shell_end(&acc->sh);
		}
		if (acc && fr == 0)
		{
			snprintf(sid, sizeof sid, "%s", rs.sid);
			xr_shell_fill(&a, &rs, acc, &ch, refs, t->sock);
		}
		else if (acc && (fr == -3 || fr == -4))
			rc = -2;
		else if (acc && fr == -1)
			rc = -3;	/* gone, or hung up mid-read */
		free(acc);
		/* fr -2: a master before #119: no corpus */
		if (qc == 0)
			cmd_info_free(&ci);
		if (!project[0])
			xr_project_key(label, project, sizeof project);
	}
	if (rc == -2 || rc == -3)
	{
		xr_chapters_free(&ch);
		xr_index_free(refs);
		return rc == -3 ? xr_no_session() : xr_failed();
	}
	a.other_projects = xr_other_projects(sid, project);
	a.available = strcmp(a.corpus, "none") != 0;
	if (a.available && !a.refs && refs && !strcmp(a.corpus, "claude"))
		a.refs = refs;
	dch_trace("xray json: chapters=%zu seam opened=%lu refused=%lu",
	          ch.n, xr_seam_opened, xr_seam_refused);
	rc = xr_out_exit(xr_print(stdout, &a) == 0);
	xr_chapters_free(&ch);
	{
		size_t keys = refs ? xr_index_keys(refs) : 0;

		xr_index_free(refs);
		/* K3: the index lives exactly as long as its query */
		dch_trace("xray refs: keys=%zu live=%lu", keys, xr_index_live);
	}
	return rc;
}

/* ---- dch --xray <n> --chapter <anchor> --json (#156, XR3b; spec 3.5) ---- */

/* The answer once the decode is done: the wanted chapter only while the
** index still holds its anchor, exactly, and the entries gathered are that
** chapter's (K14.2); else it went stale (K14.3). */
static int
xr_chapter_out(const char *session, const char *sid,
               const struct xr_chapters *ch, const struct xr_entries *e)
{
	size_t k;

	if (e->failed)
		return xr_failed();
	for (k = 0; k < ch->n; k++)
		if (!strcmp(xr_chapter_at(ch, k)->anchor, e->anchor) && e->hit &&
		    e->ord == ch->total - ch->n + k)
			return xr_out_exit(xr_chapter_print(stdout, session, sid,
			                                    xr_chapter_at(ch, k), e) == 0);
	fputs("dch: chapter gone\n", stderr);
	return 1;
}

/* One pass over a looked-up transcript (codex: a Codex rollout, 7.4): the
** chapters-only index, and the wanted chapter's entries as it goes. Never a
** seek (K14.2). */
static int
xr_chapter_file(const char *path, const struct stat *st, int codex,
                const char *sid, const char *session, const char *anchor)
{
	struct xr_chapters ch = {0};
	struct xr_entries e;
	struct xr_claude *d;
	int fd, capped, rc;

	if ((fd = xr_seam_open(path, st)) < 0)	/* there but unreadable: 3 */
		return errno == EACCES || errno == EPERM || errno == EIO
		       ? xr_failed() : xr_no_corpus();
	if (!(d = xr_claude_new()))
	{
		close(fd);
		return xr_failed();
	}
	if (codex)
		xr_claude_codex(d);
	xr_entries_begin(&e, anchor, session, XR_CH_OUT);
	xr_claude_entries(d, &e, (uint64_t)st->st_ino);
	rc = xr_claude_read(d, fd, (uint64_t)st->st_size, &capped);
	close(fd);
	xr_claude_end(d, (uint64_t)st->st_ino, &ch);
	xr_claude_free(d);
	rc = rc < 0 ? xr_failed() : xr_chapter_out(session, sid, &ch, &e);
	xr_chapters_free(&ch);
	xr_entries_free(&e);
	return rc;
}

/* --chapter over (cfg, sid)'s transcript. */
static int
xr_chapter_claude(const char *cfg, const char *sid, const char *session,
                  const char *anchor)
{
	char path[2048], home[1400], project[1200];
	struct stat st;

	if (!cfg[0])
	{
		const char *h = getenv("HOME");

		if (!h || snprintf(home, sizeof home, "%s/.claude", h)
		    >= (int)sizeof home)
			return xr_no_corpus();
		cfg = home;
	}
	if (xr_claude_lookup(cfg, sid, path, sizeof path, &st, project,
	                     sizeof project) < 0)
		return xr_no_corpus();
	return xr_chapter_file(path, &st, 0, sid, session, anchor);
}

/* --chapter: a Claude session's transcript (live, ended or History: K8), a
** live Codex's rollout (7.4), else a live shell's ring, one read of it (the
** screen holds no marks, so no chapter). An ended shell, dch-lite, a ring
** that is off or paused: no corpus. A ring cleared mid-read took the anchor
** with it. */
static int
xr_chapter(const struct xr_target *t, const char *anchor)
{
	struct xr_chapters ch = {0};
	struct xr_entries e;
	struct xr_ring_acc *acc;
	struct ring_snap rs;
	struct cmd_info ci;
	struct crec c;
	struct xr_cx xc;
	char cfg[1400];
	int qc, fr, rc, cx;

	if (t->kind == XR_ENDED)
		return t->rec->sid[0] ? xr_chapter_claude(t->rec->acfg, t->rec->sid,
		                                          t->name, anchor)
		                      : xr_no_corpus();
	if ((qc = cmd_query(t->sock, &ci)) == -4)
		return xr_no_session();
	if (qc == 0 && xr_live_claude(&ci, &c))
	{
		cmd_info_free(&ci);
		xr_crec_cfg(&c, cfg, sizeof cfg);
		return cfg[0] ? xr_chapter_claude(cfg, c.sid, t->name, anchor)
		              : xr_no_corpus();
	}
	/* a live Codex: its rollout, never the ring under it (7.4); no
	** mapped thread or no rollout is no corpus */
	if (qc == 0 && (cx = xr_live_codex(&ci, &xc)) != 0)
	{
		char path[2048];
		struct stat st;

		cmd_info_free(&ci);
		return cx > 0 && xr_codex_lookup(xc.home, xc.tid, path, sizeof path,
		                                 &st) == 0
		       ? xr_chapter_file(path, &st, 1, xc.tid, t->name, anchor)
		       : xr_no_corpus();
	}
	if (qc == 0)
		cmd_info_free(&ci);
	if (!(acc = calloc(1, sizeof *acc)))
		return xr_failed();
	xr_shell_begin(&acc->sh, &ch);
	xr_entries_begin(&e, anchor, t->name, XR_CH_OUT);
	acc->sh.want = &e;
	fr = ring_fetch(t->sock, &rs, xr_ring_rec, acc);
	xr_shell_end(&acc->sh);
	free(acc);
	if (fr == -1)
		rc = xr_no_session();
	else if (fr == -2 || (fr == 0 &&
	                      (rs.status & (DCH_RING_S_OFF | DCH_RING_S_PAUSED))))
		rc = xr_no_corpus();
	else if (fr == -4)
	{
		fputs("dch: chapter gone\n", stderr);
		rc = 1;
	}
	else
		rc = fr ? xr_failed() : xr_chapter_out(t->name, rs.sid, &ch, &e);
	xr_chapters_free(&ch);
	xr_entries_free(&e);
	return rc;
}

/* --clear: MSG_RING_CLEAR to a live master; never the pty. The header and
** the clear go over one connection, so a session resolved by its ring
** session_id is cleared only while it still carries that id (shortcut: a
** --restart between the two still lands on the new image; upgrade with an
** XR1a clear that carries the expected id). */
static int
xr_clear(const struct xr_target *t)
{
	struct cmd_info ci;
	struct crec c;
	struct ring_snap rs;
	struct ctl_reader r = {0};
	unsigned char payload[PKT_MAX], type;
	unsigned int len;
	char sid[64] = "";
	const char *corpus = "shell";
	struct xr_cx xc;
	int qc, claude = 0, agent = 0, s, novt = 0, fr, cleared, cx;

	if ((qc = cmd_query(t->sock, &ci)) == -4)
		return xr_no_session();
	if (qc == 0 && xr_live_claude(&ci, &c))
	{
		claude = agent = 1;
		corpus = "claude";
		snprintf(sid, sizeof sid, "%s", c.sid);
	}
	/* the master ignores a clear while any Claude runs (dch_agent_probe:
	** claude_find != 0), so two Claudes or a non-UUID sid clear nothing */
	else if (qc == 0 && ci.pid > 0 && claude_find(ci.pid, 1, &c) != 0)
		agent = 1;
	/* a Codex's corpus is its rollout, never cleared; unmapped, none */
	else if (qc == 0 && (cx = xr_live_codex(&ci, &xc)) != 0)
	{
		claude = agent = 1;
		corpus = cx > 0 ? "codex" : "none";
		snprintf(sid, sizeof sid, "%s", xc.tid);
	}
	if (qc == 0)
		cmd_info_free(&ci);
	if ((s = control_connect_path(t->sock)) < 0)
		return xr_no_session();
	fr = ring_fetch_fd(s, &rs, NULL, NULL);
	if (fr == 0 && t->sid[0] && strcmp(rs.sid, t->sid) != 0)
	{
		close(s);	/* another image took the name: not ours */
		return xr_no_session();
	}
	if (fr == 0 && !claude)
		snprintf(sid, sizeof sid, "%s", rs.sid);
	if (fr == 0 && (rs.status & DCH_RING_S_OFF))
		free(screen_tail(t->sock, &novt));
	if (fr == -2 || novt)
	{
		close(s);
		fputs("dch: no x-ray corpus\n", stderr);
		return 1;
	}
	if (fr != 0)
	{
		close(s);
		return fr == -1 ? xr_no_session() : xr_failed();
	}
	cleared = !agent && !(rs.status & (DCH_RING_S_OFF | DCH_RING_S_PAUSED));
	if (!claude && (rs.status & DCH_RING_S_OFF))
		corpus = "none";
	/* no ring to clear: the mirror stays (QA 1.44 MED-1) */
	if (cleared)
		send_ctl(s, MSG_RING_CLEAR, NULL, 0);
	if (cleared &&
	    (ctl_next_frame(s, &r, CTL_IDLE_MS, &type, payload, &len) != 1 ||
	     type != MSG_ACK || len != DCH_ST_OK))
	{
		close(s);	/* a hang-up before the ack: the session is gone */
		return r.eof ? xr_no_session() : xr_failed();
	}
	close(s);
	dch_trace("xray clear: ring_cleared=%d", cleared);
	fputs("{\"schema\":1,\"session\":", stdout);
	xr_put_str(stdout, t->name);
	fputs(",\"session_id\":", stdout);
	xr_put_str(stdout, sid);
	printf(",\"corpus\":\"%s\",\"ring_cleared\":%s}\n", corpus,
	       cleared ? "true" : "false");
	return xr_out_exit(fflush(stdout) == 0 && !ferror(stdout));
}

/* ---- dch --xray <n> --follow --json (#136, XR2b; spec 3.3) -------------- */

/* One loop: the target's answer as a snapshot line, then deltas as the
** corpus moves (xr_delta: a diff against what was written, so the deltas
** applied are the --json of the same moment). A reset goes round again, so
** a snapshot always follows it. Events only, never a timer (K3): the one
** finite wait is the coalescing deadline, armed only while an event waits;
** the one exception, a paused shell follow polls the Claude count once a
** second (LOW-X16). */
#define XF_BATCH_MS 250		/* 3.3: at most one batch per 250 ms */
#define XF_CLAUDES_MS 1000L	/* paused: the Claude count's poll (LOW-X16) */
#define XF_EVICT_KEYS 256	/* proposal 11.6: past this, one reset ... */
#define XF_EVICT_MS 1000	/* ... at most once a second */
#define XF_SLICE (4u << 20)	/* the full pass: bytes between events */
#define XF_RESET (-1)

enum { XE_FILE = 1, XE_ROT = 2, XE_CONN = 4, XE_OUT = 8 };

struct xf
{
	const struct xr_target *t;
	struct xr_sent *sent;
	int q;			/* kqueue, or inotify; -1 none */
	int conn;		/* the subscribed control connection, -1 none */
	struct ctl_reader rd;
#ifdef __linux__
	int wf, wd;		/* inotify watches: the transcript, its dir */
	int wd2;		/* a Codex's second dir (7.4) */
#else
	int dirfd, dirfd2;
#endif
	long long last;		/* the last batch (a snapshot is one) */
	long long evict_at;	/* the last "evicted" reset, -1 none */
	long gap;		/* the coalescing interval: 250 ms, or twice the
				** last build when that is longer */
	int pending, rc;
	const char *why;	/* XF_RESET's reason, for the next snapshot */
	unsigned long woke, timed;	/* 9.5's no-timer seam */
};

static int
xf_watch_open(struct xf *f)
{
	f->conn = -1;
	memset(&f->rd, 0, sizeof f->rd);
#ifdef __linux__
	f->wf = f->wd = f->wd2 = -1;
	f->q = inotify_init1(IN_CLOEXEC | IN_NONBLOCK);
#else
	struct kevent ev;

	f->dirfd = f->dirfd2 = -1;
	if ((f->q = kqueue()) >= 0)
	{
		/* EV_CLEAR: once per drain, and EV_EOF when the reader leaves,
		** so an idle follower ends at once (a pipe gives no POLLHUP) */
		EV_SET(&ev, 1, EVFILT_WRITE, EV_ADD | EV_CLEAR, 0, 0, NULL);
		(void)kevent(f->q, &ev, 1, NULL, 0, NULL);
		(void)fcntl(f->q, F_SETFD, FD_CLOEXEC);
	}
#endif
	return f->q < 0 ? -1 : 0;
}

static void
xf_watch_close(struct xf *f)
{
	if (f->conn >= 0)
		close(f->conn);
#ifndef __linux__
	if (f->dirfd >= 0)
		close(f->dirfd);
	if (f->dirfd2 >= 0)
		close(f->dirfd2);
	f->dirfd = f->dirfd2 = -1;
#endif
	if (f->q >= 0)
		close(f->q);
	f->conn = f->q = -1;
}

static void
xf_watch_conn(struct xf *f, int s)
{
	f->conn = s;
#ifndef __linux__
	{
		struct kevent ev;

		EV_SET(&ev, s, EVFILT_READ, EV_ADD, 0, 0, NULL);
		(void)kevent(f->q, &ev, 1, NULL, 0, NULL);
	}
#endif
}

/* The transcript (its fd: the inode we read) and its project dir. */
static void
xf_watch_file(struct xf *f, int fd, const char *dir)
{
#ifdef __linux__
	char p[64];

	if (fd >= 0)
	{
		snprintf(p, sizeof p, "/proc/self/fd/%d", fd);
		f->wf = inotify_add_watch(f->q, p, IN_MODIFY | IN_DELETE_SELF |
		                          IN_MOVE_SELF);
	}
	if (dir && f->wd >= 0)
		(void)inotify_rm_watch(f->q, f->wd);	/* moved (LOW-6) */
	if (dir)
		f->wd = inotify_add_watch(f->q, dir, IN_CREATE | IN_MOVED_TO |
		                          IN_DELETE | IN_ONLYDIR);
#else
	struct kevent ev[2];
	int n = 0;

	if (dir && f->dirfd >= 0)
		close(f->dirfd);	/* moved (LOW-6): its kevent goes with it */
	if (fd >= 0)
		EV_SET(&ev[n++], fd, EVFILT_VNODE, EV_ADD | EV_CLEAR,
		       NOTE_WRITE | NOTE_EXTEND | NOTE_ATTRIB | NOTE_DELETE |
		       NOTE_RENAME, 0,
		       NULL);
#ifdef O_EVTONLY
	if (dir && (f->dirfd = open(dir, O_EVTONLY | O_DIRECTORY |
	                                 O_CLOEXEC)) >= 0)
#else
	if (dir && (f->dirfd = open(dir, O_RDONLY | O_DIRECTORY |
	                                 O_CLOEXEC)) >= 0)
#endif
		EV_SET(&ev[n++], f->dirfd, EVFILT_VNODE, EV_ADD | EV_CLEAR,
		       NOTE_WRITE, 0, NULL);
	if (n)
		(void)kevent(f->q, ev, n, NULL, 0, NULL);
#endif
	dch_trace("xray follow: watch file=%d dir=%d", fd >= 0, dir != NULL);
}

/* A second dir whose entries changing is a rotation (a Codex's snapshot
** dir beside its day dir, 7.4). */
static void
xf_watch_dir2(struct xf *f, const char *dir)
{
#ifdef __linux__
	f->wd2 = inotify_add_watch(f->q, dir, IN_CREATE | IN_MOVED_TO |
	                           IN_DELETE | IN_MOVED_FROM | IN_ONLYDIR);
#else
	struct kevent ev;

#ifdef O_EVTONLY
	if ((f->dirfd2 = open(dir, O_EVTONLY | O_DIRECTORY | O_CLOEXEC)) >= 0)
#else
	if ((f->dirfd2 = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC)) >= 0)
#endif
	{
		EV_SET(&ev, f->dirfd2, EVFILT_VNODE, EV_ADD | EV_CLEAR, NOTE_WRITE,
		       0, NULL);
		(void)kevent(f->q, &ev, 1, NULL, 0, NULL);
	}
#endif
	dch_trace("xray follow: watch dir2");
}

/* A Claude's session record, in dir2's place (QA 1.52.0 LOW-6): a /clear
** rewrites it in place, maybe after its new transcript appeared, which no
** other watch sees. Opened again before each question, so a record
** replaced by a rename stays watched. */
static void
xf_watch_rec(struct xf *f, const char *path)
{
#ifdef __linux__
	f->wd2 = inotify_add_watch(f->q, path, IN_MODIFY | IN_ATTRIB |
	                           IN_DELETE_SELF | IN_MOVE_SELF);
#else
	struct kevent ev;

	if (f->dirfd2 >= 0)
		close(f->dirfd2);
#ifdef O_EVTONLY
	if ((f->dirfd2 = open(path, O_EVTONLY | O_CLOEXEC)) >= 0)
#else
	if ((f->dirfd2 = open(path, O_RDONLY | O_CLOEXEC)) >= 0)
#endif
	{
		EV_SET(&ev, f->dirfd2, EVFILT_VNODE, EV_ADD | EV_CLEAR,
		       NOTE_WRITE | NOTE_EXTEND | NOTE_DELETE | NOTE_RENAME, 0,
		       NULL);
		(void)kevent(f->q, &ev, 1, NULL, 0, NULL);
	}
#endif
}

/* The events in, waiting at most ms (-1: until one comes). */
static int
xf_wait(struct xf *f, long ms)
{
	int ev = 0, k, n;

	if (ms > 0)
	{
		f->timed++;
		dch_trace("xray follow: deadline %ld ms", ms);
	}
#ifdef __linux__
	{
		struct pollfd p[3] = { { f->q, POLLIN, 0 }, { 1, 0, 0 },
		                       { f->conn, POLLIN, 0 } };
		char buf[4096] __attribute__((aligned(8)));
		ssize_t got;

		n = poll(p, f->conn >= 0 ? 3 : 2, (int)ms);
		if (n > 0 && (p[1].revents & (POLLERR | POLLHUP | POLLNVAL)))
			ev |= XE_OUT;
		if (n > 0 && f->conn >= 0 && p[2].revents)
			ev |= XE_CONN;
		while (n > 0 && (p[0].revents & POLLIN) &&
		       (got = read(f->q, buf, sizeof buf)) > 0)
		{
			for (k = 0; k + (int)sizeof(struct inotify_event) <= got;)
			{
				const struct inotify_event *e =
				    (const struct inotify_event *)(buf + k);

				if (e->wd == f->wf)
					ev |= e->mask & IN_MODIFY ? XE_FILE : XE_ROT;
				else if (e->wd == f->wd || e->wd == f->wd2)
					ev |= XE_ROT;
				k += (int)sizeof *e + (int)e->len;
			}
		}
	}
#else
	{
		struct kevent out[8];
		struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };

		n = kevent(f->q, NULL, 0, out, 8, ms < 0 ? NULL : &ts);
		for (k = 0; k < n; k++)
			if (out[k].filter == EVFILT_WRITE)
				ev |= out[k].flags & EV_EOF ? XE_OUT : 0;
			else if (out[k].filter == EVFILT_READ)
				ev |= XE_CONN;
			else if ((int)out[k].ident == f->dirfd ||
			         (int)out[k].ident == f->dirfd2)
				ev |= XE_ROT;
			else
				ev |= out[k].fflags & (NOTE_DELETE | NOTE_RENAME)
				      ? XE_ROT | XE_FILE : XE_FILE;
	}
#endif
	if (n < 0 && errno != EINTR)
		ev |= XE_OUT;	/* the wait itself failed: end, never spin */
	if (n > 0 || ms != 0)
	{
		f->woke++;
		dch_trace("xray follow: woke ev=%d", ev);
	}
	return ev;
}

/* Frames already in, and those the socket holds: 1 a wake, 0 none, -1 the
** connection is gone (the master exited, or --restart dropped it). */
static int
xf_drain(struct xf *f)
{
	unsigned char payload[PKT_MAX], type;
	unsigned int len;
	int rc, wake = 0;

	if (f->conn < 0)
		return 0;
	while ((rc = ctl_next_frame(f->conn, &f->rd, 0, &type, payload,
	                            &len)) == 1)
		wake |= type == MSG_RING_WAKE;
	return rc < 0 ? -1 : wake;
}

/* A write failed: the reader left (EPIPE, exit 0), else 3. */
static int
xf_out(struct xf *f)
{
	if (fflush(stdout) == 0 && !ferror(stdout))
		return 0;
	f->rc = xr_out_exit(0);
	return 1;
}

static int
xf_line(struct xf *f, const char *what, const char *v)
{
	printf("{\"schema\":1,\"%s\":{%s}}\n", what, v);
	return xf_out(f);
}

/* The reset (when set) and a's snapshot; what was written is all of a. */
static int
xf_snapshot(struct xf *f, struct xr_answer *a, const char *reset)
{
	char b[64];

	if (reset)
	{
		snprintf(b, sizeof b, "\"why\":\"%s\"", reset);
		if (xf_line(f, "reset", b))
			return 1;
	}
	a->snapshot = 1;
	a->sent = f->sent;	/* the refs its 4 MiB cap leaves out (LOW-9) */
	if (xr_print(stdout, a) != 0)
	{
		f->rc = ferror(stdout) ? xr_out_exit(0) : xr_failed();
		return 1;
	}
	if (xf_out(f))
		return 1;
	if (xr_sent_take(f->sent, a) != 0)
	{
		f->rc = xr_failed();
		return 1;
	}
	f->last = xr_now_ms();
	dch_trace("xray follow: snapshot%s%s keys=%zu", reset ? " after " : "",
	          reset ? reset : "", a->refs ? xr_index_keys(a->refs) : 0);
	return 0;
}

static int
xf_deltas(struct xf *f, const struct xr_answer *a)
{
	if (xr_delta_write(stdout, f->sent, a) != 0)
	{
		f->rc = ferror(stdout) ? xr_out_exit(0) : xr_failed();
		return 1;
	}
	return xf_out(f);
}

/* A batch is due now, or in the ms returned (-1: none waits). */
static long
xf_due(const struct xf *f)
{
	long long left;

	if (!f->pending)
		return -1;
	left = f->last + f->gap - xr_now_ms();
	return left > 0 ? (long)left : 0;
}

/* took: the build alone, measured before its lines were written (time
** blocked on a slow reader is no build time: QA 1.52.0 LOW-8) */
static void
xf_built(struct xf *f, long long took)
{
	f->last = xr_now_ms();
	/* a rebuild that takes long spaces the next out: at most a third of
	** a core however big the ring */
	f->gap = took * 2 > XF_BATCH_MS ? (long)(took * 2) : XF_BATCH_MS;
}

/* ---- Claude: the transcript's tail (3.3 Tail) ---- */

struct xf_cl
{
	struct xr_claude *d, *d2;	/* live; the full pass (3.3 item 1) */
	struct xr_index *ix, *ix2;
	int fd, capped;
	uint64_t ino;
	char cfg[1400], sid[64], path[2048], project[1200], label[PATH_MAX];
	const struct xr_cx *cx;		/* a Codex rollout (7.4), else NULL */
	char watch[2048];		/* Codex, no rollout yet: the dir watched */
	const char *rec;		/* a Claude's session record, else NULL */
};

static int
xf_slice(void *ctx, uint64_t bytes)
{
	(void)ctx;
	return bytes >= XF_SLICE;
}

static void
xf_cl_free(struct xf_cl *s)
{
	xr_claude_free(s->d);
	xr_claude_free(s->d2);
	xr_index_free(s->ix);
	xr_index_free(s->ix2);
	if (s->fd >= 0)
		close(s->fd);
	s->d = s->d2 = NULL;
	s->ix = s->ix2 = NULL;
	s->fd = -1;
}

/* --follow's other_projects (3.2), counted once per stream: every batch
** builds an answer, only a snapshot prints it */
static long long
xf_others(const char *self, const char *project)
{
	static long long n = -1;

	if (n < 0 && project[0])
		n = xr_other_projects(self, project);
	return n < 0 ? 0 : n;
}

static void
xf_cl_answer(const struct xr_target *t, struct xf_cl *s, struct xr_answer *a)
{
	memset(a, 0, sizeof *a);
	a->session = t->name;
	a->session_id = s->sid;
	a->live = 1;
	a->corpus = "none";
	a->lines = -1;
	a->complete = 1;
	a->project = s->project;
	a->project_label = s->label;
	a->other_projects = xf_others(s->sid, s->project);
	if (!s->d)
		return;
	if ((!s->cx || !s->label[0]) && xr_claude_cwd(s->d)[0])
		snprintf(s->label, sizeof s->label, "%s", xr_claude_cwd(s->d));
	a->corpus = s->cx ? "codex" : "claude";
	a->mentions_only = s->cx != NULL;
	a->available = 1;
	a->ch = xr_claude_chapters(s->d);
	a->refs = s->ix;
	a->complete = !s->capped && a->ch->total == a->ch->n;
	a->why = a->complete ? NULL : "range";
}

/* Still the stream's transcript? Asked of the master, as --ls-json does:
** 0 yes, 1 no (rotated: another id, inode or none at all), -1 the session
** is gone. */
/* A Codex with no rollout yet: the deepest of today's sessions/YYYY/MM/DD
** there (Codex names it by local time), so its first rollout is seen.
** shortcut: a rollout made under a day dir newer than the one watched (past
** midnight) is seen at the next event; upgrade to a watch per level if
** that shows. */
static void
xf_cx_dir(const struct xr_cx *x, char *dir, size_t dirsz)
{
	time_t now = time(NULL);
	struct tm tm;
	int k;

	localtime_r(&now, &tm);
	if (snprintf(dir, dirsz, "%s/sessions/%04d/%02d/%02d", x->home,
	             tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday) >= (int)dirsz)
		snprintf(dir, dirsz, "%s", x->home);
	else
		for (k = 0; k < 4 && access(dir, X_OK) != 0; k++)
			*strrchr(dir, '/') = '\0';
}

/* A Claude with no transcript yet: the deepest of <cfg>/projects/<key>,
** <cfg>/projects and <cfg> there, so its first file is seen (LOW-6); never
** above <cfg> */
static void
xf_cl_dir(const struct xf_cl *s, char *dir, size_t dirsz)
{
	if (snprintf(dir, dirsz, "%s/projects/%s", s->cfg, s->project) >=
	    (int)dirsz)
		snprintf(dir, dirsz, "%s/projects", s->cfg);
	while (strlen(dir) > strlen(s->cfg) && access(dir, X_OK) != 0)
		*strrchr(dir, '/') = '\0';
}

static int
xf_cl_same(const struct xr_target *t, const struct xf_cl *s)
{
	struct cmd_info ci;
	struct crec c;
	struct stat st;
	char path[2048], project[1200];
	int qc = cmd_query(t->sock, &ci), same;

	struct xr_cx x;

	if (qc == -4)
		return -1;
	/* a Codex: the same mapping (unmapped stays unmapped: the id "") */
	if (s->cx)
		same = qc == 0 && !xr_live_claude(&ci, &c) &&
		       xr_live_codex(&ci, &x) != 0 && !strcmp(x.tid, s->sid) &&
		       !strcmp(x.home, s->cx->home);
	else
		same = qc == 0 && xr_live_claude(&ci, &c) && !strcmp(c.sid, s->sid);
	if (qc == 0)
		cmd_info_free(&ci);
	/* our record truncated and not yet written (Claude Code rewrites it in
	** place; xf_watch_rec wakes us then): its write wakes us again */
	if (!same && s->rec && stat(s->rec, &st) == 0 && st.st_size == 0)
		return 0;
	if (!same)
		return 1;
	if (s->cx && !s->sid[0])
		return 0;
	if (s->cx ? xr_codex_lookup(s->cx->home, s->sid, path, sizeof path, &st)
	            < 0
	          : xr_claude_lookup(s->cfg, s->sid, path, sizeof path, &st,
	                             project, sizeof project) < 0)
	{
		/* not there yet: a Codex day dir made since moves the watch */
		if (s->fd < 0 && s->cx)
		{
			xf_cx_dir(s->cx, path, sizeof path);
			return strcmp(path, s->watch) != 0;
		}
		return s->fd >= 0;	/* gone, or still not there */
	}
	return s->fd < 0 || (uint64_t)st.st_ino != s->ino;
}

/* c: a Claude's transcript; else x: a Codex's rollout (7.4) */
static int
xf_claude(struct xf *f, const struct crec *c, const struct xr_cx *x,
          const char *label)
{
	const struct xr_target *t = f->t;
	struct xf_cl s;
	struct xr_answer a;
	struct xr_sink k;
	struct ring_snap rs;
	struct stat st;
	char dir[2048] = "", kd[2048];
	const char *h;
	int rc = 0, ev, r, rot;
	long ms;

	memset(&s, 0, sizeof s);
	s.fd = -1;
	if (x)
	{
		/* its rollout, else (none yet, or no id) today's day dir; its
		** snapshot dir besides: a mapping that moves is a rotation */
		s.cx = x;
		snprintf(s.sid, sizeof s.sid, "%s", x->tid);
		snprintf(s.label, sizeof s.label, "%s", x->cwd[0] ? x->cwd : label);
		if (!s.sid[0] || xr_codex_lookup(x->home, s.sid, s.path,
		                                 sizeof s.path, &st) < 0)
		{
			xf_cx_dir(x, dir, sizeof dir);
			snprintf(s.watch, sizeof s.watch, "%s", dir);
			goto read;
		}
		if ((s.fd = xr_seam_open(s.path, &st)) < 0 ||
		    !(s.d = xr_claude_new()) || !(s.ix = xr_index_new()))
		{
			xf_cl_free(&s);
			return xr_failed();
		}
		xr_claude_codex(s.d);
		s.ino = (uint64_t)st.st_ino;
		xr_index_sink(s.ix, &k);
		xr_claude_refs(s.d, &k, s.ino, NULL, NULL);
		if (xr_claude_read(s.d, s.fd, (uint64_t)st.st_size,
		                   &s.capped) < 0)
		{
			xf_cl_free(&s);
			return xr_failed();
		}
		goto read;
	}
	snprintf(s.sid, sizeof s.sid, "%s", c->sid);
	snprintf(s.label, sizeof s.label, "%s", c->cwd[0] ? c->cwd : label);
	s.rec = c->rec;
	xr_crec_cfg(c, s.cfg, sizeof s.cfg);
	if (!s.cfg[0] && (h = getenv("HOME")))
		snprintf(s.cfg, sizeof s.cfg, "%s/.claude", h);
	if (xr_claude_lookup(s.cfg, s.sid, s.path, sizeof s.path, &st,
	                     s.project, sizeof s.project) == 0)
	{
		if ((size_t)snprintf(dir, sizeof dir, "%s/projects/%s", s.cfg,
		                     s.project) >= sizeof dir ||
		    (s.fd = xr_seam_open(s.path, &st)) < 0 ||
		    !(s.d = xr_claude_new()) || !(s.ix = xr_index_new()))
		{
			xf_cl_free(&s);
			return xr_failed();
		}
		s.ino = (uint64_t)st.st_ino;
		xr_index_sink(s.ix, &k);
		xr_claude_refs(s.d, &k, s.ino, NULL, NULL);
		if (xr_claude_read(s.d, s.fd, (uint64_t)st.st_size,
		                   &s.capped) < 0)
		{
			xf_cl_free(&s);
			return xr_failed();
		}
	}
	else
	{
		/* no transcript yet: its project dir, or the projects dir, so
		** the first file is seen (a reset then gives its snapshot) */
		xr_project_key(s.label, s.project, sizeof s.project);
		xf_cl_dir(&s, dir, sizeof dir);
	}
read:
	if (!s.project[0])
		xr_project_key(s.label, s.project, sizeof s.project);
	xf_cl_answer(t, &s, &a);
	if (xf_snapshot(f, &a, f->why))
	{
		xf_cl_free(&s);
		return f->rc;
	}
	if (s.capped && (!(s.d2 = xr_claude_new()) ||
	                 !(s.ix2 = xr_index_new())))
	{
		xf_cl_free(&s);
		return xr_failed();
	}
	if (s.d2 && x)
		xr_claude_codex(s.d2);
	if (s.d2)
	{
		xr_index_sink(s.ix2, &k);
		xr_claude_refs(s.d2, &k, s.ino, xf_slice, NULL);
	}
	/* the master's connection: its EOF is the session's end, its wake the
	** ring resuming after the agent left (no slot is no failure here) */
	if ((r = control_connect_path(t->sock)) < 0 ||
	    (xf_watch_conn(f, r),
	     ring_fetch_rd(r, &f->rd, DCH_RING_F_SUB, NULL, &rs, NULL,
	                   NULL)) == -3 ||
	    xf_drain(f) < 0)
	{
		xf_cl_free(&s);
		return 0;	/* gone already */
	}
	xf_watch_file(f, s.fd, dir[0] ? dir : NULL);
	if (c)
		xf_watch_rec(f, c->rec);
	if (x && (size_t)snprintf(dir, sizeof dir, "%s/shell_snapshots",
	                          x->home) < sizeof dir)
		xf_watch_dir2(f, dir);
	/* lines written between the read and the watch; a record rewritten
	** or a dir made then is asked about once (LOW-6) */
	f->pending = s.fd >= 0 && fstat(s.fd, &st) == 0 &&
	             (uint64_t)st.st_size > xr_claude_next(s.d);
	rot = XE_ROT;
	for (;;)
	{
		ms = xf_due(f);
		/* the full pass runs between events; rot: the first question */
		ev = xf_wait(f, s.d2 || rot ? 0 : ms) | rot;
		rot = 0;
		if (ev & XE_OUT)
			break;
		if (ev & XE_CONN)
		{
			if ((r = xf_drain(f)) < 0)
				break;	/* the master exited */
			/* a wake: re-armed by a header read, then the question */
			if (r && (ring_fetch_rd(f->conn, &f->rd, DCH_RING_F_SUB, NULL,
			                        &rs, NULL, NULL) == -3 || xf_drain(f) < 0))
				break;
			if (r)
				ev |= XE_ROT;
		}
		if ((ev & XE_ROT) && c)
		{
			/* before the question, so a write after it wakes us */
			xf_watch_rec(f, c->rec);
			/* no transcript yet: a dir made below the one watched
			** takes the watch (LOW-6); asked again once watched, as
			** the next level may have come before the watch did */
			while (s.fd < 0 && (xf_cl_dir(&s, kd, sizeof kd),
			                    strcmp(kd, dir) != 0))
			{
				snprintf(dir, sizeof dir, "%s", kd);
				xf_watch_file(f, -1, dir);
			}
		}
		if (ev & XE_ROT)
		{
			if ((r = xf_cl_same(t, &s)) < 0)
				break;
			if (r)
			{
				f->why = "rotated";
				rc = XF_RESET;
				break;
			}
		}
		if ((ev & XE_FILE) && s.fd >= 0)
			f->pending = 1;
		if (f->pending && s.fd >= 0 && xf_due(f) == 0)
		{
			long long t0 = xr_now_ms(), took;
			uint64_t from = xr_claude_next(s.d);
			size_t nref;
			int lost;

			f->pending = 0;
			if (fstat(s.fd, &st) != 0)
			{
				rc = xr_failed();
				break;
			}
			if ((uint64_t)st.st_size < from ||
			    (r = xr_claude_tail(s.d, s.fd, from,
			                        (uint64_t)st.st_size)) == 1)
			{
				f->why = "shrunk";	/* cut under us */
				rc = XF_RESET;
				break;
			}
			if (r < 0)
			{
				rc = xr_failed();
				break;
			}
			xf_cl_answer(t, &s, &a);
			/* lost ignored: past XR_CHAPTERS the oldest chapter drops
			** from --json with no line here; a reset per new chapter
			** would be worse (open decision 17) */
			if (xr_delta(f->sent, &a, 0, &nref, &lost) != 0)
			{
				rc = xr_failed();
				break;
			}
			took = xr_now_ms() - t0;	/* before the write */
			if (xf_deltas(f, &a))
			{
				rc = f->rc;
				break;
			}
			xf_built(f, took);
			dch_trace("xray follow: batch keys=%zu changed=%zu",
			          xr_index_keys(s.ix), nref);
		}
		if (s.d2)
		{
			uint64_t to = xr_claude_next(s.d);

			if ((r = xr_claude_tail(s.d2, s.fd, xr_claude_next(s.d2),
			                        to)) != 0)
			{
				f->why = "shrunk";
				rc = r < 0 ? xr_failed() : XF_RESET;
				break;
			}
			if (xr_claude_next(s.d2) >= to)
			{
				/* caught up: the whole file's index replaces the
				** capped one in one step (3.3 item 1) */
				xr_claude_free(s.d);
				xr_index_free(s.ix);
				s.d = s.d2;
				s.ix = s.ix2;
				s.d2 = NULL;
				s.ix2 = NULL;
				s.capped = 0;
				xr_index_sink(s.ix, &k);
				xr_claude_refs(s.d, &k, s.ino, NULL, NULL);
				xf_cl_answer(t, &s, &a);
				f->why = "full";
				if (xf_snapshot(f, &a, f->why))
				{
					rc = f->rc;
					break;
				}
				f->pending = 0;
			}
		}
	}
	xf_cl_free(&s);
	return rc;
}

/* ---- shell: the ring, over a subscribed connection (3.3 Shell) ---- */

struct xf_sb
{
	struct xr_ring_acc acc;
	struct xr_chapters ch;
	struct xr_index *refs;
	struct ring_snap rs;
	int none;		/* a master before #119: no ring op */
};

static void
xf_sb_free(struct xf_sb *b)
{
	xr_chapters_free(&b->ch);
	xr_index_free(b->refs);
	memset(&b->ch, 0, sizeof b->ch);
	b->refs = NULL;
}

/* The whole ring again, built as the one-shot builds it (so a batch's diff
** is exact): ring_fetch_rd's code, or -5 out of memory. */
static int
xf_sb_build(struct xf *f, struct xf_sb *b, const char *label)
{
	struct xr_sink k;
	int tries, fr = -4, r = 0;

	for (tries = 0; tries < 3 && fr == -4; tries++)
	{
		xf_sb_free(b);
		memset(&b->acc, 0, sizeof b->acc);
		xr_shell_begin(&b->acc.sh, &b->ch);
		if (!(b->refs = xr_index_new()))
			return -5;
		xr_index_sink(b->refs, &k);
		if (xr_shell_refs(&b->acc.sh, &k, label) != 0)
			return -5;
		fr = ring_fetch_rd(f->conn, &f->rd, DCH_RING_F_SUB |
		                   DCH_RING_F_SCREEN, NULL, &b->rs, xr_ring_rec,
		                   &b->acc);
		if (fr == 0)
			b->acc.lost = ring_screen(f->t->sock, &b->rs, &b->acc.sh,
			                          xr_ring_rec, &b->acc, CTL_IDLE_MS);
		xr_shell_end(&b->acc.sh);
	}
	/* moved while we read, or a wake behind the reply (kqueue says
	** nothing of bytes already read): once more */
	if (fr == 0 && (r = xf_drain(f)) < 0)
		return -3;
	if (fr == 0 && (b->rs.woke || r))
		f->pending = 1;
	return fr;
}

static void
xf_sb_answer(const struct xr_target *t, struct xf_sb *b, const char *project,
             const char *label, struct xr_answer *a)
{
	memset(a, 0, sizeof *a);
	a->session = t->name;
	a->session_id = b->rs.sid;
	a->live = 1;
	a->corpus = "none";
	a->lines = -1;
	a->complete = 1;
	a->project = project;
	a->project_label = label;
	a->other_projects = xf_others(b->rs.sid, project);
	if (!b->none)
		xr_shell_fill(a, &b->rs, &b->acc, &b->ch, b->refs, t->sock);
	a->available = strcmp(a->corpus, "none") != 0;
}

#define XF_MODE (DCH_RING_S_OFF | DCH_RING_S_PAUSED)

/* first: nothing written yet, so a fifth subscriber can still say busy. */
/* 1 when the session is gone (cmd_query -4); a live answer is freed */
static int
xf_gone(const struct xr_target *t)
{
	struct cmd_info ci;
	int qc = cmd_query(t->sock, &ci);

	if (qc == 0)
		cmd_info_free(&ci);
	return qc == -4;
}

static int
xf_shell(struct xf *f, const char *label, int first)
{
	const struct xr_target *t = f->t;
	struct xf_sb *b = calloc(1, sizeof *b);
	struct xr_answer a;
	struct ring_snap was;
	char project[1200];
	int rc = 0, ev, fr, r, wc;

	xr_project_key(label, project, sizeof project);
	if (!b)
		return xr_failed();
	if ((r = control_connect_path(t->sock)) < 0)
	{
		free(b);
		return first ? xr_no_session() : 0;
	}
	xf_watch_conn(f, r);
	fr = xf_sb_build(f, b, label);
	if (fr == -1 || fr == -3 || fr == -5)
	{
		rc = fr == -5 ? xr_failed() : fr == -1 || xf_gone(t)
		     ? (first ? xr_no_session() : 0) : xr_failed();
		goto out;
	}
	if (fr == 0 && !(b->rs.status & DCH_RING_S_SUB) &&
	    !(b->rs.status & DCH_RING_S_OFF))
	{
		if (first)
		{
			fputs("dch: busy\n", stderr);	/* 3.3: a fifth */
			rc = 1;
		}
		goto out;
	}
	/* fr -2: a master before #119, no corpus; its EOF still ends us */
	if (fr != 0)
	{
		b->none = 1;
		b->rs.status = DCH_RING_S_OFF;
	}
	xf_sb_answer(t, b, project, label, &a);
	if (xf_snapshot(f, &a, f->why))
	{
		rc = f->rc;
		goto out;
	}
	was = b->rs;
	wc = a.agent ? a.claudes : -1;
	for (;;)
	{
		long long t0, took;
		size_t nref = 0;
		int lost = 0, evict = 0;
		long due = xf_due(f);

		/* paused: the master wakes us only when its agent pid changes,
		** so the Claude count is polled; a change is a reset (LOW-X16) */
		if ((was.status & DCH_RING_S_PAUSED) &&
		    (due < 0 || due > XF_CLAUDES_MS))
			due = XF_CLAUDES_MS;
		ev = xf_wait(f, due);
		if (ev & XE_OUT)
			break;
		if ((ev & XE_CONN) && (r = xf_drain(f)) != 0)
		{
			if (r < 0)
				break;	/* the session ended, or --restart */
			f->pending = 1;
		}
		if ((was.status & DCH_RING_S_PAUSED) && !f->pending &&
		    (r = xr_claudes(t->sock)) >= 0 && r != wc)
			f->pending = 1;
		if (!f->pending || xf_due(f) != 0)
			continue;
		t0 = xr_now_ms();
		f->pending = 0;
		if ((fr = xf_sb_build(f, b, label)) != 0)
		{
			/* hung up mid-batch: the session ended (3.3: exit 0) */
			rc = fr == -1 || (fr == -3 && xf_gone(t)) ? 0 : xr_failed();
			break;
		}
		if (!(b->rs.status & DCH_RING_S_SUB))
		{
			/* our slot went to a fifth while we left a wake unanswered
			** 3 s, and none is free: busy, as a fifth at the start
			** (QA 1.52.0 LOW-7, Prosper's ruling A) */
			fputs("dch: busy\n", stderr);
			rc = 1;
			break;
		}
		xf_sb_answer(t, b, project, label, &a);
		if (strcmp(b->rs.sid, was.sid) ||
		    (b->rs.status & XF_MODE) != (was.status & XF_MODE) ||
		    (a.agent && a.claudes != wc))
		{
			/* a new image (a failed handover), the ring paused for an
			** agent or resumed, or a Claude more or fewer under the
			** pause: what the session is now */
			f->why = "rotated";
			rc = XF_RESET;
			break;
		}
		if (b->rs.status & XF_MODE)
		{
			xf_built(f, xr_now_ms() - t0);
			continue;	/* no ring: nothing to diff */
		}
		took = xr_now_ms() - t0;	/* before the write (LOW-8) */
		if (b->rs.epoch != was.epoch)
		{
			/* ED 3, RIS or --clear: always, never evict (T9g) */
			if (xf_snapshot(f, &a, "cleared"))
			{
				rc = f->rc;
				break;
			}
		}
		else
		{
			evict = b->rs.head > was.head;
			if (xr_delta(f->sent, &a, b->rs.head, &nref, &lost) != 0)
			{
				rc = xr_failed();
				break;
			}
			took = xr_now_ms() - t0;
			if (evict)
			{
				/* 3.3: the step's first line, before its counts */
				printf("{\"schema\":1,\"evict\":{\"seq\":%llu}}\n",
				       (unsigned long long)b->rs.head);
				if (xf_out(f))
				{
					rc = f->rc;
					break;
				}
			}
			if (lost || (evict && nref > XF_EVICT_KEYS &&
			             (f->evict_at < 0 ||
			              t0 - f->evict_at >= XF_EVICT_MS)))
			{
				f->evict_at = t0;
				if (xf_snapshot(f, &a, "evicted"))
				{
					rc = f->rc;
					break;
				}
			}
			else if (xf_deltas(f, &a))
			{
				rc = f->rc;
				break;
			}
		}
		xf_built(f, took);
		dch_trace("xray follow: batch keys=%zu changed=%zu evict=%d",
		          xr_index_keys(b->refs), nref, evict);
		was = b->rs;
	}
out:
	xf_sb_free(b);
	free(b);
	return rc;
}

/* --follow (3.3): a History session's snapshot and exit 0; a live one's,
** then its deltas until it ends or the reader leaves. */
static int
xr_follow(const struct xr_target *t)
{
	struct xf f;
	int rc, first = 1;

	if (t->kind == XR_ENDED)
		return xr_json(t, NULL, 1);
	memset(&f, 0, sizeof f);
	f.t = t;
	f.evict_at = -1;
	f.gap = XF_BATCH_MS;
	f.q = f.conn = -1;
	if (!(f.sent = xr_sent_new()))
		return xr_failed();
	for (;;)
	{
		struct cmd_info ci;
		struct crec c;
		struct xr_cx x;
		char label[PATH_MAX] = "";
		int qc = cmd_query(t->sock, &ci), claude, codex = 0;

		if (qc == -4)
		{
			rc = first ? xr_no_session() : 0;
			break;
		}
		if (qc == 0 && ci.pid > 0)
			(void)proc_cwd(ci.pid, label, sizeof label);
		claude = qc == 0 && xr_live_claude(&ci, &c);
		if (!claude && qc == 0)
			codex = xr_live_codex(&ci, &x);	/* 7.4 */
		if (qc == 0)
			cmd_info_free(&ci);
		if (xf_watch_open(&f) < 0)
		{
			rc = xr_failed();
			break;
		}
		f.pending = 0;
		rc = claude ? xf_claude(&f, &c, NULL, label)
		     : codex ? xf_claude(&f, NULL, &x, label)
		     : xf_shell(&f, label, first);
		xf_watch_close(&f);
		first = 0;
		if (rc != XF_RESET)
			break;
		dch_trace("xray follow: reset %s", f.why);
	}
	dch_trace("xray follow: end rc=%d woke=%lu deadlines=%lu", rc, f.woke,
	          f.timed);
	xr_sent_free(f.sent);
	return rc;
}

/* n names a History record (by name, file or <file>.json): a local name
** with ':' is resolved here before it is read as host:name. */
static int
xr_local(const char *n, const struct hrec *r, int nr)
{
	char cb[256];
	int byfile = xr_record_form(n), k;
	const char *stem = byfile ? cb : canon_name(n, cb, sizeof cb);

	if (byfile)
		snprintf(cb, sizeof cb, "%.*s", (int)(strlen(n) - 5), n);
	for (k = 0; k < nr; k++)
		if (rec_hit(&r[k], n, stem, byfile))
			return 1;
	return 0;
}

/* host:name is a remote session's (#087, #122): no local session or History
** record of that exact name, and the part before the ':' a known host
** (--hosts, remote_hosts or a cached remote row; none while remote hosts
** are off). --xray and the control verbs both route by it. Under xv_route
** the view's row or attach decides instead. */
static int
name_routes_remote(const char *n)
{
	char host[DCH_RHOST_MAX];
	struct hrec *r;
	size_t hl = strcspn(n, ":");
	int nr, hit, known;

	if (xv_route)
		return xv_route > 0 && hl < sizeof host && n[hl] == ':';
	if (!name_is_remote(n) || hl >= sizeof host)
		return 0;
	snprintf(host, sizeof host, "%.*s", (int)hl, n);
	known = dch_remote_known(host);
	if (!known && !dch_remote_on())
		return 0;
	nr = history_load(&r);
	hit = !xr_local(n, r, nr);
	hrec_free(r, nr);
	if (hit && !known && !g_rhint[0])
		snprintf(g_rhint, sizeof g_rhint, "%s", host);
	return hit && known;
}

/* The X-ray view (#175): `dch --xray <n>`, no --json, on this terminal as
** the picker runs (the alternate screen, raw), restored on every way out,
** a signal's too (picker_sigint). stdout not a terminal: refused, exit 2. */
static int
xray_view(const char *n)
{
	struct sigaction sa, owinch;
	char err[400];
	int fd = isatty(STDOUT_FILENO) ? open("/dev/tty", O_RDWR | O_CLOEXEC)
	         : -1, rc;

	if (fd < 0 || tcgetattr(fd, &picker_saved_term) < 0)
	{
		if (fd >= 0)
			close(fd);
		fputs("dch: the --xray view needs a terminal (--json prints the "
		      "object)\n", stderr);
		return 2;
	}
	picker_tty_fd = fd;
	picker_term_saved = 1;
	signal(SIGINT, picker_sigint);
	signal(SIGTERM, picker_sigint);
	signal(SIGHUP, picker_sigint);
	/* no SA_RESTART: a resize wakes the view's wait at once */
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = pk_on_winch;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGWINCH, &sa, &owinch);
	picker_raw_on(fd);
	picker_alt = 1;
	tw(fd, "\033[?1049h\033[?7l\033[H\033[2J\033[>1u");
	rc = xv_run(fd, n, err, sizeof err);
	picker_restore();
	signal(SIGINT, SIG_DFL);
	signal(SIGTERM, SIG_DFL);
	signal(SIGHUP, SIG_DFL);
	sigaction(SIGWINCH, &owinch, NULL);
	if (rc)
		fprintf(stderr, "%s\n", err);
	return rc;
}

/* Inside a bracketed paste after the n bytes at c (#230): in, then the last
** ESC[200~ (1) or ESC[201~ (0) in them. Pure. */
static int
xv_paste_in(const unsigned char *c, size_t n, int in)
{
	size_t i;

	for (i = 0; i + 6 <= n; i++)
		if (!memcmp(c + i, "\033[20", 4) && c[i + 5] == '~' &&
		    (c[i + 4] == '0' || c[i + 4] == '1'))
			in = c[i + 4] == '0';
	return in;
}

/* The in-attach X-ray chord's view (#187), in attach.c's child: this
** session's, opened on Refs (#294); a remote attach's is host:name's (#189),
** --host's host put in front. A failure is said and waits for a key: the
** attach's repaint on return would wipe it at once. */
static int
attach_xray_view(void)
{
	char n[DCH_RHOST_MAX + sizeof session_name];
	unsigned char c[4096 + 5];
	struct pollfd p = { STDIN_FILENO, POLLIN, 0 };
	long long t0 = 0;
	size_t k = 0;
	ssize_t r;
	int rc, in = 0;

	xv_tab = 1;
	snprintf(n, sizeof n, "%s%s%s", remote_host ? remote_host : "",
	         remote_host ? ":" : "", session_name);
	xv_route = attach_remote ? 1 : -1;
	rc = xray_view(n);
	xv_route = 0;
	if (rc != 0)
	{
		tw(STDERR_FILENO, "\r(any key: back to the session)");
		/* the any key is the whole read and what follows it within
		** 100 ms (1 s inside a bracketed paste, to its end): an arrow
		** key, a UTF-8 character or a paste of any size, none of it
		** for the session (LOW-R8, #230). The last 5 bytes carry over
		** for a marker cut across two reads. shortcut: a paste
		** paused longer than that, or still coming 2 s on, leaves its
		** rest to the session; a clock-free end needs the paste's
		** own end marker, which an unbracketed paste does not have */
		while ((r = read(STDIN_FILENO, c + k, sizeof c - k)) > 0)
		{
			if (!t0)
				t0 = xr_now_ms();
			k += (size_t)r;
			in = xv_paste_in(c, k, in);
			if (k > 5)
			{
				memmove(c, c + k - 5, 5);
				k = 5;
			}
			if (xr_now_ms() - t0 > 2000 ||
			    poll(&p, 1, in ? 1000 : 100) <= 0)
				break;
		}
	}
	return rc;
}

/* `dch --xray <n> [--json] [--clear|--follow]`, `--ref <text> [--scope
** session|project|all] [--full] [--budget-ms <ms>] --json`, `--search <text>
** --json` (#297) and `--chapter <anchor> --json` (spec 3.1, 3.3-3.5): flags
** in any order, each once;
** `dch --xray <n>` alone is the view (#175, xray_view; a host:name's too,
** #188, each query over its own ctl connection). A
** host:name with --json (#157, spec 4.1) runs on the host over ctl once
** 3.1's rules pass here, whatever this dch runs locally: the host answers.
** --requests (#158, spec 4.3) holds a warm connection for requests read off
** stdin. */
static int
xray_main(int argc, char **argv)
{
	const char *n = NULL, *ref = NULL, *scope = NULL, *budget = NULL, *e;
	const char *chapter = NULL, *search = NULL;
	int json = 0, clear = 0, full = 0, follow = 0, k, rc, nr, ni = 0;
	int requests = 0;
	struct hrec *r = NULL;
	struct xr_target t;
	struct xr_refq q;
	struct xr_span sp;

	xr_t0 = xr_now_ms();
	xr_noclock = getenv("DCH_XRAY_NOCLOCK") != NULL;	/* tests only */
	/* tests only: 3.8's --ref caps lowered, never raised (files, bytes,
	** ms, the per-file head and tail) */
	if ((e = getenv("DCH_XRAY_CAPS")))
	{
		unsigned long long c[5] = {0};

		(void)sscanf(e, "%llu:%llu:%llu:%llu:%llu", &c[0], &c[1], &c[2],
		             &c[3], &c[4]);
		if (c[0] && c[0] < xr_lim.files)
			xr_lim.files = c[0];
		if (c[1] && c[1] < xr_lim.bytes)
			xr_lim.bytes = c[1];
		if (c[2] && c[2] < (unsigned long long)xr_lim.ms)
			xr_lim.ms = (long long)c[2];
		if (c[3] && c[3] < xr_lim.head)
			xr_lim.head = c[3];
		if (c[4] && c[4] < xr_lim.tail)
			xr_lim.tail = c[4];
	}
	for (k = 0; k < argc; k++)
	{
		const char **v = !strcmp(argv[k], "--ref") ? &ref
		                 : !strcmp(argv[k], "--scope") ? &scope
		                 : !strcmp(argv[k], "--budget-ms") ? &budget
		                 : !strcmp(argv[k], "--chapter") ? &chapter
		                 : !strcmp(argv[k], "--search") ? &search : NULL;

		if (v && !*v && k + 1 < argc)
			*v = argv[++k];	/* the next element, even "-x" (3.1) */
		else if (!strcmp(argv[k], "--json") && !json)
			json = 1;
		else if (!strcmp(argv[k], "--clear") && !clear)
			clear = 1;
		else if (!strcmp(argv[k], "--full") && !full)
			full = 1;
		else if (!strcmp(argv[k], "--follow") && !follow)
			follow = 1;
		else if (!strcmp(argv[k], "--requests") && !requests)
			requests = 1;
		else if (argv[k][0] == '-' && argv[k][1])
		{
			fprintf(stderr, "dch: bad usage: %s%s\n", argv[k],
			        v && *v ? " twice" : v ? " needs a value"
			        : (!strcmp(argv[k], "--json") ||
			           !strcmp(argv[k], "--clear") ||
			           !strcmp(argv[k], "--full") ||
			           !strcmp(argv[k], "--follow") ||
			           !strcmp(argv[k], "--requests")) ? " twice"
			        : " is not an --xray flag of this dch");
			return 2;
		}
		else if (!n)
			n = argv[ni = k];
		else
		{
			fputs("dch: bad usage: one session per --xray\n", stderr);
			return 2;
		}
	}
	/* every form is one name-shaped word (3.1: a record name validated
	** like a name); xr_resolve tells the forms apart */
	if ((scope || full || budget) && !ref)
	{
		fputs("dch: bad usage: --scope, --full and --budget-ms come with "
		      "--ref\n", stderr);
		return 2;
	}
	if (ref && clear)
	{
		fputs("dch: bad usage: --ref and --clear\n", stderr);
		return 2;
	}
	if (search && (ref || chapter || clear || follow))
	{
		fprintf(stderr, "dch: bad usage: --search and %s\n",
		        ref ? "--ref" : chapter ? "--chapter" : clear ? "--clear"
		        : "--follow");
		return 2;
	}
	if (follow && (ref || clear))
	{
		fprintf(stderr, "dch: bad usage: --follow and %s\n",
		        ref ? "--ref" : "--clear");
		return 2;
	}
	if (chapter && (ref || clear || follow))
	{
		fprintf(stderr, "dch: bad usage: --chapter and %s\n",
		        ref ? "--ref" : clear ? "--clear" : "--follow");
		return 2;
	}
	if (!n || !name_ok(n))
	{
		fputs("dch: bad session\n", stderr);
		return 2;
	}
	if (requests)
	{
		/* #158, spec 4.3: each request's flags come on stdin; a local
		** session takes one dch --xray per request (K4.1) */
		if (!json || ref || chapter || clear || follow || scope || full ||
		    budget || search)
		{
			fputs("dch: bad usage: --requests takes host:name and --json, "
			      "nothing else\n", stderr);
			return 2;
		}
		if (!name_routes_remote(n))
		{
			fputs("dch: bad usage: --requests takes a remote host:name\n",
			      stderr);
			return 2;
		}
		return dch_remote_requests(argv[ni]);
	}
	/* the value rules (3.1), in the table's order */
	if (ref && !xr_ref_ok(ref))
	{
		fputs("dch: bad ref\n", stderr);
		return 2;
	}
	if (ref && !xr_ref_parse(ref, NULL, &sp))
	{
		fputs("dch: not a ref\n", stderr);
		return 2;
	}
	if (chapter && !xray_anchor_ok(chapter))
	{
		fputs("dch: bad anchor\n", stderr);
		return 2;
	}
	if (scope && !xray_scope_ok(scope))
	{
		fputs("dch: bad scope\n", stderr);
		return 2;
	}
	if (search && !xray_search_ok(search))
	{
		fputs("dch: bad search: 2 to 200 bytes, no control character\n",
		      stderr);
		return 2;
	}
	q.text = search ? search : ref;
	q.search = search != NULL;
	q.full = full;
	q.budget = 300;
	if (budget && (q.budget = xray_budget(budget)) < 0)
	{
		fputs("dch: bad budget\n", stderr);
		return 2;
	}
	if (json && name_routes_remote(n))
	{
		/* `--xray <name>` first, the rest as typed (4.1); the host
		** checks it all again (4.4) */
		char **av = calloc((size_t)argc + 2, sizeof *av);
		int ac = 2;

		if (!av)
			return 3;
		av[0] = "--xray";
		av[1] = argv[ni];
		for (k = 0; k < argc; k++)
			if (k != ni)
				av[ac++] = argv[k];
		rc = dch_remote_ctl(NULL, ac, av, NULL);
		free(av);
		return rc;
	}
	/* 3.4: project unless asked; a remote caller too (XR4) */
	q.scope = !scope || !strcmp(scope, "project") ? 1
	          : !strcmp(scope, "all") ? 2 : 0;
	/* the view (#175): `--xray <n>` alone */
	if (!json && (ref || chapter || clear || follow || search))
	{
		fputs("dch: bad usage: --json is required (the --xray view takes "
		      "no flag)\n", stderr);
		return 2;
	}
	/* a host:name's view too (#188): its queries run on the host; its o
	** (#194) attaches to the session as `dch -a` does */
	if (!json)
	{
		/* routed once, as it opens: the view, its queries and its o
		** stay on that session when a same-named one comes (#210) */
		int route = name_routes_remote(n) ? 1 : -1;

		xv_route = route;
		rc = xray_view(argv[ni]);
		xv_route = 0;
		if (rc != 0 || !xv_go[0])
			return rc;
		attach_route = route;
		snprintf(session_name, sizeof session_name, "%s", xv_go);
		attach_only = remote_ok = 1;
		return attach_with_switch(dch_exe, 1, 1, NULL, 0, 0);
	}
	/* SIGPIPE ignored: a failed socket write to the master exits 1 with
	** write_fail_line; stdout writes are checked apart (EPIPE is a
	** cancel, 3.4: exit 0, no done) */
	signal(SIGPIPE, SIG_IGN);
	write_fail_line = "dch: no such session\n";
	nr = history_load(&r);
	rc = xr_resolve(n, r, nr, clear, &t);
	if (rc == 0)
		rc = clear ? xr_clear(&t) : follow ? xr_follow(&t)
		     : chapter ? xr_chapter(&t, chapter)
		     : xr_json(&t, ref || search ? &q : NULL, 0);
	hrec_free(r, nr);
	return rc;
}

/* ---- main ----------------------------------------------------------- */

static int main_run(int argc, char **argv);

int
main(int argc, char **argv)
{
	int rc = main_run(argc, argv);

	if (rc != 0 && g_rhint[0])
		fprintf(stderr, "(%s is not a known host; --host %s to reach it)\n",
		        g_rhint, g_rhint);
	return rc;
}

static int
main_run(int argc, char **argv)
{
	int forced_name = 0;
	int force = 0;
	int kill_explicit = 0;
	enum { A_ATTACH, A_LIST, A_KILL, A_KILLALL, A_DETACH, A_LISTRAW,
	       A_RENAME, A_LISTJSON, A_LISTJSON2, A_SETALIAS,
	       A_SPAWN, A_SEND, A_RUN, A_KEYS, A_READ, A_WAIT, A_STATUS,
	       A_REPORT, A_META, A_RESTART, A_AGENT_LIST, A_AGENT_SEND,
	       A_RESPAWN, A_CMD, A_HISTORY, A_REVIVE, A_SAY, A_STOP,
	       A_PIN, A_UNPIN, A_FORGET, A_FOLLOW }
	    action = A_ATTACH;
	char alias_arg[600] = "";
	int opt_ansi = 0, opt_recent = -1, opt_timeout = 10000, opt_cursor = 0;
	int opt_all = 0, opt_cancel = 0, opt_agents = 0, opt_nomsg = 0;
	int opt_agent_json = 0, opt_hist_json = 0, opt_json = 0;
	int opt_remote = 0;
	int opt_lsfollow = 0;	/* --ls-json --follow (#103) */
	int say_at = 0, say_n = 0;
	char opt_as[512] = "";
	int opt_cols = 0, opt_rows = 0;
	char opt_match[DCH_WAIT_MAX + 1] = "";
	char opt_state[40] = "";

	progname = argv[0];

	/* Start with nothing blocked. A signal mask survives fork AND exec, so a
	** parent that blocks a signal hands us one we can never receive: the
	** handler installs fine and simply never runs. Prosper spawns the attach
	** client with forkpty()+execve() from a Swift runtime thread that has
	** SIGWINCH blocked, which silently disabled every window-size change —
	** rotating the phone into landscape left the session at the portrait
	** width, and the resize was pending, undeliverable, forever. Runs before
	** the --master-of dispatch so the session daemon is covered too. */
	{
		sigset_t none;
		sigemptyset(&none);
		sigprocmask(SIG_SETMASK, &none, NULL);
	}

	/* Resolve exe for the master re-exec. It must end up absolute: --restart
	** sends it to the master as the binary to land on, the master accepts
	** only an absolute path, and its own fallback re-execs with execv(),
	** which does no PATH search — a bare "dch" there resolves against the
	** master's cwd and fails (or runs whatever ./dch sits there).
	** A path (has a '/') is realpath()ed. A bare name like "dch" came from a
	** PATH lookup by the shell, so repeat that lookup the way execvp() does.
	** Never realpath() the bare name itself: that resolves RELATIVE TO CWD
	** and silently picks up an unrelated "./dch" (e.g. a `dch/` source dir),
	** making the master exec a directory ("failed to spawn"). An absolute
	** PATH hit is kept as is, so /opt/homebrew/bin/dch stays the symlink and
	** follows upgrades; a relative hit (empty or relative PATH entry) is
	** realpath()ed. No hit keeps the bare name for execvp() to search. */
	char *exe = argv[0];
	if (strchr(argv[0], '/'))
	{
		char *rp = realpath(argv[0], NULL);
		if (rp)
			exe = rp;
	}
	else
	{
		char hit[PATH_MAX];
		const char *path = getenv("PATH");

		if (path && path_lookup(argv[0], path, ".", hit, sizeof hit) == 0)
			exe = hit[0] == '/' ? strdup(hit) : realpath(hit, NULL);
		if (!exe)
			exe = argv[0];
	}

	/* Publish them for the master: MSG_RESTART re-execs this same image in
	** place, and after execvp() there is no second chance to look them up. */
	dch_exe = exe;
	dch_argv = argv;

	/* Internal sentinel: master-of <sock> -- <cmd...> */
	if (argc >= 3 && strcmp(argv[1], "--master-of") == 0)
		return run_master_of(argc - 2, argv + 2);

	/* The user config (#074): no session dir needed. */
	if (argc >= 2 && strcmp(argv[1], "--config") == 0)
		return config_main(argc - 2, argv + 2);
	if (argc >= 2 && strcmp(argv[1], "--shell-init") == 0)
		return shell_init_main(argc - 2, argv + 2);

	if (compute_sock_dir() < 0)
		return 1;

	/* The Prosper remote-frames server (#073), serve.c. */
	if (argc >= 2 && strcmp(argv[1], "serve") == 0)
		return serve_main(argc - 2, argv + 2);
	/* X-ray (#120): its own flag rules (spec 3.1), so first or never. */
	if (argc >= 2 && strcmp(argv[1], "--xray") == 0)
		return xray_main(argc - 2, argv + 2);

	/* A remote control verb (#114) is sent as typed, less --host, --hosts
	** and --cwd (cskip): argv as it was, since --say and its kin pack it. */
	/* static: main's every return would leak them otherwise */
	static char **oargv;
	static unsigned char *cskip;

	oargv = calloc((size_t)argc + 1, sizeof *oargv);
	cskip = calloc((size_t)argc + 1, 1);
	const char *opt_cwd = NULL;

	if (!oargv || !cskip)
		return 1;
	memcpy(oargv, argv, (size_t)argc * sizeof *oargv);

	int i = 1;
	while (i < argc)
	{
		const char *a = argv[i];
		if (strcmp(a, "--") == 0)
		{
			i++;
			break;
		}
		else if (strcmp(a, "-h") == 0 || strcmp(a, "--help") == 0)
		{
			usage();
			return 0;
		}
		else if (strcmp(a, "-V") == 0 || strcmp(a, "--version") == 0)
		{
			printf("dch %s\n", DCH_VERSION);
			return 0;
		}
		else if (strcmp(a, "--build") == 0)
		{
			/* --version stays exactly "dch X.Y.Z" (Prosper parses it);
			** lite = no terminal mirror: no --read, detection or
			** auto-allow */
#ifdef HAVE_LIBGHOSTTY_VT
			puts("full");
#else
			puts("lite");
#endif
			return 0;
		}
		else if (strcmp(a, "-l") == 0)
		{
			action = A_LIST;
			i++;
		}
		else if (strcmp(a, "-ls") == 0 || strcmp(a, "--ls") == 0)
		{
			action = A_LISTRAW;
			i++;
		}
		else if (strcmp(a, "-a") == 0)
		{
			/* attach, never create; host:name may be remote */
			if (++i >= argc || !argv[i][0])
			{
				fprintf(stderr, "dch: -a needs a name\n");
				return 1;
			}
			if (strlen(argv[i]) >= sizeof session_name)
			{
				fprintf(stderr, "dch: name is too long\n");
				return 1;
			}
			snprintf(session_name, sizeof(session_name), "%s",
			         argv[i++]);
			action = A_ATTACH;
			forced_name = force = attach_only = remote_ok = 1;
		}
		else if (strcmp(a, "--host") == 0 || strcmp(a, "--hosts") == 0)
		{
			if (++i >= argc || !argv[i][0])
			{
				fprintf(stderr, "dch: %s needs a value\n", a);
				return 1;
			}
			if (a[6] == 's' ? dch_remote_set_hosts(argv[i]) < 0
			    : strchr(argv[i], ':') != NULL)
			{
				if (a[6] != 's')
					fprintf(stderr, "dch: --host takes a "
					        "host's label, no ':'\n");
				return 1;
			}
			if (a[6] != 's')
				remote_host = argv[i];
			cskip[i - 1] = cskip[i] = 1;
			i++;
		}
		else if (strcmp(a, "--cwd") == 0)
		{
			/* #114: --spawn's start dir, here or on the host */
			if (++i >= argc || !argv[i][0])
			{
				fprintf(stderr, "dch: --cwd needs a directory\n");
				return 1;
			}
			opt_cwd = argv[i];
			cskip[i - 1] = cskip[i] = 1;
			i++;
		}
		else if (strcmp(a, "--scrollback") == 0)
		{
			/* #101: rows of history the attach replay sends first */
			if (++i >= argc ||
			    (dch_replay_sb = dch_scrollback_num(argv[i])) < 0)
			{
				fprintf(stderr, "dch: --scrollback wants a line "
				        "count, 0..%d\n", DCH_SCROLLBACK_MAX);
				return 1;
			}
			i++;
		}
		else if (strcmp(a, "--remote") == 0)
		{
			opt_remote = 1;
			i++;
		}
		else if (strcmp(a, "-lj") == 0)
		{
			action = A_LISTJSON;
			i++;
		}
		else if (strcmp(a, "-rl") == 0)
		{
			action = A_RENAME;
			i++;
		}
		else if (strcmp(a, "-m") == 0)
		{
			/* Non-interactive set/clear alias: -m <name> [alias]. */
			action = A_SETALIAS;
			i++;
			if (i < argc)
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				i++;
			}
			if (i < argc)
			{
				snprintf(alias_arg, sizeof(alias_arg), "%s",
				         argv[i]);
				i++;
			}
		}
		else if (strcmp(a, "-kl") == 0 || strcmp(a, "-lk") == 0)
		{
			action = A_KILLALL;
			i++;
		}
		else if (strcmp(a, "-k") == 0)
		{
			/* `-k -- <name>`: a name starting with '-' (#162) */
			int dd = i + 2 < argc && strcmp(argv[i + 1], "--") == 0 &&
			         argv[i + 2][0];

			action = A_KILL;
			i += 1 + dd;
			if (i < argc && (dd || argv[i][0] != '-'))
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				kill_explicit = 1;
				i++;
			}
		}
		else if (strcmp(a, "-d") == 0)
		{
			action = A_DETACH;
			i++;
			if (i < argc && argv[i][0] != '-')
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				kill_explicit = 1;
				i++;
			}
		}
		else if (strcmp(a, "-n") == 0)
		{
			i++;
			if (i >= argc)
			{
				fprintf(stderr, "dch: -n needs a name\n");
				return 1;
			}
			snprintf(session_name, sizeof(session_name),
			         "%s", argv[i]);
			forced_name = 1;
			i++;
		}
		else if (strcmp(a, "-f") == 0)
		{
			force = 1;
			i++;
		}
		else if (strcmp(a, "-E") == 0)
		{
			detach_char = -1;
			i++;
		}
		else if (strcmp(a, "-e") == 0)
		{
			i++;
			if (i >= argc)
			{
				fprintf(stderr, "dch: -e needs a char\n");
				return 1;
			}
			const char *c = argv[i];
			if (c[0] == '^' && c[1])
				detach_char = (c[1] == '?') ? 0177
				                            : (c[1] & 037);
			else
				detach_char = c[0];
			i++;
		}
		else if (strcmp(a, "--ls-json") == 0)
		{
			action = A_LISTJSON2;
			i++;
		}
		else if (strcmp(a, "--agent-list") == 0)
		{
			action = A_AGENT_LIST;
			i++;
			if (i < argc && strcmp(argv[i], "--json") == 0)
			{
				opt_agent_json = 1;
				i++;
			}
		}
		else if (strcmp(a, "--agent-send") == 0)
		{
			action = A_AGENT_SEND;
			i++;
			if (i >= argc || !argv[i][0])
			{
				fprintf(stderr, "dch: --agent-send needs a peer name and message\n");
				return 1;
			}
			if (strlen(argv[i]) >= sizeof session_name)
			{
				fprintf(stderr, "dch: peer name is too long\n");
				return 1;
			}
			snprintf(session_name, sizeof session_name, "%s", argv[i++]);
			break;
		}
		else if (strcmp(a, "--follow") == 0 && action == A_LISTJSON2 &&
		         !opt_lsfollow)
		{
			/* #103: the list as a stream; --follow <name> (below)
			** streams a screen */
			opt_lsfollow = 1;
			i++;
		}
		else if (strcmp(a, "--spawn") == 0 ||
		         strcmp(a, "--send") == 0 ||
		         strcmp(a, "--run") == 0 ||
		         strcmp(a, "--keys") == 0 ||
		         strcmp(a, "--read") == 0 ||
		         strcmp(a, "--follow") == 0 ||
		         strcmp(a, "--wait") == 0 ||
		         strcmp(a, "--status") == 0 ||
		         strcmp(a, "--report") == 0 ||
		         strcmp(a, "--meta") == 0 ||
		         strcmp(a, "--respawn") == 0 ||
		         strcmp(a, "--cmd") == 0)
		{
			/* Control verbs: all take a session name next. */
			action = strcmp(a, "--spawn") == 0 ? A_SPAWN
			       : strcmp(a, "--send") == 0  ? A_SEND
			       : strcmp(a, "--run") == 0   ? A_RUN
			       : strcmp(a, "--keys") == 0  ? A_KEYS
			       : strcmp(a, "--read") == 0  ? A_READ
			       : strcmp(a, "--follow") == 0 ? A_FOLLOW
			       : strcmp(a, "--wait") == 0  ? A_WAIT
			       : strcmp(a, "--status") == 0 ? A_STATUS
			       : strcmp(a, "--respawn") == 0 ? A_RESPAWN
			       : strcmp(a, "--cmd") == 0   ? A_CMD
			       : strcmp(a, "--meta") == 0  ? A_META
			                                    : A_REPORT;
			i++;
			if (i >= argc || argv[i][0] == '\0')
			{
				fprintf(stderr,
				        "dch: %s needs a session name\n", a);
				return 1;
			}
			if (action == A_SPAWN && (refuse_id_name(argv[i]) ||
			                          refuse_path_name(argv[i])))
				return 2;
			snprintf(session_name, sizeof(session_name),
			         "%s", argv[i]);
			i++;
		}
		else if (strcmp(a, "--restart") == 0)
		{
			/* Not folded into the control-verb block above: that
			** block always eats the next arg as a session name, and
			** `--restart --all` has none. */
			action = A_RESTART;
			i++;
			if (i < argc && strcmp(argv[i], "--all") == 0)
			{
				opt_all = 1;
				i++;
			}
			else if (i < argc && argv[i][0] != '\0' &&
			         argv[i][0] != '-')
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				i++;
			}
			else
			{
				fprintf(stderr, "dch: --restart needs a session "
				        "name or --all\n");
				return 1;
			}
		}
		else if (strcmp(a, "--say") == 0)
		{
			/* <name>... or --all, maybe --json, then a required
			** `--` and the text; the names are packed into
			** argv[say_at...] in place. */
			action = A_SAY;
			for (say_at = ++i; i < argc && strcmp(argv[i], "--") != 0; i++)
				if (strcmp(argv[i], "--all") == 0)
					opt_all = 1;
				else if (strcmp(argv[i], "--json") == 0)
					opt_json = 1;
				else if (argv[i][0] == '-' || argv[i][0] == '\0')
				{
					fprintf(stderr, "dch: --say: unexpected "
					        "%s\n", argv[i][0] ? argv[i] : "''");
					return 1;
				}
				else
					argv[say_at + say_n++] = argv[i];
			if (i == argc || opt_all == !!say_n)
			{
				fprintf(stderr, "dch: usage: dch --say <name>... "
				        "| --all [--json] -- <text...>\n");
				return 1;
			}
			i++;
			break;
		}
		else if (strcmp(a, "--stop") == 0)
		{
			/* <name>... or --all, maybe --json: the rest of the
			** line, names packed as --say packs them */
			int dd = 0;	/* after `--`, every word is a name (#162) */

			action = A_STOP;
			for (say_at = ++i; i < argc; i++)
				if (dd && argv[i][0])
					argv[say_at + say_n++] = argv[i];
				else if (!dd && strcmp(argv[i], "--") == 0)
					dd = 1;
				else if (strcmp(argv[i], "--all") == 0)
					opt_all = 1;
				else if (strcmp(argv[i], "--json") == 0)
					opt_json = 1;
				else if (argv[i][0] == '-' || argv[i][0] == '\0')
				{
					fprintf(stderr, "dch: --stop: unexpected "
					        "%s%s\n", argv[i][0] ? argv[i] : "''",
					        argv[i][0] ? " (a name starting with - "
					        "goes after --)" : "");
					return 1;
				}
				else
					argv[say_at + say_n++] = argv[i];
			if (opt_all == !!say_n)
			{
				fprintf(stderr, "dch: usage: dch --stop <name>... "
				        "| --all [--json]\n");
				return 1;
			}
			break;
		}
		else if (strcmp(a, "--pin") == 0 || strcmp(a, "--unpin") == 0 ||
		         strcmp(a, "--forget") == 0)
		{
			/* <name>... (--forget: or --all), maybe --json, -f:
			** the rest of the line, names packed as --stop packs
			** them; -f is gated below */
			int dd = 0;	/* after `--`, every word is a name (#162) */

			action = a[2] == 'p' ? A_PIN : a[2] == 'u' ? A_UNPIN
			         : A_FORGET;
			for (say_at = ++i; i < argc; i++)
				if (dd && argv[i][0])
					argv[say_at + say_n++] = argv[i];
				else if (!dd && strcmp(argv[i], "--") == 0)
					dd = 1;
				else if (strcmp(argv[i], "--all") == 0 &&
				    action == A_FORGET)
					opt_all = 1;
				else if (strcmp(argv[i], "--json") == 0)
					opt_json = 1;
				else if (strcmp(argv[i], "-f") == 0)
					force = 1;
				else if (argv[i][0] == '-' || argv[i][0] == '\0')
				{
					fprintf(stderr, "dch: %s: unexpected %s%s\n", a,
					        argv[i][0] ? argv[i] : "''",
					        argv[i][0] ? " (a name starting with - "
					        "goes after --)" : "");
					return 1;
				}
				else
					argv[say_at + say_n++] = argv[i];
			if (opt_all == !!say_n)
			{
				fprintf(stderr, "dch: usage: dch %s\n",
				        action == A_FORGET ? "--forget <name>... [-f] "
				        "[--json] | --forget --all [--json]"
				        : "--pin|--unpin <name>... [--json]");
				return 1;
			}
			break;
		}
		else if (strcmp(a, "--history") == 0)
		{
			action = A_HISTORY;
			i++;
			if (i < argc && strcmp(argv[i], "--json") == 0)
			{
				opt_hist_json = 1;
				i++;
			}
		}
		else if (strcmp(a, "--revive") == 0)
		{
			/* <name> or --all, like --restart */
			action = A_REVIVE;
			i++;
			if (i < argc && strcmp(argv[i], "--all") == 0)
			{
				opt_all = 1;
				i++;
			}
			else if (i < argc && argv[i][0] != '\0' &&
			         argv[i][0] != '-')
			{
				snprintf(session_name, sizeof(session_name),
				         "%s", argv[i]);
				i++;
			}
			else
			{
				fprintf(stderr, "dch: --revive needs a session "
				        "name or --all\n");
				return 1;
			}
		}
		else if (strcmp(a, "--as") == 0)
		{
			i++;
			if (i >= argc || argv[i][0] == '\0' || argv[i][0] == '-')
			{
				fprintf(stderr, "dch: --as needs a session name\n");
				return 1;
			}
			if (refuse_id_name(argv[i]) || refuse_path_name(argv[i]))
				return 2;
			snprintf(opt_as, sizeof(opt_as), "%s", argv[i]);
			i++;
		}
		else if (strcmp(a, "--cancel") == 0)
		{
			opt_cancel = 1;
			i++;
		}
		else if (strcmp(a, "--no-message") == 0)
		{
			opt_nomsg = 1;
			i++;
		}
		else if (strcmp(a, "--include-agents") == 0)
		{
			opt_agents = 1;
			i++;
		}
		else if (strcmp(a, "--json") == 0)
		{
			/* --restart, --revive (#047); --say/--stop take it inline */
			opt_json = 1;
			i++;
		}
		else if (strcmp(a, "--ansi") == 0)
		{
			opt_ansi = 1;
			i++;
		}
		else if (strcmp(a, "--cursor") == 0)
		{
			opt_cursor = 1;
			i++;
		}
		else if (strcmp(a, "--recent") == 0)
		{
			/* Optional line count; bare --recent = master default. */
			opt_recent = 0;
			i++;
			if (i < argc && argv[i][0] >= '0' && argv[i][0] <= '9')
			{
				opt_recent = atoi(argv[i]);
				if (opt_recent < 1 || opt_recent > 65535)
				{
					fprintf(stderr, "dch: --recent wants "
					        "1..65535 lines\n");
					return 1;
				}
				i++;
			}
		}
		else if (strcmp(a, "--match") == 0)
		{
			i++;
			if (i >= argc)
			{
				fprintf(stderr,
				        "dch: --match needs a string\n");
				return 1;
			}
			snprintf(opt_match, sizeof(opt_match), "%s", argv[i]);
			i++;
		}
		else if (strcmp(a, "--state") == 0)
		{
			i++;
			if (i >= argc || argv[i][0] == '\0')
			{
				fprintf(stderr,
				        "dch: --state needs a state name\n");
				return 1;
			}
			snprintf(opt_state, sizeof(opt_state), "%s", argv[i]);
			i++;
		}
		else if (strcmp(a, "--timeout") == 0)
		{
			i++;
			if (i >= argc || atoi(argv[i]) < 1)
			{
				fprintf(stderr,
				        "dch: --timeout needs milliseconds\n");
				return 1;
			}
			opt_timeout = atoi(argv[i]);
			i++;
		}
		else if (strcmp(a, "--size") == 0)
		{
			i++;
			if (i >= argc || sscanf(argv[i], "%dx%d",
			    &opt_cols, &opt_rows) != 2 ||
			    opt_cols < 1 || opt_cols > 4096 ||
			    opt_rows < 1 || opt_rows > 4096)
			{
				fprintf(stderr,
				        "dch: --size wants COLSxROWS\n");
				return 1;
			}
			i++;
		}
		else if (strcmp(a, "--env") == 0)
		{
			const char *eq;

			i++;
			if (i >= argc || !(eq = strchr(argv[i], '=')) ||
			    eq == argv[i])
			{
				fprintf(stderr,
				        "dch: --env wants KEY=VAL\n");
				return 1;
			}
			if (spawn_env_n >= SPAWN_ENV_MAX)
			{
				fprintf(stderr,
				        "dch: --env: too many (max %d)\n",
				        SPAWN_ENV_MAX);
				return 1;
			}
			spawn_env[spawn_env_n++] = argv[i];
			i++;
		}
		else if (strcmp(a, "--") == 0)
		{
			i++;
			break;
		}
		else if (strcmp(a, "--xray") == 0)
		{
			/* xray_main takes it as argv[1] only (spec 3.1) */
			fputs("dch: bad usage: --xray comes first: dch --xray <n> "
			      "--json\n", stderr);
			return 2;
		}
		else if (a[0] == '-' && a[1] != '\0')
		{
			fprintf(stderr, "dch: unknown flag %s\n", a);
			return 1;
		}
		else
			break;
	}

	/* Inner command after flags. argv is already NULL-terminated by libc. */
	int inner_argc = argc - i;
	char **inner_argv = argv + i;

	if (opt_cwd && action != A_SPAWN)
	{
		fprintf(stderr, "dch: --cwd only works with --spawn\n");
		return 1;
	}
	/* Remote control verbs (#114): --host names the host; else a name that
	** is host:name for a known host and no local session's or History
	** record's (#122: a local a:b name stays local, remote hosts off
	** included). */
	if (!attach_only &&
	    (action == A_READ || action == A_SEND || action == A_RUN ||
	     action == A_KEYS || action == A_FOLLOW || action == A_STATUS ||
	     action == A_KILL || action == A_SETALIAS || action == A_DETACH ||
	     action == A_SPAWN || action == A_HISTORY || action == A_REVIVE ||
	     action == A_PIN || action == A_UNPIN || action == A_FORGET ||
	     action == A_STOP || action == A_SAY))
	{
		int remote = remote_host != NULL, k, cn = 0;

		if (!remote && session_name[0])
			remote = name_routes_remote(session_name);
		for (k = 0; !remote && k < say_n; k++)
			remote = name_routes_remote(argv[say_at + k]);
		if (remote)
		{
			for (k = 1; k < argc; k++)
				if (!cskip[k])
					oargv[cn++] = oargv[k];
			oargv[cn] = NULL;
			return dch_remote_ctl(remote_host, cn, oargv, opt_cwd);
		}
	}
	if (remote_host && !attach_only)
	{
		fprintf(stderr, "dch: --host only works with -a <name> and the "
		        "control verbs\n");
		return 1;
	}
	if (opt_cwd)
	{
		struct stat cst;

		if (stat(opt_cwd, &cst) < 0 || !S_ISDIR(cst.st_mode) ||
		    chdir(opt_cwd) < 0)
		{
			fprintf(stderr, "dch: --cwd: no such directory: %s\n",
			        opt_cwd);
			return 1;
		}
	}
	if (attach_only && inner_argc > 0)
	{
		fprintf(stderr, "dch: -a attaches; it runs no command\n");
		return 1;
	}
	if (opt_lsfollow && (action != A_LISTJSON2 || inner_argc > 0))
	{
		fputs("dch: bad usage: dch --ls-json [--remote] --follow\n",
		      stderr);
		return 2;
	}
	if (opt_remote && action != A_LISTJSON && action != A_LISTJSON2 &&
	    action != A_LISTRAW && action != A_HISTORY)
	{
		fprintf(stderr, "dch: --remote only works with -ls, -lj, "
		        "--ls-json, --history\n");
		return 1;
	}
	/* --cancel only means something to --respawn; elsewhere it would be
	** silently ignored, so refuse it. */
	if (opt_cancel && action != A_RESPAWN)
	{
		fprintf(stderr, "dch: --cancel only works with --respawn\n");
		return 1;
	}
	if (opt_as[0] && (action != A_REVIVE || opt_all))
	{
		fprintf(stderr, "dch: --as only works with --revive <name>\n");
		return 1;
	}
	if (opt_nomsg && action != A_REVIVE)
	{
		fprintf(stderr, "dch: --no-message only works with --revive\n");
		return 1;
	}
	if (opt_agents && action != A_RESTART)
	{
		fprintf(stderr, "dch: --include-agents only works with "
		        "--restart\n");
		return 1;
	}
	if (opt_json && action != A_RESTART && action != A_REVIVE &&
	    action != A_SAY && action != A_STOP && action != A_PIN &&
	    action != A_UNPIN && action != A_FORGET)
	{
		fprintf(stderr, "dch: --json only works with --restart, --revive, "
		        "--say, --stop, --pin, --unpin, --forget\n");
		return 1;
	}
	/* clean (--forget --all) never removes a pin; a pin has no force */
	if (force && (action == A_PIN || action == A_UNPIN ||
	              (action == A_FORGET && opt_all)))
	{
		fprintf(stderr, "dch: -f does not work with --pin, --unpin or "
		        "--forget --all\n");
		return 1;
	}

	/* Auto-name only where the action needs a specific session and the
	** caller didn't already supply one. */
	if (session_name[0] == '\0')
	{
		if (action == A_ATTACH ||
		    (action == A_KILL && kill_explicit) ||
		    (action == A_DETACH && kill_explicit))
		{
			auto_name(session_name, sizeof(session_name));
		}
	}

	switch (action)
	{
	case A_AGENT_LIST:
		if (inner_argc)
		{
			fprintf(stderr, "dch: --agent-list takes only optional --json\n");
			return 1;
		}
		return dch_bridge_agent_list(opt_agent_json);
	case A_AGENT_SEND:
		if (!inner_argc)
		{
			fprintf(stderr, "dch: --agent-send needs a non-empty message\n");
			return 1;
		}
		return dch_bridge_agent_send(session_name, inner_argc, inner_argv);
	case A_RESTART:
	{
		struct slist sl = {0};
		int k, rc, bad = 0, did = 0, old = 0;

		if (!opt_all)
		{
			rc = do_restart_verb(session_name,
			                     session_version(session_name)
			                         == NULL, 0);
			master_print(session_name, rc, 0, opt_json);
			if (rc != 0)
				return 1;
			fflush(stdout);
			return opt_agents && restart_agents(force, session_name,
			                                    opt_json) ? 1 : 0;
		}

		/* --all restarts only what is actually stale: a restart is
		** cheap but not free (the app repaints, scrollback in the
		** mirror is dropped), so don't spend it on sessions already
		** serving this binary. -f overrides. */
		list_sessions(&sl);
		for (k = 0; k < sl.n; k++)
		{
			const char *v = session_version(sl.v[k]);

			if (!force && v && strcmp(v, DCH_VERSION) == 0)
			{
				master_print(sl.v[k], -1, 1, opt_json);
				continue;
			}
			/* No sidecar at all: age unknown — a master that predates
			** live restart looks identical to a current one whose
			** sidecar tmp_cleaner removed. Either way MSG_RESTART risks
			** the full idle timeout before saying nothing, so skip the
			** wait — but only once we know something is actually there;
			** a stale socket file has no sidecar either. */
			rc = do_restart_verb(sl.v[k], v == NULL, 1);
			if (rc == 2)
				continue;	/* dead socket; nothing to do */
			master_print(sl.v[k], rc, 1, opt_json);
			if (rc == 3)
			{
				old++;	/* already explained on stderr */
				continue;
			}
			did++;
			if (rc != 0)
				bad++;
		}
		slist_free(&sl);
		if (!did && !old && !opt_json)
			printf("all sessions already on dch %s\n", DCH_VERSION);
		else if (!did && !opt_json)
			printf("nothing to restart: %d session%s too old\n",
			       old, old == 1 ? " is" : "s are");
		fflush(stdout);
		/* Then the agents, on masters that now speak --respawn. */
		if (opt_agents && restart_agents(force, NULL, opt_json))
			bad++;
		return bad ? 1 : 0;
	}
	case A_LISTRAW:
	{
		struct slist sl = {0};
		list_sessions(&sl);
		load_aliases(&sl);
		load_harness_names(&sl);
		load_codex_names(&sl);
		slist_sort(&sl);
		int k;
		for (k = 0; k < sl.n; k++)
		{
			char row[1300];
			session_display_name(&sl, k, row, sizeof(row));
			puts(row);
		}
		slist_free(&sl);
		/* with --remote, then the other hosts' (#087), escaped: a
		** peer wrote them. Never without: -ls is a local check that
		** scripts (openlid's keep-awake) run often, no network. */
		if (opt_remote)
		{
			struct dch_rrow *rr;
			int n = dch_remote_rows(&rr, 1);
			char en[1100], ea[1100];

			for (k = 0; k < n; k++)
			{
				dch_remote_esc(en, sizeof en, rr[k].name);
				dch_remote_esc(ea, sizeof ea, rr[k].alias);
				if (ea[0])
					printf("%s (%s:%s)%s\n", ea, rr[k].host, en,
					       rr[k].stale ? " [stale]" : "");
				else
					printf("%s:%s%s\n", rr[k].host, en,
					       rr[k].stale ? " [stale]" : "");
			}
			free(rr);
		}
		return 0;
	}
	case A_LISTJSON2:
	{
		/* Real JSON for agents: names, aliases and harness names
		** quoted by json_quote, so bytes that are not UTF-8 read as
		** U+FFFD instead of breaking the parse. */
		struct slist sl = {0};
		struct dch_rrow *frr = NULL;
		int fn = -1;

		/* --follow (#103): each array is printed here, in a child of
		** the stream's, frr its remote rows */
		if (opt_lsfollow &&
		    (fn = dch_remote_follow(opt_remote, &frr)) < 0)
			return fn == -1 ? 0 : 1;
		list_sessions(&sl);
		load_aliases(&sl);
		load_harness_names(&sl);
		load_codex_names(&sl);
		int k;
		/* The Claude under each session's child (the root --stop
		** joins from, by MSG_CMD), read live: every sessions dir once
		** for all rows, procStart verified (#079). On Linux the rows
		** share one /proc scan (proc_tree_keep). */
		struct crec_rows cr = { calloc((size_t)sl.n + 1, sizeof *cr.v),
		                        sl.n };
		/* pty size and attached clients (#099), from the same MSG_CMD
		** round trip; rows/cols/clients, or -1: the master can't say */
		long (*sz)[3] = cr.v ? calloc((size_t)sl.n + 1, sizeof *sz)
		                     : NULL;
		char cdirs[16][1100], sp[1100];
		int nd = 0;

		proc_tree_keep = 1;
		for (k = 0; cr.v && k < sl.n; k++)
		{
			struct cmd_info ci;

			cr.v[k].verify = 1;
			if (sz)
				sz[k][0] = -1;
			session_sock_path(sl.v[k], sp, sizeof sp);
			if (cmd_query_f(sp, &ci, DCH_CMD_F_STAT) != 0)
				continue;
			if (sz && ci.stat)
			{
				sz[k][0] = ci.rows;
				sz[k][1] = ci.cols;
				sz[k][2] = ci.clients;
			}
			if (ci.pid > 0 && strcmp(ci.state, "held") != 0)
			{
				cr.v[k].root = ci.pid;
				nd = claude_dirs(ci.pid, cdirs, nd, 16);
			}
			cmd_info_free(&ci);
		}
		proc_tree_keep = 0;
		for (k = 0; cr.v && k < nd; k++)
			(void)visit_records_in(cdirs[k], crec_visit_rows, &cr);
		/* "cwd" (#115): the session's directory, from its own live
		** history record */
		char hdir[1024];
		int hfd = dch_history_dir(hdir, sizeof hdir, sock_dir, 0) < 0 ? -1
		          : open(hdir, O_RDONLY | O_DIRECTORY | O_NOFOLLOW |
		                 O_CLOEXEC);
		putchar('[');
		for (k = 0; k < sl.n; k++)
		{
			printf("%s{\"name\":", k ? "," : "");
			put_json_str(sl.v[k]);
			fputs(",\"alias\":", stdout);
			put_json_str(sl.alias[k] ? sl.alias[k] : "");
			fputs(",\"harness\":", stdout);
			put_json_str(sl.harness[k] ? sl.harness[k] : "");
			/* version is "" when the master predates the sidecar —
			** i.e. definitely older than us, which is exactly what
			** a caller deciding whether to --restart needs. */
			long mep;
			const char *meta = session_meta(sl.v[k], &mep);
			printf(",\"activity_epoch\":%ld,\"state\":\"%s\""
			       ",\"version\":\"%s\",\"agents\":%d"
			       ",\"meta\":%s,\"meta_epoch\":%ld",
			       activity_epoch(sl.v[k]),
			       session_state(sl.v[k]),
			       version_or_empty(sl.v[k]),
			       detect_agents(sl.v[k]),
			       meta ? meta : "null", mep);
			const char *aa = session_allowed(sl.v[k], &mep);
			fputs(",\"auto_allowed\":", stdout);
			if (aa)
			{
				printf("{\"at\":%ld,\"title\":", mep);
				put_json_str(aa);
				putchar('}');
			}
			else
				fputs("null", stdout);
			/* Exactly one Claude (others below it count as it,
			** #253), or no answer: a wrong transcript is worse
			** than none. */
			const struct crec *c = cr.v && cr.v[k].n == 1 &&
			                       uuid_shaped(cr.v[k].best.sid)
			                       ? &cr.v[k].best : NULL;
			char tp[2000];
			fputs(",\"claude_session_id\":", stdout);
			if (c)
				put_json_str(c->sid);
			else
				fputs("null", stdout);
			fputs(",\"transcript_path\":", stdout);
			if (c && claude_transcript(c, tp, sizeof tp) == 0)
				put_json_str(tp);
			else
				fputs("null", stdout);
			/* left out with no Claude, or when it cannot be read */
			int fg = c ? proc_fg(cr.v[k].root, c->pid) : -1;

			if (fg >= 0)
				printf(",\"claude_foreground\":%s",
				       fg ? "true" : "false");
			/* omitted, not 0, when the master can't answer */
			if (sz && sz[k][0] >= 0)
				printf(",\"cols\":%ld,\"rows\":%ld,\"clients\":%ld",
				       sz[k][1], sz[k][0], sz[k][2]);
			/* last, so every key before keeps its place; null with
			** no record (DCH_NO_HISTORY) or an ended one */
			struct hrec h = {0};
			char hf[300];

			fputs(",\"cwd\":", stdout);
			if (snprintf(hf, sizeof hf, "%s.json", canon_name(sl.v[k],
			             tp, sizeof tp)) < (int)sizeof hf &&
			    hrec_load(hfd, hf, &h) == 0 && !h.end[0] && h.cwd[0])
				put_json_str(h.cwd);
			else
				fputs("null", stdout);
			free(h.body);
			free(h.argv);
			free(h.launch);
			putchar('}');
		}
		if (hfd >= 0)
			close(hfd);
		if (opt_remote)
		{
			/* the other hosts' (#087): name and alias as the peer
			** sent them, JSON-quoted; "host" tells them apart */
			struct dch_rrow *rr = frr;
			int n = fn >= 0 ? fn : dch_remote_rows(&rr, 1), r;

			for (r = 0; r < n; r++)
			{
				printf("%s{\"name\":", k + r ? "," : "");
				put_json_str(rr[r].name);
				fputs(",\"alias\":", stdout);
				put_json_str(rr[r].alias);
				fputs(",\"host\":", stdout);
				put_json_str(rr[r].host);
				printf(",\"state\":\"%s\",\"stale\":%s,\"cwd\":",
				       rr[r].state, rr[r].stale ? "true" : "false");
				if (rr[r].cwd[0])
					put_json_str(rr[r].cwd);
				else
					fputs("null", stdout);
				putchar('}');
			}
			free(rr);
		}
		puts("]");
		free(cr.v);
		free(sz);
		slist_free(&sl);
		return 0;
	}
	case A_LISTJSON:
	{
		/* Machine list: one "name\talias\tactivity_epoch" per line (alias
		** empty when none, epoch 0 when no output yet). Tab-separated, not
		** JSON — no escaping needed since names can't contain tabs and
		** set-alias strips them. */
		struct slist sl = {0};
		list_sessions(&sl);
		load_aliases(&sl);
		int k;
		for (k = 0; k < sl.n; k++)
			printf("%s\t%s\t%ld\n", sl.v[k],
			       sl.alias[k] ? sl.alias[k] : "",
			       activity_epoch(sl.v[k]));
		slist_free(&sl);
		if (opt_remote)
		{
			/* host:name, escaped: no tab or newline gets through */
			struct dch_rrow *rr;
			int n = dch_remote_rows(&rr, 1);
			char en[1100], ea[1100];

			for (k = 0; k < n; k++)
			{
				dch_remote_esc(en, sizeof en, rr[k].name);
				dch_remote_esc(ea, sizeof ea, rr[k].alias);
				printf("%s:%s\t%s\t0\n", rr[k].host, en, ea);
			}
			free(rr);
		}
		return 0;
	}
	case A_SETALIAS:
	{
		if (session_name[0] == '\0')
		{
			fprintf(stderr, "dch: -m needs a session name\n");
			return 1;
		}
		/* Only a live session: an alias written for a missing one
		** would be inherited by a later session of that name. Same
		** test and wording as -k. */
		char sp[1100];
		struct stat st;
		session_sock_path(session_name, sp, sizeof(sp));
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		/* Keep aliases single-line and tab-free so -lj stays parseable. */
		for (char *p = alias_arg; *p; p++)
			if (*p == '\t' || *p == '\n' || *p == '\r')
				*p = ' ';
		switch (write_alias(session_name, alias_arg))
		{
		case 0:
			return 0;
		case -2:	/* the shape trimmed, as the listing shows it */
			fputs("dch: name looks like a session_id\n", stderr);
			return 2;
		default:
			return 1;
		}
	}
	case A_LIST:
	{
		char chosen[sizeof session_name];
		int rc;

		pk_remote = 1;
		rc = pick_session("", chosen,
		                  sizeof(chosen), 1);
		if (rc == 0)
		{
			remote_ok = 1;
			snprintf(session_name, sizeof(session_name),
			         "%s", chosen);
			return attach_with_switch(exe, 1, 1, inner_argv,
			                          inner_argc, 1);
		}
		return 0;
	}
	case A_KILL:
	{
		if (!kill_explicit && !forced_name)
		{
			char chosen[sizeof session_name];
			int rc = pick_session("kill session:", chosen,
			                      sizeof(chosen), 0);
			if (rc != 0)
				return 0;
			snprintf(session_name, sizeof(session_name),
			         "%s", chosen);
		}
		return kill_session(session_name);
	}
	case A_RENAME:
		return rename_sessions_interactive();
	case A_KILLALL:
		return kill_all();
	case A_DETACH:
	{
		if (!kill_explicit && !forced_name)
		{
			char chosen[sizeof session_name];
			int rc = pick_session("detach session:", chosen,
			                      sizeof(chosen), 0);
			if (rc != 0)
				return 0;
			snprintf(session_name, sizeof(session_name),
			         "%s", chosen);
		}
		char sp[1100];
		session_sock_path(session_name, sp, sizeof(sp));
		struct stat st;
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		int n = detach_all_clients_on(sp);
		printf("detached %d client%s of: %s\n",
		       n, n == 1 ? "" : "s", session_name);
		return 0;
	}
	case A_READ:
		if (opt_cursor && opt_recent >= 0)
		{
			fprintf(stderr, "dch: --cursor needs the visible screen; "
			        "not valid with --recent\n");
			return 1;
		}
		return do_read_verb(session_name, opt_ansi, opt_recent,
		                    opt_cursor);
	case A_FOLLOW:
		return do_follow_verb(session_name);
	case A_STATUS:
	{
		char sp[1100];
		struct stat st;
		session_sock_path(session_name, sp, sizeof(sp));
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		puts(session_state(session_name));
		return 0;
	}
	case A_REPORT:
	{
		char sp[1100], pp[1300];
		struct stat st;
		const char *tok = inner_argc == 1 ? inner_argv[0] : "";
		/* Closed set, rejected at the boundary (herdr-style): an
		** unknown token writes nothing and fails loudly here, so
		** consumers of --status never see a surprise state. */
		if (!valid_state_token(tok) && strcmp(tok, "clear") != 0)
		{
			fprintf(stderr, "dch: --report needs one state: "
			        "working|idle|blocked|done, or `clear`\n");
			return 1;
		}
		session_sock_path(session_name, sp, sizeof(sp));
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		if (state_file_path(session_name, pp, sizeof(pp)) != 0)
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		if (strcmp(tok, "clear") == 0)
		{
			unlink(pp); /* back to the output heuristic */
			return 0;
		}
		/* Atomic replace (write temp + rename): a concurrent --status
		** never reads a torn file; last writer wins, which is the
		** right semantic for ordered harness hooks. */
		char tp[1320];
		size_t tl = strlen(tok);
		snprintf(tp, sizeof(tp), "%s.%d.tmp", pp, (int)getpid());
		int fd = open(tp, O_WRONLY | O_CREAT | O_TRUNC, 0600);
		int wrote = fd >= 0 && write(fd, tok, tl) == (ssize_t)tl;
		if (fd >= 0 && close(fd) != 0)
			wrote = 0;
		if (!wrote || rename(tp, pp) != 0)
		{
			if (fd >= 0)
				unlink(tp);
			fprintf(stderr, "dch: cannot write state for %s\n",
			        session_name);
			return 1;
		}
		return 0;
	}
	case A_META:
	{
		/* Free-form JSON object a program in the session publishes about
		** itself (open PRs, ...); --ls-json passes it through. */
		char sp[1100], mp[1300], tp[1320];
		struct stat st;
		const char *js = inner_argc == 1 ? inner_argv[0] : "";
		size_t n;
		while (*js == ' ')
			js++;
		n = strlen(js);
		while (n && js[n - 1] == ' ')
			n--;
		if (strcmp(js, "clear") != 0 &&
		    (!meta_bytes_ok(js, n) || !dch_json_is_object(js)))
		{
			fprintf(stderr, "dch: --meta needs one single-line JSON "
			        "object (at most %d bytes, 1024 tokens), or "
			        "`clear`\n", DCH_META_MAX);
			return 1;
		}
		session_sock_path(session_name, sp, sizeof(sp));
		if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode) ||
		    meta_file_path(session_name, mp, sizeof(mp)) != 0)
		{
			fprintf(stderr, "no session: %s\n", session_name);
			return 1;
		}
		if (strcmp(js, "clear") == 0)
		{
			unlink(mp);
			return 0;
		}
		/* Unchanged content is a no-op, so meta_epoch (the mtime) stays
		** the time the content last changed and writers may re-push
		** freely. */
		{
			long ep;
			const char *cur = session_meta(session_name, &ep);
			if (cur && strlen(cur) == n && memcmp(cur, js, n) == 0)
				return 0;
		}
		/* Atomic replace, as for --report: readers never see a torn
		** object; last writer wins. */
		snprintf(tp, sizeof(tp), "%s.%d.tmp", mp, (int)getpid());
		int fd = open(tp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
		int wrote = fd >= 0 && write(fd, js, n) == (ssize_t)n;
		if (fd >= 0 && close(fd) != 0)
			wrote = 0;
		if (!wrote || rename(tp, mp) != 0)
		{
			if (fd >= 0)
				unlink(tp);
			fprintf(stderr, "dch: cannot write meta for %s\n",
			        session_name);
			return 1;
		}
		return 0;
	}
	case A_WAIT:
		if (opt_state[0] != '\0')
		{
			/* State wait: poll the sidecar-backed state every 100ms
			** (herdr's cadence). --state takes a comma list and
			** matches any of it, e.g. --state idle,blocked,done =
			** "the turn is over". Works in both variants (no mirror
			** needed). The session must exist on every poll — a
			** killed session ends the wait with "no session"
			** instead of burning the timeout. */
			char sp[1100];
			struct stat st;
			struct timeval w0, wn;
			int ntok = 0, t;
			char *want[8], *tok, *rest;
			for (tok = strtok_r(opt_state, ",", &rest);
			     tok && ntok < 8;
			     tok = strtok_r(NULL, ",", &rest))
			{
				/* "active" is the v1.3 name for the busy
				** state; map it in the compare, not just the
				** validator, or waiters on it never fire. */
				if (strcmp(tok, "active") == 0)
					tok = (char *)"working";
				else if (!valid_state_token(tok))
				{
					fprintf(stderr, "dch: --state wants "
					        "active|working|idle|blocked|"
					        "done (comma list ok)\n");
					return 1;
				}
				want[ntok++] = tok;
			}
			if (ntok == 0)
			{
				fprintf(stderr,
				        "dch: --state needs a state name\n");
				return 1;
			}
			gettimeofday(&w0, NULL);
			for (;;)
			{
				session_sock_path(session_name, sp,
				                  sizeof(sp));
				if (stat(sp, &st) < 0 || !S_ISSOCK(st.st_mode))
				{
					fprintf(stderr, "no session: %s\n",
					        session_name);
					return 1;
				}
				const char *cur =
				    session_state(session_name);
				for (t = 0; t < ntok; t++)
					if (strcmp(cur, want[t]) == 0)
					{
						puts(cur);
						return 0;
					}
				/* wall-clock, not loop-count: detection
				** adds up to DET_DEADLINE_MS per poll and a
				** fixed increment would blow the budget */
				gettimeofday(&wn, NULL);
				if ((wn.tv_sec - w0.tv_sec) * 1000 +
				    (wn.tv_usec - w0.tv_usec) / 1000 >=
				    opt_timeout)
					return 2;
				poll(NULL, 0, 100);
			}
		}
		if (opt_match[0] == '\0')
		{
			fprintf(stderr,
			        "dch: --wait needs --match or --state\n");
			return 1;
		}
		return do_wait_verb(session_name, opt_match, opt_timeout);
	case A_KEYS:
		return do_keys_verb(session_name, inner_argv, inner_argc);
	case A_RESPAWN:
		if (opt_cancel && inner_argc)
		{
			fprintf(stderr, "dch: --respawn --cancel takes no "
			        "command\n");
			return 1;
		}
		return do_respawn_verb(session_name, inner_argv, inner_argc,
		                       opt_cancel);
	case A_CMD:
		return do_cmd_verb(session_name);
	case A_SEND:
		return do_send_verb(session_name, inner_argv, inner_argc, 0);
	case A_RUN:
		return do_run_verb(session_name, inner_argv, inner_argc);
	case A_HISTORY:
		return do_history_verb(opt_hist_json, opt_remote);
	case A_SAY:
		return do_say_verb(opt_all ? NULL : argv + say_at, say_n,
		                   inner_argv, inner_argc, opt_json);
	case A_STOP:
		return do_stop_verb(opt_all ? NULL : argv + say_at, say_n,
		                    opt_json);
	case A_PIN:
	case A_UNPIN:
	case A_FORGET:
		return do_pin_verb(opt_all ? NULL : argv + say_at, say_n,
		                   action == A_PIN ? HV_PIN : action == A_UNPIN
		                   ? HV_UNPIN : HV_FORGET, force, opt_json);
	case A_REVIVE:
	case A_SPAWN:
	{
		if (opt_cols == 0)
		{
			/* Headless spawn: caller's env is the best size hint. */
			const char *c = getenv("COLUMNS"), *l = getenv("LINES");

			opt_cols = c ? atoi(c) : 0;
			opt_rows = l ? atoi(l) : 0;
			if (opt_cols < 1 || opt_cols > 4096 ||
			    opt_rows < 1 || opt_rows > 4096)
			{
				opt_cols = 80;
				opt_rows = 24;
			}
		}
		if (action == A_REVIVE)
			return do_revive_verb(exe, session_name,
			                      opt_as[0] ? opt_as : NULL, opt_all,
			                      opt_cols, opt_rows, opt_json, opt_nomsg);
		return do_spawn_verb(exe, session_name, opt_cols, opt_rows,
		                     inner_argv, inner_argc, 0);
	}
	case A_ATTACH:
		return attach_with_switch(exe, forced_name, force,
		                          inner_argv, inner_argc, 0);
	}
	return 0;
}
