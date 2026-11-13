"""cdk-nag v3 acknowledgments: one finding id per entry, each with its reason."""

from __future__ import annotations

from aws_cdk import Acknowledgment, Validations
from constructs import IConstruct


def ack(construct: IConstruct, findings: dict[str, str]) -> None:
    Validations.of(construct).acknowledge(
        *(Acknowledgment(id=finding, reason=reason) for finding, reason in findings.items())
    )
