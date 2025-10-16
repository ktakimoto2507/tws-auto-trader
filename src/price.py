from ib_insync import IB, Ticker, util
from typing import Optional
from .symbols import CATALOG
from .utils.loop import ensure_event_loop

def ensure_contracts(ib: IB):
    """各シンボルのContractを資格付け（qualify）"""
    for c in CATALOG.values():
        if not getattr(c, "conId", None):
            ib.qualifyContracts(c)

def safe_price(t: Optional[Ticker]) -> Optional[float]:
    if not t:
        return None
    # 1) marketPrice (mid or last 相当) → 2) mid手組 → 3) close
    p = t.marketPrice()
    if p and p == p:  # not NaN
        return float(p)
    mids = [x for x in [t.midpoint(), t.last, t.close] if x is not None]
    return float(mids[0]) if mids else None

def get_prices(ib: IB, symbols: list[str], delay_type: int = 3) -> dict[str, Optional[float]]:
    ensure_event_loop()  # ★ 追加
    ib.reqMarketDataType(delay_type)  # 1=RT,2=Frozen,3=Delayed,4=DelayedFrozen
    ensure_contracts(ib)
    for s in symbols:
        ib.reqMktData(CATALOG[s], "", False, False)
    util.run(ib.sleep(2.0))  # Streamlitなど同期環境でも確実に待つ
    out = {}
    for s in symbols:
        t = ib.ticker(CATALOG[s])
        out[s] = safe_price(t)
        ib.cancelMktData(CATALOG[s])  # 購読解除（推奨）
    return out
