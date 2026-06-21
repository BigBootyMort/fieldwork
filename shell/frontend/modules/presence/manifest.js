Shell.register({
    id: 'presence',
    label: 'Presence',
    icon: '📣',
    kind: 'native',
    description: 'Generate content, platform listings, and outreach for your OSINT freelance business',
    async mount(root) {
        if (!document.getElementById('presence-css')) {
            const l = document.createElement('link');
            l.id = 'presence-css'; l.rel = 'stylesheet';
            l.href = '/modules/presence/style.css';
            document.head.appendChild(l);
        }
        if (!window.PresenceView) {
            await new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = '/modules/presence/view.js';
                s.onload = resolve; s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        await window.PresenceView.mount(root);
    },
    async unmount() {
        if (window.PresenceView) window.PresenceView.unmount();
    },
    palette: [
        { icon: '📣', label: 'Open Presence', action: () => Shell.switch('presence') },
        { icon: '✍️', label: 'Generate OSINT post', action: async () => { await Shell.switch('presence'); window.PresenceView?.focusContent(); } },
    ],
});
