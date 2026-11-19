"""
Channel Link Sorter Bot (Bot-Only, No User Session)
===================================================

* Uses ONLY the Bot Token (no user session, no phone/OTP login).
* Does NOT check whether links are expired/accessible - extracts ALL Telegram
  links present in the channel immediately.
* Separates regular channel/group links from folder links starting with
  `https://t.me/addlist/`.
* Deduplicates links and never repeats links already sent to the admin.
* Numbers every link (1., 2., 3....) separated into 5-by-5 blocks, with a
  separate interactive checklist button for each 5-link block (1-5, 6-10, ...).

How to scan a channel without a user session:
1. Add the bot as an Admin (or Member) to your channel (e.g. "Mega premium"),
   then forward the latest message from that channel to the bot (or send a post
   link like https://t.me/c/123456789/500 or /scan @channelname). The bot reads
   all messages (1..latest) directly by ID using only the Bot Token.
2. Or simply select & forward messages from any channel directly to the bot
   (or upload a Telegram exported .html / .json / .txt file).
"""

import asyncio
import html
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from telethon import Button, TelegramClient, errors, events, types

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv()

DATA_DIR = os.path.abspath(os.getenv("DATA_DIR") or BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

# Public app credentials required by Telethon to open the bot connection
API_ID = int(os.getenv("API_ID") or 2040)
API_HASH = os.getenv("API_HASH") or "b18441a1ff607e10a989891a5462e627"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Mega premium").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()    # optional @username or -100...
OWNER_ID = int(os.getenv("OWNER_ID") or 0)          # 0 = first user becomes owner
INCLUDE_MENTIONS = os.getenv("INCLUDE_MENTIONS", "false").lower() == "true"

BOT_SESSION = os.path.join(DATA_DIR, "bot")
OWNER_FILE = os.path.join(DATA_DIR, "owner.json")
SENT_FILE = os.path.join(DATA_DIR, "sent_links.json")
TARGET_FILE = os.path.join(DATA_DIR, "target_channel.json")

LINKS_PER_MESSAGE = 20   # 4 blocks of 5 links per message

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("link_sorter")

# --------------------------------------------------------------------------- #
# Link extraction / parsing
# --------------------------------------------------------------------------- #
URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"'()\[\]{}]+"
    r"|tg://(?:resolve|join|addlist)\?[^\s<>\"'()\[\]{}]+",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

# t.me/<word> paths that are NOT channels or addlists
RESERVED = {
    "joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "iv",
    "login", "setlanguage", "addtheme", "bg", "confirmphone",
    "invoice", "contact", "giftcode", "nft", "m", "c", "s", "boost", "a", "k", "z",
}


@dataclass(frozen=True)
class Link:
    kind: str    # "username" | "invite" | "private_id" | "addlist"
    value: str
    url: str

    @property
    def is_addlist(self) -> bool:
        return self.kind == "addlist"

    @property
    def key(self) -> str:
        v = self.value.lower() if self.kind == "username" else self.value
        return f"{self.kind}:{v}"


def parse_link(raw: str) -> Link | None:
    """Turn a raw link / mention into a normalised Link (or None)."""
    raw = raw.strip().rstrip(".,;:!?*_~|")
    if not raw:
        return None

    if raw.startswith("@"):
        name = raw[1:]
        if USERNAME_RE.match(name):
            return Link("username", name, f"https://t.me/{name}")
        return None

    if raw.lower().startswith("tg://"):
        parsed = urlparse(raw)
        q = parse_qs(parsed.query)
        host = parsed.netloc.lower()
        if host == "addlist" and "slug" in q and q["slug"][0]:
            slug = q["slug"][0]
            return Link("addlist", slug, f"https://t.me/addlist/{slug}")
        if "invite" in q and q["invite"][0]:
            h = q["invite"][0]
            return Link("invite", h, f"https://t.me/+{h}")
        if "domain" in q and USERNAME_RE.match(q["domain"][0]):
            name = q["domain"][0]
            return Link("username", name, f"https://t.me/{name}")
        return None

    path = re.sub(r"^(?:https?://)?(?:www\.)?[^/]+/", "", raw, flags=re.I)
    path = path.split("?")[0].split("#")[0].strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    first = parts[0]

    # Folder / Addlist links: https://t.me/addlist/<slug>
    if first.lower() == "addlist" and len(parts) > 1 and parts[1]:
        slug = parts[1]
        return Link("addlist", slug, f"https://t.me/addlist/{slug}")

    # Private invite links: t.me/+HASH or t.me/joinchat/HASH
    if first.startswith("+") or first.lower().startswith("%2b"):
        h = first[1:] if first.startswith("+") else first[3:]
        if h and not h.isdigit():            # all digits = phone number link
            return Link("invite", h, f"https://t.me/+{h}")
        return None
    if first.lower() == "joinchat" and len(parts) > 1 and parts[1]:
        return Link("invite", parts[1], f"https://t.me/+{parts[1]}")

    # Private channel post links: t.me/c/<channel_id>/<msg_id>
    if first.lower() == "c" and len(parts) > 1 and parts[1].isdigit():
        cid = parts[1]
        url = f"https://t.me/c/{cid}/{parts[2]}" if len(parts) > 2 and parts[2].isdigit() else f"https://t.me/c/{cid}"
        return Link("private_id", cid, url)

    # Preview / boost links: t.me/s/<username>, t.me/boost/<username>
    if first.lower() in {"s", "boost"} and len(parts) > 1:
        first = parts[1]
    elif first.lower() in RESERVED:
        return None

    if USERNAME_RE.match(first):
        return Link("username", first, f"https://t.me/{first}")
    return None


def extract_raw_links_from_text(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def extract_raw_links(msg) -> list[str]:
    """Extract all Telegram link strings from message text, entities, buttons, webpage."""
    if not msg:
        return []
    found: list[str] = []
    text = getattr(msg, "message", None) or ""
    found += URL_RE.findall(text)

    try:
        for ent, ent_text in msg.get_entities_text():
            if isinstance(ent, types.MessageEntityTextUrl):
                found.append(ent.url)
            elif isinstance(ent, types.MessageEntityUrl):
                found.append(ent_text)
            elif INCLUDE_MENTIONS and isinstance(ent, types.MessageEntityMention):
                found.append(ent_text)
    except Exception:  # noqa: BLE001
        pass

    markup = getattr(msg, "reply_markup", None)
    if isinstance(markup, types.ReplyInlineMarkup):
        for row in markup.rows:
            for btn in row.buttons:
                url = getattr(btn, "url", None)
                if url:
                    found.append(url)

    media = getattr(msg, "media", None)
    if isinstance(media, types.MessageMediaWebPage):
        url = getattr(media.webpage, "url", None)
        if url:
            found.append(url)

    return [
        u for u in found
        if u.startswith("@") or URL_RE.match(u.strip())
    ]


# --------------------------------------------------------------------------- #
# Persistent State (Sent Links & 5-by-5 Checklists)
# --------------------------------------------------------------------------- #
# Two separate buckets:
#   "ch"  -> Regular channel / group links
#   "al"  -> https://t.me/addlist/... folder links
STATE: dict = {}


def _empty_bucket() -> dict:
    return {
        "counter": 0,
        "done": set(),   # set of completed link numbers (1, 2, 3...)
        "items": [],     # [{"n": 1, "url": "..."}]
    }


def load_sent() -> dict:
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    STATE.clear()
    STATE["keys"] = set(data.get("keys", []))

    # Backward compatibility if old sent_links.json format exists
    for kind in ("ch", "al"):
        raw_b = data.get(kind)
        if raw_b is None and kind == "ch" and "items" in data:
            done_till = int(data.get("done_till", 0))
            STATE[kind] = {
                "counter": int(data.get("counter", 0)),
                "done": set(range(1, done_till + 1)),
                "items": [{"n": it["n"], "url": it["url"]} for it in data.get("items", [])],
            }
        elif raw_b:
            STATE[kind] = {
                "counter": int(raw_b.get("counter", 0)),
                "done": set(int(x) for x in raw_b.get("done", [])),
                "items": list(raw_b.get("items", [])),
            }
        else:
            STATE[kind] = _empty_bucket()
    return STATE


def save_sent() -> None:
    out = {
        "keys": sorted(STATE["keys"]),
        "ch": {
            "counter": STATE["ch"]["counter"],
            "done": sorted(STATE["ch"]["done"]),
            "items": STATE["ch"]["items"],
        },
        "al": {
            "counter": STATE["al"]["counter"],
            "done": sorted(STATE["al"]["done"]),
            "items": STATE["al"]["items"],
        },
    }
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


def load_target() -> dict:
    try:
        with open(TARGET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_target(chat_id: int | str, title: str = "", max_id: int = 0) -> None:
    cur = load_target()
    if str(cur.get("chat_id")) == str(chat_id):
        max_id = max(int(cur.get("max_id") or 0), max_id)
        title = title or cur.get("title", "")
    with open(TARGET_FILE, "w", encoding="utf-8") as f:
        json.dump({"chat_id": chat_id, "title": title, "max_id": max_id}, f)


def load_owner() -> int:
    if OWNER_ID:
        return OWNER_ID
    try:
        with open(OWNER_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f)["owner_id"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return 0


def save_owner(uid: int) -> None:
    with open(OWNER_FILE, "w", encoding="utf-8") as f:
        json.dump({"owner_id": uid}, f)


# --------------------------------------------------------------------------- #
# Formatting & 5-by-5 Checklist Buttons
# --------------------------------------------------------------------------- #
def format_items(kind: str, items: list[dict]) -> str:
    """Format links numbered 1., 2., 3. with an empty line after every 5 links."""
    done = STATE[kind]["done"]
    header = (
        "📁 <b>Addlist / Folder Links (https://t.me/addlist/)</b>"
        if kind == "al"
        else "📢 <b>Telegram Channel / Group Links</b>"
    )
    lines = [header, ""]
    for i, it in enumerate(items):
        n = it["n"]
        if i > 0 and i % 5 == 0:
            lines.append("")
        mark = "✅ " if n in done else ""
        lines.append(f"{mark}<b>{n}.</b> {html.escape(it['url'])}")
    return "\n".join(lines)


def item_buttons(kind: str, items: list[dict]):
    """Separate checklist button for each 5-link group (e.g. 1-5, 6-10, 11-15, 16-20)
    plus a button to toggle all links in the message."""
    done = STATE[kind]["done"]
    first, last = items[0]["n"], items[-1]["n"]

    # Split items into groups of 5
    groups = [items[i:i + 5] for i in range(0, len(items), 5)]
    btns = []
    for grp in groups:
        g_first, g_last = grp[0]["n"], grp[-1]["n"]
        all_checked = all(it["n"] in done for it in grp)
        icon = "✅" if all_checked else "⬜"
        label = f"{icon} {g_first}–{g_last}" if g_first != g_last else f"{icon} #{g_first}"
        btns.append(
            Button.inline(label, f"chk:{kind}:{g_first}:{g_last}:{first}:{last}".encode())
        )

    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    all_msg_checked = all(it["n"] in done for it in items)
    all_label = (
        f"☑️ Uncheck All ({first}–{last})"
        if all_msg_checked
        else f"✅ Check All ({first}–{last})"
    )
    rows.append([Button.inline(all_label, f"chk:{kind}:{first}:{last}:{first}:{last}".encode())])
    return rows


async def send_items(bot, chat_id: int, kind: str, items: list[dict]) -> None:
    for i in range(0, len(items), LINKS_PER_MESSAGE):
        part = items[i:i + LINKS_PER_MESSAGE]
        while True:
            try:
                await bot.send_message(
                    chat_id,
                    format_items(kind, part),
                    buttons=item_buttons(kind, part),
                    link_preview=False,
                )
                break
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
        await asyncio.sleep(0.8)


async def deliver_links(bot, chat_id: int, links: list[Link]) -> tuple[list[dict], list[dict]]:
    """Assign numbers, persist to sent_links.json, and send Channel links and
    Addlist links separately in 5-by-5 blocks with checklists."""
    new_ch: list[dict] = []
    new_al: list[dict] = []

    for link in links:
        if link.key in STATE["keys"]:
            continue
        STATE["keys"].add(link.key)
        kind = "al" if link.is_addlist else "ch"
        STATE[kind]["counter"] += 1
        it = {"n": STATE[kind]["counter"], "url": link.url}
        STATE[kind]["items"].append(it)
        if kind == "al":
            new_al.append(it)
        else:
            new_ch.append(it)

    if new_ch or new_al:
        save_sent()
    if new_ch:
        await send_items(bot, chat_id, "ch", new_ch)
    if new_al:
        await send_items(bot, chat_id, "al", new_al)
    return new_ch, new_al


def make_txt_report(items: list[dict]) -> bytes:
    """Format plain text report with 5-by-5 spacing."""
    lines = []
    for i, it in enumerate(items):
        if i > 0 and i % 5 == 0:
            lines.append("")
        lines.append(f"{it['n']}. {it['url']}")
    return "\n".join(lines).encode("utf-8")


class Progress:
    def __init__(self, message):
        self.message = message
        self.last = 0.0

    async def __call__(self, text: str, force: bool = False):
        now = time.monotonic()
        if not force and now - self.last < 3.5:
            return
        self.last = now
        try:
            await self.message.edit(text)
        except (errors.MessageNotModifiedError, errors.FloodWaitError):
            pass


# --------------------------------------------------------------------------- #
# Bot-Only Channel Scanner (fetches messages by ID 1..max_id)
# --------------------------------------------------------------------------- #
POST_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:c/(\d+)|([A-Za-z][A-Za-z0-9_]{3,31}))(?:/(\d+))?",
    re.IGNORECASE,
)


def parse_target_arg(arg: str) -> tuple[int | str | None, int]:
    """Parse '/scan @channel', '/scan -1001234', or 'https://t.me/c/1234/567'."""
    arg = arg.strip()
    if not arg:
        return None, 0
    m = POST_LINK_RE.search(arg)
    if m:
        cid, uname, msg_id = m.group(1), m.group(2), int(m.group(3) or 0)
        if cid:
            return int(f"-100{cid}"), msg_id
        if uname and uname.lower() not in RESERVED and uname.lower() != "addlist":
            return uname, msg_id
    if arg.startswith("@") and USERNAME_RE.match(arg[1:]):
        return arg[1:], 0
    if re.fullmatch(r"-?\d{6,20}", arg):
        val = int(arg)
        if val > 0:
            val = int(f"-100{val}")
        return val, 0
    if USERNAME_RE.match(arg):
        return arg, 0
    return None, 0


async def scan_channel_by_ids(bot, chat_id: int, target: int | str, max_hint: int,
                              progress: Progress, cancel_event: asyncio.Event):
    """Fetch all messages in `target` by ID (1..max) in chunks of 100 using ONLY
    the Bot Token, extract all links (without checking expiration), and send
    5-by-5 lists + checklists."""
    try:
        entity = await bot.get_entity(target)
        title = html.escape(getattr(entity, "title", None) or str(target))
        own_username = (getattr(entity, "username", None) or "").lower()
    except Exception as e:  # noqa: BLE001
        await progress(
            f"❌ Cannot access channel <code>{html.escape(str(target))}</code> "
            f"({e.__class__.__name__}).\n\n"
            "👉 <b>For a private channel:</b>\n"
            "1. Add this bot (@Link_sorter_A1_bot) as an <b>Admin</b> in the channel, "
            "then forward the latest message from that channel here.\n"
            "2. <b>Or</b> if you cannot add the bot to the channel, simply select & "
            "forward the channel's messages directly to this chat (or upload a chat export file)!",
            True,
        )
        return

    await progress(f"📥 Scanning messages from <b>{title}</b>…", True)

    links: dict[str, Link] = {}
    already_sent = 0
    scanned_msgs = 0
    cur_id = 1
    empty_streak = 0
    BATCH = 100
    # Stop when we pass max_hint and see 500 consecutive missing IDs
    MAX_EMPTY_STREAK = 500

    while not cancel_event.is_set():
        ids = list(range(cur_id, cur_id + BATCH))
        while True:
            try:
                msgs = await bot.get_messages(entity, ids=ids)
                break
            except errors.FloodWaitError as e:
                await progress(
                    f"⏳ Telegram rate limit while reading messages – waiting {e.seconds}s…",
                    True,
                )
                await asyncio.sleep(e.seconds + 1)

        found_in_batch = 0
        for msg in msgs:
            if not msg or isinstance(msg, types.MessageEmpty):
                continue
            found_in_batch += 1
            scanned_msgs += 1
            if msg.id > max_hint:
                max_hint = msg.id
            for raw in extract_raw_links(msg):
                link = parse_link(raw)
                if not link or link.key in links:
                    continue
                if link.kind == "username" and link.value.lower() == own_username:
                    continue
                if link.key in STATE["keys"]:
                    already_sent += 1
                    continue
                links[link.key] = link

        cur_id += BATCH
        if found_in_batch == 0:
            empty_streak += BATCH
            if cur_id > max_hint and empty_streak >= MAX_EMPTY_STREAK:
                break
        else:
            empty_streak = 0

        if (cur_id - 1) % 500 == 0:
            await progress(
                f"📥 <b>{title}</b>: checked up to post #{cur_id - 1} "
                f"({scanned_msgs} posts found, {len(links)} new links)…"
            )

    save_target(target, getattr(entity, "title", "") or str(target), max_hint)
    await finish_extraction(bot, chat_id, title, scanned_msgs, list(links.values()), already_sent, progress)


async def finish_extraction(bot, chat_id: int, source_name: str, scanned_msgs: int,
                            link_list: list[Link], already_sent: int, progress: Progress):
    new_ch, new_al = await deliver_links(bot, chat_id, link_list)

    summary = (
        f"📊 <b>{source_name}</b>\n"
        f"Messages processed: {scanned_msgs}\n"
        f"📢 New Channel/Group links: <b>{len(new_ch)}</b>\n"
        f"📁 New Addlist (Folder) links: <b>{len(new_al)}</b>\n"
        f"🔁 Already sent skipped: {already_sent}"
    )
    await progress(summary, True)

    if not new_ch and not new_al:
        await bot.send_message(
            chat_id,
            "No new links found (all links were already sent before, or use /resetsent to resend).",
        )
        return

    if new_ch:
        f = io.BytesIO(make_txt_report(new_ch))
        f.name = "channel_links.txt"
        await bot.send_file(
            chat_id, f,
            caption=f"📢 {len(new_ch)} Channel/Group links (#{new_ch[0]['n']}–#{new_ch[-1]['n']})",
        )

    if new_al:
        f = io.BytesIO(make_txt_report(new_al))
        f.name = "addlist_links.txt"
        await bot.send_file(
            chat_id, f,
            caption=f"📁 {len(new_al)} Addlist/Folder links (#{new_al[0]['n']}–#{new_al[-1]['n']})",
        )


# --------------------------------------------------------------------------- #
# Bot Handlers
# --------------------------------------------------------------------------- #
HELP = (
    "👋 <b>Channel Link Sorter</b> (No User Login Required)\n\n"
    "Extracts <b>all</b> Telegram links without checking expiration, "
    "separates <code>https://t.me/addlist/</code> links, numbers them "
    "<b>1., 2., 3.…</b> in <b>5-by-5</b> blocks, and adds a <b>5-by-5 checklist</b>.\n\n"
    "<b>3 Ways to Extract Links from a Channel:</b>\n"
    "1️⃣ <b>Forward messages</b> from the channel directly to this bot (1 message or 100+ at once).\n"
    "2️⃣ <b>Scan whole channel automatically:</b> add this bot as an Admin in the channel, "
    "then forward its latest post here (or send a post link / <code>/scan @channel</code>).\n"
    "3️⃣ <b>Upload a file:</b> send an exported <code>.html</code>, <code>.json</code>, or <code>.txt</code> file.\n\n"
    "<b>Commands:</b>\n"
    "/scan – scan saved channel (or <code>/scan &lt;post_link or @channel&gt;</code>)\n"
    "/progress – check completed vs pending counts\n"
    "/pending – resend uncompleted links\n"
    "/done 25 – mark channel links 1–25 completed (<code>/done addlist 10</code> for addlists)\n"
    "/resetsent – clear history & restart numbering at 1\n"
    "/cancel – stop a running scan"
)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Put BOT_TOKEN in the .env file.")

    load_sent()
    if CHANNEL_ID:
        cid, hint = parse_target_arg(CHANNEL_ID)
        if cid:
            save_target(cid, CHANNEL_NAME, hint)

    bot = TelegramClient(
        BOT_SESSION, API_ID, API_HASH,
        connection_retries=None, retry_delay=5, auto_reconnect=True,
    )
    bot.parse_mode = "html"
    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info("Bot running as @%s (bot-only mode, no user session)", bot_me.username)

    owner = {"id": load_owner()}
    scan_lock = asyncio.Lock()
    cancel_event = asyncio.Event()

    # Buffer for bursts of forwarded/pasted messages
    forward_buf: dict[int, list] = {}
    forward_tasks: dict[int, asyncio.Task] = {}

    def is_owner(event) -> bool:
        if owner["id"] == 0 and event.sender_id:
            owner["id"] = event.sender_id
            save_owner(event.sender_id)
        return owner["id"] == 0 or event.sender_id == owner["id"]

    async def deny(event):
        await event.reply("⛔ This bot is private.")

    # Auto-record channel ID if the bot is added to a channel or sees a channel post
    @bot.on(events.NewMessage(func=lambda e: e.is_channel and not e.is_group))
    async def on_channel_post(event):
        chat = await event.get_chat()
        title = getattr(chat, "title", "") or ""
        save_target(event.chat_id, title, event.id)
        log.info("Detected channel %s (%s) post #%s", title, event.chat_id, event.id)

    # ---------------- /start & /help ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/(start|help)"))
    async def on_start(event):
        if not event.is_private:
            return
        if not is_owner(event):
            return await deny(event)
        tgt = load_target()
        if tgt.get("chat_id"):
            t_info = f"\n\n🎯 Saved channel: <b>{html.escape(tgt.get('title') or str(tgt['chat_id']))}</b> (<code>{tgt['chat_id']}</code>)"
        else:
            t_info = "\n\n💡 <i>Forward messages from your channel here to extract all links!</i>"
        await event.reply(HELP + t_info, link_preview=False)

    # ---------------- /cancel ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/cancel"))
    async def on_cancel(event):
        if not is_owner(event):
            return
        if scan_lock.locked():
            cancel_event.set()
            await event.reply("🛑 Stopping scan…")
        else:
            await event.reply("Nothing is running.")

    # ---------------- /resetsent ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/resetsent"))
    async def on_resetsent(event):
        if not is_owner(event):
            return await deny(event)
        if scan_lock.locked():
            return await event.reply("⏳ A scan is running. /cancel it first.")
        ch_n, al_n = STATE["ch"]["counter"], STATE["al"]["counter"]
        if os.path.exists(SENT_FILE):
            os.remove(SENT_FILE)
        load_sent()
        await event.reply(
            f"🗑 Cleared history ({ch_n} channel links, {al_n} addlist links).\n"
            "Numbering restarts at 1."
        )

    # ---------------- 5-by-5 Checklist Button Callback ---------------- #
    @bot.on(events.CallbackQuery(pattern=rb"^chk:(ch|al):(\d+):(\d+):(\d+):(\d+)$"))
    async def on_checklist_button(event):
        if not is_owner(event):
            return await event.answer("⛔ Not allowed", alert=True)
        kind = event.pattern_match.group(1).decode()
        g_first = int(event.pattern_match.group(2))
        g_last = int(event.pattern_match.group(3))
        m_first = int(event.pattern_match.group(4))
        m_last = int(event.pattern_match.group(5))

        done = STATE[kind]["done"]
        rng = list(range(g_first, g_last + 1))
        if all(n in done for n in rng):
            for n in rng:
                done.discard(n)
            toast = f"⬜ Unchecked {g_first}–{g_last}"
        else:
            for n in rng:
                done.add(n)
            toast = f"✅ Completed {g_first}–{g_last}"
        save_sent()

        items = [it for it in STATE[kind]["items"] if m_first <= it["n"] <= m_last]
        if items:
            try:
                await event.edit(
                    format_items(kind, items),
                    buttons=item_buttons(kind, items),
                    link_preview=False,
                )
            except errors.MessageNotModifiedError:
                pass
        await event.answer(toast)

    # ---------------- /done, /progress, /pending ---------------- #
    def progress_summary() -> str:
        lines = []
        for kind, label in (("ch", "📢 Channel/Group Links"), ("al", "📁 Addlist Links")):
            total = STATE[kind]["counter"]
            done_cnt = len(STATE[kind]["done"])
            pend_cnt = max(0, total - done_cnt)
            lines.append(
                f"<b>{label}</b>\n"
                f"Total: {total}  |  ✅ Done: {done_cnt}  |  ⬜ Pending: {pend_cnt}"
            )
            nxt = next((it for it in STATE[kind]["items"] if it["n"] not in STATE[kind]["done"]), None)
            if nxt:
                lines.append(f"➡️ Next pending: <b>{nxt['n']}.</b> {html.escape(nxt['url'])}")
            lines.append("")
        return "\n".join(lines).strip()

    @bot.on(events.NewMessage(pattern=r"^/done(?:@\w+)?(?:\s+(addlist|al))?(?:\s+(\d+))?\s*$"))
    async def on_done(event):
        if not is_owner(event):
            return await deny(event)
        is_al = bool(event.pattern_match.group(1))
        num_str = event.pattern_match.group(2)
        if not num_str:
            return await event.reply(
                "Usage:\n"
                "<code>/done 25</code> – mark Channel links 1–25 completed\n"
                "<code>/done addlist 10</code> – mark Addlist links 1–10 completed"
            )
        kind = "al" if is_al else "ch"
        n = min(int(num_str), STATE[kind]["counter"])
        STATE[kind]["done"].update(range(1, n + 1))
        save_sent()
        await event.reply(progress_summary(), link_preview=False)

    @bot.on(events.NewMessage(pattern=r"^/progress"))
    async def on_progress(event):
        if not is_owner(event):
            return await deny(event)
        await event.reply(progress_summary(), link_preview=False)

    @bot.on(events.NewMessage(pattern=r"^/pending"))
    async def on_pending(event):
        if not is_owner(event):
            return await deny(event)
        any_pending = False
        for kind in ("ch", "al"):
            items = [it for it in STATE[kind]["items"] if it["n"] not in STATE[kind]["done"]]
            if items:
                any_pending = True
                await send_items(bot, event.chat_id, kind, items)
        if not any_pending:
            await event.reply("🎉 Nothing pending – all links are marked completed!")

    # ---------------- /scan ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/scan(?:@\w+)?(.*)$"))
    async def on_scan(event):
        if not event.is_private:
            return
        if not is_owner(event):
            return await deny(event)
        if scan_lock.locked():
            return await event.reply("⏳ A scan is already running. Use /cancel to stop it.")

        arg = (event.pattern_match.group(1) or "").strip()
        target, max_hint = parse_target_arg(arg)
        if not target:
            saved = load_target()
            target = saved.get("chat_id")
            max_hint = int(saved.get("max_id") or 0)

        if not target:
            return await event.reply(
                "📌 <b>How to extract all links from your channel:</b>\n\n"
                "• <b>Option A:</b> Select & forward messages from the channel directly to this chat.\n"
                "• <b>Option B:</b> Add @Link_sorter_A1_bot as an Admin in the channel, "
                "then forward the latest message from that channel (or send "
                "<code>/scan https://t.me/c/XXXX/YYYY</code> or <code>/scan @channel</code>).\n"
                "• <b>Option C:</b> Upload a Telegram chat export file (<code>.html</code> / <code>.json</code> / <code>.txt</code>).",
                link_preview=False,
            )

        async with scan_lock:
            cancel_event.clear()
            status = await event.reply("🔎 Starting channel scan…")
            progress = Progress(status)
            await scan_channel_by_ids(bot, event.chat_id, target, max_hint, progress, cancel_event)

    # ---------------- Exported File Upload (.html / .json / .txt) ---------- #
    @bot.on(events.NewMessage(func=lambda e: e.is_private and e.file and not (e.raw_text or "").startswith("/")))
    async def on_file(event):
        if not is_owner(event):
            return await deny(event)
        fname = (event.file.name or "").lower()
        if not fname.endswith((".html", ".htm", ".json", ".txt", ".md", ".csv")):
            return
        status = await event.reply(f"📂 Extracting links from <b>{html.escape(event.file.name)}</b>…")
        progress = Progress(status)
        data = await event.download_media(bytes)
        text = data.decode("utf-8", errors="ignore")
        # Unescape HTML entities so href="https://t.me/..." is parsed cleanly
        text = html.unescape(text)

        links: dict[str, Link] = {}
        already_sent = 0
        for raw in extract_raw_links_from_text(text):
            link = parse_link(raw)
            if not link or link.key in links:
                continue
            if link.key in STATE["keys"]:
                already_sent += 1
                continue
            links[link.key] = link

        await finish_extraction(
            bot, event.chat_id, html.escape(event.file.name or "Uploaded file"),
            1, list(links.values()), already_sent, progress,
        )

    # ---------------- Forwarded / Pasted Messages -------------------------- #
    async def process_forward_buffer(chat_id: int):
        await asyncio.sleep(2.0)
        msgs = forward_buf.pop(chat_id, [])
        forward_tasks.pop(chat_id, None)
        if not msgs:
            return

        # If the user sent/forwarded a single message that is either:
        #   a) a post link to a channel (e.g. https://t.me/c/123456/789) with no other links, or
        #   b) a single forwarded message from a channel where the bot is a member/admin,
        # try scanning the whole channel automatically; otherwise extract from the forwarded messages!
        if len(msgs) == 1 and not scan_lock.locked():
            m = msgs[0]
            fwd = m.fwd_from
            if fwd and isinstance(fwd.from_id, types.PeerChannel) and fwd.channel_post:
                ch_id = int(f"-100{fwd.from_id.channel_id}")
                try:
                    await bot.get_entity(ch_id)
                    can_access_channel = True
                except Exception:  # noqa: BLE001
                    can_access_channel = False
                if can_access_channel:
                    async with scan_lock:
                        cancel_event.clear()
                        status = await bot.send_message(
                            chat_id,
                            f"🔎 Detected channel from forwarded post (#{fwd.channel_post}) – scanning full channel history…",
                        )
                        await scan_channel_by_ids(
                            bot, chat_id, ch_id, fwd.channel_post, Progress(status), cancel_event,
                        )
                    return

        status = await bot.send_message(chat_id, f"📥 Extracting links from {len(msgs)} message(s)…")
        progress = Progress(status)
        links: dict[str, Link] = {}
        already_sent = 0
        for m in msgs:
            for raw in extract_raw_links(m):
                link = parse_link(raw)
                if not link or link.key in links:
                    continue
                if link.key in STATE["keys"]:
                    already_sent += 1
                    continue
                links[link.key] = link

        await finish_extraction(
            bot, chat_id, f"{len(msgs)} message(s)", len(msgs),
            list(links.values()), already_sent, progress,
        )

    @bot.on(events.NewMessage(
        func=lambda e: e.is_private and not e.file and not (e.raw_text or "").startswith("/")
    ))
    async def on_forwarded_or_text(event):
        if not is_owner(event):
            return await deny(event)
        chat_id = event.chat_id
        forward_buf.setdefault(chat_id, []).append(event.message)
        old_task = forward_tasks.get(chat_id)
        if old_task and not old_task.done():
            old_task.cancel()
        forward_tasks[chat_id] = asyncio.create_task(process_forward_buffer(chat_id))

    log.info("Ready. Open @%s in Telegram.", bot_me.username)
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
