Shell.register({
    id: 'gigs',
    label: 'Gig Hunter',
    icon: '💼',
    kind: 'native',
    description: 'Monitor freelance platforms for OSINT & research gigs',
    async mount(root) {
        if (!document.getElementById('gigs-css')) {
            const l = document.createElement('link');
            l.id = 'gigs-css'; l.rel = 'stylesheet';
            l.href = '/modules/gigs/style.css';
            document.head.appendChild(l);
        }
        if (!window.GigsView) {
            await new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = '/modules/gigs/view.js';
                s.onload = resolve; s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        await window.GigsView.mount(root);
    },
    async unmount() {
        if (window.GigsView) window.GigsView.unmount();
    },
    palette: [
        { icon: '💼', label: 'Open Gig Hunter', action: () => Shell.switch('gigs') },
        { icon: '🔄', label: 'Refresh Gigs', action: async () => { await Shell.switch('gigs'); window.GigsView?.refresh(); } },
    ],
});
