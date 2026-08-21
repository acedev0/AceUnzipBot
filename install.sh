#!/usr/bin/env bash
set -Eeuo pipefail

repo_url="${ACE_REPO_URL:-https://github.com/Aceddev/AceUnzipBot.git}"
repo_branch="${ACE_BRANCH:-main}"
install_dir="${ACE_INSTALL_DIR:-/opt/aceunzipbot}"

as_root=()
if [[ $(id -u) -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required for the VPS installer." >&2
    exit 1
  fi
  as_root=(sudo)
fi

install_bootstrap_tools() {
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "This one-line installer supports Ubuntu and Debian VPS hosts." >&2
    exit 1
  fi
  "${as_root[@]}" apt-get update
  "${as_root[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [[ ! -f "$script_dir/requirements.txt" || ! -d "$script_dir/acebot" ]]; then
  install_bootstrap_tools
  if [[ -e "$install_dir" && ! -d "$install_dir/.git" ]]; then
    echo "Install target exists but is not a Git checkout: $install_dir" >&2
    echo "Set ACE_INSTALL_DIR to an empty path, or move the existing directory yourself." >&2
    exit 2
  fi
  if [[ -d "$install_dir/.git" ]]; then
    if ! "${as_root[@]}" git -C "$install_dir" diff --quiet \
        || ! "${as_root[@]}" git -C "$install_dir" diff --cached --quiet; then
      echo "Update stopped because $install_dir contains local changes." >&2
      exit 2
    fi
    "${as_root[@]}" git -C "$install_dir" fetch --prune origin "$repo_branch"
    "${as_root[@]}" git -C "$install_dir" checkout "$repo_branch"
    "${as_root[@]}" git -C "$install_dir" pull --ff-only origin "$repo_branch"
  else
    "${as_root[@]}" install -d -m 0755 "$(dirname "$install_dir")"
    "${as_root[@]}" git clone --depth 1 --branch "$repo_branch" "$repo_url" "$install_dir"
  fi
  exec "$install_dir/install.sh" --bootstrap
fi

project_dir="$script_dir"
cd "$project_dir"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer targets Ubuntu/Debian. Use Docker on other distributions." >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  echo "systemd is not running on this host. Use the Docker deployment instead." >&2
  exit 1
fi

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  distro="${ID:-} ${ID_LIKE:-}"
  if [[ "$distro" != *ubuntu* && "$distro" != *debian* ]]; then
    echo "Unsupported distribution: ${PRETTY_NAME:-unknown}. Use Docker instead." >&2
    exit 1
  fi
fi

"${as_root[@]}" apt-get update
"${as_root[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl git python3 python3-dev python3-pip python3-venv \
  ffmpeg zstd build-essential cargo pkg-config software-properties-common

if [[ "${ID:-}" == "ubuntu" ]] && command -v add-apt-repository >/dev/null 2>&1; then
  "${as_root[@]}" add-apt-repository -y universe
  "${as_root[@]}" add-apt-repository -y multiverse
  "${as_root[@]}" apt-get update
fi

if "${as_root[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y 7zip; then
  if ! "${as_root[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y 7zip-rar; then
    echo "Note: no separate 7zip-rar plugin was available; the installed 7-Zip build will be used."
  fi
else
  "${as_root[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y p7zip-full
  if ! "${as_root[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y p7zip-rar; then
    echo "Warning: this distribution has no separate RAR plugin package; test RAR archives locally."
  fi
fi

"${as_root[@]}" python3 -m venv "$project_dir/.venv"
"${as_root[@]}" "$project_dir/.venv/bin/python" -m pip install --upgrade pip wheel
"${as_root[@]}" "$project_dir/.venv/bin/python" -m pip install -r "$project_dir/requirements.txt"

service_name="ace-unzip-bot.service"
if [[ "$project_dir" == /opt/* ]]; then
  service_user="aceunzip"
  env_file="/etc/aceunzip/aceunzip.env"
  data_dir="/var/lib/aceunzip/data"
  work_dir="/var/lib/aceunzip/work"
  if ! id "$service_user" >/dev/null 2>&1; then
    "${as_root[@]}" useradd --system --create-home --home-dir /var/lib/aceunzip \
      --shell /usr/sbin/nologin "$service_user"
  fi
else
  service_user="${SUDO_USER:-$(id -un)}"
  env_file="$project_dir/.env"
  data_dir="$project_dir/data"
  work_dir="$project_dir/work"
fi
service_group="$(id -gn "$service_user")"

if [[ "$(dirname "$env_file")" != "$project_dir" ]]; then
  "${as_root[@]}" install -d -o root -g "$service_group" -m 0750 "$(dirname "$env_file")"
fi
"${as_root[@]}" install -d -o "$service_user" -g "$service_group" -m 0700 \
  "$data_dir" "$work_dir"

if [[ ! -f "$env_file" ]]; then
  "${as_root[@]}" "$project_dir/scripts/configure.sh" "$env_file" "$data_dir" "$work_dir"
fi
"${as_root[@]}" chown "root:$service_group" "$env_file"
"${as_root[@]}" chmod 0640 "$env_file"

"${as_root[@]}" env ACE_ENV_FILE="$env_file" "$project_dir/.venv/bin/python" -m acebot --check
if [[ "${ACE_SKIP_TELEGRAM_VERIFY:-0}" != "1" ]]; then
  "${as_root[@]}" env ACE_ENV_FILE="$env_file" "$project_dir/.venv/bin/python" \
    -m acebot --verify-telegram
fi

service_file="/etc/systemd/system/$service_name"
temp_service="$(mktemp /tmp/ace-unzip-service.XXXXXX)"
trap 'rm -f "$temp_service"' EXIT
sed \
  -e "s|@@WORKDIR@@|$project_dir|g" \
  -e "s|@@USER@@|$service_user|g" \
  -e "s|@@ENVFILE@@|$env_file|g" \
  -e "s|@@DATA_DIR@@|$data_dir|g" \
  -e "s|@@WORK_DIR@@|$work_dir|g" \
  "$project_dir/systemd/ace-unzip-bot.service.template" > "$temp_service"
"${as_root[@]}" install -m 0644 "$temp_service" "$service_file"
rm -f "$temp_service"
trap - EXIT

install_record="$(mktemp /tmp/ace-unzip-record.XXXXXX)"
trap 'rm -f "$install_record"' EXIT
{
  printf 'PROJECT_DIR=%s\n' "$project_dir"
  printf 'ENV_FILE=%s\n' "$env_file"
  printf 'SERVICE_NAME=%s\n' "$service_name"
  printf 'SERVICE_USER=%s\n' "$service_user"
  printf 'SERVICE_GROUP=%s\n' "$service_group"
} > "$install_record"
"${as_root[@]}" install -d -o root -g root -m 0755 /etc/aceunzip
"${as_root[@]}" install -m 0644 "$install_record" /etc/aceunzip/install.conf
rm -f "$install_record"
trap - EXIT
"${as_root[@]}" ln -sfn "$project_dir/scripts/aceunzip" /usr/local/bin/aceunzip

"${as_root[@]}" systemctl daemon-reload
"${as_root[@]}" systemctl enable --now "$service_name"

echo
echo "AceUnzipBot is installed and running."
echo "Reconfigure: aceunzip"
echo "Status:      aceunzip status"
echo "Logs:        aceunzip logs"
