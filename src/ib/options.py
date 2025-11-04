# src/ib/options.py ーーー 完全版（置き換え）

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date
from typing import Literal, Optional, Tuple, List

from zoneinfo import ZoneInfo
from ib_insync import IB, Option, Contract, Stock, ContractDetails, util
import re
import os
from ..utils.logger import get_logger

log = get_logger("options")

Right = Literal["C", "P"]

#
# === Fast/Deterministic: 決め打ちで最短に撃つためのユーティリティ ===
#   使い方の例は本ファイル末尾の buy_option 変更点の後に記載
#
@dataclass(frozen=True)
class ManualOptionSpec:
    """
    決め打ち入力。優先順: conId > localSymbol > (symbol, expiry_yyyymmdd, strike, right)
    exchange/currency は SMART/USD を既定にする。
    """
    conId: Optional[int] = None
    localSymbol: Optional[str] = None
    symbol: str = "UVIX"
    expiry_yyyymmdd: Optional[str] = None
    strike: Optional[float] = None
    right: Optional[str] = None  # "P" or "C"
    exchange: str = "SMART"
    currency: str = "USD"

def resolve_option_fast(ib: IB, spec: ManualOptionSpec, *, try_qualify_sec: float = 4.0) -> Option:
    """
    決め打ちで Option を最短構築。
      1) conId があれば Option(conId=...) で組み立て
      2) localSymbol があればそれで組み立て
      3) それ以外は (symbol, expiry, strike, right) で組み立て
    可能なら短時間だけ qualify を“試す”。失敗しても opt は返す（発注時に TWS 側で解決）。
    """
    if spec.conId:
        opt = Option(conId=int(spec.conId), exchange=spec.exchange, currency=spec.currency)
    elif spec.localSymbol:
        opt = Option(localSymbol=spec.localSymbol, exchange=spec.exchange, currency=spec.currency)
    else:
        if not (spec.expiry_yyyymmdd and spec.strike is not None and spec.right):
            raise ValueError("resolve_option_fast: need (expiry_yyyymmdd, strike, right) or conId/localSymbol")
        opt = Option(
            spec.symbol,
            spec.expiry_yyyymmdd,
            float(spec.strike),
            spec.right,
            spec.exchange,
            spec.currency,
        )

    # qualify を短時間だけ“試す”（失敗しても OK）
    saved_to = getattr(ib, "RequestTimeout", None)
    try:
        if saved_to is not None:
            ib.RequestTimeout = max(1, int(try_qualify_sec))
        try:
            ib.qualifyContracts(opt)
        except Exception as e:
            log.info(f"[FAST] qualify skip (TWSに解決委任): {e!r}")
    finally:
        if saved_to is not None:
            ib.RequestTimeout = saved_to
    return opt

def _fmt_opt_label(opt: Option) -> str:
    """ログ用の読みやすい表記に整形"""
    sym = getattr(opt, "symbol", "")
    yyyymmdd = getattr(opt, "lastTradeDateOrContractMonth", "")
    right = getattr(opt, "right", "")
    strike = getattr(opt, "strike", "")
    return f"{sym} {yyyymmdd} {strike}{right}"

@dataclass(frozen=True)
class Underlying:
    symbol: str
    exchange: str = "SMART"
    currency: str = "USD"


def _underlying_price(ib: IB, und: Underlying) -> float:
    """
    現在値のロバスト取得:
      - ライブ購読が無ければ遅延データ(3)を要求
      - last → (bid+ask)/2 → close → marketPrice の優先順位で価格を決定
    """
    import math

    # 遅延データ許可（ライブが無い環境でも数字が入る）
    try:
        ib.reqMarketDataType(3)  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
    except Exception:
        pass

    contract = Stock(und.symbol, und.exchange, und.currency)
    # snapshot=True にすると一回分を返す（高速）。ただし遅延環境では埋まりが遅い事があるため短い待ちをループで。
    ticker = ib.reqMktData(contract, "", True, False)
    for _ in range(24):  # ≈1.2秒
        if getattr(ticker, "last", None) or ticker.marketPrice():
            break
        ib.sleep(0.05)

    # 候補を順に拾う
    candidates = [
        getattr(ticker, "last", None),
        None if (getattr(ticker, "bid", None) is None or getattr(ticker, "ask", None) is None)
        else (ticker.bid + ticker.ask) / 2,
        getattr(ticker, "close", None),
        ticker.marketPrice()  # ib_insyncの便利関数（NaNのこともある）
    ]

    # キャンセル（念のため）
    ib.cancelMktData(contract)

    for px in candidates:
        if px is not None and math.isfinite(px) and px > 0:
            return float(px)

    raise RuntimeError(f"Cannot get market price for {und.symbol} (no live/delayed data)")

#
# ---- ここからUVIX向けの堅牢化ヘルパーを追加 ----
#
def ensure_delayed_md(ib: IB, prefer_live: bool = True) -> int:
    """
    prefer_live=Trueでも権限で弾かれたら即座に Delayed(3) へ切替。
    戻り値: 実際に設定された MDType (1=Live, 3=Delayed)
    """
    try:
        if prefer_live:
            ib.reqMarketDataType(1)
            log.info("[UVIX] MDType set to LIVE(1)")
            return 1
    except Exception as e:
        log.info(f"[UVIX] Live MD not permitted: {e!r}")
    ib.reqMarketDataType(3)
    log.info("[UVIX] MDType set to DELAYED(3)")
    return 3

def get_uvix_stock(ib: IB) -> Stock:
    """
    UVIX 現物を確実に特定する（遅延/無応答でも待ち続けない）。
    0) まず UVIX_CONID があればそれを使う（最も堅牢）
    1) reqMatchingSymbols は“試すだけ”（タイムアウトなら即スキップ）
    2) 直接 exch を変えて reqContractDetails を短いタイムアウトで叩く
    """
    # --- 0) conId固定の高速ルート（PaperでもTimeoutしない） ---
    env_conid = os.getenv("UVIX_CONID")
    if env_conid:
        try:
            conid = int(env_conid)
            log.info(f"[UVIX] using fixed conId={conid} (fast path)")
            # reqContractDetails は叩かず、直接 Stock を返す
            return Stock(conId=conid, symbol="UVIX", exchange="SMART", currency="USD")
        except Exception as e:
            log.info(f"[UVIX] fixed conId path failed: {e!r} -> fallback")
    exchs: list[str] = []
    try:
        matches = ib.reqMatchingSymbols("UVIX")
        if matches:
            exchs = [m.derivatives[0].exchange for m in matches if m.derivatives]
    except Exception:
        # ここで待たない（サーバ応答が弱い環境がある）
        pass
    log.info(f"[UVIX] matchingSymbols exchanges={exchs or '[]'}")

    candidates = [
        # 直接指定（primaryExchange 明示 or 直にその取引所）
        Stock("UVIX", "BATS", "USD"),
        Stock("UVIX", "CBOE", "USD"),
        Stock("UVIX", "ARCA", "USD"),
        Stock("UVIX", "NYSEARCA", "USD"),
        Stock("UVIX", "SMART", "USD", primaryExchange="BATS"),
        Stock("UVIX", "SMART", "USD", primaryExchange="ARCA"),
        Stock("UVIX", "SMART", "USD"),
    ]
    # 実在候補があれば先頭に差し込む（重複は避ける）
    for ex in exchs:
        s = Stock("UVIX", "SMART", "USD", primaryExchange=ex)
        if all(not (c.primaryExchange == s.primaryExchange) for c in candidates if hasattr(c, "primaryExchange")):
            candidates.insert(0, s)

    last_err: Exception | None = None
    # qualifyContracts が内部で長く待つことがあるため、明示的に reqContractDetails を短期で叩く
    default_to = getattr(ib, "RequestTimeout", None)
    try:
        if default_to is not None:
            ib.RequestTimeout = 6  # ← 少しだけ延長（サーバ負荷時の保険）
        for c in candidates:
            try:
                log.info("[UVIX] trying STOCK details: exch=%s primary=%s",
                         c.exchange, getattr(c, "primaryExchange", ""))
                cds: list[ContractDetails] = ib.reqContractDetails(c)
                if not cds:
                    continue
                cd = cds[0]
                log.info(
                    "[UVIX] STOCK qualified: conId=%s primary=%s exchange=%s",
                    cd.contract.conId,
                    getattr(cd.contract, "primaryExchange", ""),
                    cd.contract.exchange,
                )
                # reqContractDetails の戻りをそのまま使う（qualifyと同等に安全）
                return Stock(
                    "UVIX",
                    "SMART",
                    "USD",
                    primaryExchange=getattr(cd.contract, "primaryExchange", None),
                )
            except Exception as e:
                last_err = e
                log.info(f"[UVIX] STOCK details fail: {e!r}")
    finally:
        if default_to is not None:
            ib.RequestTimeout = default_to

    raise RuntimeError(f"UVIX stock contract resolve failed: {last_err!r}")

@dataclass(frozen=True)
class UVIXPutSpec:
    expiry: str     # YYYYMMDD
    strike: float
    right: str = "P"
    tradingClass: Optional[str] = None

def _closest_strike(strikes: List[float], target: float) -> float:
    return min(strikes, key=lambda s: abs(s - target))

def pick_uvix_atm_put(
    ib: IB,
    manual_price: Optional[float],
    offset: float = 0.0,
    dte_min: int = 7,
    dte_max: int = 35,
) -> UVIXPutSpec:
    """
    manual_price があればそれでATMを決める。無ければ現物の live/delayed 価格を読み、ATM±offset を決定。
    満期は DTE 7〜35日帯の最も近い日を優先。
    """

    # 1) conId を .env から直参照（現物詳細を解決しない）
    env_conid = os.getenv("UVIX_CONID")
    if not env_conid:
        raise RuntimeError("UVIX_CONID not set (put it in .env)")
    conid = int(env_conid)
    # 2) conId直指定でオプション鎖メタ取得（失敗/Timeout時はローカル推定でフォールバック）
    # --- チェーン取得は“粘る”：デフォは短いことがあるので 35s 程度まで許容 ---
    default_to = getattr(ib, "RequestTimeout", None)
    opt_params = None
    try:
        if default_to is not None:
            ib.RequestTimeout = max(35, int(default_to) if isinstance(default_to, int) else 35)
        opt_params = ib.reqSecDefOptParams("UVIX", "", "STK", conid)
    except Exception as e:
        log.info(f"[UVIX] reqSecDefOptParams timeout -> fallback: {e!r}")
    finally:
        if default_to is not None:
            ib.RequestTimeout = default_to

    chain = opt_params[0] if opt_params else None
    fallback_mode = False
    if chain:
        trading_class = chain.tradingClass or "UVIX"
        strikes = sorted([float(s) for s in chain.strikes if s is not None])
        expiries = sorted(chain.expirations)  # 'YYYY-MM-DD'（=実在する満期）
    else:
        # ===== フォールバック =====
        from datetime import datetime, timedelta, timezone
        fallback_mode = True
        trading_class = "UVIX"
        # ストライク刻み：UVIX は 0.5 刻み相当で十分（実際の刻みは qualify 時に補正）
        # 0.5 から 200.0 まで仮生成（必要十分なレンジ）
        strikes = [round(0.5 + 0.5*i, 2) for i in range(0, 400)]
        # 満期候補：直近金曜 ±1〜3週 の最大7候補（※後段で存在確認してから使う）
        today = datetime.now(timezone.utc).date()
        # まず最寄り金曜を特定
        base = today + timedelta(days=(4 - today.weekday()) % 7)
        # 直近金曜(=11/7に相当)を最優先し、その次に「-7 → +7」の順で舐める
        offs = [0, -7, +7, -14, +14, -21, +21]
        expiries = [(base + timedelta(days=o)).isoformat() for o in offs]
        log.info(f"[UVIX] Fallback expiry candidates={expiries} (±週) strikes≈{len(strikes)}")

    # 3) 価格は manual 必須（MD購読が無くても動かすため）
    px = manual_price
    if not px or not (px > 0):
        raise RuntimeError("UVIX underlying price not available (set Manual price & check the box)")

    # 満期（DTE 7-35）
    today = util.dt.datetime.now(util.dt.timezone.utc).date()
    def _dte(iso_like) -> int:
        # 'YYYYMMDD' / 'YYYY-MM-DD' / 'YYYYMMDD HH:MM:SS' / date/datetime を許容
        if isinstance(iso_like, datetime):
            d = iso_like.date()
        elif isinstance(iso_like, date):
            d = iso_like
        elif isinstance(iso_like, str):
            s = iso_like.strip()
            if re.fullmatch(r"\d{8}", s):
                d = datetime.strptime(s, "%Y%m%d").date()
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
                d = datetime.strptime(s, "%Y-%m-%d").date()
            else:
                d = util.parseIBDatetime(s).date()
        else:
            raise ValueError(f"Unsupported expiry value: {iso_like!r}")
        return (d - today).days

    # チェーン取得できた場合は従来通り DTE 7–35 を優先
    # フォールバック時は DTE>=0（=直近将来）を許容して最短を採用（= 11/7 を通す）
    expiries_dte_all = [(e, _dte(e)) for e in expiries]
    if fallback_mode:
        candidates = [x for x in expiries_dte_all if x[1] >= 0]
        expiries_dte = candidates or sorted(expiries_dte_all, key=lambda x: abs(x[1]))
    else:
        expiries_dte = [x for x in expiries_dte_all if dte_min <= x[1] <= dte_max] or \
                       sorted(expiries_dte_all, key=lambda x: abs(x[1]))
    expiry_iso = expiries_dte[0][0]
    expiry_yymmdd = expiry_iso.replace("-", "")

    target = max(0.5, px + offset)
    # フォールバック時は 0.5 刻みになるため、そのまま最近傍を取る
    strike = _closest_strike(strikes, target)

    log.info(f"[UVIX] ATM base={px:.2f}, offset={offset:+.2f} → strike≈{strike:.2f}, expiry={expiry_iso}, tradingClass={trading_class}")
    return UVIXPutSpec(expiry_yymmdd, strike, "P", trading_class)

def build_uvix_put_contract(ib: IB, spec: UVIXPutSpec) -> Option:
    """
    まず SMART で qualify。失敗/timeout 時は
    - 満期を「対象日 ± 0, +7, -7, +14, -14, +21, -21（営業週）」で横断
    - 取引所候補（オプション市場）を横断
    して reqContractDetails で実在契約を特定する。
    """
    from datetime import datetime, timedelta

    base_kwargs = dict(
        symbol="UVIX",
        lastTradeDateOrContractMonth=spec.expiry,   # YYYYMMDD（目安）
        strike=float(spec.strike),
        right="P",
        currency="USD",
        tradingClass=spec.tradingClass or "UVIX",
        multiplier="100",
    )

    # 満期候補（±数週間の金曜ベース）を作る
    def _to_date(yyyymmdd: str) -> datetime.date:
        return datetime.strptime(yyyymmdd, "%Y%m%d").date()

    base_d = _to_date(spec.expiry)
    # 金曜にスナップ（base が金曜でない場合、直近の金曜へ）
    base_d = base_d + timedelta(days=(4 - base_d.weekday())) if base_d.weekday() != 4 else base_d
    # 11/7 を早めに試すため、-7 を +7 より先に
    friday_offsets = [0, -7, 7, -14, 14, -21, 21, -28, 28]
    expiry_candidates = []
    seen = set()
    for off in friday_offsets:
        d = base_d + timedelta(days=off)
        ymd = d.strftime("%Y%m%d")
        if ymd not in seen:
            expiry_candidates.append(ymd)
            seen.add(ymd)

    # 取引所候補（オプション市場に限定）: CBOEOPT を最優先、SMART は最後
    exch_candidates = ["CBOEOPT", "CBOE", "BOX", "C2", "SMART"]
    # ATM穴対策: strike 近傍も試す（±0.5, ±1.0）
    strike_offsets = [0.0, +0.5, -0.5, +1.0, -1.0]

    # 1) 通常ルート：SMART で qualify
    default_to = getattr(ib, "RequestTimeout", None)
    try:
        if default_to is not None:
            # まずは“粘って”実在契約を取る（チェーンが取れていればここで成功しやすい）
            ib.RequestTimeout = max(35, int(default_to) if isinstance(default_to, int) else 35)
        c_smart = Option(exchange="SMART", **base_kwargs)
        try:
            qc = ib.qualifyContracts(c_smart)
            if qc:
                cd = ib.reqContractDetails(qc[0])[0]
                log.info(
                    "[UVIX] PUT qualified: conId=%s class=%s exch=%s",
                    cd.contract.conId, cd.contract.tradingClass, cd.contract.exchange
                )
                return qc[0]
        except Exception as e:
            log.info("[UVIX] qualifyContracts(SMART) failed -> fallback: %r", e)

        # 2) 満期×strike近傍×取引所 のフォールバック探索
        #    1トライは 12s に拡大（全体の deadline は呼び出し側で管理）
        per_try_timeout = 12
        saved_to = getattr(ib, "RequestTimeout", None)
        for ymd in expiry_candidates:
            for off in strike_offsets:
                s_try = round(float(spec.strike) + float(off), 2)
                for ex in exch_candidates:
                    c_try = Option(
                        exchange=ex,
                        **{
                            **base_kwargs,
                            "lastTradeDateOrContractMonth": ymd,
                            "strike": s_try,
                        },
                    )
                    try:
                        if saved_to is not None:
                            ib.RequestTimeout = per_try_timeout
                        cds = ib.reqContractDetails(c_try)
                        if not cds:
                            log.info("[UVIX] reqContractDetails ex=%s exp=%s strike=%.2f: empty", ex, ymd, s_try)
                            continue
                        cd = cds[0]
                        log.info(
                            "[UVIX] PUT details: conId=%s class=%s exch=%s expiry=%s strike=%.2f (off=%+.2f)",
                            cd.contract.conId, cd.contract.tradingClass, cd.contract.exchange, ymd, s_try, off
                        )
                        # 実在契約で返す（cd.contract は Option）
                        return cd.contract
                    except Exception as e2:
                        log.info("[UVIX] reqContractDetails ex=%s exp=%s strike=%.2f failed: %r", ex, ymd, s_try, e2)
                    finally:
                        if saved_to is not None:
                            ib.RequestTimeout = saved_to

        raise RuntimeError("UVIX PUT Option qualify failed (expiry/exchanges all tried)")
    finally:
        if default_to is not None:
            ib.RequestTimeout = default_to

def get_option_mid(ib: IB, opt: Option, timeout: float = 6.0) -> Optional[float]:
    """
    MID = (bid+ask)/2 を優先。無ければ close→last を使用して概算MID。
    """
    t = ib.reqMktData(opt, "", False, False)
    elapsed = 0.0
    while elapsed < timeout:
        if t.bid and t.ask and t.bid > 0 and t.ask > 0:
            return float(round((t.bid + t.ask) / 2, 2))
        if t.close and t.close > 0:
            log.info(f"[UVIX] Fallback mid=close={t.close}")
            return float(t.close)
        if t.last and t.last > 0:
            log.info(f"[UVIX] Fallback mid=last={t.last}")
            return float(t.last)
        ib.sleep(0.1)
        elapsed += 0.1
    return None

def _nearest_expiry_friday(expirations: List[str], tz: ZoneInfo) -> str:
    """
    expirations: ['2025-10-17', ...]
    一番近い将来の金曜日を優先、無ければ最短の将来日付
    """
    today = datetime.now(tz).date()
    future = sorted(d for d in expirations if datetime.fromisoformat(d).date() >= today)
    if not future:
        raise RuntimeError("No future expirations available")
    for d in future:
        if datetime.fromisoformat(d).weekday() == 4:  # Fri
            return d
    return future[0]


def _closest_strike(strikes: List[float], target: float) -> float:
    return min(strikes, key=lambda s: abs(s - target))


def pick_option_contract(
    ib: IB,
    und: Underlying,
    right: Right,
    pct_offset: float = 0.0,
    prefer_friday: bool = True,
    tz_name: str = "America/New_York",
    opt_exchange: str = "SMART",
    *,
    override_price: Optional[float] = None,  # ← 追加：手動/外部で決めた株価を優先使用
) -> Tuple[Option, float, str]:
    """
    ATM(+/-pct) のストライクを選んで Option 契約を返す。
    - right: 'C' or 'P'
    - pct_offset: 0.15 なら +15%（CallはOTM方向、PutはITM方向にずらす）
    - override_price: ここに価格を渡すと、内部の株価取得をスキップしてその価格を使う
    戻り値: (Option契約, 使用ストライク, 使用満期[YYYY-MM-DD])
    """
    tz = ZoneInfo(tz_name)
    und_px = float(override_price) if override_price is not None else _underlying_price(ib, und)

    # Underlying の conId を取得
    und_contract = Stock(und.symbol, und.exchange, und.currency)
    cds = ib.reqContractDetails(und_contract)
    if not cds:
        raise RuntimeError(f"Underlying not found: {und.symbol}")
    conId = cds[0].contract.conId

    # オプション仕様（ストライク/満期一覧）
    params = ib.reqSecDefOptParams(und.symbol, "", und_contract.secType, conId)
    if not params:
        raise RuntimeError("No option params returned")
    p = params[0]

    expirations = sorted(list(p.expirations))
    strikes = sorted([float(s) for s in p.strikes if s is not None])

    # 満期選択
    if prefer_friday:
        expiry = _nearest_expiry_friday(expirations, tz)
    else:
        today = datetime.now(tz).date()
        future = [d for d in expirations if datetime.fromisoformat(d).date() >= today]
        if not future:
            raise RuntimeError("No future expirations available")
        expiry = future[0]

    # 目標価格（ATM ± pct）
    target = und_px * (1 + pct_offset) if right == "C" else und_px * (1 - pct_offset)
    strike = _closest_strike(strikes, target)

    # IBのOptionは 'YYYYMMDD' 形式
    expiry_yyyymmdd = expiry.replace("-", "")
    opt = Option(und.symbol, expiry_yyyymmdd, float(strike), right, opt_exchange, und.currency)
    return opt, float(strike), expiry


def sell_option(
    ib: IB,
    opt: Contract,
    qty: float,
    *,
    dry_run: bool = True,
    unsafe_skip_qualify: bool = False,  # ← 追加：決め打ち時に qualify を省略
    oca_group: Optional[str] = None,
):
    from ib_insync import Order

    # 価格が取れないケースを避けるため、実売は MKT のまま（DRYでは行だけ出す）
    o = Order(orderType="MKT", action="SELL", totalQuantity=qty)
    if oca_group:
        o.ocaGroup = oca_group
    o.transmit = not dry_run

    if dry_run:
        # ログは必ず1行で “銘柄 満期 ストライク権利” を出す
        label = _fmt_opt_label(opt) if isinstance(opt, Option) else getattr(opt, "localSymbol", "")
        log.info(f"[DRY RUN] OPT MKT SELL {qty} {label}")
        return o

    # オプション契約は qualify してから発注が安全
    if unsafe_skip_qualify:
        # conId/localSymbol で決め打ちしている場合は、TWS 側に契約解決を委ねて最短で発注
        trade = ib.placeOrder(opt, o)
    else:
        [qopt] = ib.qualifyContracts(opt)
        trade = ib.placeOrder(qopt, o)
    return trade.order

def buy_option(
    ib: IB,
    opt: Contract,
    qty: float,
    *,
    dry_run: bool = True,
    unsafe_skip_qualify: bool = False,  # ← 追加：決め打ち（conId/localSymbol）時はqualify省略
    oca_group: Optional[str] = None
):
    """P+ / C+ を想定したシンプルなマーケット買い（DRY対応）。"""
    from ib_insync import Order

    o = Order(orderType="MKT", action="BUY", totalQuantity=qty)
    if oca_group:
        o.ocaGroup = oca_group
    o.transmit = not dry_run

    if dry_run:
        log.info(
            f"[DRY RUN] OPT MKT BUY {qty} {getattr(opt, 'localSymbol', opt.symbol)} "
            f"{getattr(opt, 'lastTradeDateOrContractMonth', '')} {getattr(opt, 'right', '')}{getattr(opt, 'strike', '')}"
        )
        return o

    if unsafe_skip_qualify:
        # conId / localSymbol を決め打ちしている場合は TWS 側の契約解決に委任して最短で発注
        trade = ib.placeOrder(opt, o)
    else:
        [qopt] = ib.qualifyContracts(opt)
        trade = ib.placeOrder(qopt, o)
    return trade.order