# shellcheck shell=bash
LABEL_REGEX='^[a-z0-9][a-z0-9-]{0,30}$'
PORT_BASE="${STAGEWHISPER_PORT_BASE:-8765}"
UID_BASE="${STAGEWHISPER_UID_BASE:-100}"
HERMES_VENV_PYTHON="${HERMES_VENV_PYTHON:-/opt/hermes/venv/bin/python3}"
SYSTEMD_UNIT_DIR="${STAGEWHISPER_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"

RESERVED_LABELS="root bin daemon sys sync games man lp mail news uucp proxy www-data backup list irc gnats nobody systemd sshd messagebus hermes adm disk wheel sudo staff operator shutdown halt mem kmem tty lxd docker"

ON_FAIL_HOOK="${ON_FAIL_HOOK:-}"

fail() {
  echo "ERROR: $*" >&2
  if [ -n "$ON_FAIL_HOOK" ]; then
    "$ON_FAIL_HOOK"
  fi
  exit 1
}

warn() {
  echo "WARNING: $*" >&2
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "This script must be run as root."
  fi
}

validate_label() {
  local label="$1"
  if [ -z "$label" ]; then
    fail "A member label is required."
  fi
  if [[ ! "$label" =~ $LABEL_REGEX ]]; then
    fail "Invalid label '$label'. Labels must fully match $LABEL_REGEX: lowercase letters, digits and hyphens only, must start with a letter or digit, max 31 characters."
  fi
  local reserved
  for reserved in $RESERVED_LABELS; do
    if [ "$label" = "$reserved" ]; then
      fail "Label '$label' collides with a reserved system account name. Choose a different label."
    fi
  done
}

unit_name_for() {
  printf 'hermes-gateway-%s.service' "$1"
}

home_dir_for() {
  printf '/home/%s' "$1"
}

hermes_home_for() {
  printf '%s/.hermes' "$(home_dir_for "$1")"
}

home_channel_for() {
  printf 'sw:home-%s:chat' "$1"
}

require_python_plugin_stack() {
  local python_bin="$1"
  if [ ! -x "$python_bin" ]; then
    fail "HERMES_VENV_PYTHON ('$python_bin') does not exist or is not executable. Point it at the shared Hermes gateway's venv python, e.g. HERMES_VENV_PYTHON=/opt/hermes/venv/bin/python3."
  fi
  if ! "$python_bin" -c "import hermes_cli, hermes_stagewhisper_plugin" >/dev/null 2>&1; then
    fail "'$python_bin' cannot import hermes_cli and hermes_stagewhisper_plugin. Install hermes-platform-stagewhisper into the SAME venv the Hermes gateway runs from before provisioning members."
  fi
}

nologin_shell() {
  command -v nologin 2>/dev/null || {
    if [ -x /usr/sbin/nologin ]; then
      echo /usr/sbin/nologin
    elif [ -x /sbin/nologin ]; then
      echo /sbin/nologin
    else
      echo /usr/sbin/nologin
    fi
  }
}

port_live_in_use() {
  local port="$1" output
  if command -v ss >/dev/null 2>&1; then
    output="$(ss -Htln "( sport = :$port )" 2>/dev/null || true)"
    [ -n "$output" ]
    return
  fi
  if command -v netstat >/dev/null 2>&1; then
    output="$(netstat -tln 2>/dev/null | awk '{print $4}' || true)"
    printf '%s\n' "$output" | grep -Eq "[.:]${port}\$"
    return
  fi
  local hex
  hex="$(printf '%04X' "$port")"
  grep -qi ":${hex} " /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

port_for_uid() {
  local uid="$1" port
  if [ "$uid" -lt "$UID_BASE" ]; then
    fail "UID $uid is below UID_BASE ($UID_BASE); cannot derive a port for it. Set STAGEWHISPER_UID_BASE below the lowest UID useradd assigns on this host."
  fi
  port=$((PORT_BASE + uid - UID_BASE))
  if [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
    fail "Derived port $port (uid $uid, PORT_BASE $PORT_BASE, UID_BASE $UID_BASE) is outside 1024-65535. Adjust STAGEWHISPER_PORT_BASE/STAGEWHISPER_UID_BASE."
  fi
  echo "$port"
}

generate_token() {
  head -c 48 < <(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom)
}

tailscale_bin() {
  local candidate
  if command -v tailscale >/dev/null 2>&1; then
    command -v tailscale
    return 0
  fi
  for candidate in /usr/bin/tailscale /usr/local/bin/tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

tailnet_dns_name() {
  local ts status
  ts="$(tailscale_bin)" || return 1
  status="$("$ts" status --json 2>/dev/null)" || return 1
  printf '%s' "$status" | "$HERMES_VENV_PYTHON" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if (data.get("BackendState") or "") != "Running":
    sys.exit(1)
name = ((data.get("Self") or {}).get("DNSName") or "").strip().rstrip(".")
if not name:
    sys.exit(1)
print(name)
'
}

tailscale_serve_port() {
  local port="$1" ts output
  ts="$(tailscale_bin)" || { echo "tailscale is not installed on this host"; return 1; }
  if ! output="$("$ts" serve --bg --https "$port" "http://127.0.0.1:${port}" 2>&1)"; then
    printf '%s' "$output"
    return 1
  fi
  return 0
}

tailscale_unserve_port() {
  local port="$1" ts
  ts="$(tailscale_bin)" || return 0
  "$ts" serve --https "$port" off >/dev/null 2>&1 || true
}

tailscale_serve_active() {
  local port="$1" ts status
  ts="$(tailscale_bin)" || return 1
  status="$("$ts" serve status --json 2>/dev/null)" || return 1
  printf '%s' "$status" | "$HERMES_VENV_PYTHON" -c '
import json, sys
port = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
web = data.get("Web") or {}
sys.exit(0 if any(str(key).endswith(":" + port) for key in web) else 1)
' "$port"
}
