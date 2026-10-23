# 参与贡献

感谢你愿意改进 sailbill-agent。本项目最欢迎三类贡献：

## 1. 补充航运费用词表

`src/sailbill/normalize/charges.py` 的 `STANDARD_CHARGES` 是"标准名 → 别名列表"的字典。
真实业务里同一笔费用有几十种写法（`THC` / `THC (DEST)` / `码头操作费` / `Terminal Handling`）。
新增时请：

- 标准名用英文全称（保持词典风格一致）；
- 别名按"英文变体、缩写、中文"分组追加；
- 同时补一条 `tests/test_normalize.py` 的用例。

## 2. 补充金标用例

见 `docs/harness.md` §2。要求：

- 数据必须**脱敏**或**合成**（真实单据放 `TestSamples/`，已在 gitignore 中）；
- 用例要覆盖至少一种新情形（新版式、新币种、脏 OCR、负样本）；
- 提交前跑 `python harness/run_eval.py --min-score 0.95`。

## 3. 修复解析边界

`tests/` 里有很多"回归用例"，每条都对应一个真实踩过的坑（金额千分位、Word 合并单元格、
EML 附件递归、参考号前缀污染……）。发现新坑时，**先写失败的测试，再改代码**。

---

## 开发流程

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,llm]"
python scripts/make_samples.py
pytest -q            # 必须全绿
ruff check src tests harness scripts
python harness/run_eval.py --min-score 0.95
```

## 代码约定

- 类型标注 + 中文业务注释（业务规则要让财务同事看得懂）；
- 新增外部依赖需在 PR 里说明为什么标准库/现有依赖不够；
- **不得**把真实客户数据、真实运费、密钥提交进仓库；
- 涉及决策的改动请新增 `docs/adr/NNNN-xxx.md`。

## 提交信息

```
feat(charges): 补充 12 个东南亚港口附加费别名
fix(amounts): 修正 "1.234" 被误判为三位小数的问题
docs(adr): 记录 OCR 开放式 schema 的取舍
```
