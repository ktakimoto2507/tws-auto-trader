#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UVIX P+ プローブ
 - 既存の状態ダンプ（--state）はそのまま
 - 計算プラン検証（--plan）で mark/limit/stop/qty を DRY 計算
"""
from __future__ import annotations
import os
import argparse
import logging
from ib_insync import util
from src.ib.uvix_p_plus import get_uvix_p_plus_state, place_uvix_p_plus_order

def setup_logger(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    # 10089 を赤ログにしない
    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)

def dump_state(md_type: int, host: str | None, port: int | None, client_id: int | None) -> None:
    s = get_uvix_p_plus_state(
        host=host, port=port, client_id=client_id,
        request_mktdata_type=md_type
    )
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

def run_plan(md_type: int, budget: float, stop: float, qty: int | None,
             host: str | None, port: int | None, client_id: int | None) -> int:
    logging.info(f"[PLAN] md_type={md_type} budget={budget} stop={stop} qty={qty}")
    s = get_uvix_p_plus_state(
        host=host, port=port, client_id=client_id,
        request_mktdata_type=md_type
    )
    logging.info(f"[STATE] pos={s.position_qty} avg={s.avg_cost} last={s.last} bid={s.bid} ask={s.ask} close={s.close}")
    try:
        # 計算側は clientId を +1 して接続競合を回避
        result = place_uvix_p_plus_order(
            qty=qty,                      # ← fixed_qty ではなく qty
            budget=budget,
            stop_loss_pct=stop,           # ← stop ではなく stop_loss_pct
            request_mktdata_type=md_type, # ← --md の指定をDRY側にも反映
            dry_run=True,                 # DRY計算のみ（発注しない）
            host=host, port=port,
            client_id=(None if client_id is None else client_id + 1),
        )
        logging.info(f"[PLAN RESULT] {result}")
        return 0
    except Exception as e:
        logging.exception(f"[ERROR] plan failed: {e}")
        return 2

def main() -> int:
    p = argparse.ArgumentParser(description="Probe UVIX P+")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--state", action="store_true", help="状態ダンプ（デフォルト）")
    mode.add_argument("--plan", action="store_true", help="発注プラン（DRY）を計算・出力")
    p.add_argument("--md", type=int, default=1, help="request_mktdata_type (1=LIVE,2=FROZEN,3=DELAYED,4=DELAYED_FROZEN)")
    p.add_argument("--budget", type=float, default=60000.0, help="予算（USD）")
    p.add_argument("--stop", type=float, default=0.20, help="STOP比率（例: 0.2）")
    p.add_argument("--qty", type=int, default=None, help="数量を固定する場合")
    p.add_argument("--log", default="INFO", help="ログレベル（DEBUG/INFO/WARN）")
    p.add_argument("--host", default=None, help="TWS/IB host (既定: .env or 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="TWS/IB port (既定: .env or 7497)")
    p.add_argument("--client-id", type=int, default=None, help="clientId（既定: .env or 10）")
    args = p.parse_args()

    setup_logger(args.log)
    # --client-id 未指定なら .env(TWS_CLIENT_ID) or 10 を採用
    base_client_id = args.client_id if args.client_id is not None else int(os.getenv("TWS_CLIENT_ID", "10"))

    if args.plan:
        return run_plan(
            md_type=args.md,
            budget=args.budget,
            stop=args.stop,
            qty=args.qty,
            host=args.host,
            port=args.port,
            client_id=base_client_id,   # ← base を渡す（run_plan 内で 1 してDRY側に使う）
        )
    # 状態ダンプは base_client_id をそのまま使う
    dump_state(args.md or 1, args.host, args.port, base_client_id)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
