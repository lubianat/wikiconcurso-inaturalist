from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    abort,
)
import requests
from datetime import datetime
from collections import defaultdict, Counter
from urllib.parse import quote
import os
import hashlib

app = Flask(__name__)

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------
VALID_START_DATE = datetime.strptime("2025-08-01", "%Y-%m-%d")
VALID_END_DATE = datetime.strptime("2026-08-31", "%Y-%m-%d")

VERTEBRATES = ["Mammalia", "Aves", "Reptilia", "Amphibia", "Actinopterygii"]
ARTHROPODS = ["Insecta", "Arachnida", "Crustacea", "Myriapoda"]

OPEN_LICENSES = {"cc-by", "cc-by-sa", "cc0"}

app.config["SECRET_KEY"] = "your_secret_key"  # change for production!
CACHE_FILE = "validated_observations.json"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def first_photo(observation):
    """Return the first photo-dict or None if none/invalid."""
    photos = observation.get("photos") or []
    return photos[0] if photos and isinstance(photos[0], dict) else None


def is_valid_date(observed_date: str | None) -> bool:
    """Return True iff the date string exists & falls inside the allowed window."""
    if not observed_date:
        return False
    try:
        date_str = observed_date.split("T")[0]  # handle ISO strings
        date = datetime.strptime(date_str, "%Y-%m-%d")
        return VALID_START_DATE <= date <= VALID_END_DATE
    except ValueError:
        return False


def categorize_photo(observation):
    ancestor_ids = (observation.get("taxon") or {}).get("ancestor_ids", "")
    if 355675 in ancestor_ids:  # ID for Vertebrata
        return "vertebrates"
    if 47120 in ancestor_ids:  # ID for Arthropoda
        return "arthropods"
    return "others"


def get_validation_categories(observation):
    """Return a list of validation flags for a single observation."""
    categories = []

    photo_obj = first_photo(observation)
    if photo_obj is None:
        categories.append("no-photo")
        return categories

    if not is_valid_date(observation.get("observed_on")):
        categories.append("date-before-september")

    if photo_obj.get("license_code") not in OPEN_LICENSES:
        categories.append("non-compatible-license")

    if observation.get("quality_grade") != "research":
        categories.append("non-research-grade")
        # “needs_id” fast-track
        taxon_rank = (observation.get("taxon") or {}).get("rank", "")
        if (
            observation.get("quality_grade") == "needs_id"
            and is_valid_date(observation.get("observed_on"))
            and photo_obj.get("license_code") in OPEN_LICENSES
            and observation.get("num_identification_agreements", 0) >= 2
            and taxon_rank == "genus"
        ):
            categories.append("validated")

    if not categories:
        categories.append("validated")

    return categories


def make_record(observation, photo, validation_categories=()):
    """Flatten an observation into the dict the card template expects."""
    return {
        "observation_id": observation.get("id") or "",
        "photo": photo,
        "author": (observation.get("user") or {}).get("login", "Unknown"),
        "date": observation.get("observed_on", "Unknown"),
        "license": photo.get("license_code", ""),
        "species": (observation.get("taxon") or {}).get("name", "Unknown"),
        "validation_categories": validation_categories,
    }


def organize_photos_by_user(valid_photos, unvalidated_photos):
    user_photos = defaultdict(
        lambda: {"validated": defaultdict(list), "unvalidated": defaultdict(list)}
    )

    for category, photos in valid_photos.items():
        for photo in photos:
            user_photos[photo["author"]]["validated"][category].append(photo)

    for category, photos in unvalidated_photos.items():
        for photo in photos:
            user_photos[photo["author"]]["unvalidated"][category].append(photo)

    return dict(sorted(user_photos.items()))  # alphabetic order


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.route("/logout")
def logout():
    session.pop("username", None)
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    return redirect(url_for("login"))


def fetch_project_observations(project_slug):
    per_page = 200
    page = 1
    all_results = []

    while True:
        url = (
            f"https://api.inaturalist.org/v1/observations?"
            f"project_id={project_slug}&per_page={per_page}&page={page}&order_by=observed_on"
        )
        try:
            response = requests.get(url)
            response.raise_for_status()
            results = response.json().get("results", [])
            if not results:
                break
            all_results.extend(results)
            if len(results) < per_page:
                break
            page += 1
        except requests.exceptions.RequestException as e:
            app.logger.error(f"Failed to fetch data: {e}")
            break

    return all_results


YEAR = 2026
PROJECT_SLUG = f"wikiconcurso-fotografico-inaturalist-{YEAR}"

WDQS_URL = "https://query.wikidata.org/sparql"
# P3151 = iNaturalist taxon ID
WDQS_QUERY = """
SELECT ?inat ?item ?en ?pt ?commons ?image WHERE {
  VALUES ?inat { %s }
  ?item wdt:P3151 ?inat .
  OPTIONAL { ?item wdt:P18 ?image }
  OPTIONAL { ?en schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  OPTIONAL { ?pt schema:about ?item ; schema:isPartOf <https://pt.wikipedia.org/> }
  OPTIONAL { ?commons schema:about ?item ; schema:isPartOf <https://commons.wikimedia.org/> }
}
"""


def fetch_wikidata_rows(inat_ids):
    """Ask WDQS which of these iNaturalist taxon IDs have items and sitelinks."""
    rows = []
    ids = sorted(inat_ids)
    for start in range(0, len(ids), 200):  # more than ~200 blows the URL length limit
        values = " ".join('"%d"' % i for i in ids[start : start + 200])
        response = requests.get(
            WDQS_URL,
            params={"query": WDQS_QUERY % values, "format": "json"},
            headers={"User-Agent": "wikiconcurso-inaturalist/1.0 (stats page)"},
            timeout=120,
        )
        response.raise_for_status()
        rows.extend(response.json()["results"]["bindings"])
    return rows


COMMONS_SPARQL_URL = "https://qlever.dev/api/wikimedia-commons"
# P12120 on Commons files links back to the iNaturalist observation ID (P5683 on Wikidata)
COMMONS_QUERY = """
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
SELECT ?inat_obs_id WHERE {
  VALUES ?inat_obs_id { %s }
  ?file wdt:P5683 ?inat_obs_id .
}
"""


def fetch_observations_on_commons(observation_ids):
    """Return the subset of observation IDs that already have a file on Commons."""
    found = set()
    ids = sorted(observation_ids)
    for start in range(0, len(ids), 200):
        values = " ".join('"%s"' % i for i in ids[start : start + 200])
        response = requests.get(
            COMMONS_SPARQL_URL,
            params={"query": COMMONS_QUERY % values},
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": "wikiconcurso-inaturalist/1.0 (stats page)",
            },
            timeout=120,
        )
        response.raise_for_status()
        found.update(
            r["inat_obs_id"]["value"]
            for r in response.json()["results"]["bindings"]
        )
    return found


def tally(inat_ids, rows):
    """Count species with/without Wikidata items and sitelinks.

    Returns (counts, missing), where missing maps each "without" bucket to the
    set of iNaturalist taxon IDs in it, so the page can list them.
    """
    with_item = {int(r["inat"]["value"]) for r in rows}
    missing = {"without_wikidata": inat_ids - with_item}
    counts = {
        "species": len(inat_ids),
        "with_wikidata": len(with_item),
        "without_wikidata": len(missing["without_wikidata"]),
    }
    for key in ("en", "pt", "commons", "image"):
        linked = {int(r["inat"]["value"]) for r in rows if key in r}
        missing[key + "_missing"] = with_item - linked
        counts[key] = len(linked)
        counts[key + "_missing"] = len(missing[key + "_missing"])
    return counts, missing


QS_QUERY = """
SELECT ?name ?item ?inat WHERE {
  VALUES ?name { %s }
  ?item wdt:P225 ?name .
  OPTIONAL { ?item wdt:P3151 ?inat }
}
"""


def quickstatements_for(taxa):
    """QuickStatements v1 lines adding P3151 to unambiguous taxon-name matches.

    `taxa` maps iNaturalist taxon ID -> scientific name. A pair is used only when
    the name matches exactly one Wikidata item, that item has no P3151 yet, and
    no two iNaturalist taxa here share the name.
    """
    names = sorted({n for n in taxa.values() if n})
    items = defaultdict(set)
    has_inat = set()
    for start in range(0, len(names), 200):
        values = " ".join(
            '"%s"' % n.replace("\\", "").replace('"', "")
            for n in names[start : start + 200]
        )
        response = requests.get(
            WDQS_URL,
            params={"query": QS_QUERY % values, "format": "json"},
            headers={"User-Agent": "wikiconcurso-inaturalist/1.0 (stats page)"},
            timeout=120,
        )
        response.raise_for_status()
        for row in response.json()["results"]["bindings"]:
            qid = row["item"]["value"].rsplit("/", 1)[-1]
            items[row["name"]["value"]].add(qid)
            if "inat" in row:
                has_inat.add(qid)

    name_counts = Counter(taxa.values())
    lines = []
    for taxon_id, name in sorted(taxa.items()):
        qids = items.get(name, set())
        if name_counts[name] > 1 or len(qids) != 1:
            continue
        qid = qids.pop()
        if qid not in has_inat:
            lines.append(f'{qid}|P3151|"{taxon_id}"')
    return lines


@app.route("/stats")
def stats():
    observations = fetch_project_observations(PROJECT_SLUG)
    open_obs = [
        obs
        for obs in observations
        if (first_photo(obs) or {}).get("license_code") in OPEN_LICENSES
    ]

    inat_ids = {
        (obs.get("taxon") or {}).get("id")
        for obs in open_obs
        if (obs.get("taxon") or {}).get("rank") == "species"
    }
    inat_ids.discard(None)

    counts, missing = tally(inat_ids, fetch_wikidata_rows(inat_ids))
    counts["observations"] = len(observations)
    counts["open_observations"] = len(open_obs)
    counts["users"] = len({(obs.get("user") or {}).get("login") for obs in observations})
    counts["open_users"] = len({(obs.get("user") or {}).get("login") for obs in open_obs})

    on_commons = fetch_observations_on_commons(
        {str(obs.get("id")) for obs in open_obs}
    )
    not_on_commons = [
        obs for obs in open_obs if str(obs.get("id")) not in on_commons
    ]
    counts["on_commons"] = len(open_obs) - len(not_on_commons)
    counts["not_on_commons"] = len(not_on_commons)

    # one representative observation per species, to illustrate each "sem" bucket
    example = {}
    for obs in open_obs:
        taxon_id = (obs.get("taxon") or {}).get("id")
        if taxon_id in inat_ids and taxon_id not in example:
            example[taxon_id] = make_record(obs, first_photo(obs))
    missing_cards = {
        bucket: sorted(
            (example[i] for i in ids if i in example), key=lambda r: r["species"]
        )
        for bucket, ids in missing.items()
    }
    missing_cards["not_on_commons"] = [
        make_record(obs, first_photo(obs)) for obs in not_on_commons
    ]

    # P3151 statements for the species that have no Wikidata item by iNat ID
    qs_lines = quickstatements_for(
        {
            i: example[i]["species"]
            for i in missing["without_wikidata"]
            if i in example
        }
    )

    return render_template(
        "stats.html",
        counts=counts,
        missing_cards=missing_cards,
        qs_lines=qs_lines,
        qs_url="https://qs-dev.toolforge.org/batch/new?v1=" + quote("||".join(qs_lines)),
        year=YEAR,
        today=datetime.now(),
        datetime=datetime,
    )


def build_user_photos():
    """Fetch the project and return the per-user validated/unvalidated buckets."""
    observations = fetch_project_observations(PROJECT_SLUG)

    if not observations:
        abort(500, description="Failed to fetch data from the API")

    valid_photos = {"vertebrates": [], "arthropods": [], "others": []}
    unvalidated_photos = {}

    for obs in observations:
        photo = first_photo(obs)
        if photo is None:
            continue  # nothing to show

        validation_categories = get_validation_categories(obs)

        record = make_record(obs, photo, validation_categories)

        if "validated" in validation_categories:
            valid_photos[categorize_photo(obs)].append(record)
        else:
            for cat in validation_categories:
                unvalidated_photos.setdefault(cat, []).append(record)

    user_photos = organize_photos_by_user(valid_photos, unvalidated_photos)

    # enforce max-three validated per user / taxon-category
    for tcat in ("vertebrates", "arthropods", "others"):
        for user, buckets in user_photos.items():
            validated = buckets["validated"][tcat]
            if len(validated) > 3:
                buckets["unvalidated"]["more-than-three"].extend(validated[3:])
                buckets["validated"][tcat] = validated[:3]

    return user_photos


@app.route("/")
def index():
    return render_template(
        "index_2026.html", user_photos=build_user_photos(), datetime=datetime
    )


GALLERY_TITLES = {
    "vertebrates": "Vertebrados",
    "arthropods": "Artrópodes",
    "others": "Outros",
}


@app.route("/vertebrates")
@app.route("/arthropods")
@app.route("/other")
def gallery():
    tcat = {"/other": "others"}.get(request.path, request.path.lstrip("/"))
    photos = [
        photo
        for buckets in build_user_photos().values()
        for photo in buckets["validated"][tcat]
    ]
    photos.sort(key=lambda p: p["date"] or "")
    return render_template(
        "gallery.html",
        title=GALLERY_TITLES[tcat],
        photos=photos,
        year=YEAR,
        datetime=datetime,
    )


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
