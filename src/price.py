from ib_insync import IB, Ticker, util
from typing import Optional, Dict, Any
from .symbols import CATALOG
from .utils.loop import ensure_event_loop
from datetime import datetime, date as Date
import math

# 直近の取得メタ情報（どのソースで値を採用したか）を保持
_LAST_META: Dict[str, str] = {}


def ensure_contracts(ib: IB):
    """各シンボルのContractを資格付け（qualify）"""
    for c in CATALOG.values():
        if not getattr(c, "conId", None):
            ib.qualifyContracts(c)


def _ok_number(v: Any) -> bool:
    """数値で、NaN/-1.0 ではないかを判定"""
    if v is None:
        return False
    try:
        f = float(v)
        return (not math.isnan(f)) and (f != -1.0)
    except Exception:
        return False


def safe_price(t: Optional[Ticker]) -> Optional[float]:
    """TOPティッカーから安全に代表価格を抽出（marketPrice→mid→last→close）"""
    if not t:
        return None
    p = t.marketPrice()
    if _ok_number(p):
        return float(p)
    # midpoint は無いことが多いが一応試す。last/close も -1 を除外
    for x in (t.midpoint(), t.last, t.close):
        if _ok_number(x):
            return float(x)  # type: ignore[arg-type]
    return None


def _parse_bar_time(v) -> Optional[datetime]:
    """ib_insync の BarData.date は str/datetime/date のいずれかになりうるので吸収"""
    if isinstance(v, datetime):
        return v
    if isinstance(v, Date):
        return datetime.combine(v, datetime.min.time())
    if isinstance(v, str):
        vv = v.strip()
        for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                return datetime.strptime(vv, fmt)
            except ValueError:
                pass
    return None


def _hist_close(ib: IB, sym: str) -> Optional[float]:
    """直近1営業日の終値(TRADES, RTH)を返す。遅延TOPがダメでも通ることが多い。"""
    c = CATALOG[sym]
    bars = ib.reqHistoricalData(
        c,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    if bars:
        b = bars[-1]
        try:
            close = float(b.close)
            if (not math.isnan(close)) and close != -1.0:
                return close
        except Exception:
            pass
    return None


def last_price_meta() -> Dict[str, str]:
    """直近の get_prices 実行で採用したソース（TOP/HIST）を返す。UI表示用。"""
    return dict(_LAST_META)


def get_prices(ib: IB, symbols: list[str], delay_type: int = 3) -> dict[str, Optional[float]]:
    """
    価格取得（既存シグネチャ維持）:
      1) md_type（UI指定）→ 4 → 1 → 2 の順で TOP を試す
      2) 取れなければ HIST(1D close, RTH) にフォールバック
    返り値: {symbol: price or None}
    """
    ensure_event_loop()
    ensure_contracts(ib)

    # md_type の試行順（UI選択を先頭に、残りを 4/1/2 の順で補完）
    md_try = [delay_type] + [x for x in (4, 1, 2) if x != delay_type]

    out: Dict[str, Optional[float]] = {}
    _LAST_META.clear()

    for s in symbols:
        price: Optional[float] = None
        source = ""

        # 1) TOP（ストリーミング）: md_try の順に試す
        for md in md_try:
            try:
                ib.reqMarketDataType(md)  # 1=RT, 2=Frozen, 3=Delayed, 4=DelayedFrozen
                ib.reqMktData(CATALOG[s], "", False, False)
                util.run(ib.sleep(2.5))
                t = ib.ticker(CATALOG[s])
                price = safe_price(t)
            finally:
                # 重複購読を避けるため都度キャンセル（例外でも実行）
                try:
                    ib.cancelMktData(CATALOG[s])
                except Exception:
                    pass

            if price is not None:
                source = f"TOP(md_type={md})"
                break

        # 2) TOP がダメなら HIST（1D終値）で救済
        if price is None:
            price = _hist_close(ib, s)
            if price is not None:
                source = "HIST(1D close, RTH)"

        out[s] = price
        if source:
            _LAST_META[s] = source

    return out
