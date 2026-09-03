"""API entry point. ``uvicorn anvil.main_api:app``"""

from __future__ import annotations

from anvil.api.app import app

__all__ = ["app"]
