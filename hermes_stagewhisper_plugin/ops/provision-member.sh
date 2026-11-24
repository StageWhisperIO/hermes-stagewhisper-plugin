#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

TEMPLATE_PATH="${SCRIPT_DIR}/hermes-gateway.service.template"

usage() {
  cat <<EOF
Usage: $(basename "$0") <label> [options]

  <label>            Member identifier: ^[a-z0-9][a-z0-9-]{0,30}\$

Options:
  --relay-host HOST   Tailnet name this gateway is served on, for example
                       hermes.your-tailnet.ts.net. Resolves to https://HOST:PORT.
                       Detected from this host's tailnet identity when omitted.
  --url URL           Full relay URL override, takes precedence over --relay-host.
                       Pass a loopback URL such as http://127.0.0.1:PORT for a
                       same-host trial; the member then cannot connect from
                       another machine.
  --from-profile PATH Hermes profile to inherit the model provider and its
                       credentials from (default: \$HOME/.hermes, the profile the
                       gateway itself runs from). Personal settings such as
                       memories, sessions and chat integrations are never copied.
  --venv-python PATH  Path to the shared Hermes venv's python3
                       (default: \$HERMES_VENV_PYTHON or ${HERMES_VENV_PYTHON}).
EOF
}

label=""
relay_host=""
relay_url_override=""
source_profile="${STAGEWHISPER_SOURCE_PROFILE:-${HOME:-/root}/.hermes}"
LOOPBACK_HOSTS="127.0.0.1 localhost ::1"

while [ $# -gt 0 ]; do
  case "$1" in
    --relay-host)
      relay_host="$2"
      shift 2
      ;;
    --url)
      relay_url_override="$2"
      shift 2
      ;;
    --venv-python)
      HERMES_VENV_PYTHON="$2"
      shift 2
      ;;
    --from-profile)
      source_profile="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [ -n "$label" ]; then
        fail "Unexpected argument '$1'."
      fi
      label="$1"
      shift
      ;;
  esac
done

[ -n "$label" ] || { usage; exit 1; }

require_root
validate_label "$label"
require_python_plugin_stack "$HERMES_VENV_PYTHON"

home_dir="$(home_dir_for "$label")"
hermes_home="$(hermes_home_for "$label")"
unit_name="$(unit_name_for "$label")"
unit_path="${SYSTEMD_UNIT_DIR}/${unit_name}"

rollback() {
  echo "Provisioning '$label' failed; removing the OS user and unit this run created..." >&2
  systemctl disable --now "$unit_name" >/dev/null 2>&1 || true
  rm -f "$unit_path"
  systemctl daemon-reload >/dev/null 2>&1 || true
  userdel -r "$label" >/dev/null 2>&1 || true
  echo "Rollback complete. '$label' was not provisioned." >&2
}

ensure_os_user() {
  if id "$label" >/dev/null 2>&1; then
    if [ ! -f "$unit_path" ]; then
      fail "OS user '$label' already exists but has no $unit_name unit. Refusing to adopt a pre-existing account; choose a different label."
    fi
    return 0
  fi
  local shell
  shell="$(nologin_shell)"
  useradd \
    --system \
    --create-home \
    --home-dir "$home_dir" \
    --shell "$shell" \
    --user-group \
    --comment "StageWhisper member ($label)" \
    "$label" || fail "useradd failed for '$label'."
  ON_FAIL_HOOK=rollback
  passwd -l "$label" >/dev/null || fail "Could not lock the password for '$label'."
  chmod 0700 "$home_dir" || fail "Could not set $home_dir to mode 0700."
}

bootstrap_profile_dirs() {
  local subdir
  install -d -m 0700 -o "$label" -g "$label" "$hermes_home" || fail "Could not create $hermes_home."
  for subdir in memories sessions skills skins logs plans workspace cron home plugins; do
    install -d -m 0700 -o "$label" -g "$label" "${hermes_home}/${subdir}" || fail "Could not create ${hermes_home}/${subdir}."
  done
}

ensure_config_yaml() {
  local config_path="${hermes_home}/config.yaml"
  if [ -f "$config_path" ]; then
    if grep -Eq '^[[:space:]]*-[[:space:]]*stagewhisper[[:space:]]*$' "$config_path"; then
      return 0
    fi
    fail "$config_path already exists but does not list 'stagewhisper' under plugins.enabled. Add it by hand, then re-run."
  fi
  cat > "$config_path" <<'YAML' || fail "Could not write $config_path."
plugins:
  enabled:
    - stagewhisper
YAML
  chown "$label:$label" "$config_path" || fail "Could not chown $config_path to $label."
  chmod 0600 "$config_path" || fail "Could not chmod $config_path."
}

host_from_url() {
  local host="${1#*://}"
  host="${host%%/*}"
  host="${host%%\?*}"
  if [ "${host#\[}" != "$host" ]; then
    host="${host#\[}"
    host="${host%%\]*}"
  else
    host="${host%%:*}"
  fi
  printf '%s' "$host" | tr '[:upper:]' '[:lower:]'
}

host_is_loopback() {
  local candidate="$1" loopback
  for loopback in $LOOPBACK_HOSTS; do
    [ "$candidate" = "$loopback" ] && return 0
  done
  return 1
}

ensure_env_file() {
  local port="$1" ingress_host="$2" env_path="${hermes_home}/.env"
  local token existing_token=""

  if [ -f "$env_path" ]; then
    existing_token="$(sed -n 's/^STAGEWHISPER_RELAY_TOKEN="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$env_path" | head -1)"
    if [ -z "$existing_token" ]; then
      fail "$env_path exists but has no STAGEWHISPER_RELAY_TOKEN. Remove it by hand, then re-run."
    fi
  fi

  token="${existing_token:-$(generate_token)}"
  [ -n "$token" ] || fail "Token generation produced an empty token."

  local tmp
  tmp="$(mktemp "${env_path}.XXXXXX")" || fail "mktemp failed for $env_path."
  {
    printf 'STAGEWHISPER_RELAY_TOKEN="%s"\n' "$token"
    printf 'STAGEWHISPER_LISTEN_PORT="%s"\n' "$port"
    printf 'STAGEWHISPER_HOME_CHANNEL="%s"\n' "$(home_channel_for "$label")"
    if [ -n "$ingress_host" ]; then
      printf 'STAGEWHISPER_ALLOW_INGRESS_HOSTS="%s"\n' "$ingress_host"
    fi
  } > "$tmp" || fail "Could not write $tmp."
  chmod 0600 "$tmp" || fail "Could not chmod $tmp."
  chown "$label:$label" "$tmp" || fail "Could not chown $tmp to $label."
  mv "$tmp" "$env_path" || fail "Could not move $tmp into place at $env_path."
}

seed_from_gateway_profile() {
  local source_profile="$1"
  if [ ! -d "$source_profile" ]; then
    warn "Gateway profile '$source_profile' not found, so '$label' inherits no model provider or credentials and their assistant cannot answer yet. Pass --from-profile with the profile the gateway runs from."
    return 0
  fi
  "$HERMES_VENV_PYTHON" -m hermes_stagewhisper_plugin.profile_seed \
    --source "$source_profile" \
    --target "$hermes_home" \
    --owner-uid "$(id -u "$label")" \
    --owner-gid "$(id -g "$label")" \
    || fail "Could not copy the model provider and credentials from $source_profile into $hermes_home."
}

install_plugin_shim() {
  "$HERMES_VENV_PYTHON" -m hermes_stagewhisper_plugin.install \
    --plugins-dir "${hermes_home}/plugins" >/dev/null || fail "hermes_stagewhisper_plugin.install failed for ${hermes_home}/plugins."
  chown -R "$label:$label" "${hermes_home}/plugins" || fail "Could not chown ${hermes_home}/plugins to $label."
}

render_unit() {
  local venv_bin venv_dir service_path
  venv_bin="$(dirname "$HERMES_VENV_PYTHON")"
  venv_dir="$(dirname "$venv_bin")"
  service_path="${venv_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  sed \
    -e "s|__LABEL__|${label}|g" \
    -e "s|__PYTHON__|${HERMES_VENV_PYTHON}|g" \
    -e "s|__HERMES_HOME__|${hermes_home}|g" \
    -e "s|__HOME_DIR__|${home_dir}|g" \
    -e "s|__SERVICE_PATH__|${service_path}|g" \
    -e "s|__VENV_DIR__|${venv_dir}|g" \
    "$TEMPLATE_PATH"
}

install_unit() {
  local port="$1"
  if [ "$(systemctl is-active "$unit_name" 2>/dev/null || true)" != "active" ] && port_live_in_use "$port"; then
    fail "Port $port (derived from uid $(id -u "$label")) for '$label' is already in use on this host by a process this tool does not manage. Free it, or provision under a different label, then re-run."
  fi
  render_unit > "$unit_path" || fail "Could not render the systemd unit to $unit_path."
  chmod 0644 "$unit_path" || fail "Could not chmod $unit_path."
  systemctl daemon-reload || fail "systemctl daemon-reload failed."
  systemctl enable --now "$unit_name" || fail "systemctl enable --now $unit_name failed. Inspect: journalctl -u $unit_name -n 50 --no-pager"
}

verify_unit_listening() {
  local port="$1" attempt=0 state restarts
  while : ; do
    state="$(systemctl is-active "$unit_name" 2>/dev/null || true)"
    restarts="$(systemctl show -p NRestarts --value "$unit_name" 2>/dev/null || echo 0)"
    if [ "$state" = "active" ] && [ "${restarts:-0}" -eq 0 ] && port_live_in_use "$port"; then
      return 0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 20 ]; then
      fail "Gateway unit '$unit_name' is not serving on port $port after 20s (state: $state, restarts: ${restarts:-0}). A unit that keeps restarting can report 'active' momentarily, so this checks the port too. Inspect: journalctl -u $unit_name -n 50 --no-pager"
    fi
    sleep 1
  done
}

serve_on_tailnet() {
  local port="$1" output
  if tailscale_serve_active "$port"; then
    return 0
  fi
  if ! output="$(tailscale_serve_port "$port")"; then
    warn "Could not put port $port on your tailnet automatically: ${output:-unknown error}
  '$label' is running but cannot be reached from their own laptop or phone yet. Run:
    tailscale serve --bg --https ${port} http://127.0.0.1:${port}
  If that reports HTTPS is disabled, turn on HTTPS certificates for your tailnet at
  https://login.tailscale.com/admin/dns and run it again."
    return 1
  fi
  if ! tailscale_serve_active "$port"; then
    warn "'tailscale serve' reported success for port $port but the port is not listed in 'tailscale serve status'. Check it by hand before handing the pairing code out."
    return 1
  fi
}

print_pairing_code() {
  local port="$1" relay_url="$2" ingress_host="$3" venv_bin cli_path
  venv_bin="$(dirname "$HERMES_VENV_PYTHON")"
  cli_path="${venv_bin}/stagewhisper-hermes"

  echo
  echo "Member '$label' provisioned."
  echo "  OS user:      $label"
  echo "  Home:         $home_dir"
  echo "  HERMES_HOME:  $hermes_home"
  echo "  Listen port:  $port"
  echo "  Service unit: $unit_name"
  echo "  Reachable at: $relay_url"
  echo
  if [ -z "$ingress_host" ]; then
    warn "This member is only reachable from this machine and cannot connect from their own laptop or phone. That happens when a loopback --url was passed, or when this host is not on a tailnet. Install Tailscale and re-run to fix it."
  fi
  echo "StageWhisper pairing code (hand this to '$label' only):"
  echo
  runuser -u "$label" -- env HERMES_HOME="$hermes_home" "$cli_path" pair-code --url "$relay_url" --label "$label" \
    || fail "Could not render the pairing code for '$label' via $cli_path pair-code."
}

print_approval_hint() {
  local venv_bin hermes_path
  venv_bin="$(dirname "$HERMES_VENV_PYTHON")"
  hermes_path="${venv_bin}/hermes"

  echo
  echo "One more step, the first time '$label' connects:"
  echo "their assistant will refuse to answer and print an approval code instead."
  echo "Approve it with that code, on this host:"
  echo
  echo "  sudo runuser -u ${label} -- env HERMES_HOME=${hermes_home} \\"
  echo "    ${hermes_path} pairing approve stagewhisper <CODE>"
  echo
  echo "'${label}' has their own Hermes profile, so a bare 'hermes pairing approve'"
  echo "run as root looks in the wrong place and reports the code as not found."
}

ensure_os_user
port="$(port_for_uid "$(id -u "$label")")"

if [ -n "$relay_url_override" ]; then
  relay_url="$relay_url_override"
elif [ -n "$relay_host" ]; then
  relay_url="https://${relay_host}:${port}"
elif detected_host="$(tailnet_dns_name)"; then
  echo "Using this host's tailnet name: ${detected_host}"
  relay_url="https://${detected_host}:${port}"
else
  warn "This host is not on a tailnet, or Tailscale is not running, so '$label' will only be reachable from this machine. Install Tailscale and re-run, or pass --relay-host."
  relay_url="http://127.0.0.1:${port}"
fi

ingress_host="$(host_from_url "$relay_url")"
if host_is_loopback "$ingress_host"; then
  ingress_host=""
fi

bootstrap_profile_dirs
ensure_config_yaml
ensure_env_file "$port" "$ingress_host"
seed_from_gateway_profile "$source_profile"
install_plugin_shim
chown -R "$label:$label" "$hermes_home" || fail "Could not chown $hermes_home to $label."
install_unit "$port"
verify_unit_listening "$port"
ON_FAIL_HOOK=""
if [ -n "$ingress_host" ]; then
  serve_on_tailnet "$port" || true
fi
print_pairing_code "$port" "$relay_url" "$ingress_host"
print_approval_hint
