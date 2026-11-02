/* Stands in for NetBSD's <sys/sysctl.h> in tests/native/unit/test_samba_iflist.c:
 * the parser under test never calls sysctl(), and glibc has no such header. */
#define CTL_NET 4
#define NET_RT_IFLIST 3
static inline int sysctl(const int *name, unsigned int namelen, void *old, size_t *oldlen,
			 const void *new, size_t newlen)
{
	(void)name; (void)namelen; (void)old; (void)oldlen; (void)new; (void)newlen;
	errno = ENOSYS;
	return -1;
}
