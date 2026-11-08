# One image for every university service; the container's command picks the instance:
#   docker run ftuni:endterm payment-a
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements-runtime.txt .
RUN pip install -r requirements-runtime.txt

COPY ftuni ./ftuni
COPY experiments ./experiments
COPY deploy/healthcheck.sh ./deploy/healthcheck.sh

# least privilege: no root inside the container
RUN chmod 0755 deploy/healthcheck.sh && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

ENTRYPOINT ["python", "-m", "ftuni.run"]
