from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_release_requires_trusted_pull_request_provenance() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "pull_request.head.repo.full_name == github.repository" in workflow
    assert "pull_request.user.login == 'github-actions[bot]'" in workflow
    assert "pull_request.user.type == 'Bot'" in workflow
    assert "pull_request.base.ref == 'main'" in workflow
    assert "pull_request.merge_commit_sha || 'main'" in workflow


def test_release_tags_are_created_once_without_force() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "git show-ref --verify --quiet" in workflow
    assert "git tag -a" in workflow
    assert "git tag -f" not in workflow
    assert 'git push -f origin "refs/tags/' not in workflow


def test_release_preparation_does_not_trust_user_created_tags() -> None:
    workflow = (ROOT / ".github/workflows/release-pr.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "push:\n    tags:" not in workflow
    assert "chore(release): prepare v$VERSION" in workflow
    assert "git ls-remote --exit-code --tags" in workflow
