// maplibre-gl v6 is named-exports-only; it dropped the default export that
// react-map-gl 8.x still expects.
import {
  Map as MapLibreMap,
  setWorkerUrl,
  type GeoJSONSource,
  type StyleSpecification,
} from 'maplibre-gl'
// maplibre resolves its worker at runtime as
// `new URL('./maplibre-gl-worker.mjs', import.meta.url)` with a computed
// filename, which no bundler can see -- so the asset is never emitted and the
// worker 404s. Without a worker, GeoJSON sources are never tiled and every data
// layer draws nothing, while the raster basemap looks perfectly healthy.
//
// `?worker&url` rather than plain `?url`: the worker chunk imports a sibling
// (`./maplibre-gl-shared.mjs`), and `?url` copies the file verbatim as an asset
// without following that import, so the sibling is never emitted. `?worker`
// bundles the worker with its dependency graph; `&url` hands back a URL for
// setWorkerUrl instead of a constructor.
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import { useEffect, useMemo, useRef, useState } from 'react'

import type { RendererProps } from './index'

/**
 * Geographic scatter, driven directly by maplibre-gl.
 *
 * Driven imperatively rather than through react-map-gl: this component needs
 * exactly one source and one layer, and owning the map directly means the
 * lifecycle (add on 'load', setData per frame) is visible in one place. It also
 * keeps react-map-gl out of the dependency tree, which matters because it does
 * not yet track maplibre-gl v6's move to named-only exports.
 *
 * Note the failure that made data layers invisible was neither of those: Vite's
 * dep pre-bundling broke maplibre's Web Worker URL, and GeoJSON is tiled in that
 * worker. See `optimizeDeps.exclude` in vite.config.ts.
 */

setWorkerUrl(maplibreWorkerUrl)

const STYLE_URL = import.meta.env.VITE_MAP_STYLE as string | undefined

const OSM_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      attribution: '© OpenStreetMap contributors',
    },
  },
  layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
}

const SOURCE_ID = 'dq-points'
const LAYER_ID = 'dq-points-circles'

type Bounds = [number, number, number, number]
type PointFeature = {
  type: 'Feature'
  geometry: { type: 'Point'; coordinates: [number, number] }
  properties: { value: number }
}
type FeatureCollection = { type: 'FeatureCollection'; features: PointFeature[] }

function num(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

function plottable(lat: number, lng: number): boolean {
  return lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180
}

/**
 * Frame the middle 98% of the points rather than all of them.
 *
 * A single bad coordinate — a GPS dropout recorded as (0,0), which real taxi
 * data contains — sits thousands of miles from everything else, and fitting to
 * min/max stretches the viewport across an ocean until the real data is a
 * sub-pixel dot. Outliers are still drawn; the viewport just stops chasing them.
 */
function percentileBounds(lats: number[], lngs: number[]): Bounds | null {
  if (!lats.length) return null
  const span = (values: number[]): [number, number] => {
    const sorted = [...values].sort((a, b) => a - b)
    const at = (q: number) =>
      sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))))]
    return [at(0.01), at(0.99)]
  }
  const [minLat, maxLat] = span(lats)
  const [minLng, maxLng] = span(lngs)
  // Pad so edge points are not flush against the frame, with a floor so a single
  // point does not produce a zero-area box that fitBounds cannot use.
  const padLat = Math.max((maxLat - minLat) * 0.05, 0.002)
  const padLng = Math.max((maxLng - minLng) * 0.05, 0.002)
  return [minLng - padLng, minLat - padLat, maxLng + padLng, maxLat + padLat]
}

export function MapLibreRenderer({ spec, data, height = 420 }: RendererProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const [loaded, setLoaded] = useState(false)

  const cfg = spec.spec as {
    lat_field?: string
    lng_field?: string
    value_field?: string | null
    color?: string
  }
  const latField = cfg.lat_field ?? 'lat'
  const lngField = cfg.lng_field ?? 'lng'
  const valueField = cfg.value_field ?? null
  const color = cfg.color ?? '#4269d0'
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
    const timer = setInterval(() => setFrameIndex((i) => (i + 1) % frames.length), 1000 / fps)
    return () => clearInterval(timer)
  }, [playing, frames.length, animate?.fps])

  const visible = useMemo(() => {
    if (!animate || frames.length === 0) return data
    const current = frames[frameIndex]
    return data.filter((row) => String(row[animate.field]) === current)
  }, [animate, data, frames, frameIndex])

  // --- points for the current frame -------------------------------------
  const { geojson, maxValue } = useMemo(() => {
    const features: PointFeature[] = []
    let max = 1
    for (const row of visible) {
      const lat = num(row[latField])
      const lng = num(row[lngField])
      if (lat === null || lng === null || !plottable(lat, lng)) continue
      const value = valueField ? (num(row[valueField]) ?? 1) : 1
      max = Math.max(max, value)
      features.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [lng, lat] },
        properties: { value },
      })
    }
    const collection: FeatureCollection = { type: 'FeatureCollection', features }
    return { geojson: collection, maxValue: max }
  }, [visible, latField, lngField, valueField])

  // Fit to *all* the data, not just the visible frame, so the viewport holds
  // still while the animation plays inside it.
  const bounds = useMemo(() => {
    const lats: number[] = []
    const lngs: number[] = []
    for (const row of data) {
      const lat = num(row[latField])
      const lng = num(row[lngField])
      if (lat === null || lng === null || !plottable(lat, lng)) continue
      lats.push(lat)
      lngs.push(lng)
    }
    return percentileBounds(lats, lngs)
  }, [data, latField, lngField])

  // --- the map ----------------------------------------------------------
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return
    const map = new MapLibreMap({
      container: containerRef.current,
      style: STYLE_URL ?? OSM_STYLE,
      center: [0, 20],
      zoom: 1,
      attributionControl: { compact: true },
    })
    mapRef.current = map
    map.on('load', () => setLoaded(true))
    return () => {
      map.remove()
      mapRef.current = null
      setLoaded(false)
    }
  }, [])

  // Add the layer once the style exists, then keep its data in sync. A layer
  // added before 'load' is silently dropped — exactly the failure this component
  // used to have.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded) return

    const existing = map.getSource(SOURCE_ID) as GeoJSONSource | undefined
    if (existing) {
      existing.setData(geojson as never)
      return
    }
    map.addSource(SOURCE_ID, { type: 'geojson', data: geojson as never })
    map.addLayer({
      id: LAYER_ID,
      type: 'circle',
      source: SOURCE_ID,
      paint: {
        // Scale by value only when values actually vary. Aggregated maps often
        // come back with every count equal to 1; ramping between 0 and 1 then
        // inflates every dot to the top of the scale and the map turns into a
        // solid blob.
        'circle-radius': valueField && maxValue > 1
          ? ([
              'interpolate',
              ['linear'],
              ['get', 'value'],
              1,
              3,
              maxValue,
              14,
            ] as never)
          : 4,
        'circle-color': color,
        'circle-opacity': 0.65,
        'circle-stroke-width': 0.5,
        'circle-stroke-color': '#ffffff',
      },
    })
  }, [loaded, geojson, valueField, maxValue, color])

  // Fit once the bounds are known; re-fitting every frame would fight the user.
  const fitted = useRef(false)
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded || fitted.current || !bounds) return
    fitted.current = true
    map.fitBounds(
      [
        [bounds[0], bounds[1]],
        [bounds[2], bounds[3]],
      ],
      { padding: 30, duration: 0, maxZoom: 15 },
    )
  }, [loaded, bounds])

  const dropped = visible.length - geojson.features.length

  return (
    <div>
      <div
        ref={containerRef}
        style={{ height }}
        className="overflow-hidden rounded border border-slate-200 bg-slate-100"
      />

      {!data.length && <p className="mt-1 text-sm text-slate-500">No geographic data</p>}

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
            aria-label="Animation frame"
          />
          <span className="min-w-32 font-mono text-xs text-slate-600">
            {frames[frameIndex]?.slice(0, 19) ?? ''}
          </span>
        </div>
      )}

      <div className="mt-1 text-xs text-slate-500">
        {geojson.features.length.toLocaleString()} points
        {animate && frames.length > 1 ? ` · frame ${frameIndex + 1}/${frames.length}` : ''}
        {dropped > 0 && (
          <span className="ml-1 text-amber-700">
            · {dropped.toLocaleString()} skipped (unplottable coordinates)
          </span>
        )}
      </div>
    </div>
  )
}
