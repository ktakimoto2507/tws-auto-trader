from __future__ import annotations
import os
from .ib_client import IBClient
from .utils.logger import get_logger
from .ib.orders import StockSpec, market, stop_pct, new_oca_group
from .ib.options import Underlying, pick_option_contract, sell_option, _underlying_price

log = get_logger("demo_covered_call")

def main():
    cli = IBClient()
    cli.connect()

    try:
        dry_run = True  # ← 実発注はしません
        # --- NUGT カバードコール（現物→6%STP→ATMコール売り） ---
        spec = StockSpec("NUGT", "SMART", "USD")
        und  = Underlying("NUGT", "SMART", "USD")

        # 予算（USD）。.env に BUDGET_NUGT があれば優先
        budget = float(os.getenv("BUDGET_NUGT", "5000"))

        # スナップショットで概算の現在値
        px = _underlying_price(cli.ib, und)  # 要・市場データ契約（無い場合は例外）
        qty_shares = max(1, int(budget // px))
        log.info(f"NUGT price≈{px:.2f}, budget={budget}, qty_shares={qty_shares}")

        # カバードコールは 100株=1枚
        qty_contracts = qty_shares // 100
        if qty_contracts == 0:
            log.warning("株数が100未満のため、オプション売りはスキップ（株だけDRY RUN）")

        # OCAグループ（必要ならストップ/利確を束ねるのに使える）
        oca = new_oca_group("COVERED")

        # 1) 成行で株を買う（DRY）
        market(cli.ib, spec, "BUY", qty_shares, dry_run=dry_run)

        # 2) 取得価格の 6% 下にストップ（DRY）
        stop_pct(cli.ib, spec, qty_shares, reference_price=px, pct=0.06, dry_run=dry_run, oca_group=oca)

        # 3) ATM コールをショート（DRY）
        if qty_contracts >= 1:
            opt, strike, expiry = pick_option_contract(cli.ib, und, right="C", pct_offset=0.0, prefer_friday=True)
            log.info(f"Pick option: {und.symbol} C {strike} @ {expiry}")
            sell_option(cli.ib, opt, qty_contracts, dry_run=dry_run)

        log.info("Covered call (DRY RUN) completed.")

    finally:
        cli.disconnect()

if __name__ == "__main__":
    main()