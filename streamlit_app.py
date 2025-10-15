# streamlit_app.py  — 正しい先頭レイアウト
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TWS Auto Trader – Streamlit UI"""

from __future__ import annotations  # ← docstring直後。ここだけが例外的に最優先

# 1) Windows/Streamlit用のイベントループ対策（← future import の後に来る）
import sys
import asyncio
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# 2) 通常のインポート
import os
import math
from pathlib import Path
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import streamlit as st

from src.ib_client import IBClient
from src.utils.logger import get_logger
from src.ib.orders import StockSpec, market, stop_pct, new_oca_group
from src.ib.options import Underlying, pick_option_contract, sell_option, _underlying_price

log = get_logger("st")

# 3) IBクライアント（接続）のセッション再利用
def get_client() -> IBClient:
    """
    Streamlitセッション単位でIB接続を使い回す。
    アプリ終了までdisconnectしない。
    """
    if "ib_client" not in st.session_state:
        st.session_state["ib_client"] = IBClient()
        st.session_state["ib_client"].connect()
    return st.session_state["ib_client"]

# 4) ヘルパ
def tail_log(path: Path, n: int = 200) -> str:
    if not path.exists():
        return "(logs/app.log がまだありません)"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"(ログ読み込み失敗: {e})"

def get_account_snapshot():
    cli = get_client()  # ← 再利用
    summary = cli.fetch_account_summary()
    positions = cli.fetch_positions()
    orders = cli.fetch_open_orders()
    acct_rows = [{"tag": x.tag, "value": x.value, "currency": x.currency or ""} for x in summary]
    pos_rows = [{"symbol": p.contract.symbol, "position": p.position, "avgCost": p.avgCost} for p in positions]
    ord_rows = [{
        "symbol": o.contract.symbol,
        "action": o.order.action,
        "qty": o.order.totalQuantity,
        "type": o.order.orderType,
        "lmtPrice": getattr(o.order, "lmtPrice", None),
        "auxPrice": getattr(o.order, "auxPrice", None)
    } for o in orders]
    return acct_rows, pos_rows, ord_rows

def run_nugt_cc_dry(budget: float, stop_pct_val: float = 0.06, manual_price: float | None = None) -> list[str]:
    """
    DRY RUN: 予算いっぱい現物→取得価格の6%下にSTP→ATMコール売り。
    manual_price があればそれを使用。無ければスナップショット価格を取得。
    """
    msgs: list[str] = []
    cli = get_client()

    spec = StockSpec("NUGT", "SMART", "USD")
    und  = Underlying("NUGT", "SMART", "USD")

    # 価格：手動 > スナップショット
    px = float(manual_price) if manual_price is not None else _underlying_price(cli.ib, und)
    if not math.isfinite(px) or px <= 0:
        raise RuntimeError(f"NUGT price is invalid: {px}")

    qty_shares = int(budget // px)
    if qty_shares < 1:
        raise RuntimeError(f"Budget too small: budget={budget}, price≈{px:.2f}")

    qty_contracts = qty_shares // 100
    msgs.append(f"price≈{px:.2f}, budget={budget}, shares={qty_shares}, option_contracts={qty_contracts}")

    oca = new_oca_group("COVERED")

    # 1) 株 BUY（DRY）
    market(cli.ib, spec, "BUY", qty_shares, dry_run=True)

    # 2) 6% STP（DRY）
    stop_pct(cli.ib, spec, qty_shares, reference_price=px, pct=stop_pct_val, dry_run=True, oca_group=oca)

    # 3) ATM CALL SELL（DRY）
    if qty_contracts >= 1:
        opt, strike, expiry = pick_option_contract(cli.ib, und, right="C", pct_offset=0.0, prefer_friday=True)
        sell_option(cli.ib, opt, qty_contracts, dry_run=True)
        msgs.append(f"Option: CALL {strike} @ {expiry} x {qty_contracts} (SELL)")
    else:
        msgs.append("株数が100未満のためオプション売りはスキップ")

    return msgs

def next_weekly_times(ny_hour: int = 9, ny_minute: int = 35, weeks: int = 6):
    tz_ny = ZoneInfo("America/New_York")
    tz_local = ZoneInfo(os.getenv("TZ", "Asia/Tokyo"))
    now = datetime.now(tz_ny)
    days_ahead = (4 - now.weekday()) % 7  # Fri=4
    base = datetime.combine((now + timedelta(days=days_ahead)).date(), time(ny_hour, ny_minute), tz_ny)
    if base < now:
        base += timedelta(days=7)
    return [(base + timedelta(weeks=i), (base + timedelta(weeks=i)).astimezone(tz_local)) for i in range(weeks)]

# 5) UI
st.set_page_config(page_title="IB TWS – AutoTrader", layout="wide")
st.title("IB TWS – AutoTrader (Dashboard)")

# Sidebar
st.sidebar.header("Settings")
budget_nugt = float(st.sidebar.text_input("Budget – NUGT (USD)", os.getenv("BUDGET_NUGT", "5000")))
manual_toggle = st.sidebar.checkbox("Use manual price for NUGT", value=False)
manual_price = float(st.sidebar.text_input("Manual price (NUGT)", "100.0")) if manual_toggle else None
show_logs = st.sidebar.checkbox("Show recent logs", value=True)

st.sidebar.markdown("### Manual Run")
if st.sidebar.button("Run NUGT Covered Call (DRY RUN)"):
    try:
        msgs = run_nugt_cc_dry(budget_nugt, 0.06, manual_price)
        st.success("NUGT Covered Call – DRY RUN 完了")
        for m in msgs:
            st.write("•", m)
    except Exception as e:
        st.error(f"実行エラー: {e}")

st.sidebar.markdown("### Upcoming (NY time → Local)")
for ny, local in next_weekly_times():
    st.sidebar.caption(f"Fri {ny.strftime('%Y-%m-%d %H:%M')} NY → {local.strftime('%Y-%m-%d %H:%M')} Local")

# Main tabs
tab1, tab2, tab3 = st.tabs(["Account/Positions", "Open Orders", "Logs"])

with tab1:
    st.subheader("Account & Positions")
    if st.button("Refresh account / positions"):
        st.experimental_rerun()
    try:
        acct_rows, pos_rows, ord_rows = get_account_snapshot()
        key_tags = {"NetLiquidation", "AvailableFunds", "BuyingPower", "TotalCashValue", "SMA",
                    "StockMarketValue", "OptionMarketValue", "GrossPositionValue", "RealizedPnL", "UnrealizedPnL"}
        filt = [r for r in acct_rows if r["tag"] in key_tags]
        st.table(filt)
        st.write("Positions")
        st.table(pos_rows if pos_rows else [{"info": "(No positions)"}])
    except Exception as e:
        st.error(f"取得エラー: {e}")

with tab2:
    st.subheader("Open Orders")
    try:
        _, _, ord_rows = get_account_snapshot()
        st.table(ord_rows if ord_rows else [{"info": "(No open orders)"}])
    except Exception as e:
        st.error(f"取得エラー: {e}")

with tab3:
    st.subheader("Recent Logs (logs/app.log)")
    if show_logs:
        st.code(tail_log(Path("logs") / "app.log", n=300), language="log")
    else:
        st.caption("（サイドバーのチェックで表示）")
# ーーー end ーーー
