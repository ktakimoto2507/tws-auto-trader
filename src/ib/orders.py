from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
from uuid import uuid4

from ib_insync import IB, Stock, Order
from ..utils.logger import get_logger
from .options import pick_option_contract, sell_option, buy_option, Underlying

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

# ============================================================
# ★ 追加：Covered Call（株+STOP+Call売り）本体（DRY可視性重視）
# ============================================================

def run_covered_call(
    ib: IB,
    *,
    symbol: str,
    budget_usd: float,
    stop_pct_value: float,
    manual_price: float | None = None,   # UIの「Use manual price」をそのまま渡す
    entry_slippage_bps: int = 15,        # 指値は ref*(1+bps/10000)
    dry_run: bool = True,
    oca_group: str | None = None,
):
    """
    株BUY(LMT) → STOP(SELL) → C-（ATM）を必ず評価し、DRYでも行を出す。
    - manual_price があればそれを基準として数量・C-のATM決定に使用
    - 市場価格の取得は呼び出し側で済んでいる前提でも、manualがあればそれを最優先
    """
    # 1) 参照価格を決定
    ref = manual_price
    if ref is None:
        log.warning(f"[PLAN] {symbol} manual_price 未指定。UI側のスナップショット価格を渡すとより安定します。")
        # manual無しでも動くように、最低限ログだけ残して終了（価格取得は他レイヤで実施想定）
        # 価格が無いと数量算出できないため、ここで止める
        return

    # 2) 株数量（最低100株未満ならC-不可を明示）
    if ref <= 0:
        log.warning(f"[PLAN] {symbol} 参照価格が不正: {ref}")
        return
    qty_shares = int(budget_usd // ref)
    if qty_shares <= 0:
        log.info(f"[PLAN] {symbol} 予算不足（budget={budget_usd}, ref={ref:.2f}）")
        return

    # 3) 指値・ストップを決定し、BUY(LMT) → STP(SELL) を出す（DRYはログのみ）
    lmt_price, stop_price, _ = decide_lmt_stop_take(
        reference_price=ref,
        slippage_bps=entry_slippage_bps,
        stop_pct=stop_pct_value
    )
    limit_(ib, StockSpec(symbol), action="BUY", qty=qty_shares, lmt_price=lmt_price, dry_run=dry_run, oca_group=oca_group)
    stop_pct(ib, StockSpec(symbol), qty=qty_shares, reference_price=ref, pct=stop_pct_value, dry_run=dry_run, oca_group=oca_group)

    # 4) C- 数量（100株ごとに1枚）
    qty_calls = qty_shares // 100
    if qty_calls <= 0:
        log.info(f"[PLAN] {symbol} C- スキップ（shares={qty_shares} < 100）")
        return

    # 5) ATMコールの選定（manual_priceを ref として強制使用）
    try:
        und = Underlying(symbol=symbol, exchange="SMART", currency="USD")
        opt, strike, expiry = pick_option_contract(
            ib,
            und=und,
            right="C",
            pct_offset=0.0,              # ATM
            override_price=float(ref),   # ← manual_price を必ず反映
        )
    except Exception as e:
        log.warning(f"[PLAN] {symbol} C- 不可（仕様取得失敗: {e}）。ATM@{ref:.2f} 想定。")
        return

    # 6) C- を SELL（DRYでも必ず1行出る）
    sell_option(ib, opt=opt, qty=qty_calls, dry_run=dry_run, oca_group=oca_group)

# ============================================================
# ★ 追加：純粋なP+（ATM Put BUYのみ）… UVIX向け
#   ・株は一切触らない
#   ・manual_price があればそれをATM判定に使用
# ============================================================
def run_put_long(
    ib: IB | None = None,
    *,
    symbol: str = "UVIX",
    contracts: int = 1,             # 何枚買うか（例: 10）
    manual_price: float = 0.0,
    pct_offset: float = 0.0,        # ATM=0.0, 少しOTMなら +/− を調整
    dry_run: bool = True,
    oca_group: str | None = None,
) -> List[str]:
    """
    純粋な PUT BUY（P+）。株は触らない。
    - DRY（または ib=None）のときは IB API に触らず、計算とログだけ返す。
    - LIVE のときのみ、オプションチェーン解決→最適PUT選定→発注を行う。
    戻り値: ログ行のリスト
    """
    msgs: List[str] = []
    # ★ 入口で必ずログ（ここが出ない＝そもそも関数に到達していない）
    try:
        is_conn = bool(getattr(ib, "isConnected", lambda: False)())
    except Exception:
        is_conn = False
    log.info(
        "[ENTER] run_put_long symbol=%s contracts=%s dry_run=%s manual=%s offset=%s ib_connected=%s",
        symbol, contracts, dry_run, manual_price, pct_offset, is_conn
    )

    # 0) 入力バリデーション（共通）
    qty = int(contracts)
    if qty <= 0:
        msgs.append(f"[PLAN] {symbol} P+ スキップ（contracts={contracts}）")
        return msgs

    # 1) DRY（または ib=None）の場合：計算のみで完結（イベントループ/IB不要）
    if dry_run or ib is None:
        px = float(manual_price)
        if not (px > 0):
            raise RuntimeError("DRYモードでは manual_price が必須です（ATM判定用）")
        # 目標ストライク（刻みは銘柄により異なるためここでは近似。実売買時に正規化される想定）
        target = round(px * (1.0 + float(pct_offset)), 2)
        line1 = f"[PLAN] {symbol} P+ DRY: ATM≈{px:.2f}, offset={pct_offset:+.2f} → target strike≈{target:.2f}"
        line2 = f"[PLAN] contracts={qty}, oca_group={oca_group or '(none)'}"
        log.info(line1)
        log.info(line2)
        msgs.append(line1)
        msgs.append(line2)
        return msgs

    # 2) LIVE（ib 必須）：チェーン取得→最適PUT選定→発注
    try:
        und = Underlying(symbol=symbol, exchange="SMART", currency="USD")
        opt, strike, expiry = pick_option_contract(
            ib,
            und=und,
            right="P",
            pct_offset=pct_offset,
            override_price=(float(manual_price) if manual_price else None),
        )
    except Exception as e:
        msg = f"[PLAN] {symbol} P+ 不可（仕様取得失敗: {e}）"
        log.warning(msg)
        msgs.append(msg)
        return msgs

    buy_option(ib, opt=opt, qty=qty, dry_run=False, oca_group=oca_group)
    live_line = f"[LIVE] {symbol} PUT BUY {qty} @ {strike} ({expiry})"
    log.info(live_line)
    msgs.append(live_line)
    return msgs