"""Tests and homework in the next days from Librus Synergia (unofficial API, librus-synergia), per child:
the agenda ("terminarz": tests, quizzes, trips, ...) and homework assignments ("zadania domowe").

Uses the same accounts and saved sessions as librus_grades (LIBRUS_ACCOUNTS, the state file); nothing new is
stored. Every digest shows what is coming from tomorrow on, so nothing has to be remembered.
"""

import asyncio
from datetime import date, timedelta
from typing import NamedTuple

import librus_grades


class Item(NamedTuple):
    day: object      # datetime.date
    group: str       # "test", "quiz", "homework", "other"
    subject: str     # "" if unknown; a homework without a known lesson shows its teacher here
    category: str    # Librus agenda category ("praca klasowa", ...); "" for homework assignments
    text: str


class ChildUpcoming(NamedTuple):
    name: str
    items: list           # [Item] by day
    error: object = None  # str if nothing could be fetched
    skipped: tuple = ()   # "part: error" for the part (agenda / homework) that could not be fetched


GROUPS = ("test", "quiz", "homework", "other")  # order in the digest
MAX_TEXT = 200


def group_of(category):
    """Digest group of a Librus agenda category; categories are named by each school, so by keywords."""
    c = (category or "").lower()
    if "kartk" in c:
        return "quiz"
    if any(k in c for k in ("sprawdzian", "klasow", "diagnoz", "test")):
        return "test"
    if "domow" in c:
        return "homework"
    return "other"


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _clean(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rstrip() + "…"


REFUSED = (403, 404, 405)  # the school has no such module (or not for parents)


def _error(e):
    return str(e) or type(e).__name__


async def _fetch(load, part, skipped):
    """The items of one part; a refused module is empty, another error goes to `skipped`."""
    try:
        return await load()
    except Exception as e:
        if getattr(e, "status_code", None) not in REFUSED:
            skipped.append(f"{part}: {_error(e)}")
        return []


async def _names(load):
    """A lookup that is only for display: {} when it fails."""
    try:
        return await load() or {}
    except Exception:
        return {}


async def _check(account, entry, first, last):
    name = account.name
    try:
        async with librus_grades.open_account(account.login, account.password, entry.get("session")) as librus:
            try:
                if not name:
                    name = (await librus.me()).first_name or account.login
                skipped = []
                events = [e for e in await _fetch(librus.agenda, "agenda", skipped)
                          if (d := _day(e.date)) and first <= d <= last]
                assignments = [h for h in await _fetch(librus.homework, "homework", skipped)
                               if (d := _day(h.due_date)) and first <= d <= last]
                if len(skipped) == 2:  # nothing could be fetched
                    return ChildUpcoming(name, [], "; ".join(skipped))
                items = []
                if events or assignments:  # the lookups only when there is something to show
                    subjects = await _names(librus.subjects)
                if events:
                    categories = await _names(librus.agenda_categories)
                    for e in events:
                        category = librus_grades._lookup(categories, e.category_id) or ""
                        items.append(Item(_day(e.date), group_of(category),
                                          librus_grades._lookup(subjects, e.subject_id) or "", category,
                                          _clean(e.content)))
                if assignments:
                    # a homework assignment names its lesson, not its subject (private, but the library's own lookup)
                    lessons = await _names(librus._lesson_subjects)
                    teachers = None
                    for h in assignments:
                        subject = librus_grades._lookup(subjects, librus_grades._lookup(lessons, h.lesson_id))
                        if not subject:
                            teachers = teachers if teachers is not None else await _names(librus.teachers)
                            subject = librus_grades._lookup(teachers, h.teacher_id) or ""
                        text = " – ".join(t for t in (_clean(h.topic), _clean(h.text)) if t)
                        items.append(Item(_day(h.due_date), "homework", subject, "", text))
                items.sort(key=lambda i: (i.day, GROUPS.index(i.group), i.subject))
                return ChildUpcoming(name, items, None, tuple(skipped))
            finally:
                entry["session"] = librus.session_dict()
    except Exception as e:  # one child's failure must not cost the digest or the other children
        return ChildUpcoming(name or account.login, [], _error(e))


def check_upcoming(accounts, state, today, days, failed=None):
    """[ChildUpcoming] in account order: from the day after `today` for `days` days. Sessions in `state`
    are updated (saved by the caller together with the grades). `failed` = {login: (name, error)} of
    accounts whose grades just failed: not logged into again (a wrong password would fail twice a day)."""
    first, last = today + timedelta(days=1), today + timedelta(days=days)
    failed = failed or {}

    async def one(a):
        if a.login in failed:
            name, error = failed[a.login]
            return ChildUpcoming(name, [], error)
        return await _check(a, state.setdefault(a.login, {}), first, last)

    async def run():
        return [await one(a) for a in accounts]

    return asyncio.run(run())
