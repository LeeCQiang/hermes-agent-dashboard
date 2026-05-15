# E1 信号 + 模拟交易机器人

Heikin Ashi + EMA50 + Stoch RSI 趋势交易系统。来源：K线游民抖音策略改良版。

**阿里云 ECS 24/7 运行**，每 15 分钟检查一次 BTC/USDT 1H 信号，自动执行模拟交易。

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

## 部署架构

```
阿里云 ECS (8.134.182.129)
└─ /root/e1-sim-bot/
   ├─ e1-sim-bot.py        # 主脚本（信号+模拟仓）
   ├─ state.json            # 状态持久化
   └─ logs/
      ├─ cron.log           # crontab 运行日志
      └─ sim.log            # 策略日志
```

### 运行频率

crontab `*/15 * * * *` — 每 15 分钟运行一次。

### 数据源

OKX API（通过 ECS mihomo 代理访问）。

### 通知

Telegram @Leecjarvisbot — 以下事件会推送：
- 开仓（LONG/SHORT）
- 平仓（止损/止盈/追踪止损）
- 每次运行结果摘要

## 管理命令

```bash
# 查看最新运行日志
ssh ecs 'tail -20 /root/e1-sim-bot/logs/cron.log'

# 查看策略日志
ssh ecs 'tail -30 /root/e1-sim-bot/logs/sim.log'

# 查看当前状态（仓位/余额）
ssh ecs 'cat /root/e1-sim-bot/state.json'

# 手动运行一次
ssh ecs 'cd /root/e1-sim-bot && python3 e1-sim-bot.py'
```

## 资金管理

- 初始余额: $100 模拟资金
- 单笔风险: 余额的 1%
- 仓位计算: 风险金额 / 止损距离
- 追踪止损: 1R 利润后激活，锁定 0.5R
