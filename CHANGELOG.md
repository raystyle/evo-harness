# Changelog

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
