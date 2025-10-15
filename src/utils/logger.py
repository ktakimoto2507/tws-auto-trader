import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

def get_logger(name: str = "autotrader") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    fh = RotatingFileHandler(logs_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
