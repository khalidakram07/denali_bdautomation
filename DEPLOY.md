# Deploy Guide — Denali BD Automation

## Local development (already working)

```
python -m venv venv
venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env           # then fill in your keys
python main.py
```

Open http://127.0.0.1:8000

Leave `APP_PASSWORD` empty in `.env` for dev — auth is disabled when blank.

---

## Production deploy on Render (one-time setup, ~15 min)

### 1. Push to GitHub

```
git add .
git commit -m "Ready for Render deploy"
git push origin main
```

### 2. Create the Render blueprint

1. Sign in at https://dashboard.render.com
2. Click **New +** → **Blueprint**
3. Connect your GitHub repo (`denali_bdautomation`)
4. Render reads `render.yaml` and previews:
   - 1 web service named `denali-bd`
   - 1 persistent disk (1 GB, mounted at `/data`)

### 3. Fill in the secret env vars

Render prompts you for the variables marked `sync: false`:

| Variable | What to enter |
|---|---|
| `APP_PASSWORD` | A strong password for Maryam to log in with |
| `ANTHROPIC_API_KEY` | From https://console.anthropic.com/settings/keys |
| `MAILBOXES_JSON` | Paste your full `mailboxes.json` as a single line — include the brackets |
| `ALLOWED_ORIGINS` | Skip for now; come back after first deploy |

For `MAILBOXES_JSON`, take your local `mailboxes.json` and paste it as one line:

```
[{"email":"khalid.aieng07@gmail.com","display_name":"1st mailbox","smtp_host":"smtp.gmail.com","smtp_port":587,"app_password":"dcyb nuio nhbs jnrt"}]
```

### 4. Click Apply

First build: ~3 minutes. When it goes green, you get a URL like:

```
https://denali-bd-xxxx.onrender.com
```

### 5. Add the URL to ALLOWED_ORIGINS

Go to the service → Environment → add `ALLOWED_ORIGINS=https://denali-bd-xxxx.onrender.com` → Save (triggers a redeploy).

### 6. Open the URL

Browser pops a login dialog. Use any username (e.g. `maryam`) and the `APP_PASSWORD` you set.

That's it.

---

## Optional: custom domain

In Render → Settings → Custom Domains → add `bd.denalihealth.com`.
Render gives you a CNAME target. Update your DNS at the domain registrar:

```
bd.denalihealth.com  CNAME  denali-bd-xxxx.onrender.com
```

Wait ~5 min for SSL to provision. Then update `ALLOWED_ORIGINS` to include the new domain.

---

## Backups

### Local dev (you, on your laptop)

Run the backup script manually or schedule it:

```
set BACKUP_DIR=C:\Users\44791\My Drive\denali_backups
python scripts\backup_to_drive.py
```

Schedule via Windows Task Scheduler to run every 30 minutes.

### Production (on Render)

Render's persistent disk is NOT automatically backed up. Set up a daily download:

1. In the Render dashboard → service → Shell tab → run:
   ```
   cat /data/denali.db | base64 > /tmp/dump.txt
   ```
2. Or add a cron job (Render → New + → Cron Job) that runs nightly and uploads `/data/denali.db` to S3 / Google Drive / Dropbox via their CLI.

For Phase 1 (single rep, low volume) a manual weekly download via Render's Shell tab is fine.

---

## Updating the deployed app

Every `git push origin main` auto-redeploys on Render (because `autoDeploy: true` in `render.yaml`).

Schema migrations are idempotent — `database.py` runs `ALTER TABLE` blocks on startup if columns are missing, so you don't have to do anything special when you add a column.

---

## Cost

- Render `starter` plan: **$7/month** (always-on, 512 MB RAM, no cold starts)
- 1 GB persistent disk: included in starter
- Render `free` plan exists but sleeps after 15 min — bad for follow-up automation. Avoid.

Total: **~$7/month** + ~$0.01 per draft generated (Anthropic Sonnet).

---

## Health checks

- `/health` → public, returns `{"status":"ok", ...}` (Render uses this)
- `/docs` → behind auth (Swagger UI)
- `/api/campaigns/dashboard` → behind auth, shows funnel counts
