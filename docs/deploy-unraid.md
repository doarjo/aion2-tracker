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

# Generate: python -c "import secrets; print(secrets.token_hex(32))"
MCP_AUTH_TOKEN=
# Required behind a tunnel: the MCP SDK rejects unknown Host headers.
MCP_ALLOWED_HOSTS=192.168.100.30,aion2.23nit.com
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

## Step 2 — External access via Cloudflare Tunnel

Only needed to reach the dashboard from outside your network. Skip it if LAN
access is enough.

**Order matters here.** Create the Access policy *before* the public hostname,
or there is a window where the dashboard is on the open internet with no login.

### 2a. Find out what tunnel you already have

Run on Tower:

```bash
echo "--- cloudflared containers ---"
docker ps -a --filter name=cloudflare --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
echo "--- host process ---"
pgrep -a cloudflared || echo "none"
echo "--- config on appdata ---"
ls -la /mnt/user/appdata/cloudflared/ 2>/dev/null || echo "no /mnt/user/appdata/cloudflared"
echo "--- run args (shows token vs config file) ---"
docker inspect $(docker ps -q --filter name=cloudflare) \
  --format '{{.Name}}{{range .Config.Cmd}} {{.}}{{end}}' 2>/dev/null || true
```

The result puts you in one of three cases.

| What you see | Case |
|---|---|
| A running container whose command contains `--token` | **A — dashboard-managed tunnel** |
| A running container pointing at a `config.yml` | **B — locally-managed tunnel** |
| Nothing | **C — no tunnel yet** |

### 2b. Create the Access application FIRST

Cloudflare Zero Trust → **Access → Applications → Add an application →
Self-hosted**.

| Field | Value |
|---|---|
| Application name | `AION 2 Tracker` |
| Session duration | 1 month, or whatever suits |
| Subdomain | `aion2` |
| Domain | `23nit.com` |

Then add a policy:

| Field | Value |
|---|---|
| Policy name | `Me only` |
| Action | `Allow` |
| Include | `Emails` → your own email address |

Save. Cloudflare now requires a login for that hostname even before the
hostname exists, which is exactly the ordering you want.

The free Zero Trust plan covers up to 50 users, so this costs nothing.

### 2c. Publish the hostname

**Case A — dashboard-managed tunnel**

Zero Trust → **Networks → Tunnels** → your tunnel → **Public Hostnames** →
**Add a public hostname**:

| Field | Value |
|---|---|
| Subdomain | `aion2` |
| Domain | `23nit.com` |
| Type | `HTTP` |
| URL | `192.168.100.30:8080` |

Cloudflare creates the DNS record itself. **Do not pre-create an `aion2` DNS
record** — the tunnel route manages it and a manual record conflicts.

Type is **HTTP**, not HTTPS. The container serves plain HTTP; TLS terminates at
Cloudflare's edge.

If cloudflared runs on Unraid itself, `localhost:8080` also works and saves a
hop. Use the explicit IP if the tunnel runs on a different box.

**Case B — locally-managed tunnel**

Add an ingress rule to `config.yml`, above the catch-all:

```yaml
ingress:
  - hostname: aion2.23nit.com
    service: http://192.168.100.30:8080
  # ... your existing rules ...
  - service: http_status:404          # catch-all stays last
```

Order matters: the first matching rule wins and the catch-all must stay at the
bottom. Then create the DNS record and restart:

```bash
cloudflared tunnel route dns <TUNNEL_NAME> aion2.23nit.com
docker restart cloudflared
```

**Case C — no tunnel yet**

Zero Trust → **Networks → Tunnels → Create a tunnel** → **Cloudflared**. Name
it something like `tower`. Copy the token it gives you, then on Tower:

```bash
docker run -d --name cloudflared --restart unless-stopped \
  --network host \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run --token <YOUR_TOKEN>
```

The token is a credential — treat it like a password and keep it out of git.
Then follow case A to add the hostname.

### 2d. Verify

```bash
# From Tower, confirm the app itself is still fine
curl -s http://localhost:8080/health
```

Then from your phone on mobile data, with WiFi off, open
<https://aion2.23nit.com>. You should get a **Cloudflare login page**, not the
dashboard. If the dashboard loads without asking who you are, the Access policy
is not attached — fix that before leaving it up.

DNS propagation is usually seconds, but give it a couple of minutes.

### 2e. The MCP conflict, for later

Access login pages break the claude.ai custom-connector flow, because the
connector cannot complete an interactive browser login. This does not bite yet,
since the MCP server is not built.

When it is, the options are:

1. Add a **bypass policy** scoped to the `/mcp` path only, protected by its own
   bearer token plus a Cloudflare rate-limit rule. Keeps Access on the
   dashboard.
2. Implement OAuth on the MCP server. More work, correct answer.
3. Use MCP only from Claude Code over the LAN and leave the whole hostname
   behind Access.

Decide when the MCP server exists, not now.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `"status":"degraded"` in `/health` | `config/thresholds.yaml` not mounted. Step 1d. |
| `404` from PLAYNC in the logs | `AION2_CHARACTER_ID` was decoded. It must keep `%3D`. |
| Port 8080 already in use | Change the host side of the ports mapping. Unraid's GUI uses 80 and 443. |
| `unknown shorthand flag: 'f' in -f` | `docker compose` is not installed. Unraid ships Docker without it; it comes from the **Docker Compose Manager** plugin. Use `bash deploy/bootstrap-unraid.sh` instead, which falls back to plain `docker run`. |
| `cli-plugins/docker-compose: permission denied` | Same cause: a half-installed Compose plugin. `chmod +x` it, or reinstall the plugin, or use the bootstrap script. Note Unraid rebuilds `/root` on boot, so a chmod there does not persist. |
| Snapshot returns `429` | Cooldown. Add `?force=true` to `/refresh` or wait it out. |
| Container healthy, dashboard empty | No snapshot yet. Run `docker exec aion2-tracker python -m aion2 snapshot`. |
| Permission denied on `/data` | `chown -R 1000:1000 /mnt/user/appdata/aion2-tracker/data` — the container runs as uid 1000. |
| `curl ... bootstrap-unraid.sh` gives 404 while the repo is public | `raw.githubusercontent.com` caches negative responses. Wait a minute, or fetch through the API: `curl -fsSL -H "Accept: application/vnd.github.raw" https://api.github.com/repos/doarjo/aion2-tracker/contents/deploy/bootstrap-unraid.sh \| bash` |
| `aion2.23nit.com` shows Cloudflare error 1033 | Tunnel is down or the hostname is not routed. Check `docker logs cloudflared`. |
| Error 502 through the tunnel | Tunnel reached Cloudflare but not the app. Check the ingress URL is `http://` and the port matches. |
| Dashboard loads publicly with no login | Access policy is not attached to the hostname. Fix immediately — step 2b. |
| Access login loops or rejects you | The policy's Include rule does not match your email. Check for a typo in Zero Trust → Access → Applications. |

Logs:

```bash
docker logs -f aion2-tracker
```
