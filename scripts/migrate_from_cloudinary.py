"""
Cloudinary -> Nidhi MinIO File Migration

DB records already exist from SQLite restore. This script:
1. Lists all files from Cloudinary Admin API
2. Downloads each paper file and uploads to MinIO
3. Updates the file reference if needed

Usage:
    docker exec abhyas_api bash -c 'source /app/.nidhi_env.sh && python scripts/migrate_from_cloudinary.py'
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pyqproject.settings')

import django
django.setup()

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
import requests

from pyqapp.models import Paper

CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME')
API_KEY = os.environ.get('CLOUDINARY_API_KEY')
API_SECRET = os.environ.get('CLOUDINARY_API_SECRET')

if not all([CLOUD_NAME, API_KEY, API_SECRET]):
    print("ERROR: Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET")
    sys.exit(1)

BASE = f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}"


def list_all_raw():
    resources = []
    next_cursor = None
    while True:
        params = "max_result=500"
        if next_cursor:
            params += f"&next_cursor={next_cursor}"
        r = requests.get(f"{BASE}/resources/raw/upload?{params}", auth=(API_KEY, API_SECRET), timeout=30)
        r.raise_for_status()
        data = r.json()
        resources.extend(data.get('resources', []))
        next_cursor = data.get('next_cursor')
        if not next_cursor:
            break
    return resources


def migrate_files():
    print("Fetching file list from Cloudinary...")
    all_files = list_all_raw()
    total_size = sum(f['bytes'] for f in all_files)
    print(f"Found {len(all_files)} files ({total_size/1024/1024:.1f} MB)")
    print()

    # Build lookup: public_id -> file info
    cloudinary_files = {f['public_id']: f for f in all_files}

    papers = Paper.objects.all()
    migrated = 0
    skipped = 0
    errors = 0

    for paper in papers:
        file_field = paper.file
        if not file_field or not file_field.name:
            print(f"  Paper #{paper.id}: no file reference, skipping")
            skipped += 1
            continue

        file_path = file_field.name  # e.g. "media/papers/xyz.pdf"
        clean_name = os.path.basename(file_path)

        # Check if already on MinIO
        try:
            if file_field.size and file_field.size > 0:
                print(f"  Paper #{paper.id}: already on MinIO ({clean_name})")
                skipped += 1
                continue
        except Exception:
            pass  # file doesn't exist on MinIO yet

        # Check if it exists on Cloudinary
        if file_path not in cloudinary_files:
            print(f"  Paper #{paper.id}: {clean_name} NOT FOUND on Cloudinary")
            errors += 1
            continue

        entry = cloudinary_files[file_path]
        url = f"https://res.cloudinary.com/{CLOUD_NAME}/raw/upload/{file_path}"
        bytes_size = entry['bytes']

        print(f"  Paper #{paper.id} [{bytes_size/1024:.0f}KB] {clean_name}...", end='', flush=True)

        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code != 200:
                print(f" FAIL (HTTP {resp.status_code})")
                errors += 1
                continue

            # Save to MinIO using the storage backend
            default_storage.save(file_path, ContentFile(resp.content))
            migrated += 1
            print(" OK")
        except Exception as e:
            print(f" ERROR: {e}")
            errors += 1

    print()
    print("=" * 60)
    print(f"Migrated: {migrated} paper files to MinIO")
    print(f"Skipped (already on MinIO): {skipped}")
    print(f"Errors/not found: {errors}")
    print(f"Total papers in DB: {papers.count()}")
    print("=" * 60)


if __name__ == '__main__':
    migrate_files()
