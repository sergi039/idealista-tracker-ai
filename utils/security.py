"""
Security utilities for API key validation and security configuration
"""
import os
import logging
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

class SecurityValidator:
    """Centralized security validation for the application"""
    
    # Required secrets for basic application functionality
    REQUIRED_SECRETS = {
        'SESSION_SECRET': 'Flask session security',
        'DATABASE_URL': 'Database connection'
    }
    
    # Optional secrets for enhanced functionality (warn if missing)
    OPTIONAL_SECRETS = {
        'Google_api': 'Google Maps/Places API services and geocoding',
        'GOOGLE_MAPS_API': 'Google Maps API services (alternative name)',
        'claude_key': 'Claude AI services (ANTHROPIC_API_KEY)',
        'IMAP_USER': 'Gmail IMAP access for email ingestion',
        'IMAP_PASSWORD': 'Gmail App Password for IMAP access'
    }
    
    @classmethod
    def validate_required_secrets(cls) -> Tuple[bool, List[str]]:
        """
        Validate that all required secrets are present
        Returns: (is_valid, missing_secrets)
        """
        missing_secrets = []
        
        for secret_key, description in cls.REQUIRED_SECRETS.items():
            value = os.environ.get(secret_key)
            if not value:
                missing_secrets.append(f"{secret_key} ({description})")
                logger.error(f"Missing required secret: {secret_key}")
        
        is_valid = len(missing_secrets) == 0
        return is_valid, missing_secrets
    
    @classmethod
    def check_optional_secrets(cls) -> Dict[str, bool]:
        """
        Check availability of optional secrets
        Returns: dict mapping secret name to availability
        """
        availability = {}
        
        for secret_key, description in cls.OPTIONAL_SECRETS.items():
            value = os.environ.get(secret_key)
            is_available = bool(value)
            availability[secret_key] = is_available
            
            if not is_available:
                logger.warning(f"Optional secret missing: {secret_key} ({description})")
            else:
                logger.info(f"Optional secret available: {secret_key}")
        
        return availability
    
    @classmethod
    def validate_all_secrets(cls, raise_on_missing_required: bool = True) -> Dict:
        """
        Comprehensive secret validation
        Args:
            raise_on_missing_required: Raise ValueError if required secrets missing
        Returns:
            dict with validation results
        """
        logger.info("Starting security validation...")
        
        # Check required secrets
        is_valid, missing_required = cls.validate_required_secrets()
        
        if not is_valid and raise_on_missing_required:
            error_msg = (
                f"Missing required secrets in Replit Secrets: {', '.join(missing_required)}. "
                f"Please add these in the Secrets tab."
            )
            raise ValueError(error_msg)
        
        # Check optional secrets
        optional_availability = cls.check_optional_secrets()
        
        # Security audit for exposed secrets
        cls._audit_for_exposed_secrets()
        
        results = {
            'required_valid': is_valid,
            'missing_required': missing_required,
            'optional_availability': optional_availability,
            'total_required': len(cls.REQUIRED_SECRETS),
            'total_optional': len(cls.OPTIONAL_SECRETS),
            'optional_available_count': sum(optional_availability.values())
        }
        
        logger.info(f"Security validation completed. Required: {is_valid}, "
                   f"Optional available: {results['optional_available_count']}/{results['total_optional']}")
        
        return results
    
    @classmethod
    def _audit_for_exposed_secrets(cls) -> None:
        """
        Audit for potential secret exposure risks
        """
        import glob
        import re
        
        # Pattern to detect API key exposure risks
        dangerous_patterns = [
            r'print.*[gG]oogle.*[kK]ey',
            r'print.*API.*key',
            r'print.*[sS]ecret',
            r'console\.log.*[kK]ey',
            r'logger.*[kK]ey.*=',
            r'AIza[0-9A-Za-z-_]{35}',  # Google API key pattern
        ]
        
        # Files to scan for potential exposure
        scan_patterns = [
            '*.py',
            '*.js', 
            '*.html',
            'test_*.py',
            '*test*.py'
        ]
        
        warnings = []
        
        for pattern in scan_patterns:
            for filepath in glob.glob(pattern, recursive=True):
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        
                    for line_num, line in enumerate(content.split('\n'), 1):
                        for dangerous_pattern in dangerous_patterns:
                            if re.search(dangerous_pattern, line, re.IGNORECASE):
                                warnings.append(f"SECURITY WARNING: Potential secret exposure in {filepath}:{line_num}")
                                logger.warning(f"Potential secret exposure in {filepath}:{line_num}: {line.strip()}")
                
                except Exception:
                    continue
        
        if warnings:
            logger.warning(f"Found {len(warnings)} potential security risks")
        else:
            logger.info("Security audit: No obvious secret exposure patterns found")