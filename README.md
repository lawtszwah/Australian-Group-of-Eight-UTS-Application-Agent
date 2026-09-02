# 数据分支

本分支只放抓取产出的数据集，**不放代码**。代码在 [`main`](../../tree/main)。

产出该数据集的代码版本：`3d94f36`
抓取日期：2026-09-02

## 文件

| 文件 | 说明 |
|---|---|
| `export/programs.json` | 完整数据集，含入学要求原文（`entry.raw` / `english.raw`），供人工核对与重新解析 |
| `export/programs.csv` | 一行一个项目的表格版，可直接在 GitHub 上浏览 |
| `export/coverage.json` | 覆盖率统计，含各字段缺失数 |
| `seeds/*.tsv` | 从各校 sitemap 发现的项目页清单（代码 + URL） |

`programs.json` 按 key 排序、缩进固定，所以 `git diff` 是逐字段可读的——
下次重抓能直接看出「哪个项目的哪条要求变了」。

## 数据概况

891 个项目，来自两校 handbook：

| 学校 | 项目数 |
|---|---|
| Monash University | 565 |
| UNSW Sydney | 326 |

| 学历层次 | 数量 |
|---|---|
| 硕士 (AQF 9) | 342 |
| 研究生文凭/证书 (AQF 8) | 277 |
| 本科 | 180 |
| 研究型 | 37 |
| 其他 | 55 |

硕士入学要求可用率（占原文非空的项目）：Monash 87%，UNSW 46%。

## 字段说明

| 字段 | 含义 |
|---|---|
| `program_key` | 全库唯一主键，如 `monash:C6001` |
| `entry.min_wam_percent` | 最低加权均分，**页面上明确写了数字才有值** |
| `entry.min_grade_band` | 成绩等级（`credit` / `distinction` 等），页面只给等级没给数字时使用 |
| `entry.raw` | 入学要求原文，一切结构化字段的依据 |
| `english.ielts_overall` | 雅思总分要求 |
| `source_url` | 来源页面 |
| `source_updated_at` | 学校自己标注的数据更新时间 |
| `fetched_at` | 本次抓取时间 |

## 三条必须知道的边界

**1. `null` 表示"未查到"，不是"没有要求"。**
解析不出来的字段一律留 `null`。这是刻意设计的：下游 agent 看到 `null`
会说"未查到"，看到一个编出来的数字则会一本正经地误导用户。

**2. `min_grade_band` 没有被换算成百分数。**
UNSW 大量项目写的是 "with a credit average"，页面上没有数字。
`credit = 65%` 是一条广为人知的换算，但它**不在页面上**，属于推断。
需要换算时请在下游显式进行，并标明这是推断。

**3. 这里没有中国院校分级分数线。**
UNSW 8543 的官方原文是 "weighted average mark of 65% **(or equivalent)**"
——对中国学生来说 "or equivalent" 才是关键信息，而它不在 handbook 上。
985 / 211 / 双非的分级名单多在各校 country-specific 页面或 PDF 里，
部分不公开。那部分计划走独立的手工维护表，与本数据集分开。

## 免责

数据抓自各校公开 handbook，仅供参考，入学要求以学校官方公布为准。
每条记录都带 `source_url` 和 `fetched_at`，请自行核实时效性。
