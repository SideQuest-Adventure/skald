"""Invalid launch arguments must fail before opening audio or writing user files."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("arguments, error", [
    (["--max-seconds", "0"], "--max-seconds must be positive"),
    (["--threshold", "nan"], "--threshold must be greater than 0"),
    (["--threshold", "-1"], "--threshold must be greater than 0"),
])
def test_invalid_audio_arguments_fail_before_configuration_write(tmp_path, arguments, error):
    config = tmp_path / "settings.toml"
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "skald.py"),
                             "--config", str(config), *arguments],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 2
    assert error in result.stderr
    assert not config.exists()
