"""evo_harness 预信任（pretrust）回归——隔离 HOME，不碰真实信任存储。"""

import hashlib
import json

from evo_harness import pretrust
from evo_harness.pretrust import kimi_workspace_key, pretrust_all


def test_kimi_workspace_key_known_pairs():
    # 本机实证配对（2026-08-24）：sha256(root)[:12]
    assert kimi_workspace_key(__import__("pathlib").Path("/mnt/d/cloud")).endswith(
        hashlib.sha256(b"/mnt/d/cloud").hexdigest()[:12]
    )
    key = kimi_workspace_key(__import__("pathlib").Path("/home/ray/ProjectEvo"))
    assert key == "wd_ProjectEvo_610803c31eac"


def test_pretrust_all_writes_then_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Path.home() 在 POSIX 按 HOME 解析
    target = tmp_path / "proj"
    target.mkdir()

    wrote = pretrust_all(target)
    assert wrote == {"codex": True, "kimi": True, "claude": True}

    codex = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert f'[projects."{target}"]' in codex
    assert 'trust_level = "trusted"' in codex

    ws = json.loads((home / ".kimi-code" / "workspaces.json").read_text())
    key = kimi_workspace_key(target)
    assert ws["workspaces"][key]["root"] == str(target)
    trust = (home / ".kimi-code" / "workspace-trust" / key).read_text()
    assert json.loads(trust)["root"] == str(target)

    cj = json.loads((home / ".claude.json").read_text())
    proj = cj["projects"][str(target)]
    assert proj["hasTrustDialogAccepted"] is True
    assert proj["hasTrustDialogHooksAccepted"] is True

    # 幂等：二次全 False（首写时原文件不存在，无备份是正确行为）
    again = pretrust_all(target)
    assert again == {"codex": False, "kimi": False, "claude": False}


def test_pretrust_preserves_existing_content(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('model = "x"\n', encoding="utf-8")

    changed = pretrust.pretrust_codex(tmp_path)
    assert changed is True
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith('model = "x"\n')  # 原内容不动
    assert '[projects."' in text
    assert (home / ".codex" / "config.toml.evobak").exists()  # 已有文件才备份
