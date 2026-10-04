# Pinned price catalog

`litellm-model-prices.json` is the unmodified upstream LiteLLM file at the commit
recorded in `catalog-metadata.json`. Its SHA-256 is checked before loading.
The upstream license is retained alongside it. Rates are loaded as `Decimal`.

This is a catalog snapshot, not an effective-dated historical price schedule.
Phase 1 verifies and packages it; model resolution and request pricing arrive
in Phase 3. The runtime never downloads prices automatically.
