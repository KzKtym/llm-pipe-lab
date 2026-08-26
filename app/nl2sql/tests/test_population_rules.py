"""nl2sql/core/population_rules。段階2・工程③（判定）。純コード、LLMは呼ばない。

`issue_c013` 1.3・「基準の確定」節、`DIAGNOSIS.md`「既定ポリシー」をそのまま
コードにしたものかを確かめる。`population_labels.json` は一切参照していない
（`issue_c014` の盲検制約）。

`issue_c015`：パターンB（識別子/分類列の区別・トップN除外）とパターンA
（データ照会による吸収判定）を追加。パターンAは `flagged_activity_checker` を
フェイクで注入し、DBには触れない。

`issue_c020`：型1（マスタ実体）を立てる前に、対象の母集合に実績ゼロの実体が
実在するかをデータ照会で確かめる。`roster_emptiness_checker` をフェイクで注入し、
DBには触れない（実DBでの確認は `test_flagged_activity.py`）。

`issue_c024`：`region_filter`（`stores.region`の絞り込み）を両チェッカーへ渡す。
型1と同じ表でなくても、絞り込み後にフラグ付き実体が実在しなければフラグ軸を
独立に解消する経路を追加した。フェイクでDBを避ける。

`issue_c028`：(a) 時間軸（`GROUP_BY=TIME`）にも空集合判定を広げた
（`time_emptiness_checker`、フェイクで注入）。(b) `GROUP_BY`の軸でなくても
`RATE_DENOMINATOR`がマスタ実体を指していれば型1の対象にした。
"""
from django.test import SimpleTestCase

from app.nl2sql.core.axis_extractor import AxisExtraction
from app.nl2sql.core.population_rules import classify


def extraction(
    aggregation="EVENT",
    group_by="NONE",
    group_by_granularity="NA",
    limited="NO",
    group_by_resolved="NA",
    flag_stores="NONE",
    flag_members="NONE",
    flag_products="NONE",
    synonym="NONE",
    catchall="NONE",
    period="NONE",
    region_filter="NONE",
    rate_denominator="NONE",
):
    return AxisExtraction(
        aggregation=aggregation,
        group_by=group_by,
        group_by_granularity=group_by_granularity,
        limited=limited,
        group_by_resolved=group_by_resolved,
        flag_stores=flag_stores,
        flag_members=flag_members,
        flag_products=flag_products,
        synonym=synonym,
        catchall=catchall,
        period=period,
        region_filter=region_filter,
        rate_denominator=rate_denominator,
    )


class Type1Tests(SimpleTestCase):
    def test_event_grouped_by_master_identity_is_at_risk(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_STORES", group_by_granularity="IDENTITY",
            group_by_resolved="UNRESOLVED",
        ))
        self.assertTrue(result.needs_clarification)
        axis = next(a for a in result.axes if a.kind == 1)
        self.assertFalse(axis.resolved)
        self.assertEqual(axis.default, "")  # 型1に既定は無い

    def test_event_grouped_by_time_is_at_risk(self):
        result = classify(extraction(aggregation="EVENT", group_by="TIME", group_by_resolved="UNRESOLVED"))
        self.assertTrue(result.needs_clarification)

    def test_resolved_master_group_by_does_not_need_clarification(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_STORES", group_by_granularity="IDENTITY",
            group_by_resolved="RESOLVED",
        ))
        self.assertFalse(result.needs_clarification)
        axis = next(a for a in result.axes if a.kind == 1)
        self.assertTrue(axis.resolved)

    def test_entity_aggregation_is_never_type1(self):
        result = classify(extraction(
            aggregation="ENTITY", group_by="MASTER_STORES", group_by_granularity="IDENTITY",
        ))
        self.assertFalse(any(a.kind == 1 for a in result.axes))

    def test_fact_column_group_by_is_not_type1(self):
        result = classify(extraction(aggregation="EVENT", group_by="FACT_COLUMN"))
        self.assertFalse(result.needs_clarification)
        self.assertFalse(any(a.kind == 1 for a in result.axes))

    def test_no_group_by_is_not_type1(self):
        result = classify(extraction(aggregation="EVENT", group_by="NONE"))
        self.assertFalse(result.needs_clarification)


class Type1GranularityTests(SimpleTestCase):
    """issue_c015 パターンB：分類列でのグループ化は型1にしない。"""

    def test_classification_grouping_is_never_type1(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_PRODUCTS",
            group_by_granularity="CLASSIFICATION", group_by_resolved="UNRESOLVED",
        ))
        self.assertFalse(result.needs_clarification)
        self.assertFalse(any(a.kind == 1 for a in result.axes))

    def test_classification_grouping_still_allows_type2(self):
        """分類列でのグループ化でも、マスタのフラグ自体は別に判定される。"""
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_PRODUCTS",
            group_by_granularity="CLASSIFICATION", flag_products="UNRESOLVED",
        ))
        self.assertFalse(any(a.kind == 1 for a in result.axes))
        axis = next(a for a in result.axes if a.kind == 2)
        self.assertEqual(axis.default, "全部含める")

    def test_identity_grouping_is_type1_as_before(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_PRODUCTS",
            group_by_granularity="IDENTITY", group_by_resolved="UNRESOLVED",
        ))
        self.assertTrue(any(a.kind == 1 for a in result.axes))


class Type1LimitedTests(SimpleTestCase):
    """issue_c015 パターンB：トップN形式は型1にしない。"""

    def test_limited_identity_grouping_is_not_type1(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_STORES",
            group_by_granularity="IDENTITY", limited="YES", group_by_resolved="UNRESOLVED",
        ))
        self.assertFalse(result.needs_clarification)
        self.assertFalse(any(a.kind == 1 for a in result.axes))

    def test_limited_identity_grouping_still_allows_type2(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_STORES",
            group_by_granularity="IDENTITY", limited="YES", flag_stores="UNRESOLVED",
        ))
        self.assertFalse(any(a.kind == 1 for a in result.axes))
        self.assertTrue(any(a.kind == 2 for a in result.axes))

    def test_not_limited_identity_grouping_is_type1(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_STORES",
            group_by_granularity="IDENTITY", limited="NO", group_by_resolved="UNRESOLVED",
        ))
        self.assertTrue(any(a.kind == 1 for a in result.axes))


class Type2FlagTests(SimpleTestCase):
    def test_entity_default_is_active_only(self):
        result = classify(extraction(aggregation="ENTITY", flag_stores="UNRESOLVED"))
        axis = next(a for a in result.axes if a.kind == 2)
        self.assertEqual(axis.default, "現役のみ")
        self.assertIn(axis, result.defaults_applied)

    def test_event_default_is_include_all(self):
        result = classify(extraction(aggregation="EVENT", flag_stores="UNRESOLVED"))
        axis = next(a for a in result.axes if a.kind == 2)
        self.assertEqual(axis.default, "全部含める")

    def test_resolved_flag_has_no_default(self):
        result = classify(extraction(aggregation="ENTITY", flag_stores="RESOLVED"))
        axis = next(a for a in result.axes if a.kind == 2)
        self.assertEqual(axis.default, "")
        self.assertNotIn(axis, result.defaults_applied)

    def test_multiple_tables_each_get_an_axis(self):
        result = classify(extraction(
            aggregation="EVENT", flag_stores="UNRESOLVED", flag_products="UNRESOLVED"
        ))
        kinds = [a.kind for a in result.axes]
        self.assertEqual(kinds.count(2), 2)

    def test_untouched_table_produces_no_axis(self):
        result = classify(extraction(aggregation="EVENT", flag_members="NONE"))
        self.assertFalse(any(a.kind == 2 and "members" in a.name for a in result.axes))


class Type2AbsorptionTests(SimpleTestCase):
    """issue_c015 パターンA：データ照会による吸収判定。フェイクでDBを避ける。"""

    def _identity_extraction(self, **kwargs):
        base = dict(
            aggregation="EVENT", group_by="MASTER_STORES", group_by_granularity="IDENTITY",
            group_by_resolved="UNRESOLVED", flag_stores="UNRESOLVED", period="2026-07-01..2026-08-01",
        )
        base.update(kwargs)
        return extraction(**base)

    def test_no_activity_absorbs_type2_into_type1(self):
        checker = lambda table, period, *, region_filter=None: False  # noqa: E731
        result = classify(self._identity_extraction(), flagged_activity_checker=checker)

        self.assertTrue(result.needs_clarification)
        self.assertEqual([a.kind for a in result.axes], [1])  # 型2が別立てされない

    def test_activity_keeps_type2_separate(self):
        checker = lambda table, period, *, region_filter=None: True  # noqa: E731
        result = classify(self._identity_extraction(), flagged_activity_checker=checker)

        self.assertEqual(sorted(a.kind for a in result.axes), [1, 2])

    def test_no_checker_falls_back_to_always_separate(self):
        """checker を渡さなければ吸収判定をせず、旧・issue_c014 の挙動のまま。"""
        result = classify(self._identity_extraction(), flagged_activity_checker=None)
        self.assertEqual(sorted(a.kind for a in result.axes), [1, 2])

    def test_checker_receives_table_and_period(self):
        received = []

        def checker(table, period, *, region_filter=None):
            received.append((table, period))
            return False

        classify(self._identity_extraction(period="2025-01-01..2026-01-01"), flagged_activity_checker=checker)
        self.assertEqual(received, [("stores", "2025-01-01..2026-01-01")])

    def test_absorption_does_not_apply_to_unrelated_table(self):
        """型1がstoresを指しているとき、membersのフラグは吸収の対象にならない。"""
        checker = lambda table, period, *, region_filter=None: False  # noqa: E731
        result = classify(
            self._identity_extraction(flag_members="UNRESOLVED"), flagged_activity_checker=checker
        )
        kinds = sorted(a.kind for a in result.axes)
        self.assertEqual(kinds, [1, 2])  # members分のkind=2は残る
        member_axis = next(a for a in result.axes if a.kind == 2)
        self.assertIn("members", member_axis.name)

    def test_resolved_flag_is_not_sent_to_checker(self):
        """すでに解消済みのフラグは吸収判定にかけるまでもない。"""
        called = []

        def checker(table, period, *, region_filter=None):
            called.append(table)
            return False

        classify(self._identity_extraction(flag_stores="RESOLVED"), flagged_activity_checker=checker)
        self.assertEqual(called, [])


class Type2RegionFilterTests(SimpleTestCase):
    """issue_c024：型1と同じ表でなくても、region_filterで絞り込んだ集合に
    フラグの立った実体が実在しなければ、そのフラグ軸を独立に解消する。フェイクでDBを避ける。
    """

    def _classification_extraction(self, **kwargs):
        # GROUP_BY_GRANULARITY=CLASSIFICATION なので type1_table は None になる
        # （パターンAの対象外＝s413のCLASSIFICATION側の再現）
        base = dict(
            aggregation="EVENT", group_by="MASTER_STORES", group_by_granularity="CLASSIFICATION",
            flag_stores="UNRESOLVED", period="NONE", region_filter="関西",
        )
        base.update(kwargs)
        return extraction(**base)

    def test_no_flagged_entity_in_region_resolves_the_flag_alone(self):
        """type1が立たない（type1_table=None）場合でも、絞り込み後にフラグ付き
        実体が実在しなければフラグ軸は解消する。
        """
        checker = lambda table, period, *, region_filter=None: False  # noqa: E731
        result = classify(self._classification_extraction(), flagged_activity_checker=checker)

        self.assertFalse(any(a.kind == 1 for a in result.axes))
        flag_axis = next(a for a in result.axes if a.kind == 2)
        self.assertTrue(flag_axis.resolved)
        self.assertEqual(flag_axis.default, "")

    def test_flagged_entity_exists_in_region_keeps_the_flag(self):
        checker = lambda table, period, *, region_filter=None: True  # noqa: E731
        result = classify(self._classification_extraction(), flagged_activity_checker=checker)

        flag_axis = next(a for a in result.axes if a.kind == 2)
        self.assertFalse(flag_axis.resolved)

    def test_no_region_filter_does_not_call_checker_for_unrelated_table_case(self):
        """region_filterがNONEなら、この新しい経路は発火しない（旧挙動のまま）。"""
        called = []

        def checker(table, period, *, region_filter=None):
            called.append(table)
            return False

        result = classify(
            self._classification_extraction(region_filter="NONE"),
            flagged_activity_checker=checker,
        )
        self.assertEqual(called, [])
        flag_axis = next(a for a in result.axes if a.kind == 2)
        self.assertFalse(flag_axis.resolved)  # 既定どおり未解決のまま

    def test_only_applies_to_stores(self):
        """region_filterはstoresにしか意味を持たない。membersのフラグには効かない。"""
        called = []

        def checker(table, period, *, region_filter=None):
            called.append(table)
            return False

        result = classify(
            extraction(
                aggregation="EVENT", group_by="MASTER_MEMBERS",
                group_by_granularity="CLASSIFICATION", flag_members="UNRESOLVED",
                region_filter="関西",
            ),
            flagged_activity_checker=checker,
        )
        self.assertEqual(called, [])
        flag_axis = next(a for a in result.axes if a.kind == 2)
        self.assertFalse(flag_axis.resolved)

    def test_resolved_flag_is_not_sent_to_checker(self):
        called = []

        def checker(table, period, *, region_filter=None):
            called.append(table)
            return False

        classify(
            self._classification_extraction(flag_stores="RESOLVED"),
            flagged_activity_checker=checker,
        )
        self.assertEqual(called, [])

    def test_checker_receives_region_filter(self):
        received = []

        def checker(table, period, *, region_filter=None):
            received.append((table, period, region_filter))
            return False

        classify(
            self._classification_extraction(period="2026-01-01..2026-02-01"),
            flagged_activity_checker=checker,
        )
        self.assertEqual(received, [("stores", "2026-01-01..2026-02-01", "関西")])


class Type1RosterEmptinessTests(SimpleTestCase):
    """issue_c020：母集合に実績ゼロの実体が実在するかをデータ照会で確かめる。フェイクでDBを避ける。"""

    def _identity_extraction(self, **kwargs):
        base = dict(
            aggregation="EVENT", group_by="MASTER_STORES", group_by_granularity="IDENTITY",
            group_by_resolved="UNRESOLVED", period="2026-07-01..2026-08-01",
        )
        base.update(kwargs)
        return extraction(**base)

    def test_empty_roster_resolves_type1(self):
        """実績ゼロの実体が居なければ、型1は立てない（resolvedにする）。"""
        checker = lambda table, *, active_only, period, region_filter=None: False  # noqa: E731
        result = classify(self._identity_extraction(), roster_emptiness_checker=checker)

        self.assertFalse(result.needs_clarification)
        axis = next(a for a in result.axes if a.kind == 1)
        self.assertTrue(axis.resolved)

    def test_non_empty_roster_keeps_type1(self):
        """実績ゼロの実体が実在するなら、型1はそのまま立てる。"""
        checker = lambda table, *, active_only, period, region_filter=None: True  # noqa: E731
        result = classify(self._identity_extraction(), roster_emptiness_checker=checker)

        self.assertTrue(result.needs_clarification)
        axis = next(a for a in result.axes if a.kind == 1)
        self.assertFalse(axis.resolved)

    def test_no_checker_falls_back_to_always_at_risk(self):
        """checker を渡さなければ照会をせず、issue_c015 までの挙動のまま。"""
        result = classify(self._identity_extraction(), roster_emptiness_checker=None)
        self.assertTrue(result.needs_clarification)

    def test_already_resolved_group_by_does_not_call_checker(self):
        """文面ですでに解消済みなら、照会するまでもない。"""
        called = []

        def checker(table, *, active_only, period, region_filter=None):
            called.append(table)
            return False

        classify(
            self._identity_extraction(group_by_resolved="RESOLVED"),
            roster_emptiness_checker=checker,
        )
        self.assertEqual(called, [])

    def test_active_only_reflects_resolved_flag(self):
        """フラグが解消済み（例: 現在営業中の）なら、絞り込み後の母集合で照会する。"""
        received = []

        def checker(table, *, active_only, period, region_filter=None):
            received.append((table, active_only, period))
            return False

        classify(
            self._identity_extraction(flag_stores="RESOLVED"),
            roster_emptiness_checker=checker,
        )
        self.assertEqual(received, [("stores", True, "2026-07-01..2026-08-01")])

    def test_active_only_is_false_when_flag_unresolved(self):
        """フラグが未解消（またはNONE）なら、マスタ全体で照会する。"""
        received = []

        def checker(table, *, active_only, period, region_filter=None):
            received.append(active_only)
            return False

        classify(
            self._identity_extraction(flag_stores="UNRESOLVED"),
            roster_emptiness_checker=checker,
        )
        classify(
            self._identity_extraction(flag_stores="NONE"),
            roster_emptiness_checker=checker,
        )
        self.assertEqual(received, [False, False])

    def test_checker_uses_the_flag_for_the_same_table_as_type1(self):
        """型1がstoresを指しているとき、参照するのはflag_storesであってflag_membersではない。"""
        received = []

        def checker(table, *, active_only, period, region_filter=None):
            received.append(active_only)
            return False

        classify(
            self._identity_extraction(flag_stores="NONE", flag_members="RESOLVED"),
            roster_emptiness_checker=checker,
        )
        self.assertEqual(received, [False])  # flag_membersがRESOLVEDでも無関係

    def test_time_axis_type1_is_unaffected(self):
        """issue_c020（`roster_emptiness_checker`）の対象はマスタ実体のみ。
        時間軸(GROUP_BY=TIME)は`time_emptiness_checker`（issue_c028(a)）が別に扱う
        ——`time_emptiness_checker`を渡さなければ、`roster_emptiness_checker`を
        渡していても時間軸には呼ばれず、常に型1が立つ（フォールバック）。
        """
        called = []

        def checker(table, *, active_only, period, region_filter=None):
            called.append(table)
            return False

        result = classify(
            extraction(aggregation="EVENT", group_by="TIME", group_by_resolved="UNRESOLVED"),
            roster_emptiness_checker=checker,
        )
        self.assertEqual(called, [])
        self.assertTrue(result.needs_clarification)

    def test_region_filter_is_passed_through(self):
        """issue_c024：region_filterがcheckerへそのまま渡る。"""
        received = []

        def checker(table, *, active_only, period, region_filter=None):
            received.append(region_filter)
            return False

        classify(
            self._identity_extraction(region_filter="関西"),
            roster_emptiness_checker=checker,
        )
        self.assertEqual(received, ["関西"])


class Type1TimeEmptinessTests(SimpleTestCase):
    """issue_c028 (a)：時間軸に実績ゼロの暦日が実在するかをデータ照会で確かめる。
    フェイクでDBを避ける（実DBでの確認は`test_flagged_activity.py`）。
    """

    def _time_extraction(self, **kwargs):
        base = dict(
            aggregation="EVENT", group_by="TIME", group_by_resolved="UNRESOLVED",
            period="2025-01-01..2026-01-01",
        )
        base.update(kwargs)
        return extraction(**base)

    def test_empty_period_resolves_type1(self):
        """実績ゼロの暦日が無ければ、型1は立てない（resolvedにする）。"""
        checker = lambda period: False  # noqa: E731
        result = classify(self._time_extraction(), time_emptiness_checker=checker)

        self.assertFalse(result.needs_clarification)
        axis = next(a for a in result.axes if a.kind == 1)
        self.assertTrue(axis.resolved)

    def test_non_empty_period_keeps_type1(self):
        """実績ゼロの暦日が実在するなら、型1はそのまま立てる。"""
        checker = lambda period: True  # noqa: E731
        result = classify(self._time_extraction(), time_emptiness_checker=checker)

        self.assertTrue(result.needs_clarification)
        axis = next(a for a in result.axes if a.kind == 1)
        self.assertFalse(axis.resolved)

    def test_no_checker_falls_back_to_always_at_risk(self):
        """checkerを渡さなければ照会をせず、issue_c027までの挙動のまま。"""
        result = classify(self._time_extraction(), time_emptiness_checker=None)
        self.assertTrue(result.needs_clarification)

    def test_already_resolved_group_by_does_not_call_checker(self):
        """文面ですでに解消済みなら、照会するまでもない。"""
        called = []

        def checker(period):
            called.append(period)
            return False

        classify(
            self._time_extraction(group_by_resolved="RESOLVED"),
            time_emptiness_checker=checker,
        )
        self.assertEqual(called, [])

    def test_checker_receives_period(self):
        received = []

        def checker(period):
            received.append(period)
            return False

        classify(
            self._time_extraction(period="2026-01-01..2026-02-01"),
            time_emptiness_checker=checker,
        )
        self.assertEqual(received, ["2026-01-01..2026-02-01"])

    def test_roster_emptiness_checker_not_called_for_time_axis(self):
        """`roster_emptiness_checker`（issue_c020）と`time_emptiness_checker`
        （issue_c028）は別の軸を担当する——時間軸では前者を呼ばない。"""
        called = []

        def roster_checker(table, *, active_only, period, region_filter=None):
            called.append(table)
            return False

        classify(
            self._time_extraction(),
            roster_emptiness_checker=roster_checker,
            time_emptiness_checker=lambda period: False,
        )
        self.assertEqual(called, [])


class Type1RateDenominatorTests(SimpleTestCase):
    """issue_c028 (b)：`GROUP_BY`の軸でなくても、`RATE_DENOMINATOR`（平均・比率の
    分母）がマスタ実体を指していれば型1の対象にする。フェイクでDBを避ける。
    """

    def test_rate_denominator_alone_triggers_type1(self):
        """GROUP_BYが無い単一値の平均でも、RATE_DENOMINATORがあれば型1になる
        （例:「会員1人あたりの平均購入回数」）。"""
        result = classify(extraction(
            aggregation="EVENT", group_by="NONE", group_by_resolved="UNRESOLVED",
            rate_denominator="MASTER_MEMBERS",
        ))
        self.assertTrue(result.needs_clarification)
        axis = next(a for a in result.axes if a.kind == 1)
        self.assertEqual(axis.name, "会員の母集合（結合欠落）")

    def test_rate_denominator_with_classification_group_by_triggers_type1(self):
        """分類列でグループ化していても（GRANULARITY=CLASSIFICATION、パターンBで
        通常は型1にならない）、RATE_DENOMINATORが立っていれば型1になる
        （例:「会員ランク別に、会員1人あたりの平均購入金額」）。"""
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_MEMBERS",
            group_by_granularity="CLASSIFICATION", group_by_resolved="UNRESOLVED",
            rate_denominator="MASTER_MEMBERS",
        ))
        self.assertTrue(result.needs_clarification)
        self.assertTrue(any(a.kind == 1 for a in result.axes))

    def test_group_by_identity_takes_precedence_no_duplicate_axis(self):
        """GROUP_BYがすでにマスタ軸(IDENTITY)を指しているとき、RATE_DENOMINATORが
        同じ表を指していても型1の軸は1つだけ（二重に立てない）。"""
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_STORES",
            group_by_granularity="IDENTITY", group_by_resolved="UNRESOLVED",
            rate_denominator="MASTER_STORES",
        ))
        self.assertEqual(sum(1 for a in result.axes if a.kind == 1), 1)

    def test_rate_denominator_none_is_not_type1(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="NONE", rate_denominator="NONE",
        ))
        self.assertFalse(any(a.kind == 1 for a in result.axes))

    def test_limited_excludes_rate_denominator_type1(self):
        """トップN形式なら、RATE_DENOMINATORがあっても型1にしない
        （パターンBと同じ考え方）。"""
        result = classify(extraction(
            aggregation="EVENT", group_by="NONE", limited="YES",
            rate_denominator="MASTER_MEMBERS",
        ))
        self.assertFalse(any(a.kind == 1 for a in result.axes))

    def test_entity_aggregation_with_rate_denominator_is_not_type1(self):
        """ENTITY集計（実体そのものを数える）には平均・比率の分母という概念が
        無い——型1の対象外（既存のガードをそのまま踏襲）。"""
        result = classify(extraction(
            aggregation="ENTITY", group_by="NONE", rate_denominator="MASTER_MEMBERS",
        ))
        self.assertFalse(any(a.kind == 1 for a in result.axes))

    def test_uses_roster_emptiness_checker_like_group_by_case(self):
        """データ照会の経路はGROUP_BYベースの型1と同じ`roster_emptiness_checker`
        を使う——新しい照会関数を増やしていない。"""
        received = []

        def checker(table, *, active_only, period, region_filter=None):
            received.append((table, active_only))
            return False

        result = classify(
            extraction(
                aggregation="EVENT", group_by="NONE", group_by_resolved="UNRESOLVED",
                rate_denominator="MASTER_MEMBERS", flag_members="RESOLVED",
            ),
            roster_emptiness_checker=checker,
        )
        self.assertEqual(received, [("members", True)])
        axis = next(a for a in result.axes if a.kind == 1)
        self.assertTrue(axis.resolved)  # checkerがFalse→実績ゼロの会員は居ない→解消


class Type3SynonymTests(SimpleTestCase):
    def test_unresolved_defaults_to_merge(self):
        result = classify(extraction(synonym="region_UNRESOLVED"))
        axis = next(a for a in result.axes if a.kind == 3)
        self.assertEqual(axis.default, "統合する")
        self.assertFalse(axis.resolved)

    def test_resolved_has_no_default(self):
        result = classify(extraction(synonym="channel_RESOLVED"))
        axis = next(a for a in result.axes if a.kind == 3)
        self.assertTrue(axis.resolved)
        self.assertEqual(axis.default, "")


class Type4CatchallTests(SimpleTestCase):
    def test_unresolved_defaults_to_independent_bucket(self):
        result = classify(extraction(catchall="pref_UNRESOLVED"))
        axis = next(a for a in result.axes if a.kind == 4)
        self.assertEqual(axis.default, "除外せず独立区分として表示")

    def test_resolved_has_no_default(self):
        result = classify(extraction(catchall="gender_RESOLVED"))
        axis = next(a for a in result.axes if a.kind == 4)
        self.assertTrue(axis.resolved)


class MixedCaseTests(SimpleTestCase):
    def test_type1_and_type2_can_coexist_without_checker(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_STORES", group_by_granularity="IDENTITY",
            group_by_resolved="UNRESOLVED", flag_stores="UNRESOLVED",
        ))
        self.assertTrue(result.needs_clarification)
        self.assertEqual(len(result.defaults_applied), 1)
        self.assertEqual(result.defaults_applied[0].kind, 2)

    def test_nothing_touched_yields_no_axes(self):
        result = classify(extraction())
        self.assertEqual(result.axes, [])
        self.assertFalse(result.needs_clarification)
        self.assertEqual(result.defaults_applied, [])


class StageTwoQuestionsSanityCheckTests(SimpleTestCase):
    """`stage2_questions.json`（10件、参考のみ・採点には使わない）の構造で、
    規則の判定が一致するかを確認する。`population_labels.json` は参照していない。
    """

    def test_s001_store_roster_risk(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_STORES", group_by_granularity="IDENTITY",
            group_by_resolved="UNRESOLVED",
        ))
        self.assertTrue(result.needs_clarification)

    def test_s004_member_roster_risk(self):
        result = classify(extraction(
            aggregation="EVENT", group_by="MASTER_MEMBERS", group_by_granularity="IDENTITY",
            group_by_resolved="UNRESOLVED", flag_members="RESOLVED",
        ))
        self.assertTrue(result.needs_clarification)
        flag_axis = next(a for a in result.axes if a.kind == 2)
        self.assertTrue(flag_axis.resolved)

    def test_c001_entity_count_no_risk_once_flag_resolved(self):
        result = classify(extraction(
            aggregation="ENTITY", group_by="MASTER_STORES", group_by_granularity="IDENTITY",
            flag_stores="RESOLVED",
        ))
        self.assertFalse(result.needs_clarification)
        self.assertEqual(result.defaults_applied, [])

    def test_c005_event_no_group_by_no_risk(self):
        result = classify(extraction(aggregation="EVENT", group_by="NONE"))
        self.assertFalse(result.needs_clarification)
        self.assertEqual(result.axes, [])

    def test_c008_fact_column_ratio_no_risk(self):
        result = classify(extraction(aggregation="EVENT", group_by="NONE"))
        self.assertFalse(result.needs_clarification)
