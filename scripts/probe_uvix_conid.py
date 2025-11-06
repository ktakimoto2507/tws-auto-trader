# probe_uvix_conid.py
from ib_insync import IB, Contract, Stock, Option
import os
import math

CONID = int(os.getenv("UVIX_CONID", "752090595"))

def ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x)) and x != 0

ib = IB()
print("Connecting...")
ib.connect('127.0.0.1', 7497, clientId=99)

# [1] 現物詳細
cds = ib.reqContractDetails(Contract(conId=CONID))
assert cds, "No ContractDetails"
cd = cds[0]
c = cd.contract
print(f"[1] {c.symbol} {c.secType} {c.currency} exch={c.exchange} primary={getattr(c,'primaryExchange','')}")

# [2] オプション鎖
params = ib.reqSecDefOptParams("UVIX", "", "STK", c.conId)
assert params, "No option params"
p = params[0]
print(f"[2] chain ok: class={p.tradingClass} strikes={len(p.strikes)} expirations={len(p.expirations)}")

# [3] 価格取得（多段フォールバック）
def snapshot_price(contract: Stock) -> float | None:
    t = ib.reqMktData(contract, "", True, False)
    for _ in range(40):
        v = t.last or (t.bid and t.ask and (t.bid + t.ask)/2) or t.close or t.marketPrice()
        if ok(v):
            return float(v)
        ib.sleep(0.05)
    return None

price = None
try:
    ib.reqMarketDataType(1)  # live
except Exception:
    pass
price = snapshot_price(Stock(conId=c.conId, exchange="SMART", currency="USD"))

if not price:
    ib.reqMarketDataType(3)  # delayed
    price = snapshot_price(Stock(conId=c.conId, exchange="SMART", currency="USD"))

if not price:
    # 取引所を変える
    price = snapshot_price(Stock(symbol="UVIX", exchange="ARCA", currency="USD"))
    if not price:
        price = snapshot_price(Stock(symbol="UVIX", exchange="NYSEARCA", currency="USD"))

if not price:
    # 最後の砦：履歴データ
    try:
        bars = ib.reqHistoricalData(
            Stock(conId=c.conId, exchange="SMART", currency="USD"),
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if bars:
            price = float(bars[-1].close)
            print("[3] fallback: historical close used")
    except Exception as e:
        print("[3] historical fallback failed:", e)

print(f"[3] price={price}")
assert price and price > 0, "No price fields"

# [4] qualify も確認
expiry = sorted(p.expirations)[0].replace("-", "")
strike = sorted([float(s) for s in p.strikes if s is not None])[0]
q = ib.qualifyContracts(Option(
    symbol="UVIX",
    lastTradeDateOrContractMonth=expiry,
    strike=float(strike),
    right="P",
    exchange="SMART",
    currency="USD",
    tradingClass=p.tradingClass or "UVIX",
    multiplier="100",
))
assert q, "Option qualify failed"
print("[4] option qualified ok:", q[0])

print("\n✔ All UVIX checks passed.")
ib.disconnect()
