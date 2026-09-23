#include "inspect.h"
#include "../common/worker.h"
#include "../samba/runtime.h"
#ifndef TC_PS_PATH
#define TC_PS_PATH "/bin/ps"
#endif

static int argument(const char *command, const char *text) {
    size_t length = strlen(text);
    const char *found = command;
    while ((found = strstr(found, text)) != NULL) {
        if ((found == command || isspace((unsigned char)found[-1])) &&
            (!found[length] || isspace((unsigned char)found[length])))
            return 1;
        found++;
    }
    return 0;
}
static int starts_with_arguments(const char *command, const char *prefix) {
    size_t length = strlen(prefix);
    return !strncmp(command, prefix, length) && (!command[length] || isspace((unsigned char)command[length]));
}
static enum tc_process_role classify(const char *name, const char *command) {
    if (!strcmp(name, "smbd"))
        return TC_PROC_SMBD;
    if (!strcmp(name, "rsync"))
        return TC_PROC_RSYNC;
    if (!strcmp(name, "wcifsfs"))
        return TC_PROC_WCIFSFS;
    if (!strcmp(name, "wcifsnd"))
        return TC_PROC_WCIFSND;
    if (!strcmp(name, "diskd"))
        return argument(command, "-i lo0") ? TC_PROC_DISKD_LOOPBACK : TC_PROC_DISKD;
    if (!strcmp(name, "discoveryd") && !argument(command, "--print-link-plan") &&
        !argument(command, "--print-mast") && !argument(command, "--version"))
        return TC_PROC_DISCOVERY;
    if (!strcmp(name, "telemetry") && argument(command, "--daemon"))
        return TC_PROC_TELEMETRY;
    if (!strcmp(name, "service")) {
        if (starts_with_arguments(command, "service: role=discovery") ||
            (starts_with_arguments(command, TC_SERVICE_BIN " discovery") &&
             (argument(command, "--diskless") || argument(command, "--netbios-name"))))
            return TC_PROC_DISCOVERY;
        if (starts_with_arguments(command, "service: role=telemetry") ||
            starts_with_arguments(command, TC_SERVICE_BIN " telemetry --daemon"))
            return TC_PROC_TELEMETRY;
    }
    /* Apple's mDNSResponder and afpserver are deliberately never managed here.
     * AFP preference controls only discovery; killing either breaks OEM work. */
    return TC_PROC_OTHER;
}
int tc_process_table_parse(struct tc_process_table *table, const char *text) {
    memset(table, 0, sizeof(*table));
    while (*text) {
        char line[2048], state[32], name[64];
        const char *end = strchr(text, '\n');
        int pid, parent, group, offset = 0;
        size_t length = end ? (size_t)(end - text) : strlen(text);
        if (length >= sizeof(line))
            return -1;
        memcpy(line, text, length);
        line[length] = 0;
        text += length + (end != NULL);
        if (!length)
            continue;
        if (sscanf(line, "%d %d %d %31s %63s %n", &pid, &parent, &group, state, name, &offset) != 5 ||
            !offset || pid < 0 || parent < 0 || group < 0)
            return -1;
        if (pid <= 1 || strchr(state, 'Z'))
            continue;
        enum tc_process_role role = classify(name, line + offset);
        if (role == TC_PROC_OTHER)
            continue;
        if (table->count == TC_PROCESS_MAX)
            return -1;
        struct tc_process_info *p = &table->processes[table->count++];
        p->pid = pid;
        p->parent = parent;
        p->group = group;
        p->role = role;
    }
    return 0;
}
int tc_process_table_read(struct tc_process_table *table) {
    char *buffer = malloc(65536);
    char *argv[] = {TC_PS_PATH, "axww",  "-o", "pid=",   "-o", "ppid=",    "-o", "pgid=",
                    "-o",       "stat=", "-o", "ucomm=", "-o", "command=", NULL};
    int rc;
    if (!buffer)
        return -1;
    rc = tc_command_capture(argv, buffer, 65536, 5);
    if (!rc)
        rc = tc_process_table_parse(table, buffer);
    free(buffer);
    return rc;
}
int tc_listener_present(const char *text, unsigned port) {
    while (*text) {
        const char *end = strchr(text, '\n');
        size_t length = end ? (size_t)(end - text) : strlen(text);
        char line[2048], needle[32];
        if (length < sizeof(line)) {
            memcpy(line, text, length);
            line[length] = 0;
            snprintf(needle, sizeof(needle), ":%u", port);
            const char *address = strrchr(line, ':');
            /* fstat also lists connected client sockets. A remote endpoint
             * does not prove that the parent still owns a listener. */
            if (address && !strncmp(address, needle, strlen(needle)) &&
                (!address[strlen(needle)] || isspace((unsigned char)address[strlen(needle)])) &&
                !strstr(line, "<->") && !strstr(line, "-->")) {
                if (strstr(line, " internet stream tcp ") || strstr(line, " internet6 stream tcp "))
                    return 1;
            }
        }
        text += length + (end != NULL);
    }
    return 0;
}
unsigned tc_wildcard_listener_families(const char *text, unsigned port) {
    unsigned result = 0;
    while (*text) {
        const char *end = strchr(text, '\n');
        size_t length = end ? (size_t)(end - text) : strlen(text);
        char line[2048], wildcard[32], ipv4[32], ipv6[32], ipv6_wildcard[32], *endpoint;
        if (length >= sizeof(line)) {
            text += length + (end != NULL);
            continue;
        }
        memcpy(line, text, length);
        line[length] = 0;
        text += length + (end != NULL);
        if (strstr(line, "<->") || strstr(line, "-->"))
            continue;
        endpoint = line + strlen(line);
        while (endpoint > line && isspace((unsigned char)endpoint[-1]))
            *--endpoint = 0;
        while (endpoint > line && !isspace((unsigned char)endpoint[-1]))
            endpoint--;
        snprintf(wildcard, sizeof(wildcard), "*:%u", port);
        snprintf(ipv4, sizeof(ipv4), "0.0.0.0:%u", port);
        snprintf(ipv6, sizeof(ipv6), "[::]:%u", port);
        snprintf(ipv6_wildcard, sizeof(ipv6_wildcard), "[*]:%u", port);
        if (strcmp(endpoint, wildcard) && strcmp(endpoint, ipv4) && strcmp(endpoint, ipv6) &&
            strcmp(endpoint, ipv6_wildcard))
            continue;
        if (strstr(line, " internet stream tcp "))
            result |= 1;
        if (strstr(line, " internet6 stream tcp "))
            result |= 2;
    }
    return result;
}
static int process_fstat(pid_t pid, char *buffer, size_t buffer_size) {
    char number[32];
    char *argv[] = {TC_FSTAT_PATH, "-p", number, NULL};
    snprintf(number, sizeof(number), "%ld", (long)pid);
    return tc_command_capture(argv, buffer, buffer_size, 5);
}
int tc_native_nbns_sockets_present(const char *text, pid_t pid, unsigned control_port) {
    unsigned found = 0;
    while (*text) {
        const char *end = strchr(text, '\n');
        size_t length = end ? (size_t)(end - text) : strlen(text);
        char line[2048], *endpoint;
        long owner;
        int offset = 0;
        if (length >= sizeof(line)) return 0;
        memcpy(line, text, length);
        line[length] = 0;
        text += length + (end != NULL);
        if (sscanf(line, "%*s %*s %ld %*s %n", &owner, &offset) != 1 ||
            !offset || owner != (long)pid || !strstr(line + offset, "internet dgram udp ") ||
            strstr(line, "<->") || strstr(line, "-->")) continue;
        endpoint = line + strlen(line);
        while (endpoint > line && isspace((unsigned char)endpoint[-1])) *--endpoint = 0;
        while (endpoint > line && !isspace((unsigned char)endpoint[-1])) endpoint--;
        if (!strcmp(endpoint, "*:137") || !strcmp(endpoint, "0.0.0.0:137")) found |= 1;
        if (!strcmp(endpoint, "*:138") || !strcmp(endpoint, "0.0.0.0:138")) found |= 2;
        char control[32];
        snprintf(control, sizeof(control), "*:%u", control_port);
        if (!strcmp(endpoint, control)) found |= 4;
    }
    return found == 7;
}
int tc_process_listener(pid_t pid, unsigned port, int *listening) {
    char buffer[32768];
    if (process_fstat(pid, buffer, sizeof(buffer)))
        return -1;
    *listening = tc_listener_present(buffer, port);
    return 0;
}
int tc_process_wildcard_listeners(pid_t pid, unsigned port, unsigned *families) {
    char buffer[32768];
    if (process_fstat(pid, buffer, sizeof(buffer)))
        return -1;
    *families = tc_wildcard_listener_families(buffer, port);
    return 0;
}
