# --- imports (E402対策: すべてのimportを最上部に集約) ---
import sys
import asyncio
import math
import time

from ib_insync import IB, util, Stock, Contract, Ticker
from .config import IBConfig
from .utils.logger import get_logger
# ------------------------------------------------------

# --- Windows/Streamlit 用イベントループ対策（importの後に配置） ---
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ------------------------------------------------------

log = get_logger("ib")
# --- [P0-1] 契約ユーティリティ（ETF用：SMART+ARCA を統一） ---
def make_etf(symbol: str) -> Contract:
    """ETF/銘柄の契約を統一定義（SMART ルーティング + primaryExchange=ARCA）"""
    return Stock(symbol, "SMART", "USD", primaryExchange="ARCA")

def qualify_or_raise(ib: IB, c: Contract) -> Contract:
    """qualifyContracts の安全ラッパ。失敗なら例外。"""
    res = ib.qualifyContracts(c)
    if not res or not getattr(res[0], "conId", None):
        raise RuntimeError(f"Failed to qualify contract: {c}")
    return res[0]
# ----------------------------------------------------------------

# --- [P0-2/3] 価格フォールバック決定 & ティック待ち ---

def resolve_price(t: Ticker) -> float | None:
    """last -> close -> marketPrice -> mid((bid+ask)/2) の順で決定"""
    for p in (t.last, t.close, t.marketPrice()):
        if isinstance(p, (int, float)) and math.isfinite(p) and p > 0:
            return float(p)
    if isinstance(t.bid, (int, float)) and isinstance(t.ask, (int, float)):
        if math.isfinite(t.bid) and math.isfinite(t.ask) and t.bid > 0 and t.ask > 0:
            return (t.bid + t.ask) / 2
    return None

def wait_price(ib: IB, c: Contract, timeout: float = 12.0, poll: float = 0.25) -> tuple[float | None, Ticker]:
    """
    ストリーミングMDを要求して、timeout まで価格決定を待つ。
    connect() で MDType は既に 3（遅延）に設定済みの前提。
    """
    ib.reqMktData(c, "", False, False)  # streaming
    t0 = time.time()
    t: Ticker | None = None
    while time.time() - t0 < timeout:
        ib.waitOnUpdate(timeout=1)
        t = ib.ticker(c)
        px = resolve_price(t)
        if px:
            return px, t
        time.sleep(poll)
    return None, t if t else ib.ticker(c)
# ----------------------------------------------------------------


class IBClient:
    def __init__(self, cfg: IBConfig | None = None):
        self.cfg = cfg or IBConfig()
        self.ib = IB()

    def connect(self, timeout: float = 20.0, market_data_type: int = 3, max_try_ids: int = 10):
        """
        clientId が重複したら +1 して最大 max_try_ids 回まで自動リトライ。
        """
        base_id = int(self.cfg.client_id)
        last_err: Exception | None = None
        for offset in range(max_try_ids):
            cid = base_id + offset
            log.info(f"Connecting IB: host={self.cfg.host} port={self.cfg.port} clientId={cid}")
            try:
                util.run(self.ib.connectAsync(
                    self.cfg.host, self.cfg.port,
                    clientId=cid, timeout=timeout, readonly=False
                ))
                if self.ib.isConnected():
                    # 1=リアル, 2=フローズン, 3=遅延, 4=遅延フローズン
                    self.ib.reqMarketDataType(market_data_type)
                    log.info(f"Connected (clientId={cid}, MDType={market_data_type})")
                    # 実際に使った clientId を保持
                    self.cfg.client_id = cid
                    return
            except Exception as e:
                last_err = e
                # 326（Client ID already in use）などは次のIDで続行
                try:
                    self.ib.disconnect()
                except Exception:
                    pass
        raise RuntimeError(f"Failed to connect IB after trying {max_try_ids} clientIds") from last_err

    def disconnect(self):
        try:
            self.ib.disconnect()
            log.info("Disconnected")
        except Exception as e:
            log.warning(f"Disconnect error: {e}")

    def fetch_account_summary(self):
        acct = getattr(self.cfg, "account", None)
        return self.ib.accountSummary(acct) if acct else self.ib.accountSummary()

    def fetch_positions(self):
        return self.ib.positions()

    def fetch_open_orders(self):
        return self.ib.openOrders()
    
        # --- [任意] シンボルを渡して価格だけ取るワンライナー ---
    def fetch_price_for(self, symbol: str, timeout: float = 12.0) -> tuple[float | None, Ticker, Contract]:
        c = make_etf(symbol)
        qc = qualify_or_raise(self.ib, c)
        px, t = wait_price(self.ib, qc, timeout=timeout)
        return px, t, qc

