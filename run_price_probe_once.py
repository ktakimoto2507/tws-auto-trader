# run_price_probe_once.py
from ib_insync import *
from time import time
ib = IB()
ib.connect('127.0.0.1', 7497, clientId=99)
ib.reqMarketDataType(3)

c = Stock('NUGT','SMART','USD', primaryExchange='ARCA')
ib.qualifyContracts(c)

t = ib.reqMktData(c, '', True, False)  # snapshot
ib.sleep(2)
print('snapshot:', t.last, t.close, t.bid, t.ask)

ib.cancelMktData(c)
t = ib.reqMktData(c, '', False, False) # streaming
deadline = time() + 8
while time() < deadline:
    ib.sleep(0.25)
    if any([t.last, t.close, t.bid, t.ask]):
        print('stream:', t.last, t.close, t.bid, t.ask)
        break
else:
    print('no data within 8s')
ib.disconnect()
