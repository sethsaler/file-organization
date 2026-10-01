import subprocess

from ci_scope import changed_paths, main, needs_checks


def test_only_known_documentation_skips():
    assert not needs_checks(["README.md", "CHANGELOG.md", "docs/testing.md"])
    assert not needs_checks(["install/README.md", "install/systemd/README.md"])


def test_source_and_unknown_paths_run():
    for path in ("scripts/org_safety.py", "tests/test_ci_scope.py", "pyproject.toml",
                 "uv.lock", ".github/workflows/test.yml", "macos/FileOrganizerMenuBar.swift",
                 "new-config.json", "docs/fixture.json", "tests/fixture.md", "SKILL.md"):
        assert needs_checks(["README.md", path])
    assert needs_checks([])


def test_renames_include_source_and_destination(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
    (tmp_path / "source.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(["git", "commit", "-qm", "source"], check=True)
    subprocess.run(["git", "mv", "source.py", "README.md"], check=True)
    subprocess.run(["git", "commit", "-qm", "rename"], check=True)
    assert needs_checks(changed_paths("HEAD~1", "HEAD"))


def test_invalid_diff_fails_safe(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ci_scope.py", "nonexistent-base", "HEAD"])
    main()
    assert capsys.readouterr().out == "run=true\n"


def test_missing_diff_fails_safe(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ci_scope.py"])
    main()
    assert capsys.readouterr().out == "run=true\n"
