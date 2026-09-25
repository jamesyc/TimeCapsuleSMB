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
