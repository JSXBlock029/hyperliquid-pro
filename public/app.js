document.addEventListener('DOMContentLoaded', () => {
    // Elements
    const apiStatus = document.getElementById('api-status');
    const accountBalance = document.getElementById('account-balance');
    const activePositions = document.getElementById('active-positions');
    const tradingForm = document.getElementById('trading-form');
    const orderType = document.getElementById('order-type');
    const limitPriceContainer = document.getElementById('limit-price-container');
    const refreshActivityBtn = document.getElementById('refresh-activity');
    const activityTableBody = document.getElementById('activity-table-body');
    const webhookUrl = document.getElementById('webhook-url');
    const errorCard = document.getElementById('error-card');
    const errorMessage = document.getElementById('error-message');
    const errorDetails = document.getElementById('error-details');
    const errorClose = document.getElementById('error-close');

    // Initialize Socket.IO
    const socket = io({
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionAttempts: 5,
        reconnectionDelay: 1000
    });
    
    // Socket.IO event handlers
    socket.on('connect', () => {
        console.log('WebSocket connected');
        updateConnectionStatus({ connected: true });
    });
    
    socket.on('connect_error', (error) => {
        console.error('WebSocket connection error:', error);
        updateConnectionStatus({ connected: false });
    });
    
    socket.on('disconnect', () => {
        console.log('WebSocket disconnected');
        updateConnectionStatus({ connected: false });
    });
    
    socket.on('connection_status', (status) => {
        console.log('Connection status update:', status);
        updateConnectionStatus(status);
    });

    // Set webhook URL based on current location
    webhookUrl.textContent = 'https://desktop-qs4pv16.tail63b970.ts.net/webhook';

    // Event Listeners
    if (orderType) {
        orderType.addEventListener('change', () => {
            limitPriceContainer.style.display = orderType.value === 'limit' ? 'block' : 'none';
        });
    }
    
    if (tradingForm) {
        tradingForm.addEventListener('submit', handleOrderSubmit);
    }
    
    if (refreshActivityBtn) {
        refreshActivityBtn.addEventListener('click', fetchRecentActivity);
    }
    
    // Add refresh positions button listener
    const refreshPositionsBtn = document.getElementById('refresh-positions');
    if (refreshPositionsBtn) {
        refreshPositionsBtn.addEventListener('click', () => {
            console.log('Refresh positions button clicked');
            fetchPositions();
        });
    }
    
    // Add close all positions button listener
    const closeAllPositionsBtn = document.getElementById('close-all-positions');
    if (closeAllPositionsBtn) {
        closeAllPositionsBtn.addEventListener('click', () => {
            if (confirm('Are you sure you want to close ALL positions? This action cannot be undone.')) {
                alert('Close all positions - This feature is coming soon!');
                // TODO: Implement close all positions functionality
            }
        });
    }

    // Initial actions
    checkApiStatus();
    fetchAccountInfo();
    fetchPositions();
    fetchRealizedPnl();
    fetchRecentActivity();
    initTradingSection();

    // Initialize news section
    initNewsSection();

    // Function to update position charts with real data
    function updatePositionCharts(positions, totalPnl) {
        console.log('📊 Updating position charts with real data:', positions);
        
        // Update P&L Over Time Chart
        const pnlChart = document.getElementById('positions-pnl-chart');
        if (pnlChart && window.positionsPnlChart) {
            const currentTime = new Date().toLocaleTimeString('en-US', { 
                hour12: false, 
                hour: '2-digit', 
                minute: '2-digit' 
            });
            
            // Add new data point
            window.positionsPnlChart.data.labels.push(currentTime);
            window.positionsPnlChart.data.datasets[0].data.push(totalPnl);
            
            // Keep only last 20 data points
            if (window.positionsPnlChart.data.labels.length > 20) {
                window.positionsPnlChart.data.labels.shift();
                window.positionsPnlChart.data.datasets[0].data.shift();
            }
            
            window.positionsPnlChart.update('none');
        }
        
        // Update Performance by Asset Chart
        const performanceChart = document.getElementById('positions-performance-chart');
        if (performanceChart && window.positionsPerformanceChart && positions.length > 0) {
            const labels = positions.map(p => p.symbol);
            const pnlData = positions.map(p => p.unrealizedPnl || 0);
            const colors = pnlData.map(pnl => pnl >= 0 ? '#10b981' : '#ef4444');
            
            window.positionsPerformanceChart.data.labels = labels;
            window.positionsPerformanceChart.data.datasets[0].data = pnlData;
            window.positionsPerformanceChart.data.datasets[0].backgroundColor = colors;
            
            window.positionsPerformanceChart.update('none');
        }
        
        // Update Position Distribution Chart
        const distributionChart = document.getElementById('positions-distribution-chart');
        if (distributionChart && window.positionsDistributionChart && positions.length > 0) {
            const longPositions = positions.filter(p => p.side === 'LONG').length;
            const shortPositions = positions.filter(p => p.side === 'SHORT').length;
            
            window.positionsDistributionChart.data.datasets[0].data = [longPositions, shortPositions];
            window.positionsDistributionChart.update('none');
        }
        
        // Update Risk Metrics
        updateRiskMetrics(positions, totalPnl);
    }
    
    // Function to update risk metrics
    function updateRiskMetrics(positions, totalPnl) {
        if (positions.length === 0) return;
        
        // Calculate total portfolio value
        const totalValue = positions.reduce((sum, pos) => 
            sum + (pos.size * pos.currentPrice), 0);
        
        // Calculate largest position percentage
        const largestPosition = Math.max(...positions.map(pos => 
            (pos.size * pos.currentPrice) / totalValue * 100));
        
        // Calculate average P&L percentage
        const avgPnlPercent = positions.reduce((sum, pos) => 
            sum + (pos.pnlPercentage || 0), 0) / positions.length;
        
        // Update risk metrics display - find by text content instead
        const chartCards = document.querySelectorAll('.chart-card h6');
        let riskMetricsContainer = null;
        
        for (let h6 of chartCards) {
            if (h6.textContent.includes('Risk Metrics')) {
                riskMetricsContainer = h6;
                break;
            }
        }
        
        if (riskMetricsContainer) {
            const metricsDiv = riskMetricsContainer.parentElement.querySelector('.risk-metrics') || 
                              document.createElement('div');
            metricsDiv.className = 'risk-metrics mt-3';
            
            metricsDiv.innerHTML = `
                <div class="row text-center">
                    <div class="col-4">
                        <div class="metric-item">
                            <div class="metric-value">${positions.length}</div>
                            <div class="metric-label">Active Positions</div>
                        </div>
                    </div>
                    <div class="col-4">
                        <div class="metric-item">
                            <div class="metric-value">${largestPosition.toFixed(1)}%</div>
                            <div class="metric-label">Largest Position</div>
                        </div>
                    </div>
                    <div class="col-4">
                        <div class="metric-item">
                            <div class="metric-value ${avgPnlPercent >= 0 ? 'text-success' : 'text-danger'}">
                                ${avgPnlPercent >= 0 ? '+' : ''}${avgPnlPercent.toFixed(2)}%
                            </div>
                            <div class="metric-label">Avg P&L</div>
                        </div>
                    </div>
                </div>
            `;
            
            if (!riskMetricsContainer.parentElement.contains(metricsDiv)) {
                riskMetricsContainer.parentElement.appendChild(metricsDiv);
            }
        }
    }
    
    // Initialize position charts
    function initPositionCharts() {
        console.log('📊 Initializing position charts...');
        
        // Initialize P&L Over Time Chart
        const pnlCtx = document.getElementById('positions-pnl-chart');
        if (pnlCtx) {
            window.positionsPnlChart = new Chart(pnlCtx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'Unrealized P&L',
                        data: [],
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        tension: 0.4,
                        fill: true
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: true,
                            labels: { color: '#e2e8f0' }
                        }
                    },
                    scales: {
                        x: {
                            ticks: { color: '#94a3b8' },
                            grid: { color: 'rgba(148, 163, 184, 0.1)' }
                        },
                        y: {
                            ticks: { 
                                color: '#94a3b8',
                                callback: function(value) {
                                    return '$' + value.toFixed(2);
                                }
                            },
                            grid: { color: 'rgba(148, 163, 184, 0.1)' }
                        }
                    }
                }
            });
        }
        
        // Initialize Performance by Asset Chart
        const performanceCtx = document.getElementById('positions-performance-chart');
        if (performanceCtx) {
            window.positionsPerformanceChart = new Chart(performanceCtx, {
                type: 'bar',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'P&L by Asset',
                        data: [],
                        backgroundColor: '#10b981'
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false }
                    },
                    scales: {
                        x: {
                            ticks: { color: '#94a3b8' },
                            grid: { color: 'rgba(148, 163, 184, 0.1)' }
                        },
                        y: {
                            ticks: { 
                                color: '#94a3b8',
                                callback: function(value) {
                                    return '$' + value.toFixed(2);
                                }
                            },
                            grid: { color: 'rgba(148, 163, 184, 0.1)' }
                        }
                    }
                }
            });
        }
        
        // Initialize Position Distribution Chart  
        const distributionCtx = document.getElementById('positions-distribution-chart');
        if (distributionCtx) {
            window.positionsDistributionChart = new Chart(distributionCtx, {
                type: 'doughnut',
                data: {
                    labels: ['Long', 'Short'],
                    datasets: [{
                        data: [0, 0],
                        backgroundColor: ['#10b981', '#ef4444']
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { color: '#e2e8f0' }
                        }
                    }
                }
            });
        }
        
        console.log('✅ Position charts initialized');
    }

    // Update connection status display
    function updateConnectionStatus(status) {
        if (status.connected) {
            apiStatus.textContent = 'Connected';
            apiStatus.classList.remove('bg-secondary', 'bg-disconnected');
            apiStatus.classList.add('bg-connected');
            
            // Update mode and wallet info if available
            if (status.mode) {
                const modeBadge = document.getElementById('mode-badge');
                if (modeBadge) {
                    modeBadge.textContent = status.mode.toUpperCase();
                    modeBadge.className = `badge ${status.mode === 'real' ? 'bg-success' : 'bg-warning'}`;
                }
            }
            
            if (status.wallet) {
                const walletInfo = document.getElementById('wallet-info');
                const walletAddress = walletInfo.querySelector('.wallet-address');
                if (walletInfo && walletAddress) {
                    walletAddress.textContent = status.wallet;
                    // Add click event listener for blur toggle
                    walletInfo.addEventListener('click', toggleWalletBlur);
                }
            }
        } else {
            apiStatus.textContent = 'Disconnected';
            apiStatus.classList.remove('bg-secondary', 'bg-connected');
            apiStatus.classList.add('bg-disconnected');
            
            // Show error message
            showAlert('Lost connection to server. Attempting to reconnect...', 'warning');
        }
    }

    // Toggle wallet address blur effect
    function toggleWalletBlur() {
        const walletInfo = document.getElementById('wallet-info');
        if (walletInfo) {
            walletInfo.classList.toggle('blurred');
            
            // Update the hint text
            const hint = walletInfo.querySelector('.reveal-hint');
            if (hint) {
                hint.textContent = walletInfo.classList.contains('blurred') 
                    ? 'Click to reveal wallet address' 
                    : 'Click to hide wallet address';
            }
        }
    }

    // Check API connection status
    async function checkApiStatus() {
        try {
            const response = await fetch('/api/status');
            const data = await response.json();
            updateConnectionStatus(data);
        } catch (error) {
            updateConnectionStatus({ connected: false });
            console.error('Error checking API status:', error);
            showAlert('Could not connect to server. Please check if the server is running.', 'danger');
        }
    }

    // Fetch account information
    async function fetchAccountInfo() {
        try {
            const response = await fetch('/api/account');
            
            if (!response.ok) {
                const contentType = response.headers.get('content-type');
                let errorMessage = 'Unknown error fetching account information';
                
                if (contentType && contentType.includes('application/json')) {
                    const errorData = await response.json();
                    errorMessage = errorData.error || errorMessage;
                } else {
                    const text = await response.text();
                    errorMessage = `Server error: ${text}`;
                }
                
                throw new Error(errorMessage);
            }
            
            const contentType = response.headers.get('content-type');
            if (!contentType || !contentType.includes('application/json')) {
                throw new Error('Invalid response format from server');
            }
            
            const data = await response.json();
            
            // Check for error in the response
            if (data.error) {
                throw new Error(data.error);
            }

            // Update main wallet info
            const mainWallet = data.main_wallet;
            if (mainWallet) {
                // Update dashboard card
                const accountBalanceCard = document.getElementById('account-balance');
                if (accountBalanceCard) {
                    accountBalanceCard.textContent = `$${parseFloat(mainWallet.balance || 0).toFixed(2)}`;
                }
            }

            // Update API wallet info
            const apiWallet = data.api_wallet;

            // Dashboard position count is now handled by fetchPositions() function
            // which uses the working /api/positions endpoint

            // Update positions table
            const positionsTable = document.getElementById('positionsTable');
            if (positionsTable) {
                // Combine positions from both wallets
                const allPositions = [
                    ...(mainWallet?.positions || []).map(p => ({ ...p, wallet: 'Main' })),
                    ...(apiWallet?.positions || []).map(p => ({ ...p, wallet: 'API' }))
                ];

                if (allPositions.length > 0) {
                    const tbody = positionsTable.querySelector('tbody');
                    tbody.innerHTML = allPositions.map(position => {
                        // Calculate P&L
                        const unrealizedPnl = position.unrealizedPnl || 0;
                        const pnlPercentage = position.pnlPercentage || 0;
                        const pnlClass = unrealizedPnl >= 0 ? 'text-success' : 'text-danger';
                        const pnlIcon = unrealizedPnl >= 0 ? 'bi-arrow-up' : 'bi-arrow-down';
                        
                        return `
                            <tr>
                                <td>${position.symbol}</td>
                                <td>
                                    <span class="badge bg-secondary">${position.wallet}</span>
                                </td>
                                <td>
                                    <span class="badge ${position.side === 'LONG' ? 'bg-success' : 'bg-danger'}">
                                        ${position.side}
                                    </span>
                                </td>
                                <td>${position.size.toFixed(4)}</td>
                                <td>$${parseFloat(position.entryPrice).toFixed(2)}</td>
                                <td>$${parseFloat(position.currentPrice).toFixed(2)}</td>
                                <td>$${parseFloat(position.value).toFixed(2)}</td>
                                <td class="${pnlClass}">
                                    <div class="d-flex align-items-center">
                                        <i class="bi ${pnlIcon} me-1"></i>
                                        <div>
                                            <div>$${Math.abs(unrealizedPnl).toFixed(2)}</div>
                                            <small class="opacity-75">(${pnlPercentage.toFixed(2)}%)</small>
                                        </div>
                                    </div>
                                </td>
                                <td>
                                    <div class="btn-group">
                                        <button class="btn btn-sm btn-outline-primary" onclick="editPosition('${position.symbol}')">
                                            <i class="bi bi-pencil"></i>
                                        </button>
                                        <button class="btn btn-sm btn-outline-danger" onclick="closePosition('${position.symbol}', '${position.wallet}')">
                                            <i class="bi bi-x"></i>
                                        </button>
                                    </div>
                                </td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    positionsTable.querySelector('tbody').innerHTML = `
                        <tr class="empty-state">
                            <td colspan="9">
                                <div class="empty-content">
                                    <i class="bi bi-inbox"></i>
                                    <h6>No Active Positions</h6>
                                    <p>Your positions will appear here when you have open trades</p>
                                </div>
                            </td>
                        </tr>
                    `;
                }
            }
        } catch (error) {
            console.error('Error fetching account info:', error);
            // Show error state in the UI
            document.getElementById('mainWalletBalance').textContent = 'Error loading balance';
            document.getElementById('apiWalletBalance').textContent = 'Error loading balance';
            const positionsTable = document.getElementById('positionsTable');
            if (positionsTable) {
                positionsTable.querySelector('tbody').innerHTML = `
                    <tr class="empty-state">
                        <td colspan="9">
                            <div class="empty-content">
                                <i class="bi bi-exclamation-triangle"></i>
                                <h6>Error Loading Positions</h6>
                                <p>Unable to fetch position data. Please try refreshing.</p>
                            </div>
                        </td>
                    </tr>
                `;
            }
        }
    }

    // Fetch detailed positions with P&L data
    async function fetchPositions() {
        try {
            const response = await fetch('/api/positions');
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            
            const positions = await response.json();
            console.log('Fetched positions:', positions);
            
            const positionsTable = document.getElementById('positionsTable');
            if (positionsTable) {
                const tbody = positionsTable.querySelector('tbody');
                
                if (positions.length > 0) {
                    tbody.innerHTML = positions.map(position => {
                        // Debug log position data
                        console.log('Position data:', position);
                        
                        // Calculate P&L with debugging
                        const unrealizedPnl = position.unrealizedPnl || 0;
                        const pnlPercentage = position.pnlPercentage || 0;
                        const pnlClass = unrealizedPnl > 0 ? 'text-success' : (unrealizedPnl < 0 ? 'text-danger' : 'text-muted');
                        const pnlIcon = unrealizedPnl > 0 ? 'bi-arrow-up' : (unrealizedPnl < 0 ? 'bi-arrow-down' : 'bi-dash');
                        
                        console.log(`${position.symbol} P&L: $${unrealizedPnl}, ${pnlPercentage}%, Class: ${pnlClass}`);
                        
                        return `
                            <tr>
                                <td>
                                    <strong>${position.symbol}</strong>
                                </td>
                                <td>
                                    <span class="badge bg-secondary">Main</span>
                                </td>
                                <td>
                                    <span class="badge ${position.side === 'LONG' ? 'bg-success' : 'bg-danger'}">
                                        ${position.side}
                                    </span>
                                </td>
                                <td>${position.size.toFixed(4)}</td>
                                <td>$${parseFloat(position.entryPrice).toFixed(2)}</td>
                                <td>$${parseFloat(position.currentPrice).toFixed(2)}</td>
                                <td>$${(parseFloat(position.size) * parseFloat(position.currentPrice)).toFixed(2)}</td>
                                <td class="${pnlClass}">
                                    <div class="d-flex align-items-center">
                                        <i class="bi ${pnlIcon} me-1"></i>
                                        <div>
                                            <div><strong>${unrealizedPnl >= 0 ? '+' : ''}$${unrealizedPnl.toFixed(2)}</strong></div>
                                            <small class="opacity-75">(${pnlPercentage >= 0 ? '+' : ''}${pnlPercentage.toFixed(2)}%)</small>
                                        </div>
                                    </div>
                                </td>
                                <td>
                                    <div class="btn-group btn-group-sm">
                                        <button class="btn btn-outline-primary" onclick="editPosition('${position.symbol}')" title="Edit Position">
                                            <i class="bi bi-pencil"></i>
                                        </button>
                                        <button class="btn btn-outline-danger" onclick="closePosition('${position.symbol}')" title="Close Position">
                                            <i class="bi bi-x"></i>
                                        </button>
                                    </div>
                                </td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    tbody.innerHTML = `
                        <tr class="empty-state">
                            <td colspan="9">
                                <div class="empty-content">
                                    <i class="bi bi-inbox"></i>
                                    <h6>No Active Positions</h6>
                                    <p>Your positions will appear here when you have open trades</p>
                                </div>
                            </td>
                        </tr>
                    `;
                }
            }
            
            // Update dashboard positions count
            const activePositionsCard = document.getElementById('active-positions');
            if (activePositionsCard) {
                activePositionsCard.textContent = positions.length;
            }
            
            // Calculate and update total P&L
            const totalPnl = positions.reduce((sum, position) => {
                return sum + (position.unrealizedPnl || 0);
            }, 0);
            
            const totalPnlCard = document.getElementById('total-pnl');
            if (totalPnlCard) {
                const pnlClass = totalPnl >= 0 ? 'text-success' : 'text-danger';
                const pnlSign = totalPnl >= 0 ? '+' : '';
                totalPnlCard.textContent = `${pnlSign}$${totalPnl.toFixed(2)}`;
                totalPnlCard.className = `card-value ${pnlClass}`;
                
                // Update the change indicator next to total P&L
                const changeIndicator = totalPnlCard.parentElement.querySelector('.card-change');
                if (changeIndicator) {
                    const changeClass = totalPnl >= 0 ? 'positive' : 'negative';
                    const changeIcon = totalPnl >= 0 ? 'bi-arrow-up' : 'bi-arrow-down';
                    const changeSign = totalPnl >= 0 ? '+' : '';
                    
                    changeIndicator.className = `card-change ${changeClass}`;
                    changeIndicator.innerHTML = `
                        <i class="bi ${changeIcon}"></i>
                        <span>${changeSign}$${totalPnl.toFixed(2)}</span>
                    `;
                }
            }
            
            console.log(`Total P&L calculated: $${totalPnl.toFixed(2)} from ${positions.length} positions`);
            
            // After setting unrealized P&L, update the combined total
            // We need to get the current realized P&L value
            const realizedPnlCard = document.getElementById('realized-pnl');
            let currentRealizedPnl = 0;
            if (realizedPnlCard && realizedPnlCard.textContent !== '$0.00') {
                const realizedText = realizedPnlCard.textContent.replace(/[+$,]/g, '');
                currentRealizedPnl = parseFloat(realizedText) || 0;
            }
            updateTotalPnl(currentRealizedPnl);
            
            // Update position charts with real data
            updatePositionCharts(positions, totalPnl);
            
        } catch (error) {
            console.error('Error fetching positions:', error);
            const positionsTable = document.getElementById('positionsTable');
            if (positionsTable) {
                const tbody = positionsTable.querySelector('tbody');
                tbody.innerHTML = `
                    <tr class="empty-state">
                        <td colspan="9">
                            <div class="empty-content">
                                <i class="bi bi-exclamation-triangle"></i>
                                <h6>Error Loading Positions</h6>
                                <p>Unable to fetch position data: ${error.message}</p>
                            </div>
                        </td>
                    </tr>
                `;
            }
            
            // Reset total P&L to zero on error
            const totalPnlCard = document.getElementById('total-pnl');
            if (totalPnlCard) {
                totalPnlCard.textContent = '$0.00';
                totalPnlCard.className = 'card-value text-muted';
            }
        }
    }

    // Fetch realized P&L data
    async function fetchRealizedPnl() {
        try {
            const response = await fetch('/api/realized-pnl?days=30');
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            
            const data = await response.json();
            console.log('Fetched realized P&L:', data);
            
            // Update dashboard card for realized P&L
            const realizedPnlCard = document.getElementById('realized-pnl');
            if (realizedPnlCard) {
                const realizedPnl = data.totalRealizedPnl || 0;
                const pnlClass = realizedPnl >= 0 ? 'text-success' : 'text-danger';
                const pnlSign = realizedPnl >= 0 ? '+' : '';
                
                realizedPnlCard.textContent = `${pnlSign}$${realizedPnl.toFixed(2)}`;
                realizedPnlCard.className = `card-value ${pnlClass}`;
                
                // Update the change indicator
                const changeIndicator = realizedPnlCard.parentElement.querySelector('.card-change');
                if (changeIndicator) {
                    const changeClass = realizedPnl >= 0 ? 'positive' : 'negative';
                    const changeIcon = realizedPnl >= 0 ? 'bi-arrow-up' : 'bi-arrow-down';
                    
                    changeIndicator.className = `card-change ${changeClass}`;
                    changeIndicator.innerHTML = `
                        <i class="bi ${changeIcon}"></i>
                        <span>${data.tradeCount || 0} trades (30d)</span>
                    `;
                }
            }
            
            // Update total P&L to include both realized and unrealized
            updateTotalPnl(data.totalRealizedPnl || 0);
            
        } catch (error) {
            console.error('Error fetching realized P&L:', error);
            
            // Reset realized P&L to zero on error
            const realizedPnlCard = document.getElementById('realized-pnl');
            if (realizedPnlCard) {
                realizedPnlCard.textContent = '$0.00';
                realizedPnlCard.className = 'card-value text-muted';
            }
        }
    }

    // Update total P&L combining realized and unrealized
    function updateTotalPnl(realizedPnl = 0) {
        // Get current unrealized P&L from positions
        let unrealizedPnl = 0;
        const unrealizedPnlCard = document.getElementById('total-pnl');
        if (unrealizedPnlCard && unrealizedPnlCard.textContent !== '$0.00') {
            // Extract current unrealized P&L from the display
            const currentText = unrealizedPnlCard.textContent.replace(/[+$,]/g, '');
            unrealizedPnl = parseFloat(currentText) || 0;
        }
        
        // Calculate total P&L (realized + unrealized)
        const totalPnl = realizedPnl + unrealizedPnl;
        
        // Update the combined total P&L card
        const combinedTotalPnlCard = document.getElementById('combined-total-pnl');
        if (combinedTotalPnlCard) {
            const pnlClass = totalPnl >= 0 ? 'text-success' : 'text-danger';
            const pnlSign = totalPnl >= 0 ? '+' : '';
            combinedTotalPnlCard.textContent = `${pnlSign}$${totalPnl.toFixed(2)}`;
            combinedTotalPnlCard.className = `card-value ${pnlClass}`;
            
            // Update the change indicator
            const changeIndicator = combinedTotalPnlCard.parentElement.querySelector('.card-change');
            if (changeIndicator) {
                const changeClass = totalPnl >= 0 ? 'positive' : 'negative';
                const changeIcon = totalPnl >= 0 ? 'bi-arrow-up' : 'bi-arrow-down';
                
                changeIndicator.className = `card-change ${changeClass}`;
                changeIndicator.innerHTML = `
                    <i class="bi ${changeIcon}"></i>
                    <span>Real: $${realizedPnl.toFixed(2)} | Unreal: $${unrealizedPnl.toFixed(2)}</span>
                `;
            }
        }
        
        console.log(`Total P&L: $${totalPnl.toFixed(2)} (Realized: $${realizedPnl.toFixed(2)}, Unrealized: $${unrealizedPnl.toFixed(2)})`);
    }

    // Show error information
    function showError(title, details) {
        const errorCard = document.getElementById('error-card');
        const errorTitle = document.getElementById('error-title');
        const errorDetails = document.getElementById('error-details');
        const errorClose = document.getElementById('error-close');
        
        errorTitle.textContent = title;
        errorDetails.textContent = details;
        errorCard.style.display = 'block';
        
        // Auto-hide after 10 seconds
        setTimeout(() => {
            errorCard.style.display = 'none';
        }, 10000);
        
        // Close button handler
        if (errorClose) {
            errorClose.onclick = () => {
                errorCard.style.display = 'none';
            };
        }
    }

    // Fetch recent trading activity
    async function fetchRecentActivity() {
        try {
            const response = await fetch('/api/activity');
            const data = await response.json();
            
            console.log('Fetched activity data:', data);
            
            if (data.length === 0) {
                activityTableBody.innerHTML = `
                    <tr>
                        <td colspan="8" class="text-center">No recent activity</td>
                    </tr>
                `;
                return;
            }
            
            activityTableBody.innerHTML = '';
            data.forEach(activity => {
                const row = document.createElement('tr');
                
                // Format the time (convert from milliseconds to proper date)
                const formattedTime = formatDate(activity.time);
                
                // Format quantity and price
                const quantity = parseFloat(activity.qty || 0).toFixed(4);
                const price = parseFloat(activity.price || 0).toFixed(2);
                
                // Format closed P&L if available
                let pnlDisplay = '';
                if (activity.closedPnl !== null && activity.closedPnl !== undefined && activity.closedPnl !== 0) {
                    const pnl = parseFloat(activity.closedPnl);
                    const pnlClass = pnl >= 0 ? 'text-success' : 'text-danger';
                    const pnlSign = pnl >= 0 ? '+' : '';
                    pnlDisplay = `<span class="${pnlClass}">${pnlSign}$${pnl.toFixed(2)}</span>`;
                } else {
                    pnlDisplay = '<span class="text-muted">-</span>';
                }
                
                // Get side badge class
                const sideClass = activity.side === 'BUY' ? 'bg-success' : 'bg-danger';
                
                // Order direction (Open/Close)
                const direction = activity.dir || 'Unknown';
                const directionClass = direction === 'Open' ? 'bg-primary' : 'bg-secondary';
                
                row.innerHTML = `
                    <td><small>${formattedTime}</small></td>
                    <td><strong>${activity.instrumentId}</strong></td>
                    <td><span class="badge ${directionClass}">${direction}</span></td>
                    <td><span class="badge ${sideClass}">${activity.side}</span></td>
                    <td>${quantity}</td>
                    <td>$${price}</td>
                    <td>${pnlDisplay}</td>
                    <td><span class="badge ${getBadgeClass(activity.status)}">${activity.status}</span></td>
                `;
                activityTableBody.appendChild(row);
            });
        } catch (error) {
            activityTableBody.innerHTML = `
                <tr>
                    <td colspan="8" class="text-center text-danger">Error loading activity data: ${error.message}</td>
                </tr>
            `;
            console.error('Error fetching activity:', error);
        }
    }

    // Handle order submission
    async function handleOrderSubmit(event) {
        event.preventDefault();
        
        const marketType = document.getElementById('market-type').value;
        const symbol = document.getElementById('trading-pair').value;
        const orderType = document.getElementById('order-type').value;
        const side = document.getElementById('order-side').value;
        const size = parseFloat(document.getElementById('order-size').value);
        const price = orderType === 'limit' ? parseFloat(document.getElementById('limit-price').value) : null;
        const leverage = marketType === 'spot' ? 1 : parseFloat(document.getElementById('leverage').value);

        // Get current price for validation
        try {
            const response = await fetch(`/api/pairs`);
            const data = await response.json();
            const pair = data.pairs.find(p => p.symbol === symbol);
            
            if (!pair) {
                showError('Invalid trading pair', 'Could not find pair information');
                return;
            }

            // Calculate order value
            const firstAsk = document.querySelector('.order-book .asks > div')?.dataset.price;
            const firstBid = document.querySelector('.order-book .bids > div')?.dataset.price;
            const currentPrice = price || (side === 'buy' ? 
                parseFloat(firstAsk || '0') : 
                parseFloat(firstBid || '0'));
            const orderValue = size * currentPrice;

            if (orderValue < 10) {
                showError('Order value too low', `Order value ($${orderValue.toFixed(2)}) is below minimum required value of $10`);
                return;
            }

            // Validate leverage doesn't exceed maximum for selected pair
            if (marketType === 'perpetual' && pairLeverageMap[symbol]) {
                const maxLeverage = pairLeverageMap[symbol];
                if (leverage > maxLeverage) {
                    showError('Leverage too high', `Maximum leverage for ${symbol} is ${maxLeverage}x. You entered ${leverage}x.`);
                    return;
                }
            }

            const formData = {
                pair: symbol,
                market_type: marketType,
                type: orderType,
                side: side,
                leverage: leverage,
                size: size,
                price: price
            };
            
            const orderResponse = await fetch('/api/order', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(formData)
            });
            
            const result = await orderResponse.json();
            
            if (result.success) {
                // Add to activity table
                addActivity({
                    type: 'order_placed',
                    message: `Placed ${side} ${marketType} order for ${size} ${symbol} (Value: $${orderValue.toFixed(2)})`,
                    data: {
                        ...formData,
                        value: orderValue
                    }
                });
                
                // Show success message
                showAlert(`Order placed successfully! Value: $${orderValue.toFixed(2)}`, 'success');
                
                // Clear form
                event.target.reset();
                document.getElementById('limit-price-container').style.display = 'none';
                document.getElementById('leverage-container').style.display = marketType === 'spot' ? 'none' : 'block';
            } else {
                showError('Order placement failed', result.error || 'Unknown error');
            }
        } catch (error) {
            showError('Failed to place order', error.message || 'Unknown error');
        }
    }

    // Helper function to format dates
    function formatDate(timestamp) {
        const date = new Date(timestamp);
        return date.toLocaleString();
    }

    // Helper function to get badge class based on status
    function getBadgeClass(status) {
        switch (status.toLowerCase()) {
            case 'filled':
            case 'success':
                return 'bg-success';
            case 'pending':
            case 'processing':
                return 'bg-warning text-dark';
            case 'canceled':
                return 'bg-secondary';
            case 'rejected':
            case 'failed':
                return 'bg-danger';
            default:
                return 'bg-info';
        }
    }

    // Show alert message (using global showAlert function)

    // Trading Section Functions
    let availablePairs = [];
    let pairLeverageMap = {}; // Maps symbol to maxLeverage
    let selectedPair = null;
    let orderBook = { asks: [], bids: [] };
    let recentTrades = [];

    // Initialize trading section
    function initTradingSection() {
        // Load available trading pairs
        fetchAvailablePairs();
        
        // Set up event listeners
        document.getElementById('order-type').addEventListener('change', handleOrderTypeChange);
        document.getElementById('market-type').addEventListener('change', handleMarketTypeChange);
        document.getElementById('trading-form').addEventListener('submit', handleOrderSubmit);
        document.getElementById('trading-pair').addEventListener('change', handlePairChange);
        
        // Set up leverage limit updates when pair changes
        document.getElementById('trading-pair').addEventListener('change', updateLeverageLimits);
        
        // Set up position value display updates
        document.getElementById('order-size').addEventListener('input', updatePositionValueDisplay);
        document.getElementById('leverage').addEventListener('input', updatePositionValueDisplay);
        document.getElementById('trading-pair').addEventListener('change', updatePositionValueDisplay);
        
        // Set up position management buttons
        const closeAllBtn = document.getElementById('close-all-positions');
        if (closeAllBtn) {
            closeAllBtn.addEventListener('click', closeAllPositions);
        }
        
        // Initialize leverage limits on page load
        setTimeout(() => {
            updateLeverageLimits(); // Call after pairs are loaded
        }, 1000);
        
        // Start WebSocket connection for real-time data
        if (socket.connected) {
            initTradingWebSocket();
        } else {
            socket.on('connect', () => {
                initTradingWebSocket();
            });
        }
    }

    // Fetch available trading pairs
    async function fetchAvailablePairs() {
        try {
            const response = await fetch('/api/pairs');
            const data = await response.json();
            availablePairs = data.pairs;
            
            // Build leverage map for quick lookup
            pairLeverageMap = {};
            availablePairs.forEach(pair => {
                pairLeverageMap[pair.symbol] = pair.maxLeverage || 20; // Default to 20x if not specified
            });
            
            console.log('📊 Loaded leverage limits:', pairLeverageMap);
            
            const pairSelect = document.getElementById('trading-pair');
            pairSelect.innerHTML = '<option value="">Select Pair</option>';
            
            availablePairs.forEach(pair => {
                const option = document.createElement('option');
                option.value = pair.symbol;
                option.textContent = `${pair.symbol} (Max: ${pair.maxLeverage || 20}x)`;
                pairSelect.appendChild(option);
            });
            
            // Set initial leverage limit if a pair is already selected
            if (pairSelect.value) {
                updateLeverageLimits();
            }
        } catch (error) {
            showError('Failed to load trading pairs', error);
        }
    }

    // Update leverage limits based on selected pair
    function updateLeverageLimits() {
        const selectedSymbol = document.getElementById('trading-pair').value;
        const leverageInput = document.getElementById('leverage');
        
        if (selectedSymbol && pairLeverageMap[selectedSymbol]) {
            const maxLeverage = pairLeverageMap[selectedSymbol];
            
            // Update leverage input attributes
            leverageInput.max = maxLeverage;
            leverageInput.title = `Maximum leverage for ${selectedSymbol}: ${maxLeverage}x`;
            
            // If current value exceeds max, reset to max
            if (parseInt(leverageInput.value) > maxLeverage) {
                leverageInput.value = maxLeverage;
            }
            
            // Update placeholder text
            leverageInput.placeholder = `1-${maxLeverage}x`;
            
            console.log(`📊 Updated leverage limit for ${selectedSymbol}: ${maxLeverage}x`);
            
            // Show leverage info in UI
            const leverageLabel = document.querySelector('label[for="leverage"]');
            if (leverageLabel) {
                leverageLabel.textContent = `Leverage (Max: ${maxLeverage}x)`;
            }
            
            // Update leverage change handler to show position value calculation
            leverageInput.addEventListener('input', updatePositionValueDisplay);
        } else {
            // Reset to default if no pair selected - set a high default to avoid blocking
            leverageInput.max = 100; // High default to avoid HTML5 validation blocking
            leverageInput.title = 'Select a trading pair to see max leverage';
            leverageInput.placeholder = '1x';
            
            const leverageLabel = document.querySelector('label[for="leverage"]');
            if (leverageLabel) {
                leverageLabel.textContent = 'Leverage';
            }
        }
    }
    
    // Update position value display to show leverage calculation
    function updatePositionValueDisplay() {
        const sizeInput = document.getElementById('order-size');
        const leverageInput = document.getElementById('leverage');
        const selectedSymbol = document.getElementById('trading-pair').value;
        
        const marginAmount = parseFloat(sizeInput.value) || 0;
        const leverage = parseFloat(leverageInput.value) || 1;
        const positionValue = marginAmount * leverage;
        
        // Find or create position info display
        let positionInfo = document.getElementById('position-value-info');
        if (!positionInfo) {
            positionInfo = document.createElement('div');
            positionInfo.id = 'position-value-info';
            positionInfo.className = 'mt-2 text-info small';
            
            // Add after leverage container
            const leverageContainer = document.getElementById('leverage-container');
            leverageContainer.appendChild(positionInfo);
        }
        
        if (marginAmount > 0 && leverage > 1 && selectedSymbol) {
            positionInfo.innerHTML = `
                <i class="bi bi-info-circle"></i> 
                Position: $${marginAmount} margin × ${leverage}x leverage = $${positionValue.toFixed(2)} ${selectedSymbol} position
            `;
            positionInfo.style.display = 'block';
        } else if (marginAmount > 0 && selectedSymbol) {
            positionInfo.innerHTML = `
                <i class="bi bi-info-circle"></i> 
                Position: $${marginAmount} ${selectedSymbol} (no leverage)
            `;
            positionInfo.style.display = 'block';
        } else {
            positionInfo.style.display = 'none';
        }
    }

    // Handle order type change
    function handleOrderTypeChange(event) {
        const limitPriceContainer = document.getElementById('limit-price-container');
        limitPriceContainer.style.display = event.target.value === 'limit' ? 'block' : 'none';
    }

    // Handle market type change
    function handleMarketTypeChange(event) {
        const marketType = event.target.value;
        const leverageContainer = document.getElementById('leverage-container');
        const leverageInput = document.getElementById('leverage');
        
        if (marketType === 'spot') {
            leverageContainer.style.display = 'none';
            leverageInput.value = '1';  // Force leverage to 1 for spot
        } else {
            leverageContainer.style.display = 'block';
        }
    }

    // Handle pair selection change
    function handlePairChange(event) {
        selectedPair = event.target.value;
        if (selectedPair) {
            // Subscribe to order book and trades for selected pair
            subscribeToPair(selectedPair);
        }
    }

    // Initialize WebSocket connection for trading data
    function initTradingWebSocket() {
        socket.on('orderbook_update', (data) => {
            if (data.symbol === selectedPair) {
                updateOrderBook(data);
            }
        });
        
        socket.on('trade_update', (data) => {
            if (data.symbol === selectedPair) {
                updateRecentTrades(data);
            }
        });
    }

    // Update order book display
    function updateOrderBook(data) {
        orderBook = data;
        
        const asksDiv = document.querySelector('.order-book .asks');
        const bidsDiv = document.querySelector('.order-book .bids');
        const spreadSpan = document.getElementById('price-spread');
        
        // Update asks
        asksDiv.innerHTML = data.asks.slice(0, 10).map(ask => `
            <div class="d-flex justify-content-between text-danger" data-price="${ask.price}" data-size="${ask.size}">
                <span>${ask.price}</span>
                <span>${ask.size}</span>
            </div>
        `).join('');
        
        // Update bids
        bidsDiv.innerHTML = data.bids.slice(0, 10).map(bid => `
            <div class="d-flex justify-content-between text-success" data-price="${bid.price}" data-size="${bid.size}">
                <span>${bid.price}</span>
                <span>${bid.size}</span>
            </div>
        `).join('');
        
        // Update spread
        if (data.asks.length > 0 && data.bids.length > 0) {
            const spread = data.asks[0].price - data.bids[0].price;
            spreadSpan.textContent = spread.toFixed(4);
        }
    }

    // Update recent trades display
    function updateRecentTrades(data) {
        recentTrades.unshift(data);
        if (recentTrades.length > 20) recentTrades.pop();
        
        const tradesDiv = document.querySelector('.recent-trades');
        tradesDiv.innerHTML = recentTrades.map(trade => `
            <div class="d-flex justify-content-between ${trade.side === 'buy' ? 'text-success' : 'text-danger'}">
                <span>${trade.price}</span>
                <span>${trade.size}</span>
                <small>${new Date(trade.time).toLocaleTimeString()}</small>
            </div>
        `).join('');
    }

    // Subscribe to pair updates
    function subscribeToPair(symbol) {
        socket.emit('subscribe', { symbol });
    }

    // Webhook functionality
    const copyWebhookUrlBtn = document.getElementById('copy-webhook-url');
    const testWebhookBtn = document.getElementById('test-webhook');
    const testSymbol = document.getElementById('test-symbol');
    const testSide = document.getElementById('test-side');
    const webhookHistory = document.getElementById('webhook-history');

    // Copy webhook URL to clipboard
    if (copyWebhookUrlBtn) {
        copyWebhookUrlBtn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(webhookUrl.value);
                showAlert('Webhook URL copied to clipboard', 'success');
            } catch (err) {
                showAlert('Failed to copy webhook URL', 'danger');
                console.error('Failed to copy webhook URL:', err);
            }
        });
    }

    // Test webhook functionality (old button)
    if (testWebhookBtn) {
        testWebhookBtn.addEventListener('click', async () => {
            try {
                const testPayload = {
                    symbol: testSymbol.value,
                    side: testSide.value,
                    leverage: 10,
                    stop_loss_percent: 2,
                    take_profit_percent: 5,
                    rsi: 30,
                    macd: -0.5,
                    signal: -0.3,
                    adx: 25,
                    bb_upper: testSide.value === 'SELL' ? 50000 : 0,  // Buy short when SELL
                    bb_middle: 0,
                    bb_lower: testSide.value === 'BUY' ? 46000 : 0,   // Buy long when BUY
                    ema_fast: 48500,
                    ema_slow: 47500,
                    volume: 1000,
                    vol_ma: 800,
                    atr: 500
                };

                const response = await fetch('/webhook', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(testPayload)
                });

                const result = await response.json();
                
                if (response.ok) {
                    showAlert('Test webhook sent successfully', 'success');
                    addWebhookToHistory(testPayload, 'success', result);
                } else {
                    throw new Error(result.error || 'Failed to send test webhook');
                }
            } catch (error) {
                console.error('Error sending test webhook:', error);
                showAlert(error.message, 'danger');
                addWebhookToHistory(testPayload, 'error', { error: error.message });
            }
        });
    }

    // New test webhook button (new button)
    const newTestWebhookBtn = document.getElementById('new-test-webhook');
    if (newTestWebhookBtn) {
        newTestWebhookBtn.addEventListener('click', async () => {
            console.log("New test webhook button clicked");
            try {
                const testPayload = {
                    symbol: testSymbol.value,
                    side: testSide.value,
                    leverage: 10,
                    stop_loss_percent: 2,
                    take_profit_percent: 5,
                    rsi: 30,
                    macd: -0.5,
                    signal: -0.3,
                    adx: 25,
                    bb_upper: testSide.value === 'SELL' ? 50000 : 0,  // Buy short when SELL
                    bb_middle: 0,
                    bb_lower: testSide.value === 'BUY' ? 46000 : 0,   // Buy long when BUY
                    ema_fast: 48500,
                    ema_slow: 47500,
                    volume: 1000,
                    vol_ma: 800,
                    atr: 500
                };

                const response = await fetch('/webhook', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(testPayload)
                });

                const result = await response.json();
                if (response.ok) {
                    showAlert("New test webhook sent successfully", "success");
                    addWebhookToHistory(testPayload, "success", result);
                } else {
                    throw new Error(result.error || "Failed to send new test webhook");
                }
            } catch (error) {
                console.error("Error sending new test webhook:", error);
                showAlert(error.message, "danger");
                addWebhookToHistory(testPayload, "error", { error: error.message });
            }
        });
    }

    // Add webhook to history
    function addWebhookToHistory(webhook, status, result) {
        const now = new Date();
        const row = document.createElement('tr');
        
        // Format the details for display
        const details = {
            leverage: webhook.leverage,
            stop_loss: `${webhook.stop_loss_percent}%`,
            take_profit: `${webhook.take_profit_percent}%`,
            technical_indicators: {
                rsi: webhook.rsi,
                macd: webhook.macd,
                signal: webhook.signal,
                adx: webhook.adx
            }
        };

        row.innerHTML = `
            <td>${formatDate(now)}</td>
            <td>${webhook.symbol}</td>
            <td>${webhook.side}</td>
            <td>${webhook.leverage}x</td>
            <td><span class="badge ${status === 'success' ? 'bg-success' : 'bg-danger'}">${status}</span></td>
            <td>
                <button class="btn btn-sm btn-outline-secondary" onclick="showWebhookDetails(this)" 
                    data-details='${JSON.stringify(details)}'
                    data-result='${JSON.stringify(result)}'>
                    View Details
                </button>
            </td>
        `;

        // Add to the top of the table
        const tbody = webhookHistory;
        if (tbody.firstElementChild && tbody.firstElementChild.cells.length === 1) {
            tbody.innerHTML = ''; // Clear the "No webhook history" message
        }
        tbody.insertBefore(row, tbody.firstChild);

        // Limit history to 20 entries
        while (tbody.children.length > 20) {
            tbody.removeChild(tbody.lastChild);
        }
    }

    // Show webhook details in a modal
    window.showWebhookDetails = function(button) {
        const details = JSON.parse(button.dataset.details);
        const result = JSON.parse(button.dataset.result);
        
        const modalHtml = `
            <div class="modal fade" id="webhookDetailsModal" tabindex="-1">
                <div class="modal-dialog modal-lg">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title">Webhook Details</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                        </div>
                        <div class="modal-body">
                            <div class="row">
                                <div class="col-md-6">
                                    <h6>Webhook Payload</h6>
                                    <pre class="bg-light p-3 rounded"><code>${JSON.stringify(details, null, 2)}</code></pre>
                                </div>
                                <div class="col-md-6">
                                    <h6>Server Response</h6>
                                    <pre class="bg-light p-3 rounded"><code>${JSON.stringify(result, null, 2)}</code></pre>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;

        // Remove existing modal if any
        const existingModal = document.getElementById('webhookDetailsModal');
        if (existingModal) {
            existingModal.remove();
        }

        // Add new modal to document
        document.body.insertAdjacentHTML('beforeend', modalHtml);

        // Show modal
        const modal = new bootstrap.Modal(document.getElementById('webhookDetailsModal'));
        modal.show();
    };

    // Listen for webhook events from server
    socket.on('webhook_received', (data) => {
        addWebhookToHistory(data.payload, data.status, data.result);
    });

    async function fetchVolume24h() {
        try {
            const response = await fetch('/api/volume24h');
            const data = await response.json();
            const volumeCard = document.getElementById('volume-24h');
            if (volumeCard) {
                if (data.volume24h !== undefined && !data.error) {
                    volumeCard.textContent = `$${parseFloat(data.volume24h).toLocaleString(undefined, {maximumFractionDigits: 2})}`;
                } else {
                    volumeCard.textContent = (data.error) ? data.error : 'No 24h volume available. Please make a trade on Hyperliquid.';
                }
            }
        } catch (e) {
            const volumeCard = document.getElementById('volume-24h');
            if (volumeCard) volumeCard.textContent = 'No 24h volume available. Please make a trade on Hyperliquid.';
        }
    }

    // Call this on page load
    fetchVolume24h();
    
    // Periodically refresh positions (every 60 seconds to reduce API calls)
    setInterval(fetchPositions, 60000);
    
    // Initialize strategies section
    initStrategiesSection();
    
    // Initialize strategy engine
    initStrategyEngine();
    
    // Initialize backtester section
    initBacktesterSection();
    
    // Initialize position charts
    initPositionCharts();

    function updateDateRangeDisplay() {
        const startDateInput = document.getElementById('start-date');
        const endDateInput = document.getElementById('end-date');
        const dateRangeDisplay = document.getElementById('date-range-display');
        
        if (startDateInput && endDateInput && dateRangeDisplay) {
            const startDate = startDateInput.value;
            const endDate = endDateInput.value;
            
            if (startDate && endDate) {
                const start = new Date(startDate).toLocaleDateString();
                const end = new Date(endDate).toLocaleDateString();
                dateRangeDisplay.textContent = `${start} - ${end}`;
            } else {
                dateRangeDisplay.textContent = 'Select date range';
            }
        }
    }

    // News Section Functions
    function initNewsSection() {
        console.log('Initializing news section...');
        
        // Add refresh button listener
        const refreshNewsBtn = document.getElementById('refresh-news');
        if (refreshNewsBtn) {
            refreshNewsBtn.addEventListener('click', () => {
                console.log('Refresh news button clicked');
                fetchNews();
            });
        }
        
        // Initial news fetch
        fetchNews();
        
        // Set up real-time news updates via Socket.IO
        socket.on('news_update', (data) => {
            console.log('Received news update via WebSocket:', data);
            displayNews(data.news, data.last_updated);
        });
    }

    async function fetchNews() {
        console.log('Fetching news...');
        const newsGrid = document.getElementById('news-grid');
        const newsLastUpdated = document.getElementById('news-last-updated');
        
        if (!newsGrid) {
            console.error('News grid element not found');
            return;
        }
        
        // Show loading state
        newsGrid.innerHTML = `
            <div class="news-loading">
                <div class="loading-spinner"></div>
                <p>Loading latest crypto news...</p>
            </div>
        `;
        
        try {
            const response = await fetch('/api/news');
            
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            
            const data = await response.json();
            
            if (data.error) {
                throw new Error(data.error);
            }
            
            console.log('News data received:', data);
            displayNews(data.news, data.last_updated);
            
        } catch (error) {
            console.error('Error fetching news:', error);
            newsGrid.innerHTML = `
                <div class="news-error">
                    <i class="bi bi-exclamation-triangle"></i>
                    <h6>Failed to Load News</h6>
                    <p>${error.message}</p>
                    <button class="btn btn-outline-primary btn-sm" onclick="fetchNews()">
                        <i class="bi bi-arrow-clockwise"></i>
                        Try Again
                    </button>
                </div>
            `;
        }
    }

    function displayNews(newsItems, lastUpdated) {
        const newsGrid = document.getElementById('news-grid');
        const newsLastUpdated = document.getElementById('news-last-updated');
        
        if (!newsGrid) {
            console.error('News grid element not found');
            return;
        }
        
        // Update last updated time
        if (newsLastUpdated && lastUpdated) {
            const updateTime = new Date(lastUpdated);
            newsLastUpdated.textContent = `Last updated: ${updateTime.toLocaleTimeString()}`;
        }
        
        // Clear loading state
        newsGrid.innerHTML = '';
        
        if (!newsItems || newsItems.length === 0) {
            newsGrid.innerHTML = `
                <div class="news-error">
                    <i class="bi bi-newspaper"></i>
                    <h6>No News Available</h6>
                    <p>No crypto news found at the moment. Please try again later.</p>
                </div>
            `;
            return;
        }
        
        // Create news items
        newsItems.forEach((item, index) => {
            const newsItem = createNewsItem(item, index);
            newsGrid.appendChild(newsItem);
        });
        
        console.log(`Displayed ${newsItems.length} news items`);
    }

    function createNewsItem(item, index) {
        const newsItem = document.createElement('div');
        newsItem.className = 'news-item';
        newsItem.style.animationDelay = `${index * 0.1}s`;
        
        // Format published time
        let timeDisplay = 'Unknown time';
        if (item.published_at) {
            try {
                const publishedDate = new Date(item.published_at);
                const now = new Date();
                const diffMs = now - publishedDate;
                const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
                const diffMinutes = Math.floor(diffMs / (1000 * 60));
                
                if (diffHours > 24) {
                    const diffDays = Math.floor(diffHours / 24);
                    timeDisplay = `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;
                } else if (diffHours > 0) {
                    timeDisplay = `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
                } else if (diffMinutes > 0) {
                    timeDisplay = `${diffMinutes} minute${diffMinutes > 1 ? 's' : ''} ago`;
                } else {
                    timeDisplay = 'Just now';
                }
            } catch (e) {
                console.warn('Error parsing date:', item.published_at);
            }
        }
        
        // Create news item HTML
        newsItem.innerHTML = `
            <div class="news-header">
                <h3 class="news-title" onclick="openNewsLink('${item.url}')">${escapeHtml(item.title)}</h3>
                <span class="news-source">${escapeHtml(item.source)}</span>
            </div>
            <div class="news-description">
                ${escapeHtml(item.description)}
            </div>
            <div class="news-footer">
                <div class="news-time">
                    <i class="bi bi-clock"></i>
                    ${timeDisplay}
                </div>
                <a href="${item.url}" target="_blank" rel="noopener noreferrer" class="news-link">
                    Read More
                    <i class="bi bi-arrow-up-right"></i>
                </a>
            </div>
        `;
        
        return newsItem;
    }

    function openNewsLink(url) {
        if (url && url !== '#') {
            window.open(url, '_blank', 'noopener,noreferrer');
        }
    }

    function escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Make fetchNews available globally for error retry buttons
    window.fetchNews = fetchNews;
});

// Strategies functionality
function initStrategiesSection() {
    // Copy strategy code button
    const copyStrategyCodeBtn = document.getElementById('copy-strategy-code');
    if (copyStrategyCodeBtn) {
        copyStrategyCodeBtn.addEventListener('click', () => {
            const codeBlock = document.querySelector('.strategy-code .code-block code');
            if (codeBlock) {
                navigator.clipboard.writeText(codeBlock.textContent)
                    .then(() => {
                        // Show success feedback
                        const originalText = copyStrategyCodeBtn.innerHTML;
                        copyStrategyCodeBtn.innerHTML = '<i class="bi bi-check"></i> Copied!';
                        copyStrategyCodeBtn.classList.add('btn-success-modern');
                        copyStrategyCodeBtn.classList.remove('btn-outline-modern');
                        
                        setTimeout(() => {
                            copyStrategyCodeBtn.innerHTML = originalText;
                            copyStrategyCodeBtn.classList.remove('btn-success-modern');
                            copyStrategyCodeBtn.classList.add('btn-outline-modern');
                        }, 2000);
                    })
                    .catch(err => {
                        console.error('Failed to copy code:', err);
                        showAlert('Failed to copy strategy code', 'danger');
                    });
            }
        });
    }
    
    // Edit strategy parameters button
    const editStrategyBtn = document.getElementById('edit-strategy');
    if (editStrategyBtn) {
        editStrategyBtn.addEventListener('click', () => {
            showStrategyEditor();
        });
    }
    
    // Copy BB strategy code button
    const copyBBStrategyCodeBtn = document.getElementById('copy-bb-strategy-code');
    if (copyBBStrategyCodeBtn) {
        copyBBStrategyCodeBtn.addEventListener('click', () => {
            // Find the BB strategy code block
            const bbCodeBlocks = document.querySelectorAll('.strategy-code .code-block code');
            // The BB strategy is the second code block
            const bbCodeBlock = bbCodeBlocks[1];
            if (bbCodeBlock) {
                navigator.clipboard.writeText(bbCodeBlock.textContent)
                    .then(() => {
                        // Show success feedback
                        const originalText = copyBBStrategyCodeBtn.innerHTML;
                        copyBBStrategyCodeBtn.innerHTML = '<i class="bi bi-check"></i> Copied!';
                        copyBBStrategyCodeBtn.classList.add('btn-success-modern');
                        copyBBStrategyCodeBtn.classList.remove('btn-outline-modern');
                        
                        setTimeout(() => {
                            copyBBStrategyCodeBtn.innerHTML = originalText;
                            copyBBStrategyCodeBtn.classList.remove('btn-success-modern');
                            copyBBStrategyCodeBtn.classList.add('btn-outline-modern');
                        }, 2000);
                    })
                    .catch(err => {
                        console.error('Failed to copy BB strategy code:', err);
                        showAlert('Failed to copy BB strategy code', 'danger');
                    });
            }
        });
    }
    
    // Edit BB strategy parameters button
    const editBBStrategyBtn = document.getElementById('edit-bb-strategy');
    if (editBBStrategyBtn) {
        editBBStrategyBtn.addEventListener('click', () => {
            showBBStrategyEditor();
        });
    }

    // Copy Volume Momentum strategy code button
    const copyVMStrategyCodeBtn = document.getElementById('copy-vm-strategy-code');
    if (copyVMStrategyCodeBtn) {
        copyVMStrategyCodeBtn.addEventListener('click', () => {
            // Find the VM strategy code block
            const vmCodeBlocks = document.querySelectorAll('.strategy-code .code-block code');
            // The VM strategy is the third code block
            const vmCodeBlock = vmCodeBlocks[2];
            if (vmCodeBlock) {
                navigator.clipboard.writeText(vmCodeBlock.textContent)
                    .then(() => {
                        // Show success feedback
                        const originalText = copyVMStrategyCodeBtn.innerHTML;
                        copyVMStrategyCodeBtn.innerHTML = '<i class="bi bi-check"></i> Copied!';
                        copyVMStrategyCodeBtn.classList.add('btn-success-modern');
                        copyVMStrategyCodeBtn.classList.remove('btn-outline-modern');
                        
                        setTimeout(() => {
                            copyVMStrategyCodeBtn.innerHTML = originalText;
                            copyVMStrategyCodeBtn.classList.remove('btn-success-modern');
                            copyVMStrategyCodeBtn.classList.add('btn-outline-modern');
                        }, 2000);
                    })
                    .catch(err => {
                        console.error('Failed to copy VM strategy code:', err);
                        showAlert('Failed to copy VM strategy code', 'danger');
                    });
            }
        });
    }
    
    // Edit Volume Momentum strategy parameters button
    const editVMStrategyBtn = document.getElementById('edit-vm-strategy');
    if (editVMStrategyBtn) {
        editVMStrategyBtn.addEventListener('click', () => {
            showVMStrategyEditor();
        });
    }
    
    // Copy BTC Scalping strategy code button
    const copyBTCScalpingStrategyCodeBtn = document.getElementById('copy-btc-scalping-strategy-code');
    if (copyBTCScalpingStrategyCodeBtn) {
        copyBTCScalpingStrategyCodeBtn.addEventListener('click', () => {
            // Find the BTC Scalping strategy code block
            const btcScalpingCodeBlocks = document.querySelectorAll('.strategy-code .code-block code');
            // The BTC Scalping strategy is the fourth code block
            const btcScalpingCodeBlock = btcScalpingCodeBlocks[3];
            if (btcScalpingCodeBlock) {
                navigator.clipboard.writeText(btcScalpingCodeBlock.textContent)
                    .then(() => {
                        // Show success feedback
                        const originalText = copyBTCScalpingStrategyCodeBtn.innerHTML;
                        copyBTCScalpingStrategyCodeBtn.innerHTML = '<i class="bi bi-check"></i> Copied!';
                        copyBTCScalpingStrategyCodeBtn.classList.add('btn-success-modern');
                        copyBTCScalpingStrategyCodeBtn.classList.remove('btn-outline-modern');
                        
                        setTimeout(() => {
                            copyBTCScalpingStrategyCodeBtn.innerHTML = originalText;
                            copyBTCScalpingStrategyCodeBtn.classList.remove('btn-success-modern');
                            copyBTCScalpingStrategyCodeBtn.classList.add('btn-outline-modern');
                        }, 2000);
                    })
                    .catch(err => {
                        console.error('Failed to copy BTC Scalping strategy code:', err);
                        showAlert('Failed to copy BTC Scalping strategy code', 'danger');
                    });
            }
        });
    }
    
    // Edit BTC Scalping strategy parameters button
    const editBTCScalpingStrategyBtn = document.getElementById('edit-btc-scalping-strategy');
    if (editBTCScalpingStrategyBtn) {
        editBTCScalpingStrategyBtn.addEventListener('click', () => {
            showBTCScalpingStrategyEditor();
        });
    }
}

// Show strategy parameter editor modal
function showStrategyEditor() {
    const modalHtml = `
        <div class="modal fade" id="strategyEditorModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">
                            <i class="bi bi-gear"></i>
                            Edit Strategy Parameters
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <div class="alert alert-info">
                            <i class="bi bi-info-circle"></i>
                            Modify the parameters below and copy the updated Pine Script code to TradingView.
                        </div>
                        
                        <form id="strategy-params-form">
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Length</label>
                                    <input type="number" class="form-control" id="param-rsi-length" value="14" min="1" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Overbought</label>
                                    <input type="number" class="form-control" id="param-rsi-overbought" value="70" min="50" max="100">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Oversold</label>
                                    <input type="number" class="form-control" id="param-rsi-oversold" value="30" min="0" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Fast EMA Length</label>
                                    <input type="number" class="form-control" id="param-ema-fast" value="9" min="1" max="50">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Slow EMA Length</label>
                                    <input type="number" class="form-control" id="param-ema-slow" value="21" min="1" max="100">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">ADX Threshold</label>
                                    <input type="number" class="form-control" id="param-adx-threshold" value="25" min="0" max="100">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Bollinger Bands Length</label>
                                    <input type="number" class="form-control" id="param-bb-length" value="20" min="1" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Bollinger Bands Multiplier</label>
                                    <input type="number" class="form-control" id="param-bb-mult" value="2.0" min="0.1" max="5.0" step="0.1">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Volume Threshold</label>
                                    <input type="number" class="form-control" id="param-vol-threshold" value="1.5" min="0.1" max="5.0" step="0.1">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Default Leverage</label>
                                    <input type="number" class="form-control" id="param-leverage" value="10" min="1" max="20">
                                </div>
                            </div>
                        </form>
                        
                        <div class="mt-4">
                            <h6>Updated Pine Script Code:</h6>
                            <div class="bg-light p-3 rounded" style="max-height: 300px; overflow-y: auto;">
                                <pre><code id="updated-strategy-code">// Code will be generated here...</code></pre>
                            </div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-outline-primary" id="copy-updated-code">
                            <i class="bi bi-clipboard"></i>
                            Copy Updated Code
                        </button>
                        <button type="button" class="btn btn-primary" id="update-strategy-params">
                            <i class="bi bi-save"></i>
                            Update Parameters
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;

    // Remove existing modal if any
    const existingModal = document.getElementById('strategyEditorModal');
    if (existingModal) {
        existingModal.remove();
    }

    // Add modal to document
    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Show modal
    const modal = new bootstrap.Modal(document.getElementById('strategyEditorModal'));
    modal.show();
    
    // Add event listeners
    const updateBtn = document.getElementById('update-strategy-params');
    const copyBtn = document.getElementById('copy-updated-code');
    
    // Generate updated code when parameters change
    const inputs = document.querySelectorAll('#strategy-params-form input');
    inputs.forEach(input => {
        input.addEventListener('input', generateUpdatedStrategyCode);
    });
    
    // Initial code generation
    generateUpdatedStrategyCode();
    
    if (updateBtn) {
        updateBtn.addEventListener('click', () => {
            updateStrategyParameters();
            modal.hide();
        });
    }
    
    if (copyBtn) {
        copyBtn.addEventListener('click', () => {
            const codeElement = document.getElementById('updated-strategy-code');
            if (codeElement) {
                navigator.clipboard.writeText(codeElement.textContent)
                    .then(() => {
                        const originalText = copyBtn.innerHTML;
                        copyBtn.innerHTML = '<i class="bi bi-check"></i> Copied!';
                        copyBtn.classList.add('btn-success');
                        copyBtn.classList.remove('btn-outline-primary');
                        
                        setTimeout(() => {
                            copyBtn.innerHTML = originalText;
                            copyBtn.classList.remove('btn-success');
                            copyBtn.classList.add('btn-outline-primary');
                        }, 2000);
                    })
                    .catch(err => {
                        console.error('Failed to copy updated code:', err);
                        showAlert('Failed to copy updated code', 'danger');
                    });
            }
        });
    }
}

// Generate updated strategy code with new parameters
function generateUpdatedStrategyCode() {
    const params = {
        rsiLength: document.getElementById('param-rsi-length')?.value || 14,
        rsiOverbought: document.getElementById('param-rsi-overbought')?.value || 70,
        rsiOversold: document.getElementById('param-rsi-oversold')?.value || 30,
        emaFast: document.getElementById('param-ema-fast')?.value || 9,
        emaSlow: document.getElementById('param-ema-slow')?.value || 21,
        adxThreshold: document.getElementById('param-adx-threshold')?.value || 25,
        bbLength: document.getElementById('param-bb-length')?.value || 20,
        bbMult: document.getElementById('param-bb-mult')?.value || 2.0,
        volThreshold: document.getElementById('param-vol-threshold')?.value || 1.5,
        leverage: document.getElementById('param-leverage')?.value || 10
    };
    
    const updatedCode = `// @version=6
// Enhanced AI-powered trading signal generator - CUSTOMIZED
indicator("OpenAI Signal Generator - Enhanced Accuracy", overlay=true)

// Configuration
signal_frequency = input.string("Daily", "Signal Check Frequency", 
    options=["Daily", "Weekly", "4-Hour", "Hourly", "On Every Close"])
enable_signals = input.bool(true, "Enable Signal Generation")

// RSI - CUSTOMIZED PARAMETERS
rsi_length = input.int(${params.rsiLength}, "RSI Length", minval=1)
rsi_value = ta.rsi(close, rsi_length)
rsi_overbought = input.int(${params.rsiOverbought}, "RSI Overbought Level", minval=50, maxval=100)
rsi_oversold = input.int(${params.rsiOversold}, "RSI Oversold Level", minval=0, maxval=50)

// EMAs for crossover confirmation - CUSTOMIZED PARAMETERS
ema_fast_length = input.int(${params.emaFast}, "Fast EMA Length", minval=1)
ema_slow_length = input.int(${params.emaSlow}, "Slow EMA Length", minval=1)
ema_fast = ta.ema(close, ema_fast_length)
ema_slow = ta.ema(close, ema_slow_length)

// MACD
[macd_line, signal_line, histogram] = ta.macd(close, 12, 26, 9)

// Bollinger Bands - CUSTOMIZED PARAMETERS
[bb_middle, bb_upper, bb_lower] = ta.bb(close, ${params.bbLength}, ${params.bbMult})

// ADX for trend strength - CUSTOMIZED PARAMETERS
[diplus, diminus, adx_value] = ta.dmi(14, 1)
adx_threshold = ${params.adxThreshold}

// Volume confirmation - CUSTOMIZED PARAMETERS
vol_ma = ta.sma(volume, 20)
vol_threshold = ${params.volThreshold}
high_volume = volume > vol_ma * vol_threshold

// Enhanced buy/sell signals
buy_signal = ta.crossover(rsi_value, rsi_oversold) and 
             close < bb_lower and histogram > histogram[1] and
             ema_fast > ema_slow and adx_value > adx_threshold and high_volume

sell_signal = ta.crossunder(rsi_value, rsi_overbought) and 
              close > bb_upper and histogram < histogram[1] and
              ema_fast < ema_slow and adx_value > adx_threshold and high_volume

// Alert generation with JSON payload - CUSTOMIZED LEVERAGE
generate_message() =>
    message = "{\\"\\n"
    message := message + "  \\"symbol\\": \\"" + syminfo.ticker + "\\",\\n"
    message := message + "  \\"side\\": \\"" + (buy_signal ? "BUY" : "SELL") + "\\",\\n"
    message := message + "  \\"rsi\\": " + str.tostring(rsi_value, "#.##") + ",\\n"
    message := message + "  \\"macd\\": " + str.tostring(macd_line, "#.##") + ",\\n"
    message := message + "  \\"signal\\": " + str.tostring(signal_line, "#.##") + ",\\n"
    message := message + "  \\"adx\\": " + str.tostring(adx_value, "#.##") + ",\\n"
    message := message + "  \\"leverage\\": ${params.leverage},\\n"
    message := message + "  \\"stop_loss_percent\\": 2,\\n"
    message := message + "  \\"take_profit_percent\\": 5\\n"
    message := message + "}"
    message

if enable_signals and (buy_signal or sell_signal)
    alert(generate_message(), alert.freq_once_per_bar_close)

// Plot indicators
plot(ema_fast, "Fast EMA", color=color.yellow)
plot(ema_slow, "Slow EMA", color=color.purple)
plot(bb_upper, "BB Upper", color=color.green)
plot(bb_lower, "BB Lower", color=color.green)

plotshape(buy_signal, "Buy", location.belowbar, color.green, shape.triangleup)
plotshape(sell_signal, "Sell", location.abovebar, color.red, shape.triangledown)`;

    const codeElement = document.getElementById('updated-strategy-code');
    if (codeElement) {
        codeElement.textContent = updatedCode;
    }
}

// Update strategy parameters in the UI
function updateStrategyParameters() {
    const params = {
        rsiLength: document.getElementById('param-rsi-length')?.value || 14,
        rsiOverbought: document.getElementById('param-rsi-overbought')?.value || 70,
        rsiOversold: document.getElementById('param-rsi-oversold')?.value || 30,
        emaFast: document.getElementById('param-ema-fast')?.value || 9,
        emaSlow: document.getElementById('param-ema-slow')?.value || 21,
        adxThreshold: document.getElementById('param-adx-threshold')?.value || 25,
        bbLength: document.getElementById('param-bb-length')?.value || 20,
        bbMult: document.getElementById('param-bb-mult')?.value || 2.0,
        volThreshold: document.getElementById('param-vol-threshold')?.value || 1.5,
        leverage: document.getElementById('param-leverage')?.value || 10
    };
    
    // Update the parameter display in the main strategies section
    const parameterItems = document.querySelectorAll('.parameter-item');
    parameterItems.forEach(item => {
        const label = item.querySelector('label').textContent;
        const span = item.querySelector('span');
        
        switch(label) {
            case 'RSI Length:':
                span.textContent = params.rsiLength;
                break;
            case 'RSI Overbought:':
                span.textContent = params.rsiOverbought;
                break;
            case 'RSI Oversold:':
                span.textContent = params.rsiOversold;
                break;
            case 'Fast EMA:':
                span.textContent = params.emaFast;
                break;
            case 'Slow EMA:':
                span.textContent = params.emaSlow;
                break;
            case 'ADX Threshold:':
                span.textContent = params.adxThreshold;
                break;
            case 'BB Length:':
                span.textContent = params.bbLength;
                break;
            case 'BB Multiplier:':
                span.textContent = params.bbMult;
                break;
        }
    });
    
    showAlert('Strategy parameters updated successfully!', 'success');
}

// Show BB strategy parameter editor modal
function showBBStrategyEditor() {
    const modalHtml = `
        <div class="modal fade" id="bbStrategyEditorModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">
                            <i class="bi bi-gear"></i>
                            Edit BB Strategy Parameters
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <div class="alert alert-info">
                            <i class="bi bi-info-circle"></i>
                            Customize the Enhanced BB Strategy parameters and copy the updated Pine Script code to TradingView.
                        </div>
                        
                        <form id="bb-strategy-params-form">
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Bollinger Bands Length</label>
                                    <input type="number" class="form-control" id="bb-param-bb-length" value="20" min="1" max="100">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Bollinger Bands Multiplier</label>
                                    <input type="number" class="form-control" id="bb-param-bb-mult" value="2.0" min="0.1" max="5.0" step="0.1">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Length</label>
                                    <input type="number" class="form-control" id="bb-param-rsi-length" value="14" min="1" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Overbought</label>
                                    <input type="number" class="form-control" id="bb-param-rsi-overbought" value="65" min="50" max="100">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Oversold</label>
                                    <input type="number" class="form-control" id="bb-param-rsi-oversold" value="35" min="0" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Trend SMA Length</label>
                                    <input type="number" class="form-control" id="bb-param-sma-length" value="50" min="1" max="200">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Leverage</label>
                                    <input type="number" class="form-control" id="bb-param-leverage" value="40" min="1" max="100">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Stop Loss %</label>
                                    <input type="number" class="form-control" id="bb-param-stop-loss" value="10.0" min="1.0" max="50.0" step="0.1">
                                </div>
                            </div>
                        </form>
                        
                        <div class="mt-4">
                            <h6>Updated BB Strategy Pine Script Code:</h6>
                            <div class="bg-light p-3 rounded" style="max-height: 300px; overflow-y: auto;">
                                <pre><code id="updated-bb-strategy-code">// Code will be generated here...</code></pre>
                            </div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-outline-primary" id="copy-updated-bb-code">
                            <i class="bi bi-clipboard"></i>
                            Copy Updated Code
                        </button>
                        <button type="button" class="btn btn-primary" id="update-bb-strategy-params">
                            <i class="bi bi-save"></i>
                            Update Parameters
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;

    // Remove existing modal if any
    const existingModal = document.getElementById('bbStrategyEditorModal');
    if (existingModal) {
        existingModal.remove();
    }

    // Add modal to document
    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Show modal
    const modal = new bootstrap.Modal(document.getElementById('bbStrategyEditorModal'));
    modal.show();
    
    // Add event listeners
    const updateBtn = document.getElementById('update-bb-strategy-params');
    const copyBtn = document.getElementById('copy-updated-bb-code');
    
    // Generate updated code when parameters change
    const inputs = document.querySelectorAll('#bb-strategy-params-form input');
    inputs.forEach(input => {
        input.addEventListener('input', generateUpdatedBBStrategyCode);
    });
    
    // Initial code generation
    generateUpdatedBBStrategyCode();
    
    if (updateBtn) {
        updateBtn.addEventListener('click', () => {
            updateBBStrategyParameters();
            modal.hide();
        });
    }
    
    if (copyBtn) {
        copyBtn.addEventListener('click', () => {
            const codeElement = document.getElementById('updated-bb-strategy-code');
            if (codeElement) {
                navigator.clipboard.writeText(codeElement.textContent)
                    .then(() => {
                        const originalText = copyBtn.innerHTML;
                        copyBtn.innerHTML = '<i class="bi bi-check"></i> Copied!';
                        copyBtn.classList.add('btn-success');
                        copyBtn.classList.remove('btn-outline-primary');
                        
                        setTimeout(() => {
                            copyBtn.innerHTML = originalText;
                            copyBtn.classList.remove('btn-success');
                            copyBtn.classList.add('btn-outline-primary');
                        }, 2000);
                    })
                    .catch(err => {
                        console.error('Failed to copy updated BB code:', err);
                        showAlert('Failed to copy updated BB code', 'danger');
                    });
            }
        });
    }
}

// Generate updated BB strategy code with new parameters
function generateUpdatedBBStrategyCode() {
    const params = {
        bbLength: document.getElementById('bb-param-bb-length')?.value || 20,
        bbMult: document.getElementById('bb-param-bb-mult')?.value || 2.0,
        rsiLength: document.getElementById('bb-param-rsi-length')?.value || 14,
        rsiOverbought: document.getElementById('bb-param-rsi-overbought')?.value || 65,
        rsiOversold: document.getElementById('bb-param-rsi-oversold')?.value || 35,
        smaLength: document.getElementById('bb-param-sma-length')?.value || 50,
        leverage: document.getElementById('bb-param-leverage')?.value || 40,
        stopLoss: document.getElementById('bb-param-stop-loss')?.value || 10.0
    };
    
    const updatedCode = `//@version=5
strategy("Enhanced BB Strategy - High Win Rate - CUSTOMIZED", overlay=true, 
         initial_capital=10000, default_qty_type=strategy.percent_of_equity, 
         default_qty_value=10)

// ===== INPUTS - CUSTOMIZED =====
// Bollinger Bands
bb_length = input.int(${params.bbLength}, "Bollinger Bands Length", minval=1)
bb_mult = input.float(${params.bbMult}, "Bollinger Bands Multiplier", minval=0.1, step=0.1)

// RSI Settings
rsi_length = input.int(${params.rsiLength}, "RSI Length", minval=1)
rsi_overbought = input.int(${params.rsiOverbought}, "RSI Overbought Level", minval=50, maxval=100)
rsi_oversold = input.int(${params.rsiOversold}, "RSI Oversold Level", minval=0, maxval=50)

// Trend Filter
sma_trend_length = input.int(${params.smaLength}, "Trend SMA Length", minval=1)

// Trading Settings
leverage = input.int(${params.leverage}, "Leverage", minval=1, maxval=100)
stop_loss_percent = input.float(${params.stopLoss}, "Stop Loss %", minval=1.0, maxval=50.0)

// ===== INDICATORS =====
// Bollinger Bands
basis = ta.sma(close, bb_length)
dev = bb_mult * ta.stdev(close, bb_length)
bb_upper = basis + dev
bb_lower = basis - dev

// RSI
rsi = ta.rsi(close, rsi_length)

// Trend Filter
sma_50 = ta.sma(close, sma_trend_length)

// Previous close values for bounce detection
prev_close = close[1]
prev_close_2 = close[2]

// ===== ENHANCED STRATEGY LOGIC =====
// Long Entry: Enhanced bounce detection + Trend + RSI
price_bounce_lower = (prev_close_2 > bb_lower[2]) and 
                     (prev_close <= bb_lower[1]) and (close > prev_close)
uptrend = close > sma_50
rsi_not_overbought = rsi < rsi_overbought
long_entry = price_bounce_lower and uptrend and rsi_not_overbought

// Short Entry: Enhanced bounce detection + Trend + RSI
price_bounce_upper = (prev_close_2 < bb_upper[2]) and 
                     (prev_close >= bb_upper[1]) and (close < prev_close)
downtrend = close < sma_50
rsi_not_oversold = rsi > rsi_oversold
short_entry = price_bounce_upper and downtrend and rsi_not_oversold

// Dynamic Exit Conditions
long_exit = ta.crossover(close, basis) or rsi > 70 or close < sma_50
short_exit = ta.crossunder(close, basis) or rsi < 30 or close > sma_50

// ===== STRATEGY EXECUTION =====
if long_entry
    strategy.entry("Long", strategy.long)
if long_exit
    strategy.close("Long")

if short_entry
    strategy.entry("Short", strategy.short)
if short_exit
    strategy.close("Short")

// Dynamic Stop Losses
if strategy.position_size > 0
    strategy.exit("Long SL", "Long", stop=close * (1 - stop_loss_percent / 100))
if strategy.position_size < 0
    strategy.exit("Short SL", "Short", stop=close * (1 + stop_loss_percent / 100))

// ===== VISUAL ELEMENTS =====
plot(bb_upper, color=color.blue, title="BB Upper", linewidth=2)
plot(basis, color=color.orange, title="BB Middle", linewidth=2)
plot(bb_lower, color=color.blue, title="BB Lower", linewidth=2)
fill(plot(bb_upper), plot(bb_lower), color=color.new(color.blue, 95))

plot(sma_50, color=color.red, title="Trend SMA", linewidth=3)

plotshape(long_entry, title="Buy Signal", location=location.belowbar, 
          style=shape.labelup, size=size.large, 
          color=color.green, textcolor=color.white, text="BUY")

plotshape(short_entry, title="Sell Signal", location=location.abovebar, 
          style=shape.labeldown, size=size.large, 
          color=color.red, textcolor=color.white, text="SELL")

bgcolor(uptrend ? color.new(color.green, 95) : color.new(color.red, 95))

// ===== ALERTS =====
alertcondition(long_entry, title="BB Long Entry", 
               message="Enhanced BB Strategy - BUY Signal: Bounce + Trend + RSI")
alertcondition(short_entry, title="BB Short Entry", 
               message="Enhanced BB Strategy - SELL Signal: Bounce + Trend + RSI")
alertcondition(long_exit, title="BB Long Exit", 
               message="Enhanced BB Strategy - EXIT LONG")
alertcondition(short_exit, title="BB Short Exit", 
               message="Enhanced BB Strategy - EXIT SHORT")`;

    const codeElement = document.getElementById('updated-bb-strategy-code');
    if (codeElement) {
        codeElement.textContent = updatedCode;
    }
}

// Update BB strategy parameters in the UI
function updateBBStrategyParameters() {
    const params = {
        bbLength: document.getElementById('bb-param-bb-length')?.value || 20,
        bbMult: document.getElementById('bb-param-bb-mult')?.value || 2.0,
        rsiLength: document.getElementById('bb-param-rsi-length')?.value || 14,
        rsiOverbought: document.getElementById('bb-param-rsi-overbought')?.value || 65,
        rsiOversold: document.getElementById('bb-param-rsi-oversold')?.value || 35,
        smaLength: document.getElementById('bb-param-sma-length')?.value || 50,
        leverage: document.getElementById('bb-param-leverage')?.value || 40,
        stopLoss: document.getElementById('bb-param-stop-loss')?.value || 10.0
    };
    
    // Update the parameter display in the BB strategy card
    const bbStrategyCards = document.querySelectorAll('.strategy-card');
    const bbStrategyCard = bbStrategyCards[1]; // Second strategy card is BB strategy
    
    if (bbStrategyCard) {
        const parameterItems = bbStrategyCard.querySelectorAll('.parameter-item');
        parameterItems.forEach(item => {
            const label = item.querySelector('label').textContent;
            const span = item.querySelector('span');
            
            switch(label) {
                case 'BB Length:':
                    span.textContent = params.bbLength;
                    break;
                case 'BB Multiplier:':
                    span.textContent = params.bbMult;
                    break;
                case 'RSI Length:':
                    span.textContent = params.rsiLength;
                    break;
                case 'RSI Overbought:':
                    span.textContent = params.rsiOverbought;
                    break;
                case 'RSI Oversold:':
                    span.textContent = params.rsiOversold;
                    break;
                case 'Trend SMA:':
                    span.textContent = params.smaLength;
                    break;
                case 'Leverage:':
                    span.textContent = params.leverage + 'x';
                    break;
                case 'Stop Loss:':
                    span.textContent = params.stopLoss + '%';
                    break;
            }
        });
    }
    
    showAlert('BB Strategy parameters updated successfully!', 'success');
}

// Show BTC Scalping strategy parameter editor modal
function showBTCScalpingStrategyEditor() {
    const modalHtml = `
        <div class="modal fade" id="btcScalpingStrategyEditorModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">
                            <i class="bi bi-lightning-charge"></i>
                            Edit BTC Scalping Strategy Parameters
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <div class="alert alert-info">
                            <i class="bi bi-info-circle"></i>
                            Customize the BTC Futures Scalping Strategy parameters and copy the updated Pine Script code to TradingView.
                        </div>
                        
                        <form id="btc-scalping-strategy-params-form">
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Fast SMA Length</label>
                                    <input type="number" class="form-control" id="btc-param-fast-sma" value="5" min="1" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Slow SMA Length</label>
                                    <input type="number" class="form-control" id="btc-param-slow-sma" value="13" min="1" max="100">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Length</label>
                                    <input type="number" class="form-control" id="btc-param-rsi-length" value="7" min="1" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Overbought</label>
                                    <input type="number" class="form-control" id="btc-param-rsi-overbought" value="70" min="50" max="100">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">RSI Oversold</label>
                                    <input type="number" class="form-control" id="btc-param-rsi-oversold" value="30" min="0" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">ATR Length</label>
                                    <input type="number" class="form-control" id="btc-param-atr-length" value="5" min="1" max="50">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Volume Threshold</label>
                                    <input type="number" class="form-control" id="btc-param-volume-threshold" value="1.0" min="0.5" max="3.0" step="0.1">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Start Hour (UTC)</label>
                                    <input type="number" class="form-control" id="btc-param-start-hour" value="0" min="0" max="23">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">End Hour (UTC)</label>
                                    <input type="number" class="form-control" id="btc-param-end-hour" value="23" min="0" max="23">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Use Time Filter</label>
                                    <select class="form-control" id="btc-param-use-time-filter">
                                        <option value="true">Yes</option>
                                        <option value="false">No</option>
                                    </select>
                                </div>
                            </div>
                        </form>
                        
                        <div class="mt-4">
                            <h6>Updated BTC Scalping Strategy Pine Script Code:</h6>
                            <div class="bg-light p-3 rounded" style="max-height: 300px; overflow-y: auto;">
                                <pre><code id="updated-btc-scalping-strategy-code">// Code will be generated here...</code></pre>
                            </div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-outline-primary" id="copy-updated-btc-scalping-code">
                            <i class="bi bi-clipboard"></i>
                            Copy Updated Code
                        </button>
                        <button type="button" class="btn btn-primary" id="update-btc-scalping-strategy-params">
                            <i class="bi bi-save"></i>
                            Update Parameters
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;

    // Remove existing modal if any
    const existingModal = document.getElementById('btcScalpingStrategyEditorModal');
    if (existingModal) {
        existingModal.remove();
    }

    // Add modal to document
    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Show modal
    const modal = new bootstrap.Modal(document.getElementById('btcScalpingStrategyEditorModal'));
    modal.show();
    
    // Add event listeners
    const updateBtn = document.getElementById('update-btc-scalping-strategy-params');
    const copyBtn = document.getElementById('copy-updated-btc-scalping-code');
    
    // Generate updated code when parameters change
    const inputs = document.querySelectorAll('#btc-scalping-strategy-params-form input, #btc-scalping-strategy-params-form select');
    inputs.forEach(input => {
        input.addEventListener('input', generateUpdatedBTCScalpingStrategyCode);
        input.addEventListener('change', generateUpdatedBTCScalpingStrategyCode);
    });
    
    // Initial code generation
    generateUpdatedBTCScalpingStrategyCode();
    
    if (updateBtn) {
        updateBtn.addEventListener('click', () => {
            updateBTCScalpingStrategyParameters();
            modal.hide();
        });
    }
    
    if (copyBtn) {
        copyBtn.addEventListener('click', () => {
            const codeElement = document.getElementById('updated-btc-scalping-strategy-code');
            if (codeElement) {
                navigator.clipboard.writeText(codeElement.textContent)
                    .then(() => {
                        const originalText = copyBtn.innerHTML;
                        copyBtn.innerHTML = '<i class="bi bi-check"></i> Copied!';
                        copyBtn.classList.add('btn-success');
                        copyBtn.classList.remove('btn-outline-primary');
                        
                        setTimeout(() => {
                            copyBtn.innerHTML = originalText;
                            copyBtn.classList.remove('btn-success');
                            copyBtn.classList.add('btn-outline-primary');
                        }, 2000);
                    })
                    .catch(err => {
                        console.error('Failed to copy updated BTC Scalping code:', err);
                        showAlert('Failed to copy updated BTC Scalping code', 'danger');
                    });
            }
        });
    }
}

// Generate updated BTC Scalping strategy code with new parameters
function generateUpdatedBTCScalpingStrategyCode() {
    const params = {
        fastSMA: document.getElementById('btc-param-fast-sma')?.value || 5,
        slowSMA: document.getElementById('btc-param-slow-sma')?.value || 13,
        rsiLength: document.getElementById('btc-param-rsi-length')?.value || 7,
        rsiOverbought: document.getElementById('btc-param-rsi-overbought')?.value || 70,
        rsiOversold: document.getElementById('btc-param-rsi-oversold')?.value || 30,
        atrLength: document.getElementById('btc-param-atr-length')?.value || 5,
        volumeThreshold: document.getElementById('btc-param-volume-threshold')?.value || 1.0,
        startHour: document.getElementById('btc-param-start-hour')?.value || 0,
        endHour: document.getElementById('btc-param-end-hour')?.value || 23,
        useTimeFilter: document.getElementById('btc-param-use-time-filter')?.value || 'true'
    };
    
    const updatedCode = `//@version=6
indicator("BTC Futures Scalping Strategy - CUSTOMIZED", overlay=true)

// Scalping-optimized parameters - CUSTOMIZED
smaFast = input.int(${params.fastSMA}, title="Fast SMA", minval=1, group="Scalping Settings")
smaSlow = input.int(${params.slowSMA}, title="Slow SMA", minval=1, group="Scalping Settings")
rsiLength = input.int(${params.rsiLength}, title="RSI Length", minval=1, group="Scalping Settings")
rsiOverbought = input.float(${params.rsiOverbought}, title="RSI Overbought", minval=0, maxval=100, group="Scalping Settings")
rsiOversold = input.float(${params.rsiOversold}, title="RSI Oversold", minval=0, maxval=100, group="Scalping Settings")

// Volatility settings for scalping
atrLength = input.int(${params.atrLength}, title="ATR Length", minval=1, group="Volatility")
atrMultiplier = input.float(1.2, title="ATR Multiplier", minval=0.5, maxval=3.0, group="Volatility")

// Volume confirmation
volumeThreshold = input.float(${params.volumeThreshold}, title="Volume Threshold", minval=0.5, maxval=2.0, group="Volume")

// Time filters for scalping
useTimeFilter = input.bool(${params.useTimeFilter}, title="Use Time Filter", group="Time Settings")
startHour = input.int(${params.startHour}, title="Start Hour (UTC)", minval=0, maxval=23, group="Time Settings")
endHour = input.int(${params.endHour}, title="End Hour (UTC)", minval=0, maxval=23, group="Time Settings")

// Scalping indicators
fastSMA = ta.sma(close, smaFast)
slowSMA = ta.sma(close, smaSlow)
rsi = ta.rsi(close, rsiLength)
volumeSMA = ta.sma(volume, 5)
atr = ta.atr(atrLength)

// Bollinger Bands for scalping
[bbUpper, bbMiddle, bbLower] = ta.bb(close, 10, 1.5)

// MACD for momentum
[macdLine, signalLine, histogram] = ta.macd(close, 8, 21, 5)

// Stochastic for scalping
stochK = ta.stoch(close, high, low, 7)
stochD = ta.sma(stochK, 3)

// Pre-calculate values to avoid warnings
lowestClose5 = ta.lowest(close, 5)
highestClose5 = ta.highest(close, 5)

// Time filter
isValidTime = not useTimeFilter or (hour >= startHour and hour <= endHour)

// SCALPING LONG SIGNALS (Buy)
// 1. Fast SMA crossover above slow SMA
longSMA = ta.crossover(fastSMA, slowSMA)

// 2. RSI oversold bounce
longRSI = ta.crossover(rsi, 35)

// 3. Price above lower Bollinger Band with volume
longBB = close > bbLower and volume > volumeSMA * volumeThreshold

// 4. MACD bullish crossover
longMACD = ta.crossover(macdLine, signalLine)

// 5. Stochastic oversold bounce
longStoch = ta.crossover(stochK, 25)

// 6. Price making higher low
longHL = close > lowestClose5 and close > fastSMA

// 7. ATR expansion for volatility confirmation
longATR = atr > ta.sma(atr, 10)

// SCALPING SHORT SIGNALS (Sell)
// 1. Fast SMA crossover below slow SMA
shortSMA = ta.crossunder(fastSMA, slowSMA)

// 2. RSI overbought rejection
shortRSI = ta.crossunder(rsi, 65)

// 3. Price below upper Bollinger Band with volume
shortBB = close < bbUpper and volume > volumeSMA * volumeThreshold

// 4. MACD bearish crossover
shortMACD = ta.crossunder(macdLine, signalLine)

// 5. Stochastic overbought rejection
shortStoch = ta.crossunder(stochK, 75)

// 6. Price making lower high
shortLH = close < highestClose5 and close < fastSMA

// 7. ATR expansion for volatility confirmation
shortATR = atr > ta.sma(atr, 10)

// Combine signals for scalping
// LONG: Need at least 3 confirmations
longScore = (longSMA ? 1 : 0) + (longRSI ? 1 : 0) + (longBB ? 1 : 0) + (longMACD ? 1 : 0) + (longStoch ? 1 : 0) + (longHL ? 1 : 0) + (longATR ? 1 : 0)
longSignal = longScore >= 3 and isValidTime

// SHORT: Need at least 3 confirmations
shortScore = (shortSMA ? 1 : 0) + (shortRSI ? 1 : 0) + (shortBB ? 1 : 0) + (shortMACD ? 1 : 0) + (shortStoch ? 1 : 0) + (shortLH ? 1 : 0) + (shortATR ? 1 : 0)
shortSignal = shortScore >= 3 and isValidTime

// Plotting
plot(fastSMA, title="Fast SMA", color=color.blue, linewidth=2)
plot(slowSMA, title="Slow SMA", color=color.red, linewidth=2)
plot(bbUpper, title="BB Upper", color=color.gray, linewidth=1)
plot(bbLower, title="BB Lower", color=color.gray, linewidth=1)

// Scalping signals
plotshape(series=longSignal, title="LONG", location=location.belowbar, color=color.green, style=shape.triangleup, size=size.large, text="LONG")
plotshape(series=shortSignal, title="SHORT", location=location.abovebar, color=color.red, style=shape.triangledown, size=size.large, text="SHORT")

// Signal counter for scalping
var int longCount = 0
var int shortCount = 0
var int totalSignals = 0

if longSignal
    longCount := longCount + 1
    totalSignals := totalSignals + 1

if shortSignal
    shortCount := shortCount + 1
    totalSignals := totalSignals + 1

// Reset counter daily
if ta.change(time("D")) != 0
    longCount := 0
    shortCount := 0
    totalSignals := 0

// Display scalping signal count
var table infoTable = table.new(position.top_right, 2, 4, bgcolor=color.white, border_width=1)
if barstate.islast
    table.cell(infoTable, 0, 0, "Scalping Signals", text_color=color.black, bgcolor=color.gray)
    table.cell(infoTable, 1, 0, str.tostring(totalSignals), text_color=color.black, bgcolor=color.white)
    table.cell(infoTable, 0, 1, "LONG Signals", text_color=color.black, bgcolor=color.gray)
    table.cell(infoTable, 1, 1, str.tostring(longCount), text_color=color.green, bgcolor=color.white)
    table.cell(infoTable, 0, 2, "SHORT Signals", text_color=color.black, bgcolor=color.gray)
    table.cell(infoTable, 1, 2, str.tostring(shortCount), text_color=color.red, bgcolor=color.white)

// Scalping alerts
alertcondition(longSignal, title="BTC Scalping LONG", message="BTC SCALPING LONG - Price: {{close}}, Fast SMA: {{plot('Fast SMA')}}, RSI: {{plot('RSI')}}")
alertcondition(shortSignal, title="BTC Scalping SHORT", message="BTC SCALPING SHORT - Price: {{close}}, Fast SMA: {{plot('Fast SMA')}}, RSI: {{plot('RSI')}}")

// Additional info
hline(0, "Zero Line", color=color.gray, linestyle=hline.style_dashed)`;

    const codeElement = document.getElementById('updated-btc-scalping-strategy-code');
    if (codeElement) {
        codeElement.textContent = updatedCode;
    }
}

// Update BTC Scalping strategy parameters in the UI
function updateBTCScalpingStrategyParameters() {
    const params = {
        fastSMA: document.getElementById('btc-param-fast-sma')?.value || 5,
        slowSMA: document.getElementById('btc-param-slow-sma')?.value || 13,
        rsiLength: document.getElementById('btc-param-rsi-length')?.value || 7,
        rsiOverbought: document.getElementById('btc-param-rsi-overbought')?.value || 70,
        rsiOversold: document.getElementById('btc-param-rsi-oversold')?.value || 30,
        atrLength: document.getElementById('btc-param-atr-length')?.value || 5,
        volumeThreshold: document.getElementById('btc-param-volume-threshold')?.value || 1.0,
        startHour: document.getElementById('btc-param-start-hour')?.value || 0,
        endHour: document.getElementById('btc-param-end-hour')?.value || 23,
        useTimeFilter: document.getElementById('btc-param-use-time-filter')?.value || 'true'
    };
    
    // Update the parameter display in the BTC Scalping strategy card
    const btcScalpingStrategyCards = document.querySelectorAll('.strategy-card');
    const btcScalpingStrategyCard = btcScalpingStrategyCards[3]; // Fourth strategy card is BTC Scalping strategy
    
    if (btcScalpingStrategyCard) {
        const parameterItems = btcScalpingStrategyCard.querySelectorAll('.parameter-item');
        parameterItems.forEach(item => {
            const label = item.querySelector('label').textContent;
            const span = item.querySelector('span');
            
            switch(label) {
                case 'Fast SMA:':
                    span.textContent = params.fastSMA;
                    break;
                case 'Slow SMA:':
                    span.textContent = params.slowSMA;
                    break;
                case 'RSI Length:':
                    span.textContent = params.rsiLength;
                    break;
                case 'RSI Overbought:':
                    span.textContent = params.rsiOverbought;
                    break;
                case 'RSI Oversold:':
                    span.textContent = params.rsiOversold;
                    break;
                case 'ATR Length:':
                    span.textContent = params.atrLength;
                    break;
                case 'Volume Threshold:':
                    span.textContent = params.volumeThreshold + 'x';
                    break;
                case 'Time Filter:':
                    span.textContent = params.startHour + '-' + params.endHour + ' UTC';
                    break;
            }
        });
    }
    
    showAlert('BTC Scalping Strategy parameters updated successfully!', 'success');
}

// Show Volume Momentum strategy parameter editor modal
function showVMStrategyEditor() {
    const modalHtml = `
        <div class="modal fade" id="vmStrategyEditorModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">
                            <i class="bi bi-gear"></i>
                            Edit Volume Momentum Strategy Parameters
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <div class="alert alert-info">
                            <i class="bi bi-info-circle"></i>
                            Modify the parameters below and copy the updated Pine Script code to TradingView.
                        </div>
                        
                        <form id="vm-strategy-params-form">
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">VWAP Period</label>
                                    <input type="number" class="form-control" id="vm-param-vwap-length" value="20" min="1" max="100">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Volume MA Length</label>
                                    <input type="number" class="form-control" id="vm-param-vol-ma-length" value="50" min="1" max="200">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Momentum RSI Length</label>
                                    <input type="number" class="form-control" id="vm-param-rsi-length" value="14" min="1" max="50">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Volume Threshold</label>
                                    <input type="number" class="form-control" id="vm-param-vol-threshold" value="2.0" min="0.1" max="10.0" step="0.1">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Delta Threshold</label>
                                    <input type="number" class="form-control" id="vm-param-delta-threshold" value="1.5" min="0.1" max="10.0" step="0.1">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Max Leverage</label>
                                    <input type="number" class="form-control" id="vm-param-max-leverage" value="25" min="1" max="100">
                                </div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Stop Loss %</label>
                                    <input type="number" class="form-control" id="vm-param-stop-loss" value="1.5" min="0.1" max="10.0" step="0.1">
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Take Profit %</label>
                                    <input type="number" class="form-control" id="vm-param-take-profit" value="3.0" min="0.1" max="20.0" step="0.1">
                                </div>
                            </div>
                        </form>
                        
                        <div class="mt-4">
                            <h6>Updated Pine Script Code:</h6>
                            <div class="bg-light p-3 rounded" style="max-height: 300px; overflow-y: auto;">
                                <pre><code id="updated-vm-strategy-code">// Code will be generated here...</code></pre>
                            </div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-outline-primary" id="copy-updated-vm-code">
                            <i class="bi bi-clipboard"></i>
                            Copy Updated Code
                        </button>
                        <button type="button" class="btn btn-primary" id="update-vm-strategy-params">
                            <i class="bi bi-save"></i>
                            Update Parameters
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;

    // Remove existing modal if any
    const existingModal = document.getElementById('vmStrategyEditorModal');
    if (existingModal) {
        existingModal.remove();
    }

    // Add modal to document
    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Show modal
    const modal = new bootstrap.Modal(document.getElementById('vmStrategyEditorModal'));
    modal.show();
    
    // Add event listeners
    const updateBtn = document.getElementById('update-vm-strategy-params');
    const copyBtn = document.getElementById('copy-updated-vm-code');
    
    // Generate updated code when parameters change
    const inputs = document.querySelectorAll('#vm-strategy-params-form input');
    inputs.forEach(input => {
        input.addEventListener('input', generateUpdatedVMStrategyCode);
    });
    
    // Initial code generation
    generateUpdatedVMStrategyCode();
    
    if (updateBtn) {
        updateBtn.addEventListener('click', () => {
            updateVMStrategyParameters();
            modal.hide();
        });
    }
    
    if (copyBtn) {
        copyBtn.addEventListener('click', () => {
            const codeElement = document.getElementById('updated-vm-strategy-code');
            if (codeElement) {
                navigator.clipboard.writeText(codeElement.textContent)
                    .then(() => {
                        const originalText = copyBtn.innerHTML;
                        copyBtn.innerHTML = '<i class="bi bi-check"></i> Copied!';
                        setTimeout(() => {
                            copyBtn.innerHTML = originalText;
                        }, 2000);
                    })
                    .catch(err => {
                        console.error('Failed to copy code:', err);
                        showAlert('Failed to copy strategy code', 'danger');
                    });
            }
        });
    }
}

// Generate updated Volume Momentum strategy code
function generateUpdatedVMStrategyCode() {
    const params = {
        vwapLength: document.getElementById('vm-param-vwap-length')?.value || 20,
        volMALength: document.getElementById('vm-param-vol-ma-length')?.value || 50,
        rsiLength: document.getElementById('vm-param-rsi-length')?.value || 14,
        volThreshold: document.getElementById('vm-param-vol-threshold')?.value || 2.0,
        deltaThreshold: document.getElementById('vm-param-delta-threshold')?.value || 1.5,
        maxLeverage: document.getElementById('vm-param-max-leverage')?.value || 25,
        stopLossPct: document.getElementById('vm-param-stop-loss')?.value || 1.5,
        takeProfitPct: document.getElementById('vm-param-take-profit')?.value || 3.0
    };
    
    const code = `//@version=5
strategy("Volume Momentum Strategy", overlay=true)

// Input parameters
vwapLength = input(${params.vwapLength}, "VWAP Period")
volMALength = input(${params.volMALength}, "Volume MA Length")
rsiLength = input(${params.rsiLength}, "Momentum RSI Length")
volThreshold = input.float(${params.volThreshold}, "Volume Threshold")
deltaThreshold = input.float(${params.deltaThreshold}, "Delta Threshold")
maxLeverage = input.float(${params.maxLeverage}, "Max Leverage")
stopLossPct = input.float(${params.stopLossPct}, "Stop Loss %")
takeProfitPct = input.float(${params.takeProfitPct}, "Take Profit %")

// Calculate indicators
vwap = ta.vwap
volMA = ta.sma(volume, volMALength)
rsi = ta.rsi(close, rsiLength)

// Volume analysis
volRatio = volume / volMA
volDelta = volume * (close - open) / open

// Entry conditions
longCondition = volRatio > volThreshold and 
                volDelta > deltaThreshold and 
                rsi < 30 and 
                close > vwap

shortCondition = volRatio > volThreshold and 
                 volDelta < -deltaThreshold and 
                 rsi > 70 and 
                 close < vwap

// Position sizing based on volume strength
positionSize = math.min(volRatio / volThreshold, maxLeverage)

// Execute trades
if (longCondition)
    strategy.entry("Long", strategy.long, qty=positionSize)
    strategy.exit("Long Exit", "Long", 
                 stop=close * (1 - stopLossPct/100),
                 limit=close * (1 + takeProfitPct/100))

if (shortCondition)
    strategy.entry("Short", strategy.short, qty=positionSize)
    strategy.exit("Short Exit", "Short", 
                 stop=close * (1 + stopLossPct/100),
                 limit=close * (1 - takeProfitPct/100))

// Plotting
plot(vwap, "VWAP", color=color.blue)
plot(volMA, "Volume MA", color=color.purple, display=display.none)

// Background coloring
bgcolor(longCondition ? color.new(color.green, 95) : 
        shortCondition ? color.new(color.red, 95) : 
        color.new(color.blue, 95))

// Alerts
alertcondition(longCondition, title="Long Entry Alert", 
               message="Volume Momentum Strategy - BUY Signal")
alertcondition(shortCondition, title="Short Entry Alert", 
               message="Volume Momentum Strategy - SELL Signal")`;

    const codeElement = document.getElementById('updated-vm-strategy-code');
    if (codeElement) {
        codeElement.textContent = code;
    }
}

// Update Volume Momentum strategy parameters in the UI
function updateVMStrategyParameters() {
    const params = {
        vwapLength: document.getElementById('vm-param-vwap-length')?.value || 20,
        volMALength: document.getElementById('vm-param-vol-ma-length')?.value || 50,
        rsiLength: document.getElementById('vm-param-rsi-length')?.value || 14,
        volThreshold: document.getElementById('vm-param-vol-threshold')?.value || 2.0,
        deltaThreshold: document.getElementById('vm-param-delta-threshold')?.value || 1.5,
        maxLeverage: document.getElementById('vm-param-max-leverage')?.value || 25,
        stopLossPct: document.getElementById('vm-param-stop-loss')?.value || 1.5,
        takeProfitPct: document.getElementById('vm-param-take-profit')?.value || 3.0
    };
    
    // Update the parameter display in the VM strategy card
    const vmStrategyCards = document.querySelectorAll('.strategy-card');
    const vmStrategyCard = vmStrategyCards[2]; // Third strategy card
    
    if (vmStrategyCard) {
        const parameterItems = vmStrategyCard.querySelectorAll('.parameter-item');
        parameterItems.forEach(item => {
            const label = item.querySelector('label').textContent;
            const span = item.querySelector('span');
            
            switch(label) {
                case 'VWAP Period:':
                    span.textContent = params.vwapLength;
                    break;
                case 'Volume MA:':
                    span.textContent = params.volMALength;
                    break;
                case 'Momentum RSI:':
                    span.textContent = params.rsiLength;
                    break;
                case 'Volume Threshold:':
                    span.textContent = params.volThreshold;
                    break;
                case 'Delta Threshold:':
                    span.textContent = params.deltaThreshold;
                    break;
                case 'Max Leverage:':
                    span.textContent = params.maxLeverage + 'x';
                    break;
                case 'Stop Loss:':
                    span.textContent = params.stopLossPct + '%';
                    break;
                case 'Take Profit:':
                    span.textContent = params.takeProfitPct + '%';
                    break;
            }
        });
    }
    
    showAlert('Volume Momentum Strategy parameters updated successfully!', 'success');
}

// Global functions for position management
window.editPosition = function(symbol) {
    alert(`Edit position for ${symbol} - This feature is coming soon!`);
    // TODO: Implement position editing modal
};

window.closePosition = async function(symbol) {
    console.log(`🎯 closePosition function called for ${symbol}`);
    
    if (!confirm(`⚠️ Are you sure you want to close your ${symbol} position?\n\nThis will place a market order to close your entire position immediately.`)) {
        console.log(`❌ User cancelled close position for ${symbol}`);
        return;
    }
    
    try {
        console.log(`🔄 Closing position for ${symbol}...`);
        console.log(`📡 Making API call to /api/close-position`);
        
        // Show loading state
        const closeButton = document.querySelector(`button[onclick="closePosition('${symbol}')"]`);
        const originalContent = closeButton.innerHTML;
        closeButton.innerHTML = '<i class="bi bi-hourglass-split"></i>';
        closeButton.disabled = true;
        
        const response = await fetch('/api/close-position', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                symbol: symbol
            })
        });
        
        console.log(`📡 Response status: ${response.status}`);
        console.log(`📡 Response ok: ${response.ok}`);
        
        const result = await response.json();
        console.log(`📡 Response result:`, result);
        
        if (result.success) {
            const details = result.details;
            const positionType = details.was_long ? 'LONG' : 'SHORT';
            const closeAction = details.was_long ? 'SOLD' : 'BOUGHT';
            
            showAlert(`✅ Position Closed Successfully!\n\n${symbol} ${positionType} position closed\n${closeAction} ${details.closed_size} at $${details.close_price}`, 'success');
            
            // Refresh positions and activity
            fetchPositions();
            fetchRecentActivity();
            
            console.log(`✅ Successfully closed ${symbol} position:`, details);
        } else {
            showAlert(`❌ Failed to close ${symbol} position: ${result.error}`, 'danger');
            console.error(`❌ Close position error:`, result.error);
        }
        
    } catch (error) {
        console.error(`❌ Error closing ${symbol} position:`, error);
        showAlert(`❌ Error closing ${symbol} position: ${error.message}`, 'danger');
    } finally {
        // Restore button state
        const closeButton = document.querySelector(`button[onclick="closePosition('${symbol}')"]`);
        if (closeButton) {
            closeButton.innerHTML = originalContent;
            closeButton.disabled = false;
        }
    }
};

// Close all positions function
async function closeAllPositions() {
    console.log(`🎯 closeAllPositions function called`);
    
    if (!confirm(`⚠️ Are you sure you want to close ALL your positions?\n\nThis will place market orders to close every open position immediately.\n\nThis action cannot be undone!`)) {
        console.log(`❌ User cancelled close all positions`);
        return;
    }
    
    try {
        console.log('🔄 Closing all positions...');
        console.log(`📡 Making API call to /api/close-all-positions`);
        
        // Show loading state
        const closeAllBtn = document.getElementById('close-all-positions');
        const originalContent = closeAllBtn.innerHTML;
        closeAllBtn.innerHTML = '<i class="bi bi-hourglass-split"></i> Closing...';
        closeAllBtn.disabled = true;
        
        const response = await fetch('/api/close-all-positions', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        console.log(`📡 Close All Response status: ${response.status}`);
        console.log(`📡 Close All Response ok: ${response.ok}`);
        
        const result = await response.json();
        console.log(`📡 Close All Response result:`, result);
        
        if (result.success) {
            const summary = result.summary;
            let message = `✅ Positions Closed!\n\n`;
            
            if (summary.successfully_closed === summary.total_positions) {
                message += `Successfully closed all ${summary.successfully_closed} positions.`;
            } else {
                message += `Closed ${summary.successfully_closed}/${summary.total_positions} positions.`;
                if (summary.failed_to_close > 0) {
                    message += `\n\n⚠️ ${summary.failed_to_close} positions failed to close.`;
                }
            }
            
            // Show detailed results
            if (result.closed_positions.length > 0) {
                message += '\n\nClosed positions:';
                result.closed_positions.forEach(pos => {
                    const posType = pos.was_long ? 'LONG' : 'SHORT';
                    message += `\n• ${pos.symbol} ${posType} (${pos.size} @ $${pos.price})`;
                });
            }
            
            if (result.failed_positions.length > 0) {
                message += '\n\nFailed positions:';
                result.failed_positions.forEach(pos => {
                    message += `\n• ${pos.symbol}: ${pos.error}`;
                });
            }
            
            showAlert(message, summary.failed_to_close === 0 ? 'success' : 'warning');
            
            // Refresh positions and activity
            fetchPositions();
            fetchRecentActivity();
            
            console.log('✅ Close all positions result:', result);
        } else {
            showAlert(`❌ Failed to close positions: ${result.error}`, 'danger');
            console.error('❌ Close all positions error:', result.error);
        }
        
    } catch (error) {
        console.error('❌ Error closing all positions:', error);
        showAlert(`❌ Error closing all positions: ${error.message}`, 'danger');
    } finally {
        // Restore button state
        const closeAllBtn = document.getElementById('close-all-positions');
        if (closeAllBtn) {
            closeAllBtn.innerHTML = originalContent;
            closeAllBtn.disabled = false;
        }
    }
}

// Strategy Engine Functionality
function initStrategyEngine() {
    // Toggle technical indicators section
    const toggleIndicatorsBtn = document.getElementById('toggle-indicators');
    const indicatorsSection = document.querySelector('.technical-indicators-section');
    
    if (toggleIndicatorsBtn && indicatorsSection) {
        toggleIndicatorsBtn.addEventListener('click', () => {
            const isVisible = indicatorsSection.style.display !== 'none';
            indicatorsSection.style.display = isVisible ? 'none' : 'block';
            toggleIndicatorsBtn.innerHTML = isVisible ? 
                '<i class="bi bi-eye"></i> Show Indicators' : 
                '<i class="bi bi-eye-slash"></i> Hide Indicators';
        });
    }
    
    // Clear indicators button
    const clearIndicatorsBtn = document.getElementById('clear-indicators');
    if (clearIndicatorsBtn) {
        clearIndicatorsBtn.addEventListener('click', () => {
            const inputs = document.querySelectorAll('.technical-indicators-section input');
            inputs.forEach(input => input.value = '');
        });
    }
    
    // Analyze strategy button
    const analyzeBtn = document.getElementById('analyze-strategy');
    if (analyzeBtn) {
        analyzeBtn.addEventListener('click', performStrategyAnalysis);
    }
    
    // Execute trade button
    const executeBtn = document.getElementById('execute-trade');
    if (executeBtn) {
        executeBtn.addEventListener('click', executeTrade);
    }
    
    // Save analysis button
    const saveBtn = document.getElementById('save-analysis');
    if (saveBtn) {
        saveBtn.addEventListener('click', saveAnalysis);
    }
    
    // Export analysis button
    const exportBtn = document.getElementById('export-analysis');
    if (exportBtn) {
        exportBtn.addEventListener('click', exportAnalysis);
    }
}

async function performStrategyAnalysis() {
    const analyzeBtn = document.getElementById('analyze-strategy');
    const resultsSection = document.getElementById('strategy-results');
    
    // Get form data
    const symbol = document.getElementById('strategy-symbol').value;
    const side = document.getElementById('strategy-side').value;
    const balance = parseFloat(document.getElementById('strategy-balance').value);
    
    if (!symbol || !balance || balance <= 0) {
        showAlert('Please select a symbol and enter a valid balance', 'warning');
        return;
    }
    
    // Show loading state
    const originalText = analyzeBtn.innerHTML;
    analyzeBtn.innerHTML = '<span class="loading-spinner"></span> Analyzing...';
    analyzeBtn.disabled = true;
    
    try {
        // Collect technical indicators
        const technicalIndicators = {
            rsi: parseFloat(document.getElementById('ti-rsi').value) || null,
            macd: parseFloat(document.getElementById('ti-macd').value) || null,
            signal: parseFloat(document.getElementById('ti-signal').value) || null,
            adx: parseFloat(document.getElementById('ti-adx').value) || null,
            volume: parseFloat(document.getElementById('ti-volume').value) || null,
            atr: parseFloat(document.getElementById('ti-atr').value) || null,
            bb_upper: parseFloat(document.getElementById('ti-bb-upper').value) || null,
            bb_middle: parseFloat(document.getElementById('ti-bb-middle').value) || null,
            bb_lower: parseFloat(document.getElementById('ti-bb-lower').value) || null,
            ema_fast: parseFloat(document.getElementById('ti-ema-fast').value) || null,
            ema_slow: parseFloat(document.getElementById('ti-ema-slow').value) || null,
            vol_ma: parseFloat(document.getElementById('ti-vol-ma').value) || null
        };
        
        // Remove null values
        Object.keys(technicalIndicators).forEach(key => {
            if (technicalIndicators[key] === null) {
                delete technicalIndicators[key];
            }
        });
        
        // Call full analysis API
        const response = await fetch('/api/strategy/full-analysis', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                symbol,
                side,
                available_balance: balance,
                technical_indicators: technicalIndicators
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            displayAnalysisResults(data);
            resultsSection.style.display = 'block';
            
            // Scroll to results
            resultsSection.scrollIntoView({ behavior: 'smooth' });
        } else {
            showAlert(`Analysis failed: ${data.error}`, 'danger');
        }
        
    } catch (error) {
        console.error('Strategy analysis error:', error);
        showAlert('Failed to perform strategy analysis', 'danger');
    } finally {
        // Reset button
        analyzeBtn.innerHTML = originalText;
        analyzeBtn.disabled = false;
    }
}

function displayAnalysisResults(data) {
    // Market Analysis
    displayMarketAnalysis(data.market_analysis);
    
    // Entry Analysis
    displayEntryAnalysis(data.entry_analysis);
    
    // Position Sizing
    displayPositionSizing(data.position_sizing);
    
    // Exit Strategy
    displayExitStrategy(data.exit_strategy);
    
    // Recommendation
    displayRecommendation(data.recommendation);
    
    // Show/hide execute button
    const executeBtn = document.getElementById('execute-trade');
    if (data.recommendation.action === 'ENTER') {
        executeBtn.style.display = 'inline-block';
        executeBtn.dataset.analysisData = JSON.stringify(data);
    } else {
        executeBtn.style.display = 'none';
    }
}

function displayMarketAnalysis(analysis) {
    // Overall metrics
    document.getElementById('market-score').textContent = (analysis.overall_score * 100).toFixed(1) + '%';
    document.getElementById('volatility-regime').textContent = analysis.volatility.regime.replace('_', ' ').toUpperCase();
    document.getElementById('trend-strength').textContent = analysis.trend.strength.toUpperCase();
    document.getElementById('market-quality').textContent = analysis.microstructure.market_quality.toUpperCase();
    
    // Set indicators
    setMetricIndicator('market-score-indicator', analysis.overall_score);
    setMetricIndicator('volatility-indicator', analysis.volatility.vol_score);
    setMetricIndicator('trend-indicator', analysis.trend.alignment_score);
    setMetricIndicator('quality-indicator', analysis.microstructure.efficiency_score);
    
    // Detailed metrics
    document.getElementById('ob-imbalance').textContent = (analysis.orderbook.imbalance * 100).toFixed(2) + '%';
    document.getElementById('ob-liquidity').textContent = (analysis.orderbook.liquidity_score * 100).toFixed(1) + '%';
    document.getElementById('ob-spread').textContent = analysis.orderbook.spread_pct.toFixed(3) + '%';
    document.getElementById('ob-pressure').textContent = analysis.orderbook.pressure.toUpperCase();
    
    document.getElementById('vol-score').textContent = (analysis.volume.volume_score * 100).toFixed(1) + '%';
    document.getElementById('vol-conviction').textContent = analysis.volume.conviction_level.toUpperCase();
    document.getElementById('vol-trend').textContent = analysis.volume.volume_trend.toUpperCase();
    document.getElementById('vol-ratio').textContent = analysis.volume.volume_ratio.toFixed(2) + 'x';
}

function displayEntryAnalysis(analysis) {
    document.getElementById('should-enter').textContent = analysis.should_enter ? 'YES' : 'NO';
    document.getElementById('entry-confidence').textContent = (analysis.confidence * 100).toFixed(1) + '%';
    document.getElementById('confidence-level').textContent = analysis.confidence_level.toUpperCase();
    
    // Set indicators
    setMetricIndicator('entry-indicator', analysis.should_enter ? 1 : 0);
    setMetricIndicator('confidence-indicator', analysis.confidence);
    setMetricIndicator('level-indicator', getConfidenceLevelScore(analysis.confidence_level));
    
    // Display conditions
    displayConditions(analysis.analysis.conditions);
}

function displayPositionSizing(sizing) {
    if (sizing) {
        document.getElementById('position-size').textContent = sizing.position_size.toFixed(6);
        document.getElementById('position-value').textContent = '$' + sizing.position_value.toFixed(2);
        document.getElementById('optimal-leverage').textContent = sizing.optimal_leverage + 'x';
        document.getElementById('risk-percentage').textContent = sizing.risk_percentage.toFixed(2) + '%';
    } else {
        document.getElementById('position-size').textContent = '-';
        document.getElementById('position-value').textContent = '-';
        document.getElementById('optimal-leverage').textContent = '-';
        document.getElementById('risk-percentage').textContent = '-';
    }
}

function displayExitStrategy(strategy) {
    const content = document.getElementById('exit-strategy-content');
    
    if (!strategy) {
        content.innerHTML = '<p class="text-muted">No exit strategy available</p>';
        return;
    }
    
    let html = '';
    
    // Stop Loss
    html += `
        <div class="exit-level">
            <div class="exit-level-header">
                <span class="exit-level-title">Stop Loss</span>
                <span class="exit-level-type">${strategy.stop_loss.type}</span>
            </div>
            <div class="exit-details">
                <div class="exit-detail">
                    <span class="exit-detail-label">Price</span>
                    <span class="exit-detail-value">$${strategy.stop_loss.price.toFixed(4)}</span>
                </div>
                <div class="exit-detail">
                    <span class="exit-detail-label">Percentage</span>
                    <span class="exit-detail-value">${strategy.stop_loss.percentage.toFixed(2)}%</span>
                </div>
            </div>
        </div>
    `;
    
    // Take Profits
    strategy.take_profits.forEach((tp, index) => {
        html += `
            <div class="exit-level">
                <div class="exit-level-header">
                    <span class="exit-level-title">Take Profit ${index + 1}</span>
                    <span class="exit-level-type">${tp.type}</span>
                </div>
                <div class="exit-details">
                    <div class="exit-detail">
                        <span class="exit-detail-label">Price</span>
                        <span class="exit-detail-value">$${tp.price.toFixed(4)}</span>
                    </div>
                    <div class="exit-detail">
                        <span class="exit-detail-label">Size</span>
                        <span class="exit-detail-value">${tp.size.toFixed(6)}</span>
                    </div>
                </div>
            </div>
        `;
    });
    
    // Risk Management Summary
    html += `
        <div class="exit-level">
            <div class="exit-level-header">
                <span class="exit-level-title">Risk Management</span>
            </div>
            <div class="exit-details">
                <div class="exit-detail">
                    <span class="exit-detail-label">Max Loss</span>
                    <span class="exit-detail-value">$${strategy.risk_management.max_loss_per_trade.toFixed(2)}</span>
                </div>
                <div class="exit-detail">
                    <span class="exit-detail-label">Risk/Reward</span>
                    <span class="exit-detail-value">${strategy.risk_management.risk_reward_ratio.toFixed(2)}</span>
                </div>
            </div>
        </div>
    `;
    
    content.innerHTML = html;
}

function displayRecommendation(recommendation) {
    const card = document.getElementById('recommendation-card');
    
    // Set card class
    card.className = 'recommendation-card ' + (recommendation.action === 'ENTER' ? 'enter' : 'wait');
    
    let html = `
        <div class="recommendation-header">
            <span class="recommendation-action">${recommendation.action}</span>
            <span class="recommendation-confidence">${(recommendation.confidence * 100).toFixed(1)}% Confidence</span>
        </div>
    `;
    
    if (recommendation.reasons.length > 0) {
        html += `
            <div class="recommendation-reasons">
                <h6>Reasons to Enter:</h6>
                <ul>
                    ${recommendation.reasons.map(reason => `<li>${reason}</li>`).join('')}
                </ul>
            </div>
        `;
    }
    
    if (recommendation.warnings.length > 0) {
        html += `
            <div class="recommendation-warnings">
                <h6>Warnings:</h6>
                <ul>
                    ${recommendation.warnings.map(warning => `<li>${warning}</li>`).join('')}
                </ul>
            </div>
        `;
    }
    
    card.innerHTML = html;
}

function displayConditions(conditions) {
    const grid = document.getElementById('conditions-grid');
    
    let html = '';
    Object.entries(conditions).forEach(([key, met]) => {
        const label = key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
        html += `
            <div class="condition-item ${met ? 'met' : 'not-met'}">
                <div class="condition-icon ${met ? 'met' : 'not-met'}"></div>
                <span class="condition-label">${label}</span>
            </div>
        `;
    });
    
    grid.innerHTML = html;
}

function setMetricIndicator(elementId, score) {
    const indicator = document.getElementById(elementId);
    if (!indicator) return;
    
    indicator.className = 'metric-indicator';
    
    if (score >= 0.8) {
        indicator.classList.add('excellent');
    } else if (score >= 0.6) {
        indicator.classList.add('good');
    } else if (score >= 0.4) {
        indicator.classList.add('medium');
    } else {
        indicator.classList.add('poor');
    }
}

function getConfidenceLevelScore(level) {
    switch (level) {
        case 'high': return 0.9;
        case 'medium': return 0.7;
        case 'low': return 0.5;
        default: return 0.3;
    }
}

async function executeTrade() {
    const executeBtn = document.getElementById('execute-trade');
    const analysisData = JSON.parse(executeBtn.dataset.analysisData);
    
    if (!analysisData) {
        showAlert('No analysis data available', 'danger');
        return;
    }
    
    // Show confirmation dialog
    const confirmed = confirm(`Execute ${analysisData.side.toUpperCase()} trade for ${analysisData.symbol}?\n\nPosition Size: ${analysisData.position_sizing.position_size.toFixed(6)}\nPosition Value: $${analysisData.position_sizing.position_value.toFixed(2)}\nLeverage: ${analysisData.position_sizing.optimal_leverage}x`);
    
    if (!confirmed) return;
    
    // Show loading state
    const originalText = executeBtn.innerHTML;
    executeBtn.innerHTML = '<span class="loading-spinner"></span> Executing...';
    executeBtn.disabled = true;
    
    try {
        const response = await fetch('/api/order', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                pair: analysisData.symbol,
                type: 'market',
                side: analysisData.side,
                size: analysisData.position_sizing.position_size,
                leverage: analysisData.position_sizing.optimal_leverage,
                market_type: 'perpetual'
            })
        });
        
        const result = await response.json();
        
        if (result.success) {
            showAlert('Trade executed successfully!', 'success');
            executeBtn.style.display = 'none';
            
            // Refresh positions and activity
            fetchPositions();
            fetchRecentActivity();
        } else {
            showAlert(`Trade execution failed: ${result.error}`, 'danger');
        }
        
    } catch (error) {
        console.error('Trade execution error:', error);
        showAlert('Failed to execute trade', 'danger');
    } finally {
        executeBtn.innerHTML = originalText;
        executeBtn.disabled = false;
    }
}

function saveAnalysis() {
    const analysisData = {
        timestamp: new Date().toISOString(),
        symbol: document.getElementById('strategy-symbol').value,
        side: document.getElementById('strategy-side').value,
        balance: document.getElementById('strategy-balance').value,
        results: document.getElementById('strategy-results').innerHTML
    };
    
    // Save to localStorage
    const savedAnalyses = JSON.parse(localStorage.getItem('strategyAnalyses') || '[]');
    savedAnalyses.unshift(analysisData);
    
    // Keep only last 10 analyses
    if (savedAnalyses.length > 10) {
        savedAnalyses.splice(10);
    }
    
    localStorage.setItem('strategyAnalyses', JSON.stringify(savedAnalyses));
    
    showAlert('Analysis saved successfully!', 'success');
}

function exportAnalysis() {
    const symbol = document.getElementById('strategy-symbol').value;
    const timestamp = new Date().toISOString().split('T')[0];
    
    // Create export data
    const exportData = {
        timestamp: new Date().toISOString(),
        symbol,
        side: document.getElementById('strategy-side').value,
        balance: document.getElementById('strategy-balance').value,
        market_score: document.getElementById('market-score').textContent,
        should_enter: document.getElementById('should-enter').textContent,
        confidence: document.getElementById('entry-confidence').textContent,
        position_size: document.getElementById('position-size').textContent,
        optimal_leverage: document.getElementById('optimal-leverage').textContent
    };
    
    // Create and download file
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `strategy-analysis-${symbol}-${timestamp}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    showAlert('Analysis exported successfully!', 'success');
}

// Global showAlert function (accessible by all functions)
function showAlert(message, type = 'info') {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    `;
    
    const container = document.querySelector('.container') || document.body;
    if (container.firstChild) {
        container.insertBefore(alertDiv, container.firstChild);
    } else {
        container.appendChild(alertDiv);
    }
    
    // Auto dismiss after 5 seconds
    setTimeout(() => {
        if (alertDiv.parentNode) {
            alertDiv.remove();
        }
    }, 5000);
}

// Global showAlert function for use by all components
function showAlert(message, type = 'info') {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    `;
    
    const container = document.querySelector('.container');
    if (container) {
        container.insertBefore(alertDiv, container.firstChild);
        
        // Auto dismiss after 5 seconds
        setTimeout(() => {
            if (alertDiv.parentNode) {
                alertDiv.remove();
            }
        }, 5000);
    }
}

// Backtester functionality
function initBacktesterSection() {
    const runBacktestBtn = document.getElementById('run-backtest');
    if (runBacktestBtn) {
        runBacktestBtn.addEventListener('click', runBacktest);
    }
    
    // Initialize clear errors button
    const clearErrorsBtn = document.getElementById('clear-errors');
    if (clearErrorsBtn) {
        clearErrorsBtn.addEventListener('click', clearBacktestErrors);
    }
    
    // Add event listeners for date changes
    const startDateInput = document.getElementById('backtest-start-date');
    const endDateInput = document.getElementById('backtest-end-date');
    const intervalInput = document.getElementById('backtest-interval');
    
    if (startDateInput) {
        startDateInput.addEventListener('change', updateDateRangeDisplay);
    }
    if (endDateInput) {
        endDateInput.addEventListener('change', updateDateRangeDisplay);
    }
    if (intervalInput) {
        intervalInput.addEventListener('change', updateIntervalDisplay);
    }
    
    // Load initial status and update displays
    updateBacktestStatus();
    updateDateRangeDisplay();
    updateIntervalDisplay();
}

function clearBacktestErrors() {
    console.log('=== clearBacktestErrors STARTED ===');
    
    // Remove error details section
    const errorSection = document.getElementById('backtest-error-details');
    if (errorSection) {
        errorSection.remove();
    }
    
    // Hide clear errors button
    const clearErrorsBtn = document.getElementById('clear-errors');
    if (clearErrorsBtn) {
        clearErrorsBtn.style.display = 'none';
    }
    
    // Reset status to Ready
    const statusElement = document.getElementById('backtest-status');
    if (statusElement) {
        statusElement.textContent = 'Ready';
    }
    
    console.log('=== clearBacktestErrors COMPLETED ===');
}

async function runBacktest() {
    const runBtn = document.getElementById('run-backtest');
    const resultsSection = document.getElementById('backtest-results');
    
    // Get form data
    const symbol = document.getElementById('backtest-symbol').value;
    const symbolText = document.getElementById('backtest-symbol').options[document.getElementById('backtest-symbol').selectedIndex].text;
    const startDate = document.getElementById('backtest-start-date').value;
    const endDate = document.getElementById('backtest-end-date').value;
    const strategy = document.getElementById('backtest-strategy').value;
    const interval = document.getElementById('backtest-interval').value;
    const intervalText = document.getElementById('backtest-interval').options[document.getElementById('backtest-interval').selectedIndex].text;
    
    if (!symbol || !startDate || !endDate) {
        showAlert('Please fill in all required fields', 'warning');
        return;
    }
    
    // Validate date range
    if (new Date(startDate) >= new Date(endDate)) {
        showAlert('End date must be after start date', 'warning');
        return;
    }
    
    // Clear previous results and errors
    if (resultsSection) {
        resultsSection.innerHTML = '';
    }
    
    // Clear previous error details
    const errorSection = document.getElementById('backtest-error-details');
    if (errorSection) {
        errorSection.remove();
    }
    
    // Show loading state
    const originalText = runBtn.innerHTML;
    runBtn.innerHTML = '<span class="loading-spinner"></span> Running Backtest...';
    runBtn.disabled = true;
    
    // Update status
    document.getElementById('backtest-status').textContent = 'Running...';
    
    try {
        const response = await fetch('/api/backtest/run', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                symbol,
                start_date: startDate,
                end_date: endDate,
                strategy,
                interval
            })
        });
        
        const data = await response.json();
        
        // Debug logging
        console.log('Backtest API Response:', data);
        console.log('Response has success field:', 'success' in data);
        console.log('Success value:', data.success);
        
        if (data.success) {
            console.log('Success condition met, displaying results');
            console.log('Full API response:', JSON.stringify(data, null, 2));
            console.log('Results object:', data.results);
            console.log('Results keys:', Object.keys(data.results));
            
            try {
                // Clear any previous error details on success
                const errorSection = document.getElementById('backtest-error-details');
                if (errorSection) {
                    errorSection.remove();
                }
                
                displayBacktestResults(data.results);
                console.log('displayBacktestResults completed successfully');
                
                resultsSection.style.display = 'block';
                document.getElementById('backtest-status').textContent = 'Completed';
                showAlert(`Backtest completed successfully for ${symbolText} using ${intervalText} interval!`, 'success');
                
                // Scroll to results
                resultsSection.scrollIntoView({ behavior: 'smooth' });
                console.log('Results section shown and scrolled to');
            } catch (error) {
                console.error('Error in displayBacktestResults:', error);
                showAlert(`Error displaying results: ${error.message}`, 'danger');
            }
        } else {
            console.log('Success condition failed, showing error');
            document.getElementById('backtest-status').textContent = 'Failed';
            
            // Display detailed error information
            if (data.error) {
                displayDetailedError(data.error, data.error_type, data.error_details, data.timestamp);
                showAlert(`Backtest failed: ${data.error.split('\n')[0]}`, 'danger');
            } else {
                showAlert('Backtest failed with unknown error', 'danger');
            }
        }
        
    } catch (error) {
        console.error('Backtest error:', error);
        document.getElementById('backtest-status').textContent = 'Error';
        
        // Display network/connection error details
        const errorMsg = `Network/Connection Error: ${error.message}\n\nThis could be due to:\n• Server not running\n• Network connectivity issues\n• Server overload\n• Invalid request format`;
        displayDetailedError(errorMsg, 'NetworkError', error.message, new Date().toISOString());
        
        showAlert('Failed to run backtest', 'danger');
    } finally {
        // Reset button
        runBtn.innerHTML = originalText;
        runBtn.disabled = false;
        
        // Update status
        updateBacktestStatus();
    }
}

function displayDetailedError(errorMessage, errorType, errorDetails, timestamp) {
    console.log('=== displayDetailedError STARTED ===');
    console.log('Error details:', { errorMessage, errorType, errorDetails, timestamp });
    
    // Create or get the error display section
    let errorSection = document.getElementById('backtest-error-details');
    if (!errorSection) {
        errorSection = document.createElement('div');
        errorSection.id = 'backtest-error-details';
        errorSection.className = 'alert alert-danger mt-3';
        errorSection.style.cssText = 'white-space: pre-line; font-family: monospace; font-size: 0.9em;';
        
        // Insert after the backtest status section
        const statusSection = document.getElementById('backtest-status-info');
        if (statusSection) {
            statusSection.parentNode.insertBefore(errorSection, statusSection.nextSibling);
        } else {
            // Fallback: insert after the backtest configuration
            const configSection = document.querySelector('.backtester-section .col-md-6:first-child');
            if (configSection) {
                configSection.appendChild(errorSection);
            }
        }
    }
    
    // Format the error message for display
    let formattedError = '';
    
    if (errorMessage) {
        formattedError += errorMessage + '\n\n';
    }
    
    if (errorType) {
        formattedError += `Error Type: ${errorType}\n`;
    }
    
    if (errorDetails) {
        formattedError += `Details: ${errorDetails}\n`;
    }
    
    if (timestamp) {
        formattedError += `Time: ${timestamp}\n`;
    }
    
    // Update the error section
    errorSection.innerHTML = `
        <div class="d-flex justify-content-between align-items-start">
            <div>
                <strong><i class="bi bi-exclamation-triangle"></i> Backtest Error Details</strong>
                <div class="mt-2" style="white-space: pre-line;">${formattedError}</div>
            </div>
            <button type="button" class="btn-close" onclick="this.parentElement.parentElement.remove()"></button>
        </div>
    `;
    
    // Show the error section
    errorSection.style.display = 'block';
    
    // Show the clear errors button
    const clearErrorsBtn = document.getElementById('clear-errors');
    if (clearErrorsBtn) {
        clearErrorsBtn.style.display = 'block';
    }
    
    console.log('=== displayDetailedError COMPLETED ===');
}

function displayBacktestResults(results) {
    console.log('=== displayBacktestResults STARTED ===');
    console.log('displayBacktestResults called with:', results);
    console.log('Results type:', typeof results);
    console.log('Results is array?', Array.isArray(results));
    
    // Get the strategy results (should only be one since we clear on each run)
    const strategyNames = Object.keys(results);
    console.log('Strategy names found:', strategyNames);
    console.log('Strategy names length:', strategyNames.length);
    
    if (strategyNames.length === 0) {
        console.log('ERROR: No strategy names found');
        showAlert('No strategy results available', 'warning');
        return;
    }
    
    const strategyName = strategyNames[0];
    console.log('Using strategy name:', strategyName);
    
    const strategyResults = results[strategyName];
    console.log('Strategy results for', strategyName, ':', strategyResults);
    console.log('Strategy results type:', typeof strategyResults);
    console.log('Strategy results keys:', strategyResults ? Object.keys(strategyResults) : 'null');
    
    if (!strategyResults) {
        console.log('ERROR: No strategy results found');
        showAlert('No strategy results found', 'warning');
        return;
    }
    
    if (!strategyResults.metrics) {
        console.log('ERROR: No metrics found in strategy results');
        console.log('Available keys in strategyResults:', Object.keys(strategyResults));
        showAlert('No metrics found in results', 'warning');
        return;
    }
    
    console.log('✅ All validation passed, proceeding with display');
    
    const metrics = strategyResults.metrics;
    
    // Update the results section title to show which strategy was tested
    const resultsSection = document.getElementById('backtest-results');
    let strategyTitle = resultsSection.querySelector('.strategy-results-title');
    if (!strategyTitle) {
        // Create title element if it doesn't exist
        strategyTitle = document.createElement('div');
        strategyTitle.className = 'strategy-results-title mb-3';
        strategyTitle.style.cssText = 'font-weight: 600; color: var(--primary-color); font-size: 1.1em;';
        resultsSection.insertBefore(strategyTitle, resultsSection.firstChild);
    }
    // Get current interval for display
    const currentInterval = document.getElementById('backtest-interval');
    const intervalText = currentInterval ? currentInterval.options[currentInterval.selectedIndex].text : '1 Day';
    
    strategyTitle.innerHTML = `<i class="bi bi-graph-up"></i> Results for: ${strategyName} (${intervalText})`;
    
    // Update metric cards
    console.log('📊 Updating metric cards...');
    console.log('Metrics object:', metrics);
    
    try {
        updateMetricCard('total-return', metrics.total_return, true);
        console.log('✅ Updated total-return card');
        
        updateMetricCard('annual-return', metrics.annual_return, true);
        console.log('✅ Updated annual-return card');
        
        updateMetricCard('sharpe-ratio', metrics.sharpe_ratio, false);
        console.log('✅ Updated sharpe-ratio card');
        
        updateMetricCard('max-drawdown', metrics.max_drawdown, false, true);
        console.log('✅ Updated max-drawdown card');
        
        updateMetricCard('volatility', metrics.volatility, false);
        console.log('✅ Updated volatility card');
        
        updateMetricCard('signal-count', strategyResults.signal_count, false);
        console.log('✅ Updated signal-count card');
        
        console.log('📊 All metric cards updated successfully');
    } catch (error) {
        console.error('❌ Error updating metric cards:', error);
    }
    
    // Create performance chart
    console.log('📈 Creating performance chart...');
    console.log('Chart data:', strategyResults.chart_data);
    
    try {
        createBacktestChart(strategyResults.chart_data, strategyName);
        console.log('✅ Chart created successfully');
    } catch (error) {
        console.error('❌ Error creating chart:', error);
    }
    
    console.log(`✅ Displaying results for strategy: ${strategyName}`, metrics);
    console.log('=== displayBacktestResults COMPLETED ===');
}

// Global chart instance to prevent memory leaks
let backtestChart = null;

function createBacktestChart(chartData, strategyName) {
    console.log('📈 createBacktestChart called with:', { chartData, strategyName });
    
    const canvas = document.getElementById('backtest-chart');
    if (!canvas) {
        console.error('❌ Chart canvas not found');
        return;
    }
    
    console.log('✅ Canvas found:', canvas);
    
    // Validate chart data
    if (!chartData || !chartData.timestamps || !chartData.cumulative_returns) {
        console.error('❌ Invalid chart data structure:', chartData);
        return;
    }
    
    console.log('📊 Chart data validation passed');
    console.log('📊 Timestamps length:', chartData.timestamps.length);
    console.log('📊 OHLC data length:', chartData.ohlc_data ? chartData.ohlc_data.length : 0);
    console.log('📊 Returns length:', chartData.cumulative_returns.length);
    console.log('📊 Sample OHLC:', chartData.ohlc_data ? chartData.ohlc_data[0] : 'N/A');
    
    // Destroy existing chart if it exists
    if (backtestChart) {
        console.log('🗑️ Destroying existing chart');
        backtestChart.destroy();
        backtestChart = null;
    }
    
    // Clear the canvas context
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    
    // Ensure canvas has proper dimensions
    const container = canvas.parentElement;
    if (container) {
        canvas.style.width = '100%';
        canvas.style.height = '500px'; // Increased height for candlestick chart
        canvas.width = container.clientWidth || 800;
        canvas.height = 500;
    }
    
    console.log('📏 Canvas dimensions:', canvas.width, 'x', canvas.height);
    
    // Prepare datasets
    const datasets = [];
    
    // Check if candlestick plugin is available
    const hasCandlestickPlugin = typeof Chart.controllers.candlestick !== 'undefined';
    
    // Add candlestick data if available and plugin is loaded
    if (chartData.ohlc_data && chartData.ohlc_data.length > 0 && hasCandlestickPlugin) {
        console.log('📊 Adding candlestick data');
        datasets.push({
            label: 'Price (OHLC)',
            type: 'candlestick',
            data: chartData.ohlc_data,
            yAxisID: 'y',
            borderColor: {
                up: '#10b981',    // Green for bullish candles
                down: '#ef4444',  // Red for bearish candles
                unchanged: '#94a3b8'
            },
            backgroundColor: {
                up: 'rgba(16, 185, 129, 0.8)',
                down: 'rgba(239, 68, 68, 0.8)',
                unchanged: 'rgba(148, 163, 184, 0.8)'
            },
            borderWidth: 1,
            order: 1
        });
    } else {
        // Fallback to line chart if no OHLC data or no candlestick plugin
        const reason = !chartData.ohlc_data ? 'no OHLC data available' : 
                      chartData.ohlc_data.length === 0 ? 'empty OHLC data' : 
                      'candlestick plugin not loaded';
        console.log(`📊 Adding line chart (${reason})`);
        
        datasets.push({
            label: 'Price ($)',
            type: 'line',
            data: chartData.prices || [],
            borderColor: '#94a3b8',
            backgroundColor: 'rgba(148, 163, 184, 0.1)',
            borderWidth: 2,
            fill: false,
            yAxisID: 'y',
            tension: 0.1,
            order: 1
        });
    }
    
    // Add strategy returns
    datasets.push({
        label: 'Strategy Returns (%)',
        type: 'line',
        data: chartData.cumulative_returns,
        borderColor: '#4f46e5',
        backgroundColor: 'rgba(79, 70, 229, 0.1)',
        borderWidth: 3,
        fill: true,
        yAxisID: 'y1',
        tension: 0.1,
        order: 2
    });
    
    // Add buy signals
    if (chartData.buy_signals && chartData.buy_signals.length > 0) {
        console.log('📊 Adding buy signals:', chartData.buy_signals.length);
        
        datasets.push({
            label: '🟢 Buy Signals',
            type: 'scatter',
            data: chartData.buy_signals.map(signal => ({
                x: signal.x,
                y: signal.y
            })),
            backgroundColor: '#10b981',
            borderColor: '#10b981',
            pointRadius: 8,
            pointHoverRadius: 12,
            showLine: false,
            yAxisID: 'y',
            pointStyle: 'triangle',
            rotation: 0,
            order: 3
        });
    }
    
    // Add sell signals
    if (chartData.sell_signals && chartData.sell_signals.length > 0) {
        console.log('📊 Adding sell signals:', chartData.sell_signals.length);
        
        datasets.push({
            label: '🔴 Sell Signals',
            type: 'scatter',
            data: chartData.sell_signals.map(signal => ({
                x: signal.x,
                y: signal.y
            })),
            backgroundColor: '#ef4444',
            borderColor: '#ef4444',
            pointRadius: 8,
            pointHoverRadius: 12,
            showLine: false,
            yAxisID: 'y',
            pointStyle: 'triangle',
            rotation: 180,
            order: 3
        });
    }
    
    console.log('📊 Creating Chart.js instance with datasets:', datasets.length);
    
    try {
        // Check if candlestick chart type is available
        const chartType = hasCandlestickPlugin && chartData.ohlc_data && chartData.ohlc_data.length > 0 ? 'candlestick' : 'line';
        console.log('📊 Using chart type:', chartType);
        
        backtestChart = new Chart(ctx, {
            type: chartType,
            data: {
                labels: chartData.timestamps,
                datasets: datasets
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: {
                    duration: 1000,
                    easing: 'easeInOutQuart'
                },
                plugins: {
                    title: {
                        display: true,
                        text: `📈 ${strategyName} - Performance Analysis`,
                        color: '#f8fafc',
                        font: {
                            size: 18,
                            weight: 'bold',
                            family: "'Orbitron', monospace"
                        },
                        padding: 20
                    },
                    legend: {
                        display: true,
                        position: 'top',
                        labels: {
                            color: '#f8fafc',
                            font: {
                                family: "'Rajdhani', sans-serif",
                                size: 12
                            },
                            padding: 15,
                            usePointStyle: true
                        }
                    },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        backgroundColor: 'rgba(15, 23, 42, 0.95)',
                        titleColor: '#00ffff',
                        bodyColor: '#f8fafc',
                        borderColor: '#00ffff',
                        borderWidth: 1,
                        cornerRadius: 8,
                        titleFont: {
                            family: "'Rajdhani', sans-serif",
                            size: 14,
                            weight: 'bold'
                        },
                        bodyFont: {
                            family: "'Rajdhani', sans-serif",
                            size: 12
                        },
                        callbacks: {
                            title: function(context) {
                                if (context.length > 0) {
                                    const dataPoint = context[0];
                                    const index = dataPoint.dataIndex;
                                    
                                    // For candlestick data, use the raw x value
                                    if (dataPoint.dataset.type === 'candlestick' && dataPoint.raw && dataPoint.raw.x) {
                                        const date = new Date(dataPoint.raw.x);
                                        return date.toLocaleDateString('en-US', {
                                            month: 'short',
                                            day: 'numeric',
                                            year: 'numeric'
                                        });
                                    }
                                    
                                    // For other data types, use the label
                                    if (chartData.timestamps && chartData.timestamps[index]) {
                                        const date = new Date(chartData.timestamps[index]);
                                        return date.toLocaleDateString('en-US', {
                                            month: 'short',
                                            day: 'numeric',
                                            year: 'numeric'
                                        });
                                    }
                                }
                                return '';
                            },
                            label: function(context) {
                                const label = context.dataset.label || '';
                                
                                // Handle candlestick data
                                if (context.dataset.type === 'candlestick' && context.raw) {
                                    const ohlc = context.raw;
                                    return [
                                        `Open: $${ohlc.o.toFixed(2)}`,
                                        `High: $${ohlc.h.toFixed(2)}`,
                                        `Low: $${ohlc.l.toFixed(2)}`,
                                        `Close: $${ohlc.c.toFixed(2)}`
                                    ];
                                }
                                
                                // Handle other data types
                                const value = context.parsed.y;
                                if (value === null || value === undefined) return '';
                                
                                if (label.includes('Price')) {
                                    return `${label}: $${value.toFixed(2)}`;
                                } else if (label.includes('Returns')) {
                                    return `${label}: ${value.toFixed(2)}%`;
                                } else if (label.includes('Signal')) {
                                    return `${label}: $${value.toFixed(2)}`;
                                } else {
                                    return `${label}: $${value.toFixed(2)}`;
                                }
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        type: 'category',
                        grid: {
                            color: 'rgba(51, 65, 85, 0.3)',
                            drawOnChartArea: true
                        },
                        ticks: {
                            color: '#94a3b8',
                            font: {
                                family: "'Rajdhani', sans-serif",
                                size: 11
                            },
                            maxTicksLimit: 8,
                            autoSkip: true,
                            callback: function(value, index) {
                                // Show every nth label to avoid crowding
                                const labels = this.chart.data.labels;
                                const totalLabels = labels.length;
                                const skipInterval = Math.max(1, Math.floor(totalLabels / 8));
                                if (index % skipInterval === 0) {
                                    const date = new Date(labels[index]);
                                    return date.toLocaleDateString('en-US', { 
                                        month: 'short', 
                                        day: 'numeric'
                                    });
                                }
                                return '';
                            }
                        },
                        title: {
                            display: true,
                            text: 'Date',
                            color: '#94a3b8',
                            font: {
                                family: "'Rajdhani', sans-serif",
                                size: 12,
                                weight: 'bold'
                            }
                        }
                    },
                    y: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        grid: {
                            color: 'rgba(51, 65, 85, 0.3)',
                            drawOnChartArea: true
                        },
                        ticks: {
                            color: '#94a3b8',
                            font: {
                                family: "'Rajdhani', sans-serif",
                                size: 11
                            },
                            callback: function(value) {
                                return '$' + value.toFixed(0);
                            }
                        },
                        title: {
                            display: true,
                            text: 'Price ($)',
                            color: '#94a3b8',
                            font: {
                                family: "'Rajdhani', sans-serif",
                                size: 12,
                                weight: 'bold'
                            }
                        }
                    },
                    y1: {
                        type: 'linear',
                        display: true,
                        position: 'right',
                        grid: {
                            drawOnChartArea: false,
                        },
                        ticks: {
                            color: '#4f46e5',
                            font: {
                                family: "'Rajdhani', sans-serif",
                                size: 11
                            },
                            callback: function(value) {
                                return value.toFixed(1) + '%';
                            }
                        },
                        title: {
                            display: true,
                            text: 'Strategy Returns (%)',
                            color: '#4f46e5',
                            font: {
                                family: "'Rajdhani', sans-serif",
                                size: 12,
                                weight: 'bold'
                            }
                        }
                    }
                },
                interaction: {
                    mode: 'nearest',
                    axis: 'x',
                    intersect: false
                }
            }
        });
        
        console.log('✅ Chart created successfully:', backtestChart);
        
        // Force a resize to ensure proper rendering
        setTimeout(() => {
            if (backtestChart) {
                backtestChart.resize();
                console.log('📏 Chart resized');
            }
        }, 100);
        
    } catch (error) {
        console.error('❌ Error creating chart:', error);
        console.error('❌ Chart.js error details:', error.stack);
        
        // Show error message to user
        const chartContainer = canvas.parentElement;
        if (chartContainer) {
            chartContainer.innerHTML = `
                <div style="padding: 40px; text-align: center; color: #ef4444;">
                    <h4>📊 Chart Loading Error</h4>
                    <p>Unable to render the backtest chart. Please try again.</p>
                    <p style="font-size: 12px; color: #94a3b8;">Error: ${error.message}</p>
                </div>
            `;
        }
    }
}

function updateMetricCard(elementId, value, isPercentage = false, isNegative = false) {
    const element = document.getElementById(elementId);
    if (!element) return;
    
    let displayValue;
    let className = '';
    
    if (typeof value === 'number') {
        if (isPercentage) {
            displayValue = (value * 100).toFixed(2) + '%';
            className = value > 0 ? 'positive' : 'negative';
        } else if (elementId === 'sharpe-ratio') {
            displayValue = value.toFixed(2);
            className = value > 1 ? 'positive' : value > 0.5 ? '' : 'negative';
        } else if (elementId === 'volatility') {
            displayValue = (value * 100).toFixed(2) + '%';
        } else {
            displayValue = value.toString();
        }
        
        if (isNegative) {
            className = value < 0 ? 'negative' : 'positive';
            displayValue = Math.abs(value * 100).toFixed(2) + '%';
        }
    } else {
        displayValue = value.toString();
    }
    
    element.textContent = displayValue;
    element.className = 'metric-value ' + className;
}

async function updateBacktestStatus() {
    try {
        const response = await fetch('/api/backtest/status');
        const status = await response.json();
        
        document.getElementById('backtest-data-points').textContent = status.data_length || 0;
        document.getElementById('backtest-strategies-count').textContent = status.strategies_run.length || 0;
        
        if (!status.has_data && document.getElementById('backtest-status').textContent === 'Running...') {
            // Don't update if currently running
            return;
        }
        
        if (status.has_results) {
            document.getElementById('backtest-status').textContent = 'Ready (Results Available)';
        } else {
            document.getElementById('backtest-status').textContent = 'Ready';
        }
        
    } catch (error) {
        console.error('Error updating backtest status:', error);
    }
}

// Quick date range preset function
window.setDateRange = function(range) {
    const endDate = new Date();
    const startDate = new Date();
    
    // Calculate start date based on range
    switch(range) {
        case '1m':
            startDate.setMonth(endDate.getMonth() - 1);
            break;
        case '3m':
            startDate.setMonth(endDate.getMonth() - 3);
            break;
        case '6m':
            startDate.setMonth(endDate.getMonth() - 6);
            break;
        case '1y':
            startDate.setFullYear(endDate.getFullYear() - 1);
            break;
    }
    
    // Format dates as YYYY-MM-DD
    const formatDate = (date) => {
        return date.toISOString().split('T')[0];
    };
    
    // Update the date inputs
    document.getElementById('backtest-start-date').value = formatDate(startDate);
    document.getElementById('backtest-end-date').value = formatDate(endDate);
    
    // Update the status display
    updateDateRangeDisplay();
    
    // Show feedback
    const rangeNames = {
        '1m': 'Last Month',
        '3m': 'Last 3 Months', 
        '6m': 'Last 6 Months',
        '1y': 'Last Year'
    };
    
    showAlert(`Date range set to: ${rangeNames[range]}`, 'info');
};

// Update date range display in status
function updateDateRangeDisplay() {
    const startDate = document.getElementById('backtest-start-date').value;
    const endDate = document.getElementById('backtest-end-date').value;
    const dateRangeElement = document.getElementById('backtest-date-range');
    
    if (dateRangeElement && startDate && endDate) {
        dateRangeElement.textContent = `${startDate} to ${endDate}`;
    }
}

// Update interval display in status
function updateIntervalDisplay() {
    const intervalSelect = document.getElementById('backtest-interval');
    const intervalDisplayElement = document.getElementById('backtest-interval-display');
    
    if (intervalSelect && intervalDisplayElement) {
        const selectedText = intervalSelect.options[intervalSelect.selectedIndex].text;
        intervalDisplayElement.textContent = selectedText;
        
        // Add interval-specific validation or warnings
        const selectedValue = intervalSelect.value;
        const startDate = document.getElementById('backtest-start-date').value;
        const endDate = document.getElementById('backtest-end-date').value;
        
        if (startDate && endDate) {
            const start = new Date(startDate);
            const end = new Date(endDate);
            const daysDiff = Math.ceil((end - start) / (1000 * 60 * 60 * 24));
            
            // Show warnings for problematic interval/period combinations
            if ((selectedValue === '1m' || selectedValue === '2m') && daysDiff > 7) {
                showAlert('Warning: Very short intervals (1m-2m) work best with periods of 7 days or less for optimal performance', 'warning');
            } else if ((selectedValue === '5m' || selectedValue === '15m') && daysDiff > 30) {
                showAlert('Note: Short intervals (5m-15m) are recommended for periods up to 30 days for best results', 'info');
            } else if (selectedValue === '1mo' && daysDiff < 365) {
                showAlert('Tip: Monthly intervals work best with periods of 1 year or more for meaningful analysis', 'info');
            }
        }
    }
}