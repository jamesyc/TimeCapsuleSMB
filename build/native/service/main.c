#include "service.h"
#ifdef TC_SERVICE_MULTICALL
#define main tc_service_helper_main
#endif
static void stop_acp(int signo) { (void)signo; acp_stop_requested = 1; }

/* Model discovery belongs with Samba's native naming projection, not a second
 * set of shell normalization/ACP routines. No network policy is queried here. */
int tc_samba_identity_read(struct tc_samba_identity *out) {
    static const char *const models[] = {
        "AirPort5,104", "AirPort5,105", "TimeCapsule6,106", "AirPort5,108",
        "TimeCapsule6,109", "TimeCapsule6,113", "AirPort5,114", "TimeCapsule6,116",
        "AirPort5,117", "TimeCapsule8,119", "AirPort7,120"
    };
    struct device_facts facts;
    struct identity id;
    struct acp_value syap, syam;
    struct acp_request requests[3];
    struct acp_u32 model_id;
    char server[256];
    const char *model = "MacSamba";
    size_t i;
    memset(&facts, 0, sizeof(facts));
    memset(requests, 0, sizeof(requests));
    requests[0].key = "syNm"; requests[0].output = facts.acp[ACP_KEY_syNm].text;
    requests[1].key = "syAP"; requests[1].output = syap.text;
    requests[2].key = "syAM"; requests[2].output = syam.text;
    for (i = 0; i < 3; i++) { requests[i].capacity = ACP_VALUE_MAX; requests[i].trim_whitespace = 1; }
    if (acp_collect_run(requests, 3, (long long)TC_ACP_TIMEOUT_SECONDS * 1000,
                        (long long)TC_ACP_COLLECTION_BUDGET_SECONDS * 1000) < 0) return 1;
    facts.acp[ACP_KEY_syNm].status = requests[0].status;
    syap.status = requests[1].status; syam.status = requests[2].status;
    if (gethostname(facts.hostname, sizeof(facts.hostname)) != 0) facts.hostname[0] = '\0';
    facts.hostname[sizeof(facts.hostname) - 1] = '\0';
    identity_derive(&id, &facts);
    if (normalize_server_string(server, sizeof(server), facts.acp[ACP_KEY_syNm].text) != 0)
        strcpy(server, id.instance);
    model_id = acp_u32(&syap);
    for (i = 0; i < sizeof(models) / sizeof(models[0]); i++) {
        if (model_id.available && strtoul(strchr(models[i], ',') + 1, NULL, 10) == model_id.value) {
            model = models[i]; break;
        }
    }
    if (!strcmp(model, "MacSamba") && syam.status == ACP_OK) {
        for (i = 0; i < sizeof(models) / sizeof(models[0]); i++)
            if (strstr(syam.text, models[i])) { model = models[i]; break; }
    }
    memset(out, 0, sizeof(*out));
    strcpy(out->netbios, id.netbios); strcpy(out->server, server); strcpy(out->model, model);
    out->name_observed = requests[0].status == ACP_OK;
    return 0;
}

static int print_samba_identity(void) {
    struct tc_samba_identity identity;
    if (tc_samba_identity_read(&identity)) return 1;
    return printf("samba-identity 1\n%s\n%s\n%s\n", identity.netbios, identity.server, identity.model) < 0 || fflush(stdout) != 0;
}

static void usage(void) {
    fputs("Usage: service --print-nt-hash-from-stdin | --print-device-nt-hash | --print-samba-identity | --print-smb-bind-interfaces [--retain-policy] | --print-link-plan | --version\n", stderr);
}
int main(int argc, char **argv) {
    const char *facts_file = NULL;
    const char *command = NULL;
    struct device_plan plan;
    struct device_plan history;
    int retain_policy = 0;
    int i;

    for (i = 1; i < argc; i++) {
#ifdef TC_NATIVE_TEST
        if (!strcmp(argv[i], "--facts-file") && i + 1 < argc) {
            facts_file = argv[++i];
            continue;
        }
#endif
        if (!strcmp(argv[i], "--retain-policy")) {
            retain_policy = 1;
        } else if (command == NULL && argv[i][0] == '-') {
            command = argv[i];
        } else {
            usage();
            return EXIT_USAGE;
        }
    }
    if (command == NULL) {
        usage();
        return EXIT_USAGE;
    }
    signal(SIGTERM, stop_acp); signal(SIGINT, stop_acp); signal(SIGPIPE, SIG_IGN);
    if (retain_policy && (strcmp(command, "--print-smb-bind-interfaces") || service_read_policy(stdin, &history) != 0)) {
        fputs("service: invalid retained policy\n", stderr);
        return EXIT_PLAN_FAILED;
    }
    if (!strcmp(command, "--version")) { printf("%d\n", SERVICE_VERSION_CODE); return EXIT_OK; }
    if (!strcmp(command, "--print-samba-identity")) return print_samba_identity();
    if (!strcmp(command, "--print-device-nt-hash")) return print_device_nt_hash();
    if (!strcmp(command, "--print-nt-hash-from-stdin")) return print_nt_hash_from_stdin();
    if (!strcmp(command, "--print-smb-bind-interfaces") || !strcmp(command, "--print-link-plan")) {
        if (service_collect_plan(&plan, facts_file, retain_policy ? &history : NULL) != 0) {
            fputs("service: device plan collection failed\n", stderr);
            return EXIT_PLAN_FAILED;
        }
        if (!strcmp(command, "--print-link-plan")) {
            return print_link_plan(stdout, &plan) == 0 ? EXIT_OK : EXIT_PLAN_FAILED;
        }
        if (print_smb_bind_interfaces(stdout, &plan) != 0 ||
            (retain_policy && service_print_policy(stdout, &history) != 0)) return EXIT_PLAN_FAILED;
        return EXIT_OK;
    }
    usage();
    return EXIT_USAGE;
}
