import { useEffect, useMemo, useRef, useState } from 'react'
import Map, { Layer, Source, type MapRef } from 'react-map-gl/maplibre'

import type { RendererProps } from './index'

/**
 * Basemap style. OpenStreetMap raster tiles need no API key, which keeps local
 * setup zero-config; point VITE_MAP_STYLE at a vector style for production use
 * (OSM's tile policy is not meant for heavy traffic).
 */
const STYLE_URL = import.meta.env.VITE_MAP_STYLE as string | undefined

const OSM_STYLE = {
  version: 8 as const,
  sources: {
    osm: {
      type: 'raster' as const,
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      attribution: '© OpenStreetMap contributors',
    },
  },
  layers: [{ id: 'osm', type: 'raster' as const, source: 'osm' }],
}

function num(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

export function MapLibreRenderer({ spec, data, height = 420 }: RendererProps) {
  const mapRef = useRef<MapRef | null>(null)
  const cfg = spec.spec as {
    lat_field?: string
    lng_field?: string
    value_field?: string | null
    color?: string
  }
  const latField = cfg.lat_field ?? 'lat'
  const lngField = cfg.lng_field ?? 'lng'
  const valueField = cfg.value_field ?? null
  const animate = spec.animate ?? null

  // --- animation frames -------------------------------------------------
  const frames = useMemo(() => {
    if (!animate) return []
    const seen = new Set<string>()
    for (const row of data) {
      const v = row[animate.field]
      if (v != null) seen.add(String(v))
    }
    return [...seen].sort()
  }, [animate, data])

  const [frameIndex, setFrameIndex] = useState(0)
  const [playing, setPlaying] = useState(false)

  useEffect(() => {
    setFrameIndex(0)
  }, [frames.length])

  useEffect(() => {
    if (!playing || frames.length === 0) return
    const fps = animate?.fps ?? 2
    const timer = setInterval(() => {
      setFrameIndex((i) => (i + 1) % frames.length)
    }, 1000 / fps)
    return () => clearInterval(timer)
  }, [playing, frames.length, animate?.fps])

  const visible = useMemo(() => {
    if (!animate || frames.length === 0) return data
    const current = frames[frameIndex]
    return data.filter((row) => String(row[animate.field]) === current)
  }, [animate, data, frames, frameIndex])

  // --- geojson ----------------------------------------------------------
  const { geojson, bounds, maxValue } = useMemo(() => {
    const features = []
    let minLng = 180
    let minLat = 90
    let maxLng = -180
    let maxLat = -90
    let max = 1
    for (const row of visible) {
      const lat = num(row[latField])
      const lng = num(row[lngField])
      if (lat === null || lng === null) continue
      const value = valueField ? (num(row[valueField]) ?? 1) : 1
      max = Math.max(max, value)
      minLng = Math.min(minLng, lng)
      maxLng = Math.max(maxLng, lng)
      minLat = Math.min(minLat, lat)
      maxLat = Math.max(maxLat, lat)
      features.push({
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: [lng, lat] },
        properties: { value },
      })
    }
    return {
      geojson: { type: 'FeatureCollection' as const, features },
      bounds: features.length ? ([minLng, minLat, maxLng, maxLat] as const) : null,
      maxValue: max,
    }
  }, [visible, latField, lngField, valueField])

  // Fit to the data once it is known, rather than guessing an initial viewport.
  const fitted = useRef(false)
  useEffect(() => {
    if (fitted.current || !bounds || !mapRef.current) return
    fitted.current = true
    mapRef.current.fitBounds(
      [
        [bounds[0], bounds[1]],
        [bounds[2], bounds[3]],
      ],
      { padding: 40, duration: 0 },
    )
  }, [bounds])

  if (!data.length) {
    return <div className="p-6 text-center text-sm text-slate-500">No geographic data</div>
  }

  return (
    <div>
      <div style={{ height }} className="overflow-hidden rounded border border-slate-200">
        <Map
          ref={mapRef}
          initialViewState={{ longitude: -73.95, latitude: 40.75, zoom: 10 }}
          mapStyle={STYLE_URL ?? OSM_STYLE}
          style={{ width: '100%', height: '100%' }}
        >
          <Source id="points" type="geojson" data={geojson}>
            <Layer
              id="points-circles"
              type="circle"
              paint={{
                'circle-radius': valueField
                  ? ['interpolate', ['linear'], ['get', 'value'], 0, 3, maxValue, 18]
                  : 4,
                'circle-color': cfg.color ?? '#4269d0',
                'circle-opacity': 0.65,
                'circle-stroke-width': 0.5,
                'circle-stroke-color': '#ffffff',
              }}
            />
          </Source>
        </Map>
      </div>

      {animate && frames.length > 1 && (
        <div className="mt-2 flex items-center gap-3">
          <button
            type="button"
            onClick={() => setPlaying((p) => !p)}
            className="rounded bg-slate-800 px-3 py-1 text-sm text-white hover:bg-slate-700"
          >
            {playing ? 'Pause' : 'Play'}
          </button>
          <input
            type="range"
            min={0}
            max={frames.length - 1}
            value={frameIndex}
            onChange={(e) => {
              setPlaying(false)
              setFrameIndex(Number(e.target.value))
            }}
            className="flex-1"
          />
          <span className="min-w-32 font-mono text-xs text-slate-600">
            {frames[frameIndex]?.slice(0, 19) ?? ''}
          </span>
        </div>
      )}
      <div className="mt-1 text-xs text-slate-500">
        {visible.length.toLocaleString()} points
        {animate && frames.length > 1 ? ` · frame ${frameIndex + 1}/${frames.length}` : ''}
      </div>
    </div>
  )
}
