"""Cross-camera registration — issue #29: determining the geometric
relationship between two frames from *different* optical trains (e.g.
where the Main camera's field falls within the Guide camera's field),
kept deliberately separate from `astrotool_core.target.translation_offset`
(same-camera displacement, issue #27/#28) and from mount calibration.

See `result.CrossCameraRegistrationResult` for the common contract both
`terrestrial_registrar.TerrestrialRegistrar` and
`star_field_registrar.StarFieldRegistrar` return, and this package's own
sub-modules for the architecture split: `optical_prior` (persistent
sensor/plate-scale geometry), `geometry` (pure polygon/rotation math,
camera-count-independent), `alignment` (guide-scope adjustment guidance
derived from a result), `astap_adapter` (ASTAP subprocess boundary).
"""
