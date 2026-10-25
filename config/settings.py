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
        # Reuse a connection across requests: opening one costs SQLite file work on every
        # request (50-300 ms on Windows). It only helps a server whose threads live on, such
        # as `runserver --nothreading`; plain runserver starts a thread per request.
        "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "600")),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            # WAL lets reads run during a write, and synchronous=NORMAL drops the fsync on
            # every commit (about 100 ms on Windows). A power cut can lose the last writes
            # but never corrupts the file: fine for cached routes and saved plans.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL",
            # Take the write lock at BEGIN, so concurrent writers wait instead of failing.
            "transaction_mode": "IMMEDIATE",
        },
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
    # Every error leaves as {"error": {"code", "message", "details"}}.
    "EXCEPTION_HANDLER": "planner.api.errors.exception_handler",
    # Per client IP; a client over the limit gets 429 with Retry-After.
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.environ.get("API_THROTTLE_RATE", "60/min")},
}

# Geocoding answers, circuit-breaker state and both throttles live here. The in-process
# cache needs no setup; with several worker processes, point it at Redis instead.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    # Every outbound call is logged here with the service, HTTP status and duration.
    "loggers": {"planner": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")}},
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

# --- Routing ------------------------------------------------------------------------
# Tried in this order. A provider that fails hands over to the next; nothing is retried.
ROUTING_PROVIDERS = env_list("ROUTING_PROVIDERS", "osrm,ors")
# (connect, read) seconds for every routing call, so a stuck provider cannot hang a request.
ROUTING_TIMEOUT_SECONDS = (
    float(os.environ.get("ROUTING_CONNECT_TIMEOUT_SECONDS", "3")),
    float(os.environ.get("ROUTING_READ_TIMEOUT_SECONDS", "15")),
)
# After this many failures in a row a provider is skipped for BREAKER_RESET_SECONDS.
BREAKER_FAILURE_THRESHOLD = int(os.environ.get("BREAKER_FAILURE_THRESHOLD", "3"))
BREAKER_RESET_SECONDS = int(os.environ.get("BREAKER_RESET_SECONDS", "60"))

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
# The public server's policy: at most one request per second.
OSRM_MIN_INTERVAL_SECONDS = float(os.environ.get("OSRM_MIN_INTERVAL_SECONDS", "1.0"))

# openrouteservice is the fallback, and only runs when a (free) key is set.
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
ORS_BASE_URL = os.environ.get("ORS_BASE_URL", "https://api.openrouteservice.org").rstrip("/")
# "driving-hgv" routes as a heavy goods vehicle instead of a car.
ORS_PROFILE = os.environ.get("ORS_PROFILE", "driving-car")
