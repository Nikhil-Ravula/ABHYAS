FROM python:3.12-slim

WORKDIR /app

RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends libpq-dev gcc fonts-dejavu-core git && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install gunicorn psycopg2-binary dj-database-url whitenoise django-storages[boto3]

COPY . /app/

RUN DATABASE_URL=sqlite:///tmp/db.sqlite3 \
    NIDHI_MINIO_URL=http://dummy \
    NIDHI_MINIO_ACCESS_KEY=dummy \
    NIDHI_MINIO_SECRET_KEY=dummy \
    python manage.py collectstatic --no-input

EXPOSE 8000

RUN chmod +x /app/nidhi-init.sh
ENTRYPOINT ["/app/nidhi-init.sh"]
CMD ["gunicorn", "pyqproject.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "300"]
