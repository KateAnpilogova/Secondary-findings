"""
Отчет интерпретатора (.docx) -> JSON -> PDF в новом шаблоне.

Что делает:
  1. Читает docx интерпретатора: таблицу участника, таблицу вариантов,
     раздел «Интерпретация вариантов», глубину покрытия из тех. характеристик
     и (если есть) список литературы.
  2. Приводит формулировки к допустимым значениям (зиготность, тип наследования),
     раскладывает ячейку «Изменение ДНК» на позицию / HGVS c. / HGVS p.
  3. Сохраняет JSON рядом с docx (чтобы было видно, что распозналось)
     и сразу собирает PDF через render.py.
  Если что-то не распознано — PDF не собирается, выводится список ошибок
  (с флагом --gui — во всплывающем окне macOS).

Запуск:
  python docx_to_report.py путь/к/отчету.docx            # JSON и PDF рядом с docx
  python docx_to_report.py отчет.docx -o output/          # PDF в другую папку
  python docx_to_report.py *.docx --gui                   # для «Быстрого действия» Finder
"""

import argparse
import html
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

import render as R  # render.py из этой же папки

log = logging.getLogger("docx2report")


# ---------------------------------------------------------------- словари нормализации
ZYGOSITY_MAP = [            # порядок важен: «компаунд-гетерозигота» раньше «гетерозигота»
    ("компаунд", "компаунд-гетерозигота"),
    ("гетерозиг", "гетерозигота"),
    ("гомозиг", "гомозигота"),
    ("гемизиг", "гемизигота"),
    ("het", "гетерозигота"),
    ("hom", "гомозигота"),
    ("hemi", "гемизигота"),
]
INHERITANCE_MAP = [
    (r"\bАД\b|аутосомно[- ]доминант|\bAD\b", "АД"),
    (r"\bАР\b|аутосомно[- ]рецессив|\bAR\b", "АР"),
    (r"\bХД\b|\bXD\b|[ХX][- ]сцеплен\w*\s+доминант", "ХД"),
    (r"\bХР\b|\bXR\b|[ХX][- ]сцеплен\w*\s+рецессив", "ХР"),
    (r"\bХС\b|\bXL\b|[ХX][- ]сцеплен", "ХС"),
]
SEX_MAP = {"ж": "женский", "жен": "женский", "женский": "женский",
           "м": "мужской", "муж": "мужской", "мужской": "мужской"}

# Стандартные фразы, которые шаблон и так печатает сам, — из интерпретации их выкидываем.
BOILERPLATE = [
    "значимых изменений, соответствующих критериям поиска, не обнаружено",
    "оценка клинической значимости (патогенности) выявленных вариантов",
    "результаты данного исследования могут быть правильно интерпретированы",
]


# ---------------------------------------------------------------- обход docx по порядку
def iter_blocks(doc):
    """Абзацы и таблицы в том порядке, в каком они идут в документе."""
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            yield Table(child, doc)


def cell_text(cell) -> str:
    return re.sub(r"\s+", " ", cell.text).strip()


def is_heading(par: Paragraph) -> bool:
    """Заголовки разделов в отчетах интерпретаторов набраны ПРОПИСНЫМИ."""
    t = par.text.strip()
    letters = re.sub(r"[^А-Яа-яЁё]", "", t)          # заголовки русские; «BRCA1/BRCA2» — не заголовок
    return len(letters) >= 6 and letters == letters.upper()


def par_to_html(par: Paragraph) -> str:
    """Текст абзаца с сохранением курсива (названия генов) — <i>…</i>."""
    out = []
    for run in par.runs:
        t = html.escape(run.text, quote=False)
        if not t:
            continue
        out.append(f"<i>{t}</i>" if run.italic else t)
    s = "".join(out)
    s = s.replace("</i><i>", "")                 # склеить соседние курсивные куски
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- разбор ячеек
def parse_zygosity_cell(text: str) -> Tuple[Optional[str], Optional[str]]:
    low = text.lower()
    zyg = next((v for k, v in ZYGOSITY_MAP if k in low), None)
    inh = next((v for pat, v in INHERITANCE_MAP if re.search(pat, text, re.I)), None)
    return zyg, inh


def parse_dna_cell(text: str) -> Dict[str, Optional[str]]:
    pos = None
    m = re.search(r"(chr[\dXYM]+)\s*[:\-]\s*(?:g\.)?(\d+)\s*[:\-]?\s*([ACGT]+)\s*[>\-/]\s*([ACGT]+)", text, re.I)
    if m:
        pos = f"{m.group(1)}:{m.group(2)} {m.group(3).upper()}>{m.group(4).upper()}"
    m = re.search(r"((?:NM|NR|XM|ENST)_?\d+(?:\.\d+)?\s*:\s*)?c\.[^\s,;()]+", text)
    hgvs_c = re.sub(r"\s+", "", m.group(0)) if m else None
    m = re.search(r"p\.[^\s,;]+", text)
    hgvs_p = None
    if m:
        hgvs_p = m.group(0)
        if hgvs_p.count(")") > hgvs_p.count("("):      # «(p.Cys282Tyr)» -> «p.Cys282Tyr»
            hgvs_p = hgvs_p.rstrip(")")
        if hgvs_p.startswith("p.(") and hgvs_p.endswith(")"):
            hgvs_p = "p." + hgvs_p[3:-1]
    return {"position": pos, "hgvs_c": hgvs_c, "hgvs_p": hgvs_p}


def parse_disease_cell(text: str) -> Tuple[str, Optional[str]]:
    m = re.search(r"#?\s*(\d{6})", text)
    omim = m.group(1) if m else None
    disease = re.sub(r"\(?\s*(OMIM)?\s*:?\s*#?\s*\d{6}\s*\)?", "", text, flags=re.I)
    disease = re.sub(r"\bOMIM\b\s*:?", "", disease, flags=re.I)
    return disease.strip(" ,;()"), omim


def parse_frequency(text: str):
    t = text.strip().replace(" ", "")
    if not t or t in {"-", "—", "–"} or re.search(r"не|н/д|nd|n/a", t, re.I):
        return None
    t = t.replace(",", ".")
    m = re.fullmatch(r"([\d.]+)[×xX*]10\^?([−\-–]?\d+)", t)
    if m:
        x = float(m.group(1)) * 10 ** int(m.group(2).replace("−", "-").replace("–", "-"))
        return float(f"{x:.3g}")
    try:
        return float(f"{float(t):.3g}")
    except ValueError:
        return text.strip()                      # оставим как есть, шаблон напечатает строку


# ---------------------------------------------------------------- литература
def _norm_ref(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"^\d+[.)]\s*", "", s)
    s = re.sub(r"^https?://(www\.)?", "", s)
    return s.rstrip("/ .")


def map_citations(paragraphs: List[str], literature: Dict[int, str], static_refs: List[str],
                  errors: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """[12] / [12, 13] / [12–14] в тексте -> [@ref12] для доп. источников.
    Ссылки на постоянные источники (базы данных, ACMG) переводятся на их номер в study.json."""
    static_norm = {_norm_ref(r): i + 1 for i, r in enumerate(static_refs)}
    extra: Dict[str, str] = {}

    def static_number(txt: str) -> Optional[int]:
        n = _norm_ref(txt)
        if n in static_norm:
            return static_norm[n]
        if "acmg sf" in n:
            return next((i for k, i in static_norm.items() if "acmg sf" in k), None)
        return None

    def expand(group: str) -> List[int]:
        nums = []
        for part in re.split(r"[,;]\s*", group):
            m = re.fullmatch(r"\s*(\d+)\s*[–\-—]\s*(\d+)\s*", part)
            if m:
                nums.extend(range(int(m.group(1)), int(m.group(2)) + 1))
            elif part.strip().isdigit():
                nums.append(int(part))
        return nums

    def repl(m):
        nums = expand(m.group(1))
        if not nums:
            return m.group(0)
        static, keys = [], []
        for n in nums:
            if n not in literature:
                errors.append(f"в интерпретации ссылка [{n}], но в списке литературы нет пункта {n}")
                continue
            s = static_number(literature[n])
            if s is not None:
                static.append(str(s))
            else:
                key = f"ref{n}"
                extra[key] = literature[n]
                keys.append("@" + key)
        parts = []
        if static:
            parts.append(f"[{', '.join(static)}]")
        if keys:
            parts.append(f"[{'; '.join(keys)}]")
        return " ".join(parts)

    out = [re.sub(r"\[(\d+(?:\s*[,;–\-—]\s*\d+)*)\]", repl, p) for p in paragraphs]
    return out, extra


# ---------------------------------------------------------------- основной разбор
def parse_docx(path: Path, static_refs: List[str]) -> Tuple[dict, List[str]]:
    doc = Document(str(path))
    errors: List[str] = []
    patient = {"participant": None, "sex": None, "date": None, "mean_depth": None,
               "variants": [], "interpretation": []}

    section = None
    interp_pars: List[Paragraph] = []
    lit_pars: List[str] = []
    variants_table_found = False

    for block in iter_blocks(doc):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            if is_heading(block):
                low = text.lower()
                section = ("interp" if "интерпретац" in low else
                           "lit" if "литератур" in low or "источник" in low else
                           "other")
                continue
            if section == "interp":
                interp_pars.append(block)
            elif section == "lit":
                lit_pars.append(text)
            continue

        # ---- таблицы
        rows = [[cell_text(c) for c in r.cells] for r in block.rows]
        flat = " ".join(" ".join(r) for r in rows).lower()

        if "участник исследования" in flat:                        # таблица участника
            for r in rows:
                k, v = r[0].lower(), r[-1].strip()
                if "участник" in k:
                    patient["participant"] = v
                elif k.startswith("пол"):
                    patient["sex"] = SEX_MAP.get(v.lower().rstrip("."), v)
                elif "дата" in k:
                    patient["date"] = v

        elif "зиготность" in flat:                                  # таблица вариантов
            variants_table_found = True
            for i, r in enumerate(rows[1:], start=1):
                cells = list(dict.fromkeys(r)) if len(set(r)) == 1 else r   # объединенная строка
                if len(cells) == 1 or "не обнаружено" in " ".join(cells).lower():
                    continue
                if len(r) < 6:
                    errors.append(f"таблица вариантов, строка {i}: ожидалось 6 колонок, найдено {len(r)}")
                    continue
                gene, dis, dna, zyg, freq, reads = r[:6]
                disease, omim = parse_disease_cell(dis)
                dna_p = parse_dna_cell(dna)
                zygosity, inheritance = parse_zygosity_cell(zyg)
                m = re.search(r"(\d+)\s*/\s*(\d+)", reads)
                v = {
                    "gene": re.sub(r"[^\w-]", "", gene),
                    "disease": disease, "omim": omim,
                    **dna_p,
                    "classification": None,          # уточняется ниже по тексту интерпретации
                    "zygosity": zygosity, "inheritance": inheritance,
                    "frequency": parse_frequency(freq),
                    "reads_alt": int(m.group(1)) if m else None,
                    "reads_total": int(m.group(2)) if m else None,
                }
                tag = f"вариант {i} ({v['gene'] or '?'})"
                for field, src in [("position", dna), ("hgvs_c", dna), ("zygosity", zyg),
                                   ("inheritance", zyg), ("reads_alt", reads)]:
                    if not v[field]:
                        errors.append(f"{tag}: не распознано поле «{field}» в ячейке «{src}»")
                patient["variants"].append(v)

        elif "средняя глубина" in flat:                             # тех. характеристики
            for r in rows:
                if "средняя глубина" in r[0].lower():
                    val = r[-1].strip()
                    if val and val != r[0]:
                        patient["mean_depth"] = val if re.search(r"[×x]", val) else f"{val}×"

    if not variants_table_found:
        errors.append("не найдена таблица вариантов (столбец «Зиготность»)")
    for key, name in [("participant", "Участник исследования"), ("sex", "Пол"), ("date", "Дата исследования")]:
        if not patient[key]:
            errors.append(f"не найдено поле «{name}» в таблице участника")
    if not patient["mean_depth"]:
        log.warning("%s: средняя глубина прочтения не указана — в отчете будет «—»", path.name)
        patient["mean_depth"] = "—"

    # ---- интерпретация: убрать стандартные фразы, сохранить курсив
    paragraphs = [par_to_html(p) for p in interp_pars]
    paragraphs = [p for p in paragraphs
                  if p and not any(b in p.lower() for b in BOILERPLATE)]

    # ---- литература: «12. Текст» или автонумерация Word (номер = порядковый)
    literature: Dict[int, str] = {}
    for i, t in enumerate(lit_pars, start=1):
        m = re.match(r"^(\d+)[.)]\s*(.+)", t)
        literature[int(m.group(1)) if m else i] = (m.group(2) if m else t).strip()

    paragraphs, extra_refs = map_citations(paragraphs, literature, static_refs, errors)

    if patient["variants"]:
        patient["interpretation"] = paragraphs
        if extra_refs:
            patient["references"] = {k: html.escape(v, quote=False) for k, v in extra_refs.items()}
        if len(patient["variants"]) == 1:                           # классификация из текста
            low = " ".join(paragraphs).lower()
            if "вероятно патоген" in low:
                patient["variants"][0]["classification"] = "вероятно патогенный"
            elif "патоген" in low:
                patient["variants"][0]["classification"] = "патогенный"
    return patient, errors


# ---------------------------------------------------------------- запуск
def notify(title: str, message: str):
    """Всплывающее окно macOS (для запуска из Finder, где нет консоли).
    Текст передается аргументом, а не внутри скрипта, — так не нужно экранировать кавычки и кириллицу."""
    script = ['-e', 'on run argv',
              '-e', 'display dialog (item 1 of argv) with title (item 2 of argv) buttons {"OK"} default button 1',
              '-e', 'end run']
    try:
        subprocess.run(["osascript", *script, message, title], check=False)
    except FileNotFoundError:                                       # не macOS
        pass


def convert(docx_path: Path, out_dir: Optional[Path], gui: bool) -> bool:
    study = R.load_json(R.ROOT / "sups" / "jsons" / "study.json")
    gene_groups = R.load_json(R.ROOT / "sups" / "jsons" / "genes_acmg.json")
    static_refs = [r["text"] if isinstance(r, dict) else r for r in study["references"]]

    try:
        patient, errors = parse_docx(docx_path, static_refs)
    except Exception as e:                                          # битый или не тот файл
        errors, patient = [f"не удалось прочитать файл: {e}"], None

    if patient is not None:
        json_path = docx_path.with_suffix(".json")
        json_path.write_text(json.dumps(patient, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("%s -> %s", docx_path.name, json_path.name)

    if errors:
        for e in errors:
            log.error("%s: %s", docx_path.name, e)
        if gui:
            notify("Отчет НЕ собран", f"{docx_path.name}:\n\n" + "\n".join("• " + e for e in errors))
        return False

    # Дальше — та же сборка и те же проверки, что у render.py
    target = out_dir or docx_path.parent
    handler = _Collector()
    logging.getLogger("screening").addHandler(handler)
    try:
        ok = R.render(json_path, target, R.build_env(), study, gene_groups)
    except Exception as e:                                          # чтобы ошибка не терялась в Finder
        log.exception("%s: ошибка при сборке PDF", docx_path.name)
        handler.messages.append(f"ошибка при сборке PDF: {e}")
        ok = False
    logging.getLogger("screening").removeHandler(handler)
    if gui:
        if ok:
            notify("Отчет собран", f"{(target / json_path.stem).with_suffix('.pdf')}")
        else:
            notify("Отчет НЕ собран", f"{docx_path.name}:\n\n" + "\n".join("• " + m for m in handler.messages))
    return ok


class _Collector(logging.Handler):
    """Собирает ошибки render.py, чтобы показать их в окне."""
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def main():
    ap = argparse.ArgumentParser(description="docx интерпретатора -> JSON -> PDF")
    ap.add_argument("files", nargs="+", type=Path, help="отчеты интерпретаторов (.docx)")
    ap.add_argument("-o", "--out", type=Path, default=None, help="папка для PDF (по умолчанию — рядом с docx)")
    ap.add_argument("--gui", action="store_true", help="показывать результат во всплывающем окне (для Finder)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    logging.getLogger("weasyprint").setLevel(logging.ERROR)

    ok = []
    for f in args.files:
        if f.suffix.lower() != ".docx":
            msg = f"{f.name}: нужен файл .docx (из Pages: Файл → Экспортировать → Word)"
            log.error(msg)
            if args.gui:
                notify("Отчет НЕ собран", msg)
            ok.append(False)
            continue
        ok.append(convert(f.resolve(), args.out, args.gui))
    # В режиме Finder результат уже показан в окне; ненулевой код Automator
    # превратил бы в пустое окно «ошибка: «»».
    sys.exit(0 if (all(ok) or args.gui) else 1)


if __name__ == "__main__":
    main()
