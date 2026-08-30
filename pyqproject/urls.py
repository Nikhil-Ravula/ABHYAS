"""
URL configuration for pyqproject project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.sitemaps.views import sitemap
from pyqapp.sitemaps import AbhyasStaticSitemap, AbhyasPaperSitemap, AbhyasQuestionSitemap

sitemaps = {
    'static': AbhyasStaticSitemap,
    'papers': AbhyasPaperSitemap,
    'questions': AbhyasQuestionSitemap,
}

urlpatterns = [
    path('admin/', admin.site.urls),
    # Child sitemap at /abhyas/app/sitemap.xml; private account routes are excluded.
    # nginx proxies /abhyas/sitemap.xml? -> needs host nginx mapping; direct at /sitemap.xml works for app mount
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),
    path('', include('pyqapp.urls')),
]

if settings.DEBUG:
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += staticfiles_urlpatterns()
