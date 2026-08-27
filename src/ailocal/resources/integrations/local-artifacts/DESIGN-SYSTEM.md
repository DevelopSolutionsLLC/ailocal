# The artifact design system

Authoritative. Everything else — README, the skill, the ADR — links here rather than
restating it.

## The boundary this exists to hold

| | Owns |
|---|---|
| **The model** | semantic content, relationships, labels, the requested `format` |
| **The design system** | colour, typeface, visual hierarchy, spacing, presentation defaults |

A local model asked for a diagram will happily emit `style Start fill:#f9f`. That is the
model reaching across the boundary, and the renderer takes it back: Mermaid presentation
directives (`style`, `classDef`, `class`, `linkStyle`) are **stripped** before the page is
built. [REAL] on captured model output, five directives dropped and none of `#f9f`, `#0f0`,
`#e1f5fe`, `#fff9c4` survive into the page.

Stripping is not repair. A *syntactic* defect the model reliably makes and Mermaid reliably
rejects — an unquoted `Reviewer(s)` inside `[...]` — is normalised, idempotently, changing no
semantics. A *semantic* error stays a visible failure rather than being guessed at.

There is deliberately **no opt-in** for model-specified presentation. Adding one is a design
change, not a configuration change: it would need its own tests, because the value here is
that every artifact from every model looks like one product.

## Where the values come from

Colour values are **not hand-picked**. They are derived from IBM's Carbon Design System,
which publishes its tokens in the [DTCG](https://tr.designtokens.org/format/) format —
Design Tokens Format Module **2025.10**, a Final Community Group Report and the first stable
version of that spec.

```
@carbon/themes@11.80.0  src/dtcg/{color-palette,g10,g100}.json   upstream, pinned
        │
        │  tools/update_carbon_tokens.py   (developer command, resolves aliases)
        ▼
themes/carbon.tokens.json                  GENERATED — values only, do not edit
        │
        │  themes/artifact.tokens.json     AUTHORED — which role each value serves
        ▼
tokens.py  ->  architecture.py SVG  /  the Mermaid page
```

Two files, and the split is the whole point:

| File | Status | Holds |
|---|---|---|
| `themes/artifact.tokens.json` | **hand-authored** | the mapping: `surface` is `{carbon.light.layer.01}`. Plus typography, geometry, edge dash patterns and type labels, which Carbon has no opinion about. |
| `themes/carbon.tokens.json` | **generated** | resolved sRGB values, each recording the upstream path it came from. |

Renderers read semantic names — `surface`, `ink`, `accent.client` — through `tokens.load()`.
**No renderer learns that a value came from `blue.60`.** Swapping the upstream is therefore a
regeneration, not a renderer change, and a test asserts the renderer hard-codes no colour.

### Which upstream theme backs which mode

`light` ← Carbon **g10** (gray.10 ground, white layers). `dark` ← Carbon **g100** (gray.100
ground, gray.90 layers). g100 is g10's structural inverse, which is what keeps the two modes
identical in construction rather than two separately-tuned palettes.

### The accent step rule

Diagram accents take Carbon step **60** on light and step **40** on dark. One rule, because
Carbon tunes every family so a given step lands at the same contrast:

[REAL] measured across blue, teal, purple, cyan, green, orange, magenta, coolGray, yellow and
red — light step 60 lands at 4.53–4.57:1 on white, dark step 40 at 6.15–6.48:1 on gray.100.

That uniformity has a consequence worth stating plainly: **the accents are near-identical in
luminance, so they are nearly indistinguishable in greyscale.** That is not a defect being
tolerated. It is why the C4 rule below is load-bearing rather than decorative.

## Accessibility, as gates rather than opinions

`test_design.py` computes every number from the token files, so a palette edit or an upstream
bump that regresses legibility fails there rather than in someone's browser. WCAG 2.2
thresholds, applied where they actually apply:

| Claim | Gate | Measured (light / dark) |
|---|---|---|
| `ink`, `muted`, `faint` on `surface` — normal text | ≥ 4.5:1 | 18.10 / 7.81 / 5.02 · 13.76 / 8.86 / 6.36 |
| Accents on `canvas`, `surface`, `group_surface` — connectors and type rails carry the relationships, so they are graphical objects required for understanding | ≥ 3.0:1 | worst 3.78 · worst 4.69 |
| `border` differs from every ground it sits on | not equal | holds |

A decorative border is deliberately **not** held to 3:1. A card outline that merely reinforces
a boundary already carried by fill, accent rail and text is not required for understanding,
and demanding 3:1 of it would push the palette somewhere worse for no accessibility gain. It
must still be *visible*, which is a weaker and separate claim — and a real one: the first
Carbon mapping tried `border.subtle.01`, which resolves to the same `gray.20` as
`layer.accent.01`. A group outline that could not be seen at all. That is now gated.

### Colour is never the sole carrier

From C4 notation guidance (`c4model.com/diagrams/notation`): every element states its TYPE,
relationships are labelled and directional, and the notation must survive black-and-white
printing and colour blindness. So:

- every node prints its kind — `CLIENT`, `ROUTER`, `MODEL` — as text;
- every edge kind pairs its accent with a **dash pattern** *and* a text label;
- every diagram carries a key.

Tests assert each accent kind has a printed label and each edge kind has a text label. Given
the luminance uniformity noted above, these are the mechanism, not a nicety.

## Updating the tokens

```sh
python3 tools/update_carbon_tokens.py            # regenerate at the pin
python3 tools/update_carbon_tokens.py --check     # fail if the committed file is stale
python3 tools/update_carbon_tokens.py --pin 11.81.0
```

Properties this update path is required to have:

- **No network at render time.** Rendering reads two committed JSON files. The generator is a
  developer command; nothing in the server calls it.
- **Nothing regenerates silently.** Tokens change when a person runs the command and commits
  the diff.
- **Pinned and verified.** The version is pinned in the generator; the npm tarball's
  `integrity` hash is checked before a byte of it is trusted, and recorded in the output.
- **Reproducible.** The output carries no timestamp, so regenerating at the same pin is
  byte-identical — `--check` is therefore meaningful in a gate.
- **Diff-friendly and small.** Only the tokens the authored file actually aliases are
  resolved: 255 KB upstream becomes ~16 KB committed.
- **Gated.** Bump the pin and `test_design.py` re-derives every contrast number. An upstream
  change that hurts legibility fails.

After a bump, re-run the artifact suite and look at a rendered diagram in both modes. The
gates catch contrast; they do not catch "this looks wrong".

## Licensing

`@carbon/themes` is Apache-2.0, Copyright IBM Corp. `themes/carbon.tokens.json` is a derived
work: values extracted from the pinned release's DTCG source and resolved from Carbon's alias
chains into concrete sRGB. No Carbon code, CSS or component is redistributed. Upstream ships
no `NOTICE` file (checked at v11.80.0), so there is none to propagate. Full text is at
`licenses/carbon-LICENSE.txt`; attribution and the list of modifications are in `NOTICE`.

This project is not affiliated with or endorsed by IBM.
