"""
Abhyas child sitemap — 29-08-2026

Audit: All /abhyas/app/* routes (dashboard, paper view/download, IQ) are
@login_required. No public paper/subject detail page exists, so the Paper
sitemap is intentionally EMPTY (0 loc). Indexing private 302s would harm SEO.

When founder wants SEO indexing:
  Public paper previews now use explicit Paper.is_public opt-in; downloads remain auth-protected.
  3. This file serves https://www.vitharn.com/abhyas/sitemap.xml via Django
     (nginx proxy /abhyas/app/ -> :8003). Vitharn main sitemap index can reference it.

Static landing at /abhyas (SPA) is public and indexed via Vitharn's StaticViewSitemap.
Public paper/question previews are opt-in via is_public; files and account pages remain private.
"""
from django.contrib.sitemaps import Sitemap
from .models import Paper, ImportantQuestionEntry


class AbhyasStaticSitemap(Sitemap):
    priority = 1.0
    changefreq = 'daily'
    protocol = 'https'

    def items(self):
        return ['index', 'public_paper_list', 'public_iq_list']

    def location(self, item):
        return {
            'index': '/abhyas/app/',
            'public_paper_list': '/abhyas/app/papers/',
            'public_iq_list': '/abhyas/app/important-questions/',
        }[item]

    def get_urls(self, page=1, site=None, protocol=None):
        return super().get_urls(page=page, site=site, protocol='https')


class AbhyasPaperSitemap(Sitemap):
    """Explicitly public paper previews; files/downloads remain private."""
    priority = 0.7
    changefreq = 'weekly'
    protocol = 'https'

    def items(self):
        return Paper.objects.filter(is_public=True).order_by('-uploaded_at')

    def location(self, obj):
        return f"/abhyas/app/papers/{obj.id}/"

    def lastmod(self, obj):
        return obj.uploaded_at


class AbhyasQuestionSitemap(Sitemap):
    priority = 0.7
    changefreq = 'weekly'
    protocol = 'https'

    def items(self):
        return ImportantQuestionEntry.objects.filter(is_public=True).order_by('-uploaded_at')

    def location(self, obj):
        return f"/abhyas/app/important-questions/{obj.id}/"

    def lastmod(self, obj):
        return obj.uploaded_at
