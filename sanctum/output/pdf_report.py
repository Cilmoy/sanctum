"""
pdf_report.py — Branded PDF export (Phase 2).

Stub implementation. Requires reportlab.
"""

import logging

logger = logging.getLogger(__name__)


class PDFReport:
    """Generates branded Sanctum PDF reports. Phase 2 feature."""

    def __init__(self, config: dict):
        self.config = config
        self.brand = config.get("output", {}).get("brand_name", "SANCTUM LLC")

    def generate_screen(self, results: list, shortlisted: list) -> None:
        logger.warning("PDF export not yet implemented (Phase 2).")

    def generate_analysis(self, result: dict) -> None:
        logger.warning("PDF export not yet implemented (Phase 2).")
