"""命令行入口。

典型流程：
    python -m go8agent discover monash          # 从 sitemap 找出所有项目
    python -m go8agent crawl monash --filter "information technology" --limit 10
    python -m go8agent search --keyword "information technology" --max-ielts 6.5
    python -m go8agent changes                  # 重抓之后看有什么变了
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .db import Database
from .fetch import SITEMAPS, Fetcher
from .models import Program
from .sources import courseloop

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
SEED_DIR = ROOT / "seeds"
DB_PATH = DATA_DIR / "go8.db"


def _seed_file(university: str) -> Path:
    return SEED_DIR / f"{university}.tsv"


def cmd_discover(args: argparse.Namespace) -> int:
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    with Fetcher(DATA_DIR, delay_seconds=args.delay) as fetcher:
        entries = fetcher.discover(args.university, year=args.year)
    path = _seed_file(args.university)
    path.write_text(
        "\n".join(f"{code}\t{url}" for code, url in entries) + "\n", encoding="utf-8"
    )
    print(f"发现 {len(entries)} 个项目页 -> {path.relative_to(ROOT)}")
    return 0


def _load_seeds(university: str, keyword: str | None, limit: int | None) -> list[tuple[str, str]]:
    path = _seed_file(university)
    if not path.exists():
        raise SystemExit(f"没有种子文件 {path}，先跑：python -m go8agent discover {university}")
    entries = [
        tuple(line.split("\t", 1))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "\t" in line
    ]
    if limit:
        entries = entries[:limit]
    return entries  # type: ignore[return-value]


def _store(db: Database, program: Program, quiet: bool = False) -> None:
    diffs = db.upsert(program)
    gaps = program.gaps()
    flag = "!" if gaps else " "
    if not quiet:
        wam = program.entry.min_wam_percent
        ielts = program.english.ielts_overall
        print(
            f" {flag} {program.program_key:16} {program.title[:44]:44} "
            f"WAM={wam if wam is not None else '--':>5} IELTS={ielts if ielts is not None else '--':>4}"
            + (f"  缺:{','.join(gaps)}" if gaps else "")
        )
    for field, old, new in diffs:
        print(f"     ~ 变更 {field}: {old!r} -> {new!r}")


def cmd_crawl(args: argparse.Namespace) -> int:
    entries = _load_seeds(args.university, args.filter, None)
    if args.codes:
        wanted = {c.strip().upper() for c in args.codes.split(",")}
        entries = [(code, url) for code, url in entries if code.upper() in wanted]
        missing = wanted - {code.upper() for code, _ in entries}
        if missing:
            print(f"种子文件里没有这些代码: {sorted(missing)}", file=sys.stderr)

    db = Database(DB_PATH)
    ok = failed = skipped = fetched = 0

    with Fetcher(DATA_DIR, delay_seconds=args.delay) as fetcher:
        for code, url in entries:
            if ok >= args.limit:
                break
            if fetched >= args.max_fetch:
                print(f"\n达到 --max-fetch={args.max_fetch} 上限，停止", file=sys.stderr)
                break
            fetched += 1
            try:
                snapshot = fetcher.snapshot(args.university, code, url)
                program = courseloop.parse(
                    snapshot.read(), args.university, url, snapshot.fetched_at
                )
            except Exception as exc:
                failed += 1
                print(f" x {code:16} 解析失败: {exc}", file=sys.stderr)
                continue

            # --filter 在解析后按标题过滤：sitemap 的 URL 里没有专业名
            if args.filter and args.filter.lower() not in program.title.lower():
                skipped += 1
                snapshot.path.unlink(missing_ok=True)
                continue
            if args.level and program.level != args.level:
                skipped += 1
                snapshot.path.unlink(missing_ok=True)
                continue

            db.record_snapshot(
                program.program_key, url, snapshot.fetched_at, snapshot.sha256, snapshot.path
            )
            _store(db, program)
            ok += 1

    db.close()
    print(f"\n入库 {ok}，跳过 {skipped}，失败 {failed}")
    return 0 if failed == 0 else 1


def cmd_reparse(args: argparse.Namespace) -> int:
    """不联网，用本地快照重跑解析。改完解析逻辑就该跑这个。"""
    db = Database(DB_PATH)
    fetcher = Fetcher(DATA_DIR)
    urls = dict(_load_seeds(args.university, None, None))
    ok = failed = 0
    for snapshot in fetcher.latest_snapshots(args.university):
        try:
            program = courseloop.parse(
                snapshot.read(), args.university,
                urls.get(snapshot.code, ""), snapshot.fetched_at,
            )
        except Exception as exc:
            failed += 1
            print(f" x {snapshot.code}: {exc}", file=sys.stderr)
            continue
        _store(db, program, quiet=args.quiet)
        ok += 1
    fetcher.close()
    db.close()
    print(f"\n重解析 {ok}，失败 {failed}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    db = Database(DB_PATH)
    rows = db.search(
        keyword=args.keyword, university=args.university, level=args.level,
        max_ielts=args.max_ielts, max_wam=args.max_wam, limit=args.limit,
    )
    if not rows:
        print("没有匹配的项目")
        return 0
    for row in rows:
        print(
            f"{row['program_key']:16} {row['title'][:46]:46} "
            f"WAM={row['min_wam_percent'] or '--':>5} "
            f"IELTS={row['ielts_overall'] or '--':>4} "
            f"{row['university']}"
        )
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    db = Database(DB_PATH)
    program = db.get(args.program_key)
    if program is None:
        print(f"库里没有 {args.program_key}")
        return 1
    print(program.model_dump_json(indent=2))
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    db = Database(DB_PATH)
    rows = db.recent_changes(args.limit)
    if not rows:
        print("没有记录到变更")
        return 0
    for row in rows:
        print(f"{row['changed_at'][:19]}  {row['program_key']:16} {row['field']:20} "
              f"{row['old_value']!r} -> {row['new_value']!r}")
    return 0


EXPORT_COLUMNS = [
    "program_key", "university", "code", "title", "level", "faculty",
    "cricos_code", "credit_points", "duration_full_time", "campus", "intakes",
    "ielts_overall", "ielts_min_band", "toefl_ibt", "pte_overall",
    "min_wam_percent", "min_grade_band", "requires_cognate_degree",
    "source_url", "source_updated_at", "fetched_at",
]


def _flatten(program: Program) -> dict[str, object]:
    """摊平成一行，给 CSV 和 JSON 共用。"""
    data = program.model_dump(mode="json")
    flat = {k: data.get(k) for k in EXPORT_COLUMNS if k in data}
    flat.update({
        "campus": "; ".join(program.campus),
        "intakes": "; ".join(program.intakes),
        "ielts_overall": program.english.ielts_overall,
        "ielts_min_band": program.english.ielts_min_band,
        "toefl_ibt": program.english.toefl_ibt,
        "pte_overall": program.english.pte_overall,
        "min_wam_percent": program.entry.min_wam_percent,
        "min_grade_band": program.entry.min_grade_band,
        "requires_cognate_degree": program.entry.requires_cognate_degree,
    })
    return {k: flat.get(k) for k in EXPORT_COLUMNS}


def cmd_export(args: argparse.Namespace) -> int:
    """导出结构化数据集。

    输出按 program_key 排序、JSON 缩进固定——这样 git diff 才是逐字段可读的，
    下次重抓能直接看出"哪个项目的哪个要求变了"。
    """
    db = Database(DB_PATH)
    programs = db.all_programs()
    if not programs:
        print("库里没有数据，先跑 crawl", file=sys.stderr)
        return 1

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 完整版：保留 raw 原文，供人工核对和以后重新解析
    full = [p.model_dump(mode="json") for p in programs]
    (out_dir / "programs.json").write_text(
        json.dumps(full, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 表格版：一行一个项目，方便直接在 GitHub 上看
    rows = [_flatten(p) for p in programs]
    with (out_dir / "programs.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    gaps: dict[str, int] = {}
    for program in programs:
        for gap in program.gaps():
            gaps[gap] = gaps.get(gap, 0) + 1
    (out_dir / "coverage.json").write_text(
        json.dumps({
            "total": len(programs),
            "by_university": {
                u: sum(1 for p in programs if p.university == u)
                for u in sorted({p.university for p in programs})
            },
            "by_level": {
                lv: sum(1 for p in programs if p.level == lv)
                for lv in sorted({p.level for p in programs})
            },
            "missing_fields": dict(sorted(gaps.items())),
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"导出 {len(programs)} 个项目 -> {out_dir}")
    for name in ("programs.json", "programs.csv", "coverage.json"):
        print(f"  {name}  {(out_dir / name).stat().st_size / 1024:.0f} KB")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db = Database(DB_PATH)
    stats = db.stats()
    print(f"项目总数: {stats['total']}\n")
    print(f"{'学校':24} {'层次':10} {'数量':>5} {'有雅思':>7} {'有均分':>7}")
    for group in stats["by_group"]:
        print(f"{group['university']:24} {group['level']:10} {group['n']:>5} "
              f"{group['with_ielts'] or 0:>7} {group['with_wam'] or 0:>7}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="go8agent", description="澳洲八大+UTS 申请 agent 的数据层")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="从 sitemap 发现所有项目页")
    p.add_argument("university", choices=sorted(SITEMAPS))
    p.add_argument("--year", type=int, default=None, help="只保留指定年份，默认取最新年份")
    p.add_argument("--delay", type=float, default=1.5)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("crawl", help="抓取 + 解析 + 入库")
    p.add_argument("university", choices=sorted(SITEMAPS))
    p.add_argument("--limit", type=int, default=20, help="最多入库多少个（默认 20）")
    p.add_argument("--codes", default=None,
                   help="只抓指定代码，逗号分隔，如 'C6001,C6003'（最省事的定向抓取方式）")
    p.add_argument("--filter", default=None,
                   help="按项目名过滤。注意：sitemap 里没有项目名，所以是抓下来才能过滤，"
                        "会遍历种子清单——配合 --max-fetch 使用")
    p.add_argument("--max-fetch", type=int, default=60,
                   help="本次最多请求多少个页面（默认 60），防止 --filter 扫全站")
    p.add_argument("--level", default=None, choices=["bachelor", "master", "research", "other"])
    p.add_argument("--delay", type=float, default=1.5)
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("reparse", help="用本地快照重跑解析（不联网）")
    p.add_argument("university", choices=sorted(SITEMAPS))
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_reparse)

    p = sub.add_parser("search", help="查询项目")
    p.add_argument("--keyword")
    p.add_argument("--university")
    p.add_argument("--level", choices=["bachelor", "master", "research", "other"])
    p.add_argument("--max-ielts", type=float, help="我的雅思分数，筛出够得着的项目")
    p.add_argument("--max-wam", type=float, help="我的均分")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="看一个项目的完整数据")
    p.add_argument("program_key", help="如 monash:C6001")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("changes", help="查看字段变更历史")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("export", help="导出结构化数据集（给 data 分支用）")
    p.add_argument("--out", default="export", help="输出目录，默认 ./export")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="数据覆盖情况")
    p.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
