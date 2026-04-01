# Stage 1 — Build React frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 — FastAPI backend serving the built frontend
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN uv pip install --system fastapi==0.135.3 "uvicorn[standard]"
COPY app.py .
COPY --from=frontend /frontend/dist ./static
EXPOSE {{.PORT}}
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
