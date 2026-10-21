# ADR 0005 · 编排层引入 LangGraph（作为可选引擎，而非替换）

- 状态：已采纳
- 日期：2026-10-22

## 背景

题面要求"构建一个 AI Agent"。顺序管线（解析 → 抽取 → 校验 → 匹配 → 决策）已经能跑通，
但它回答不了生产上最要紧的三个问题：

1. **人工在哪里介入？** 财务场景必须"复核后入账"，管线只能整批重跑；
2. **失败从哪一步重来？** 现在靠 trace 日志，OCR/模型都白跑；
3. **流程能否按单据分叉？** 无费用单据（提单、订舱确认）不该走匹配。

LangGraph 直接提供状态图、逐节点 checkpoint 与 `interrupt_before`。

## 决策

引入 **LangGraph 作为可选引擎**，而不是重写成 LangGraph 应用：

* `pipeline.py` 拆成 `stage_extract / stage_validate / stage_posting / stage_decide / stage_trace`，
  两个引擎调用**同一批函数**；
* `classic` 保持默认：零额外依赖、CI 快、确定性最强；
* `graph.py` 把同一批函数接成 `StateGraph`，额外提供 `--commit`（写回账单库）与 `--hitl`（写账前暂停）；
* 用 `tests/test_graph.py::test_graph_matches_classic` 钉死两者产物一致。

配套的硬性约束：**图状态只放 JSON 原生数据**（`doc_id` + trace），业务对象留在进程内注册表。
第一版把 Pydantic 对象塞进 state，checkpoint 反序列化后 `records` 变空，HITL 恢复时静默不提交——
这个坑已写进 `docs/graph.md` §3。

## 后果

- ✅ 拿到断点续跑、人工复核闸门、阶段级可观测，且不牺牲确定性；
- ✅ 未安装 LangGraph 的环境照常工作（`pip install -e .` 即可），不会被框架绑死；
- ✅ 新增"写回账单库"能力，真正闭环（此前只输出 JSON）；
- ⚠️ 跨进程 resume 需要 `SqliteSaver` + 重新解析文档，尚未实现；
- ⚠️ 两个引擎意味着两套代码路径，靠一致性测试守住，长期若确认收益可在 1.0 后移除 `classic`。
