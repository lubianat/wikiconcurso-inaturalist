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
from collections import defaultdict
import os
import hashlib
from tqdm import tqdm

app = Flask(__name__)

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------
VALID_START_DATE = datetime.strptime("2024-09-01", "%Y-%m-%d")
VALID_END_DATE = datetime.strptime("2025-07-31", "%Y-%m-%d")

VERTEBRATES = ["Mammalia", "Aves", "Reptilia", "Amphibia", "Actinopterygii"]
ARTHROPODS = ["Insecta", "Arachnida", "Crustacea", "Myriapoda"]

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

    if photo_obj.get("license_code") not in {"cc-by", "cc-by-sa", "cc0"}:
        categories.append("non-compatible-license")

    if observation.get("quality_grade") != "research":
        categories.append("non-research-grade")
        # “needs_id” fast-track
        taxon_rank = (observation.get("taxon") or {}).get("rank", "")
        if (
            observation.get("quality_grade") == "needs_id"
            and is_valid_date(observation.get("observed_on"))
            and photo_obj.get("license_code") in {"cc-by", "cc-by-sa", "cc0"}
            and observation.get("num_identification_agreements", 0) >= 2
            and taxon_rank == "genus"
        ):
            categories.append("validated")

    if not categories:
        categories.append("validated")

    return categories


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


@app.route("/")
def index():
    project_slug = "wikiconcurso-fotografico-inaturalist-2025"
    observations = fetch_project_observations(project_slug)

    if not observations:
        abort(500, description="Failed to fetch data from the API")

    valid_photos = {"vertebrates": [], "arthropods": [], "others": []}
    unvalidated_photos = {}

    for obs in tqdm(observations):
        photo = first_photo(obs)
        if photo is None:
            continue  # nothing to show

        validation_categories = get_validation_categories(obs)

        record = {
            "observation_id": obs.get("id") or "",
            "photo": photo,
            "author": (obs.get("user") or {}).get("login", "Unknown"),
            "date": obs.get("observed_on", "Unknown"),
            "license": photo.get("license_code", ""),
            "species": (obs.get("taxon") or {}).get("name", "Unknown"),
            "validation_categories": validation_categories,
        }

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

    return render_template(
        "index_2025.html", user_photos=user_photos, datetime=datetime
    )


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
