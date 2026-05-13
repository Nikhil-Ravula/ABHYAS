from django.contrib import admin
from .models import Paper, Ticket, TicketReply, UserSession

admin.site.register(Paper)
admin.site.register(Ticket)
admin.site.register(TicketReply)


# ── Single Device Login: Show active sessions in admin ──
@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'session_key', 'ip_address', 'device_info_short', 'logged_in_at')
    list_filter = ('logged_in_at',)
    search_fields = ('user__username', 'ip_address')
    readonly_fields = ('user', 'session_key', 'ip_address', 'device_info', 'logged_in_at')

    def device_info_short(self, obj):
        """Show truncated device info in the list view."""
        return obj.device_info[:80] + '...' if len(obj.device_info) > 80 else obj.device_info
    device_info_short.short_description = 'Device'
