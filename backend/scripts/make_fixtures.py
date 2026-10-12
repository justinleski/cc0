"""Regenerate the recorded Met fixture catalog in backend/fixtures/met.

Run from the repository root:

    backend/.venv/bin/python backend/scripts/make_fixtures.py

The JSON is Met-shaped but sanitized and hand-written, and the images are
generated solids, not provider bytes. Ten explicitly CC0 records, plus the
negative-cache and refused-download cases the acceptance tests need.
"""

import json
from pathlib import Path

from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = BACKEND_ROOT / "fixtures" / "met"
LANDING = "https://www.metmuseum.org/art/collection/search"
IMAGE_HOST = "https://images.metmuseum.org/CRDImages/cc0"

# Ten CC0 records: isPublicDomain true, no restrictive statement, image on the
# allowlisted host.
CC0_ITEMS = [
    (100001, "Harbor at Dawn", "Hokusai Katsushika", "1830", 1830, 1832, "Edo", "Japan"),
    (100002, "Study of Reeds", "Hokusai Katsushika", "1831", 1831, 1831, "Edo", "Japan"),
    (100003, "Still Life with Pears", "Paul Cezanne", "1885", 1885, 1888, "Aix", "France"),
    (100004, "Field in Late Summer", "Vincent van Gogh", "1889", 1889, 1889, "Arles", "France"),
    (100005, "Portrait of a Clerk", "Unknown", "1640", 1640, 1650, "Delft", "Netherlands"),
    (100006, "Silver Teapot", "Paul Revere", "1775", 1775, 1780, "Boston", "United States"),
    (100007, "Mountain Pass", "Thomas Cole", "1838", 1838, 1839, "Catskill", "United States"),
    (100008, "Woven Cotton Panel", None, "1500", 1500, 1550, "Chancay", "Peru"),
    (100009, "Bronze Mirror", None, "0200", 200, 250, "Luoyang", "China"),
    (100010, "Dancer Resting", "Edgar Degas", "1879", 1879, 1881, "Paris", "France"),
]

ULAN = {
    "Hokusai Katsushika": "http://vocab.getty.edu/page/ulan/500007311",
    "Paul Cezanne": "http://vocab.getty.edu/page/ulan/500004793",
    "Vincent van Gogh": "http://vocab.getty.edu/page/ulan/500115588",
    "Paul Revere": "http://vocab.getty.edu/page/ulan/500026270",
    "Thomas Cole": "http://vocab.getty.edu/page/ulan/500011298",
    "Edgar Degas": "http://vocab.getty.edu/page/ulan/500115507",
}

COLORS = [
    (198, 112, 76),
    (76, 112, 198),
    (112, 160, 96),
    (210, 180, 92),
    (140, 96, 168),
    (96, 168, 160),
    (188, 92, 120),
    (120, 120, 120),
    (160, 140, 96),
    (92, 140, 188),
]


def _item(object_id: int, **overrides) -> dict:
    payload = {
        "objectID": object_id,
        "title": "Untitled",
        "artistDisplayName": "",
        "artistULAN_URL": "",
        "artistWikidata_URL": "",
        "objectDate": "",
        "objectBeginDate": 0,
        "objectEndDate": 0,
        "objectURL": f"{LANDING}/{object_id}",
        "primaryImage": "",
        "primaryImageSmall": "",
        "isPublicDomain": False,
        "rightsAndReproduction": "",
        "medium": "Oil on canvas",
        "classification": "Paintings",
        "department": "European Paintings",
        "city": "",
        "country": "",
        # Present in real payloads and deliberately dropped by the allowlist.
        "measurements": [{"elementName": "Overall", "elementDescription": None}],
        "constituents": None,
        "GalleryNumber": "634",
    }
    payload.update(overrides)
    return payload


def _cc0_items() -> list[dict]:
    items = []
    for index, (object_id, title, artist, date, start, end, city, country) in enumerate(CC0_ITEMS):
        items.append(
            _item(
                object_id,
                title=title,
                artistDisplayName=artist or "",
                artistULAN_URL=ULAN.get(artist or "", ""),
                objectDate=date,
                objectBeginDate=start,
                objectEndDate=end,
                city=city,
                country=country,
                primaryImage=f"{IMAGE_HOST}/{object_id}.jpg",
                isPublicDomain=True,
                medium="Woodblock print" if index < 2 else "Oil on canvas",
            )
        )
    return items


def _negative_items() -> list[dict]:
    return [
        # The provider says the image is not open.
        _item(
            200001,
            title="Contemporary Study",
            artistDisplayName="Living Artist",
            primaryImage=f"{IMAGE_HOST}/200001.jpg",
            isPublicDomain=False,
            rightsAndReproduction="Rights reserved by the artist",
        ),
        # An affirmative flag contradicted by the provider's own statement. The
        # gate fails closed: a conflict is not explicit CC0.
        _item(
            200002,
            title="Photograph of a Sculpture",
            artistDisplayName="Museum Photographer",
            primaryImage=f"{IMAGE_HOST}/200002.jpg",
            isPublicDomain=True,
            rightsAndReproduction="Licensed CC BY 4.0, attribution required",
        ),
        # Ordinary public-domain status is not a CC0 designation.
        _item(
            200003,
            title="Engraved Plate",
            primaryImage=f"{IMAGE_HOST}/200003.jpg",
            isPublicDomain=True,
            rightsAndReproduction="Public Domain Mark 1.0",
        ),
    ]


def _unknown_items() -> list[dict]:
    missing_flag = _item(
        300001,
        title="Unattributed Fragment",
        primaryImage=f"{IMAGE_HOST}/300001.jpg",
    )
    del missing_flag["isPublicDomain"]
    wrong_type = _item(
        300002,
        title="Mislabelled Record",
        primaryImage=f"{IMAGE_HOST}/300002.jpg",
    )
    wrong_type["isPublicDomain"] = "yes"
    no_landing = _item(
        300003,
        title="Record Without Landing Page",
        primaryImage=f"{IMAGE_HOST}/300003.jpg",
        isPublicDomain=True,
    )
    no_landing["objectURL"] = ""
    return [missing_flag, wrong_type, no_landing]


def _refused_image_items() -> list[dict]:
    return [
        # Explicit CC0, but there is no image to fetch.
        _item(
            400001,
            title="Rights Cleared, No Image",
            primaryImage="",
            isPublicDomain=True,
        ),
        # Explicit CC0, image hosted off the allowlist.
        _item(
            400002,
            title="Rights Cleared, Foreign Host",
            primaryImage="https://cdn.example.net/CRDImages/400002.jpg",
            isPublicDomain=True,
        ),
        # Explicit CC0, but the body is not an image.
        _item(
            400003,
            title="Rights Cleared, HTML Body",
            primaryImage=f"{IMAGE_HOST}/400003.jpg",
            isPublicDomain=True,
        ),
    ]


def main() -> None:
    objects_dir = FIXTURE_ROOT / "objects"
    images_dir = FIXTURE_ROOT / "images"
    objects_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    items = _cc0_items() + _negative_items() + _unknown_items() + _refused_image_items()
    for item in items:
        path = objects_dir / f"{item['objectID']}.json"
        path.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    catalog = {
        "total": len(items),
        "objectIDs": [item["objectID"] for item in items],
    }
    (FIXTURE_ROOT / "catalog.json").write_text(
        json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
    )

    manifest: dict[str, dict] = {}
    for index, (object_id, *_rest) in enumerate(CC0_ITEMS):
        name = f"{object_id}.jpg"
        image = Image.new("RGB", (640 + index, 480 + index), COLORS[index % len(COLORS)])
        # A gradient band keeps each file distinct, so digests differ.
        for x in range(0, image.width, 16):
            for y in range(index * 4, min(index * 4 + 24, image.height)):
                image.putpixel((x, y), (255 - x % 256, (x + y) % 256, index * 20 % 256))
        image.save(images_dir / name, format="JPEG", quality=88)
        manifest[f"{IMAGE_HOST}/{name}"] = {"file": name, "content_type": "image/jpeg"}

    # Recorded refusals. No server is needed to prove these stop the pipeline.
    (images_dir / "400003.html").write_text(
        "<html><body>not an image</body></html>\n", encoding="utf-8"
    )
    manifest[f"{IMAGE_HOST}/400003.jpg"] = {
        "file": "400003.html",
        "content_type": "text/html",
    }
    (images_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(items)} records and {len(manifest)} image entries to {FIXTURE_ROOT}")


if __name__ == "__main__":
    main()
