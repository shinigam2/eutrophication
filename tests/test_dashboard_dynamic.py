"""Проверка динамической панели (frontend/index.html, style.css, app.js): настоящий скрипт панели + реальные HTTP-запросы.
FastAPI не нужен: тестовый сервер на stdlib повторяет маршруты main.py поверх того же конвейера
(pipeline.run_pipeline / pipeline.history) и тех же форматов ответов/ошибок. Нужен Node.js.
Запуск: python tests/test_dashboard_dynamic.py"""
import csv, json, os, re, shutil, socket, subprocess, sys, tempfile, threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
sys.path.insert(0, ".")
os.environ["PROJECT_ROOT"] = tempfile.mkdtemp()      # пустой проект: заглушки

from src.api import config, pipeline, stubs
from src.api.pipeline import HORIZON_DAYS, build_components, run_pipeline

C, LOG, HLOG, HITS = build_components(), [], [], {}
# метрики с MAE: проверяем доверительный интервал ±MAE и полосу на графике
C["metrics"] = {"regression": {"chl_a_10d": {"rmse": 3.8, "mae": 3.0, "r2": 0.71}},
                "classification": {"bloom": {"f1": 0.68, "roc_auc": 0.83}}}
QUALITY = {"decades_total": 69, "valid_share": 0.64, "interpolated_share": 0.03,
           "ice_share": 0.30, "cloudy_share": 0.03}

MODES = ("satellite", "lab")
LAB_COLUMNS = ["water_body_id", "sample_date", "chl_a_lab_ug_l", "secchi_depth_m",
               "total_phosphorus_ug_l", "source_note"]


def _read_rows(path):
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _lab_upload(body):
    """Повторяет проверки маршрута POST /lab/upload из main.py."""
    wb = body.get("water_body_id")
    if not wb:
        return 422, {"detail": "Нужен water_body_id"}
    if wb not in C["registry"]:
        return 404, {"detail": f"Водоём '{wb}' не найден. Доступны: {list(C['registry'])}"}
    try:
        sample = date.fromisoformat(str(body.get("sample_date")))
    except ValueError:
        return 422, {"detail": "sample_date должен быть YYYY-MM-DD"}
    if sample > date.today():
        return 422, {"detail": "Дата пробы не может быть в будущем"}
    try:
        chl = float(body.get("chl_a_lab_ug_l"))
    except (TypeError, ValueError):
        return 422, {"detail": "chl_a_lab_ug_l должен быть числом"}
    if not 0 < chl <= 1000:
        return 422, {"detail": "Хлорофилл-а вне диапазона 0–1000 мкг/л"}
    record = dict(water_body_id=wb, sample_date=sample.isoformat(), chl_a_lab_ug_l=chl,
                  secchi_depth_m=body.get("secchi_depth_m") or "",
                  total_phosphorus_ug_l=body.get("total_phosphorus_ug_l") or "",
                  source_note=body.get("source_note") or "Загружено через веб-панель")
    path = config.find_lab_csv(for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = _read_rows(path)
    if any((r.get("water_body_id") == wb and r.get("sample_date") == record["sample_date"]) for r in old):
        return 409, {"detail": f"Проба {wb} на {sample} уже есть в таблице"}
    new = not old
    with path.open("a" if not new else "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LAB_COLUMNS)
        if new:
            w.writeheader()
        w.writerow(record)
    return 200, {"saved": True, "path": str(path), "record": record, "satellite_comparison": None,
                 "message": "Проба сохранена. В витрину Части 1 она попадёт после пересборки ETL."}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body):
        raw = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

    def hit(self, u):
        key = "/predict" if u.path.startswith("/predict/") else "/history" if u.path.startswith("/history/") else u.path
        HITS[key] = HITS.get(key, 0) + 1

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        self.hit(u)
        if u.path == "/__log": return self.send(200, LOG)
        if u.path == "/__hlog": return self.send(200, HLOG)
        if u.path == "/__hits": return self.send(200, HITS)
        if u.path == "/__clear": LOG.clear(); HLOG.clear(); return self.send(200, {})
        if u.path == "/__add_lake":                      # имитация: конвейер части 1 добавил озеро
            b = dict(min_lat=54.80, min_lon=26.70, max_lat=54.84, max_lon=26.76)
            stubs.WATER_BODIES["novoe"] = dict(id="novoe", name="Новое озеро", type="lake", lat=54.82, lon=26.73, bbox=b,
                geometry=None if q.get("nogeo") else stubs._polygon(b), area_km2=5.0, mean_depth_m=3.0,
                base=2.0, amp=20.0, lab_base=1.0, lab_amp=4.0)
            C["timeline_cache"].clear()
            return self.send(200, {})
        if u.path == "/__remove_lake":
            stubs.WATER_BODIES.pop("novoe", None)
            C["timeline_cache"].clear()
            return self.send(200, {})
        if u.path == "/":
            return self.send(200, {"status": "ok", "horizon_days": HORIZON_DAYS,
                                   "components": C["status"], "modes": pipeline.mode_catalog(C)})
        if u.path in ("/lakes", "/water-bodies"):
            return self.send(200, [{**wb, "modes": pipeline.modes_for(C, wb_id)}
                                   for wb_id, wb in C["registry"].items()])
        if u.path == "/timeline":
            mode = q.get("mode", ["satellite"])[0]
            if mode not in MODES: return self.send(422, {"detail": "Режим только satellite или lab"})
            return self.send(200, pipeline.timeline(C, mode))
        if u.path == "/metrics":
            return self.send(200, {**C["metrics"], "data_quality": QUALITY,
                                   "modes": pipeline.mode_catalog(C), "horizon_days": HORIZON_DAYS})
        if u.path.startswith("/predict/"):
            wb = u.path.split("/", 2)[2]
            ds = q.get("date", [None])[0]
            mode = q.get("mode", ["satellite"])[0]
            LOG.append({"wb": wb, "date": ds, "mode": mode})
            if wb not in C["registry"]: return self.send(404, {"detail": f"Водоём '{wb}' не найден"})
            if mode not in MODES: return self.send(422, {"detail": "Режим только satellite или lab"})
            try: requested = date.fromisoformat(ds) if ds else config.DEFAULT_DATE
            except ValueError: return self.send(422, {"detail": [{"msg": "Input should be a valid date"}]})
            try:
                res = run_pipeline(C, wb, requested, mode)
            except pipeline.ModeUnavailable as e:
                return self.send(404, {"detail": str(e)})
            if res is None:
                prov = C["providers"].get(mode) or C["provider"]
                lo, hi = prov.date_range(wb)
                return self.send(404, {"detail": f"Нет данных на {requested}. Диапазон: {lo} .. {hi}"})
            return self.send(200, res)
        if u.path.startswith("/history/"):
            wb = u.path.split("/", 2)[2]
            mode = q.get("mode", ["satellite"])[0]
            ds = q.get("date", [None])[0]
            limit = int(q.get("limit", ["36"])[0])
            HLOG.append({"wb": wb, "mode": mode, "date": ds, "limit": limit})
            if wb not in C["registry"]: return self.send(404, {"detail": f"Водоём '{wb}' не найден"})
            try: end = date.fromisoformat(ds) if ds else config.DEFAULT_DATE
            except ValueError: return self.send(422, {"detail": "bad date"})
            try:
                return self.send(200, pipeline.history(C, wb, mode, end, limit))
            except pipeline.ModeUnavailable as e:
                return self.send(404, {"detail": str(e)})
        self.send(404, {"detail": "Not Found"})

    def do_POST(self):
        u = urlparse(self.path); self.hit(u)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(422, {"detail": "Тело запроса не JSON"})
        if u.path == "/lab/upload":
            code, payload = _lab_upload(body)
            return self.send(code, payload)
        self.send(404, {"detail": "Not Found"})


FRONT = Path(__file__).resolve().parents[1] / "frontend"
DYNAMIC_IDS = {"drawerClose", "chart", "histWin"}            # создаются панелью по ходу работы


def check_files():
    """Файлы панели согласованы между собой: всё, на что ссылается JS, есть в HTML и CSS."""
    html, js, css = [(FRONT / f).read_text(encoding="utf-8") for f in ("index.html", "app.js", "style.css")]
    assert 'href="style.css"' in html and 'src="app.js"' in html
    assert "<style>" not in html and "<script>" not in html, "стили и код вынесены в отдельные файлы"
    assert "const D =" not in js, "данных внутри страницы быть не должно"
    for ep in ("/lakes", "/timeline", "/predict/", "/history/", "/metrics", "/lab/upload"):
        assert ep in js, ep
    ids = set(re.findall(r"\$\('#([\w-]+)", js))
    missing = sorted(i for i in ids - DYNAMIC_IDS if f'id="{i}"' not in html)
    assert not missing, f"JS обращается к элементам, которых нет в index.html: {missing}"
    used = set()
    for chunk in re.findall(r'class="([^"]*)"', js):
        cleaned = re.sub(r"\$\{[^}]*\}", " ", chunk)
        used |= {t for t in re.split(r"\s+", cleaned) if t and re.fullmatch(r"[A-Za-z_][\w-]*", t)}
    in_html = {t for chunk in re.findall(r'class="([^"]*)"', html) for t in chunk.split()}
    no_style = sorted(t for t in used if not re.search(r"\." + re.escape(t) + r"(?![\w-])", css) and t not in in_html)
    assert not no_style, f"в style.css нет правил для классов из JS: {no_style}"
    print(f"Файлы панели согласованы: {len(ids)} элементов, {len(used)} классов")


def run_node(front, base, mode=""):
    r = subprocess.run(["node", "tests/dashboard_harness.js", str(front), base, mode], capture_output=True, text=True, timeout=120)
    print(r.stdout.strip() or r.stderr.strip())
    assert r.returncode == 0, r.stderr


def test_dashboard_dynamic():
    main()


def main():
    check_files()
    if not shutil.which("node"):
        print("Node.js не найден: тест панели пропущен"); return
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        run_node(FRONT, f"http://127.0.0.1:{srv.server_port}")
    finally:
        srv.shutdown()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()   # порт, где никого нет
    run_node(FRONT, f"http://127.0.0.1:{dead}", "dead")
    print("OK")


if __name__ == "__main__":
    main()
