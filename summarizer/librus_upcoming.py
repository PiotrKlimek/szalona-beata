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


async def _lesson_subjects(librus):
    """{lesson id: subject id} - a homework assignment names its lesson, not its subject."""
    payload = await librus.client.async_get_lessons()
    lessons = payload.get("Lessons") if isinstance(payload, dict) else None
    result = {}
    for lesson in lessons if isinstance(lessons, list) else ():
        subject = lesson.get("Subject") if isinstance(lesson, dict) else None
        if isinstance(subject, dict) and lesson.get("Id") is not None and subject.get("Id") is not None:
            result[str(lesson["Id"])] = subject["Id"]
    return result


async def _check(account, entry, first, last):
    name = account.name
    try:
        async with librus_grades.open_account(account.login, account.password, entry.get("session")) as librus:
            try:
                if not name:
                    name = (await librus.me()).first_name or account.login
                skipped, events, assignments = [], [], []
                try:
                    events = [e for e in await librus.agenda() if (d := _day(e.date)) and first <= d <= last]
                except Exception as e:
                    skipped.append(f"agenda: {e or type(e).__name__}")
                try:
                    assignments = [h for h in await librus.homework()
                                   if (d := _day(h.due_date)) and first <= d <= last]
                except Exception as e:
                    skipped.append(f"homework: {e or type(e).__name__}")
                items = []
                if events or assignments:  # the lookups only when there is something to show
                    subjects = await librus.subjects()
                if events:
                    categories = await librus.agenda_categories()
                    for e in events:
                        category = librus_grades._lookup(categories, e.category_id) or ""
                        items.append(Item(_day(e.date), group_of(category),
                                          librus_grades._lookup(subjects, e.subject_id) or "", category,
                                          _clean(e.content)))
                if assignments:
                    try:
                        lessons = await _lesson_subjects(librus)
                    except Exception:  # only for the subject's name
                        lessons = {}
                    teachers = None
                    for h in assignments:
                        subject = librus_grades._lookup(subjects, lessons.get(str(h.lesson_id)))
                        if not subject:
                            teachers = teachers if teachers is not None else await librus.teachers()
                            subject = librus_grades._lookup(teachers, h.teacher_id) or ""
                        text = " – ".join(t for t in (_clean(h.topic), _clean(h.text)) if t)
                        items.append(Item(_day(h.due_date), "homework", subject, "", text))
                items.sort(key=lambda i: (i.day, GROUPS.index(i.group), i.subject))
                return ChildUpcoming(name, items, None, tuple(skipped))
            finally:
                entry["session"] = librus.session_dict()
    except Exception as e:  # one child's failure must not cost the digest or the other children
        return ChildUpcoming(name or account.login, [], str(e) or type(e).__name__)


def check_upcoming(accounts, state, today, days):
    """[ChildUpcoming] in account order: from the day after `today` for `days` days. Sessions in `state`
    are updated (saved by the caller together with the grades)."""
    first, last = today + timedelta(days=1), today + timedelta(days=days)

    async def run():
        return [await _check(a, state.setdefault(a.login, {}), first, last) for a in accounts]

    return asyncio.run(run())
