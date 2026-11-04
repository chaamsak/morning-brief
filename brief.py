"""Morning Brief: AI + energy news to Telegram, with a voice version and urgent alerts.

Usage:
    python brief.py daily            # written brief + voice note
    python brief.py alerts           # check for urgent news, push only if something qualifies
    python brief.py daily --dry-run  # print instead of sending (no Telegram needed)

Environment variables (set as GitHub Actions secrets):
    ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    CLAUDE_MODEL (optional, defaults to claude-haiku-5-5)
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
UA = {"User-Agent": "morning-brief/1.0 (personal news digest)"}
TG_LIMIT = 4000  # Telegram caps messages at 4096 chars


# ---------------------------------------------------------------- collection

def item_id(link: str, title: str) -> str:
    return hashlib.sha1((link or title).encode()).hexdigest()[:16]


def fetch_feed(url: str, section: str, since: datetime) -> list[dict]:
    try:
        resp = requests.get(url, headers=UA, timeout=20)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # one bad feed must never kill the run
        print(f"[skip] {url}: {exc}", file=sys.stderr)
        return []

    items = []
    for e in parsed.entries:
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        if not stamp:
            continue
        published = datetime.fromtimestamp(time.mktime(stamp), tz=timezone.utc)
        if published < since:
            continue
        summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))
        items.append({
            "id": item_id(e.get("link", ""), e.get("title", "")),
            "section": section,
            "source": parsed.feed.get("title", url),
            "title": e.get("title", "").strip(),
            "link": e.get("link", ""),
            "summary": " ".join(summary.split())[:400],
            "published": published.isoformat(),
        })
    return items


def fetch_cisa_kev(since: datetime) -> list[dict]:
    """Vulnerabilities CISA confirms are being exploited right now."""
    try:
        data = requests.get(CONFIG["cisa_kev_url"], headers=UA, timeout=30).json()
    except Exception as exc:
        print(f"[skip] CISA KEV: {exc}", file=sys.stderr)
        return []
    items = []
    for v in data.get("vulnerabilities", []):
        added = datetime.fromisoformat(v["dateAdded"]).replace(tzinfo=timezone.utc)
        if added < since - timedelta(days=1):  # KEV dates have no time component
            continue
        items.append({
            "id": item_id(v["cveID"], v["cveID"]),
            "section": "security",
            "source": "CISA Known Exploited Vulnerabilities",
            "title": f"{v['cveID']}: {v['vendorProject']} {v['product']} actively exploited",
            "link": f"https://nvd.nist.gov/vuln/detail/{v['cveID']}",
            "summary": v.get("shortDescription", "")[:400],
            "published": added.isoformat(),
        })
    return items


def collect(sections: list[str], hours: int, include_kev: bool = False) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[dict] = []
    for section in sections:
        for url in CONFIG["feeds"].get(section, []):
            items.extend(fetch_feed(url, section, since))
    if include_kev:
        items.extend(fetch_cisa_kev(since))
    unique = {i["id"]: i for i in items}  # dedupe across feeds
    return sorted(unique.values(), key=lambda i: i["published"], reverse=True)


# ---------------------------------------------------------------- state

def load_seen() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"alerted": {}, "scored": {}}


def save_seen(seen: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    for key in ("alerted", "scored"):  # keep the file small
        seen[key] = {k: v for k, v in seen[key].items() if v.get("at", "") > cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(seen, indent=1))


# ---------------------------------------------------------------- LLM

def ask_claude(system: str, prompt: str, max_tokens: int = 4000) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json()["content"])


def parse_json(text: str):
    decoder = json.JSONDecoder()
    last_error = None
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
            return value
        except json.JSONDecodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise ValueError(f"No JSON in model output: {text[:200]}")


def ask_claude_json(system: str, prompt: str, max_tokens: int = 4000):
    raw = ask_claude(system, prompt, max_tokens)
    try:
        return parse_json(raw)
    except (ValueError, json.JSONDecodeError) as first_error:
        repair_system = (
            "You repair malformed JSON. Return only the corrected JSON, with the same "
            "meaning and structure as the candidate. Escape quotes and control characters "
            "inside strings. Do not use markdown fences or add commentary."
        )
        repair_prompt = (
            f"The candidate JSON is invalid: {first_error}\n\n"
            "Correct this candidate and return one complete valid JSON value:\n"
            f"{raw}"
        )
        return parse_json(ask_claude(repair_system, repair_prompt, max(max_tokens, 5000)))


def ask_claude_structured(
    system: str, prompt: str, tool_name: str, schema: dict, max_tokens: int = 4000
) -> dict:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "tools": [{"name": tool_name, "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": tool_name},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    resp.raise_for_status()
    for block in resp.json().get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block["input"]
    raise ValueError(f"Claude did not return structured data for {tool_name}")


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "sections": {"type": "array", "items": {"type": "object"}},
        "voice_script": {"type": "string"},
    },
    "required": ["headline", "sections", "voice_script"],
}

ALERT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "score": {"type": "integer"},
            "alert": {"type": "string"},
        },
        "required": ["id", "score", "alert"],
    },
}


BRIEF_SYSTEM = """You are a sharp news editor writing a personal morning brief for a data scientist
who works at the intersection of AI and energy (data center power, grid, batteries, ERCOT, AI hardware).
Rules: never use em dashes. Be concrete: numbers, names, why it matters. Skip fluff, funding rounds under
$50M, and listicles. Merge duplicates. If a section has nothing worth reading, say so in one line."""

BRIEF_PROMPT = """Today is {today}. Write the brief in {language}.

Return ONLY a JSON object with these keys:
- "headline": one sentence, the single most important thing today
- "sections": list of {{"name": str, "items": [{{"title": str, "why": str, "link": str}}]}}
  using sections in this order: "AI x Energy" (items touching both), "AI", "Energy",
  "Africa" (Côte d'Ivoire first, then West Africa, then the rest of the continent), "Security".
  Translate French titles into the brief's language.
  At most {max_items} items per section. "why" is one sentence on why it matters.
- "voice_script": a spoken version, 350 to 500 words, natural radio style, no URLs, no lists,
  no symbols, numbers written so they read well aloud. Start with a greeting and today's date.

Articles:
{articles}"""


ALERT_SYSTEM = """You triage news for urgency. Urgent (score 8 to 10) means a reader should know within the
hour: a major exchange or DeFi hack, a critical vulnerability being exploited in widely used software
(PHP, OpenSSL, Linux kernel, major browsers, cloud providers), a large AI provider outage, or a grid
emergency (ERCOT EEA, large blackout). Routine CVEs, minor breaches, opinion pieces and product news score
low. Be strict: a false alarm costs more than a late one. Never use em dashes."""

ALERT_PROMPT = """Score each item. Already alerted recently (do not re-alert the same story):
{recent}

Return ONLY a JSON list: [{{"id": str, "score": int, "alert": str}}]
"alert" is two short sentences in {language}: what happened, and what to do or watch (empty if score < 8).

Items:
{items}"""


# ---------------------------------------------------------------- Telegram

def tg(method: str, data: dict, files: dict | None = None, dry: bool = False) -> None:
    if dry:
        print(f"\n===== telegram.{method} =====")
        print(data.get("text") or data.get("caption") or data)
        if files:
            print(f"[file attached: {list(files)}]")
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], **data}
    resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Telegram {method} failed: {resp.text}")


def send_text(text: str, dry: bool = False, silent: bool = False) -> None:
    chunks, current = [], ""
    for block in text.split("\n\n"):  # split on paragraph boundaries so HTML tags stay intact
        if len(current) + len(block) + 2 > TG_LIMIT and current:
            chunks.append(current)
            current = ""
        current += block + "\n\n"
    if current.strip():
        chunks.append(current)
    for chunk in chunks:
        tg("sendMessage", {
            "text": chunk.strip(),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "disable_notification": "true" if silent else "false",
        }, dry=dry)


def render_brief(brief: dict, today: str) -> str:
    e = html.escape
    parts = [f"<b>☀️ Morning Brief · {e(today)}</b>", f"<i>{e(brief['headline'])}</i>"]
    for section in brief["sections"]:
        if not section.get("items"):
            continue
        lines = [f"<b>{e(section['name'])}</b>"]
        for it in section["items"]:
            lines.append(f"• <a href=\"{e(it['link'], quote=True)}\">{e(it['title'])}</a>\n  {e(it['why'])}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- voice

async def _tts(text: str, voice: str, out: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice).save(str(out))


def send_voice(script: str, caption: str, voice: str | None = None) -> None:
    """Speak the script and send it. Voice note if ffmpeg exists, otherwise a regular audio file."""
    tmp = Path(tempfile.mkdtemp())
    mp3, ogg = tmp / "brief.mp3", tmp / "brief.ogg"
    asyncio.run(_tts(script, voice or CONFIG["brief"]["voice"], mp3))
    if shutil.which("ffmpeg"):
        # Telegram only shows a playable voice note for OGG/Opus
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-c:a", "libopus", "-b:a", "48k", str(ogg)],
            check=True,
        )
        with ogg.open("rb") as f:
            tg("sendVoice", {"caption": caption}, files={"voice": f})
    else:
        with mp3.open("rb") as f:
            tg("sendAudio", {"caption": caption, "title": caption}, files={"audio": f})


# ---------------------------------------------------------------- modes

def run_daily(dry: bool) -> None:
    cfg = CONFIG["brief"]
    items = collect(["ai", "energy", "africa", "security"], cfg["lookback_hours"], include_kev=True)
    print(f"Collected {len(items)} items", file=sys.stderr)
    # cap each section so a busy one (arXiv, security) cannot crowd out the others
    per_section: dict[str, int] = {}
    balanced = []
    for i in items:
        per_section[i["section"]] = per_section.get(i["section"], 0) + 1
        if per_section[i["section"]] <= 35:
            balanced.append(i)
    items = balanced
    today = datetime.now().strftime("%A, %B %d, %Y")
    if not items:
        send_text(f"<b>☀️ Morning Brief · {today}</b>\n\nNo new items from your feeds today.", dry)
        return

    articles = "\n".join(
        f"[{i['section']}] {i['title']} ({i['source']}) {i['link']}\n  {i['summary']}" for i in items
    )
    try:
        brief = ask_claude_structured(BRIEF_SYSTEM, BRIEF_PROMPT.format(
            today=today, language=cfg["language"], max_items=cfg["max_items_per_section"], articles=articles,
        ), "morning_brief", BRIEF_SCHEMA)
    except (ValueError, requests.RequestException) as exc:
        print(f"[warn] brief generation fallback: {exc}", file=sys.stderr)
        brief = None
    if not isinstance(brief, dict) or "headline" not in brief or "sections" not in brief:
        lines = [
            f"<b>☀️ Morning Brief · {html.escape(today)}</b>",
            "<i>Automated summary unavailable right now. Here are the latest items from your feeds.</i>",
        ]
        for i in items[: min(len(items), 12)]:
            lines.append(
                f"• <a href=\"{html.escape(i['link'], quote=True)}\">{html.escape(i['title'])}</a>"
                f" ({html.escape(i['source'])})"
            )
        send_text("\n\n".join(lines), dry)
        return

    send_text(render_brief(brief, today), dry)
    if dry:
        print("\n===== voice script =====\n" + brief["voice_script"])
        return
    send_voice(brief["voice_script"], f"🎧 Audio brief · {today}")


def run_alerts(dry: bool) -> None:
    cfg = CONFIG["alerts"]
    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat()
    keywords = [k.lower() for k in CONFIG["urgent_keywords"]]

    items = collect(["security", "ai", "energy", "africa"], cfg["lookback_hours"], include_kev=True)
    candidates = [
        i for i in items
        if i["id"] not in seen["scored"]
        and any(k in f"{i['title']} {i['summary']}".lower() for k in keywords)
    ]
    print(f"{len(items)} items, {len(candidates)} new keyword matches", file=sys.stderr)
    if not candidates:
        save_seen(seen)
        return

    recent = "\n".join(f"- {v['title']}" for v in seen["alerted"].values()) or "(none)"
    listing = "\n".join(f"id={i['id']} | {i['title']} ({i['source']})\n  {i['summary']}" for i in candidates[:40])
    try:
        scores = ask_claude_structured(ALERT_SYSTEM, ALERT_PROMPT.format(
            recent=recent, language=CONFIG["brief"]["language"], items=listing,
        ), "alert_scores", ALERT_SCHEMA, max_tokens=2000)
        if not isinstance(scores, list):
            raise ValueError("Alert JSON is not a list")
    except (ValueError, json.JSONDecodeError, requests.RequestException) as exc:
        print(f"[warn] alert scoring skipped: {exc}", file=sys.stderr)
        save_seen(seen)
        return

    by_id = {i["id"]: i for i in candidates}
    for s in scores:
        item = by_id.get(s.get("id"))
        if not item:
            continue
        seen["scored"][item["id"]] = {"at": now}
        if int(s.get("score", 0)) >= cfg["threshold"]:
            text = (
                f"🚨 <b>{html.escape(item['title'])}</b>\n\n{html.escape(s.get('alert', ''))}\n\n"
                f"<a href=\"{html.escape(item['link'], quote=True)}\">{html.escape(item['source'])}</a>"
                f" · urgency {s['score']}/10"
            )
            send_text(text, dry)
            seen["alerted"][item["id"]] = {"at": now, "title": item["title"]}
    save_seen(seen)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"daily", "alerts"}:
        sys.exit(__doc__)
    dry_run = "--dry-run" in sys.argv
    {"daily": run_daily, "alerts": run_alerts}[sys.argv[1]](dry_run)
