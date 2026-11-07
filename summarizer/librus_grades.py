"""New grades from Librus Synergia, one account per child (unofficial API, librus-synergia).

LIBRUS_ACCOUNTS="Ania:1234567u:password;Jaś:7654321u:password" - a child's name (empty: the
student's first name from Librus), the Librus login and its password. What has already been
reported is kept in a JSON state file (seen grade ids and the Librus session per login, no
passwords). New grades are remembered only after the digest has been delivered (`commit`), so
a preview run doesn't use them up.
"""

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from typing import NamedTuple


class Account(NamedTuple):
    name: str      # shown in the digest; "" = first name from Librus
    login: str
    password: str


class Grade(NamedTuple):
    subject: str
    value: str          # "5", "4+", "np", ...
    category: str       # "sprawdzian", ...; "" if unknown
    weight: object      # number, or None when the category doesn't count towards the average
    teacher: str
    day: object         # datetime.date it was added, or None
    kind: str           # "" (ordinary), semester, semester_proposition, final, final_proposition
    improvement: bool   # a correction (poprawa) of an earlier grade
    comment: str


class ChildGrades(NamedTuple):
    name: str
    grades: list        # [Grade] new since the last delivered digest
    error: object = None  # str if they could not be fetched
    seeded: int = 0     # > 0 on the first check: how many existing grades were only remembered


def parse_accounts(value):
    """[Account] from 'name:login:password;...'. The password may contain ':' (but not ';')."""
    accounts = []
    for number, entry in enumerate((e.strip() for e in (value or "").split(";")), 1):
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) != 3 or not parts[1].strip() or not parts[2]:
            # never echo the entry: it holds a password
            raise ValueError(f"LIBRUS_ACCOUNTS entry {number} must be name:login:password")
        account = Account(parts[0].strip(), parts[1].strip(), parts[2])
        if any(a.login == account.login for a in accounts):
            raise ValueError(f"LIBRUS_ACCOUNTS entry {number} repeats login {account.login}")
        accounts.append(account)
    return accounts


def load_state(path):
    """{login: {"seen": [grade ids], "session": {...}}}; missing or unreadable file = {}."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path, state):
    """Atomically, readable by the owner only: the sessions grant access to the accounts."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".librus_state.")  # created with mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def commit(state, seen):
    """Remember the grades of a delivered digest ({login: [ids]} from check_grades)."""
    for login, ids in seen.items():
        state.setdefault(login, {})["seen"] = ids


@asynccontextmanager
async def open_account(login, password, session):
    """A librus_synergia.Librus for one account (own HTTP session), with session_dict() to save."""
    import librus_synergia  # only needed when Librus is configured

    saved = librus_synergia.LibrusSessionData(**session) if session else None
    librus = librus_synergia.Librus(login, password, session_data=saved)
    librus.session_dict = lambda: {"cookies": list(librus.session_data.cookies),
                                   "logged_in_at": librus.session_data.logged_in_at}
    try:
        yield librus
    finally:
        await librus.close()


def _kind(g):
    for attr, kind in (("is_final", "final"), ("is_final_proposition", "final_proposition"),
                       ("is_semester", "semester"), ("is_semester_proposition", "semester_proposition")):
        if getattr(g, attr, False):
            return kind
    return ""


def _day(add_date):
    try:
        return date.fromisoformat(str(add_date)[:10]) if add_date else None
    except ValueError:
        return None


async def _check(account, entry):
    """(ChildGrades, seen ids or None); updates entry["session"]."""
    name = account.name
    try:
        async with open_account(account.login, account.password, entry.get("session")) as librus:
            try:
                if not name:
                    name = (await librus.me()).first_name or account.login
                grades = await librus.grades()
                known = set(entry.get("seen") or ())
                new = [g for g in grades if str(g.id) not in known]
                if "seen" not in entry:  # first check: report nothing, remember everything
                    seen = sorted({str(g.id) for g in grades}, key=_id_order)
                    return ChildGrades(name, [], None, len(seen)), seen
                result = []
                if new:  # the lookups only when there is something to show
                    subjects = await librus.subjects()
                    categories = await librus.grade_categories()
                    teachers = await librus.teachers()
                    for g in new:
                        cat = categories.get(g.category_id) if g.category_id is not None else None
                        result.append(Grade(
                            subjects.get(g.subject_id, "") if g.subject_id is not None else "",
                            str(g.value),
                            cat.name if cat else "",
                            cat.weight if cat and cat.count_to_average else None,
                            teachers.get(g.teacher_id, "") if g.teacher_id is not None else "",
                            _day(g.add_date),
                            _kind(g),
                            g.improves_id is not None,
                            " ".join(" ".join(c.split()) for c in (g.comments or []) if c),
                        ))
                    result.sort(key=lambda r: (r.day or date.max, r.subject))
                seen = sorted(known | {str(g.id) for g in grades}, key=_id_order)
                return ChildGrades(name, result), seen
            finally:
                entry["session"] = librus.session_dict()
    except Exception as e:  # one child's failure must not cost the digest or the other children
        return ChildGrades(name or account.login, [], str(e) or type(e).__name__), None


def _id_order(i):
    return (0, int(i), "") if i.isdigit() else (1, 0, i)


def check_grades(accounts, state):
    """([ChildGrades] in account order, {login: seen ids to commit}). Sessions in `state` are updated."""
    async def run():
        return [await _check(a, state.setdefault(a.login, {})) for a in accounts]

    children, seen = [], {}
    for account, (child, ids) in zip(accounts, asyncio.run(run())):
        children.append(child)
        if ids is not None:
            seen[account.login] = ids
    return children, seen
