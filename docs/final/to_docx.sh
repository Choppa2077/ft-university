#!/bin/bash
# Build docs/final/Final_Report.docx from Final_Report.md (pandoc). Final_Report.md itself is
# generated from the template Final_Report.src.md by `python docs/final/fill_final.py`.
# Image paths in the Markdown are relative to docs/final/ (../../results/...), which works both
# on GitHub and here through --resource-path. No --toc: the report has its own contents list.
set -e
cd "$(dirname "$0")/../.."
pandoc docs/final/Final_Report.md \
  --resource-path=docs/final \
  --from gfm --to docx \
  -o docs/final/Final_Report.docx
echo "wrote docs/final/Final_Report.docx ($(du -h docs/final/Final_Report.docx | cut -f1))"
