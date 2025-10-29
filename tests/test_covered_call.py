import logging
from src.ib.orders import run_covered_call
from src.ib import orders as orders_mod

def fake_pick(ib, und, right, pct_offset=0.0, prefer_friday=True, tz_name="America/New_York",
              opt_exchange="SMART", *, override_price=None):
    ref = float(override_price or 100.0)
    r = right  # ← 外に退避しておく
    class DummyOpt:
        symbol = und.symbol
        lastTradeDateOrContractMonth = "20251115"
        right = r
        strike = float(round(ref))
    return DummyOpt(), ref, "2025-11-15"

    # --- sell_option をダミー化（ログだけ出す） ---
    def fake_sell(ib, opt, qty, *, dry_run=True, oca_group=None):
        logging.getLogger("options").info(
            f"[DRY RUN] OPT MKT SELL {qty} {opt.symbol} {opt.lastTradeDateOrContractMonth} {int(opt.strike)}{opt.right}"
        )
        return None

    monkeypatch.setattr(orders_mod, "pick_option_contract", fake_pick)
    monkeypatch.setattr(orders_mod, "sell_option", fake_sell)

    class DummyIB: pass
    ib = DummyIB()

    with caplog.at_level(logging.INFO):
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
