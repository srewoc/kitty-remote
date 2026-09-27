# Deployment

A public setup has two halves: the relay runs on a server behind HTTPS, and the agent runs on your desktop as a systemd user service. The agent connects out to the relay; Kitty's sockets never leave the desktop.

## Relay with Docker Compose

Requirements: Docker Compose, a domain that points to the server, and inbound ports 80 and 443.

From the project root on the server:

```bash
cp deploy/env.example deploy/.env
# set DOMAIN in deploy/.env

docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm relay create-admin
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

`create-admin` asks for the password interactively. Caddy obtains and renews TLS certificates and proxies HTTP and WebSocket traffic. The relay container runs as a non-root user, exposes port 8765 only on the internal network, and keeps its database in the `relay-data` volume at `/data/relay.sqlite3`.

`KR_ORIGIN` must match the browser address exactly, for example `https://terminal.example.com` with no trailing slash. Keep `KR_DEV=0` in production.

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail 50 relay caddy
```

`GET /api/health` reports process health and the protocol version. Logs never contain input, screen content or plaintext tokens.

If the server is slow to reach PyPI, pass a mirror at build time with `--build-arg PIP_INDEX_URL=https://<mirror>/simple`. Dependencies are exported from `uv.lock` with pinned versions and hashes and verified on install, so a mirror cannot change them.

### Server with an existing HTTPS proxy

If ports 80 and 443 are already in use, use `deploy/compose.existing-proxy.yaml` instead of `deploy/compose.yaml`, and add `-p kitty-remote` to every command so the project name and database volume stay the same. This file runs only the relay, listening on `127.0.0.1:18765`. Add a site to your existing proxy, for example with Caddy:

```caddyfile
terminal.example.com {
    request_body {
        max_size 8KB
    }
    reverse_proxy 127.0.0.1:18765
}
```

Back up and validate the full proxy configuration before applying it.

### Public IP without a domain

Let's Encrypt also issues certificates for IP addresses. Set `DOMAIN` to the server's public IP, use `compose.existing-proxy.yaml`, and merge the site from `deploy/Caddyfile.ip` into your Caddy configuration, replacing `{$DOMAIN}` with the IP. Also set `default_sni <your IP>` in the global block for clients that do not send SNI when connecting to an IP address. The template uses the short-lived certificate profile, which Caddy renews automatically; port 80 must stay reachable for HTTP-01 validation. See [IP certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) and the [Caddy ACME issuer](https://caddyserver.com/docs/caddyfile/directives/tls#issuers).

## Pairing

On the desktop, from the project root:

```bash
uv run kitty-remote pair --relay https://terminal.example.com --name 'My desktop'
```

Open the same address on your phone, sign in, enter the pairing code and confirm the device name. The credentials are written to `~/.config/kitty-remote/agent.json` with mode `0600`.

## Agent as a systemd user service

Check that `uv run kitty-remote doctor` finds your Kitty instances and that `uv run kitty-remote agent` connects before installing the service. Only one agent can use a credentials file at a time.

1. Copy `deploy/kitty-remote-agent.service` to `~/.config/systemd/user/kitty-remote-agent.service`.
2. Replace both `/REPLACE/ABSOLUTE/PROJECT/PATH` placeholders with the project path. Keep the quotes around the executable in `ExecStart`; `WorkingDirectory` takes no quotes and may contain spaces.
3. Append `--credentials`, `--socket` or `--socket-glob` to `ExecStart` if needed. Add Kitty's directory to the unit's `PATH` if it is installed somewhere unusual.
4. If your Kitty sockets live in `/tmp`, remove `PrivateTmp=true`.

```bash
systemctl --user daemon-reload
systemctl --user enable --now kitty-remote-agent.service
systemctl --user status kitty-remote-agent.service
journalctl --user -u kitty-remote-agent.service --since '10 minutes ago'
```

The agent does not need root. Whether terminal tasks survive logout, suspend or a Kitty restart depends on your desktop and the programs themselves.

## Configuration and limits

| Setting | Default |
| --- | --- |
| `KR_DATABASE` | `.state/relay.sqlite3`, relative to the working directory |
| `KR_ORIGIN` | `http://127.0.0.1:8765`; this default requires `KR_DEV=1` |
| `KR_DEV` | `0`; plain HTTP is allowed only for localhost in development |
| `KR_WEB_DIR` | `web/dist` |
| Device credentials | `~/.config/kitty-remote/agent.json`, or `--credentials` |
| Pairing code / session / control lease | 5 minutes / 12 hours / 30 seconds, renewed by active connections |
| Input validity | at most 10 seconds after receipt |
| De-duplication | up to 4,096 entries kept for 60 seconds, in memory |
| Text input | 64 KiB of UTF-8 |
| History | 16 MiB of UTF-8, sent in chunks of at most 1 MiB |

## Upgrading

Stop the agent, update the code, then run `uv sync --locked`, `npm ci --prefix web` and `npm run build --prefix web` on the desktop, and rebuild the relay image on the server. The web app, relay and agent share one protocol version, so update them together. Restarting the relay or the agent drops connections; pending input is never replayed. Tag the old image before upgrading, for example `docker tag kitty-remote:0.1.0 kitty-remote:previous`, so you can roll back.

## Removal

Revoke the device in the web app first, then stop the agent:

```bash
systemctl --user disable --now kitty-remote-agent.service
```

Delete the unit file and `~/.config/kitty-remote/agent.json`, then run `systemctl --user daemon-reload`. Stop the relay with `docker compose ... down`; the database volume is kept unless you remove it explicitly, which deletes the account and all pairings. Stop the relay or use SQLite's online backup API before backing up the database.
