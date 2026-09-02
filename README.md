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

**用项目独立的虚拟环境，不要装在 anaconda base 里。**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m go8agent --help
```

想少敲几个字就先激活：`source .venv/bin/activate`，之后直接用 `python -m go8agent`。

### 为什么强调这一点

在 anaconda base 里跑会撞上一个很难看懂的报错：

```
TypeError: Decompressor.decompress() got an unexpected keyword argument 'output_buffer_limit'
→ openai.APIConnectionError: Connection error.
```

原因是 base 环境里 conda 和 pip 的包混装：`httpx2` 解压响应时传了
`output_buffer_limit=`，而同环境里那个版本的 `brotlicffi` 不接受这个参数。
表面看是"连接错误"，实际和网络毫无关系。

干净的 venv 里不会装上这个冲突的包，问题自然不存在。**这类问题多数不值得
逐个去修版本号，换个干净环境更快。**

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

# 5. 导出结构化数据集（提交到 data 分支）
python -m go8agent export

# 6. 资格判断（纯本地计算，不调模型）
python -m go8agent check monash:C6001 --wam 78 --ielts 6.5 --ielts-min 6.0 --no-cognate

# 7. 自然语言提问
cp .env.example .env                 # 然后把真实密钥填进 .env
python -m go8agent ask "双非 78 分、雅思 6.5，跨专业能申哪些 IT 硕士"

# 换供应商做对比（需要对应的 key）
python -m go8agent ask "..." --provider anthropic

# 8. 重抓之后看有什么变了
python -m go8agent changes
python -m go8agent stats
```

---

## 模型供应商

默认 **DeepSeek**（`deepseek-v4-flash`，OpenAI 兼容接口），也支持 Anthropic。
两边共用 `tools.py` 里的同一套工具和 schema，只在消息格式上各写一个薄适配器。

这样设计不只是为了省钱：做评估时可以拿同一套工具去比不同模型，
这是判断「该用哪个模型」的唯一靠谱方式。

| 供应商 | 环境变量 | 默认模型 |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-v4-flash` |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-opus-5` |

用 `GO8_PROVIDER` 或 `--provider` 切换。

密钥放项目根目录的 `.env`（`cp .env.example .env` 后填写）。
`.env` 已在 `.gitignore` 里，不会被提交。真实环境变量优先级高于 `.env`，
所以临时 `export` 一个别的 key 可以立刻覆盖，不用改文件。

**参数校验不是可选项。** DeepSeek 官方文档明确写着模型「不一定生成合法 JSON，
也可能编造出 schema 里没有定义的参数」。`tools.dispatch()` 会在调用前校验
参数名、类型和枚举值，失败时返回错误说明而不是抛异常——模型读到之后
通常能自己改正重试。

## 分支约定

| 分支 | 内容 |
|---|---|
| `main` | 只放代码。`seeds/`、`data/`、`export/` 都被 gitignore |
| `data` | 抓取产出的数据集：`programs.json`、`programs.csv`、`coverage.json`、`seeds/` |

`data` 是一条 orphan 分支（独立历史，不含代码），这样数据的提交记录不会和代码
的提交记录搅在一起。原始 HTML 快照**不入库**——可随时重抓，且每次抓取都会新增
一批带时间戳的文件，提交进去会让仓库无限膨胀。

导出的 JSON 按 key 排序、缩进固定，所以 `git diff` 是逐字段可读的：
下次重抓能直接看出「哪个项目的哪条要求变了」。

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
├── eligibility.py         资格判断（纯 Python，不调模型）
├── tools.py               工具实现、schema 与参数校验（与供应商无关）
├── agent.py               agent loop（DeepSeek / Anthropic 两套）
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
- [x] `check_eligibility` —— 纯代码算资格，不交给模型
- [x] Claude tool use：把查询函数暴露成 agent 工具
- [ ] golden set 评估（30–50 条人工标注）
- [ ] 扩展到 ANU / UQ / Adelaide / UTS
- [ ] USYD / UWA（需浏览器渲染）

---

## 免责

数据抓自各校公开 handbook，仅供参考。入学要求以学校官方公布为准。
每条记录都带 `source_url` 和 `fetched_at`，请自行核实时效性。
