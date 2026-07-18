import os
from storages.backends.s3boto3 import S3Boto3Storage


class NidhiMediaStorage(S3Boto3Storage):
    """S3Boto3Storage subclass that routes all file URLs through Nidhi's Media Gateway.
    MinIO is NEVER exposed directly. Every URL includes the API key for authentication."""

    def url(self, name):
        nidhi_url = os.environ.get('NIDHI_DEV_SERVER_URL', '')
        bucket = os.environ.get('MEDIA_BUCKET_NAME', '')
        api_key = os.environ.get('NIDHI_APP_API_KEY', '')

        if nidhi_url and bucket and api_key:
            return f"{nidhi_url.rstrip('/')}/api/media/{bucket}/{name}?api_key={api_key}"

        # Fallback: local dev without Nidhi (e.g. FileSystemStorage)
        return super().url(name)
