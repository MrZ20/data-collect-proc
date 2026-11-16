FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv \
    && groupadd --gid 10001 collector \
    && useradd --uid 10001 --gid 10001 --no-log-init --no-create-home collector \
    && mkdir /state && chown collector:collector /state
COPY tasks/vllm-ascend-changes/pyproject.toml /tmp/task/pyproject.toml
COPY tasks/vllm-ascend-changes/src /tmp/task/src
RUN pip install --no-cache-dir /tmp/task && rm -rf /tmp/task
USER 10001:10001
ENTRYPOINT ["ascend-changes"]
