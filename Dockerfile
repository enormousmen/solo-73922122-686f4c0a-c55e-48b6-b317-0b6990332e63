FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OCI_LAYOUT_DIR=/oci-layout \
    OCI_STANDBY_LAYOUT_DIR=/oci-layout-standby \
    STATE_DIR=/state \
    OCI_INDEX_REF=registry.example.com/edge/stable:latest

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY examples ./examples

# Both layouts are mounted read-only at runtime; /state is the only writable
# path.  Pre-create the mount points (and /state) with the runtime user's
# ownership so Docker seeds named volumes with usable permissions.
RUN mkdir -p /state /oci-layout /oci-layout-standby \
    && chown -R 10001:10001 /state /oci-layout /oci-layout-standby

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=12 \
    CMD python -c "import json,urllib.request,sys; \
sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz'))['status']=='ok' else 1)"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
