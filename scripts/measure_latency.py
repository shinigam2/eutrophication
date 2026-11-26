"""
Измерение времени обработки запроса прогноза (требование НТ-И2: не более 2 с).
    python scripts/measure_latency.py [--n 200]                  # конвейер в процессе, без HTTP
    python scripts/measure_latency.py --url http://127.0.0.1:8000 # полный запрос к запущенному серверу
Замер на заглушках для отчёта не годится: запускайте после подключения реальных частей.
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.api import config
from src.api.pipeline import build_components, run_pipeline


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200, help="число запросов")
    ap.add_argument("--url", default="", help="адрес запущенного сервера (режим HTTP)")
    args = ap.parse_args()

    c = build_components()
    jobs = [(w, d) for w in c["registry"] for d in c["provider"].available_dates(w) if d >= config.TEST_START]
    if not jobs:
        sys.exit("Нет дат для измерения")
    if c["stub_parts"]:
        print("ВНИМАНИЕ: заглушки вместо", ", ".join(c["stub_parts"]), "- значения для отчёта брать нельзя\n")

    times = []
    for i in range(args.n):
        w, d = jobs[i % len(jobs)]
        t0 = time.perf_counter()
        if args.url:
            with urllib.request.urlopen(f"{args.url.rstrip('/')}/predict/{w}?date={d}") as r:
                json.load(r)
        else:
            run_pipeline(c, w, d)
        times.append((time.perf_counter() - t0) * 1000)

    mode = "HTTP-запрос" if args.url else "конвейер в процессе"
    print(f"Режим: {mode}; запросов: {args.n}; различных пар (водоём, дата): {len(jobs)}")
    print(f"медиана {statistics.median(times):.2f} мс | p95 {pct(times, 95):.2f} мс | максимум {max(times):.2f} мс")
    print(f"требование НТ-И2 (2000 мс): {'выполнено' if max(times) <= 2000 else 'НЕ выполнено'}")


if __name__ == "__main__":
    main()
