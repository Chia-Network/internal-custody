import re
from pathlib import Path

import yaml

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


def test_python_matrix_matches_classifiers():
    """CI's Python matrix must match the Python classifiers declared in setup.py."""
    root = WORKFLOW_DIR.parents[1]
    workflow = yaml.safe_load((WORKFLOW_DIR / "run-test-suite.yml").read_text(encoding="utf-8"))
    setup_py = (root / "setup.py").read_text(encoding="utf-8")

    matrix_lists = [
        job["strategy"]["matrix"]["python-version"]
        for job in workflow["jobs"].values()
        if isinstance(job, dict)
        and isinstance(job.get("strategy"), dict)
        and isinstance(job["strategy"].get("matrix"), dict)
        and "python-version" in job["strategy"]["matrix"]
    ]
    assert (
        len(matrix_lists) == 1
    ), f"run-test-suite.yml must define exactly one python-version matrix list, found {len(matrix_lists)}"
    matrix = matrix_lists[0]
    assert isinstance(matrix, list), "run-test-suite.yml python-version matrix must be a list"

    classifiers = re.findall(r'"Programming Language :: Python :: 3\.(\d+)"', setup_py)
    assert classifiers, "setup.py is missing Python 3 classifiers"

    matrix_versions = sorted(tuple(int(part) for part in str(version).split(".")) for version in matrix)
    classifier_versions = sorted((3, int(minor)) for minor in classifiers)
    expected_versions = [(3, 11), (3, 12), (3, 13), (3, 14)]
    assert (
        matrix_versions == classifier_versions
    ), f"CI Python matrix {matrix} does not match setup.py classifiers {classifiers}"
    assert matrix_versions == expected_versions, f"CI Python matrix {matrix} must cover 3.11-3.14"

    python_requires_match = re.search(r"python_requires\s*=\s*[\"']>=3\.(\d+)[\"']", setup_py)
    assert python_requires_match, "setup.py is missing a python_requires lower bound of the form >=3.X"
    lower_bound = (3, int(python_requires_match.group(1)))
    assert (
        min(matrix_versions) == lower_bound
    ), f"Lowest CI Python version {matrix_versions[0]} does not match python_requires >=3.{lower_bound[1]}"


def test_no_unsupported_manylinux_python_paths():
    unsupported = re.compile(
        r"/opt/python/cp3[0-8](?![0-9])-",
    )
    for path in workflow_files():
        matches = unsupported.findall(path.read_text(encoding="utf-8"))
        assert not matches, f"{path.name} references an unsupported manylinux Python: {matches}"
