#include "../../../build/native/samba/runtime.h"

int device_nt_hash(char output[33]) {
    strcpy(output, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"); return 0;
}

int device_plan_bind_tokens(const struct device_plan *plan, char *out, size_t out_len) {
    (void)plan;
    return snprintf(out, out_len, "127.0.0.1/8 ::1/128 10.0.0.2/24") < (int)out_len ? 0 : -1;
}

int main(int argc, char **argv) {
    struct tc_runtime_config config;
    struct device_plan plan;
    struct tc_share_set shares;
    char source[512], destination[512], directory[512];
    FILE *file;
    if (argc != 2) return 2;
    memset(&config, 0, sizeof(config)); memset(&plan, 0, sizeof(plan)); memset(&shares, 0, sizeof(shares));
    strncpy(config.payload_dir, argv[1], sizeof(config.payload_dir) - 1);
    config.netatalk = 1; config.require_encryption = 1;
    plan.status.validated = 1; plan.status.cold_start = 1;
    strcpy(plan.id.netbios, "TIMECAPSULE"); strcpy(plan.id.instance, "Caf\303\251 Capsule");
    shares.count = 1; strcpy(shares.values[0].name, "Data"); strcpy(shares.values[0].path, "/Volumes/dk2/ShareRoot");
    mkdir(TC_SAMBA_RAM_ROOT, 0755);
    snprintf(directory, sizeof(directory), "%s/sbin", TC_SAMBA_RAM_ROOT); mkdir(directory, 0755);
    snprintf(destination, sizeof(destination), "%s/sbin/smbd", TC_SAMBA_RAM_ROOT);
    file = fopen(destination, "w"); if (!file) return 3; fputs("stale-smbd-that-must-be-replaced", file); fclose(file);
    snprintf(source, sizeof(source), "%s/smbd", argv[1]);
    file = fopen(source, "w"); if (!file) return 4; fputs("fake-smbd", file); fclose(file); chmod(source, 0755);
    return tc_samba_prepare(&config, &plan, &shares) == 0 ? 0 : 5;
}
