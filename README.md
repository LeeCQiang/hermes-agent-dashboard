# E1 信号机器人

Heikin Ashi + EMA50 + Stoch RSI 趋势交易系统。来源：K线游民抖音策略改良版。

每小时自动检测 BTC/USDT 1H 级别交易信号，通过 Telegram 推送通知。

## 策略概述

| 指标 | 参数 |
|------|------|
| K线类型 | Heikin Ashi |
| 趋势过滤 | EMA50 |
| 震荡指标 | Stoch RSI (14, 3, 3) |
| 超买/超卖 | 80 / 20 |
| 止盈 | R:R 2.5 |
| 止损 | 1% 风险 |
| 时间框架 | 1H |

### 做多条件
1. 价格在 EMA50 之上
2. Heikin Ashi 看涨（收盘 > 开盘）
3. Stoch RSI %K 上穿 20（超卖区金叉）

### 做空条件
1. 价格在 EMA50 之下
2. Heikin Ashi 看跌（收盘 < 开盘）
3. Stoch RSI %K 下穿 80（超买区死叉）

## 回测数据

BTC/USDT 1H, 5个月（2026-01 ~ 2026-05）

| 指标 | 数值 |
|------|------|
| 初始资金 | $100 |
| 最终资金 | $208 |
| 收益率 | +108.3% |
| 胜率 | 61.2% |
| 盈利因子 | 2.20 |
| 最大回撤 | -5.8% |
| 交易次数 | 129 |
| 同期BTC持有 | -22% |

## 部署方式

**GitHub Actions** 每小时触发（UTC :03），北京时间每小时+3分钟。

数据源：OKX API（现货 1H K线）

通知：Telegram @Leecjarvisbot

### 仓库文件

```
e1-signal-bot.py              # 信号检测脚本
.github/workflows/e1-signal-bot.yml  # GH Actions 工作流
```

### GitHub Secrets

| Secret | 用途 |
|--------|------|
| `TELEGRAM_BOT_TOKEN` | Telegram 机器人 Token |
| `TELEGRAM_CHAT_ID` | 通知目标 Chat ID |

## 运行状态检查

前往 [Actions 页面](https://github.com/LeeCQiang/hermes-agent-dashboard/actions/workflows/e1-signal-bot.yml) 查看最新执行日志。
