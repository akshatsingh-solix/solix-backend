# Going live: Radar free data sources

Step by step, in order. Time: about 90 minutes, most of it signing up for free keys. Cost: nothing
(one provider, Brave, asks for a card to give its free $5 a month; skip it if you prefer).

Who: someone with admin rights on the two GitHub repositories, the Render dashboard and the Solix
admin. Background on every provider: [`radar-free-sources.md`](radar-free-sources.md). The wider
launch checklist (email, compliance, Instantly): [`GO-LIVE.md`](GO-LIVE.md).

---

> Company policy: **Exa and Adzuna are not used** and have been removed from the Radar. Don't sign
> up for them or add their keys.

## Step 1. Merge the two pull requests (10 minutes)

Order matters: the admin screen calls the new API, so the backend goes first.

1. **solix-backend** pull request *Radar: free data sources with failover...* → review → **Merge**.
2. Render deploys it by itself if auto-deploy is on (Render → *solix-backend* → **Settings → Build &
   Deploy → Auto-Deploy: On Commit**). If it is off: **Manual Deploy → Deploy latest commit**.
   Wait for **Live** in the Events list (3 to 5 minutes).
3. Check it answers: open `https://solix-backend-mf5w.onrender.com/api/` → `{"service":"solix-api","status":"ok"}`.
4. **Website** pull request *Admin Radar: Data sources screen...* → **Merge**. The *Deploy frontend
   to GitHub Pages* workflow runs by itself (GitHub → Website → **Actions**); wait for the green tick
   (4 to 6 minutes).
5. Sign in to the admin → **Radar → Setup**. A **Data sources** panel is now there. Before any key is
   added, the keyless sources (public registers, RSS, DNS) already show as ready.

Nothing else changes for users at this point: until keys are added, the Radar works as before plus
the free keyless sources.

## Step 2. Sign up for the free keys (45 to 60 minutes)

Open a private note to collect the keys as you go (never paste them into GitHub issues, pull requests
or chat). Each line is: provider → where to sign up → the variable name the key goes into.

### Tier 1: do these first (biggest gain, no card needed)

| # | Provider | Sign up | Copy into |
|---|---|---|---|
| 1 | Tavily (web search, 1,000/month) | tavily.com → Sign up → API key on the dashboard | `TAVILY_API_KEY` |
| 2 | Serper (Google results, 2,500 once) | serper.dev → Sign up → API key | `SERPER_API_KEY` |
| 3 | TheirStack (job postings, 200/month) | theirstack.com → Sign up → Settings → API key | `THEIRSTACK_API_KEY` |
| 4 | JSearch (Google for Jobs, LinkedIn, Indeed jobs) | rapidapi.com → search *JSearch* → **Subscribe to Basic (free)** → copy *X-RapidAPI-Key* | `RAPIDAPI_KEY` |
| 5 | Jooble (jobs) | jooble.org/api/about → request a key (arrives by email) | `JOOBLE_API_KEY` |
| 6 | Tomba (people + emails, 25/month) | tomba.io → Sign up → API → copy the key **and** the secret | `TOMBA_KEY`, `TOMBA_SECRET` |
| 7 | MailboxValidator (API credits, can expire) | mailboxvalidator.com → Free plan → API key. Note how many API credits you got and their expiry date: you enter both in Step 4 | `MAILBOXVALIDATOR_API_KEY` |
| 8 | IPinfo Lite (website visitor companies, unlimited) | ipinfo.io → Sign up → Token | `IPINFO_TOKEN` |
| 9 | UK Companies House (UK size, HQ, directors) | developer.company-information.service.gov.uk → Register → *Create an application* → *Create new key* (type **REST**) | `COMPANIES_HOUSE_API_KEY` |
| 10 | Google Gemini (AI for Sol, the website chat) | aistudio.google.com → **Get API key** | `GEMINI_API_KEY` (see Step 6 before using it for prospect data) |

If Firecrawl, Parallel and Hunter keys are already on Render, keep them: they stay first in their
chains and the new ones take over when they run out.

### Tier 2: more capacity (do when you have 30 more minutes)

| Provider | Sign up | Copy into |
|---|---|---|
| Brave Search ($5 credit/month, card required) | brave.com/search/api → Subscribe to the free/"Search" plan → API key | `BRAVE_API_KEY` |
| USAJOBS (US federal jobs) | developer.usajobs.gov → Request API key | `USAJOBS_API_KEY` and the email you registered with in `USAJOBS_EMAIL` |
| Reed (UK jobs) | reed.co.uk/developers → Jobseeker API → key | `REED_API_KEY` |
| SAM.gov (US federal tenders) | sam.gov → sign in → profile → **Account Details** → request public API key | `SAM_GOV_API_KEY` |
| People Data Labs (100 companies + 100 people/month) | peopledatalabs.com → Sign up → API key | `PDL_API_KEY` |
| Snov.io (50 credits/month) | snov.io → Account settings → API → *User ID* and *Secret* | `SNOV_CLIENT_ID`, `SNOV_CLIENT_SECRET` |
| ZeroBounce (100 checks/month, sign up with a business email) | zerobounce.net → API → key | `ZEROBOUNCE_API_KEY` |
| Abstract Email Validation (100/month) | abstractapi.com → Email Validation → key | `ABSTRACT_EMAIL_API_KEY` |
| Prospeo (75 emails/month) | prospeo.io → API → key | `PROSPEO_API_KEY` |
| ipapi.is (1,000 visitor lookups/day) | ipapi.is → Sign up → key | `IPAPI_IS_KEY` |
| Ipregistry (100,000 lookups once) | ipregistry.co → Sign up → key | `IPREGISTRY_KEY` |
| NewsData.io (200/day) | newsdata.io → Sign up → key | `NEWSDATA_API_KEY` |
| urlscan.io (subdomains) | urlscan.io → Sign up → Settings → API key | `URLSCAN_API_KEY` |

### Tier 3: optional

SerpApi (`SERPAPI_API_KEY`, 250/month), Jina (`JINA_API_KEY`), Apollo (`APOLLO_API_KEY`, needs an
Apollo plan with API access), Skrapp (`SKRAPP_API_KEY`), an existing Google Programmable Search key
(`GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX`; closed to new sign-ups), a self-hosted SearXNG (`SEARXNG_URL`,
with `json` enabled under `search.formats` in its settings), AlienVault OTX (`OTX_API_KEY`), Cert
Spotter (`CERTSPOTTER_API_KEY`).

More free AI models (only used for prospect data if you allow them in Step 6): Cerebras
(`CEREBRAS_API_KEY`), SambaNova (`SAMBANOVA_API_KEY`), GitHub Models (`GITHUB_MODELS_TOKEN`, a GitHub
token with the *models* permission), NVIDIA (`NVIDIA_API_KEY`), Cloudflare Workers AI
(`CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`), Hugging Face (`HF_TOKEN`).

## Step 3. Put the keys on Render (10 minutes)

1. Render → *solix-backend* → **Environment**.
2. Either **Add Environment Variable** once per key, or **Add from .env** and paste lines like
   `TAVILY_API_KEY=tvly-...` (one per line, no quotes, no spaces around `=`).
3. **Save changes**. Render restarts the API (1 to 3 minutes).

Keys live only on Render. They are never written to either repository.

## Step 4. Check every key works (10 minutes)

1. Admin → **Radar → Setup → Data sources** → **Refresh**.
2. Each function shows **N of M ready**. Aim for at least 2 ready in *Web search*, *Job postings*,
   *People at the account* and *Email verification*.
3. Click **Test** on every provider you added. Green text = working. What a red result means:

| Test says | Fix |
|---|---|
| `returned 401` or `key rejected` | The key was pasted wrongly (a missing character or a space). Paste it again on Render. |
| `returned 403` | The account has no API access on its plan (JSearch: subscribe to the free Basic plan on RapidAPI; Apollo: needs a paid plan). |
| `returned 402` / `out of credits` | That free allowance is already used. It rests and the next provider answers; nothing to do. |
| `Not set up: VARIABLE` | The variable name on Render is spelled differently. Copy it from this page exactly. |
| `rate limited` | Normal on a burst of tests. Wait a minute and test again. |

4. **Time-limited and one-time credits.** For any provider whose free credits are a one-off grant
   or expire (for example MailboxValidator's API credits, valid for 6 months, or Serper's 2,500
   sign-up queries): click **Allowance** on its row → *One-time credits* → enter the number of
   credits left → set **Expires on** → **Save**. The Radar then spreads those credits over the days
   to the expiry date, skips the provider once they are used or expired, and warns you in
   Outbound → Health two weeks before they expire. When you renew or top up, enter the new grant the
   same way (it starts a fresh count). For a monthly or daily allowance that differs from the one
   shown, pick *Every month* or *Every day* instead.
5. Admin → **Outbound → Health**: the checks **Radar data sources** and **Radar one-time credits**
   should be green.

## Step 5. Radar settings (5 minutes)

Admin → **Radar → Setup → Sources**:

1. Tick the new switches (on by default): *Job market (free job APIs)*, *ATS X-ray (find new boards)*,
   *Subdomains + passive DNS*, *US/UK tenders + public spend*, *Website visitor companies*.
2. **Min. employees (job sources)**: set **200** if it still shows 500 (an earlier save keeps the old
   value). This is what lets mid-market companies through.
3. **Mid-market from** 200 and **Enterprise from** 1,000 employees (change if sales defines them
   differently). **Deep dives first for**: Enterprise and Mid-market.
4. Optional: **Extra job-search phrases** (for example `records retention, SAP decommissioning`).
5. **Save sources**.

## Step 6. Decide how deep dives use AI (5 minutes)

Deep dives, outreach openers and reply labels contain prospect names, so they only go to the AI
providers named in `LLM_PII_PROVIDERS` on Render (default `openai`). Pick one:

- **Have an OpenAI key**: nothing to do.
- **Stay free**: read the data terms of a free provider you have a key for (Groq, Cerebras...),
  and if they are acceptable to your legal/compliance owner, set for example
  `LLM_PII_PROVIDERS=groq,cerebras` on Render.
- **Gemini**: on a free key, Google's terms say not to send personal information (free use may be used
  to improve Google's products). So keep `gemini` out of `LLM_PII_PROVIDERS` unless you enable billing
  on the key's Google Cloud project (Google Cloud console → Billing → link a billing account). Use then
  becomes a paid service under Google's data processing terms, and `LLM_PII_PROVIDERS=gemini` is
  reasonable to propose to your compliance owner. Either way the key works for Sol straight away.
- **Free and private**: run a model on your own server (for example Ollama), set `SOL_CUSTOM_BASE_URL`,
  `SOL_CUSTOM_MODELS`, and `LLM_PII_PROVIDERS=custom`.
- **Do nothing**: deep dives are drafted from the signals by rules (useful, but shorter).

## Step 7. Website visitor companies: privacy wording (10 minutes, with whoever owns the privacy notice)

Only visitors who accept analytics cookies are tracked, their network's organisation is looked up,
home and mobile connections are ignored, and the address itself is never stored. Still, add a
sentence to the cookie banner or privacy notice, for example: *"If you accept analytics cookies, we
use your IP address to identify the organisation whose network you are browsing from. We store the
organisation, not the address."* If you would rather not, untick *Website visitor companies* in
Step 5.

## Step 7b. Companies researching competitors (optional, 15 minutes)

Seeing who visits a competitor's website is only possible for that competitor; the lawful sources
are intent providers. If you have (or buy) one, export its account list as CSV and import it:
Admin → **Radar → Setup → Companies researching competitors** → set **Source** (e.g. "G2 Buyer
Intent" or "Bombora") → **Import CSV export**. The accounts appear on the **Researching** tab, tied to
the competitor they researched. Repeat weekly; re-importing never duplicates. Details and providers:
[`radar-free-sources.md`](radar-free-sources.md#companies-researching-competitors).

Free and built in: organisations visiting *your* site who read or search for a competitor appear on
the **Website intent** tab with that competitor. To get more of them, publish comparison and
migration pages ("Solix vs OpenText InfoArchive", "Migrating off Documentum") with the competitor's
name in the page address.

## Step 8. First run (5 minutes to start, 10 to 30 minutes to finish)

1. Keep the API awake while it runs: the cron-job.org tick from [`GO-LIVE.md`](GO-LIVE.md) step 3b
   should already be calling `/api/ops/tick` every 5 minutes.
2. Admin → **Radar → Setup → Run → Collect, size, score, research**.
3. When it finishes (the **Run log** sheet of the Radar's **Excel** export, or Outbound → Health, shows
   the latest `radar_daily` run), check:
   - the size filter (**All sizes** → *Mid-market and enterprise*) and the Enterprise / Mid-market badges;
   - the **Website**, **Tenders** and **Jobs** tabs (new visit, public-spend and job-market signals);
   - **Setup → Sources → Job boards**: boards found by the ATS X-ray have been added;
   - open a strong account → **Find people**: people marked *works there now*, with where each was found.
4. From now on it runs by itself every day at 05:00 UTC (`RADAR_DAILY_UTC`), inside the API.

## Step 9. Optional: the manual GitHub run

Only if you use **Actions → Radar daily run** by hand: add the same keys as repository secrets
(solix-backend → **Settings → Secrets and variables → Actions → New repository secret**). To see which
keys the runner has, run the workflow with steps `sources`: it lists variable names that are set or
missing, never their values.

## Every week (5 minutes)

- **Radar → Setup → Data sources**: any function at **0 ready** or amber? Add another free key for it.
  Allowances reset on the 1st of each month.
- **Outbound → Health**: *Radar data sources* is green. If it turns amber, Slack #watchdog gets the same
  message (when the Slack webhook is set).
- Approve or reject drafted deep dives in **Radar → Review**.

## If something is wrong

| Symptom | Cause and fix |
|---|---|
| No **Data sources** panel | The Website pull request isn't deployed yet (Step 1.4), or the browser has the old page cached: hard-refresh. |
| Data sources shows an error | The backend isn't deployed yet (Step 1.2). |
| A function stays at 0 ready | No key set for it, or every key is resting. Add a key from Step 2. |
| No new job-market accounts | Job sources skip companies under *Min. employees*; check Step 5.2. The first day only uses a share of each monthly allowance by design. |
| No website visit signals | Visitors must accept analytics cookies; only company networks count (not home or mobile); signals appear after the next daily run. |
| Deep dives say "Drafted without an AI model" | Step 6. |
| Need to stop a provider at once | Data sources → untick **On**, or set `RADAR_PROVIDERS_DISABLED=name` on Render. |
