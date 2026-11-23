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

**Do not convert an icon-only button yet — `icon=` is broken in meho-ui
0.6.0.** This page said the opposite until it was checked by rendering the
macro rather than reading its signature. What it actually emits:

```jinja
{{ button("", variant="quiet", icon="trash") }}
<button type="button" class="btn btn-ghost btn-sm btn-square" aria-label=""></button>
```

Two faults in one line. The icon is never drawn: only the
`unavailable_reason` branch mentions a `caller()`, so the `<a>` and
`<button>` branches set `btn-square` and emit nothing inside. And the
`aria-label` is guarded by `{% if icon and not label %}`, so it is rendered
only when the label is empty — which is exactly when it has nothing to say.
A converted icon button is therefore an empty square announced as "button",
which is the regression the old wording claimed to prevent.

This blocks **25 icon-only buttons**. It needs a fix in the component set
(P2.1's own rule: open an issue against `meho-ui`, do not reach for raw
markup), not a workaround here.

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

## Three cases the component set did not cover

These are the reason this page exists. Each needed a decision **before** the
batch that hit it, because none could be resolved by the person doing the
conversion without changing how a page looks.

**Two are settled.** The UI lead chose to widen the enum in both, over a
silent restyle; `meho-ui` 0.7.0 ships them (meho-ui#17, 2026-11-23). They are
kept here with their reasoning because the reasoning is the point, not the
open question.

### 1. The bare `btn` and bare `badge` — 67 elements — **RESOLVED: `default`**

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

**Resolved** by the first option: `meho-ui` 0.7.0 adds a `default` variant to
both macros, compiling to no modifier at all. It widens the enum P2.1
narrowed, which was the trade accepted rather than silently restyling 67
elements.

Of the 67, far fewer were actually reachable: most bare `btn` elements also
carry `hx-*`, and several bare badges take a dynamic class. The decision
unblocked them in principle; the other blockers on this page still apply.

### 2. `badge-ghost badge-outline` together — 13 elements — **RESOLVED: `quiet-outline`**

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

**Resolved** by a named variant for the pair, on the `danger-quiet`
precedent: `meho-ui` 0.7.0 adds `quiet-outline`, emitting
`badge-ghost badge-outline`.

### 3. The cut variants — 4 elements

`badge-soft` ×2, `badge-secondary` ×1, `badge-lg` ×1. P2.1 cut all three.

Per P2.1's own rule, a template needing a cut variant **opens an issue
against the component set** rather than reaching for raw markup. Four
elements is small enough that converting them to the nearest surviving
variant is likely right, but it is still a visible change and belongs in a
batch's screenshot review.

## Two rules the first batch settled

Neither needed a decision from the UI lead: both are facts about the macros,
established by rendering them rather than by reading them.

### Layout classes ride along in `class=`

`badge()` and `button()` both take a `class=""` passthrough, appended after
the variant and size classes. So a layout or typography class on the old
markup is not a reason to leave an element raw:

```jinja
<span class="badge badge-outline badge-xs font-mono">{{ x }}</span>
{{ badge(x, variant="outline", size="xs", class="font-mono") }}
```

This covers `font-mono`, `shrink-0`, `ml-1` and `gap-1` — 20 of the 128
elements converted so far, so it is the common case, not the exception.

### A sizeless original is `size="md"`, never the default

The macro's own default is `size="sm"`, which **emits `badge-sm`**. Old markup
carrying no size class at all is DaisyUI's default size, which the macro emits
only for `size="md"`:

| Old classes | Call | Emits |
|---|---|---|
| `badge badge-warning badge-sm` | `badge(…, variant="warning")` | `badge badge-warning badge-sm` |
| `badge badge-warning` | `badge(…, variant="warning", size="md")` | `badge badge-warning` |

Passing nothing for a sizeless original silently shrinks it. This is the same
shape of trap as the bare-`btn` case above, one axis over.

## One behaviour the macro changes: an empty label

`badge("")` renders **nothing**. The old markup rendered an empty pill:

```jinja
<span class="badge badge-ghost badge-xs"></span>   {# old: an empty pill #}
{{ badge("", variant="quiet", size="xs") }}        {# new: no output #}
```

This applies to every converted element, not to any one batch. It is an
improvement — an empty pill is a rendering artefact, not a label — and no
converted site can currently reach it, but it is a real difference and a
batch that converts a badge over a nullable value should check that site.

## How a batch is verified

Per element, not per eyeball. For every element the converter would change,
the macro is rendered with the planned arguments and compared **node-wise**
against the original markup — tag, attributes (classes sorted), text — so
attribute order and whitespace cannot hide a difference. Then each changed
template is rendered whole, before and after, through the app's real Jinja
environment and compared the same way.

So far: **128 badges and 32 buttons across 62 templates**, every one
identical. That is 128 of 340 badges and 32 of 406 buttons.

It earns its keep. On the buttons it caught two converter faults that read
fine in a diff: a bare `data-classify-request` dropped from two submit
buttons, and a `&larr;` label escaped into the visible text `&larr; Back to
runs`. Neither is visible in a side-by-side of the Jinja source.

The stand-in context such a sweep uses must be chosen with care. A generic
"everything is truthy" object makes both arms of an `{% if %}` render, which
is what you want, but it also leaves nested lookups undefined — and an
undefined label is empty, which the macro drops (above). That shows up as a
whole-template difference which is an artefact of the harness, not of the
conversion. Four files tripped it; all four were identical once rendered with
a realistic row. Re-render before believing a difference.

## A fourth blocker the first batches did not see

**26 badges and 9 buttons choose their DaisyUI modifier at render time.**

```jinja
<span class="badge {{ row.status_badge }} badge-sm">{{ row.status }}</span>
```

`row.status_badge` is a view-computed string such as `badge-success`. The
macro takes a variant **name**, not a class, so there is nothing to pass it.
Converting one means changing what the view hands the template — mapping
server-side values onto variant names — which is a code change in the route,
not a template conversion, and belongs in its own ticket.

They are easy to convert *wrongly*. A converter that reads the class
attribute as a token list sees the literal text and emits

```jinja
{{ badge(row.status, variant="default", class="row.status_badge {{ }}") }}
```

which compiles, renders, and is wrong. Five files were written that way
before the node-wise check caught it. Any converter has to refuse a `class`
containing `{{` or `{%` outright.

## Not every family converts mechanically

P2.6 was scoped as "convert the templates". Measured per family, only one of
the four is a mechanical substitution. This is the finding that should shape
the remaining batches.

| Family | In tree | Mechanically convertible | What stops the rest |
|---|---|---|---|
| `badge` | 340 | **166 done** | 32 nested labels, 26 dynamic classes, 5 unmapped |
| `btn` | 406 | **39 done** | 337 unnamed attributes, 9 dynamic classes, 7 nested labels |
| `alert` | 175 | ~7 | 133 of 140 wrap nested markup |
| `modal` | 73 | — | same shape as `alert` |

### Buttons: the attributes are the problem, not the variants

367 of 406 buttons carry `hx-*`. Add `data-*`, `aria-*`, `onclick` and
Alpine's `x-on:` / `@click` and only **32 of 406** are expressible with the
macro's named parameters (`type`, `href`, `target`, `download`, `disabled`).
The rest have to pass everything else through `extra={…}`, and a hundred of
them carry four or more such attributes.

Count bare attributes when measuring this. A regex of the shape
`(\w+)\s*=` sees only valued ones, which silently hides
`data-classify-request`, `disabled` and `open` — it put this figure at 53
until the node-wise check caught two buttons whose bare attribute the
conversion would have dropped.

That conversion does not read better than the markup it replaces:

```jinja
<button type="button" class="btn btn-sm btn-ghost" data-action="enable"
        hx-post="/ui/agents/{{ agent.name | urlencode }}/toggle"
        hx-vals='{"enabled": "true"}' hx-target="body" hx-swap="none">Enable</button>

{{ button("Enable", variant="quiet", extra={
     "data-action": "enable",
     "hx-post": "/ui/agents/" ~ (agent.name | urlencode) ~ "/toggle",
     "hx-vals": '{"enabled": "true"}',
     "hx-target": "body",
     "hx-swap": "none"}) }}
```

It is longer, it moves HTML attributes into a dict literal, and it turns an
interpolated URL into a `~` concatenation. The variant naming is still worth
having; whether it is worth that per call site is a decision for the UI lead,
not for the person doing the conversion. **Convert the 53, leave the 353.**

### Alerts and modals: the macro imposes a structure

`alert(message, …)` takes an escaped **string** and wraps it:

```html
<div class="alert alert-warning" role="status"><div><p>{{ message }}</p></div></div>
```

133 of 140 alerts in the tree hold nested markup — an icon, a `<ul>`, a
button, a `<strong>`. Passing that as `message` needs `|safe` at every call
site, which trades a template that cannot inject markup for one that can, and
it still adds a `<div><p>` wrapper the current markup does not have. `dialog`
takes `body` the same way, so the 73 modals have the same shape.

Neither is a conversion. Both are a redesign of the component or of the call
sites, and they belong in their own ticket rather than inside a batch.

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
