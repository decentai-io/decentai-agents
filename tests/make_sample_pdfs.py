"""Write the two PDFs the Documents agent's samples ship.

Run once when the text changes; the PDFs are committed, so loading a
sample never depends on a PDF library being present:

    python tests/make_sample_pdfs.py
"""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent.parent / "documents" / "samples"

LEASE_PAGES = [
    [
        "LEASE AGREEMENT",
        "Riverside Business Park, Unit 4B",
        "",
        "Between Riverside Estates LLC (the Landlord) and Sidra Office Supplies",
        "LLC (the Tenant). Reference RBP-4B-2024. Fictional; for trying the",
        "Documents agent.",
        "",
        "1. Premises",
        "The Landlord lets Unit 4B, Riverside Business Park, comprising 320 square",
        "metres of office space on the second floor, with four parking bays.",
        "",
        "2. Term",
        "The Term begins on 1 March 2024 and ends on 28 February 2027 (the",
        "Expiry Date), subject to clause 14.",
        "",
        "3. Rent",
        "The Rent is 4,000 USD per month, payable in advance on the first day",
        "of each month to the account the Landlord names in writing.",
    ],
    [
        "4. Service charge",
        "The Tenant pays a fair proportion of the Landlord's costs of maintaining",
        "the common parts, invoiced quarterly, not exceeding 400 USD a month",
        "in the first year of the Term.",
        "",
        "5. Use",
        "The Premises are used as offices and for the storage and display of",
        "office supplies, and for no other purpose without consent.",
        "",
        "6. Repairs",
        "The Tenant keeps the interior of the Premises in good repair. The",
        "Landlord keeps the structure, the roof and the common parts in repair.",
        "",
        "7. Insurance",
        "The Landlord insures the building; the Tenant insures its contents.",
    ],
    [
        "8. Alterations",
        "No structural alterations without the Landlord's written consent, not",
        "to be unreasonably withheld for partitions and cabling.",
        "",
        "9. Assignment",
        "The Tenant may not assign or sublet the whole or any part of the",
        "Premises without the Landlord's consent.",
        "",
        "10. Access",
        "The Landlord may enter on 48 hours' notice to inspect or repair, and",
        "at any time in an emergency.",
    ],
    [
        "11. Default",
        "If the Rent is more than 21 days late, or the Tenant is in breach of",
        "a material term and has not remedied it within 30 days of notice,",
        "the Landlord may re-enter the Premises.",
        "",
        "12. Deposit",
        "A deposit of 8,000 USD is held for the Term and returned within 30",
        "days of the Expiry Date less any sums due.",
        "",
        "13. Notices",
        "Notices are in writing and delivered by hand or by courier to the",
        "addresses on the first page, or by email to the addresses each party",
        "designates for notices.",
    ],
    [
        "14. Renewal and notice of non-renewal",
        "",
        "14.1 This Lease shall renew automatically for a further term of three",
        "(3) years on the Renewal Date unless either party gives written notice",
        "of non-renewal. The Renewal Date is 1 March 2027.",
        "",
        "14.2 Notice of non-renewal must be received by the Landlord not less",
        "than ninety (90) days before the Renewal Date.",
        "",
        "14.3 On renewal the Rent is reviewed to the open market rent, and in",
        "any case does not fall below the Rent then payable.",
        "",
        "15. Governing law",
        "This Lease is governed by the laws of the Emirate of Dubai.",
        "",
        "Signed for the Landlord: R. Marwan, 20 February 2024",
        "Signed for the Tenant: S. Haddad, 20 February 2024",
    ],
]

INVOICE = [
    "SIDRA OFFICE SUPPLIES LLC",
    "Riverside Business Park, Unit 4B, Dubai",
    "",
    "TAX INVOICE",
    "",
    "Invoice number: INV-1043",
    "Invoice date: 20 August 2026",
    "Due date: 19 September 2026",
    "",
    "Bill to: Harbourline Logistics",
    "Attention: Dana Harbour, Operations Director",
    "Marina Gate, floor 3, Dubai",
    "",
    "Reference: Quotation SQ-2026-118, first delivery (desks)",
    "",
    "Description                                         Qty   Unit    Amount",
    "Height-adjustable desk, oak top, 140 cm              20    240.00  4,800.00",
    "",
    "Subtotal                                                          4,800.00 USD",
    "Tax (0%)                                                              0.00 USD",
    "Total due                                                         4,800.00 USD",
    "",
    "Payment within 30 days to Sidra Office Supplies LLC, account named",
    "on the quotation. Please quote INV-1043.",
]


def write(path: Path, pages, title: str) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle(title)
    width, height = A4
    for page in pages:
        y = height - 2.5 * cm
        pdf.setFont("Helvetica", 11)
        for line in page:
            if line.isupper() and line.strip():
                pdf.setFont("Helvetica-Bold", 13)
            pdf.drawString(2.5 * cm, y, line)
            pdf.setFont("Helvetica", 11)
            y -= 0.6 * cm
        pdf.showPage()
    pdf.save()


if __name__ == "__main__":
    HERE.mkdir(parents=True, exist_ok=True)
    write(HERE / "riverside-office-lease.pdf", LEASE_PAGES, "Lease agreement — Riverside Business Park, Unit 4B")
    write(HERE / "invoice-INV-1043.pdf", [INVOICE], "Invoice INV-1043")
    for name in ("riverside-office-lease.pdf", "invoice-INV-1043.pdf"):
        print(name, (HERE / name).stat().st_size, "bytes")
