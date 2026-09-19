#include "ipc.h"

#define TC_IPC_MAGIC 0x54435356U
#define TC_IPC_HEADER_SIZE 32

static void put16(unsigned char *out, uint16_t value) {
    out[0] = (unsigned char)(value >> 8); out[1] = (unsigned char)value;
}
static void put32(unsigned char *out, uint32_t value) {
    out[0] = (unsigned char)(value >> 24); out[1] = (unsigned char)(value >> 16);
    out[2] = (unsigned char)(value >> 8); out[3] = (unsigned char)value;
}
static void put64(unsigned char *out, uint64_t value) {
    put32(out, (uint32_t)(value >> 32)); put32(out + 4, (uint32_t)value);
}
static uint16_t get16(const unsigned char *in) {
    return (uint16_t)(((uint16_t)in[0] << 8) | in[1]);
}
static uint32_t get32(const unsigned char *in) {
    return ((uint32_t)in[0] << 24) | ((uint32_t)in[1] << 16) |
           ((uint32_t)in[2] << 8) | in[3];
}
static uint64_t get64(const unsigned char *in) {
    return ((uint64_t)get32(in) << 32) | get32(in + 4);
}

int tc_ipc_configure_fd(int fd, int nonblocking, int close_on_exec) {
    int flags = fcntl(fd, F_GETFL, 0);
    int descriptor_flags = fcntl(fd, F_GETFD, 0);
    if (flags < 0 || descriptor_flags < 0) return -1;
    if (nonblocking) flags |= O_NONBLOCK; else flags &= ~O_NONBLOCK;
    if (close_on_exec) descriptor_flags |= FD_CLOEXEC; else descriptor_flags &= ~FD_CLOEXEC;
    return fcntl(fd, F_SETFL, flags) == 0 && fcntl(fd, F_SETFD, descriptor_flags) == 0 ? 0 : -1;
}

static int write_all(int fd, const unsigned char *data, size_t length) {
    while (length) {
        ssize_t written = write(fd, data, length);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return -1;
        data += written; length -= (size_t)written;
    }
    return 0;
}

int tc_ipc_send(int fd, uint16_t type, uint16_t role, uint64_t instance,
                uint64_t generation, const void *payload, uint32_t length) {
    unsigned char header[TC_IPC_HEADER_SIZE];
    if (length > TC_IPC_MAX_PAYLOAD || (length && payload == NULL)) return -1;
    memset(header, 0, sizeof(header));
    put32(header, TC_IPC_MAGIC); put16(header + 4, TC_IPC_VERSION);
    put16(header + 6, type); put16(header + 8, role);
    put64(header + 12, instance); put64(header + 20, generation);
    put32(header + 28, length);
    if (write_all(fd, header, sizeof(header)) != 0) return -1;
    return length ? write_all(fd, payload, length) : 0;
}

static int read_all(int fd, unsigned char *data, size_t length) {
    size_t used = 0;
    while (used < length) {
        ssize_t got = read(fd, data + used, length - used);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && used == 0) return -2;
        if (got <= 0) return got == 0 && used == 0 ? 0 : -1;
        used += (size_t)got;
    }
    return 1;
}

int tc_ipc_recv(int fd, struct tc_ipc_message *message) {
    unsigned char header[TC_IPC_HEADER_SIZE];
    int rc = read_all(fd, header, sizeof(header));
    if (rc <= 0) return rc;
    if (get32(header) != TC_IPC_MAGIC || get16(header + 4) != TC_IPC_VERSION) return -1;
    memset(message, 0, sizeof(*message));
    message->type = get16(header + 6); message->role = get16(header + 8);
    message->instance = get64(header + 12); message->generation = get64(header + 20);
    message->length = get32(header + 28);
    if (message->length > TC_IPC_MAX_PAYLOAD) return -1;
    return message->length ? read_all(fd, message->payload, message->length) : 1;
}

int tc_ipc_worker_handshake(int fd, uint16_t role, uint64_t *instance,
                            uint64_t *generation) {
    struct tc_ipc_message message;
    int rc = tc_ipc_recv(fd, &message);
    if (rc != 1 || message.type != TC_IPC_INIT || message.role != role ||
        message.instance == 0 || message.generation == 0) return -1;
    *instance = message.instance; *generation = message.generation;
    return 0;
}
