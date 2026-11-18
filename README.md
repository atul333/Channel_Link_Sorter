# Channel Link Sorter Bot (@Link_sorter_A1_bot)

Logs in to your Telegram account (Telethon, one time only), opens the pinned
channel **Mega premium**, extracts every Telegram channel/group link, checks
each one and sends back **only the accessible links**.

## Setup
1. Get `API_ID` and `API_HASH` at https://my.telegram.org → *API development tools*.
2. Put them in `.env` (the bot token is already filled in).
3. Install and run:
   ```powershell
   pip install -r requirements.txt
   python bot.py
   ```
4. **First run only:** the console asks for your phone number, the login code
   Telegram sends you and your 2FA password (if set). This is saved in `user.session`,
   so you won't be asked again.
5. From the same Telegram account, open t.me/Link_sorter_A1_bot and send `/scan`.

## Commands
| Command | What it does |
|---|---|
| `/scan` | Scan the whole channel |
| `/scan 500` | Scan only the latest 500 messages |
| `/scan fresh` | Ignore cached results, re-check every link |
| `/cancel` | Stop a running scan |

## How "accessible" is decided
| Link type | Accessible when |
|---|---|
| `t.me/username` | Username exists, is a channel/group, not banned/blocked |
| `t.me/+hash`, `t.me/joinchat/hash` | Invite is valid (not expired/revoked) |
| `t.me/c/123/45` (private post) | Your account is a member of that chat |

Links to users/bots are skipped. Results go to chat as plain links, plus an
`accessible_links.txt` file with channel titles.

## Notes
- Telegram rate-limits username lookups. If a long wait comes up, the bot stops
  and shows partial results; checked links are cached in `link_cache.json`, so
  running `/scan` again carries on from where it stopped.
- Keep `.env` and `*.session` private: they give full access to your bot/account.

