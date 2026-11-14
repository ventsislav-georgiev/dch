/* serve_test's fake tailscale CLI, compiled: no shell start per call. dch
** serve gives each call 3 s (TS_MS); under load a /bin/sh per whois went past
** that, a whois_failed refusal cached 30 s (#246, #277). Same contract as the
** shell fake FAKE_TS in serve_test.py: appends "<verb> <loopback var>" to
** calls next to itself, prints status.json, whois.json or ip.txt from there
** for status, whois or ip, exits 1 for anything else or a missing file. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int
main(int argc, char **argv)
{
	char d[4096], p[4200], buf[8192];
	const char *f, *lb = getenv("DCH_SERVE_TEST_LOOPBACK"), *sl;
	FILE *in, *log;
	size_t n;

	sl = strrchr(argv[0], '/');
	snprintf(d, sizeof d, "%.*s", sl ? (int)(sl - argv[0]) : 1,
	         sl ? argv[0] : ".");
	snprintf(p, sizeof p, "%s/calls", d);
	if ((log = fopen(p, "a")) != NULL)
	{
		fprintf(log, "%s %s\n", argc > 1 ? argv[1] : "",
		        lb && *lb ? lb : "unset");
		fclose(log);
	}
	if (argc < 2)
		return 1;
	f = !strcmp(argv[1], "status") ? "status.json"
	  : !strcmp(argv[1], "whois") ? "whois.json"
	  : !strcmp(argv[1], "ip") ? "ip.txt" : NULL;
	if (!f)
		return 1;
	snprintf(p, sizeof p, "%s/%s", d, f);
	if ((in = fopen(p, "r")) == NULL)
		return 1;
	while ((n = fread(buf, 1, sizeof buf, in)) > 0)
		fwrite(buf, 1, n, stdout);
	fclose(in);
	return 0;
}
