# Radar intelligence: accuracy, product fit, persona messaging, self-review

Four layers on top of the Radar, each built from evidence on the account and each explaining itself.

## 1. Data accuracy (`data_quality.py`)

**No data source can promise 98% accuracy.** Bought and scraped company data has errors. MarkHub makes accuracy
*measured* and *enforced* instead of claimed.

* **Provenance.** Every value a source reports is kept with where it came from (`companies.facts`, plus
  `size_candidates` for headcount). The free registries (SEC, Wikidata, GLEIF, Companies House) are asked even after
  a paid source answered, so most facts get a second, independent source.
* **Probability per value.** Agreeing sources multiply their chances of being wrong; a disagreeing source counts
  against. Each value is one of:
  * **Verified:** a person confirmed it, or its sources push it past the accuracy target (98% by default, Radar
    setting `accuracy_target`).
  * **Likely:** 90% or more.
  * **Unverified:** one weak source.
  * **Sources disagree:** sources conflict and no value is clearly right.
* **Measured source accuracy.** Each source is scored right or wrong (`accuracy_log`) whenever:
  * a person confirms a value, or corrects it in Account details;
  * a mailbox is verified, an email bounces, or a contact replies;
  * a reply says "wrong person".

  A source's trust is its measured rate, blended with a starting estimate until it has about 20 checks. A source that
  proves unreliable stops being able to verify anything alone.
* **The headline number** (Radar → Data accuracy) is the share of *shown* values people found right, with a 95%
  interval. The target counts as met only when the low end of the interval clears it, so 10 lucky checks can't claim
  98%. The **audit queue** picks values at random so that number isn't biased towards accounts people already looked
  at. About 200 checks at 99% are enough to prove 98%.
* **Outreach gate** (Compliance setting `data_gate`):
  * `conflicts` (default) blocks accounts whose size or industry sources disagree;
  * `verified` blocks until every core fact (industry, HQ country, website, employees) is verified;
  * `off` only warns.
* **Filters:** "Verified data only" and "Industry" on the Radar list.

## 2. Product fit, cross-sell and up-sell (`product_fit.py`)

Each account gets a ranked pitch plan, and every play states the reasons behind it:

* **Displacement:** a competitor product in their job posts, DNS records or filings points to its Solix replacement.
* **Problems:** words in their signals, such as decommissioning, SAP, mainframe, retention, legal hold, privacy or AI.
* **Industry:** the regulation or estate that makes the pain real (for example SEC 17a-4 in banking, legacy EHRs in
  healthcare, or S/4HANA in manufacturing).
* **Website intent:** products their people read about on solix.com.
* **Size:** enterprises with several needs see the platform.
* **Relationship:** record the products a customer owns ("Already a customer?" on the account). Those products are
  never pitched again; what follows them becomes **up-sell** (same line or the platform) or **cross-sell** (another
  line). For prospects, the top play is **Lead with** and the rest show what to expand into once it lands.

## 3. Persona-based first touch (`messaging.py`)

Each contact's first email is built from a brief:

* **Their role.** Eight personas: IT leadership, data leadership, security, compliance/legal/records, architecture,
  infrastructure, finance and content. Each has what it cares about, its likely pain, the outcome Solix gives it, and
  its own words.
* **Seniority.** Executives get three short lines, managers the programme, practitioners the how.
* **Their industry** and the account's **lead product** from the pitch plan.
* **The account's own trigger.** A competitor is never named.

The sequence **Persona-led: explore, then buy** is added automatically:

| Touch | Day | Intent | What it does |
|---|---|---|---|
| 1 | 0 | Exploration | Offers something useful to read or asks one question. No meeting request. |
| 2 | 4 | Exploration | A different angle on the same pain, with a link to the product page. |
| 3 | 9 | Buying | Offers a 20-minute working session with a concrete outcome. |
| 4 | 14 | Close | Closes the loop and asks who owns it if not them. |

Every opener is scored for personalisation. A model-written draft that is generic, names a competitor, uses stock
phrases or asks for a meeting too early is replaced by the approved deep dive or the persona rules. **First touch** on
any contact previews the brief and all four emails before anyone is enrolled. Compliance setting `site_url` sets the
domain for product links.

## 4. Weekly self-review (`learning.py`, Dashboard → MarkHub intelligence)

Every Monday, after signal weights are re-learned, and on demand with **Review now**, it does four things:

* **Learn.** Positive reply rates by persona, seniority, industry, pitched product, opener source and opener quality.
  Measured accuracy by source. Strong accounts nobody is working, by industry, territory and segment.
* **Tune, within bounds.** The product-fit weight for each industry and product moves towards its observed reply
  lift:
  * at most ±0.2 a week;
  * always between 0.5 and 1.5;
  * only once that pair has 8 or more sends behind it.

  Every change is logged with its evidence and has **Undo**. Turn **autotune** off and changes are only suggested,
  for an admin to apply.
* **Track.** A weekly snapshot (accounts, signals, sends, positive replies, accuracy), so the brief can say what moved.
* **Report.** Wins, risks, opportunities and data issues, each with the action to take. These show on the Executive
  and Manager dashboards and are posted to the pipeline channel.

## API

| Endpoint | What it does |
|---|---|
| `GET /api/admin/radar/accounts/{id}` | Now includes `quality` (per-field views) and `fit` (pitch plan) |
| `POST /api/admin/radar/accounts/{id}/confirm` `{fields, audit}` | Confirm shown values; scores their sources |
| `GET /api/admin/radar/accuracy` | Measured accuracy by field and source, with intervals |
| `GET /api/admin/radar/accuracy/audit` | A random sample of values to check |
| `PATCH /api/admin/radar/accounts/{id}` `{products_owned}` | Record what a customer owns |
| `GET /api/admin/outbound/contacts/{id}/brief` | The persona brief and the four touches |
| `GET /api/admin/intelligence` | Latest review, settings, tuning log, trend |
| `POST /api/admin/intelligence/review` (admin) | Run the review now |
| `PUT /api/admin/intelligence/settings` (admin) | Change settings |
| `POST /api/admin/intelligence/apply` (admin) | Apply suggested tuning |
| `POST /api/admin/intelligence/tuning/{id}/revert` (admin) | Undo one change |

## 5. Free AI without personal data (`privacy.py`)

The Radar's AI runs on free model tiers (Gemini free, Groq, Mistral, OpenRouter `:free`, Cerebras, and others).
Personal data never reaches them:

* **Removed before any call:** email addresses, phone numbers, personal profile links (LinkedIn `/in/`, X, GitHub),
  and people's names. Names come from the people we know about, plus any "Name, Title" pattern. Each is replaced with a
  placeholder such as `[PERSON_1]` and put back on our server when the answer arrives.
* **Deep dives:** leadership pages are never sent. The model returns no contacts; people are found by the rules on
  our server. The provider shows as, for example, "groq (personal data removed)".
* **Openers:** the brief carries the role and company only, never the person's name or exact title.
* **Reply reading:** the signature is cut off and every identifier is removed before classification.
* **Providers cleared for personal data** (`LLM_PII_PROVIDERS`: OpenAI, your own or a company endpoint) still get the
  full text when they're configured, and take priority.
* **Turning it off:** set `RADAR_FREE_AI=off` to keep free tiers out of Radar work completely.

To switch it on, add one free key on Render: `GEMINI_API_KEY` (already set for Sol), `GROQ_API_KEY`, or
`OPENROUTER_API_KEY`. Live sources then shows **AI model: on · free, personal data removed**.

## 6. Buying stage, committee and lookalikes (`account_intel.py`)

* **Buying stage.** Each account is placed at the furthest stage its evidence supports:

  | Stage | Evidence |
  |---|---|
  | Problem aware | Signals |
  | Exploring | Website visits, intent data, a "not now" reply |
  | Evaluating | An MQL, 3+ people engaged, an "interested" reply |
  | In conversation | A meeting, or a sales-accepted lead |
  | Buying | An opportunity, or won |

  Each stage comes with its play: an **exploration** play (insight, resource, a question) until the account is
  evaluating, a **buying** play (working session, business case, proof) after that. Stage changes are dated and kept
  in the account history. You can filter the Radar by buying stage.
* **Buying committee.** The roles the lead product line needs (for example, archiving needs IT leadership,
  architecture, compliance and records, and infrastructure). The drawer shows who we have for each role, who has
  engaged, and which roles are missing.
* **Lookalikes.** Each week, every account is scored on how strongly it shares the traits of the accounts that
  replied positively or reached a meeting: industry, size, competitors, signal mix and lead product. The Radar's
  **Lookalikes** tab lists those scoring 60 or more, with the traits they share. This starts once 3 accounts are
  positive.
* **The weekly brief** now also reports:
  * accounts that moved forward a stage;
  * accounts stuck in evaluating for 3+ weeks;
  * the most-missing committee role;
  * lookalikes.

  It opens with a four-sentence summary written by whichever model is available. The model sees only aggregate
  numbers.
