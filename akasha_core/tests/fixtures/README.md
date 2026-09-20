# Test fixtures

Fixture classes required by spec doc 06 §2 (generated in phase P03):

| ID  | Description                                                        |
|-----|--------------------------------------------------------------------|
| F01 | Native text PDF: clean digital PDF with headings, paragraphs, citations |
| F02 | Scanned PDF: image-only pages requiring OCR                        |
| F03 | Mixed PDF: some native, some scanned, some mixed pages             |
| F04 | Rich scientific PDF: tables, equations, figures, multi-column text |
| F05 | Failure PDF: malformed metadata, unusual encoding, corrupt page    |
| F06 | Synthetic truth PDF: known facts (dataset size exactly 1,000; accuracy exactly 82.5%; improvement exactly 2.1pp; one hypothesis; one limitation; one technique; one table) |

Expected structured outputs for golden tests live in `expected/`.
Golden tests compare structured facts and evidence linkage, not writing style.
