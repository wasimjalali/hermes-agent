/**
 * Sanad mode — company knowledge inside Hermes desktop chrome.
 * Hermes tokens/components only. Calls Sanad HTTP API for data.
 */
import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { setBuroojMode } from '@/store/burooj-mode'

const DEFAULT_API = 'http://127.0.0.1:8787'

type PrincipalKey = 'support' | 'manager' | 'warehouse'

const PRINCIPALS: Record<
  PrincipalKey,
  { user_id: string; roles: string[]; departments: string[]; label: string }
> = {
  support: {
    user_id: 'support_agent',
    roles: ['standard'],
    departments: ['support'],
    label: 'Support agent'
  },
  manager: {
    user_id: 'manager_user',
    roles: ['lead'],
    departments: ['management'],
    label: 'Manager'
  },
  warehouse: {
    user_id: 'warehouse_user',
    roles: ['standard'],
    departments: ['warehouse'],
    label: 'Warehouse'
  }
}

interface Citation {
  source_name: string
  section_heading: string
  chunk_id: string
  document_id: string
  source_path: string
}

interface SearchHit {
  chunk_id: string
  content: string
  score: number
  citation: Citation
}

interface SearchResponse {
  hits: SearchHit[]
  citations: Citation[]
  not_enough_evidence: boolean
  trace: {
    merged_candidate_count?: number
    permission_removed_count?: number
    final_chunk_ids?: string[]
    // Reason to count. Sanad no longer returns per-chunk removal detail: a
    // chunk_id encodes the document id and section heading of a chunk this
    // principal is not allowed to see.
    permission_removals?: Record<string, number>
    latency_ms?: number
    principal?: { user_id: string }
  }
}

interface CorpusDoc {
  document_id: string
  title: string
  source_path: string
  chunk_count: number
  access_scope: string
  department?: string | null
}

function sanadApiBase(): string {
  try {
    return (localStorage.getItem('burooj.sanadBaseUrl') || DEFAULT_API).replace(/\/$/, '')
  } catch {
    return DEFAULT_API
  }
}

async function sanadFetch(path: string, init?: RequestInit) {
  const res = await fetch(`${sanadApiBase()}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {})
    }
  })
  const text = await res.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = { detail: text }
  }
  if (!res.ok) {
    const detail =
      typeof (data as { detail?: unknown })?.detail === 'string'
        ? (data as { detail: string }).detail
        : res.statusText
    throw new Error(detail || `Sanad API ${res.status}`)
  }
  return data
}

export function SanadModePage() {
  useEffect(() => {
    setBuroojMode('sanad')
  }, [])

  const [principalKey, setPrincipalKey] = useState<PrincipalKey>('support')
  const [question, setQuestion] = useState(
    'Can we offer RF-75 for a Germany renewed subscription?'
  )
  const [topK, setTopK] = useState(5)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [health, setHealth] = useState<string>('Checking Sanad API…')
  const [corpus, setCorpus] = useState<CorpusDoc[]>([])
  const [result, setResult] = useState<SearchResponse | null>(null)
  const [syncRoot, setSyncRoot] = useState('./fixtures')
  const [syncMsg, setSyncMsg] = useState<string | null>(null)

  const refreshMeta = useCallback(async () => {
    try {
      const h = (await sanadFetch('/health')) as {
        chunk_count?: number
        embedding?: { provider?: string; model?: string }
      }
      setHealth(
        `${h.chunk_count ?? 0} chunks · ${h.embedding?.provider ?? '?'} · ${h.embedding?.model ?? '?'}`
      )
      // Corpus is ACL filtered, so it needs the same principal as search. The
      // document list reflects what this principal is allowed to see.
      const principal = PRINCIPALS[principalKey]
      const query = new URLSearchParams({
        user_id: principal.user_id,
        roles: principal.roles.join(','),
        departments: principal.departments.join(',')
      })
      const c = (await sanadFetch(`/v1/corpus?${query}`)) as { documents?: CorpusDoc[] }
      setCorpus(c.documents || [])
      setError(null)
    } catch (e) {
      setHealth('Sanad API offline')
      setError(
        e instanceof Error
          ? `${e.message}. Start the knowledge service: cd sanad && uvicorn sanad.api.app:app --port 8787`
          : String(e)
      )
    }
  }, [principalKey])

  useEffect(() => {
    void refreshMeta()
  }, [refreshMeta])

  const runSearch = async () => {
    const q = question.trim()
    if (!q) {
      setError('Enter a question.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const principal = PRINCIPALS[principalKey]
      const data = (await sanadFetch('/v1/search', {
        method: 'POST',
        body: JSON.stringify({
          query: q,
          principal: {
            user_id: principal.user_id,
            roles: principal.roles,
            departments: principal.departments
          },
          top_k: topK,
          candidate_limit: 24
        })
      })) as SearchResponse
      setResult(data)
    } catch (e) {
      setResult(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  const runSync = async () => {
    setSyncMsg('Syncing…')
    try {
      const data = (await sanadFetch('/v1/sync', {
        method: 'POST',
        body: JSON.stringify({
          source_type: 'local_files',
          config: { root: syncRoot.trim() || './fixtures' }
        })
      })) as { document_count: number; chunk_count: number }
      setSyncMsg(`Synced ${data.document_count} docs · ${data.chunk_count} chunks`)
      await refreshMeta()
    } catch (e) {
      setSyncMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const hits = result?.hits ?? []
  const trace = result?.trace

  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-(--ui-bg-primary) text-(--ui-text-primary)">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-(--ui-stroke-tertiary) px-4 py-2.5">
        <div className="min-w-0">
          <h1 className="text-[0.9375rem] font-semibold tracking-tight">Sanad</h1>
          <p className="text-[0.75rem] text-(--ui-text-quaternary)">
            Company knowledge · source-backed answers
          </p>
        </div>
        <p className="font-mono text-[0.6875rem] text-(--ui-text-tertiary)">{health}</p>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-0 lg:grid-cols-[minmax(220px,280px)_minmax(0,1fr)_minmax(240px,300px)]">
        {/* Sources */}
        <aside className="flex min-h-0 flex-col border-b border-(--ui-stroke-tertiary) lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between px-3 py-2">
            <h2 className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-(--ui-text-quaternary)">
              Sources
            </h2>
            <Button onClick={() => void refreshMeta()} size="xs" type="button" variant="text">
              Refresh
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
            {corpus.length === 0 ? (
              <p className="rounded-md border border-dashed border-(--ui-stroke-tertiary) px-2 py-3 text-[0.75rem] text-(--ui-text-quaternary)">
                No documents indexed. Sync a local folder below (API must be running).
              </p>
            ) : (
              <ul className="flex flex-col gap-1">
                {corpus.map(doc => (
                  <li
                    className="rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) px-2 py-1.5"
                    key={doc.document_id}
                  >
                    <p className="truncate text-[0.8125rem] font-medium">{doc.title}</p>
                    <p className="truncate font-mono text-[0.625rem] text-(--ui-text-quaternary)">
                      {doc.chunk_count} chunks · {doc.access_scope}
                      {doc.department ? ` · ${doc.department}` : ''}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="border-t border-(--ui-stroke-tertiary) px-3 py-2">
            <p className="mb-1 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-(--ui-text-quaternary)">
              Sync local
            </p>
            <div className="flex flex-col gap-1.5">
              <Input
                aria-label="Local path to sync"
                onChange={e => setSyncRoot(e.target.value)}
                placeholder="./fixtures"
                value={syncRoot}
              />
              <Button onClick={() => void runSync()} size="sm" type="button" variant="secondary">
                Sync local files
              </Button>
              {syncMsg ? (
                <p className="text-[0.6875rem] text-(--ui-text-tertiary)">{syncMsg}</p>
              ) : null}
            </div>
          </div>
        </aside>

        {/* Ask */}
        <section className="flex min-h-0 flex-col border-b border-(--ui-stroke-tertiary) px-4 py-3 lg:border-b-0">
          <div className="mb-3 grid gap-2 sm:grid-cols-[1fr_5rem]">
            <div>
              <label className="mb-1 block text-[0.6875rem] font-medium text-(--ui-text-quaternary)">
                Demo principal
              </label>
              <Select
                onValueChange={v => setPrincipalKey(v as PrincipalKey)}
                value={principalKey}
              >
                <SelectTrigger aria-label="Demo principal" className="w-full" size="sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(PRINCIPALS) as PrincipalKey[]).map(key => (
                    <SelectItem key={key} value={key}>
                      {PRINCIPALS[key].label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="mb-1 block text-[0.6875rem] font-medium text-(--ui-text-quaternary)">
                Top k
              </label>
              <Input
                aria-label="Top k"
                max={20}
                min={1}
                onChange={e => setTopK(Number(e.target.value) || 5)}
                type="number"
                value={topK}
              />
            </div>
          </div>
          <label className="mb-1 block text-[0.6875rem] font-medium text-(--ui-text-quaternary)">
            Question
          </label>
          <textarea
            aria-label="Question"
            className="min-h-[5.5rem] w-full resize-y rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-2.5 py-2 text-[0.875rem] text-(--ui-text-primary) outline-none focus-visible:ring-2 focus-visible:ring-(--ui-accent)"
            onChange={e => setQuestion(e.target.value)}
            value={question}
          />
          <div className="mt-2 flex flex-wrap gap-2">
            <Button disabled={loading} onClick={() => void runSearch()} type="button">
              {loading ? 'Searching' : 'Search knowledge'}
            </Button>
            <Button
              onClick={() =>
                setQuestion('Can we offer RF-75 for a Germany renewed subscription?')
              }
              type="button"
              variant="text"
            >
              Sample question
            </Button>
          </div>

          {error ? (
            <div
              className="mt-3 rounded-md border border-(--ui-stroke-tertiary) bg-[color-mix(in_srgb,var(--ui-error,#c72e4d)_10%,transparent)] px-3 py-2 text-[0.8125rem] text-(--ui-text-primary)"
              role="alert"
            >
              {error}
            </div>
          ) : null}

          {result?.not_enough_evidence || (result && hits.length === 0) ? (
            <p className="mt-3 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-tertiary) px-3 py-2 text-[0.8125rem] text-(--ui-text-secondary)">
              The available documents do not provide enough evidence to answer this confidently.
            </p>
          ) : null}

          {hits.length > 0 ? (
            <div className="mt-4 min-h-0 flex-1 overflow-y-auto">
              <h2 className="mb-2 text-[0.8125rem] font-semibold">Evidence hits</h2>
              <ol className="flex flex-col gap-2">
                {hits.map((hit, i) => (
                  <li
                    className="rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) px-3 py-2"
                    key={hit.chunk_id}
                  >
                    <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
                      <span className="text-[0.8125rem] font-semibold">
                        {i + 1}. {hit.citation?.source_name ?? 'Source'}
                      </span>
                      <span className="font-mono text-[0.6875rem] text-(--ui-text-quaternary)">
                        {hit.score.toFixed(3)}
                      </span>
                    </div>
                    <p className="mb-1 text-[0.75rem] text-(--ui-text-tertiary)">
                      {hit.citation?.section_heading} ·{' '}
                      <span className="font-mono text-[0.625rem]">{hit.chunk_id}</span>
                    </p>
                    <p className="whitespace-pre-wrap text-[0.8125rem] leading-relaxed">
                      {hit.content}
                    </p>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
        </section>

        {/* Evidence pipeline */}
        <aside className="flex min-h-0 flex-col overflow-y-auto px-3 py-3">
          <h2 className="mb-2 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-(--ui-text-quaternary)">
            Evidence pipeline
          </h2>
          <ol className="mb-4 space-y-2 border-l-2 border-(--ui-stroke-tertiary) pl-3">
            <PipeStep
              label="Retrieve"
              value={
                trace?.merged_candidate_count != null
                  ? `${trace.merged_candidate_count} candidates`
                  : '—'
              }
            />
            <PipeStep
              label="Permission filter"
              value={
                trace?.permission_removed_count != null
                  ? `${trace.permission_removed_count} removed`
                  : '—'
              }
            />
            <PipeStep
              label="Final context"
              value={
                trace?.final_chunk_ids
                  ? `${trace.final_chunk_ids.length} chunks`
                  : '—'
              }
            />
            <PipeStep
              label="Latency"
              value={
                typeof trace?.latency_ms === 'number'
                  ? `${trace.latency_ms.toFixed(1)} ms`
                  : '—'
              }
            />
          </ol>
          <h3 className="mb-1 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-(--ui-text-quaternary)">
            Removals
          </h3>
          <ul className="mb-3 space-y-1">
            {Object.keys(trace?.permission_removals ?? {}).length > 0 ? (
              Object.entries(trace!.permission_removals!).map(([reason, count]) => (
                <li
                  className="rounded border border-(--ui-stroke-tertiary) px-2 py-1 font-mono text-[0.625rem] text-(--ui-text-tertiary)"
                  key={reason}
                >
                  {count} · {reason}
                </li>
              ))
            ) : (
              <li className="text-[0.75rem] text-(--ui-text-quaternary)">No permission removals</li>
            )}
          </ul>
          <h3 className="mb-1 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-(--ui-text-quaternary)">
            Citations
          </h3>
          <ul className="space-y-1">
            {(result?.citations?.length ?? 0) > 0 ? (
              result!.citations.map(c => (
                <li
                  className="rounded border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) px-2 py-1.5 text-[0.75rem]"
                  key={c.chunk_id}
                >
                  <p className="font-medium">{c.source_name}</p>
                  <p className="text-(--ui-text-tertiary)">{c.section_heading}</p>
                  <p className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">{c.chunk_id}</p>
                </li>
              ))
            ) : (
              <li className="text-[0.75rem] text-(--ui-text-quaternary)">Run a search to see citations</li>
            )}
          </ul>
        </aside>
      </div>
    </div>
  )
}

function PipeStep({ label, value }: { label: string; value: string }) {
  return (
    <li className={cn('relative')}>
      <p className="text-[0.625rem] font-semibold uppercase tracking-[0.08em] text-(--ui-text-quaternary)">
        {label}
      </p>
      <p className="font-mono text-[0.8125rem] text-(--ui-text-primary)">{value}</p>
    </li>
  )
}
