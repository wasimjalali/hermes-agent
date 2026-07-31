---
name: scaffold-app
description: "Use when scaffolding a new app in Build mode. Next.js + Tailwind + shadcn + Convex + design system."
version: 1.0.0
author: Burooj
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [scaffold, nextjs, build, burooj, app]
    related_skills: [scaffold-design-system]
---

# Scaffold App

Use this skill when Build mode needs to create a new application from scratch. This is the "never start from a blank page" refusal made concrete.

## When to use

- User says "build me an app" or "create a new project."
- Build mode is active and no workspace/project exists yet.
- User wants the pinned stack (Next.js, TypeScript, Tailwind, shadcn/ui, Convex).

## The pinned stack

| Layer | Choice | Why |
|-------|--------|-----|
| Framework | Next.js (App Router) | SSR, routing, API routes |
| Language | TypeScript | Type safety, IDE support |
| Styling | Tailwind CSS | Utility-first, token-driven |
| Components | shadcn/ui | Copy-paste, customizable, token-compatible |
| Database | Convex | Real-time, hosted, TypeScript-native |
| Design system | burooj.design/ | DTCG tokens compiled to CSS + Tailwind config |

## Steps

1. **Create the workspace directory**

   ```bash
   mkdir -p ~/Burooj/workspaces/<project-name>
   cd ~/Burooj/workspaces/<project-name>
   ```

2. **Scaffold Next.js**

   ```bash
   npx create-next-app@latest . --typescript --tailwind --app --src-dir --eslint --no-import-alias --use-npm
   ```

   Flags: TypeScript on, Tailwind on, App Router on, src/ directory on, ESLint on, npm as package manager.

3. **Init shadcn/ui**

   ```bash
   npx shadcn@latest init --defaults
   ```

4. **Add Convex**

   ```bash
   npm install convex
   npx convex init
   ```

5. **Scaffold burooj.design/**

   Use the `scaffold-design-system` skill to create the design tokens directory. Copy templates from that skill, then compile:

   ```bash
   node burooj.design/compile.mjs
   ```

6. **Write burooj.build.json**

   Copy `templates/burooj.build.json` into the workspace root.

   It deliberately has **no `test` section**. A fresh `create-next-app` has no `test` script, so declaring one makes `npm test` exit non-zero and the guard rung fail on a scaffold that is in fact fine. Switch to `templates/burooj.build.with-tests.json` the moment you add a real test runner.

7. **Copy the design-lint exemptions**

   Copy `templates/lint-ignore.json` to `burooj.design/lint-ignore.json`.

   The stock `create-next-app` homepage ships `bg-[#383838]`, `gap-[32px]` and raw hex, so without this the design gate fails on an untouched scaffold. **Delete the `src/app/page.tsx` entry as soon as you replace that page with real content**, or the gate stops watching your actual homepage.

8. **Install the check dependencies**

   ```bash
   npm ci && npm i -D axe-core
   ```

   `axe-core` powers the a11y rung. Without it that check skips rather than fails, so the gate would pass while checking less than you think.

9. **Run verify**

   ```bash
   # From the agent, call:
   verify()
   ```

   Run the whole ladder, not a subset. On a fresh scaffold expect: `install`, `typecheck`, `lint`, `build` and `render` to pass; `fix` and `guard` to **skip** (no test section yet); `design_gate` to pass.

   A `skip` means the project declares no such step. An `error` means a check could not run, which is never a pass. Investigate any `error` before writing application code.

## After scaffolding

- Edit `burooj.design/tokens.json` for the brand palette.
- Edit `burooj.design/brief.md` for voice and constraints.
- Recompile: `node burooj.design/compile.mjs`
- Start building features. Run `verify` after every edit.

## Pitfalls

- Never use `yarn`, `pnpm`, or `bun`. npm only.
- Never skip the verify step. A scaffold that doesn't pass verify is broken.
- Never add raw color values. Everything goes through burooj.design/ tokens.
- The scaffold creates `src/app/` (App Router). Do not mix in Pages Router.
- Convex schema goes in `convex/schema.ts`. Do not put DB logic in API routes.
- Do not install style-dictionary as a dependency. The compiler is self-contained.
- Do not add a `test` section to the manifest before there is a test runner. An empty `guard` list means "run the whole suite", which fails when there is no suite.
- Do not widen `lint-ignore.json` to silence a failure you have not read. It exists for the generated scaffold, not for your own code.
- Do not run `visual_diff` with `update_baselines` to clear a red gate. Baselines are the record of what shipped; only update them for a change you intended.
