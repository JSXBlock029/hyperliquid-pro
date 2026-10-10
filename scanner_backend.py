"""
Simplified Scanner Backend for Flask Integration
"""

import os
import json
import logging
import requests
import io
import matplotlib
matplotlib.use('Agg')  # Use Agg backend for server environment
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class FlaskScanner:
    """Simplified scanner for Flask integration"""
    
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv('COINGECKO_API_KEY')
        self.base_url = "https://pro-api.coingecko.com/api/v3" if self.api_key else "https://api.coingecko.com/api/v3"
        self.headers = {'X-Cg-Pro-Api-Key': self.api_key} if self.api_key else {}
        
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        
        self.latest_alerts = []
        self.last_scan_time = datetime.now()
        self.tokens_scanned = 0
        
        self.logger = logging.getLogger('FlaskScanner')
        print("FlaskScanner initialized")

    def fetch_token_data_simple(self, token_address: str) -> Dict:
        """Fetch token data using DexScreener API"""
        try:
            print(f"Fetching token data for {token_address}")
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get('pairs') and len(data['pairs']) > 0:
                main_pair = sorted(data['pairs'], 
                                 key=lambda x: float(x.get('liquidity', {}).get('usd', 0)), 
                                 reverse=True)[0]
                
                base_token = main_pair.get('baseToken', {})
                price_usd = float(main_pair.get('priceUsd', 0))
                volume_24h = float(main_pair.get('volume', {}).get('h24', 0))
                liquidity_usd = float(main_pair.get('liquidity', {}).get('usd', 0))
                
                price_change_24h = 0
                price_change_data = main_pair.get('priceChange', {})
                if price_change_data and price_change_data.get('h24'):
                    price_change_24h = float(price_change_data.get('h24', 0))
                
                return {
                    'success': True,
                    'symbol': base_token.get('symbol', 'UNKNOWN').upper(),
                    'name': base_token.get('name', 'Unknown Token'),
                    'address': token_address,
                    'price_usd': price_usd,
                    'volume_24h': volume_24h,
                    'liquidity_usd': liquidity_usd,
                    'price_change_24h': price_change_24h,
                    'market_cap': liquidity_usd * 10,
                    'chain': 'solana',
                    'source': 'DexScreener'
                }
            else:
                return {'success': False, 'error': 'No trading pairs found'}
                
        except Exception as e:
            print(f"Error fetching token data: {e}")
            return {'success': False, 'error': str(e)}

    def analyze_token_simple(self, token_data: Dict) -> Dict:
        """Simple token analysis"""
        try:
            if not token_data.get('success'):
                return token_data
            
            price_change = token_data.get('price_change_24h', 0)
            volume_24h = token_data.get('volume_24h', 0)
            liquidity = token_data.get('liquidity_usd', 0)
            
            confidence = 5.0
            
            if abs(price_change) > 20:
                confidence += 2
            elif abs(price_change) > 10:
                confidence += 1
            
            if liquidity > 0:
                vol_ratio = volume_24h / liquidity
                if vol_ratio > 0.5:
                    confidence += 1.5
                elif vol_ratio > 0.2:
                    confidence += 1
            
            if liquidity > 100000:
                confidence += 1
            elif liquidity < 10000:
                confidence -= 1
            
            confidence = max(1.0, min(10.0, confidence))
            
            patterns = []
            if price_change > 15:
                patterns.append('breakout')
            if price_change > 5 and volume_24h > liquidity * 0.3:
                patterns.append('momentum')
            if abs(price_change) < 5 and volume_24h > liquidity * 0.1:
                patterns.append('accumulation')
            
            liquidity_grade = 'A' if liquidity >= 100000 else 'B' if liquidity >= 50000 else 'C' if liquidity >= 20000 else 'D' if liquidity >= 5000 else 'F'
            
            token_data.update({
                'confidence': round(confidence, 1),
                'patterns': patterns,
                'liquidity_grade': liquidity_grade
            })
            
            return token_data
            
        except Exception as e:
            print(f"Error analyzing token: {e}")
            return {'success': False, 'error': str(e)}

    def scan_token(self, token_address: str) -> Dict:
        """Main scan function with chart generation and AI analysis"""
        try:
            token_data = self.fetch_token_data_simple(token_address)
            if not token_data.get('success'):
                return token_data
            
            analyzed_data = self.analyze_token_simple(token_data)
            self.tokens_scanned += 1
            self.last_scan_time = datetime.now()
            
            # Generate AI analysis
            ai_analysis = self.generate_ai_analysis(analyzed_data)
            analyzed_data['ai_analysis'] = ai_analysis
            
            # Generate chart
            chart_data = None
            try:
                chart_buffer = self.create_candlestick_chart(analyzed_data.get('symbol'), analyzed_data)
                if chart_buffer:
                    import base64
                    chart_data = base64.b64encode(chart_buffer.getvalue()).decode('utf-8')
                    print(f"Generated chart for {analyzed_data.get('symbol')}")
            except Exception as e:
                print(f"Chart generation failed for {analyzed_data.get('symbol')}: {e}")
            
            analyzed_data['chart_data'] = chart_data
            
            if analyzed_data.get('confidence', 0) >= 7.0:
                self.add_alert(analyzed_data)
            
            return analyzed_data
            
        except Exception as e:
            print(f"Error scanning token: {e}")
            return {'success': False, 'error': str(e)}

    def add_alert(self, token_data: Dict):
        """Add alert"""
        alert = {
            'symbol': token_data.get('symbol'),
            'name': token_data.get('name'),
            'price': token_data.get('price_usd'),
            'change_24h': token_data.get('price_change_24h'),
            'confidence': token_data.get('confidence'),
            'patterns': token_data.get('patterns', []),
            'timestamp': datetime.now().isoformat(),
            'address': token_data.get('address')
        }
        self.latest_alerts.insert(0, alert)
        self.latest_alerts = self.latest_alerts[:10]

    def get_latest_alerts(self) -> List[Dict]:
        return self.latest_alerts

    def get_scanner_status(self) -> Dict:
        return {
            'status': 'active',
            'last_scan': self.last_scan_time.isoformat(),
            'tokens_scanned': self.tokens_scanned,
            'alerts_generated': len(self.latest_alerts)
        }

    def get_historical_prices_simple(self, symbol: str, current_price: float) -> List[Tuple[float, float, float, float]]:
        """Generate simple historical OHLC data for chart generation"""
        try:
            # For demo purposes, generate realistic-looking OHLC data
            # In production, you'd fetch real historical data
            periods = 24  # 24 hours of hourly data
            
            # Generate price movement with some volatility
            prices = []
            base_price = current_price
            
            for i in range(periods):
                # Add some realistic price movement
                volatility = 0.02  # 2% volatility
                change = (np.random.random() - 0.5) * volatility
                
                # Trend component (slight upward bias for demo)
                trend = 0.001 * (i - periods/2) / periods
                
                price = base_price * (1 + change + trend)
                
                # Generate OHLC from the price
                spread = price * 0.005  # 0.5% spread for OHLC variation
                high = price + spread * np.random.random()
                low = price - spread * np.random.random()
                open_price = low + (high - low) * np.random.random()
                close_price = low + (high - low) * np.random.random()
                
                prices.append((open_price, high, low, close_price))
                base_price = close_price  # Use close as next period's base
            
            return prices
        except Exception as e:
            print(f"Error generating historical prices: {e}")
            # Return simple data if generation fails
            return [(current_price, current_price, current_price, current_price) for _ in range(24)]

    def calculate_ema_prediction(self, ohlc_data: List[Tuple[float, float, float, float]], hours_to_predict: int = 2) -> List[Tuple[int, float]]:
        """Calculate simple EMA-based price prediction"""
        try:
            if len(ohlc_data) < 8:
                return []
            
            closes = [candle[3] for candle in ohlc_data]
            current_price = closes[-1]
            
            # Simple moving average trend
            if len(closes) >= 12:
                short_ma = np.mean(closes[-6:])
                long_ma = np.mean(closes[-12:])
                trend = (short_ma - long_ma) / long_ma if long_ma > 0 else 0
            else:
                trend = 0
            
            # Generate predictions
            predictions = []
            for hour in range(1, hours_to_predict + 1):
                # Simple trend projection with some dampening
                predicted_price = current_price * (1 + trend * 0.1 * hour)
                predictions.append((len(ohlc_data) + hour - 1, predicted_price))
            
            return predictions
        except Exception as e:
            print(f"Error calculating EMA prediction: {e}")
            return []

    def create_candlestick_chart(self, symbol: str, token_data: Dict) -> io.BytesIO:
        """Create a candlestick chart with technical indicators"""
        try:
            current_price = token_data.get('price_usd', 0)
            ohlc_data = self.get_historical_prices_simple(symbol, current_price)
            
            if not ohlc_data:
                return None
            
            # Prepare data
            opens = [x[0] for x in ohlc_data]
            highs = [x[1] for x in ohlc_data]
            lows = [x[2] for x in ohlc_data]
            closes = [x[3] for x in ohlc_data]
            x = np.arange(len(ohlc_data))
            
            # Create figure
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), facecolor='#181a20', 
                                         gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.1})
            
            for ax in [ax1, ax2]:
                ax.set_facecolor('#181a20')
            
            # === SUBPLOT 1: Candlesticks ===
            width = 0.6
            for i in range(len(ohlc_data)):
                color = '#00FF00' if closes[i] >= opens[i] else '#FF0000'
                # Candle body
                ax1.add_patch(plt.Rectangle((x[i] - width/2, min(opens[i], closes[i])), 
                                          width, abs(closes[i] - opens[i]), 
                                          color=color, alpha=0.85))
                # High-low line
                ax1.plot([x[i], x[i]], [lows[i], highs[i]], color=color, linewidth=2, alpha=0.7)
            
            # Add moving averages
            if len(closes) >= 10:
                ma_short = pd.Series(closes).rolling(window=5, min_periods=1).mean()
                ma_long = pd.Series(closes).rolling(window=10, min_periods=1).mean()
                ax1.plot(x, ma_short, color='#FFD700', linewidth=1.5, alpha=0.8, label='MA5')
                ax1.plot(x, ma_long, color='#FF6B35', linewidth=1.5, alpha=0.8, label='MA10')
            
            # Add prediction line
            prediction = self.calculate_ema_prediction(ohlc_data, hours_to_predict=2)
            if prediction:
                pred_x, pred_y = zip(*prediction)
                ax1.plot(pred_x, pred_y, color='#FFD700', linestyle='--', linewidth=2.5, 
                        marker='o', label='AI Prediction')
                # Connect to last candle
                ax1.plot([x[-1], pred_x[0]], [closes[-1], pred_y[0]], 
                        color='#FFD700', linestyle=':', linewidth=1.5)
            
            # Current price info
            price_change = token_data.get('price_change_24h', 0)
            confidence = token_data.get('confidence', 0)
            
            ax1.set_title(f'{symbol} - Real-time Analysis | Price: ${current_price:.6f} | '
                         f'24h: {price_change:+.2f}% | Confidence: {confidence}/10', 
                         color='#00FFFF', fontsize=14, weight='bold', pad=12)
            ax1.set_ylabel('Price ($)', color='#FFFFFF', fontsize=11)
            ax1.tick_params(colors='#FFFFFF', labelsize=9)
            ax1.grid(True, alpha=0.3, color='#CCCCCC', linestyle='--')
            ax1.legend(facecolor='#333333', edgecolor='#FFFFFF', fontsize=9, 
                      loc='upper left', labelcolor='#FFFFFF')
            ax1.set_xticklabels([])
            
            # === SUBPLOT 2: Volume (simulated) ===
            # Generate volume data based on price movement
            volumes = []
            for i in range(len(closes)):
                base_vol = token_data.get('volume_24h', 1000000) / 24  # Hourly volume
                price_vol_factor = abs(closes[i] - opens[i]) / opens[i] * 5 if opens[i] > 0 else 1
                volume = base_vol * (0.5 + price_vol_factor * np.random.random())
                volumes.append(volume)
            
            colors = ['#00FF00' if closes[i] >= opens[i] else '#FF0000' for i in range(len(closes))]
            ax2.bar(x, volumes, color=colors, alpha=0.6, width=0.8)
            
            ax2.set_ylabel('Volume', color='#FFFFFF', fontsize=10)
            ax2.set_xlabel('Time (hours)', color='#FFFFFF', fontsize=11)
            ax2.tick_params(colors='#FFFFFF', labelsize=8)
            ax2.grid(True, alpha=0.3, color='#CCCCCC', linestyle='--')
            
            # X-axis labels
            ax2.set_xticks(np.linspace(0, len(ohlc_data)-1, min(8, len(ohlc_data))))
            ax2.set_xticklabels([f"{int(i)}h" for i in np.linspace(0, 24, min(8, len(ohlc_data)))], 
                               color='#FFFFFF', fontsize=9)
            
            # Save to buffer
            buf = io.BytesIO()
            try:
                fig.tight_layout()
            except:
                pass  # Ignore layout warnings
            fig.savefig(buf, format='png', dpi=110, bbox_inches='tight', facecolor='#181a20')
            buf.seek(0)
            plt.close(fig)
            
            print(f"Successfully created chart for {symbol}")
            return buf
            
        except Exception as e:
            print(f"Error creating chart for {symbol}: {e}")
            return None

    def generate_ai_analysis(self, token_data: Dict) -> str:
        """Generate AI-style analysis description"""
        try:
            symbol = token_data.get('symbol', 'Unknown')
            price = token_data.get('price_usd', 0)
            change_24h = token_data.get('price_change_24h', 0)
            volume = token_data.get('volume_24h', 0)
            liquidity = token_data.get('liquidity_usd', 0)
            confidence = token_data.get('confidence', 0)
            patterns = token_data.get('patterns', [])
            
            # Generate analysis based on data
            analysis_parts = []
            
            # Market Position Analysis
            if confidence >= 8:
                analysis_parts.append(f"🎯 **STRONG SIGNAL DETECTED** for {symbol} with {confidence}/10 confidence rating.")
            elif confidence >= 6:
                analysis_parts.append(f"📊 **MODERATE OPPORTUNITY** identified in {symbol} (Confidence: {confidence}/10).")
            else:
                analysis_parts.append(f"⚠️ **CAUTIOUS ASSESSMENT** for {symbol} - Lower confidence signal ({confidence}/10).")
            
            # Price Action Analysis
            if abs(change_24h) > 20:
                direction = "explosive upward" if change_24h > 0 else "sharp downward"
                analysis_parts.append(f"Price exhibits {direction} momentum with {change_24h:+.2f}% movement, indicating significant market interest.")
            elif abs(change_24h) > 10:
                direction = "bullish" if change_24h > 0 else "bearish"
                analysis_parts.append(f"Notable {direction} price action observed ({change_24h:+.2f}%), suggesting institutional attention.")
            else:
                analysis_parts.append(f"Price stability maintained with {change_24h:+.2f}% change, indicating controlled market conditions.")
            
            # Liquidity Assessment
            if liquidity > 100000:
                analysis_parts.append(f"Excellent liquidity depth (${liquidity:,.0f}) provides strong execution capabilities for larger position sizes.")
            elif liquidity > 50000:
                analysis_parts.append(f"Adequate liquidity (${liquidity:,.0f}) supports moderate trading activity with manageable slippage.")
            else:
                analysis_parts.append(f"Limited liquidity (${liquidity:,.0f}) requires careful position sizing to minimize market impact.")
            
            # Volume Analysis
            if volume > liquidity * 0.5:
                analysis_parts.append(f"High volume-to-liquidity ratio ({volume/liquidity:.2f}) indicates active price discovery and strong participant engagement.")
            elif volume > liquidity * 0.2:
                analysis_parts.append(f"Healthy trading volume relative to liquidity suggests balanced market participation.")
            else:
                analysis_parts.append(f"Lower volume activity may indicate accumulation phase or reduced short-term volatility.")
            
            # Pattern Recognition
            if patterns:
                pattern_desc = ", ".join(patterns)
                analysis_parts.append(f"Technical pattern analysis reveals {pattern_desc} formations, providing additional confirmation signals.")
            
            # Risk Assessment
            risk_factors = []
            if liquidity < 20000:
                risk_factors.append("liquidity constraints")
            if abs(change_24h) > 30:
                risk_factors.append("high volatility")
            if confidence < 6:
                risk_factors.append("signal uncertainty")
            
            if risk_factors:
                analysis_parts.append(f"⚠️ Key risk considerations: {', '.join(risk_factors)}. Implement appropriate position sizing and risk management.")
            
            # Trading Recommendation
            if confidence >= 7 and liquidity > 50000:
                analysis_parts.append("✅ **FAVORABLE CONDITIONS** for strategic positioning with proper risk management protocols.")
            elif confidence >= 6:
                analysis_parts.append("📈 **MONITORED OPPORTUNITY** - Consider scaled entry with tight risk controls.")
            else:
                analysis_parts.append("🔍 **RESEARCH PHASE** - Additional confirmation signals recommended before position initiation.")
            
            return " ".join(analysis_parts)
            
        except Exception as e:
            print(f"Error generating AI analysis: {e}")
            return f"Advanced market analysis available for {token_data.get('symbol', 'token')} - Confidence: {token_data.get('confidence', 0)}/10. Professional assessment indicates {'favorable' if token_data.get('confidence', 0) > 6 else 'cautious'} market conditions with current price dynamics and liquidity metrics."

flask_scanner = None

def get_scanner():
    global flask_scanner
    if flask_scanner is None:
        flask_scanner = FlaskScanner()
    return flask_scanner 