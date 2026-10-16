"""The SchoolOS cloud API, as the bridge sees it (bearer token, outbound only)."""

import re

import requests

from . import __version__
from .config import normalize_cloud_url


class CloudError(Exception):
    """The cloud could not be reached or refused the request (retry later)."""

    def __init__(self, message: str, status: int | None = None, location: str = ""):
        super().__init__(message)
        self.status = status
        self.location = location


class CloudRejected(CloudError):
    """The cloud refused the request's *content* (4xx validation) — sending the
    same thing again will never succeed."""


#: Statuses that mean "this payload is bad", not "try again later".
REJECTED_STATUSES = {400, 413, 422}


class CloudClient:
    def __init__(self, base_url: str, token: str, session=None, timeout: float = 20):
        self.base_url = normalize_cloud_url(base_url)
        self.api_base = f"{self.base_url}/api"
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method: str, path: str, *, json=None, params=None, allow_404=False):
        try:
            response = self.session.request(
                method,
                f"{self.api_base}/devices/{path}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CloudError(f"{method} {path}: {exc}") from exc
        return self._checked(method, path, response, allow_404=allow_404)

    def _checked(self, method, path, response, allow_404=False):
        if allow_404 and response.status_code == 404:
            return None
        if 300 <= response.status_code < 400:
            # Never follow: requests turns a redirected POST into a GET, which the
            # API answers with a misleading 405. Say where it wanted us instead.
            raise CloudError(
                f"{method} {path}: HTTP {response.status_code} -> {response.headers.get('Location', '')}",
                status=response.status_code,
                location=response.headers.get("Location", ""),
            )
        if not response.ok:
            error = CloudRejected if response.status_code in REJECTED_STATUSES else CloudError
            raise error(f"{method} {path}: HTTP {response.status_code}", status=response.status_code)
        return response

    def push_events(self, events: list[dict]) -> dict:
        return self._request("POST", "events/", json=events).json()

    def heartbeat(self, devices: list[dict]) -> None:
        self._request("POST", "heartbeat/", json={"devices": devices, "version": __version__})

    def fetch_update(self, platform: str) -> dict | None:
        """The signed release the cloud offers for `platform`, or None."""
        response = self._request("GET", "update/", params={"platform": platform})
        return response.json() if response.status_code == 200 else None

    def push_logs(self, lines: list[dict]) -> None:
        self._request("POST", "logs/", json=lines)

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
                f"{self.api_base}/devices/commands/{command_id}/capture/",
                headers={"Authorization": f"Bearer {self.token}"},
                files={"photo": ("face.jpg", photo, "image/jpeg")},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CloudError(f"POST capture: {exc}") from exc
        self._checked("POST", "capture", response)

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
                f"{self.api_base}/devices/unmatched/",
                headers={"Authorization": f"Bearer {self.token}"},
                data=data,
                files={"photo": ("face.jpg", photo, "image/jpeg")},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CloudError(f"POST unmatched: {exc}") from exc
        self._checked("POST", "unmatched", response)


def explain(exc: CloudError, base_url: str = "") -> tuple[str, str]:
    """(code, Uzbek hint) for a failed cloud call — one wording for the CLI
    check, the setup wizard and the logs, telling the technician what to fix."""
    status = getattr(exc, "status", None)
    if status is None:
        match = re.search(r"HTTP (\d{3})", str(exc))
        status = int(match.group(1)) if match else None
    where = base_url or "bulut manzili"
    if status in (401, 403):
        return "invalid_token", "Token yaroqsiz yoki bridge faolsiz. Terminallar sahifasidan yangi token oling."
    if status in (301, 302, 303, 307, 308):
        return "redirect", (
            f"{where} boshqa manzilga yo'naltiryapti ({exc.location or '?'}). "
            "Config'dagi cloud_url'ni shu manzilning ildiziga almashtiring."
        )
    if status in (404, 405):
        return "wrong_url", (
            f"{where} da bridge API topilmadi (HTTP {status}). "
            "cloud_url sayt ildizi bo'lsin, masalan https://tochna.uz."
        )
    if status is not None and status >= 500:
        return "server", f"Bulut serverida xato (HTTP {status}). Birozdan keyin qayta urinib ko'ring."
    if status is not None:
        return "cloud", f"Bulut xato qaytardi: {exc}"
    cause = exc.__cause__
    if isinstance(cause, requests.exceptions.SSLError):
        return "tls", "HTTPS sertifikat tekshiruvi o'tmadi. Kompyuter soati to'g'rimi? Proxy/antivirus HTTPS'ni ushlayaptimi?"
    if isinstance(cause, requests.exceptions.Timeout):
        return "timeout", "Bulut javob bermadi (timeout). Internet sekin yoki firewall bloklayapti."
    return "unreachable", "Bulutga ulanib bo'lmadi (internet / DNS / proxy?)."
