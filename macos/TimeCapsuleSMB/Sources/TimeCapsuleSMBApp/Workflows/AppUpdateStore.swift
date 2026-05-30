import Combine
import Foundation

enum AppUpdateState: String, Equatable {
    case idle
    case checking
    case current
    case unavailable
    case updateAvailable
    case failed

    var title: String {
        switch self {
        case .idle:
            return L10n.string("app_update.state.idle")
        case .checking:
            return L10n.string("app_update.state.checking")
        case .current:
            return L10n.string("app_update.state.current")
        case .unavailable:
            return L10n.string("app_update.state.unavailable")
        case .updateAvailable:
            return L10n.string("app_update.state.update_available")
        case .failed:
            return L10n.string("app_update.state.failed")
        }
    }
}

@MainActor
final class AppUpdateStore: ObservableObject {
    @Published private(set) var state: AppUpdateState = .idle
    @Published private(set) var payload: VersionCheckPayload?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?

    let lane: OperationLane

    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []

    init(coordinator: OperationCoordinator) {
        self.lane = coordinator.lane(for: .localPath("app-update"))
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
        lane.backend.$isRunning
            .dropFirst()
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    var isChecking: Bool {
        lane.backend.isRunning
    }

    func checkNow(settings: AppSettings) {
        guard !lane.isBusy else {
            state = .failed
            error = BackendErrorViewModel(
                operation: "version-check",
                code: "operation_rejected",
                message: L10n.string("operation.error.already_running")
            )
            return
        }
        lane.clear()
        operationObserver.clear()
        state = .checking
        payload = nil
        error = nil
        currentStage = nil

        let params = OperationParams.versionCheck(url: settings.versionCheckURL)
        switch lane.run(operation: "version-check", params: params, context: nil, activeDeviceID: nil) {
        case .started(let operation):
            operationObserver.start(operation)
            process(lane.backend.events)
        case .rejected(let message):
            state = .failed
            operationObserver.clear()
            error = BackendErrorViewModel(
                operation: "version-check",
                code: "operation_rejected",
                message: message
            )
        }
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, _ in
            handle(event)
        }
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "version-check" else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }
        if event.type == "error" {
            error = BackendErrorViewModel(event: event)
            state = .failed
            operationObserver.finish()
            return
        }
        guard event.type == "result" else {
            return
        }
        do {
            let result = try event.decodePayload(VersionCheckPayload.self)
            payload = result
            if result.shouldBlock {
                state = .updateAvailable
            } else if result.source == "unavailable" {
                state = .unavailable
            } else {
                state = .current
            }
            error = nil
            operationObserver.finish()
        } catch {
            self.error = BackendErrorViewModel(
                operation: "version-check",
                code: "contract_decode_failed",
                message: error.localizedDescription
            )
            state = .failed
            operationObserver.finish()
        }
    }
}
