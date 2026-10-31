# Deepseek Trading Terminal

本地运行的多市场量化看板。一个 HTTP 服务、一个单文件 HTML，四个页签回答四个不同问题。

| 页签 | 直达 | 回答的问题 | 标的 |
|---|---|---|---|
| 短期动能 / 超买超卖 | `/` | 现在有多热 | A股 4：科创50、创业板指、沪深300、中证红利；美股 4：QQQ、SOXX、标普500、道琼斯 |
| 估值分位 | `#val` | 现在贵不贵 | 同上八个（道琼斯无可用估值口径，页面留空并说明） |
| 短线精灵 · A股 | `#senti` | A 股今天冷不冷 | A 股全市场 |
| 短线精灵 · 美股 | `#us` | 美股今天冷不冷 | 美股四池 + 11 个行业 |

## 快速开始

双击 `start-dashboard.bat`，浏览器自动打开 `http://127.0.0.1:8765/`。

**必须通过 `http://127.0.0.1:8765/` 访问，不要双击 `output/` 里的 HTML 文件。** 用 `file://` 打开时页面脚本无法请求本地接口，刷新按钮一定失效，页首会显示红色提示带。

依赖：

```bash
pip install pandas numpy akshare requests
# westock CLI 需自行安装到 ~/.local/bin/westock.exe（腾讯自选股行情）
```

## 目录结构

```
scripts/
  server.py           HTTP 服务 + 四个刷新接口（三把独立锁，互不阻塞）
  indicator_core.py   位置/拥挤/动能三模块计算（A 股与美股两套口径）
  market_context.py   两市成交额 + 两融 → 拥挤模块的分母与杠杆观察
  valuation.py        A 股估值（蛋卷基金）：科创50 / 创业板指 / 沪深300 / 中证红利
  us_valuation.py     美股估值（QQQ、标普500 走蛋卷，SOXX 走前瞻 PE，道琼斯无源）
  us_etf.py           QQQ / SOXX / IVV / DIA 日线（缓存优先 + 增量补尾）
  wsfeed.py           westock CLI 封装：限频重试、节流、行数完整性校验
  sentiment.py        A 股情绪周期复盘（第三页）
  us_senti.py         美股八个模块（第四页）
  tpl.html            页面模板（含 /*__DATA__*/ 等占位符）
  build_dashboard.py  模板 + 数据 → output/指数超买超卖看板.html
  indicator_v1~v3_1.py  历史版本，仅作算法演进的对照，不被任何代码引用；
                        当前生效的是 indicator_core.py（与其 v3.1 算法一致）
research/             口径文档、公式规范、验证结论
data/                 行情缓存（不入库，仅 fng_archive.json 例外）
output/               构建产物（不入库）
VERSION               版本号唯一来源（纯文本，如 1.4.2）
```

改页面只动 `tpl.html`，然后 `python scripts/build_dashboard.py` 重新生成。改计算逻辑只动 `indicator_core.py`。

**版本号只在一个地方写**：根目录 `VERSION`。构建时由 `build_dashboard.py` 注入页面右上角角标
和 `/api/ping` 的返回，不要在其他文件里另写一份——两处不同步比没有版本号更麻烦。
发版流程：改 `VERSION` → 改 `CHANGELOG.md` → `python scripts/build_dashboard.py` →
`git tag -a vX.Y.Z` → `git push --tags`。

## 数据源

| 用途 | 来源 |
|---|---|
| A 股指数日线 / 美股 ETF 日线 / 成分股行情 | 腾讯自选股 westock CLI |
| 两市成交额、两融余额 | westock + akshare |
| A 股估值 | 蛋卷基金 `index_eva` |
| 美股估值 | 蛋卷（NDX）、History of Market（SOX 前瞻 PE） |
| 美股恐惧贪婪指数 | CNN dataviz |
| 龙虎榜 / 连板梯队 / 板块排行 | westock |

## 三条口径红线

这几条是踩过坑之后定的，改动前务必先看 `research/` 里对应的文档。

**1. 不同来源的 PE 绝对值不可横向比较。** 同一日科创50 的 PE，蛋卷报 105.19、中证指数官网报 84.64（总股本法），差 24%。页面只用分位做纵向比较。

**2. SOXX 用的是前瞻 PE，不是 PE-TTM。** 它放在 A 股旁边会显得极度便宜，那是口径造成的。只能在同一口径内看纵向分位。

**3. CNN 恐惧贪婪指数与 A 股自建情绪温度毫无关系。** 两套算法、两套刻度，只能各看各的趋势，绝不可放在同一根轴上比高低。且 CNN 衡量的是标普 500 全市场，不是纳指或半导体情绪。

另外：`data/us_senti/fng_archive.json` 是每日累积的 F&G 快照。CNN 官方只提供滚动 1 年历史，**这个文件是唯一的长周期记录，丢失不可重建**，因此单独放行入库。

## 文档约定

- **`SPEC.md`**：完整技术规格，自包含，可供外部评审或复验。含全部公式、数据源、口径红线、已知局限与待解决问题。
- `research/` 存口径与方法论：指标怎么定义、为什么这么定、验证结果、代价与局限。
- `CHANGELOG.md` 存变更记录：每次改了什么、为什么改、付出什么代价。
- 版本用 git tag 标记（`v1.4.0` 这类），对应 CHANGELOG 的段落。

需求是边做边定的，因此不维护独立的需求规格书——CHANGELOG 里的动机说明和 `research/` 里的口径文档合起来承担这个职责。

## 免责

本项目为指标状态播报工具，输出不含任何买卖建议。历史分位不代表未来回报。
