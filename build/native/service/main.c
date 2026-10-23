#include "service.h"
#include <sys/param.h>
#if defined(__NetBSD__) && !defined(TC_NATIVE_TEST)
#include <sys/sysctl.h>
#endif
static void stop_acp(int signo) { (void)signo; acp_stop_requested = 1; }

static int print_acp_mast(FILE *stream, long long timeout_ms) {
    struct acp_request request;
    char *output = malloc(65537);
    size_t nonempty;
    int rc = 1;
    if (!output) return 1;
    memset(&request, 0, sizeof(request));
    request.key = "MaSt";
    request.form = ACP_ARRAY;
    request.multiline = 1;
    request.output = output;
    request.capacity = 65537;
    (void)acp_collect_run(&request, 1, timeout_ms, timeout_ms);
    nonempty = request.length;
    while (nonempty && output[nonempty - 1] == '\n') nonempty--;
    if (request.status == ACP_OK && nonempty) {
        rc = fwrite(output, 1, request.length, stream) == request.length && fflush(stream) == 0 ? 0 : 1;
    } else if (request.status == ACP_UNAVAILABLE && request.exit_status > 0) {
        rc = request.exit_status;
    }
    free(output);
    return rc;
}

static int print_mast_command(int argc, char **argv) {
    long long timeout_ms = (long long)TC_ACP_TIMEOUT_SECONDS * 1000;
    if (argc == 4 && !strcmp(argv[2], "--timeout-seconds")) {
        char *end;
        unsigned long seconds;
        errno = 0;
        seconds = strtoul(argv[3], &end, 10);
        if (errno || !*argv[3] || *end || argv[3][0] == '-' || !seconds || seconds > 3600) return EXIT_USAGE;
        timeout_ms = (long long)seconds * 1000;
    } else if (argc != 2) return EXIT_USAGE;
    return print_acp_mast(stdout, timeout_ms);
}

/* Model discovery belongs with Samba's native naming projection, not a second
 * set of shell normalization/ACP routines. No network policy is queried here. */
int tc_samba_identity_read(struct tc_samba_identity *out, const char *hostname) {
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
    snprintf(facts.hostname, sizeof(facts.hostname), "%s", hostname);
    identity_derive(&id, &facts);
    /* A blank syNm names the server by the host label, as before the
     * instance name kept syNm's whitespace. */
    if (normalize_server_string(server, sizeof(server), facts.acp[ACP_KEY_syNm].text) != 0 &&
        normalize_host_label(server, sizeof(server), facts.hostname) != 0)
        strcpy(server, "timecapsule");
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
    char hostname[MAXHOSTNAMELEN];
    tc_hostname_read(hostname, sizeof(hostname));
    if (tc_samba_identity_read(&identity, hostname)) return 1;
    return printf("samba-identity 1\n%s\n%s\n%s\n", identity.netbios, identity.server, identity.model) < 0 || fflush(stdout) != 0;
}

/* Now on the kernel's monotonic clock, in ms: the clock of the manager's
 * title started= (stuck.h tc_manager_title). Doctor subtracts the two, so the
 * startup age never mixes it with the wall clock. */
static int print_monotonic_ms(void) {
    long long now = acp_monotonic_ms();
    if (now < 0) return 1;
    return printf("%lld\n", now) < 0 || fflush(stdout) != 0;
}

/* Lists the fcntl() byte-range locks other processes hold on each file: an
 * smbd asleep in "lockf" may wait for one of these on a Samba TDB, and only
 * the holder's PID says which process to look at. F_GETLK reports one
 * conflicting lock at a time (one of several readers of a range), so the scan
 * resumes after each. A flock() lock is listed as well, as pid -1 for the
 * whole file: it belongs to an open file, not a process. Opening the file
 * read-only takes no lock, and closing it drops none, since this process
 * holds none. */
static int print_file_locks(int argc, char **argv) {
    int i, rc = 0;
    for (i = 2; i < argc; i++) {
        struct flock lock;
        off_t start = 0;
        size_t length = strlen(argv[i]);
        /* Every process that has a TDB open holds a read lock on its byte 4
         * (TDB's ACTIVE_LOCK) until it closes it: nobody waits for that one,
         * and only one of its holders is listed. */
        int tdb = length > 4 && !strcmp(argv[i] + length - 4, ".tdb");
        int fd = open(argv[i], O_RDONLY);
        if (fd < 0) {
            printf("%s: %s\n", argv[i], strerror(errno));
            rc = 1;
            continue;
        }
        for (;;) {
            memset(&lock, 0, sizeof(lock));
            lock.l_type = F_WRLCK;
            lock.l_whence = SEEK_SET;
            lock.l_start = start;
            if (fcntl(fd, F_GETLK, &lock) < 0) {
                printf("%s: %s\n", argv[i], strerror(errno));
                rc = 1;
                break;
            }
            if (lock.l_type == F_UNLCK) break;
            printf("%s pid %ld %s %lld+%lld%s\n", argv[i], (long)lock.l_pid,
                   lock.l_type == F_WRLCK ? "write" : "read", (long long)lock.l_start, (long long)lock.l_len,
                   tdb && lock.l_type == F_RDLCK && lock.l_start == 4 && lock.l_len == 1 ? " (TDB open marker)" : "");
            /* Length 0 runs to the end of the file. */
            if (lock.l_len <= 0) break;
            start = lock.l_start + lock.l_len;
        }
        close(fd);
    }
    return fflush(stdout) != 0 ? 1 : rc;
}

/* The user registers a process asleep in a system call entered the kernel
 * with. On ARM, r0-r3 hold the call's arguments, so for an smbd asleep in
 * "lockf" they tell flock(fd, LOCK_EX) from fcntl(fd, F_SETLKW, ...) and give
 * the descriptor. The kernel saves them in a struct trapframe at the top of
 * the process's 8 KB u-area (arm32 USPACE_SVC_STACK_TOP); this reads that copy
 * through /dev/kmem at the u-area address KERN_PROC2 reports, without
 * stopping the process or touching its memory. Apple's NetBSD 6 kernel, like
 * the NetBSD 7 headers its lane builds with, pads the frame after spsr;
 * NetBSD 4's frame has no pad. The frame ends at the top of the u-area either
 * way, so only spsr depends on the pad.
 *
 * The registers describe the sleep only while the process stays in it, so
 * its wait channel is read before and after the frame: a process that woke,
 * slept elsewhere or exited in between is reported as changed, not read.
 * Stock NetBSD 4 and 6 check every /dev/kmem address (uvm_kernacc) and fail a
 * read of an unmapped one with EFAULT. */
#ifndef TC_TRAPFRAME_FILL
#if defined(__NetBSD__) && __NetBSD_Version__ < 500000000
#define TC_TRAPFRAME_FILL 0
#else
#define TC_TRAPFRAME_FILL 1
#endif
#endif
#define TC_USPACE 8192
#define TC_WCHAN_MAX 9 /* kernel wait messages are 8 bytes, not NUL-terminated */
struct tc_trapframe {
    uint32_t spsr;
#if TC_TRAPFRAME_FILL
    uint32_t fill;
#endif
    uint32_t r[13], usr_sp, usr_lr, svc_sp, svc_lr, pc;
};

#if defined(__NetBSD__) && !defined(TC_NATIVE_TEST)
/* The process's u-area address and wait channel ("-" when it is not asleep).
 * look is 0 before the frame is read and 1 after; the kernel ignores it. */
static int sleep_state(pid_t pid, int look, unsigned long long *uarea, char wchan[TC_WCHAN_MAX]) {
    struct kinfo_proc2 p;
    int mib[6] = {CTL_KERN, KERN_PROC2, KERN_PROC_PID, (int)pid, sizeof(p), 1};
    size_t length = sizeof(p);
    (void)look;
    if (sysctl(mib, 6, &p, &length, NULL, 0) < 0) return -1;
    if (length < sizeof(p)) {
        errno = ESRCH;
        return -1;
    }
    *uarea = p.p_addr;
    snprintf(wchan, TC_WCHAN_MAX, "%.*s", (int)sizeof(p.p_wmesg), p.p_wmesg);
    if (!*wchan) strcpy(wchan, "-");
    return 0;
}
static const char *kmem_path(void) { return "/dev/kmem"; }
#else
/* Host tests: TC_TEST_UAREAS names a file of "PID ADDRESS WCHAN [WCHAN_AFTER]"
 * lines (WCHAN_AFTER, when given, is what the second look sees), and
 * TC_TEST_KMEM a file standing in for kernel memory at those addresses. */
static int sleep_state(pid_t pid, int look, unsigned long long *uarea, char wchan[TC_WCHAN_MAX]) {
    const char *path = getenv("TC_TEST_UAREAS");
    FILE *file = path ? fopen(path, "r") : NULL;
    char line[256], first[TC_WCHAN_MAX], second[TC_WCHAN_MAX];
    long row;
    unsigned long long value;
    int found = 0, fields;
    if (!file) return -1;
    while (!found && fgets(line, sizeof(line), file)) {
        fields = sscanf(line, "%ld %llx %8s %8s", &row, &value, first, second);
        if (fields >= 3 && row == (long)pid) {
            *uarea = value;
            strcpy(wchan, look && fields == 4 ? second : first);
            found = 1;
        }
    }
    fclose(file);
    if (!found) errno = ESRCH;
    return found ? 0 : -1;
}
static const char *kmem_path(void) { return getenv("TC_TEST_KMEM") ? getenv("TC_TEST_KMEM") : "/nonexistent"; }
#endif

static int print_syscall_registers(int argc, char **argv) {
    int i, rc = 0, kmem = open(kmem_path(), O_RDONLY);
    if (kmem < 0) {
        printf("%s: %s\n", kmem_path(), strerror(errno));
        return 1;
    }
    for (i = 2; i < argc; i++) {
        struct tc_trapframe frame;
        unsigned long long uarea, later;
        char before[TC_WCHAN_MAX], after[TC_WCHAN_MAX], *end;
        long pid;
        errno = 0;
        pid = strtol(argv[i], &end, 10);
        if (errno || !*argv[i] || *end || pid <= 0 || (long)(pid_t)pid != pid) {
            printf("%s: not a PID\n", argv[i]);
            rc = 1;
            continue;
        }
        if (sleep_state((pid_t)pid, 0, &uarea, before) < 0 ||
            pread(kmem, &frame, sizeof(frame), (off_t)(uarea + TC_USPACE - sizeof(frame))) != (ssize_t)sizeof(frame)) {
            printf("%ld: %s\n", pid, errno ? strerror(errno) : "short read");
            rc = 1;
            continue;
        }
        if (sleep_state((pid_t)pid, 1, &later, after) < 0 || later != uarea || strcmp(before, after)) {
            printf("%ld: changed during the read\n", pid);
            continue;
        }
        printf("%ld wchan=%s spsr=%08lx r0=%08lx r1=%08lx r2=%08lx r3=%08lx pc=%08lx\n", pid, before,
               (unsigned long)frame.spsr, (unsigned long)frame.r[0], (unsigned long)frame.r[1],
               (unsigned long)frame.r[2], (unsigned long)frame.r[3], (unsigned long)frame.pc);
    }
    close(kmem);
    return fflush(stdout) != 0 ? 1 : rc;
}

static void usage(void) {
    fputs("Usage: service --print-nt-hash-from-stdin | --print-device-nt-hash | --print-samba-identity | --print-link-plan | --print-monotonic-ms | --print-mast [--timeout-seconds N] | --print-file-locks FILE... | --print-syscall-registers PID...\n", stderr);
}
int tc_service_helper_main(int argc, char **argv) {
    const char *facts_file = NULL;
    const char *command = NULL;
    struct device_plan plan;
    int i;

    signal(SIGTERM, stop_acp); signal(SIGINT, stop_acp); signal(SIGPIPE, SIG_IGN);
    if (argc >= 2 && !strcmp(argv[1], "--print-mast")) return print_mast_command(argc, argv);
    if (argc >= 2 && !strcmp(argv[1], "--print-file-locks")) return print_file_locks(argc, argv);
    if (argc >= 2 && !strcmp(argv[1], "--print-syscall-registers")) return print_syscall_registers(argc, argv);
    for (i = 1; i < argc; i++) {
#ifdef TC_NATIVE_TEST
        if (!strcmp(argv[i], "--facts-file") && i + 1 < argc) {
            facts_file = argv[++i];
            continue;
        }
#endif
        if (command == NULL && argv[i][0] == '-') {
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
    if (!strcmp(command, "--print-samba-identity")) return print_samba_identity();
    if (!strcmp(command, "--print-device-nt-hash")) return print_device_nt_hash();
    if (!strcmp(command, "--print-nt-hash-from-stdin")) return print_nt_hash_from_stdin();
    if (!strcmp(command, "--print-monotonic-ms")) return print_monotonic_ms();
    if (!strcmp(command, "--print-link-plan")) {
        if (service_collect_plan(&plan, facts_file) != 0) {
            fputs("service: device plan collection failed\n", stderr);
            return EXIT_PLAN_FAILED;
        }
        return print_link_plan(stdout, &plan) == 0 ? EXIT_OK : EXIT_PLAN_FAILED;
    }
    usage();
    return EXIT_USAGE;
}
