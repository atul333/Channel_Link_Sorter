"""
Channel Link Sorter Bot
=======================

* Logs in to your Telegram account with Telethon via phone + OTP sent through
  the bot chat (saved to `user.session` / `session_string.txt` so it is needed
  only once).
* Finds the first pinned channel called "Mega premium" (configurable).
* Extracts all Telegram links from the channel:
  1. Validates Channel / Group links using the `/checklinks` validation logic
     from "Channel link provider" (HTTP web-preview + `?embed=1` ToS ban check)
     and sends ONLY the working links.
  2. Separately extracts and sends ALL `https://t.me/addlist/` folder links.
* Deduplicates links and never repeats links already sent to the admin.
* Numbers every link (1., 2., 3.…) separated into 5-by-5 blocks, with a
  separate interactive checklist button for each 5-link block (1–5, 6–10, …).
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
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv
from telethon import Button, TelegramClient, errors, events, types
from telethon.sessions import StringSession
from telethon.tl.types.auth import SentCodeTypeApp

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv()

DATA_DIR = os.path.abspath(os.getenv("DATA_DIR") or BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

API_ID = int(os.getenv("API_ID") or 2040)
API_HASH = os.getenv("API_HASH") or "b18441a1ff607e10a989891a5462e627"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Mega premium").strip()
OWNER_ID = int(os.getenv("OWNER_ID") or 0)          # 0 = whoever logs in first
CHECK_DELAY = float(os.getenv("CHECK_DELAY", "0.35"))
CACHE_HOURS = float(os.getenv("CACHE_HOURS", "24"))
INCLUDE_MENTIONS = os.getenv("INCLUDE_MENTIONS", "false").lower() == "true"

USER_SESSION = os.path.join(DATA_DIR, "user")        # -> user.session
BOT_SESSION = os.path.join(DATA_DIR, "bot")          # -> bot.session
SESSION_FILE = os.path.join(DATA_DIR, "session_string.txt")
OWNER_FILE = os.path.join(DATA_DIR, "owner.json")
SENT_FILE = os.path.join(DATA_DIR, "sent_links.json")
CACHE_FILE = os.path.join(DATA_DIR, "link_cache.json")

LINKS_PER_MESSAGE = 20   # 4 blocks of 5 links per message

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("link_sorter")

# Shared persistent AsyncClient for link health checks (from Channel link provider)
_ASYNC_HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(12.0, connect=5.0),
    follow_redirects=True,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    },
)

# --------------------------------------------------------------------------- #
# Link Validation Logic (exact /checklinks logic from "Channel link provider")
# --------------------------------------------------------------------------- #
_GENERIC_TG_PAGE_TITLES = {
    "not available",
    "unavailable",
    "join group chat on telegram",
    "telegram",
    "channel not found",
    "group not found",
}

_ADULT_RESTRICTION_REASONS = {"porno", "porn", "sensitive", "adult", "nsfw", "18+", "age"}


def _normalize_tg_html_text(raw_html: str) -> str:
    """Unescape HTML entities, normalize curly apostrophes and collapse whitespace to lowercase."""
    if not raw_html:
        return ""
    unescaped = html.unescape(raw_html)
    norm = re.sub(r"[\u2018\u2019`´]", "'", unescaped)
    return re.sub(r"\s+", " ", norm).lower()


def _is_telegram_adult_sensitive_notice(raw_html: str) -> bool:
    """
    Detect Telegram's 18+ / sensitive / adult content warning or filter notice
    (e.g. '18+ This chat may contain sensitive content suitable only for adults.
    Do you still want to view it? Always show 18+ media / View Anyway', or
    web-preview 'used to spread pornographic content').
    Channels showing these notices ARE ACTIVE/LIVE — never expired or ToS-banned.
    """
    norm = _normalize_tg_html_text(raw_html)
    if not norm:
        return False

    direct_phrases = (
        "may contain sensitive content",
        "sensitive content suitable only for adults",
        "suitable only for adults",
        "always show 18+ media",
        "show 18+ media",
        "contains sensitive content",
        "marked as sensitive",
        "spread pornographic content",
        "pornographic content",
        "contains pornographic",
        "contains adult content",
        "adult-only content",
        "adults-only content",
        "nsfw content",
    )
    if any(p in norm for p in direct_phrases):
        return True

    # Combined signals from Telegram's 18+ / sensitive modal or preview gates
    if "18+" in norm and any(k in norm for k in ("sensitive", "adults", "view anyway", "media", "age-restricted", "age restricted")):
        return True
    if "sensitive content" in norm and any(k in norm for k in ("this chat", "this channel", "this group", "adults", "view it", "view anyway", "can't be displayed", "cannot be displayed")):
        return True
    if ("adult content" in norm or "age-restricted" in norm or "age restricted" in norm) and any(
        k in norm for k in ("this chat", "this channel", "this group", "can't be displayed", "cannot be displayed", "18+", "view anyway")
    ):
        return True

    return False


def _detect_telegram_ban_reason(raw_html: str, *, allow_web_porn_notice: bool = True) -> str:
    """
    Detect Telegram Terms of Service bans, copyright/law blocks, and unavailable notices
    after unescaping HTML entities (e.g. &#39;, &rsquo;) and normalizing apostrophes.
    Explicitly treats 18+ / sensitive / pornographic content warnings as ACTIVE (returns '').
    Returns a human-readable dead reason string, or '' if no ban notice is detected.
    """
    norm = _normalize_tg_html_text(raw_html)
    if not norm:
        return ""

    # 1. Hard Terms of Service / permanent bans (always dead on any surface)
    if (
        "violated telegram's terms of service" in norm
        or "violated telegrams terms of service" in norm
        or "violated telegram" in norm
        or (
            "terms of service" in norm
            and any(k in norm for k in ("can't be displayed", "cannot be displayed", "not available", "violated", "blocked"))
        )
    ):
        return "Not available: Violated Telegram's Terms of Service"

    if "copyright infringement" in norm or "violated copyright" in norm:
        return "Not available: Blocked for copyright infringement"

    if "violated local laws" in norm or "unavailable in your country" in norm or "restricted in your region" in norm:
        return "Not available: Blocked due to local laws"

    if "because it was blocked" in norm or "channel has been blocked" in norm or "group has been blocked" in norm:
        return "Channel is blocked/banned by Telegram"

    if "channel with username" in norm and "not found" in norm:
        return "Channel not found"

    # 2. Live 18+ / sensitive / adult content gates are NEVER considered expired or banned
    if allow_web_porn_notice and _is_telegram_adult_sensitive_notice(raw_html):
        return ""

    if any(
        phrase in norm
        for phrase in (
            "channel is inaccessible",
            "group is inaccessible",
            "chat is inaccessible",
            "channel is unavailable",
            "chat is unavailable",
            "this channel is not available",
            "this chat is not available",
            "this group is not available",
        )
    ):
        return "Channel is not available / inaccessible"

    # 3. Generic "can't be displayed" notices (only reached if NOT an 18+/sensitive notice above)
    if any(
        phrase in norm
        for phrase in (
            "this channel can't be displayed",
            "this group can't be displayed",
            "this chat can't be displayed",
            "cannot be displayed",
        )
    ):
        return "Not available: Channel can't be displayed (Banned/Restricted)"

    return ""


async def check_channel_link_alive(link: str) -> tuple[bool, str]:
    """
    Check if a Telegram channel link is active and valid (exact /checklinks logic
    from Channel link provider).
    Detects expired/revoked links, deleted channels, and Terms of Service bans
    ('Not available — This channel can't be displayed because it violated Telegram's Terms of Service.').
    Explicitly treats 18+ / sensitive content warning gates ('This chat may contain sensitive content
    suitable only for adults', 'Always show 18+ media', web NSFW/pornographic preview notices,
    and empty embed error divs on live 18+ channel stubs) as ACTIVE.
    Returns (is_alive: bool, detail_or_reason: str).
    """
    # 1. Normalize the link (preserving exact case of invite hashes)
    clean_link = (link or "").strip()
    if not clean_link:
        return False, "Empty channel link"

    if clean_link.startswith("@"):
        clean_link = f"https://t.me/{clean_link.lstrip('@')}"
    elif not clean_link.startswith(("http://", "https://")):
        if clean_link.lower().startswith(("t.me/", "telegram.me/")):
            clean_link = f"https://t.me/{clean_link.split('/', 1)[1]}"
        else:
            clean_link = f"https://t.me/{clean_link.lstrip('@')}"
    else:
        clean_link = re.sub(
            r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/",
            "https://t.me/",
            clean_link,
            flags=re.IGNORECASE,
        )
    # Normalize preview URLs like https://t.me/s/username -> https://t.me/username
    clean_link = re.sub(
        r"^https://t\.me/s/([A-Za-z0-9_]{4,})(?:[/?].*)?$",
        r"https://t.me/\1",
        clean_link,
        flags=re.IGNORECASE,
    )

    is_invite = "/+" in clean_link or "joinchat/" in clean_link.lower()
    public_slug = ""
    if not is_invite and "t.me/" in clean_link:
        tail = clean_link.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        first_seg = tail.split("/", 1)[0].strip()
        if first_seg and not first_seg.startswith("+") and first_seg.lower() not in (
            "c", "s", "addlist", "joinchat", "proxy", "socks"
        ):
            public_slug = first_seg.lstrip("@")
            clean_link = f"https://t.me/{public_slug}"

    # 2. For public channels, check Telegram's ?embed=1 widget endpoint FIRST because it explicitly
    #    distinguishes:
    #      - Terms of Service / copyright / law bans -> non-empty ban text in tgme_widget_message_error (DEAD)
    #      - Nonexistent / deleted username          -> "Channel with username @... not found" (DEAD)
    #      - Live 18+ / sensitive-content channels   -> EMPTY tgme_widget_message_error (e.g. @oiMALIBOGonly)
    #                                                   or pornographic/sensitive notice or live data-peer (ACTIVE)
    embed_ban_reason = ""
    embed_confirmed_adult = False
    embed_confirmed_live_peer = False
    embed_empty_sensitive_gate = False
    embed_post_not_found = False
    if public_slug:
        try:
            emb_resp = await _ASYNC_HTTP_CLIENT.get(f"https://t.me/{public_slug}/1?embed=1")
            if emb_resp.status_code == 200:
                emb_text = emb_resp.text
                embed_ban_reason = _detect_telegram_ban_reason(emb_text, allow_web_porn_notice=True)
                if embed_ban_reason:
                    return False, embed_ban_reason
                if _is_telegram_adult_sensitive_notice(emb_text):
                    embed_confirmed_adult = True
                else:
                    m_emb_err = re.search(
                        r'class=["\']tgme_widget_message_error["\'][^>]*>(.*?)</div>',
                        emb_text,
                        re.S,
                    )
                    if m_emb_err is not None:
                        emb_err_txt = html.unescape(re.sub(r"<[^>]+>", "", m_emb_err.group(1)).strip())
                        if emb_err_txt == "":
                            # Telegram suppresses embed text with an empty error div on live 18+/sensitive channels
                            embed_empty_sensitive_gate = True
                        elif emb_err_txt.lower() == "post not found":
                            # Channel exists (not ToS-banned, not "channel not found"), only post #1 was deleted
                            embed_post_not_found = True
                    elif (
                        re.search(r'data-peer=["\']c\d+', emb_text)
                        or "please open telegram to view this post" in emb_text.lower()
                    ):
                        embed_confirmed_live_peer = True
        except Exception as exc:  # noqa: BLE001
            log.debug("[LINK CHECK] Embed check skipped for @%s: %s", public_slug, exc)

    # 3. Check the web preview of the link on t.me
    for attempt in range(2):
        try:
            resp = await _ASYNC_HTTP_CLIENT.get(clean_link)
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                return False, f"Telegram rate limit / server busy (HTTP {resp.status_code})"

            if resp.status_code == 404:
                return False, "Link not found (HTTP 404)"

            if resp.status_code != 200:
                return False, f"Unexpected HTTP status {resp.status_code}"

            # If Telegram redirected an invalid username link away from t.me to telegram.org homepage
            final_host = (resp.url.host or "").lower()
            if final_host and "telegram.org" in final_host and "t.me" not in final_host:
                return False, "Invalid or deleted channel (redirected to telegram.org)"

            text = resp.text

            # Check for Terms of Service ban, copyright/law blocks, and unavailable notices (entity-decoded).
            # 18+ / sensitive / pornographic content notices are explicitly allowed and treated as ACTIVE.
            ban_reason = _detect_telegram_ban_reason(text, allow_web_porn_notice=True)
            if ban_reason:
                return False, ban_reason

            is_adult_page = _is_telegram_adult_sensitive_notice(text)

            # Extract channel title and subscriber/member count from t.me preview
            m_title = re.search(
                r'class=["\']tgme_page_title["\'][^>]*>\s*(?:<span[^>]*>)?(.*?)(?:</span>)?\s*</div>',
                text,
                re.S,
            )
            title_str = html.unescape(re.sub(r"<[^>]+>", "", m_title.group(1)).strip()) if m_title else ""

            m_extra = re.search(r'class=["\']tgme_page_extra["\'][^>]*>(.*?)</div>', text, re.S)
            extra_str = html.unescape(re.sub(r"<[^>]+>", "", m_extra.group(1)).strip()) if m_extra else ""

            # Reject error / placeholder titles such as "Not available" or "Join group chat on Telegram"
            if title_str and (
                title_str.lower() in _GENERIC_TG_PAGE_TITLES
                or title_str.lower().startswith("telegram: contact @")
            ):
                if (
                    not (is_adult_page or embed_confirmed_adult or embed_empty_sensitive_gate)
                    and ("not available" in title_str.lower() or "unavailable" in title_str.lower())
                ):
                    return False, "Not available (Channel banned or restricted)"
                title_str = ""

            if title_str or extra_str:
                if title_str and extra_str:
                    return True, f"{title_str} ({extra_str})"
                if title_str:
                    if is_adult_page or embed_confirmed_adult or embed_empty_sensitive_gate:
                        return True, f"{title_str} (18+ Sensitive Content)"
                    return True, title_str
                return True, f"Active ({extra_str})"

            # Check if t.me/<slug> confirms an existing public channel stub ("View in Telegram" / "Preview channel")
            text_low = text.lower()
            main_is_channel_stub = bool(public_slug) and (
                'class="tgme_page_context_link"' in text
                or "preview channel" in text_low
                or "view in telegram" in text_low
                or "you can view posts" in text_low
            )

            # Live 18+ / sensitive content channel where Telegram hides public title/subscriber preview
            # (e.g. https://t.me/oiMALIBOGonly -> "18+ sensitive content" modal in app, empty embed error div on web)
            if (
                is_adult_page
                or embed_confirmed_adult
                or (embed_empty_sensitive_gate and main_is_channel_stub)
            ):
                display = f"@{public_slug}" if public_slug else "Active"
                return True, f"{display} (18+ Sensitive Content)"

            # Public channel confirmed live by embed widget peer data or "Post not found" on an existing channel stub
            if embed_confirmed_live_peer or (embed_post_not_found and main_is_channel_stub):
                display = f"@{public_slug}" if public_slug else "Active"
                return True, f"{display} (Active)"

            if is_invite:
                return False, "Invite link is expired, banned, or invalid"
            return False, embed_ban_reason or "Channel not available, banned, or not found"

        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            log.warning("[LINK CHECK] Network exception for %s: %s", clean_link, exc)
            return False, f"Connection error ({exc})"

    return False, "Could not verify link"


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
    kind: str    # "username" | "invite" | "addlist"
    value: str
    url: str

    @property
    def is_addlist(self) -> bool:
        return self.kind == "addlist"

    @property
    def key(self) -> str:
        v = self.value.lower() if self.kind == "username" else self.value
        return f"{self.kind}:{v}"


def parse_link(raw: str) -> Optional[Link]:
    """Turn a raw link / mention into a normalised Link (or None)."""
    raw = raw.strip().rstrip(".,;:!?*_~|)>]}")
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
# Persistent State (Sent Links, Cache & 5-by-5 Checklists)
# --------------------------------------------------------------------------- #
# Two separate buckets:
#   "ch"  -> Working channel / group links
#   "al"  -> All https://t.me/addlist/... folder links
STATE: dict = {}


def _empty_bucket() -> dict:
    return {
        "counter": 0,
        "done": set(),   # set of completed link numbers (1, 2, 3...)
        "items": [],     # [{"n": 1, "url": "...", "info": "..."}]
    }


def load_cache() -> dict:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def load_sent() -> dict:
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    STATE.clear()
    STATE["keys"] = set(data.get("keys", []))

    for kind in ("ch", "al"):
        raw_b = data.get(kind)
        if raw_b is None and kind == "ch" and "items" in data:
            done_till = int(data.get("done_till", 0))
            STATE[kind] = {
                "counter": int(data.get("counter", 0)),
                "done": set(range(1, done_till + 1)),
                "items": [
                    {"n": it["n"], "url": it["url"], "info": it.get("info", "")}
                    for it in data.get("items", [])
                ],
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


# --------------------------------------------------------------------------- #
# Formatting & 5-by-5 Checklist Buttons
# --------------------------------------------------------------------------- #
def format_items(kind: str, items: list[dict]) -> str:
    """Format links numbered 1., 2., 3. with an empty line after every 5 links."""
    done = STATE[kind]["done"]
    header = (
        "📁 <b>Addlist / Folder Links (https://t.me/addlist/)</b>"
        if kind == "al"
        else "✅ <b>Working Channel / Group Links</b>"
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


async def deliver_batch(bot, chat_id: int, kind: str,
                        batch: list[tuple[Link, str]]) -> list[dict]:
    """Number + persist + send a batch of links of a given kind ('ch' or 'al')."""
    items: list[dict] = []
    for link, info in batch:
        if link.key in STATE["keys"]:
            continue
        STATE["keys"].add(link.key)
        STATE[kind]["counter"] += 1
        it = {"n": STATE[kind]["counter"], "url": link.url, "info": info}
        STATE[kind]["items"].append(it)
        items.append(it)
    if items:
        save_sent()
        await send_items(bot, chat_id, kind, items)
    return items


def make_txt_report(items: list[dict], include_info: bool = True) -> bytes:
    """Format plain text report with 5-by-5 spacing."""
    lines = []
    for i, it in enumerate(items):
        if i > 0 and i % 5 == 0:
            lines.append("")
        info = it.get("info", "")
        if include_info and info:
            lines.append(f"{it['n']}. {it['url']}  |  {info}")
        else:
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
# Session Loading & Bot Setup
# --------------------------------------------------------------------------- #
HELP = (
    "👋 <b>Channel Link Sorter</b>\n\n"
    f"Logs in to your Telegram account once, opens the pinned channel <b>{CHANNEL_NAME}</b>, "
    "validates every link using the <code>/checklinks</code> logic, sends "
    "<b>only the working channel links</b>, and separately sends <b>all "
    "<code>https://t.me/addlist/</code> links</b> in <b>5-by-5</b> numbered lists "
    "with <b>5-by-5 checklists</b>.\n\n"
    "<b>Account</b>\n"
    "/login – log in with phone + OTP (one time)\n"
    "/setsession &lt;string&gt; – log in directly with a session string (no OTP)\n"
    "/session – get your SESSION_STRING for cloud servers\n"
    "/logout – log out and delete the saved session\n\n"
    "<b>Scan & Validate</b>\n"
    "/scan – scan whole channel, validate links & send working links + all addlists\n"
    "/scan 500 – scan only the latest 500 messages\n"
    "/scan fresh – ignore cached link checks and re-validate every link\n\n"
    "<b>Progress & History</b>\n"
    "/progress – check completed vs pending counts\n"
    "/pending – resend uncompleted links\n"
    "/done 25 – mark working channel links 1–25 completed (<code>/done addlist 10</code> for addlists)\n"
    "/resetsent – clear history & restart numbering at 1\n"
    "/cancel – stop a running scan / login"
)


def find_user_session():
    """Return StringSession if SESSION_STRING or session_string.txt exists,
    otherwise locate user.session (or any non-bot .session file)."""
    if SESSION_STRING:
        log.info("Loading session from SESSION_STRING environment variable")
        return StringSession(SESSION_STRING)
    if os.path.exists(SESSION_FILE):
        try:
            s = open(SESSION_FILE, "r", encoding="utf-8").read().strip()
            if s:
                log.info("Loading session from %s", SESSION_FILE)
                return StringSession(s)
        except OSError:
            pass
    default_file = USER_SESSION + ".session"
    if os.path.exists(default_file) and os.path.getsize(default_file) > 0:
        log.info("Loading session file: %s", default_file)
        return USER_SESSION
    for folder in dict.fromkeys([DATA_DIR, BASE_DIR, os.getcwd()]):
        try:
            for fname in sorted(os.listdir(folder)):
                if fname.endswith(".session") and fname != "bot.session":
                    full = os.path.join(folder, fname)
                    if os.path.getsize(full) > 0:
                        log.info("Found existing session file: %s", full)
                        return full[:-8]
        except OSError:
            pass
    return USER_SESSION


def new_user_client(sess=None, fresh_file: bool = False) -> TelegramClient:
    if sess is None:
        sess = USER_SESSION if fresh_file else find_user_session()
    return TelegramClient(
        sess, API_ID, API_HASH,
        connection_retries=None, retry_delay=5, auto_reconnect=True,
    )


async def is_authorized_safe(client: TelegramClient) -> bool:
    try:
        if not client.is_connected():
            await client.connect()
        return await client.is_user_authorized()
    except errors.AuthKeyUnregisteredError:
        return False


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Put BOT_TOKEN in the .env file.")

    load_sent()
    clients = {"user": new_user_client()}
    await clients["user"].connect()

    owner = {"id": load_owner()}
    if await is_authorized_safe(clients["user"]):
        me = await clients["user"].get_me()
        log.info("Using saved session: %s (id=%s)", me.first_name, me.id)
        if not owner["id"]:
            owner["id"] = me.id
            save_owner(me.id)
    else:
        log.info("No active session yet - send /login to the bot to log in with OTP")

    bot = TelegramClient(
        BOT_SESSION, API_ID, API_HASH,
        connection_retries=None, retry_delay=5, auto_reconnect=True,
    )
    bot.parse_mode = "html"
    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info("Bot running as @%s", bot_me.username)

    scan_lock = asyncio.Lock()
    cancel_event = asyncio.Event()
    login_state: dict[int, dict] = {}       # chat_id -> {"step", "phone", "hash"}

    def is_owner(event) -> bool:
        return owner["id"] == 0 or event.sender_id == owner["id"]

    async def deny(event):
        await event.reply("⛔ This bot is private.")

    # ---------------- /start & /help ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/(start|help)"))
    async def on_start(event):
        if not is_owner(event):
            return await deny(event)
        if await is_authorized_safe(clients["user"]):
            me = await clients["user"].get_me()
            state = f"🟢 Logged in as <b>{html.escape(me.first_name or '')}</b>"
        else:
            state = "🔴 Not logged in – send /login"
        await event.reply(f"{HELP}\n\n{state}", link_preview=False)

    # ---------------- /login ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/login"))
    async def on_login(event):
        if not event.is_private:
            return await event.reply("Use /login in a private chat with me.")
        if not is_owner(event):
            return await deny(event)
        user = clients["user"]
        if await is_authorized_safe(user):
            me = await user.get_me()
            return await event.reply(
                f"✅ Already logged in as <b>{html.escape(me.first_name or '')}</b>. "
                "Use /logout first to switch account."
            )
        try:
            await user.disconnect()
        except Exception:  # noqa: BLE001
            pass
        clients["user"] = new_user_client(fresh_file=True)
        await clients["user"].connect()

        login_state[event.chat_id] = {"step": "phone"}
        await event.reply(
            "📱 Send your phone number with country code\n"
            "e.g. <code>+919876543210</code>\n\n"
            "<i>(Or paste a Telethon SESSION_STRING directly to log in without OTP)</i>\n"
            "/cancel to abort"
        )

    async def finish_login(event):
        login_state.pop(event.chat_id, None)
        me = await clients["user"].get_me()
        if not OWNER_ID:
            owner["id"] = event.sender_id
            save_owner(event.sender_id)
        s_str = StringSession.save(clients["user"].session)
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                f.write(s_str)
        except OSError:
            pass
        log.info("Logged in as %s (id=%s)", me.first_name, me.id)
        await event.respond(
            f"✅ Logged in as <b>{html.escape(me.first_name or '')}</b>.\n"
            "Session saved – you won't need OTP again.\n\n"
            "☁️ <b>For cloud servers (so redeploys never ask for login again):</b>\n"
            f"<code>/setsession {html.escape(s_str)}</code>\n\n"
            "Send /scan to validate and extract all working links from the channel."
        )

    # ---------------- /setsession ---------------- #
    async def apply_session_string(event, raw_str: str):
        raw_str = raw_str.replace("SESSION_STRING=", "").strip()
        try:
            await event.delete()
        except Exception:  # noqa: BLE001
            pass
        try:
            test_client = new_user_client(sess=StringSession(raw_str))
            await test_client.connect()
            if not await test_client.is_user_authorized():
                await test_client.disconnect()
                return await event.respond("❌ That session string is not authorized or has expired.")
        except Exception as e:  # noqa: BLE001
            return await event.respond(f"❌ Invalid session string ({e.__class__.__name__}).")

        try:
            await clients["user"].disconnect()
        except Exception:  # noqa: BLE001
            pass
        clients["user"] = test_client
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            f.write(raw_str)
        await finish_login(event)

    @bot.on(events.NewMessage(pattern=r"^/setsession(?:\s+(.+))?$"))
    async def on_setsession(event):
        if not event.is_private:
            return
        if not is_owner(event):
            return await deny(event)
        s_str = (event.pattern_match.group(1) or "").strip()
        if not s_str:
            return await event.reply("Usage: <code>/setsession &lt;your_session_string&gt;</code>")
        await apply_session_string(event, s_str)

    # ---------------- /session ---------------- #
    @bot.on(events.NewMessage(pattern=r"^/session"))
    async def on_session(event):
        if not event.is_private:
            return
        if not is_owner(event):
            return await deny(event)
        user = clients["user"]
        if not await is_authorized_safe(user):
            return await event.reply("🔴 Not logged in. Send /login first.")
        s_str = StringSession.save(user.session)
        await event.reply(
            "🔑 <b>Your Telethon Session String</b>\n"
            "Send this command to your server bot (or set <code>SESSION_STRING</code> in server env vars):\n\n"
            f"<code>/setsession {html.escape(s_str)}</code>"
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
                if len(text) > 100 and not text.startswith("+"):
                    return await apply_session_string(event, text)
                phone = re.sub(r"[^\d+]", "", text)
                if not re.fullmatch(r"\+?\d{7,15}", phone):
                    return await event.reply("❌ That doesn't look like a phone number. Try again.")
                phone = phone if phone.startswith("+") else "+" + phone
                sent = await user.send_code_request(phone)
                st.update(step="code", phone=phone, hash=sent.phone_code_hash)
                where = (
                    "your Telegram app (check the Telegram service chat on that account)"
                    if isinstance(sent.type, SentCodeTypeApp)
                    else f"SMS / {sent.type.__class__.__name__}"
                )
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
        if await is_authorized_safe(user):
            try:
                await user.log_out()
            except Exception:  # noqa: BLE001
                pass
        for fpath in (SESSION_FILE, USER_SESSION + ".session", OWNER_FILE):
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass
        clients["user"] = new_user_client(fresh_file=True)
        await clients["user"].connect()
        owner["id"] = OWNER_ID
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
            await event.reply("🛑 Stopping…")
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
            f"🗑 Cleared history ({ch_n} working channel links, {al_n} addlist links).\n"
            "Numbering restarts at 1 and next /scan sends everything again."
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
        for kind, label in (("ch", "✅ Working Channel/Group Links"), ("al", "📁 Addlist Links")):
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

    # ---------------- /scan (extracts + validates via /checklinks logic) --- #
    @bot.on(events.NewMessage(pattern=r"^/scan(?:@\w+)?(.*)$"))
    async def on_scan(event):
        if not is_owner(event):
            return await deny(event)
        user = clients["user"]
        if not await is_authorized_safe(user):
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

    links: dict[str, Link] = {}
    already_sent = 0
    scanned = 0
    offset_id = 0

    while not cancel_event.is_set():
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
                    if link.key in STATE["keys"]:
                        already_sent += 1
                        continue
                    links[link.key] = link
                if scanned % 300 == 0:
                    await progress(
                        f"📥 <b>{name}</b>: {scanned} messages read, "
                        f"{len(links)} new unique links found…"
                    )
            break
        except errors.FloodWaitError as e:
            await progress(
                f"⏳ Telegram rate limit while reading {name} – waiting {e.seconds}s…",
                True,
            )
            await asyncio.sleep(e.seconds + 1)

    if cancel_event.is_set():
        await progress("🛑 Scan cancelled.", True)
        return

    # Oldest post first
    link_list = list(links.values())[::-1]
    await validate_and_deliver_links(
        bot, chat_id, name, scanned, link_list, already_sent, fresh, progress, cancel_event
    )


async def validate_and_deliver_links(
    bot,
    chat_id: int,
    source_name: str,
    scanned_msgs: int,
    link_list: list[Link],
    already_sent: int,
    fresh: bool,
    progress: Progress,
    cancel_event: asyncio.Event,
):
    """
    1. Separates all `https://t.me/addlist/` links and delivers ALL of them.
    2. Validates all channel/group links using `check_channel_link_alive` (from
       Channel link provider's /checklinks) and delivers ONLY the working ones
       in 5-by-5 numbered blocks with 5-by-5 checklists.
    """
    addlist_links = [l for l in link_list if l.is_addlist]
    channel_links = [l for l in link_list if not l.is_addlist]

    cache = load_cache()
    working_sent: list[dict] = []
    pending_ch: list[tuple[Link, str]] = []
    dead_count = 0
    dup_channel_count = 0
    seen_titles: set[str] = set()
    total_ch = len(channel_links)

    async def flush_ch():
        nonlocal pending_ch
        if pending_ch:
            working_sent.extend(await deliver_batch(bot, chat_id, "ch", pending_ch))
            pending_ch = []

    for idx, link in enumerate(channel_links, 1):
        if cancel_event.is_set():
            break
        if link.key in STATE["keys"]:
            already_sent += 1
            continue

        cached = cache.get(link.key)
        if (
            cached
            and cached.get("v") == 3
            and not fresh
            and (time.time() - cached.get("ts", 0) < CACHE_HOURS * 3600)
        ):
            is_alive, detail = bool(cached.get("ok")), str(cached.get("info") or "")
        else:
            if idx > 1:
                await asyncio.sleep(CHECK_DELAY)
            is_alive, detail = await check_channel_link_alive(link.url)
            cache[link.key] = {"v": 3, "ok": is_alive, "info": detail, "ts": time.time()}
            if idx % 20 == 0:
                save_cache(cache)

        if is_alive:
            # Also deduplicate two different invite links pointing to the exact same channel title+count
            norm_detail = detail.strip().casefold() if detail else ""
            if norm_detail and not norm_detail.startswith("active") and norm_detail in seen_titles:
                dup_channel_count += 1
            else:
                if norm_detail:
                    seen_titles.add(norm_detail)
                pending_ch.append((link, detail))
                if len(pending_ch) >= LINKS_PER_MESSAGE:
                    await flush_ch()
        else:
            dead_count += 1

        await progress(
            f"🔍 Validating channel links {idx}/{total_ch}\n"
            f"✅ Working: <b>{len(working_sent) + len(pending_ch)}</b>  |  "
            f"❌ Expired/Banned: {dead_count}  |  "
            f"📁 Addlists: {len(addlist_links)}"
        )

    save_cache(cache)
    await flush_ch()

    # Deliver ALL https://t.me/addlist/ links separately
    addlist_sent: list[dict] = []
    if addlist_links and not cancel_event.is_set():
        addlist_sent = await deliver_batch(
            bot, chat_id, "al", [(l, "Addlist Folder") for l in addlist_links]
        )

    status_tag = "stopped" if cancel_event.is_set() else "validation complete"
    summary = (
        f"📊 <b>{source_name}</b> – {status_tag}\n"
        f"Messages scanned: {scanned_msgs}\n"
        f"Channel links checked: {total_ch}\n"
        f"✅ <b>New Working Channel Links: {len(working_sent)}</b>\n"
        f"❌ Expired / Banned / Invalid: {dead_count}\n"
        f"♊ Duplicate channels skipped: {dup_channel_count}\n"
        f"📁 <b>New Addlist (Folder) Links: {len(addlist_sent)}</b>\n"
        f"🔁 Already sent before: {already_sent}"
    )
    await progress(summary, True)

    if not working_sent and not addlist_sent:
        await bot.send_message(
            chat_id,
            "No new working links or addlist links found (use /resetsent to clear sent history).",
        )
        return

    if working_sent:
        f = io.BytesIO(make_txt_report(working_sent, include_info=True))
        f.name = "working_channel_links.txt"
        await bot.send_file(
            chat_id, f,
            caption=(
                f"✅ {len(working_sent)} Working Channel/Group links "
                f"(#{working_sent[0]['n']}–#{working_sent[-1]['n']})"
            ),
        )

    if addlist_sent:
        f = io.BytesIO(make_txt_report(addlist_sent, include_info=False))
        f.name = "addlist_links.txt"
        await bot.send_file(
            chat_id, f,
            caption=(
                f"📁 {len(addlist_sent)} Addlist/Folder links "
                f"(#{addlist_sent[0]['n']}–#{addlist_sent[-1]['n']})"
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
