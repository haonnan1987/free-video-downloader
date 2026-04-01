"""
Vercel Python 入口：只在此文件暴露 ASGI 变量名 `app`，避免误把 app/main.py 当作独立 Serverless 函数。
"""
from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from app.main import fastapi_app

app = fastapi_app
