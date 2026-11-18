"""
Channel Link Sorter bot
=======================

* Logs in to YOUR Telegram account with Telethon (only once - the session is
  saved to `user.session`).
* Finds the first pinned channel called "Mega premium" (configurable).
* Extracts every Telegram channel / group link from its messages
  (plain text, hidden hyperlinks and inline buttons).
* Checks each link and replies (through the bot) with ONLY the accessible ones.

Bot commands (only the account owner can use them):
    /start              - help
    /scan               - scan the whole channel
    /scan 500           - scan only the latest 500 messages
    /scan fresh         - ignore cached results and re-check every link
    /cancel             - stop a running scan
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
from telethon import TelegramClient, errors, events, functions, types

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
load_dotenv()

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PHONE = os.getenv("PHONE") or None
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Mega premium").strip()
OWNER_ID = int(os.getenv("OWNER_ID") or 0)          # 0 = the logged-in account
CHECK_DELAY = float(os.getenv("CHECK_DELAY", "2"))   # seconds between checks
MAX_FLOOD_WAIT = int(os.getenv("MAX_FLOOD_WAIT", "300"))
CACHE_HOURS = float(os.getenv("CACHE_HOURS", "24"))
INCLUDE_MENTIONS = os.getenv("INCLUDE_MENTIONS", "false").lower() == "true"

CACHE_FILE = "link_cache.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("link_sorter")

# --------------------------------------------------------------------------- #
# Link extraction / parsing
# --------------------------------------------------------------------------- #
URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"'()\[\]{}]+"
    r"|tg://(?:resolve|join)\?[^\s<>\"'()\[\]{}]+",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

# t.me/<word> paths that are NOT usernames
RESERVED = {
    "joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "iv",
    "login", "addlist", "setlanguage", "addtheme", "bg", "confirmphone",
    "invoice", "contact", "giftcode", "nft", "m", "c", "s", "boost", "a", "k", "z",
}


@dataclass(frozen=True)
class Link:
    kind: str    # "username" | "invite" | "private_id"
    value: str
    url: str

    @property
    def key(self) -> str:
        v = self.value.lower() if self.kind == "username" else self.value
        return f"{self.kind}:{v}"


def parse_link(raw: str):
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
        q = parse_qs(urlparse(raw).query)
        if "invite" in q:
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

    # Private invite links: t.me/+HASH  or  t.me/joinchat/HASH
    if first.startswith("+") or first.startswith("%2B"):
        h = first[1:] if first.startswith("+") else first[3:]
        if h and not h.isdigit():            # all digits = phone number link
            return Link("invite", h, f"https://t.me/+{h}")
        return None
    if first.lower() == "joinchat" and len(parts) > 1:
        return Link("invite", parts[1], f"https://t.me/+{parts[1]}")

    # Private message links: t.me/c/<channel_id>/<msg_id>
    if first.lower() == "c" and len(parts) > 1 and parts[1].isdigit():
        return Link("private_id", parts[1], f"https://t.me/c/{parts[1]}")

    # Preview / boost links: t.me/s/<username>, t.me/boost/<username>
    if first.lower() in {"s", "boost"} and len(parts) > 1:
        first = parts[1]
    elif first.lower() in RESERVED:
        return None

    if USERNAME_RE.match(first):
        return Link("username", first, f"https://t.me/{first}")
    return None


def extract_raw_links(msg) -> list[str]:
    """All link-looking strings from text, entities and inline buttons."""
    found: list[str] = []
    text = msg.message or ""
    found += URL_RE.findall(text)

    try:
        for ent, ent_text in msg.get_entities_text():
            if isinstance(ent, types.MessageEntityTextUrl):
                found.append(ent.url)
            elif isinstance(ent, types.MessageEntityUrl):
                found.append(ent_text)
            elif INCLUDE_MENTIONS and isinstance(ent, types.MessageEntityMention):
                found.append(ent_text)
    except Exception:  # noqa: BLE001 - entity parsing should never kill a scan
        pass

    markup = msg.reply_markup
    if isinstance(markup, types.ReplyInlineMarkup):
        for row in markup.rows:
            for btn in row.buttons:
                url = getattr(btn, "url", None)
                if url:
                    found.append(url)

    if isinstance(msg.media, types.MessageMediaWebPage):
        url = getattr(msg.media.webpage, "url", None)
        if url:
            found.append(url)

    # keep only Telegram links (button / hyperlink URLs can be anything)
    return [
        u for u in found
        if u.startswith("@") or URL_RE.match(u.strip())
    ]


# --------------------------------------------------------------------------- #
# Accessibility checks
# --------------------------------------------------------------------------- #
def _blocked_everywhere(chat) -> bool:
    for r in getattr(chat, "restriction_reason", None) or []:
        if getattr(r, "platform", "") == "all":
            return True
    return False


async def call_with_flood(client, request, notify=None):
    """Run a request; sleep through short flood waits, re-raise long ones."""
    while True:
        try:
            return await client(request)
        except errors.FloodWaitError as e:
            if e.seconds > MAX_FLOOD_WAIT:
                raise
            log.warning("Flood wait %ss", e.seconds)
            if notify:
                await notify(f"⏳ Telegram rate limit - waiting {e.seconds}s…", True)
            await asyncio.sleep(e.seconds + 1)


async def check_username(client, username, notify):
    try:
        res = await call_with_flood(
            client, functions.contacts.ResolveUsernameRequest(username), notify
        )
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
        return False, "username not found"
    except errors.ChannelPrivateError:
        return False, "private / banned"

    peer = res.peer
    if isinstance(peer, types.PeerUser):
        return None, "user or bot (not a channel)"

    chat = next((c for c in res.chats if c.id == getattr(peer, "channel_id",
                 getattr(peer, "chat_id", None))), None)
    if chat is None or isinstance(chat, (types.ChannelForbidden, types.ChatForbidden)):
        return False, "forbidden"
    if _blocked_everywhere(chat):
        return False, "blocked by Telegram"
    kind = "channel" if getattr(chat, "broadcast", False) else "group"
    return True, f"{kind}: {chat.title}"


async def check_invite(client, invite_hash, notify):
    try:
        res = await call_with_flood(
            client, functions.messages.CheckChatInviteRequest(invite_hash), notify
        )
    except (errors.InviteHashExpiredError, errors.InviteHashInvalidError,
            errors.InviteHashEmptyError):
        return False, "invite expired / invalid"

    if isinstance(res, (types.ChatInviteAlready, types.ChatInvitePeek)):
        chat = res.chat
        if isinstance(chat, (types.ChannelForbidden, types.ChatForbidden)):
            return False, "forbidden"
        return True, f"already joined: {chat.title}"
    if isinstance(res, types.ChatInvite):
        extra = " (join request needed)" if getattr(res, "request_needed", False) else ""
        kind = "channel" if res.broadcast else "group"
        return True, f"{kind}: {res.title}{extra}"
    return False, "unknown invite type"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_cache() -> dict:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


async def find_target_channel(client):
    """First pinned dialog named CHANNEL_NAME (fallbacks: any dialog with that
    name, then the first pinned channel)."""
    target = CHANNEL_NAME.casefold()
    first_pinned_channel = None
    async for d in client.iter_dialogs():
        if not d.pinned:
            break
        if d.is_channel and first_pinned_channel is None:
            first_pinned_channel = d
        if (d.name or "").strip().casefold() == target:
            return d
    async for d in client.iter_dialogs():
        if (d.name or "").strip().casefold() == target:
            return d
    return first_pinned_channel


class Progress:
    def __init__(self, message):
        self.message = message
        self.last = 0.0

    async def __call__(self, text: str, force: bool = False):
        now = time.monotonic()
        if not force and now - self.last < 4:
            return
        self.last = now
        try:
            await self.message.edit(text)
        except (errors.MessageNotModifiedError, errors.FloodWaitError):
            pass


def chunk_lines(lines: list[str], limit: int = 3900) -> list[str]:
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
HELP = (
    "👋 <b>Channel Link Sorter</b>\n\n"
    f"I read the pinned channel <b>{CHANNEL_NAME}</b> from your account, "
    "collect every Telegram channel/group link and send back only the "
    "<b>accessible</b> ones.\n\n"
    "/scan – scan whole channel\n"
    "/scan 500 – scan latest 500 messages\n"
    "/scan fresh – ignore cache, re-check everything\n"
    "/cancel – stop a running scan"
)


async def main():
    global OWNER_ID
    if not (API_ID and API_HASH and BOT_TOKEN):
        raise SystemExit("❌ Fill API_ID, API_HASH and BOT_TOKEN in the .env file.")

    user = TelegramClient("user", API_ID, API_HASH)
    bot = TelegramClient("bot", API_ID, API_HASH)
    bot.parse_mode = "html"

    # Interactive only on the very first run (phone, code, 2FA password).
    await user.start(phone=PHONE)
    me = await user.get_me()
    if not OWNER_ID:
        OWNER_ID = me.id
    log.info("User account logged in as %s (id=%s)", me.first_name, me.id)

    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info("Bot running as @%s - owner id %s", bot_me.username, OWNER_ID)

    scan_lock = asyncio.Lock()
    cancel_event = asyncio.Event()

    def is_owner(event) -> bool:
        return event.sender_id == OWNER_ID

    @bot.on(events.NewMessage(pattern=r"^/(start|help)"))
    async def on_start(event):
        if not is_owner(event):
            await event.reply("⛔ This bot is private.")
            return
        await event.reply(HELP)

    @bot.on(events.NewMessage(pattern=r"^/cancel"))
    async def on_cancel(event):
        if not is_owner(event):
            return
        if scan_lock.locked():
            cancel_event.set()
            await event.reply("🛑 Cancelling…")
        else:
            await event.reply("Nothing is running.")

    @bot.on(events.NewMessage(pattern=r"^/scan(?:@\w+)?(.*)$"))
    async def on_scan(event):
        if not is_owner(event):
            await event.reply("⛔ This bot is private.")
            return
        if scan_lock.locked():
            await event.reply("⏳ A scan is already running. Use /cancel to stop it.")
            return

        args = event.pattern_match.group(1).split()
        limit = next((int(a) for a in args if a.isdigit()), None)
        fresh = "fresh" in [a.lower() for a in args]

        async with scan_lock:
            cancel_event.clear()
            status = await event.reply("🔎 Looking for the channel…")
            progress = Progress(status)
            try:
                await run_scan(user, bot, event, progress, limit, fresh, cancel_event)
            except Exception as e:  # noqa: BLE001
                log.exception("Scan failed")
                await progress(f"❌ Scan failed: {type(e).__name__}: {e}", True)

    log.info("Ready. Open your bot in Telegram and send /scan")
    await bot.run_until_disconnected()


async def run_scan(user, bot, event, progress, limit, fresh, cancel_event):
    dialog = await find_target_channel(user)
    if dialog is None:
        await progress(f"❌ Could not find a pinned channel named “{CHANNEL_NAME}”.", True)
        return
    channel = dialog.entity
    name = html.escape(dialog.name or "channel")
    own_username = (getattr(channel, "username", None) or "").lower()
    await progress(f"📥 Reading messages from <b>{name}</b>…", True)

    # ---- 1. Collect links -------------------------------------------------- #
    links: dict[str, Link] = {}
    scanned = 0
    async for msg in user.iter_messages(channel, limit=limit):
        if cancel_event.is_set():
            break
        scanned += 1
        for raw in extract_raw_links(msg):
            link = parse_link(raw)
            if not link or link.key in links:
                continue
            if link.kind == "username" and link.value.lower() == own_username:
                continue
            links[link.key] = link
        if scanned % 200 == 0:
            await progress(f"📥 {name}: {scanned} messages read, "
                           f"{len(links)} unique links found…")

    if cancel_event.is_set():
        await progress("🛑 Scan cancelled.", True)
        return
    if not links:
        await progress(f"No Telegram links found in {scanned} messages.", True)
        return

    # ---- 2. Check links ---------------------------------------------------- #
    cache = load_cache()
    accessible: list[tuple[Link, str]] = []
    dead = skipped = 0
    stop_reason = ""
    member_chats: dict[str, str] | None = None
    total = len(links)

    for i, link in enumerate(links.values(), 1):
        if cancel_event.is_set():
            stop_reason = "🛑 Cancelled by user - partial results below."
            break

        cached = cache.get(link.key)
        if (cached and not fresh and link.kind != "private_id"
                and time.time() - cached["ts"] < CACHE_HOURS * 3600):
            ok, info = cached["ok"], cached["info"]
        else:
            try:
                if link.kind == "username":
                    ok, info = await check_username(user, link.value, progress)
                elif link.kind == "invite":
                    ok, info = await check_invite(user, link.value, progress)
                else:  # private t.me/c/<id> link -> accessible only if joined
                    if member_chats is None:
                        member_chats = {
                            str(d.entity.id): d.name
                            async for d in user.iter_dialogs()
                            if d.is_channel or d.is_group
                        }
                    title = member_chats.get(link.value)
                    ok, info = (True, f"joined: {title}") if title else (False, "private, not joined")
            except errors.FloodWaitError as e:
                stop_reason = (f"⚠️ Telegram rate limit: must wait {e.seconds // 60} min. "
                               "Run /scan again later - checked links are cached.")
                break
            except errors.RPCError as e:
                ok, info = False, f"error: {e.__class__.__name__}"

            if link.kind != "private_id":
                cache[link.key] = {"ok": ok, "info": info, "ts": time.time()}
                if i % 10 == 0:
                    save_cache(cache)
                await asyncio.sleep(CHECK_DELAY)

        if ok is True:
            accessible.append((link, info))
        elif ok is None:
            skipped += 1
        else:
            dead += 1

        await progress(f"🔍 Checking links {i}/{total}\n"
                       f"✅ {len(accessible)}  ❌ {dead}  ➖ {skipped}")

    save_cache(cache)

    # ---- 3. Report --------------------------------------------------------- #
    summary = (
        f"📊 <b>{name}</b>\n"
        f"Messages scanned: {scanned}\n"
        f"Unique links: {total}\n"
        f"✅ Accessible: {len(accessible)}\n"
        f"❌ Not accessible: {dead}\n"
        f"➖ Users/bots skipped: {skipped}"
    )
    if stop_reason:
        summary += f"\n\n{stop_reason}"
    await progress(summary, True)

    if not accessible:
        await bot.send_message(event.chat_id, "No accessible links found.")
        return

    for chunk in chunk_lines([l.url for l, _ in accessible]):
        await bot.send_message(event.chat_id, chunk, link_preview=False)

    report = "\n".join(f"{l.url}  |  {info}" for l, info in accessible)
    f = io.BytesIO(report.encode("utf-8"))
    f.name = "accessible_links.txt"
    await bot.send_file(event.chat_id, f, caption=f"✅ {len(accessible)} accessible links")


if __name__ == "__main__":
    asyncio.run(main())
