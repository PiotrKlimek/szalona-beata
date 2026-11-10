import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

import librus_grades as lg
import librus_upcoming as lu
from test_librus_grades import FakeLibrus

TODAY = date(2026, 11, 10)  # a Tuesday


def event(id, day, category_id=1, subject_id=1, content="Dział II", time_from="10:45:00"):
    return SimpleNamespace(id=id, date=day, content=content, category_id=category_id, subject_id=subject_id,
                           time_from=time_from)


def assignment(id, due, topic="Ułamki", text="ćw. 3/13", lesson_id=7, teacher_id=100, date="2026-11-09"):
    return SimpleNamespace(id=id, topic=topic, text=text, teacher_id=teacher_id, date=date, due_date=due,
                           category_id=None, lesson_id=lesson_id, attachments=[])


CATEGORIES = {1: "sprawdzian", 2: "kartkówka", 3: "praca klasowa", 4: "praca domowa", 5: "wycieczka edukacyjna",
              6: "poprawa kartkówki", 7: "poprawa sprawdzianu", 8: "diagnoza przedmiotowa"}


class GroupTest(unittest.TestCase):
    def test_categories_to_groups(self):
        cases = {"sprawdzian": "test", "Praca klasowa": "test", "poprawa pracy klasowej": "test",
                 "nowy termin sprawdzianu": "test", "diagnoza przedmiotowa": "test", "test": "test",
                 "kartkówka": "quiz", "poprawa kartkówki": "quiz",
                 "praca domowa": "homework", "zadanie domowe": "homework",
                 "wycieczka edukacyjna": "other", "spotkanie z Rodzicami": "other", "": "other"}
        for category, group in cases.items():
            self.assertEqual(lu.group_of(category), group, category)


class CheckUpcomingTest(unittest.TestCase):
    def setUp(self):
        FakeLibrus.instances = []
        self.extra = {}

        def opener(login, password, session):
            return FakeLibrus(login, password, session, extra=self.extra.get(login))

        patcher = mock.patch.object(lg, "open_account", opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, accounts, state=None, days=7):
        return lu.check_upcoming(accounts, {} if state is None else state, TODAY, days)

    def test_next_seven_days_from_tomorrow_sorted_by_day(self):
        self.extra["1u"] = {
            "agenda": [event(1, "2026-11-17", 2, content="koniec okna"), event(2, "2026-11-10"),  # today: past at 19:00
                       event(3, "2026-11-18"), event(4, "2026-11-12", 3, 2), event(5, "2026-11-09"),
                       event(6, None)],
            "agenda_categories": CATEGORIES,
            "homework": [assignment(10, "2026-11-11"), assignment(11, "2026-11-20"), assignment(12, None)],
            "lessons": {"Lessons": [{"Id": 7, "Subject": {"Id": 1}}]},
        }
        [child] = self.check([lg.Account("Ania", "1u", "p")])
        self.assertIsNone(child.error)
        self.assertEqual(child.name, "Ania")
        self.assertEqual([(i.day, i.group, i.subject, i.category, i.text) for i in child.items], [
            (date(2026, 11, 11), "homework", "Matematyka", "", "Ułamki – ćw. 3/13"),
            (date(2026, 11, 12), "test", "Język polski", "praca klasowa", "Dział II"),
            (date(2026, 11, 17), "quiz", "Matematyka", "kartkówka", "koniec okna"),
        ])

    def test_homework_without_a_known_lesson_shows_the_teacher(self):
        self.extra["1u"] = {"homework": [assignment(10, "2026-11-11", lesson_id=None, topic="", text="  zad  4 ")],
                            "lessons": RuntimeError("403")}
        [child] = self.check([lg.Account("Ania", "1u", "p")])
        [item] = child.items
        self.assertEqual((item.subject, item.text), ("Jan Kowalski", "zad 4"))

    def test_long_text_is_shortened(self):
        self.extra["1u"] = {"agenda": [event(1, "2026-11-11", content="x " * 400)], "agenda_categories": CATEGORIES}
        [child] = self.check([lg.Account("Ania", "1u", "p")])
        self.assertLessEqual(len(child.items[0].text), lu.MAX_TEXT + 1)
        self.assertTrue(child.items[0].text.endswith("…"))

    def test_one_part_failing_keeps_the_other(self):
        self.extra["1u"] = {"agenda": RuntimeError("agenda down"), "homework": [assignment(10, "2026-11-11")]}
        [child] = self.check([lg.Account("Ania", "1u", "p")])
        self.assertIsNone(child.error)
        self.assertEqual(len(child.items), 1)
        self.assertEqual(child.skipped, ("agenda: agenda down",))

    def test_login_failure_is_an_error_for_that_child_only(self):
        def opener(login, password, session):
            return FakeLibrus(login, password, session, fail=ConnectionError("down") if login == "1u" else None,
                              extra={"agenda": [event(1, "2026-11-11")], "agenda_categories": CATEGORIES})

        with mock.patch.object(lg, "open_account", opener):
            ania, jas = self.check([lg.Account("", "1u", "p"), lg.Account("Jaś", "2u", "p")])
        self.assertEqual((ania.name, ania.error, ania.items), ("1u", "down", []))
        self.assertIsNone(jas.error)
        self.assertEqual(len(jas.items), 1)

    def test_empty_name_takes_the_first_name_and_session_is_kept(self):
        state = {}
        [child] = self.check([lg.Account("", "1u", "p")], state)
        self.assertEqual(child.name, "Ania")
        self.assertEqual(state["1u"]["session"]["cookies"][0]["value"], "dev-1u")
        self.assertEqual(FakeLibrus.instances[0].session, None)
        self.check([lg.Account("", "1u", "p")], state)
        self.assertEqual(FakeLibrus.instances[1].session, state["1u"]["session"])


if __name__ == "__main__":
    unittest.main()
