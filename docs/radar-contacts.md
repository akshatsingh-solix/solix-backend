# Finding the right people, their titles, emails and phone numbers

## Who: people and their current titles

**Find people** (account → Contacts) asks every source that's set up at once and merges the same person across
sources. Each person is labelled with the buying-committee role they'd fill, and **people who fill a role you have
nobody for come first**.

| Source | Gives | Cost |
|---|---|---|
| **SEC insider filings** (new) | Officers and directors of US-listed companies with the exact title they filed under ("EVP, Chief Information Officer") | Free, no key |
| **Prospeo person search** (new) | Directors, VPs, heads and C-suite at the domain, with current titles and LinkedIn links | 1 of Prospeo's 75 monthly credits per 25 people |
| UK Companies House officers | Directors of UK companies (now matched to the parent, e.g. "RS GROUP PLC") | Free key, set up below |
| Hunter, Tomba, Snov.io | People with work emails | 25 / 25 / 50 a month |
| Leadership pages, LinkedIn X-ray | Leaders named on the company's pages and public profiles | Uses web search |
| People Data Labs, Apollo | Person records | Free tiers (PDL hides contact details; Apollo depends on plan) |

## Email

**Find email** now tries cheapest-first:

1. **The company's own address pattern.** It is learned from emails you have already verified at that domain, or from
   the pattern Hunter reports. Trying it costs one verification and no finder credit.
2. Hunter, then Tomba, Snov.io, Prospeo and Skrapp.
3. Common patterns, each checked by the mailbox verifier.

Prospeo moved to its new `enrich-person` API: it only returns verified emails and charges nothing when there's no
match.

## Phone numbers: the person's own

**Find phone** on a contact only ever saves **that person's own mobile or direct line**. It tries, in order:

1. **A direct line the company prints next to their name** on its contact, press or investor pages. Free.
2. **Prospeo verified mobile.** 10 credits, only charged when a mobile is found, so about 7 a month on the free plan. The
   same call refreshes their title, adds a verified email if missing, and flags a recent job change.
3. **Tomba Phone Finder.** 5 a month, contacts outside the EU only, needs their email or LinkedIn URL.
4. **Apollo mobile reveal.** Uses Apollo's free credits. Apollo looks the number up in the background and sends it to a
   signed MarkHub webhook, so it appears on the contact a few minutes later. Apollo also says whether the number is on a
   Do Not Call list, and MarkHub shows that warning on the contact. Needs `APOLLO_API_KEY` and `PUBLIC_API_URL`.

If none of them has a personal number, MarkHub says so. It shows the company's switchboard (from SEC, Wikidata or the
company's own pages) as a clearly labelled fallback, never saved as the person's number.

**Company phone numbers** (Contacts tab) lists the switchboard, media, investor and direct lines the company
publishes, and adds any direct line printed next to a contact's name to that contact.

**How many personal mobiles to expect a month, free:** about 7 (Prospeo) + 5 (Tomba) + Apollo's free allowance, plus
any direct lines companies publish. Use them on the people the committee view says matter most.

**Calling rules still apply.** Mobile numbers are personal data:
* in the UK, screen against TPS and CTPS before calling;
* in the US, check the national Do Not Call registry for mobiles;
* in the EU, rely on your legitimate-interest assessment and honour objections at once.

## Test buttons and usage counters

* Every people, email and phone source now has a real **Test**:
  * account checks that spend no credit for Hunter, Tomba, Snov.io, Prospeo and Apollo;
  * a live lookup for Companies House (Tesco PLC's officers), SEC (Microsoft) and the company-website reader.
  * PDL stays untested, because a test would spend one of its free records.
* **Hunter's counter now matches Hunter.**
  * **What went wrong:** company lookups had no cap of their own, so they kept running and counting after the 25
    searches were used, and email lookups were counted twice. That's why the bar showed 282/25.
  * **The fix:** both now share Hunter's 25. **Test** on any Hunter row, and every Radar run, reads Hunter's own
    usage figures and sets the counter to match.

## Setting up Companies House

1. Go to developer.company-information.service.gov.uk, sign in, then **Create an application**.
2. **Application name:** "MarkHub Radar". **Description:** "Look up UK company details and directors for account
   research".
3. **Environment: choose Live.** Test keys only work on the sandbox, which has no real companies. This is the most
   common reason no data comes back.
4. Save, open the application, choose **Create new key**, and pick **REST** as the key type (not Stream). Leave
   *Restricted IPs* and *JavaScript domains* empty, because Render's addresses change.
5. Copy the key into Render → solix-backend → Environment → `COMPANIES_HOUSE_API_KEY`, then save. Render redeploys.
6. In Data sources, press **Test** on *UK Companies House officers*. It should list Tesco PLC's directors. If the key is
   wrong, the message now says so (Live vs Test, REST vs Stream).

The API you use is the **Public Data API**, which every new application can call. You don't need to subscribe to
Discrepancies, Document, Filing or Streaming.

## Adding Prospeo (recommended, free)

Sign up at prospeo.io and copy the API key from the dashboard into Render as `PROSPEO_API_KEY`. That one key turns on:
* person search;
* the email finder;
* verified mobiles.

They share 75 credits a month. **Test** shows the credits left.
