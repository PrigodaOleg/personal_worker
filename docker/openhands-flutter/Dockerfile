# Образ сервера OpenHands.
#
# Здесь Flutter НЕ нужен: сервер только управляет агентом и поднимает дочерний
# runtime-контейнер. Все команды (flutter test / flutter build) выполняются
# внутри runtime-образа — см. docker/runtime-flutter/Dockerfile.
FROM ghcr.io/all-hands-ai/openhands:latest

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория OpenHands (менять нельзя, ожидается образом).
WORKDIR /app
