# run_price_probe_once.py ーー 安定版
from ib_insync import IB, Stock
from datetime import datetime, date as Date
import math

def valid_num(v):
    if v is None:
        return False
    try:
        f = float(v)
        return not math.isnan(f) and f != -1.0
    except Exception:
        return False

def parse_bar_time(v):
    # ib_insync の bar.date は環境で str / datetime / date のいずれかになり得る
    if isinstance(v, datetime):
        return v
    if isinstance(v, Date):
        return datetime.combine(v, datetime.min.time())
    if isinstance(v, str):
        # 例: "20251023  23:59:59" or "20251023"
        v = v.strip()
        for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                pass
    return None

def get_top(ib: IB, c: Stock, md_type=3, wait_sec=2.5):
    ib.reqMarketDataType(md_type)  # 1=RT,2=Frozen,3=Delayed,4=Delayed-Frozen
    t = ib.reqMktData(c, "", False, False)
    ib.sleep(wait_sec)
    mp = t.marketPrice()
    if valid_num(mp) or valid_num(t.last) or valid_num(t.bid) or valid_num(t.ask):
        return {
            "source": f"TOP(md_type={md_type},{c.exchange}/{c.primaryExchange})",
            "marketPrice": mp, "last": t.last, "bid": t.bid, "ask": t.ask
        }
    return None

def get_hist_close(ib: IB, c: Stock):
    # 直近1日の日足終値（RTHのみ）。遅延TOPがダメでも大抵これは返る
    bars = ib.reqHistoricalData(
        c, endDateTime="", durationStr="1 D", barSizeSetting="1 day",
        whatToShow="TRADES", useRTH=True, formatDate=1
    )
    if bars:
        b = bars[-1]
        return {
            "source": "HIST(1D close, RTH)",
            "marketPrice": b.close, "last": b.close, "bid": None, "ask": None,
            "barTime": parse_bar_time(b.date)
        }
    return None

if __name__ == "__main__":
    ib = IB()
    ib.connect("127.0.0.1", 7497, clientId=99)

    # SMART→ARCA、md_type は遅延優先（3→4）、次に保険で 1→2
    tried = []
    for ex, pe in [("SMART", "ARCA"), ("ARCA", "ARCA")]:
        c = Stock("NUGT", ex, "USD", primaryExchange=pe)
        ib.qualifyContracts(c)

        for md in [3, 4, 1, 2]:
            res = get_top(ib, c, md_type=md)
            tried.append((ex, pe, md, bool(res)))
            if res:
                print("[OK]", res["source"], res)
                ib.disconnect()
                raise SystemExit(0)

        # TOPが全部ダメなら履歴データ（終値）でフォールバック
        hist = get_hist_close(ib, c)
        if hist:
            print("[OK-FALLBACK]", hist["source"], hist)
            ib.disconnect()
            raise SystemExit(0)

    print("[NG] 価格が取れません。設定/約款/共有を再確認してください。 Tried:", tried)
    ib.disconnect()
