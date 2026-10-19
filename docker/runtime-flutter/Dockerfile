# Образ дочернего runtime-контейнера, в котором агент реально выполняет команды.
#
# Важно: Flutter должен стоять ИМЕННО здесь, а не в образе сервера OpenHands.
# Сервер лишь управляет агентом, а `flutter test` / `flutter build` выполняются
# внутри runtime-контейнера, который OpenHands поднимает на каждую задачу.
FROM ghcr.io/all-hands-ai/runtime:oh_v0.59.0_1z87fcmwpofr5a4i

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    unzip \
    xz-utils \
    zip \
    libglu1-mesa \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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

# Команды агента выполняются в non-login shell, который не читает
# /etc/profile.d, поэтому ENV PATH до них не доходит. Кладём симлинки в
# /usr/local/bin (он уже в PATH по умолчанию) и дублируем в bash.bashrc,
# чтобы `flutter` работал без абсолютного пути.
RUN ln -sf "${FLUTTER_HOME}/bin/flutter" /usr/local/bin/flutter \
    && ln -sf "${FLUTTER_HOME}/bin/dart" /usr/local/bin/dart \
    && printf 'export PATH="%s/bin:%s/bin/cache/dart-sdk/bin:$PATH"\n' "${FLUTTER_HOME}" "${FLUTTER_HOME}" \
       | tee /etc/profile.d/flutter.sh >> /etc/bash.bashrc \
    && chmod +x /etc/profile.d/flutter.sh \
    && flutter --version
