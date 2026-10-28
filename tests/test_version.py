import subprocess
import sys
from importlib.metadata import version

import cic


def test_version() -> None:
    assert cic.__version__ == version("chia-internal-custody")
    assert cic.__version__ != "unknown"


def test_import_does_not_require_pkg_resources() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['pkg_resources'] = None; import cic"],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
