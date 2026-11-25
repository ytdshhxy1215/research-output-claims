"""Use PostgreSQL integration settings without requiring collectstatic for pytest."""

from config.settings import *  # noqa: F403
from config.settings import MIDDLEWARE as BASE_MIDDLEWARE
from config.settings import STORAGES as BASE_STORAGES

MIDDLEWARE = [item for item in BASE_MIDDLEWARE if not item.startswith("whitenoise.")]
STORAGES = {
    **BASE_STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
