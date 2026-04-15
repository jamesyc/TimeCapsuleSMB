#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

mkdir -p "$OUT" "$SAMBA4_WORK"

{
    echo "Starting Samba 4 download workflow at $(date -u)"
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "SAMBA4_VERSION=$SAMBA4_VERSION"
    echo "SAMBA4_GIT_URL=$SAMBA4_GIT_URL"
    echo "SAMBA4_GIT_REF=$SAMBA4_GIT_REF"
    echo "SAMBA4_SRC_DIR=$SAMBA4_SRC_DIR"

    # Samba 4.2 waf expects a host-side Python 2 environment on the VM.
    echo "This part is installing Python 2.7 on the VM with pkgin so Samba 4 waf can find the host interpreter and headers."
    if pkg_info python27 >/dev/null 2>&1; then
        echo "python27 is already installed on the VM; skipping pkgin install."
    else
        /usr/pkg/bin/pkgin -4 -y install python27
    fi

    if [ -d "$SAMBA4_SRC_DIR/.git" ]; then
        printf 'Refreshing existing git checkout at %s\n' "$SAMBA4_SRC_DIR"
        git -C "$SAMBA4_SRC_DIR" fetch --depth 1 origin "$SAMBA4_GIT_REF"
        git -C "$SAMBA4_SRC_DIR" checkout -B "$SAMBA4_GIT_REF" "FETCH_HEAD"
        git -C "$SAMBA4_SRC_DIR" reset --hard "FETCH_HEAD"
    elif [ -d "$SAMBA4_SRC_DIR" ]; then
        printf 'Removing existing non-git Samba source tree at %s\n' "$SAMBA4_SRC_DIR"
        rm -rf "$SAMBA4_SRC_DIR"
        git clone --depth 1 --branch "$SAMBA4_GIT_REF" "$SAMBA4_GIT_URL" "$SAMBA4_SRC_DIR"
    else
        git clone --depth 1 --branch "$SAMBA4_GIT_REF" "$SAMBA4_GIT_URL" "$SAMBA4_SRC_DIR"
    fi

    perl -0pi -e 's/perl_inc = read_perl_config_var\('\''print "\@INC"'\''\)\n(?:\s*if '\''\.'\'' in perl_inc:\n)*(?:\s*perl_inc\.remove\('\''\.'\''\)\n)?/perl_inc = read_perl_config_var('\''print "\@INC"'\'')\n    if '\''.'\'' in perl_inc:\n        perl_inc.remove('\''.'\'')\n/s' \
        "$SAMBA4_SRC_DIR/buildtools/wafsamba/samba_perl.py"
    perl -0pi -e 's/#ifndef PRINT_MAX_JOBID/#include <time.h>\n\n#ifndef PRINT_MAX_JOBID/' \
        "$SAMBA4_SRC_DIR/lib/param/loadparm.h"
    perl -0pi -e 's/conf\.SAMBA_CHECK_PYTHON_HEADERS\(mandatory=True\)/conf.SAMBA_CHECK_PYTHON_HEADERS(mandatory=False)/g' \
        "$SAMBA4_SRC_DIR/wscript" \
        "$SAMBA4_SRC_DIR/ctdb/wscript" \
        "$SAMBA4_SRC_DIR/lib/ldb/wscript"
    perl -0pi -e 's/conf\.SAMBA_CHECK_PYTHON_HEADERS\(mandatory=\(not conf\.env\.disable_python\)\)/conf.SAMBA_CHECK_PYTHON_HEADERS(mandatory=False)/g' \
        "$SAMBA4_SRC_DIR/wscript" \
        "$SAMBA4_SRC_DIR/lib/ldb/wscript"
    perl -0pi -e 's/conf\.SAMBA_CHECK_PYTHON_HEADERS\(mandatory=not conf\.env\.disable_python\)/conf.SAMBA_CHECK_PYTHON_HEADERS(mandatory=False)/g' \
        "$SAMBA4_SRC_DIR/lib/ldb/wscript"
    perl -0pi -e 's/conf\.SAMBA_CHECK_PYTHON_HEADERS\(mandatory=False\)\n/conf.SAMBA_CHECK_PYTHON_HEADERS(mandatory=False)\n    conf.env.disable_python = not conf.env.HAVE_PYTHON_H\n/' \
        "$SAMBA4_SRC_DIR/wscript"
    perl -0pi -e 's/enabled=enabled\)/enabled=(enabled and not bld.env.disable_python and bld.CONFIG_SET('\''HAVE_PYTHON_H'\'')))/' \
        "$SAMBA4_SRC_DIR/buildtools/wafsamba/samba_python.py"
    # dynconfig.c includes replace.h, which includes generated config.h. On a
    # clean waf tree that file lives under bin/default/include, so dynconfig
    # needs the "include" build path explicitly or the compile dies on
    # "config.h: No such file or directory".
    perl -0pi -e "s/deps='replace',\\n/deps='replace',\\n                        includes='include',\\n/" \
        "$SAMBA4_SRC_DIR/dynconfig/wscript"

    # Samba 4.8 bundles a very old waf. During long cross-configure runs it can
    # attempt to save .wafpickle under a transient conf-check build directory
    # before recreating that directory, which aborts configure with ENOENT.
    # Create the cache directory defensively before writing the temporary file.
    awk '
        {
            print
            if ($0 ~ /^[[:space:]]*db = os\.path\.join\(self\.bdir, DBFILE\)/ &&
                inserted != 1) {
                print "\t\ttry: os.makedirs(self.bdir)"
                print "\t\texcept OSError: pass"
                inserted = 1
            }
        }
    ' "$SAMBA4_SRC_DIR/third_party/waf/wafadmin/Build.py" >"$SAMBA4_SRC_DIR/third_party/waf/wafadmin/Build.py.tmp"
    mv "$SAMBA4_SRC_DIR/third_party/waf/wafadmin/Build.py.tmp" \
        "$SAMBA4_SRC_DIR/third_party/waf/wafadmin/Build.py"
    awk '
        {
            if ($0 ~ /^[[:space:]]*dest = open\(os\.path\.join\(dir, test_f_name\), '\''w'\''\)/ &&
                inserted != 1) {
                print "\ttry: os.makedirs(dir)"
                print "\texcept OSError: pass"
                inserted = 1
            }
            print
        }
    ' "$SAMBA4_SRC_DIR/third_party/waf/wafadmin/Tools/config_c.py" >"$SAMBA4_SRC_DIR/third_party/waf/wafadmin/Tools/config_c.py.tmp"
    mv "$SAMBA4_SRC_DIR/third_party/waf/wafadmin/Tools/config_c.py.tmp" \
        "$SAMBA4_SRC_DIR/third_party/waf/wafadmin/Tools/config_c.py"

    # On the NetBSD 4 Time Capsule, Samba's first talloc NULL-context
    # allocation is stamped with the constructor-derived magic value, then the
    # process later observes the original non-random static initializer again.
    # The next talloc(NULL, ...) path aborts with "Bad talloc magic value" in
    # lp_set_logfile(). Keep talloc's magic stable for this static NetBSD4
    # target by disabling the randomized constructor update.
    if [ "$SDK_FAMILY" = "netbsd4" ]; then
        perl -0pi -e 's/(void talloc_lib_init\(void\)\n\{\n)/$1\treturn;\n\n/s' \
            "$SAMBA4_SRC_DIR/lib/talloc/talloc.c"
    fi

    awk '
        BEGIN { wrap = 0 }
        !wrap && /^bld\.SAMBA_SUBSYSTEM\('\''pyrpc_util'\''/ {
            print "if not bld.env.disable_python:"
            wrap = 1
        }
        {
            if (wrap) {
                print "    " $0
                if ($0 ~ /^\t\)$/) {
                    print "else:"
                    print "    bld.SAMBA_SUBSYSTEM('\''pyrpc_util'\'', source='\'''\'')"
                    wrap = 0
                }
            } else {
                print
            }
        }
    ' "$SAMBA4_SRC_DIR/source4/librpc/wscript_build" >"$SAMBA4_SRC_DIR/source4/librpc/wscript_build.tmp"
    mv "$SAMBA4_SRC_DIR/source4/librpc/wscript_build.tmp" "$SAMBA4_SRC_DIR/source4/librpc/wscript_build"

    awk '
        BEGIN { wrap = 0 }
        !wrap && /^bld\.SAMBA_SUBSYSTEM\('\''PROVISION'\''/ {
            print "if not bld.env.disable_python:"
            wrap = 1
        }
        {
            if (wrap) {
                print "    " $0
                if ($0 ~ /^\t\)$/) {
                    print "else:"
                    print "    bld.SAMBA_SUBSYSTEM('\''PROVISION'\'', source='\'''\'')"
                    wrap = 0
                }
            } else {
                print
            }
        }
    ' "$SAMBA4_SRC_DIR/source4/param/wscript_build" >"$SAMBA4_SRC_DIR/source4/param/wscript_build.tmp"
    mv "$SAMBA4_SRC_DIR/source4/param/wscript_build.tmp" "$SAMBA4_SRC_DIR/source4/param/wscript_build"

    awk '
        BEGIN { wrap = 0 }
        !wrap && /^bld\.SAMBA_SUBSYSTEM\('\''pyparam_util'\''/ {
            print "if not bld.env.disable_python:"
            wrap = 1
        }
        {
            if (wrap) {
                print "    " $0
                if ($0 ~ /^\t\)$/) {
                    print "else:"
                    print "    bld.SAMBA_SUBSYSTEM('\''pyparam_util'\'', source='\'''\'')"
                    wrap = 0
                }
            } else {
                print
            }
        }
    ' "$SAMBA4_SRC_DIR/source4/param/wscript_build" >"$SAMBA4_SRC_DIR/source4/param/wscript_build.tmp"
    mv "$SAMBA4_SRC_DIR/source4/param/wscript_build.tmp" "$SAMBA4_SRC_DIR/source4/param/wscript_build"

    cat >"$SAMBA4_SRC_DIR/python/wscript_build" <<'EOF'
#!/usr/bin/env python

if not bld.env.disable_python:
    bld.SAMBA_LIBRARY('samba_python',
        source=[],
        deps='LIBPYTHON pytalloc-util pyrpc_util',
        grouping_library=True,
        private_library=True,
        pyembed=True)

    bld.SAMBA_SUBSYSTEM('LIBPYTHON',
        source='modules.c',
        public_deps='',
        init_function_sentinel='{NULL,NULL}',
        deps='talloc',
        pyext=True,
        )

    bld.SAMBA_PYTHON('python_uuid',
        source='uuidmodule.c',
        deps='ndr',
        realname='uuid.so',
        enabled = float(bld.env.PYTHON_VERSION) <= 2.4
        )

    bld.SAMBA_PYTHON('python_glue',
        source='pyglue.c',
        deps='pyparam_util samba-util netif pytalloc-util',
        realname='samba/_glue.so'
        )

    bld.SAMBA_SCRIPT('samba_python_files',
        pattern='samba/**/*.py',
        installdir='python')

    bld.INSTALL_WILDCARD('${PYTHONARCHDIR}', 'samba/**/*.py', flat=False)
EOF

    # The native NetBSD 6 getifaddrs() probe can hang on the Time Capsule.
    # Force Samba onto the older libreplace interface enumeration path.
    perl -0pi -e "s/conf\\.CHECK_FUNCS\\('timegm getifaddrs freeifaddrs mmap setgroups syscall setsid'\\)/conf.CHECK_FUNCS('timegm mmap setgroups syscall setsid')/" \
        "$SAMBA4_SRC_DIR/lib/replace/wscript"
    perl -0pi -e "s/for method in \\['HAVE_IFACE_GETIFADDRS', 'HAVE_IFACE_AIX', 'HAVE_IFACE_IFCONF', 'HAVE_IFACE_IFREQ'\\]:/for method in ['HAVE_IFACE_IFCONF', 'HAVE_IFACE_IFREQ', 'HAVE_IFACE_AIX']:/" \
        "$SAMBA4_SRC_DIR/lib/replace/wscript"

    # The NetBSD Time Capsule build only needs file serving plus the macOS
    # Time Machine VFS stack. Replace the printing/spoolss implementation with
    # small stubs so the build does not pull in the printer stack.
    cat >"$SAMBA4_SRC_DIR/source3/printing/notify_disabled.c" <<'EOF'
#include "includes.h"
#include "printing/notify.h"

int print_queue_snum(const char *qname)
{
	return -1;
}

void print_notify_send_messages(struct messaging_context *msg_ctx,
				unsigned int timeout) {}
void notify_printer_status_byname(struct tevent_context *ev,
				  struct messaging_context *msg_ctx,
				  const char *sharename, uint32_t status) {}
void notify_printer_status(struct tevent_context *ev,
			   struct messaging_context *msg_ctx,
			   int snum, uint32_t status) {}
void notify_job_status_byname(struct tevent_context *ev,
			      struct messaging_context *msg_ctx,
			      const char *sharename, uint32_t jobid,
			      uint32_t status, uint32_t flags) {}
void notify_job_status(struct tevent_context *ev,
		       struct messaging_context *msg_ctx,
		       const char *sharename, uint32_t jobid, uint32_t status) {}
void notify_job_total_bytes(struct tevent_context *ev,
			    struct messaging_context *msg_ctx,
			    const char *sharename, uint32_t jobid,
			    uint32_t size) {}
void notify_job_total_pages(struct tevent_context *ev,
			    struct messaging_context *msg_ctx,
			    const char *sharename, uint32_t jobid,
			    uint32_t pages) {}
void notify_job_username(struct tevent_context *ev,
			 struct messaging_context *msg_ctx,
			 const char *sharename, uint32_t jobid, char *name) {}
void notify_job_name(struct tevent_context *ev,
		     struct messaging_context *msg_ctx,
		     const char *sharename, uint32_t jobid, char *name) {}
void notify_job_submitted(struct tevent_context *ev,
			  struct messaging_context *msg_ctx,
			  const char *sharename, uint32_t jobid,
			  time_t submitted) {}
void notify_printer_driver(struct tevent_context *ev,
			   struct messaging_context *msg_ctx,
			   int snum, const char *driver_name) {}
void notify_printer_comment(struct tevent_context *ev,
			    struct messaging_context *msg_ctx,
			    int snum, const char *comment) {}
void notify_printer_sharename(struct tevent_context *ev,
			      struct messaging_context *msg_ctx,
			      int snum, const char *share_name) {}
void notify_printer_printername(struct tevent_context *ev,
				struct messaging_context *msg_ctx,
				int snum, const char *printername) {}
void notify_printer_port(struct tevent_context *ev,
			 struct messaging_context *msg_ctx,
			 int snum, const char *port_name) {}
void notify_printer_location(struct tevent_context *ev,
			     struct messaging_context *msg_ctx,
			     int snum, const char *location) {}
void notify_printer_sepfile(struct tevent_context *ev,
			    struct messaging_context *msg_ctx,
			    int snum, const char *sepfile) {}
void notify_printer_byname(struct tevent_context *ev,
			   struct messaging_context *msg_ctx,
			   const char *printername, uint32_t attribute,
			   const char *value) {}
EOF

    cat >"$SAMBA4_SRC_DIR/source3/printing/queue_process_disabled.c" <<'EOF'
#include "includes.h"
#include "printing/load.h"
#include "printing/pcap.h"
#include "printing/queue_process.h"

bool printing_subsystem_init(struct tevent_context *ev_ctx,
			     struct messaging_context *msg_ctx,
			     bool start_daemons,
			     bool background_queue)
{
	return true;
}

void printing_subsystem_update(struct tevent_context *ev_ctx,
			       struct messaging_context *msg_ctx,
			       bool force) {}

pid_t start_background_queue(struct tevent_context *ev,
			     struct messaging_context *msg,
			     char *logfile)
{
	return (pid_t)-1;
}

bool pcap_cache_loaded(time_t *_last_change)
{
	return false;
}

bool pcap_printername_ok(const char *printername)
{
	return false;
}

void load_printers(struct tevent_context *ev,
		   struct messaging_context *msg_ctx) {}

void update_monitored_printq_cache(struct messaging_context *msg_ctx) {}
EOF

    cat >"$SAMBA4_SRC_DIR/source3/printing/printspoolss_disabled.c" <<'EOF'
#include "includes.h"
#include "printing.h"

NTSTATUS print_spool_open(files_struct *fsp,
			  const char *fname,
			  uint64_t current_vuid)
{
	return NT_STATUS_NOT_SUPPORTED;
}

int print_spool_write(files_struct *fsp, const char *data, uint32_t size,
		      off_t offset, uint32_t *written)
{
	if (written != NULL) {
		*written = 0;
	}
	errno = ENOSYS;
	return -1;
}

void print_spool_end(files_struct *fsp, enum file_close_type close_type) {}

void print_spool_terminate(struct connection_struct *conn,
			   struct print_file_data *print_file) {}

uint16_t print_spool_rap_jobid(struct print_file_data *print_file)
{
	return 0;
}
EOF

    cat >"$SAMBA4_SRC_DIR/source3/printing/printing_disabled.c" <<'EOF'
#include "includes.h"
#include "printing.h"

static int disabled_queue_get(const char *printer_name,
			      enum printing_types printing_type,
			      char *lpq_command,
			      print_queue_struct **q,
			      print_status_struct *status)
{
	if (q != NULL) {
		*q = NULL;
	}
	if (status != NULL) {
		ZERO_STRUCTP(status);
	}
	return 0;
}

static int disabled_queue_int(int snum)
{
	return -1;
}

static int disabled_job_action(const char *sharename,
			       const char *lprm_command,
			       struct printjob *pjob)
{
	return -1;
}

static int disabled_job_control(int snum, struct printjob *pjob)
{
	return -1;
}

static int disabled_job_submit(int snum, struct printjob *pjob,
			       enum printing_types printing_type,
			       char *lpq_command)
{
	return -1;
}

struct printif generic_printif = {
	.type = PRINT_BSD,
	.queue_get = disabled_queue_get,
	.queue_pause = disabled_queue_int,
	.queue_resume = disabled_queue_int,
	.job_delete = disabled_job_action,
	.job_pause = disabled_job_control,
	.job_resume = disabled_job_control,
	.job_submit = disabled_job_submit,
};

uint32_t sysjob_to_jobid_pdb(struct tdb_print_db *pdb, int sysjob) { return 0; }
uint32_t sysjob_to_jobid(int unix_jobid) { return 0; }
int jobid_to_sysjob_pdb(struct tdb_print_db *pdb, uint32_t jobid) { return -1; }
bool print_notify_register_pid(int snum) { return false; }
bool print_notify_deregister_pid(int snum) { return false; }
bool print_job_exists(const char *sharename, uint32_t jobid) { return false; }
struct spoolss_DeviceMode *print_job_devmode(TALLOC_CTX *mem_ctx,
					     const char *sharename,
					     uint32_t jobid) { return NULL; }
bool print_job_set_name(struct tevent_context *ev,
			struct messaging_context *msg_ctx,
			const char *sharename, uint32_t jobid,
			const char *name) { return false; }
bool print_job_get_name(TALLOC_CTX *mem_ctx, const char *sharename,
			uint32_t jobid, char **name) { return false; }
WERROR print_job_delete(const struct auth_session_info *server_info,
			struct messaging_context *msg_ctx,
			int snum, uint32_t jobid) { return WERR_NOT_SUPPORTED; }
WERROR print_job_pause(const struct auth_session_info *server_info,
		       struct messaging_context *msg_ctx,
		       int snum, uint32_t jobid) { return WERR_NOT_SUPPORTED; }
WERROR print_job_resume(const struct auth_session_info *server_info,
			struct messaging_context *msg_ctx,
			int snum, uint32_t jobid) { return WERR_NOT_SUPPORTED; }
ssize_t print_job_write(struct tevent_context *ev,
			struct messaging_context *msg_ctx,
			int snum, uint32_t jobid,
			const char *buf, size_t size)
{
	errno = ENOSYS;
	return -1;
}

int print_queue_length(struct messaging_context *msg_ctx, int snum,
		       print_status_struct *pstatus)
{
	if (pstatus != NULL) {
		ZERO_STRUCTP(pstatus);
	}
	return 0;
}

WERROR print_job_start(const struct auth_session_info *server_info,
		       struct messaging_context *msg_ctx,
		       const char *clientmachine,
		       int snum, const char *docname, const char *filename,
		       struct spoolss_DeviceMode *devmode,
		       uint32_t *_jobid) { return WERR_NOT_SUPPORTED; }
void print_job_endpage(struct messaging_context *msg_ctx,
		       int snum, uint32_t jobid) {}
NTSTATUS print_job_end(struct messaging_context *msg_ctx, int snum,
		       uint32_t jobid,
		       enum file_close_type close_type)
{
	return NT_STATUS_NOT_SUPPORTED;
}

int print_queue_status(struct messaging_context *msg_ctx, int snum,
		       print_queue_struct **ppqueue,
		       print_status_struct *status)
{
	if (ppqueue != NULL) {
		*ppqueue = NULL;
	}
	if (status != NULL) {
		ZERO_STRUCTP(status);
	}
	return 0;
}

WERROR print_queue_pause(const struct auth_session_info *server_info,
			 struct messaging_context *msg_ctx,
			 int snum) { return WERR_NOT_SUPPORTED; }
WERROR print_queue_resume(const struct auth_session_info *server_info,
			  struct messaging_context *msg_ctx,
			  int snum) { return WERR_NOT_SUPPORTED; }
WERROR print_queue_purge(const struct auth_session_info *server_info,
			 struct messaging_context *msg_ctx,
			 int snum) { return WERR_NOT_SUPPORTED; }
uint32_t print_queue_c_set(struct messaging_context *msg_ctx, int snum) { return 0; }
void print_queue_update(struct messaging_context *msg_ctx, int snum) {}
uint16_t pjobid_to_rap(const char *sharename, uint32_t jobid) { return 0; }
bool rap_to_pjobid(uint16_t rap_jobid, fstring sharename, uint32_t *pjobid)
{
	return false;
}

void rap_jobid_delete(const char *sharename, uint32_t jobid) {}
bool print_backend_init(struct messaging_context *msg_ctx) { return true; }
void printing_end(void) {}

bool parse_lpq_entry(enum printing_types printing_type, char *line,
		     print_queue_struct *buf,
		     print_status_struct *status, bool first)
{
	return false;
}

struct tdb_print_db *get_print_db_byname(const char *printername) { return NULL; }
void release_print_db(struct tdb_print_db *pdb) {}
void close_all_print_db(void) {}
TDB_DATA get_printer_notify_pid_list(struct tdb_context *tdb,
				     const char *printer_name,
				     bool cleanlist)
{
	TDB_DATA data = { NULL, 0 };
	return data;
}

void print_queue_receive(struct messaging_context *msg,
			 void *private_data,
			 uint32_t msg_type,
			 struct server_id server_id,
			 DATA_BLOB *data) {}
EOF

    cat >"$SAMBA4_SRC_DIR/source3/rpc_server/spoolss/spoolss_disabled.c" <<'EOF'
#include "includes.h"
#include "rpc_server/spoolss/srv_spoolss_nt.h"

void srv_spoolss_cleanup(void) {}
EOF

    cat >"$SAMBA4_SRC_DIR/source3/rpc_server/spoolss/iremotewinspool_disabled.c" <<'EOF'
#include "includes.h"
EOF

    cat >"$SAMBA4_SRC_DIR/source3/rpc_server/wkssvc/wkssvc_disabled.c" <<'EOF'
#include "includes.h"
EOF

    perl -0pi -e "s/printing\\/printspoolss\\.c/printing\\/printspoolss_disabled.c/" \
        "$SAMBA4_SRC_DIR/source3/wscript_build"
    perl -0pi -e "s/bld\\.SAMBA3_SUBSYSTEM\\('PRINTBASE',\\n\\s*source='''\\n\\s*printing\\/notify\\.c\\n\\s*printing\\/printing_db\\.c\\n\\s*'''/bld.SAMBA3_SUBSYSTEM('PRINTBASE',\\n                    source='''\\n                           printing\\/notify_disabled.c\\n                           printing\\/queue_process_disabled.c\\n                           '''/s" \
        "$SAMBA4_SRC_DIR/source3/wscript_build"
    perl -0pi -e "s/bld\\.SAMBA3_SUBSYSTEM\\('PRINTBACKEND',\\n\\s*source='''\\n.*?\\n\\s*'''/bld.SAMBA3_SUBSYSTEM('PRINTBACKEND',\\n                    source='''\\n                           printing\\/printing_disabled.c\\n                           '''/s" \
        "$SAMBA4_SRC_DIR/source3/wscript_build"
    perl -0pi -e "s/bld\\.SAMBA3_SUBSYSTEM\\('PRINTING',\\n\\s*source='''\\n.*?\\n\\s*'''/bld.SAMBA3_SUBSYSTEM('PRINTING',\\n                    source='''\\n                           printing\\/printing_disabled.c\\n                           '''/s" \
        "$SAMBA4_SRC_DIR/source3/wscript_build"

    perl -0pi -e "s/bld\\.SAMBA3_SUBSYSTEM\\('RPC_SPOOLSS',\\n\\s*source='''.*?''',\\n\\s*deps='.*?'\\)/bld.SAMBA3_SUBSYSTEM('RPC_SPOOLSS',\\n                    source='''spoolss\\/spoolss_disabled.c''',\\n                    deps='')/s" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/wscript_build"
    perl -0pi -e "s/bld\\.SAMBA3_SUBSYSTEM\\('RPC_IREMOTEWINSPOOL',\\n\\s*source='''.*?''',\\n\\s*deps='RPC_SPOOLSS'\\)/bld.SAMBA3_SUBSYSTEM('RPC_IREMOTEWINSPOOL',\\n                    source='''spoolss\\/iremotewinspool_disabled.c''',\\n                    deps='')/s" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/wscript_build"
    perl -0pi -e "s/bld\\.SAMBA3_SUBSYSTEM\\('RPC_WKSSVC',\\n\\s*source='''.*?''',\\n\\s*deps='LIBNET'\\)/bld.SAMBA3_SUBSYSTEM('RPC_WKSSVC',\\n                    source='''wkssvc\\/wkssvc_disabled.c''',\\n                    deps='')/s" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/wscript_build"
    perl -0pi -e "s/\\n\\s*RPC_SPOOLSS\\n\\s*RPC_IREMOTEWINSPOOL//s" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/wscript_build"
    perl -0pi -e "s/\\n\\s*RPC_WKSSVC\\n//s" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/wscript_build"

    perl -0pi -e "s/static bool rpc_setup_spoolss\\(.*?\\n\\}\\n\\n/static bool rpc_setup_spoolss(struct tevent_context *ev_ctx,\\n                              struct messaging_context *msg_ctx)\\n{\\n    return true;\\n}\\n\\n/s" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/rpc_service_setup.c"
    perl -0pi -e "s/static bool rpc_setup_wkssvc\\(.*?\\n\\}\\n\\n/static bool rpc_setup_wkssvc(struct tevent_context *ev_ctx,\\n                             struct messaging_context *msg_ctx)\\n{\\n    return true;\\n}\\n\\n/s" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/rpc_service_setup.c"
    perl -0pi -e 's/^\s*rpc_spoolss_shutdown\(\);\n//m' \
        "$SAMBA4_SRC_DIR/source3/smbd/server_exit.c"
    perl -0pi -e 's/^\s*rpc_wkssvc_shutdown\(\);\n//m' \
        "$SAMBA4_SRC_DIR/source3/smbd/server_exit.c"
    perl -0pi -e "s/bool lp_disable_spoolss\\( void \\)\\n\\{\\n.*?\\n\\}/bool lp_disable_spoolss( void )\\n{\\n\\treturn true;\\n\\}/s" \
        "$SAMBA4_SRC_DIR/source3/param/loadparm.c"
    perl -0pi -e "s/epmapper wkssvc rpcecho/epmapper rpcecho/" \
        "$SAMBA4_SRC_DIR/source3/param/loadparm.c"
    perl -0pi -e "s/static bool api_DosPrintQGetInfo\\(.*?^\\}/static bool api_DosPrintQGetInfo(struct smbd_server_connection *sconn,\\n\\t\\t\\t connection_struct *conn, uint64_t vuid,\\n\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_DosPrintQEnum\\(.*?^\\}/static bool api_DosPrintQEnum(struct smbd_server_connection *sconn,\\n\\t\\t\\t      connection_struct *conn, uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt, int mprcnt,\\n\\t\\t\\t\\tchar **rdata, char** rparam,\\n\\t\\t\\t\\tint *rdata_len, int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_PrintJobInfo\\(.*?^\\}/static bool api_PrintJobInfo(struct smbd_server_connection *sconn,\\n\\t\\t\\t     connection_struct *conn, uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_WPrintJobGetInfo\\(.*?^\\}/static bool api_WPrintJobGetInfo(struct smbd_server_connection *sconn,\\n\\t\\t\\t\\t connection_struct *conn, uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_WPrintDestGetInfo\\(.*?^\\}/static bool api_WPrintDestGetInfo(struct smbd_server_connection *sconn,\\n\\t\\t\\t\\t  connection_struct *conn, uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_WPrintDestEnum\\(.*?^\\}/static bool api_WPrintDestEnum(struct smbd_server_connection *sconn,\\n\\t\\t\\t       connection_struct *conn, uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_WPrintJobEnumerate\\(.*?^\\}/static bool api_WPrintJobEnumerate(struct smbd_server_connection *sconn,\\n\\t\\t\\t   connection_struct *conn, uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_RNetServerGetInfo\\(.*?^\\}/static bool api_RNetServerGetInfo(struct smbd_server_connection *sconn,\\n\\t\\t\\t  connection_struct *conn, uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_NetWkstaGetInfo\\(.*?^\\}/static bool api_NetWkstaGetInfo(struct smbd_server_connection *sconn,\\n\\t\\t\\t\\tconnection_struct *conn,uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/static bool api_RNetSessionEnum\\(.*?^\\}/static bool api_RNetSessionEnum(struct smbd_server_connection *sconn,\\n\\t\\t\\t   connection_struct *conn,uint64_t vuid,\\n\\t\\t\\t\\tchar *param, int tpscnt,\\n\\t\\t\\t\\tchar *data, int tdscnt,\\n\\t\\t\\t\\tint mdrcnt,int mprcnt,\\n\\t\\t\\t\\tchar **rdata,char **rparam,\\n\\t\\t\\t\\tint *rdata_len,int *rparam_len)\\n{\\n\\treturn False;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/lanman.c"
    perl -0pi -e "s/void reply_printqueue\\(struct smb_request \\*req\\)\\n\\{.*?^\\}/void reply_printqueue(struct smb_request *req)\\n{\\n\\treply_nterror(req, NT_STATUS_NOT_SUPPORTED);\\n\\treturn;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/smbd/reply.c"
    perl -0pi -e "s/WERROR _srvsvc_NetFileEnum\\(.*?^\\}/WERROR _srvsvc_NetFileEnum(struct pipes_struct *p,\\n\\t\\t\\t   struct srvsvc_NetFileEnum *r)\\n{\\n\\treturn WERR_NOT_SUPPORTED;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/srvsvc/srv_srvsvc_nt.c"
    perl -0pi -e "s/WERROR _srvsvc_NetSrvGetInfo\\(.*?^\\}/WERROR _srvsvc_NetSrvGetInfo(struct pipes_struct *p,\\n\\t\\t\\t     struct srvsvc_NetSrvGetInfo *r)\\n{\\n\\treturn WERR_NOT_SUPPORTED;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/srvsvc/srv_srvsvc_nt.c"
    perl -0pi -e "s/WERROR _srvsvc_NetSrvSetInfo\\(.*?^\\}/WERROR _srvsvc_NetSrvSetInfo(struct pipes_struct *p,\\n\\t\\t\\t     struct srvsvc_NetSrvSetInfo *r)\\n{\\n\\treturn WERR_NOT_SUPPORTED;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/srvsvc/srv_srvsvc_nt.c"
    perl -0pi -e "s/WERROR _srvsvc_NetConnEnum\\(.*?^\\}/WERROR _srvsvc_NetConnEnum(struct pipes_struct *p,\\n\\t\\t\\t   struct srvsvc_NetConnEnum *r)\\n{\\n\\treturn WERR_NOT_SUPPORTED;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/srvsvc/srv_srvsvc_nt.c"
    perl -0pi -e "s/WERROR _srvsvc_NetSessEnum\\(.*?^\\}/WERROR _srvsvc_NetSessEnum(struct pipes_struct *p,\\n\\t\\t\\t   struct srvsvc_NetSessEnum *r)\\n{\\n\\treturn WERR_NOT_SUPPORTED;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/srvsvc/srv_srvsvc_nt.c"
    perl -0pi -e "s/WERROR _srvsvc_NetSessDel\\(.*?^\\}/WERROR _srvsvc_NetSessDel(struct pipes_struct *p,\\n\\t\\t\\t  struct srvsvc_NetSessDel *r)\\n{\\n\\treturn WERR_NOT_SUPPORTED;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/srvsvc/srv_srvsvc_nt.c"
    perl -0pi -e "s/WERROR _srvsvc_NetRemoteTOD\\(.*?^\\}/WERROR _srvsvc_NetRemoteTOD(struct pipes_struct *p,\\n\\t\\t\\t    struct srvsvc_NetRemoteTOD *r)\\n{\\n\\treturn WERR_NOT_SUPPORTED;\\n\\}/ms" \
        "$SAMBA4_SRC_DIR/source3/rpc_server/srvsvc/srv_srvsvc_nt.c"

    perl -0pi -e "s/SRC = '''tevent\\.c tevent_debug\\.c tevent_fd\\.c tevent_immediate\\.c\\n             tevent_queue\\.c tevent_req\\.c\\n             tevent_poll\\.c tevent_threads\\.c\\n             tevent_signal\\.c tevent_standard\\.c tevent_timed\\.c tevent_util\\.c tevent_wakeup\\.c'''/SRC = '''tevent.c tevent_debug.c tevent_fd.c tevent_immediate.c\\n             tevent_queue.c tevent_req.c\\n             tevent_poll.c\\n             tevent_signal.c tevent_standard.c tevent_timed.c tevent_util.c tevent_wakeup.c'''\\n\\n    if bld.CONFIG_SET('HAVE_PTHREAD'):\\n        SRC += ' tevent_threads.c'/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/wscript"
    perl -0pi -e "s/\\n\\ttevent_poll_init\\(\\);\\n\\ttevent_poll_mt_init\\(\\);/\\n\\ttevent_poll_init();\\n#ifdef HAVE_PTHREAD\\n\\ttevent_poll_mt_init();\\n#endif/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/tevent.c"
    perl -0pi -e "s/\\n\\tif \\(ev->threaded_contexts != NULL\\) \\{\\n\\t\\ttevent_common_threaded_activate_immediate\\(ev\\);\\n\\t\\}/\\n#ifdef HAVE_PTHREAD\\n\\tif (ev->threaded_contexts != NULL) {\\n\\t\\ttevent_common_threaded_activate_immediate(ev);\\n\\t}\\n#endif/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/tevent_poll.c"
    perl -0pi -e "s/\\n\\tif \\(ev->threaded_contexts != NULL\\) \\{\\n\\t\\ttevent_common_threaded_activate_immediate\\(ev\\);\\n\\t\\}/\\n#ifdef HAVE_PTHREAD\\n\\tif (ev->threaded_contexts != NULL) {\\n\\t\\ttevent_common_threaded_activate_immediate(ev);\\n\\t}\\n#endif/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/tevent_epoll.c"
    perl -0pi -e "s/\\n\\tif \\(ev->threaded_contexts != NULL\\) \\{\\n\\t\\ttevent_common_threaded_activate_immediate\\(ev\\);\\n\\t\\}/\\n#ifdef HAVE_PTHREAD\\n\\tif (ev->threaded_contexts != NULL) {\\n\\t\\ttevent_common_threaded_activate_immediate(ev);\\n\\t}\\n#endif/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/tevent_port.c"

    # The AirPort's NetBSD userland aborts in malloc when the Samba build pulls
    # in libpthread-backed code paths. Keep the old NO_PTHREADS behavior by
    # stripping pthread dependencies from Samba's wscript graph before
    # configure; the generated cache is forced off later in _samba4.sh.
    if [ "$NO_PTHREADS" = "1" ]; then
        perl -0pi -e "s/tevent execinfo pthread strv/tevent execinfo strv/" \
            "$SAMBA4_SRC_DIR/lib/util/wscript_build"
        perl -0pi -e "s/public_deps='talloc tevent execinfo pthread/public_deps='talloc tevent execinfo/" \
            "$SAMBA4_SRC_DIR/lib/util/wscript_build"
        perl -0pi -e "s/deps='replace socket-blocking sys_rw pthread'/deps='replace socket-blocking sys_rw'/" \
            "$SAMBA4_SRC_DIR/lib/pthreadpool/wscript_build"
        perl -0pi -e "s/public_deps='replace pthread'/public_deps='replace'/" \
            "$SAMBA4_SRC_DIR/lib/pthreadpool/wscript_build"
    fi

    # The NetBSD 7 static libexecinfo archive depends on libelf, but this old
    # Samba 4.8 waf setup does not model that transitive dependency. Rather
    # than patch generated link lines repeatedly, turn off the optional
    # backtrace/execinfo feature at the source-tree level for reproducible
    # static cross-builds.
    perl -0pi -e "s/tevent execinfo pthread strv/tevent pthread strv/" \
        "$SAMBA4_SRC_DIR/lib/util/wscript_build"
    perl -0pi -e "s/public_deps='talloc tevent execinfo pthread/public_deps='talloc tevent pthread/" \
        "$SAMBA4_SRC_DIR/lib/util/wscript_build"
    perl -0pi -e "s/ deps='roken wind asn1 hx509 hcrypto com_err HEIMDAL_CONFIG heimbase execinfo samba_intl',/ deps='roken wind asn1 hx509 hcrypto com_err HEIMDAL_CONFIG heimbase samba_intl',/" \
        "$SAMBA4_SRC_DIR/source4/heimdal_build/wscript_build"

    # NetBSD/HFS + fruit/streams_xattr/xattr_tdb hits a Samba bug where a
    # missing TDB record is treated as corruption instead of "no xattrs yet".
    # That bubbles up as EINVAL from listxattr/getxattr and breaks SMB
    # rename/delete paths. Patch xattr_tdb to treat NOT_FOUND as an empty set.
    perl -0pi -e 's/\tstatus = dbwrap_fetch\(db, frame, key, &data\);\n\tif \(!NT_STATUS_IS_OK\(status\)\) \{\n\t\treturn NT_STATUS_INTERNAL_DB_CORRUPTION;\n\t\}/\tstatus = dbwrap_fetch(db, frame, key, \&data);\n\tif (NT_STATUS_EQUAL(status, NT_STATUS_NOT_FOUND)) {\n\t\t*presult = talloc_zero(mem_ctx, struct tdb_xattrs);\n\t\tif (*presult == NULL) {\n\t\t\treturn NT_STATUS_NO_MEMORY;\n\t\t}\n\t\treturn NT_STATUS_OK;\n\t}\n\tif (!NT_STATUS_IS_OK(status)) {\n\t\treturn NT_STATUS_INTERNAL_DB_CORRUPTION;\n\t}/' \
        "$SAMBA4_SRC_DIR/source3/lib/xattr_tdb.c"

    git -C "$SAMBA4_SRC_DIR" rev-parse --short HEAD
    git -C "$SAMBA4_SRC_DIR" log -1 --format='%H%n%cd%n%s' --date=iso
    echo "Finished Samba 4 download workflow at $(date -u)"
} >"$SAMBA4_DOWNLOAD_LOG" 2>&1

printf 'Samba 4 download complete.\n'
printf 'Log: %s\n' "$SAMBA4_DOWNLOAD_LOG"
