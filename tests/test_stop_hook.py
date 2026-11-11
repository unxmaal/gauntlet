"""The corpus's Stop hook passes its own red-proof script."""

import shutil
import subprocess
from pathlib import Path

import pytest

CORPUS = Path(__file__).resolve().parent.parent / "corpus"


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("jq") and shutil.which("shasum")),
                    reason="the hook needs bash, jq and shasum")
def test_the_stop_hook_red_proof_passes():
    out = subprocess.run(["bash", str(CORPUS / "test-stop-hook.sh")], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "all cases pass" in out.stdout
