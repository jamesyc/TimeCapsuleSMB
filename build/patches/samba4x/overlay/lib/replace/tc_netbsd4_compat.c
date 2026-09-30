/* Included by replace.c (patch 0002) for TC_SAMBA4X_NETBSD4_COMPAT. */

/*
 * NetBSD 4 libc functions that Samba uses and NetBSD 4 lacks. The file-system
 * calls it also lacks (the *at family, futimens and fdopendir) are emulated
 * for both appliance kernels in tc_at_emulation.c.
 */

void arc4random_buf(void *buf, size_t n)
{
	unsigned char *p = buf;
	size_t done = 0;
	int fd = open("/dev/urandom", O_RDONLY);
	if (fd != -1) {
		while (done < n) {
			ssize_t ret = read(fd, p + done, n - done);
			if (ret == -1 && errno == EINTR) {
				continue;
			}
			if (ret <= 0) {
				break;
			}
			done += ret;
		}
		close(fd);
		if (done == n) {
			return;
		}
	}

	/*
	 * Samba/GnuTLS may use this for security-sensitive randomness. A weak
	 * random() fallback would hide a serious platform failure, so fail hard.
	 */
	abort();
}

ssize_t getline(char **lineptr, size_t *n, FILE *stream)
{
	int c = 0;
	size_t pos = 0;
	char *new_line = NULL;
	size_t new_size = 0;

	if (lineptr == NULL || n == NULL || stream == NULL) {
		errno = EINVAL;
		return -1;
	}
	if (*lineptr == NULL || *n == 0) {
		*n = 128;
		*lineptr = malloc(*n);
		if (*lineptr == NULL) {
			return -1;
		}
	}

	while ((c = fgetc(stream)) != EOF) {
		if (pos + 1 >= *n) {
			new_size = *n * 2;
			if (new_size <= *n) {
				errno = ENOMEM;
				return -1;
			}
			new_line = realloc(*lineptr, new_size);
			if (new_line == NULL) {
				return -1;
			}
			*lineptr = new_line;
			*n = new_size;
		}
		(*lineptr)[pos++] = (char)c;
		if (c == '\n') {
			break;
		}
	}
	if (pos == 0 && c == EOF) {
		return -1;
	}
	(*lineptr)[pos] = '\0';
	return (ssize_t)pos;
}
