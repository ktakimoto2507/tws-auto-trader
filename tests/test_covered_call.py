import logging
from src.ib.orders import run_covered_call
from src.ib import orders as orders_mod


def test_covered_call(monkeypatch, caplog):
    # --- fakes ---
    def fake_pick(
        ib,
        und,
        right,
        pct_offset: float = 0.0,
        prefer_friday: bool = True,
        tz_name: str = "America/New_York",
        opt_exchange: str = "SMART",
        *,
        override_price=None,
    ):
        ref = float(override_price or 100.0)
        r = right  # 外に退避
        class DummyOpt:
            symbol = und.symbol
            lastTradeDateOrContractMonth = "20251115"
            right = r
            strike = float(round(ref))
        # 実装側が (opt, ref_price, expiry_iso) の形を期待している前提
        return DummyOpt(), ref, "2025-11-15"

    def fake_sell(ib, opt, qty, *, dry_run=True, oca_group=None):
        logging.getLogger("orders").info(
            f"[DRY RUN] OPT MKT SELL {qty} {opt.symbol} {opt.lastTradeDateOrContractMonth} {int(opt.strike)}{opt.right}"
        )
        return None

    # --- monkeypatch: run_covered_call が解決する参照先（orders_mod 内の名前）を差し替える ---
    monkeypatch.setattr(orders_mod, "pick_option_contract", fake_pick)
    monkeypatch.setattr(orders_mod, "sell_option", fake_sell)

    class DummyIB:
        pass

    ib = DummyIB()

    # "orders" ロガーを INFO で捕捉
    with caplog.at_level(logging.INFO, logger="orders"):
        run_covered_call(
            ib,
            symbol="NUGT",
            budget_usd=600000,
            stop_pct_value=0.10,
            manual_price=100.0,
            dry_run=True,
        )

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "STOCK LMT BUY" in text          # 指値買いログ
    assert "STOCK STP SELL" in text         # ストップ売りログ
    assert "OPT MKT SELL" in text           # C-売りログ（DRY）
