@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title HH.RU BOT - ПАНЕЛЬ УПРАВЛЕНИЯ
cd /d "%~dp0"


:MENU_LOOP
cls
echo ======================================================================
echo                 HH.RU AUTO-APPLY - ПАНЕЛЬ УПРАВЛЕНИЯ
echo ======================================================================

if exist "pause.flag" (
    echo   СТАТУС: [ПАУЗА] Бот приостановлен (активен pause.flag)
) else if exist "stop.flag" (
    echo   СТАТУС: [СТОП] Отправлен сигнал остановки (активен stop.flag)
) else (
    echo   СТАТУС: [ГОТОВ] Ожидание команды
)
echo ----------------------------------------------------------------------
echo  ОСНОВНЫЕ РЕЖИМЫ:
echo   [1] Запустить полный цикл (аудит отказов + адаптация + поиск + отклики)
echo   [2] Запустить в отдельном окне (меню остается для управления)
echo   [3] Быстрый запуск по сохранённому списку (браузер с окном)
echo   [4] Фоновый запуск (фоновый режим без окна)
echo.
echo  УПРАВЛЕНИЕ ЗАПУЩЕННЫМ БОТОМ:
echo   [5] Пауза / Возобновить
echo   [6] Плавная остановка (завершить текущую вакансию и выйти)
echo.
echo  АНАЛИТИКА И РЕЗЮМЕ:
echo   [7] Статистика откликов и приглашений
echo   [8] Разбор отказов работодателей
echo   [9] Комплексная модернизация резюме на HH.ru (навыки, уровни, Обо мне)
echo   [B] Бесплатно поднять резюме в поиске (обновить дату, раз в 4 часа)
echo   [A] Разбор чатов с отказами (переписка + автоправка резюме/ответа)
echo.
echo  НАСТРОЙКИ И РЕЗЮМЕ:
echo   [R] Выбрать целевое резюме из аккаунта hh.ru
echo   [W] Профиль соискателя на hh.ru
echo   [S] Сменить направление поиска / фильтры
echo   [T] Настроить сопроводительное письмо и Telegram
echo   [G] Ввести / изменить ключ ИИ
echo.
echo  СЕРВИСНЫЕ КОМАНДЫ:
echo   [C] Очистить список найденных вакансий
echo   [X] Закрыть зависший браузер
echo   [0] Выход
echo ======================================================================
echo  Горячие клавиши в консоли бота: [P] Пауза | [S] Стоп | [I] Статус
echo ======================================================================

echo   [N] Аккаунты, AI-фильтр и капча
set "choice="
set /p "choice=Выберите действие [1-9, B, A, R, W, S, T, G, C, X, 0]: "

if "%choice%"=="1" goto RUN_DIRECT
if "%choice%"=="2" goto RUN_SEPARATE
if "%choice%"=="3" goto RUN_CACHE
if "%choice%"=="4" goto RUN_HEADLESS
if "%choice%"=="5" goto TOGGLE_PAUSE
if "%choice%"=="6" goto SEND_STOP
if "%choice%"=="7" goto SHOW_STATS
if "%choice%"=="8" goto RUN_ANALYZER
if "%choice%"=="9" goto RUN_RESUME_UPDATE
if /i "%choice%"=="B" goto BUMP_RESUME
if /i "%choice%"=="A" goto RUN_CHAT_ANALYZER
if /i "%choice%"=="R" goto PICK_RESUME
if /i "%choice%"=="W" goto SHOW_WHOAMI
if /i "%choice%"=="S" goto PICK_SEARCH
if /i "%choice%"=="T" goto EDIT_LETTER
if /i "%choice%"=="G" goto SET_GEMINI
if /i "%choice%"=="C" goto CLEAR_CACHE
if /i "%choice%"=="X" goto KILL_CHROME
if /i "%choice%"=="N" goto NEW_SETTINGS
if "%choice%"=="0" goto EXIT_MENU

echo [!] Неверный ввод, повторите попытку.
timeout /t 2 >nul
goto MENU_LOOP

:RUN_DIRECT
echo.
echo [*] Запуск полного цикла откликов в текущем окне...
echo Нажмите [P] во время работы для паузы, [S] для остановки.
echo.
python test.py
echo.
echo [*] Сессия завершена.
pause
goto MENU_LOOP

:RUN_SEPARATE
echo.
echo [*] Запуск бота в отдельном окне...
start "HH.ru Bot Runner" cmd /k "chcp 65001 >nul && cd /d "%~dp0" && python test.py"
echo [OK] Бот запущен в отдельном окне.
echo Вы можете ставить его на паузу [5] или останавливать [6] из этого меню.
timeout /t 3 >nul
goto MENU_LOOP

:RUN_CACHE
echo.
echo [*] Запуск откликов по вакансиям из кеша (с окном браузера)...
python hh_selenium.py --api-cache --limit 200
echo.
pause
goto MENU_LOOP

:RUN_HEADLESS
echo.
echo [*] Запускаю отклики в фоне, без окна браузера...
python hh_selenium.py --api-cache --headless --limit 200
echo.
pause
goto MENU_LOOP

:TOGGLE_PAUSE
echo.
if exist "pause.flag" (
    del /f /q "pause.flag" 2>nul
    echo [ПУСК] Сигнал снятия с паузы отправлен.
    echo        Бот продолжает работу.
) else (
    type nul > "pause.flag"
    echo [ПАУЗА] Сигнал паузы отправлен .
    echo         Бот приостановит работу после текущей операции.
)
timeout /t 2 >nul
goto MENU_LOOP

:SEND_STOP
echo.
type nul > "stop.flag"
echo [СТОП] Сигнал плавной остановки отправлен .
echo        Бот завершит текущую вакансию, сохранит базу и закроет браузер.
timeout /t 2 >nul
goto MENU_LOOP

:SHOW_STATS
echo.
python db_manager.py
echo.
pause
goto MENU_LOOP

:RUN_ANALYZER
echo.
echo [*] Запуск анализа отказов и ATS-аудита...
python rejection_analyzer.py --limit 20
echo.
pause
goto MENU_LOOP

:RUN_RESUME_UPDATE
echo.
echo [*] Запуск комплексной модернизации резюме на hh.ru...
python resume_updater.py --full-update
echo.
pause
goto MENU_LOOP

:BUMP_RESUME
echo.
echo [*] Поднятие резюме в поиске на hh.ru...
python resume_updater.py --bump
echo.
pause
goto MENU_LOOP

:RUN_CHAT_ANALYZER
echo.
echo [*] Запуск глубокого разбора чатов и переписки с отказами...
python rejection_analyzer.py --chats --limit 25
echo.
pause
goto MENU_LOOP

:PICK_RESUME
echo.
python config_manager.py --pick-resume
goto MENU_LOOP

:SHOW_WHOAMI
echo.
echo [*] Запрашиваю профиль на hh.ru...
python test.py --whoami
echo.
pause
goto MENU_LOOP

:PICK_SEARCH
echo.
python config_manager.py --pick-search
goto MENU_LOOP

:EDIT_LETTER
echo.
python config_manager.py --edit-letter
goto MENU_LOOP

:SET_GEMINI
echo.
python config_manager.py --set-gemini-key
goto MENU_LOOP

:CLEAR_CACHE
echo.
python test.py --clear-cache
timeout /t 2 >nul
goto MENU_LOOP

:KILL_CHROME
echo.
echo [*] Закрываю зависший браузер...
taskkill /f /im chromedriver.exe /t 2>nul
echo [OK] Завершение процессов выполнено.
timeout /t 2 >nul
goto MENU_LOOP

:NEW_SETTINGS
python hh.py menu
goto MENU_LOOP

:EXIT_MENU
echo.
echo Завершение работы.
exit /b 0
