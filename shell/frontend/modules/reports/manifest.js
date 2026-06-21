/* Reports module — registers with Shell */
Shell.register({
    id:    'reports',
    label: 'Reports',
    icon:  '📋',
    kind:  'native',
    description: 'Generate & archive AI-powered intelligence briefings',

    async mount(root) {
        if (!window.ReportsView) {
            await new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = '/modules/reports/view.js';
                s.onload  = resolve;
                s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        await window.ReportsView.mount(root);
    },

    async unmount() {
        if (window.ReportsView) window.ReportsView.unmount();
    },

    palette: [
        { icon: '📋', label: 'Open Reports',
          action: () => Shell.switch('reports') },
        { icon: '⚡', label: 'Reports — generate new brief',
          action: async () => {
              await Shell.switch('reports');
              document.getElementById('reports-generate-btn')?.click();
          } },
    ],
});
