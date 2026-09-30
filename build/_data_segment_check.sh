# Sourced by the Samba, service and rsync builds after env.sh; needs TOOLDIR
# and TRIPLE.
#
# Apple's NetBSD 4 and NetBSD 6 kernels lose writes to a static binary's
# initialized data: the first write to a .data page makes UVM fault-ahead map
# the neighbouring pages from the executable again (see data_page_writes in
# tests/samba/tc_aio_fork_test.c). Every shipped static binary therefore runs
# a constructor that calls madvise(MADV_RANDOM) from __preinit_array_start to
# "end". Nothing fails visibly when that call is missing or the linker moves
# .data outside that range, so refuse to stage such a binary.
#
# verify_data_faultahead <unstripped binary> <constructor function name>
verify_data_faultahead() {
    dfa_binary=$1
    dfa_function=$2
    dfa_readelf="$TOOLDIR/bin/$TRIPLE-readelf"

    # One writable PT_LOAD: "LOAD off vaddr paddr filesz memsz RW align".
    dfa_rw="$("$dfa_readelf" -lW "$dfa_binary" | awk '$1 == "LOAD" && $7 == "RW" { print $3, $6 }')"
    set -- $dfa_rw
    if [ "$#" != "2" ]; then
        echo "$dfa_binary: expected one writable PT_LOAD segment, found: $dfa_rw"
        return 1
    fi
    dfa_rw_start=$(($1))
    dfa_rw_end=$(($1 + $2))

    # "[Nr] Name Type Addr ...": the bracket may hold a space, so find the name.
    dfa_data="$("$dfa_readelf" -SW "$dfa_binary" | awk '{ for (i = 1; i < NF; i++) if ($i == ".data") { print $(i + 2); exit } }')"
    dfa_symbols="$("$dfa_readelf" -sW "$dfa_binary")"
    dfa_first="$(printf '%s\n' "$dfa_symbols" | awk '$8 == "__preinit_array_start" { print $2; exit }')"
    dfa_end="$(printf '%s\n' "$dfa_symbols" | awk '$8 == "end" { print $2; exit }')"
    dfa_func="$(printf '%s\n' "$dfa_symbols" | awk -v f="$dfa_function" '$4 == "FUNC" && $8 == f { print $2, $3; exit }')"
    for dfa_pair in ".data:$dfa_data" "__preinit_array_start:$dfa_first" "end:$dfa_end" "$dfa_function:$dfa_func"; do
        if [ -z "${dfa_pair#*:}" ]; then
            echo "$dfa_binary: missing ${dfa_pair%%:*}"
            return 1
        fi
    done

    if [ $((0x$dfa_first)) -lt "$dfa_rw_start" ] || [ $((0x$dfa_first)) -gt $((0x$dfa_data)) ]; then
        echo "$dfa_binary: __preinit_array_start 0x$dfa_first is not between the writable segment start and .data 0x$dfa_data"
        return 1
    fi
    # _end is wrong in the NetBSD 4 migrator link; "end" must close the segment.
    if [ $((0x$dfa_end)) -ne "$dfa_rw_end" ]; then
        echo "$dfa_binary: end 0x$dfa_end does not close the writable segment ($(printf '0x%x' "$dfa_rw_end"))"
        return 1
    fi

    set -- $dfa_func
    if ! "$TOOLDIR/bin/$TRIPLE-objdump" -d \
        --start-address="0x$1" --stop-address="$(printf '0x%x' $((0x$1 + $2)))" "$dfa_binary" |
        grep -Eq '<_*madvise>'; then
        echo "$dfa_binary: $dfa_function does not call madvise"
        return 1
    fi
    echo "$dfa_binary: $dfa_function turns off fault-ahead for 0x$dfa_first..0x$dfa_end"
}

# Apple's NetBSD 6 kernel shows a child the writes its parent makes after
# fork() to pages whose reference bit was cleared before it (its ARM
# pmap_protect() skips such pages when fork() write-protects the parent). The
# NetBSD 6 lanes link lib/replace/tc_fork_repair.c (Samba patch 0070's overlay
# file) into every static binary: it repairs the parent's mappings right
# before each fork(), and keeps a registry of private mappings through these
# linker wrappers. libc reaches fork through _fork (daemon(), wordexp()) and
# mmap through _mmap (jemalloc, arc4random), so both names are wrapped.
# NetBSD 4's kernel is not affected; its lanes link none of this.
# TC_FORK_REPAIR builds the repair itself, TC_FORK_REPAIR_WRAPPERS the
# wrappers, whose __real_ references resolve only in a link that uses --wrap.
# The service and rsync compile both into their one binary; Samba's
# libreplace has only the repair (Samba also links it into a shared library)
# and its static links get the wrappers as a separate object.
TC_FORK_REPAIR_CORE_CFLAGS="-DTC_FORK_REPAIR=1"
TC_FORK_REPAIR_WRAPPERS_CFLAGS="-DTC_FORK_REPAIR_WRAPPERS=1"
TC_FORK_REPAIR_CFLAGS="$TC_FORK_REPAIR_CORE_CFLAGS $TC_FORK_REPAIR_WRAPPERS_CFLAGS"
TC_FORK_REPAIR_LDFLAGS="-Wl,--wrap=fork -Wl,--wrap=_fork -Wl,--wrap=mmap -Wl,--wrap=_mmap -Wl,--wrap=munmap -Wl,--wrap=mremap -Wl,--wrap=mprotect"

# The wrapper flags and the given link inputs, as a Python list body for
# Samba's waf cache.
tc_fork_repair_waf_list() {
    tfr_list=
    for tfr_flag in $TC_FORK_REPAIR_LDFLAGS "$@"; do
        tfr_list="$tfr_list, '$tfr_flag'"
    done
    printf '%s\n' "$tfr_list"
}

# verify_fork_repair <unstripped binary>
# Every libc function the repair wraps that the binary links must be reached
# through a wrapper. Without the --wrap flags the wrappers are unreferenced,
# --gc-sections drops them and libc's function stays, silently.
verify_fork_repair() {
    vfr_binary=$1
    vfr_symbols="$("$TOOLDIR/bin/$TRIPLE-nm" "$vfr_binary")"
    vfr_has() { printf '%s\n' "$vfr_symbols" | grep -Eq " [TtWw] $1\$"; }
    # "libc symbols:wrappers, one of which must be present".
    for vfr_group in "_fork:__wrap_fork,__wrap__fork" "_mmap:__wrap_mmap,__wrap__mmap" \
        "munmap:__wrap_munmap" "mremap:__wrap_mremap" "mprotect:__wrap_mprotect"; do
        vfr_libc=${vfr_group%%:*}
        vfr_wrappers=${vfr_group#*:}
        vfr_has "$vfr_libc" || continue
        vfr_found=
        for vfr_wrapper in $(printf '%s\n' "$vfr_wrappers" | sed 's/,/ /g'); do
            vfr_has "$vfr_wrapper" && vfr_found=1
        done
        if [ -z "$vfr_found" ]; then
            echo "$vfr_binary: links libc's $vfr_libc without the fork repair's wrapper ($vfr_wrappers)"
            return 1
        fi
    done
    if vfr_has _fork && ! vfr_has tc_fork_repair_cycle; then
        echo "$vfr_binary: forks without tc_fork_repair_cycle"
        return 1
    fi
    echo "$vfr_binary: fork() and the mmap family go through the fork repair"
}
