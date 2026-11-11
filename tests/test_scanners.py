"""Tier-1 scanners, each proven to fire and to stay quiet."""

from gauntlet import scanners


def _quiet_then_fires(scan, innocent, bad, rel):
    # Quiet on the innocent text, and firing once the bad line joins it: quiet for the right reason.
    assert scan(innocent, rel) == []
    assert scan(innocent + bad, rel) != []


def test_a_mutable_default_argument_is_found():
    src = "def add(x, acc=[]):\n    return acc\n\ndef put(k, *, into=dict()):\n    return into\n"
    got = scanners.mutable_defaults(src, "m.py")
    assert [f.split(":")[1] for f in got] == ["1", "4"]

def test_a_swallowed_error_is_found():
    src = "try:\n    go()\nexcept Exception:\n    pass\ntry:\n    go()\nexcept:\n    log()\n"
    assert [f.split(":")[1] for f in scanners.swallowed_python(src, "m.py")] == ["3", "7"]
    sh = "#!/usr/bin/env bash\nrm -rf \"$d\" || true\nkill \"$p\" || :\n"
    assert [f.split(":")[1] for f in scanners.swallowed_shell(sh, "s.sh")] == ["2", "3"]

def test_an_unchecked_exit_status_is_found():
    sh = "#!/usr/bin/env bash\nset -u\nproduce | consume\ncd \"$dir\"\nrm -rf ./out\n"
    got = scanners.unchecked_status_shell(sh, "s.sh")
    assert any("pipefail" in f for f in got)
    assert any(":4:" in f and "cd" in f for f in got)

def test_a_fixed_resource_in_a_test_is_found():
    src = ("import socket\nfrom http.server import HTTPServer\n"
           "s = socket.socket()\ns.bind((\"127.0.0.1\", 49221))\n"
           "srv = HTTPServer((\"127.0.0.1\", 8080), H)\n"
           "Path(\"/tmp/lh-test.lock\").write_text(\"x\", encoding=\"utf-8\")\n"
           "open(\"/tmp/out.txt\", \"w\", encoding=\"utf-8\")\n")
    got = scanners.fixed_resources(src, "tests/test_x.py")
    assert [f.split(":")[1] for f in got] == ["4", "5", "6", "7"]

def test_an_inventory_above_its_pin_fails_and_names_the_count():
    msg = scanners.ratchet(["a", "b", "c"], 2, "swallowed errors")
    assert msg and "3" in msg and "2" in msg

def test_innocent_defaults_are_left_alone():
    innocent = ("def f(x=None, y=(), z=frozenset(), *, w=0, name=\"a\"):\n    acc = []\n    return acc\n"
                "g = lambda k=(): k\n")
    _quiet_then_fires(scanners.mutable_defaults, innocent, "def h(a={}):\n    return a\n", "m.py")

def test_a_handled_error_is_left_alone():
    innocent = ("try:\n    go()\nexcept OSError as e:\n    raise SystemExit(str(e)) from e\n"
                "try:\n    go()\nexcept ValueError:\n    n = 0\n    log(n)\n")
    _quiet_then_fires(scanners.swallowed_python, innocent, "try:\n    go()\nexcept KeyError:\n    pass\n", "m.py")
    sh = ("#!/usr/bin/env bash\n# a comment saying || true is fine\nrm -rf \"$d\" || exit 1\n"
          "[ -f x ] || truncate_log\nfoo || true_name\n")
    _quiet_then_fires(scanners.swallowed_shell, sh, "kill \"$p\" || true\n", "s.sh")

def test_a_checked_exit_status_is_left_alone():
    strict = "#!/usr/bin/env bash\nset -euo pipefail\nproduce | consume\ncd \"$dir\"\n"
    assert scanners.unchecked_status_shell(strict, "s.sh") == []
    loose = ("#!/usr/bin/env bash\nset -uo pipefail\nproduce | consume\ncd \"$dir\" || exit 1\n"
             "if cd \"$x\" && make; then :; fi\n[ a ] || [ b ]\necho cdrom\n")
    _quiet_then_fires(scanners.unchecked_status_shell, loose, "cd \"$dir\"\n", "s.sh")
    probe = ("#!/usr/bin/env bash\nset -u\nif fc-list 2>/dev/null | grep -qi dejavu; then ok; fi\n"
             "need uv \"curl -LsSf https://example.org/install.sh | sh\"\nhint 'a | b'\n")
    _quiet_then_fires(scanners.unchecked_status_shell, probe, "fc-list | sort > fonts.txt\n", "s.sh")
    sourced = "build() {\n  produce | consume\n}\n"
    assert scanners.unchecked_status_shell(sourced, "lib.sh") == []
    assert scanners.unchecked_status_shell("#!/bin/sh\n" + sourced, "x.sh") != []

def test_resources_the_os_hands_out_are_left_alone():
    innocent = ("s.bind((\"127.0.0.1\", 0))\nsrv = HTTPServer((\"127.0.0.1\", 0), H)\n"
                "url = \"http://127.0.0.1:8080/v1\"\nsecurity(host=\"0.0.0.0\", port=8899)\n"
                "(tmp_path / \"x\").write_text(\"y\", encoding=\"utf-8\")\n"
                "argv(Path(\"/tmp/o.wav\"))\nopen(\"/etc/hosts\", encoding=\"utf-8\")\n"
                "Path(\"/tmp/x\").read_text(encoding=\"utf-8\")\n")
    _quiet_then_fires(scanners.fixed_resources, innocent, "s.bind((\"\", 49222))\n", "tests/test_x.py")

def test_a_posix_only_primitive_is_found():
    src = "import os, signal\nimport fcntl\ndef stop(p):\n    os.killpg(p.pid, signal.SIGKILL)\n"
    assert [f.split(":")[1] for f in scanners.posix_primitives(src, "m.py")] == ["2", "4", "4"]

def test_a_posix_primitive_behind_a_platform_guard_is_left_alone():
    innocent = ("import os, signal, sys\n"
                "def take(fd):\n    if sys.platform == \"win32\":\n        import msvcrt\n"
                "        try:\n            return msvcrt.locking(fd, 1, 1)\n        except OSError:\n"
                "            return False\n    import fcntl\n    return fcntl.flock(fd, 1)\n"
                "def stop(p):\n    if hasattr(os, \"killpg\"):\n        os.killpg(p.pid, signal.SIGTERM)\n"
                "    else:\n        p.terminate()\n"
                "try:\n    import pwd\nexcept ImportError:\n    pwd = None\n"
                "SIG = signal.SIGKILL if os.name == \"posix\" else signal.SIGTERM\n")
    _quiet_then_fires(scanners.posix_primitives, innocent, "os.killpg(1, signal.SIGTERM)\n", "m.py")

def test_a_bare_shell_in_a_subprocess_argv_is_found():
    src = ("subprocess.run([\"bash\", str(p)], check=True)\nsubprocess.Popen(\n    [\"zsh\", \"-c\", x])\n")
    assert [f.split(":")[1] for f in scanners.bare_shells(src, "t.py")] == ["1", "2"]

def test_a_resolved_shell_is_left_alone():
    innocent = ("subprocess.run([shells.BASH, str(p)])\nsubprocess.run([sys.executable, \"-c\", \"bash\"])\n"
                "x = [\"bash\", \"zsh\"]\n")
    _quiet_then_fires(scanners.bare_shells, innocent, "subprocess.check_output([\"sh\", \"-c\", c])\n", "t.py")

def test_a_file_grown_by_truncate_is_found():
    src = ("with open(p, \"wb\") as fh:\n    fh.truncate(18 * GIB)\n"
           "os.truncate(p, size)\nf.truncate(length=4 << 30)\n")
    assert [f.split(":")[1] for f in scanners.grown_files(src, "t.py")] == ["2", "3", "4"]

def test_a_small_or_shrinking_truncate_is_left_alone():
    innocent = "fh.seek(0)\nfh.truncate()\nfh.truncate(0)\nos.truncate(p, 1024)\n"
    _quiet_then_fires(scanners.grown_files, innocent, "fh.truncate(60 * GIB)\n", "t.py")

def test_a_text_mode_tempfile_without_an_encoding_is_found():
    src = ("tempfile.NamedTemporaryFile(\"w\", suffix=\".svg\")\nTemporaryFile(\"w+\")\n"
           "tempfile.SpooledTemporaryFile(mode=\"r+\")\n")
    assert [f.split(":")[1] for f in scanners.text_tempfiles(src, "t.py")] == ["1", "2", "3"]

def test_a_binary_or_encoded_tempfile_is_left_alone():
    innocent = ("tempfile.NamedTemporaryFile(suffix=\".wav\")\ntempfile.TemporaryFile(\"w+b\")\n"
                "tempfile.NamedTemporaryFile(\"w\", encoding=\"utf-8\")\n")
    _quiet_then_fires(scanners.text_tempfiles, innocent, "tempfile.TemporaryFile(\"w+\")\n", "t.py")

def test_a_native_relative_path_key_is_found():
    src = ("found.add(str(f.relative_to(REPO)))\nwhere = f\"{p.relative_to(root)}:{n}\"\n"
           "k = [str(q.relative_to(REPO)) for q in qs]\n")
    assert [f.split(":")[1] for f in scanners.native_relpaths(src, "t.py")] == ["1", "2", "3"]

def test_a_posix_relative_path_key_is_left_alone():
    innocent = ("found.add(f.relative_to(REPO).as_posix())\nwhere = f\"{p.relative_to(root).as_posix()}:{n}\"\n"
                "parts = f.relative_to(REPO).parts\nsame = sorted(p.relative_to(a) for p in ps)\nname = str(p)\n")
    _quiet_then_fires(scanners.native_relpaths, innocent, "k = str(p.relative_to(r))\n", "t.py")

def test_an_unguarded_import_is_found_and_a_guarded_one_is_not():
    src = ("import yaml\ntry:\n    import torch\nexcept ImportError:\n    torch = None\n"
           "def f():\n    from huggingface_hub import snapshot_download\n    from . import local\n")
    assert scanners.unguarded_imports(src, "m.py") == [(1, "yaml"), (7, "huggingface_hub")]

def test_a_module_level_importorskip_is_read_as_a_skip():
    src = "import pytest\ntorch = pytest.importorskip(\"torch.nn\", reason=\"x\")\ndef t():\n    pytest.importorskip(\"mlx\")\n"
    assert scanners.module_skips(src, "t.py") == {"torch"}

def test_an_inventory_at_its_pin_is_quiet_and_below_it_asks_for_a_lower_pin():
    assert scanners.ratchet(["a", "b"], 2, "x") is None
    msg = scanners.ratchet(["a"], 2, "x")
    assert msg and "lower the pin to 1" in msg
