from django.db import models
from django.contrib.auth.models import User

REGULATION_CHOICES = [
    ('R02','R02'),('R05','R05'),('R07','R07'),('R09','R09'),('R13','R13'),
    ('R15','R15'),('R16','R16'),('R18','R18'),('R22','R22'),('R25','R25'),
]

class Paper(models.Model):
    PAPER_TYPES = [
        ('regular', 'Regular'),
        ('supply', 'Supply'),
        ('mid', 'Mid'),
    ]

    subject = models.CharField(max_length=200)
    year = models.IntegerField()
    paper_type = models.CharField(max_length=10, choices=PAPER_TYPES)
    regulation = models.CharField(max_length=5, choices=REGULATION_CHOICES, default='R22')
    # Storing multiple branches as a comma-separated string to match your PHP logic
    branch = models.CharField(max_length=100) 
    hashtags = models.CharField(max_length=255, blank=True, help_text="e.g. java, oopj")
    
    # File handling
    file = models.FileField(upload_to='papers/')
    original_filename = models.CharField(max_length=255)
    
    # Metadata
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.subject} ({self.year})"
    
class Ticket(models.Model):
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('pending', 'Pending'),
        ('resolved', 'Resolved'),
    ]
    
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tickets')
    subject = models.CharField(max_length=200)
    description = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='open')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.subject} - {self.student.username}"


class TicketReply(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='replies')
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name_plural = 'Ticket Replies'

    def __str__(self):
        return f"Reply to '{self.ticket.subject}' by {self.author.username}"

class ImportantQuestionEntry(models.Model):
    subject = models.CharField(max_length=200)
    hashtags = models.CharField(max_length=255, blank=True, help_text="e.g. java, oopj")
    branch = models.CharField(max_length=100)
    regulation = models.CharField(max_length=5, choices=REGULATION_CHOICES, default='R22')
    
    unit = models.IntegerField()
    question_type = models.CharField(max_length=10) # 'short' or 'long'
    question_number = models.IntegerField()
    question_text = models.TextField(blank=True)
    file = models.FileField(upload_to='iq/', blank=True, null=True)
    original_filename = models.CharField(max_length=255, blank=True)
    
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.subject} - Q{self.question_number} (Unit {self.unit})"


class PaperView(models.Model):
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='views')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('paper', 'user')

    def __str__(self):
        return f"{self.user.username} viewed {self.paper.subject}"


class PaperDownload(models.Model):
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='downloads')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    downloaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('paper', 'user')

    def __str__(self):
        return f"{self.user.username} downloaded {self.paper.subject}"


class IQView(models.Model):
    subject = models.CharField(max_length=200)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('subject', 'user')

    def __str__(self):
        return f"{self.user.username} viewed IQ: {self.subject}"


class IQDownload(models.Model):
    subject = models.CharField(max_length=200)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    downloaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('subject', 'user')

    def __str__(self):
        return f"{self.user.username} downloaded IQ: {self.subject}"


class SiteVisit(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    first_visit = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} visited site"


# ── Single Device Login ──────────────────────────────────────────────────────
# Stores the active session key for each user. When a user logs in from a new
# device, the old session key is invalidated and replaced with the new one.
# This ensures only ONE active session per user account at any time.

class Announcement(models.Model):
    message    = models.TextField()
    posted_by  = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active  = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.message[:60]


class UserSession(models.Model):
    # One-to-one link: each user has exactly one active session record
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='user_session')

    # The Django session key for the currently active session
    session_key = models.CharField(max_length=40, blank=True, null=True)

    # Login tracking — no PII stored
    login_count = models.IntegerField(default=0, help_text="Total number of successful logins")
    logged_in_at = models.DateTimeField(auto_now=True, help_text="Timestamp of last login")

    # Last activity timestamp — updated on every page request via middleware
    last_seen = models.DateTimeField(null=True, blank=True, help_text="Timestamp of last page request")

    def __str__(self):
        return f"{self.user.username} - Session: {self.session_key[:8] if self.session_key else 'None'}..."

    def logout_all_devices(self):
        """
        Invalidate the current active session by deleting it from the session store.
        This effectively logs the user out from whatever device they're on.
        """
        from django.contrib.sessions.models import Session
        if self.session_key:
            try:
                Session.objects.get(session_key=self.session_key).delete()
            except Session.DoesNotExist:
                pass  # Session already expired or was deleted
            self.session_key = None
            self.save(update_fields=['session_key'])