# ADR-0011: The console is served by the API with no build step

- **Status:** Accepted
- **Date:** 2026-09-03
- **Supersedes:** an unrecorded earlier decision to build the console in Next.js

## Context

The console is what makes the system assessable. A reviewer with fifteen minutes
will click before they read.

The default modern choice is a separate Next.js application. It looks more
substantial in a repository listing. It also means a second process, a Node
toolchain, a build step, a second port and CORS — five things that can each be
broken on a machine that is not mine, in front of someone whose time I do not
control.

A Next.js scaffold with Tailwind was in fact set up first, and then removed.

## Decision

The console is a **single self-contained HTML page served by FastAPI**. No build
step, no bundler, no second process, no CORS. `uvicorn` and it is up.

The API needs **no database and no credentials**: it drives the seeded simulator
in process, so a clean checkout boots straight to a working interface.

## Consequences

`make console` works from a fresh clone with nothing installed but Python.

There is one deployable and one place a failure can be.

The design system in `docs/reference/design-system.md` is implemented in hand-written
CSS rather than Tailwind, which is more verbose and gave complete control over
the theme tokens — the page is genuinely correct in light, dark and system
themes, which is easier to get right when nothing is generating classes.

The cost is real: no component model, no type checking in the front end, and a
single large file. At the size of this console — seven screens, no shared state
beyond a fetch — that is an acceptable trade. It would not be at three times the
size, and the honest answer to "would you do this at work?" is *no, and here is
the threshold at which I would switch*.

## Alternatives considered

**Next.js with the design system in Tailwind.** Rejected on demo risk, as above.
The scaffold was built and deleted rather than argued about hypothetically.

**A static export served by any web server.** Rejected because the interesting
screens need a live API — a paused LangGraph thread cannot be pre-rendered.

**Streamlit or Gradio.** Rejected because the design system exists and matters
here: this is an operator console for money, and the constraints in
`design-system.md` about tabular numerals, semantic colour and never showing a
lift without its interval are not expressible in a widget toolkit.
