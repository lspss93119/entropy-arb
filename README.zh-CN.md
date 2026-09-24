# entropy-arb

**[English documentation / 英文文档 → README.md](README.md)**

开源双交易所永续合约套利机器人。其中一条腿永远是 **Entropy**（Hyperliquid 上的
`io` builder dex）；另一条腿（对冲腿）三选一：

| `--hedge` | 交易所 | 计价货币 | 吃单费 | 协议 |
|---|---|---|---|---|
| `lighter` | Lighter 主网 | USDC | 0 bps | zkLighter ws（增量订单簿，异步结算） |
| `lighter-rh` | Lighter Robinhood 链 | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book，IOC 同步结算 |

> **推荐链接** —— 通过以下链接注册即可支持本项目：
> - Entropy — Tier 4 推荐，100% 返佣：<https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood 链：<https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz（Hyperliquid）：<https://app.hyperliquid.xyz/join/QUANTGUY>

当同一品种在一边贵、另一边便宜时，机器人同时在贵的一边卖出、便宜的一边买入
（均为吃单），持有 delta 中性仓位，等溢价回归后反向平仓。所有交易决策使用的
价格都来自**将要实际成交的那个交易所的真实订单簿**——Hyperliquid 的盘口来自
官方 websocket（`wss://api.hyperliquid.xyz/ws`），Lighter 的盘口来自 Lighter
官方 websocket。

机器人运行期间（即使没有密钥、没有开策略）会自动把两边盘口记录成**分钟级
CSV 数据**，配套的分析工具可以直接把这些数据变成策略所需的三个核心参数。

## 信号逻辑

整个信号就是 `config.yaml` 里三个数字，由你根据采集的数据自己设定：

```
premium_bps =（Entropy 价格 / 对冲腿价格 − 1）× 10 000

                          ┌──────────────  卖出 Entropy + 买入对冲腿
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   溢价的长期中枢
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  买入 Entropy + 卖出对冲腿
```

- `midline_bps` —— 溢价的常态水平。跨所溢价几乎从不以零为中心（预言机不同、
  计价货币不同、新上市溢价等），零中心的带只会朝一个方向开仓、打满仓位上限、
  永远无法平仓。请实际测量溢价所在的位置，然后填入。
- `upper_bps` / `lower_bps` —— 中枢上下两侧的入场带宽。

两个方向的门槛都作用于**可实际成交的价格**（Entropy 买一 对 对冲腿卖一，
反之亦然），并且是**扣除双边吃单手续费之后的净门槛**——引擎会在阈值之上
另行叠加手续费。因此一次完整往返扣费后**净赚 ≥ upper + lower bps**，这是
结构上保证的。

有一点必须理解：当 `midline_bps: 5` 时，买入 Entropy 的门槛是
`lower − midline`，可能为**负数**。这是有意为之——如果 Entropy 长期贵 5 bps，
那么在溢价为 0 时买入它，相对其自身均衡水平就是便宜了 5 bps，这笔交易正是
此前在 `midline + upper` 处卖出的获利平仓。这同时意味着**中枢填错就是亏钱
策略**：若真实溢价中枢是 0 而你填了 5，机器人会整天以公允价买入 Entropy。
先测量、再交易——数据采集器和分析工具就是为此而生。

### 可选的滚动窗口策略

固定阈值策略仍然是默认值。要明确启用 walk-forward 滚动策略，请在
`config.yaml` 中设置 `strategy.mode: rolling`，并调整
[config.example.yaml](config.example.yaml) 里的 `rolling:` 区块。它只使用
严格早于当前更新区块的已完成分钟采集行，以中位数作为动态中枢，并按
`update_minutes` 更新。`thresholds.upper_bps` 与 `thresholds.lower_bps` 仍然是
动态中枢两侧的可执行入场带宽；`thresholds.midline_bps` 只为 fixed 模式兼容而
保留，rolling 交易不会使用它。当前盘口点差、覆盖率和原有的含手续费可成交
双腿计划仍然必须通过。

rolling 可以在同方向入场信号持续成立时连续增加库存。每次两腿结算后的入场都以
实际平均成交价记录为一个 pair-specific lot。遇到动态带的相反方向信号时，程序会
建立 reduce-only 计划，按当前可成交深度优先选择预期回收较好的 lot，并且不会让
平仓低于其 break-even 门槛。部分平仓会继续保留在 ledger 中，直到全部库存关闭才
回到 flat；没有 timeout 或强制平仓。窗口无效或覆盖率不足时会停止新入场，不会偷偷
退回固定策略。实盘启动和后续对账时，持久化 lot ledger 必须与交易所权威仓位一致；
缺失、损坏或不匹配都会停止 rolling。实盘 rolling 仍必须保持
`recorder.enabled: true`，停机后需要完成对账并手动重启。

现有 `thresholds:` 区块仍然必须保留，方便同一份配置切回 `fixed`；
`--record-only` 的无下单行为不变。

## 快速开始

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 数据采集只需要这些

cp config.example.yaml config.yaml       # 策略配置（阈值、规模、风控）
cp .env.example .env                     # 密钥——交易必填
```

交易哪个市场**不在**配置文件中——每次启动时用命令行参数显式指定：
`--symbol`（两个交易所共同交易的品种）和 `--hedge`（三选一：
`lighter`、`lighter-rh`、`tradexyz`；Entropy 永远是
另一条腿）。已知的交易所原生别名不区分大小写，例如
`--symbol OAI` 和 `--symbol OPENAI` 都会统一成 canonical `OAI`，然后由
对冲适配器使用对应交易所的原生市场名称。

本机器人**没有模拟盘**——要么采集数据（`--record-only`），要么实盘交易。
请用采集的数据和最小的仓位上限来验证策略，而不是模拟成交。

**第一步：先采集数据**（不需要任何密钥）：

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh
```

至少运行几个小时（最好一整天——溢价存在日内规律），数据写入
`logs/record/minutes-SYMBOL-HEDGE.csv`，例如
`logs/record/minutes-SNDK-lighter-rh.csv`。

如需进行基础多市场采集，可以重复参数（也可以使用逗号分隔）。这是仅采集模式，
会为每个 `品种 x 对冲交易所` 配对启动一个独立采集器：

```bash
python3 main.py --record-only --no-dashboard \
  --symbol SNDK --symbol BTC \
  --hedge lighter --hedge lighter-rh
```

程序会自动区分输出文件，例如
`logs/record/minutes-SNDK-lighter.csv` 和
`logs/record/minutes-BTC-lighter-rh.csv`。
市场清单仍由命令行显式提供；这个第一版多市场模式不会自动扫描品种。

**第二步：分析数据、设定阈值：**

```bash
python3 tools/analyze.py
# 多市场示例：
python3 tools/analyze.py --csv logs/record/minutes-SNDK-lighter.csv
```

它会输出溢价分布、各档带宽的历史触发频率，以及可直接粘贴进
`config.yaml` 的 `thresholds:` 配置块。

成交结果使用独立的分析器，不会混入分钟级分析。它一次读取一个市场与对冲交易所
的成交文件，输出成交质量、相对 BBO 的滑点、对冲结果，以及各信号区间的实际价差：
包括实际成交价差 bps、加权滑点，以及对冲成交价格与名义金额：

```bash
python3 tools/analyze_trades.py \
  --csv logs/trades/trades-SNDK-lighter-rh.csv
```

如果存在多个市场成交文件，请明确传入 `--csv`，避免把不同市场和对冲交易所的
统计混在一起。

**第三步：实盘** —— 填写 `.env`，安装签名 SDK，仓位上限从刚好满足
交易所最小名义的水平开始：

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --hedge lighter-rh
```

不带 `--record-only` 运行时，只要两边行情就绪且溢价越过带宽，就会立即
发送真实订单。

**仪表盘。** 在终端运行时会显示实时 Rich 仪表盘：两边盘口（含数据龄/点差）、
持仓与上限、账户权益与本次会话盈亏、两个方向的可成交溢价对比完整门槛
（已含手续费与库存加价，● 表示已武装）、数据采集进度、最近成交，以及日志
尾部（完整日志写入 `logging.file`，默认
`logs/engine/engine-SYMBOL-HEDGE.log`）。`--record-only`
模式同样可用。加 `--cn` 参数可使仪表盘全部以中文显示。`--no-dashboard`
可切换为纯日志输出（nohup/systemd 等非终端环境会自动退回纯日志），也可
设置 `logging.dashboard: false`。

## 数据采集与分析

采集器在所有模式下自动运行（`recorder.enabled: true`）：每秒采样一次两边
的真实盘口，每分钟写一行：

| 列 | 含义 |
|---|---|
| `symbol`, `hedge` | 市场配对身份 |
| `minute_ts`, `time_utc` | 分钟起点（epoch 秒 / ISO UTC） |
| `entropy_bid/ask`, `hedge_bid/ask` | 该分钟最后一次有效盘口 |
| `entropy_bid/ask_qty`, `hedge_bid/ask_qty` | 该分钟最后一次有效盘口数量 |
| `premium_open/high/low/close/mean/std_bps` | Entropy 相对对冲腿的中间价溢价 |
| `sell_edge_mean/max_bps` | 卖出 Entropy 方向的可成交溢价（Entropy 买一 / 对冲腿卖一 − 1） |
| `buy_edge_mean/max_bps` | 买入 Entropy 方向的可成交溢价（对冲腿买一 / Entropy 卖一 − 1） |
| `entropy_update_count`, `hedge_update_count` | 该分钟观察到的盘口更新次数 |
| `*_gap_p50/p95_ms` | 两个 feed 的盘口更新间隔 p50/p95 |
| `entropy_age_p95_ms` | 相对 Hyperliquid 盘口 server timestamp 的本地接收延迟 p95 |
| `samples` | 该分钟约 60 秒中两边盘口同时有效的秒数 |

采集的 edge 为费前口径；分析工具在统计触发频率前会先扣除 `--fees-bps`
（请传入**两边吃单费之和**——零费交易所默认 0.0，对冲腿为 `tradexyz` 时
约为 1.0），因此其表格与建议值可直接填入配置。`--hours 24`
可只分析最近数据；溢价中枢会漂移，请定期重新分析并更新 `config.yaml`。

## 配置说明

策略在 `config.yaml`（严格校验——未知键名直接报错），密钥在 `.env`。
交易市场由命令行指定（`--symbol`、`--hedge`）。完整的双语注释参考：
[config.example.yaml](config.example.yaml)。核心项：

| 键 | 含义 | 默认值 |
|---|---|---|
| `strategy.mode` | 信号模式：`fixed` 或 `rolling` | `fixed` |
| `thresholds.midline_bps` | 溢价中枢（必须实测！） | — |
| `thresholds.upper_bps` / `lower_bps` | 入场带宽（> 0） | — |
| `rolling.*` | 因果中位数窗口、更新频率、覆盖率、CSV seed 和平仓回收门槛 | 见配置文件 |
| `entropy.dex` | Entropy 在 Hyperliquid 上的 dex 名 | `io` |
| `*.taker_fee_bps` | 各所吃单费 | 0.0（tradexyz 对冲腿：1.0） |
| `*.max_position_usd` | 各所持仓上限 | 1000 |
| `*.max_orders_per_min` | 各所每分钟下单预算（滑动 60 秒） | 120；Lighter 对冲腿 30 |
| `sizing.take_fraction` | 吃掉可套利深度的比例 | 0.5 |
| `sizing.max_order_notional_usd` | 单笔名义上限 | 500 |
| `inventory.scale_bps` / `floor_frac` | 库存阶梯（仓位超过上限的 `floor_frac` 后额外加价） | 10 / 0.5 |
| `execution.premium_persist_sec` | 信号需持续多久才触发 | 0.3 |
| `execution.*` | 滑点保护、超时、对账周期等 | 见配置文件 |
| `recorder.*` | 分钟数据采集器 | 开启，`logs/record/minutes-SYMBOL-HEDGE.csv` |
| `logging.trades_csv` | 每个市场与对冲交易所的成交摘要 | `logs/trades/trades-SYMBOL-HEDGE.csv` |
| `logging.dashboard` / `logging.file` | 终端仪表盘；开启时日志写入文件 | 开启，`logs/engine/engine-SYMBOL-HEDGE.log` |

每次启动程序还会把本次实际生效的策略参数追加到
`logs/engine/runs-SYMBOL-HEDGE.csv`。成交 CSV 会写入对应的
`run_id`，因此可以在不重复保存完整配置的情况下比较不同参数运行结果。
实盘成交列还会记录两腿各自的完成耗时、先完成的腿、信号当下的报价/feed
年龄，以及交易所原生取消或拒绝原因。每次实盘启动也会记录主机区域、行情／
下单传输模式和 `code_version`；部署到没有 Git checkout 的环境时，设置
`ENTROPY_ARB_CODE_VERSION` 即可标记版本。

## 密钥配置（`.env`，仅实盘需要）

- **Entropy / tradexyz（Hyperliquid）** —— 在
  <https://app.hyperliquid.xyz/API> 创建 API（agent）钱包。`HL_PRIVATE_KEY`
  填 **agent 钱包私钥**，`HL_ACCOUNT_ADDRESS` 填主账户地址。当
  `--hedge tradexyz` 时两条腿默认共用该账户（内部自动共享 nonce 序列）；
  如需分开，设置 `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`。注意给
  所交易的各 dex 分别充入保证金。
- **Lighter** —— 密钥按部署分开命名：
  - `--hedge lighter` 读取 `LIGHTER_MAINNET_ACCOUNT_INDEX`、
    `LIGHTER_MAINNET_API_KEY_INDEX`、`LIGHTER_MAINNET_API_PRIVATE_KEY`。
  - `--hedge lighter-rh` 读取 `LIGHTER_RH_ACCOUNT_INDEX`、
    `LIGHTER_RH_API_KEY_INDEX`、`LIGHTER_RH_API_PRIVATE_KEY`。
  两组可以同时填写在同一个 `.env` 中，但账户 index、API key index 和私钥必须
  属于对应部署，不可混用。参见
  [lighter-python](https://github.com/elliottech/lighter-python)。

## 执行机制

- 两条腿**同时发出吃单**：Lighter 用带均价保护的市价单，在鉴权 websocket
  上异步确认成交；Hyperliquid 用 IOC 限价单同步结算（结果未知时轮询
  orderStatus 兜底）。
- **持续性闸门**（`premium_persist_sec`）：信号先"武装"，持续存在才触发，
  过滤单 tick 的假信号。
- **Rolling 模式**（明确选择后）：使用严格排除当前区块的滚动中位数，允许同方向
  库存追加，并在相反动态带出现时以 break-even 安全的 reduce-only 主腿平仓。
- **库存阶梯**：仓位超过上限的 `floor_frac` 后，同方向加仓需要线性递增的
  额外溢价，满仓时最高加 `scale_bps`。
- **净敞口对冲**：两腿成交不对等时立即用 reduce-only 单（带滑点保护）
  削减敞口，并每 `reconcile_sec` 与链上仓位对账。
- **故障隔离**：被限频的交易所短暂暂停；交易所不可达（如例行维护）时暂停
  交易并每 `venue_probe_sec` 探测直至恢复；连续 `max_consecutive_errors`
  次执行异常则整体停机。
- **仅实盘**：没有模拟成交模式。`--record-only` 是唯一无风险的运行方式，
  其余都是真金白银。

## 目录结构

```
main.py                  入口（--record-only，默认即实盘）
entropy_arb/config.py    YAML + .env 配置契约与校验
entropy_arb/book.py      订单簿 + 含手续费的套利规模计算
entropy_arb/feeds.py     官方 HL ws + zkLighter ws 行情
entropy_arb/venue_hl.py  Hyperliquid dex 适配器（Entropy、tradexyz）
entropy_arb/venue_lighter.py  zkLighter 适配器（主网、Robinhood 链）
entropy_arb/engine.py    双交易所策略主循环
entropy_arb/dashboard.py Rich 终端仪表盘
entropy_arb/recorder.py  分钟级盘口数据采集
tools/analyze.py         logs/record/minutes-SYMBOL-HEDGE.csv -> 阈值建议
tools/analyze_trades.py  logs/trades/trades-SYMBOL-HEDGE.csv -> 成交质量分析
tests/                   python3 -m pytest tests/
```

## 已知风险

- **中枢填错就是亏钱策略。** 溢价中枢会漂移，请定期重新测量并保持
  `config.yaml` 与市场同步。
- **USDG 基差**（`lighter-rh`）：对冲腿以 USDG 计价，持续溢价中有
  一部分是稳定币本身的基差；midline 吸收其水平，但 USDG 的*变动*是真实盈亏。
- **资金费**：两个交易所、两套独立的资金费率，持仓成本未建模——仓位上限
  请设小一些。
- **薄盘口**：Entropy 深度可能很小；`take_fraction` 与名义上限控制单笔规模，
  但部分成交后对冲腿的滑点是真实存在的。
- **交易时段**：股票类永续（如 SNDK）盘后各所预言机行为不同，建议加宽带宽
  或避开盘后。
- **单腿风险**：一条腿成交后另一条可能失败。机器人会自动对冲并对账，但
  仍需人工关注。

风险自负。本软件直接操作真实资金，本文档不构成任何投资建议。请从最小的
仓位上限开始。

## 开源协议

[MIT](LICENSE)
