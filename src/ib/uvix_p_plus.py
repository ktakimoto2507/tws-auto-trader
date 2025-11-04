# -*- coding: utf-8 -*-
"""
UVIX P+ state probe
- 現在のUVIX（P+想定＝現物）の保有状況、平均コスト、最新価格、含み損益、未約定オーダーを取得
- .env に UVIX_CONID が存在する前提（例: UVIX_CONID='752090595'）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple

from ib_insync import IB, Contract, Stock, Option, LimitOrder, MarketOrder, StopOrder, Trade
from dotenv import load_dotenv
import math
import os
import uuid


log = logging.getLogger("uvix_p_plus")


# 規制スナップショット（NBBO単発取得）を使うかどうか（※口座設定によっては課金あり）
USE_REG_SNAPSHOT = os.getenv("IB_USE_REG_SNAPSHOT", "0") == "1"

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

def _round_to_increment(x: float, inc: float) -> float:
    """x を inc 刻みに四捨五入（例: 0.5 刻み）"""
    return round(round(x / inc) * inc, 2)

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

def _calc_unrealized(qty: float, avg_cost: float, mark: Optional[float]) -> Optional[float]:
    if qty == 0 or avg_cost is None or mark is None:
        return None
    # qtyはロングで正、ショートで負
    return (mark - avg_cost) * qty

def get_uvix_p_plus_state(
    host: str = None,
    port: int = None,
    client_id: int = None,
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

    host = host or os.getenv("TWS_HOST", "127.0.0.1")
    port = port or int(os.getenv("TWS_PORT", "7497"))
    client_id = client_id or int(os.getenv("TWS_CLIENT_ID", "10"))

    ib = IB()
    connected = False
    try:
        ib.connect(host, port, clientId=client_id, timeout=5)
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

    try:
        ib.reqMarketDataType(request_mktdata_type)
    except Exception:
        pass

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
                state.position_qty = float(p.position or 0.0)
                state.avg_cost = float(p.avgCost or 0.0)
                break
    except Exception as e:
        log.exception("positions() の取得に失敗: %s", e)

    # --- 最新価格（Ticker）: フォールバックで取得、板購読なしの警告は黙殺
    try:
        def _squelch_errors(err):
            if getattr(err, "code", None) in (10089, 10090, 10091, 10167, 354):
                return  # 板購読系の注意は無視
            log.warning("IB error %s: %s", getattr(err, "code", "?"), getattr(err, "errorMsg", err))
        ib.errorEvent += _squelch_errors
        try:
            state.last, state.bid, state.ask, state.close = _fetch_mark_prices(ib, contract, wait_sec=mktdata_wait_sec)
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
    ib = IB()
    host = host or os.getenv("TWS_HOST", "127.0.0.1")
    port = port or int(os.getenv("TWS_PORT", "7497"))
    client_id = client_id or int(os.getenv("TWS_CLIENT_ID", "10"))
    if not ib.isConnected():
        ib.connect(host, port, clientId=client_id, timeout=5)
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
    # 発注用の“マーク”は、ask→last→close→bid の順で選択
    for x in (ask, last, close, bid):
        if _is_finite(x):
            return float(x)
    raise RuntimeError("UVIXの価格が取得できませんでした（購読権限または接続を確認）")

def _calc_limit_from_mark(mark: float, side: str, bps: int) -> float:
    # BUY指値は mark に上乗せ、SELL指値は下乗せ（今回はBUYのみ）
    if side.upper() == "BUY":
        return round(mark * (1 + bps / 10000.0), 2)
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

    # マーク価格
    mark = _mark_price_for_entry(ib, contract, request_mktdata_type=request_mktdata_type)

    # 指値の決定
    if order_type.upper() == "LMT":
        if limit_price is not None:
            limit = float(limit_price)
        elif limit_from_mark_pct is not None:
            limit = round(mark * (1 + float(limit_from_mark_pct)), 2)
        else:
            limit = _calc_limit_from_mark(mark, "BUY", limit_slippage_bps)
    elif order_type.upper() == "MKT":
        limit = None
    else:
        raise ValueError("order_type は 'MKT' または 'LMT' を指定してください。")

    # 数量の決定（LMT のときは “その指値” を基準に算出）
    if qty is None:
        if budget is None or budget <= 0:
            raise ValueError("qty か budget のどちらかは必要です。")
        ref_px = (limit if order_type.upper() == "LMT" and limit is not None else mark)
        qty = int(max(1, budget // ref_px))

    # 親注文（BUY）
    if order_type.upper() == "MKT":
        parent = MarketOrder("BUY", qty, tif=tif, account=account)
    else:
        parent = LimitOrder("BUY", qty, limit, tif=tif, account=account)

    # 子注文（STOP 売り）
    children = []
    stop_px = None
    if stop_loss_pct is not None and stop_loss_pct > 0:
        stop_px = round(mark * (1 - stop_loss_pct), 2)
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
        return {
            "dry_run": True,
            "qty": qty,
            "limit": limit,
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

    return {
        "dry_run": False,
        "qty": qty,
        "limit": limit,
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
) -> dict:
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
    min_premium = float(os.getenv("MIN_OPTION_PREMIUM", "0.05"))
    max_contracts = int(os.getenv("UVIX_OPT_MAX_CONTRACTS", "300"))
    if min_premium is None:
        min_premium = float(os.getenv("MIN_OPTION_PREMIUM", "0.05"))
    if max_contracts is None:
        max_contracts = int(os.getenv("UVIX_OPT_MAX_CONTRACTS", "300"))

    # 接続と現物契約
    ib = _ensure_connection(host, port, client_id)
    underlying = _qualify_uvix_contract(ib, conid)

    # マーケットデータ種別
    try:
        ib.reqMarketDataType(request_mktdata_type)
    except Exception:
        pass

    # 基礎価格（現値）
    mark = _mark_price_for_entry(ib, underlying, request_mktdata_type=request_mktdata_type)

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

    # strike を明示指定されたらそれを最優先（チェーンに無ければ上側へ寄せる）
    if strike is not None:
        if strike not in strikes:
            ge = [s for s in strikes if s >= strike]
            strike = (min(ge) if ge else min(strikes, key=lambda s: abs(s - strike)))
    else:
        target_raw = mark * (1 + float(moneyness_pct))
        eps = 1e-9  # 浮動小数点誤差対策
        if strike_round.lower() == "ceil":
            ge = [s for s in strikes if s + eps >= target_raw]
            strike = (min(ge) if ge else max(strikes))
        else:
            strike = min(strikes, key=lambda s: abs(s - target_raw))

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
        strike=strike,
        right="P",
        exchange="SMART",
        currency="USD",
        tradingClass=underlying.symbol
    )
    try:
        opt = ib.qualifyContracts(opt)[0]
    except Exception as e:
        raise RuntimeError(f"オプション契約の解決に失敗: {e}")

    # 1st try: 通常スナップショット（正の価格のみ採用）
    t = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=False)
    ib.sleep(0.7)
    bid = _num(getattr(t, "bid", None))
    bid = bid if _is_pos(bid) else None
    ask = _num(getattr(t, "ask", None))
    ask = ask if _is_pos(ask) else None
    last = _num(getattr(t, "last", None) or t.marketPrice())
    last = last if _is_pos(last) else None
    # 2nd try: NBBO 単発（USE_REG_SNAPSHOT=1 のとき）
    if not (bid or ask or last) and USE_REG_SNAPSHOT:
        t = ib.reqMktData(opt, "", snapshot=True, regulatorySnapshot=True)
        ib.sleep(0.7)
        b2 = _num(getattr(t, "bid", None))
        b2 = b2 if _is_pos(b2) else None
        a2 = _num(getattr(t, "ask", None))
        a2 = a2 if _is_pos(a2) else None
        l2 = _num(getattr(t, "last", None) or t.marketPrice())
        l2 = l2 if _is_pos(l2) else None
        bid = bid or b2
        ask = ask or a2
        last = last or l2

    # MID を計算（正値のみ）
    mid = None
    if _is_pos(bid) and _is_pos(ask):
        mid = round((bid + ask) / 2.0, 2)
    elif _is_pos(last):
        mid = round(last, 2)

    # Buy は ASK を最優先（約定性重視）。次に MID-改善、BID、LAST の順。
    if _is_pos(ask):
        limit = float(ask)
    elif _is_pos(mid):
        limit = round(mid - float(price_improve), 2)
        if _is_pos(bid):
            limit = max(limit, float(bid))  # 下限をBIDにクランプ
    elif _is_pos(bid):
        limit = float(bid)
    elif _is_pos(last):
        limit = float(last)
    else:
        raise RuntimeError("オプション価格が取得できません（板/購読権限をご確認ください）。")

    # 最低プレミアムの下限を適用（0.01暴走防止）
    if not _is_pos(limit) or float(limit) < float(min_premium):
        raise RuntimeError(f"算出指値 {limit:.2f} が最低プレミアム {min_premium:.2f} 未満のため中止しました。")

    # 口数（枚数）= 予算 / (指値 × 100)
    qty = int(max(1, budget // (limit * 100.0)))
    if qty > int(max_contracts):
        qty = int(max_contracts)

    # 親注文（LMT BUY、STOPなし）
    parent = LimitOrder("BUY", qty, limit, tif=tif, account=account)
    parent.transmit = True

    if dry_run:
        return {
            "dry_run": True,
            "underlying_mark": mark,
            "expiry": expiry,
            "strike": strike,
            "right": "P",
            "limit": limit,
            "qty": qty,
            "tif": tif,
            "account": account,
            "opt_contract": {"symbol": opt.symbol, "expiry": opt.lastTradeDateOrContractMonth, "strike": opt.strike, "right": opt.right},
        }

    tr = ib.placeOrder(opt, parent)
    return {
        "dry_run": False,
        "underlying_mark": mark,
        "expiry": expiry,
        "strike": strike,
        "right": "P",
        "limit": limit,
        "qty": qty,
        "tif": tif,
        "account": account,
        "orderId": tr.order.orderId,
        "opt_contract": {"symbol": opt.symbol, "expiry": opt.lastTradeDateOrContractMonth, "strike": opt.strike, "right": opt.right},
    }