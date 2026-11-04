"""Telegram chat for Morning Brief: ask for news in plain words and get a summary back.

Examples you can send the bot:
    "Résume-moi les grands titres de la semaine dernière en Côte d'Ivoire"
    "What happened with data center power deals in the last 3 days?"
    "Top AI news in Nigeria this month, in English, with a voice version"
    /brief   -> run the full morning brief right now
    /help    -> show examples

Runs 24/7 on Heroku (Procfile: worker) or any always-on machine: python bot.py
Only answers the chat id in TELEGRAM_CHAT_ID, so nobody else can spend your API credit.
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests

import brief

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER = str(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"

VOICES = {"French": "fr-FR-HenriNeural", "English": brief.CONFIG["brief"]["voice"]}

HELP = """<b>Morning Brief bot</b>

Ask in plain words, in French or English:
• <i>Résume-moi les titres de la semaine dernière en Côte d'Ivoire</i>
• <i>Data center power news from the last 3 days</i>
• <i>Top AI news in Nigeria this month, with a voice version</i>

Commands:
/brief  run the full morning brief now
/help  show this message

Your daily brief and urgent alerts keep running on their own."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["news", "brief", "answer"]},
        "queries": {"type": "array", "items": {"type": "object"}},
        "days": {"type": "integer"},
        "max_items": {"type": "integer"},
        "reply_language": {"type": "string", "enum": ["French", "English"]},
        "voice": {"type": "boolean"},
        "topic": {"type": "string"},
        "text": {"type": "string"},
    },
    "required": ["action"],
}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "takeaway": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "link": {"type": "string"},
                },
                "required": ["title", "why", "link"],
            },
        },
        "voice_script": {"type": "string"},
    },
    "required": ["takeaway", "items", "voice_script"],
}


PLANNER_SYSTEM = """You turn a Telegram message into a news search plan. Today is {today}.
Return ONLY JSON, one of:
{{"action": "news", "queries": [{{"q": str, "hl": str, "gl": str}}], "days": int, "max_items": int,
  "reply_language": "French" or "English", "voice": bool, "topic": str}}
{{"action": "brief"}}            when they want the regular morning brief now
{{"action": "answer", "text": str}}  for anything that is not a news request (reply briefly)

Rules for "news":
- 1 to 3 Google News queries. Write each query in the language of the local press
  (French for Côte d'Ivoire, Senegal, Mali, Cameroon, France...; English otherwise).
  Use quotes for exact names, OR for alternatives. Do not add date words; dates are handled separately.
- hl/gl: Google News edition, e.g. hl "fr", gl "CI" for Côte d'Ivoire; hl "fr", gl "SN" for Senegal;
  hl "en-US", gl "US" for global English news.
- days: "today" 1, "this week"/"last week" 7, "this month" 30. Default 7. Max 30.
- max_items: 8 by default, up to 15 if they ask for more.
- reply_language: the language of the user's message unless they ask otherwise.
- voice: true only if they ask for audio, vocal, voice, or to listen.
- topic: a short label like "Côte d'Ivoire, last 7 days".
Never use em dashes."""


SUMMARY_SYSTEM = """You are a sharp news editor. Write in {language}. Never use em dashes.
Group duplicate stories into one item. Rank by importance, not by recency. Be concrete: names, numbers,
why it matters. Ignore items that do not match the request."""

SUMMARY_PROMPT = """Request: {request}
Topic: {topic}

Return ONLY JSON:
{{"takeaway": "one or two sentences: the big picture",
  "items": [{{"title": str, "why": "one sentence", "link": str}}],   (at most {max_items})
  "voice_script": {voice_rule}}}

Articles (newest first):
{articles}"""


# ---------------------------------------------------------------- Telegram helpers

def reply(text: str) -> None:
    brief.send_text(text)


def typing() -> None:
    requests.post(f"{API}/sendChatAction", data={"chat_id": OWNER, "action": "typing"}, timeout=10)


# ---------------------------------------------------------------- news search

def google_news_url(q: str, hl: str, gl: str, days: int) -> str:
    lang = hl.split("-")[0]
    return (
        f"https://news.google.com/rss/search?q={quote_plus(f'{q} when:{days}d')}"
        f"&hl={hl}&gl={gl}&ceid={gl}:{lang}"
    )


def search(plan: dict) -> list[dict]:
    days = max(1, min(int(plan.get("days", 7)), 30))
    since = datetime.now(timezone.utc) - timedelta(days=days, hours=2)
    found: dict[str, dict] = {}
    for q in plan.get("queries", [])[:3]:
        url = google_news_url(q["q"], q.get("hl", "en-US"), q.get("gl", "US"), days)
        for item in brief.fetch_feed(url, "request", since):
            found[item["id"]] = item
    return sorted(found.values(), key=lambda i: i["published"], reverse=True)[:80]


def answer_news(request: str, plan: dict) -> None:
    items = search(plan)
    language = plan.get("reply_language", "English")
    if not items:
        reply("Rien trouvé pour cette recherche. Essaie d'autres mots ou une période plus longue."
              if language == "French" else
              "Nothing found for that search. Try other words or a longer period.")
        return

    articles = "\n".join(
        f"- {i['title']} ({i['published'][:10]}) {i['link']}\n  {i['summary'][:200]}" for i in items
    )
    voice_rule = ('"a spoken version, 200 to 350 words, radio style, no URLs or symbols"'
                  if plan.get("voice") else '""')
    max_items = max(3, min(int(plan.get("max_items", 8)), 15))
    out = brief.ask_claude_structured(
        SUMMARY_SYSTEM.format(language=language),
        SUMMARY_PROMPT.format(request=request, topic=plan.get("topic", ""), max_items=max_items,
                              voice_rule=voice_rule, articles=articles),
        "news_summary",
        SUMMARY_SCHEMA,
        max_tokens=3000,
    )

    e = html.escape
    lines = [f"<b>📰 {e(plan.get('topic', 'News'))}</b>", f"<i>{e(out.get('takeaway', ''))}</i>"]
    for n, it in enumerate(out.get("items", [])[:max_items], 1):
        lines.append(f"{n}. <a href=\"{e(it['link'], quote=True)}\">{e(it['title'])}</a>\n   {e(it['why'])}")
    lines.append(f"<i>{len(items)} articles scanned</i>")
    reply("\n\n".join(lines))

    if plan.get("voice") and out.get("voice_script"):
        brief.send_voice(out["voice_script"], f"🎧 {plan.get('topic', 'News')}", VOICES.get(language))


# ---------------------------------------------------------------- routing

def handle(message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if not text:
        return
    if chat_id != OWNER:
        requests.post(f"{API}/sendMessage", data={"chat_id": chat_id, "text": "This is a private bot."}, timeout=10)
        print(f"[ignored] message from {chat_id}")
        return

    command = text.split()[0].split("@")[0].lower()
    if command in {"/start", "/help"}:
        reply(HELP)
        return
    if command == "/brief":
        reply("☀️ Building your brief, about a minute...")
        brief.run_daily(dry=False)
        return

    typing()
    today = datetime.now().strftime("%A, %B %d, %Y")
    plan = brief.ask_claude_structured(
        PLANNER_SYSTEM.format(today=today), text, "news_plan", PLAN_SCHEMA, max_tokens=600
    )
    action = plan.get("action")
    if action == "brief":
        reply("☀️ Building your brief, about a minute...")
        brief.run_daily(dry=False)
    elif action == "news":
        typing()
        answer_news(text, plan)
    else:
        reply(html.escape(plan.get("text", "I did not understand. Send /help for examples.")))


def main() -> None:
    requests.post(f"{API}/deleteWebhook", timeout=10)  # polling and webhooks cannot run together
    requests.post(f"{API}/setMyCommands", timeout=10, data={"commands": json.dumps([
        {"command": "brief", "description": "Run the full morning brief now"},
        {"command": "help", "description": "Examples of what to ask"},
    ])})
    print("Bot running. Waiting for messages...")
    offset = None
    while True:
        try:
            resp = requests.get(f"{API}/getUpdates", timeout=60, params={
                "timeout": 50, "offset": offset, "allowed_updates": json.dumps(["message"]),
            })
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle(update.get("message") or {})
                except Exception as exc:
                    traceback.print_exc()
                    reply(f"⚠️ Something failed: {html.escape(str(exc))[:300]}")
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--once":
        # Free mode: the Cloudflare relay starts a GitHub Action with one message (already owner-checked)
        try:
            handle({"chat": {"id": OWNER}, "text": sys.argv[2]})
        except Exception as exc:
            traceback.print_exc()
            reply(f"⚠️ Something failed: {html.escape(str(exc))[:300]}")
            sys.exit(1)
    else:
        main()  # always-on mode (Heroku or any machine)
