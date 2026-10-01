"""Skip work only when a complete git diff proves it contains documentation."""

import subprocess
import sys


def is_documentation(path):
    return path in {
        "README.md", "CHANGELOG.md", "design-qa.md",
        "install/README.md", "install/systemd/README.md",
    } or (path.startswith("docs/") and path.endswith(".md"))


def needs_checks(paths):
    # Empty or unavailable diffs must not silently bypass verification.
    return not paths or any(not is_documentation(path) for path in paths)


def changed_paths(base, head):
    result = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
        check=True, stdout=subprocess.PIPE,
    )
    return result.stdout.decode("utf-8").rstrip("\0").split("\0") if result.stdout else []


def main():
    try:
        run = needs_checks(changed_paths(sys.argv[1], sys.argv[2]))
    except (IndexError, OSError, subprocess.CalledProcessError, UnicodeError):
        run = True
    print("run=" + str(run).lower())


if __name__ == "__main__":
    main()
