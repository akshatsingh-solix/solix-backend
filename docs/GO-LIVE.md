# Going live: Radar and EchoLocate

Everything below is set from the Render dashboard, the GitHub repository
settings or the admin. No code changes are needed. Steps are in the order to do
them; each says who does it and whether it costs anything.

| | Free | Paid (optional) |
|---|---|---|
| Hosting, database, daily job | Render free, MongoDB Atlas free, GitHub Actions | |
| AI (openers, reply labels, deep dives) | Groq or Gemini free keys | |
| Alerts | Slack incoming webhooks | |
| Signals | Job boards, DNS, news, SEC, EU tenders | Crustdata job index (credits) |
| Sending | Google Workspace / Microsoft 365 mailbox on a secondary domain | Instantly (existing subscription), ~$10/yr for the domain |
| Mailbox verification | Mail-server check, "Mark verified", bounce auto-pause | Reacher on a VPS with port 25 open (~€4/month) |

## 1. Merge and deploy (admin, 10 minutes)

1. Merge **solix-backend PR #17** first: Render redeploys the API. Wait until
   `https://solix-backend-mf5w.onrender.com/api/` answers.
2. Merge **Website PR #38**: the *Deploy frontend* workflow republishes the admin
   to GitHub Pages in about 5 minutes.
3. Sign in to the admin: **Radar** and **Outbound** appear in the section bar.

## 2. Render environment (admin, 10 minutes)

Render → *solix-backend* → **Environment**. Generate random values with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

| Variable | Needed for | Value |
|---|---|---|
| `PUBLIC_API_URL` | unsubscribe links, Instantly webhook | `https://solix-backend-mf5w.onrender.com` (check it isn't the older `solix-backend.onrender.com`) |
| `UNSUBSCRIBE_SECRET` | signing unsubscribe links | random; set it so rotating `JWT_SECRET` never breaks old links |
| `OPS_TOKEN` | optional external scheduler (`POST /api/ops/tick`) | random |
| `GROQ_API_KEY` or `GEMINI_API_KEY` | openers, reply classification, deep dives | free key from console.groq.com or aistudio.google.com. Without one, openers come from the deep dive and replies are labelled by rules |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASSWORD` `EMAIL_FROM` (or `RESEND_API_KEY`) | owner alerts, digests, SMTP sending | your existing transport |
| `CRUSTDATA_API_KEY` | paid job index | only if you buy credits |
| `INSTANTLY_API_KEY`, `INSTANTLY_WEBHOOK_SECRET` | sending through Instantly | API v2 key (Instantly → Settings → Integrations → API keys) and a random secret |
| `REACHER_URL`, `REACHER_TOKEN` | mailbox verification | see step 7 |
| `DIGIPRISE_URL`, `DIGIPRISE_TOKEN` | Digiprise sync | from the Digiprise team (the URL can also be set in the admin) |
| `SLACK_WEBHOOK_REPLIES` / `_PIPELINE` / `_CONFLICTS` / `_WATCHDOG` | Slack alerts | or paste them in the admin (step 8) |

Save; Render restarts the API. The API runs its own loop while awake (deliveries
every minute, the sender every 10 minutes, checks every 30), and the Website
repo's *Keep backend warm* workflow keeps it awake.

## 3. Daily Radar job (admin, 10 minutes)

solix-backend on GitHub → **Settings → Secrets and variables → Actions**.

- **Secrets**: `MONGO_URL`, `DB_NAME` (same as Render), `GROQ_API_KEY` and/or
  `GEMINI_API_KEY` / `OPENROUTER_API_KEY`, `SMTP_HOST`, `SMTP_PORT`,
  `SMTP_USER`, `SMTP_PASSWORD` or `RESEND_API_KEY`, `EMAIL_FROM`, and
  `CRUSTDATA_API_KEY` if you have it.
- **Variable**: `SITE_URL` = `https://akshatsingh-solix.github.io/Website`
  (links in the digest emails).
- MongoDB Atlas → **Network Access** must allow `0.0.0.0/0`: GitHub runners
  have changing addresses.
- Test it: **Actions → Radar daily run → Run workflow** (steps `daily`). It then
  runs by itself at 05:00 UTC and emails each rep their digest.

## 4. People and roles (admin, 10 minutes)

Admin → **Settings**.

- **Team logins**: a login per rep (*Sales*), one for whoever signs the
  Legitimate Interest Assessments (*Compliance*), and *Leadership (read-only)*
  for viewers.
- **Sales team**: every rep's name and email, with names spelled exactly as in
  the Radar territory table (for example "Bo Deas"). This is how routed accounts
  turn into emails and digests. Reps who don't log in can be given emails in
  **Radar → Setup → Rep emails** instead.

## 5. Radar (sales ops, 20 minutes)

Admin → **Radar → Setup**.

1. **Import from the Leads Radar**: choose the original `radar.html` (or its
   JSON). Accounts, signals, deep dives, Owner, Status and Notes come across;
   importing again never duplicates.
2. **Territories**: check the named accounts, US states, region owners, country
   overrides and the California split (ZIP 93600) with sales leadership, then
   **Save**.
3. **Sources**: job boards (`Company, domain, ats, board`), domains to
   fingerprint, and the source switches. Leave *Crustdata* off unless the key is
   set. **Save sources**.
4. **Run → Collect, score, research** once to see it work, then let the daily
   job take over.
5. Approve or reject drafted deep dives in the **Review** view. An approved deep
   dive raises the account's score.

## 6. Compliance before any email (Compliance role or admin, 30 minutes)

Admin → **Outbound → Compliance**.

1. **Legitimate Interest Assessments**: write one for each region you will email
   (US, UK, EU): purpose, necessity, balancing test, safeguards. **Save LIA**,
   then **Sign**. Nobody in a region can be enrolled until its LIA is signed.
2. **Sending** panel:
   - Postal address (it goes in every email) and sender name.
   - Daily cap: start at 20–30 per warmed mailbox.
   - Send hours (UTC), and a minimum account score of 30.
   - Blocked countries: Germany and Poland (prior consent required).
   - Approval required: keep it on.
   - **Verified emails only**: keep it on if you run Reacher (step 7). Without
     it, use **Mark verified** on contacts you have confirmed, or switch this
     off. Sending pauses itself above 3% bounces either way.
3. Leave **Provider: Dry run** and click **Turn sending on**. For a week,
   approve enrollments and read the openers in **Approvals**. Nothing is emailed.

## 7. Mailbox verification (optional)

Reacher (free, MIT) checks the mailbox itself, but it needs outbound port 25,
which free hosts (Render, Fly, Railway, Oracle) block. Run it on a small VPS
where port 25 can be opened (Hetzner or OVH, about €4 a month), then set
`REACHER_URL` and `REACHER_TOKEN`. To stay free, skip it: every address is still
checked for a mail server, reps can **Mark verified**, and the bounce
auto-pause protects the domain.

## 8. Notifications (admin, 10 minutes)

Admin → **Outbound → Health → Integrations**.

- **Slack**: create incoming webhooks (Slack → Apps → *Incoming Webhooks*) for
  replies, pipeline, conflicts and watchdog channels, and paste them.
- **Digiprise**: the endpoint URL from the Digiprise team.

## 9. Real sending (admin)

Use a **secondary domain** (for example `trysolix.com`) with SPF, DKIM and DMARC,
warmed for 2–3 weeks. Never cold-email from `solix.com`. Resend, SendGrid and
similar services forbid cold outreach in their terms, so use one of these:

**Instantly (recommended, already licensed)**
1. In Instantly, create a campaign whose steps are only variables:
   subject `{{subject_1}}`, body `{{body_1}}`, then `{{subject_2}}` / `{{body_2}}`
   and so on, one step per sequence touch (up to 4). `{{opener}}`,
   `{{rep_name}}` and `{{unsubscribe_url}}` are available too. Attach the warmed
   sending accounts and set the schedule.
2. Admin → **Outbound → Compliance → Sending**: Provider **Instantly**, paste the
   campaign ID as the default (or per sequence under **Sequences**), **Save**.
3. **Outbound → Health → Connect Instantly webhook**: replies, bounces,
   meetings and unsubscribes then flow back by themselves.
4. Activate the campaign in Instantly.

**SMTP (a Workspace or 365 mailbox on the secondary domain)**
Set the `SMTP_*` variables to that mailbox, then choose Provider **SMTP / Resend**.
Emails carry a one-click unsubscribe header and link. Log replies in
**Outbound → Replies** (or forward them there), since there is no webhook.

## 10. Every day

- **Reps**: Radar → pick your name → open new accounts → add contacts from the
  deep dive → **Check gate** → **Enroll**.
- **Approver**: Outbound → **Approvals** → read, approve or reject.
- **Everyone**: replies stop the sequence and create the lead (meeting = SQL).
  Watch **Overview** (bounce under 3%, reply rate, the Day-30 checklist) and
  **Health** (all green).

## Decisions to make

- Make **solix-backend private**: its Action logs are public (the workflow only
  prints counts). The daily job fits well inside the private-repo free minutes.
- Retire the old `Website/backend/` copy of the API (the live API is
  solix-backend). First point `frontend/scripts/sol-knowledge.js` at
  solix-backend's `sol_knowledge.json`: it still writes the copy in
  `Website/backend/`, and the deploy workflow checks it.
- Have sales leadership confirm the territory table.
- Export from HubSpot before retiring it.
