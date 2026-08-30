from django.contrib import admin
from .models import Paper, Ticket, TicketReply, UserSession, ActivityLog, ImportantQuestionEntry

@admin.register(Paper)
class PaperAdmin(admin.ModelAdmin):
    list_display = ('subject', 'year', 'paper_type', 'regulation', 'is_public', 'uploaded_at')
    list_filter = ('is_public', 'paper_type', 'regulation')
    search_fields = ('subject', 'branch', 'hashtags')


@admin.register(ImportantQuestionEntry)
class ImportantQuestionAdmin(admin.ModelAdmin):
    list_display = ('subject', 'unit', 'question_number', 'is_public', 'uploaded_at')
    list_filter = ('is_public', 'regulation', 'question_type')
    search_fields = ('subject', 'question_text', 'hashtags')
admin.site.register(Ticket)
admin.site.register(TicketReply)
admin.site.register(ActivityLog)


# ── User Session Admin: shows login activity without any PII ──────────────────
@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'login_count', 'logged_in_at', 'last_seen', 'session_key_short')
    list_filter = ('logged_in_at',)
    search_fields = ('user__username', 'user__email')
    readonly_fields = ('user', 'session_key', 'login_count', 'logged_in_at', 'last_seen')
    ordering = ('-logged_in_at',)

    def session_key_short(self, obj):
        """Show truncated session key for reference."""
        if obj.session_key:
            return obj.session_key[:8] + '...'
        return '—'
    session_key_short.short_description = 'Session'
