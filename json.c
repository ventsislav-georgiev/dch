/*
** JSON and UTF-8: the tokenizer, string decoding and quoting that bridge.c,
** serve.c and the dch client for remote hosts (remote.c) share. Moved here
** from bridge.c, unchanged, so the Windows client (win/) links it without
** the rest of bridge.c. Pure: no I/O. See bridge.h.
*/
#include <ctype.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bridge.h"

/* Length of the valid UTF-8 sequence at s (n bytes left), 0 when it is not
   one: no overlongs, surrogates or code points past U+10FFFF. */
static size_t utf8_len(const unsigned char *s, size_t n) {
  unsigned c = s[0];
  size_t more;
  uint32_t cp;
  if (c < 0x80)
    return 1;
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
  if (more >= n)
    return 0;
  for (size_t j = 1; j <= more; j++) {
    if ((s[j] & 0xc0) != 0x80)
      return 0;
    cp = (cp << 6) | (s[j] & 63);
  }
  if ((more == 2 && cp < 0x800) || (more == 3 && cp < 0x10000) ||
      cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
    return 0;
  return more + 1;
}

static int utf8_ok(const unsigned char *s, size_t n) {
  for (size_t i = 0, l; i < n; i += l)
    if (!s[i] || !(l = utf8_len(s + i, n - i)))
      return 0;
  return 1;
}

/* The token types live in bridge.h (serve.c walks documents too). Tokens
   grow with the text (a record is up to 1 MiB: a 1100-word argv is past any
   fixed cap); json_free() after every json_parse(), even a failed one.
   Nesting stays bounded so the recursion does. */
#define JSON_MAX_DEPTH 1024

static void json_free(struct json_parser *p) {
  free(p->tok);
  p->tok = NULL;
  p->ntok = p->cap = 0;
}

static void json_space(struct json_parser *p) {
  while (p->pos < p->len && strchr(" \t\r\n", p->text[p->pos]))
    p->pos++;
}

static int json_new(struct json_parser *p, enum json_kind kind, int start,
                    int parent) {
  int n;
  if (p->ntok == p->cap) {
    struct json_token *t;
    if (p->cap > INT_MAX / 2 ||
        !(t = realloc(p->tok, (size_t)(p->cap ? p->cap * 2 : 64) * sizeof *t)))
      return -1;
    p->tok = t;
    p->cap = p->cap ? p->cap * 2 : 64;
  }
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
  int r;
  if (p->pos >= p->len)
    return -1;
  if (p->text[p->pos] == '{' || p->text[p->pos] == '[') {
    if (p->depth == JSON_MAX_DEPTH)
      return -1;
    p->depth++;
    r = p->text[p->pos] == '{' ? json_parse_object(p, parent)
                               : json_parse_array(p, parent);
    p->depth--;
    return r;
  }
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

/* Bytes that are not UTF-8 (a Linux path, an argv word, a name cut
   mid-character) are written as U+FFFD, as dch's other JSON output does: the
   result always parses, and a replaced byte reads as replaced. A \u00XX
   escape would decode to a different, valid character (0xff -> "\u00ff",
   y-diaeresis) that nothing tells apart from the real one. */
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
    else if (c < 32 || c == 0x7f) {
      snprintf(small, sizeof small, "\\u%04x", c);
      esc = small;
    } else if (c >= 0x80) {
      /* NUL-terminated, so utf8_len stops at the end on its own */
      size_t l = utf8_len((const unsigned char *)s, SIZE_MAX);
      const unsigned char *u = (const unsigned char *)s;
      if (!l)
        esc = "\\ufffd";
      else if ((l == 2 && c == 0xc2 && u[1] < 0xa0) ||
               (l == 3 && c == 0xe2 &&
                ((u[1] == 0x80 && ((u[2] >= 0xaa && u[2] <= 0xae) ||
                                   u[2] == 0x8e || u[2] == 0x8f)) ||
                 (u[1] == 0x81 && u[2] >= 0xa6 && u[2] <= 0xa9)))) {
        /* C1 controls, the bidi marks (LRM, RLM), embeddings, overrides
           and isolates as \u: the same JSON value, and inert on a
           terminal */
        snprintf(small, sizeof small, "\\u%04x",
                 l == 2 ? u[1]
                        : (c & 0x0f) << 12 | (u[1] & 0x3f) << 6 | (u[2] & 0x3f));
        esc = small;
        s += l - 1;
      } else {
        for (size_t k = 0; k < l; k++, n++)
          if (n < cap)
            out[n] = s[k];
        s += l - 1;
        continue;
      }
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

size_t dch_utf8_len(const unsigned char *s, size_t n) { return utf8_len(s, n); }

int dch_utf8_ok(const unsigned char *s, size_t n) { return utf8_ok(s, n); }

size_t dch_json_quote(char *out, size_t cap, const char *s) {
  return json_quote(out, cap, s);
}

/* The tokenizer itself, for callers that walk a document (serve.c). */
int dch_json_parse(struct json_parser *p, const char *text) {
  return json_parse(p, text);
}

void dch_json_free(struct json_parser *p) { json_free(p); }

int dch_json_field(const struct json_parser *p, int object, const char *key) {
  return json_field(p, object, key);
}

int dch_json_decode(const struct json_parser *p, int token, char *out,
                    size_t cap) {
  return json_decode(p, token, out, cap);
}
