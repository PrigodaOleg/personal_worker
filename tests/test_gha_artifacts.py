"""Тесты секции артефактов GitHub Actions в финальном уведомлении.

Финальный комментарий к задаче должен ссылаться на артефакты, которые
соберёт GHA: имена парсятся из workflow на нашей ветке, прямые ссылки
дописываются в тот же комментарий после завершения сборки.
"""

import os

from orchestrator.core.scheduler import Scheduler


def _make_workspace(tmp_path: str, workflows: dict[str, str]) -> str:
    wf_dir = os.path.join(tmp_path, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    for fn, text in workflows.items():
        with open(os.path.join(wf_dir, fn), "w", encoding="utf-8") as f:
            f.write(text)
    return tmp_path


DART_YML = """
name: Closers Flutter
on:
  pull_request:
jobs:
  build-android:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/upload-artifact@v4
        with:
          name: Closers
          path: build/app/outputs/flutter-apk/
  build-ios:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - name: Upload iOS artifact
        uses: actions/upload-artifact@v4
        with:
          name: Closers-iOS
          path: build/ios/iphoneos/Runner.app
      # имя с выражением заранее не вычислить — пропустить
      - uses: actions/upload-artifact@v4
        with:
          name: report-${{ github.run_id }}
"""


def test_parse_workflow_artifacts(tmp_path):
    """Имена upload-artifact из всех workflow, без выражений, без дублей."""
    ws = _make_workspace(str(tmp_path), {"dart.yml": DART_YML})
    assert Scheduler.parse_workflow_artifacts(ws) == ["Closers", "Closers-iOS"]


def test_parse_workflow_artifacts_empty(tmp_path):
    """Нет workflow/нет артефактов — пустой список (секция не добавляется)."""
    assert Scheduler.parse_workflow_artifacts(str(tmp_path)) == []
    ws = _make_workspace(str(tmp_path / "b"), {"plain.yml": "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n"})
    assert Scheduler.parse_workflow_artifacts(ws) == []
    # битый yaml не роняет парсер
    ws2 = _make_workspace(str(tmp_path / "c"), {"bad.yml": ":\n::\n  ["})
    assert Scheduler.parse_workflow_artifacts(ws2) == []


def test_replace_marker_section():
    """Фоновое дописывание меняет только секцию между маркерами."""
    head = "✅ Задача выполнена! PR создан: https://github.com/r/p/pull/1\nСтоимость: 0.8 ₽\n\n"
    old_section = (
        "<!-- gha-artifacts-start -->\n"
        "📦 Артефакты GHA (собираются в CI):\n"
        "  • Closers-iOS\n"
        "_(прямые ссылки появятся позже)_\n"
        "<!-- gha-artifacts-end -->"
    )
    body = head + old_section
    new_section = (
        "<!-- gha-artifacts-start -->\n"
        "📦 Артефакты GHA (готовы):\n"
        "  • [Closers-iOS](https://github.com/r/p/actions/runs/1/artifacts/9) — 10.9 МБ\n"
        "<!-- gha-artifacts-end -->"
    )
    out = Scheduler.replace_marker_section(body, new_section)
    assert out.startswith(head)
    assert "готовы" in out
    assert "появятся позже" not in out
    assert out.count("gha-artifacts") == 2  # маркеры ровно одна пара


def test_replace_marker_section_without_markers():
    """Нет маркеров (комментарий без секции) — тело не ломаем."""
    body = "просто текст"
    assert Scheduler.replace_marker_section(body, "NEW") == body


def test_format_artifacts_section_success():
    """Успешная сборка: прямые ссылки на артефакты + размер."""
    section = Scheduler.format_artifacts_section(
        run_id=42,
        run_url="https://github.com/o/r/actions/runs/42",
        conclusion="success",
        artifacts=[
            {"id": 9, "name": "Closers-iOS", "size_in_bytes": 10_918_060},
            {"id": 8, "name": "Closers", "size_in_bytes": 28_870_812},
        ],
        repo_full_name="o/r",
    )
    assert "[Closers-iOS](https://github.com/o/r/actions/runs/42/artifacts/9) — 10.9 МБ" in section
    assert "[Closers](https://github.com/o/r/actions/runs/42/artifacts/8) — 28.9 МБ" in section
    assert "✅" in section
    assert section.startswith("<!-- gha-artifacts-start -->")


def test_format_artifacts_section_failed_and_empty():
    """Ошибка сборки и успех без артефактов — честный вердикт без ссылок."""
    failed = Scheduler.format_artifacts_section(
        42, "https://x/42", "failure", [], "o/r"
    )
    assert "failure" in failed and "готовы" not in failed
    empty = Scheduler.format_artifacts_section(
        42, "https://x/42", "success", [], "o/r"
    )
    assert "артефактов нет" in empty
