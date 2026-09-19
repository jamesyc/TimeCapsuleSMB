#ifndef TC_IPC_H
#define TC_IPC_H

#include "platform.h"

#define TC_IPC_VERSION 1
#define TC_IPC_MAX_PAYLOAD 4096

enum tc_ipc_role {
    TC_ROLE_SUPERVISOR = 0,
    TC_ROLE_MDNS = 1,
    TC_ROLE_NETBIOS = 2,
    TC_ROLE_TELEMETRY = 3,
    TC_ROLE_SAMBA = 4,
    TC_ROLE_RSYNC = 5
};

enum tc_ipc_type {
    TC_IPC_INIT = 1,
    TC_IPC_READY = 2,
    TC_IPC_REFRESH = 3,
    TC_IPC_STOP = 4,
    TC_IPC_DEGRADED = 5
};

struct tc_ipc_message {
    uint16_t type;
    uint16_t role;
    uint64_t instance;
    uint64_t generation;
    uint32_t length;
    unsigned char payload[TC_IPC_MAX_PAYLOAD];
};

int tc_ipc_configure_fd(int fd, int nonblocking, int close_on_exec);
int tc_ipc_send(int fd, uint16_t type, uint16_t role, uint64_t instance,
                uint64_t generation, const void *payload, uint32_t length);
/* 1 message, 0 EOF, -1 malformed/error, -2 would block. */
int tc_ipc_recv(int fd, struct tc_ipc_message *message);
int tc_ipc_worker_handshake(int fd, uint16_t role, uint64_t *instance,
                            uint64_t *generation);

#endif
