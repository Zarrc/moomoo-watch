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


def find_vault_root(start: Path | None = None) -> Path:
    """从本文件（或其父目录）往上找含 `.claude` 的目录 = vault 根。

    ⚠️ **这是全库唯一的「vault 根」解析器** —— `summarize._vault_root()` 也调它。
    为什么只能有一份：不用数 parent 层数（路径深度改一次就得重数，数错是**静默的**：
    子代理 cwd 错了 → 相对路径全错 → 还 exit 0）。往上找锚点更稳，而且只此一处。
    """
    node = (start or Path(__file__).resolve().parent).resolve()
    for parent in (node, *node.parents):
        if (parent / ".claude").is_dir():
            return parent
    raise RuntimeError("找不到 vault 根（往上没有 .claude 目录）—— 项目是否被移动了？")


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
        """项目内路径：相对 `PROJECT_ROOT`（本仓库目录）。"""
        rel = self.get(f"paths.{key}")
        if rel is None:
            raise KeyError(f"config.paths.{key} 未定义")
        p = PROJECT_ROOT / rel
        if create:
            p.parent.mkdir(parents=True, exist_ok=True) if p.suffix else p.mkdir(parents=True, exist_ok=True)
        return p

    def vault_root(self) -> Path:
        """vault 根（含 `.claude` 的那层）。"""
        return find_vault_root()

    def vault_path_for(self, key: str, create: bool = False) -> Path:
        """vault 内路径：相对 **vault 根**（不是项目根）。

        `paths.vault_invest` 这类字段是「相对 vault 根」的 —— 落在用户个人区
        `Self/投资/`，而不是项目目录里。别用 `path_for()` 取它，会取到项目内。
        """
        rel = self.get(f"paths.{key}")
        if rel is None:
            raise KeyError(f"config.paths.{key} 未定义")
        p = self.vault_root() / rel
        if create:
            p.mkdir(parents=True, exist_ok=True)
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
