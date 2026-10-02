# HH Auto-Apply

Поиск вакансий, отклики через Selenium, AI-письма и ответы на анкеты, подъём резюме, локальная история и анализ отказов.

Проект имеет отдельную реализацию. Это не полная копия [hh-applicant-tool](https://github.com/s3rgeym/hh-applicant-tool). У исходного проекта тоже есть AI-письма, решение тестов и гибридная работа через API и браузер; прежняя сравнительная таблица здесь была неверной. Успех отправки зависит от сессии, ограничений HH и изменений сайта.

### Поддержать проект

[![Donate BTC](https://img.shields.io/badge/Donate-BTC-F7931A?style=for-the-badge&logo=bitcoin&logoColor=white)](https://mempool.space/address/bc1q472jja3q5zftdrsj07hnj44wjnh2kgup6ypzze)
[![Donate ETH / USDT](https://img.shields.io/badge/Donate-ETH%20%7C%20USDT-3C3C3D?style=for-the-badge&logo=ethereum&logoColor=white)](https://etherscan.io/address/0xdc07c830a2E7A641f28465dc69aaf94e622c64Ed)
[![Donate USDT TRC-20](https://img.shields.io/badge/Donate-USDT%20TRC--20-EB0029?style=for-the-badge&logo=tether&logoColor=white)](https://tronscan.org/#/address/TDfN2H5ANr3oyBiN6BGgJWNBYDPEDACzsZ)
[![Donate SOL](https://img.shields.io/badge/Donate-SOL-9945FF?style=for-the-badge&logo=solana&logoColor=white)](https://solscan.io/account/7C8F58uvaU3ooBtrmatbMfSTrL6abZVpw8Pxpc54rHRj)

| Сеть | Что отправлять | Адрес |
|---|---|---|
| Bitcoin | BTC | `bc1q472jja3q5zftdrsj07hnj44wjnh2kgup6ypzze` |
| Ethereum, BNB Smart Chain, Polygon, Arbitrum, Base | ETH, BNB, USDT, USDC | `0xdc07c830a2E7A641f28465dc69aaf94e622c64Ed` |
| TRON | USDT (TRC-20), TRX | `TDfN2H5ANr3oyBiN6BGgJWNBYDPEDACzsZ` |
| Solana | SOL, USDT, USDC | `7C8F58uvaU3ooBtrmatbMfSTrL6abZVpw8Pxpc54rHRj` |

Донаты двигают разработку: hh регулярно меняет вёрстку, и бота приходится подстраивать.

> [!CAUTION]
> Отправляйте монету только на адрес её сети из таблицы. Перевод в чужую сеть, например USDT
> TRC-20 на адрес Ethereum, не дойдёт.

Не хотите платить, поставьте звезду: так бот проще найти другим соискателям.

---

## Установка и запуск

Нужны Python 3.10+ и Google Chrome.

```powershell
pip install -r requirements.txt
python hh.py menu
```

В `menu.bat` и `menu.sh` пункт **N** открывает это меню. В Python-меню:

- **N → L** — войти в HH в видимом браузере, без отправки откликов.
- **P → R** — выбрать целевое резюме.
- **N → S** — направление поиска.
- **N → T** — письмо и контакты.
- **N → G / K** — основной Gemini / запасной Groq.
- **N → F** — AI-фильтрация и Vision для текстовой капчи.
- **N → U** — открыть или создать отдельный аккаунт.

Проверьте свои данные в `candidate_profile` в настройках выбранного аккаунта. AI-фильтр использует именно эти данные; он не предполагает, что вы владеете навыками из описания вакансии. Новый профиль создаётся с пустыми резюме, данными кандидата и ключами.

## AI-фильтрация

По умолчанию выключена, чтобы добавление функции не меняло текущий отбор. Работает до создания письма и нажатия кнопки отклика, в обработке кеша и поиске на сайте. При включении полный цикл также направляет отправку через этот общий браузерный путь.

| Режим | Контекст |
|---|---|
| `off` | Без AI-фильтра |
| `light` | Название, навыки вакансии, специализация и навыки кандидата |
| `heavy` | Дополнительно описание вакансии и заполненные поля опыта, образования, условий и «О себе» кандидата |
| `custom` | Контекст heavy и критерии пользователя |

```powershell
python hh.py settings --ai-filter heavy
python hh.py settings --ai-filter custom --filter-prompt "Только удалённый Python backend"
python hh.py settings --ai-filter off
```

Фильтр использует существующую цепочку AI-провайдеров, включая настроенные CLI-провайдеры. При отсутствии ответа или неправильном JSON отклик не отправляется, вакансия остаётся для повторной проверки. Решение и причина сохраняются в SQLite: `ai_filter` — отклонение, `ai_unavailable` — отложенная проверка. Имя и контакты кандидата в запрос фильтра не включаются.

## Текстовая капча

```powershell
python hh.py settings --captcha on
python hh.py settings --captcha off
```

Распознаётся текстовая картинка HH с полем ввода (`account-captcha-picture` / `account-captcha-input`). В Gemini или OpenAI-совместимый API передаётся только снимок элемента с картинкой. Нужна модель, принимающая изображения. По умолчанию используются модель и API-ключ основного ИИ; отдельную модель можно задать через `--captcha-model` или меню N → F.

Отдельный провайдер настраивается в файле профиля:

```json
{
  "captcha": {
    "enabled": true,
    "provider": "openai",
    "model": "gpt-4o-mini",
    "max_attempts": 2,
    "timeout_seconds": 20
  }
}
```

Ключ можно передать через `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` или поле `captcha.api_key`. Для совместимого сервера предусмотрено `captcha.base_url`.

Бот проверяет исчезновение капчи после ввода. Максимум — три попытки за обработку. При ошибке в видимом окне остаётся ручное решение; в headless вакансия остаётся в очереди. Картинные пазлы, перетаскивание и сторонние виджеты этой функцией не решаются.

## Аккаунты

```powershell
python hh.py profiles
python hh.py --profile-id second login
python hh.py --profile-id second menu
python hh.py --profile-id second settings --ai-filter light
python hh.py --profile-id second run --headless --limit 20
```

`default` сохраняет прежние файлы в корне проекта. У остальных аккаунтов данные лежат в `profiles/<имя>/`: настройки, токены, Chrome cookies, база, история, кеши, флаги, расписание подъёма и отчёты. Существующий аккаунт в новый профиль не копируется. Имена: латинские буквы, цифры, `_`, `-`, до 64 символов.

`HH_DATA_DIR` меняет корень данных, `HH_PROFILE_ID` выбирает профиль для прямых запусков старых Python-модулей. Для управления аккаунтами используйте `hh.py`: его команды блокируют повторный одновременный запуск того же профиля. Не запускайте старые скрипты параллельно с ним для одного аккаунта. Окна разных аккаунтов независимы.

## Расписание

```powershell
python hh.py --profile-id second schedule --headless --every-minutes 240 --limit 100
```

Первый цикл запускается сразу. Следующий — через заданный интервал после завершения предыдущего; циклы не перекрываются. Каждый цикл читает свежие настройки, обрабатывает кеш и ищет вакансии на сайте, используя существующие фильтры, дневной лимит и подъём резюме по его сроку. Ошибка цикла выводится с кодом завершения; следующий цикл пробует снова. Остановка — Ctrl+C. Для другого аккаунта запускается отдельная команда с его именем.

## Docker

Образ содержит Chromium, ChromeDriver и временное окно входа через noVNC. Личные файлы, токены, база и cookies не включаются в контекст сборки: `.dockerignore` разрешает только нужные исходники. Данные сохраняются в именованном томе `hh_data`.

```powershell
docker compose build
docker compose run --rm --service-ports auth
```

Откройте [локальное окно входа](http://localhost:6080/vnc.html?autoconnect=true) и войдите в HH в течение трёх минут. Порт опубликован только на `127.0.0.1`. После входа временный браузер закроется. Сессия Windows Chrome не копируется: контейнер создаёт собственную.

Затем выберите резюме и настройте кандидата, направление и ИИ:

```powershell
docker compose run --rm bot python hh.py menu
docker compose up -d bot
docker compose logs -f bot
```

Остановка: `docker compose stop bot`. Перед повторным входом остановите bot, затем повторите auth. `HH_INTERVAL_MINUTES` задаёт интервал (по умолчанию 240). `HH_PROFILE_ID` выбирает аккаунт; для второго аккаунта сначала выполните auth и menu с его именем:

```powershell
docker compose run --rm --service-ports -e HH_PROFILE_ID=second auth
docker compose run --rm -e HH_PROFILE_ID=second bot python hh.py menu
docker compose run -d --name hh-second -e HH_PROFILE_ID=second bot
```

## Проверка

```powershell
python -m pytest tests -q
$env:HH_BROWSER_SMOKE = '1'
python -m pytest tests/test_applicant_features.py::test_captcha_in_real_chrome -q
```

Обычные тесты подменяют сеть и браузер. Последняя команда запускает настоящий Chrome с временным профилем и локальной формой: проверяет снимок картинки, ввод и подтверждение; ответ Vision подменяется. Это не проверка реального распознавания моделью или реального отклика на HH.

Форматы изображений в API сверены с документацией [Gemini](https://ai.google.dev/api/generate-content) и [OpenAI](https://developers.openai.com/api/docs/guides/images-vision).

## Изменения 2 октября 2026

- Проверка входа в HH по элементам аккаунта; ожидание авторизации без повторной перезагрузки формы.
- Выбор основного ИИ и отдельный учёт моделей: временные ошибки и ограничения одного провайдера позволяют продолжить через следующий.
- Проверка писем и ответов на выдуманные навыки, числа и имена; зарплатные вопросы требуют согласования с кандидатом.
- Разбор отказов использует собственные сообщения кандидата и учитывает автора реплики.
- Настройки направления, уровня, резюме и браузера доступны в меню; обновлены проверки анкеты, фильтров и состояния отклика.

Для OAuth-приложения задайте `HH_CLIENT_ID` и `HH_CLIENT_SECRET` через окружение. Контакт для HH задаётся в меню или через `HH_CONTACT_EMAIL`. Личные настройки, база, cookies, отчёты и ключи остаются локально и исключены из Git и Docker-контекста.
