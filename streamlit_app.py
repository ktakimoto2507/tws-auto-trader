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
from src.ib.orders import StockSpec, bracket_buy_with_stop
from src.ib.options import Underlying, pick_option_contract, sell_option, _underlying_price

log = get_logger("st")

# 3) IBクライアント（接続）のセッション再利用
def get_client() -> IBClient | None:
    """接続済みなら返す。未接続なら None。"""
    return st.session_state.get("ib_client")

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

def run_nugt_cc(budget: float, stop_pct_val: float = 0.06,
                manual_price: float | None = None, live: bool = False) -> list[str]:
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

    #oca = new_oca_group("COVERED")

    # 1) 株 BUY（DRY）
    #market(cli.ib, spec, "BUY", qty_shares, dry_run=not live)

    # 2) 6% STP（DRY）
    #stop_pct(cli.ib, spec, qty_shares, reference_price=px, pct=stop_pct_val,
    #         dry_run=not live, oca_group=oca)
    # 1+2) 親子（ブランケット）：BUY → 親Fill後にSTOPを自動有効化
    stop_price = round(px * (1 - stop_pct_val), 2)
    parent, child, parent_trade = bracket_buy_with_stop(
        ib=cli.ib,
        spec=spec,
        qty=qty_shares,
        entry_type="MKT",      # すぐ約定を見たいなら "LMT" と lmt_price を指定
        lmt_price=None,        # entry_type="LMT" のときだけ値を入れる
        stop_price=stop_price,
        tif="DAY",             # 長く持つなら "GTC"
        outside_rth=True,      # 時間外も許可したいなら True
        dry_run=not live,      # ← サイドバーの Live トグルで切り替え
    )
    msgs.append(f"Bracket: BUY({parent.orderType}) {qty_shares} → STOP {stop_price:.2f}")

    # 3) ATM CALL SELL（DRY）
    if qty_contracts >= 1:
        opt, strike, expiry = pick_option_contract(cli.ib, und, right="C", pct_offset=0.0,
                                                   prefer_friday=True, override_price=px)
        sell_option(cli.ib, opt, qty_contracts, dry_run=not live)
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
# 接続設定（secretsがあればそれを初期値に）
def _sget(key: str, default: str):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

host = st.sidebar.text_input("IB Host", _sget("IB_HOST", "127.0.0.1"))
port = st.sidebar.number_input("IB Port", value=int(_sget("IB_PORT", "7497")), step=1)
client_id = st.sidebar.number_input("Client ID", value=int(_sget("IB_CLIENT_ID", "10")), step=1)
md_type = st.sidebar.selectbox("Market Data Type", [1,2,3,4], index=2)

colA, colB = st.sidebar.columns(2)
if colA.button("Connect"):
    cfg = type("Tmp", (), {"host": host, "port": int(port), "client_id": int(client_id), "account": None})
    cli = IBClient(cfg)  # 簡易cfg
    try:
        cli.connect(market_data_type=int(md_type))
        st.session_state["ib_client"] = cli
        st.success("Connected")
    except Exception as e:
        st.error(f"Connect failed: {e}")

if colB.button("Disconnect"):
    cli = get_client()
    if cli:
        cli.disconnect()
        st.session_state.pop("ib_client", None)
        st.info("Disconnected")
budget_nugt = float(st.sidebar.text_input("Budget – NUGT (USD)", os.getenv("BUDGET_NUGT", "5000")))
manual_toggle = st.sidebar.checkbox("Use manual price for NUGT", value=False)
manual_price = float(st.sidebar.text_input("Manual price (NUGT)", "100.0")) if manual_toggle else None
show_logs = st.sidebar.checkbox("Show recent logs", value=True)
live = st.sidebar.checkbox("Live orders (Paper/Real)", value=False,
                           help="Off=DRY RUN（注文は送らない） / On=実注文（Paper/RealはTWSのログインに依存）")

st.sidebar.markdown("### Manual Run")
if st.sidebar.button("Run NUGT Covered Call"):
    try:
        msgs = run_nugt_cc(budget_nugt, 0.06, manual_price, live=live)
        st.success("NUGT Covered Call – " + ("LIVE（Paper/Real）" if live else "DRY RUN") + " 完了")

        for m in msgs:
            st.write("•", m)
    except Exception as e:
        st.error(f"実行エラー: {e}")

st.sidebar.markdown("### Upcoming (NY time → Local)")
for ny, local in next_weekly_times():
    st.sidebar.caption(f"Fri {ny.strftime('%Y-%m-%d %H:%M')} NY → {local.strftime('%Y-%m-%d %H:%M')} Local")

# --- ここからタブ部分 丸ごと置換 ---------------------------------
# Main tabs
tab1, tab2, tab3 = st.tabs(["Account/Positions", "Open Orders", "Logs"])

with tab1:
    st.subheader("Account & Positions")

    # 接続確認
    cli = get_client()
    if not cli:
        st.info("未接続です。左のサイドバーから接続してください。")
    else:
        # 表示更新ボタン
        if st.button("Refresh account / positions"):
            st.experimental_rerun()

        try:
            acct_rows, pos_rows, _ = get_account_snapshot()
            key_tags = {
                "NetLiquidation", "AvailableFunds", "BuyingPower", "TotalCashValue", "SMA",
                "StockMarketValue", "OptionMarketValue", "GrossPositionValue", "RealizedPnL", "UnrealizedPnL"
            }
            filt = [r for r in acct_rows if r["tag"] in key_tags]
            st.write("Account Summary")
            st.table(filt)
            st.write("Positions")
            st.table(pos_rows if pos_rows else [{"info": "(No positions)"}])
        except Exception as e:
            st.error(f"取得エラー: {e}")

with tab2:
    st.subheader("Open Orders")

    cli = get_client()
    if not cli:
        st.info("未接続です。左のサイドバーから接続してください。")
    else:
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
# --- 置換ここまで --------------------------------------------------

