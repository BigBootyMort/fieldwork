/* Markets module — registers with Runi Shell */
Shell.register({
    id:          'markets',
    label:       'Trading Desk',
    icon:        '🖥',
    kind:        'native',
    description: 'Markets, research, strategy backtesting & broker — one desk',

    async mount(root) {
        // Lazy-load CSS
        if (!document.getElementById('markets-css')) {
            const link = document.createElement('link');
            link.id   = 'markets-css';
            link.rel  = 'stylesheet';
            link.href = '/modules/markets/style.css';
            document.head.appendChild(link);
        }
        // Lazy-load JS (exposes window.MarketsView)
        if (!window.MarketsView) {
            await new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src     = '/modules/markets/view.js';
                s.onload  = resolve;
                s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        await window.MarketsView.mount(root);
    },

    async unmount() {
        if (window.MarketsView) window.MarketsView.unmount();
    },

    palette: [
        { icon: '📈', label: 'Open Markets',
          action: () => Shell.switch('markets') },
        { icon: '🔄', label: 'Markets — refresh prices',
          action: async () => {
              await Shell.switch('markets');
              document.getElementById('markets-refresh-btn')?.click();
          } },
    ],
});
