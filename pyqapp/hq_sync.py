"""Best-effort user mirroring to the Rubix HQ hub.

The HQ endpoint is deliberately called from a short-lived daemon thread. A
login or registration request must not wait for a remote service, and a hub
outage must never turn into a local authentication failure. Secrets are read
from Django settings (which are populated by Kavach) or the process
environment; this module never supplies a secret fallback.
"""

import logging
import os
import threading
from urllib.parse import urlparse

import requests
from django.conf import settings


logger = logging.getLogger("pyqapp.hq_sync")

HQ_SOURCE_APP = "abhyas"
HQ_SYNC_TIMEOUT_SECONDS = 5
HQ_SYNC_PATH = "/api/hq/users/sync"


def _get_sync_secret():
    """Resolve the Kavach/environment secret without providing a fallback."""
    configured = getattr(settings, "HQ_SYNC_SECRET", "") or ""
    if isinstance(configured, str) and configured.strip():
        return configured.strip()

    for name in ("HQ_SYNC_SECRET", "HQ_HUB_SYNC_SECRET", "HQ_SSO_SYNC_SECRET"):
        value = os.environ.get(name, "")
        if value.strip():
            return value.strip()
    return ""


def _get_sync_url():
    """Return only the configured HTTPS HQ sync endpoint.

    The hostname/path allow-list prevents a mistaken or attacker-controlled
    setting from turning this background hook into an arbitrary HTTP client.
    """
    configured = getattr(
        settings,
        "HQ_SYNC_URL",
        "https://novamymentor.in/api/hq/users/sync",
    )
    if not isinstance(configured, str):
        return ""

    url = configured.strip().rstrip("/")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "novamymentor.in"
        or parsed.path != HQ_SYNC_PATH
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        return ""
    return url


def _text_value(value, max_length):
    """Convert a model field to bounded text for the outbound JSON payload."""
    if value is None:
        return ""
    return str(value).strip()[:max_length]


def _build_payload(user):
    """Build the documented HQ mirror payload from a saved Django user."""
    return {
        "username": _text_value(getattr(user, "username", ""), 150),
        "email": _text_value(getattr(user, "email", ""), 254),
        "source_app": HQ_SOURCE_APP,
        "app_key": HQ_SOURCE_APP,
        "external_id": _text_value(getattr(user, "pk", ""), 128),
        "first_name": _text_value(getattr(user, "first_name", ""), 150),
        "last_name": _text_value(getattr(user, "last_name", ""), 150),
    }


def _post_user_to_hq(payload, url, secret):
    """Perform the remote request inside the background worker only."""
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-HQ-Sync-Secret": secret,
            },
            timeout=HQ_SYNC_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        # Do not include exception text: some HTTP client exceptions can carry
        # request details. The user-facing auth flow is already complete.
        logger.warning("HQ user mirror request failed")
        return False, None, "request_failed"
    except Exception:
        logger.exception("HQ user mirror worker failed")
        return False, None, "worker_failed"

    if response.status_code in (200, 201):
        logger.info("HQ user mirror completed status=%s", response.status_code)
        return True, response.status_code, "ok"
    if response.status_code == 401:
        logger.warning("HQ user mirror rejected by hub")
    elif response.status_code == 429:
        logger.warning("HQ user mirror rate-limited by hub")
    else:
        logger.warning("HQ user mirror returned status=%s", response.status_code)
    return False, response.status_code, "remote_rejected"


def sync_user_to_hq(user, request=None):
    """Schedule a best-effort HQ mirror without delaying the caller.

    Returns ``(scheduled, status_code, result)`` for callers/tests. The
    worker itself catches all remote failures, so this function never raises
    because HQ is unavailable. ``request`` is accepted for compatibility with
    authentication views but is intentionally not sent to HQ.
    """
    del request

    secret = _get_sync_secret()
    if not secret:
        logger.warning("HQ user mirror skipped: sync secret is not configured")
        return False, None, "no_secret"

    url = _get_sync_url()
    if not url:
        logger.error("HQ user mirror skipped: endpoint configuration is invalid")
        return False, None, "invalid_endpoint"

    payload = _build_payload(user)
    if not payload["username"] and not payload["email"]:
        logger.warning("HQ user mirror skipped: user has no local identity")
        return False, None, "no_identity"

    try:
        worker = threading.Thread(
            target=_post_user_to_hq,
            args=(payload, url, secret),
            name="abhyas-hq-user-mirror",
            daemon=True,
        )
        worker.start()
    except Exception:
        # Scheduling is best-effort too; local login/signup has already won.
        logger.exception("HQ user mirror could not be scheduled")
        return False, None, "schedule_failed"

    return True, None, "scheduled"
