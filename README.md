# Channel Link Sorter Bot (@Link_sorter_A1_bot)

Logs in to your Telegram account with Telethon (via `/login` phone + OTP or
`/setsession`), opens your first pinned channel **Mega premium**, validates
every link using the `/checklinks` logic from **Channel link provider**, sends
**only the working channel links**, and separately provides **all
`https://t.me/addlist/` links** in 5-by-5 numbered blocks with 5-by-5 checklists.

## Features
- **Fast `/checklinks` Link Validation (Zero Telegram Rate Limits):**
  - Uses the exact HTTP web-preview + `?embed=1` Terms-of-Service ban detection from `Channel link provider`.
  - Filters out expired/revoked invite links, deleted usernames, and channels banned for Terms of Service or copyright violations.
  - Sends **only the working channel/group links** (`working_channel_links.txt`).
- **All `https://t.me/addlist/` Links Provided Separately:**
  - All folder links starting with `https://t.me/addlist/` are extracted and sent in their own separate numbered 5-by-5 list + `addlist_links.txt`.
- **5-by-5 Grouping & Separate 5-by-5 Checklists:**
  - Links are numbered `1.`, `2.`, `3.`… with a blank line after every 5 links.
  - Each message has separate checklist toggle buttons for every 5-link block (`⬜ 1–5`, `⬜ 6–10`, `⬜ 11–15`, `⬜ 16–20`) plus `✅ Check All`.
- **No Duplicates / No Repeats:** Duplicate links (and different invite links to the exact same channel) are skipped, and previously sent links are stored in `sent_links.json`.

## Commands
| Command | Description |
|---|---|
| `/login` | Log in to your Telegram account with phone + OTP |
| `/setsession <string>` | Log in directly using a session string (no OTP needed on cloud servers) |
| `/session` | Get your session string to use on a cloud server |
| `/logout` | Log out and delete the saved session |
| `/scan` | Scan whole channel, validate links & return working links + all addlists |
| `/scan 500` | Scan only the latest 500 messages |
| `/scan fresh` | Ignore cached link checks and re-validate every link |
| `/checklinks` | Paste or forward any links to validate and get working links + addlists |
| `/progress` | Show completed vs pending counts and next link |
| `/pending` | Resend uncompleted links |
| `/done 25` | Mark Working Channel links `1–25` completed (`/done addlist 10` for Addlist links) |
| `/resetsent` | Clear sent history and restart numbering at `1` |
| `/cancel` | Stop a running scan or login |
