import io
import unittest
import urllib.parse
from contextlib import redirect_stderr
from datetime import date

import librus_grades as lg
import librus_upcoming as lu
import sms_notices as sms
from test_summarize import FakeServer


def g(subject, value, category="", kind=""):
    return lg.Grade(subject, value, category, None, "Jan Kowalski", date(2026, 11, 6), kind, False, "")


def item(day, group, subject, text="", category=""):
    return lu.Item(day, group, subject, category, text)


THU, FRI = date(2026, 11, 12), date(2026, 11, 13)


class ParseRecipientsTest(unittest.TestCase):
    def test_names_and_numbers(self):
        self.assertEqual(sms.parse_recipients("Ania:500100200; Jaś : +48 600-100-200"),
                         {"Ania": "48500100200", "Jaś": "48600100200"})

    def test_empty(self):
        self.assertEqual(sms.parse_recipients(""), {})

    def test_bad_entry(self):
        for value in ("Ania", "Ania:", ":500100200", "Ania:12ab", "Ania:1;ania:500100200"):
            with self.assertRaisesRegex(ValueError, "SMS_TO"):
                sms.parse_recipients(value)


class ComposeTest(unittest.TestCase):
    def test_new_grades_and_tests_without_polish_letters(self):
        grades = lg.ChildGrades("Ania", [g("Matematyka", "5", "sprawdzian"), g("Język polski", "3")])
        upcoming = lu.ChildUpcoming("Ania", [
            item(THU, "test", "Matematyka", "Dział II – ułamki", "praca klasowa"),
            item(THU, "homework", "Fizyka", "zad. 3"),
            item(FRI, "quiz", "Przyroda"),
            item(FRI, "other", "", "wycieczka"),
        ])
        self.assertEqual(
            sms.compose(grades, upcoming),
            "Ania, nowe oceny: Matematyka 5 (sprawdzian); Jezyk polski 3. "
            "Sprawdziany: czw 12.11 Matematyka: Dzial II - ulamki; pt 13.11 kartkowka Przyroda.")

    def test_nothing_new_means_no_sms(self):
        self.assertIsNone(sms.compose(lg.ChildGrades("Ania", []), lu.ChildUpcoming("Ania", [
            item(THU, "homework", "Fizyka")])))
        self.assertIsNone(sms.compose(None, None))

    def test_errors_and_behaviour_are_left_out(self):
        grades = lg.ChildGrades("Ania", [g("", "+5", kind="behaviour")])
        upcoming = lu.ChildUpcoming("Ania", [], error="Librus is down")
        self.assertIsNone(sms.compose(grades, upcoming))
        self.assertIsNone(sms.compose(lg.ChildGrades("Ania", [], error="down"), None))

    def test_only_tests(self):
        self.assertEqual(sms.compose(lg.ChildGrades("Ania", []),
                                     lu.ChildUpcoming("Ania", [item(FRI, "test", "Historia")])),
                         "Ania, sprawdziany: pt 13.11 Historia.")

    def test_long_message_is_cut_to_three_sms(self):
        grades = lg.ChildGrades("Ania", [g(f"Przedmiot {i}", "5", "sprawdzian") for i in range(40)])
        text = sms.compose(grades, None)
        self.assertEqual(len(text), sms.MAX_CHARS)
        self.assertTrue(text.endswith("..."))

    def test_long_test_description_is_shortened(self):
        text = sms.compose(None, lu.ChildUpcoming("Ania", [item(FRI, "test", "Historia", "x" * 100)]))
        self.assertEqual(text, f"Ania, sprawdziany: pt 13.11 Historia: {'x' * 39}....")


class PlainTest(unittest.TestCase):
    def test_only_single_gsm_characters_remain(self):
        self.assertEqual(sms.plain("[str. 12] {a|b} ~^\\ `x` Kowalski’s «x» 2×3 – „ok”…"),
                         '(str. 12) (a/b) -/ \'x\' Kowalski\'s "x" 2x3 - "ok"...')


class GradeKindTest(unittest.TestCase):
    def test_kind_and_improvement_are_shown(self):
        grades = lg.ChildGrades("Ania", [g("Matematyka", "3", kind="final_proposition"),
                                         g("Fizyka", "4", kind="semester"),
                                         g("Chemia", "5", "sprawdzian")._replace(improvement=True)])
        self.assertEqual(sms.compose(grades, None),
                         "Ania, nowe oceny: Matematyka 3 (propozycja rocznej); Fizyka 4 (semestralna); "
                         "Chemia 5 (sprawdzian, poprawa).")


class BrokenDataTest(unittest.TestCase):
    def test_a_child_that_cannot_be_composed_is_a_warning(self):
        broken = lu.ChildUpcoming("Ania", [item(None, "test", "Historia")])
        err = io.StringIO()
        with redirect_stderr(err):
            sms.notify(sms.Config("tok", {"Ania": "48500100200"}, "", "http://127.0.0.1:9"), [], [broken])
        self.assertIn("warning: SMS to Ania failed", err.getvalue())


class SendTest(unittest.TestCase):
    def test_posts_to_smsapi_with_token(self):
        srv = FakeServer({("POST", "/sms.do"): (200, {"count": 1, "list": [{"id": "1", "status": "QUEUE"}]})})
        self.addCleanup(srv.close)

        sms.send(sms.Config("tok", {}, "", srv.url), "48500100200", "Ania, sprawdziany: pt 13.11 Historia.")

        (method, path, body), = srv.requests
        self.assertEqual(srv.headers[0]["Authorization"], "Bearer tok")
        self.assertEqual(urllib.parse.parse_qs(body), {
            "to": ["48500100200"], "message": ["Ania, sprawdziany: pt 13.11 Historia."],
            "format": ["json"], "encoding": ["utf-8"], "max_parts": ["3"]})

    def test_sender_name_when_set(self):
        srv = FakeServer({("POST", "/sms.do"): (200, {"count": 1, "list": []})})
        self.addCleanup(srv.close)
        sms.send(sms.Config("tok", {}, "Oceny", srv.url), "48500100200", "x")
        self.assertEqual(urllib.parse.parse_qs(srv.requests[0][2])["from"], ["Oceny"])

    def test_error_answer_raises(self):
        for status in (200, 401):
            srv = FakeServer({("POST", "/sms.do"): (status, {"error": 101, "message": "Authorization failed"})})
            self.addCleanup(srv.close)
            with self.assertRaisesRegex(RuntimeError, "101.*Authorization failed"):
                sms.send(sms.Config("tok", {}, "", srv.url), "48500100200", "x")


    def test_refused_number_raises(self):
        srv = FakeServer({("POST", "/sms.do"): (200, {"count": 1, "list": [{"id": "1", "error": 13}]})})
        self.addCleanup(srv.close)
        with self.assertRaisesRegex(RuntimeError, "13"):
            sms.send(sms.Config("tok", {}, "", srv.url), "48500100200", "x")


class NotifyTest(unittest.TestCase):
    def test_each_child_with_a_number_gets_its_own_news(self):
        srv = FakeServer({("POST", "/sms.do"): (200, {"count": 1, "list": []})})
        self.addCleanup(srv.close)
        config = sms.Config("tok", {"ania": "48500100200"}, "", srv.url)
        grades = [lg.ChildGrades("Ania", [g("Matematyka", "5")]), lg.ChildGrades("Jaś", [g("Fizyka", "1")])]
        upcoming = [lu.ChildUpcoming("Ania", []), lu.ChildUpcoming("Jaś", [item(FRI, "test", "Historia")])]

        err = io.StringIO()
        with redirect_stderr(err):
            sms.notify(config, grades, upcoming)

        self.assertEqual([urllib.parse.parse_qs(b)["message"] for _, _, b in srv.requests],
                         [["Ania, nowe oceny: Matematyka 5."]])
        self.assertIn("SMS sent to Ania", err.getvalue())

    def test_grades_without_agenda(self):
        srv = FakeServer({("POST", "/sms.do"): (200, {"count": 1, "list": []})})
        self.addCleanup(srv.close)
        with redirect_stderr(io.StringIO()):
            sms.notify(sms.Config("tok", {"ania": "48500100200"}, "", srv.url),
                       [lg.ChildGrades("Ania", [g("Matematyka", "5")])], [])
        self.assertEqual(len(srv.requests), 1)

    def test_failure_is_a_warning_and_other_children_still_get_theirs(self):
        srv = FakeServer({("POST", "/sms.do"): [(500, {"error": 999, "message": "down"}),
                                                (200, {"count": 1, "list": []})]})
        self.addCleanup(srv.close)
        config = sms.Config("tok", {"ania": "48500100200", "JAŚ": "48600100200"}, "", srv.url)
        grades = [lg.ChildGrades("Ania", [g("Matematyka", "5")]), lg.ChildGrades("Jaś", [g("Fizyka", "1")])]

        err = io.StringIO()
        with redirect_stderr(err):
            sms.notify(config, grades, [])

        self.assertEqual(len(srv.requests), 2)
        self.assertIn("warning: SMS to Ania failed", err.getvalue())
        self.assertNotIn("48500100200", err.getvalue())  # numbers stay out of the logs

    def test_unknown_name_is_a_warning(self):
        err = io.StringIO()
        with redirect_stderr(err):
            sms.notify(sms.Config("tok", {"Ola": "48500100200"}, "", "http://127.0.0.1:9"),
                       [lg.ChildGrades("Ania", [])], [])
        self.assertIn("warning: SMS_TO: no Librus child named Ola", err.getvalue())


if __name__ == "__main__":
    unittest.main()
