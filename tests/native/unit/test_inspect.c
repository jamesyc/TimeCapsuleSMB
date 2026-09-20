#include "service/inspect.h"
#include <assert.h>

int main(void) {
    struct tc_process_table table;
    const char *ps = "1 0 1 Ss init init\n"
                     "10 1 2 S mDNSResponder /sbin/mDNSResponder -d\n"
                     "11 1 2 S afpserver /sbin/afpserver -debug\n"
                     "12 1 2 S diskd /sbin/diskd -i lo0 -d local.\n"
                     "13 1 2 S diskd /sbin/diskd -i lo0x\n"
                     "20 2 20 S service service: role=discovery nbns=ready\n"
                     "21 20 20 S wcifsnd wcifsnd\n"
                     "22 2 22 S service /mnt/Flash/service telemetry --daemon\n"
        "23 2 23 S service service: role=job telemetry\n"
        "24 2 24 S service /mnt/Flash/service telemetry --once role=discovery\n"
        "25 2 25 S service /mnt/Flash/service discovery --print-link-plan\n"
        "26 2 26 S service /mnt/Flash/service discovery --diskless --print-link-plan\n"
        "27 2 27 S service /mnt/Flash/service telemetry --cleanup\n"
                     "30 2 30 S smbd /mnt/Memory/samba4/sbin/smbd -F --no-process-group\n"
                     "31 2 31 Z smbd (smbd)\n"
                     "32 2 32 S wcifsfs wcifsfs\n";
    assert(!tc_process_table_parse(&table, ps));
    assert(table.count == 7);
    assert(table.processes[0].role == TC_PROC_DISKD_LOOPBACK);
    assert(table.processes[1].role == TC_PROC_DISKD);
    assert(table.processes[2].role == TC_PROC_DISCOVERY && table.processes[2].parent == 2);
    assert(table.processes[3].role == TC_PROC_WCIFSND);
    assert(table.processes[4].role == TC_PROC_TELEMETRY);
    assert(tc_process_table_parse(&table, "truncated\n") < 0);
    assert(tc_listener_families("root smbd 30 3* internet stream tcp 192.0.2.2:445\n"
                                "root smbd 30 4* internet6 stream tcp [fe80::445:1%bridge0]:445\n",
                                445) == 3);
    assert(tc_listener_families("root smbd 30 3* internet stream tcp 192.0.2.2:4455\n", 445) == 0);
    assert(tc_listener_families("root smbd 30 3* internet stream tcp 192.0.2.2:445 <-> 192.0.2.3:2345\n",
                                445) == 0);
    assert(tc_listener_families("root wcifsnd 30 3* internet dgram udp 192.0.2.2:445\n", 445) == 0);
    return 0;
}
