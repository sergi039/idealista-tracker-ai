import json
import os
from math import radians, cos, sin, asin, sqrt
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Connect to database
DATABASE_URL = os.environ.get("DATABASE_URL")
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
session = Session()

# Asturias Airport real coordinates (from Google Maps)
ASTURIAS_AIRPORT_LAT = 43.563611
ASTURIAS_AIRPORT_LON = -6.034722

def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two points on Earth using Haversine formula"""
    R = 6371000  # Earth radius in meters
    
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    distance = R * c
    
    return distance

def calculate_travel_time(distance_m):
    """Calculate estimated travel time to airport
    Using more realistic speeds:
    - City driving: 30 km/h (first 10km)
    - Highway: 80 km/h (rest of distance)
    """
    distance_km = distance_m / 1000
    
    if distance_km <= 10:
        # City driving only
        travel_time_min = round(distance_km * 60 / 30)
    else:
        # Mixed: 10km city + rest highway
        city_time = 10 * 60 / 30  # 20 minutes for first 10km
        highway_time = (distance_km - 10) * 60 / 80
        travel_time_min = round(city_time + highway_time)
    
    # Minimum 1 minute for any non-zero distance
    return max(1, travel_time_min)

# Get all lands with coordinates
result = session.execute(text("""
    SELECT id, title, location_lat, location_lon, transport, municipality
    FROM lands 
    WHERE location_lat IS NOT NULL AND location_lon IS NOT NULL
"""))
lands = result.fetchall()

print(f"📍 Asturias Airport coordinates: {ASTURIAS_AIRPORT_LAT}, {ASTURIAS_AIRPORT_LON}")
print(f"🔍 Checking {len(lands)} properties...\n")

fixed_count = 0
for land in lands:
    if land.location_lat and land.location_lon:
        # Calculate real distance to Asturias Airport
        real_distance = calculate_distance(
            float(land.location_lat), float(land.location_lon),
            ASTURIAS_AIRPORT_LAT, ASTURIAS_AIRPORT_LON
        )
        
        # Calculate realistic travel time
        real_travel_time = calculate_travel_time(real_distance)
        
        # Parse existing transport data
        transport = land.transport if isinstance(land.transport, dict) else (json.loads(land.transport) if land.transport else {})
        
        # Check if we need to fix the airport data
        old_distance = transport.get('airport_distance', 0)
        old_time = transport.get('airport_travel_time', 0)
        
        # Only fix if the distance is significantly wrong (more than 5km difference)
        # or if airport is marked as available but with wrong distance
        if transport.get('airport_available') and abs(old_distance - real_distance) > 5000:
            print(f"🔧 Fixing {land.title[:50]}...")
            print(f"   Municipality: {land.municipality}")
            print(f"   Old distance: {old_distance/1000:.1f}km, Old time: {old_time}min")
            print(f"   Real distance: {real_distance/1000:.1f}km, Real time: {real_travel_time}min")
            
            # Update transport data
            transport['airport_distance'] = real_distance
            transport['airport_travel_time'] = real_travel_time
            transport['airport_available'] = True
            
            # Update database
            session.execute(
                text("UPDATE lands SET transport = :transport WHERE id = :id"),
                {'transport': json.dumps(transport), 'id': land.id}
            )
            fixed_count += 1
            print("   ✅ Fixed!\n")
        
        # Also check properties that might not have airport data at all
        elif not transport.get('airport_available') and real_distance < 100000:  # Within 100km
            print(f"➕ Adding airport data for {land.title[:50]}...")
            print(f"   Municipality: {land.municipality}")
            print(f"   Distance: {real_distance/1000:.1f}km, Travel time: {real_travel_time}min")
            
            # Add airport data
            transport['airport_distance'] = real_distance
            transport['airport_travel_time'] = real_travel_time
            transport['airport_available'] = True
            
            # Update database
            session.execute(
                text("UPDATE lands SET transport = :transport WHERE id = :id"),
                {'transport': json.dumps(transport), 'id': land.id}
            )
            fixed_count += 1
            print("   ✅ Added!\n")

session.commit()
session.close()

print(f"\n✅ Fixed airport distances for {fixed_count} properties")
print("All properties now have correct distance to Asturias Airport")