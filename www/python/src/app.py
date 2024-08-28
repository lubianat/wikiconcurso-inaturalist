from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    abort,
    Response,
)
from flask_sqlalchemy import SQLAlchemy
import requests
from datetime import datetime
from collections import defaultdict
import os
import json
import hashlib
import csv
from io import StringIO


app = Flask(__name__)

# Constants for date range and license codes
VALID_START_DATE = datetime.strptime("2023-09-01", "%Y-%m-%d")
VALID_END_DATE = datetime.strptime("2024-08-31", "%Y-%m-%d")

VERTEBRATES = ["Mammalia", "Aves", "Reptilia", "Amphibia", "Actinopterygii"]
ARTHROPODS = ["Insecta", "Arachnida", "Crustacea", "Myriapoda"]

app.config["SECRET_KEY"] = "your_secret_key"  # yeah, I know, it's not a secret
app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"sqlite:///{os.path.join(os.getcwd(), 'evaluations.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class Evaluation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    inat_username = db.Column(db.String(80), nullable=False)
    observation_id = db.Column(db.Integer, nullable=False)
    wikipedia_score = db.Column(db.Integer, nullable=False)
    science_score = db.Column(db.Integer, nullable=False)
    photographic_score = db.Column(db.Integer, nullable=False)
    total_score = db.Column(db.Integer, nullable=False)

    def __init__(
        self,
        inat_username,
        observation_id,
        wikipedia_score,
        science_score,
        photographic_score,
    ):
        self.inat_username = inat_username
        self.observation_id = observation_id
        self.wikipedia_score = wikipedia_score
        self.science_score = science_score
        self.photographic_score = photographic_score
        self.total_score = wikipedia_score + science_score + photographic_score


def validate_observation(observation):
    """Check if an observation is valid based on date, license, and research grade."""
    validation_categories = get_validation_categories(observation)
    return "validated" in validation_categories


@app.route("/download_evaluations", methods=["GET"])
def download_evaluations():
    if "username" not in session:
        return redirect(url_for("login"))

    # Query all evaluations
    evaluations = Evaluation.query.all()

    # Create a string buffer to hold the TSV data
    output = StringIO()
    writer = csv.writer(output, delimiter="\t")

    # Write the header row, including the observation link
    writer.writerow(
        [
            "id",
            "inat_username",
            "observation_id",
            "observation_link",
            "wikipedia_score",
            "science_score",
            "photographic_score",
            "total_score",
        ]
    )

    # Write data rows
    for evaluation in evaluations:
        observation_link = (
            f"https://www.inaturalist.org/observations/{evaluation.observation_id}"
        )
        writer.writerow(
            [
                evaluation.id,
                evaluation.inat_username,
                evaluation.observation_id,
                observation_link,
                evaluation.wikipedia_score,
                evaluation.science_score,
                evaluation.photographic_score,
                evaluation.total_score,
            ]
        )

    # Create a response with the TSV data
    output.seek(0)
    return Response(
        output,
        mimetype="text/tab-separated-values",
        headers={"Content-Disposition": "attachment;filename=evaluations.tsv"},
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        # Hash the input password
        hashed_password = hashlib.sha256(password.encode()).hexdigest()

        # The stored hash of the correct password
        correct_hashed_password = (
            "4727fbdab27502adbf3a2ade065b30e2095cacb12d58711b596a3755756f8323"
        )

        if hashed_password == correct_hashed_password:
            session["username"] = username
            return redirect(url_for("evaluate"))
        else:
            return "Invalid password"
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("username", None)
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    return redirect(url_for("login"))


CACHE_FILE = "validated_observations.json"


@app.route("/evaluate", methods=["GET", "POST"])
def evaluate():
    if "username" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        observation_id = request.form["observation_id"]
        wikipedia_score = int(request.form["wikipedia_score"])
        science_score = int(request.form["science_score"])
        photographic_score = int(request.form["photographic_score"])

        total_score = wikipedia_score + science_score + photographic_score

        evaluation = Evaluation.query.filter_by(
            inat_username=session["username"], observation_id=observation_id
        ).first()

        if evaluation:
            evaluation.wikipedia_score = wikipedia_score
            evaluation.science_score = science_score
            evaluation.photographic_score = photographic_score
        else:
            evaluation = Evaluation(
                inat_username=session["username"],
                observation_id=observation_id,
                wikipedia_score=wikipedia_score,
                science_score=science_score,
                photographic_score=photographic_score,
            )
            db.session.add(evaluation)

        db.session.commit()

        index = int(request.args.get("index", 0))

        with open(CACHE_FILE, "r") as f:
            validated_observations = json.load(f)

        total_observations = len(validated_observations)

        next_index = index + 1
        return redirect(url_for("evaluate", index=next_index))

    if not os.path.exists(CACHE_FILE):
        observations = fetch_project_observations(
            "wikiconcurso-fotografico-inaturalist-2024"
        )

        validated_observations = [
            {
                "observation_id": obs.get("id", ""),
                "photo": obs.get("photos", [])[0],
                "author": obs.get("user", {}).get("login", "Unknown"),
                "date": obs.get("observed_on", "Unknown"),
                "license": obs.get("photos", [])[0].get("license_code", ""),
                "species": obs.get("taxon", {}).get("name", "Unknown"),
                "taxon_id": obs.get("taxon", {}).get("id", None),
            }
            for obs in observations
            if validate_observation(obs) and obs.get("photos", [])
        ]

        with open(CACHE_FILE, "w") as f:
            json.dump(validated_observations, f)

    with open(CACHE_FILE, "r") as f:
        validated_observations = json.load(f)

    total_observations = len(validated_observations)

    index = int(request.args.get("index", 0))
    prev_index = index - 1 if index > 0 else -1
    next_index = index + 1 if index < total_observations - 1 else total_observations

    current_observation = None
    previous_evaluation = None
    evaluations_info = []

    for i, obs in enumerate(validated_observations):
        evaluations_count = Evaluation.query.filter_by(
            observation_id=obs["observation_id"]
        ).count()
        user_evaluated = (
            Evaluation.query.filter_by(
                inat_username=session["username"], observation_id=obs["observation_id"]
            ).first()
            is not None
        )

        evaluations_info.append(
            {
                "index": i,
                "evaluations_count": evaluations_count,
                "user_evaluated": user_evaluated,
            }
        )

    if 0 <= index < total_observations:
        current_observation = validated_observations[index]
        previous_evaluation = Evaluation.query.filter_by(
            inat_username=session["username"],
            observation_id=current_observation["observation_id"],
        ).first()

    return render_template(
        "evaluate.html",
        current_observation=current_observation,
        prev_index=prev_index,
        next_index=next_index,
        total_observations=total_observations,
        previous_evaluation=previous_evaluation,
        datetime=datetime,
        evaluations_info=evaluations_info,  # Pass evaluations info to the template
    )


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
    except (ValueError, TypeError) as e:
        if isinstance(e, TypeError):
            app.logger.error(f"Invalid date format: {observed_date}")
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
    # Ensure the database is created from scratch
    with app.app_context():
        db.create_all()  # Create all tables based on the models

    app.run(debug=True)
