# Moving solix-backend to Oracle Cloud (API + MongoDB)

## Why

Render's free instance has **512 MB RAM** and this API runs everything in one process: the Radar daily run, scoring,
the outbox/sender loop, PDF and Excel exports, site migration jobs. The "exceeded its memory limit" email came at
10:31 IST, one minute after `RADAR_DAILY_UTC=05:00` (10:30 IST), so the Radar run is the likely trigger.
MongoDB Atlas M0 caps storage at 512 MB and has no backups.

## Target

| Part | Before | After |
|---|---|---|
| API | Render free, 512 MB, sleeps | Docker on an **Oracle Always Free ARM VM** (2-4 CPU, 12-24 GB), always on |
| Database | MongoDB Atlas M0, 512 MB | **MongoDB 8 in Docker on the same VM**, 2 GB+ cache, ~150 GB of disk available |
| HTTPS | Render | Caddy, automatic Let's Encrypt |
| Backups | none | nightly `mongodump`, 14 days on the VM + copy to Oracle Object Storage (20 GB free) |
| Website | GitHub Pages | unchanged |

Everything runs from `docker-compose.yml`. The database has no public port: only the API container can reach it.

**What you take on by self-hosting the database:** backups, disk space and updates are now yours. Steps 9 and 10
cover them. Do not skip the off-VM backup copy. Oracle can reclaim Always Free instances it considers idle, and a
backup kept only on the VM's disk would go with it.

The app was checked for Atlas-only features (Atlas Search, vector search, change streams, transactions). It uses none,
so plain MongoDB works with no code changes.

---

## Phase 1: Build the server (about 30 min, nothing changes for users)

### 1. Create the VM

1. oracle.com/cloud/free. Pick the **home region** closest to your users (Mumbai or Hyderabad). It cannot be changed.
2. Compute > Instances > Create instance:
   - Image **Ubuntu 24.04** (aarch64)
   - Shape **VM.Standard.A1.Flex**, **2 OCPU / 12 GB** (you can use up to 4 / 24 in total)
   - Boot volume **100 GB** (of the 200 GB free; leaves room for a second VM or volume later)
   - Add your SSH public key, or download the one Oracle generates
3. "Out of host capacity" is common. Try another availability domain or try again later.
4. Note the **public IP**. Then Networking > your VCN > Security List > add Ingress TCP **80** and **443** from
   `0.0.0.0/0`. Do **not** open 27017.

### 2. Get a hostname

DuckDNS (free): create `solix-api.duckdns.org` pointing at the VM IP. Or, on a domain you own, an `A` record
`api.yourdomain.com` (Cloudflare DNS with the proxy off).

### 3. Prepare the server

```bash
ssh ubuntu@<VM_PUBLIC_IP>
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git rclone
sudo usermod -aG docker ubuntu && newgrp docker
# Oracle's Ubuntu images also block ports with iptables:
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo apt-get install -y iptables-persistent && sudo netfilter-persistent save
```

### 4. Code and settings

```bash
git clone -b claude/amazing-albattani-euy7ja https://github.com/akshatsingh-solix/solix-backend.git
cd solix-backend
mkdir -p backups            # create it as your user, so the backup script can rotate files
cp .env.example .env && chmod 600 .env
openssl rand -hex 24        # use the output as MONGO_ROOT_PASSWORD (hex: no characters that break the URL)
nano .env
```

In `.env`:

1. Copy every value from Render > solix-backend > Environment. **Copy these exactly**, because new values would log
   everyone out and break unsubscribe links already sent: `JWT_SECRET`, `UNSUBSCRIBE_SECRET`,
   `INSTANTLY_WEBHOOK_SECRET`, `OPS_TOKEN`.
2. Add:
   ```
   MONGO_ROOT_USER=solix
   MONGO_ROOT_PASSWORD=<the hex value>
   DB_NAME=solix
   API_DOMAIN=solix-api.duckdns.org
   PUBLIC_API_URL=https://solix-api.duckdns.org
   KEEP_AWAKE=0
   ```
3. Leave `MONGO_URL` as anything: `docker-compose.yml` points the API at the local database and overrides it.
   Keep the Atlas string somewhere safe; step 6 needs it once.

### 5. Start the database only

```bash
docker compose up -d mongo
docker compose ps           # mongo should become "healthy" within ~30 s
```

### 6. Let the VM read Atlas, then copy the data

Atlas > Network Access > Add IP Address > the VM's public IP.

```bash
ATLAS_URL='mongodb+srv://USER:PASS@cluster0.xxxxx.mongodb.net' scripts/copy_from_atlas.sh
```

It dumps Atlas into `backups/atlas-<date>.gz`, restores it locally, then compares document counts per collection.
"OK: every collection has the same count" means the copy is complete. Small differences are expected while Render
is still taking writes. The final copy in step 8 fixes that.

### 7. Start the API and test it

```bash
docker compose up -d --build
docker compose logs -f api            # wait for "Application startup complete"
curl https://solix-api.duckdns.org/api/   # from your laptop; first call may take ~30 s for the certificate
```

Test against the new URL: admin login, leads list, a Sol chat question, a PDF or Excel export. Then run
`docker stats` and check memory.

The API starts its background loops here too (sender, outbox, Radar at 05:00 UTC). It writes only to the local
copy, which step 8 replaces. Two things to avoid while both servers run:

- **Real outbound email.** Keep `OUTBOUND_LIVE_SENDING` empty on the VM until cutover, or the same email could leave twice.
- **Running the Radar by hand** on the VM before cutover.

## Phase 2: Cutover (about 15 min of downtime, pick a quiet hour)

### 8. Freeze, final copy, switch

1. **Render > solix-backend > Suspend.** Atlas now stops receiving writes, so nothing is lost after the copy.
2. On the VM, run the final copy and restart the API on the fresh data:
   ```bash
   ATLAS_URL='mongodb+srv://...' scripts/copy_from_atlas.sh   # must end with "OK"
   docker compose restart api
   ```
3. Set `OUTBOUND_LIVE_SENDING` in `.env` to whatever it was on Render, then `docker compose up -d`.
4. Point everything at the new address:
   - Website repo > Settings > Secrets and variables > Actions > **Variables**: `BACKEND_URL=https://solix-api.duckdns.org`,
     then re-run the Pages deploy workflow.
   - `Website/empower/src/lib/api.js`: the hard-coded Render fallback URL.
   - Instantly webhook URL (Admin > Outbound).
   - Any external cron calling `/api/ops/tick` (not needed any more on an always-on VM: delete it).
5. Check the live site: form submission and lead alert, chat, gated download email, unsubscribe link.

**Rollback** (if something is wrong in the first days): resume Render and set `BACKEND_URL` back. Data written to the
VM since cutover would need copying back with `mongodump`/`mongorestore` (the same commands in reverse).

### 9. Backups (same day)

```bash
crontab -e
# add:
15 2 * * * /home/ubuntu/solix-backend/scripts/backup.sh >> /home/ubuntu/backup.log 2>&1
```

Off-VM copy (strongly recommended): Oracle Console > Storage > Buckets > create `solix-backups`. Identity > your user
> Customer Secret Keys > generate one. Then `rclone config` to add an **S3 / Oracle Cloud** remote named
`ocibucket` with endpoint `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`, and add to `.env`:

```
BACKUP_REMOTE=ocibucket:solix-backups
```

Run `scripts/backup.sh` once by hand and check the file appears in the bucket.
**Test a restore once:** `docker compose exec -T mongo mongorestore --uri "<local uri>" --gzip --archive=/backups/<file> --nsFrom='solix.*' --nsTo='restoretest.*'`.

### 10. Monitoring

- UptimeRobot (free): HTTPS check on `https://solix-api.duckdns.org/api/` every 5 minutes, email alerts.
- Disk: `df -h` now and then. MongoDB data lives in a Docker volume on the boot disk.

## Phase 3: Clean up (after a week of no problems)

1. Delete the Render service.
2. Atlas: keep the cluster paused or delete it, after one last `mongodump` kept in the bucket.
3. GitHub Actions secrets `MONGO_URL` / `DB_NAME` for the manual Radar workflow point at Atlas. Remove them. The
   workflow cannot reach the VM's database (no public port), and the API already runs the Radar daily.
4. Update `README.md` ("Deploy (Render, free tier)"), `docs/GO-LIVE.md`, and delete `render.yaml`.

## Day-to-day

```bash
cd ~/solix-backend && git pull && docker compose up -d --build   # deploy a new version
docker compose logs -f api                                        # logs
docker compose exec mongo mongosh -u solix -p                     # database shell
```

### 11. Browse the database from your laptop (Compass)

```bash
ssh -L 27017:localhost:27017 ubuntu@<VM_IP> \
  "docker run --rm -p 127.0.0.1:27017:27017 --network solix-backend_default alpine/socat TCP-LISTEN:27017,fork TCP:mongo:27017"
```

Then connect Compass to `mongodb://solix:<password>@localhost:27017/?authSource=admin`.
