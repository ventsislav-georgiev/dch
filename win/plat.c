/*
** The Windows client's platform layer (#077): the config and the cache dir,
** the tailscale CLI through CreateProcess, Winsock's error text, and the
** console in raw VT mode. See win/win.h.
**
** Paths are the ANSI code page's, as the C runtime's file calls take them.
** ponytail: a profile path outside that code page has no cache (and no
** config at %APPDATA%); DCH_CONFIG or a UTF-8 manifest if one ever matters.
*/
#include "win/win.h"
#include "rframe.h"

#include <ctype.h>
#include <stdint.h>

/* ---- config and state ------------------------------------------------ */

/* An absolute path that starts with a drive: X:\ or X:/. A relative one
** names whatever directory dch was started from. */
static int
drive_abs(const char *d, size_t l)
{
	return l >= 3 && isalpha((unsigned char)d[0]) && d[1] == ':' &&
	       (d[2] == '\\' || d[2] == '/');
}

static int
is_file(const char *p)
{
	DWORD a = GetFileAttributesA(p);

	return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

/* The value of a config line that sets `key`, cut in place, else NULL:
** dch.c's config_line, the same format. */
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

/* The config: $DCH_CONFIG, else %APPDATA%\dch\config. `key`'s value
** (last one wins) into out[cap]: 1 when set, else 0. */
int
dch_config_get(const char *key, char *out, size_t cap)
{
	const char *e = getenv("DCH_CONFIG"), *a = getenv("APPDATA");
	char path[1024], line[1024], *v;
	int found = 0, n;
	FILE *f;

	if (e && *e)
		n = snprintf(path, sizeof path, "%s", e);
	else if (a && *a)
		n = snprintf(path, sizeof path, "%s\\dch\\config", a);
	else
		return 0;
	if (n < 0 || (size_t)n >= sizeof path || !(f = fopen(path, "r")))
		return 0;
	while (fgets(line, sizeof line, f))
		if ((v = config_line(line, key)))
		{
			snprintf(out, cap, "%s", v);
			found = 1;
		}
	fclose(f);
	return found;
}

/* A DCH_NO_* switch: unset, empty or "0" is off, anything else on. */
int
dch_env_on(const char *key)
{
	const char *e = getenv(key);

	return e && *e && strcmp(e, "0") != 0;
}

/* The remote cache's directory: %LOCALAPPDATA%\dch, made when create is
** set. 0, or -1 (no cache) when there is none, it is not a directory, or
** it is a reparse point (a link to elsewhere). */
int
dch_history_dir(char *out, size_t outsz, const char *sockdir, int create)
{
	const char *b = getenv("LOCALAPPDATA");
	DWORD a;
	int n;

	(void)sockdir;
	if (!b || !drive_abs(b, strlen(b)) ||
	    (n = snprintf(out, outsz, "%s\\dch", b)) < 0 || (size_t)n >= outsz)
		return -1;
	if (create && !CreateDirectoryA(out, NULL) &&
	    GetLastError() != ERROR_ALREADY_EXISTS)
		return -1;
	a = GetFileAttributesA(out);
	return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) &&
	       !(a & FILE_ATTRIBUTE_REPARSE_POINT) ? 0 : -1;
}

/* ---- sockets --------------------------------------------------------- */

/* For sockets only, here: no child inherits one, and a non-blocking one. */
int
cloexec(int fd)
{
	return SetHandleInformation((HANDLE)(uintptr_t)(unsigned)fd,
	                            HANDLE_FLAG_INHERIT, 0) ? 0 : -1;
}

int
nonblock(int fd)
{
	u_long on = 1;

	return ioctlsocket((SOCKET)fd, FIONBIO, &on) ? -1 : 0;
}

/* Winsock error e as text, UTF-8, no trailing period. */
const char *
sock_str(int e)
{
	static char s[256];
	WCHAR w[256];
	DWORD n = FormatMessageW(FORMAT_MESSAGE_FROM_SYSTEM |
	                         FORMAT_MESSAGE_IGNORE_INSERTS, NULL, (DWORD)e,
	                         0, w, sizeof w / sizeof *w, NULL);
	int k = n ? WideCharToMultiByte(CP_UTF8, 0, w, (int)n, s,
	                                (int)sizeof s - 1, NULL, NULL) : 0;

	while (k > 0 && strchr(" \t\r\n.", s[k - 1]))
		k--;
	if (k <= 0)
		k = snprintf(s, sizeof s, "error %d", e);
	s[k] = '\0';
	return s;
}

/* ---- the tailscale CLI ----------------------------------------------- */

/* The tailscale CLI into g_ts: tailscale.exe in the first drive-absolute
** PATH entry that has one, else in %ProgramFiles%\Tailscale, else in
** C:\Program Files\Tailscale (its installer's). */
void
ts_find(void)
{
	static const char leaf[] = "\\tailscale.exe";
	const char *e = getenv("PATH"), *pf = getenv("ProgramFiles"), *d, *c;
	char p[PATH_MAX];
	size_t l;

	g_ts[0] = '\0';
	for (d = e; d && *d; d = c ? c + 1 : NULL)
	{
		c = strchr(d, ';');
		l = c ? (size_t)(c - d) : strlen(d);
		if (l >= 2 && d[0] == '"' && d[l - 1] == '"')
		{
			d++;
			l -= 2;
		}
		while (l > 3 && (d[l - 1] == '\\' || d[l - 1] == '/'))
			l--;
		if (!drive_abs(d, l) || l + sizeof leaf > sizeof p)
			continue;
		memcpy(p, d, l);
		memcpy(p + l, leaf, sizeof leaf);
		if (is_file(p))
		{
			copy_str(g_ts, PATH_MAX, p);
			return;
		}
	}
	if (pf && drive_abs(pf, strlen(pf)) &&
	    snprintf(p, sizeof p, "%s\\Tailscale\\tailscale.exe", pf) <
	    (int)sizeof p && is_file(p))
		copy_str(g_ts, PATH_MAX, p);
	else if (is_file("C:\\Program Files\\Tailscale\\tailscale.exe"))
		copy_str(g_ts, PATH_MAX,
		         "C:\\Program Files\\Tailscale\\tailscale.exe");
}

/* a as one argument of a command line, quoted the way the C runtime's
** argv parsing undoes it. */
static void
arg_quote(struct sb *b, const char *a)
{
	size_t bs, i;

	if (*a && !strpbrk(a, " \t\n\v\""))
	{
		sb_s(b, a);
		return;
	}
	sb_add(b, "\"", 1);
	for (;; a++)
	{
		for (bs = 0; *a == '\\'; a++)
			bs++;
		/* backslashes double before a quote, the closing one too */
		for (i = 0; i < (*a && *a != '"' ? bs : bs * 2); i++)
			sb_add(b, "\\", 1);
		if (!*a)
			break;
		if (*a == '"')
			sb_add(b, "\\", 1);
		sb_add(b, a, 1);
	}
	sb_add(b, "\"", 1);
}

/* Run argv (argv[0] an absolute path; no shell, no window) with stdin and
** stderr on NUL; it inherits no handle but those. Its stdout goes to *out
** (malloc'd, NUL-terminated, *outn bytes, at most cap) when out is set.
** Past timeout_ms or cap it is killed. Its exit code (win.h's WIFEXITED
** and WEXITSTATUS read it), or -1 when it could not run, timed out or
** overflowed. err: nothing here reads a child's stderr; "" when given. */
int
rf_run(char *const argv[], char **out, size_t *outn, size_t cap, char *err,
       size_t errcap, int timeout_ms)
{
	SECURITY_ATTRIBUTES sa = { sizeof sa, NULL, TRUE };
	STARTUPINFOEXA si;
	PROCESS_INFORMATION pi;
	HANDLE rd = NULL, wr = NULL, nul, inh[2];
	LPPROC_THREAD_ATTRIBUTE_LIST al = NULL;
	SIZE_T asz = 0;
	struct sb cmd = {0};
	long long end = now_ms() + timeout_ms;
	char *ob = NULL;
	size_t on = 0;
	DWORD code = 1;
	int i, bad = 0, ran = 0;

	if (err && errcap)
		err[0] = '\0';
	if (out)
		*out = NULL;
	for (i = 0; argv[i]; i++)
	{
		if (i)
			sb_add(&cmd, " ", 1);
		arg_quote(&cmd, argv[i]);
	}
	nul = CreateFileA("NUL", GENERIC_READ | GENERIC_WRITE,
	                  FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
	                  OPEN_EXISTING, 0, NULL);
	if (cmd.bad || nul == INVALID_HANDLE_VALUE ||
	    (out && (!(ob = malloc(cap + 1)) || !CreatePipe(&rd, &wr, &sa, 0) ||
	             !SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0))))
		goto fail;
	InitializeProcThreadAttributeList(NULL, 1, 0, &asz);
	if (!(al = malloc(asz)) ||
	    !InitializeProcThreadAttributeList(al, 1, 0, &asz))
	{
		free(al);
		al = NULL;
		goto fail;
	}
	inh[0] = nul;
	inh[1] = out ? wr : nul;
	if (!UpdateProcThreadAttribute(al, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
	                               inh, (out ? 2 : 1) * sizeof *inh, NULL,
	                               NULL))
		goto fail;
	memset(&si, 0, sizeof si);
	si.StartupInfo.cb = sizeof si;
	si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
	si.StartupInfo.hStdInput = nul;
	si.StartupInfo.hStdOutput = out ? wr : nul;
	si.StartupInfo.hStdError = nul;
	si.lpAttributeList = al;
	if (!CreateProcessA(argv[0], cmd.p, NULL, NULL, TRUE,
	                    CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
	                    NULL, NULL, &si.StartupInfo, &pi))
		goto fail;
	ran = 1;
	if (wr)
		CloseHandle(wr);
	wr = NULL;
	/* anonymous pipes cannot be waited on: peek, else wait a little on
	** the process; the pipe breaks once it has exited and been drained */
	while (rd && !bad)
	{
		DWORD avail = 0, got = 0;

		if (!PeekNamedPipe(rd, NULL, 0, NULL, &avail, NULL))
			break;
		if (avail && on >= cap)
			bad = 1;	/* over the cap */
		else if (avail)
		{
			if (avail > cap - on)
				avail = (DWORD)(cap - on);
			if (!ReadFile(rd, ob + on, avail, &got, NULL) || !got)
				break;
			on += got;
		}
		else if (now_ms() >= end || g_stop)
			bad = 1;
		else
			WaitForSingleObject(pi.hProcess, 10);
	}
	if (!bad)
	{
		long long left = end - now_ms();

		if (WaitForSingleObject(pi.hProcess, left > 0 ? (DWORD)left : 0)
		    != WAIT_OBJECT_0)
			bad = 1;
	}
	if (bad)
	{
		TerminateProcess(pi.hProcess, 1);
		WaitForSingleObject(pi.hProcess, 1000);
	}
	else if (!GetExitCodeProcess(pi.hProcess, &code))
		bad = 1;
	CloseHandle(pi.hThread);
	CloseHandle(pi.hProcess);
fail:
	if (al)
	{
		DeleteProcThreadAttributeList(al);
		free(al);
	}
	if (rd)
		CloseHandle(rd);
	if (wr)
		CloseHandle(wr);
	if (nul != INVALID_HANDLE_VALUE)
		CloseHandle(nul);
	free(cmd.p);
	if (!ran || bad)
	{
		free(ob);
		return -1;
	}
	if (out)
	{
		ob[on] = '\0';
		*out = ob;
		if (outn)
			*outn = on;
	}
	return code > 255 ? 255 : (int)code;
}

/* ---- the console ----------------------------------------------------- */

/* attach.c's TERM_RESET: what a program in the session may have left on
** (mouse, bracketed paste, the alternate screen, the kitty keyboard
** stack) off again. */
#define TERM_RESET "\033[?25h" \
	"\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l" \
	"\033[?1004l" \
	"\033[?2004l\033[?7h" \
	"\033[<8u" \
	"\033[>4;0m" \
	"\033[?1049l" \
	"\033[<8u"

static HANDLE c_in, c_out;
static DWORD in_mode, out_mode;
static volatile LONG c_raw;	/* 1 while the modes are ours */
static WCHAR c_high;		/* a high surrogate whose low is to come */

/* Ctrl-Break, a closed window, a logoff: the modes back, then the default
** handler ends the process. Ctrl-C is a key in raw mode. */
static BOOL WINAPI
on_ctrl(DWORD ev)
{
	(void)ev;
	con_restore();
	return FALSE;
}

int
con_raw(void)
{
	DWORD m;

	c_in = GetStdHandle(STD_INPUT_HANDLE);
	c_out = GetStdHandle(STD_OUTPUT_HANDLE);
	if (!GetConsoleMode(c_in, &in_mode) || !GetConsoleMode(c_out, &out_mode))
		return -1;
	SetConsoleCtrlHandler(on_ctrl, TRUE);
	atexit(con_restore);
	InterlockedExchange(&c_raw, 1);
	m = (in_mode & ~(DWORD)(ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT |
	                        ENABLE_PROCESSED_INPUT | ENABLE_QUICK_EDIT_MODE))
	    | ENABLE_EXTENDED_FLAGS | ENABLE_WINDOW_INPUT |
	    ENABLE_VIRTUAL_TERMINAL_INPUT;
	if (!SetConsoleMode(c_out, out_mode | ENABLE_PROCESSED_OUTPUT |
	                    ENABLE_VIRTUAL_TERMINAL_PROCESSING |
	                    DISABLE_NEWLINE_AUTO_RETURN) ||
	    !SetConsoleMode(c_in, m))
	{
		InterlockedExchange(&c_raw, 0);
		SetConsoleMode(c_in, in_mode);
		SetConsoleMode(c_out, out_mode);
		return -2;
	}
	return 0;
}

/* The modes as they were, once (the handler thread or exit may race the
** main one here). */
void
con_restore(void)
{
	DWORD w;

	if (InterlockedExchange(&c_raw, 0) != 1)
		return;
	WriteFile(c_out, TERM_RESET, sizeof TERM_RESET - 1, &w, NULL);
	SetConsoleMode(c_in, in_mode);
	SetConsoleMode(c_out, out_mode);
}

/* The visible window's size, not the buffer's: 0 x 0 when unknown. */
void
con_size(struct winsize *ws)
{
	CONSOLE_SCREEN_BUFFER_INFO bi;

	memset(ws, 0, sizeof *ws);
	if (GetConsoleScreenBufferInfo(c_out, &bi))
	{
		ws->ws_col = (unsigned short)(bi.srWindow.Right -
		                              bi.srWindow.Left + 1);
		ws->ws_row = (unsigned short)(bi.srWindow.Bottom -
		                              bi.srWindow.Top + 1);
	}
}

HANDLE
con_in(void)
{
	return c_in;
}

/* The pending input records' characters as UTF-8 into buf[cap] (cap of
** 1024 or more): a key press's, repeated as it says, and the character a
** released Alt carries (Alt+numpad). VT input mode makes every other key
** a sequence of those. A resize record only wakes the caller, which reads
** the window's size itself. */
int
con_keys(unsigned char *buf, size_t cap)
{
	INPUT_RECORD rec[128];
	WCHAR w[300];
	DWORD n = 0, got = 0, i;
	int wn = 0, k;

	if (c_high)
		w[wn++] = c_high;
	c_high = 0;
	if (!GetNumberOfConsoleInputEvents(c_in, &n))
		return -1;
	if (n && !ReadConsoleInputW(c_in, rec, n < 128 ? n : 128, &got))
		return -1;
	for (i = 0; i < got; i++)
	{
		const KEY_EVENT_RECORD *ke = &rec[i].Event.KeyEvent;
		WORD r;

		if (rec[i].EventType != KEY_EVENT || !ke->uChar.UnicodeChar ||
		    (!ke->bKeyDown && ke->wVirtualKeyCode != VK_MENU))
			continue;
		for (r = ke->wRepeatCount ? ke->wRepeatCount : 1;
		     r-- && wn < 256;)
			w[wn++] = ke->uChar.UnicodeChar;
	}
	if (wn && w[wn - 1] >= 0xd800 && w[wn - 1] < 0xdc00)
		c_high = w[--wn];
	if (!wn)
		return 0;
	/* at most 3 bytes a UTF-16 unit: 768 */
	k = WideCharToMultiByte(CP_UTF8, 0, w, wn, (char *)buf, (int)cap, NULL,
	                        NULL);
	return k > 0 ? k : 0;
}

/* All of p[0..n) to the console: 0, or -1 when it is gone. */
int
con_write(const void *p, size_t n)
{
	const char *s = p;
	DWORD w;

	while (n)
	{
		if (!WriteFile(c_out, s, n > 65536 ? 65536 : (DWORD)n, &w, NULL) ||
		    !w)
			return -1;
		s += w;
		n -= w;
	}
	return 0;
}
