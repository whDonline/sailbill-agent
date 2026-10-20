"""Generate the synthetic sample corpus (no real customer data, ever).

Everything under ``samples/`` is produced by this script so the repository can
be open sourced without leaking anybody's freight rates.  It writes:

* ``debit_note.xlsx``  -- spreadsheet DN with title rows + totals block
* ``credit_note.docx`` -- Word CN with a charge table
* ``debit_note.pdf``   -- text-layer PDF built by a tiny embedded PDF writer
* ``booking_email.eml``-- email whose *attachments* carry the real content
* ``existing_bills.json`` -- already booked bills used by the matcher demo

Run: ``python scripts/make_samples.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


# --------------------------------------------------------------------------- #
# minimal PDF writer (text layer only, ASCII)
# --------------------------------------------------------------------------- #
def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(lines: list[str], *, width: int = 595, height: int = 842, size: int = 10) -> bytes:
    """A valid single-page PDF with a real text layer (Helvetica)."""
    content_parts = ["BT", f"/F1 {size} Tf"]
    y = height - 60
    for line in lines:
        content_parts.append(f"1 0 0 1 50 {y} Tm ({_escape(line)}) Tj")
        y -= size + 6
    content_parts.append("ET")
    content = "\n".join(content_parts).encode("latin-1")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ).encode("latin-1"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


DN_LINES = [
    "ABC LOGISTICS (SHANGHAI) CO., LTD",
    "25F Tower B, 1000 Century Avenue, Shanghai, China",
    "",
    "DEBIT NOTE",
    "",
    "Debit Note No.: DN-202609-001          Date: 2026-09-20",
    "B/L No.: COSU9510258060                Booking No.: BK20260901",
    "Container No.: CSLU1234567             Vessel/Voyage: EVER GIVEN / 0926E",
    "",
    "Issued By: ABC Logistics (Shanghai) Co., Ltd",
    "Bill To: XYZ Freight Forwarding Ltd",
    "",
    "Charge Description                  Qty    Unit Price      Amount   Currency  PP/CC",
    "Ocean Freight                       1      2,750.00      2,750.00  USD       PP",
    "Terminal Handling Charge (DEST)     2        150.00        300.00  USD       CC",
    "Documentation Fee                   1         50.00         50.00  USD       PP",
    "Customs Clearance Fee               1         80.00         80.00  USD       PP",
    "Handling Fee                        1         40.00         40.00  USD       PP",
    "",
    "Subtotal:  3,220.00",
    "VAT (0%):  0.00",
    "Total Due: 3,220.00 USD",
    "",
    "Remarks: please settle within 30 days. Thank you for your business.",
]


AR_LINES = [
    "XYZ FREIGHT FORWARDING LTD",
    "18F, 88 Harbour Road, Shanghai, China",
    "",
    "DEBIT NOTE",
    "",
    "Debit Note No.: DN-202609-004          Date: 2026-09-25",
    "B/L No.: SITCQINGDAO2609              Booking No.: BK20260904",
    "Container No.: TGHU8765432            Vessel/Voyage: CSCL STAR / 0310W",
    "",
    "Issued By: XYZ Freight Forwarding Ltd",
    "Bill To: Golden Dragon Trading Co., Ltd",
    "",
    "Charge Description                  Qty    Unit Price      Amount   Currency  PP/CC",
    "Ocean Freight                       1      1,850.00      1,850.00  USD       PP",
    "Documentation Fee                   1         45.00         45.00  USD       PP",
    "Customs Clearance Fee               1         90.00         90.00  USD       PP",
    "",
    "Subtotal:  1,985.00",
    "Total Due: 1,985.00 USD",
]

PENDING_LINES = [
    "ABC LOGISTICS (SHANGHAI) CO., LTD",
    "",
    "DEBIT NOTE",
    "",
    "Date: 2026-09-26",
    "B/L No.: COSU7777000123",
    "",
    "Issued By: ABC Logistics (Shanghai) Co., Ltd",
    "Bill To: XYZ Freight Forwarding Ltd",
    "",
    "Charge Description                  Qty    Unit Price      Amount   Currency  PP/CC",
    "Special Project Surcharge XYZ       1        120.00        120.00  USO       PP",
    "Terminal Handling Charge            1        180.00        180.00  USD       CC",
    "",
    "Total Due: 420.00 USD",
]


def make_extra_pdfs() -> None:
    """Two more scenarios: an AR bill (we are the issuer) and a messy one that
    must end in PENDING_REVIEW (unknown charge, bad currency code, bad total)."""
    (SAMPLES / "ar_debit_note.pdf").write_bytes(build_pdf(AR_LINES))
    (SAMPLES / "pending_debit_note.pdf").write_bytes(build_pdf(PENDING_LINES))


def make_pdf() -> None:
    (SAMPLES / "debit_note.pdf").write_bytes(build_pdf(DN_LINES))


def make_xlsx() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Debit Note"
    rows = [
        ["ABC LOGISTICS (SHANGHAI) CO., LTD", None, None, None, None, None],
        ["Debit Note", None, None, None, None, None],
        ["Debit Note No.", "DN-202609-002", None, "Date", "2026-09-21", None],
        ["B/L No.", "COSU6505001240", None, "Booking No.", "BK20260902", None],
        ["Issued By", "ABC Logistics (Shanghai) Co., Ltd", None, "Bill To", "XYZ Freight Forwarding Ltd", None],
        [None, None, None, None, None, None],
        ["Charge Description", "Qty", "Unit Price", "Amount", "Currency", "PP/CC"],
        ["Ocean Freight", 1, 3100, 3100, "USD", "PP"],
        ["Terminal Handling Charge", 2, 160, 320, "USD", "CC"],
        ["Documentation Fee", 1, 55, 55, "USD", "PP"],
        ["Trucking Fee", 1, 420, 420, "USD", "PP"],
        [None, None, None, None, None, None],
        ["Subtotal", None, None, 3895, "USD", None],
        ["VAT", None, None, 0, "USD", None],
        ["Total Due", None, None, 3895, "USD", None],
    ]
    for row in rows:
        ws.append(row)
    wb.save(SAMPLES / "debit_note.xlsx")


def make_docx() -> None:
    import docx

    document = docx.Document()
    document.add_heading("CREDIT NOTE", level=0)
    document.add_paragraph("Credit Note No.: CN-202609-003")
    document.add_paragraph("Date: 2026-09-22")
    document.add_paragraph("Issued By: Destination Agent Pte Ltd")
    document.add_paragraph("Bill To: XYZ Freight Forwarding Ltd")
    document.add_paragraph("B/L No.: HLCUTA12609HKBX6    Booking No.: BK20260903")
    document.add_paragraph("Reference: freight difference refunded after re-weighing.")

    table = document.add_table(rows=1, cols=5)
    headers = ["Charge Description", "Qty", "Unit Price", "Amount", "Currency"]
    for i, name in enumerate(headers):
        table.rows[0].cells[i].text = name
    for row in [
        ("Handling Fee", "1", "40.00", "40.00", "USD"),
        ("Storage Fee", "3", "25.00", "75.00", "USD"),
        ("Customs Fee", "1", "60.00", "60.00", "USD"),
    ]:
        cells = table.add_row().cells
        for i, value in enumerate(row):
            cells[i].text = value

    document.add_paragraph("Subtotal: 175.00 USD")
    document.add_paragraph("Total: 175.00 USD")
    document.save(SAMPLES / "credit_note.docx")


def make_eml() -> None:
    """A mail: body asks for a correction, attachment carries the DN."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Billing <billing@abc-logistics.example>"
    msg["To"] = "AP Team <ap@xyz-forwarding.example>"
    msg["Cc"] = "tom.chen@abc-logistics.example"
    msg["Subject"] = "Debit Note DN-202609-001 (revised handling fee) - COSU9510258060"
    msg["Date"] = "Wed, 23 Sep 2026 09:15:00 +0800"
    msg["Message-ID"] = "<20260923091500.abc123@abc-logistics.example>"
    msg["In-Reply-To"] = "<20260921080000.def456@xyz-forwarding.example>"

    body = (
        "Dear AP team,\n\n"
        "Please find attached the revised debit note for B/L COSU9510258060.\n"
        "It is the same note as before, resent with the corrected container number.\n\n"
        "Debit Note No.: DN-202609-001\n"
        "Date: 2026-09-20\n"
        "Total Due: 3,220.00 USD\n\n"
        "Best regards,\nABC Logistics Billing\n"
    )
    msg.set_content(body)
    msg.add_alternative(
        "<html><body><p>Dear AP team,</p><p>Please find attached the revised debit note for "
        "<b>B/L COSU9510258060</b>.</p><p>It is the same note as before, resent with the corrected "
        "container number.</p></body></html>",
        subtype="html",
    )

    msg.add_attachment(
        (SAMPLES / "debit_note.pdf").read_bytes(),
        maintype="application",
        subtype="pdf",
        filename="Debit Note DN-202609-001.pdf",
    )
    (SAMPLES / "booking_email.eml").write_bytes(bytes(msg))


def make_existing_bills() -> None:
    bills = [
        {
            "bill_id": "B-1001",
            "document_type": "Debit Note",
            "reference_no": "DN-202609-001",
            "document_date": "2026-09-20",
            "issuer": "ABC Logistics (Shanghai) Co., Ltd",
            "recipient": "XYZ Freight Forwarding Ltd",
            "accounting_entity": "XYZ Freight Forwarding Ltd",
            "posting_type": "AP",
            "references": {"bl_no": ["COSU9510258060"], "booking_no": ["BK20260901"]},
            "lines": [
                {"charge_item": "Ocean Freight", "amount": 2750.00, "currency": "USD", "payment_term": "PP"},
                {"charge_item": "Terminal Handling Charge", "amount": 280.00, "currency": "USD", "payment_term": "CC"},
                {"charge_item": "Documentation Fee", "amount": 50.00, "currency": "USD", "payment_term": "PP"},
            ],
        },
        {
            "bill_id": "B-1002",
            "document_type": "Debit Note",
            "reference_no": "DN-202609-002",
            "document_date": "2026-09-21",
            "issuer": "ABC Logistics (Shanghai) Co., Ltd",
            "recipient": "XYZ Freight Forwarding Ltd",
            "accounting_entity": "XYZ Freight Forwarding Ltd",
            "posting_type": "AP",
            "references": {"bl_no": ["COSU6505001240"], "booking_no": ["BK20260902"]},
            "lines": [
                {"charge_item": "Ocean Freight", "amount": 3100.00, "currency": "USD", "payment_term": "PP"},
                {"charge_item": "Documentation Fee", "amount": 55.00, "currency": "USD", "payment_term": "PP"},
            ],
        },
    ]
    (SAMPLES / "existing_bills.json").write_text(
        json.dumps(bills, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    make_pdf()
    make_extra_pdfs()
    make_xlsx()
    make_docx()
    make_existing_bills()
    make_eml()  # last: it embeds the two files above
    for path in sorted(SAMPLES.iterdir()):
        print(f"  {path.name:24} {path.stat().st_size:>8} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
