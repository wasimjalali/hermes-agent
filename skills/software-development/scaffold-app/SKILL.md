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

   Copy the template from this skill's `templates/burooj.build.json` into the workspace root. Adjust if the project name differs.

7. **Install dependencies**

   ```bash
   npm ci
   ```

8. **Run verify (rungs 1-5)**

   ```bash
   # From the agent, call:
   verify(rungs=["typecheck", "lint", "guard", "build"])
   ```

   All must pass on the fresh scaffold before writing any application code.

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
