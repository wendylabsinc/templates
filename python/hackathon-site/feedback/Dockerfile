# Q&A / bug-report inbox for the hackathon page. slim → prebuilt arm64 wheels.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PORT=8090 DATA_DIR=/app/data ADMIN_TOKEN={{.ADMIN_TOKEN}}
EXPOSE 8090
CMD ["python", "app.py"]
