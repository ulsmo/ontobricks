"""HL7 FHIR R5 import service."""

from back.core.industry.fhir.FhirImportService import FhirImportService

get_fhir_catalog = FhirImportService.get_fhir_catalog
fetch_and_parse_fhir = FhirImportService.fetch_and_parse_fhir

__all__ = [
    "FhirImportService",
    "get_fhir_catalog",
    "fetch_and_parse_fhir",
]
