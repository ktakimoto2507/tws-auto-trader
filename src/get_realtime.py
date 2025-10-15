import time
from ib_insync import Stock
from .ib_client import IBClient
from .utils.logger import get_logger

log = get_logger("get_realtime")

def main():
    cli = IBClient()
    try:
        cli.connect()
        contract = Stock("AAPL", "SMART", "USD")
        ticker = cli.ib.reqMktData(contract, "", False, False)

        log.info("Subscribing realtime for 10 seconds...")
        start = time.time()
        while time.time() - start < 10:
            if ticker.last is not None:
                log.info(f"last={ticker.last} bid={ticker.bid} ask={ticker.ask} volume={ticker.volume}")
            time.sleep(1)

        cli.ib.cancelMktData(ticker)
    finally:
        cli.disconnect()

if __name__ == "__main__":
    main()
