# Аналитика токенов Pump.Fun: три модуля

Рабочее название: `PumpFun_project`.

Проект объединяет три настольных модуля на Python для анализа токенов Pump.Fun в сети Solana. «История цен» собирает данные, «Оптимизатор стратегий» проверяет правила на сохранённой истории, а «Симулятор стратегий» наблюдает за токенами и ведёт виртуальные сделки. Каждый модуль имеет собственный интерфейс и запускается отдельно.

**Python-портфолио Евгения Толмачева** · Python · PyQt5 · pyqtgraph · DexScreener API · GeckoTerminal API · JSONL

<img src="docs/screenshots/optimizer/1.jpg" alt="Скриншот 1" width="860">

## Скриншоты

<details>
<summary>Оптимизатор стратегий - 11 снимков</summary>

### Скриншот 1

<img src="docs/screenshots/optimizer/1.jpg" alt="Скриншот 1" width="860">

### Скриншот 2

<img src="docs/screenshots/optimizer/2.jpg" alt="Скриншот 2" width="860">

### Скриншот 3

<img src="docs/screenshots/optimizer/3.jpg" alt="Скриншот 3" width="860">

### Скриншот 4

<img src="docs/screenshots/optimizer/4.jpg" alt="Скриншот 4" width="860">

### Скриншот 7

<img src="docs/screenshots/optimizer/7.jpg" alt="Скриншот 7" width="860">

### Скриншот 8

<img src="docs/screenshots/optimizer/8.jpg" alt="Скриншот 8" width="860">

### Скриншот 9

<img src="docs/screenshots/optimizer/9.jpg" alt="Скриншот 9" width="860">

### Скриншот 10

<img src="docs/screenshots/optimizer/10.jpg" alt="Скриншот 10" width="860">

### Скриншот 11

<img src="docs/screenshots/optimizer/11.jpg" alt="Скриншот 11" width="860">

### Скриншот 12

<img src="docs/screenshots/optimizer/12.jpg" alt="Скриншот 12" width="860">

### Скриншот 13

<img src="docs/screenshots/optimizer/13.jpg" alt="Скриншот 13" width="860">

</details>

<details>
<summary>История цен - 5 снимков</summary>

### Скриншот 1

<img src="docs/screenshots/history/1.jpg" alt="Скриншот 1" width="860">

### Скриншот 2

<img src="docs/screenshots/history/2.jpg" alt="Скриншот 2" width="860">

### Скриншот 3

<img src="docs/screenshots/history/3.jpg" alt="Скриншот 3" width="860">

### Скриншот 4

<img src="docs/screenshots/history/4.jpg" alt="Скриншот 4" width="860">

### Скриншот 5

<img src="docs/screenshots/history/5.jpg" alt="Скриншот 5" width="860">

</details>

<details>
<summary>Симулятор стратегий - 5 снимков</summary>

### Скриншот 1

<img src="docs/screenshots/simulator/1.jpg" alt="Скриншот 1" width="860">

### Скриншот 2

<img src="docs/screenshots/simulator/2.jpg" alt="Скриншот 2" width="860">

### Скриншот 3

<img src="docs/screenshots/simulator/3.jpg" alt="Скриншот 3" width="860">

### Скриншот 4

<img src="docs/screenshots/simulator/4.jpg" alt="Скриншот 4" width="860">

### Скриншот 5

<img src="docs/screenshots/simulator/5.jpg" alt="Скриншот 5" width="860">

</details>

## Возможности

- Сбор и обновление истории токенов через DexScreener и GeckoTerminal.
- Проверка правил входа и выхода и подбор параметров на исторических данных.
- Мониторинг и параллельная симуляция нескольких стратегий с учётом комиссии и проскальзывания.
- Просмотр графиков, открытых позиций и результатов отдельных сделок.

## Как устроен проект

### Оптимизатор стратегий

[`strategy_optimizer.py`](strategy_optimizer.py)

DataLoader читает историю, Backtester моделирует сделки, OptimizerWorker перебирает параметры. Таблицы результатов и ChartWindow позволяют изучить отдельный запуск.

### История цен

[`price_history_app.py`](price_history_app.py), [`candle_engine.py`](candle_engine.py)

PriceHistoryApp управляет интерфейсом и таблицами. CandleService в candle_engine.py координирует пакетную загрузку и Live-поток; окна HistoryWindow и CandleChartWindow показывают данные.

### Симулятор стратегий

[`boost_main.py`](boost_main.py), [`common_dsx.py`](common_dsx.py)

MainWindow в boost_main.py связывает мониторинг, настройки и симуляцию. Worker и CustomTableModel из common_dsx.py загружают данные и обновляют таблицы; JSON хранит состояние и журналы.

## Технологии и их роль

- **PyQt5 и фоновые потоки**: Интерфейсы мониторинга, загрузки данных и управления расчётами.
- **DexScreener и GeckoTerminal API**: Обнаружение токенов, рыночные показатели и исторические свечи.
- **JSON/JSONL**: Сохранение настроек, метаданных, цен и результатов симуляции.
- **pyqtgraph и QPainter**: Графики сделок и собственная отрисовка свечной истории.

## С чего начать просмотр кода


Код разделён по трём модулям: strategy_optimizer.py - оптимизатор, price_history_app.py и candle_engine.py - история цен, boost_main.py и common_dsx.py - мониторинг и симулятор. В карточке каждого модуля можно открыть только относящиеся к нему файлы.

## Подготовка и запуск

Создайте отдельное окружение и установите исходные зависимости:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Приложение зависит от доступности DexScreener и других API, указанных в исходниках. Кэш котировок и JSON-состояние не включены. QT_QPA_PLATFORM_PLUGIN_PATH нужен только при ручной настройке Qt; не задавайте ему буквальное значение YOUR_QT_QPA_PLATFORM_PLUGIN_PATH.

Переменные окружения: `QT_QPA_PLATFORM_PLUGIN_PATH`. Образец находится в [.env.example](.env.example). Сам файл .env автоматически не загружается: исходники читают окружение процесса. Значения нужно передать перед запуском, например в PowerShell:

```powershell
$env:QT_QPA_PLATFORM_PLUGIN_PATH = "YOUR_VALUE"
```

После подготовки настроек и необходимых данных основная точка входа:

```powershell
python boost_main.py
```

`requirements.txt` сохранён из проекта. Это снимок окружения, а не гарантия совместимости с новейшей версией Python. Для старых проектов могут потребоваться соответствующая версия Python и настройка системных компонентов.

## Структура

```text
PumpFun_project/
├── docs/
│   ├── preview.webp
│   └── screenshots/  # скриншоты из портфолио
├── .env.example
├── .gitignore
├── boost_main.py
├── candle_engine.py
├── common_dsx.py
├── price_history_app.py
├── README.md
├── requirements.txt
└── strategy_optimizer.py
```

## Контекст и ограничения

Проект 2025 года разделён на мониторинг, сбор истории и исторические тесты. Общие форматы JSON/JSONL связывают инструменты, а длительные операции выполняются в фоновых потоках. Торговые результаты здесь относятся к моделированию сделок на собранных данных.

Проверены синтаксис Python-файлов и состав публикации. Полный запуск этой версии с внешними сервисами, локальными данными или моделями не выполнялся; команды выше описывают подготовку по исходникам.

## Состав репозитория

Сохранены авторские исходники, их имена и структура, зависимости и скриншоты интерфейса. Рабочие базы, наборы данных, модели и виртуальные окружения исключены. Секреты заменены обозначениями `YOUR_...` / `REDACTED_...`; реальные значения задаются локально. Исходные алгоритмы при оформлении портфолио не перерабатывались.

Автор: [Евгений Толмачев](https://github.com/EvgenyET) · [Портфолио](https://evgeny.tech/)
