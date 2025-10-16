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
        # DEPRECATED: 直接の成行発注は使用しない方針。ログ表現も中立にする。
        log.info(f"[DRY RUN] STOCK {o.orderType} {action} {qty} {spec.symbol}")
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

# --- 親子（ブランケット）注文：BUY後にSTOPを自動有効化 -----------------
def bracket_buy_with_stop(
    ib: IB,
    spec: StockSpec,
    *,
    qty: float,
    entry_type: str = "LMT",        # "MKT"=成行 / "LMT"=指値
    lmt_price: float | None = None, # entry_type="LMT" の時だけ必須
    stop_price: float,              # 例: 参照価格×(1-0.06)
    tif: str = "DAY",               # "DAY" or "GTC"
    outside_rth: bool = False,      # 立会時間外も約定させるなら True
    dry_run: bool = True,
):
    """
    親: BUY（MKT/LMT） → 親がFillしたら 子: SELL STOP を自動で有効化する。
    戻り値: (親Order, 子Order, parent_trade または None)
    """
    c = stock_contract(spec)

    # 親（BUY）
    parent = Order(action="BUY", orderType=entry_type, totalQuantity=qty, tif=tif)
    if entry_type == "LMT":
        assert lmt_price is not None, "entry_type='LMT' では lmt_price が必須です。"
        parent.lmtPrice = float(lmt_price)
    parent.outsideRth = bool(outside_rth)

    # 子（STOP SELL）…親のFillが付くまで眠らせる
    child = Order(action="SELL", orderType="STP", totalQuantity=qty, tif=tif)
    child.auxPrice = float(stop_price)
    child.outsideRth = bool(outside_rth)

    if dry_run:
        # 実送信しない（形だけ返す）
        return parent, child, None

    # 親を送信 → 返ってきた orderId を子の parentId に設定して送信
    parent_trade = ib.placeOrder(c, parent)
    child.parentId = parent_trade.order.orderId
    ib.placeOrder(c, child)

    return parent, child, parent_trade
# ---------------------------------------------------------------------

# --- 追加：指値/逆指値の自動算出（基準価格ベース） -----------------------
def decide_lmt_stop_take(
    reference_price: float,
    *,
    slippage_bps: int = 15,
    stop_pct: float = 0.06,
    take_profit_pct: float | None = None,
) -> tuple[float, float, float | None]:
    """
    戻り値: (lmt_price, stop_price, take_profit_price|None)
      - lmt = ref * (1 + bps/10000), 小数2桁丸め
      - stop = ref * (1 - stop_pct), 小数2桁丸め（ロング前提）
      - take = ref * (1 + take_profit_pct) or None
    """
    lmt = round(reference_price * (1 + slippage_bps / 10000), 2)
    stp = round(reference_price * (1 - stop_pct), 2)
    tpf = None if take_profit_pct is None else round(reference_price * (1 + take_profit_pct), 2)
    return lmt, stp, tpf