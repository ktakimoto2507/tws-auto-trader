# src/ib_client.py まるごと置換してOK（先頭～connectを修正）

# --- ここは既に入れているはず。残しておいてOK ---
import sys
import asyncio
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ------------------------------------------------------

from ib_insync import IB, util   # ★ utilを追加
from .config import IBConfig
from .utils.logger import get_logger

log = get_logger("ib")

class IBClient:
    def __init__(self, cfg: IBConfig | None = None):
        self.cfg = cfg or IBConfig()
        self.ib = IB()

    def connect(self, timeout: float = 20.0):
        log.info(f"Connecting IB: host={self.cfg.host} port={self.cfg.port} clientId={self.cfg.client_id}")
        # ★ ここを connectAsync + util.run に
        util.run(self.ib.connectAsync(
            self.cfg.host, self.cfg.port,
            clientId=self.cfg.client_id, timeout=timeout, readonly=False
        ))
        if not self.ib.isConnected():
            raise RuntimeError("Failed to connect IB")
        log.info("Connected")

    def disconnect(self):
        try:
            self.ib.disconnect()
            log.info("Disconnected")
        except Exception as e:
            log.warning(f"Disconnect error: {e}")

    def fetch_account_summary(self):
        acct = getattr(self.cfg, "account", None)
        return self.ib.accountSummary(acct) if acct else self.ib.accountSummary()

    def fetch_positions(self):
        return self.ib.positions()

    def fetch_open_orders(self):
        return self.ib.openOrders()
