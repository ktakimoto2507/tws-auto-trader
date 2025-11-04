# -*- coding: utf-8 -*-
"""
単体実行で UVIX P+ 状態を標準出力に表示
"""
from src.ib.uvix_p_plus import get_uvix_p_plus_state
from ib_insync import util

if __name__ == "__main__":
    s = get_uvix_p_plus_state(request_mktdata_type=1)
    # 見やすく整形
    out = {
        "connected": s.connected,
        "conid": s.conid,
        "symbol": s.symbol,
        "currency": s.currency,
        "position_qty": s.position_qty,
        "avg_cost": s.avg_cost,
        "last": s.last,
        "bid": s.bid,
        "ask": s.ask,
        "close": s.close,
        "unrealized_pnl": s.unrealized_pnl,
        "open_orders": s.open_orders,
    }
    print(util.tree(out))
