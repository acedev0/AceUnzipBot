#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

env_file="${ACE_ENV_FILE:-$project_dir/.env}"
if [[ ! -f "$env_file" ]]; then
  echo "Missing configuration: $env_file. Run: ./setup.sh"
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing virtual environment. Run: ./install.sh"
  exit 1
fi

exec env ACE_ENV_FILE="$env_file" .venv/bin/python -m acebot
