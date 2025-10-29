from ib_insync import Stock, Index

# 重要:
# - ETF/ETN は SMART で取り、primaryExchange を明示する
# - UVIX は Cboe BZX → IB API では "BATS"
# - VIX は環境により CBOE/CFE。既存の CBOE のまま運用（必要なら切替）
CATALOG = {
    "NUGT": Stock("NUGT", "SMART", "USD", primaryExchange="ARCA"),
    "TMF":  Stock("TMF",  "SMART", "USD", primaryExchange="ARCA"),
    "UVIX": Stock("UVIX", "SMART", "USD", primaryExchange="BATS"),  # ← ここがキー
    "VIX":  Index("VIX", "CBOE"),  # 必要なら "CFE" に変更
}
SYMBOLS_ORDER = ["NUGT", "TMF", "UVIX", "VIX"]