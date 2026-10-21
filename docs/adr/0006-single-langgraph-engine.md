# ADR 0006 · 切换为单一 LangGraph 引擎（取代 ADR 0005）

- 状态：已采纳
- 日期：2026-10-23
- 取代：[ADR 0005](0005-langgraph-engine.md)

## 背景

ADR 0005 引入了 LangGraph 作为**可选**引擎，与顺序管线并存。实际跑下来暴露两点：

1. 双引擎的代价是真实的：`stage_*` 与 `_process_unit` 两套调用路径、`--engine` 开关、
   以及一条只为一对引擎存在的一致性测试；
2. 收益也是真实的：`--commit` 写回账单库、`--hitl` 写账前暂停、逐节点 checkpoint，
   而这些能力**顺序管线根本给不了**，导致"默认路径"反而是能力更弱的那个。

题面要求"构建一个 AI Agent"。默认路径不能是那个没有断点、没有人工闸门的路径。

## 决策

**只保留 LangGraph**：

* `langgraph` 从可选 extra 提升为**核心依赖**（`pyproject.toml` 的 `dependencies`）；
* 删除 `_process_unit`、`stage_*`、`run_paths(engine=...)` 与一致性测试；
* 业务节点内联在 `graph.py` 里（抽取/校验/AR-AP/决策/写账），业务规则仍留在
  `pipeline.py` / `matching.py` / `finance.py`，节点只编排；
* `checkpointer` 常开（每次调用一个 `MemorySaver`，随单位结束释放）。

## 后果

- ✅ 只有一条路径，评审看到的就是唯一行为，没有"文档说 A、默认跑 B"的割裂；
- ✅ Agent 的能力成为默认能力：断点、人工闸门、写账、可 resume；
- ✅ 少了 150 行重复编排代码与一条冗余测试；
- ⚠️ 安装体积与启动成本增加（LangGraph + 其依赖），CI 稍慢；
- ⚠️ 业务与框架的耦合面变大：`graph.py` 现在同时承担"编排"和"调用业务"；
  将来若要换回顺序执行，需要把节点里的调用抽出来（业务函数本来就是独立的，成本可控）；
- ⚠️ 跨进程 resume 仍受 `MemorySaver` 限制，见 `docs/graph.md` §3。
