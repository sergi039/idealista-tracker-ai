/**
 * Main JavaScript functionality for Idealista Land Watch & Rank
 */

// Global app object
window.IdealistaApp = {
    init: function() {
        this.setupGlobalErrorHandling();
        this.setupEventListeners();
        this.setupHTMX();
        this.setupTooltips();
        this.updateLastSync().catch(error => {
            console.warn('Initial sync update failed:', error);
        });
        this.setupTableInteractions();
    },

    setupGlobalErrorHandling: function() {
        // Handle unhandled Promise rejections
        window.addEventListener('unhandledrejection', function(event) {
            console.warn('Unhandled promise rejection:', event.reason);
            // Prevent the default console error
            event.preventDefault();
        });

        // Handle general JavaScript errors
        window.addEventListener('error', function(event) {
            console.warn('JavaScript error:', event.error || event.message);
        });
    },

    setupEventListeners: function() {
        console.log('[INIT] Setting up event listeners...');
        
        // Form auto-submission with debouncing
        this.setupFilterForms();
        
        // Table row click handling
        this.setupTableRowClicks();
        
        // Responsive table handling
        this.setupResponsiveTables();
        
        // Criteria form enhancements
        this.setupCriteriaForm();
        
        // Setup tabs with delay to ensure DOM is fully ready (only for non-criteria pages)
        if (!window.location.pathname.includes('/criteria')) {
            setTimeout(() => {
                console.log('[INIT] Setting up tabs after DOM ready...');
                this.setupCriteriaTabs();
            }, 100);
        } else {
            console.log('[INIT] Skipping global tab setup for criteria page - using inline script');
        }
        
        // Description enhancement functionality
        this.setupDescriptionEnhancement();
        
        // View switching functionality
        this.setupViewSwitching();
        
        console.log('[INIT] Event listeners setup completed');
    },

    setupHTMX: function() {
        // HTMX event listeners
        document.addEventListener('htmx:beforeRequest', function(evt) {
            const target = evt.target;
            
            // Add loading state
            target.classList.add('loading');
            
            // Show loading spinner if indicator exists
            const indicator = document.querySelector(target.getAttribute('hx-indicator'));
            if (indicator) {
                indicator.style.display = 'block';
            }
        });

        document.addEventListener('htmx:afterRequest', function(evt) {
            const target = evt.target;
            
            // Remove loading state
            target.classList.remove('loading');
            
            // Hide loading spinner
            const indicator = document.querySelector(target.getAttribute('hx-indicator'));
            if (indicator) {
                indicator.style.display = 'none';
            }
            
            // Handle successful responses
            if (evt.detail.successful) {
                // Special handling for sync buttons - prevent JSON from showing in button
                if (target.getAttribute('hx-post') && target.getAttribute('hx-post').includes('/api/ingest/email/run')) {
                    // Parse JSON response and show user-friendly message
                    try {
                        const response = JSON.parse(evt.detail.xhr.responseText);
                        let message = 'Sync completed';
                        
                        if (response.processed_count !== undefined) {
                            if (response.processed_count === 0) {
                                message = 'Sync completed - no new properties found';
                            } else {
                                message = `Sync completed - ${response.processed_count} new ${response.processed_count === 1 ? 'property' : 'properties'} added`;
                            }
                        }
                        
                        IdealistaApp.showNotification(message, 'success');
                        
                        // Restore button content (prevent JSON replacement)
                        if (target.textContent.startsWith('{')) {
                            // Safe DOM manipulation - prevent XSS
                            target.textContent = '';
                            const icon = document.createElement('i');
                            icon.className = 'fas fa-sync-alt me-1';
                            target.appendChild(icon);
                            target.appendChild(document.createTextNode('Manual Sync'));
                        }
                    } catch (e) {
                        IdealistaApp.showNotification('Sync completed successfully', 'success');
                        // Restore button content
                        if (target.textContent.startsWith('{')) {
                            // Safe DOM manipulation - prevent XSS
                            target.textContent = '';
                            const icon = document.createElement('i');
                            icon.className = 'fas fa-sync-alt me-1';
                            target.appendChild(icon);
                            target.appendChild(document.createTextNode('Manual Sync'));
                        }
                    }
                } else {
                    IdealistaApp.showNotification('Operation completed successfully', 'success');
                }
                
                // Update last sync time
                setTimeout(() => {
                    IdealistaApp.updateLastSync();
                }, 1000);
            }
        });

        document.addEventListener('htmx:responseError', function(evt) {
            let errorMessage = 'An error occurred';
            
            try {
                const response = JSON.parse(evt.detail.xhr.responseText);
                errorMessage = response.error || response.message || errorMessage;
            } catch (e) {
                errorMessage = `HTTP ${evt.detail.xhr.status}: ${evt.detail.xhr.statusText}`;
            }
            
            IdealistaApp.showNotification(errorMessage, 'error');
        });
    },

    setupTooltips: function() {
        // Initialize native HTML tooltips (title attribute)
        const tooltipElements = document.querySelectorAll('[data-tooltip], [title]');
        tooltipElements.forEach(element => {
            // Ensure tooltip content is available via title attribute
            const tooltipText = element.getAttribute('data-tooltip') || element.getAttribute('title');
            if (tooltipText && !element.getAttribute('title')) {
                element.setAttribute('title', tooltipText);
            }
        });
    },

    setupFilterForms: function() {
        const filterForm = document.getElementById('filter-form');
        if (!filterForm) return;

        let filterTimeout;
        const inputs = filterForm.querySelectorAll('input, select');
        
        inputs.forEach(input => {
            // Auto-submit on change (except search)
            input.addEventListener('change', function() {
                clearTimeout(filterTimeout);
                if (input.name !== 'search') {
                    filterTimeout = setTimeout(() => {
                        filterForm.submit();
                    }, 300);
                }
            });
            
            // Debounced search
            if (input.name === 'search') {
                input.addEventListener('input', function() {
                    clearTimeout(filterTimeout);
                    filterTimeout = setTimeout(() => {
                        filterForm.submit();
                    }, 1000);
                });
            }
        });

        // Clear filters button
        const clearButton = document.querySelector('a[href*="lands"]:not([href*="?"])');
        if (clearButton) {
            clearButton.addEventListener('click', function(e) {
                // Reset form before navigation
                filterForm.reset();
            });
        }
    },

    setupTableRowClicks: function() {
        document.querySelectorAll('.land-row').forEach(row => {
            row.addEventListener('click', function(e) {
                // Don't navigate if clicking on a button or link
                if (e.target.closest('.btn, a')) {
                    return;
                }
                
                const landId = this.getAttribute('data-land-id');
                if (landId) {
                    // Preserve current filter/sort state when navigating to detail
                    const currentUrl = new URL(window.location);
                    const params = currentUrl.searchParams.toString();
                    const targetUrl = `/lands/${landId}${params ? '?' + params : ''}`;
                    window.location.href = targetUrl;
                }
            });
            
            // Add keyboard navigation
            row.addEventListener('keypress', function(e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    const landId = this.getAttribute('data-land-id');
                    if (landId) {
                        // Preserve current filter/sort state when navigating to detail
                        const currentUrl = new URL(window.location);
                        const params = currentUrl.searchParams.toString();
                        const targetUrl = `/lands/${landId}${params ? '?' + params : ''}`;
                        window.location.href = targetUrl;
                    }
                }
            });
            
            // Make rows focusable for accessibility
            row.setAttribute('tabindex', '0');
            row.setAttribute('role', 'button');
            row.setAttribute('aria-label', 'View property details');
        });
    },

    setupResponsiveTables: function() {
        // Add data labels for responsive table
        const tables = document.querySelectorAll('.table-responsive table');
        tables.forEach(table => {
            const headers = table.querySelectorAll('thead th');
            const rows = table.querySelectorAll('tbody tr');
            
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                cells.forEach((cell, index) => {
                    if (headers[index]) {
                        cell.setAttribute('data-label', headers[index].textContent.trim());
                    }
                });
            });
        });
    },

    setupTableInteractions: function() {
        // Sort indicators
        document.querySelectorAll('th[data-sort]').forEach(header => {
            header.style.cursor = 'pointer';
            header.addEventListener('click', function() {
                const sortBy = this.getAttribute('data-sort');
                const currentUrl = new URL(window.location);
                const currentSort = currentUrl.searchParams.get('sort');
                const currentOrder = currentUrl.searchParams.get('order');
                
                // Toggle order if same column
                let newOrder = 'desc';
                if (currentSort === sortBy && currentOrder === 'desc') {
                    newOrder = 'asc';
                }
                
                currentUrl.searchParams.set('sort', sortBy);
                currentUrl.searchParams.set('order', newOrder);
                window.location.href = currentUrl.toString();
            });
        });
    },

    setupCriteriaTabs: function() {
        console.log('[TABS] Setting up criteria tabs...');
        
        // Wait for DOM to be fully ready
        const checkTabsExist = () => {
            const tabButtons = document.querySelectorAll('.md3-tab');
            const tabContents = document.querySelectorAll('.md3-tab-content');
            
            console.log('[TABS] Found', tabButtons.length, 'tab buttons and', tabContents.length, 'tab contents');
            
            if (tabButtons.length === 0) {
                console.log('[TABS] No tab buttons found, retrying in 100ms...');
                setTimeout(checkTabsExist, 100);
                return;
            }
            
            // Create simple tab switching function
            window.switchTab = function(targetId) {
                console.log('[TABS] Switching to tab:', targetId);
                
                // Hide all content
                tabContents.forEach(content => {
                    content.classList.remove('md3-tab-content--active');
                    console.log('[TABS] Hiding content:', content.id);
                });
                
                // Deactivate all tabs
                tabButtons.forEach(btn => {
                    btn.classList.remove('md3-tab--active');
                    btn.setAttribute('aria-selected', 'false');
                });
                
                // Show target content
                const targetContent = document.getElementById(targetId);
                if (targetContent) {
                    targetContent.classList.add('md3-tab-content--active');
                    console.log('[TABS] Showing content:', targetId);
                }
                
                // Activate clicked tab
                const targetTab = document.querySelector(`[data-target="${targetId}"]`);
                if (targetTab) {
                    targetTab.classList.add('md3-tab--active');
                    targetTab.setAttribute('aria-selected', 'true');
                    console.log('[TABS] Activating tab for:', targetId);
                }
                
                // Initialize form for the active tab
                if (targetId === 'investment' || targetId === 'lifestyle') {
                    setTimeout(() => {
                        IdealistaApp.initializeActiveProfile(targetId);
                    }, 100);
                }
            };
            
            // Attach click listeners to each tab
            tabButtons.forEach((button, index) => {
                const targetId = button.getAttribute('data-target');
                console.log('[TABS] Setting up button', index, 'with data-target:', targetId);
                
                button.addEventListener('click', function(e) {
                    e.preventDefault();
                    console.log('[TABS] Tab clicked:', targetId);
                    window.switchTab(targetId);
                });
                
                // Keyboard navigation
                button.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        window.switchTab(targetId);
                    }
                });
            });
            
            console.log('[TABS] Tab setup completed successfully');
        };
        
        // Start checking for tabs
        checkTabsExist();
    },

    setupCriteriaForm: function() {
        // Setup both investment and lifestyle forms
        ['investment', 'lifestyle'].forEach(profile => {
            this.setupProfileForm(profile);
        });
        
        // Initialize the currently active profile
        const activeTab = document.querySelector('.md3-tab--active');
        if (activeTab) {
            const activeProfile = activeTab.getAttribute('data-target');
            setTimeout(() => {
                this.initializeActiveProfile(activeProfile);
            }, 100);
        }
    },

    setupProfileForm: function(profile) {
        const form = document.getElementById(`${profile}-form`);
        if (!form) return;
        
        // Weight adjustment buttons
        const adjustButtons = form.querySelectorAll('button.weight-adjust');
        adjustButtons.forEach(button => {
            button.addEventListener('click', function(e) {
                e.preventDefault();
                const criteria = this.getAttribute('data-criteria');
                const profile = this.getAttribute('data-profile');
                const delta = parseFloat(this.getAttribute('data-delta'));
                const input = document.getElementById(`${profile}_weight_${criteria}`);
                
                if (input) {
                    const newValue = Math.max(0, Math.min(1, parseFloat(input.value) + delta));
                    input.value = newValue.toFixed(2);
                    
                    // Update slider and visualizations
                    IdealistaApp.syncSliderWithInput(profile, criteria, newValue);
                    IdealistaApp.normalizeProfileWeights(profile);
                    IdealistaApp.updateProfileVisualizations(profile);
                }
            });
        });
        
        // Slider interactions
        const sliders = form.querySelectorAll('input[type="range"].weight-slider');
        sliders.forEach(slider => {
            slider.addEventListener('input', function() {
                const criteria = this.getAttribute('data-criteria');
                const profile = this.getAttribute('data-profile');
                const value = parseFloat(this.value) / 100;
                const weightInput = document.getElementById(`${profile}_weight_${criteria}`);
                
                if (weightInput) {
                    weightInput.value = value.toFixed(2);
                    IdealistaApp.normalizeProfileWeights(profile);
                    IdealistaApp.updateProfileVisualizations(profile);
                }
            });
        });
        
        // Number input changes
        const numberInputs = form.querySelectorAll('input[type="number"].weight-input');
        numberInputs.forEach(input => {
            input.addEventListener('input', function() {
                const inputName = this.name.replace('weight_', '');
                const profile = this.id.split('_')[0]; // Extract profile from ID
                const value = Math.max(0, Math.min(1, parseFloat(this.value) || 0));
                
                // Sync with slider
                IdealistaApp.syncSliderWithInput(profile, inputName, value);
                IdealistaApp.normalizeProfileWeights(profile);
                IdealistaApp.updateProfileVisualizations(profile);
            });
        });
        
        // Form validation
        form.addEventListener('submit', function(e) {
            if (!IdealistaApp.validateProfileForm(profile)) {
                e.preventDefault();
                return false;
            }
            
            // Confirmation dialog
            const profileName = profile.charAt(0).toUpperCase() + profile.slice(1);
            if (!confirm(`This will update ${profileName} profile weights and rescore all properties. This may take a few moments. Continue?`)) {
                e.preventDefault();
                return false;
            }
            
            // Show loading state
            IdealistaApp.setFormLoadingState(form, true);
        });
    },

    initializeActiveProfile: function(profile) {
        if (!profile || (profile !== 'investment' && profile !== 'lifestyle')) return;
        
        // Normalize weights and update visualizations for the active profile
        this.normalizeProfileWeights(profile);
        this.updateProfileVisualizations(profile);
    },

    syncSliderWithInput: function(profile, criteria, value) {
        const slider = document.getElementById(`${profile}_slider_${criteria}`);
        if (slider) {
            slider.value = (value * 100).toFixed(0);
        }
    },

    normalizeProfileWeights: function(profile) {
        const form = document.getElementById(`${profile}-form`);
        if (!form) return;
        
        const inputs = form.querySelectorAll('input[type="number"].weight-input');
        const weights = [];
        
        // Collect all weights
        inputs.forEach(input => {
            const weight = Math.max(0, parseFloat(input.value) || 0);
            weights.push(weight);
        });
        
        // Calculate sum
        const sum = weights.reduce((a, b) => a + b, 0);
        
        if (sum === 0) {
            // If all weights are 0, distribute equally
            const equalWeight = 1 / weights.length;
            inputs.forEach(input => {
                input.value = equalWeight.toFixed(2);
            });
        } else if (Math.abs(sum - 1) > 0.001) {
            // Normalize to sum to 1 (100%)
            inputs.forEach((input, index) => {
                const normalizedWeight = weights[index] / sum;
                input.value = normalizedWeight.toFixed(2);
                
                // Sync sliders
                const inputName = input.name.replace('weight_', '');
                IdealistaApp.syncSliderWithInput(profile, inputName, normalizedWeight);
            });
        }
    },

    updateProfileVisualizations: function(profile) {
        const form = document.getElementById(`${profile}-form`);
        if (!form) return;
        
        const inputs = form.querySelectorAll('input[type="number"].weight-input');
        let totalWeight = 0;
        
        inputs.forEach(input => {
            const inputName = input.name.replace('weight_', '');
            const weight = parseFloat(input.value) || 0;
            const progressBar = document.getElementById(`${profile}_progress_${inputName}`);
            const percentSpan = document.getElementById(`${profile}_percent_${inputName}`);
            
            totalWeight += weight;
            
            if (progressBar) {
                const percentage = weight * 100;
                progressBar.style.width = `${percentage}%`;
                progressBar.setAttribute('data-weight', weight.toString());
            }
            
            if (percentSpan) {
                percentSpan.textContent = `${(weight * 100).toFixed(1)}%`;
            }
        });
        
        // Update total weight indicator
        const weightBar = document.getElementById(`${profile}-weight-bar`);
        const weightText = document.getElementById(`${profile}-weight-text`);
        
        if (weightBar && weightText) {
            const percentage = Math.min(100, totalWeight * 100);
            weightBar.style.width = `${percentage}%`;
            weightText.textContent = `${percentage.toFixed(1)}%`;
            
            // Color coding based on total
            if (Math.abs(totalWeight - 1) < 0.001) {
                weightBar.className = 'md3-progress-bar md3-progress-bar--success';
            } else if (totalWeight > 1.1) {
                weightBar.className = 'md3-progress-bar md3-progress-bar--error';
            } else {
                weightBar.className = 'md3-progress-bar md3-progress-bar--warning';
            }
        }
    },

    validateProfileForm: function(profile) {
        const form = document.getElementById(`${profile}-form`);
        if (!form) return false;
        
        const inputs = form.querySelectorAll('input[type="number"].weight-input');
        let totalWeight = 0;
        let hasError = false;
        
        inputs.forEach(input => {
            const weight = parseFloat(input.value) || 0;
            totalWeight += weight;
            
            if (weight < 0) {
                IdealistaApp.showNotification('Weights cannot be negative', 'error');
                hasError = true;
            }
            
            if (weight > 1) {
                IdealistaApp.showNotification('Individual weights cannot exceed 100%', 'error');
                hasError = true;
            }
        });
        
        // Check if sum is approximately 1 (100%)
        if (Math.abs(totalWeight - 1) > 0.01) {
            IdealistaApp.showNotification(`${profile.charAt(0).toUpperCase() + profile.slice(1)} profile weights must sum to 100% (currently ${(totalWeight * 100).toFixed(1)}%)`, 'error');
            hasError = true;
        }
        
        return !hasError;
    },

    setFormLoadingState: function(form, loading) {
        const submitButton = form.querySelector('button[type="submit"]');
        if (!submitButton) return;
        
        if (loading) {
            // Store original content
            submitButton.dataset.originalContent = submitButton.innerHTML;
            
            // Set loading state
            submitButton.innerHTML = '<span class="material-symbols-outlined">hourglass_empty</span>Updating...';
            submitButton.disabled = true;
        } else {
            // Restore original content
            if (submitButton.dataset.originalContent) {
                submitButton.innerHTML = submitButton.dataset.originalContent;
            }
            submitButton.disabled = false;
        }
    },

    normalizeWeightsMCDM: function(changedInput = null) {
        // Legacy function - redirect to appropriate profile
        const activeTab = document.querySelector('.md3-tab--active');
        if (activeTab) {
            const profile = activeTab.getAttribute('data-target');
            if (profile === 'investment' || profile === 'lifestyle') {
                this.normalizeProfileWeights(profile);
            }
        }
    },

    updateWeightVisualizations: function() {
        // Legacy function - redirect to appropriate profile
        const activeTab = document.querySelector('.md3-tab--active');
        if (activeTab) {
            const profile = activeTab.getAttribute('data-target');
            if (profile === 'investment' || profile === 'lifestyle') {
                this.updateProfileVisualizations(profile);
            }
        }
    },

    validateCriteriaForm: function() {
        // Legacy function - redirect to appropriate profile
        const activeTab = document.querySelector('.md3-tab--active');
        if (activeTab) {
            const profile = activeTab.getAttribute('data-target');
            if (profile === 'investment' || profile === 'lifestyle') {
                return this.validateProfileForm(profile);
            }
        }
        return false;
    },

    updateLastSync: async function() {
        // Fetch and update last sync time
        try {
            const response = await fetch('/api/stats');
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            
            const data = await response.json();
            if (data.success && data.stats.last_sync) {
                const lastSyncElement = document.getElementById('last-sync');
                if (lastSyncElement) {
                    const sync = data.stats.last_sync;
                    
                    // Handle null or invalid completed_at
                    if (sync.completed_at) {
                        const date = new Date(sync.completed_at);
                        // Check if date is valid
                        if (!isNaN(date.getTime()) && date.getFullYear() > 1970) {
                            const dateStr = date.toLocaleDateString();
                            const timeStr = date.toLocaleTimeString();
                            lastSyncElement.textContent = `${dateStr} at ${timeStr} (+${sync.new_properties} new)`;
                        } else {
                            lastSyncElement.textContent = `Sync completed (+${sync.new_properties} new) - time unavailable`;
                        }
                    } else {
                        lastSyncElement.textContent = `Sync completed (+${sync.new_properties} new) - time unavailable`;
                    }
                }
            } else {
                const lastSyncElement = document.getElementById('last-sync');
                if (lastSyncElement) {
                    lastSyncElement.textContent = 'No sync data';
                }
            }
        } catch (error) {
            console.warn('Failed to update last sync time:', error);
            const lastSyncElement = document.getElementById('last-sync');
            if (lastSyncElement) {
                lastSyncElement.textContent = 'Error loading';
            }
        }
    },

    showNotification: function(message, type = 'info') {
        // Create notification element
        const alertClass = type === 'error' ? 'alert-danger' : 
                          type === 'success' ? 'alert-success' : 'alert-info';
        
        const notification = document.createElement('div');
        notification.className = `alert ${alertClass} alert-dismissible fade show position-fixed`;
        notification.style.cssText = 'top: 20px; right: 20px; z-index: 9999; max-width: 400px;';
        
        // Safe DOM manipulation - prevent XSS
        notification.textContent = message;
        
        const closeButton = document.createElement('button');
        closeButton.type = 'button';
        closeButton.className = 'btn-close';
        closeButton.setAttribute('data-bs-dismiss', 'alert');
        closeButton.setAttribute('aria-label', 'Close');
        
        notification.appendChild(closeButton);
        
        document.body.appendChild(notification);
        
        // Auto-remove after 5 seconds
        setTimeout(() => {
            if (notification.parentNode) {
                notification.remove();
            }
        }, 5000);
    },

    // Utility functions
    formatCurrency: function(amount) {
        return new Intl.NumberFormat('es-ES', {
            style: 'currency',
            currency: 'EUR',
            minimumFractionDigits: 0,
            maximumFractionDigits: 0
        }).format(amount);
    },

    formatArea: function(area) {
        return new Intl.NumberFormat('es-ES', {
            minimumFractionDigits: 0,
            maximumFractionDigits: 0
        }).format(area) + ' m²';
    },

    formatDistance: function(meters) {
        if (meters < 1000) {
            return Math.round(meters) + ' m';
        } else {
            return (meters / 1000).toFixed(1) + ' km';
        }
    },

    formatDuration: function(seconds) {
        const minutes = Math.round(seconds / 60);
        if (minutes < 60) {
            return minutes + ' min';
        } else {
            const hours = Math.floor(minutes / 60);
            const remainingMinutes = minutes % 60;
            return hours + 'h' + (remainingMinutes > 0 ? ' ' + remainingMinutes + 'm' : '');
        }
    },

    // Export functionality
    exportTableToCSV: function(tableId, filename = 'export.csv') {
        const table = document.getElementById(tableId);
        if (!table) return;
        
        const csv = [];
        const rows = table.querySelectorAll('tr');
        
        rows.forEach(row => {
            const cols = row.querySelectorAll('td, th');
            const rowData = [];
            
            cols.forEach(col => {
                let cellData = col.textContent.trim();
                // Escape quotes and wrap in quotes if contains comma
                if (cellData.includes(',') || cellData.includes('"')) {
                    cellData = '"' + cellData.replace(/"/g, '""') + '"';
                }
                rowData.push(cellData);
            });
            
            csv.push(rowData.join(','));
        });
        
        // Download CSV
        const csvContent = csv.join('\n');
        const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        
        if (link.download !== undefined) {
            const url = URL.createObjectURL(blob);
            link.setAttribute('href', url);
            link.setAttribute('download', filename);
            link.style.visibility = 'hidden';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }
    },

    // Description Enhancement Functions
    setupDescriptionEnhancement: function() {
        console.log('[DESC] Setting up description enhancement...');
        
        // Check if we're on the property detail page
        const descriptionSection = document.getElementById('description-section');
        if (!descriptionSection) {
            console.log('[DESC] No description section found, skipping');
            return;
        }

        console.log('[DESC] Description section found, initializing UI...');
        // Initialize description enhancement UI
        this.initializeDescriptionUI();
        
        // Setup language toggle
        const langToggle = document.getElementById('description-language-toggle');
        if (langToggle) {
            console.log('[DESC] Language toggle found, adding event listener');
            langToggle.addEventListener('click', this.handleLanguageToggle.bind(this));
        } else {
            console.log('[DESC] Language toggle not found');
        }
    },

    initializeDescriptionUI: function() {
        console.log('[DESC] Initializing description UI...');
        
        // Check if enhanced description already exists
        const landId = document.querySelector('[data-land-id]')?.getAttribute('data-land-id');
        if (!landId) {
            console.log('[DESC] No land ID found, skipping description enhancement');
            return;
        }

        console.log(`[DESC] Found land ID: ${landId}, fetching description variants...`);

        // Fetch existing description variants
        fetch(`/api/description/variants/${landId}`)
            .then(response => response.json())
            .then(data => {
                console.log('[DESC] Received description variants response:', data);
                if (data.success) {
                    if (data.status === 'not_processed') {
                        console.log('[DESC] Description not processed, auto-enhancing...');
                        // Auto-enhance the description silently
                        this.autoEnhanceDescription(landId);
                    } else {
                        console.log('[DESC] Description already processed, displaying enhanced version...');
                        // Show language toggle and enhanced description
                        this.displayEnhancedDescription(data);
                    }
                } else {
                    console.log('[DESC] API returned error:', data.error);
                }
            })
            .catch(error => {
                console.error('[DESC] Failed to load description variants:', error);
                // Try to auto-enhance as fallback
                this.autoEnhanceDescription(landId);
            });
    },

    autoEnhanceDescription: function(landId) {
        // Silently enhance the description in the background
        fetch(`/api/enhance/description/${landId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            }
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Display enhanced description with language toggle
                this.displayEnhancedDescription(data);
            } else {
                // Keep original description visible, hide any loading indicators
                console.log('Auto-enhancement failed, keeping original description');
            }
        })
        .catch(error => {
            console.error('Auto-enhancement failed:', error);
            // Keep original description visible
        });
    },


    displayEnhancedDescription: function(data) {
        // Store description variants for language switching
        this.descriptionData = data;
        
        // Update enhanced description content (default to English)
        const enhancedTextEl = document.getElementById('enhanced-description-text');
        if (enhancedTextEl) {
            enhancedTextEl.textContent = data.enhanced_en || data.enhanced_description || data.enhanced;
        }

        // Display key highlights if available
        if (data.key_highlights && data.key_highlights.length > 0) {
            const highlightsEl = document.getElementById('highlights-list');
            if (highlightsEl) {
                // Clear existing content
                highlightsEl.textContent = '';
                
                // Create highlight badges using safe DOM methods
                data.key_highlights.forEach(highlight => {
                    const span = document.createElement('span');
                    span.className = 'badge bg-info me-1 mb-1';
                    span.textContent = highlight;
                    highlightsEl.appendChild(span);
                });
                document.getElementById('description-highlights').style.display = 'block';
            }
        }

        // Display price info if available
        if (data.price_info && Object.keys(data.price_info).length > 0) {
            const priceInfo = data.price_info;
            let priceText = '';
            
            if (priceInfo.current_price) {
                priceText = `Current price: €${priceInfo.current_price.toLocaleString()}`;
                
                if (priceInfo.original_price && priceInfo.original_price !== priceInfo.current_price) {
                    priceText += ` (Originally €${priceInfo.original_price.toLocaleString()}`;
                    if (priceInfo.discount) {
                        priceText += ` - ${priceInfo.discount}% off!`;
                    }
                    priceText += ')';
                }
            }
            
            if (priceText) {
                const priceInfoText = document.getElementById('price-info-text');
                if (priceInfoText) {
                    priceInfoText.textContent = priceText;
                    document.getElementById('price-info-section').style.display = 'block';
                }
            }
        }

        // Show enhanced description and language toggle
        const enhancedDesc = document.getElementById('enhanced-description');
        const originalDesc = document.getElementById('original-description');
        const langToggle = document.getElementById('description-language-toggle');
        
        if (enhancedDesc && originalDesc && langToggle) {
            enhancedDesc.style.display = 'block';
            originalDesc.style.display = 'none';
            langToggle.style.display = 'block';
            
            // Ensure English button is active
            const enBtn = document.querySelector('[data-lang="en"]');
            const esBtn = document.querySelector('[data-lang="es"]'); 
            const originalBtn = document.querySelector('[data-lang="original"]');
            if (enBtn) {
                enBtn.classList.add('active');
            }
            if (esBtn) {
                esBtn.classList.remove('active');
            }
            if (originalBtn) {
                originalBtn.classList.remove('active');
            }
        }
    },

    handleLanguageToggle: function(event) {
        if (!event.target.hasAttribute('data-lang')) return;
        
        const lang = event.target.getAttribute('data-lang');
        const enhancedTextEl = document.getElementById('enhanced-description-text');
        
        // Update content based on language selection  
        if (enhancedTextEl && this.descriptionData) {
            if (lang === 'en') {
                enhancedTextEl.textContent = this.descriptionData.enhanced_en || this.descriptionData.enhanced_description || this.descriptionData.enhanced;
            } else if (lang === 'es') {
                enhancedTextEl.textContent = this.descriptionData.enhanced_es || this.descriptionData.original;
            } else if (lang === 'original') {
                // Show original Idealista description if available, else fallback to email description
                const originalIdealistaDesc = this.getOriginalIdealistaDescription();
                enhancedTextEl.textContent = originalIdealistaDesc || this.descriptionData.original || 'Original Idealista description not available';
            }
        }
        
        // Update button states
        document.querySelectorAll('[data-lang]').forEach(btn => {
            btn.classList.remove('active');
        });
        event.target.classList.add('active');
    },

    getOriginalIdealistaDescription: function() {
        // Return the original email description since Idealista scraping is no longer available
        try {
            if (this.descriptionData && this.descriptionData.original) {
                return this.descriptionData.original + ' (from email source)';
            }
            
            return 'Original description not available';
        } catch (e) {
            console.log('[DESC] Could not get original description:', e);
            return 'Error loading original description';
        }
    },

    showDescriptionLoading: function(show) {
        const loadingEl = document.getElementById('description-loading');
        if (loadingEl) {
            loadingEl.style.display = show ? 'flex' : 'none';
        }
    }
};

// Helper function to update URL parameters
function updateUrlParameter(url, param, paramVal) {
    var newAdditionalURL = "";
    var tempArray = url.split("?");
    var baseURL = tempArray[0];
    var additionalURL = tempArray[1];
    var temp = "";
    
    if (additionalURL) {
        tempArray = additionalURL.split("&");
        for (var i = 0; i < tempArray.length; i++) {
            if (tempArray[i].split('=')[0] != param) {
                newAdditionalURL += temp + tempArray[i];
                temp = "&";
            }
        }
    }
    
    var rows_txt = temp + "" + param + "=" + paramVal;
    return baseURL + "?" + newAdditionalURL + rows_txt;
}

// Environment editing functions
function toggleEnvironmentEdit(landId) {
    const displayDiv = document.getElementById('environment-display');
    const editDiv = document.getElementById('environment-edit');
    const editBtn = document.getElementById('env-edit-btn');
    
    if (editDiv.style.display === 'none') {
        displayDiv.style.display = 'none';
        editDiv.style.display = 'block';
        editBtn.innerHTML = '<i class="fas fa-times"></i> Cancel';
    } else {
        displayDiv.style.display = 'block';
        editDiv.style.display = 'none';
        editBtn.innerHTML = '<i class="fas fa-edit"></i> Edit';
    }
}

function cancelEnvironmentEdit() {
    const displayDiv = document.getElementById('environment-display');
    const editDiv = document.getElementById('environment-edit');
    const editBtn = document.getElementById('env-edit-btn');
    
    displayDiv.style.display = 'block';
    editDiv.style.display = 'none';
    editBtn.innerHTML = '<i class="fas fa-edit"></i> Edit';
}

function saveEnvironment(event, landId) {
    event.preventDefault();
    
    const form = document.getElementById('environment-form');
    const formData = new FormData(form);
    
    // Build environment object
    const environment = {
        sea_view: formData.has('sea_view'),
        mountain_view: formData.has('mountain_view'),
        forest_view: formData.has('forest_view'),
        orientation: formData.get('orientation') || '',
        buildable_floors: formData.get('buildable_floors') || '',
        access_type: formData.get('access_type') || '',
        certified_for: formData.get('certified_for') || ''
    };
    
    // Send update to server
    fetch(`/api/land/${landId}/environment`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(environment)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Reload page to show updated data
            location.reload();
        } else {
            alert('Failed to update environment data: ' + (data.error || 'Unknown error'));
        }
    })
    .catch(error => {
        console.error('Error updating environment:', error);
        alert('Failed to update environment data');
    });
    },

    // View Switching Functionality
    setupViewSwitching: function() {
        console.log('[INIT] Setting up view switching...');
        
        // Initialize view on page load
        this.initializeViewOnLoad();
        
        // Handle browser back/forward buttons
        window.addEventListener('popstate', (event) => {
            if (event.state && event.state.viewType) {
                const viewType = event.state.viewType;
                this.switchView(viewType);
            }
        });
        
        console.log('[INIT] View switching setup completed');
    },
    
    switchView: function(viewType) {
        const listView = document.getElementById('list-view');
        const cardsView = document.getElementById('cards-view');
        const listBtn = document.getElementById('view-list-btn');
        const cardsBtn = document.getElementById('view-cards-btn');
        
        if (!listView || !cardsView || !listBtn || !cardsBtn) {
            console.warn('View switching elements not found');
            return;
        }
        
        // Save user preference to localStorage
        localStorage.setItem('preferredViewType', viewType);
        
        // Add transition classes
        listView.style.transition = 'opacity 0.3s ease-in-out';
        cardsView.style.transition = 'opacity 0.3s ease-in-out';
        
        if (viewType === 'list') {
            // Fade out current view
            cardsView.style.opacity = '0';
            setTimeout(() => {
                cardsView.style.display = 'none';
                listView.style.display = 'block';
                listView.style.opacity = '0';
                // Fade in new view
                setTimeout(() => {
                    listView.style.opacity = '1';
                }, 10);
            }, 300);
            
            // Update button states - use MD3 classes
            listBtn.classList.add('md3-button--filled');
            cardsBtn.classList.remove('md3-button--filled');
        } else {
            // Fade out current view
            listView.style.opacity = '0';
            setTimeout(() => {
                listView.style.display = 'none';
                cardsView.style.display = 'block';
                cardsView.style.opacity = '0';
                // Fade in new view
                setTimeout(() => {
                    cardsView.style.opacity = '1';
                }, 10);
            }, 300);
            
            // Update button states - use MD3 classes
            cardsBtn.classList.add('md3-button--filled');
            listBtn.classList.remove('md3-button--filled');
        }
        
        // Update URL without page reload
        this.updateViewTypeInUrl(viewType);
    },
    
    updateViewTypeInUrl: function(viewType) {
        const url = new URL(window.location);
        url.searchParams.set('view_type', viewType);
        
        // Update URL without reload
        window.history.pushState({ viewType: viewType }, '', url.toString());
    },
    
    initializeViewOnLoad: function() {
        const listView = document.getElementById('list-view');
        const cardsView = document.getElementById('cards-view');
        const listBtn = document.getElementById('view-list-btn');
        const cardsBtn = document.getElementById('view-cards-btn');
        
        if (!listView || !cardsView || !listBtn || !cardsBtn) {
            console.warn('View elements not found during initialization');
            return;
        }
        
        // Load user preference from localStorage
        const savedViewType = localStorage.getItem('preferredViewType');
        const url = new URL(window.location);
        const currentServerView = url.searchParams.get('view_type') || 'cards';
        
        if (savedViewType && (savedViewType === 'list' || savedViewType === 'cards')) {
            // Apply saved preference if different from server-side default
            if (savedViewType !== currentServerView) {
                // Switch instantly without animation on page load
                if (savedViewType === 'list') {
                    cardsView.style.display = 'none';
                    listView.style.display = 'block';
                    listView.style.opacity = '1';
                    listBtn.classList.add('md3-button--filled');
                    cardsBtn.classList.remove('md3-button--filled');
                    
                    // Update URL without reload
                    url.searchParams.set('view_type', 'list');
                    window.history.replaceState({ viewType: 'list' }, '', url.toString());
                } else {
                    listView.style.display = 'none';
                    cardsView.style.display = 'block';
                    cardsView.style.opacity = '1';
                    cardsBtn.classList.add('md3-button--filled');
                    listBtn.classList.remove('md3-button--filled');
                    
                    // Update URL without reload
                    url.searchParams.set('view_type', 'cards');
                    window.history.replaceState({ viewType: 'cards' }, '', url.toString());
                }
            } else {
                // Set opacity for the current view
                if (listView.style.display !== 'none') {
                    listView.style.opacity = '1';
                    listBtn.classList.add('md3-button--filled');
                    cardsBtn.classList.remove('md3-button--filled');
                }
                if (cardsView.style.display !== 'none') {
                    cardsView.style.opacity = '1';
                    cardsBtn.classList.add('md3-button--filled');
                    listBtn.classList.remove('md3-button--filled');
                }
            }
        } else {
            // Set opacity for the current view (no saved preference)
            if (listView.style.display !== 'none') {
                listView.style.opacity = '1';
                listBtn.classList.add('md3-button--filled');
                cardsBtn.classList.remove('md3-button--filled');
            }
            if (cardsView.style.display !== 'none') {
                cardsView.style.opacity = '1';
                cardsBtn.classList.add('md3-button--filled');
                listBtn.classList.remove('md3-button--filled');
            }
        }
    }
};

// Make switchView globally accessible for template onclick handlers
window.switchView = function(viewType) {
    if (window.IdealistaApp && window.IdealistaApp.switchView) {
        window.IdealistaApp.switchView(viewType);
    } else {
        console.error('IdealistaApp not ready for view switching');
    }
};

// Mode switching functionality (moved from template)
window.switchMode = function(mode) {
    const url = new URL(window.location);
    url.searchParams.set('mode', mode);
    
    // Auto-sync sort with mode selection for better UX
    const currentSort = url.searchParams.get('sort');
    if (mode === 'investment' && currentSort !== 'score_investment') {
        url.searchParams.set('sort', 'score_investment');
    } else if (mode === 'lifestyle' && currentSort !== 'score_lifestyle') {
        url.searchParams.set('sort', 'score_lifestyle');
    } else if (mode === 'combined' && currentSort !== 'score_total') {
        url.searchParams.set('sort', 'score_total');
    }
    
    // Reset to first page when changing mode
    url.searchParams.delete('page');
    
    // Update the URL and reload to apply mode change
    window.location.href = url.toString();
};

// Initialize app when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    console.log('[INIT] DOM loaded, initializing app...');
    window.IdealistaApp.init();
    console.log('[INIT] App initialization completed');
    
    // Extra fallback for tabs specifically
    setTimeout(() => {
        console.log('[INIT] Fallback tabs setup...');
        if (window.IdealistaApp && typeof window.IdealistaApp.setupCriteriaTabs === 'function') {
            window.IdealistaApp.setupCriteriaTabs();
        }
    }, 500);
});

// Handle page visibility changes
document.addEventListener('visibilitychange', function() {
    if (!document.hidden) {
        // Page became visible, update last sync time
        setTimeout(() => {
            window.IdealistaApp.updateLastSync();
        }, 1000);
    }
});

// Global error handler
window.addEventListener('error', function(e) {
    console.error('JavaScript error:', e.error);
    // Could send to logging service in production
});

// Service Worker registration (for future PWA capabilities)
if ('serviceWorker' in navigator) {
    window.addEventListener('load', function() {
        // Service worker would be implemented later for offline capabilities
    });
}

// Toggle Score Breakdown Details
function toggleScoreBreakdown() {
    const details = document.getElementById('score-breakdown-details');
    const button = document.getElementById('score-breakdown-btn');
    const icon = button ? button.querySelector('.material-icons') : null;
    const textSpan = button ? button.querySelector('span:not(.material-icons)') : null;
    
    if (details && button && icon) {
        const isHidden = details.style.display === 'none' || !details.style.display;
        
        if (isHidden) {
            details.style.display = 'block';
            icon.textContent = 'expand_less';
            if (textSpan) textSpan.textContent = 'Hide Detailed Breakdown';
        } else {
            details.style.display = 'none';
            icon.textContent = 'expand_more';
            if (textSpan) textSpan.textContent = 'View Detailed Breakdown';
        }
    }
}
