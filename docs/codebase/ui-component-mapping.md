# Old class → component mapping

The table the template conversion is driven by. Agreed **before** the first
batch, because a conversion that re-decides per batch produces two batches
that disagree.

Every number here was measured fresh against `templates/` on `main` on
**2026-11-20**, counting whole class tokens inside `class="…"` attributes
only. The commands are at the end.

They are **not** from `meho-ui`'s `docs/inventory.md`, which is dated
2026-10-28 and has drifted — its dead-v4 counts are all two to five off
against the tree today. Re-measure; do not cite it.

## Scale

| | Combinations in use | Total |
|---|---|---|
| `btn` | 31 distinct | 406 elements |
| `badge` | 36 distinct | 340 elements |

The component API has **5** button variants and **8** badge variants. The gap
between 31 and 5 is mostly orthogonal axes — size, shape and behaviour
multiply out — but not entirely, and the exceptions are the point of this
page.

## Buttons

| Old classes | Component call | Uses |
|---|---|---|
| `btn btn-ghost btn-sm` | `button(…, variant="quiet")` | 103 |
| `btn btn-primary btn-sm` | `button(…, variant="primary")` | 77 |
| `btn btn-ghost btn-xs` | `button(…, variant="quiet", size="xs")` | 66 |
| `btn btn-error btn-sm` | `button(…, variant="danger")` | 25 |
| `btn btn-ghost btn-sm btn-square` | `button(label, variant="quiet", icon=…)` | 24 |
| `btn btn-outline btn-sm` | **see "outline alone" below** | 15 |
| `btn btn-warning btn-sm` | `button(…, variant="warning")` | 13 |
| `btn btn-outline btn-error btn-sm` | `button(…, variant="danger-quiet")` | 8 |
| `btn btn-primary btn-xs` | `button(…, variant="primary", size="xs")` | 7 |

`size="sm"` is the component's default, so it is omitted above rather than
passed.

**An icon-only button becomes `icon=` with a `label`**, not `btn-square`. The
component emits `btn-square` itself when `icon` is set and the label is
empty, and the label becomes the accessible name. Dropping the label here is
how a button ends up announced as "button".

## Badges

| Old classes | Component call | Uses |
|---|---|---|
| `badge badge-ghost badge-sm` | `badge(…, variant="quiet")` | 54 |
| `badge badge-outline badge-sm` | `badge(…, variant="outline")` | 38 |
| `badge badge-ghost badge-xs` | `badge(…, variant="quiet", size="xs")` | 30 |
| `badge badge-outline badge-xs` | `badge(…, variant="outline", size="xs")` | 25 |
| `badge badge-warning badge-sm` | `badge(…, variant="warning")` | 23 |
| `badge badge-success badge-sm` | `badge(…, variant="success")` | 19 |
| `badge badge-error badge-sm` | `badge(…, variant="error")` | 15 |
| `badge badge-info badge-sm` | `badge(…, variant="info")` | 10 |

## Three cases the component set does not cover

These are the reason this page exists. Each needs a decision **before** the
batch that hits it, and none of them can be resolved by the person doing the
conversion without changing how a page looks.

### 1. The bare `btn` and bare `badge` — 67 elements

30 buttons carry a size and no variant (`btn btn-sm`, `btn btn-xs`). 37
badges do the same.

They are **not** the component's defaults. `button()` defaults to `quiet`,
which is `btn-ghost`, and the compiled CSS shows `.btn-ghost` setting
`--btn-bg: #0000` — transparent. A bare `.btn` has a background. Mapping
these to `quiet` would make 30 buttons change appearance, silently, across
the whole console.

Same for `badge()`, which defaults to `neutral`.

**Options:**

- **Add a `default` variant** to both components, emitting a bare `btn` /
  `badge`. Honest, and the conversion becomes mechanical. Costs a release and
  widens the enum P2.1 deliberately narrowed.
- **Map them to the nearest variant and accept the change**, reviewing it as
  an intentional visual difference rather than a regression. Cheaper, but 67
  elements across the console is not a small intentional change.
- **Leave them as raw markup** until decided. Keeps the batch honest, but
  leaves raw `btn` in the tree, which is what P2.6 exists to remove.

**Not decided here.** This needs the UI lead, like P2.1's variant question.

### 2. `badge-ghost badge-outline` together — 13 elements

In `retrieval/_usage_results.html`, `runbooks/_list.html`,
`runbooks/detail.html` and `retrieval/_diagnostics_results.html`.

The component's `variant` is one value, and `quiet` and `outline` are two of
them. The combination is not expressible.

It is also not redundant: the compiled CSS shows `badge-ghost` setting a
border and background from `base-200`, and `badge-outline` setting
`--badge-bg: #0000` with its own border colour. Together they render a third
appearance.

**This is the same shape as P2.1's open question, on an axis P2.1 did not
measure.** That analysis counted style-plus-colour combinations on *buttons*
and found twelve, all identical. It did not check style-plus-style on
*badges*. Thirteen exist.

**Options:** a named variant for the pair (the `danger-quiet` precedent), or
convert these four files to whichever single variant is closest and accept
the change.

### 3. The cut variants — 4 elements

`badge-soft` ×2, `badge-secondary` ×1, `badge-lg` ×1. P2.1 cut all three.

Per P2.1's own rule, a template needing a cut variant **opens an issue
against the component set** rather than reaching for raw markup. Four
elements is small enough that converting them to the nearest surviving
variant is likely right, but it is still a visible change and belongs in a
batch's screenshot review.

## Dead v4 classes — remove on sight

These compile to **zero** CSS rules. Every batch deletes them from the files
it touches.

| Class | Uses / files |
|---|---|
| `input-bordered` | 138 / 55 |
| `label-text` | 104 / 39 |
| `form-control` | 100 / 39 |
| `select-bordered` | 51 / 33 |
| `textarea-bordered` | 35 / 29 |
| `label-text-alt` | 12 / 3 |

They spread by copying an existing template, so leaving them in a file a
batch has already opened means the next person copies them again.

## How the numbers were taken

From `backend/src/meho_backplane/ui/templates` on `main`:

```python
import re, pathlib, collections
combos = collections.Counter()
for p in pathlib.Path('.').rglob('*.html'):
    for m in re.finditer(r'class="([^"]*)"', p.read_text(encoding='utf-8', errors='replace')):
        toks = m.group(1).split()
        if 'btn' in toks:
            combos[tuple(sorted(t[4:] for t in toks if t.startswith('btn-')))] += 1
```

Counting must be scoped to `class="…"`. A bare grep for `badge-` also matches
the event name `meho:approval-badge-poll` and reports a `badge-poll` variant
that does not exist.
