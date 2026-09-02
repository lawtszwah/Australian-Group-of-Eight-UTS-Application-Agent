"""抓取层：礼貌抓取 + 原始快照落盘。

两条硬规则：
1. 抓下来的原始内容一律先落盘，再谈解析。解析逻辑以后一定会改，
   到时候重跑本地快照就行，不用再去打学校的服务器。
2. 串行 + 固定间隔。总共几百个页面，没有任何理由并发轰炸。
"""

from __future__ import annotations

import gzip
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import httpx

USER_AGENT = (
    "go8-application-agent/0.1 (educational project; "
    "https://github.com/lawtszwah/Australian-Group-of-Eight-UTS-Application-Agent)"
)

SITEMAPS = {
    "monash": "https://handbook.monash.edu/sitemap.xml",
    "unsw": "https://www.handbook.unsw.edu.au/sitemap.xml",
}

# 从 sitemap 里筛出"研究生项目页"的 URL 形态
URL_PATTERNS = {
    "monash": re.compile(r"^https://handbook\.monash\.edu/(\d{4})/courses/([A-Z]\d{4})$"),
    "unsw": re.compile(
        r"^https://www\.handbook\.unsw\.edu\.au/postgraduate/programs/(\d{4})/(\d{4})$"
    ),
}


@dataclass
class Snapshot:
    """一份原始页面快照。"""

    university: str
    code: str
    url: str
    fetched_at: datetime
    sha256: str
    path: Path

    def read(self) -> str:
        with gzip.open(self.path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()


class Fetcher:
    def __init__(
        self,
        data_dir: Path,
        delay_seconds: float = 1.5,
        timeout: float = 30.0,
    ) -> None:
        self.snapshot_dir = data_dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay_seconds
        self._last_request = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def get(self, url: str, retries: int = 2) -> str:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle()
            try:
                response = self._client.get(url)
                response.raise_for_status()
                return response.text
            except httpx.HTTPStatusError as exc:
                # 4xx 重试没意义，直接抛；5xx 退避后再试
                if exc.response.status_code < 500:
                    raise
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
        raise RuntimeError(f"抓取失败 {url}: {last_error}") from last_error

    # ------------------------------------------------------------------
    # 项目发现：从 sitemap 里找出所有项目页，不用手工维护 URL 清单
    # ------------------------------------------------------------------
    def discover(self, university: str, year: int | None = None) -> list[tuple[str, str]]:
        """返回 [(code, url), ...]。"""
        if university not in SITEMAPS:
            raise ValueError(f"未知学校 {university}")
        pattern = URL_PATTERNS[university]

        index = self._client.get(SITEMAPS[university]).text
        sub_sitemaps = self._sitemap_locs(index)

        found: dict[str, tuple[str, str]] = {}
        for sub in sub_sitemaps:
            try:
                body = self.get(sub)
            except Exception:
                continue
            for loc in self._sitemap_locs(body):
                match = pattern.match(loc)
                if not match:
                    continue
                loc_year, code = match.group(1), match.group(2)
                if year is not None and int(loc_year) != year:
                    continue
                # 同一 code 可能有多个年份，保留年份最大的
                previous = found.get(code)
                if previous is None or loc_year > previous[0]:
                    found[code] = (loc_year, loc)
        return sorted((code, url) for code, (_, url) in found.items())

    @staticmethod
    def _sitemap_locs(xml_text: str) -> list[str]:
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            return []
        namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        return [
            element.text.strip()
            for element in root.iter(f"{namespace}loc")
            if element.text
        ]

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self, university: str, code: str, url: str) -> Snapshot:
        html = self.get(url)
        fetched_at = datetime.now(timezone.utc)
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()

        directory = self.snapshot_dir / university
        directory.mkdir(parents=True, exist_ok=True)
        stamp = fetched_at.strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{code}__{stamp}.html.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(html)

        return Snapshot(university, code, url, fetched_at, digest, path)

    def latest_snapshots(self, university: str) -> list[Snapshot]:
        """读取本地已有的最新快照（每个 code 取最近一次），用于离线重跑解析。"""
        directory = self.snapshot_dir / university
        if not directory.exists():
            return []
        newest: dict[str, Path] = {}
        for path in sorted(directory.glob("*.html.gz")):
            code = path.name.split("__")[0]
            newest[code] = path  # 文件名含时间戳，字典序即时间序
        results = []
        for code, path in sorted(newest.items()):
            stamp = path.name.split("__")[1].removesuffix(".html.gz")
            results.append(
                Snapshot(
                    university=university,
                    code=code,
                    url="",  # 离线重跑时由数据库补回
                    fetched_at=datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                        tzinfo=timezone.utc
                    ),
                    sha256="",
                    path=path,
                )
            )
        return results
