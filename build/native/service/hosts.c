#include "service.h"

int tc_hosts_ensure(const char *path, const char *hostname) {
    char local[272], line[1024];
    FILE *file;
    int error;
    if (!hostname || !*hostname || strlen(hostname) > 255 ||
        strspn(hostname, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-") !=
            strlen(hostname))
        return -1;
    snprintf(local, sizeof(local), "%s.local", hostname);
    file = fopen(path, "r");
    if (file) {
        while (fgets(line, sizeof(line), file)) {
            char *word, *save, *comment = strchr(line, '#');
            if (comment)
                *comment = 0;
            word = strtok_r(line, " \t\r\n", &save); /* address */
            while (word && (word = strtok_r(NULL, " \t\r\n", &save))) {
                if (!strcmp(word, hostname) || !strcmp(word, local)) {
                    fclose(file);
                    return 0;
                }
            }
        }
        error = ferror(file);
        fclose(file);
        if (error)
            return -1;
    } else if (errno != ENOENT)
        return -1;
    /* Preserve the old manager's local resolver repair. Keep any entry Apple
     * already installed; this shared OS file must be visible to Samba's libc
     * resolver, so a manager-local variable cannot serve the same purpose. */
    file = fopen(path, "a");
    if (!file)
        return -1;
    error = fprintf(file, "\n127.0.0.1\t%s %s\n", hostname, local) < 0;
    if (fclose(file))
        error = 1;
    return error ? -1 : 0;
}
