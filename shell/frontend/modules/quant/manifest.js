Shell.register({
    id: 'quant',
    label: 'Quant',
    icon: '📈',
    kind: 'native',
    description: 'NL → NautilusTrader strategy → backtest (research only)',
    async mount(root) {
        if (!document.getElementById('quant-css')) {
            const l = document.createElement('link');
            l.id = 'quant-css'; l.rel = 'stylesheet';
            l.href = '/modules/quant/style.css';
            document.head.appendChild(l);
        }
        if (!window.QuantView) {
            await new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = '/modules/quant/view.js';
                s.onload = resolve; s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        await window.QuantView.mount(root);
    },
    async unmount() {
        if (window.QuantView) window.QuantView.unmount();
    },
    palette: [
        { icon: '📈', label: 'Open Quant', action: () => Shell.switch('quant') },
    ],
});
