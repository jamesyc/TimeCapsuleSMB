#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

int fake_socket(int domain, int type, int protocol);
int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen);
int fake_bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen);
int fake_close(int fd);

#include "mdns/mdns.h"

static int socket_calls;
static int bind_calls;
static int close_calls;
static int membership_sets;
static int drop_membership_sets;
static int outbound_sets;
static int fail_ipv6_socket;
static int fail_second_membership;
static int next_fd = 100;

static void reset_fakes(void) {
    socket_calls = 0;
    bind_calls = 0;
    close_calls = 0;
    membership_sets = 0;
    drop_membership_sets = 0;
    outbound_sets = 0;
    fail_ipv6_socket = 0;
    fail_second_membership = 0;
    next_fd = 100;
}

int fake_socket(int domain, int type, int protocol) {
    (void)type;
    (void)protocol;
    socket_calls++;
    if (fail_ipv6_socket && domain == AF_INET6) {
        errno = EAFNOSUPPORT;
        return -1;
    }
    return next_fd++;
}

int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen) {
    (void)sockfd;
    (void)optval;
    (void)optlen;
    if (level == IPPROTO_IP && optname == IP_ADD_MEMBERSHIP) {
        membership_sets++;
        if (fail_second_membership && membership_sets >= 2) {
            errno = EADDRINUSE;
            return -1;
        }
    }
#ifdef IP_DROP_MEMBERSHIP
    if (level == IPPROTO_IP && optname == IP_DROP_MEMBERSHIP) {
        drop_membership_sets++;
    }
#endif
    if (level == IPPROTO_IP && optname == IP_MULTICAST_IF) {
        outbound_sets++;
    }
    return 0;
}

int fake_bind(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    (void)sockfd;
    (void)addr;
    (void)addrlen;
    bind_calls++;
    return 0;
}

int fake_close(int fd) {
    (void)fd;
    close_calls++;
    return 0;
}

static void add_ipv4_link(struct link_context_set *set, const char *name, const char *addr) {
    append_link_ipv4(set, name, inet_addr(addr), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
}

int main(void) {
    struct link_context_set old_links;
    struct link_context_set new_links;
    struct mdns_socket_pair sockets;
    struct in6_addr ula;

    reset_fakes();
    memset(&old_links, 0, sizeof(old_links));
    memset(&new_links, 0, sizeof(new_links));
    add_ipv4_link(&old_links, "bridge0", "10.0.1.1");
    add_ipv4_link(&new_links, "bridge0", "10.0.1.1");
    add_ipv4_link(&new_links, "en1", "192.168.50.2");
    add_ipv4_link(&new_links, "en2", "192.168.60.2");
    sockets.ipv4_fd = 55;
    sockets.ipv6_fd = -1;
    fail_second_membership = 1;
    if (prepare_runtime_mdns_sockets_for_links(0, &sockets, &old_links, &new_links) != 0) {
        return 1;
    }
#ifdef IP_DROP_MEMBERSHIP
    if (drop_membership_sets != 0) {
        return 2;
    }
#endif
    if (sockets.ipv4_fd != 55 || close_calls != 0 || membership_sets != 3 || outbound_sets != 1) {
        return 3;
    }
    if (new_links.count != 2 ||
        strcmp(new_links.links[0].name, "bridge0") != 0 ||
        strcmp(new_links.links[1].name, "en1") != 0) {
        return 16;
    }

    reset_fakes();
    memset(&new_links, 0, sizeof(new_links));
    add_ipv4_link(&new_links, "bridge0", "10.0.1.1");
    if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula) != 1) {
        return 4;
    }
    append_link_ipv6(&new_links, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
    fail_ipv6_socket = 1;
    sockets.ipv4_fd = -1;
    sockets.ipv6_fd = -1;
    if (open_dualstack_mdns_sockets(0, &new_links, 0, &sockets) != 0) {
        return 5;
    }
    if (sockets.ipv4_fd < 0 || sockets.ipv6_fd >= 0 || link_contexts_need_ipv6_socket(&new_links)) {
        return 6;
    }
    close_mdns_socket_pair(&sockets);
    printf("ok\n");
    return 0;
}
