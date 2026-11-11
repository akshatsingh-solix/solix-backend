# Radar data sources: free providers with automatic failover

Every Radar function now has a **chain** of providers, most of them free. When one is rate
limited, out of credits, rejects its key or is down, the next one answers, and the Radar keeps
working. Code: `providers.py` (failover, cooldowns, quotas), `radar_sources.py` (one adapter per
provider), `radar_visitors.py` (website visitors), `radar_run.py` (the daily steps).

**Admin → Radar → Setup → Data sources** shows every chain: which providers are set up, how much of
each free allowance is left this month, why one is resting, and a **Test** button per provider. You
can reorder a chain, switch a provider off, or set its allowance there (stored under the settings
key `providers`).

## How the limits stop running out

1. **Failover.** A call walks its chain in order and takes the first useful answer.
2. **Cooldowns.** A provider that fails rests, so it isn't hammered:
   rate limited (429) 75 seconds, 15 minutes after three in ten minutes; out of credits (402, or a
   200 that says so) 6 hours; key rejected (401/403) 1 hour; server error 2 minutes; unreachable
   1 minute. A new key starts with no cooldown.
3. **Allowances.** Each provider has its free monthly or daily allowance (editable). It is skipped
   once that is used, and usage is counted in MongoDB (`provider_usage`), so restarts and the
   GitHub runner share the count.
4. **Pacing.** The daily run spends at most the day's share of each monthly allowance
   (what's left divided by the days left in the month), so day one can't drain the month. A person
   clicking *Find people* or *Find email* is only capped, never paced.
5. **Caching.** Identical web searches are reused for a day (LinkedIn X-ray searches for a week,
   ATS discovery searches for three days); website-visitor lookups for 30 days; company size and HQ
   for 90 days.
6. **Keyless sources first where they're good enough.** Public registries (Wikidata, GLEIF, SEC,
   RDAP), RSS feeds, certificate logs and DNS-over-HTTPS cost nothing and need no sign-up.

Total capacity is the sum of every free allowance you sign up for. Signing up for all the free
web-search keys below gives roughly 5,000+ searches a month instead of Firecrawl's alone.

## The chains

Order is the default; change it in Data sources. "Keyless" needs nothing.

### 1. Web search (deep-dive sources, leadership pages, LinkedIn X-ray, ATS discovery)

| Provider | Free allowance | Environment variable(s) | Sign up |
|---|---|---|---|
| Firecrawl | Monthly free credits (2 per search) | `FIRECRAWL_API_KEY` | firecrawl.dev |
| Tavily | 1,000 credits a month | `TAVILY_API_KEY` | tavily.com |
| Serper (Google results) | 2,500 queries on sign-up | `SERPER_API_KEY` | serper.dev |
| Brave Search | $5 credit a month (~1,000 searches) | `BRAVE_API_KEY` | brave.com/search/api |
| Exa | Monthly free credit | `EXA_API_KEY` | exa.ai |
| Google Programmable Search | 100 a day (existing keys only) | `GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX` | developers.google.com/custom-search |
| SerpApi | 250 a month | `SERPAPI_API_KEY` | serpapi.com |
| SearXNG | Unlimited (self-hosted) | `SEARXNG_URL` | docs.searxng.org |
| Jina Search | Free token grant | `JINA_API_KEY` | jina.ai |
| Parallel | $5 credit a month | `PARALLEL_API_KEY` | parallel.ai |

### 2. Page reading (full text of a leadership page or posting)

Firecrawl (`FIRECRAWL_API_KEY`), **Jina Reader (keyless**, `JINA_API_KEY` raises the limit), Tavily
Extract (`TAVILY_API_KEY`), Exa Contents (`EXA_API_KEY`), **direct fetch + local text extraction
(keyless**, public http(s) addresses only).

### 3. News and triggers (acquisitions, migrations, decommissioning, fines)

**Google News RSS (keyless)**, **Bing News RSS (keyless)**, **GDELT (keyless**, 65 languages),
NewsData.io (`NEWSDATA_API_KEY`, 200 a day), and the web-search chain limited to the past year.

### 4. Job postings across the market (new prospects naming a competitor)

| Provider | Free allowance | Environment variable(s) |
|---|---|---|
| TheirStack | 200 credits a month (1 per job) | `THEIRSTACK_API_KEY` |
| Adzuna | Free developer access | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` |
| JSearch (Google for Jobs, LinkedIn, Indeed) | ~200 a month on RapidAPI | `RAPIDAPI_KEY` |
| Jooble | Free key on request | `JOOBLE_API_KEY` |
| Reed (UK) | Free key | `REED_API_KEY` |
| USAJOBS (US federal) | Free key | `USAJOBS_API_KEY`, `USAJOBS_EMAIL` |
| **ATS X-ray** | Keyless postings; uses the web-search chain | — |
| Crustdata | Paid, capped per run | `CRUSTDATA_API_KEY` |

Every competitor product name plus "application retirement", "data archiving" and "legacy
decommissioning" (and any `job_terms` you add in Radar settings) are searched daily, starting at a
different phrase each day. A posting from a company the source says is below `min_employees`
(default 200) is dropped.

**ATS X-ray** asks the search engines for competitor-named postings on Greenhouse, Lever, Ashby,
Workday, SmartRecruiters, Recruitee and Workable. Each new board is read immediately through its free
public API and **added to the daily ATS scan** (`auto_targets`), so coverage grows every day without
spending more. The enrich step also probes accounts' own domains for a public board.

Public job boards read directly (keyless): Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
Recruitee, Workable, Personio.

### 5. Company size and HQ (mid-market vs enterprise, routing)

**Wikidata, GLEIF, SEC EDGAR (keyless)**, UK Companies House (`COMPANIES_HOUSE_API_KEY`, free),
People Data Labs (`PDL_API_KEY`, 100 a month), Hunter (`HUNTER_API_KEY`). Merged until headcount,
HQ (with state or ZIP for the US and Canada), domain and industry are known. Values a person typed
are never overwritten.

Every account gets a **segment**: Enterprise (1,000+ employees or $1B+ revenue), Mid-market (200+ or
$50M+), Small business, or unknown. Thresholds are the `segments` Radar setting; `target_segments`
(enterprise and mid-market by default) get deep dives first. Filter the account list with
`segment=enterprise|mid_market|smb|unknown|target`.

### 6. People who work at the account now

| Source | What makes it "current" | Environment variable(s) |
|---|---|---|
| Leadership pages (web search + reading) | the company's own page | uses the web-search chain |
| LinkedIn X-ray (search engines) | the public headline names the company as employer | uses the web-search chain |
| Hunter | the page it was found on still lists them | `HUNTER_API_KEY` (25 a month) |
| Tomba | same | `TOMBA_KEY`, `TOMBA_SECRET` (25 a month) |
| Snov.io | — | `SNOV_CLIENT_ID`, `SNOV_CLIENT_SECRET` (50 a month) |
| People Data Labs | current employer field | `PDL_API_KEY` (100 a month) |
| Apollo.io | current employer | `APOLLO_API_KEY` (plan dependent) |
| UK Companies House officers | active appointment, not resigned | `COMPANIES_HOUSE_API_KEY` |

The same person found by several sources becomes one candidate with every detail found (email,
LinkedIn, title) and `also_via`. People confirmed as current are listed first. Nobody is invented:
each person keeps the page or record that names them.

### 7. Email finder and 8. Email verification

Finder: Hunter, Tomba, Snov.io, Prospeo (`PROSPEO_API_KEY`, 75 a month), Skrapp
(`SKRAPP_API_KEY`), then address patterns checked by a verifier.

Verification: Reacher (self-hosted, unlimited, `REACHER_URL`), MailboxValidator
(`MAILBOXVALIDATOR_API_KEY`, 300 a month), ZeroBounce (`ZEROBOUNCE_API_KEY`, 100 a month),
Abstract (`ABSTRACT_EMAIL_API_KEY`, 100 a month), Hunter (50 a month), Tomba (50 a month). Only a
definite answer (valid, invalid, catch-all) stops the chain.

### 9. Website visitor companies (who is reading your site before they fill a form)

ipapi.is (`IPAPI_IS_KEY`, 1,000 a day), IPinfo Lite (`IPINFO_TOKEN`, unlimited, attribution
required), Ipregistry (`IPREGISTRY_KEY`, 100,000 on sign-up), **RDAP and reverse DNS (keyless)**.

Only visitors who accepted analytics cookies are tracked (the site sends `/api/track` only then).
Their network is looked up once a week; home broadband, mobile, VPN and cloud addresses are dropped,
and only business, government and education networks count. **The address is never stored**: the
lookup cache keys on a salted hash and the visitor keeps only the organisation's name, domain and
network type. Each daily run turns a week's identified visits into a **visit** signal on the
matching account (visitors, pages, which product lines they read about), creating the account if
it's new. Your own company and the competitors' staff are ignored. Switch off with the `visitors`
collector.

### 10. DNS and certificate history (competitor fingerprints)

MX/TXT from the local resolver, then **Google and Cloudflare DNS-over-HTTPS**; certificates from
**crt.sh, then Cert Spotter**; subdomains and CNAMEs from **HackerTarget, AlienVault OTX** (keyless,
`OTX_API_KEY` optional) and urlscan.io (`URLSCAN_API_KEY`). A host named for a product
(`documentum.acme.com`) or a CNAME into a vendor's service becomes a DNS/certificate signal. A batch
of `subdomain_batch` (default 40) account domains is checked a day, least recently checked first.

### 11. Tenders and public spend

EU TED (keyless), SAM.gov opportunities (`SAM_GOV_API_KEY`, free), UK Contracts Finder (keyless),
and **USAspending.gov awards to competitors (keyless)**: federal agencies already paying OpenText,
Smarsh, Global Relay, Mimecast, Proofpoint, Veritas, Archive360, Informatica or Delphix become
accounts with a **spend** signal.

### 12. AI models (deep dives, openers, reply classification)

`llm.py` fails over across Gemini, Groq, Mistral, OpenRouter, OpenAI and any OpenAI-compatible
server, and now also Cerebras (`CEREBRAS_API_KEY`), SambaNova (`SAMBANOVA_API_KEY`), GitHub Models
(`GITHUB_MODELS_TOKEN`), NVIDIA NIM (`NVIDIA_API_KEY`), Cloudflare Workers AI
(`CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`) and Hugging Face (`HF_TOKEN`). Model names change;
override them with the `SOL_*_MODELS` variables.

**Decision for you:** prospect personal data (deep dives, openers, reply classification) only goes
to the providers listed in `LLM_PII_PROVIDERS`, which defaults to `openai` because free tiers
generally have no data processing agreement and some may use prompts for training. With no OpenAI
key, deep dives fall back to the rules-based brief. To run them free, either list free providers you
have checked the terms of (for example `LLM_PII_PROVIDERS=groq,cerebras`) or run a model on your own
server (`SOL_CUSTOM_BASE_URL`, e.g. Ollama) and set `LLM_PII_PROVIDERS=custom`.

## What free sources cannot do

- **People from a prospect visiting competitors' websites, or researching solutions elsewhere.**
  That data only exists in paid intent co-ops (Bombora, G2 Buyer Intent, TrustRadius, 6sense); no
  free or lawful public source records other sites' visitors. What the Radar does instead, free: your
  own site's visitors by organisation (above), competitor job postings, competitor DNS/certificate
  fingerprints, filings, tenders, public spend and news. If you later buy an intent feed, its
  companies can be added as signals through the existing account and signal endpoints.
- **Sending more cold email by rotating free transactional email services.** Brevo, Mailjet, Resend
  and similar prohibit unsolicited email in their terms; rotating them gets accounts closed and hurts
  your domain's reputation. Outbound sending stays on your own SMTP mailboxes or Instantly.
- **Scraping LinkedIn itself.** The X-ray only reads search-engine results for public profiles.

## Settings added to Radar config (`PUT /api/admin/radar/config`)

| Key | Default | Meaning |
|---|---|---|
| `collectors.jobs_api` | on | Job-market search (section 4) |
| `collectors.ats_xray` | on | ATS discovery through search engines |
| `collectors.subdomains` | on | Passive DNS and certificate subdomains |
| `collectors.public_sector` | on | SAM.gov, Contracts Finder, USAspending |
| `collectors.visitors` | on | Website visitor organisations |
| `min_employees` | 200 | Postings from smaller companies are dropped (was 500) |
| `segments` | 200 / 1,000 employees, $50M / $1B | Segment thresholds |
| `target_segments` | enterprise, mid-market | Deep dives go here first |
| `job_terms` | none | Extra job-search phrases |
| `jobs_per_term` | 2 | Job sources asked per phrase a day |
| `xray_terms_per_run` | 5 | Competitor phrases searched on ATS hosts a day |
| `auto_targets` | on | Found boards join the daily ATS scan |
| `enrich_per_run` | 40 | Accounts sized and located a run |
| `subdomain_batch` | 40 | Domains checked for subdomains a run |
| `visitor_days` | 30 | Window for visit signals |

`RADAR_KEYLESS_SOURCES=off` turns the new keyless sources off; `RADAR_PROVIDERS_DISABLED=a,b` switches
named providers off from the environment.

## Endpoints

- `GET /api/admin/radar/providers`: every chain with status, usage and allowances.
- `PUT /api/admin/radar/providers` (admin): `{"order": {"web_search": ["tavily", ...]}, "disabled": [...], "limits": {"tavily": {"monthly": 1000}}}`.
- `POST /api/admin/radar/providers/{name}/test` (admin): one light call.
- `POST /api/admin/radar/accounts/{id}/enrich`: size, HQ and domain for one account now.
- `POST /api/admin/radar/run` with `steps` including `enrich`; `python radar_run.py enrich`.
