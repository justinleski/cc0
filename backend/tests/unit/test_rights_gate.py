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
    item = met.normalize(str(object_id), _payload(object_id, **overrides))
    return rights.decide(item, met.POLICY_URL)


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
    ("overrides", "reason"),
    [
        ({"isPublicDomain": None}, "missing_rights_flag"),
        ({"isPublicDomain": "yes"}, "missing_rights_flag"),
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
    assert set(item.raw_metadata) <= set(met.METADATA_FIELDS)
    # A provider preview is image bytes. A negative-cache row must not keep it.
    assert "thumbnail" not in item.raw_metadata
    assert "primaryImageSmall" not in item.raw_metadata
    assert "constituents" not in item.raw_metadata
    assert "data:" not in json.dumps(item.raw_metadata)


def test_a_data_uri_image_field_is_dropped_so_it_cannot_be_fetched_or_stored():
    item = met.normalize("100001", _payload(100001, primaryImage="data:image/jpeg;base64,/9j/4A=="))
    assert item.original_url is None


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
        external_id: rights.decide(adapter.fetch_item(external_id), adapter.policy_url)
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
