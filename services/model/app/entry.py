"""model 进程入口：日志与 HTTP 服务装配。"""
from __future__ import annotations

import logging

from infra import run

from .common import _log
from .routes import routes
from .state import PORT, counts, router


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _log(f"starting on 127.0.0.1:{PORT}; providers={list(router.providers)}")
    run(PORT, routes, service="model", version="0.1.0", stats=counts)
