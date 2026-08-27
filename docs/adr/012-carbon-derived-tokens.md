# ADR 012 — Artifact colour comes from a pinned upstream design system, generated

**Status:** Accepted · **Date:** 2026-08

## Problem

The artifact renderer's visual foundation was a hand-authored palette: ~20 colour literals
chosen by eye, in one JSON file. It passed its contrast gates, so it was not broken. It was
*unaccountable* — no value could be traced to a source, no rule explained why `#1470b8` was
the client accent, and every future kind, mode or renderer meant picking more colours by hand
and hoping the gates still passed.

The requirement was a foundation that could be **updated from an authoritative source**
rather than maintained by eyedropper, without importing a design system or adding a runtime
dependency to a component whose entire premise is that it works with no network.

## Decision

Derive colour **values** from IBM Carbon at a pinned version, generated into the repository;
keep the **mapping** hand-authored.

- Upstream: `@carbon/themes@11.80.0` (Apache-2.0), which publishes its tokens as DTCG JSON at
  `src/dtcg/`. Format: DTCG Format Module **2025.10**, the first stable release.
- `themes/carbon.tokens.json` — generated, values only, each recording its upstream path.
- `themes/artifact.tokens.json` — authored: which semantic role each value serves, plus
  typography, geometry, edge dash patterns and type labels, which Carbon has no opinion about.
- `tools/update_carbon_tokens.py` — developer command. Pinned, integrity-verified, resolves
  only the tokens the authored file aliases, emits no timestamp.
- `tokens.py` — resolves both into the flat dict the renderers already read.

Renderers see semantic names only. A test asserts the renderer hard-codes no colour.

## Alternatives considered

1. **Depend on `@carbon/themes` at runtime.** Rejected outright. The component's value is that
   an artifact renders with no network and no package manager; the renderer is Python and
   would need Node for this.
2. **Copy Carbon's values by hand into the existing file.** Rejected: it looks like the same
   result and is not. There is no update path, no provenance, and the copy silently drifts.
3. **Vendor Carbon's DTCG files whole.** Rejected: 255 KB of tokens for a diagram renderer
   that uses about thirty, and every unused token is one more thing a future reader assumes is
   load-bearing.
4. **Keep the hand-authored palette.** Rejected — but it is worth naming what it did well: it
   was already semantic, already gated, and the renderer already had no colour literals. That
   is why this change is a substitution behind an existing seam rather than a rewrite.

## Consequences

- Bumping the upstream is `--pin` plus a commit. `--check` fails a stale tree, and it is
  meaningful because the output is byte-reproducible at a given pin.
- Contrast is now a **property of a documented rule** rather than of individual choices:
  accents take Carbon step 60 on light, 40 on dark. [REAL] across ten families that lands at
  4.53–4.57:1 light and 6.15–6.48:1 dark.
- That uniformity makes accents near-identical in luminance, so the C4 text-label rule stops
  being belt-and-braces and becomes the actual carrier of meaning in greyscale. Already
  implemented; now load-bearing, and tested as such.
- Apache-2.0 obliges attribution and a statement of modification. `NOTICE` carries both;
  `licenses/carbon-LICENSE.txt` carries the text. Upstream ships no `NOTICE` to propagate.
- ailocal takes no new dependency and remains standard-library only. The generator uses
  `urllib` and `tarfile`, runs on a developer's machine, and is never imported by the server.

## Measurements

- Design gates: **25 → 47** checks, 0 failures. New gates cover accents on all three grounds,
  border visibility, DTCG conformance of the generated file, and provenance. [REAL]
- Full artifact suite through the provisioned runtime: 4/4 files, **171** checks. [REAL]
- Generated palette **15.8 KB** from 255 KB upstream. [REAL]
- A rendered dense diagram contains **26 distinct colours, all 26 traceable to a token, zero
  stray literals**, in both modes. [REAL]
- The first mapping attempt was caught by a new gate: `border.subtle.01` resolves to the same
  `gray.20` as `layer.accent.01`, i.e. an invisible group outline. Remapped to
  `border.subtle.02`. [REAL]

## Revisit if

- Carbon stops publishing DTCG source, or moves it out of the package — the generator fails
  loudly on a missing path rather than falling back, which is the intended behaviour.
- A renderer needs tokens Carbon does not model. Diagram edges already are that case, and the
  answer was to author them, not to bend a UI token into the role.

## Deeper reference

- `src/ailocal/resources/integrations/local-artifacts/DESIGN-SYSTEM.md` — authoritative:
  token architecture, accessibility gates, update procedure, licensing.
- ADR 011 — why the component ships inside ailocal at all.
