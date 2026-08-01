/**
 * Settings → Sanad knowledge: corpus management and document upload.
 * Hermes chrome and tokens only. Calls the Sanad HTTP API directly, same as
 * the Sanad mode page. See docs/burooj-shell-ui.md.
 */
import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Bookmark, FileText, FolderOpen, Trash2 } from '@/lib/icons'
import { notify, notifyError } from '@/store/notifications'

import { EmptyState, ListRow, SectionHeading, SettingsContent } from './primitives'

const DEFAULT_API = 'http://127.0.0.1:8787'

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
  const res = await fetch(`${sanadApiBase()}${path}`, init)
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

export function SanadKnowledgeSettings() {
  const [health, setHealth] = useState<string>('Checking Sanad API…')
  const [corpus, setCorpus] = useState<CorpusDoc[]>([])
  const [loading, setLoading] = useState(true)
  const [busyDoc, setBusyDoc] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadNote, setUploadNote] = useState<string | null>(null)
  const [syncRoot, setSyncRoot] = useState('./fixtures')
  const [syncing, setSyncing] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const h = (await sanadFetch('/health')) as {
        chunk_count?: number
        embedding?: { provider?: string; model?: string }
      }

      setHealth(
        `${h.chunk_count ?? 0} chunks · ${h.embedding?.provider ?? '?'} · ${h.embedding?.model ?? '?'}`
      )
      const c = (await sanadFetch('/v1/corpus')) as { documents?: CorpusDoc[] }
      setCorpus(c.documents || [])
    } catch (e) {
      setHealth('Sanad API offline')
      notifyError(
        e,
        'Sanad API unreachable. Start it with: cd sanad && uvicorn sanad.api.app:app --port 8787'
      )
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const uploadFile = async (file: File) => {
    setUploading(true)
    setUploadNote(null)

    try {
      const form = new FormData()

      form.append('file', file)

      const res = await fetch(`${sanadApiBase()}/v1/upload`, {
        body: form,
        method: 'POST'
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

        throw new Error(detail || `Upload failed (${res.status})`)
      }

      const body = data as { document_id?: string; chunk_count?: number }

      setUploadNote(
        body.document_id
          ? `Indexed ${body.chunk_count ?? 0} chunk(s) from ${file.name}`
          : `Indexed ${file.name}`
      )
      await refresh()
    } catch (e) {
      notifyError(e, 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  const deleteDocument = async (doc: CorpusDoc) => {
    setBusyDoc(doc.document_id)

    try {
      const result = (await sanadFetch(`/v1/corpus/${encodeURIComponent(doc.document_id)}`, {
        method: 'DELETE'
      })) as { chunks_removed?: number }

      notify({ kind: 'info', title: 'Sanad corpus', message: `Removed ${result.chunks_removed ?? 0} chunk(s) of ${doc.title}` })
      await refresh()
    } catch (e) {
      notifyError(e, 'Delete failed')
    } finally {
      setBusyDoc(null)
    }
  }

  const runSync = async () => {
    setSyncing(true)

    try {
      const data = (await sanadFetch('/v1/sync', {
        method: 'POST',
        body: JSON.stringify({
          source_type: 'local_files',
          config: { root: syncRoot.trim() || './fixtures' }
        })
      })) as { document_count: number; chunk_count: number }

      notify({ kind: 'info', title: 'Sanad sync', message: `Synced ${data.document_count} docs · ${data.chunk_count} chunks` })
      await refresh()
    } catch (e) {
      notifyError(e, 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  return (
    <SettingsContent>
      <SectionHeading icon={Bookmark} meta={health} title="Sanad knowledge" />

      <div className="flex flex-col gap-1">
        <ListRow
          action={
            <label className="inline-flex">
              <input
                accept=".md,.markdown,.txt,.json,.html,.htm"
                className="hidden"
                disabled={uploading}
                onChange={e => {
                  const file = e.target.files?.[0]

                  if (file) {
                    void uploadFile(file)
                  }

                  e.target.value = ''
                }}
                type="file"
              />
              <Button disabled={uploading} size="sm" type="button" variant="secondary">
                <FileText />
                {uploading ? 'Uploading…' : 'Upload document'}
              </Button>
            </label>
          }
          description="Markdown, text, JSON and HTML. Public scope. Writes need SANAD_ADMIN_TOKEN on the API (or SANAD_ALLOW_UNAUTHENTICATED_WRITES=1 for open local). Upload stays disabled until one is set; gateway token wiring is not wired yet."
          title="Upload a document"
        />
        {uploadNote ? (
          <p className="px-1 pb-1 text-[0.75rem] text-(--ui-text-tertiary)">{uploadNote}</p>
        ) : null}

        <ListRow
          action={
            <div className="flex gap-2">
              <Input
                aria-label="Local path to sync"
                className="w-56 font-mono text-[0.75rem]"
                onChange={e => setSyncRoot(e.target.value)}
                placeholder="./fixtures"
                value={syncRoot}
              />
              <Button disabled={syncing} onClick={() => void runSync()} size="sm" type="button">
                <FolderOpen />
                {syncing ? 'Syncing…' : 'Sync folder'}
              </Button>
            </div>
          }
          description="Sync a local folder of markdown or text files through the local_files connector."
          title="Sync a local folder"
        />
      </div>

      <SectionHeading
        aside={
          <Button disabled={loading} onClick={() => void refresh()} size="xs" type="button" variant="text">
            Refresh
          </Button>
        }
        icon={Bookmark}
        meta={`${corpus.length} doc(s)`}
        title="Corpus"
      />

      {corpus.length === 0 ? (
        <EmptyState
          description={
            loading
              ? 'Contacting the Sanad API.'
              : 'Upload a document or sync a folder above.'
          }
          title={loading ? 'Loading…' : 'No documents indexed'}
        />
      ) : (
        <div className="flex flex-col gap-1">
          {corpus.map(doc => (
            <ListRow
              action={
                <Button
                  disabled={busyDoc === doc.document_id}
                  onClick={() => void deleteDocument(doc)}
                  size="sm"
                  type="button"
                  variant="text"
                >
                  <Trash2 />
                  {busyDoc === doc.document_id ? 'Removing…' : 'Delete'}
                </Button>
              }
              description={
                <span className="font-mono text-[0.68rem]">
                  {doc.chunk_count} chunk(s) · {doc.access_scope}
                  {doc.department ? ` · ${doc.department}` : ''}
                </span>
              }
              hint={doc.source_path}
              key={doc.document_id}
              title={doc.title}
            />
          ))}
        </div>
      )}
    </SettingsContent>
  )
}
