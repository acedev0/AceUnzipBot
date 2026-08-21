#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip wheel
  .venv/bin/python -m pip install -r requirements.txt
fi

export DATA_DIR="${DATA_DIR:-./data}"
export WORK_DIR="${WORK_DIR:-./work}"
exec .venv/bin/python -m acebot
