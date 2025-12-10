/**
 * Main JavaScript functionality for Idealista Land Watch & Rank
 */

// Global app object
window.IdealistaApp = {
    init: function() {
        this.setupEventListeners();
        this.setupHTMX();
        this.setupTooltips();
        this.updateLastSync();
        this.setupTableInteractions();
    },

    setupEventListeners: function() {
        // Form auto-submission with debouncing
        this.setupFilterForms();
        
        // Table row click handling
        this.setupTableRowClicks();
        
        // Responsive table handling
        this.setupResponsiveTables();
        
        // Criteria form enhancements
        this.setupCriteriaForm();
        
        // Description enhancement functionality
        this.setupDescriptionEnhancement();
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
        // Initialize Bootstrap tooltips
        const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
        const tooltipList = tooltipTriggerList.map(function(tooltipTriggerEl) {
            return new bootstrap.Tooltip(tooltipTriggerEl);
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

    setupCriteriaForm: function() {
        const criteriaForm = document.getElementById('criteria-form');
        if (!criteriaForm) return;

        // MCDM weight normalization on input change
        const inputs = criteriaForm.querySelectorAll('input[type="number"]');
        inputs.forEach(input => {
            input.addEventListener('input', function() {
                IdealistaApp.normalizeWeightsMCDM(this);
                IdealistaApp.updateWeightVisualizations();
            });
        });

        // Weight adjustment buttons
        const adjustButtons = criteriaForm.querySelectorAll('button.weight-adjust');
        adjustButtons.forEach(button => {
            button.addEventListener('click', function() {
                const criteria = this.getAttribute('data-criteria');
                const delta = parseFloat(this.getAttribute('data-delta'));
                const input = document.getElementById(`weight_${criteria}`);
                
                if (input) {
                    const newValue = Math.max(0, Math.min(1, parseFloat(input.value) + delta));
                    input.value = newValue.toFixed(3);
                    IdealistaApp.normalizeWeightsMCDM(input);
                    IdealistaApp.updateWeightVisualizations();
                }
            });
        });

        // Slider interactions
        const sliders = criteriaForm.querySelectorAll('input[type="range"]');
        sliders.forEach(slider => {
            slider.addEventListener('input', function() {
                const criteria = this.getAttribute('data-criteria');
                const weightInput = document.getElementById(`weight_${criteria}`);
                if (weightInput) {
                    weightInput.value = (parseFloat(this.value) / 100).toFixed(3);
                    IdealistaApp.normalizeWeightsMCDM(weightInput);
                    IdealistaApp.updateWeightVisualizations();
                }
            });
        });

        // Initial normalization
        IdealistaApp.normalizeWeightsMCDM();
        IdealistaApp.updateWeightVisualizations();

        // Form validation
        criteriaForm.addEventListener('submit', function(e) {
            if (!IdealistaApp.validateCriteriaForm()) {
                e.preventDefault();
                return false;
            }
            
            // Confirmation dialog
            if (!confirm('This will update scoring weights using MCDM methodology and rescore all properties. This may take a few moments. Continue?')) {
                e.preventDefault();
                return false;
            }
            
            // Show loading state
            const submitButton = this.querySelector('button[type="submit"]');
            if (submitButton) {
                // Store original content safely
                const originalIcon = submitButton.querySelector('i');
                const originalIconClasses = originalIcon ? originalIcon.className : '';
                const originalTextContent = submitButton.textContent.trim();
                
                // Clear button content safely
                submitButton.textContent = '';
                
                // Create loading state with safe DOM methods
                const loadingIcon = document.createElement('i');
                loadingIcon.className = 'fas fa-spinner fa-spin me-2';
                const loadingText = document.createTextNode('Updating...');
                
                submitButton.appendChild(loadingIcon);
                submitButton.appendChild(loadingText);
                submitButton.disabled = true;
                
                // Re-enable after timeout (fallback)
                setTimeout(() => {
                    // Restore original content safely
                    submitButton.textContent = '';
                    
                    if (originalIconClasses) {
                        const restoredIcon = document.createElement('i');
                        restoredIcon.className = originalIconClasses;
                        submitButton.appendChild(restoredIcon);
                    }
                    
                    const restoredText = document.createTextNode(originalTextContent);
                    submitButton.appendChild(restoredText);
                    submitButton.disabled = false;
                }, 30000);
            }
        });
    },

    normalizeWeightsMCDM: function(changedInput = null) {
        const inputs = document.querySelectorAll('#criteria-form input[type="number"]');
        const weights = [];
        let changedIndex = -1;
        
        // Collect all weights and find changed input index
        inputs.forEach((input, index) => {
            const weight = Math.max(0, parseFloat(input.value) || 0);
            weights.push(weight);
            if (changedInput && input === changedInput) {
                changedIndex = index;
            }
        });
        
        // Calculate current total
        const currentTotal = weights.reduce((sum, w) => sum + w, 0);
        
        // If total is 0, set equal weights
        if (currentTotal === 0) {
            const equalWeight = (1 / weights.length).toFixed(3);
            inputs.forEach(input => {
                input.value = equalWeight;
            });
            return;
        }
        
        // MCDM normalization: adjust other weights proportionally
        if (changedIndex !== -1 && currentTotal !== 1) {
            const changedWeight = weights[changedIndex];
            const othersTotal = currentTotal - changedWeight;
            const remainingWeight = Math.max(0, 1 - changedWeight);
            
            // Adjust other weights proportionally
            inputs.forEach((input, index) => {
                if (index !== changedIndex && othersTotal > 0) {
                    const originalWeight = weights[index];
                    const newWeight = (originalWeight / othersTotal) * remainingWeight;
                    input.value = Math.max(0, newWeight).toFixed(3);
                } else if (index === changedIndex) {
                    // Ensure changed weight doesn't exceed 1
                    input.value = Math.min(1, changedWeight).toFixed(3);
                }
            });
        } else if (currentTotal !== 1) {
            // Normalize all weights to sum to 1
            inputs.forEach((input, index) => {
                const normalizedWeight = weights[index] / currentTotal;
                input.value = normalizedWeight.toFixed(3);
            });
        }
        
        // Update sliders to match normalized weights
        inputs.forEach(input => {
            const criteriaName = input.name.replace('weight_', '');
            const slider = document.getElementById(`slider_${criteriaName}`);
            if (slider) {
                slider.value = (parseFloat(input.value) * 100).toFixed(0);
            }
        });
    },

    updateWeightVisualizations: function() {
        const inputs = document.querySelectorAll('#criteria-form input[type="number"]');
        let totalWeight = 0;
        
        inputs.forEach(input => {
            const criteriaName = input.name.replace('weight_', '');
            const weight = parseFloat(input.value) || 0;
            const progressBar = document.getElementById(`progress_${criteriaName}`);
            const percentSpan = document.getElementById(`percent_${criteriaName}`);
            
            totalWeight += weight;
            
            if (progressBar) {
                // Update progress bar (show actual percentage of total)
                const percentage = weight * 100;
                progressBar.style.width = `${percentage}%`;
                
                // Color coding based on MCDM standards
                progressBar.className = 'progress-bar';
                if (weight >= 0.2) {  // High importance (≥20%)
                    progressBar.classList.add('bg-success');
                } else if (weight >= 0.1) {  // Medium importance (10-19%)
                    progressBar.classList.add('bg-info');
                } else if (weight >= 0.05) {  // Low importance (5-9%)
                    progressBar.classList.add('bg-warning');
                } else {  // Very low importance (<5%)
                    progressBar.classList.add('bg-secondary');
                }
            }
            
            if (percentSpan) {
                percentSpan.textContent = `${(weight * 100).toFixed(1)}%`;
            }
        });
        
        // Update total weight display (should always be 1.0 for MCDM)
        const totalWeightSpan = document.getElementById('total-weight');
        if (totalWeightSpan) {
            totalWeightSpan.textContent = totalWeight.toFixed(3);
            
            // Color coding for total (MCDM requires exactly 1.0)
            totalWeightSpan.className = 'fw-bold';
            const difference = Math.abs(totalWeight - 1.0);
            if (difference < 0.001) {  // Within tolerance
                totalWeightSpan.classList.add('text-success');
            } else if (difference < 0.01) {  // Minor deviation
                totalWeightSpan.classList.add('text-warning');
            } else {  // Significant deviation
                totalWeightSpan.classList.add('text-danger');
            }
        }
    },

    validateCriteriaForm: function() {
        const inputs = document.querySelectorAll('#criteria-form input[type="number"]');
        let totalWeight = 0;
        let hasError = false;
        
        inputs.forEach(input => {
            const weight = parseFloat(input.value) || 0;
            totalWeight += weight;
            
            // Individual validation (MCDM standards)
            if (weight < 0) {
                IdealistaApp.showNotification('Weights cannot be negative (MCDM requirement)', 'error');
                input.focus();
                hasError = true;
                return;
            }
            
            if (weight > 1) {
                IdealistaApp.showNotification('Individual weights cannot exceed 1.0 (MCDM requirement)', 'error');
                input.focus();
                hasError = true;
                return;
            }
        });
        
        if (hasError) return false;
        
        // MCDM validation: weights must sum to 1.0
        const difference = Math.abs(totalWeight - 1.0);
        if (difference > 0.001) {
            IdealistaApp.showNotification(`MCDM validation failed: Total weights must equal 1.0 (current: ${totalWeight.toFixed(3)})`, 'error');
            return false;
        }
        
        return true;
    },

    updateLastSync: function() {
        // Fetch and update last sync time
        fetch('/api/stats')
            .then(response => response.json())
            .then(data => {
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
            })
            .catch(error => {
                console.warn('Failed to update last sync time:', error);
                const lastSyncElement = document.getElementById('last-sync');
                if (lastSyncElement) {
                    lastSyncElement.textContent = 'Error loading';
                }
            });
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
}

// Initialize app when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    window.IdealistaApp.init();
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
