# ui — web dashboard for the policy runner (status + start/stop/e-stop).
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY static ./static

ENV PORT={{.WEB_PORT}} \
    RUNNER_PORT=3700
EXPOSE {{.WEB_PORT}}
CMD ["python", "main.py"]
