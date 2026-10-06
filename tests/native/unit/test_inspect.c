#include "service/inspect.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

/* The manager's table for the pass, and each process's command line, from
 * "PID PPID PGID STATE NAME COMMAND" lines as ps would print them. */
static struct tc_proctable snapshot;
static char commands[TC_PROCESS_MAX][16384];
static int argv_result, argv_reads;
static pid_t argv_failing;
static int fake_argv(pid_t pid, char *out, size_t size) {
    size_t i;
    argv_reads++;
    if (pid == argv_failing)
        return argv_result;
    for (i = 0; i < snapshot.count; i++)
        if (snapshot.procs[i].pid == pid) {
            snprintf(out, size, "%s", commands[i]);
            return 0;
        }
    return 1;
}
static void load(const char *text) {
    memset(&snapshot, 0, sizeof(snapshot));
    argv_failing = 0;
    argv_reads = 0;
    while (*text) {
        const char *end = strchr(text, '\n');
        size_t length = end ? (size_t)(end - text) : strlen(text);
        static char line[16384];
        char state[32], name[64];
        int pid, parent, group, offset = 0;
        struct tc_proc *p = &snapshot.procs[snapshot.count];
        memcpy(line, text, length);
        line[length] = 0;
        text += length + (end != NULL);
        assert(sscanf(line, "%d %d %d %31s %63s %n", &pid, &parent, &group, state, name, &offset) == 5);
        p->pid = pid;
        p->parent = parent;
        p->group = group;
        p->stat = strchr(state, 'Z') ? TC_LSZOMB : TC_LSSLEEP;
        snprintf(p->comm, sizeof(p->comm), "%.16s", name);
        snprintf(commands[snapshot.count++], sizeof(commands[0]), "%s", line + offset);
    }
}
static int build(struct tc_process_table *table, const char *text) {
    load(text);
    return tc_process_table_build(table, &snapshot, fake_argv);
}

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
                     "25 2 25 S service /mnt/Flash/service --print-link-plan\n"
                     "26 2 26 S service /mnt/Flash/service --print-mast\n"
                     "27 2 27 S service /mnt/Flash/service telemetry --cleanup\n"
                     "30 2 30 S smbd /mnt/Memory/samba4/sbin/smbd -F --no-process-group\n"
                     "31 2 31 Z smbd (smbd)\n"
                     "32 2 32 S wcifsfs wcifsfs\n";
    assert(!build(&table, ps));
    assert(table.count == 7);
    assert(table.processes[0].role == TC_PROC_DISKD_LOOPBACK);
    assert(table.processes[1].role == TC_PROC_DISKD);
    assert(table.processes[2].role == TC_PROC_DISCOVERY && table.processes[2].parent == 2);
    assert(table.processes[3].role == TC_PROC_WCIFSND);
    assert(table.processes[4].role == TC_PROC_TELEMETRY);
    assert(table.processes[5].role == TC_PROC_SMBD && table.processes[5].pid == 30);
    assert(table.processes[6].role == TC_PROC_WCIFSFS);
    /* Command lines are read only where the role depends on them: the two
     * diskd and seven service processes, not init, Apple's daemons, smbd,
     * wcifsnd or wcifsfs. */
    assert(argv_reads == 9);

    /* A process that exited since the table was read is left out. */
    load(ps);
    argv_failing = 12;
    argv_result = 1;
    assert(!tc_process_table_build(&table, &snapshot, fake_argv));
    assert(table.count == 6 && table.processes[0].role == TC_PROC_DISKD && table.processes[0].pid == 13);
    /* Any other failure fails the table: an empty command line would make
     * the loopback diskd look foreign. */
    load(ps);
    argv_failing = 12;
    argv_result = -1;
    assert(tc_process_table_build(&table, &snapshot, fake_argv) < 0);
    /* smbd needs no command line, so its failure cannot matter. */
    load(ps);
    argv_failing = 30;
    argv_result = -1;
    assert(!tc_process_table_build(&table, &snapshot, fake_argv) && table.count == 7);

    /* Only the daemon holds TCP 873. Clients and the servers sshd starts for
     * a remote client are user transfers the manager must leave alone. */
    const char *rsync = "40 2 40 S rsync /mnt/Memory/samba4/sbin/rsync --daemon --no-detach "
                        "--config=/mnt/Memory/samba4/etc/rsyncd.conf\n"
                        "41 40 40 S rsync /mnt/Memory/samba4/sbin/rsync --daemon --no-detach "
                        "--config=/mnt/Memory/samba4/etc/rsyncd.conf\n"
                        "42 1 42 S rsync rsync --daemon\n"
                        "50 9 50 S rsync /mnt/Memory/samba4/sbin/rsync -rlptD --info=progress2 "
                        "/Volumes/dk2/ShareRoot/ 192.168.1.248::shareroot/\n"
                        "51 9 51 S rsync /mnt/Memory/samba4/sbin/rsync --server -logDtpre.iLsfxCIvu . /x\n"
                        "52 9 52 S rsync /mnt/Memory/samba4/sbin/rsync --server --daemon .\n"
                        "53 9 53 S rsync rsync -a /Volumes/dk2/--daemon/ /tmp/x\n";
    assert(!build(&table, rsync));
    assert(table.count == 3);
    assert(table.processes[0].pid == 40 && table.processes[0].role == TC_PROC_RSYNC);
    assert(table.processes[1].pid == 41 && table.processes[1].group == 40 &&
           table.processes[1].role == TC_PROC_RSYNC);
    assert(table.processes[2].pid == 42 && table.processes[2].role == TC_PROC_RSYNC);

    /* A kilobytes-long command line is read only as far as the buffer and
     * classified from its start. */
    static char text[16384];
    size_t used = (size_t)snprintf(text, sizeof(text), "60 9 60 S rsync rsync -a");
    while (used < 5000)
        used += (size_t)snprintf(text + used, sizeof(text) - used, " /Volumes/dk2/ShareRoot/file%zu", used);
    used += (size_t)snprintf(text + used, sizeof(text) - used, " 192.168.1.248::shareroot/\n"
                             "61 1 61 S rsync /mnt/Memory/samba4/sbin/rsync --daemon --no-detach\n"
                             "62 1 62 S smbd /mnt/Memory/samba4/sbin/smbd -F --no-process-group");
    while (used < 11000)
        used += (size_t)snprintf(text + used, sizeof(text) - used, " --option=x%zu", used);
    snprintf(text + used, sizeof(text) - used, "\n63 1 63 S wcifsfs wcifsfs\n");
    assert(!build(&table, text));
    assert(table.count == 3);
    assert(table.processes[0].pid == 61 && table.processes[0].role == TC_PROC_RSYNC);
    assert(table.processes[1].pid == 62 && table.processes[1].role == TC_PROC_SMBD);
    assert(table.processes[2].pid == 63 && table.processes[2].role == TC_PROC_WCIFSFS);
    /* An rsync whose daemon flag lies past the part read is a user transfer. */
    used = (size_t)snprintf(text, sizeof(text), "70 1 70 S rsync rsync");
    while (used < 3000)
        used += (size_t)snprintf(text + used, sizeof(text) - used, " --option=x%zu", used);
    snprintf(text + used, sizeof(text) - used, " --daemon\n");
    assert(!build(&table, text) && table.count == 0);
    assert(tc_listener_present("root smbd 30 3* internet stream tcp 192.0.2.2:445\n", 445));
    assert(tc_listener_present("root smbd 30 4* internet6 stream tcp [fe80::445:1%bridge0]:445\n", 445));
    assert(!tc_listener_present("root smbd 30 3* internet stream tcp 192.0.2.2:4455\n", 445));
    assert(!tc_listener_present("root smbd 30 3* internet stream tcp 192.0.2.2:445 <-> 192.0.2.3:2345\n",
                                445));
    assert(!tc_listener_present("root wcifsnd 30 3* internet dgram udp 192.0.2.2:445\n", 445));
    assert(tc_wildcard_listener_families("root smbd 30 3* internet stream tcp deadbeef *:445\n"
                                         "root smbd 30 4* internet6 stream tcp deadbeef *:445\n",
                                         445) == 3);
    assert(tc_wildcard_listener_families("root smbd 30 3* internet stream tcp 0.0.0.0:445\n"
                                         "root smbd 30 4* internet6 stream tcp [::]:445\n",
                                         445) == 3);
    assert(tc_wildcard_listener_families("root smbd 30 3* internet stream tcp *:445\n"
                                         "root smbd 30 4* internet6 stream tcp [*]:445\n",
                                         445) == 3);
    assert(tc_wildcard_listener_families("root smbd 30 3* internet stream tcp 192.0.2.2:445\n"
                                         "root smbd 30 4* internet6 stream tcp [::1]:445\n",
                                         445) == 0);
    assert(tc_wildcard_listener_families("root smbd 30 3* internet stream tcp *:445 <-> 192.0.2.3:2345\n",
                                         445) == 0);
    const char *native = "root wcifsnd 21 3* internet dgram udp *:137\n"
                         "root wcifsnd 21 4* internet dgram udp *:138\n"
                         "root wcifsnd 21 5* internet dgram udp *:922\n";
    assert(tc_native_nbns_sockets_present(native, 21, 922));
    assert(tc_native_nbns_sockets_present("root wcifsnd 21 3* internet dgram udp c276b870 *:137\n"
                                          "root wcifsnd 21 4* internet dgram udp c276b8dc *:138\n"
                                          "root wcifsnd 21 5* internet dgram udp c276b438 *:922\n", 21, 922));
    assert(!tc_native_nbns_sockets_present(native, 22, 922));
    assert(!tc_native_nbns_sockets_present("root wcifsnd 21 3* internet dgram udp *:137\n"
                                           "root wcifsnd 22 4* internet dgram udp *:138\n"
                                           "root wcifsnd 21 5* internet dgram udp *:922\n", 21, 922));
    assert(!tc_native_nbns_sockets_present("root wcifsnd 21 3* internet dgram udp *:137\n"
                                           "root wcifsnd 21 4* internet dgram udp *:138\n"
                                           "root wcifsnd 21 5* internet dgram udp *:9220\n", 21, 922));
    assert(!tc_native_nbns_sockets_present("root wcifsnd 21 3* internet dgram udp *:137\n"
                                           "root wcifsnd 21 4* internet dgram udp *:138\n"
                                           "root wcifsnd 21 5* internet dgram udp *:922 <-> 127.0.0.1:1\n", 21, 922));
    assert(!tc_native_nbns_sockets_present("root wcifsnd 21 3* internet stream tcp *:137\n"
                                           "root wcifsnd 21 4* internet dgram udp *:138\n"
                                           "root wcifsnd 21 5* internet dgram udp *:922\n", 21, 922));
    return 0;
}
