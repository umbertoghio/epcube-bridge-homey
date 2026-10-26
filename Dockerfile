FROM python:3.12-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml constraints.txt THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -c constraints.txt .

FROM builder AS test
COPY tests ./tests
RUN /opt/venv/bin/pip install --no-cache-dir -c constraints.txt '.[test]'
RUN /opt/venv/bin/pytest -q

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 bridge && useradd --uid 10001 --gid bridge --no-create-home bridge \
    && mkdir /data && chown bridge:bridge /data && chmod 700 /data
COPY --from=builder /opt/venv /opt/venv
COPY THIRD_PARTY_NOTICES.md /opt/THIRD_PARTY_NOTICES.md
USER 10001:10001
WORKDIR /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).close()"]
CMD ["epcube-bridge"]
