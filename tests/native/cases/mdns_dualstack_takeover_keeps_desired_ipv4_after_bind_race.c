#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

int fake_socket(int domain, int type, int protocol);
int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen);
int fake_bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen);
int fake_close(int fd);
int fake_system(const char *cmd);
int fake_usleep(useconds_t usec);
FILE *fake_popen(const char *cmd, const char *mode);
char *fake_fgets(char *s, int size, FILE *stream);
int fake_pclose(FILE *fp);

#include "mdns/mdns.h"

static int next_fd = 100;
static int ipv4_bind_failures_remaining;
static int ipv4_bind_attempts;
static int ipv6_bind_attempts;

int fake_socket(int domain, int type, int protocol) {
    (void)domain;
    (void)type;
    (void)protocol;
    return next_fd++;
}

int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen) {
    (void)sockfd;
    (void)level;
    (void)optname;
    (void)optval;
    (void)optlen;
    return 0;
}

int fake_bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    (void)sockfd;
    if (addrlen >= sizeof(struct sockaddr_in) && addr != NULL && addr->sa_family == AF_INET) {
        ipv4_bind_attempts++;
        if (ipv4_bind_failures_remaining > 0) {
            ipv4_bind_failures_remaining--;
            errno = EADDRINUSE;
            return -1;
        }
    } else if (addrlen >= sizeof(struct sockaddr_in6) && addr != NULL && addr->sa_family == AF_INET6) {
        ipv6_bind_attempts++;
    }
    return 0;
}

int fake_close(int fd) {
    (void)fd;
    return 0;
}

int fake_system(const char *cmd) {
    (void)cmd;
    return 0;
}

int fake_usleep(useconds_t usec) {
    (void)usec;
    return 0;
}

FILE *fake_popen(const char *cmd, const char *mode) {
    (void)cmd;
    (void)mode;
    return NULL;
}

char *fake_fgets(char *s, int size, FILE *stream) {
    (void)s;
    (void)size;
    (void)stream;
    return NULL;
}

int fake_pclose(FILE *fp) {
    (void)fp;
    return 0;
}

static void make_desired_links(struct link_context_set *links) {
    struct in6_addr ula;
    memset(links, 0, sizeof(*links));
    append_link_ipv4(links, "bridge0", inet_addr("10.0.1.40"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula) != 1) {
        return;
    }
    append_link_ipv6(links, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
}

int main(void) {
    struct link_context_set desired;
    struct link_context_set active;
    struct mdns_socket_pair sockets;
    struct mdns_transport_status status;
    int rc;

    make_desired_links(&desired);
    memset(&active, 0, sizeof(active));
    sockets.ipv4_fd = -1;
    sockets.ipv6_fd = -1;
    ipv4_bind_failures_remaining = 2;
    rc = acquire_dualstack_mdns_sockets(0, &desired, &active, &sockets, &status);
    if (rc != 0) {
        return 1;
    }
    if (!link_contexts_need_ipv4_socket(&desired) || !link_contexts_need_ipv6_socket(&desired)) {
        return 2;
    }
    if (!status.active_ipv4 || !status.active_ipv6 || status.missing_required_ipv4) {
        return 3;
    }
    if (ipv4_bind_attempts < 3 || ipv6_bind_attempts < 1) {
        return 4;
    }
    close_mdns_socket_pair(&sockets);

    make_desired_links(&desired);
    memset(&active, 0, sizeof(active));
    sockets.ipv4_fd = -1;
    sockets.ipv6_fd = -1;
    ipv4_bind_attempts = 0;
    ipv6_bind_attempts = 0;
    ipv4_bind_failures_remaining = 100;
    rc = acquire_dualstack_mdns_sockets(0, &desired, &active, &sockets, &status);
    if (rc != 1) {
        return 5;
    }
    if (!link_contexts_need_ipv4_socket(&desired) || !status.missing_required_ipv4) {
        return 6;
    }
    if (status.active_ipv4 || !status.active_ipv6) {
        return 7;
    }
    if (ipv4_bind_attempts <= 1) {
        return 8;
    }
    close_mdns_socket_pair(&sockets);
    return 0;
}
