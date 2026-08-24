#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TerrySP / Altema 配合データ 最終抽出ツール

前提
----
v28改2 の実行結果 ZIP（altema-breeding-result.zip）を入力し、
これまでの人手確認結果を反映した「最終配合CSV」を作成します。

反映方針
--------
1. v28改2 の正式マスター 826件を基準にする。
2. v28 false-positive review の18件は、スクリーンショット確認済みの
   正常な「同一親×同一親」配合として、そのまま KEEP する。
   （これらは826件に既に含まれているため、追加処理はしない。）
3. v27 semantic review 8件のうち、図鑑名との照合で解決した
   「空白入りモンスター名」3レシピを正式名へ正規化して復帰する。
4. 「○○など」5件は代表表記であり、完全な親指定ではないため復帰しない。
5. v27 semantic rejected 49件（PLACEHOLDER / 崩れたOR式）は復帰しない。
6. v28改2 semantic rejected 19件（人手確認済み偽陽性）は復帰しない。

期待最終件数
------------
v28改2正式マスター826件を基準に、既に重複済みの空白名3件は件数増なし。
さらに既存正式レシピと重なる表記ゆれを統合し、
「メタルカイザー × 2」の分解残骸1件を除外して 818件。

使い方
------
  python final_extract_altema_breeding.py

または入力ZIPを指定:
  python final_extract_altema_breeding.py /path/to/altema-breeding-result.zip

出力
----
final_breeding/
  breeding.csv                       # アプリ投入用コア列
  altema_breeding_final_full.csv     # 監査情報付き完全版
  final_extract_summary.csv          # 件数・検証結果
  final_restored_from_v27_review.csv # 復帰した3件
  final_excluded_v27_review.csv      # 「○○など」等、復帰しなかった行
  final_v28_review_kept.csv           # KEEP確認した18件
final_breeding_result.zip
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from pathlib import Path
from typing import Iterable


BASE_MASTER_NAME = "altema_individual_breeding_659.csv"
V27_REVIEW_NAME = "altema_breeding_v27_semantic_review.csv"
V28_REVIEW_NAME = "altema_breeding_v28_false_positive_review.csv"
V27_REJECT_NAME = "altema_breeding_v27_semantic_rejected.csv"
V28K2_REJECT_NAME = "altema_breeding_v28k2_semantic_rejected.csv"
V28K2_SUMMARY_NAME = "altema_breeding_v28k2_cleanup_summary.csv"

EXPECTED_BASE_COUNT = 826
EXPECTED_RESTORED_COUNT = 3
EXPECTED_FINAL_COUNT = 818
EXPECTED_V28_REVIEW_KEEP_COUNT = 18
EXPECTED_V28K2_REJECT_COUNT = 19
EXPECTED_V27_REJECT_COUNT = 49

# ユーザー確認済み: アルテマ表示上の空白を除けば図鑑正式名と一致。
CONFIRMED_SPACED_NAME_MAP = {
    "ワンダー エッグ": (354, "ワンダーエッグ"),
    "エビル スピリッツ": (352, "エビルスピリッツ"),
    "ダーク スライム": (337, "ダークスライム"),
    "スライム ナイト": (163, "スライムナイト"),
    "スライム マデュラ": (538, "スライムマデュラ"),
}

# 人手確認済みの正常な同一親×同一親18件。
# v28監査CSVがこの集合と一致することを最終チェックする。

# ユーザー確認済みの表記ゆれ / 図鑑No対応。
CONFIRMED_RULE_MONSTER_MAP = {
    "魔王オルゴデミーラ": (624, "魔王オルゴ・デミーラ"),
    "地獄のマドンナ": (455, "地獄のマドンナ"),
    "キャプテンクロウ": (557, "キャプテンクロウ"),
    "オルゴ・デミーラ": (489, "オルゴ・デミーラ"),
    "エッグラ&チキーラ": (568, "エッグラ&チキーラ"),
    "ボル": (416, "ボル"),
    "ブル": (417, "ブル"),
    "バル": (418, "バル"),
    "ベル": (419, "ベル"),
}

RULE_ANY_ALIASES = {"誰でも", "相手を問わない", "相手は問わず"}

CONFIRMED_SAME_PARENT_KEEP = {
    (104, "ドラゴン", "ドラゴンキッズ"),
    (115, "おどるほうせき", "わらいぶくろ"),
    (279, "キングスライム", "もりもりスライム"),
    (296, "エビラ", "ぐんたいガニ"),
    (319, "はぐれメタル", "メタルスライム"),
    (366, "ダンジョンえび", "エビラ"),
    (378, "マンドラゴラ", "ダンスキャロット"),
    (384, "デスゴーゴン", "アイアンブルドー"),
    (387, "スライムベホマズン", "キングスライム"),
    (398, "マンイーター", "ひとくいそう"),
    (400, "ブオーン", "プオーン"),
    (404, "ギガミュータント", "ガマキャノン"),
    (428, "がいこつけんし", "しりょうのきし"),
    (462, "ナイトキング", "ナイトリッチ"),
    (465, "スライムジェネラル", "メタルカイザー"),
    (529, "死神の騎士", "ピサロナイト"),
    (547, "ヘルバトラー", "アンクルホーン"),
    (623, "サージタウス", "キラーマジンガ"),
}

CORE_FIELDS = [
    "result_no", "result_name", "result_rank", "result_family", "result_size",
    "recipe_type",
    "parent1_type", "parent1_no", "parent1",
    "parent2_type", "parent2_no", "parent2",
    "parent3_type", "parent3_no", "parent3",
    "parent4_type", "parent4_no", "parent4",
]

FULL_EXTRA_FIELDS = [
    "confidence", "note", "source", "source_url", "source_page",
    "signature", "raw_text",
    "v26_source_count", "v26_source_url_count", "v26_all_source_urls",
    "v26_all_source_methods", "v26_all_source_contexts",
    "final_action", "final_reason",
]


def clean(v) -> str:
    return str(v or "").replace("\u3000", " ").strip()


def read_csv_bytes(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def write_csv(path: Path, rows: Iterable[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def locate_input_zip() -> Path:
    if len(sys.argv) >= 2:
        p = Path(sys.argv[1]).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"入力ZIPが見つかりません: {p}")
        return p

    candidates = [
        Path.cwd() / "altema-breeding-result.zip",
        Path(__file__).resolve().parent / "altema-breeding-result.zip",
    ]
    for p in candidates:
        if p.exists():
            return p.resolve()
    raise FileNotFoundError(
        "altema-breeding-result.zip が見つかりません。"
        "このpyと同じフォルダへ置くか、引数でZIPを指定してください。"
    )


def flatten_zip_entries(zip_path: Path) -> dict[str, bytes]:
    """
    外側ZIPの中に output_breeding.zip が1段入っていても、
    直接CSVが入っていても読めるように全CSVを名前で収集する。
    """
    found: dict[str, bytes] = {}

    def visit_zip(raw_zip: bytes, depth: int) -> None:
        if depth > 3:
            return
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as z:
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                data = z.read(name)
                base = Path(name).name
                if base.lower().endswith(".csv"):
                    found[base] = data
                elif base.lower().endswith(".zip"):
                    try:
                        visit_zip(data, depth + 1)
                    except zipfile.BadZipFile:
                        pass

    with zip_path.open("rb") as f:
        outer = f.read()
    visit_zip(outer, 0)
    return found


def require(entries: dict[str, bytes], name: str) -> bytes:
    if name not in entries:
        raise RuntimeError(f"必要なCSVがZIP内にありません: {name}")
    return entries[name]


def make_signature(r: dict) -> str:
    parts = [clean(r.get("result_no")), clean(r.get("recipe_type"))]
    for i in range(1, 5):
        parts.extend([
            clean(r.get(f"parent{i}_type")),
            clean(r.get(f"parent{i}_no")),
            clean(r.get(f"parent{i}")),
        ])
    return "|".join(parts)


def recipe_key(r: dict) -> tuple:
    """最終重複検出用。2体配合は親順不同、4体配合は原順序保持。"""
    result_no = int(clean(r.get("result_no")) or 0)
    typ = clean(r.get("recipe_type"))
    parents = []
    for i in range(1, 5):
        p = (
            clean(r.get(f"parent{i}_type")),
            clean(r.get(f"parent{i}_no")),
            clean(r.get(f"parent{i}")),
        )
        if any(p):
            parents.append(p)
    if typ == "2体配合":
        parents = sorted(parents)
    return (result_no, typ, tuple(parents))


def normalize_spaced_review_row(src: dict) -> dict | None:
    r = dict(src)
    changed = False

    for i in range(1, 5):
        val = clean(r.get(f"parent{i}"))
        if not val:
            continue
        mapped = CONFIRMED_SPACED_NAME_MAP.get(val)
        if mapped is None:
            continue
        no, canonical = mapped
        r[f"parent{i}_type"] = "monster"
        r[f"parent{i}_no"] = str(no)
        r[f"parent{i}"] = canonical
        changed = True

    # review行に空白名以外の未解決ruleが残るなら復帰しない。
    unresolved = []
    for i in range(1, 5):
        typ = clean(r.get(f"parent{i}_type"))
        val = clean(r.get(f"parent{i}"))
        if not val:
            continue
        if typ == "rule" and val != "相手問わず":
            unresolved.append((i, val))

    if not changed or unresolved:
        return None

    r["signature"] = make_signature(r)
    r["final_action"] = "RESTORE"
    r["final_reason"] = "確認済み空白入り図鑑名を正式モンスター名へ正規化"
    return r


def same_parent_keep_key(r: dict) -> tuple[int, str, str] | None:
    try:
        no = int(clean(r.get("result_no")))
    except ValueError:
        return None
    p1 = clean(r.get("parent1"))
    p2 = clean(r.get("parent2"))
    if p1 and p1 == p2:
        return (no, clean(r.get("result_name")), p1)
    return None


def normalize_final_rules(rows: list[dict]):
    """
    最終段階で、人手確認済みの rule 表記をだけを安全に正規化する。
    - 既知モンスター名/表記ゆれ -> monster + No
    - 誰でも/相手を問わない -> 相手問わず
    - スライムジェネラルの単独「2」 -> メタルカイザー×2の分解残骸として除外
    """
    out, rejected = [], []
    for src in rows:
        r = dict(src)
        changed = []
        reject_reason = ""

        for i in range(1, 5):
            typ = clean(r.get(f"parent{i}_type"))
            val = clean(r.get(f"parent{i}"))
            if typ != "rule" or not val:
                continue

            if val in RULE_ANY_ALIASES:
                r[f"parent{i}"] = "相手問わず"
                changed.append(f"parent{i}:{val}->相手問わず")
                continue

            if val in CONFIRMED_RULE_MONSTER_MAP:
                no, canonical = CONFIRMED_RULE_MONSTER_MAP[val]
                r[f"parent{i}_type"] = "monster"
                r[f"parent{i}_no"] = str(no)
                r[f"parent{i}"] = canonical
                changed.append(f"parent{i}:{val}->No.{no} {canonical}")
                continue

            if val == "2" and clean(r.get("result_name")) == "スライムジェネラル":
                reject_reason = "メタルカイザー×2 の分解で単独『2』が残った重複観測"

        if reject_reason:
            r["final_action"] = "REJECT"
            r["final_reason"] = reject_reason
            rejected.append(r)
            continue

        r["signature"] = make_signature(r)
        if changed:
            r["final_reason"] = "v28改2正式マスター / 最終正規化: " + " ; ".join(changed)
        out.append(r)

    return out, rejected


def main() -> None:
    input_zip = locate_input_zip()
    entries = flatten_zip_entries(input_zip)

    base_rows = read_csv_bytes(require(entries, BASE_MASTER_NAME))
    base_count_before_final_normalization = len(base_rows)
    v27_review = read_csv_bytes(require(entries, V27_REVIEW_NAME))
    v28_review = read_csv_bytes(require(entries, V28_REVIEW_NAME))
    v27_rejected = read_csv_bytes(require(entries, V27_REJECT_NAME))
    v28k2_rejected = read_csv_bytes(require(entries, V28K2_REJECT_NAME))
    v28k2_summary = read_csv_bytes(require(entries, V28K2_SUMMARY_NAME))

    errors: list[str] = []
    warnings: list[str] = []

    # ---- 基準件数の固定 ----
    if len(base_rows) != EXPECTED_BASE_COUNT:
        errors.append(
            f"基準マスター件数が想定外: {len(base_rows)} (期待 {EXPECTED_BASE_COUNT})"
        )

    if len(v27_rejected) != EXPECTED_V27_REJECT_COUNT:
        warnings.append(
            f"v27 rejected件数が想定外: {len(v27_rejected)} (期待 {EXPECTED_V27_REJECT_COUNT})"
        )

    if len(v28k2_rejected) != EXPECTED_V28K2_REJECT_COUNT:
        errors.append(
            f"v28改2 rejected件数が想定外: {len(v28k2_rejected)} (期待 {EXPECTED_V28K2_REJECT_COUNT})"
        )

    if v28k2_summary:
        reported = clean(v28k2_summary[0].get("v28k2_final_master_count"))
        if reported and int(reported) != len(base_rows):
            errors.append(
                f"v28改2 summaryとmaster件数が不一致: summary={reported}, master={len(base_rows)}"
            )

    # ---- v28監査18件は全件KEEP確認 ----
    review_keep_keys = {same_parent_keep_key(r) for r in v28_review}
    review_keep_keys.discard(None)
    if review_keep_keys != CONFIRMED_SAME_PARENT_KEEP:
        missing = CONFIRMED_SAME_PARENT_KEEP - review_keep_keys
        extra = review_keep_keys - CONFIRMED_SAME_PARENT_KEEP
        if missing:
            errors.append("v28 KEEP確認18件の不足: " + ", ".join(map(str, sorted(missing))))
        if extra:
            errors.append("v28 reviewに未確認の追加候補: " + ", ".join(map(str, sorted(extra))))

    # 18件が基準マスターに実際に存在することも確認。
    base_same = {same_parent_keep_key(r) for r in base_rows}
    base_same.discard(None)
    missing_from_base = CONFIRMED_SAME_PARENT_KEEP - base_same
    if missing_from_base:
        errors.append(
            "KEEP確認済み同一親配合が基準マスターに存在しません: "
            + ", ".join(map(str, sorted(missing_from_base)))
        )

    # ---- v27 review から空白名だけ復帰 ----
    restored = []
    excluded_review = []
    for r in v27_review:
        x = normalize_spaced_review_row(r)
        if x is not None:
            restored.append(x)
        else:
            y = dict(r)
            y["final_action"] = "EXCLUDE"
            if "ETC_EXPRESSION" in clean(r.get("v27_reason")):
                y["final_reason"] = "『○○など』は代表表記のため完全レシピとして採用しない"
            else:
                y["final_reason"] = "最終確定条件に該当しないv27 REVIEW"
            excluded_review.append(y)

    if len(restored) != EXPECTED_RESTORED_COUNT:
        errors.append(
            f"空白名正規化で復帰した件数が想定外: {len(restored)} (期待 {EXPECTED_RESTORED_COUNT})"
        )

    # ---- 最終rule正規化 ----
    base_rows, final_rule_rejected = normalize_final_rules(base_rows)

    # ---- 最終マージ ----
    final_rows = []
    seen = set()

    for src in base_rows:
        r = dict(src)
        r["final_action"] = "KEEP"
        r["final_reason"] = "v28改2正式マスター"
        k = recipe_key(r)
        if k in seen:
            continue
        seen.add(k)
        final_rows.append(r)

    for r in restored:
        k = recipe_key(r)
        if k in seen:
            warnings.append(
                f"復帰候補が既存マスターと重複したため追加せず: {r.get('result_name')} {r.get('parent1')}×{r.get('parent2')}"
            )
            continue
        seen.add(k)
        final_rows.append(r)

    final_rows.sort(key=lambda r: (
        int(clean(r.get("result_no")) or 9999),
        clean(r.get("recipe_type")),
        clean(r.get("parent1")), clean(r.get("parent2")),
        clean(r.get("parent3")), clean(r.get("parent4")),
    ))

    if len(final_rows) != EXPECTED_FINAL_COUNT:
        errors.append(
            f"最終件数が想定外: {len(final_rows)} (期待 {EXPECTED_FINAL_COUNT})"
        )

    # 最終行の最低限整合性
    for r in final_rows:
        for i in range(1, 5):
            typ = clean(r.get(f"parent{i}_type"))
            name = clean(r.get(f"parent{i}"))
            no = clean(r.get(f"parent{i}_no"))
            if typ == "monster" and name and not no:
                errors.append(
                    f"monster親なのにNoなし: {r.get('result_name')} parent{i}={name}"
                )
            if typ == "rule" and name not in ("", "相手問わず", "神獣", "神獣モンスター"):
                errors.append(
                    f"未解決ruleが最終データに残っています: {r.get('result_name')} parent{i}={name}"
                )

    if errors:
        print("===== FINAL EXTRACTION FAILED =====")
        for e in errors:
            print("ERROR:", e)
        for w in warnings:
            print("WARNING:", w)
        raise SystemExit(1)

    # ---- 出力 ----
    out_dir = Path.cwd() / "final_breeding"
    out_dir.mkdir(parents=True, exist_ok=True)

    full_fields = []
    for f in CORE_FIELDS + FULL_EXTRA_FIELDS:
        if f not in full_fields:
            full_fields.append(f)

    # アプリ投入用は余計な監査列を外す。
    write_csv(out_dir / "breeding.csv", final_rows, CORE_FIELDS)
    write_csv(out_dir / "altema_breeding_final_full.csv", final_rows, full_fields)

    review_fields = []
    for rows in (restored, excluded_review, v28_review):
        for r in rows:
            for k in r.keys():
                if k not in review_fields:
                    review_fields.append(k)
    # 空リスト時にも安定する固定fallback
    if not review_fields:
        review_fields = CORE_FIELDS + ["final_action", "final_reason"]

    write_csv(out_dir / "final_restored_from_v27_review.csv", restored, review_fields)
    write_csv(out_dir / "final_excluded_v27_review.csv", excluded_review, review_fields)

    v28_keep_rows = []
    for r in v28_review:
        x = dict(r)
        x["final_action"] = "KEEP"
        x["final_reason"] = "スクリーンショット確認済み正常な同一親×同一親"
        v28_keep_rows.append(x)
    write_csv(out_dir / "final_v28_review_kept.csv", v28_keep_rows, review_fields)
    write_csv(out_dir / "final_rule_normalization_rejected.csv", final_rule_rejected, review_fields)

    summary_rows = [
        {"item": "input_zip", "value": str(input_zip)},
        {"item": "v28k2_base_master", "value": str(base_count_before_final_normalization)},
        {"item": "v27_review_total", "value": str(len(v27_review))},
        {"item": "v27_review_restored_spaced_names", "value": str(len(restored))},
        {"item": "v27_review_excluded_etc_or_other", "value": str(len(excluded_review))},
        {"item": "v27_rejected_kept_excluded", "value": str(len(v27_rejected))},
        {"item": "v28k2_false_positive_rejected_kept_excluded", "value": str(len(v28k2_rejected))},
        {"item": "v28_same_parent_review_confirmed_keep", "value": str(len(v28_keep_rows))},
        {"item": "final_rule_fragment_rejected", "value": str(len(final_rule_rejected))},
        {"item": "final_recipe_count", "value": str(len(final_rows))},
        {"item": "warnings", "value": str(len(warnings))},
        {"item": "status", "value": "OK"},
    ]
    write_csv(out_dir / "final_extract_summary.csv", summary_rows, ["item", "value"])

    # ZIP化
    zip_out = Path.cwd() / "final_breeding_result.zip"
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out_dir.iterdir()):
            if p.is_file():
                z.write(p, arcname=f"final_breeding/{p.name}")

    print("===== FINAL EXTRACTION OK =====")
    print(f"v28改2基準: {base_count_before_final_normalization}")
    print(f"空白名正規化で復帰: {len(restored)}")
    print(f"『○○など』等を除外: {len(excluded_review)}")
    print(f"同一親×同一親 KEEP確認: {len(v28_keep_rows)}")
    print(f"最終rule分解残骸除外: {len(final_rule_rejected)}")
    print(f"最終配合件数: {len(final_rows)}")
    if warnings:
        for w in warnings:
            print("WARNING:", w)
    print(f"アプリ投入用: {out_dir / 'breeding.csv'}")
    print(f"完全版: {out_dir / 'altema_breeding_final_full.csv'}")
    print(f"ZIP: {zip_out}")


if __name__ == "__main__":
    main()
