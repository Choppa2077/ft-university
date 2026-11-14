"""Post-process the pandoc docx so that wide tables stay readable in Word.

    python docs/final/docx_tables.py docs/final/Final_Report.docx

Pandoc gives table cells no paragraph style, so they take the 12 pt body text. This puts every
paragraph inside a table in the "Table Text" style (9 pt, defined in docs/final/reference.docx)
and makes every table as wide as the text column. Run by to_docx.sh after pandoc.
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

STYLE = '<w:pStyle w:val="TableText" />'


def fix_table(m: re.Match) -> str:
    t = m.group(0)
    t = re.sub(r'<w:tblW w:type="auto" w:w="0"\s*/>', '<w:tblW w:type="pct" w:w="5000" />', t)
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
            if info.filename == "word/document.xml":
                xml = data.decode("utf-8")
                n = xml.count("<w:tbl>")
                xml = re.sub(r"<w:tbl>.*?</w:tbl>", fix_table, xml, flags=re.S)
                data = xml.encode("utf-8")
                print(f"{path.name}: {n} tables set in Table Text, full width")
            out.writestr(info, data)


if __name__ == "__main__":
    main()
