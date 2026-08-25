# evo-harness

六阶段 Graph-of-Loops 编排器：用 rmux 后台会话把 **codex / kimi / claude** 装进同一
执行体系，按阶段图推进——模型只填角色模板，控制权在代码。

```
explore → research → plan → execute ⇄ review → merge → DONE
```

两平面架构：**控制平面 rmux**（编排 panes/agents，flow 本体跑在专属 rmux 后台会话）；
**操作平面 herdr**（宿主侧窗格操作：monitor 单窗格控制台 + notifyd 决策唤醒守护）。

## 前提（运行时三依赖）

| 依赖 | 用途 | 检查 |
|------|------|------|
| **uv** ≥0.12 | Python 运行时与依赖管理 | `uv --version` |
| **rmux** ≥0.10 | 控制平面 daemon：worker panes / flow 会话 | `rmux -V` |
| **herdr** | 操作平面：monitor 落位 / 决策注入（可选，非 herdr 会话自动降级） | `herdr --version` |

Python 侧依赖（librmux / rich / watchdog）由 `uv sync` 自动装。

## 安装（独立 CLI，全局命令）

```bash
uv tool install git+https://github.com/raystyle/evo-harness   # 装为全局 evo-harness 命令
evo-harness --help                                            # 验证
```

升级：`uv tool upgrade evo-harness`（或 `--reinstall` 重装到最新 commit）。

## 快速开始

```bash
evo-harness run "<目标>" --fake              # 确定性模式（不调真 LLM，先验编排）
evo-harness run "<目标>"                     # 真实 agent 六阶段
evo-harness review-cycle "<目标>" [--rounds N]   # 三 agent 循环 review 到一致
evo-harness monitor [--run-id ID]            # TUI 控制台（graph loop / 事件流 / 决策面板）
evo-harness status / wait / abort / decide / contract / clean-wt
```

开发仓内亦可 `uv sync` 后 `uv run evo-harness …`（同入口）。

herdr 会话内（`HERDR_ENV=1`）起 run：自动落位 monitor 右窗格 + 无头 notifyd +
flow 专属 rmux 会话，CLI 单行交接即退——决策（※）/终态（✔）由 notifyd 恰在
主 agent idle 时注入，零阻塞零后台 shell。

## 控制模型

- **程序编排**：感知 markdown 产物状态 + agent hook 状态 → 队列/graph 调度 →
  门禁 → 自愈（三路：submit-stale / worker-silent / idle-nudge）
- **模型产物只有 markdown**；控制面 JSON 一律程序生成（`evo-harness contract`）
- **主 agent 决策节点**：plan-approval / escalate-review / merge-conflict /
  merge-approval——真 run 无限期等人，唤醒走 notifyd 注入

产物全在 `.evo_tasks/<run_id>/`（gitignore）。

## 测试

```bash
uv run pytest -q          # 200+ 例：状态机路由 / 门禁 / 自愈 / 决策 / 控制台 / 护栏
```

## 沿革

由 [ProjectEvo](https://github.com/raystyle/ProjectEvo) 的 `.evotools/evo_harness`
抽离（v0.4.0，2026-08-25）；方法论（evo skill：布局规范 / 五阶段拓扑 / 踩坑表）
仍在 ProjectEvo 维护，本项目专注编排器本体。
