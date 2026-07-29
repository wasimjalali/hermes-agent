/**
 * Burooj mode pages inside Hermes desktop shell.
 * Themes/fonts/chrome = Hermes only. See monorepo docs/burooj-shell-ui.md.
 */
import { useEffect, type ReactNode } from 'react'

import { setBuroojMode } from '@/store/burooj-mode'

import { SanadModePage } from './sanad-view'

export { SanadModePage }

export function BuildModePage() {
  useEffect(() => {
    setBuroojMode('build')
  }, [])
  return (
    <ModeChrome>
      <Placeholder
        body="Build will ship apps and sites with a specialist harness. Hermes remains the Agent mode. Same desktop chrome and themes."
        title="Build is next"
      />
    </ModeChrome>
  )
}

export function DesignModePage() {
  useEffect(() => {
    setBuroojMode('design')
  }, [])
  return (
    <ModeChrome>
      <Placeholder
        body="Design will cover product UI and flows. Hermes remains the Agent mode. Same desktop chrome and themes."
        title="Design is next"
      />
    </ModeChrome>
  )
}

function ModeChrome({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-(--ui-bg-primary) text-(--ui-text-primary)">
      {children}
    </div>
  )
}

function Placeholder({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex flex-1 flex-col items-start justify-center gap-2 px-8 py-12">
      <h1 className="text-[1.125rem] font-semibold tracking-tight text-(--ui-text-primary)">{title}</h1>
      <p className="max-w-xl text-[0.875rem] leading-relaxed text-(--ui-text-secondary)">{body}</p>
      <p className="text-[0.75rem] text-(--ui-text-quaternary)">
        Shell chrome, themes and fonts stay Hermes. See docs/burooj-shell-ui.md.
      </p>
    </div>
  )
}
