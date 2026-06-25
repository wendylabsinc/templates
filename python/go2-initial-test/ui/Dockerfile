# go2-initial-test dashboard — aggregates every test service into a go/no-go board.
# network: host, so it reaches each test on 127.0.0.1:<port>.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
COPY static ./static

ENV PORT={{.UI_PORT}}
EXPOSE {{.UI_PORT}}

CMD ["python", "main.py"]
