/* News module — registers with Shell */
Shell.register({
    id:    'news',
    label: 'News & Brief',
    icon:  '📰',
    kind:  'native',
    description: 'World news choropleth + LLM brief + AI assistant',

    async mount(root) {
        const load = (src) => new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = src; s.onload = resolve; s.onerror = reject;
            document.head.appendChild(s);
        });
        // Lazy-load the avatar + view the first time the module is opened
        if (!window.RuniAvatar) await load('/modules/news/avatar.js');
        if (!window.NewsView)   await load('/modules/news/view.js');
        await window.NewsView.mount(root);
    },

    async unmount() {
        if (window.NewsView) window.NewsView.unmount();
    },

    palette: [
        { icon: '📰', label: 'Open News & Brief',
          action: () => Shell.switch('news') },
        { icon: '🔄', label: 'News — poll feeds now',
          action: async () => {
              await Shell.switch('news');
              document.getElementById('news-poll-btn')?.click();
          } },
        { icon: '🧠', label: 'News — generate morning brief',
          action: async () => {
              await Shell.switch('news');
              document.getElementById('news-brief-btn')?.click();
          } },
    ],
});
