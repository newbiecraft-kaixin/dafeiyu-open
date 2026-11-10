"""brain 运行时单例：配置、harness、记忆库、计数。"""
from __future__ import annotations

import logging
from pathlib import Path

from infra import Stats, port_of

import roles as roles_mod
from harness import Harness
from store import MemoryStore

LOG = logging.getLogger("brain")
PORT = port_of("brain")
ROLES_PATH = Path(__file__).resolve().parents[1] / "roles.json"

config = roles_mod.RolesConfig.from_file(ROLES_PATH)
harness = Harness(config, logger=LOG)
store = MemoryStore()
counts = Stats()
