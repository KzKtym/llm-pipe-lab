"""段階2・工程①：自然文を構造化された対象（テーブル・軸・集計対象）に落とす。

3工程のうち、ここは AI が担当する部分である。

    ① 自然文を構造化された対象に落とす … AI（このモジュール）
    ② 開示文・確認文言の言い回しを作る … AI（`ambiguity_judge.phrase_disclosure`）
    ③ その構造が実際に曖昧かどうかの判定 … ルール（`population_rules.py`）

**SQLは書かない。判定もしない。** 要るのは「何を集計しているか」「何で分けているか」
という軽い理解だけで足りる。質問文がすでに軸を確定させていれば `RESOLVED`、
していなければ `UNRESOLVED` を返す——この切り分けはテキストの読み取りであり、AIの仕事。

出力契約は本体10行＋`FLAG_*`用3行。値域を正規表現に埋めてあるので、
契約から外れた値はパースで落ちる。パースできない場合は `LLMTransientError` を送出する。

設計と、この契約に至った経緯は `docs/AD_Stage2_RuleDesign.md` 4節。
プロンプトを変更する際の作法は `docs/MEASUREMENT.md` 8節（**変更前後で
蓄積した全セットを引き直し、全項目を突き合わせること**）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.common.llm_client import BaseLLMClient, LLMTransientError

DEFAULT_MAX_TOKENS = 300

_PROMPT = """\
あなたはデータ分析システムの構造化担当です。ユーザーの質問を読み、集計の構造を
特定してください。SQLは書かず、次の情報だけを判定します。

# スキーマの手がかり
マスタ表: stores（店舗）／members（会員）／products（商品）
実績表: sales（取引）／sale_items（明細）／daily_store_summary（日次店舗集計）
マスタ表にはそれぞれ状態フラグがある: stores.close_date（閉店日、NULLなら営業中）／
products.is_discontinued（廃番）／members.is_active（在籍中）
同義の表記ゆれを持つ列: stores.region（「関西」「近畿」）／sales.channel（「EC」「オンライン」）
包括値・欠損を持つ列: members.pref（「その他」を含む）／members.gender（「その他」・未回答を含む）
基準日: 2026-08-14（相対期間はここから絶対日付に直す）

# 判定すること

1. AGGREGATION: この質問が集計しているのは「実績」（event、例:売上・購入回数・販売数量）
   か「マスタの行そのもの」（entity、例:店舗数・会員数・商品数）か

2. GROUP_BY: 出力を分けている軸があれば、その種類
   - MASTER_STORES / MASTER_MEMBERS / MASTER_PRODUCTS: マスタ表の列で分けている
   - TIME: 日付・時刻から**開いた単位**（月別、日別、年別 等。列挙され得る個数が
     決まっていない）で分けている。**列がどの表にあるかは問わない。** 実績表の
     日付列（`sales.sold_at` 等）だけでなく、**マスタ表が持つ日付列
     （開店日・発売日 等）から導いた年・月・日の区分も同じく TIME である。**
     「マスタ表の列だから MASTER_*」と読まないこと——年・月・日のように
     値の種類が決まっていない区分は、列がどの表にあってもTIMEである。
     **「年」（暦年。2024年・2025年のように値の種類が年々増え続ける、日付列から
     一意に決まる区分）は常にTIMEである。3.のCLASSIFICATIONで例に挙げている
     「年代」（10代・20代のような、あらかじめ小数に決まった年齢層の区分）とは
     語が似ているが別物であり、混同しないこと。**
     **「今月と先月を比べて」のように、名前のついた
     固定2時点（またはそれ以上の決まった個数）を比較するだけの場合はここに含めない**
     （その場合は集計対象に応じて FACT_COLUMN か NONE にする）。
     **曜日別（月〜日の7区分）・時間帯別のように、日付・時刻から導いていても
     値の種類があらかじめ決まっている（閉じている）区分もTIMEではない**
     ——「列挙され得る個数が決まっていない」に当てはまらない。この場合も
     集計対象に応じて FACT_COLUMN か NONE にする。**曜日だけの特例ではなく、
     値の種類が閉じているかどうかで判定すること**
   - FACT_COLUMN: 実績表自身の列・条件で分けている（支払方法別、チャネル別、
     「会員／非会員」のように `sales.member_id` の NULL 判定だけで分かれる区分 等。
     **マスタ表の行を1件も見なくても判定できるなら、マスタに言及する語
     （「会員」等）が含まれていてもここに分類する**）
   - NONE: 分けていない（単一の値を1つ返す。固定2時点の比較もここ）

3. GROUP_BY_GRANULARITY: GROUP_BY が MASTER_* のときだけ判定する（それ以外は NA）
   - IDENTITY: 個体を識別する軸（店舗名、会員（個人）、商品名 等）で分けている。
     行が「消える」と実体そのものが結果から丸ごと落ちる
   - CLASSIFICATION: マスタが持つ少数の固定的な分類列（店舗形態、商品大分類・中分類、
     会員ランク、年代のように属性から作った区分 等）で分けている。値の種類が
     あらかじめ少数に決まっており、行の欠落で分類自体が丸ごと消えることはない

4. LIMITED: 出力が「トップN」「上位N件」「ワーストN」のように**件数を明示的に
   区切ったランキング**かどうか
   - YES: 件数を区切っている（トップ5、上位10 等）
   - NO: 区切っていない（「多い順に」のような並び替えだけの指定は NO）

5. GROUP_BY_RESOLVED: GROUP_BY が MASTER_* か TIME のとき、または
   11.のRATE_DENOMINATOR が NONE でないときだけ判定する（それ以外なら NA）。
   **「実績の無いマスタ行／期間も0（ゼロ）の行として並べるか、
   そもそも出さないか」を質問文がすでに明言しているか。**

   **同じマスタ表について、この軸（ロスターの完全性）と、実体そのものの状態
   （閉店・廃番・在籍かどうか等）による絞り込みの軸（この抽出とは別に判定する）は
   独立した別の軸である。一方を解消する語はもう一方を解消しない。** 見分け方は
   次のとおり。

   - **ロスター完全性を解消する語**は、**「実績・活動の有無」を主語にして、
     ゼロ実績のものを含めるか除くかを能動的に示す**。**この種の語がある時点で
     `RESOLVED` にすること——含める向き・除く向きのどちらでも同じ強さで解消する**
     （**「〜も」「〜さえ」のように含める向きの目印だけを探さないこと**）。
     - 含める向き（例:「取引が無かった店舗**も**含めて」「売れなかった商品**も**
       並べて」「実績の無い日**も**0で」）
     - 除く向き（例:「実績のあった商品**だけ**を対象に」「購入の**あった**会員に
       絞って」のように、実績の存在そのものを条件にして、無いものを対象外にすると
       述べている語）。**この種の語が指すのは「実績・活動の有無」であって、
       実体そのものの状態（閉店・廃番・在籍かどうか等）を絞り込む語ではない。**
       その条件が実体の状態を指しているのか、実績の有無を指しているのかを
       見分けること。**「実績があった」「〜のあった」のように実績の有無を
       条件にした語は、含める向きの語（「〜も」等）と同じ強さでロスター完全性を
       `RESOLVED`にする——実体の状態を絞り込む語だと誤読して、この軸を
       `UNRESOLVED`のままにしないこと**
   - **フラグ（状態）を解消する語**は、**実体そのものの現在の状態を絞り込む**
     （例:「現在営業中の」「まだ閉店していない」「廃番を除いた」「有効な会員」）。
     **これらの語は「その状態の実体だけを対象にする」と言っているだけで、
     「対象にした実体のうち、実績がゼロのものを0行として並べるか」には
     一切触れていない。** フラグを解消する語（「現在営業中の」「廃番を除いた」等）
     だけでは、それがどれほど対象を絞り込んでいても `GROUP_BY_RESOLVED` を
     `RESOLVED` にしないこと——絞り込んだ後の母集合のうち実績ゼロの行を
     0として並べるかどうかは、フラグの絞り込みとは無関係になお未確定のまま残る
   - 日付の粒度を解消する語（「取引が無かった**日**も含めて」）も、
     マスタ実体側の母集合（店舗・会員・商品）は解消しない。両者は別の軸
   - RESOLVED: ロスター完全性そのものを明言している
   - UNRESOLVED: 明言していない（フラグだけを解消する語がある場合を含む）
   - NA: GROUP_BY が FACT_COLUMN か NONE で、かつ RATE_DENOMINATOR も NONE のとき

7. SYNONYM: **`region` か `channel` の値そのものを条件・グループ化に使っているときだけ**
   判定する（支払方法・分類など他の列では絶対に使わない）。使っているなら、
   表記ゆれ（関西/近畿、EC/オンライン）を統合するかどうかを質問文が明言しているか。
   **「EC＋オンライン」のように合算後の呼び方まで書いてあれば RESOLVED**
   - NONE / region_RESOLVED / region_UNRESOLVED / channel_RESOLVED / channel_UNRESOLVED

8. CATCHALL: `pref` か `gender` の値で分けているか。分けているなら、
   包括値（その他・未回答）の扱いを質問文が明言しているか。**質問文に値そのもの
   （「男」「女」のような具体的な値）が現れており、それが値域の一部だけを
   名指ししている場合（例:「男女別」は「男」「女」という値そのものが現れ、
   性別のうち2値だけを名指ししている）は、名指しされなかった包括値・NULLは
   対象外という意思表示とみなし RESOLVED とする。** **列名だけを言う「〜別」
   「〜ごと」（例:「都道府県別」「性別ごと」）は値の名指しではない——値そのものが
   現れていなければ、列名を挙げているだけでは RESOLVED にしないこと**
   - NONE / pref_RESOLVED / pref_UNRESOLVED / gender_RESOLVED / gender_UNRESOLVED

9. PERIOD: 質問が対象とする期間。基準日から絶対日付に直し、開始日と終了日（終了日は
   含まない半開区間）を `YYYY-MM-DD..YYYY-MM-DD` の形式で書く。期間の指定が無ければ `NONE`

10. REGION_FILTER: 質問文が `stores.region` の**値を条件として名指しし、その値に
    絞り込んでいる**か（例:「関西エリアの店舗」「北海道の店舗」）。絞り込んでいるなら、
    その値（中国／中部／九州／北海道／東北／近畿／関東／関西 のいずれか）を書く。
    **7.のSYNONYMのように地域別に分ける（グループ化する）だけで、特定の値に
    絞り込んでいない場合は NONE**

11. RATE_DENOMINATOR: 質問文に「会員1人あたり」「店舗1店あたり」「商品1点あたり」
    のように**分母を明示する語がある**場合だけ、**マスタ実体の個数を分母にした
    平均・比率**を求めているとみなす。**明示する語が無ければ`NONE`にすること**
    ——「会員ランク別の平均購入金額」のように分母を明示していない平均は、
    それだけでは対象にしない（推測しない）。
    **2.のGROUP_BYがそのマスタ表を指しているかどうかは問わない**——「会員ランク別に、
    会員1人あたりの平均購入金額」のように、別の軸（会員ランク）でグループ化していても、
    「1人あたり」と明示していれば対象にする。
    **「1取引あたり」「1件あたり」のように実績（`sales`の1行）を分母にする語は、
    マスタ実体を分母にしていないため対象にしない**——マスタの名前（「会員」等）が
    近くに出てきていても、分母として明示されているのが取引・件数であれば`NONE`。
    - NONE: マスタ実体の個数を分母にした平均・比率ではない（分母の明示が無い、
      または実績を分母にしている場合を含む）
    - MASTER_STORES / MASTER_MEMBERS / MASTER_PRODUCTS: その表の実体数が分母

# 出力形式
次の10行だけを出力してください。説明・前置きは付けないでください。

AGGREGATION: EVENT または ENTITY
GROUP_BY: MASTER_STORES / MASTER_MEMBERS / MASTER_PRODUCTS / TIME / FACT_COLUMN / NONE のいずれか
GROUP_BY_GRANULARITY: IDENTITY / CLASSIFICATION / NA のいずれか
LIMITED: YES / NO のいずれか
GROUP_BY_RESOLVED: RESOLVED / UNRESOLVED / NA のいずれか
SYNONYM: NONE / region_RESOLVED / region_UNRESOLVED / channel_RESOLVED / channel_UNRESOLVED のいずれか
CATCHALL: NONE / pref_RESOLVED / pref_UNRESOLVED / gender_RESOLVED / gender_UNRESOLVED のいずれか
PERIOD: NONE または YYYY-MM-DD..YYYY-MM-DD
REGION_FILTER: NONE / 中国 / 中部 / 九州 / 北海道 / 東北 / 近畿 / 関東 / 関西 のいずれか
RATE_DENOMINATOR: NONE / MASTER_STORES / MASTER_MEMBERS / MASTER_PRODUCTS のいずれか

# 質問
{question}

# 出力"""

_FLAG_PROMPT = """\
あなたはデータ分析システムの構造化担当です。ユーザーの質問を読み、
stores（店舗）・members（会員）・products（商品）という3つのマスタ表それぞれが、
この質問に関わるかどうかだけを判定してください。SQLは書きません。

# スキーマの手がかり
マスタ表: stores（店舗）／members（会員）／products（商品）
実績表: sales（取引）／sale_items（明細）／daily_store_summary（日次店舗集計）
マスタ表にはそれぞれ状態フラグがある: stores.close_date（閉店日、NULLなら営業中）／
products.is_discontinued（廃番）／members.is_active（在籍中）
基準日: 2026-08-14（相対期間はここから絶対日付に直す）

# すでに分かっていること（別の抽出で判定済み。そのまま使うこと）
AGGREGATION: {aggregation}
GROUP_BY: {group_by}
GROUP_BY_GRANULARITY: {group_by_granularity}

**GROUP_BY が MASTER_STORES／MASTER_MEMBERS／MASTER_PRODUCTS であれば、
そのマスタ表は必ず「関わる」（NONE にしないこと）。** それ以外の表について、
または GROUP_BY 以外の経路でこの質問に関わるかどうかは、質問文から判定すること。

**「関わる」は、そのマスタ表自身の列（状態フラグ・分類列・属性等）を、
グループ化・条件・計算のいずれかで明示的に使っているときだけ成立する。**
**`sales`／`sale_items`／`daily_store_summary`（実績表）自身の列だけで分かれる
区分は、それだけでは`stores`・`members`・`products`のどれも「関わる」にしない
——「売上を集計しているのだから店舗も関わるはずだ」のような推測はしないこと。**

**例（関わらない＝`NONE`。実績表自身の列だけで分かれる区分）:**

- 「チャネル別」「支払方法別」のように実績表自身の列（`sales.channel`・
  `sales.payment_method`）だけで分かれる区分は、`stores`・`members`・`products`の
  どの列も使っていなければ3つとも`NONE`とすること。
- 「会員／非会員」のように`sales.member_id`のNULL判定だけで分かれる区分も、
  `members`自身の列（`is_active`等）を条件・計算に使っていなければ
  `FLAG_MEMBERS`は`NONE`である——「会員」という語が出てきているという理由だけで
  `members`が関わるとみなさないこと。

**ただし、「関わる」ことと「フラグの扱いを明言している」ことは別である。**
GROUP_BY や AGGREGATION に現れているという事実だけでは、現役のみか全部含めるかを
**明言したことにはならない。** 質問文自体に、その状態（閉店・廃番・在籍等）の
扱いを示す語（「現在営業中の」「廃番を除いた」「有効な会員」等）が無ければ、
関わっていても `UNRESOLVED` のままにすること。**これは`GROUP_BY_GRANULARITY`が
`IDENTITY`（個体ごとの内訳）でも`CLASSIFICATION`（分類ごとの内訳）でも、
`AGGREGATION`が`EVENT`でも`ENTITY`（マスタ実体そのものを選ぶ・数える・
ランキングする）でも、同じ判断である。**

**例（関わる。3つのマスタ表いずれについても、`IDENTITY`/`CLASSIFICATION`の
どちらでも、同じ判断であること）:**

- 「店舗形態別の売上合計」は`stores`の分類列（`CLASSIFICATION`）でグループ化
  しているので`FLAG_STORES`は関わるが、閉店した店舗を含めるかどうかは何も
  述べていないため`UNRESOLVED`。**「現在営業中の店舗を、店舗形態別に」であれば、
  同じグループ化でも状態を明言しているため`RESOLVED`。**
- 「店舗別」「店舗ごと」のように`stores`を個体（`IDENTITY`）ごとに分けている
  場合や、「最も〜な店舗」のように実体そのものを選ぶ（`AGGREGATION: ENTITY`）
  場合も、閉店した店舗を対象・候補に含めるかどうかを明言していなければ
  `FLAG_STORES`は`UNRESOLVED`である——個体ごとの内訳や実体の選択だからといって
  明言済みとはみなさないこと。
- 「会員ランク別の会員数」は`members`の分類列でグループ化しているので
  `FLAG_MEMBERS`は関わるが、退会した会員を含めるかどうかは何も述べていないため
  `UNRESOLVED`。**「有効な会員だけを対象に、会員ランク別に」であれば、
  同じグループ化でも状態を明言しているため`RESOLVED`。**
- 「都道府県別」「性別ごと」のように`pref`・`gender`で分類グループ化している
  場合も、`会員ランク`のような他の分類列と**まったく同じ扱いである**——
  `pref`は値の種類が多く、`gender`は少ないという違いはあるが、値の種類の
  多さは判断に関係しない。どちらも、明言が無ければ`FLAG_MEMBERS`は
  `UNRESOLVED`とすること。
- 「商品の大分類別の販売数量」は`products`の分類列でグループ化しているので
  `FLAG_PRODUCTS`は関わるが、廃番の商品を含めるかどうかは何も述べていないため
  `UNRESOLVED`。**「廃番でない商品について、大分類別に」であれば、
  同じグループ化でも状態を明言しているため`RESOLVED`。**

**グループ化・分類の軸に使われていること自体を、状態の扱いの明言と取り違えないこと
——`stores`・`members`・`products`のどれについても、`IDENTITY`/`CLASSIFICATION`の
どちらでも、`EVENT`/`ENTITY`のどちらでも同じである。**

# 判定すること

FLAG_STORES / FLAG_MEMBERS / FLAG_PRODUCTS: それぞれのマスタ表がこの質問に
関わるか。**AGGREGATION の対象、GROUP_BY の軸（そのマスタ表の列でグループ化・分類している
場合を含む。上の「すでに分かっていること」を使う）、または**グループ化を伴わない条件・計算に
その表の属性を使っている場合**（例:「定価より安い」→ `products.unit_price`、
「生年月日が登録されている会員」→ `members.birth_date`、「ゴールド会員」→
`members.member_rank`）も「関わる」に含める。**「実績・活動の有無」
（取引があった／無かった 等）を条件にしている場合は、ここには含めない
——それは別の抽出（ロスターの完全性）が扱う軸である。** 関わるなら、フラグの扱い
（現役のみか全部含めるか）を質問文がすでに明言しているか
   - NONE: 関わらない
   - RESOLVED: 関わり、かつ質問文がフラグの扱いを明言している
   - UNRESOLVED: 関わるが、質問文が明言していない

# 出力形式
次の3行だけを出力してください。説明・前置きは付けないでください。

FLAG_STORES: NONE / RESOLVED / UNRESOLVED のいずれか
FLAG_MEMBERS: NONE / RESOLVED / UNRESOLVED のいずれか
FLAG_PRODUCTS: NONE / RESOLVED / UNRESOLVED のいずれか

# 質問
{question}

# 出力"""

_AGGREGATION_RE = re.compile(r"AGGREGATION:\s*(EVENT|ENTITY)", re.IGNORECASE)
_GROUP_BY_RE = re.compile(
    r"GROUP_BY:\s*(MASTER_STORES|MASTER_MEMBERS|MASTER_PRODUCTS|TIME|FACT_COLUMN|NONE)",
    re.IGNORECASE,
)
_GRANULARITY_RE = re.compile(
    r"GROUP_BY_GRANULARITY:\s*(IDENTITY|CLASSIFICATION|NA)", re.IGNORECASE
)
_LIMITED_RE = re.compile(r"LIMITED:\s*(YES|NO)", re.IGNORECASE)
_GROUP_BY_RESOLVED_RE = re.compile(
    r"GROUP_BY_RESOLVED:\s*(RESOLVED|UNRESOLVED|NA)", re.IGNORECASE
)
_FLAG_RE = {
    "stores": re.compile(r"FLAG_STORES:\s*(NONE|RESOLVED|UNRESOLVED)", re.IGNORECASE),
    "members": re.compile(r"FLAG_MEMBERS:\s*(NONE|RESOLVED|UNRESOLVED)", re.IGNORECASE),
    "products": re.compile(r"FLAG_PRODUCTS:\s*(NONE|RESOLVED|UNRESOLVED)", re.IGNORECASE),
}
_SYNONYM_RE = re.compile(
    r"SYNONYM:\s*(NONE|region_RESOLVED|region_UNRESOLVED|channel_RESOLVED|channel_UNRESOLVED)",
    re.IGNORECASE,
)
_CATCHALL_RE = re.compile(
    r"CATCHALL:\s*(NONE|pref_RESOLVED|pref_UNRESOLVED|gender_RESOLVED|gender_UNRESOLVED)",
    re.IGNORECASE,
)
_PERIOD_RE = re.compile(
    r"PERIOD:\s*(NONE|\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2})", re.IGNORECASE
)
_REGION_FILTER_RE = re.compile(
    r"REGION_FILTER:\s*(NONE|中国|中部|九州|北海道|東北|近畿|関東|関西)", re.IGNORECASE
)
_RATE_DENOMINATOR_RE = re.compile(
    r"RATE_DENOMINATOR:\s*(NONE|MASTER_STORES|MASTER_MEMBERS|MASTER_PRODUCTS)", re.IGNORECASE
)


def build_prompt(question: str) -> str:
    return _PROMPT.format(question=question)


def build_flag_prompt(
    question: str, *, aggregation: str, group_by: str, group_by_granularity: str
) -> str:
    """`FLAG_*`用の別呼び出しのプロンプトを組み立てる（`issue_c034`）。

    本体の抽出結果（`AGGREGATION`/`GROUP_BY`/`GROUP_BY_GRANULARITY`）を渡す——
    2回の呼び出し間で「`GROUP_BY`は`MASTER_STORES`なのに`FLAG_STORES`が`NONE`」
    のような食い違いが起きにくくするため。
    """
    return _FLAG_PROMPT.format(
        question=question,
        aggregation=aggregation,
        group_by=group_by,
        group_by_granularity=group_by_granularity,
    )


def prompt_digest() -> str:
    """`_PROMPT`（本体。質問文を差し込む前のテンプレート）の内容ハッシュ。

    `issue_c031`：実験記録にプロンプトの版を残すため。`_PROMPT`の文言を
    1文字でも変えると値が変わる（`dataset_digest`と同じ形）。
    """
    return hashlib.sha256(_PROMPT.encode("utf-8")).hexdigest()


def flag_prompt_digest() -> str:
    """`_FLAG_PROMPT`（`FLAG_*`用。質問文・本体の結果を差し込む前のテンプレート）の
    内容ハッシュ（`issue_c034`）。`prompt_digest()`と同じ形。
    """
    return hashlib.sha256(_FLAG_PROMPT.encode("utf-8")).hexdigest()


@dataclass
class AxisExtraction:
    """質問1件から抽出した構造。判定（③）はここではまだ行わない。"""

    aggregation: str              # "EVENT" | "ENTITY"
    group_by: str                 # "MASTER_STORES" | "MASTER_MEMBERS" | "MASTER_PRODUCTS" | "TIME" | "FACT_COLUMN" | "NONE"
    group_by_granularity: str     # "IDENTITY" | "CLASSIFICATION" | "NA"
    limited: str                  # "YES" | "NO"
    group_by_resolved: str        # "RESOLVED" | "UNRESOLVED" | "NA"
    flag_stores: str              # "NONE" | "RESOLVED" | "UNRESOLVED"
    flag_members: str
    flag_products: str
    synonym: str                  # "NONE" | "region_RESOLVED" | "region_UNRESOLVED" | "channel_RESOLVED" | "channel_UNRESOLVED"
    catchall: str                 # "NONE" | "pref_RESOLVED" | "pref_UNRESOLVED" | "gender_RESOLVED" | "gender_UNRESOLVED"
    period: str = "NONE"          # "NONE" | "YYYY-MM-DD..YYYY-MM-DD"
    region_filter: str = "NONE"   # "NONE" | 中国 | 中部 | 九州 | 北海道 | 東北 | 近畿 | 関東 | 関西
    rate_denominator: str = "NONE"  # "NONE" | "MASTER_STORES" | "MASTER_MEMBERS" | "MASTER_PRODUCTS"
    raw_response: str = ""
    prompt: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _match_one(pattern: re.Pattern, text: str, field: str) -> str:
    match = pattern.search(text or "")
    if not match:
        raise LLMTransientError(f"{field} 行が見つかりません: {text!r}")
    return match.group(1).upper() if field not in ("SYNONYM", "CATCHALL") else match.group(1)


def parse_main_extraction(text: str) -> dict:
    """本体（10行）をパースする。1行でも欠けたり値が不正なら `LLMTransientError`。"""
    aggregation = _match_one(_AGGREGATION_RE, text, "AGGREGATION")
    group_by = _match_one(_GROUP_BY_RE, text, "GROUP_BY")
    granularity = _match_one(_GRANULARITY_RE, text, "GROUP_BY_GRANULARITY")
    limited = _match_one(_LIMITED_RE, text, "LIMITED")
    group_by_resolved = _match_one(_GROUP_BY_RESOLVED_RE, text, "GROUP_BY_RESOLVED")
    synonym_match = _SYNONYM_RE.search(text or "")
    if not synonym_match:
        raise LLMTransientError(f"SYNONYM 行が見つかりません: {text!r}")
    catchall_match = _CATCHALL_RE.search(text or "")
    if not catchall_match:
        raise LLMTransientError(f"CATCHALL 行が見つかりません: {text!r}")
    period_match = _PERIOD_RE.search(text or "")
    if not period_match:
        raise LLMTransientError(f"PERIOD 行が見つかりません: {text!r}")
    region_filter_match = _REGION_FILTER_RE.search(text or "")
    if not region_filter_match:
        raise LLMTransientError(f"REGION_FILTER 行が見つかりません: {text!r}")
    rate_denominator = _match_one(_RATE_DENOMINATOR_RE, text, "RATE_DENOMINATOR")

    return {
        "aggregation": aggregation,
        "group_by": group_by,
        "group_by_granularity": granularity,
        "limited": limited,
        "group_by_resolved": group_by_resolved,
        "synonym": _normalize_enum(synonym_match.group(1)),
        "catchall": _normalize_enum(catchall_match.group(1)),
        "period": _normalize_none_or_value(period_match.group(1)),
        "region_filter": _normalize_none_or_value(region_filter_match.group(1)),
        "rate_denominator": rate_denominator,
    }


def parse_flag_extraction(text: str) -> dict:
    """`FLAG_*`用（3行）をパースする（`issue_c034`）。

    1行でも欠けたり値が不正なら `LLMTransientError`。
    """
    flags = {
        table: _match_one(pattern, text, f"FLAG_{table.upper()}")
        for table, pattern in _FLAG_RE.items()
    }
    return {
        "flag_stores": flags["stores"],
        "flag_members": flags["members"],
        "flag_products": flags["products"],
    }


def _normalize_enum(value: str) -> str:
    """`region_resolved` のような大文字小文字ゆれを正規の表記に揃える。"""
    if value.upper() == "NONE":
        return "NONE"
    prefix, _, suffix = value.partition("_")
    return f"{prefix.lower()}_{suffix.upper()}"


def _normalize_none_or_value(value: str) -> str:
    if value.upper() == "NONE":
        return "NONE"
    return value


class AxisExtractor:
    """工程①だけを走らせる。SQLは扱わない。"""

    def __init__(
        self,
        client: BaseLLMClient,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ):
        self.client = client
        self.max_tokens = max_tokens
        self.temperature = temperature

    def extract(self, question: str) -> AxisExtraction:
        """質問1件から構造を抽出する。

        `issue_c034`：本体（`FLAG_*`を除く9項目）と`FLAG_*`（3項目）を別々に呼ぶ。
        `AxisExtraction`の形は変わらない——2回分を1つにまとめて返す。

        Raises:
            LLMFatalError: 認証切れ・レート超過など（呼び出し側でループ中断）
            LLMTransientError: 通信断・単発の不備・出力のパース失敗
        """
        prompt = build_prompt(question)
        response = self.client.complete(
            prompt, max_tokens=self.max_tokens, temperature=self.temperature
        )
        fields = parse_main_extraction(response.text)

        flag_prompt = build_flag_prompt(
            question,
            aggregation=fields["aggregation"],
            group_by=fields["group_by"],
            group_by_granularity=fields["group_by_granularity"],
        )
        flag_response = self.client.complete(
            flag_prompt, max_tokens=self.max_tokens, temperature=self.temperature
        )
        flag_fields = parse_flag_extraction(flag_response.text)

        return AxisExtraction(
            **fields,
            **flag_fields,
            raw_response=response.text + "\n---\n" + flag_response.text,
            prompt=prompt + "\n---\n" + flag_prompt,
            model=response.model,
            prompt_tokens=response.prompt_tokens + flag_response.prompt_tokens,
            completion_tokens=response.completion_tokens + flag_response.completion_tokens,
            latency_ms=response.latency_ms + flag_response.latency_ms,
        )
