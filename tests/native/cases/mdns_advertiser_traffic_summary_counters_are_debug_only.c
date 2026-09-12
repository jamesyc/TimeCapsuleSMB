#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    memset(&g_mdns_counters, 0, sizeof(g_mdns_counters));
    memset(&g_mdns_counter_log_state, 0, sizeof(g_mdns_counter_log_state));
    g_debug_logging = 0;
    g_mdns_counters.ipv4_packets_received = 1;
    maybe_log_mdns_counters("traffic_summary", 1000);
    if (g_mdns_counter_log_state.last_log_ms != 0) {
        return 1;
    }

    g_debug_logging = 1;
    maybe_log_mdns_counters("traffic_summary", 1000);
    if (g_mdns_counter_log_state.last_log_ms != 1000) {
        return 2;
    }

    maybe_log_mdns_counters("traffic_summary", 2000);
    if (g_mdns_counter_log_state.last_log_ms != 1000) {
        return 3;
    }

    g_mdns_counters.ipv4_packets_received = 2;
    maybe_log_mdns_counters("traffic_summary", 32000);
    return g_mdns_counter_log_state.last_log_ms == 32000 ? 0 : 4;
}
