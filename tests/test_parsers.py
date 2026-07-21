import importlib.util
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.parsers import parse_document


HAS_DOCUMENT_DEPS = all(
    importlib.util.find_spec(name) for name in ("pypdf", "docx", "openpyxl", "pptx")
)


@unittest.skipUnless(HAS_DOCUMENT_DEPS, "document parser extras are not installed")
class DocumentParserTests(unittest.TestCase):
    def test_pdf_page_locator(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.pdf"
            _write_minimal_pdf(path, "Evidence first knowledge")
            result = parse_document(path)
            self.assertIn("Evidence first knowledge", result.units[0].text)
            self.assertEqual(result.units[0].locator["page"], 1)

    def test_docx_heading_and_paragraph_locator(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.docx"
            document = Document()
            document.add_heading("审核规则", level=1)
            document.add_paragraph("每条证据必须保存原文片段。")
            document.save(path)
            result = parse_document(path)
            self.assertEqual(result.units[0].locator["heading_path"], ["审核规则"])
            self.assertEqual(result.units[0].text, "每条证据必须保存原文片段。")

    def test_xlsx_sheet_and_cell_range(self):
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "知识"
            sheet.append(["规则", "必须引用证据"])
            workbook.save(path)
            workbook.close()
            result = parse_document(path)
            self.assertEqual(result.units[0].locator["sheet"], "知识")
            self.assertEqual(result.units[0].locator["cell_range"], "A1:B1")

    def test_pptx_slide_locator(self):
        from pptx import Presentation

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.pptx"
            presentation = Presentation()
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = "知识审核"
            slide.placeholders[1].text = "冲突证据不能自动覆盖正式知识"
            presentation.save(path)
            result = parse_document(path)
            self.assertEqual(result.units[0].locator["slide"], 1)
            self.assertIn("冲突证据", result.units[0].text)


def _write_minimal_pdf(path: Path, text: str) -> None:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    path.write_bytes(output)

