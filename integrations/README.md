# MarkHub on www.solix.com

The full specification is [docs/solixcom-integration.md](../docs/solixcom-integration.md). It covers the audit of
the live site, every change on both sides, hosting on AWS, the rollout and the off switches.

| Folder or file | What it is |
|---|---|
| `wordpress/markhub-connector/` | The WordPress plugin. Zip the folder, then upload it under Plugins › Add New. It copies form submissions to MarkHub without changing what the site does with them. |
| `solixcom/markhub-tag.js` | The tracking tag, served at `/api/markhub.js` and loaded with one GTM Custom HTML tag. It follows the site's CookieYes banner. |
| `solixcom/site-map.json`, `site-map.csv` | The page and asset map (3,226 URLs → type, stage, products, product line, industry, gating, PDF). Rebuild with `python -m site_map build`. |
| `solixcom/site-map-overrides.csv` | Marketing's corrections (create it). The columns are `path`, `products`, `industry`, `type` and `stage`. These win over the generated map. |
