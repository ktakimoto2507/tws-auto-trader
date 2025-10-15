from .ib_client import IBClient
from .utils.logger import get_logger

log = get_logger("get_account")

def main():
    cli = IBClient()
    try:
        cli.connect()
        summary = cli.fetch_account_summary()
        positions = cli.fetch_positions()
        orders = cli.fetch_open_orders()

        log.info("=== Account Summary ===")
        for x in summary:
            log.info(f"{x.tag}: {x.value} {x.currency or ''}")

        log.info("=== Positions ===")
        for p in positions:
            log.info(f"{p.contract.symbol} {p.position} @ {p.avgCost}")

        log.info("=== Open Orders ===")
        for o in orders:
            log.info(f"{o.order.orderType} {o.order.action} {o.order.totalQuantity} {o.contract.symbol}")
    finally:
        cli.disconnect()

if __name__ == "__main__":
    main()
