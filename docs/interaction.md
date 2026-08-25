# 人机交互模型：控制单元 × 操作单元，全程无阻塞

evo-harness 把「谁来干活」「谁来操作窗口」「谁来决策」拆成两类单元，
跨窗口、跨工作单元协作，主对话全程零阻塞。

## 两类单元

| 类别 | 单元 | 宿主 | 职责 |
|------|------|------|------|
| 控制单元 | flow 编排进程 | rmux 后台会话 `evo-<run_id>` | 状态机推进、任务队列、门禁、自愈 |
| 控制单元 | worker 池（claude/kimi/codex） | rmux panes | 干活：探索/研究/实现/评审，产物只有 markdown |
| 操作单元 | monitor 控制台 | herdr 会话右窗格 | 人看的：六面板直播（UNITS/GOAL/GRAPH/NOTIFY/AGENTS/EVENT） |
| 操作单元 | notifyd 守护 | 无头进程 | 机器对机器的唤醒：决策简报注入主 agent |
| 决策单元 | 主 agent（人机同体） | herdr 主窗格 | 关键节点批准/改道/否决（※ 决策） |

控制单元之间走 rmux（daemon 独立，关掉任何窗口都不影响编排）；
操作单元经 herdr 操作宿主窗口（落位、读屏、注入）；
两类单元只通过文件总线（`.evo_tasks/<run>/`）交换状态，无任何进程间阻塞调用。

## 无阻塞特性（跨窗口、跨工作单元）

```
main window (herdr)                    rmux daemon (independent)
+---------------------------+          +--------------------------+
| main agent pane   [idle]  |<--inject--+ flow session  evo-<run> |
|   ^ decision brief (※)    |          |   | task queue           |
|   | decides via CLI       |          |   v                      |
| monitor pane (right)      |          | worker x3 panes          |
|   live 6-panel console    |          |   claude / kimi / codex  |
+---------------------------+          +--------------------------+
        ^ read-only                      ^ file bus only
        +---------- .evo_tasks/<run>/ ---+
```

1. **主对话永不等待**。`evo-harness run/review-cycle` 是一次「下指令」：
   单行交接即退，不占 shell、不留后台任务。编排进程活在 rmux 会话里，
   关掉终端、切换窗口、重启 herdr 都不影响它。
2. **决策不轮询、不空转**。需要主 agent 参与的节点（plan 批准、复核升级、
   冲突裁决、合入批准）由 notifyd 守护投递：它先等主 agent 转入 idle
   （idle 门），再复核窗口末屏无忙碌特征（内容权威兜底），然后把决策简报
   连同 decide 命令注入主窗格。通知恰好在「能接单的时刻」到达。
3. **观测零干扰**。monitor 是纯读者：只读文件总线渲染六面板，
   不写任何编排状态；随时开、随时关、随时重开。
4. **工作单元互不连坐**。每个 worker 独占一个 rmux pane（必要时再独占
   一个 git worktree），一个卡死由编排进程三路自愈（重提/催写/重派），
   不阻塞其它 worker，也不惊动主对话。
5. **人只在决策点出现**。程序管机械与自愈，人管批准与方向；
   批准是责任，所以真 run 的决策节点无限期等人，绝不超时默认。

## 一次交互的时序

```
t0   main agent: evo-harness review-cycle "<goal>"
     -> console placed (monitor right pane)   [herdr, ops plane]
     -> notifyd spawned headless              [ops plane]
     -> flow scheduled in rmux session        [rmux, control plane]
     -> CLI exits, main agent goes idle
t1   workers run round 1 (three panes, parallel)
t2   aggregate -> must-fix -> fixer -> round 2 ...
t3   rounds exhausted without consensus
     -> decisions/escalate-review.json (pending)
     -> notifyd: wait idle -> screen check -> inject brief (※)
     -> main agent receives it as a user message, reads reports, runs:
        evo-harness decide --node escalate-review --choice extend|finalize|abort
t4   terminal state -> notifyd injects final notice (✔) -> daemon exits
     -> rmux session self-destructs, monitor can be closed
```

主 agent 在 t0 到 t3 之间可以自由做任何事：开新窗口、跑别的任务、
与人对话。系统不占它的 shell，不抢它的 turn，只在它空闲且需要它时
递上一条消息。
