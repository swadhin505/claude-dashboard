# UI refresh

The dashboard keeps its existing FastAPI/Jinja stack and local CSS/JavaScript.
This is a presentation-only update: ingestion, reports, pricing policies, receipt
selection, database schema, and JSON API values are unchanged. The reference
`claude-usage` repository is untouched.

## Design

- Compact sidebar: Overview, Sessions, and Data health. On small screens it becomes
  horizontal navigation; no menu framework or extra JavaScript is needed.
- Neutral surfaces, restrained warm accents, tabular numbers, clearer typography,
  and consistent cards/tables. Layout grids adapt to narrower viewports.
- Four overview totals, daily token bars with readable counts and costs, exclusive
  token buckets, request attribution, and linked model/project breakdowns.
- Native expandable filters, cost coverage, assumptions, and receipt evidence keep
  the primary view shorter. Applied filters open automatically. Source-unavailable
  and failed-refresh notices open automatically; other notices remain discoverable.
- Sessions emphasize project, interval, usage, and cost. Details preserve all six
  token buckets, source evidence, pricing receipts, and tool-result counts.
- Data health groups sources/ingestion, pricing/provenance, and coverage/diagnostics.

Design references: [shadcn/ui dashboard blocks](https://ui.shadcn.com/blocks?category=dashboard)
and [Tabler](https://tabler.io/). These informed the organization and restrained
card/table styling; no third-party component code, fonts, or assets were copied.
No dependency or frontend build step was added.

## Money and coverage

Primary cost labels use integer half-even rounding to cents. A positive amount
below one cent displays `<$0.01`, never `$0.00`. Unpriced amounts remain explicitly
unpriced; partial amounts remain labelled as known portions. Estimated badges and
missing-token/unit coverage remain visible without expanding the exact-value panel.

Expand **Exact value & coverage** for the full nanodollar-precision amount and
coverage counts. Request-level exact effective values remain under **Calculation
receipt & source evidence**. No accounting value is rounded in storage or the API.
For example, `5,143,653,450` nanodollars displays as `$5.14`, with `$5.14365345`
available in the disclosure. API-equivalent value is not a subscription invoice.

Daily bars show known token counts relative to the maximum on that page. Their
exact counts are readable text; the SVG is decorative for screen readers. They
replace the old separate vertical token/cost charts with one compact daily table.
Paged groups, timezone rules, inclusive dates, and full-selection totals are unchanged.

## Verification and remaining visual check

**279 automated tests pass** on Windows/Python 3.12.11. The new checks cover integer
cent rounding (including sub-cent and large values), exact-value preservation,
chart geometry, semantic navigation, table headers, native filters/disclosures,
missing-source notices, and local-only markup. Existing truth fixtures, HTTP/API
equivalence, escaping, security, refresh, and recovery tests also pass. Ruff lint
and formatting checks pass.

No connected browser was available. Rendered HTML/HTTP checks are **not** a visual
or full accessibility sign-off. Before visual acceptance, check in a browser:

1. Overview, Sessions, a request detail, and Data health at desktop width, 768px,
   and 375px. Confirm narrow tables scroll within their panels and long IDs/paths
   do not widen the page.
2. Keyboard navigation: skip link, navigation, refresh, filters, disclosures, and
   pagination; visible focus must remain clear.
3. Apply and reset filters, follow model/project/session links, and check timezone
   and date preservation. Expand costs and compare exact values with the API/CLI.
4. Review empty, estimated, partial, unpriced, missing-source, and stale states.
   Check loading, successful reload, and error text when refreshing.

Use the normal README serve command. To review only retained data without a startup
scan, append `--no-scan`. If the server was already running during the code update,
restart it and reload the page. This UI update did not scan or reprice personal data.
