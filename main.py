"""Entry point — run with: python main.py"""
from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False, workers=1)
