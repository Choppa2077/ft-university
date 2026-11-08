#!/bin/bash
# Convert the endterm report to .docx (pandoc). Run after fill_report.py.
cd "$(dirname "$0")/../.."
pandoc docs/endterm/Endterm_Report.md \
  --resource-path=results/endterm:docs/endterm \
  --from gfm --to docx \
  --toc --toc-depth=2 \
  -o docs/endterm/Endterm_Report.docx
echo "wrote docs/endterm/Endterm_Report.docx ($(du -h docs/endterm/Endterm_Report.docx | cut -f1))"
