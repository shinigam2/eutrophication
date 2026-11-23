"""Автоматическое извлечение и валидация реальных лабораторных данных БГУ.

Источники (подлинные PDF-бюллетени НИЛ гидроэкологии БГУ в data/raw/lab_pdfs/):
  1. bulleten_2018_+.pdf — «Бюллетень экологического состояния озер Нарочь, Мястро,
     Баторино (2018 год)» (Табл. 2.3 прозрачность, Табл. 2.11 общий фосфор,
     Табл. 2.17 хлорофилл-а; Приложение с. 94–108 — 15-летний ряд оз. Нарочь
     на горизонте 0,5 м за 2004–2018 гг. с точными датами отбора проб).
  2. bulletin-2019_.pdf — «Бюллетень экологического состояния озер Нарочь, Мястро,
     Баторино (2019 год)» (Табл. 2.3, Табл. 2.11, Табл. 2.17).

Принцип научной честности:
  - Ни одно число не придумывается и не округляется «на глаз»: парсер читает
    текстовый слой PDF через `pdftotext -layout` и проверяет контрольные суммы
    (например, 6 месяцев V–X для каждого озера и каждого показателя).
  - Интегральные пробы (фильтр 0,4 мкм, 2018–2019 гг., 3 озера) и поверхностные
    пробы приложения Нарочи (фильтр 1,1–1,5 мкм, горизонт 0,5 м, 2004–2018 гг.)
    явно разделены колонкой `sample_type`, чтобы никогда не смешивать разные
    методики фильтрации.

Выходы:
  - data/lab/lab_measurements.csv         — основная таблица 2018–2019 (Нарочь, Мястро, Баторино)
                                            в точном формате DATA_CONTRACT.md (можно открывать в Excel
                                            и дополнять свежими пробами!).
  - data/lab/naroch_surface_2004_2018.csv — 15-летний ряд точных дат по оз. Нарочь (0,5 м).
  - data/interim/lab_clean.parquet        — проверенная объединённая таблица для Части 1 и Части 2.

Запуск:
  python -m src.etl.lab_loader
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.etl.config import INTERIM_DIR, LAB_DIR, RAW_DIR, load_config

LAB_PDF_DIR = RAW_DIR / "lab_pdfs"
PDF_2018 = LAB_PDF_DIR / "bulleten_2018_+.pdf"
PDF_2019 = LAB_PDF_DIR / "bulletin-2019_.pdf"

LAB_CSV_PATH = LAB_DIR / "lab_measurements.csv"
NAROCH_APP_CSV_PATH = LAB_DIR / "naroch_surface_2004_2018.csv"
LAB_CLEAN_PARQUET_PATH = INTERIM_DIR / "lab_clean.parquet"

MONTHS_VEG = [5, 6, 7, 8, 9, 10]
FLOAT_RE = re.compile(r"\d+(?:,\d+)?")


def _pdftotext_pages(pdf_path: Path, first_page: int, last_page: int) -> str:
    """Извлекает текст с сохранением табличной разметки из диапазона страниц PDF."""
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF-бюллетень БГУ не найден: {pdf_path}")
    cmd = [
        "pdftotext",
        "-layout",
        "-f",
        str(first_page),
        "-l",
        str(last_page),
        str(pdf_path),
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return proc.stdout


def _parse_ru_floats(line: str, expected: int = 6) -> list[float]:
    """Извлекает ровно `expected` вещественных чисел (с запятой) с конца строки таблицы."""
    tokens = FLOAT_RE.findall(line)
    if len(tokens) < expected:
        raise ValueError(
            f"Ожидалось >= {expected} чисел в строке, найдено {len(tokens)}: {line!r}"
        )
    return [float(tok.replace(",", ".")) for tok in tokens[-expected:]]


def _parse_table_2_17_chl(text: str, year: int) -> dict[str, dict[int, dict[str, float]]]:
    """Парсит Таблицу 2.17 (хлорофилл-а на фильтрах 0,4 мкм и 1,5 мкм, месяцы V–X)."""
    start_idx = text.find("Таблица 2.17")
    if start_idx == -1:
        start_idx = text.find("Table 2.17")
    if start_idx == -1:
        raise ValueError(f"Не найдена Таблица 2.17 в бюллетене {year} г.")

    section = text[start_idx:]
    lines = section.splitlines()

    current_block: str | None = None
    # block_key -> {filter_key -> [6 monthly floats]}
    parsed: dict[str, dict[str, list[float]]] = {}

    for line in lines:
        low = line.lower()
        if "малый плес" in low:
            current_block = "naroch_small"
            parsed.setdefault(current_block, {})
        elif "большой плес" in low:
            current_block = "naroch_big"
            parsed.setdefault(current_block, {})
        elif "озеро мястро" in low:
            current_block = "myastro"
            parsed.setdefault(current_block, {})
        elif "озеро баторино" in low:
            current_block = "batorino"
            parsed.setdefault(current_block, {})
        elif current_block is not None:
            if "1,5 мкм" in low and "схл" in low:
                parsed[current_block]["f15"] = _parse_ru_floats(line, 6)
            elif "0,4 мкм" in low and "схл" in low:
                parsed[current_block]["f04"] = _parse_ru_floats(line, 6)
            # После блока Баторино останавливаемся, когда оба фильтра прочитаны
            if (
                current_block == "batorino"
                and "f04" in parsed["batorino"]
                and "f15" in parsed["batorino"]
            ):
                break

    required_blocks = ["naroch_small", "naroch_big", "myastro", "batorino"]
    for blk in required_blocks:
        if blk not in parsed or "f04" not in parsed[blk] or "f15" not in parsed[blk]:
            raise ValueError(
                f"Неполный разбор Таблицы 2.17 ({year} г.) для блока {blk}: {parsed.get(blk)}"
            )

    result: dict[str, dict[int, dict[str, float]]] = {}
    for lake_id in ("naroch", "myastro", "batorino"):
        result[lake_id] = {}
        for idx, month in enumerate(MONTHS_VEG):
            if lake_id == "naroch":
                v04_s = parsed["naroch_small"]["f04"][idx]
                v04_b = parsed["naroch_big"]["f04"][idx]
                v15_s = parsed["naroch_small"]["f15"][idx]
                v15_b = parsed["naroch_big"]["f15"][idx]
                result[lake_id][month] = {
                    "chl_04": round((v04_s + v04_b) / 2.0, 3),
                    "chl_15": round((v15_s + v15_b) / 2.0, 3),
                    "buoy_note": f"Малый плес={v04_s:.2f}, Большой плес={v04_b:.2f} мкг/л (0,4 мкм)",
                }
            else:
                result[lake_id][month] = {
                    "chl_04": parsed[lake_id]["f04"][idx],
                    "chl_15": parsed[lake_id]["f15"][idx],
                    "buoy_note": "пелагиальная станция",
                }
    return result


def _parse_table_2_3_secchi(text: str, year: int) -> dict[str, dict[int, float]]:
    """Парсит Таблицу 2.3 (прозрачность воды по диску Секки, м, месяцы V–X)."""
    start_idx = text.find("Таблица 2.3")
    if start_idx == -1:
        raise ValueError(f"Не найдена Таблица 2.3 в бюллетене {year} г.")

    lines = text[start_idx:].splitlines()
    raw_vals: dict[str, list[float]] = {}
    for line in lines:
        low = line.lower()
        if "малый плес" in low and "нарочь" in low:
            raw_vals["naroch_small"] = _parse_ru_floats(line, 6)
        elif "большой плес" in low and "naroch_small" in raw_vals:
            raw_vals["naroch_big"] = _parse_ru_floats(line, 6)
        elif low.strip().startswith("мястро"):
            raw_vals["myastro"] = _parse_ru_floats(line, 6)
        elif low.strip().startswith("баторино"):
            raw_vals["batorino"] = _parse_ru_floats(line, 6)
            break

    for k in ("naroch_small", "naroch_big", "myastro", "batorino"):
        if k not in raw_vals:
            raise ValueError(f"Не найдена строка {k} в Таблице 2.3 ({year} г.)")

    out: dict[str, dict[int, float]] = {"naroch": {}, "myastro": {}, "batorino": {}}
    for idx, month in enumerate(MONTHS_VEG):
        out["naroch"][month] = round(
            (raw_vals["naroch_small"][idx] + raw_vals["naroch_big"][idx]) / 2.0, 2
        )
        out["myastro"][month] = raw_vals["myastro"][idx]
        out["batorino"][month] = raw_vals["batorino"][idx]
    return out


def _parse_table_2_11_tp(text: str, year: int) -> dict[str, dict[int, float]]:
    """Парсит Таблицу 2.11 (общий фосфор Робщ в мг Р/л -> переводим в мкг/л)."""
    start_idx = text.find("Таблица 2.11")
    if start_idx == -1:
        start_idx = text.find("Table 2.11")
    if start_idx == -1:
        raise ValueError(f"Не найдена Таблица 2.11 в бюллетене {year} г.")

    lines = text[start_idx:].splitlines()
    current_block: str | None = None
    raw_vals: dict[str, list[float]] = {}

    for line in lines:
        low = line.lower()
        if "малый плес" in low:
            current_block = "naroch_small"
        elif "большой плес" in low:
            current_block = "naroch_big"
        elif "озеро мястро" in low:
            current_block = "myastro"
        elif "озеро баторино" in low:
            current_block = "batorino"
        elif current_block is not None and "робщ" in low:
            # Переводим мг Р/л -> мкг Р/л (× 1000) по DATA_CONTRACT.md
            raw_vals[current_block] = [round(v * 1000.0, 2) for v in _parse_ru_floats(line, 6)]
            if current_block == "batorino":
                break

    for k in ("naroch_small", "naroch_big", "myastro", "batorino"):
        if k not in raw_vals:
            raise ValueError(f"Не найдена строка Робщ для {k} в Таблице 2.11 ({year} г.)")

    out: dict[str, dict[int, float]] = {"naroch": {}, "myastro": {}, "batorino": {}}
    for idx, month in enumerate(MONTHS_VEG):
        out["naroch"][month] = round(
            (raw_vals["naroch_small"][idx] + raw_vals["naroch_big"][idx]) / 2.0, 2
        )
        out["myastro"][month] = raw_vals["myastro"][idx]
        out["batorino"][month] = raw_vals["batorino"][idx]
    return out


def parse_naroch_appendix_2004_2018(pdf_2018: Path) -> pd.DataFrame:
    """Парсит Приложение бюллетеня 2018 г. (с. 94–108): горизонт 0,5 м оз. Нарочь за 2004–2018 гг."""
    text = _pdftotext_pages(pdf_2018, 94, 108)
    # Ищем строки, начинающиеся с точной даты ДД.ММ.ГГГГ и горизонта 0,5 (или 0.5)
    row_re = re.compile(
        r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s+"
        r"(0[,.]5)\s+"
        r"(\d+(?:[,.]\d+)?)\s+"
        r"(\d+(?:[,.]\d+)?)\s+"
        r"(\d+(?:[,.]\d+)?)"
        r"(?:\s+(\d+(?:[,.]\d+)?))?"
    )

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = row_re.match(line)
        if not m:
            continue
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (2004 <= yyyy <= 2018 and 1 <= mm <= 12 and 1 <= dd <= 31):
            continue
        temp_c = float(m.group(5).replace(",", "."))
        seston_mg_l = float(m.group(6).replace(",", "."))
        chl_plus_pheo = float(m.group(7).replace(",", "."))
        chl_pct = float(m.group(8).replace(",", ".")) if m.group(8) else np.nan

        rows.append(
            {
                "water_body_id": "naroch",
                "sample_date": f"{yyyy:04d}-{mm:02d}-{dd:02d}",
                "horizon_m": 0.5,
                "water_temp_surface_c": temp_c,
                "seston_mg_l": seston_mg_l,
                "chl_a_plus_pheo_ug_l": chl_plus_pheo,
                "chl_pct_in_seston": chl_pct,
                "station": "Малый плес (глубоководная зона)",
                "sample_type": "surface_0.5m_appendix_1.5um",
                "date_precision": "exact",
                "source_note": "Бюллетень БГУ 2018, Приложение (с. 94–108), фильтр 1,1–1,5 мкм, Chl-a + феопигменты",
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("Не удалось извлечь ни одной строки из Приложения 2004–2018!")
    df = df.drop_duplicates(subset=["water_body_id", "sample_date"]).sort_values(
        "sample_date"
    ).reset_index(drop=True)
    return df


def extract_bsu_monthly_tables(
    naroch_2018_exact_dates: dict[int, str],
) -> pd.DataFrame:
    """Извлекает реальные ежемесячные таблицы (2018 и 2019 гг.) для Нарочи, Мястро и Баторино."""
    specs = [
        (2018, PDF_2018, (25, 28), (30, 33), (35, 39), "Бюллетень БГУ 2018"),
        (2019, PDF_2019, (23, 26), (29, 32), (34, 38), "Бюллетень БГУ 2019"),
    ]

    records: list[dict[str, Any]] = []
    for year, pdf_path, secchi_pages, tp_pages, chl_pages, label in specs:
        secchi_txt = _pdftotext_pages(pdf_path, *secchi_pages)
        tp_txt = _pdftotext_pages(pdf_path, *tp_pages)
        chl_txt = _pdftotext_pages(pdf_path, *chl_pages)

        secchi_map = _parse_table_2_3_secchi(secchi_txt, year)
        tp_map = _parse_table_2_11_tp(tp_txt, year)
        chl_map = _parse_table_2_17_chl(chl_txt, year)

        for lake_id in ("naroch", "myastro", "batorino"):
            for month in MONTHS_VEG:
                # Для Нарочи 2018 г. берём точную календарную дату из Приложения 2018;
                # для остальных (где в таблицах указан месяц, а в предисловии «середина месяца») — 15-е число.
                if year == 2018 and lake_id == "naroch" and month in naroch_2018_exact_dates:
                    sample_date = naroch_2018_exact_dates[month]
                    precision = "exact_from_appendix"
                else:
                    sample_date = f"{year:04d}-{month:02d}-15"
                    precision = "mid_month"

                c_info = chl_map[lake_id][month]
                records.append(
                    {
                        "water_body_id": lake_id,
                        "sample_date": sample_date,
                        "chl_a_lab_ug_l": c_info["chl_04"],
                        "chl_a_filter_1_5_um_ug_l": c_info["chl_15"],
                        "secchi_depth_m": secchi_map[lake_id][month],
                        "total_phosphorus_ug_l": tp_map[lake_id][month],
                        "sample_type": "integral_monthly_0.4um",
                        "date_precision": precision,
                        "source_note": f"{label} (Табл. 2.17, 2.3, 2.11; {c_info['buoy_note']})",
                    }
                )

    df = pd.DataFrame(records)
    df = df.sort_values(["water_body_id", "sample_date"]).reset_index(drop=True)
    return df


def validate_and_save_lab_data(
    rebuild_from_pdfs: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Извлекает (при необходимости) данные из PDF БГУ, валидирует и сохраняет CSV + Parquet."""
    cfg = load_config()
    known_lakes = {wb.id for wb in cfg.water_bodies}

    LAB_DIR.mkdir(parents=True, exist_ok=True)
    INTERIM_DIR.mkdir(parents=True, exist_ok=True)

    app_df = parse_naroch_appendix_2004_2018(PDF_2018)
    app_df.to_csv(NAROCH_APP_CSV_PATH, index=False)

    # Извлекаем точные даты отбора проб по Нарочи за май–октябрь 2018 г.
    n2018 = app_df[app_df["sample_date"].str.startswith("2018-")]
    naroch_2018_exact_dates = {
        int(dt[5:7]): dt for dt in n2018["sample_date"].tolist()
    }

    if rebuild_from_pdfs or not LAB_CSV_PATH.exists():
        monthly_df = extract_bsu_monthly_tables(naroch_2018_exact_dates)
        monthly_df.to_csv(LAB_CSV_PATH, index=False)
    else:
        monthly_df = pd.read_csv(LAB_CSV_PATH)

    # Валидация основной лабораторной таблицы (по DATA_CONTRACT.md)
    required_cols = [
        "water_body_id",
        "sample_date",
        "chl_a_lab_ug_l",
        "secchi_depth_m",
        "total_phosphorus_ug_l",
        "source_note",
    ]
    missing_cols = [c for c in required_cols if c not in monthly_df.columns]
    if missing_cols:
        raise ValueError(f"В {LAB_CSV_PATH} отсутствуют обязательные колонки: {missing_cols}")

    unknown = set(monthly_df["water_body_id"]) - known_lakes
    if unknown:
        raise ValueError(f"Неизвестные коды озёр в лабораторных данных: {unknown}")

    monthly_df["sample_date"] = pd.to_datetime(monthly_df["sample_date"]).dt.strftime(
        "%Y-%m-%d"
    )
    if (monthly_df["chl_a_lab_ug_l"] <= 0).any() or (monthly_df["chl_a_lab_ug_l"] > 500).any():
        raise ValueError("Обнаружены физически невозможные значения chl_a_lab_ug_l!")

    if monthly_df.duplicated(subset=["water_body_id", "sample_date"]).any():
        raise ValueError("Обнаружены дубликаты (water_body_id, sample_date) в lab_measurements.csv!")

    monthly_df.to_parquet(LAB_CLEAN_PARQUET_PATH, index=False)

    print("=" * 96)
    print("ЛАБОРАТОРНЫЕ ДАННЫЕ БГУ — ИЗВЛЕЧЕНЫ И ПРОШЛИ ВАЛИДАЦИЮ")
    print("=" * 96)
    print(f"1) Основная таблица 2018–2019 (интегральные пробы, 0,4 мкм): {len(monthly_df)} записей -> {LAB_CSV_PATH}")
    for lid, grp in monthly_df.groupby("water_body_id"):
        print(
            f"   • {lid:<10} | проб: {len(grp):2d} | Chl-a: {grp['chl_a_lab_ug_l'].min():.2f}–{grp['chl_a_lab_ug_l'].max():.2f} мкг/л "
            f"| Секки: {grp['secchi_depth_m'].min():.2f}–{grp['secchi_depth_m'].max():.2f} м "
            f"| Pобщ: {grp['total_phosphorus_ug_l'].min():.0f}–{grp['total_phosphorus_ug_l'].max():.0f} мкг/л"
        )
    print(
        f"2) Многолетний ряд оз. Нарочь (горизонт 0,5 м, точные даты 2004–2018): "
        f"{len(app_df)} записей ({app_df['sample_date'].min()} .. {app_df['sample_date'].max()}) -> {NAROCH_APP_CSV_PATH}"
    )
    print(f"   • Точные даты отбора проб 2018 г.: {sorted(naroch_2018_exact_dates.values())}")
    print(f"3) Очищенный Parquet для ETL/ML: {LAB_CLEAN_PARQUET_PATH}")
    return monthly_df, app_df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Извлечение и проверка реальных лабораторных замеров из PDF-бюллетеней БГУ"
    )
    parser.add_argument(
        "--from-csv",
        action="store_true",
        help="Валидировать существующий data/lab/lab_measurements.csv без перезаписи из PDF",
    )
    args = parser.parse_args()
    validate_and_save_lab_data(rebuild_from_pdfs=not args.from_csv)


if __name__ == "__main__":
    main()
