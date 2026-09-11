from __future__ import annotations

import base64
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from .config import int_setting, setting


VALID_SELECTED_AGENCIES = {"getty", "adobe_shutterstock"}
DEFAULT_SELECTED_AGENCY = "adobe_shutterstock"
DEFAULT_AGENCY_ROUTING_THRESHOLDS = {
    "strong_getty_threshold": 88,
    "possible_getty_threshold": 73,
    "adobe_shutterstock_threshold": 58,
}
DEFAULT_LM_STUDIO_BASE_URL = str(setting("models.lm_studio_base_url", "http://localhost:1234/v1", "LM_STUDIO_BASE_URL")).strip() or "http://localhost:1234/v1"
DEFAULT_LM_STUDIO_MODEL = str(setting("models.lm_studio_model", "", "LM_STUDIO_MODEL")).strip()
DEFAULT_HYBRID_METADATA_MODEL = (
    str(setting("models.hybrid_local_model", "", "HYBRID_LOCAL_MODEL"))
    or str(setting("models.hybrid_local_metadata_model", "", "HYBRID_LOCAL_METADATA_MODEL"))
    or str(setting("models.hybrid_local_fact_model", "", "HYBRID_LOCAL_FACT_MODEL"))
    or DEFAULT_LM_STUDIO_MODEL
).strip()
DEFAULT_CLOUD_PROVIDER = str(setting("models.fallback_provider", "gemini", "PHOTO_METADATA_FALLBACK_PROVIDER")).strip().lower()
ALLOW_CLOUD_FALLBACK = str(setting("models.allow_cloud_fallback", False, "PHOTO_METADATA_ALLOW_CLOUD_FALLBACK")).strip().lower() in ("1", "true", "yes", "on")
GEMINI_MODEL_NAME = str(setting("models.gemini_model", "gemini-3-flash-preview", "GEMINI_MODEL")).strip()
OPENAI_MODEL_NAME = str(setting("models.openai_model", "gpt-5.4-mini", "OPENAI_METADATA_MODEL")).strip()
NOMINATIM_REVERSE_URL = str(setting("geocoding.reverse_url", "https://nominatim.openstreetmap.org/reverse", "PHOTO_METADATA_REVERSE_GEOCODER_URL")).strip()
NOMINATIM_USER_AGENT = str(setting("geocoding.user_agent", "PhotoMetadataTool/1.0", "PHOTO_METADATA_GEOCODER_USER_AGENT")).strip() or "PhotoMetadataTool/1.0"
WIKIPEDIA_GEOSEARCH_URL = str(setting("geocoding.wikipedia_geosearch_url", "https://en.wikipedia.org/w/api.php", "PHOTO_METADATA_WIKIPEDIA_GEOSEARCH_URL")).strip()

_reverse_geocode_cache: dict[tuple[float, float], dict] = {}
_wikipedia_geosearch_cache: dict[tuple[float, float], list[dict]] = {}


def generate_metadata_for_image(image_path: str, original_filename: str, context: dict) -> dict:
    """Generate Lightroom-ready stock metadata using the configured hybrid model path."""
    path = Path(image_path)
    context = context or {}
    agency_style = str(context.get("agency_style") or setting("metadata.agency_style", "generic", "PHOTO_METADATA_AGENCY_STYLE")).strip()
    thresholds = normalize_agency_routing_thresholds(
        context.get("agency_routing_thresholds")
        or setting("metadata.agency_routing_thresholds", DEFAULT_AGENCY_ROUTING_THRESHOLDS)
    )
    date_str = _extract_date_created_from_image(path)
    gps_lat, gps_lon = _extract_gps_from_image(path)
    if gps_lat is None or gps_lon is None:
        raise ValueError(
            "GPS latitude and longitude are required for the hybrid metadata path. "
            "The exported preview JPEG has no readable GPS EXIF."
        )

    logs: list[str] = []
    log = logs.append
    base_folder = path.parent

    # Hybrid path: local/primary first, then configured cloud fallback.
    local_model = str(context.get("hybrid_metadata_model") or DEFAULT_HYBRID_METADATA_MODEL).strip()
    local_error_status = ""
    local_error_raw = ""
    if local_model:
        try:
            client = create_lm_studio_client(str(context.get("lm_studio_base_url") or DEFAULT_LM_STUDIO_BASE_URL))
            result, status, raw = call_hybrid_local_lm_for_metadata(
                client=client,
                metadata_model=local_model,
                image_path=path,
                base_folder=base_folder,
                gps_lat=gps_lat,
                gps_lon=gps_lon,
                date_str=date_str,
                agency_style=agency_style,
                agency_routing_thresholds=thresholds,
                log_func=log,
                exif_data=_context_to_exif(context),
            )
            if status.startswith("ok_") and result is not None:
                return _to_api_response(result, date_str, status, logs)
            local_error_status = status
            local_error_raw = raw
            log(f"Hybrid local path failed: {status}.")
        except Exception as exc:
            local_error_status = f"hybrid_local_exception_{type(exc).__name__}"
            local_error_raw = str(exc)
            log(f"Hybrid local path exception: {type(exc).__name__}: {exc}.")
    else:
        local_error_status = "hybrid_local_metadata_model_missing"
        local_error_raw = "Hybrid local metadata model id is empty."
        log("Hybrid local metadata model is not configured.")

    if not ALLOW_CLOUD_FALLBACK:
        raise RuntimeError(f"Hybrid local path failed and cloud fallback is disabled: {local_error_status}. {local_error_raw}")

    result, status, raw, retries = call_configured_cloud_fallback(
        image_path=path,
        base_folder=base_folder,
        gps_lat=gps_lat,
        gps_lon=gps_lon,
        date_str=date_str,
        agency_style=agency_style,
        agency_routing_thresholds=thresholds,
        log_func=log,
    )
    if status.startswith("ok_") and result is not None:
        if retries:
            log(f"Cloud fallback JSON retries used: {retries}")
        return _to_api_response(result, date_str, status, logs)
    raise RuntimeError(f"Metadata generation failed: {status}. {raw}")


def _to_api_response(result: "MetadataResult", date_str: str, status: str, logs: list[str]) -> dict:
    description = _build_final_description(result, date_str)
    selected_agency = normalize_selected_agency(result.selected_agency)
    agency_instruction = f"agency:{selected_agency}"
    if selected_agency == "getty":
        agency_instruction = "agency:getty_editorial" if result.editorial else "agency:getty_commercial"
    elif result.editorial and selected_agency == "adobe_shutterstock":
        agency_instruction = "agency:shutterstock_editorial"
    return {
        "title": result.title.strip(),
        "description": description.strip(),
        "keywords": [item.strip() for item in result.keywords.split(",") if item.strip()],
        "agency_instruction": agency_instruction,
        "editorial": bool(result.editorial),
        "commercial": not bool(result.editorial),
        "reason": f"{status}; {' | '.join(logs[-4:])}"[:500],
    }


def _build_final_description(data: "MetadataResult", date_str: str) -> str:
    base_desc = data.description.rstrip(" .,")
    ai_loc = ", ".join([part for part in (data.city.strip(), data.country.strip()) if part])
    if data.editorial:
        if ai_loc and date_str:
            return f"{base_desc}. {ai_loc} - {date_str}"
        if ai_loc:
            return f"{base_desc}. {ai_loc}"
        if date_str:
            return f"{base_desc}. {date_str}"
        return base_desc
    return f"{base_desc}. {ai_loc}" if ai_loc else base_desc


def _context_to_exif(context: dict) -> dict:
    return {
        "DateTimeOriginal": str(context.get("capture_time") or ""),
        "GPSLatitude": "",
        "GPSLongitude": "",
        "City": str(context.get("city") or ""),
        "State": str(context.get("state") or ""),
        "Country": str(context.get("country") or ""),
    }


def _extract_gps_from_image(image_path: Path) -> tuple[float | None, float | None]:
    try:
        with Image.open(image_path) as image:
            exif = image.getexif()
            gps_info = exif.get_ifd(0x8825)
    except Exception:
        return None, None
    if not gps_info:
        return None, None

    lat = _exif_gps_value_to_decimal(gps_info.get(2), gps_info.get(1))
    lon = _exif_gps_value_to_decimal(gps_info.get(4), gps_info.get(3))
    if lat is None or lon is None:
        return None, None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None, None


def _exif_gps_value_to_decimal(value, ref) -> float | None:
    if not value:
        return None
    try:
        degrees = _ratio_to_float(value[0])
        minutes = _ratio_to_float(value[1])
        seconds = _ratio_to_float(value[2])
    except (TypeError, IndexError, ZeroDivisionError):
        return None
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if str(ref or "").upper() in ("S", "W"):
        decimal *= -1
    return decimal


def _ratio_to_float(value) -> float:
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    if numerator is not None and denominator is not None:
        return float(numerator) / float(denominator)
    if isinstance(value, tuple) and len(value) == 2:
        return float(value[0]) / float(value[1])
    return float(value)


def _format_context_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = re.sub(r"\.\d{1,6}\b", "", raw)
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", normalized)
    if iso_match:
        try:
            return datetime.fromisoformat(iso_match.group(0).replace(" ", "T")).strftime("%B %d, %Y")
        except ValueError:
            pass
    for fmt in (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d",
        "%d-%b-%y %I:%M:%S %p",
        "%d-%b-%Y %I:%M:%S %p",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(_date_prefix_for_format(normalized, fmt), fmt).strftime("%B %d, %Y")
        except ValueError:
            continue
    return raw


def _date_prefix_for_format(value: str, fmt: str) -> str:
    lengths = {
        "%Y:%m:%d %H:%M:%S": 19,
        "%Y-%m-%d": 10,
        "%d-%b-%y %I:%M:%S %p": 21,
        "%d-%b-%Y %I:%M:%S %p": 23,
    }
    length = lengths.get(fmt)
    return value[:length] if length else value


def _extract_date_created_from_image(image_path: Path) -> str:
    xmp_date = _extract_xmp_date_created(image_path)
    if xmp_date:
        return xmp_date
    try:
        with Image.open(image_path) as image:
            exif = image.getexif()
            for tag in (36867, 36868, 306):
                formatted = _format_context_date(exif.get(tag))
                if formatted:
                    return formatted
    except Exception:
        return ""
    return ""


def _extract_xmp_date_created(image_path: Path) -> str:
    try:
        text = image_path.read_bytes().decode("utf-8", errors="ignore")
    except OSError:
        return ""
    patterns = (
        r"<photoshop:DateCreated>(.*?)</photoshop:DateCreated>",
        r'photoshop:DateCreated=["\'](.*?)["\']',
        r"<xmp:CreateDate>(.*?)</xmp:CreateDate>",
        r'xmp:CreateDate=["\'](.*?)["\']',
        r"<exif:DateTimeOriginal>(.*?)</exif:DateTimeOriginal>",
        r'exif:DateTimeOriginal=["\'](.*?)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        formatted = _format_context_date(match.group(1).strip())
        if formatted:
            return formatted
    return ""


def create_lm_studio_client(base_url: str):
    from openai import OpenAI

    api_key = str(setting("models.lm_studio_api_key", "lm-studio", "LM_STUDIO_API_KEY")).strip() or "lm-studio"
    return OpenAI(base_url=normalize_lm_studio_base_url(base_url), api_key=api_key)


def normalize_lm_studio_base_url(base_url: str) -> str:
    value = (base_url or DEFAULT_LM_STUDIO_BASE_URL).strip().rstrip("/")
    if not value:
        raise RuntimeError("LM Studio base URL is empty.")
    if not value.endswith("/v1"):
        value = f"{value}/v1"
    return value


def create_resized_jpeg(source_path: Path, base_folder: Path) -> Path:
    resize_folder = base_folder / "_resized_for_ai"
    resize_folder.mkdir(exist_ok=True)
    out_path = resize_folder / (source_path.stem + "_1200.jpg")
    with Image.open(source_path) as img:
        img = img.convert("RGB")
        w, h = img.size
        max_side = max(w, h)
        if max_side > 1200:
            scale = 1200 / float(max_side)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img.save(out_path, "JPEG", quality=90)
    return out_path


def encode_image_to_base64(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def format_gps_for_prompt(lat: float, lon: float) -> str:
    return f"{lat:.6f}, {lon:.6f}"


def normalize_agency_routing_thresholds(agency_routing_thresholds=None) -> dict:
    thresholds = DEFAULT_AGENCY_ROUTING_THRESHOLDS.copy()
    if isinstance(agency_routing_thresholds, dict):
        for key, default_value in DEFAULT_AGENCY_ROUTING_THRESHOLDS.items():
            try:
                thresholds[key] = int(agency_routing_thresholds.get(key, default_value))
            except (TypeError, ValueError):
                thresholds[key] = default_value
    strong = thresholds["strong_getty_threshold"]
    possible = thresholds["possible_getty_threshold"]
    adobe = thresholds["adobe_shutterstock_threshold"]
    if not (0 <= adobe <= 100 and 0 <= possible <= 100 and 0 <= strong <= 100 and strong > possible > adobe):
        return DEFAULT_AGENCY_ROUTING_THRESHOLDS.copy()
    return thresholds


def build_common_style_rules() -> str:
    return """
GENERAL STYLE RULES:
- Write all returned metadata in English only, including title, description, keywords, city, and country.
- Use English names for places and landmarks when an English name is available.
- Write factual, concise, professional stock-photo metadata.
- Describe only visible content in a factual, non-promotional way.
- Do not use phrases like "in this image".
""".strip()


def agency_rules(agency_style: str) -> str:
    style = agency_style.lower()
    if style == "getty":
        keyword_rule = "20 to 30 keywords"
        title_rule = "Sentence case. Maximum 120 characters."
    elif style == "shutterstock":
        keyword_rule = "40 to 60 keywords"
        title_rule = "Descriptive and commercially useful."
    elif style == "adobe":
        keyword_rule = "10 to 25 keywords"
        title_rule = "Clean, simple, and descriptive."
    else:
        keyword_rule = "15 to 30 keywords"
        title_rule = "Clean and descriptive."
    return f"""
AGENCY STYLE:
TITLE:
- {title_rule}
- Start with the main visible subject.
- Location may appear naturally inside the title, but not at the beginning or end.
- Do not include dates.

DESCRIPTION:
- 1 to 3 factual sentences.
- Describe only visible content.
- Location may appear naturally inside the description when relevant, but not at the beginning or end.
- Do not include dates.

KEYWORDS:
- {keyword_rule}.
- Return as an array of strings.
- Lowercase.
- No duplicates.
- No dates.
- Include relevant subject terms, scene terms, and location or landmark terms when known.
""".strip()


def build_common_editorial_rules() -> str:
    return """
EDITORIAL FLAG:
- Set "editorial" to true only if the image visibly shows any identifiable human figure/person, including people seen from behind, side view, partially visible, small but clearly visible, or not facing the camera; OR if it shows brands, logos, trademarks, product packaging, signage, event signage, sports uniforms or teams, license plates, or other personal identifiers.
- Do not set "editorial" to true based only on GPS data, city/country names, or landmark identification.
- Use false for generic commercial subjects with no visible editorial trigger, including flowers, plants, landscapes, gardens, nature, textures, food, objects, and non-branded architecture.
- If a human figure or logo is visible, that is a clear editorial trigger.
""".strip()


def build_common_agency_selection_rules(agency_routing_thresholds=None) -> str:
    return """
AGENCY SELECTION:
Evaluate the image for stock agency routing based on:
- Technical Excellence: sharpness, exposure, clean processing, professional optics.
- Rarity & Access: difficult access, rare conditions, unusual timing, temporary scenes, or uncommon viewpoints.
- Authentic Story: candid human activity, documentary value, emotional/contextual storytelling.
- Atmospheric / Cinematic Distinctiveness: mood, light, weather, solitude, night atmosphere, environmental tension, or visual emotion.
- Visual Interpretation & Memorability: distinctive composition, graphic strength, unusual visual treatment, or imagery likely to stand out among many similar images.
- Commercial Utility: buyer usefulness, but commercial value alone is NOT enough for Getty.
- Visual Differentiation: whether the visible treatment is distinctive rather than a routine, generic, or easily repeated stock-style view.
- Landmark / Location Significance: whether supplied location context and visible evidence identify a recognizable or important landmark or location, and whether the image presents it distinctively.

Return selected_agency as exactly one of:
- getty
- adobe_shutterstock

Decision Rules:
- getty: Image must be technically usable, properly exposed, and reasonably sharp AND pass at least ONE Getty eligibility gate:
  A) less routine or less easily repeated due to conditions, timing, access, or viewpoint;
  B) strong authentic candid human/documentary story;
  C) clear atmospheric, cinematic, environmental, or visual distinctiveness.
  D) a recognizable or important landmark or location presented with distinctive composition, conditions, timing, access, context, or visual interpretation.
- adobe_shutterstock: technically clean but repeatable/generic images, normal travel scenes, ordinary architecture, birds, landmarks, landscapes, objects, and other common subjects without a distinctive documentary, atmospheric, visual, or emotional edge.

POSED vs CANDID PEOPLE RULE (HARD OVERRIDE):
Determine whether people in the image are CANDID or POSED based only on visible behavior.

CANDID:
- not looking at camera
- engaged in natural activity
- unaware of the photographer
- part of a wider observed scene, not arranged as the main posed subject

POSED:
- facing or looking at the camera
- smiling for the camera or clearly aware
- standing still as the main subject
- portrait-style composition or intentional posing

HARD RULE:
- If the image contains ANY clearly posed person, selected_agency MUST be adobe_shutterstock.
- This rule overrides ALL other conditions.
- If people are candid, continue normal evaluation.
- Candid people only remove the posed-person block.
- Do NOT select getty merely because people are candid.
- Mere human presence is not story.

CANDID PEOPLE ARE NOT AUTOMATICALLY GETTY:
- Candid people must create a strong documentary/story moment, emotional tension, cultural significance, unusual activity, or clear editorial context.
- Ordinary tourists, diners, walkers, shoppers, passengers, beachgoers, or people casually present in a scene usually route to adobe_shutterstock.
- Environmental people used only for scale or atmosphere are usually adobe_shutterstock unless the full scene is exceptional.
- A candid person only supports Getty when their activity or placement makes the image meaningfully stronger than the same scene without them.

Internal Decision Tests:
1. Recreation Test:
   Could a talented traveler with a modern smartphone easily recreate BOTH the subject and the visual impact of this image tomorrow from a public location?
   - If yes, apply a moderate penalty.
   - If no, Getty eligibility is strengthened.
   - Exceptional composition, atmosphere, timing, weather, access, or storytelling can offset this penalty.

2. Subject vs Interpretation Test:
   - Subject Documentation: the image mainly records what a place, object, animal, building, or landscape looks like.
   - Distinctive Interpretation: the image adds unusual light, atmosphere, visual tension, graphic composition, emotional impact, rare timing, or compelling context.
   Images that primarily document a subject lean toward adobe_shutterstock.
   Images that provide a distinctive interpretation may qualify for Getty even without a human story.

3. Atmosphere Test:
   Does the image create a strong mood or cinematic/environmental feeling beyond simple documentation?
   Examples: fog, storm light, night streets, solitude, unusual shadows, rare weather, emotional quietness, dramatic natural conditions.
   If yes, Getty may be considered even without people.

4. Human Story Gate:
   To pass the Getty human-story gate, answer YES to this:
   'Would the image still be editorially meaningful if the location were unknown?'
   If no, route to adobe_shutterstock unless rarity/access or atmosphere gate is very strong.

5. Visual Memorability Test:
   Would an experienced photo editor remember this image after viewing 100 similar images?
   If yes, Getty eligibility increases.
   If no, adobe_shutterstock becomes more likely.

6. Landmark / Location Significance Test:
   Does the supplied location context and visible image evidence identify a recognizable or important landmark or location?
   - Use only supplied reverse-geocode information, supplied nearby candidates, and visible evidence.
   - Do not invent or assume a landmark from visual similarity alone.
   - Distinctive conditions, composition, timing, access, context, or interpretation strengthen Getty eligibility.
   - A routine view may qualify for Getty when its composition, light, atmosphere, context, or buyer usefulness is clearly above ordinary.

Important Constraints:
- Technical quality is required for Getty but is never sufficient by itself.
- Commercial value is useful but is never sufficient by itself.
- Location, GPS, city, country, or famous landmark status must not drive Getty selection by itself.
- Do not estimate current market demand, sales potential, search trends, popularity, or the current contents of any stock library.
- A recognizable landmark or location must also provide distinctive interpretation, unusual conditions, valuable context, rare timing, access, or difficult-to-repeat coverage.
- Generic images default to adobe_shutterstock, but attainable subjects may qualify for Getty when their visual treatment is notably distinctive.
- No-people images may still be Getty if they have exceptional atmosphere, rare timing, difficult access, strong environmental storytelling, or distinctive visual interpretation.
- People images should not automatically become Getty. Candid people only matter if they create story, context, or emotional/documentary value.
- Ordinary candid people in common travel scenes usually remain adobe_shutterstock.
""".strip()


def build_common_output_rules() -> str:
    return """
OUTPUT RULES:
- Return only valid JSON.
- No markdown.
- No backticks.
- No explanation.
- No text before or after the JSON.
- Fill every key.
- "keywords" must be an array of lowercase strings.
- "selected_agency" must be exactly "getty" or "adobe_shutterstock".

JSON SCHEMA:
{
  "title": "string",
  "description": "string",
  "keywords": ["keyword"],
  "editorial": true,
  "city": "string",
  "country": "string",
  "selected_agency": "getty"
}
""".strip()


def build_hybrid_local_llm_system_prompt(agency_style: str, agency_routing_thresholds=None) -> str:
    return f"""
You are a professional stock photography metadata editor.

Treat each request independently and ignore any previous tasks.

{build_common_style_rules()}

{agency_rules(agency_style)}

LOCATION RULES:
- Use reverse geocode as the source of truth for country, city, and area.
- You may use nearby candidates in the title, description, and keywords only if the candidate is clearly visible, obviously the place/landmark, or describes the broad visible setting.
- If no candidate is visually supported, use only the reverse geocode location.
- Do not invent city, country, area, landmark, or place names.
- If reverse geocode is unavailable, keep location wording conservative and return empty city/country strings when uncertain.

{build_common_editorial_rules()}

{build_common_agency_selection_rules(agency_routing_thresholds)}

{build_common_output_rules()}
""".strip()


def build_cloud_system_prompt(agency_style: str, agency_routing_thresholds=None) -> str:
    return f"""
You are a professional stock photography metadata editor.

{build_common_style_rules()}

{agency_rules(agency_style)}

LOCATION RESOLUTION RULES:
- If GPS coordinates are provided, they are the only source of truth for location.
- Determine location from GPS first.
- Do not override GPS-derived location based on visual appearance.
- Visual recognition may help describe the scene, but must not replace GPS-based location.
- Convert GPS into a well-known, commonly recognized place name suitable for stock photography.
- Avoid obscure villages, districts, or administrative labels.
- Never name a city, landmark, or place that contradicts the GPS-derived location.
- If city or country is uncertain, return empty strings rather than guessing.

LANDMARK RULES:
- Identify specific landmarks, mountains, buildings, bridges, temples, skylines, or famous places only when reasonably confident.
- If not confident, describe the subject generically and do not invent a landmark.
- Any landmark name must remain consistent with GPS-derived location.

{build_common_editorial_rules()}

{build_common_agency_selection_rules(agency_routing_thresholds)}

{build_common_output_rules()}
""".strip()


def build_common_user_prompt(gps_str: str, date_str: str) -> str:
    return f"""
INPUT CONTEXT:
GPS coordinates (WGS84): {gps_str}
Capture date (context only; do not include in title or description): {date_str or "Unknown"}

TASK:
1) Use the image and GPS coordinates to determine the best matching city and country.
2) Return the city and country in the JSON output.
3) If city or country is uncertain, return empty strings rather than guessing.
4) Return selected_agency using the agency selection rules.

IMPORTANT APP BEHAVIOR:
- The app may append city and country to the final description when available.
- If the image is editorial, the app may also append the capture date.
- Do not append "location - date" yourself.
""".strip()


def _build_hybrid_single_call_prompt(gps_str: str, date_str: str, exif_location: dict, geocoder_result: dict, wikipedia_candidates: list[dict]) -> str:
    context = {
        "gps_coordinates_wgs84": gps_str,
        "capture_date": date_str or "",
        "reverse_geocode_source_of_truth": {
            "provider": geocoder_result.get("provider", ""),
            "status": geocoder_result.get("status", ""),
            "display_name": geocoder_result.get("display_name", ""),
            "name": geocoder_result.get("name", ""),
            "type": geocoder_result.get("type", ""),
            "category": geocoder_result.get("category", ""),
            "city": geocoder_result.get("city", ""),
            "state": geocoder_result.get("state", ""),
            "country": geocoder_result.get("country", ""),
            "country_code": geocoder_result.get("country_code", ""),
            "address": geocoder_result.get("address", {}),
            "nearby_places": geocoder_result.get("nearby_places", []),
            "nearest_well_known_location": geocoder_result.get("nearest_well_known_location", ""),
            "error": geocoder_result.get("error", ""),
        },
        "exif_location_supporting_only": exif_location,
        "nearby_candidates": wikipedia_candidates,
    }
    return f"""
Generate polished stock-photo metadata from the image and supplied location context.
All returned metadata must be written in English only, including title, description, keywords, city, and country.

INPUT CONTEXT:
{json.dumps(context, ensure_ascii=False, indent=2)}

LOCATION RULES:
- Use reverse geocode as the source of truth for country, city, and area.
- You may use nearby candidates only if clearly visually supported.
- If no candidate is visually supported, use only the reverse geocode location.

TASK:
1. Describe only visible image content.
2. Return city and country from reverse geocode when available.
3. Return selected_agency using the agency selection rules.
4. Do not append "location - date" yourself.

Return only valid JSON with this exact schema:
{{
  "title": "string",
  "description": "string",
  "keywords": ["keyword"],
  "editorial": true,
  "city": "string",
  "country": "string",
  "selected_agency": "getty"
}}
""".strip()


def call_hybrid_local_lm_for_metadata(client, metadata_model: str, image_path: Path, base_folder: Path, gps_lat: float, gps_lon: float, date_str: str, agency_style: str, agency_routing_thresholds=None, log_func=None, exif_data=None):
    metadata_model = (metadata_model or "").strip()
    if not metadata_model:
        return None, "hybrid_local_metadata_model_missing", "Hybrid Local LM metadata model id is empty."
    if log_func:
        log_func(f"Hybrid Local LM ({metadata_model}): preparing image.")
    resized_path = create_resized_jpeg(image_path, base_folder)
    b64 = encode_image_to_base64(resized_path)
    gps_str = format_gps_for_prompt(gps_lat, gps_lon)
    exif_location = _exif_location_context(exif_data)
    geocoder_result = _reverse_geocode_gps(gps_lat, gps_lon, log_func=log_func)
    wikipedia_candidates = _fetch_wikipedia_nearby_candidates(gps_lat, gps_lon, log_func=log_func)
    system_text = build_hybrid_local_llm_system_prompt(agency_style, agency_routing_thresholds)
    user_text = _build_hybrid_single_call_prompt(gps_str, date_str, exif_location, geocoder_result, wikipedia_candidates)

    try:
        response = client.chat.completions.create(
            model=metadata_model,
            messages=[
                {"role": "system", "content": system_text},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": user_text},
                    ],
                },
            ],
            max_tokens=6000,
            temperature=0.2,
        )
        raw = _chat_response_text(response)
        if not raw:
            return None, "hybrid_local_metadata_empty_response", _chat_response_debug_text(response)
        data, cleaned_raw = _extract_local_json_from_text(raw)
        result = MetadataResult.from_model_data(data)
        return result, "ok_hybrid_local_lm", cleaned_raw
    except Exception as exc:
        return None, f"hybrid_local_metadata_error_{type(exc).__name__}", str(exc)


def call_configured_cloud_fallback(image_path: Path, base_folder: Path, gps_lat: float, gps_lon: float, date_str: str, agency_style: str, agency_routing_thresholds=None, log_func=None):
    provider = DEFAULT_CLOUD_PROVIDER
    if provider == "openai":
        return call_openai_for_metadata(image_path, base_folder, gps_lat, gps_lon, date_str, agency_style, agency_routing_thresholds, log_func)
    return call_gemini_for_metadata(image_path, base_folder, gps_lat, gps_lon, date_str, agency_style, agency_routing_thresholds, log_func)


def call_openai_for_metadata(image_path: Path, base_folder: Path, gps_lat: float, gps_lon: float, date_str: str, agency_style: str, agency_routing_thresholds=None, log_func=None):
    from openai import OpenAI

    api_key = str(setting("models.openai_api_key", "", "OPENAI_API_KEY")).strip()
    if not api_key:
        return None, "openai_api_key_missing", "OPENAI_API_KEY environment variable is not set.", 0
    resized_path = create_resized_jpeg(image_path, base_folder)
    b64 = encode_image_to_base64(resized_path)
    gps_str = format_gps_for_prompt(gps_lat, gps_lon)
    try:
        response = OpenAI(api_key=api_key).chat.completions.create(
            model=OPENAI_MODEL_NAME,
            messages=[
                {"role": "system", "content": build_cloud_system_prompt(agency_style, agency_routing_thresholds)},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": build_common_user_prompt(gps_str, date_str)},
                    ],
                },
            ],
            max_completion_tokens=1000,
        )
        raw = response.choices[0].message.content
        data, cleaned = _extract_cloud_json_from_text(raw)
        return MetadataResult.from_model_data(data), "ok_openai", cleaned, 0
    except Exception as exc:
        return None, f"openai_error_{type(exc).__name__}", str(exc), 0


def call_gemini_for_metadata(image_path: Path, base_folder: Path, gps_lat: float, gps_lon: float, date_str: str, agency_style: str, agency_routing_thresholds=None, log_func=None, max_json_retries: int = 2):
    api_key = str(setting("models.gemini_api_key", "", "GEMINI_API_KEY")).strip()
    if not api_key:
        return None, "gemini_api_key_missing", "GEMINI_API_KEY environment variable is not set.", 0
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        return None, "gemini_dependency_missing", str(exc), 0

    client = genai.Client(api_key=api_key)
    resized_path = create_resized_jpeg(image_path, base_folder)
    image_bytes = resized_path.read_bytes()
    gps_str = format_gps_for_prompt(gps_lat, gps_lon)
    combined_prompt = f"{build_cloud_system_prompt(agency_style, agency_routing_thresholds)}\n\n{build_common_user_prompt(gps_str, date_str)}"
    last_raw = ""
    retries_used = 0
    for attempt in range(1, max_json_retries + 2):
        try:
            contents = [
                combined_prompt,
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            ] if attempt == 1 else [build_gemini_repair_prompt(_clean_model_text(last_raw))]
            response = client.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(max_output_tokens=2000, response_mime_type="application/json"),
            )
            last_raw = response.text
            data, cleaned = _extract_cloud_json_from_text(last_raw)
            return MetadataResult.from_model_data(data), "ok_gemini", cleaned, retries_used
        except Exception as exc:
            if attempt <= max_json_retries:
                retries_used += 1
                time.sleep(4.0)
                continue
            return None, f"gemini_error_{type(exc).__name__}", last_raw or str(exc), retries_used
    return None, "gemini_json_error", last_raw, retries_used


def build_gemini_repair_prompt(previous_response: str) -> str:
    return f"""
Your previous response did not match the required JSON schema.
Return the same result again, but as one valid JSON object only.
No markdown, no backticks, no explanation, and include every required key.
Previous response:
{previous_response}
""".strip()


def _exif_location_context(exif_data: dict | None) -> dict:
    exif_data = exif_data or {}
    return {
        "city": str(exif_data.get("City", "")).strip(),
        "state": str(exif_data.get("State", "")).strip(),
        "country": str(exif_data.get("Country", "")).strip(),
    }


def _env_int(name: str, default: int) -> int:
    try:
        config_path = {
            "PHOTO_METADATA_WIKIPEDIA_RADIUS_METERS": "geocoding.wikipedia_radius_meters",
            "PHOTO_METADATA_WIKIPEDIA_LIMIT": "geocoding.wikipedia_limit",
        }.get(name, "")
        if config_path:
            return int_setting(config_path, default, name)
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _reverse_geocode_gps(gps_lat: float, gps_lon: float, log_func=None) -> dict:
    key = (round(float(gps_lat), 6), round(float(gps_lon), 6))
    if key in _reverse_geocode_cache:
        return _reverse_geocode_cache[key]
    result = {
        "provider": "nominatim",
        "status": "not_run",
        "lat": key[0],
        "lon": key[1],
        "display_name": "",
        "name": "",
        "type": "",
        "category": "",
        "city": "",
        "state": "",
        "country": "",
        "country_code": "",
        "address": {},
        "nearby_places": [],
        "nearest_well_known_location": "",
        "error": "",
    }
    if not NOMINATIM_REVERSE_URL:
        result["status"] = "disabled"
        _reverse_geocode_cache[key] = result
        return result
    params = urllib.parse.urlencode(
        {
            "format": "jsonv2",
            "lat": f"{gps_lat:.6f}",
            "lon": f"{gps_lon:.6f}",
            "zoom": "18",
            "addressdetails": "1",
            "namedetails": "1",
            "extratags": "1",
            "accept-language": "en",
        }
    )
    try:
        req = urllib.request.Request(
            f"{NOMINATIM_REVERSE_URL}?{params}",
            headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept": "application/json", "Accept-Language": "en"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        address = data.get("address") if isinstance(data, dict) else {}
        address = address if isinstance(address, dict) else {}
        city = normalize_english_ascii_text(_first_nonempty(address, ["city", "town", "village", "municipality", "hamlet", "suburb", "county", "state_district", "state"]))
        country = normalize_english_ascii_text(address.get("country", ""))
        nearest = normalize_english_ascii_text(_first_nonempty(address, ["city", "town", "village", "municipality", "hamlet", "county", "state"]) or data.get("name", ""))
        if nearest and country and country.lower() not in nearest.lower():
            nearest = f"{nearest}, {country}"
        result.update(
            {
                "status": "ok",
                "display_name": normalize_english_ascii_text(data.get("display_name", "")),
                "name": normalize_english_ascii_text(data.get("name", "")),
                "type": str(data.get("type", "")).strip(),
                "category": str(data.get("category", "")).strip(),
                "city": city,
                "state": normalize_english_ascii_text(_first_nonempty(address, ["state", "region", "state_district", "county"])),
                "country": country,
                "country_code": str(address.get("country_code", "")).upper(),
                "address": {k: normalize_english_ascii_text(v) if isinstance(v, str) else v for k, v in address.items()},
                "nearby_places": _nearby_places(address),
                "nearest_well_known_location": nearest,
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        if log_func:
            log_func(f"Reverse geocoder unavailable: {exc}")
    _reverse_geocode_cache[key] = result
    return result


def _fetch_wikipedia_nearby_candidates(gps_lat: float, gps_lon: float, log_func=None) -> list[dict]:
    key = (round(float(gps_lat), 6), round(float(gps_lon), 6))
    if key in _wikipedia_geosearch_cache:
        return _wikipedia_geosearch_cache[key]
    if not WIKIPEDIA_GEOSEARCH_URL:
        _wikipedia_geosearch_cache[key] = []
        return []
    radius = max(10, min(_env_int("PHOTO_METADATA_WIKIPEDIA_RADIUS_METERS", 10000), 10000))
    limit = max(1, min(_env_int("PHOTO_METADATA_WIKIPEDIA_LIMIT", 10), 50))
    params = urllib.parse.urlencode({"action": "query", "list": "geosearch", "gscoord": f"{gps_lat:.6f}|{gps_lon:.6f}", "gsradius": str(radius), "gslimit": str(limit), "format": "json", "uselang": "en"})
    candidates = []
    try:
        req = urllib.request.Request(f"{WIKIPEDIA_GEOSEARCH_URL}?{params}", headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        seen = set()
        for item in data.get("query", {}).get("geosearch", []):
            title = normalize_english_ascii_text(item.get("title", ""))
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            dist = item.get("dist")
            candidates.append({"name": title, "source": "wikipedia_geosearch", "distance_km": round(float(dist) / 1000, 3) if dist is not None else None, "pageid": item.get("pageid")})
    except Exception as exc:
        if log_func:
            log_func(f"Wikipedia nearby candidates unavailable: {exc}")
    _wikipedia_geosearch_cache[key] = candidates
    return candidates


def _first_nonempty(mapping: dict, keys: list[str]) -> str:
    for key in keys:
        value = str(mapping.get(key, "")).strip()
        if value:
            return value
    return ""


def _nearby_places(address: dict) -> list[dict]:
    places = []
    seen = set()
    for key in ("attraction", "tourism", "amenity", "building", "historic", "road", "neighbourhood", "suburb", "city_district", "city", "town", "village", "county", "state"):
        value = str(address.get(key, "")).strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            places.append({"name": normalize_english_ascii_text(value), "type": key, "source": f"nominatim_address.{key}"})
    return places[:8]


def _clean_model_text(raw):
    if raw is None:
        return ""
    if isinstance(raw, list):
        raw = "\n".join([str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in raw])
    raw = str(raw).strip()
    raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    return raw.strip()


def _extract_cloud_json_from_text(raw):
    text = _clean_model_text(raw)
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found")
    content = text[start:].strip()
    end = content.rfind("}")
    if end != -1:
        content = content[: end + 1]
    return json.loads(content), content


def _extract_local_json_from_text(raw):
    text = _clean_model_text(raw)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found")
    content = text[start : end + 1]
    return json.loads(content), content


def _chat_response_text(response) -> str:
    choice = response.choices[0]
    message = choice.message
    for key in ("content", "reasoning_content", "reasoning", "text"):
        value = getattr(message, key, None)
        text = _stringify_message_content(value).strip()
        if text:
            return text
    return ""


def _stringify_message_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if value is not None:
                    parts.append(str(value))
        return "\n".join(parts)
    return str(content)


def _chat_response_debug_text(response) -> str:
    try:
        choice = response.choices[0]
        message = choice.message
        return json.dumps({"empty_response": True, "finish_reason": getattr(choice, "finish_reason", None), "message": str(message)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"empty_response": True, "debug_error": str(exc)}, ensure_ascii=False)


ASCII_TEXT_REPLACEMENTS = {
    "â€™": "'",
    "â€˜": "'",
    "â€œ": '"',
    "â€": '"',
    "â€“": "-",
    "â€”": "-",
}


def normalize_selected_agency(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in VALID_SELECTED_AGENCIES else DEFAULT_SELECTED_AGENCY


def normalize_english_ascii_text(value) -> str:
    text = str(value or "").strip()
    for source, replacement in ASCII_TEXT_REPLACEMENTS.items():
        text = text.replace(source, replacement)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.replace("?", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@dataclass
class MetadataResult:
    title: str
    description: str
    keywords: str
    editorial: bool
    city: str
    country: str
    selected_agency: str

    @classmethod
    def from_model_data(cls, data: dict) -> "MetadataResult":
        required = ["title", "description", "keywords", "editorial", "city", "country"]
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Missing required keys: {missing}")
        if not isinstance(data["editorial"], bool):
            raise ValueError("'editorial' must be true/false")
        return cls(
            title=normalize_english_ascii_text(data["title"]),
            description=normalize_english_ascii_text(data["description"]),
            keywords=cls._normalize_keywords(data["keywords"]),
            editorial=data["editorial"],
            city=normalize_english_ascii_text(data["city"]),
            country=normalize_english_ascii_text(data["country"]),
            selected_agency=normalize_selected_agency(data.get("selected_agency")),
        )

    @staticmethod
    def _normalize_keywords(keywords) -> str:
        if isinstance(keywords, list):
            cleaned = [normalize_english_ascii_text(item).lower() for item in keywords if isinstance(item, str) and item.strip()]
        elif isinstance(keywords, str):
            cleaned = [normalize_english_ascii_text(part).lower() for part in keywords.split(",") if part.strip()]
        else:
            raise ValueError("'keywords' must be a string or list")
        seen = set()
        deduped = []
        for keyword in cleaned:
            if keyword and keyword not in seen:
                seen.add(keyword)
                deduped.append(keyword)
        return ", ".join(deduped)
