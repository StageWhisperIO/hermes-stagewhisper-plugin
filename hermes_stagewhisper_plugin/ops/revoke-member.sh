#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  echo "Usage: $(basename "$0") <label>"
  echo
  echo "Stops and disables that member's gateway service, verifies it actually"
  echo "stopped, and removes their relay token so a restart cannot resurrect it."
  echo "Does NOT delete the OS user, home directory or any of their Hermes state."
}

label="${1:-}"
if [ -z "$label" ] || [ "$label" = "-h" ] || [ "$label" = "--help" ]; then
  usage
  exit 1
fi

require_root
validate_label "$label"
require_python_plugin_stack "$HERMES_VENV_PYTHON"

home_dir="$(home_dir_for "$label")"
hermes_home="$(hermes_home_for "$label")"
unit_name="$(unit_name_for "$label")"
env_path="${hermes_home}/.env"

id "$label" >/dev/null 2>&1 || fail "No OS user '$label' on this host. Nothing to revoke."

unit_found=false
if systemctl list-unit-files "$unit_name" >/dev/null 2>&1; then
  unit_found=true
  systemctl disable --now "$unit_name" >/dev/null 2>&1 || true
fi

state="$(systemctl is-active "$unit_name" 2>/dev/null || true)"
attempt=0
while [ "$state" = "active" ] || [ "$state" = "activating" ] || [ "$state" = "deactivating" ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 10 ]; then
    fail "Gateway unit '$unit_name' is still '$state' after waiting 10s. Revocation FAILED - the member's process may still be reachable. Investigate: systemctl status $unit_name"
  fi
  sleep 1
  state="$(systemctl is-active "$unit_name" 2>/dev/null || true)"
done

served_port=""
if served_port="$(port_for_uid "$(id -u "$label")" 2>/dev/null)"; then
  tailscale_unserve_port "$served_port"
fi

member_uid="$(id -u "$label")"
token_removed=false
remove_output=""
if remove_output="$("$HERMES_VENV_PYTHON" -m hermes_stagewhisper_plugin.secure_fs remove-owned-file "$env_path" --owner-uid "$member_uid" 2>&1)"; then
  if [ "$remove_output" = "removed" ]; then
    token_removed=true
  fi
else
  fail "Refusing to remove '$env_path': $remove_output"
fi

systemctl reset-failed "$unit_name" >/dev/null 2>&1 || true

echo "Revoked member '$label':"
if [ "$unit_found" = true ]; then
  echo "  - gateway unit '$unit_name': disabled and stopped"
else
  echo "  - gateway unit '$unit_name': no unit file found on this host (was it ever installed here?)"
fi
echo "  - verified stopped: yes (no longer running, port released)"
if [ -n "$served_port" ]; then
  echo "  - tailnet: port $served_port is no longer served"
fi
if [ "$token_removed" = true ]; then
  echo "  - relay token: removed from $env_path"
else
  echo "  - relay token: no .env file found at $env_path; nothing to remove"
fi
echo
echo "Not done automatically:"
echo "  - OS user '$label' and home directory '$home_dir' were preserved (memories, session history, config)."
echo "  - To fully delete this member and their data: userdel -r $label"
