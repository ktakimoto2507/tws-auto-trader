from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class IBConfig:
    host: str = os.getenv("IB_HOST", "127.0.0.1")
    port: int = int(os.getenv("IB_PORT", "7497"))  # Paper=7497 / Live=7496
    client_id: int = int(os.getenv("IB_CLIENT_ID", "10"))
    account: str | None = os.getenv("IB_ACCOUNT")  # ★追加
