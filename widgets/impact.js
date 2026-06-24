/**
 * Impact Panel widget.
 *
 * Listens on the dashboard WebSocket for `impact_places` messages (pushed when
 * an operator clicks the "scan impacted places" button on an alert) and renders
 * a static side panel listing the populated places and vulnerable facilities
 * inside the warning polygon. `impact_clear` hides it.
 */
class ImpactPanel {
    constructor() {
        this.config = { reconnectDelay: 5000 };
        this.ws = null;

        const params = new URLSearchParams(window.location.search);
        const side = (params.get('side') || 'right').toLowerCase();

        this.panelEl = document.getElementById('impact-panel');
        this.eventEl = document.getElementById('impact-event');
        this.bodyEl = document.getElementById('impact-body');

        // Pin to the requested edge (default right; opposite the talent).
        this.panelEl.classList.remove('side-left', 'side-right');
        this.panelEl.classList.add(side === 'left' ? 'side-left' : 'side-right');

        this.connect();
    }

    connect() {
        // Tear down any prior socket so its stale handlers can't fire or stack a
        // reconnect, and guard against multiple in-flight reconnect timers.
        if (this.ws) {
            this.ws.onopen = this.ws.onmessage = this.ws.onclose = this.ws.onerror = null;
            try { this.ws.close(); } catch (e) {}
            this.ws = null;
        }
        if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }

        const wsUrl = getWebSocketUrl();
        try {
            this.ws = new WebSocket(wsUrl);
            this.ws.onopen = () => console.log('[impact] WebSocket connected');
            this.ws.onmessage = (event) => this.handleMessage(event.data);
            this.ws.onclose = () => this.scheduleReconnect();
            this.ws.onerror = (err) => console.error('[impact] WebSocket error:', err);
        } catch (err) {
            console.error('[impact] Failed to create WebSocket:', err);
            this.scheduleReconnect();
        }
    }

    scheduleReconnect() {
        if (this._reconnectTimer) return;
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            this.connect();
        }, this.config.reconnectDelay);
    }

    handleMessage(data) {
        let message;
        try {
            message = JSON.parse(data);
        } catch {
            return;
        }
        switch (message.type) {
            case 'impact_places':
                this.render(message.data);
                break;
            case 'impact_clear':
                this.hide();
                break;
            default:
                break;
        }
    }

    hide() {
        this.panelEl.classList.add('hidden');
    }

    render(data) {
        if (!data || !Array.isArray(data.categories) || data.total === 0) {
            this.hide();
            return;
        }

        this.eventEl.textContent = data.event_name || 'Active Warning';
        this.bodyEl.innerHTML = '';

        for (const cat of data.categories) {
            if (!cat.items || cat.items.length === 0) continue;
            this.bodyEl.appendChild(this.renderCategory(cat));
        }

        this.panelEl.classList.remove('hidden');
    }

    renderCategory(cat) {
        const section = document.createElement('div');
        section.className = 'impact-section' + (cat.at_risk ? ' at-risk' : '');

        const header = document.createElement('div');
        header.className = 'impact-section-title';
        const icon = cat.at_risk ? '<i class="fas fa-triangle-exclamation"></i> ' : '';
        header.innerHTML = icon + this.escape(cat.label);
        section.appendChild(header);

        const list = document.createElement('div');
        list.className = 'impact-list';
        for (const item of cat.items) {
            const row = document.createElement('div');
            row.className = 'impact-item';
            const sub = item.sub ? `<span class="impact-item-sub">${this.escape(item.sub)}</span>` : '';
            row.innerHTML = `<span class="impact-item-name">${this.escape(item.name)}</span>${sub}`;
            list.appendChild(row);
        }

        // "+N more" when the polygon held more than we display.
        const overflow = (cat.total || cat.items.length) - cat.items.length;
        if (overflow > 0) {
            const more = document.createElement('div');
            more.className = 'impact-more';
            more.textContent = `+${overflow} more`;
            list.appendChild(more);
        }

        section.appendChild(list);
        return section;
    }

    escape(str) {
        const div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }
}

document.addEventListener('DOMContentLoaded', () => new ImpactPanel());
