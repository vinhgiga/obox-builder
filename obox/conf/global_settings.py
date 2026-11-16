"""Defaults for the supported settings subset."""

from typing import Any

DEBUG = False
SECRET_KEY = ""
INSTALLED_APPS: list[str] = []
DATABASES: dict[str, Any] = {}
TEMPLATES: list[dict[str, Any]] = []
ROOT_URLCONF = None
STATIC_URL = "/static/"
STATIC_ROOT = None
MEDIA_URL = "/media/"
MEDIA_ROOT = ""
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Chicago"
USE_TZ = True
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/accounts/profile/"
AUTH_USER_MODEL = "auth.User"
AUTHENTICATION_BACKENDS = ["obox.contrib.auth.backends.ModelBackend"]
AUTH_PASSWORD_VALIDATORS: list[dict[str, Any]] = []
REST_FRAMEWORK: dict[str, Any] = {}

CSRF_COOKIE_NAME = "csrftoken"
CSRF_COOKIE_AGE = 31449600
CSRF_COOKIE_PATH = "/"
CSRF_COOKIE_DOMAIN = None
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = False
CSRF_TRUSTED_ORIGINS: list[str] = []
