#!/bin/bash
# Скрипт для «Быстрого действия» Finder: правый клик по .docx -> «Собрать отчет».
# Automator передает выбранные файлы как аргументы.

# 1) Python, в котором у вас уже работает render.py (узнать: в терминале `which python`).
#    В нем должны стоять weasyprint, jinja2 и python-docx (`pip install python-docx`).
PYTHON="/opt/anaconda3/bin/python"

# 2) Finder запускает скрипты без вашего обычного окружения терминала, поэтому
#    библиотеки Homebrew (pango и т.п., нужны WeasyPrint) указываем явно.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib:$DYLD_FALLBACK_LIBRARY_PATH"

# Файлы приходят либо аргументами, либо (если в Automator выбрано «Передать ввод: на stdin»)
# по одному пути на строку — второй вариант не требует кавычек в Automator.
if [ "$#" -eq 0 ]; then
  files=()
  # «|| [ -n "$line" ]» — чтобы не потерять последнюю строку без перевода строки
  while IFS= read -r line || [ -n "$line" ]; do [ -n "$line" ] && files+=("$line"); done
  set -- "${files[@]}"
fi

# Запуск без файлов (например, кнопкой «Запустить» внутри Automator) — не ошибка, просто подсказка.
if [ "$#" -eq 0 ]; then
  osascript -e 'display dialog "Файл не передан. Выделите .docx в Finder и выберите: правый клик → Быстрые действия → Собрать отчет." with title "Собрать отчет" buttons {"OK"} default button 1'
  exit 0
fi

cd "$(dirname "$0")" || exit 1
mkdir -p output
echo "--- $(date '+%F %T') получено файлов: $#: $*" >> output/finder_action.log
"$PYTHON" docx_to_report.py --gui "$@" >> output/finder_action.log 2>&1
rc=$?
# Ненулевой код здесь означает аварийное падение Python (ожидаемые ошибки он показывает сам).
if [ "$rc" -ne 0 ]; then
  osascript -e 'display dialog "Скрипт завершился с ошибкой. Подробности в файле output/finder_action.log в папке проекта." with title "Собрать отчет" buttons {"OK"} default button 1'
fi
exit 0
