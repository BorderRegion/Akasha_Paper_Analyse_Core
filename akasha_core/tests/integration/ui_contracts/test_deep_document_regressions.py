"""Geometry from the canonical PDF, independent of disposable extraction caches."""

import fitz
import pytest
from sqlalchemy import select

from paperintel.database.models import EvidenceRow, UiEvidenceLocatorRow
from paperintel.errors import DomainError
from paperintel.services.ui import documents


def mixed_pdf():
    with fitz.open() as pdf:
        page = pdf.new_page(width=500, height=700)
        page.insert_text((80, 100), "Rotated crop geometry evidence for the research paper.")
        page.set_cropbox(fitz.Rect(30, 40, 470, 640))
        page.set_rotation(90)
        page = pdf.new_page(width=300, height=400)
        page.insert_text((20, 60), "A second page with a different paper size.")
        return pdf.tobytes()


def test_rotated_cropped_pdf_and_mixed_page_sizes(api_env, import_pdf_into):
    imported = import_pdf_into(builder=mixed_pdf, name="rotated-crop.pdf")
    with api_env["state"].session_factory() as session:
        version_id = imported.paper_version_id
        page = documents.page_evidence(session, version_id, 1, data_dir=api_env["data_dir"])
        assert (page["page_display_width"], page["page_display_height"]) == (600, 440)
        assert page["evidence"], "fixture must exercise actual extracted evidence"
        for locator in page["evidence"]:
            row = session.get(EvidenceRow, locator.evidence_id)
            bbox = row.bbox
            assert locator.precision == "REGION"
            # Crop origin was already subtracted by native extraction; a 90°
            # rotation maps (x,y) to (600-y,x).
            expected = [(600 - bbox["y1"]) / 600, bbox["x0"] / 440,
                        (600 - bbox["y0"]) / 600, bbox["x1"] / 440]
            assert locator.rect_norm == pytest.approx(expected)
        second = documents.page_evidence(session, version_id, 2, data_dir=api_env["data_dir"])
        assert (second["page_display_width"], second["page_display_height"]) == (300, 400)
        documents.rebuild_locators(session, version_id, data_dir=api_env["data_dir"])
        session.commit()  # rebuilding must not insert duplicate projection keys
        assert len(session.scalars(select(UiEvidenceLocatorRow).where(
            UiEvidenceLocatorRow.paper_version_id == version_id)).all()) == len(page["evidence"]) + len(second["evidence"])


def test_changed_pdf_refuses_old_evidence_boxes(api_env, import_pdf_into):
    imported = import_pdf_into(builder=mixed_pdf, name="tampered.pdf")
    with api_env["state"].session_factory() as session:
        info = documents.document_info(session, imported.paper_version_id, data_dir=api_env["data_dir"])
        original = info.path.read_bytes()
        try:
            info.path.write_bytes(original + b"\n% changed after import\n")
            with pytest.raises(DomainError) as error:
                documents.page_evidence(session, imported.paper_version_id, 1, data_dir=api_env["data_dir"])
            assert error.value.code == "STORAGE_002"
            response = api_env["client"].get(f"/v1/ui/paper-versions/{imported.paper_version_id}/document")
            assert response.status_code != 200
            assert response.json()["error"]["code"] == "STORAGE_002"
        finally:
            info.path.write_bytes(original)
