"""
Property enrichment — backend/app/core/property_enrichment.py

Pipeline that auto-populates property data from multiple sources:

  1. Website URL  → schema.org JSON-LD + OG meta tags + HTML parsing
  2. Google Places → structured name, address, photos, type, rating
  3. BDC listing  → amenities, capacity, description, photos from public page
  4. WebHotelier  → API pull (when credentials available)
  5. Claude vision → photo analysis for amenities + bedroom count

Each source fills gaps left by the previous one. By the time all sources
are tried, 80%+ of fields are populated without the owner typing anything.

Called from:
  POST /auth/onboarding/enrich-property
  POST /auth/onboarding/enrich-from-photos
  POST /auth/onboarding/enrich-from-bdc
  POST /auth/onboarding/enrich-from-webhotelier

Returns a PropertyData dict with confidence scores per field so the
frontend knows which fields to lock vs leave editable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

# ── Property data model ───────────────────────────────────────────────────────

@dataclass
class PropertyData:
    name:        str | None = None
    location:    str | None = None
    address:     str | None = None
    description: str | None = None
    max_guests:  int | None = None
    bedrooms:    int | None = None
    bathrooms:   int | None = None
    base_rate:   float | None = None
    amenities:   list[str] = field(default_factory=list)
    photos:      list[str] = field(default_factory=list)
    latitude:    float | None = None
    longitude:   float | None = None
    # Confidence: field → source that provided it
    sources:     dict[str, str] = field(default_factory=dict)

    def fill_from(self, other: "PropertyData", source: str) -> None:
        """Fill only empty fields from another PropertyData."""
        for f_name in ["name","location","address","description",
                       "max_guests","bedrooms","bathrooms","base_rate",
                       "latitude","longitude"]:
            if getattr(self, f_name) is None and getattr(other, f_name) is not None:
                setattr(self, f_name, getattr(other, f_name))
                self.sources[f_name] = source
        # Merge amenities
        for a in other.amenities:
            if a not in self.amenities:
                self.amenities.append(a)
        # Merge photos (cap at 10)
        for ph in other.photos:
            if ph not in self.photos and len(self.photos) < 10:
                self.photos.append(ph)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["completeness"] = self._completeness()
        return d

    def _completeness(self) -> int:
        fields = ["name","location","max_guests","bedrooms","amenities"]
        filled = sum(1 for f in fields if getattr(self, f))
        return int(filled / len(fields) * 100)


# ── Amenity normalisation ─────────────────────────────────────────────────────
# Maps various source vocabularies to our canonical amenity slugs.

AMENITY_MAP = {
    # Pool variants
    "swimming pool": "pool", "private pool": "pool", "outdoor pool": "pool",
    "heated pool": "pool", "infinity pool": "pool", "pool": "pool",
    # View
    "sea view": "sea_view", "ocean view": "sea_view", "sea views": "sea_view",
    "panoramic view": "sea_view", "water view": "sea_view",
    # WiFi
    "wifi": "wifi", "wi-fi": "wifi", "free wifi": "wifi",
    "wireless internet": "wifi", "internet": "wifi",
    # AC
    "air conditioning": "ac", "air-conditioning": "ac", "ac": "ac",
    "climate control": "ac",
    # BBQ
    "bbq": "bbq", "barbecue": "bbq", "grill": "bbq",
    # Parking
    "parking": "parking", "free parking": "parking", "private parking": "parking",
    "car park": "parking",
    # Gym
    "gym": "gym", "fitness": "gym", "fitness center": "gym",
    # Beach
    "beach access": "beach_access", "beach": "beach_access",
    "private beach": "beach_access", "beachfront": "beach_access",
    # Garden
    "garden": "garden", "terrace": "garden", "outdoor area": "garden",
    # Concierge
    "concierge": "concierge", "24-hour front desk": "concierge",
}

def normalise_amenity(raw: str) -> str | None:
    clean = raw.lower().strip()
    return AMENITY_MAP.get(clean)


# ── Source 1: Website scrape ──────────────────────────────────────────────────

async def enrich_from_website(url: str) -> PropertyData:
    """
    Fetch website and extract property data from:
    1. schema.org/LodgingBusiness JSON-LD (most structured)
    2. Open Graph meta tags (title, description, image)
    3. Basic HTML title + meta description
    """
    data = PropertyData()
    if not url.startswith("http"):
        url = f"https://{url}"

    try:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Vayancy/1.0)"}
        ) as client:
            r = await client.get(url)
            if r.status_code >= 400:
                return data
            html = r.text
    except Exception as e:
        log.warning("website_fetch_failed", url=url, error=str(e))
        return data

    # 1. Schema.org JSON-LD
    json_ld_matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    for raw in json_ld_matches:
        try:
            obj = json.loads(raw.strip())
            # Handle @graph arrays
            if isinstance(obj, dict) and "@graph" in obj:
                items = obj["@graph"]
            elif isinstance(obj, list):
                items = obj
            else:
                items = [obj]

            for item in items:
                t = item.get("@type", "")
                if not any(k in str(t) for k in
                           ["LodgingBusiness","Hotel","VacationRental",
                            "BedAndBreakfast","Resort","Accommodation"]):
                    continue

                if not data.name:
                    data.name = item.get("name")
                if not data.description:
                    data.description = str(item.get("description", ""))[:500] or None
                if not data.address:
                    addr = item.get("address", {})
                    if isinstance(addr, dict):
                        data.location = addr.get("addressLocality") or addr.get("addressRegion")
                        data.address  = addr.get("streetAddress")
                if not data.latitude and "geo" in item:
                    geo = item["geo"]
                    data.latitude  = float(geo.get("latitude", 0)) or None
                    data.longitude = float(geo.get("longitude", 0)) or None

                # Amenities from schema
                for feat in item.get("amenityFeature", []):
                    name = feat.get("name", "") if isinstance(feat, dict) else str(feat)
                    norm = normalise_amenity(name)
                    if norm and norm not in data.amenities:
                        data.amenities.append(norm)

                # Number of rooms
                if not data.bedrooms:
                    rooms = item.get("numberOfRooms")
                    if rooms:
                        try: data.bedrooms = int(rooms)
                        except: pass

                # Photos
                for img in item.get("photo", []):
                    url2 = img.get("url") if isinstance(img, dict) else str(img)
                    if url2 and len(data.photos) < 8:
                        data.photos.append(url2)

        except Exception:
            continue

    # 2. Open Graph tags
    og = {}
    for m in re.finditer(r'<meta[^>]+property=["\']og:([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']', html):
        og[m.group(1)] = m.group(2)
    for m in re.finditer(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:([^"\']+)["\']', html):
        og[m.group(2)] = m.group(1)

    if not data.name and og.get("title"):
        data.name = og["title"].split("|")[0].split("-")[0].strip()
    if not data.description and og.get("description"):
        data.description = og["description"][:500]
    if og.get("image") and len(data.photos) < 8:
        data.photos.append(og["image"])

    # 3. HTML title fallback
    if not data.name:
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        if title_match:
            data.name = title_match.group(1).split("|")[0].split("-")[0].strip()[:80]

    # 4. Meta description fallback
    if not data.description:
        desc_match = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{20,})["\']',
            html, re.IGNORECASE
        )
        if desc_match:
            data.description = desc_match.group(1)[:500]

    log.info("website_enrichment", url=url,
             name=data.name, amenities=len(data.amenities), photos=len(data.photos))
    return data


# ── Source 2: Google Places ───────────────────────────────────────────────────

async def enrich_from_google_places(query: str) -> PropertyData:
    """
    Text search Google Places API for the property.
    Returns structured name, address, location, photos, rating.

    Requires GOOGLE_PLACES_API_KEY in settings (optional — graceful no-op if absent).
    """
    data = PropertyData()
    api_key = getattr(settings, "google_places_api_key", "")
    if not api_key:
        log.debug("google_places_skipped_no_key")
        return data

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Text search
            r = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={
                    "query": query,
                    "type":  "lodging",
                    "key":   api_key,
                },
            )
            res = r.json()
            if not res.get("results"):
                return data

            place = res["results"][0]
            data.name     = place.get("name")
            data.address  = place.get("formatted_address")
            data.latitude = place.get("geometry", {}).get("location", {}).get("lat")
            data.longitude = place.get("geometry", {}).get("location", {}).get("lng")

            # Extract location from address
            if data.address:
                parts = data.address.split(",")
                if len(parts) >= 2:
                    data.location = parts[-3].strip() if len(parts) >= 3 else parts[-2].strip()

            # Photos (need a second call for each photo reference)
            place_id = place.get("place_id")
            if place_id:
                details_r = await client.get(
                    "https://maps.googleapis.com/maps/api/place/details/json",
                    params={
                        "place_id": place_id,
                        "fields": "photos,name,formatted_address,geometry,types",
                        "key": api_key,
                    },
                )
                details = details_r.json().get("result", {})
                for ph in details.get("photos", [])[:5]:
                    ref = ph.get("photo_reference")
                    if ref:
                        photo_url = (
                            f"https://maps.googleapis.com/maps/api/place/photo"
                            f"?maxwidth=800&photo_reference={ref}&key={api_key}"
                        )
                        data.photos.append(photo_url)

        log.info("google_places_enrichment", query=query, name=data.name)
    except Exception as e:
        log.warning("google_places_failed", error=str(e))

    return data


# ── Source 3: Booking.com listing ─────────────────────────────────────────────

async def enrich_from_bdc(bdc_url: str) -> PropertyData:
    """
    Scrape a public Booking.com property listing.
    BDC pages have rich structured data in JSON-LD and meta tags.

    We extract: name, location, amenities, max occupancy, description, photos.
    We do NOT sign in — public page only.
    """
    data = PropertyData()
    if not bdc_url:
        return data

    # Normalise URL
    if "booking.com" not in bdc_url:
        return data
    if not bdc_url.startswith("http"):
        bdc_url = f"https://{bdc_url}"

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            }
        ) as client:
            r = await client.get(bdc_url)
            if r.status_code >= 400:
                return data
            html = r.text
    except Exception as e:
        log.warning("bdc_fetch_failed", url=bdc_url, error=str(e))
        return data

    # 1. JSON-LD (BDC uses Hotel schema) — parse directly from fetched HTML
    json_ld_matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    for raw in json_ld_matches:
        try:
            obj = json.loads(raw.strip())
            items = obj if isinstance(obj, list) else [obj]
            for item in items:
                t = str(item.get("@type", ""))
                if not any(k in t for k in ["Hotel","LodgingBusiness","Accommodation"]):
                    continue
                if not data.name:
                    data.name = item.get("name")
                addr = item.get("address", {})
                if isinstance(addr, dict) and not data.location:
                    data.location = addr.get("addressLocality") or addr.get("addressRegion")
                    data.address  = addr.get("streetAddress")
                if not data.description:
                    data.description = str(item.get("description", ""))[:500] or None
                for feat in item.get("amenityFeature", []):
                    name = feat.get("name","") if isinstance(feat,dict) else str(feat)
                    norm = normalise_amenity(name)
                    if norm and norm not in data.amenities:
                        data.amenities.append(norm)
        except Exception:
            continue

    # 2. BDC-specific meta tags
    name_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
    if not data.name and name_match:
        data.name = name_match.group(1).split("–")[0].split("|")[0].strip()

    # 3. BDC facility list (class-based)
    # BDC renders amenities as list items with data-testid or class patterns
    facility_patterns = [
        r'data-testid=["\']facility-item["\'][^>]*>([^<]+)<',
        r'class=["\'][^"\']*facility[^"\']*["\'][^>]*>([^<]{3,40})<',
        r'<span[^>]*class=["\'][^"\']*amenity[^"\']*["\'][^>]*>([^<]{3,40})<',
    ]
    for pattern in facility_patterns:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            text = m.group(1).strip()
            norm = normalise_amenity(text)
            if norm and norm not in data.amenities:
                data.amenities.append(norm)

    # 4. Max occupancy
    occ_match = re.search(r'(\d+)\s*(?:guests?|people|persons?|adults?)', html, re.IGNORECASE)
    if occ_match and not data.max_guests:
        try:
            n = int(occ_match.group(1))
            if 1 <= n <= 30:
                data.max_guests = n
        except ValueError:
            pass

    # 5. Photos from og:image tags
    for m in re.finditer(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html):
        url2 = m.group(1)
        if url2 and len(data.photos) < 8:
            data.photos.append(url2)

    log.info("bdc_enrichment", url=bdc_url,
             name=data.name, amenities=len(data.amenities))
    return data


# ── Source 4: WebHotelier API ─────────────────────────────────────────────────

async def enrich_from_webhotelier(api_key: str, property_id: str) -> PropertyData:
    """
    Pull property details directly from WebHotelier v2 API.
    Most complete source — returns room types, amenities, photos, rates.
    """
    data = PropertyData()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.webhotelier.net/v2/properties/{property_id}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Property-Id": property_id,
                },
            )
            if r.status_code != 200:
                # Try listing endpoint as fallback
                r2 = await client.get(
                    "https://api.webhotelier.net/v2/properties",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "X-Property-Id": property_id,
                    },
                )
                if r2.status_code != 200:
                    return data
                prop_list = r2.json()
                props = prop_list if isinstance(prop_list, list) else prop_list.get("data", [])
                prop = next((p for p in props if str(p.get("id")) == str(property_id)), None)
                if not prop:
                    return data
            else:
                prop = r.json()

        data.name        = prop.get("name") or prop.get("hotel_name")
        data.address     = prop.get("address") or prop.get("street_address")
        data.location    = (prop.get("city") or prop.get("location") or
                           prop.get("destination"))
        data.description = str(prop.get("description", ""))[:500] or None
        data.latitude    = float(prop.get("latitude", 0)) or None
        data.longitude   = float(prop.get("longitude", 0)) or None

        # Room types → max_guests, bedrooms
        rooms = prop.get("room_types") or prop.get("rooms") or []
        if rooms:
            capacities = [r.get("max_occupancy") or r.get("capacity", 0) for r in rooms]
            bedrooms   = [r.get("bedrooms", 0) for r in rooms]
            if max(capacities, default=0):
                data.max_guests = max(capacities)
            if max(bedrooms, default=0):
                data.bedrooms = max(bedrooms)

        # Amenities
        for a in prop.get("amenities", []):
            name = a.get("name","") if isinstance(a,dict) else str(a)
            norm = normalise_amenity(name)
            if norm and norm not in data.amenities:
                data.amenities.append(norm)

        # Base rate
        rate = prop.get("base_rate") or prop.get("from_price")
        if rate:
            try: data.base_rate = float(rate)
            except: pass

        # Photos
        for ph in prop.get("photos", [])[:8]:
            url2 = ph.get("url") if isinstance(ph, dict) else str(ph)
            if url2: data.photos.append(url2)

        log.info("webhotelier_enrichment", property_id=property_id, name=data.name)
    except Exception as e:
        log.warning("webhotelier_enrichment_failed", error=str(e))

    return data


# ── Source 5: Claude vision ───────────────────────────────────────────────────

async def enrich_from_photos(image_base64_list: list[str]) -> PropertyData:
    """
    Send property photos to Claude and ask it to identify:
    - Number of bedrooms visible
    - Number of bathrooms
    - Amenities (pool, sea view, garden, bbq, gym etc.)
    - Property style (luxury villa, boutique hotel, etc.)

    Returns a PropertyData with amenities and bedroom/bathroom counts.
    Max 4 images to keep token cost low.
    """
    data = PropertyData()
    if not image_base64_list:
        return data

    import anthropic as _anthropic
    client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    images = image_base64_list[:4]
    content: list[dict] = []

    for b64 in images:
        # Detect format from base64 header or assume JPEG
        media_type = "image/jpeg"
        if b64.startswith("iVBOR"):
            media_type = "image/png"
        elif b64.startswith("R0lGOD"):
            media_type = "image/gif"
        elif b64.startswith("UklGR"):
            media_type = "image/webp"

        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })

    content.append({
        "type": "text",
        "text": (
            "Look at these property photos and extract the following information. "
            "Respond ONLY with valid JSON, no other text:\n\n"
            "{\n"
            '  "bedrooms": <integer or null>,\n'
            '  "bathrooms": <integer or null>,\n'
            '  "max_guests": <estimated integer or null>,\n'
            '  "amenities": ["pool","sea_view","garden","bbq","gym","beach_access","ac","parking"],\n'
            '  "style": "<luxury villa|boutique hotel|apartment|guesthouse>",\n'
            '  "confidence": <0-100>\n'
            "}\n\n"
            "Only include amenities you can clearly see. "
            "Use these exact amenity slugs: pool, sea_view, wifi, ac, bbq, "
            "parking, gym, beach_access, garden, concierge."
        ),
    })

    try:
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=300,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()
        clean = re.sub(r"^```(?:json)?\s*|```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(clean)

        data.bedrooms   = parsed.get("bedrooms")
        data.bathrooms  = parsed.get("bathrooms")
        data.max_guests = parsed.get("max_guests")

        for a in parsed.get("amenities", []):
            if a not in data.amenities:
                data.amenities.append(a)

        log.info("vision_enrichment",
                 bedrooms=data.bedrooms,
                 amenities=data.amenities,
                 confidence=parsed.get("confidence"))
    except Exception as e:
        log.warning("vision_enrichment_failed", error=str(e))

    return data


# ── Full pipeline ─────────────────────────────────────────────────────────────

async def run_enrichment_pipeline(
    website_url:   str | None = None,
    bdc_url:       str | None = None,
    wh_api_key:    str | None = None,
    wh_property_id: str | None = None,
    photos_b64:    list[str] | None = None,
) -> dict[str, Any]:
    """
    Run all available enrichment sources in priority order.
    Returns merged PropertyData as dict with completeness score.
    """
    result = PropertyData()

    # 1. Website (always try if URL provided)
    if website_url:
        website_data = await enrich_from_website(website_url)
        result.fill_from(website_data, "website")

        # 2. Google Places using name + location from website
        if result.name or website_url:
            query = f"{result.name} {result.location or ''}" if result.name else website_url
            places_data = await enrich_from_google_places(query.strip())
            result.fill_from(places_data, "google_places")

    # 3. BDC listing
    if bdc_url:
        bdc_data = await enrich_from_bdc(bdc_url)
        result.fill_from(bdc_data, "booking_com")

    # 4. WebHotelier API (highest quality when available)
    if wh_api_key and wh_property_id:
        wh_data = await enrich_from_webhotelier(wh_api_key, wh_property_id)
        result.fill_from(wh_data, "webhotelier")

    # 5. Vision (fills amenities and room counts from photos)
    if photos_b64:
        vision_data = await enrich_from_photos(photos_b64)
        result.fill_from(vision_data, "claude_vision")

    log.info("enrichment_pipeline_complete",
             name=result.name, completeness=result._completeness(),
             sources=list(set(result.sources.values())),
             amenities=result.amenities)

    return result.to_dict()
