# heartbeat

让 AI 主动来找你。

大多数聊天 bot 只会等你开口。heartbeat 给它装一个“心跳”：你一段时间没说话，它会自己发一条消息过来。

有两种心跳，可以随时切换：

| | 怎么定下一次 | 什么时候发 |
|---|---|---|
| **A 固定间隔** | 每次固定 55 分钟 | 只在白天（默认 08:30–次日 02:00），夜里不打扰 |
| **B AI 自己定** | AI 每次回复，在最后一行写 `[心跳:N]`，N 分钟后再来 | 24 小时都跑：白天 N 在 5–55 之间，夜里放宽到 30–180；夜里的落点越过 08:30 就截到 08:30 |

两种模式有一个共同点：**你一说话，就从这次回复重新计时。**
`[心跳:N]` 这一行会在发给你之前删掉，你看不到。

## 为什么 B 的白天上限是 55 分钟

Claude 的提示缓存最长保留 1 小时。间隔不超过 55 分钟，每次心跳都还能用上缓存，便宜也快。

## 踩过的坑（代码里都处理了）

- **夜里排的心跳会错位**：A 模式在夜里如果还按 55 分钟往后排，白天第一条会落在一个随机时间。现在夜里一律直接等到 08:30，准点发。
- **离边界差零点几秒时会原地空转**：剩下的秒数要向上取整，并且至少等 1 秒。
- **一重启就被往后推**：如果每次启动都从“现在”开始重新数 55 分钟，一小时里重启几次，心跳就永远发不出来。现在会记下上次真正发出去的时间，重启后接着这个时间排；已经过点的，马上补一条。
- **撞上正在回的消息**：AI 正忙着回你时心跳来了，这一次先跳过，5 分钟后再试，不白等一整轮。
- **心跳链断了还显示开着**：每次心跳发完才排下一次。排程这一步要是出了错，后面就再也不来了，开关却还显示开着。所以排程加了兜底，出错就按固定间隔补排；打开开关时如果发现没排上，也会补排。
- **AI 写的 N 不可信**：标记只认 1–4 位数字，读出来还要夹进允许的范围；不合法的一律按 55 分钟算。

## 文件

- `heartbeat.py`：排程逻辑，全是纯函数，不依赖 Telegram，可以接到任何聊天程序上
- `bot_example.py`：最小可跑示例，Telegram bot + [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) CLI
- `tests/`：排程逻辑的测试

## 跑起来

需要 Python 3.10+，并且已经装好、登录过 Claude Code（命令行里能直接运行 `claude`）。

```bash
pip install -r requirements.txt
cp .env.example .env    # 填上你的 bot token 和 Telegram user id
set -a; . ./.env; set +a
python bot_example.py
```

在 Telegram 里给 bot 发 `/start`。

| 命令 | 作用 |
|---|---|
| `/start` | 记住这个聊天，开始心跳 |
| `/heartbeat` | 开 / 关心跳 |
| `/beat` | 在 A 和 B 之间切换 |
| `/new` | 开新对话 |

跑测试：

```bash
python -m unittest discover -s tests -t .
```

## 改时间和间隔

都在 `heartbeat.py` 的 `Config` 里：

```python
Config(
    window_start=dtime(8, 30),  # 白天从几点开始
    window_end=dtime(2, 0),     # 到几点结束，可以跨零点
    interval=55 * 60,           # A 的固定间隔，也是 B 白天的上限
    busy_retry=5 * 60,
    b_day_min=5 * 60,
    b_night_min=30 * 60,
    b_night_max=180 * 60,
)
```

时区用环境变量 `HEARTBEAT_TZ`，默认 `Asia/Shanghai`。

## 接到你自己的 bot 上

只需要在三个地方调用 `heartbeat.py`：

1. **发给 AI 的每条消息**：B 模式下末尾加上 `b_hint()`，告诉 AI 怎么写标记
2. **收到 AI 的回复**：用 `pop_tag()` 删掉标记，拿到下一次的间隔
3. **排下一次**：用 `next_delay()` 算出要等几秒；程序刚启动时用 `delay_on_restart()`

`bot_example.py` 就是照这个接的，可以照着改。

---

## English

**heartbeat** lets your AI text you first. Two modes: **A** sends on a fixed interval (55 min) during daytime only; **B** lets the AI pick its own next wake-up by ending each reply with `[heartbeat:N]` (5–55 min by day, 30–180 at night, never past the morning start). Any message from you resets the timer. The tag is stripped before you see the reply.

`heartbeat.py` is pure scheduling logic with no Telegram dependency; `bot_example.py` is a minimal Telegram + Claude Code CLI bot. See the Chinese sections above for the edge cases it handles (night alignment, sub-second spin, restarts, busy retry, broken-chain guard, untrusted N).

## License

MIT
