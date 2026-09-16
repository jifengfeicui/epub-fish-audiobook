FROM node:22-alpine AS frontend-build

WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /alexandria

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /alexandria/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ /alexandria/backend/
COPY app/ /alexandria/app/
COPY fish_adapter/ /alexandria/fish_adapter/
COPY tools/ /alexandria/tools/
COPY default_prompts.txt review_prompts.txt /alexandria/
COPY --from=frontend-build /build/frontend/dist /alexandria/frontend/dist

RUN mkdir -p /alexandria/data

ENV ALEXANDRIA_HOST=0.0.0.0
ENV ALEXANDRIA_DATA_DIR=/alexandria/data
EXPOSE 4200

CMD ["python", "-m", "backend.alexandria.main"]
