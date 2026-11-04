# Morning Brief

Your personal news desk on Telegram: AI, energy, Africa and security.

## What it does

| Part | Runs on | What you get |
|---|---|---|
| Daily brief | GitHub Actions, 6 AM Central | Written brief (AI x Energy, AI, Energy, Africa, Security) + voice note |
| Urgent alerts | GitHub Actions, every 30 min | 🚨 message only for big events (exchange hacks, exploited critical flaws, grid emergencies, blackouts) |
| Chat | Free: Cloudflare + GitHub Actions (about 1 min reply) or Heroku (instant) | Ask anything in plain French or English, get a summary |

Things you can send the bot:
- `Résume-moi les grands titres de la semaine dernière en Côte d'Ivoire`
- `Data center power news from the last 3 days`
- `Top AI news in Nigeria this month, with a voice version`
- `/brief` runs the full morning brief right now
- `/help` shows examples

Only your chat id gets answers; anyone else is told it is a private bot.

## Setup

### 1. Telegram bot
1. In Telegram, message `@BotFather`, send `/newbot`, pick a name and a username ending in `bot`. Copy the **token**.
2. Open your bot, press **Start**, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`.
   Your **chat id** is the number in `"chat":{"id": ...}`.
   Do this before step 4: once the chat is connected, this page stays empty.

### 2. Anthropic key
console.anthropic.com → Billing: add $5 → API Keys → Create Key.

### 3. GitHub (daily brief + alerts)
1. github.com/new → name `morning-brief` → **Public** → Create (no README).
2. From this folder:
   ```bash
   git init && git add . && git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/morning-brief.git
   git push -u origin main
   ```
3. Settings → Secrets and variables → Actions → add `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Settings → Actions → General → Workflow permissions → **Read and write** → Save.
5. Actions → morning-brief → Run workflow → `daily`. Your brief arrives in 2 to 3 minutes.

### 4A. Chat, free option: Cloudflare relay (replies in about 1 minute, $0 forever, no card)

How it works: Telegram → Cloudflare Worker (checks it is you) → starts the `chat` GitHub Action → answer sent to Telegram.

1. **GitHub token**: github.com → your photo → Settings → Developer settings → Personal access tokens →
   **Fine-grained tokens** → Generate new token.
   - Repository access: **Only select repositories** → `morning-brief`
   - Permissions → Repository permissions → **Actions: Read and write**
   - Expiration: 1 year (put a reminder to renew it). Copy the token.
2. **Cloudflare Worker**: sign up free at dash.cloudflare.com (no card) → Workers & Pages → Create →
   Create Worker → name it `morning-brief-relay` → Deploy → **Edit code** → delete everything,
   paste the content of `relay/worker.js` → **Deploy**. Copy the Worker URL (`https://morning-brief-relay.<you>.workers.dev`).
3. Worker → **Settings → Variables and Secrets** → add 5, type **Secret**:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WEBHOOK_SECRET` (make one up, e.g. `vody_brief_8f3k2m9x`),
   `GITHUB_TOKEN`, `GITHUB_REPO` (`YOUR_USERNAME/morning-brief`).
4. **Connect Telegram to the Worker**: open this in a browser, with your values filled in:
   ```
   https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WORKER_URL>&secret_token=<WEBHOOK_SECRET>
   ```
   You should see `"ok":true` and `"Webhook was set"`.
5. Send `/help` to your bot. The answer arrives in about 30 to 60 seconds.
   If nothing comes: GitHub → Actions → **chat** shows each run and its log.

Cost: Cloudflare free plan allows 100,000 requests a day; each message uses one GitHub Actions run (free on a public repo).

### 4B. Chat, instant option: Heroku (Student Pack credit, $0 for 24 months)

Use this instead of 4A, not both. If you set up 4A before, the bot removes the webhook on startup automatically.

1. Claim the Heroku offer in your GitHub Student Developer Pack (education.github.com/pack), then sign up at heroku.com
   with the same email. Heroku may ask for a card to verify; the student credit covers the dyno.
2. Dashboard → New → Create new app → any name (e.g. `vody-brief`).
3. **Deploy** tab → Deployment method: GitHub → connect → pick `morning-brief`.
   Use **Manual deploy → Deploy Branch** (main). Do NOT enable automatic deploys:
   the alert job commits every 30 minutes and would redeploy the app each time.
4. **Settings** tab → Reveal Config Vars → add the same 3: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
5. **Resources** tab → change dyno type to **Basic** ($7/month, covered by the $13/month credit) →
   turn the `worker` switch **on**. There is no web dyno; that is normal.
6. In Telegram, send `/help` to your bot. It should answer within a few seconds.
   If not: More (top right) → View logs.

After changing code, push to GitHub, then Heroku → Deploy → Deploy Branch again.

## Customize

`config.yaml`:
- `brief.language` / `brief.voice`: `French` + `fr-FR-HenriNeural` for a French brief
- `alerts.threshold`: 7 = more alerts, 9 = only the biggest events
- `feeds`: add RSS URLs. The Africa section uses Google News search feeds;
  copy one and change the words after `q=` to follow any country or topic
- `urgent_keywords`: words (English and French) that make a story eligible for alert scoring

Local test without sending anything: `python brief.py daily --dry-run`

## Costs
- GitHub Actions: free (public repo)
- Chat: $0 with the Cloudflare relay, or Heroku Basic $7/month covered by the Student Pack credit for 24 months
- Claude Haiku: about $2 to $5/month depending on how much you chat with it
- Voice (edge-tts) and Google News feeds: free

## Notes
- GitHub schedules run in UTC and can start 5 to 20 minutes late.
- `edge-tts` uses Microsoft Edge's free read-aloud voices. It is unofficial; if it breaks, swap `send_voice()`
  in `brief.py` for OpenAI or ElevenLabs TTS.
- In chat, audio arrives as an audio file instead of a voice bubble (no ffmpeg there, to keep replies fast). Same content.
- Feeds that fail show up as `[skip]` lines in the logs and never stop a run.
