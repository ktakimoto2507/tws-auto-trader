#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TWS Auto Trader – Streamlit UI"""

from __future__ import annotations
from src.utils.loop import ensure_event_loop

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
from ib_insync import Index, util
from src.ib_client import make_etf  # 既存の統一契約ユーティリティを再利用
from src.utils.logger import get_logger
from src.ib.orders import StockSpec, bracket_buy_with_stop, decide_lmt_stop_take
from src.ib.options import Underlying, pick_option_contract, sell_option  # ← _underlying_price 削除
from src.config import OrderPolicy
from src.orders.manual_order import place_manual_order

def _resolve_last_then_close(t) -> float | None:
    """TWSウォッチリストの『直近』風: last があればそれ、無ければ close。"""
    for v in (getattr(t, "last", None), getattr(t, "close", None)):
        if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
            return float(v)
    return None


def fetch_prices_tws_like(ib, symbols: list[str], delay_type: int = 3, timeout: float = 2.0) -> dict[str, float | None]:
    """
    last→close の順で採用。snapshot=True -> 短時間待ち。
    delay_type: 1=RT, 2=Frozen, 3=Delayed, 4=DelayedFrozen
    """
    ensure_event_loop()
    try:
        ib.reqMarketDataType(int(delay_type))
    except Exception:
        pass

    contracts = []
    for s in symbols:
        if s.upper() == "VIX":
            c = Index("VIX", "CBOE", "USD")
        else:
            c = make_etf(s)  # SMART+USD（primaryExchange=ARCA）統一
        # qualify は失敗しても後段で拾えるように best-effort
        try:
            ib.qualifyContracts(c)
        except Exception:
            pass
        contracts.append((s, c))

    # snapshot でreq、少し待って読む
    tickers = {}
    for s, c in contracts:
        try:
            t = ib.reqMktData(c, "", True, False)  # snapshot=True
            util.run(ib.sleep(timeout))
            tickers[s] = t
            ib.cancelMktData(c)
        except Exception:
            tickers[s] = None

    prices = {}
    for s, t in tickers.items():
        prices[s] = _resolve_last_then_close(t) if t else None
    return prices

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

# LIVE/DRY を一元管理
def is_live() -> bool:
    return bool(st.session_state.get("live_orders", False))

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
    ensure_event_loop()           # ← 追加
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
        stop_pct=float(stop_pct_val),          # ← ここを“指定値優先”に変更（10%を通せる）
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

def run_tmf_cc(budget: float, stop_pct_val: float, ref: float, live: bool = False) -> list[str]:
    """
    TMF: 予算いっぱいで現物→取得価格の7%下にSTP→ATM(5刻み切り上げ)コール売り
    """
    ensure_event_loop()           # ← 追加
    orders_log.info(f"[DEBUG] run_tmf_cc live={live} budget={budget} stop={stop_pct_val}")
    assert ref and ref > 0, "ref price is required"

    cli = get_client()
    if not cli:
        raise RuntimeError("IB 未接続です")

    spec = StockSpec("TMF", "SMART", "USD")
    und  = Underlying("TMF", "SMART", "USD")

    px = float(ref)
    if not math.isfinite(px) or px <= 0:
        raise RuntimeError(f"TMF price is invalid: {px}")

    qty_shares = int(budget // px)
    if qty_shares < 1:
        raise RuntimeError(f"Budget too small: budget={budget}, price≈{px:.2f}")
    qty_contracts = qty_shares // 100

    # LMT/STOP/TP（STOPは指定値優先=7%）
    lmt_price, stop_price, take_profit = decide_lmt_stop_take(
        px,
        slippage_bps=POL.slippage_bps,
        stop_pct=float(stop_pct_val),
        take_profit_pct=POL.take_profit_pct,
    )

    # 送信（DRY/LIVE）
    if not live:
        orders_log.info(f"[DRY RUN] TMF LMT BUY {qty_shares} @ {lmt_price:.2f}")
        orders_log.info(f"[DRY RUN] TMF STP SELL {qty_shares} @ {stop_price:.2f} (ref={px:.2f})")
        if take_profit is not None:
            orders_log.info(f"[DRY RUN] TMF LMT SELL(TP) {qty_shares} @ {take_profit:.2f}")

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
                f"[PLACED] orderId={parent_trade.order.orderId} "
                f"permId={parent_trade.order.permId} parentType={parent_trade.order.orderType}"
            )
        except Exception:
            pass

    msgs = [
        f"TMF: price≈{px:.2f}, budget={budget}, shares={qty_shares}, option_contracts={qty_contracts}",
        f"Bracket: BUY {parent.orderType} {qty_shares} @ {getattr(parent, 'lmtPrice', None)} "
        f"→ STOP {stop_price:.2f}" + (f" → TP {take_profit:.2f}" if take_profit is not None else "") +
        f" | TIF={POL.tif} outsideRth={POL.outside_rth}"
    ]

    # ATM “5刻み切り上げ” ルール（例: 124.7→125, 132.5→135）
    # ここでは“方針と対象ストライク”を明確にログ出し（実際のコン約定は pick_option_contract の結果に従う）
    if qty_contracts >= 1:
        # 5刻みに切り上げ
        rounded = math.ceil(px / 5.0) * 5.0
        # 現行ヘルパーの都合で "ATM選好" で取得し、ストライク乖離はログに明示
        opt, strike, expiry = pick_option_contract(cli.ib, und, right="C", pct_offset=0.0,
                                                   prefer_friday=True, override_price=px)
        if float(strike) != float(rounded):
            orders_log.info(f"[NOTICE] TMF CC strike policy: ceil_to_5={rounded:.0f}, picked={strike} (helper制約でATM優先)")
        # 売り発注（DRY/LIVE）
        sell_option(cli.ib, opt, qty_contracts, dry_run=not live)
        msgs.append(f"Option: CALL target≈{rounded:.0f} (picked {strike}) @ {expiry} x {qty_contracts} (SELL)")
    else:
        msgs.append("株数が100未満のためオプション売りはスキップ")

    return msgs


def run_etf_buy_with_stop(symbol: str, budget: float, ref: float, live: bool = False) -> list[str]:
    """
    任意ETFを NUGTと同じロジック（親=指値, 子=逆指値(+TP)）で購入。
    ref: 決定価格（手動またはPriceタブ採用価格）
    """
    ensure_event_loop()           # ← 追加
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

def run_uvix_put_idea(budget: float, ref: float) -> list[str]:
    """
    UVIX: ATMから+15%近辺のPUTを「買い」想定（まずはアイデア出しとDRYプレビューのみ）
    - 例: ATM=10 → 11.5P
    - 予算 60,000 USD, ロスカットなし（リスク限定はオプションプレミアム）
    """
    assert ref and ref > 0, "ref price is required"
    px = float(ref)
    target_strike = round(px * 1.15, 1)  # 0.1刻みに丸め（例から逆算）。取引所刻みに合わせて後で調整可。

    # ここでは「どの契約・枚数を狙うか」を決めるだけ（IV/プレミアムは未取得）
    msg = []
    msg.append(f"UVIX: ATM≈{px:.2f} → target PUT strike≈{target_strike:.1f} (+15%)")
    msg.append(f"Budget: {budget:,.0f} USD (no stop; premium-defined risk)")

    # 実際のコン選定・買い発注は次段（buy_optionヘルパ追加）で実装。
    # いまは UI/ログに“狙い”を明示しておく。
    return msg


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
        ensure_event_loop()                           # ← 追加
        cli.connect(market_data_type=int(md_type))
        st.session_state["ib_client"] = cli
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

# === DRY/LIVE 切替（サイドバー・トグル） ===
live_toggle = st.sidebar.toggle(
    "LIVE orders (Paper/Real)",
    value=False,
    help="OFF=DRY RUN（注文は送らない） / ON=実注文（Paper/RealはTWSのログインに依存）",
)
st.session_state["live_orders"] = bool(live_toggle)
if live_toggle:
    st.sidebar.warning("LIVE モードです。注文は実際に送信されます。")


st.sidebar.markdown("### Upcoming (NY time → Local)")
for ny, local in next_weekly_times():
    st.sidebar.caption(f"Fri {ny.strftime('%Y-%m-%d %H:%M')} NY → {local.strftime('%Y-%m-%d %H:%M')} Local")

# --- Quick Portal（メイン） ---
with st.container(border=True):
    st.subheader("Quick Portal")
    c1, c2, c3, c4 = st.columns([1,1,1,2])

    # ===== NUGT =====
    with c1:
        st.markdown("**NUGT**")
        with st.form("form_nugt", clear_on_submit=False):
            budget_nugt = st.number_input("Budget (USD)", min_value=0.0, value=float(os.getenv("BUDGET_NUGT", "600000")), step=1000.0, key="budget_nugt_main")
            manual_price_nugt = st.number_input("Manual price", min_value=0.0, value=100.0, step=0.01, key="manual_price_nugt_main")
            use_manual_nugt = st.checkbox("Use manual price", value=False, key="use_manual_nugt_main")
            stop_nugt = st.number_input("Stop %", min_value=0.0, value=float(st.session_state.get("stop_pct_nugt", 0.10)), step=0.01, format="%.2f", key="stop_nugt_main")
            submit_nugt = st.form_submit_button("▶ NUGT CoveredCall", use_container_width=True)

        if submit_nugt:
            try:
                # 決定価格の解決
                if use_manual_nugt and manual_price_nugt > 0:
                    price = float(manual_price_nugt)
                    st.session_state["manual_price:NUGT"]  = price
                    st.session_state["decided_price:NUGT"] = price
                else:
                    price = st.session_state.get("decided_price:NUGT") or st.session_state.get("price:NUGT")
                if not price:
                    st.warning("NUGTの決定価格がありません。Priceタブで取得し、または Use manual price をONにして手動価格を入力してください。")
                else:
                    ensure_event_loop()  # 念のため
                    msgs = run_nugt_cc(
                        float(budget_nugt),
                        float(stop_nugt),
                        float(price),
                        is_live(), 
                    )
                    for m in msgs:
                        orders_log.info(m)
                    st.success("NUGT CC 完了（ログ参照）")
            except Exception as e:
                st.error(f"NUGT 実行エラー: {e}")

        st.caption("DRY RUN: 予算いっぱい現物→取得価格の6%下にSTP→ATMコール売り。manual_priceがあればそれを使用。無ければスナップショット価格を取得。")

    # ===== TMF =====
    with c2:
        st.markdown("**TMF**")
        with st.form("form_tmf", clear_on_submit=False):
            budget_tmf = st.number_input("Budget (USD)", min_value=0.0, value=float(os.getenv("BUDGET_TMF", "600000")), step=1000.0, key="budget_tmf_main")
            manual_price_tmf = st.number_input("Manual price", min_value=0.0, value=43.0, step=0.01, key="manual_price_tmf_main")
            use_manual_tmf = st.checkbox("Use manual price", value=False, key="use_manual_tmf_main")
            stop_tmf = st.number_input("Stop %", min_value=0.0, value=float(st.session_state.get("stop_pct_tmf", 0.07)), step=0.01, format="%.2f", key="stop_tmf_main")
            submit_tmf = st.form_submit_button("▶ TMF CoveredCall", use_container_width=True)

        if submit_tmf:
            try:
                if use_manual_tmf and manual_price_tmf > 0:
                    price = float(manual_price_tmf)
                    st.session_state["manual_price:TMF"]  = price
                    st.session_state["decided_price:TMF"] = price
                else:
                    price = st.session_state.get("decided_price:TMF") or st.session_state.get("price:TMF")
                if not price:
                    st.warning("TMFの決定価格がありません。Priceタブで取得するか手動価格を使用してください。")
                else:
                    ensure_event_loop()
                    msgs = run_tmf_cc(
                        float(budget_tmf),
                        float(stop_tmf),
                        float(price),
                        is_live(), 
                    )
                    for m in msgs:
                        orders_log.info(m)
                    st.success("TMF CC 完了（ログ参照）")
            except Exception as e:
                st.error(f"TMF 実行エラー: {e}")

    # ===== UVIX =====
    with c3:
        st.markdown("**UVIX**")
        with st.form("form_uvix", clear_on_submit=False):
            budget_uvix = st.number_input("Budget (USD)", min_value=0.0, value=float(os.getenv("BUDGET_UVIX", "300000")), step=1000.0, key="budget_uvix_main")
            manual_price_uvix = st.number_input("Manual price", min_value=0.0, value=10.0, step=0.01, key="manual_price_uvix_main")
            use_manual_uvix = st.checkbox("Use manual price", value=False, key="use_manual_uvix_main")
            submit_uvix_plan = st.form_submit_button("▶ UVIX +15% PUT (Plan)", use_container_width=True)

        if submit_uvix_plan:
            try:
                if use_manual_uvix and manual_price_uvix > 0:
                    price = float(manual_price_uvix)
                    st.session_state["manual_price:UVIX"]  = price
                    st.session_state["decided_price:UVIX"] = price
                else:
                    price = st.session_state.get("decided_price:UVIX") or st.session_state.get("price:UVIX")
                if not price:
                    st.warning("UVIXの決定価格がありません。Priceタブで取得するか手動価格を使用してください。")
                else:
                    msgs = run_uvix_put_idea(float(budget_uvix), float(price))
                    for m in msgs:
                        orders_log.info(m)
                    st.info("UVIX PUT 設計をログに出力しました（次段で発注ヘルパを追加）")
            except Exception as e:
                st.error(f"UVIX 計算エラー: {e}")

    # ===== Connection =====
    with c4:
        cli_tmp = get_client()
        conn = bool(cli_tmp and cli_tmp.ib and cli_tmp.ib.isConnected())
        st.metric("Connection", "Connected" if conn else "Disconnected")
        st.caption("最小操作で実行できる“クイック・ポータル”。詳細は下のタブで確認/採用。")


# Logs の表示可否（サイドバーから操作したいならここをサイドバーにしてOK）
show_logs = st.sidebar.checkbox("Show logs", value=True)

# --- ここからタブ部分 丸ごと置換 ---------------------------------
# Main tabs
tab_price, tab1, tab2, tab3 = st.tabs(["Price (NUGT/TMF/UVIX/VIX)", "Account/Positions", "Open Orders", "Logs"])

with tab_price:
    st.subheader("Price – TWS『直近』相当（last → close）")
    cli = get_client()
    if not cli:
        st.info("未接続です。左サイドバーから接続してください。")
    else:
        symbols = ["NUGT", "TMF", "UVIX", "VIX"]
        col1, col2 = st.columns([1,1])
        with col1:
            if st.button("価格を更新（TWS準拠）", use_container_width=True):
                try:
                    prices = fetch_prices_tws_like(cli.ib, symbols, delay_type=int(md_type), timeout=2.0)
                    for s in symbols:
                        st.session_state[f"price:{s}"] = prices.get(s)
                    st.success("価格更新しました。")
                except Exception as e:
                    log.exception("price fetch error")
                    st.error(f"価格取得エラー: {e}")
        with col2:
            st.caption("TWSのウォッチリスト『直近』に合わせ、last→close の順で採用します。")

        st.divider()
        cols = st.columns(len(symbols))
        for i, s in enumerate(symbols):
            with cols[i]:
                px = st.session_state.get(f"price:{s}")
                st.metric(s, f"{px:.4f}" if px else "—")
                if px:
                    if st.button("採用", key=f"adopt_{s}", use_container_width=True):
                        st.session_state[f"decided_price:{s}"] = float(px)
                        st.success(f"{s} 決定価格を {px:.4f} に設定")


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
    st.caption(f"Global mode: {'LIVE' if is_live() else 'DRY RUN'}")
    dry = not is_live()   # LIVEなら False（実送信）、DRYなら True（試走）


side = st.radio("Action", ["BUY", "SELL"], horizontal=True)

cli = get_client()

if st.button("Place Order"):
    ensure_event_loop()           # ← 追加
    if cli and cli.ib and cli.ib.isConnected():
        result = place_manual_order(cli.ib, manual_symbol, int(qty), action=side, dry_run=dry)
        st.success(f"✅ {side} {manual_symbol} x {qty} ({result['status']})")
    else:
        st.error("❌ IB未接続です。左サイドバーからConnectしてください。")


