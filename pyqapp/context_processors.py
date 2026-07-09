from django.conf import settings

def umami(request):
    config = {
        'UMAMI_SRC': getattr(settings, 'UMAMI_SRC', ''),
        'UMAMI_WEBSITE_ID': getattr(settings, 'UMAMI_WEBSITE_ID', ''),
    }
    if settings.DEBUG:
        config['UMAMI_SRC'] = ''
        config['UMAMI_WEBSITE_ID'] = ''
    return config
