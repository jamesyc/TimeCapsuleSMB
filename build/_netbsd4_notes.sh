# Sourced by the Samba, service and rsync builds after env.sh; needs TOOLDIR
# and TRIPLE.
#
# The NetBSD kernel runs an ELF binary only when it carries the
# .note.netbsd.ident note (with .note.netbsd.pax beside it). libc's startup
# objects supply both, but nothing references them, and NetBSD 4's ld 2.16
# drops unreferenced note sections under --gc-sections, so a garbage-collected
# NetBSD 4 binary would not run. These links add an object with both notes and
# a copy of ld's default script that KEEPs them. NetBSD 6's ld 2.23 keeps note
# sections itself and needs none of this.

# netbsd4_keep_notes_inputs <work directory>
# Writes the notes object and the linker script into the directory and sets
# NETBSD4_KEEP_NOTES_LDFLAGS to the flags of a garbage-collected link.
netbsd4_keep_notes_inputs() {
    nkn_dir=$1
    mkdir -p "$nkn_dir" || return 1
    # The ident note's descriptor is the OS version, 400000003 (4.0).
    cat >"$nkn_dir/netbsd4-notes.S" <<'EOF' || return 1
    .section .note.netbsd.ident,"a",%note
    .balign 4
    .long 7
    .long 4
    .long 1
    .asciz "NetBSD"
    .balign 4
    .long 0x17d78403

    .section .note.netbsd.pax,"a",%note
    .balign 4
    .long 4
    .long 4
    .long 3
    .asciz "PaX"
    .balign 4
    .long 0
EOF
    "$TOOLDIR/bin/$TRIPLE-gcc" -c "$nkn_dir/netbsd4-notes.S" -o "$nkn_dir/netbsd4-notes.o" || return 1
    # ld --verbose prints its default script between two "====" lines.
    "$TOOLDIR/bin/$TRIPLE-ld" --verbose >"$nkn_dir/ld-verbose.txt" || {
        echo "$TOOLDIR/bin/$TRIPLE-ld --verbose failed"
        return 1
    }
    awk '
        /^====/ { seen++; next }
        seen == 1 { print }
    ' "$nkn_dir/ld-verbose.txt" >"$nkn_dir/netbsd4-default.ld" || return 1
    awk '
        /SIZEOF_HEADERS;/ {
            print
            print "  .note.netbsd.ident : { KEEP(*(.note.netbsd.ident)) }"
            print "  .note.netbsd.pax : { KEEP(*(.note.netbsd.pax)) }"
            kept = 1
            next
        }
        { print }
        END { if (!kept) exit 1 }
    ' "$nkn_dir/netbsd4-default.ld" >"$nkn_dir/netbsd4-keep-notes.ld" || {
        echo "ld's default script has no SIZEOF_HEADERS line to keep the notes after"
        return 1
    }
    NETBSD4_KEEP_NOTES_OBJ="$nkn_dir/netbsd4-notes.o"
    NETBSD4_KEEP_NOTES_LD="$nkn_dir/netbsd4-keep-notes.ld"
    NETBSD4_KEEP_NOTES_LDFLAGS="-Wl,--gc-sections -Wl,-T,$NETBSD4_KEEP_NOTES_LD $NETBSD4_KEEP_NOTES_OBJ"
}

# netbsd4_require_notes <binary>
netbsd4_require_notes() {
    nrn_sections="$("$TOOLDIR/bin/$TRIPLE-objdump" -h "$1" 2>/dev/null | awk '{ print $2 }')"
    for nrn_section in .note.netbsd.ident .note.netbsd.pax; do
        if ! printf '%s\n' "$nrn_sections" | grep -Fqx "$nrn_section"; then
            echo "$1: missing $nrn_section; the NetBSD 4 kernel will not run it"
            return 1
        fi
    done
    echo "$1: NetBSD note sections are present"
}
