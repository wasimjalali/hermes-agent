/**
 * Burooj mode selector — sits ABOVE "New session" in the Hermes sidebar.
 * Order: Agent · Sanad · Build · Design. Uses Hermes Select + tokens only.
 */
import { useStore } from '@nanostores/react'
import { useNavigate } from 'react-router-dom'

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  $buroojMode,
  BUROOJ_MODES,
  type BuroojModeId,
  setBuroojMode
} from '@/store/burooj-mode'

const MODE_PATH: Record<BuroojModeId, string> = {
  agent: '/',
  sanad: '/sanad',
  build: '/build',
  design: '/design'
}

export function BuroojModeSelector() {
  const mode = useStore($buroojMode)
  const navigate = useNavigate()

  return (
    <div className="shrink-0 px-0 pb-1.5 pt-0 [-webkit-app-region:no-drag]">
      <label className="mb-1 block px-2 text-[0.64rem] font-semibold uppercase tracking-[0.12em] text-(--ui-text-quaternary)">
        Mode
      </label>
      <Select
        onValueChange={value => {
          const next = value as BuroojModeId
          setBuroojMode(next)
          navigate(MODE_PATH[next])
        }}
        value={mode}
      >
        <SelectTrigger
          aria-label="Burooj product mode"
          className="h-7 w-full justify-between rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-control-active-background) px-2 text-left text-[0.8125rem] font-medium text-(--ui-text-secondary) shadow-none hover:bg-(--ui-control-hover-background) hover:text-foreground"
          size="sm"
        >
          <SelectValue placeholder="Agent">
            {BUROOJ_MODES.find(m => m.id === mode)?.label ?? 'Agent'}
          </SelectValue>
        </SelectTrigger>
        <SelectContent align="start" className="min-w-[var(--radix-select-trigger-width)]">
          {BUROOJ_MODES.map(option => (
            <SelectItem key={option.id} value={option.id}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
