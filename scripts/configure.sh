#!/usr/bin/env bash
set -Eeuo pipefail

env_file="${1:-}"
data_default="${2:-./data}"
work_default="${3:-./work}"

if [[ -z "$env_file" ]]; then
  echo "Usage: configure.sh ENV_FILE [DATA_DIR] [WORK_DIR]" >&2
  exit 2
fi
if [[ ! -t 0 ]]; then
  echo "The secure credential wizard needs an interactive terminal." >&2
  echo "Use: bash <(curl -fLsS URL)" >&2
  exit 2
fi

bold=""
cyan=""
purple=""
green=""
reset=""
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  bold=$'\033[1m'
  cyan=$'\033[38;5;45m'
  purple=$'\033[38;5;141m'
  green=$'\033[38;5;48m'
  reset=$'\033[0m'
fi

banner() {
  printf '%s\n' "${cyan}${bold}    _    ____ _____"
  printf '%s\n' "   / \\  / ___| ____|"
  printf '%s\n' "  / _ \\| |   |  _|"
  printf '%s\n' " / ___ \\ |___| |___"
  printf '%s\n' "/_/   \\_\\____|_____|${reset}"
  printf '%s\n' "${purple}${bold}ACE UNZIP BOT • SECURE SETUP${reset}"
  printf '%s\n\n' "Made by Ace • @Aceddev • Acedevmc@gmail.com"
}

existing_value() {
  local key="$1"
  [[ -f "$env_file" ]] || return 0
  sed -n "s/^${key}=//p" "$env_file" | tail -n 1
}

prompt_value() {
  local label="$1"
  local current="$2"
  local value=""
  if [[ -n "$current" ]]; then
    read -r -p "$label [$current]: " value
    printf '%s' "${value:-$current}"
  else
    read -r -p "$label: " value
    printf '%s' "$value"
  fi
}

prompt_secret() {
  local label="$1"
  local current="$2"
  local value=""
  if [[ -n "$current" ]]; then
    read -r -s -p "$label [press Enter to keep current]: " value
    printf '\n' >&2
    printf '%s' "${value:-$current}"
  else
    read -r -s -p "$label: " value
    printf '\n' >&2
    printf '%s' "$value"
  fi
}

banner
printf '%s\n' "Secrets are masked and stored only in a mode-600 environment file."
printf '%s\n\n' "Press Enter to keep an existing value."

current_api_id="$(existing_value API_ID)"
current_api_hash="$(existing_value API_HASH)"
current_bot_token="$(existing_value BOT_TOKEN)"
current_owner_id="$(existing_value OWNER_ID)"
current_log_channel="$(existing_value LOG_CHANNEL_ID)"
current_data_dir="$(existing_value DATA_DIR)"
current_work_dir="$(existing_value WORK_DIR)"
current_log_level="$(existing_value LOG_LEVEL)"

while true; do
  api_id="$(prompt_value 'Enter API ID' "$current_api_id")"
  [[ "$api_id" =~ ^[0-9]+$ && "$api_id" -gt 0 ]] && break
  echo "API ID must be a positive number." >&2
done

while true; do
  api_hash="$(prompt_secret 'Enter API hash' "$current_api_hash")"
  [[ "$api_hash" =~ ^[0-9A-Fa-f]{32}$ ]] && break
  echo "API hash must contain exactly 32 hexadecimal characters." >&2
done

while true; do
  bot_token="$(prompt_secret 'Enter bot token' "$current_bot_token")"
  [[ "$bot_token" =~ ^[0-9]{6,15}:[A-Za-z0-9_-]{20,}$ ]] && break
  echo "Bot token does not match the format issued by @BotFather." >&2
done

while true; do
  owner_id="$(prompt_value 'Enter owner Telegram ID' "$current_owner_id")"
  [[ "$owner_id" =~ ^[0-9]+$ && "$owner_id" -gt 0 ]] && break
  echo "Owner ID must be a positive numeric Telegram user ID." >&2
done

while true; do
  if [[ -n "$current_log_channel" ]]; then
    read -r -p "Enter log channel ID [${current_log_channel}; blank keeps it, 'off' disables]: " log_channel
    log_channel="${log_channel:-$current_log_channel}"
  else
    read -r -p "Enter log channel ID [optional; blank disables]: " log_channel
  fi
  case "${log_channel,,}" in
    ""|off|none|disable|disabled|0)
      log_channel=""
      break
      ;;
  esac
  [[ "$log_channel" =~ ^-[0-9]+$ ]] && break
  echo "Use a negative Telegram channel/group ID, or leave it blank." >&2
done

data_dir="${current_data_dir:-$data_default}"
work_dir="${current_work_dir:-$work_default}"
log_level="${current_log_level:-INFO}"
if [[ "$data_dir" == "$work_dir" ]]; then
  echo "DATA_DIR and WORK_DIR must be different." >&2
  exit 2
fi

env_dir="$(dirname "$env_file")"
if [[ ! -d "$env_dir" ]]; then
  install -d -m 0700 "$env_dir"
fi
temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
trap 'rm -f "$temp_file"' EXIT
umask 077
{
  printf 'API_ID=%s\n' "$api_id"
  printf 'API_HASH=%s\n' "$api_hash"
  printf 'BOT_TOKEN=%s\n' "$bot_token"
  printf 'OWNER_ID=%s\n' "$owner_id"
  printf 'LOG_CHANNEL_ID=%s\n' "$log_channel"
  printf 'DATA_DIR=%s\n' "$data_dir"
  printf 'WORK_DIR=%s\n' "$work_dir"
  printf 'LOG_LEVEL=%s\n' "$log_level"
} > "$temp_file"
chmod 0600 "$temp_file"
mv -f "$temp_file" "$env_file"
trap - EXIT

printf '\n%s\n' "${green}${bold}Configuration saved securely.${reset}"
printf '%s\n' "No API hash or bot token was printed."
