#!/bin/sh
set -eu

path=${1:-/Volumes/dk2}
line=$(df -P -k "$path" | sed -n '2p')

set -- $line

# Samba expects: total_blocks free_blocks block_size
echo "$2 $4 1024"
