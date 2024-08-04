from flask import Flask, render_template, abort
import requests
from datetime import datetime
from collections import defaultdict

app = Flask(__name__)

# Constants for date range and license codes
VALID_START_DATE = datetime.strptime("2023-09-01", "%Y-%m-%d")
VALID_END_DATE = datetime.strptime("2024-08-31", "%Y-%m-%d")

VERTEBRATES = ["Mammalia", "Aves", "Reptilia", "Amphibia", "Actinopterygii"]
ARTHROPODS = ["Insecta", "Arachnida", "Crustacea", "Myriapoda"]


def fetch_project_observations(project_slug):
    per_page = 200
    page = 1
    all_results = []

    while True:
        url = f"https://api.inaturalist.org/v1/observations?project_id={project_slug}&per_page={per_page}&page={page}&order_by=observed_on"
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


def is_valid_date(observed_date):
    try:
        date = datetime.strptime(observed_date, "%Y-%m-%d")
        return VALID_START_DATE <= date <= VALID_END_DATE
    except ValueError:
        return False


def categorize_photo(observation):
    iconic_taxon = observation.get("taxon", {}).get("iconic_taxon_name", "")
    if iconic_taxon in VERTEBRATES:
        return "vertebrates"
    elif iconic_taxon in ARTHROPODS:
        return "arthropods"
    else:
        return "others"


def get_validation_categories(observation):
    categories = []
    if not is_valid_date(observation.get("observed_on", "")):
        categories.append("date-before-september")
    if observation.get("photos")[0].get("license_code", "") not in [
        "cc-by",
        "cc-by-sa",
        "cc0",
    ]:
        categories.append("non-compatible-license")
    if observation.get("quality_grade") != "research":
        categories.append("non-research-grade")
    if not categories:
        categories.append("validated")
    return categories


def organize_photos_by_user(valid_photos, unvalidated_photos):
    user_photos = defaultdict(
        lambda: {"validated": defaultdict(list), "unvalidated": defaultdict(list)}
    )

    for category, photos in valid_photos.items():
        for photo in photos:
            user = photo["author"]
            user_photos[user]["validated"][category].append(photo)

    for category, photos in unvalidated_photos.items():
        for photo in photos:
            user = photo["author"]
            user_photos[user]["unvalidated"][category].append(photo)

    # Sorting users alphabetically
    sorted_user_photos = dict(sorted(user_photos.items()))

    return sorted_user_photos


@app.route("/")
def index():
    project_slug = "wikiconcurso-fotografico-inaturalist-2024"
    observations = fetch_project_observations(project_slug)

    if not observations:
        abort(500, description="Failed to fetch data from the API")

    valid_photos = {"vertebrates": [], "arthropods": [], "others": []}
    unvalidated_photos = {}
    for observation in observations:
        validation_categories = get_validation_categories(observation)

        photos = observation.get("photos", [])
        if not photos:
            continue

        photo = photos[0]
        observation_data = {
            "observation_id": observation.get("id", ""),
            "photo": photo,
            "author": observation.get("user", {}).get("login", "Unknown"),
            "date": observation.get("observed_on", "Unknown"),
            "license": photo.get("license_code", ""),
            "species": observation.get("taxon", {}).get("name", "Unknown"),
        }

        observation_data["validation_categories"] = validation_categories

        if "validated" in validation_categories:
            category = categorize_photo(observation)
            valid_photos[category].append(observation_data)
        else:
            for validation_category in validation_categories:
                unvalidated_photos.setdefault(validation_category, []).append(
                    observation_data
                )

    user_photos = organize_photos_by_user(valid_photos, unvalidated_photos)

    for taxon_category in ["vertebrates", "arthropods", "others"]:
        # Check if number of photos for each author is > 3
        for user, photos in user_photos.items():
            if len(photos["validated"][taxon_category]) > 3:
                user_photos[user]["unvalidated"]["more-than-three"].extend(
                    photos["validated"][taxon_category][3:]
                )
                user_photos[user]["validated"][taxon_category] = photos["validated"][
                    taxon_category
                ][:3]

    return render_template(
        "index.html",
        user_photos=user_photos,
        datetime=datetime,  # Pass datetime to the template
    )


if __name__ == "__main__":
    app.run(debug=True)
