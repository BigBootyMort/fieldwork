/* News module — registers with Shell */
Shell.register({
    id:    'news',
    label: 'News & Brief',
    icon:  '📰',
    kind:  'native',
    description: 'World news choropleth + LLM brief + AI assistant',

    async mount(root) {
        // Lazy-load view.js the first time the module is opened
        if (!window.NewsView) {
            await new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = '/modules/news/view.js';
                s.onload  = resolve;
                s.onerror = reject;
                document.head.appendChild(s);
            });
        }
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
