/**
 * Markets view — live stock & crypto dashboard.
 *
 * Stocks  → proxied through /api/markets/quote  (Yahoo Finance, CORS-blocked)
 * Crypto  → fetched directly from CoinGecko      (CORS-open free tier)
 * Search  → proxied through /api/markets/search  (Yahoo Finance, CORS-blocked)
 * Sparklines for stocks → /api/markets/sparklines (Yahoo v8 chart, 7d daily)
 * Sparklines for crypto → embedded in CoinGecko response (7d hourly)
 *
 * Watchlist stored in localStorage under key "markets_watchlist".
 */
window.MarketsView = (() => {
    'use strict';

    // ── Constants ────────────────────────────────────────────────────────────
    const BACKEND    = '/api/markets';
    const COINGECKO  = 'https://api.coingecko.com/api/v3';
    const REFRESH_MS = 60_000;
    const LS_KEY     = 'markets_watchlist';

    // ── Crypto catalogue (symbol → CoinGecko ID + display name) ─────────────
    const CRYPTO_CATALOGUE = [
        { symbol: 'BTC',   name: 'Bitcoin',           id: 'bitcoin' },
        { symbol: 'ETH',   name: 'Ethereum',           id: 'ethereum' },
        { symbol: 'SOL',   name: 'Solana',             id: 'solana' },
        { symbol: 'XRP',   name: 'XRP / Ripple',       id: 'ripple' },
        { symbol: 'ADA',   name: 'Cardano',            id: 'cardano' },
        { symbol: 'DOT',   name: 'Polkadot',           id: 'polkadot' },
        { symbol: 'AVAX',  name: 'Avalanche',          id: 'avalanche-2' },
        { symbol: 'LINK',  name: 'Chainlink',          id: 'chainlink' },
        { symbol: 'MATIC', name: 'Polygon / MATIC',    id: 'matic-network' },
        { symbol: 'BNB',   name: 'BNB / Binance Coin', id: 'binancecoin' },
        { symbol: 'DOGE',  name: 'Dogecoin',           id: 'dogecoin' },
        { symbol: 'SHIB',  name: 'Shiba Inu',          id: 'shiba-inu' },
        { symbol: 'LTC',   name: 'Litecoin',           id: 'litecoin' },
        { symbol: 'UNI',   name: 'Uniswap',            id: 'uniswap' },
        { symbol: 'ATOM',  name: 'Cosmos',             id: 'cosmos' },
        { symbol: 'NEAR',  name: 'NEAR Protocol',      id: 'near' },
        { symbol: 'ARB',   name: 'Arbitrum',           id: 'arbitrum' },
        { symbol: 'OP',    name: 'Optimism',           id: 'optimism' },
        { symbol: 'SUI',   name: 'Sui',                id: 'sui' },
        { symbol: 'TON',   name: 'Toncoin',            id: 'the-open-network' },
    ];

    // Build the legacy CRYPTO_IDS map from the catalogue (backward compat)
    const CRYPTO_IDS = Object.fromEntries(CRYPTO_CATALOGUE.map(c => [c.symbol, c.id]));

    // ── Curated stock list for instant local search ──────────────────────────
    const POPULAR_STOCKS = [
        // US mega-cap
        { symbol: 'AAPL',   name: 'Apple Inc.',               exchange: 'NASDAQ' },
        { symbol: 'MSFT',   name: 'Microsoft Corporation',     exchange: 'NASDAQ' },
        { symbol: 'NVDA',   name: 'NVIDIA Corporation',        exchange: 'NASDAQ' },
        { symbol: 'GOOGL',  name: 'Alphabet Inc.',             exchange: 'NASDAQ' },
        { symbol: 'AMZN',   name: 'Amazon.com Inc.',           exchange: 'NASDAQ' },
        { symbol: 'META',   name: 'Meta Platforms Inc.',        exchange: 'NASDAQ' },
        { symbol: 'TSLA',   name: 'Tesla Inc.',                exchange: 'NASDAQ' },
        { symbol: 'AVGO',   name: 'Broadcom Inc.',             exchange: 'NASDAQ' },
        { symbol: 'LLY',    name: 'Eli Lilly and Co.',         exchange: 'NYSE' },
        { symbol: 'JPM',    name: 'JPMorgan Chase & Co.',      exchange: 'NYSE' },
        { symbol: 'V',      name: 'Visa Inc.',                 exchange: 'NYSE' },
        { symbol: 'MA',     name: 'Mastercard Inc.',           exchange: 'NYSE' },
        { symbol: 'XOM',    name: 'Exxon Mobil Corp.',         exchange: 'NYSE' },
        { symbol: 'WMT',    name: 'Walmart Inc.',              exchange: 'NYSE' },
        { symbol: 'UNH',    name: 'UnitedHealth Group',        exchange: 'NYSE' },
        { symbol: 'JNJ',    name: 'Johnson & Johnson',         exchange: 'NYSE' },
        { symbol: 'PG',     name: 'Procter & Gamble Co.',      exchange: 'NYSE' },
        { symbol: 'HD',     name: 'Home Depot Inc.',           exchange: 'NYSE' },
        { symbol: 'COST',   name: 'Costco Wholesale Corp.',    exchange: 'NASDAQ' },
        { symbol: 'AMD',    name: 'Advanced Micro Devices',    exchange: 'NASDAQ' },
        { symbol: 'NFLX',   name: 'Netflix Inc.',              exchange: 'NASDAQ' },
        { symbol: 'INTC',   name: 'Intel Corporation',         exchange: 'NASDAQ' },
        { symbol: 'QCOM',   name: 'Qualcomm Inc.',             exchange: 'NASDAQ' },
        { symbol: 'CRM',    name: 'Salesforce Inc.',           exchange: 'NYSE' },
        { symbol: 'ADBE',   name: 'Adobe Inc.',                exchange: 'NASDAQ' },
        { symbol: 'PYPL',   name: 'PayPal Holdings Inc.',      exchange: 'NASDAQ' },
        { symbol: 'BA',     name: 'Boeing Co.',                exchange: 'NYSE' },
        { symbol: 'DIS',    name: 'Walt Disney Co.',           exchange: 'NYSE' },
        { symbol: 'NKE',    name: 'Nike Inc.',                 exchange: 'NYSE' },
        { symbol: 'BRK-B',  name: 'Berkshire Hathaway B',     exchange: 'NYSE' },
        { symbol: 'GS',     name: 'Goldman Sachs Group',       exchange: 'NYSE' },
        { symbol: 'MS',     name: 'Morgan Stanley',            exchange: 'NYSE' },
        { symbol: 'COIN',   name: 'Coinbase Global Inc.',      exchange: 'NASDAQ' },
        { symbol: 'MSTR',   name: 'MicroStrategy Inc.',        exchange: 'NASDAQ' },
        { symbol: 'PLTR',   name: 'Palantir Technologies',     exchange: 'NYSE' },
        { symbol: 'HOOD',   name: 'Robinhood Markets',         exchange: 'NASDAQ' },
        // European
        { symbol: 'RHM.DE',  name: 'Rheinmetall AG',           exchange: 'XETRA' },
        { symbol: 'SAP.DE',  name: 'SAP SE',                   exchange: 'XETRA' },
        { symbol: 'SIE.DE',  name: 'Siemens AG',               exchange: 'XETRA' },
        { symbol: 'BMW.DE',  name: 'BMW AG',                   exchange: 'XETRA' },
        { symbol: 'ASML.AS', name: 'ASML Holding NV',          exchange: 'AMS' },
        { symbol: 'LVMH.PA', name: 'LVMH Moët Hennessy',       exchange: 'PARIS' },
        { symbol: 'NOVO-B.CO', name: 'Novo Nordisk A/S',       exchange: 'CPH' },
        // ETFs
        { symbol: 'SPY',    name: 'SPDR S&P 500 ETF',          exchange: 'NYSE Arca' },
        { symbol: 'QQQ',    name: 'Invesco QQQ Trust',          exchange: 'NASDAQ' },
        { symbol: 'VTI',    name: 'Vanguard Total Stock Mkt ETF', exchange: 'NYSE Arca' },
        { symbol: 'VWCE.DE', name: 'Vanguard FTSE All-World ETF', exchange: 'XETRA' },
        { symbol: 'GLD',    name: 'SPDR Gold Trust ETF',        exchange: 'NYSE Arca' },
        { symbol: 'ARKK',   name: 'ARK Innovation ETF',         exchange: 'NYSE Arca' },
        { symbol: 'IUIT.L', name: 'iShares S&P 500 IT ETF',    exchange: 'LSE' },
    ];

    const DEFAULTS = {
        stocks: ['AAPL', 'TSLA', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'RHM.DE'],
        crypto: ['BTC', 'ETH', 'SOL', 'XRP'],
    };

    // ── Range config ─────────────────────────────────────────────────────────
    const RANGES = {
        '7d':  { label: '7D',    xSlots: ['[T-7D]','[T-6D]','[T-5D]','[T-4D]','[T-3D]','[T-2D]','[T-1D]','[NOW]'] },
        '1mo': { label: '1M',    xSlots: ['[T-30D]','[T-25D]','[T-20D]','[T-15D]','[T-10D]','[T-5D]','[T-2D]','[NOW]'] },
        '3mo': { label: '3M',    xSlots: ['[T-90D]','[T-75D]','[T-60D]','[T-45D]','[T-30D]','[T-15D]','[T-5D]','[NOW]'] },
        '6mo': { label: '6M',    xSlots: ['[T-6M]','[T-5M]','[T-4M]','[T-3M]','[T-2M]','[T-1M]','[T-2W]','[NOW]'] },
        '1y':  { label: '1Y',    xSlots: ['[T-12M]','[T-10M]','[T-8M]','[T-6M]','[T-4M]','[T-2M]','[T-1M]','[NOW]'] },
        'max': { label: 'START', xSlots: null },   // dynamic — built from position dates
    };

    // ── Module state (persists across mount/unmount cycles) ──────────────────
    const state = {
        stocks:            [],
        crypto:            [],
        sparklines:        {},      // 7d sparklines (watchlist)
        sparklinesByRange: {},      // { '7d': {SYM: [...]}, '1mo': {...}, ... }
        watchlistStocks:   [],
        watchlistCrypto:   [],
        lastUpdated:       null,
        refreshTimer:      null,
        loading:           false,
        filter:            'all',
        activeTab:         'watchlist',
        chartRange:        '7d',    // currently selected portfolio range
    };

    // ── Portfolio persistence ────────────────────────────────────────────────
    const PORTFOLIO_KEY = 'markets_portfolio';

    function loadPortfolio() {
        try { return JSON.parse(localStorage.getItem(PORTFOLIO_KEY) || '[]'); }
        catch { return []; }
    }

    function savePortfolio(positions) {
        localStorage.setItem(PORTFOLIO_KEY, JSON.stringify(positions));
    }

    // ── Watchlist persistence ────────────────────────────────────────────────
    function loadWatchlist() {
        try {
            const saved = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
            state.watchlistStocks = Array.isArray(saved.stocks) ? saved.stocks : [...DEFAULTS.stocks];
            state.watchlistCrypto = Array.isArray(saved.crypto) ? saved.crypto : [...DEFAULTS.crypto];
        } catch {
            state.watchlistStocks = [...DEFAULTS.stocks];
            state.watchlistCrypto = [...DEFAULTS.crypto];
        }
    }

    function saveWatchlist() {
        localStorage.setItem(LS_KEY, JSON.stringify({
            stocks: state.watchlistStocks,
            crypto: state.watchlistCrypto,
        }));
    }

    // ── Data fetching ────────────────────────────────────────────────────────
    async function fetchStocks() {
        if (!state.watchlistStocks.length) { state.stocks = []; return; }
        const syms = state.watchlistStocks.join(',');
        const [quoteRes, sparkRes] = await Promise.allSettled([
            fetch(`${BACKEND}/quote?symbols=${syms}`).then(r => r.json()),
            fetch(`${BACKEND}/sparklines?symbols=${syms}`).then(r => r.json()),
        ]);
        if (quoteRes.status === 'fulfilled' && quoteRes.value.quotes) {
            state.stocks = quoteRes.value.quotes;
        }
        if (sparkRes.status === 'fulfilled' && sparkRes.value.sparklines) {
            Object.assign(state.sparklines, sparkRes.value.sparklines);
        }
    }

    async function fetchCrypto() {
        if (!state.watchlistCrypto.length) { state.crypto = []; return; }
        const ids = state.watchlistCrypto
            .map(s => CRYPTO_IDS[s.toUpperCase()])
            .filter(Boolean)
            .join(',');
        if (!ids) return;
        const r = await fetch(
            `${COINGECKO}/coins/markets?vs_currency=usd&ids=${ids}` +
            `&sparkline=true&price_change_percentage=24h,7d&per_page=25&order=market_cap_desc`
        );
        if (!r.ok) throw new Error(`CoinGecko ${r.status}`);
        const coins = await r.json();
        state.crypto = coins;
        const symById = Object.fromEntries(
            Object.entries(CRYPTO_IDS).map(([sym, id]) => [id, sym])
        );
        for (const c of coins) {
            const sym = symById[c.id] || c.symbol.toUpperCase();
            const prices = c.sparkline_in_7d?.price;
            if (prices?.length) state.sparklines[sym] = prices;
        }
    }

    /**
     * Fetch sparklines for a given range and store in state.sparklinesByRange[range].
     * Returns immediately if data is already cached.
     */
    async function fetchRangeSparklines(range) {
        if (state.sparklinesByRange[range]) return;  // already cached

        const positions  = loadPortfolio();
        const stockSyms  = positions.filter(p => p.type === 'stock').map(p => p.symbol);
        const cryptoSyms = positions.filter(p => p.type === 'crypto').map(p => p.symbol);

        const cache = {};
        const fetches = [];

        if (stockSyms.length) {
            fetches.push(
                fetch(`${BACKEND}/sparklines?symbols=${stockSyms.join(',')}&range=${range}`)
                    .then(r => r.json())
                    .then(d => { Object.assign(cache, d.sparklines || {}); })
                    .catch(() => {})
            );
        }

        // Crypto sparklines for longer ranges — also use backend proxy via
        // CoinGecko's market_chart endpoint (7d only), so fall back to existing
        // sparkline data for crypto on non-7d ranges (CoinGecko free tier limit).
        if (cryptoSyms.length) {
            if (range === '7d') {
                // Already in state.sparklines
                for (const sym of cryptoSyms) {
                    if (state.sparklines[sym]) cache[sym] = state.sparklines[sym];
                }
            } else {
                // For non-7d ranges, try the Yahoo proxy (works for crypto tickers
                // like BTC-USD if present) — silently fall back to 7d data.
                const yahooSyms = cryptoSyms.map(s => {
                    // CoinGecko symbols → Yahoo ticker heuristic: BTC → BTC-USD
                    return s + '-USD';
                });
                fetches.push(
                    fetch(`${BACKEND}/sparklines?symbols=${yahooSyms.join(',')}&range=${range}`)
                        .then(r => r.json())
                        .then(d => {
                            for (const [ytsym, prices] of Object.entries(d.sparklines || {})) {
                                const baseSym = ytsym.replace(/-USD$/i, '');
                                if (prices && prices.length > 1) cache[baseSym] = prices;
                                else if (state.sparklines[baseSym]) cache[baseSym] = state.sparklines[baseSym];
                            }
                        })
                        .catch(() => {
                            // Fall back: copy existing 7d data
                            for (const sym of cryptoSyms) {
                                if (state.sparklines[sym]) cache[sym] = state.sparklines[sym];
                            }
                        })
                );
            }
        }

        if (fetches.length) await Promise.allSettled(fetches);
        state.sparklinesByRange[range] = cache;
    }

    async function fetchPortfolioData() {
        const positions = loadPortfolio();
        if (!positions.length) return;

        const extraStocks = positions
            .filter(p => p.type === 'stock' && !state.watchlistStocks.includes(p.symbol))
            .map(p => p.symbol);

        const extraCryptoSyms = positions
            .filter(p => p.type === 'crypto')
            .map(p => p.symbol)
            .filter(s => CRYPTO_IDS[s] && !state.crypto.find(c => c.id === CRYPTO_IDS[s]));

        const fetches = [];

        if (extraStocks.length) {
            const syms = extraStocks.join(',');
            fetches.push(
                fetch(`${BACKEND}/quote?symbols=${syms}`).then(r => r.json())
                    .then(d => {
                        if (d.quotes) {
                            state.stocks.push(...d.quotes.filter(q =>
                                !state.stocks.find(s => s.symbol === q.symbol)));
                        }
                    }).catch(() => {}),
                fetch(`${BACKEND}/sparklines?symbols=${syms}`).then(r => r.json())
                    .then(d => { if (d.sparklines) Object.assign(state.sparklines, d.sparklines); })
                    .catch(() => {})
            );
        }

        if (extraCryptoSyms.length) {
            const ids = extraCryptoSyms.map(s => CRYPTO_IDS[s]).filter(Boolean).join(',');
            if (ids) {
                fetches.push(
                    fetch(`${COINGECKO}/coins/markets?vs_currency=usd&ids=${ids}` +
                          `&sparkline=true&price_change_percentage=24h,7d&per_page=25&order=market_cap_desc`)
                        .then(r => r.ok ? r.json() : [])
                        .then(coins => {
                            const symById = Object.fromEntries(
                                Object.entries(CRYPTO_IDS).map(([s, id]) => [id, s]));
                            for (const c of coins) {
                                if (!state.crypto.find(x => x.id === c.id)) state.crypto.push(c);
                                const sym = symById[c.id] || c.symbol.toUpperCase();
                                if (c.sparkline_in_7d?.price?.length)
                                    state.sparklines[sym] = c.sparkline_in_7d.price;
                            }
                        }).catch(() => {})
                );
            }
        }

        if (fetches.length) await Promise.allSettled(fetches);
    }

    async function refresh() {
        if (state.loading) return;
        state.loading = true;
        setRefreshDisabled(true);
        try {
            await Promise.allSettled([fetchStocks(), fetchCrypto()]);
            await fetchPortfolioData();
            state.lastUpdated = new Date();
        } catch (err) {
            console.warn('[Markets] refresh error:', err);
        } finally {
            state.loading = false;
            setRefreshDisabled(false);
            renderGrid();
            renderLastUpdated();
            if (state.activeTab === 'portfolio') renderPortfolio();
        }
    }

    // ── Color palette (Edgerunner) ───────────────────────────────────────────
    const COL_UP   = '#00f0ff';                      // electric cyan  — gain
    const COL_DN   = '#ff003c';                      // tactical red   — loss
    const FILL_UP  = 'rgba(0,240,255,0.07)';
    const FILL_DN  = 'rgba(255,0,60,0.07)';

    /**
     * Convert a flat array of "x,y" coordinate strings into a stepped
     * (horizontal-first) polyline string — gives the jagged terminal look.
     */
    function _stepPolyline(coordPairs) {
        const parsed = coordPairs.map(p => p.split(',').map(Number));
        const out = [];
        for (let i = 0; i < parsed.length; i++) {
            out.push(`${parsed[i][0].toFixed(1)},${parsed[i][1].toFixed(1)}`);
            if (i < parsed.length - 1) {
                // Horizontal leg to next x at current y  (the "step")
                out.push(`${parsed[i + 1][0].toFixed(1)},${parsed[i][1].toFixed(1)}`);
            }
        }
        return out.join(' ');
    }

    // ── SVG sparkline renderer (stepped / jagged) ────────────────────────────
    function sparklineSVG(prices, up) {
        let pts = prices;
        if (pts.length > 40) {
            const step = Math.ceil(pts.length / 40);
            pts = pts.filter((_, i) => i % step === 0);
        }
        if (pts.length < 2) return '<svg class="mkt-spark" viewBox="0 0 90 36"></svg>';

        const min   = Math.min(...pts);
        const max   = Math.max(...pts);
        const range = max - min || 1;
        const W = 90, H = 36, PAD = 2;

        const rawPairs = pts.map((p, i) => {
            const x = PAD + (i / (pts.length - 1)) * (W - PAD * 2);
            const y = H - PAD - ((p - min) / range) * (H - PAD * 2);
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        });

        const steppedPts = _stepPolyline(rawPairs);
        const col  = up ? COL_UP  : COL_DN;
        const fill = up ? FILL_UP : FILL_DN;
        const fillPts = `${PAD},${H} ${steppedPts} ${W - PAD},${H}`;

        return `<svg class="mkt-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
            <polygon points="${fillPts}" fill="${fill}"/>
            <polyline points="${steppedPts}" fill="none" stroke="${col}"
                      stroke-width="1.4" stroke-linecap="square" stroke-linejoin="miter"/>
        </svg>`;
    }

    // ── Formatting helpers ───────────────────────────────────────────────────
    function fmtPrice(price, currency) {
        if (price == null) return '—';
        const sym = currency === 'USD' ? '$'
                  : currency === 'EUR' ? '€'
                  : currency === 'GBP' ? '£'
                  : (currency || '') + ' ';
        if (price >= 10_000) return sym + price.toLocaleString('en-US', { maximumFractionDigits: 0 });
        if (price >= 100)    return sym + price.toFixed(2);
        if (price >= 1)      return sym + price.toFixed(3);
        if (price >= 0.001)  return sym + price.toFixed(5);
        return sym + price.toFixed(8);
    }

    function fmtPct(pct) {
        if (pct == null) return '';
        const sign = pct >= 0 ? '+' : '';
        return `${sign}${pct.toFixed(2)}%`;
    }

    function esc(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // ── Micro-data helpers ───────────────────────────────────────────────────
    /** Generate a deterministic hex ID from a ticker symbol. */
    function _hexId(sym) {
        let h = 0;
        for (let i = 0; i < sym.length; i++) h = (h * 31 + sym.charCodeAt(i)) & 0xffff;
        return '0x' + h.toString(16).toUpperCase().padStart(4, '0');
    }

    /** Block-meter bar (6 chars) showing 52-week position. */
    function _blockBar(pct, width = 6) {
        const filled = Math.round(Math.max(0, Math.min(1, pct / 100)) * width);
        return '█'.repeat(filled) + '▒'.repeat(width - filled);
    }

    // ── Card renderers ───────────────────────────────────────────────────────
    function stockCard(q) {
        const up    = (q.change_pct || 0) >= 0;
        const dir   = up ? '▲' : '▼';
        const cls   = up ? 'mkt-up' : 'mkt-dn';
        const spark = sparklineSVG(state.sparklines[q.symbol] || [], up);
        const ms    = q.market_state;
        const badge = (ms && ms !== 'REGULAR')
            ? `<span class="mkt-state-badge">${esc(ms)}</span>` : '';

        const absHtml = q.change != null
            ? `<span class="mkt-chg-abs ${cls}">${up ? '+' : ''}${fmtPrice(Math.abs(q.change), q.currency)}</span>`
            : '';

        // 52-week range with block meter
        let rangeHtml = '';
        const lo = q.week52_low, hi = q.week52_high, px = q.price;
        if (lo != null && hi != null && px != null && hi > lo) {
            const pos52 = Math.max(0, Math.min(100, ((px - lo) / (hi - lo)) * 100));
            rangeHtml = `
            <div class="mkt-52w">
                <div class="mkt-micro-meter">${_blockBar(pos52)}</div>
                <div class="mkt-range-track">
                    <div class="mkt-range-fill" style="width:${pos52.toFixed(1)}%"></div>
                    <div class="mkt-range-dot"  style="left:${pos52.toFixed(1)}%"></div>
                </div>
                <div class="mkt-52w-labels">
                    <span>${fmtPrice(lo, q.currency)}</span>
                    <span class="mkt-52w-mid">52W</span>
                    <span>${fmtPrice(hi, q.currency)}</span>
                </div>
            </div>`;
        }

        const micro = `<div class="mkt-micro-data">
            <span class="mkt-micro-id">${_hexId(q.symbol)}</span>
            <span class="mkt-micro-exch">${esc(q.exchange || 'EQ')}</span>
        </div>`;

        return `<div class="mkt-card mkt-card--${up ? 'up' : 'dn'}"
                     data-sym="${esc(q.symbol)}" data-type="stock">
            <button class="mkt-remove" data-sym="${esc(q.symbol)}" data-type="stock"
                    title="Remove from watchlist" aria-label="Remove">×</button>
            <div class="mkt-card-head">
                <span class="mkt-sym">${esc(q.symbol)}</span>
                ${badge}
            </div>
            <div class="mkt-card-subhead">
                <span class="mkt-name" title="${esc(q.name || '')}">${esc(q.name || '')}</span>
            </div>
            <div class="mkt-card-body">
                <div class="mkt-price-block">
                    <span class="mkt-price" data-price-el="${esc(q.symbol)}">${fmtPrice(q.price, q.currency)}</span>
                    <div class="mkt-changes">
                        <span class="mkt-chg ${cls}">${dir} ${fmtPct(q.change_pct)}</span>
                        ${absHtml}
                    </div>
                </div>
                ${spark}
            </div>
            ${rangeHtml}
            ${micro}
        </div>`;
    }

    function cryptoCard(c) {
        const sym   = c.symbol.toUpperCase();
        const pct24 = c.price_change_percentage_24h;
        const pct7d = c.price_change_percentage_7d_in_currency;
        const up    = (pct24 || 0) >= 0;
        const dir   = up ? '▲' : '▼';
        const cls   = up ? 'mkt-up' : 'mkt-dn';
        const spark = sparklineSVG(state.sparklines[sym] || c.sparkline_in_7d?.price || [], up);
        const img   = c.image
            ? `<img class="mkt-coin-img" src="${esc(c.image)}" alt="${sym}" loading="lazy">`
            : '';

        const tag7d = pct7d != null
            ? `<span class="mkt-chg-7d ${pct7d >= 0 ? 'mkt-up' : 'mkt-dn'}">${pct7d >= 0 ? '+' : ''}${pct7d.toFixed(2)}% 7d</span>`
            : '';

        const cap = c.market_cap
            ? (c.market_cap >= 1e12 ? `$${(c.market_cap / 1e12).toFixed(2)}T`
             : c.market_cap >= 1e9  ? `$${(c.market_cap / 1e9).toFixed(1)}B`
             : c.market_cap >= 1e6  ? `$${(c.market_cap / 1e6).toFixed(0)}M`
             : null)
            : null;

        // 24h position within 7d range (rough estimate from sparkline)
        const sparkPrices = state.sparklines[sym] || c.sparkline_in_7d?.price || [];
        let blockMeter = '';
        if (sparkPrices.length >= 2) {
            const lo7 = Math.min(...sparkPrices);
            const hi7 = Math.max(...sparkPrices);
            const px7 = sparkPrices[sparkPrices.length - 1];
            const pct7 = hi7 > lo7 ? ((px7 - lo7) / (hi7 - lo7)) * 100 : 50;
            blockMeter = `<div class="mkt-micro-meter">${_blockBar(pct7)}</div>`;
        }

        const micro = `<div class="mkt-micro-data">
            <span class="mkt-micro-id">${_hexId(sym)}</span>
            <span class="mkt-micro-exch">CRYPTO</span>
        </div>`;

        return `<div class="mkt-card mkt-card--${up ? 'up' : 'dn'}"
                     data-sym="${sym}" data-type="crypto">
            <button class="mkt-remove" data-sym="${sym}" data-type="crypto"
                    title="Remove from watchlist" aria-label="Remove">×</button>
            <div class="mkt-card-head">
                ${img}
                <span class="mkt-sym">${sym}</span>
                <span class="mkt-rank">#${c.market_cap_rank || '?'}</span>
            </div>
            <div class="mkt-card-subhead">
                <span class="mkt-name" title="${esc(c.name)}">${esc(c.name)}</span>
            </div>
            <div class="mkt-card-body">
                <div class="mkt-price-block">
                    <span class="mkt-price" data-price-el="${sym}">${fmtPrice(c.current_price, 'USD')}</span>
                    <div class="mkt-changes">
                        <span class="mkt-chg ${cls}">${dir} ${fmtPct(pct24)}</span>
                        ${tag7d}
                    </div>
                </div>
                ${spark}
            </div>
            ${cap ? `<div class="mkt-crypto-foot">
                ${blockMeter}
                <span class="mkt-cap-label">MCAP</span>
                <span class="mkt-cap-val">${cap}</span>
            </div>` : ''}
            ${micro}
        </div>`;
    }

    // ── Render ───────────────────────────────────────────────────────────────
    function renderGrid() {
        const grid = document.getElementById('markets-grid');
        if (!grid) return;

        const f     = state.filter;
        const parts = [];

        if (f !== 'crypto' && state.stocks.length) {
            parts.push('<div class="mkt-section-label">// EQUITIES &amp; ETFS</div>');
            parts.push(...state.stocks.map(stockCard));
        }
        if (f !== 'stocks' && state.crypto.length) {
            parts.push('<div class="mkt-section-label">// CRYPTO</div>');
            parts.push(...state.crypto.map(cryptoCard));
        }

        if (!parts.length) {
            if (state.loading) {
                parts.push('<div class="mkt-loading">Loading market data…</div>');
            } else {
                parts.push(
                    '<div class="mkt-empty">' +
                    'No data yet.<br>Search and add a symbol above or wait for the next refresh.' +
                    '</div>'
                );
            }
        }

        grid.innerHTML = parts.join('');

        grid.querySelectorAll('.mkt-remove').forEach(btn => {
            btn.addEventListener('click', e => {
                e.stopPropagation();
                removeSymbol(btn.dataset.sym, btn.dataset.type);
            });
        });

        // Click a card → open AI Analysis with the right ticker.
        // Crypto needs Yahoo's "-USD" form (BTC → BTC-USD) for indicators to resolve.
        grid.querySelectorAll('.mkt-card').forEach(card => {
            card.style.cursor = 'pointer';
            card.title = 'Click to analyse';
            card.addEventListener('click', e => {
                if (e.target.closest('.mkt-remove')) return;
                const sym  = card.dataset.sym;
                const type = card.dataset.type;
                if (!sym) return;
                const ticker = (type === 'crypto' && !/-USD$/i.test(sym)) ? `${sym}-USD` : sym;
                _mktGoAnalyse(ticker);
            });
        });

        // Trigger ticker flash for changed prices
        _flashChangedPrices();
    }

    // ── Ticker flash: background pulse on price change ───────────────────────
    const _prevPrices = {};

    function _flashChangedPrices() {
        document.querySelectorAll('[data-price-el]').forEach(el => {
            const sym = el.dataset.priceEl;
            const raw = el.textContent.replace(/[^0-9.]/g, '');
            const now = parseFloat(raw);
            if (isNaN(now)) return;

            const prev = _prevPrices[sym];
            if (prev !== undefined && prev !== now) {
                const up  = now > prev;
                const cls = up ? 'mkt-tick-up' : 'mkt-tick-dn';
                el.classList.remove('mkt-tick-up', 'mkt-tick-dn');
                void el.offsetWidth; // reflow to restart animation
                el.classList.add(cls);
                setTimeout(() => el.classList.remove(cls), 500);
            }
            _prevPrices[sym] = now;
        });
    }

    function renderLastUpdated() {
        const el = document.getElementById('markets-last-updated');
        if (!el) return;
        if (!state.lastUpdated) { el.textContent = ''; return; }
        el.textContent = `Updated ${state.lastUpdated.toLocaleTimeString('en-US', {
            hour: '2-digit', minute: '2-digit',
        })}`;
    }

    function setRefreshDisabled(disabled) {
        const btn = document.getElementById('markets-refresh-btn');
        if (btn) btn.disabled = disabled;
        if (btn) btn.textContent = disabled ? '…' : '↻';
    }

    // ── Watchlist management ─────────────────────────────────────────────────
    function removeSymbol(sym, type) {
        if (type === 'stock') {
            state.watchlistStocks = state.watchlistStocks.filter(s => s !== sym);
            state.stocks          = state.stocks.filter(q => q.symbol !== sym);
        } else {
            state.watchlistCrypto = state.watchlistCrypto.filter(s => s !== sym);
            state.crypto          = state.crypto.filter(c => c.symbol.toUpperCase() !== sym);
        }
        delete state.sparklines[sym];
        saveWatchlist();
        renderGrid();
    }

    /**
     * Add a symbol to the watchlist.
     * type: 'crypto' | 'stock' | null (null = auto-detect via CRYPTO_IDS)
     */
    function addSymbol(raw, type = null) {
        const sym = raw.trim().toUpperCase().replace(/[^A-Z0-9.-]/g, '');
        if (!sym) return;

        const isCrypto = type === 'crypto' || (type === null && !!CRYPTO_IDS[sym]);

        if (isCrypto) {
            if (!CRYPTO_IDS[sym]) {
                Shell.toast(`"${sym}" not in the crypto catalogue — try the full symbol`, 'warning', 3500);
                return;
            }
            if (!state.watchlistCrypto.includes(sym)) {
                state.watchlistCrypto.push(sym);
                saveWatchlist();
                refresh();
            }
        } else {
            if (!state.watchlistStocks.includes(sym)) {
                state.watchlistStocks.push(sym);
                saveWatchlist();
                refresh();
            }
        }
    }

    // ── Symbol search ────────────────────────────────────────────────────────

    /** Search the local curated lists instantly (no network). */
    function _searchLocal(q) {
        const ql = q.toLowerCase().trim();
        if (!ql) return [];
        const results = [];
        const seen    = new Set();

        const score = (item, isCrypto) => {
            const sym  = item.symbol.toLowerCase();
            const name = item.name.toLowerCase();
            if (sym === ql)              return 10;
            if (sym.startsWith(ql))     return 8;
            if (name.startsWith(ql))    return 6;
            if (name.includes(ql))      return 4;
            if (sym.includes(ql))       return 3;
            return 0;
        };

        // Crypto first (users often search BTC, ETH etc.)
        for (const c of CRYPTO_CATALOGUE) {
            const s = score(c, true);
            if (s > 0 && !seen.has(c.symbol)) {
                seen.add(c.symbol);
                results.push({ symbol: c.symbol, name: c.name, assetType: 'crypto', exchange: 'CRYPTO', _score: s });
            }
        }

        // Stocks / ETFs
        for (const st of POPULAR_STOCKS) {
            const s = score(st, false);
            if (s > 0 && !seen.has(st.symbol)) {
                seen.add(st.symbol);
                results.push({ symbol: st.symbol, name: st.name, assetType: 'stock', exchange: st.exchange, _score: s });
            }
        }

        return results.sort((a, b) => b._score - a._score).slice(0, 8);
    }

    /** Backend search via Yahoo Finance (debounced by caller). */
    async function _searchBackend(q) {
        try {
            const data = await fetch(`${BACKEND}/search?q=${encodeURIComponent(q)}`).then(r => r.json());
            return (data.results || []).map(r => ({
                symbol:    r.symbol,
                name:      r.name,
                assetType: r.type?.toLowerCase().includes('crypto') ? 'crypto' : 'stock',
                exchange:  r.exchange || r.type || '',
                _score:    1,
            }));
        } catch {
            return [];
        }
    }

    /**
     * Attach a search-with-dropdown to an input element.
     *
     * config = {
     *   inputId:    string,         // ID of the <input>
     *   dropdownId: string,         // ID of the <ul> dropdown
     *   clearId:    string|null,    // ID of the clear button (optional)
     *   onSelect:   fn(sym, type, name) → void,
     * }
     */
    function initSymbolSearch({ inputId, dropdownId, clearId, onSelect }) {
        const input    = document.getElementById(inputId);
        const drop     = document.getElementById(dropdownId);
        const clearBtn = clearId ? document.getElementById(clearId) : null;
        if (!input || !drop) return;

        let _results      = [];
        let _selectedIdx  = -1;
        let _debounceTimer = null;

        const showDrop = () => {
            drop.style.display = '';
            input.setAttribute('aria-expanded', 'true');
        };
        const hideDrop = () => {
            drop.style.display = 'none';
            drop.innerHTML     = '';
            _results           = [];
            _selectedIdx       = -1;
            input.setAttribute('aria-expanded', 'false');
        };

        function renderDrop(results) {
            _results     = results;
            _selectedIdx = -1;
            if (!results.length) { hideDrop(); return; }

            drop.innerHTML = results.map((r, i) => {
                const isCrypto   = r.assetType === 'crypto';
                const badgeTxt   = isCrypto ? 'CRYPTO'
                                 : r.exchange ? r.exchange.split(' ')[0].toUpperCase()
                                 : 'STOCK';
                const badgeCls   = isCrypto ? 'mkt-si-badge--crypto' : 'mkt-si-badge--stock';
                const symCls     = isCrypto ? 'mkt-si-sym mkt-si-crypto' : 'mkt-si-sym';
                // Check if already in watchlist
                const inList = isCrypto
                    ? state.watchlistCrypto.includes(r.symbol)
                    : state.watchlistStocks.includes(r.symbol);
                return `<li class="mkt-search-item${inList ? ' mkt-si-added' : ''}"
                             data-idx="${i}" role="option"
                             data-sym="${esc(r.symbol)}"
                             data-type="${isCrypto ? 'crypto' : 'stock'}"
                             title="${esc(r.name)}">
                    <span class="${symCls}">${esc(r.symbol)}</span>
                    <span class="mkt-si-name">${esc(r.name)}</span>
                    <span class="mkt-si-badge ${badgeCls}">${esc(badgeTxt)}</span>
                    ${inList ? '<span class="mkt-si-check">✓</span>' : ''}
                </li>`;
            }).join('');

            showDrop();

            // Attach click handlers
            drop.querySelectorAll('.mkt-search-item').forEach(item => {
                item.addEventListener('mousedown', e => {
                    e.preventDefault();   // prevent input blur before mouseup
                    const idx = parseInt(item.dataset.idx);
                    const r   = _results[idx];
                    if (!r) return;
                    hideDrop();
                    input.value = '';
                    if (clearBtn) clearBtn.classList.add('mkt-hidden');
                    onSelect(r.symbol, r.assetType, r.name);
                });
            });
        }

        function highlight(idx) {
            _selectedIdx = idx;
            drop.querySelectorAll('.mkt-search-item').forEach((item, i) => {
                item.classList.toggle('selected', i === idx);
            });
        }

        function doSearch(q) {
            clearTimeout(_debounceTimer);
            if (!q || q.length < 1) { hideDrop(); return; }

            // 1. Immediate local results
            const local = _searchLocal(q);
            renderDrop(local);

            // 2. Backend augment after 350 ms
            if (q.length >= 2) {
                _debounceTimer = setTimeout(async () => {
                    const backend   = await _searchBackend(q);
                    const localSyms = new Set(local.map(r => r.symbol));
                    const merged    = [...local, ...backend.filter(r => !localSyms.has(r.symbol))];
                    renderDrop(merged.slice(0, 10));
                }, 350);
            }
        }

        // Wire events
        input.addEventListener('input', () => {
            const q = input.value.trim();
            if (clearBtn) clearBtn.classList.toggle('mkt-hidden', !q);
            doSearch(q);
        });

        input.addEventListener('keydown', e => {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                highlight(Math.min(_selectedIdx + 1, _results.length - 1));
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                highlight(Math.max(_selectedIdx - 1, 0));
            } else if (e.key === 'Enter') {
                if (_selectedIdx >= 0 && _results[_selectedIdx]) {
                    e.preventDefault();
                    const r = _results[_selectedIdx];
                    hideDrop();
                    input.value = '';
                    if (clearBtn) clearBtn.classList.add('mkt-hidden');
                    onSelect(r.symbol, r.assetType, r.name);
                }
            } else if (e.key === 'Escape') {
                hideDrop();
            }
        });

        input.addEventListener('blur', () => {
            // Delay so mousedown on items fires before blur
            setTimeout(hideDrop, 160);
        });

        input.addEventListener('focus', () => {
            const q = input.value.trim();
            if (q.length >= 1) doSearch(q);
        });

        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                input.value = '';
                clearBtn.classList.add('mkt-hidden');
                hideDrop();
                input.focus();
            });
        }
    }

    // ── Event binding ────────────────────────────────────────────────────────
    function bindEvents() {
        document.getElementById('markets-refresh-btn')
            ?.addEventListener('click', () => refresh());

        document.querySelectorAll('.mkt-filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                state.filter = btn.dataset.filter;
                document.querySelectorAll('.mkt-filter-btn')
                    .forEach(b => b.classList.toggle('active', b === btn));
                renderGrid();
            });
        });

        // Watchlist search-add
        initSymbolSearch({
            inputId:    'markets-add-input',
            dropdownId: 'markets-search-dropdown',
            clearId:    'mkt-search-clear',
            onSelect:   (sym, type, name) => {
                addSymbol(sym, type);
                Shell.toast(`Added ${sym} to watchlist`, 'success', 2000);
            },
        });

        // Prevent the form submit (Enter key) from doing a page reload;
        // Enter-to-select is handled by the search dropdown's keydown handler.
        document.getElementById('markets-add-form')
            ?.addEventListener('submit', e => e.preventDefault());
    }

    // ── Portfolio helpers ────────────────────────────────────────────────────
    function getCurrentPrice(symbol, type) {
        if (type === 'crypto') {
            const c = state.crypto.find(c => c.symbol.toUpperCase() === symbol.toUpperCase());
            return c?.current_price ?? null;
        }
        const q = state.stocks.find(s => s.symbol === symbol);
        return q?.price ?? null;
    }

    function positionCurrentValue(pos, currentPrice) {
        if (currentPrice == null || !pos.startPrice) return pos.invested;
        return pos.invested * (currentPrice / pos.startPrice);
    }

    function niceNumber(range, round) {
        const exp  = Math.floor(Math.log10(range));
        const frac = range / Math.pow(10, exp);
        let nf;
        if (round) {
            if (frac < 1.5) nf = 1;
            else if (frac < 3) nf = 2;
            else if (frac < 7) nf = 5;
            else nf = 10;
        } else {
            if (frac <= 1) nf = 1;
            else if (frac <= 2) nf = 2;
            else if (frac <= 5) nf = 5;
            else nf = 10;
        }
        return nf * Math.pow(10, exp);
    }

    function interpolate(arr, len) {
        if (arr.length === len) return arr;
        const out = [];
        for (let i = 0; i < len; i++) {
            const t   = i / (len - 1);
            const idx = t * (arr.length - 1);
            const lo  = Math.floor(idx);
            const hi  = Math.min(lo + 1, arr.length - 1);
            const fr  = idx - lo;
            out.push(arr[lo] * (1 - fr) + arr[hi] * fr);
        }
        return out;
    }

    // ── Portfolio chart (SVG — stepped, wire-grid, tactical labels) ──────────
    function portfolioChartSVG(positions) {
        // Sparkline source: use range-specific cache, fall back to 7d
        const rangeCache  = state.sparklinesByRange[state.chartRange] || state.sparklines;

        const series = [];
        for (const pos of positions) {
            const spark = rangeCache[pos.symbol] || state.sparklines[pos.symbol];
            if (!spark || spark.length < 2 || !pos.startPrice) continue;
            series.push({ pos, values: spark.map(p => pos.invested * (p / pos.startPrice)) });
        }
        if (!series.length) return '<div class="mkt-chart-nodata">// NO PRICE DATA — REFRESH TO LOAD</div>';

        const gridLen  = Math.max(...series.map(s => s.values.length));
        const combined = new Array(gridLen).fill(0);
        for (const { values } of series) {
            const v = interpolate(values, gridLen);
            for (let i = 0; i < gridLen; i++) combined[i] += v[i];
        }

        const totalInvested = positions.reduce((s, p) => s + p.invested, 0);
        const up = combined[gridLen - 1] >= totalInvested;

        // VW:VH = 5:1.4 — matches the CSS aspect-ratio constraint so
        // preserveAspectRatio="none" maps correctly without distortion.
        const VW = 700, VH = 196;
        const PAD = { t: 20, r: 16, b: 36, l: 62 };
        const cw = VW - PAD.l - PAD.r;
        const ch = VH - PAD.t - PAD.b;

        let minV = Math.min(...combined, totalInvested);
        let maxV = Math.max(...combined, totalInvested);
        const vpad = (maxV - minV) * 0.1 || totalInvested * 0.02 || 1;
        minV -= vpad; maxV += vpad;
        const range = maxV - minV;

        const sx = i => PAD.l + (i / (gridLen - 1)) * cw;
        const sy = v => PAD.t + ch - ((v - minV) / range) * ch;

        const lineCol  = up ? COL_UP  : COL_DN;
        const fillCol  = up ? FILL_UP : FILL_DN;
        const glowCol  = up ? 'rgba(0,240,255,0.4)' : 'rgba(255,0,60,0.5)';

        // Build stepped coordinate pairs
        const rawPairs = combined.map((v, i) => `${sx(i).toFixed(1)},${sy(v).toFixed(1)}`);
        const steppedPts = _stepPolyline(rawPairs);
        const fillPts    = `${PAD.l},${VH - PAD.b} ${steppedPts} ${(PAD.l + cw).toFixed(1)},${VH - PAD.b}`;

        const currSym = { EUR: '€', USD: '$', GBP: '£' }[positions[0]?.currency] || '';

        // Y-axis grid lines
        const yStep   = niceNumber((maxV - minV) / 4, true);
        const yStart  = Math.ceil(minV / yStep) * yStep;
        const yLabels = [];
        for (let v = yStart; v <= maxV + yStep * 0.5; v += yStep) {
            if (v >= minV && v <= maxV) yLabels.push(v);
        }

        // X-axis: dynamic tactical labels based on selected range
        const rangeCfg = RANGES[state.chartRange] || RANGES['7d'];
        let xSlots;
        if (state.chartRange === 'max' || !rangeCfg.xSlots) {
            // "Start till now" — derive labels from earliest position date
            const earliest = positions.reduce((d, p) => {
                const pd = new Date(p.startDate);
                return pd < d ? pd : d;
            }, new Date());
            const daysSince = Math.max(1, Math.ceil((Date.now() - earliest) / 86_400_000));
            const step = Math.round(daysSince / 7);
            xSlots = Array.from({ length: 8 }, (_, i) =>
                i < 7 ? `[T-${((7 - i) * step)}D]` : '[NOW // REALTIME]'
            );
        } else {
            xSlots = [...rangeCfg.xSlots];
            // Replace the last slot with the realtime label
            xSlots[xSlots.length - 1] = '[NOW // REALTIME]';
        }
        const xStep2  = (gridLen - 1) / (xSlots.length - 1);

        // X-axis vertical grid lines (wire-frame)
        const vGridLines = xSlots.map((_, i) => {
            const x = sx(Math.round(i * xStep2));
            return `<line x1="${x.toFixed(1)}" y1="${PAD.t}" x2="${x.toFixed(1)}" y2="${VH - PAD.b}"
                          stroke="rgba(255,0,60,0.07)" stroke-width="1"/>`;
        }).join('');

        const refY = sy(totalInvested);
        const endX = sx(gridLen - 1);
        const endY = sy(combined[gridLen - 1]);

        return `<svg class="mkt-chart-svg" viewBox="0 0 ${VW} ${VH}"
                    preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
                <linearGradient id="pgrd" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%"   stop-color="${lineCol}" stop-opacity="0.22"/>
                    <stop offset="100%" stop-color="${lineCol}" stop-opacity="0.0"/>
                </linearGradient>
                <filter id="neon-glow">
                    <feGaussianBlur stdDeviation="2" result="blur"/>
                    <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
                </filter>
            </defs>

            <!-- Wire-frame horizontal grid -->
            ${yLabels.map(v => `
                <line x1="${PAD.l}" y1="${sy(v).toFixed(1)}"
                      x2="${PAD.l + cw}" y2="${sy(v).toFixed(1)}"
                      stroke="rgba(255,0,60,0.1)" stroke-width="1" stroke-dasharray="3,5"/>
                <text x="${PAD.l - 8}" y="${sy(v).toFixed(1)}" dy="4" text-anchor="end"
                      font-size="8" fill="rgba(255,0,60,0.5)" font-family="'JetBrains Mono',monospace">
                    ${currSym}${Math.round(v)}
                </text>`).join('')}

            <!-- Wire-frame vertical grid -->
            ${vGridLines}

            <!-- Cost-basis reference line -->
            <line x1="${PAD.l}" y1="${refY.toFixed(1)}"
                  x2="${(PAD.l + cw).toFixed(1)}" y2="${refY.toFixed(1)}"
                  stroke="rgba(255,0,60,0.28)" stroke-width="1" stroke-dasharray="4,3"/>
            <text x="${PAD.l - 8}" y="${refY.toFixed(1)}" dy="4" text-anchor="end"
                  font-size="7" fill="rgba(255,0,60,0.45)" font-family="'JetBrains Mono',monospace">
                IN
            </text>

            <!-- Gradient fill under stepped line -->
            <polygon points="${fillPts}" fill="url(#pgrd)"/>

            <!-- Stepped performance line -->
            <polyline points="${steppedPts}" fill="none" stroke="${lineCol}"
                      stroke-width="1.8" stroke-linecap="square" stroke-linejoin="miter"
                      filter="url(#neon-glow)"/>

            <!-- Live endpoint dot -->
            <circle cx="${endX.toFixed(1)}" cy="${endY.toFixed(1)}" r="4"
                    fill="${lineCol}" stroke="rgba(0,0,0,0.6)" stroke-width="1.5"
                    filter="url(#neon-glow)"/>
            <circle cx="${endX.toFixed(1)}" cy="${endY.toFixed(1)}" r="7"
                    fill="none" stroke="${lineCol}" stroke-width="0.8" opacity="0.4"/>

            <!-- X-axis tactical labels -->
            ${xSlots.map((lbl, i) => {
                const x = sx(Math.round(i * xStep2));
                return `<text x="${x.toFixed(1)}" y="${VH - PAD.b + 18}" text-anchor="middle"
                              font-size="7.5" fill="rgba(255,0,60,0.42)"
                              font-family="'JetBrains Mono',monospace">
                            ${lbl}
                        </text>`;
            }).join('')}
        </svg>`;
    }

    function positionSparkSVG(pos) {
        const spark = state.sparklines[pos.symbol];
        if (!spark || spark.length < 2 || !pos.startPrice) return '';
        let pts = spark.map(p => pos.invested * (p / pos.startPrice));
        if (pts.length > 60) {
            const step = Math.ceil(pts.length / 60);
            pts = pts.filter((_, i) => i % step === 0);
        }
        const min   = Math.min(...pts), max = Math.max(...pts);
        const range = max - min || 1;
        const W = 240, H = 44, PAD = 2;
        const up   = pts[pts.length - 1] >= pts[0];
        const col  = up ? COL_UP : COL_DN;
        const fill = up ? FILL_UP : FILL_DN;

        const rawPairs = pts.map((p, i) => {
            const x = PAD + (i / (pts.length - 1)) * (W - PAD * 2);
            const y = H - PAD - ((p - min) / range) * (H - PAD * 2);
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        });
        const steppedPts = _stepPolyline(rawPairs);
        const fillPts    = `${PAD},${H} ${steppedPts} ${W - PAD},${H}`;

        return `<svg class="mkt-pos-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
            <polygon points="${fillPts}" fill="${fill}"/>
            <polyline points="${steppedPts}" fill="none" stroke="${col}"
                      stroke-width="1.5" stroke-linecap="square" stroke-linejoin="miter"/>
        </svg>`;
    }

    // ── Position card ─────────────────────────────────────────────────────────
    function positionCard(pos) {
        const currentPrice = getCurrentPrice(pos.symbol, pos.type);
        const current = positionCurrentValue(pos, currentPrice);
        const pl      = current - pos.invested;
        const plPct   = (pl / pos.invested) * 100;
        const up      = pl >= 0;
        const cls     = up ? 'mkt-up' : 'mkt-dn';
        const dir     = up ? '▲' : '▼';
        const cs      = { EUR: '€', USD: '$', GBP: '£' }[pos.currency] || '';
        const fmt     = n => n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const dateStr = new Date(pos.startDate).toLocaleDateString('en-GB', {
            day: 'numeric', month: 'short', year: 'numeric',
        });
        const spark     = positionSparkSVG(pos);
        const typeLabel = pos.type === 'crypto' ? 'Crypto' : 'Stock / ETF';

        return `<div class="mkt-pos-card mkt-card--${up ? 'up' : 'dn'}">
            <div class="mkt-pos-actions">
                <button class="mkt-pos-edit"   data-id="${esc(pos.id)}" title="Edit position">✎</button>
                <button class="mkt-pos-remove" data-id="${esc(pos.id)}" title="Remove position">×</button>
            </div>
            <div class="mkt-pos-head">
                <span class="mkt-sym">${esc(pos.symbol)}</span>
                <span class="mkt-pos-type">${typeLabel}</span>
            </div>
            <div class="mkt-pos-row">
                <span class="mkt-pos-lbl">Invested</span>
                <span class="mkt-pos-val">${cs}${fmt(pos.invested)}</span>
                <span class="mkt-pos-date">${dateStr}</span>
            </div>
            <div class="mkt-pos-row">
                <span class="mkt-pos-lbl">Current</span>
                <span class="mkt-pos-val ${cls}">${cs}${fmt(current)}</span>
            </div>
            <div class="mkt-pos-pnl">
                <span class="mkt-chg ${cls}">${dir} ${cs}${fmt(Math.abs(pl))}</span>
                <span class="mkt-chg-abs ${cls}">(${plPct >= 0 ? '+' : ''}${plPct.toFixed(2)}%)</span>
            </div>
            ${spark ? `<div class="mkt-pos-spark-wrap">${spark}</div>` : ''}
        </div>`;
    }

    // ── Portfolio pane renderer ───────────────────────────────────────────────
    function renderPortfolio() {
        const positions = loadPortfolio();
        const emptyEl   = document.getElementById('mkt-portfolio-empty');
        const contentEl = document.getElementById('mkt-portfolio-content');
        if (!emptyEl || !contentEl) return;

        if (!positions.length) {
            emptyEl.style.display   = '';
            contentEl.style.display = 'none';
            return;
        }
        emptyEl.style.display   = 'none';
        contentEl.style.display = '';

        const cs = { EUR: '€', USD: '$', GBP: '£' }[positions[0]?.currency] || '';
        let totalInvested = 0, totalCurrent = 0;
        for (const pos of positions) {
            totalInvested += pos.invested;
            totalCurrent  += positionCurrentValue(pos, getCurrentPrice(pos.symbol, pos.type));
        }
        const pl    = totalCurrent - totalInvested;
        const plPct = (pl / totalInvested) * 100;
        const up    = pl >= 0;
        const fmt   = n => n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

        const totalEl = document.getElementById('mkt-chart-total');
        const pnlEl   = document.getElementById('mkt-chart-pnl');
        if (totalEl) totalEl.textContent = `${cs}${fmt(totalCurrent)}`;
        if (pnlEl) {
            pnlEl.textContent = `${up ? '▲' : '▼'} ${cs}${fmt(Math.abs(pl))} (${plPct >= 0 ? '+' : ''}${plPct.toFixed(2)}%)`;
            pnlEl.className   = `mkt-chart-pnl ${up ? 'mkt-up' : 'mkt-dn'}`;
        }

        // Update chart header label to reflect current range
        const labelEl = document.getElementById('mkt-chart-label');
        if (labelEl) {
            const rng = RANGES[state.chartRange] || RANGES['7d'];
            labelEl.textContent = `Portfolio · ${rng.label}`;
        }

        const chartInner = document.getElementById('mkt-chart-inner');
        if (chartInner) {
            chartInner.innerHTML = portfolioChartSVG(positions);
            const svg = chartInner.querySelector('.mkt-chart-svg');
            if (svg) {
                svg.classList.remove('mkt-chart-draw');
                void svg.offsetWidth;
                svg.classList.add('mkt-chart-draw');
            }
        }

        const grid = document.getElementById('mkt-positions-grid');
        if (grid) {
            grid.innerHTML = positions.map(positionCard).join('');
            grid.querySelectorAll('.mkt-pos-remove').forEach(btn => {
                btn.addEventListener('click', () => {
                    const remaining = loadPortfolio().filter(p => p.id !== btn.dataset.id);
                    savePortfolio(remaining);
                    renderPortfolio();
                });
            });
            grid.querySelectorAll('.mkt-pos-edit').forEach(btn => {
                btn.addEventListener('click', () => openEditModal(btn.dataset.id));
            });
        }
    }

    // ── Tab switching ─────────────────────────────────────────────────────────
    function switchTab(tab) {
        state.activeTab = tab;
        document.querySelectorAll('.mkt-tab').forEach(b =>
            b.classList.toggle('active', b.dataset.tab === tab));

        const watchlist = tab === 'watchlist';
        document.getElementById('markets-watchlist-pane').style.display = watchlist ? '' : 'none';
        document.getElementById('markets-portfolio-pane').style.display = watchlist ? 'none' : '';
        document.getElementById('mkt-watchlist-filters').style.display  = watchlist ? '' : 'none';
        document.getElementById('markets-add-form').style.display       = watchlist ? '' : 'none';
        document.getElementById('mkt-track-btn').style.display          = watchlist ? 'none' : '';

        if (!watchlist) renderPortfolio();
    }

    // ── Add-position modal ────────────────────────────────────────────────────
    function openTrackModal() {
        document.getElementById('mkt-modal-overlay').style.display = 'flex';
        setTimeout(() => document.getElementById('mkt-field-symbol')?.focus(), 60);
    }

    function closeTrackModal() {
        document.getElementById('mkt-modal-overlay').style.display = 'none';
        document.getElementById('mkt-track-form')?.reset();
        const p = document.getElementById('mkt-field-preview');
        if (p) { p.textContent = ''; p.className = 'mkt-field-preview'; }
    }

    // ── Edit-position modal ───────────────────────────────────────────────────

    function openEditModal(posId) {
        const positions = loadPortfolio();
        const pos = positions.find(p => p.id === posId);
        if (!pos) return;

        const currentPrice = getCurrentPrice(pos.symbol, pos.type);
        const currentValue = positionCurrentValue(pos, currentPrice);
        const pl           = currentValue - pos.invested;
        const plPct        = (pl / pos.invested) * 100;
        const cs           = { EUR: '€', USD: '$', GBP: '£' }[pos.currency] || '';
        const fmt          = n => n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const up           = pl >= 0;

        // Populate fields
        document.getElementById('mkt-edit-id').value       = posId;
        document.getElementById('mkt-edit-sym').textContent = pos.symbol;
        document.getElementById('mkt-edit-type').textContent = pos.type === 'crypto' ? 'CRYPTO' : 'EQUITY';
        document.getElementById('mkt-edit-amount').value    = pos.invested;
        document.getElementById('mkt-edit-currency').value  = pos.currency;
        document.getElementById('mkt-edit-reset-basis').checked = false;

        // Snapshot: show current state
        const snap = document.getElementById('mkt-edit-snapshot');
        if (snap) {
            const plSign  = up ? '+' : '';
            const plColor = up ? '#00f0ff' : '#ff003c';
            snap.innerHTML = `
                <div class="mkt-snap-row">
                    <span class="mkt-snap-lbl">Invested</span>
                    <span class="mkt-snap-val">${cs}${fmt(pos.invested)}</span>
                </div>
                <div class="mkt-snap-row">
                    <span class="mkt-snap-lbl">Current value</span>
                    <span class="mkt-snap-val" style="color:${plColor}">${cs}${fmt(currentValue)}</span>
                </div>
                <div class="mkt-snap-row">
                    <span class="mkt-snap-lbl">Unrealised P&amp;L</span>
                    <span class="mkt-snap-val" style="color:${plColor}">
                        ${plSign}${cs}${fmt(Math.abs(pl))} (${plSign}${plPct.toFixed(2)}%)
                    </span>
                </div>
                ${currentPrice != null ? `
                <div class="mkt-snap-row">
                    <span class="mkt-snap-lbl">Market price</span>
                    <span class="mkt-snap-val">${fmtPrice(currentPrice, pos.currency)}</span>
                </div>` : ''}`;
        }

        // Hint text on amount field
        const hint = document.getElementById('mkt-edit-amount-hint');
        if (hint) hint.textContent = `Currently: ${cs}${fmt(pos.invested)}`;

        // Reset preview
        const prev = document.getElementById('mkt-edit-preview');
        if (prev) { prev.textContent = ''; prev.className = 'mkt-field-preview'; }

        document.getElementById('mkt-edit-overlay').style.display = 'flex';
        setTimeout(() => document.getElementById('mkt-edit-amount')?.focus(), 60);
    }

    function closeEditModal() {
        document.getElementById('mkt-edit-overlay').style.display = 'none';
        document.getElementById('mkt-edit-form')?.reset();
        const snap = document.getElementById('mkt-edit-snapshot');
        if (snap) snap.innerHTML = '';
        const prev = document.getElementById('mkt-edit-preview');
        if (prev) { prev.textContent = ''; prev.className = 'mkt-field-preview'; }
    }

    async function handleEditPosition(e) {
        e.preventDefault();
        const posId      = document.getElementById('mkt-edit-id')?.value;
        const newAmount  = parseFloat(document.getElementById('mkt-edit-amount')?.value);
        const newCur     = document.getElementById('mkt-edit-currency')?.value || 'EUR';
        const resetBasis = document.getElementById('mkt-edit-reset-basis')?.checked;
        const submitBtn  = document.getElementById('mkt-edit-submit');
        const prev       = document.getElementById('mkt-edit-preview');

        if (!posId || !newAmount || newAmount <= 0) {
            if (prev) { prev.textContent = '// Invalid amount.'; prev.className = 'mkt-field-preview mkt-preview-err'; }
            return;
        }

        if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = '// Saving…'; }

        try {
            const positions = loadPortfolio();
            const idx = positions.findIndex(p => p.id === posId);
            if (idx === -1) throw new Error('Position not found.');

            const pos = positions[idx];

            // If resetting basis: fetch current market price and use it as new startPrice
            if (resetBasis) {
                let freshPrice;
                if (pos.type === 'crypto' && CRYPTO_IDS[pos.symbol]) {
                    const id = CRYPTO_IDS[pos.symbol];
                    const r  = await fetch(`${COINGECKO}/simple/price?ids=${id}&vs_currencies=usd`);
                    if (!r.ok) throw new Error(`CoinGecko ${r.status}`);
                    freshPrice = (await r.json())[id]?.usd;
                } else {
                    const r = await fetch(`${BACKEND}/quote?symbols=${encodeURIComponent(pos.symbol)}`);
                    if (!r.ok) throw new Error(`Backend ${r.status}`);
                    freshPrice = (await r.json()).quotes?.[0]?.price;
                }
                if (!freshPrice) throw new Error('Could not fetch current price for basis reset.');
                positions[idx] = { ...pos, invested: newAmount, currency: newCur, startPrice: freshPrice, startDate: new Date().toISOString() };
            } else {
                positions[idx] = { ...pos, invested: newAmount, currency: newCur };
            }

            savePortfolio(positions);
            // Invalidate sparkline range cache so chart re-renders fresh
            state.sparklinesByRange = {};
            closeEditModal();
            await refresh();
            renderPortfolio();
            Shell.toast(`${pos.symbol} position updated`, 'success', 2500);

        } catch (err) {
            if (prev) { prev.textContent = `// Error: ${err.message}`; prev.className = 'mkt-field-preview mkt-preview-err'; }
        } finally {
            if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Save changes'; }
        }
    }

    /** Fetch and preview the current price for a symbol (called on modal symbol selection). */
    async function previewSymbolPrice(sym, type) {
        const prev = document.getElementById('mkt-field-preview');
        if (!prev) return;
        prev.textContent = '⏳ Fetching current price…';
        prev.className   = 'mkt-field-preview mkt-preview-loading';
        try {
            let price, currency;
            if (type === 'crypto' && CRYPTO_IDS[sym]) {
                const id = CRYPTO_IDS[sym];
                const r  = await fetch(`${COINGECKO}/simple/price?ids=${id}&vs_currencies=usd`);
                if (!r.ok) throw new Error(`CoinGecko ${r.status}`);
                const d  = await r.json();
                price    = d[id]?.usd;
                currency = 'USD';
            } else {
                const r  = await fetch(`${BACKEND}/quote?symbols=${encodeURIComponent(sym)}`);
                if (!r.ok) throw new Error(`Backend ${r.status}`);
                const d  = await r.json();
                const q  = d.quotes?.[0];
                price    = q?.price;
                currency = q?.currency || 'USD';
            }
            if (price == null) throw new Error('No price data');
            prev.textContent = `Current price: ${fmtPrice(price, currency)}`;
            prev.className   = 'mkt-field-preview mkt-preview-ok';
            // Focus the amount field so the user can just type their investment
            setTimeout(() => document.getElementById('mkt-field-amount')?.focus(), 60);
        } catch (err) {
            prev.textContent = `⚠ ${err.message}`;
            prev.className   = 'mkt-field-preview mkt-preview-err';
        }
    }

    async function handleAddPosition(e) {
        e.preventDefault();
        const sym    = document.getElementById('mkt-field-symbol')?.value.trim()
                           .toUpperCase().replace(/[^A-Z0-9.-]/g, '');
        const amount = parseFloat(document.getElementById('mkt-field-amount')?.value);
        const cur    = document.getElementById('mkt-field-currency')?.value || 'EUR';
        const btn    = document.getElementById('mkt-modal-submit');
        const prev   = document.getElementById('mkt-field-preview');

        if (!sym || !amount || amount <= 0) {
            if (prev) { prev.textContent = 'Enter a valid symbol and amount.'; prev.className = 'mkt-field-preview mkt-preview-err'; }
            return;
        }

        if (btn)  { btn.disabled = true; btn.textContent = '⏳ Saving…'; }

        try {
            const type = CRYPTO_IDS[sym] ? 'crypto' : 'stock';
            let startPrice;

            if (type === 'crypto') {
                const id = CRYPTO_IDS[sym];
                const r  = await fetch(`${COINGECKO}/simple/price?ids=${id}&vs_currencies=usd`);
                if (!r.ok) throw new Error(`CoinGecko ${r.status}`);
                const d  = await r.json();
                startPrice = d[id]?.usd;
            } else {
                const r  = await fetch(`${BACKEND}/quote?symbols=${encodeURIComponent(sym)}`);
                if (!r.ok) throw new Error(`Backend ${r.status}`);
                const d  = await r.json();
                startPrice = d.quotes?.[0]?.price;
            }

            if (!startPrice) throw new Error(`No price data returned for ${sym}`);

            const position = {
                id:         `pos_${Date.now()}`,
                symbol:     sym,
                type,
                invested:   amount,
                currency:   cur,
                startPrice,
                startDate:  new Date().toISOString(),
            };

            const positions = loadPortfolio();
            positions.push(position);
            savePortfolio(positions);

            if (type === 'stock'  && !state.watchlistStocks.includes(sym)) {
                state.watchlistStocks.push(sym); saveWatchlist();
            } else if (type === 'crypto' && !state.watchlistCrypto.includes(sym)) {
                state.watchlistCrypto.push(sym); saveWatchlist();
            }

            closeTrackModal();
            await refresh();
            renderPortfolio();
            Shell.toast(`Position tracked: ${sym}`, 'success', 2500);

        } catch (err) {
            if (prev) { prev.textContent = `Error: ${err.message}`; prev.className = 'mkt-field-preview mkt-preview-err'; }
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = 'Start tracking'; }
        }
    }

    // ── Lifecycle ────────────────────────────────────────────────────────────
    async function mount(root) {
        const html = await fetch('/modules/markets/view.html').then(r => r.text());
        root.innerHTML = html;

        loadWatchlist();
        bindEvents();

        // Tab switching
        document.querySelectorAll('.mkt-tab').forEach(btn => {
            btn.addEventListener('click', () => switchTab(btn.dataset.tab));
        });

        // ── Portfolio range selector ──────────────────────────────────────────
        document.querySelectorAll('.mkt-range-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                const range = btn.dataset.range;
                if (!range || range === state.chartRange) return;

                // Update active state on buttons
                document.querySelectorAll('.mkt-range-btn')
                    .forEach(b => b.classList.toggle('active', b === btn));

                state.chartRange = range;

                // Show loading state on chart
                const inner = document.getElementById('mkt-chart-inner');
                if (inner) {
                    inner.innerHTML = '<div class="mkt-chart-nodata mkt-range-loading">// FETCHING DATA…</div>';
                }

                // Fetch range-specific sparklines (cached after first load)
                await fetchRangeSparklines(range);

                // Re-render with new data
                renderPortfolio();
            });
        });

        // Portfolio modal
        document.getElementById('mkt-track-btn')   ?.addEventListener('click', openTrackModal);
        document.getElementById('mkt-modal-close') ?.addEventListener('click', closeTrackModal);
        document.getElementById('mkt-modal-cancel')?.addEventListener('click', closeTrackModal);
        document.getElementById('mkt-modal-overlay')?.addEventListener('click', e => {
            if (e.target === e.currentTarget) closeTrackModal();
        });
        document.getElementById('mkt-track-form')?.addEventListener('submit', handleAddPosition);

        // Edit-position modal
        document.getElementById('mkt-edit-close') ?.addEventListener('click', closeEditModal);
        document.getElementById('mkt-edit-cancel')?.addEventListener('click', closeEditModal);
        document.getElementById('mkt-edit-overlay')?.addEventListener('click', e => {
            if (e.target === e.currentTarget) closeEditModal();
        });
        document.getElementById('mkt-edit-form')?.addEventListener('submit', handleEditPosition);

        // Modal symbol search
        initSymbolSearch({
            inputId:    'mkt-field-symbol',
            dropdownId: 'mkt-modal-search-dropdown',
            clearId:    null,
            onSelect:   (sym, type, name) => {
                const inp = document.getElementById('mkt-field-symbol');
                if (inp) inp.value = sym;
                previewSymbolPrice(sym, type);
            },
        });

        // Restore tab
        if (state.activeTab === 'portfolio') switchTab('portfolio');

        renderGrid();
        refresh();

        if (state.refreshTimer) clearInterval(state.refreshTimer);
        state.refreshTimer = setInterval(refresh, REFRESH_MS);

        // ── Chassis ambient updaters ─────────────────────────────────────────
        _startChassisLife();
    }

    // Hex nibbles for the left stream panel
    const _HEX_POOL = '0123456789ABCDEF';
    function _rndHex(n) {
        let s = '0x';
        for (let i = 0; i < n; i++) s += _HEX_POOL[Math.random() * 16 | 0];
        return s;
    }
    function _rndByte() { return (_rndHex(2) + ' ').repeat(4).trim(); }

    function _startChassisLife() {
        const hexEl = document.getElementById('mkt-hex-stream-l');
        const sbSec = document.getElementById('mkt-sb-sec');
        const sbNet = document.getElementById('mkt-sb-net');
        const sbSyn = document.getElementById('mkt-sb-syn');
        const sbEnc = document.getElementById('mkt-sb-enc');

        // Left hex stream — new row every 0.9s
        const _HEX_ROWS = 14;
        const hexLines = Array.from({ length: _HEX_ROWS }, _rndHex.bind(null, 4));
        function _updateHex() {
            if (!hexEl) return;
            hexLines.shift();
            hexLines.push(_rndHex(4));
            hexEl.textContent = hexLines.join('\n');
        }
        setInterval(_updateHex, 900);
        _updateHex();

        // Right status blocks — cycle between states
        const _STATUS = {
            sec: [['SEC','ok'],['ARMED','warn'],['SCAN','ok'],['SEC','ok']],
            net: [['ONLINE','ok'],['NET','ok'],['RX','ok'],['TX','warn']],
            syn: [['SYNC','ok'],['SYN','ok'],['LOCK','ok'],['WAIT','warn']],
            enc: [['ENC','ok'],['AES','ok'],['KEY','ok'],['ENC','ok']],
        };
        let _tick = 0;
        function _updateStatus() {
            const t = _tick % 4;
            [[sbSec,'sec'],[sbNet,'net'],[sbSyn,'syn'],[sbEnc,'enc']].forEach(([el, k]) => {
                if (!el) return;
                const [txt, cls] = _STATUS[k][t];
                el.textContent  = txt;
                el.className    = `mkt-status-block mkt-sb-${cls}`;
            });
            _tick++;
        }
        setInterval(_updateStatus, 2200);
        _updateStatus();

        // Dummy end of _startChassisLife
        void 0;
    }

    function unmount() {
        clearInterval(state.refreshTimer);
        state.refreshTimer = null;
    }

    return { mount, unmount };

// ════════════════════════════════════════════════════════
// AI ANALYSIS TAB
// ════════════════════════════════════════════════════════

function _initAnalysisTab() {
  const btn   = document.getElementById('mkt-analysis-btn');
  const input = document.getElementById('mkt-analysis-symbol');
  const drop  = document.getElementById('mkt-analysis-dropdown');

  if (!btn || !input) return;

  // Avoid wiring twice
  if (btn._mktWired) return;
  btn._mktWired = true;

  btn.addEventListener('click', _runAnalysis);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') _runAnalysis(); });

  let _debounce;
  input.addEventListener('input', () => {
    clearTimeout(_debounce);
    const q = input.value.trim();
    if (q.length < 1) { drop.style.display = 'none'; return; }
    _debounce = setTimeout(async () => {
      try {
        const r = await fetch(`${BACKEND}/search?q=${encodeURIComponent(q)}`);
        const { results } = await r.json();
        if (!results?.length) { drop.style.display = 'none'; return; }
        drop.innerHTML = results.slice(0, 6).map(s =>
          `<li data-symbol="${s.symbol}" data-name="${s.name}">${s.symbol} <span style="color:var(--text-dim);font-size:0.78rem">${s.name}</span></li>`
        ).join('');
        drop.style.display = 'block';
        drop.querySelectorAll('li').forEach(li => {
          li.addEventListener('click', () => {
            input.value = li.dataset.symbol;
            input.dataset.name = li.dataset.name;
            drop.style.display = 'none';
          });
        });
      } catch {}
    }, 250);
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('#mkt-analysis-search-wrap')) drop.style.display = 'none';
  });
}

async function _runAnalysis() {
  const input  = document.getElementById('mkt-analysis-symbol');
  const range  = document.getElementById('mkt-analysis-range')?.value || '3mo';
  const symbol = input?.value.trim().toUpperCase();
  const name   = input?.dataset.name || symbol;
  if (!symbol) return;

  const loading    = document.getElementById('mkt-analysis-loading');
  const loadingMsg = document.getElementById('mkt-analysis-loading-msg');
  const empty      = document.getElementById('mkt-analysis-empty');
  const indRow     = document.getElementById('mkt-indicators-row');
  const aiOutput   = document.getElementById('mkt-ai-output');
  const chartWrap  = document.getElementById('mkt-analysis-chart-wrap');
  const btn        = document.getElementById('mkt-analysis-btn');

  if (loading)   loading.style.display   = 'flex';
  if (empty)     empty.style.display     = 'none';
  if (indRow)    indRow.style.display    = 'none';
  if (aiOutput)  aiOutput.style.display  = 'none';
  if (chartWrap) chartWrap.style.display = 'none';
  if (btn)       btn.disabled = true;
  if (loadingMsg) loadingMsg.textContent = 'Fetching indicators…';

  try {
    let useSym = symbol;
    let indResp = await fetch(`${BACKEND}/indicators?symbol=${encodeURIComponent(useSym)}&range=${range}`);
    // Crypto fallback: a bare crypto ticker (e.g. BTC) resolves on Yahoo only
    // as BTC-USD. Retry once if the first lookup failed and it looks like crypto.
    if (!indResp.ok && !/-USD$/i.test(useSym) && (CRYPTO_IDS[useSym] || /^[A-Z]{2,6}$/.test(useSym))) {
      const alt = `${useSym}-USD`;
      const retry = await fetch(`${BACKEND}/indicators?symbol=${encodeURIComponent(alt)}&range=${range}`);
      if (retry.ok) { indResp = retry; useSym = alt; if (input) input.value = alt; }
    }
    if (!indResp.ok) throw new Error(`Indicators: HTTP ${indResp.status}`);
    const ind = await indResp.json();

    _renderIndicatorChips(ind);
    _drawAnalysisChart(ind);
    if (chartWrap) chartWrap.style.display = 'block';
    if (indRow)    indRow.style.display    = 'flex';

    if (loadingMsg) loadingMsg.textContent = 'Running AI analysis (20-40s)…';
    const aiResp = await fetch(`${BACKEND}/analyze`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ symbol, name, range }),
    });
    if (!aiResp.ok) throw new Error(`Analysis: HTTP ${aiResp.status}`);
    const ai = await aiResp.json();

    _renderAIOutput(symbol, ai);
  } catch (e) {
    const empty2 = document.getElementById('mkt-analysis-empty');
    if (empty2) { empty2.textContent = `Error: ${e.message}`; empty2.style.display = 'block'; }
  } finally {
    if (loading) loading.style.display = 'none';
    if (btn)     btn.disabled = false;
  }
}

function _renderIndicatorChips(ind) {
  const chips = {
    'mkt-ind-rsi':    ind.rsi    ? `RSI ${ind.rsi.current?.toFixed(1)} · ${ind.rsi.signal}` : null,
    'mkt-ind-macd':   ind.macd   ? `MACD · ${ind.macd.trend}` : null,
    'mkt-ind-sma20':  ind.sma_20  ? `SMA20 · ${ind.sma_20.signal}` : null,
    'mkt-ind-sma50':  ind.sma_50  ? `SMA50 · ${ind.sma_50.signal}` : null,
    'mkt-ind-sma200': ind.sma_200 ? `SMA200 · ${ind.sma_200.signal}` : null,
    'mkt-ind-bb':     ind.bb     ? `BB width ${ind.bb.width_pct?.toFixed(1)}%` : null,
  };
  const classMap = {
    'BULLISH': 'bullish', 'BEARISH': 'bearish', 'NEUTRAL': 'neutral',
    'ABOVE':   'bullish', 'BELOW':   'bearish',
    'OVERSOLD':'oversold','OVERBOUGHT':'overbought',
  };
  Object.entries(chips).forEach(([id, text]) => {
    const el = document.getElementById(id);
    if (!el || !text) return;
    el.textContent = text;
    const kw = Object.keys(classMap).find(k => text.includes(k)) || '';
    el.className = 'mkt-indicator-chip ' + (classMap[kw] || 'neutral');
  });
}

function _drawAnalysisChart(ind) {
  const canvas = document.getElementById('mkt-analysis-canvas');
  if (!canvas || !ind.closes?.length) return;
  const ctx    = canvas.getContext('2d');
  const W      = canvas.offsetWidth || 600;
  const H      = canvas.height || 180;
  canvas.width = W;
  const prices = ind.closes;
  const sma20  = ind.sma_20?.series || [];
  const bb_u   = ind.bb?.upper || [];
  const bb_l   = ind.bb?.lower || [];
  const min    = Math.min(...prices, ...bb_l.filter(Boolean)) * 0.998;
  const max    = Math.max(...prices, ...bb_u.filter(Boolean)) * 1.002;
  const scaleY = v => H - 8 - ((v - min) / (max - min)) * (H - 16);
  const scaleX = i => (i / (prices.length - 1)) * W;
  ctx.clearRect(0, 0, W, H);

  const drawLine = (arr, color, width = 1.5, dash = []) => {
    if (!arr.length) return;
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
    arr.forEach((v, i) => v != null && (i === 0 ? ctx.moveTo(scaleX(i), scaleY(v)) : ctx.lineTo(scaleX(i), scaleY(v))));
    ctx.stroke(); ctx.setLineDash([]);
  };

  if (bb_u.length && bb_l.length) {
    ctx.beginPath();
    bb_u.forEach((v, i) => i === 0 ? ctx.moveTo(scaleX(i), scaleY(v)) : ctx.lineTo(scaleX(i), scaleY(v)));
    for (let i = bb_l.length - 1; i >= 0; i--) ctx.lineTo(scaleX(i), scaleY(bb_l[i]));
    ctx.closePath();
    ctx.fillStyle = 'rgba(245,230,66,0.04)';
    ctx.fill();
  }
  drawLine(bb_u, 'rgba(245,230,66,0.25)', 1, [3, 3]);
  drawLine(bb_l, 'rgba(245,230,66,0.25)', 1, [3, 3]);
  drawLine(sma20, '#60a5fa', 1.2);
  drawLine(prices, '#f5e642', 2);
}

function _renderAIOutput(symbol, ai) {
  const out    = document.getElementById('mkt-ai-output');
  const badge  = document.getElementById('mkt-ai-symbol-badge');
  const timeEl = document.getElementById('mkt-ai-time');
  const textEl = document.getElementById('mkt-ai-text');
  if (!out) return;
  if (badge)  badge.textContent  = symbol;
  if (timeEl) timeEl.textContent = ai.generated_at ? new Date(ai.generated_at).toLocaleTimeString() : '';
  if (textEl) {
    const html = (ai.analysis || '')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/\n/g, '<br>');
    textEl.innerHTML = html;
  }
  out.style.display = 'block';
}

// ════════════════════════════════════════════════════════
// SCREENER TAB
// ════════════════════════════════════════════════════════

let _screenerFilter = 'all';
let _screenerData   = null;

function _initScreenerTab() {
  document.querySelectorAll('.mkt-filter-pill').forEach(pill => {
    if (pill._mktWired) return;
    pill._mktWired = true;
    pill.addEventListener('click', () => {
      document.querySelectorAll('.mkt-filter-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      _screenerFilter = pill.dataset.filter || 'all';
      if (_screenerData) _renderScreener(_screenerData);
      else _loadScreener();
    });
  });
  const refreshBtn = document.getElementById('mkt-screener-refresh');
  if (refreshBtn && !refreshBtn._mktWired) {
    refreshBtn._mktWired = true;
    refreshBtn.addEventListener('click', () => { _screenerData = null; _loadScreener(); });
  }
}

async function _loadScreener() {
  const grid   = document.getElementById('mkt-screener-grid');
  const status = document.getElementById('mkt-screener-status');
  if (grid) grid.innerHTML = '<div class="mkt-screener-loading">Scanning 30 stocks… (10-20 seconds)</div>';
  try {
    const r = await fetch(`${BACKEND}/screener`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    _screenerData = data.results || [];
    if (status) status.textContent = `Screened ${data.screened || _screenerData.length} stocks · Last updated ${new Date().toLocaleTimeString()}`;
    _renderScreener(_screenerData);
  } catch (e) {
    if (grid) grid.innerHTML = `<div class="mkt-screener-loading">Error: ${e.message}</div>`;
  }
}

function _renderScreener(results) {
  const grid = document.getElementById('mkt-screener-grid');
  if (!grid) return;
  let filtered = results;
  if (_screenerFilter !== 'all') filtered = results.filter(r => r.signal === _screenerFilter);
  if (!filtered.length) {
    grid.innerHTML = `<div class="mkt-screener-loading">No stocks match the "${_screenerFilter}" filter.</div>`;
    return;
  }
  grid.innerHTML = filtered.map(r => {
    const pct = r.change_pct?.toFixed(2) ?? '0.00';
    const pos = parseFloat(pct) >= 0;
    const signalClass = r.signal ? `signal-${r.signal}` : '';
    const chips = [
      r.rsi       ? `<span class="mkt-screener-chip mkt-sc-rsi">RSI ${r.rsi.toFixed(0)}</span>` : '',
      r.macd_trend? `<span class="mkt-screener-chip mkt-sc-macd">${r.macd_trend}</span>` : '',
      r.sma50_signal ? `<span class="mkt-screener-chip mkt-sc-sma">SMA50 ${r.sma50_signal}</span>` : '',
    ].join('');
    return `<div class="mkt-screener-card ${signalClass}" onclick="_mktGoAnalyse('${r.symbol}')">
      <div class="mkt-screener-card-header">
        <span class="mkt-screener-symbol">${r.symbol}</span>
        <span class="mkt-screener-change ${pos ? 'pos' : 'neg'}">${pos ? '+' : ''}${pct}%</span>
      </div>
      <div class="mkt-screener-price">$${r.price?.toFixed(2) ?? '—'}</div>
      <div class="mkt-screener-chips">${chips}</div>
    </div>`;
  }).join('');
}

function _mktGoAnalyse(symbol) {
  const input = document.getElementById('mkt-analysis-symbol');
  if (input) input.value = symbol;
  _mktTabSwitch('analysis');
  _runAnalysis();
}

// ════════════════════════════════════════════════════════
// ACCOUNT TAB (ALPACA)
// ════════════════════════════════════════════════════════

async function _loadAccount() {
  try {
    const r = await fetch(`${BACKEND}/alpaca/account`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();

    const unconfigured = document.getElementById('mkt-account-unconfigured');
    const content      = document.getElementById('mkt-account-content');

    if (!data.configured) {
      if (unconfigured) unconfigured.style.display = 'flex';
      if (content)      content.style.display      = 'none';
      return;
    }
    if (unconfigured) unconfigured.style.display = 'none';
    if (content)      content.style.display      = 'block';

    const cards = document.getElementById('mkt-account-cards');
    if (cards) {
      const fmt = v => v != null ? `$${parseFloat(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—';
      cards.innerHTML = [
        ['Portfolio Value', fmt(data.portfolio_value)],
        ['Equity',         fmt(data.equity)],
        ['Cash',           fmt(data.cash)],
        ['Buying Power',   fmt(data.buying_power)],
      ].map(([label, val]) => `
        <div class="mkt-account-card">
          <div class="mkt-account-card-label">${label}</div>
          <div class="mkt-account-card-value">${val}</div>
        </div>`).join('');
    }

    const mode = document.getElementById('mkt-order-mode');
    const isPaper = (data.mode || 'paper') === 'paper';
    if (mode) {
      mode.textContent = isPaper ? 'Paper mode' : 'LIVE MONEY';
      mode.className   = 'mkt-order-mode' + (isPaper ? '' : ' live');
    }

    await _loadPositions();
    _initOrderForm(data.mode || 'paper');
  } catch (e) {
    const cards = document.getElementById('mkt-account-cards');
    if (cards) cards.innerHTML = `<div style="padding:1rem;color:var(--danger,#ef4444)">Error: ${e.message}</div>`;
  }
}

async function _loadPositions() {
  const el = document.getElementById('mkt-account-positions');
  if (!el) return;
  try {
    const r = await fetch(`${BACKEND}/alpaca/positions`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const { positions } = await r.json();
    if (!positions?.length) {
      el.innerHTML = '<div style="padding:0.5rem 0;color:var(--text-dim);font-size:0.82rem">No open positions.</div>';
      return;
    }
    el.innerHTML = `
      <div class="mkt-pos-row mkt-pos-header">
        <span>Symbol</span><span>Qty</span><span>Price</span><span>Value</span><span>P&amp;L%</span>
      </div>
      ${positions.map(p => {
        const pct = parseFloat(p.unrealized_plpc || 0) * 100;
        return `<div class="mkt-pos-row">
          <span class="mkt-pos-symbol">${p.symbol}</span>
          <span>${p.qty}</span>
          <span>$${parseFloat(p.current_price || 0).toFixed(2)}</span>
          <span>$${parseFloat(p.market_value || 0).toFixed(2)}</span>
          <span class="mkt-pos-pnl ${pct >= 0 ? 'pos' : 'neg'}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</span>
        </div>`;
      }).join('')}`;
  } catch (e) {
    el.innerHTML = `<div style="padding:0.5rem 0;color:var(--danger,#ef4444);font-size:0.82rem">Error loading positions: ${e.message}</div>`;
  }
}

let _lastOrderTs = 0;

function _initOrderForm(mode) {
  const typeEl   = document.getElementById('mkt-order-type');
  const limitEl  = document.getElementById('mkt-order-limit-price');
  const submitEl = document.getElementById('mkt-order-submit');
  const statusEl = document.getElementById('mkt-order-status');

  if (submitEl && submitEl._mktWired) return;
  if (submitEl) submitEl._mktWired = true;

  typeEl?.addEventListener('change', () => {
    if (limitEl) limitEl.style.display = typeEl.value === 'limit' ? 'block' : 'none';
  });

  submitEl?.addEventListener('click', async () => {
    const now = Date.now();
    if (now - _lastOrderTs < 5000) {
      if (statusEl) { statusEl.textContent = 'Wait 5s between orders.'; statusEl.className = 'mkt-order-status err'; }
      return;
    }
    const symbol = document.getElementById('mkt-order-symbol')?.value.trim().toUpperCase();
    const qty    = parseInt(document.getElementById('mkt-order-qty')?.value);
    const side   = document.getElementById('mkt-order-side')?.value;
    const type   = typeEl?.value || 'market';
    const limit  = parseFloat(limitEl?.value) || null;
    if (!symbol || !qty || qty < 1) {
      if (statusEl) { statusEl.textContent = 'Symbol and quantity required.'; statusEl.className = 'mkt-order-status err'; }
      return;
    }
    const isPaper = mode === 'paper';
    const confirmed = window.confirm(`${isPaper ? '[PAPER] ' : 'LIVE MONEY — '}${side.toUpperCase()} ${qty} x ${symbol} (${type}${limit ? ' @ $' + limit : ''}). Proceed?`);
    if (!confirmed) return;
    _lastOrderTs = now;
    if (statusEl) { statusEl.textContent = 'Submitting…'; statusEl.className = 'mkt-order-status'; }
    try {
      const r = await fetch(`${BACKEND}/alpaca/order`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ symbol, qty, side, type, time_in_force: 'day', limit_price: limit }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      if (statusEl) { statusEl.textContent = `Order submitted: ${d.id?.slice(0, 8)}…`; statusEl.className = 'mkt-order-status ok'; }
      setTimeout(_loadPositions, 3000);
    } catch (e) {
      if (statusEl) { statusEl.textContent = `${e.message}`; statusEl.className = 'mkt-order-status err'; }
    }
  });
}

// ════════════════════════════════════════════════════════
// TAB SWITCHING — augment existing system
// ════════════════════════════════════════════════════════

function _mktTabSwitch(tab) {
  document.querySelectorAll('.mkt-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.mkt-tab').forEach(t => t.classList.remove('active'));

  const pane = document.getElementById(`markets-${tab}-pane`);
  const btn  = document.querySelector(`.mkt-tab[data-tab="${tab}"]`);
  if (pane) pane.style.display = 'block';
  if (btn)  btn.classList.add('active');

  // Show/hide watchlist-only toolbar elements
  const wlFilters = document.getElementById('mkt-watchlist-filters');
  const addForm   = document.getElementById('markets-add-form');
  const trackBtn  = document.getElementById('mkt-track-btn');
  if (wlFilters) wlFilters.style.display = tab === 'watchlist' ? '' : 'none';
  if (addForm)   addForm.style.display   = tab === 'watchlist' ? '' : 'none';
  if (trackBtn)  trackBtn.style.display  = tab === 'portfolio' ? '' : 'none';

  if (tab === 'screener')  { _initScreenerTab(); if (!_screenerData) _loadScreener(); }
  if (tab === 'account')   _loadAccount();
  if (tab === 'analysis')  _initAnalysisTab();
  if (tab === 'research')  _initResearchTab();
}

// Wire new tab buttons (existing ones keep their original listeners for watchlist/portfolio)
(function _wireNewTabs() {
  ['analysis', 'screener', 'account'].forEach(tabName => {
    const btn = document.querySelector(`.mkt-tab[data-tab="${tabName}"]`);
    if (btn && !btn._mktNewWired) {
      btn._mktNewWired = true;
      btn.addEventListener('click', () => _mktTabSwitch(tabName));
    }
  });
})();

// ════════════════════════════════════════════════════════
// DEEP RESEARCH TAB
// ════════════════════════════════════════════════════════

let _macroLoaded = false;

async function _loadMacroBar() {
  if (_macroLoaded) return;
  try {
    const r = await fetch(`${BACKEND}/macro`);
    if (!r.ok) return;
    const { macro: m } = await r.json();
    if (!m) return;
    _macroLoaded = true;
    const bar = document.getElementById('mkt-macro-bar');
    if (bar) bar.style.display = 'flex';

    const _set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
    const fmt = (v, unit='%') => v != null ? `${v}${unit}` : '—';
    const curve = m.yield_curve;
    const curveClass = m.inverted ? 'mkt-macro-inverted' : '';

    _set('mkt-macro-fed',   `<span>Fed Rate</span> ${fmt(m.fed_funds_rate)}`);
    _set('mkt-macro-10y',   `<span>10Y</span> ${fmt(m.treasury_10y)}`);
    _set('mkt-macro-curve', `<span>Curve</span> <span class="${curveClass}">${fmt(curve)}${m.inverted?' ⚠':''}</span>`);
    _set('mkt-macro-cpi',   `<span>CPI</span> ${fmt(m.cpi_yoy)}`);
    _set('mkt-macro-unemp', `<span>Unemp</span> ${fmt(m.unemployment)}`);
  } catch {}
}

function _initResearchTab() {
  const btn      = document.getElementById('mkt-research-btn');
  const symInput = document.getElementById('mkt-research-symbol');

  // Auto-fill symbol from analysis tab if available
  const analysisSym = document.getElementById('mkt-analysis-symbol');
  if (analysisSym?.value && symInput && !symInput.value) {
    symInput.value = analysisSym.value;
  }

  if (btn && !btn._mktResearchWired) {
    btn._mktResearchWired = true;
    btn.addEventListener('click', _runDeepResearch);
  }
  symInput?.addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('mkt-research-company')?.focus(); });
  document.getElementById('mkt-research-company')?.addEventListener('keydown', e => { if (e.key === 'Enter') _runDeepResearch(); });

  _loadMacroBar();
}

async function _runDeepResearch() {
  const symbol    = document.getElementById('mkt-research-symbol')?.value.trim().toUpperCase();
  const company   = document.getElementById('mkt-research-company')?.value.trim();
  const modelEl   = document.getElementById('mkt-research-model')?.value;
  const contracts = document.getElementById('mkt-research-contracts')?.checked !== false;
  const macro     = document.getElementById('mkt-research-macro')?.checked !== false;

  if (!symbol)  { alert('Enter a ticker symbol'); return; }
  if (!company) { alert('Enter the company name for contract search'); return; }

  const loading    = document.getElementById('mkt-research-loading');
  const loadingMsg = document.getElementById('mkt-research-loading-msg');
  const empty      = document.getElementById('mkt-research-empty');
  const output     = document.getElementById('mkt-research-output');
  const preview    = document.getElementById('mkt-contracts-preview');
  const btn        = document.getElementById('mkt-research-btn');

  if (empty)   empty.style.display   = 'none';
  if (output)  output.style.display  = 'none';
  if (preview) preview.style.display = 'none';
  if (loading) loading.style.display = 'flex';
  if (btn)     btn.disabled = true;
  if (loadingMsg) loadingMsg.textContent = 'Fetching government contracts…';

  // If user picks ollama selector, still send haiku — server falls back to Ollama if no key
  const actualModel = (modelEl === 'ollama') ? 'claude-3-5-haiku-20241022' : modelEl;

  try {
    // Fetch contracts first for preview while AI thinks
    if (contracts) {
      try {
        const cr = await fetch(`${BACKEND}/research/contracts?company=${encodeURIComponent(company)}&limit=8`);
        if (cr.ok) {
          const cd = await cr.json();
          _renderContractsPreview(cd);
          if (loadingMsg) loadingMsg.textContent = `Found ${cd.count} contracts (${cd.total_value_fmt}) — running AI analysis…`;
        }
      } catch {}
    }

    if (loadingMsg) loadingMsg.textContent = 'Claude is analysing all data sources…';

    const r = await fetch(`${BACKEND}/research/analyze`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol,
        company_name:      company,
        model:             actualModel,
        include_contracts: contracts,
        include_macro:     macro,
      }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    const data = await r.json();
    _renderResearchOutput(symbol, data);

  } catch (e) {
    if (empty) { empty.textContent = `Error: ${e.message}`; empty.style.display = 'block'; }
  } finally {
    if (loading) loading.style.display = 'none';
    if (btn)     btn.disabled = false;
  }
}

function _renderContractsPreview(data) {
  const list    = document.getElementById('mkt-contracts-list');
  const preview = document.getElementById('mkt-contracts-preview');
  if (!list || !data.contracts?.length) return;
  list.innerHTML = data.contracts.slice(0, 6).map(c => `
    <div class="mkt-contract-row">
      <span class="mkt-contract-amount">${c.amount_fmt}</span>
      <span class="mkt-contract-agency">${(c.agency || '—').substring(0, 45)}</span>
      <span class="mkt-contract-desc">${(c.description || c.type || '—').substring(0, 90)}</span>
    </div>`).join('');
  if (preview) preview.style.display = 'block';
}

function _renderResearchOutput(symbol, data) {
  const out    = document.getElementById('mkt-research-output');
  const badge  = document.getElementById('mkt-research-symbol-badge');
  const mBadge = document.getElementById('mkt-research-model-badge');
  const timeEl = document.getElementById('mkt-research-time');
  const textEl = document.getElementById('mkt-research-text');
  if (!out) return;

  if (badge)   badge.textContent  = symbol;
  if (mBadge)  mBadge.textContent = data.model_used || '';
  if (timeEl)  timeEl.textContent = data.generated_at ? new Date(data.generated_at).toLocaleString() : '';
  if (textEl) {
    const html = (data.analysis || '')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/^[\*\-] (.+)$/gm, '<li>$1</li>')
      .replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>')
      .replace(/\n{2,}/g, '<br><br>')
      .replace(/\n/g, '<br>');
    textEl.innerHTML = html;
  }
  out.style.display = 'block';
}

// Wire research tab into the _mktTabSwitch system
(function _wireResearchTab() {
  const btn = document.querySelector('.mkt-tab[data-tab="research"]');
  if (!btn || btn._mktNewWired) return;
  btn._mktNewWired = true;
  btn.addEventListener('click', () => _mktTabSwitch('research'));
})();

// Extend _mktTabSwitch to handle research tab
const _origMktTabSwitch = _mktTabSwitch;
// Patch: _mktTabSwitch already handles unknown tabs gracefully (shows pane, clears active)
// We just need to wire the init call — do it via the existing tab click wiring above
// and ensure _initResearchTab is called on switch.
})();
