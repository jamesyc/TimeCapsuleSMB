#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH

exec /mnt/Flash/service run
