"""
Channel Link Sorter bot
=======================

* Logs in to YOUR Telegram account with Telethon via phone + OTP sent
  through the bot chat (only once - the session is saved to `user.session`).
* Finds the first pinned channel called "Mega premium" (configurable).
* Extracts every Telegram channel / group link from its messages
  (plain text, hidden hyperlinks and inline buttons).
* Checks each link and replies (through the bot) with ONLY the accessible ones.

Login: send /login to the bot -> phone number -> OTP (-> 2FA password).
The session is saved to user.session, so this is needed only once.

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
from telethon import Button, TelegramClient, errors, events, functions, types
from telethon.tl.types.auth import SentCodeTypeApp

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
load_dotenv()

# No personal API keys needed: Telethon uses the public app credentials of the
# open-source Telegram Desktop client unless you override them in .env.
API_ID = int(os.getenv("API_ID") or 2040)
API_HASH = os.getenv("API_HASH") or "b18441a1ff607e10a989891a5462e627"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Mega premium").strip()
OWNER_ID = int(os.getenv("OWNER_ID") or 0)          # 0 = whoever logs in first
OWNER_FILE = "owner.json"
USER_SESSION = "user"   # -> user.session (created after the OTP login)
CHECK_DELAY = float(os.getenv("CHECK_DELAY", "2"))   # seconds between checks
MAX_FLOOD_WAIT = int(os.getenv("MAX_FLOOD_WAIT", "300"))
CACHE_HOURS = float(os.getenv("CACHE_HOURS", "24"))
INCLUDE_MENTIONS = os.getenv("INCLUDE_MENTIONS", "false").lower() == "true"

CACHE_FILE = "link_cache.json"
SENT_FILE = "sent_links.json"   # links already delivered to the admin

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
        return False, "username not found", None
    except errors.ChannelPrivateError:
        return False, "private / banned", None

    peer = res.peer
    if isinstance(peer, types.PeerUser):
        return None, "user or bot (not a channel)", None

    chat = next((c for c in res.chats if c.id == getattr(peer, "channel_id",
                 getattr(peer, "chat_id", None))), None)
    if chat is None or isinstance(chat, (types.ChannelForbidden, types.ChatForbidden)):
        return False, "forbidden", None
    if _blocked_everywhere(chat):
        return False, "blocked by Telegram", None
    kind = "channel" if getattr(chat, "broadcast", False) else "group"
    return True, f"{kind}: {chat.title}", str(chat.id)


async def check_invite(client, invite_hash, notify):
    try:
        res = await call_with_flood(
            client, functions.messages.CheckChatInviteRequest(invite_hash), notify
        )
    except (errors.InviteHashExpiredError, errors.InviteHashInvalidError,
            errors.InviteHashEmptyError):
        return False, "invite expired / invalid", None

    if isinstance(res, (types.ChatInviteAlready, types.ChatInvitePeek)):
        chat = res.chat
        if isinstance(chat, (types.ChannelForbidden, types.ChatForbidden)):
            return False, "forbidden", None
        return True, f"already joined: {chat.title}", str(chat.id)
    if isinstance(res, types.ChatInvite):
        extra = " (join request needed)" if getattr(res, "request_needed", False) else ""
        kind = "channel" if res.broadcast else "group"
        return True, f"{kind}: {res.title}{extra}", None
    return False, "unknown invite type", None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_cache() -> dict:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


LINKS_PER_MESSAGE = 20   # links per bot message (gap after every 5)

# In-memory history of delivered links (persisted to SENT_FILE):
#   keys / chats  -> what was already sent (never send again)
#   counter       -> last serial number given to a link (1, 2, 3 ...)
#   done_till     -> admin marked "work completed till #N"
#   items         -> [{"n": 1, "url": "...", "info": "..."}, ...]
STATE: dict = {}


def load_sent() -> dict:
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    STATE.clear()
    STATE.update(
        keys=set(data.get("keys", [])),
        chats=set(data.get("chats", [])),
        counter=int(data.get("counter", 0)),
        done_till=int(data.get("done_till", 0)),
        items=list(data.get("items", [])),
    )
    return STATE


def save_sent() -> None:
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "counter": STATE["counter"],
            "done_till": STATE["done_till"],
            "items": STATE["items"],
            "keys": sorted(STATE["keys"]),
            "chats": sorted(STATE["chats"]),
        }, f, ensure_ascii=False, indent=1)


def format_items(items: list[dict]) -> str:
    """1. link / 2. link ... with an empty line after every 5 links.
    Links up to the admin's "done" mark get a ✅."""
    done = STATE["done_till"]
    lines = []
    for i, it in enumerate(items):
        n = it["n"]
        if i and n % 5 == 1:
            lines.append("")
        mark = "✅ " if n <= done else ""
        lines.append(f"{mark}<b>{n}.</b> {html.escape(it['url'])}")
    return "\n".join(lines)


def item_buttons(items: list[dict]):
    """One button per block of 5: tap to mark work completed till that link."""
    first, last = items[0]["n"], items[-1]["n"]
    done = STATE["done_till"]
    btns = []
    for it in items:
        n = it["n"]
        if n % 5 == 0 or n == last:
            label = f"✅ Till {n}" if n <= done else f"☑️ Till {n}"
            btns.append(Button.inline(label, f"done:{n}:{first}:{last}".encode()))
    return [btns[i:i + 4] for i in range(0, len(btns), 4)]


async def send_items(bot, chat_id, items: list[dict]) -> None:
    for i in range(0, len(items), LINKS_PER_MESSAGE):
        part = items[i:i + LINKS_PER_MESSAGE]
        await bot.send_message(chat_id, format_items(part),
                               buttons=item_buttons(part), link_preview=False)
        await asyncio.sleep(1)


async def deliver(bot, chat_id, found: list) -> list[dict]:
    """Number + send new accessible links, skipping anything already sent."""
    items = []
    for link, info, cid in found:
        if link.key in STATE["keys"] or (cid and cid in STATE["chats"]):
            continue
        STATE["counter"] += 1
        it = {"n": STATE["counter"], "url": link.url, "info": info}
        STATE["items"].append(it)
        STATE["keys"].add(link.key)
        if cid:
            STATE["chats"].add(cid)
        items.append(it)
    if items:
        save_sent()
        await send_items(bot, chat_id, items)
    return items


def fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


async def wait_out(seconds: float, cancel_event, progress, text: str) -> bool:
    """Sleep through a rate limit (showing a countdown). False if cancelled."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        if cancel_event.is_set():
            return False
        await progress(f"{text}\n\n⏳ Auto-resume in {fmt_dur(left)}  (/cancel to stop)", True)
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=min(30, left))
        except asyncio.TimeoutError:
            pass
    return not cancel_event.is_set()


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
    "<b>Account</b>\n"
    "/login – log in to your Telegram account with OTP (one time)\n"
    "/logout – log out and delete the saved session\n\n"
    "<b>Scan</b>\n"
    "/scan – scan whole channel\n"
    "/scan 500 – scan latest 500 messages\n"
    "/scan fresh – ignore cache, re-check everything\n"
    "/cancel – stop a running scan / login\n"
    "/resetsent – forget already-sent links\n\n"
    "<b>Progress</b>\n"
    "Tap <b>☑️ Till N</b> under a list, or /done 25 – mark completed till link #25\n"
    "/progress – where you stopped\n"
    "/pending – resend links not yet completed\n\n"
    "ℹ️ Duplicate links and links already sent to you are never sent again."
)


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


def new_user_client() -> TelegramClient:
    return TelegramClient(USER_SESSION, API_ID, API_HASH)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Put BOT_TOKEN in the .env file.")

    clients = {"user": new_user_client()}
    await clients["user"].connect()

    owner = {"id": load_owner()}
    if await clients["user"].is_user_authorized():
        me = await clients["user"].get_me()
        log.info("Using saved session: %s (id=%s)", me.first_name, me.id)
        if not owner["id"]:
            owner["id"] = me.id
            save_owner(me.id)
    else:
        log.info("No saved session yet - send /login to the bot to log in with OTP")

    bot = TelegramClient("bot", API_ID, API_HASH)
    bot.parse_mode = "html"
    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info("Bot running as @%s", bot_me.username)

    scan_lock = asyncio.Lock()
    cancel_event = asyncio.Event()
    load_sent()
    login_state: dict[int, dict] = {}   # chat_id -> {"step", "phone", "hash"}

    def is_owner(event) -> bool:
        # Before the first login there is no owner yet (set OWNER_ID to lock it).
        return owner["id"] == 0 or event.sender_id == owner["id"]

    async def deny(event):
        await event.reply("⛔ This bot is private.")

    # ---------------- /start ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/(start|help)"))
    async def on_start(event):
        if not is_owner(event):
            return await deny(event)
        if await clients["user"].is_user_authorized():
            me = await clients["user"].get_me()
            state = f"🟢 Logged in as <b>{html.escape(me.first_name or '')}</b>"
        else:
            state = "🔴 Not logged in – send /login"
        await event.reply(f"{HELP}\n\n{state}")

    # ---------------- /login ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/login"))
    async def on_login(event):
        if not event.is_private:
            return await event.reply("Use /login in a private chat with me.")
        if not is_owner(event):
            return await deny(event)
        user = clients["user"]
        if not user.is_connected():
            await user.connect()
        if await user.is_user_authorized():
            me = await user.get_me()
            return await event.reply(
                f"✅ Already logged in as <b>{html.escape(me.first_name or '')}</b>. "
                "Use /logout first to switch account."
            )
        login_state[event.chat_id] = {"step": "phone"}
        await event.reply(
            "📱 Send your phone number with country code\n"
            "e.g. <code>+919876543210</code>\n\n/cancel to abort"
        )

    async def finish_login(event):
        login_state.pop(event.chat_id, None)
        me = await clients["user"].get_me()
        if not OWNER_ID:
            owner["id"] = event.sender_id
            save_owner(event.sender_id)
        log.info("Logged in as %s (id=%s)", me.first_name, me.id)
        await event.respond(
            f"✅ Logged in as <b>{html.escape(me.first_name or '')}</b>.\n"
            "Session saved – you won't need OTP again.\n\nSend /scan to start."
        )

    @bot.on(events.NewMessage(
        func=lambda e: e.is_private and e.chat_id in login_state
        and not (e.raw_text or "").startswith("/")))
    async def on_login_step(event):
        if not is_owner(event):
            return
        st = login_state[event.chat_id]
        text = (event.raw_text or "").strip()
        user = clients["user"]
        try:
            if st["step"] == "phone":
                phone = re.sub(r"[^\d+]", "", text)
                if not re.fullmatch(r"\+?\d{7,15}", phone):
                    return await event.reply("❌ That doesn't look like a phone number. Try again.")
                phone = phone if phone.startswith("+") else "+" + phone
                sent = await user.send_code_request(phone)
                st.update(step="code", phone=phone, hash=sent.phone_code_hash)
                where = ("your Telegram app" if isinstance(sent.type, SentCodeTypeApp)
                         else "SMS")
                await event.reply(
                    f"📩 OTP sent to {where}.\n\n"
                    "⚠️ Send it <b>with spaces</b>, e.g. <code>1 2 3 4 5</code>\n"
                    "(Telegram cancels the login if the plain code is shared in a chat.)"
                )

            elif st["step"] == "code":
                code = re.sub(r"\D", "", text)
                if len(code) < 5:
                    return await event.reply("❌ Send the full code, e.g. <code>1 2 3 4 5</code>")
                try:
                    await event.delete()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await user.sign_in(st["phone"], code, phone_code_hash=st["hash"])
                except errors.SessionPasswordNeededError:
                    st["step"] = "password"
                    return await event.respond(
                        "🔐 Two-step verification is on. Send your cloud password "
                        "(the message will be deleted right away)."
                    )
                await finish_login(event)

            elif st["step"] == "password":
                try:
                    await event.delete()
                except Exception:  # noqa: BLE001
                    pass
                await user.sign_in(password=text)
                await finish_login(event)

        except errors.PhoneNumberInvalidError:
            await event.reply("❌ Invalid phone number. Send it again with country code.")
        except errors.PhoneNumberBannedError:
            login_state.pop(event.chat_id, None)
            await event.reply("❌ This phone number is banned by Telegram.")
        except errors.PhoneCodeInvalidError:
            await event.respond("❌ Wrong code. Send it again (with spaces).")
        except errors.PhoneCodeExpiredError:
            login_state.pop(event.chat_id, None)
            await event.respond("⌛ Code expired (or was shared without spaces). Send /login again.")
        except errors.PasswordHashInvalidError:
            await event.respond("❌ Wrong password. Try again.")
        except errors.FloodWaitError as e:
            login_state.pop(event.chat_id, None)
            await event.respond(f"⏳ Too many attempts. Try again in {e.seconds // 60 + 1} min.")
        except errors.RPCError as e:
            login_state.pop(event.chat_id, None)
            await event.respond(f"❌ Login failed: {e.__class__.__name__}. Send /login to retry.")

    # ---------------- /logout ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/logout"))
    async def on_logout(event):
        if not is_owner(event):
            return await deny(event)
        if scan_lock.locked():
            return await event.reply("⏳ A scan is running. /cancel it first.")
        user = clients["user"]
        if await user.is_user_authorized():
            await user.log_out()           # also deletes user.session
        clients["user"] = new_user_client()
        await clients["user"].connect()
        owner["id"] = OWNER_ID
        if os.path.exists(OWNER_FILE):
            os.remove(OWNER_FILE)
        await event.reply("👋 Logged out and session deleted. Send /login to log in again.")

    # ---------------- /cancel ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/cancel"))
    async def on_cancel(event):
        if not is_owner(event):
            return
        if login_state.pop(event.chat_id, None):
            await event.reply("Login cancelled.")
        elif scan_lock.locked():
            cancel_event.set()
            await event.reply("🛑 Cancelling…")
        else:
            await event.reply("Nothing is running.")

    # ---------------- /resetsent ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/resetsent"))
    async def on_resetsent(event):
        if not is_owner(event):
            return await deny(event)
        if scan_lock.locked():
            return await event.reply("⏳ A scan is running. /cancel it first.")
        n = STATE["counter"]
        if os.path.exists(SENT_FILE):
            os.remove(SENT_FILE)
        load_sent()
        await event.reply(f"🗑 Sent history cleared ({n} links). "
                          "Numbering restarts at 1 and the next /scan sends everything again.")

    # ---------------- progress marking ---------------- #
    def progress_text() -> str:
        done, total = STATE["done_till"], STATE["counter"]
        text = (f"📌 Completed till <b>#{done}</b> of {total} links\n"
                f"⏳ Pending: {max(0, total - done)}")
        nxt = next((it for it in STATE["items"] if it["n"] == done + 1), None)
        if nxt:
            text += f"\n➡️ Next: <b>{nxt['n']}.</b> {html.escape(nxt['url'])}"
        return text

    @bot.on(events.CallbackQuery(pattern=rb"^done:(\d+):(\d+):(\d+)$"))
    async def on_done_button(event):
        if not is_owner(event):
            return await event.answer("⛔ Not allowed", alert=True)
        n, first, last = (int(x) for x in event.pattern_match.groups())
        STATE["done_till"] = n
        save_sent()
        items = [it for it in STATE["items"] if first <= it["n"] <= last]
        if items:
            try:
                await event.edit(format_items(items), buttons=item_buttons(items),
                                 link_preview=False)
            except errors.MessageNotModifiedError:
                pass
        await event.answer(f"✅ Marked completed till #{n}")

    @bot.on(events.NewMessage(pattern=r"^/done(?:@\w+)?(?:\s+(\d+))?\s*$"))
    async def on_done(event):
        if not is_owner(event):
            return await deny(event)
        num = event.pattern_match.group(1)
        if not num:
            return await event.reply("Usage: <code>/done 25</code> – marks work completed till link #25")
        n = min(int(num), STATE["counter"])
        STATE["done_till"] = n
        save_sent()
        await event.reply(progress_text(), link_preview=False)

    @bot.on(events.NewMessage(pattern=r"^/progress"))
    async def on_progress(event):
        if not is_owner(event):
            return await deny(event)
        await event.reply(progress_text(), link_preview=False)

    @bot.on(events.NewMessage(pattern=r"^/pending"))
    async def on_pending(event):
        if not is_owner(event):
            return await deny(event)
        items = [it for it in STATE["items"] if it["n"] > STATE["done_till"]]
        if not items:
            return await event.reply("🎉 Nothing pending – all sent links are marked completed.")
        await event.reply(f"⏳ {len(items)} pending links (after #{STATE['done_till']}):")
        await send_items(bot, event.chat_id, items)

    # ---------------- /scan ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/scan(?:@\w+)?(.*)$"))
    async def on_scan(event):
        if not is_owner(event):
            return await deny(event)
        user = clients["user"]
        if not await user.is_user_authorized():
            return await event.reply("🔴 Not logged in. Send /login first.")
        if scan_lock.locked():
            return await event.reply("⏳ A scan is already running. Use /cancel to stop it.")

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

    log.info("Ready. Open @%s in Telegram and send /login (first time) or /scan",
             bot_me.username)
    await bot.run_until_disconnected()


async def run_scan(user, bot, event, progress, limit, fresh, cancel_event):
    chat_id = event.chat_id
    dialog = await find_target_channel(user)
    if dialog is None:
        await progress(f"❌ Could not find a pinned channel named “{CHANNEL_NAME}”.", True)
        return
    channel = dialog.entity
    name = html.escape(dialog.name or "channel")
    own_username = (getattr(channel, "username", None) or "").lower()
    await progress(f"📥 Reading messages from <b>{name}</b>…", True)

    # ---- 1. Collect links (resumes after rate limits) ---------------------- #
    links: dict[str, Link] = {}
    scanned = 0
    offset_id = 0
    while True:
        try:
            remaining = None if limit is None else max(0, limit - scanned)
            if remaining == 0:
                break
            async for msg in user.iter_messages(channel, limit=remaining, offset_id=offset_id):
                if cancel_event.is_set():
                    break
                offset_id = msg.id
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
            break
        except errors.FloodWaitError as e:
            if not await wait_out(e.seconds + 5, cancel_event, progress,
                                  f"⚠️ Rate limit while reading {name} ({scanned} messages so far)."):
                break

    if cancel_event.is_set():
        await progress("🛑 Scan cancelled.", True)
        return
    if not links:
        await progress(f"No Telegram links found in {scanned} messages.", True)
        return

    link_list = list(links.values())[::-1]      # oldest post first
    total = len(link_list)

    # ---- 2. Check links; send in numbered batches as they are found -------- #
    cache = load_cache()
    pending: list = []                 # accessible, not yet sent
    scan_items: list[dict] = []        # sent during this scan
    seen_chats: set[str] = set()
    dead = skipped = already_sent = duplicates = rate_limits = 0
    member_chats: dict[str, str] | None = None
    cancelled = False

    async def flush():
        nonlocal pending
        if pending:
            scan_items.extend(await deliver(bot, chat_id, pending))
            pending = []

    i = 0
    while i < total:
        if cancel_event.is_set():
            cancelled = True
            break
        link = link_list[i]

        if link.key in STATE["keys"]:          # sent in an earlier scan
            already_sent += 1
            i += 1
            continue

        cached = cache.get(link.key)
        if (cached and not fresh and link.kind != "private_id"
                and time.time() - cached["ts"] < CACHE_HOURS * 3600):
            ok, info, cid = cached["ok"], cached["info"], cached.get("cid")
        else:
            try:
                if link.kind == "username":
                    ok, info, cid = await check_username(user, link.value, progress)
                elif link.kind == "invite":
                    ok, info, cid = await check_invite(user, link.value, progress)
                else:  # private t.me/c/<id> link -> accessible only if joined
                    if member_chats is None:
                        member_chats = {
                            str(d.entity.id): d.name
                            async for d in user.iter_dialogs()
                            if d.is_channel or d.is_group
                        }
                    title = member_chats.get(link.value)
                    cid = link.value
                    ok, info = (True, f"joined: {title}") if title else (False, "private, not joined")
            except errors.FloodWaitError as e:
                # Long rate limit: send what we have, wait, then retry this link
                rate_limits += 1
                save_cache(cache)
                await flush()
                wait = e.seconds + 5
                resume_at = time.strftime("%I:%M %p", time.localtime(time.time() + wait))
                await bot.send_message(
                    chat_id,
                    f"⚠️ Telegram rate limit at link {i + 1}/{total}.\n"
                    f"Links found so far are sent above.\n"
                    f"⏳ Waiting {fmt_dur(wait)} – auto-resume at ~{resume_at}.",
                )
                if not await wait_out(wait, cancel_event, progress,
                                      f"⚠️ Rate limited at link {i + 1}/{total}\n"
                                      f"✅ {len(scan_items)} sent so far"):
                    cancelled = True
                    break
                await bot.send_message(chat_id, f"▶️ Rate limit over – resuming from link {i + 1}/{total}.")
                continue                                       # retry same link
            except errors.RPCError as e:
                ok, info, cid = False, f"error: {e.__class__.__name__}", None

            if link.kind != "private_id":
                cache[link.key] = {"ok": ok, "info": info, "cid": cid, "ts": time.time()}
                if i % 10 == 0:
                    save_cache(cache)
                await asyncio.sleep(CHECK_DELAY)

        if ok is True:
            if cid and cid in STATE["chats"]:
                already_sent += 1          # same channel was sent before via another link
            elif cid and cid in seen_chats:
                duplicates += 1            # same channel already found in this scan
            else:
                pending.append((link, info, cid))
                if cid:
                    seen_chats.add(cid)
                if len(pending) >= LINKS_PER_MESSAGE:
                    await flush()
        elif ok is None:
            skipped += 1
        else:
            dead += 1

        i += 1
        await progress(f"🔍 Checking links {i}/{total}\n"
                       f"✅ {len(scan_items) + len(pending)} new  ❌ {dead}  "
                       f"➖ {skipped}  🔁 {already_sent + duplicates}")

    save_cache(cache)
    await flush()

    # ---- 3. Report --------------------------------------------------------- #
    summary = (
        f"📊 <b>{name}</b> – {'stopped' if cancelled else 'whole channel checked'}\n"
        f"Messages scanned: {scanned}\n"
        f"Unique links: {total} (checked {i})\n"
        f"✅ New accessible sent: {len(scan_items)}\n"
        f"🔁 Already sent before: {already_sent}\n"
        f"♊ Duplicates (same channel): {duplicates}\n"
        f"❌ Not accessible: {dead}\n"
        f"➖ Users/bots skipped: {skipped}\n"
        f"⏳ Rate limits waited out: {rate_limits}"
    )
    if cancelled:
        summary += "\n\n🛑 Cancelled – run /scan again to continue (sent links won't repeat)."
    await progress(summary, True)

    if not scan_items:
        await bot.send_message(chat_id, "No new accessible links "
                               "(everything was already sent before).")
        return

    report = "\n".join(f"{it['n']}. {it['url']}  |  {it['info']}" for it in scan_items)
    f = io.BytesIO(report.encode("utf-8"))
    f.name = "accessible_links.txt"
    await bot.send_file(
        chat_id, f,
        caption=(f"✅ {len(scan_items)} new accessible links "
                 f"(#{scan_items[0]['n']}–#{scan_items[-1]['n']})\n"
                 "Tap ☑️ Till N under the lists or send /done N to mark your progress."),
    )


if __name__ == "__main__":
    asyncio.run(main())
