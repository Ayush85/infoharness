FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt requirements-browser.txt ./
RUN pip install -r requirements.txt
# Optional headless Chromium for JavaScript-only docs: docker compose build --build-arg WITH_BROWSER=1
ARG WITH_BROWSER=0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN if [ "$WITH_BROWSER" = "1" ]; then pip install -r requirements-browser.txt && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw-browsers && rm -rf /var/lib/apt/lists/*; fi

COPY app ./app
RUN useradd --create-home --uid 10001 harness && chown -R harness /srv
USER harness

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
