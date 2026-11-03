"""互动强度纯模型测试。"""

from __future__ import annotations

import random

import pytest

from core.domain.intensity import (
    IntensitySettings,
    IntensitySignals,
    compute_score,
    effective_interval_range,
    interval_scale_for_score,
    probability_for_score,
    sample_interval_seconds,
    skip_retry_seconds,
)


def hot_signals() -> IntensitySignals:
    return IntensitySignals(
        messages_24h=60,
        messages_7d=175,
        active_days_14=10,
        median_gap_minutes=1.5,
        avg_length_7d=80,
        question_ratio=0.4,
        exclaim_ratio=0.3,
        emoji_ratio=0.2,
        hours_since_last_user=0.0,
        reply_rate=0.9,
        avg_reply_minutes=3.0,
        total_user_messages=600,
        proactive_count_30d=12,
    )


def cold_signals() -> IntensitySignals:
    return IntensitySignals(
        messages_24h=0,
        messages_7d=1,
        active_days_14=1,
        median_gap_minutes=None,
        avg_length_7d=8,
        question_ratio=0.0,
        exclaim_ratio=0.0,
        emoji_ratio=0.0,
        hours_since_last_user=72.0,
        reply_rate=None,
        avg_reply_minutes=None,
        total_user_messages=1,
    )


class TestScore:
    def test_empty_user_is_dormant(self):
        result = compute_score(IntensitySignals())
        assert result.score == 0
        assert result.tier == "dormant"
        assert result.tier_label == "冷淡"

    def test_hot_user_reaches_blazing(self):
        result = compute_score(hot_signals())
        assert result.score >= 80, result
        assert result.tier == "blazing"
        assert result.tier_label == "炽热"

    def test_cold_user_stays_low(self):
        result = compute_score(cold_signals())
        assert result.score < 20, result
        assert result.tier == "dormant"

    def test_more_messages_increase_score(self):
        base = IntensitySignals(
            messages_24h=3,
            messages_7d=7,
            active_days_14=2,
            median_gap_minutes=20,
            avg_length_7d=30,
            hours_since_last_user=1,
        )
        hotter = IntensitySignals(**{**base.__dict__, "messages_24h": 20})
        assert compute_score(hotter).score > compute_score(base).score

    def test_faster_tempo_increases_score(self):
        base = hot_signals()
        slower = IntensitySignals(**{**base.__dict__, "median_gap_minutes": 90})
        assert compute_score(base).score > compute_score(slower).score

    def test_recency_decay(self):
        fresh = hot_signals()
        stale = IntensitySignals(**{**fresh.__dict__, "hours_since_last_user": 72.0})
        assert compute_score(fresh).score > compute_score(stale).score

    def test_components_are_normalized(self):
        result = compute_score(hot_signals())
        for name, value in result.components.items():
            assert 0.0 <= value <= 1.0, (name, value)

    def test_reply_history_affects_engagement(self):
        base = hot_signals()
        ignored = IntensitySignals(
            **{**base.__dict__, "reply_rate": 0.0, "avg_reply_minutes": None}
        )
        assert compute_score(base).score > compute_score(ignored).score


class TestProbabilityAndInterval:
    def test_probability_bounds(self):
        settings = IntensitySettings(probability_min=0.15, probability_max=0.95)
        assert probability_for_score(0, settings) == pytest.approx(0.15)
        assert probability_for_score(100, settings) == pytest.approx(0.95)
        assert (
            probability_for_score(50, settings)
            > probability_for_score(20, settings)
        )

    def test_probability_monotonic(self):
        settings = IntensitySettings()
        values = [probability_for_score(score, settings) for score in range(0, 101, 5)]
        assert values == sorted(values)

    def test_interval_scale_decreases_with_score(self):
        settings = IntensitySettings()
        cold = interval_scale_for_score(0, settings)
        hot = interval_scale_for_score(100, settings)
        assert cold > hot
        assert hot == pytest.approx(settings.interval_scale_hot)
        assert cold == pytest.approx(settings.interval_scale_cold)

    def test_effective_interval_floor(self):
        settings = IntensitySettings(min_interval_floor_minutes=10)
        min_seconds, max_seconds, _ = effective_interval_range(
            1, 2, 100, settings
        )
        assert min_seconds == 600
        assert max_seconds >= min_seconds

    def test_high_intensity_shortens_interval(self):
        settings = IntensitySettings()
        cold_range = effective_interval_range(30, 600, 0, settings)
        hot_range = effective_interval_range(30, 600, 100, settings)
        assert hot_range[0] < cold_range[0]
        assert hot_range[1] < cold_range[1]

    def test_sample_within_range(self):
        rng = random.Random(42)
        for _ in range(50):
            value = sample_interval_seconds(600, 1200, rng)
            assert 600 <= value <= 1200

    def test_skip_retry_is_short_and_bounded(self):
        settings = IntensitySettings(min_skip_retry_minutes=5, skip_retry_ratio=0.2)
        retry = skip_retry_seconds(1200, settings)
        assert retry == 300


class TestSettingsParsing:
    def test_percent_values_are_normalized(self):
        settings = IntensitySettings.from_dict(
            {
                "probability_min": 15,
                "probability_max": 95,
                "interval_scale_hot": 35,
                "interval_scale_cold": 115,
                "probability_gamma": 115,
                "interval_gamma": 100,
            }
        )
        assert settings.probability_min == pytest.approx(0.15)
        assert settings.probability_max == pytest.approx(0.95)
        assert settings.interval_scale_hot == pytest.approx(0.35)
        assert settings.interval_scale_cold == pytest.approx(1.15)
        assert settings.probability_gamma == pytest.approx(1.15)
        assert settings.interval_gamma == pytest.approx(1.0)

    def test_integer_one_is_one_percent(self):
        settings = IntensitySettings.from_dict(
            {"probability_min": 1, "probability_max": 2}
        )
        assert settings.probability_min == pytest.approx(0.01)
        assert settings.probability_max == pytest.approx(0.02)

    def test_invalid_values_fall_back(self):
        settings = IntensitySettings.from_dict(
            {"probability_min": "bad", "probability_max": None, "enable": "no"}
        )
        assert settings.probability_min == pytest.approx(0.15)
        assert settings.probability_max == pytest.approx(0.95)
        assert settings.enabled is False

    def test_max_below_min_is_clamped(self):
        settings = IntensitySettings.from_dict(
            {"probability_min": 80, "probability_max": 20}
        )
        assert settings.probability_min == pytest.approx(0.8)
        assert settings.probability_max == pytest.approx(0.8)
