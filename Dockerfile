FROM python:3.14.10-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements-demo-api.txt ./
RUN python -m pip install --no-cache-dir -r requirements-demo-api.txt
COPY scripts/*.py ./scripts/

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "--app-dir", "scripts", "demo_draft_api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
