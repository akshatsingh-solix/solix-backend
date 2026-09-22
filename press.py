import io
import re
from pathlib import Path
from typing import List, Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle, Image

router = APIRouter(prefix="/api/press", tags=["press"])

EMBER = colors.HexColor("#ED2423")
INK = colors.HexColor("#020817")
SLATE = colors.HexColor("#475569")


class PressBlock(BaseModel):
    type: str
    text: Optional[str] = None
    title: Optional[str] = None
    cite: Optional[str] = None
    items: Optional[List] = None


class PressPdfRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]{3,80}$")
    title: str = Field(max_length=300)
    date: str = Field(max_length=60)
    category: str = Field(max_length=40)
    summary: str = Field(max_length=1000)
    blocks: List[PressBlock] = Field(max_length=40)
    boilerplate: str = Field(max_length=2000)
    contact: str = Field(max_length=300)


def _styles():
    return {
        "eyebrow": ParagraphStyle("eyebrow", fontName="Helvetica-Bold", fontSize=8, textColor=EMBER, leading=11, spaceAfter=6),
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, textColor=INK, leading=25, spaceAfter=10),
        "summary": ParagraphStyle("summary", fontName="Helvetica-Oblique", fontSize=11, textColor=SLATE, leading=15, spaceAfter=14),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12.5, textColor=INK, leading=16, spaceBefore=10, spaceAfter=6),
        "p": ParagraphStyle("p", fontName="Helvetica", fontSize=10.5, textColor=colors.HexColor("#0f172a"), leading=15, spaceAfter=8, alignment=TA_LEFT),
        "li": ParagraphStyle("li", fontName="Helvetica", fontSize=10.5, textColor=colors.HexColor("#0f172a"), leading=15, leftIndent=14, bulletIndent=4, spaceAfter=3),
        "quote": ParagraphStyle("quote", fontName="Helvetica-Oblique", fontSize=11, textColor=INK, leading=15, leftIndent=12, spaceAfter=2),
        "cite": ParagraphStyle("cite", fontName="Helvetica", fontSize=8.5, textColor=SLATE, leading=11, leftIndent=12, spaceAfter=10),
        "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8.5, textColor=SLATE, leading=12, spaceAfter=4),
    }


def _clean(t: Optional[str]) -> str:
    return escape(re.sub(r"\s+", " ", t or "").strip())


@router.post("/pdf")
async def press_pdf(req: PressPdfRequest):
    s = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=0.9 * inch, rightMargin=0.9 * inch, topMargin=0.8 * inch, bottomMargin=0.8 * inch, title=req.title, author="Solix Technologies")
    story = []

    logo_path = Path(__file__).resolve().parent.parent / "frontend" / "public" / "brand" / "solix-logo.png"
    logo = Image(str(logo_path), width=1.5 * inch, height=0.78 * inch) if logo_path.exists() else Paragraph("<b>SOLIX</b>", ParagraphStyle("logo", fontName="Helvetica-Bold", fontSize=16, textColor=INK))
    header = Table([[logo, Paragraph("PRESS RELEASE", ParagraphStyle("pr", fontName="Helvetica-Bold", fontSize=8, textColor=SLATE, alignment=2))]], colWidths=[3.4 * inch, 3.3 * inch])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [header, HRFlowable(width="100%", thickness=1.2, color=EMBER, spaceAfter=14)]
    story += [Paragraph(f"{_clean(req.category).upper()} &nbsp;·&nbsp; {_clean(req.date)}", s["eyebrow"]), Paragraph(_clean(req.title), s["title"]), Paragraph(_clean(req.summary), s["summary"])]

    for b in req.blocks:
        if b.type == "h2":
            story.append(Paragraph(_clean(b.text), s["h2"]))
        elif b.type == "p":
            story.append(Paragraph(_clean(b.text), s["p"]))
        elif b.type == "ul" and b.items:
            for it in b.items:
                story.append(Paragraph(_clean(str(it)), s["li"], bulletText="•"))
            story.append(Spacer(1, 6))
        elif b.type == "quote":
            story.append(Paragraph(f"“{_clean(b.text)}”", s["quote"]))
            if b.cite:
                story.append(Paragraph(f"— {_clean(b.cite)}", s["cite"]))
        elif b.type == "callout":
            story.append(Paragraph(f"<b>{_clean(b.title)}</b> {_clean(b.text)}", s["p"]))

    story += [Spacer(1, 10), HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#CBD5E1"), spaceAfter=10)]
    story += [Paragraph("About Solix Technologies", s["h2"]), Paragraph(_clean(req.boilerplate), s["small"]), Spacer(1, 6), Paragraph(f"<b>Media contact:</b> {_clean(req.contact)}", s["small"]), Paragraph("###", ParagraphStyle("end", fontName="Helvetica", fontSize=9, textColor=SLATE, alignment=1, spaceBefore=14))]

    doc.build(story)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="solix-press-{req.id}.pdf"'})
