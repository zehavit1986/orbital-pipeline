"""Client for the Orbital billing usage API.

Handles the parts of the API that the vendor documents:
- short-lived OAuth tokens (30 s): refreshed before they expire, and once more on a 401;
- 429 with Retry-After, 503, and connection errors: retried with backoff;
- pagination with next_page_token.
The 31-day limit per request is the caller's job (see ingest_usage.plan_windows).
"""

import logging
import time
from datetime import date
from typing import Iterator

import requests

log = logging.getLogger(__name__)

PAGE_SIZE = 200          # the API maximum
MAX_ATTEMPTS = 8         # per request
REQUEST_TIMEOUT = 30     # seconds
TOKEN_SAFETY_MARGIN = 5  # refresh the token this many seconds before it expires


class BillingApiError(RuntimeError):
    pass


class BillingApiClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 session: requests.Session | None = None, sleep=time.sleep):
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self._sleep = sleep  # injectable so tests don't actually wait
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.request_count = 0

    # -- public ----------------------------------------------------------------------------

    def iter_usage(self, start_date: date, end_date: date) -> Iterator[dict]:
        """Yield every usage record with start_date <= usage_date <= end_date (max 31 days)."""
        params = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                  "page_size": PAGE_SIZE}
        while True:
            page = self._get("/v1/usage", params)
            yield from page["data"]
            if not page.get("next_page_token"):
                return
            params["page_token"] = page["next_page_token"]

    # -- auth ------------------------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires_at - TOKEN_SAFETY_MARGIN:
            body = self._request_with_retry(
                "POST", "/oauth/token",
                json={"client_id": self.client_id, "client_secret": self.client_secret},
            ).json()
            self._token = body["access_token"]
            self._token_expires_at = time.monotonic() + float(body["expires_in"])
            log.debug("Got a new access token, valid for %ss", body["expires_in"])
        return self._token

    # -- HTTP with retries -----------------------------------------------------------------

    def _get(self, path: str, params: dict) -> dict:
        def auth_headers():
            return {"Authorization": f"Bearer {self._access_token()}"}
        return self._request_with_retry("GET", path, params=params, headers_factory=auth_headers).json()

    def _request_with_retry(self, method: str, path: str, headers_factory=None, **kwargs) -> requests.Response:
        url = self.base_url + path
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = headers_factory() if headers_factory else {}
            try:
                self.request_count += 1
                response = self.session.request(method, url, headers=headers,
                                                timeout=REQUEST_TIMEOUT, **kwargs)
            except requests.ConnectionError as exc:
                # Typical while the API pod restarts (e.g. after its AS_OF changes).
                wait = _backoff(attempt)
                log.warning("%s %s: connection error (%s), retrying in %.0fs", method, path, exc, wait)
                self._sleep(wait)
                continue

            if response.status_code == 200:
                return response
            if response.status_code == 401 and headers_factory:
                log.info("%s %s: token rejected, getting a new one", method, path)
                self._token = None
                continue
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After", _backoff(attempt)))
                log.info("%s %s: rate limited, retrying in %.0fs", method, path, wait)
                self._sleep(wait)
                continue
            if response.status_code >= 500:
                wait = _backoff(attempt)
                log.info("%s %s: HTTP %s, retrying in %.0fs", method, path, response.status_code, wait)
                self._sleep(wait)
                continue
            # Any other 4xx is a bug on our side (bad dates, bad credentials): don't retry.
            raise BillingApiError(f"{method} {path} failed: HTTP {response.status_code} {response.text}")

        raise BillingApiError(f"{method} {path} failed after {MAX_ATTEMPTS} attempts")


def _backoff(attempt: int) -> float:
    """1, 2, 4, 8, ... seconds, capped at 30."""
    return float(min(2 ** (attempt - 1), 30))
