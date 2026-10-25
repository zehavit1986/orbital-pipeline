import unittest
from datetime import date

from orbital_pipeline.billing_api import BillingApiClient, BillingApiError
from orbital_pipeline.ingest_usage import plan_windows


class PlanWindowsTest(unittest.TestCase):
    def test_windows_cover_range_without_gaps_or_overlap(self):
        windows = plan_windows(date(2026, 1, 1), date(2026, 3, 5))
        self.assertEqual(windows[0], (date(2026, 1, 1), date(2026, 1, 31)))
        self.assertEqual(windows[1], (date(2026, 2, 1), date(2026, 3, 3)))
        self.assertEqual(windows[2], (date(2026, 3, 4), date(2026, 3, 5)))

    def test_single_day(self):
        self.assertEqual(plan_windows(date(2026, 1, 1), date(2026, 1, 1)),
                         [(date(2026, 1, 1), date(2026, 1, 1))])

    def test_empty_when_start_after_end(self):
        self.assertEqual(plan_windows(date(2026, 1, 2), date(2026, 1, 1)), [])


class FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    """Replays a scripted list of responses for GET /v1/usage; tokens always succeed."""

    def __init__(self, usage_responses):
        self.usage_responses = list(usage_responses)
        self.token_requests = 0
        self.usage_requests = []

    def request(self, method, url, headers=None, **kwargs):
        if url.endswith("/oauth/token"):
            self.token_requests += 1
            return FakeResponse(200, {"access_token": f"t{self.token_requests}", "expires_in": 30})
        self.usage_requests.append(kwargs["params"].copy())
        return self.usage_responses.pop(0)


def page(records, next_token=None):
    return FakeResponse(200, {"data": records, "next_page_token": next_token})


class BillingApiClientTest(unittest.TestCase):
    def client(self, responses):
        session = FakeSession(responses)
        self.sleeps = []
        return BillingApiClient("http://api", "id", "secret", session=session,
                                sleep=self.sleeps.append), session

    def test_follows_pagination(self):
        client, session = self.client([page([{"n": 1}], "abc"), page([{"n": 2}])])
        records = list(client.iter_usage(date(2026, 1, 1), date(2026, 1, 31)))
        self.assertEqual(records, [{"n": 1}, {"n": 2}])
        self.assertEqual(session.usage_requests[1]["page_token"], "abc")

    def test_retries_429_with_retry_after_and_503(self):
        client, _ = self.client([FakeResponse(429, headers={"Retry-After": "3"}),
                                 FakeResponse(503), page([{"n": 1}])])
        self.assertEqual(list(client.iter_usage(date(2026, 1, 1), date(2026, 1, 1))), [{"n": 1}])
        self.assertEqual(self.sleeps, [3.0, 2.0])

    def test_gets_new_token_after_401(self):
        client, session = self.client([FakeResponse(401), page([])])
        list(client.iter_usage(date(2026, 1, 1), date(2026, 1, 1)))
        self.assertEqual(session.token_requests, 2)

    def test_bad_request_is_not_retried(self):
        client, session = self.client([FakeResponse(400, {"error": "bad range"})])
        with self.assertRaises(BillingApiError):
            list(client.iter_usage(date(2026, 1, 1), date(2026, 3, 1)))
        self.assertEqual(len(session.usage_requests), 1)


if __name__ == "__main__":
    unittest.main()
