
import ccxt.async_support as ccxt
import asyncio
import os
import hashlib
import math
from datetime import datetime, timedelta
from telegram.ext import Application, CommandHandler
from telegram.constants import ParseMode

# === .env настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
MODE = os.getenv("MODE", "demo").lower()

# === Telegram и биржа ===
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "spot",
        "recvWindow": 5000
    }
})
if MODE == "demo":
    exchange.set_sandbox_mode(True)
    print("⚙️ Работаем в DEMO режиме")
else:
    print("⚙️ Работаем в LIVE режиме")

# === Глобальные настройки ===
commission_rate = 0.001
min_profit = 0.1
target_volume_usdt = 100
start_coins = ['USDT', 'BTC', 'ETH']
debug_mode = True
triangle_hold_time = 5
log_file = "triangle_log.csv"
in_trade = False
triangle_cache = {}
net_profit = 0.0
recent_trades = []
MAX_DRAWDOWN = -5.0
MAX_TRADES_PER_MIN = 3

# === Telegram ===
async def send_telegram_message(text):
    try:
        await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        if debug_mode:
            print(f"[Ошибка Telegram]: {e}")

async def fetch_balances():
    try:
        balances = await exchange.fetch_balance()
        return balances["total"]
    except Exception as e:
        print(f"[Ошибка баланса]: {e}")
        return {}

# === Telegram /status команда ===
async def status(update, context):
    try:
        balances = await fetch_balances()
        usdt = balances.get("USDT", 0.0)
        await update.message.reply_text(
            f"🤖 Статус бота:\n"
            f"Режим: {'DEMO' if MODE == 'demo' else 'LIVE'}\n"
            f"Баланс USDT: {usdt:.2f}\n"
            f"Суммарный профит: {net_profit:.2f} USDT"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при получении статуса:\n{e}")

# === Вспомогательные функции ===
def log_route(base, mid1, mid2, profit, liquidity):
    with open(log_file, "a") as f:
        f.write(f"{datetime.utcnow()},{base}->{mid1}->{mid2}->{base},{profit:.4f},{liquidity}\n")

async def round_to_precision(value, precision):
    if precision == 0: return int(value)
    factor = 10 ** precision
    return math.floor(value * factor) / factor

async def get_symbol_meta(symbol, markets):
    m = markets.get(symbol)
    if not m: return None
    return {
        "price_precision": m['precision']['price'],
        "amount_precision": m['precision']['amount'],
        "min_cost": m['limits']['cost']['min'],
    }

async def get_avg_price(orderbook_side, target_usdt):
    total_base = 0
    total_usd = 0
    max_liquidity = 0
    for price, volume in orderbook_side:
        price, volume = float(price), float(volume)
        usd = price * volume
        max_liquidity += usd
        if total_usd + usd >= target_usdt:
            remain_usd = target_usdt - total_usd
            total_base += remain_usd / price
            total_usd += remain_usd
            break
        total_base += volume
        total_usd += usd
    if total_usd < target_usdt:
        return None, 0, max_liquidity
    return total_usd / total_base, total_usd, max_liquidity

async def get_execution_price(symbol, side, target_usdt):
    try:
        orderbook = await exchange.fetch_order_book(symbol)
        return await get_avg_price(orderbook['asks' if side == 'buy' else 'bids'], target_usdt)
    except Exception as e:
        print(f"[Ошибка стакана {symbol}]: {e}")
        return None, 0, 0

async def execute_trade_step(symbol, side, usdt_amount, exchange, markets):
    try:
        orderbook = await exchange.fetch_order_book(symbol)
        price = orderbook['asks'][0][0] if side == 'buy' else orderbook['bids'][0][0]
        meta = await get_symbol_meta(symbol, markets)
        if not meta: return None
        amount = usdt_amount / price if side == 'buy' else usdt_amount
        amount = await round_to_precision(amount, meta['amount_precision'])
        price = await round_to_precision(price, meta['price_precision'])
        if price * amount < meta['min_cost']: return None
        print(f"[🔄] Ордер: {side.upper()} {amount} {symbol} по {price}")
        return await exchange.create_order(symbol, "market", side, amount)
    except Exception as e:
        print(f"[❌] Ошибка при ордере {symbol} {side}: {e}")
        await send_telegram_message(f"❌ Ошибка при ордере {symbol} {side}: {e}")
        return None

async def execute_triangle_trade(base, mid1, mid2, symbols, exchange, markets, target_volume_usdt, profit_percent, pure_profit_usdt):
    global in_trade, net_profit, recent_trades
    if in_trade: return
    if net_profit < MAX_DRAWDOWN:
        await send_telegram_message(f"🛑 Max Drawdown достигнут: {net_profit:.2f} USDT.")
        return
    now = datetime.utcnow()
    recent_trades = [t for t in recent_trades if (now - t).total_seconds() < 60]
    if len(recent_trades) >= MAX_TRADES_PER_MIN: return
    in_trade = True
    try:
        print(f"🚀 Торговля: {base} → {mid1} → {mid2} → {base}")
        s1 = f"{mid1}/{base}" if f"{mid1}/{base}" in symbols else f"{base}/{mid1}"
        s2 = f"{mid2}/{mid1}" if f"{mid2}/{mid1}" in symbols else f"{mid1}/{mid2}"
        s3 = f"{mid2}/{base}" if f"{mid2}/{base}" in symbols else f"{base}/{mid2}"
        if not await execute_trade_step(s1, 'buy', target_volume_usdt, exchange, markets): return
        if not await execute_trade_step(s2, 'buy', target_volume_usdt, exchange, markets): return
        if not await execute_trade_step(s3, 'sell', target_volume_usdt, exchange, markets): return
        net_profit += pure_profit_usdt
        recent_trades.append(datetime.utcnow())
        await send_telegram_message(f"✅ <b>Сделка выполнена!</b>\n{base} → {mid1} → {mid2} → {base}\nПрофит: ${pure_profit_usdt:.2f} ({profit_percent:.2f}%)")
    finally:
        in_trade = False

async def load_symbols():
    markets = await exchange.load_markets()
    # Исключаем приостановленные и неактивные пары
    active_symbols = [s for s in markets if markets[s].get("active", True)]
    return active_symbols, markets

async def find_triangles(symbols):
    triangles = []
    for base in start_coins:
        for sym1 in symbols:
            if not sym1.endswith('/' + base): continue
            mid1 = sym1.split('/')[0]
            for sym2 in symbols:
                if not sym2.startswith(mid1 + '/'): continue
                mid2 = sym2.split('/')[1]
                third = f"{mid2}/{base}"
                if third in symbols or f"{base}/{mid2}" in symbols:
                    triangles.append((base, mid1, mid2))
    return triangles

async def check_triangle(base, mid1, mid2, symbols, markets):
    try:
        s1 = f"{mid1}/{base}" if f"{mid1}/{base}" in symbols else f"{base}/{mid1}"
        s2 = f"{mid2}/{mid1}" if f"{mid2}/{mid1}" in symbols else f"{mid1}/{mid2}"
        s3 = f"{mid2}/{base}" if f"{mid2}/{base}" in symbols else f"{base}/{mid2}"
        price1, _, liq1 = await get_execution_price(s1, "buy", target_volume_usdt)
        price2, _, liq2 = await get_execution_price(s2, "buy", target_volume_usdt)
        price3, _, liq3 = await get_execution_price(s3, "sell", target_volume_usdt)
        if not price1 or not price2 or not price3: return
        step1 = (1 / price1) * (1 - commission_rate)
        step2 = (1 / price2) * (1 - commission_rate)
        step3 = price3 * (1 - commission_rate)
        result = step1 * step2 * step3
        profit_percent = (result - 1) * 100
        if not (min_profit <= profit_percent <= 2.0): return
        min_liq = min(liq1, liq2, liq3)
        if min_liq < target_volume_usdt: return
        pure_profit_usdt = round((result - 1) * target_volume_usdt, 2)
        route_hash = hashlib.md5(f"{base}->{mid1}->{mid2}->{base}".encode()).hexdigest()
        now = datetime.utcnow()
        if triangle_cache.get(route_hash) and (now - triangle_cache[route_hash]).total_seconds() < triangle_hold_time:
            return
        triangle_cache[route_hash] = now
        await send_telegram_message(f"""
🟢 1. {s1} - {price1:.6f} (BUY)
🟡 2. {s2} - {price2:.6f} (BUY)
🟥 3. {s3} - {price3:.6f} (SELL)

💰 Прибыль: {pure_profit_usdt:.2f} USDT
📈 Спред: {profit_percent:.2f}%
💧 Мин. ликвидность: ${min_liq:.2f}
⚙️ Готов к сделке: ДА""")
        balances = await fetch_balances()
        if balances.get(base, 0) >= target_volume_usdt:
            await execute_triangle_trade(base, mid1, mid2, symbols, exchange, markets, target_volume_usdt, profit_percent, pure_profit_usdt)
    except Exception as e:
        print(f"[Ошибка маршрута]: {e}")
        await send_telegram_message(f"❌ Ошибка: {e}")

# === MAIN ===
async def main():
    await telegram_app.initialize()
    telegram_app.add_handler(CommandHandler("status", status))
    await telegram_app.start()
    await send_telegram_message("🤖 Бот запущен.")
    symbols, markets = await load_symbols()
    triangles = await find_triangles(symbols)
    while True:
        tasks = [check_triangle(base, mid1, mid2, symbols, markets) for base, mid1, mid2 in triangles]
        await asyncio.gather(*tasks)
        await asyncio.sleep(10)

if __name__ == '__main__':
    asyncio.run(main())
