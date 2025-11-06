# ib_connect_probe.py
import sys
import asyncio

# Windows用: ループポリシー設定
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# ループを必ず保持
try:
    asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from ib_insync import IB  # noqa: E402

HOST = "127.0.0.1"
PORT = 7497   # TWS=7497 / Gateway=4002
CID  = 998

ib = IB()
print("CONNECT_MODE=sync (probe)")
ok = ib.connect(HOST, PORT, clientId=CID, timeout=10)
print("connected?", ok, "isConnected=", ib.isConnected())
if ib.isConnected():
    print("managedAccounts:", ib.managedAccounts())
    ib.disconnect()
