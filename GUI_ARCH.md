# TimeCapsuleSMB GUI Architecture

This is the living architecture target for the macOS GUI. Future GUI changes
should reference this file and keep the implementation moving toward these
boundaries.

## Product Shape

The GUI is a native multi-device manager for Apple Time Capsules. It should not
feel like a wrapper around CLI commands.

The main user flows are:

1. Add one or more Time Capsules.
2. Save device profiles with per-device config files.
3. Store passwords in Keychain only.
4. Install or update SMB support.
5. Run checkups and show structured health.
6. Run maintenance tasks with explicit plans and confirmations.
7. Surface advanced logs and helper details only when needed.

`bootstrap`, `paths`, and `validate-install` are app readiness concerns. They
run in the background or diagnostics surfaces, not as first-class user actions.
The bundled app should already contain the helper, runtime, tools, artifacts,
and manifests needed by those checks.

## Architectural Principles

- The app is profile-first. Screens operate on `DeviceProfile`, not loose host
  fields or a shared `.env`.
- Views are thin. They render state and send user intents to stores.
- Stores own state machines. Each workflow has explicit states, terminal states,
  validation, and event-to-model parsing.
- Backend execution is centralized. There is one global `OperationCoordinator`
  and one active helper operation at a time.
- Backend contracts are typed at the GUI boundary. Swift decodes payloads into
  models and does not parse human log text for app behavior.
- Credentials never persist to `.env`. GUI passwords live in Keychain and are
  passed per operation as credentials.
- Runtime context is explicit. Profile-scoped operations always carry
  `DeviceRuntimeContext`.
- Device snapshots are attributed to the operation profile ID, not the currently
  selected sidebar item.
- Advanced diagnostics exist, but normal workflows use user-facing language:
  Install / Update, Checkup, Maintenance, Add Time Capsule.

## Layer Map

Target source organization:

```text
TimeCapsuleSMBApp/
  App/
    AppStore.swift
    AppReadinessStore.swift
  Backend/
    BackendClient.swift
    BackendPayloads.swift
    HelperLocator.swift
    HelperRunner.swift
    OperationCoordinator.swift
    OperationParams.swift
    PendingConfirmation.swift
  Profiles/
    DeviceProfile.swift
    DeviceRegistryStore.swift
    PasswordStore.swift
  Policies/
    HostCompatibilityPolicy.swift
  Workflows/
    AddDeviceFlowStore.swift
    DashboardStore.swift
    DeployWorkflowStore.swift
    DoctorStore.swift
    MaintenanceStore.swift
  Views/
    Shell/
    AddDevice/
    Dashboard/
    Diagnostics/
    Components/
```

The current code can keep file names during transition, but new substantial
screen code should move toward this split instead of growing `ContentView.swift`.

## Ownership

### AppStore

`AppStore` is the app composition root. It owns:

- `AppReadinessStore`
- `DeviceRegistryStore`
- `OperationCoordinator`
- `PasswordStore`
- selected profile ID
- high-level navigation state

`AppStore` should not parse backend events. It may derive cross-cutting summary
state such as the dashboard primary action, host compatibility warnings, and
password availability.

### DeviceRegistryStore

`DeviceRegistryStore` owns persistent device profiles:

```text
~/Library/Application Support/TimeCapsuleSMB/devices.json
~/Library/Application Support/TimeCapsuleSMB/Devices/<device-id>/.env
```

The registry is responsible for:

- loading and saving `devices.json`
- creating per-device config directories
- duplicate matching by Bonjour fullname and normalized host
- deleting profile config directories
- persisting checkup and deploy snapshots

It must not delete corrupt registries automatically. Corrupt registry state
goes to diagnostics and waits for explicit user recovery.

### PasswordStore

`PasswordStore` abstracts Keychain access.

Production storage:

```text
service = TimeCapsuleSMB.DevicePassword
account = <DeviceProfile.id>
```

Rules:

- Add Device saves a password only after `configure` succeeds.
- `.env` files never contain `TC_PASSWORD`.
- Missing Keychain item maps to `passwordNeeded` or `.missing`.
- Keychain access errors map to `.keychainUnavailable`.
- Auth failures mark the password invalid, but do not delete it automatically.
- Forget Device deletes the profile, per-device config directory, and Keychain
  item as one user-visible action.

## Backend Execution

`BackendClient` owns process execution state and raw events. It should not know
about UI screens.

`OperationCoordinator` is the only workflow-facing entry point for helper runs:

```swift
run(operation:params:profile:password:)
run(operation:params:context:activeDeviceID:password:)
```

Responsibilities:

- reject a second operation while one is running
- expose active operation and active profile ID
- inject password credentials when provided
- delegate profile context to `BackendClient`
- preserve context through confirmation replay
- support cancel and clear semantics

Profile-scoped operations must pass `DeviceRuntimeContext`. The backend layer
injects:

- `params["config"] = context.configURL.path`
- `TCAPSULE_CONFIG = context.configURL.path`

`TCAPSULE_STATE_DIR` remains app-level so bootstrap/version/cache state is not
multiplied per profile.

## Operation Attribution

Workflow stores must attribute terminal results to the profile that started the
operation.

Do not write snapshots using `selectedProfile` at result time. The user can
change sidebar selection while an operation runs. A workflow should capture
`activeProfileID` when it starts, then use that ID when persisting:

- `DeviceCheckupSnapshot`
- `DeviceDeploySnapshot`
- future maintenance snapshots

If `OperationCoordinator` rejects a run, the caller must leave or restore its
state to a non-running failure state. No workflow should enter `running`,
`planning`, `configuring`, or `saving` unless the operation actually started.

## Backend Contract

The Python app API is the source of truth for structured payloads. GUI-facing
payloads should remain stable and versioned.

Important contracts:

- `discover` returns `devices`, a deduped list of selectable Time Capsules.
- Each discovered device includes `selected_record`, which the GUI passes back
  to `configure`.
- `configure` accepts either `selected_record` or `host`.
- Manual `host` values are treated as root SSH targets by the backend.
- GUI `configure` sends `persist_password: false`.
- Deploy, doctor, activate, uninstall, and fsck receive credentials from
  Keychain-backed GUI state.

Swift should prefer decoding structured fields over reading `summary` strings.
Raw summaries are for display only.

## Add Device Flow

Add Device is a state machine with mutually exclusive entry modes:

- Discover
- Manual Address

States:

```text
idle
discovering
discoveryEmpty
discoveryReady
manualEntry
passwordEntry
configuring
savingProfile
saved
authFailed
unsupported
failed
```

Discover mode:

- runs backend `discover`
- shows only `payload.devices`
- auto-selects if there is exactly one device
- fills and disables Host/IP from the selected device
- routes already saved devices to their existing profile

Manual mode:

- clears discovered candidates from the active flow
- enables Host/IP entry
- assumes root SSH unless the user explicitly enters a user

Save rules:

- no profile is saved until `configure` succeeds
- wrong password saves nothing
- unsupported device saves nothing
- duplicate host or Bonjour fullname updates the existing profile
- Keychain save failure may keep the profile, but marks password state missing

## Dashboard

The dashboard has these user-facing tabs:

- Overview
- Install / Update
- Checkup
- Maintenance
- Advanced

Overview is decision-oriented. It shows device identity, password state, host
macOS warnings, last checkup, last install/update, and one primary action.

Install / Update wraps deploy planning and deploy execution. Dry-run planning
should remain first-class.

Checkup wraps doctor and shows grouped checks by domain and status.

Maintenance wraps:

- NetBSD4 activation
- uninstall
- fsck
- repair xattrs
- future flash workflow

Advanced contains raw events, helper path, profile ID, config path, and other
technical diagnostics.

## App Readiness And Bundling

Readiness runs at app launch and validates the bundled runtime. It is not a
device workflow.

Production bundle target:

```text
Contents/MacOS/TimeCapsuleSMB
Contents/Helpers/tcapsule
Contents/Resources/Distribution/...
Contents/Resources/Tools/...
```

The app sets:

- `TCAPSULE_CONFIG` per profile operation
- `TCAPSULE_STATE_DIR` to app support
- `TCAPSULE_DISTRIBUTION_ROOT` to bundled distribution resources
- `PATH` to bundled tools where required

If bundled resources are missing or invalid, normal workflows are blocked and
diagnostics explain that the app install is incomplete.

## Host Compatibility

`HostCompatibilityPolicy` is pure Swift and side-effect free. It warns
non-blockingly for host macOS versions with known Time Machine network backup
issues:

- macOS 15.7.5
- macOS 15.7.6
- macOS 15.7.7
- macOS 26.4.x

Warnings appear globally or on dashboards, but they do not prevent SMB install
or maintenance.

## Error Handling

Errors should preserve machine-readable codes and user-facing recovery.

Workflow stores should map backend errors into:

- state transition
- concise visible message
- recovery action, when available
- raw details in Advanced or Diagnostics

Authentication failures must prompt for password replacement without deleting
the existing Keychain item automatically.

Unsupported devices must show the compatibility explanation and avoid creating
profiles.

## Testing Standards

Every workflow state enum should have an inventory test. Tests should verify
state transitions and side effects through mocks, not string grep checks.

Required coverage areas:

- missing, corrupt, save, update, duplicate, and delete registry behavior
- Keychain save/read/update/delete, missing item, and unavailable item
- backend context injection and confirmation replay context preservation
- operation rejection while another operation is active
- add-device discover/manual/auth/unsupported/duplicate/password-save failure
- dashboard primary action derivation
- operation snapshots attributed to active operation profile ID
- host compatibility warning matrix
- helper locator production and development environment behavior

Regression runs:

```bash
cd macos/TimeCapsuleSMB && swift test
.venv/bin/pytest
```

Run Python tests from the repo root. Run Swift tests from
`macos/TimeCapsuleSMB`.
