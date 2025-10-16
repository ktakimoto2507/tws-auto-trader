from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class IBConfig:
    host: str = os.getenv("IB_HOST", "127.0.0.1")
    port: int = int(os.getenv("IB_PORT", "7497"))  # Paper=7497 / Live=7496
    client_id: int = int(os.getenv("IB_CLIENT_ID", "10"))
    account: str | None = os.getenv("IB_ACCOUNT")  # ★追加

@dataclass(frozen=True)
class OrderPolicy:
    """
    発注の既定ポリシー。環境変数で上書き可能。
      - ORDER_TIF: "DAY" | "GTC"
      - OUTSIDE_RTH: "true"/"false"
      - SLIPPAGE_BPS: 指値の上乗せ(bps) 例:15 → +0.15%
      - STOP_PCT: 損切り％（0.06 なら -6%）
      - TAKE_PROFIT_PCT: 利確％（未設定なら None）
      - DRY_RUN: 既定でドライランにするか（UIのLiveトグルが最終決定）
    """
    tif: str = os.getenv("ORDER_TIF", "DAY")
    outside_rth: bool = os.getenv("OUTSIDE_RTH", "false").lower() == "true"
    slippage_bps: int = int(os.getenv("SLIPPAGE_BPS", "15"))
    stop_pct: float = float(os.getenv("STOP_PCT", "0.06"))
    take_profit_pct: float | None = (
        float(os.getenv("TAKE_PROFIT_PCT")) if os.getenv("TAKE_PROFIT_PCT") else None
    )
    dry_run_default: bool = os.getenv("DRY_RUN", "true").lower() == "true"
