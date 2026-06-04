from django.contrib import admin
from .models import Paper, Ticket, TicketReply, UserSession

admin.site.register(Paper)
admin.site.register(Ticket)
admin.site.register(TicketReply)


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
