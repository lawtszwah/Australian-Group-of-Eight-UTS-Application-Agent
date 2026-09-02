"""环境变量加载。

密钥放项目根目录的 .env 里，而不是 ~/.zshrc：
  - 只对这个项目生效，不污染其他项目
  - 换机器/换人接手时，看 .env.example 就知道要配哪些变量
  - .env 已在 .gitignore 里，不会被误提交

这里手写了一个极简的解析器而不是引入 python-dotenv：需求只有
「读 KEY=VALUE」这一件事，十几行就够，不值得多一个依赖。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"


def load_dotenv(path: Path | None = None) -> list[str]:
    """把 .env 里的变量读进 os.environ，返回本次新设置的变量名。

    已经存在于环境里的变量不会被覆盖——真实环境变量的优先级高于 .env，
    这样临时 export 一个别的 key 就能立刻生效，不用去改文件。
    """
    path = path or ENV_FILE
    if not path.exists():
        return []

    loaded: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去掉包裹的引号：KEY='xxx' 和 KEY="xxx" 都当作 xxx
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
