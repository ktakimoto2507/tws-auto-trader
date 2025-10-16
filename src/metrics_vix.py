# src/metrics_vix.py
from __future__ import annotations
from pathlib import Path
from datetime import date, datetime

DATA_DIR = Path("data")
CSV_PATH = DATA_DIR / "vix_monthly.csv"

def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def save_vix_snapshot(d: date | datetime, close: float) -> None:
    """Append a row (date, close) to data/vix_monthly.csv."""
    _ensure_dir()
    if isinstance(d, datetime):
        d = d.date()
    line = f"{d.isoformat()},{close:.4f}\n"
    if not CSV_PATH.exists():
        CSV_PATH.write_text("date,close\n", encoding="utf-8")
    with CSV_PATH.open("a", encoding="utf-8") as f:
        f.write(line)
