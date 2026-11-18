# MarkHub access control and dashboards

Settings → **Access and dashboards** (admins only) controls who sees and changes each part of MarkHub, and
which dashboard each person lands on. The server enforces every rule; the admin app only hides what a person
can't use.

## Levels

Each person has one level for each feature (Leads, Radar, Outbound, Compliance, Sol, SEO, Content, Migrate,
Website, Events, Form inbox, Dashboard, Settings):

| Level | Means |
|---|---|
| No access | The section is hidden and its API refuses them |
| View | Read only |
| Edit | The role's normal work there (a rep works leads and accounts) |
| Full | Everything in that feature, including what is otherwise admin-only (Radar settings, any account) |

A person's level comes from their **role template**, then any **personal exception** an admin sets. Admins
always have full access, so nobody can lock the admins out. The default templates match what each role could
do before this existed, so nothing changes until an admin changes it.

Giving someone **Full on Settings** lets them manage users and access. In practice that makes them an admin.

## What an admin can do

* **Features in use**: switch a whole area off for everyone but admins (Settings can't be switched off).
* **What each role can do**: set the level per feature for Sales, Content editor, Leadership and Compliance,
  and the dashboard each role lands on.
* **People**: per person, set their seniority, their dashboard and exceptions to their role (for example a
  sales manager with Full Radar, or a contractor with no Leads).

## Account ownership

Company-level fields on a Radar account (owner, status, notes, size, HQ, domains) can be changed by:

* the account's owner;
* an admin;
* anyone with Full Radar access.

Anyone who works the Radar can **claim** an unassigned account for themselves. Other accounts open read-only,
with a note naming the owner.

## Dashboards

| Dashboard | Who it suits | Shows |
|---|---|---|
| Executive | Leadership, admins | The existing overview: pipeline, channels, Radar and outbound KPIs |
| Manager | Sales managers | Team scorecard, strong accounts with no owner, accounts needing routing, sizes to confirm, pipeline |
| Rep | Sales | My accounts and their next steps, surging accounts, replies, my leads |
| Marketing | Content and marketing | New leads by channel, form submissions, content published |
| Compliance | Compliance | Suppression list, erasures, signed LIAs, send volume and bounce rate |

A person lands on the dashboard an admin assigned them. Otherwise it follows their role; a Sales user marked
Manager or Executive seniority lands on the Manager or Executive view. Admins can switch between all the views
on the Dashboard page to see what each team sees. Each view only shows areas the person has access to.

## API

* `GET /api/auth/me` returns `access`, `dashboard`, `seniority` and `disabled_features` for the signed-in user.
* `GET/PUT /api/admin/access` and `PUT /api/admin/access/users/{id}` (admins) manage the above.
* `GET /api/admin/home` returns the data for the caller's dashboard (`?view=` lets admins pick one).
