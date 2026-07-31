/**
 * Burooj mode pages inside Hermes desktop shell.
 * Themes/fonts/chrome = Hermes only. See monorepo docs/burooj-shell-ui.md.
 *
 * Build and Design panels render what the gateway backend reports. The tools
 * run in the gateway process; this page is a thin client over the JSON-RPC
 * methods in tui_gateway/methods_burooj.py. A check that has not run yet in
 * the backend reports null and the panel says so, it never invents a result.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import type { GatewayRequester } from '@/app/contrib/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { readDesktopFileDataUrl } from '@/lib/desktop-fs'
import { cn } from '@/lib/utils'
import { setBuroojMode } from '@/store/burooj-mode'
import { $currentCwd } from '@/store/session'

import { SanadModePage } from './sanad-view'

export { SanadModePage }

const WORKSPACE_KEY = 'burooj.workspace'

function rememberedWorkspace(): string {
  try {
    return (localStorage.getItem(WORKSPACE_KEY) || $currentCwd.get() || '').trim()
  } catch {
    return ''
  }
}

function persistWorkspace(path: string) {
  try {
    const trimmed = path.trim()

    if (trimmed) {
      localStorage.setItem(WORKSPACE_KEY, trimmed)
    } else {
      localStorage.removeItem(WORKSPACE_KEY)
    }
  } catch {
    // localStorage unavailable; the workspace just is not remembered.
  }
}

interface Rung {
  name: string
  status: 'pass' | 'fail' | 'skip' | 'error' | 'not_run'
  output?: string
  duration_ms?: number
  hint?: string
}

interface VerifyResult {
  results?: Rung[]
  passed?: boolean
  disclosure?: { workspace?: string; manifest?: string }
  error?: string
}

const ALL_RUNG_NAMES = [
  'install',
  'typecheck',
  'lint',
  'fix',
  'guard',
  'build',
  'render',
  'design_gate'
] as const

/** Fill missing rungs as not_run (never skip). skip means "manifest has no step". */
export function buildLadderRungs(results: Rung[] | null | undefined): Rung[] {
  return ALL_RUNG_NAMES.map(name => {
    const found = results?.find(r => r.name === name)

    return (
      found ?? {
        name,
        status: 'not_run' as const,
        output: 'Not run in this process yet. Run the ladder or verify() in a session.'
      }
    )
  })
}

/**
 * Overall ladder label. A partial run (any not_run) is never PASSED, even if
 * the backend's gate_passed is true for the subset that ran.
 */
export function ladderVerdict(verifyResult: VerifyResult): 'PASSED' | 'NOT PASSED' | 'PARTIAL' {
  const rungs = buildLadderRungs(verifyResult.results)
  if (rungs.some(r => r.status === 'not_run')) {
    return 'PARTIAL'
  }
  return verifyResult.passed === true ? 'PASSED' : 'NOT PASSED'
}

/** Merge burooj.design.checks into existing status; never drop workspace/tokens. */
export function mergeDesignChecks(
  prev: DesignStatus | null,
  checks: Partial<DesignStatus> & { passed?: boolean }
): DesignStatus {
  const base: DesignStatus = prev ?? {
    workspace: '',
    tokens: { status: 'missing' },
    contrast: {},
    lint: {},
    visual_diff: null,
    a11y_check: null
  }

  return {
    ...base,
    workspace: base.workspace || (checks as DesignStatus).workspace || '',
    tokens: base.tokens?.tokens || base.tokens?.status
      ? base.tokens
      : (checks as DesignStatus).tokens ?? base.tokens,
    contrast: checks.contrast ?? base.contrast,
    lint: checks.lint ?? base.lint,
    visual_diff: checks.visual_diff !== undefined ? checks.visual_diff : base.visual_diff,
    a11y_check: checks.a11y_check !== undefined ? checks.a11y_check : base.a11y_check
  }
}

interface BuildStatus {
  workspace: string
  ladder: string[]
  manifest_path: string | null
  manifest: {
    install?: string
    typecheck?: string
    lint?: string
    test_command?: string | null
    test_fix?: string[]
    test_guard?: string[]
    build?: string
    dev_command?: string | null
    dev_port?: number | null
    routes?: string[]
  } | null
  manifest_error?: string | null
  dev_server: {
    running: boolean
    port: number
    base_url: string
    stdout_tail: string[]
    stderr_tail: string[]
  } | null
  last_verify: VerifyResult | null
  last_preview: {
    routes?: { path: string; screenshot: string; console_errors?: string[]; network_errors?: string[]; status?: number }[]
    server_healthy?: boolean
    error?: string
  } | null
  screenshots: { path: string; name: string }[]
}

interface DesignStatus {
  workspace: string
  tokens: { status: string; tokens?: { path: string; value: string }[]; reason?: string; error?: string }
  contrast: {
    status?: string
    pairs?: { foreground: string; background: string; ratio: number; required: number; passed: boolean }[]
    reason?: string
    error?: string
    summary?: string
  }
  lint: {
    status?: string
    violations?: { file: string; line: number; rule: string; value: string; message: string }[]
    reason?: string
    error?: string
    files_scanned?: number
  }
  visual_diff: {
    status?: string
    routes?: { path: string; baseline: string; diff_pct: number; threshold: number; passed: boolean; new_baseline?: boolean; breakpoint?: number; theme?: string; note?: string }[]
    reason?: string
    error?: string
  } | null
  a11y_check: { status?: string; reason?: string; error?: string } | null
}

const RUNG_LABELS: Record<string, string> = {
  install: 'Install',
  typecheck: 'Typecheck',
  lint: 'Lint',
  fix: 'Fix tests',
  guard: 'Guard tests',
  build: 'Build',
  render: 'Render',
  design_gate: 'Design gate'
}

const STATUS_LABEL: Record<Rung['status'], string> = {
  pass: 'Pass',
  fail: 'Fail',
  skip: 'Skip',
  error: 'Error',
  not_run: 'Not run'
}

// Semantic state colors only: real pass/fail/error states from the Hermes
// palette. Never a second design system.
const STATUS_CLASS: Record<Rung['status'], string> = {
  pass: 'bg-(--ui-green) text-(--ui-bg-elevated)',
  fail: 'bg-(--ui-red) text-(--ui-bg-elevated)',
  skip: 'bg-(--ui-bg-quinary) text-(--ui-text-quaternary)',
  error: 'bg-(--ui-orange) text-(--ui-bg-elevated)',
  not_run: 'bg-(--ui-bg-quinary) text-(--ui-text-tertiary)'
}

function StatusChip({ status }: { status: Rung['status'] | string }) {
  const key = (['pass', 'fail', 'skip', 'error', 'not_run'] as const).includes(
    status as Rung['status']
  )
    ? (status as Rung['status'])
    : 'error'

  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[0.625rem] font-semibold uppercase tracking-wide',
        STATUS_CLASS[key]
      )}
    >
      {STATUS_LABEL[key]}
    </span>
  )
}

function ModeHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-(--ui-stroke-tertiary) px-4 py-2.5">
      <div className="min-w-0">
        <h1 className="text-[0.9375rem] font-semibold tracking-tight">{title}</h1>
        <p className="text-[0.75rem] text-(--ui-text-quaternary)">{subtitle}</p>
      </div>
    </header>
  )
}

function WorkspaceBar({
  workspace,
  onWorkspace,
  onRefresh,
  busy,
  busyLabel
}: {
  workspace: string
  onWorkspace: (next: string) => void
  onRefresh: () => void
  busy: boolean
  busyLabel: string
}) {
  const [draft, setDraft] = useState(workspace)
  useEffect(() => setDraft(workspace), [workspace])

  return (
    <div className="flex flex-wrap items-center gap-2 px-4 py-2">
      <Input
        aria-label="Workspace path"
        className="w-full max-w-xl font-mono text-[0.75rem]"
        onChange={e => setDraft(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter') {onWorkspace(draft)}
        }}
        placeholder="Workspace path"
        spellCheck={false}
        value={draft}
      />
      <Button onClick={() => onWorkspace(draft)} size="sm" type="button" variant="secondary">
        Open
      </Button>
      <Button disabled={busy} onClick={onRefresh} size="sm" type="button" variant="text">
        {busy ? busyLabel : 'Refresh'}
      </Button>
    </div>
  )
}

function SectionCard({
  title,
  children,
  className
}: {
  title: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <section className={cn('flex min-h-0 flex-col rounded-md border border-(--ui-stroke-tertiary)', className)}>
      <h2 className="shrink-0 border-b border-(--ui-stroke-tertiary) px-3 py-2 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-(--ui-text-quaternary)">
        {title}
      </h2>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">{children}</div>
    </section>
  )
}

function ScreenshotThumb({ path, label }: { path: string; label: string }) {
  const [src, setSrc] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    readDesktopFileDataUrl(path)
      .then(dataUrl => {
        if (alive) {setSrc(dataUrl)}
      })
      .catch((e: unknown) => {
        if (alive) {setErr(e instanceof Error ? e.message : String(e))}
      })

    return () => {
      alive = false
    }
  }, [path])

  if (err) {
    return <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{err}</p>
  }

  if (!src) {
    return <p className="text-[0.6875rem] text-(--ui-text-quaternary)">Loading…</p>
  }

  return (
    <figure className="overflow-hidden rounded-md border border-(--ui-stroke-tertiary)">
      <img alt={label} className="block w-full" src={src} />
      <figcaption className="border-t border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) px-2 py-1 font-mono text-[0.625rem] text-(--ui-text-tertiary)">
        {label}
      </figcaption>
    </figure>
  )
}

function BuildLadder({ verifyResult }: { verifyResult: VerifyResult | null }) {
  if (verifyResult?.error) {
    return <p className="text-[0.8125rem] text-(--ui-text-tertiary)">{verifyResult.error}</p>
  }

  if (!verifyResult?.results) {
    return (
      <p className="text-[0.8125rem] text-(--ui-text-quaternary)">
        No ladder run yet. Run the ladder, or ask the agent to verify() in a session.
      </p>
    )
  }

  const allRungs = buildLadderRungs(verifyResult.results)
  const verdict = ladderVerdict(verifyResult)

  return (
    <div className="flex flex-col gap-1.5">
      {allRungs.map(rung => (
        <div
          className={cn(
            'rounded-md border px-2.5 py-2',
            rung.status === 'fail' || rung.status === 'error'
              ? 'border-(--ui-stroke-tertiary) bg-[color-mix(in_srgb,var(--ui-red,#cf2d56)_6%,transparent)]'
              : 'border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary)'
          )}
          key={rung.name}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[0.8125rem] font-medium">
              {RUNG_LABELS[rung.name] ?? rung.name}
            </span>
            <span className="flex items-center gap-2">
              {typeof rung.duration_ms === 'number' ? (
                <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">
                  {(rung.duration_ms / 1000).toFixed(1)}s
                </span>
              ) : null}
              <StatusChip status={rung.status} />
            </span>
          </div>
          {rung.status === 'fail' && rung.hint ? (
            <p className="mt-1 text-[0.75rem] text-(--ui-text-secondary)">{rung.hint}</p>
          ) : null}
          {rung.output && rung.status !== 'pass' ? (
            <p className="mt-1 truncate font-mono text-[0.625rem] text-(--ui-text-tertiary)" title={rung.output}>
              {rung.output}
            </p>
          ) : null}
        </div>
      ))}
      <p className="text-[0.75rem] font-medium">
        Ladder: {verdict === 'PASSED' ? 'PASSED' : verdict === 'PARTIAL' ? 'PARTIAL' : 'NOT PASSED'}
      </p>
    </div>
  )
}

export function BuildModePage({ requestGateway }: { requestGateway: GatewayRequester }) {
  useEffect(() => {
    setBuroojMode('build')
  }, [])

  const [workspace, setWorkspace] = useState(rememberedWorkspace)
  const [status, setStatus] = useState<BuildStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)

    try {
      const result = await requestGateway<BuildStatus>('burooj.build.status', {
        ...(workspace ? { workspace } : {})
      })

      setStatus(result)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [requestGateway, workspace])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const runLadder = useCallback(async () => {
    setRunning('ladder')

    try {
      // Builds and tests take minutes; the default 30s RPC timeout is a
      // guaranteed false failure for a real ladder run.
      const result = await requestGateway<VerifyResult>('burooj.build.verify', {
        ...(workspace ? { workspace } : {})
      }, 600_000)

      setStatus(prev => (prev ? { ...prev, last_verify: result } : prev))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(null)
    }
  }, [requestGateway, workspace])

  const runPreview = useCallback(async () => {
    setRunning('preview')

    try {
      const result = await requestGateway<BuildStatus['last_preview']>('burooj.build.preview', {
        ...(workspace ? { workspace } : {})
      }, 300_000)

      setStatus(prev => (prev ? { ...prev, last_preview: result } : prev))
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(null)
    }
  }, [refresh, requestGateway, workspace])

  const applyWorkspace = useCallback(
    (next: string) => {
      persistWorkspace(next)
      setWorkspace(next.trim())
    },
    []
  )

  const devServer = status?.dev_server

  const serverLines = useMemo(() => {
    if (!devServer) {return []}
    const stdout = devServer.stdout_tail ?? []
    const stderr = devServer.stderr_tail ?? []
    const tail = [...stdout.slice(-30), ...stderr.slice(-30).map(l => `stderr: ${l}`)]

    return tail.slice(-40)
  }, [devServer])

  const previewRoutes = status?.last_preview?.routes ?? []
  const previewError = status?.last_preview?.error ?? null

  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-(--ui-bg-primary) text-(--ui-text-primary)">
      <ModeHeader subtitle="Code, run, ship · the ladder decides done" title="Build" />
      <WorkspaceBar
        busy={loading}
        busyLabel="Refreshing…"
        onRefresh={() => void refresh()}
        onWorkspace={applyWorkspace}
        workspace={workspace}
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 overflow-y-auto px-4 py-3 lg:grid-cols-2 xl:grid-cols-3">
        {/* Workspace + ladder */}
        <div className="flex min-h-0 flex-col gap-3 xl:col-span-2">
          <SectionCard title="Workspace">
            <dl className="grid gap-1.5">
              <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2">
                <dt className="text-[0.6875rem] uppercase tracking-wide text-(--ui-text-quaternary)">Path</dt>
                <dd className="truncate font-mono text-[0.75rem]">{status?.workspace ?? (workspace || 'not resolved yet')}</dd>              </div>
              <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2">
                <dt className="text-[0.6875rem] uppercase tracking-wide text-(--ui-text-quaternary)">Manifest</dt>
                <dd className="truncate font-mono text-[0.75rem]">
                  {status?.manifest_path ?? status?.manifest_error ?? 'no burooj.build.json'}
                </dd>
              </div>
              {status?.manifest ? (
                <>
                  <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2">
                    <dt className="text-[0.6875rem] uppercase tracking-wide text-(--ui-text-quaternary)">Routes</dt>
                    <dd className="font-mono text-[0.75rem]">
                      {(status.manifest.routes ?? []).join(' · ') || '—'}
                    </dd>
                  </div>
                  <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2">
                    <dt className="text-[0.6875rem] uppercase tracking-wide text-(--ui-text-quaternary)">Commands</dt>
                    <dd className="font-mono text-[0.625rem] leading-relaxed text-(--ui-text-tertiary)">
                      {['install', 'typecheck', 'lint', 'build'].map(k => {
                        const cmd = status.manifest?.[k as keyof typeof status.manifest] as string | undefined

                        return cmd ? <div key={k}>{k}: {cmd}</div> : null
                      })}
                      {status.manifest.test_command ? <div>test: {status.manifest.test_command}</div> : null}
                      {status.manifest.dev_command ? <div>dev: {status.manifest.dev_command}</div> : null}
                    </dd>
                  </div>
                </>
              ) : null}
            </dl>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button disabled={running !== null} onClick={() => void runLadder()} size="sm" type="button">
                {running === 'ladder' ? 'Running ladder…' : 'Run ladder'}
              </Button>
              <Button disabled={running !== null} onClick={() => void runPreview()} size="sm" type="button" variant="secondary">
                {running === 'preview' ? 'Previewing…' : 'Preview routes'}
              </Button>
            </div>
            {status?.last_verify?.disclosure ? (
              <p className="mt-2 text-[0.6875rem] text-(--ui-text-quaternary)">
                Verified workspace: {status.last_verify.disclosure.workspace} ·{' '}
                {status.last_verify.disclosure.manifest}
              </p>
            ) : null}
          </SectionCard>

          <SectionCard className="flex-1" title="Verification ladder">
            <BuildLadder verifyResult={status?.last_verify ?? null} />
          </SectionCard>
        </div>

        {/* Dev server */}
        <SectionCard title="Dev server">
          {devServer ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[0.8125rem] font-medium">
                  {devServer.running ? 'Running' : 'Stopped'}
                </span>
                <StatusChip status={devServer.running ? 'pass' : 'skip'} />
              </div>
              <p className="font-mono text-[0.75rem] text-(--ui-text-secondary)">
                {devServer.base_url} · port {devServer.port}
              </p>
              <pre className="mt-1 max-h-56 overflow-auto rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-editor) p-2 font-mono text-[0.625rem] leading-relaxed text-(--ui-text-secondary)">
                {serverLines.length > 0 ? serverLines.join('\n') : 'No output captured yet. Run the ladder or preview().'}
              </pre>
            </div>
          ) : (
            <p className="text-[0.75rem] text-(--ui-text-quaternary)">
              {status?.manifest_error
                ? 'No burooj.build.json, so no dev server.'
                : 'No dev section in burooj.build.json.'}
            </p>
          )}
        </SectionCard>

        {/* Screenshots */}
        <SectionCard className="lg:col-span-2 xl:col-span-3" title="Route screenshots">
          {previewError ? (
            <p className="text-[0.75rem] text-(--ui-text-tertiary)">{previewError}</p>
          ) : null}
          {previewRoutes.length === 0 && status?.screenshots?.length === 0 ? (
            <p className="text-[0.75rem] text-(--ui-text-quaternary)">
              No captures yet. Preview the routes, or run the ladder to render them.
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {previewRoutes.map(route => (
                <div className="flex flex-col gap-1.5" key={route.path}>
                  <ScreenshotThumb label={route.path} path={route.screenshot} />
                  {(route.console_errors?.length ?? 0) > 0 ? (
                    <p className="text-[0.6875rem] text-(--ui-red)">
                      {route.console_errors!.length} console error(s)
                    </p>
                  ) : null}
                  {(route.network_errors?.length ?? 0) > 0 ? (
                    <p className="text-[0.6875rem] text-(--ui-orange)">
                      {route.network_errors!.length} network error(s)
                    </p>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </SectionCard>
      </div>

      {error ? (
        <div
          className="shrink-0 border-t border-(--ui-stroke-tertiary) px-4 py-2 text-[0.75rem] text-(--ui-red)"
          role="alert"
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}

export function DesignModePage({ requestGateway }: { requestGateway: GatewayRequester }) {
  useEffect(() => {
    setBuroojMode('design')
  }, [])

  const [workspace, setWorkspace] = useState(rememberedWorkspace)
  const [status, setStatus] = useState<DesignStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)

    try {
      const result = await requestGateway<DesignStatus>('burooj.design.status', {
        ...(workspace ? { workspace } : {})
      })

      setStatus(result)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [requestGateway, workspace])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const runChecks = useCallback(async () => {
    setRunning(true)

    try {
      const result = await requestGateway<Partial<DesignStatus> & { passed?: boolean }>(
        'burooj.design.checks',
        {
          ...(workspace ? { workspace } : {})
        },
        300_000
      )

      setStatus(prev => mergeDesignChecks(prev, result))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }, [requestGateway, workspace])

  const applyWorkspace = useCallback(
    (next: string) => {
      persistWorkspace(next)
      setWorkspace(next.trim())
    },
    []
  )

  const tokens = status?.tokens?.tokens ?? []
  const tokenError = status?.tokens?.error ?? null
  const contrastPairs = status?.contrast?.pairs ?? []
  const lintError = status?.lint?.error ?? null
  const vdiffRoutes = status?.visual_diff?.routes ?? []
  const vdiffError = status?.visual_diff?.error ?? null

  const violationsByFile = useMemo(() => {
    const lintViolations = status?.lint?.violations ?? []
    const byFile = new Map<string, typeof lintViolations>()

    for (const v of lintViolations) {
      const list = byFile.get(v.file) ?? []
      list.push(v)
      byFile.set(v.file, list)
    }

    return [...byFile.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [status])

  const a11yStatus = status?.a11y_check?.status ?? null
  const a11yNote = status?.a11y_check?.reason ?? status?.a11y_check?.error ?? null

  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-(--ui-bg-primary) text-(--ui-text-primary)">
      <ModeHeader subtitle="Design system · tokens, gate, baselines" title="Design" />
      <WorkspaceBar
        busy={loading}
        busyLabel="Refreshing…"
        onRefresh={() => void refresh()}
        onWorkspace={applyWorkspace}
        workspace={workspace}
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 overflow-y-auto px-4 py-3 lg:grid-cols-2 xl:grid-cols-3">
        {/* Tokens */}
        <SectionCard title="Token swatches">
          {tokenError ? <p className="text-[0.75rem] text-(--ui-text-tertiary)">{tokenError}</p> : null}
          {tokens.length === 0 ? (
            <p className="text-[0.75rem] text-(--ui-text-quaternary)">
              {status?.tokens?.reason ?? 'No burooj.design/tokens.json in this workspace.'}
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {tokens.map(token => (
                <li
                  className="flex items-center gap-2 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) px-2 py-1.5"
                  key={token.path}
                >
                  <span
                    aria-label={token.path}
                    className="size-6 shrink-0 rounded border border-(--ui-stroke-tertiary)"
                    style={{ backgroundColor: token.value.startsWith('#') || token.value.startsWith('rgb') || token.value.startsWith('oklch') ? token.value : 'transparent' }}
                  />
                  <span className="min-w-0 flex-1 truncate font-mono text-[0.6875rem]">{token.path}</span>
                  <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">{token.value}</span>
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        {/* Contrast */}
        <SectionCard title="Contrast pairs · WCAG AA">
          {status?.contrast?.status === 'skip' || contrastPairs.length === 0 ? (
            <p className="text-[0.75rem] text-(--ui-text-quaternary)">
              {status?.contrast?.reason ?? 'No token pairs to check.'}
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {contrastPairs.map((pair, i) => (
                <li
                  className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) px-2 py-1.5"
                  key={`${pair.foreground}-${pair.background}-${i}`}
                >
                  <div className="min-w-0">
                    <p className="font-mono text-[0.6875rem]">
                      {pair.foreground} on {pair.background}
                    </p>
                    <p className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">
                      {pair.ratio.toFixed(2)}:1 · needs {pair.required}:1
                    </p>
                  </div>
                  <StatusChip status={pair.passed ? 'pass' : 'fail'} />
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        {/* Lint */}
        <SectionCard title="design_lint">
          {lintError ? <p className="text-[0.75rem] text-(--ui-text-tertiary)">{lintError}</p> : null}
          {status?.lint?.status === 'skip' || violationsByFile.length === 0 ? (
            <p className="text-[0.75rem] text-(--ui-text-quaternary)">
              {status?.lint?.reason ?? 'No violations. The workspace follows the design system.'}
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {violationsByFile.map(([file, violations]) => (
                <div key={file}>
                  <p className="font-mono text-[0.6875rem] font-medium text-(--ui-text-secondary)">{file}</p>
                  <ul className="mt-0.5 flex flex-col gap-1">
                    {violations.map((v, i) => (
                      <li
                        className="rounded border border-(--ui-stroke-tertiary) px-2 py-1 text-[0.6875rem]"
                        key={`${file}-${v.line}-${i}`}
                      >
                        <span className="font-mono text-(--ui-text-quaternary)">L{v.line}</span>{' '}
                        <span className="font-medium">[{v.rule}]</span>{' '}
                        <span className="font-mono text-(--ui-text-tertiary)">{v.value}</span>
                        <span className="block text-(--ui-text-tertiary)">{v.message}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </SectionCard>

        {/* Visual diff */}
        <SectionCard className="lg:col-span-2" title="Visual diff · baseline vs current">
          {vdiffError ? <p className="text-[0.75rem] text-(--ui-text-tertiary)">{vdiffError}</p> : null}
          {vdiffRoutes.length === 0 ? (
            <p className="text-[0.75rem] text-(--ui-text-quaternary)">
              {status?.visual_diff?.reason ??
                'No visual diff yet. Run the checks (needs the dev server and baselines).'}
            </p>
          ) : (
            <div className="flex flex-col gap-3">
              {vdiffRoutes.map((route, i) => {
                const cell = route.breakpoint || route.theme
                  ? `${route.breakpoint ?? '?'}px · ${route.theme || 'default'}`
                  : null

                return (
                  <div className="flex flex-col gap-1.5" key={`${route.path}-${i}`}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-mono text-[0.75rem] font-medium">
                        {route.path}
                        {cell ? <span className="ml-2 text-(--ui-text-quaternary)">({cell})</span> : null}
                      </span>
                      <span className="flex items-center gap-2">
                        <span className="font-mono text-[0.6875rem] text-(--ui-text-quaternary)">
                          {route.diff_pct.toFixed(2)}% changed · threshold {route.threshold}%
                        </span>
                        <StatusChip status={route.passed ? 'pass' : 'fail'} />
                      </span>
                    </div>
                    {route.new_baseline ? (
                      <p className="text-[0.6875rem] text-(--ui-text-tertiary)">
                        {route.note ?? 'First run, baseline saved.'}
                      </p>
                    ) : (
                      <div className="grid grid-cols-2 gap-2">
                        <ScreenshotThumb label={`baseline: ${route.baseline}`} path={route.baseline} />
                        {route.note ? <p className="text-[0.6875rem] text-(--ui-text-tertiary)">{route.note}</p> : null}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </SectionCard>

        {/* A11y + actions */}
        <SectionCard title="Design gate">
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[0.8125rem] font-medium">a11y_check</span>
              {a11yStatus ? (
                <StatusChip status={a11yStatus === 'fail' ? 'fail' : a11yStatus} />
              ) : (
                <StatusChip status="skip" />
              )}
            </div>
            {a11yNote ? <p className="text-[0.6875rem] text-(--ui-text-tertiary)">{a11yNote}</p> : null}
            <Button
              className="mt-1 self-start"
              disabled={running}
              onClick={() => void runChecks()}
              size="sm"
              type="button"
            >
              {running ? 'Running checks…' : 'Run design checks'}
            </Button>
            <p className="text-[0.6875rem] leading-relaxed text-(--ui-text-quaternary)">
              Lint and contrast run live. a11y and visual diff need the dev server, so they show
              the last run until you re-run the checks.
            </p>
          </div>
        </SectionCard>
      </div>

      {error ? (
        <div
          className="shrink-0 border-t border-(--ui-stroke-tertiary) px-4 py-2 text-[0.75rem] text-(--ui-red)"
          role="alert"
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}
