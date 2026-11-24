"""Excel export: the page sends the same rows it puts in a CSV; this turns them into a readable .xlsx."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

MAX_ROWS = 5000


def xlsx(sheets):
    """sheets: [{"name": str, "rows": [[header...], [cell...], ...]}] -> .xlsx bytes."""
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("Nothing to export.")
    book = Workbook()
    book.remove(book.active)
    for sheet in sheets[:5]:
        rows = sheet.get("rows") if isinstance(sheet, dict) else None
        if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS or not all(isinstance(r, list) for r in rows):
            raise ValueError("Nothing to export.")
        ws = book.create_sheet(str(sheet.get("name") or "Sheet")[:31])
        for r, row in enumerate(rows, 1):
            for c, value in enumerate(row, 1):
                value = value if isinstance(value, (int, float)) and not isinstance(value, bool) else ("" if value is None else str(value))
                cell = ws.cell(r, c, value)
                if isinstance(value, str):
                    cell.data_type = "s"  # text from the web stays text: "=..." must never run as a formula
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="205C49")
        ws.freeze_panes = "C2"
        for col in ws.columns:
            longest = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = max(10, min(60, longest + 2))
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
