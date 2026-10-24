"""Django settings for the Spotter fuel planner.

Deploy-specific values come from the environment, or from a local `.env` file (see
`.env.example`), so the same code runs unchanged on a laptop and on a server.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Real environment variables win over .env (load_dotenv does not override by default).
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# The fallback key only exists so a fresh clone runs; `manage.py check --deploy` flags it.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Before anything that touches the body, so the final response is compressed.
    # A route's GeoJSON shrinks several times over.
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# SQLite keeps the reviewer's setup at zero; nothing here needs a server database.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"


REST_FRAMEWORK = {
    # A plain JSON API: no browsable HTML pages, and no auth (out of scope for the brief).
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}


# --- Outbound HTTP ------------------------------------------------------------------
# The public OSM services (Nominatim, OSRM) ask every client to identify itself.
HTTP_USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT",
    "spotter-fuel-planner/1.0 (+https://github.com/MBouallegue/spotter-fuel-planner)",
)
NOMINATIM_BASE_URL = os.environ.get(
    "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
NOMINATIM_TIMEOUT_SECONDS = float(os.environ.get("NOMINATIM_TIMEOUT_SECONDS", "5"))
