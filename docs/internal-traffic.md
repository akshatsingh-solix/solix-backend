# Internal traffic: keeping Solix's own people out of the numbers

Staff browse the site, test the forms, chat with Sol, sign up for trials and register for events. Counted as
prospects, that activity inflates visitors, leads, MQLs, chat totals, content views and Radar "visit" signals,
and emails the sales inbox about the team itself. `internal_traffic.py` recognises it, flags every record it
touches, and gives every metric in MarkHub one switch.

## How someone is recognised

The first rule that matches is kept as the record's reason (`internal_reason.kind`):

| Reason | What matches | Set in |
| --- | --- | --- |
| `email_domain` | An address at an internal domain or any subdomain (`solix.com`, `india.solix.com`, `solixtechnologies.com`) | Settings → Internal traffic, or `INTERNAL_DOMAINS` |
| `email` | An address on the internal list: agencies, contractors, colleagues' personal test addresses | Settings → Internal traffic |
| `staff` | A MarkHub user or a Settings → Team member | Automatic (can be switched off) |
| `ip` | A request from an office or VPN range (CIDR) | Settings → Internal traffic, or `INTERNAL_IP_RANGES` |
| `network` | The visitor IP lookup (`radar_visitors.py`) names an internal domain as the network's owner | Automatic |
| `device` | A browser that signed in to MarkHub, or opened any site page with `?solix_internal=1` | Automatic / shareable link |
| `linked` | The same person as an internal record | Automatic |
| `manual` | Marked by hand in MarkHub (lead drawer, form inbox, Sol transcript) | Per record |

IP addresses are compared as they arrive and never stored; the record keeps the rule's label (for example
"Santa Clara office"), not the address.

## One person, every record

A flag spreads to everything that is the same person:

```
visitor ──► its page events, Sol profile, Sol conversations
   ▲  │
   │  ▼
  lead ◄──► every form submission and event registration under the email, every Sol chat that shared it
```

So a colleague who browses from the office and then fills in the demo form from home with a Gmail address is
still internal: the browser was flagged by the office range, and the lead inherits it (`linked`).

## Hand corrections win

"Not internal" on a lead (or a form, or a chat) is sticky: no rule, re-scan or later form flips it back, and the
person's forms, registrations and chats follow. "Mark internal" is sticky the same way. Browsing that was only
internal because of that lead is cleared with it; a browser flagged in its own right (office network, MarkHub
sign-in) stays flagged.

## What the flag changes

| Where | Behaviour |
| --- | --- |
| Every admin metric and list | Hidden by default. The header switch (Hidden / Included / Only) sends `internal=exclude|include|only` on every admin read. Dashboard, Leads, Form inbox, Sol chats and Events say how many internal records the current view leaves out. |
| Leads export, form export, registration export | Follow the switch; the lead and registration exports carry an "Internal" column. |
| Home dashboards (rep, manager, marketing) | Always exclude internal traffic. |
| Sales alert email | Skipped for internal forms and chats (logged as `skipped`, "Internal traffic: …"). Setting: *No sales alerts for internal forms and chats*. |
| Radar and outbound | Internal leads aren't linked to Radar accounts or handed to outbound; internal visitors never create a Radar "visit" signal. Setting: *Keep internal leads out of Radar and outbound*. |
| Sol | An internal conversation's questions don't become knowledge gaps; Sol quality insights follow the switch. |
| Content engagement (views and downloads in the content list) | Staff reads don't count. |
| Event seats | Staff registrations still take a seat (capacity, seats left), but are left out of attendee numbers by default. |
| AI tools / MCP | `leads_search` and `reports_overview` take `internal: exclude|include|only` (default exclude). |

## Settings → Internal traffic (admins)

* Company email domains, other internal addresses, office/VPN ranges with labels.
* **Add the network I'm on**: adds the address MarkHub sees you on as a range (it also tells you whether you
  are already on an internal network).
* Switches: recognise internal traffic at all; staff addresses count; mark browsers that sign in to MarkHub;
  no sales alerts for internal activity; keep internal leads out of Radar and outbound.
* **Re-scan history** applies today's rules to everything already recorded and clears rule-based flags that no
  longer match (an email-domain flag after the domain is removed). IP, network and browser flags are kept: the
  address was never stored, so they can't be re-derived. Hand corrections are never touched.
* **This browser**: mark or unmark the browser you're using. Colleagues who never sign in to MarkHub can open the
  shown `?solix_internal=1` link once per device (`?solix_internal=0` undoes it).

Sales users (and editors, for Sol chats) can correct individual records; only admins change the rules.

## API

| Method | Path | Who |
| --- | --- | --- |
| GET | `/api/admin/internal` | Settings, current summary by reason, your network | anyone with Settings access |
| PUT | `/api/admin/internal` | Save the rules | admin |
| POST | `/api/admin/internal/rescan` | Apply the rules to history | admin |
| POST | `/api/admin/internal/this-browser` | `{visitor_id, internal, explicit}`: mark the caller's browser | any signed-in user |
| POST | `/api/admin/leads/{id}/internal` | `{internal}` | admin, sales |
| POST | `/api/admin/submissions/{id}/internal` | `{internal}` (moves the whole lead when there is one) | admin, sales |
| POST | `/api/admin/chats/{session_id}/internal` | `{internal}` | admin, sales, editor |

Every admin list or report that counts people reads the `internal` query parameter (default: the saved
`default_view`, normally `exclude`) through `internal_traffic.view_mode` and adds `internal_traffic.scope(mode)`
to its query. New metrics should do the same.

## Environment (optional)

```
INTERNAL_DOMAINS=solix.com,solixtechnologies.com   # defaults until an admin saves the settings
INTERNAL_IP_RANGES=203.0.113.0/24,198.51.100.10    # office / VPN egress, CIDR or single addresses
```

Saved settings in MarkHub replace these.
