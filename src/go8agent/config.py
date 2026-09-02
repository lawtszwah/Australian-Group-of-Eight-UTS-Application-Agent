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


# .env.example 里的占位符。用户忘记替换是最常见的第一次配置错误，
# 而且它会一路混到 HTTP 请求头里才报「ascii codec can't encode」——
# 那个报错完全看不出真正原因，所以在这里提前拦住。
PLACEHOLDER_MARKERS = ("在这里填", "your-key", "xxx", "sk-xxx")


def require_api_key(var_name: str, how_to_get: str) -> str:
    """取出并校验 API 密钥，格式不对时给出能照做的提示。"""
    load_dotenv()
    value = (os.environ.get(var_name) or "").strip()

    if not value:
        raise RuntimeError(
            f"未设置 {var_name}。\n"
            f"  方式一：在项目根目录的 .env 里写 {var_name}=sk-xxx\n"
            f"         （可先 cp .env.example .env 再用编辑器填写）\n"
            f"  方式二：在终端里 export {var_name}='sk-xxx'\n"
            f"  {how_to_get}"
        )

    if any(marker in value for marker in PLACEHOLDER_MARKERS):
        raise RuntimeError(
            f"{var_name} 还是 .env.example 里的占位符，没有换成真实密钥。\n"
            f"  用编辑器打开 .env，把这一行改成你的真实密钥：\n"
            f"      {var_name}=sk-你的真实密钥\n"
            f"  （cp 只是复制模板，不会自动填内容）\n"
            f"  {how_to_get}"
        )

    if not value.isascii():
        # 非 ASCII 塞进 HTTP 头会抛 UnicodeEncodeError，报错文本完全看不出原因
        raise RuntimeError(
            f"{var_name} 含有非 ASCII 字符（可能是中文占位符，或复制时混入了全角符号）。\n"
            f"  API 密钥只会是英文字母、数字和连字符。请重新复制一遍。\n"
            f"  {how_to_get}"
        )

    return value
