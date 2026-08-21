#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$project_dir/scripts/configure.sh" "$project_dir/.env" "./data" "./work"

echo
echo "Configuration is ready. Run: ./install.sh"
