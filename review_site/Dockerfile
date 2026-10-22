# The review site only. It does not include the comic pipeline (bom_comic); the publish tool runs on your PC.
# Build from this folder:  cd review_site && docker build -t bom-review .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    REVIEW_DATA_DIR=/data REVIEW_SECURE_COOKIES=1

WORKDIR /srv
COPY requirements.txt /srv/review_site/requirements.txt
RUN pip install --no-cache-dir -r /srv/review_site/requirements.txt

COPY . /srv/review_site
# Runs as root inside the container: Fly mounts the data volume root-owned, and the VM is already isolated.
RUN mkdir -p /data

EXPOSE 8080
# Create/upgrade the database, then serve. One process on purpose: SQLite and the login limiter live in this process.
CMD ["sh", "-c", "python -m review_site.manage upgrade && exec uvicorn review_site.app.main:create_app --factory --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'"]
