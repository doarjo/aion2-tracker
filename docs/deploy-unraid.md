# Deploying to Unraid

Target: Unraid at `192.168.100.30`, hostname `Tower`.

Two independent steps. **You only need step 1 to use it.** Step 2 is purely
for reaching it from outside your house.

---

## Do I need a Cloudflare subdomain?

**No, not to run it.** Once step 1 is done the dashboard is at
`http://192.168.100.30:8080` from anywhere on your LAN.

You need `aion2.23nit.com` only if you want to open it from outside your
network. As of 2026-10-28 that subdomain does not exist yet. Your domain is
already on Cloudflare, so adding it is a few minutes of work whenever you want
it — see step 2.

---

## Step 1 — Get it running on Unraid

### 1a. Create the appdata layout

SSH to the server, or use the Unraid terminal in the web UI:

```bash
mkdir -p /mnt/user/appdata/aion2-tracker/{data,src}
cd /mnt/user/appdata/aion2-tracker
```

Only `data` and `src` are needed. Thresholds are mounted straight from the
checkout, so there is no separate config directory to keep in sync.

### 1b. Get the code onto the server

The repo is **private**, so cloning needs credentials. Pick one:

**Option 0 — make the repo public for a few minutes (fastest)**

Flip the repo to public, run the bootstrap script, flip it back:

```bash
curl -fsSL https://raw.githubusercontent.com/doarjo/aion2-tracker/main/deploy/bootstrap-unraid.sh | bash
```

That script does everything in step 1: preflight checks, clone, `.env`, build,
start, health check and first snapshot. Then set the repo private again.

Two things to accept before choosing this:

* **A public window is effectively permanent.** GitHub's public events feed is
  scraped continuously and repos get mirrored within seconds. For this repo the
  impact is low — no secrets, no personal email in the commit metadata, and the
  only identifying content is a character id readable from the official armory
  — but you cannot un-publish.
* **`git pull` stops working** once it is private again, so every future update
  means repeating the flip. If you expect to iterate, use option A or B below
  instead.

**Option A — clone with a Personal Access Token (simplest)**

Create a fine-grained PAT at <https://github.com/settings/tokens> with
*Contents: read-only* scope limited to `aion2-tracker`, then:

```bash
git clone https://<YOUR_PAT>@github.com/doarjo/aion2-tracker.git src
```

The token ends up in `src/.git/config`. That file is on your own server and
the token is read-only for one repo, which is an acceptable trade. To avoid it
entirely, use option B.

**Option B — deploy key (no token stored in the repo config)**

```bash
ssh-keygen -t ed25519 -f /root/.ssh/aion2_deploy -N ""
cat /root/.ssh/aion2_deploy.pub
```

Add that public key at
<https://github.com/doarjo/aion2-tracker/settings/keys> as a read-only deploy
key, then:

```bash
GIT_SSH_COMMAND="ssh -i /root/.ssh/aion2_deploy" \
  git clone git@github.com:doarjo/aion2-tracker.git src
```

**Option C — no git on the server at all**

Ship the already-built image from your Windows machine. Use `-o`, **not** a
pipe: PowerShell pipes are text streams and will corrupt the tar.

```powershell
docker save -o aion2-tracker.tar aion2-tracker:latest
scp aion2-tracker.tar root@192.168.100.30:/mnt/user/appdata/aion2-tracker/
ssh root@192.168.100.30 "docker load -i /mnt/user/appdata/aion2-tracker/aion2-tracker.tar"
```

Then run it without compose at all, since there is no checkout to read
`.env` or `./config` from:

```bash
docker run -d --name aion2-tracker --restart unless-stopped \
  -p 8080:8080 \
  --env-file /mnt/user/appdata/aion2-tracker/.env \
  -v /mnt/user/appdata/aion2-tracker/data:/data \
  aion2-tracker:latest
```

The image already contains `config/thresholds.yaml` at `/app/config`, so this
works, but you lose the ability to retune thresholds without a rebuild. With
this option put `.env` at `/mnt/user/appdata/aion2-tracker/.env` rather than
inside a checkout, and adjust step 1c accordingly.

Every update means repeating the save, copy and load. Fine as a one-off,
worse over time — prefer A or B if you expect to iterate.

### 1c. Write the environment file

It goes **inside the checkout**, at `src/.env`. That is where Compose looks
when the project directory is the repo root, and `.env` is gitignored so a
`git pull` will never clobber it.

```bash
cat > /mnt/user/appdata/aion2-tracker/src/.env <<'EOF'
AION2_REGION=eu
AION2_SERVER_ID=1302
AION2_CHARACTER_ID=5Ld-L4slCNU8JB2OIV-C-ehrKjJevX4KUri3vfirCBU%3D
AION2_CHARACTER_NAME=Epitet

AION2_RATE_LIMIT_SECONDS=4
AION2_SNAPSHOT_COOLDOWN_MINUTES=20
SNAPSHOT_INTERVAL_HOURS=6

AION2_DB_PATH=/data/aion2.db
AION2_CONFIG_DIR=/app/config
AION2_PROJECT_ROOT=/app
AION2_HOST=0.0.0.0
AION2_PORT=8080

MCP_AUTH_TOKEN=
TZ=Europe/Bucharest
EOF

chmod 600 /mnt/user/appdata/aion2-tracker/src/.env
```

**Keep `AION2_CHARACTER_ID` exactly as written, including the trailing `%3D`.**
It is percent-encoded and decoding it causes a 404.

### 1d. Build and start

```bash
cd /mnt/user/appdata/aion2-tracker/src
docker compose -f docker-compose.yml -f deploy/unraid.override.yml up -d --build
```

Both `-f` flags matter. The first sets the project directory to the repo root,
which is what makes `.env` and `./config` resolve. The second swaps the data
volume onto the appdata share and adds Unraid's WebUI labels.

First build pulls `python:3.13-slim` and takes a few minutes.

If 8080 is already taken on your Unraid box, change the host side of the port
mapping in `docker-compose.yml` before starting. Unraid's own GUI uses 80
and 443.

### 1e. Verify

```bash
docker ps --filter name=aion2-tracker
curl -s http://localhost:8080/health
```

You want `"status":"ok"` and `"thresholds_loaded":true`. If it says
`"degraded"`, the config mount did not land — you almost certainly ran compose
with only the override file, or from the wrong directory.

Then take the first snapshot. It is about 30 rate-limited requests, so roughly
two minutes:

```bash
docker exec aion2-tracker python -m aion2 snapshot
```

Open `http://192.168.100.30:8080` and you are done.

---

## Using the Unraid UI instead of the CLI

If you prefer clicking: install **Docker Compose Manager** from Community
Applications and add a stack named `aion2-tracker` pointing at
`/mnt/user/appdata/aion2-tracker/src`. Set its compose files to
`docker-compose.yml` and `deploy/unraid.override.yml`.

Steps 1a to 1c still apply, because the stack needs the checkout and the
`.env` beside it. The override carries `net.unraid.docker.webui`, so a WebUI
link appears on the Docker tab once it is up.

---

## Updating later

```bash
cd /mnt/user/appdata/aion2-tracker/src
git pull
docker compose -f docker-compose.yml -f deploy/unraid.override.yml up -d --build
```

Your database is on the appdata share and `.env` is gitignored, so neither is
touched by a pull or a rebuild. Thresholds come from the checkout, so a pull
will update them — if you have retuned them on the server, commit your
changes or keep a copy.

---

## Step 2 — Cloudflare Tunnel (optional, external access only)

Skip this entirely if LAN access is enough.

### 2a. Add the route

In the Cloudflare Zero Trust dashboard, under **Networks → Tunnels**, pick the
tunnel already running on your Unraid box and add a public hostname:

| Field | Value |
|---|---|
| Subdomain | `aion2` |
| Domain | `23nit.com` |
| Type | `HTTP` |
| URL | `192.168.100.30:8080` |

Cloudflare creates the DNS record for you. Do not create the `aion2` DNS
record by hand first; the tunnel route manages it.

Type is **HTTP**, not HTTPS. The app serves plain HTTP and the tunnel provides
TLS at Cloudflare's edge.

### 2b. Put Cloudflare Access in front of it

The dashboard has **no authentication of its own**. Anyone who reaches the URL
sees it. Without Access, `aion2.23nit.com` is public to the internet.

Under **Access → Applications**, add a self-hosted application for
`aion2.23nit.com` with a policy allowing your own email. That gives you a login
page in front of the dashboard.

### 2c. A conflict to know about in advance

Your original brief flagged this and it still holds. Cloudflare Access login
pages **break** the claude.ai custom-connector flow, because the connector
cannot complete an interactive browser login.

This does not matter yet, since the MCP server is not built. When it is, the
options are:

1. Put Access on `/` and the dashboard only, leaving `/mcp` open with a bearer
   token and a Cloudflare WAF rate-limit rule. The data is public game data, so
   the exposure is low, but the endpoint is reachable by anyone who finds it.
2. Implement OAuth on the MCP server. More work, correct answer.
3. Use the MCP server only from Claude Code over the LAN, and leave the whole
   hostname behind Access.

Decide that when the MCP server exists, not now.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `"status":"degraded"` in `/health` | `config/thresholds.yaml` not mounted. Step 1d. |
| `404` from PLAYNC in the logs | `AION2_CHARACTER_ID` was decoded. It must keep `%3D`. |
| Port 8080 already in use | Change the host side of the ports mapping. Unraid's GUI uses 80 and 443. |
| Snapshot returns `429` | Cooldown. Add `?force=true` to `/refresh` or wait it out. |
| Container healthy, dashboard empty | No snapshot yet. Run `docker exec aion2-tracker python -m aion2 snapshot`. |
| Permission denied on `/data` | `chown -R 1000:1000 /mnt/user/appdata/aion2-tracker/data` — the container runs as uid 1000. |

Logs:

```bash
docker logs -f aion2-tracker
```
