# Stage 1 — Build frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 — Build backend
FROM node:22-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

# Stage 3 — Production
FROM node:22-slim
WORKDIR /app
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
COPY --from=frontend /frontend/dist ./static
EXPOSE {{.PORT}}
CMD ["node", "dist/index.js"]
