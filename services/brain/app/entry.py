"""brain 进程入口：日志与 HTTP 服务装配。"""
from __future__ import annotations

import logging

from infra import run

from .common import _log
from .routes import routes
from .state import PORT, config, counts, harness, store


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _log(f"starting on 127.0.0.1:{PORT}; roles={list(config.roles)}")
    _log(f"harness dsh_root={config.dsh_root()} dsh_home={config.dsh_home()} model={harness.model_url}")
    _log(f"memory db={store.path}")
    for role in config.roles.values():
        _log(f"role {role.name}: profile={role.profile} mode={role.credential.mode}")
    run(PORT, routes, service="brain", version="0.2.0", stats=counts)
