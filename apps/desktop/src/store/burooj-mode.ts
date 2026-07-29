/**
 * Burooj product mode — Agent · Sanad · Build · Design.
 * Shell chrome and themes stay Hermes; this only switches mode content.
 * See docs/burooj-shell-ui.md in the monorepo root.
 */
import { atom } from 'nanostores'

export type BuroojModeId = 'agent' | 'sanad' | 'build' | 'design'

export interface BuroojModeOption {
  id: BuroojModeId
  label: string
  subtitle: string
}

/** Locked display order. */
export const BUROOJ_MODES: readonly BuroojModeOption[] = [
  { id: 'agent', label: 'Agent', subtitle: 'Hermes coworker' },
  { id: 'sanad', label: 'Sanad', subtitle: 'Company knowledge' },
  { id: 'build', label: 'Build', subtitle: 'Ship apps and sites' },
  { id: 'design', label: 'Design', subtitle: 'Product design' }
] as const

const STORAGE_KEY = 'burooj.mode'

function readStoredMode(): BuroojModeId {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === 'agent' || raw === 'sanad' || raw === 'build' || raw === 'design') {
      return raw
    }
  } catch {
    // ignore
  }
  return 'agent'
}

export const $buroojMode = atom<BuroojModeId>(readStoredMode())

export function setBuroojMode(mode: BuroojModeId): void {
  $buroojMode.set(mode)
  try {
    localStorage.setItem(STORAGE_KEY, mode)
  } catch {
    // ignore
  }
}

export function buroojModeOption(id: BuroojModeId): BuroojModeOption {
  return BUROOJ_MODES.find(m => m.id === id) ?? BUROOJ_MODES[0]
}
