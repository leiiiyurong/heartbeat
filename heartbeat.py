"""heartbeat：让 AI 主动来找你的排程逻辑。

这里全是纯函数，不碰 Telegram、不碰模型，只回答一个问题：
“现在是 now，下一次心跳该等多少秒？”
所以可以接到任何聊天程序上，bot_example.py 是接 Telegram + Claude Code 的示例。

两种模式：
- A：固定间隔（默认 55 分钟），只在白天时段内发，夜里不打扰。
- B：AI 每次回复时在最后一行写 [心跳:N]，自己决定 N 分钟后再来。
     白天 N 夹在 5~55，夜里放宽到 30~180，落点越过白天起点就截到起点。
两种模式下，对方只要一说话，就从这次回复重新计时。
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta


@dataclass(frozen=True)
class Config:
    window_start: dtime = dtime(8, 30)  # 白天从几点开始
    window_end: dtime = dtime(2, 0)     # 白天到几点结束，可以跨零点
    interval: int = 55 * 60             # A 的固定间隔，也是 B 白天的上限
    busy_retry: int = 5 * 60            # AI 正忙着回别的消息时，过多久再试
    b_day_min: int = 5 * 60
    b_night_min: int = 30 * 60
    b_night_max: int = 180 * 60


DEFAULT = Config()

# 中英文写法都认，冒号全角半角都行，N 最多 4 位
TAG_RE = re.compile(r"^\[(?:心跳|heartbeat)\s*[:：]\s*(\d{1,4})\s*\]$", re.IGNORECASE)


def in_window(t, cfg=DEFAULT):
    """t（datetime.time）是不是在白天时段内。"""
    if cfg.window_start <= cfg.window_end:
        return cfg.window_start <= t <= cfg.window_end
    # 跨零点（比如 08:30 到次日 02:00）：晚于起点，或者早于终点
    return t >= cfg.window_start or t <= cfg.window_end


def next_window_start(now, cfg=DEFAULT):
    start = datetime.combine(now.date(), cfg.window_start, tzinfo=now.tzinfo)
    if start <= now:
        start += timedelta(days=1)
    return start


def _seconds_until(target, now):
    # 向上取整并且至少 1 秒：差零点几秒时如果截成 0，定时器会原地空转
    return max(1, math.ceil((target - now).total_seconds()))


def delay_a(now, interval=None, cfg=DEFAULT):
    """A 模式：现在在夜里就直接等到白天起点；在白天，等完 interval 还在白天就等 interval，否则也等到白天起点。"""
    interval = cfg.interval if interval is None else interval
    if not in_window(now.time(), cfg):
        return _seconds_until(next_window_start(now, cfg), now)
    if in_window((now + timedelta(seconds=interval)).time(), cfg):
        return interval
    return _seconds_until(next_window_start(now, cfg), now)


def delay_b(now, interval, cfg=DEFAULT):
    """B 模式：24 小时都跑；只有现在是夜里时，落点越过白天起点就截到起点。"""
    if in_window(now.time(), cfg):
        return interval
    return max(1, min(interval, _seconds_until(next_window_start(now, cfg), now)))


def next_delay(now, interval=None, mode="A", cfg=DEFAULT):
    interval = sane_interval(cfg.interval if interval is None else interval, cfg)
    if mode == "B":
        return delay_b(now, interval, cfg)
    return delay_a(now, interval, cfg)


def b_interval(minutes, now, cfg=DEFAULT):
    """把 AI 写的 N 分钟夹进允许范围：白天 b_day_min~interval，夜里 b_night_min~b_night_max。"""
    seconds = minutes * 60
    if in_window(now.time(), cfg):
        return min(cfg.interval, max(cfg.b_day_min, seconds))
    return min(cfg.b_night_max, max(cfg.b_night_min, seconds))


def pop_tag(reply, mode, now, cfg=DEFAULT):
    """从回复里删掉最后一行的 [心跳:N]，返回 (给人看的回复, 下次间隔秒数)。

    不管哪种模式都要删，不能让对方看到这行；只有 B 才真的采用 N。
    没写、写错，一律按 cfg.interval。
    """
    if not reply:
        return reply, cfg.interval
    lines = reply.rstrip().split("\n")
    m = TAG_RE.match(lines[-1].strip())
    if not m:
        return reply, cfg.interval
    reply = "\n".join(lines[:-1]).rstrip()
    if mode == "B":
        return reply, b_interval(int(m.group(1)), now, cfg)
    return reply, cfg.interval


def sane_interval(seconds, cfg=DEFAULT):
    """空的、不是数字、≤0、比夜里上限还长的，一律回退到 cfg.interval。"""
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0 or seconds > cfg.b_night_max):
        return cfg.interval
    return seconds


def delay_on_restart(now, last_sent_at, mode="A", cfg=DEFAULT):
    """程序刚启动时用：按“上次真正发出去的时间”接着排，而不是从启动那一刻重新数。

    - 从没发过（last_sent_at 是 None）：从现在起等一个完整间隔。
    - 本该发的时间已经过了：马上补一条（A 模式夜里除外，要等到白天）。
    - 还没到：照常等到那个点。
    不这么做的话，一小时内重启几次，心跳会被一次次往后推，永远发不出来。
    """
    if last_sent_at is None:
        due = now + timedelta(seconds=cfg.interval)
    else:
        due = max(now, last_sent_at + timedelta(seconds=cfg.interval))
    wait = max(1, math.ceil((due - now).total_seconds()))
    if mode == "B":
        return delay_b(now, wait, cfg)
    if not in_window(now.time(), cfg):
        return _seconds_until(next_window_start(now, cfg), now)
    if in_window(due.time(), cfg):
        return wait
    return _seconds_until(next_window_start(now, cfg), now)


def b_hint(cfg=DEFAULT):
    """B 模式下附在发给 AI 的每条消息末尾，告诉它怎么写标记。"""
    def hm(t):
        return t.strftime("%H:%M")
    return (
        f"\n\n（心跳 B：回复最后一行单独写 [心跳:N]，N 为分钟数，定下次主动找对方的间隔："
        f"{hm(cfg.window_start)}–{hm(cfg.window_end)} 为 {cfg.b_day_min // 60}–{cfg.interval // 60}；"
        f"其余时间为 {cfg.b_night_min // 60}–{cfg.b_night_max // 60}，落点越过 {hm(cfg.window_start)} 会截到 {hm(cfg.window_start)}。"
        f"这一行会被删掉，对方看不到。）"
    )
