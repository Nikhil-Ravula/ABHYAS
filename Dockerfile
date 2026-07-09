FROM python:3.12-slim

WORKDIR /app

RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends libpq-dev gcc fonts-dejavu-core git && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install gunicorn psycopg2-binary dj-database-url whitenoise django-storages[boto3]

COPY . /app/

RUN python manage.py collectstatic --no-input

EXPOSE 8000

RUN chmod +x /app/nidhi-init.sh
ENTRYPOINT ["/app/nidhi-init.sh"]
CMD ["gunicorn", "pyqproject.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "300"]
