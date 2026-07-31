---
name: verify-loop
description: "Tight edit-then-verify loop. Read rung statuses correctly."
version: 1.0.0
author: Burooj
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [verify, build, burooj, ladder, loop]
    related_skills: [scaffold-app, ship-landing-page]
---

# Verify Loop

Use this skill in Build mode for the tight edit-then-verify cycle. It covers when to run the full ladder versus a rung subset, how to read each status, and how to avoid the trap of treating a `skip` as a pass.

## When to Use

- After any edit, before calling work done.
- When a rung fails and you need to know which one to attack next.
- When the full ladder is too slow for a one-line change.
- When someone says "it works" without a green ladder.

## Prerequisites

- Build mode profile with `verify` available.
- A `burooj.build.json` in the workspace. See `docs/burooj-build-manifest.md` in the monorepo root for the format.
- A dev server port for the render rung, and axe-core installed for the a11y check.

## How to Run

1. Run the full ladder once to learn the workspace: `verify()`.
2. Edit. Run the smallest rung subset that can prove the edit.
3. On green, widen the loop: the rungs the subset skipped.
4. Full ladder before "done".

## Quick Reference

| Status | Meaning | Ladder still passing? |
|--------|---------|----------------------|
| `pass` | the check ran and succeeded | yes |
| `skip` | the project declares no such step | yes |
| `fail` | the check ran and found a problem | no |
| `error` | the check could not run | **no** |

`error` is never a pass. A gate that cannot execute must not report green. `skip` requires a reason, and the output says it: "No 'test' section in burooj.build.json" is a skip; "dev server did not become ready" is an error.

## Procedure

1. **Run the full ladder first.** `verify()` with no `rungs` argument runs all eight in canonical order and stops at the first failure. The first run in a workspace returns a `disclosure` naming the resolved manifest and every command it will run. Read it.

2. **Know the rung order.** install, typecheck, lint, fix, guard, build, render, design_gate. Cheap to expensive, fail fast. A typecheck failure means the build and render rungs never ran, and that is correct: do not pay for a build you know is broken.

3. **Edit, then verify the smallest subset that proves the edit.** A one-line type fix wants `verify(rungs=["typecheck"])`. A layout change wants `verify(rungs=["typecheck", "lint", "render", "design_gate"])` because render and the design gate are what a layout change can break. The subset must cover what the edit touches; anything else can stay for the full run.

4. **Read each status honestly.**

   - `fail` has a hint. The `hint` field names the likely fix: type errors to fix, lint to auto-fix, a broken test to diff, a dev server that compiled but does not serve.
   - `skip` means the project declares no such step. "No 'test' section" is information, not a pass. The fix rung skipping also means the ladder cannot tell a real fix from a no-op: if you are fixing a bug, declare `test.fix`.
   - `error` means the check could not run. Investigate before anything else. An error is a broken harness, not a failed check.

5. **Never treat `skip` as `pass`.** Both leave the ladder green, but they mean different things. Skipped rungs are uncovered ground. Before calling the work done, know which rungs skipped and why. "It passes" with the render rung skipped is "it has not been rendered".

6. **Narrow the failure before fixing.** The output of the failing rung is the diagnostic, not the whole ladder. A typecheck error names the file and line. Fix the named thing, then re-run just that rung, then widen.

7. **Guard against fake green.** If a manifest command is missing, a rung skips. If the manifest is malformed, `verify` refuses to load it with an error. Never "simplify" a manifest to make rungs skip: a manifest that declares nothing is rejected on purpose.

8. **Full ladder before done.** When the subset is green, run `verify()` with no `rungs`. Done means the whole ladder is green, with every `skip` accounted for.

## Pitfalls

- Do not run the full ladder after every keystroke. The subset exists so the loop stays tight.
- Do not run only the cheap rungs because the expensive ones are slow. Slow rungs are where real bugs live.
- Do not fix a `fail` by deleting the step from the manifest. That turns a failure into a skip and reports green while checking less.
- Do not ignore `error`. An unreachable dev server, an undecodable baseline or a malformed token file means you know less than you did, not more.
- Do not treat the design gate's `skip` as approval. `a11y_check` skips when axe-core is not installed; install it. `visual_diff` skips when there is no baseline; the first run saves one.
- Do not stop at the first green subset. The subset proves the edit; the full ladder proves the workspace.

## Verification

- Every rung that can run ran, and the ladder reports `passed: true`.
- Every `skip` has a reason you can state.
- Every `error` was investigated, not papered over.
- The loop was tight: subsets after small edits, full ladder before done.
