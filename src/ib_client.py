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

from ib_insync import IB, util
from .config import IBConfig
from .utils.logger import get_logger

log = get_logger("ib")

class IBClient:
    def __init__(self, cfg: IBConfig | None = None):
        self.cfg = cfg or IBConfig()
        self.ib = IB()

    def connect(self, timeout: float = 20.0, market_data_type: int = 3, max_try_ids: int = 10):
        """
        clientId が重複したら +1 して最大 max_try_ids 回まで自動リトライ。
        """
        base_id = int(self.cfg.client_id)
        last_err: Exception | None = None
        for offset in range(max_try_ids):
            cid = base_id + offset
            log.info(f"Connecting IB: host={self.cfg.host} port={self.cfg.port} clientId={cid}")
            try:
                util.run(self.ib.connectAsync(
                    self.cfg.host, self.cfg.port,
                    clientId=cid, timeout=timeout, readonly=False
                ))
                if self.ib.isConnected():
                    # 1=リアル, 2=フローズン, 3=遅延, 4=遅延フローズン
                    self.ib.reqMarketDataType(market_data_type)
                    log.info(f"Connected (clientId={cid}, MDType={market_data_type})")
                    # 実際に使った clientId を保持
                    self.cfg.client_id = cid
                    return
            except Exception as e:
                last_err = e
                # 326（Client ID already in use）などは次のIDで続行
                try:
                    self.ib.disconnect()
                except Exception:
                    pass
        raise RuntimeError(f"Failed to connect IB after trying {max_try_ids} clientIds") from last_err

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
