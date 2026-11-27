// Прогон скрипта веб-ГИС панели в Node: имитация DOM / MapLibre GL JS / Chart.js, реальные HTTP-запросы.
// node tests/dashboard_harness.js <каталог frontend> <api-url> [dead]
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const [, , frontDir, base, mode] = process.argv;
const code = fs.readFileSync(frontDir + '/app.js', 'utf8');

const els = {};
class El {
  constructor(sel) {
    this.sel = sel;
    this.attrs = {};
    this.ls = {};
    this.innerHTML = '';
    this.textContent = '';
    this.hidden = false;
    this.checked = true;
    this.value = '';
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.children = [];
    this.id = sel.replace(/^#/, '');
    this.cl = new Set();
    this.classList = {
      toggle: (c, f) => { (f === undefined ? !this.cl.has(c) : f) ? this.cl.add(c) : this.cl.delete(c); },
      add: (c) => this.cl.add(c),
      remove: (c) => this.cl.delete(c),
      contains: (c) => this.cl.has(c),
    };
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }
  async fire(t, e = {}) { for (const f of this.ls[t] || []) await f({ target: this, preventDefault() {}, stopPropagation() {}, ...e }); }
  querySelector(sel) { return get(sel); }
  querySelectorAll() { return []; }
  closest() { return null; }
  reset() { }
  focus() { }
}

const get = (sel) => (els[sel] = els[sel] || new El(sel));
const segEls = ['satellite', 'lab'].map((m) => {
  const e = new El('.seg-' + m);
  e.dataset = { mode: m };
  if (m === 'satellite') e.cl.add('active');
  return e;
});
const baseEls = ['dark', 'light', 'satellite', 'hybrid'].map((b) => {
  const e = new El('.base-' + b);
  e.dataset = { basemap: b };
  if (b === 'dark') e.cl.add('active');
  return e;
});
const tabEls = ['overview', 'history'].map((t) => {
  const e = new El('.tab-' + t);
  e.dataset = { tab: t };
  if (t === 'overview') e.cl.add('active');
  return e;
});

const themeEls = ['chl', 'tsi', 'coverage'].map((t) => {
  const e = new El('.theme-' + t);
  e.dataset = { theme: t };
  if (t === 'chl') e.cl.add('active');
  return e;
});
const seasonEls = ['all', 'open', 'summer'].map((s) => {
  const e = new El('.season-' + s);
  e.dataset = { season: s };
  if (s === 'open') e.cl.add('active');
  return e;
});

const winListeners = {};
globalThis.document = {
  body: new El('body'),
  createElement: (tag) => new El('<' + tag + '>'),
  querySelector: get,
  querySelectorAll: (sel) => {
    if (sel === '#modeSwitch .seg') return segEls;
    if (sel === '#basemapSwitch .mode-btn') return baseEls;
    if (sel === '.drawer-tab') return tabEls;
    if (sel === '#themeSwitch .theme-btn') return themeEls;
    if (sel === '#seasonSwitch .season-btn') return seasonEls;
    return [];
  },
  addEventListener: () => {},
};
globalThis.window = globalThis;
globalThis.innerWidth = 1440;
globalThis.addEventListener = (t, f) => { (winListeners[t] = winListeners[t] || []).push(f); };
globalThis.location = { search: '?api=' + encodeURIComponent(base), protocol: 'http:', origin: 'http://unused' };

// Mock FormData для тестирования POST /lab/upload
globalThis.FormData = class {
  constructor(form) { this._data = (form && form._fields) || {}; }
  get(k) { return this._data[k] == null ? '' : this._data[k]; }
};

// Mock MapLibre GL JS с проверкой источников, слоёв, стилей ({diff: false}) и маркеров
const mapState = {
  styles: [],
  styleOpts: [],
  sources: {},
  layers: {},
  markers: [],
  flyCalls: [],
  paintUpdates: [],
};
class FakeMap {
  constructor(opts) {
    this.opts = opts;
    this.ls = {};
    this._loaded = false;
    mapState.styles.push(opts.style);
    setTimeout(() => { this._loaded = true; this._emit('style.load'); this._emit('load'); }, 5);
  }
  isStyleLoaded() { return this._loaded; }
  addControl() {}
  on(ev, layerOrFn, fn) {
    const cb = typeof layerOrFn === 'function' ? layerOrFn : fn;
    (this.ls[ev] = this.ls[ev] || []).push(cb);
  }
  _emit(ev, payload) { for (const f of this.ls[ev] || []) f(payload || {}); }
  getCanvas() { return { style: {} }; }
  setStyle(style, opts) {
    mapState.styles.push(style);
    mapState.styleOpts.push(opts || {});
    mapState.sources = {};
    mapState.layers = {};
    this._loaded = false;
    setTimeout(() => { this._loaded = true; this._emit('style.load'); }, 5);
  }
  getSource(id) { return mapState.sources[id]; }
  addSource(id, cfg) {
    const src = { id, data: cfg.data, setData(d) { this.data = d; } };
    mapState.sources[id] = src;
  }
  getLayer(id) { return mapState.layers[id]; }
  addLayer(cfg) { mapState.layers[cfg.id] = cfg; }
  setPaintProperty(layerId, prop, val) { mapState.paintUpdates.push({ layerId, prop, val }); }
  flyTo(opts) { mapState.flyCalls.push(opts); }
  jumpTo() {}
}
class FakeMarker {
  constructor(opts) { this.el = opts.element; this.lngLat = null; this.removed = false; }
  setLngLat(ll) { this.lngLat = ll; return this; }
  addTo() { mapState.markers.push(this); return this; }
  remove() { this.removed = true; }
}
globalThis.maplibregl = { Map: FakeMap, Marker: FakeMarker, NavigationControl: class {} };
globalThis.Chart = class {
  constructor(el, cfg) { this.el = el; this.data = cfg.data; this.options = cfg.options; globalThis.__chart = this; }
  update() {}
  destroy() {}
};

for (const s of ['#stub', '#apierr', '#panel', '#metricsModal', '#labModal', '#helpModal', '#lakeDropdown', '#mapTooltip']) {
  get(s).hidden = true;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const J = async (p) => (await fetch(base + p)).json();
const hits = () => J('/__hits');
const hlog = () => J('/__hlog');

(async () => {
  vm.runInThisContext(code, { filename: 'dashboard-script' });
  await window.dashboardReady;
  await sleep(60);

  if (mode === 'dead') {
    assert.strictEqual(get('#apierr').hidden, false, 'баннер ошибки связи должен показываться');
    assert(get('#apierr-msg').textContent.includes('Нет связи'));
    await get('#retry').fire('click');
    await sleep(120);
    assert.strictEqual(get('#apierr').hidden, false);
    console.log('JS harness (сервер недоступен): OK');
    process.exit(0);
  }

  // 1. Первая загрузка: /lakes + /timeline, GeoJSON-полигоны, маркеры и слои выделения
  const h0 = await hits();
  assert.strictEqual(h0['/lakes'], 1, 'список водоёмов запрошен через /lakes');
  assert.strictEqual(h0['/timeline'], 1, 'линия времени запрошена через /timeline');
  assert.strictEqual(window.__dash.state.lakes.length, 2);
  assert.strictEqual(window.__dash.state.date, '2026-08-01');
  assert(mapState.sources.lakes && mapState.sources.lakes.data.features.length === 2, 'на карте 2 полигона');
  assert(mapState.layers['selected-lake-halo'] && mapState.layers['selected-lake-outline']);
  assert(mapState.layers['selected-buffer-line'] && mapState.layers['cascade-arrow-heads']);
  assert.strictEqual(mapState.markers.length, 2, 'созданы 2 плавающих бейджа озёр');
  assert.strictEqual(get('#stub').hidden, false, 'в режиме заглушек показан честный статус');
  assert.strictEqual(get('#panel').hidden, true, 'правый drawer скрыт до клика по озеру');

  // 2. Переключение всех 4 подложек с { diff: false } + проверка обновления карты после смены подложки!
  for (const bm of ['light', 'satellite', 'hybrid', 'dark']) {
    window.__dash.setBasemap(bm);
    await sleep(30);
    assert.strictEqual(document.body.dataset.basemap, bm);
    assert(mapState.sources.lakes && mapState.layers['selected-lake-outline'], `слои пересозданы после setBasemap(${bm})`);
  }
  assert(mapState.styleOpts.every((o) => o.diff === false), 'setStyle всегда вызывается с { diff: false }');

  // 3. Клик по озеру: selected-lake заполняется полигоном, drawer открывается, flyTo срабатывает
  await window.__dash.selectLake('zaslavskoe');
  await sleep(60);
  assert.strictEqual(get('#panel').hidden, false);
  assert.strictEqual(mapState.sources['selected-lake'].data.features.length, 1, 'выделена площадь выбранного озера');
  assert.strictEqual(mapState.sources['selected-lake'].data.features[0].properties.id, 'zaslavskoe');
  assert(get('#panelHeader').innerHTML.includes('Заславское'));
  assert(get('#panelBody').innerHTML.includes('Прогноз на +10 дней'));
  assert(get('#panelBody').innerHTML.includes('±MAE'));
  assert(mapState.flyCalls.length >= 1, 'камера выполнила flyTo к озеру');

  // 4. Вкладка «График и паспорт» + режим сравнения 2 озёр (Заславское vs Нарочь)
  await tabEls[1].fire('click');
  await sleep(40);
  assert(get('#panelBody').innerHTML.includes('Паспорт водоёма'));
  assert(globalThis.__chart, 'график Chart.js создан');
  await window.__dash.setCompareLake('naroch');
  await sleep(50);
  assert.strictEqual(mapState.sources['compared-lake'].data.features.length, 1, 'второе озеро подсвечено на карте');
  assert(globalThis.__chart.data.datasets.some((d) => d.label.includes('Сравнение:')), 'на графике появилась кривая сравнения');
  assert((await hlog()).length >= 2, 'загружена история для обоих озёр');

  // 5. Тематические режимы карты (Chl-a -> TSI -> Спутник %), скорость и фильтр сезонов (Лето)
  window.__dash.setTheme('tsi');
  assert(get('#legendTitle').textContent.includes('TSI'));
  window.__dash.setTheme('coverage');
  assert(get('#legendTitle').textContent.includes('Покрытие'));
  window.__dash.setTheme('chl');
  assert(get('#legendItems').innerHTML.includes('Норма (&lt; 25 мкг/л)') || get('#legendItems').innerHTML.includes('Норма (< 25 мкг/л)'));
  assert(get('#legendItems').innerHTML.includes('Предупреждение (≥ 25)'));
  assert(!get('#legendItems').innerHTML.includes('Внимание (15–25)'), '3-й уровень Внимание (YELLOW) должен быть убран');

  window.__dash.cycleSpeed();
  assert.strictEqual(get('#speedBtn').textContent, '2×');
  window.__dash.setSeason('summer');
  assert.strictEqual(window.__dash.state.season, 'summer');
  window.__dash.setSeason('all');

  // 6. Линия времени обновляет карту (даже во время Play и после смены подложек!)
  await tabEls[0].fire('click');
  window.__dash.setDate('2025-01-11', true);
  await sleep(220);
  assert(get('#panelHeader').innerHTML.includes('Ледяной покров') || get('#panelBody').innerHTML.includes('Ледяной покров'));
  assert(mapState.sources.lakes.data.features.every((f) => f.properties.obs_status === 'ice_cover'));
  assert.strictEqual(mapState.markers[0].el.children[1].textContent, 'Лёд', 'маркер на карте обновился на Лёд');

  // 7. Переключение режима данных Спутник -> Лаборатория
  await window.__dash.setMode('lab');
  await sleep(60);
  assert.strictEqual(window.__dash.state.mode, 'lab');
  assert(get('#sub').textContent.includes('Лабораторные'));

  // 8. Загрузка лабораторной пробы (POST /lab/upload)
  const mkLabForm = (chl) => ({
    _fields: {
      water_body_id: 'zaslavskoe',
      sample_date: '2025-07-05',
      chl_a_lab_ug_l: chl,
      secchi_depth_m: '1.8',
      total_phosphorus_ug_l: '42',
      source_note: 'тест новой панели',
    },
    reset() {},
  });
  await get('#labForm').fire('submit', { target: mkLabForm('18.5') });
  await sleep(60);
  assert(get('#labMsg').textContent.includes('Проба сохранена'), get('#labMsg').textContent);
  await get('#labForm').fire('submit', { target: mkLabForm('19.0') });
  await sleep(60);
  assert(get('#labMsg').className.includes('err'), 'дубль должен вернуть ошибку');
  await get('#labForm').fire('submit', { target: mkLabForm('5000') });
  await sleep(60);
  assert(get('#labMsg').className.includes('err'), 'значение вне диапазона должно вернуть ошибку');

  // 9. Модалка метрик
  await get('#metricsBtn').fire('click');
  await sleep(60);
  assert.strictEqual(get('#metricsModal').hidden, false);
  const metHtml = get('#metrics').innerHTML;
  for (const s of ['RMSE', 'MAE', 'R²', 'Доступность режимов', 'Качество данных', 'pill on']) {
    assert(metHtml.includes(s), 'в метриках нет: ' + s);
  }
  await get('#metricsClose').fire('click');
  assert.strictEqual(get('#metricsModal').hidden, true);

  // 10. Живое обновление: добавление нового озера на сервере + кнопка «Обновить»
  await fetch(base + '/__add_lake');
  await get('#refresh').fire('click');
  await sleep(60);
  assert.strictEqual(window.__dash.state.lakes.length, 3, 'появилось третье озеро');
  assert.strictEqual(mapState.sources.lakes.data.features.length, 3, 'на карте 3 полигона');
  await fetch(base + '/__remove_lake');
  await get('#refresh').fire('click');
  await sleep(60);
  assert.strictEqual(window.__dash.state.lakes.length, 2);

  console.log('JS harness (MapLibre + Live Sync + Highlight + 6 Features): OK');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL:', e.stack || e);
  process.exit(1);
});
