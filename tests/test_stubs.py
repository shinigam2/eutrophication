"""Проверка логики заглушек без FastAPI: python tests/test_stubs.py"""
import sys
sys.path.insert(0, ".")
from datetime import date
from src.api.stubs import *


def test_stub_contract():
    p, m, e = StubDataProvider(), StubPredictor(), StubGraphEngine()
    assert snap_to_decade(date(2025, 8, 5)) == date(2025, 8, 1)
    assert snap_to_decade(date(2025, 8, 16)) in (date(2025, 8, 11), date(2025, 8, 21))
    assert p.get_features("nope", date(2025, 8, 1)) is None
    assert p.get_features("naroch", date(2030, 1, 1)) is None
    for wb in WATER_BODIES:
        for d in (date(2025, 1, 11), date(2025, 5, 21), date(2025, 7, 11), date(2025, 8, 1)):
            f = p.get_features(wb, d)
            if f["chl_a_current"] is None:                      # зима/облачность: прогноза нет
                assert f["obs_status"] in ("ice_cover", "cloudy_missing", "insufficient_pixels")
                print(f"{wb:11} {d} now=  —   -> нет наблюдения ({f['obs_status']})")
                continue
            chl, prob = m.predict(f)
            v = e.assess(wb, "name", d, chl, prob, f["temp_mean_14d"], f["calm_fraction_7d"])
            print(f"{wb:11} {d} now={f['chl_a_current']:5} -> {chl:5} P={prob:.2f} "
                  f"{v.alert_level:6} {v.trophic_class}")
    for mode in ("satellite", "lab"):
        prov, pred = StubDataProvider(mode), StubPredictor(mode)
        frame = prov.history_frame("naroch", date(2025, 7, 1), 12)
        assert len(frame) == 12
        assert all(len(x) == 2 for x in pred.predict_many(frame))     # пакетный прогноз для истории
    print("OK")


if __name__ == "__main__":
    test_stub_contract()
