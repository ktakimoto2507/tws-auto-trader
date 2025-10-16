from ib_insync import Stock, Index

CATALOG = {
    "NUGT": Stock("NUGT", "ARCA", "USD"),
    "TMF":  Stock("TMF",  "ARCA", "USD"),
    "UVIX": Stock("UVIX","ARCA","USD"),  # 2x VIX 短期系ETN
    "VIX":  Index("VIX", "CBOE"),        # VIX指数（要: IB上の取引所は環境によってCBOE/CFE）
}
SYMBOLS_ORDER = ["NUGT", "TMF", "UVIX", "VIX"]
