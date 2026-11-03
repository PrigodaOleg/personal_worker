"""Синк из контейнера: удаления файлов, добавленных задачей, не теряются.

База диффа — merge-base HEAD с origin/main, а не origin/main. Файл,
добавленный коммитами задачи в PR-ветке (isar.wasm в PR #67) и затем
удалённый агентом, на фоне origin/main невидим: добавлено и удалено в
одной сессии → diff пуст → удаление не переносилось на хост, и блоб
оставался в PR после допуша.

Интеграционный тест: _sync_changes_from_runtime с подменённым
docker exec/docker cp (git «контейнера» эмулируется на фикстуре).
"""

from __future__ import annotations

import asyncio


from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


MERGE_BASE = "00d0d956bf2c06dfd9ae69f94adbdf9be9b4d10f"


class FakeGitContainer:
    """Эмуляция git в контейнере: файлы относительно «базы».

    Состояние: файл A изменён агентом, файл B (добавленный задачей)
    удалён агентом. На origin/main файла B нет — дифф против main
    его удаление НЕ показывает (история бага), против merge-base —
    показывает.
    """

    def __init__(self, base_files: dict[str, bytes]):
        self.base_files = base_files  # состояние на merge-base
        self.head_files = dict(base_files)

    def change(self, path: str, data: bytes) -> None:
        self.head_files[path] = data

    def delete(self, path: str) -> None:
        self.head_files.pop(path, None)

    @property
    def main_files(self) -> dict[str, bytes]:
        # origin/main: без файла, который добавила задача (B).
        return {k: v for k, v in self.base_files.items() if k != "B.txt"}

    def run(self, cmd: str) -> tuple[int, str, str]:
        if "merge-base" in cmd:
            return (0, MERGE_BASE + "\n", "")
        # Состояние базы зависит от того, против чего дифф: против
        # merge-base файл B (добавленный задачей) ЕСТЬ, против origin/main —
        # нет (именно поэтому баг #67 делал удаление невидимым).
        against_merge_base = MERGE_BASE[:7] in cmd
        base_state = self.base_files if against_merge_base else self.main_files
        if "diff-filter=ACMR" in cmd:
            changed = [
                p for p in self.head_files
                if p not in base_state or self.head_files[p] != base_state[p]
            ]
            return (0, "\0".join(changed) + ("\0" if changed else ""), "")
        if "diff-filter=D" in cmd:
            deleted = [p for p in base_state if p not in self.head_files]
            return (0, "\0".join(deleted) + ("\0" if deleted else ""), "")
        if "ls-files --others" in cmd:
            return (0, "", "")
        return (0, "", "")


def make_task(tmp_path) -> tuple[Scheduler, Task, FakeGitContainer]:
    git = FakeGitContainer({
        "A.txt": b"original\n",
        "B.txt": b"blob-added-by-task\n",  # isar.wasm в истории бага
    })
    git.change("A.txt", b"agent edit\n")
    git.delete("B.txt")  # агент удалил блоб

    s = object.__new__(Scheduler)
    s._container_ws = lambda t: "/workspace/social_mind"
    s._SECRET_FILE_NAMES = {"google-services.json", "build.env"}

    async def fake_run(container, cmd):
        return git.run(cmd)

    s._run_in_container = fake_run
    s._write_matched = Scheduler._write_matched.__get__(s)

    task = Task(
        id="tk1", source=TaskSource.GITHUB, source_id="67", title="t",
        description="", status=TaskStatus.DONE, conversation_id="conv-x",
        repo="PrigodaOleg/social_mind",
    )
    return s, task, git


def patch_docker(monkeypatch, git: FakeGitContainer):
    """docker exec → git.run; docker cp → отдаём файл из head_files."""

    class P:
        def __init__(self, rc=0, out=b"", err=b""):
            self.returncode = rc
            self._out, self._err = out, err

        async def communicate(self):
            return (self._out, self._err)

    async def fake_exec(*argv, **kw):
        args = list(argv)
        if args[:2] == ["docker", "exec"]:
            cmd = args[-1]
            # bash -lc '...' — берём последнюю строку
            code, out, err = git.run(cmd)
            return P(code, out.encode(), err.encode())
        if args[:2] == ["docker", "cp"]:
            # args[2] = "container:/workspace/social_mind/<path>"; args[3] = tmp
            src = args[2]
            path = src.split("/workspace/social_mind/", 1)[1]
            data = git.head_files.get(path)
            if data is None:
                return P(1, b"", b"no such file")
            with open(args[3], "wb") as f:
                f.write(data)
            return P()
        return P(0)

    async def _exec(*a, **kw):
        return await fake_exec(*a, **kw)

    import orchestrator.core.scheduler as sched

    class FakeProcFactory:
        @staticmethod
        async def create_subprocess_exec(*a, **kw):
            return await _exec(*a, **kw)

    monkeypatch.setattr(sched.asyncio, "create_subprocess_exec", _exec)


def test_deletion_of_task_added_file_is_synced(tmp_path, monkeypatch):
    """Регрессия PR #67: удаление файла, добавленного задачей, переносится."""
    s, task, git = make_task(tmp_path)
    patch_docker(monkeypatch, git)

    client_dir = tmp_path / "client"
    client_dir.mkdir()
    # Хост-копия: блоб ещё на месте (он уехал с предыдущим пушем).
    (client_dir / "B.txt").write_bytes(b"blob-added-by-task\n")
    (client_dir / "A.txt").write_bytes(b"original\n")

    res = asyncio.run(s._sync_changes_from_runtime(task, str(client_dir)))
    assert res["status"] == "ok", res
    assert res["removed"] == 1
    assert not (client_dir / "B.txt").exists()
    assert (client_dir / "A.txt").read_bytes() == b"agent edit\n"


def test_diff_base_is_merge_base(tmp_path, monkeypatch):
    """Команда диффа идёт против merge-base (fallback на origin/main —
    только если merge-base не удался)."""
    s, task, git = make_task(tmp_path)
    patch_docker(monkeypatch, git)

    client_dir = tmp_path / "client"
    client_dir.mkdir()
    (client_dir / "A.txt").write_bytes(b"x")
    (client_dir / "B.txt").write_bytes(b"y")

    cmds: list[str] = []
    real_run = s._run_in_container

    async def spy(container, cmd):
        cmds.append(cmd)
        return await real_run(container, cmd)

    s._run_in_container = spy
    asyncio.run(s._sync_changes_from_runtime(task, str(client_dir)))
    assert any(MERGE_BASE[:7] in c and "diff-filter=D" in c for c in cmds)
    assert not any(
        "origin/main" in c and "diff-filter=D" in c and "merge-base" not in c
        for c in cmds
    )


def test_merge_base_failure_falls_back(tmp_path, monkeypatch):
    """Если merge-base не вычислился — синк работает против origin/main."""
    s, task, git = make_task(tmp_path)
    patch_docker(monkeypatch, git)

    async def broken_run(container, cmd):
        if "merge-base" in cmd:
            return (128, "", "fatal: not a git repo")
        return git.run(cmd)

    s._run_in_container = broken_run
    client_dir = tmp_path / "client"
    client_dir.mkdir()
    (client_dir / "A.txt").write_bytes(b"x")

    res = asyncio.run(s._sync_changes_from_runtime(task, str(client_dir)))
    assert res["status"] == "ok"
    # Против main удалённого файла нет — B не в списке удалений.
    assert res["removed"] == 0
