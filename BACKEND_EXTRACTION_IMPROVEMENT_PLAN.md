# Backend AI Extraction Improvement Plan

This plan focuses on improving extraction accuracy across:
- **Digital PDFs** (embedded selectable text)
- **Scanned PDFs** (image-only pages)
- **Image uploads** (JPG/PNG from mobile scans)

## 1) Add an input triage stage before extraction

Route every page through a classifier first:

1. **Digital text present**: parse with PDF text extraction first.
2. **Scanned/image-like**: route to OCR pipeline with preprocessing.
3. **Mixed pages**: run hybrid mode (direct text + OCR) and merge.

Recommended signals:
- Character count from direct extraction
- Printable character ratio
- OCR confidence / text density
- Presence of invoice anchors (`INVOICE`, `GSTIN`, `TAXABLE`, `TOTAL`)

Why this helps: one extraction strategy is rarely optimal for all document types.

## 2) Improve scanned PDF and image OCR quality

Build a preprocessing pipeline before OCR:
- Auto-rotate / deskew
- Denoise (median/bilateral)
- Adaptive thresholding
- Contrast normalization
- Optional region cropping for header and totals zone

OCR strategy:
- Use at least 2 OCR profiles (e.g., line mode + block mode) and pick the best per field
- Preserve page coordinates when possible for layout-aware rules
- Add language and symbol tuning (`₹`, `%`, `/`, `-`, decimal separators)

Why this helps: OCR accuracy usually improves significantly with preprocessing and multi-pass OCR.

## 3) Add image input support as a first-class path

Currently many real invoices come as photos or screenshots.

Backend changes:
- Accept `image/jpeg`, `image/png`, `image/webp`
- Normalize to a standard internal raster format
- Reuse the same OCR + postprocessing stack used for scanned PDFs

Why this helps: avoids forcing users to convert images to PDFs before upload.

## 4) Switch from regex-only extraction to layered extraction

Use a **layered field extraction** approach:
1. Deterministic rules (regex/anchors/tables)
2. Layout-aware fallback (nearest label-value pairs, same row/column)
3. Model-based fallback (small LLM or classifier for unresolved fields)

For each field, keep:
- `value`
- `source_engine` (direct_text / ocr / model)
- `confidence`
- `evidence_span` (line text and page index)

Why this helps: improves reliability and makes debugging easier.

## 5) Add field-level reconciliation and accounting checks

After extraction, run hard validations:
- `taxable + tax components ≈ final total` (with tolerance)
- GSTIN checksum validation
- Date normalization and validity checks
- Invoice number plausibility checks

When conflicting candidates exist:
- Rank by confidence + proximity to known anchors
- Keep top candidate and alternate candidates for audit

Why this helps: catches OCR/model errors before output generation.

## 6) Strengthen confidence scoring and manual review routing

Move from one global confidence to **field-level confidence** + weighted overall confidence.

Suggested confidence features:
- OCR confidence
- Rule strength (exact anchor match vs fuzzy)
- Cross-field consistency (math checks passed)
- Historical vendor template match

Routing policy:
- Auto-accept if overall + mandatory fields exceed threshold
- Send to review queue if key fields are uncertain
- Store review corrections for retraining

Why this helps: reduces silent failures and improves trust.

## 7) Add vendor template memory (high-impact for repeated senders)

Many invoices repeat vendor layouts.

Implement:
- Vendor fingerprint (GSTIN + logo hash + layout signature)
- Cached extraction template for known vendors
- Automatic template suggestion for new near-matches

Why this helps: large accuracy gains for repeat documents with minimal latency.

## 8) Build an evaluation harness and track accuracy weekly

Create a benchmark set split by type:
- Digital PDF
- Scanned PDF
- Mobile image
- Low quality / skewed

Track metrics:
- Field-level precision/recall/F1 for critical fields
- End-to-end pass rate (all mandatory fields correct)
- Manual review rate
- Latency per page

Why this helps: you can measure whether a change actually improved production quality.

## 9) Practical quick wins (1-2 sprints)

1. Add input triage + hybrid extraction merge.
2. Add image upload path with same OCR pipeline.
3. Add preprocessing (deskew + threshold + denoise).
4. Record field-level confidence and evidence spans.
5. Expand post-extraction math/consistency checks.

## 10) Recommended target architecture

- `ingest_service`: file type detection, page rendering, preprocessing
- `extraction_service`: direct PDF parser + OCR engines + model fallback
- `reconciliation_service`: validations, score, alternate candidates
- `review_service`: human corrections + feedback store
- `evaluation_service`: offline benchmark and regression reports

This modular architecture keeps extraction logic maintainable while improving accuracy over time.
