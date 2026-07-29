/**
 * Full-page shell for non-Agent Burooj modes (Sanad / Build / Design).
 * Uses Hermes tokens only — no separate brand palette.
 */
import { useStore } from '@nanostores/react'
import { useEffect, type ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import { $buroojMode, type BuroojModeId, buroojModeOption, setBuroojMode } from '@/store/burooj-mode'

const SANAD_DEFAULT_URL = 'http://127.0.0.1:8787/'

function sanadBaseUrl(): string {
  try {
    return (localStorage.getItem('burooj.sanadBaseUrl') || SANAD_DEFAULT_URL).replace(/\/?$/, '/')
  } catch {
    return SANAD_DEFAULT_URL
  }
}

export function SanadModePage() {
  useEffect(() => {
    setBuroojMode('sanad')
  }, [])

  const url = sanadBaseUrl()

  return (
    <ModeChrome modeId="sanad">
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary) px-4 py-2">
          <div className="min-w-0">
            <p className="text-[0.8125rem] font-medium text-(--ui-text-primary)">Sanad</p>
            <p className="truncate text-[0.75rem] text-(--ui-text-quaternary)">
              Company knowledge · source-backed answers · {url}
            </p>
          </div>
          <Button
            onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}
            size="sm"
            type="button"
            variant="secondary"
          >
            Open in browser
          </Button>
        </div>
        <iframe
          className="min-h-0 w-full flex-1 border-0 bg-(--ui-bg-primary)"
          src={url}
          title="Sanad knowledge"
        />
      </div>
    </ModeChrome>
  )
}

export function BuildModePage() {
  useEffect(() => {
    setBuroojMode('build')
  }, [])
  return (
    <ModeChrome modeId="build">
      <Placeholder
        body="Build will ship apps and sites with a specialist harness. Hermes remains the Agent mode."
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
    <ModeChrome modeId="design">
      <Placeholder
        body="Design will cover product UI and flows. Hermes remains the Agent mode."
        title="Design is next"
      />
    </ModeChrome>
  )
}

function ModeChrome({ modeId, children }: { modeId: BuroojModeId; children: ReactNode }) {
  const mode = useStore($buroojMode)
  const option = buroojModeOption(modeId)
  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-(--ui-bg-primary) text-(--ui-text-primary)">
      <div className="sr-only">
        Mode {option.label}
        {mode !== modeId ? ' (syncing)' : ''}
      </div>
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
