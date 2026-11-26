# glava-kurs — конвейер «Глава → курс»

Глава книги по геологии → учебный слой (граф узлов) → форматы → демо-курс (офлайн HTML) + ролики.
Хакатон KMG Digital.

## Установка

```powershell
uv sync
copy .env.example .env    # или ввести ключ и модель в приложении (⚙️)
uv run g2c llm-ping       # проверка ключа и модели
```

## Материалы организаторов

Материалы организаторов в репозитории не публикуются (по ТЗ: главы и ролики не публикуются, до хакатона материалы не раскрываются). У команды они лежат локально, раскладка такая:

| Папка | Что |
|---|---|
| `inputs/books_md/` | md книг (`train_*.md`, `ctrl_*.md`) с метками `<!-- page N -->` |
| `inputs/books_pdf/` | PDF книг — из них рендерятся сканы страниц |
| `inputs/analysis/` | анализы книг |
| `inputs/review/ctrl_vendelshtein/` | контрольный пакет модуля проверки (ключ не открывать до прогона) |
| `data/raw/` | архивы Volve и F3 из пакета вводных |
| `schemas/` | JSON-схемы, пример слоя, `score_review.py` |
| `templates/` | шаблоны сдачи (журнал правок, метрики, паспорт) |
| `domain/geology/` | заготовки доменного пакета: `styleguide.json`, `legend.html`, `misconceptions.json`, `physics_checklist.json`, `pronunciation.md` |
| `docs/` | ТЗ, задание, инструменты для видео, разборы роликов |

Без этих файлов тесты, которым они нужны, пропускаются (`pytest` покажет `skipped`), остальное работает. Глоссарий, слой, курсы и ролики (`work/`, `out/`) содержат цитаты из книг и тоже не коммитятся.

## Команды

```powershell
uv run g2c chapters                       # главы из chapters.json
uv run g2c toc train_latyshova.md         # оглавление книги со страницами PDF
uv run g2c ingest latyshova_gl5_par17-20 --scans   # глава → единицы + сканы
uv run g2c check-layer latyshova_gl5_par17-20      # проверка слоя: схема, цитаты, числа, термины, связи
uv run g2c check-schema schemas/example_layer.json node
uv run g2c journal <глава> <файл> "что изменено" <минуты> --reason "почему"
uv run g2c metrics                        # metrics.csv из usage.jsonl и manual_edits.csv
uv run pytest -q                          # тесты ядра (без обращения к модели)
```

## Что уже готово

- Реестр глав `chapters.json`: 3 обучающие и 3 контрольные главы, точные границы по заголовкам, страницы короткого чтения.
- `g2c ingest`: глава → единицы `p095.03` по типам `coverage.schema.json`; описания рисунков, сделанные моделью при извлечении,
  хранятся отдельно от подписи автора (`model_description`); задачи книги с номерами; флаг брака распознавания `ocr_suspect`.
- Проверки по правилам организаторов: JSON-схемы, цитата на странице (без учёта пробелов, переносов, регистра),
  числа, порядок терминов, связи, покрытие («непоказуемо» — не причина).
- Адаптер LLM: роль → модель (`config.yaml`, `LLM_MODEL` в `.env`), кэш ответов, повторы при 503, учёт токенов в `work/usage.jsonl`.
- Журнал ручных правок и метрики в форматах шаблонов организаторов.

## Структура

```
chapters.json  config.yaml  MANIFEST.md  manual_edits.csv  metrics.csv
g2c/        код конвейера (cli, paths, mdbook, ingest, validate, llm, scans, metrics)
schemas/    схемы и пример слоя организаторов, score_review.py
domain/     доменные пакеты (geology, drilling) — движок не знает геологии
templates/  шаблоны сдачи организаторов
docs/       ТЗ, инструкция, требования, подача доменов, разборы роликов, открытые данные
inputs/ data/raw/   материалы организаторов (не в git)
work/       промежуточные результаты по главам (не в git)
out/        готовые курсы
```
