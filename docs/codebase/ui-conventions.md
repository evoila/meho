# `ui/` — conventions for developers and coding agents

> The rules for working on the operator console. Short on purpose:
> [`ui.md`](./ui.md) is the reference for how the chassis works, this is
> the list of things not to get wrong. Read this first; reach for `ui.md`
> when you need the mechanism.

The console is the server-rendered surface under
`backend/src/meho_backplane/ui/` — Jinja templates, HTMX and Alpine,
styled with Tailwind 4 and DaisyUI 5. **Pages are built on the server.
There is no single-page app here and we are not adding one** — if you
find a decision record that still says otherwise, it is out of date.

## Four rules

**1. Use the shared pieces; do not write new raw markup.** A button, a
badge, an alert or a dialog should come from a template macro. If you are
typing `class="btn btn-sm btn-ghost"` from memory, look for the macro
first.

`_icons.html` is the example that worked: **61 templates import its
`icon()` macro, and the only `<svg>` anywhere in the template tree is the
one inside that macro.** No template hand-rolls an icon.
The rest of the set is being assembled in a shared UI package that the
three MEHO web interfaces will install. Until a piece exists there, copy
the closest existing template
rather than inventing a new shape — and prefer extending a macro over
adding a seventh spelling of the same thing.

Measured on `main`, 2026-11-02: `btn` appears 1219 times across 156
templates in 15 variants, `badge` 900 times in 15 variants. Four of those
variants (`btn-accent`, `btn-secondary`, `btn-active`, `btn-circle`)
appear once or twice each. That is what drift looks like.

**2. No JavaScript inside a template.** No `onclick="..."`, no `<script>`
block carrying logic. Put it in a file under `static/src/app/` and load
it.

This is not style. The console is moving to a Content Security Policy
that blocks scripts written inline, and anything written that way will
stop working when it lands.

**3. evoila colours only.** Do not introduce a colour by writing a hex
value into a template. Use the theme. If the colour you need does not
exist, that is a decision for the team, not something to route around in
one page.

**4. One light theme.** No dark mode, no second palette, no theme
switcher.

**This is the target, not the current state.** The console ships both
themes today: `base.html` sets `data-theme="meho-dark"` and
`static/src/app/theme.js` switches between `meho-dark` and `meho-light`.
Removing them is its own change. Until it lands, the rule binds *new*
work — do not add a third theme, a new palette, or a second switcher, and
do not write a style that only resolves in one of the two.

## Two things that cost an afternoon

### The dev server can serve new templates against old Python

**If you started it without `--reload`.** The documented commands in
`dev/devserver.py` and [`ui.md`](./ui.md) do pass it, and with it this
does not happen — but reload thrashes when new files appear under
`dev/`, so it does get turned off.

Without it: Jinja re-reads a template from disk on every render, while
Python modules are imported once, at startup. Switch branches while the
server is running and you get the new templates against the old Python.

What it looks like: a 500 naming a variable the template expects, on a
branch that reads correctly — because the branch *is* correct. Restart
the server. **A 500 naming a template variable right after a branch
change is almost always a stale process, not broken code.**

A second, related failure: `dev.db` drifting behind `main`. The symptom
is an `OperationalError` naming a column that exists in the models and
not in the database. `alembic upgrade head` is always the fix.

### Six DaisyUI classes compile to nothing

DaisyUI 5 removed these. They produce **zero CSS rules** — verified
against the built `static/dist/tailwind.css`, where each yields no rule
at all while a live class such as `btn-ghost` yields four:

| Class | Uses on `main` (2026-11-02) |
|---|---|
| `input-bordered` | 138 across 56 templates |
| `form-control` | 96 across 39 |
| `label-text` | 98 across 39 |
| `select-bordered` | 47 across 32 |
| `textarea-bordered` | 34 across 28 |
| `label-text-alt` | 14 across 4 |
| **Total** | **427 across 65 distinct templates** |

Count `label-text` and `label-text-alt` separately — a naive
`grep label-text` matches both and overstates the first by the second.

They spread by copying an existing template, which is why they are listed
here rather than left to be discovered.

The three `-bordered` classes are straight deletions — DaisyUI 5 draws
those borders by default. For the other three, the shape settled by the
earlier migration rounds is:

```html
<label class="flex flex-col gap-1">
  <span class="text-sm font-medium">Name</span>
  <input name="name" class="input input-sm w-full" />
  <span class="text-xs opacity-60">Help text.</span>
  <span class="text-xs text-error">Error text.</span>
</label>
```

Two details that are easy to get wrong:

- Put `w-full` on the control. Every form migrated so far does — see
  `agents/_agent_form_fields.html`, `agents/grants/_grant_form_fields.html`
  and `connectors/_target_form_fields.html` — and a field without it does
  not fill its column.
- A checkbox row stays inline. `.label` is **still live** in v5 — it sets
  `display:inline-flex` and puts the box beside its text — so do not wrap
  one in the column layout above.

One trap for whoever clears the remainder:
`tests/test_ui_corpus_collections_table.py:619-620` asserts the dead
classes are *present*. That test changes in the same pull request, or CI
fails.

## Before opening a pull request

- A visible change carries before/after screenshots.
- A pull request that only moves code must not change how a page looks.
  One that does change the look says so in its title.
- A bug fix ships with a test that fails before and passes after.
- Accessibility is not a follow-up: real `<button>` and `<a>`, a label on
  every field, a title on every dialog, visible focus, and keyboard
  access to everything the mouse can reach.

## Where the rest lives

| | |
|---|---|
| [`ui.md`](./ui.md) | How the chassis works — module layout, build pipeline, session storage, login flow, CSRF |
| [`ui-agent-run-console.md`](./ui-agent-run-console.md) | The agent-run surface |
| [`ui-keycloak-console.md`](./ui-keycloak-console.md) | The Keycloak surface |
