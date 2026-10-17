FROM docker.all-hands.dev/all-hands-ai/openhands:0.35

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

# Установка Flutter SDK (stable)
ENV FLUTTER_VERSION=3.27.0
ENV FLUTTER_HOME=/opt/flutter
ENV PATH="${FLUTTER_HOME}/bin:${PATH}"

RUN curl -sSL "https://storage.googleapis.com/flutter_infra/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz" \
    | tar -xJ -C /opt

# Предварительная настройка Flutter (чтобы агент не тратил время на первый запуск)
RUN flutter config --no-analytics \
    && flutter doctor 2>&1 || true

# Рабочая директория OpenHands
WORKDIR /opt/openhands/workspace

USER openhands
