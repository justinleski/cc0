"""The gate that decides whether any image request is allowed to happen."""

import json
from pathlib import Path

import pytest

from app.db import catalog
from app.ingestion import met, rights
from app.ingestion.errors import PermanentFailure
from app.ingestion.fixtures import FixtureSource

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "met"
METADATA_HOSTS = frozenset({"collectionapi.metmuseum.org"})
IMAGE_HOSTS = frozenset({"images.metmuseum.org"})


def _payload(object_id: int, **overrides) -> dict:
    payload = {
        "objectID": object_id,
        "title": "A Title",
        "objectURL": f"https://www.metmuseum.org/art/collection/search/{object_id}",
        "primaryImage": f"https://images.metmuseum.org/CRDImages/cc0/{object_id}.jpg",
        "isPublicDomain": True,
        "artistDisplayName": "Someone",
        "objectBeginDate": 1880,
        "objectEndDate": 1881,
    }
    payload.update(overrides)
    return payload


def _decide(object_id: int, **overrides) -> rights.RightsDecision:
    """Decide as the Met adapter does: its policy defines the item flag."""
    item = met.normalize(str(object_id), _payload(object_id, **overrides))
    return rights.decide(item, met.POLICY_URL, trusted_public_domain_flag=True)


def test_explicit_provider_flag_with_policy_is_the_only_pass():
    decision = _decide(100001)
    assert decision.is_eligible_cc0
    assert decision.classification == catalog.RIGHTS_CC0
    assert decision.verification_method == catalog.METHOD_PROVIDER_POLICY
    # The evidence has to name the policy that gives the flag its meaning.
    assert decision.evidence_url == met.POLICY_URL
    assert "isPublicDomain=true" in decision.raw_statement


@pytest.mark.parametrize(
    ("overrides", "classification"),
    [
        ({"isPublicDomain": False}, catalog.RIGHTS_REJECTED),
        ({"rightsAndReproduction": "Licensed CC BY 4.0"}, catalog.RIGHTS_OTHER_OPEN),
        ({"rightsAndReproduction": "Public Domain Mark 1.0"}, catalog.RIGHTS_PUBLIC_DOMAIN_MARK),
        ({"rightsAndReproduction": "\u00a9 2020 the artist"}, catalog.RIGHTS_REJECTED),
        ({"rightsAndReproduction": "All rights reserved"}, catalog.RIGHTS_REJECTED),
    ],
)
def test_non_cc0_statements_are_cached_not_eligible(overrides, classification):
    decision = _decide(200001, **overrides)
    assert not decision.is_eligible_cc0
    assert decision.eligibility == catalog.ELIGIBILITY_NON_CC0_CACHED
    assert decision.classification == classification


def test_a_restrictive_statement_beats_an_affirmative_flag():
    """Ordinary public-domain status is not a CC0 designation, flag or not."""
    decision = _decide(200003, isPublicDomain=True, rightsAndReproduction="Public Domain Mark 1.0")
    assert decision.eligibility == catalog.ELIGIBILITY_NON_CC0_CACHED
    assert decision.classification == catalog.RIGHTS_PUBLIC_DOMAIN_MARK


@pytest.mark.parametrize(
    ("statement", "classification"),
    [
        # A license URL is the form the item statement actually takes most of
        # the time, and no amount of substring matching on prose catches it.
        ("https://creativecommons.org/licenses/by/4.0/", catalog.RIGHTS_OTHER_OPEN),
        ("https://creativecommons.org/licenses/by-nc-sa/3.0/", catalog.RIGHTS_OTHER_OPEN),
        ("http://creativecommons.org/publicdomain/mark/1.0/", catalog.RIGHTS_PUBLIC_DOMAIN_MARK),
        ("Permission required; personal use only", catalog.RIGHTS_REJECTED),
        ("Reproduction only by permission of the artist", catalog.RIGHTS_REJECTED),
        ("May not be reproduced without written consent", catalog.RIGHTS_REJECTED),
        ("Rights reserved by the artist", catalog.RIGHTS_REJECTED),
    ],
)
def test_a_conflicting_license_or_restriction_fails_closed(statement, classification):
    """The flag cannot admit an item whose own statement contradicts it."""
    decision = _decide(200001, isPublicDomain=True, rightsAndReproduction=statement)
    assert not decision.is_eligible_cc0
    assert (decision.eligibility, decision.classification) == (
        catalog.ELIGIBILITY_NON_CC0_CACHED,
        classification,
    )
    assert decision.reason == "restrictive_statement"
    # The provider's own words survive as the evidence for that answer.
    assert decision.raw_statement == statement


def test_an_unrecognized_credit_line_still_passes_for_a_trusted_source():
    """Not every statement is a license. A credit line is not a restriction."""
    decision = _decide(100001, rightsAndReproduction="Gift of a generous donor, 1952")
    assert decision.is_eligible_cc0
    assert decision.reason == "provider_policy_and_item_flag"
    # Our reading of the flag, with the provider's text kept beside it.
    assert "isPublicDomain=true" in decision.raw_statement
    assert "Gift of a generous donor, 1952" in decision.raw_statement


def test_an_untrusted_source_cannot_admit_anything_on_the_flag_alone():
    """A flag only means CC0 where a published policy says so."""
    item = met.normalize("100001", _payload(100001))
    decision = rights.decide(item, met.POLICY_URL)

    assert not decision.is_eligible_cc0
    assert decision.eligibility == catalog.ELIGIBILITY_UNKNOWN_CACHED
    assert decision.reason == "untrusted_public_domain_flag"
    # The default is the safe one, so a source added later cannot inherit the
    # Met's exception by forgetting to opt out of it.
    assert rights.decide(item, met.POLICY_URL, trusted_public_domain_flag=True).is_eligible_cc0


def test_an_explicit_cc0_dedication_stands_without_a_trusted_policy():
    item = met.normalize(
        "100001",
        _payload(100001, rightsAndReproduction="CC0 1.0 Universal"),
    )
    decision = rights.decide(item, met.POLICY_URL)

    assert decision.is_eligible_cc0
    assert decision.verification_method == catalog.METHOD_ITEM_STATEMENT
    assert decision.reason == "explicit_cc0_statement"


def test_a_statement_that_both_dedicates_and_restricts_is_unknown():
    """Not a firm non-CC0 answer. No answer, which means no bytes."""
    decision = _decide(
        200001,
        rightsAndReproduction="CC0 for the metadata; \u00a9 2020 for the image",
    )
    assert decision.eligibility == catalog.ELIGIBILITY_UNKNOWN_CACHED
    assert decision.reason == "contradictory_rights_statement"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"isPublicDomain": None}, "missing_rights_flag"),
        # A string where the flag belongs is not a CC0 claim we can read, so
        # the payload is not the shape this adapter validated against.
        ({"isPublicDomain": "yes"}, "metadata_field_wrong_type"),
        ({"objectURL": "http://www.metmuseum.org/art/1"}, "missing_landing_url"),
        ({"objectURL": "https://example.com/art/1"}, "missing_landing_url"),
        ({"objectID": None}, "missing_object_id"),
        ({"primaryImage": {"url": "x"}}, "image_url_not_a_string"),
    ],
)
def test_unreadable_metadata_is_unknown_cached(overrides, reason):
    decision = _decide(300001, **overrides)
    assert decision.eligibility == catalog.ELIGIBILITY_UNKNOWN_CACHED
    assert decision.classification == catalog.RIGHTS_UNKNOWN
    assert decision.reason == reason


def test_payload_for_another_object_is_never_evidence():
    item = met.normalize("300001", _payload(999999))
    assert item.invalid_reason == "object_id_mismatch"
    assert rights.decide(item, met.POLICY_URL).eligibility == catalog.ELIGIBILITY_UNKNOWN_CACHED


def test_metadata_is_reduced_to_the_allowlist_without_previews():
    item = met.normalize(
        "100001",
        _payload(
            100001,
            primaryImageSmall="https://images.metmuseum.org/small.jpg",
            GalleryNumber="634",
            constituents=[{"name": "Someone"}],
            thumbnail="data:image/png;base64,iVBORw0KGgo=",
        ),
    )
    assert set(item.raw_metadata) <= set(met.METADATA_FIELD_NAMES)
    # A provider preview is image bytes. A negative-cache row must not keep it.
    assert "thumbnail" not in item.raw_metadata
    assert "primaryImageSmall" not in item.raw_metadata
    assert "constituents" not in item.raw_metadata
    assert "data:" not in json.dumps(item.raw_metadata)


def test_a_data_uri_image_field_is_dropped_so_it_cannot_be_fetched_or_stored():
    item = met.normalize("100001", _payload(100001, primaryImage="data:image/jpeg;base64,/9j/4A=="))
    assert item.original_url is None


def test_a_nested_value_under_an_allowed_name_never_reaches_the_cache():
    """An allowlisted name is not a licence to store whatever is under it."""
    item = met.normalize(
        "100001",
        _payload(100001, title={"preview": "data:image/png;base64,iVBORw0KGgo="}),
    )
    assert item.invalid_reason == "metadata_field_wrong_type"
    assert "title" not in item.raw_metadata
    assert "data:" not in json.dumps(item.raw_metadata)
    # The item page survives the rejection: that link is the negative cache.
    assert item.landing_url is not None


def test_an_oversized_field_is_rejected_rather_than_stored():
    item = met.normalize("100001", _payload(100001, medium="x" * (met.MAX_TEXT_LENGTH + 1)))
    assert item.invalid_reason == "metadata_field_wrong_type"
    assert "medium" not in item.raw_metadata


@pytest.mark.parametrize(
    "years",
    [
        {"objectBeginDate": 1900, "objectEndDate": 1880},
        {"objectBeginDate": 10**12, "objectEndDate": 10**12},
    ],
)
def test_unusable_years_stop_before_any_download(years):
    """These violate the asset constraint, which only fails after both PUTs."""
    decision = _decide(100001, **years)
    assert decision.eligibility == catalog.ELIGIBILITY_UNKNOWN_CACHED
    assert decision.reason == "invalid_year_range"


def test_creator_identity_prefers_an_authority_url_over_a_name():
    with_ulan = met.normalize(
        "100001",
        _payload(100001, artistULAN_URL="http://vocab.getty.edu/page/ulan/500115588"),
    )
    assert with_ulan.creator_external_key == "ulan:http://vocab.getty.edu/page/ulan/500115588"
    # Without an authority id the key is provider-scoped and case-folded, which
    # is a dedupe key within one provider, not a claim about a person.
    assert met.normalize("100001", _payload(100001)).creator_external_key == "name:someone"
    anonymous = met.normalize("100001", _payload(100001, artistDisplayName=""))
    assert anonymous.creator_external_key is None


def test_fixture_catalog_holds_ten_cc0_records_and_the_negative_cases():
    source = FixtureSource(
        root=FIXTURE_ROOT,
        metadata_hosts=METADATA_HOSTS,
        image_hosts=IMAGE_HOSTS,
    )
    adapter = met.MetAdapter(source)
    decisions = {
        external_id: rights.decide(
            adapter.fetch_item(external_id),
            adapter.policy_url,
            trusted_public_domain_flag=adapter.trusted_public_domain_flag,
        )
        for external_id in adapter.discover(100)
    }
    eligible = [key for key, value in decisions.items() if value.is_eligible_cc0]
    # Thirteen pass the rights gate; three of those have an image that the fetch
    # gate will refuse, which is a job failure and not a rights failure.
    assert len(eligible) == 13
    assert len([key for key in eligible if key.startswith("1")]) == 10
    assert [key for key, value in decisions.items() if not value.is_eligible_cc0] == [
        "200001",
        "200002",
        "200003",
        "300001",
        "300002",
        "300003",
    ]
    # Discovery and metadata only. No fixture image was read.
    assert source.image_requests == []


def test_a_fixture_cannot_serve_an_unlisted_host():
    source = FixtureSource(
        root=FIXTURE_ROOT,
        metadata_hosts=METADATA_HOSTS,
        image_hosts=IMAGE_HOSTS,
    )
    with pytest.raises(PermanentFailure, match="image_host_not_allowed"):
        source.fetch("https://cdn.example.net/CRDImages/400002.jpg")
