"""
Views for the PYQ (Previous Year Questions) application.

This module handles all request/response logic including:
- Authentication (login, register, logout)
- Paper and IQ uploads and downloads
- Support tickets and user management
- Admin dashboard functionality
"""

import base64
import hashlib
import io
import json
import logging
import os
import secrets
from datetime import datetime
import unicodedata
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect
from django.db.models import Q, Count, Max, F
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.models import User
from django.conf import settings
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from django.utils.html import escape
from django.utils import timezone
from django.core.cache import cache

import requests as http_requests
import urllib.parse
import jwt as pyjwt
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Image
from reportlab.lib.utils import ImageReader
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.colors import grey
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Register Unicode-capable fonts for PDF generation ──────────────────────
# Default Helvetica/Times only support Latin-1. Arial supports full Unicode
# including math symbols, superscripts (x², e^−x), etc.
try:
    _ARIAL_PATH = 'C:/Windows/Fonts/arial.ttf'
    _ARIAL_BOLD_PATH = 'C:/Windows/Fonts/arialbd.ttf'
    pdfmetrics.registerFont(TTFont('ArialUnicode', _ARIAL_PATH))
    pdfmetrics.registerFont(TTFont('ArialUnicode-Bold', _ARIAL_BOLD_PATH))
    _PDF_FONT = 'ArialUnicode'
    _PDF_FONT_BOLD = 'ArialUnicode-Bold'
except Exception:
    # Fallback to built-in fonts if Arial is not found (e.g. on Linux servers)
    _PDF_FONT = 'Helvetica'
    _PDF_FONT_BOLD = 'Helvetica-Bold'

from .models import (
    Paper, Ticket, TicketReply, ImportantQuestionEntry,
    PaperView, PaperDownload, IQView, IQDownload, SiteVisit,
    UserSession, Announcement, ActivityLog
)

# Setup logging
logger = logging.getLogger(__name__)


# ── Rate Limiting Helpers ───────────────────────────────────────────────────

def _get_rate_limit_key(username):
    """Cache key for tracking failed login attempts per username."""
    return f"login_fail_{username.lower()}"


def _is_rate_limited(username):
    """
    Returns True if this username has exceeded 5 failed login attempts
    within the last 15 minutes.
    """
    key = _get_rate_limit_key(username)
    attempts = cache.get(key, 0)
    return attempts >= 5


def _record_failed_attempt(username):
    """Increment failed attempt counter. Expires after 15 minutes."""
    key = _get_rate_limit_key(username)
    attempts = cache.get(key, 0)
    cache.set(key, attempts + 1, timeout=900)  # 15 minutes


def _reset_rate_limit(username):
    """Clear failed attempt counter on successful login."""
    cache.delete(_get_rate_limit_key(username))


# ── Helper Functions ────────────────────────────────────────────────────────

def validate_uploaded_file(uploaded_file):
    """
    Validate uploaded file for security.
    
    Args:
        uploaded_file: Django UploadedFile object
        
    Returns:
        tuple: (is_valid, error_message)
    """
    # Check file size
    max_size = getattr(settings, 'MAX_UPLOAD_SIZE', 50 * 1024 * 1024)
    if uploaded_file.size > max_size:
        return False, f"File size exceeds {max_size / 1024 / 1024:.0f}MB limit"
    
    # Check file extension
    allowed_extensions = getattr(settings, 'ALLOWED_UPLOAD_EXTENSIONS', {'pdf', 'doc', 'docx', 'txt', 'jpg', 'jpeg', 'png', 'gif'})
    filename = uploaded_file.name.lower()
    file_ext = filename.split('.')[-1] if '.' in filename else ''
    
    if file_ext not in allowed_extensions:
        return False, f"File type .{file_ext} not allowed. Allowed types: {', '.join(allowed_extensions)}"
    
    return True, None


def _get_paper_content(paper):
    """
    Get file content for a paper from remote storage (MinIO) or local disk.
    
    Tries the file URL (presigned S3/MinIO URL) first via HTTP GET.
    Falls back to reading from local disk via MEDIA_ROOT.
    
    Args:
        paper: Paper model instance
        
    Returns:
        bytes: File content or None if not found
    """
    file_url = paper.file.url

    if file_url.startswith('http'):
        try:
            resp = http_requests.get(file_url, stream=True, timeout=30)
            if resp.status_code == 200:
                return resp.content
        except http_requests.RequestException as e:
            logger.error(f"Error fetching file from storage: {e}")
            return None

    local_path = os.path.join(settings.MEDIA_ROOT, paper.file.name)
    if os.path.exists(local_path):
        try:
            with open(local_path, 'rb') as f:
                return f.read()
        except IOError as e:
            logger.error(f"Error reading local file: {e}")
            return None

    return None


def draw_page_border(canvas, doc):
    """
    Draw a border and watermark around PDF page for aesthetic purposes.
    
    Args:
        canvas: ReportLab canvas object
        doc: SimpleDocTemplate object
    """
    width, height = letter

    # Draw Watermark
    canvas.saveState()
    # Use the globally defined bold font, fallback to Helvetica-Bold if needed
    font_name = globals().get('_PDF_FONT_BOLD', 'Helvetica-Bold')
    canvas.setFont(font_name, 100)
    canvas.setFillColorRGB(0.92, 0.92, 0.92)  # Very light gray
    canvas.translate(width / 2.0, height / 2.0)
    canvas.rotate(45)
    canvas.drawCentredString(0, 0, "ABHYAS")
    canvas.restoreState()

    # Draw Border
    canvas.saveState()
    canvas.setStrokeColorRGB(0.3, 0.3, 0.3)  # Dark grey border
    canvas.setLineWidth(1.5)
    margin = 20
    canvas.rect(margin, margin, width - 2 * margin, height - 2 * margin)
    canvas.restoreState()


# ── Authentication Views ────────────────────────────────────────────────────

def index(request):
    """Entry point for Abhyas app.
    
    - Authenticated (via Vitharn): show 3-second countdown, then JS sends to dashboard.
    - Not authenticated: immediately redirect to Vitharn landing page.
    """
    if not request.user.is_authenticated:
        return HttpResponseRedirect(settings.LOGIN_URL)
    return render(request, 'pyqapp/index.html')


# ── Aacharya OIDC SSO ────────────────────────────────────────────────────────

def oidc_login(request):
    """Redirect user to Aacharya's OIDC authorize endpoint (PKCE + state)."""
    oidc = settings.AACHARYA_OIDC

    # PKCE: generate verifier + S256 challenge (Aacharya requires code_challenge)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip('=')
    state = secrets.token_urlsafe(24)
    request.session['aacharya_code_verifier'] = code_verifier
    request.session['aacharya_oauth_state'] = state

    params = {
        'client_id': oidc['CLIENT_ID'],
        'response_type': 'code',
        'redirect_uri': oidc['REDIRECT_URI'],
        'scope': oidc['SCOPE'],
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    }
    url = f"{oidc['AUTHORIZE_URL']}?{urllib.parse.urlencode(params)}"
    return redirect(url)


@require_http_methods(["GET"])
def aacharya_oidc_callback(request):
    """Handle OIDC callback from Aacharya: exchange code, fetch user, log in."""
    code = request.GET.get('code')
    error = request.GET.get('error')
    state = request.GET.get('state')

    # CSRF / state validation
    sess_state = request.session.pop('aacharya_oauth_state', None)
    if error or not code or state != sess_state:
        messages.error(request, f"Aacharya login failed: {error or 'Invalid state'}")
        return redirect('login')

    oidc = settings.AACHARYA_OIDC
    code_verifier = request.session.pop('aacharya_code_verifier', '')

    # Exchange code for token (PKCE: include code_verifier)
    token_data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': oidc['REDIRECT_URI'],
        'client_id': oidc['CLIENT_ID'],
        'client_secret': oidc['CLIENT_SECRET'],
        'code_verifier': code_verifier,
    }
    try:
        token_resp = http_requests.post(oidc['TOKEN_URL'], data=token_data, timeout=10)
        token_resp.raise_for_status()
        token_json = token_resp.json()
        access_token = token_json.get('access_token')
        if not access_token:
            messages.error(request, "Failed to get access token from Aacharya.")
            return redirect('login')
    except Exception as e:
        logger.error(f"Aacharya OIDC token exchange failed: {e}")
        messages.error(request, "Failed to authenticate with Aacharya.")
        return redirect('login')

    # Fetch user info
    try:
        userinfo_resp = http_requests.get(
            oidc['USERINFO_URL'],
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()
    except Exception as e:
        logger.error(f"Aacharya OIDC userinfo failed: {e}")
        messages.error(request, "Failed to fetch user info from Aacharya.")
        return redirect('login')

    email = userinfo.get('email', '')
    aacharya_sub = str(userinfo.get('sub', ''))
    name = userinfo.get('name', userinfo.get('preferred_username', ''))
    preferred_username = userinfo.get('preferred_username', '')
    # If the name is just the roll number (matches preferred_username), use email prefix instead
    if name == preferred_username:
        name = email.split('@')[0] if '@' in email else name
    role = userinfo.get('role', 'unknown')

    if not email:
        messages.error(request, "Aacharya account has no email address.")
        return redirect('login')

    # Find or create local user
    user = User.objects.filter(email__iexact=email).first()
    if not user:
        username = email.split('@')[0][:150]
        base_username = username
        suffix = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{suffix}"
            suffix += 1
        user = User.objects.create_user(
            username=username,
            email=email,
            password=secrets.token_urlsafe(32),
        )
        user.first_name = name.split()[0] if name else ''
        if len(name.split()) > 1:
            user.last_name = ' '.join(name.split()[1:])
        user.save()
        logger.info(f"Created new user from Aacharya SSO: {email}")

    # Log the user in
    user.backend = 'django.contrib.auth.backends.ModelBackend'
    login(request, user)
    messages.success(request, f"Welcome back, {user.username}!")

    if user.is_superuser:
        return redirect('admin_log')
    if user.is_staff:
        return redirect('staff_dashboard')
    return redirect('links')


@require_http_methods(["GET"])
def vitharn_login(request):
    """Log in via Vitharn JWT token.
    
    Decodes the JWT locally using VITHARN_JWT_SECRET to avoid making
    an HTTP call to the Vitharn API (which returns 401 for expired tokens).
    """
    token = request.GET.get('token')
    if not token:
        return HttpResponseRedirect(f'{settings.LOGIN_URL}?auth_error=1')

    # Decode the JWT locally — no network call, no expiry issue.
    # Vitharn API signs tokens with HS256 using its SECRET_KEY.
    vitharn_jwt_secret = os.environ.get(
        'VITHARN_JWT_SECRET', 'django-insecure-vitharn-default-key'
    )
    try:
        payload = pyjwt.decode(
            token,
            vitharn_jwt_secret,
            algorithms=['HS256'],
            options={'verify_exp': False}  # Accept tokens regardless of expiry
        )
        # SimpleJWT stores user_id in the token payload
        user_id = payload.get('user_id')
        if not user_id:
            logger.warning("Vitharn JWT missing user_id in payload")
            return HttpResponseRedirect(f'{settings.LOGIN_URL}?auth_error=1')

        # Check if the new token format includes email and full_name directly
        email = payload.get('email')
        full_name = payload.get('full_name', '')

        # Fallback to Vitharn API if email is missing (for older tokens)
        if not email:
            vitharn_api_url = os.environ.get('VITHARN_API_URL', 'http://vitharn_api:8000')
            try:
                response = http_requests.get(
                    f"{vitharn_api_url}/api/auth/me/",
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Host': 'vitharn.com',
                        'X-Forwarded-Host': 'vitharn.com'
                    },
                    timeout=5
                )
                response.raise_for_status()
                user_data = response.json()
                email = user_data.get('email')
                full_name = user_data.get('full_name', '')
            except Exception as api_err:
                logger.warning(f"Could not fetch user data from Vitharn API: {api_err}")
                messages.warning(request, "Vitharn token expired, using limited identity.")

        if not email:
            # Fallback: use vitharn user_id to create a stable synthetic identity.
            logger.warning(
                "Using synthetic identity for Vitharn user_id=%s (API unavailable)", user_id
            )
            email = f"vitharn_{user_id}@vitharn.internal"
            if not full_name:
                full_name = f"Vitharn User {user_id}"

        username = email.split('@')[0]
        user, created = User.objects.get_or_create(
            username=username,
            defaults={'email': email, 'first_name': full_name[:30]}
        )
        
        # Ensure name updates sync to existing Abhyas profiles
        if not created and user.first_name != full_name[:30]:
            user.first_name = full_name[:30]
            user.save(update_fields=['first_name'])
        if created:
            user.set_unusable_password()
            user.first_name = full_name
            user.save()

        # Log the user in
        user.backend = 'django.contrib.auth.backends.ModelBackend'
        login(request, user)
        logger.info("Vitharn login success for %s (created=%s)", email, created)
        if user.is_superuser:
            return redirect('admin_log')
        if user.is_staff:
            return redirect('staff_dashboard')
        return redirect('links')

    except pyjwt.DecodeError as e:
        logger.error("Vitharn JWT decode error: %s", e)
        return HttpResponseRedirect(f'{settings.LOGIN_URL}?auth_error=1')
    except pyjwt.ExpiredSignatureError:
        logger.warning("Vitharn login failed: ExpiredSignatureError")
        return HttpResponseRedirect(f'{settings.LOGIN_URL}?auth_error=1')
    except Exception as e:
        logger.error("Vitharn login unexpected error: %s", e)
        return HttpResponseRedirect(f'{settings.LOGIN_URL}?auth_error=1')

@require_http_methods(["GET", "POST"])
def login_view(request):
    """Login view — redirects to Vitharn in dev/prod, handles form locally."""
    if getattr(settings, 'ENVIRONMENT', 'development') != 'local':
        return HttpResponseRedirect(settings.LOGIN_URL)

    if request.method == 'POST':
        from django.contrib.auth import authenticate, login
        username = request.POST.get('username', '')
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            if user.is_superuser:
                return redirect('admin_log')
            if user.is_staff:
                return redirect('staff_dashboard')
            return redirect('links')
        messages.error(request, 'Invalid username or password.')

    return render(request, 'pyqapp/login.html')


@require_http_methods(["GET", "POST"])
def abhyas_logout(request):
    """Log out from Abhyas and redirect to Vitharn login."""
    logout(request)
    return HttpResponseRedirect(settings.LOGIN_URL)


def register_view(request):
    """Register view — redirects to Vitharn in dev/prod, handles form locally."""
    if getattr(settings, 'ENVIRONMENT', 'development') != 'local':
        return HttpResponseRedirect(settings.LOGIN_URL)

    if request.method == 'POST':
        # Honeypot check
        if request.POST.get('website'):
            return redirect('register')

        from django.contrib.auth.models import User
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')

        if not username or not password:
            messages.error(request, 'Username and password are required.')
        elif User.objects.filter(username=username).exists():
            messages.error(request, 'Username already taken.')
        else:
            user = User.objects.create_user(username=username, email=email, password=password)
            from django.contrib.auth import login
            login(request, user)
            return redirect('links')

    return render(request, 'pyqapp/register.html')



@never_cache
@require_http_methods(["GET", "POST"])
def dev_secret_login(request, secret):
    """Developer-only superuser backdoor login (production only).

    This is intentionally NOT linked anywhere in the UI and is disabled by
    default. It only activates when:
      1. ENVIRONMENT == 'production'
      2. DEV_LOGIN_SECRET env var is set AND matches the ``secret`` URL arg
    The secret lives only in the production container env (never in the repo),
    so inspecting the source or the live page reveals nothing usable.
    Authentication uses Django's native ``authenticate()`` and ONLY succeeds
    for users who are already ``is_superuser`` — students/SSO users are rejected.
    """
    expected = os.environ.get("DEV_LOGIN_SECRET")
    if settings.ENVIRONMENT != "production" or not expected or secret != expected:
        return HttpResponseNotFound("Not Found")

    if request.method == "POST":
        from django.contrib.auth import authenticate, login
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None and user.is_superuser:
            login(request, user)
            logger.info("Dev secret superuser login for %s", username)
            return redirect("admin_log")
        logger.warning("Dev secret login rejected for %s (not superuser or bad creds)", username)
        return HttpResponse("Forbidden", status=403)

    return HttpResponse(
        '<!doctype html><meta name="robots" content="noindex"><form method="post">'
        '<input name="username" placeholder="username" autofocus>'
        '<input name="password" type="password" placeholder="password">'
        '<button type="submit">login</button></form>'
    )


def logout_view(request):
    """Clear session record and log user out."""
    if request.user.is_authenticated:
        # ── Activity Log (exclude superusers) ─────────────────────────
        if not request.user.is_superuser:
            ActivityLog.objects.create(user=request.user, event_type='logout')

        try:
            session_record = request.user.user_session
            session_record.session_key = None
            session_record.save(update_fields=['session_key'])
            logger.info(f"User {request.user.username} logged out")
        except UserSession.DoesNotExist:
            pass
    
    logout(request)
    return HttpResponseRedirect(settings.LOGIN_URL)


def vitharn_logout(request):
    """Explicitly clear sessions when Vitharn single sign-out occurs."""
    logout(request)
    return HttpResponseRedirect(settings.LOGIN_URL)


@never_cache
@login_required
def logout_all_devices_view(request):
    """Log out the user from all devices by invalidating the active session."""
    # ── Activity Log (exclude superusers) ─────────────────────────────
    if not request.user.is_superuser:
        ActivityLog.objects.create(user=request.user, event_type='logout')

    try:
        session_record = request.user.user_session
        session_record.logout_all_devices()
        logger.warning(f"User {request.user.username} logged out from all devices")
    except UserSession.DoesNotExist:
        pass
    
    logout(request)
    messages.success(request, "You have been logged out from all devices.")
    return HttpResponseRedirect(settings.LOGIN_URL)


# ── Student Dashboard ────────────────────────────────────────────────────────

@never_cache
@login_required
def student_dashboard(request):
    """
    Student dashboard with AJAX search for papers and important questions.
    
    Handles both AJAX requests for search results and regular page loads.
    """
    # Handle AJAX requests
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        
        # IQ Search
        if request.GET.get('iq') == '1':
            unit = request.GET.get('unit', '').strip()
            iq_type = request.GET.get('type', '').strip()
            search = request.GET.get('search', '').strip()
            
            iqs = ImportantQuestionEntry.objects.all()

            if unit and unit.isdigit():
                iqs = iqs.filter(unit=int(unit))
            if iq_type:
                iqs = iqs.filter(question_type=iq_type)
            if search:
                # Search by exact subject name OR exact tag in comma-separated hashtags list
                iqs = iqs.filter(
                    Q(subject__iexact=search) |
                    Q(hashtags__iexact=search) |
                    Q(hashtags__istartswith=search + ',') |
                    Q(hashtags__iendswith=',' + search) |
                    Q(hashtags__icontains=',' + search + ',')
                )

            # CRITICAL: Order by subject first to prevent alternating subject cards
            iqs = iqs.order_by('subject', 'unit', 'question_type', 'question_number')


            # Track views
            if request.user.is_authenticated and search:
                for q in iqs:
                    IQView.objects.create(subject=q.subject, user=request.user)
            
            results = []
            for q in iqs:
                is_image = False
                if q.file:
                    ext = q.file.name.lower().split('.')[-1]
                    if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                        is_image = True

                results.append({
                    'subject': q.subject,
                    'unit': q.unit,
                    'type': q.question_type,
                    'number': q.question_number,
                    'text': escape(q.question_text),
                    'file_url': q.file.url if q.file else '',
                    'original_filename': q.original_filename,
                    'is_image': is_image,
                    'uploader': q.uploaded_by.username,
                    'date': q.uploaded_at.strftime('%d %b, %Y')
                })
            # ── Activity Log: IQ Search (exclude superusers) ──────────
            # Only log if query >= 3 chars and same query not logged in last 5s
            # (prevents duplicate entries from live-typing on every keystroke)
            if request.user.is_authenticated and search and len(search) >= 3 and not request.user.is_superuser:
                from django.utils import timezone
                import datetime as dt
                recent_cutoff = timezone.now() - dt.timedelta(seconds=5)
                already_logged = ActivityLog.objects.filter(
                    user=request.user, event_type='search_iq',
                    detail__iexact=search, created_at__gte=recent_cutoff
                ).exists()
                if not already_logged:
                    ActivityLog.objects.create(
                        user=request.user, event_type='search_iq',
                        detail=search, results_count=len(results)
                    )

            return JsonResponse(results, safe=False)

        # Paper Search
        hashtag = request.GET.get('hashtag', '').strip()
        year = request.GET.get('year', '').strip()
        paper_type = request.GET.get('type', '').strip()
        branch = request.GET.get('branch', '').strip()
        regulation = request.GET.get('regulation', '').strip()

        papers = Paper.objects.all()

        if hashtag:
            # Search by exact subject name OR exact tag in comma-separated hashtags list
            papers = papers.filter(
                Q(subject__iexact=hashtag) |
                Q(hashtags__iexact=hashtag) |
                Q(hashtags__istartswith=hashtag + ',') |
                Q(hashtags__iendswith=',' + hashtag) |
                Q(hashtags__icontains=',' + hashtag + ',')
            )
        if year and year.isdigit():
            papers = papers.filter(year=int(year))
        if paper_type:
            papers = papers.filter(paper_type=paper_type)
        if branch:
            papers = papers.filter(branch__icontains=branch)
        if regulation:
            papers = papers.filter(regulation=regulation)

        # Track views
        if request.user.is_authenticated:
            for p in papers:
                PaperView.objects.create(paper=p, user=request.user)

        results = []
        for p in papers:
            results.append({
                'id': p.id,
                'subject': p.subject,
                'year': p.year,
                'type': p.paper_type,
                'branch': p.branch,
                'regulation': p.regulation,
                'file_url': p.file.url,
                'original_filename': p.original_filename,
                'uploader': p.uploaded_by.username,
                'date': p.uploaded_at.strftime('%d %b, %Y')
            })
        # ── Activity Log: PYQ Search (exclude superusers) ─────────
        # Only log if query >= 3 chars and same query not logged in last 5s
        if request.user.is_authenticated and hashtag and len(hashtag) >= 3 and not request.user.is_superuser:
            from django.utils import timezone
            import datetime as dt
            recent_cutoff = timezone.now() - dt.timedelta(seconds=5)
            already_logged = ActivityLog.objects.filter(
                user=request.user, event_type='search_pyq',
                detail__iexact=hashtag, created_at__gte=recent_cutoff
            ).exists()
            if not already_logged:
                ActivityLog.objects.create(
                    user=request.user, event_type='search_pyq',
                    detail=hashtag, results_count=len(results)
                )

        return JsonResponse(results, safe=False)

    # Regular page load
    if request.user.is_authenticated:
        SiteVisit.objects.get_or_create(user=request.user)

    years = Paper.objects.values_list('year', flat=True).distinct().order_by('-year')
    announcements = Announcement.objects.filter(is_active=True)
    return render(request, 'pyqapp/student.html', {'years': years, 'announcements': announcements})


# ── Links Page ───────────────────────────────────────────────────────────────

@never_cache
@login_required
def links_view(request):
    """Links page — shown immediately after login."""
    return render(request, 'pyqapp/links.html')


# ── Important Questions Page ──────────────────────────────────────────────────

@never_cache
@login_required
def impq_view(request):
    """Important Questions page with AJAX search."""
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        unit = request.GET.get('unit', '').strip()
        iq_type = request.GET.get('type', '').strip()
        search = request.GET.get('search', '').strip()

        iqs = ImportantQuestionEntry.objects.all()

        if unit and unit.isdigit():
            iqs = iqs.filter(unit=int(unit))
        if iq_type:
            iqs = iqs.filter(question_type=iq_type)
        if search:
            iqs = iqs.filter(
                Q(subject__iexact=search) |
                Q(hashtags__iexact=search) |
                Q(hashtags__istartswith=search + ',') |
                Q(hashtags__iendswith=',' + search) |
                Q(hashtags__icontains=',' + search + ',')
            )

        iqs = iqs.order_by('subject', 'unit', 'question_type', 'question_number')

        if request.user.is_authenticated and search:
            for q in iqs:
                IQView.objects.create(subject=q.subject, user=request.user)

        results = []
        for q in iqs:
            is_image = False
            if q.file:
                ext = q.file.name.lower().split('.')[-1]
                if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                    is_image = True
            results.append({
                'subject': q.subject,
                'unit': q.unit,
                'type': q.question_type,
                'number': q.question_number,
                'text': escape(q.question_text),
                'file_url': q.file.url if q.file else '',
                'original_filename': q.original_filename,
                'is_image': is_image,
                'uploader': q.uploaded_by.username,
                'date': q.uploaded_at.strftime('%d %b, %Y')
            })

        if request.user.is_authenticated and search and len(search) >= 3 and not request.user.is_superuser:
            import datetime as dt
            recent_cutoff = timezone.now() - dt.timedelta(seconds=5)
            already_logged = ActivityLog.objects.filter(
                user=request.user, event_type='search_iq',
                detail__iexact=search, created_at__gte=recent_cutoff
            ).exists()
            if not already_logged:
                ActivityLog.objects.create(
                    user=request.user, event_type='search_iq',
                    detail=search, results_count=len(results)
                )

        return JsonResponse(results, safe=False)

    return render(request, 'pyqapp/impQ.html')


# ── Staff Dashboard ──────────────────────────────────────────────────────────

@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def staff_dashboard(request):
    """Staff dashboard for uploading papers and important questions."""
    if not request.user.is_staff:
        return redirect('dashboard')

    branches_list = ["CSE", "CSM", "ECE", "EEE", "MECH", "CIVIL", "IT", "AI&DS", "AI&ML"]

    if request.method == 'POST':
        
        # Upload IQ
        if 'upload_iq' in request.POST:
            subject = request.POST.get('subject', '').strip()
            hashtags = request.POST.get('hashtags', '').strip()
            selected_branches = ",".join(request.POST.getlist('branch'))
            regulation = request.POST.get('regulation', 'R22')
            unit = request.POST.get('unit', '')
            q_type = request.POST.get('type', '')
            
            success_count = 0
            i = 1
            while True:
                q_text_key = f'question_{i}'
                q_file_key = f'file_{i}'
                
                if q_text_key not in request.POST and q_file_key not in request.FILES:
                    break
                
                q_text = request.POST.get(q_text_key, '').strip()
                q_file = request.FILES.get(q_file_key)
                
                if q_file:
                    is_valid, error_msg = validate_uploaded_file(q_file)
                    if not is_valid:
                        messages.error(request, f"Question {i}: {error_msg}")
                        logger.warning(f"Invalid file upload by {request.user.username}: {error_msg}")
                        i += 1
                        continue
                
                if q_text or q_file:
                    ImportantQuestionEntry.objects.create(
                        subject=subject,
                        hashtags=hashtags,
                        branch=selected_branches,
                        regulation=regulation,
                        unit=int(unit) if unit.isdigit() else 1,
                        question_type=q_type,
                        question_number=i,
                        question_text=q_text,
                        file=q_file,
                        original_filename=q_file.name if q_file else '',
                        uploaded_by=request.user
                    )
                    success_count += 1
                i += 1
            
            if success_count > 0:
                messages.success(request, f"{success_count} Important Questions uploaded successfully!")
                logger.info(f"User {request.user.username} uploaded {success_count} IQs")
            return redirect('staff_dashboard')
        
        # Upload Paper
        else:
            subject = request.POST.get('subject', '').strip()
            year = request.POST.get('year', '')
            p_type = request.POST.get('type', '')
            hashtags = request.POST.get('hashtags', '').strip()
            selected_branches = ",".join(request.POST.getlist('branch'))
            regulation = request.POST.get('regulation', 'R22')
            paper_file = request.FILES.get('paper_file')

            if paper_file:
                is_valid, error_msg = validate_uploaded_file(paper_file)
                if not is_valid:
                    messages.error(request, f"Upload failed: {error_msg}")
                    logger.warning(f"Invalid paper upload by {request.user.username}: {error_msg}")
                else:
                    Paper.objects.create(
                        subject=subject,
                        year=int(year) if year.isdigit() else 2024,
                        paper_type=p_type,
                        branch=selected_branches,
                        regulation=regulation,
                        hashtags=hashtags,
                        file=paper_file,
                        original_filename=paper_file.name,
                        uploaded_by=request.user
                    )
                    messages.success(request, "Paper uploaded successfully!")
                    logger.info(f"User {request.user.username} uploaded paper: {subject}")
            
            return redirect('staff_dashboard')

    papers = Paper.objects.filter(uploaded_by=request.user).order_by('-uploaded_at')
    my_iqs = ImportantQuestionEntry.objects.filter(uploaded_by=request.user).values(
        'subject', 'regulation', 'unit', 'question_type'
    ).annotate(
        q_count=Count('id'),
        latest_upload=Max('uploaded_at')
    ).order_by('-latest_upload')

    # Full IQ entries per group (for edit modals) — keyed by "subject|unit|type"
    iq_entries_map = {}
    for iq in ImportantQuestionEntry.objects.filter(uploaded_by=request.user).order_by('unit', 'question_type', 'question_number'):
        key = f"{iq.subject}|{iq.unit}|{iq.question_type}"
        if key not in iq_entries_map:
            iq_entries_map[key] = []
        iq_entries_map[key].append({
            'number': iq.question_number,
            'text': iq.question_text,
            'file_url': iq.file.url if iq.file else '',
            'original_filename': iq.original_filename,
            'branch': iq.branch,
            'hashtags': iq.hashtags,
        })

    context = {
        'papers': papers,
        'my_iqs': my_iqs,
        'iq_entries_map_json': json.dumps(iq_entries_map),
        'branches': branches_list
    }
    return render(request, 'pyqapp/staff.html', context)



# ── Support & Profile ────────────────────────────────────────────────────────

@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def support_view(request):
    """Support ticket management."""
    if request.method == 'POST':
        subject = request.POST.get('subject', '').strip()
        description = request.POST.get('description', '').strip()
        
        if subject and description:
            Ticket.objects.create(
                student=request.user,
                subject=subject,
                description=description
            )
            messages.success(request, "Ticket submitted successfully!")
            logger.info(f"Support ticket created by {request.user.username}")
            return redirect('support')

    # Students see only their tickets; staff see all
    if request.user.is_staff:
        tickets = Ticket.objects.all().order_by('-created_at')
    else:
        tickets = Ticket.objects.filter(student=request.user).order_by('-created_at')

    tickets = tickets.prefetch_related('replies', 'replies__author')
    return render(request, 'pyqapp/support.html', {'tickets': tickets})


@never_cache
@login_required
def profile_view(request):
    """Display user profile with session activity info."""
    session_info = None
    try:
        session_record = request.user.user_session
        session_info = {
            'login_count': session_record.login_count,
            'logged_in_at': session_record.logged_in_at,
            'last_seen': session_record.last_seen,
        }
    except UserSession.DoesNotExist:
        pass

    return render(request, 'pyqapp/profile.html', {'session_info': session_info})


# ── Admin Dashboard ──────────────────────────────────────────────────────────

@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def admin_log_view(request):
    """Admin dashboard for system management and monitoring."""
    if not request.user.is_superuser:
        return redirect('dashboard')

    branches_list = ["CSE", "CSM", "ECE", "EEE", "MECH", "CIVIL", "IT", "AI&DS", "AI&ML"]

    # Create staff
    if request.method == 'POST' and 'create_staff' in request.POST:
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')

        if not all([username, email, password]):
            messages.error(request, "All fields are required")
        elif User.objects.filter(username=username).exists():
            messages.error(request, "Username already taken")
        else:
            try:
                User.objects.create_user(username=username, email=email, password=password, is_staff=True)
                messages.success(request, f"Staff account '{username}' created successfully!")
                logger.info(f"Admin {request.user.username} created staff account: {username}")
            except Exception as e:
                messages.error(request, "Error creating staff account")
                logger.error(f"Error creating staff account: {e}")
        return redirect('admin_log')

    # Upload paper
    if request.method == 'POST' and 'upload_paper' in request.POST:
        subject = request.POST.get('subject', '').strip()
        year = request.POST.get('year', '')
        p_type = request.POST.get('type', '')
        hashtags = request.POST.get('hashtags', '').strip()
        selected_branches = ",".join(request.POST.getlist('branch'))
        regulation = request.POST.get('regulation', 'R22')
        paper_file = request.FILES.get('paper_file')

        if paper_file:
            is_valid, error_msg = validate_uploaded_file(paper_file)
            if not is_valid:
                messages.error(request, f"Upload failed: {error_msg}")
            else:
                Paper.objects.create(
                    subject=subject,
                    year=int(year) if year.isdigit() else 2024,
                    paper_type=p_type,
                    branch=selected_branches,
                    regulation=regulation,
                    hashtags=hashtags,
                    file=paper_file,
                    original_filename=paper_file.name,
                    uploaded_by=request.user
                )
                messages.success(request, "Paper uploaded successfully!")
        return redirect('admin_log')

    # Upload IQ
    if request.method == 'POST' and 'upload_iq' in request.POST:
        subject = request.POST.get('subject', '').strip()
        hashtags = request.POST.get('hashtags', '').strip()
        selected_branches = ",".join(request.POST.getlist('branch'))
        regulation = request.POST.get('regulation', 'R22')
        unit = request.POST.get('unit', '')
        q_type = request.POST.get('type', '')
        
        success_count = 0
        i = 1
        while True:
            q_text_key = f'question_{i}'
            q_file_key = f'file_{i}'
            
            if q_text_key not in request.POST and q_file_key not in request.FILES:
                break
            
            q_text = request.POST.get(q_text_key, '').strip()
            q_file = request.FILES.get(q_file_key)
            
            if q_file:
                is_valid, error_msg = validate_uploaded_file(q_file)
                if not is_valid:
                    messages.error(request, f"Question {i}: {error_msg}")
                    i += 1
                    continue
            
            if q_text or q_file:
                ImportantQuestionEntry.objects.create(
                    subject=subject,
                    hashtags=hashtags,
                    branch=selected_branches,
                    regulation=regulation,
                    unit=int(unit) if unit.isdigit() else 1,
                    question_type=q_type,
                    question_number=i,
                    question_text=q_text,
                    file=q_file,
                    original_filename=q_file.name if q_file else '',
                    uploaded_by=request.user
                )
                success_count += 1
            i += 1
        
        if success_count > 0:
            messages.success(request, f"{success_count} Important Questions uploaded successfully!")
        return redirect('admin_log')

    # Create Announcement
    if request.method == 'POST' and request.POST.get('action') == 'create_announcement':
        message = request.POST.get('message', '').strip()
        if message:
            Announcement.objects.create(message=message, posted_by=request.user)
            messages.success(request, 'Announcement posted successfully!')
        else:
            messages.error(request, 'Announcement message cannot be empty.')
        return redirect('admin_log')

    # Edit Announcement
    if request.method == 'POST' and request.POST.get('action') == 'edit_announcement':
        ann_id = request.POST.get('announcement_id', '')
        message = request.POST.get('message', '').strip()
        if ann_id and message:
            ann = get_object_or_404(Announcement, id=ann_id)
            ann.message = message
            ann.save()
            messages.success(request, 'Announcement updated successfully!')
        else:
            messages.error(request, 'Invalid edit request.')
        return redirect('admin_log')

    # Delete Announcement
    if request.method == 'POST' and request.POST.get('action') == 'delete_announcement':
        ann_id = request.POST.get('announcement_id', '')
        if ann_id:
            get_object_or_404(Announcement, id=ann_id).delete()
            messages.success(request, 'Announcement deleted.')
        return redirect('admin_log')

    # Fetch dashboard data
    staff_users = User.objects.filter(is_staff=True).order_by('-date_joined').select_related('user_session')
    all_users = User.objects.filter(is_staff=False, is_superuser=False).order_by('-date_joined').select_related('user_session')
    tickets = Ticket.objects.all().order_by('-created_at').prefetch_related('replies', 'replies__author', 'student')
    
    all_papers = Paper.objects.all().annotate(
        total_views=Count('views', distinct=True),
        total_downloads=Count('downloads', distinct=True)
    ).order_by('-uploaded_at')

    iq_subjects = ImportantQuestionEntry.objects.values('subject').annotate(
        total_questions=Count('id')
    ).order_by('subject')

    iq_stats = []
    for iq in iq_subjects:
        subject = iq['subject']
        iq_stats.append({
            'subject': subject,
            'total_questions': iq['total_questions'],
            'total_views': IQView.objects.filter(subject=subject).count(),
            'total_downloads': IQDownload.objects.filter(subject=subject).count(),
        })

    unique_visitors = SiteVisit.objects.count()

    my_iqs = ImportantQuestionEntry.objects.filter(uploaded_by=request.user).values(
        'subject', 'regulation', 'unit', 'question_type'
    ).annotate(
        q_count=Count('id'),
        latest_upload=Max('uploaded_at')
    ).order_by('-latest_upload')

    # Admin's own uploads (for My Uploads tab)
    papers = Paper.objects.filter(uploaded_by=request.user).order_by('-uploaded_at')

    # Full IQ entries per group (for edit modals) — keyed by (subject, unit, type)
    iq_entries_map = {}
    for iq in ImportantQuestionEntry.objects.filter(uploaded_by=request.user).order_by('unit', 'question_type', 'question_number'):
        key = f"{iq.subject}|{iq.unit}|{iq.question_type}"
        if key not in iq_entries_map:
            iq_entries_map[key] = []
        iq_entries_map[key].append({
            'number': iq.question_number,
            'text': iq.question_text,
            'file_url': iq.file.url if iq.file else '',
            'original_filename': iq.original_filename,
            'branch': iq.branch,
            'hashtags': iq.hashtags,
        })

    announcements = Announcement.objects.all()

    context = {
        'staff_users': staff_users,
        'all_users': all_users,
        'tickets': tickets,
        'papers': papers,
        'all_papers': all_papers,
        'iq_stats': iq_stats,
        'my_iqs': my_iqs,
        'iq_entries_map_json': json.dumps(iq_entries_map),
        'branches': branches_list,
        'unique_visitors': unique_visitors,
        'announcements': announcements,
    }
    return render(request, 'pyqapp/admin_log.html', context)


# ── File Operations ──────────────────────────────────────────────────────────

@login_required
def view_paper(request, paper_id):
    """Serve a paper for inline viewing in the browser."""
    paper = get_object_or_404(Paper, id=paper_id)

    if request.user.is_authenticated:
        PaperView.objects.create(paper=paper, user=request.user)

    content = _get_paper_content(paper)
    if content is None:
        messages.error(request, "File not found or inaccessible")
        return redirect('dashboard')

    response = HttpResponse(content, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{paper.original_filename}"'
    return response


@login_required
def download_paper(request, paper_id):
    """Download a paper file."""
    paper = get_object_or_404(Paper, id=paper_id)

    if request.user.is_authenticated:
        PaperDownload.objects.create(paper=paper, user=request.user)

    content = _get_paper_content(paper)
    if content is None:
        messages.error(request, "File not found or inaccessible")
        return redirect('dashboard')

    response = HttpResponse(content, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{paper.original_filename}"'
    return response


@login_required
def download_iq_pdf(request):
    """Generate and download a PDF with important questions."""
    search = request.GET.get('search', '').strip()
    unit = request.GET.get('unit', '').strip()
    iq_type = request.GET.get('type', '').strip()
    
    if not search:
        return HttpResponse("Search term is required.", status=400)

    # Search by exact subject name OR exact tag in comma-separated hashtags list
    # Restrict strictly to a single subject if multiple matches occur
    matched_subject = ImportantQuestionEntry.objects.filter(
        Q(subject__iexact=search) |
        Q(hashtags__iexact=search) |
        Q(hashtags__istartswith=search + ',') |
        Q(hashtags__iendswith=',' + search) |
        Q(hashtags__icontains=',' + search + ',')
    ).values_list('subject', flat=True).first()

    if matched_subject:
        iqs = ImportantQuestionEntry.objects.filter(subject=matched_subject)
    else:
        iqs = ImportantQuestionEntry.objects.none()

    if unit and unit.isdigit():
        iqs = iqs.filter(unit=int(unit))
    if iq_type:
        iqs = iqs.filter(question_type=iq_type)

    iqs = iqs.order_by('question_number')

    if not iqs.exists():
        return HttpResponse("No questions found.", status=404)


    # Track downloads
    if request.user.is_authenticated:
        for q in iqs:
            IQDownload.objects.create(subject=q.subject, user=request.user)

    subject_name = search
    filename = f"{subject_name}_unit{unit}_{iq_type}.pdf".replace(" ", "_")
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50
    )
    styles = getSampleStyleSheet()
    story = []

    # Use Unicode-capable font styles so math symbols (x², e^−x, etc.) render correctly
    title_style = ParagraphStyle(
        'UniTitle',
        parent=styles['Title'],
        fontName=_PDF_FONT_BOLD,
    )
    normal_style = ParagraphStyle(
        'UniNormal',
        parent=styles['Normal'],
        fontName=_PDF_FONT,
        fontSize=11,
        leading=16,
        spaceAfter=4,
    )
    q_label_style = ParagraphStyle(
        'QLabel',
        parent=styles['Normal'],
        fontName=_PDF_FONT_BOLD,
        fontSize=11,
        leading=16,
    )

    story.append(Paragraph(f"Important Questions: {escape(subject_name)}", title_style))
    story.append(Paragraph(
        f"Unit: {unit if unit else 'All'} | Type: {iq_type if iq_type else 'All'}",
        normal_style
    ))
    story.append(Spacer(1, 12))

    for q in iqs:
        # Determine fallback text if no question text is provided
        if q.question_text:
            text = q.question_text
        elif q.file:
            ext = q.file.name.lower().split('.')[-1]
            if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                text = ""  # No text needed, image will be rendered below
            else:
                text = "(See attached file)"
        else:
            text = ""
            
        # Normalize the text to convert mathematical italic characters (like 𝑖, 𝑥) to standard ASCII
        text = unicodedata.normalize('NFKC', text)
        
        # Render question number in bold + question text in unicode-safe font
        if text:
            q_paragraph = Paragraph(f"<font name='{_PDF_FONT_BOLD}'>Q{q.question_number}:</font>  {escape(text)}", normal_style)
        else:
            q_paragraph = Paragraph(f"<font name='{_PDF_FONT_BOLD}'>Q{q.question_number}:</font>", normal_style)
        story.append(q_paragraph)
        
        # If there's an image file attached, embed it in the PDF
        if q.file:
            ext = q.file.name.lower().split('.')[-1]
            if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                try:
                    img_bytes = _get_paper_content(q)
                    if img_bytes:
                        img_data = io.BytesIO(img_bytes)
                        img_reader = ImageReader(img_data)
                        iw, ih = img_reader.getSize()
                        
                        max_width = 400
                        if iw > max_width:
                            aspect = ih / float(iw)
                            img_obj = Image(img_data, width=max_width, height=(max_width * aspect))
                        else:
                            img_obj = Image(img_data, width=iw, height=ih)
                        
                        story.append(Spacer(1, 4))
                        story.append(img_obj)
                except Exception as e:
                    logger.error(f"Error embedding image for question {q.id}: {e}")

        story.append(Spacer(1, 6))

    # Add notes section
    story.append(Spacer(1, 30))
    story.append(HRFlowable(width="100%", thickness=1, color=grey))
    story.append(Spacer(1, 10))

    note_style = ParagraphStyle(
        'NoteStyle',
        parent=styles['Normal'],
        fontName=_PDF_FONT,
        fontSize=8,
        leading=11,
        spaceAfter=8,
        textColor=grey,
    )

    story.append(Paragraph(
        f"<font name='{_PDF_FONT_BOLD}'>Note 1:</font> The probability of these Important Questions appearing in the "
        "examination is approximately 50%. These questions are prepared based on previous "
        "question papers and reference books.",
        note_style
    ))

    story.append(Paragraph(
        f"<font name='{_PDF_FONT_BOLD}'>Note 2:</font> For previous year question papers, visit the website.",
        note_style
    ))

    story.append(Paragraph(
        f"<font name='{_PDF_FONT_BOLD}'>Note 3:</font> Some questions in this PDF may not belong to your regulation. "
        "Please verify with your official syllabus copy.",
        note_style
    ))

    # Add footer
    story.append(Spacer(1, 20))
    atb_style = ParagraphStyle(
        'ATBStyle',
        parent=styles['Normal'],
        fontSize=14,
        leading=18,
        alignment=TA_CENTER,
        fontName=_PDF_FONT_BOLD,
    )
    story.append(Paragraph("ALL THE BEST", atb_style))

    doc.build(story, onFirstPage=draw_page_border, onLaterPages=draw_page_border)
    
    pdf_content = buffer.getvalue()
    buffer.close()
    
    response = HttpResponse(pdf_content, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ── Delete Operations ────────────────────────────────────────────────────────

@login_required
@require_http_methods(["POST"])
def delete_paper(request, paper_id):
    """Delete a paper (only owner or superuser can delete)."""
    paper = get_object_or_404(Paper, id=paper_id)
    
    if request.user.is_superuser or paper.uploaded_by == request.user:
        subject = paper.subject
        paper.delete()
        messages.success(request, f"Paper '{subject}' deleted successfully.")
        logger.info(f"User {request.user.username} deleted paper: {subject}")
    else:
        messages.error(request, "You do not have permission to delete this paper.")
        logger.warning(f"Unauthorized delete attempt by {request.user.username}")
    
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))


@login_required
@require_http_methods(["POST"])
def delete_iq(request):
    """Delete a set of important questions (only owner or superuser can delete)."""
    subject = request.POST.get('subject', '').strip()
    unit = request.POST.get('unit', '').strip()
    q_type = request.POST.get('type', '').strip()
    
    iqs = ImportantQuestionEntry.objects.filter(subject=subject, unit=int(unit) if unit.isdigit() else 1, question_type=q_type)
    
    if iqs.exists():
        first_iq = iqs.first()
        if request.user.is_superuser or first_iq.uploaded_by == request.user:
            count = iqs.count()
            iqs.delete()
            messages.success(request, f"Deleted {count} Important Questions for '{subject}' (Unit {unit}).")
            logger.info(f"User {request.user.username} deleted {count} IQs")
        else:
            messages.error(request, "You do not have permission to delete these questions.")
            logger.warning(f"Unauthorized IQ delete attempt by {request.user.username}")
    else:
        messages.error(request, "Important questions not found.")
    
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))


# ── Edit Operations ──────────────────────────────────────────────────────────

@login_required
@require_http_methods(["POST"])
def edit_paper(request, paper_id):
    """Edit metadata of a PYQ paper (only owner or superuser)."""
    paper = get_object_or_404(Paper, id=paper_id)

    if not (request.user.is_superuser or paper.uploaded_by == request.user):
        messages.error(request, "You do not have permission to edit this paper.")
        logger.warning(f"Unauthorized paper edit attempt by {request.user.username}")
        return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

    subject = request.POST.get('subject', '').strip()
    year = request.POST.get('year', '').strip()
    p_type = request.POST.get('type', '').strip()
    regulation = request.POST.get('regulation', paper.regulation)
    hashtags = request.POST.get('hashtags', '').strip()
    selected_branches = ",".join(request.POST.getlist('branch'))
    new_file = request.FILES.get('paper_file')

    paper.subject = subject or paper.subject
    paper.year = int(year) if year.isdigit() else paper.year
    paper.paper_type = p_type or paper.paper_type
    paper.regulation = regulation
    paper.hashtags = hashtags
    if selected_branches:
        paper.branch = selected_branches

    if new_file:
        is_valid, error_msg = validate_uploaded_file(new_file)
        if not is_valid:
            messages.error(request, f"File update failed: {error_msg}")
            return redirect(request.META.get('HTTP_REFERER', 'dashboard'))
        paper.file = new_file
        paper.original_filename = new_file.name

    paper.save()
    messages.success(request, f"Paper '{paper.subject}' updated successfully.")
    logger.info(f"User {request.user.username} edited paper ID {paper_id}")
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))


@login_required
@require_http_methods(["POST"])
def edit_iq(request):
    """Edit metadata and question texts of an IQ group (only owner or superuser)."""
    orig_subject = request.POST.get('orig_subject', '').strip()
    orig_unit = request.POST.get('orig_unit', '').strip()
    orig_type = request.POST.get('orig_type', '').strip()

    iqs = ImportantQuestionEntry.objects.filter(
        subject=orig_subject,
        unit=int(orig_unit) if orig_unit.isdigit() else 1,
        question_type=orig_type,
    )

    if not iqs.exists():
        messages.error(request, "Important questions not found.")
        return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

    first_iq = iqs.first()
    if not (request.user.is_superuser or first_iq.uploaded_by == request.user):
        messages.error(request, "You do not have permission to edit these questions.")
        logger.warning(f"Unauthorized IQ edit attempt by {request.user.username}")
        return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

    # Update group-level metadata on every entry
    new_subject = request.POST.get('subject', orig_subject).strip()
    new_hashtags = request.POST.get('hashtags', '').strip()
    new_branches = ",".join(request.POST.getlist('branch'))
    new_regulation = request.POST.get('regulation', first_iq.regulation)
    new_unit = request.POST.get('unit', orig_unit).strip()
    new_type = request.POST.get('type', orig_type).strip()

    for iq in iqs:
        iq.subject = new_subject
        iq.hashtags = new_hashtags
        if new_branches:
            iq.branch = new_branches
        iq.regulation = new_regulation
        if new_unit.isdigit():
            iq.unit = int(new_unit)
        iq.question_type = new_type

        # Update individual question text if provided
        q_text = request.POST.get(f'question_{iq.question_number}', '').strip()
        iq.question_text = q_text

        # Replace file if a new one was uploaded for this specific question
        new_file = request.FILES.get(f'file_{iq.question_number}')
        if new_file:
            is_valid, error_msg = validate_uploaded_file(new_file)
            if is_valid:
                iq.file = new_file
                iq.original_filename = new_file.name

        iq.save()

    messages.success(request, f"Important Questions for '{new_subject}' updated successfully.")
    logger.info(f"User {request.user.username} edited IQ group: {orig_subject} Unit {orig_unit} {orig_type}")
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))


@login_required
@require_http_methods(["POST"])
def reply_ticket_view(request, ticket_id):
    """Reply to a support ticket (admin only)."""
    if not request.user.is_superuser:
        return redirect('dashboard')

    ticket = get_object_or_404(Ticket, id=ticket_id)

    message = request.POST.get('message', '').strip()
    new_status = request.POST.get('status', 'pending')

    if message:
        TicketReply.objects.create(
            ticket=ticket,
            author=request.user,
            message=message
        )
        ticket.status = new_status
        ticket.save()
        messages.success(request, "Reply sent successfully!")
        logger.info(f"Admin {request.user.username} replied to ticket #{ticket_id}")
    else:
        messages.error(request, "Reply message cannot be empty")

    return redirect('admin_log')


# ── Activity History PDF Download ────────────────────────────────────────────

@never_cache
@login_required
def download_search_history_pdf(request):
    """
    Generate a PDF report of all user activity (login, logout, searches)
    for non-superuser users. After download, all ActivityLog entries are
    deleted so each PDF is a fresh snapshot.
    """
    if not request.user.is_superuser:
        return redirect('dashboard')

    logs = ActivityLog.objects.all().order_by('created_at')

    # ── Build the PDF ─────────────────────────────────────────────────────
    import datetime as dt
    ist_tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = timezone.now().astimezone(ist_tz)
    filename = f"abhyas_searchhistory_{now.strftime('%d-%m-%Y_%H-%M')}.pdf"

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=40, rightMargin=40, topMargin=50, bottomMargin=50
    )
    styles = getSampleStyleSheet()
    story = []

    # Title style
    title_style = ParagraphStyle(
        'ActivityTitle',
        parent=styles['Title'],
        fontName=_PDF_FONT_BOLD,
        fontSize=18,
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        'ActivitySubtitle',
        parent=styles['Normal'],
        fontName=_PDF_FONT,
        fontSize=9,
        textColor=grey,
        alignment=TA_CENTER,
        spaceAfter=20,
    )
    from reportlab.lib.colors import HexColor, white
    normal_style = ParagraphStyle(
        'ActivityNormal',
        parent=styles['Normal'],
        fontName=_PDF_FONT,
        fontSize=9,
        leading=13,
        textColor=HexColor('#e0e0e0'),
    )
    header_style = ParagraphStyle(
        'ActivityHeader',
        parent=styles['Normal'],
        fontName=_PDF_FONT_BOLD,
        fontSize=9,
        leading=13,
        textColor=white,
    )

    # Title
    story.append(Paragraph('ABHYAS — Activity Report', title_style))
    story.append(Paragraph(
        f"Generated on {now.strftime('%d %b %Y at %I:%M %p')}",
        subtitle_style
    ))
    story.append(HRFlowable(
        width='100%', thickness=1, color=grey,
        spaceAfter=14, spaceBefore=4
    ))

    if not logs.exists():
        empty_style = ParagraphStyle(
            'ActivityEmpty',
            parent=styles['Normal'],
            fontName=_PDF_FONT,
            fontSize=12,
            alignment=TA_CENTER,
            spaceAfter=20,
            spaceBefore=40,
        )
        story.append(Paragraph(
            'No activity recorded since last download.',
            empty_style
        ))
    else:
        # Build table data
        from reportlab.platypus import Table, TableStyle
        from reportlab.lib import colors

        table_data = [[
            Paragraph('#', header_style),
            Paragraph('User', header_style),
            Paragraph('Event', header_style),
            Paragraph('Details', header_style),
            Paragraph('Date & Time', header_style),
        ]]

        for idx, log in enumerate(logs, 1):
            # Determine event display
            if log.event_type == 'login':
                event_str = 'Login'
                detail_str = '\u2014'
            elif log.event_type == 'logout':
                event_str = 'Logout'
                detail_str = '\u2014'
            elif log.event_type in ('search_pyq', 'search_iq'):
                search_label = 'PYQ' if log.event_type == 'search_pyq' else 'IQ'

                # Determine status
                if log.results_count is not None and log.results_count == 0:
                    status = 'Not Found'
                else:
                    # Check for downloads first, then views
                    if log.event_type == 'search_pyq':
                        has_download = PaperDownload.objects.filter(
                            user=log.user,
                            paper__subject__iexact=log.detail
                        ).exists() or PaperDownload.objects.filter(
                            user=log.user,
                            paper__hashtags__icontains=log.detail
                        ).exists()
                        if has_download:
                            status = 'Downloaded'
                        else:
                            status = 'Viewed'
                    else:  # search_iq
                        has_download = IQDownload.objects.filter(
                            user=log.user,
                            subject__iexact=log.detail
                        ).exists()
                        if has_download:
                            status = 'Downloaded'
                        else:
                            status = 'Viewed'

                event_str = f'Search {search_label}'
                detail_str = f'{log.detail} \u2014 {status}'
            else:
                event_str = log.event_type
                detail_str = log.detail or '\u2014'

            time_str = timezone.localtime(log.created_at).strftime('%d %b, %Y \u00b7 %I:%M %p')

            table_data.append([
                Paragraph(str(idx), normal_style),
                Paragraph(log.user.username, normal_style),
                Paragraph(event_str, normal_style),
                Paragraph(detail_str, normal_style),
                Paragraph(time_str, normal_style),
            ])

        table = Table(
            table_data,
            colWidths=[30, 80, 75, 190, 140],
            repeatRows=1
        )
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2d2d3f')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#444466')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [
                colors.HexColor('#1a1a2e'),
                colors.HexColor('#16162a'),
            ]),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.HexColor('#e0e0e0')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(table)

        # Summary footer
        story.append(Spacer(1, 20))
        total = logs.count()
        logins = logs.filter(event_type='login').count()
        logouts = logs.filter(event_type='logout').count()
        searches = logs.filter(event_type__startswith='search').count()
        summary_style = ParagraphStyle(
            'ActivitySummary',
            parent=styles['Normal'],
            fontName=_PDF_FONT,
            fontSize=9,
            textColor=grey,
        )
        story.append(Paragraph(
            f'Total entries: {total} &nbsp;|&nbsp; '
            f'Logins: {logins} &nbsp;|&nbsp; '
            f'Logouts: {logouts} &nbsp;|&nbsp; '
            f'Searches: {searches}',
            summary_style
        ))

    doc.build(story, onFirstPage=draw_page_border, onLaterPages=draw_page_border)

    # ── Clear all activity log entries ────────────────────────────────────
    ActivityLog.objects.all().delete()

    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

# ── Paper Stats PDF Download ─────────────────────────────────────────────────

@never_cache
@login_required
def download_paper_stats_pdf(request):
    """
    Generate a PDF report of PYQ and IQ stats grouped by subject.
    PYQ papers are combined by subject regardless of year/regulation/branch.
    """
    if not request.user.is_superuser:
        return redirect('dashboard')

    from reportlab.platypus import Table, TableStyle, Spacer, SimpleDocTemplate, Paragraph, HRFlowable
    from reportlab.lib import colors
    from reportlab.lib.colors import HexColor, white

    import datetime as dt
    ist_tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = timezone.now().astimezone(ist_tz)
    filename = f"abhyas_paperstats_{now.strftime('%d-%m-%Y_%H-%M')}.pdf"

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=40, rightMargin=40, topMargin=50, bottomMargin=50
    )
    styles = getSampleStyleSheet()
    story = []

    # Styles
    title_style = ParagraphStyle(
        'StatsTitle', parent=styles['Title'],
        fontName=_PDF_FONT_BOLD, fontSize=18, spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        'StatsSubtitle', parent=styles['Normal'],
        fontName=_PDF_FONT, fontSize=9, textColor=grey,
        alignment=TA_CENTER, spaceAfter=20,
    )
    section_style = ParagraphStyle(
        'StatsSection', parent=styles['Heading2'],
        fontName=_PDF_FONT_BOLD, fontSize=13, spaceAfter=10,
        spaceBefore=20, textColor=HexColor('#e0e0e0'),
    )
    header_style = ParagraphStyle(
        'StatsHeader', parent=styles['Normal'],
        fontName=_PDF_FONT_BOLD, fontSize=9, leading=13, textColor=white,
    )
    normal_style = ParagraphStyle(
        'StatsNormal', parent=styles['Normal'],
        fontName=_PDF_FONT, fontSize=9, leading=13, textColor=HexColor('#e0e0e0'),
    )
    summary_style = ParagraphStyle(
        'StatsSummary', parent=styles['Normal'],
        fontName=_PDF_FONT, fontSize=9, textColor=grey,
    )

    table_style_def = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#2d2d3f')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#444466')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [
            HexColor('#1a1a2e'), HexColor('#16162a'),
        ]),
        ('TEXTCOLOR', (0, 1), (-1, -1), HexColor('#e0e0e0')),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ])

    # Title
    story.append(Paragraph('ABHYAS — Paper Stats Report', title_style))
    story.append(Paragraph(
        f"Generated on {now.strftime('%d %b %Y at %I:%M %p')}",
        subtitle_style
    ))
    story.append(HRFlowable(
        width='100%', thickness=1, color=grey,
        spaceAfter=14, spaceBefore=4
    ))

    # ── PYQ Stats by Subject ──────────────────────────────────────────────
    story.append(Paragraph('PYQ Papers — Stats by Subject', section_style))

    # Group PYQ papers by subject (case-insensitive), sum views & downloads
    from django.db.models.functions import Lower
    pyq_stats = Paper.objects.values(
        subject_lower=Lower('subject')
    ).annotate(
        total_papers=Count('id', distinct=True),
        total_views=Count('views', distinct=True),
        total_downloads=Count('downloads', distinct=True),
    ).order_by('subject_lower')

    # Get the original subject name (first occurrence)
    subject_names = {}
    for p in Paper.objects.values_list('subject', flat=True):
        key = p.lower()
        if key not in subject_names:
            subject_names[key] = p

    pyq_table_data = [[
        Paragraph('#', header_style),
        Paragraph('Subject', header_style),
        Paragraph('Papers', header_style),
        Paragraph('Views', header_style),
        Paragraph('Downloads', header_style),
    ]]

    grand_papers = grand_views = grand_downloads = 0
    for idx, stat in enumerate(pyq_stats, 1):
        subj = subject_names.get(stat['subject_lower'], stat['subject_lower'])
        grand_papers += stat['total_papers']
        grand_views += stat['total_views']
        grand_downloads += stat['total_downloads']
        pyq_table_data.append([
            Paragraph(str(idx), normal_style),
            Paragraph(subj, normal_style),
            Paragraph(str(stat['total_papers']), normal_style),
            Paragraph(str(stat['total_views']), normal_style),
            Paragraph(str(stat['total_downloads']), normal_style),
        ])

    if len(pyq_table_data) > 1:
        pyq_table = Table(pyq_table_data, colWidths=[30, 250, 60, 70, 80], repeatRows=1)
        pyq_table.setStyle(table_style_def)
        story.append(pyq_table)
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            f'Total: {grand_papers} papers &nbsp;|&nbsp; '
            f'{grand_views} views &nbsp;|&nbsp; '
            f'{grand_downloads} downloads',
            summary_style
        ))
    else:
        story.append(Paragraph('No PYQ papers uploaded yet.', normal_style))

    # ── IQ Stats by Subject ───────────────────────────────────────────────
    story.append(Spacer(1, 20))
    story.append(Paragraph('Important Questions — Stats by Subject', section_style))

    iq_subjects = ImportantQuestionEntry.objects.values('subject').annotate(
        total_questions=Count('id')
    ).order_by('subject')

    iq_table_data = [[
        Paragraph('#', header_style),
        Paragraph('Subject', header_style),
        Paragraph('Questions', header_style),
        Paragraph('Views', header_style),
        Paragraph('Downloads', header_style),
    ]]

    iq_grand_q = iq_grand_views = iq_grand_downloads = 0
    for idx, iq in enumerate(iq_subjects, 1):
        subject = iq['subject']
        views = IQView.objects.filter(subject=subject).count()
        downloads = IQDownload.objects.filter(subject=subject).count()
        iq_grand_q += iq['total_questions']
        iq_grand_views += views
        iq_grand_downloads += downloads
        iq_table_data.append([
            Paragraph(str(idx), normal_style),
            Paragraph(subject, normal_style),
            Paragraph(str(iq['total_questions']), normal_style),
            Paragraph(str(views), normal_style),
            Paragraph(str(downloads), normal_style),
        ])

    if len(iq_table_data) > 1:
        iq_table = Table(iq_table_data, colWidths=[30, 250, 70, 70, 80], repeatRows=1)
        iq_table.setStyle(table_style_def)
        story.append(iq_table)
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            f'Total: {iq_grand_q} questions &nbsp;|&nbsp; '
            f'{iq_grand_views} views &nbsp;|&nbsp; '
            f'{iq_grand_downloads} downloads',
            summary_style
        ))
    else:
        story.append(Paragraph('No important questions uploaded yet.', normal_style))

    doc.build(story, onFirstPage=draw_page_border, onLaterPages=draw_page_border)

    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
def download_error_log(request):
    """Download the system error log file formatted as a beautiful PDF. Admin-only."""
    if not request.user.is_superuser:
        return redirect('dashboard')

    # Dynamically locate PythonAnywhere error log
    log_path = None
    username = os.environ.get('USER', 'abhyas')
    possible_paths = [
        f"/var/log/{username}.pythonanywhere.com.error.log",
        f"/var/log/{username}.pythonanywhere.com.server.log",
    ]
    
    try:
        import glob
        possible_paths.extend(glob.glob("/var/log/*.error.log"))
    except Exception:
        pass

    for path in possible_paths:
        if path and os.path.exists(path):
            log_path = path
            break

    is_python_anywhere_log = (log_path is not None)

    if not log_path:
        # Fallback to local logs/django.log
        log_path = os.path.join(settings.BASE_DIR, 'logs', 'django.log')

    if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
        messages.error(request, "No error log entries found.")
        return redirect('admin_log')

    import datetime as dt
    import re
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib import colors
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter

    ist_tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = timezone.now() # Current UTC time

    # Days filter parameter
    days_str = request.GET.get('days', 'all')
    cutoff_date = None
    if days_str.isdigit():
        days_limit = int(days_str)
        cutoff_date = now - dt.timedelta(days=days_limit)
        filename = f"abhyas_errorlog_{days_str}days_{now.astimezone(ist_tz).strftime('%d-%m-%Y_%H-%M')}.pdf"
    else:
        filename = f"abhyas_errorlog_all_{now.astimezone(ist_tz).strftime('%d-%m-%Y_%H-%M')}.pdf"

    # Read log entries
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        raw_lines = f.readlines()

    # Parse and group log entries
    # Regex to match YYYY-MM-DD HH:MM:SS
    date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

    parsed_logs = []
    current_entry = None
    keep_current = True

    for line in raw_lines:
        match = date_pattern.search(line)
        if match:
            # New log entry starts
            if current_entry and keep_current:
                parsed_logs.append(current_entry)
            
            current_entry = line
            keep_current = True
            
            if cutoff_date:
                try:
                    log_time_str = match.group(1)
                    log_time = dt.datetime.strptime(log_time_str, '%Y-%m-%d %H:%M:%S')
                    if is_python_anywhere_log:
                        # PythonAnywhere system error.log is written in UTC
                        log_time = log_time.replace(tzinfo=dt.timezone.utc)
                    else:
                        # local django.log is in IST
                        log_time = log_time.replace(tzinfo=ist_tz)
                        
                    if log_time < cutoff_date:
                        keep_current = False
                except Exception:
                    pass
        else:
            # Continuation line (like tracebacks)
            if current_entry:
                current_entry += '\n' + line
            else:
                current_entry = line

    # Append the last entry
    if current_entry and keep_current:
        parsed_logs.append(current_entry)

    # ── Build the PDF ─────────────────────────────────────────────────────
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=40, rightMargin=40, topMargin=50, bottomMargin=50
    )
    styles = getSampleStyleSheet()
    story = []

    # Custom styles matching our dark theme
    title_style = ParagraphStyle(
        'LogTitle', parent=styles['Title'],
        fontName=_PDF_FONT_BOLD, fontSize=16, spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        'LogSubtitle', parent=styles['Normal'],
        fontName=_PDF_FONT, fontSize=9, textColor=colors.grey,
        alignment=TA_CENTER, spaceAfter=20,
    )
    
    # Monospace styling for log lines
    log_style = ParagraphStyle(
        'LogLine',
        fontName='Courier',
        fontSize=7,
        leading=9,
        textColor=HexColor('#333333'),
        spaceAfter=4,
    )
    
    # Monospace bold styling for error lines (Thick black)
    err_log_style = ParagraphStyle(
        'ErrLogLine',
        parent=log_style,
        fontName='Courier-Bold',
        textColor=HexColor('#000000'),
    )
    
    # Monospace bold styling for warning lines (Thick dark gold)
    warn_log_style = ParagraphStyle(
        'WarnLogLine',
        parent=log_style,
        fontName='Courier-Bold',
        textColor=HexColor('#c57f00'),
    )

    story.append(Paragraph('ABHYAS — System Diagnostics &amp; Error Report', title_style))
    filter_label = f"Last {days_str} days" if days_str != 'all' else "All logs"
    story.append(Paragraph(
        f"Generated on {now.astimezone(ist_tz).strftime('%d %b %Y at %I:%M %p')} ({filter_label})",
        subtitle_style
    ))
    story.append(HRFlowable(
        width='100%', thickness=1, color=colors.grey,
        spaceAfter=14, spaceBefore=4
    ))

    if not parsed_logs:
        empty_style = ParagraphStyle(
            'EmptyStyle', parent=styles['Normal'],
            fontName=_PDF_FONT, fontSize=10, textColor=colors.grey,
            alignment=TA_CENTER
        )
        story.append(Spacer(1, 20))
        story.append(Paragraph("No log entries match the selected date filter.", empty_style))
    else:
        from django.utils.html import escape
        # To prevent oversized PDFs, limit to the last 500 entries
        for entry in parsed_logs[-500:]:
            # Clean up spacing/carriage returns
            cleaned = entry.strip('\r\n')
            
            # If it's a PythonAnywhere log (UTC), convert the timestamp in the message to IST
            if is_python_anywhere_log:
                match = date_pattern.search(cleaned)
                if match:
                    try:
                        utc_time_str = match.group(1)
                        utc_time = dt.datetime.strptime(utc_time_str, '%Y-%m-%d %H:%M:%S')
                        utc_time = utc_time.replace(tzinfo=dt.timezone.utc)
                        ist_time = utc_time.astimezone(ist_tz)
                        ist_time_str = ist_time.strftime('%Y-%m-%d %H:%M:%S')
                        cleaned = cleaned.replace(utc_time_str, ist_time_str)
                    except Exception:
                        pass
                        
            escaped = escape(cleaned).replace('\n', '<br/>').replace(' ', '&nbsp;')
            
            # Color-code lines based on severity
            cleaned_upper = cleaned.upper()
            is_error = any(k in cleaned_upper for k in ['ERROR', 'EXCEPTION', 'TRACEBACK', 'OSERROR', 'FAILED LOGIN', 'REGISTRATION ATTEMPT'])
            is_warning = any(k in cleaned_upper for k in ['WARNING', 'WARN', 'NOT FOUND', 'INVALIDATED'])
            
            if is_error:
                entry_style = err_log_style
                # Wrap specific error targets in bold red font tag
                red_targets = [
                    'django.core.exceptions.DisallowedHost:',
                    'ValueError:',
                    'Traceback (most recent call last):',
                    'OSError:',
                    'AttributeError:'
                ]
                for target in red_targets:
                    escaped_target = escape(target).replace(' ', '&nbsp;')
                    if escaped_target in escaped:
                        replacement = f'<font color="#d32f2f"><b>{escaped_target}</b></font>'
                        escaped = escaped.replace(escaped_target, replacement)
            elif is_warning:
                entry_style = warn_log_style
            else:
                entry_style = log_style
                
            story.append(Paragraph(escaped, entry_style))
            story.append(Spacer(1, 2))

    # Page numbering / border canvas callback
    def draw_decorations(canvas, document):
        canvas.saveState()
        # Page border
        canvas.setStrokeColor(HexColor('#3a3a50'))
        canvas.setLineWidth(1)
        canvas.rect(30, 30, letter[0] - 60, letter[1] - 60)
        
        # Footer
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(45, 40, "Confidential — For Administrator Use Only")
        canvas.drawRightString(letter[0] - 45, 40, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_decorations, onLaterPages=draw_decorations)

    buffer.seek(0)
    response = HttpResponse(buffer.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
