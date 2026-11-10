"""New grades from Librus Synergia, one account per child (unofficial API, librus-synergia):
ordinary, point and text grades and behaviour points.

LIBRUS_ACCOUNTS="Ania:1234567u:password;Jaś:7654321u:password" - a child's name (empty: the
student's first name from Librus), the Librus login and its password. What has already been
reported is kept in a JSON state file (seen grade ids and the Librus session per login, no
passwords). New grades are remembered only after the digest has been delivered (`commit`), so
a preview run (or a --send-to post, which never includes grades) doesn't use them up.
"""

import asyncio
import json
import os
import sys
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
    first: bool = True  # False: `seeded` are only new kinds of grades of a child checked before
    skipped: tuple = ()  # "kind: error" for kinds that could not be fetched this time (retried next time)


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


SESSION_KEYS = {"cookies", "logged_in_at"}  # librus_synergia.LibrusSessionData


def _clean_entry(entry):
    """Only well-formed parts of one login's state: a bad entry must not fail that child forever."""
    if not isinstance(entry, dict):
        return {}
    clean = {}
    seen = entry.get("seen")
    if isinstance(seen, list):
        clean["seen"] = [str(i) for i in seen]
    kinds = entry.get("kinds")
    if isinstance(kinds, list):
        clean["kinds"] = [str(k) for k in kinds]
    session = entry.get("session")
    if isinstance(session, dict) and set(session) <= SESSION_KEYS and isinstance(session.get("cookies", []), list):
        clean["session"] = session
    return clean


def load_state(path):
    """{login: {"seen": [grade ids], "session": {...}}}; missing file = {}, unreadable = {} with a warning
    (every account then starts with a first check)."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"warning: could not read {path} ({e}); Librus accounts start over with a first check",
              file=sys.stderr, flush=True)
        return {}
    if not isinstance(state, dict):
        print(f"warning: unexpected content in {path}; Librus accounts start over with a first check",
              file=sys.stderr, flush=True)
        return {}
    return {str(login): _clean_entry(entry) for login, entry in state.items()}


def save_state(path, state):
    """Atomically, readable by the owner only: the sessions grant access to the accounts."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".librus_state.")  # created with mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # never an empty file after a power loss
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def commit(state, seen):
    """Remember the grades of a delivered digest ({login: {"seen": ids, "kinds": kinds}} from check_grades)."""
    for login, update in seen.items():
        state.setdefault(login, {}).update(update)


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


def _lookup(table, key):
    """table[key] for ids that come as 5 or "5" (lookups are keyed by int)."""
    if key is None:
        return None
    if key in table:
        return table[key]
    text = str(key).strip()
    return table.get(int(text)) if text.isdigit() else None


def _day(add_date):
    try:
        return date.fromisoformat(str(add_date)[:10]) if add_date else None
    except ValueError:
        return None


# Kinds of grades: the library call, and the prefix of their ids in the state file (ordinary grades have
# none, as in the first version of the file, so that state keeps working).
KINDS = (("grades", "grades", ""), ("point", "point_grades", "point:"),
         ("text", "text_grades", "text:"), ("behaviour", "behaviour_grades", "behaviour:"))
MAX_TEXT = 300  # a text grade can be a whole paragraph


def _num(x):
    return f"{x:g}"


def _comments(items):
    return " ".join(" ".join(c.split()) for c in (items or []) if c and c.strip())


def _point_value(g):
    points = g.points
    if points is None:
        try:
            points = float(g.value)
        except (TypeError, ValueError):
            return str(g.value or "")
    return f"{_num(points)}/{_num(g.max_points)}" if g.max_points else _num(points)


def _shorten(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rstrip() + "…"


def _grade(kind, g, subjects, categories, teachers):
    subject = _lookup(subjects, getattr(g, "subject_id", None)) or ""
    teacher = _lookup(teachers, g.teacher_id) or ""
    day = _day(g.add_date)
    if kind == "point":
        return Grade(subject, _point_value(g), g.category or "", g.weight if g.counts_to_average and g.weight else None,
                     teacher, day, "", False, "")
    if kind == "text":
        return Grade(subject, _shorten(g.value), g.category or "", None, teacher, _day(g.date) or day, "", False, "")
    if kind == "behaviour":
        if getattr(g, "grade_id", None) is not None:  # classic scale ("bdb"), may come with no short name
            value, comment = getattr(g, "display", "") or g.short_name or g.text, _comments(g.comments)
        elif g.value is not None:  # points; the library passes Value through unconverted
            try:
                value = f"{float(g.value):+g}"
            except (TypeError, ValueError):
                value = str(g.value)
            comment = _comments([*(g.comments or []), g.text])
        else:
            value, comment = g.short_name or g.text, _comments(g.comments)
        return Grade("", value, "", None, teacher, day, "behaviour", False, comment)
    cat = _lookup(categories, g.category_id)
    return Grade(subject, str(g.value), cat.name if cat else "", cat.weight if cat and cat.count_to_average else None,
                 teacher, day, _kind(g), g.improves_id is not None, _comments(g.comments))


async def _check(account, entry):
    """(ChildGrades, {"seen": ids, "kinds": seeded kinds} or None); updates entry["session"]."""
    name = account.name
    try:
        async with open_account(account.login, account.password, entry.get("session")) as librus:
            try:
                if not name:
                    name = (await librus.me()).first_name or account.login
                known = set(entry.get("seen") or ())
                # a file from the first version has "seen" for ordinary grades only
                kinds = set(entry.get("kinds") or (["grades"] if "seen" in entry else []))
                first = "seen" not in entry
                seen, new, seeded, skipped, done = set(known), [], 0, [], set()
                for kind, method, prefix in KINDS:
                    try:
                        ids = {prefix + str(g.id): g for g in await getattr(librus, method)()}
                    except Exception as e:
                        if kind == "grades":  # without the ordinary grades there is no section
                            raise
                        # the other kinds are extras: skip this one for now, keep its state, retry next time
                        skipped.append(f"{kind}: {str(e) or type(e).__name__}")
                        done |= kinds & {kind}
                        continue
                    if kind in kinds:
                        new += [(kind, g) for key, g in ids.items() if key not in known]
                        done.add(kind)
                    else:  # first check of this kind: report nothing, remember everything
                        seeded += len(set(ids) - known)
                        # Librus answers a refused module (or disabled point grades) with []: an empty kind
                        # stays unseeded, or its whole history would come as "new" once the module answers
                        if ids or kind == "grades":
                            done.add(kind)
                    seen |= set(ids)
                result = []
                if new:  # the lookups only when there is something to show
                    subjects = await librus.subjects()
                    teachers = await librus.teachers()
                    categories = await librus.grade_categories() if any(k == "grades" for k, _ in new) else {}
                    result = [_grade(kind, g, subjects, categories, teachers) for kind, g in new]
                    result.sort(key=lambda r: (r.day or date.max, r.kind == "behaviour", r.subject))
                commit_ = {"seen": sorted(seen, key=_id_order), "kinds": sorted(done)}
                return ChildGrades(name, result, None, seeded, first, tuple(skipped)), commit_
            finally:
                entry["session"] = librus.session_dict()
    except Exception as e:  # one child's failure must not cost the digest or the other children
        return ChildGrades(name or account.login, [], str(e) or type(e).__name__), None


def _id_order(key):
    """'7' < '12' < 'behaviour:3' < 'point:5': by kind prefix, then by number."""
    prefix, _, rest = key.rpartition(":")
    return (prefix, 0, int(rest), "") if rest.isdigit() else (prefix, 1, 0, rest)


def check_grades(accounts, state):
    """([ChildGrades] in account order, {login: what to commit}). Sessions in `state` are updated."""
    async def run():
        return [await _check(a, state.setdefault(a.login, {})) for a in accounts]

    children, seen = [], {}
    for account, (child, ids) in zip(accounts, asyncio.run(run())):
        children.append(child)
        if ids is not None:
            seen[account.login] = ids
    return children, seen
