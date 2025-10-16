from ib_insync import IB, MarketOrder
from ..ib_client import make_etf, qualify_or_raise
from ..utils.loop import ensure_event_loop
from ..utils.logger import get_logger

log = get_logger("orders")

def place_manual_order(ib: IB, symbol: str, quantity: int, action: str = "BUY", dry_run: bool = True):
    """
    Streamlit上から手動発注を行う（Market成行）
    - ib: IB インスタンス
    - symbol: "NUGT" / "TMF" / "UVIX" 等
    - quantity: 発注株数
    - action: "BUY" or "SELL"
    - dry_run: Trueならログのみ、Falseで実発注
    """
    ensure_event_loop()
    c = make_etf(symbol)
    qc = qualify_or_raise(ib, c)
    log.info(f"[{action}] {symbol} x {quantity} shares {'(DRY_RUN)' if dry_run else ''}")

    if dry_run:
        log.info(f"[DRY RUN] {action} {quantity} {symbol}")
        return {"status": "dry-run", "symbol": symbol, "qty": quantity}

    order = MarketOrder(action, quantity)
    trade = ib.placeOrder(qc, order)
    ib.sleep(1.0)
    log.info(f"Order placed: {trade}")
    return {"status": "sent", "symbol": symbol, "qty": quantity, "order": order}
