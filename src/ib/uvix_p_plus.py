# -*- coding: utf-8 -*-
"""
UVIX P+ state probe
- 現在のUVIX（P+想定＝現物）の保有状況、平均コスト、最新価格、含み損益、未約定オーダーを取得
- .env に UVIX_CONID が存在する前提（例: UVIX_CONID='752090595'）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple, cast

from ib_insync import IB, Contract, Stock, Option, LimitOrder, MarketOrder, StopOrder, Trade
from dotenv import load_dotenv
import math
import os
import uuid

# 気配の中心値（両方finiteのときだけ平均。ダメなら NaN を返す）
def _mid(bid: float | None, ask: float | None) -> float:
    if bid is None or ask is None:
        return float("nan")
    try:
        m = 0.5 * (bid + ask)
        return m if math.isfinite(m) else float("nan")
    except Exception:
        return float("nan")

def first_finite(*vals: Optional[float]) -> Optional[float]:
    for v in vals:
        try:
            if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v)):
                return float(v)
        except Exception:
            pass
    return None

def require_float(name: str, val: Optional[float]) -> float:
    # Optional を先に排除してから cast → float に確定
    if val is None:
        raise RuntimeError(f"{name} が取得できませんでした。前段の購読/スナップショット設定をご確認ください。")
    v = cast(float, val)      # ここで静的に float 確定
    f = float(v)
    if not math.isfinite(f):
        raise RuntimeError(f"{name} が有限値ではありません: {f!r}")
    return f

def ensure_positive(name: str, val: float) -> float:
    if val <= 0:
        raise RuntimeError(f"{name} が正の値ではありません: {val}")
    return val


log = logging.getLogger("uvix_p_plus")
# 自動価格改善のモジュール既定値（.envで上書き可）
DEFAULT_MAX_IMPROVE_TICKS = int(os.getenv("UVIX_MAX_IMPROVE_TICKS", "2"))
DEFAULT_IMPROVE_WAIT_SEC = float(os.getenv("UVIX_IMPROVE_WAIT_SEC", "1.2"))


# 規制スナップショット（NBBO単発取得）を使うかどうか（※口座設定によっては課金あり）
# 既存 IB_USE_REG_SNAPSHOT を尊重しつつ、USE_REG_SNAPSHOT も受け付ける
USE_REG_SNAPSHOT = (
    os.getenv("IB_USE_REG_SNAPSHOT", os.getenv("USE_REG_SNAPSHOT", "0")) == "1"
)

@dataclass
class UVIXPPlusState:
    connected: bool
    conid: int
    symbol: str
    currency: str
    position_qty: float
    avg_cost: float
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    close: Optional[float]
    unrealized_pnl: Optional[float]
    open_orders: List[Tuple[str, float, float, str]]  # [(action, totalQty, lmtPrice/auxPrice, orderType)]

def _mk_stock_from_conid(conid: int) -> Contract:
    # conIdが分かっている場合、余計な解決を避けられる
    c = Contract(conId=conid, secType="STK", exchange="SMART", currency="USD")
    return c

def _fallback_symbol_contract(symbol: str = "UVIX") -> Stock:
    # conId解決に失敗した場合のフォールバック
    return Stock(symbol, "SMART", "USD")

def _num(n) -> Optional[float]:
    try:
        return float(n) if n is not None else None
    except Exception:
        return None

def _is_finite(x):
    return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))

def _is_pos(x) -> bool:
    """有限かつ正（>0）の価格だけを '使える価格' とみなす"""
    try:
        return _is_finite(x) and float(x) > 0.0
    except Exception:
        return False

def _fetch_mark_prices(ib: IB, contract: Contract, wait_sec: float = 1.0):
    """
    last/bid/ask/close をできるだけ埋めるフォールバック取得。
    戻り値: (last, bid, ask, close)
    """
    last = bid = ask = close = None

    # --- 1) 通常ストリーミング
    try:
        t = ib.reqMktData(contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
        ib.sleep(wait_sec)
        last = _num(getattr(t, "last", None) or t.marketPrice())
        bid  = _num(getattr(t, "bid", None))
        ask  = _num(getattr(t, "ask", None))
        close = _num(getattr(t, "close", None))
    except Exception:
        pass

    # --- 2) スナップショット（板購読が無くても入ることがある）
    if not (_is_finite(last) and (_is_finite(bid) or _is_finite(ask))):
        try:
            t = ib.reqMktData(
                contract,
                genericTickList="",
                snapshot=True,
                regulatorySnapshot=USE_REG_SNAPSHOT  # ← .env で 1 にすると使う（課金注意）
            )
            ib.sleep(0.6)
            if not _is_finite(last):
                last = _num(getattr(t, "last", None) or t.marketPrice())
            if not _is_finite(bid):
                bid  = _num(getattr(t, "bid", None))
            if not _is_finite(ask):
                ask  = _num(getattr(t, "ask", None))
            if not _is_finite(close):
                close = _num(getattr(t, "close", None))
        except Exception:
            pass

    # --- 3) ヒストリカル（最終手段）
    if not (_is_finite(last) or _is_finite(close) or _is_finite(bid) or _is_finite(ask)):
        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            if bars:
                close = float(bars[-1].close)
                if not _is_finite(last):
                    last = close
        except Exception:
            pass

    return last, bid, ask, close
def _get_option_min_tick(ib: IB, opt: Contract) -> float:
    """オプションの minTick を取得。失敗時は 0.05 フォールバック。"""
    try:
        cds = ib.reqContractDetails(opt)
        if cds:
            mt = cds[0].minTick
            if mt and mt > 0:
                return float(mt)
    except Exception:
        pass
    return 0.05

def _calc_unrealized(qty: float, avg_cost: float, mark: Optional[float]) -> Optional[float]:
    if qty == 0 or mark is None:
        return None
    # pyrightに Optional を完全排除させる
    mark_f: float = cast(float, mark)
    return (mark_f - avg_cost) * qty

def get_uvix_p_plus_state(
    host: Optional[str] = None,
    port: Optional[int] = None,
    client_id: Optional[int] = None,
    request_mktdata_type: int = 3,  # 3=Delayed, 1=Live, 2=Frozen, 4=Delayed-Frozen
    mktdata_wait_sec: float = 1.0
) -> UVIXPPlusState:
    """
    UVIXの現物（P+前提）状態を取得して返す。
    - host/port/client_id は .env があればそれを使い、無ければ 127.0.0.1:7497 / 10 を用いる
    - リターンには openTrades() を元にした未約定オーダーのサマリも含む
    """
    load_dotenv()
    conid_str = os.getenv("UVIX_CONID", "").strip()
    if not conid_str.isdigit():
        raise RuntimeError("環境変数 UVIX_CONID が見つからないか不正です（例: UVIX_CONID='752090595'）。")
    conid = int(conid_str)

    # ← Optional を確実に str/int に落とす（pyright が Optional を疑わない形に）
    host_s: str = cast(str, host if host is not None else (os.getenv("TWS_HOST") or "127.0.0.1"))
    port_s: str = cast(str, str(port) if port is not None else (os.getenv("TWS_PORT") or "7497"))
    client_s: str = cast(str, str(client_id) if client_id is not None else (os.getenv("TWS_CLIENT_ID") or "10"))
    port_i: int = int(port_s)
    client_i: int = int(client_s)

    ib = IB()
    connected = False
    try:
        ib.connect(host_s, port_i, clientId=client_i, timeout=5)
        connected = ib.isConnected()
    except Exception as e:
        log.exception("TWS/IB接続に失敗しました: %s", e)

    # 返却用の初期値
    state = UVIXPPlusState(
        connected=connected,
        conid=conid,
        symbol="UVIX",
        currency="USD",
        position_qty=0.0,
        avg_cost=0.0,
        last=None, bid=None, ask=None, close=None,
        unrealized_pnl=None,
        open_orders=[],
    )

    if not connected:
        return state

    # Live→Delayed 自動フォールバック（10089/10091系）
    def _set_md(md: int) -> None:
        try:
            ib.reqMarketDataType(md)
        except Exception:
            pass
    _set_md(request_mktdata_type)

    # --- Contract 解決（conId優先、BATSならARCA/NYSE/NASDAQを優先試行）
    def _q(c):
        try:
            return ib.qualifyContracts(c)[0]
        except Exception:
            return None
    qc = _q(_mk_stock_from_conid(conid))
    if qc and getattr(qc, "primaryExchange", "") not in ("BATS","BATSZ"):
        contract = qc
    else:
        contract = None
        for px in ("ARCA","NYSE","NASDAQ"):
            alt = _q(Stock("UVIX","SMART","USD",primaryExchange=px))
            if alt:
                contract = alt
                break
        if contract is None:
            contract = _fallback_symbol_contract("UVIX")
            contract = _q(contract) or contract

    state.symbol = getattr(contract, "symbol", "UVIX") or "UVIX"
    state.currency = getattr(contract, "currency", "USD") or "USD"

    # --- 現在ポジション（portfolio / positions）
    # positions() はアカウント跨ぎの集約、portfolio() は口座に紐づく。ここでは positions() でOK
    try:
        positions = ib.positions()
        for p in positions:
            # p.contract.conId が一致するものを拾う
            if getattr(p.contract, "conId", None) == contract.conId and p.contract.secType == "STK":
                 # NOTE: p.position / p.avgCost は float | None の可能性。明示分岐で float に確定
                pos_raw = getattr(p, "position", None)
                avg_raw = getattr(p, "avgCost", None)
                state.position_qty = float(pos_raw) if isinstance(pos_raw, (int, float)) else 0.0
                state.avg_cost = float(avg_raw) if isinstance(avg_raw, (int, float)) else 0.0
                break
    except Exception as e:
        log.exception("positions() の取得に失敗: %s", e)

    # --- 最新価格（Ticker）: フォールバックで取得、板購読不足（10089等）は黙殺
    try:
        def _squelch_errors(reqId: int, code: int, msg: str, advanced: str | None = None):
            # 10089 系（購読不足）や板関連の注意は検証時は無視
            if code in (10089, 10090, 10091, 10167, 354):
                log.info("[MD] ignore %s reqId=%s msg=%s", code, reqId, msg)
                return
            log.warning("[MD] %s reqId=%s msg=%s", code, reqId, msg)
        ib.errorEvent += _squelch_errors
        try:
            state.last, state.bid, state.ask, state.close = _fetch_mark_prices(
                ib, contract, wait_sec=mktdata_wait_sec
            )
        finally:
            ib.errorEvent -= _squelch_errors
    except Exception as e:
        log.exception("マーケットデータ取得に失敗: %s", e)

    # --- 含み損益
    # NaN安全にマーク価格を選定
    candidates = [state.last, state.close, state.bid, state.ask]
    mark = next((x for x in candidates if _is_finite(x)), None)
    state.unrealized_pnl = _calc_unrealized(state.position_qty, state.avg_cost, mark)

    # --- 未約定オーダー（openTrades）
    try:
        trades = ib.openTrades()
        orders = []
        for tr in trades:
            c = tr.contract
            if getattr(c, "conId", None) == contract.conId and c.secType == "STK":
                o = tr.order
                # Limit/Stopの価格は orderType によって lmtPrice / auxPrice を見分け
                px = o.lmtPrice if o.orderType in ("LMT", "REL", "TRAILLIMIT") else o.auxPrice
                orders.append((o.action, float(o.totalQuantity or 0.0), float(px or 0.0), o.orderType))
        state.open_orders = orders
    except Exception as e:
        log.exception("openTrades() の取得に失敗: %s", e)

    return state
# ======== 発注（BUY / optional STOP） ========


def _ensure_connection(host: str | None, port: int | None, client_id: int | None) -> IB:
    """環境変数を用いて None を解消し、確実に str/int を渡す。"""
    ib = IB()
    # ← Optional を確実に str/int に落とす（pyrightに確実に伝わるよう cast で固定）
    host_s: str = cast(str, host if host is not None else (os.getenv("TWS_HOST") or "127.0.0.1"))
    port_s: str = cast(str, str(port) if port is not None else (os.getenv("TWS_PORT") or "7497"))
    client_s: str = cast(str, str(client_id) if client_id is not None else (os.getenv("TWS_CLIENT_ID") or "10"))
    port_i: int = int(port_s)
    client_i: int = int(client_s)
    if not ib.isConnected():
        # 326(clientId使用中) / Timeout 対策：clientId をずらして最大3回まで試行
        attempts = 0
        cid = client_i
        while attempts < 3 and not ib.isConnected():
            try:
                ib.connect(host_s, port_i, clientId=cid, timeout=5)
            except Exception:
                attempts = 1
                cid = 1  # 次の候補へ
                continue
    return ib

def _qualify_uvix_contract(ib: IB, conid: int) -> Contract:
    # conId 優先、BATS回避のため ARCA/NYSE/NASDAQ も試行
    def _q(c: Contract):
        try:
            return ib.qualifyContracts(c)[0]
        except Exception:
            return None

    c = _mk_stock_from_conid(conid)
    qc = _q(c)
    if qc and getattr(qc, "primaryExchange", "") not in ("BATS", "BATSZ"):
        return qc
    for px in ("ARCA", "NYSE", "NASDAQ"):
        alt = _q(Stock("UVIX", "SMART", "USD", primaryExchange=px))
        if alt:
            return alt
    return _fallback_symbol_contract("UVIX")

def _mark_price_for_entry(ib: IB, contract: Contract, request_mktdata_type: int = 1) -> float:
    try:
        ib.reqMarketDataType(request_mktdata_type)
    except Exception:
        pass
    last, bid, ask, close = _fetch_mark_prices(ib, contract, wait_sec=0.7)
    # 発注用“mark”は、まず気配中心（_mid）が取れれば最優先。無ければ TRADES(=last) → close → ask/bid
    mark = first_finite(_mid(bid, ask), last, close, ask, bid)
    return require_float("UVIXの価格", mark)

def _calc_limit_from_mark(mark: float, side: str, bps: int) -> float:
    # BUY指値は mark に上乗せ、SELL指値は下乗せ（今回はBUYのみ）
    if side.upper() == "BUY":
        raw = mark * (1 + bps / 10000.0)
        lim = round(raw, 2)
        if lim <= mark:
            lim = round(mark + 0.01, 2)  # 最低1セントは上に
        return lim
    return round(mark * (1 - bps / 10000.0), 2)

def place_uvix_p_plus_order(
    qty: int | None = None,
    budget: float | None = None,
    order_type: str = "LMT",
    limit_price: float | None = None,
    limit_from_mark_pct: float | None = None,  # 例: 0.15 → mark×1.15 を指値に
    limit_slippage_bps: int = 5,    # 指値はマーク+5bps
    stop_loss_pct: float | None = None,  # 0.1 = 10% 下でSTP
    tif: str = "DAY",
    account: str | None = None,
    request_mktdata_type: int | None = None,
    dry_run: bool = True,
    # ここから追記：自動改善の上限・待機
    max_improve_ticks: int = DEFAULT_MAX_IMPROVE_TICKS,
    improve_wait_sec: float = DEFAULT_IMPROVE_WAIT_SEC,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
) -> dict:
    """
    UVIX 現物を BUY。必要に応じて STP を子注文で添付（親子/OCA）。
    戻り値: { 'parentOrderId': int, 'childrenOrderIds': [int, ...], 'qty': int, 'limit': float|None, 'stop': float|None }
    """
    load_dotenv()
    conid = int(os.getenv("UVIX_CONID", "0") or 0)
    if not conid:
        raise RuntimeError("UVIX_CONID が .env にありません。例: UVIX_CONID='752090595'")

    # 既定の値を .env から
    if request_mktdata_type is None:
        request_mktdata_type = int(os.getenv("IB_MKT_TYPE", "1"))
    if budget is None:
        budget = float(os.getenv("UVIX_DEFAULT_BUDGET", "60000"))
    if stop_loss_pct is None:
        env_stp = os.getenv("UVIX_STOP_PCT", "")
        stop_loss_pct = float(env_stp) if env_stp.strip() != "" else None
    if account is None:
        account = os.getenv("ORDER_ACCOUNT", None)

    ib = _ensure_connection(host, port, client_id)
    contract = _qualify_uvix_contract(ib, conid)

    # マーク価格（float 確定）
    mark: float = _mark_price_for_entry(ib, contract, request_mktdata_type=request_mktdata_type)

    # 指値（LMT時のみ有効）
    ot = (order_type or "LMT").upper()
    limit_v_opt: Optional[float] = None
    # LMT の最終確定 float（Optional を残さない）
    limit_v_f: float = 0.0
    if ot == "LMT":
        # Optional で候補を作る
        if limit_price is not None:
            limit_v_opt = float(limit_price)
        elif limit_from_mark_pct is not None:
            limit_v_opt = round(mark * (1 + float(limit_from_mark_pct)), 2)
        else:
            limit_v_opt = _calc_limit_from_mark(mark, "BUY", limit_slippage_bps)
        # ここで “非Optionalの確定 float” を作る（別名に固定）
        limit_v_f = ensure_positive("limit", require_float("limit", limit_v_opt))
    elif ot == "MKT":
        pass  # 指値なし
    else:
        raise ValueError("order_type は 'MKT' または 'LMT' を指定してください。")

    # 数量の決定（LMT のときは “その指値” を基準に算出）
    if qty is None:
        # Optional をここで完全に排除して Pyright に確実に伝える
        if budget is None or budget <= 0:
            raise ValueError("qty か budget のどちらかは必要です。")
        # 以降は float に固定して使う
        budget_f: float = float(budget)
        if ot == "LMT":
            # LMT の基準価格は “確定 float” の limit_v_f
            ref_px: float = limit_v_f
        else:
            ref_px = mark
        qty = int(max(1, budget_f // ref_px))
    # pyright向けに qty をintへ固定
    if not isinstance(qty, int):
        qty = int(qty or 0)
        if qty <= 0:
            raise RuntimeError("internal: qty must be positive int")
    # 親注文（BUY）
    if ot == "MKT":
        parent = MarketOrder("BUY", qty, tif=tif, account=account)
    else:
        # LMT：確定 float をそのまま渡す（Optional 不使用）
        parent = LimitOrder("BUY", qty, limit_v_f, tif=tif, account=account)

    # 子注文（STOP 売り）
    children = []
    stop_px = None
    if stop_loss_pct is not None and stop_loss_pct > 0:
        # NOTE: stop_loss_pct は Optional[float]。ガード内で float に固定してから使用
        stp_f: float = float(stop_loss_pct)
        one_minus: float = 1.0 - stp_f  # 演算片側がOptionalに見える誤判定の抑止
        stop_px = round(mark * one_minus, 2)
        stp = StopOrder("SELL", qty, stop_px, tif="GTC", account=account)
        children.append(stp)

    # OCA / transmit 設計：親→子(…最後True)
    oca = f"OCA_UVIX_P_PLUS_{uuid.uuid4().hex[:8]}"
    # 親は transmit=False（子がある場合）
    if children:
        parent.transmit = False
        for ch in children:
            ch.parentId = 0  # placeOrder 時に上書きされるので 0/None でOK
            ch.ocaGroup = oca
        children[-1].transmit = True
    else:
        parent.transmit = True

    if dry_run:
        limit_ret: Optional[float] = (limit_v_f if ot == "LMT" else None)
        return {
            "dry_run": True,
            "qty": qty,
            "limit": limit_ret,
            "stop": stop_px,
            "tif": tif,
            "account": account,
            "mark": mark,
            "contract": {"conId": contract.conId, "symbol": contract.symbol, "primary": getattr(contract, "primaryExchange", "")},
        }

    # 実発注
    trades: list[Trade] = []
    tr_parent = ib.placeOrder(contract, parent)
    trades.append(tr_parent)
    ib.sleep(0.1)

    if children:
        # 親の orderId/permId が採番された後に parentId を反映して送る
        parent_id = tr_parent.order.orderId
        for i, ch in enumerate(children):
            ch.parentId = parent_id
            # 最後の子以外は transmit=False、最後の子が True
            ch.transmit = (i == len(children) - 1)
            tr_child = ib.placeOrder(contract, ch)
            trades.append(tr_child)
    limit_ret2: Optional[float] = (limit_v_f if ot == "LMT" else None)
    return {
        "dry_run": False,
        "qty": qty,
        "limit": limit_ret2,
        "stop": stop_px,
        "tif": tif,
        "account": account,
        "mark": mark,
        "parentOrderId": trades[0].order.orderId,
        "childrenOrderIds": [t.order.orderId for t in trades[1:]],
        "contract": {"conId": contract.conId, "symbol": contract.symbol, "primary": getattr(contract, "primaryExchange", "")},
    }

# ======== UVIX PUT ロング（P+）: ATM +% のストライク、予算で口数自動、STOPなし、LMT ========
def place_uvix_put_plus_order(
    moneyness_pct: float = 0.15,     # ATM +15%（= 現値×1.15）を目安にストライク選定
    budget: float = 60000,           # 予算（USD）
    tif: str = "DAY",
    account: Optional[str] = None,
    request_mktdata_type: int = 1,   # 1=Live, 3=Delayed
    strike_increment: float = 0.5,   # UVIXの一般的な刻み
    price_improve: float = 0.01,
    expiry: Optional[str] = None,
    strike: Optional[float] = None,        # ← 明示ストライク（例: 12.0）を優先
    strike_round: str = "ceil",            # 'ceil'（推奨）/ 'nearest'
    min_premium: Optional[float] = None,   # 最低プレミアム（既定は .env or 0.05）
    max_contracts: Optional[int] = None,   # 最大枚数（既定は .env or 300）
    dry_run: bool = True,
    host: Optional[str] = None,
    port: Optional[int] = None,
    client_id: Optional[int] = None,
    # ▼ 自動価格改善（LIVE時のみ使用）
    max_improve_ticks: int = DEFAULT_MAX_IMPROVE_TICKS,
    improve_wait_sec: float = DEFAULT_IMPROVE_WAIT_SEC,
) -> dict:
    
    # マーケットデータ種別（Live→Delayed フォールバック用の小関数）
    def _set_md(md: int) -> None:
        try:
            ib.reqMarketDataType(md)
        except Exception:
            pass
    _set_md(request_mktdata_type)

    """
    1) 基礎価格（UVIX現物の mark）を取得
    2) 目標ストライク = mark * (1 + moneyness_pct) を刻みに丸め
    3) もっとも近い満期（最短の上場日）を選ぶ
    4) そのPUTの板から MID を取り、買い指値 = min(ASK, max(BID, MID - price_improve))
    5) 予算 / (指値 × 100) で口数（枚数）を算出
    6) LMT 買いを送信（STOPなし）
    """
    load_dotenv()
    conid = int(os.getenv("UVIX_CONID", "0") or 0)
    if not conid:
        raise RuntimeError("UVIX_CONID が .env にありません。例: UVIX_CONID='752090595'")
    if account is None:
        account = os.getenv("ORDER_ACCOUNT", None)
    if min_premium is None:
        min_premium = float(os.getenv("MIN_OPTION_PREMIUM", "0.05"))
    else:
        min_premium = float(min_premium)
    if max_contracts is None:
        max_contracts = int(os.getenv("UVIX_OPT_MAX_CONTRACTS", "300"))
    else:
        max_contracts = int(max_contracts)

    # 接続と現物契約
    ib = _ensure_connection(host, port, client_id)
    underlying = _qualify_uvix_contract(ib, conid)



    # 基礎価格（現値）: float 確定
    mark: float = _mark_price_for_entry(ib, underlying, request_mktdata_type=request_mktdata_type)

    # --- オプション仕様の取得
    # 正しい引数順: (symbol, futFopExchange, underlyingSecType, underlyingConId)
    params = ib.reqSecDefOptParams(underlying.symbol, "", underlying.secType, underlying.conId)
    if not params:
        raise RuntimeError("UVIX のオプション仕様が取得できませんでした。")
    # SMART優先 / 期限が多いチェーン優先で選ぶ
    candidates = [p for p in params if getattr(p, "exchange", "") in ("SMART", "")]
    p0 = max(candidates or params, key=lambda p: len(getattr(p, "expirations", [])))

    # 期限・ストライク集合
    expirations = sorted(p0.expirations)  # 'YYYYMMDD' の文字列
    strikes = sorted(float(s) for s in p0.strikes if isinstance(s, (int, float)))
    if not expirations or not strikes:
        raise RuntimeError("UVIX の有効な満期/ストライクが見つかりません。")

    # strike を Optional のまま使わず、ここで float に確定
    if strike is not None:
        s0 = float(strike)
        if s0 not in strikes:
            ge = [s for s in strikes if s >= s0]
            s0 = (min(ge) if ge else min(strikes, key=lambda x: abs(x - s0)))
        strike_f: float = float(s0)
    else:
        target_raw = mark * (1 + float(moneyness_pct))
        eps = 1e-9
        if strike_round.lower() == "ceil":
            ge = [s for s in strikes if s + eps >= target_raw]
            strike_f = float(min(ge) if ge else max(strikes))
        else:
            strike_f = float(min(strikes, key=lambda x: abs(x - target_raw)))

    # 満期：指定があればそれ、なければ最短
    if expiry:
        if expiry not in expirations:
            raise RuntimeError(f"指定満期 {expiry} が上場一覧に見つかりません: {expirations[:6]} ...")
    else:
        expiry = expirations[0]

    # 契約を構築＆解決
    opt = Option(
        symbol=underlying.symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=strike_f,
        right="P",
        exchange="SMART",
        currency="USD",
        tradingClass=underlying.symbol
    )
    try:
        opt = ib.qualifyContracts(opt)[0]
    except Exception as e:
        raise RuntimeError(f"オプション契約の解決に失敗: {e}")

    def _opt_snap(reg: bool = False, wait: float = 0.7):
        t = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=reg)
        ib.sleep(wait)
        return t

    def _opt_num_pos(x):
        try:
            v = float(x) if x is not None else None
            return v if (v is not None and v > 0) else None
        except Exception:
            return None

    # === 見積り取得ユーティリティ ===
    def _snap(reg: bool, wait: float = 0.7):
        t = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=reg)
        ib.sleep(wait)
        return t

    def _num_pos(x):
        try:
            v = float(x) if x is not None else None
            return v if (v is not None and math.isfinite(v) and v > 0.0) else None
        except Exception:
            return None

    # 1st: 現在のmdで通常snap
    bid = ask = last = None
    try:
        t = _opt_snap(reg=False)
        bid = _opt_num_pos(getattr(t,"bid",None))
        ask = _opt_num_pos(getattr(t,"ask",None))
        last = _opt_num_pos(getattr(t,"last",None) or t.marketPrice())
    except Exception:
        pass

    # 2nd: 規制snap（NBBO）
    if not (bid or ask or last):
        try:
            t = _opt_snap(reg=True)
            bid = bid or _opt_num_pos(getattr(t,"bid",None))
            ask = ask or _opt_num_pos(getattr(t,"ask",None))
            last = last or _opt_num_pos(getattr(t,"last",None) or t.marketPrice())
        except Exception:
            pass

    # 3rd: Live/Frozen 指定で空 → Delayed(3)へフォールバック（通常→規制）
    if request_mktdata_type in (1, 2) and not (bid or ask or last):
        _set_md(3)
        try:
            t = _opt_snap(reg=False)
            bid = bid or _opt_num_pos(getattr(t,"bid",None))
            ask = ask or _opt_num_pos(getattr(t,"ask",None))
            last = last or _opt_num_pos(getattr(t,"last",None) or t.marketPrice())
        except Exception:
            pass
        if not (bid or ask or last):
            try:
                t = _opt_snap(reg=True)
                bid = bid or _opt_num_pos(getattr(t,"bid",None))
                ask = ask or _opt_num_pos(getattr(t,"ask",None))
                last = last or _opt_num_pos(getattr(t,"last",None) or t.marketPrice())
            except Exception:
                pass

    # MID を計算（正値のみ）
    mid: Optional[float] = None
    if isinstance(bid, float) and isinstance(ask, float):
        mid = round((bid + ask) / 2.0, 2)
    elif isinstance(last, float):
        mid = round(last, 2)

    # Buy は ASK を最優先（約定性重視）。次に MID-改善、BID、LAST の順。
    limit_opt: Optional[float] = None
    chosen_source = "unknown"
    if isinstance(ask, float):
        limit_opt = ask
        chosen_source = "ask"
    elif isinstance(mid, float):
        mid_f = cast(float, mid)
        pi_f = float(price_improve)
        lim_tmp = round(mid_f - pi_f, 2)
        if isinstance(bid, float):
            lim_tmp = max(lim_tmp, bid)  # 下限をBIDにクランプ
        limit_opt = lim_tmp
        chosen_source = "mid"
    elif isinstance(bid, float):
        limit_opt = bid
        chosen_source = "bid"
    elif isinstance(last, float):
        limit_opt = last
        chosen_source = "last"
    else:
        # ここまでで取得できなければ DRY 継続のため最小限の数値を返す
        limit_opt = float(min_premium)
        chosen_source = "min_premium_fallback"

    # ここで float に確定し、正の値＆最低プレミアムを保証
    limit_v: float = ensure_positive("limit", require_float("limit", limit_opt))
    if limit_v < float(min_premium):
        raise RuntimeError(
            f"算出指値 {limit_v:.2f} が最低プレミアム {float(min_premium):.2f} 未満のため中止しました。"
        )

    # 口数（枚数）= 予算 / (指値 × 100)
    qty = int(max(1, budget // (limit_v * 100.0)))
    if qty > int(max_contracts):
        qty = int(max_contracts)

    # 親注文（LMT BUY、STOPなし）
    parent = LimitOrder("BUY", qty, limit_v, tif=tif, account=account)
    parent.transmit = True

    if dry_run:
        return {
            "dry_run": True,
            "underlying_mark": mark,
            "expiry": expiry,
            "strike": strike_f,
            "right": "P",
            "limit": limit_v,
            "qty": qty,
            "tif": tif,
            "account": account,
            "opt_contract": {"symbol": opt.symbol, "expiry": opt.lastTradeDateOrContractMonth, "strike": opt.strike, "right": opt.right},
            # DryRun 要約に板も返す
            "bid": bid, "ask": ask, "mid": mid, "limit_source": chosen_source,
        }

    # ===== ここから LIVE 時の自動 price_improve（attempt 0..max_improve_ticks）=====
    tick = _get_option_min_tick(ib, opt)
    trade = ib.placeOrder(opt, parent)
    ib.sleep(0.1)

    attempt = 0
    # 以後の演算で Optional を避ける
    last_limit: float = limit_v
    while attempt < int(max_improve_ticks):
        # Filled か Cancel なら終了
        status = (trade.orderStatus.status or "").capitalize()
        if status in ("Filled", "ApiCancelled"):
            break
        ib.sleep(float(improve_wait_sec))
        status = (trade.orderStatus.status or "").capitalize()
        if status in ("Filled", "ApiCancelled"):
            break
        # 改善：BUY は ASK ベースで tick 上げ、板が無ければ直前指値ベース
        attempt += 1
        base: float = float(ask) if isinstance(ask, float) else last_limit
        new_limit = round(base + attempt * float(tick), 2)
        log.info("[IMPROVE] attempt=%d tick=%.4f last=%.2f → new_limit=%.2f", attempt, tick, last_limit, new_limit)
        ib.cancelOrder(trade.order)
        ib.sleep(0.3)
        parent = LimitOrder("BUY", qty, new_limit, tif=tif, account=account)
        parent.transmit = True
        trade = ib.placeOrder(opt, parent)
        last_limit = new_limit

    return {
        "dry_run": False,
        "underlying_mark": mark,
        "expiry": expiry,
        "strike": strike_f,
        "right": "P",
        "limit": last_limit,
        "qty": qty,
        "tif": tif,
        "account": account,
        "orderId": trade.order.orderId,
        "status": trade.orderStatus.status,
        "improve_attempts": attempt,
        "tick": tick,
        "opt_contract": {"symbol": opt.symbol, "expiry": opt.lastTradeDateOrContractMonth, "strike": opt.strike, "right": opt.right},
    }

# ======== 追加：PUT+ の事前計画（DryRun専用） ========
def plan_uvix_put_plus(
    moneyness_pct: float = 0.15,
    budget: float = 60000,
    request_mktdata_type: int = 1,
    expiry: Optional[str] = None,
    strike: Optional[float] = None,
    strike_round: str = "ceil",
    price_improve: float = 0.01,
    max_improve_ticks: int = DEFAULT_MAX_IMPROVE_TICKS,
    improve_wait_sec: float = DEFAULT_IMPROVE_WAIT_SEC,
    host: Optional[str] = None,
    port: Optional[int] = None,
    client_id: Optional[int] = None,
) -> dict:
    """実発注せず、place_uvix_put_plus_order の DryRun と同等サマリを返す軽量API。"""
    return place_uvix_put_plus_order(
        moneyness_pct=moneyness_pct,
        budget=budget,
        tif="DAY",
        account=None,
        request_mktdata_type=request_mktdata_type,
        strike_increment=0.5,
        price_improve=price_improve,
        expiry=expiry,
        strike=strike,
        strike_round=strike_round,
        min_premium=None,
        max_contracts=None,
        dry_run=True,
        host=host,
        port=port,
        client_id=client_id,
        max_improve_ticks=max_improve_ticks,
        improve_wait_sec=improve_wait_sec,
    )

# ======== 追加：通常 vs 規制スナップショットの比較 ========
def compare_quote_sources_opt(ib: IB, opt: Contract) -> None:
    """同一オプションを通常snapとregulatory snapで見積比較し、差分をINFOログに出す。"""
    def _num2(x):
        try:
            return float(x) if x is not None and not math.isnan(float(x)) else None
        except Exception:
            return None

    t1 = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=False)
    ib.sleep(0.7)
    n = dict(bid=_num2(getattr(t1,"bid",None)), ask=_num2(getattr(t1,"ask",None)), last=_num2(getattr(t1,"last",None) or t1.marketPrice()))

    t2 = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=True)
    ib.sleep(0.7)
    r = dict(bid=_num2(getattr(t2,"bid",None)), ask=_num2(getattr(t2,"ask",None)), last=_num2(getattr(t2,"last",None) or t2.marketPrice()))

    log.info("[QUOTE/normal] %s", n)
    log.info("[QUOTE/snap  ] %s", r)
    def _fmt(x): return "None" if x is None else f"{x:.2f}"
    log.info("[DIFF] bid: %s → %s / ask: %s → %s / last: %s → %s",
             _fmt(n['bid']), _fmt(r['bid']),
             _fmt(n['ask']), _fmt(r['ask']),
             _fmt(n['last']), _fmt(r['last']))