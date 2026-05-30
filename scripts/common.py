from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = ROOT_DIR / "data_transfermarkt"
RESULTS_DIR = ROOT_DIR / "results"

POSITION_MAP = {
    "goleiro": "Goleiro",
    "goalkeeper": "Goleiro",
    "defesa": "Defesa",
    "defender": "Defesa",
    "defensor": "Defesa",
    "meio-campo": "Meio-campo",
    "meio campo": "Meio-campo",
    "midfield": "Meio-campo",
    "ataque": "Ataque",
    "forward": "Ataque",
    "striker": "Ataque",
}


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_date(series: pd.Series, utc: bool = False) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=utc)
    if utc:
        return parsed.dt.tz_convert(None)
    return parsed


def normalize_position_group(value: Any) -> Any:
    if pd.isna(value):
        return np.nan
    raw = str(value).strip()
    if not raw:
        return np.nan
    lowered = raw.lower()
    return POSITION_MAP.get(lowered, raw)


def season_end_year_from_label(value: Any) -> float:
    if pd.isna(value):
        return np.nan
    text = str(value).strip()
    if not text:
        return np.nan
    match = re.search(r"(\d{2})/(\d{2})", text)
    if not match:
        match = re.search(r"(\d{4})", text)
        if match:
            return float(match.group(1))
        return np.nan
    end_two_digits = int(match.group(2))
    if end_two_digits <= 40:
        return float(2000 + end_two_digits)
    return float(1900 + end_two_digits)


def season_anchor_year(row: pd.Series) -> float:
    text = str(row.get("summary_row_text") or "")
    match = re.search(r"Total\s+(\d{4})", text)
    if match:
        return float(match.group(1))
    return season_end_year_from_label(row.get("season_label"))


def json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        if math.isnan(number):
            return None
        return number
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=json_default)


def load_raw_datasets(base_dir: str | Path = RAW_DATA_DIR) -> dict[str, pd.DataFrame]:
    base = Path(base_dir)
    datasets = {}
    for name in [
        "profiles",
        "market_values",
        "injuries",
        "transfers",
        "performance_summaries",
        "squad_membership",
        "career_arcs_base",
    ]:
        datasets[name] = pd.read_csv(base / f"{name}.csv")
    return datasets


def missing_summary(df: pd.DataFrame, dataset_name: str) -> pd.DataFrame:
    rows = []
    for column in df.columns:
        rows.append(
            {
                "dataset": dataset_name,
                "column": column,
                "missing_count": int(df[column].isna().sum()),
                "missing_pct": float(df[column].isna().mean()),
            }
        )
    return pd.DataFrame(rows)


def safe_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def round_frame(df: pd.DataFrame, digits: int = 4) -> pd.DataFrame:
    numeric_cols = df.select_dtypes(include=["number"]).columns
    rounded = df.copy()
    rounded[numeric_cols] = rounded[numeric_cols].round(digits)
    return rounded
