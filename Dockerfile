# git >= 2.41 is required by ocr: Debian trixie ships 2.47 (bookworm ships 2.39).
FROM node:22-trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends git python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @alibaba-group/open-code-review@1.14.3 && ocr --version
WORKDIR /app
COPY app.py audit.py render.py ./
COPY prompts/ prompts/
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "app.py"]
