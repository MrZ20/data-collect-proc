FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY tasks/vllm-ascend-ci/pyproject.toml /tmp/task/pyproject.toml
COPY tasks/vllm-ascend-ci/src /tmp/task/src
RUN python -m venv /opt/venv \
    && pip install --no-cache-dir /tmp/task \
    && rm -rf /tmp/task \
    && groupadd --gid 10001 collector \
    && useradd --uid 10001 --gid 10001 --no-log-init --no-create-home collector \
    && mkdir /state \
    && chown collector:collector /state

USER 10001:10001
ENTRYPOINT ["ascend-ci"]
CMD ["--config", "/config.yaml", "collect"]
