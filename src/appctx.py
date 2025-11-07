from __future__ import annotations

try:
    import streamlit as st
except Exception:
    st = None  # テスト時などStreamlit不在でもimport可能に

class _Ctx:
    ib_client = None
    live_orders = False

_CTX = _Ctx()

def set_client(cli) -> None:
    if st and hasattr(st, "session_state"):
        st.session_state["ib_client"] = cli
    _CTX.ib_client = cli

def get_client():
    if st and hasattr(st, "session_state"):
        return st.session_state.get("ib_client") or _CTX.ib_client
    return _CTX.ib_client

def set_live(flag: bool) -> None:
    if st and hasattr(st, "session_state"):
        st.session_state["live_orders"] = bool(flag)
    _CTX.live_orders = bool(flag)

def is_live() -> bool:
    if st and hasattr(st, "session_state"):
        return bool(st.session_state.get("live_orders", False))
    return bool(_CTX.live_orders)
