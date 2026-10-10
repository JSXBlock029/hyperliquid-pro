import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import json
import logging
import requests
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class MultiSourceDataLoader:
    """Multi-source data loader with fallback mechanisms for crypto and stock data"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
    def get_coinbase_data(self, symbol: str, start_date: str, end_date: str, interval: str = '1m') -> Optional[pd.DataFrame]:
        """Get data from Coinbase Pro API"""
        try:
            logger.info(f"Attempting to get data from Coinbase for {symbol}")
            
            # Convert symbol format (e.g., BTC-USD -> BTC-USD)
            coinbase_symbol = symbol.replace('/', '-')
            
            # Convert interval to Coinbase format
            interval_map = {
                '1m': 60,
                '5m': 300,
                '15m': 900,
                '1h': 3600,
                '1d': 86400
            }
            
            if interval not in interval_map:
                logger.warning(f"Interval {interval} not supported by Coinbase, using 1m")
                interval = '1m'
            
            granularity = interval_map[interval]
            
            # Convert dates to timestamps
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            
            # Coinbase has limits, so we need to fetch in chunks
            chunk_size = timedelta(days=1) if interval == '1m' else timedelta(days=7)
            all_data = []
            
            current_start = start_dt
            while current_start < end_dt:
                current_end = min(current_start + chunk_size, end_dt)
                
                start_ts = int(current_start.timestamp())
                end_ts = int(current_end.timestamp())
                
                url = f"https://api.pro.coinbase.com/products/{coinbase_symbol}/candles"
                params = {
                    'start': start_ts,
                    'end': end_ts,
                    'granularity': granularity
                }
                
                response = self.session.get(url, params=params)
                response.raise_for_status()
                
                data = response.json()
                if data:
                    all_data.extend(data)
                
                current_start = current_end
                time.sleep(0.1)  # Rate limiting
            
            if not all_data:
                logger.warning("No data received from Coinbase")
                return None
            
            # Convert to DataFrame
            df = pd.DataFrame(all_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            df.set_index('timestamp', inplace=True)
            
            # Convert to numeric
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # Sort by timestamp
            df.sort_index(inplace=True)
            
            logger.info(f"Successfully loaded {len(df)} candles from Coinbase")
            return df
            
        except Exception as e:
            logger.warning(f"Coinbase data fetch failed: {e}")
            return None
    
    def get_binance_data(self, symbol: str, start_date: str, end_date: str, interval: str = '1m') -> Optional[pd.DataFrame]:
        """Get data from Binance API"""
        try:
            logger.info(f"Attempting to get data from Binance for {symbol}")
            
            # Convert symbol format (e.g., BTC-USD -> BTCUSDT)
            binance_symbol = symbol.replace('-', '').replace('/', '')
            if not binance_symbol.endswith('USDT'):
                binance_symbol += 'USDT'
            
            # Convert interval to Binance format
            interval_map = {
                '1m': '1m',
                '5m': '5m',
                '15m': '15m',
                '1h': '1h',
                '1d': '1d'
            }
            
            if interval not in interval_map:
                logger.warning(f"Interval {interval} not supported by Binance, using 1m")
                interval = '1m'
            
            binance_interval = interval_map[interval]
            
            # Convert dates to timestamps
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            
            start_ts = int(start_dt.timestamp() * 1000)
            end_ts = int(end_dt.timestamp() * 1000)
            
            url = "https://api.binance.com/api/v3/klines"
            params = {
                'symbol': binance_symbol,
                'interval': binance_interval,
                'startTime': start_ts,
                'endTime': end_ts,
                'limit': 1000
            }
            
            response = self.session.get(url, params=params)
            response.raise_for_status()
            
            data = response.json()
            if not data:
                logger.warning("No data received from Binance")
                return None
            
            # Convert to DataFrame
            df = pd.DataFrame(data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # Convert to numeric
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # Sort by timestamp
            df.sort_index(inplace=True)
            
            logger.info(f"Successfully loaded {len(df)} candles from Binance")
            return df
            
        except Exception as e:
            logger.warning(f"Binance data fetch failed: {e}")
            return None
    
    def get_alpha_vantage_data(self, symbol: str, start_date: str, end_date: str, interval: str = '1d') -> Optional[pd.DataFrame]:
        """Get data from Alpha Vantage API (free tier available)"""
        try:
            logger.info(f"Attempting to get data from Alpha Vantage for {symbol}")
            
            # Alpha Vantage free tier only supports daily data
            if interval != '1d':
                logger.warning(f"Alpha Vantage only supports daily data, requested {interval}")
                return None
            
            # You would need an API key for Alpha Vantage
            # For now, we'll just log that it's available
            logger.info("Alpha Vantage requires API key - skipping for now")
            return None
            
        except Exception as e:
            logger.warning(f"Alpha Vantage data fetch failed: {e}")
            return None
    
    def get_data_with_fallback(self, symbol: str, start_date: str, end_date: str, interval: str = '1d') -> Optional[pd.DataFrame]:
        """Get data with fallback between multiple sources"""
        logger.info(f"=== Multi-source data loading for {symbol} ===")
        logger.info(f"Interval: {interval}, Start: {start_date}, End: {end_date}")
        
        # Try different data sources in order of preference
        data_sources = [
            ('Coinbase', lambda: self.get_coinbase_data(symbol, start_date, end_date, interval)),
            ('Binance', lambda: self.get_binance_data(symbol, start_date, end_date, interval)),
            ('Alpha Vantage', lambda: self.get_alpha_vantage_data(symbol, start_date, end_date, interval))
        ]
        
        for source_name, source_func in data_sources:
            try:
                logger.info(f"Trying {source_name}...")
                data = source_func()
                if data is not None and not data.empty:
                    logger.info(f"Successfully loaded data from {source_name}")
                    return data
                else:
                    logger.warning(f"{source_name} returned empty data")
            except Exception as e:
                logger.warning(f"{source_name} failed: {e}")
                continue
        
        logger.error("All data sources failed")
        return None


class WebBacktester:
    """Simplified backtester optimized for web integration with multiple timeframe support"""
    
    # Supported timeframes with their descriptions
    SUPPORTED_INTERVALS = {
        '1m': {'name': '1 Minute', 'min_period_days': 1, 'max_period_days': 7, 'yf_interval': '1m'},
        '5m': {'name': '5 Minutes', 'min_period_days': 1, 'max_period_days': 30, 'yf_interval': '5m'},
        '15m': {'name': '15 Minutes', 'min_period_days': 1, 'max_period_days': 60, 'yf_interval': '15m'},
        '30m': {'name': '30 Minutes', 'min_period_days': 1, 'max_period_days': 90, 'yf_interval': '30m'},
        '1h': {'name': '1 Hour', 'min_period_days': 1, 'max_period_days': 180, 'yf_interval': '1h'},
        '1d': {'name': '1 Day', 'min_period_days': 1, 'max_period_days': 3650, 'yf_interval': '1d'},
        '1wk': {'name': '1 Week', 'min_period_days': 7, 'max_period_days': 3650, 'yf_interval': '1wk'},
        '1mo': {'name': '1 Month', 'min_period_days': 30, 'max_period_days': 3650, 'yf_interval': '1mo'}
    }
    
    def __init__(self):
        self.data = None
        self.results = {}
        self.trades = []
        self.current_interval = '1d'
        self.interval_info = None
        self.data_loader = MultiSourceDataLoader()
        self.data_source = None  # Track which source provided the data
        
    def get_supported_intervals(self):
        """Get list of supported intervals with their descriptions"""
        return {
            interval: {
                'name': info['name'],
                'min_period_days': info['min_period_days'],
                'max_period_days': info['max_period_days']
            }
            for interval, info in self.SUPPORTED_INTERVALS.items()
        }
    
    def validate_interval_and_period(self, interval, start_date, end_date):
        """Validate that the interval and date range are compatible"""
        try:
            logger.info(f"=== Validating interval and period ===")
            logger.info(f"Interval: {interval}, Start: {start_date}, End: {end_date}")
            
            if interval not in self.SUPPORTED_INTERVALS:
                logger.error(f"Unsupported interval '{interval}'")
                logger.error(f"Supported intervals: {list(self.SUPPORTED_INTERVALS.keys())}")
                return False, f"Unsupported interval '{interval}'. Supported intervals: {list(self.SUPPORTED_INTERVALS.keys())}"
            
            # Special validation for 1-minute intervals
            if interval == '1m':
                logger.info("Validating 1-minute interval requirements...")
                try:
                    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
                    period_days = (end_dt - start_dt).days
                    
                    if period_days > 7:
                        logger.warning(f"1-minute interval requested for {period_days} days - this may cause issues with yfinance")
                        logger.warning("Consider using 5m or 15m intervals for longer periods")
                    
                    # Check if we're within market hours (rough estimate)
                    current_time = datetime.now()
                    if current_time.hour < 9 or current_time.hour > 16:
                        logger.warning("1-minute data may be limited outside regular market hours")
                        
                except Exception as e:
                    logger.error(f"Error validating 1-minute interval: {e}")
            
            # Calculate period length in days
            try:
                start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                end_dt = datetime.strptime(end_date, '%Y-%m-%d')
                period_days = (end_dt - start_dt).days
                logger.info(f"Period length: {period_days} days")
            except ValueError as e:
                logger.error(f"Invalid date format: {e}")
                return False, f"Invalid date format. Use YYYY-MM-DD format. Error: {e}"
            
            if period_days <= 0:
                logger.error("End date must be after start date")
                return False, "End date must be after start date"
            
            interval_info = self.SUPPORTED_INTERVALS[interval]
            min_days = interval_info['min_period_days']
            max_days = interval_info['max_period_days']
            
            logger.info(f"Interval requirements: min={min_days} days, max={max_days} days")
            
            if period_days < min_days:
                logger.error(f"Period too short for {interval} interval")
                logger.error(f"Minimum: {min_days} days, provided: {period_days} days")
                return False, f"Period too short for {interval} interval. Minimum: {min_days} days, provided: {period_days} days"
            
            if period_days > max_days:
                logger.error(f"Period too long for {interval} interval")
                logger.error(f"Maximum: {max_days} days, provided: {period_days} days")
                return False, f"Period too long for {interval} interval. Maximum: {max_days} days, provided: {period_days} days"
            
            logger.info(f"=== Interval and period validation successful ===")
            return True, "Valid"
            
        except Exception as e:
            logger.error(f"=== Interval validation FAILED ===")
            logger.error(f"Error during validation: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return False, f"Validation error: {e}"
    
    def get_recommended_interval(self, start_date, end_date):
        """Recommend the best interval based on the date range"""
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            period_days = (end_dt - start_dt).days
        except ValueError:
            return '1d'  # Default fallback
        
        # Recommend based on period length
        if period_days <= 7:
            return '5m'
        elif period_days <= 30:
            return '15m'
        elif period_days <= 90:
            return '1h'
        elif period_days <= 365:
            return '1d'
        elif period_days <= 1095:
            return '1wk'
        else:
            return '1mo'
    
    def load_data(self, symbol, start_date, end_date, interval='1d'):
        """Load market data using multi-source data loader with fallback mechanisms"""
        try:
            logger.info(f"=== Starting load_data ===")
            logger.info(f"Parameters: symbol={symbol}, start_date={start_date}, end_date={end_date}, interval={interval}")
            
            # Validate interval and period
            is_valid, message = self.validate_interval_and_period(interval, start_date, end_date)
            if not is_valid:
                logger.error(f"Interval validation failed: {message}")
                raise ValueError(message)
            
            # Store current interval and info
            self.current_interval = interval
            self.interval_info = self.SUPPORTED_INTERVALS[interval]
            
            logger.info(f"Loading data for {symbol} from {start_date} to {end_date} with {interval} ({self.interval_info['name']}) interval")
            
            # Try multi-source data loading first
            logger.info(f"Attempting multi-source data loading...")
            try:
                self.data = self.data_loader.get_data_with_fallback(symbol, start_date, end_date, interval)
                if self.data is not None and not self.data.empty:
                    logger.info(f"Multi-source data loading successful! Loaded {len(self.data)} candles")
                    self.data_source = "multi_source"
                else:
                    raise ValueError("Multi-source data loading returned empty data")
                    
            except Exception as multi_source_error:
                logger.warning(f"Multi-source data loading failed: {multi_source_error}")
                logger.info("Falling back to yfinance...")
                
                # Fallback to yfinance
                try:
                    yf_interval = self.interval_info['yf_interval']
                    logger.info(f"Using yfinance with interval: {yf_interval}")
                    
                    self.data = yf.download(
                        symbol, 
                        start=start_date, 
                        end=end_date, 
                        interval=yf_interval, 
                        progress=False,
                        auto_adjust=True,
                        prepost=True,
                        threads=True
                    )
                    
                    if not self.data.empty:
                        logger.info(f"YFinance fallback successful! Loaded {len(self.data)} candles")
                        self.data_source = "yfinance"
                    else:
                        raise ValueError(f"No data found for symbol {symbol}. Please check symbol format or try a different date range.")
                        
                except Exception as yf_error:
                    logger.error(f"YFinance fallback also failed: {yf_error}")
                    
                    # For 1-minute intervals, try to fallback to 5-minute
                    if interval == '1m':
                        logger.info("Attempting fallback to 5-minute interval...")
                        try:
                            fallback_interval = '5m'
                            logger.info(f"Trying fallback interval: {fallback_interval}")
                            
                            self.data = yf.download(
                                symbol, 
                                start=start_date, 
                                end=end_date, 
                                interval=fallback_interval, 
                                progress=False,
                                auto_adjust=True,
                                prepost=True,
                                threads=True
                            )
                            
                            if not self.data.empty:
                                logger.info(f"5-minute fallback successful! Loaded {len(self.data)} candles")
                                logger.warning(f"Note: Using 5-minute data instead of requested 1-minute data")
                                self.current_interval = fallback_interval
                                self.interval_info = self.SUPPORTED_INTERVALS[fallback_interval]
                                self.data_source = "yfinance_5m"
                            else:
                                raise ValueError("5-minute fallback also failed")
                                
                        except Exception as fallback_error:
                            logger.error(f"5-minute fallback also failed: {fallback_error}")
                            raise yf_error  # Re-raise the yfinance error
                    else:
                        raise yf_error  # Re-raise the yfinance error for non-1m intervals
            
            # Log data details
            logger.info(f"Download completed. Data shape: {self.data.shape}")
            logger.info(f"Data columns: {list(self.data.columns)}")
            logger.info(f"Data index type: {type(self.data.index)}")
            logger.info(f"First few rows: {self.data.head()}")
            logger.info(f"Data source: {self.data_source}")
            logger.info(f"Loaded {len(self.data)} {self.interval_info['name']} candles")
            
            # Determine minimum required data points based on interval
            min_required_points = self._get_min_required_points(interval)
            logger.info(f"Minimum required points for {interval}: {min_required_points}")
            
            if len(self.data) < min_required_points:
                logger.warning(f"Only {len(self.data)} data points available for {interval} interval. "
                             f"Minimum recommended: {min_required_points}. Consider extending the date range or using a longer interval.")
            
            # Check data quality for 1-minute intervals
            if interval == '1m' or self.current_interval == '1m':
                logger.info("Checking data quality for 1-minute interval...")
                
                # Check for gaps in data (missing timestamps)
                expected_minutes = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).total_seconds() / 60
                actual_points = len(self.data)
                
                if actual_points < expected_minutes * 0.8:  # Allow 20% missing data
                    logger.warning(f"Data appears to have gaps. Expected ~{expected_minutes:.0f} minutes, got {actual_points} points")
                    logger.warning("This may indicate market closure, data limitations, or yfinance issues")
                
                # Check for duplicate timestamps
                duplicate_timestamps = self.data.index.duplicated().sum()
                if duplicate_timestamps > 0:
                    logger.warning(f"Found {duplicate_timestamps} duplicate timestamps - this may cause issues")
                
                # Check for very small price changes (indicating stale data)
                if 'Close' in self.data.columns:
                    price_changes = self.data['Close'].pct_change().abs()
                    zero_changes = (price_changes == 0).sum()
                    if zero_changes > len(self.data) * 0.1:  # More than 10% zero changes
                        logger.warning(f"Found {zero_changes} price bars with zero change - data may be stale")
                
                logger.info("Data quality check completed")
            
            # Add interval metadata to results
            self.results['interval_info'] = {
                'interval': interval,
                'interval_name': self.interval_info['name'],
                'data_points': len(self.data),
                'period_start': start_date,
                'period_end': end_date,
                'recommended_min_points': min_required_points
            }
            
            logger.info(f"=== load_data completed successfully ===")
            return True
            
        except Exception as e:
            logger.error(f"=== load_data FAILED ===")
            logger.error(f"Error loading data: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            logger.error(f"Data state: {getattr(self, 'data', 'No data attribute')}")
            logger.error(f"Results state: {getattr(self, 'results', 'No results attribute')}")
            return False
    
    def _get_min_required_points(self, interval):
        """Get minimum required data points based on interval for proper indicator calculation"""
        # Base requirements for technical indicators (longest lookback period needed)
        base_min = 50  # For most indicators like SMA_50
        
        # Adjust based on interval - shorter intervals need more points for same time coverage
        if interval in ['1m', '2m']:
            return base_min * 12  # Need more points for very short intervals
        elif interval in ['5m', '15m']:
            return base_min * 6
        elif interval in ['30m', '60m', '1h']:
            return base_min * 3
        elif interval in ['90m']:
            return base_min * 2
        else:  # Daily and longer intervals
            return base_min
    
    def _standardize_columns(self):
        """Standardize column names from different data sources"""
        try:
            logger.info("Standardizing column names...")
            
            # Create a mapping of possible column names to standard names
            column_mapping = {}
            
            for col in self.data.columns:
                col_lower = str(col).lower()
                
                if 'open' in col_lower or 'o' == col_lower:
                    column_mapping[col] = 'Open'
                elif 'high' in col_lower or 'h' == col_lower:
                    column_mapping[col] = 'High'
                elif 'low' in col_lower or 'l' == col_lower:
                    column_mapping[col] = 'Low'
                elif 'close' in col_lower or 'c' == col_lower:
                    column_mapping[col] = 'Close'
                elif 'volume' in col_lower or 'vol' in col_lower:
                    column_mapping[col] = 'Volume'
                else:
                    # Keep other columns as is
                    column_mapping[col] = col
            
            # Rename columns
            self.data = self.data.rename(columns=column_mapping)
            logger.info(f"Standardized columns: {list(self.data.columns)}")
            
        except Exception as e:
            logger.error(f"Error standardizing columns: {e}")
    
    def _check_data_quality(self, start_date, end_date, interval):
        """Check data quality for various intervals"""
        try:
            logger.info("Checking data quality...")
            
            # Check for gaps in data (missing timestamps)
            start_dt = pd.to_datetime(start_date)
            end_dt = pd.to_datetime(end_date)
            
            if interval == '1m':
                expected_points = int((end_dt - start_dt).total_seconds() / 60)
            elif interval == '5m':
                expected_points = int((end_dt - start_dt).total_seconds() / 300)
            elif interval == '15m':
                expected_points = int((end_dt - start_dt).total_seconds() / 900)
            elif interval == '1h':
                expected_points = int((end_dt - start_dt).total_seconds() / 3600)
            else:
                expected_points = len(self.data)
            
            actual_points = len(self.data)
            
            if actual_points < expected_points * 0.8:  # Allow 20% missing data
                logger.warning(f"Data appears to have gaps. Expected ~{expected_points} points, got {actual_points}")
                logger.warning("This may indicate market closure, data limitations, or API issues")
            
            # Check for duplicate timestamps
            duplicate_timestamps = self.data.index.duplicated().sum()
            if duplicate_timestamps > 0:
                logger.warning(f"Found {duplicate_timestamps} duplicate timestamps - this may cause issues")
                # Remove duplicates
                self.data = self.data[~self.data.index.duplicated(keep='first')]
                logger.info(f"Removed duplicate timestamps, new shape: {self.data.shape}")
            
            # Check for very small price changes (indicating stale data)
            if 'Close' in self.data.columns:
                price_changes = self.data['Close'].pct_change().abs()
                zero_changes = (price_changes == 0).sum()
                if zero_changes > len(self.data) * 0.1:  # More than 10% zero changes
                    logger.warning(f"Found {zero_changes} price bars with zero change - data may be stale")
            
            logger.info("Data quality check completed")
            
        except Exception as e:
            logger.error(f"Error checking data quality: {e}")
    
    def calculate_indicators(self):
        """Calculate all technical indicators needed for strategies, adjusted for timeframe"""
        try:
            logger.info(f"=== Starting calculate_indicators ===")
            logger.info(f"Current interval: {self.current_interval}")
            logger.info(f"Data available: {self.data is not None}")
            
            if self.data is None:
                logger.error("No data available for indicator calculation")
                return False
            
            logger.info(f"Data shape: {self.data.shape}")
            logger.info(f"Data columns: {list(self.data.columns)}")
            logger.info(f"Data index: {self.data.index[:5]} to {self.data.index[-5:]}")
            
            # Check for required columns
            required_columns = ['Close', 'High', 'Low', 'Volume']
            missing_columns = [col for col in required_columns if col not in self.data.columns]
            if missing_columns:
                logger.error(f"Missing required columns: {missing_columns}")
                logger.error(f"Available columns: {list(self.data.columns)}")
                return False
            
            # Extract price data with error checking
            try:
                close = self.data['Close'].squeeze()
                high = self.data['High'].squeeze()
                low = self.data['Low'].squeeze()
                volume = self.data['Volume'].squeeze()
                
                logger.info(f"Price data extracted successfully")
                logger.info(f"Close data shape: {close.shape}, type: {type(close)}")
                logger.info(f"Close data sample: {close.head()}")
                logger.info(f"Close data has NaN: {close.isna().sum()}")
                
            except Exception as e:
                logger.error(f"Error extracting price data: {e}")
                logger.error(f"Data types: Close={type(self.data['Close'])}, High={type(self.data['High'])}, Low={type(self.data['Low'])}, Volume={type(self.data['Volume'])}")
                return False
            
            # Adjust indicator periods based on timeframe
            try:
                rsi_period, sma_short, sma_long, bb_period, ema_fast, ema_slow = self._get_adjusted_periods()
                logger.info(f"Adjusted periods: RSI:{rsi_period}, SMA:{sma_short}/{sma_long}, BB:{bb_period}, EMA:{ema_fast}/{ema_slow}")
            except Exception as e:
                logger.error(f"Error getting adjusted periods: {e}")
                return False
            
            # Calculate indicators with error checking
            try:
                # RSI with adjusted period
                logger.info("Calculating RSI...")
                delta = close.diff()
                gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
                loss = -delta.where(delta < 0, 0).rolling(window=rsi_period).mean()
                rs = gain / loss
                self.data['RSI'] = 100 - (100 / (1 + rs))
                logger.info(f"RSI calculated successfully, shape: {self.data['RSI'].shape}")
                
                # Moving Averages with adjusted periods
                logger.info("Calculating moving averages...")
                self.data['SMA_20'] = close.rolling(window=sma_short).mean()
                self.data['SMA_50'] = close.rolling(window=sma_long).mean()
                self.data['EMA_12'] = close.ewm(span=ema_fast).mean()
                self.data['EMA_26'] = close.ewm(span=ema_slow).mean()
                
                # BTC Scalping specific SMAs (fixed periods for scalping)
                self.data['SMA_5'] = close.rolling(window=5).mean()
                self.data['SMA_13'] = close.rolling(window=13).mean()
                logger.info("Moving averages calculated successfully")
                
                # Bollinger Bands with adjusted period
                logger.info("Calculating Bollinger Bands...")
                self.data['BB_middle'] = close.rolling(window=bb_period).mean()
                bb_std = close.rolling(window=bb_period).std()
                self.data['BB_upper'] = self.data['BB_middle'] + (bb_std * 2)
                self.data['BB_lower'] = self.data['BB_middle'] - (bb_std * 2)
                
                # Additional Bollinger Bands for BTC Scalping (10-period, 1.5 multiplier)
                bb_scalping_middle = close.rolling(window=10).mean()
                bb_scalping_std = close.rolling(window=10).std()
                self.data['BB_upper_scalping'] = bb_scalping_middle + (bb_scalping_std * 1.5)
                self.data['BB_lower_scalping'] = bb_scalping_middle - (bb_scalping_std * 1.5)
                logger.info("Bollinger Bands calculated successfully")
                
                # MACD
                logger.info("Calculating MACD...")
                self.data['MACD'] = self.data['EMA_12'] - self.data['EMA_26']
                macd_signal_period = max(6, ema_fast // 2)  # Adjust MACD signal period
                self.data['MACD_signal'] = self.data['MACD'].ewm(span=macd_signal_period).mean()
                self.data['MACD_hist'] = self.data['MACD'] - self.data['MACD_signal']
                logger.info("MACD calculated successfully")
                
                # Stochastic Oscillator (for BTC Scalping)
                logger.info("Calculating Stochastic...")
                stoch_period = 7
                lowest_low = low.rolling(window=stoch_period).min()
                highest_high = high.rolling(window=stoch_period).max()
                self.data['Stoch_K'] = 100 * ((close - lowest_low) / (highest_high - lowest_low))
                self.data['Stoch_D'] = self.data['Stoch_K'].rolling(window=3).mean()
                logger.info("Stochastic calculated successfully")
                
                # ATR (Average True Range) for volatility (for BTC Scalping)
                logger.info("Calculating ATR...")
                tr1 = high - low
                tr2 = abs(high - close.shift(1))
                tr3 = abs(low - close.shift(1))
                true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                self.data['ATR'] = true_range.rolling(window=5).mean()
                logger.info("ATR calculated successfully")
                
                # Volume indicators
                logger.info("Calculating volume indicators...")
                vol_period = max(10, sma_short // 2)  # Adjust volume MA period
                self.data['Volume_MA'] = volume.rolling(window=vol_period).mean()
                
                # Price change for momentum (adjusted for timeframe)
                momentum_period = max(5, sma_short // 4)
                self.data['Price_Change_10'] = close.pct_change(momentum_period)
                logger.info("Volume indicators calculated successfully")
                
            except Exception as e:
                logger.error(f"Error during indicator calculation: {e}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                return False
            
            # Store indicator parameters in results
            try:
                self.results['indicator_params'] = {
                    'rsi_period': rsi_period,
                    'sma_short': sma_short,
                    'sma_long': sma_long,
                    'bb_period': bb_period,
                    'ema_fast': ema_fast,
                    'ema_slow': ema_slow,
                    'macd_signal_period': macd_signal_period,
                    'volume_ma_period': vol_period,
                    'momentum_period': momentum_period
                }
                logger.info("Indicator parameters stored successfully")
            except Exception as e:
                logger.error(f"Error storing indicator parameters: {e}")
                return False
            
            # Final verification
            logger.info(f"Final data shape: {self.data.shape}")
            logger.info(f"Final columns: {list(self.data.columns)}")
            logger.info(f"=== calculate_indicators completed successfully ===")
            return True
            
        except Exception as e:
            logger.error(f"=== calculate_indicators FAILED ===")
            logger.error(f"Error calculating indicators: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            logger.error(f"Data state: {getattr(self, 'data', 'No data attribute')}")
            logger.error(f"Results state: {getattr(self, 'results', 'No results attribute')}")
            return False
    
    def _get_adjusted_periods(self):
        """Get indicator periods adjusted for the current timeframe"""
        interval = self.current_interval
        
        # Base periods for daily timeframe
        base_rsi = 14
        base_sma_short = 20
        base_sma_long = 50
        base_bb = 20
        base_ema_fast = 12
        base_ema_slow = 26
        
        # Adjustment factors based on timeframe
        if interval in ['1m', '2m']:
            # For very short intervals, use much shorter periods
            factor = 0.25
        elif interval in ['5m']:
            factor = 0.4
        elif interval in ['15m']:
            factor = 0.6
        elif interval in ['30m']:
            factor = 0.7
        elif interval in ['60m', '1h']:
            factor = 0.8
        elif interval in ['90m']:
            factor = 0.9
        elif interval in ['1d']:
            factor = 1.0  # Base periods
        elif interval in ['5d']:
            factor = 1.2
        elif interval in ['1wk']:
            factor = 1.5
        elif interval in ['1mo', '3mo']:
            factor = 2.0
        else:
            factor = 1.0
        
        # Apply adjustments and ensure minimum values
        rsi_period = max(5, int(base_rsi * factor))
        sma_short = max(5, int(base_sma_short * factor))
        sma_long = max(10, int(base_sma_long * factor))
        bb_period = max(5, int(base_bb * factor))
        ema_fast = max(3, int(base_ema_fast * factor))
        ema_slow = max(6, int(base_ema_slow * factor))
        
        # Ensure EMA fast is less than EMA slow
        if ema_fast >= ema_slow:
            ema_slow = ema_fast + max(3, int(ema_fast * 0.5))
        
        # Ensure SMA short is less than SMA long
        if sma_short >= sma_long:
            sma_long = sma_short + max(5, int(sma_short * 0.5))
        
        return rsi_period, sma_short, sma_long, bb_period, ema_fast, ema_slow
    
    def run_strategy(self, strategy_name):
        """Run the specified strategy"""
        try:
            logger.info(f"=== Starting run_strategy for: {strategy_name} ===")
            logger.info(f"Strategy name: {strategy_name}")
            logger.info(f"Data available: {self.data is not None}")
            
            if self.data is None:
                logger.error("No data available for strategy execution")
                return None
            
            logger.info(f"Data shape: {self.data.shape}")
            logger.info(f"Data columns: {list(self.data.columns)}")
            logger.info(f"Current interval: {self.current_interval}")
            
            # Clear ALL previous results to ensure clean state
            logger.info("Clearing previous results...")
            self.results.clear()
            self.trades.clear()
            
            logger.info(f"Results cleared. Current results keys: {list(self.results.keys())}")
            
            # Remove any previous strategy columns from data
            logger.info("Removing previous strategy columns...")
            strategy_columns = ['returns', 'strategy_returns', 'cumulative_returns', 'positions']
            for col in strategy_columns:
                if col in self.data.columns:
                    self.data.drop(columns=[col], inplace=True)
                    logger.info(f"Removed column: {col}")
            
            strategy_map = {
                "Simple MA Cross": self.run_simple_ma_strategy,
                "RSI Mean Reversion": self.run_rsi_strategy,
                "Bollinger Bands": self.run_bollinger_strategy,
                "Crypto Momentum": self.run_momentum_strategy,
                "MACD Strategy": self.run_macd_strategy,
                "BTC Futures Scalping": self.run_btc_scalping_strategy
            }
            
            logger.info(f"Available strategies: {list(strategy_map.keys())}")
            
            if strategy_name in strategy_map:
                logger.info(f"Strategy '{strategy_name}' found in strategy map")
                logger.info(f"Running strategy: {strategy_name}")
                
                try:
                    result = strategy_map[strategy_name](strategy_name)
                    logger.info(f"Strategy execution completed successfully")
                    logger.info(f"Results after strategy run: {list(self.results.keys())}")
                    
                    if result is None:
                        logger.warning(f"Strategy '{strategy_name}' returned None result")
                    else:
                        logger.info(f"Strategy result keys: {list(result.keys()) if isinstance(result, dict) else 'Not a dict'}")
                    
                    return result
                    
                except Exception as e:
                    logger.error(f"Error executing strategy '{strategy_name}': {e}")
                    import traceback
                    logger.error(f"Strategy execution traceback: {traceback.format_exc()}")
                    return None
            else:
                logger.error(f"Unknown strategy: {strategy_name}")
                logger.error(f"Available strategies: {list(strategy_map.keys())}")
                return None
                
        except Exception as e:
            logger.error(f"=== run_strategy FAILED ===")
            logger.error(f"Error in run_strategy: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            logger.error(f"Data state: {getattr(self, 'data', 'No data attribute')}")
            logger.error(f"Results state: {getattr(self, 'results', 'No results attribute')}")
            return None
    
    def run_simple_ma_strategy(self, strategy_name="Simple MA Cross"):
        """Run simple moving average crossover strategy"""
        try:
            signals = pd.DataFrame(index=self.data.index)
            signals['signal'] = 0
            
            # Find the correct column names for MultiIndex
            close_col = self._find_column('Close')
            sma_20_col = self._find_column('SMA_20')
            sma_50_col = self._find_column('SMA_50')
            
            if not all([close_col, sma_20_col, sma_50_col]):
                logger.error("Required Simple MA strategy columns not found")
                return None
            
            # Simple moving average crossover signals
            for i in range(50, len(self.data)):
                sma_20 = self.data[sma_20_col].iloc[i]
                sma_50 = self.data[sma_50_col].iloc[i]
                sma_20_prev = self.data[sma_20_col].iloc[i-1]
                sma_50_prev = self.data[sma_50_col].iloc[i-1]
                
                if pd.notna(sma_20) and pd.notna(sma_50):
                    # Buy when fast MA crosses above slow MA
                    if sma_20_prev <= sma_50_prev and sma_20 > sma_50:
                        signals['signal'].iloc[i] = 1
                    # Sell when fast MA crosses below slow MA
                    elif sma_20_prev >= sma_50_prev and sma_20 < sma_50:
                        signals['signal'].iloc[i] = -1
            
            return self._calculate_strategy_results(signals, strategy_name)
            
        except Exception as e:
            logger.error(f"Error running Simple MA strategy: {e}")
            return None
    
    def run_rsi_strategy(self, strategy_name="RSI Mean Reversion"):
        """Run RSI mean reversion strategy"""
        try:
            signals = pd.DataFrame(index=self.data.index)
            signals['signal'] = 0
            signals['position'] = 0
            
            # Find the correct column names for MultiIndex
            rsi_col = self._find_column('RSI')
            
            if not rsi_col:
                logger.error("Required RSI column not found")
                return None
            
            # RSI mean reversion signals
            for i in range(14, len(self.data)):
                rsi = self.data[rsi_col].iloc[i]
                
                if pd.notna(rsi):
                    # Buy when RSI is oversold (< 30)
                    if rsi < 30:
                        signals['signal'].iloc[i] = 1
                    # Sell when RSI is overbought (> 70)  
                    elif rsi > 70:
                        signals['signal'].iloc[i] = -1
                    # Exit when RSI returns to neutral (40-60)
                    elif 40 <= rsi <= 60:
                        signals['signal'].iloc[i] = 0
            
            return self._calculate_strategy_results(signals, strategy_name)
            
        except Exception as e:
            logger.error(f"Error running RSI strategy: {e}")
            return None
    
    def run_bollinger_strategy(self, strategy_name="Bollinger Bands"):
        """Run Bollinger Bands strategy"""
        try:
            signals = pd.DataFrame(index=self.data.index)
            signals['signal'] = 0
            signals['position'] = 0
            
            # Find the correct column names for MultiIndex
            close_col = self._find_column('Close')
            bb_upper_col = self._find_column('BB_upper')
            bb_lower_col = self._find_column('BB_lower')
            bb_middle_col = self._find_column('BB_middle')
            
            if not all([close_col, bb_upper_col, bb_lower_col, bb_middle_col]):
                logger.error("Required Bollinger Bands columns not found")
                return None
            
            # Bollinger Bands signals
            for i in range(20, len(self.data)):
                close = self.data[close_col].iloc[i]
                bb_upper = self.data[bb_upper_col].iloc[i]
                bb_lower = self.data[bb_lower_col].iloc[i]
                bb_middle = self.data[bb_middle_col].iloc[i]
                
                if pd.notna(bb_upper) and pd.notna(bb_lower) and pd.notna(bb_middle):
                    # Buy when price touches lower band
                    if close <= bb_lower:
                        signals['signal'].iloc[i] = 1
                    # Sell when price touches upper band
                    elif close >= bb_upper:
                        signals['signal'].iloc[i] = -1
                    # Exit when price returns to middle band
                    elif abs(close - bb_middle) < abs(bb_upper - bb_middle) * 0.1:
                        signals['signal'].iloc[i] = 0
            
            return self._calculate_strategy_results(signals, strategy_name)
            
        except Exception as e:
            logger.error(f"Error running Bollinger strategy: {e}")
            return None
    
    def run_momentum_strategy(self, strategy_name="Crypto Momentum"):
        """Run momentum strategy with volume confirmation"""
        try:
            signals = pd.DataFrame(index=self.data.index)
            signals['signal'] = 0
            signals['position'] = 0
            
            # Find the correct column names for MultiIndex
            price_change_col = self._find_column('Price_Change_10')
            volume_col = self._find_column('Volume')
            volume_ma_col = self._find_column('Volume_MA')
            rsi_col = self._find_column('RSI')
            
            if not all([price_change_col, volume_col, volume_ma_col, rsi_col]):
                logger.error("Required Momentum strategy columns not found")
                return None
            
            # Momentum signals
            for i in range(20, len(self.data)):
                price_change = self.data[price_change_col].iloc[i]
                volume = self.data[volume_col].iloc[i]
                volume_ma = self.data[volume_ma_col].iloc[i]
                rsi = self.data[rsi_col].iloc[i]
                
                if (pd.notna(price_change) and pd.notna(volume) and 
                    pd.notna(volume_ma) and pd.notna(rsi)):
                    
                    # High volume confirmation
                    high_volume = volume > volume_ma * 1.2
                    
                    # Strong upward momentum with volume
                    if price_change > 0.05 and high_volume and rsi < 70:
                        signals['signal'].iloc[i] = 1
                    # Strong downward momentum with volume
                    elif price_change < -0.05 and high_volume and rsi > 30:
                        signals['signal'].iloc[i] = -1
                    # Exit on momentum reversal
                    elif abs(price_change) < 0.01:
                        signals['signal'].iloc[i] = 0
            
            return self._calculate_strategy_results(signals, strategy_name)
            
        except Exception as e:
            logger.error(f"Error running Momentum strategy: {e}")
            return None
    
    def run_macd_strategy(self, strategy_name="MACD Strategy"):
        """Run MACD crossover strategy"""
        try:
            signals = pd.DataFrame(index=self.data.index)
            signals['signal'] = 0
            signals['position'] = 0
            
            # Find the correct column names for MultiIndex
            macd_col = self._find_column('MACD')
            macd_signal_col = self._find_column('MACD_signal')
            
            if not all([macd_col, macd_signal_col]):
                logger.error("Required MACD strategy columns not found")
                return None
            
            # MACD signals
            for i in range(26, len(self.data)):
                macd = self.data[macd_col].iloc[i]
                macd_signal = self.data[macd_signal_col].iloc[i] 
                macd_prev = self.data[macd_col].iloc[i-1]
                signal_prev = self.data[macd_signal_col].iloc[i-1]
                
                if (pd.notna(macd) and pd.notna(macd_signal) and 
                    pd.notna(macd_prev) and pd.notna(signal_prev)):
                    
                    # MACD bullish crossover (MACD crosses above signal)
                    if macd_prev <= signal_prev and macd > macd_signal:
                        signals['signal'].iloc[i] = 1
                    # MACD bearish crossover (MACD crosses below signal)
                    elif macd_prev >= signal_prev and macd < macd_signal:
                        signals['signal'].iloc[i] = -1
            
            return self._calculate_strategy_results(signals, strategy_name)
            
        except Exception as e:
            logger.error(f"Error running MACD strategy: {e}")
            return None
    
    def run_btc_scalping_strategy(self, strategy_name="BTC Futures Scalping"):
        """Run BTC Futures Scalping strategy with multiple confirmations"""
        try:
            logger.info(f"=== Starting BTC Scalping Strategy ===")
            logger.info(f"Strategy name: {strategy_name}")
            logger.info(f"Data shape: {self.data.shape}")
            logger.info(f"Data columns: {list(self.data.columns)}")
            
            signals = pd.DataFrame(index=self.data.index)
            signals['signal'] = 0
            signals['position'] = 0
            
            logger.info("Finding required columns...")
            
            # Find the correct column names for MultiIndex
            close_col = self._find_column('Close')
            volume_col = self._find_column('Volume')
            sma_5_col = self._find_column('SMA_5')
            sma_13_col = self._find_column('SMA_13')
            rsi_col = self._find_column('RSI')
            bb_upper_col = self._find_column('BB_upper_scalping')
            bb_lower_col = self._find_column('BB_lower_scalping')
            macd_col = self._find_column('MACD')
            macd_signal_col = self._find_column('MACD_signal')
            stoch_col = self._find_column('Stoch_K')
            atr_col = self._find_column('ATR')
            
            logger.info(f"Column mapping:")
            logger.info(f"  Close: {close_col}")
            logger.info(f"  Volume: {volume_col}")
            logger.info(f"  SMA_5: {sma_5_col}")
            logger.info(f"  SMA_13: {sma_13_col}")
            logger.info(f"  RSI: {rsi_col}")
            logger.info(f"  BB_upper_scalping: {bb_upper_col}")
            logger.info(f"  BB_lower_scalping: {bb_lower_col}")
            logger.info(f"  MACD: {macd_col}")
            logger.info(f"  MACD_signal: {macd_signal_col}")
            logger.info(f"  Stoch_K: {stoch_col}")
            logger.info(f"  ATR: {atr_col}")
            
            # Check required columns
            required_columns = [close_col, volume_col, sma_5_col, sma_13_col, rsi_col]
            missing_columns = [col for col in required_columns if col is None]
            
            if missing_columns:
                logger.error(f"Required BTC Scalping strategy columns not found: {missing_columns}")
                logger.error(f"Available columns: {list(self.data.columns)}")
                logger.error("This usually means indicators were not calculated properly")
                return None
            
            logger.info("All required columns found successfully")
            
            # BTC Scalping strategy signals
            logger.info(f"Starting signal generation loop for {len(self.data)} data points...")
            logger.info(f"Will process from index 26 to {len(self.data)}")
            
            signal_count = 0
            error_count = 0
            
            for i in range(26, len(self.data)):
                if i < 5:  # Need at least 5 bars for lowest/highest calculations
                    continue
                
                try:
                    close = self.data[close_col].iloc[i]
                    volume = self.data[volume_col].iloc[i] if volume_col else 1
                    sma_5 = self.data[sma_5_col].iloc[i]
                    sma_13 = self.data[sma_13_col].iloc[i]
                    rsi = self.data[rsi_col].iloc[i]
                    
                    # Check for NaN values
                    if pd.isna(close) or pd.isna(sma_5) or pd.isna(sma_13) or pd.isna(rsi):
                        continue  # Skip this iteration if we have NaN values
                    
                    # Calculate volume SMA (5-period)
                    volume_sma = 1
                    if volume_col and i >= 5:
                        volume_sma = self.data[volume_col].iloc[i-5:i].mean()
                    
                    # Calculate lowest and highest close over 5 periods
                    lowest_close_5 = self.data[close_col].iloc[i-5:i].min()
                    highest_close_5 = self.data[close_col].iloc[i-5:i].max()
                    
                    # Initialize signal components
                    long_score = 0
                    short_score = 0
                
                    # LONG SIGNAL COMPONENTS (need at least 3 confirmations)
                    # 1. Fast SMA crossover above slow SMA
                    if i > 0:
                        sma_5_prev = self.data[sma_5_col].iloc[i-1]
                        sma_13_prev = self.data[sma_13_col].iloc[i-1]
                        if pd.notna(sma_5_prev) and pd.notna(sma_13_prev):
                            if sma_5_prev <= sma_13_prev and sma_5 > sma_13:
                                long_score += 1
                    
                    # 2. RSI oversold bounce
                    if pd.notna(rsi) and rsi > 30:
                        long_score += 1
                    
                    # 3. Price above lower Bollinger Band with volume
                    if bb_lower_col and pd.notna(self.data[bb_lower_col].iloc[i]):
                        bb_lower = self.data[bb_lower_col].iloc[i]
                        if close > bb_lower and volume > volume_sma * 1.0:
                            long_score += 1
                    
                    # 4. MACD bullish crossover
                    if macd_col and macd_signal_col and i > 0:
                        macd = self.data[macd_col].iloc[i]
                        macd_signal = self.data[macd_signal_col].iloc[i]
                        macd_prev = self.data[macd_col].iloc[i-1]
                        signal_prev = self.data[macd_signal_col].iloc[i-1]
                        if (pd.notna(macd) and pd.notna(macd_signal) and 
                            pd.notna(macd_prev) and pd.notna(signal_prev)):
                            if macd_prev <= signal_prev and macd > macd_signal:
                                long_score += 1
                    
                    # 5. Stochastic oversold bounce
                    if stoch_col and pd.notna(self.data[stoch_col].iloc[i]):
                        stoch = self.data[stoch_col].iloc[i]
                        if stoch > 25:
                            long_score += 1
                    
                    # 6. Price making higher low
                    if close > lowest_close_5 and close > sma_5:
                        long_score += 1
                    
                    # 7. ATR expansion for volatility confirmation
                    if atr_col and i >= 10:
                        atr = self.data[atr_col].iloc[i]
                        atr_sma = self.data[atr_col].iloc[i-10:i].mean()
                        if pd.notna(atr) and pd.notna(atr_sma) and atr > atr_sma:
                            long_score += 1
                    
                    # SHORT SIGNAL COMPONENTS (need at least 3 confirmations)
                    # 1. Fast SMA crossover below slow SMA
                    if i > 0:
                        sma_5_prev = self.data[sma_5_col].iloc[i-1]
                        sma_13_prev = self.data[sma_13_col].iloc[i-1]
                        if pd.notna(sma_5_prev) and pd.notna(sma_13_prev):
                            if sma_5_prev >= sma_13_prev and sma_5 < sma_13:
                                short_score += 1
                    
                    # 2. RSI overbought rejection
                    if pd.notna(rsi) and rsi < 70:
                        short_score += 1
                    
                    # 3. Price below upper Bollinger Band with volume
                    if bb_upper_col and pd.notna(self.data[bb_upper_col].iloc[i]):
                        bb_upper = self.data[bb_upper_col].iloc[i]
                        if close < bb_upper and volume > volume_sma * 1.0:
                            short_score += 1
                    
                    # 4. MACD bearish crossover
                    if macd_col and macd_signal_col and i > 0:
                        macd = self.data[macd_col].iloc[i]
                        macd_signal = self.data[macd_signal_col].iloc[i]
                        macd_prev = self.data[macd_col].iloc[i-1]
                        signal_prev = self.data[macd_signal_col].iloc[i-1]
                        if (pd.notna(macd) and pd.notna(macd_signal) and 
                            pd.notna(macd_prev) and pd.notna(signal_prev)):
                            if macd_prev >= signal_prev and macd < macd_signal:
                                short_score += 1
                    
                    # 5. Stochastic overbought rejection
                    if stoch_col and pd.notna(self.data[stoch_col].iloc[i]):
                        stoch = self.data[stoch_col].iloc[i]
                        if stoch < 75:
                            short_score += 1
                    
                    # 6. Price making lower high
                    if close < highest_close_5 and close < sma_5:
                        short_score += 1
                    
                    # 7. ATR expansion for volatility confirmation
                    if atr_col and i >= 10:
                        atr = self.data[atr_col].iloc[i]
                        atr_sma = self.data[atr_col].iloc[i-10:i].mean()
                        if pd.notna(atr) and pd.notna(atr_sma) and atr > atr_sma:
                            short_score += 1
                    
                    # Generate signals based on score (need at least 3 confirmations)
                    if long_score >= 3:
                        signals['signal'].iloc[i] = 1
                        signal_count += 1
                    elif short_score >= 3:
                        signals['signal'].iloc[i] = -1
                        signal_count += 1
                        
                except Exception as e:
                    error_count += 1
                    if error_count <= 5:  # Log first 5 errors to avoid spam
                        logger.warning(f"Error processing data point {i}: {e}")
                    continue
            
            logger.info(f"Signal generation completed successfully")
            logger.info(f"Total signals generated: {signal_count}")
            logger.info(f"Errors encountered: {error_count}")
            logger.info(f"Final signals shape: {signals.shape}")
            logger.info(f"Signals with non-zero values: {(signals['signal'] != 0).sum()}")
            
            logger.info(f"=== BTC Scalping Strategy completed successfully ===")
            return self._calculate_strategy_results(signals, strategy_name)
            
        except Exception as e:
            logger.error(f"=== BTC Scalping Strategy FAILED ===")
            logger.error(f"Error running BTC Scalping strategy: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            logger.error(f"Data state: {getattr(self, 'data', 'No data attribute')}")
            logger.error(f"Signals state: {getattr(self, 'signals', 'No signals attribute')}")
            return None
    
    def _calculate_strategy_results(self, signals, strategy_name):
        """Calculate strategy results and metrics"""
        try:
            logger.info(f"=== Starting _calculate_strategy_results for {strategy_name} ===")
            logger.info(f"Signals shape: {signals.shape}")
            logger.info(f"Signals columns: {list(signals.columns)}")
            logger.info(f"Data shape: {self.data.shape}")
            logger.info(f"Data columns: {list(self.data.columns)}")
            
            # Check if Close column exists
            close_col = self._find_column('Close')
            if close_col is None:
                logger.error("Close column not found for returns calculation")
                return None
            
            logger.info(f"Using Close column: {close_col}")
            
            # Calculate returns
            logger.info("Calculating returns...")
            try:
                self.data['returns'] = self.data[close_col].pct_change()
                self.data['strategy_returns'] = self.data['returns'] * signals['signal'].shift(1)
                self.data['cumulative_returns'] = (1 + self.data['strategy_returns']).cumprod()
                logger.info("Returns calculated successfully")
            except Exception as e:
                logger.error(f"Error calculating returns: {e}")
                return None
            
            # Calculate metrics
            logger.info("Calculating metrics...")
            try:
                metrics = self.calculate_metrics()
                logger.info(f"Metrics calculated: {list(metrics.keys()) if isinstance(metrics, dict) else 'Not a dict'}")
            except Exception as e:
                logger.error(f"Error calculating metrics: {e}")
                metrics = {}
            
            # Prepare chart data
            logger.info(f"Preparing chart data for {strategy_name}")
            try:
                chart_data = self._prepare_chart_data(signals)
                logger.info(f"Chart data prepared with {len(chart_data.get('timestamps', []))} points")
            except Exception as e:
                logger.error(f"Error preparing chart data: {e}")
                chart_data = self._empty_chart_data()
            
            # Store results
            try:
                final_return = self.data['cumulative_returns'].iloc[-1] - 1 if 'cumulative_returns' in self.data.columns else 0
                
                self.results[strategy_name] = {
                    'signals': signals,
                    'metrics': metrics,
                    'final_return': final_return,
                    'chart_data': chart_data
                }
                
                logger.info(f"Strategy results stored for {strategy_name}")
                logger.info(f"Results keys: {list(self.results[strategy_name].keys())}")
                logger.info(f"Chart data available: {bool(chart_data.get('timestamps'))}")
                
            except Exception as e:
                logger.error(f"Error storing results: {e}")
                return None
            
            logger.info(f"=== _calculate_strategy_results completed successfully ===")
            return self.results[strategy_name]
            
        except Exception as e:
            logger.error(f"=== _calculate_strategy_results FAILED ===")
            logger.error(f"Error calculating strategy results: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            logger.error(f"Signals state: {getattr(signals, 'shape', 'No shape attribute') if signals is not None else 'Signals is None'}")
            logger.error(f"Data state: {getattr(self, 'data', 'No data attribute')}")
            return None
    
    def _prepare_chart_data(self, signals):
        """Prepare data for frontend charts including OHLC data for candlestick charts"""
        try:
            logger.info(f"Data columns available: {list(self.data.columns)}")
            logger.info(f"Data shape: {self.data.shape}")
            
            # Handle MultiIndex columns - find the correct column names
            open_col = None
            high_col = None
            low_col = None
            close_col = None
            volume_col = None
            cumret_col = None
            
            for col in self.data.columns:
                if isinstance(col, tuple):
                    if col[0] == 'Open':
                        open_col = col
                    elif col[0] == 'High':
                        high_col = col
                    elif col[0] == 'Low':
                        low_col = col
                    elif col[0] == 'Close':
                        close_col = col
                    elif col[0] == 'Volume':
                        volume_col = col
                    elif col[0] == 'cumulative_returns':
                        cumret_col = col
                else:
                    if col == 'Open':
                        open_col = col
                    elif col == 'High':
                        high_col = col
                    elif col == 'Low':
                        low_col = col
                    elif col == 'Close':
                        close_col = col
                    elif col == 'Volume':
                        volume_col = col
                    elif col == 'cumulative_returns':
                        cumret_col = col
            
            if close_col is None or cumret_col is None:
                logger.error(f"Required columns not found. Close: {close_col}, Cumulative returns: {cumret_col}")
                return self._empty_chart_data()
            
            logger.info(f"Using columns - Open: {open_col}, High: {high_col}, Low: {low_col}, Close: {close_col}, Volume: {volume_col}, Cumulative returns: {cumret_col}")
            
            # Get data with valid cumulative returns
            valid_data = self.data.dropna(subset=[close_col, cumret_col])
            logger.info(f"Valid data shape after dropna: {valid_data.shape}")
            
            if len(valid_data) == 0:
                logger.warning("No valid data for chart after dropna")
                return self._empty_chart_data()
            
            # Limit to last 500 points for performance
            if len(valid_data) > 500:
                valid_data = valid_data.tail(500)
                logger.info(f"Limited to last 500 points: {valid_data.shape}")
            
            # Convert timestamps to strings for JSON serialization
            timestamps = [str(ts.date()) for ts in valid_data.index]
            logger.info(f"Generated {len(timestamps)} timestamps")
            
            # OHLC data for candlestick charts
            ohlc_data = []
            for i, idx in enumerate(valid_data.index):
                candle = {
                    'x': timestamps[i],
                    'o': float(valid_data[open_col].iloc[i]) if open_col and not pd.isna(valid_data[open_col].iloc[i]) else float(valid_data[close_col].iloc[i]),
                    'h': float(valid_data[high_col].iloc[i]) if high_col and not pd.isna(valid_data[high_col].iloc[i]) else float(valid_data[close_col].iloc[i]),
                    'l': float(valid_data[low_col].iloc[i]) if low_col and not pd.isna(valid_data[low_col].iloc[i]) else float(valid_data[close_col].iloc[i]),
                    'c': float(valid_data[close_col].iloc[i])
                }
                ohlc_data.append(candle)
            
            # Price data (close prices for backward compatibility)
            prices = valid_data[close_col].tolist()
            
            # Volume data
            volumes = valid_data[volume_col].tolist() if volume_col else [0] * len(valid_data)
            
            # Cumulative returns (convert to percentage)
            cumulative_returns = [(ret - 1) * 100 for ret in valid_data[cumret_col].tolist()]
            
            # Buy/sell signals - align with valid_data index
            buy_signals = []
            sell_signals = []
            
            # Iterate through valid_data index and check for signals
            for i, idx in enumerate(valid_data.index):
                try:
                    if idx in signals.index:
                        signal_value = signals.loc[idx, 'signal']
                        timestamp = timestamps[i]
                        price = prices[i]
                        
                        if signal_value == 1:  # Buy signal
                            buy_signals.append({
                                'x': timestamp,
                                'y': price
                            })
                        elif signal_value == -1:  # Sell signal
                            sell_signals.append({
                                'x': timestamp,
                                'y': price
                            })
                except Exception as e:
                    # Skip this signal if there's an error
                    logger.debug(f"Skipping signal at index {idx}: {e}")
                    continue
            
            chart_data = {
                'timestamps': timestamps,
                'prices': prices,
                'ohlc_data': ohlc_data,
                'volumes': volumes,
                'cumulative_returns': cumulative_returns,
                'buy_signals': buy_signals,
                'sell_signals': sell_signals
            }
            
            logger.info(f"Prepared chart data: {len(timestamps)} points, {len(ohlc_data)} OHLC candles, {len(buy_signals)} buy signals, {len(sell_signals)} sell signals")
            return chart_data
            
        except Exception as e:
            logger.error(f"Error preparing chart data: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return self._empty_chart_data()
    
    def _empty_chart_data(self):
        """Return empty chart data structure"""
        return {
            'timestamps': [],
            'prices': [],
            'ohlc_data': [],
            'volumes': [],
            'cumulative_returns': [],
            'buy_signals': [],
            'sell_signals': []
        }
    
    def run_simple_strategy(self, strategy_name="Simple MA Cross"):
        """Backwards compatibility method"""
        return self.run_strategy(strategy_name)
    
    def calculate_metrics(self):
        """Calculate basic performance metrics"""
        try:
            returns = self.data['strategy_returns'].dropna()
            
            if len(returns) == 0:
                return {}
            
            total_return = self.data['cumulative_returns'].iloc[-1] - 1
            annual_return = (1 + total_return) ** (252 / len(returns)) - 1
            volatility = returns.std() * np.sqrt(252)
            sharpe_ratio = annual_return / volatility if volatility > 0 else 0
            
            # Max drawdown
            cumulative = self.data['cumulative_returns']
            drawdown = 1 - cumulative / cumulative.cummax()
            max_drawdown = drawdown.max()
            
            return {
                'total_return': float(total_return),
                'annual_return': float(annual_return),
                'volatility': float(volatility),
                'sharpe_ratio': float(sharpe_ratio),
                'max_drawdown': float(max_drawdown)
            }
        except Exception as e:
            logger.error(f"Error calculating metrics: {e}")
            return {}
    
    def get_results_json(self):
        """Get results in JSON format for web API"""
        try:
            logger.info(f"get_results_json called. Available results: {list(self.results.keys())}")
            
            if not self.results:
                logger.warning("No results available")
                return {'error': 'No results available'}
            
            # Get all strategy names
            strategy_names = list(self.results.keys())
            logger.info(f"Strategy names found: {strategy_names}")
            
            if not strategy_names:
                logger.warning("No strategy results found")
                return {'error': 'No strategy results found'}
            
            # Get the LAST (most recent) strategy only
            current_strategy = strategy_names[-1]
            result = self.results[current_strategy]
            chart_data = result.get('chart_data', {})
            
            logger.info(f"Returning results for single strategy: {current_strategy}")
            logger.info(f"Chart data available: {bool(chart_data.get('timestamps'))}")
            
            # Return results for just the current strategy
            web_results = {
                current_strategy: {
                    'metrics': result['metrics'],
                    'final_return': result['final_return'],
                    'signal_count': int((result['signals']['signal'] != 0).sum()),
                    'chart_data': chart_data
                }
            }
            
            logger.info(f"Returning single strategy result with chart_data included")
            return web_results
            
        except Exception as e:
            logger.error(f"Error formatting results: {e}")
            return {'error': str(e)}
    
    def _find_column(self, column_name):
        """Find column in MultiIndex or regular DataFrame"""
        for col in self.data.columns:
            if isinstance(col, tuple):
                if col[0] == column_name:
                    return col
            else:
                if col == column_name:
                    return col
        return None 