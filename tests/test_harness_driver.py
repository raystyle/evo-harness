"""P8.1 driver 回归：先查再发 + bracketed-paste 编码 Enter 三段式。

r10 研究收敛（research-synth §五）：① paste-buffer -p（daemon 按 pane 模式
感知包壳，发送侧不自包壳防双重包裹）→ ② 原生静默同步 → ③ send-keys -H 0d
（hex 编码 Enter）。对话框在发送前按全 agent marker 并集扫（先查再发，
替代「发了再修」）。全部 fake，不起真 rmux。
"""

import asyncio
from types import SimpleNamespace

from evo_harness.config import DIALOGS
from evo_harness.driver import HarnessDriver

TASK = "請讀取任務文件 a.md 並嚴格執行其中全部指令，完成即止。"
HEAD = TASK[:10]


class _FakeRMUX:
    """cmd 记录到共享 log；fail_cmds 里的子命令恒 rc=1（模拟老 daemon）。"""

    def __init__(self, log, fail_cmds=()):
        self.log = log
        self.fail = tuple(fail_cmds)
        self.buffers = {}  # buf name → load 时刻的 payload

    def cmd(self, *args, check=False):
        self.log.append(("rmux",) + tuple(str(a) for a in args))
        if args[0] == "load-buffer":
            path = args[-1]
            buf = args[args.index("-b") + 1]
            with open(path, encoding="utf-8") as f:
                self.buffers[buf] = f.read()
        return SimpleNamespace(
            returncode=1 if args[0] in self.fail else 0, stdout="", stderr=""
        )


class _FakePane:
    """屏幕 = base 行 + 未处置对话框行；send_keys 默认弹掉首个对话框。"""

    def __init__(self, log, screen="❯ Ask…", dialog_lines=(), static_dialog=False):
        self.log = log
        self.target = "sess:w0.p1"
        self.base_lines = screen.splitlines()
        self.dialog_lines = list(dialog_lines)
        self.static_dialog = static_dialog

    def capture_text(self):
        return "\n".join(self.base_lines + self.dialog_lines)

    def send_text(self, text):
        self.log.append(("pane.send_text", text))

    def send_keys(self, *keys):
        self.log.append(("pane.send_keys", keys))
        if self.dialog_lines and not self.static_dialog:
            self.dialog_lines.pop(0)

    def wait_for_text(self, text, timeout=0.0):
        if text in self.capture_text():
            return True
        raise TimeoutError(text)


def _mk(log, fail_cmds=(), **pane_kw):
    """driver 直构（绕过 __init__ 的 RMUX 实例化），全 fake 无真进程。"""
    rmux = _FakeRMUX(log, fail_cmds)
    pane = _FakePane(log, **pane_kw)
    d = HarnessDriver.__new__(HarnessDriver)
    d.config = None
    d.run_id = "r10t"
    d.socket = "sock"
    d._rmux = rmux
    d.session = None
    return d, rmux, pane


def _idx(log, pred):
    return next(i for i, e in enumerate(log) if pred(e))


class _SplitWindow:
    """spawn 单测窗：记录 split 的 shell_command（空 pane 列表走 split 分支）。"""

    def __init__(self, log):
        self.log = log

    def list_panes(self):
        return []

    def pane(self, _i):
        return self

    def split(self, shell_command=""):
        self.log.append(("win.split", shell_command))
        return self


def test_spawn_unit_quotes_paths_with_spaces(tmp_path):
    """rc-r1 must-fix：state_file/cwd 含空格必须逐段 shlex.quote，
    否则 env 拆参 / cd 失败 → 全池 LAUNCH_FAILED（Windows/WSL 路径常态）。"""
    import shlex

    from evo_harness.config import HarnessConfig

    log = []
    d, _, _ = _mk(log)
    d.config = HarnessConfig()

    async def _win(_stage, cwd=None):
        return _SplitWindow(log)
    d.window_for = _win

    sf = tmp_path / "a b" / "state x.json"      # 两段都含空格
    cwd = tmp_path / "my repo" / "wt u1"
    asyncio.run(d.spawn_unit(
        "pool", "claude", cwd=str(cwd), unit_id="worker-claude", state_file=sf,
    ))
    cmd = log[-1][1]
    # shell 再解析后逐段还原：env 三键完整、cd 到位
    parts = shlex.split(cmd)
    assert parts[:4] == [
        "env",
        f"EVO_RUN={d.run_id}",
        "EVO_UNIT=worker-claude",
        f"EVO_STATE_FILE={sf.resolve()}",
    ]
    cd_at = parts.index("cd")
    assert parts[cd_at + 1] == str(cwd)
    assert parts[cd_at + 2] == "&&" and parts[cd_at + 3] == "exec"
    # 无特殊字符的取值不加多余引号（可读性不回退）
    assert f"EVO_RUN={d.run_id}" in cmd


def test_drive_three_stage_paste_then_encoded_enter():
    """三段式：load-buffer → paste-buffer -p -d → 静默等待 → send-keys -H 0d。"""
    log = []
    d, rmux, pane = _mk(log)
    assert asyncio.run(d.drive(pane, TASK, tui=False)) is True
    load = _idx(log, lambda e: e[0] == "rmux" and e[1] == "load-buffer")
    paste = _idx(log, lambda e: e[0] == "rmux" and e[1] == "paste-buffer")
    enter = _idx(log, lambda e: e[0] == "rmux" and e[1] == "send-keys")
    assert load < paste < enter
    # paste-buffer：bracketed 感知（-p）+ 用后即删（-d）+ 显式目标
    assert log[paste][2:6] == ("-p", "-d", "-b", "evo-r10t-sess_w0_p1")
    assert log[paste][7] == pane.target
    # 编码 Enter：hex 字节 0d，而非键名 "Enter"
    assert log[enter][2:] == ("-t", pane.target, "-H", "0d")
    # 文本走 buffer 路（payload 完整、无裸 ESC），不再 send_text 字面量
    assert rmux.buffers["evo-r10t-sess_w0_p1"] == TASK
    assert not any(e[0] == "pane.send_text" for e in log)
    assert not any(e[0] == "pane.send_keys" for e in log)


def test_drive_sweeps_dialog_before_sending():
    """先查再发：对话框键序发生在任何文本注入之前，且处置后文本照发。"""
    log = []
    d, rmux, pane = _mk(
        log, dialog_lines=["Do you trust the contents of this folder?"]
    )
    assert asyncio.run(d.drive(pane, TASK, tui=False)) is True
    # 第一个动作 = codex 信任框键序（Enter），先于 load-buffer
    assert log[0] == ("pane.send_keys", ("Enter",))
    load = _idx(log, lambda e: e[0] == "rmux" and e[1] == "load-buffer")
    assert load > 0
    assert pane.dialog_lines == []  # 处置成功
    assert rmux.buffers["evo-r10t-sess_w0_p1"] == TASK  # 文本未被吞


def test_drive_clean_screen_sends_no_dialog_keys():
    """无对话框：不发任何键序，直接进三段式。"""
    log = []
    d, _, pane = _mk(log)
    assert asyncio.run(d.drive(pane, TASK, tui=False)) is True
    assert not any(e[0] == "pane.send_keys" for e in log)
    assert log[0][:2] == ("rmux", "wait-pane")  # 退避静默等待先行


def test_drive_dialog_sweep_covers_all_agents():
    """并集扫描：codex/kimi/claude 各自的 marker 都能命中并按各自键序处置。"""
    for agent, specs in DIALOGS.items():
        if not specs:  # grok：--trust 门下无已知确认框
            continue
        marker, keys = specs[0]
        log = []
        d, _, pane = _mk(log, dialog_lines=[marker.upper()])
        assert asyncio.run(d.drive(pane, TASK, tui=False)) is True
        assert log[0] == ("pane.send_keys", keys), agent


def test_drive_dialog_sweep_bounded_rounds():
    """对话框永不消失（处置无效）：处置轮数有上界，不无限发键。"""
    log = []
    d, _, pane = _mk(
        log, dialog_lines=["Do you trust the contents"], static_dialog=True
    )
    assert asyncio.run(d.drive(pane, TASK, tui=False)) is True
    assert sum(1 for e in log if e[0] == "pane.send_keys") == 3


def test_drive_paste_fallback_to_literal_send_text():
    """buffer 路不可用（老 daemon 无 load-buffer）：退字面量 send-text。"""
    log = []
    d, _, pane = _mk(log, fail_cmds=("load-buffer",))
    assert asyncio.run(d.drive(pane, TASK, tui=False)) is True
    assert ("pane.send_text", TASK) in log
    # Enter 仍走编码路（cmd send-keys 未失败）
    assert any(
        e[0] == "rmux" and e[1] == "send-keys" and e[-1] == "0d" for e in log
    )


def test_drive_enter_fallback_plain_key():
    """编码 Enter 不可用（cmd send-keys 失败）：退 pane.send_keys("Enter")。"""
    log = []
    d, _, pane = _mk(log, fail_cmds=("send-keys",))
    assert asyncio.run(d.drive(pane, TASK, tui=False)) is True
    assert ("pane.send_keys", ("Enter",)) in log


def test_send_paste_strips_bare_esc():
    """payload 剥裸 ESC（orca 实证：防控制序列注入）。"""
    log = []
    d, rmux, pane = _mk(log)
    ok = asyncio.run(d._send_paste(pane, "ok\x1b[31mred"))
    assert ok is True
    [payload] = rmux.buffers.values()
    assert "\x1b" not in payload
    assert payload == "ok[31mred"


def test_drive_late_confirm_no_resend():
    """迟到确认保持：head 已在屏幕 → 直接 True，不 paste 不发键。"""
    log = []
    d, _, pane = _mk(log, screen=f"❯ {TASK}")
    assert asyncio.run(d.drive(pane, TASK, tui=False)) is True
    assert not any(e[0] == "rmux" and e[1] == "load-buffer" for e in log)
    assert not any(e[0].startswith("pane.") for e in log)
