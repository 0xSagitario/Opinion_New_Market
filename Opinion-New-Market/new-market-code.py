import asyncio
import logging
from typing import Dict, List, Set, Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import json
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    JobQueue
)
from telegram.constants import ParseMode
import pytz

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
OPINION_TRADE_API_URL = "https://api.opinion.trade/api/v1/markets"
CHECK_INTERVAL_SECONDS = 60  # How often to check for new markets
DATA_FILE = "bot_data.json"  # File to store user preferences and seen markets

# Opinion.Trade categories (update based on actual platform categories)
CATEGORIES = {
    "politics": "Politics",
    "crypto": "Cryptocurrency",
    "sports": "Sports",
    "entertainment": "Entertainment",
    "technology": "Technology",
    "finance": "Finance",
    "science": "Science",
    "other": "Other"
}

# ==================== DATA STRUCTURES ====================
@dataclass
class Market:
    """Represents a prediction market"""
    id: str
    question: str
    description: str
    category: str
    volume: float
    liquidity: float
    expiry: datetime
    url: str
    created_at: datetime
    tags: List[str]

@dataclass
class UserPreferences:
    """User notification preferences"""
    user_id: int
    enabled_categories: Set[str]
    keywords: List[str]
    min_liquidity: float
    min_volume: float
    notify_on_launch: bool
    last_notified: Dict[str, datetime]  # market_id -> last notified time

# ==================== BOT CORE ====================
class OpinionTradeMonitorBot:
    def __init__(self):
        self.application = None
        self.seen_markets: Set[str] = set()
        self.user_prefs: Dict[int, UserPreferences] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def init(self):
        """Initialize the bot"""
        await self.load_data()
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.setup_handlers()
        
        # Create aiohttp session for API calls
        self.session = aiohttp.ClientSession(
            headers={
                'User-Agent': 'Telegram-OpinionTradeBot/1.0',
                'Accept': 'application/json'
            }
        )
        
    async def close(self):
        """Cleanup resources"""
        if self.session:
            await self.session.close()
        await self.save_data()
    
    # ============= DATA PERSISTENCE =============
    async def load_data(self):
        """Load saved data from file"""
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                self.seen_markets = set(data.get('seen_markets', []))
                
                # Load user preferences
                for user_id_str, prefs in data.get('user_prefs', {}).items():
                    user_id = int(user_id_str)
                    self.user_prefs[user_id] = UserPreferences(
                        user_id=user_id,
                        enabled_categories=set(prefs['enabled_categories']),
                        keywords=prefs['keywords'],
                        min_liquidity=prefs['min_liquidity'],
                        min_volume=prefs['min_volume'],
                        notify_on_launch=prefs['notify_on_launch'],
                        last_notified=prefs['last_notified']
                    )
        except FileNotFoundError:
            logging.info("No existing data file found, starting fresh")
        except Exception as e:
            logging.error(f"Error loading data: {e}")
    
    async def save_data(self):
        """Save data to file"""
        data = {
            'seen_markets': list(self.seen_markets),
            'user_prefs': {}
        }
        
        for user_id, prefs in self.user_prefs.items():
            data['user_prefs'][str(user_id)] = {
                'enabled_categories': list(prefs.enabled_categories),
                'keywords': prefs.keywords,
                'min_liquidity': prefs.min_liquidity,
                'min_volume': prefs.min_volume,
                'notify_on_launch': prefs.notify_on_launch,
                'last_notified': prefs.last_notified
            }
        
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    
    # ============= API INTEGRATION =============
    async def fetch_markets(self) -> List[Market]:
        """Fetch recent markets from Opinion.Trade API"""
        try:
            async with self.session.get(
                OPINION_TRADE_API_URL,
                params={
                    'limit': 50,
                    'sort': 'newest',
                    'status': 'open'
                },
                timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return self.parse_markets(data)
                else:
                    logging.error(f"API error: {response.status}")
                    return []
        except Exception as e:
            logging.error(f"Error fetching markets: {e}")
            return []
    
    def parse_markets(self, api_data) -> List[Market]:
        """Parse API response into Market objects"""
        markets = []
        
        # Adjust this parsing based on actual Opinion.Trade API response structure
        for item in api_data.get('markets', []):
            try:
                market = Market(
                    id=str(item['id']),
                    question=item.get('question', 'No title'),
                    description=item.get('description', ''),
                    category=item.get('category', 'other').lower(),
                    volume=float(item.get('volume', 0)),
                    liquidity=float(item.get('liquidity', 0)),
                    expiry=datetime.fromisoformat(item['expiry'].replace('Z', '+00:00')),
                    url=f"https://opinion.trade/market/{item['id']}",
                    created_at=datetime.fromisoformat(item['created_at'].replace('Z', '+00:00')),
                    tags=item.get('tags', [])
                )
                markets.append(market)
            except (KeyError, ValueError) as e:
                logging.warning(f"Error parsing market data: {e}")
                continue
        
        return markets
    
    # ============= MARKET FILTERING =============
    def market_matches_preferences(self, market: Market, prefs: UserPreferences) -> bool:
        """Check if a market matches user's preferences"""
        # Check category
        if prefs.enabled_categories and market.category not in prefs.enabled_categories:
            return False
        
        # Check liquidity
        if market.liquidity < prefs.min_liquidity:
            return False
        
        # Check volume
        if market.volume < prefs.min_volume:
            return False
        
        # Check keywords
        if prefs.keywords:
            market_text = f"{market.question} {market.description}".lower()
            if not any(keyword.lower() in market_text for keyword in prefs.keywords):
                return False
        
        return True
    
    # ============= NOTIFICATION SYSTEM =============
    async def send_market_alert(self, user_id: int, market: Market):
        """Send alert about new market to user"""
        prefs = self.user_prefs.get(user_id)
        if not prefs or not prefs.notify_on_launch:
            return
        
        # Check if we recently notified about this market
        last_notified = prefs.last_notified.get(market.id)
        if last_notified and datetime.now() - last_notified < timedelta(hours=1):
            return
        
        # Format expiry time
        expiry_str = market.expiry.strftime("%Y-%m-%d %H:%M UTC")
        time_to_expiry = market.expiry - datetime.now(pytz.UTC)
        days_left = time_to_expiry.days
        
        # Create message
        message = (
            f"🎯 *New Market Launched!*\n\n"
            f"*{market.question}*\n\n"
            f"📊 *Volume:* ${market.volume:,.2f}\n"
            f"💧 *Liquidity:* ${market.liquidity:,.2f}\n"
            f"📈 *Category:* {CATEGORIES.get(market.category, market.category)}\n"
            f"⏰ *Expires:* {expiry_str} ({days_left} days)\n"
            f"🏷️ *Tags:* {', '.join(market.tags[:5]) if market.tags else 'None'}\n\n"
            f"[View Market ↗]({market.url})"
        )
        
        try:
            await self.application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False
            )
            
            # Update last notified time
            prefs.last_notified[market.id] = datetime.now()
            
        except Exception as e:
            logging.error(f"Failed to send alert to {user_id}: {e}")
    
    async def check_new_markets(self, context: ContextTypes.DEFAULT_TYPE):
        """Periodic job to check for new markets"""
        logging.info("Checking for new markets...")
        
        markets = await self.fetch_markets()
        new_markets = [m for m in markets if m.id not in self.seen_markets]
        
        for market in new_markets:
            logging.info(f"New market detected: {market.question}")
            self.seen_markets.add(market.id)
            
            # Notify users who have matching preferences
            for user_id, prefs in self.user_prefs.items():
                if self.mark_matches_preferences(market, prefs):
                    await self.send_market_alert(user_id, market)
        
        await self.save_data()
    
    # ============= TELEGRAM COMMAND HANDLERS =============
    def setup_handlers(self):
        """Setup Telegram command handlers"""
        
        # Command handlers
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("help", self.cmd_help))
        self.application.add_handler(CommandHandler("settings", self.cmd_settings))
        self.application.add_handler(CommandHandler("categories", self.cmd_categories))
        self.application.add_handler(CommandHandler("keywords", self.cmd_keywords))
        self.application.add_handler(CommandHandler("filters", self.cmd_filters))
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("test", self.cmd_test_alert))
        
        # Callback query handlers for buttons
        self.application.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^cat_"))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^toggle_"))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^set_"))
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        
        # Initialize user preferences if not exists
        if user_id not in self.user_prefs:
            self.user_prefs[user_id] = UserPreferences(
                user_id=user_id,
                enabled_categories=set(CATEGORIES.keys()),  # All categories by default
                keywords=[],
                min_liquidity=0,
                min_volume=0,
                notify_on_launch=True,
                last_notified={}
            )
            await self.save_data()
        
        welcome_msg = (
            "🤖 *Opinion.Trade Market Monitor*\n\n"
            "I'll notify you when new prediction markets launch!\n\n"
            "*Commands:*\n"
            "• /settings - Configure notifications\n"
            "• /categories - Choose market categories\n"
            "• /keywords - Set keyword filters\n"
            "• /filters - Set liquidity/volume filters\n"
            "• /status - View current settings\n"
            "• /help - Show help\n\n"
            "I check for new markets every 60 seconds."
        )
        
        await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN)
    
    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show settings menu"""
        user_id = update.effective_user.id
        prefs = self.user_prefs.get(user_id)
        
        if not prefs:
            await update.message.reply_text("Please use /start first")
            return
        
        keyboard = [
            [InlineKeyboardButton("📁 Categories", callback_data="cat_menu")],
            [InlineKeyboardButton("🔍 Keywords", callback_data="set_keywords")],
            [InlineKeyboardButton("💰 Liquidity/Volume", callback_data="set_filters")],
            [InlineKeyboardButton(f"🔔 {'Disable' if prefs.notify_on_launch else 'Enable'} Alerts", 
                                 callback_data="toggle_alerts")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status = "enabled" if prefs.notify_on_launch else "disabled"
        msg = f"*Current Settings:*\n• Alerts: {status}\n• Categories: {len(prefs.enabled_categories)} selected\n• Keywords: {len(prefs.keywords)}\n• Min liquidity: ${prefs.min_liquidity}\n• Min volume: ${prefs.min_volume}"
        
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    
    async def cmd_categories(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show category selection"""
        await self.show_category_menu(update.effective_chat.id)
    
    async def show_category_menu(self, chat_id: int):
        """Display category selection menu"""
        user_id = chat_id
        prefs = self.user_prefs.get(user_id)
        
        if not prefs:
            return
        
        keyboard = []
        for cat_id, cat_name in CATEGORIES.items():
            is_selected = "✅" if cat_id in prefs.enabled_categories else "⬜"
            keyboard.append([InlineKeyboardButton(
                f"{is_selected} {cat_name}", 
                callback_data=f"cat_{cat_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_settings")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await self.application.bot.send_message(
            chat_id=chat_id,
            text="*Select categories to monitor:*\nClick to toggle",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    
    async def cmd_keywords(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set keyword filters"""
        if context.args:
            keywords = [k.strip() for k in " ".join(context.args).split(",")]
            user_id = update.effective_user.id
            if user_id in self.user_prefs:
                self.user_prefs[user_id].keywords = keywords
                await self.save_data()
                await update.message.reply_text(f"✅ Keywords set: {', '.join(keywords)}")
        else:
            await update.message.reply_text(
                "Usage: /keywords <comma-separated list>\n"
                "Example: /keywords bitcoin, ethereum, election"
            )
    
    async def cmd_filters(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set liquidity and volume filters"""
        user_id = update.effective_user.id
        prefs = self.user_prefs.get(user_id)
        
        if not prefs:
            return
        
        if len(context.args) >= 2:
            try:
                min_liquidity = float(context.args[0])
                min_volume = float(context.args[1])
                
                prefs.min_liquidity = min_liquidity
                prefs.min_volume = min_volume
                await self.save_data()
                
                await update.message.reply_text(
                    f"✅ Filters updated:\n"
                    f"• Min liquidity: ${min_liquidity:,.2f}\n"
                    f"• Min volume: ${min_volume:,.2f}"
                )
            except ValueError:
                await update.message.reply_text("Please enter valid numbers")
        else:
            await update.message.reply_text(
                "Usage: /filters <min_liquidity> <min_volume>\n"
                "Example: /filters 100 500\n"
                "Current: "
                f"Liquidity: ${prefs.min_liquidity:,.2f}, "
                f"Volume: ${prefs.min_volume:,.2f}"
            )
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current status and settings"""
        user_id = update.effective_user.id
        prefs = self.user_prefs.get(user_id)
        
        if not prefs:
            await update.message.reply_text("Please use /start first")
            return
        
        enabled_cats = [CATEGORIES.get(c, c) for c in prefs.enabled_categories]
        
        status_msg = (
            f"*Status for @{update.effective_user.username}*\n\n"
            f"🔔 Alerts: {'✅ Enabled' if prefs.notify_on_launch else '❌ Disabled'}\n"
            f"📁 Categories ({len(enabled_cats)}): {', '.join(enabled_cats)}\n"
            f"🔍 Keywords ({len(prefs.keywords)}): {', '.join(prefs.keywords) if prefs.keywords else 'None'}\n"
            f"💰 Min liquidity: ${prefs.min_liquidity:,.2f}\n"
            f"📊 Min volume: ${prefs.min_volume:,.2f}\n\n"
            f"*Monitoring Stats:*\n"
            f"• Markets tracked: {len(self.seen_markets)}\n"
            f"• Check interval: {CHECK_INTERVAL_SECONDS} seconds"
        )
        
        await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)
    
    async def cmd_test_alert(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a test alert"""
        test_market = Market(
            id="test_123",
            question="Test: Will this notification work?",
            description="This is a test market to verify notifications are working properly.",
            category="crypto",
            volume=1500.50,
            liquidity=750.25,
            expiry=datetime.now() + timedelta(days=30),
            url="https://opinion.trade/market/test",
            created_at=datetime.now(),
            tags=["test", "notification"]
        )
        
        await self.send_market_alert(update.effective_user.id, test_market)
        await update.message.reply_text("✅ Test alert sent!")
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help message"""
        help_text = (
            "🤖 *Opinion.Trade Market Monitor Help*\n\n"
            "*Commands:*\n"
            "• /start - Initialize bot\n"
            "• /settings - Configure notifications\n"
            "• /categories - Select market categories\n"
            "• /keywords <words> - Filter by keywords (comma-separated)\n"
            "• /filters <liquidity> <volume> - Set minimum amounts\n"
            "• /status - View current settings\n"
            "• /test - Send test alert\n"
            "• /help - This message\n\n"
            "*How it works:*\n"
            "1. I check Opinion.Trade every 60 seconds\n"
            "2. When new markets launch, I check against your filters\n"
            "3. If they match, you get an alert with market details\n\n"
            "*Filtering options:*\n"
            "• Select specific categories\n"
            "• Set keyword filters\n"
            "• Minimum liquidity requirement\n"
            "• Minimum volume requirement"
        )
        
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
    
    # ============= CALLBACK HANDLERS =============
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        data = query.data
        
        if data.startswith("cat_"):
            # Toggle category
            category = data[4:]
            prefs = self.user_prefs.get(user_id)
            
            if prefs:
                if category in prefs.enabled_categories:
                    prefs.enabled_categories.remove(category)
                else:
                    prefs.enabled_categories.add(category)
                
                await self.save_data()
                await self.show_category_menu(user_id)
        
        elif data == "cat_menu":
            await self.show_category_menu(user_id)
        
        elif data == "toggle_alerts":
            prefs = self.user_prefs.get(user_id)
            if prefs:
                prefs.notify_on_launch = not prefs.notify_on_launch
                await self.save_data()
                await self.cmd_settings(update, context)
        
        elif data == "set_keywords":
            await query.edit_message_text(
                "Send: /keywords <comma-separated list>\nExample: /keywords bitcoin, election, trump"
            )
        
        elif data == "set_filters":
            await query.edit_message_text(
                "Send: /filters <min_liquidity> <min_volume>\nExample: /filters 100 500"
            )
        
        elif data == "back_settings":
            await self.cmd_settings(update, context)
        
        # Delete the callback message
        try:
            await query.delete_message()
        except:
            pass

# ==================== MAIN ====================
async def main():
    """Main function"""
    # Setup logging
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    # Create and initialize bot
    bot = OpinionTradeMonitorBot()
    await bot.init()
    
    try:
        # Add periodic job
        job_queue = bot.application.job_queue
        job_queue.run_repeating(
            bot.check_new_markets,
            interval=CHECK_INTERVAL_SECONDS,
            first=10
        )
        
        logging.info("Bot starting...")
        await bot.application.initialize()
        await bot.application.start()
        await bot.application.updater.start_polling()
        
        # Keep running
        while True:
            await asyncio.sleep(3600)  # Sleep for 1 hour
        
    except KeyboardInterrupt:
        logging.info("Bot shutting down...")
    finally:
        await bot.application.stop()
        await bot.close()

if __name__ == "__main__":
    # Create and run event loop
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()