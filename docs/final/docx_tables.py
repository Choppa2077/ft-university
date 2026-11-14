"""Post-process the pandoc docx so that wide tables stay readable in Word.

    python docs/final/docx_tables.py docs/final/Final_Report.docx

Pandoc gives table cells no paragraph style, so they take the 12 pt body text, and it gives every
column of a pipe table the same width. This puts every paragraph inside a table in the
"Table Text" style (9 pt, defined in docs/final/reference.docx), makes every table as wide as the
text column, and sizes its columns by their content. Run by to_docx.sh after pandoc.
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

STYLE = '<w:pStyle w:val="TableText" />'


TEXT_WIDTH = 9864          # twips: A4 (11906) minus the 1021-twip side margins of reference.docx
CHAR = 95                  # twips per character of 9 pt Table Text, on average
CAP = 48                   # a column never asks for more than this many characters per line


def cell_text(tc: str) -> str:
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", tc))


def column_widths(t: str) -> list[int] | None:
    """Pandoc gives every column of a pipe table the same width, so an ID column is as wide as a
    column of sentences. Size the columns by their content instead: each asks for its longest cell
    (capped at CAP characters) and never less than its longest word."""
    rows = [re.findall(r"<w:tc>.*?</w:tc>", tr, re.S) for tr in re.findall(r"<w:tr>.*?</w:tr>", t, re.S)]
    n = len(re.findall(r"<w:gridCol ", t))
    if not rows or any(len(r) != n for r in rows):
        return None                                                  # merged cells: leave as is
    texts = [[cell_text(c) for c in r] for r in rows]
    want = [min(max(len(r[i]) for r in texts), CAP) for i in range(n)]
    floor = [max((len(w) for r in texts for w in r[i].split()), default=1) + 1 for i in range(n)]
    want = [max(w, f) for w, f in zip(want, floor)]
    total = sum(want)
    widths = [round(TEXT_WIDTH * w / total) for w in want]
    short = [i for i in range(n) if widths[i] < floor[i] * CHAR]      # a long word would break
    if short and sum(floor) * CHAR <= TEXT_WIDTH:
        fixed = {i: floor[i] * CHAR for i in short}
        rest = TEXT_WIDTH - sum(fixed.values())
        others = [i for i in range(n) if i not in fixed]
        widths = [fixed[i] if i in fixed else round(rest * want[i] / sum(want[j] for j in others))
                  for i in range(n)]
    return widths


def set_widths(t: str, widths: list[int]) -> str:
    grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}" />' for w in widths) + "</w:tblGrid>"
    t = re.sub(r"<w:tblGrid>.*?</w:tblGrid>", grid, t, flags=re.S)
    # schema order inside tblPr: ... tblW, jc, ..., tblLayout, tblCellMar, tblLook
    layout = '<w:tblLayout w:type="fixed" />'
    pr = re.search(r"<w:tblPr>.*?</w:tblPr>", t, re.S).group(0)
    anchor = next((a for a in ("<w:tblCellMar", "<w:tblLook", "</w:tblPr>") if a in pr), "</w:tblPr>")
    t = t.replace(pr, pr.replace(anchor, layout + anchor, 1), 1)

    def row(tr: re.Match) -> str:
        cells = iter(widths)
        return re.sub(r"<w:tcPr />|<w:tcPr>", lambda c: (f'<w:tcPr><w:tcW w:type="dxa" w:w="{next(cells)}" />'
                                                         + ("</w:tcPr>" if c.group(0) == "<w:tcPr />" else "")),
                      tr.group(0))
    return re.sub(r"<w:tr>.*?</w:tr>", row, t, flags=re.S)


def fix_table(m: re.Match) -> str:
    t = m.group(0)
    t = re.sub(r'<w:tblW w:type="auto" w:w="0"\s*/>', '<w:tblW w:type="pct" w:w="5000" />', t)
    widths = column_widths(t)
    if widths:
        t = set_widths(t, widths)
    t = re.sub(r'<w:pStyle w:val="[^"]*"\s*/>', STYLE, t)                    # replace any style
    t = re.sub(r'<w:p>(?!<w:pPr>)', f'<w:p><w:pPr>{STYLE}</w:pPr>', t)       # no properties yet
    t = re.sub(r'<w:p><w:pPr>(?!<w:pStyle)', f'<w:p><w:pPr>{STYLE}', t)      # properties, no style
    return t


def main() -> None:
    path = Path(sys.argv[1])
    src = zipfile.ZipFile(path)
    members = [(i, src.read(i.filename)) for i in src.infolist()]
    src.close()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in members:
            if info.filename == "[Content_Types].xml":          # pandoc declares images only per part
                types = data.decode("utf-8")
                for ext, ctype in (("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg")):
                    if f'Extension="{ext}"' not in types:
                        types = types.replace("<Default ", f'<Default Extension="{ext}" ContentType="{ctype}" /><Default ', 1)
                data = types.encode("utf-8")
            if info.filename == "word/document.xml":
                xml = data.decode("utf-8")
                n = xml.count("<w:tbl>")
                xml = re.sub(r"<w:tbl>.*?</w:tbl>", fix_table, xml, flags=re.S)
                data = xml.encode("utf-8")
                print(f"{path.name}: {n} tables set in Table Text, full width")
            out.writestr(info, data)


if __name__ == "__main__":
    main()
