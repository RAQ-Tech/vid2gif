# vid2gif Design System

## Product Character

vid2gif is a trusted-LAN operations tool for creating GIF previews and maintaining large video libraries. It should feel like dependable professional software: quiet, precise, efficient, and transparent about what it will do to the user's files.

The interface is an application, not a marketing site. Prioritize scanability, comparison, repeated action, and confidence over spectacle. The first screen of every area must be the real working interface.

### Reference Blend

Use these references as visual direction, not as templates to copy:

- **65% [Retool](https://getdesign.md/design-md/retool):** application shell, dense workbenches, forms, tables, filters, toolbars, and action hierarchy.
- **25% [Steep](https://getdesign.md/design-md/steep):** dashboard metric hierarchy, restrained charts, generous spacing around important summaries, and quiet data visualization.
- **10% [Rootly](https://getdesign.md/design-md/rootly):** operational progress, severity, timelines, remediation states, and the transition from detection to resolution.

Do not import the references' landing-page composition, large promotional headlines, decorative gradients, or illustration-heavy sections. vid2gif should borrow their product-interface language only.

## Design Principles

1. **Work first.** Put the active workflow, results, or media comparison above documentation and settings.
2. **Dense, not cramped.** Favor compact controls and tables, but preserve clear grouping and 12-24px section spacing.
3. **State is explicit.** Scanning, queued, running, stale, blocked, failed, complete, and skipped must never rely on color alone.
4. **Destructive work is deliberate.** Separate scan, review, and apply. Show exact file and group counts before mutation.
5. **Media remains inspectable.** Actual GIFs, posters, previews, and source paths take priority over decorative imagery.
6. **Stable under change.** Loading text, long paths, badges, and live counters must not resize controls or shift the surrounding layout.
7. **Light and dark are peers.** Both themes must preserve hierarchy, contrast, and semantic meaning.

## Foundation

### Technology

- Continue using Bootstrap 5.3, Bootstrap Icons, Inter, and the shared `app/static/app.css` stylesheet.
- Prefer Bootstrap variables and semantic utilities before adding isolated literal colors.
- Use Bootstrap Icons for familiar actions. Do not add manually drawn SVG icons when a suitable icon exists.
- Keep the shared maximum workspace width at `1480px`.

### Typography

- Primary family: `Inter`, followed by the existing system sans-serif stack.
- Page title: `1.75rem`, weight 600, line height 1.2.
- Section title: `1.125rem`, weight 600.
- Panel/card title: `0.875-1rem`, weight 600.
- Body: `0.925rem` for work surfaces; `1rem` only for relaxed explanatory copy.
- Secondary text: `0.75-0.875rem`.
- Metric values: `1.5-2rem`; reserve larger values for the dashboard's primary lifetime metric.
- Paths, IDs, commands, and logs use the existing monospace stack.
- Letter spacing is `0`. Uppercase is limited to short metric labels and table headers.
- Never scale font size with viewport width.

### Color

Use a neutral surface system with semantic accents. No single hue should dominate the application.

| Role | Light | Dark | Use |
| --- | --- | --- | --- |
| App background | `#f8f9fa` | `#151a1f` | Page canvas |
| Primary surface | `#ffffff` | `#1d2329` | Panels and tools |
| Secondary surface | `#f1f3f5` | `#252c33` | Table heads, inset controls, grouped rows |
| Border | `#dee2e6` | `#39434c` | Dividers and boundaries |
| Primary text | `#212529` | `#f5f7f9` | Titles and primary values |
| Secondary text | `#6c757d` | `#aebbc6` | Supporting copy and metadata |
| Primary action | `#0d6efd` | `#4c8dff` | Selected state and main command |
| Success | `#198754` | `#67d9a5` | Complete, healthy, installed |
| Warning | `#d39e00` | `#f3c969` | Stale, review, skipped, attention |
| Danger | `#dc3545` | `#ff7b86` | Failed, delete, unsafe |
| Information | `#0aa2c0` | `#58c8dc` | Active scan detail and neutral information |

Category accents may distinguish dashboard streams: duplicates blue, video previews orange, subtitles cyan, posters green, and actor images purple. Purple is a category marker only, never a dominant page palette.

### Spacing and Geometry

- Spacing scale: `4, 6, 8, 12, 16, 24, 32px`.
- Desktop page padding: `24px`; mobile page padding: `12px`.
- Standard grid gap: `12-16px`; dense control gap: `6-8px`.
- Panel padding: `16-18px`; compact row padding: `8-12px`.
- Cards and panels: maximum `8px` radius.
- Compact controls and selected tabs: `4-6px` radius.
- Pills are reserved for statuses, small filters, and metadata. Commands do not use pill shapes.
- Avoid shadows on ordinary panels. Use a restrained shadow only for modals, active drag objects, menus, or a single dashboard emphasis band.

## Layout System

### Application Shell

- Keep a compact top navigation with the `vid2gif` wordmark as the first viewport signal.
- Global navigation contains Dashboard, GIFs, Library Maintenance, Settings, and System.
- The theme toggle is an icon-only button with an accessible label and tooltip.
- Page headings use a left-aligned title and short supporting sentence, with page-level commands aligned right.
- Do not place page headings inside cards.

### Work Surfaces

- Use full-width unframed page sections with constrained inner content.
- Use panels only for genuine tools, repeated entities, or clearly bounded result regions.
- Never place a decorative card inside another card. Inside a tool panel, use dividers, bands, or unframed subsections.
- Status strips use stable grid tracks and stay above the workflow they summarize.
- Prefer a split workbench when users must configure and inspect simultaneously. The configuration rail is narrower; the primary result or media area gets the flexible track.
- Sticky regions are appropriate for comparison players and review summaries when they do not hide content.

### Responsive Behavior

- Desktop: use multi-column workbenches and compact tables.
- Tablet: reduce supporting columns before reducing control clarity.
- Mobile: stack the active workflow in task order, keep primary actions visible, and let tables scroll horizontally inside their own bounded region.
- Never create page-level horizontal overflow.
- Fixed-format media uses explicit aspect ratios. Toolbars and tile headers use stable minimum heights.
- Text wraps before it overlaps; long paths and names truncate with a title or equivalent full-value access.

## Component Language

### Panels and Bands

- `workspace-panel` is the default tool boundary: one-pixel border, body background, 8px radius, and no default shadow.
- Use a metric or status band for a short summary plus progress, not a row of decorative cards.
- The dashboard's lifetime-impact band may remain the strongest visual surface, but should use a neutral dark base with green/cyan operational accents rather than a gradient.
- A colored top or left border may identify an active tool or severity. Do not combine several accent treatments on one panel.

### Buttons and Commands

- One primary action per local workflow.
- Use icon plus text for clear commands such as Scan All, Generate, Review Plan, Apply, or Save.
- Use icon-only buttons for familiar compact actions such as refresh, open, download, remove, reorder, retry, and theme toggle. Provide tooltips and accessible labels.
- Secondary commands use outline styling. Tertiary commands use quiet text or icon treatment.
- Permanent deletion is danger-styled. Quarantine is the default safer operation and should not look equally destructive.
- Disabled buttons must remain legible and should be accompanied by nearby state text when the reason is not obvious.

### Forms and Toolbars

- Labels sit above controls except in compact filter bars where label-control pairs may stay inline.
- Use selects for bounded options, segmented controls for modes, switches or checkboxes for binary settings, and numeric inputs/sliders for quantities.
- Keep path selection and folder browsing together as one control group.
- Toolbars follow workflow order: scope/filter, selection, operation, then review/apply.
- Advanced and automation settings belong below the primary workflow or on Settings, not before results.

### Tabs

- Use tabs for peer views or variants, not for sequential steps.
- Active tabs use clear text contrast plus a border or inset accent; color alone is insufficient.
- Keep tab labels short and include icons only when they improve scanning.
- On mobile, tabs may scroll horizontally but must not shrink text below the type scale.

### Tables and Result Lists

- Tables are the primary representation for files, paths, statuses, sizes, and actions.
- Headers are sticky inside bounded result regions where practical.
- Table headers use compact uppercase labels; row content remains normal case.
- Align numbers consistently and keep action columns narrow.
- Use checkboxes for selection and an explicit selection summary near the review action.
- Pagination is page-scoped. Selection and destructive plans must never silently include offscreen items.
- Expanded rows reveal detail inline without creating nested cards.
- Empty tables explain the state and present the next valid command when one exists.

### Status, Progress, and Freshness

- Every long operation shows a state label, progress label, percentage, and progress bar.
- Use these state families consistently:
  - Neutral: not scanned, idle, unknown.
  - Active/info: queued, scanning, decoding, checking.
  - Success: complete, healthy, installed, unchanged.
  - Warning: stale, skipped, needs review, configuration required.
  - Danger: failed, unsafe, blocked, destructive.
- Status pills are compact and text-labeled. Pair them with icons in dense operational lists.
- Freshness uses `checking`, `unchanged`, `changed`, and `unknown`. Changed results remain visible but action creation is disabled until rescan.
- Progress updates should change content without rebuilding stable controls or shifting layout.

### Charts and Metrics

- Follow Steep's restrained analytics language: thin axes, direct labels, sparse gridlines, and one accent per series.
- Prefer bars, trends, progress tracks, and ranked lists over decorative gauges.
- Always show the numeric value alongside a chart.
- Use color semantically and provide text/tooltips so charts remain understandable without color.
- Avoid 3D charts, gradients, glowing effects, and oversized empty visualizations.

### Modals and Confirmation

- Modals are for review/confirmation, focused editing, or irreversible decisions.
- Review dialogs show operation, visible scope, exact group/file count, estimated size, and exceptions.
- Permanent delete requires stronger copy and confirmation than quarantine.
- Do not hide validation errors behind a closed modal or replace detailed errors with generic toasts.

### Empty, Loading, and Error States

- Loading states preserve the final component's dimensions.
- Use concise operational language: `Scanning subtitles`, `Decoding GIF`, `Checking freshness`.
- Empty states are quiet and task-oriented. Avoid illustrations in tool surfaces.
- Errors state what failed, what remained unchanged, and the available recovery command.
- Do not silently fall back to behavior with weaker fidelity or safety.

## Page Patterns

### Dashboard: Steep-Led

- Lead with current health and lifetime impact, followed immediately by the maintenance scanner.
- Use a small number of high-value metrics rather than equal-weight cards for every count.
- Workstream rows/cards show state, result count, freshness, Scan/Rescan, and Open Results.
- Charts support decisions: backlog mix, resolution trend, and completed maintenance. They are secondary to actions.
- Keep the dashboard information-rich without turning it into a marketing achievement page.

### Library Maintenance: Retool-Led

- Each tab is a complete scan-review-apply workbench.
- Keep source selection, scan controls, current status, filters, result table, and page-scoped action controls in predictable order.
- Detection and mutation are separate. Scan buttons never perform cleanup.
- Place automation settings below manual analysis and apply workflows.
- Use Rootly-style severity and progress treatment for failures, warnings, stale results, and remediation logs.

### Test Lab: Retool Workbench with Media Priority

- Preserve the split workbench: compact configuration on the left, synchronized comparison player and saved tray on the right.
- The player is the dominant surface and uses black only as the media canvas background.
- Variant tabs remain compact and summarize settings without expanding every form.
- Playback controls use familiar icons and stable dimensions.
- Comparison tiles prioritize the rendered GIF; metadata and original Open/Download access remain available but visually secondary.
- Drag handles are explicit. Dragging, loading, and decoding states must not resize tiles.

### GIF Jobs

- Treat job creation, queue, completed output, and logs as operational views.
- Queue rows emphasize source, state, progress, elapsed time, and the next valid command.
- Logs use a stable monospace surface with restrained severity highlighting.
- Completed GIFs show actual output media and direct Open/Download actions.

### Settings and System

- Group related settings in unframed sections or sibling panels; do not nest cards.
- Separate safe preferences from credentials and destructive/system operations.
- Show effective values, persistence state, and connection-test results close to the relevant controls.
- System health uses direct labels and remediation guidance, not decorative scores.

## Interaction and Motion

- Interaction feedback is immediate and restrained.
- Standard transition duration: `120-180ms`; progress width may use up to `600ms`.
- Animate opacity, color, border, and small positional changes. Do not animate layout dimensions for live operational updates.
- Respect `prefers-reduced-motion`.
- Hover states must not move controls or change their dimensions.
- Keyboard focus is always visible. Drag-and-drop workflows require keyboard-equivalent placement and reordering.
- Suspend nonessential polling and animation when the document is hidden.

## Content Style

- Use direct task language: `Scan Posters`, `Review Selected`, `Generate 3 GIFs`, `Move to Quarantine`.
- Use sentence case for headings, labels, and buttons.
- State outcomes precisely: `3 files quarantined`, not `Cleanup successful`.
- Name the affected object and scope in destructive confirmations.
- Avoid promotional phrases, feature narration, and visible instructions that merely describe how the UI works.
- Use singular/plural wording correctly and format file sizes, durations, and timestamps consistently.

## Accessibility

- Target WCAG 2.1 AA contrast in both themes.
- All controls need programmatic labels; icon-only controls need accessible names and tooltips.
- Color is never the only status signal.
- Use semantic headings, landmarks, tables, tablists, progress bars, and live regions.
- Maintain at least a 32px compact control target on desktop and approximately 40px for primary mobile controls.
- Preserve logical focus order when panels stack or results refresh.
- Do not replace focused controls or repeatedly rewrite their DOM during polling.

## Do and Do Not

### Do

- Show the real tool or media result in the first viewport.
- Keep controls compact, aligned, and close to the content they affect.
- Use tables and structured lists for maintenance data.
- Use semantic color sparingly and consistently.
- Preserve originals, show exact scope, and explain skipped work.
- Verify desktop and mobile with real long paths and populated results.

### Do Not

- Do not create landing-page heroes inside the application.
- Do not use decorative gradients, glowing orbs, bokeh, or illustration-only empty states.
- Do not make the interface predominantly purple, dark blue, beige, or another single hue family.
- Do not make every section a floating card or put cards inside cards.
- Do not use oversized headings inside dashboards, sidebars, or compact tools.
- Do not hide primary actions in menus when there is sufficient toolbar space.
- Do not select or mutate offscreen maintenance results.
- Do not let live status updates restart media, replace focused controls, or shift layout.

## Implementation Checklist

Before merging user-facing UI work, verify:

- The first viewport contains the actual workflow, result, or media surface.
- There is one clear primary action per local workflow.
- Scan, review, and apply remain distinct where files may change.
- Light and dark themes both preserve hierarchy and contrast.
- Desktop and 375px mobile layouts have no incoherent overlap or page-level horizontal overflow.
- Long names, paths, badges, and live counters do not resize fixed-format controls.
- Empty, loading, stale, failed, and successful states are implemented.
- Keyboard focus, labels, tooltips, and non-color status signals are present.
- Tables and destructive actions remain explicitly page-scoped.
- Browser tests use populated real-world data, not only empty states.
