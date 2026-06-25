# go2-initial-test — cloud connectivity test (outbound reachability + optional push).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

ENV PORT=3617
# Reachability target (Wendy Cloud services), injected from the template var so
# the hostname isn't baked into source. Set CLOUD_PUSH_URL to also POST a JSON
# payload and assert a 2xx ingest.
ENV CLOUD_HEALTH_URL={{.CLOUD_HEALTH_URL}}
ENV CLOUD_PUSH_URL=
# Public endpoint for the raw internet-connectivity check (returns 204).
ENV INTERNET_URL=https://www.google.com/generate_204
EXPOSE 3617

CMD ["python", "app.py"]
