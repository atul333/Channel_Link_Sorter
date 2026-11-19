# Channel Link Sorter Bot (@Link_sorter_A1_bot)

Runs using **only the Bot Token** (no user session, no phone/OTP login) and
extracts **all** Telegram links present in a channel without checking link
expiration.

## Features
- **No User Session / No OTP Login:** Works with `BOT_TOKEN` alone.
- **No Expiration Checks / No Rate Limits:** Extracts all links immediately.
- **Separates `https://t.me/addlist/` Links:**
  - **📢 Telegram Channel / Group Links** (`t.me/username`, `t.me/+hash`, `t.me/joinchat/hash`, `t.me/c/...`)
  - **📁 Addlist / Folder Links** (`https://t.me/addlist/...`) — numbered and delivered in a separate list and separate `.txt` file.
- **Numbered 5-by-5 Blocks + Separate 5-by-5 Checklists:**
  - Links are numbered `1.`, `2.`, `3.`… with a blank line after every 5 links.
  - Each message has separate checklist toggle buttons for every 5-link block (`⬜ 1–5`, `⬜ 6–10`, `⬜ 11–15`, `⬜ 16–20`) plus `✅ Check All`.
- **No Duplicates / No Repeats:** Duplicate links are removed and previously sent links are stored in `sent_links.json`.

## 3 Ways to Extract Links from a Channel
1. **Forward Messages Directly:** Select and forward messages from the channel to @Link_sorter_A1_bot (1 message or 100+ at a time).
2. **Scan Whole Channel Automatically:** Add @Link_sorter_A1_bot as an Admin in the channel, then forward the latest post from that channel to the bot (or send `/scan https://t.me/c/XXXX/YYYY` or `/scan @channelname`). The bot reads all posts (`1..latest`) directly by ID.
3. **Upload Chat Export File:** Send an exported `.html`, `.json`, or `.txt` file to the bot.

## Commands
| Command | Description |
|---|---|
| `/start` | Show help and saved channel |
| `/scan` | Scan saved channel (or `/scan <post_link or @channel>`) |
| `/progress` | Show completed vs pending counts and next link |
| `/pending` | Resend uncompleted links |
| `/done 25` | Mark Channel links `1–25` completed (`/done addlist 10` for Addlist links) |
| `/resetsent` | Clear sent history and restart numbering at `1` |
| `/cancel` | Stop a running scan |
