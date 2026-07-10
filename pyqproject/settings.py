import os
from pathlib import Path
from dotenv import load_dotenv
import posixpath

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required.")

DEBUG = os.environ.get('DEBUG', 'False') == 'True'

allowed_hosts_from_env = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
ALLOWED_HOSTS = allowed_hosts_from_env

ALLOWED_HOSTS += [
    'rubix.tail2d2f35.ts.net',
    'vitharn.com',
    'www.vitharn.com',
]

CSRF_TRUSTED_ORIGINS = [
    'https://vitharn.com',
    'https://www.vitharn.com',
    'https://rubix.tail2d2f35.ts.net',
]

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'storages',
    'pyqapp',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'pyqapp.middleware.SingleDeviceLoginMiddleware',
    'pyqapp.middleware.LastSeenMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'pyqproject.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'pyqapp.context_processors.umami',
            ],
        },
    },
]

WSGI_APPLICATION = 'pyqproject.wsgi.application'

# Database via Nidhi SDK (DATABASE_URL from nidhi-init.sh)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}
from nidhi_sdk.django import inject_nidhi_database
inject_nidhi_database(locals())

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

ENVIRONMENT = os.environ.get('ENVIRONMENT', 'development')

if ENVIRONMENT == 'production':
    FORCE_SCRIPT_NAME = '/abhyas/app'
    LOGIN_URL = '/abhyas/'
    STATIC_URL = '/abhyas/static/'
else:
    FORCE_SCRIPT_NAME = '/vitharn/abhyas/app'
    LOGIN_URL = '/vitharn/abhyas/'
    STATIC_URL = '/vitharn/abhyas/static/'

# Aacharya OIDC SSO
AACHARYA_OIDC = {
    'BASE_URL': os.environ.get('AACHARYA_BASE_URL', 'https://rubix.tail2d2f35.ts.net/aacharya'),
    'CLIENT_ID': os.environ.get('ABHYAS_OIDC_CLIENT_ID', 'ABHYAS_CLIENT'),
    'CLIENT_SECRET': os.environ.get('ABHYAS_OIDC_CLIENT_SECRET', ''),
    'SCOPE': 'openid email profile',
}
AACHARYA_OIDC['AUTHORIZE_URL'] = f"{AACHARYA_OIDC['BASE_URL']}/o/authorize/"
AACHARYA_OIDC['TOKEN_URL'] = f"{AACHARYA_OIDC['BASE_URL']}/o/token/"
AACHARYA_OIDC['USERINFO_URL'] = f"{AACHARYA_OIDC['BASE_URL']}/o/userinfo/"
AACHARYA_OIDC['REDIRECT_URI'] = os.environ.get('ABHYAS_PUBLIC_URL', 'https://vitharn.com') + FORCE_SCRIPT_NAME + '/auth/aacharya/callback/'
STATICFILES_DIRS = []

# Umami Analytics
UMAMI_SRC = os.environ.get('UMAMI_SRC', '')
UMAMI_WEBSITE_ID = os.environ.get('UMAMI_WEBSITE_ID', '')

FAVICON_URL = os.environ.get('FAVICON_URL', f'{_script_parent}/static/favicon.ico')
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

if ENVIRONMENT == 'production':
    MEDIA_URL = '/abhyas/media/'
else:
    MEDIA_URL = '/vitharn/abhyas/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Nidhi MinIO Storage via SDK
from nidhi_sdk.django import inject_nidhi_storage
inject_nidhi_storage(locals())

# Fix for Django 4.2+ (DEFAULT_FILE_STORAGE was removed in Django 5.1)
if 'DEFAULT_FILE_STORAGE' in locals():
    STORAGES = {
        "default": {
            "BACKEND": locals()['DEFAULT_FILE_STORAGE'],
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
        }
    }

# Make MinIO URLs point to Nginx proxy without querystring auth
if os.environ.get('MEDIA_BUCKET_NAME'):
    host = os.environ.get('ABHYAS_PUBLIC_URL', 'https://rubix.tail2d2f35.ts.net').replace('https://', '').replace('http://', '')
    bucket = os.environ.get('MEDIA_BUCKET_NAME')
    AWS_S3_CUSTOM_DOMAIN = f"{host}/minio/{bucket}"
    AWS_QUERYSTRING_AUTH = False

MAX_UPLOAD_SIZE = 50 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'txt',
    'jpg', 'jpeg', 'png', 'gif',
}
SESSION_KEY_LENGTH = 40
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
X_FRAME_OPTIONS = 'SAMEORIGIN'

import time
import datetime as dt
import logging

def ist_converter(*args, **kwargs):
    secs = None
    for arg in args:
        if isinstance(arg, (int, float)):
            secs = arg
            break
    if secs is None:
        secs = time.time()
    utc_dt = dt.datetime.fromtimestamp(secs, dt.timezone.utc)
    ist_tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    ist_dt = utc_dt.astimezone(ist_tz)
    return ist_dt.timetuple()

logging.Formatter.converter = ist_converter

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {asctime} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'simple'
        },
        'file': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(BASE_DIR, 'logs', 'django.log'),
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
        },
        'django.request': {
            'handlers': ['console', 'file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'pyqapp': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG' if DEBUG else 'INFO',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'ERROR',
    },
}

LOGS_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)
