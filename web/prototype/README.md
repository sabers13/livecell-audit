# Guided interface prototype

Open `index.html` in a browser with the sibling `assets` directory present. No installation or build is needed.

This prototype implements the recruiter-friendly design in `../../DESIGN.md` and `../../docs/04_react_ui_spec.md`. It is a reference for the eventual React frontend, not a completed audit engine.

Working interactions:

- Switch between a shared-image finding and a sparse-label example.
- Show or hide the actual supplied polygon for image ID 150535.
- Expand evidence and technical context.
- Download the saved snapshot counts and example records.

The images are derived display copies of original LIVECell TIFF files, converted to PNG. Original images and annotations are from Sartorius Corporate Research under CC BY-NC 4.0. The overlay uses the supplied polygon; its enlarged circle and pointer are explanatory aids.

Snapshot counts were independently checked on 17 October 2026. The audit package, automated tests, and derived data exports remain to be implemented. A human comprehension check remains to be performed.
