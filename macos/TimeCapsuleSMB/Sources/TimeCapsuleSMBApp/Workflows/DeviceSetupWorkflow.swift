import Combine
import Foundation

enum DeviceSetupWorkflowState: Equatable {
    case idle
    case checkingLocalNetwork
    case configuring
    case awaitingConfirmation
    case savingProfile
    case saved
    case authFailed
    case unsupported
    case failed
}

@MainActor
final class DeviceSetupWorkflow: ObservableObject {
    @Published private(set) var state: DeviceSetupWorkflowState = .idle
    @Published private(set) var savedProfile: DeviceProfile?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?

    let coordinator: OperationCoordinator
    let profilePersistence: DeviceProfilePersistenceService
    private let localNetworkPreflightChecker: LocalNetworkPreflightChecking?

    // Reset invalidates pending UI updates; persistence still owns its draft
    // until it returns, independently of whether the helper has stopped.
    private var generation = UUID()
    private var savingProfile = false
    private var pendingConfigureDraft: ConfigureProfileDraft?
    private var activeLaneKey: OperationLaneKey?
    private var localNetworkPreflightTask: Task<Void, Never>?
    private var operationObservers: [OperationLaneKey: BackendOperationObserver] = [:]
    private var cancellables: Set<AnyCancellable> = []
    private var observedLaneKeys: Set<OperationLaneKey> = []
    private var currentRun: ConfigureRun?
    private var stoppedAtSavedAirport: SavedAirportStop?

    /// An add as runConfigureBackend started it: what running it again needs,
    /// besides the password, which stays in pendingPassword.
    private struct ConfigureRun {
        let target: AddDeviceTarget
        let preferredID: DeviceProfile.ID
        let newProfileSettings: DeviceProfileSettings
        let localNetworkPreflight: LocalNetworkPreflightResult?
    }

    /// Configure stopped this add at a saved AirPort (device_already_saved), a
    /// step of the add and not a failure. Once the helper exits, the stopped run
    /// leaves Activity, and the add runs again as that saved device, or ends as
    /// cancelled when a Cancel from any page stopped the helper first.
    private struct SavedAirportStop {
        let airportMAC: String
        let run: ConfigureRun
    }

    init(
        coordinator: OperationCoordinator,
        profilePersistence: DeviceProfilePersistenceService,
        localNetworkPreflightChecker: LocalNetworkPreflightChecking? = nil
    ) {
        self.coordinator = coordinator
        self.profilePersistence = profilePersistence
        self.localNetworkPreflightChecker = localNetworkPreflightChecker
    }

    var isRunning: Bool {
        if localNetworkPreflightTask != nil || savingProfile {
            return true
        }
        switch activeLaneKey {
        case .some(let key):
            return coordinator.lane(for: key).isBusy
        case .none:
            return false
        }
    }

    var canCancel: Bool {
        if localNetworkPreflightTask != nil {
            return true
        }
        guard let activeLaneKey else {
            return false
        }
        return coordinator.lane(for: activeLaneKey).backend.canCancel
    }

    func start(
        target: AddDeviceTarget,
        password: String,
        existingProfile: DeviceProfile?,
        preferredID: DeviceProfile.ID = UUID().uuidString.lowercased(),
        settings: DeviceProfileSettings,
        newProfileSettings: DeviceProfileSettings
    ) {
        begin(
            target: target,
            password: password,
            existingProfile: existingProfile,
            preferredID: preferredID,
            settings: settings,
            newProfileSettings: newProfileSettings,
            rerunOf: nil
        )
    }

    /// A rerun (`rerunOf`) skips the Local Network check its first run made,
    /// and never sends saved MACs: it cannot stop at a saved AirPort again.
    private func begin(
        target: AddDeviceTarget,
        password: String,
        existingProfile: DeviceProfile?,
        preferredID: DeviceProfile.ID,
        settings: DeviceProfileSettings,
        newProfileSettings: DeviceProfileSettings,
        rerunOf firstRun: ConfigureRun?
    ) {
        guard !isRunning else { return }
        let profileID = existingProfile?.id ?? preferredID
        let laneKey = target.setupLaneKey(existingProfileID: existingProfile?.id)
        let lane = coordinator.lane(for: laneKey)
        observe(lane: lane)

        guard !lane.isBusy else {
            clearPendingConfigureDraft()
            rejectRun(L10n.string("operation.error.already_running"))
            return
        }

        let configureDraft: ConfigureProfileDraft
        do {
            configureDraft = try profilePersistence.prepareConfigureTarget(
                targetHost: target.targetHost,
                discoveredDevice: target.discoveredDevice,
                existingProfile: existingProfile,
                preferredID: profileID,
                settings: settings
            )
        } catch {
            failProfileSave(error)
            return
        }

        resetRunState()
        pendingConfigureDraft = configureDraft
        pendingPassword = password
        if configureDraft.existingProfileID == nil {
            pendingNewProfileSettings = newProfileSettings
        }
        observer(for: laneKey).clear()
        // A device matched to none saved by address or Bonjour record may still
        // be one by its MAC: configure stops there before writing a config.
        let sendsSavedAirportMACs = existingProfile == nil && firstRun == nil
        // Telemetry's account of how a saved device's new address was found:
        // typed in, or a discovered record only its MAC tied to it.
        let addressSource: AddressSource? = firstRun == nil || existingProfile == nil ? nil
            : target.discoveredDevice == nil ? .manual : .mac

        if firstRun == nil, let localNetworkPreflightChecker {
            state = .checkingLocalNetwork
            localNetworkPreflightTask = Task { [weak self, localNetworkPreflightChecker] in
                let result = await localNetworkPreflightChecker.check()
                await MainActor.run {
                    guard let self, !Task.isCancelled else {
                        return
                    }
                    self.localNetworkPreflightTask = nil
                    self.runConfigureBackend(
                        target: target,
                        password: password,
                        settings: settings,
                        newProfileSettings: newProfileSettings,
                        existingProfile: existingProfile,
                        configureDraft: configureDraft,
                        laneKey: laneKey,
                        localNetworkPreflight: result,
                        sendsSavedAirportMACs: sendsSavedAirportMACs,
                        addressSource: addressSource
                    )
                }
            }
            return
        }

        runConfigureBackend(
            target: target,
            password: password,
            settings: settings,
            newProfileSettings: newProfileSettings,
            existingProfile: existingProfile,
            configureDraft: configureDraft,
            laneKey: laneKey,
            localNetworkPreflight: firstRun?.localNetworkPreflight,
            sendsSavedAirportMACs: sendsSavedAirportMACs,
            addressSource: addressSource
        )
    }

    private func runConfigureBackend(
        target: AddDeviceTarget,
        password: String,
        settings: DeviceProfileSettings,
        newProfileSettings: DeviceProfileSettings,
        existingProfile: DeviceProfile?,
        configureDraft: ConfigureProfileDraft,
        laneKey: OperationLaneKey,
        localNetworkPreflight: LocalNetworkPreflightResult?,
        sendsSavedAirportMACs: Bool,
        addressSource: AddressSource?
    ) {
        let lane = coordinator.lane(for: laneKey)
        let savedAirportMACs = sendsSavedAirportMACs
            ? profilePersistence.registry.profiles.compactMap(\.network.airportMAC).sorted()
            : []
        switch coordinator.run(
            operation: "configure",
            params: OperationParams.Configure.save(
                host: target.targetHost,
                selectedRecord: target.selectedRecord,
                password: password,
                debugLogging: settings.debugLogging,
                internalShareUseDiskRoot: settings.internalShareUseDiskRoot,
                smbBrowseCompatibility: settings.smbBrowseCompatibility,
                mdnsAdvertiseAFP: settings.mdnsAdvertiseAFP,
                anyProtocol: settings.anyProtocol,
                requireSMBEncryption: settings.requireSMBEncryption,
                forceDisableSMBSigningAndEncryption: settings.forceDisableSMBSigningAndEncryption,
                fruitMetadataNetatalk: settings.fruitMetadataNetatalk,
                vfsAIOForkEnabled: settings.vfsAIOForkEnabled,
                ataIdleSeconds: settings.ataIdleSeconds,
                ataStandby: settings.ataStandby,
                includeAtaStandby: true,
                localNetworkPreflight: localNetworkPreflight,
                addressSource: addressSource,
                savedAirportMACs: savedAirportMACs
            ),
            context: configureDraft.context,
            activeDeviceID: existingProfile?.id,
            laneKey: laneKey
        ) {
        case .started(let operation):
            activeLaneKey = laneKey
            currentRun = ConfigureRun(
                target: target,
                preferredID: configureDraft.profileID,
                newProfileSettings: newProfileSettings,
                localNetworkPreflight: localNetworkPreflight
            )
            observer(for: laneKey).start(operation)
            state = .configuring
            process(lane.backend.events, laneKey: laneKey)
        case .rejected(let message):
            clearPendingConfigureDraft()
            rejectRun(message)
        }
    }

    func cancel() {
        if let localNetworkPreflightTask {
            localNetworkPreflightTask.cancel()
            self.localNetworkPreflightTask = nil
            clearPendingConfigureDraft()
            pendingNewProfileSettings = nil
            pendingPassword = ""
            state = .idle
            return
        }
        guard let activeLaneKey else {
            return
        }
        coordinator.cancel(laneKey: activeLaneKey)
    }

    func reset() {
        generation = UUID()
        stoppedAtSavedAirport = nil
        localNetworkPreflightTask?.cancel()
        localNetworkPreflightTask = nil
        if let activeLaneKey {
            let lane = coordinator.lane(for: activeLaneKey)
            if !lane.isBusy {
                lane.clear()
            }
        }
        savedProfile = nil
        error = nil
        currentStage = nil
        if !savingProfile { clearPendingConfigureDraft() }
        if !isRunning { activeLaneKey = nil }
        operationObservers = [:]
        pendingNewProfileSettings = nil
        state = .idle
    }

    private var pendingNewProfileSettings: DeviceProfileSettings?

    private func observe(lane: OperationLane) {
        guard observedLaneKeys.insert(lane.key).inserted else {
            return
        }
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events, laneKey: lane.key)
                }
            }
            .store(in: &cancellables)
        lane.backend.$isRunning
            .dropFirst()
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.releaseFinishedLane()
                    self?.objectWillChange.send()
                }
            }
            .store(in: &cancellables)
    }

    private func resetRunState() {
        generation = UUID()
        localNetworkPreflightTask?.cancel()
        localNetworkPreflightTask = nil
        if let activeLaneKey {
            let lane = coordinator.lane(for: activeLaneKey)
            if !lane.isBusy {
                lane.clear()
            }
            observer(for: activeLaneKey).clear()
        }
        error = nil
        currentStage = nil
        savedProfile = nil
        activeLaneKey = nil
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
    }

    private func process(_ events: [BackendEvent], laneKey: OperationLaneKey) {
        observer(for: laneKey).process(events) { event, _ in
            handle(event)
        }
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "configure" else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if state == .awaitingConfirmation {
                state = .configuring
            }
            return
        }
        if event.type == "error" {
            applyError(event)
            return
        }
        guard event.type == "result" else {
            return
        }
        if event.ok == false {
            failFromResult(event)
            return
        }
        applyConfigureResult(event)
    }

    private func applyConfigureResult(_ event: BackendEvent) {
        let configured: ConfiguredDeviceState
        do {
            configured = ConfiguredDeviceState(payload: try event.decodePayload(ConfigurePayload.self))
        } catch {
            failContract(error)
            return
        }

        state = .savingProfile
        guard let configureDraft = pendingConfigureDraft else {
            failContract(DeviceRegistryError.profileNotFound("pending"))
            return
        }
        let overrides = ConfiguredDeviceProfileOverrides(
            displayName: nil,
            settings: pendingNewProfileSettings
        )
        let savedPassword = pendingPassword
        let generation = self.generation
        savingProfile = true
        Task { @MainActor in
            defer {
                savingProfile = false
                releaseFinishedLane()
                objectWillChange.send()
            }
            do {
                let saved = try await profilePersistence.commitConfiguredProfile(
                    configuredDevice: configured,
                    draft: configureDraft,
                    password: savedPassword,
                    overrides: overrides
                )
                guard generation == self.generation else { return }
                savedProfile = saved
                error = nil
                state = .saved
                finishActiveOperation()
                clearPendingConfigureDraft()
                pendingNewProfileSettings = nil
                pendingPassword = ""
            } catch {
                guard generation == self.generation else { return }
                failProfileSave(error)
            }
        }
    }

    private var pendingPassword = ""

    private func applyError(_ event: BackendEvent) {
        if event.code == "device_already_saved", activeLaneKey != nil, let run = currentRun,
           case .object(let details)? = event.details, case .string(let reported)? = details["airport_mac"],
           let airportMAC = DeviceNetworkIdentity.normalizedAirportMAC(reported) {
            // Configure stopped at a saved AirPort before writing a config. The
            // add stays in progress and runs again as that saved device.
            stoppedAtSavedAirport = SavedAirportStop(airportMAC: airportMAC, run: run)
            currentStage = nil
            finishActiveOperation()
            return
        }
        if event.code == "confirmation_required" {
            error = nil
            state = .awaitingConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            applyConfirmationCancelled()
            return
        }
        error = BackendErrorViewModel(event: event)
        switch event.code {
        case "auth_failed":
            state = .authFailed
        case "unsupported_device":
            state = .unsupported
        default:
            state = .failed
        }
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func applyConfirmationCancelled() {
        error = nil
        currentStage = nil
        savedProfile = nil
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        state = .idle
    }

    private func failFromResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: event.operation,
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func failContract(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "add-device",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func failProfileSave(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "add-device",
            code: "profile_save_failed",
            message: profileSaveFailureMessage(error)
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    /// A save refused because the AirPort is another saved device, by its MAC,
    /// names that device; configure stops at a saved AirPort first, so this is
    /// rare. A clash on the address alone can be another AirPort that only had
    /// it, and keeps the registry's own message.
    private func profileSaveFailureMessage(_ error: Error) -> String {
        if let savedID = (error as? DeviceRegistryError)?.profileWithSameAirportMAC,
           let saved = profilePersistence.registry.profile(id: savedID) {
            return L10n.format("add_device.error.already_saved", saved.title)
        }
        return error.localizedDescription
    }

    private func rejectRun(_ message: String) {
        error = BackendErrorViewModel(
            operation: "add-device",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func observer(for laneKey: OperationLaneKey) -> BackendOperationObserver {
        if let observer = operationObservers[laneKey] {
            return observer
        }
        let observer = BackendOperationObserver()
        operationObservers[laneKey] = observer
        return observer
    }

    private func finishActiveOperation() {
        if let activeLaneKey {
            operationObservers[activeLaneKey]?.finish()
        }
        // Stop consuming results, but retain the lane until the helper releases it.
        // isRunning reads live ownership even while an error is already visible.
        releaseFinishedLane()
    }

    private func releaseFinishedLane() {
        guard let key = activeLaneKey, !savingProfile,
              operationObservers[key]?.activeOperation == nil else { return }
        let lane = coordinator.lane(for: key)
        guard !lane.isBusy else { return }
        activeLaneKey = nil
        // While a stop waits, no other run can start, so it is this lane's.
        guard let stop = stoppedAtSavedAirport else { return }
        stoppedAtSavedAirport = nil
        // Read before clear(), which forgets it.
        let cancelled = lane.backend.lastExitCode == HelperRunResult.cancelledExitCode
        coordinator.clear(laneKey: key)
        if cancelled {
            // What a cancelled helper reports, which this one, already
            // finished, could not.
            applyError(BackendEvent.error(operation: "configure", code: "cancelled",
                                          message: L10n.string("helper.error.cancelled")))
        } else {
            runAgain(stop)
        }
    }

    /// Runs a stopped add again as the saved device with the AirPort's MAC: its
    /// lane, settings, config and MAC check, as when its address is changed in
    /// Settings. A device forgotten in the meantime is added as a new one. A
    /// device runs one operation at a time, so a busy one fails the add with
    /// its name; the page keeps the address and password for Save Device.
    private func runAgain(_ stop: SavedAirportStop) {
        let run = stop.run
        let saved = profilePersistence.registry.profiles.first { $0.network.airportMAC == stop.airportMAC }
        if let saved, coordinator.isDeviceBusy(saved) {
            rejectRun(L10n.format("add_device.error.saved_device_busy", saved.title))
            return
        }
        begin(
            target: run.target,
            password: pendingPassword,
            existingProfile: saved,
            preferredID: run.preferredID,
            settings: saved?.settings ?? run.newProfileSettings,
            newProfileSettings: run.newProfileSettings,
            rerunOf: run
        )
    }

    private func clearPendingConfigureDraft() {
        profilePersistence.discardConfigureDraft(pendingConfigureDraft)
        pendingConfigureDraft = nil
    }
}
