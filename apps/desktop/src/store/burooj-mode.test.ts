import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $buroojMode,
  BUROOJ_MODES,
  setBuroojMode,
  type BuroojModeId
} from './burooj-mode'

describe('burooj-mode', () => {
  beforeEach(() => {
    const store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => {
        store.set(k, v)
      },
      removeItem: (k: string) => {
        store.delete(k)
      }
    })
    setBuroojMode('agent')
  })

  it('locks display order Agent Sanad Build Design', () => {
    expect(BUROOJ_MODES.map(m => m.id)).toEqual(['agent', 'sanad', 'build', 'design'])
  })

  it('defaults to agent', () => {
    expect($buroojMode.get()).toBe('agent')
  })

  it('persists mode switches', () => {
    for (const id of ['sanad', 'build', 'design', 'agent'] as BuroojModeId[]) {
      setBuroojMode(id)
      expect($buroojMode.get()).toBe(id)
      expect(localStorage.getItem('burooj.mode')).toBe(id)
    }
  })
})
