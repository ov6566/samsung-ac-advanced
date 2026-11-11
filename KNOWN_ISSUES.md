# Known issues and limitations — Samsung AC Advanced 1.0.0

There are currently no known release-blocking defects in the tested 1.0.0 feature set.

The following limitations are known and should be considered part of the current compatibility scope.

## Model compatibility

Samsung AC Advanced was developed and tested primarily with a Samsung Room A/C reporting the SmartThings model identifier `ARTIK051_PRAC_20K`.

Other Samsung models may expose different SmartThings capabilities, accept different values or normalize settings differently. The developer does not have access to other models for validation, so compatibility beyond the tested model depends on testing and feedback from the community.

## SmartThings authentication changes

Version 1.0.0 authenticates directly to the SmartThings REST API with a Personal Access Token (PAT).

Samsung has announced that affected legacy PATs expire on **December 15, 2026**. Any installation that depends on such a PAT may stop communicating with SmartThings after that date unless Samsung AC Advanced is migrated to a supported authentication method or the user has another supported API access path.

The long-term authentication approach is intentionally not changed in the 1.0.0 release candidate. It will be evaluated separately after release, including the implications of SmartThings API plans and OAuth for an open-source Home Assistant custom integration.

## SmartThings cloud dependency

The integration communicates through the SmartThings REST API and therefore depends on Internet connectivity, SmartThings cloud availability, API latency and SmartThings account/token access.

## Display-lighting feedback

Display-lighting commands are exposed as stateless On and Off buttons because reliable readable state feedback is not available through the tested SmartThings capability.

## Oscillation capability metadata

The original test model can report broader oscillation metadata than the hardware physically supports. Samsung AC Advanced intentionally exposes only the physically validated oscillation choices for that model.

## Samsung state normalization

Samsung firmware may normalize requested setting combinations. When no integration controller owns an active transition, coordinator-confirmed Samsung state is accepted as authoritative rather than repeatedly forcing a requested value.
