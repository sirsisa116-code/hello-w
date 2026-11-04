"""指標を点数に変え、分野ごとの点数と総合点を出し、ランキングにする。

点数ルールは config/weights.yaml に書いてある。ここではルールを当てはめるだけで、
数字そのものは持たない。どの指標で何点入ったかは Contribution として全部残し、
レポートで「スコアの根拠」として表示する。
"""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from kabu.analysis.technical import INDICATORS, compute_indicators
from kabu.config import PROJECT_ROOT
from kabu.quality import QualityLog

SOURCE = "scoring"
CATEGORY_LABELS = {"technical": "テクニカル", "fundamental": "ファンダメンタル", "news": "ニュース"}
NEUTRAL = 50.0


@dataclass
class Contribution:
    key: str
    label: str
    value: float
    value_text: str
    points: float
    note: str


@dataclass
class CategoryScore:
    name: str
    score: float | None                 # None は「欠損」（データ不足で点数を付けられない）
    contributions: list[Contribution] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 計算できなかった指標の表示名


def load_weights(path: Path | None = None) -> dict:
    path = path or PROJECT_ROOT / "config" / "weights.yaml"
    with open(path, encoding="utf-8") as f:
        weights = yaml.safe_load(f)
    unknown = set(weights["technical"]["rules"]) - set(INDICATORS)
    if unknown:
        raise ValueError(f"weights.yaml に知らない指標名があります: {sorted(unknown)}"
                         f"（使える名前: {list(INDICATORS)}）")
    return weights


def match_band(value: float, bands: list[dict]) -> tuple[float, str]:
    """上から順に条件を見て、最初に当てはまった行の (点数, 説明) を返す。"""
    for band in bands:
        if "min" in band and value < band["min"]:
            continue
        if "max" in band and value >= band["max"]:
            continue
        return float(band["points"]), band["note"]
    return 0.0, "どの条件にも当てはまらない"


def score_technical(indicators: dict, conf: dict) -> CategoryScore:
    rules = conf["rules"]
    result = CategoryScore("technical", None)
    for key, bands in rules.items():
        label, fmt = INDICATORS[key]
        value = indicators.get(key, np.nan)
        if pd.isna(value):
            result.missing.append(label)
            continue
        points, note = match_band(value, bands)
        result.contributions.append(Contribution(key, label, value, fmt.format(value), points, note))

    if len(result.contributions) / len(rules) >= conf["min_available_ratio"]:
        total = NEUTRAL + sum(c.points for c in result.contributions)
        result.score = float(min(100.0, max(0.0, total)))
    return result


def combine(categories: dict[str, CategoryScore], category_weights: dict) -> tuple[float | None, list[str], list[str]]:
    """分野ごとの点数を重みで平均する。戻り値は (総合点, 使った分野, 欠損した分野)。
    重み0の分野は数えない。欠損した分野は除き、残りの重みで計算し直す。"""
    weighted, weight_sum, used, missing = 0.0, 0.0, [], []
    for name, weight in category_weights.items():
        if not weight or weight <= 0:
            continue
        category = categories.get(name)
        if category is None or category.score is None:
            missing.append(name)
            continue
        weighted += weight * category.score
        weight_sum += weight
        used.append(name)
    if weight_sum == 0:
        return None, used, missing
    return weighted / weight_sum, used, missing


def _reason_text(contributions: list[Contribution], positive: bool, limit: int = 3) -> str:
    picked = [c for c in contributions if (c.points > 0 if positive else c.points < 0)]
    picked.sort(key=lambda c: abs(c.points), reverse=True)
    return " / ".join(f"{c.label} {c.value_text} ({c.points:+.0f})" for c in picked[:limit])


def rank_stocks(
    universe: pd.DataFrame,
    prices: dict[str, pd.DataFrame],
    reference: pd.Timestamp,
    weights: dict,
    quality: QualityLog,
) -> tuple[pd.DataFrame, dict[str, dict[str, CategoryScore]], pd.DataFrame]:
    """全銘柄に点数を付けて並べる。

    戻り値:
      ranking:  点数順に並べた表
      details:  銘柄コード → 分野ごとの CategoryScore（レポートの根拠表示用）
      excluded: ランキングから外した銘柄と理由
    """
    min_turnover = weights["filters"]["min_turnover_oku"]
    rows, details, excluded = [], {}, []

    for item in universe.itertuples(index=False):
        code, name = item.code, item.name
        sector = getattr(item, "sector33", None)

        def exclude(reason: str) -> None:
            excluded.append({"code": code, "name": name, "reason": reason})

        df = prices.get(code)
        # 基準日より後のデータは見ない（後付けの再現で過去の日付を基準日にするときのため）
        known = df.index[df.index <= reference] if df is not None else []
        if len(known) == 0:
            exclude("株価データなし")
            continue
        if known[-1] < reference:
            exclude(f"株価が古い（最新 {known[-1].date()}、基準日 {reference.date()}）")
            continue

        ind = compute_indicators(df, as_of=reference)
        if pd.isna(ind["turnover20_oku"]):
            exclude("売買代金を計算できない（履歴不足）")
            continue
        if ind["turnover20_oku"] < min_turnover:
            exclude(f"売買代金が少ない（20日平均 {ind['turnover20_oku']:.1f}億円 < {min_turnover}億円）")
            continue

        categories = {"technical": score_technical(ind, weights["technical"])}
        total, used, missing = combine(categories, weights["category_weights"])
        if total is None:
            exclude("点数を付けられる分野がない（データ不足）")
            continue

        technical = categories["technical"]
        if technical.missing:
            quality.warn(SOURCE, code, f"計算できない指標があり点数0で扱いました: {', '.join(technical.missing)}")

        details[code] = categories
        row = {
            "code": code,
            "name": name,
            "sector33": sector,
            "total_score": round(total, 1),
            "technical_score": None if technical.score is None else round(technical.score, 1),
            "close": ind["close"],
            "day_change_pct": round(ind["day_change_pct"], 2),
            "turnover20_oku": round(ind["turnover20_oku"], 1),
            "plus_reasons": _reason_text(technical.contributions, positive=True),
            "minus_reasons": _reason_text(technical.contributions, positive=False),
            "missing": " / ".join([CATEGORY_LABELS[m] for m in missing] + technical.missing),
        }
        for key in INDICATORS:
            row[key] = None if pd.isna(ind[key]) else round(float(ind[key]), 2)
        rows.append(row)

    ranking = pd.DataFrame(rows)
    if not ranking.empty:
        ranking = ranking.sort_values("total_score", ascending=False, kind="stable").reset_index(drop=True)
        ranking.insert(0, "rank", range(1, len(ranking) + 1))
    return ranking, details, pd.DataFrame(excluded, columns=["code", "name", "reason"])
