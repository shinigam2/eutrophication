"""
Самопроверка интеграции:  python -m src.api.check_integration
Показывает, какие файлы нашлись, какие части реальные/заглушки, прогоняет конвейер по всем
водоёмам, режимам и датам и подсказывает даты для демонстрации GREEN / RED (2 уровня).
Код возврата 0 = всё в порядке, 1 = есть проблемы.
"""
import sys
from collections import Counter, defaultdict

from src.api import config, pipeline
from src.api.pipeline import PART_NAMES, build_components, run_pipeline

OK, BAD = "[ ok ]", "[FAIL]"


def main() -> int:
    print(f"Корень проекта: {config.root()}\n")
    print("Найденные файлы:")
    for label, p in (("реестр водоёмов", config.find_registry()),
                     ("спутниковая витрина", config.find_features("satellite")),
                     ("лабораторная витрина", config.find_features("lab")),
                     ("метрики моделей", config.find_metrics()),
                     ("отчёт качества", config.find_quality()),
                     ("онтология", config.find_ontology())):
        print(f"  {label:20} {p or '— не найден'}")
    for mode in config.MODES:
        models = config.find_models(mode)
        print(f"  {('модели ' + mode):20} {models[0].parent if models else '— не найдены'}\n")

    try:
        c = build_components()
    except Exception as e:   # файл найден, но не подошёл
        print(f"{BAD} Не удалось подключить компоненты: {type(e).__name__}: {e}")
        return 1
    print("Состояние частей:")
    for k, v in c["status"].items():
        print(f"  {OK if str(v).startswith('реальная') else '[stub]'} {PART_NAMES[k]}: {v}")
    print("\nРежимы:")
    for mode, info in pipeline.mode_catalog(c).items():
        print(f"  {mode:10} доступен: {'да' if info['available'] else 'нет'}, файл: {info['data_file'] or '—'}"
              f", водоёмов: {len(info['lakes'])}" + (f", причина: {info['reason']}" if info["reason"] else ""))

    try:
        from src.api.schemas import AlertResponse
    except ImportError:
        AlertResponse = None
        print("(pydantic не установлен — схему ответа не проверяю)")

    n_geo = sum(1 for w in c["registry"].values() if w.get("geometry"))
    print(f"\nОзёр в реестре: {len(c['registry'])}, из них с полигоном: {n_geo}"
          + ("" if n_geo == len(c["registry"]) else "  (без полигона на карте рисуется прямоугольник BBox)"))
    if c.get("reload_error"):
        print(f"  {BAD} ошибка перечитывания файлов: {c['reload_error']}")

    problems, demo = 0, defaultdict(dict)
    print("\nПрогон по водоёмам и режимам:")
    for wb in c["registry"]:
        modes = pipeline.modes_for(c, wb)
        for mode in modes:
            prov = c["providers"].get(mode)
            dates = [d for d in prov.available_dates(wb) if d >= config.TEST_START] if prov else []
            dates = dates or (prov.available_dates(wb) if prov else [])
            errors, statuses = 0, Counter()
            for d in dates:
                try:
                    r = run_pipeline(c, wb, d, mode)
                    if r is None:
                        raise ValueError("нет данных")
                    if AlertResponse:
                        AlertResponse(**r)
                    statuses[r["obs_status"] or "нет столбца obs_status"] += 1
                    if r["alert_level"]:
                        demo[(wb, mode)].setdefault(r["alert_level"], d)
                except Exception as e:
                    errors += 1
                    if errors == 1:
                        print(f"  {BAD} {wb}/{mode} {d}: {type(e).__name__}: {e}")
            problems += errors
            lo, hi = (dates[0], dates[-1]) if dates else ("-", "-")
            print(f"  {OK if not errors else BAD} {wb}/{mode}: дат {len(dates)} ({lo} .. {hi}), ошибок {errors}")
            print("         статусы: " + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())))
            print("         демо-даты: " + (", ".join(f"{k} {v}" for k, v in sorted(demo[(wb, mode)].items()))
                                           or "—"))
    print("\n" + ("Всё подключено, проблем нет." if not problems else f"Есть проблемы: {problems}."))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
