"""Record a public-index observation for a previously matched property.

Invoke only after reading the actual public card and matching it to the house.
The command does not contact either portal or update primary listing status.
"""

import argparse
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("property_id", type=int)
    parser.add_argument("primary_url")
    parser.add_argument("source_url")
    parser.add_argument("observed_at", help="ISO timestamp of the public-index read")
    args = parser.parse_args()

    from app import create_app, db
    from models import Property
    from services.alternate_publication import record_publication

    with create_app().app_context():
        prop = db.session.get(Property, args.property_id)
        if prop is None:
            parser.error("Property not found")
        try:
            changed = record_publication(
                prop,
                args.primary_url,
                args.source_url,
                args.observed_at,
                datetime.now(timezone.utc),
            )
        except (ValueError, OverflowError) as exc:
            parser.error(str(exc))
    print(
        f"Property {args.property_id}: {'recorded' if changed else 'already recorded'}"
    )


if __name__ == "__main__":
    main()
