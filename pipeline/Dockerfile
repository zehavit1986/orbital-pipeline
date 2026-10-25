# Pipeline image: code and dependencies only. Configuration, credentials and the commitments
# workbooks come from Kubernetes (ConfigMaps and a Secret), not from the image.
#   docker build -t orbital-pipeline:1.0 pipeline
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY orbital_pipeline ./orbital_pipeline
COPY tests ./tests

USER 65534:65534
ENTRYPOINT ["python", "-m", "orbital_pipeline"]
