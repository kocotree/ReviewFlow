"""Immutable content snapshots used by collection and PDF bundling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MaterialGroup(str, Enum):
    """Top-level ordering groups for review materials."""

    ONLINE_DOC = "online_doc"
    ATTACHMENT = "attachment"

    @property
    def display_name(self) -> str:
        """Human-readable label written on a source separator page."""
        if self is MaterialGroup.ONLINE_DOC:
            return "在线需求文档"
        return "附件"


@dataclass(frozen=True, slots=True)
class PdfMaterial:
    """One immutable PDF input with explicit deterministic ordering metadata."""

    sequence: int
    group: MaterialGroup
    title: str
    pdf_bytes: bytes

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("PdfMaterial.sequence must be an integer")

        try:
            normalized_group = MaterialGroup(self.group)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "PdfMaterial.group must be 'online_doc' or 'attachment'"
            ) from exc
        object.__setattr__(self, "group", normalized_group)

        if not isinstance(self.title, str):
            raise TypeError("PdfMaterial.title must be a string")
        if not isinstance(self.pdf_bytes, bytes):
            raise TypeError("PdfMaterial.pdf_bytes must be bytes")


@dataclass(frozen=True, slots=True)
class CollectedContent:
    """A complete, immutable content snapshot for one scoring task.

    The original description stays outside the PDF. All online documents and
    attachments are represented by the single ``review_bundle_pdf`` value.
    """

    original_description: str
    review_bundle_pdf: bytes
    collection_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.original_description, str):
            raise TypeError("original_description must be a string")
        if not isinstance(self.review_bundle_pdf, bytes):
            raise TypeError("review_bundle_pdf must be bytes")
        if not self.review_bundle_pdf:
            raise ValueError("review_bundle_pdf must not be empty")
        object.__setattr__(self, "collection_warnings", tuple(self.collection_warnings))
