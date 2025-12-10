#!/usr/bin/env python3
"""
Test Google Maps API configuration and permissions
"""

import os
import requests
import json

def test_google_api_detailed():
    """Test Google API with detailed error reporting"""
    google_key = os.environ.get("Google_api")
    
    if not google_key:
        print("ERROR: No Google API key found")
        return
    
    print(f"Testing Google API key (length: {len(google_key)})")
    print(f"Key starts with: {google_key[:10]}...")
    
    # Test different Google APIs that we need
    apis_to_test = [
        {
            'name': 'Geocoding API',
            'url': 'https://maps.googleapis.com/maps/api/geocode/json',
            'params': {'address': 'Oviedo, Spain', 'key': google_key}
        },
        {
            'name': 'Places API (Nearby Search)',
            'url': 'https://maps.googleapis.com/maps/api/place/nearbysearch/json',
            'params': {
                'location': '43.3636546,-4.5727598',
                'radius': 5000,
                'type': 'restaurant',
                'key': google_key
            }
        },
        {
            'name': 'Distance Matrix API',
            'url': 'https://maps.googleapis.com/maps/api/distancematrix/json',
            'params': {
                'origins': '43.3636546,-4.5727598',
                'destinations': 'Oviedo, Spain',
                'mode': 'driving',
                'key': google_key
            }
        }
    ]
    
    for api in apis_to_test:
        print(f"\n=== Testing {api['name']} ===")
        try:
            response = requests.get(api['url'], params=api['params'])
            data = response.json()
            
            print(f"Status Code: {response.status_code}")
            print(f"Response Status: {data.get('status', 'NO_STATUS')}")
            
            if data.get('error_message'):
                print(f"Error Message: {data['error_message']}")
            
            if data.get('status') == 'REQUEST_DENIED':
                print("REQUEST_DENIED - This usually means:")
                print("1. API key doesn't have access to this specific API")
                print("2. API key has domain/IP restrictions")
                print("3. Billing is not enabled for this API")
                print("4. API is not enabled in Google Cloud Console")
            
            # Show some sample data if successful
            if data.get('status') == 'OK':
                if 'results' in data and data['results']:
                    print(f"SUCCESS: Got {len(data['results'])} results")
                elif 'rows' in data:
                    print(f"SUCCESS: Got distance matrix data")
                else:
                    print("SUCCESS: API responded OK")
            
        except Exception as e:
            print(f"ERROR: {str(e)}")

if __name__ == "__main__":
    test_google_api_detailed()