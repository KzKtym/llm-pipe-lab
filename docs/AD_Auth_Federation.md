# 設計書: ポートフォリオ(A) → ツール(B) 署名付きチケット認証連携

## 1. 目的とスコープ

A で認証済みのユーザーが A 上のリンクから B へ遷移したとき、B で再ログインを求めずに
B のセッションを確立する。

対象は A→B の片方向の遷移のみ。B→A の連携、ログアウトの伝播、B 単体でのユーザー管理は
本設計の対象外とする。

## 2. 構成

| 項目 | A | B |
|------|---|---|
| ホスト | portfolio.example.com | toolb.example.com |
| プロセス | gunicorn (既存) | gunicorn (新規・別プロセス) |
| DB | A 専用 | B 専用 |
| リポジトリ | 既存 | 新規・独立（clone して単体で動作） |

同一 VPS 上で nginx が server_name により振り分ける。両ホストとも HTTPS 必須。
本方針では A と B を**別サブドメイン**に置く（Cookie の分離が確実で事故が起きにくい）。

## 3. 信頼モデル

**共有するもの: 共有秘密鍵 `SSO_TICKET_KEY` 1 個のみ。**

以下は共有しない。

- SECRET_KEY（A と B で別値）
- データベース
- セッションストア
- ユーザーテーブル
- アプリケーションコード

`SSO_TICKET_KEY` が未設定の場合、B の連携エンドポイントは無効化され、B は通常の
Django 認証アプリとして単体で動作する。これにより公開リポジトリとしての独立性を保つ。

## 4. ユーザーの扱い（共有デモユーザー・読み取り専用）

B 側では **共有デモユーザー 1 名**に固定でマップする。A のユーザーごとの払い出しは行わない。

帰結として、チケットには A のユーザー識別情報を含めない。A の個人情報は B へ一切渡らない。
B のユーザーテーブルは増加しない。

### 4.1 権限方針: 書き込み禁止

公開デモサイトでは、**共有デモユーザーに書き込み系操作を一切許可しない。**
デモの目的は次の 2 点に限定する。

1. あらかじめローカルで実行した結果（選別してアップした記録）の**参照**
2. 画面の**操作感**の確認

したがって B は「閲覧専用アプリ」として振る舞う。新規実験の実行、プロジェクトの作成・編集・
削除、スター付与、ログ生成など、状態を変える操作はデモユーザーに対して拒否する。

### 4.2 読み取り専用の担保（多層）

一箇所の実装ミスが書き込みを通してしまわないよう、独立した層で二重・三重に止める。

- **アプリ層:** デモユーザー(=SSO 経由ログイン)には書き込み系ビューを 403 で拒否する。
  書き込みビューへ `@require_readwrite`（自作デコレータ。デモユーザーなら 403）を付ける、
  もしくは書き込みビューを一括で塞ぐミドルウェアを置く。POST/PUT/PATCH/DELETE を
  横断的に拒否する方式が最も漏れにくい（GET と冪等な HEAD/OPTIONS のみ許可）。
- **DB 層:** B の接続ロールを **読み取り専用ロール**にする運用を推奨（`GRANT SELECT` のみ、
  `INSERT/UPDATE/DELETE` を与えない）。アプリ層をすり抜けても DB が最終的に拒否する。
  参照用データの投入はマイグレーション/管理コマンド（別ロール）で行う。
- **前段 (nginx):** 公開サイトの B ロケーションで、GET/HEAD 以外の method を 405 で
  返す設定を併用してもよい（補強。単独では頼らない）。

デモユーザーは Django の `is_staff=False` / `is_superuser=False` とし、admin へも入れない。

## 5. チケット仕様

### 生成

`django.core.signing.dumps()` を使用する。HMAC-SHA256 署名 + URL-safe base64。

```python
payload = {
    "v":   1,                 # 仕様バージョン
    "iss": "portfolio",       # 発行元
    "aud": "toolb",           # 宛先（B 側で検証）
    "jti": uuid.uuid4().hex,  # 一意 ID（再利用検知用）
}
ticket = signing.dumps(payload, key=SSO_TICKET_KEY, salt="portfolio.sso.v1")
```

### 重要な性質

- **署名であって暗号化ではない。** payload はデコードすれば誰でも読める。
  上記のとおり秘密情報を含めないため問題にならないが、後から情報を足す際はこの前提を守ること。
- **有効期間 60 秒。** 検証側で `max_age=60` を指定する。
- **salt にバージョンを含める。** 仕様変更や鍵ローテーション時に `v2` へ切り替える。

## 6. シーケンス

```
[A] ユーザーが A 上のリンク "/go/toolb/" をクリック
     │
     ├─ 未ログイン → A のログイン画面へ（login_required）
     │
     └─ ログイン済み
          │  クリック時点でチケットを生成（※事前に埋め込まない）
          ▼
      302 → https://toolb.example.com/sso/consume/?t=<ticket>
          │
          ▼
[B] チケット検証
     ├─ 鍵未設定            → 404
     ├─ 署名不正 / 期限切れ / aud 不一致 / jti 再利用 → B のログイン画面へ 302
     └─ 検証 OK
          │  共有デモユーザー(読み取り専用)で login()
          ▼
      302 → https://toolb.example.com/   （URL からチケットが消える）
```

**チケットはクリック時に生成する。** ページ描画時に URL へ埋め込むと、その画面を開いたまま
放置された場合に 60 秒の TTL が破綻する。そのため A 側にリダイレクト専用ビューを置く。

## 7. A 側の設計

### 設定 (.env → settings)

```python
SSO_TICKET_KEY = env("SSO_TICKET_KEY", default="")
TOOLB_URL      = env("TOOLB_URL", default="https://toolb.example.com")
```

`SESSION_COOKIE_DOMAIN` は **設定しない**（ホスト限定のままにする）。
`.example.com` を指定すると A のセッション Cookie が B にも送信され、
分離の前提が崩れる。

### ビュー

```python
# app/portfolio/views.py
import uuid
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import Http404
from django.shortcuts import redirect


@login_required
def go_toolb(request):
    if not settings.SSO_TICKET_KEY:
        raise Http404
    ticket = signing.dumps(
        {"v": 1, "iss": "portfolio", "aud": "toolb", "jti": uuid.uuid4().hex},
        key=settings.SSO_TICKET_KEY,
        salt="portfolio.sso.v1",
    )
    return redirect(f"{settings.TOOLB_URL}/sso/consume/?t={ticket}")
```

### URL とテンプレート

```python
path("go/toolb/", views.go_toolb, name="go_toolb"),
```

テンプレート側のリンクは B の URL を直書きせず `{% url 'go_toolb' %}` を使う。

## 8. B 側の設計

### 設定

```python
SSO_TICKET_KEY    = env("SSO_TICKET_KEY", default="")   # 空なら連携機能を無効化
SSO_TICKET_MAXAGE = 60
SSO_DEMO_USERNAME = "demo"

SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "sso_ticket_cache",
    }
}
```

**キャッシュバックエンドの選定理由。** `LocMemCache` は gunicorn のワーカープロセスごとに
独立するため、ワーカーが複数ある環境では jti の再利用検知が漏れる。
`DatabaseCache`（`manage.py createcachetable`）か Redis を使うこと。
B 専用 DB 内で完結するため、A との結合にはならない。

### 連携ビュー

```python
# app/sso/views.py
import logging

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core import signing
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import redirect

logger = logging.getLogger(__name__)
SALT = "portfolio.sso.v1"


def sso_consume(request):
    if not settings.SSO_TICKET_KEY:
        raise Http404

    token = request.GET.get("t", "")
    try:
        data = signing.loads(
            token,
            key=settings.SSO_TICKET_KEY,
            salt=SALT,
            max_age=settings.SSO_TICKET_MAXAGE,
        )
    except signing.SignatureExpired:
        logger.warning("sso: ticket expired")
        return redirect("/accounts/login/?e=expired")
    except signing.BadSignature:
        logger.warning("sso: bad signature")
        return redirect("/accounts/login/")

    if data.get("aud") != "toolb" or data.get("v") != 1:
        logger.warning("sso: audience/version mismatch")
        return redirect("/accounts/login/")

    jti = data.get("jti", "")
    if not jti or not cache.add(f"sso:jti:{jti}", 1,
                                timeout=settings.SSO_TICKET_MAXAGE + 60):
        logger.warning("sso: ticket replay rejected")
        return redirect("/accounts/login/")

    user = _get_demo_user()
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("/")


def _get_demo_user():
    """共有デモユーザー。読み取り専用・通常ログイン不可。"""
    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=settings.SSO_DEMO_USERNAME,
        defaults={"is_active": True, "is_staff": False, "is_superuser": False},
    )
    if created:
        user.set_unusable_password()   # 通常ログイン経路からは入れない
        user.save(update_fields=["password"])
    return user
```

**実装上の注意.** `authenticate()` を経由しないため、`login()` には `backend` を
明示的に渡す必要がある。省略すると `ValueError` になる。

### 書き込み禁止の実装（4.2 のアプリ層）

デモユーザーの書き込みを止める最も漏れにくい方法は、method ベースの横断ミドルウェア。

```python
# app/sso/middleware.py
from django.http import HttpResponseForbidden

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class DemoReadOnlyMiddleware:
    """SSO 経由の共有デモユーザーには冪等でない method を許さない。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (user and user.is_authenticated
                and user.get_username() == settings.SSO_DEMO_USERNAME
                and request.method not in SAFE_METHODS):
            return HttpResponseForbidden("read-only demo")
        return self.get_response(request)
```

`AuthenticationMiddleware` より後ろに置く。これで新規実験・編集・削除・スター等の
POST 系がデモユーザーに対して一律 403 になる。個別ビューでの付け漏れを構造的に防ぐ。

### 遷移先を固定する

consume 後の遷移先は `/` 固定とし、`next` パラメータは受け付けない。
受け付けると B が任意サイトへのオープンリダイレクタになる。
将来的に遷移先を可変にする場合は、B 内部のパスのみを許可する allowlist を設ける。

## 9. エラー時の挙動

| 状況 | 応答 | ログ |
|------|------|------|
| `SSO_TICKET_KEY` 未設定 | 404 | なし |
| `t` なし / 空 | ログイン画面へ 302 | WARNING |
| 署名不正 | ログイン画面へ 302 | WARNING |
| 期限切れ | ログイン画面へ 302（`?e=expired` でメッセージ表示） | WARNING |
| aud / v 不一致 | ログイン画面へ 302 | WARNING |
| jti 再利用 | ログイン画面へ 302 | WARNING |
| デモユーザーの書き込み操作 | 403 | （ミドルウェアで拒否） |

失敗理由を画面上で細かく区別して見せない。期限切れのみ、UX のためメッセージを出す。

## 10. セキュリティ考慮事項

**チケットがアクセスログに残る点.** クエリ文字列で渡すため nginx の access log に記録される。
TTL 60 秒 + ワンタイム消費により、ログから拾っても再利用できない状態にする。
必要なら nginx の log_format からクエリ文字列を除外する。

**Referer 経由の漏洩.** B に `Referrer-Policy: strict-origin-when-cross-origin` を設定する。
consume は即座に 302 するため露出窓は狭いが、明示しておく。

**強制ログイン (session fixation).** 攻撃者が被害者に consume URL を踏ませる経路は理論上あるが、
ログイン先は権限のない読み取り専用の共有デモユーザーであり、`login()` がセッションを
再生成するため実害はない。

**通信路.** 両ホストとも HTTPS 必須。`SECURE_SSL_REDIRECT`、HSTS、
`SESSION_COOKIE_SECURE = True`、`CSRF_COOKIE_SECURE = True` を設定する。
nginx を前段に置くため `SECURE_PROXY_SSL_HEADER` と `ALLOWED_HOSTS`、
`CSRF_TRUSTED_ORIGINS` も忘れずに設定する。

**鍵の管理.** `SSO_TICKET_KEY` は 32 バイト以上のランダム値。両側の `.env` に同じ値を置き、
リポジトリにはコミットしない。`.env.example` にはキー名のみ記載する。

生成例:

    python -c "import secrets; print(secrets.token_urlsafe(48))"

**鍵ローテーション.** salt を `portfolio.sso.v2` に変更し、両側の鍵と salt を同時に入れ替える。
TTL が 60 秒のため、切替時の影響は最大 1 分間の遷移失敗にとどまる。

## 11. 割り切る点

- **A でログアウトしても B のセッションは残る.** ログアウトの伝播は行わない。
  B 側のセッション寿命を 1 時間 + ブラウザ終了で失効とすることで対処する。
- **B は A のユーザーを識別しない.** B のログには誰が操作したかが残らない。
  デモ用途（読み取り専用）の範囲としてこれを許容する。識別が必要になった時点で、
  チケットに `sub` を追加しユーザー払い出し方式へ移行する（設計変更は B 側のみで完結する）。
- **B は閲覧専用.** 公開デモでの実験実行・編集はできない。参照する結果は、
  ローカルで実行・選別してアップした記録に限る（データ投入は書き込み権を持つ別経路で行う）。

## 12. テスト観点

- 正常系: 有効チケットで `/` に到達し、`request.user.is_authenticated` が真
- 期限切れ: `max_age` 超過のチケットが拒否される
- 署名改ざん: トークンの 1 文字を変えたものが拒否される
- aud 不一致: `aud` が別値のチケットが拒否される
- 再利用: 同一チケットの 2 回目の消費が拒否される
- 鍵未設定: `SSO_TICKET_KEY=""` のとき 404
- A 側未ログイン: `/go/toolb/` が A のログイン画面へ遷移する
- デモユーザーの自動作成と、`set_unusable_password` により通常ログインが失敗すること
- 読み取り専用: デモユーザーでの POST/PUT/PATCH/DELETE が 403 になること
- （運用）DB 読み取り専用ロールで INSERT/UPDATE/DELETE が拒否されること

## 13. 実装チェックリスト

### A 側
- [ ] `SSO_TICKET_KEY` / `TOOLB_URL` を settings と `.env.example` に追加
- [ ] `SESSION_COOKIE_DOMAIN` が設定されていないことを確認
- [ ] `go_toolb` ビューと URL を追加
- [ ] テンプレートのリンクを `{% url 'go_toolb' %}` に

### B 側
- [ ] `sso` アプリを作成（views / urls / middleware）
- [ ] settings に SSO 関連 4 項目とセッション設定、CACHES を追加
- [ ] `DemoReadOnlyMiddleware` を登録（AuthenticationMiddleware の後ろ）
- [ ] `manage.py createcachetable` をデプロイ手順に追加
- [ ] `/accounts/login/` 系（通常認証）を整備
- [ ] 参照データ投入用の書き込み経路（管理コマンド or 別ロール）を用意
- [ ] テスト 10 件

### インフラ
- [ ] toolb.example.com の DNS レコード
- [ ] nginx の server ブロックと証明書（B は GET/HEAD 以外を 405 で補強）
- [ ] 両ホストの `.env` に同一の `SSO_TICKET_KEY`
- [ ] B の DB 接続ロールを読み取り専用に（参照データ投入は別ロール）
- [ ] B 用 gunicorn の systemd ユニット
