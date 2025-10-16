# src/utils/loop.py
from __future__ import annotations
import sys
import asyncio

def ensure_event_loop() -> None:
    """StreamlitのScriptRunnerなど、イベントループが無いスレッドでも安全にIB呼び出しできるようにする"""
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
