import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api } from './client'
import type { Job, OperationRequest, QuerySpec } from './types'
import { TERMINAL_STATUSES } from './types'

export const keys = {
  datasets: ['datasets'] as const,
  dataset: (id: string) => ['dataset', id] as const,
  profile: (id: string, v?: number) => ['profile', id, v ?? 'latest'] as const,
  versions: (id: string) => ['versions', id] as const,
  lineage: (id: string) => ['lineage', id] as const,
  suggestions: (id: string, kind?: string) => ['suggestions', id, kind ?? 'all'] as const,
  plugins: (p: object) => ['plugins', p] as const,
  jobs: ['jobs'] as const,
  job: (id: string) => ['job', id] as const,
  dashboards: ['dashboards'] as const,
}

export const useDatasets = () => useQuery({ queryKey: keys.datasets, queryFn: api.datasets })

/** One dataset's summary — name, kind, row count. The profile does not carry these. */
export const useDataset = (id: string | undefined) =>
  useQuery({
    queryKey: keys.dataset(id ?? ''),
    queryFn: () => api.dataset(id!),
    enabled: Boolean(id),
  })

export const useProfile = (id: string | undefined, version?: number) =>
  useQuery({
    queryKey: keys.profile(id ?? '', version),
    queryFn: () => api.profile(id!, version),
    enabled: Boolean(id),
  })

export const useVersions = (id: string | undefined) =>
  useQuery({
    queryKey: keys.versions(id ?? ''),
    queryFn: () => api.versions(id!),
    enabled: Boolean(id),
  })

export const useLineage = (id: string | undefined) =>
  useQuery({
    queryKey: keys.lineage(id ?? ''),
    queryFn: () => api.lineage(id!),
    enabled: Boolean(id),
  })

export const useSuggestions = (id: string | undefined, kind?: string) =>
  useQuery({
    queryKey: keys.suggestions(id ?? '', kind),
    queryFn: () => api.suggestions(id!, kind),
    enabled: Boolean(id),
  })

export const usePlugins = (params: { kind?: string; mode?: string; applicable_to?: string } = {}) =>
  useQuery({ queryKey: keys.plugins(params), queryFn: () => api.plugins(params) })

export const useDashboards = () =>
  useQuery({ queryKey: keys.dashboards, queryFn: api.dashboards })

/** Fire an operation and hand back the job id so the caller can watch it. */
export function useOperation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (req: OperationRequest) => api.operation(req),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.jobs })
    },
  })
}

export function useQuerySpec(spec: QuerySpec | null) {
  return useQuery({
    queryKey: ['query', spec],
    queryFn: () => api.query(spec!),
    enabled: Boolean(spec?.dataset),
  })
}

/**
 * Follow a job to completion over SSE, falling back to polling if the stream
 * cannot be established. Invalidates dataset caches when the job finishes so new
 * versions appear without a manual refresh.
 */
export function useJobWatcher(jobId: string | null) {
  const [job, setJob] = useState<Job | null>(null)
  const qc = useQueryClient()

  useEffect(() => {
    if (!jobId) {
      setJob(null)
      return
    }
    let cancelled = false
    let poll: ReturnType<typeof setInterval> | undefined

    const finish = () => {
      qc.invalidateQueries({ queryKey: keys.datasets })
      qc.invalidateQueries({ queryKey: keys.jobs })
    }

    const source = new EventSource(`/api/jobs/${jobId}/stream`)
    source.onmessage = (event) => {
      if (cancelled) return
      const payload = JSON.parse(event.data) as Job
      setJob(payload)
      if (TERMINAL_STATUSES.includes(payload.status)) {
        source.close()
        finish()
      }
    }
    source.onerror = () => {
      // Some proxies buffer SSE; degrade to polling rather than stalling the UI.
      source.close()
      if (cancelled || poll) return
      poll = setInterval(async () => {
        const payload = await api.job(jobId)
        if (cancelled) return
        setJob(payload)
        if (TERMINAL_STATUSES.includes(payload.status)) {
          clearInterval(poll)
          poll = undefined
          finish()
        }
      }, 1000)
    }

    return () => {
      cancelled = true
      source.close()
      if (poll) clearInterval(poll)
    }
  }, [jobId, qc])

  return job
}
