"""The rights gate. It runs before any image request and it can only say no.

The one passing claim: The Met's published Open Access policy plus
`isPublicDomain: true` on that item. Ordinary public-domain status, a public
domain mark, CC BY, or a missing statement all stop here with a metadata-only
negative cache row and a recheck date.

A restrictive statement beats an affirmative flag. Fail closed: a conflict
means we do not know, and not knowing means no bytes are fetched.
"""

from dataclasses import dataclass

from app.db import catalog
from app.ingestion.met import MetItem

# Substrings checked against the provider's own rights text, casefolded. These
# are discovery signals, not legal parsing: any hit means "not explicitly CC0".
_RESTRICTIVE_TOKENS: tuple[tuple[str, str], ...] = (
    ("public domain mark", catalog.RIGHTS_PUBLIC_DOMAIN_MARK),
    ("publicdomain/mark", catalog.RIGHTS_PUBLIC_DOMAIN_MARK),
    ("cc by", catalog.RIGHTS_OTHER_OPEN),
    ("cc-by", catalog.RIGHTS_OTHER_OPEN),
    ("creative commons attribution", catalog.RIGHTS_OTHER_OPEN),
    ("share-alike", catalog.RIGHTS_OTHER_OPEN),
    ("noncommercial", catalog.RIGHTS_OTHER_OPEN),
    ("all rights reserved", catalog.RIGHTS_REJECTED),
    ("in copyright", catalog.RIGHTS_REJECTED),
    ("copyright", catalog.RIGHTS_REJECTED),
    ("\u00a9", catalog.RIGHTS_REJECTED),
)


@dataclass(frozen=True)
class RightsDecision:
    classification: str
    eligibility: str
    verification_status: str
    verification_method: str
    evidence_url: str | None
    # Brief evidence text. Never a file body or a data URI preview.
    raw_statement: str
    reason: str

    @property
    def is_eligible_cc0(self) -> bool:
        return (
            self.eligibility == catalog.ELIGIBILITY_ELIGIBLE_CC0
            and self.classification == catalog.RIGHTS_CC0
            and self.verification_status == catalog.VERIFICATION_VERIFIED
        )


def _restrictive_classification(statement: str) -> str | None:
    folded = statement.casefold()
    for token, classification in _RESTRICTIVE_TOKENS:
        if token in folded:
            return classification
    return None


def decide(item: MetItem, policy_url: str) -> RightsDecision:
    """Classify one item. This function never performs I/O."""
    if item.invalid_reason is not None:
        return RightsDecision(
            classification=catalog.RIGHTS_UNKNOWN,
            eligibility=catalog.ELIGIBILITY_UNKNOWN_CACHED,
            verification_status=catalog.VERIFICATION_UNVERIFIED,
            verification_method=catalog.METHOD_PROVIDER_POLICY,
            evidence_url=item.landing_url,
            raw_statement=f"metadata rejected: {item.invalid_reason}",
            reason=item.invalid_reason,
        )

    restrictive = _restrictive_classification(item.rights_statement)
    if restrictive is not None:
        # Reached even when the flag says public domain. The provider is
        # contradicting itself and the image stays unfetched.
        return RightsDecision(
            classification=restrictive,
            eligibility=catalog.ELIGIBILITY_NON_CC0_CACHED,
            verification_status=catalog.VERIFICATION_REJECTED,
            verification_method=catalog.METHOD_ITEM_STATEMENT,
            evidence_url=item.landing_url,
            raw_statement=item.rights_statement[:500],
            reason="restrictive_statement",
        )

    if item.is_public_domain is True:
        return RightsDecision(
            classification=catalog.RIGHTS_CC0,
            eligibility=catalog.ELIGIBILITY_ELIGIBLE_CC0,
            verification_status=catalog.VERIFICATION_VERIFIED,
            verification_method=catalog.METHOD_PROVIDER_POLICY,
            evidence_url=policy_url,
            raw_statement=(
                f"isPublicDomain=true for object {item.external_id}; "
                f"provider Open Access policy designates the hosted image CC0"
            ),
            reason="provider_policy_and_item_flag",
        )

    if item.is_public_domain is False:
        return RightsDecision(
            classification=catalog.RIGHTS_REJECTED,
            eligibility=catalog.ELIGIBILITY_NON_CC0_CACHED,
            verification_status=catalog.VERIFICATION_REJECTED,
            verification_method=catalog.METHOD_PROVIDER_POLICY,
            evidence_url=item.landing_url,
            raw_statement=f"isPublicDomain=false for object {item.external_id}",
            reason="provider_flag_false",
        )

    return RightsDecision(
        classification=catalog.RIGHTS_UNKNOWN,
        eligibility=catalog.ELIGIBILITY_UNKNOWN_CACHED,
        verification_status=catalog.VERIFICATION_UNVERIFIED,
        verification_method=catalog.METHOD_PROVIDER_POLICY,
        evidence_url=item.landing_url,
        raw_statement=f"no rights flag for object {item.external_id}",
        reason="missing_rights_flag",
    )
