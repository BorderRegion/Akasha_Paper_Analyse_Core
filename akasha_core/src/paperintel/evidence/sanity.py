"""Fatal input checks before canonical extraction writes (doc 06 §5)."""

from paperintel.errors import DomainError
from paperintel.schemas.enums import SanityOutcome


def extraction_sanity(report):
    reasons = []
    units = [unit for page in report.pages for unit in page.units]
    if report.page_count <= 0 or len(report.pages) != report.page_count:
        reasons.append("page count is zero or inconsistent")
    if {page.page_number for page in report.pages} != set(range(1, report.page_count + 1)):
        reasons.append("page numbers are incomplete or duplicated")
    if not any((unit.text or "").strip() or unit.asset_sha256 for unit in units):
        reasons.append("no usable evidence")
    for page in report.pages:
        if any(unit.page_number != page.page_number for unit in page.units):
            reasons.append("unit page does not match its containing page")
    if not report.quality_checks.location_sanity:
        reasons.append("invalid evidence location")
    if reasons:
        return SanityOutcome.FAIL, reasons
    if (
        report.quality_checks.suspiciously_empty_text
        or report.quality_checks.ocr_confidence_warnings
    ):
        return SanityOutcome.WARN, ["sparse text or low OCR confidence"]
    return SanityOutcome.PASS, []


def require_safe_extraction(report):
    outcome, reasons = extraction_sanity(report)
    if outcome is SanityOutcome.FAIL:
        raise DomainError(
            "EXTRACT_001",
            message="Extraction sanity gate blocked unsafe input.",
            details={"sanity": outcome.value, "reasons": reasons},
        )
    return reasons
