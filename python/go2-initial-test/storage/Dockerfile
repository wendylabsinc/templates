# go2-initial-test — persistent storage test (write/read a persist volume).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

ENV PORT=3619
ENV DATA_DIR=/data
EXPOSE 3619

CMD ["python", "app.py"]
