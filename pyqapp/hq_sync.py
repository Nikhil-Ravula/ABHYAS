"""
HQ Hub Mirror — POST to https://novamymentor.in/api/hq/users/sync

Called on signup/login after local user create/auth.
Never blocks caller; logs only. Secret via settings.HQ_SYNC_SECRET (or env).
Payload per .agents/rubix-it-solutions/hq-hub-contract.md:
  username, email, source_app="abhyas", external_id=str(user.id), first_name, last_name
Header: X-HQ-Sync-Secret (also supports Authorization: Bearer fallback on HQ side)

Behaviour on HQ: 201 created, 200 updated, 401 bad secret, 429 rate-limit.
"""
import logging
import os
import requests
from django.conf import settings

logger = logging.getLogger("pyqapp.hq_sync")

HQ_SOURCE_APP = "abhyas"


def _get_sync_secret():
    """Resolve secret from settings then env fallbacks."""
    sec = getattr(settings, "HQ_SYNC_SECRET", "") or ""
    if sec:
        return sec.strip()
    # fallback direct env (if settings not yet loaded or empty)
    for k in ("HQ_SYNC_SECRET", "HQ_HUB_SYNC_SECRET", "HQ_SSO_SYNC_SECRET"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


def _get_sync_url():
    return getattr(settings, "HQ_SYNC_URL", "https://novamymentor.in/api/hq/users/sync").strip()


def sync_user_to_hq(user, request=None):
    """
    Best-effort mirror user to HQ. Never raises.
    Returns (ok:bool, status_code_or_none, body_snippet)
    """
    secret = _get_sync_secret()
    url = _get_sync_url()

    if not secret:
        # In local mode without secret, HQ side allows bypass with warning — but we log and skip
        if getattr(settings, "ENVIRONMENT", "") == "local":
            logger.info("HQ sync skipped (no secret, local mode) for %s", getattr(user, "username", "?"))
            return False, None, "no_secret_local"
        logger.warning("HQ sync skipped: HQ_SYNC_SECRET not set for user %s", getattr(user, "username", "?"))
        return False, None, "no_secret"

    if not getattr(user, "username", None) and not getattr(user, "email", None):
        logger.warning("HQ sync skipped: user has no username/email id=%s", getattr(user, "id", "?"))
        return False, None, "no_identity"

    payload = {
        "username": getattr(user, "username", "") or "",
        "email": getattr(user, "email", "") or "",
        "source_app": HQ_SOURCE_APP,
        "app_key": HQ_SOURCE_APP,
        "external_id": str(getattr(user, "id", "")),
        "first_name": getattr(user, "first_name", "") or "",
        "last_name": getattr(user, "last_name", "") or "",
    }
    # Prune empty username if only email — HQ derives from email local-part
    if not payload["username"] and payload["email"]:
        # leave empty, HQ will derive
        pass

    headers = {
        "Content-Type": "application/json",
        "X-HQ-Sync-Secret": secret,
    }

    try:
        # short timeout, never block login
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
        # log redacted secret presence only
        if resp.status_code in (200, 201):
            logger.info("HQ sync ok user=%s status=%s source=%s", payload["username"] or payload["email"], resp.status_code, HQ_SOURCE_APP)
            return True, resp.status_code, resp.text[:300]
        elif resp.status_code == 401:
            logger.warning("HQ sync 401 unauthorized (bad secret) for %s url=%s", payload["username"], url)
            return False, 401, resp.text[:300]
        elif resp.status_code == 429:
            logger.warning("HQ sync 429 rate-limited for %s", payload["username"])
            return False, 429, resp.text[:300]
        else:
            logger.warning("HQ sync unexpected %s for %s: %s", resp.status_code, payload["username"], resp.text[:500])
            return False, resp.status_code, resp.text[:300]
    except requests.RequestException as e:
        logger.warning("HQ sync request failed for %s: %s", payload.get("username") or payload.get("email"), e)
        return False, None, str(e)[:300]
    except Exception as e:
        logger.exception("HQ sync unexpected error for %s: %s", payload.get("username"), e)
        return False, None, str(e)[:300]
