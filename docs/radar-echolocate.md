# Radar and EchoLocate

How the admin finds accounts with a live competitive signal (the **Radar**), and
how it turns them into compliant, personalised outreach and pipeline
(**EchoLocate**). Code: `radar_model.py` (rules), `radar_service.py` (data),
`radar.py` (Radar API), `radar_collectors.py`, `radar_research.py`,
`radar_run.py` (daily job), `outbound.py`, `compliance.py`, `ops.py`.

## 1. Signals

A signal is public evidence that a company runs, or is struggling with, a
product Solix displaces. Every signal keeps a date and an evidence link.

| Type | Source | Notes |
|---|---|---|
| Competitor job | Greenhouse, Lever, Ashby, Workday boards (free); Crustdata (paid, capped) | A posting that names a competitor product, with the matching sentence |
| Problem job | Same | Archiving, retention, retirement or decommissioning work with no competitor named |
| DNS / certificate | Public DNS (MX, SPF) and crt.sh | Records that fingerprint Mimecast, Proofpoint, Smarsh, Global Relay, Archive360 and others |
| News | Google News RSS | For strong accounts only: acquisitions, migrations, decommissioning, fines |
| Filing | SEC EDGAR full-text search (free) | 10-K, 10-Q, 8-K filings that name a competitor product |
| Tender | EU TED (free) | Public tenders for archiving, retirement and records systems |
| LinkedIn | Logged by hand from an account | No lawful free feed exists |
| Website | Your own `/api/track` intent | Leads whose email domain matches the account |

Signals are stored once per `dedupe_key`; later sightings only refresh
`last_seen_at`. Staffing firms and consultancies are dropped.

## 2. Competitors and product lines

Each competitor maps to the Solix products that displace it, so a Radar
account gets a primary product line like an inbound lead:

| Competitor | Solix product |
|---|---|
| OpenText Documentum / Content Server | Enterprise Content Services |
| OpenText InfoArchive / Archive Center | Enterprise Archiving, Application Retirement |
| Informatica IDMC | Common Data Platform |
| Informatica Data Archive / ILM, IBM Optim | Database Archiving, Application Retirement |
| Smarsh, Global Relay, Mimecast, Proofpoint, Veritas Enterprise Vault | Email Archiving, Active Archiving for Compliance |
| Archive360 | Enterprise Archiving |
| Platform 3 Archon, JiVS | Application Retirement (SAP Archiving for JiVS) |
| Delphix | Enterprise Data Governance |

## 3. Strength, score and surge

- **Lead strength** comes from the posting's role: Strong for people who run the
  tool (IT, engineering, platform, data, developer, analyst), Medium for owners
  (manager, director, VP), Weak for administrative roles.
- **Account strength** is its strongest posting.
- **Score**: Strong 20, Medium 12, Weak 6 points per posting (16 for other
  signal types), each multiplied by a per-type weight and halved every 30 days
  (the same half-life as lead intent). Website intent and an approved deep dive
  add to it. A fresh Strong posting is about 20; two signals or a deep dive clear
  the outreach floor of 30.
- **Surge**: three or more new signals in seven days.

## 4. Routing

Named account first, then US state (California splits at ZIP 93600: north to
Hameed Hemmat, south to Brenda Goldstein), then country and region. The table
is editable in **Admin → Radar → Territories**; reps map to staff emails through
the Team list or the rep-email overrides. A person's owner choice is never
overwritten. Accounts the rules can't place are flagged "needs routing" and the
watchdog alerts on them.

## 5. Deep dives (Master Prospector)

Up to `research_per_run` accounts a day (Strong, two or more signal types, or
surging; not researched in 30 days) get a deep dive: numbered sources from their
own signals and fresh news, Solix facts from the site knowledge base, and a model
from `llm.py` (free tiers first). Without a model, a rules-based brief is drafted
from the signals. Every draft is checked:

- the first-touch email must not name a competitor (blocks approval);
- citations must point at real sources (blocks approval);
- pricing language and long emails are flagged.

Drafts wait in **Radar → Review** until a person approves them. Only an approved
deep dive scoring 6 or more marks an account **Ready for outreach**.

## 6. Account status

New → Researching → Ready → Contacted → Replied → Meeting booked → Opportunity →
Won, plus Lost, Not a fit and Excluded. The system only moves accounts forward.
A lead on the account reaching SQL, opportunity or won moves the account to
match, and also excludes it from automated outreach (the rep is working it).

## 7. EchoLocate outbound

1. **Contacts** come from deep dives, CSV import or the account drawer, with
   their source recorded (GDPR Art. 14). Email is checked for a mail server;
   with `REACHER_URL` set, the mailbox itself is verified.
2. **Gate** (`compliance.gate`), checked at enrollment, approval and every send:
   verified business email, name, title, lawful basis, not suppressed, not in
   Germany or Poland, account not excluded, account has a signal and a score of
   at least 30, a signed Legitimate Interest Assessment for the region, and not
   already in another active sequence.
3. **Personalisation**: a two-sentence opener grounded in the account's strongest
   signal or approved deep dive. It never names a competitor and never quotes
   prices, and it's rejected and replaced if it does.
4. **Approval** (on by default): enrollments wait in **Outbound → Approvals**.
5. **Sending**: within the daily cap and send window, through `dry_run` (records
   only), `smtp` (your existing transport, with a one-click List-Unsubscribe
   header, the postal address and an unsubscribe link) or `instantly` (the lead
   and every personalised step go to an Instantly campaign as custom variables).
6. **Replies** arrive from the Instantly webhook or are logged by hand, and are
   classified as interested, meeting, objection, referral, forward, out of
   office, not interested or unsubscribe:
   - interested or meeting: every other touch stops, the person becomes an MQL
     (meeting: SQL) lead in the existing funnel, and the owner gets an email.
     Slack and Digiprise are pushed;
   - objection: paused for 90 days;
   - out of office: paused until they're back;
   - referral or forward: new contacts are added;
   - not interested or unsubscribe: suppressed immediately.
7. **Guardrails**: a 7-day bounce rate over 3% pauses all sending. The watchdog
   alerts on a stalled Radar run (over 26 hours) or a stalled sender (over 45
   minutes while sending is on), on unassigned strong accounts, and on failed
   deliveries.
8. **A/B**: variants alternate until each has 50 first sends; then new
   enrollments get the variant with the higher positive-reply rate.
9. **Learning**: every Monday, with 20 or more outcomes, signal-type weights are
   re-learned from which accounts replied or booked meetings.

## 8. Compliance

- **Suppression**: unsubscribes, bounces, not-interested replies, rep DNCs and
  erasures. Erasures keep only a SHA-256 of the address.
- **Erasure** (Art. 17): one request deletes the person from contacts, leads,
  submissions, browsing, chats, trial accounts, registrations and outreach
  history, and queues deletion in Instantly and Digiprise.
- **LIA register**: sending to a region stays blocked until its LIA is signed by
  an admin or a user with the Compliance role.
- **Blocked countries**: Germany and Poland by default (UWG §7, PKE Art. 398).

## 9. Running it

- **Daily**: `.github/workflows/radar-daily.yml` at 05:00 UTC runs
  `python radar_run.py daily` against the same MongoDB. Set the repository
  secrets it lists. **Run now** in the admin does the same in the API process.
- **Continuous**: inside the API, the outbox delivers every minute, the sender
  runs every 10 minutes, and the watchdog every 30 minutes. `POST /api/ops/tick`
  with `X-Ops-Token` lets an external scheduler drive the same work.
- **Import** the original Leads Radar from **Radar → Import** (its `radar.html`
  or JSON). Owner, Status and Notes columns are kept.
- **Legacy**: `GET /api/admin/radar/legacy.json` returns the original page's
  JSON contract, and `export.xlsx` the workbook layout.
