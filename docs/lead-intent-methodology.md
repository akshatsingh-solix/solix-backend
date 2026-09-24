# Lead intent and MQL methodology

How the website works out what each visitor is interested in, tags every lead
to a product line, and decides when a lead is an MQL. The code lives in
`scoring.py` (the model) and `intent.py` (tracking and lead rollup).

## 1. What we capture

Only after the visitor clicks **Accept analytics** in the cookie banner.
Visitors who choose **Essential only**, or whose browser sends Global Privacy
Control / Do Not Track, are never tracked. Their forms still work and still
create leads, just without browsing history. Nobody's chat text is stored as a
tracking event, only which product a question was about.

| Signal | When it fires | Points per product |
|---|---|---|
| Product page view | Opening `/products/<product>` (and `/platform`, `/services-support`) | 3 |
| Engaged reading | 45+ seconds of active reading on a topic page | 5 |
| Deep scroll | Reading 75%+ of a topic page | 2 |
| Resource opened | Opening a blog, white paper, datasheet, etc. tagged with products | 6 |
| Gated download | Unlocking or downloading gated material | 15 |
| Concierge question | Asking Sol about a product | 6 |
| Pricing question | Asking about price or cost | 10 |
| CTA click | Clicking demo / trial / contact from a topic page | 8 |
| Resource search | Searching Resources for a product | 2 |

Rules that keep the numbers honest:

- The **browser never sends points**. It reports what happened; the server
  applies the weights above.
- A signal scores **once per visit** per page/product, so reloads and repeat
  clicks don't inflate intent.
- Points **decay** with a half-life (default 30 days): interest from last
  quarter counts for much less than interest from this week.
- Campaign data (UTM tags, referrer, landing page) is captured from the first
  page of the visit.

## 2. From visitor to lead

A visitor becomes a **lead** the moment they identify themselves through any
form: demo, contact, gated download, newsletter, partner application, Solix
ECS trial sign-up, or a demo booked in the concierge chat. (Job applications
never create leads.)

- There is **one lead per email address**. Every form, visit and device the
  person uses is merged into it.
- Their earlier anonymous browsing is attached, and later browsing keeps adding
  to the same lead.
- Form submissions add points to the product the person chose or was reading
  about:

| Form | Points |
|---|---|
| Demo request | 30 |
| Trial sign-up | 25 |
| Contact sales | 15 |
| Gated download | 15 |
| Newsletter / partner | 5 |

## 3. Tagging to a product line

Product scores roll up into six product lines. The line with the most intent is
the lead's **primary product line**, and the top product within it is the
**primary product**.

| Product line | Products |
|---|---|
| Enterprise Edition & Common Data Platform | Enterprise Edition, Common Data Platform, Enterprise Data Lake |
| Archiving & Application Retirement | Enterprise Archiving, Application Retirement, Data Preservation, SAP, Oracle/OEBS, Mainframe, Email, File and Database Archiving, Active Archiving for Compliance |
| Data Governance, Privacy & eDiscovery | Enterprise Data Governance, Consumer Data Privacy, eDiscovery |
| Enterprise AI | Enterprise AI, Data Sense, Data Ask, Application Knowledge Graph, AI Warehouse, Agentic, AI Governance, AI Healthcare, EAI Pharma |
| Enterprise Content Services (ECS) | Enterprise Content Services |
| Professional & Managed Services | Services & Support |

Content published from the admin carries product tags, so every new blog or
datasheet feeds this model automatically.

## 4. Fit (who they are), max 40

| Signal | Points |
|---|---|
| Business email (not Gmail, Outlook, etc.) | 10 |
| Director, Head, VP, C-level, founder | 15 |
| Manager, lead, architect | 8 |
| Company size 1,000+ / 200+ / 50+ | 10 / 6 / 3 |
| Phone number given | 5 |

**Lead score = intent in the primary product line + fit.**

## 5. Stages

| Stage | Set by | Meaning |
|---|---|---|
| Lead | System | Known contact, not yet qualified |
| **MQL** | System | Asked for a demo, trial or contact (hand-raiser), **or** lead score reached the MQL threshold (default 45) |
| SAL | Sales | A rep accepted it |
| SQL | Sales | Budget, need and timing confirmed |
| Opportunity | Sales | Active deal |
| Won / Lost / Disqualified | Sales | Outcome |

The system only ever moves leads from Lead to MQL. Stages set by people are
never overwritten. Every change is recorded in the lead's history with who made
it and why.

The date a lead became an MQL, and the product line it had at that moment, are
kept, so "MQLs by product line" in reports never shifts after the fact.

## 6. Tuning

In **Admin → Settings → Lead scoring** an admin can change:

- the MQL threshold;
- the interest half-life.

Saving rescores every lead immediately. Scores also decay automatically every
6 hours. Event and form weights live in `scoring.py`, so changing them is a
code change and gets reviewed.

## 7. Reporting and access

- **Dashboard**: the leadership view. It shows visitors, leads, MQLs, SQLs and
  conversion rates, a daily trend, results by product line, the funnel, lead
  sources (first-touch channel), product interest, owners, top content, and
  country/industry. It filters by date range and product line, and every
  number links to the matching leads.
- **Leads**: filter by product line, product, stage, owner, source, country,
  industry, score, date or recent activity. Save a filter set as a shared
  view. Assign owners or change stages in bulk. Export to CSV or Excel; the
  Excel file includes a per-product-line summary sheet.
- **Roles**:

| Role | Can do |
|---|---|
| Admin | Everything |
| Sales | Work leads |
| Content editor | Publish content |
| Leadership (viewer) | Read-only dashboard, leads and exports |

## 8. Data retention

Raw behaviour events are deleted automatically after 180 days, which keeps the
free MongoDB tier small. Lead records, their scores and stage history are kept.
