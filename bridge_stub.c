#include "bridge.h"

#include <stdio.h>
#include <stddef.h>

void dch_bridge_prepare(char **argv, int resumed) { (void)argv; (void)resumed; }
void dch_bridge_start(char **argv, int resumed, int ptyfd, int listenfd,
                      int statusfd)
{
	(void)argv; (void)resumed; (void)ptyfd; (void)listenfd; (void)statusfd;
}
void dch_bridge_reap(void) {}

int dch_bridge_agent_list(int json)
{
	(void)json;
	fprintf(stderr, "dch: native agent bridge is unavailable in dch-lite\n");
	return 3;
}

int dch_bridge_agent_send(const char *name, int argc, char **argv)
{
	(void)name; (void)argc; (void)argv;
	fprintf(stderr, "dch: native agent bridge is unavailable in dch-lite\n");
	return 3;
}
