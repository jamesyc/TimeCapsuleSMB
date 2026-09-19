#include "config.h"

static int config_append(char *out, size_t out_len, size_t *used, char ch) {
    if (*used + 1 >= out_len) {
        return -1;
    }
    out[(*used)++] = ch;
    out[*used] = '\0';
    return 0;
}

int config_decode_assignment_value(const char *text, char *out, size_t out_len) {
    size_t used = 0;
    const char *p = text;

    if (out_len == 0) {
        return -1;
    }
    out[0] = '\0';
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    /* Concatenated fragments: bare words, '...' and "..." segments, exactly
     * as shlex.quote emits them. A bare `$`, backtick or backslash would need
     * shell evaluation; shlex.quote never emits them bare, so reject. */
    for (;;) {
        if (*p == '\'') {
            p++;
            while (*p != '\'') {
                if (*p == '\0' || config_append(out, out_len, &used, *p) != 0) {
                    out[0] = '\0';
                    return -1;
                }
                p++;
            }
            p++;
        } else if (*p == '"') {
            p++;
            while (*p != '"') {
                char ch = *p;
                if (ch == '\0') {
                    out[0] = '\0';
                    return -1;
                }
                if (ch == '\\' && (p[1] == '"' || p[1] == '\\' || p[1] == '$' || p[1] == '`')) {
                    ch = p[1];
                    p++;
                } else if (ch == '$' || ch == '`') {
                    out[0] = '\0';
                    return -1;
                }
                if (config_append(out, out_len, &used, ch) != 0) {
                    out[0] = '\0';
                    return -1;
                }
                p++;
            }
            p++;
        } else if (*p == '\0' || *p == ' ' || *p == '\t' || *p == '\r' || *p == '\n' || *p == '#') {
            break;
        } else {
            if (*p == '$' || *p == '`' || *p == '\\' || *p == ';' || *p == '&' || *p == '|' ||
                *p == '<' || *p == '>' || *p == '(' || *p == ')' ||
                config_append(out, out_len, &used, *p) != 0) {
                out[0] = '\0';
                return -1;
            }
            p++;
        }
    }
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    if (*p != '\0' && *p != '\r' && *p != '\n' && *p != '#') {
        out[0] = '\0';
        return -1;
    }
    return 0;
}

int config_read_value(const char *path, const char *key, char *out, size_t out_len) {
    FILE *fp;
    char line[1024];
    size_t key_len = strlen(key);
    int found = 1;

    if (out_len == 0) {
        return -1;
    }
    out[0] = '\0';
    fp = fopen(path, "r");
    if (fp == NULL) {
        return -1;
    }
    while (fgets(line, sizeof(line), fp) != NULL) {
        char *cursor = line;
        while (*cursor == ' ' || *cursor == '\t') {
            cursor++;
        }
        if (strncmp(cursor, key, key_len) != 0) {
            continue;
        }
        cursor += key_len;
        while (*cursor == ' ' || *cursor == '\t') {
            cursor++;
        }
        if (*cursor != '=') {
            continue;
        }
        cursor++;
        /* Last assignment wins, like the shell that also sources the file. */
        found = config_decode_assignment_value(cursor, out, out_len);
    }
    if (ferror(fp)) found = -1;
    (void)fclose(fp);
    return found;
}

int config_read_snapshot(const char *path, struct config_item *items, size_t count) {
    FILE *fp;
    char line[1024];
    size_t i;
    for (i = 0; i < count; i++) { items[i].value[0] = '\0'; items[i].present = 0; }
    fp = fopen(path, "r");
    if (fp == NULL) return -1;
    while (fgets(line, sizeof(line), fp) != NULL) {
        char *cursor = line;
        while (*cursor == ' ' || *cursor == '\t') cursor++;
        for (i = 0; i < count; i++) {
            size_t key_len = strlen(items[i].key);
            char *value;
            if (strncmp(cursor, items[i].key, key_len) != 0) continue;
            value = cursor + key_len;
            while (*value == ' ' || *value == '\t') value++;
            if (*value++ != '=') continue;
            if (config_decode_assignment_value(value, items[i].value, sizeof(items[i].value)) != 0) {
                fclose(fp); return -1;
            }
            items[i].present = 1;
        }
    }
    if (ferror(fp)) { fclose(fp); return -1; }
    return fclose(fp) == 0 ? 0 : -1;
}

int config_bool_value(const char *text, int fallback) {
    if (text == NULL) {
        return fallback;
    }
    if (!strcmp(text, "1") || !strcasecmp(text, "true") || !strcasecmp(text, "yes")) {
        return 1;
    }
    if (!strcmp(text, "0") || !strcasecmp(text, "false") || !strcasecmp(text, "no")) {
        return 0;
    }
    return fallback;
}
