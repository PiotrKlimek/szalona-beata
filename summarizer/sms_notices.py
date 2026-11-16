"""Short SMS to a child about their own new grades and upcoming tests, through SMSAPI.pl.

SMS_TO="Ania:500100200;Jaś:600100200" - a child's name as the digest shows it (LIBRUS_ACCOUNTS) and their
phone number; SMSAPI_TOKEN is the API token, SMSAPI_FROM an optional registered sender name. A child gets
an SMS only when there is something to say: new grades (the same as in the email) or tests and quizzes
coming up in the next LIBRUS_UPCOMING_DAYS days. Homework, other agenda entries, behaviour, errors and
the parents' sections stay in the email. Polish letters are replaced (ą -> a): with them one SMS holds
70 characters instead of 160.
"""

import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

DEFAULT_URL = "https://api.smsapi.pl"
MAX_CHARS = 459   # three SMS parts of 153 characters: a busy week must not cost a dozen
MAX_ITEM = 40     # a test's description, a text grade
WEEKDAYS = ("pon", "wt", "sr", "czw", "pt", "sob", "nd")


class Config(NamedTuple):
    token: str
    recipients: dict  # {child name: phone number with the country code}
    sender: str       # "" = SMSAPI's default sender
    url: str = DEFAULT_URL


def _phone(raw):
    digits = re.sub(r"[\s()+-]", "", raw)
    if not digits.isdigit() or not 9 <= len(digits) <= 15:
        return None
    return "48" + digits if len(digits) == 9 else digits  # 9 digits: a Polish number


def parse_recipients(value):
    """{name: phone} from 'name:phone;...'; a 9-digit number is Polish (48 is added)."""
    recipients = {}
    for number, entry in enumerate((e.strip() for e in (value or "").split(";")), 1):
        if not entry:
            continue
        name, _, raw = (x.strip() for x in entry.partition(":"))
        phone = _phone(raw)
        if not name or not phone:
            raise ValueError(f"SMS_TO entry {number} must be name:phone number")
        if any(n.casefold() == name.casefold() for n in recipients):
            raise ValueError(f"SMS_TO entry {number} repeats {name}")
        recipients[name] = phone
    return recipients


def plain(text):
    """Without Polish letters and other characters that make an SMS hold 70 characters."""
    text = str(text).replace("ł", "l").replace("Ł", "L").replace("–", "-").replace("—", "-")
    text = text.replace("„", '"').replace("”", '"').replace("…", "...")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _short(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_ITEM else text[:MAX_ITEM - 1].rstrip() + "..."


def _grade(g):
    line = f"{g.subject} {_short(g.value)}".strip()
    return f"{line} ({g.category})" if g.category else line


def _test(i):
    when = f"{WEEKDAYS[i.day.weekday()]} {i.day.strftime('%d.%m')}"
    what = ": ".join(x for x in (i.subject, _short(i.text) if i.text else "") if x)
    return " ".join(x for x in (when, "kartkowka" if i.group == "quiz" else "", what) if x)


def compose(grades, upcoming):
    """The SMS for one child (ChildGrades, ChildUpcoming, either may be None), or None with nothing new."""
    parts = []
    if grades and not grades.error:
        if lines := [_grade(g) for g in grades.grades if g.kind != "behaviour"]:
            parts.append("nowe oceny: " + "; ".join(lines))
    if upcoming and not upcoming.error:
        if lines := [_test(i) for i in upcoming.items if i.group in ("test", "quiz")]:
            parts.append("sprawdziany: " + "; ".join(lines))
    if not parts:
        return None
    name = (grades or upcoming).name
    parts[1:] = [p[0].upper() + p[1:] for p in parts[1:]]
    text = plain(f"{name}, {'. '.join(parts)}.")
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 3] + "..."


def send(config, phone, text):
    data = {"to": phone, "message": text, "format": "json", "encoding": "utf-8"}
    if config.sender:
        data["from"] = config.sender
    req = urllib.request.Request(
        f"{config.url.rstrip('/')}/sms.do", data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"Authorization": f"Bearer {config.token}", "User-Agent": "szalona-beata/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            answer = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            answer = json.load(e)
        except ValueError:
            raise RuntimeError(f"SMSAPI answered {e.code}") from None
        if "error" not in answer:
            raise RuntimeError(f"SMSAPI answered {e.code}") from None
    if "error" in answer:  # SMSAPI reports some errors with HTTP 200
        raise RuntimeError(f"SMSAPI error {answer['error']}: {answer.get('message', '')}")
    return answer


def notify(config, grades, upcoming):
    """One SMS per child in config.recipients that has news; `grades` and `upcoming` are lists by child.
    A failure is only a warning: the email has already gone out."""
    children = {}
    for child in [*grades, *upcoming]:
        children.setdefault(child.name.casefold(), [None, None])
    for child in grades:
        children[child.name.casefold()][0] = child
    for child in upcoming:
        children[child.name.casefold()][1] = child
    for name, phone in config.recipients.items():
        found = children.get(name.casefold())
        if found is None:
            print(f"warning: SMS_TO: no Librus child named {name}", file=sys.stderr, flush=True)
            continue
        text = compose(*found)
        if not text:
            continue
        shown = next(c for c in found if c).name
        try:
            send(config, phone, text)
            print(f"SMS sent to {shown}.", file=sys.stderr, flush=True)
        except Exception as e:  # never the digest, never the other children
            print(f"warning: SMS to {shown} failed: {e}", file=sys.stderr, flush=True)
