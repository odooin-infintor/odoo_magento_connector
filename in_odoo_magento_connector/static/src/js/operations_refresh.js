/** @odoo-module **/

import { onMounted, onWillUnmount, useExternalListener, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { FormController } from "@web/views/form/form_controller";
import { formView } from "@web/views/form/form_view";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { kanbanView } from "@web/views/kanban/kanban_view";

const REFRESH_INTERVAL = 10000;

// Only the two read-only Operations views opt in. Editable connection settings
// and setup wizards retain their normal save/discard lifecycle.
export function useOperationsRefresh(controller) {
    const state = useState({ updatedAt: "", failed: false });
    let mounted = false;
    let busy = false;
    let timer;
    const timestamp = () => new Date().toLocaleTimeString();
    const refresh = async () => {
        if (!mounted || busy || document.hidden || document.body.classList.contains("modal-open") ||
            !controller.rootRef.el?.isConnected || !controller.model.isReady ||
            controller.model.root.dirty) {
            return;
        }
        busy = true;
        try {
            // Root.load retains domain, grouping, paging and the current record.
            // It also uses the relational model mutex to serialize record reads.
            await controller.model.root.load();
            if (mounted) {
                state.updatedAt = timestamp();
                state.failed = false;
            }
        } catch {
            // Keep the last good data visible, but never present it as current.
            // The next timer retries without opening repetitive error dialogs.
            if (mounted) {
                state.failed = true;
            }
        } finally {
            busy = false;
        }
    };
    onMounted(() => {
        mounted = true;
        // Odoo can restore a cached controller when navigating back. Read its
        // data immediately; only a successful read may advance Last loaded.
        refresh();
        timer = browser.setInterval(refresh, REFRESH_INTERVAL);
    });
    onWillUnmount(() => {
        mounted = false;
        browser.clearInterval(timer);
    });
    useExternalListener(document, "visibilitychange", refresh);
    return state;
}

class MagentoOperationsKanbanController extends KanbanController {
    static template = "in_odoo_magento_connector.OperationsKanbanView";
    setup() {
        super.setup();
        this.operationsRefresh = useOperationsRefresh(this);
    }
}

class MagentoOperationsFormController extends FormController {
    static template = "in_odoo_magento_connector.OperationsFormView";
    setup() {
        super.setup();
        this.operationsRefresh = useOperationsRefresh(this);
    }
}

registry.category("views").add("magento_operations_kanban", {
    ...kanbanView, Controller: MagentoOperationsKanbanController,
});
registry.category("views").add("magento_operations_form", {
    ...formView, Controller: MagentoOperationsFormController,
});
