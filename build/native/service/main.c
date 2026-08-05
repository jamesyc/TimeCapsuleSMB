#include "service.h"
static int collect(struct link_context_set *out, void *unused) {
    (void)unused;
    return collect_usable_link_contexts(out);
}
int main(int argc, char **argv) {
    if (argc == 2) {
        if (!strcmp(argv[1], "--version")) { puts("1"); return 0; }
        if (!strcmp(argv[1], "--print-nt-hash-from-stdin")) return print_nt_hash_from_stdin();
        if (!strcmp(argv[1], "--print-auto-ip-cidrs")) return print_auto_ip_cidrs_with_provider(stdout, collect, NULL);
        if (!strcmp(argv[1], "--print-smb-bind-interfaces")) return print_smb_bind_interfaces_with_provider(stdout, collect, NULL);
        if (!strcmp(argv[1], "--print-smb-bind-interfaces-lan")) return print_smb_bind_interfaces_lan_with_provider(stdout, collect, NULL);
    }
    fputs("Usage: service --print-nt-hash-from-stdin | --print-auto-ip-cidrs | --print-smb-bind-interfaces[-lan] | --version\n", stderr);
    return EXIT_USAGE;
}
