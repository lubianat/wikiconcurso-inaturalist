# AGENTS.md

Review site for the Wikiconcurso Fotográfico iNaturalist (Rede Brasileira de Naturalistas). A small Flask app pulls observations from an iNaturalist project via the public API and lists them per contributor, flagged against the contest rules.

## Running

```bash
source .venv/bin/activate        # venv already exists at repo root
cd www/python/src
pip install -r requirements.txt
python app.py                    # Flask dev server, debug=True
```

No tests, linter, or build step.

## Layout

- `www/python/src/app.py` — current year's app; the only one to edit.
- `app_2024.py`, `app_2025.py` — frozen snapshots of past editions. Leave them alone.
- `templates/index_<year>.html` — one per edition, plus shared `card_macro.html`. `login.html`/`evaluate.html` belong to the 2024 manual-evaluation flow only.
- `validated_observations.json`, `evaluations.db` — runtime artifacts, not source data.
- `images/` — banners and flyers (SVG sources kept alongside exports).

## How it works

`fetch_project_observations()` paginates the iNaturalist API → `get_validation_categories()` flags each observation (has photo, date inside the window, license in cc-by/cc-by-sa/cc0, research grade, plus a needs_id + genus-rank fast-track) → `categorize_photo()` buckets it as vertebrates/arthropods/others by ancestor taxon ID → `organize_photos_by_user()` groups by contributor → the `/` route caps each user at 3 validated photos per bucket, overflow goes to `more-than-three`.

## Per-edition changes

Each year is a copy-fork, not a config change. A new edition means bumping, in `app.py`: `VALID_START_DATE` / `VALID_END_DATE`, the `project_slug`, and the rendered template — and adding the matching `index_<year>.html`. Keep it that way; there's no shared config layer and it doesn't need one.
