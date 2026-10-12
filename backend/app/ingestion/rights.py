"""The rights gate. It runs before any image request and it can only say no.

The one passing claim for a trusted source: that provider's published Open
Access policy plus `isPublicDomain: true` on that item. The Met is trusted
because its policy states what the item flag means for the hosted image.

Public domain is not CC0 here. An expired copyright term is a conclusion that
depends on the country, and a photograph of a public-domain work can carry its
own copyright; CC0 is a waiver from a rights holder. `PUBLIC_DOMAIN_MARK` stays
its own classification and never becomes eligible, so those bytes are never
fetched.

Everything else stops with a metadata-only negative cache row and a recheck
date. A restrictive statement beats an affirmative flag, and an untrusted
source's flag means nothing on its own. Fail closed: a conflict means we do not
know, and not knowing means no bytes are fetched.
"""

from dataclasses import dataclass

from app.db import catalog
from app.ingestion.met import MetItem

# The provider's own rights text is the evidence; these are the substrings we
# can actually recognize in it, casefolded. Discovery signals, not legal
# parsing: any hit means "not explicitly CC0".
_RESTRICTIVE_TOKENS: tuple[tuple[str, str], ...] = (
    ("public domain mark", catalog.RIGHTS_PUBLIC_DOMAIN_MARK),
    ("publicdomain/mark", catalog.RIGHTS_PUBLIC_DOMAIN_MARK),
    # Every Creative Commons license except CC0 lives under /licenses/. A CC0
    # dedication is /publicdomain/zero/, so this cannot match one.
    ("creativecommons.org/licenses", catalog.RIGHTS_OTHER_OPEN),
    ("cc by", catalog.RIGHTS_OTHER_OPEN),
    ("cc-by", catalog.RIGHTS_OTHER_OPEN),
    ("creative commons attribution", catalog.RIGHTS_OTHER_OPEN),
    ("share-alike", catalog.RIGHTS_OTHER_OPEN),
    ("sharealike", catalog.RIGHTS_OTHER_OPEN),
    ("noncommercial", catalog.RIGHTS_OTHER_OPEN),
    ("non-commercial", catalog.RIGHTS_OTHER_OPEN),
    ("noderivatives", catalog.RIGHTS_OTHER_OPEN),
    ("no derivatives", catalog.RIGHTS_OTHER_OPEN),
    ("rights reserved", catalog.RIGHTS_REJECTED),
    ("in copyright", catalog.RIGHTS_REJECTED),
    ("copyright", catalog.RIGHTS_REJECTED),
    ("\u00a9", catalog.RIGHTS_REJECTED),
    ("permission required", catalog.RIGHTS_REJECTED),
    ("permission of", catalog.RIGHTS_REJECTED),
    ("personal use", catalog.RIGHTS_REJECTED),
    ("not for reproduction", catalog.RIGHTS_REJECTED),
    ("may not be reproduced", catalog.RIGHTS_REJECTED),
)

# An explicit CC0 dedication in the item's own statement. This is the only
# claim that admits a record without a trusted provider policy.
_CC0_TOKENS: tuple[str, ...] = (
    "cc0",
    "publicdomain/zero",
    "public domain dedication",
)

# Evidence text is capped: a statement is a short quote, never a payload.
_STATEMENT_LIMIT = 500


@dataclass(frozen=True)
class RightsDecision:
    classification: str
    eligibility: str
    verification_status: str
    verification_method: str
    evidence_url: str | None
    # Brief evidence text. Never a file body or a data URI preview.
    raw_statement: str
    # Stable token for why this decision came out the way it did. Stored on the
    # evidence row, so a later audit reads the reason instead of inferring it.
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


def _is_explicit_cc0(statement: str) -> bool:
    folded = statement.casefold()
    return any(token in folded for token in _CC0_TOKENS)


def _unknown(
    item: MetItem,
    statement: str,
    reason: str,
    *,
    method: str = catalog.METHOD_PROVIDER_POLICY,
) -> RightsDecision:
    return RightsDecision(
        classification=catalog.RIGHTS_UNKNOWN,
        eligibility=catalog.ELIGIBILITY_UNKNOWN_CACHED,
        verification_status=catalog.VERIFICATION_UNVERIFIED,
        verification_method=method,
        evidence_url=item.landing_url,
        raw_statement=statement,
        reason=reason,
    )


def decide(
    item: MetItem,
    policy_url: str,
    *,
    trusted_public_domain_flag: bool = False,
) -> RightsDecision:
    """Classify one item. This function never performs I/O.

    `trusted_public_domain_flag` says this provider publishes what its
    `isPublicDomain` flag means for the hosted image. It defaults to False so a
    source added later fails closed instead of inheriting the Met's exception:
    without it, only an explicit CC0 dedication in the item's own statement can
    admit a record.
    """
    if item.invalid_reason is not None:
        return _unknown(
            item,
            f"metadata rejected: {item.invalid_reason}",
            item.invalid_reason,
        )

    statement = item.rights_statement.strip()[:_STATEMENT_LIMIT]
    restrictive = _restrictive_classification(statement)
    explicit_cc0 = _is_explicit_cc0(statement)

    if restrictive is not None:
        if explicit_cc0:
            # The statement claims CC0 and restricts the work at the same time.
            # That is not a firm non-CC0 answer, it is no answer.
            return _unknown(
                item,
                statement,
                "contradictory_rights_statement",
                method=catalog.METHOD_ITEM_STATEMENT,
            )
        # Reached even when the flag says public domain. The provider is
        # contradicting itself and the image stays unfetched.
        return RightsDecision(
            classification=restrictive,
            eligibility=catalog.ELIGIBILITY_NON_CC0_CACHED,
            verification_status=catalog.VERIFICATION_REJECTED,
            verification_method=catalog.METHOD_ITEM_STATEMENT,
            evidence_url=item.landing_url,
            raw_statement=statement,
            reason="restrictive_statement",
        )

    if explicit_cc0:
        if item.is_public_domain is False:
            return _unknown(
                item,
                _with_statement(
                    f"isPublicDomain=false for object {item.external_id}", statement
                ),
                "contradictory_rights_statement",
                method=catalog.METHOD_ITEM_STATEMENT,
            )
        return RightsDecision(
            classification=catalog.RIGHTS_CC0,
            eligibility=catalog.ELIGIBILITY_ELIGIBLE_CC0,
            verification_status=catalog.VERIFICATION_VERIFIED,
            verification_method=catalog.METHOD_ITEM_STATEMENT,
            evidence_url=item.landing_url,
            raw_statement=statement,
            reason="explicit_cc0_statement",
        )

    if item.is_public_domain is False:
        return RightsDecision(
            classification=catalog.RIGHTS_REJECTED,
            eligibility=catalog.ELIGIBILITY_NON_CC0_CACHED,
            verification_status=catalog.VERIFICATION_REJECTED,
            verification_method=catalog.METHOD_PROVIDER_POLICY,
            evidence_url=item.landing_url,
            raw_statement=_with_statement(
                f"isPublicDomain=false for object {item.external_id}", statement
            ),
            reason="provider_flag_false",
        )

    if item.is_public_domain is True:
        if not trusted_public_domain_flag:
            # A flag without a published policy behind it is an unverified
            # claim about ordinary public-domain status, not a CC0 waiver.
            return _unknown(
                item,
                _with_statement(
                    f"isPublicDomain=true for object {item.external_id}; "
                    f"no trusted provider policy defines that flag as CC0",
                    statement,
                ),
                "untrusted_public_domain_flag",
            )
        return RightsDecision(
            classification=catalog.RIGHTS_CC0,
            eligibility=catalog.ELIGIBILITY_ELIGIBLE_CC0,
            verification_status=catalog.VERIFICATION_VERIFIED,
            verification_method=catalog.METHOD_PROVIDER_POLICY,
            evidence_url=policy_url,
            raw_statement=_with_statement(
                f"isPublicDomain=true for object {item.external_id}; "
                f"provider Open Access policy designates the hosted image CC0",
                statement,
            ),
            reason="provider_policy_and_item_flag",
        )

    if statement:
        # Text we cannot recognize, and no flag to read it against.
        return _unknown(item, statement, "unrecognized_rights_statement")
    return _unknown(
        item,
        f"no rights flag for object {item.external_id}",
        "missing_rights_flag",
    )


def _with_statement(explanation: str, statement: str) -> str:
    """Keep the provider's own words next to our reading of them."""
    if not statement:
        return explanation
    combined = f"{explanation}; provider statement: {statement}"
    return combined[:_STATEMENT_LIMIT]
