"""テスト用の共通土台。

移植元（別プロジェクト）では LoginRequiredMiddleware により全ビューがログイン必須に
なるため、ログイン済みクライアントを用意する目的でこのクラスを置いていた。

llm_pipe_lab は現状 認証を持たず、LoginRequiredMiddleware も外してある
（issue_081401 の判断）。そのため force_login は「無くても素通りする」状態だが、
移植元と同じ名前・同じ使い方で残しておく（A案）。将来 認証
（docs/AD_Auth_Federation.md）が入った時点で、テスト側を書き換えずにそのまま効く。

「未ログインだと弾かれること」自体の検証は認証導入時に別途用意する
（各アプリのテストで繰り返さない）。
"""
from django.contrib.auth.models import User
from django.test import TestCase

TEST_USERNAME = 'testclient'
TEST_PASSWORD = 'test-pass-1234'


class LoggedInTestCase(TestCase):
    """ログイン済みの self.client を持つ TestCase。

    setUp より前に走る _pre_setup でログインするため、各テストクラスの setUp を
    書き換えずに済む（super().setUp() の呼び忘れで壊れない）。
    ログインユーザーは self.test_user で参照できる。
    """

    def _pre_setup(self):
        super()._pre_setup()
        self.test_user = User.objects.create_user(
            username=TEST_USERNAME, password=TEST_PASSWORD
        )
        self.client.force_login(self.test_user)
