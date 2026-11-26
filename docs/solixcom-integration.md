# MarkHub × www.solix.com: integration specification

Status: November 2026. Code: `feat/solixcom-integration`. Everything here was checked against the live site on
26 Nov 2026, through public pages, its WordPress REST API and its scripts. Nothing on the site was changed.

## 1. Goal and principles

MarkHub runs **alongside** the current lead flow as a test and insight tool. It captures visitor activity, scores leads,
interprets behaviour, and maps each lead to a product line by intent.

- **Digiprise stays the system of record.** Leads, deals and companies keep flowing to Digiprise exactly as today.
  MarkHub receives a *copy*, and never alerts sales or emails a visitor about a solix.com lead.
- **The site keeps working if MarkHub is down.** Every piece fails open: the plugin queues, the tag sends in the
  background, and the sync only reads.
- **Consent comes from the site's own CookieYes banner.** MarkHub shows no banner and tracks nothing before the
  visitor accepts **Analytics**.
- **Every part has an off switch** (section 8).

## 2. How the pieces connect

```
 Visitor's browser on www.solix.com
   ├─ CookieYes banner ── "Analytics" accepted? ──┐
   ├─ GTM-53944LZ → MarkHub tag (/api/markhub.js) ┘── page views, reading, CTA clicks, PDF downloads ──► POST /api/track
   │                    └─ stamps markhub_visitor_id into every form
   └─ submits a form ──► WordPress theme handler (unchanged: saves, emails, Digiprise)
                            └─ MarkHub Connector plugin (after the handler succeeds) ──► POST /api/integrations/wordpress/submissions
 MarkHub (AWS)
   ├─ page & asset map (integrations/solixcom/site-map.json) → product, product line, industry, stage for every URL
   ├─ leads: scored, linked to browsing, mapped to product lines — observe-only
   └─ Sol: reads www.solix.com daily through /wp-json (read-only) and cites its pages
```

## 3. What solix.com looks like today (audit)

| Area | Finding | Impact |
|---|---|---|
| Platform | WordPress 7.1.5, Vantage child theme, WP Rocket (with RocketCDN), Apache on Amazon Linux | The plugin route works; WP Rocket doesn't cache POSTs. |
| Consent | **CookieYes**, GDPR mode, with categories Necessary, Functional, Analytics, Performance, Advertisement, Uncategorised. It sends Google Consent Mode v2 (all denied by default) and a `cookie_consent_update` dataLayer event. | MarkHub reads `getCkyConsent().categories.analytics` and listens for `cookieyes_consent_update`. |
| ⚠ Trackers before consent | With consent still "no", these were already running or had set cookies: Yandex Metrika (`_ym_uid`), Salesloft Scout (`sliguid`), factors.ai (`_fuid`), GetSiteControl, Clarity, LinkedIn Insight, Google Ads remarketing and lijit. | **A GDPR exposure that exists today, unrelated to MarkHub.** Fix in §5 W4. |
| Tag manager | GTM-53944LZ, plus hard-coded gtag (AW-766123600) and legacy `analytics.js` (Universal Analytics, which stopped processing data in 2023–24) | MarkHub is one more GTM tag. Remove `analytics.js`. |
| Forms | 9 forms. Two post back to their own page (full-page posts). The other seven post by jQuery AJAX to `templates/ajax-*.php` with the fields nested under a key (table below). | The plugin handles both kinds. The AJAX handlers must load WordPress (§5 W2). |
| ⚠ Gated assets | Every resource exists three times: `/resources/<kind>/<slug>/` (open page), `/resources/lg/<kind>/<slug>/` (form) and `/documents/<kind>/<slug>.pdf`. **The open pages link the PDF directly**, so the form is optional. | Leads from gating are undercounted. Fix in §5 W7. |
| Content | About 2,900 pages, 262 KB articles, 20 glossary entries and 42 leadership lessons. Also about 770 SEO "answers" pages under `/products/*/answers/` and a 27,000-item `answer` post type. "Posts" are links to external news sites. | All of it is mapped (§4.3). Sol skips the bulk SEO pages. |
| Overlapping tools | factors.ai and Salesloft Scout already identify visiting companies. The "Jessica" AI agent widget loads from `uat-cdn.nextlevel.ai`, a **UAT** CDN on production. | Decide which visitor-identification tool is the source of truth. Move the widget to its production CDN. |

### Forms

| # | Form (page) | How it's submitted | Wrapper key → fields | MarkHub type / form id |
|---|---|---|---|---|
| 1 | Request a demo (`/products/request-demo/`) | Full-page POST | top level: `demo_email`, `demo_fName`, `demo_lName`, `demo_business_name`, `demo_jobtitle`, `reg_solution_name`, `reg_message` | demo / `request-demo` |
| 2 | Gated resource (`/resources/lg/...`) | Full-page POST | top level: `new_email`, `first_name`, `last_name`, `company_name`, `user_phone`, `user_country` | download / `gated-download` |
| 3 | Contact sales (`/company/contact-sales/`) | AJAX → `ajax-contact-us.php` | `contact_us[cts_*]` | contact / `contact-sales` |
| 4 | Questions box (contact sales) | (unconfirmed) | `full_name`, `email`, `msg` | contact / `contact-question` |
| 5 | ECS demo | AJAX → `ajax-form-process-ecs-finance.php` | `ecs_finance_demo[...]` | demo / `ecs-demo` |
| 6 | CDP free trial ("get started") | AJAX → same | `cdp_demo_getStarted[new_email, page_url]` | trial / `cdp-trial` |
| 7 | ECS free trial ("get started") | AJAX → same | `ecs_finance_getStarted[new_email, page_url]` | trial / `ecs-trial` |
| 8 | Become a partner | AJAX → `ajax-form-process.php` | `partnerForm[...]`, fields found by name | partner / `partner` |
| 9 | Footer newsletter | AJAX → `ajax-form-process.php` | `newsletterHomeForm[...]`, fields found by name | newsletter / `newsletter` |

## 4. MarkHub changes (this branch)

### 4.1 Lead intake: `POST /api/integrations/wordpress/submissions`
- **Authentication:** the `X-MarkHub-Key` header must equal `WP_INTAKE_SECRET` (compared in constant time). An empty
  body with the right key returns 422 and creates nothing, which the plugin uses as a connection test.
- **Storage:** leads are stored with `source: "solix.com"` and the form id.
- **No duplicates:** no sales alert unless `WP_INTAKE_ALERTS=1`, and never a download email.
- **Product line:** taken from the visitor's stated interest, then the page or asset the form was on (via the map), then
  the resource slug. All of it is linked to the visitor's browsing through `markhub_visitor_id`.

### 4.2 The tag (`/api/markhub.js`, `integrations/solixcom/markhub-tag.js`)
- **Consent:** follows CookieYes. Before consent there is no identifier and nothing is sent. Withdrawing consent
  erases the identifier and stops tracking. It has no banner of its own.
- **What it tracks:** sessions, page and resource views, 45 seconds of engaged reading, 75% scroll depth, demo, contact
  and trial clicks, form views, and PDF downloads (recorded against the file's own path, so they map to a product).
- **Staff:** opening any page with `?solix_internal=1` marks that browser as internal, so it's left out of every metric.
- **Size:** about 10 KB minified, loaded `async`, so it never blocks rendering.
- **Verified** in a browser against the live CookieYes banner: nothing before consent; events after acceptance;
  identifier removed after withdrawal.

### 4.3 Page and asset map (`site_map.py`, `integrations/solixcom/site-map.{json,csv}`)
- **3,226 URLs.** Each has a type, a buying stage (awareness / consideration / decision), up to three products, its
  product lines, an industry, whether it's gated, and its PDF.
- **Resources:** the open page, its gated form page and its PDF resolve to one entry.
- **Coverage:** 91% of URLs map to a product. All product, solution, ebook, event and answer pages do.
- **Scoring** uses the map for every solix.com page view, resource view and download. Address rules come first.
- **Marketing review:** corrections go in `integrations/solixcom/site-map-overrides.csv` (columns: `path`,
  `products`, `industry`, `type`, `stage`) and survive rebuilds.
- **Rebuild** with `python -m site_map build`, monthly or after a site restructure. It's read-only and takes about a
  minute.

### 4.4 Sol (`site_knowledge.py`)
- **Sync:** daily, read-only, through `/wp-json` (about 40 requests, 0.5 seconds apart). It covers pages, KB, glossary
  and leadership lessons, and skips bulk SEO pages and gated duplicates.
- **Footprint:** about 1,700 pages, about 4,900 search chunks, +24 MB of memory.
- **Admin endpoints:** `GET /api/admin/sol/site-knowledge` (status) and `POST /api/admin/sol/site-knowledge/sync` (sync now).

### 4.5 Ready for AWS
- **`OPS_LOOP`** now gates all scheduled work: outbox, outbound sending, watchdog, the Radar daily run, rescoring,
  the solix.com sync, and resuming migrations. Exactly one container may run with `OPS_LOOP=1` (§6).
- **Client IP:** `CLIENT_IP_HEADER=cloudfront-viewer-address` is supported, with its `:port` removed.

### 4.6 Configuration

| Variable | Value | Notes |
|---|---|---|
| `WP_INTAKE_SECRET` | long random value | Shared with the WordPress plugin only. |
| `WP_INTAKE_ALERTS` | unset | Set to `1` only if MarkHub, not Digiprise, should alert sales. |
| `CORS_ORIGINS` | `https://www.solix.com,https://solix.com,<MarkHub admin origin>` | |
| `SOL_WORDPRESS_SITES` | `https://www.solix.com` | |
| `OPS_LOOP` | `1` on exactly one container | |
| `PUBLIC_API_URL` | `https://<markhub-api-host>` | Used in links inside emails. |
| `TRUSTED_PROXY_HOPS` or `CLIENT_IP_HEADER` | see §6 | Check with `GET /api/admin/ops/client-ip`. |
| `KEEP_AWAKE` | `0` | Only needed on Render's free tier. |

### 4.7 Not done yet
- **A review screen for the page map.** Today it's a CSV in the repo, edited by hand.
- **GA4 and Semrush for solix.com.** Proposed as a separate task.
- **A Digiprise sync policy.** MarkHub pushes `lead.updated` events to Digiprise when someone edits a lead in MarkHub.
  Keep that off during the test phase (unset `DIGIPRISE_URL`, or switch it off in Admin › Outbound › Health).
- **Free-trial interest.** Trial submissions always add Enterprise Content Services interest, even CDP trials. This
  should follow the trial's own product.
- **A flaky test.** `test_cms_migration::test_test_delivery_uses_latest_gated_item` failed once in four full runs
  and passes on its own. Not yet diagnosed.

## 5. Changes on the solix.com side

Owners: **W** = web/WordPress team, **M** = marketing ops (GTM, CookieYes), **L** = legal/privacy.

| # | Owner | Change | Required? |
|---|---|---|---|
| W1 | W | Install **MarkHub Connector 1.1**. Configure it in `wp-config.php`: `define('MARKHUB_API_URL', 'https://<markhub-api-host>'); define('MARKHUB_INTAKE_KEY', '<WP_INTAKE_SECRET>');`. Enable forwarding only after testing (§7). | Yes |
| W2 | W | Check that every `wp-content/themes/vantage-child/templates/ajax-*.php` handler loads WordPress (`require ... wp-load.php`). If one doesn't, add one line after its successful save, e.g. `do_action('markhub_capture', 'contact-sales', $_POST['contact_us']);`. The form ids are in §3. | Yes |
| W3 | W | If the server limits outbound traffic, allow HTTPS to the MarkHub API host. If a WAF (Wordfence, AWS WAF) protects `/wp-json/`, allow MarkHub's fixed egress IP (§6) to read it. | If applicable |
| M1 | M | GTM: add a **Custom HTML** tag on **All Pages**: `<script src="https://<markhub-api-host>/api/markhub.js" async></script>`. Check it in Preview mode first. No consent trigger is needed, because the tag checks CookieYes itself. | Yes |
| M2 | M | CookieYes: under **Analytics**, declare the MarkHub storage items (`markhub_vid` in localStorage; `markhub_session` and `markhub_touch` in sessionStorage), so the cookie table is accurate. | Yes |
| W4 | M | **Fix the pre-consent trackers.** Fire Yandex, Salesloft, factors.ai, GetSiteControl, Clarity, LinkedIn, Ads and lijit only after the matching CookieYes category: use GTM consent checks or CookieYes's own script blocking, and move hard-coded scripts into GTM. | Strongly advised (compliance) |
| W5 | W | Remove the legacy `analytics.js` (Universal Analytics). Move the AI agent widget off `uat-cdn.nextlevel.ai`. | Advised |
| L1 | L | Privacy policy: name MarkHub (first-party analytics and lead scoring, hosted on Solix's AWS) and the purposes. Record MarkHub in the processing register. | Yes |
| W7 | W | Real gating, if wanted: serve `/documents/*.pdf` only through a signed link issued after the form, or stop linking the PDF from the open pages. | Business decision |

## 6. Hosting MarkHub on Solix's AWS

**Compute: ECS on Fargate.** The repo's `Dockerfile` already works there.
- Run one uvicorn worker per container: rate limits and caches live in each process.
- Start with **one task** (`OPS_LOOP=1`), which is how it runs on Render today.
- To scale out, run a **web service** (2 or more tasks, `OPS_LOOP=0`) plus a **worker service** (exactly 1 task,
  `OPS_LOOP=1`). Running more than one task with `OPS_LOOP=1` sends outbound email twice.
- ALB health check: `GET /api/`.

**Database.**
- Recommended: **MongoDB Atlas on AWS** in the same region, over PrivateLink. No code changes.
- Amazon DocumentDB is possible. The features in use are compatible: only `$match`/`$group` aggregations, TTL indexes
  and `find_one_and_update`, with no `$text`, `$lookup` or change streams. Files are stored in documents of up to
  15 MB, under DocumentDB's 16 MB limit. Run the full test suite against a real DocumentDB cluster before choosing
  it: the in-memory tests can't prove compatibility.
- Move the data with `mongodump`/`mongorestore` during a short freeze.

**Secrets: Secrets Manager → task environment.** Copy these values from Render rather than generating new ones:
- `JWT_SECRET`: a new value signs everyone out.
- `UNSUBSCRIBE_SECRET`: a new value breaks the unsubscribe links in emails already sent.
- `TOOL_CONFIRM_SECRET`.
- `INSTANTLY_WEBHOOK_SECRET` and `DRIPIFY_WEBHOOK_SECRET`.
- All provider keys.

**Domain.** For example `markhub-api.solix.com`, with an ACM certificate on CloudFront or the ALB. After the move:
- Update `PUBLIC_API_URL` and `CORS_ORIGINS`.
- Update `REACT_APP_BACKEND_URL` for the admin and website builds.
- Update the Instantly and Dripify webhook URLs.
- Update the WordPress plugin's API address.
- Keep the old Render URL redirecting for 90 days, so links in emails already sent still work.

**Client IP.** Check each setup with `GET /api/admin/ops/client-ip` after deploying.
- **ALB only:** `TRUSTED_PROXY_HOPS=1`.
- **CloudFront → ALB:** `CLIENT_IP_HEADER=cloudfront-viewer-address`. Also restrict the ALB's security group to the
  CloudFront managed prefix list, so nobody can bypass CloudFront and forge the header.

**Outbound traffic.** Use a NAT gateway with an **Elastic IP**. That gives one fixed address for solix.com's WAF and
for partners' allowlists.

**Email.** Use Amazon SES: set its SMTP credentials as `SMTP_*` and verify the sending domain with SPF, DKIM and DMARC.
Keep `OUTBOUND_LIVE_SENDING` unset until launch is signed off.

**Edge.**
- AWS WAF with rate-based rules on `/api/track`, `/api/chat/*` and `/api/integrations/*`. The app's own limits are
  per process.
- CloudWatch Logs, with alarms on 5xx errors, task restarts and `/api/` health.
- Keep `DEADMAN_URL`.

**Backups.** Atlas continuous backup, or DocumentDB snapshots with point-in-time recovery.

**Frontends.** The MarkHub admin can move to S3 + CloudFront. If the GitHub Pages site stays where it is, keep
`GITHUB_DEPLOY_TOKEN` for instant rebuilds.

## 7. Rollout (each step can be undone)

1. **AWS:** deploy MarkHub with one task and `OPS_LOOP=1`, plus the configuration in §4.6. Check `/api/`,
   `/api/markhub.js` and `/api/admin/ops/client-ip`, then set the client-IP option.
2. **Internal traffic:** in MarkHub, add Solix's email domains and office IP ranges under Settings › Internal traffic.
3. **WordPress (W1–W3):** install the plugin with forwarding **off**, then run **Test connection** until it says
   "Connected".
4. **Turn forwarding on.** Submit each of the 9 forms with an internal email address. For each one:
   - Digiprise received it, and the site's own emails arrived once.
   - MarkHub shows it with source `solix.com` and a product line.
   - The plugin log says `sent`.
5. **GTM (M1–M2):** test in Preview mode, then publish as a named version. In a private window, accept Analytics, browse
   a product page, then submit a demo request. The MarkHub lead should show the browsing history and the product line.
6. **Privacy (W4, L1):** the pre-consent fixes and the privacy policy wording, in coordination with marketing, because
   tracker numbers will drop to consenting visitors only.
7. **Week 1:** each day, check the plugin log and queue. Each week, compare solix.com lead counts in MarkHub with
   Digiprise. Have marketing review `site-map.csv` and fill in `site-map-overrides.csv`.

## 8. Off switches

| Part | How to switch it off | Effect on solix.com |
|---|---|---|
| Lead copies | Untick forwarding in WordPress, or deactivate the plugin | None |
| Tracking | Pause the GTM tag, or revert to the previous GTM version | None (no banner of its own) |
| Sol reading solix.com | Unset `SOL_WORDPRESS_SITES` | None |
| The intake endpoint | Unset `WP_INTAKE_SECRET` (returns 503; the plugin queues for a week) | None |

## 9. Acceptance checks
- Before consent: no `/api/track` requests and no `markhub_*` storage. After accepting Analytics: requests every few
  seconds while browsing. After withdrawing: the storage is cleared.
- Each of the 9 forms produces exactly one MarkHub submission with the right type, plus the existing Digiprise record
  and site emails, unchanged.
- A visitor who reads `/resources/datasheets/data-ask/` and downloads its PDF has Data Ask interest. Their demo
  request maps to the AI product line.
- Page weight goes up by 10 KB or less. Form submission time doesn't change (the plugin sends after the response is
  finished, or queues the lead when it can't).
- `GET /api/admin/sol/site-knowledge` shows about 1,700 pages, and Sol answers "What is HIPAA compliance?" with a
  www.solix.com citation.
