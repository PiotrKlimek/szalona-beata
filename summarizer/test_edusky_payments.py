import unittest
from datetime import date
from urllib.parse import urlencode

import edusky_payments as ep
from test_summarize import FakeServer

CLIENT_ID = "1_client"
PARENT = {"id": 7, "id_userdata": 70, "username": "Rodzic", "roles": ["ROLE_PARENT"]}
CHILDREN = [{"id": 11, "name": "Ania", "surname": "Nowak"}, {"id": 12, "name": "Jaś", "surname": "Nowak"}]


def agreement(name, upcoming_date="2026-11-10", upcoming_balance="255.00", overdue_value="85.00", number="A/2026"):
    """Shape of one entry of ext/upcomingpayments (fields the module reads, plus a few it ignores)."""
    return {"id": 1, "agreementNumber": number, "studentName": name, "studentSurname": "Nowak",
            "upcomingDate": upcoming_date, "upcomingSum": "340.00", "upcomingBalance": upcoming_balance,
            "overdueValue": overdue_value, "paymentsMadeSum": "425.00", "paymentPlanLive": []}


def token_path(login="Rodzic", password="tajne:hasło"):
    query = urlencode({"grant_type": "password", "client_id": CLIENT_ID, "client_secret": ep.CLIENT_SECRET,
                       "username": login, "password": password})
    return f"/core/web/oauth/v2/token?{query}"


def payments_path(child):
    return "/core/web/api/ext/upcomingpayments?" + urlencode(
        {"idUserParent": 70, "idUserChild": child, "status[]": 5})


class FetchPaymentsTest(unittest.TestCase):
    def server(self, overrides=None):
        responses = {
            ("GET", "/core/web/external/baseconfig"): (200, {"OAuth2": {
                "clientId": CLIENT_ID, "accessTokenUri": "https://elsewhere.example/core/web/oauth/v2/token"}}),
            ("GET", token_path()): (200, {"access_token": "TOKEN", "refresh_token": "R", "expires_in": 3600}),
            ("GET", "/core/web/api/common/users/auth"): (200, PARENT),
            ("GET", "/core/web/api/common/users?idUserParent=70"): (200, CHILDREN),
            ("GET", payments_path(11)): (200, [agreement("Ania")]),
            ("GET", payments_path(12)): (200, [agreement("Jaś", upcoming_balance="340.00", overdue_value="0.00")]),
        }
        responses.update(overrides or {})
        srv = FakeServer(responses)
        self.addCleanup(srv.close)
        return srv

    def test_amount_due_per_child(self):
        srv = self.server()
        result = ep.fetch_payments(srv.url + "/", "Rodzic", "tajne:hasło", "Angielski")
        self.assertIsNone(result.error)
        self.assertEqual(result.name, "Angielski")
        self.assertEqual(result.items, [
            ep.Due("Ania", date(2026, 11, 10), 255.0, 0.0, 85.0),
            ep.Due("Jaś", date(2026, 11, 10), 340.0, 0.0, 0.0),
        ])
        # the token goes only to the school's own site, as a bearer token
        api_calls = [h for (m, p, _), h in zip(srv.requests, srv.headers) if p.startswith("/core/web/api/")]
        self.assertEqual({h.get("Authorization") for h in api_calls}, {"Bearer TOKEN"})
        # eduSky's front rejects urllib's default User-Agent with 403
        self.assertEqual({h.get("User-Agent") for h in srv.headers}, {"szalona-beata/0.1"})

    def test_arrears_are_negative_overdue_value(self):
        srv = self.server({("GET", payments_path(11)): (200, [agreement(
            "Ania", upcoming_balance="440.00", overdue_value="-100.00")])})
        result = ep.fetch_payments(srv.url, "Rodzic", "tajne:hasło")
        self.assertEqual(result.items[0], ep.Due("Ania", date(2026, 11, 10), 440.0, 100.0, 0.0))
        self.assertEqual(result.name, "Edusky")  # default name

    def test_paid_up_agreement_and_child_without_agreements(self):
        srv = self.server({
            ("GET", payments_path(11)): (200, [agreement("Ania", upcoming_date=None, upcoming_balance="-20.00",
                                                         overdue_value="20.00")]),
            ("GET", payments_path(12)): (200, []),
        })
        result = ep.fetch_payments(srv.url, "Rodzic", "tajne:hasło")
        self.assertEqual(result.items, [ep.Due("Ania", None, 0.0, 0.0, 20.0)])

    def test_wrong_password_is_an_error_without_the_password(self):
        srv = self.server({("GET", token_path()): (400, {"error": "invalid_grant"})})
        result = ep.fetch_payments(srv.url, "Rodzic", "tajne:hasło")
        self.assertEqual(result.items, [])
        self.assertIn("400", result.error)
        self.assertNotIn("tajne", result.error)

    def test_unexpected_answer_is_an_error_not_an_exception(self):
        srv = self.server({("GET", payments_path(11)): (200, {"unexpected": True})})
        result = ep.fetch_payments(srv.url, "Rodzic", "tajne:hasło")
        self.assertEqual(result.items, [])
        self.assertTrue(result.error)

    def test_unreachable_site_is_an_error(self):
        result = ep.fetch_payments("http://127.0.0.1:9", "Rodzic", "tajne:hasło", timeout=2)
        self.assertEqual(result.items, [])
        self.assertTrue(result.error)
        self.assertNotIn("tajne", result.error)


if __name__ == "__main__":
    unittest.main()
