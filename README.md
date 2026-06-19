## StageWhisper Hermes Platform Adapter

Hermes platform adapter that exposes the StageWhisper desktop app as a first-class messaging surface for your Hermes Agent. Live-call transcripts arrive on a `:reasoning` chat thread, in-call chat arrives on a `:chat` thread, and the agent's reply is POSTed back to the desktop's per-task callback URL.

### Install

```bash
pipx install hermes-platform-stagewhisper
stagewhisper-hermes-install
```

The installer drops a minimal shim into `~/.hermes/plugins/stagewhisper/`:

- `PLUGIN.yaml`
- `adapter.py` (6-line shim that re-exports the in-package adapter)

`pipx upgrade hermes-platform-stagewhisper` patches the adapter without re-running the installer. `stagewhisper-hermes-install --uninstall` removes the shim.

### Configure

Required environment variables (set in `~/.hermes/.env` or via `hermes config`):

| Variable | Purpose |
|---|---|
| `STAGEWHISPER_RELAY_TOKEN` | Bearer token shared with the desktop app (32+ chars). |
| `STAGEWHISPER_LISTEN_PORT` | Loopback TCP port the adapter binds (1024-65535). |

Optional:

| Variable | Default | Purpose |
|---|---|---|
| `STAGEWHISPER_LISTEN_HOST` | `127.0.0.1` | Must be loopback. |
| `STAGEWHISPER_ALLOW_INGRESS_HOSTS` | unset | Comma-separated `Host` names accepted in addition to localhost. Set this to your tailnet name when reaching the adapter through `tailscale serve`. |
| `STAGEWHISPER_ALLOW_CALLBACK_URLS` | unset | Comma-separated exact origins (scheme + host + port) the adapter may POST replies to. Loopback is trusted implicitly only while `STAGEWHISPER_ALLOW_INGRESS_HOSTS` is unset; once remote ingress is enabled, every callback origin (loopback included) must be listed here. |
| `STAGEWHISPER_ALLOWED_USERS` | unset | Comma-separated `session_id` allowlist. |
| `STAGEWHISPER_ALLOW_ALL_USERS` | unset | `1` to bypass allowlist. |
| `STAGEWHISPER_MAX_CONCURRENT` | `4` | Max in-flight tasks. |
| `STAGEWHISPER_CALLBACK_TIMEOUT_S` | `10` | Callback HTTP timeout. |
| `STAGEWHISPER_DEDUP_CACHE_SIZE` | `2048` | Idempotency LRU size. |

After setting the env vars, restart Hermes. `hermes status` will list `StageWhisper (plugin) - connected`.

### Wire protocol

Desktop posts to `http://127.0.0.1:<port>/v1/incoming` with bearer auth. Three event reasons:

- `transcript_chunk` (only `is_final: true` routes through the agent) -> chat thread `sw:<session>:reasoning`
- `chat_message` -> chat thread `sw:<session>:chat`
- `system_prelude` -> stashed and prepended to the next inbound message for that session as `[Context: ...]`, then cleared

The adapter POSTs the agent's reply to `<callback.url>/tasks/<task_id>` on the desktop side, where `callback.url` is a loopback base URL the desktop included on the inbound event.

### Local development

```bash
uv run --with pytest --with pytest-asyncio python -m pytest -v
```
