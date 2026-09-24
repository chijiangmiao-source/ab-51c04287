FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
COPY app ./app
COPY tests ./tests
COPY verify.py ./

# Default command runs the API; compose overrides per-service.
CMD ["python", "-m", "app.api"]
