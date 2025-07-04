import ccxt.async_support as ccxt
import asyncio
import os
import hashlib
import math
from datetime import datetime, timedelta
from telegram.ext import Application, CommandHandler
from telegram.constants import ParseMode
import logging
from typing import Dict, Tuple, List, Optional

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
MODE = os.getenv("MODE", "demo").lower()

# Валидация конфигурации
if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, API_KEY, API_SECRET]):
    raise EnvironmentError("Missing required environment variables")

# === Инициализация ===
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "spot", "recvWindow": 10000}
})
if MODE == "demo":
    exchange.set_sandbox_mode(True)

# === Конфигурация ===
COMMISSION_RATE = 0.001
MIN_PROFIT = 0.1
TARGET_VOLUME_USDT = 100
START_COINS = ['USDT', 'BTC', 'ETH']
DEBUG_MODE = True
TRIANGLE_HOLD_TIME = 5
LOG_FILE = "triangle_log.csv"
MAX_DRAWDOWN = -5.0
MAX_TRADES_PER_MIN = 3
ORDERBOOK_CACHE_TTL = 5  # секунд

# === Состояние ===
net_profit = 0.0
recent_trades = asyncio.Queue()
triangle_cache = {}
orderbook_cache = {}
orderbook_last_updated = datetime.min

# Блокировки
trade_lock = asyncio.Lock()
cache_lock = asyncio.Lock()

# === Логирование ===
logging.basicConfig(
    filename='arbitrage_bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === Telegram функции ===
async def send_telegram_message(text: str):
    try:
        await telegram_app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# === Базовые функции ===
async def fetch_balances():
    try:
        balances = await exchange.fetch_balance()
        return balances["total"]
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return {}

async def get_symbol_meta(symbol: str, markets: dict) -> Optional[dict]:
    m = markets.get(symbol)
    if not m: 
        return None
    return {
        "price_precision": m['precision']['price'],
        "amount_precision": m['precision']['amount'],
        "min_cost": m['limits']['cost']['min'],
        "min_amount": m['limits']['amount']['min']
    }

async def round_to_precision(value: float, precision: int) -> float:
    if precision == 0: 
        return math.floor(value)
    factor = 10 ** precision
    return math.floor(value * factor) / factor

# === Кэширование стаканов ===
async def update_orderbooks(symbols: list):
    global orderbook_last_updated
    async with cache_lock:
        if (datetime.utcnow() - orderbook_last_updated).seconds < ORDERBOOK_CACHE_TTL:
            return
            
        logger.info("Updating orderbooks...")
        tasks = [exchange.fetch_order_book(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                logger.warning(f"Orderbook error for {symbol}: {result}")
                continue
            orderbook_cache[symbol] = {
                'asks': result['asks'],
                'bids': result['bids'],
                'timestamp': datetime.utcnow()
            }
        
        orderbook_last_updated = datetime.utcnow()

async def get_execution_price(symbol: str, side: str, target_amount: float) -> Tuple[float, float, float]:
    try:
        orderbook = orderbook_cache.get(symbol)
        if not orderbook:
            return None, 0, 0
            
        orderbook_side = orderbook['asks'] if side == 'buy' else orderbook['bids']
        total_base = 0
        total_quote = 0
        max_liquidity = 0
        
        for price, volume in orderbook_side:
            price, volume = float(price), float(volume)
            quote_amount = price * volume
            
            max_liquidity += quote_amount
            if total_quote + quote_amount >= target_amount:
                remaining = target_amount - total_quote
                volume_used = remaining / price
                total_base += volume_used
                total_quote += remaining
                break
                
            total_base += volume
            total_quote += quote_amount
            
        if total_quote < target_amount * 0.9:  # Минимум 90% ликвидности
            return None, 0, max_liquidity
            
        avg_price = total_quote / total_base
        return avg_price, total_quote, max_liquidity
        
    except Exception as e:
        logger.error(f"Price calculation error: {e}")
        return None, 0, 0

# === Исполнение сделок ===
async def execute_trade_step(
    symbol: str, 
    side: str, 
    amount: float, 
    currency: str,
    markets: dict
) -> Optional[float]:
    try:
        meta = await get_symbol_meta(symbol, markets)
        if not meta:
            return None
            
        # Определяем базовую и котируемую валюты
        base_currency, quote_currency = symbol.split('/')
        
        # Проверяем соответствие валюты операции
        if side == 'buy' and currency != quote_currency:
            logger.error(f"Currency mismatch for buy: have {currency}, need {quote_currency}")
            return None
        if side == 'sell' and currency != base_currency:
            logger.error(f"Currency mismatch for sell: have {currency}, need {base_currency}")
            return None
            
        # Рассчитываем объем
        orderbook = orderbook_cache.get(symbol)
        if not orderbook:
            return None
            
        price = orderbook['asks'][0][0] if side == 'buy' else orderbook['bids'][0][0]
        price = float(price)
        
        if side == 'buy':
            # Для покупки: amount в котируемой валюте
            order_amount = amount / price
        else:
            # Для продажи: amount в базовой валюте
            order_amount = amount
            
        # Округление
        order_amount = await round_to_precision(order_amount, meta['amount_precision'])
        order_price = await round_to_precision(price, meta['price_precision'])
        
        # Проверка минимальных значений
        if order_amount < meta['min_amount']:
            logger.warning(f"Amount too small: {order_amount} < {meta['min_amount']}")
            return None
            
        cost = order_amount * order_price
        if cost < meta['min_cost']:
            logger.warning(f"Cost too small: {cost} < {meta['min_cost']}")
            return None
            
        # Создание ордера
        logger.info(f"Executing {side} {symbol}: {order_amount} @ {order_price}")
        order = await exchange.create_order(
            symbol, 
            "market", 
            side, 
            order_amount
        )
        
        # Возвращаем исполненное количество
        if side == 'buy':
            return float(order['filled'])  # Полученная базовая валюта
        else:
            return float(order['filled']) * float(order['price'])  # Полученная котируемая валюта
            
    except ccxt.InsufficientFunds as e:
        logger.error(f"Insufficient funds: {e}")
        await send_telegram_message(f"❌ Недостаточно средств: {e}")
    except ccxt.BaseError as e:
        logger.error(f"Exchange error: {e}")
        await send_telegram_message(f"❌ Ошибка биржи: {e}")
    except Exception as e:
        logger.exception(f"Trade execution error: {e}")
        await send_telegram_message(f"❌ Критическая ошибка: {e}")
    
    return None

async def execute_triangle_trade(
    base: str, 
    mid1: str, 
    mid2: str, 
    markets: dict,
    start_amount: float
):
    global net_profit
    
    # Проверяем блокировку
    if trade_lock.locked():
        logger.info("Trade already in progress, skipping")
        return
        
    async with trade_lock:
        try:
            # Проверяем лимиты частоты сделок
            now = datetime.utcnow()
            recent_count = sum(1 for t in recent_trades._queue if (now - t).seconds < 60)
            if recent_count >= MAX_TRADES_PER_MIN:
                logger.info("Trade frequency limit reached")
                return
                
            # Проверяем просадку
            if net_profit < MAX_DRAWDOWN:
                await send_telegram_message(f"🛑 Максимальная просадка достигнута: {net_profit:.2f} USDT")
                return
                
            # Получаем баланс
            balances = await fetch_balances()
            if balances.get(base, 0) < start_amount:
                logger.warning(f"Insufficient {base} balance")
                return
                
            logger.info(f"Starting triangle: {base}->{mid1}->{mid2}->{base}")
            
            # Шаг 1: base -> mid1
            symbol1 = f"{mid1}/{base}"
            amount1 = await execute_trade_step(symbol1, 'buy', start_amount, base, markets)
            if not amount1:
                raise Exception(f"Step 1 failed: {symbol1}")
                
            # Шаг 2: mid1 -> mid2
            symbol2 = f"{mid2}/{mid1}"
            amount2 = await execute_trade_step(symbol2, 'buy', amount1, mid1, markets)
            if not amount2:
                # Компенсация: продаем mid1 обратно в base
                await execute_trade_step(symbol1, 'sell', amount1, mid1, markets)
                raise Exception(f"Step 2 failed: {symbol2}")
                
            # Шаг 3: mid2 -> base
            symbol3 = f"{mid2}/{base}"
            amount3 = await execute_trade_step(symbol3, 'sell', amount2, mid2, markets)
            if not amount3:
                # Компенсация: продаем mid2 обратно в base
                await execute_trade_step(symbol3, 'sell', amount2, mid2, markets)
                raise Exception(f"Step 3 failed: {symbol3}")
                
            # Рассчет прибыли
            profit = amount3 - start_amount
            net_profit += profit
            recent_trades.put_nowait(datetime.utcnow())
            
            # Логирование
            profit_percent = (profit / start_amount) * 100
            message = (
                f"✅ <b>Арбитраж выполнен!</b>\n"
                f"Маршрут: {base} → {mid1} → {mid2} → {base}\n"
                f"Стартовый баланс: {start_amount:.2f} {base}\n"
                f"Конечный баланс: {amount3:.2f} {base}\n"
                f"Прибыль: {profit:.2f} {base} ({profit_percent:.2f}%)\n"
                f"Суммарная прибыль: {net_profit:.2f} {base}"
            )
            logger.info(message)
            await send_telegram_message(message)
            
            # Логирование в CSV
            with open(LOG_FILE, "a") as f:
                f.write(f"{datetime.utcnow()},{base},{mid1},{mid2},{start_amount},{amount3},{profit}\n")
                
        except Exception as e:
            logger.error(f"Triangle trade failed: {e}")
            await send_telegram_message(f"❌ Ошибка арбитража: {e}")

# === Поиск арбитражных возможностей ===
async def load_symbols() -> Tuple[list, dict]:
    markets = await exchange.load_markets()
    active_symbols = [
        s for s, m in markets.items() 
        if m.get('active') and m['quote'] == 'USDT'
    ]
    return active_symbols, markets

async def find_triangles(symbols: list) -> List[Tuple[str, str, str]]:
    triangles = []
    graph = {}
    
    # Строим граф отношений
    for symbol in symbols:
        base, quote = symbol.split('/')
        if base not in graph:
            graph[base] = []
        graph[base].append(quote)
        
    # Поиск треугольников
    for a in START_COINS:
        for b in graph.get(a, []):
            for c in graph.get(b, []):
                # Проверяем замыкание A->B->C->A
                if c in graph and a in graph[c]:
                    triangles.append((a, b, c))
                    
    return triangles

async def check_triangle(
    base: str, 
    mid1: str, 
    mid2: str, 
    markets: dict
):
    try:
        # Проверяем кэш треугольников
        triangle_id = f"{base}-{mid1}-{mid2}"
        async with cache_lock:
            last_seen = triangle_cache.get(triangle_id)
            if last_seen and (datetime.utcnow() - last_seen).seconds < TRIANGLE_HOLD_TIME:
                return
                
        symbols = {
            's1': f"{mid1}/{base}",
            's2': f"{mid2}/{mid1}",
            's3': f"{mid2}/{base}"
        }
        
        # Проверяем наличие пар на бирже
        if not all(s in orderbook_cache for s in symbols.values()):
            return
            
        # Получаем цены исполнения
        price1, volume1, liq1 = await get_execution_price(symbols['s1'], 'buy', TARGET_VOLUME_USDT)
        price2, volume2, liq2 = await get_execution_price(symbols['s2'], 'buy', TARGET_VOLUME_USDT)
        price3, volume3, liq3 = await get_execution_price(symbols['s3'], 'sell', TARGET_VOLUME_USDT)
        
        if not all([price1, price2, price3]):
            return
            
        # Расчет прибыли с учетом комиссий
        step1 = (1 / price1) * (1 - COMMISSION_RATE)
        step2 = (1 / price2) * (1 - COMMISSION_RATE)
        step3 = price3 * (1 - COMMISSION_RATE)
        
        result = step1 * step2 * step3
        profit_percent = (result - 1) * 100
        
        # Проверка минимальной прибыли
        if profit_percent < MIN_PROFIT:
            return
            
        min_liquidity = min(liq1, liq2, liq3)
        pure_profit = (result - 1) * TARGET_VOLUME_USDT
        
        # Обновляем кэш
        async with cache_lock:
            triangle_cache[triangle_id] = datetime.utcnow()
            
        # Отправляем уведомление
        message = (
            f"🔍 <b>Арбитражная возможность</b>\n"
            f"Маршрут: {base} → {mid1} → {mid2} → {base}\n"
            f"Прибыль: {pure_profit:.2f} USDT ({profit_percent:.2f}%)\n"
            f"Ликвидность: ${min_liquidity:.2f}\n"
            f"1. {symbols['s1']} (BUY): {price1:.6f}\n"
            f"2. {symbols['s2']} (BUY): {price2:.6f}\n"
            f"3. {symbols['s3']} (SELL): {price3:.6f}"
        )
        logger.info(f"Arbitrage opportunity: {profit_percent:.2f}% profit")
        await send_telegram_message(message)
        
        # Запускаем сделку
        await execute_triangle_trade(base, mid1, mid2, markets, TARGET_VOLUME_USDT)
        
    except Exception as e:
        logger.error(f"Triangle check error: {e}")

# === Команды Telegram ===
async def status(update, context):
    try:
        balances = await fetch_balances()
        usdt_balance = balances.get("USDT", 0)
        response = (
            f"♻️ <b>Статус бота</b>\n"
            f"• Режим: {'DEMO' if MODE == 'demo' else 'LIVE'}\n"
            f"• Баланс USDT: {usdt_balance:.2f}\n"
            f"• Суммарная прибыль: {net_profit:.2f} USDT\n"
            f"• Активных сделок: {'Да' if trade_lock.locked() else 'Нет'}\n"
            f"• Последние сделки: {recent_trades.qsize()}"
        )
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Status command error: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

# === Основной цикл ===
async def main():
    # Инициализация Telegram
    await telegram_app.initialize()
    telegram_app.add_handler(CommandHandler("status", status))
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    
    # Загрузка рынков
    symbols, markets = await load_symbols()
    triangles = await find_triangles(symbols)
    
    await send_telegram_message(f"♻️ Бот запущен в режиме {'DEMO' if MODE == 'demo' else 'LIVE'}")
    logger.info(f"Loaded {len(triangles)} triangles")
    
    # Основной цикл
    while True:
        try:
            # Обновляем стаканы
            await update_orderbooks(symbols)
            
            # Проверяем треугольники
            tasks = [check_triangle(base, mid1, mid2, markets) for base, mid1, mid2 in triangles]
            await asyncio.gather(*tasks)
            
            # Очищаем кэш треугольников
            async with cache_lock:
                now = datetime.utcnow()
                expired = [k for k, v in triangle_cache.items() if (now - v).seconds > 3600]
                for k in expired:
                    del triangle_cache[k]
            
            # Очищаем историю сделок
            while not recent_trades.empty():
                trade_time = recent_trades.get_nowait()
                if (datetime.utcnow() - trade_time).seconds < 300:
                    recent_trades.put_nowait(trade_time)
                else:
                    logger.info(f"Cleared old trade from {trade_time}")
            
            await asyncio.sleep(10)
            
        except Exception as e:
            logger.exception(f"Main loop error: {e}")
            await asyncio.sleep(30)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        asyncio.run(telegram_app.stop())