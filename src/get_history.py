from ib_insync import Stock, util
from .ib_client import IBClient
from .utils.logger import get_logger

log = get_logger("get_history")

def main():
    cli = IBClient()
    try:
        cli.connect()
        contract = Stock("AAPL", "SMART", "USD")
        bars = cli.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="5 D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            keepUpToDate=False,
        )
        df = util.df(bars)
        log.info(f"bars={len(df)}")
        log.info(df.tail(5).to_string())
        df.to_csv("logs/aapl_5d_5m.csv", index=False, encoding="utf-8")
        log.info("Saved: logs/aapl_5d_5m.csv")
    finally:
        cli.disconnect()

if __name__ == "__main__":
    main()
