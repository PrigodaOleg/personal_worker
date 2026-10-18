FROM ghcr.io/all-hands-ai/openhands:latest

USER root

# Системные зависимости для Flutter
RUN apt-get update && apt-get install -y \
    curl \
    git \
    unzip \
    xz-utils \
    zip \
    libglu1-mesa \
    && rm -rf /var/lib/apt/lists/*

# Установка Flutter SDK (stable) через git
ENV FLUTTER_HOME=/opt/flutter
ENV PATH="${FLUTTER_HOME}/bin:${PATH}"

RUN git clone --depth 1 --branch stable https://github.com/flutter/flutter.git "${FLUTTER_HOME}"

# Рабочая директория OpenHands (оставляем /app, как в базовом образе)
WORKDIR /app
