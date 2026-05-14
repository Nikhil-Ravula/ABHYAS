from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),
    path('logout-all-devices/', views.logout_all_devices_view, name='logout_all_devices'),
    path('dashboard/', views.student_dashboard, name='dashboard'),
    path('staff/', views.staff_dashboard, name='staff_dashboard'),
    path('support/', views.support_view, name='support'),
    path('profile/', views.profile_view, name='profile'),
    path('admin-log/', views.admin_log_view, name='admin_log'),
    path('reply-ticket/<int:ticket_id>/', views.reply_ticket_view, name='reply_ticket'),
    path('view-paper/<int:paper_id>/', views.view_paper, name='view_paper'),
    path('download-paper/<int:paper_id>/', views.download_paper, name='download_paper'),
    path('download-iq-pdf/', views.download_iq_pdf, name='download_iq_pdf'),
    path('delete-paper/<int:paper_id>/', views.delete_paper, name='delete_paper'),
    path('delete-iq/', views.delete_iq, name='delete_iq'),
    path('edit-paper/<int:paper_id>/', views.edit_paper, name='edit_paper'),
    path('edit-iq/', views.edit_iq, name='edit_iq'),
]