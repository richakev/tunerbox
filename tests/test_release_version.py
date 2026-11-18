"""Exercise the desktop workflow's version step with a shallow checkout and real tags."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def checkout(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    for name in ("packaging/set_version.py", "tunerbox/__init__.py"):
        target = origin / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    git(origin, "add", ".")
    git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "Initial version")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init")
    git(work, "remote", "add", "origin", origin.as_uri())
    git(work, "fetch", "--depth=1", "--no-tags", "origin", "HEAD")
    git(work, "checkout", "--detach", "FETCH_HEAD")
    return origin, work


@pytest.fixture(params=["windows", "macos", "linux"])
def version_step(request):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    return next(
        step["run"] for step in workflow["jobs"][request.param]["steps"]
        if step.get("name") == "Set version"
    )


def run_step(work, step, is_tag=False, ref="feature/test"):
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step],
        cwd=work, capture_output=True, text=True,
        env={**os.environ, "IS_TAG": str(is_tag).lower(), "GITHUB_REF_NAME": ref,
             "GITHUB_SHA": "abcdef0123456789", "GITHUB_ENV": str(work / "build.env")},
    )


@pytest.mark.parametrize("tags,expected", [
    (["1.0", "1.4.1", "1.5", "1.6"], "1.6.0"),
    (["v1.9.9", "v1.10.0"], "1.10.0"),
    (["v1.9.0", "1.10", "v99.0.0-rc1", "notes", "2.0.invalid"], "1.10.0"),
    (["1.4.1"], "1.4.1"),
])
def test_branch_version(checkout, version_step, tags, expected):
    origin, work = checkout
    for tag in tags:
        git(origin, "tag", tag)
    result = run_step(work, version_step)
    assert result.returncode == 0, result.stderr
    assert f'__version__ = "{expected}"' in (work / "tunerbox/__init__.py").read_text()
    assert (work / "build.env").read_text().strip() == "BUILD_LABEL=-feature-test-abcdef0"


def test_tag_version_does_not_fetch(checkout, version_step):
    _, work = checkout
    git(work, "remote", "remove", "origin")
    result = run_step(work, version_step, is_tag=True, ref="v2.3.4")
    assert result.returncode == 0, result.stderr
    assert '__version__ = "2.3.4"' in (work / "tunerbox/__init__.py").read_text()
    assert not (work / "build.env").exists()


@pytest.mark.parametrize("tags", [[], ["notes", "v2.0.0-rc1"]])
def test_no_release_tags_fails_without_changing_version(checkout, version_step, tags):
    origin, work = checkout
    for tag in tags:
        git(origin, "tag", tag)
    before = (work / "tunerbox/__init__.py").read_text()
    result = run_step(work, version_step)
    assert result.returncode != 0
    if tags:
        assert "Version must look like" in result.stderr
    assert (work / "tunerbox/__init__.py").read_text() == before
    assert not (work / "build.env").exists()
