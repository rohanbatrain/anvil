# Anvil Console — Design System

The binding contract for everything under `console/`. Any component that
contradicts this document is wrong, not the document.

---

## 0. Brief inference

**Page kind.** An operational control plane, not a landing page. Four surfaces:
an approval inbox where a human decides whether money moves, a cockpit showing
recovered revenue against a control arm, a policy studio where prose becomes
enforceable rules, and an audit viewer that replays a case decision by decision.

**Audience.** Payments engineers evaluating whether this person can be trusted
with a ledger, and — in the fiction of the product — a merchant's finance
operator approving concessions at 10am on a Tuesday.

**The governing constraint.** This interface authorises the movement of other
people's money. It must read as a *precision instrument*, not as a SaaS
dashboard. Every decision below follows from that.

**Explicit override of the installed taste skills.** `minimalist-ui` and
`high-end-visual-design` both mandate macro-whitespace (`py-24` to `py-40`) and
`max-w-4xl` measure. Those rules are written for landing pages and are *wrong
here* — they would push a fourteen-column reconciliation table below the fold
and make an operator scroll to compare two numbers. Density is a feature of an
instrument. What we keep from those skills: the banned-defaults list, the
hairline discipline, colour as a scarce resource, custom easing, and the
double-bezel treatment used sparingly. What we discard: the whitespace scale and
the narrow measure. `design-taste-frontend` is not used at all — it excludes
dashboards by its own first line.

---

## 1. Absolute bans

Violating any of these fails review.

- **Fonts.** No Inter, Roboto, Open Sans, Helvetica, Arial, system-ui default.
- **Icons.** No Lucide, Feather, Font Awesome, Material, Heroicons. Icons are
  hand-authored inline SVG on a 16px grid at 1.5px stroke, or Phosphor.
- **Shadows.** No `shadow-md/lg/xl`. Elevation is communicated by a hairline and
  a background-value step, never by a dark blur.
- **Gradients.** None, except a single ≤4%-opacity radial used as ambient
  ground. No glassmorphism. No neon.
- **Radii.** No `rounded-full` on containers, cards or primary buttons. Pills are
  reserved for status badges alone.
- **Emoji.** Nowhere. Not in copy, not in code, not in alt text.
- **Placeholder content.** No "John Doe", no "Acme Corp", no lorem. Every name,
  amount, VPA-hint and failure code in a mock is drawn from the simulator's own
  realistic generators.
- **Copy clichés.** No "Elevate", "Seamless", "Unleash", "Next-gen",
  "Powerful", "Robust", "Effortlessly". Write like a payments engineer:
  specific, quantified, unexcited.
- **Fake precision.** Never render a lift figure without its confidence
  interval. Never round an insignificant result into a significant-looking one.

---

## 2. Type

Three families, each with exactly one job.

| Role | Family | Why |
|------|--------|-----|
| UI, headings, labels | **Space Grotesk** | A grotesk with genuine character — the slightly mechanical `a`, `g` and `1` read as technical rather than corporate, and it is nothing like the banned defaults. |
| **All numerals**, ids, codes, money | **JetBrains Mono** | Tabular figures are non-negotiable: ₹12,34,567.89 above ₹1,04,999.00 must align on the decimal, or an operator scanning a column cannot compare magnitudes at a glance. |
| Long-form prose (rationale, audit narrative) | **Newsreader** | Model reasoning and audit narrative are *read*, not scanned. A text serif at 15/1.6 signals "this is an argument to evaluate", which is exactly the posture we want from an approver. |

```css
--font-ui:    'Space Grotesk', 'SF Pro Display', system-ui, sans-serif;
--font-mono:  'JetBrains Mono', 'SF Mono', ui-monospace, monospace;
--font-read:  'Newsreader', 'Lyon Text', Georgia, serif;
```

**Every numeric element must set `font-variant-numeric: tabular-nums`.** This is
the single most consequential typographic rule in the system.

Scale — a 4px baseline, tight tracking on display sizes:

| Token | Size / line | Tracking | Use |
|-------|-------------|----------|-----|
| `display` | 44 / 1.05 | −0.03em | The one hero metric per screen |
| `h1` | 28 / 1.15 | −0.02em | Page title |
| `h2` | 20 / 1.25 | −0.015em | Section |
| `h3` | 16 / 1.35 | −0.01em | Card title |
| `body` | 14 / 1.55 | 0 | Default UI |
| `read` | 15 / 1.65 | 0 | Serif prose |
| `small` | 13 / 1.5 | 0 | Secondary |
| `micro` | 11 / 1.4 | +0.10em, uppercase | Column heads, eyebrows |
| `numeric-lg` | 32 / 1.1 | −0.02em, tabular | Money, large |
| `numeric` | 14 / 1.4 | 0, tabular | Money, in tables |

---

## 3. Colour

Colour is a scarce semantic resource. The interface is warm near-monochrome; the
only saturated colour on screen at rest is a status badge or the single accent.

**The accent is molten orange, and it is deliberate.** Razorpay's own product
blue would make this look like a Razorpay screenshot. Anvil is the thing that
gets hammered on — the forge metaphor is the identity, and orange is the one
hue that reads as heat without reading as an error state.

```css
:root {
  --canvas:        #FAF9F7;   /* warm bone */
  --surface:       #FFFFFF;
  --surface-sunken:#F2F0EC;
  --surface-raised:#FFFFFF;
  --hairline:      #E4E0D9;
  --hairline-strong:#CFC9C0;

  --ink:           #14120F;   /* never pure black */
  --ink-muted:     #6E6862;
  --ink-faint:     #9A938B;

  --accent:        #C2410C;
  --accent-hot:    #EA580C;
  --accent-wash:   #FBEDE6;

  --ok:   #1F6B4D;  --ok-wash:   #E7F2EC;
  --warn: #8A5A00;  --warn-wash: #FBF3DB;
  --risk: #A32F2B;  --risk-wash: #FBEAEA;
  --info: #1F5F8F;  --info-wash: #E4F1FB;
}

:root:not([data-theme='light']) { /* system dark */ }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme='light']) {
    --canvas: #0C0B0A;  --surface: #141311;  --surface-sunken: #0F0E0D;
    --surface-raised: #1C1A18;
    --hairline: #2A2724; --hairline-strong: #3A3632;
    --ink: #F0EDE8; --ink-muted: #9C948B; --ink-faint: #6B645D;
    --accent: #F97316; --accent-hot: #FB923C; --accent-wash: #2A1710;
    --ok:   #4ADE9B;  --ok-wash:   #10231A;
    --warn: #FBBF57;  --warn-wash: #241C0C;
    --risk: #F87171;  --risk-wash: #2A1414;
    --info: #7DD3FC;  --info-wash: #0F1E29;
  }
}
:root[data-theme='dark'] { /* same block again, so the toggle wins both ways */ }
```

Semantic mapping is fixed and must never be reassigned:

| Meaning | Token |
|---------|-------|
| Recovered, settled, approved, significant lift | `ok` |
| At risk, awaiting approval, awaiting step-up, insignificant result | `warn` |
| Unrecoverable, denied by policy or authorisation, churned, model-safety event | `risk` |
| Scheduled, in flight, informational | `info` |
| The one thing on screen asking for attention | `accent` |

**Never encode meaning in colour alone.** Every status carries a text label; the
lift chart distinguishes arms by dash pattern and direct label as well as hue.

---

## 4. Layout and density

- **4px baseline.** Every spacing value is a multiple of 4. Card padding 16 or
  20; section gaps 20 or 24; table cells 8 vertical / 12 horizontal.
- **Full-bleed working area.** `max-width: 100%` with 24px gutters. There is no
  centred 4xl measure — the exception is serif prose blocks, which cap at `68ch`
  because that is a legibility limit, not an aesthetic one.
- **Shell.** A fixed 224px left rail (collapsible to 56px), a 52px top bar
  carrying merchant scope, mode badge (`OFFLINE` / `LIVE`) and theme toggle, and
  the working area. The rail is a rail, not a floating island — this is an
  instrument, and chrome that detaches from the frame reads as decoration.
- **Tables are first-class.** Sticky header, hairline row separators only (never
  zebra fill), right-aligned tabular numerals, a monospace id column that
  truncates in the middle (`cse_01M1…90RQ`) with click-to-copy, and per-row
  density that fits ~24 rows on a 900px viewport.
- **Wide content scrolls inside its own container** with `overflow-x: auto`. The
  page body never scrolls horizontally.

---

## 5. Components

**Card.** `background: var(--surface)`, `border: 1px solid var(--hairline)`,
`border-radius: 8px`, no shadow. Header row is `micro` uppercase in `ink-faint`
with an optional right-aligned action.

**Double-bezel — used exactly twice.** The single hero metric on the cockpit,
and the pending-action card at the top of the approval inbox. Outer shell
`padding: 6px`, `background: var(--surface-sunken)`, `border-radius: 12px`,
hairline; inner core `background: var(--surface)`, `border-radius: 6px`
(concentric), its own hairline. Reserving it for two places is what makes it
mean "this is the thing that matters" rather than "this is a card".

**Money.** Always `--font-mono`, tabular, right-aligned in tables. Always
Indian digit grouping (`₹12,34,567.89`). Negative values carry a leading minus
and `--risk`, never parentheses — parentheses are an accountant's convention
that an operator misreads at speed.

**Status badge.** Pill, `micro` type, `{semantic}-wash` background with
`{semantic}` text, no border. This is the *only* pill in the system.

**Button.** Radius 6px. Primary: `--ink` fill, `--surface` text; hover shifts
one step; `:active` applies `scale(0.98)`. Destructive uses `--risk` and always
requires a typed confirmation when it moves money. Secondary is a hairline
button with a transparent fill.

**Approve / Reject.** Never adjacent same-weight buttons — an operator clicking
by muscle memory must not be able to approve a ₹40,000 concession by accident.
Approve is primary; Reject is a hairline button; both are separated by 16px and
Approve is disabled until the rationale panel has been scrolled into view.

**Diff view** (policy studio, replay). Two-column, monospace, `ok-wash` for
additions and `risk-wash` for removals, with an immutable-rule marker that
cannot be diffed away.

**Empty states** state what would appear here and why it is empty — "No actions
awaiting approval. 14 executed autonomously in the last hour, all within
policy." — never an illustration and never a shrug.

---

## 6. Motion

Restraint is the whole brief. Motion exists to explain a state change, never to
decorate.

- Easing is always `cubic-bezier(0.32, 0.72, 0, 1)`. Never `linear`, never
  `ease-in-out`.
- Durations: 120ms for hover and press, 200ms for panel and row transitions,
  400ms for a chart's first paint. Nothing exceeds 400ms.
- **A number that changes animates its transition** — a recovered total ticking
  up is the one moment of delight the product earns, because it is the product
  working.
- List entry staggers at 30ms per row, capped at 8 rows of stagger; beyond that
  everything lands together, because a 200-row table cascading is a distraction.
- Animate `transform` and `opacity` only. `backdrop-blur` only on fixed
  elements. Respect `prefers-reduced-motion: reduce` by dropping to opacity-only.

---

## 7. Accessibility

- Body text ≥ 4.5:1 against its surface in both themes; `micro` labels ≥ 4.5:1
  (they carry column meaning, so the large-text exemption does not apply).
- Every interactive element has a visible `:focus-visible` ring: 2px `--accent`
  at 2px offset. Never `outline: none` without a replacement.
- The approval queue is fully keyboard-operable: `j`/`k` to move, `Enter` to
  open, `a`/`r` to approve/reject *with a confirmation step*, `?` for the map.
- Live regions announce arriving approvals and completed recoveries politely.
- Charts carry an accessible data table behind a disclosure, so the evidence is
  readable without seeing colour.

---

## 8. Pre-flight

Before any console screen is considered done:

- [ ] No banned font, icon set, shadow, gradient or pill-container.
- [ ] Every numeral is monospace and `tabular-nums`.
- [ ] Every money value is Indian-grouped with its currency symbol.
- [ ] Light and dark both defined on bare `:root`, and the toggle wins in both.
- [ ] No status conveyed by colour alone.
- [ ] Every lift figure shows its confidence interval and says plainly when it is
      not significant.
- [ ] Focus rings visible on every interactive element.
- [ ] Wide tables scroll inside their container; the body does not scroll sideways.
- [ ] `prefers-reduced-motion` honoured.
- [ ] Every string of mock data came from the simulator's generators.
