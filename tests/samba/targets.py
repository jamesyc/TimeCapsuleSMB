# Loaded by the regression preparation step in the actual Samba source tree.
# The VFS tests include production .c files, as Samba's own VFS tests do.
for name in ('tc_aio_fork_test', 'tc_durable_reconnect_test'):
    bld.SAMBA3_BINARY(name, source=name + '.c', deps='smbd_base', cflags='-g', install=False)  # noqa: F821
