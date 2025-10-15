from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional, Tuple, List

from zoneinfo import ZoneInfo
from ib_insync import IB, Option, Contract, Stock
from ..utils.logger import get_logger

log = get_logger("options")

Right = Literal["C", "P"]


@dataclass(frozen=True)
class Underlying:
    symbol: str
    exchange: str = "SMART"
    currency: str = "USD"


def _underlying_price(ib: IB, und: Underlying) -> float:
    """
    スナップショットで現在値の近似を取得
    """
    contract = Stock(und.symbol, und.exchange, und.currency)
    ticker = ib.reqMktData(contract, "", True, False)  # snapshot=True
    ib.sleep(1.0)
    price = ticker.marketPrice()
    ib.cancelMktData(contract)
    if price is None or price <= 0:
        raise RuntimeError(f"Cannot get market price for {und.symbol}")
    return float(price)


def _nearest_expiry_friday(expirations: List[str], tz: ZoneInfo) -> str:
    """
    expirations: ['2025-10-17', ...]
    一番近い将来の金曜日を優先、無ければ最短の将来日付
    """
    today = datetime.now(tz).date()
    future = sorted(d for d in expirations if datetime.fromisoformat(d).date() >= today)
    if not future:
        raise RuntimeError("No future expirations available")
    for d in future:
        if datetime.fromisoformat(d).weekday() == 4:  # Fri
            return d
    return future[0]


def _closest_strike(strikes: List[float], target: float) -> float:
    return min(strikes, key=lambda s: abs(s - target))


def pick_option_contract(
    ib: IB,
    und: Underlying,
    right: Right,
    pct_offset: float = 0.0,
    prefer_friday: bool = True,
    tz_name: str = "America/New_York",
    opt_exchange: str = "SMART",
) -> Tuple[Option, float, str]:
    """
    ATM(+/-pct) のストライクを選んで Option 契約を返す。
    - right: 'C' or 'P'
    - pct_offset: 0.15 なら +15%（CallはOTM方向、PutはITM方向にずらす）
    戻り値: (Option契約, 使用ストライク, 使用満期[YYYY-MM-DD])
    """
    tz = ZoneInfo(tz_name)
    und_px = _underlying_price(ib, und)

    # Underlying の conId を取得
    und_contract = Stock(und.symbol, und.exchange, und.currency)
    cds = ib.reqContractDetails(und_contract)
    if not cds:
        raise RuntimeError(f"Underlying not found: {und.symbol}")
    conId = cds[0].contract.conId

    # オプション仕様（ストライク/満期一覧）
    params = ib.reqSecDefOptParams(und.symbol, "", und_contract.secType, conId)
    if not params:
        raise RuntimeError("No option params returned")
    p = params[0]

    expirations = sorted(list(p.expirations))
    strikes = sorted([float(s) for s in p.strikes if s is not None])

    # 満期選択
    if prefer_friday:
        expiry = _nearest_expiry_friday(expirations, tz)
    else:
        today = datetime.now(tz).date()
        future = [d for d in expirations if datetime.fromisoformat(d).date() >= today]
        if not future:
            raise RuntimeError("No future expirations available")
        expiry = future[0]

    # 目標価格（ATM ± pct）
    target = und_px * (1 + pct_offset) if right == "C" else und_px * (1 - pct_offset)
    strike = _closest_strike(strikes, target)

    # IBのOptionは 'YYYYMMDD' 形式
    expiry_yyyymmdd = expiry.replace("-", "")
    opt = Option(und.symbol, expiry_yyyymmdd, float(strike), right, opt_exchange, und.currency)
    return opt, float(strike), expiry


def sell_option(
    ib: IB,
    opt: Contract,
    qty: float,
    *,
    dry_run: bool = True,
    oca_group: Optional[str] = None,
):
    from ib_insync import Order

    o = Order(orderType="MKT", action="SELL", totalQuantity=qty)
    if oca_group:
        o.ocaGroup = oca_group
    o.transmit = not dry_run

    if dry_run:
        log.info(
            f"[DRY RUN] OPT SELL {qty} {getattr(opt, 'localSymbol', opt.symbol)} "
            f"{getattr(opt, 'lastTradeDateOrContractMonth', '')} {getattr(opt, 'right', '')}{getattr(opt, 'strike', '')}"
        )
        return o

    # オプション契約は qualify してから発注が安全
    [qopt] = ib.qualifyContracts(opt)
    trade = ib.placeOrder(qopt, o)
    return trade.order
