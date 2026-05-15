"""
Views for the PYQ (Previous Year Questions) application.

This module handles all request/response logic including:
- Authentication (login, register, logout)
- Paper and IQ uploads and downloads
- Support tickets and user management
- Admin dashboard functionality
"""

import io
import json
import logging
import os
import unicodedata
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.db.models import Q, Count, Max
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.models import User
from django.conf import settings
from django.views.decorators.http import require_http_methods
from django.utils.html import escape

import requests as http_requests
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
    UserSession
)

# Setup logging
logger = logging.getLogger(__name__)


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
    Get file content for a paper from Cloudinary or local storage.
    
    Tries Cloudinary URL first. If the file doesn't exist there (old upload),
    falls back to reading from local disk via MEDIA_ROOT.
    
    Args:
        paper: Paper model instance
        
    Returns:
        bytes: File content or None if not found
    """
    file_url = paper.file.url

    # Try Cloudinary first (all URLs are https://res.cloudinary.com/...)
    if file_url.startswith('http'):
        try:
            cloudinary_response = http_requests.get(file_url, stream=True, timeout=10)
            if cloudinary_response.status_code == 200:
                return cloudinary_response.content
        except http_requests.RequestException as e:
            logger.error(f"Error fetching file from Cloudinary: {e}")
            return None

    # Fallback: read from local disk (old files uploaded before Cloudinary)
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
    """Homepage - redirects authenticated users to their dashboard."""
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect('admin_log')
        if request.user.is_staff:
            return redirect('staff_dashboard')
        return redirect('dashboard')
    return render(request, 'pyqapp/index.html')


@require_http_methods(["GET", "POST"])
def login_view(request):
    """
    Handle user login with single-device enforcement.
    
    On successful login, invalidates any existing session for the user
    to enforce single-device login policy.
    """
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            # ── Single Device Login: Invalidate previous session ──
            from django.contrib.sessions.models import Session
            try:
                old_session = user.user_session
                if old_session.session_key:
                    try:
                        Session.objects.get(session_key=old_session.session_key).delete()
                        logger.info(f"Invalidated previous session for user {user.username}")
                    except Session.DoesNotExist:
                        pass
            except UserSession.DoesNotExist:
                pass

            # Log the user in
            login(request, user)

            # Save new session and device info
            if not request.session.session_key:
                request.session.save()

            # Get client IP (handle proxies)
            ip_address = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            if not ip_address:
                ip_address = request.META.get('REMOTE_ADDR', '')

            # Get browser/device info
            device_info = request.META.get('HTTP_USER_AGENT', '')[:getattr(settings, 'DEVICE_INFO_MAX_LENGTH', 512)]

            # Store session record
            UserSession.objects.update_or_create(
                user=user,
                defaults={
                    'session_key': request.session.session_key,
                    'ip_address': ip_address or None,
                    'device_info': device_info,
                }
            )

            logger.info(f"User {user.username} logged in from IP {ip_address}")

            # Redirect to appropriate dashboard
            if user.is_superuser:
                return redirect('admin_log')
            if user.is_staff:
                return redirect('staff_dashboard')
            return redirect('dashboard')
        else:
            messages.error(request, "Invalid username or password")
            logger.warning(f"Failed login attempt for username: {username}")
    
    return render(request, 'pyqapp/login.html')


@require_http_methods(["GET", "POST"])
def register_view(request):
    """
    Handle new user registration with generic error messages.
    
    Uses generic messages to prevent user enumeration attacks.
    """
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')

        # Validate inputs
        if not all([username, email, password]):
            messages.error(request, "All fields are required")
            return render(request, 'pyqapp/register.html')

        if len(username) < 3:
            messages.error(request, "Username must be at least 3 characters")
            return render(request, 'pyqapp/register.html')

        if len(password) < 8:
            messages.error(request, "Password must be at least 8 characters")
            return render(request, 'pyqapp/register.html')

        # Generic message to prevent user enumeration
        if User.objects.filter(username=username).exists() or User.objects.filter(email=email).exists():
            messages.success(request, "If this account doesn't exist, it has been created. Check your email.")
            logger.warning(f"Registration attempt for existing username: {username}")
        else:
            try:
                User.objects.create_user(username=username, email=email, password=password)
                messages.success(request, "Account created! You can now login.")
                logger.info(f"New user registered: {username}")
            except Exception as e:
                logger.error(f"Error creating user {username}: {e}")
                messages.error(request, "An error occurred during registration")
                return render(request, 'pyqapp/register.html')
        
        return redirect('login')
    
    return render(request, 'pyqapp/register.html')


@login_required
def logout_view(request):
    """Clear session record and log user out."""
    if request.user.is_authenticated:
        try:
            session_record = request.user.user_session
            session_record.session_key = None
            session_record.save(update_fields=['session_key'])
            logger.info(f"User {request.user.username} logged out")
        except UserSession.DoesNotExist:
            pass
    
    logout(request)
    return redirect('login')


@login_required
def logout_all_devices_view(request):
    """Log out the user from all devices by invalidating the active session."""
    try:
        session_record = request.user.user_session
        session_record.logout_all_devices()
        logger.warning(f"User {request.user.username} logged out from all devices")
    except UserSession.DoesNotExist:
        pass
    
    logout(request)
    messages.success(request, "You have been logged out from all devices.")
    return redirect('login')


# ── Student Dashboard ────────────────────────────────────────────────────────

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
                iqs = iqs.filter(Q(subject__icontains=search) | Q(hashtags__icontains=search))
            
            iqs = iqs.order_by('unit', 'question_type', 'question_number')

            # Track views
            if request.user.is_authenticated and search:
                viewed_subjects = set()
                for q in iqs:
                    if q.subject not in viewed_subjects:
                        IQView.objects.get_or_create(subject=q.subject, user=request.user)
                        viewed_subjects.add(q.subject)
            
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
            return JsonResponse(results, safe=False)

        # Paper Search
        hashtag = request.GET.get('hashtag', '').strip()
        year = request.GET.get('year', '').strip()
        paper_type = request.GET.get('type', '').strip()
        branch = request.GET.get('branch', '').strip()
        regulation = request.GET.get('regulation', '').strip()

        papers = Paper.objects.all()

        if hashtag:
            papers = papers.filter(Q(subject__icontains=hashtag) | Q(hashtags__icontains=hashtag))
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
                PaperView.objects.get_or_create(paper=p, user=request.user)

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
        return JsonResponse(results, safe=False)

    # Regular page load
    if request.user.is_authenticated:
        SiteVisit.objects.get_or_create(user=request.user)

    years = Paper.objects.values_list('year', flat=True).distinct().order_by('-year')
    return render(request, 'pyqapp/student.html', {'years': years})


# ── Staff Dashboard ──────────────────────────────────────────────────────────

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


@login_required
def profile_view(request):
    """Display user profile and active session info."""
    session_info = None
    try:
        session_record = request.user.user_session
        session_info = {
            'ip_address': session_record.ip_address or 'Unknown',
            'device_info': session_record.device_info or 'Unknown',
            'logged_in_at': session_record.logged_in_at,
        }
    except UserSession.DoesNotExist:
        pass

    return render(request, 'pyqapp/profile.html', {'session_info': session_info})


# ── Admin Dashboard ──────────────────────────────────────────────────────────

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

    # Fetch dashboard data
    staff_users = User.objects.filter(is_staff=True).order_by('-date_joined')
    tickets = Ticket.objects.all().order_by('-created_at').prefetch_related('replies', 'replies__author', 'student')
    
    all_papers = Paper.objects.all().annotate(
        unique_views=Count('views', distinct=True),
        unique_downloads=Count('downloads', distinct=True)
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
            'unique_views': IQView.objects.filter(subject=subject).count(),
            'unique_downloads': IQDownload.objects.filter(subject=subject).count(),
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

    context = {
        'staff_users': staff_users,
        'tickets': tickets,
        'papers': papers,
        'all_papers': all_papers,
        'iq_stats': iq_stats,
        'my_iqs': my_iqs,
        'iq_entries_map_json': json.dumps(iq_entries_map),
        'branches': branches_list,
        'unique_visitors': unique_visitors,
    }
    return render(request, 'pyqapp/admin_log.html', context)


# ── File Operations ──────────────────────────────────────────────────────────

@login_required
def view_paper(request, paper_id):
    """Serve a paper for inline viewing in the browser."""
    paper = get_object_or_404(Paper, id=paper_id)

    if request.user.is_authenticated:
        PaperView.objects.get_or_create(paper=paper, user=request.user)

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
        PaperDownload.objects.get_or_create(paper=paper, user=request.user)

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

    iqs = ImportantQuestionEntry.objects.filter(
        Q(subject__icontains=search) | Q(hashtags__icontains=search)
    )
    
    if unit and unit.isdigit():
        iqs = iqs.filter(unit=int(unit))
    if iq_type:
        iqs = iqs.filter(question_type=iq_type)
    
    iqs = iqs.order_by('question_number')
    
    if not iqs.exists():
        return HttpResponse("No questions found.", status=404)

    # Track downloads
    if request.user.is_authenticated:
        seen_subjects = set()
        for q in iqs:
            if q.subject not in seen_subjects:
                IQDownload.objects.get_or_create(subject=q.subject, user=request.user)
                seen_subjects.add(q.subject)

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
