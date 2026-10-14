"""The SchoolOS cloud API, as the bridge sees it (bearer token, outbound only)."""

import requests


class CloudError(Exception):
    """The cloud could not be reached or refused the request (retry later)."""


class CloudRejected(CloudError):
    """The cloud refused the request's *content* (4xx validation) — sending the
    same thing again will never succeed."""


#: Statuses that mean "this payload is bad", not "try again later".
REJECTED_STATUSES = {400, 413, 422}


class CloudClient:
    def __init__(self, base_url: str, token: str, session=None, timeout: float = 20):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method: str, path: str, *, json=None, params=None, allow_404=False):
        try:
            response = self.session.request(
                method,
                f"{self.base_url}/api/devices/{path}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CloudError(f"{method} {path}: {exc}") from exc
        if allow_404 and response.status_code == 404:
            return None
        if not response.ok:
            error = CloudRejected if response.status_code in REJECTED_STATUSES else CloudError
            raise error(f"{method} {path}: HTTP {response.status_code}")
        return response

    def push_events(self, events: list[dict]) -> dict:
        return self._request("POST", "events/", json=events).json()

    def heartbeat(self, devices: list[dict]) -> None:
        self._request("POST", "heartbeat/", json={"devices": devices})

    def fetch_commands(self) -> list[dict]:
        return self._request("GET", "commands/").json()

    def ack(self, command_id: int, status: str, result: str = "") -> None:
        self._request("POST", f"commands/{command_id}/ack/", json={"status": status, "result": result})

    def fetch_photo(self, command_id: int, osid: str | None = None) -> bytes | None:
        """An ENROLL command's photo, or (with `osid`) one RECONCILE roster member's."""
        params = {"osid": osid} if osid else None
        response = self._request("GET", f"commands/{command_id}/photo/", params=params, allow_404=True)
        return None if response is None else response.content

    def upload_capture(self, command_id: int, photo: bytes) -> None:
        """Send a CAPTURE command's freshly taken photo; the cloud stores it as
        the person's avatar."""
        try:
            response = self.session.request(
                "POST",
                f"{self.base_url}/api/devices/commands/{command_id}/capture/",
                headers={"Authorization": f"Bearer {self.token}"},
                files={"photo": ("face.jpg", photo, "image/jpeg")},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CloudError(f"POST capture: {exc}") from exc
        if not response.ok:
            error = CloudRejected if response.status_code in REJECTED_STATUSES else CloudError
            raise error(f"POST capture: HTTP {response.status_code}")

    def upload_unmatched(
        self,
        device_serial: str,
        osid: str,
        event_time: str,
        photo: bytes,
        candidate_osid: str | None = None,
        similarity: float | None = None,
    ) -> None:
        """Report a face a terminal could not match, with the shot the bridge
        took and (when the terminal found one) its closest library face."""
        data = {"device_serial": device_serial, "osid": osid, "event_time": event_time}
        if candidate_osid:
            data["candidate_osid"] = candidate_osid
        if similarity is not None:
            data["similarity"] = str(similarity)
        try:
            response = self.session.request(
                "POST",
                f"{self.base_url}/api/devices/unmatched/",
                headers={"Authorization": f"Bearer {self.token}"},
                data=data,
                files={"photo": ("face.jpg", photo, "image/jpeg")},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CloudError(f"POST unmatched: {exc}") from exc
        if not response.ok:
            error = CloudRejected if response.status_code in REJECTED_STATUSES else CloudError
            raise error(f"POST unmatched: HTTP {response.status_code}")
