# IdealistaRank interface contract

Approved by the owner on 2026-11-04 for implementation and publication. This
contract replaces the old horizontal navigation, subscription chip strip and
always-expanded filter wall. Product rules under `docs/rules/` still own data,
selection, costs and truthfulness.

## Foundation

Keep Bootstrap **5.3.2**, Flask/Jinja and the existing Font Awesome 6 family.
Do not install another CSS framework or icon library. Tabler is an open-source
reference for composition, not a runtime dependency. A framework replacement
needs a separate compatibility decision across every page.

Load existing styles, `static/css/design-tokens.css`, then
`static/css/workspace.css`. The token file owns the shared light/dark palette,
spacing, component radii and control height. New shared workspace rules use
those values; justified geometry such as responsive breakpoints and image
dimensions may have explicit dimensions. Do not add page-specific copies of
buttons, navigation or status styles.

The read-only `/design-system` page renders shared examples using the actual
base template and CSS. It performs no writes or background work. Add component
states there when extending the shared vocabulary.

## Components

`templates/components/ui.html` owns:

- `icon(name)`: one Font Awesome family and size; decorative, `aria-hidden`.
- `nav_item(endpoint, label, symbol, active)`: visible translated text and one
  active location. Endpoint ownership remains with Flask.
- `filter_chip(label, value, href)`: a readable condition and accessible removal
  link. Its caller supplies the complete canonical query state.
- `filter_value(key, value, choices)`: existing translated option meanings and
  explicit distance units; never expose query sentinels or invent walking times.
- `state_label(label, tone)`: neutral/success/warning/danger only; never infer
  meaning from color alone.

Native Bootstrap buttons use primary for the local main action, neutral
outline for secondary actions, and danger only for a meaningful destructive or
high-impact action. Ordinary controls are at least 40px, primary touch targets
44px. Familiar icon-only actions (favorite, close, theme) need accessible names;
unfamiliar modes require visible text. Tooltip-only discovery is insufficient.

Body text is 14–16px, supplementary text at least 12px. Table headers may be
11px but never carry the only description of an action. Numeric comparisons
use tabular numerals. No emoji as system icons, arbitrary accent colors or
inline `!important` width fixes. Dark mode uses explicit foreground/background
pairs, not a mechanical inversion.

Default link colors use low specificity so Bootstrap button/badge/alert
foregrounds keep their component semantics. The fixed desktop sidebar must
scroll independently in short windows and at enlarged text sizes.

## Navigation and width

At 1200px and wider, show the labelled sidebar; it can be completely collapsed
and restored with **Menu**. There is no icon-only rail. Its preference may live
in localStorage because it is presentation, not listing state. Below 1200px,
use Bootstrap's labelled offcanvas menu with keyboard dismissal and focus
return. The main area uses **all remaining width**, without a maximum cap.
Entering the desktop layout must dismiss any mobile offcanvas through Bootstrap
so no backdrop, focus trap or body scroll lock survives the transition.

Properties is the one listing entry. Map is a representation control carrying
the same selection, not a duplicate global listing section. Manage subscriptions
means configuration; selecting subscriptions in the catalog means scope. Do not
rename either to Source: source already means the listing portal.

## Catalog controls

Each control has one home:

| Zone | Responsibility |
|---|---|
| Subscription toolbar | Scope multiselect, archived/unassigned access, Favorites, Hide removed; rare subscription tools disclose on demand |
| Filter form | Search/type/municipality, additional filters, explicit Apply and Clear |
| Result controls | All properties/recommendations, named assessment modes, sort/order, table/cards/map and collection actions |

All GET filter edits are drafts until **Apply** or Enter submits the form.
Subscription menu checkboxes use the same form through `form=filter-form`;
closing the menu must not submit unrelated drafts. One-click scope/view/mode
links use the **applied** server state, not unsaved DOM values.
The closed subscription label always names that applied scope. A pending scope
change stays visibly disclosed outside the menu until Apply or reversal of the
draft; an explicitly empty selection and All remain distinct.
Reversing checkbox edits restores the original fallback, including an empty
scope. Choosing the All button is an explicit widening action.

Every applied additional condition stays visible outside the closed panel as
a removable chip. Generate removal URLs from `base_args`, retaining `mode`,
`view_type`, remaining filters, sort/order and page reset. The signed session
remembers whole explicit choices; it does not restore an omitted individual
field. Global Clear uses the existing `remember=forget` contract. Never add a
second listing-state store in localStorage.

The initial view remains table/date. An explicit recommendation entry selects
recommendation ordering and removes Favorites as before; disclose that before
the action while Favorites is active. All other filters remain. Assessments
(overall/investment/lifestyle/taste), favorites, ranking and representation are
different axes. Always name the selected assessment and retain access to old
scores. No invented match probability.

## Rows and evidence

Four groups: object/photo/municipality and important facts; price/area; reason
or existing assessment; owner decision and next action. Keep the real decision
and action independent. No new persistence entity is introduced by layout.

Secondary measurements, coordinates, source links, dates and investment
information remain in row details and the property page. Significant conflicts,
approximate location and unknown measurements cannot disappear to meet a height
budget. A brief reason may wrap; its full evidence is keyboard-accessible. A
coordinate with unknown accuracy must say unknown, not approximate. A
missing photograph is not proof that no photograph exists. A rejected/favorite
row remains governed by the existing selection service, never by CSS or JS.

At tablet widths, prioritize object/price/reason/owner action; additional facts
remain in expandable details. On phones the same table rows stack into readable
summaries. Do not silently change the query's representation preference or
shrink text to fit all historical columns.

## Recommendation states

Read the existing service's state and profile version. Operation state and
available evidence are independent; a failed refresh can still have old data.

| State | Presentation |
|---|---|
| current | Current profile and its actual basis |
| dirty | New feedback; previous evidence only if a profile exists |
| pending | Updating; no duplicate refresh control; prior evidence only if present |
| failed | Failed update; disclose whether previous evidence remains |
| none | Honest starting state, not an endless spinner or fabricated ranking |

Recommendations rank the filtered collection, not a new top-N shortlist.
Refresh recommendations and retrain taste remain separate existing POSTs with
CSRF and explicit confirmation. No paid operation on GET, tab, sort or view
change. Preserve `unknown` versus measured absence and stale versus current
across list, map, details and export.

## Verification and enforcement

The scoped design-contract check applies to the new workspace layer only:
palette values belong in tokens, and another icon/framework bundle cannot be
silently introduced. Legacy/vendor styles are not subjected to a blanket
pixel/hex ban. New exceptions need a documented missing scenario.

Verify a **built image**, repeated independent loads at 1440 and 834, and also
1200/1280 (expanded/collapsed navigation), 390 and a wide desktop. Test EN/ES,
both themes, long titles/reasons, 200% zoom/reflow, keyboard focus, empty/error
states and delayed samples. Use `elementFromPoint` for visible control centers;
static markup assertions cannot prove layout. There must be no sideways page
or tablet-table scrolling. Default closed panels target the first row by
360px at1440 and520px at834; active conditions/errors/large text may expand it.
Never remove evidence to satisfy the target.

Retain project checks for filter memory, map/list/CSV parity and every relevant
state. New regression tests require a real-hunk mutation result. The owner task
runs the full repository gate; independent reviewers do not repeat that suite.
Ship through the protected PR and existing deploy watcher with a verified head
and a real rollback. UI approval alone is not evidence of a working release.
