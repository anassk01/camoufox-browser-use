"""Client-side JavaScript evaluation script for extracting accessible and interactive DOM elements."""

DOM_EXTRACTOR_JS = r"""(args) => {
    const now = () => (
        window.performance && window.performance.now ? window.performance.now() : Date.now()
    );
    const deadline = now() + (args.maxExtractionMs || 5000);
    const selectorList = [
        'button', 'a[href]', 'input', 'textarea', 'select',
        '[contenteditable=""]', '[contenteditable="true"]', '[role="button"]',
        '[role="link"]', '[role="textbox"]', '[role="checkbox"]', '[role="radio"]',
        '[role="listbox"]', '[role="option"]', '[role="menu"]', '[role="menuitem"]', '[tabindex]',
        '[role="switch"]', '[role="tab"]', '[role="slider"]', '[role="spinbutton"]',
        '[role="searchbox"]', '[role="treeitem"]', '[role="gridcell"]',
        '[role="menuitemcheckbox"]', '[role="menuitemradio"]', '[role="scrollbar"]',
    ];
    const attributeNames = [
        'id', 'class', 'name', 'type', 'role', 'href', 'src', 'target', 'title', 'value', 'placeholder', 'alt',
        'aria-label', 'aria-description', 'aria-expanded', 'aria-checked', 'aria-selected', 'aria-disabled',
        'aria-pressed', 'aria-invalid', 'aria-required', 'aria-busy', 'aria-live', 'aria-haspopup',
        'aria-valuemin', 'aria-valuemax', 'aria-valuenow', 'aria-valuetext', 'aria-keyshortcuts',
        'aria-multiselectable', 'aria-readonly', 'aria-autocomplete', 'aria-placeholder',
        'aria-controls', 'aria-labelledby', 'aria-describedby', 'aria-errormessage', 'aria-details',
        'aria-current', 'aria-orientation', 'aria-sort', 'aria-level',
        'disabled', 'readonly', 'required',
        'checked', 'selected', 'multiple', 'accept', 'pattern', 'min', 'max', 'minlength',
        'maxlength', 'step', 'inputmode', 'autocomplete', 'list',
        'contenteditable', 'tabindex', 'datetime',
    ];
    const sensitiveAttributeMarkers = args.sensitiveAttributeMarkers || [];
    const normalizeText = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const isSensitiveAttribute = (name) => {
        const normalized = String(name || '').toLowerCase();
        return sensitiveAttributeMarkers.some((marker) => normalized.includes(marker));
    };
    const safeAttributeValue = (name, value) => {
        if (name !== 'data-key' && isSensitiveAttribute(name)) return null;
        return normalizeText(value).slice(0, args.maxAttributeValueLength || 120);
    };
    const semanticTags = new Set([
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'dt', 'dd', 'summary', 'figcaption',
        'blockquote', 'caption', 'td', 'th', 'time', 'main', 'article', 'section', 'ul', 'ol', 'table',
    ]);
    const semanticRoles = new Set([
        'alert', 'article', 'cell', 'columnheader', 'gridcell', 'heading', 'note', 'rowheader',
        'status', 'tabpanel', 'main', 'list', 'grid', 'table', 'region',
    ]);
    const nonAriaSuggestionSelector = [
        '[class*="suggest" i]', '[class*="option" i]', '[class*="result" i]',
        '[data-value]', '[data-key]'
    ].join(', ');
    const centralContainers = [
        'main', '[role="main"]', 'article', '[role="article"]', '[role="list"]',
        'ul', 'ol', '[role="grid"]', 'table',
    ].join(', ');
    const repeatedChromeContainers = [
        'nav', 'aside', 'header', 'footer', '[role="navigation"]', '[aria-label*="filter" i]',
    ].join(', ');
    const selectorMatches = new Map();

    const matchingElements = (selector) => {
        if (selectorMatches.has(selector)) return selectorMatches.get(selector);
        const matches = [];
        const visit = (root) => {
            for (const candidate of Array.from(root.querySelectorAll('*'))) {
                if (candidate.matches(selector)) matches.push(candidate);
                if (candidate.shadowRoot) visit(candidate.shadowRoot);
            }
        };
        visit(document);
        selectorMatches.set(selector, matches);
        return matches;
    };

    const isExposedAtPaintPoint = (element, rect) => {
        if (!args.paintOrderFiltering) return true;
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        // Keep off-viewport elements available for scrolling actions. Hit-testing
        // is meaningful only for points inside the current frame viewport.
        if (centerX < 0 || centerY < 0 || centerX >= innerWidth || centerY >= innerHeight) return true;
        let root = element.getRootNode();
        if (!root || typeof root.elementFromPoint !== 'function') root = document;
        const insetX = Math.min(2, rect.width / 4);
        const insetY = Math.min(2, rect.height / 4);
        const points = [
            [centerX, centerY],
            [rect.left + insetX, rect.top + insetY],
            [rect.right - insetX, rect.top + insetY],
            [rect.left + insetX, rect.bottom - insetY],
            [rect.right - insetX, rect.bottom - insetY],
        ];
        return points.some(([x, y]) => {
            const hit = root.elementFromPoint(x, y);
            return Boolean(hit && (
                hit === element
                || element.contains(hit)
                || (element.labels && Array.from(element.labels).includes(hit))
                || (root.host && hit === root.host)
            ));
        });
    };

    const elementPayload = (element, shadowRootType = null) => {
        const tagName = element.tagName.toLowerCase();
        // HTML portals can contain legacy colon-qualified names such as
        // <as:ajaxinclude>. They are valid DOM names but a bare CSS selector
        // treats the colon as a pseudo-class and aborts frame extraction.
        const tagSelector = CSS.escape(tagName);
        // Browser-Use parity: live values of secret, file, hidden, payment, and
        // one-time-code fields never reach the observation.
        const inputType = (element.getAttribute('type') || '').toLowerCase();
        const autocomplete = (element.getAttribute('autocomplete') || '').toLowerCase();
        const isSensitiveInput = (tagName === 'input' && ['password', 'file', 'hidden'].includes(inputType))
            || (['input', 'textarea'].includes(tagName)
                && (autocomplete.startsWith('cc-') || autocomplete.startsWith('one-time-code')));
        const isFrameElement = tagName === 'iframe' || tagName === 'frame';
        const isShadowHost = Boolean(element.shadowRoot)
            || (element.localName.includes('-') && !element.shadowRoot);
        const inCentralContent = Boolean(element.closest(centralContainers));
        const inRepeatedChrome = Boolean(element.closest(repeatedChromeContainers));
        const looksLikeClickable = tagName === 'button'
            || tagName === 'a'
            || element.hasAttribute('onclick')
            || window.getComputedStyle(element).cursor === 'pointer'
            || Boolean(element.closest('[class*="suggest" i], [class*="option" i]'));
        const isInteractive = selectorList.some((candidate) => element.matches(candidate))
            || tagName === 'summary'
            || (window.getComputedStyle(element).cursor === 'pointer'
                && (!element.parentElement
                    || window.getComputedStyle(element.parentElement).cursor !== 'pointer'))
            || typeof element.onclick === 'function'
            || ((element.matches(nonAriaSuggestionSelector) || element.hasAttribute('onclick')) && looksLikeClickable);
        const matchedSelector = selectorList.find((candidate) => element.matches(candidate))
            || (element.matches(nonAriaSuggestionSelector) ? nonAriaSuggestionSelector : tagSelector);

        let stableSelector = matchedSelector;
        if (element.id) {
            stableSelector = `#${CSS.escape(element.id)}`;
        } else if (element.getAttribute('data-testid')) {
            stableSelector = `[data-testid="${CSS.escape(element.getAttribute('data-testid'))}"]`;
        } else if (element.getAttribute('name')) {
            stableSelector = `${tagSelector}[name="${CSS.escape(element.getAttribute('name'))}"]`;
        } else if (element.getAttribute('aria-label')) {
            const ariaLabel = CSS.escape(element.getAttribute('aria-label'));
            stableSelector = `${matchedSelector}[aria-label="${ariaLabel}"]`;
        } else if (element.getAttribute('role')) {
            // ARIA roles are semantic and normally survive focus/selection class
            // changes. The ordinal is calculated in document order, including
            // hidden peers, so it remains compatible with locator().nth().
            stableSelector = `[role="${CSS.escape(element.getAttribute('role'))}"]`;
        } else if (element.getAttribute('type')) {
            stableSelector = `${tagSelector}[type="${CSS.escape(element.getAttribute('type'))}"]`;
        } else if (element.className && typeof element.className === 'string' && element.className.trim()) {
            const classes = element.className.trim().split(/\s+/).filter(c => c && !c.startsWith('data-'));
            if (classes.length > 0) {
                stableSelector = `${tagSelector}.${classes.map(c => CSS.escape(c)).join('.')}`;
            }
        }

        // locator(selector).nth(ordinal) counts every matching element,
        // including hidden elements that selected a different preferred
        // selector for themselves. Derive the ordinal from that same complete
        // match set so an indexed visible control cannot resolve to a hidden
        // peer such as another button[type=submit].
        // Real forms sometimes reuse one id on a wrapper and several controls.
        // An id selector is only unique when the document actually makes it so.
        const ordinal = Math.max(0, matchingElements(stableSelector).indexOf(element));
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        const attributes = {};
        for (const name of attributeNames) {
            if (element.hasAttribute(name)) {
                if (isSensitiveInput && name === 'value') continue;
                const value = safeAttributeValue(name, element.getAttribute(name) || '');
                if (value !== null) attributes[name] = value;
            }
        }

        const ownerElement = element.closest(
            'main, [role="main"], article, section, form, fieldset, [role="group"], [role="region"]'
        );
        const ownerLabel = ownerElement && ownerElement !== element
            ? normalizeText(
                ownerElement.getAttribute('aria-label')
                || ownerElement.getAttribute('title')
                || (ownerElement.querySelector('h1, h2, h3, legend') || {}).innerText
                || ''
            ).slice(0, args.maxAttributeValueLength || 120)
            : '';

        const labels = [];
        if (element.id) {
            for (const label of Array.from(document.querySelectorAll(
                `label[for="${CSS.escape(element.id)}"]`
            )).slice(0, 2)) {
                const text = normalizeText(label.innerText || label.textContent || '');
                if (text) labels.push(text.slice(0, args.maxAttributeValueLength || 120));
            }
        }
        const wrappingLabel = element.closest('label');
        if (wrappingLabel) {
            const text = normalizeText(wrappingLabel.innerText || wrappingLabel.textContent || '');
            if (text) labels.push(text.slice(0, args.maxAttributeValueLength || 120));
        }
        if (labels.length) {
            attributes['data-browser-use-camoufox-labels'] = [...new Set(labels)].join(' | ');
        }
        if (ownerLabel) attributes['data-browser-use-camoufox-owner'] = ownerLabel;

        // Keep the explanation attached to the control. Modern applications
        // often render help and validation text elsewhere in the DOM and join
        // it to the input only through ARIA ID references. Without resolving
        // those references, the model can see an invalid field and an error
        // message but cannot reliably tell that they belong together.
        const referencedText = (attributeName) => {
            const ids = (element.getAttribute(attributeName) || '').split(/\s+/).filter(Boolean);
            const root = element.getRootNode && element.getRootNode();
            const values = [];
            for (const id of ids.slice(0, 4)) {
                const referenced = (root && typeof root.getElementById === 'function'
                    ? root.getElementById(id)
                    : null) || document.getElementById(id);
                if (!referenced) continue;
                const value = normalizeText(referenced.innerText || referenced.textContent || '');
                if (value) values.push(value);
            }
            return [...new Set(values)].join(' | ');
        };
        const evidence = [];
        const addEvidence = (kind, value) => {
            const normalized = normalizeText(value);
            if (normalized) evidence.push(`${kind}: ${normalized}`);
        };
        addEvidence('label', referencedText('aria-labelledby'));
        addEvidence('description', referencedText('aria-describedby'));
        addEvidence('details', referencedText('aria-details'));
        const ariaInvalid = (element.getAttribute('aria-invalid') || '').toLowerCase();
        const explicitlyInvalid = Boolean(ariaInvalid && ariaInvalid !== 'false');
        let userInvalid = false;
        try {
            userInvalid = element.matches(':user-invalid');
        } catch(e) {}
        if (explicitlyInvalid || userInvalid) {
            addEvidence('error', referencedText('aria-errormessage'));
        }
        if (
            element.willValidate && element.validity && !element.validity.valid
            && (explicitlyInvalid || userInvalid)
        ) {
            addEvidence('validation', element.validationMessage || 'invalid value');
        }
        if (evidence.length) {
            attributes['data-browser-use-camoufox-semantic-evidence'] = [...new Set(evidence)]
                .join(' | ')
                .slice(0, args.maxSemanticEvidenceLength || 240);
        }

        for (const attr of Array.from(element.attributes)) {
            const value = safeAttributeValue(attr.name, attr.value);
            if (
                value !== null
                && attr.name.startsWith('data-')
                && attr.name.length <= 40
                && attr.value.length <= 120
                && (
                    attr.name === 'data-key'
                    || attr.name.startsWith('data-browser-use-camoufox-')
                    || Object.keys(attributes).filter((name) => (
                        name.startsWith('data-') && !name.startsWith('data-browser-use-camoufox-')
                    )).length < 8
                )
            ) {
                attributes[attr.name] = value;
            }
        }
        if (!isSensitiveInput && ['input', 'textarea', 'select'].includes(tagName)) {
            attributes.value = normalizeText(element.value || '').slice(0, args.maxAttributeValueLength || 120);
        }
        // Live state wins over the static default attribute in both directions.
        if ('checked' in element) {
            if (element.checked) attributes.checked = 'true';
            else delete attributes.checked;
        }
        if ('selected' in element) {
            if (element.selected) attributes.selected = 'true';
            else delete attributes.selected;
        }

        const disabled = element.disabled === true || element.getAttribute('aria-disabled') === 'true';
        let visible = rect.width > 0 && rect.height > 0
            && style.visibility !== 'hidden' && style.visibility !== 'collapse'
            && style.display !== 'none' && style.opacity !== '0';
        if (visible && typeof element.checkVisibility === 'function') {
            try {
                visible = element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
            } catch(e) {}
        }
        // Static text painted underneath a modal or menu is not observable either
        // (Browser-Use hides paint-order-occluded text). Hit testing skips
        // pointer-events:none, so those elements keep CSS visibility only.
        if (visible && (isInteractive || style.pointerEvents !== 'none')) {
            visible = isExposedAtPaintPoint(element, rect);
        }
        const text = normalizeText(
            element.innerText
            || (isSensitiveInput ? '' : element.value)
            || (isSensitiveInput ? '' : element.textContent)
            || element.getAttribute('aria-label')
            || element.getAttribute('datetime')
            || ''
        ).slice(0, 200);
        const role = element.getAttribute('role') || '';
        const hasObservableState = Object.keys(attributes).some((name) => (
            name.startsWith('aria-') || name.startsWith('data-') || name === 'datetime'
        ));
        // Preserve concise leaf text such as validation messages, totals, and
        // live status updates. These often use plain div/span elements and are
        // the only rendered proof that an action succeeded.
        const isConciseLeafText = text.length <= 200
            && element.childElementCount === 0
            && (inCentralContent || Boolean(element.id) || Boolean(role) || Boolean(element.closest('form')))
            && !['script', 'style', 'noscript', 'template'].includes(tagName);
        // Images inside controls carry the control's meaning (logo links, icon
        // buttons); Browser-Use serializes their alt/title/src as image context.
        const isControlImage = tagName === 'img'
            && Boolean(element.parentElement && element.parentElement.closest('a, button, [role="button"], [role="link"], [onclick]'));
        const isObservable = isInteractive || isFrameElement || isShadowHost || isControlImage || (
            text
            && (
                semanticTags.has(tagName)
                || semanticRoles.has(role)
                || hasObservableState
                || element.getAttribute('aria-label')
                || isConciseLeafText
            )
        );
        const geometry = [
            `${Math.round(rect.x)},${Math.round(rect.y)}`,
            `${Math.round(rect.width)}x${Math.round(rect.height)}`,
        ].join(',');
        attributes['data-browser-use-camoufox-geometry'] = geometry;

        return {
            tagName: element.nodeName,
            text,
            attributes,
            selector: stableSelector,
            ordinal,
            is_visible: visible,
            is_disabled: disabled,
            is_interactive: isInteractive && !disabled,
            is_observable: isObservable,
            frame_id: args.frameId,
            frame_url: args.frameUrl,
            frame_depth: args.frameDepth || 0,
            shadow_root_type: shadowRootType,
            is_shadow_host: isShadowHost,
            frame_ordinal: isFrameElement
                ? Array.from(document.querySelectorAll('iframe, frame')).indexOf(element)
                : null,
            priority:
                (inCentralContent ? 1000 : 0)
                - (inRepeatedChrome ? 100 : 0)
                + (isInteractive ? 10 : 0),
            rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
        };
    };

    const elements = [];
    let documentOrder = 0;
    const visited = new Set();

    const walkRoot = (root, shadowRootType = null) => {
        for (const element of Array.from(root.querySelectorAll('*'))) {
            if (now() > deadline) return;
            if (visited.has(element)) continue;
            visited.add(element);
            const effectiveShadowType = !element.shadowRoot && element.localName.includes('-')
                ? 'closed'
                : shadowRootType;
            const payload = {...elementPayload(element, effectiveShadowType), documentOrder};
            if (payload.is_visible && payload.is_observable) elements.push({element, payload});
            documentOrder += 1;
            if (element.shadowRoot) {
                walkRoot(element.shadowRoot, 'open');
            }
        }
    };

    walkRoot(document.body || document);

    const limited = elements
        .map((entry, index) => ({
            ...entry,
            payload: {
                ...entry.payload,
                originalOrder: entry.payload.documentOrder ?? index,
                tree_id: `${args.frameId}:${entry.payload.documentOrder ?? index}`,
            },
        }))
        .sort((left, right) => (
            (right.payload.priority - left.payload.priority)
            || (left.payload.originalOrder - right.payload.originalOrder)
        ))
        .slice(0, args.maxElements || 1000)
        .sort((left, right) => left.payload.originalOrder - right.payload.originalOrder);

    const treeIdByElement = new Map(
        limited.map(entry => [entry.element, entry.payload.tree_id])
    );
    const parentAcrossShadowBoundary = (element) => {
        if (element.parentElement) return element.parentElement;
        const root = element.getRootNode && element.getRootNode();
        return root && root.host ? root.host : null;
    };

    return limited.map(({element, payload}) => {
        let parent = parentAcrossShadowBoundary(element);
        while (parent && !treeIdByElement.has(parent)) {
            parent = parentAcrossShadowBoundary(parent);
        }
        return {
            ...payload,
            parent_tree_id: parent ? treeIdByElement.get(parent) : null,
        };
    });
};
"""
