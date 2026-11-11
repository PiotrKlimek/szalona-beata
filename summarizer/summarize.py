#!/usr/bin/env python3
"""Summarize WhatsApp group messages collected by wa-bridge.

Reads the bridge's SQLite database (read-only), summarizes selected groups
with either a trivial statistics "model" (dummy) or a local Ollama model,
and prints the digest or sends it to a group through the bridge.
"""

import argparse
import html as htmllib
import http.client
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
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import urlsplit

# `python -I` (the Dockerfile) leaves the script's own folder off sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edusky_payments  # noqa: E402
import librus_grades  # noqa: E402
import librus_upcoming  # noqa: E402

# The bridge appends text read from a photo (OCR) after this tag; see bridge/ocr.go.
OCR_TAG = "[text in image]"
OCR_NOTE = (
    f"Text after '{OCR_TAG}' was read from a photo by OCR: treat it as part of that message "
    "(e.g. a timetable or a notice someone photographed) and ignore garbled fragments; "
    "if the recognized text is not a readable notice or sentence (e.g. scattered words from a photo of "
    "a notebook or a poster), skip it entirely: never quote it and never guess what it means."
)
# The bridge appends the transcript of a voice message after this tag; see bridge/voice.go.
VOICE_TAG = "[voice transcript]"
VOICE_NOTE = (
    f"Text after '{VOICE_TAG}' is an automatic transcript of a voice message: treat it as what "
    "the sender said and ignore misheard fragments."
)
# The bridge appends the text of a PDF document after this tag; see bridge/pdf.go.
DOC_TAG = "[text in document]"
DOC_NOTE = (
    f"Text after '{DOC_TAG}' was read from a PDF the sender attached (its text, or OCR of a scan): "
    "treat it as the content of that document (e.g. homework or a notice) and ignore garbled fragments."
)

SYSTEM_PROMPT = (
    "You summarize WhatsApp group chats. Write a short summary in {language}: "
    "the main topics, decisions, dates and open questions, as 3-6 bullet points. "
    "Do not invent anything that is not in the chat. " + OCR_NOTE + " " + VOICE_NOTE + " " + DOC_NOTE
)


# Media without a caption, e.g. "[image]" - noise for a language model.
# Images with text recognized by the bridge are not bare: "[image]\n[text in image]\n...",
# nor are transcribed voice messages: "[voice note]\n[voice transcript]\n...".
BARE_PLACEHOLDER = re.compile(r"^\[[a-z ]+\]$")


def _sent(msgs, keep_senders=()):
    """Messages that go to the model: captionless media is dropped, except from IMPORTANT_SENDERS
    (a teacher's homework photo or PDF matters even without a caption; a sticker never does)."""
    keep = {n.strip().casefold() for n in keep_senders}
    return [m for m in msgs if not BARE_PLACEHOLDER.match(m.text)
            or (m.sender.strip().casefold() in keep and m.text != "[sticker]")]


THREADS_PROMPT = (
    "You summarize WhatsApp group chats. Messages about different topics are often interleaved; "
    "split the chat into separate threads, one per topic (e.g. homework, a lost item, a trip), "
    "and put every message on that topic into its thread. For each thread write a short title "
    "(max 6 words) and up to 4 concise points with decisions, dates, amounts and open questions; "
    "fewer points are fine when there is little to say. "
    "Messages about the same thing (e.g. a link and the text it points to) form one thread - "
    "never split one topic into several threads, and don't make a thread for every sentence of one message. "
    "A question someone asked in the chat that nobody answered is written as a question, "
    "never as a decision or fact, always prefixed exactly like this: '{question}: ...'; "
    "if anyone replied, write the answer as a fact instead. A reply that is only a guess stays "
    "hedged (e.g. 'probably ...'). "
    "Never add questions of your own: a '{question}:' point must repeat a question that a message "
    "in the chat actually asks; an announcement, request or reminder is not a question. "
    "Skip greetings and thanks-only chatter. Do not invent anything that is not in the chat, "
    "and keep who did what to whom and when as written (e.g. 'X paid' is not 'X was paid', "
    "'will send details' is not 'sent details'); when it is unclear, keep the chat's wording. "
    "Copy dates, names and abbreviations as written: don't add a year nobody wrote and don't expand "
    "abbreviations. "
    + OCR_NOTE + " " + VOICE_NOTE + " " + DOC_NOTE + " "
    "Write titles and points in {language}. Respond with JSON only: "
    '{{"threads": [{{"title": "...", "points": ["...", "..."]}}]}}'
)

NOTHING_NOTABLE = "No notable topics."


def important_senders_note(names):
    """Prompt rule for IMPORTANT_SENDERS (e.g. a teacher): nothing they send is skipped."""
    if not names:
        return ""
    quoted = ", ".join(f"'{n}'" for n in names)
    return (
        f"Messages from these senders are always important (e.g. a teacher's homework or notices): {quoted}. "
        "Never skip any of their messages, even an attachment without any text (e.g. '[document] file.pdf' "
        "or '[image]'): give it a thread or a point saying who sent what, with the file name if there is one."
    )


def _with_note(prompt, note):
    return f"{prompt} {note}" if note else prompt

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
    documents: int = 0  # PDFs whose text the model was given


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
        "doc": "📄 Includes text from {count} (may contain recognition errors).",
        "document": ("PDF document", "PDF documents", "PDF documents"),
        "footer": "Generated locally by szalona-beata from your WhatsApp groups.",
        "subst_title": "Substitutions – class {cls}, {day}",
        "subst_none": "No substitutions.",
        "subst_error": "Could not fetch the substitutions ({error}).",
        "subst_link": "Substitutions page",
        "lesson": ("period", "periods"),
        "weekdays": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        "grades_title": "New grades – {name}",
        "grades_none": "No new grades.",
        "grades_error": "Could not fetch the grades ({error}).",
        "grades_first": "First check: the existing grades ({count}) are skipped, from the next digest on only new ones are shown here.",
        "grades_first_kind": "Point grades, text grades and behaviour points are now checked too: the existing ones ({count}) are skipped, from the next digest on only new ones are shown here.",
        "weight": "weight {weight}", "improvement": "improvement", "no_subject": "(unknown subject)",
        "behaviour": "Behaviour",
        "upcoming_title": "Tests and homework, next {days} days – {name}",
        "upcoming_none": "Nothing in the next {days} days.",
        "upcoming_error": "Could not fetch the agenda ({error}).",
        "upcoming_skipped": "Could not fetch part of the data ({error}).",
        "upcoming_groups": {"test": "Tests", "quiz": "Quizzes", "homework": "Homework", "other": "Other"},
        "payments_title": "Payments – {name}",
        "payments_none": "Nothing to pay.",
        "payments_error": "Could not fetch the payments ({error}).",
        "payments_due": "To pay: {amount} by {day}",
        "payments_owed": "To pay: {amount}",
        "payments_nothing": "Nothing to pay",
        "payments_overdue": "including {amount} overdue",
        "payments_overpaid": "overpaid {amount}",
        "grade_kinds": {"semester": "semester grade", "semester_proposition": "proposed semester grade",
                        "final": "final grade", "final_proposition": "proposed final grade"},
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
        "doc": "📄 Uwzględniono tekst z dokumentów PDF (możliwe błędy odczytu): {count}.",
        "document": ("dokument", "dokumenty", "dokumentów"),
        "voice_note": ("wiadomość głosowa", "wiadomości głosowe", "wiadomości głosowych"),
        "footer": "Wygenerowane lokalnie przez szalona-beata z Twoich grup WhatsApp.",
        "subst_title": "Zastępstwa – klasa {cls}, {day}",
        "subst_none": "Brak zastępstw.",
        "subst_error": "Nie udało się pobrać zastępstw ({error}).",
        "subst_link": "Strona zastępstw",
        "lesson": ("lekcja", "lekcje"),
        "weekdays": ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"),
        "grades_title": "Nowe oceny – {name}",
        "grades_none": "Brak nowych ocen.",
        "grades_error": "Nie udało się pobrać ocen ({error}).",
        "grades_first": "Pierwsze sprawdzenie: pominięto dotychczasowe oceny ({count}), od następnego podsumowania pojawią się tu tylko nowe.",
        "grades_first_kind": "Sprawdzane są teraz także oceny punktowe, tekstowe i punkty z zachowania: pominięto dotychczasowe ({count}), od następnego podsumowania pojawią się tu tylko nowe.",
        "weight": "waga {weight}", "improvement": "poprawa", "no_subject": "(przedmiot nieznany)",
        "behaviour": "Zachowanie",
        "upcoming_title": "Sprawdziany i zadania na {days} dni – {name}",
        "upcoming_none": "Nic w najbliższych {days} dniach.",
        "upcoming_error": "Nie udało się pobrać terminarza ({error}).",
        "upcoming_skipped": "Nie udało się pobrać części danych ({error}).",
        "upcoming_groups": {"test": "Sprawdziany i prace klasowe", "quiz": "Kartkówki",
                            "homework": "Zadania domowe", "other": "Inne"},
        "payments_title": "Płatności – {name}",
        "payments_none": "Nic do zapłaty.",
        "payments_error": "Nie udało się pobrać płatności ({error}).",
        "payments_due": "Do zapłaty: {amount} do {day}",
        "payments_owed": "Do zapłaty: {amount}",
        "payments_nothing": "Nic do zapłaty",
        "payments_overdue": "w tym zaległość {amount}",
        "payments_overpaid": "nadpłata {amount}",
        "grade_kinds": {"semester": "ocena śródroczna", "semester_proposition": "propozycja oceny śródrocznej",
                        "final": "ocena roczna", "final_proposition": "propozycja oceny rocznej"},
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


def format_transcript(msgs, max_chars, tz=None, keep_senders=()):
    """The transcript text only (see build_transcript)."""
    return build_transcript(msgs, max_chars, tz, keep_senders)[0]


def build_transcript(msgs, max_chars, tz=None, keep_senders=()):
    """Render messages as chat lines, keeping the most recent ones within max_chars.
    Returns (text, number of messages left out). Captionless media ("[image]") is
    not sent (see _sent), so it doesn't count as left out."""
    lines = [f"[{_hhmm(m.ts, tz)}] {m.sender}: {m.text}" for m in _sent(msgs, keep_senders)]
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


def _doc_note(section, lang):
    L = labels(lang)
    return L["doc"].format(count=plural(section.documents, L["document"], lang))


def _section_notes(section, lang):
    """Notes under a group's name: truncated chat, text read from photos, voice messages and PDFs."""
    return ([_truncated_note(section, lang)] if section.omitted else []) + \
        ([_ocr_note(section, lang)] if section.ocr_images else []) + \
        ([_voice_note(section, lang)] if section.voice_notes else []) + \
        ([_doc_note(section, lang)] if section.documents else [])


def tagged_sent(msgs, omitted, tag, keep_senders=()):
    """Messages with text added by the bridge (OCR_TAG, VOICE_TAG, DOC_TAG) among those build_transcript kept."""
    sent = _sent(msgs, keep_senders)[omitted:]
    return sum(tag in m.text for m in sent)


class Substitutions(NamedTuple):
    cls: str      # class name as configured, e.g. "1C"
    day: object   # datetime.date the substitutions are for
    rows: list    # [(period, text)]; empty = no changes for the class
    error: object = None  # str if they could not be fetched
    url: str = ""  # page to link to


def next_school_day(now, tz):
    """The day after `now`, skipping Saturday and Sunday (holidays are not known)."""
    day = datetime.fromtimestamp(now, tz).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def edupage_base(url):
    """'https://school.edupage.org' from any page of the school's Edupage site."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}"


class _SubstParser(HTMLParser):
    """Collects (header, period, text) rows from Edupage's substitution report."""

    ROLES = ("header", "period", "info", "row", "section")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []  # role (or None) of each open <div>
        self.header = ""
        self.rows = []  # [header, period, text]

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        classes = (dict(attrs).get("class") or "").split()
        role = next((r for r in self.ROLES if r in classes), None)
        if role == "section":
            self.header = ""
        elif role == "row":
            self.rows.append([self.header, "", ""])
        self.stack.append(role)

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        role = next((r for r in reversed(self.stack) if r), None)
        if role == "header":
            self.header += data
        elif role in ("period", "info") and self.rows and "row" in self.stack:
            self.rows[-1][1 if role == "period" else 2] += data


def parse_substitutions(report_html, cls):
    """[(period, text)] for one class (exact name, case-insensitive) from the report HTML."""
    parser = _SubstParser()
    parser.feed(report_html)
    parser.close()
    want = cls.strip().lower()
    return [(" ".join(period.split()).strip("() "), " ".join(text.split()))
            for header, period, text in parser.rows if header.strip().lower() == want]


SUBST_FUNC = "/substitution/server/viewer.js?__func=getSubstViewerDayDataHtml"


def fetch_substitutions(url, cls, day):
    """Substitutions for `cls` on `day` from a public Edupage site; errors end up in .error, never raised."""
    try:
        reply = _request("POST", edupage_base(url) + SUBST_FUNC,
                         {"__args": [None, {"date": day.isoformat(), "mode": "classes"}], "__gsh": "00000000"},
                         timeout=30)
        report = reply.get("r") if isinstance(reply, dict) else None
        if not isinstance(report, str):
            raise ValueError("unexpected reply from Edupage")
        return Substitutions(cls, day, parse_substitutions(report, cls), None, url)
    except Exception as e:  # best-effort section: a broken reply (e.g. IncompleteRead) must not cost the digest
        return Substitutions(cls, day, [], str(e) or type(e).__name__, url)


def _subst_title(subs, lang):
    L = labels(lang)
    day = f"{L['weekdays'][subs.day.weekday()]} {subs.day.strftime('%d.%m.%Y')}"
    return L["subst_title"].format(cls=subs.cls, day=day)


def _subst_lines(subs, lang):
    """'lekcja 4: ...' per row, or the 'none' / 'error' message as a single line (second item False)."""
    L = labels(lang)
    if subs.error:
        return [L["subst_error"].format(error=subs.error)], False
    if not subs.rows:
        return [L["subst_none"]], False
    one, many = L["lesson"]
    lines = []
    for period, text in subs.rows:
        period = period.strip("() ")  # Edupage puts cancelled periods in parentheses
        if m := re.fullmatch(r"(\d+)\s*-\s*(\d+)", period):
            label = f"{many} {m[1]}–{m[2]}"
        elif period.isdigit():
            label = f"{one} {period}"
        else:
            label = period  # e.g. "Cały dzień"
        lines.append(f"{label}: {text}" if label else text)
    return lines, True


def _subst_text(subs, lang):
    lines, is_list = _subst_lines(subs, lang)
    body = "\n".join(f"- {line}" for line in lines) if is_list else lines[0]
    return f"*{_subst_title(subs, lang)}*\n{body}"


def _grade_line(g, lang):
    """'Matematyka: 5 (sprawdzian, waga 3) – Jan Kowalski, 06.11 – „comment”'."""
    L = labels(lang)
    details = [L["grade_kinds"][g.kind]] if g.kind in L["grade_kinds"] else []
    if g.category:
        details.append(g.category)
    if g.weight is not None:
        weight = f"{g.weight:g}"
        details.append(L["weight"].format(weight=weight.replace(".", ",") if lang.lower() == "polish" else weight))
    if g.improvement:
        details.append(L["improvement"])
    subject = L["behaviour"] if g.kind == "behaviour" else g.subject or L["no_subject"]
    line = f"{subject}: {g.value}"
    if details:
        line += f" ({', '.join(details)})"
    who_when = ", ".join(x for x in (g.teacher, g.day.strftime("%d.%m") if g.day else "") if x)
    if who_when:
        line += f" – {who_when}"
    if g.comment:
        line += f" – „{g.comment}”"
    return line


def _grades_lines(child, lang):
    """(lines, note): one line per new grade, and the 'first check' / 'none' / 'error' message or None."""
    L = labels(lang)
    if child.error:
        return [], L["grades_error"].format(error=child.error)
    lines = [_grade_line(g, lang) for g in child.grades]
    if child.seeded:
        return lines, L["grades_first" if child.first else "grades_first_kind"].format(count=child.seeded)
    return lines, None if lines else L["grades_none"]


def _grades_text(child, lang):
    lines, note = _grades_lines(child, lang)
    body = "\n".join([*(f"- {line}" for line in lines), *([note] if note else [])])
    return f"*{labels(lang)['grades_title'].format(name=child.name)}*\n{body}"


# An agenda category that only repeats its group's name is not shown next to the item.
PLAIN_CATEGORY = {"test": {"sprawdzian", "sprawdziany"}, "quiz": {"kartkówka", "kartkówki"},
                  "homework": {"praca domowa", "prace domowe", "zadanie domowe", "zadania domowe"}}


def _upcoming_line(item, lang):
    """'czwartek 12.11 – Matematyka: z.z. 43-60 (praca klasowa)'."""
    when = f"{labels(lang)['weekdays'][item.day.weekday()]} {item.day.strftime('%d.%m')}"
    what = ": ".join(x for x in (item.subject, item.text) if x)
    line = f"{when} – {what}" if what else when
    if item.category and item.category.lower() not in PLAIN_CATEGORY.get(item.group, ()):
        line += f" ({item.category})"
    return line


def _upcoming_parts(child, days, lang):
    """([(group title, [lines])] in group order, [notes]): the notes are 'nothing' / the error / skipped parts."""
    L = labels(lang)
    if child.error:
        return [], [L["upcoming_error"].format(error=child.error)]
    groups = [(L["upcoming_groups"][g], [_upcoming_line(i, lang) for i in child.items if i.group == g])
              for g in librus_upcoming.GROUPS]
    groups = [(title, lines) for title, lines in groups if lines]
    # with a part missing, "nothing" can't be said
    notes = [] if groups or child.skipped else [L["upcoming_none"].format(days=days)]
    if child.skipped:
        notes.append(L["upcoming_skipped"].format(error="; ".join(child.skipped)))
    return groups, notes


def _upcoming_title(child, days, lang):
    return labels(lang)["upcoming_title"].format(days=days, name=child.name)


def _upcoming_text(child, days, lang):
    groups, notes = _upcoming_parts(child, days, lang)
    body = [line for title, lines in groups for line in (f"{title}:", *(f"- {x}" for x in lines))]
    return "\n".join([f"*{_upcoming_title(child, days, lang)}*", *body, *notes])


def _money(amount, lang):
    if lang.lower() == "polish":
        return f"{amount:.2f}".replace(".", ",") + " zł"
    return f"PLN {amount:.2f}"


def _payments_line(payments, lang):
    """(line, muted): one total for all children - what is due by the nearest date, arrears included -
    or 'nothing to pay' (with the overpayment) / 'no agreements' / the error."""
    L = labels(lang)
    if payments.error:
        return L["payments_error"].format(error=payments.error), True
    if not payments.items:
        return L["payments_none"], True
    owing = [d for d in payments.items if d.amount > 0]
    if not owing:
        line = L["payments_nothing"]
        if overpaid := sum(d.overpaid for d in payments.items):
            line += f" ({L['payments_overpaid'].format(amount=_money(overpaid, lang))})"
        return line + ".", True
    days = [d.day for d in owing if d.day]
    amount = _money(sum(d.amount for d in owing), lang)
    line = (L["payments_due"].format(amount=amount, day=min(days).strftime("%d.%m.%Y")) if days
            else L["payments_owed"].format(amount=amount))
    if overdue := sum(d.overdue for d in owing):
        line += f" ({L['payments_overdue'].format(amount=_money(overdue, lang))})"
    return line, False


def _payments_title(payments, lang):
    return labels(lang)["payments_title"].format(name=payments.name)


def _payments_text(payments, lang):
    return f"*{_payments_title(payments, lang)}*\n{_payments_line(payments, lang)[0]}"


def build_digest(sections, hours, title=None, lang="English", warning=None, substitutions=None, grades=(),
                 payments=None, upcoming=(), upcoming_days=7):
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
    parts.extend(_grades_text(child, lang) for child in grades or ())
    parts.extend(_upcoming_text(child, upcoming_days, lang) for child in upcoming or ())
    if substitutions:
        parts.append(_subst_text(substitutions, lang))
    if payments:
        parts.append(_payments_text(payments, lang))
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
    if section.documents:
        note = htmllib.escape(_doc_note(section, lang))
        html += f'<div style="font-size:13px;color:#7a8783;margin:0 0 8px">{note}</div>'
    return html


SUBST_BLOCK = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px" data-substitutions="1">
<tr><td style="background:#fff8e6;border-left:4px solid #e0a100;border-radius:0 8px 8px 0;padding:10px 14px">
<div style="font-size:17px;font-weight:700;color:#6b4a00;margin:0 0 8px">📅 {title}</div>
<div style="font-size:15px;line-height:1.5">{body}</div>
{link}
</td></tr></table>"""


def render_substitutions_html(subs, lang):
    lines, is_list = _subst_lines(subs, lang)
    if is_list:
        lis = "".join(f'<li style="{LI_STYLE}">{htmllib.escape(line)}</li>' for line in lines)
        body = f'<ul style="{UL_STYLE};margin-bottom:0">{lis}</ul>'
    else:
        body = f'<p style="margin:0;color:#7a8783;font-style:italic">{htmllib.escape(lines[0])}</p>'
    link = ""
    if subs.url:
        link = (f'<div style="font-size:13px;margin-top:8px"><a href="{htmllib.escape(subs.url, quote=True)}" '
                f'style="color:#0b5f53">{htmllib.escape(labels(lang)["subst_link"])}</a></div>')
    return SUBST_BLOCK.format(title=htmllib.escape(_subst_title(subs, lang)), body=body, link=link)


GRADES_BLOCK = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px" data-grades="1">
<tr><td style="background:#eef4fd;border-left:4px solid #3b74c4;border-radius:0 8px 8px 0;padding:10px 14px">
<div style="font-size:17px;font-weight:700;color:#1d4580;margin:0 0 8px">🎓 {title}</div>
<div style="font-size:15px;line-height:1.5">{body}</div>
</td></tr></table>"""


def render_grades_html(child, lang):
    lines, note = _grades_lines(child, lang)
    body = ""
    if lines:
        lis = "".join(f'<li style="{LI_STYLE}">{htmllib.escape(line)}</li>' for line in lines)
        body = f'<ul style="{UL_STYLE};margin-bottom:0">{lis}</ul>'
    if note:
        body += f'<p style="margin:0;color:#7a8783;font-style:italic">{htmllib.escape(note)}</p>'
    title = labels(lang)["grades_title"].format(name=child.name)
    return GRADES_BLOCK.format(title=htmllib.escape(title), body=body)


UPCOMING_BLOCK = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px" data-upcoming="1">
<tr><td style="background:#ecf7f0;border-left:4px solid #2f8f5b;border-radius:0 8px 8px 0;padding:10px 14px">
<div style="font-size:17px;font-weight:700;color:#1d5c3a;margin:0 0 8px">📝 {title}</div>
<div style="font-size:15px;line-height:1.5">{body}</div>
</td></tr></table>"""


def render_upcoming_html(child, days, lang):
    groups, notes = _upcoming_parts(child, days, lang)
    body = ""
    for title, lines in groups:
        lis = "".join(f'<li style="{LI_STYLE}">{htmllib.escape(line)}</li>' for line in lines)
        body += (f'<div style="font-weight:700;color:#1d5c3a;margin:4px 0 2px">{htmllib.escape(title)}</div>'
                 f'<ul style="{UL_STYLE}">{lis}</ul>')
    for note in notes:
        body += f'<p style="margin:0;color:#7a8783;font-style:italic">{htmllib.escape(note)}</p>'
    return UPCOMING_BLOCK.format(title=htmllib.escape(_upcoming_title(child, days, lang)), body=body)


PAYMENTS_BLOCK = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px" data-payments="1">
<tr><td style="background:#f4effa;border-left:4px solid #7a4fb5;border-radius:0 8px 8px 0;padding:10px 14px">
<div style="font-size:17px;font-weight:700;color:#4a2a78;margin:0 0 8px">💳 {title}</div>
<div style="font-size:15px;line-height:1.5">{body}</div>
</td></tr></table>"""


def render_payments_html(payments, lang):
    line, muted = _payments_line(payments, lang)
    style = "margin:0;color:#7a8783;font-style:italic" if muted else "margin:0;font-weight:600"
    body = f'<p style="{style}">{htmllib.escape(line)}</p>'
    return PAYMENTS_BLOCK.format(title=htmllib.escape(_payments_title(payments, lang)), body=body)


KICKER = ('<div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:#a8dcd1">'
          'Szalona Beata</div>\n')
WARNING_BANNER = ('<div data-warning="1" style="background:#fdecea;color:#8a1c12;border-radius:8px;'
                  'padding:10px 12px;margin:16px 0 0;font-weight:600">{}</div>\n')


def build_html_digest(sections, hours, day, title=None, lang="English", warning=None, substitutions=None,
                      grades=(), payments=None, upcoming=(), upcoming_days=7):
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
    for child in grades or ():
        cards += "\n" + render_grades_html(child, lang)
    for child in upcoming or ():
        cards += "\n" + render_upcoming_html(child, upcoming_days, lang)
    if substitutions:
        cards += "\n" + render_substitutions_html(substitutions, lang)
    if payments:
        cards += "\n" + render_payments_html(payments, lang)
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
            wait = None
            if e.code == 429:
                try:
                    wait = float(e.headers.get("Retry-After", 10))
                except ValueError:
                    wait = 10
                if attempt < retries and wait <= MAX_RETRY_WAIT:
                    print(f"Rate limited, retrying in {wait:g}s...", file=sys.stderr, flush=True)
                    time.sleep(wait)
                    continue
            _raise_http_error(method, url, e, wait)


class HTTPStatusError(RuntimeError):
    """An HTTP error answer; `retry_after` = the server's Retry-After of a 429, in seconds."""

    def __init__(self, message, code, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _raise_http_error(method, url, e, retry_after=None):
    try:
        detail = json.load(e).get("error", e.reason)
    except ValueError:
        detail = e.reason
    if isinstance(detail, dict):  # OpenAI-style {"error": {"message": ...}}
        detail = detail.get("message", detail)
    raise HTTPStatusError(f"{method} {url} failed ({e.code}): {detail}", e.code, retry_after) from None


def is_transient(e):
    """True for an error that may pass by itself: network (also DNS), timeout, a cut-off answer, 408, 5xx,
    a short 429. Not for a wrong key (401), a too large request (413), an exhausted daily quota (429 with a
    long wait), nor for a complete answer that makes no sense (ValueError, KeyError)."""
    if isinstance(e, HTTPStatusError):
        if e.code == 429:
            return e.retry_after is not None and e.retry_after <= MAX_RETRY_WAIT
        return e.code == 408 or e.code >= 500
    return isinstance(e, (OSError, http.client.HTTPException))


class Ollama:
    # a local model that fails is slow or out of memory, not briefly away: asking again only delays the digest
    retry = False

    def __init__(self, base_url, model, important_senders=()):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.note = important_senders_note(important_senders)

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
                {"role": "system", "content": _with_note(SYSTEM_PROMPT.format(language=language), self.note)},
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


OPENAI_TIMEOUT = 180


class OpenAICompat:
    """Any OpenAI-compatible chat completions API (Groq, Mistral, OpenRouter, Ollama's /v1...)."""

    retry = True  # a remote API that doesn't answer is asked again (LLM_RETRY_MINUTES)

    def __init__(self, base_url, api_key, model, reasoning_effort=None, important_senders=(), timeout=OPENAI_TIMEOUT):
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.note = important_senders_note(important_senders)
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def summarize(self, transcript, language):
        system_prompt = _with_note(THREADS_PROMPT.format(
            language=language, question=QUESTION_LABEL.get(language.lower(), "Question")), self.note)
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
                # LLM_TIMEOUT: a hung API must not eat the retry time; Groq answers in seconds
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
                retries=3,
            )
        except Exception as e:
            ex["error"] = str(e) or type(e).__name__
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


MAX_REPLAY_IDS = 1000


def parse_ids(spec):
    """'25,30-32' -> [25, 30, 31, 32]; ValueError on anything else or more than MAX_REPLAY_IDS ids."""
    ids = []
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not m:
            raise ValueError(f"bad call id {part.strip()!r} (use e.g. 25,30-32)")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        if last < first:
            raise ValueError(f"bad range {part.strip()!r}")
        if len(ids) + last - first + 1 > MAX_REPLAY_IDS:
            raise ValueError(f"at most {MAX_REPLAY_IDS} calls per replay")
        ids.extend(range(first, last + 1))
    return ids


def replay(log_path, ids, llm, tz, allow_real_data, out=None):
    """Run stored transcripts through the current prompt; print the stored and the new response.

    Nothing is logged or sent. Returns an exit code."""
    out = out or sys.stdout
    try:
        db = sqlite3.connect(f"file:{log_path}?mode=ro", uri=True)
        rows = {r[0]: r for r in db.execute(
            f"SELECT id, ts, group_name, language, transcript, response, total_tokens, synthetic, error "
            f"FROM llm_calls WHERE id IN ({', '.join('?' * len(ids))})", ids)}
    except sqlite3.Error as e:
        print(f"error: cannot read {log_path}: {e}", file=sys.stderr)
        return 2
    for missing in (i for i in ids if i not in rows):
        print(f"warning: no call #{missing} in {log_path}", file=sys.stderr)
    if not allow_real_data and any(not r[7] for r in rows.values()):
        print("error: refusing to send real chats to an external API; pass --allow-real-data", file=sys.stderr)
        return 2
    before = done = 0
    sent = {}  # transcript -> id: the failed tries of a retried call share it, one replay is enough
    for i in ids:
        if i not in rows:
            continue
        _, ts, group, language, transcript, response, tokens, _, error = rows[i]
        if transcript in sent:
            print(f"=== #{i}: same transcript as #{sent[transcript]}, skipped", file=out)
            continue
        sent[transcript] = i
        when = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
        print(f"=== #{i} {when} {group}", file=out)
        if response is None:
            print(f"--- before: failed: {error}", file=out)
        else:
            print(f"--- before ({tokens or 0} tokens)", file=out)
            print(_summary_text(parse_threads(response), language), file=out)
        used = llm.usage["total_tokens"]
        try:
            new = llm.summarize(transcript, language)
        except Exception as e:  # one bad reply must not end the whole replay
            print(f"--- after: failed: {e!r}", file=out, flush=True)
            continue
        print(f"--- after ({llm.usage['total_tokens'] - used} tokens)", file=out)
        print(_summary_text(new, language) + "\n", file=out, flush=True)
        before += tokens or 0
        done += 1
    after = llm.usage["total_tokens"]
    print(f"Replayed {done} calls: {before} tokens before, {after} after.", file=out)
    return 0


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


def _ids_arg(spec):
    try:
        return parse_ids(spec)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


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
    p.add_argument("--replay", type=_ids_arg, metavar="IDS",
                   help="re-run logged LLM calls (ids from llm_log.db, e.g. 25,30-32) with the current prompt "
                        "and print old vs new; nothing is emailed or logged")
    p.add_argument("--html-out", help="also write the HTML email to this file (preview without sending)")
    p.add_argument("--ollama-url", default=env("OLLAMA_URL", "http://ollama:11434"))
    # 0.5B models make things up; 3B is the smallest that gives usable summaries (~2.5 GB RAM)
    p.add_argument("--ollama-model", default=env("OLLAMA_MODEL", "qwen2.5:3b"))
    p.add_argument("--bridge-url", default=env("BRIDGE_URL", "http://bridge:8080"))
    p.add_argument("--language", default=env("SUMMARY_LANGUAGE", "Polish"))
    p.add_argument("--max-chars", type=int, default=int(env("MAX_TRANSCRIPT_CHARS", "12000")))
    p.add_argument("--tz", default=env("TZ", "Europe/Warsaw"))
    p.add_argument("--daily-at", default=env("DAILY_AT"), help="keep running and send a digest every day at HH:MM")
    p.add_argument("--substitutions-url", default=env("SUBSTITUTIONS_URL", ""),
                   help="school's Edupage site (e.g. https://school.edupage.org/substitution/)")
    p.add_argument("--substitutions-class", default=env("SUBSTITUTIONS_CLASS", ""),
                   help="class whose substitutions for the next school day are added to the digest, e.g. 1C")
    args = p.parse_args(argv)
    retry = env("FETCH_RETRY_MINUTES", "10").strip()
    if not (retry.isascii() and retry.isdigit() and int(retry) <= 120):
        p.error(f"FETCH_RETRY_MINUTES must be a whole number of minutes between 0 and 120, got {retry!r}")
    args.fetch_retry_minutes = int(retry)
    retry = env("LLM_RETRY_MINUTES", "10").strip()
    if not (retry.isascii() and retry.isdigit() and int(retry) <= 120):
        p.error(f"LLM_RETRY_MINUTES must be a whole number of minutes between 0 and 120, got {retry!r}")
    args.llm_retry_minutes = int(retry)
    timeout = env("LLM_TIMEOUT", str(OPENAI_TIMEOUT)).strip()
    if not (timeout.isascii() and timeout.isdigit() and 10 <= int(timeout) <= 3600):
        p.error(f"LLM_TIMEOUT must be a whole number of seconds between 10 and 3600, got {timeout!r}")
    args.llm_timeout = int(timeout)
    try:  # env only: passwords don't belong on the command line
        args.librus_accounts = librus_grades.parse_accounts(env("LIBRUS_ACCOUNTS", ""))
    except ValueError as e:
        p.error(str(e))
    # env only, like LIBRUS_ACCOUNTS: the password doesn't belong on the command line
    args.edusky = {key: env(f"EDUSKY_{key.upper()}", "") for key in ("url", "login", "password", "name")}
    days = env("LIBRUS_UPCOMING_DAYS", "7").strip()
    valid = days.isascii() and days.isdigit() and int(days) <= 60
    if not valid and args.librus_accounts:  # Librus off: not in the way of the digest
        p.error(f"LIBRUS_UPCOMING_DAYS must be a whole number of days between 0 and 60, got {days!r}")
    args.librus_upcoming_days = int(days) if valid else 0
    args.librus_state = env("LIBRUS_STATE", "") or os.path.join(os.path.dirname(args.db) or ".", "librus_state.json")
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
    # sender names exactly as the digest shows them, e.g. a teacher whose bare attachments must not be skipped
    args.important_senders = [n.strip() for n in env("IMPORTANT_SENDERS", "").split(",") if n.strip()]
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


# Failed school sections are fetched again (a site that is down for a moment must not cost a section):
# after FIRST_RETRY_WAIT seconds, then twice as long each time up to MAX_FETCH_RETRY_WAIT, for at most
# FETCH_RETRY_MINUTES (checked between rounds: the last round may start right at the limit). The clock and sleep are module attributes so that tests can fake them.
FIRST_RETRY_WAIT = 30
MAX_FETCH_RETRY_WAIT = 120
_clock = time.monotonic
_sleep = time.sleep


def _retry_rank(result):
    """0 = complete, 1 = partly fetched (`skipped`), 2 = failed; None = nothing yet."""
    if result is None:
        return 3
    return 2 if result.error else 1 if getattr(result, "skipped", ()) else 0


def _retryable(result):
    return result is None or (_retry_rank(result) > 0 and not getattr(result, "final", False))


def _keep_better(results, key, new):
    """Store `new` unless what is already there is more complete; True if stored."""
    if _retry_rank(new) <= _retry_rank(results.get(key)):
        results[key] = new
        return True
    return False


def fetch_school_sections(args, now, tz):
    """(substitutions, grades, librus_state, librus_seen, upcoming, payments); what fails is fetched
    again, only that, until it answers or the retry time is over. Problems left are logged as warnings."""
    subs_on = bool(args.substitutions_url) and bool(args.substitutions_class)
    if bool(args.substitutions_url) != bool(args.substitutions_class):
        print("warning: substitutions need both SUBSTITUTIONS_URL and SUBSTITUTIONS_CLASS; skipped",
              file=sys.stderr, flush=True)
    edusky = args.edusky
    missing = [f"EDUSKY_{k.upper()}" for k in ("url", "login", "password") if not edusky[k]]
    if len(missing) < 3 and missing:
        print(f"warning: edusky payments need {', '.join(missing)}; skipped", file=sys.stderr, flush=True)
    edusky_on = not missing
    accounts = args.librus_accounts
    upcoming_on = bool(accounts) and bool(args.librus_upcoming_days)
    librus_state = librus_grades.load_state(args.librus_state) if accounts else None
    today = datetime.fromtimestamp(now, tz).date()
    one = {}  # "substitutions" / "edusky" -> result
    grades, seen, upcoming = {}, {}, {}  # by login
    names = {a.login: a.name or a.login for a in accounts}

    deadline = _clock() + args.fetch_retry_minutes * 60
    wait = FIRST_RETRY_WAIT
    while True:
        if subs_on and _retryable(one.get("substitutions")):
            _keep_better(one, "substitutions", fetch_substitutions(
                args.substitutions_url, args.substitutions_class, next_school_day(now, tz)))
        if edusky_on and _retryable(one.get("edusky")):
            _keep_better(one, "edusky", edusky_payments.fetch_payments(
                edusky["url"], edusky["login"], edusky["password"], edusky["name"]))
        todo = [a for a in accounts if _retryable(grades.get(a.login))]
        if todo:
            children, ids = librus_grades.check_grades(todo, librus_state)
            for account, child in zip(todo, children):
                if _keep_better(grades, account.login, child):
                    names[account.login] = child.name
                    if account.login in ids:
                        seen[account.login] = ids[account.login]
                    else:
                        seen.pop(account.login, None)
        if upcoming_on:
            # not for an account whose grades failed for good: its error is shown there too
            todo = [a for a in accounts if _retryable(upcoming.get(a.login))
                    and not (grades[a.login].error and grades[a.login].final)]
            # accounts whose grades just failed are not logged into again (see check_upcoming)
            failed = {a.login: (grades[a.login].name, grades[a.login].error) for a in todo
                      if grades[a.login].error}
            for account, child in zip(todo, librus_upcoming.check_upcoming(
                    todo, librus_state, today, args.librus_upcoming_days, failed)):
                _keep_better(upcoming, account.login, child)
            for a in accounts:  # a final grades error is the agenda's too (also over an earlier passing one)
                if grades[a.login].error and grades[a.login].final:
                    upcoming[a.login] = librus_upcoming.ChildUpcoming(
                        grades[a.login].name, [], grades[a.login].error, final=True)
        # with the reason, so a site that recovers on a retry still leaves a trace in the log
        pending = [f"{k} ({one[k].error or '; '.join(one[k].skipped)})" for k in ("substitutions", "edusky")
                   if k in one and _retryable(one[k])]
        for a in accounts:
            parts = [r for r in (grades[a.login], upcoming.get(a.login)) if r is not None and _retryable(r)]
            if parts:
                reasons = [r.error or "; ".join(r.skipped) for r in parts]
                pending.append(f"librus {names[a.login]} ({'; '.join(dict.fromkeys(reasons))})")
        left = deadline - _clock()
        if not pending or left <= 0:
            break
        wait = min(wait, left)
        print(f"warning: retrying in {wait:.0f}s: {', '.join(pending)}", file=sys.stderr, flush=True)
        _sleep(wait)
        wait = min(wait * 2, MAX_FETCH_RETRY_WAIT)

    substitutions, payments = one.get("substitutions"), one.get("edusky")
    if substitutions and substitutions.error:
        print(f"warning: substitutions: {substitutions.error}", file=sys.stderr, flush=True)
    grades = [grades[a.login] for a in accounts]
    for child in grades:
        if child.error:
            print(f"warning: librus: {child.name}: {child.error}", file=sys.stderr, flush=True)
        for skipped in child.skipped:
            print(f"warning: librus: {child.name}: {skipped}", file=sys.stderr, flush=True)
    upcoming = [upcoming[a.login] for a in accounts] if upcoming_on else []
    for g, child in zip(grades, upcoming):
        if g.error:
            continue  # already in the log as the grades' error
        for problem in ([child.error] if child.error else []) + list(child.skipped):
            print(f"warning: librus agenda: {child.name}: {problem}", file=sys.stderr, flush=True)
    if payments and payments.error:
        print(f"warning: edusky: {payments.error}", file=sys.stderr, flush=True)
    return substitutions, grades, librus_state, seen, upcoming, payments


class RetryWindow:
    """Waits between tries of one kind of call, shared by the whole run: FIRST_RETRY_WAIT, then twice as
    long up to MAX_FETCH_RETRY_WAIT, until `minutes` after the first failure."""

    def __init__(self, minutes):
        self.seconds = minutes * 60
        self.deadline = None
        self.wait = FIRST_RETRY_WAIT

    def next_wait(self):
        """Seconds to wait before trying again, or None when the time is over."""
        now = _clock()
        if self.deadline is None:
            self.deadline = now + self.seconds
        left = self.deadline - now
        if left <= 0:
            return None
        wait = min(self.wait, left)
        self.wait = min(self.wait * 2, MAX_FETCH_RETRY_WAIT)
        return wait


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
    llm_retry = RetryWindow(args.llm_retry_minutes)  # one for all groups: a dead API delays the digest once
    sections = []
    for jid, name in groups:
        msgs = fetch_messages(db, jid, since)
        omitted = ocr = voice = docs = 0
        if not msgs:
            summary = None
        elif llm:
            keep = args.important_senders
            transcript, omitted = build_transcript(msgs, args.max_chars, tz, keep)
            while True:
                if hasattr(llm, "last_exchange"):
                    llm.last_exchange = None
                try:
                    summary = llm.summarize(transcript, args.language)
                    error = None
                except Exception as e:  # API error, timeout, network, a broken answer: never the whole digest
                    error = e
                if call_log and getattr(llm, "last_exchange", None):  # every try, the failed ones too
                    try:
                        call_log.record(llm.last_exchange, jid, name, now, synthetic)
                    except (sqlite3.Error, OSError) as e:  # logging must never block the digest
                        print(f"warning: could not log the LLM call: {e}", file=sys.stderr, flush=True)
                if error is None:
                    ocr = tagged_sent(msgs, omitted, OCR_TAG, keep)
                    voice = tagged_sent(msgs, omitted, VOICE_TAG, keep)
                    docs = tagged_sent(msgs, omitted, DOC_TAG, keep)
                    break
                text = str(error) or type(error).__name__
                wait = llm_retry.next_wait() if getattr(llm, "retry", False) and is_transient(error) else None
                if wait is None:
                    print(f"warning: {name}: {text}", file=sys.stderr, flush=True)
                    omitted = 0  # the fallback statistics cover every message
                    unavailable = labels(lang)["model_unavailable"].format(error=text)
                    summary = f"{dummy_summary(msgs, tz, lang)}\n({unavailable})"
                    break
                print(f"warning: {name}: {text}; retrying in {wait:.0f}s", file=sys.stderr, flush=True)
                _sleep(wait)
        else:
            summary = dummy_summary(msgs, tz, lang)
        sections.append(Section(name, summary, len(msgs), omitted, ocr, voice, docs))

    substitutions, grades, librus_state, librus_seen, upcoming, payments = fetch_school_sections(args, now, tz)
    common = {"title": args.email_title, "lang": lang, "warning": warning, "substitutions": substitutions,
              "grades": grades, "payments": payments, "upcoming": upcoming,
              "upcoming_days": args.librus_upcoming_days}
    digest = build_digest(sections, args.hours, **common)
    print(digest, flush=True)
    if usage := getattr(llm, "usage", None):
        run = {k: v - usage_before.get(k, 0) for k, v in usage.items()}  # this run only
        print(
            f"\nTokens: {run['prompt_tokens']} in + {run['completion_tokens']} out "
            f"= {run['total_tokens']} total",
            file=sys.stderr,
        )
    if target:  # a group chat: children's grades, agenda and payments are only for the email
        private = grades or payments or upcoming
        group_text = build_digest(sections, args.hours,
                                  **{**common, "grades": (), "payments": None, "upcoming": ()}) \
            if private else digest
        send_to_bridge(args.bridge_url, target[0], group_text)
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
    if librus_state is not None:
        if args.email_to:  # emailed: these grades are no longer new
            librus_grades.commit(librus_state, librus_seen)
        try:  # sessions are kept either way (fewer password logins)
            librus_grades.save_state(args.librus_state, librus_state)
        except OSError as e:
            print(f"warning: could not save {args.librus_state}: {e}", file=sys.stderr, flush=True)


def main(argv=None, now=None):
    args = parse_args(argv)
    tz = _local_tz(args.tz)
    if args.replay:
        if args.model != "openai" or not args.llm_api_key:
            print("error: --replay needs --model openai and LLM_API_KEY", file=sys.stderr)
            return 2
        if not args.llm_log:
            print("error: --replay reads the LLM call log, which is disabled (LLM_LOG_DB=off)", file=sys.stderr)
            return 2
        if not os.path.exists(args.llm_log):
            print(f"error: no LLM call log at {args.llm_log} (LLM_LOG_DB)", file=sys.stderr)
            return 2
        llm = OpenAICompat(args.llm_base_url, args.llm_api_key, args.llm_model, args.llm_reasoning_effort,
                           args.important_senders, args.llm_timeout)
        return replay(args.llm_log, args.replay, llm, tz, args.allow_real_data)
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
        llm = Ollama(args.ollama_url, args.ollama_model, args.important_senders)
        llm.ensure_model()
    elif args.model == "openai":
        llm = OpenAICompat(args.llm_base_url, args.llm_api_key, args.llm_model, args.llm_reasoning_effort,
                           args.important_senders, args.llm_timeout)
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
