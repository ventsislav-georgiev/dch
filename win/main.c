/*
** dch.exe (#077): the dch client for remote hosts on Windows. It lists the
** sessions of the user's other machines and attaches to one; it hosts none,
** so every row and every attach is remote, and -ls implies --remote. The
** hosts, discovery, the probes and the attach's checks are remote.c's, as
** on POSIX. The picker (dch -l, and dch alone) is #090.
*/
#include "win/win.h"
#include "rframe.h"

#include <shellapi.h>

static void
usage(FILE *f)
{
	fprintf(f,
	    "dch " DCH_VERSION " - the Windows client: sessions on your other "
	    "hosts\n"
	    "Usage:\n"
	    "  dch -ls, --ls    list the remote sessions, host:name (--remote is\n"
	    "                   implied: this machine hosts none)\n"
	    "  dch -lj          host:name<TAB>alias<TAB>0 per line\n"
	    "  dch --ls-json    a JSON array, each row with a \"host\"\n"
	    "  dch -a host:name attach to a session on a remote host\n"
	    "  dch --host <h> -a <name>  attach to <name> on host h\n"
	    "  dch --hosts <h1,h2,...>   the remote hosts, [label=]host[:port],\n"
	    "                   instead of your online tailnet peers\n"
	    "  dch -h, --help   this help\n"
	    "  dch -V, --version\n"
	    "Ctrl-\\ detaches; the session lives on.\n"
	    "Config: %%DCH_CONFIG%%, else %%APPDATA%%\\dch\\config: remote_hosts =\n"
	    "  [label=]host[:port],... (empty: your tailnet peers; off: none).\n"
	    "Env: DCH_NO_REMOTE=1 lists no remote hosts (but --hosts);\n"
	    "  DCH_REMOTE_BOX_MS the listing's time box (default 1000).\n");
}

/* s as a JSON string on stdout. */
static void
put_json_str(const char *s)
{
	size_t cap = strlen(s) * 6 + 3;
	char *q = malloc(cap);

	fputs(q && dch_json_quote(q, cap, s) != (size_t)-1 ? q : "\"\"", stdout);
	free(q);
}

enum { A_NONE, A_LS, A_LJ, A_JSON, A_ATTACH };

/* The remote rows as -ls, -lj or --ls-json print them (dch.c's), every
** name a peer sent escaped, or JSON-quoted. */
static int
list(int action)
{
	struct dch_rrow *rr;
	int n = dch_remote_rows(&rr, 1), k;
	char en[1100], ea[1100];

	if (action == A_JSON)
		putchar('[');
	for (k = 0; k < n; k++)
	{
		dch_remote_esc(en, sizeof en, rr[k].name);
		dch_remote_esc(ea, sizeof ea, rr[k].alias);
		if (action == A_LJ)
			printf("%s:%s\t%s\t0\n", rr[k].host, en, ea);
		else if (action == A_LS && ea[0])
			printf("%s (%s:%s)%s\n", ea, rr[k].host, en,
			       rr[k].stale ? " [stale]" : "");
		else if (action == A_LS)
			printf("%s:%s%s\n", rr[k].host, en,
			       rr[k].stale ? " [stale]" : "");
		else
		{
			printf("%s{\"name\":", k ? "," : "");
			put_json_str(rr[k].name);
			fputs(",\"alias\":", stdout);
			put_json_str(rr[k].alias);
			fputs(",\"host\":", stdout);
			put_json_str(rr[k].host);
			printf(",\"state\":\"%s\",\"stale\":%s}", rr[k].state,
			       rr[k].stale ? "true" : "false");
		}
	}
	if (action == A_JSON)
		puts("]");
	free(rr);
	return fflush(stdout) ? 1 : 0;
}

static UINT out_cp;

static void
cp_back(void)
{
	SetConsoleOutputCP(out_cp);
}

static char **u_argv;	/* the process's, kept for its life */

/* The command line as UTF-8 (a session name goes on the wire as that),
** NULL-terminated; *argc its count. NULL when it cannot be had. */
static char **
utf8_argv(int *argc)
{
	LPWSTR *w = CommandLineToArgvW(GetCommandLineW(), argc);
	int i, k = 0;

	if (!w || !(u_argv = calloc((size_t)*argc + 1, sizeof *u_argv)))
	{
		LocalFree(w);
		return NULL;
	}
	for (i = 0; i < *argc; i++)
		if ((k = WideCharToMultiByte(CP_UTF8, 0, w[i], -1, NULL, 0, NULL,
		                             NULL)) <= 0 ||
		    !(u_argv[i] = malloc((size_t)k)) ||
		    !WideCharToMultiByte(CP_UTF8, 0, w[i], -1, u_argv[i], k, NULL,
		                         NULL))
			break;
	LocalFree(w);
	return i == *argc ? u_argv : NULL;
}

int
main(int argc, char **argv)
{
	const char *aname = NULL, *host = NULL;
	char hbuf[512];
	int action = A_NONE, i;
	WSADATA wd;

	if (!(argv = utf8_argv(&argc)))
	{
		fprintf(stderr, "dch: cannot read the command line\n");
		return 1;
	}
	/* LF, not CRLF: -lj and --ls-json are for scripts, as on POSIX */
	_setmode(_fileno(stdout), _O_BINARY);
	_setmode(_fileno(stderr), _O_BINARY);
	if ((out_cp = GetConsoleOutputCP()) && out_cp != CP_UTF8 &&
	    SetConsoleOutputCP(CP_UTF8))
		atexit(cp_back);
	for (i = 1; i < argc; i++)
	{
		const char *a = argv[i];

		if (!strcmp(a, "-h") || !strcmp(a, "--help"))
		{
			usage(stdout);
			return 0;
		}
		if (!strcmp(a, "-V") || !strcmp(a, "--version"))
		{
			printf("dch %s\n", DCH_VERSION);
			return 0;
		}
		if (!strcmp(a, "-ls") || !strcmp(a, "--ls"))
			action = A_LS;
		else if (!strcmp(a, "-lj"))
			action = A_LJ;
		else if (!strcmp(a, "--ls-json"))
			action = A_JSON;
		else if (!strcmp(a, "--remote"))
			;	/* implied */
		else if (!strcmp(a, "-a") || !strcmp(a, "--host") ||
		         !strcmp(a, "--hosts"))
		{
			if (++i >= argc || !argv[i][0])
			{
				fprintf(stderr, "dch: %s needs a %s\n", a,
				        a[1] == 'a' ? "name" : "value");
				return 1;
			}
			if (a[1] == 'a')
			{
				aname = argv[i];
				action = A_ATTACH;
			}
			else if (a[6] == 's')
			{
				if (dch_remote_set_hosts(argv[i]) < 0)
					return 1;
			}
			else if (strchr(argv[i], ':'))
			{
				fprintf(stderr, "dch: --host takes a host's "
				        "label, no ':'\n");
				return 1;
			}
			else
				host = argv[i];
		}
		else if (a[0] == '-')
		{
			fprintf(stderr, "dch: unknown flag %s\n", a);
			return 1;
		}
		else
		{
			fprintf(stderr, "dch: %s: on Windows dch runs no command "
			        "(dch -h)\n", a);
			return 1;
		}
	}
	if (host && action != A_ATTACH)
	{
		fprintf(stderr, "dch: --host only works with -a <name>\n");
		return 1;
	}
	if (action == A_NONE)
	{
		fprintf(stderr, "dch: the session picker is not on Windows yet; "
		        "dch -ls lists, dch -a host:name attaches\n\n");
		usage(stderr);
		return 1;
	}
	if (WSAStartup(MAKEWORD(2, 2), &wd))
	{
		fprintf(stderr, "dch: Winsock would not start\n");
		return 1;
	}
	if (action != A_ATTACH)
		return list(action);
	/* host:name, or --host h with a name; no local names here */
	if (!host)
	{
		const char *c = strchr(aname, ':');

		if (!c || c == aname || (size_t)(c - aname) >= sizeof hbuf)
		{
			fprintf(stderr, "dch: -a takes host:name on Windows (it "
			        "hosts no sessions)\n");
			return 1;
		}
		memcpy(hbuf, aname, (size_t)(c - aname));
		hbuf[c - aname] = '\0';
		host = hbuf;
		aname = c + 1;
	}
	if (strlen(aname) > 1000)
	{
		fprintf(stderr, "dch: name is too long\n");
		return 1;
	}
	return dch_remote_attach(host, aname);
}
