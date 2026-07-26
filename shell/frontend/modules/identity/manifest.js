/* Identity Forge — registers with Shell */
Shell.register({
    id:    'identity',
    label: 'Identity Forge',
    icon:  '🎭',
    kind:  'native',
    description: 'Generate synthetic OSINT cover personas (sock puppets)',

    async mount(root) {
        if (!window.IdentityView) {
            await new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = '/modules/identity/view.js';
                s.onload = resolve; s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        await window.IdentityView.mount(root);
    },
    async unmount() { if (window.IdentityView) window.IdentityView.unmount(); },

    palette: [
        { icon: '🎭', label: 'Open Identity Forge', action: () => Shell.switch('identity') },
        { icon: '🎲', label: 'Forge a new persona',
          action: async () => { await Shell.switch('identity'); document.getElementById('idf-generate')?.click(); } },
    ],
});
