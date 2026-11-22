# Browser-Use 0.13.13 parity

This adapter follows Browser-Use's public event and session contracts while
replacing Chromium/CDP mechanics with native Camoufox Playwright operations.
Parity means equivalent observable behavior, not copied CDP implementation.

## Architecture

| Browser-Use responsibility | Camoufox owner | Status |
| --- | --- | --- |
| Public session API and shared event bus | `CamoufoxSession` | Native equivalent |
| Browser/context/tab lifecycle and stable target IDs | `CamoufoxSessionManager` | Native equivalent with Browser-Use `viewport`/`no_viewport` profile propagation, cross-process locking for persistent profiles, caller-executor isolation, and bounded driver shutdown |
| Domain-scoped sensitive input | `CamoufoxSessionManager.get_target` and upstream tool registry | Native target lookup reads the current Playwright URL; secret substitution remains scoped to the approved origin |
| Click, type, navigation, history, scrolling, keys, dropdowns, upload | `ActionWatchdog` | Native event owner; mutating input delegates to the serialized Firefox interaction boundary. Native selects and owned ARIA dropdowns support exact option enumeration/selection, clipped-menu scrolling, frame scoping, and committed-value verification. |
| Observe-act-verify-recover interaction transactions | `InteractionTransaction` plus `FirefoxNativeInput` | Native equivalent with one session action lock, bounded pre-input retries, at-most-once delivery after dispatch begins, trusted variable-cadence keys and viewport-safe curved pointer input, reacquisition, delayed input-commit polling, and asynchronous ARIA activation acknowledgement |
| DOM state and element lookup | `DOMWatchdog` plus `dom/` | Native equivalent with a nearest-ancestor observation tree, bounded cross-origin frame traversal, stable frame IDs, paint filtering of controls and occluded static text, recursive open shadow roots, resolved field help/error relationships, live input/checked state with password/file/hidden/payment/one-time-code values redacted, and image context for images inside controls |
| Browser state capture resilience | `DOMWatchdog` and `CamoufoxSession` | Native equivalent: screenshots are bounded by the 20s parallel-task budget and a failed screenshot returns DOM-only state; a timed-out capture returns `state_error` with every cached selector cleared; `pending_network_requests` comes from Playwright request events with Browser-Use's noise filters |
| Accessibility semantics | CDP `Accessibility.getFullAXTree` merged by backend node ID | Firefox-native Playwright ARIA snapshots collected per frame in parallel with DOM extraction and merged by frame-local geometry, with a DOM/ARIA fallback |
| Domain/IP navigation policy | `SecurityWatchdog` | Native route interception |
| Cookies and storage state | `StorageStateWatchdog` | Native equivalent |
| Downloads | `DownloadsWatchdog` | Native Playwright download events |
| Dialogs | `PopupsWatchdog` | Native Playwright dialog events |
| Screenshots and highlights | `ScreenshotWatchdog` | Native equivalent. With `highlight_elements` (upstream default `True`), the state screenshot shows the index at the top centre of each text-less interactive element, as the upstream agent prompt describes. Labels are drawn inside the element, shrink to fit and are translucent, with no outlines (upstream's `python_highlights` placed them over neighbouring content and hid small elements' state). 0.13 stopped drawing them for code-use mode (`97420238c`) while the prompt still promises them. Set `highlight_elements=False` for clean state screenshots. |
| Crash reporting and health checks | `CrashWatchdog` | Native Playwright signals plus page and context-level pings; repeated same-target failures on both channels are required so main-thread work is not reported as a browser crash |
| Durable failure bundles | `DiagnosticsWatchdog` | Opt-in JSON, screenshot, and sanitized LLM DOM artifacts |
| Permissions, HAR, and video context options | `CamoufoxSessionManager` | Delegated to Playwright context creation |
| About-blank last-tab protection | `CamoufoxSessionManager` | Native equivalent |
| Graceful stop versus forced kill | `CamoufoxSession` and `CamoufoxSessionManager` | Browser-Use `keep_alive` semantics preserved |

## Event contract

Every Browser-Use 0.13.13 request event has exactly one native owner:

- Lifecycle: `BrowserStartEvent`, `BrowserStopEvent`, `BrowserLaunchEvent`, `BrowserKillEvent`
- Navigation/tabs: `NavigateToUrlEvent`, `GoBackEvent`, `GoForwardEvent`, `RefreshEvent`, `SwitchTabEvent`, `CloseTabEvent`, `WaitEvent`
- Interaction: `ClickElementEvent`, `ClickCoordinateEvent`, `TypeTextEvent`, `ScrollEvent`, `ScrollToTextEvent`, `SendKeysEvent`, `UploadFileEvent`, `GetDropdownOptionsEvent`, `SelectDropdownOptionEvent`
- State: `BrowserStateRequestEvent`, `ScreenshotEvent`, `SaveStorageStateEvent`, `LoadStorageStateEvent`

The adapter emits native equivalents for connected/stopped, focus, tab,
navigation, dialog, download, storage, crash, health, and policy-error notifications.

`SendKeysEvent.target_id` switches to the requested native tab before sending
keys. Omitting it uses the focused tab; an unknown target fails without sending
keys to the current tab. Covered by `tests/test_upstream_01313.py`.

## 0.13.13 source upgrade

The event contract was reviewed against upstream commit
`129853546ff9cc169977d38b6038f9c4a4e734db`. The package installs compatible
`browser-use` 0.13.x releases from PyPI, starting at 0.13.13.
The 0.13.10–0.13.13 contract review classified changes as follows:

- Ported natively: explicit keyboard target selection through `SendKeysEvent`.
- Already covered: coordinate clicks preserve right/middle buttons; native
  input and tab-focus handling already provide the corresponding behavior.
- Inherited from upstream: nested-list secret redaction, provider/system-message
  fixes, case-insensitive hidden-code serialization, and filesystem image writes.
- CDP/cloud-only: actor CDP helpers, cloud lifecycle, and provider browser-tool
  drivers do not replace this adapter's Firefox-native event handlers.

The package caps Playwright below **1.64.0**. With Camoufox
`156.0.1-beta.36`, Playwright 1.64.0 sends an unsupported service-worker
request-interception parameter that breaks routing.

Baseline validation on Linux/Python 3.12 recorded 106 unified tests passing,
12 focused upstream parity tests passing, and zero Camoufox regressions in the
strict five-iteration differential benchmark. Timing-sensitive suites ran
sequentially after concurrent runs hit timeouts and a DOM performance threshold.

## Intentional boundaries

| Capability | Status | Reason |
| --- | --- | --- |
| Raw `cdp_client*` commands | Unsupported explicitly | Firefox/Camoufox has no Chromium CDP session |
| Connect to Browser-Use cloud CDP URL | Unsupported explicitly | This backend launches Camoufox through Playwright |
| Chromium extension pages | Not applicable | Camoufox does not install Browser-Use Chromium extensions |
| About-blank DVD animation | Not implemented | Cosmetic, not an automation contract |
| Remove live init script | Unsupported explicitly | Playwright cannot remove a context init script after registration |
| PDF text/vector printing | Adapted | Firefox output is a paginated raster PDF; default header/footer metadata (date, URL, page numbers) is drawn natively, custom HTML header/footer templates fail explicitly |
| Indexed element click trust | Native equivalent | Uses Playwright actionability and trusted input; occluded targets fail closed |
| Coordinate-only click trust | Native equivalent | Scales resized LLM-image coordinates to the cached CSS viewport, validates the hit point, and dispatches trusted Firefox pointer input |
| Coordinate drag action | Adapter extension | Scales resized LLM-image coordinates, validates both endpoints, dispatches one serialized trusted Firefox press, move, and release gesture, and reports observed endpoint/DOM changes separately from delivery |
| CSS element action geometry | Adapter extension | `find_elements` returns visible rectangles and centers in the current LLM screenshot coordinate space |
| Hung `evaluate` script | Adapter extension | Upstream bounds `evaluate` only by the 180s step timeout. Here, after 20s the page is pinged: if it still answers, the action reports a timeout; if the agent's script froze its main thread (Firefox cannot interrupt it), the tab is replaced by a fresh load of the same URL and the error says so. Busy pages that the agent did not freeze are left alone. |
| Closed shadow roots | Unsupported | Browser page JavaScript cannot inspect closed roots |
| Bounded task execution and recovery | Opt-in application policy | `BrowserTaskRunner` wraps the upstream Agent with budgets, a rolling public history view, adaptive screenshots, post-action evidence, bounded strategy feedback, and a per-run `lessons.md` notebook. Opt-in `ExperienceLearner` recall promotes lessons only after verified outcomes. Direct Agent use follows upstream behavior. The runner bounds `evaluate` output to one observation. Resuming a run requires the same live caller-owned context. Novelty is a progress heuristic. |
| Browser-Use CDP DOMSnapshot internals | Replaced natively | Playwright evaluates each selected Firefox frame and merges equivalent observable state |

## Verification

The optional runner defaults to observable page effects or caller-verified progress;
inspection-only novelty requires explicit opt-in. Adaptive vision is also opt-in
and uses upstream `use_vision='auto'` for pages without detected visual surfaces.
Firefox extraction indexes visible pointer-cursor boundaries and DOM `onclick`
properties, including custom div controls, without inspecting framework state.
General JavaScript evaluation remains available; prompt restrictions are not a
security boundary. Event-delegated controls without observable semantics may
still require coordinate interaction.

- Deterministic suite: `python run_tests.py` (fast contracts and browser integration tests)
- Lifecycle/recovery suite: `python -m unittest tests/test_reliability.py`
- Public-site suite: `CAMOUFOX_RUN_LIVE_TESTS=1 python -m unittest tests/test_live_compatibility.py`
- Version contract: this matrix describes the `browser-use` 0.13.13 baseline.
