#!/bin/sh
set -eu

data_dir="${DATA_DIR:-/app/data}"
work_dir="${WORK_DIR:-/app/work}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$data_dir" "$work_dir"
  chown -R acebot:acebot "$data_dir" "$work_dir"
  exec gosu acebot "$@"
fi

mkdir -p "$data_dir" "$work_dir"
exec "$@"
