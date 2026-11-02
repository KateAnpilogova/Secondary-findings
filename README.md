

1. Поставить системные библиотеки WeasyPrint (Pango и HarfBuzz). Через pip они не ставятся.
bash
   macOS
   brew install pango
   Ubuntu / Debian
   sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b
2. Склонировать репозиторий и перейти в папку:
bash
   git clone <адрес репозитория>
   cd secondary_findings
3. Создать виртуальное окружение. Нужен Python 3.10 или новее, отчёты проверены на 3.12.
bash
   python3 -m venv .venv
   source .venv/bin/activate
4. Поставить зависимости:
bash
   pip install -r requirements.txt
*Только на Mac с чипом M1/M2/M3:
bash
   export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib

Иначе WeasyPrint не найдёт Pango. Чтобы не вводить это каждый раз, строку можно добавить в ~/.zshrc. На Linux этот шаг не нужен.

Пробный запуск на примерах:
bash
   python render.py sups/jsons/patient_neg.json sups/jsons/patient_pos.json

В папке output/ должны появиться patient_neg.pdf (без находок) и patient_pos.pdf (с находкой). Папка создаётся сама; другую можно указать через -o.

Свой отчёт: сделать JSON участника по образцу patient_pos.json и запустить:
bash
   python render.py путь/к/участник.json -o output/

Как понять, что всё в порядке

В консоли на каждый отчёт строка INFO: … -> output/….pdf.
ERROR: Failed to load stylesheet значит, что скрипт запущен не из той структуры папок или в репозитории не хватает styles/.
cannot load library 'libgobject-2.0-0' значит, что не установлен Pango (шаг 1) или на Mac пропущен шаг 5.
Если в JSON ошибка, PDF не создаётся, а в консоли перечисляются причины.