# Moving solix-backend off Render (free tier)

## Why Render keeps restarting the API

Render's free instance has **512 MB RAM**. This service is heavy for that size:

- the Radar daily run, Radar scoring, the outbox/sender loop, PDF (reportlab) and Excel exports and the site
  migration jobs all run **inside the one API process** (`ops.py`, `radar_run.py`, `migrate.py`);
- the Mongo driver pool defaults to 100 connections;
- the "exceeded its memory limit" email arrived at 10:31 IST, one minute after `RADAR_DAILY_UTC=05:00`
  (05:00 UTC = 10:30 IST). That is the likely trigger. Confirm in Render > Metrics > Memory and Logs around that time.

A bigger host fixes the cause. A different 512 MB host would repeat it.

## What stays, what moves

| Part | Today | Plan |
|---|---|---|
| API (FastAPI) | Render free, 512 MB, sleeps | **Oracle Cloud Always Free ARM VM** (up to 4 CPU / 24 GB, always on) |
| Database | MongoDB Atlas M0 (512 MB) | Keep for now. Optional later: Mongo on the VM (200 GB) with nightly backups |
| Website | GitHub Pages | Keep (free, static, fast) |
| Daily jobs | in-process loop | Keep (VM is always on, so nothing is missed) |
| HTTPS | Render | Caddy in Docker, automatic Let's Encrypt |
| Uptime alerts | none | UptimeRobot or Better Stack free |

Fallback if Oracle will not give you a VM (see step 1): Google Cloud Run + Cloud Scheduler, section B at the end.

Free-tier facts below were checked on 2026-11-18 against Oracle's and Google's docs. Re-check the pricing pages before you rely on them.

---

## A. Oracle Always Free VM (recommended)

Oracle gives 200 GB block storage, and "idle Always Free compute instances may be reclaimed". This API pings itself and runs loops, so it is not idle, but still keep backups (step 9).

### 1. Create the VM (about 20 min)

1. Sign up at oracle.com/cloud/free. A card is needed for identity checks and is not charged on Always Free.
   Choose the **home region closest to your users** (Mumbai or Hyderabad for India). It cannot be changed later.
2. Compute > Instances > Create instance.
   - Image: **Ubuntu 22.04 or 24.04** (aarch64).
   - Shape: Ampere **VM.Standard.A1.Flex**, 2 OCPU / 12 GB RAM is plenty. You can go to 4 / 24.
   - Boot volume: 50 GB (default).
   - Add your SSH public key (or let Oracle generate one and download it).
3. If you get "Out of host capacity", retry later or in another availability domain. This is common.
4. Note the **public IP**.
5. Networking: VCN > Security List > add **Ingress** rules for TCP **80** and **443** from `0.0.0.0/0`. Leave 22 as is.

### 2. Get a free hostname

Pick one:
- **DuckDNS** (duckdns.org): create `solix-api.duckdns.org`, point it at the VM's public IP.
- Or a domain you own: add an `A` record `api.yourdomain.com` to the VM IP (Cloudflare DNS, proxy off).

### 3. Prepare the server

```bash
ssh ubuntu@<VM_PUBLIC_IP>
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker ubuntu && newgrp docker
# Ubuntu images on Oracle block ports with iptables even after the security list is open:
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo apt-get install -y iptables-persistent && sudo netfilter-persistent save
```

### 4. Get the code and secrets onto the VM

```bash
git clone https://github.com/akshatsingh-solix/solix-backend.git
cd solix-backend
cp .env.example .env
nano .env
```

Fill `.env` from the Render dashboard (Environment tab). Copy every value that has one, especially:
`MONGO_URL`, `DB_NAME`, `JWT_SECRET`, `UNSUBSCRIBE_SECRET`, `OPS_TOKEN`, `INSTANTLY_WEBHOOK_SECRET`,
`ADMIN_EMAIL`, `ADMIN_PASSWORD`, all provider keys.

**Copy the generated secrets exactly** (`JWT_SECRET`, `UNSUBSCRIBE_SECRET`, `INSTANTLY_WEBHOOK_SECRET`, `OPS_TOKEN`).
New values would log everyone out and break unsubscribe links already sent in emails.

Then set these for the new host:

```
API_DOMAIN=solix-api.duckdns.org
PUBLIC_API_URL=https://solix-api.duckdns.org
KEEP_AWAKE=0
```

Keep the file private: `chmod 600 .env`. Never commit it.

### 5. Let the new host reach Mongo Atlas

Atlas > Network Access > add the VM's public IP. (While testing, `0.0.0.0/0` works, then tighten it.)

### 6. Start it

```bash
docker compose up -d --build
docker compose logs -f api
```

Wait for "Application startup complete", then from your laptop:

```bash
curl https://solix-api.duckdns.org/api/
```

The first HTTPS request can take about 30 seconds while Caddy gets the certificate.

### 7. Test before switching anything

The old Render service keeps running during all of this, so nothing breaks yet.

- Open `https://<new-host>/api/` and `/docs` if enabled.
- Log in to the admin using the new base URL.
- Submit a test lead from a dev build of the site pointed at the new URL. Check the lead and email alert arrive.
- Ask Sol a question in the chat widget.
- Watch memory: `docker stats` (run Radar once from Admin > Radar and watch it stay well under 2 GB).

Both servers share one database, so do not run two copies of the Radar at the same time. If you test the Radar,
do it near the cutover, then stop Render.

### 8. Cut over (10 min, low traffic hour)

1. Website repo > Settings > Secrets and variables > Actions > **Variables**: set `BACKEND_URL` to
   `https://solix-api.duckdns.org`. Re-run the Pages deploy workflow.
2. `Website/empower/src/lib/api.js` still has the Render URL as a fallback. Change it to the new URL too.
3. Update every external caller of the old address:
   - Instantly webhook URL (Admin > Outbound), it must use `PUBLIC_API_URL`.
   - Any external cron (cron-job.org etc.) that calls `/api/ops/tick`.
   - GitHub Actions secrets for the manual Radar workflow (`MONGO_URL`, `DB_NAME`), only if you use it.
4. Check the live site: forms, chat, downloads, unsubscribe link.
5. In Render, **suspend** the service (do not delete yet). Keep it a week as a rollback.
   To roll back: set `BACKEND_URL` back and resume Render.
6. Update `render.yaml`, `README.md` ("Deploy (Render, free tier)") and `docs/GO-LIVE.md` to describe the new host,
   and delete `render.yaml` once Render is gone.

### 9. Backups and monitoring (do not skip)

Atlas M0 has no backups. Add a nightly dump to free object storage (OCI Object Storage 20 GB free, or Cloudflare R2 10 GB free):

```bash
# /home/ubuntu/backup.sh  (chmod +x), then: crontab -e  ->  15 2 * * * /home/ubuntu/backup.sh
docker run --rm -v /home/ubuntu/backups:/out mongo:7 \
  mongodump --uri "$MONGO_URL" --db solix --gzip --archive=/out/solix-$(date +%F).gz
find /home/ubuntu/backups -mtime +14 -delete
# then rclone/oci-cli copy /home/ubuntu/backups to the bucket
```

Uptime: UptimeRobot free, HTTPS check on `/api/` every 5 minutes, alerts to your email.
Errors: Sentry free tier (5k events/month) if you want stack traces.

### 10. Updating the app later

```bash
cd ~/solix-backend && git pull && docker compose up -d --build
```

Optional: a GitHub Actions workflow that runs that command over SSH on every push to main (store the SSH key as a repo secret).

---

## B. Fallback: Google Cloud Run

Free per month: 2M requests, 180,000 vCPU-seconds, 360,000 GiB-seconds. Needs a billing account (card) on file.
The existing `Dockerfile` works unchanged.

Catch: Cloud Run scales to zero, and this app's background loops only run while an instance is alive. So:

1. `gcloud run deploy solix-backend --source . --region asia-south1 --memory 1Gi --cpu 1 --allow-unauthenticated`
   and set the env vars (use Secret Manager for the keys).
2. Use **Cloud Scheduler** (3 free jobs) to call `POST /api/ops/tick` every 5 minutes with `OPS_TOKEN`
   (the app already supports this, see `docs/GO-LIVE.md`), plus one job at 05:00 UTC for the Radar.
3. `KEEP_AWAKE=0`.

Cold starts are a few seconds, much better than Render's up to 60 s. Setting `--min-instances 1` makes it always on but is billed.

## Not recommended

- **Fly.io, Railway**: no real free tier any more.
- **Cloudflare Workers**: cannot run FastAPI + Motor.
- **Koyeb / Hugging Face Spaces free**: also sleep, and Spaces are meant for demos.

## Optional clean-ups that cut memory on any host

`requirements.txt` ships dev tools (`black`, `mypy`, `flake8`, `pytest`, `pytest-xdist`, `isort`) and `pandas`/`numpy`
which no app module imports (checked with grep). Moving them to a `requirements-dev.txt` makes builds faster and the
image smaller. Setting `MONGO_MAX_POOL_SIZE=20` lowers connection memory. Neither is required for the move.
