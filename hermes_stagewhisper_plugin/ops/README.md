# StageWhisper org deployment: one member, one OS user

This targets **Linux + systemd**. macOS is a dev host for editing this package's `ops/` scripts
only, none of them run there (no `useradd`, no `systemd`, no passwordless root).

These scripts ship inside the `hermes-platform-stagewhisper` wheel as package data
(`hermes_stagewhisper_plugin/ops/`). Operators do not clone or copy them anywhere; the
`stagewhisper-hermes` CLI, installed alongside the package, wraps them.

## Quickstart: onboarding a new org member

Two steps, run on the Hermes gateway host.

1. Install the plugin into the Hermes gateway's own venv (not `pipx`, not a per-member venv, the
   same venv that runs `hermes_cli.main gateway run`):

   ```bash
   uv pip install --python <hermes-venv>/bin/python hermes-platform-stagewhisper
   <hermes-venv>/bin/stagewhisper-hermes-install
   ```

   Hermes installs its venv with `uv`, so that venv has no `pip` binary and no `pip` module.
   Use `uv pip install --python` as shown, not `<hermes-venv>/bin/pip`.

   The second command prints the exact next command for this host, using its correct absolute
   path, for example:

   ```
   Next, set up a member and get their pairing code:
     sudo /usr/local/lib/hermes-agent/venv/bin/stagewhisper-hermes provision <name>
   ```

2. Provision the member and hand them what it prints:

   ```bash
   sudo /usr/local/lib/hermes-agent/venv/bin/stagewhisper-hermes provision alice
   ```

That's it. `provision` prints a QR code to scan on a phone, then a
`stagewhisper://pair?v=1&code=...` link to open on a computer (the desktop app prefills its
pairing field from that link, the member still clicks Pair, it never auto-connects), then the
raw `stagewhisper-pair:v1:...` code to paste as a fallback.

`stagewhisper-hermes` is not on `PATH` by default. It lives in the gateway venv's `bin/`. Either
use the absolute path each time, or symlink it onto `PATH` once:

```bash
ln -s /usr/local/lib/hermes-agent/venv/bin/stagewhisper-hermes /usr/local/bin/stagewhisper-hermes
```

## Topology

A member of an org is **one OS user + one Hermes profile + one gateway process + one listen port
+ one pairing token.** Nothing here is shared between members.

```
/home/alice/                    <- OS user "alice", mode 0700
  .hermes/                      <- alice's HERMES_HOME (her Hermes profile)
    config.yaml                 <- plugins.enabled: [stagewhisper]
    .env                        <- STAGEWHISPER_RELAY_TOKEN, STAGEWHISPER_LISTEN_PORT (mode 0600)
    plugins/stagewhisper/       <- plugin shim, installed from the shared venv's package
    memories/ sessions/ skills/ skins/ logs/ plans/ workspace/ cron/ home/

/home/bob/                      <- OS user "bob", mode 0700, completely separate tree
  .hermes/  ...

/etc/systemd/system/
  hermes-gateway-alice.service  <- User=alice Group=alice
  hermes-gateway-bob.service    <- User=bob   Group=bob
```

One shared Hermes install (one checkout, one venv) runs `N` separate gateway processes, one per
member, each started by its own `hermes-gateway-<label>` systemd unit as that member's own OS user.

There is no separate member registry. The OS already tracks every member: its user database is the
member list, and `useradd` is the only thing that ever needs to allocate anything, atomically, under
its own lock. Listing members is `systemctl list-units 'hermes-gateway-*'` (see Audit trail below)
or reading `/etc/passwd` for accounts with a `hermes-gateway-<label>.service` unit next to them;
nothing here maintains a parallel file that could drift from that truth.

## Why the OS user is the isolation boundary

Hermes has a profile-global memory store: everything under a profile's `memories/` gets injected
into that profile's system prompt on every turn, and the agent has a standing tool to write to it
(`hermes_cli/tools/memory_tool.py`, `get_memory_dir() = HERMES_HOME / "memories"`, no session/user
component in the path). One process serving two members means one memory store serving two
members: a member's playbook, extracted from their calls, ends up injected into a different
member's prompt. No amount of application-level bookkeeping (per-member tokens, per-member
`chat_id` prefixes) touches this, because it all sits *above* a shared file the agent can read and
write regardless of which "member" the request claims to be.

Giving every member their own `HERMES_HOME`, run by their own OS user, fixes this the same way the
kernel already fixes it for `read_file`/`terminal` tool calls on a `0700` home: a process running
as `bob` cannot open `/home/alice/.hermes/memories/MEMORY.md` no matter what its prompt says. This
is DAC (Unix file permissions), enforced by the kernel, not by our Python. The systemd hardening in
`hermes-gateway.service.template` (`ProtectHome=tmpfs` + `BindPaths`/`ReadWritePaths=<their own
home>`) adds a second, independent layer on top: even a misconfigured home directory, a symlink, or
a bug in some future shared code path still can't traverse into another member's home, because the
mount namespace hides everything under `/home/` except the one path explicitly reopened for that
unit.

Revocation follows the same logic: disabling and stopping a member's unit removes their *process*,
not just a token check inside a shared process. There is nothing left running that could still
answer for that member once the unit is stopped and confirmed inactive.

## Ports

The plugin binds a loopback listener per profile; nothing upstream allocates that port for you.
`provision-member.sh` derives it from the member's own OS uid:

```
port = STAGEWHISPER_PORT_BASE + (uid - STAGEWHISPER_UID_BASE)
```

with defaults `STAGEWHISPER_PORT_BASE=8765` (matching the plugin's own single-tenant default) and
`STAGEWHISPER_UID_BASE=100` (the lowest uid `useradd --system` hands out on a stock Debian/Ubuntu
host, adjust it if your `login.defs` `SYS_UID_MIN` differs). For example, uid 999 gives port 9664.
`useradd` already allocates uids atomically under its own lock, so this reuses a kernel-guaranteed
unique allocation instead of maintaining a second one: no shared counter, no file to race, and two
concurrent provisioning runs for different labels cannot land on the same port, because they cannot
land on the same uid. The same label always maps to the same port on a given host, without
consulting anything but `id -u <label>`.

If the computed port falls outside `1024-65535`, provisioning fails loudly rather than guessing;
widen the gap between `STAGEWHISPER_PORT_BASE` and `STAGEWHISPER_UID_BASE` or check that the uid is
sane. Before starting a brand-new member's unit, provisioning also checks the port isn't already
bound by some unrelated process on the host (skipped when the member's own unit is already the one
holding it, e.g. on a re-run), cheap insurance against unrelated software squatting on it.

## Adding a member

```
sudo stagewhisper-hermes provision alice --relay-host alice.your-tailnet.ts.net
```

(use the CLI's absolute path if it isn't on `PATH`; see Quickstart above.)

This is idempotent: run it again with the same label and it reuses the OS user, the profile, the
port and the token it already created, and just reprints the pairing code. It only mutates state
it hasn't already put in place.

The CLI must run as **root**. Before touching anything it refuses to run if `bash` isn't on `PATH`
or if `/run/systemd/system` doesn't exist: member provisioning is a systemd-Linux-only operation,
and it fails with a clear message rather than a confusing partial run on any other host. It also
sets `HERMES_VENV_PYTHON` to its own interpreter automatically, since that interpreter is, by
construction, the venv the plugin is installed into, so operators no longer need to set that
themselves.

What provisioning does, in order:

1. Validates `alice` against `^[a-z0-9][a-z0-9-]{0,30}$` (this string is later used verbatim in
   `useradd`, in filesystem paths, and in a systemd unit name), so it is treated as hostile input
   and rejected outside that exact shape (also rejects reserved names like `root`).
2. Creates a system OS user `alice`, no login shell, no password, home `/home/alice` mode `0700`.
   This `useradd` is the atomic claim on the label: it either succeeds and hands back a fresh,
   kernel-unique uid, or it fails outright because the account already exists (a second concurrent
   run for the same label loses this race cleanly instead of corrupting shared state). The port
   (see Ports above) is derived from that uid.
3. Bootstraps `/home/alice/.hermes` (and its `memories/`, `sessions/`, `skills/`, `plugins/`, …
   subdirectories) owned by `alice`, mode `0700`.
4. Writes `config.yaml` with `plugins.enabled: [stagewhisper]` (Hermes plugins are opt-in by
   default, `hermes_cli/plugins.py`); without this the shim installed in step 6 loads but never
   activates.
5. Generates a 48-character random token and writes `.env` (`STAGEWHISPER_RELAY_TOKEN`,
   `STAGEWHISPER_LISTEN_PORT`), mode `0600`, owned by `alice`.
6. Runs the shared venv's `hermes_stagewhisper_plugin.install` module to drop the plugin shim
   (`plugin.yaml` + `__init__.py`) into `alice`'s own `plugins/stagewhisper/`, then fixes
   ownership.
7. Renders `hermes-gateway.service.template` into
   `/etc/systemd/system/hermes-gateway-alice.service` and does
   `systemctl daemon-reload && systemctl enable --now hermes-gateway-alice.service`, then polls
   `systemctl is-active` until it reports `active` and the port is genuinely bound (a
   crash-looping unit can momentarily report `active`, so the port is checked too), or fails
   loudly with a `journalctl` pointer.
8. Renders the pairing code by running `stagewhisper-hermes pair-code` **as `alice`**
   (`runuser -u alice`), with `HERMES_HOME` pointed at her profile. It prints, in order: a QR to
   scan on a phone, the `stagewhisper://pair?v=1&code=...` link to open on a computer, and the raw
   `stagewhisper-pair:v1:...` code as a paste fallback. The plugin itself
   (`hermes_stagewhisper_plugin/cli.py`) owns the JSON encoding, the link and the QR rendering
   (via `segno`, if installed in the venv). These scripts never build or parse that payload
   themselves.

If any step fails after `useradd` created the user, that user and its unit are rolled back
(`systemctl disable --now`, unit file removed, `userdel -r alice`); a failed run does not leave a
half-provisioned member sitting around. If the OS user already existed before this run (an
idempotent re-run, or a genuinely already-provisioned member), a later failure is reported and left
alone rather than tearing down a member that was already working.

### Tailscale is required, and `--relay-host` is how you name it

The plugin only ever accepts a **loopback peer**. `_peer_is_loopback` in `listener.py` compares the
connecting socket against `127.0.0.1`/`::1` and there is no allowlist that relaxes it. Nothing
reaches the gateway directly over the network, by design.

Tailscale satisfies that because `tailscale serve` terminates the tailnet connection on the Hermes
host itself and proxies to `127.0.0.1:<port>`. The peer the plugin sees is still loopback; only the
`Host:` header changes, to the tailnet name. That header is checked separately by `_host_header_ok`,
which is why the member's `.env` carries `STAGEWHISPER_ALLOW_INGRESS_HOSTS`.

Serve the member's port, then provision with the name it is served on:

```bash
tailscale serve --bg --https <port> http://127.0.0.1:<port>
sudo stagewhisper-hermes provision alice --relay-host alice.your-tailnet.ts.net
```

`--relay-host HOST` resolves to `https://HOST:<port>`, because `tailscale serve` terminates TLS.
Provisioning writes that hostname into the member's `STAGEWHISPER_ALLOW_INGRESS_HOSTS` for you, so
the `Host:` check passes. Set it by hand only if you use `--url` with a host you serve some other
way.

Omit the flag only for a same-host trial. The pairing code then points at `http://127.0.0.1:<port>`,
the member cannot connect from their own devices, and provisioning says so.

## Revoking a member

```bash
sudo stagewhisper-hermes revoke alice
```

This stops and disables `hermes-gateway-alice.service`, **verifies** with `systemctl is-active`
that it actually went inactive (retrying for up to 10s), and only then reports success. If the
unit is still active after that window it exits non-zero instead of claiming revocation worked. It
runs `systemctl reset-failed` afterwards so the unit settles as `inactive` rather than `failed`,
and reports `verified stopped: yes (no longer running, port released)`. It then wipes `alice`'s
`.env`, so even a manual `systemctl start` afterwards comes up with no token and can't rejoin.

It deliberately does **not** delete the OS user, the home directory, or any of `alice`'s Hermes
state (memories, session history). Revocation is about cutting off access, not data destruction;
the output says so explicitly. To fully remove a member and their data:

```bash
sudo userdel -r alice
```

That single command is sufficient because there is no separate registry entry to clean up
alongside it: everything StageWhisper-specific about `alice` lives inside her own home.

## Audit trail

There is no in-plugin audit module in this design: the process boundary itself is the audit
boundary. Every member's gateway unit logs to the systemd journal
(`StandardOutput=journal`/`StandardError=journal`, `SyslogIdentifier=<label>`), and every unit
is named `hermes-gateway-<label>`, so the org-wide view is:

```
journalctl -u 'hermes-gateway-*'                    # everyone, merged, time-ordered
journalctl -u hermes-gateway-alice                  # just alice
journalctl -u 'hermes-gateway-*' --since -1h -f      # tail live across the whole org
```

This is metadata/log-level visibility (process start/stop, restarts, stdout/stderr), not a
transcript-content audit log; the plugin never puts call content on stdout. Retention and rotation
are journald's, not ours: check `Storage=` and `SystemMaxUse=`/`MaxRetentionSec=` in
`/etc/systemd/journald.conf` (set `Storage=persistent` so history survives reboots), and use
`journalctl --vacuum-time=90d` (or whatever your retention policy requires) rather than inventing a
separate log store. If compliance needs longer retention or off-host copies, forward the journal
(`systemd-journal-upload`/`-remote`, or a standard syslog/SIEM forwarder); that's an org-level
journald configuration question, not something these two scripts should own.

## What's still on the operator

- **Hosting.** These scripts assume a shared Hermes checkout + venv already exists on the host and
  is already fully set up (model provider configured, etc.). They do not run `hermes setup`; they
  only write the StageWhisper-specific `config.yaml`/`.env`/plugin shim into each member's own
  profile. If your Hermes version needs more than that to boot a fresh profile (a different default
  `config.yaml` shape, a required `SOUL.md`, seeded skills), provision the first member, confirm
  `journalctl -u hermes-gateway-<label>` shows a clean startup, and fold anything it's missing into
  `bootstrap_profile_dirs`/`ensure_config_yaml` in `provision-member.sh`.
- **Tunnels.** One per member, to their own port, terminating on their own tunnel identity. Not
  scripted here; pick whatever this org already uses (Tailscale, a reverse proxy per member, a
  plain SSH tunnel, etc.) and pass its hostname via `--relay-host`.
- **Package installation.** `hermes-platform-stagewhisper` must be installed into the *same* venv
  the gateway runs from (see Quickstart); the plugin shim does
  `from hermes_stagewhisper_plugin.adapter import StageWhisperAdapter`, which only resolves if that
  package is importable by whatever Python runs `hermes_cli.main gateway run`. The `ops/` scripts
  ship inside that same package, so this is the only install step; there's nothing separate to
  clone or copy.

## Advanced: invoking the scripts directly

`stagewhisper-hermes provision`/`revoke` are thin wrappers: they locate the bundled scripts inside
the installed package, check root/`bash`/systemd, default `HERMES_VENV_PYTHON` to their own
interpreter, and run the script under `bash`. This is the documented path.

The scripts themselves still work stand-alone, which is occasionally useful when developing on
them or debugging around the CLI's guard rails. Find them inside the installed package (e.g.
`<venv>/lib/python*/site-packages/hermes_stagewhisper_plugin/ops/`) and run directly:

```bash
sudo HERMES_VENV_PYTHON=/opt/hermes/venv/bin/python3 \
  ./provision-member.sh alice --relay-host alice.your-tailnet.ts.net

sudo ./revoke-member.sh alice
```

`provision-member.sh` also accepts `--venv-python PATH` as an alternative to the environment
variable. Neither script performs the CLI's bash/systemd preflight checks, so run them only on a
systemd Linux host as root, same as the CLI requires.

## Files

- `provision-member.sh`: idempotent, fail-closed member provisioning.
- `revoke-member.sh`: fail-closed revocation (stop, verify, invalidate token).
- `hermes-gateway.service.template`: the systemd unit shape, rendered per member.
  Named `hermes-gateway.service.template` rather than `hermes-gateway@.service` on purpose: it is
  not a real systemd `%i` instance template (systemd would let you `systemctl enable
  hermes-gateway@alice`, which this setup does not support and which would not match the
  `hermes-gateway-<label>` naming Hermes' own tooling looks for). `provision-member.sh` renders it
  with `sed` into a fully static, per-member unit file instead, the same way Hermes' own
  `hermes gateway install --system` generates one static unit per profile
  (`hermes_cli/gateway.py:generate_systemd_unit`).
- `lib/common.sh`: shared label validation, uid-derived port math, and the port liveness check,
  sourced by both scripts so the security-critical label regex exists in exactly one place. It does
  not encode the pairing code; that stays inside the plugin (`hermes_stagewhisper_plugin/cli.py`),
  the one place that already knows how to read a profile's own token.

All three ship as package data (`hermes_stagewhisper_plugin/ops/*.sh`, `ops/*.template`,
`ops/lib/*.sh` in `pyproject.toml`), so installing `hermes-platform-stagewhisper` is what puts them
on the host, so there is no separate deployment step.

### Hardening in the unit template

`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectKernelTunables`,
`ProtectKernelModules`, `ProtectKernelLogs`, `ProtectControlGroups`, `ProtectClock`,
`ProtectHostname`, `RestrictSUIDSGID`, `RestrictRealtime`, `LockPersonality`, `RemoveIPC` cost
nothing for a plain Python network service that needs no kernel module access, no SUID, no realtime
scheduling; they only close doors a compromised gateway process would otherwise have open.

`ProtectHome=tmpfs` on its own would hide **all** of `/home` from the service, including the
member's own home it needs to read and write every session, so it's paired with `BindPaths` and
`ReadWritePaths=<that member's home>`, which reopens exactly that one path inside the mount
namespace. The net effect: this member's process can read/write its own home and nothing else
under `/home`, and combined DAC (`0700`, owned by that OS user) already denies it that even without
the namespace hiding; this is the second, independent layer described above, not the only one.
