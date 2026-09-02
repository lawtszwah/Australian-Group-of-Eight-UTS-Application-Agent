# 澳洲八大 + UTS 申请 Agent

一个用来练手 agent 开发的项目：给定学生背景，输出可申项目、差距分析和申请时间线。

当前进度：**阶段一 — 数据层（Monash + UNSW）**

---

## 为什么先做数据层

这类项目的成败不在 agent 框架，而在数据。澳洲高校的录取要求会变、有院校分级、
措辞含糊（"or equivalent"）。**让模型凭记忆生成分数线，答案基本都是错的。**

所以整个项目围绕一条规则设计：

> 模型不许自己产出录取要求，只能通过工具查询本地数据库；
> 每条结论必须能追溯到来源 URL 和原文片段。

`Program.gaps()` 会标出哪些字段没解析出来。解析不出来就是 `None`，
下游 agent 看到 `None` 会说"未查到"——这远好过一个编出来的数字。

---

## 为什么先做 Monash 和 UNSW

实测了九所学校的课程页形态，差异很大：

| 学校 | 页面形态 | 难度 |
|---|---|---|
| **Monash** | Next.js，`__NEXT_DATA__` 内嵌结构化 JSON | ⭐ |
| **UNSW** | 同一个 handbook 平台，**schema 完全一致** | ⭐ |
| ANU | 服务端渲染 HTML | ⭐⭐ |
| UQ / Adelaide / UTS | 服务端渲染，需解析 HTML | ⭐⭐ |
| USYD / UWA | SPA，需浏览器渲染 | ⭐⭐⭐ |
| UniMelb | 返回 403 且挂反爬脚本 | 手工录入 |

Monash 和 UNSW 用同一个 CourseLoop handbook 平台，**一个解析器同时吃两所学校**，
是投入产出比最高的起点。

UniMelb 明确拒绝自动访问，本项目不做任何绕过，改为人工录入。

---

## 安装

```bash
pip install -e ".[dev]"
```

或直接用仓库自带依赖（只需要 `httpx` 和 `pydantic`）：

```bash
PYTHONPATH=src python3 -m go8agent --help
```

---

## 用法

```bash
# 1. 从 sitemap 发现所有项目页（Monash 565 个，UNSW 326 个）
python -m go8agent discover monash
python -m go8agent discover unsw

# 2. 全量抓取并入库。约 900 个页面，串行 1.2 秒间隔，20 分钟左右
python -m go8agent crawl monash --limit 9999 --max-fetch 9999

# 定向抓几个也行
python -m go8agent crawl monash --codes C6001,C6003

# 3. 查询（这些查询以后会直接变成 agent 的 tool）
python -m go8agent search --keyword "information technology" --level master
python -m go8agent search --max-ielts 6.5 --max-wam 70   # 我的条件够得着哪些
python -m go8agent show monash:C6001

# 4. 改完解析逻辑后，用本地快照重跑，不碰学校服务器
python -m go8agent reparse monash

# 5. 重抓之后看有什么变了
python -m go8agent changes
python -m go8agent stats
```

---

## 架构

```
sitemap 发现  ->  抓取(限速)  ->  快照落盘  ->  解析  ->  校验  ->  SQLite  ->  差分
                                    |                                          |
                            原始 HTML 存 data/                        改了什么字段留痕
```

两条硬规则：

1. **原始快照必须落盘。** 解析逻辑以后一定会改，到时候跑 `reparse` 就行，
   不用重新请求学校服务器。既省事，也是基本礼貌。
2. **串行 + 固定间隔。** 总共几百个页面，没有任何理由并发轰炸。

```
src/go8agent/
├── models.py              Pydantic 模型 + 数值合理性校验
├── fetch.py               限速抓取、sitemap 发现、快照存取
├── db.py                  SQLite、字段级变更历史、查询
├── cli.py                 命令行
└── sources/courseloop.py  Monash + UNSW 解析器
tests/
├── fixtures/              真实页面快照（离线，测试不联网）
└── test_courseloop.py     回归测试
```

---

## 已知边界

- **UNSW handbook 不登载语言要求**，解析结果里 `english` 恒为 `None`，
  `gaps()` 会标出来。语言要求在 UNSW 的 international 站点上，属于待补数据源。
- **官网只给笼统要求。** UNSW 8543 的原文是 "weighted average mark of 65%
  **(or equivalent)**"——对中国学生来说 "or equivalent" 才是关键信息，
  而它不在这个页面上。
- **院校分级分数线（985 / 211 / 双非）爬不到**，多在各校 country-specific
  页面或 PDF 里，部分不公开。这部分计划走独立的手工维护表，
  和爬虫管线分开，标注来源和核实日期。

---

## 路线图

- [x] CourseLoop 解析器（Monash + UNSW）
- [x] 快照落盘 + 变更差分
- [x] 回归测试
- [ ] 中国院校分级分数线表（手工维护）
- [ ] `check_eligibility` —— 纯代码算资格，不交给模型
- [ ] Claude tool use：把查询函数暴露成 agent 工具
- [ ] golden set 评估（30–50 条人工标注）
- [ ] 扩展到 ANU / UQ / Adelaide / UTS
- [ ] USYD / UWA（需浏览器渲染）

---

## 免责

数据抓自各校公开 handbook，仅供参考。入学要求以学校官方公布为准。
每条记录都带 `source_url` 和 `fetched_at`，请自行核实时效性。
