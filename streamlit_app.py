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
import streamlit as st
# --- logging bootstrap (診断用) ---
import logging, sys
if not logging.getLogger().handlers:
    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    h.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)
orders_log = logging.getLogger("orders")
orders_log.propagate = False
if not orders_log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
    orders_log.addHandler(_h)
    orders_log.setLevel(logging.INFO)
    
from pathlib import Path
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from src.ib_client import IBClient, make_etf, qualify_or_raise, wait_price
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

def run_nugt_cc(budget: float, stop_pct_val: float = 0.06, ref: float | None = None, live: bool = False, **kwargs):
    #print(f"[DEBUG] enter run_nugt_cc live={live} budget={budget} stop={stop_pct_val}")
    orders_log.info(f"[DEBUG] enter run_nugt_cc live={live} budget={budget} stop={stop_pct_val}")
    # 互換: ref_price/price/entry でも受ける
    if ref is None:
        ref = kwargs.get("ref_price") or kwargs.get("price") or kwargs.get("entry")
    assert ref is not None and ref > 0, "ref price is required"
    """
    DRY RUN: 予算いっぱい現物→取得価格の6%下にSTP→ATMコール売り。
    manual_price があればそれを使用。無ければスナップショット価格を取得。
    """
    msgs: list[str] = []
    cli = get_client()

    spec = StockSpec("NUGT", "SMART", "USD")
    und  = Underlying("NUGT", "SMART", "USD")

    # 価格：手動 > スナップショット
    px = float(ref)
    if not math.isfinite(px) or px <= 0:
        raise RuntimeError(f"NUGT price is invalid: {px}")

    qty_shares = int(budget // px)
    if qty_shares < 1:
        raise RuntimeError(f"Budget too small: budget={budget}, price≈{px:.2f}")

    qty_contracts = qty_shares // 100
    msgs.append(f"price≈{px:.2f}, budget={budget}, shares={qty_shares}, option_contracts={qty_contracts}")

    # 1+2) 親子（ブランケット）：BUY → 親Fill後にSTOPを自動有効化
    stop_price = round(px * (1 - stop_pct_val), 2)
    # --- ここで DRY/LIVE に関わらず必ず人間が読めるログを出す ---
    if not live:
        orders_log.info(f"[DRY RUN] STOCK MKT BUY {qty_shares} NUGT")
        orders_log.info(f"[DRY RUN] STOCK STP SELL {qty_shares} NUGT @ {stop_price:.2f} (ref={px:.2f}, pct={stop_pct_val})")
    else:
        orders_log.info(f"[LIVE PREVIEW] BUY {qty_shares} NUGT -> STOP {stop_price:.2f} (ref={px:.2f}, pct={stop_pct_val})")

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
# --- Patch: 手動価格ON時は決定価格として採用＆取引許可 ---
if manual_toggle and manual_price:
    st.session_state["manual_price:NUGT"] = manual_price
    st.session_state["decided_price:NUGT"] = manual_price
    st.session_state["can_trade"] = True
    st.sidebar.success(f"Manual decided price set: {manual_price}")
show_logs = st.sidebar.checkbox("Show recent logs", value=True)
live = st.sidebar.checkbox("Live orders (Paper/Real)", value=False,
                           help="Off=DRY RUN（注文は送らない） / On=実注文（Paper/RealはTWSのログインに依存）")
st.session_state["budget_nugt"] = budget_nugt
st.session_state["live_orders"]  = live
# .env の DRY_RUN を見て“デフォルトの意図”を明示（実際の発注可否は live チェックに依存）
ENV_DRY = os.getenv("DRY_RUN", "true").lower() == "true"
mode_text = "DRY RUN（env=DRY_RUN=true）" if ENV_DRY else "LIVE準備（env=DRY_RUN=false）"
st.sidebar.markdown(f"**Env Mode**: {mode_text}")

st.sidebar.markdown("### Manual Run")
if st.sidebar.button("Run NUGT Covered Call"):
    # 価格の決定（手動→自動の順で拾う）
    price = (
        st.session_state.get("decided_price:NUGT")
        or st.session_state.get("manual_price:NUGT")
    )
    if not price or float(price) <= 0:
        st.sidebar.error("決定価格がありません。Priceタブで手動 or 自動で価格を確定してください。")
        st.stop()
    # DRY/LIVE 切替（サイドバーの Live orders）
    live = bool(st.session_state.get("live_orders", False))
    # 予算とストップ％（サイドバーの値を使う。無ければ既定値）
    budget = float(st.session_state.get("budget_nugt", 600000))
    stop_pct = float(st.session_state.get("stop_pct", 0.06))
    # 念のため can_trade を最終的に True に
    st.session_state["can_trade"] = True
    # 実行
    # run_nugt_cc の定義順: (budget, stop_pct_val, ref, live) を想定し位置引数で渡す
    try:
        msgs = run_nugt_cc(budget, stop_pct, float(price), live)
        st.sidebar.success(f"NUGT Covered Call – {'LIVE' if live else 'DRY RUN'} 完了")
        if msgs:
            for m in msgs:
                orders_log.info(m)
    except Exception as e:
        orders_log.exception("manual run failed")
        st.sidebar.error(f"実行エラー: {e}")

st.sidebar.markdown("### Upcoming (NY time → Local)")
for ny, local in next_weekly_times():
    st.sidebar.caption(f"Fri {ny.strftime('%Y-%m-%d %H:%M')} NY → {local.strftime('%Y-%m-%d %H:%M')} Local")

# --- ここからタブ部分 丸ごと置換 ---------------------------------
# Main tabs
tab_price, tab1, tab2, tab3 = st.tabs(["Price (NUGT/TMF)", "Account/Positions", "Open Orders", "Logs"])

with tab_price:
    st.subheader("Price Probe (Delayed MD)")

    cli = get_client()
    if not cli:
        st.info("未接続です。左サイドバーから接続してください。")
    else:
        col1, col2, col3 = st.columns([2,1,1])
        with col1:
            symbol = st.selectbox("Symbol", ["NUGT", "TMF"], index=0, key="price_symbol")
        with col2:
            timeout_sec = st.number_input("Timeout (sec)", min_value=3.0, max_value=30.0, value=12.0, step=0.5)
        with col3:
            probe_btn = st.button("価格取得（待機実行）", use_container_width=True)

        if probe_btn:
            try:
                log.info(f"[price-probe] start symbol={symbol} timeout={timeout_sec}")
                c = make_etf(symbol)
                qc = qualify_or_raise(cli.ib, c)

                # 取得中はスピナー表示（任意だが便利）
                with st.spinner("マーケットデータ受信待ち…"):
                    px, t = wait_price(cli.ib, qc, timeout=float(timeout_sec))

                # 結果をセッションに保存
                st.session_state[f"price:{symbol}"] = px
                st.session_state[f"ticker:{symbol}"] = {
                    "last": t.last, "bid": t.bid, "ask": t.ask, "close": t.close
                }

                # 画面表示
                if px:
                    st.success(f"{symbol} 決定価格: {px:.4f}")
                else:
                    st.warning(f"{symbol} の価格が timeout({timeout_sec:.1f}s) 内に確定しませんでした。")

                log.info(f"[price-probe] end   symbol={symbol} price={px} "
                         f"last={t.last} bid={t.bid} ask={t.ask} close={t.close}")
            except Exception as e:
                log.exception(f"[price-probe] error symbol={symbol}: {e}")
                st.error(f"価格取得エラー: {e}")

        px = st.session_state.get(f"price:{symbol}")
        tick = st.session_state.get(f"ticker:{symbol}", {})
        valid = isinstance(px, (int, float)) and px and px > 0

        met1, met2, met3, met4, met5 = st.columns(5)
        met1.metric("決定価格", f"{px:.4f}" if valid else "—")
        met2.metric("last",  f"{tick.get('last'):.4f}"  if tick.get("last")  else "—")
        met3.metric("close", f"{tick.get('close'):.4f}" if tick.get("close") else "—")
        met4.metric("bid",   f"{tick.get('bid'):.4f}"   if tick.get("bid")   else "—")
        met5.metric("ask",   f"{tick.get('ask'):.4f}"   if tick.get("ask")   else "—")

        # 価格が確定するまで発注不可の可視フラグ
        st.session_state["can_trade"] = bool(valid)
        st.caption("※ 決定価格は last→close→mid((bid+ask)/2) の順でフォールバックして決定します。")


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

