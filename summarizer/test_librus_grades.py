import asyncio
import json
import os
import shutil
import stat
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

import librus_grades as lg


def grade(id, value="5", subject_id=1, category_id=10, add_date="2026-11-06 10:13:52", **kw):
    fields = dict(id=id, value=value, subject_id=subject_id, category_id=category_id, add_date=add_date,
                  semester=1, is_semester=False, is_final=False, is_semester_proposition=False,
                  is_final_proposition=False, comments=[], teacher_id=100, improves_id=None)
    fields.update(kw)
    return SimpleNamespace(**fields)


class FakeLibrus:
    """Same calls the module makes on librus_synergia.Librus (through open_account)."""

    instances = []

    def __init__(self, login, password, session, grades=(), first_name="Ania", fail=None):
        self.login, self.password, self.session = login, password, session
        self._grades = list(grades)
        self.first_name = first_name
        self.fail = fail
        self.calls = []
        FakeLibrus.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def grades(self):
        self.calls.append("grades")
        if self.fail:
            raise self.fail
        return self._grades

    async def subjects(self):
        self.calls.append("subjects")
        return {1: "Matematyka", 2: "Język polski"}

    async def grade_categories(self):
        self.calls.append("grade_categories")
        return {10: SimpleNamespace(id=10, name="sprawdzian", weight=3, count_to_average=True),
                11: SimpleNamespace(id=11, name="aktywność", weight=1, count_to_average=False)}

    async def teachers(self):
        self.calls.append("teachers")
        return {100: "Jan Kowalski"}

    async def me(self):
        self.calls.append("me")
        if self.fail:  # e.g. a failed login: the first call raises
            raise self.fail
        return SimpleNamespace(first_name=self.first_name, last_name="Nowak")

    def session_dict(self):
        return {"cookies": [{"name": "DeviceCookie", "value": f"dev-{self.login}"}], "logged_in_at": 1.0}


class ParseAccountsTest(unittest.TestCase):
    def test_several_children(self):
        accounts = lg.parse_accounts("Ania:1234567u:tajne;  Jaś:7654321u:inne ")
        self.assertEqual(accounts, [lg.Account("Ania", "1234567u", "tajne"), lg.Account("Jaś", "7654321u", "inne")])

    def test_password_may_contain_colons_and_name_may_be_empty(self):
        self.assertEqual(lg.parse_accounts(":1234567u:a:b:c"), [lg.Account("", "1234567u", "a:b:c")])

    def test_empty_means_off(self):
        self.assertEqual(lg.parse_accounts(""), [])
        self.assertEqual(lg.parse_accounts(" ; "), [])

    def test_bad_entry_error_never_shows_the_password(self):
        for value in ("Ania:1234567u", "Ania::sekret123", "Ania:1234567u:"):
            with self.assertRaises(ValueError) as ctx:
                lg.parse_accounts(value)
            self.assertNotIn("sekret123", str(ctx.exception))
            self.assertIn("name:login:password", str(ctx.exception))

    def test_same_login_twice_is_an_error(self):
        with self.assertRaises(ValueError):
            lg.parse_accounts("Ania:1234567u:a;Ania2:1234567u:b")


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "librus_state.json")

    def test_missing_file_is_empty_state(self):
        self.assertEqual(lg.load_state(self.path), {})

    def test_round_trip_is_private_to_the_owner(self):
        lg.save_state(self.path, {"1234567u": {"seen": ["1"], "session": {"cookies": []}}})
        self.assertEqual(lg.load_state(self.path), {"1234567u": {"seen": ["1"], "session": {"cookies": []}}})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_broken_file_is_empty_state_with_a_warning(self):
        import io
        from contextlib import redirect_stderr

        with open(self.path, "w") as f:
            f.write("{not json")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(lg.load_state(self.path), {})
        self.assertIn("librus_state.json", err.getvalue())

    def test_wrong_shapes_are_dropped(self):
        with open(self.path, "w") as f:
            json.dump({"a": [], "b": {"seen": "x", "session": {"cookies": [], "evil": 1}},
                       "c": {"seen": [1, "2"], "session": {"cookies": [{"n": "v"}], "logged_in_at": 3}}}, f)
        self.assertEqual(lg.load_state(self.path), {
            "a": {}, "b": {}, "c": {"seen": ["1", "2"], "session": {"cookies": [{"n": "v"}], "logged_in_at": 3}}})


class CheckGradesTest(unittest.TestCase):
    def setUp(self):
        FakeLibrus.instances = []
        self.grades = {}  # login -> grades the fake account has
        self.failures = {}
        self.names = {}

        def opener(login, password, session):
            return FakeLibrus(login, password, session, self.grades.get(login, ()),
                              self.names.get(login, "Ania"), self.failures.get(login))

        patcher = mock.patch.object(lg, "open_account", opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, accounts, state):
        return lg.check_grades(accounts, state)

    def test_first_check_only_remembers_existing_grades(self):
        self.grades["1234567u"] = [grade(1), grade(2)]
        state = {}
        children, seen = self.check([lg.Account("Ania", "1234567u", "p")], state)

        self.assertEqual(children, [lg.ChildGrades("Ania", [], None, 2)])
        self.assertEqual(seen, {"1234567u": ["1", "2"]})
        self.assertNotIn("seen", state.get("1234567u", {}))  # committed only after delivery
        self.assertEqual(FakeLibrus.instances[0].calls, ["grades"])  # no lookups when nothing is new

    def test_reports_only_new_grades_with_details(self):
        self.grades["1234567u"] = [
            grade(1),
            grade(3, "4+", subject_id=2, category_id=11, add_date="2026-11-07 08:00:00", comments=["Brawo"]),
            grade(2, "5", improves_id=1),
        ]
        state = {"1234567u": {"seen": ["1"]}}
        children, seen = self.check([lg.Account("Ania", "1234567u", "p")], state)

        ania = children[0]
        self.assertIsNone(ania.error)
        self.assertEqual(ania.seeded, 0)
        self.assertEqual(ania.grades, [
            lg.Grade("Matematyka", "5", "sprawdzian", 3, "Jan Kowalski", date(2026, 11, 6), "", True, ""),
            lg.Grade("Język polski", "4+", "aktywność", None, "Jan Kowalski", date(2026, 11, 7), "", False, "Brawo"),
        ])
        self.assertEqual(seen, {"1234567u": ["1", "2", "3"]})

    def test_semester_and_final_grades_are_marked(self):
        self.grades["1234567u"] = [grade(5, is_semester_proposition=True), grade(6, is_final=True),
                                   grade(7, is_semester=True), grade(8, is_final_proposition=True)]
        children, _ = self.check([lg.Account("Ania", "1234567u", "p")], {"1234567u": {"seen": []}})
        self.assertEqual([g.kind for g in children[0].grades],
                         ["semester_proposition", "final", "semester", "final_proposition"])

    def test_string_ids_find_their_names(self):
        self.grades["1234567u"] = [grade(9, subject_id="2", category_id="10", teacher_id="100")]
        children, _ = self.check([lg.Account("Ania", "1234567u", "p")], {"1234567u": {"seen": []}})
        g = children[0].grades[0]
        self.assertEqual((g.subject, g.category, g.teacher), ("Język polski", "sprawdzian", "Jan Kowalski"))

    def test_unknown_ids_and_missing_date_do_not_fail(self):
        self.grades["1234567u"] = [grade(9, subject_id=99, category_id=None, teacher_id=None, add_date=None)]
        children, _ = self.check([lg.Account("Ania", "1234567u", "p")], {"1234567u": {"seen": []}})
        self.assertEqual(children[0].grades, [lg.Grade("", "5", "", None, "", None, "", False, "")])

    def test_several_children_each_with_own_account_and_session(self):
        self.grades = {"1111111u": [grade(1)], "2222222u": [grade(1), grade(2)]}
        state = {"1111111u": {"seen": [], "session": {"cookies": ["old"]}}, "2222222u": {"seen": ["1"]}}
        accounts = [lg.Account("Ania", "1111111u", "a"), lg.Account("Jaś", "2222222u", "b")]
        children, seen = self.check(accounts, state)

        self.assertEqual([(c.name, len(c.grades)) for c in children], [("Ania", 1), ("Jaś", 1)])
        self.assertEqual(seen, {"1111111u": ["1"], "2222222u": ["1", "2"]})
        first, second = FakeLibrus.instances
        self.assertEqual((first.login, first.password, first.session), ("1111111u", "a", {"cookies": ["old"]}))
        self.assertEqual((second.login, second.password, second.session), ("2222222u", "b", None))
        # the renewed session is kept right away (it is not a "seen" commit)
        self.assertEqual(state["2222222u"]["session"]["cookies"][0]["value"], "dev-2222222u")

    def test_empty_name_uses_the_students_first_name(self):
        self.names["1234567u"] = "Zosia"
        children, _ = self.check([lg.Account("", "1234567u", "p")], {})
        self.assertEqual(children[0].name, "Zosia")

    def test_one_failing_account_does_not_hide_the_others(self):
        self.grades = {"2222222u": [grade(1)]}
        self.failures = {"1111111u": RuntimeError("Invalid credentials")}
        state = {"1111111u": {"seen": ["7"]}, "2222222u": {"seen": []}}
        accounts = [lg.Account("Ania", "1111111u", "a"), lg.Account("Jaś", "2222222u", "b")]
        children, seen = self.check(accounts, state)

        self.assertEqual(children[0], lg.ChildGrades("Ania", [], "Invalid credentials", 0))
        self.assertEqual(len(children[1].grades), 1)
        self.assertNotIn("1111111u", seen)  # nothing new learned: keep what was seen

    def test_failure_without_a_name_falls_back_to_the_login(self):
        self.failures = {"1234567u": TimeoutError()}
        children, _ = self.check([lg.Account("", "1234567u", "p")], {})
        self.assertEqual(children[0].name, "1234567u")
        self.assertEqual(children[0].error, "TimeoutError")

    def test_commit_stores_seen_ids(self):
        state = {"1234567u": {"session": {"cookies": []}}}
        lg.commit(state, {"1234567u": ["1", "2"]})
        self.assertEqual(state, {"1234567u": {"session": {"cookies": []}, "seen": ["1", "2"]}})


class OpenAccountTest(unittest.TestCase):
    def test_real_library_is_wrapped_with_the_saved_session(self):
        # the real librus_synergia isn't installed in the test image: stand in for it
        made = {}

        class SessionData:
            def __init__(self, cookies=(), logged_in_at=0.0):
                self.cookies, self.logged_in_at = list(cookies), logged_in_at

        class Librus:
            def __init__(self, login, password, *, session_data=None):
                made.update(login=login, password=password, session_data=session_data)
                self.session_data = SessionData([{"name": "x"}], 5.0)

            async def close(self):
                made["closed"] = True

            async def grades(self):
                return []

        fake = SimpleNamespace(Librus=Librus, LibrusSessionData=SessionData)
        with mock.patch.dict("sys.modules", {"librus_synergia": fake}):
            async def go():
                async with lg.open_account("1234567u", "p", {"cookies": [{"name": "a"}], "logged_in_at": 2.0}) as acc:
                    await acc.grades()
                    return acc.session_dict()
            session = asyncio.run(go())

        self.assertEqual(made["session_data"].cookies, [{"name": "a"}])
        self.assertTrue(made["closed"])
        self.assertEqual(session, {"cookies": [{"name": "x"}], "logged_in_at": 5.0})
        self.assertEqual(json.loads(json.dumps(session)), session)


if __name__ == "__main__":
    unittest.main()
