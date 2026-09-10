"""
HQ SSO ENTER callback for Abhyas — accepts HQ JWT (RS256).

HQ contract: POST /dashboard/sso/enter/abhyas/ on HQ (login_required) → 302 to
  https://vitharn.com/abhyas/app/api/auth/hq-callback?hq_token=<JWT RS256 60s>
Abhyas must verify:
  - RS256 signature with HQ public key (oidc_rsa.key)
  - iss=https://novamymentor.in, aud=abhyas, exp/iat (60s TTL), jti replay (cache 70s)
  - jti single-use via cache key hq_nonce_<jti> (70s)
Then find/create local User by email (iexact) then username (sub), set_unusable_password,
login, redirect to links/admin_log/staff.

GET only. Mirrors HQ dashboard/sso views 263-583 (HQ_SSO_TTL_SECONDS=60, NONCE_TTL=70).
"""
import logging
import re

import jwt as pyjwt
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.views.decorators.http import require_http_methods
from django.utils import timezone

logger = logging.getLogger("pyqapp.hq_sso")

HQ_JWT_TTL = 70  # seconds, allow slight slack over HQ 60s
HQ_NONCE_TTL = 70
HQ_JTI_CACHE_PREFIX = "hq_nonce_"
HQ_ISSUER = "https://novamymentor.in"
HQ_AUD = "abhyas"


def _get_public_key():
    key = getattr(settings, "HQ_JWT_PUBLIC_KEY", "") or ""
    if key and "BEGIN PUBLIC KEY" in key:
        return key.strip()
    # fallback hard-coded (same as settings default)
    return """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAy3WR+o9dqST5W+LEvDFJ
bm1DmMJDYHOnVPBveBTpioqKb6N1WPhjn3H7j3l3uV5uNHh/xLy00bmjGQQDHCCh
jEd5bQ7zsraVSCmFEVwu3819F5JKM5l5simBOLQE22magy8pOX/36Wmc4p5ol0Ui
qvb7fYoSFsPcetZM5UOVVQFB148yFgqne4u1kNhyFCHGHXKpAtPWkYVMk0bXVPFk
7zJxABKA5iUWFZtBPMe/9jT2UZnzwl4BPP/UpJ3e1kodWIgGuzeErbmqMHhmDl/u
0BzKq4X2/PQ5/LDyhEo8i6CoWIFG9ssyqVS8CDVnQ/E0X240wh6hISH5TcB1cRsY
hwIDAQAB
-----END PUBLIC KEY-----"""


def _sanitize_username(base):
    """Sanitize to ^[a-zA-Z0-9._@+\\-] like HQ, fallback to alphanum."""
    if not base:
        return ""
    # HQ sanitizes local-part allow list; replicate looser
    sanitized = re.sub(r'[^a-zA-Z0-9._@+\-]', '', base)
    return sanitized[:150] or "hquser"


def _derive_username(email, sub):
    """Prefer sub, fallback to email local-part sanitized."""
    if sub:
        u = _sanitize_username(sub)
        if u:
            return u
    if email and "@" in email:
        local = email.split("@")[0]
        return _sanitize_username(local) or "hquser"
    return "hquser"


@require_http_methods(["GET"])
def hq_callback(request):
    """
    GET /api/auth/hq-callback?hq_token=<jwt>
    Also accepts ?token= for compat.

    On success: login and redirect to app home (links / staff / admin_log).
    On failure: redirect to LOGIN_URL?auth_error=1 (same as vitharn_login).
    """
    token = request.GET.get("hq_token") or request.GET.get("token") or request.GET.get("hqToken") or ""
    token = token.strip()
    if not token:
        logger.warning("HQ callback missing hq_token ip=%s", request.META.get("REMOTE_ADDR", ""))
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")

    public_key = _get_public_key()

    try:
        # Use PyJWT RS256 verification
        payload = pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=HQ_ISSUER,
            audience=HQ_AUD,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
                "require": ["exp", "iat", "iss", "aud", "sub", "jti"],
            },
        )
    except pyjwt.ExpiredSignatureError as e:
        logger.warning("HQ callback expired token: %s", e)
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")
    except pyjwt.InvalidIssuerError as e:
        logger.warning("HQ callback bad issuer: %s", e)
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")
    except pyjwt.InvalidAudienceError as e:
        logger.warning("HQ callback bad audience: %s aud expected %s err=%s", token[:20], HQ_AUD, e)
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")
    except pyjwt.InvalidTokenError as e:
        logger.warning("HQ callback invalid token: %s", e)
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")
    except Exception as e:
        logger.exception("HQ callback unexpected decode error: %s", e)
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")

    # TTL sanity: HQ issues 60s, we allow up to 70s
    try:
        iat = int(payload.get("iat", 0))
        exp = int(payload.get("exp", 0))
        if exp - iat > HQ_JWT_TTL + 5:  # slight slack
            logger.warning("HQ callback TTL too large iat=%s exp=%s diff=%s", iat, exp, exp - iat)
            return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")
    except Exception:
        pass

    jti = payload.get("jti", "")
    if not jti:
        logger.warning("HQ callback missing jti")
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")

    # Replay protection: cache + single-use
    cache_key = f"{HQ_JTI_CACHE_PREFIX}{jti}"
    if cache.get(cache_key):
        logger.warning("HQ callback replay jti=%s", jti)
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")
    # mark used for 70s (HQ nonce TTL)
    try:
        cache.set(cache_key, payload.get("sub", ""), HQ_NONCE_TTL)
    except Exception:
        logger.warning("HQ callback cache set failed jti=%s", jti)

    # Extract identity
    sub = (payload.get("sub") or "").strip()
    email = (payload.get("email") or "").strip()
    role = payload.get("role") or payload.get("scope") or ""

    # Normalize email
    if email:
        email = email.lower().strip()

    # Lookup existing user: email iexact first, then username
    user = None
    if email:
        user = User.objects.filter(email__iexact=email).first()
    if not user and sub:
        user = User.objects.filter(username__iexact=sub).first()

    created = False
    if not user:
        # Derive username
        base_username = _derive_username(email, sub)
        username = base_username
        suffix = 1
        # Ensure uniqueness (case-insensitive check loop like HQ)
        while User.objects.filter(username__iexact=username).exists():
            username = f"{base_username}{suffix}"
            suffix += 1
            if suffix > 100:
                username = f"{base_username}_{jti[:6]}"
                break
        # Create
        try:
            user = User.objects.create_user(
                username=username,
                email=email or f"{username}@hq.mirror.invalid",
                password=None,
            )
            # set_unusable_password already via create_user with None? ensure
            user.set_unusable_password()
            # Try to fill first_name from sub if email missing? use sub
            # role mapping: HQ founding_engineer vs employee — store as first_name? Not needed.
            # Keep user active
            user.is_active = True
            # Optionally store role in first_name/last_name? Skip — could use profile.
            user.save()
            created = True
            logger.info("HQ callback created user username=%s email=%s jti=%s role=%s", username, email, jti, role)
        except Exception as e:
            logger.exception("HQ callback user create failed sub=%s email=%s: %s", sub, email, e)
            return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")
    else:
        # Fill missing email if blank (don't overwrite)
        if email and (not user.email or user.email.endswith("@hq.mirror.invalid")):
            user.email = email
            try:
                user.save(update_fields=["email"])
            except Exception:
                pass
        logger.info("HQ callback login existing user=%s jti=%s created=%s", user.username, jti, created)

    # Log the user in
    try:
        user.backend = "django.contrib.auth.backends.ModelBackend"
        login(request, user)
    except Exception as e:
        logger.exception("HQ callback login failed for %s: %s", user.username, e)
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?auth_error=1")

    # Mirror to HQ is not needed (HQ is source) but we could no-op

    # Redirect per role
    try:
        if user.is_superuser:
            from django.urls import reverse
            return HttpResponseRedirect(reverse("admin_log"))
        if user.is_staff:
            from django.urls import reverse
            return HttpResponseRedirect(reverse("staff_dashboard"))
        from django.urls import reverse
        return HttpResponseRedirect(reverse("links"))
    except Exception:
        # fallback to links path with prefix awareness? reverse should handle FORCE_SCRIPT_NAME
        return HttpResponseRedirect("/links/")
