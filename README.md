# Weather Signal Bot

Pulls NBM (National Blend of Models) forecast data + current METAR obs for
LAX, SFO, MIA, LGA, and ORD, then sends you a Telegram message with the
TXN/XND signals — free, running on GitHub's servers, nothing installed on
your phone.

## Setup (about 10 minutes)

### 1. Create a Telegram bot (free)
1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts.
2. It gives you a **bot token** — save it.
3. Message your new bot anything (e.g. "hi") so it can reply to you.
4. Get your **chat ID**: visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   after messaging the bot — look for `"chat":{"id":...}` in the response.

### 2. Create a GitHub repo
1. Create a **private** repo (private is fine — you still get 2,000 free
   Actions minutes/month, plenty for this).
2. Upload these files, keeping the folder structure:
   ```
   weather-bot/
   ├── weather_check.py
   ├── README.md
   └── .github/workflows/weather-check.yml
   ```

### 3. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**
- `TELEGRAM_BOT_TOKEN` → the token from BotFather
- `TELEGRAM_CHAT_ID` → your chat ID

### 4. Test it
Go to the **Actions** tab → **Weather Check** → **Run workflow** (this is
the `workflow_dispatch` trigger — you can do this from the GitHub mobile
app too). You should get a Telegram message within a minute or two.

### 5. Let it run
Once secrets are set, it runs automatically:
- **01:45 UTC** — nightly pull on the NBM 01Z cycle
- **13:45 UTC** — morning pull on the NBM 13Z cycle

Convert those to your local time and adjust the cron lines in
`weather-check.yml` if you want different windows (cron times are always
UTC on GitHub Actions).

## What it does NOT do
- It does not place trades. It only pulls data and evaluates signals, then
  notifies you — you still make the call and execute manually.
- It does not do the 11am local METAR spot-check for you automatically,
  since your 5 cities span 3 timezones and one UTC cron can't hit "11am"
  for all of them. Use **Run workflow** manually from your phone for that,
  or add more cron lines per timezone if you want it automatic.
- TXN/XND parsing pulls the *first* value in each row, which corresponds
  to the nearest upcoming forecast period in the bulletin — double check
  this lines up with "tomorrow" vs "today" depending on what time you're
  running it, especially right around local midnight.

## Extending it
- Add your bucket-convergence and app-price checks into
  `evaluate_station()` in `weather_check.py` — right now it only flags
  the XND threshold as an example.
- Add NBP (probabilistic P10/P50/P90) by requesting `ele=nbs,nbp` in
  `fetch_nbm_bulletins()` and parsing the extra rows.

Done! Congratulations on your new bot. You will find it at t.me/gcmpBOT. You can now add a description, about section and profile picture for your bot, see /help for a list of commands. By the way, when you've finished creating your cool bot, ping our Bot Support if you want a better username for it. Just make sure the bot is fully operational before you do this.

Use this token to access the HTTP API:
8746163302:AAGYtOdPBiPi50swdaKURbxI25GivppwENU
Keep your token secure and store it safely, it can be used by anyone to control your bot.

For a description of the Bot API, see this page: https://core.telegram.org/bots/api
