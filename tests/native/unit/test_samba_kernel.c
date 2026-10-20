#include "samba/runtime.h"
#include <assert.h>

/* These are observed at the syscall boundary, not the helper's internal enum. */
static int values[5] = {262144, 1024, 84, 0, 262144};
static int reads[5], writes[5], queries;
static int read_fail = -1, write_fail = -1, omit = -1, query_fail;
static const int kern_ids[3] = {KERN_SBMAX, KERN_MAXFILES, KERN_MAXPROC};
static const int auto_id = 907, max_id = 311;

int tc_test_sysctl(const int *mib, unsigned count, void *old, size_t *length,
                   const void *value, size_t size) {
    int index = -1;
    if (count == 4) {
        assert(mib[0] == CTL_NET && mib[1] == PF_INET && mib[2] == IPPROTO_TCP);
        if (mib[3] == CTL_QUERY) {
            struct sysctlnode nodes[3];
            const struct sysctlnode *query = value;
            size_t n = 0, bytes;
            assert(length && query && size == sizeof(*query));
            assert(query->sysctl_flags == SYSCTL_VERSION);
            queries++;
            if (queries == query_fail) {
                errno = EIO;
                return -1;
            }
            /* Reverse the relevant nodes and include a foreign node: the
             * implementation must resolve names, not assume node positions. */
            memset(nodes, 0, sizeof(nodes));
            if (omit != 4) {
                nodes[n].sysctl_num = max_id;
                strcpy(nodes[n++].sysctl_name, "sendbuf_max");
            }
            nodes[n].sysctl_num = 777;
            strcpy(nodes[n++].sysctl_name, "recvbuf_max");
            if (omit != 3) {
                nodes[n].sysctl_num = auto_id;
                strcpy(nodes[n++].sysctl_name, "sendbuf_auto");
            }
            bytes = n * sizeof(nodes[0]);
            if (old) {
                assert(*length == bytes);
                memcpy(old, nodes, bytes);
            } else
                assert(!*length);
            *length = bytes;
            return 0;
        }
        if (mib[3] == auto_id) index = 3;
        if (mib[3] == max_id) index = 4;
    } else {
        assert(count == 2 && mib[0] == CTL_KERN);
        for (int i = 0; i < 3; i++)
            if (mib[1] == kern_ids[i]) index = i;
    }
    /* No other sysctl, including either shared network pool, may be accessed. */
    assert(index >= 0);
    if (old) {
        assert(length && *length == sizeof(int) && !value && !size);
        reads[index]++;
        if (index == read_fail) {
            errno = EIO;
            return -1;
        }
        memcpy(old, &values[index], sizeof(int));
        *length = sizeof(int);
    } else {
        assert(!length && value && size == sizeof(int));
        writes[index]++;
        if (index == write_fail) {
            errno = EACCES;
            return -1;
        }
        memcpy(&values[index], value, sizeof(int));
    }
    return 0;
}

static void equal(const int expected[5]) {
    for (int i = 0; i < 5; i++) assert(values[i] == expected[i]);
}

int main(int argc, char **argv) {
    int expected[5] = {2359296, 4096, 90, 1, 2097152};
    int before[5], failed = -1, netbsd4;
    assert(argc >= 2);
    netbsd4 = !strcmp(argv[1], "netbsd4");
    if (netbsd4) {
        expected[0] = 589824;
#ifdef __ARMEB__
        expected[2] = 99;
#else
        expected[2] = 103;
#endif
        expected[3] = values[3];
        expected[4] = values[4];
    } else if (!strcmp(argv[1], "larger")) {
        values[0] = expected[0] = 8388608;
        values[1] = expected[1] = 16384;
        values[2] = 128;
        values[3] = 2;
        values[4] = 4194304;
    } else if (!strcmp(argv[1], "same")) {
        memcpy(values, expected, sizeof(values));
    } else if (!strcmp(argv[1], "query-size-fail")) {
        query_fail = 1;
        failed = 3;
    } else if (!strcmp(argv[1], "query-fill-fail")) {
        query_fail = 2;
        failed = 3;
    } else if (!strcmp(argv[1], "missing")) {
        assert(argc == 3);
        failed = omit = atoi(argv[2]);
        assert(omit == 3 || omit == 4);
    } else if (!strcmp(argv[1], "read-fail") || !strcmp(argv[1], "write-fail")) {
        assert(argc == 3);
        failed = atoi(argv[2]);
        assert(failed >= 0 && failed < 5);
        if (!strcmp(argv[1], "read-fail")) read_fail = failed;
        else write_fail = failed;
    } else
        assert(!strcmp(argv[1], "netbsd6") || !strcmp(argv[1], "restart"));
    memcpy(before, values, sizeof(values));
    if (failed >= 0) expected[failed] = before[failed];
    tc_samba_prepare_kernel(netbsd4);
    equal(expected);
    for (int i = 0; i < 5; i++) {
        int active = !netbsd4 || i < 3;
        int undiscovered = i == omit || (query_fail && i == 3);
        assert(reads[i] == (active && !undiscovered));
        int attempted = active && !undiscovered && i != read_fail &&
                        (before[i] != expected[i] || i == write_fail);
        assert(writes[i] == attempted);
    }
    if (netbsd4) assert(!queries);
    if (!strcmp(argv[1], "same"))
        for (int i = 0; i < 5; i++) assert(!writes[i]);
    if (!strcmp(argv[1], "restart")) {
        assert(queries == 4);
        memcpy(values, before, sizeof(values));
        tc_samba_prepare_kernel(0);
        equal(expected);
        /* Reapply changed values, reusing only the discovered node numbers. */
        assert(queries == 4);
        for (int i = 0; i < 5; i++) assert(reads[i] == 2 && writes[i] == 2);
    }
    return 0;
}
