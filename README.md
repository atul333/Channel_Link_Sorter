# Channel Link Sorter Bot (@Link_sorter_A1_bot)

Logs in to your Telegram account with Telethon (via `/login` phone + OTP or
`/setsession`), opens your first pinned channel **Mega premium**, and extracts
**all** Telegram links present in the channel without checking link expiration.

## Features
- **One-Time Telethon Login:** Log in once via `/login` (phone + OTP) or `/setsession <string>`. Saved to `user.session` / `session_string.txt`.
- **No Expiration Checks / Instant Extraction:** Extracts all links immediately without checking whether links are expired or accessible.
- **Separates `https://t.me/addlist/` Links:**
  - **📢 Telegram Channel / Group Links** (`t.me/username`, `t.me/+hash`, `t.me/joinchat/hash`, `t.me/c/...`) -> sent in 5-by-5 numbered lists + `channel_links.txt`.
  - **📁 Addlist / Folder Links** (`https://t.me/addlist/...`) -> separated into their own 5-by-5 numbered lists + `addlist_links.txt`.
- **5-by-5 Grouping & Separate 5-by-5 Checklists:**
  - Links are numbered `1.`, `2.`, `3.`… with a blank line after every 5 links.
  - Each message has separate checklist toggle buttons for every 5-link block (`⬜ 1–5`, `⬜ 6–10`, `⬜ 11–15`, `⬜ 16–20`) plus `✅ Check All`.
- **No Duplicates / No Repeats:** Duplicate links are removed and previously sent links are stored in `sent_links.json`.

## Commands
| Command | Description |
|---|---|
| `/login` | Log in to your Telegram account with phone + OTP |
| `/setsession <string>` | Log in directly using a session string (no OTP needed on cloud servers) |
| `/session` | Get your session string to use on a cloud server |
| `/logout` | Log out and delete the saved session |
| `/scan` | Extract all links from the whole channel (`/scan 500` for latest 500 messages) |
| `/progress` | Show completed vs pending counts and next link |
| `/pending` | Resend uncompleted links |
| `/done 25` | Mark Channel links `1–25` completed (`/done addlist 10` for Addlist links) |
| `/resetsent` | Clear sent history and restart numbering at `1` |
| `/cancel` | Stop a running scan or login |
