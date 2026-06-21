/* Agent module — registers with Shell */
Shell.register({
    id:    'agent',
    label: 'Agent',
    icon:  '🤖',
    kind:  'native',
    description: 'Autonomous AI Agent — plan, execute, and monitor multi-step tasks',

    async mount(root) {
        // Load CSS
        if (!document.getElementById('agent-css')) {
            const l = document.createElement('link');
            l.id   = 'agent-css';
            l.rel  = 'stylesheet';
            l.href = '/modules/agent/style.css';
            document.head.appendChild(l);
        }
        // Lazy-load view.js
        if (!window.AgentView) {
            await new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src     = '/modules/agent/view.js';
                s.onload  = resolve;
                s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        await window.AgentView.mount(root);
    },

    async unmount() {
        if (window.AgentView) window.AgentView.unmount();
    },

    palette: [
        { icon: '🤖', label: 'Open Agent',
          action: () => Shell.switch('agent') },
        { icon: '▶',  label: 'Agent — new task',
          action: async () => {
              await Shell.switch('agent');
              document.getElementById('agent-objective')?.focus();
          }},
    ],
});
