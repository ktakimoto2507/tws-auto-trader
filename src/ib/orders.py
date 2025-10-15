from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from ib_insync import IB, Stock, Order
from ..utils.logger import get_logger

log = get_logger("orders")


@dataclass(frozen=True)
class StockSpec:
    symbol: str
    exchange: str = "SMART"
    currency: str = "USD"


def stock_contract(spec: StockSpec) -> Stock:
    return Stock(spec.symbol, spec.exchange, spec.currency)


def new_oca_group(prefix: str = "OCA") -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _maybe_transmit(order: Order, dry_run: bool) -> None:
    # DRY_RUN のときは送信しない
    order.transmit = not dry_run


# --- 株の基本オーダー ---------------------------------------------------------
def market(
    ib: IB,
    spec: StockSpec,
    action: str,
    qty: float,
    *,
    dry_run: bool = True,
    oca_group: Optional[str] = None,
) -> Order:
    """
    action: 'BUY' or 'SELL'
    """
    c = stock_contract(spec)
    o = Order(orderType="MKT", action=action, totalQuantity=qty)
    if oca_group:
        o.ocaGroup = oca_group
    _maybe_transmit(o, dry_run)

    if dry_run:
        log.info(f"[DRY RUN] STOCK MKT {action} {qty} {spec.symbol}")
        return o

    trade = ib.placeOrder(c, o)
    return trade.order


def limit_(
    ib: IB,
    spec: StockSpec,
    action: str,
    qty: float,
    lmt_price: float,
    *,
    dry_run: bool = True,
    oca_group: Optional[str] = None,
) -> Order:
    c = stock_contract(spec)
    o = Order(orderType="LMT", action=action, totalQuantity=qty, lmtPrice=float(lmt_price))
    if oca_group:
        o.ocaGroup = oca_group
    _maybe_transmit(o, dry_run)

    if dry_run:
        log.info(f"[DRY RUN] STOCK LMT {action} {qty} {spec.symbol} @ {lmt_price}")
        return o

    trade = ib.placeOrder(c, o)
    return trade.order


def stop_pct(
    ib: IB,
    spec: StockSpec,
    qty: float,
    *,
    reference_price: float,
    pct: float,
    side_for_stop: Optional[str] = None,
    dry_run: bool = True,
    oca_group: Optional[str] = None,
) -> Order:
    """
    ロングの損切り6%なら reference*(1-0.06) で SELL の STP。
    side_for_stop を省略した場合はロング前提で SELL。
    """
    action = side_for_stop or "SELL"  # ロング前提
    # SELL ストップは下方向、BUY ストップは上方向
    stop_price = (
        round(reference_price * (1 - pct), 2) if action == "SELL" else round(reference_price * (1 + pct), 2)
    )

    c = stock_contract(spec)
    o = Order(orderType="STP", action=action, totalQuantity=qty, auxPrice=float(stop_price))
    if oca_group:
        o.ocaGroup = oca_group
    _maybe_transmit(o, dry_run)

    if dry_run:
        log.info(
            f"[DRY RUN] STOCK STP {action} {qty} {spec.symbol} @ {stop_price} (ref={reference_price}, pct={pct})"
        )
        return o

    trade = ib.placeOrder(c, o)
    return trade.order