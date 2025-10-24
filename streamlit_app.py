#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TWS Auto Trader – Streamlit UI"""

from __future__ import annotations

# --- 標準ライブラリ ---
import sys
import asyncio
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
import os
import math
import logging
from pathlib import Path
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

# --- サードパーティ ---
import streamlit as st

# --- プロジェクト内部 ---
from src.ib_client import IBClient
from src.utils.logger import get_logger
from src.ib.orders import StockSpec, bracket_buy_with_stop, decide_lmt_stop_take
from src.ib.options import Underlying, pick_option_contract, sell_option  # ← _underlying_price 削除
from src.config import OrderPolicy
from src.price import get_prices, last_price_meta
from src.symbols import SYMBOLS_ORDER
from src.metrics_vix import save_vix_snapshot
from src.orders.manual_order import place_manual_order
from src.utils.loop import ensure_event_loop

# --- logging bootstrap (診断用) ---
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


log = get_logger("st")
POL = OrderPolicy()  # 発注ポリシーをここで固定（UIはLiveの可否のみ））

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
    ensure_event_loop()  # ★ 追加（この関数内でib_insync同期APIを安全に使えるように）
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

    # 1+2) 親子（ブランケット）：親=指値、子=逆指値（既定ポリシーで自動決定）
    lmt_price, stop_price, take_profit = decide_lmt_stop_take(
        px,
        slippage_bps=POL.slippage_bps,
        stop_pct=POL.stop_pct,  # ← stop_pct_val（UI）よりもポリシー優先に統一
        take_profit_pct=POL.take_profit_pct,
    )
    # --- ここで DRY/LIVE を問わず、**LMTベース**のプレビューを出す ---
    if not live:
        orders_log.info(f"[DRY RUN] STOCK LMT BUY {qty_shares} NUGT @ {lmt_price:.2f}")
        orders_log.info(f"[DRY RUN] STOCK STP SELL {qty_shares} NUGT @ {stop_price:.2f} (ref={px:.2f})")
        if take_profit is not None:
            orders_log.info(f"[DRY RUN] STOCK LMT SELL(TP) {qty_shares} NUGT @ {take_profit:.2f}")
    else:
        orders_log.info(
            f"[LIVE PREVIEW] BUY LMT {qty_shares} @ {lmt_price:.2f} -> "
            f"STOP {stop_price:.2f}{' -> TP ' + str(take_profit) if take_profit else ''} "
            f"(ref={px:.2f}, TIF={POL.tif}, outsideRth={POL.outside_rth})"
        )
    parent, child, parent_trade = bracket_buy_with_stop(
        ib=cli.ib,
        spec=spec,
        qty=qty_shares,
        entry_type="LMT",              # ★ 親をLMTに統一
        lmt_price=lmt_price,           # ★ 自動算出した指値
        stop_price=stop_price,
        tif=POL.tif,                   # "DAY" / "GTC" をポリシーで固定
        outside_rth=POL.outside_rth,   # 立会時間外の約定可否をポリシーで固定
        dry_run=not live,              # ← UIのLiveトグルで送信可否
    )
    # ★ 実発注（Paper/Live）のときだけ親注文のIDをログへ残す
    if parent_trade is not None:
        try:
            orders_log.info(
                f"[PLACED] orderId={parent_trade.order.orderId} "
                f"permId={parent_trade.order.permId} "
                f"parentType={parent_trade.order.orderType}"
            )
        except Exception:
            # ここでのログ失敗は致命ではないので握りつぶす
            pass
    msgs.append(
        f"Bracket: BUY {parent.orderType} {qty_shares} @ {getattr(parent, 'lmtPrice', None)} "
        f"→ STOP {stop_price:.2f}"
        + (f" → TP {take_profit:.2f}" if take_profit is not None else "")
        + f" | TIF={POL.tif} outsideRth={POL.outside_rth}"
    )

    # 3) ATM CALL SELL（DRY）
    if qty_contracts >= 1:
        opt, strike, expiry = pick_option_contract(cli.ib, und, right="C", pct_offset=0.0,
                                                   prefer_friday=True, override_price=px)
        sell_option(cli.ib, opt, qty_contracts, dry_run=not live)
        msgs.append(f"Option: CALL {strike} @ {expiry} x {qty_contracts} (SELL)")
    else:
        msgs.append("株数が100未満のためオプション売りはスキップ")

    return msgs

def run_etf_buy_with_stop(symbol: str, budget: float, ref: float, live: bool = False) -> list[str]:
    ensure_event_loop()  # ★ 追加
    """
    任意ETFを NUGTと同じロジック（親=指値, 子=逆指値(+TP)）で購入。
    ref: 決定価格（手動またはPriceタブ採用価格）
    """
    orders_log.info(f"[DEBUG] run_etf_buy_with_stop symbol={symbol} live={live} budget={budget} ref={ref}")
    if not (ref and ref > 0):
        raise RuntimeError("決定価格(ref)が不正です")

    cli = get_client()
    if not cli:
        raise RuntimeError("IB 未接続です")

    spec = StockSpec(symbol, "SMART", "USD")
    px = float(ref)

    qty_shares = int(budget // px)
    if qty_shares < 1:
        raise RuntimeError(f"Budget too small: budget={budget}, price≈{px:.2f}")

    # NUGTと同じポリシーでLMT/STOP/TPを決定
    lmt_price, stop_price, take_profit = decide_lmt_stop_take(
        px,
        slippage_bps=POL.slippage_bps,
        stop_pct=POL.stop_pct,
        take_profit_pct=POL.take_profit_pct,
    )

    # プレビュー & 送信
    if not live:
        orders_log.info(f"[DRY RUN] {symbol} LMT BUY {qty_shares} @ {lmt_price:.2f}")
        orders_log.info(f"[DRY RUN] {symbol} STP SELL {qty_shares} @ {stop_price:.2f} (ref={px:.2f})")
        if take_profit is not None:
            orders_log.info(f"[DRY RUN] {symbol} LMT SELL(TP) {qty_shares} @ {take_profit:.2f}")

    parent, child, parent_trade = bracket_buy_with_stop(
        ib=cli.ib,
        spec=spec,
        qty=qty_shares,
        entry_type="LMT",
        lmt_price=lmt_price,
        stop_price=stop_price,
        tif=POL.tif,
        outside_rth=POL.outside_rth,
        dry_run=not live,
    )
    if parent_trade is not None:
        try:
            orders_log.info(
                f"[PLACED] orderId={parent_trade.order.orderId} permId={parent_trade.order.permId} "
                f"parentType={parent_trade.order.orderType}"
            )
        except Exception:
            pass

    msgs = [
        f"{symbol}: price≈{px:.2f}, budget={budget}, shares={qty_shares}",
        f"Bracket: BUY {parent.orderType} {qty_shares} @ {getattr(parent, 'lmtPrice', None)} "
        f"→ STOP {stop_price:.2f}" + (f" → TP {take_profit:.2f}" if take_profit is not None else "") +
        f" | TIF={POL.tif} outsideRth={POL.outside_rth}"
    ]
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
        # ★ Streamlit実行スレッドにイベントループを保証（ib_insyncの同期APIが使えるように）
        ensure_event_loop()
        # --- 環境バナー＆口座検証（ここなら cli が存在） ---
        env = os.getenv("RUN_MODE", "paper")
        accts = cli.ib.managedAccounts() or []
        acct_hint = ", ".join(accts) if accts else "(unknown)"
        is_paper = any(a.startswith("DU") for a in accts)  # IBKR: Paper=DUxxxxx
        st.success(f"Connected – Accounts: {acct_hint} | RUN_MODE={env}")
        if env == "live" and is_paper:
            st.warning("RUN_MODE=live ですが Paper 口座に接続しています。PORT/ログイン/PORT番号を再確認してください。")
        if env == "paper" and not is_paper:
            st.warning("RUN_MODE=paper ですが Live 口座に接続しています。PORT/ログイン/PORT番号を再確認してください。")

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
live_toggle = st.sidebar.checkbox("Live orders (Paper/Real)", value=False,
                           help="Off=DRY RUN（注文は送らない） / On=実注文（Paper/RealはTWSのログインに依存）")
st.session_state["budget_nugt"] = budget_nugt
st.session_state["live_orders"]  = live_toggle
# .env の DRY_RUN を見て“デフォルトの意図”を明示（実際の発注可否は live チェックに依存）
ENV_DRY = os.getenv("DRY_RUN", "true").lower() == "true"
mode_text = "DRY RUN（env=DRY_RUN=true）" if ENV_DRY else "LIVE準備（env=DRY_RUN=false）"
st.sidebar.markdown(f"**Env Mode**: {mode_text}")
st.sidebar.markdown("### Live Arming")
confirm_text = st.sidebar.text_input('Type to arm LIVE (exact):', value='', help='本番送信するには "LIVE" と入力')
armed_live = bool(live_toggle and (confirm_text.strip() == "LIVE"))
if live_toggle and not armed_live:
    st.sidebar.warning('LIVE送信を有効化するには "LIVE" と入力してください。')
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
    # DRY/LIVE 切替（サイドバーの Live orders + 確認ワード）
    live = bool(armed_live)
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

# === TMF 手動価格 & 走行 ===
st.sidebar.markdown("---")
st.sidebar.markdown("### TMF – Manual")

budget_tmf = float(st.sidebar.text_input("Budget – TMF (USD)", os.getenv("BUDGET_TMF", "5000")))
manual_toggle_tmf = st.sidebar.checkbox("Use manual price for TMF", value=False, key="man_tmf")
manual_price_tmf = float(st.sidebar.text_input("Manual price (TMF)", "5.00")) if manual_toggle_tmf else None
if manual_toggle_tmf and manual_price_tmf:
    st.session_state["manual_price:TMF"] = manual_price_tmf
    st.session_state["decided_price:TMF"] = manual_price_tmf
    st.session_state["can_trade"] = True
    st.sidebar.success(f"Manual decided price (TMF): {manual_price_tmf}")

if st.sidebar.button("Run TMF Buy with Stop"):
    price = st.session_state.get("decided_price:TMF") or st.session_state.get("manual_price:TMF")
    if not price or float(price) <= 0:
        st.sidebar.error("TMFの決定価格がありません。Priceタブ or 手動で設定してください。")
        st.stop()
    live = bool(armed_live)  # 既存のライブ武装フラグを使う
    try:
        msgs = run_etf_buy_with_stop("TMF", budget_tmf, float(price), live)
        st.sidebar.success(f"TMF – {'LIVE' if live else 'DRY RUN'} 完了")
        for m in msgs:
            orders_log.info(m)
    except Exception as e:
        orders_log.exception("TMF manual run failed")
        st.sidebar.error(f"TMF 実行エラー: {e}")

# === UVIX 手動価格 & 走行 ===
st.sidebar.markdown("---")
st.sidebar.markdown("### UVIX – Manual")

budget_uvix = float(st.sidebar.text_input("Budget – UVIX (USD)", os.getenv("BUDGET_UVIX", "3000")))
manual_toggle_uvix = st.sidebar.checkbox("Use manual price for UVIX", value=False, key="man_uvix")
manual_price_uvix = float(st.sidebar.text_input("Manual price (UVIX)", "10.00")) if manual_toggle_uvix else None
if manual_toggle_uvix and manual_price_uvix:
    st.session_state["manual_price:UVIX"] = manual_price_uvix
    st.session_state["decided_price:UVIX"] = manual_price_uvix
    st.session_state["can_trade"] = True
    st.sidebar.success(f"Manual decided price (UVIX): {manual_price_uvix}")

if st.sidebar.button("Run UVIX Buy with Stop"):
    price = st.session_state.get("decided_price:UVIX") or st.session_state.get("manual_price:UVIX")
    if not price or float(price) <= 0:
        st.sidebar.error("UVIXの決定価格がありません。Priceタブ or 手動で設定してください。")
        st.stop()
    live = bool(armed_live)
    try:
        msgs = run_etf_buy_with_stop("UVIX", budget_uvix, float(price), live)
        st.sidebar.success(f"UVIX – {'LIVE' if live else 'DRY RUN'} 完了")
        for m in msgs:
            orders_log.info(m)
    except Exception as e:
        orders_log.exception("UVIX manual run failed")
        st.sidebar.error(f"UVIX 実行エラー: {e}")


st.sidebar.markdown("### Upcoming (NY time → Local)")
for ny, local in next_weekly_times():
    st.sidebar.caption(f"Fri {ny.strftime('%Y-%m-%d %H:%M')} NY → {local.strftime('%Y-%m-%d %H:%M')} Local")

# --- ここからタブ部分 丸ごと置換 ---------------------------------
# Main tabs
tab_price, tab1, tab2, tab3 = st.tabs(["Price (NUGT/TMF/UVIX/VIX)", "Account/Positions", "Open Orders", "Logs"])

with tab_price:
    st.subheader("Price Probe (Delayed/Fallback)")

    cli = get_client()
    if not cli:
        st.info("未接続です。左サイドバーから接続してください。")
    else:
        sel = st.multiselect("Symbols", SYMBOLS_ORDER, default=["NUGT","TMF","UVIX","VIX"])
        col1, col2 = st.columns([1,1])
        with col1:
            probe_btn = st.button("価格取得（遅延／フォールバック）", use_container_width=True)
        with col2:
            st.caption("決定価格は marketPrice→mid→close の順でフォールバック")

        if probe_btn and sel:
            try:
                from src.utils.loop import ensure_event_loop
                ensure_event_loop()  # 念押し
                with st.spinner("マーケットデータ取得中…"):
                    prices = get_prices(cli.ib, sel, delay_type=int(md_type))
                meta = last_price_meta()
                # セッションへ保存＋表描画用に整形
                rows = []
                for s in sel:
                    px = prices.get(s)
                    st.session_state[f"price:{s}"] = px
                    rows.append({
                        "symbol": s,
                        "price": (f"{px:.4f}" if px else "—"),
                        "source": meta.get(s, "")
                    })
                st.success("価格更新しました。")
                st.table(rows)
            except Exception as e:
                log.exception("price fetch error")
                st.error(f"価格取得エラー: {e}")

        # 一覧と「採用」ボタン（決定価格にセット）
        if sel:
            st.divider()
            st.write("決定価格に採用（各行のボタンで session_state['decided_price:<SYM>'] に保存）")
            grid = st.columns(len(sel))
            for i, s in enumerate(sel):
                with grid[i]:
                    px = st.session_state.get(f"price:{s}")
                    st.metric(s, f"{px:.4f}" if px else "—")
                    if st.button("採用", key=f"adopt_{s}", use_container_width=True, disabled=px is None):
                        st.session_state[f"decided_price:{s}"] = float(px)
                        st.success(f"{s} 決定価格を {px:.4f} に設定")
                        if s == "NUGT":
                            st.session_state["can_trade"] = True

        # VIX 月次スナップショット（手動保存）
        st.divider()
        with st.expander("VIX 月次スナップショット（手動保存）"):
            default_px = st.session_state.get("price:VIX")
            vix_px = st.number_input("VIX Close（手入力可）", value=float(default_px) if default_px else 0.0, step=0.1)
            vix_date = st.date_input("対象日", value=datetime.today())
            if st.button("保存（data/vix_monthly.csv）"):
                try:
                    assert vix_px and vix_px > 0
                    save_vix_snapshot(vix_date, float(vix_px))
                    st.success("保存しました（data/vix_monthly.csv）")
                except Exception as e:
                    st.error(f"保存失敗: {e}")

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



# --- 手動購入セクション ---
st.markdown("### Manual Order (成行)")

col1, col2, col3 = st.columns(3)
with col1:
    manual_symbol = st.selectbox("Symbol", ["NUGT", "TMF", "UVIX", "VIX"], index=0)
with col2:
    qty = st.number_input("Quantity", min_value=100, step=100, value=100)
with col3:
    dry = st.checkbox("Dry run only", value=True)

side = st.radio("Action", ["BUY", "SELL"], horizontal=True)

if st.button("Place Order"):
    ensure_event_loop()  # ★ 念のため
    if cli.ib and cli.ib.isConnected():
        result = place_manual_order(cli.ib, manual_symbol, int(qty), action=side, dry_run=dry)
        st.success(f"✅ {side} {manual_symbol} x {qty} ({result['status']})")
    else:
        st.error("❌ IB未接続です。左サイドバーからConnectしてください。")


