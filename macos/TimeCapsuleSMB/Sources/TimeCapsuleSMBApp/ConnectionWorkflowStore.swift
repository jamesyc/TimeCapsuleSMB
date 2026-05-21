import Combine
import Foundation

enum ConnectionWorkflowState: String, CaseIterable, Equatable {
    case idle
    case discovering
    case discoveryReady
    case discoveryEmpty
    case discoveryFailed
    case configuring
    case configured
    case configureFailed

    var title: String {
        switch self {
        case .idle:
            return "Idle"
        case .discovering:
            return "Discovering"
        case .discoveryReady:
            return "Devices Found"
        case .discoveryEmpty:
            return "No Devices Found"
        case .discoveryFailed:
            return "Discovery Failed"
        case .configuring:
            return "Configuring"
        case .configured:
            return "Configured"
        case .configureFailed:
            return "Configure Failed"
        }
    }
}

struct DiscoveredDevice: Identifiable, Equatable {
    let id: String
    let name: String
    let host: String
    let hostname: String
    let addresses: [String]
    let syap: String?
    let model: String?
    let rawRecord: JSONValue

    init(payload: DiscoveredDevicePayload, index: Int) {
        self.id = payload.id.isEmpty ? "discovered-\(index)" : payload.id
        self.name = payload.name.isEmpty ? (payload.hostname.isEmpty ? "AirPort Device" : payload.hostname) : payload.name
        self.host = payload.host
        self.hostname = payload.hostname
        self.addresses = payload.addresses.isEmpty ? payload.ipv4 + payload.ipv6 : payload.addresses
        self.syap = payload.syap
        self.model = payload.model
        self.rawRecord = payload.selectedRecord
    }

    init(record: BonjourResolvedServicePayload, index: Int) {
        let stableParts = [
            record.fullname,
            record.serviceType,
            record.name,
            record.hostname,
            record.ipv4.joined(separator: ","),
            record.ipv6.joined(separator: ",")
        ]
        let stableID = stableParts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: "|")

        self.id = stableID.isEmpty ? "discovered-\(index)" : stableID
        self.name = record.name.isEmpty ? (record.hostname.isEmpty ? "AirPort Device" : record.hostname) : record.name
        self.hostname = record.hostname
        self.addresses = record.ipv4 + record.ipv6
        self.host = Self.displayHost(record)
        self.syap = record.properties["syAP"] ?? record.properties["syap"]
        self.model = record.properties["model"] ?? record.properties["am"]
        self.rawRecord = record.jsonValue
    }

    private static func displayHost(_ record: BonjourResolvedServicePayload) -> String {
        if let address = record.ipv4.first ?? record.ipv6.first {
            return address
        }
        return record.hostname
    }
}

struct ConfiguredDeviceState: Equatable {
    let host: String
    let configPath: String
    let configureId: String
    let sshAuthenticated: Bool
    let syap: String?
    let model: String?
    let compatibility: DeviceCompatibilityPayload?

    init(payload: ConfigurePayload) {
        self.host = payload.host
        self.configPath = payload.configPath
        self.configureId = payload.configureId
        self.sshAuthenticated = payload.sshAuthenticated
        self.syap = payload.deviceSyap ?? payload.device?.syap
        self.model = payload.deviceModel ?? payload.device?.model
        self.compatibility = payload.compatibility
    }
}

@MainActor
final class ConnectionWorkflowStore: ObservableObject {
    @Published var manualHost = ""
    @Published var bonjourTimeout = "6"
    @Published var debugLogging = false
    @Published private(set) var state: ConnectionWorkflowState = .idle
    @Published private(set) var devices: [DiscoveredDevice] = []
    @Published var selectedDeviceID: DiscoveredDevice.ID?
    @Published private(set) var configuredDevice: ConfiguredDeviceState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?

    let backend: BackendClient

    private var lastProcessedEventCount = 0
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        self.backend = backend
        backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
    }

    var events: [BackendEvent] {
        backend.events
    }

    var isRunning: Bool {
        backend.isRunning
    }

    var canCancel: Bool {
        backend.canCancel
    }

    var bonjourTimeoutValue: Double? {
        nonNegativeDouble(bonjourTimeout)
    }

    var selectedDevice: DiscoveredDevice? {
        guard let selectedDeviceID else {
            return nil
        }
        return devices.first { $0.id == selectedDeviceID }
    }

    func canConfigure(password: String) -> Bool {
        !backend.isRunning
            && !password.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && (selectedDevice != nil || !manualHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    func runDiscover() {
        guard let timeout = bonjourTimeoutValue else {
            failLocally(operation: "discover", state: .discoveryFailed, message: "Bonjour timeout must be a non-negative number.")
            return
        }
        resetRunState(clearDevices: true, clearConfiguredDevice: true)
        state = .discovering
        backend.run(operation: "discover", params: OperationParams.discover(timeout: timeout))
    }

    func runConfigure(password: String) {
        let trimmedPassword = password.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedPassword.isEmpty else {
            failLocally(operation: "configure", state: .configureFailed, message: "Password is required.")
            return
        }
        let selectedDevice = selectedDevice
        let trimmedHost = manualHost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard selectedDevice != nil || !trimmedHost.isEmpty else {
            failLocally(operation: "configure", state: .configureFailed, message: "Choose a discovered device or enter a host.")
            return
        }

        resetRunState(clearDevices: false, clearConfiguredDevice: true)
        state = .configuring
        let params = OperationParams.configure(
            host: trimmedHost,
            selectedRecord: selectedDevice?.rawRecord,
            password: password,
            debugLogging: debugLogging
        )
        backend.run(operation: "configure", params: params)
    }

    func select(_ device: DiscoveredDevice) {
        selectedDeviceID = device.id
    }

    func clear() {
        backend.clear()
        lastProcessedEventCount = 0
        state = .idle
        devices = []
        selectedDeviceID = nil
        configuredDevice = nil
        error = nil
        currentStage = nil
    }

    func cancel() {
        backend.cancel()
    }

    private func resetRunState(clearDevices: Bool, clearConfiguredDevice: Bool) {
        backend.clear()
        lastProcessedEventCount = 0
        error = nil
        currentStage = nil
        if clearDevices {
            devices = []
            selectedDeviceID = nil
        }
        if clearConfiguredDevice {
            configuredDevice = nil
        }
    }

    private func process(_ events: [BackendEvent]) {
        if events.count < lastProcessedEventCount {
            lastProcessedEventCount = 0
        }
        guard events.count > lastProcessedEventCount else {
            return
        }

        for event in events.dropFirst(lastProcessedEventCount) {
            handle(event)
        }
        lastProcessedEventCount = events.count
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "discover" || event.operation == "configure" else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
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
            applyFailureResult(event)
            return
        }

        switch event.operation {
        case "discover":
            applyDiscoverResult(event)
        case "configure":
            applyConfigureResult(event)
        default:
            break
        }
    }

    private func applyDiscoverResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(DiscoverPayload.self)
            let discoveredDevices = payload.devices.isEmpty
                ? payload.resolved.enumerated().map { index, record in DiscoveredDevice(record: record, index: index) }
                : payload.devices.enumerated().map { index, device in DiscoveredDevice(payload: device, index: index) }
            devices = discoveredDevices
            selectedDeviceID = discoveredDevices.count == 1 ? discoveredDevices[0].id : nil
            error = nil
            state = discoveredDevices.isEmpty ? .discoveryEmpty : .discoveryReady
        } catch {
            failContract(operation: "discover", state: .discoveryFailed, error: error)
        }
    }

    private func applyConfigureResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(ConfigurePayload.self)
            configuredDevice = ConfiguredDeviceState(payload: payload)
            error = nil
            state = .configured
        } catch {
            failContract(operation: "configure", state: .configureFailed, error: error)
        }
    }

    private func applyError(_ event: BackendEvent) {
        error = BackendErrorViewModel(event: event)
        switch event.operation {
        case "discover":
            state = .discoveryFailed
        case "configure":
            state = .configureFailed
        default:
            break
        }
    }

    private func applyFailureResult(_ event: BackendEvent) {
        let message = event.payloadSummaryText ?? event.summary
        error = BackendErrorViewModel(
            operation: event.operation,
            code: "operation_failed",
            message: message
        )
        switch event.operation {
        case "discover":
            state = .discoveryFailed
        case "configure":
            state = .configureFailed
        default:
            break
        }
    }

    private func failContract(operation: String, state: ConnectionWorkflowState, error: Error) {
        self.error = BackendErrorViewModel(
            operation: operation,
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        self.state = state
    }

    private func failLocally(operation: String, state: ConnectionWorkflowState, message: String) {
        error = BackendErrorViewModel(
            operation: operation,
            code: "validation_failed",
            message: message
        )
        currentStage = nil
        self.state = state
    }

    private func nonNegativeDouble(_ text: String) -> Double? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value = Double(trimmed), value.isFinite, value >= 0 else {
            return nil
        }
        return value
    }
}
