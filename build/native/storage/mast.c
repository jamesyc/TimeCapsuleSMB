#include "mast.h"
#include "../common/acp.h"

struct parsed_part {
    char device[16];
    char name[TC_STORAGE_NAME_MAX];
    char format[16];
    char uuid[64];
};

struct parsed_disk {
    char device[16];
    int builtin;
    struct parsed_part parts[TC_MAX_VOLUMES];
    size_t count;
};

static char *trim(char *text) {
    char *end;
    while (*text && isspace((unsigned char)*text)) text++;
    end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) *--end = '\0';
    return text;
}

static void decode_value(char *out, size_t size, const char *raw) {
    const char *start = raw;
    const char *end;
    size_t used = 0;
    while (*start && isspace((unsigned char)*start)) start++;
    if (*start == '"') {
        start++;
        end = strrchr(start, '"');
        if (end == NULL) end = start + strlen(start);
    } else if (*start == '<') {
        start++;
        end = strchr(start, '>');
        if (end == NULL) end = start + strlen(start);
    } else {
        end = start + strcspn(start, ";,\r\n");
    }
    while (start < end && used + 1 < size) {
        unsigned char ch = (unsigned char)*start++;
        if (ch == '\\' && start < end && used + 1 < size) ch = (unsigned char)*start++;
        if (!isspace(ch) || out != NULL) out[used++] = (char)ch;
    }
    while (used && isspace((unsigned char)out[used - 1])) used--;
    out[used] = '\0';
}

static int safe_device(const char *value) {
    return *value && strlen(value) < 16 && strspn(value, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") == strlen(value);
}

static void format_uuid(char out[37], const char *raw) {
    char hex[33];
    size_t used = 0, i;
    for (i = 0; raw[i] && used < 32; i++)
        if (isxdigit((unsigned char)raw[i])) hex[used++] = (char)tolower((unsigned char)raw[i]);
    if (used != 32) { out[0] = '\0'; return; }
    hex[32] = '\0';
    snprintf(out, 37, "%.8s-%.4s-%.4s-%.4s-%.12s", hex, hex + 8, hex + 12, hex + 16, hex + 20);
}

static int append_disk(struct tc_inventory *inventory, const struct parsed_disk *disk) {
    size_t i;
    for (i = 0; i < disk->count; i++) {
        const struct parsed_part *part = &disk->parts[i];
        struct tc_volume *volume;
        char uuid[37];
        if (strcasecmp(part->format, "hfs") != 0) continue;
        format_uuid(uuid, part->uuid);
        if (strncmp(part->device, "dk", 2) != 0 || !isdigit((unsigned char)part->device[2]) ||
            !uuid[0] || !strcmp(uuid, "00000000-0000-0000-0000-000000000000")) continue;
        if (!safe_device(disk->device) || !safe_device(part->device) || inventory->count >= TC_MAX_VOLUMES) return -1;
        volume = &inventory->volumes[inventory->count++];
        memset(volume, 0, sizeof(*volume));
        strcpy(volume->disk, disk->device); strcpy(volume->device, part->device);
        snprintf(volume->root, sizeof(volume->root), "/Volumes/%s", part->device);
        if (part->name[0]) strncpy(volume->name, part->name, sizeof(volume->name) - 1);
        else strncpy(volume->name, part->device, sizeof(volume->name) - 1);
        strcpy(volume->uuid, uuid);
        volume->builtin = disk->builtin; volume->hfs = 1;
    }
    return 0;
}

int tc_mast_parse(struct tc_inventory *inventory, const char *text) {
    char *copy, *line, *save = NULL;
    struct parsed_disk disk;
    int brace_depth = 0, collection_depth = 0;
    int disk_depth = 0, part_depth = 0, partitions_depth = 0, partitions_pending = 0;
    memset(inventory, 0, sizeof(*inventory)); memset(&disk, 0, sizeof(disk));
    if (text != NULL && (strstr(text, "<?xml") || strstr(text, "<plist"))) return -1;
    if (text == NULL || (copy = strdup(text)) == NULL) return -1;
    for (line = strtok_r(copy, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        char *value, *key, *cursor = trim(line);
        char *structural = cursor;
        int open_braces = 0, close_braces = 0, open_collections = 0, close_collections = 0;
        char *assignment = strchr(cursor, '=');
        if (assignment != NULL) structural = trim(assignment + 1);
        if (*structural == '{') open_braces = 1;
        else if (*structural == '}') close_braces = 1;
        else if (*structural == '(' || *structural == '[') open_collections = 1;
        else if (*structural == ')' || *structural == ']') close_collections = 1;
        if (strstr(cursor, "partitions") && strchr(cursor, '=')) partitions_pending = 1;
        collection_depth += open_collections;
        if (partitions_pending && open_collections) {
            partitions_depth = collection_depth;
            partitions_pending = 0;
        }
        while (open_braces--) {
            brace_depth++;
            if (partitions_depth && collection_depth >= partitions_depth) {
                if (!part_depth) {
                    if (disk.count >= TC_MAX_VOLUMES) { free(copy); return -1; }
                    memset(&disk.parts[disk.count], 0, sizeof(disk.parts[disk.count]));
                    part_depth = brace_depth;
                }
            } else if (!disk_depth) {
                memset(&disk, 0, sizeof(disk)); disk_depth = brace_depth;
            }
        }
        value = strchr(cursor, '=');
        if (value) {
            *value++ = '\0'; key = trim(cursor);
            if (!strcmp(key, "deviceName")) {
                if (part_depth) decode_value(disk.parts[disk.count].device, sizeof(disk.parts[disk.count].device), value);
                else decode_value(disk.device, sizeof(disk.device), value);
            } else if (part_depth && !strcmp(key, "name"))
                decode_value(disk.parts[disk.count].name, sizeof(disk.parts[disk.count].name), value);
            else if (part_depth && !strcmp(key, "format"))
                decode_value(disk.parts[disk.count].format, sizeof(disk.parts[disk.count].format), value);
            else if (part_depth && !strcmp(key, "uuid"))
                decode_value(disk.parts[disk.count].uuid, sizeof(disk.parts[disk.count].uuid), value);
            else if (!part_depth && !strcmp(key, "builtin")) {
                char decoded[16]; decode_value(decoded, sizeof(decoded), value);
                disk.builtin = !strcasecmp(decoded, "true") || !strcmp(decoded, "1");
            }
        }
        while (close_braces--) {
            if (part_depth == brace_depth) {
                if (disk.parts[disk.count].device[0]) disk.count++;
                part_depth = 0;
            } else if (disk_depth == brace_depth) {
                if (append_disk(inventory, &disk) != 0) { free(copy); return -1; }
                disk_depth = 0;
            }
            if (brace_depth > 0) brace_depth--;
        }
        collection_depth -= close_collections;
        if (collection_depth < partitions_depth) partitions_depth = 0;
        if (collection_depth < 0 || brace_depth < 0) { free(copy); return -1; }
    }
    free(copy);
    inventory->valid = disk_depth == 0 && part_depth == 0 && brace_depth == 0 && collection_depth == 0;
    inventory->empty = inventory->valid && inventory->count == 0;
    if (!inventory->valid)
        fprintf(stderr, "MaSt parse incomplete disk_depth=%d part_depth=%d brace_depth=%d collection_depth=%d volumes=%zu\n",
                disk_depth, part_depth, brace_depth, collection_depth, inventory->count);
    return inventory->valid ? 0 : -1;
}

int tc_mast_collect(struct tc_inventory *inventory) {
    struct acp_request request;
    char *output = malloc(65537);
    int result;
    if (output == NULL) return -1;
    memset(&request, 0, sizeof(request)); request.key = "MaSt"; request.form = ACP_ARRAY;
    request.multiline = 1; request.output = output; request.capacity = 65537;
    (void)acp_collect_run(&request, 1, 30000, 30000);
    result = request.status == ACP_OK ? tc_mast_parse(inventory, output) : -1;
    free(output); return result;
}

int tc_mast_print(FILE *stream) {
    struct tc_inventory inventory;
    size_t i;
    if (tc_mast_collect(&inventory) != 0) return -1;
    fprintf(stream, "storage: valid=%d empty=%d volumes=%zu\n", inventory.valid, inventory.empty, inventory.count);
    for (i = 0; i < inventory.count; i++) {
        struct tc_volume *volume = &inventory.volumes[i];
        fprintf(stream, "volume: disk=%s device=%s root=%s name=\"%s\" uuid=%s builtin=%d\n",
                volume->disk, volume->device, volume->root, volume->name, volume->uuid, volume->builtin);
    }
    return fflush(stream) == 0 ? 0 : -1;
}

static int mounted_identity(struct tc_volume *volume) {
    struct stat root, parent;
    volume->available = 0;
    if (stat(volume->root, &root) != 0 || stat("/Volumes", &parent) != 0 ||
        !S_ISDIR(root.st_mode) || root.st_dev == parent.st_dev) return -1;
    volume->mount_identity = (uint64_t)root.st_dev;
    volume->available = 1;
    return 0;
}

static int claim_volume(const char *root) {
    char argument[96];
    pid_t child;
    int status, attempts;
    if (snprintf(argument, sizeof(argument), "path:s:%s", root) >= (int)sizeof(argument)) return -1;
    child = fork();
    if (child == 0) {
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL);
        execl(TC_ACP_PATH, "acp", "rpc", "diskd.useVolume", argument, (char *)NULL);
        _exit(127);
    }
    if (child < 0) return -1;
    for (attempts = 0; attempts < 300; attempts++) {
        pid_t waited = waitpid(child, &status, WNOHANG);
        if (waited == child) return WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
        if (waited < 0 && errno != EINTR) return -1;
        usleep(100000);
    }
    kill(child, SIGTERM); usleep(200000); kill(child, SIGKILL); (void)waitpid(child, &status, 0);
    return -1;
}

int tc_storage_activate(struct tc_volume *volume) {
    char probe[96];
    if (mkdir(volume->root, 0755) != 0 && errno != EEXIST) return -1;
    if (mounted_identity(volume) != 0) {
        int attempt;
        if (claim_volume(volume->root) != 0) return -1;
        for (attempt = 0; attempt < 300 && mounted_identity(volume) != 0; attempt++) usleep(100000);
        if (!volume->available) return -1;
    }
    if (snprintf(probe, sizeof(probe), "%s/.tc-service-write-test.%ld", volume->root, (long)getpid()) >= (int)sizeof(probe)) return -1;
    if (mkdir(probe, 0700) == 0) { volume->writable = rmdir(probe) == 0; }
    return volume->writable ? 0 : -1;
}

int tc_storage_verify_identity(const struct tc_volume *volume) {
    struct stat root;
    return volume->available && stat(volume->root, &root) == 0 &&
           (uint64_t)root.st_dev == volume->mount_identity ? 0 : -1;
}
