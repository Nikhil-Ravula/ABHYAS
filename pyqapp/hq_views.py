"""Secure Rubix HQ ENTER callback for Abhyas.

HQ sends a short-lived RS256 JWT to ``/api/auth/hq-callback/``. The callback
verifies the fixed HQ issuer/audience and the exact RS256 algorithm, enforces a
60-second claim window, rate-limits callback attempts, atomically consumes the
JWT ``jti`` for 70 seconds, and only then establishes a Django session.
"""

import hashlib
import logging
import re
import time
from datetime import timedelta
from urllib.parse import urlencode

import jwt as pyjwt
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .hq_sync import sync_user_to_hq
from .models import HQSSONonce


logger = logging.getLogger("pyqapp.hq_sso")

HQ_SSO_ISSUER = "https://novamymentor.in"
HQ_SSO_AUDIENCE = "abhyas"
HQ_SSO_TTL_SECONDS = 60
HQ_SSO_NONCE_TTL_SECONDS = 70
HQ_SSO_RATE_LIMIT = 20
HQ_SSO_RATE_WINDOW_SECONDS = 60
_JTI_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _no_store(response):
    """Prevent callback URLs or authentication redirects being cached/reused."""
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _auth_error_response():
    location = f"{settings.LOGIN_URL}?{urlencode({'auth_error': '1'})}"
    return _no_store(HttpResponseRedirect(location))


def _get_public_key():
    """Read the HQ public key from Kavach/environment-backed settings.

    There is intentionally no embedded key or development fallback. A missing
    key must make the callback unusable rather than silently weaken
    authentication.
    """
    configured = getattr(settings, "HQ_JWT_PUBLIC_KEY", "")
    if not isinstance(configured, str):
        return ""
    key = configured.strip()
    if not key:
        return ""
    if "BEGIN PUBLIC KEY" not in key and "BEGIN RSA PUBLIC KEY" not in key:
        return ""
    return key


def _client_rate_limit_key(request):
    """Hash the server-observed peer address before using it in a cache key."""
    peer = request.META.get("REMOTE_ADDR") or "unknown"
    digest = hashlib.sha256(peer.encode("utf-8", "replace")).hexdigest()
    return f"abhyas_hq_sso_rate:{digest}"


def _allow_callback_attempt(request):
    """Allow at most 20 callback attempts per peer per minute.

    Cache availability is part of the security boundary: if the counter cannot
    be atomically maintained, reject the request rather than fail open.
    """
    try:
        limit = int(getattr(settings, "HQ_SSO_RATE_LIMIT", HQ_SSO_RATE_LIMIT))
        window = int(
            getattr(
                settings,
                "HQ_SSO_RATE_WINDOW_SECONDS",
                HQ_SSO_RATE_WINDOW_SECONDS,
            )
        )
        if limit < 1 or window < 1:
            return False

        key = _client_rate_limit_key(request)
        if cache.add(key, 1, timeout=window):
            return True
        count = cache.incr(key)
        return count <= limit
    except Exception:
        logger.error("HQ SSO rate-limit cache unavailable; rejecting callback")
        return False


def _valid_claim_window(payload):
    """Require integer iat/exp and an active window of no more than 60 seconds."""
    iat = payload.get("iat")
    exp = payload.get("exp")
    if (
        isinstance(iat, bool)
        or isinstance(exp, bool)
        or not isinstance(iat, int)
        or not isinstance(exp, int)
    ):
        return False

    now = int(time.time())
    return (
        iat <= now
        and exp > now
        and 0 < exp - iat <= HQ_SSO_TTL_SECONDS
    )


def _valid_jti(value):
    return isinstance(value, str) and bool(_JTI_PATTERN.fullmatch(value))


def _reserve_jti(jti):
    """Atomically reserve a signed JTI for the complete 70-second window."""
    now = timezone.now()
    try:
        with transaction.atomic():
            HQSSONonce.objects.filter(expires_at__lte=now).delete()
            HQSSONonce.objects.create(
                jti_digest=hashlib.sha256(jti.encode("utf-8")).hexdigest(),
                expires_at=now + timedelta(seconds=HQ_SSO_NONCE_TTL_SECONDS),
            )
    except IntegrityError:
        # Unique constraint means another worker has already consumed it.
        return "replay"
    except Exception:
        logger.error("HQ SSO replay database unavailable; rejecting callback")
        return "unavailable"
    return "reserved"


def _sanitize_username(value):
    if not isinstance(value, str):
        return ""
    sanitized = re.sub(r"[^a-zA-Z0-9._@+\-]", "", value)
    return sanitized[:150]


def _derive_username(email, subject):
    base = _sanitize_username(subject)
    if not base and isinstance(email, str) and "@" in email:
        base = _sanitize_username(email.split("@", 1)[0])
    return base or "hquser"


def _find_or_create_user(subject, email):
    """Resolve the signed HQ identity without overwriting local profile data."""
    user = User.objects.filter(email__iexact=email).first() if email else None
    if not user:
        user = User.objects.filter(username__iexact=subject).first()
    if user:
        return user

    base_username = _derive_username(email, subject)
    for suffix in range(0, 101):
        candidate = base_username if suffix == 0 else f"{base_username}{suffix}"
        if not User.objects.filter(username__iexact=candidate).exists():
            try:
                user = User(username=candidate, email=email)
                user.set_unusable_password()
                user.is_active = True
                user.save(force_insert=True)
                return user
            except IntegrityError:
                # A concurrent callback may have won the unique username race.
                user = User.objects.filter(username__iexact=candidate).first()
                if user:
                    return user
                continue

    raise ValueError("could not allocate a local username")


def _redirect_for_user(user):
    if user.is_superuser:
        target = reverse("admin_log")
    elif user.is_staff:
        target = reverse("staff_dashboard")
    else:
        target = reverse("links")
    return _no_store(HttpResponseRedirect(target))


@require_http_methods(["GET"])
def hq_callback(request):
    """Validate one HQ ENTER token and establish the local Django session."""
    if not _allow_callback_attempt(request):
        logger.warning("HQ SSO callback rate-limited")
        return _auth_error_response()

    token = (request.GET.get("hq_token") or "").strip()
    if not token or len(token) > 8192:
        logger.warning("HQ SSO callback missing or oversized token")
        return _auth_error_response()

    public_key = _get_public_key()
    if not public_key:
        logger.error("HQ SSO callback disabled: public key is not configured")
        return _auth_error_response()

    try:
        payload = pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=HQ_SSO_ISSUER,
            audience=HQ_SSO_AUDIENCE,
            leeway=0,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
                "require": ["exp", "iat", "iss", "aud", "sub", "jti"],
            },
        )
    except (pyjwt.PyJWTError, ValueError, TypeError):
        # Never log the bearer token or decoder details that might echo it.
        logger.warning("HQ SSO callback token rejected")
        return _auth_error_response()

    if not isinstance(payload, dict) or not _valid_claim_window(payload):
        logger.warning("HQ SSO callback claim window rejected")
        return _auth_error_response()

    jti = payload.get("jti")
    if not _valid_jti(jti):
        logger.warning("HQ SSO callback jti rejected")
        return _auth_error_response()

    reservation = _reserve_jti(jti)
    if reservation != "reserved":
        if reservation == "replay":
            logger.warning("HQ SSO callback replay rejected")
        else:
            logger.error("HQ SSO replay protection unavailable; rejecting callback")
        return _auth_error_response()

    subject = payload.get("sub")
    email = payload.get("email", "")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject.strip()) > 150
        or not isinstance(email, str)
        or len(email.strip()) > 254
    ):
        logger.warning("HQ SSO callback identity claims rejected")
        return _auth_error_response()

    subject = subject.strip()
    email = email.strip().lower()
    try:
        user = _find_or_create_user(subject, email)
        if not user.is_active:
            logger.warning("HQ SSO callback inactive user rejected")
            return _auth_error_response()

        user.backend = "django.contrib.auth.backends.ModelBackend"
        login(request, user)
        # Reuse Abhyas' single-device/session accounting for HQ ENTER logins.
        from .views import _record_login_session

        _record_login_session(request, user)
    except Exception:
        logger.exception("HQ SSO callback local session setup failed")
        return _auth_error_response()

    # Keep HQ's ENTER login path covered by the same best-effort mirror contract
    # as the local, Vitharn, and Aacharya authentication paths. The local
    # session is already established, so a hub outage must never change the
    # successful authentication result.
    try:
        sync_user_to_hq(user, request)
    except Exception:
        logger.exception("HQ user mirror scheduling failed after HQ ENTER")

    return _redirect_for_user(user)
