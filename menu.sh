#!/usr/bin/env bash
# ==============================================================================
#  HH.RU AUTO-APPLY BOT - ПАНЕЛЬ УПРАВЛЕНИЯ (BASH / GIT BASH)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"


RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

if command -v python &>/dev/null; then
    PYTHON_CMD="python"
elif command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
else
    echo -e "${RED}[ERROR] Python не найден в PATH!${NC}"
    exit 1
fi

get_bot_status() {
    if [ -f "stop.flag" ]; then
        echo -e "${RED}[СТОП] Активен stop.flag${NC}"
    elif [ -f "pause.flag" ]; then
        echo -e "${YELLOW}[ПАУЗА] Активен pause.flag${NC}"
    else
        echo -e "${GREEN}[ГОТОВ] Ожидание команды${NC}"
    fi
}

while true; do
    clear
    echo -e "${CYAN}${BOLD}======================================================================${NC}"
    echo -e "${CYAN}${BOLD}                 HH.RU AUTO-APPLY - ПАНЕЛЬ УПРАВЛЕНИЯ${NC}"
    echo -e "${CYAN}${BOLD}======================================================================${NC}"
    echo -e "  Статус: $(get_bot_status)"
    echo -e "${CYAN}----------------------------------------------------------------------${NC}"
    echo -e "  ${BOLD}ОСНОВНЫЕ РЕЖИМЫ:${NC}"
    echo -e "   ${GREEN}[1]${NC} Запустить полный цикл (API поиск + отклики в консоли)"
    echo -e "   ${GREEN}[2]${NC} Быстрый запуск по API-кешу (Selenium с окном браузера)"
    echo -e "   ${GREEN}[3]${NC} Фоновый запуск (Headless режим без окна)"
    echo -e ""
    echo -e "  ${BOLD}УПРАВЛЕНИЕ ЗАПУЩЕННЫМ БОТОМ:${NC}"
    echo -e "   ${YELLOW}[4]${NC} Пауза / Возобновить (переключить pause.flag)"
    echo -e "   ${RED}[5]${NC} Плавная остановка (сохранить базу и завершить через stop.flag)"
    echo -e ""
    echo -e "  ${BOLD}АНАЛИТИКА И РЕЗЮМЕ:${NC}"
    echo -e "   ${BLUE}[6]${NC} Статистика базы данных и конверсия (SQLite)"
    echo -e "   ${BLUE}[7]${NC} Анализ отказов и ATS-аудит (rejection_analyzer)"
    echo -e "   ${BLUE}[8]${NC} Комплексная модернизация резюме на HH.ru (навыки, уровни, Обо мне)"
    echo -e ""
    echo -e "  ${BOLD}НАСТРОЙКИ И РЕЗЮМЕ:${NC}"
    echo -e "   ${CYAN}[r]${NC} Выбрать целевое резюме из аккаунта hh.ru"
    echo -e "   ${CYAN}[s]${NC} Сменить направление поиска / фильтры"
    echo -e "   ${CYAN}[t]${NC} Настроить сопроводительное письмо и Telegram"
    echo -e "   ${CYAN}[g]${NC} Ввести / изменить Google Gemini API ключ"
    echo -e ""
    echo -e "  ${BOLD}СЕРВИСНЫЕ КОМАНДЫ:${NC}"
    echo -e "   ${MAGENTA}[c]${NC} Очистить кеш вакансий (vacancies_cache.json)"
    echo -e "   ${RED}[x]${NC} Завершить зависшие процессы ChromeDriver"
    echo -e "   ${BOLD}[0]${NC} Выход"
    echo -e "${CYAN}${BOLD}======================================================================${NC}"
    echo -e "  Горячие клавиши в консоли бота: ${BOLD}[P] Пауза | [S] Стоп | [I] Статус${NC}"
    echo -e "${CYAN}${BOLD}======================================================================${NC}"

    echo "   [N] Аккаунты, AI-фильтр и капча"
    read -rp "Выберите действие [1-8, r, s, t, g, c, x, 0]: " choice
    echo ""

    case "$choice" in
        1)
            echo -e "${GREEN}[*] Запуск полного цикла откликов...${NC}"
            echo -e "${YELLOW}Горячие клавиши: [P] - пауза, [S] - стоп, [I] - статус.${NC}"
            "$PYTHON_CMD" test.py
            echo ""
            read -rp "Нажмите Enter для возврата в меню..."
            ;;
        2)
            echo -e "${GREEN}[*] Запуск откликов по API-кешу (Selenium с окном)...${NC}"
            "$PYTHON_CMD" hh_selenium.py --api-cache --limit 200
            echo ""
            read -rp "Нажмите Enter для возврата в меню..."
            ;;
        3)
            echo -e "${GREEN}[*] Запуск Selenium в фоновом режиме (Headless)...${NC}"
            "$PYTHON_CMD" hh_selenium.py --api-cache --headless --limit 200
            echo ""
            read -rp "Нажмите Enter для возврата в меню..."
            ;;
        4)
            if [ -f "pause.flag" ]; then
                rm -f "pause.flag"
                echo -e "${GREEN}[ПУСК] Сигнал снятия с паузы отправлен (pause.flag удален).${NC}"
                echo -e "       Бот продолжает отправку откликов."
            else
                touch "pause.flag"
                echo -e "${YELLOW}[ПАУЗА] Сигнал паузы отправлен (pause.flag создан).${NC}"
                echo -e "        Бот приостановит работу после текущей операции."
            fi
            sleep 2
            ;;
        5)
            touch "stop.flag"
            echo -e "${RED}[СТОП] Сигнал плавной остановки отправлен (stop.flag создан).${NC}"
            echo -e "       Бот завершит текущую вакансию, сохранит базу и закроет браузер."
            sleep 2
            ;;
        6)
            echo -e "${BLUE}[*] Загрузка статистики базы данных...${NC}"
            "$PYTHON_CMD" db_manager.py
            echo ""
            read -rp "Нажмите Enter для возврата в меню..."
            ;;
        7)
            echo -e "${BLUE}[*] Запуск анализа отказов и ATS-аудита...${NC}"
            "$PYTHON_CMD" rejection_analyzer.py --limit 20
            echo ""
            read -rp "Нажмите Enter для возврата в меню..."
            ;;
        8)
            echo -e "${BLUE}[*] Комплексная модернизация резюме на hh.ru (навыки, уровни, Обо мне)...${NC}"
            "$PYTHON_CMD" resume_updater.py --full-update
            echo ""
            read -rp "Нажмите Enter для возврата в меню..."
            ;;
        [rR])
            echo ""
            "$PYTHON_CMD" config_manager.py --pick-resume
            ;;
        [sS])
            echo ""
            "$PYTHON_CMD" config_manager.py --pick-search
            ;;
        [tT])
            echo ""
            "$PYTHON_CMD" config_manager.py --edit-letter
            ;;
        [gG])
            echo ""
            "$PYTHON_CMD" config_manager.py --set-gemini-key
            ;;
        [cC])
            echo -e "${MAGENTA}[*] Очистка кеша вакансий...${NC}"
            "$PYTHON_CMD" test.py --clear-cache
            sleep 2
            ;;
        [xX])
            echo -e "${RED}[*] Завершение зависших процессов ChromeDriver...${NC}"
            taskkill //f //im chromedriver.exe 2>/dev/null || pkill -f chromedriver 2>/dev/null
            echo -e "${GREEN}[OK] Очистка завершена.${NC}"
            sleep 2
            ;;
        [nN])
            "$PYTHON_CMD" hh.py menu
            ;;
        0)
            echo "Завершение работы."
            exit 0
            ;;
        *)
            echo -e "${RED}[!] Неверный ввод, повторите попытку.${NC}"
            sleep 1
            ;;
    esac
done
