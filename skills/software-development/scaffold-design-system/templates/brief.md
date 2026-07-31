# Design Brief

## Brand voice

<!-- Replace this with your brand's voice description. One paragraph. -->

Clean, confident, minimal. Prefer clarity over cleverness. Technical without being cold. The interface should feel like a well-made tool, not a marketing page.

## Density

Comfortable. Standard spacing scale (not cramped, not sprawling). Dense where data demands it (tables, lists), spacious where content breathes (hero sections, onboarding).

## Motion

Subtle. Transitions exist to orient the user, not to entertain. 150-200ms for micro-interactions. No bounce, no spring physics. Opacity and transform only (compositor-friendly).

## Canonical references

<!-- 3-5 real products whose visual quality you want to match. -->

1. Linear (app.linear.app) - density, keyboard-first, dark mode done right
2. Vercel dashboard (vercel.com/dashboard) - typography hierarchy, whitespace
3. Stripe Docs (docs.stripe.com) - information density without clutter

## Anti-patterns

- No gradient backgrounds on containers (flat surfaces only)
- No rounded-full on rectangular containers (full radius is for pills and avatars only)
- No raw hex or rgb values in components (tokens only)
- No arbitrary Tailwind values (e.g. `w-[347px]`) when a spacing token fits
- No decorative shadows that don't communicate elevation
- No more than 3 font sizes on a single screen
