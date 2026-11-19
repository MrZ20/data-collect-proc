FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 GIT_TERMINAL_PROMPT=0
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY tasks/github-work-watch/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tasks/github-work-watch/*.py .
USER 10001:10001
ENTRYPOINT ["python", "monitor.py"]
