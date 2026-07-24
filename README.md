# MRKT ↔ Getgems Arbitrage Bot

Кросс-маркет арбитраж NFT-подарков Telegram между маркетплейсами **MRKT** и **Getgems**.

## Как работает

1. **Сканирует** цены на MRKT и Getgems каждые 30 сек
2. **Покупает** на дешёвом маркете автоматически
3. **Выводит** из MRKT → шлёт уведомление в Telegram
4. **Пользователь** переводит подарок @gemsrelayer (10 сек) и нажимает кнопку
5. **Автоматически** выставляет на продажу на Getgems

## Стратегии

| Стратегия | Описание | Скорость |
|-----------|----------|----------|
| 🌐 Кросс-маркет | Купить на MRKT → продать на Getgems (или наоборот) | ~5 мин |
| ⚡ Арбитраж MRKT | Купить листинг → продать в ордер | Мгновенно |
| 💎 Глубокая скидка | Цена ниже медианы на 40%+ → купить и перепродать | ~часы |

## Установка

```bash
# Клонировать
git clone <repo_url>
cd mrkt-resale-bot

# Зависимости
pip install -r requirements.txt
playwright install chromium

# Конфигурация
cp .env.example .env
# Заполнить .env (см. ниже)

# Запуск
python main.py
```

## Конфигурация (.env)

```env
# Telegram
TELEGRAM_BOT_TOKEN=...      # Токен от @BotFather
ADMIN_CHAT_ID=...            # Ваш Telegram user ID

# MRKT
MRKT_AUTH_TOKEN=...          # Токен авторизации MRKT API

# Getgems (для кросс-маркет арбитража)
WALLET_MNEMONIC=word1 word2 ... word24  # Seed phrase TON кошелька
GETGEMS_CDP_URL=http://localhost:29229  # Chrome DevTools Protocol URL

# База данных
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/mrkt_bot

# Торговля
MIN_ROI_PERCENT=10           # Минимальный ROI для сделки (%)
MAX_TRADE_AMOUNT_TON=0       # Лимит на сделку (0 = без лимита)
SCAN_INTERVAL_SECONDS=30     # Интервал сканирования
CROSS_MARKET_ENABLED=true    # Включить кросс-маркет
```

## Команды Telegram бота

| Команда | Описание |
|---------|----------|
| `/start` | Запустить бота |
| `/status` | Статус сканера и рынка |
| `/balance` | Баланс MRKT |
| `/market` | Сравнение цен MRKT vs Getgems |
| `/deals` | Последние сделки |
| `/portfolio` | Статистика портфеля |
| `/settings` | Настройки (ROI, интервал, лимиты) |
| `/scanner_on` | Включить сканер |
| `/scanner_off` | Выключить сканер |
| `/token <токен>` | Обновить MRKT токен |

## Архитектура

```
main.py              — точка входа, инициализация
bot/
  config.py          — настройки (pydantic-settings)
  fees.py            — калькулятор комиссий
  analyzer.py        — анализ цен, поиск сделок
  scanner.py         — фоновый сканер (главный цикл)
  trader.py          — исполнение сделок (покупка/продажа)
  mrkt_client.py     — API клиент MRKT
  getgems_client.py  — Getgems API + автолистинг (Playwright + TON Connect)
  ton_crypto.py      — криптография TON (ключи, подписи)
  telegram_bot.py    — Telegram интерфейс
  database.py        — SQLAlchemy модели (PostgreSQL)
```

## Безопасность

- Seed phrase хранится только в `.env` (не коммитится)
- MRKT токен обновляется через Telegram команду `/token`
- Лимит дневных потерь (5% по умолчанию)
- Все сделки записываются в БД
