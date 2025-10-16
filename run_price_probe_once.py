# run_price_probe_once.py
from ib_insync import IB, Stock

ib = IB()
ib.connect('127.0.0.1', 7497, clientId=99)
ib.reqMarketDataType(3)

c = Stock('NUGT', 'SMART', 'USD', primaryExchange='ARCA')
ib.qualifyContracts(c)
ticker = ib.reqMktData(c, "", False, False)

ib.sleep(3)
print("NUGT", "last=", ticker.last, "bid=", ticker.bid, "ask=", ticker.ask)
ib.disconnect()
