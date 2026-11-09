"""What a parent has to pay a school that runs on eduSky (e.g. a language school), per child.

Unofficial: eduSky has no public API. This uses the same private endpoints as the eduSky
student/parent panel (https://<school>.edusky.pl/studentpanel/payments) and logs in the way
that panel does (OAuth2 password grant with the panel's built-in client secret). Nothing is
stored: every run logs in again. Errors end up in Payments.error, never raised.

EDUSKY_URL=https://school.edusky.pl  EDUSKY_LOGIN=...  EDUSKY_PASSWORD=...  EDUSKY_NAME=English
"""

import json
import urllib.error
import urllib.request
from datetime import date
from typing import NamedTuple
from urllib.parse import urlencode, urlsplit

# Public: shipped in the panel's JavaScript (studentpanel/assets/index-*.js), the same for every school.
CLIENT_SECRET = "4ok2x70rlfokc8g0wws8c8kwcokw80k44sg48goc0ok4w0so0k"
CONFIG_PATH = "/core/web/external/baseconfig"
TOKEN_PATH = "/core/web/oauth/v2/token"
API_PATH = "/core/web/api/"


class Due(NamedTuple):
    child: str
    day: object       # datetime.date of the next payment, or None when nothing is scheduled
    amount: float     # to pay by `day`, arrears included and overpayment deducted (0 = nothing to pay)
    overdue: float    # arrears included in `amount`
    overpaid: float   # money paid in advance


class Payments(NamedTuple):
    name: str         # shown in the digest's section title
    items: list       # [Due], one per agreement
    error: object = None


def _get(url, timeout, token=None):
    # like the rest of szalona-beata: eduSky's front answers urllib's default User-Agent with 403
    headers = {"Accept": "application/json", "User-Agent": "szalona-beata/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
        return json.load(resp)


def _money(value):
    return float(value or 0)


def _day(value):
    return date.fromisoformat(str(value)[:10]) if value else None


def parse_agreement(agreement):
    """Due for one entry of ext/upcomingpayments. overdueValue is paid minus required so far:
    negative = arrears, positive = overpayment (the panel shows max(0, -overdueValue) as arrears)."""
    balance = _money(agreement.get("overdueValue"))
    child = " ".join(x for x in (agreement.get("studentName"), agreement.get("studentSurname")) if x)
    return Due(
        child=agreement.get("studentName") or child,
        day=_day(agreement.get("upcomingDate")),
        amount=max(0.0, _money(agreement.get("upcomingBalance"))),
        overdue=max(0.0, -balance),
        overpaid=max(0.0, balance),
    )


def fetch_payments(base_url, login, password, name="", timeout=30):
    """Payments for every child of the parent account `login` at the eduSky site `base_url`."""
    name = name or "Edusky"
    base = base_url.rstrip("/")
    try:
        config = _get(base + CONFIG_PATH, timeout)
        oauth = config.get("OAuth2") or {}
        # only the path: the token must go to the site the user configured, nowhere else
        token_path = urlsplit(oauth.get("accessTokenUri") or "").path or TOKEN_PATH
        query = urlencode({"grant_type": "password", "client_id": oauth["clientId"],
                           "client_secret": CLIENT_SECRET, "username": login, "password": password})
        token = _get(f"{base}{token_path}?{query}", timeout)["access_token"]
        api = base + API_PATH
        parent = _get(api + "common/users/auth", timeout, token)["id_userdata"]
        children = _get(api + "common/users?" + urlencode({"idUserParent": parent}), timeout, token)
        items = []
        for child in children:
            query = urlencode({"idUserParent": parent, "idUserChild": child["id"], "status[]": 5})
            agreements = _get(api + "ext/upcomingpayments?" + query, timeout, token)
            if not isinstance(agreements, list):
                raise ValueError("unexpected answer from ext/upcomingpayments")
            for agreement in agreements:
                due = parse_agreement(agreement)
                items.append(due if due.child else due._replace(child=child.get("name") or ""))
        return Payments(name, items)
    except urllib.error.HTTPError as e:  # str(e) has no URL, so never the password from the query
        return Payments(name, [], f"HTTP {e.code} {e.reason}")
    except urllib.error.URLError as e:
        return Payments(name, [], str(e.reason))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return Payments(name, [], f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
