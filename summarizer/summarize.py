#!/usr/bin/env python3
"""Summarize WhatsApp group messages collected by wa-bridge.

Reads the bridge's SQLite database (read-only), summarizes selected groups
with either a trivial statistics "model" (dummy) or a local Ollama model,
and prints the digest or sends it to a group through the bridge.
"""

import argparse
import html as htmllib
import json
import os
import re
import smtplib
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import NamedTuple

# The bridge appends text read from a photo (OCR) after this tag; see bridge/ocr.go.
OCR_TAG = "[text in image]"
OCR_NOTE = (
    f"Text after '{OCR_TAG}' was read from a photo by OCR: treat it as part of that message "
    "(e.g. a timetable or a notice someone photographed) and ignore garbled fragments."
)
# The bridge appends the transcript of a voice message after this tag; see bridge/voice.go.
VOICE_TAG = "[voice transcript]"
VOICE_NOTE = (
    f"Text after '{VOICE_TAG}' is an automatic transcript of a voice message: treat it as what "
    "the sender said and ignore misheard fragments."
)

SYSTEM_PROMPT = (
    "You summarize WhatsApp group chats. Write a short summary in {language}: "
    "the main topics, decisions, dates and open questions, as 3-6 bullet points. "
    "Do not invent anything that is not in the chat. " + OCR_NOTE + " " + VOICE_NOTE
)


# Media without a caption, e.g. "[image]" - noise for a language model.
# Images with text recognized by the bridge are not bare: "[image]\n[text in image]\n...",
# nor are transcribed voice messages: "[voice note]\n[voice transcript]\n...".
BARE_PLACEHOLDER = re.compile(r"^\[[a-z ]+\]$")


THREADS_PROMPT = (
    "You summarize WhatsApp group chats. Messages about different topics are often interleaved; "
    "split the chat into separate threads, one per topic (e.g. homework, a lost item, a trip), "
    "and put every message on that topic into its thread. For each thread write a short title "
    "(max 6 words) and 1-4 concise points with decisions, dates, amounts and open questions. "
    "Messages about the same thing (e.g. a link and the text it points to) form one thread - "
    "never split one topic into several threads. "
    "Write unanswered questions as questions, never as decisions or facts, "
    "always prefixed exactly like this: '{question}: ...'. "
    "Skip greetings and thanks-only chatter. Do not invent anything that is not in the chat. "
    + OCR_NOTE + " " + VOICE_NOTE + " "
    "Write titles and points in {language}. Respond with JSON only: "
    '{{"threads": [{{"title": "...", "points": ["...", "..."]}}]}}'
)

NOTHING_NOTABLE = "No notable topics."

# Label for unanswered questions; models copy an English example word, so give it in the target language.
QUESTION_LABEL = {"polish": "Pytanie", "english": "Question", "german": "Frage"}


class Thread(NamedTuple):
    title: str
    points: list


class Section(NamedTuple):
    name: str
    summary: object  # str, [Thread] or None
    messages: int = 0
    omitted: int = 0  # messages left out of a too-long transcript
    ocr_images: int = 0  # photos whose recognized text the model was given
    voice_notes: int = 0  # voice messages whose transcript the model was given


# Email/digest labels by SUMMARY_LANGUAGE; plural forms are (one, few, many).
LABELS = {
    "english": {
        "digest": "Digest", "html_title": "WhatsApp digest", "last": "last {hours}h",
        "message": ("message", "messages", "messages"), "person": ("person", "people", "people"),
        "group": ("group", "groups", "groups"),
        "no_messages": "No messages.", "nothing_notable": "No notable topics.",
        "stats": "{messages} from {people} between {first} and {last}. Most active: {ranking}.",
        "model_unavailable": "model unavailable, showing statistics: {error}",
        "truncated": "⚠️ Long chat: {count} (the oldest) left out of the summary.",
        "ocr": "📷 Includes text read from {count} (OCR, may contain recognition errors).",
        "photo": ("photo", "photos", "photos"),
        "voice": "🎤 Includes {count} (automatic transcript, may contain errors).",
        "voice_note": ("voice message", "voice messages", "voice messages"),
        "footer": "Generated locally by szalona-beata from your WhatsApp groups.",
        "bridge": {
            "logged_out": "⚠️ The WhatsApp bridge is logged out: new messages are not collected. "
                          "Link the device again (README, step 3).",
            "disconnected": "⚠️ The WhatsApp bridge is not connected to WhatsApp: this digest may be incomplete.",
            "unreachable": "⚠️ The WhatsApp bridge is not responding (check the bridge container): "
                           "this digest may be incomplete.",
        },
    },
    "polish": {
        "digest": "Podsumowanie", "html_title": "Podsumowanie WhatsApp", "last": "ostatnie {hours} h",
        "message": ("wiadomość", "wiadomości", "wiadomości"), "person": ("osoba", "osoby", "osób"),
        "group": ("grupa", "grupy", "grup"),
        "no_messages": "Brak wiadomości.", "nothing_notable": "Nic istotnego.",
        "stats": "{messages} ({people}), {first}–{last}. Najaktywniejsi: {ranking}.",
        "model_unavailable": "model niedostępny, pokazuję statystyki: {error}",
        "truncated": "⚠️ Długa rozmowa: w podsumowaniu pominięto najstarsze – {count}.",
        "ocr": "📷 Uwzględniono tekst odczytany ze zdjęć (OCR, możliwe błędy odczytu): {count}.",
        "photo": ("zdjęcie", "zdjęcia", "zdjęć"),
        "voice": "🎤 Uwzględniono treść wiadomości głosowych (automatyczna transkrypcja, możliwe błędy): {count}.",
        "voice_note": ("wiadomość głosowa", "wiadomości głosowe", "wiadomości głosowych"),
        "footer": "Wygenerowane lokalnie przez szalona-beata z Twoich grup WhatsApp.",
        "bridge": {
            "logged_out": "⚠️ Mostek WhatsApp jest wylogowany: nowe wiadomości nie są zbierane. "
                          "Połącz urządzenie ponownie (README, krok 3).",
            "disconnected": "⚠️ Mostek WhatsApp nie ma połączenia z WhatsAppem: podsumowanie może być niepełne.",
            "unreachable": "⚠️ Mostek WhatsApp nie odpowiada (sprawdź kontener bridge): "
                           "podsumowanie może być niepełne.",
        },
    },
}


def labels(lang):
    return LABELS.get(lang.lower(), LABELS["english"])


def plural(n, forms, lang):
    """'1 wiadomość', '3 wiadomości', '5 osób' - Polish has three plural forms."""
    one, few, many = forms
    if n == 1:
        return f"{n} {one}"
    if lang.lower() == "polish":
        return f"{n} {few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many}"
    return f"{n} {many}"


def _total_line(sections, lang):
    """'122 messages' across all groups, or None without messages."""
    total = sum(s.messages for s in sections)
    return plural(total, labels(lang)["message"], lang) if total else None


class Msg(NamedTuple):
    ts: int
    sender: str
    text: str


def is_synthetic(db):
    """True for databases made by sample_data.py (made-up chats, safe for external APIs)."""
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'synthetic'").fetchone() is not None


def resolve_group(db, query):
    """Find a group by JID, exact name, or unique name substring (case-insensitive)."""
    chats = db.execute("SELECT jid, COALESCE(name, '') FROM chats").fetchall()
    for jid, name in chats:
        if query == jid:
            return jid, name
    q = query.lower()
    exact = [(jid, name) for jid, name in chats if name.lower() == q]
    if exact:
        return exact[0]
    partial = [(jid, name) for jid, name in chats if q in name.lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = ", ".join(sorted(name for _, name in partial))
        raise ValueError(f"'{query}' matches several groups: {names}")
    raise ValueError(f"no group matches '{query}' (try --list-groups)")


def fetch_messages(db, jid, since_ts):
    rows = db.execute(
        "SELECT ts, COALESCE(NULLIF(sender_name, ''), sender_jid), text FROM messages "
        "WHERE chat_jid = ? AND ts >= ? ORDER BY ts",
        (jid, since_ts),
    )
    return [Msg(ts, sender.split("@")[0], text) for ts, sender, text in rows]


def _hhmm(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def format_transcript(msgs, max_chars, tz=None):
    """The transcript text only (see build_transcript)."""
    return build_transcript(msgs, max_chars, tz)[0]


def build_transcript(msgs, max_chars, tz=None):
    """Render messages as chat lines, keeping the most recent ones within max_chars.
    Returns (text, number of messages left out). Captionless media ("[image]") is
    never sent, so it doesn't count as left out."""
    lines = [f"[{_hhmm(m.ts, tz)}] {m.sender}: {m.text}" for m in msgs if not BARE_PLACEHOLDER.match(m.text)]
    kept, size = [], 0
    for line in reversed(lines):
        size += len(line) + (1 if kept else 0)
        if size > max_chars:
            break
        kept.append(line)
    if lines and not kept:  # the newest message alone is too long: cut it rather than send nothing
        kept.append(lines[-1][: max_chars - 1] + "…")
    kept.reverse()
    omitted = len(lines) - len(kept)
    if omitted:
        kept.insert(0, f"[... {omitted} earlier messages omitted]")
    return "\n".join(kept), omitted


def dummy_summary(msgs, tz=None, lang="English"):
    """Statistics-only summary, used to test the pipeline without any model."""
    L = labels(lang)
    senders = Counter(m.sender for m in msgs)
    ranking = ", ".join(f"{name} ({n})" for name, n in senders.most_common(5))
    first, last = (datetime.fromtimestamp(m.ts, tz) for m in (msgs[0], msgs[-1]))
    fmt = "%H:%M" if first.date() == last.date() else "%d.%m %H:%M"
    return L["stats"].format(
        messages=plural(len(msgs), L["message"], lang), people=plural(len(senders), L["person"], lang),
        first=first.strftime(fmt), last=last.strftime(fmt), ranking=ranking,
    )


def _summary_text(summary, lang="English"):
    if isinstance(summary, list):
        return "\n".join(f"▸ {t.title}\n" + "\n".join(f"- {p}" for p in t.points) for t in summary)
    if summary == NOTHING_NOTABLE:
        return labels(lang)["nothing_notable"]
    return summary or labels(lang)["no_messages"]


def _truncated_note(section, lang):
    L = labels(lang)
    return L["truncated"].format(count=plural(section.omitted, L["message"], lang))


def _ocr_note(section, lang):
    L = labels(lang)
    return L["ocr"].format(count=plural(section.ocr_images, L["photo"], lang))


def _voice_note(section, lang):
    L = labels(lang)
    return L["voice"].format(count=plural(section.voice_notes, L["voice_note"], lang))


def _section_notes(section, lang):
    """Notes under a group's name: truncated chat, text read from photos, transcribed voice messages."""
    return ([_truncated_note(section, lang)] if section.omitted else []) + \
        ([_ocr_note(section, lang)] if section.ocr_images else []) + \
        ([_voice_note(section, lang)] if section.voice_notes else [])


def tagged_sent(msgs, omitted, tag):
    """Messages with text added by the bridge (tag: OCR_TAG, VOICE_TAG) among those build_transcript kept."""
    sent = [m for m in msgs if not BARE_PLACEHOLDER.match(m.text)][omitted:]
    return sum(tag in m.text for m in sent)


def build_digest(sections, hours, title=None, lang="English", warning=None):
    L = labels(lang)
    sections = [Section(*s) for s in sections]
    header = f"📋 {title or L['digest']} ({L['last'].format(hours=hours)})"
    if total := _total_line(sections, lang):
        header += f"\n{total}"
    parts = [header] + ([warning] if warning else [])
    for s in sections:
        count = f" ({plural(s.messages, L['message'], lang)})" if s.messages else ""
        note = "".join(f"{n}\n" for n in _section_notes(s, lang))
        parts.append(f"*{s.name}*{count}\n{note}{_summary_text(s.summary, lang)}")
    return "\n\n".join(parts)


BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
BOLD = re.compile(r"\*\*(.+?)\*\*")

# Email clients strip <style> blocks, so styles are inline.
P_STYLE = "margin:0 0 8px"
UL_STYLE = "margin:0 0 8px;padding-left:20px"
LI_STYLE = "margin:0 0 4px"

HTML_PAGE = """<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title} {day}</title></head>
<body style="margin:0;padding:0;background:#f2f4f3">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f3">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2a27">
<tr><td style="background:#0b5f53;border-radius:12px 12px 0 0;padding:22px 24px">
{kicker}<div style="font-size:22px;font-weight:700;color:#ffffff;margin-top:4px">{title}</div>
<div style="font-size:14px;color:#d6eee8;margin-top:4px">{day} &middot; {last} &middot; {count}</div>
{total}
</td></tr>
<tr><td style="background:#ffffff;border-radius:0 0 12px 12px;padding:8px 24px 16px">
{warning}{cards}
</td></tr>
<tr><td style="padding:16px 24px;font-size:12px;color:#7a8783;text-align:center">
{footer}
</td></tr>
</table>
</td></tr>
</table>
</body></html>
"""

CARD = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px">
<tr><td style="border-left:4px solid #25a18e;padding:4px 0 4px 14px">
<div style="font-size:17px;font-weight:700;color:#0b5f53;margin:0 0 8px">{name}</div>
{meta}
<div style="font-size:15px;line-height:1.5">{summary}</div>
</td></tr></table>"""


def _inline_html(text):
    return BOLD.sub(r"<strong>\1</strong>", htmllib.escape(text))


THREAD_BLOCK = (
    '<div data-thread="{n}" style="background:#f3f8f6;border-radius:8px;padding:10px 12px;margin:0 0 10px">'
    '<div style="font-weight:700;color:#1f2a27;margin:0 0 6px">{title}</div>'
    '<ul style="' + UL_STYLE + ';margin-bottom:0">{items}</ul></div>'
)


def render_summary_html(summary, lang="English"):
    """Render a summary as safe HTML: a list of Threads becomes one block per thread;
    plain text (optional "- " bullets and **bold**) becomes paragraphs and lists."""
    if isinstance(summary, list):
        return "".join(
            THREAD_BLOCK.format(
                n=n,
                title=_inline_html(t.title),
                items="".join(f'<li style="{LI_STYLE}">{_inline_html(p)}</li>' for p in t.points),
            )
            for n, t in enumerate(summary, start=1)
        )
    if not summary or summary == NOTHING_NOTABLE:
        text = labels(lang)["nothing_notable" if summary else "no_messages"]
        return f'<p style="margin:0 0 8px;color:#7a8783;font-style:italic">{text}</p>'
    parts, items = [], []

    def flush():
        if items:
            lis = "".join(f'<li style="{LI_STYLE}">{i}</li>' for i in items)
            parts.append(f'<ul style="{UL_STYLE}">{lis}</ul>')
            items.clear()

    for line in summary.splitlines():
        if m := BULLET.match(line):
            items.append(_inline_html(m.group(1)))
            continue
        flush()
        if line.strip():
            parts.append(f'<p style="{P_STYLE}">{_inline_html(line.strip())}</p>')
    flush()
    return "".join(parts)


def _card_meta_html(section, lang):
    """Message count and the notes (left-out messages, text from photos, voice messages) under a group's name."""
    html = ""
    if section.messages:
        count = plural(section.messages, labels(lang)["message"], lang)
        html += f'<div style="font-size:13px;color:#7a8783;margin:-4px 0 8px">{count}</div>'
    if section.omitted:
        note = htmllib.escape(_truncated_note(section, lang))
        html += f'<div style="font-size:13px;color:#8a5a00;margin:0 0 8px">{note}</div>'
    if section.ocr_images:
        note = htmllib.escape(_ocr_note(section, lang))
        html += f'<div style="font-size:13px;color:#7a8783;margin:0 0 8px">{note}</div>'
    if section.voice_notes:
        note = htmllib.escape(_voice_note(section, lang))
        html += f'<div style="font-size:13px;color:#7a8783;margin:0 0 8px">{note}</div>'
    return html


KICKER = ('<div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:#a8dcd1">'
          'Szalona Beata</div>\n')
WARNING_BANNER = ('<div data-warning="1" style="background:#fdecea;color:#8a1c12;border-radius:8px;'
                  'padding:10px 12px;margin:16px 0 0;font-weight:600">{}</div>\n')


def build_html_digest(sections, hours, day, title=None, lang="English", warning=None):
    L = labels(lang)
    sections = [Section(*s) for s in sections]
    cards = "\n".join(
        CARD.format(
            name=htmllib.escape(s.name),
            meta=_card_meta_html(s, lang),
            summary=render_summary_html(s.summary, lang),
        )
        for s in sections
    )
    total = _total_line(sections, lang)
    return HTML_PAGE.format(
        title=htmllib.escape(title or L["html_title"]),
        kicker="" if title else KICKER,  # a custom title already names the sender
        day=htmllib.escape(day),
        last=L["last"].format(hours=hours),
        count=plural(len(sections), L["group"], lang),
        cards=cards,
        warning=WARNING_BANNER.format(htmllib.escape(warning)) if warning else "",
        footer=L["footer"],
        total=(f'<div style="display:inline-block;margin-top:12px;padding:6px 10px;border-radius:6px;'
               f'background:#0e7466;color:#ffffff;font-size:14px">💬 {htmllib.escape(total)}</div>'
               if total else ""),
    )


# Longer server-requested waits (e.g. an exhausted daily quota) are not worth blocking for.
MAX_RETRY_WAIT = 120


def _request(method, url, payload=None, timeout=600, headers=None, retries=0):
    """JSON request; HTTP 429 is retried up to `retries` times after the server's Retry-After delay."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        # Some API fronts (e.g. Groq's Cloudflare) reject urllib's default User-Agent.
        headers={"Content-Type": "application/json", "User-Agent": "szalona-beata/0.1", **(headers or {})},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                try:
                    wait = float(e.headers.get("Retry-After", 10))
                except ValueError:
                    wait = 10
                if wait <= MAX_RETRY_WAIT:
                    print(f"Rate limited, retrying in {wait:g}s...", file=sys.stderr, flush=True)
                    time.sleep(wait)
                    continue
            _raise_http_error(method, url, e)


def _raise_http_error(method, url, e):
    try:
        detail = json.load(e).get("error", e.reason)
    except ValueError:
        detail = e.reason
    if isinstance(detail, dict):  # OpenAI-style {"error": {"message": ...}}
        detail = detail.get("message", detail)
    raise RuntimeError(f"{method} {url} failed ({e.code}): {detail}") from None


class Ollama:
    def __init__(self, base_url, model):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def ensure_model(self):
        tags = _request("GET", f"{self.base_url}/api/tags")
        if not any(m.get("name") == self.model for m in tags.get("models", [])):
            print(f"Pulling model {self.model} (first run only)...", file=sys.stderr)
            # no data arrives until the download finishes: allow for a ~2 GB model on a slow link
            _request("POST", f"{self.base_url}/api/pull", {"model": self.model, "stream": False}, timeout=3600)

    def summarize(self, transcript, language):
        reply = _request("POST", f"{self.base_url}/api/chat", {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(language=language)},
                {"role": "user", "content": transcript},
            ],
        })
        return reply["message"]["content"].strip()


def parse_threads(content):
    """Turn a JSON threads reply into [Thread]; anything unexpected is returned as plain text."""
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
        threads = [
            Thread(t["title"].strip(), [p.strip() for p in t["points"] if isinstance(p, str) and p.strip()])
            for t in data["threads"]
        ]
    except (ValueError, KeyError, TypeError, AttributeError):
        return text
    threads = [t for t in threads if t.title]
    return threads or NOTHING_NOTABLE


class OpenAICompat:
    """Any OpenAI-compatible chat completions API (Groq, Mistral, OpenRouter, Ollama's /v1...)."""

    def __init__(self, base_url, api_key, model, reasoning_effort=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def summarize(self, transcript, language):
        system_prompt = THREADS_PROMPT.format(
            language=language, question=QUESTION_LABEL.get(language.lower(), "Question"))
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript},
            ],
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        # What was sent and received, for the call log (prompt tuning).
        self.last_exchange = ex = {
            "provider": "openai", "model": self.model, "reasoning_effort": self.reasoning_effort,
            "language": language, "system_prompt": system_prompt, "transcript": transcript,
            "response": None, "parsed": False, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "duration_ms": 0, "error": None,
        }
        started = time.monotonic()
        try:
            reply = _request(
                "POST",
                f"{self.base_url}/chat/completions",
                payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                retries=3,
            )
        except (RuntimeError, OSError) as e:
            ex["error"] = str(e)
            raise
        finally:
            ex["duration_ms"] = int((time.monotonic() - started) * 1000)
        usage = reply.get("usage") or {}
        for key in self.usage:
            self.usage[key] += usage.get(key, 0)
            ex[key] = usage.get(key, 0)
        ex["response"] = reply["choices"][0]["message"]["content"]
        result = parse_threads(ex["response"])
        ex["parsed"] = isinstance(result, list) or result == NOTHING_NOTABLE
        return result


CALL_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY,
    ts                INTEGER NOT NULL,   -- digest time (unix)
    group_jid         TEXT,
    group_name        TEXT,
    synthetic         INTEGER NOT NULL,   -- 1 = made-up sample data
    provider          TEXT,
    model             TEXT,
    reasoning_effort  TEXT,
    language          TEXT,
    system_prompt     TEXT,
    transcript        TEXT,
    response          TEXT,               -- raw model output; NULL if the call failed
    parsed            INTEGER,            -- 1 = valid threads JSON
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    duration_ms       INTEGER,
    error             TEXT
);
"""


class CallLog:
    """Stores every LLM prompt and response in SQLite, to tune the prompt on real cases later."""

    FIELDS = ("provider", "model", "reasoning_effort", "language", "system_prompt", "transcript", "response",
              "parsed", "prompt_tokens", "completion_tokens", "total_tokens", "duration_ms", "error")

    def __init__(self, path, retention_days=0, now=None):
        self.db = sqlite3.connect(path)
        self.db.executescript(CALL_LOG_SCHEMA)
        self.retention_days = retention_days
        self.purge(time.time() if now is None else now)

    def purge(self, now):
        """Delete entries older than the retention period (0 = keep forever)."""
        if self.retention_days > 0:
            self.db.execute("DELETE FROM llm_calls WHERE ts < ?", (int(now - self.retention_days * 86400),))
            self.db.commit()

    def record(self, exchange, group_jid, group_name, ts, synthetic):
        self.purge(ts)  # the scheduled digest keeps one log open for months
        cols = ("ts", "group_jid", "group_name", "synthetic") + self.FIELDS
        values = (int(ts), group_jid, group_name, int(synthetic)) + tuple(exchange.get(f) for f in self.FIELDS)
        self.db.execute(f"INSERT INTO llm_calls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)
        self.db.commit()


def check_bridge(bridge_url):
    """None if the bridge is linked and connected, else 'logged_out', 'disconnected' or 'unreachable'."""
    try:
        health = _request("GET", f"{bridge_url.rstrip('/')}/health", timeout=10)
    except (RuntimeError, OSError, ValueError):
        return "unreachable"
    if not health.get("logged_in"):
        return "logged_out"
    if not health.get("connected"):
        return "disconnected"
    return None


def send_to_bridge(bridge_url, jid, text):
    _request("POST", f"{bridge_url.rstrip('/')}/send", {"jid": jid, "text": text}, timeout=60)


class SMTPConfig(NamedTuple):
    host: str
    port: int
    user: str
    password: str
    security: str  # "ssl", "starttls" or "none"


def send_email(smtp, sender, to, subject, body, html=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    if smtp.security == "ssl":
        conn = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=60)
    else:
        conn = smtplib.SMTP(smtp.host, smtp.port, timeout=60)
    with conn:
        if smtp.security == "starttls":
            conn.starttls()
        if smtp.user:
            conn.login(smtp.user, smtp.password)
        conn.send_message(msg)


def seconds_until(hhmm, now, tz):
    """Seconds from `now` until the next local wall-clock time HH:MM (strictly in the future)."""
    if not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
        raise ValueError(f"expected HH:MM, got {hhmm!r}")
    at = datetime.strptime(hhmm, "%H:%M").time()
    today = datetime.fromtimestamp(now, tz).date()
    for day in (today, today + timedelta(days=1)):
        target = datetime.combine(day, at, tzinfo=tz).timestamp()
        if target > now:
            return target - now
    raise AssertionError("unreachable")


def _local_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def parse_args(argv):
    def env(key, default=None):
        return os.environ.get(key) or default  # empty counts as unset

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=env("DB_PATH", "/data/messages.db"))
    p.add_argument("--list-groups", action="store_true", help="list known groups and exit")
    p.add_argument("--group", action="append", default=[], help="group name, name fragment or JID (repeatable)")
    p.add_argument("--hours", type=int, default=24, help="summarize messages from the last N hours")
    p.add_argument("--model", choices=["dummy", "ollama", "openai"], default=env("SUMMARY_MODEL", "dummy"),
                   help="dummy: stats; ollama: local model; openai: external OpenAI-compatible API (Groq by default)")
    p.add_argument("--allow-real-data", action="store_true",
                   help="let --model openai send chats from a non-synthetic database to the external API")
    p.add_argument("--llm-base-url", default=env("LLM_BASE_URL", "https://api.groq.com/openai/v1"))
    p.add_argument("--llm-model", default=env("LLM_MODEL", "openai/gpt-oss-120b"))
    p.add_argument("--send-to", help="group to send the digest to (otherwise just print it)")
    p.add_argument("--email-to", default=env("EMAIL_TO", ""), help="comma-separated addresses to email the digest to")
    p.add_argument("--email-from", default=env("EMAIL_FROM", ""))
    p.add_argument("--email-title", default=env("EMAIL_TITLE"), help="heading at the top of the email")
    p.add_argument("--email-subject", default=env("EMAIL_SUBJECT", "WhatsApp digest {date}: {groups}"),
                   help="subject template with {date}, {hours} and {groups}")
    p.add_argument("--html-out", help="also write the HTML email to this file (preview without sending)")
    p.add_argument("--ollama-url", default=env("OLLAMA_URL", "http://ollama:11434"))
    # 0.5B models make things up; 3B is the smallest that gives usable summaries (~2.5 GB RAM)
    p.add_argument("--ollama-model", default=env("OLLAMA_MODEL", "qwen2.5:3b"))
    p.add_argument("--bridge-url", default=env("BRIDGE_URL", "http://bridge:8080"))
    p.add_argument("--language", default=env("SUMMARY_LANGUAGE", "Polish"))
    p.add_argument("--max-chars", type=int, default=int(env("MAX_TRANSCRIPT_CHARS", "12000")))
    p.add_argument("--tz", default=env("TZ", "Europe/Warsaw"))
    p.add_argument("--daily-at", default=env("DAILY_AT"), help="keep running and send a digest every day at HH:MM")
    args = p.parse_args(argv)
    if args.max_chars < 1:
        p.error(f"--max-chars / MAX_TRANSCRIPT_CHARS must be at least 1, got {args.max_chars}")
    args.llm_api_key = env("LLM_API_KEY", "")
    retention = env("RETENTION_DAYS", "0").strip()
    if not (retention.isascii() and retention.isdigit() and int(retention) <= 36500):
        p.error(f"RETENTION_DAYS must be a whole number of days between 0 and 36500, got {retention!r}")
    args.retention_days = int(retention)
    log = env("LLM_LOG_DB", "")
    args.llm_log = None if log == "off" else log or os.path.join(os.path.dirname(args.db) or ".", "llm_log.db")
    effort = env("LLM_REASONING_EFFORT", "low")
    args.llm_reasoning_effort = None if effort == "off" else effort
    args.email_to = [a.strip() for a in args.email_to.split(",") if a.strip()]
    if not args.group:
        groups_env = env("DIGEST_GROUPS") or env("WATCH_GROUPS", "")
        args.group = [g.strip() for g in groups_env.split(",") if g.strip()]
    # Defaults target Resend (password = API key); any SMTP server works.
    args.smtp = SMTPConfig(
        host=env("SMTP_HOST", "smtp.resend.com"),
        port=int(env("SMTP_PORT", "465")),
        user=env("SMTP_USER", "resend"),
        password=env("SMTP_PASSWORD", ""),
        security=env("SMTP_SECURITY", "ssl"),
    )
    return args


def run_digest(args, db, groups, target, llm, now, tz, call_log=None, synthetic=False):
    """Build one digest and deliver it (print, and optionally group and/or email)."""
    since = int(now - args.hours * 3600)
    lang = args.language
    warning = None
    if not synthetic:  # sample databases have no bridge behind them
        if status := check_bridge(args.bridge_url):
            warning = labels(lang)["bridge"][status]
            print(f"warning: bridge {status}", file=sys.stderr, flush=True)
    usage_before = dict(getattr(llm, "usage", None) or {})
    sections = []
    for jid, name in groups:
        msgs = fetch_messages(db, jid, since)
        omitted = ocr = voice = 0
        if not msgs:
            summary = None
        elif llm:
            transcript, omitted = build_transcript(msgs, args.max_chars, tz)
            try:
                summary = llm.summarize(transcript, args.language)
                ocr = tagged_sent(msgs, omitted, OCR_TAG)
                voice = tagged_sent(msgs, omitted, VOICE_TAG)
            except (RuntimeError, OSError) as e:  # API error, timeout, network
                print(f"warning: {name}: {e}", file=sys.stderr, flush=True)
                omitted = 0  # the fallback statistics cover every message
                unavailable = labels(lang)["model_unavailable"].format(error=e)
                summary = f"{dummy_summary(msgs, tz, lang)}\n({unavailable})"
            if call_log and getattr(llm, "last_exchange", None):
                try:
                    call_log.record(llm.last_exchange, jid, name, now, synthetic)
                except (sqlite3.Error, OSError) as e:  # logging must never block the digest
                    print(f"warning: could not log the LLM call: {e}", file=sys.stderr, flush=True)
        else:
            summary = dummy_summary(msgs, tz, lang)
        sections.append(Section(name, summary, len(msgs), omitted, ocr, voice))

    common = {"title": args.email_title, "lang": lang, "warning": warning}
    digest = build_digest(sections, args.hours, **common)
    print(digest, flush=True)
    if usage := getattr(llm, "usage", None):
        run = {k: v - usage_before.get(k, 0) for k, v in usage.items()}  # this run only
        print(
            f"\nTokens: {run['prompt_tokens']} in + {run['completion_tokens']} out "
            f"= {run['total_tokens']} total",
            file=sys.stderr,
        )
    if target:
        send_to_bridge(args.bridge_url, target[0], digest)
        print(f"\nSent to {target[1]}.", file=sys.stderr)
    day = datetime.fromtimestamp(now, tz).strftime("%d.%m.%Y")
    html = build_html_digest(sections, args.hours, day, **common)
    if args.html_out:
        with open(args.html_out, "w", encoding="utf-8") as f:
            f.write(html)
    if args.email_to:
        groups_list = ", ".join(s.name for s in sections)
        subject = args.email_subject.format(date=day, hours=args.hours, groups=groups_list)
        send_email(args.smtp, args.email_from, args.email_to, subject, digest, html=html)
        print(f"\nEmailed to {', '.join(args.email_to)}.", file=sys.stderr)


def main(argv=None, now=None):
    args = parse_args(argv)
    tz = _local_tz(args.tz)
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    if args.list_groups:
        rows = db.execute(
            "SELECT c.jid, COALESCE(c.name, ''), COUNT(m.id) FROM chats c "
            "LEFT JOIN messages m ON m.chat_jid = c.jid GROUP BY c.jid ORDER BY c.name"
        )
        for jid, name, count in rows:
            print(f"{jid}  {name}  ({count} messages)")
        return 0

    if not args.group:
        print("error: give at least one --group (see --list-groups)", file=sys.stderr)
        return 2
    if args.email_to and not (args.email_from and args.smtp.password):
        print("error: emailing needs EMAIL_FROM and SMTP_PASSWORD (see .env.example)", file=sys.stderr)
        return 2
    try:
        args.email_subject.format(date="", hours=0, groups="")
    except (KeyError, IndexError, ValueError) as e:
        print(f"error: bad EMAIL_SUBJECT template ({e!r}); use {{date}}, {{hours}}, {{groups}}", file=sys.stderr)
        return 2
    if args.model == "openai":
        if not args.llm_api_key:
            print("error: --model openai needs LLM_API_KEY", file=sys.stderr)
            return 2
        if not is_synthetic(db) and not args.allow_real_data:
            print(
                "error: refusing to send real chats to an external API; use a sample database "
                "(sample_data.py) or pass --allow-real-data",
                file=sys.stderr,
            )
            return 2
    try:
        groups = [resolve_group(db, g) for g in args.group]
        target = resolve_group(db, args.send_to) if args.send_to else None
        if args.daily_at:
            seconds_until(args.daily_at, time.time(), tz)  # validate early
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    llm = None
    if args.model == "ollama":
        llm = Ollama(args.ollama_url, args.ollama_model)
        llm.ensure_model()
    elif args.model == "openai":
        llm = OpenAICompat(args.llm_base_url, args.llm_api_key, args.llm_model, args.llm_reasoning_effort)
    call_log = CallLog(args.llm_log, args.retention_days, now) if llm and args.llm_log else None

    def purge_unused_log(at):
        # an old log keeps chats even when this run doesn't log (dummy/ollama): purge it anyway,
        # but never let a failing purge (locked file, full disk) stop the digest
        if not call_log and args.llm_log and args.retention_days and os.path.exists(args.llm_log):
            try:
                CallLog(args.llm_log, args.retention_days, at).db.close()
            except (sqlite3.Error, OSError) as e:
                print(f"warning: could not purge {args.llm_log}: {e}", file=sys.stderr, flush=True)
    synthetic = is_synthetic(db)

    if not args.daily_at:
        purge_unused_log(now)
        run_digest(args, db, groups, target, llm, time.time() if now is None else now, tz, call_log, synthetic)
        return 0

    print(f"Scheduled daily at {args.daily_at} ({args.tz}).", file=sys.stderr, flush=True)
    purge_unused_log(None)
    while True:
        time.sleep(seconds_until(args.daily_at, time.time(), tz))
        purge_unused_log(None)
        try:
            run_digest(args, db, groups, target, llm, time.time(), tz, call_log, synthetic)
        except Exception as e:  # keep the schedule alive; the next run may succeed
            print(f"error: digest failed: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
