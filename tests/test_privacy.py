"""Nothing in the repo names a machine, a person or a local path: hostnames, usernames, IPs, home directories."""

import getpass
import os
import re
import socket
from pathlib import Path

from gauntlet import files

REPO = Path(__file__).resolve().parent.parent
_HOME = re.compile(r"(?:/Users|/home)/[A-Za-z0-9._-]+|[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+(?:com|org|net|io|dev|edu|gov|me|ai|app|co|us|uk|de)\b")
_LAN_HOST = re.compile(r"\b[A-Za-z0-9-]+\.(?:local|lan|internal|home|corp|ts\.net)\b")
_SAFE_IPS = {"127.0.0.1", "0.0.0.0"}
_SAFE_EMAIL = re.compile(r"(?:noreply@[\w.-]+|[\w.+-]+@example\.(?:org|com|net))$")
_COMMON = {"runner", "root", "user", "admin", "localhost", "ubuntu", "mac", "test"}


def _local_words():
    # This machine's own names: whoever runs the suite, CI or a laptop, checks for their own leak.
    words = {getpass.getuser(), Path(os.path.expanduser("~")).name, socket.gethostname().split(".")[0]}
    return {w.lower() for w in words if len(w) >= 4 and w.lower() not in _COMMON}


def findings(text, rel, words=()):
    out = [f"{rel}: home path {m}" for m in _HOME.findall(text)]
    out += [f"{rel}: IP address {m}" for m in _IPV4.findall(text)
            if m not in _SAFE_IPS and all(int(p) <= 255 for p in m.split("."))]
    out += [f"{rel}: email {m}" for m in _EMAIL.findall(text) if not _SAFE_EMAIL.match(m)]
    out += [f"{rel}: LAN hostname {m}" for m in _LAN_HOST.findall(text)]
    for w in words:
        if re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", text):
            out.append(f"{rel}: names this machine or its user ({w})")
    return out


def test_each_kind_of_identifier_is_found():
    home, win, ip = "/" + "Users/someone", "C:" + "\\Users\\someone", "192" + ".168.1.20"
    mail, host = "someone" + "@" + "gmail.com", "studio" + ".local"
    assert findings(f"cd {home}/projects", "a") == [f"a: home path {home}"]
    assert findings(win + "\\x", "a") == [f"a: home path {win}"]
    assert findings(f"http://{ip}:8080/", "a") == [f"a: IP address {ip}"]
    assert findings(f"mail {mail}", "a") == [f"a: email {mail}"]
    assert findings(f"ssh {host}", "a") == [f"a: LAN hostname {host}"]
    assert findings("ssh studio", "a", {"studio"}) == ["a: names this machine or its user (studio)"]


def test_ordinary_text_is_left_alone():
    text = ("http://127.0.0.1:8080 version 3.12.1 ~/.claude/skills/gauntlet noreply@anthropic.com "
            "t@example.org localhost studios \\n@pytest.mark.gauntlet( os.path.expanduser('~') 1.2.3.4.5")
    assert findings(text, "a", {"studio"}) == []


def test_the_repo_names_no_host_user_ip_or_home_path():
    words = _local_words()
    out = []
    for path in files.publishable(REPO):
        if path.is_file() and path.suffix not in (".lock",):
            out += findings(path.read_text(encoding="utf-8", errors="replace"), path.relative_to(REPO).as_posix(), words)
    assert out == []
