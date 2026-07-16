"""Strict schema for validated AI scoring responses."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScoringDimensions(BaseModel):
    """Per-dimension scores with their business-defined ranges."""

    model_config = ConfigDict(extra="forbid", strict=True)

    completeness: int = Field(ge=0, le=30)
    logic: int = Field(ge=0, le=30)
    format: int = Field(ge=0, le=20)
    quality: int = Field(ge=0, le=20)

    @property
    def total(self) -> int:
        """Return the sum used to validate the overall score."""
        return self.completeness + self.logic + self.format + self.quality


class ScoringResult(BaseModel):
    """Complete, strictly typed result accepted from the scoring model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    score: int = Field(ge=0, le=100)
    detail: str = Field(max_length=500)
    highlights: str = Field(max_length=150)
    improvements: str = Field(max_length=250)
    dimensions: ScoringDimensions

    @model_validator(mode="after")
    def score_must_equal_dimension_sum(self) -> Self:
        """Reject internally inconsistent scores instead of normalizing them."""
        if self.score != self.dimensions.total:
            raise ValueError("score must equal the sum of all dimensions")
        return self
