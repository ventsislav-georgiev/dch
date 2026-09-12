#include "dtach.h"

#include <ctype.h>
#include <dirent.h>
#include <limits.h>
#include <stdint.h>
#include <sys/poll.h>
#include <sys/random.h>
#include <unistd.h>

#define BRIDGE_MARKER "DCH_NATIVE_BRIDGE_ID"
#define FRAME_MAX 65536
#define MESSAGE_MAX 16384
#define PENDING_MAX 16
#define QUEUE_RETRY_MS 5000
#define QUEUE_TIMEOUT_MS 30000
#define QUEUE_ERROR_MAX 2048

static int owner_fd = -1;
static pid_t sidecar_pid = -1;
static volatile sig_atomic_t sidecar_stop;
static int sidecar_life_fd = -1;
static long long request_deadline;
static int random_bytes(unsigned char *out, size_t len);
static int process_start(long pid, char *out, size_t cap);
static int receipt_status(const char *status);
static int monotonic_ms(long long *out);
struct peer;
static int send_receipt(const struct peer *target, const char *own_socket,
                        const char *id, const char *status);

static void stop_sidecar(int sig) {
  (void)sig;
  sidecar_stop = 1;
}

struct sha256 {
  uint32_t h[8];
  uint64_t bits;
  unsigned char buf[64];
  size_t n;
};

static uint32_t ror(uint32_t x, unsigned n) {
  return (x >> n) | (x << (32 - n));
}

static void sha_block(struct sha256 *s, const unsigned char *p) {
  static const uint32_t k[64] = {
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
      0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
      0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
      0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
      0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
      0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
      0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
      0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
      0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
  uint32_t w[64], a, b, c, d, e, f, g, h, t1, t2;
  for (int i = 0; i < 16; i++)
    w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
           ((uint32_t)p[i * 4 + 2] << 8) | p[i * 4 + 3];
  for (int i = 16; i < 64; i++)
    w[i] = (ror(w[i - 15], 7) ^ ror(w[i - 15], 18) ^ (w[i - 15] >> 3)) +
           w[i - 16] +
           (ror(w[i - 2], 17) ^ ror(w[i - 2], 19) ^ (w[i - 2] >> 10)) +
           w[i - 7];
  a = s->h[0];
  b = s->h[1];
  c = s->h[2];
  d = s->h[3];
  e = s->h[4];
  f = s->h[5];
  g = s->h[6];
  h = s->h[7];
  for (int i = 0; i < 64; i++) {
    t1 = h + (ror(e, 6) ^ ror(e, 11) ^ ror(e, 25)) + ((e & f) ^ ((~e) & g)) +
         k[i] + w[i];
    t2 = (ror(a, 2) ^ ror(a, 13) ^ ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
    h = g;
    g = f;
    f = e;
    e = d + t1;
    d = c;
    c = b;
    b = a;
    a = t1 + t2;
  }
  s->h[0] += a;
  s->h[1] += b;
  s->h[2] += c;
  s->h[3] += d;
  s->h[4] += e;
  s->h[5] += f;
  s->h[6] += g;
  s->h[7] += h;
}

static void sha_init(struct sha256 *s) {
  static const uint32_t h[] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                               0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
  memcpy(s->h, h, sizeof h);
  s->bits = 0;
  s->n = 0;
}

static void sha_add(struct sha256 *s, const void *data, size_t n) {
  const unsigned char *p = data;
  s->bits += (uint64_t)n * 8;
  while (n) {
    size_t take = 64 - s->n;
    if (take > n)
      take = n;
    memcpy(s->buf + s->n, p, take);
    s->n += take;
    p += take;
    n -= take;
    if (s->n == 64) {
      sha_block(s, s->buf);
      s->n = 0;
    }
  }
}

static void sha_hex(const void *data, size_t n, char out[65]) {
  struct sha256 s;
  unsigned char d[32];
  uint64_t bits;
  size_t i;
  sha_init(&s);
  sha_add(&s, data, n);
  bits = s.bits;
  s.buf[s.n++] = 0x80;
  if (s.n > 56) {
    memset(s.buf + s.n, 0, 64 - s.n);
    sha_block(&s, s.buf);
    s.n = 0;
  }
  memset(s.buf + s.n, 0, 56 - s.n);
  for (i = 0; i < 8; i++)
    s.buf[63 - i] = (unsigned char)(bits >> (i * 8));
  sha_block(&s, s.buf);
  for (i = 0; i < 8; i++) {
    d[i * 4] = s.h[i] >> 24;
    d[i * 4 + 1] = s.h[i] >> 16;
    d[i * 4 + 2] = s.h[i] >> 8;
    d[i * 4 + 3] = s.h[i];
  }
  {
    static const char hex[] = "0123456789abcdef";
    for (i = 0; i < 32; i++) {
      out[i * 2] = hex[d[i] >> 4];
      out[i * 2 + 1] = hex[d[i] & 15];
    }
    out[64] = '\0';
  }
}

static int utf8_ok(const unsigned char *s, size_t n) {
  for (size_t i = 0; i < n;) {
    unsigned c = s[i++];
    int more;
    uint32_t cp;
    if (c < 0x80) {
      if (!c)
        return 0;
      continue;
    }
    if (c >= 0xc2 && c <= 0xdf) {
      more = 1;
      cp = c & 31;
    } else if (c >= 0xe0 && c <= 0xef) {
      more = 2;
      cp = c & 15;
    } else if (c >= 0xf0 && c <= 0xf4) {
      more = 3;
      cp = c & 7;
    } else
      return 0;
    if (i + (size_t)more > n)
      return 0;
    for (int j = 0; j < more; j++) {
      unsigned q = s[i++];
      if ((q & 0xc0) != 0x80)
        return 0;
      cp = (cp << 6) | (q & 63);
    }
    if ((more == 2 && cp < 0x800) || (more == 3 && cp < 0x10000) ||
        cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
      return 0;
  }
  return 1;
}

enum json_kind { JSON_STRING, JSON_OBJECT, JSON_ARRAY, JSON_PRIMITIVE };
struct json_token {
  enum json_kind kind;
  int start, end, parent, next;
};
struct json_parser {
  const char *text;
  size_t len, pos;
  struct json_token tok[1024];
  int ntok;
};

static void json_space(struct json_parser *p) {
  while (p->pos < p->len && strchr(" \t\r\n", p->text[p->pos]))
    p->pos++;
}

static int json_new(struct json_parser *p, enum json_kind kind, int start,
                    int parent) {
  int n;
  if (p->ntok == (int)(sizeof p->tok / sizeof p->tok[0]))
    return -1;
  n = p->ntok++;
  p->tok[n].kind = kind;
  p->tok[n].start = start;
  p->tok[n].end = -1;
  p->tok[n].parent = parent;
  p->tok[n].next = -1;
  return n;
}

static int json_hex4(const char *s, uint32_t *value) {
  uint32_t v = 0;
  for (int i = 0; i < 4; i++) {
    int c = (unsigned char)s[i];
    if (c >= '0' && c <= '9')
      c -= '0';
    else if (c >= 'a' && c <= 'f')
      c = c - 'a' + 10;
    else if (c >= 'A' && c <= 'F')
      c = c - 'A' + 10;
    else
      return -1;
    v = (v << 4) | (uint32_t)c;
  }
  *value = v;
  return 0;
}

static int json_parse_string(struct json_parser *p, int parent) {
  int token = json_new(p, JSON_STRING, (int)++p->pos, parent);
  if (token < 0)
    return -1;
  while (p->pos < p->len) {
    unsigned char c = (unsigned char)p->text[p->pos++];
    if (c == '"') {
      p->tok[token].end = (int)p->pos - 1;
      p->tok[token].next = p->ntok;
      return token;
    }
    if (c < 0x20)
      return -1;
    if (c == '\\') {
      if (p->pos >= p->len)
        return -1;
      c = (unsigned char)p->text[p->pos++];
      if (!strchr("\"\\/bfnrtu", c))
        return -1;
      if (c == 'u') {
        uint32_t ignored;
        if (p->len - p->pos < 4 || json_hex4(p->text + p->pos, &ignored) < 0)
          return -1;
        p->pos += 4;
      }
    }
  }
  return -1;
}

static int json_parse_value(struct json_parser *, int);

static int json_number(const char *s, size_t n) {
  size_t i = 0;
  if (i < n && s[i] == '-')
    i++;
  if (i == n)
    return 0;
  if (s[i] == '0')
    i++;
  else {
    if (s[i] < '1' || s[i] > '9')
      return 0;
    while (i < n && isdigit((unsigned char)s[i]))
      i++;
  }
  if (i < n && s[i] == '.') {
    if (++i == n || !isdigit((unsigned char)s[i]))
      return 0;
    while (i < n && isdigit((unsigned char)s[i]))
      i++;
  }
  if (i < n && (s[i] == 'e' || s[i] == 'E')) {
    i++;
    if (i < n && (s[i] == '+' || s[i] == '-'))
      i++;
    if (i == n || !isdigit((unsigned char)s[i]))
      return 0;
    while (i < n && isdigit((unsigned char)s[i]))
      i++;
  }
  return i == n;
}

static int json_parse_object(struct json_parser *p, int parent) {
  int object = json_new(p, JSON_OBJECT, (int)p->pos++, parent);
  if (object < 0)
    return -1;
  json_space(p);
  if (p->pos < p->len && p->text[p->pos] == '}') {
    p->pos++;
    p->tok[object].end = (int)p->pos;
    p->tok[object].next = p->ntok;
    return object;
  }
  for (;;) {
    int key;
    if (p->pos >= p->len || p->text[p->pos] != '"' ||
        (key = json_parse_string(p, object)) < 0)
      return -1;
    json_space(p);
    if (p->pos >= p->len || p->text[p->pos++] != ':')
      return -1;
    json_space(p);
    if (json_parse_value(p, key) < 0)
      return -1;
    json_space(p);
    if (p->pos >= p->len)
      return -1;
    if (p->text[p->pos++] == '}')
      break;
    if (p->text[p->pos - 1] != ',')
      return -1;
    json_space(p);
  }
  p->tok[object].end = (int)p->pos;
  p->tok[object].next = p->ntok;
  return object;
}

static int json_parse_array(struct json_parser *p, int parent) {
  int array = json_new(p, JSON_ARRAY, (int)p->pos++, parent);
  if (array < 0)
    return -1;
  json_space(p);
  if (p->pos < p->len && p->text[p->pos] == ']')
    p->pos++;
  else
    for (;;) {
      if (json_parse_value(p, array) < 0)
        return -1;
      json_space(p);
      if (p->pos >= p->len)
        return -1;
      if (p->text[p->pos++] == ']')
        break;
      if (p->text[p->pos - 1] != ',')
        return -1;
      json_space(p);
    }
  p->tok[array].end = (int)p->pos;
  p->tok[array].next = p->ntok;
  return array;
}

static int json_parse_primitive(struct json_parser *p, int parent) {
  int start = (int)p->pos, token;
  size_t n;
  while (p->pos < p->len && !strchr(" \t\r\n,]}", p->text[p->pos]))
    p->pos++;
  if (p->pos == (size_t)start)
    return -1;
  n = p->pos - (size_t)start;
  if (!((n == 4 && memcmp(p->text + start, "true", 4) == 0) ||
        (n == 5 && memcmp(p->text + start, "false", 5) == 0) ||
        (n == 4 && memcmp(p->text + start, "null", 4) == 0))) {
    if (!json_number(p->text + start, n))
      return -1;
  }
  token = json_new(p, JSON_PRIMITIVE, start, parent);
  if (token < 0)
    return -1;
  p->tok[token].end = (int)p->pos;
  p->tok[token].next = p->ntok;
  return token;
}

static int json_parse_value(struct json_parser *p, int parent) {
  if (p->pos >= p->len)
    return -1;
  if (p->text[p->pos] == '{')
    return json_parse_object(p, parent);
  if (p->text[p->pos] == '[')
    return json_parse_array(p, parent);
  if (p->text[p->pos] == '"')
    return json_parse_string(p, parent);
  return json_parse_primitive(p, parent);
}

static int json_parse(struct json_parser *p, const char *text) {
  int root;
  memset(p, 0, sizeof *p);
  p->text = text;
  p->len = strlen(text);
  if (!utf8_ok((const unsigned char *)text, p->len))
    return -1;
  json_space(p);
  root = json_parse_value(p, -1);
  json_space(p);
  return root == 0 && p->pos == p->len ? 0 : -1;
}

static int json_decode(const struct json_parser *p, int token, char *out,
                       size_t cap) {
  size_t n = 0;
  if (token < 0 || p->tok[token].kind != JSON_STRING)
    return -1;
  for (int i = p->tok[token].start; i < p->tok[token].end; i++) {
    uint32_t cp;
    unsigned char c = (unsigned char)p->text[i];
    if (c != '\\') {
      if (n + 1 >= cap)
        return -1;
      out[n++] = (char)c;
      continue;
    }
    c = (unsigned char)p->text[++i];
    if (c == 'b')
      c = '\b';
    else if (c == 'f')
      c = '\f';
    else if (c == 'n')
      c = '\n';
    else if (c == 'r')
      c = '\r';
    else if (c == 't')
      c = '\t';
    else if (c == 'u') {
      if (json_hex4(p->text + i + 1, &cp) < 0)
        return -1;
      i += 4;
      if (cp >= 0xd800 && cp <= 0xdbff) {
        uint32_t low;
        if (i + 6 >= p->tok[token].end || p->text[i + 1] != '\\' ||
            p->text[i + 2] != 'u' || json_hex4(p->text + i + 3, &low) < 0 ||
            low < 0xdc00 || low > 0xdfff)
          return -1;
        cp = 0x10000 + ((cp - 0xd800) << 10) + low - 0xdc00;
        i += 6;
      } else if (cp >= 0xdc00 && cp <= 0xdfff)
        return -1;
      if (cp < 0x80) {
        if (n + 1 >= cap)
          return -1;
        out[n++] = (char)cp;
      } else if (cp < 0x800) {
        if (n + 2 >= cap)
          return -1;
        out[n++] = (char)(0xc0 | (cp >> 6));
        out[n++] = (char)(0x80 | (cp & 63));
      } else if (cp < 0x10000) {
        if (n + 3 >= cap)
          return -1;
        out[n++] = (char)(0xe0 | (cp >> 12));
        out[n++] = (char)(0x80 | ((cp >> 6) & 63));
        out[n++] = (char)(0x80 | (cp & 63));
      } else {
        if (n + 4 >= cap)
          return -1;
        out[n++] = (char)(0xf0 | (cp >> 18));
        out[n++] = (char)(0x80 | ((cp >> 12) & 63));
        out[n++] = (char)(0x80 | ((cp >> 6) & 63));
        out[n++] = (char)(0x80 | (cp & 63));
      }
      continue;
    }
    if (n + 1 >= cap)
      return -1;
    out[n++] = (char)c;
  }
  out[n] = '\0';
  return utf8_ok((unsigned char *)out, n) ? 0 : -1;
}

static int json_key_eq(const struct json_parser *p, int token,
                       const char *key) {
  char decoded[160];
  return json_decode(p, token, decoded, sizeof decoded) == 0 &&
         strcmp(decoded, key) == 0;
}

static int json_field(const struct json_parser *p, int object,
                      const char *key) {
  int found = -1;
  if (p->tok[object].kind != JSON_OBJECT)
    return -1;
  for (int i = object + 1; i < p->tok[object].next;) {
    int value;
    if (p->tok[i].parent != object || p->tok[i].kind != JSON_STRING ||
        i + 1 >= p->ntok || p->tok[i + 1].parent != i)
      return -1;
    value = i + 1;
    if (json_key_eq(p, i, key)) {
      if (found >= 0)
        return -2;
      found = value;
    }
    i = p->tok[value].next;
  }
  return found;
}

static int json_string(const char *json, const char *key, char *out, size_t cap,
                       int unique) {
  struct json_parser p;
  int field;
  (void)unique;
  if (json_parse(&p, json) < 0 || p.tok[0].kind != JSON_OBJECT ||
      (field = json_field(&p, 0, key)) < 0)
    return -1;
  return json_decode(&p, field, out, cap);
}

static int json_nested_string(const char *json, const char *object,
                              const char *key, char *out, size_t cap) {
  struct json_parser p;
  int parent, field;
  if (json_parse(&p, json) < 0 || (parent = json_field(&p, 0, object)) < 0 ||
      p.tok[parent].kind != JSON_OBJECT ||
      (field = json_field(&p, parent, key)) < 0)
    return -1;
  return json_decode(&p, field, out, cap);
}

static int json_long(const char *json, const char *key, long *out) {
  struct json_parser p;
  char number[64], *end;
  int field, n;
  if (json_parse(&p, json) < 0 || (field = json_field(&p, 0, key)) < 0 ||
      p.tok[field].kind != JSON_PRIMITIVE)
    return -1;
  n = p.tok[field].end - p.tok[field].start;
  if (n <= 0 || n >= (int)sizeof number)
    return -1;
  memcpy(number, p.text + p.tok[field].start, (size_t)n);
  number[n] = '\0';
  errno = 0;
  *out = strtol(number, &end, 10);
  return errno || *end ? -1 : 0;
}

static int json_has(const char *json, const char *key) {
  struct json_parser p;
  return json_parse(&p, json) == 0 && json_field(&p, 0, key) != -1;
}

static int json_attachments_present(const char *json) {
  struct json_parser p;
  int field;
  if (json_parse(&p, json) < 0)
    return 1;
  field = json_field(&p, 0, "file_attachments");
  if (field == -1)
    return 0;
  if (field < 0)
    return 1;
  if (p.tok[field].kind == JSON_ARRAY)
    return p.tok[field].next != field + 1;
  if (p.tok[field].kind == JSON_PRIMITIVE &&
      p.tok[field].end - p.tok[field].start == 4 &&
      memcmp(p.text + p.tok[field].start, "null", 4) == 0)
    return 0;
  return 1;
}

static size_t json_quote(char *out, size_t cap, const char *s) {
  size_t n = 0;
  if (cap)
    n < cap && (out[n] = '\"');
  n++;
  for (; *s; s++) {
    const char *esc = NULL;
    char small[7];
    unsigned char c = (unsigned char)*s;
    if (c == '\"')
      esc = "\\\"";
    else if (c == '\\')
      esc = "\\\\";
    else if (c == '\n')
      esc = "\\n";
    else if (c == '\r')
      esc = "\\r";
    else if (c == '\t')
      esc = "\\t";
    else if (c < 32) {
      snprintf(small, sizeof small, "\\u%04x", c);
      esc = small;
    }
    if (esc) {
      for (const char *q = esc; *q; q++) {
        if (n < cap)
          out[n] = *q;
        n++;
      }
    } else {
      if (n < cap)
        out[n] = c;
      n++;
    }
  }
  if (n < cap)
    out[n] = '\"';
  n++;
  if (n >= cap) {
    if (cap)
      out[0] = '\0';
    return SIZE_MAX;
  }
  out[n] = '\0';
  return n;
}

static int sha_selftest(void) {
  char h[65];
  sha_hex("", 0, h);
  if (strcmp(
          h,
          "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"))
    return -1;
  sha_hex("abc", 3, h);
  return strcmp(
             h,
             "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
             ? -1
             : 0;
}

struct peer {
  long pid;
  char name[512], socket[104], token[257], proc_start[128];
  char pid_domain[32], session_id[128], marker[128];
};

struct pending_message {
  struct peer sender;
  char id[257];
  char name[512];
  char content[MESSAGE_MAX + 1];
  char thread[128];
  int held;
};

struct pending_queue {
  struct pending_message items[PENDING_MAX];
  int head, count, error_fd;
  pid_t child;
  long long child_deadline, retry_at;
  char error[QUEUE_ERROR_MAX + 1];
  size_t error_len;
};

static int private_dir(const char *path, int create) {
  struct stat st;
  if (create && mkdir(path, 0700) < 0 && errno != EEXIST)
    return -1;
  if (lstat(path, &st) < 0 || !S_ISDIR(st.st_mode) || st.st_uid != getuid() ||
      (st.st_mode & 077))
    return -1;
  return 0;
}

static int claude_sessions(char *out, size_t cap, int create) {
  const char *base = getenv("CLAUDE_CONFIG_DIR"), *home = getenv("HOME");
  if (!base || !*base) {
    if (!home || !*home)
      return -1;
    if (snprintf(out, cap, "%s/.claude", home) >= (int)cap)
      return -1;
    if (create && mkdir(out, 0700) < 0 && errno != EEXIST)
      return -1;
    base = out;
  }
  if (base != out && snprintf(out, cap, "%s", base) >= (int)cap)
    return -1;
  if (strlen(out) + 10 >= cap)
    return -1;
  strcat(out, "/sessions");
  return private_dir(out, create);
}

static int codex_home(char *out, size_t cap) {
  const char *base = getenv("CODEX_HOME"), *home = getenv("HOME");
  if (base && *base)
    return snprintf(out, cap, "%s", base) >= (int)cap ? -1 : 0;
  return home && *home && snprintf(out, cap, "%s/.codex", home) < (int)cap ? 0
                                                                           : -1;
}

static int read_file(const char *path, char *buf, size_t cap, mode_t allowed,
                     int private) {
  int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
  struct stat st, after;
  size_t off = 0;
  char extra;
  (void)allowed;
  if (fd < 0)
    return -1;
  if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) || st.st_uid != getuid() ||
      (st.st_mode & 022) || (private && (st.st_mode & 077)) ||
      st.st_size <= 0 || st.st_size >= (off_t)cap) {
    close(fd);
    return -1;
  }
  while (off < (size_t)st.st_size) {
    ssize_t n = read(fd, buf + off, (size_t)st.st_size - off);
    if (n > 0)
      off += (size_t)n;
    else if (n < 0 && errno == EINTR)
      continue;
    else {
      close(fd);
      return -1;
    }
  }
  if (read(fd, &extra, 1) != 0 || fstat(fd, &after) < 0 ||
      after.st_size != st.st_size || after.st_dev != st.st_dev ||
      after.st_ino != st.st_ino) {
    close(fd);
    return -1;
  }
  close(fd);
  if (memchr(buf, '\0', off))
    return -1;
  buf[off] = '\0';
  return 0;
}

static int socket_safe(const char *path) {
  struct stat st, ds;
  char dir[104], *slash;
  size_t n = strlen(path);
  if (path[0] != '/' || n >= sizeof(((struct sockaddr_un *)0)->sun_path) ||
      strstr(path, "/../") || n < 2)
    return -1;
  if (snprintf(dir, sizeof dir, "%s", path) >= (int)sizeof dir)
    return -1;
  slash = strrchr(dir, '/');
  if (!slash || slash == dir)
    return -1;
  *slash = '\0';
  if (lstat(dir, &ds) < 0 || !S_ISDIR(ds.st_mode) || ds.st_uid != getuid() ||
      (ds.st_mode & 077))
    return -1;
  if (lstat(path, &st) < 0 || !S_ISSOCK(st.st_mode) || st.st_uid != getuid() ||
      (st.st_mode & 077))
    return -1;
  return 0;
}

static int load_peer_record(const char *path, const char *record_body,
                            struct peer *p, int verify_ps) {
  char b[8192], num[32], key[1400], hash[65], actual[128], source[32];
  struct stat st;
  char *base, *end;
  long record_pid, protocol;
  if (strlen(record_body) >= sizeof b || lstat(path, &st) < 0)
    return -1;
  memcpy(b, record_body, strlen(record_body) + 1);
  base = strrchr(path, '/');
  base = base ? base + 1 : (char *)path;
  errno = 0;
  p->pid = strtol(base, &end, 10);
  if (errno || p->pid <= 0 || (long)(pid_t)p->pid != p->pid ||
      strcmp(end, ".json") || (verify_ps && kill((pid_t)p->pid, 0) < 0))
    return -1;
  if (json_long(b, "pid", &record_pid) < 0 || record_pid != p->pid ||
      json_long(b, "peerProtocol", &protocol) < 0 || protocol != 1 ||
      json_string(b, "name", p->name, sizeof p->name, 1) < 0 || !p->name[0] ||
      (json_has(b, "nameSource") &&
       (json_string(b, "nameSource", source, sizeof source, 1) < 0 ||
        (strcmp(source, "user") && strcmp(source, "derived")))) ||
      json_string(b, "messagingSocketPath", p->socket, sizeof p->socket, 1) <
          0 ||
      !p->socket[0] ||
      json_string(b, "procStart", p->proc_start, sizeof p->proc_start, 1) < 0 ||
      !p->proc_start[0] ||
      json_string(b, "pidDomain", p->pid_domain, sizeof p->pid_domain, 1) < 0 ||
      !p->pid_domain[0] ||
      json_string(b, "sessionId", p->session_id, sizeof p->session_id, 1) < 0 ||
      !p->session_id[0] || socket_safe(p->socket) < 0)
    return -1;
#ifdef __APPLE__
  if (strcmp(p->pid_domain, "darwin"))
    return -1;
#else
  if (strcmp(p->pid_domain, "linux"))
    return -1;
#endif
  p->marker[0] = '\0';
  (void)json_string(b, "dchBridgeMarker", p->marker, sizeof p->marker, 1);
  if (verify_ps && (process_start(p->pid, actual, sizeof actual) < 0 ||
                    strcmp(actual, p->proc_start)))
    return -1;
  sha_hex(p->socket, strlen(p->socket), hash);
  if (snprintf(num, sizeof num, "%ld", p->pid) >= (int)sizeof num ||
      snprintf(key, sizeof key, "%.*s/%s.%s.key", (int)(base - path - 1), path,
               num, hash) >= (int)sizeof key)
    return -1;
  if (read_file(key, b, sizeof b, 0600, 1) < 0 ||
      json_string(b, "peerToken", p->token, sizeof p->token, 1) < 0 ||
      !p->token[0] ||
      json_string(b, "procStart", actual, sizeof actual, 1) < 0 ||
      strcmp(actual, p->proc_start) ||
      json_string(b, "pidDomain", actual, sizeof actual, 1) < 0 ||
      strcmp(actual, p->pid_domain))
    return -1;
  return 0;
}

struct peer_scan {
  struct peer *peers;
  int count, cap, verify_ps, overflow;
};

static int scan_peer_record(const char *path, const char *body, size_t len,
                            void *arg) {
  struct peer_scan *scan = arg;
  struct peer peer;
  long long now;
  (void)len;
  if (request_deadline > 0 &&
      (monotonic_ms(&now) < 0 || now >= request_deadline)) {
    scan->overflow = 1;
    return -1;
  }
  if (load_peer_record(path, body, &peer, scan->verify_ps) < 0)
    return 0;
  if (scan->count == scan->cap) {
    scan->overflow = 1;
    return -1;
  }
  scan->peers[scan->count++] = peer;
  return 0;
}

static int scan_peers(struct peer *peers, int cap, int verify_ps) {
  struct peer_scan scan = {peers, 0, cap, verify_ps, 0};
  if (sha_selftest() < 0 ||
      dch_visit_claude_records(scan_peer_record, &scan) < 0)
    return scan.overflow ? -1 : 0;
  return scan.count;
}

static int constant_eq(const char *a, const char *b) {
  size_t an = strlen(a), bn = strlen(b), n = an > bn ? an : bn;
  unsigned x = (unsigned)(an ^ bn);
  for (size_t i = 0; i < n; i++)
    x |=
        (unsigned char)(i < an ? a[i] : 0) ^ (unsigned char)(i < bn ? b[i] : 0);
  return x == 0;
}

static int write_all(int fd, const void *buf, size_t n) {
  const char *p = buf;
  while (n) {
    ssize_t w = write(fd, p, n);
    if (w > 0) {
      p += w;
      n -= (size_t)w;
    } else if (w < 0 && errno == EINTR)
      continue;
    else
      return -1;
  }
  return 0;
}

static int monotonic_ms(long long *out) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0)
    return -1;
  *out = (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
  return 0;
}

static long long deadline_after(int timeout_ms) {
  long long now, deadline;
  if (monotonic_ms(&now) < 0)
    return -1;
  deadline = now + timeout_ms;
  return request_deadline > 0 && request_deadline < deadline ? request_deadline
                                                             : deadline;
}

static int cancelled(void) {
  struct pollfd p = {sidecar_life_fd, POLLIN | POLLHUP, 0};
  return sidecar_stop || (sidecar_life_fd >= 0 && poll(&p, 1, 0) > 0);
}

static int wait_ready(int fd, short events, long long deadline) {
  for (;;) {
    struct pollfd p[2] = {{fd, events, 0},
                          {sidecar_life_fd, POLLIN | POLLHUP, 0}};
    long long now;
    int n = sidecar_life_fd >= 0 ? 2 : 1, r, remain;
    if (monotonic_ms(&now) < 0 || now >= deadline || cancelled())
      return -1;
    remain = deadline - now > INT_MAX ? INT_MAX : (int)(deadline - now);
    r = poll(p, n, remain);
    if (r < 0 && errno == EINTR)
      continue;
    if (r <= 0 || (n == 2 && p[1].revents) ||
        !(p[0].revents & (events | POLLERR | POLLHUP)))
      return -1;
    return 0;
  }
}

static int write_deadline(int fd, const void *buf, size_t len,
                          long long deadline) {
  const char *p = buf;
  while (len) {
    ssize_t n;
    if (wait_ready(fd, POLLOUT, deadline) < 0)
      return -1;
    n = write(fd, p, len);
    if (n > 0) {
      p += n;
      len -= (size_t)n;
    } else if (n < 0 && (errno == EINTR || errno == EAGAIN))
      continue;
    else
      return -1;
  }
  return 0;
}

static void terminate_child(pid_t pid) {
  int status;
  long long end, now;
  pid_t r;
  do
    r = waitpid(pid, &status, WNOHANG);
  while (r < 0 && errno == EINTR);
  if (r == pid || (r < 0 && errno == ECHILD))
    return;
  if (r < 0)
    return;
  kill(pid, SIGTERM);
  if (monotonic_ms(&now) == 0) {
    end = now + 1000;
    do {
      if (waitpid(pid, &status, WNOHANG) == pid)
        return;
      poll(NULL, 0, 20);
    } while (monotonic_ms(&now) == 0 && now < end);
  }
  kill(pid, SIGKILL);
  while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
  }
}

static int process_start(long pid, char *out, size_t cap) {
  int p[2], status, flags;
  pid_t child;
  size_t off = 0;
  char id[32];
  long long now, end;
  if (snprintf(id, sizeof id, "%ld", pid) >= (int)sizeof id || pipe(p) < 0)
    return -1;
  child = fork();
  if (child < 0) {
    close(p[0]);
    close(p[1]);
    return -1;
  }
  if (child == 0) {
    char *av[] = {"ps", "-o", "lstart=", "-p", id, NULL};
    char *env[] = {"LC_ALL=C", "TZ=UTC", NULL};
    long maxfd;
    close(p[0]);
    if (dup2(p[1], STDOUT_FILENO) < 0)
      _exit(127);
    close(p[1]);
    maxfd = sysconf(_SC_OPEN_MAX);
    if (maxfd < 0 || maxfd > 65536)
      maxfd = 65536;
    for (int fd = 3; fd < maxfd; fd++)
      close(fd);
    execve("/bin/ps", av, env);
    _exit(127);
  }
  close(p[1]);
  flags = fcntl(p[0], F_GETFL, 0);
  if (flags < 0 || fcntl(p[0], F_SETFL, flags | O_NONBLOCK) < 0) {
    close(p[0]);
    terminate_child(child);
    return -1;
  }
  end = deadline_after(10000);
  if (end < 0) {
    close(p[0]);
    terminate_child(child);
    return -1;
  }
  for (;;) {
    ssize_t n;
    if (wait_ready(p[0], POLLIN, end) < 0)
      break;
    if (off + 1 == cap) {
      char extra;
      n = read(p[0], &extra, 1);
      if (n > 0) {
        close(p[0]);
        terminate_child(child);
        return -1;
      }
    } else
      n = read(p[0], out + off, cap - 1 - off);
    if (n > 0)
      off += (size_t)n;
    else if (n == 0)
      break;
    else if (errno == EINTR || errno == EAGAIN)
      continue;
    else
      break;
  }
  close(p[0]);
  if (cancelled() || monotonic_ms(&now) < 0 || now >= end) {
    terminate_child(child);
    return -1;
  }
  for (;;) {
    pid_t r = waitpid(child, &status, WNOHANG);
    if (r == child)
      break;
    if (r < 0 && errno != EINTR) {
      terminate_child(child);
      return -1;
    }
    if (cancelled() || monotonic_ms(&now) < 0 || now >= end) {
      terminate_child(child);
      return -1;
    }
    poll(NULL, 0, end - now > 20 ? 20 : (int)(end - now));
  }
  if (!WIFEXITED(status) || WEXITSTATUS(status) || !off)
    return -1;
  out[off] = '\0';
  while (off && isspace((unsigned char)out[off - 1]))
    out[--off] = '\0';
  {
    size_t lead = 0;
    while (lead < off && isspace((unsigned char)out[lead]))
      lead++;
    if (lead) {
      memmove(out, out + lead, off - lead + 1);
      off -= lead;
    }
  }
  return off ? 0 : -1;
}

/* Peek, then consume exactly up to and including the newline, so the next
   frame on the same connection is never swallowed and no carry buffer exists. */
static int recv_line_before(int fd, char *out, size_t cap, long long end) {
  size_t n = 0;
  long long now;
  while (n + 1 < cap) {
    ssize_t z;
    char *nl;
    if (monotonic_ms(&now) < 0 || now >= end || cancelled() ||
        wait_ready(fd, POLLIN, end) < 0)
      return -1;
    z = recv(fd, out + n, cap - 1 - n, MSG_PEEK);
    if (z < 0 && (errno == EINTR || errno == EAGAIN))
      continue;
    if (z <= 0)
      return -1;
    nl = memchr(out + n, '\n', (size_t)z);
    if (nl)
      z = nl - (out + n) + 1;
    z = read(fd, out + n, (size_t)z);
    if (z <= 0 || memchr(out + n, '\0', (size_t)z))
      return -1;
    n += (size_t)z;
    if (out[n - 1] == '\n') {
      if (--n == 0)
        return -1;
      out[n] = '\0';
      return utf8_ok((unsigned char *)out, n) ? 0 : -1;
    }
  }
  return -1;
}

static int recv_line(int fd, char *out, size_t cap, int timeout_ms) {
  long long deadline = deadline_after(timeout_ms);
  return deadline < 0 ? -1 : recv_line_before(fd, out, cap, deadline);
}

static int connect_unix(const char *path, int timeout_ms) {
  int fd = socket(AF_UNIX, SOCK_STREAM, 0), flags, error = 0, rc;
  struct sockaddr_un un;
  socklen_t elen = sizeof error;
  long long end;
  if (fd < 0 || strlen(path) >= sizeof un.sun_path) {
    if (fd >= 0)
      close(fd);
    return -1;
  }
  memset(&un, 0, sizeof un);
  un.sun_family = AF_UNIX;
  strcpy(un.sun_path, path);
  fcntl(fd, F_SETFD, FD_CLOEXEC);
  flags = fcntl(fd, F_GETFL, 0);
  if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
    close(fd);
    return -1;
  }
  end = deadline_after(timeout_ms);
  if (end < 0) {
    close(fd);
    return -1;
  }
  rc = connect(fd, (struct sockaddr *)&un, sizeof un);
  if (rc < 0 && errno != EINPROGRESS) {
    close(fd);
    return -1;
  }
  if (rc < 0 &&
      (wait_ready(fd, POLLOUT, end) < 0 ||
       getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &elen) < 0 || error)) {
    close(fd);
    return -1;
  }
  return fd;
}

static int nonblocking(int fd) {
  int flags = fcntl(fd, F_GETFL, 0);
  return flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0 ? -1 : 0;
}

static int send_frames(int fd, const char *token, const char *payload) {
  char auth[1700], q[1600];
  long long end;
  if (json_quote(q, sizeof q, token) == SIZE_MAX ||
      snprintf(auth, sizeof auth, "{\"type\":\"auth\",\"token\":%s}\n", q) >=
          (int)sizeof auth ||
      (end = deadline_after(10000)) < 0)
    return -1;
  return write_deadline(fd, auth, strlen(auth), end) < 0 ||
                 write_deadline(fd, payload, strlen(payload), end) < 0 ||
                 write_deadline(fd, "\n", 1, end) < 0
             ? -1
             : 0;
}

static int auth_conn_before(int fd, const char *token, char *payload,
                            size_t cap, long long deadline) {
  char auth[FRAME_MAX + 1], got[257], type[32];
  if (recv_line_before(fd, auth, sizeof auth, deadline) < 0 ||
      json_string(auth, "type", type, sizeof type, 1) < 0 ||
      strcmp(type, "auth") ||
      json_string(auth, "token", got, sizeof got, 1) < 0 ||
      !constant_eq(got, token) ||
      recv_line_before(fd, payload, cap, deadline) < 0)
    return -1;
  return 0;
}

static int auth_conn(int fd, const char *token, char *payload, size_t cap) {
  long long deadline = deadline_after(10000);
  return deadline < 0 ? -1
                      : auth_conn_before(fd, token, payload, cap, deadline);
}

static int atomic_file(const char *path, const char *body, mode_t mode,
                       dev_t *dev, ino_t *ino) {
  char tmp[1500];
  unsigned char r[8];
  int fd;
  struct stat st;
  if (random_bytes(r, sizeof r) < 0)
    return -1;
  if (snprintf(tmp, sizeof tmp, "%s.tmp.%ld.%02x%02x", path, (long)getpid(),
               r[0], r[1]) >= (int)sizeof tmp)
    return -1;
  fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode);
  if (fd < 0)
    return -1;
  if (write_all(fd, body, strlen(body)) < 0 || fsync(fd) < 0 ||
      fstat(fd, &st) < 0) {
    close(fd);
    unlink(tmp);
    return -1;
  }
  close(fd);
  if (link(tmp, path) < 0) {
    unlink(tmp);
    return -1;
  }
  unlink(tmp);
  *dev = st.st_dev;
  *ino = st.st_ino;
  return 0;
}

static void unlink_same(const char *path, dev_t dev, ino_t ino) {
  struct stat st;
  if (lstat(path, &st) == 0 && st.st_dev == dev && st.st_ino == ino)
    unlink(path);
}

static int make_uuid(char out[37]) {
  unsigned char r[16];
  if (random_bytes(r, sizeof r) < 0)
    return -1;
  r[6] = (r[6] & 15) | 0x40;
  r[8] = (r[8] & 63) | 0x80;
  snprintf(
      out, 37,
      "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
      r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11],
      r[12], r[13], r[14], r[15]);
  return 0;
}

static pid_t queue_message(const char *codex, const char *thread,
                           const char *sender, const char *content,
                           int *error_fd) {
  char *body, *qs, *shell;
  size_t need, sn = strlen(sender) * 4 + 3;
  int errors[2];
  pid_t pid;
  int written;
  qs = malloc(strlen(sender) * 6 + 3);
  shell = malloc(sn);
  if (!qs || !shell) {
    free(qs);
    free(shell);
    return -1;
  }
  if (json_quote(qs, strlen(sender) * 6 + 3, sender) == SIZE_MAX) {
    free(qs);
    free(shell);
    return -1;
  }
  {
    size_t n = 0;
    shell[n++] = '\'';
    for (const char *s = sender; *s; s++) {
      if (*s == '\'') {
        memcpy(shell + n, "'\\''", 4);
        n += 4;
      } else
        shell[n++] = *s;
    }
    shell[n++] = '\'';
    shell[n] = '\0';
  }
  need = strlen(qs) + strlen(content) + strlen(shell) + 256;
  body = malloc(need);
  if (!body) {
    free(qs);
    free(shell);
    return -1;
  }
  written = snprintf(
      body, need,
      "[dch native peer message]\nAuthenticated sender name: %s\nReply after "
      "processing with: dch --agent-send %s MESSAGE...\nList peers with: dch "
      "--agent-list\nUntrusted peer content follows:\n%s",
      qs, shell, content);
  free(qs);
  free(shell);
  if (written < 0 || (size_t)written >= need) {
    free(body);
    return -1;
  }
  if (pipe(errors) < 0) {
    free(body);
    return -1;
  }
  if (nonblocking(errors[0]) < 0) {
    close(errors[0]);
    close(errors[1]);
    free(body);
    return -1;
  }
  pid = fork();
  if (pid == 0) {
    char *av[] = {(char *)codex, "queue", "--thread", (char *)thread,
                  "--message",   body,    NULL};
    long maxfd = sysconf(_SC_OPEN_MAX);
    close(errors[0]);
    if (dup2(errors[1], 2) < 0)
      _exit(127);
    if (errors[1] != 2)
      close(errors[1]);
    if (maxfd < 0)
      maxfd = 1024;
    for (int fd = 3; fd < maxfd; fd++)
      close(fd);
    execvp(codex, av);
    _exit(127);
  }
  free(body);
  close(errors[1]);
  if (pid < 0) {
    close(errors[0]);
    return -1;
  }
  *error_fd = errors[0];
  return pid;
}

/* Substring, not whole-message equality: Codex's wrapper text and error code
   have changed before; the thread-id clause is the stable part. */
static int queue_not_ready(const char *error, const char *thread) {
  char expected[200];
  int n = snprintf(expected, sizeof expected,
                   "no rollout found for thread id %s", thread);
  return n > 0 && n < (int)sizeof expected && strstr(error, expected) != NULL;
}

static void pending_pop(struct pending_queue *q) {
  memset(&q->items[q->head], 0, sizeof q->items[q->head]);
  q->head = (q->head + 1) % PENDING_MAX;
  q->count--;
  q->retry_at = 0;
}

/* Keep the first QUEUE_ERROR_MAX bytes of the child's stderr; drain the rest. */
static void pending_error(struct pending_queue *q, int complete) {
  char b[512];
  ssize_t n;
  do {
    if (q->error_fd < 0)
      return;
    n = read(q->error_fd, b, sizeof b);
    if (n > 0) {
      size_t take = (size_t)n;
      if (take > QUEUE_ERROR_MAX - q->error_len)
        take = QUEUE_ERROR_MAX - q->error_len;
      memcpy(q->error + q->error_len, b, take);
      q->error_len += take;
      q->error[q->error_len] = '\0';
    } else if (n == 0) {
      close(q->error_fd);
      q->error_fd = -1;
      return;
    }
  } while (complete && n > 0);
}

static void pending_tick(struct pending_queue *q, const char *codex,
                         const char *home, const char *sess, const char *marker,
                         const char *own_socket) {
  struct pending_message *m;
  long long now;
  int status;
  pid_t done;
  char fresh[128];
  if (monotonic_ms(&now) < 0 || !q->count)
    return;
  m = &q->items[q->head];
  if (q->child > 0) {
    pending_error(q, 0);
    done = waitpid(q->child, &status, WNOHANG);
    if (done == 0 && now < q->child_deadline)
      return;
    if (done == 0) {
      terminate_child(q->child);
      close(q->error_fd);
      q->error_fd = -1;
      q->child = -1;
      send_receipt(&m->sender, own_socket, m->id, "dropped");
      pending_pop(q);
      return;
    }
    if (done < 0 && errno == EINTR)
      return;
    pending_error(q, 1);
    if (q->error_fd >= 0)
      close(q->error_fd);
    q->error_fd = -1;
    q->child = -1;
    if (done > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0) {
      send_receipt(&m->sender, own_socket, m->id, "delivered");
      pending_pop(q);
      return;
    }
    if (done > 0 && WIFEXITED(status) && WEXITSTATUS(status) != 0 &&
        queue_not_ready(q->error, m->thread)) {
      if (!m->held) {
        send_receipt(&m->sender, own_socket, m->id, "held");
        m->held = 1;
      }
      q->retry_at = now + QUEUE_RETRY_MS;
      return;
    }
    send_receipt(&m->sender, own_socket, m->id,
                 done > 0 && WIFEXITED(status) ? "refused" : "dropped");
    pending_pop(q);
    return;
  }
  if (q->retry_at && now < q->retry_at)
    return;
  if (cancelled())
    return;
  if (dch_codex_snapshot_id(home, sess, marker, fresh, sizeof fresh, 1) < 0 ||
      strcmp(fresh, m->thread)) {
    send_receipt(&m->sender, own_socket, m->id, "refused");
    pending_pop(q);
    return;
  }
  q->error_len = 0;
  q->error[0] = '\0';
  q->child =
      queue_message(codex, m->thread, m->name, m->content, &q->error_fd);
  if (q->child < 0) {
    send_receipt(&m->sender, own_socket, m->id, "refused");
    pending_pop(q);
    return;
  }
  q->child_deadline = now + QUEUE_TIMEOUT_MS;
}

static int pending_add(struct pending_queue *q, const struct peer *sender,
                       const char *id, const char *content, const char *thread,
                       const char *own_socket) {
  int slot;
  for (int i = 0; i < q->count; i++) {
    struct pending_message *m = &q->items[(q->head + i) % PENDING_MAX];
    if (!strcmp(m->sender.socket, sender->socket) &&
        !strcmp(m->sender.session_id, sender->session_id) &&
        !strcmp(m->sender.proc_start, sender->proc_start) && !strcmp(m->id, id)) {
      if (strcmp(m->content, content))
        return -2;
      if (m->held)
        send_receipt(sender, own_socket, id, "held");
      return 0;
    }
  }
  if (q->count == PENDING_MAX)
    return -1;
  slot = (q->head + q->count) % PENDING_MAX;
  q->items[slot].sender = *sender;
  snprintf(q->items[slot].id, sizeof q->items[slot].id, "%s", id);
  snprintf(q->items[slot].name, sizeof q->items[slot].name, "%s", sender->name);
  snprintf(q->items[slot].content, sizeof q->items[slot].content, "%s", content);
  snprintf(q->items[slot].thread, sizeof q->items[slot].thread, "%s", thread);
  q->items[slot].held = q->count > 0;
  q->count++;
  if (q->items[slot].held)
    send_receipt(sender, own_socket, id, "held");
  return 0;
}

static int send_receipt(const struct peer *target, const char *own_socket,
                        const char *id, const char *status) {
  char p[2400], qid[1600], address[108], qfrom[700];
  int fd, n, timeout = 10000;
  long long now;
  if (request_deadline > 0 && monotonic_ms(&now) == 0) {
    if (now >= request_deadline)
      return -1;
    if (request_deadline - now < timeout)
      timeout = (int)(request_deadline - now);
  }
  if (!receipt_status(status) || socket_safe(target->socket) < 0 ||
      (fd = connect_unix(target->socket, timeout)) < 0)
    return -1;
  if (snprintf(address, sizeof address, "uds:%s", own_socket) >=
          (int)sizeof address ||
      json_quote(qid, sizeof qid, id) == SIZE_MAX ||
      json_quote(qfrom, sizeof qfrom, address) == SIZE_MAX) {
    close(fd);
    return -1;
  }
  n = snprintf(p, sizeof p,
               "{\"type\":\"control\",\"action\":\"peer_message_status\","
               "\"status\":\"%s\",\"orig_msg_id\":%s,\"from\":%s}",
               status, qid, qfrom);
  if (n < 0 || n >= (int)sizeof p) {
    close(fd);
    return -1;
  }
  int rc = send_frames(fd, target->token, p);
  close(fd);
  return rc;
}

/* ps is forked only for records that already match; a full verified scan
   would fork once per live Claude session on every inbound frame. */
static int peer_live(const struct peer *p) {
  char actual[128];
  return kill((pid_t)p->pid, 0) == 0 &&
         process_start(p->pid, actual, sizeof actual) == 0 &&
         !strcmp(actual, p->proc_start);
}

static int peer_by_socket(const char *address, struct peer *out) {
  struct peer p[128];
  int n, hits = 0;
  if (strncmp(address, "uds:", 4) || socket_safe(address + 4) < 0)
    return -1;
  n = scan_peers(p, 128, 0);
  if (n < 0)
    return -1;
  for (int i = 0; i < n; i++)
    if (!strcmp(p[i].socket, address + 4) && peer_live(&p[i])) {
      *out = p[i];
      hits++;
    }
  return hits == 1 ? 0 : hits > 1 ? -2 : -1;
}

/* Any argument whose basename is codex counts, so launchers such as
   `headroom wrap codex` or `env X=1 codex` still get a sidecar. A false
   match (a file named codex) only leaves an idle sidecar that never finds a
   snapshot and never publishes. */
static int is_codex(char **argv) {
  for (int i = 0; argv && argv[i]; i++) {
    const char *base = strrchr(argv[i], '/');
    base = base ? base + 1 : argv[i];
    if (!strcmp(base, "codex"))
      return 1;
  }
  return 0;
}

static int random_bytes(unsigned char *out, size_t len) {
#if defined(__APPLE__) || defined(__OpenBSD__) || defined(__FreeBSD__)
  if (getentropy(out, len) == 0)
    return 0;
#endif
  int fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  size_t off = 0;

  if (fd < 0)
    return -1;
  while (off < len) {
    ssize_t n = read(fd, out + off, len - off);
    if (n > 0)
      off += (size_t)n;
    else if (n < 0 && errno == EINTR)
      continue;
    else {
      close(fd);
      return -1;
    }
  }
  close(fd);
  return 0;
}

void dch_bridge_prepare(char **argv, int resumed) {
  unsigned char raw[16];
  char marker[80];
  const char *old;
  long pid;

  if (!is_codex(argv))
    return;
  pid = (long)getpid();
  old = getenv(BRIDGE_MARKER);
  if (resumed && old) {
    char prefix[32];
    size_t n;
    snprintf(prefix, sizeof prefix, "%ld.", pid);
    n = strlen(prefix);
    if (strncmp(old, prefix, n) == 0 && strlen(old + n) == 32) {
      int valid = 1;
      for (size_t i = n; old[i]; i++)
        if (!isxdigit((unsigned char)old[i]))
          valid = 0;
      if (valid)
        return;
    }
  }
  if (random_bytes(raw, sizeof raw) < 0) {
    unsetenv(BRIDGE_MARKER);
    return;
  }
  snprintf(marker, sizeof marker, "%ld.", pid);
  for (size_t i = 0, n = strlen(marker); i < sizeof raw; i++)
    snprintf(marker + n + i * 2, sizeof marker - n - i * 2, "%02x", raw[i]);
  setenv(BRIDGE_MARKER, marker, 1);
}

static void bridge_sidecar(int life_fd, const char *codex);

void dch_bridge_start(char **argv, int resumed, int ptyfd, int listenfd,
                      int statusfd) {
  int p[2];
  pid_t pid;
  long maxfd;

  (void)resumed;
  if (!is_codex(argv) || !getenv(BRIDGE_MARKER) || pipe(p) < 0)
    return;
  if (resumed) {
    const char *old = getenv("DCH_BRIDGE_SIDECAR_PID");
    char *end;
    long value;
    errno = 0;
    value = old ? strtol(old, &end, 10) : -1;
    if (old && !errno && *old && !*end && value > 0 && value <= INT_MAX) {
      int status;
      long long now, deadline;
      if (monotonic_ms(&now) == 0) {
        deadline = now + 2500;
        while (waitpid((pid_t)value, &status, WNOHANG) == 0 &&
               monotonic_ms(&now) == 0 && now < deadline)
          poll(NULL, 0, 20);
      }
      terminate_child((pid_t)value);
    }
  }
  if (fcntl(p[1], F_SETFD, FD_CLOEXEC) < 0) {
    close(p[0]);
    close(p[1]);
    return;
  }
  pid = fork();
  if (pid < 0) {
    close(p[0]);
    close(p[1]);
    return;
  }
  if (pid == 0) {
    int life = 3, nullfd;
    close(p[1]);
    if (p[0] != life) {
      if (dup2(p[0], life) < 0)
        _exit(1);
      close(p[0]);
    }
    signal(SIGCHLD, SIG_DFL);
    signal(SIGINT, stop_sidecar);
    signal(SIGTERM, stop_sidecar);
    signal(SIGHUP, stop_sidecar);
    signal(SIGPIPE, SIG_IGN);
    maxfd = sysconf(_SC_OPEN_MAX);
    if (maxfd < 0 || maxfd > 65536)
      maxfd = 65536;
    for (int fd = 0; fd < maxfd; fd++)
      if (fd != life)
        close(fd);
    nullfd = open("/dev/null", O_RDWR);
    if (nullfd >= 0) {
      dup2(nullfd, 0);
      dup2(nullfd, 1);
      dup2(nullfd, 2);
      if (nullfd > 3)
        close(nullfd);
    }
    (void)ptyfd;
    (void)listenfd;
    (void)statusfd;
    bridge_sidecar(life, argv[0]);
    _exit(0);
  }
  close(p[0]);
  if (owner_fd >= 0)
    close(owner_fd);
  owner_fd = p[1];
  sidecar_pid = pid;
  {
    char value[32];
    snprintf(value, sizeof value, "%ld", (long)pid);
    setenv("DCH_BRIDGE_SIDECAR_PID", value, 1);
  }
}

void dch_bridge_reap(void) {
  int status;

  if (sidecar_pid > 0 && waitpid(sidecar_pid, &status, WNOHANG) == sidecar_pid)
    sidecar_pid = -1;
}

static int server_socket(const char *path, dev_t *dev, ino_t *ino) {
  int fd;
  struct sockaddr_un un;
  struct stat st;
  char dir[104];
  if (snprintf(dir, sizeof dir, "%s", path) >= (int)sizeof dir)
    return -1;
  *strrchr(dir, '/') = '\0';
  if (private_dir(dir, 1) < 0 || lstat(path, &st) == 0 || errno != ENOENT)
    return -1;
  fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd < 0)
    return -1;
  fcntl(fd, F_SETFD, FD_CLOEXEC);
  memset(&un, 0, sizeof un);
  un.sun_family = AF_UNIX;
  if (snprintf(un.sun_path, sizeof un.sun_path, "%s", path) >=
      (int)sizeof un.sun_path) {
    close(fd);
    return -1;
  }
  if (bind(fd, (struct sockaddr *)&un, sizeof un) < 0) {
    close(fd);
    return -1;
  }
  if (lstat(path, &st) < 0) {
    close(fd);
    return -1;
  }
  *dev = st.st_dev;
  *ino = st.st_ino;
  if (chmod(path, 0600) < 0 || listen(fd, 8) < 0 || nonblocking(fd) < 0) {
    close(fd);
    unlink_same(path, *dev, *ino);
    return -1;
  }
  return fd;
}

static int find_named(const char *name, struct peer *out) {
  struct peer p[128];
  int n = scan_peers(p, 128, 0), hits = 0;
  if (n < 0)
    return -1;
  for (int i = 0; i < n; i++)
    if (!strcmp(name, p[i].name) && peer_live(&p[i])) {
      *out = p[i];
      hits++;
    }
  return hits == 1 ? 0 : hits > 1 ? -2 : -1;
}

/* The marker alone identifies the sidecar: the record name follows the
   Codex thread title, not DCH_SESSION. */
static int find_source(const char *marker, struct peer *out) {
  struct peer p[128];
  int n = scan_peers(p, 128, 0), hits = 0;
  if (n < 0)
    return -1;
  for (int i = 0; i < n; i++)
    if (!strcmp(marker, p[i].marker)) {
      *out = p[i];
      hits++;
    }
  return hits == 1 ? 0 : -1;
}

/* Liveness without ps: only ESRCH proves the process is gone. EPERM means
   it exists but is off limits, which is what the Codex sandbox returns for
   every foreign pid. */
static int pid_gone(long pid) {
  return kill((pid_t)pid, 0) < 0 && errno == ESRCH;
}

/* verify_ps=0 is for the CLI inside the Codex sandbox, where /bin/ps and
   signals to other processes are blocked. */
static int peer_list_json(char *out, size_t cap, int verify_ps) {
  struct peer peers[128];
  int count = scan_peers(peers, 128, verify_ps);
  size_t used = 0;
  if (count < 0 || cap < 3)
    return -1;
  out[used++] = '[';
  for (int i = 0; i < count; i++) {
    char name[3100], session[800];
    int n;
    if (!verify_ps && pid_gone(peers[i].pid))
      continue;
    if (json_quote(name, sizeof name, peers[i].name) == SIZE_MAX ||
        json_quote(session, sizeof session, peers[i].session_id) == SIZE_MAX)
      return -1;
    n = snprintf(out + used, cap - used, "%s{\"name\":%s,\"session_id\":%s}",
                 used > 1 ? "," : "", name, session);
    if (n < 0 || (size_t)n >= cap - used)
      return -1;
    used += (size_t)n;
  }
  if (used + 3 > cap)
    return -1;
  out[used++] = ']';
  out[used++] = '\n';
  out[used] = '\0';
  return 0;
}

static void local_result(int fd, const char *status, const char *detail) {
  char b[700], q[520];
  int n;
  if (json_quote(q, sizeof q, detail ? detail : "") == SIZE_MAX)
    return;
  n = snprintf(b, sizeof b, "{\"status\":\"%s\",\"detail\":%s}\n", status, q);
  if (n < 0 || n >= (int)sizeof b)
    return;
  write_all(fd, b, (size_t)n);
}

static int receipt_status(const char *status) {
  return !strcmp(status, "delivered") || !strcmp(status, "refused") ||
         !strcmp(status, "dropped") || !strcmp(status, "held") ||
         !strcmp(status, "denied") || !strcmp(status, "expired");
}

/* Fire and forget: Claude 2.1.x emits no peer_message_status for messages it
   accepts, so a receipt wait only stalls the caller and invites resends. The
   frame is written, "sent" is reported, and the main loop continues. */
/* Claude only accepts a from-name it can reproduce byte for byte: no quotes
   or angle brackets, no control bytes, trimmed, at most 64 characters. A
   name that cannot meet that is left out and Claude falls back to the
   socket name. */
static void wrapper_name(const char *name, char *out, size_t cap) {
  size_t used = 0, chars = 0;
  for (; *name && used + 1 < cap; name++) {
    unsigned char c = (unsigned char)*name;
    if (c < 0x20 || c == 0x7f || c == '"' || c == '<' || c == '>')
      continue;
    if (used == 0 && c == ' ')
      continue;
    out[used++] = (char)c;
    chars += (c & 0xC0) != 0x80;
  }
  while (used && out[used - 1] == ' ')
    used--;
  out[used] = '\0';
  if (chars > 64 || *name)
    out[0] = '\0';
}

/* The body is wrapped the way Claude wraps its own peer messages, so the
   receiving session renders it as a one-line "@ sender" preview instead of
   the verbose unwrapped-peer framing. */
static int outgoing(int local, const char *payload, const char *own_socket,
                    const char *own_name) {
  char target[512], message[MESSAGE_MAX + 1], msgid[37], address[108],
      qmsg[MESSAGE_MAX * 6 + 3], qid[80], qfrom[700], qsess[800],
      frame[FRAME_MAX + 1], wrapped[MESSAGE_MAX + 900], safe[700];
  struct peer peer;
  int fd, n;
  if (json_string(payload, "target", target, sizeof target, 1) < 0 ||
      json_string(payload, "message", message, sizeof message, 1) < 0 ||
      !message[0]) {
    local_result(local, "refused", "target must name one live peer");
    return -1;
  }
  n = find_named(target, &peer);
  if (n < 0) {
    local_result(local, "refused",
                 n == -2 ? "target name is ambiguous"
                         : "target must name one live peer");
    return -1;
  }
  wrapper_name(own_name, safe, sizeof safe);
  if (snprintf(address, sizeof address, "uds:%s", own_socket) >=
          (int)sizeof address ||
      snprintf(wrapped, sizeof wrapped,
               "<cross-session-message from=\"%s\"%s%s%s>\n%s\n"
               "</cross-session-message>",
               address, safe[0] ? " from-name=\"" : "", safe,
               safe[0] ? "\"" : "", message) >= (int)sizeof wrapped ||
      make_uuid(msgid) < 0 ||
      json_quote(qmsg, sizeof qmsg, wrapped) == SIZE_MAX ||
      json_quote(qid, sizeof qid, msgid) == SIZE_MAX ||
      json_quote(qfrom, sizeof qfrom, address) == SIZE_MAX ||
      json_quote(qsess, sizeof qsess, peer.session_id) == SIZE_MAX) {
    local_result(local, "refused", "message encoding failed");
    return -1;
  }
  n = snprintf(
      frame, sizeof frame,
      "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":%s},\"msg_"
      "id\":%s,\"from\":%s,\"session_id\":%s,\"priority\":\"next\"}",
      qmsg, qid, qfrom, qsess);
  if (n < 0 || n >= (int)sizeof frame) {
    local_result(local, "refused",
                 "encoded message exceeds native frame limit");
    return -1;
  }
  if ((fd = connect_unix(peer.socket, 10000)) < 0 ||
      send_frames(fd, peer.token, frame) < 0) {
    if (fd >= 0)
      close(fd);
    local_result(local, "refused", "peer connection failed");
    return -1;
  }
  close(fd);
  local_result(local, "sent", "message written to peer");
  return 0;
}

static void handle_connection(int fd, const char *token,
                              const char *own_socket, const char *session,
                              const char *home, const char *sess,
                              const char *marker, const char *own_name,
                              struct pending_queue *queue) {
  char p[FRAME_MAX + 1], type[40], role[20], content[MESSAGE_MAX + 1], id[257],
      from[300], sid[128], fresh[128];
  struct peer sender;
  if (auth_conn(fd, token, p, sizeof p) < 0 ||
      json_string(p, "type", type, sizeof type, 1) < 0)
    return;
  if (!strcmp(type, "dch_send")) {
    outgoing(fd, p, own_socket, own_name);
    return;
  }
  if (strcmp(type, "user") ||
      json_nested_string(p, "message", "role", role, sizeof role) < 0 ||
      strcmp(role, "user") ||
      json_nested_string(p, "message", "content", content, sizeof content) <
          0 ||
      !content[0] || json_string(p, "msg_id", id, sizeof id, 1) < 0 ||
      json_string(p, "from", from, sizeof from, 1) < 0 ||
      json_has(p, "attachments") || json_attachments_present(p))
    return;
  if (json_has(p, "session_id") &&
      (json_string(p, "session_id", sid, sizeof sid, 1) < 0 ||
       strcmp(sid, session)))
    return;
  if (peer_by_socket(from, &sender) < 0)
    return;
  if (dch_codex_snapshot_id(home, sess, marker, fresh, sizeof fresh, 1) < 0) {
    send_receipt(&sender, own_socket, id, "refused");
    return;
  }
  if (pending_add(queue, &sender, id, content, fresh, own_socket) == -1)
    send_receipt(&sender, own_socket, id, "refused");
}

/* The Codex thread title is what the user sees in the TUI, so it is the
   peer name when present; DCH_SESSION otherwise. */
static void peer_name(const char *home, const char *thread, const char *sess,
                      char *out, size_t cap) {
  if (dch_codex_thread_name(home, thread, out, cap) < 0)
    snprintf(out, cap, "%s", sess);
}

static int record_json(char *out, size_t cap, const char *quuid,
                       long long started, const char *qproc,
                       const char *qsocket, const char *qname,
                       const char *qmarker, long long name_since) {
  long long now = (long long)time(NULL) * 1000;
  return snprintf(out, cap,
                  "{\"pid\":%ld,\"sessionId\":%s,\"cwd\":\"\",\"startedAt\":%lld,"
                  "\"procStart\":%s,\"version\":\"dch-%s\",\"peerProtocol\":1,"
                  "\"peerFeatures\":[\"reply_across_default_dirs\"],\"kind\":"
                  "\"interactive\",\"entrypoint\":\"cli\",\"pidDomain\":\"%s\","
                  "\"messagingSocketPath\":%s,\"name\":%s,\"nameSource\":\"user\","
                  "\"dchBridgeMarker\":%s,\"nameSince\":%lld,\"status\":\"idle\","
                  "\"updatedAt\":%lld,\"statusUpdatedAt\":%lld}\n",
                  (long)getpid(), quuid, started, qproc, DCH_VERSION,
#ifdef __APPLE__
                  "darwin",
#else
                  "linux",
#endif
                  qsocket, qname, qmarker, name_since, now, now) < (int)cap
             ? 0
             : -1;
}

static void bridge_sidecar(int life_fd, const char *codex) {
  char home[1200], sessions[1200], socket_dir[80], socket_path[104],
      record[1400], key[1400], hash[65], proc[128], thread[128], uuid[37],
      token[65], key_body[2400], record_body[7000];
  char qtoken[400], qproc[800], quuid[230], qsocket[700], qname[3100],
      qmarker[800], name[700];
  const char *sess = getenv("DCH_SESSION"), *marker = getenv(BRIDGE_MARKER);
  unsigned char raw[16]; /* Claude's key schema: peerToken is exactly 32 hex */
  int server = -1;
  struct pending_queue queue = {.error_fd = -1, .child = -1};
  dev_t sockdev = 0, keydev = 0, recdev = 0;
  ino_t sockino = 0, keyino = 0, recino = 0;
  long long now, name_check_at = 0;
  sidecar_life_fd = life_fd;
  if (!sess || !*sess || !marker || !*marker || sha_selftest() < 0 ||
      codex_home(home, sizeof home) < 0 ||
      claude_sessions(sessions, sizeof sessions, 1) < 0 ||
      process_start((long)getpid(), proc, sizeof proc) < 0 ||
      random_bytes(raw, sizeof raw) < 0)
    return;
  for (size_t i = 0; i < sizeof raw; i++)
    snprintf(token + i * 2, sizeof token - i * 2, "%02x", raw[i]);
  if (make_uuid(uuid) < 0)
    return;
  if (snprintf(socket_dir, sizeof socket_dir, "/tmp/dch-agent-%u",
               (unsigned)getuid()) >= (int)sizeof socket_dir ||
      snprintf(socket_path, sizeof socket_path, "%s/%ld.sock", socket_dir,
               (long)getpid()) >= (int)sizeof socket_path)
    return;
  if ((server = server_socket(socket_path, &sockdev, &sockino)) < 0)
    return;
  sha_hex(socket_path, strlen(socket_path), hash);
  if (snprintf(record, sizeof record, "%s/%ld.json", sessions,
               (long)getpid()) >= (int)sizeof record ||
      snprintf(key, sizeof key, "%s/%ld.%s.key", sessions, (long)getpid(),
               hash) >= (int)sizeof key) {
    close(server);
    unlink_same(socket_path, sockdev, sockino);
    return;
  }
  if (json_quote(qtoken, sizeof qtoken, token) == SIZE_MAX ||
      json_quote(qproc, sizeof qproc, proc) == SIZE_MAX ||
      json_quote(quuid, sizeof quuid, uuid) == SIZE_MAX ||
      json_quote(qsocket, sizeof qsocket, socket_path) == SIZE_MAX ||
      json_quote(qmarker, sizeof qmarker, marker) == SIZE_MAX) {
    close(server);
    unlink_same(socket_path, sockdev, sockino);
    return;
  }
  if (snprintf(key_body, sizeof key_body,
               "{\"peerToken\":%s,\"procStart\":%s,\"pidDomain\":\"%s\"}\n",
               qtoken, qproc,
#ifdef __APPLE__
               "darwin"
#else
               "linux"
#endif
               ) >= (int)sizeof key_body) {
    close(server);
    unlink_same(socket_path, sockdev, sockino);
    return;
  }
  /* Do not publish until this incarnation owns exactly one thread. */
  while (!sidecar_stop && dch_codex_snapshot_id(home, sess, marker, thread,
                                                sizeof thread, 1) < 0) {
    struct pollfd p = {life_fd, POLLIN | POLLHUP, 0};
    int r = poll(&p, 1, 250);
    if (r > 0) {
      close(server);
      unlink_same(socket_path, sockdev, sockino);
      return;
    }
    if (r < 0 && errno != EINTR) {
      close(server);
      unlink_same(socket_path, sockdev, sockino);
      return;
    }
  }
  if (sidecar_stop) {
    close(server);
    unlink_same(socket_path, sockdev, sockino);
    return;
  }
  now = (long long)time(NULL) * 1000;
  peer_name(home, thread, sess, name, sizeof name);
  if (json_quote(qname, sizeof qname, name) == SIZE_MAX ||
      record_json(record_body, sizeof record_body, quuid, now, qproc, qsocket,
                  qname, qmarker, now) < 0) {
    close(server);
    unlink_same(socket_path, sockdev, sockino);
    return;
  }
  if (atomic_file(key, key_body, 0600, &keydev, &keyino) < 0 ||
      atomic_file(record, record_body, 0644, &recdev, &recino) < 0) {
    unlink_same(key, keydev, keyino);
    close(server);
    unlink_same(socket_path, sockdev, sockino);
    return;
  }
  /* ponytail: one serial listener bounds subprocesses and memory. Add a
  ** worker pool only if measured native-message throughput requires it. */
  for (; !sidecar_stop;) {
    struct pollfd p[3] = {{life_fd, POLLIN | POLLHUP, 0},
                          {server, POLLIN, 0},
                          {queue.error_fd, POLLIN | POLLHUP, 0}};
    long long tick_now, next = 0;
    int timeout = -1, count;
    pending_tick(&queue, codex, home, sess, marker, socket_path);
    p[2].fd = queue.error_fd;
    count = queue.error_fd >= 0 ? 3 : 2;
    if (monotonic_ms(&tick_now) == 0 && queue.count) {
      next = queue.child > 0
                 ? (queue.error_fd < 0 && tick_now + 50 < queue.child_deadline
                        ? tick_now + 50
                        : queue.child_deadline)
                 : queue.retry_at;
      if (!next || next <= tick_now)
        timeout = 0;
      else
        timeout = next - tick_now > INT_MAX ? INT_MAX : (int)(next - tick_now);
    }
    if (timeout < 0 || timeout > 5000)
      timeout = 5000;
    int r = poll(p, count, timeout);
    if (r < 0 && errno == EINTR)
      continue;
    if (r < 0 || p[0].revents)
      break;
    if (monotonic_ms(&tick_now) == 0 && tick_now >= name_check_at) {
      char fresh[700];
      name_check_at = tick_now + 5000;
      peer_name(home, thread, sess, fresh, sizeof fresh);
      if (strcmp(fresh, name) &&
          json_quote(qname, sizeof qname, fresh) != SIZE_MAX &&
          record_json(record_body, sizeof record_body, quuid, now, qproc,
                      qsocket, qname, qmarker,
                      (long long)time(NULL) * 1000) == 0) {
        /* atomic_file is create-only, so drop our own record first. */
        unlink_same(record, recdev, recino);
        if (atomic_file(record, record_body, 0644, &recdev, &recino) == 0)
          snprintf(name, sizeof name, "%s", fresh);
        else
          break;
      }
    }
    if (p[1].revents & POLLIN) {
      int c = accept(server, NULL, NULL);
      if (c >= 0) {
        if (nonblocking(c) == 0)
          handle_connection(c, token, socket_path, uuid, home, sess, marker,
                            name, &queue);
        close(c);
      }
    }
    pending_tick(&queue, codex, home, sess, marker, socket_path);
  }
  if (queue.child > 0) {
    terminate_child(queue.child);
    if (queue.error_fd >= 0)
      close(queue.error_fd);
    queue.child = -1;
    queue.error_fd = -1;
  }
  if (queue.count && monotonic_ms(&now) == 0) {
    request_deadline = now + 1200;
    sidecar_life_fd = -1;
    sidecar_stop = 0;
    while (queue.count) {
      struct pending_message *m = &queue.items[queue.head];
      send_receipt(&m->sender, socket_path, m->id, "dropped");
      pending_pop(&queue);
    }
  }
  unlink_same(record, recdev, recino);
  unlink_same(key, keydev, keyino);
  close(server);
  unlink_same(socket_path, sockdev, sockino);
}

int dch_bridge_agent_list(int json) {
  const char *marker = getenv(BRIDGE_MARKER);
  char output[FRAME_MAX + 1];
  struct json_parser parser;
  if (peer_list_json(output, sizeof output, !(marker && *marker)) < 0) {
    fprintf(stderr, "dch: native peer registry is ambiguous or too large\n");
    return 1;
  }
  if (json_parse(&parser, output) < 0 || parser.tok[0].kind != JSON_ARRAY)
    return 1;
  for (int i = 1; i < parser.tok[0].next;) {
    char name[512], session[128];
    int name_field, session_field;
    if (parser.tok[i].parent != 0 || parser.tok[i].kind != JSON_OBJECT ||
        (name_field = json_field(&parser, i, "name")) < 0 ||
        (session_field = json_field(&parser, i, "session_id")) < 0 ||
        json_decode(&parser, name_field, name, sizeof name) < 0 || !name[0] ||
        json_decode(&parser, session_field, session, sizeof session) < 0 ||
        !session[0])
      return 1;
    i = parser.tok[i].next;
  }
  if (json) {
    puts(output);
    return 0;
  }
  for (int i = 1; i < parser.tok[0].next;) {
    char name[512];
    int field = json_field(&parser, i, "name");
    if (field < 0 || json_decode(&parser, field, name, sizeof name) < 0)
      return 1;
    puts(name);
    i = parser.tok[i].next;
  }
  return 0;
}

int dch_bridge_agent_send(const char *name, int argc, char **argv) {
  const char *sess = getenv("DCH_SESSION"), *marker = getenv(BRIDGE_MARKER);
  struct peer source;
  char *message, *p, frame[FRAME_MAX + 1], qt[3100], qm[MESSAGE_MAX * 6 + 3],
      line[800], status[40], detail[520];
  size_t len = 0;
  int fd, n;
  if (!sess || !*sess || !marker || !*marker) {
    fprintf(stderr,
            "dch: --agent-send must run inside a bridged Codex session\n");
    return 1;
  }
  if (strlen(name) >= sizeof source.name) {
    fprintf(stderr, "dch: peer name is too long\n");
    return 1;
  }
  for (int i = 0; i < argc; i++) {
    size_t z = strlen(argv[i]), space = i ? 1 : 0;
    if (len > MESSAGE_MAX || space > MESSAGE_MAX - len ||
        z > MESSAGE_MAX - len - space) {
      fprintf(stderr, "dch: message is too long\n");
      return 1;
    }
    len += z + space;
  }
  if (!len) {
    fprintf(stderr, "dch: message is empty\n");
    return 1;
  }
  message = malloc(len + 1);
  if (!message)
    return 1;
  p = message;
  for (int i = 0; i < argc; i++) {
    if (i)
      *p++ = ' ';
    memcpy(p, argv[i], strlen(argv[i]));
    p += strlen(argv[i]);
  }
  *p = '\0';
  if (find_source(marker, &source) < 0) {
    free(message);
    fprintf(stderr,
            "dch: no unique live source sidecar for DCH_SESSION=%s; start a "
            "fresh dch Codex session\n",
            sess);
    return 1;
  }
  if (json_quote(qt, sizeof qt, name) == SIZE_MAX ||
      json_quote(qm, sizeof qm, message) == SIZE_MAX) {
    free(message);
    fprintf(stderr, "dch: message encoding failed\n");
    return 1;
  }
  free(message);
  n = snprintf(frame, sizeof frame,
               "{\"type\":\"dch_send\",\"target\":%s,\"message\":%s}", qt, qm);
  if (n < 0 || n >= (int)sizeof frame) {
    fprintf(stderr, "dch: encoded message exceeds native frame limit\n");
    return 1;
  }
  if ((fd = connect_unix(source.socket, 10000)) < 0 ||
      send_frames(fd, source.token, frame) < 0 ||
      recv_line(fd, line, sizeof line, 15000) < 0) {
    if (fd >= 0)
      close(fd);
    fprintf(stderr, "dch: native send failed\n");
    return 1;
  }
  close(fd);
  if (json_string(line, "status", status, sizeof status, 1) < 0 ||
      json_string(line, "detail", detail, sizeof detail, 1) < 0) {
    fprintf(stderr, "dch: invalid source-sidecar response\n");
    return 1;
  }
  if (strcmp(status, "sent")) {
    fprintf(stderr, "dch: message %s: %s\n", status, detail);
    return 1;
  }
  puts(status);
  return 0;
}

#ifdef DCH_BRIDGE_SELFTEST
int dch_codex_thread_name(const char *home, const char *id, char *out,
                          size_t outsz) {
  (void)home;
  (void)id;
  (void)out;
  (void)outsz;
  return -1;
}
int dch_visit_claude_records(dch_claude_record_fn visit, void *arg) {
  (void)visit;
  (void)arg;
  return 0;
}
int dch_codex_snapshot_id(const char *home, const char *sess,
                          const char *marker, char *out, size_t outsz,
                          int strict) {
  (void)home;
  (void)sess;
  (void)marker;
  (void)out;
  (void)outsz;
  (void)strict;
  return -1;
}

int main(void) {
  static const char thread[] = "123e4567-e89b-12d3-a456-426614174000";
  char out[32], tiny[4], ready[512];
  int life[2], status;
  pid_t child;
  {
    char *wrapped[] = {"headroom", "wrap", "/opt/homebrew/bin/codex", NULL};
    char *plain[] = {"vim", "notes.txt", NULL};
    if (!is_codex(wrapped) || is_codex(plain))
      return 20;
    char safe[200], accented[131];
    wrapper_name("  <us\"er>\x01 3 ", safe, sizeof safe);
    if (strcmp(safe, "user 3"))
      return 21;
    for (int i = 0; i < 65; i++)
      memcpy(accented + 2 * i, "\xC3\xA9", 2);
    accented[128] = '\0';
    wrapper_name(accented, safe, sizeof safe);
    if (strcmp(safe, accented))
      return 22;
    memcpy(accented + 128, "\xC3\xA9", 3);
    wrapper_name(accented, safe, sizeof safe);
    if (safe[0])
      return 23;
  }
  if (sha_selftest() < 0)
    return 1;
  if (json_string("{\"type\":\"auth\",\"unknown\":{\"type\":\"x\"}}", "type",
                  out, sizeof out, 1) < 0 ||
      strcmp(out, "auth"))
    return 2;
  if (json_string("{\"type\":\"a\",\"type\":1}", "type", out, sizeof out, 1) ==
      0)
    return 3;
  if (json_string("{\"v\":\"\\ud83d\\ude00\"}", "v", out, sizeof out, 1) < 0 ||
      strlen(out) != 4)
    return 4;
  if (json_string("{\"v\":1}", "v", out, sizeof out, 1) == 0)
    return 5;
  if (json_quote(tiny, sizeof tiny, "abcd") != SIZE_MAX)
    return 6;
  snprintf(ready, sizeof ready,
           "Error: failed to queue session message: thread/queue/add failed: "
           "failed to read thread: invalid thread-store request: no rollout "
           "found for thread id %s (code -32603)\n",
           thread);
  if (!queue_not_ready(ready, thread) ||
      !queue_not_ready("no rollout found for thread id "
                       "123e4567-e89b-12d3-a456-426614174000\n",
                       thread) ||
      (strcat(ready, "trailing"), !queue_not_ready(ready, thread)) ||
      queue_not_ready("Error: something else entirely\n", thread) ||
      queue_not_ready(
          "Error: failed to queue session message: thread/queue/add failed: "
          "failed to read thread: invalid thread-store request: no rollout "
          "found for thread id 123e4567-e89b-12d3-a456-426614174001 "
          "(code -32603)\n",
          thread))
    return 7;
  if (pipe(life) < 0 || (child = fork()) < 0)
    return 8;
  if (child == 0) {
    close(life[0]);
    close(life[1]);
    for (;;)
      pause();
  }
  close(life[1]);
  terminate_child(child);
  close(life[0]);
  errno = 0;
  if (waitpid(child, &status, WNOHANG) != -1 || errno != ECHILD)
    return 9;
  puts("bridge core self-check: ok");
  return 0;
}
#endif
