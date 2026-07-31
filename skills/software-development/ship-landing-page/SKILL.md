---
name: ship-landing-page
description: "Brief to shipped landing page, ending green on the ladder."
version: 1.0.0
author: Burooj
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [landing-page, nextjs, build, burooj, ship]
    related_skills: [scaffold-app, verify-loop, scaffold-design-system]
---

# Ship Landing Page

Use this skill when Build mode takes a brief and delivers a shipped landing page. It is the "never start from a blank page" refusal applied to a concrete deliverable: scaffold the pinned stack, read every visual decision from `burooj.design/`, and do not call it done until the ladder is green.

## When to Use

- User describes a landing page ("a landing page for X", "marketing page for our product").
- Build mode is active and the workspace has no app yet.
- The page needs to be real: content, sections, responsive behavior, not a mock.

## Prerequisites

- Build mode profile (toolset includes `repo_map`, `verify`, `preview`, `sanad_search`, `skills_list`).
- The pinned stack: Next.js, TypeScript, Tailwind, shadcn/ui, Convex.
- A `burooj.design/` artifact with compiled tokens, or the `scaffold-design-system` skill to create one.
- A `burooj.build.json` manifest. The `scaffold-app` skill ships templates.
- `sanad_search` for company standards: brand guidelines, voice, product requirements. Service-gated: works when Sanad is running.

## How to Run

1. Load the `scaffold-app` skill and run its steps if no workspace exists.
2. Gather the brief: audience, message, sections, calls to action.
3. Search Sanad for brand and product material before inventing copy.
4. Read `burooj.design/tokens.json`, `burooj.design/brief.md` and the compiled `burooj.design/build/tokens.css`.
5. Build the page. No raw hex, no arbitrary spacing.
6. Run `verify()` until every rung that can run is green.
7. Run `preview()` and look at the screenshots. Fix what looks wrong.
8. Report the ladder result and the routes rendered.

## Quick Reference

- Pinned stack: Next.js App Router, TypeScript, Tailwind, shadcn/ui, Convex.
- Visual source of truth: `burooj.design/`. Never a raw color or pixel value.
- Done means: `verify()` green, `preview()` shows the routes, no console or network errors.
- Copy: `sanad_search` first, then the brief, then the design system's own voice.

## Procedure

1. **Resolve the brief.** Name the audience, the one message, and the calls to action. A landing page with three messages is a page with none.

2. **Check the workspace.** If no app exists, run the `scaffold-app` skill end to end. If an app exists, run `repo_map()` first to see what is there before touching anything.

3. **Search Sanad for material.** `sanad_search` for the product name, the company, the brand voice. A brief is not enough when the company already wrote the words. Do not invent brand facts that retrieval does not support; flag them instead.

4. **Read the design system.** Open `burooj.design/brief.md`, then `tokens.json`. Every visual decision maps to a token: color to `color.*`, spacing to `spacing.*`, type to `typography.*`, radius to `radius.*`. If a needed token is missing, extend `tokens.json` and run `node burooj.design/compile.mjs`, then `contrast_check` on the new pairs.

5. **Scaffold the sections.** Hero, problem, solution, proof, pricing or plan, FAQ, final CTA, footer. Each section gets one job. Use shadcn/ui components from the registry; compose, do not hand-roll primitives.

6. **Write real copy.** No lorem ipsum. Use the Sanad-backed facts and the brief's voice. Keep sentences short. Every CTA says exactly what happens next.

7. **Verify after every edit.** The `verify-loop` skill covers when to run the full ladder versus a subset. Never let the ladder go red and keep going.

8. **Preview and inspect.** `preview()` captures every declared route. Look at the screenshots, the console errors and the network errors. A route that renders but throws on click is not shipped.

9. **Re-run the design gate.** `design_lint`, `contrast_check`, `a11y_check` and `visual_diff` all must pass or skip with a reason. A failing pair is a token problem, not a "make the text smaller" problem.

10. **Report.** State which rungs ran and what each said. Name the routes you previewed. The user decides what happens next; the ladder only decides "not done yet".

## Pitfalls

- Never start from a blank page. The pinned stack is the refusal, not a preference.
- Never invent visual decisions. Raw hex, arbitrary px, and arbitrary Tailwind values are lint violations, and the gate will fail.
- Never call it done because it compiled. Compiling is rung 6 of 8.
- Never treat a `skip` as a pass. `skip` means "no such step declared", and the ladder output says which and why.
- Never widen `lint-ignore.json` to silence your own code. It exists for the generated scaffold only.
- Do not add a `test` section to the manifest before a real test runner exists.
- Do not ship a landing page whose links point nowhere. Every nav link and CTA needs a target route.

## Verification

- `verify()` reports `passed: true` and names every rung.
- `preview()` returns screenshots for every declared route with zero console and network errors.
- `design_lint` reports zero violations on the app code.
- `contrast_check` reports every gated pair meeting its AA threshold.
- The page reads correctly at one mobile breakpoint and one desktop breakpoint.
