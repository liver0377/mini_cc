from __future__ import annotations

import subprocess

import pytest

from mini_cc.agent.worktree import WorktreeError, WorktreeService


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)
    (repo / "README.md").write_text("# test", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


@pytest.fixture
def service(git_repo):
    return WorktreeService(git_repo)


class TestWorktreeServiceCreate:
    def test_creates_worktree_dir(self, service, git_repo):
        wt = service.create("a1b2c3d4")
        assert wt.exists()
        assert wt.is_dir()
        assert (wt / "README.md").exists()

    def test_worktree_contains_project_files(self, service, git_repo):
        wt = service.create("deadbeef")
        assert (wt / "README.md").read_text(encoding="utf-8") == "# test"

    def test_worktree_path_format(self, service, git_repo):
        wt = service.create("abc12345")
        assert str(wt) == str(git_repo / ".mini_cc" / "worktrees" / "abc12345")

    def test_duplicate_id_raises(self, service):
        service.create("dup12345")
        with pytest.raises(WorktreeError, match="git worktree add failed"):
            service.create("dup12345")


class TestWorktreeServiceRemove:
    def test_removes_worktree(self, service):
        wt = service.create("rm000001")
        assert wt.exists()
        service.remove("rm000001")
        assert not wt.exists()

    def test_remove_nonexistent_no_error(self, service):
        service.remove("nonexist")

    def test_remove_and_reuse_id(self, service):
        service.create("reuse001")
        service.remove("reuse001")
        wt2 = service.create("reuse001")
        assert wt2.exists()


class TestWorktreeServiceOutput:
    def test_cleanup_output_removes_file(self, service):
        service.output_dir.mkdir(parents=True, exist_ok=True)
        out = service.output_dir / "abc12345.output"
        out.write_text("result", encoding="utf-8")
        service.cleanup_output("abc12345")
        assert not out.exists()

    def test_cleanup_output_nonexistent_no_error(self, service):
        service.cleanup_output("nonexist")

    def test_cleanup_all(self, service):
        service.create("cln00001")
        service.create("cln00002")
        service.output_dir.mkdir(parents=True, exist_ok=True)
        (service.output_dir / "cln00001.output").write_text("r1", encoding="utf-8")
        (service.output_dir / "cln00002.output").write_text("r2", encoding="utf-8")

        service.cleanup_all(["cln00001", "cln00002"])

        assert not (service.output_dir / "cln00001.output").exists()
        assert not (service.output_dir / "cln00002.output").exists()


class TestWorktreeServiceListActive:
    def test_empty_when_none(self, service):
        assert service.list_active() == []

    def test_lists_active_worktrees(self, service):
        service.create("lst00001")
        service.create("lst00002")
        active = service.list_active()
        assert "lst00001" in active
        assert "lst00002" in active

    def test_excludes_removed(self, service):
        service.create("exc00001")
        service.create("exc00002")
        service.remove("exc00001")
        active = service.list_active()
        assert "exc00001" not in active
        assert "exc00002" in active


class TestWorktreeServiceGitignore:
    def test_creates_gitignore(self, service, git_repo):
        service.ensure_gitignore()
        gitignore = git_repo / ".gitignore"
        assert gitignore.exists()
        assert ".mini_cc/worktrees/" in gitignore.read_text(encoding="utf-8")

    def test_appends_to_existing_gitignore(self, service, git_repo):
        gitignore = git_repo / ".gitignore"
        gitignore.write_text("*.pyc\n", encoding="utf-8")
        service.ensure_gitignore()
        content = gitignore.read_text(encoding="utf-8")
        assert "*.pyc" in content
        assert ".mini_cc/worktrees/" in content

    def test_idempotent(self, service, git_repo):
        service.ensure_gitignore()
        service.ensure_gitignore()
        content = (git_repo / ".gitignore").read_text(encoding="utf-8")
        assert content.count(".mini_cc/worktrees/") == 1
