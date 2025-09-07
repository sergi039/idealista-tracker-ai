import os
import requests
import json

# Property coordinates from Cabueñes, Gijón
lat = 43.5219209
lon = -5.6065041

# Google Places API key
google_places_key = os.environ.get('GOOGLE_PLACES_API_KEY')

if not google_places_key:
    print("❌ Google Places API key not found in environment")
    print("The issue is that we need Google Places API to find airports")
else:
    # Search for airports near this location
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    
    # First, search within 5km (standard search)
    params = {
        'location': f"{lat},{lon}",
        'radius': 5000,
        'type': 'airport',
        'key': google_places_key
    }
    
    print(f"🔍 Searching for airports within 5km of {lat}, {lon}")
    response = requests.get(url, params=params)
    
    if response.status_code == 200:
        data = response.json()
        results = data.get('results', [])
        
        if results:
            print(f"\n✅ Found {len(results)} airport(s) within 5km:")
            for place in results:
                name = place.get('name')
                place_lat = place.get('geometry', {}).get('location', {}).get('lat')
                place_lon = place.get('geometry', {}).get('location', {}).get('lng')
                
                # Calculate distance manually
                from math import radians, cos, sin, asin, sqrt
                R = 6371000  # Earth radius in meters
                
                lat1, lon1, lat2, lon2 = map(radians, [lat, lon, place_lat, place_lon])
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
                c = 2 * asin(sqrt(a))
                distance = R * c
                
                print(f"  - {name}: {distance:.0f}m away")
                print(f"    Types: {', '.join(place.get('types', []))}")
        else:
            print("\n❌ No airports found within 5km")
    
    # Now search within 100km for real airports
    params['radius'] = 100000
    print(f"\n🔍 Searching for airports within 100km...")
    response = requests.get(url, params=params)
    
    if response.status_code == 200:
        data = response.json()
        results = data.get('results', [])
        
        if results:
            print(f"\n✅ Found {len(results)} airport(s) within 100km:")
            for place in results[:5]:  # Show first 5
                name = place.get('name')
                place_lat = place.get('geometry', {}).get('location', {}).get('lat')
                place_lon = place.get('geometry', {}).get('location', {}).get('lng')
                
                # Calculate distance
                from math import radians, cos, sin, asin, sqrt
                R = 6371000  # Earth radius in meters
                
                lat1, lon1, lat2, lon2 = map(radians, [lat, lon, place_lat, place_lon])
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
                c = 2 * asin(sqrt(a))
                distance = R * c
                
                print(f"  - {name}: {distance/1000:.1f}km away")
                print(f"    Types: {', '.join(place.get('types', []))}")

# Check the known Asturias Airport coordinates
print("\n📍 Known Asturias Airport (from Google Maps):")
print("  - Name: Aeropuerto de Asturias")
print("  - Distance: 48.2 km")
print("  - Travel time: 33 minutes by car")
print("\n⚠️  The issue is that Google Places API found something else as 'airport' 307m away")
print("This is likely a helipad, private airfield, or incorrect POI")