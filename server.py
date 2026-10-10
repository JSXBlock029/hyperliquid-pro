#!/usr/bin/env python3
"""
Hyperliquid TradingView Webhook Server
Receives webhooks from TradingView and places orders on Hyperliquid

Rules for changes to this file:
- Never place a trading order without explicit user confirmation.
- Use environment variables for anything sensitive; never hardcode keys or addresses.
- Test in paper mode first (SCALP_LIVE=false).
"""

# Apply inspect module patch for compatibility
import patch_inspect

import os
import json
import logging
from decimal import Decimal
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit, join_room, leave_room
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account
from dotenv import load_dotenv
from datetime import datetime, timedelta
import threading
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import numpy as np
import openai
from openai import OpenAI
import asyncio
import json

# Import the backtester
from backtester import WebBacktester

# Scalp mode (local engine + local-LLM agent). See scalp_controller.py.
from scalp_controller import ScalpController

# Load environment variables
load_dotenv()

# Rate limiting and caching for API endpoints
api_call_timestamps = {}
api_cache = {}
API_RATE_LIMIT_SECONDS = 30  # Minimum 30 seconds between API calls
CACHE_DURATION = 60  # Cache results for 60 seconds

def rate_limit_check(endpoint_name):
    """Check if enough time has passed since last API call"""
    current_time = time.time()
    last_call = api_call_timestamps.get(endpoint_name, 0)
    
    if current_time - last_call < API_RATE_LIMIT_SECONDS:
        return False
    
    api_call_timestamps[endpoint_name] = current_time
    return True

def get_cached_result(cache_key):
    """Get cached result if still valid"""
    if cache_key in api_cache:
        cached_data, timestamp = api_cache[cache_key]
        if time.time() - timestamp < CACHE_DURATION:
            return cached_data
    return None

def set_cache_result(cache_key, data):
    """Cache result with timestamp"""
    api_cache[cache_key] = (data, time.time())

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()  # Keep console output
    ]
)
logger = logging.getLogger(__name__)

# Reduce noise from Flask and SocketIO
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)
logging.getLogger('engineio').setLevel(logging.WARNING)

# Initialize Flask app
app = Flask(__name__, static_folder='public', static_url_path='')


# --- Access control -------------------------------------------------------------
# Order, close and scalp-toggle routes move real money. Requests from the local machine
# are trusted (the bundled UI runs there). State-changing requests from anywhere else
# must carry HLPRO_API_TOKEN, as an "X-API-Token" header or a "?token=" query parameter
# (TradingView webhooks cannot set headers, so use the query form there). With no token
# configured, remote writes are refused outright.
import hmac as _hmac

_LOCAL_ADDRS = {"127.0.0.1", "::1"}


@app.before_request
def _require_token_for_remote_writes():
    if request.method in ("GET", "HEAD", "OPTIONS") or request.remote_addr in _LOCAL_ADDRS:
        return None
    expected = os.getenv("HLPRO_API_TOKEN", "")
    supplied = request.headers.get("X-API-Token") or request.args.get("token", "")
    if expected and supplied and _hmac.compare_digest(supplied.encode(), expected.encode()):
        return None
    return jsonify({"error": "unauthorized"}), 401
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=1e8
)

# Configuration
MAINNET_API_URL = constants.MAINNET_API_URL
RISK_PERCENTAGE = float(os.getenv('RISK_PERCENTAGE', '20'))  # 20% default
MIN_POSITION_VALUE = 10  # Minimum $10 position

# Server configuration - Read from .env file
SERVER_HOST = os.getenv('SERVER_HOST') or '127.0.0.1'  # set SERVER_HOST=0.0.0.0 to expose, and set HLPRO_API_TOKEN
SERVER_PORT = int(os.getenv('PORT') or '3000')  # Read PORT from .env file
API_TIMEOUT = int(os.getenv('API_TIMEOUT') or '30')  # Timeout in seconds for API requests
API_RETRIES = int(os.getenv('API_RETRIES') or '3')   # Number of retries for API requests

# Create a custom session with retry logic
session = requests.Session()
retry_strategy = Retry(
    total=API_RETRIES,
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)
session.timeout = API_TIMEOUT

# Initialize OpenAI client
openai_client = None
if os.getenv('OPENAI_API_KEY'):
    try:
        openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        logger.info("OpenAI client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize OpenAI client: {e}")
else:
    logger.warning("No OpenAI API key found - AI agent features disabled")

# Initialize Hyperliquid clients
info = Info(MAINNET_API_URL, skip_ws=True)

# Initialize exchange client only if private key is available
exchange = None
account = None

# Use API wallet for trading - only API wallet can place trades on Hyperliquid
# Main wallet can only view positions and transfer funds
api_wallet_key = os.getenv('HYPERLIQUID_API_PRIVATE_KEY')
main_wallet_key = os.getenv('HYPERLIQUID_MAIN_PRIVATE_KEY')

# API wallet is required for trading
trading_wallet_key = api_wallet_key

if trading_wallet_key:
    try:
        account = Account.from_key(trading_wallet_key)
        exchange = Exchange(account, MAINNET_API_URL)
        logger.info(f"Exchange initialized for address: {account.address}")
        
        # Log which key was used with clear indication
        if api_wallet_key:
            logger.info("✅ USING API WALLET for trading (HYPERLIQUID_API_PRIVATE_KEY) - Required for trades")
        else:
            logger.warning("⚠️  No API wallet configured - trading will not work")
            
    except Exception as e:
        logger.error(f"Failed to initialize exchange: {e}")
else:
    logger.warning("No private key found - running in simulation mode")

# Store recent activity in memory
recent_activity = []
max_activity_items = 100

# Connection status
connection_status = {
    'connected': True,
    'last_check': datetime.now().isoformat(),
    'mode': 'real' if exchange else 'simulation',
    'wallet': account.address if account else os.getenv('HYPERLIQUID_API_WALLET'),
    'wallet_type': 'API' if api_wallet_key and account else 'MAIN' if main_wallet_key and account else 'NONE'
}

def update_connection_status():
    """Update connection status and emit to all clients"""
    try:
        # Test API connection with retry logic
        max_retries = 3
        retry_delay = 1  # seconds
        was_connected = connection_status['connected']
        
        for attempt in range(max_retries):
            try:
                meta_response = info.meta()
                connection_status['connected'] = True
                if not was_connected:
                    logger.info("Successfully reconnected to Hyperliquid API")
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    connection_status['connected'] = False
                    if was_connected:
                        logger.error(f"Lost connection to Hyperliquid API: {str(e)}")
    except Exception as e:
        connection_status['connected'] = False
        logger.error(f"Unexpected error in connection status update: {str(e)}")
    
    connection_status['last_check'] = datetime.now().isoformat()
    socketio.emit('connection_status', connection_status)

def background_status_check():
    """Background thread to check connection status"""
    while True:
        update_connection_status()
        time.sleep(30)  # Check every 30 seconds to reduce API calls

# Start background status check
status_thread = threading.Thread(target=background_status_check, daemon=True)
status_thread.start()

def add_activity(type, message, data=None):
    """Add an activity to the recent activity list"""
    activity = {
        'timestamp': datetime.now().isoformat(),
        'type': type,
        'message': message,
        'data': data
    }
    recent_activity.insert(0, activity)
    if len(recent_activity) > max_activity_items:
        recent_activity.pop()
    return activity

def get_asset_info(symbol):
    """Get asset information including index and decimals"""
    try:
        meta = info.meta()
        for i, asset in enumerate(meta['universe']):
            if asset['name'] == symbol:
                return {
                    'index': i,
                    'name': asset['name'],
                    'szDecimals': asset['szDecimals'],
                    'maxLeverage': asset['maxLeverage']
                }
        return None
    except Exception as e:
        logger.error(f"Error getting asset info: {e}")
        return None

def get_current_price(symbol):
    """Get current mid price for an asset"""
    try:
        all_mids = info.all_mids()
        # Try symbol name first (e.g., "SOL")
        price = all_mids.get(symbol)
        if price:
            return float(price)
        # If not found, log available keys for debugging
        logger.warning(f"Price not found for {symbol}. Available keys sample: {list(all_mids.keys())[:10]}")
        return None
    except Exception as e:
        logger.error(f"Error getting price: {e}")
        return None

def get_account_balance(address):
    """Get account balance"""
    try:
        logger.info(f"Getting balance for address: {address}")
        user_state = info.user_state(address)
        logger.info(f"Raw user_state: {user_state.get('crossMarginSummary', {})}")
        
        account_value = float(user_state['crossMarginSummary']['accountValue'])
        margin_used = float(user_state['crossMarginSummary']['totalMarginUsed'])
        available = account_value - margin_used
        
        logger.info(f"Account value: ${account_value}")
        logger.info(f"Margin used: ${margin_used}")
        logger.info(f"Available balance: ${available}")
        
        return available
    except Exception as e:
        logger.error(f"Error getting account balance: {e}")
        return None

def round_size(size, sz_decimals):
    """Round size according to Hyperliquid requirements"""
    # First apply 5 significant figures
    size_str = f"{size:.5g}"
    size_5sf = float(size_str)
    
    # Then round to sz_decimals
    multiplier = 10 ** sz_decimals
    rounded = int(size_5sf * multiplier) / multiplier
    
    return rounded

@app.route('/')
def index():
    """Serve the main page"""
    return app.send_static_file('index.html')

def get_wallet_info():
    """Get detailed wallet information"""
    wallet_info = {
        'trading_wallet': account.address if account else None,
        'wallet_type': 'API' if api_wallet_key and account else 'MAIN' if main_wallet_key and account else 'NONE',
        'api_wallet_address': os.getenv('HYPERLIQUID_API_WALLET'),
        'main_wallet_address': os.getenv('HYPERLIQUID_MAIN_WALLET'),
        'api_wallet_configured': bool(api_wallet_key),
        'main_wallet_configured': bool(main_wallet_key)
    }
    return wallet_info

@app.route('/api/status', methods=['GET'])
def get_status():
    """Get bot status"""
    status = connection_status.copy()
    status.update(get_wallet_info())
    return jsonify(status)

@app.route('/api/activity', methods=['GET'])
def get_activity():
    """Get recent trading activity from Hyperliquid"""
    # Rate limiting check
    if not rate_limit_check('activity'):
        return jsonify({'error': 'Rate limited - please wait before requesting activity again', 'activity': []}), 429
    
    try:
        # Use the same wallet that's being used for trading
        wallet_address = account.address if account else os.getenv('HYPERLIQUID_API_WALLET')
        
        # Get time range for recent activity (default: last 7 days)
        from datetime import datetime, timedelta
        
        days = request.args.get('days', '7')
        try:
            days = int(days)
        except:
            days = 7
            
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        start_time_ms = int(start_time.timestamp() * 1000)
        end_time_ms = int(end_time.timestamp() * 1000)
        
        logger.info(f"Fetching trading activity from {start_time} to {end_time}")
        
        # Get user fills with trading activity
        fills = info.user_fills_by_time(wallet_address, start_time_ms, end_time_ms)
        
        activity_list = []
        
        for fill in fills:
            # Convert Hyperliquid fill data to activity format
            # Convert timestamp to proper format
            timestamp_ms = fill.get('time', 0)
            if timestamp_ms:
                # Convert from milliseconds to seconds, then to datetime
                timestamp_dt = datetime.fromtimestamp(timestamp_ms / 1000)
                formatted_time = timestamp_dt.isoformat()
            else:
                formatted_time = datetime.now().isoformat()
                
            activity = {
                'time': formatted_time,  # Properly formatted datetime
                'timestamp': timestamp_ms,  # Keep original timestamp for sorting
                'instrumentId': fill.get('coin', 'Unknown'),
                'type': 'futures',  # Hyperliquid is derivatives
                'side': fill.get('side', 'Unknown').upper(),
                'qty': float(fill.get('sz', 0)),
                'price': float(fill.get('px', 0)),
                'status': 'Filled',
                'orderId': fill.get('oid', 'N/A'),
                'dir': fill.get('dir', 'Unknown'),  # Open/Close
                'closedPnl': float(fill.get('closedPnl', 0)) if fill.get('closedPnl') else None,
                'hash': fill.get('hash', 'N/A')
            }
            activity_list.append(activity)
        
        # Sort by timestamp (most recent first)
        activity_list.sort(key=lambda x: x['timestamp'], reverse=True)
        
        # Limit to 50 most recent trades
        activity_list = activity_list[:50]
        
        logger.info(f"Returning {len(activity_list)} trading activities")
        return jsonify(activity_list)
        
    except Exception as e:
        logger.error(f"Error getting trading activity: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        # Return empty activity list on error
        return jsonify([])

@app.route('/api/positions', methods=['GET'])
def get_positions():
    """Get current positions with detailed P&L calculations"""
    # Rate limiting check
    if not rate_limit_check('positions'):
        return jsonify({'error': 'Rate limited - please wait before requesting positions again', 'positions': []}), 429
    
    try:
        # Use API wallet address for positions (seamless trading setup)
        wallet_address = account.address if account else (os.getenv('HYPERLIQUID_MAIN_WALLET') or '')
        logger.info(f"DEBUG: Fetching positions for API wallet: {wallet_address}")
        
        user_state = info.user_state(wallet_address)
        logger.info(f"DEBUG: Got user_state with keys: {list(user_state.keys())}")
        
        if 'assetPositions' not in user_state:
            logger.error("DEBUG: No assetPositions in user_state")
            return jsonify([])
        
        asset_positions = user_state['assetPositions']
        logger.info(f"DEBUG: Found {len(asset_positions)} asset positions")
        
        all_mids = info.all_mids()
        logger.info(f"DEBUG: Got {len(all_mids)} price data points")
        
        positions = []
        for i, position in enumerate(asset_positions):
            logger.info(f"DEBUG: Processing position {i}")
            
            if 'position' not in position:
                logger.error(f"DEBUG: Position {i} missing 'position' key")
                continue
                
            pos_data = position['position']
            
            if 'szi' not in pos_data:
                logger.error(f"DEBUG: Position {i} missing 'szi' key")
                continue
                
            size_str = pos_data['szi']
            
            if size_str == '0':
                logger.info(f"DEBUG: Position {i} has zero size, skipping")
                continue
                
            # Extract position details (simplified like test endpoint)
            symbol = pos_data['coin']
            current_price = float(all_mids.get(symbol, 0))
            size = float(size_str)
            entry_price = float(pos_data['entryPx'])
            
            logger.info(f"DEBUG: Position {i} - Symbol: {symbol}, Size: {size}, Entry: {entry_price}, Current: {current_price}")
            
            # Use Hyperliquid's native P&L first, fallback to manual calculation
            unrealized_pnl = None
            if 'unrealizedPnl' in pos_data:
                unrealized_pnl = float(pos_data['unrealizedPnl'])
                logger.info(f"DEBUG: Using Hyperliquid P&L for {symbol}: ${unrealized_pnl}")
            else:
                # Fallback to manual calculation
                unrealized_pnl = size * (current_price - entry_price)
                logger.info(f"DEBUG: Using manual P&L calculation for {symbol}: ${unrealized_pnl}")
            
            pnl_percentage = (unrealized_pnl / (abs(size) * entry_price)) * 100 if size != 0 else 0
            
            position_result = {
                'symbol': symbol,
                'side': 'LONG' if size > 0 else 'SHORT',
                'size': abs(size),
                'entryPrice': entry_price,
                'currentPrice': current_price,
                'unrealizedPnl': unrealized_pnl,
                'pnlPercentage': pnl_percentage,
            }
            
            positions.append(position_result)
            logger.info(f"DEBUG: Added position: {position_result}")
        
        logger.info(f"DEBUG: Returning {len(positions)} positions")
        return jsonify(positions)
        
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify([])

@app.route('/api/balance', methods=['GET'])
def get_balance():
    """Get account balance for specified wallet type or current trading wallet"""
    # Rate limiting check
    if not rate_limit_check('balance'):
        return jsonify({'error': 'Rate limited - please wait before requesting balance again', 'balance': 0}), 429
    
    try:
        wallet_type = request.args.get('type', 'current')
        
        if wallet_type == 'main':
            # Get main wallet balance
            main_wallet_address = os.getenv('HYPERLIQUID_MAIN_WALLET')
            if not main_wallet_address:
                return jsonify({
                    'error': 'Main wallet address not configured',
                    'accountValue': 0,
                    'availableBalance': 0,
                    'marginUsed': 0,
                    'unrealizedPnl': 0,
                    'wallet': ''
                })
            wallet_address = main_wallet_address
            
        elif wallet_type == 'api':
            # Get API wallet balance
            if not account:
                return jsonify({
                    'error': 'API wallet not configured',
                    'accountValue': 0,
                    'availableBalance': 0,
                    'marginUsed': 0,
                    'unrealizedPnl': 0,
                    'wallet': ''
                })
            wallet_address = account.address
            
        else:
            # Use API wallet for balance (where the trading funds are)
            wallet_address = account.address if account else os.getenv('HYPERLIQUID_API_WALLET')
            if not wallet_address:
                wallet_address = os.getenv('HYPERLIQUID_MAIN_WALLET') or ''
        
        logger.info(f"Checking balance for {wallet_type} wallet: {wallet_address}")
        
        user_state = info.user_state(wallet_address)
        
        return jsonify({
            'accountValue': float(user_state['crossMarginSummary']['accountValue']),
            'availableBalance': float(user_state['crossMarginSummary']['accountValue']) - 
                               float(user_state['crossMarginSummary']['totalMarginUsed']),
            'marginUsed': float(user_state['crossMarginSummary']['totalMarginUsed']),
            'unrealizedPnl': float(user_state['crossMarginSummary']['totalUnrealizedPnl']),
            'wallet': wallet_address
        })
    except Exception as e:
        logger.error(f"Error getting balance: {e}")
        return jsonify({
            'accountValue': 0,
            'availableBalance': 0,
            'marginUsed': 0,
            'unrealizedPnl': 0,
            'wallet': wallet_address if 'wallet_address' in locals() else ''
        })

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'mode': 'real' if exchange else 'simulation',
        'wallet': account.address if account else 'N/A'
    })

@app.route('/api/debug/positions', methods=['GET'])
def debug_positions():
    """Debug endpoint to see raw position data"""
    try:
        # Use API wallet address for positions (seamless trading setup)
        wallet_address = account.address if account else (os.getenv('HYPERLIQUID_MAIN_WALLET') or '')
        logger.info(f"DEBUG: Fetching positions for API wallet: {wallet_address}")
        
        user_state = info.user_state(wallet_address)
        all_mids = info.all_mids()
        
        debug_data = {
            'wallet_address': wallet_address,
            'has_asset_positions': 'assetPositions' in user_state,
            'asset_positions_count': len(user_state.get('assetPositions', [])),
            'all_mids_sample': {k: v for k, v in list(all_mids.items())[:5]},  # First 5 prices
            'positions': []
        }
        
        if 'assetPositions' in user_state:
            for i, position in enumerate(user_state['assetPositions']):
                if position['position']['szi'] != '0':
                    symbol = position['position']['coin']
                    current_price = float(all_mids.get(symbol, 0))
                    size = float(position['position']['szi'])
                    entry_price = float(position['position']['entryPx'])
                    
                    # Raw calculation
                    unrealized_pnl = size * (current_price - entry_price)
                    pnl_percentage = (unrealized_pnl / (abs(size) * entry_price)) * 100 if size != 0 else 0
                    
                    position_debug = {
                        'index': i,
                        'symbol': symbol,
                        'raw_position_data': position['position'],
                        'size': size,
                        'entry_price': entry_price,
                        'current_price': current_price,
                        'price_in_all_mids': all_mids.get(symbol),
                        'price_difference': current_price - entry_price,
                        'unrealized_pnl': unrealized_pnl,
                        'pnl_percentage': pnl_percentage,
                        'side': 'LONG' if size > 0 else 'SHORT'
                    }
                    debug_data['positions'].append(position_debug)
        
        return jsonify(debug_data)
    except Exception as e:
        logger.error(f"Debug positions error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/webhook', methods=['POST', 'OPTIONS'])
def webhook():
    """Handle TradingView webhooks with enhanced technical analysis"""
    # Handle CORS preflight requests
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST')
        return response
    
    try:
        # Parse webhook data - handle different content types
        data = None
        raw_data = None
        content_type = request.content_type or 'unknown'
        
        # Log incoming webhook details
        logger.info(f"Webhook received - Content-Type: {content_type}")
        
        # Method 1: Try Flask's built-in JSON parsing
        try:
            if request.is_json:
                data = request.get_json()
                logger.info("Successfully parsed using request.get_json()")
        except Exception as e:
            logger.debug(f"Method 1 failed: {e}")
        
        # Method 2: Try parsing raw data as JSON (handles text/plain)
        if not data:
            try:
                raw_data = request.get_data(as_text=True)
                if raw_data:
                    data = json.loads(raw_data)
                    logger.info("Successfully parsed raw data as JSON")
            except Exception as e:
                logger.debug(f"Method 2 failed: {e}")
        
        # Method 3: Try form data parsing
        if not data and request.form:
            try:
                form_data = request.form.get('data') or request.form.get('payload')
                if not form_data and request.form:
                    # Sometimes the JSON is the key itself
                    form_data = list(request.form.keys())[0]
                if form_data:
                    data = json.loads(form_data)
                    logger.info("Successfully parsed form data as JSON")
            except Exception as e:
                logger.debug(f"Method 3 failed: {e}")
        
        # Method 4: Try to parse as URL-encoded data
        if not data:
            try:
                raw_data = raw_data or request.get_data(as_text=True)
                if raw_data and '=' in raw_data:
                    # Might be URL-encoded
                    from urllib.parse import parse_qs, unquote
                    parsed = parse_qs(raw_data)
                    for key, value in parsed.items():
                        try:
                            data = json.loads(unquote(value[0]))
                            logger.info("Successfully parsed URL-encoded data as JSON")
                            break
                        except:
                            continue
            except Exception as e:
                logger.debug(f"Method 4 failed: {e}")
         
        if not data:
            logger.error(f"Failed to parse webhook data using all methods.")
            logger.error(f"Content-Type: {content_type}")
            logger.error(f"Raw data: {raw_data or request.get_data()}")
            logger.error(f"Form data: {dict(request.form)}")
            logger.error(f"Headers: {dict(request.headers)}")
            return jsonify({'error': 'No data received'}), 400

        # Handle different symbol field formats
        symbol = data.get('symbol') or data.get('instrument_id') or data.get('pair')
        
        # Extract symbol from instrument_id if needed (e.g., "SOL-USDT-PERP" -> "SOL")
        if symbol and '-' in symbol:
            symbol = symbol.split('-')[0]
        
        # Handle different signal type formats
        signal_type = (
            data.get('signal_type') or 
            data.get('side') or 
            data.get('action')
        )
        if signal_type:
            signal_type = signal_type.lower()
        
        # Get technical indicators from the webhook
        rsi = float(data.get('rsi', 50))  # Default to neutral
        macd = float(data.get('macd', 0))
        signal_line = float(data.get('signal', 0))
        adx = float(data.get('adx', 0))
        bb_upper = float(data.get('bb_upper', 0))
        bb_middle = float(data.get('bb_middle', 0))
        bb_lower = float(data.get('bb_lower', 0))
        ema_fast = float(data.get('ema_fast', 0))
        ema_slow = float(data.get('ema_slow', 0))
        volume = float(data.get('volume', 0))
        vol_ma = float(data.get('vol_ma', 0))
        atr = float(data.get('atr', 0))
        
        # If signal_type is not provided, infer from BB fields
        if not signal_type:
            if bb_upper > 0 and bb_middle == 0 and bb_lower == 0:
                signal_type = 'buy_short'
            elif bb_lower > 0 and bb_middle == 0 and bb_upper == 0:
                signal_type = 'buy_long'
            elif bb_middle > 0 and bb_upper == 0 and bb_lower == 0:
                signal_type = 'sell'
            else:
                logger.warning(f"Could not infer signal_type from BB fields: upper={bb_upper}, middle={bb_middle}, lower={bb_lower}")
                signal_type = ''
        
        # Trading parameters
        leverage = int(data.get('leverage', 10))  # Default to 10x leverage
        stop_loss_percent = float(data.get('stop_loss_percent', 2))  # Default 2% stop loss
        take_profit_percent = float(data.get('take_profit_percent', 5))  # Default 5% take profit
        
        # Check market type - Hyperliquid primarily supports perpetual trading
        market_type = data.get('market_type', 'perpetual').lower()
        order_type = data.get('order_type', 'perpetual').lower()
        
        # Handle spot trading - convert to perpetual or provide clear error
        if market_type == 'spot' or order_type == 'spot':
            logger.warning(f"Spot trading requested for {symbol}, but Hyperliquid primarily supports perpetual trading")
            logger.info(f"Converting spot order to perpetual order for {symbol}")
            # Continue with perpetual trading logic
            market_type = 'perpetual'
        
        # If signal_type is still empty, try to infer from other fields
        if not signal_type:
            if data.get('order_type') == 'spot':
                signal_type = 'buy' if data.get('side', '').lower() == 'buy' else 'sell'
            else:
                # Default to buy_long if we can't determine
                signal_type = 'buy_long'
                
        if not symbol or not signal_type:
            error_msg = {'error': 'Missing symbol or signal_type'}
            logger.error(f"Webhook validation failed - Symbol: {symbol}, Signal: {signal_type}, Raw data: {data}")
            socketio.emit('webhook_received', {
                'payload': data,
                'status': 'error',
                'result': error_msg
            })
            return jsonify(error_msg), 400

        # Log webhook receipt with detailed info
        logger.info(f"Webhook received: {symbol} {signal_type} (Leverage: {leverage}x, SL: {stop_loss_percent}%, TP: {take_profit_percent}%)")
        logger.info(f"Webhook data parsed - Symbol: {symbol}, Signal: {signal_type}, Market Type: {market_type}, Price: {data.get('price', 'N/A')}")
        logger.debug(f"Full webhook payload: {json.dumps(data, indent=2)}")
        
        # Check if this is a test webhook
        is_test = data.get('is_test', False)
        if is_test:
            logger.info("🧪 Test webhook detected - processing in test mode")
        
        # Emit webhook received event to all clients
        socketio.emit('webhook_received', {
            'payload': data,
            'status': 'received',
            'result': None,
            'is_test': is_test
        })

        # Get asset info (skip for test webhooks)
        logger.info(f"Step 1: Getting asset info for {symbol}")
        if is_test:
            # Use mock asset info for testing
            asset_info = {
                'index': 0,
                'name': symbol,
                'szDecimals': 3,
                'maxLeverage': 20
            }
            logger.info(f"Using mock asset info for test: {asset_info}")
        else:
            asset_info = get_asset_info(symbol)
            if not asset_info:
                logger.error(f"Asset {symbol} not found in Hyperliquid universe")
                return jsonify({'error': f'Asset {symbol} not found'}), 400
        
        # Get current price (use test price for test webhooks)
        logger.info(f"Step 2: Getting current price for {symbol}")
        if is_test:
            current_price = float(data.get('price', 100.0))
            logger.info(f"Using test price for {symbol}: ${current_price}")
        else:
            current_price = get_current_price(symbol)
            if not current_price:
                logger.error(f"Could not get price for {symbol}")
                return jsonify({'error': f'Could not get price for {symbol}'}), 400
            logger.info(f"{symbol} current price: ${current_price}")
        
        # Get account balance (use test balance for test webhooks)
        logger.info(f"Step 3: Getting account balance")
        if is_test:
            available_balance = float(data.get('test_balance', 1000.0))
            logger.info(f"Using test balance: ${available_balance}")
        else:
            # Use API wallet address for trading (where the funds actually are)
            wallet_address = account.address if account else os.getenv('HYPERLIQUID_API_WALLET')
            if not wallet_address:
                wallet_address = os.getenv('HYPERLIQUID_MAIN_WALLET')
            
            logger.info(f"Checking balance for wallet: {wallet_address}")
            logger.info(f"Using API wallet: {account.address if account else 'None'}")
            logger.info(f"Main wallet from env: {os.getenv('HYPERLIQUID_MAIN_WALLET')}")
            
            available_balance = get_account_balance(wallet_address)
            if available_balance is None:
                logger.error("Failed to get account balance - API call returned None")
                return jsonify({'error': 'Could not get account balance'}), 400
            
            logger.info(f"Available balance: ${available_balance:.2f}")
        
        # Check if balance is sufficient
        if available_balance <= 0:
            error_msg = f"Insufficient balance: ${available_balance:.2f}"
            logger.error(error_msg)
            return jsonify({'error': error_msg}), 400
        
        # Get current positions
        positions = info.user_state(wallet_address).get('assetPositions', [])
        current_position = None
        for pos in positions:
            if pos['position']['coin'] == symbol:
                current_position = pos['position']
                break
        current_size = float(current_position['szi']) if current_position else 0

        # Determine side based on signal_type and current position
        if signal_type == 'buy_short':
            if current_size == 0:
                side = 'SELL'
            else:
                logger.info(f"Buy short ignored: Position already open for {symbol}")
                return jsonify({'error': f'Position already open for {symbol}, buy short ignored.'}), 200
        elif signal_type == 'buy_long':
            if current_size == 0:
                side = 'BUY'
            else:
                logger.info(f"Buy long ignored: Position already open for {symbol}")
                return jsonify({'error': f'Position already open for {symbol}, buy long ignored.'}), 200
        elif signal_type == 'sell':
            if current_size > 0:
                side = 'SELL'  # Close long
            elif current_size < 0:
                side = 'BUY'   # Close short
            else:
                logger.info(f"Sell ignored: No open position for {symbol}")
                return jsonify({'error': f'No open position for {symbol}, sell ignored.'}), 200
        else:
            return jsonify({'error': 'Invalid signal_type'}), 400

        # Calculate position size with leverage
        risk_amount = available_balance * (RISK_PERCENTAGE / 100)
        leveraged_amount = risk_amount * leverage
        raw_size = leveraged_amount / current_price
        
        # Apply rounding
        position_size = round_size(raw_size, asset_info['szDecimals'])
        
        # Check minimum position value
        position_value = position_size * current_price
        if position_value < MIN_POSITION_VALUE:
            min_size = MIN_POSITION_VALUE / current_price
            position_size = round_size(min_size, asset_info['szDecimals'])
            position_value = position_size * current_price
            
            # If still below minimum after rounding, increase by one unit
            if position_value < MIN_POSITION_VALUE:
                min_unit = 1 / (10 ** asset_info['szDecimals'])
                position_size += min_unit
                position_value = position_size * current_price
        
        logger.info(f"Position size: {position_size} {symbol} (${position_value:.2f})")
        
        # Calculate stop loss and take profit prices based on ATR
        # Ensure ATR has a minimum value to avoid zero stop loss/take profit
        atr = max(atr, current_price * 0.001)  # At least 0.1% of current price
        
        atr_multiplier = 2.0  # Use ATR for dynamic stop loss/take profit
        stop_loss_atr = atr * atr_multiplier
        take_profit_atr = atr * (atr_multiplier * 2)  # Take profit is 2x the stop loss distance
        
        if side == 'BUY':
            stop_loss_price = current_price - stop_loss_atr
            take_profit_price = current_price + take_profit_atr
        else:
            stop_loss_price = current_price + stop_loss_atr
            take_profit_price = current_price - take_profit_atr
        
        # Convert to percentages for logging
        stop_loss_percent = abs((stop_loss_price - current_price) / current_price * 100)
        take_profit_percent = abs((take_profit_price - current_price) / current_price * 100)
        
        logger.info(f"Stop Loss: ${stop_loss_price:.2f} ({stop_loss_percent:.2f}%)")
        logger.info(f"Take Profit: ${take_profit_price:.2f} ({take_profit_percent:.2f}%)")
        
        # Place order or simulate
        if exchange and not is_test:
            # Place real order
            try:
                # Note: Hyperliquid handles leverage through position sizing, not as a separate setting
                logger.info(f"Placing order with effective leverage {leverage}x for {symbol}")
                
                # Validate order parameters before placing
                if position_size <= 0:
                    raise ValueError(f"Invalid position size: {position_size}")
                
                if current_price <= 0:
                    raise ValueError(f"Invalid current price: {current_price}")
                
                # Place main order
                order_result = exchange.market_open(
                    symbol,  # name as positional argument
                    is_buy=(side == 'BUY'),
                    sz=position_size,
                    px=None  # Market order
                )
                
                logger.info(f"Main order placed successfully: {order_result}")
                
                # Note: Hyperliquid doesn't have built-in stop loss/take profit orders
                # You would need to monitor positions and place exit orders manually
                logger.info(f"Note: Stop loss at ${stop_loss_price:.2f} and take profit at ${take_profit_price:.2f} would need to be monitored manually")
                
                add_activity('order_placed', f'Placed {side} order for {position_size} {symbol} at ${current_price}', {
                    'symbol': symbol,
                    'side': side,
                    'size': position_size,
                    'price': current_price,
                    'value': position_value,
                    'leverage': leverage,
                    'stop_loss': stop_loss_price,
                    'take_profit': take_profit_price,
                    'technical_indicators': {
                        'rsi': rsi,
                        'macd': macd,
                        'signal': signal_line,
                        'adx': adx,
                        'bb_upper': bb_upper,
                        'bb_middle': bb_middle,
                        'bb_lower': bb_lower,
                        'ema_fast': ema_fast,
                        'ema_slow': ema_slow,
                        'volume': volume,
                        'vol_ma': vol_ma,
                        'atr': atr
                    },
                    'orderId': order_result.get('response', {}).get('data', {}).get('statuses', [{}])[0].get('filled', {}).get('oid')
                })
                
                # Emit success event
                socketio.emit('webhook_received', {
                    'payload': data,
                    'status': 'success',
                    'result': {
                        'main_order': order_result,
                        'stop_loss': None,
                        'take_profit': None
                    }
                })
                
                return jsonify({
                    'success': True,
                    'simulation': False,
                    'order': {
                        'symbol': symbol,
                        'side': side,
                        'size': position_size,
                        'price': current_price,
                        'value': position_value,
                        'leverage': leverage,
                        'stop_loss': stop_loss_price,
                        'take_profit': take_profit_price,
                        'technical_indicators': {
                            'rsi': rsi,
                            'macd': macd,
                            'signal': signal_line,
                            'adx': adx,
                            'bb_upper': bb_upper,
                            'bb_middle': bb_middle,
                            'bb_lower': bb_lower,
                            'ema_fast': ema_fast,
                            'ema_slow': ema_slow,
                            'volume': volume,
                            'vol_ma': vol_ma,
                            'atr': atr
                        }
                    },
                    'result': {
                        'main_order': order_result,
                        'stop_loss': None,
                        'take_profit': None
                    }
                })
                
            except Exception as e:
                error_str = str(e)
                logger.error(f"Order placement failed: {e}")
                
                # Handle specific error cases
                if "Order has invalid price" in error_str:
                    error_msg = {
                        'error': f'Order rejected: Invalid price for {symbol}. This may be due to spot trading not being supported on Hyperliquid. Try using perpetual trading instead.',
                        'suggestion': 'Hyperliquid primarily supports perpetual trading. Please use market_type: "perpetual" instead of "spot".',
                        'symbol': symbol,
                        'current_price': current_price,
                        'position_size': position_size
                    }
                elif "Insufficient balance" in error_str:
                    error_msg = {'error': f'Insufficient balance: {error_str}'}
                else:
                    error_msg = {'error': f'Order placement failed: {error_str}'}
                
                socketio.emit('webhook_received', {
                    'payload': data,
                    'status': 'error',
                    'result': error_msg
                })
                
                return jsonify(error_msg), 500
        elif is_test:
            # Test mode - simulate order without making real API calls
            logger.info(f"🧪 TEST MODE: Simulating {side} order for {position_size} {symbol}")
            
            # Create mock order result
            order_result = {
                'status': 'ok',
                'response': {
                    'data': {
                        'statuses': [{
                            'resting': {'oid': 'test_order_' + str(int(time.time()))},
                            'filled': position_size
                        }]
                    }
                }
            }
            
            add_activity('test_order_placed', f'TEST: Placed {side} order for {position_size} {symbol} at ${current_price}', {
                'symbol': symbol,
                'side': side,
                'size': position_size,
                'price': current_price,
                'value': position_value,
                'leverage': leverage,
                'stop_loss': stop_loss_price,
                'take_profit': take_profit_price,
                'test_mode': True,
                'technical_indicators': {
                    'rsi': rsi,
                    'macd': macd,
                    'signal': signal_line,
                    'adx': adx,
                    'bb_upper': bb_upper,
                    'bb_middle': bb_middle,
                    'bb_lower': bb_lower,
                    'ema_fast': ema_fast,
                    'ema_slow': ema_slow,
                    'volume': volume,
                    'vol_ma': vol_ma,
                    'atr': atr
                }
            })
            
            # Emit test success event
            socketio.emit('webhook_received', {
                'payload': data,
                'status': 'test_success',
                'result': {
                    'main_order': order_result,
                    'stop_loss': None,
                    'take_profit': None,
                    'test_mode': True
                }
            })
            
            return jsonify({
                'success': True,
                'test_mode': True,
                'message': f'Test webhook processed successfully for {symbol} {side}',
                'order': {
                    'symbol': symbol,
                    'side': side,
                    'size': position_size,
                    'price': current_price,
                    'value': position_value,
                    'leverage': leverage,
                    'stop_loss': stop_loss_price,
                    'take_profit': take_profit_price,
                    'technical_indicators': {
                        'rsi': rsi,
                        'macd': macd,
                        'signal': signal_line,
                        'adx': adx,
                        'bb_upper': bb_upper,
                        'bb_middle': bb_middle,
                        'bb_lower': bb_lower,
                        'ema_fast': ema_fast,
                        'ema_slow': ema_slow,
                        'volume': volume,
                        'vol_ma': vol_ma,
                        'atr': atr
                    }
                },
                'result': {
                    'main_order': order_result,
                    'stop_loss': None,
                    'take_profit': None
                }
            })
        else:
            # Simulation mode
            logger.info(f"SIMULATION: Would place {side} order for {position_size} {symbol}")
            
            # Emit simulation event
            socketio.emit('webhook_received', {
                'payload': data,
                'status': 'simulation',
                'result': {
                    'symbol': symbol,
                    'side': side,
                    'size': position_size,
                    'price': current_price,
                    'value': position_value,
                    'leverage': leverage,
                    'stop_loss': stop_loss_price,
                    'take_profit': take_profit_price
                }
            })
            
            return jsonify({
                'success': True,
                'simulation': True,
                'order': {
                    'symbol': symbol,
                    'side': side,
                    'size': position_size,
                    'price': current_price,
                    'value': position_value,
                    'leverage': leverage,
                    'stop_loss': stop_loss_price,
                    'take_profit': take_profit_price,
                    'technical_indicators': {
                        'rsi': rsi,
                        'macd': macd,
                        'signal': signal_line,
                        'adx': adx,
                        'bb_upper': bb_upper,
                        'bb_middle': bb_middle,
                        'bb_lower': bb_lower,
                        'ema_fast': ema_fast,
                        'ema_slow': ema_slow,
                        'volume': volume,
                        'vol_ma': vol_ma,
                        'atr': atr
                    }
                }
            })
            
    except Exception as e:
        error_msg = {'error': str(e)}
        socketio.emit('webhook_received', {
            'payload': request.get_json() if request.is_json else {},
            'status': 'error',
            'result': error_msg
        })
        logger.error(f"Webhook error: {e}")
        return jsonify(error_msg), 500

@app.route('/api/account', methods=['GET'])
def get_account_info():
    """Get account information for both main wallet and API wallet"""
    try:
        logger.info("=== DEBUG: /api/account endpoint called ===")
        
        # Hardcode the main wallet address for testing
        main_wallet = os.getenv('HYPERLIQUID_MAIN_WALLET', '')
        # Hardcode the API wallet address for testing
        api_wallet = os.getenv('HYPERLIQUID_API_WALLET', '')
        
        logger.info(f"DEBUG: Main wallet: {main_wallet}")
        logger.info(f"DEBUG: API wallet: {api_wallet}")

        # Get asset metadata first
        logger.info("DEBUG: Getting meta and all_mids data...")
        meta = info.meta()
        all_mids = info.all_mids()
        logger.info(f"DEBUG: Got {len(meta['universe'])} assets and {len(all_mids)} prices")

        # Get main wallet info
        main_wallet_info = {
            'address': main_wallet,
            'balance': '0',
            'positions': []
        }
        try:
            logger.info(f"DEBUG: Getting user state for main wallet: {main_wallet}")
            main_user_state = info.user_state(main_wallet)
            logger.info(f"DEBUG: Main wallet user state keys: {list(main_user_state.keys())}")
            
            if 'crossMarginSummary' in main_user_state:
                main_account_value = float(main_user_state['crossMarginSummary']['accountValue'])
                main_margin_used = float(main_user_state['crossMarginSummary']['totalMarginUsed'])
                main_balance = main_account_value - main_margin_used
                main_wallet_info['balance'] = str(main_balance)
                logger.info(f"DEBUG: Main wallet balance: ${main_balance:.2f}")
            else:
                logger.warning("DEBUG: No crossMarginSummary found in main wallet state")

            # Get main wallet positions
            if 'assetPositions' in main_user_state:
                position_count = 0
                for i, position in enumerate(main_user_state['assetPositions']):
                    if position['position']['szi'] != '0':
                        position_count += 1
                        symbol = position['position']['coin']
                        current_price = float(all_mids.get(symbol, 0))
                        size = float(position['position']['szi'])
                        entry_price = float(position['position']['entryPx'])
                        
                        main_wallet_info['positions'].append({
                            'symbol': symbol,
                            'side': 'LONG' if size > 0 else 'SHORT',
                            'size': abs(size),
                            'entryPrice': entry_price,
                            'currentPrice': current_price,
                            'value': abs(size) * current_price
                        })
                logger.info(f"DEBUG: Main wallet has {position_count} active positions")
            else:
                logger.warning("DEBUG: No assetPositions found in main wallet state")
                
        except Exception as e:
            logger.error(f"DEBUG: Error getting main wallet info: {e}")

        # Get API wallet info
        api_wallet_info = {
            'address': api_wallet,
            'balance': '0',
            'positions': []
        }
        if api_wallet:
            try:
                logger.info(f"DEBUG: Getting user state for API wallet: {api_wallet}")
                api_user_state = info.user_state(api_wallet)
                logger.info(f"DEBUG: API wallet user state keys: {list(api_user_state.keys())}")
                
                if 'crossMarginSummary' in api_user_state:
                    api_account_value = float(api_user_state['crossMarginSummary']['accountValue'])
                    api_margin_used = float(api_user_state['crossMarginSummary']['totalMarginUsed'])
                    api_balance = api_account_value - api_margin_used
                    api_wallet_info['balance'] = str(api_balance)
                    logger.info(f"DEBUG: API wallet balance: ${api_balance:.2f}")
                else:
                    logger.warning("DEBUG: No crossMarginSummary found in API wallet state")

                # Get API wallet positions
                if 'assetPositions' in api_user_state:
                    position_count = 0
                    for i, position in enumerate(api_user_state['assetPositions']):
                        if position['position']['szi'] != '0':
                            position_count += 1
                            symbol = position['position']['coin']
                            current_price = float(all_mids.get(symbol, 0))
                            size = float(position['position']['szi'])
                            entry_price = float(position['position']['entryPx'])
                            
                            api_wallet_info['positions'].append({
                                'symbol': symbol,
                                'side': 'LONG' if size > 0 else 'SHORT',
                                'size': abs(size),
                                'entryPrice': entry_price,
                                'currentPrice': current_price,
                                'value': abs(size) * current_price
                            })
                    logger.info(f"DEBUG: API wallet has {position_count} active positions")
                else:
                    logger.warning("DEBUG: No assetPositions found in API wallet state")
                    
            except Exception as e:
                logger.error(f"DEBUG: Error getting API wallet info: {e}")

        result = {
            'main_wallet': main_wallet_info,
            'api_wallet': api_wallet_info
        }
        logger.info(f"DEBUG: Final result - Main balance: {main_wallet_info['balance']}, API balance: {api_wallet_info['balance']}")
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"DEBUG: Error in get_account_info: {e}")
        error_result = {
            'error': str(e),
            'main_wallet': {
                'address': main_wallet,
                'balance': '0',
                'positions': []
            },
            'api_wallet': {
                'address': api_wallet,
                'balance': '0',
                'positions': []
            }
        }
        return jsonify(error_result), 500

@app.route('/api/pairs', methods=['GET'])
def get_trading_pairs():
    """Get available trading pairs"""
    try:
        meta = info.meta()
        pairs = [{
            'symbol': asset['name'],
            'maxLeverage': asset['maxLeverage'],
            'szDecimals': asset['szDecimals']
        } for asset in meta['universe']]
        
        return jsonify({'pairs': pairs})
    except Exception as e:
        logger.error(f"Error getting trading pairs: {e}")
        return jsonify({'error': str(e)}), 500

class StrategyEngine:
    """Sophisticated trading strategy engine with advanced market analysis and risk management"""
    
    def __init__(self, exchange, info):
        self.exchange = exchange
        self.info = info
        self.indicators = {}
        self.position_cache = {}
        self.market_state = {}
        self.timeframe_data = {}
        self.volume_profile = {}
        self.confidence_thresholds = {
            'high': 0.85,
            'medium': 0.70,
            'low': 0.55
        }
        self.risk_multipliers = {
            'high': 1.0,
            'medium': 0.75,
            'low': 0.5
        }
        
    def analyze_market_state(self, symbol):
        """Comprehensive market state analysis with multiple factors"""
        try:
            # Get order book data
            l2_snapshot = self.info.l2_snapshot(symbol)
            current_price = float(self.info.all_mids()[symbol])
            
            # Advanced Order Book Analysis
            orderbook_analysis = self._analyze_orderbook(l2_snapshot, current_price)
            
            # Volume Analysis
            volume_analysis = self._analyze_volume_profile(symbol)
            
            # Multi-timeframe Trend Analysis
            trend_analysis = self._analyze_multi_timeframe_trends(symbol, current_price)
            
            # Market Microstructure Analysis
            microstructure = self._analyze_market_microstructure(symbol, l2_snapshot)
            
            # Volatility and Risk Assessment
            volatility_analysis = self._analyze_volatility_regime(symbol, current_price)
            
            # Comprehensive market state
            market_state = {
                'symbol': symbol,
                'timestamp': time.time(),
                'current_price': current_price,
                'orderbook': orderbook_analysis,
                'volume': volume_analysis,
                'trend': trend_analysis,
                'microstructure': microstructure,
                'volatility': volatility_analysis,
                'overall_score': self._calculate_market_score(
                    orderbook_analysis, volume_analysis, trend_analysis, 
                    microstructure, volatility_analysis
                )
            }
            
            self.market_state[symbol] = market_state
            logger.info(f"Market analysis for {symbol}: Score {market_state['overall_score']:.2f}")
            
            return market_state
            
        except Exception as e:
            logger.error(f"Error in comprehensive market analysis: {e}")
            return None
    
    def _analyze_orderbook(self, l2_snapshot, current_price):
        """Advanced order book imbalance and concentration analysis"""
        try:
            bids = []
            asks = []
            bid_prices = []
            ask_prices = []
            
            for level in l2_snapshot['levels']:
                price = float(level['px'])
                size = float(level['sz'])
                
                if level['side'] == 'B':
                    bids.append(size)
                    bid_prices.append(price)
                else:
                    asks.append(size)
                    ask_prices.append(price)
            
            total_bids = sum(bids)
            total_asks = sum(asks)
            
            # Basic imbalance
            imbalance = (total_bids - total_asks) / (total_bids + total_asks) if (total_bids + total_asks) > 0 else 0
            
            # Concentration analysis (top 3 levels)
            bid_concentration = sum(bids[:3]) / total_bids if total_bids > 0 else 0
            ask_concentration = sum(asks[:3]) / total_asks if total_asks > 0 else 0
            
            # Spread analysis
            best_bid = max(bid_prices) if bid_prices else current_price * 0.99
            best_ask = min(ask_prices) if ask_prices else current_price * 1.01
            spread = (best_ask - best_bid) / current_price
            
            # Support/Resistance levels
            strong_support = self._find_support_resistance(bid_prices, bids, current_price, 'support')
            strong_resistance = self._find_support_resistance(ask_prices, asks, current_price, 'resistance')
            
            return {
                'imbalance': imbalance,
                'bid_concentration': bid_concentration,
                'ask_concentration': ask_concentration,
                'spread_pct': spread * 100,
                'liquidity_score': min(total_bids + total_asks, 1000000) / 1000000,
                'strong_support': strong_support,
                'strong_resistance': strong_resistance,
                'pressure': 'bullish' if imbalance > 0.15 else 'bearish' if imbalance < -0.15 else 'neutral'
            }
            
        except Exception as e:
            logger.error(f"Error in orderbook analysis: {e}")
            return {}
    
    def _analyze_volume_profile(self, symbol):
        """Volume concentration and flow analysis"""
        try:
            # Get recent volume data (simplified - would need actual volume history)
            current_volume = 1000  # Placeholder - implement actual volume fetching
            avg_volume = 800      # Placeholder - implement moving average
            
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
            
            # Volume trend analysis
            volume_trend = 'increasing' if volume_ratio > 1.2 else 'decreasing' if volume_ratio < 0.8 else 'stable'
            
            # Volume concentration (high volume = strong conviction)
            volume_score = min(volume_ratio, 3.0) / 3.0  # Cap at 3x average
            
            return {
                'current_volume': current_volume,
                'volume_ratio': volume_ratio,
                'volume_trend': volume_trend,
                'volume_score': volume_score,
                'conviction_level': 'high' if volume_ratio > 1.5 else 'medium' if volume_ratio > 0.8 else 'low'
            }
            
        except Exception as e:
            logger.error(f"Error in volume analysis: {e}")
            return {'volume_score': 0.5, 'conviction_level': 'medium'}
    
    def _analyze_multi_timeframe_trends(self, symbol, current_price):
        """Multi-timeframe trend analysis and alignment"""
        try:
            # Simulate different timeframe trends (would implement actual historical data)
            timeframes = {
                '1m': self._get_trend_direction(current_price, 1),
                '5m': self._get_trend_direction(current_price, 5),
                '15m': self._get_trend_direction(current_price, 15),
                '1h': self._get_trend_direction(current_price, 60),
                '4h': self._get_trend_direction(current_price, 240)
            }
            
            # Calculate trend alignment
            bullish_count = sum(1 for trend in timeframes.values() if trend == 'bullish')
            bearish_count = sum(1 for trend in timeframes.values() if trend == 'bearish')
            
            alignment_score = max(bullish_count, bearish_count) / len(timeframes)
            dominant_trend = 'bullish' if bullish_count > bearish_count else 'bearish' if bearish_count > bullish_count else 'neutral'
            
            return {
                'timeframes': timeframes,
                'alignment_score': alignment_score,
                'dominant_trend': dominant_trend,
                'strength': 'strong' if alignment_score > 0.8 else 'medium' if alignment_score > 0.6 else 'weak'
            }
            
        except Exception as e:
            logger.error(f"Error in trend analysis: {e}")
            return {'alignment_score': 0.5, 'dominant_trend': 'neutral'}
    
    def _analyze_market_microstructure(self, symbol, l2_snapshot):
        """Market microstructure and efficiency analysis"""
        try:
            # Analyze order book depth and distribution
            total_levels = len(l2_snapshot['levels'])
            
            # Calculate price impact for different order sizes
            impact_small = self._calculate_price_impact(l2_snapshot, 1000)  # $1k order
            impact_medium = self._calculate_price_impact(l2_snapshot, 10000)  # $10k order
            impact_large = self._calculate_price_impact(l2_snapshot, 100000)  # $100k order
            
            # Market efficiency score (lower impact = more efficient)
            efficiency_score = 1 - min(impact_large, 0.1) / 0.1
            
            return {
                'depth_levels': total_levels,
                'price_impact': {
                    'small_order': impact_small,
                    'medium_order': impact_medium,
                    'large_order': impact_large
                },
                'efficiency_score': efficiency_score,
                'market_quality': 'excellent' if efficiency_score > 0.8 else 'good' if efficiency_score > 0.6 else 'poor'
            }
            
        except Exception as e:
            logger.error(f"Error in microstructure analysis: {e}")
            return {'efficiency_score': 0.5}
    
    def _analyze_volatility_regime(self, symbol, current_price):
        """Volatility regime and risk assessment"""
        try:
            # Get historical volatility (simplified)
            volatility = self.get_historical_volatility(symbol)
            
            # Classify volatility regime
            if volatility < 0.3:
                regime = 'low_vol'
                risk_level = 'low'
            elif volatility < 0.6:
                regime = 'medium_vol'
                risk_level = 'medium'
            else:
                regime = 'high_vol'
                risk_level = 'high'
            
            # Volatility persistence (simplified)
            persistence = 0.7  # Would calculate actual persistence
            
            return {
                'current_volatility': volatility,
                'regime': regime,
                'risk_level': risk_level,
                'persistence': persistence,
                'vol_score': 1 - min(volatility, 1.0)  # Lower vol = higher score
            }
            
        except Exception as e:
            logger.error(f"Error in volatility analysis: {e}")
            return {'vol_score': 0.5, 'risk_level': 'medium'}
    
    def _calculate_market_score(self, orderbook, volume, trend, microstructure, volatility):
        """Calculate overall market attractiveness score"""
        try:
            weights = {
                'orderbook': 0.25,
                'volume': 0.20,
                'trend': 0.25,
                'microstructure': 0.15,
                'volatility': 0.15
            }
            
            # Component scores
            ob_score = (abs(orderbook.get('imbalance', 0)) + orderbook.get('liquidity_score', 0)) / 2
            vol_score = volume.get('volume_score', 0.5)
            trend_score = trend.get('alignment_score', 0.5)
            micro_score = microstructure.get('efficiency_score', 0.5)
            vol_regime_score = volatility.get('vol_score', 0.5)
            
            # Weighted overall score
            overall_score = (
                ob_score * weights['orderbook'] +
                vol_score * weights['volume'] +
                trend_score * weights['trend'] +
                micro_score * weights['microstructure'] +
                vol_regime_score * weights['volatility']
            )
            
            return min(max(overall_score, 0), 1)  # Clamp between 0 and 1
            
        except Exception as e:
            logger.error(f"Error calculating market score: {e}")
            return 0.5
    
    def _find_support_resistance(self, prices, volumes, current_price, level_type):
        """Find significant support/resistance levels"""
        try:
            if not prices or not volumes:
                return current_price
            
            # Weighted price levels by volume
            weighted_levels = []
            for i, price in enumerate(prices):
                if i < len(volumes):
                    weight = volumes[i]
                    weighted_levels.append((price, weight))
            
            # Sort by volume (descending)
            weighted_levels.sort(key=lambda x: x[1], reverse=True)
            
            # Return strongest level
            return weighted_levels[0][0] if weighted_levels else current_price
            
        except Exception as e:
            logger.error(f"Error finding support/resistance: {e}")
            return current_price
    
    def _get_trend_direction(self, current_price, timeframe_minutes):
        """Get trend direction for specific timeframe (simplified)"""
        try:
            # Simplified trend calculation (would use actual historical data)
            # Random simulation for now
            import random
            trends = ['bullish', 'bearish', 'neutral']
            return random.choice(trends)
        except:
            return 'neutral'
    
    def _calculate_price_impact(self, l2_snapshot, order_value):
        """Calculate price impact for given order value"""
        try:
            # Simplified price impact calculation
            total_liquidity = sum(float(level['sz']) for level in l2_snapshot['levels'])
            impact = min(order_value / total_liquidity, 0.1)  # Cap at 10%
            return impact
        except:
            return 0.01  # Default 1% impact
    
    def calculate_position_size(self, symbol, available_balance, current_price, confidence_level='medium'):
        """Advanced risk-adjusted position sizing with multiple factors"""
        try:
            # Get market analysis
            market_state = self.market_state.get(symbol, {})
            
            # Base risk calculation
            base_risk_pct = RISK_PERCENTAGE / 100
            
            # Confidence-based adjustment
            confidence_multiplier = self.risk_multipliers.get(confidence_level, 0.75)
            
            # Volatility adjustment
            volatility = self.get_historical_volatility(symbol)
            vol_adjustment = 1 / (1 + volatility * 2)  # Reduce size for higher volatility
            
            # Market quality adjustment
            market_score = market_state.get('overall_score', 0.5)
            quality_adjustment = 0.5 + (market_score * 0.5)  # 0.5x to 1.0x based on quality
            
            # Liquidity adjustment
            liquidity_score = market_state.get('orderbook', {}).get('liquidity_score', 0.5)
            liquidity_adjustment = 0.7 + (liquidity_score * 0.3)  # 0.7x to 1.0x based on liquidity
            
            # Trend strength adjustment
            trend_strength = market_state.get('trend', {}).get('alignment_score', 0.5)
            trend_adjustment = 0.8 + (trend_strength * 0.4)  # 0.8x to 1.2x based on trend
            
            # Combined adjustment factor
            total_adjustment = (
                confidence_multiplier * 
                vol_adjustment * 
                quality_adjustment * 
                liquidity_adjustment * 
                trend_adjustment
            )
            
            # Calculate final position size
            adjusted_risk_pct = base_risk_pct * total_adjustment
            risk_amount = available_balance * adjusted_risk_pct
            position_size = risk_amount / current_price
            
            # Apply minimum and maximum position limits
            min_position_value = MIN_POSITION_VALUE
            max_position_value = available_balance * 0.25  # Max 25% of available balance
            
            position_value = position_size * current_price
            
            if position_value < min_position_value:
                position_size = min_position_value / current_price
            elif position_value > max_position_value:
                position_size = max_position_value / current_price
            
            # Round to appropriate decimals
            asset_info = get_asset_info(symbol)
            if asset_info:
                position_size = round_size(position_size, asset_info['szDecimals'])
            
            # Log sizing decision
            logger.info(f"Position sizing for {symbol}: "
                       f"Base: {base_risk_pct:.2%}, "
                       f"Adjusted: {adjusted_risk_pct:.2%}, "
                       f"Confidence: {confidence_level}, "
                       f"Final size: {position_size}")
            
            return position_size
            
        except Exception as e:
            logger.error(f"Error calculating position size: {e}")
            return None
    
    def calculate_optimal_leverage(self, symbol, confidence_level):
        """Calculate optimal leverage based on market conditions and confidence"""
        try:
            market_state = self.market_state.get(symbol, {})
            
            # Base leverage based on confidence
            base_leverage = {
                'high': 15,
                'medium': 10,
                'low': 5
            }.get(confidence_level, 10)
            
            # Volatility adjustment
            volatility = market_state.get('volatility', {}).get('current_volatility', 0.5)
            vol_adjustment = max(0.3, 1 - volatility)  # Reduce leverage for high volatility
            
            # Market quality adjustment
            market_score = market_state.get('overall_score', 0.5)
            quality_adjustment = 0.5 + (market_score * 0.5)
            
            # Liquidity adjustment
            liquidity_score = market_state.get('orderbook', {}).get('liquidity_score', 0.5)
            liquidity_adjustment = 0.7 + (liquidity_score * 0.3)
            
            # Calculate optimal leverage
            optimal_leverage = int(base_leverage * vol_adjustment * quality_adjustment * liquidity_adjustment)
            
            # Asset-specific maximum leverage
            asset_info = get_asset_info(symbol)
            max_leverage = asset_info.get('maxLeverage', 20) if asset_info else 20
            
            final_leverage = min(optimal_leverage, max_leverage, 20)  # Cap at 20x for safety
            
            logger.info(f"Optimal leverage for {symbol}: {final_leverage}x (base: {base_leverage}x)")
            
            return final_leverage
            
        except Exception as e:
            logger.error(f"Error calculating optimal leverage: {e}")
            return 10  # Safe default
    
    def get_historical_volatility(self, symbol, period=14):
        """Calculate historical volatility"""
        try:
            # Get recent trades or candles
            # This is a simplified version - you'll want to implement proper OHLCV data
            wallet_address = account.address if account else os.getenv('HYPERLIQUID_API_WALLET')
            trades = self.info.user_fills(wallet_address)  # Replace with proper historical data
            
            if not trades:
                return 0.5  # Default moderate volatility if no data
                
            # Calculate returns
            prices = [float(trade['px']) for trade in trades]
            returns = [np.log(prices[i] / prices[i-1]) for i in range(1, len(prices))]
            
            # Calculate volatility
            if returns:
                return np.std(returns) * np.sqrt(252)  # Annualized volatility
            return 0.5
            
        except Exception as e:
            logger.error(f"Error calculating volatility: {e}")
            return 0.5
    
    def should_open_position(self, symbol, side, data=None):
        """Sophisticated multi-factor position entry analysis"""
        try:
            # Get comprehensive market analysis
            market_state = self.analyze_market_state(symbol)
            if not market_state:
                logger.warning(f"Could not analyze market state for {symbol}")
                return {'should_enter': False, 'confidence': 0, 'analysis': {}}
            
            # Initialize condition analysis
            conditions = {}
            weights = {}
            
            # 1. Order Book Analysis (Weight: 25%)
            orderbook = market_state.get('orderbook', {})
            imbalance = orderbook.get('imbalance', 0)
            liquidity_score = orderbook.get('liquidity_score', 0)
            spread_pct = orderbook.get('spread_pct', 1)
            
            # Order book conditions
            conditions['orderbook_pressure'] = self._check_orderbook_pressure(side, imbalance)
            conditions['liquidity_quality'] = liquidity_score > 0.6
            conditions['tight_spread'] = spread_pct < 0.1  # Less than 0.1% spread
            weights['orderbook'] = 0.25
            
            # 2. Volume Analysis (Weight: 20%)
            volume = market_state.get('volume', {})
            volume_score = volume.get('volume_score', 0.5)
            conviction_level = volume.get('conviction_level', 'medium')
            
            conditions['volume_confirmation'] = volume_score > 0.7
            conditions['high_conviction'] = conviction_level in ['high', 'medium']
            weights['volume'] = 0.20
            
            # 3. Multi-timeframe Trend Analysis (Weight: 25%)
            trend = market_state.get('trend', {})
            alignment_score = trend.get('alignment_score', 0.5)
            dominant_trend = trend.get('dominant_trend', 'neutral')
            
            conditions['trend_alignment'] = self._check_trend_alignment(side, dominant_trend, alignment_score)
            conditions['strong_trend'] = alignment_score > 0.6
            weights['trend'] = 0.25
            
            # 4. Market Microstructure (Weight: 15%)
            microstructure = market_state.get('microstructure', {})
            efficiency_score = microstructure.get('efficiency_score', 0.5)
            market_quality = microstructure.get('market_quality', 'good')
            
            conditions['market_efficiency'] = efficiency_score > 0.6
            conditions['good_market_quality'] = market_quality in ['excellent', 'good']
            weights['microstructure'] = 0.15
            
            # 5. Volatility and Risk (Weight: 15%)
            volatility = market_state.get('volatility', {})
            risk_level = volatility.get('risk_level', 'medium')
            vol_score = volatility.get('vol_score', 0.5)
            
            conditions['acceptable_volatility'] = risk_level in ['low', 'medium']
            conditions['stable_environment'] = vol_score > 0.4
            weights['volatility'] = 0.15
            
            # 6. Technical Indicator Confluence (if provided)
            if data and 'technical_indicators' in data:
                ti_conditions = self._analyze_technical_indicators(data['technical_indicators'], side)
                conditions.update(ti_conditions)
                # Adjust weights to include technical analysis
                for category in weights:
                    weights[category] *= 0.85  # Reduce other weights by 15%
                weights['technical'] = 0.15
            
            # Calculate weighted confidence score
            category_scores = {}
            for category, weight in weights.items():
                if category == 'technical':
                    # Technical indicators have their own subconditions
                    tech_conditions = [k for k in conditions.keys() if k.startswith('tech_')]
                    category_score = sum(conditions.get(k, False) for k in tech_conditions) / max(len(tech_conditions), 1)
                elif category == 'orderbook':
                    ob_conditions = ['orderbook_pressure', 'liquidity_quality', 'tight_spread']
                    category_score = sum(conditions.get(k, False) for k in ob_conditions) / len(ob_conditions)
                elif category == 'volume':
                    vol_conditions = ['volume_confirmation', 'high_conviction']
                    category_score = sum(conditions.get(k, False) for k in vol_conditions) / len(vol_conditions)
                elif category == 'trend':
                    trend_conditions = ['trend_alignment', 'strong_trend']
                    category_score = sum(conditions.get(k, False) for k in trend_conditions) / len(trend_conditions)
                elif category == 'microstructure':
                    micro_conditions = ['market_efficiency', 'good_market_quality']
                    category_score = sum(conditions.get(k, False) for k in micro_conditions) / len(micro_conditions)
                elif category == 'volatility':
                    vol_conditions = ['acceptable_volatility', 'stable_environment']
                    category_score = sum(conditions.get(k, False) for k in vol_conditions) / len(vol_conditions)
                else:
                    category_score = 0.5
                
                category_scores[category] = category_score
            
            # Calculate overall confidence
            confidence = sum(score * weights[category] for category, score in category_scores.items())
            
            # Determine confidence level
            if confidence >= self.confidence_thresholds['high']:
                confidence_level = 'high'
                should_enter = True
            elif confidence >= self.confidence_thresholds['medium']:
                confidence_level = 'medium'
                should_enter = True
            elif confidence >= self.confidence_thresholds['low']:
                confidence_level = 'low'
                should_enter = True  # Enter with reduced size
            else:
                confidence_level = 'very_low'
                should_enter = False
            
            # Additional safety checks
            overall_market_score = market_state.get('overall_score', 0)
            if overall_market_score < 0.3:
                should_enter = False
                confidence_level = 'very_low'
                logger.warning(f"Market conditions too poor for {symbol}: score {overall_market_score:.2f}")
            
            # Risk management override
            if market_state.get('volatility', {}).get('risk_level') == 'high' and confidence_level != 'high':
                should_enter = False
                logger.warning(f"High volatility override for {symbol}")
            
            analysis_result = {
                'should_enter': should_enter,
                'confidence': confidence,
                'confidence_level': confidence_level,
                'analysis': {
                    'conditions': conditions,
                    'category_scores': category_scores,
                    'weights': weights,
                    'market_state': market_state,
                    'overall_market_score': overall_market_score
                }
            }
            
            # Detailed logging
            logger.info(f"Entry analysis for {symbol} {side.upper()}:")
            logger.info(f"  Confidence: {confidence:.2%} ({confidence_level})")
            logger.info(f"  Should enter: {should_enter}")
            logger.info(f"  Category scores: {category_scores}")
            logger.info(f"  Conditions met: {sum(conditions.values())}/{len(conditions)}")
            
            return analysis_result
            
        except Exception as e:
            logger.error(f"Error in sophisticated entry analysis: {e}")
            return {'should_enter': False, 'confidence': 0, 'analysis': {}}
    
    def _check_orderbook_pressure(self, side, imbalance):
        """Check if order book pressure supports the trade direction"""
        if side in ['buy', 'BUY']:
            return imbalance > 0.10  # Buying pressure
        else:
            return imbalance < -0.10  # Selling pressure
    
    def _check_trend_alignment(self, side, dominant_trend, alignment_score):
        """Check if trend aligns with trade direction"""
        if side in ['buy', 'BUY']:
            return dominant_trend == 'bullish' and alignment_score > 0.5
        else:
            return dominant_trend == 'bearish' and alignment_score > 0.5
    
    def _analyze_technical_indicators(self, technical_indicators, side):
        """Analyze technical indicator confluence"""
        try:
            conditions = {}
            
            # RSI analysis
            rsi = float(technical_indicators.get('rsi', 50))
            if side in ['buy', 'BUY']:
                conditions['tech_rsi_oversold'] = rsi < 35
                conditions['tech_rsi_bullish'] = 35 <= rsi <= 65
            else:
                conditions['tech_rsi_overbought'] = rsi > 65
                conditions['tech_rsi_bearish'] = 35 <= rsi <= 65
            
            # MACD analysis
            macd = float(technical_indicators.get('macd', 0))
            signal = float(technical_indicators.get('signal', 0))
            if side in ['buy', 'BUY']:
                conditions['tech_macd_bullish'] = macd > signal and macd > 0
            else:
                conditions['tech_macd_bearish'] = macd < signal and macd < 0
            
            # EMA trend analysis
            ema_fast = float(technical_indicators.get('ema_fast', 0))
            ema_slow = float(technical_indicators.get('ema_slow', 0))
            if ema_fast > 0 and ema_slow > 0:
                if side in ['buy', 'BUY']:
                    conditions['tech_ema_bullish'] = ema_fast > ema_slow
                else:
                    conditions['tech_ema_bearish'] = ema_fast < ema_slow
            
            # Bollinger Bands analysis
            bb_upper = float(technical_indicators.get('bb_upper', 0))
            bb_lower = float(technical_indicators.get('bb_lower', 0))
            bb_middle = float(technical_indicators.get('bb_middle', 0))
            current_price = float(technical_indicators.get('current_price', 0))
            
            if bb_upper > 0 and bb_lower > 0 and current_price > 0:
                bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)
                if side in ['buy', 'BUY']:
                    conditions['tech_bb_oversold'] = bb_position < 0.2
                else:
                    conditions['tech_bb_overbought'] = bb_position > 0.8
            
            # ADX strength
            adx = float(technical_indicators.get('adx', 0))
            conditions['tech_strong_trend'] = adx > 25
            
            # Volume confirmation
            volume = float(technical_indicators.get('volume', 0))
            vol_ma = float(technical_indicators.get('vol_ma', 0))
            if vol_ma > 0:
                conditions['tech_volume_confirmation'] = volume > vol_ma * 1.2
            
            return conditions
            
        except Exception as e:
            logger.error(f"Error analyzing technical indicators: {e}")
            return {}
    
    def calculate_exit_points(self, symbol, side, entry_price, position_size, confidence_level='medium'):
        """Calculate sophisticated dynamic exit points with multiple strategies"""
        try:
            # Get market state for comprehensive analysis
            market_state = self.analyze_market_state(symbol)
            if not market_state:
                return None
            
            # Extract market factors
            orderbook = market_state.get('orderbook', {})
            volatility = market_state.get('volatility', {})
            trend = market_state.get('trend', {})
            microstructure = market_state.get('microstructure', {})
            
            # Dynamic stop loss calculation based on multiple factors
            base_stop_loss_pct = self._calculate_dynamic_stop_loss(
                orderbook, volatility, confidence_level
            )
            
            # ATR-based adjustments
            atr_multiplier = self._get_atr_multiplier(volatility, confidence_level)
            
            # Support/Resistance based adjustments
            support_resistance_adjustment = self._calculate_sr_adjustment(
                orderbook, entry_price, side
            )
            
            # Final stop loss percentage
            stop_loss_pct = base_stop_loss_pct * atr_multiplier * support_resistance_adjustment
            stop_loss_pct = max(0.3, min(5.0, stop_loss_pct))  # Clamp between 0.3% and 5%
            
            # Multiple take profit levels with dynamic ratios
            take_profit_levels = self._calculate_take_profit_levels(
                stop_loss_pct, trend, confidence_level
            )
            
            # Calculate actual prices
            if side in ['buy', 'BUY']:
                stop_loss_price = entry_price * (1 - stop_loss_pct/100)
                take_profits = [
                    {
                        'price': entry_price * (1 + level['percentage']/100),
                        'size': position_size * level['size_percentage'],
                        'type': level['type']
                    }
                    for level in take_profit_levels
                ]
            else:
                stop_loss_price = entry_price * (1 + stop_loss_pct/100)
                take_profits = [
                    {
                        'price': entry_price * (1 - level['percentage']/100),
                        'size': position_size * level['size_percentage'],
                        'type': level['type']
                    }
                    for level in take_profit_levels
                ]
            
            # Trailing stop configuration
            trailing_stop = self._configure_trailing_stop(
                take_profits, stop_loss_pct, trend, confidence_level
            )
            
            # Order book based exit signals
            orderbook_exits = self._configure_orderbook_exits(
                orderbook, entry_price, side
            )
            
            # Time-based exits
            time_exits = self._configure_time_exits(
                trend, volatility, confidence_level
            )
            
            exit_strategy = {
                'stop_loss': {
                    'price': stop_loss_price,
                    'percentage': stop_loss_pct,
                    'type': 'market'
                },
                'take_profits': take_profits,
                'trailing_stop': trailing_stop,
                'orderbook_exits': orderbook_exits,
                'time_exits': time_exits,
                'risk_management': {
                    'max_loss_per_trade': position_size * entry_price * (stop_loss_pct / 100),
                    'risk_reward_ratio': sum(level['percentage'] for level in take_profit_levels) / stop_loss_pct,
                    'confidence_level': confidence_level
                },
                'market_conditions': {
                    'volatility_regime': volatility.get('regime', 'medium_vol'),
                    'trend_strength': trend.get('strength', 'medium'),
                    'market_quality': microstructure.get('market_quality', 'good')
                }
            }
            
            # Log exit strategy
            logger.info(f"Exit strategy for {symbol} {side.upper()}:")
            logger.info(f"  Stop Loss: {stop_loss_price:.4f} ({stop_loss_pct:.2f}%)")
            logger.info(f"  Take Profits: {len(take_profits)} levels")
            logger.info(f"  Risk/Reward: {exit_strategy['risk_management']['risk_reward_ratio']:.2f}")
            
            return exit_strategy
            
        except Exception as e:
            logger.error(f"Error calculating sophisticated exit points: {e}")
            return None
    
    def _calculate_dynamic_stop_loss(self, orderbook, volatility, confidence_level):
        """Calculate dynamic stop loss based on market conditions"""
        try:
            # Base stop loss by confidence
            base_stops = {
                'high': 1.0,
                'medium': 1.5,
                'low': 2.0
            }
            base_stop = base_stops.get(confidence_level, 1.5)
            
            # Order book imbalance adjustment
            imbalance = abs(orderbook.get('imbalance', 0))
            imbalance_adjustment = 1 + (imbalance * 0.5)  # Tighter stops with strong imbalance
            
            # Volatility adjustment
            vol_score = volatility.get('vol_score', 0.5)
            vol_adjustment = 2 - vol_score  # Higher volatility = wider stops
            
            # Liquidity adjustment
            liquidity_score = orderbook.get('liquidity_score', 0.5)
            liquidity_adjustment = 2 - liquidity_score  # Lower liquidity = wider stops
            
            return base_stop * imbalance_adjustment * vol_adjustment * liquidity_adjustment
            
        except Exception as e:
            logger.error(f"Error calculating dynamic stop loss: {e}")
            return 1.5  # Default
    
    def _get_atr_multiplier(self, volatility, confidence_level):
        """Get ATR multiplier based on volatility and confidence"""
        try:
            current_vol = volatility.get('current_volatility', 0.5)
            
            # Base multipliers by confidence
            base_multipliers = {
                'high': 1.5,
                'medium': 2.0,
                'low': 2.5
            }
            base = base_multipliers.get(confidence_level, 2.0)
            
            # Adjust for volatility
            vol_adjustment = 0.5 + current_vol
            
            return base * vol_adjustment
            
        except Exception as e:
            logger.error(f"Error calculating ATR multiplier: {e}")
            return 2.0
    
    def _calculate_sr_adjustment(self, orderbook, entry_price, side):
        """Calculate support/resistance adjustment"""
        try:
            if side in ['buy', 'BUY']:
                support_level = orderbook.get('strong_support', entry_price)
                distance_to_support = abs(entry_price - support_level) / entry_price
                return max(0.5, min(1.5, distance_to_support * 10))  # Closer support = tighter stop
            else:
                resistance_level = orderbook.get('strong_resistance', entry_price)
                distance_to_resistance = abs(entry_price - resistance_level) / entry_price
                return max(0.5, min(1.5, distance_to_resistance * 10))
                
        except Exception as e:
            logger.error(f"Error calculating S/R adjustment: {e}")
            return 1.0
    
    def _calculate_take_profit_levels(self, stop_loss_pct, trend, confidence_level):
        """Calculate sophisticated take profit levels"""
        try:
            # Base ratios by confidence
            base_ratios = {
                'high': [1.5, 2.5, 4.0, 6.0],
                'medium': [2.0, 3.0, 4.5],
                'low': [2.5, 4.0]
            }
            ratios = base_ratios.get(confidence_level, [2.0, 3.0, 4.5])
            
            # Trend strength adjustment
            trend_strength = trend.get('alignment_score', 0.5)
            if trend_strength > 0.8:
                # Strong trend - extend targets
                ratios = [r * 1.3 for r in ratios]
            elif trend_strength < 0.4:
                # Weak trend - conservative targets
                ratios = [r * 0.8 for r in ratios]
            
            # Size distribution
            if len(ratios) == 2:
                size_distribution = [0.6, 0.4]
            elif len(ratios) == 3:
                size_distribution = [0.4, 0.35, 0.25]
            else:
                size_distribution = [0.3, 0.3, 0.25, 0.15]
            
            levels = []
            for i, (ratio, size_pct) in enumerate(zip(ratios, size_distribution)):
                levels.append({
                    'percentage': stop_loss_pct * ratio,
                    'size_percentage': size_pct,
                    'type': 'limit' if i < len(ratios) - 1 else 'market'
                })
            
            return levels
            
        except Exception as e:
            logger.error(f"Error calculating take profit levels: {e}")
            return [
                {'percentage': stop_loss_pct * 2, 'size_percentage': 0.5, 'type': 'limit'},
                {'percentage': stop_loss_pct * 3, 'size_percentage': 0.5, 'type': 'market'}
            ]
    
    def _configure_trailing_stop(self, take_profits, stop_loss_pct, trend, confidence_level):
        """Configure trailing stop parameters"""
        try:
            if not take_profits:
                return None
            
            # Activation after first TP
            activation_price = take_profits[0]['price']
            
            # Callback rate based on confidence and trend
            base_callback = {
                'high': 0.3,
                'medium': 0.5,
                'low': 0.7
            }.get(confidence_level, 0.5)
            
            # Trend adjustment
            trend_strength = trend.get('alignment_score', 0.5)
            trend_adjustment = 1 - (trend_strength * 0.3)  # Stronger trend = tighter trailing
            
            callback_rate = base_callback * trend_adjustment * stop_loss_pct
            
            return {
                'activation_price': activation_price,
                'callback_rate': callback_rate,
                'step_size': stop_loss_pct * 0.2,  # Move stop in steps
                'max_callback': stop_loss_pct * 1.5  # Maximum callback
            }
            
        except Exception as e:
            logger.error(f"Error configuring trailing stop: {e}")
            return None
    
    def _configure_orderbook_exits(self, orderbook, entry_price, side):
        """Configure order book based exit signals"""
        try:
            return {
                'imbalance_threshold': 0.3,  # Exit if imbalance reverses strongly
                'liquidity_threshold': 0.3,  # Exit if liquidity drops significantly
                'spread_threshold': 0.5,     # Exit if spread widens too much
                'monitor_levels': 5          # Monitor top 5 order book levels
            }
            
        except Exception as e:
            logger.error(f"Error configuring orderbook exits: {e}")
            return {}
    
    def _configure_time_exits(self, trend, volatility, confidence_level):
        """Configure time-based exit rules"""
        try:
            # Base time limits by confidence
            base_times = {
                'high': {'max_hold': 24, 'profit_target_time': 4},
                'medium': {'max_hold': 12, 'profit_target_time': 2},
                'low': {'max_hold': 6, 'profit_target_time': 1}
            }
            times = base_times.get(confidence_level, base_times['medium'])
            
            # Volatility adjustment
            vol_regime = volatility.get('regime', 'medium_vol')
            if vol_regime == 'high_vol':
                times['max_hold'] *= 0.7
                times['profit_target_time'] *= 0.5
            elif vol_regime == 'low_vol':
                times['max_hold'] *= 1.3
                times['profit_target_time'] *= 1.5
            
            return {
                'max_hold_hours': times['max_hold'],
                'profit_target_hours': times['profit_target_time'],
                'weekend_close': True,  # Close positions before weekend
                'news_events': True     # Close before major news events
            }
            
        except Exception as e:
            logger.error(f"Error configuring time exits: {e}")
            return {}

# Initialize strategy engine
strategy_engine = StrategyEngine(exchange, info)

@app.route('/api/strategy/analyze', methods=['POST'])
def analyze_market():
    """Analyze market conditions for a specific symbol"""
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        
        if not symbol:
            return jsonify({'error': 'Symbol is required'}), 400
        
        # Perform comprehensive market analysis
        market_analysis = strategy_engine.analyze_market_state(symbol)
        
        if not market_analysis:
            return jsonify({'error': f'Could not analyze market for {symbol}'}), 400
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'analysis': market_analysis,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error in market analysis: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy/entry-analysis', methods=['POST'])
def analyze_entry():
    """Analyze entry conditions for a potential trade"""
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        side = data.get('side')
        technical_indicators = data.get('technical_indicators', {})
        
        if not symbol or not side:
            return jsonify({'error': 'Symbol and side are required'}), 400
        
        # Perform entry analysis
        entry_analysis = strategy_engine.should_open_position(
            symbol, side, {'technical_indicators': technical_indicators}
        )
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'side': side,
            'entry_analysis': entry_analysis,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error in entry analysis: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy/position-sizing', methods=['POST'])
def calculate_position_sizing():
    """Calculate optimal position size"""
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        available_balance = float(data.get('available_balance', 0))
        current_price = float(data.get('current_price', 0))
        confidence_level = data.get('confidence_level', 'medium')
        
        if not symbol or available_balance <= 0 or current_price <= 0:
            return jsonify({'error': 'Valid symbol, balance, and price are required'}), 400
        
        # Calculate position size
        position_size = strategy_engine.calculate_position_size(
            symbol, available_balance, current_price, confidence_level
        )
        
        # Calculate optimal leverage
        optimal_leverage = strategy_engine.calculate_optimal_leverage(symbol, confidence_level)
        
        if position_size is None:
            return jsonify({'error': 'Could not calculate position size'}), 400
        
        position_value = position_size * current_price
        risk_percentage = (position_value / available_balance) * 100
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'position_sizing': {
                'position_size': position_size,
                'position_value': position_value,
                'optimal_leverage': optimal_leverage,
                'risk_percentage': risk_percentage,
                'confidence_level': confidence_level,
                'available_balance': available_balance,
                'current_price': current_price
            },
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error in position sizing: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy/exit-strategy', methods=['POST'])
def calculate_exit_strategy():
    """Calculate exit strategy for a position"""
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        side = data.get('side')
        entry_price = float(data.get('entry_price', 0))
        position_size = float(data.get('position_size', 0))
        confidence_level = data.get('confidence_level', 'medium')
        
        if not symbol or not side or entry_price <= 0 or position_size <= 0:
            return jsonify({'error': 'Valid symbol, side, entry price, and position size are required'}), 400
        
        # Calculate exit strategy
        exit_strategy = strategy_engine.calculate_exit_points(
            symbol, side, entry_price, position_size, confidence_level
        )
        
        if not exit_strategy:
            return jsonify({'error': 'Could not calculate exit strategy'}), 400
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'side': side,
            'exit_strategy': exit_strategy,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error in exit strategy calculation: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy/full-analysis', methods=['POST'])
def full_strategy_analysis():
    """Perform complete strategy analysis for a potential trade"""
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        side = data.get('side')
        available_balance = float(data.get('available_balance', 0))
        technical_indicators = data.get('technical_indicators', {})
        
        if not symbol or not side or available_balance <= 0:
            return jsonify({'error': 'Valid symbol, side, and balance are required'}), 400
        
        # Get current price
        current_price = get_current_price(symbol)
        if not current_price:
            return jsonify({'error': f'Could not get current price for {symbol}'}), 400
        
        # 1. Market Analysis
        market_analysis = strategy_engine.analyze_market_state(symbol)
        if not market_analysis:
            return jsonify({'error': f'Could not analyze market for {symbol}'}), 400
        
        # 2. Entry Analysis
        entry_analysis = strategy_engine.should_open_position(
            symbol, side, {'technical_indicators': technical_indicators}
        )
        
        confidence_level = entry_analysis.get('confidence_level', 'medium')
        
        # 3. Position Sizing
        position_size = strategy_engine.calculate_position_size(
            symbol, available_balance, current_price, confidence_level
        )
        optimal_leverage = strategy_engine.calculate_optimal_leverage(symbol, confidence_level)
        
        # 4. Exit Strategy
        exit_strategy = None
        if position_size and entry_analysis.get('should_enter', False):
            exit_strategy = strategy_engine.calculate_exit_points(
                symbol, side, current_price, position_size, confidence_level
            )
        
        # 5. Risk Assessment
        position_value = position_size * current_price if position_size else 0
        risk_percentage = (position_value / available_balance) * 100 if position_size else 0
        
        # 6. Trade Recommendation
        recommendation = {
            'action': 'ENTER' if entry_analysis.get('should_enter', False) else 'WAIT',
            'confidence': entry_analysis.get('confidence', 0),
            'confidence_level': confidence_level,
            'reasons': [],
            'warnings': []
        }
        
        # Add reasons and warnings
        if entry_analysis.get('should_enter', False):
            recommendation['reasons'].append(f"High confidence entry signal ({confidence_level})")
            recommendation['reasons'].append(f"Market score: {market_analysis.get('overall_score', 0):.2f}")
        else:
            recommendation['warnings'].append("Entry conditions not met")
            recommendation['warnings'].append("Wait for better market conditions")
        
        if market_analysis.get('volatility', {}).get('risk_level') == 'high':
            recommendation['warnings'].append("High volatility detected")
        
        if market_analysis.get('orderbook', {}).get('liquidity_score', 0) < 0.5:
            recommendation['warnings'].append("Low liquidity conditions")
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'side': side,
            'current_price': current_price,
            'market_analysis': market_analysis,
            'entry_analysis': entry_analysis,
            'position_sizing': {
                'position_size': position_size,
                'position_value': position_value,
                'optimal_leverage': optimal_leverage,
                'risk_percentage': risk_percentage
            } if position_size else None,
            'exit_strategy': exit_strategy,
            'recommendation': recommendation,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error in full strategy analysis: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/close-position', methods=['POST'])
def close_position():
    """Close an existing position"""
    try:
        data = request.get_json()
        symbol = data.get('symbol')
        
        if not symbol:
            return jsonify({'error': 'Symbol is required'}), 400
        
        logger.info(f"🔄 Attempting to close position for {symbol}")
        logger.info(f"DEBUG: exchange object exists: {exchange is not None}")
        logger.info(f"DEBUG: account object exists: {account is not None}")
        
        # Get current positions to validate the position exists
        if not exchange:
            logger.error("❌ Exchange not initialized")
            return jsonify({'error': 'Exchange not initialized'}), 400
        
        # Use the same wallet that the exchange object was initialized with
        wallet_address = account.address if account else os.getenv('HYPERLIQUID_MAIN_WALLET')
        logger.info(f"📍 Using wallet address for positions: {wallet_address}")
        logger.info(f"📍 Exchange wallet (account.address): {account.address if account else 'None'}")
        logger.info(f"📍 Main wallet from env: {os.getenv('HYPERLIQUID_MAIN_WALLET')}")
        
        if not wallet_address:
            logger.error("❌ No wallet address available")
            return jsonify({'error': 'No wallet address available'}), 400
            
        logger.info(f"🔍 Fetching user state for wallet: {wallet_address}")
        try:
            user_state = exchange.info.user_state(wallet_address)
            asset_positions = user_state.get('assetPositions', [])
            logger.info(f"📊 Found {len(asset_positions)} asset positions")
        except Exception as user_state_error:
            logger.error(f"DEBUG: Failed to get user_state: {str(user_state_error)}")
            logger.error(f"DEBUG: Error type: {type(user_state_error)}")
            import traceback
            logger.error(f"DEBUG: Full traceback: {traceback.format_exc()}")
            return jsonify({'error': f'Failed to get user state: {str(user_state_error)}'}), 500
        
        # Find the position to close
        position_to_close = None
        logger.info(f"🔍 Searching for {symbol} position in {len(asset_positions)} positions")
        
        for i, position in enumerate(asset_positions):
            pos_data = position.get('position', {})
            coin = pos_data.get('coin', 'N/A')
            szi = pos_data.get('szi', '0')
            logger.info(f"   Position {i}: {coin} = {szi}")
            
            if pos_data.get('coin') == symbol and pos_data.get('szi', '0') != '0':
                position_to_close = pos_data
                logger.info(f"✅ Found matching position: {symbol} = {szi}")
                break
        
        if not position_to_close:
            logger.error(f"❌ No open position found for {symbol}")
            return jsonify({'error': f'No open position found for {symbol}'}), 400
        
        # Get position details
        position_size = abs(float(position_to_close['szi']))  # Use absolute value
        is_long = float(position_to_close['szi']) > 0
        
        logger.info(f"📊 Found position: {symbol}, Size: {position_size}, Is Long: {is_long}")
        
        # Get current price for the asset
        logger.info(f"💰 Fetching current price for {symbol}")
        all_mids = exchange.info.all_mids()
        current_price = float(all_mids.get(symbol, 0))
        logger.info(f"💰 Current price for {symbol}: ${current_price}")
        
        if not current_price:
            logger.error(f"❌ Could not get current price for {symbol}")
            return jsonify({'error': f'Could not get current price for {symbol}'}), 400
        
        # Place a reduce-only market order to close the position
        # For closing: if we have a long position, we need to sell; if short, we need to buy
        close_side_is_buy = not is_long  # Opposite of current position
        
        from hyperliquid.utils.signing import OrderType
        
        try:
            # Use market order with minimal slippage for quick execution
            market_price = current_price * (1.001 if close_side_is_buy else 0.999)  # 0.1% slippage
            
            logger.info(f"🎯 Placing close order:")
            logger.info(f"   Symbol: {symbol}")
            logger.info(f"   Is Buy: {close_side_is_buy}")
            logger.info(f"   Size: {position_size}")
            logger.info(f"   Market Price: ${market_price}")
            logger.info(f"   Reduce Only: True")
            
            order_result = exchange.order(
                name=symbol,
                is_buy=close_side_is_buy,
                sz=position_size,
                limit_px=market_price,
                order_type=OrderType({"limit": {"tif": "Gtc"}}),
                reduce_only=True  # This is the key parameter for closing positions
            )
            
            logger.info(f"✅ Close position order result: {order_result}")
            
            # Check for errors in the response
            if isinstance(order_result, dict) and 'error' in order_result:
                error_msg = order_result['error']
                logger.error(f"❌ Order rejected by exchange: {error_msg}")
                return jsonify({'error': f'Order rejected: {error_msg}'}), 400
            
            # Add activity record
            add_activity('position_closed', {
                'symbol': symbol,
                'size': position_size,
                'side': 'sell' if is_long else 'buy',
                'price': current_price,
                'type': 'market',
                'order_result': order_result
            })
            
            return jsonify({
                'success': True,
                'message': f'Position closed successfully for {symbol}',
                'details': {
                    'symbol': symbol,
                    'closed_size': position_size,
                    'close_price': current_price,
                    'was_long': is_long,
                    'order_result': order_result
                }
            })
            
        except Exception as order_error:
            logger.error(f"❌ Failed to place close order: {str(order_error)}")
            logger.error(f"❌ Error type: {type(order_error)}")
            import traceback
            logger.error(f"❌ Full traceback: {traceback.format_exc()}")
            return jsonify({'error': f'Failed to close position: {str(order_error)}'}), 400
            
    except Exception as e:
        logger.error(f"❌ Close position error: {str(e)}")
        return jsonify({'error': f'Failed to close position: {str(e)}'}), 500

@app.route('/api/close-all-positions', methods=['POST'])
def close_all_positions():
    """Close all open positions"""
    try:
        logger.info("🔄 Attempting to close ALL positions")
        logger.info(f"DEBUG: exchange object exists: {exchange is not None}")
        logger.info(f"DEBUG: account object exists: {account is not None}")
        
        if not exchange:
            return jsonify({'error': 'Exchange not initialized'}), 400
        
        # Use the same wallet that the exchange object was initialized with
        wallet_address = account.address if account else os.getenv('HYPERLIQUID_MAIN_WALLET')
        logger.info(f"DEBUG: wallet_address = {wallet_address}")
        logger.info(f"DEBUG: Exchange wallet (account.address): {account.address if account else 'None'}")
        logger.info(f"DEBUG: Main wallet from env: {os.getenv('HYPERLIQUID_MAIN_WALLET')}")
        
        if not wallet_address:
            logger.error("DEBUG: No wallet address available")
            return jsonify({'error': 'No wallet address available'}), 400
            
        logger.info(f"DEBUG: About to call exchange.info.user_state({wallet_address})")
        try:
            user_state = exchange.info.user_state(wallet_address)
            logger.info(f"DEBUG: Got user_state with keys: {list(user_state.keys()) if user_state else 'None'}")
        except Exception as user_state_error:
            logger.error(f"DEBUG: Failed to get user_state: {str(user_state_error)}")
            logger.error(f"DEBUG: Error type: {type(user_state_error)}")
            import traceback
            logger.error(f"DEBUG: Full traceback: {traceback.format_exc()}")
            return jsonify({'error': f'Failed to get user state: {str(user_state_error)}'}), 500
        
        asset_positions = user_state.get('assetPositions', [])
        logger.info(f"DEBUG: Found {len(asset_positions)} asset positions")
        
        # Find all positions to close
        positions_to_close = []
        for position in asset_positions:
            pos_data = position.get('position', {})
            if pos_data.get('szi', '0') != '0':
                positions_to_close.append({
                    'symbol': pos_data['coin'],
                    'size': abs(float(pos_data['szi'])),
                    'is_long': float(pos_data['szi']) > 0
                })
        
        if not positions_to_close:
            return jsonify({'error': 'No open positions to close'}), 400
        
        logger.info(f"📊 Found {len(positions_to_close)} positions to close")
        
        # Get current prices
        all_mids = exchange.info.all_mids()
        
        from hyperliquid.utils.signing import OrderType
        
        closed_positions = []
        failed_positions = []
        
        # Close each position
        for position in positions_to_close:
            symbol = position['symbol']
            position_size = position['size']
            is_long = position['is_long']
            
            try:
                current_price = float(all_mids.get(symbol, 0))
                if not current_price:
                    failed_positions.append({'symbol': symbol, 'error': 'Could not get current price'})
                    continue
                
                # Determine close direction (opposite of current position)
                close_side_is_buy = not is_long
                market_price = current_price * (1.001 if close_side_is_buy else 0.999)  # 0.1% slippage
                
                order_result = exchange.order(
                    name=symbol,
                    is_buy=close_side_is_buy,
                    sz=position_size,
                    limit_px=market_price,
                    order_type=OrderType({"limit": {"tif": "Gtc"}}),
                    reduce_only=True
                )
                
                logger.info(f"✅ Closed {symbol} position: {order_result}")
                
                # Check for errors
                if isinstance(order_result, dict) and 'error' in order_result:
                    failed_positions.append({'symbol': symbol, 'error': order_result['error']})
                else:
                    closed_positions.append({
                        'symbol': symbol,
                        'size': position_size,
                        'price': current_price,
                        'was_long': is_long,
                        'order_result': order_result
                    })
                    
                    # Add activity record
                    add_activity('position_closed', {
                        'symbol': symbol,
                        'size': position_size,
                        'side': 'sell' if is_long else 'buy',
                        'price': current_price,
                        'type': 'market',
                        'order_result': order_result
                    })
                    
            except Exception as e:
                logger.error(f"❌ Failed to close {symbol}: {str(e)}")
                failed_positions.append({'symbol': symbol, 'error': str(e)})
        
        # Prepare response
        success_count = len(closed_positions)
        total_count = len(positions_to_close)
        
        response_data = {
            'success': success_count > 0,
            'message': f'Closed {success_count}/{total_count} positions',
            'closed_positions': closed_positions,
            'failed_positions': failed_positions,
            'summary': {
                'total_positions': total_count,
                'successfully_closed': success_count,
                'failed_to_close': len(failed_positions)
            }
        }
        
        if success_count == total_count:
            logger.info(f"✅ Successfully closed all {success_count} positions")
            return jsonify(response_data)
        elif success_count > 0:
            logger.warning(f"⚠️ Partially successful: closed {success_count}/{total_count} positions")
            return jsonify(response_data), 207  # Multi-status
        else:
            logger.error(f"❌ Failed to close any positions")
            return jsonify(response_data), 400
            
    except Exception as e:
        logger.error(f"❌ Close all positions error: {str(e)}")
        return jsonify({'error': f'Failed to close positions: {str(e)}'}), 500

@app.route('/api/order', methods=['POST'])
def place_order():
    """Place a new order with advanced strategy logic"""
    try:
        data = request.get_json()
        symbol = data.get('pair')
        market_type = data.get('market_type', 'perpetual')
        order_type = data.get('type')
        side = data.get('side')
        size = data.get('size')
        price = data.get('price')
        leverage = data.get('leverage', 1)
        
        if not all([symbol, order_type, side, size]):
            return jsonify({'error': 'Missing required parameters'}), 400
        
        # Get asset info
        asset_info = get_asset_info(symbol)
        if not asset_info:
            return jsonify({'error': f'Asset {symbol} not found'}), 400
        
        # Get current price for validation
        current_price = get_current_price(symbol)
        if not current_price:
            return jsonify({'error': f'Could not get current price for {symbol}'}), 400
        
        # Check strategy conditions with enhanced analysis
        # Use more lenient validation for both spot and perpetual orders
        if market_type == 'spot':
            # Simplified validation for spot orders
            entry_analysis = {
                'should_enter': True,
                'confidence': 0.75,
                'confidence_level': 'medium',
                'analysis': {'spot_override': True, 'message': 'Spot order validation bypassed'}
            }
            logger.info(f"Spot order validation bypassed for {symbol} {side}")
        else:
            # More lenient validation for perpetual orders
            entry_analysis = strategy_engine.should_open_position(symbol, side, data)
            if not entry_analysis.get('should_enter', False):
                # Override strategy rejection for manual orders
                logger.warning(f"Strategy conditions not met for {symbol} {side}, but allowing manual override")
                entry_analysis = {
                    'should_enter': True,
                    'confidence': 0.60,
                    'confidence_level': 'medium',
                    'analysis': {'manual_override': True, 'message': 'Manual order override applied'}
                }
        
        confidence_level = entry_analysis.get('confidence_level', 'medium')
        
        # Calculate position size - CRITICAL FIX: Apply leverage to margin amount
        order_price = price if order_type == 'limit' else current_price
        
        # Size is the margin amount (what user wants to spend)
        # Total position value = margin amount × leverage
        total_position_value = size * leverage
        
        # Convert total position value to token quantity
        position_size_tokens = total_position_value / order_price
        position_size = round_size(position_size_tokens, asset_info['szDecimals'])
        
        # Calculate actual order value (should equal total_position_value)
        order_value = position_size * order_price
        
        logger.info(f"Position calculation: Margin ${size} × {leverage}x leverage = ${total_position_value} position value")
        logger.info(f"Token quantity: ${total_position_value} ÷ ${order_price} = {position_size} {symbol}")
        
        # Validate minimum order value
        if order_value < MIN_POSITION_VALUE:
            error_msg = f'Order value (${order_value:.2f}) is below minimum required value of ${MIN_POSITION_VALUE}'
            logger.warning(error_msg)
            return jsonify({'error': error_msg}), 400
        
        # Calculate exit points with confidence level
        exit_points = strategy_engine.calculate_exit_points(
            symbol,
            side,
            order_price,
            position_size,
            confidence_level
        )
        
        if not exit_points:
            return jsonify({'error': 'Failed to calculate exit points'}), 400
        
        # Place order
        if exchange:
            try:
                if market_type == 'spot':
                    # For spot trading, we need to use the order method with proper OrderType
                    from hyperliquid.utils.signing import OrderType
                    
                    if order_type == 'market':
                        # For market orders on spot, use a limit order with minimal slippage
                        market_price = current_price * (1.001 if side == 'buy' else 0.999)  # 0.1% slippage
                        order_result = exchange.order(
                            name=symbol,
                            is_buy=(side == 'buy'),
                            sz=position_size,
                            limit_px=market_price,
                            order_type=OrderType({"limit": {"tif": "Gtc"}}),
                            reduce_only=False
                        )
                    else:  # limit order
                        if not price:
                            return jsonify({'error': 'Limit price required for limit orders'}), 400
                        order_result = exchange.order(
                            name=symbol,
                            is_buy=(side == 'buy'),
                            sz=position_size,
                            limit_px=price,
                            order_type=OrderType({"limit": {"tif": "Gtc"}}),
                            reduce_only=False
                        )
                else:
                    # Place perpetual order
                    if order_type == 'market':
                        order_result = exchange.market_open(
                            symbol,
                            is_buy=(side == 'buy'),
                            sz=position_size
                        )
                    else:  # limit order
                        if not price:
                            return jsonify({'error': 'Limit price required for limit orders'}), 400
                        # Use the order method for limit orders on perpetuals too
                        from hyperliquid.utils.signing import OrderType
                        order_result = exchange.order(
                            name=symbol,
                            is_buy=(side == 'buy'),
                            sz=position_size,
                            limit_px=price,
                            order_type=OrderType({"limit": {"tif": "Gtc"}}),
                            reduce_only=False
                        )
                
                # Check for errors in the response
                if order_result.get('status') == 'ok':
                    response_data = order_result.get('response', {}).get('data', {})
                    statuses = response_data.get('statuses', [])
                    
                    if statuses and 'error' in statuses[0]:
                        error_msg = statuses[0]['error']
                        logger.error(f"Order rejected by exchange: {error_msg}")
                        return jsonify({'error': error_msg}), 400
                    
                    logger.info(f"Order placed successfully: {order_result}")
                    
                    # Log the strategy analysis
                    strategy_log = {
                        'market_state': strategy_engine.market_state.get(symbol, {}),
                        'exit_strategy': exit_points,
                        'order_details': {
                            'symbol': symbol,
                            'market_type': market_type,
                            'side': side,
                            'size': position_size,
                            'price': price,
                            'type': order_type,
                            'leverage': leverage,
                            'value': order_value
                        }
                    }
                    
                    add_activity('order_placed', 
                        f'Placed {side.upper()} {market_type} order for {position_size} {symbol} with advanced strategy',
                        strategy_log
                    )
                    
                    return jsonify({
                        'success': True,
                        'order': strategy_log['order_details'],
                        'exit_strategy': exit_points,
                        'market_analysis': strategy_engine.market_state.get(symbol, {}),
                        'result': order_result
                    })
                else:
                    error_msg = f"Order placement failed: {order_result.get('error', 'Unknown error')}"
                    logger.error(error_msg)
                    return jsonify({'error': error_msg}), 400
                
            except Exception as e:
                error_msg = f"Order placement failed: {str(e)}"
                logger.error(error_msg)
                return jsonify({'error': error_msg}), 500
        else:
            # Simulation mode
            logger.info(f"SIMULATION: Would place {side} {market_type} order for {position_size} {symbol}")
            
            strategy_log = {
                'market_state': strategy_engine.market_state.get(symbol, {}),
                'exit_strategy': exit_points,
                'order_details': {
                    'symbol': symbol,
                    'market_type': market_type,
                    'side': side,
                    'size': position_size,
                    'price': price,
                    'type': order_type,
                    'leverage': leverage,
                    'value': order_value
                }
            }
            
            add_activity('order_simulated', 
                f'Simulated {side.upper()} {market_type} order for {position_size} {symbol} with advanced strategy',
                strategy_log
            )
            
            return jsonify({
                'success': True,
                'simulation': True,
                'order': strategy_log['order_details'],
                'exit_strategy': exit_points,
                'market_analysis': strategy_engine.market_state.get(symbol, {})
            })
            
    except Exception as e:
        error_msg = f"Order placement error: {str(e)}"
        logger.error(error_msg)
        return jsonify({'error': error_msg}), 500

@app.route('/api/volume24h', methods=['GET'])
def get_24h_volume():
    try:
        main_wallet = os.getenv('HYPERLIQUID_MAIN_WALLET', '')
        logger.info(f"DEBUG: /api/volume24h called for main_wallet: {main_wallet}")
        
        # Try multiple approaches to get volume data
        
        # Method 1: Try user_fees if available
        try:
            if hasattr(info, 'user_fees'):
                logger.info("DEBUG: Trying user_fees method...")
                user_fees = info.user_fees(main_wallet)
                logger.info(f"DEBUG: /api/volume24h raw user_fees response: {user_fees}")
                daily_vlm = user_fees.get('dailyUserVlm', [])
                logger.info(f"DEBUG: /api/volume24h dailyUserVlm: {daily_vlm}")
                if daily_vlm:
                    last_day = daily_vlm[-1]
                    logger.info(f"DEBUG: /api/volume24h last_day: {last_day}")
                    volume = float(last_day.get('userAdd', 0)) + float(last_day.get('userCross', 0))
                    logger.info(f"DEBUG: /api/volume24h calculated volume from user_fees: {volume}")
                    return jsonify({'volume24h': volume})
                else:
                    logger.info("DEBUG: No daily volume data found in user_fees")
            else:
                logger.warning("DEBUG: user_fees method not available on this SDK version")
        except Exception as e:
            logger.error(f"DEBUG: user_fees method failed: {e}")
        
        # Method 2: Calculate volume from recent fills
        try:
            logger.info("DEBUG: Trying to calculate volume from user_fills...")
            from datetime import datetime, timedelta
            
            # Get fills from last 24 hours
            fills = info.user_fills(main_wallet)
            logger.info(f"DEBUG: Got {len(fills)} total fills")
            
            # Calculate 24h volume from fills
            now = datetime.now()
            twenty_four_hours_ago = now - timedelta(hours=24)
            twenty_four_hours_ago_ms = int(twenty_four_hours_ago.timestamp() * 1000)
            
            volume_24h = 0
            recent_fills = 0
            
            for fill in fills:
                fill_time = fill.get('time', 0)
                if fill_time >= twenty_four_hours_ago_ms:
                    recent_fills += 1
                    # Volume = price * size
                    price = float(fill.get('px', 0))
                    size = float(fill.get('sz', 0))
                    fill_volume = price * size
                    volume_24h += fill_volume
                    logger.debug(f"DEBUG: Fill - {fill.get('coin')} {price} x {size} = ${fill_volume}")
            
            logger.info(f"DEBUG: Calculated 24h volume from {recent_fills} recent fills: ${volume_24h}")
            return jsonify({'volume24h': volume_24h})
            
        except Exception as e:
            logger.error(f"DEBUG: user_fills calculation failed: {e}")
        
        # Method 3: Return 0 with helpful message
        logger.info("DEBUG: All volume calculation methods failed, returning 0")
        return jsonify({
            'volume24h': 0,
            'message': 'Unable to calculate 24h volume - no trading data available or SDK version too old'
        })
        
    except Exception as e:
        logger.error(f"DEBUG: /api/volume24h error: {e}")
        if "Trading user must request before staking user" in str(e):
            return jsonify({'error': "Staking link error: Trading user must request before staking user. Please make a trade on Hyperliquid to enable volume tracking.", 'volume24h': 0}), 200
        return jsonify({'error': str(e), 'volume24h': 0}), 500

@app.route('/api/test/positions', methods=['GET'])
def test_positions():
    """Test endpoint to debug positions issue"""
    try:
        # Use main wallet address for positions (not API wallet)
        wallet_address = os.getenv('HYPERLIQUID_MAIN_WALLET') or ''
        logger.info(f"TEST: Fetching positions for main wallet: {wallet_address}")
        
        user_state = info.user_state(wallet_address)
        logger.info(f"TEST: Got user_state with keys: {list(user_state.keys())}")
        
        if 'assetPositions' not in user_state:
            logger.error("TEST: No assetPositions in user_state")
            return jsonify({'error': 'No assetPositions found', 'user_state_keys': list(user_state.keys())})
        
        asset_positions = user_state['assetPositions']
        logger.info(f"TEST: Found {len(asset_positions)} asset positions")
        
        all_mids = info.all_mids()
        logger.info(f"TEST: Got {len(all_mids)} price data points")
        
        positions = []
        for i, position in enumerate(asset_positions):
            logger.info(f"TEST: Processing position {i}: {position}")
            
            if 'position' not in position:
                logger.error(f"TEST: Position {i} missing 'position' key")
                continue
                
            pos_data = position['position']
            logger.info(f"TEST: Position {i} data keys: {list(pos_data.keys())}")
            
            if 'szi' not in pos_data:
                logger.error(f"TEST: Position {i} missing 'szi' key")
                continue
                
            size_str = pos_data['szi']
            logger.info(f"TEST: Position {i} size string: '{size_str}'")
            
            if size_str == '0':
                logger.info(f"TEST: Position {i} has zero size, skipping")
                continue
                
            # Extract position details
            symbol = pos_data['coin']
            current_price = float(all_mids.get(symbol, 0))
            size = float(size_str)
            entry_price = float(pos_data['entryPx'])
            
            logger.info(f"TEST: Position {i} - Symbol: {symbol}, Size: {size}, Entry: {entry_price}, Current: {current_price}")
            
            # Calculate P&L
            unrealized_pnl = size * (current_price - entry_price)
            pnl_percentage = (unrealized_pnl / (abs(size) * entry_price)) * 100 if size != 0 else 0
            
            position_result = {
                'symbol': symbol,
                'side': 'LONG' if size > 0 else 'SHORT',
                'size': abs(size),
                'entryPrice': entry_price,
                'currentPrice': current_price,
                'unrealizedPnl': unrealized_pnl,
                'pnlPercentage': pnl_percentage,
            }
            
            positions.append(position_result)
            logger.info(f"TEST: Added position: {position_result}")
        
        logger.info(f"TEST: Returning {len(positions)} positions")
        return jsonify({'positions': positions, 'count': len(positions)})
        
    except Exception as e:
        logger.error(f"TEST: Error in test_positions: {e}")
        import traceback
        logger.error(f"TEST: Traceback: {traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/buy', methods=['POST'])
def api_buy():
    """Manual buy order endpoint for web interface"""
    try:
        if not exchange:
            return jsonify({'error': 'Exchange not initialized - no private key available'}), 400
            
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
            
        # Required parameters
        symbol = data.get('symbol', '').upper()
        size = data.get('size')
        
        if not symbol:
            return jsonify({'error': 'Symbol is required'}), 400
        if not size:
            return jsonify({'error': 'Size is required'}), 400
            
        try:
            size = float(size)
            if size <= 0:
                return jsonify({'error': 'Size must be positive'}), 400
        except ValueError:
            return jsonify({'error': 'Invalid size format'}), 400
            
        # Optional parameters
        price = data.get('price')  # If provided, place limit order, otherwise market order
        leverage = int(data.get('leverage', 1))
        
        # Take Profit and Stop Loss parameters
        take_profit_price = data.get('take_profit_price')  # Optional TP price
        stop_loss_price = data.get('stop_loss_price')      # Optional SL price
        take_profit_percentage = data.get('take_profit_percentage')  # Optional TP percentage
        stop_loss_percentage = data.get('stop_loss_percentage')      # Optional SL percentage
        
        logger.info(f"🟢 BUY ORDER: {symbol} - Size: {size}, Price: {price or 'Market'}, Leverage: {leverage}x")
        
        # Get asset info
        asset_info = get_asset_info(symbol)
        if not asset_info:
            return jsonify({'error': f'Asset {symbol} not found'}), 400
            
        # Get current price for validation
        current_price = get_current_price(symbol)
        if not current_price:
            return jsonify({'error': f'Could not get price for {symbol}'}), 400
            
        # Round size according to asset requirements
        rounded_size = round_size(size, asset_info['szDecimals'])
        
        # Check minimum position value
        position_value = rounded_size * current_price
        if position_value < MIN_POSITION_VALUE:
            return jsonify({'error': f'Position value ${position_value:.2f} is below minimum ${MIN_POSITION_VALUE}'}), 400
            
        # Get balance from main wallet (where funds are) but trade with API wallet
        main_wallet_address = os.getenv('HYPERLIQUID_MAIN_WALLET')
        if not main_wallet_address:
            main_wallet_address = account.address  # Fallback to API wallet
        
        available_balance = get_account_balance(main_wallet_address)
        logger.info(f"Checking balance from main wallet: {main_wallet_address}")
        logger.info(f"Trading with API wallet: {account.address}")
        
        if available_balance is None:
            return jsonify({'error': 'Could not get account balance'}), 400
            
        # Check if balance is sufficient (with leverage)
        required_margin = position_value / leverage
        if required_margin > available_balance:
            return jsonify({'error': f'Insufficient balance. Required: ${required_margin:.2f}, Available: ${available_balance:.2f}'}), 400
            
        logger.info(f"Placing BUY order for {rounded_size} {symbol} (${position_value:.2f} with {leverage}x leverage)")
        
        # Place the order
        if price:
            # Limit order
            order_result = exchange.order(
                symbol,
                is_buy=True,
                sz=rounded_size,
                limit_px=float(price),
                order_type={'limit': {'tif': 'Gtc'}}
            )
        else:
            # Market order
            order_result = exchange.market_open(
                symbol,
                is_buy=True,
                sz=rounded_size,
                px=None
            )
            
        logger.info(f"✅ BUY order placed successfully: {order_result}")
        
        # Handle Take Profit and Stop Loss orders
        tp_sl_results = []
        entry_price = price or current_price
        
        # Calculate TP/SL prices if percentages are provided
        if take_profit_percentage:
            try:
                tp_pct = float(take_profit_percentage)
                take_profit_price = entry_price * (1 + tp_pct / 100)
                logger.info(f"Calculated TP price from {tp_pct}%: {take_profit_price}")
            except (ValueError, TypeError):
                logger.warning(f"Invalid take_profit_percentage: {take_profit_percentage}")
                
        if stop_loss_percentage:
            try:
                sl_pct = float(stop_loss_percentage)
                stop_loss_price = entry_price * (1 - sl_pct / 100)
                logger.info(f"Calculated SL price from {sl_pct}%: {stop_loss_price}")
            except (ValueError, TypeError):
                logger.warning(f"Invalid stop_loss_percentage: {stop_loss_percentage}")
        
        # Place Take Profit order if specified
        if take_profit_price:
            try:
                tp_price = float(take_profit_price)
                if tp_price > entry_price:  # TP should be above entry for long position
                    logger.info(f"Placing Take Profit order at {tp_price}")
                    tp_result = exchange.order(
                        symbol,
                        is_buy=False,  # Sell to take profit on long position
                        sz=rounded_size,
                        limit_px=tp_price,
                        order_type={'limit': {'tif': 'Gtc'}}
                    )
                    tp_sl_results.append({
                        'type': 'take_profit',
                        'price': tp_price,
                        'result': tp_result
                    })
                    logger.info(f"✅ Take Profit order placed: {tp_result}")
                else:
                    logger.warning(f"Take Profit price {tp_price} must be above entry price {entry_price}")
            except Exception as e:
                logger.error(f"Failed to place Take Profit order: {e}")
                tp_sl_results.append({
                    'type': 'take_profit',
                    'error': str(e)
                })
        
        # Place Stop Loss order if specified
        if stop_loss_price:
            try:
                sl_price = float(stop_loss_price)
                if sl_price < entry_price:  # SL should be below entry for long position
                    logger.info(f"Placing Stop Loss order at {sl_price}")
                    sl_result = exchange.order(
                        symbol,
                        is_buy=False,  # Sell to stop loss on long position
                        sz=rounded_size,
                        limit_px=sl_price,
                        order_type={'limit': {'tif': 'Gtc'}}
                    )
                    tp_sl_results.append({
                        'type': 'stop_loss',
                        'price': sl_price,
                        'result': sl_result
                    })
                    logger.info(f"✅ Stop Loss order placed: {sl_result}")
                else:
                    logger.warning(f"Stop Loss price {sl_price} must be below entry price {entry_price}")
            except Exception as e:
                logger.error(f"Failed to place Stop Loss order: {e}")
                tp_sl_results.append({
                    'type': 'stop_loss',
                    'error': str(e)
                })
        
        # Add to activity log
        add_activity('manual_buy', f'Manual BUY: {rounded_size} {symbol} @ ${price or "Market"}', {
            'symbol': symbol,
            'side': 'BUY',
            'size': rounded_size,
            'price': price or current_price,
            'leverage': leverage,
            'order_result': order_result
        })
        
        # Emit to websocket clients
        socketio.emit('order_placed', {
            'type': 'buy',
            'symbol': symbol,
            'size': rounded_size,
            'price': price or current_price,
            'leverage': leverage,
            'result': order_result
        })
        
        return jsonify({
            'success': True,
            'message': f'BUY order placed successfully',
            'order': {
                'symbol': symbol,
                'side': 'BUY',
                'size': rounded_size,
                'price': price or current_price,
                'leverage': leverage,
                'value': position_value
            },
            'result': order_result,
            'take_profit_stop_loss': tp_sl_results if tp_sl_results else None
        })
        
    except Exception as e:
        logger.error(f"Error in buy order: {e}")
        return jsonify({'error': f'Buy order failed: {str(e)}'}), 500

@app.route('/api/sell', methods=['POST'])
def api_sell():
    """Manual sell order endpoint for web interface"""
    try:
        if not exchange:
            return jsonify({'error': 'Exchange not initialized - no private key available'}), 400
            
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
            
        # Required parameters
        symbol = data.get('symbol', '').upper()
        size = data.get('size')
        
        if not symbol:
            return jsonify({'error': 'Symbol is required'}), 400
        if not size:
            return jsonify({'error': 'Size is required'}), 400
            
        try:
            size = float(size)
            if size <= 0:
                return jsonify({'error': 'Size must be positive'}), 400
        except ValueError:
            return jsonify({'error': 'Invalid size format'}), 400
            
        # Optional parameters
        price = data.get('price')  # If provided, place limit order, otherwise market order
        close_position = data.get('close_position', False)  # If true, close existing position
        
        # Take Profit and Stop Loss parameters
        take_profit_price = data.get('take_profit_price')  # Optional TP price
        stop_loss_price = data.get('stop_loss_price')      # Optional SL price
        take_profit_percentage = data.get('take_profit_percentage')  # Optional TP percentage
        stop_loss_percentage = data.get('stop_loss_percentage')      # Optional SL percentage
        
        logger.info(f"🔴 SELL ORDER: {symbol} - Size: {size}, Price: {price or 'Market'}, Close Position: {close_position}")
        
        # Get asset info
        asset_info = get_asset_info(symbol)
        if not asset_info:
            return jsonify({'error': f'Asset {symbol} not found'}), 400
            
        # Get current price for validation
        current_price = get_current_price(symbol)
        if not current_price:
            return jsonify({'error': f'Could not get price for {symbol}'}), 400
            
        # Round size according to asset requirements
        rounded_size = round_size(size, asset_info['szDecimals'])
        
        # If closing position, check current positions
        if close_position:
            trading_wallet_address = account.address
            user_state = info.user_state(trading_wallet_address)
            positions = user_state.get('assetPositions', [])
            
            current_position = None
            for pos in positions:
                if pos['position']['coin'] == symbol:
                    current_position = pos['position']
                    break
                    
            if not current_position or current_position['szi'] == '0':
                return jsonify({'error': f'No open position found for {symbol}'}), 400
                
            current_size = float(current_position['szi'])
            if current_size <= 0:
                return jsonify({'error': f'No long position to close for {symbol}'}), 400
                
            # Use market_close for position closing
            logger.info(f"Closing position for {symbol} with size {abs(current_size)}")
            
            order_result = exchange.market_close(symbol, sz=abs(current_size))
            
            logger.info(f"✅ Position closed successfully: {order_result}")
            
            # Add to activity log
            add_activity('position_closed', f'Closed position: {symbol} ({abs(current_size)} units)', {
                'symbol': symbol,
                'side': 'CLOSE',
                'size': abs(current_size),
                'price': current_price,
                'order_result': order_result
            })
            
            # Emit to websocket clients
            socketio.emit('position_closed', {
                'symbol': symbol,
                'size': abs(current_size),
                'price': current_price,
                'result': order_result
            })
            
            return jsonify({
                'success': True,
                'message': f'Position closed successfully',
                'order': {
                    'symbol': symbol,
                    'side': 'CLOSE',
                    'size': abs(current_size),
                    'price': current_price
                },
                'result': order_result
            })
        else:
            # Regular sell order (short position)
            position_value = rounded_size * current_price
            
            # Check minimum position value
            if position_value < MIN_POSITION_VALUE:
                return jsonify({'error': f'Position value ${position_value:.2f} is below minimum ${MIN_POSITION_VALUE}'}), 400
                
            logger.info(f"Placing SELL order for {rounded_size} {symbol} (${position_value:.2f})")
            
            # Place the order
            if price:
                # Limit order
                order_result = exchange.order(
                    symbol,
                    is_buy=False,
                    sz=rounded_size,
                    limit_px=float(price),
                    order_type={'limit': {'tif': 'Gtc'}}
                )
            else:
                # Market order
                order_result = exchange.market_open(
                    symbol,
                    is_buy=False,
                    sz=rounded_size,
                    px=None
                )
                
            logger.info(f"✅ SELL order placed successfully: {order_result}")
            
            # Handle Take Profit and Stop Loss orders for short position
            tp_sl_results = []
            entry_price = price or current_price
            
            # Calculate TP/SL prices if percentages are provided
            if take_profit_percentage:
                try:
                    tp_pct = float(take_profit_percentage)
                    take_profit_price = entry_price * (1 - tp_pct / 100)  # TP below entry for short
                    logger.info(f"Calculated TP price from {tp_pct}%: {take_profit_price}")
                except (ValueError, TypeError):
                    logger.warning(f"Invalid take_profit_percentage: {take_profit_percentage}")
                    
            if stop_loss_percentage:
                try:
                    sl_pct = float(stop_loss_percentage)
                    stop_loss_price = entry_price * (1 + sl_pct / 100)  # SL above entry for short
                    logger.info(f"Calculated SL price from {sl_pct}%: {stop_loss_price}")
                except (ValueError, TypeError):
                    logger.warning(f"Invalid stop_loss_percentage: {stop_loss_percentage}")
            
            # Place Take Profit order if specified (for short position)
            if take_profit_price:
                try:
                    tp_price = float(take_profit_price)
                    if tp_price < entry_price:  # TP should be below entry for short position
                        logger.info(f"Placing Take Profit order at {tp_price}")
                        tp_result = exchange.order(
                            symbol,
                            is_buy=True,  # Buy to take profit on short position
                            sz=rounded_size,
                            limit_px=tp_price,
                            order_type={'limit': {'tif': 'Gtc'}}
                        )
                        tp_sl_results.append({
                            'type': 'take_profit',
                            'price': tp_price,
                            'result': tp_result
                        })
                        logger.info(f"✅ Take Profit order placed: {tp_result}")
                    else:
                        logger.warning(f"Take Profit price {tp_price} must be below entry price {entry_price} for short position")
                except Exception as e:
                    logger.error(f"Failed to place Take Profit order: {e}")
                    tp_sl_results.append({
                        'type': 'take_profit',
                        'error': str(e)
                    })
            
            # Place Stop Loss order if specified (for short position)
            if stop_loss_price:
                try:
                    sl_price = float(stop_loss_price)
                    if sl_price > entry_price:  # SL should be above entry for short position
                        logger.info(f"Placing Stop Loss order at {sl_price}")
                        sl_result = exchange.order(
                            symbol,
                            is_buy=True,  # Buy to stop loss on short position
                            sz=rounded_size,
                            limit_px=sl_price,
                            order_type={'limit': {'tif': 'Gtc'}}
                        )
                        tp_sl_results.append({
                            'type': 'stop_loss',
                            'price': sl_price,
                            'result': sl_result
                        })
                        logger.info(f"✅ Stop Loss order placed: {sl_result}")
                    else:
                        logger.warning(f"Stop Loss price {sl_price} must be above entry price {entry_price} for short position")
                except Exception as e:
                    logger.error(f"Failed to place Stop Loss order: {e}")
                    tp_sl_results.append({
                        'type': 'stop_loss',
                        'error': str(e)
                    })
            
            # Add to activity log
            add_activity('manual_sell', f'Manual SELL: {rounded_size} {symbol} @ ${price or "Market"}', {
                'symbol': symbol,
                'side': 'SELL',
                'size': rounded_size,
                'price': price or current_price,
                'order_result': order_result
            })
            
            # Emit to websocket clients
            socketio.emit('order_placed', {
                'type': 'sell',
                'symbol': symbol,
                'size': rounded_size,
                'price': price or current_price,
                'result': order_result
            })
            
            return jsonify({
                'success': True,
                'message': f'SELL order placed successfully',
                'order': {
                    'symbol': symbol,
                    'side': 'SELL',
                    'size': rounded_size,
                    'price': price or current_price,
                    'value': position_value
                },
                'result': order_result,
                'take_profit_stop_loss': tp_sl_results if tp_sl_results else None
            })
        
    except Exception as e:
        logger.error(f"Error in sell order: {e}")
        return jsonify({'error': f'Sell order failed: {str(e)}'}), 500

@app.route('/api/transfer-to-trading', methods=['POST'])
def transfer_to_trading_wallet():
    """Transfer funds between wallets - handles both directions"""
    try:
        if not api_wallet_key:
            return jsonify({'error': 'API wallet private key not configured'}), 400
            
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
            
        amount = data.get('amount')
        direction = data.get('direction', 'main-to-api')  # Default to main-to-api
        
        if not amount:
            return jsonify({'error': 'Amount is required'}), 400
            
        try:
            amount = float(amount)
            if amount <= 0:
                return jsonify({'error': 'Amount must be positive'}), 400
        except ValueError:
            return jsonify({'error': 'Invalid amount format'}), 400
            
        # Get wallet addresses
        api_account = Account.from_key(api_wallet_key)
        api_wallet_address = api_account.address
        main_wallet_address = os.getenv('HYPERLIQUID_MAIN_WALLET')
        
        if not main_wallet_address:
            return jsonify({'error': 'Main wallet address not configured'}), 400
            
        # Initialize API wallet exchange (this is the trading wallet)
        api_exchange = Exchange(api_account, MAINNET_API_URL)
        
        if direction == 'main-to-api':
            # Transfer from main wallet to API wallet
            # Check main wallet balance
            main_state = info.user_state(main_wallet_address)
            main_available = float(main_state['crossMarginSummary']['accountValue']) - float(main_state['crossMarginSummary']['totalMarginUsed'])
            
            if amount > main_available:
                return jsonify({'error': f'Insufficient balance in main wallet. Available: ${main_available:.2f}'}), 400
                
            logger.info(f"💸 Transferring ${amount:.2f} from main wallet to API wallet for trading")
            logger.info(f"From: {main_wallet_address}")
            logger.info(f"To: {api_wallet_address}")
            
            # Note: Hyperliquid main wallets can't directly transfer via API
            # This would need to be done through the Hyperliquid web interface
            # For now, we'll return an informative message
            
            return jsonify({
                'success': False,
                'error': 'Main wallet transfers must be done through Hyperliquid web interface',
                'message': f'To transfer ${amount:.2f} from main wallet to API wallet:',
                'instructions': [
                    '1. Go to Hyperliquid web interface',
                    '2. Connect your main wallet',
                    f'3. Transfer ${amount:.2f} to {api_wallet_address}',
                    '4. Return here to continue trading'
                ],
                'transfer': {
                    'amount': amount,
                    'from_wallet': main_wallet_address,
                    'to_wallet': api_wallet_address,
                    'direction': 'main-to-api'
                }
            })
            
        else:
            # Transfer from API wallet to main wallet (store profits)
            # Check API wallet balance
            api_state = info.user_state(api_wallet_address)
            api_available = float(api_state['crossMarginSummary']['accountValue']) - float(api_state['crossMarginSummary']['totalMarginUsed'])
            
            if amount > api_available:
                return jsonify({'error': f'Insufficient balance in API wallet. Available: ${api_available:.2f}'}), 400
                
            logger.info(f"💸 Transferring ${amount:.2f} from API wallet to main wallet")
            logger.info(f"From: {api_wallet_address}")
            logger.info(f"To: {main_wallet_address}")
            
            # Perform the transfer using API wallet
            result = api_exchange.usd_transfer(
                destination=main_wallet_address,
                amount=amount
            )
            
            logger.info(f"✅ Transfer successful: {result}")
            
            # Add to activity log
            add_activity('wallet_transfer', f'Transferred ${amount:.2f} to main wallet', {
                'from_wallet': 'api',
                'to_wallet': 'main',
                'amount': amount,
                'result': result
            })
            
            return jsonify({
                'success': True,
                'message': f'Successfully transferred ${amount:.2f} to main wallet',
                'transfer': {
                    'amount': amount,
                    'from_wallet': api_wallet_address,
                    'to_wallet': main_wallet_address,
                    'direction': 'api-to-main'
                },
                'result': result
            })
        
    except Exception as e:
        logger.error(f"Error in wallet transfer: {e}")
        return jsonify({'error': f'Transfer failed: {str(e)}'}), 500

# WebSocket event handlers
@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info(f"Client connected: {request.sid}")
    emit('connection_status', connection_status)

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    try:
        # Get all rooms the client is in
        rooms = list(socketio.server.manager.rooms.get(request.sid, set()))
        # Remove client from all rooms
        for room in rooms:
            if room != request.sid:  # Skip the client's own room
                leave_room(room)
        logger.info(f"Client disconnected: {request.sid}")
    except Exception as e:
        logger.error(f"Error during disconnect: {e}")

@socketio.on('subscribe')
def handle_subscribe(data):
    """Handle subscription to trading pair updates"""
    try:
        symbol = data.get('symbol')
        if symbol:
            # Join room for this symbol
            join_room(symbol)
            # Send initial order book and trades
            send_orderbook(symbol)
            send_recent_trades(symbol)
            logger.info(f"Client {request.sid} subscribed to {symbol}")
    except Exception as e:
        logger.error(f"Error in subscription: {e}")

def send_orderbook(symbol):
    """Send order book data to clients"""
    try:
        # Get order book data from Hyperliquid
        l2_snapshot = info.l2_snapshot(symbol)
        
        # Initialize arrays
        asks = []
        bids = []
        
        # Debug logging
        logger.debug(f"Orderbook raw response for {symbol}: {type(l2_snapshot)} - {l2_snapshot}")
        
        # Handle different possible response formats from Hyperliquid API
        if isinstance(l2_snapshot, dict):
            if 'levels' in l2_snapshot:
                # New format with levels array
                for level in l2_snapshot['levels']:
                    try:
                        price = float(level['px'])
                        size = float(level['sz'])
                        if level['side'] == 'A':  # Ask
                            asks.append([price, size])
                        else:  # Bid
                            bids.append([price, size])
                    except (KeyError, ValueError, TypeError) as e:
                        logger.warning(f"Error processing level {level}: {e}")
                        continue
                        
            elif 'asks' in l2_snapshot and 'bids' in l2_snapshot:
                # Handle asks - could be list of lists or list of dicts
                if isinstance(l2_snapshot['asks'], list) and len(l2_snapshot['asks']) > 0:
                    for ask in l2_snapshot['asks']:
                        try:
                            if isinstance(ask, list) and len(ask) >= 2:
                                # Format: [price, size]
                                price = float(ask[0])
                                size = float(ask[1])
                                asks.append([price, size])
                            elif isinstance(ask, dict):
                                # Format: {"px": "price", "sz": "size"}
                                price = float(ask.get('px', ask.get('price', 0)))
                                size = float(ask.get('sz', ask.get('size', 0)))
                                asks.append([price, size])
                        except (IndexError, KeyError, ValueError, TypeError) as e:
                            logger.warning(f"Error processing ask {ask}: {e}")
                            continue
                
                # Handle bids - could be list of lists or list of dicts  
                if isinstance(l2_snapshot['bids'], list) and len(l2_snapshot['bids']) > 0:
                    for bid in l2_snapshot['bids']:
                        try:
                            if isinstance(bid, list) and len(bid) >= 2:
                                # Format: [price, size]
                                price = float(bid[0])
                                size = float(bid[1])
                                bids.append([price, size])
                            elif isinstance(bid, dict):
                                # Format: {"px": "price", "sz": "size"}
                                price = float(bid.get('px', bid.get('price', 0)))
                                size = float(bid.get('sz', bid.get('size', 0)))
                                bids.append([price, size])
                        except (IndexError, KeyError, ValueError, TypeError) as e:
                            logger.warning(f"Error processing bid {bid}: {e}")
                            continue
                            
        elif isinstance(l2_snapshot, list):
            # Handle case where response is directly a list of levels
            for level in l2_snapshot:
                try:
                    if isinstance(level, dict):
                        price = float(level.get('px', level.get('price', 0)))
                        size = float(level.get('sz', level.get('size', 0)))
                        side = level.get('side', level.get('type', ''))
                        
                        if side == 'A' or side.lower() == 'ask':
                            asks.append([price, size])
                        elif side == 'B' or side.lower() == 'bid':
                            bids.append([price, size])
                except (KeyError, ValueError, TypeError) as e:
                    logger.warning(f"Error processing level {level}: {e}")
                    continue
        
        # Sort asks in ascending order (lowest first)
        asks.sort(key=lambda x: x[0] if len(x) > 0 else 0)
        # Sort bids in descending order (highest first)
        bids.sort(key=lambda x: x[0] if len(x) > 0 else 0, reverse=True)
        
        # Take top 10 levels
        asks = asks[:10]
        bids = bids[:10]
        
        # Emit to clients if we have data
        if asks or bids:
            socketio.emit('orderbook_update', {
                'symbol': symbol,
                'asks': asks,
                'bids': bids
            }, room=symbol)
            logger.debug(f"Sent orderbook for {symbol}: {len(asks)} asks, {len(bids)} bids")
        else:
            logger.warning(f"No orderbook data found for {symbol}")
            
    except Exception as e:
        logger.error(f"Error sending orderbook for {symbol}: {e}")
        logger.debug(f"Orderbook error details - Type: {type(l2_snapshot)}, Data: {l2_snapshot}")
        
        # Send empty orderbook to prevent client hanging
        try:
            socketio.emit('orderbook_update', {
                'symbol': symbol,
                'asks': [],
                'bids': [],
                'error': str(e)
            }, room=symbol)
        except Exception as emit_error:
            logger.error(f"Failed to emit error orderbook: {emit_error}")

def send_recent_trades(symbol):
    """Send recent trades to clients"""
    try:
        # Get recent trades from Hyperliquid using user_fills
        # We'll get fills for the last hour to show recent market activity
        from datetime import datetime, timedelta
        
        now = datetime.now()
        one_hour_ago = now - timedelta(hours=1)
        one_hour_ago_ms = int(one_hour_ago.timestamp() * 1000)
        
        # Get fills for the symbol using the main wallet
        main_wallet = os.getenv('HYPERLIQUID_MAIN_WALLET', '')
        fills = info.user_fills(main_wallet)
        recent_trades = []
        
        for fill in fills:
            if fill.get('coin') == symbol and fill.get('time', 0) >= one_hour_ago_ms:
                recent_trades.append({
                    'price': float(fill.get('px', 0)),
                    'size': float(fill.get('sz', 0)),
                    'side': 'buy' if fill.get('side') == 'B' else 'sell',
                    'time': fill.get('time', 0)
                })
        
        # Sort by time, most recent first
        recent_trades.sort(key=lambda x: x['time'], reverse=True)
        # Take most recent 20 trades
        recent_trades = recent_trades[:20]
        
        # Format and emit
        socketio.emit('trade_update', {
            'symbol': symbol,
            'trades': recent_trades
        }, room=symbol)
    except Exception as e:
        logger.error(f"Error sending trades: {e}")
        logger.debug(f"Trades error details: {str(e)}")

# Background task to update order book and trades
def background_market_data():
    """Background thread to update market data"""
    while True:
        try:
            # Get all rooms that are subscribed to symbol updates
            active_symbols = set()
            
            # Only get rooms that are actual symbol subscriptions
            for namespace in socketio.server.manager.rooms:
                for room_name, clients in socketio.server.manager.rooms[namespace].items():
                    # Skip default rooms and client-specific rooms
                    if (room_name and 
                        room_name != '/' and 
                        not room_name.startswith('client_') and 
                        len(clients) > 0 and
                        room_name.isalpha() and
                        len(room_name) <= 10):  # Valid symbol format
                        active_symbols.add(room_name)
            
            # Update market data for active symbols only
            for symbol in active_symbols:
                try:
                    send_orderbook(symbol)
                    send_recent_trades(symbol)
                except Exception as e:
                    logger.error(f"Error updating market data for {symbol}: {e}")
                    
        except Exception as e:
            logger.error(f"Error in market data update: {e}")
        
        # Update every 30 seconds to reduce load and avoid rate limiting
        time.sleep(30)

# Start background market data thread
market_data_thread = threading.Thread(target=background_market_data, daemon=True)
market_data_thread.start()

# Add a test function to verify API connectivity
def test_api_connection():
    """Test the API connection and print detailed diagnostics"""
    try:
        logger.info("Testing API connection...")
        logger.info(f"API URL: {MAINNET_API_URL}")
        
        # Test DNS resolution
        import socket
        try:
            hostname = MAINNET_API_URL.split('://')[1].split('/')[0]
            ip = socket.gethostbyname(hostname)
            logger.info(f"DNS Resolution: {hostname} -> {ip}")
        except Exception as e:
            logger.error(f"DNS Resolution failed: {str(e)}")
        
        # Test HTTPS connection
        import requests
        try:
            response = requests.get(MAINNET_API_URL, timeout=10)
            logger.info(f"HTTPS Connection test: Status {response.status_code}")
        except Exception as e:
            logger.error(f"HTTPS Connection test failed: {str(e)}")
        
        # Test actual API call
        try:
            meta = info.meta()
            # Log only the number of available assets instead of the full response
            asset_count = len(meta['universe'])
            logger.info(f"API connection successful - {asset_count} trading pairs available")
        except Exception as e:
            logger.error(f"API meta call failed: {str(e)}")
            
    except Exception as e:
        logger.error(f"API connection test failed: {str(e)}", exc_info=True)

# Backtester API endpoints with enhanced error handling
@app.route('/api/backtest/run', methods=['POST'])
def run_backtest():
    """Run a backtest with given parameters"""
    try:
        data = request.get_json()
        
        # Extract parameters
        symbol = data.get('symbol', 'AAPL')
        start_date = data.get('start_date', '2023-01-01')
        end_date = data.get('end_date', '2024-01-01')
        strategy = data.get('strategy', 'Simple MA Cross')
        interval = data.get('interval', '1d')
        
        logger.info(f"Running backtest for {symbol} from {start_date} to {end_date} with strategy {strategy} and interval {interval}")
        
        # Create a fresh backtester instance for each request
        backtester = WebBacktester()
        
        # Load data with detailed error handling
        logger.info("Step 1: Loading market data...")
        if not backtester.load_data(symbol, start_date, end_date, interval):
            error_msg = f"Failed to load data for {symbol}. This could be due to:\n"
            error_msg += "• Invalid symbol format\n"
            error_msg += "• Date range too long for selected interval\n"
            error_msg += "• Network issues with data providers\n"
            error_msg += "• Market data not available for the specified period"
            logger.error(error_msg)
            return jsonify({'error': error_msg}), 400
        
        # Calculate indicators with detailed error handling
        logger.info("Step 2: Calculating technical indicators...")
        if not backtester.calculate_indicators():
            error_msg = f"Failed to calculate indicators for {symbol}. This could be due to:\n"
            error_msg += "• Insufficient data points for indicator calculation\n"
            error_msg += "• Data quality issues\n"
            error_msg += "• Memory constraints"
            logger.error(error_msg)
            return jsonify({'error': error_msg}), 400
        
        # Run strategy with detailed error handling
        logger.info("Step 3: Running trading strategy...")
        result = backtester.run_strategy(strategy)
        if result is None:
            error_msg = f"Failed to run strategy '{strategy}' for {symbol}. This could be due to:\n"
            error_msg += "• Strategy not found or not implemented\n"
            error_msg += "• Missing required indicators\n"
            error_msg += "• Data format issues"
            logger.error(error_msg)
            return jsonify({'error': error_msg}), 400
        
        # Get results with detailed error handling
        logger.info("Step 4: Preparing results...")
        results_json = backtester.get_results_json()
        if 'error' in results_json:
            error_msg = f"Failed to prepare results: {results_json['error']}"
            logger.error(error_msg)
            return jsonify({'error': error_msg}), 400
        
        # Return successful results
        logger.info("Backtest completed successfully")
        return jsonify({
            'success': True,
            'symbol': symbol,
            'period': f"{start_date} to {end_date}",
            'strategy': strategy,
            'interval': interval,
            'data_source': getattr(backtester, 'data_source', 'unknown'),
            'data_points': len(backtester.data) if backtester.data is not None else 0,
            'results': results_json
        })
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logger.error(f"Backtest error: {e}")
        logger.error(f"Full traceback: {error_details}")
        
        # Provide detailed error information
        error_msg = f"Backtest failed with error: {str(e)}\n\n"
        error_msg += "Technical details:\n"
        error_msg += f"• Error type: {type(e).__name__}\n"
        error_msg += f"• Error message: {str(e)}\n"
        error_msg += f"• Timestamp: {datetime.now().isoformat()}\n\n"
        error_msg += "Common solutions:\n"
        error_msg += "• Check symbol format (e.g., BTC-USD, AAPL)\n"
        error_msg += "• Use shorter date ranges for 1-minute intervals\n"
        error_msg += "• Ensure dates are in YYYY-MM-DD format\n"
        error_msg += "• Try different time intervals (5m, 15m, 1h, 1d)"
        
        return jsonify({
            'error': error_msg,
            'error_type': type(e).__name__,
            'error_details': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/api/backtest/status', methods=['GET'])
def backtest_status():
    """Get current backtest status"""
    try:
        # Since each request creates a new backtester, we can't track global state
        # Return a basic status indicating the service is ready
        status = {
            'has_data': False,
            'has_results': False,
            'data_length': 0,
            'strategies_run': [],
            'service_status': 'Ready',
            'supported_intervals': ['1m', '5m', '15m', '30m', '1h', '1d', '1wk', '1mo'],
            'supported_strategies': [
                'Simple MA Cross',
                'RSI Mean Reversion', 
                'Bollinger Bands',
                'Crypto Momentum',
                'MACD Strategy',
                'BTC Futures Scalping'
            ]
        }
        
        return jsonify(status)
        
    except Exception as e:
        logger.error(f"Backtest status error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/backtest/results', methods=['GET'])
def get_backtest_results():
    """Get latest backtest results"""
    try:
        # Since each request creates a new backtester, there are no persisted results
        # The frontend should use the results from run response directly
        return jsonify({'error': 'No persisted results available - use results from run response'})
    except Exception as e:
        logger.error(f"Get backtest results error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/realized-pnl', methods=['GET'])
def get_realized_pnl():
    """Get realized P&L from closed trades"""
    # Check cache first
    cache_key = 'realized_pnl'
    cached_result = get_cached_result(cache_key)
    if cached_result:
        logger.info("Returning cached realized P&L result")
        return jsonify(cached_result)
    
    # Rate limiting check
    if not rate_limit_check('realized_pnl'):
        return jsonify({'error': 'Rate limited - please wait before requesting P&L again', 'totalRealizedPnl': 0, 'tradeCount': 0}), 429
    
    try:
        wallet_address = os.getenv('HYPERLIQUID_MAIN_WALLET')
        
        # Get time range for calculation (default: last 30 days)
        from datetime import datetime, timedelta
        
        days = request.args.get('days', '30')
        try:
            days = int(days)
        except:
            days = 30
            
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        start_time_ms = int(start_time.timestamp() * 1000)
        end_time_ms = int(end_time.timestamp() * 1000)
        
        logger.info(f"Fetching realized P&L from {start_time} to {end_time}")
        
        # Get user fills with closed P&L data
        fills = info.user_fills_by_time(wallet_address, start_time_ms, end_time_ms)
        
        total_realized_pnl = 0
        trade_count = 0
        daily_pnl = {}
        
        for fill in fills:
            # Extract closed P&L from the fill
            closed_pnl = fill.get('closedPnl')
            if closed_pnl:
                pnl_value = float(closed_pnl)
                total_realized_pnl += pnl_value
                trade_count += 1
                
                # Group by day for breakdown
                fill_time = datetime.fromtimestamp(fill.get('time', 0) / 1000)
                day_key = fill_time.strftime('%Y-%m-%d')
                
                if day_key not in daily_pnl:
                    daily_pnl[day_key] = 0
                daily_pnl[day_key] += pnl_value
                
                logger.debug(f"Trade: {fill.get('coin')} - Closed P&L: ${pnl_value:.2f}")
        
        logger.info(f"Total realized P&L over {days} days: ${total_realized_pnl:.2f} from {trade_count} trades")
        
        result = {
            'totalRealizedPnl': total_realized_pnl,
            'tradeCount': trade_count,
            'periodDays': days,
            'dailyBreakdown': daily_pnl,
            'startDate': start_time.isoformat(),
            'endDate': end_time.isoformat()
        }
        
        # Cache the result
        set_cache_result(cache_key, result)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error getting realized P&L: {e}")
        return jsonify({
            'totalRealizedPnl': 0,
            'tradeCount': 0,
            'error': str(e)
        })

# Import scanner backend
from scanner_backend import get_scanner

# Scanner API Endpoints
@app.route('/api/scanner/scan-token', methods=['POST'])
def scan_token():
    """Scan a specific token by address"""
    try:
        data = request.get_json()
        token_address = data.get('address')
        
        if not token_address:
            return jsonify({'error': 'Token address required'}), 400
        
        # Use real scanner
        scanner = get_scanner()
        result = scanner.scan_token(token_address)
        
        if result.get('success'):
            return jsonify({
                'success': True,
                'message': f'Successfully scanned {result.get("symbol", "token")}',
                'data': {
                    'address': result.get('address'),
                    'symbol': result.get('symbol'),
                    'name': result.get('name'),
                    'price': result.get('price_usd'),
                    'change_24h': result.get('price_change_24h'),
                    'confidence': result.get('confidence'),
                    'patterns': result.get('patterns', []),
                    'liquidity_grade': result.get('liquidity_grade'),
                    'volume_24h': result.get('volume_24h'),
                    'liquidity_usd': result.get('liquidity_usd')
                }
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error')
            }), 400
            
    except Exception as e:
        logger.error(f"Error scanning token: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/scanner/latest-alerts', methods=['GET'])
def get_latest_alerts():
    """Get latest scanner alerts"""
    try:
        scanner = get_scanner()
        alerts = scanner.get_latest_alerts()
        
        return jsonify({
            'alerts': alerts,
            'count': len(alerts),
            'last_updated': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error fetching alerts: {e}")
        return jsonify({'error': str(e)}), 500

# ==============================
# Minimal Call Bot (Free Plan)
# ==============================

# In-memory state for a lightweight Call Bot so the UI works without Pro APIs
callbot_state = {
    'active_calls': [],
    'history': [],
    'next_id': 1,
    'supported_symbols': ['SOL'],  # Keep minimal and reliable on free sources
    'supported_timeframes': ['5m', '15m', '1h', '4h'],
}

def _map_symbol_to_coingecko_id(symbol: str) -> str | None:
    """Map common symbols to CoinGecko IDs for the free endpoint.
    Extend this map as needed.
    """
    symbol_upper = (symbol or '').upper()
    symbol_map = {
        'SOL': 'solana',
        # Popular Solana ecosystem tokens (best-effort; free plan availability may vary)
        'BONK': 'bonk',
        'JUP': 'jupiter-exchange',
        'PYTH': 'pyth-network',
        'MSOL': 'marinade-staked-sol',
        'WIF': 'dogwifcoin',  # a.k.a. WIF
    }
    return symbol_map.get(symbol_upper)

def _get_price_usd_free(symbol: str) -> float | None:
    """Get USD price using Hyperliquid mids first, then fallback to CoinGecko free API.
    """
    try:
        # Try Hyperliquid mids for well-known assets (e.g., SOL)
        px = get_current_price(symbol)
        if px is not None:
            return float(px)
    except Exception:
        pass

    try:
        # Fallback to CoinGecko simple price (free plan)
        cg_id = _map_symbol_to_coingecko_id(symbol)
        if not cg_id:
            return None
        url = (
            f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
        )
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            val = data.get(cg_id, {}).get('usd')
            return float(val) if val is not None else None
        return None
    except Exception:
        return None

def _emit_calls_update():
    try:
        socketio.emit('calls_update', {
            'active_calls': callbot_state['active_calls'],
            'performance': {
                'total_calls': len(callbot_state['history']) + len(callbot_state['active_calls']),
                'successful_calls': 0,
                'failed_calls': 0,
                'pending_calls': len(callbot_state['active_calls']),
            },
            'last_updated': datetime.now().isoformat()
        })
    except Exception:
        pass

@app.route('/api/calls/status', methods=['GET'])
def callbot_status():
    try:
        return jsonify({
            'available': True,
            'config': {
                'supported_symbols': callbot_state['supported_symbols'],
                'supported_timeframes': callbot_state['supported_timeframes'],
                'min_confidence_threshold': 0.7,
                'max_active_calls': 10,
                'max_history': 100,
            },
            'current_state': {
                'active_calls_count': len(callbot_state['active_calls']),
                'history_count': len(callbot_state['history']),
                'next_call_id': callbot_state['next_id'],
            }
        })
    except Exception as e:
        logger.error(f"Error getting CallBot status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/calls/active', methods=['GET'])
def callbot_active():
    try:
        return jsonify({
            'active_calls': callbot_state['active_calls'],
            'count': len(callbot_state['active_calls'])
        })
    except Exception as e:
        logger.error(f"Error getting active calls: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/calls/generate', methods=['POST'])
def callbot_generate():
    try:
        data = request.get_json(silent=True) or {}
        symbol = (data.get('symbol') or 'SOL').upper()
        timeframe = data.get('timeframe', '1h')
        force_analysis = bool(data.get('force_analysis', False))

        if symbol not in callbot_state['supported_symbols']:
            return jsonify({'error': f'Symbol {symbol} not supported. Supported: {callbot_state["supported_symbols"]}'}), 400

        price = _get_price_usd_free(symbol)
        if price is None:
            return jsonify({'success': False, 'message': f'Could not fetch price for {symbol} using free sources'}), 200

        # Simple confidence heuristic (demo): higher confidence if price fetched reliably
        confidence = 0.78 if force_analysis else 0.72

        call_id = callbot_state['next_id']
        callbot_state['next_id'] += 1

        # Basic LONG call with simple target/stop levels
        target_price = round(price * 1.02, 6)
        stop_price = round(price * 0.98, 6)

        call = {
            'id': call_id,
            'symbol': symbol,
            'direction': 'LONG',
            'timeframe': timeframe,
            'entry_price': price,
            'targets': [
                {'price': target_price, 'percentage': 2.0}
            ],
            'stop_loss': stop_price,
            'confidence': confidence,
            'reasoning': f"Free-plan call generated for {symbol} using public data",
            'timestamp': datetime.now().isoformat(),
            'status': 'active',
            'pnl': 0.0
        }

        callbot_state['active_calls'].insert(0, call)
        _emit_calls_update()

        return jsonify({
            'success': True,
            'call': call,
            'message': f'Generated LONG call for {symbol} with {confidence:.0%} confidence'
        })
    except Exception as e:
        logger.error(f"Error generating call: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/scanner/status', methods=['GET'])
def get_scanner_status():
    """Get scanner status"""
    try:
        scanner = get_scanner()
        status = scanner.get_scanner_status()
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error getting scanner status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/scanner', methods=['GET'])
def scanner_widget():
    """Serve the scanner widget page"""
    try:
        return render_template('scanner_widget.html')
    except Exception as e:
        logger.error(f"Error serving scanner widget: {e}")
        return f"Scanner widget unavailable: {str(e)}", 500

# News storage and functionality
latest_news = []
news_last_updated = None

def fetch_crypto_news():
    """Fetch latest crypto news from multiple sources"""
    global latest_news, news_last_updated
    
    try:
        logger.info("Fetching crypto news...")
        
        # Simple news fetching using NewsAPI or crypto-specific APIs
        news_sources = []
        
        # Try to fetch from CoinGecko API (free tier)
        try:
            coingecko_url = "https://api.coingecko.com/api/v3/news"
            response = session.get(coingecko_url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                for item in data.get('data', [])[:5]:  # Get first 5 news items
                    news_sources.append({
                        'title': item.get('title', 'No title'),
                        'description': item.get('description', '')[:150] + '...' if item.get('description') else 'No description available',
                        'url': item.get('url', '#'),
                        'source': item.get('news_site', 'CoinGecko'),
                        'published_at': item.get('updated_at', datetime.now().isoformat()),
                        'thumb_2x': item.get('thumb_2x', '')
                    })
        except Exception as e:
            logger.warning(f"Failed to fetch CoinGecko news: {e}")
        
        # Try to fetch from CryptoPanic API (free tier)
        try:
            cryptopanic_url = "https://cryptopanic.com/api/v1/posts/?auth_token=free&filter=hot&public=true"
            response = session.get(cryptopanic_url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                for item in data.get('results', [])[:5]:  # Get first 5 news items
                    news_sources.append({
                        'title': item.get('title', 'No title'),
                        'description': 'Click to read more...',
                        'url': item.get('url', '#'),
                        'source': 'CryptoPanic',
                        'published_at': item.get('published_at', datetime.now().isoformat()),
                        'thumb_2x': ''
                    })
        except Exception as e:
            logger.warning(f"Failed to fetch CryptoPanic news: {e}")
        
        # Add some fallback demo news if no real news was fetched
        if not news_sources:
            news_sources = [
                {
                    'title': 'Bitcoin Reaches New Highs Amid Institutional Adoption',
                    'description': 'Bitcoin continues its upward trajectory as more institutions announce crypto adoption strategies...',
                    'url': '#',
                    'source': 'Crypto News',
                    'published_at': datetime.now().isoformat(),
                    'thumb_2x': ''
                },
                {
                    'title': 'Ethereum 2.0 Staking Rewards Attract More Validators',
                    'description': 'The number of Ethereum validators continues to grow as staking rewards remain attractive...',
                    'url': '#',
                    'source': 'DeFi Today',
                    'published_at': (datetime.now() - timedelta(minutes=30)).isoformat(),
                    'thumb_2x': ''
                },
                {
                    'title': 'DeFi Protocol Launches New Yield Farming Opportunities',
                    'description': 'New decentralized finance protocols are offering innovative yield farming strategies...',
                    'url': '#',
                    'source': 'DeFi Pulse',
                    'published_at': (datetime.now() - timedelta(hours=1)).isoformat(),
                    'thumb_2x': ''
                }
            ]
        
        latest_news = news_sources[:10]  # Keep only top 10 news items
        news_last_updated = datetime.now()
        
        logger.info(f"Successfully fetched {len(latest_news)} news items")
        return True
        
    except Exception as e:
        logger.error(f"Error fetching crypto news: {e}")
        return False

def background_news_update():
    """Background thread to update news every 2 minutes"""
    while True:
        try:
            fetch_crypto_news()
            # Emit news update to all connected clients
            socketio.emit('news_update', {
                'news': latest_news,
                'last_updated': news_last_updated.isoformat() if news_last_updated else None
            })
        except Exception as e:
            logger.error(f"Error in background news update: {e}")
        
        # Wait 2 minutes before next update
        time.sleep(120)

@app.route('/api/news', methods=['GET'])
def get_news():
    """Get latest crypto news"""
    try:
        # Fetch fresh news if we don't have any or it's been more than 2 minutes
        if not latest_news or not news_last_updated or \
           (datetime.now() - news_last_updated).total_seconds() > 120:
            fetch_crypto_news()
        
        return jsonify({
            'news': latest_news,
            'last_updated': news_last_updated.isoformat() if news_last_updated else None,
            'count': len(latest_news)
        })
        
    except Exception as e:
        logger.error(f"Error getting news: {e}")
        return jsonify({
            'news': [],
            'last_updated': None,
            'count': 0,
            'error': str(e)
        })

# Start background news update thread
news_thread = threading.Thread(target=background_news_update, daemon=True)
news_thread.start()

# --- Scalp mode -------------------------------------------------------------
scalp_controller = ScalpController(info, exchange)
scalp_controller.start()


@app.route('/scalp')
def scalp_page():
    return app.send_static_file('scalp.html')


@app.route('/scalp/status', methods=['GET'])
def scalp_status():
    return jsonify(scalp_controller.status())


@app.route('/scalp/feed', methods=['GET'])
def scalp_feed():
    limit = int(request.args.get('limit', 100))
    stage = request.args.get('stage')
    return jsonify({'items': scalp_controller.get_feed(limit=limit, stage=stage)})


@app.route('/scalp/equity', methods=['GET'])
def scalp_equity():
    return jsonify({'points': scalp_controller.get_equity()})


@app.route('/scalp/trades', methods=['GET'])
def scalp_trades():
    import scalp_stats
    limit = int(request.args.get('limit', 200))
    return jsonify({'trades': scalp_stats.get_trades(limit=limit)})


@app.route('/scalp/stats', methods=['GET'])
def scalp_stats_route():
    import scalp_stats
    return jsonify(scalp_stats.get_stats())


@app.route('/scalp/chart', methods=['GET'])
def scalp_chart():
    """Recent candles + entry/exit markers + open-position stop/TP for one symbol."""
    import scalp_stats
    symbol = request.args.get('symbol', 'BTC')
    lookback_m = int(request.args.get('lookback', 90))
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_m * 60 * 1000
    candles = []
    try:
        raw = info.candles_snapshot(symbol, '1m', start_ms, end_ms)
        candles = [{
            't': int(c['t']), 'o': float(c['o']), 'h': float(c['h']),
            'l': float(c['l']), 'c': float(c['c']),
        } for c in (raw or [])]
    except Exception as e:
        logger.warning(f"scalp_chart candles failed for {symbol}: {e}")
    markers = scalp_stats.get_markers(symbol)
    pos = scalp_controller.open_pos.get(symbol)
    open_pos = None
    if pos:
        open_pos = {
            'side': pos.side, 'entry': pos.entry,
            'stop_px': pos.stop_px, 'tp_px': pos.tp_px,
            'opened_at': pos.opened_at,
        }
    return jsonify({
        'symbol': symbol, 'candles': candles,
        'entries': markers['entries'], 'exits': markers['exits'],
        'open_pos': open_pos,
    })


@app.route('/scalp/toggle', methods=['POST'])
def scalp_toggle():
    data = request.get_json(silent=True) or {}
    return jsonify(scalp_controller.toggle_live(bool(data.get('on', False))))


@app.route('/scalp/exit-all', methods=['POST'])
def scalp_exit_all():
    return jsonify(scalp_controller.force_exit_all('http'))

# Add the test call when starting the server
if __name__ == '__main__':
    logger.info("Running API connection diagnostics...")
    test_api_connection()
    
    logger.info(f"Starting Hyperliquid webhook server on {SERVER_HOST}:{SERVER_PORT}")
    socketio.run(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        debug=False,
        use_reloader=False,  # Disable reloader in production
        ssl_context=None     # No SSL for now, can be added later if needed
    )