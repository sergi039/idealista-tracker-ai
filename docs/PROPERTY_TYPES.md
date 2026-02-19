# Property Types (Idealista Sale) — Universal Build

The universal build is **sale-first** and uses a **configurable, regex-based classification** to map incoming Idealista alert emails into:

- `Property.property_category` (high-level bucket)
- `Property.property_subtype` (more specific)

Defaults live in `services/settings_service.py` (`DEFAULT_PROPERTY_CLASSIFICATION_RULES`) and can be edited:

- Globally: `Settings → Property settings → Global classification rules`
- Per search: `Profiles → Edit profile → Classification rules`

## Supported categories (defaults)

## Priority now (active alerts)

Right now we validate end-to-end behavior primarily for:

- `housing` (apartments + houses)
- `land` (plots)

Other categories are kept as defaults but should be validated against real email samples when alerts exist.

### `housing`

Subtypes (defaults):

- `apartment` — piso, apartamento, flat, apartment, estudio, loft
- `house` — casa, chalet, villa, adosado/pareado, bungalow
- `duplex` — dúplex/duplex
- `penthouse` — ático/atico, penthouse

### `land`

Subtype (default):

- `plot` — terreno, parcela, plot/land, suelo (contextual), solar (contextual), finca rústica (contextual)

### `garage`

Subtypes (defaults):

- `garage` — garaje, plaza de garaje, parking
- `storage` — trastero, storage

### `commercial`

Subtypes (defaults):

- `office` — oficina, office
- `industrial` — nave, warehouse, industrial, almacén/almacen
- `retail` — local comercial / local en venta, shop, retail

### `building`

Subtype (default):

- `building` — edificio, bloque, building

### `new_development`

Subtype (default):

- `obra_nueva` — obra nueva, promoción/promocion, new development

## Notes

- Idealista categories and email wording vary by language (Spanish/English) and by alert type (“New home…”, “Price reduction…”). The defaults are meant to be safe and easy to override.
- If you want strict separation between legacy land tracker and the universal build in a shared Gmail label setup, use `Excluded categories = land` in `Settings → Property settings`.
