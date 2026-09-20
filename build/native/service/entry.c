#include "service.h"

int tc_manager_main(int argc, char **argv);
int tc_service_helper_main(int argc, char **argv);
int tc_discovery_main(int argc, char **argv);
int tc_telemetry_main(int argc, char **argv);

/* One static image, independent processes. Each daemon still owns its own
 * collector/history; this dispatcher does not introduce a plan IPC protocol. */
int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "manager"))
        return tc_manager_main(argc - 1, argv + 1);
    if (argc > 1 && !strcmp(argv[1], "discovery"))
        return tc_discovery_main(argc - 1, argv + 1);
    if (argc > 1 && !strcmp(argv[1], "telemetry"))
        return tc_telemetry_main(argc - 1, argv + 1);
    if (argc > 1 && !strcmp(argv[1], "--print-mast"))
        return tc_discovery_main(argc, argv);
    return tc_service_helper_main(argc, argv);
}
