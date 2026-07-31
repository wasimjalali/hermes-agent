import { describe, expect, it } from 'vitest'

import { buildLadderRungs, ladderVerdict, mergeDesignChecks } from './mode-page'

describe('buildLadderRungs', () => {
  it('marks missing rungs as not_run, not skip', () => {
    const rungs = buildLadderRungs([
      { name: 'typecheck', status: 'pass', duration_ms: 10 }
    ])
    expect(rungs).toHaveLength(8)
    const byName = Object.fromEntries(rungs.map(r => [r.name, r.status]))
    expect(byName.typecheck).toBe('pass')
    expect(byName.install).toBe('not_run')
    expect(byName.lint).toBe('not_run')
    expect(byName.design_gate).toBe('not_run')
    expect(Object.values(byName).filter(s => s === 'skip')).toHaveLength(0)
  })

  it('preserves an explicit skip from the backend', () => {
    const rungs = buildLadderRungs([
      { name: 'install', status: 'skip', output: 'no install step' },
      { name: 'typecheck', status: 'pass' }
    ])
    expect(rungs.find(r => r.name === 'install')?.status).toBe('skip')
    expect(rungs.find(r => r.name === 'typecheck')?.status).toBe('pass')
    expect(rungs.find(r => r.name === 'lint')?.status).toBe('not_run')
  })
})

describe('ladderVerdict', () => {
  it('never says PASSED for a partial run', () => {
    // Backend may report passed:true when only the requested rungs ran.
    const verdict = ladderVerdict({
      passed: true,
      results: [{ name: 'typecheck', status: 'pass' }]
    })
    expect(verdict).toBe('PARTIAL')
  })

  it('says PASSED only when every rung has a real status', () => {
    const results = [
      'install',
      'typecheck',
      'lint',
      'fix',
      'guard',
      'build',
      'render',
      'design_gate'
    ].map(name => ({ name, status: 'pass' as const }))
    expect(ladderVerdict({ passed: true, results })).toBe('PASSED')
  })

  it('treats skip as a real status, not incomplete', () => {
    const results = [
      'install',
      'typecheck',
      'lint',
      'fix',
      'guard',
      'build',
      'render',
      'design_gate'
    ].map(name => ({
      name,
      status: (name === 'fix' ? 'skip' : 'pass') as 'pass' | 'skip'
    }))
    expect(ladderVerdict({ passed: true, results })).toBe('PASSED')
  })
})

describe('mergeDesignChecks', () => {
  it('keeps workspace and tokens when checks omit them', () => {
    const prev = {
      workspace: '/tmp/app',
      tokens: {
        status: 'ok',
        tokens: [{ path: 'color.bg', value: '#000' }]
      },
      contrast: { status: 'pass', pairs: [] },
      lint: { status: 'pass', violations: [] },
      visual_diff: null,
      a11y_check: null
    }
    const next = mergeDesignChecks(prev, {
      lint: { status: 'fail', violations: [{ file: 'a.tsx', line: 1, rule: 'x', value: 'y', message: 'z' }] },
      contrast: { status: 'pass', pairs: [] },
      a11y_check: { status: 'pass' },
      visual_diff: { status: 'pass', routes: [] },
      passed: false
    })
    expect(next.workspace).toBe('/tmp/app')
    expect(next.tokens.tokens?.[0]?.path).toBe('color.bg')
    expect(next.lint.status).toBe('fail')
    expect(next.a11y_check?.status).toBe('pass')
  })
})
