# Channel Link Sorter Bot (@Link_sorter_A1_bot)

Logs in to your Telegram account with Telethon (phone + OTP, entered through
the bot chat, one time only), opens the pinned channel **Mega premium**,
extracts every Telegram channel/group link, checks each one and sends back
**only the accessible links**.

## Run
```powershell
pip install -r requirements.txt
python bot.py
```
No API_ID / API_HASH needed – only the bot token in `.env`.

## Log in (first time only)
1. Open t.me/Link_sorter_A1_bot and send `/login`.
2. Send your phone number, e.g. `+919876543210`.
3. Telegram sends you an OTP. Reply with it **using spaces**, e.g. `1 2 3 4 5`
   (Telegram cancels the login if the plain code is shared in a chat).
4. If two-step verification is on, send your password (deleted right away).
5. Done – the session is saved to `user.session`; restarts don't need OTP.

## Commands
| Command | What it does |
|---|---|
| `/login` | Log in with phone + OTP |
| `/logout` | Log out and delete the session |
| `/scan` | Scan the whole channel |
| `/scan 500` | Scan only the latest 500 messages |
| `/scan fresh` | Ignore cached results, re-check every link |
| `/cancel` | Stop a running scan or login |

## How "accessible" is decided
| Link type | Accessible when |
|---|---|
| `t.me/username` | Username exists, is a channel/group, not banned/blocked |
| `t.me/+hash`, `t.me/joinchat/hash` | Invite is valid (not expired/revoked) |
| `t.me/c/123/45` (private post) | Your account is a member of that chat |

## Notes
- Whoever logs in first becomes the bot owner. Set `OWNER_ID` in `.env` to lock it to you.
- Telegram rate-limits username lookups; results are cached in `link_cache.json`
  so a repeated `/scan` continues where it stopped.
- Telethon always needs app keys internally; by default the public keys of the
  open-source Telegram Desktop app are used. You can put your own in `.env`.
- Keep `.env` and `*.session` private.
