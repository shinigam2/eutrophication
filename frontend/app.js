(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const params = new URLSearchParams(window.location.search || '');
  const API = (
    params.get('api') ||
    (window.location.protocol && window.location.protocol.startsWith('http')
      ? window.location.origin
      : 'http://127.0.0.1:8000')
  ).replace(/\/$/, '');

  const MONTHS_GEN = [
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
  ];

  const STRAT_RU = {
    dimictic_deep: 'Димиктическое (глубокое)',
    weakly_stratified: 'Слабо стратифицированное',
    polymictic_shallow: 'Полимиктическое (мелководное)',
  };

  const STATUS_RU = {
    valid: 'Чистое наблюдение',
    interpolated_1step: '1-шаговая интерполяция',
    cloudy_missing: 'Сплошная облачность',
    ice_cover: 'Ледяной покров',
    insufficient_pixels: 'Недостаточно пикселей воды',
  };

  const SPEEDS = [
    { label: '1×', ms: 500 },
    { label: '2×', ms: 240 },
    { label: '4×', ms: 110 },
  ];

  const DASH_SEQUENCE = [
    [0, 2, 2.5],
    [0.5, 2, 2],
    [1.0, 2, 1.5],
    [1.5, 2, 1.0],
    [2.0, 2, 0.5],
    [2.5, 2, 0],
    [0, 0.5, 2.5, 1.5],
    [0, 1.0, 2.5, 1.0],
    [0, 1.5, 2.5, 0.5],
  ];

  const FALLBACK_STYLE = {
    version: 8,
    sources: {},
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#0b101b' } }],
  };

  const BASEMAPS = {
    dark: 'https://tiles.openfreemap.org/styles/dark',
    light: 'https://tiles.openfreemap.org/styles/positron',
    satellite: {
      version: 8,
      sources: {
        'esri-imagery': {
          type: 'raster',
          tiles: [
            'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
          ],
          tileSize: 256,
          maxzoom: 18,
          attribution: '© Esri, Maxar, Earthstar Geographics',
        },
      },
      layers: [{ id: 'esri-imagery-layer', type: 'raster', source: 'esri-imagery' }],
    },
    hybrid: {
      version: 8,
      sources: {
        'esri-imagery': {
          type: 'raster',
          tiles: [
            'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
          ],
          tileSize: 256,
          maxzoom: 18,
          attribution: '© Esri, Maxar, Earthstar Geographics',
        },
        'esri-roads': {
          type: 'raster',
          tiles: [
            'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}',
          ],
          tileSize: 256,
          maxzoom: 18,
        },
        'esri-labels': {
          type: 'raster',
          tiles: [
            'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
          ],
          tileSize: 256,
          maxzoom: 18,
        },
      },
      layers: [
        { id: 'esri-imagery-layer', type: 'raster', source: 'esri-imagery' },
        { id: 'esri-roads-layer', type: 'raster', source: 'esri-roads', paint: { 'raster-opacity': 0.85 } },
        { id: 'esri-labels-layer', type: 'raster', source: 'esri-labels', paint: { 'raster-opacity': 0.95 } },
      ],
    },
  };

  const S = {
    root: null,
    lakes: [],
    lakesById: {},
    timeline: null,
    mode: 'satellite',
    basemap: 'dark',
    theme: 'chl',          // 'chl' | 'tsi' | 'coverage'
    season: 'open',        // 'all' | 'open' | 'summer'
    speedIdx: 0,           // index in SPEEDS
    date: null,
    showCascades: true,
    showBuffer: true,
    playing: false,
    playTimer: null,
    dashTimer: null,
    dashStep: 0,
    selected: null,
    compareId: null,
    activeTab: 'overview',
    histWin: 108,
    pickerOpen: false,
    searchQuery: '',
    predictCache: new Map(),
    historyCache: new Map(),
    activePredict: null,
    activeHistory: null,
    compareHistory: null,
    predictSeq: 0,
    historySeq: 0,
    debounceTimer: null,
    map: null,
    mapStyleReady: false,
    markers: {},
    chart: null,
    defaultView: { center: [26.855, 54.915], zoom: 10.95, pitch: 32, bearing: -8 },
  };

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function shortLakeName(name) {
    return String(name || '')
      .replace(/^Озеро\s+/i, '')
      .replace(/\s*\([^)]*\)/g, '')
      .trim();
  }

  function fmtDateRu(iso) {
    if (!iso || iso.length < 10) return '—';
    const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
    if (!y || !m || !d) return iso;
    return `${d} ${MONTHS_GEN[m - 1] || ''} ${y}`;
  }

  function fmtNum(v, digits = 1, suffix = '') {
    if (v == null || Number.isNaN(Number(v))) return '—';
    return `${Number(v).toFixed(digits)}${suffix}`;
  }

  // Smooth RGB color ramp interpolation for vivid, honest real-time visual feedback
  function hexToRgb(hex) {
    const h = hex.replace('#', '');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }

  function rgbToHex(r, g, b) {
    const clamp = (n) => Math.max(0, Math.min(255, Math.round(n)));
    return '#' + [clamp(r), clamp(g), clamp(b)].map((v) => v.toString(16).padStart(2, '0')).join('');
  }

  function lerpRamp(val, stops) {
    if (val <= stops[0][0]) return stops[0][1];
    for (let i = 1; i < stops.length; i += 1) {
      const [v0, c0] = stops[i - 1];
      const [v1, c1] = stops[i];
      if (val <= v1) {
        const t = (val - v0) / Math.max(1e-6, v1 - v0);
        const [r0, g0, b0] = hexToRgb(c0);
        const [r1, g1, b1] = hexToRgb(c1);
        return rgbToHex(r0 + (r1 - r0) * t, g0 + (g1 - g0) * t, b0 + (b1 - b0) * t);
      }
    }
    return stops[stops.length - 1][1];
  }

  const CHL_STOPS = [
    [2.0, '#0ea5e9'],   // глубокий сапфир (ультрачистая вода)
    [5.0, '#22d3ee'],   // чистый циан
    [8.5, '#2dd4bf'],   // бирюза
    [11.5, '#34d399'],  // изумруд
    [14.2, '#a3e635'],  // лайм (подход к порогу внимания)
    [16.0, '#fbbf24'],  // янтарь (уровень внимания 15–25)
    [21.5, '#fb923c'],  // оранжевый
    [25.5, '#fb7185'],  // кораллово-алый (цветение >=25)
    [40.0, '#e11d48'],  // малиново-красный (сильная вспышка)
  ];

  const TSI_STOPS = [
    [34, '#0ea5e9'],
    [40, '#22d3ee'],
    [48, '#34d399'],
    [54, '#a3e635'],
    [58, '#fbbf24'],
    [64, '#fb923c'],
    [70, '#fb7185'],
    [80, '#e11d48'],
  ];

  const COV_STOPS = [
    [30, '#fb7185'],
    [50, '#fbbf24'],
    [70, '#22d3ee'],
    [95, '#34d399'],
  ];

  async function apiGet(path) {
    const resp = await fetch(API + path);
    let data = null;
    try {
      data = await resp.json();
    } catch (_) {
      data = null;
    }
    if (!resp.ok) {
      const msg = (data && (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))) || `HTTP ${resp.status}`;
      const err = new Error(msg);
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  async function apiPost(path, body) {
    const resp = await fetch(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = null;
    try {
      data = await resp.json();
    } catch (_) {
      data = null;
    }
    if (!resp.ok) {
      const msg = (data && (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))) || `HTTP ${resp.status}`;
      const err = new Error(msg);
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  function bboxToPolygon(bbox) {
    if (!bbox) return null;
    const { min_lon, min_lat, max_lon, max_lat } = bbox;
    return {
      type: 'Polygon',
      coordinates: [[
        [min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat],
      ]],
    };
  }

  function getLakeCenter(lake) {
    if (lake && lake.lon != null && lake.lat != null) return [Number(lake.lon), Number(lake.lat)];
    if (lake && lake.bbox) {
      return [(lake.bbox.min_lon + lake.bbox.max_lon) / 2, (lake.bbox.min_lat + lake.bbox.max_lat) / 2];
    }
    return [26.855, 54.915];
  }

  function evalSnapshot(lake, snap) {
    if (!snap) {
      return {
        color: '#64748b',
        badgeClass: 'badge-muted',
        statusLabel: 'Нет данных',
        shortVal: '—',
        trend: 0,
        delta: null,
        category: 'nodata',
        sortScore: -2,
        isForecast: false,
      };
    }
    if (snap.s === 'ice_cover') {
      return {
        color: '#93c5fd',
        badgeClass: 'badge-ice',
        statusLabel: 'Ледяной покров',
        shortVal: 'Лёд',
        trend: 0,
        delta: null,
        category: 'ice',
        sortScore: -1,
        isForecast: false,
      };
    }
    if (snap.s === 'cloudy_missing' || snap.s === 'insufficient_pixels') {
      return {
        color: '#64748b',
        badgeClass: 'badge-muted',
        statusLabel: STATUS_RU[snap.s] || 'Нет наблюдения',
        shortVal: S.theme === 'coverage' ? '0%' : 'Облака',
        trend: 0,
        delta: null,
        category: 'nodata',
        sortScore: -1.5,
        isForecast: false,
      };
    }

    const dc = snap.dc != null ? Number(snap.dc) : null;
    const trend = dc == null ? 0 : (dc >= 1.0 ? 1 : (dc <= -1.0 ? -1 : 0));
    const prefix = snap.s === 'interpolated_1step' ? '~' : '';

    // Thematic layer 3: Satellite coverage (%)
    if (S.theme === 'coverage') {
      if (snap.s === 'interpolated_1step') {
        return {
          color: '#c084fc',
          badgeClass: 'badge-warn',
          statusLabel: 'Интерполяция (1 шаг)',
          shortVal: 'Интерп.',
          trend: 0,
          delta: dc,
          category: 'interp',
          sortScore: 30,
          isForecast: false,
        };
      }
      const cov = snap.cov != null ? Number(snap.cov) : (snap.c != null ? 100 : null);
      if (cov == null) {
        return {
          color: '#64748b',
          badgeClass: 'badge-muted',
          statusLabel: 'Нет данных',
          shortVal: '—',
          trend: 0,
          delta: dc,
          category: 'nodata',
          sortScore: -2,
          isForecast: false,
        };
      }
      return {
        color: lerpRamp(cov, COV_STOPS),
        badgeClass: cov >= 75 ? 'badge-meso' : cov >= 50 ? 'badge-oligo' : 'badge-warn',
        statusLabel: `Чистой воды: ${Math.round(cov)}%`,
        shortVal: `${Math.round(cov)}%`,
        trend: 0,
        delta: dc,
        category: cov >= 75 ? 'high' : cov >= 50 ? 'mid' : 'low',
        sortScore: cov,
        isForecast: false,
      };
    }

    // Thematic layer 2: Carlson Trophic State Index (TSI, factual on selected date)
    if (S.theme === 'tsi') {
      const tsi = snap.t != null ? Number(snap.t) : null;
      if (tsi == null) {
        return {
          color: '#64748b',
          badgeClass: 'badge-muted',
          statusLabel: 'Нет TSI',
          shortVal: '—',
          trend: 0,
          delta: dc,
          category: 'nodata',
          sortScore: -2,
          isForecast: false,
        };
      }
      let statusLabel = 'Эвтрофное (50–70)';
      let badgeClass = 'badge-warn';
      let category = 'eutro';
      if (tsi < 40) { statusLabel = 'Олиготрофное (<40)'; badgeClass = 'badge-oligo'; category = 'oligo'; }
      else if (tsi < 50) { statusLabel = 'Мезотрофное (40–50)'; badgeClass = 'badge-meso'; category = 'meso'; }
      else if (tsi >= 70) { statusLabel = 'Гиперэвтрофное (≥70)'; badgeClass = 'badge-eutro'; category = 'hyper'; }
      return {
        color: lerpRamp(tsi, TSI_STOPS),
        badgeClass,
        statusLabel,
        shortVal: `${prefix}TSI ${tsi.toFixed(0)}`,
        trend,
        delta: dc,
        category,
        sortScore: tsi,
        isForecast: false,
      };
    }

    // Thematic layer 1 (default): Factual Chlorophyll-a on selected date (forecast +10d is shown in lake drawer)
    const bloomThr = (lake && lake.bloom_ug_l) || 25;

    if (snap.c == null) {
      return {
        color: '#64748b',
        badgeClass: 'badge-muted',
        statusLabel: 'Нет данных',
        shortVal: '—',
        trend: 0,
        delta: null,
        category: 'nodata',
        sortScore: -2,
        isForecast: false,
      };
    }

    const c = Number(snap.c);
    const color = lerpRamp(c, CHL_STOPS);
    const shortVal = `${prefix}${c.toFixed(1)}`;
    if (c >= bloomThr) {
      return {
        color,
        badgeClass: 'badge-eutro',
        statusLabel: `Предупреждение (факт ≥${bloomThr})`,
        shortVal,
        trend,
        delta: dc,
        category: 'red',
        sortScore: 1000 + c,
        isForecast: false,
      };
    }
    if (c < 5) {
      return {
        color,
        badgeClass: 'badge-oligo',
        statusLabel: 'Норма · чистая вода (<5)',
        shortVal,
        trend,
        delta: dc,
        category: 'green',
        sortScore: c,
        isForecast: false,
      };
    }
    return {
      color,
      badgeClass: 'badge-meso',
      statusLabel: `Норма (факт <${bloomThr})`,
      shortVal,
      trend,
      delta: dc,
      category: 'green',
      sortScore: c,
      isForecast: false,
    };
  }

  function getActiveSnapshot(lakeId) {
    if (!S.timeline || !S.date || !S.timeline.snapshots) return null;
    const dayMap = S.timeline.snapshots[S.date];
    return dayMap ? dayMap[lakeId] || null : null;
  }

  function getTimelineDates() {
    if (!S.timeline || !Array.isArray(S.timeline.dates)) return [];
    const all = S.timeline.dates;
    if (S.mode !== 'satellite' || S.season === 'all') return all;
    if (S.season === 'summer') {
      const summer = all.filter((d) => {
        const m = Number(d.slice(5, 7));
        return m >= 6 && m <= 9;
      });
      return summer.length ? summer : all;
    }
    const open = all.filter((d) => {
      const m = Number(d.slice(5, 7));
      if (m >= 4 && m <= 11) return true;
      const day = S.timeline.snapshots[d] || {};
      return Object.values(day).some((v) => v && v.s !== 'ice_cover');
    });
    return open.length ? open : all;
  }

  function computeDefaultView() {
    const pts = S.lakes.map(getLakeCenter).filter((p) => p && Number.isFinite(p[0]) && Number.isFinite(p[1]));
    if (!pts.length) return S.defaultView;
    const lons = pts.map((p) => p[0]);
    const lats = pts.map((p) => p[1]);
    const cx = (Math.min(...lons) + Math.max(...lons)) / 2;
    const cy = (Math.min(...lats) + Math.max(...lats)) / 2;
    const span = Math.max(Math.max(...lons) - Math.min(...lons), Math.max(...lats) - Math.min(...lats));
    const zoom = span > 0.6 ? 8.8 : span > 0.25 ? 10.7 : 11.2;
    return { center: [cx, cy], zoom, pitch: 32, bearing: -8 };
  }

  // Cascade connectivity helper: finds all cascade links & lakes in the same chain as selected lake
  function getConnectedCascadeSet(lakeId) {
    const links = (S.timeline && S.timeline.cascade_links) || [];
    const nodes = new Set();
    if (!lakeId) return nodes;
    nodes.add(lakeId);
    let changed = true;
    while (changed) {
      changed = false;
      for (const lk of links) {
        if (nodes.has(lk.from) && !nodes.has(lk.to)) { nodes.add(lk.to); changed = true; }
        if (nodes.has(lk.to) && !nodes.has(lk.from)) { nodes.add(lk.from); changed = true; }
      }
    }
    return nodes.size > 1 ? nodes : new Set();
  }

  // =========================================================================
  // MapLibre GL JS initialization, 4-basemap switching & rich highlight layers
  // =========================================================================
  function harmonizeStyle() {
    const map = S.map;
    if (!map) return;
    if (S.basemap === 'dark') {
      try {
        if (map.getLayer('background')) map.setPaintProperty('background', 'background-color', '#0b101b');
        if (map.getLayer('water')) map.setPaintProperty('water', 'fill-color', '#111c30');
        if (map.getLayer('water_name')) map.setPaintProperty('water_name', 'text-opacity', 0.45);
      } catch (_) {
        // Ignore style layer differences
      }
    }
  }

  function buildLakesFeatureCollection() {
    const features = [];
    for (const lake of S.lakes) {
      const geom = lake.geometry || bboxToPolygon(lake.bbox);
      if (!geom) continue;
      const snap = getActiveSnapshot(lake.id);
      const ev = evalSnapshot(lake, snap);
      features.push({
        type: 'Feature',
        properties: {
          id: lake.id,
          name: lake.name,
          short_name: shortLakeName(lake.name),
          area_km2: lake.area_km2 || 1,
          color: ev.color,
          status_label: ev.statusLabel,
          obs_status: snap ? snap.s : 'missing',
          chl_current: snap && snap.c != null ? snap.c : null,
          chl_forecast: snap && snap.f != null ? snap.f : null,
          tsi: snap && snap.t != null ? snap.t : null,
          is_interp: Boolean(snap && snap.s === 'interpolated_1step'),
        },
        geometry: geom,
      });
    }
    return { type: 'FeatureCollection', features };
  }

  function buildAnchorsFeatureCollection(lakesFC) {
    const features = lakesFC.features.map((f) => {
      const lake = S.lakesById[f.properties.id];
      const center = getLakeCenter(lake);
      return {
        type: 'Feature',
        properties: { ...f.properties },
        geometry: { type: 'Point', coordinates: center },
      };
    });
    return { type: 'FeatureCollection', features };
  }

  function buildCascadeFeatureCollection() {
    const links = (S.timeline && S.timeline.cascade_links) || [];
    const chainNodes = getConnectedCascadeSet(S.selected);
    const lineFeatures = [];
    const arrowFeatures = [];

    for (const lk of links) {
      const isActive = chainNodes.has(lk.from) && chainNodes.has(lk.to);
      lineFeatures.push({
        type: 'Feature',
        properties: { from: lk.from, to: lk.to, label: lk.label, active: isActive ? 1 : 0 },
        geometry: { type: 'LineString', coordinates: lk.coordinates },
      });

      // Directional chevron arrow triangle at midpoint pointing downstream (from -> to)
      if (Array.isArray(lk.coordinates) && lk.coordinates.length >= 2) {
        const [x0, y0] = lk.coordinates[0];
        const [x1, y1] = lk.coordinates[lk.coordinates.length - 1];
        const mx = (x0 + x1) / 2;
        const my = (y0 + y1) / 2;
        const cosLat = Math.cos(((y0 + y1) / 2) * (Math.PI / 180));
        const dx = (x1 - x0) * cosLat;
        const dy = y1 - y0;
        const len = Math.hypot(dx, dy) || 1;
        const ux = dx / len;
        const uy = dy / len;
        const px = -uy;
        const py = ux;
        const sz = isActive ? 0.0052 : 0.0040;
        const tip = [mx + (ux * sz) / cosLat, my + uy * sz];
        const left = [mx + ((-ux * sz * 0.6 + px * sz * 0.65) / cosLat), my + (-uy * sz * 0.6 + py * sz * 0.65)];
        const right = [mx + ((-ux * sz * 0.6 - px * sz * 0.65) / cosLat), my + (-uy * sz * 0.6 - py * sz * 0.65)];
        arrowFeatures.push({
          type: 'Feature',
          properties: { active: isActive ? 1 : 0 },
          geometry: { type: 'Polygon', coordinates: [[tip, left, right, tip]] },
        });
      }
    }
    return {
      lines: { type: 'FeatureCollection', features: lineFeatures },
      arrows: { type: 'FeatureCollection', features: arrowFeatures },
    };
  }

  function buildSingleLakeFC(lakeId) {
    const lake = lakeId ? S.lakesById[lakeId] : null;
    if (!lake) return { type: 'FeatureCollection', features: [] };
    const geom = lake.geometry || bboxToPolygon(lake.bbox);
    if (!geom) return { type: 'FeatureCollection', features: [] };
    const ev = evalSnapshot(lake, getActiveSnapshot(lake.id));
    return {
      type: 'FeatureCollection',
      features: [{
        type: 'Feature',
        properties: { id: lake.id, color: ev.color },
        geometry: geom,
      }],
    };
  }

  function buildSelectedBufferFC() {
    const lake = S.showBuffer && S.selected ? S.lakesById[S.selected] : null;
    if (!lake || !lake.buffer_geometry) return { type: 'FeatureCollection', features: [] };
    return {
      type: 'FeatureCollection',
      features: [{
        type: 'Feature',
        properties: { id: lake.id },
        geometry: lake.buffer_geometry,
      }],
    };
  }

  function ensureCustomLayers() {
    const map = S.map;
    if (!map) return;

    const lakesFC = buildLakesFeatureCollection();
    const anchorsFC = buildAnchorsFeatureCollection(lakesFC);
    const cascade = buildCascadeFeatureCollection();
    const selFC = buildSingleLakeFC(S.selected);
    const cmpFC = buildSingleLakeFC(S.compareId);
    const bufFC = buildSelectedBufferFC();
    const isImagery = S.basemap === 'satellite' || S.basemap === 'hybrid';

    if (!map.getSource('cascade-flows')) map.addSource('cascade-flows', { type: 'geojson', data: cascade.lines });
    if (!map.getSource('cascade-arrows')) map.addSource('cascade-arrows', { type: 'geojson', data: cascade.arrows });
    if (!map.getSource('lakes')) map.addSource('lakes', { type: 'geojson', data: lakesFC });
    if (!map.getSource('lake-anchors')) map.addSource('lake-anchors', { type: 'geojson', data: anchorsFC });
    if (!map.getSource('selected-lake')) map.addSource('selected-lake', { type: 'geojson', data: selFC });
    if (!map.getSource('compared-lake')) map.addSource('compared-lake', { type: 'geojson', data: cmpFC });
    if (!map.getSource('selected-buffer')) map.addSource('selected-buffer', { type: 'geojson', data: bufFC });

    if (!map.getLayer('cascade-active-glow')) {
      map.addLayer({
        id: 'cascade-active-glow',
        type: 'line',
        source: 'cascade-flows',
        filter: ['==', ['get', 'active'], 1],
        paint: {
          'line-color': '#22d3ee',
          'line-width': 8,
          'line-blur': 6,
          'line-opacity': S.showCascades ? 0.55 : 0.0,
        },
      });
    }

    if (!map.getLayer('cascade-lines')) {
      map.addLayer({
        id: 'cascade-lines',
        type: 'line',
        source: 'cascade-flows',
        paint: {
          'line-color': ['case', ['==', ['get', 'active'], 1], '#22d3ee', '#67e8f9'],
          'line-width': ['case', ['==', ['get', 'active'], 1], 3.4, 2.0],
          'line-dasharray': DASH_SEQUENCE[0],
          'line-opacity': S.showCascades ? 0.85 : 0.0,
        },
      });
    }

    if (!map.getLayer('cascade-arrow-heads')) {
      map.addLayer({
        id: 'cascade-arrow-heads',
        type: 'fill',
        source: 'cascade-arrows',
        paint: {
          'fill-color': ['case', ['==', ['get', 'active'], 1], '#ffffff', '#67e8f9'],
          'fill-opacity': S.showCascades ? 0.92 : 0.0,
        },
      });
    }

    if (!map.getLayer('lakes-glow')) {
      map.addLayer({
        id: 'lakes-glow',
        type: 'line',
        source: 'lakes',
        paint: {
          'line-color': ['coalesce', ['get', 'color'], '#22d3ee'],
          'line-width': isImagery ? 8 : 7,
          'line-blur': 6,
          'line-opacity': isImagery ? 0.52 : 0.38,
        },
      });
    }

    if (!map.getLayer('lakes-fill')) {
      map.addLayer({
        id: 'lakes-fill',
        type: 'fill',
        source: 'lakes',
        paint: {
          'fill-color': ['coalesce', ['get', 'color'], '#22d3ee'],
          'fill-opacity': isImagery ? 0.40 : 0.76,
        },
      });
    }

    if (!map.getLayer('lakes-outline')) {
      map.addLayer({
        id: 'lakes-outline',
        type: 'line',
        source: 'lakes',
        paint: {
          'line-color': isImagery ? '#f8fafc' : '#cbd5e1',
          'line-width': isImagery ? 1.6 : 1.2,
          'line-opacity': isImagery ? 0.85 : 0.72,
        },
      });
    }

    // Real -25m Sentinel-2 shore exclusion buffer on selected lake
    if (!map.getLayer('selected-buffer-fill')) {
      map.addLayer({
        id: 'selected-buffer-fill',
        type: 'fill',
        source: 'selected-buffer',
        paint: {
          'fill-color': '#22d3ee',
          'fill-opacity': 0.14,
        },
      });
    }

    if (!map.getLayer('selected-buffer-line')) {
      map.addLayer({
        id: 'selected-buffer-line',
        type: 'line',
        source: 'selected-buffer',
        paint: {
          'line-color': '#e0f2fe',
          'line-width': 1.6,
          'line-dasharray': [3, 2],
          'line-opacity': 0.92,
        },
      });
    }

    // Compared lake highlight (violet dashed halo + outline)
    if (!map.getLayer('compared-lake-halo')) {
      map.addLayer({
        id: 'compared-lake-halo',
        type: 'line',
        source: 'compared-lake',
        paint: {
          'line-color': '#e879f9',
          'line-width': 11,
          'line-blur': 7,
          'line-opacity': 0.65,
        },
      });
    }

    if (!map.getLayer('compared-lake-outline')) {
      map.addLayer({
        id: 'compared-lake-outline',
        type: 'line',
        source: 'compared-lake',
        paint: {
          'line-color': '#f0abfc',
          'line-width': 2.8,
          'line-dasharray': [4, 2],
          'line-opacity': 0.95,
        },
      });
    }

    // Selected lake unmistakable luminous area highlight
    if (!map.getLayer('selected-lake-halo')) {
      map.addLayer({
        id: 'selected-lake-halo',
        type: 'line',
        source: 'selected-lake',
        paint: {
          'line-color': ['coalesce', ['get', 'color'], '#22d3ee'],
          'line-width': 15,
          'line-blur': 10,
          'line-opacity': 0.78,
        },
      });
    }

    if (!map.getLayer('selected-lake-outline')) {
      map.addLayer({
        id: 'selected-lake-outline',
        type: 'line',
        source: 'selected-lake',
        paint: {
          'line-color': '#ffffff',
          'line-width': 3.4,
          'line-opacity': 0.98,
        },
      });
    }

    if (!map.getLayer('lake-anchors-circle')) {
      map.addLayer({
        id: 'lake-anchors-circle',
        type: 'circle',
        source: 'lake-anchors',
        paint: {
          'circle-radius': ['case', ['<', ['get', 'area_km2'], 0.8], 6, 4.2],
          'circle-color': ['coalesce', ['get', 'color'], '#22d3ee'],
          'circle-stroke-color': '#070b12',
          'circle-stroke-width': 1.5,
          'circle-opacity': 0.94,
        },
      });
    }
  }

  function syncMarkers() {
    const map = S.map;
    if (!map || typeof maplibregl === 'undefined') return;

    const activeIds = new Set(S.lakes.map((l) => l.id));
    for (const id of Object.keys(S.markers)) {
      if (!activeIds.has(id)) {
        S.markers[id].marker.remove();
        delete S.markers[id];
      }
    }

    for (const lake of S.lakes) {
      const snap = getActiveSnapshot(lake.id);
      const ev = evalSnapshot(lake, snap);
      const isSelected = lake.id === S.selected;
      const isCompared = lake.id === S.compareId;
      const center = getLakeCenter(lake);

      if (!S.markers[lake.id]) {
        const el = document.createElement('div');
        el.className = 'lake-marker-pill';
        const titleSpan = document.createElement('span');
        titleSpan.textContent = shortLakeName(lake.name);
        const valSpan = document.createElement('span');
        valSpan.className = 'lake-marker-val';
        const trendSpan = document.createElement('span');
        trendSpan.className = 'lake-marker-trend';
        el.appendChild(titleSpan);
        el.appendChild(valSpan);
        el.appendChild(trendSpan);
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          selectLake(lake.id);
        });
        const marker = new maplibregl.Marker({ element: el, anchor: 'bottom' })
          .setLngLat(center)
          .addTo(map);
        S.markers[lake.id] = { marker, el, valSpan, trendSpan, titleSpan };
      }

      const entry = S.markers[lake.id];
      entry.marker.setLngLat(center);
      entry.titleSpan.textContent = shortLakeName(lake.name);
      entry.valSpan.textContent = ev.shortVal;
      entry.valSpan.style.color = ev.color;
      if (ev.trend > 0) {
        entry.trendSpan.textContent = '↗';
        entry.trendSpan.className = 'lake-marker-trend up';
      } else if (ev.trend < 0) {
        entry.trendSpan.textContent = '↘';
        entry.trendSpan.className = 'lake-marker-trend down';
      } else {
        entry.trendSpan.textContent = '';
        entry.trendSpan.className = 'lake-marker-trend';
      }

      entry.el.classList.toggle('selected', isSelected);
      entry.el.classList.toggle('compared', isCompared);
      entry.el.style.borderColor = isSelected ? ev.color : (isCompared ? '#e879f9' : 'rgba(148, 163, 184, 0.28)');
      entry.el.style.boxShadow = isSelected
        ? `0 0 22px ${ev.color}88`
        : (isCompared ? '0 0 16px rgba(232, 121, 249, 0.55)' : '0 4px 12px rgba(0, 0, 0, 0.45)');
      entry.el.style.zIndex = isSelected ? '20' : (isCompared ? '14' : '2');
    }
  }

  function isMapStyleUsable() {
    const map = S.map;
    if (!map) return false;
    if (S.mapStyleReady) return true;
    if (typeof map.isStyleLoaded === 'function' && map.isStyleLoaded()) {
      S.mapStyleReady = true;
      return true;
    }
    return false;
  }

  function syncMapData() {
    // HTML badges always update immediately regardless of WebGL style transition state!
    syncMarkers();

    const map = S.map;
    if (!map || !isMapStyleUsable()) return;

    ensureCustomLayers();
    const lakesFC = buildLakesFeatureCollection();
    const anchorsFC = buildAnchorsFeatureCollection(lakesFC);
    const cascade = buildCascadeFeatureCollection();
    const selFC = buildSingleLakeFC(S.selected);
    const cmpFC = buildSingleLakeFC(S.compareId);
    const bufFC = buildSelectedBufferFC();

    const setSrc = (id, data) => {
      const src = map.getSource(id);
      if (src && src.setData) src.setData(data);
    };
    setSrc('lakes', lakesFC);
    setSrc('lake-anchors', anchorsFC);
    setSrc('cascade-flows', cascade.lines);
    setSrc('cascade-arrows', cascade.arrows);
    setSrc('selected-lake', selFC);
    setSrc('compared-lake', cmpFC);
    setSrc('selected-buffer', bufFC);

    const isImagery = S.basemap === 'satellite' || S.basemap === 'hybrid';
    const hasSel = Boolean(S.selected);

    // Highlight selected lake's full water area strongly and subtly dim non-selected lakes
    if (map.getLayer('lakes-fill')) {
      const selOp = isImagery ? 0.54 : 0.88;
      const cmpOp = isImagery ? 0.46 : 0.78;
      const otherOp = hasSel ? (isImagery ? 0.24 : 0.46) : (isImagery ? 0.40 : 0.76);
      map.setPaintProperty('lakes-fill', 'fill-opacity', [
        'case',
        ['==', ['get', 'id'], S.selected || ''], selOp,
        ['==', ['get', 'id'], S.compareId || ''], cmpOp,
        otherOp,
      ]);
    }
    if (map.getLayer('lakes-glow')) {
      const baseGlow = isImagery ? 0.52 : 0.38;
      map.setPaintProperty('lakes-glow', 'line-opacity', [
        'case',
        ['==', ['get', 'id'], S.selected || ''], 0.0, // handled by brighter selected-lake-halo
        hasSel ? baseGlow * 0.55 : baseGlow,
      ]);
    }
    if (map.getLayer('cascade-lines')) {
      map.setPaintProperty('cascade-lines', 'line-opacity', S.showCascades ? 0.85 : 0.0);
    }
    if (map.getLayer('cascade-active-glow')) {
      map.setPaintProperty('cascade-active-glow', 'line-opacity', S.showCascades ? 0.55 : 0.0);
    }
    if (map.getLayer('cascade-arrow-heads')) {
      map.setPaintProperty('cascade-arrow-heads', 'fill-opacity', S.showCascades ? 0.92 : 0.0);
    }
  }

  function startCascadeAnimation() {
    clearInterval(S.dashTimer);
    S.dashTimer = setInterval(() => {
      if (!S.showCascades || !S.map || !S.mapStyleReady) return;
      if (!S.map.getLayer || !S.map.getLayer('cascade-lines')) return;
      S.dashStep = (S.dashStep + 1) % DASH_SEQUENCE.length;
      try {
        S.map.setPaintProperty('cascade-lines', 'line-dasharray', DASH_SEQUENCE[S.dashStep]);
      } catch (_) {
        // Ignore if style is currently mid-swap
      }
    }, 150);
  }

  function hideTooltip() {
    const tip = $('#mapTooltip');
    if (tip) tip.hidden = true;
  }

  function showTooltip(e, props) {
    const tip = $('#mapTooltip');
    if (!tip || !props) return;
    const lake = S.lakesById[props.id];
    const snap = getActiveSnapshot(props.id);
    const ev = evalSnapshot(lake, snap);
    const parts = [];
    if (snap && snap.c != null) parts.push(`Chl-a ${Number(snap.c).toFixed(1)} мкг/л`);
    if (snap && snap.dc != null) parts.push(`${snap.dc > 0 ? '+' : ''}${snap.dc.toFixed(1)}`);
    if (snap && snap.t != null) parts.push(`TSI ${Number(snap.t).toFixed(0)}`);
    if (snap && snap.cov != null) parts.push(`${Math.round(snap.cov)}% воды`);

    tip.innerHTML =
      `<div class="tooltip-title" style="color:${esc(ev.color)}">${esc(lake ? lake.name : props.name)}</div>` +
      `<div class="tooltip-sub">${esc(parts.join(' · ') || ev.statusLabel)}</div>` +
      `<div class="tooltip-note">${esc(ev.statusLabel)} · Площадь ${fmtNum(lake && lake.area_km2, 2, ' км²')}</div>`;
    const left = Math.min(window.innerWidth - 230, (e.point ? e.point.x : 0) + 14);
    const top = Math.max(12, (e.point ? e.point.y : 0) - 54);
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    tip.hidden = false;
  }

  function onMapStyleReady() {
    S.mapStyleReady = true;
    harmonizeStyle();
    syncMapData();
  }

  function initMap() {
    if (typeof maplibregl === 'undefined') return;
    const map = new maplibregl.Map({
      container: 'map',
      style: BASEMAPS[S.basemap],
      attributionControl: { compact: true },
      center: S.defaultView.center,
      zoom: S.defaultView.zoom,
      pitch: S.defaultView.pitch,
      bearing: S.defaultView.bearing,
      maxZoom: 16.5,
      minZoom: 6.5,
    });
    if (maplibregl.NavigationControl) {
      map.addControl(new maplibregl.NavigationControl({ showCompass: true, visualizePitch: true }), 'bottom-right');
    }
    S.map = map;

    map.on('style.load', onMapStyleReady);
    map.on('load', onMapStyleReady);

    // Fallback ONLY if the entire style JSON failed to load, never on transient tile errors
    map.on('error', (ev) => {
      const msg = String((ev && ev.error && ev.error.message) || '');
      const isStyleFailure = !S.mapStyleReady && (msg.includes('style') || msg.includes('Failed to fetch'));
      if (isStyleFailure && S.basemap !== 'fallback') {
        S.basemap = 'fallback';
        map.setStyle(FALLBACK_STYLE, { diff: false });
      }
    });

    const onMove = (e) => {
      if (map.getCanvas) map.getCanvas().style.cursor = 'pointer';
      const feat = e.features && e.features[0];
      if (feat && feat.properties) showTooltip(e, feat.properties);
    };
    const onLeave = () => {
      if (map.getCanvas) map.getCanvas().style.cursor = '';
      hideTooltip();
    };
    const onClick = (e) => {
      const feat = e.features && e.features[0];
      const id = feat && feat.properties && feat.properties.id;
      if (id) selectLake(String(id));
    };

    for (const layerId of ['lakes-fill', 'lake-anchors-circle']) {
      map.on('mousemove', layerId, onMove);
      map.on('mouseleave', layerId, onLeave);
      map.on('click', layerId, onClick);
    }

    startCascadeAnimation();
  }

  function setBasemap(name) {
    if (!BASEMAPS[name] || name === S.basemap) return;
    S.basemap = name;
    if (document.body) document.body.dataset.basemap = name;
    $$('#basemapSwitch .mode-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.basemap === name);
    });
    if (S.map) {
      S.mapStyleReady = false;
      // { diff: false } guarantees MapLibre GL JS v4 always emits style.load reliably!
      S.map.setStyle(BASEMAPS[name], { diff: false });
    }
  }

  function setTheme(theme) {
    if (!['chl', 'tsi', 'coverage'].includes(theme) || theme === S.theme) return;
    S.theme = theme;
    $$('#themeSwitch .theme-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.theme === theme);
    });
    renderLegend();
    renderLakePicker();
    syncMapData();
    if (S.selected) renderDrawerHeader();
  }

  function setSeason(season) {
    if (!['all', 'open', 'summer'].includes(season)) return;
    S.season = season;
    $$('#seasonSwitch .season-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.season === season);
    });
    const dates = getTimelineDates();
    if (dates.length && !dates.includes(S.date)) {
      const target = dates.find((d) => d >= (S.date || '')) || dates[dates.length - 1];
      setDate(target, true);
    } else {
      renderTimelineControls();
    }
  }

  function cycleSpeed() {
    S.speedIdx = (S.speedIdx + 1) % SPEEDS.length;
    const btn = $('#speedBtn');
    if (btn) btn.textContent = SPEEDS[S.speedIdx].label;
    if (S.playing) {
      clearInterval(S.playTimer);
      S.playTimer = setInterval(playTick, SPEEDS[S.speedIdx].ms);
    }
  }

  function flyToLake(lake) {
    if (!S.map || !S.map.flyTo || !lake) return;
    const center = getLakeCenter(lake);
    const area = lake.area_km2 || 2;
    const zoom = area > 30 ? 11.7 : area > 5 ? 12.6 : area > 1 ? 13.4 : 14.0;
    S.map.flyTo({ center, zoom, pitch: 38, duration: 850, essential: true });
  }

  function resetCamera() {
    if (!S.map || !S.map.flyTo) return;
    const v = S.defaultView;
    S.map.flyTo({ center: v.center, zoom: v.zoom, pitch: v.pitch, bearing: v.bearing, duration: 850 });
  }

  // =========================================================================
  // UI Rendering: Legend, Lake Picker, Timeline
  // =========================================================================
  function renderLegend() {
    const counts = {};
    for (const lake of S.lakes) {
      const ev = evalSnapshot(lake, getActiveSnapshot(lake.id));
      counts[ev.category] = (counts[ev.category] || 0) + 1;
    }

    const titleEl = $('#legendTitle');
    let rows = [];
    if (S.theme === 'tsi') {
      if (titleEl) titleEl.textContent = 'Трофность (TSI, факт)';
      rows = [
        { color: '#22d3ee', label: 'Олиготрофное (< 40)', count: counts.oligo || 0 },
        { color: '#34d399', label: 'Мезотрофное (40–50)', count: counts.meso || 0 },
        { color: '#fbbf24', label: 'Эвтрофное (50–70)', count: counts.eutro || 0 },
        { color: '#fb7185', label: 'Гиперэвтрофное (≥ 70)', count: counts.hyper || 0 },
        { color: '#93c5fd', label: 'Лёд / Облака', count: (counts.ice || 0) + (counts.nodata || 0) },
      ];
    } else if (S.theme === 'coverage') {
      if (titleEl) titleEl.textContent = 'Покрытие Sentinel-2 (%)';
      rows = [
        { color: '#34d399', label: 'Отличное (≥ 75%)', count: counts.high || 0 },
        { color: '#22d3ee', label: 'Хорошее (50–75%)', count: counts.mid || 0 },
        { color: '#fbbf24', label: 'Частичное (30–50%)', count: counts.low || 0 },
        { color: '#c084fc', label: 'Интерполяция (1 шаг)', count: counts.interp || 0 },
        { color: '#64748b', label: 'Лёд / Облака', count: (counts.ice || 0) + (counts.nodata || 0) },
      ];
    } else {
      if (titleEl) titleEl.textContent = S.mode === 'lab' ? 'Хлорофилл-а (лаб., факт)' : 'Хлорофилл-а (факт, мкг/л)';
      rows = [
        { color: '#34d399', label: 'Норма (< 25 мкг/л)', count: counts.green || 0 },
        { color: '#fb7185', label: 'Предупреждение (≥ 25)', count: counts.red || 0 },
        { color: '#93c5fd', label: 'Ледяной покров', count: counts.ice || 0 },
        { color: '#64748b', label: 'Облака / нет данных', count: counts.nodata || 0 },
      ];
    }

    const el = $('#legendItems');
    if (el) {
      el.innerHTML = rows.map((r) => (
        `<div class="legend-row">` +
        `<span class="legend-left"><span class="dot-sm" style="background:${r.color}"></span><span>${esc(r.label)}</span></span>` +
        `<span class="legend-count">${r.count}</span>` +
        `</div>`
      )).join('');
    }
  }

  function renderLakePicker() {
    const countEl = $('#pickerCount');
    if (countEl) countEl.textContent = String(S.lakes.length);

    const selLake = S.selected ? S.lakesById[S.selected] : null;
    const labelEl = $('#pickerLabel');
    const dotEl = $('#pickerDot');
    if (selLake) {
      const ev = evalSnapshot(selLake, getActiveSnapshot(selLake.id));
      if (labelEl) labelEl.textContent = shortLakeName(selLake.name);
      if (dotEl) dotEl.style.background = ev.color;
    } else {
      if (labelEl) labelEl.textContent = 'Водоёмы';
      if (dotEl) dotEl.style.background = '#22d3ee';
    }

    const listEl = $('#lakeList');
    if (!listEl) return;
    const q = (S.searchQuery || '').trim().toLowerCase();
    const sorted = S.lakes
      .map((lake) => ({ lake, ev: evalSnapshot(lake, getActiveSnapshot(lake.id)) }))
      .filter((item) => !q || item.lake.name.toLowerCase().includes(q) || item.lake.id.toLowerCase().includes(q))
      .sort((a, b) => b.ev.sortScore - a.ev.sortScore);

    listEl.innerHTML = sorted.length
      ? sorted.map(({ lake, ev }) => {
          const unit = S.theme === 'chl' && Number.isFinite(Number(ev.shortVal.replace('~', ''))) ? ' мкг/л' : '';
          const arrow = ev.trend > 0 ? ' ↗' : (ev.trend < 0 ? ' ↘' : '');
          return (
            `<button type="button" class="lake-dropdown-item${lake.id === S.selected ? ' selected' : ''}" data-id="${esc(lake.id)}">` +
            `<span class="lake-dropdown-name">` +
            `<span class="dot-sm" style="background:${ev.color}"></span>` +
            `<span>${esc(shortLakeName(lake.name))}${lake.protected_area ? ' <span class="lake-dropdown-sub">ООПТ</span>' : ''}</span>` +
            `</span>` +
            `<span class="lake-dropdown-meta" style="color:${ev.color}">${esc(ev.shortVal)}${unit}${arrow}</span>` +
            `</button>`
          );
        }).join('')
      : '<div class="muted-text" style="padding:8px 10px">Совпадений не найдено</div>';
  }

  function renderTimelineControls() {
    const dates = getTimelineDates();
    const slider = $('#timeSlider');
    if (slider) {
      slider.min = '0';
      slider.max = String(Math.max(0, dates.length - 1));
      const idx = dates.indexOf(S.date);
      slider.value = String(idx >= 0 ? idx : Math.max(0, dates.length - 1));
    }
    const dinp = $('#dinp');
    if (dinp && S.date) dinp.value = S.date;

    const dateLabel = $('#activeDateLabel');
    if (dateLabel) dateLabel.textContent = fmtDateRu(S.date);

    const activeYear = S.date ? Number(S.date.slice(0, 4)) : null;
    const years = (S.timeline && S.timeline.years) || [];
    const track = $('#yearTrack');
    if (track) {
      track.innerHTML = years.map((y) => (
        `<button type="button" class="epoch-btn${y === activeYear ? ' active' : ''}" data-year="${y}">${y}</button>`
      )).join('');
    }

    const sumEl = $('#timelineSummary');
    if (sumEl) {
      let valid = 0; let interp = 0; let ice = 0; let cloud = 0;
      for (const lake of S.lakes) {
        const snap = getActiveSnapshot(lake.id);
        if (!snap) continue;
        if (snap.s === 'valid') valid += 1;
        else if (snap.s === 'interpolated_1step') interp += 1;
        else if (snap.s === 'ice_cover') ice += 1;
        else cloud += 1;
      }
      const parts = [];
      if (valid) parts.push(`${valid} чистых`);
      if (interp) parts.push(`${interp} интерп.`);
      if (ice) parts.push(`${ice} лёд`);
      if (cloud) parts.push(`${cloud} облака`);
      const modeTag = S.mode === 'lab' ? 'Лаборатория БГУ' : 'Sentinel-2';
      sumEl.textContent = `${modeTag} · ${parts.join(' · ') || 'нет данных'}`;
    }

    const seasonSw = $('#seasonSwitch');
    if (seasonSw) seasonSw.hidden = S.mode === 'lab';
  }

  function updateAllViewsForDate(fetchDetail = true) {
    renderTimelineControls();
    renderLegend();
    renderLakePicker();
    syncMapData();

    if (S.selected) {
      renderDrawerHeader();
      if (S.activeTab === 'history' && S.chart) {
        S.chart.update('none');
      }
      if (fetchDetail) {
        clearTimeout(S.debounceTimer);
        S.debounceTimer = setTimeout(() => {
          loadSelectedLakeDetail(S.selected, false);
        }, S.playing ? 450 : 120);
      }
    }
  }

  function setDate(isoDate, fetchDetail = true) {
    if (!isoDate || !S.timeline) return;
    const all = S.timeline.dates || [];
    if (!all.length) return;
    let target = isoDate;
    if (!S.timeline.snapshots[target]) {
      const tMs = Date.parse(isoDate);
      if (!Number.isNaN(tMs)) {
        target = all.reduce((best, cand) => (
          Math.abs(Date.parse(cand) - tMs) < Math.abs(Date.parse(best) - tMs) ? cand : best
        ), all[0]);
      } else {
        target = all[all.length - 1];
      }
    }
    S.date = target;
    updateAllViewsForDate(fetchDetail);
  }

  function stepDate(delta) {
    const dates = getTimelineDates();
    if (!dates.length) return;
    let idx = dates.indexOf(S.date);
    if (idx < 0) {
      idx = dates.findIndex((d) => d >= (S.date || ''));
      if (idx < 0) idx = dates.length - 1;
    }
    const nextIdx = Math.max(0, Math.min(dates.length - 1, idx + delta));
    setDate(dates[nextIdx], true);
  }

  function jumpToYear(year) {
    const dates = getTimelineDates();
    const inYear = dates.filter((d) => Number(d.slice(0, 4)) === Number(year));
    if (!inYear.length) return;
    const preferred = inYear.find((d) => d.slice(5) >= '07-21') || inYear[Math.floor(inYear.length / 2)];
    setDate(preferred, true);
  }

  function playTick() {
    const dates = getTimelineDates();
    if (!dates.length) return;
    const idx = dates.indexOf(S.date);
    const nextIdx = (idx < 0 || idx >= dates.length - 1) ? 0 : idx + 1;
    setDate(dates[nextIdx], false);
  }

  function togglePlay() {
    S.playing = !S.playing;
    const btn = $('#playBtn');
    const icon = $('#playIcon');
    if (btn) btn.classList.toggle('playing', S.playing);
    if (icon) {
      icon.innerHTML = S.playing
        ? '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>'
        : '<polygon points="6 4 20 12 6 20 6 4"/>';
    }
    clearInterval(S.playTimer);
    if (S.playing) {
      S.playTimer = setInterval(playTick, SPEEDS[S.speedIdx].ms);
    } else if (S.selected) {
      loadSelectedLakeDetail(S.selected, false);
    }
  }

  function stopPlay() {
    if (S.playing) togglePlay();
  }

  // =========================================================================
  // Lake Inspector Drawer (Header, Overview Tab, History + Compare + CSV)
  // =========================================================================
  function getFreshPredict(lakeId) {
    const p = S.activePredict;
    if (!p || p.water_body_id !== lakeId) return null;
    if (p._error) return !S.playing && p._date === S.date ? p : null;
    if (p.date_used === S.date || (!S.playing && p.requested_date === S.date)) return p;
    return null;
  }

  function renderDrawerHeader() {
    const hdr = $('#panelHeader');
    const lake = S.selected ? S.lakesById[S.selected] : null;
    if (!hdr || !lake) return;

    const snap = getActiveSnapshot(lake.id);
    const pred = getFreshPredict(lake.id);
    const ev = evalSnapshot(lake, snap);

    const hasFc = Boolean(pred && pred.chl_a_forecast != null);
    const chlMain = pred && pred.chl_a_current != null ? pred.chl_a_current : (snap ? snap.c : null);
    const mainLabel = `${S.mode === 'lab' ? 'Лаборатория БГУ (факт)' : 'Факт Sentinel-2'} · ${fmtDateRu(pred ? pred.date_used : S.date)}`;

    const tsiVal = pred && pred.observed_tsi != null ? pred.observed_tsi : (snap ? snap.t : null);
    const trophicCls = (pred && pred.observed_trophic_class) || '';

    const kpi1Val = hasFc
      ? fmtNum(pred.chl_a_forecast, 1, ' мкг/л')
      : fmtNum(tsiVal, 1);
    const kpi1Lbl = hasFc ? `Прогноз +${pred.horizon_days || 10} д` : 'Индекс TSI';

    const kpi2Val = pred && pred.bloom_probability != null
      ? `${Math.round(pred.bloom_probability * 100)}%`
      : fmtNum(pred ? pred.temp_mean_14d : (snap ? snap.t14 : null), 1, ' °C');
    const kpi2Lbl = pred && pred.bloom_probability != null ? 'Риск цветения' : 'Темп. 14 д';

    const calmVal = pred ? pred.calm_fraction_7d : (snap ? snap.clm : null);
    const kpi3Val = calmVal != null
      ? `${Math.round(calmVal * 100)}%`
      : (snap && snap.cov != null ? `${Math.round(snap.cov)}%` : fmtNum(lake.mean_depth_m, 1, ' м'));
    const kpi3Lbl = calmVal != null ? 'Штиль 7 д' : (snap && snap.cov != null ? 'Вода S2' : 'Ср. глубина');

    let deltaHtml = '';
    const deltaVal = snap && snap.dc != null && snap.c != null ? Number(snap.dc) : null;
    const deltaTitle = 'Изменение факта к предыдущему наблюдению';
    if (deltaVal != null && Number.isFinite(deltaVal)) {
      const cls = deltaVal >= 0.5 ? 'up' : (deltaVal <= -0.5 ? 'down' : 'flat');
      const arrow = deltaVal >= 0.5 ? '↗' : (deltaVal <= -0.5 ? '↘' : '→');
      deltaHtml = `<span class="delta-pill ${cls}" title="${esc(deltaTitle)}">${deltaVal > 0 ? '+' : ''}${deltaVal.toFixed(1)} ${arrow}</span>`;
    }

    const cleanOopt = lake.protected_area ? String(lake.protected_area).replace(/\s*\(ООПТ\)\s*$/i, '').trim() : '';
    const ooptHtml = cleanOopt
      ? `<div class="oopt-pill"><span>ООПТ · ${esc(cleanOopt)}</span></div>`
      : '';

    hdr.innerHTML =
      `<div class="drawer-title-row">` +
      `<div>` +
      `<h2 class="drawer-title">${esc(lake.name)}</h2>` +
      `<div class="drawer-subtitle">${esc(lake.basin || (lake.group === 'myadel' ? 'Мядельская группа озёр' : 'Нарочанская группа озёр'))} · ${fmtNum(lake.area_km2, 2, ' км²')}</div>` +
      `</div>` +
      `<button type="button" class="icon-btn" id="drawerClose" aria-label="Закрыть панель">×</button>` +
      `</div>` +
      ooptHtml +
      `<div class="tsi-hero">` +
      `<div class="tsi-value-block">` +
      `<div class="tsi-number-row">` +
      `<span class="tsi-number" style="color:${ev.color}">${chlMain != null ? Number(chlMain).toFixed(1) : ev.shortVal}</span>` +
      `${chlMain != null ? '<span class="tsi-unit">мкг/л</span>' : ''}` +
      deltaHtml +
      `</div>` +
      `<span class="tsi-label">${esc(mainLabel)}</span>` +
      `</div>` +
      `<div class="hero-right">` +
      `<span class="status-badge ${ev.badgeClass}">${esc(ev.statusLabel)}</span>` +
      `<span class="hero-sub">${tsiVal != null ? `TSI ${Number(tsiVal).toFixed(1)}` : ''}${trophicCls ? ` · ${esc(trophicCls)}` : ''}</span>` +
      `</div>` +
      `</div>` +
      `<div class="kpi-strip">` +
      `<div class="kpi-box"><div class="kpi-box-val">${esc(kpi1Val)}</div><div class="kpi-box-lbl">${esc(kpi1Lbl)}</div></div>` +
      `<div class="kpi-box"><div class="kpi-box-val">${esc(kpi2Val)}</div><div class="kpi-box-lbl">${esc(kpi2Lbl)}</div></div>` +
      `<div class="kpi-box"><div class="kpi-box-val">${esc(kpi3Val)}</div><div class="kpi-box-lbl">${esc(kpi3Lbl)}</div></div>` +
      `</div>`;

    const closeBtn = $('#drawerClose', hdr);
    if (closeBtn) closeBtn.addEventListener('click', closeDrawer);
  }

  function renderDrawerBody() {
    const body = $('#panelBody');
    const lake = S.selected ? S.lakesById[S.selected] : null;
    if (!body || !lake) return;

    destroyChart();
    if (S.activeTab === 'history') {
      renderHistoryTab(body, lake);
    } else {
      renderOverviewTab(body, lake);
    }
  }

  function renderOverviewTab(body, lake) {
    const p = getFreshPredict(lake.id);
    if (!p) {
      body.innerHTML = '<div class="lake-note">Загрузка детального среза наблюдений и прогноза…</div>';
      return;
    }

    if (p._error) {
      body.innerHTML = `<div class="lake-note">${esc(p._error)}</div>`;
      return;
    }

    let forecastHtml = '';
    if (p.chl_a_forecast != null) {
      const bloomThr = (lake && lake.bloom_ug_l) || 25;
      const interval = (p.pred_low_ug_l != null && p.pred_high_ug_l != null)
        ? `Диапазон ±MAE (${fmtNum(p.uncertainty_ug_l, 1)}): ${fmtNum(p.pred_low_ug_l, 1)} – ${fmtNum(p.pred_high_ug_l, 1)} мкг/л`
        : 'Интервал ±MAE будет показан после подключения model_metrics.json';
      const probPct = p.bloom_probability != null ? Math.round(p.bloom_probability * 100) : null;
      const attPct = p.attention_probability != null ? Math.round(p.attention_probability * 100) : null;
      const isRed = p.alert_level === 'RED';
      const probColor = (isRed || (probPct != null && probPct >= 50)) ? '#fb7185' : '#34d399';
      const fcBadgeCls = isRed ? 'badge-eutro' : 'badge-meso';
      const fcBadgeTxt = isRed ? `Предупреждение (≥${bloomThr})` : `Норма (<${bloomThr})`;
      let fcDeltaHtml = '';
      if (p.chl_a_current != null) {
        const fd = Number(p.chl_a_forecast) - Number(p.chl_a_current);
        if (Number.isFinite(fd)) {
          const fCls = fd >= 0.5 ? 'up' : (fd <= -0.5 ? 'down' : 'flat');
          const fArrow = fd >= 0.5 ? '↗' : (fd <= -0.5 ? '↘' : '→');
          fcDeltaHtml = `<span class="delta-pill ${fCls}" title="Ожидаемое изменение прогноза относительно факта сейчас">${fd > 0 ? '+' : ''}${fd.toFixed(1)} ${fArrow}</span>`;
        }
      }
      const fcMetaParts = [
        p.trophic_index != null ? `TSI ${Number(p.trophic_index).toFixed(1)}${p.trophic_class ? ` (${p.trophic_class})` : ''}` : '',
        probPct != null ? `P(≥25) = ${probPct}%` : '',
        attPct != null ? `P(≥15) = ${attPct}%` : '',
      ].filter(Boolean).join(' · ');
      forecastHtml =
        `<div class="section-block">` +
        `<div class="section-label">Прогноз на +${p.horizon_days || 10} дней · от ${fmtDateRu(p.date_used)}</div>` +
        `<div class="forecast-card">` +
        `<div class="forecast-row">` +
        `<span class="forecast-val" style="color:${probColor}">${fmtNum(p.chl_a_forecast, 1, ' мкг/л')}</span>` +
        fcDeltaHtml +
        `<span class="status-badge ${fcBadgeCls}">${esc(fcBadgeTxt)}</span>` +
        `</div>` +
        `${fcMetaParts ? `<div class="forecast-meta">${esc(fcMetaParts)}</div>` : ''}` +
        `${probPct != null ? `<div class="prob-bar-track"><div class="prob-bar-fill" style="width:${probPct}%;background:${probColor}"></div></div>` : ''}` +
        `<div class="forecast-meta">${esc(interval)}</div>` +
        `</div></div>`;
    } else {
      const msg = (p.reasons && p.reasons[0]) || 'Прогноз на эту декаду не рассчитывается.';
      forecastHtml =
        `<div class="section-block">` +
        `<div class="section-label">Статус прогноза (+${p.horizon_days || 10} дней)</div>` +
        `<div class="lake-note">${esc(msg)}</div>` +
        `</div>`;
    }

    const reasonsHtml = (p.reasons && p.reasons.length && p.chl_a_forecast != null)
      ? `<div class="section-block"><div class="section-label">Вердикт графа знаний (Часть 3)</div>` +
        `<ul class="clean-list">${p.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>`
      : '';

    const recsHtml = (p.recommendations && p.recommendations.length)
      ? `<div class="section-block"><div class="section-label">Рекомендации</div>` +
        `<ul class="clean-list">${p.recommendations.map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>`
      : '';

    const inflows = (lake.cascade_inflow_from || []).filter(Boolean);
    const outflow = lake.cascade_outflow_to;
    const reserveNames = { rossokhi: 'Россохи (резерв)' };
    let cascadeHtml = '';
    if (inflows.length || outflow) {
      const chips = [];
      for (const srcId of inflows) {
        const srcLake = S.lakesById[srcId];
        chips.push(srcLake
          ? `<button type="button" class="cascade-chip" data-jump="${esc(srcId)}">← Приток: ${esc(shortLakeName(srcLake.name))}</button>`
          : `<span class="cascade-chip static">← Приток: ${esc(reserveNames[srcId] || srcId)}</span>`);
      }
      if (outflow) {
        const dstLake = S.lakesById[outflow];
        if (dstLake) {
          chips.push(`<button type="button" class="cascade-chip" data-jump="${esc(outflow)}">Сток → ${esc(shortLakeName(dstLake.name))}</button>`);
        } else if (String(outflow).toLowerCase() === 'бессточное') {
          chips.push('<span class="cascade-chip static">Бессточное озеро</span>');
        } else {
          chips.push(`<span class="cascade-chip static">Сток → ${esc(outflow)}</span>`);
        }
      }
      cascadeHtml =
        `<div class="section-block"><div class="section-label">Гидрологический каскад</div>` +
        `<div class="cascade-chips">${chips.join('')}</div></div>`;
    }

    const factors = [
      { k: 'Темп. воздуха (14 д)', v: fmtNum(p.temp_mean_14d, 1, ' °C') },
      { k: 'Доля штиля (7 д)', v: p.calm_fraction_7d != null ? `${Math.round(p.calm_fraction_7d * 100)}%` : '—' },
      { k: 'Осадки (14 д)', v: fmtNum(p.precip_sum_14d, 1, ' мм') },
      { k: 'Ветер (7 д)', v: fmtNum(p.wind_mean_7d, 1, ' м/с') },
      { k: 'Индекс NDCI', v: fmtNum(p.ndci_current, 3) },
      { k: 'Отношение B04/B02', v: fmtNum(p.b04_b02_ratio, 3) },
    ];
    if (p.secchi_depth_m != null) factors.push({ k: 'Диск Секки (лаб.)', v: fmtNum(p.secchi_depth_m, 2, ' м') });
    if (p.total_phosphorus_ug_l != null) factors.push({ k: 'Общий фосфор P', v: fmtNum(p.total_phosphorus_ug_l, 1, ' мкг/л') });

    const meteoHtml =
      `<div class="section-block"><div class="section-label">Метеоусловия и оптика</div>` +
      `<div class="factors-grid">${factors.map((f) => (
        `<div class="factor-card"><div class="factor-name">${esc(f.k)}</div><div class="factor-val">${esc(f.v)}</div></div>`
      )).join('')}</div></div>`;

    const obs = p.observation || {};
    const statusText = STATUS_RU[p.obs_status] || p.obs_status || (p.mode === 'lab' ? 'Лабораторная проба' : 'н/д');
    const metaCards = [
      { k: p.mode === 'lab' ? 'Тип наблюдения' : 'Статус декады', v: statusText },
      { k: 'Чистых пикселей', v: obs.valid_water_pixels != null ? String(obs.valid_water_pixels) : '—' },
      { k: 'Сцен за декаду', v: obs.scenes_in_decade != null ? String(obs.scenes_in_decade) : '—' },
      { k: 'Покрытие воды', v: obs.water_coverage_pct != null ? `${Number(obs.water_coverage_pct).toFixed(0)}%` : '—' },
    ];
    const provHtml =
      `<div class="section-block"><div class="section-label">Качество и источник наблюдения</div>` +
      `<div class="factors-grid">${metaCards.map((m) => (
        `<div class="factor-card"><div class="factor-name">${esc(m.k)}</div><div class="factor-val">${esc(m.v)}</div></div>`
      )).join('')}</div>` +
      `${obs.scene_id ? `<div class="scene-tag">Сцена: ${esc(obs.scene_id)}</div>` : ''}` +
      `${p.notes && p.notes.length ? `<div class="lake-note">${esc(p.notes.join(' '))}</div>` : ''}` +
      `</div>`;

    body.innerHTML = forecastHtml + reasonsHtml + recsHtml + cascadeHtml + meteoHtml + provHtml;

    $$('[data-jump]', body).forEach((btn) => {
      btn.addEventListener('click', () => selectLake(btn.dataset.jump));
    });
  }

  function exportSelectedCsv() {
    const lake = S.selected ? S.lakesById[S.selected] : null;
    const hist = S.activeHistory;
    if (!lake || !hist || !Array.isArray(hist.points)) return;

    const cmpLake = S.compareId ? S.lakesById[S.compareId] : null;
    const cmpMap = new Map();
    if (cmpLake && S.compareHistory && Array.isArray(S.compareHistory.points)) {
      for (const pt of S.compareHistory.points) cmpMap.set(pt.date, pt.observed);
    }

    const header = [
      'water_body_id', 'water_body_name', 'mode', 'date', 'obs_status',
      'is_interpolated', 'chl_a_observed_ug_l', 'chl_a_forecast_10d_ug_l',
      'forecast_low_ug_l', 'forecast_high_ug_l',
    ];
    if (cmpLake) header.push(`compare_${cmpLake.id}_chl_a_ug_l`);

    const qCell = (v) => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`;
    const lines = [header.join(',')];
    for (const pt of hist.points) {
      const row = [
        qCell(lake.id), qCell(lake.name), qCell(S.mode), qCell(pt.date),
        qCell(pt.obs_status || ''), pt.is_interpolated ? 'true' : 'false',
        pt.observed == null ? '' : pt.observed,
        pt.forecast == null ? '' : pt.forecast,
        pt.forecast_low == null ? '' : pt.forecast_low,
        pt.forecast_high == null ? '' : pt.forecast_high,
      ];
      if (cmpLake) {
        const cv = cmpMap.get(pt.date);
        row.push(cv == null ? '' : cv);
      }
      lines.push(row.join(','));
    }

    const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${lake.id}_${S.mode}_${S.date || 'series'}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function renderHistoryTab(body, lake) {
    const h = S.activeHistory && S.activeHistory.water_body_id === lake.id ? S.activeHistory : null;
    const otherLakes = S.lakes.filter((l) => l.id !== lake.id);
    const passport = [
      { k: 'Площадь зеркала', v: fmtNum(lake.area_km2, 2, ' км²') },
      { k: 'Средняя глубина', v: fmtNum(lake.mean_depth_m, 1, ' м') },
      { k: 'Макс. глубина', v: fmtNum(lake.max_depth_m, 1, ' м') },
      { k: 'Объём воды', v: fmtNum(lake.volume_mln_m3, 1, ' млн м³') },
      { k: 'Площадь водосбора', v: fmtNum(lake.watershed_km2, 1, ' км²') },
      { k: 'Коэфф. водосбора', v: fmtNum(lake.watershed_ratio, 2) },
      { k: 'Стратификация', v: STRAT_RU[lake.stratification_type] || lake.stratification_type || '—' },
      { k: 'Режимы данных', v: (lake.modes || ['satellite']).map((m) => (m === 'lab' ? 'Лаборатория' : 'Спутник')).join(' + ') },
    ];

    body.innerHTML =
      `<div class="section-block">` +
      `<div class="section-row">` +
      `<span class="section-label">Временной ряд Chl-a (мкг/л)</span>` +
      `<button type="button" id="exportCsvBtn" class="export-btn" title="Скачать таблицу временного ряда в CSV">↓ CSV</button>` +
      `</div>` +
      `<div class="chart-controls">` +
      `<select id="histWin" class="win-select" aria-label="Период графика">` +
      `<option value="36"${S.histWin === 36 ? ' selected' : ''}>1 год (36 дек.)</option>` +
      `<option value="108"${S.histWin === 108 ? ' selected' : ''}>3 года (108 дек.)</option>` +
      `<option value="360"${S.histWin === 360 ? ' selected' : ''}>Весь ряд (2018–2026)</option>` +
      `</select>` +
      `<select id="compareSelect" class="win-select" aria-label="Сравнить с другим озером">` +
      `<option value="">Сравнить с озером…</option>` +
      otherLakes.map((l) => `<option value="${esc(l.id)}"${S.compareId === l.id ? ' selected' : ''}>vs ${esc(shortLakeName(l.name))}</option>`).join('') +
      `</select>` +
      `</div>` +
      `<div class="chart-card">` +
      `<div class="chart-canvas-wrap"><canvas id="chart"></canvas></div>` +
      `<div class="chart-hint">${h ? 'Клик по графику переводит карту на выбранную дату. Разрывы — лёд и облачность.' : 'Загрузка истории наблюдений…'}</div>` +
      `</div>` +
      `</div>` +
      `<div class="section-block">` +
      `<div class="section-label">Паспорт водоёма</div>` +
      `<div class="factors-grid">${passport.map((p) => (
        `<div class="factor-card"><div class="factor-name">${esc(p.k)}</div><div class="factor-val">${esc(p.v)}</div></div>`
      )).join('')}</div>` +
      `${lake.notes ? `<div class="lake-note">${esc(lake.notes)}</div>` : ''}` +
      `</div>`;

    const winSel = $('#histWin', body);
    if (winSel) {
      winSel.addEventListener('change', () => {
        S.histWin = Number(winSel.value) || 108;
        loadSelectedLakeHistory(lake.id);
        if (S.compareId) loadCompareLakeHistory(S.compareId);
      });
    }

    const cmpSel = $('#compareSelect', body);
    if (cmpSel) {
      cmpSel.addEventListener('change', () => {
        setCompareLake(cmpSel.value || null);
      });
    }

    const expBtn = $('#exportCsvBtn', body);
    if (expBtn) expBtn.addEventListener('click', exportSelectedCsv);

    if (h) drawHistoryChart(lake, h);
  }

  function destroyChart() {
    if (S.chart) {
      S.chart.destroy();
      S.chart = null;
    }
  }

  function drawHistoryChart(lake, hist) {
    const canvas = $('#chart');
    if (!canvas || typeof Chart === 'undefined') return;
    destroyChart();

    const pts = hist.points || [];
    const labels = pts.map((p) => p.date);
    const obsData = pts.map((p) => p.observed);
    const fcData = pts.map((p) => p.forecast);
    const fcLow = pts.map((p) => p.forecast_low);
    const fcHigh = pts.map((p) => p.forecast_high);
    const attThr = lake.attention_ug_l || 15;
    const bloomThr = lake.bloom_ug_l || 25;

    const datasets = [
      {
        label: 'Наблюдения',
        data: obsData,
        borderColor: '#22d3ee',
        backgroundColor: 'rgba(34, 211, 238, 0.14)',
        borderWidth: 2,
        pointRadius: pts.length > 140 ? 1.5 : 2.5,
        pointHoverRadius: 5,
        spanGaps: false,
        tension: 0.25,
      },
    ];

    // Overlay second lake for direct comparison!
    if (S.compareId && S.compareHistory && S.compareHistory.water_body_id === S.compareId && labels.length) {
      const cmpLake = S.lakesById[S.compareId];
      const cmpByDate = new Map((S.compareHistory.points || []).map((pt) => [pt.date, pt.observed]));
      const cmpSeries = labels.map((d) => (cmpByDate.has(d) ? cmpByDate.get(d) : null));
      datasets.push({
        label: `Сравнение: ${shortLakeName(cmpLake ? cmpLake.name : S.compareId)}`,
        data: cmpSeries,
        borderColor: '#e879f9',
        borderDash: [4, 3],
        borderWidth: 2,
        pointRadius: pts.length > 140 ? 1.2 : 2.2,
        spanGaps: false,
        tension: 0.25,
      });
    }

    if (fcData.some((v) => v != null)) {
      if (fcLow.some((v) => v != null) && fcHigh.some((v) => v != null)) {
        datasets.push({
          label: 'Коридор ±MAE (низ)',
          data: fcLow,
          borderColor: 'transparent',
          pointRadius: 0,
          fill: false,
          spanGaps: false,
        });
        datasets.push({
          label: 'Коридор ±MAE',
          data: fcHigh,
          borderColor: 'transparent',
          backgroundColor: 'rgba(251, 191, 36, 0.16)',
          pointRadius: 0,
          fill: '-1',
          spanGaps: false,
        });
      }
      datasets.push({
        label: 'Прогноз +10 сут',
        data: fcData,
        borderColor: '#fbbf24',
        borderDash: [5, 4],
        borderWidth: 1.8,
        pointRadius: 0,
        spanGaps: false,
        tension: 0.25,
      });
    }

    const labPts = S.mode === 'satellite' && S.timeline && S.timeline.lab_points ? (S.timeline.lab_points[lake.id] || []) : [];
    if (labPts.length && labels.length) {
      const labSeries = new Array(labels.length).fill(null);
      const labelMs = labels.map((d) => Date.parse(d));
      for (const lp of labPts) {
        const ms = Date.parse(lp.date);
        let bestIdx = -1;
        let bestDiff = 10 * 86400 * 1000;
        for (let i = 0; i < labelMs.length; i += 1) {
          const diff = Math.abs(labelMs[i] - ms);
          if (diff <= bestDiff) {
            bestDiff = diff;
            bestIdx = i;
          }
        }
        if (bestIdx >= 0) labSeries[bestIdx] = lp.chl;
      }
      if (labSeries.some((v) => v != null)) {
        datasets.push({
          label: 'Пробы БГУ (лаб.)',
          data: labSeries,
          borderColor: '#34d399',
          backgroundColor: '#a7f3d0',
          pointStyle: 'rectRot',
          pointRadius: 5,
          pointHoverRadius: 7,
          showLine: false,
        });
      }
    }

    datasets.push(
      {
        label: `Порог внимания ${attThr}`,
        data: labels.map(() => attThr),
        borderColor: 'rgba(251, 191, 36, 0.5)',
        borderDash: [4, 4],
        borderWidth: 1.1,
        pointRadius: 0,
      },
      {
        label: `Порог цветения ${bloomThr}`,
        data: labels.map(() => bloomThr),
        borderColor: 'rgba(251, 113, 133, 0.55)',
        borderDash: [4, 4],
        borderWidth: 1.1,
        pointRadius: 0,
      },
    );

    const activeDateCursorPlugin = {
      id: 'activeDateCursor',
      afterDatasetsDraw(chart) {
        if (!S.date || !labels.length) return;
        const idx = labels.indexOf(S.date);
        if (idx < 0) return;
        const xScale = chart.scales && chart.scales.x;
        const area = chart.chartArea;
        if (!xScale || !area) return;
        const x = xScale.getPixelForValue(idx);
        const ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = 'rgba(34, 211, 238, 0.85)';
        ctx.lineWidth = 1.4;
        ctx.moveTo(x, area.top);
        ctx.lineTo(x, area.bottom);
        ctx.stroke();
        ctx.restore();
      },
    };

    S.chart = new Chart(canvas, {
      type: 'line',
      data: { labels, datasets },
      plugins: [activeDateCursorPlugin],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        onClick(_evt, elements) {
          if (elements && elements.length) {
            const idx = elements[0].index;
            if (labels[idx]) setDate(labels[idx], true);
          }
        },
        plugins: {
          legend: {
            display: true,
            labels: {
              color: '#94a3b8',
              boxWidth: 10,
              font: { size: 10 },
              filter: (item) => !item.text.includes('(низ)') && !item.text.startsWith('Порог'),
            },
          },
          tooltip: {
            backgroundColor: 'rgba(11, 16, 26, 0.94)',
            titleColor: '#f1f5f9',
            bodyColor: '#cbd5e1',
            borderColor: 'rgba(148, 163, 184, 0.25)',
            borderWidth: 1,
          },
        },
        scales: {
          x: {
            ticks: { color: '#64748b', maxTicksLimit: 6, font: { size: 10 } },
            grid: { color: 'rgba(148, 163, 184, 0.08)' },
          },
          y: {
            beginAtZero: true,
            ticks: { color: '#64748b', font: { size: 10 } },
            grid: { color: 'rgba(148, 163, 184, 0.08)' },
          },
        },
      },
    });
  }

  // =========================================================================
  // Data Loading, Lake Selection & Comparison
  // =========================================================================
  async function loadSelectedLakeDetail(lakeId, alsoHistory = true) {
    if (!lakeId || !S.date) return;
    const seq = ++S.predictSeq;
    const reqDate = S.date;
    const cacheKey = `${lakeId}|${reqDate}|${S.mode}`;
    let data = S.predictCache.get(cacheKey);
    if (!data) {
      try {
        data = await apiGet(`/predict/${encodeURIComponent(lakeId)}?date=${encodeURIComponent(reqDate)}&mode=${encodeURIComponent(S.mode)}`);
        S.predictCache.set(cacheKey, data);
      } catch (err) {
        data = { water_body_id: lakeId, _date: reqDate, _error: `Нет детальных данных на ${reqDate}: ${err.message}` };
      }
    }
    if (seq !== S.predictSeq || S.selected !== lakeId) return;
    S.activePredict = data;
    renderDrawerHeader();
    if (S.activeTab === 'overview') renderDrawerBody();
    const chartHasDate = S.activeHistory && S.activeHistory.water_body_id === lakeId &&
      Array.isArray(S.activeHistory.points) && S.activeHistory.points.some((pt) => pt.date === S.date);
    if (alsoHistory || (S.activeTab === 'history' && !chartHasDate)) {
      loadSelectedLakeHistory(lakeId);
      if (S.compareId) loadCompareLakeHistory(S.compareId);
    }
  }

  async function fetchLakeHistoryCached(lakeId) {
    const lastTimelineDate = (S.timeline && S.timeline.dates && S.timeline.dates[S.timeline.dates.length - 1]) || S.date || '2026-11-15';
    const endIso = S.histWin >= 360 ? lastTimelineDate : (S.date || lastTimelineDate);
    const cacheKey = `${lakeId}|${S.mode}|${S.histWin}|${endIso}`;
    let hist = S.historyCache.get(cacheKey);
    if (!hist) {
      try {
        hist = await apiGet(`/history/${encodeURIComponent(lakeId)}?mode=${encodeURIComponent(S.mode)}&date=${encodeURIComponent(endIso)}&limit=${S.histWin}`);
        S.historyCache.set(cacheKey, hist);
      } catch (_) {
        hist = { water_body_id: lakeId, points: [] };
      }
    }
    return hist;
  }

  async function loadSelectedLakeHistory(lakeId) {
    if (!lakeId) return;
    const seq = ++S.historySeq;
    const hist = await fetchLakeHistoryCached(lakeId);
    if (seq !== S.historySeq || S.selected !== lakeId) return;
    S.activeHistory = hist;
    if (S.activeTab === 'history') renderDrawerBody();
  }

  async function loadCompareLakeHistory(compareId) {
    if (!compareId) {
      S.compareHistory = null;
      if (S.activeTab === 'history' && S.selected) renderDrawerBody();
      return;
    }
    const hist = await fetchLakeHistoryCached(compareId);
    if (S.compareId !== compareId) return;
    S.compareHistory = hist;
    if (S.activeTab === 'history' && S.selected) renderDrawerBody();
  }

  async function setCompareLake(lakeId) {
    S.compareId = lakeId && lakeId !== S.selected && S.lakesById[lakeId] ? lakeId : null;
    S.compareHistory = null;
    syncMapData();
    await loadCompareLakeHistory(S.compareId);
  }

  async function selectLake(lakeId, shouldFly = true) {
    const lake = S.lakesById[lakeId];
    if (!lake) return;
    S.selected = lakeId;
    if (S.compareId === lakeId) {
      S.compareId = null;
      S.compareHistory = null;
    }
    S.pickerOpen = false;
    const dd = $('#lakeDropdown');
    if (dd) dd.hidden = true;
    const pBtn = $('#lakePickerBtn');
    if (pBtn) pBtn.setAttribute('aria-expanded', 'false');

    const panel = $('#panel');
    if (panel) panel.hidden = false;
    renderLakePicker();
    syncMapData();
    renderDrawerHeader();
    if (shouldFly) flyToLake(lake);
    await loadSelectedLakeDetail(lakeId, true);
  }

  function closeDrawer() {
    S.selected = null;
    S.compareId = null;
    S.activePredict = null;
    S.activeHistory = null;
    S.compareHistory = null;
    destroyChart();
    const panel = $('#panel');
    if (panel) panel.hidden = true;
    renderLakePicker();
    syncMapData();
  }

  function setDrawerTab(tab) {
    S.activeTab = tab;
    $$('.drawer-tab').forEach((btn) => {
      const on = btn.dataset.tab === tab;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', String(on));
    });
    renderDrawerBody();
    if (tab === 'history' && S.selected && (!S.activeHistory || S.activeHistory.water_body_id !== S.selected)) {
      loadSelectedLakeHistory(S.selected);
    }
  }

  async function setMode(mode) {
    if (mode !== 'satellite' && mode !== 'lab') return;
    if (mode === S.mode && S.timeline) return;
    stopPlay();
    S.mode = mode;
    S.predictCache.clear();
    S.historyCache.clear();
    S.compareHistory = null;
    $$('#modeSwitch .seg').forEach((btn) => {
      const on = btn.dataset.mode === mode;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', String(on));
    });
    const sub = $('#sub');
    if (sub) {
      sub.textContent = mode === 'lab'
        ? 'Лабораторные пробы БГУ · Сверка'
        : 'Sentinel-2 L2A · Прогноз +10 сут';
    }
    try {
      S.timeline = await apiGet(`/timeline?mode=${encodeURIComponent(mode)}`);
      const prevDate = S.date;
      S.date = (prevDate && S.timeline.snapshots && S.timeline.snapshots[prevDate])
        ? prevDate
        : S.timeline.default_date;
      updateAllViewsForDate(true);
      if (S.selected) {
        loadSelectedLakeHistory(S.selected);
        if (S.compareId) loadCompareLakeHistory(S.compareId);
      }
    } catch (err) {
      showApiError(`Режим «${mode === 'lab' ? 'Лаборатория' : 'Спутник'}» недоступен: ${err.message}`);
    }
  }

  function renderStatusBanner() {
    const stubBox = $('#stub');
    const stubText = $('#stubText');
    if (!stubBox || !stubText || !S.root) return;
    const comps = S.root.components || {};
    const stubs = Object.entries(comps).filter(([, v]) => String(v).startsWith('заглушка'));
    if (!stubs.length) {
      stubBox.hidden = true;
      return;
    }
    const p1Real = String(comps.part1 || '').startsWith('реальная');
    const p2Real = String(comps.part2 || '').startsWith('реальная');
    const p3Real = String(comps.part3 || '').startsWith('реальная');
    if (p1Real && p3Real && !p2Real) {
      stubText.textContent = `Часть 1 (данные, ${S.lakes.length} озёр) и Часть 3 (граф знаний, 2 уровня) подключены. Модели прогноза (+10д, Часть 2) ещё не подключены — показан факт Sentinel-2.`;
    } else if (p1Real && p2Real && !p3Real) {
      stubText.textContent = `Часть 1 (${S.lakes.length} озёр) и Часть 2 (модели +10д) подключены. Граф знаний (Часть 3) работает в режиме заглушки.`;
    } else if (p1Real) {
      stubText.textContent = `Часть 1: реальные данные (${S.lakes.length} озёр, 2018–2026). Модели прогноза (+10д) и граф знаний ещё не подключены — показан факт Sentinel-2.`;
    } else {
      stubText.textContent = 'Демонстрационный режим (заглушки): реальные витрины и модели не найдены.';
    }
    stubBox.hidden = false;
  }

  function showApiError(msg) {
    const box = $('#apierr');
    const txt = $('#apierr-msg');
    if (txt) txt.textContent = msg;
    if (box) box.hidden = false;
  }

  function hideApiError() {
    const box = $('#apierr');
    if (box) box.hidden = true;
  }

  function populateLabLakeSelect() {
    const sel = $('#labLakeSelect');
    if (!sel) return;
    sel.innerHTML = S.lakes.map((l) => (
      `<option value="${esc(l.id)}"${l.id === S.selected ? ' selected' : ''}>${esc(l.name)}</option>`
    )).join('');
    const dInp = $('#labDateInput');
    if (dInp && !dInp.value) {
      dInp.value = new Date().toISOString().slice(0, 10);
    }
  }

  // =========================================================================
  // Modals: Metrics, Lab Upload, Help
  // =========================================================================
  async function openMetricsModal() {
    const modal = $('#metricsModal');
    const container = $('#metrics');
    if (!modal || !container) return;
    modal.hidden = false;
    container.innerHTML = '<p class="muted-text">Загрузка метрик с сервера…</p>';
    try {
      const m = await apiGet('/metrics');
      const modes = m.modes || {};
      const modesHtml =
        `<div class="section-block"><div class="section-label">Доступность режимов</div>` +
        `<div class="mode-pills-row">` +
        Object.entries(modes).map(([k, info]) => {
          const ok = Boolean(info && info.available);
          const lbl = k === 'lab' ? 'Лаборатория' : 'Спутник';
          const detail = info && info.reason
            ? (info.data_file ? `${info.data_file} · ${info.reason}` : info.reason)
            : (info && info.data_file ? `${info.data_file}` : 'только факт');
          return `<span class="pill ${ok ? 'on' : 'off'}">${esc(lbl)}: ${ok ? 'данные + модели' : esc(detail)}</span>`;
        }).join('') +
        `</div></div>`;

      let modelTablesHtml = '';
      if (m.regression && typeof m.regression === 'object') {
        const rows = [];
        for (const [k, v] of Object.entries(m.regression)) {
          if (v && typeof v === 'object') {
            rows.push(`<tr><td>${esc(k)}</td><td>${fmtNum(v.rmse, 2)}</td><td>${fmtNum(v.mae, 2)}</td><td>${fmtNum(v.r2, 2)}</td></tr>`);
          }
        }
        if (!rows.length && ('rmse' in m.regression || 'mae' in m.regression)) {
          rows.push(`<tr><td>Регрессия (+${m.horizon_days || 10}д)</td><td>${fmtNum(m.regression.rmse, 2)}</td><td>${fmtNum(m.regression.mae, 2)}</td><td>${fmtNum(m.regression.r2, 2)}</td></tr>`);
        }
        if (rows.length) {
          modelTablesHtml +=
            `<div class="section-block"><div class="section-label">Регрессия хлорофилла-а</div>` +
            `<table class="metrics-table"><thead><tr><th>Модель</th><th>RMSE</th><th>MAE</th><th>R²</th></tr></thead>` +
            `<tbody>${rows.join('')}</tbody></table></div>`;
        }
      } else if (m.model_1_current_chlorophyll || m.model_2_forecast_10d) {
        const rows = [];
        const m1 = m.model_1_current_chlorophyll;
        if (m1) {
          rows.push(
            `<tr><td>Модель 1: ${esc(m1.winner || 'Chl-a')} (БГУ)</td>` +
            `<td>${fmtNum(m1.bsu_rmse_ug_l, 2)}</td><td>${fmtNum(m1.bsu_mae_ug_l, 2)}</td>` +
            `<td>${fmtNum(m1.bsu_spearman_rho, 3)}</td></tr>`
          );
        }
        const m2 = m.model_2_forecast_10d;
        if (m2) {
          rows.push(
            `<tr><td>Модель 2: ${esc(m2.winner || 'Прогноз +10д')}</td>` +
            `<td>${fmtNum(m2.test_rmse_ug_l, 2)}</td><td>${fmtNum(m2.test_mae_ug_l, 2)}</td>` +
            `<td>${fmtNum(m2.test_spearman_rho, 3)}</td></tr>`
          );
        }
        if (rows.length) {
          modelTablesHtml +=
            `<div class="section-block"><div class="section-label">Метрики моделей (Часть 2)</div>` +
            `<table class="metrics-table"><thead><tr><th>Модель</th><th>RMSE</th><th>MAE</th><th>Spearman ρ</th></tr></thead>` +
            `<tbody>${rows.join('')}</tbody></table></div>`;
        }
      }

      let dqHtml = '';
      const dq = m.data_quality;
      if (dq && typeof dq === 'object') {
        const satQ = dq.satellite_quality || {};
        const metQ = dq.meteo_quality || {};
        const roster = dq.lakes_roster || {};
        const labG = dq.lab_and_gloria_summary || {};
        const covByLake = dq.decadal_coverage_by_lake || {};
        const rawObs = satQ.raw_observations_total != null ? satQ.raw_observations_total : satQ.raw_scenes_input;
        let validDec = satQ.valid_decades_total;
        if (validDec == null && Object.keys(covByLake).length) {
          validDec = Object.values(covByLake).reduce((acc, st) => acc + Number((st && st.open_water_valid_decades) || 0), 0);
        }
        const gloriaPairs = labG.gloria_clean_pairs != null ? labG.gloria_clean_pairs : labG.gloria_total_clean_pairs;
        const bsuRows = labG.bsu_lab_2018_2019_rows != null ? labG.bsu_lab_2018_2019_rows : labG.bsu_2018_2019_samples;
        const cards = [];
        if (roster.active_count != null) cards.push({ k: 'Активных озёр', v: String(roster.active_count) });
        if (rawObs != null) cards.push({ k: 'Сцен Sentinel-2', v: String(rawObs) });
        if (validDec != null) cards.push({ k: 'Чистых декад', v: String(validDec) });
        if (metQ.total_daily_rows != null) cards.push({ k: 'Метео NASA POWER', v: `${metQ.total_daily_rows} сут (0% проп.)` });
        if (gloriaPairs != null) cards.push({ k: 'Калибровка GLORIA', v: `${gloriaPairs} пар` });
        if (bsuRows != null) cards.push({ k: 'Пробы БГУ 2018–19', v: `${bsuRows} шт.` });
        if (!cards.length) {
          for (const [k, v] of Object.entries(dq)) {
            if (typeof v === 'number' || typeof v === 'string') {
              cards.push({ k, v: typeof v === 'number' && v <= 1 ? `${Math.round(v * 100)}%` : String(v) });
            }
          }
        }
        if (cards.length) {
          dqHtml =
            `<div class="section-block"><div class="section-label">Качество данных Части 1</div>` +
            `<div class="factors-grid">${cards.map((c) => (
              `<div class="factor-card"><div class="factor-name">${esc(c.k)}</div><div class="factor-val">${esc(c.v)}</div></div>`
            )).join('')}</div></div>`;
        }
      }

      container.innerHTML = modesHtml + modelTablesHtml + dqHtml;
    } catch (err) {
      container.innerHTML = `<div class="lab-msg err">Не удалось загрузить метрики: ${esc(err.message)}</div>`;
    }
  }

  function openLabModal() {
    populateLabLakeSelect();
    const msg = $('#labMsg');
    if (msg) msg.hidden = true;
    const modal = $('#labModal');
    if (modal) modal.hidden = false;
  }

  async function handleLabSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const msg = $('#labMsg');
    const fd = new FormData(form);
    const body = {
      water_body_id: String(fd.get('water_body_id') || ''),
      sample_date: String(fd.get('sample_date') || ''),
      chl_a_lab_ug_l: Number(fd.get('chl_a_lab_ug_l')),
      secchi_depth_m: fd.get('secchi_depth_m') ? Number(fd.get('secchi_depth_m')) : null,
      total_phosphorus_ug_l: fd.get('total_phosphorus_ug_l') ? Number(fd.get('total_phosphorus_ug_l')) : null,
      source_note: String(fd.get('source_note') || '').trim() || 'Загружено через веб-панель',
    };
    try {
      const res = await apiPost('/lab/upload', body);
      let text = res.message || 'Проба сохранена.';
      if (res.satellite_comparison) {
        const sc = res.satellite_comparison;
        text += ` Сверка со спутником (${sc.satellite_date}): Sentinel-2 = ${sc.satellite_chl_a_ug_l} мкг/л, разница Δ = ${sc.difference_ug_l > 0 ? '+' : ''}${sc.difference_ug_l} мкг/л.`;
      }
      if (msg) {
        msg.textContent = text;
        msg.className = 'lab-msg';
        msg.hidden = false;
      }
      form.reset();
      populateLabLakeSelect();
    } catch (err) {
      if (msg) {
        msg.textContent = `Ошибка: ${err.message}`;
        msg.className = 'lab-msg err';
        msg.hidden = false;
      }
    }
  }

  // =========================================================================
  // Bootstrap & Refresh
  // =========================================================================
  async function loadAll(preserveSelection = false) {
    hideApiError();
    try {
      const [rootInfo, lakes, tl] = await Promise.all([
        apiGet('/'),
        apiGet('/lakes'),
        apiGet(`/timeline?mode=${encodeURIComponent(S.mode)}`),
      ]);
      S.root = rootInfo;
      S.lakes = lakes || [];
      S.lakesById = Object.fromEntries(S.lakes.map((l) => [l.id, l]));
      S.timeline = tl;
      S.predictCache.clear();
      S.historyCache.clear();
      S.defaultView = computeDefaultView();

      if (!preserveSelection || !S.date || !tl.snapshots[S.date]) {
        S.date = tl.default_date || (tl.dates && tl.dates[tl.dates.length - 1]) || null;
      }
      if (S.selected && !S.lakesById[S.selected]) {
        closeDrawer();
      }

      renderStatusBanner();
      populateLabLakeSelect();
      updateAllViewsForDate(Boolean(S.selected));
      if (!preserveSelection && S.map && S.map.jumpTo) {
        S.map.jumpTo({
          center: S.defaultView.center,
          zoom: S.defaultView.zoom,
          pitch: S.defaultView.pitch,
          bearing: S.defaultView.bearing,
        });
      }

      const upd = $('#upd');
      if (upd) {
        const now = new Date();
        upd.textContent = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
      }
    } catch (err) {
      showApiError(`Нет связи с API (${API}): ${err.message}`);
    }
  }

  function bindEvents() {
    $$('#basemapSwitch .mode-btn').forEach((btn) => {
      btn.addEventListener('click', () => setBasemap(btn.dataset.basemap));
    });

    $$('#modeSwitch .seg').forEach((btn) => {
      btn.addEventListener('click', () => setMode(btn.dataset.mode));
    });

    $$('#themeSwitch .theme-btn').forEach((btn) => {
      btn.addEventListener('click', () => setTheme(btn.dataset.theme));
    });

    $$('#seasonSwitch .season-btn').forEach((btn) => {
      btn.addEventListener('click', () => setSeason(btn.dataset.season));
    });

    const resetBtn = $('#resetCamBtn');
    if (resetBtn) resetBtn.addEventListener('click', resetCamera);

    const refreshBtn = $('#refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', () => loadAll(true));

    const retryBtn = $('#retry');
    if (retryBtn) retryBtn.addEventListener('click', () => loadAll(true));

    const stubClose = $('#stubClose');
    if (stubClose) stubClose.addEventListener('click', () => { const el = $('#stub'); if (el) el.hidden = true; });

    const metricsBtn = $('#metricsBtn');
    if (metricsBtn) metricsBtn.addEventListener('click', openMetricsModal);

    const metricsClose = $('#metricsClose');
    if (metricsClose) metricsClose.addEventListener('click', () => { const m = $('#metricsModal'); if (m) m.hidden = true; });

    const labBtn = $('#labModalBtn');
    if (labBtn) labBtn.addEventListener('click', openLabModal);

    const labClose = $('#labModalClose');
    if (labClose) labClose.addEventListener('click', () => { const m = $('#labModal'); if (m) m.hidden = true; });

    const labForm = $('#labForm');
    if (labForm) labForm.addEventListener('submit', handleLabSubmit);

    const helpBtn = $('#helpBtn');
    if (helpBtn) helpBtn.addEventListener('click', () => { const m = $('#helpModal'); if (m) m.hidden = false; });

    const helpClose = $('#helpClose');
    if (helpClose) helpClose.addEventListener('click', () => { const m = $('#helpModal'); if (m) m.hidden = true; });

    for (const id of ['#metricsModal', '#labModal', '#helpModal']) {
      const m = $(id);
      if (m) {
        m.addEventListener('click', (e) => {
          if (e.target === m) m.hidden = true;
        });
      }
    }

    const pickerBtn = $('#lakePickerBtn');
    const dropdown = $('#lakeDropdown');
    if (pickerBtn && dropdown) {
      pickerBtn.addEventListener('click', () => {
        S.pickerOpen = !S.pickerOpen;
        dropdown.hidden = !S.pickerOpen;
        pickerBtn.setAttribute('aria-expanded', String(S.pickerOpen));
        if (S.pickerOpen) {
          const inp = $('#q');
          if (inp && inp.focus) inp.focus();
        }
      });
    }

    const searchInput = $('#q');
    if (searchInput) {
      searchInput.addEventListener('input', () => {
        S.searchQuery = searchInput.value;
        renderLakePicker();
      });
    }

    const lakeList = $('#lakeList');
    if (lakeList) {
      lakeList.addEventListener('click', (e) => {
        const item = e.target.closest ? e.target.closest('[data-id]') : null;
        if (item && item.dataset.id) selectLake(item.dataset.id);
      });
    }

    if (document.addEventListener) {
      document.addEventListener('click', (e) => {
        if (!S.pickerOpen) return;
        const wrap = e.target && e.target.closest ? e.target.closest('.lake-picker-wrap') : null;
        if (!wrap) {
          S.pickerOpen = false;
          if (dropdown) dropdown.hidden = true;
          if (pickerBtn) pickerBtn.setAttribute('aria-expanded', 'false');
        }
      });
    }

    $$('.drawer-tab').forEach((btn) => {
      btn.addEventListener('click', () => setDrawerTab(btn.dataset.tab));
    });

    const cascadeToggle = $('#cascadeToggle');
    if (cascadeToggle) {
      cascadeToggle.addEventListener('change', () => {
        S.showCascades = Boolean(cascadeToggle.checked);
        syncMapData();
      });
    }

    const bufferToggle = $('#bufferToggle');
    if (bufferToggle) {
      bufferToggle.addEventListener('change', () => {
        S.showBuffer = Boolean(bufferToggle.checked);
        syncMapData();
      });
    }

    const playBtn = $('#playBtn');
    if (playBtn) playBtn.addEventListener('click', togglePlay);

    const speedBtn = $('#speedBtn');
    if (speedBtn) speedBtn.addEventListener('click', cycleSpeed);

    const prevBtn = $('#prev');
    if (prevBtn) prevBtn.addEventListener('click', () => { stopPlay(); stepDate(-1); });

    const nextBtn = $('#next');
    if (nextBtn) nextBtn.addEventListener('click', () => { stopPlay(); stepDate(1); });

    const yearTrack = $('#yearTrack');
    if (yearTrack) {
      yearTrack.addEventListener('click', (e) => {
        const btn = e.target.closest ? e.target.closest('[data-year]') : null;
        if (btn && btn.dataset.year) {
          stopPlay();
          jumpToYear(Number(btn.dataset.year));
        }
      });
    }

    const slider = $('#timeSlider');
    if (slider) {
      slider.addEventListener('input', () => {
        stopPlay();
        const dates = getTimelineDates();
        const idx = Number(slider.value);
        if (dates[idx]) setDate(dates[idx], true);
      });
    }

    const dinp = $('#dinp');
    if (dinp) {
      dinp.addEventListener('change', () => {
        if (dinp.value) {
          stopPlay();
          setDate(dinp.value, true);
        }
      });
    }

    window.addEventListener('keydown', (e) => {
      const tag = e.target && e.target.tagName ? e.target.tagName.toLowerCase() : '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (e.key === 'Escape') {
        for (const id of ['#metricsModal', '#labModal', '#helpModal']) {
          const m = $(id);
          if (m && !m.hidden) { m.hidden = true; return; }
        }
        if (S.pickerOpen) {
          S.pickerOpen = false;
          const dd = $('#lakeDropdown');
          if (dd) dd.hidden = true;
          return;
        }
        if (S.selected) closeDrawer();
      } else if (e.key === ' ') {
        e.preventDefault();
        togglePlay();
      } else if (e.key === 'ArrowLeft') {
        stopPlay();
        stepDate(-1);
      } else if (e.key === 'ArrowRight') {
        stopPlay();
        stepDate(1);
      }
    });
  }

  window.__dash = {
    state: S,
    selectLake,
    closeDrawer,
    setMode,
    setBasemap,
    setTheme,
    setSeason,
    setCompareLake,
    cycleSpeed,
    setDate,
    stepDate,
    togglePlay,
    openMetricsModal,
    openLabModal,
    exportSelectedCsv,
    loadAll,
  };

  initMap();
  bindEvents();
  window.dashboardReady = loadAll(false);
})();
