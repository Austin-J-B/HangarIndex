# HangarIndex demonstration corpus

`FAA-Public/` is a locally stored, public-source library of 39 reference documents plus its source register for demonstrating aerospace maintenance search and RAG. The folder name is retained for the app's default path; the documents cover several authorities and related materials.

The documents themselves are not kept in git, only this README and the source register. To rebuild the folder on a new machine, download each item from the official link in [SOURCE-REGISTER.md](FAA-Public/SOURCE-REGISTER.md) into `FAA-Public/`.

## Included coverage

- FAA repair-station and maintenance requirements, manual and quality guidance, records, replacement-parts eligibility, general repair practices, and corrosion control.
- EASA continuing-airworthiness rules and Part-145 AMC/GM context, with a 2025 full baseline plus newer 2026 law and Part-145 amendment sheets.
- Spain's AESA Part-145 organization, MOE-evaluation, and component-certifier guidance.
- China's CAAC CCAR-145 Revision 4, organization-manual guidance, and QMS/SMS reference.
- Transport Canada's Standard 573 and current certificate-application/amendment guidance.
- DLA aluminum conversion-coating and aerospace topcoat specifications, Sherwin-Williams coating qualification/color/product references.
- FAA maintenance training material plus NASA background on aluminum alloys, Ti-6Al-4V properties, corrosion mechanisms, corrosion-control coatings, and titanium surface research.
- Public Lufthansa Technik quality, purchasing, parts-catalog, and repair-service context.

The item-by-item links, revisions, languages, and scope notes are in [SOURCE-REGISTER.md](FAA-Public/SOURCE-REGISTER.md). The F93 demo target is black per the requester; the public product-family files do not yet identify the exact product number or formula code.

## Limits

This is a realistic discovery stand-in, not a replacement for the Z: drive. It contains no company Repair Station Manual/MOE/QCM, CMM, AMM, SRM, job card, approved repair data, part applicability data, or Lufthansa-controlled technical manual. The FAA handbook is training background; NASA documents are historical or experimental research. Public supplier information does not establish maintenance instructions for a specific part.

The local EASA September 2025 Easy Access file is clearly marked as a non-current baseline; consult its accompanying 2026 consolidated rule and Part-145 amendments and verify the live EASA register. DLA coating specifications are available locally as searchable text extracts with page markers where the source PDF download was unavailable. Open the linked official records for the authoritative layout and current status.

Index this folder to try document, materials, and regulatory discovery. The 92 MB FAA handbook makes the first index take longer than later incremental updates. When you receive company files, point HangarIndex at a narrow company-public folder first. The current engine uses local SQLite full-text search and lexical ranking; the LLM answers from retrieved excerpts. It does not yet use embeddings, OCR, or an aviation-specific reranker.
