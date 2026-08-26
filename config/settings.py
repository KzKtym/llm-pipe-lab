"""
Django settings for llm_pipe_lab project.
Development environment configuration.

このプロジェクトは別プロジェクトの実験基盤を独立させたもの。
とりあえず runserver で動くことを目的とした最小構成。
認証連携（署名付きチケット / 共有デモユーザー・読み取り専用）は docs/AD_Auth_Federation.md
の設計に基づき別途実装する。現状はログイン必須化(LoginRequiredMiddleware)を外し、
/nl2sql/ を直接開ける状態にしている。
"""
from pathlib import Path

from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY')

DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=lambda v: [s.strip() for s in v.split(',')])

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'whitenoise.runserver_nostatic',
    'django.contrib.staticfiles',

    # Local apps
    'app.nl2sql',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='llm_pipe_lab'),
        'USER': config('DB_USER', default='admin'),
        'PASSWORD': config('DB_PASSWORD', default='admin'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}

# テストDB名を環境変数で上書きできる口。本体(c)・画面(v)・環境(e)の3系統を別セッションで
# 並行実行すると、Django 既定のテストDB名 `test_<DB名>` が衝突し、後発が先行中のテストDBを
# 削除しにいく。系統ごとに別名を渡して衝突を避ける（例: TEST_DB_NAME=test_llm_pipe_lab_e）。
# default=None なら Django 既定のままなので、指定しない限り現状の挙動は変わらない。
DATABASES['default']['TEST'] = {'NAME': config('TEST_DB_NAME', default=None)}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'ja'
TIME_ZONE = 'Asia/Tokyo'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'

SESSION_ENGINE = 'django.contrib.sessions.backends.db'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
        },
        'app.nl2sql': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# ---------------------------------------------------------------------------
# テスト実行時のみ、実験明細の保存先をテンポラリへ逃がす（実データ保護の網）。
#   nl2sql の store.get_logs_dir() は settings.NL2SQL_LOGS_DIR があればそちらへ書く。
#   これが無いと、テストDBで採番された id で実ファイル data/nl2sql/logs/exp_*.json を
#   書き潰す（issue_081415 で実際に明細を消失）。個々のテストも override_settings で
#   逃がしているが、書き忘れても実データに届かないよう設定側にも網を張る。
#   canonical なテスト実行は素の config.settings（--settings 差し替え無し）なので、ここに置く。
import sys as _sys  # noqa: E402

if 'test' in _sys.argv:
    import tempfile as _tempfile  # noqa: E402

    NL2SQL_LOGS_DIR = _tempfile.mkdtemp(prefix='nl2sql-test-logs-')
    # 段階2（stage2_store.get_logs_dir()）も同じ理由で別途逃がす。
    # 段階1と同じキーにすると、片方の上書き漏れがもう片方の事故に気付けなくなる。
    NL2SQL_STAGE2_LOGS_DIR = _tempfile.mkdtemp(prefix='nl2sql-stage2-test-logs-')
    # 段階2ルール判定版（stage2_rule_store.get_logs_dir()）も同じ理由で別キー。
    NL2SQL_STAGE2_RULE_LOGS_DIR = _tempfile.mkdtemp(prefix='nl2sql-stage2-rule-test-logs-')
