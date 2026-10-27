import html
import logging
import os
import random
import re
from pathlib import Path

from telegram import Message, MessageOriginChannel, ReactionTypeEmoji, Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    TypeHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # hides URLs containing the token
logger = logging.getLogger(__name__)


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.environ.get("BOT_TOKEN")
STATE_PATH = Path(
    os.environ.get("BOT_STATE_PATH", Path(__file__).with_name("bot-state.pkl"))
)

DEFAULT_N = 100              # highest accepted number when the post has just "/list"
MAX_N = 2000
ONE_NUMBER_PER_USER = True  # a user's newer (free) number replaces their older one

# /list                      -> numbers 1..DEFAULT_N
# /list 20                   -> numbers 1..20
# /list "Saturday football"  -> with description (quoted text can be anywhere in the post)
# /list 20 "Saturday football"
CMD_RE = re.compile(r"(?<!\S)/list(?:@\w+)?(?!\w)(?:[ \t]+(\d+)(?!\w))?")
QUOTES = "\"\u201c\u201d\u201e\u201f\u00ab\u00bb\u2033\uff02"  # " “ ” „ ‟ « » ″ ＂
DESC_RE = re.compile(rf"[{QUOTES}](.+?)[{QUOTES}]", re.DOTALL)
EMOJI_CODEPOINT_RANGES = (
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
)

# bot_data["lists"][(channel_id, post_id)] -> {"channel_id", "msg_id", "n", "desc", "slots"}
# bot_data["list_posts"] -> set of (channel_id, msg_id) of the list posts the bot made
# bot_data["threads"][(group_id, forwarded_msg_id)] -> (channel_id, post_id)


def display_name(user: dict) -> str:
    first = html.escape(user["first_name"])
    start, end = random.choice(EMOJI_CODEPOINT_RANGES)
    emoji = chr(random.randint(start, end))
    if user["username"]:
        return f"@{html.escape(user['username'])} ({first}) {emoji}"
    return f'<a href="tg://user?id={user["id"]}">{first}</a> {emoji}'


def render(desc: str | None, slots: dict) -> str:
    title = f"<b>{html.escape(desc)}</b>" if desc else "<b>📋 List</b>"
    lines = [title]
    if slots:
        lines.append("")
        for num in sorted(slots):
            lines.append(f"{num}. {display_name(slots[num])}")
    return "\n".join(lines)


async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    await update.message.reply_text(f"Hello {update.effective_user.first_name}")


# ---------- 0. Debug: log every incoming update ----------
async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        logger.debug("Update without message: %s", list(update.to_dict()))
        return
    user = update.effective_user
    logger.debug(
        "chat=%s(%s) msg_id=%s thread=%s auto_fwd=%s reply_to=%s "
        "user_id=%s username=%r first_name=%r text=%r",
        msg.chat.type,
        msg.chat_id,
        msg.message_id,
        msg.message_thread_id,
        msg.is_automatic_forward,
        msg.reply_to_message.message_id if msg.reply_to_message else None,
        user.id if user else None,
        user.username if user else None,
        user.first_name if user else None,
        msg.text or msg.caption,
    )


# ---------- 1. A channel post containing /list -> bot makes a separate list post ----------
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post = update.channel_post
    if post is None:
        return

    text = post.text or post.caption or ""
    match = CMD_RE.search(text)
    logger.debug(
        "Channel post %s text=%r -> /list match=%s",
        post.message_id,
        text,
        bool(match),
    )
    if not match:
        return

    n = int(match.group(1)) if match.group(1) else DEFAULT_N
    n = max(1, min(n, MAX_N))
    desc_match = DESC_RE.search(text)
    desc = desc_match.group(1).strip() if desc_match else None

    lists = context.bot_data.setdefault("lists", {})
    key = (post.chat_id, post.message_id)
    if key in lists:
        return

    sent = await context.bot.send_message(
        chat_id=post.chat_id,
        text=render(desc, {}),
        parse_mode="HTML",
    )
    lists[key] = {
        "channel_id": post.chat_id,
        "msg_id": sent.message_id,
        "n": n,
        "desc": desc,
        "slots": {},
    }
    context.bot_data.setdefault("list_posts", set()).add((post.chat_id, sent.message_id))
    logger.info(
        "List created for post %s -> list message %s, N=%s, desc=%r",
        key,
        sent.message_id,
        n,
        desc,
    )


async def on_group_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if (
        message is None
        or message.text is None
        or message.is_automatic_forward
        or message.chat.type not in {"group", "supergroup"}
    ):
        return

    match = CMD_RE.search(message.text)
    if not match:
        return

    n = int(match.group(1)) if match.group(1) else DEFAULT_N
    n = max(1, min(n, MAX_N))
    desc_match = DESC_RE.search(message.text)
    desc = desc_match.group(1).strip() if desc_match else None
    key = (message.chat_id, message.message_id)
    lists = context.bot_data.setdefault("lists", {})
    if key in lists:
        return

    sent = await context.bot.send_message(
        chat_id=message.chat_id,
        text=render(desc, {}),
        parse_mode="HTML",
    )
    lists[key] = {
        "channel_id": message.chat_id,
        "msg_id": sent.message_id,
        "n": n,
        "desc": desc,
        "slots": {},
    }
    context.bot_data.setdefault("list_message_keys", {})[
        (message.chat_id, sent.message_id)
    ] = key
    logger.info(
        "Group list created for message %s -> list message %s, N=%s, desc=%r",
        key,
        sent.message_id,
        n,
        desc,
    )


# ---------- 2. Discussion group: map forwards to posts, kill comments of the list post ----------
async def on_auto_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.forward_origin is None:
        return
    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return

    channel_post = (origin.chat.id, origin.message_id)

    # The bot's own list post: delete its copy in the discussion group,
    # which removes the comment section from that post.
    if channel_post in context.bot_data.get("list_posts", set()):
        try:
            await message.delete()
            logger.info("Comments disabled for list post %s", channel_post)
        except BadRequest as e:
            logger.warning(
                "Could not delete forwarded list post %s. "
                "The bot may need Delete messages permission: %s",
                channel_post,
                e,
            )
        return

    threads = context.bot_data.setdefault("threads", {})
    threads[(message.chat_id, message.message_id)] = channel_post
    logger.debug(
        "Thread mapped: %s -> %s",
        (message.chat_id, message.message_id),
        channel_post,
    )


def resolve_post(message: Message, context: ContextTypes.DEFAULT_TYPE):
    """Return the list key this comment or reply belongs to."""
    reply = message.reply_to_message
    if reply is not None:
        group_list_key = context.bot_data.get("list_message_keys", {}).get(
            (reply.chat_id, reply.message_id)
        )
        if group_list_key is not None:
            return group_list_key
    if reply is not None and reply.is_automatic_forward:
        origin = reply.forward_origin
        if isinstance(origin, MessageOriginChannel):
            return origin.chat.id, origin.message_id
    if message.message_thread_id is not None:
        return context.bot_data.get("threads", {}).get(
            (message.chat_id, message.message_thread_id)
        )
    return None


# ---------- 3. Comments with a number -> update the list post ----------
async def on_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or message.text is None or user is None or user.is_bot:
        return
    if message.is_automatic_forward:
        return

    key = resolve_post(message, context)
    logger.debug("Comment from %r resolved post: %s", user.username, key)
    if key is None:
        logger.debug("Dropped comment: could not resolve its channel post")
        return

    data = context.bot_data.get("lists", {}).get(key)
    if data is None:
        logger.debug("Dropped comment: no /list exists for post %s", key)
        return

    match = re.fullmatch(r"\d+", message.text.strip())
    if not match:
        logger.debug("Dropped comment: comment must contain only a number")
        return
    num = int(match.group())
    if not 1 <= num <= data["n"]:
        logger.debug("Dropped number %s: outside 1..%s", num, data["n"])
        return

    slots = data["slots"]

    # First come, first served: updates are handled one at a time in arrival order,
    # so whoever's comment reaches the bot first keeps the number.
    taken = slots.get(num)
    if taken is not None:
        logger.debug(
            "Dropped number %s: already taken by %s",
            num,
            taken["username"] or taken["first_name"],
        )
        return

    if ONE_NUMBER_PER_USER:
        for k in [k for k, v in slots.items() if v["id"] == user.id]:
            del slots[k]

    slots[num] = {"id": user.id, "username": user.username, "first_name": user.first_name}
    logger.info("Saved number %s for user %s", num, user.id)

    try:
        await context.bot.set_message_reaction(
            chat_id=message.chat_id,
            message_id=message.message_id,
            reaction=ReactionTypeEmoji("❤"),
        )
    except BadRequest as e:
        logger.warning(
            "Could not react to accepted comment %s in chat %s: %s",
            message.message_id,
            message.chat_id,
            e,
        )

    try:
        await context.bot.edit_message_text(
            chat_id=data["channel_id"],
            message_id=data["msg_id"],
            text=render(data["desc"], slots),
            parse_mode="HTML",
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update %r", update, exc_info=context.error)


def build_application():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to the .env file.")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(
        filepath=str(STATE_PATH)
    )
    application = (
        ApplicationBuilder().token(TOKEN).persistence(persistence).build()
    )

    application.add_handler(TypeHandler(Update, debug_all), group=-2)
    application.add_handler(CommandHandler("hello", hello))
    application.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post)
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.SUPERGROUP & filters.IS_AUTOMATIC_FORWARD,
            on_auto_forward,
        ),
        group=-1,
    )
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_list),
        group=-1,
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
            on_comment,
        )
    )
    application.add_error_handler(error_handler)
    return application


if __name__ == "__main__":
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)