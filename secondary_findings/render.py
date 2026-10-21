"""
Сборка отчета «Профилактический ДНК-скрининг» (HTML -> PDF через WeasyPrint).

Что откуда берется:
  sups/jsons/study.json       — постоянные параметры исследования (тех. характеристики,
                                стандартные рекомендации, список литературы);
  sups/jsons/genes_acmg.json  — группы генов ACMG с текстами (стр. «Список генов»);
  sups/jsons/<patient>.json   — то, что вносит специалист: данные участника,
                                таблица вариантов, интерпретация, рекомендации.

Дополнительная литература (функциональные исследования и т.п.):
  в тексте интерпретации пишем [@key] (или [@key1; @key2]), а в patient.json
  добавляем "references": {"key": "Полное описание источника"}.
  Код пронумерует эти источники после постоянного списка из study.json
  и допишет их в конец списка литературы.

Все относительные пути в шаблонах (стили, картинки) считаются от папки,
где лежит render.py (base_url=ROOT), а НЕ от папки templates/.

Запуск:
  python render.py sups/jsons/patient_neg.json
  python render.py sups/jsons/*.json -o output/
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from weasyprint import HTML

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("screening")

# Допустимые значения для полей, которые заполняют люди.
# Всё, что не из списка, — ошибка: опечатка в зиготности в клиническом отчете недопустима.
ZYGOSITY = {"гетерозигота", "гомозигота", "гемизигота", "компаунд-гетерозигота"}
INHERITANCE = {
    "АД": "аутосомно-доминантный",
    "АР": "аутосомно-рецессивный",
    "ХД": "Х-сцепленный доминантный",
    "ХР": "Х-сцепленный рецессивный",
    "ХС": "Х-сцепленный",
}
CLASSIFICATION = {"патогенный", "вероятно патогенный"}
REQUIRED_PATIENT = ["participant", "sex", "date", "mean_depth", "variants"]
REQUIRED_VARIANT = ["gene", "disease", "position", "hgvs_c", "zygosity",
                    "inheritance", "reads_alt", "reads_total"]


# ---------------------------------------------------------------- данные
def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate(patient: dict, acmg_genes: set, source: str) -> list[str]:
    """Возвращает список ошибок. Предупреждения пишет в лог сам."""
    errors = []
    for key in REQUIRED_PATIENT:
        if key not in patient or patient[key] in ("", None):
            errors.append(f"нет поля «{key}»")

    for i, v in enumerate(patient.get("variants", []), start=1):
        tag = f"вариант {i} ({v.get('gene', '?')})"
        for key in REQUIRED_VARIANT:
            if v.get(key) in ("", None):
                errors.append(f"{tag}: нет поля «{key}»")
        if v.get("gene") and v["gene"] not in acmg_genes:
            log.warning("%s: %s: ген %s не входит в список ACMG", source, tag, v["gene"])
        if v.get("zygosity") and v["zygosity"] not in ZYGOSITY:
            errors.append(f"{tag}: зиготность «{v['zygosity']}» не из списка {sorted(ZYGOSITY)}")
        if v.get("inheritance") and v["inheritance"] not in INHERITANCE:
            errors.append(f"{tag}: тип наследования «{v['inheritance']}» не из списка {list(INHERITANCE)}")
        if v.get("classification") and v["classification"] not in CLASSIFICATION:
            errors.append(f"{tag}: классификация «{v['classification']}» не из списка {sorted(CLASSIFICATION)}")
        try:
            if int(v["reads_alt"]) > int(v["reads_total"]):
                errors.append(f"{tag}: альт. прочтений больше, чем общих")
        except (KeyError, TypeError, ValueError):
            errors.append(f"{tag}: число прочтений должно быть целым")

    # Если варианты есть, без интерпретации отчет выпускать нельзя.
    if patient.get("variants") and not patient.get("interpretation"):
        errors.append("есть варианты, но пустое поле «interpretation»")
    if not patient.get("variants") and patient.get("interpretation"):
        log.warning("%s: вариантов нет, текст «interpretation» будет проигнорирован", source)
    return errors


# ---------------------------------------------------------------- фильтры
def fmt_freq(value) -> str:
    """0.0384 -> «0,0384»; 3.2e-05 -> «3,2×10⁻⁵»; None -> «—»; строка — как есть."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        return value
    if value == 0:
        return "0"
    if value >= 1e-4:
        return f"{value:.4g}".replace(".", ",")
    mant, exp = f"{value:.1e}".split("e")
    sup = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")
    return f"{mant.replace('.', ',')}×10{str(int(exp)).translate(sup)}"


def inheritance_legend(variants: list) -> str:
    used = [k for k in INHERITANCE if any(v.get("inheritance") == k for v in variants)]
    return "; ".join(f"{k} – {INHERITANCE[k]}" for k in used) + "." if used else ""


# ---------------------------------------------------------------- литература
# Только ДОПОЛНИТЕЛЬНЫЕ источники, которые интерпретатор приводит в подтверждение
# (функциональные исследования, описания случаев и т.п.).
# В тексте: [@key] или [@key1; @key2]; в patient.json: "references": {"key": "текст"}.
# Нумерация продолжает постоянный список из study.json (12, 13, ...) по порядку
# первого упоминания; [@key] заменяется на [12] / [12, 14] / [12–14].
CITE_RE = re.compile(r"\[@([^\]]+)\]")


def _compress(nums: list) -> str:
    """[12, 13, 14, 16] -> «12–14, 16»."""
    nums = sorted(set(nums))
    parts, start = [], nums[0]
    for prev, cur in zip(nums, nums[1:] + [None]):
        if cur != prev + 1:
            parts.append(str(start) if start == prev else
                         f"{start}, {prev}" if prev == start + 1 else f"{start}–{prev}")
            start = cur
    return ", ".join(parts)


def resolve_citations(texts: list, first_number: int, extra_refs: dict, source: str):
    """Возвращает (тексты с номерами, список доп. источников по порядку, ошибки)."""
    errors, numbers, extra_list = [], {}, []

    def replace(match):
        nums = []
        for k in (k.strip().lstrip("@").strip() for k in match.group(1).split(";")):
            if k not in extra_refs:
                errors.append(f"ссылка [@{k}] есть в тексте, но нет в «references»")
                continue
            if k not in numbers:
                extra_list.append(extra_refs[k])
                numbers[k] = first_number + len(extra_list) - 1
            nums.append(numbers[k])
        return f"[{_compress(nums)}]" if nums else match.group(0)

    new_texts = [CITE_RE.sub(replace, t) for t in texts]
    for k in extra_refs:
        if k not in numbers:
            log.warning("%s: источник «%s» нигде не процитирован — в список не попадет", source, k)
    return new_texts, extra_list, errors


# ---------------------------------------------------------------- сборка
def build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,      # забытая переменная = ошибка, а не пустое место
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters["freq"] = fmt_freq
    return env


def render(patient_path: Path, out_dir: Path, env, study, gene_groups, html_too=False) -> bool:
    patient = load_json(patient_path)
    acmg_genes = sorted({g for grp in gene_groups for g in grp["genes"]})

    errors = validate(patient, set(acmg_genes), patient_path.name)
    if errors:
        for e in errors:
            log.error("%s: %s", patient_path.name, e)
        log.error("%s: отчет НЕ собран", patient_path.name)
        return False

    patient.setdefault("interpretation", [])
    recommendations = patient.get("recommendations") or study["default_recommendations"]
    # Вариантов нет -> интерпретация не печатается, значит и ее ссылки не нужны.
    interp = patient["interpretation"] if patient["variants"] else []
    texts, extra_refs, cite_errors = resolve_citations(
        interp + recommendations, len(study["references"]) + 1,
        patient.get("references", {}) if patient["variants"] else {},
        patient_path.name)
    if cite_errors:
        for e in cite_errors:
            log.error("%s: %s", patient_path.name, e)
        log.error("%s: отчет НЕ собран", patient_path.name)
        return False
    patient["interpretation"] = texts[:len(interp)]
    recommendations = texts[len(interp):]

    context = {
        "patient": patient,
        "study": study,
        "gene_groups": gene_groups,
        "acmg_genes": acmg_genes,
        "recommendations": recommendations,
        "references": study["references"] + extra_refs,   # постоянные + дополнительные из интерпретации
        "inheritance_legend": inheritance_legend(patient["variants"]),
    }
    html = env.get_template("whole.html").render(**context)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / f"{patient_path.stem}.pdf"
    if html_too:                          # удобно для отладки верстки в браузере
        (out_dir / f"{patient_path.stem}.html").write_text(html, encoding="utf-8")
    HTML(string=html, base_url=str(ROOT)).write_pdf(str(out_pdf))
    log.info("%s -> %s", patient_path.name, out_pdf)
    return True


def main():
    ap = argparse.ArgumentParser(description="Отчет «Профилактический ДНК-скрининг»")
    ap.add_argument("patients", nargs="+", type=Path, help="JSON-файлы участников")
    ap.add_argument("-o", "--out", type=Path, default=ROOT / "output", help="папка для PDF")
    ap.add_argument("--html", action="store_true", help="сохранить также промежуточный HTML")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    logging.getLogger("weasyprint").setLevel(logging.ERROR)

    env = build_env()
    study = load_json(ROOT/'sups'/'jsons'/'study.json')
    gene_groups = load_json(ROOT / 'sups'/'jsons'/'genes_acmg.json')

    ok = [render(p, args.out, env, study, gene_groups, args.html) for p in args.patients]
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
