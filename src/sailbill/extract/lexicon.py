"""Domain lexicon: labels used by real freight documents (EN + zh).

Kept in one place so that table headers, text labels and prompts cannot drift
apart, and so a new customer's wording is a one-line change.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# document types
# --------------------------------------------------------------------------- #
DOC_TYPE_PATTERNS: dict[str, list[str]] = {
    "Debit Note": ["debit note", "debit memo", "debit advice", "dn", "借项通知", "借记通知", "借方通知"],
    "Credit Note": ["credit note", "credit memo", "credit advice", "cn", "贷项通知", "贷记通知", "贷方通知"],
    "Invoice": ["commercial invoice", "proforma invoice", "tax invoice", "invoice", "发票", "账单", "费用清单"],
    "Statement of Account": ["statement of account", "statement", "soa", "对账单", "结算单"],
    "Bill of Lading": ["bill of lading", "b/l", "提单"],
}

# --------------------------------------------------------------------------- #
# reference numbers
# --------------------------------------------------------------------------- #
REFERENCE_LABELS: dict[str, list[str]] = {
    "reference_no": [
        "debit note no", "debit note number", "credit note no", "credit note number",
        "d/c no", "dc no", "d/n no", "d/c", "debit credit no",
        "invoice no", "invoice number", "invoice#", "inv no", "document no", "doc no",
        "reference no", "ref no", "dn no", "cn no", "note no", "bill no", "statement no",
        "账单号", "单据号", "发票号", "单号",
    ],
    "bl_no": ["b/l no", "bl no", "bill of lading no", "mbl no", "master bl", "提单号", "主提单号"],
    "hbl_no": ["hbl no", "house bl", "hbl", "分提单号"],
    "booking_no": ["booking no", "booking ref", "bk no", "so no", "订舱号", "订舱编号"],
    "container_no": ["container no", "cntr no", "container#", "箱号", "集装箱号"],
    "voyage_no": ["voyage no", "voy no", "vessel/voyage", "航次"],
}

# --------------------------------------------------------------------------- #
# transaction parties
# --------------------------------------------------------------------------- #
PARTY_LABELS: dict[str, list[str]] = {
    "issuer": [
        "issued by", "bill from", "billed by", "seller", "beneficiary", "payee", "biller",
        "shipping agent", "agent", "开票方", "签发方", "出票方",
    ],
    "recipient": [
        "bill to", "billed to", "sold to", "invoice to", "customer", "consignee", "buyer",
        "payer", "account of", "付款方", "接收方", "客户",
    ],
}

# values that are never a company name
BAD_PARTY_VALUES = [
    "to order", "to the order", "same as", "as above", "see above", "n/a", "na", "none",
    "null", "n.a.", "tba", "tbd", "unknown",
]

# --------------------------------------------------------------------------- #
# table headers
# --------------------------------------------------------------------------- #
COLUMN_LABELS: dict[str, list[str]] = {
    "charge": [
        "charge", "charges", "charge item", "charge name", "description", "particulars",
        "item", "fee", "fee name", "details", "service", "费用", "费用名称", "项目",
        "收费项目", "内容", "摘要",
    ],
    "quantity": ["qty", "quantity", "数量", "箱量", "件数"],
    "unit_price": ["unit price", "unit rate", "rate", "price", "单价", "费率"],
    "amount": ["amount", "amt", "total", "total amount", "value", "金额", "小计金额", "费用金额"],
    "currency": ["currency", "ccy", "curr", "币种", "货币"],
    "payment_term": ["payment", "payment term", "pp/cc", "prepaid/collect", "term", "付款方式", "结算方式"],
    "tax": ["tax", "vat", "gst", "税额", "税"],
    "subtotal": ["subtotal", "sub total", "sub-total", "小计"],
    "total": ["grand total", "total amount due", "total due", "amount due", "total", "合计", "总计", "总额"],
}

# rows that end a charge table
FOOTER_LABELS = [
    "subtotal", "sub total", "sub-total", "grand total", "total", "amount due", "balance",
    "tax", "vat", "gst", "小计", "合计", "总计", "税额", "应付", "应收",
]

# --------------------------------------------------------------------------- #
# totals
# --------------------------------------------------------------------------- #
TOTAL_LABELS: dict[str, list[str]] = {
    "subtotal_amount": ["subtotal", "sub total", "sub-total", "小计", "不含税合计"],
    "tax_amount": ["vat", "gst", "tax amount", "tax", "税额", "增值税"],
    "total_amount": [
        "grand total", "total amount due", "amount due", "total due", "balance due",
        "total", "合计", "总计", "应付总额", "应收总额",
    ],
}
