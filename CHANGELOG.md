# Changelog

## [Unreleased]

- fix: `run`/`review-cycle` 兜底非 HardStop 异常（WorktreeError 等），
  一律落 ESCALATE 终态 + exit 2——旧实现裸穿后 run.json 假活 running、
  notifyd 无限空转（accept-v04-r2 rc-r1 must-fix）
- fix: `contract alloc` 校验 branch 跨 unit 唯一（1 unit = 1 branch），
  重名 branch 不再等到第二个 `worktree add -b` 必败
- fix: `spawn_unit` 前缀逐段 `shlex.quote`，仓库/worktree 路径含空格
  不再 env 拆参 / cd 失败炸整池
- fix: review/fixer 提示词模板去 ProjectEvo 硬编码布局（AGENTS.md、
  `.evotools/`、`skills/evo/`、`evo_gate.py`），改为「以仓内实际存在者
  为准」+ 原子性范围约束 + 通用测试门禁命令
- chore: `.claude/settings.json` 出库并忽略（安装器运行期产物，含本机
  解释器绝对路径，不入库污染新机）
- ci: GitHub Actions 门禁（uv sync + pytest + uv build）
- test: main() 集成 fixture 显式 mock `shutil.which`，干净 runner（无 rmux/herdr）
  下 CI 门禁亦全绿（accept-v04-r2 rc-r2 must-fix）
- chore: `.claude/settings.json.bak` 出库（rc-r3 must-fix：旧配置副本随
  e02637e 误入 git，携本机解释器绝对路径；磁盘文件保留且已被忽略）
- fix: `run --plan-only` 路径同款非 HardStop 兜底（`_prepare` 一并入
  try）——旧实现异常裸穿后 run.json 假活 running（accept-v04-r2 rc-r4
  must-fix，rc-r1 兜底之遗漏分支）

## [v0.4.0] - 2026-08-25

由 ProjectEvo `.evotools/evo_harness` 抽离为独立项目（源头同日已发
ProjectEvo v0.4.0 skill 版）。本仓库自此为编排器唯一权威源。

### 携带能力（ProjectEvo 全量，203 测试随迁）
- 六阶段 Graph-of-Loops：explore→research→plan→execute⇄review→merge，
  worktree 并行 + merge_order + CHANGELOG-only 冲突自解
- 控制模型 v2：程序编排 + 四决策节点（真 run 无限期等人）；notifyd
  决策唤醒守护（双锁：idle 门 + 末屏内容复核，内容权威兜底；三态
  投递；心跳线程）
- 控制台：单窗格 monitor（UNITS/GOAL/GRAPH/NOTIFY/AGENTS/EVENT 五面板
  五色）+ 事件流融合 + rmux 会话调度 flow（CLI 纯操作）
- 自愈三路 + 提交级 stalled + 超时重派；pytest 宿主护栏
  （PYTEST_CURRENT_TEST 拒真 herdr/rmux 操作）

### 独立化调整
- pyproject 脱离 ProjectEvo workspace；去僵尸依赖 textual
- 运行时三前提写明：uv / rmux / herdr（README 表格）
