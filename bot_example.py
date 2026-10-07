"""最小可跑示例：Telegram bot + Claude Code CLI + 心跳。

一个人用的私聊 bot。你发消息，它调 `claude -p` 回复；你不说话时，它按心跳主动找你。

命令：
  /start      记住这个聊天，开始心跳
  /heartbeat  开/关心跳
  /beat       在 A（固定间隔）和 B（AI 自己定下次时间）之间切换
  /new        开新对话

环境变量见 .env.example。
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import heartbeat as hb

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
TZ = ZoneInfo(os.environ.get("HEARTBEAT_TZ", "Asia/Shanghai"))
STATE_FILE = os.environ.get("HEARTBEAT_STATE_FILE", "heartbeat_state.json")
CLAUDE_TIMEOUT = 600

HEARTBEAT_PROMPT = (
    "[心跳] 主动给对方发一条消息。说你真正想说的，语气跟平时聊天一样，"
    "不要提这是自动消息。"
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("heartbeat")

busy = asyncio.Lock()  # 同一时间只跑一个 claude，心跳撞上正在回的消息就稍后再试


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"chat_id": None, "enabled": True, "mode": "A", "session_id": None, "last_sent_at": None}


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


state = load_state()


def now():
    return datetime.now(TZ)


def ask_claude(msg):
    """同步调用 Claude Code CLI，接着上一轮对话。返回 (回复文本, session_id)。"""
    if state.get("mode") == "B":
        msg += hb.b_hint()
    cmd = ["claude", "-p", msg, "--output-format", "json"]
    if state.get("session_id"):
        cmd += ["--resume", state["session_id"]]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:500])
    data = json.loads(out.stdout)
    return data.get("result", ""), data.get("session_id")


async def run_claude(msg):
    reply, session_id = await asyncio.to_thread(ask_claude, msg)
    if session_id:
        state["session_id"] = session_id
        save_state()
    return hb.pop_tag(reply, state.get("mode", "A"), now())


# ---- 排程 ----

def schedule(job_queue, interval=None):
    """清掉旧的心跳任务，排下一次。"""
    for job in job_queue.get_jobs_by_name("heartbeat"):
        job.schedule_removal()
    delay = hb.next_delay(now(), interval, state.get("mode", "A"))
    job_queue.run_once(heartbeat_tick, when=delay, name="heartbeat")
    log.info("下一次心跳 %d 秒后", delay)


def schedule_safely(job_queue, interval=None):
    """排程本身出错时用固定间隔兜底，保证链不断：不然开关显示开着，心跳却再也不来。"""
    try:
        schedule(job_queue, interval)
        return
    except Exception:
        log.exception("排下一次心跳失败，改用兜底间隔")
    try:
        for job in job_queue.get_jobs_by_name("heartbeat"):
            job.schedule_removal()
        job_queue.run_once(heartbeat_tick, when=hb.DEFAULT.interval, name="heartbeat")
    except Exception:
        log.exception("兜底也失败了，心跳链已断，需要重启或 /heartbeat 关了再开")


def schedule_on_startup(job_queue):
    last = state.get("last_sent_at")
    last = datetime.fromisoformat(last) if last else None
    delay = hb.delay_on_restart(now(), last, state.get("mode", "A"))
    job_queue.run_once(heartbeat_tick, when=delay, name="heartbeat")
    log.info("启动：下一次心跳 %d 秒后", delay)


async def heartbeat_tick(context: ContextTypes.DEFAULT_TYPE):
    next_interval = None
    try:
        if not state.get("enabled", True) or not state.get("chat_id"):
            return
        mode = state.get("mode", "A")
        if mode == "A" and not hb.in_window(now().time()):
            return
        if busy.locked():
            next_interval = hb.DEFAULT.busy_retry
            return
        async with busy:
            reply, next_interval = await run_claude(HEARTBEAT_PROMPT)
        if reply:
            await context.bot.send_message(chat_id=state["chat_id"], text=reply[:4096])
            state["last_sent_at"] = now().isoformat()
            save_state()
    except Exception:
        log.exception("心跳出错")
    finally:
        # 不管这一棒成没成功，都要排下一棒
        schedule_safely(context.job_queue, next_interval)


# ---- 消息和命令 ----

def allowed(update: Update):
    return update.effective_user and update.effective_user.id == ALLOWED_USER_ID


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    state["chat_id"] = update.effective_chat.id
    save_state()
    next_interval = None
    try:
        async with busy:
            reply, next_interval = await run_claude(update.message.text)
    except Exception as e:
        log.exception("回复出错")
        reply = f"(出错了：{e})"
    if reply:
        await update.message.reply_text(reply[:4096])
    # 对方说话了，从这次回复重新计时
    schedule_safely(context.job_queue, next_interval)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    state["chat_id"] = update.effective_chat.id
    state["enabled"] = True
    save_state()
    schedule_safely(context.job_queue)
    await update.message.reply_text("心跳已开启")


async def cmd_heartbeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    state["enabled"] = not state.get("enabled", True)
    save_state()
    # 关着的时候链照样空转；万一链断过，打开时补排一棒，让开关和真实排程对得上
    if state["enabled"] and not context.job_queue.get_jobs_by_name("heartbeat"):
        schedule_safely(context.job_queue)
    await update.message.reply_text("心跳已" + ("开启" if state["enabled"] else "关闭"))


async def cmd_beat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    state["mode"] = "B" if state.get("mode", "A") == "A" else "A"
    save_state()
    desc = "固定间隔" if state["mode"] == "A" else "AI 每次回复自己定下次时间"
    await update.message.reply_text(f"已切换到心跳 {state['mode']}：{desc}")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    state["session_id"] = None
    save_state()
    await update.message.reply_text("已开启新对话")


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("heartbeat", cmd_heartbeat))
    app.add_handler(CommandHandler("beat", cmd_beat))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    schedule_on_startup(app.job_queue)
    app.run_polling()


if __name__ == "__main__":
    main()
