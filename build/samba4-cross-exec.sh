#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <binary> [args...]" >&2
    exit 2
fi

quote_arg() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
        return
    fi
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
        return
    fi
    openssl dgst -sha256 "$1" | awk '{print $NF}'
}

ensure_replay_parent() {
    replay_parent=$(dirname "$1")
    mkdir -p "$replay_parent"
}

record_probe() {
    replay_file=$1
    kind=$2
    key=$3
    status=$4
    stdout_file=$5
    stderr_file=$6
    shift 6

    ensure_replay_parent "$replay_file"
    perl -MJSON::PP -MMIME::Base64 -e '
        use strict;
        use warnings;

        my ($replay_file, $kind, $key, $status, $stdout_file, $stderr_file, @argv) = @ARGV;

        open my $stdout_fh, "<", $stdout_file or die "open stdout: $!";
        local $/;
        my $stdout = <$stdout_fh>;
        close $stdout_fh;

        open my $stderr_fh, "<", $stderr_file or die "open stderr: $!";
        local $/;
        my $stderr = <$stderr_fh>;
        close $stderr_fh;

        my $record = {
            kind => $kind,
            key => $key,
            argv => \@argv,
            status => int($status),
            stdout_b64 => encode_base64($stdout, q{}),
            stderr_b64 => encode_base64($stderr, q{}),
        };

        open my $out_fh, ">>", $replay_file or die "open replay: $!";
        print {$out_fh} encode_json($record), "\n";
        close $out_fh;
    ' "$replay_file" "$kind" "$key" "$status" "$stdout_file" "$stderr_file" "$@"
}

replay_probe() {
    replay_file=$1
    kind=$2
    key=$3
    stdout_file=$4
    stderr_file=$5
    shift 5

    perl -MJSON::PP -MMIME::Base64 -e '
        use strict;
        use warnings;

        my ($replay_file, $kind, $key, $stdout_file, $stderr_file, @wanted_argv) = @ARGV;

        open my $in_fh, "<", $replay_file or die "open replay: $!";
        while (my $line = <$in_fh>) {
            chomp $line;
            next if $line eq q{};

            my $record = decode_json($line);
            next if $record->{kind} ne $kind;
            next if $record->{key} ne $key;

            my @record_argv = @{ $record->{argv} || [] };
            next if @record_argv != @wanted_argv;

            my $match = 1;
            for my $index (0 .. $#wanted_argv) {
                if ($record_argv[$index] ne $wanted_argv[$index]) {
                    $match = 0;
                    last;
                }
            }
            next unless $match;

            open my $stdout_fh, ">", $stdout_file or die "write stdout: $!";
            print {$stdout_fh} decode_base64($record->{stdout_b64} // q{});
            close $stdout_fh;

            open my $stderr_fh, ">", $stderr_file or die "write stderr: $!";
            print {$stderr_fh} decode_base64($record->{stderr_b64} // q{});
            close $stderr_fh;

            print $record->{status};
            exit 0;
        }

        die "No replay match for kind=$kind key=$key argv=[@wanted_argv]\n";
    ' "$replay_file" "$kind" "$key" "$stdout_file" "$stderr_file" "$@"
}

emit_captured_output() {
    stdout_file=$1
    stderr_file=$2
    cat "$stdout_file"
    cat "$stderr_file" >&2
}

run_remote_and_capture() {
    stdout_file=$1
    stderr_file=$2
    shift 2

    status=0
    "$@" >"$stdout_file" 2>"$stderr_file" || status=$?
    printf '%s\n' "$status"
}

LOCAL_CMD=$1
shift

MODE=${SAMBA4_CROSS_EXEC_MODE:-live}

case "$MODE" in
    live|record|replay)
        ;;
    *)
        echo "Unsupported SAMBA4_CROSS_EXEC_MODE: $MODE" >&2
        exit 2
        ;;
esac

if [ "$MODE" = "record" ] && [ -z "${SAMBA4_COMPAT_REPLAY_OUT:-}" ]; then
    echo "SAMBA4_COMPAT_REPLAY_OUT is required in record mode" >&2
    exit 2
fi

if [ "$MODE" = "replay" ] && [ -z "${SAMBA4_COMPAT_REPLAY_IN:-}" ]; then
    echo "SAMBA4_COMPAT_REPLAY_IN is required in replay mode" >&2
    exit 2
fi

stdout_file=$(mktemp "${TMPDIR:-/tmp}/samba4-cross-stdout.XXXXXX")
stderr_file=$(mktemp "${TMPDIR:-/tmp}/samba4-cross-stderr.XXXXXX")
cleanup() {
    rm -f "$stdout_file" "$stderr_file"
}
trap cleanup EXIT HUP INT TERM

if [ -f "$LOCAL_CMD" ]; then
    REMOTE_DIR="/tmp/tc-samba4-probes"
    REMOTE_BIN="$REMOTE_DIR/$(basename "$LOCAL_CMD").$$"
    REMOTE_CMD=$(quote_arg "$REMOTE_BIN")
    PROBE_SHA256=$(sha256_file "$LOCAL_CMD")

    for arg in "$@"; do
        REMOTE_CMD="$REMOTE_CMD $(quote_arg "$arg")"
    done

    if [ "$MODE" = "replay" ]; then
        status=$(replay_probe "$SAMBA4_COMPAT_REPLAY_IN" file "$PROBE_SHA256" "$stdout_file" "$stderr_file" "$@")
        emit_captured_output "$stdout_file" "$stderr_file"
        exit "$status"
    fi

    tc_ssh "$TC_HOST" "mkdir -p \"$REMOTE_DIR\""
    cat "$LOCAL_CMD" | tc_ssh "$TC_HOST" "cat > \"$REMOTE_BIN\""

    status=$(run_remote_and_capture "$stdout_file" "$stderr_file" \
        tc_ssh "$TC_HOST" "chmod +x \"$REMOTE_BIN\" && exec $REMOTE_CMD")
    tc_ssh "$TC_HOST" "rm -f \"$REMOTE_BIN\"" >/dev/null 2>&1 || true

    emit_captured_output "$stdout_file" "$stderr_file"

    if [ "$MODE" = "record" ]; then
        record_probe "$SAMBA4_COMPAT_REPLAY_OUT" file "$PROBE_SHA256" "$status" "$stdout_file" "$stderr_file" "$@"
    fi

    exit "$status"
fi

REMOTE_CMD=$(quote_arg "$LOCAL_CMD")
for arg in "$@"; do
    REMOTE_CMD="$REMOTE_CMD $(quote_arg "$arg")"
done

if [ "$MODE" = "replay" ]; then
    status=$(replay_probe "$SAMBA4_COMPAT_REPLAY_IN" command "$LOCAL_CMD" "$stdout_file" "$stderr_file" "$@")
    emit_captured_output "$stdout_file" "$stderr_file"
    exit "$status"
fi

status=$(run_remote_and_capture "$stdout_file" "$stderr_file" \
    tc_ssh "$TC_HOST" "exec $REMOTE_CMD")
emit_captured_output "$stdout_file" "$stderr_file"

if [ "$MODE" = "record" ]; then
    record_probe "$SAMBA4_COMPAT_REPLAY_OUT" command "$LOCAL_CMD" "$status" "$stdout_file" "$stderr_file" "$@"
fi

exit "$status"
