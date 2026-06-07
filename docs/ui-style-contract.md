# GoCareers — UI/UX Style Contract

**This is the single source of truth for UI. Before building ANY new UI, read this and reuse what exists. Add a new pattern ONLY if none here fits, and document it here when you do.**

The #1 cause of UI inconsistency is AI creating one-off components. Don't. Reuse first.

---

## 1. Brand color vs Status color — NEVER mix these

These serve different purposes and must stay separate:

| Use | Where | How |
|-----|-------|-----|
| **Brand color** | Navigation, primary actions, active states, focus rings | CSS vars `--brand-*` (set by `PlatformConfig.color_theme`). Use classes `.btn-primary`, `.brand-link`, `.brand-nav`, `.hero-gradient` |
| **Status color** | Success / warning / error / info / live state | Hardcoded Tailwind. NEVER use brand vars for status. |

**Status color palette (fixed — do not theme these):**

| Status | Background | Text | Dot/accent |
|--------|-----------|------|-----------|
| Success / Live / Active | `bg-emerald-50` | `text-emerald-700` | `bg-emerald-500` |
| Info / Applied | `bg-blue-50` | `text-blue-700` | `bg-blue-500` |
| In-progress / Interview | `bg-violet-50` | `text-violet-700` | `bg-violet-500` |
| Warning / Pending / Bench | `bg-amber-50` | `text-amber-700` | `bg-amber-500` |
| Error / Failed / Rejected | `bg-red-50` | `text-red-700` | `bg-red-500` |
| Neutral / Closed / Inactive | `bg-gray-100` | `text-gray-600` | `bg-gray-400` |

> Even if the brand theme is "emerald", a *success* badge still uses the fixed emerald-50/700 status classes — never `--brand-*`. This keeps "this is success" distinct from "this is the brand."

---

## 2. Component classes — reuse these exact patterns

### Cards
```html
<div class="bg-white rounded-xl border border-gray-200 shadow-sm">...</div>
```
- KPI cards: add `p-4 hover:shadow-md hover:border-{color}-200 transition group`
- Section cards: header is `px-5 py-3 border-b border-gray-100`, body is `p-5`

### Buttons
| Type | Classes |
|------|---------|
| Primary | `px-4 py-2 rounded-lg text-sm font-semibold text-white bg-blue-600 hover:bg-blue-700 shadow-sm transition` |
| Secondary | `px-4 py-2 rounded-lg text-sm font-semibold text-gray-600 bg-white border border-gray-200 hover:bg-gray-50 transition` |
| Danger | `px-4 py-2 rounded-lg text-sm font-bold text-red-600 bg-red-50 border border-red-200 hover:bg-red-100 transition` |
| Themed primary | `.btn-primary` (follows brand color) |

### Badges / pills
```html
<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold {status classes from §1}">
  <span class="w-1.5 h-1.5 rounded-full {dot}"></span> Label
</span>
```

### Avatars
```html
<div class="h-10 w-10 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-white flex items-center justify-center font-bold shadow-sm">
  {{ first|first|upper }}{{ last|first|upper }}
</div>
```

### Tables
```html
<table class="w-full text-sm">
  <thead>
    <tr class="text-left text-[10px] font-bold text-gray-400 uppercase tracking-wider border-b border-gray-100 bg-gray-50">
  <tbody class="divide-y divide-gray-50">
    <tr class="hover:bg-gray-50 transition">
```

### Breadcrumbs
```html
<nav class="flex items-center gap-2 text-sm text-gray-500">
  <a href="..." class="hover:text-gray-700 transition">Parent</a>
  <svg class="w-3.5 h-3.5 text-gray-300">...chevron...</svg>
  <span class="text-gray-900 font-medium">Current</span>
</nav>
```

---

## 3. Long text overflow (#3) — always handle it

Titles, company names, URLs, role names CAN be long. Required:

- **Single-line cells**: `truncate max-w-[200px]` (or appropriate width)
- **Card titles**: `line-clamp-2`
- **Wide tables**: wrap in `<div class="overflow-x-auto">`
- **Long values with detail**: truncate + show full on the detail page, OR `title="{{ full }}"` tooltip
- Never let text push layout wider than its container.

---

## 4. Empty / Loading / Error / No-permission states (#7)

Every list/data view needs all four. Use these patterns:

### Empty
```html
<div class="text-center py-12">
  <svg class="w-10 h-10 text-gray-200 mx-auto mb-2">...</svg>
  <p class="text-sm font-medium text-gray-500">No {items} yet</p>
  <p class="text-xs text-gray-400 mt-1">{helpful next step}</p>
</div>
```

### All-clear (success empty)
```html
<svg class="text-emerald-300">...check...</svg>
<p class="text-emerald-600 font-medium">All clear!</p>
```

### Loading (HTMX)
```html
<div class="htmx-indicator flex justify-center py-3">
  <div class="animate-spin rounded-full h-8 w-8 border-2 border-indigo-200 border-t-indigo-600"></div>
</div>
```

### No permission
Gate at the view (`UserPassesTestMixin`) AND hide the trigger in template (`{% if is_admin %}`).

---

## 5. Destructive actions (#8) — always confirm

Delete / cleanup / force-reclassify / bulk / archive / purge MUST:
1. Use a danger-styled button (red)
2. Confirm via Alpine modal (preferred) or `onclick="return confirm(...)"`
3. State the impact clearly ("permanently delete X and all submissions, drafts...")
4. Be POST, never GET

Reuse the delete-modal pattern from `templates/users/employee_edit.html` (`x-data="{ showDeleteModal: false }"`).

---

## 6. Long-running jobs (#9) — guard duplicate clicks

Harvest / classify / backfill / sync buttons MUST:
1. Disable on click + show spinner (`.generate-btn` pattern in `consultant_detail.html`)
2. Show running state (poll progress where available)
3. Expose last-run result

---

## 7. Admin-only UI (#5) — gate twice

Admin/superuser-only controls (deploy footer, engine config, destructive ops, incidents):
```django
{% if request.user.is_superuser or request.user.role == 'ADMIN' %}...{% endif %}
```
Gate in BOTH the view (`test_func`) and the template. Never rely on template-only.

---

## 8. HTMX partials (#6) — same classes as full pages

An HTMX partial (`_*.html`) renders standalone but MUST use the exact same card/table/button/badge classes from §2. Add `{% load humanize %}` at the top of each partial since it renders independently.

---

## 9. Filters preserve state (#10)

Search + tabs + pagination + page-size + filters must not wipe each other. Carry existing params forward:
```django
<a href="?{% if pagination_query %}{{ pagination_query }}&{% endif %}page={{ n }}">
```
HTMX search must `hx-include` the other filter inputs.

---

## 10. Forms (#12) — consistent structure

- Label: `block text-[10px] font-bold text-gray-500 mb-1 uppercase tracking-wider`
- Input: `w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition`
- Error: `text-red-500 text-xs mt-1`
- Help: `text-[10px] text-gray-400 mt-1`
- Required marker: `<span class="text-red-400">*</span>`
- Save/Cancel: Cancel left (secondary), Save right (primary), in a sticky bar for long forms.

---

## 11. Accessibility (#14) baseline

- Every icon-only button needs `title="..."` or `aria-label`
- Focus rings: keep `focus:ring-2` (don't remove outlines)
- Modals: trap focus, close on Escape (`@keydown.escape.window`)
- Tables: real `<th>` headers

---

## 12. Theme safety (#13) — handled

`base.html` uses `{% else %}` to fall back to indigo for any invalid `color_theme`. Don't add a theme without a complete `--brand-*` var set (8 vars).

---

## Adding a new pattern

If nothing here fits:
1. Build it consistent with the spirit above (same radius `rounded-xl`, same border `border-gray-200`, same spacing scale).
2. **Document it in this file** in the same session.
3. Reuse it next time instead of making another variant.
