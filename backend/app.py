from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import io
from datetime import datetime, timedelta
import json

from config import Config
from models import db, User, Portfolio, Watchlist
from auth import init_auth, register_user, login_user, jwt_required

app = Flask(__name__)
app.config.from_object(Config)
CORS(app)

db.init_app(app)
init_auth(app)

# Initialize database
with app.app_context():
    db.create_all()

# Financial API Service
class FinancialDataService:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://www.alphavantage.co/query"
    
    def get_stock_quote(self, symbol):
        params = {
            'function': 'GLOBAL_QUOTE',
            'symbol': symbol,
            'apikey': self.api_key
        }
        try:
            response = requests.get(self.base_url, params=params)
            data = response.json()
            if 'Global Quote' in data:
                quote = data['Global Quote']
                return {
                    'symbol': symbol,
                    'price': float(quote['05. price']),
                    'change': float(quote['09. change']),
                    'change_percent': quote['10. change percent'].rstrip('%'),
                    'volume': int(quote['06. volume']),
                    'latest_trading_day': quote['07. latest trading day']
                }
        except Exception as e:
            print(f"Error fetching stock quote: {e}")
        return None
    
    def get_historical_data(self, symbol, period='1month'):
        periods = {
            '1week': 'TIME_SERIES_DAILY',
            '1month': 'TIME_SERIES_DAILY',
            '3months': 'TIME_SERIES_DAILY',
            '1year': 'TIME_SERIES_MONTHLY'
        }
        
        params = {
            'function': periods.get(period, 'TIME_SERIES_DAILY'),
            'symbol': symbol,
            'apikey': self.api_key,
            'outputsize': 'compact'
        }
        
        try:
            response = requests.get(self.base_url, params=params)
            data = response.json()
            
            time_series = None
            if 'Time Series (Daily)' in data:
                time_series = data['Time Series (Daily)']
            elif 'Monthly Time Series' in data:
                time_series = data['Monthly Time Series']
            
            if time_series:
                dates = []
                prices = []
                for date_str, values in list(time_series.items())[:30]:  # Last 30 data points
                    dates.append(datetime.strptime(date_str, '%Y-%m-%d'))
                    prices.append(float(values['4. close']))
                
                dates.reverse()
                prices.reverse()
                
                return {
                    'dates': [d.strftime('%Y-%m-%d') for d in dates],
                    'prices': prices
                }
        except Exception as e:
            print(f"Error fetching historical data: {e}")
        
        return None

financial_service = FinancialDataService(app.config['ALPHA_VANTAGE_API_KEY'])

# Authentication Routes
@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    user, error = register_user(data['username'], data['email'], data['password'])
    
    if error:
        return jsonify({'error': error}), 400
    
    access_token = create_access_token(identity=user)
    return jsonify({
        'message': 'User created successfully',
        'access_token': access_token,
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'role': user.role
        }
    }), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    access_token, error = login_user(data['username'], data['password'])
    
    if error:
        return jsonify({'error': error}), 401
    
    user = User.query.filter_by(username=data['username']).first()
    return jsonify({
        'access_token': access_token,
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'role': user.role
        }
    })

# Dashboard Routes
@app.route('/api/dashboard/overview')
@jwt_required()
def dashboard_overview():
    current_user = get_jwt_identity()
    
    # Get user's portfolio
    portfolios = Portfolio.query.filter_by(user_id=current_user.id).all()
    
    total_investment = 0
    current_value = 0
    portfolio_data = []
    
    for portfolio in portfolios:
        stock_data = financial_service.get_stock_quote(portfolio.symbol)
        if stock_data:
            investment = portfolio.quantity * portfolio.purchase_price
            current_val = portfolio.quantity * stock_data['price']
            gain_loss = current_val - investment
            gain_loss_percent = (gain_loss / investment) * 100
            
            total_investment += investment
            current_value += current_val
            
            portfolio_data.append({
                'symbol': portfolio.symbol,
                'quantity': portfolio.quantity,
                'purchase_price': portfolio.purchase_price,
                'current_price': stock_data['price'],
                'investment': investment,
                'current_value': current_val,
                'gain_loss': gain_loss,
                'gain_loss_percent': gain_loss_percent
            })
    
    overall_gain_loss = current_value - total_investment
    overall_gain_loss_percent = (overall_gain_loss / total_investment) * 100 if total_investment > 0 else 0
    
    return jsonify({
        'portfolio_summary': {
            'total_investment': total_investment,
            'current_value': current_value,
            'overall_gain_loss': overall_gain_loss,
            'overall_gain_loss_percent': overall_gain_loss_percent
        },
        'holdings': portfolio_data
    })

@app.route('/api/stocks/<symbol>')
@jwt_required()
def get_stock_data(symbol):
    quote = financial_service.get_stock_quote(symbol)
    historical = financial_service.get_historical_data(symbol, '1month')
    
    return jsonify({
        'quote': quote,
        'historical': historical
    })

@app.route('/api/stocks/<symbol>/chart')
@jwt_required()
def get_stock_chart(symbol):
    historical = financial_service.get_historical_data(symbol, '3months')
    
    if not historical:
        return jsonify({'error': 'Could not fetch historical data'}), 400
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=historical['dates'],
        y=historical['prices'],
        mode='lines',
        name=f'{symbol} Price',
        line=dict(color='#00D4AA', width=2)
    ))
    
    fig.update_layout(
        title=f'{symbol} Price Trend',
        xaxis_title='Date',
        yaxis_title='Price ($)',
        template='plotly_dark',
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='white')
    )
    
    return jsonify(fig.to_dict())

@app.route('/api/portfolio', methods=['POST'])
@jwt_required()
def add_to_portfolio():
    current_user = get_jwt_identity()
    data = request.get_json()
    
    portfolio = Portfolio(
        user_id=current_user.id,
        symbol=data['symbol'],
        quantity=data['quantity'],
        purchase_price=data['purchase_price']
    )
    
    db.session.add(portfolio)
    db.session.commit()
    
    return jsonify({'message': 'Stock added to portfolio'})

@app.route('/api/watchlist', methods=['POST'])
@jwt_required()
def add_to_watchlist():
    current_user = get_jwt_identity()
    data = request.get_json()
    
    # Check if already in watchlist
    existing = Watchlist.query.filter_by(user_id=current_user.id, symbol=data['symbol']).first()
    if existing:
        return jsonify({'error': 'Stock already in watchlist'}), 400
    
    watchlist = Watchlist(
        user_id=current_user.id,
        symbol=data['symbol']
    )
    
    db.session.add(watchlist)
    db.session.commit()
    
    return jsonify({'message': 'Stock added to watchlist'})

@app.route('/api/watchlist')
@jwt_required()
def get_watchlist():
    current_user = get_jwt_identity()
    watchlist_items = Watchlist.query.filter_by(user_id=current_user.id).all()
    
    watchlist_data = []
    for item in watchlist_items:
        stock_data = financial_service.get_stock_quote(item.symbol)
        if stock_data:
            watchlist_data.append({
                'symbol': item.symbol,
                'price': stock_data['price'],
                'change': stock_data['change'],
                'change_percent': stock_data['change_percent']
            })
    
    return jsonify(watchlist_data)

@app.route('/api/reports/portfolio')
@jwt_required()
def generate_portfolio_report():
    current_user = get_jwt_identity()
    
    # Get portfolio data
    portfolios = Portfolio.query.filter_by(user_id=current_user.id).all()
    
    report_data = []
    for portfolio in portfolios:
        stock_data = financial_service.get_stock_quote(portfolio.symbol)
        if stock_data:
            report_data.append({
                'Symbol': portfolio.symbol,
                'Quantity': portfolio.quantity,
                'Purchase Price': f"${portfolio.purchase_price:.2f}",
                'Current Price': f"${stock_data['price']:.2f}",
                'Investment': f"${portfolio.quantity * portfolio.purchase_price:.2f}",
                'Current Value': f"${portfolio.quantity * stock_data['price']:.2f}",
                'Gain/Loss': f"${portfolio.quantity * (stock_data['price'] - portfolio.purchase_price):.2f}"
            })
    
    # Create CSV
    df = pd.DataFrame(report_data)
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    
    return send_file(
        io.BytesIO(csv_buffer.getvalue().encode()),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'portfolio_report_{datetime.now().strftime("%Y%m%d")}.csv'
    )

if __name__ == '__main__':
    app.run(debug=True, port=5000)
