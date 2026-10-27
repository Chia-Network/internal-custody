import re
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def workflow_files():
    return sorted(path for path in WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"})


def test_artifact_actions_are_not_deprecated():
    deprecated = re.compile(
        r"actions/(?:upload|download)-artifact@v[123](?![0-9])",
    )
    for path in workflow_files():
        matches = deprecated.findall(path.read_text(encoding="utf-8"))
        assert not matches, f"{path.name} uses a deprecated artifact action: {matches}"


def test_no_unsupported_manylinux_python_paths():
    unsupported = re.compile(
        r"/opt/python/cp3[0-8](?![0-9])-",
    )
    for path in workflow_files():
        matches = unsupported.findall(path.read_text(encoding="utf-8"))
        assert not matches, f"{path.name} references an unsupported manylinux Python: {matches}"
