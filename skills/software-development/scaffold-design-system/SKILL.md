---
name: scaffold-design-system
description: "Use when scaffolding burooj.design/ in a workspace. DTCG tokens + shadcn registry + compiler."
version: 1.0.0
author: Burooj
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design-system, tokens, scaffold, burooj, build]
    related_skills: [plan]
---

# Scaffold Design System

Use this skill when creating a `burooj.design/` directory in a workspace. This is the shared artifact that Design mode owns and Build mode reads.

## When to use

- A new workspace needs a design system before Build can ship anything.
- The user says "set up the design system" or "scaffold design tokens."
- Build mode refuses to start because no `burooj.design/` exists.

## What it creates

```
burooj.design/
  tokens.json       DTCG format design tokens (colors, spacing, radius, type, shadow)
  registry.json     shadcn component registry (which components exist)
  brief.md          brand voice, density, motion, anti-patterns
  compile.mjs       Self-contained compiler (Style Dictionary, no deps)
  build/            Compiled output (gitignored)
    tokens.css      CSS custom properties
    tailwind.tokens.js  Tailwind config extension
  .gitignore        Ignores build/
```

## Steps

1. Copy the template files from this skill's `templates/` into `<workspace>/burooj.design/`.
2. Copy `scripts/compile.mjs` into `<workspace>/burooj.design/compile.mjs`.
3. Run the compiler: `node burooj.design/compile.mjs`
4. Verify `burooj.design/build/tokens.css` and `burooj.design/build/tailwind.tokens.js` exist.
5. Tell the user to customize `tokens.json` (palette, spacing scale) and `brief.md` (voice, references) for their brand.

## Scope boundary

`burooj.design/` governs the apps Build ships. It does NOT govern Burooj's own desktop chrome. The Burooj shell stays on Hermes tokens and themes. Two design systems, never merged.

## Pitfalls

- Do not put raw hex values in component code. Always reference tokens.
- Do not edit `build/` by hand. Edit `tokens.json`, recompile.
- Do not merge this with Hermes' own design tokens (different system).
- The compiler is self-contained. Do not add `style-dictionary` to the workspace's package.json.
