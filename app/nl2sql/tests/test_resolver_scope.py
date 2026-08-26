"""nl2sql/core/axis_extractor。`issue_c016`：解消語の作用範囲（実LLM呼び出しでの検証）。

プロンプトの意味理解が正しく効いているかは、実際にLLMを呼ばないと
「効いているつもり」で終わる（`test_sql_executor.py` / `test_flagged_activity.py` と
同じ作法）。ただし、ここでのプローブはAPIキーの有無だけを見る（実呼び出しはしない）。
`test_flagged_activity.py` のプローブはローカルDBへの疎通確認で無料だが、ここでの
実体はOpenAI APIの有料呼び出しであり、Djangoのテスト検出（import）はこのファイルを
指定しなくても全モジュールを読み込むため、実呼び出しのプローブだと毎回のテスト実行に
隠れた課金が1回混入してしまう（`issue_c016` フォローアップ (2)）。APIキーが無い、
または空の環境では skip する。

固定文言（`FakeLLMClient`）でのパース・分岐のテストは `test_axis_extractor.py` に
すでにある。ここで確かめるのは**プロンプトが実際のモデルに対して意図どおり働くか**で、
対象は次の2方向。

    (1) 状態フラグを解消する語だけでは、ロスター完全性（型1）を解消しない
        → `issue_c015` パターンDの、角度を変えた再発（`s101`）の直り
    (2) 状態フラグを解消する語は、引き続きフラグ自体（型2）を解消する
        → 厳格化のしすぎで逆方向を壊していないか（`issue_c016` (2)）

`issue_c022`：`issue_c021`（4本目の盲検測定）で残った型1の誤り2件（どちらも
時間軸、`s310`・`s320` とは別の言い回しで確認）を追加した。

    (3) 実績の存在を条件にして無いものを除く「除く向き」の語も、
        ロスター完全性（型1）を解消する（含める向きの「〜も」しか読めなかった穴）
    (4) マスタ表が持つ日付列から導いた年・月・日の区分は TIME である
        （実績表の日付列に限らない）

`issue_c024`：`issue_c023`（5本目の盲検測定）で5回とも同じ誤りを出した`CATCHALL`の
規則を絞った（`s407`とは別の言い回しで確認）。

    (5) 列名だけを言う「〜別」「〜ごと」は値の名指しではなく、CATCHALLをRESOLVED
        にしない（「都道府県別」に限らず一般則であることの確認）
    (6) 値そのもの（「男」「女」）が現れている「男女別」はRESOLVEDのまま
        （厳格化のしすぎで逆方向を壊していないかの確認）

`issue_c027`：`issue_c025`で残った「安定した誤り」2件（`s513`・`s514`）のうち、`s514`
（閉じた区分はTIMEではない）は項目2への追記で直った。`s513`（グループ化している
マスタ表を「関わらない」と読まない）は項目6への追記で試みたが、`issue_c022`の
既存テスト（マスタ表の日付列由来の区分がTIME）を壊す相互作用が見つかり、
費用対効果で統括・設計レビューが項目6の追記を取り下げる判断をした
（`issue_c027_ClosedSetAndFlagInvolvement.md`「切り分けの結果を受けて」節）。
`s513`は未解決のまま——根本原因（`GROUP_BY`が複数軸を同時表現できない出力契約の
限界）は`docs/AD_Nl2Sql_Design.md` 8節「既知の限界」に記録した。

`issue_c030`：`issue_c029`で見つかった安定した誤り3件（`s605`・`s607`・`s620`）を
両方とも試みたが見送った。項目7（`SYNONYM`）の追記は`s607`・`s620`自体は直したが
（実測14/14ずつ）、無関係な`s617`・`s624`型（`店舗別`の`FLAG_STORES`判定）を
`issue_c026`以来の安定（20/20）から不安定（`exp_46`〜`50`の実測で`UNRESOLVED`
3/5・`NONE` 2/5＝約60%）へ悪化させる副作用が見つかった。
項目2（`GROUP_BY`）の追記も`s605`自体は直したが、同じく`FLAG_STORES`判定を
壊す相互作用が再現した。**どちらも費用対効果の判断が要るため、設計レビュー・
統括の決定を仰ぐことにし、規則の文面は`issue_c029`時点のまま変更していない**
（`issue_c030_SynonymAndFactColumnScope.md`参照）。
"""
import unittest

from decouple import config
from django.test import SimpleTestCase

from app.common.llm_client import get_client
from app.nl2sql.core.axis_extractor import AxisExtractor

MODEL = "openai:gpt-4.1-mini"


def _probe() -> bool:
    return bool(config("OPENAI_API_KEY", default=""))


API_AVAILABLE = _probe()


@unittest.skipUnless(API_AVAILABLE, "OpenAI API に接続できない")
class ResolverScopeRealCallTests(SimpleTestCase):
    def _extract(self, question: str):
        return AxisExtractor(get_client(MODEL)).extract(question)

    def test_state_language_does_not_resolve_roster(self):
        """`issue_c016` の見逃し実例。「現在営業中の」はフラグ（型2）だけを解消し、
        ロスター完全性（型1）は未解決のままであるべき。
        """
        extraction = self._extract(
            "現在営業中の各店舗について、直近1年間の売上合計を出してください。"
        )
        self.assertEqual(extraction.flag_stores, "RESOLVED")
        self.assertEqual(extraction.group_by_resolved, "UNRESOLVED")

    def test_another_state_language_example_does_not_resolve_roster(self):
        """同型の別文言でも同じ挙動になること（`s101` 特例でないことの確認）。"""
        extraction = self._extract(
            "廃番でない商品について、直近1年間の販売数量を商品名ごとに教えて。"
        )
        self.assertEqual(extraction.flag_products, "RESOLVED")
        self.assertEqual(extraction.group_by_resolved, "UNRESOLVED")

    def test_roster_language_resolves_roster(self):
        """逆に、ロスターを名指しした語は型1を解消する（`issue_c015` までの挙動を維持）。"""
        extraction = self._extract(
            "取引の無かった店舗も0円として含めて、店舗名ごとに7月の売上を教えて。"
        )
        self.assertEqual(extraction.group_by_resolved, "RESOLVED")

    def test_flag_language_still_resolves_the_flag_axis(self):
        """`issue_c016` (2)：厳格化のしすぎで、フラグ自体の解消判定が
        `UNRESOLVED` に倒れていないかを確認する（過検出防止の逆方向チェック）。
        """
        extraction = self._extract(
            "有効な会員だけを対象に、会員ランク別の人数を教えてください。"
        )
        self.assertEqual(extraction.flag_members, "RESOLVED")

    def test_exclusion_language_resolves_roster(self):
        """`issue_c022` (1)：`s310`の見立て（`s310`本体とは別の言い回し）。
        「実績があった〜だけ」のように無いものを除く語も、ロスター完全性を解消する。

        統括の実測でこの言い回しは6/6で安定（受け入れレビュー参照）。同型の
        別言い回し（会員版）は3/6と不安定だったため、断定できる対象ではないとして
        常設テストから外した（`issue_c022_TimeAxisResolution.md`参照）——単発の
        `assertEqual`で「1回の値」を書く行為になるため、通る文を選び直すのではなく
        テスト自体を置かないことにした。
        """
        extraction = self._extract(
            "取引実績があった店舗だけを対象に、店舗名ごとの売上合計を出してください。"
        )
        self.assertEqual(extraction.group_by_resolved, "RESOLVED")

    def test_master_table_date_column_grouping_is_time_axis(self):
        """`issue_c022` (2)：`s320`の見立て（`s320`本体とは別の言い回し）。
        マスタ表の日付列から導いた区分（実績表の日付列に限らない）もTIMEである。

        同型の別言い回し「店舗の開店年別に、店舗数の推移を教えてください。」は
        `issue_c022`受け入れ時点で統括の実測6/6と安定していたが、`issue_c027`の
        追記後に崩れた。状態ごとの実測は次のとおり（`issue_c027_ClosedSetAndFlagInvolvement.md`
        「切り分けの結果を受けて」「決定への対応の確認」節）。

            項目2＋項目6（取り下げ前） … 1/6・統括0/6
            項目2のみ（現行）           … 2/16・統括3/12。合わせて5/28（約18%）

        **現行でも約2割しかTIMEにならない。** 同じ規則でも「商品の発売年別に、
        新商品の登録点数を」は12/12で維持されており、壊れているのは実体を数える形だけ。「店舗数の**推移**」という言い方がENTITY・
        MASTER_STORESへの読みを強めている疑いがあるが、断定できるほど確度は高くない。
        `issue_c022`の作法どおり、断定できる（安定して外れる）ため常設テストから外した
        ——単発の`assertEqual`で「1回の値」を書く行為になるため、通る文を選び直すのではなく
        テスト自体を置かないことにした。規則の文面は変えていない。
        """
        extraction = self._extract(
            "商品の発売年別に、新商品の登録点数を教えてください。"
        )
        self.assertEqual(extraction.group_by, "TIME")

    def test_column_name_only_phrasing_does_not_resolve_catchall(self):
        """`issue_c024` (1)：`s407`の見立て（`s407`本体とは別の言い回し）。
        値そのものを含まない「〜ごと」（列名だけ）はCATCHALLをRESOLVEDにしない。
        """
        extraction = self._extract(
            "会員を居住都道府県ごとに集計してください。"
        )
        self.assertEqual(extraction.catchall, "pref_UNRESOLVED")

    def test_another_column_name_only_phrasing_does_not_resolve_catchall(self):
        """同型の別文言・別列（gender）でも同じ挙動になること（特例でないことの確認）。"""
        extraction = self._extract(
            "性別ごとの会員数を教えてください。"
        )
        self.assertEqual(extraction.catchall, "gender_UNRESOLVED")

    def test_named_values_phrasing_still_resolves_catchall(self):
        """`issue_c024`：厳格化のしすぎで、値そのものを名指しした言い回し
        （既存の規則の例文どおり「男女別」）までUNRESOLVEDに倒れていないかの確認。
        """
        extraction = self._extract("男女別の会員数を教えてください。")
        self.assertEqual(extraction.catchall, "gender_RESOLVED")
