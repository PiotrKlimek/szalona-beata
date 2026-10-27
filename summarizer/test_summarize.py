import email.policy
import io
import json
import sqlite3
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sample_data
import summarize

# Same schema the Go bridge creates (bridge/store.go).
SCHEMA = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT);
CREATE TABLE messages (
    chat_jid TEXT NOT NULL, id TEXT NOT NULL, sender_jid TEXT, sender_name TEXT,
    ts INTEGER NOT NULL, text TEXT NOT NULL, from_me INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_jid, id)
);
"""

BIKES = "120363000000000001@g.us"
BIKES_GDYNIA = "120363000000000002@g.us"
SCHOOL = "120363000000000003@g.us"

# 2025-10-23 10:00:00 UTC
T0 = 1761213600


def make_db():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO chats VALUES (?, ?)",
        [(BIKES, "Rowery"), (BIKES_GDYNIA, "Rowery Gdynia"), (SCHOOL, "Klasa 3B")],
    )
    return db


def add_msg(db, chat, msg_id, ts, sender, text):
    db.execute(
        "INSERT INTO messages (chat_jid, id, sender_jid, sender_name, ts, text) VALUES (?, ?, ?, ?, ?, ?)",
        (chat, msg_id, sender.lower() + "@s.whatsapp.net", sender, ts, text),
    )


class ResolveGroupTest(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def test_exact_jid(self):
        self.assertEqual(summarize.resolve_group(self.db, SCHOOL), (SCHOOL, "Klasa 3B"))

    def test_exact_name_is_case_insensitive(self):
        # "rowery" is also a substring of "Rowery Gdynia" - exact match must win.
        self.assertEqual(summarize.resolve_group(self.db, "rowery"), (BIKES, "Rowery"))

    def test_unique_substring(self):
        self.assertEqual(summarize.resolve_group(self.db, "3b"), (SCHOOL, "Klasa 3B"))

    def test_ambiguous_substring_raises(self):
        with self.assertRaisesRegex(ValueError, "Rowery Gdynia"):
            summarize.resolve_group(self.db, "ower")

    def test_unknown_group_raises(self):
        with self.assertRaises(ValueError):
            summarize.resolve_group(self.db, "nonexistent")


class FetchMessagesTest(unittest.TestCase):
    def test_returns_chat_messages_since_timestamp_in_order(self):
        db = make_db()
        add_msg(db, BIKES, "c", T0 + 120, "Bartek", "third")
        add_msg(db, BIKES, "a", T0 - 1, "Ania", "too old")
        add_msg(db, BIKES, "b", T0, "Ania", "first")
        add_msg(db, SCHOOL, "d", T0 + 60, "Celina", "other chat")

        got = summarize.fetch_messages(db, BIKES, since_ts=T0)

        self.assertEqual(
            got,
            [
                summarize.Msg(T0, "Ania", "first"),
                summarize.Msg(T0 + 120, "Bartek", "third"),
            ],
        )

    def test_missing_sender_name_falls_back_to_jid_user(self):
        db = make_db()
        db.execute(
            "INSERT INTO messages (chat_jid, id, sender_jid, sender_name, ts, text) VALUES (?, ?, ?, ?, ?, ?)",
            (BIKES, "x", "48500100200@s.whatsapp.net", "", T0, "hi"),
        )
        self.assertEqual(summarize.fetch_messages(db, BIKES, T0)[0].sender, "48500100200")


class FormatTranscriptTest(unittest.TestCase):
    MSGS = [
        summarize.Msg(T0, "Ania", "Jedziemy w sobotę?"),
        summarize.Msg(T0 + 90, "Bartek", "Tak, 9:00"),
        summarize.Msg(T0 + 300, "Ania", "Super"),
    ]

    def test_formats_time_sender_and_text(self):
        got = summarize.format_transcript(self.MSGS, max_chars=10_000, tz=timezone.utc)
        self.assertEqual(
            got,
            "[10:00] Ania: Jedziemy w sobotę?\n"
            "[10:01] Bartek: Tak, 9:00\n"
            "[10:05] Ania: Super",
        )

    def test_truncation_keeps_most_recent_messages(self):
        # Room for the last two lines (25 + 1 + 19 chars) but not all three (+ 32 + 1).
        got = summarize.format_transcript(self.MSGS, max_chars=50, tz=timezone.utc)
        self.assertEqual(
            got,
            "[... 1 earlier messages omitted]\n"
            "[10:01] Bartek: Tak, 9:00\n"
            "[10:05] Ania: Super",
        )


class FormatTranscriptPlaceholderTest(unittest.TestCase):
    def test_skips_media_without_caption(self):
        msgs = [
            summarize.Msg(T0, "Ania", "[image]"),
            summarize.Msg(T0 + 60, "Ania", "[image] Plan lekcji"),
            summarize.Msg(T0 + 120, "Bartek", "[sticker]"),
            summarize.Msg(T0 + 180, "Bartek", "[voice note]"),
            summarize.Msg(T0 + 240, "Celina", "Dzięki!"),
        ]
        got = summarize.format_transcript(msgs, max_chars=10_000, tz=timezone.utc)
        self.assertEqual(got, "[10:01] Ania: [image] Plan lekcji\n[10:04] Celina: Dzięki!")

    def test_keeps_image_with_recognized_text(self):
        msgs = [summarize.Msg(T0, "Ania", "[image]\n[text in image]\nZebranie wtorek 18:00")]
        got = summarize.format_transcript(msgs, max_chars=10_000, tz=timezone.utc)
        self.assertEqual(got, "[10:00] Ania: [image]\n[text in image]\nZebranie wtorek 18:00")


class PromptOCRNoteTest(unittest.TestCase):
    def test_prompts_explain_recognized_text(self):
        for prompt in (summarize.SYSTEM_PROMPT, summarize.THREADS_PROMPT):
            text = prompt.format(language="Polish", question="Pytanie")
            self.assertIn("'[text in image]'", text)


class TranscriptOmittedCountTest(unittest.TestCase):
    MSGS = [
        summarize.Msg(T0, "Ania", "Jedziemy w sobotę?"),
        summarize.Msg(T0 + 90, "Bartek", "Tak, 9:00"),
        summarize.Msg(T0 + 300, "Ania", "Super"),
    ]

    def test_reports_how_many_messages_were_left_out(self):
        text, omitted = summarize.build_transcript(self.MSGS, max_chars=50, tz=timezone.utc)
        self.assertEqual(omitted, 1)
        self.assertTrue(text.startswith("[... 1 earlier messages omitted]"))

    def test_single_message_longer_than_limit_is_cut_not_dropped(self):
        msgs = [summarize.Msg(T0, "Ania", "a" * 500)]
        text, omitted = summarize.build_transcript(msgs, max_chars=60, tz=timezone.utc)
        self.assertEqual(omitted, 0)
        self.assertTrue(text.startswith("[10:00] Ania: aaa"), text)
        self.assertLessEqual(len(text), 60)
        self.assertTrue(text.endswith("…"))

    def test_newest_message_kept_even_when_it_alone_exceeds_limit(self):
        msgs = [summarize.Msg(T0, "Ania", "stara"), summarize.Msg(T0 + 60, "Bartek", "b" * 500)]
        text, omitted = summarize.build_transcript(msgs, max_chars=60, tz=timezone.utc)
        self.assertEqual(omitted, 1)
        self.assertIn("Bartek: bbb", text)

    def test_nothing_left_out(self):
        _, omitted = summarize.build_transcript(self.MSGS, max_chars=10_000, tz=timezone.utc)
        self.assertEqual(omitted, 0)


class TruncationNoticeTest(unittest.TestCase):
    SECTIONS = [summarize.Section("Klasa 1a (test)", "Wycieczka.", messages=120, omitted=37),
                summarize.Section("Klasa 2b (test)", "Zebranie.", messages=5)]

    def test_text_digest_notes_left_out_messages_under_the_group(self):
        got = summarize.build_digest(self.SECTIONS, hours=24, lang="Polish")
        self.assertIn("*Klasa 1a (test)* (120 wiadomości)\n"
                      "⚠️ Długa rozmowa: w podsumowaniu pominięto najstarsze – 37 wiadomości.\n"
                      "Wycieczka.", got)
        self.assertIn("*Klasa 2b (test)* (5 wiadomości)\nZebranie.", got)  # no note

    def test_english_notice_and_singular(self):
        got = summarize.build_digest([summarize.Section("A", "x", messages=9, omitted=1)], hours=24)
        self.assertIn("⚠️ Long chat: 1 message (the oldest) left out of the summary.", got)

    def test_html_notice(self):
        html = summarize.build_html_digest(self.SECTIONS, hours=24, day="23.10.2026", lang="Polish")
        self.assertEqual(html.count("pominięto najstarsze"), 1)
        self.assertLess(html.index("Klasa 1a (test)"), html.index("pominięto najstarsze – 37 wiadomości"))
        self.assertLess(html.index("pominięto najstarsze – 37 wiadomości"), html.index("Wycieczka."))

    def test_polish_singular(self):
        got = summarize.build_digest([summarize.Section("A", "x", messages=9, omitted=1)], hours=24, lang="Polish")
        self.assertIn("pominięto najstarsze – 1 wiadomość.", got)


class DummySummaryTest(unittest.TestCase):
    def test_range_spanning_days_includes_dates(self):
        msgs = [
            summarize.Msg(T0 - 86400 + 3600, "Ania", "a"),  # 2025-10-22 11:00 UTC
            summarize.Msg(T0, "Bartek", "b"),  # 2025-10-23 10:00 UTC
        ]
        got = summarize.dummy_summary(msgs, tz=timezone.utc)
        self.assertTrue(
            got.startswith("2 messages from 2 people between 22.10 11:00 and 23.10 10:00."), got
        )

    def test_counts_messages_and_ranks_senders(self):
        msgs = [
            summarize.Msg(T0, "Ania", "a"),
            summarize.Msg(T0 + 60, "Bartek", "b"),
            summarize.Msg(T0 + 120, "Ania", "c"),
            summarize.Msg(T0 + 7200, "Ania", "d"),
        ]
        got = summarize.dummy_summary(msgs, tz=timezone.utc)
        self.assertEqual(
            got,
            "4 messages from 2 people between 10:00 and 12:00. "
            "Most active: Ania (3), Bartek (1).",
        )


class BuildDigestTest(unittest.TestCase):
    def test_lists_each_group_and_marks_quiet_ones(self):
        got = summarize.build_digest(
            [("Rowery", "Plan na sobotę."), ("Klasa 3B", None)], hours=24
        )
        self.assertEqual(
            got,
            "📋 Digest (last 24h)\n\n"
            "*Rowery*\nPlan na sobotę.\n\n"
            "*Klasa 3B*\nNo messages.",
        )


class RenderSummaryHtmlTest(unittest.TestCase):
    def test_plain_sentence_becomes_paragraph(self):
        self.assertEqual(
            summarize.render_summary_html("2 messages from 2 people."),
            '<p style="margin:0 0 8px">2 messages from 2 people.</p>',
        )

    def test_bullets_become_list_and_bold_is_kept(self):
        got = summarize.render_summary_html("Ustalenia:\n- **Zebranie** w czwartek\n* Wycieczka 5.11\n\nKoniec.")
        self.assertEqual(
            got,
            '<p style="margin:0 0 8px">Ustalenia:</p>'
            '<ul style="margin:0 0 8px;padding-left:20px">'
            '<li style="margin:0 0 4px"><strong>Zebranie</strong> w czwartek</li>'
            '<li style="margin:0 0 4px">Wycieczka 5.11</li>'
            "</ul>"
            '<p style="margin:0 0 8px">Koniec.</p>',
        )

    def test_message_content_is_escaped(self):
        got = summarize.render_summary_html('<script>alert(1)</script> & "x"')
        self.assertNotIn("<script>", got)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;", got)

    def test_no_messages(self):
        self.assertIn("No messages.", summarize.render_summary_html(None))


class BuildHtmlDigestTest(unittest.TestCase):
    def test_each_group_name_heads_its_summary_in_order(self):
        html = summarize.build_html_digest(
            [("Klasa 1a (test)", "Pierwsze."), ("Klasa 6d (test)", None)], hours=24, day="23.10.2026"
        )

        first_name, first_summary = html.index("Klasa 1a (test)"), html.index("Pierwsze.")
        second_name, second_summary = html.index("Klasa 6d (test)"), html.index("No messages.")
        self.assertLess(first_name, first_summary)
        self.assertLess(first_summary, second_name)
        self.assertLess(second_name, second_summary)
        self.assertIn("23.10.2026", html)
        self.assertIn("last 24h", html)
        self.assertTrue(html.startswith("<!doctype html>"))

    def test_custom_title_replaces_default_heading(self):
        html = summarize.build_html_digest(
            [("Klasa 6d (test)", "x")], hours=24, day="23.10.2026", title="Podsumowanie grup <klasowych>"
        )
        self.assertIn("Podsumowanie grup &lt;klasowych&gt;", html)
        self.assertNotIn("WhatsApp digest", html)

    def test_group_name_is_escaped(self):
        html = summarize.build_html_digest([("Rodzice <3 & dzieci", "x")], hours=24, day="23.10.2026")
        self.assertIn("Rodzice &lt;3 &amp; dzieci", html)
        self.assertNotIn("<3 &", html)


class MessageCountsTest(unittest.TestCase):
    SECTIONS = [
        summarize.Section("Klasa 1a (test)", "Wycieczka 6.11.", messages=120),
        summarize.Section("Klasa 2b (test)", "Zebranie.", messages=2),
        summarize.Section("Klasa 3c (test)", None),
    ]

    def test_text_digest_shows_total_on_top_and_count_per_group(self):
        got = summarize.build_digest(self.SECTIONS, hours=24)
        self.assertTrue(got.startswith("📋 Digest (last 24h)\n122 messages\n\n"), got)
        self.assertIn("*Klasa 1a (test)* (120 messages)\nWycieczka 6.11.", got)
        self.assertIn("*Klasa 2b (test)* (2 messages)\nZebranie.", got)
        self.assertIn("*Klasa 3c (test)*\nNo messages.", got)  # no "(0 messages)"

    def test_html_shows_total_in_header_and_count_per_group(self):
        html = summarize.build_html_digest(self.SECTIONS, hours=24, day="23.10.2026")

        self.assertLess(html.index("122 messages"), html.index("Klasa 1a (test)"))
        after_name = html.index("Klasa 1a (test)")
        self.assertLess(html.index("120 messages", after_name), html.index("Wycieczka 6.11."))

    def test_no_reading_time(self):
        for lang in ("English", "Polish"):
            text = summarize.build_digest(self.SECTIONS, hours=24, lang=lang)
            html = summarize.build_html_digest(self.SECTIONS, hours=24, day="23.10.2026", lang=lang)
            for out in (text, html):
                self.assertNotIn("min", out.replace("Generated", "").replace("minut", ""))

    def test_no_total_without_messages(self):
        quiet = [summarize.Section("Klasa 3c (test)", None)]
        self.assertTrue(summarize.build_digest(quiet, hours=24).startswith("📋 Digest (last 24h)\n\n*Klasa"))

    def test_singular_message(self):
        got = summarize.build_digest([summarize.Section("A", "x", messages=1)], hours=24)
        self.assertTrue(got.startswith("📋 Digest (last 24h)\n1 message\n\n"), got)
        self.assertIn("*A* (1 message)", got)


class PolishLabelsTest(unittest.TestCase):
    def test_polish_plural_forms(self):
        forms = ("wiadomość", "wiadomości", "wiadomości")
        got = [summarize.plural(n, forms, "Polish") for n in (1, 2, 4, 5, 12, 22, 25, 101)]
        self.assertEqual(got, ["1 wiadomość", "2 wiadomości", "4 wiadomości", "5 wiadomości",
                               "12 wiadomości", "22 wiadomości", "25 wiadomości", "101 wiadomości"])
        people = ("osoba", "osoby", "osób")
        self.assertEqual([summarize.plural(n, people, "Polish") for n in (1, 3, 5, 13, 24)],
                         ["1 osoba", "3 osoby", "5 osób", "13 osób", "24 osoby"])

    def test_polish_text_digest(self):
        sections = [summarize.Section("Klasa 1a (test)", "Wycieczka.", messages=120),
                    summarize.Section("Klasa 2b (test)", None),
                    summarize.Section("Klasa 3c (test)", summarize.NOTHING_NOTABLE, messages=2)]
        got = summarize.build_digest(sections, hours=24, lang="Polish")
        self.assertTrue(got.startswith("📋 Podsumowanie (ostatnie 24 h)\n122 wiadomości\n\n"), got)
        self.assertIn("*Klasa 1a (test)* (120 wiadomości)\nWycieczka.", got)
        self.assertIn("*Klasa 2b (test)*\nBrak wiadomości.", got)
        self.assertIn("*Klasa 3c (test)* (2 wiadomości)\nNic istotnego.", got)

    def test_polish_html_digest(self):
        html = summarize.build_html_digest(
            [summarize.Section("Klasa 1a (test)", None), summarize.Section("Klasa 2b (test)", "x", 1)],
            hours=24, day="23.10.2026", lang="Polish")
        self.assertIn("23.10.2026 &middot; ostatnie 24 h &middot; 2 grupy", html)
        self.assertIn("Brak wiadomości.", html)
        self.assertIn("1 wiadomość", html)
        self.assertIn("Wygenerowane lokalnie", html)
        self.assertNotIn("last 24h", html)

    def test_polish_dummy_summary(self):
        msgs = [summarize.Msg(T0, "Ania", "a"), summarize.Msg(T0 + 60, "Bartek", "b"),
                summarize.Msg(T0 + 7200, "Ania", "c")]
        self.assertEqual(
            summarize.dummy_summary(msgs, tz=timezone.utc, lang="Polish"),
            "3 wiadomości (2 osoby), 10:00–12:00. Najaktywniejsi: Ania (2), Bartek (1).",
        )

    def test_kicker_hidden_with_custom_title(self):
        default = summarize.build_html_digest([("A", "x")], hours=24, day="1.1.2026")
        custom = summarize.build_html_digest([("A", "x")], hours=24, day="1.1.2026", title="Podsumowanie od Beaty")
        self.assertIn("Szalona Beata", default)
        self.assertNotIn("Szalona Beata", custom)


class BridgeHealthTest(unittest.TestCase):
    def status(self, response):
        srv = FakeServer({("GET", "/health"): response})
        self.addCleanup(srv.close)
        return summarize.check_bridge(srv.url)

    def test_healthy(self):
        self.assertIsNone(self.status((200, {"connected": True, "logged_in": True})))

    def test_logged_out(self):
        self.assertEqual(self.status((200, {"connected": True, "logged_in": False})), "logged_out")

    def test_disconnected(self):
        self.assertEqual(self.status((200, {"connected": False, "logged_in": True})), "disconnected")

    def test_unreachable(self):
        import socket

        with socket.socket() as sock:  # a port nothing listens on
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertEqual(summarize.check_bridge(f"http://127.0.0.1:{port}"), "unreachable")

    def test_warning_heads_text_and_html_digest(self):
        warning = "⚠️ Mostek WhatsApp jest wylogowany"
        text = summarize.build_digest([("A", None)], hours=24, warning=warning)
        html = summarize.build_html_digest([("A", None)], hours=24, day="1.1.2026", warning=warning)
        self.assertLess(text.index(warning), text.index("*A*"))
        self.assertIn('data-warning="1"', html)
        self.assertLess(html.index(warning), html.index(">A<"))


class CallLogRetentionTest(unittest.TestCase):
    def test_old_entries_are_deleted_on_open(self):
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "llm_log.db")
        log = summarize.CallLog(path)
        day = 86400
        for age_days in (800, 721, 719, 1):
            log.record({"model": f"{age_days}d"}, "g@g.us", "G", T0 - age_days * day, synthetic=False)
        log.db.close()

        summarize.CallLog(path, retention_days=720, now=T0)

        left = [r[0] for r in sqlite3.connect(path).execute("SELECT model FROM llm_calls ORDER BY ts")]
        self.assertEqual(left, ["719d", "1d"])

    def test_record_purges_old_entries_in_a_long_running_process(self):
        # the scheduled digest opens the log once and then only calls record() for months
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "llm_log.db")
        log = summarize.CallLog(path, retention_days=720, now=T0 - 800 * 86400)
        log.record({"model": "old"}, "g@g.us", "G", T0 - 800 * 86400, synthetic=False)

        log.record({"model": "new"}, "g@g.us", "G", T0, synthetic=False)

        left = [r[0] for r in sqlite3.connect(path).execute("SELECT model FROM llm_calls")]
        self.assertEqual(left, ["new"])

    def test_zero_retention_keeps_everything(self):
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "llm_log.db")
        summarize.CallLog(path).record({"model": "old"}, "g@g.us", "G", 0, synthetic=False)

        summarize.CallLog(path, retention_days=0, now=T0)

        self.assertEqual(sqlite3.connect(path).execute("SELECT COUNT(*) FROM llm_calls").fetchone(), (1,))


class RetentionConfigTest(unittest.TestCase):
    def parse(self, value):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"RETENTION_DAYS": value}):
            return summarize.parse_args([]).retention_days

    def test_default_keeps_forever(self):
        self.assertEqual(self.parse(""), 0)

    def test_value(self):
        self.assertEqual(self.parse("720"), 720)

    def test_invalid_values_are_rejected(self):
        for value in ("-5", "²", "two years", "36501"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.parse(value)


class ThreadRenderingTest(unittest.TestCase):
    THREADS = [
        summarize.Thread("Zadanie z angielskiego", ["Str. 20-21 na jutro", "Słówka w zeszycie"]),
        summarize.Thread("Zgubiona bluza <niebieska>", ["Odnaleziona"]),
    ]

    def test_text_digest_separates_threads(self):
        got = summarize.build_digest([("Klasa 4e (test)", self.THREADS)], hours=24)
        self.assertEqual(
            got,
            "📋 Digest (last 24h)\n\n"
            "*Klasa 4e (test)*\n"
            "▸ Zadanie z angielskiego\n- Str. 20-21 na jutro\n- Słówka w zeszycie\n"
            "▸ Zgubiona bluza <niebieska>\n- Odnaleziona",
        )

    def test_html_renders_each_thread_as_its_own_block_in_order(self):
        html = summarize.render_summary_html(self.THREADS)

        self.assertEqual(html.count('data-thread="'), 2)
        first, second = html.index("Zadanie z angielskiego"), html.index("Zgubiona bluza")
        self.assertLess(first, html.index("Słówka w zeszycie"))
        self.assertLess(html.index("Słówka w zeszycie"), second)
        self.assertLess(second, html.index("Odnaleziona"))
        # the second block starts after the first one closes
        self.assertLess(html.index("</div>", html.index("Słówka w zeszycie")), second)

    def test_html_escapes_thread_titles_and_points(self):
        html = summarize.render_summary_html([summarize.Thread("<b>x</b>", ["a & <i>b</i>"])])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertIn("a &amp; &lt;i&gt;b&lt;/i&gt;", html)
        self.assertNotIn("<i>", html)


class FakeServer:
    """Real HTTP server on localhost that records requests and replays canned responses."""

    def __init__(self, responses):
        self.responses = responses  # {(method, path): (status, body_dict)}
        self.requests = []
        self.headers = []  # request headers, parallel to self.requests
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append((self.command, self.path, body))
                outer.headers.append(dict(self.headers))
                # A response is (status, body) or (status, body, headers); a list is replayed in order.
                resp = outer.responses.get((self.command, self.path), (404, {"error": "not found"}))
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                status, payload, extra_headers = (*resp, {})[:3]
                data = json.dumps(payload).encode()
                self.send_response(status)
                for name, value in extra_headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OllamaTest(unittest.TestCase):
    def serve(self, responses):
        srv = FakeServer(responses)
        self.addCleanup(srv.close)
        return srv

    def test_summarize_sends_transcript_and_returns_reply(self):
        srv = self.serve({
            ("POST", "/api/chat"): (200, {"message": {"role": "assistant", "content": " Ustalono wyjazd. "}}),
        })
        ollama = summarize.Ollama(srv.url, "qwen2.5:0.5b")

        got = ollama.summarize("[10:00] Ania: Jedziemy?", language="Polish")

        self.assertEqual(got, "Ustalono wyjazd.")
        method, path, body = srv.requests[0]
        self.assertEqual((method, path), ("POST", "/api/chat"))
        self.assertEqual(body["model"], "qwen2.5:0.5b")
        self.assertIs(body["stream"], False)
        self.assertIn("Polish", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1], {"role": "user", "content": "[10:00] Ania: Jedziemy?"})

    def test_ensure_model_pulls_missing_model(self):
        srv = self.serve({
            ("GET", "/api/tags"): (200, {"models": [{"name": "llama3.2:1b"}]}),
            ("POST", "/api/pull"): (200, {"status": "success"}),
        })

        summarize.Ollama(srv.url, "qwen2.5:0.5b").ensure_model()

        self.assertEqual(srv.requests[-1], ("POST", "/api/pull", {"model": "qwen2.5:0.5b", "stream": False}))

    def test_ensure_model_skips_pull_when_present(self):
        srv = self.serve({("GET", "/api/tags"): (200, {"models": [{"name": "qwen2.5:0.5b"}]})})

        summarize.Ollama(srv.url, "qwen2.5:0.5b").ensure_model()

        self.assertEqual([r[:2] for r in srv.requests], [("GET", "/api/tags")])


class OpenAICompatTest(unittest.TestCase):
    def serve(self, responses):
        srv = FakeServer(responses)
        self.addCleanup(srv.close)
        return srv

    def test_summarize_posts_chat_completion_with_bearer_key(self):
        srv = self.serve({
            ("POST", "/openai/v1/chat/completions"): (200, {
                "id": "chatcmpl-1", "object": "chat.completion", "model": "openai/gpt-oss-120b",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "\n- Wycieczka we wtorek\n"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58},
            }),
        })
        llm = summarize.OpenAICompat(srv.url + "/openai/v1/", "gsk_test", "openai/gpt-oss-120b")

        got = llm.summarize("[10:00] Ania: Wycieczka we wtorek?", language="Polish")

        self.assertEqual(got, "- Wycieczka we wtorek")
        _, _, body = srv.requests[0]
        self.assertEqual(srv.headers[0]["Authorization"], "Bearer gsk_test")
        self.assertEqual(body["model"], "openai/gpt-oss-120b")
        self.assertIn("Polish", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1], {"role": "user", "content": "[10:00] Ania: Wycieczka we wtorek?"})

    def reply_with(self, content):
        return {("POST", "/v1/chat/completions"): (200, {"choices": [{"message": {"content": content}}]})}

    def test_returns_separate_threads_from_json_reply(self):
        srv = self.serve(self.reply_with(json.dumps({"threads": [
            {"title": "Zadanie z angielskiego", "points": ["Str. 20-21 na jutro", "Niejasne, czy str. 20 jest zadana"]},
            {"title": "Zgubiona bluza", "points": ["Niebieska, rozmiar 128 - odnaleziona"]},
        ]}, ensure_ascii=False)))

        got = summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")

        self.assertEqual(got, [
            summarize.Thread("Zadanie z angielskiego", ["Str. 20-21 na jutro", "Niejasne, czy str. 20 jest zadana"]),
            summarize.Thread("Zgubiona bluza", ["Niebieska, rozmiar 128 - odnaleziona"]),
        ])
        self.assertEqual(srv.requests[0][2]["response_format"], {"type": "json_object"})

    def test_json_wrapped_in_code_fence_is_accepted(self):
        srv = self.serve(self.reply_with('```json\n{"threads": [{"title": "Wycieczka", "points": ["6.11"]}]}\n```'))

        got = summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")

        self.assertEqual(got, [summarize.Thread("Wycieczka", ["6.11"])])

    def test_no_threads_means_nothing_notable(self):
        srv = self.serve(self.reply_with('{"threads": []}'))

        got = summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")

        self.assertEqual(got, "No notable topics.")

    def test_unexpected_json_shape_falls_back_to_text(self):
        content = '{"summary": "Wycieczka 6.11"}'
        srv = self.serve(self.reply_with(content))

        got = summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")

        self.assertEqual(got, content)

    def test_last_exchange_records_what_was_sent_and_received(self):
        content = '{"threads": [{"title": "Wycieczka", "points": ["6.11"]}]}'
        srv = self.serve({("POST", "/v1/chat/completions"): (200, {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        })})
        llm = summarize.OpenAICompat(srv.url + "/v1", "k", "openai/gpt-oss-120b", reasoning_effort="medium")

        llm.summarize("[10:00] Ania: Wycieczka 6.11", "Polish")

        sent = srv.requests[0][2]
        ex = llm.last_exchange
        self.assertEqual(ex["model"], "openai/gpt-oss-120b")
        self.assertEqual(ex["reasoning_effort"], "medium")
        self.assertEqual(ex["system_prompt"], sent["messages"][0]["content"])
        self.assertEqual(ex["transcript"], "[10:00] Ania: Wycieczka 6.11")
        self.assertEqual(ex["response"], content)
        self.assertTrue(ex["parsed"])
        self.assertEqual((ex["prompt_tokens"], ex["completion_tokens"], ex["total_tokens"]), (120, 30, 150))
        self.assertIsNone(ex["error"])
        self.assertGreaterEqual(ex["duration_ms"], 0)

    def test_last_exchange_marks_unparsed_reply(self):
        srv = self.serve({("POST", "/v1/chat/completions"): (200, {"choices": [{"message": {"content": "plain"}}]})})
        llm = summarize.OpenAICompat(srv.url + "/v1", "k", "m")

        llm.summarize("x", "Polish")

        self.assertFalse(llm.last_exchange["parsed"])
        self.assertEqual(llm.last_exchange["response"], "plain")

    def test_last_exchange_records_error(self):
        srv = self.serve({("POST", "/v1/chat/completions"): (500, {"error": {"message": "boom"}})})
        llm = summarize.OpenAICompat(srv.url + "/v1", "k", "m")

        with self.assertRaises(RuntimeError):
            llm.summarize("x", "Polish")

        self.assertIn("boom", llm.last_exchange["error"])
        self.assertIsNone(llm.last_exchange["response"])
        self.assertEqual(llm.last_exchange["transcript"], "x")

    def test_accumulates_token_usage_across_calls(self):
        srv = self.serve({("POST", "/v1/chat/completions"): (200, {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
        })})
        llm = summarize.OpenAICompat(srv.url + "/v1", "k", "m")

        llm.summarize("a", "Polish")
        llm.summarize("b", "Polish")

        self.assertEqual(llm.usage, {"prompt_tokens": 2400, "completion_tokens": 600, "total_tokens": 3000})

    def test_missing_usage_field_is_tolerated(self):
        srv = self.serve({("POST", "/v1/chat/completions"): (200, {"choices": [{"message": {"content": "ok"}}]})})
        llm = summarize.OpenAICompat(srv.url + "/v1", "k", "m")

        llm.summarize("a", "Polish")

        self.assertEqual(llm.usage, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    RATE_LIMITED = (429, {"error": {"message": "Rate limit reached for model on tokens per minute (TPM)",
                                    "type": "tokens", "code": "rate_limit_exceeded"}}, {"Retry-After": "7"})
    OK = (200, {"choices": [{"message": {"content": "Podsumowanie"}}]})

    def test_rate_limited_request_is_retried_after_server_delay(self):
        from unittest import mock

        srv = self.serve({("POST", "/v1/chat/completions"): [self.RATE_LIMITED, self.OK]})

        with mock.patch.object(summarize.time, "sleep") as sleep:
            got = summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")

        self.assertEqual(got, "Podsumowanie")
        self.assertEqual(len(srv.requests), 2)
        sleep.assert_called_once_with(7)

    def test_gives_up_after_repeated_rate_limits(self):
        from unittest import mock

        srv = self.serve({("POST", "/v1/chat/completions"): [self.RATE_LIMITED]})

        with mock.patch.object(summarize.time, "sleep"), self.assertRaisesRegex(RuntimeError, "429"):
            summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")
        self.assertEqual(len(srv.requests), 4)  # first try + 3 retries

    def test_does_not_wait_for_long_retry_after(self):
        # e.g. the daily quota is exhausted - waiting hours would stall the digest
        from unittest import mock

        daily = (429, {"error": {"message": "Rate limit reached (RPD)"}}, {"Retry-After": "3600"})
        srv = self.serve({("POST", "/v1/chat/completions"): [daily, self.OK]})

        with mock.patch.object(summarize.time, "sleep") as sleep, self.assertRaisesRegex(RuntimeError, "RPD"):
            summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")
        sleep.assert_not_called()

    def test_reasoning_effort_is_sent_when_configured(self):
        srv = self.serve({("POST", "/v1/chat/completions"): self.OK})

        summarize.OpenAICompat(srv.url + "/v1", "k", "m", reasoning_effort="low").summarize("x", "Polish")
        summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")

        self.assertEqual(srv.requests[0][2]["reasoning_effort"], "low")
        self.assertNotIn("reasoning_effort", srv.requests[1][2])

    def test_does_not_send_default_python_user_agent(self):
        # Groq's Cloudflare front rejects "Python-urllib/x.y" with 403 (error 1010).
        srv = self.serve({("POST", "/v1/chat/completions"): (200, {"choices": [{"message": {"content": "ok"}}]})})

        summarize.OpenAICompat(srv.url + "/v1", "k", "m").summarize("x", "Polish")

        self.assertFalse(srv.headers[0]["User-Agent"].startswith("Python-urllib"), srv.headers[0]["User-Agent"])

    def test_api_error_message_is_surfaced(self):
        srv = self.serve({
            ("POST", "/v1/chat/completions"): (401, {
                "error": {"message": "Invalid API Key", "type": "invalid_request_error", "code": "invalid_api_key"},
            }),
        })

        with self.assertRaisesRegex(RuntimeError, r"\(401\): Invalid API Key"):
            summarize.OpenAICompat(srv.url + "/v1", "bad", "m").summarize("x", "Polish")


class SendToBridgeTest(unittest.TestCase):
    def test_posts_group_and_text(self):
        srv = FakeServer({("POST", "/send"): (200, {"id": "3EB0SENT"})})
        self.addCleanup(srv.close)

        summarize.send_to_bridge(srv.url, SCHOOL, "Digest")

        self.assertEqual(srv.requests, [("POST", "/send", {"jid": SCHOOL, "text": "Digest"})])

    def test_raises_with_bridge_error_message(self):
        srv = FakeServer({("POST", "/send"): (502, {"error": "websocket not connected"})})
        self.addCleanup(srv.close)

        with self.assertRaisesRegex(RuntimeError, "websocket not connected"):
            summarize.send_to_bridge(srv.url, SCHOOL, "Digest")


class FakeSMTPServer:
    """Minimal plaintext SMTP server on localhost that records auth and messages."""

    def __init__(self):
        import base64
        import socketserver

        self.auth = []
        self.envelopes = []  # (mail_from, [rcpt_to], data)
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line):
                self.wfile.write((line + "\r\n").encode())

            def handle(self):
                mail_from, rcpts = None, []
                self.reply("220 fake ESMTP")
                while line := self.rfile.readline().decode().rstrip("\r\n"):
                    cmd = line.split(" ", 1)[0].upper()
                    if cmd == "EHLO":
                        self.reply("250-fake")
                        self.reply("250 AUTH PLAIN")
                    elif cmd == "AUTH":
                        _, user, password = base64.b64decode(line.split()[2]).decode().split("\0")
                        outer.auth.append((user, password))
                        self.reply("235 ok")
                    elif cmd == "MAIL":
                        mail_from = line.split(":", 1)[1].split()[0].strip("<>")
                        self.reply("250 ok")
                    elif cmd == "RCPT":
                        rcpts.append(line.split(":", 1)[1].strip().strip("<>"))
                        self.reply("250 ok")
                    elif cmd == "DATA":
                        self.reply("354 go")
                        data = []
                        while (l := self.rfile.readline().decode()) != ".\r\n":
                            data.append(l.replace("\r\n", "\n"))  # wire CRLF -> text
                        outer.envelopes.append((mail_from, rcpts, "".join(data)))
                        mail_from, rcpts = None, []
                        self.reply("250 queued")
                    elif cmd == "QUIT":
                        self.reply("221 bye")
                        return
                    else:
                        self.reply("250 ok")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SendEmailTest(unittest.TestCase):
    def test_delivers_digest_with_auth_recipients_and_subject(self):
        import email

        srv = FakeSMTPServer()
        self.addCleanup(srv.close)
        smtp = summarize.SMTPConfig(
            host="127.0.0.1", port=srv.port, user="resend", password="re_secret", security="none"
        )

        summarize.send_email(
            smtp,
            sender="Szalona Beata <onboarding@resend.dev>",
            to=["piko@example.com", "druga@example.com"],
            subject="Digest 23.10",
            body="*Klasa 6d (test)*\nZebranie w czwartek o 18.",
        )

        self.assertEqual(srv.auth, [("resend", "re_secret")])
        mail_from, rcpts, data = srv.envelopes[0]
        self.assertEqual(mail_from, "onboarding@resend.dev")
        self.assertEqual(rcpts, ["piko@example.com", "druga@example.com"])
        msg = email.message_from_string(data, policy=email.policy.default)
        self.assertEqual(msg["Subject"], "Digest 23.10")
        self.assertEqual(msg["To"], "piko@example.com, druga@example.com")
        self.assertEqual(msg.get_content().strip(), "*Klasa 6d (test)*\nZebranie w czwartek o 18.")

    def test_html_body_is_sent_with_plain_text_alternative(self):
        import email

        srv = FakeSMTPServer()
        self.addCleanup(srv.close)
        smtp = summarize.SMTPConfig(host="127.0.0.1", port=srv.port, user="", password="", security="none")

        summarize.send_email(
            smtp, sender="a@example.com", to=["b@example.com"], subject="Digest",
            body="plain version", html="<p>html version</p>",
        )

        msg = email.message_from_string(srv.envelopes[0][2], policy=email.policy.default)
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        self.assertEqual(msg.get_body(("plain",)).get_content().strip(), "plain version")
        self.assertEqual(msg.get_body(("html",)).get_content().strip(), "<p>html version</p>")


class SecondsUntilTest(unittest.TestCase):
    def test_later_today(self):
        # T0 is 10:00 UTC
        self.assertEqual(summarize.seconds_until("20:00", now=T0, tz=timezone.utc), 10 * 3600)

    def test_already_passed_today_waits_for_tomorrow(self):
        self.assertEqual(summarize.seconds_until("09:30", now=T0, tz=timezone.utc), 23.5 * 3600)

    def test_exactly_now_waits_a_full_day(self):
        # Prevents a double run right after a scheduled run finishes in the same second.
        self.assertEqual(summarize.seconds_until("10:00", now=T0, tz=timezone.utc), 24 * 3600)

    def test_dst_change_keeps_wall_clock_time(self):
        from zoneinfo import ZoneInfo

        # 2025-10-25 21:00 Warsaw (CEST, UTC+2); clocks go back on 2025-10-26 03:00,
        # so 20:00 the next day (CET) is 23 wall-clock hours but 24 real hours away.
        now = 1761418800
        self.assertEqual(summarize.seconds_until("20:00", now=now, tz=ZoneInfo("Europe/Warsaw")), 24 * 3600)

    def test_rejects_malformed_time(self):
        with self.assertRaises(ValueError):
            summarize.seconds_until("8pm", now=T0, tz=timezone.utc)


class ParseArgsTest(unittest.TestCase):
    def test_empty_env_vars_fall_back_to_defaults(self):
        # docker compose passes unset ${VAR:-} as empty strings
        import os
        from unittest import mock

        env = {"SMTP_HOST": "", "SMTP_PORT": "", "SMTP_SECURITY": "", "SUMMARY_MODEL": ""}
        with mock.patch.dict(os.environ, env):
            args = summarize.parse_args([])

        self.assertEqual((args.smtp.host, args.smtp.port, args.smtp.security), ("smtp.resend.com", 465, "ssl"))
        self.assertEqual(args.model, "dummy")


class ReasoningEffortConfigTest(unittest.TestCase):
    def parse(self, value):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"LLM_REASONING_EFFORT": value}):
            return summarize.parse_args([]).llm_reasoning_effort

    def test_defaults_to_low(self):
        self.assertEqual(self.parse(""), "low")

    def test_off_disables_parameter(self):
        # models without reasoning support reject the parameter
        self.assertIsNone(self.parse("off"))

    def test_explicit_value(self):
        self.assertEqual(self.parse("high"), "high")


class MaxCharsConfigTest(unittest.TestCase):
    def test_non_positive_limit_is_rejected(self):
        for value in ("0", "-5"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                summarize.parse_args(["--max-chars", value])

    def test_positive_limit(self):
        self.assertEqual(summarize.parse_args(["--max-chars", "500"]).max_chars, 500)


class GroupsFromEnvTest(unittest.TestCase):
    def parse(self, env, argv=()):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"DIGEST_GROUPS": "", "WATCH_GROUPS": "", **env}):
            return summarize.parse_args(list(argv)).group

    def test_digest_groups_env(self):
        self.assertEqual(self.parse({"DIGEST_GROUPS": "1@g.us, Klasa 6d (test)"}), ["1@g.us", "Klasa 6d (test)"])

    def test_falls_back_to_watch_groups(self):
        self.assertEqual(self.parse({"WATCH_GROUPS": "1@g.us,2@g.us"}), ["1@g.us", "2@g.us"])

    def test_command_line_wins(self):
        self.assertEqual(self.parse({"DIGEST_GROUPS": "1@g.us"}, ["--group", "Rowery"]), ["Rowery"])


class SampleDataTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        self.path = os.path.join(tempfile.mkdtemp(), "sample.db")

    def test_sample_db_is_marked_synthetic_and_has_recent_group_chats(self):
        sample_data.create_sample_db(self.path, now=T0)

        db = sqlite3.connect(self.path)
        self.assertTrue(summarize.is_synthetic(db))
        per_chat = db.execute(
            "SELECT c.name, COUNT(*), MIN(m.ts), MAX(m.ts) FROM messages m JOIN chats c ON c.jid = m.chat_jid "
            "GROUP BY c.jid"
        ).fetchall()
        self.assertGreaterEqual(len(per_chat), 3)
        for name, count, first, last in per_chat:
            self.assertGreaterEqual(count, 10, name)
            self.assertGreaterEqual(first, T0 - 24 * 3600, name)
            self.assertLessEqual(last, T0, name)

    def test_refuses_to_overwrite_existing_file(self):
        open(self.path, "w").close()
        with self.assertRaises(FileExistsError):
            sample_data.create_sample_db(self.path, now=T0)

    def test_regular_db_is_not_synthetic(self):
        self.assertFalse(summarize.is_synthetic(make_db()))


class MainTest(unittest.TestCase):
    """End-to-end CLI runs against a real (non-synthetic) SQLite file."""

    def setUp(self):
        import os
        import tempfile

        import shutil

        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.db_path = os.path.join(self.dir, "messages.db")
        self.log_path = os.path.join(self.dir, "llm_log.db")  # default: next to --db
        db = sqlite3.connect(self.db_path)
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO chats VALUES (?, ?)", [(BIKES, "Rowery"), (SCHOOL, "Klasa 3B")])
        add_msg(db, BIKES, "a", T0, "Ania", "Jedziemy?")
        add_msg(db, BIKES, "b", T0 + 60, "Bartek", "Tak")
        db.commit()
        db.close()

        # a healthy bridge unless a test says otherwise
        self.bridge = FakeServer({("GET", "/health"): (200, {"connected": True, "logged_in": True})})
        self.addCleanup(self.bridge.close)
        from unittest import mock

        env = mock.patch.dict(os.environ, {"BRIDGE_URL": self.bridge.url, "SUMMARY_LANGUAGE": "English"})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = summarize.main(["--db", self.db_path, *args], now=T0 + 3600)
        return code, out.getvalue()

    def test_list_groups_shows_jid_name_and_count(self):
        code, out = self.run_main("--list-groups")
        self.assertEqual(code, 0)
        self.assertIn(f"{BIKES}  Rowery  (2 messages)", out)
        self.assertIn(f"{SCHOOL}  Klasa 3B  (0 messages)", out)

    def test_dummy_digest_for_selected_groups(self):
        code, out = self.run_main("--group", "rowery", "--group", "3b", "--hours", "2", "--model", "dummy")
        self.assertEqual(code, 0)
        self.assertIn("*Rowery* (2 messages)\n2 messages from 2 people", out)
        self.assertIn("*Klasa 3B*\nNo messages.", out)

    def test_emails_digest_when_email_to_given(self):
        import email
        import os
        from unittest import mock

        srv = FakeSMTPServer()
        self.addCleanup(srv.close)
        env = {
            "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(srv.port), "SMTP_SECURITY": "none",
            "SMTP_USER": "resend", "SMTP_PASSWORD": "re_secret", "EMAIL_FROM": "onboarding@resend.dev",
        }
        with mock.patch.dict(os.environ, env):
            code, _ = self.run_main("--group", "rowery", "--hours", "2", "--email-to", "piko@example.com")

        self.assertEqual(code, 0)
        _, rcpts, data = srv.envelopes[0]
        self.assertEqual(rcpts, ["piko@example.com"])
        msg = email.message_from_string(data, policy=email.policy.default)
        self.assertIn("*Rowery* (2 messages)\n2 messages from 2 people", msg.get_body(("plain",)).get_content())
        self.assertIn("2 messages from 2 people", msg.get_body(("html",)).get_content())
        self.assertIn("Rowery", msg["Subject"])

    def test_html_out_writes_preview_file(self):
        import os
        import tempfile

        out_path = os.path.join(tempfile.mkdtemp(), "digest.html")
        code, _ = self.run_main("--group", "rowery", "--hours", "2", "--html-out", out_path)

        self.assertEqual(code, 0)
        with open(out_path, encoding="utf-8") as f:
            html = f.read()
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("2 messages from 2 people", html)

    def test_email_subject_template_from_env(self):
        import email
        import os
        from unittest import mock

        srv = FakeSMTPServer()
        self.addCleanup(srv.close)
        env = {
            "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(srv.port), "SMTP_SECURITY": "none",
            "SMTP_PASSWORD": "x", "EMAIL_FROM": "a@example.com",
            "EMAIL_SUBJECT": "Podsumowanie grup klasowych z ostatnich {hours}h {date} ({groups})",
        }
        with mock.patch.dict(os.environ, env):
            code, _ = self.run_main("--group", "rowery", "--hours", "24", "--email-to", "piko@example.com",
                                    "--tz", "UTC")

        self.assertEqual(code, 0)
        msg = email.message_from_string(srv.envelopes[0][2], policy=email.policy.default)
        # now = T0 + 3600 = 2025-10-23 11:00 UTC
        self.assertEqual(msg["Subject"], "Podsumowanie grup klasowych z ostatnich 24h 23.10.2025 (Rowery)")

    def test_bad_subject_template_fails_before_summarizing(self):
        import os
        from unittest import mock

        env = {"SMTP_PASSWORD": "x", "EMAIL_FROM": "a@example.com", "EMAIL_SUBJECT": "Digest {dat}"}
        with mock.patch.dict(os.environ, env):
            code, out = self.run_main("--group", "rowery", "--email-to", "piko@example.com")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")

    def test_email_title_from_env_heads_both_email_versions(self):
        import email
        import os
        from unittest import mock

        srv = FakeSMTPServer()
        self.addCleanup(srv.close)
        env = {
            "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(srv.port), "SMTP_SECURITY": "none",
            "SMTP_PASSWORD": "x", "EMAIL_FROM": "a@example.com",
            "EMAIL_TITLE": "Podsumowanie grup klasowych od Szalonej Beaty",
        }
        with mock.patch.dict(os.environ, env):
            code, out = self.run_main("--group", "rowery", "--hours", "24", "--email-to", "piko@example.com")

        self.assertEqual(code, 0)
        msg = email.message_from_string(srv.envelopes[0][2], policy=email.policy.default)
        self.assertIn("Podsumowanie grup klasowych od Szalonej Beaty", msg.get_body(("html",)).get_content())
        self.assertTrue(
            msg.get_body(("plain",)).get_content().startswith("📋 Podsumowanie grup klasowych od Szalonej Beaty")
        )

    def test_email_without_sender_configured_fails_before_summarizing(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"EMAIL_FROM": ""}):
            code, out = self.run_main("--group", "rowery", "--email-to", "piko@example.com")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")

    def test_external_llm_refuses_real_data(self):
        import os
        from unittest import mock

        srv = FakeServer({})
        self.addCleanup(srv.close)
        env = {"LLM_API_KEY": "gsk_test", "LLM_BASE_URL": srv.url}
        with mock.patch.dict(os.environ, env):
            code, out = self.run_main("--group", "rowery", "--model", "openai")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(srv.requests, [])

    def test_external_llm_allowed_on_real_data_with_explicit_flag(self):
        import os
        from unittest import mock

        srv = FakeServer({("POST", "/chat/completions"): (200, {"choices": [{"message": {"content": "Ustalono."}}]})})
        self.addCleanup(srv.close)
        env = {"LLM_API_KEY": "gsk_test", "LLM_BASE_URL": srv.url}
        with mock.patch.dict(os.environ, env):
            code, out = self.run_main("--group", "rowery", "--hours", "2", "--model", "openai", "--allow-real-data")

        self.assertEqual(code, 0)
        self.assertIn("*Rowery* (2 messages)\nUstalono.", out)

    def test_external_llm_runs_on_synthetic_data_without_flag(self):
        import os
        import tempfile
        from unittest import mock

        sample = os.path.join(tempfile.mkdtemp(), "sample.db")
        sample_data.create_sample_db(sample, now=T0)
        srv = FakeServer({("POST", "/chat/completions"): (200, {"choices": [{"message": {"content": "Wycieczka 6.11."}}]})})
        self.addCleanup(srv.close)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "gsk_test", "LLM_BASE_URL": srv.url}), redirect_stdout(out):
            code = summarize.main(["--db", sample, "--group", "Klasa 2c", "--model", "openai"], now=T0)

        self.assertEqual(code, 0)
        self.assertIn("*Klasa 2c (test)* (16 messages)\nWycieczka 6.11.", out.getvalue())
        self.assertNotIn("Tokens", out.getvalue())  # usage goes to stderr, not into the digest
        self.assertIn("Marta Zielińska: Dzień dobry!", srv.requests[0][2]["messages"][1]["content"])
        log = os.path.join(os.path.dirname(sample), "llm_log.db")
        self.assertEqual(sqlite3.connect(log).execute("SELECT synthetic FROM llm_calls").fetchone(), (1,))

    def test_unavailable_model_falls_back_to_stats_and_still_emails(self):
        import email
        import os
        from unittest import mock

        llm = FakeServer({("POST", "/chat/completions"): (500, {"error": {"message": "Internal Server Error"}})})
        self.addCleanup(llm.close)
        smtp = FakeSMTPServer()
        self.addCleanup(smtp.close)
        env = {
            "LLM_API_KEY": "k", "LLM_BASE_URL": llm.url,
            "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp.port), "SMTP_SECURITY": "none",
            "SMTP_PASSWORD": "x", "EMAIL_FROM": "a@example.com",
        }
        with mock.patch.dict(os.environ, env):
            code, out = self.run_main(
                "--group", "rowery", "--hours", "2", "--model", "openai", "--allow-real-data",
                "--email-to", "piko@example.com",
            )

        self.assertEqual(code, 0)
        self.assertIn("*Rowery* (2 messages)\n2 messages from 2 people", out)
        self.assertIn("model unavailable", out)
        self.assertEqual(len(smtp.envelopes), 1)

    def llm_server(self, content='{"threads": [{"title": "Wyjazd", "points": ["Tak"]}]}'):
        srv = FakeServer({("POST", "/chat/completions"): (200, {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        })})
        self.addCleanup(srv.close)
        return srv

    def test_llm_calls_are_logged_next_to_the_database(self):
        import os
        from unittest import mock

        srv = self.llm_server()
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": srv.url, "LLM_LOG_DB": ""}):
            code, _ = self.run_main("--group", "rowery", "--group", "3b", "--hours", "2",
                                    "--model", "openai", "--allow-real-data")

        self.assertEqual(code, 0)
        rows = sqlite3.connect(self.log_path).execute(
            "SELECT group_jid, group_name, provider, model, transcript, response, parsed, "
            "total_tokens, error, synthetic, ts FROM llm_calls"
        ).fetchall()
        # only Rowery has messages; groups without messages don't call the model
        self.assertEqual(len(rows), 1)
        jid, name, provider, model, transcript, response, parsed, tokens, error, synthetic, ts = rows[0]
        self.assertEqual((jid, name, provider, model), (BIKES, "Rowery", "openai", "openai/gpt-oss-120b"))
        self.assertIn("Ania: Jedziemy?", transcript)
        self.assertIn('"Wyjazd"', response)
        self.assertEqual((parsed, tokens, error, synthetic), (1, 60, None, 0))
        self.assertEqual(ts, T0 + 3600)

    def test_failed_llm_call_is_logged_with_error(self):
        import os
        from unittest import mock

        srv = FakeServer({("POST", "/chat/completions"): (500, {"error": {"message": "Internal Server Error"}})})
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": srv.url, "LLM_LOG_DB": ""}):
            self.run_main("--group", "rowery", "--hours", "2", "--model", "openai", "--allow-real-data")

        (error, response), = sqlite3.connect(self.log_path).execute("SELECT error, response FROM llm_calls")
        self.assertIn("Internal Server Error", error)
        self.assertIsNone(response)

    def test_failing_log_write_does_not_block_the_digest(self):
        import os
        from unittest import mock

        srv = self.llm_server()
        env = {"LLM_API_KEY": "k", "LLM_BASE_URL": srv.url, "LLM_LOG_DB": ""}
        locked = sqlite3.OperationalError("database is locked")
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(summarize.CallLog, "record", side_effect=locked), \
                redirect_stderr(err):
            code, out = self.run_main("--group", "rowery", "--hours", "2", "--model", "openai", "--allow-real-data")

        self.assertEqual(code, 0)
        self.assertIn("▸ Wyjazd", out)
        self.assertIn("warning: could not log", err.getvalue())

    def test_llm_log_can_be_disabled(self):
        import os
        from unittest import mock

        srv = self.llm_server()
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": srv.url, "LLM_LOG_DB": "off"}):
            code, _ = self.run_main("--group", "rowery", "--hours", "2", "--model", "openai", "--allow-real-data")

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.log_path))

    def test_existing_log_is_purged_even_without_llm(self):
        # switching from openai to dummy must not keep old chats in the log forever
        import os
        from unittest import mock

        summarize.CallLog(self.log_path).record({"model": "old"}, BIKES, "Rowery", T0 - 800 * 86400, False)
        with mock.patch.dict(os.environ, {"RETENTION_DAYS": "720", "LLM_LOG_DB": ""}):
            self.run_main("--group", "rowery", "--hours", "2", "--model", "dummy")

        self.assertEqual(sqlite3.connect(self.log_path).execute("SELECT COUNT(*) FROM llm_calls").fetchone(), (0,))

    def test_failing_log_purge_does_not_block_the_digest(self):
        # e.g. the log is locked by someone querying it at digest time
        import os
        from unittest import mock

        os.mkdir(self.log_path)  # exists, but SQLite can't open it
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"RETENTION_DAYS": "720", "LLM_LOG_DB": ""}), redirect_stderr(err):
            code, out = self.run_main("--group", "rowery", "--hours", "2", "--model", "dummy")

        self.assertEqual(code, 0)
        self.assertIn("*Rowery* (2 messages)", out)
        self.assertIn("warning: could not purge", err.getvalue())

    def test_dummy_mode_writes_no_log(self):
        import os

        self.run_main("--group", "rowery", "--hours", "2", "--model", "dummy")

        self.assertFalse(os.path.exists(self.log_path))

    def test_truncated_chat_is_noted_in_the_digest(self):
        import os
        from unittest import mock

        srv = self.llm_server()
        env = {"LLM_API_KEY": "k", "LLM_BASE_URL": srv.url, "LLM_LOG_DB": "off"}
        with mock.patch.dict(os.environ, env):
            code, out = self.run_main("--group", "rowery", "--hours", "2", "--model", "openai",
                                      "--allow-real-data", "--max-chars", "20")

        self.assertEqual(code, 0)
        self.assertIn("⚠️ Long chat: 1 message (the oldest) left out of the summary.", out)

    def test_no_truncation_note_when_model_failed_and_stats_are_shown(self):
        import os
        from unittest import mock

        srv = FakeServer({("POST", "/chat/completions"): (500, {"error": {"message": "Internal Server Error"}})})
        self.addCleanup(srv.close)
        env = {"LLM_API_KEY": "k", "LLM_BASE_URL": srv.url, "LLM_LOG_DB": "off"}
        with mock.patch.dict(os.environ, env), redirect_stderr(io.StringIO()):
            code, out = self.run_main("--group", "rowery", "--hours", "2", "--model", "openai",
                                      "--allow-real-data", "--max-chars", "20")

        self.assertEqual(code, 0)
        self.assertIn("model unavailable", out)  # the stats cover every message...
        self.assertNotIn("Long chat", out)       # ...so nothing was left out

    def test_external_llm_without_api_key_exits_with_error(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"LLM_API_KEY": ""}):
            code, _ = self.run_main("--group", "rowery", "--model", "openai", "--allow-real-data")
        self.assertEqual(code, 2)

    def test_logged_out_bridge_warns_in_digest_and_still_emails(self):
        import email
        import os
        from unittest import mock

        bridge = FakeServer({("GET", "/health"): (200, {"connected": False, "logged_in": False})})
        self.addCleanup(bridge.close)
        smtp = FakeSMTPServer()
        self.addCleanup(smtp.close)
        env = {"BRIDGE_URL": bridge.url, "SUMMARY_LANGUAGE": "Polish",
               "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp.port), "SMTP_SECURITY": "none",
               "SMTP_PASSWORD": "x", "EMAIL_FROM": "a@example.com"}
        with mock.patch.dict(os.environ, env):
            code, out = self.run_main("--group", "rowery", "--hours", "2", "--email-to", "piko@example.com")

        self.assertEqual(code, 0)
        self.assertIn("wylogowany", out)
        msg = email.message_from_string(smtp.envelopes[0][2], policy=email.policy.default)
        self.assertIn("wylogowany", msg.get_body(("html",)).get_content())

    def test_synthetic_database_skips_bridge_check(self):
        import os
        import tempfile
        from unittest import mock

        sample = os.path.join(tempfile.mkdtemp(), "sample.db")
        sample_data.create_sample_db(sample, now=T0)
        with mock.patch.dict(os.environ, {"LLM_LOG_DB": "off"}), redirect_stdout(io.StringIO()):
            summarize.main(["--db", sample, "--group", "Klasa 2c", "--model", "dummy"], now=T0)

        self.assertEqual(self.bridge.requests, [])

    def test_token_usage_is_printed_per_run(self):
        # the scheduled digest reuses one client for days; each run must show only its own usage
        import os
        from unittest import mock

        srv = self.llm_server()  # 50 in + 10 out per call
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": srv.url, "LLM_LOG_DB": "off"}):
            args = summarize.parse_args(["--db", self.db_path, "--hours", "2", "--model", "openai"])
        llm = summarize.OpenAICompat(srv.url, "k", "m")
        db = sqlite3.connect(self.db_path)
        groups = [(BIKES, "Rowery")]

        outputs = []
        for _ in range(2):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                summarize.run_digest(args, db, groups, None, llm, T0 + 3600, timezone.utc, synthetic=True)
            outputs.append(err.getvalue())

        self.assertIn("Tokens: 50 in + 10 out = 60 total", outputs[1])

    def test_long_chat_shows_counts_but_no_reading_time(self):
        db = sqlite3.connect(self.db_path)
        add_msg(db, BIKES, "long", T0 + 120, "Celina", " ".join(["słowo"] * 1000))
        db.commit()
        db.close()

        code, out = self.run_main("--group", "rowery", "--hours", "2", "--model", "dummy")

        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("📋 Digest (last 2h)\n3 messages\n\n"), out)
        self.assertIn("*Rowery* (3 messages)", out)

    def test_unknown_group_exits_with_error(self):
        code, _ = self.run_main("--group", "nonexistent", "--model", "dummy")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
