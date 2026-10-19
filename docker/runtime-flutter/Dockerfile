# Образ дочернего runtime-контейнера, в котором агент реально выполняет команды.
#
# Важно: Flutter должен стоять ИМЕННО здесь, а не в образе сервера OpenHands.
# Сервер лишь управляет агентом, а `flutter test` / `flutter build` выполняются
# внутри runtime-контейнера, который OpenHands поднимает на каждую задачу.
FROM ghcr.io/all-hands-ai/runtime:oh_v0.59.0_1z87fcmwpofr5a4i

USER root

# Базовые утилиты + toolchain для `flutter build linux` (нужны cmake, ninja,
# clang, pkg-config, GTK) + JDK для `flutter build apk`. Без них агент
# натыкается на ошибки сборки и пытается доустанавливать пакеты сам.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    git \
    unzip \
    xz-utils \
    zip \
    ca-certificates \
    libglu1-mesa \
    cmake \
    ninja-build \
    clang \
    pkg-config \
    libgtk-3-dev \
    liblzma-dev \
    libstdc++-14-dev \
    default-jdk \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/default-java

ENV FLUTTER_HOME=/opt/flutter
ENV PATH="${FLUTTER_HOME}/bin:${FLUTTER_HOME}/bin/cache/dart-sdk/bin:${PATH}"

RUN git clone --depth 1 --branch stable https://github.com/flutter/flutter.git "${FLUTTER_HOME}"

# Flutter отказывается работать в git-репозитории с чужим владельцем и
# прогревает кеш только при первом вызове — делаем это на этапе сборки,
# чтобы агент не тратил на это время каждой задачи.
RUN git config --global --add safe.directory "${FLUTTER_HOME}" \
    && flutter config --no-analytics \
    && flutter precache --linux --universal \
    && flutter --version

# Android SDK для `flutter build apk`. Ставим commandline-tools и через них
# platform-tools/platforms/build-tools, иначе сборка падает с
# "No Android SDK found". Лицензии принимаем на этапе сборки, чтобы агент
# не зависал на интерактивном запросе.
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=${ANDROID_SDK_ROOT}
ENV PATH="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:${ANDROID_SDK_ROOT}/platform-tools:${PATH}"

RUN mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools" \
    && cd /tmp \
    && wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O cmdline-tools.zip \
    && unzip -q cmdline-tools.zip -d "${ANDROID_SDK_ROOT}/cmdline-tools" \
    && mv "${ANDROID_SDK_ROOT}/cmdline-tools/cmdline-tools" "${ANDROID_SDK_ROOT}/cmdline-tools/latest" \
    && rm cmdline-tools.zip \
    && yes | sdkmanager --licenses > /dev/null 2>&1 || true \
    && sdkmanager --install "platform-tools" "platforms;android-36" "build-tools;36.0.0" > /dev/null \
    && flutter config --android-sdk "${ANDROID_SDK_ROOT}"

# Команды агента выполняются в non-login shell, который не читает
# /etc/profile.d, поэтому ENV PATH до них не доходит. Кладём симлинки в
# /usr/local/bin (он уже в PATH по умолчанию) и дублируем в bash.bashrc,
# чтобы `flutter` работал без абсолютного пути.
RUN ln -sf "${FLUTTER_HOME}/bin/flutter" /usr/local/bin/flutter \
    && ln -sf "${FLUTTER_HOME}/bin/dart" /usr/local/bin/dart \
    && { \
        printf 'export FLUTTER_HOME=%s\n' "${FLUTTER_HOME}"; \
        printf 'export ANDROID_SDK_ROOT=%s\n' "${ANDROID_SDK_ROOT}"; \
        printf 'export ANDROID_HOME=%s\n' "${ANDROID_SDK_ROOT}"; \
        printf 'export JAVA_HOME=%s\n' "${JAVA_HOME}"; \
        printf 'export PATH="%s/bin:%s/bin/cache/dart-sdk/bin:%s/cmdline-tools/latest/bin:%s/platform-tools:$PATH"\n' \
            "${FLUTTER_HOME}" "${FLUTTER_HOME}" "${ANDROID_SDK_ROOT}" "${ANDROID_SDK_ROOT}"; \
    } | tee /etc/profile.d/flutter.sh >> /etc/bash.bashrc \
    && chmod +x /etc/profile.d/flutter.sh \
    && flutter --version
