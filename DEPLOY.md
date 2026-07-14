# Deploying Jarvis 24/7 (secure hosting)

This makes the reporting app reachable off your Mac, with a login gate and secrets
kept server-side. Auth is **opt-in**: locally it's off; in the container the
`Dockerfile` sets `JARVIS_REQUIRE_AUTH=1`, so a session (or API key) is required.

## What runs in the cloud
✅ Reporting suite (Query Panel, Designer, Dashboards, maps), the brain loop,
BigQuery, governance/row-security, SQL reviews, evaluations, settings, share links,
scheduled email.
❌ macOS-only integrations (Apple Calendar/Mail/Notes, opening local apps) — Linux
can't run AppleScript. Those features are simply inert in the container.

## 1. Secrets to set (as env vars / platform secrets — never in git)
| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude (voice, brain skills, evaluations) |
| `GOOGLE_APPLICATION_CREDENTIALS` **or** inline creds | BigQuery service account |
| `BIGQUERY_PROJECT_ID` | e.g. `oh-data-warehouse` |
| `JARVIS_AUTH_SECRET` | random 32+ char string — signs session tokens |
| `JARVIS_REQUIRE_AUTH` | `1` to enforce the login gate (set in Dockerfile) |
| `SMTP_HOST/PORT/FROM/USER/PASS` | scheduled-report email (optional) |
| `JARVIS_VAULT` | optional path for the brain vault (use a persistent volume) |

> Put the BigQuery key file on a secret mount and point
> `GOOGLE_APPLICATION_CREDENTIALS` at it. Never bake it into the image.

## 2. Create the first admin login
The login verifies against `~/.config/jarvis/users.json` (salted password hash).
Before deploying, create an admin user with a password via **Admin → Users**
(Create User → set password), or seed `users.json` on the persistent volume.

## 3. Persistent storage
Mount a volume for state that must survive restarts:
`~/.config/jarvis/` (users, api keys, auth secret) and the semantic dir
(`semantic/*.json` — queries, reports, policies, reviews, evaluations, settings).

## 4. Deploy — Fly.io (example)
```bash
fly launch --no-deploy               # detects the Dockerfile
fly secrets set ANTHROPIC_API_KEY=... BIGQUERY_PROJECT_ID=oh-data-warehouse \
                JARVIS_AUTH_SECRET=$(openssl rand -hex 32)
fly volumes create jarvis_data --size 1
# add [[mounts]] source="jarvis_data" destination="/root/.config" in fly.toml
fly deploy
```
Your app: `https://<app>.fly.dev/app`.

## 4b. Deploy — Render (example)
- New → Web Service → connect the repo → Docker.
- Add the env vars above; add a Disk mounted at `/root/.config`.
- Deploy. App at `https://<service>.onrender.com/app`.

## 5. Turn Vercel into the public front door (optional)
Deploy `jarvis-landing/` to Vercel as the marketing page and link its CTA to the
hosted `/app` URL.

## Security checklist
- [ ] `JARVIS_REQUIRE_AUTH=1` (Dockerfile default) — login enforced.
- [ ] `JARVIS_AUTH_SECRET` set to a strong random value.
- [ ] BigQuery key on a secret mount, not in the image.
- [ ] An admin user with a strong password exists.
- [ ] (Optional) Rotate/enforce API keys for programmatic access.
- [ ] Restrict the SQL policy (Admin → Security) and set row-security props.
