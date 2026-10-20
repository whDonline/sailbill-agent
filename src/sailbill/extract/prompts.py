"""Prompts for the LLM extraction stage.

Prompts live in code (not in loose text files) so a prompt change is reviewable
in the diff, and every prompt carries a version string that is written into the
trace -- when extraction quality drops, you can tell which prompt produced which
output.

Design notes behind the wording:

* ``evidence`` is mandatory per field.  This is what makes grounding checks and
  the audit trail possible; "explain yourself" also measurably reduces numeric
  hallucination.
* ``absent_fields`` vs ``unreadable_fields`` separates "the document does not
  have it" from "it is there but illegible".  The first may inherit values on
  update, the second never may.
* The model is told *not* to normalise names, currencies or dates: that is done
  deterministically afterwards.  Models are bad at lookup tables and good at
  reading layout and language.
"""

EXTRACT_PROMPT_VERSION = "2026-09-25.1"

SYSTEM = """你是一名国际货运代理财务单据信息抽取专家，熟悉 Debit Note、Credit Note、Invoice、
对账单（Statement of Account）以及提单、订舱确认等单据的版式与术语。

你的任务：从给定的单据文本（可能来自邮件正文、Excel/Word 表格或 OCR 结果）中，抽取账单级信息
与全部费用明细行，输出严格符合给定 JSON Schema 的结构化数据。

必须遵守的铁律：
1. 只抽取文本中确实出现的内容。任何字段只要无法在原文中找到明确依据，就不要填值。
2. 每个抽出的字段都要在 evidence 中给出原文片段（逐字复制，不要改写、不要翻译）。
3. 区分两种"没有"：
   - absent_fields：单据本身没有这个字段（例如邮件正文里根本没有币种信息）；
   - unreadable_fields：单据有该字段但无法可靠辨认（例如 OCR 出现乱码、被截断）。
   两者都填 null，但必须分别列入对应数组。
4. 金额一律输出数字（不要千分位、不要货币符号、不要引号）。原币种另行说明。
5. 币种保持原文写法（如 "USD"、"US$"、"RMB"），不要自行折算或统一。
6. 费用名称保持单据上的原始写法（charge_item_raw），不要翻译、不要标准化、不要合并同类行。
7. 每个费用项目单独一条记录；即使两行名称金额完全相同，只要原文出现两次就输出两条。
8. 小计(Subtotal)、税额(VAT/GST/Tax)、总计(Total) 不是费用行，只能填入 subtotal_amount /
   tax_amount / total_amount。
9. 不要推测缺失的单据编号、日期或交易方；宁可留空。
10. 只输出 JSON，不要输出解释文字、不要输出 Markdown 代码块。"""

USER = """请抽取下面这份单据。

================ 单据文本开始 ================
{text}
================ 单据文本结束 ================

补充线索（由规则引擎给出，可能是空的，仅供定位版式，仍需以原文为准）：
{hints}

请输出 JSON，字段如下：
{{
  "document_type": "Debit Note | Credit Note | Invoice | Statement of Account | Bill of Lading | null",
  "reference_no": "单据编号，如 DN-202609-001",
  "document_date": "单据日期，保持原文格式",
  "issuer": "签发方/开票方",
  "recipient": "接收方/付款方",
  "currency": "单据主要币种",
  "subtotal_amount": 数字或 null,
  "tax_amount": 数字或 null,
  "total_amount": 数字或 null,
  "payment_term": "PP | CC | null",
  "charges": [
    {{
      "charge_item_raw": "原文费用名称",
      "description": "原文描述（如有）",
      "quantity": 数字或 null,
      "unit_price": 数字或 null,
      "amount": 数字,
      "currency": "原文币种或 null",
      "payment_term": "原文付款方式或 null",
      "evidence": "该行原文片段"
    }}
  ],
  "absent_fields": ["单据中确实不存在的字段名"],
  "unreadable_fields": ["存在但无法辨认的字段名"],
  "evidence": {{"字段名": "对应原文片段"}},
  "notes": "需要人工关注的情况，没有则 null"
}}"""


REPAIR = """上一次输出未通过校验，错误如下：
{errors}

请基于同样的单据文本重新输出 JSON，修正上述错误。务必只输出 JSON。
"""


def render_user(text: str, hints: str = "") -> str:
    return USER.format(text=text, hints=hints or "（无）")
