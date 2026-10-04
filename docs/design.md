# Design — Claude Dashboard

A locked presentation system for the local dashboard. All pages share this system;
application logic, report shapes, and accounting remain outside its scope.

## Genre

Modern-minimal, tuned for a technical and austere local developer tool.

## Macrostructure family

- App pages: Workbench — compact operational header, instrument-style totals,
  ruled data surfaces, and expandable evidence.
- Content pages: the same Workbench shell with denser tabular detail.
- Marketing pages: not applicable.

## Theme

An application-specific Cobalt-on-light system. The cool paper and blue signal
borrow Cobalt's instrument-panel discipline, but the app deliberately keeps its
existing side rail and uses no external fonts, CDN assets, command palette, fake
browser chrome, or decorative dark band.

- `--color-paper`: cool near-white application canvas.
- `--color-ink`: cool graphite primary text.
- `--color-accent`: restrained electric cobalt for active and focus states only.
- Semantic warning, error, and success colours appear only when the data requires them.

## Typography

- Display: Bahnschrift / Aptos Display / Segoe UI Variable Display, weight 700, roman.
- Body: Aptos / Segoe UI Variable Text / Segoe UI, weight 400.
- Mono: Cascadia Code / SFMono-Regular / Liberation Mono, weight 500.
- No network font requests. The UI remains fully local and CSP-compatible.

## Spacing

A named four-point scale lives in
`src/claude_metrics/web/static/tokens.css`. Presentation CSS uses the named
tokens instead of raw spacing values.

## Motion

- No page or scroll reveals.
- Button press and refresh-state feedback only.
- Motion affects opacity or transform only.
- Reduced motion removes spatial transitions and caps feedback at 150 ms.

## Microinteractions stance

- Refresh exposes loading and failure states in-place.
- Successful refresh is silent: the updated page is the confirmation.
- Focus rings are immediate and visible.
- No hover-only functionality.

## Component voice

- Navigation: persistent side rail on desktop, horizontal route strip on narrow screens.
- Surfaces: thin rules and grouped rows, not nested floating cards.
- Controls: compact 6 px corners, explicit labels, 44 px minimum hit targets.
- Tables: tabular numerals, contained horizontal overflow, readable row rhythm.
- Footer: one compact inline rule, no sitemap columns.

## Per-page allowances

- Overview may use a wider cost cell and compact daily activity bars.
- Sessions prioritizes scan-friendly rows and stable identifiers.
- Detail uses ruled request blocks; receipts remain native disclosures.
- Data health may use semantic state colour, always paired with text.

## What pages must share

- Side rail, operational header, filter treatment, metric board, rule language,
  typography, colour, control states, and footer.
- Exact accounting labels and coverage language.
- Mobile behavior at 320, 375, 414, and 768 CSS pixels.

## What pages may differ on

- Density, column count, and which ruled data surface receives visual priority.
- No page may introduce another theme or navigation pattern.

## Exports

The production source of truth is the packaged `web/static/tokens.css`. These
portable mappings document how the same roles translate elsewhere.

### tokens.css

See `src/claude_metrics/web/static/tokens.css`.

### Tailwind v4 `@theme`

```css
@theme {
  --color-paper: oklch(98.5% 0.006 252);
  --color-ink: oklch(20% 0.025 258);
  --color-accent: oklch(52% 0.2 256);
  --font-display: "Bahnschrift", "Aptos Display", sans-serif;
  --font-body: "Aptos", "Segoe UI Variable Text", sans-serif;
  --spacing-md: 1rem;
  --text-md: 1rem;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
}
```

### DTCG `tokens.json`

```json
{
  "$schema": "https://design-tokens.github.io/community-group/format/",
  "color": {
    "paper": { "$value": "oklch(98.5% 0.006 252)", "$type": "color" },
    "ink": { "$value": "oklch(20% 0.025 258)", "$type": "color" },
    "accent": { "$value": "oklch(52% 0.2 256)", "$type": "color" }
  },
  "font": {
    "display": { "$value": "Bahnschrift, Aptos Display, sans-serif", "$type": "fontFamily" },
    "body": { "$value": "Aptos, Segoe UI Variable Text, sans-serif", "$type": "fontFamily" }
  },
  "space": {
    "md": { "$value": "1rem", "$type": "dimension" }
  }
}
```

### shadcn/ui CSS variables

```css
:root {
  --background: 98.5% 0.006 252;
  --foreground: 20% 0.025 258;
  --primary: 52% 0.2 256;
  --primary-foreground: 98.5% 0.006 252;
  --muted: 94.5% 0.012 252;
  --muted-foreground: 47% 0.02 258;
  --border: 88% 0.014 252;
  --input: 76% 0.018 252;
  --ring: 29% 0.13 256;
  --radius: 0.375rem;
}
```
