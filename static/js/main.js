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
                        if (target.innerHTML.startsWith('{')) {
                            target.innerHTML = '<i class="fas fa-sync-alt me-1"></i>Manual Sync';
                        }
                    } catch (e) {
                        IdealistaApp.showNotification('Sync completed successfully', 'success');
                        // Restore button content
                        if (target.innerHTML.startsWith('{')) {
                            target.innerHTML = '<i class="fas fa-sync-alt me-1"></i>Manual Sync';
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
                    window.location.href = `/lands/${landId}`;
                }
            });
            
            // Add keyboard navigation
            row.addEventListener('keypress', function(e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    const landId = this.getAttribute('data-land-id');
                    if (landId) {
                        window.location.href = `/lands/${landId}`;
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

        // Real-time weight visualization updates
        const inputs = criteriaForm.querySelectorAll('input[type="number"]');
        inputs.forEach(input => {
            input.addEventListener('input', function() {
                IdealistaApp.updateWeightVisualizations();
            });
        });

        // Form validation
        criteriaForm.addEventListener('submit', function(e) {
            if (!IdealistaApp.validateCriteriaForm()) {
                e.preventDefault();
                return false;
            }
            
            // Confirmation dialog
            if (!confirm('This will update scoring weights and rescore all properties. This may take a few moments. Continue?')) {
                e.preventDefault();
                return false;
            }
            
            // Show loading state
            const submitButton = this.querySelector('button[type="submit"]');
            if (submitButton) {
                const originalText = submitButton.innerHTML;
                submitButton.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Updating...';
                submitButton.disabled = true;
                
                // Re-enable after timeout (fallback)
                setTimeout(() => {
                    submitButton.innerHTML = originalText;
                    submitButton.disabled = false;
                }, 30000);
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
                // Update progress bar (normalize for visualization)
                const percentage = Math.min((weight / 2) * 100, 100);
                progressBar.style.width = `${percentage}%`;
                
                // Color coding
                progressBar.className = 'progress-bar';
                if (weight > 0.5) {
                    progressBar.classList.add('bg-success');
                } else if (weight > 0.2) {
                    progressBar.classList.add('bg-warning');
                } else {
                    progressBar.classList.add('bg-danger');
                }
            }
            
            if (percentSpan) {
                percentSpan.textContent = `${(weight * 10).toFixed(1)}%`;
            }
        });
        
        // Update total weight display
        const totalWeightSpan = document.getElementById('total-weight');
        if (totalWeightSpan) {
            totalWeightSpan.textContent = totalWeight.toFixed(2);
            
            // Color coding for total
            totalWeightSpan.className = 'fw-bold';
            if (totalWeight < 0.8 || totalWeight > 1.5) {
                totalWeightSpan.classList.add('text-warning');
            } else {
                totalWeightSpan.classList.add('text-success');
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
            
            // Individual validation
            if (weight < 0) {
                IdealistaApp.showNotification('Weights cannot be negative', 'error');
                input.focus();
                hasError = true;
                return;
            }
            
            if (weight > 10) {
                IdealistaApp.showNotification('Weights cannot exceed 10.0', 'error');
                input.focus();
                hasError = true;
                return;
            }
        });
        
        if (hasError) return false;
        
        // Total weight validation
        if (totalWeight === 0) {
            IdealistaApp.showNotification('Total weight cannot be zero. Please set at least one weight above 0.', 'error');
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
                                lastSyncElement.innerHTML = `${dateStr} at ${timeStr} (+${sync.new_properties} new)`;
                            } else {
                                lastSyncElement.innerHTML = `Sync completed (+${sync.new_properties} new) - time unavailable`;
                            }
                        } else {
                            lastSyncElement.innerHTML = `Sync completed (+${sync.new_properties} new) - time unavailable`;
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
        notification.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
        `;
        
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
    }
};

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
