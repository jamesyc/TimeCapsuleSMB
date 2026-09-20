# Loaded by the regression preparation step in the actual Samba source tree.
# The VFS tests include production .c files, as Samba's own VFS tests do.
for name in ('tc_aio_fork_test', 'tc_durable_reconnect_test', 'tc_streams_xattr_test',
             'tc_native_metadata_test', 'tc_xattr_migrate_test', 'tc_storage_reload_test'):
    # Shared-module host builds do not inherit the stream module's dependencies
    # through smbd_base, unlike the static appliance build.
    if name == 'tc_streams_xattr_test':
        deps = 'smbd_base HASH_INODE'
    elif name == 'tc_native_metadata_test':
        deps = 'smbd_base HASH_INODE ADOUBLE OFFLOAD_TOKEN STRING_REPLACE dbwrap xattr_tdb'
    elif name == 'tc_xattr_migrate_test':
        deps = 'smbd_base dbwrap xattr_tdb'
    else:
        deps = 'smbd_base'
    bld.SAMBA3_BINARY(name, source=name + '.c', deps=deps, cflags='-g', install=False)  # noqa: F821
