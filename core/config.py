"""配置加载 + 密钥注入。

设计要点：
- config.yaml 只放非敏感信息；密钥一律从 vault **外**的 .env 读。
- 为什么在 vault 外：本 vault 是 OneDrive 同步目录且**不是 git 仓库** —— 放里面等于把
  SendKey 同步上云，且没有 .gitignore 能兜底。
- 位置：%USERPROFILE%\\.moomoo-watch\\.env（可用环境变量 MOOMOO_WATCH_ENV 覆盖）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = Path.home() / ".moomoo-watch" / ".env"


def _load_dotenv(path: Path) -> Dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与引号。不覆盖已存在的真实环境变量。"""
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or (PROJECT_ROOT / "config.yaml")
        if not self.path.is_file():
            raise FileNotFoundError(f"配置文件不存在：{self.path}")
        with self.path.open(encoding="utf-8") as fh:
            self.raw: Dict[str, Any] = yaml.safe_load(fh) or {}

        env_path = Path(os.environ.get("MOOMOO_WATCH_ENV", DEFAULT_ENV_PATH))
        self.env_path = env_path
        self._env = _load_dotenv(env_path)

    # -- 通用取值 ------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def secret(self, name: str) -> str | None:
        """密钥取值顺序：真实环境变量 > .env 文件。"""
        return os.environ.get(name) or self._env.get(name)

    # -- 路径 ----------------------------------------------------------------
    def path_for(self, key: str, create: bool = False) -> Path:
        rel = self.get(f"paths.{key}")
        if rel is None:
            raise KeyError(f"config.paths.{key} 未定义")
        p = PROJECT_ROOT / rel
        if create:
            p.parent.mkdir(parents=True, exist_ok=True) if p.suffix else p.mkdir(parents=True, exist_ok=True)
        return p

    # -- 便捷只读属性 --------------------------------------------------------
    @property
    def mode(self) -> str:
        return str(self.get("mode", "simulate")).lower()

    @property
    def source(self) -> str:
        return str(self.get("source", "fixture")).lower()

    @property
    def live_push(self) -> bool:
        return self.mode == "live"

    def missing_secrets(self) -> list[str]:
        """返回**已被路由用到**的渠道里缺的密钥名（不打印值），供启动自检。

        只检查参与路由的渠道：配了 Telegram 但没配 Server酱，不该因此报警。
        """
        routing = self.get("push.routing") or {}
        used = {str(v).lower() for v in routing.values() if v}
        if not used:
            fallback = self.get("push.channel")
            if fallback:
                used = {str(fallback).lower()}

        env_cfg = self.get("push.channels") or {}
        missing: list[str] = []
        for name in used:
            spec = env_cfg.get(name) or {}
            for key, env_name in spec.items():
                if key.startswith("env_") and env_name and not self.secret(env_name):
                    missing.append(f"{name}:{env_name}")
        return missing
