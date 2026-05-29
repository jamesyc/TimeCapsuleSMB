import Combine
import Foundation

enum AppReadinessStateKind: String, CaseIterable, Equatable {
    case idle
    case resolvingBundle
    case checkingVersion
    case checkingCapabilities
    case validatingInstall
    case ready
    case degraded
    case blocked

    var title: String {
        switch self {
        case .idle:
            return L10n.string("app_readiness.state.idle")
        case .resolvingBundle:
            return L10n.string("app_readiness.state.resolving_bundle")
        case .checkingVersion:
            return L10n.string("app_readiness.state.checking_version")
        case .checkingCapabilities:
            return L10n.string("app_readiness.state.checking_capabilities")
        case .validatingInstall:
            return L10n.string("app_readiness.state.validating_install")
        case .ready:
            return L10n.string("app_readiness.state.ready")
        case .degraded:
            return L10n.string("app_readiness.state.degraded")
        case .blocked:
            return L10n.string("app_readiness.state.blocked")
        }
    }
}

struct AppReadinessSummary: Equatable {
    let runtimeMode: BundleRuntimeMode
    let helperVersion: String
    let distributionRoot: String
    let validationSummary: String
    let validationCounts: [String: Int]
}

enum AppReadinessState: Equatable {
    case idle
    case resolvingBundle
    case checkingVersion
    case checkingCapabilities
    case validatingInstall
    case ready(AppReadinessSummary)
    case degraded(AppReadinessSummary, [BundleRuntimeIssue])
    case blocked(BundleRuntimeIssue)

    var kind: AppReadinessStateKind {
        switch self {
        case .idle:
            return .idle
        case .resolvingBundle:
            return .resolvingBundle
        case .checkingVersion:
            return .checkingVersion
        case .checkingCapabilities:
            return .checkingCapabilities
        case .validatingInstall:
            return .validatingInstall
        case .ready:
            return .ready
        case .degraded:
            return .degraded
        case .blocked:
            return .blocked
        }
    }
}

struct AppReadinessVersionCheck: Equatable {
    var url: String

    func params() -> [String: JSONValue] {
        OperationParams.versionCheck(url: url)
    }
}

protocol AppRuntimeResolving {
    func resolve(helperPath: String?) throws -> HelperResolution
    func runtimeIssues(for resolution: HelperResolution) -> [BundleRuntimeIssue]
}

extension HelperLocator: AppRuntimeResolving {}

@MainActor
final class AppReadinessStore: ObservableObject {
    @Published private(set) var state: AppReadinessState = .idle
    @Published private(set) var capabilities: CapabilitiesPayload?
    @Published private(set) var validation: InstallValidationPayload?
    @Published private(set) var versionCheckPayload: VersionCheckPayload?
    @Published private(set) var issues: [BundleRuntimeIssue] = []
    @Published private(set) var currentStage: OperationStageState?

    let backend: BackendClient

    private let runtimeResolver: any AppRuntimeResolving
    private let helperPathProvider: () -> String
    private var runtimeMode: BundleRuntimeMode = .developmentCheckout
    private var versionCheck: AppReadinessVersionCheck?
    private var pendingOperation: PendingReadinessOperation?
    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []

    convenience init(backend: BackendClient) {
        self.init(
            backend: backend,
            runtimeResolver: HelperLocator(),
            helperPathProvider: { backend.helperPath }
        )
    }

    init(
        backend: BackendClient,
        runtimeResolver: any AppRuntimeResolving,
        helperPathProvider: @escaping () -> String
    ) {
        self.backend = backend
        self.runtimeResolver = runtimeResolver
        self.helperPathProvider = helperPathProvider
        backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
        backend.$isRunning
            .sink { [weak self] isRunning in
                guard !isRunning else { return }
                Task { @MainActor in
                    self?.runPendingOperation()
                }
            }
            .store(in: &cancellables)
    }

    var canRetry: Bool {
        !backend.isRunning
    }

    func applyVersionCheck(_ versionCheck: AppReadinessVersionCheck?) {
        self.versionCheck = versionCheck
    }

    func start() {
        guard !backend.isRunning else { return }
        backend.clear()
        capabilities = nil
        validation = nil
        versionCheckPayload = nil
        issues = []
        currentStage = nil
        pendingOperation = nil
        operationObserver.clear()
        state = .resolvingBundle

        let helperPath = normalized(helperPathProvider())
        do {
            let resolution = try runtimeResolver.resolve(helperPath: helperPath)
            runtimeMode = resolution.mode
            issues = runtimeResolver.runtimeIssues(for: resolution)
            if let blockingIssue = issues.first(where: { $0.severity == .error }) {
                state = .blocked(blockingIssue)
                return
            }
        } catch {
            state = .blocked(BundleRuntimeIssue(
                code: .helperMissing,
                severity: .error,
                message: error.localizedDescription
            ))
            return
        }

        if let versionCheck {
            pendingOperation = PendingReadinessOperation(operation: "version-check", params: versionCheck.params())
        } else {
            pendingOperation = PendingReadinessOperation(operation: "capabilities")
        }
        runPendingOperation()
    }

    func clear() {
        backend.clear()
        capabilities = nil
        validation = nil
        versionCheckPayload = nil
        issues = []
        currentStage = nil
        pendingOperation = nil
        operationObserver.clear()
        state = .idle
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, _ in
            handle(event)
        }
    }

    private func handle(_ event: BackendEvent) {
        guard ["version-check", "capabilities", "validate-install"].contains(event.operation) else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }

        if event.type == "error" {
            if event.operation == "version-check" {
                issues.append(versionMetadataIssue(message: event.message ?? event.localizedSummary))
                pendingOperation = PendingReadinessOperation(operation: "capabilities")
                operationObserver.finish()
                runPendingOperation()
                return
            }
            operationObserver.finish()
            state = .blocked(issue(from: event))
            return
        }

        guard event.type == "result" else {
            return
        }

        switch event.operation {
        case "version-check":
            applyVersionCheckResult(event)
        case "capabilities":
            applyCapabilitiesResult(event)
        case "validate-install":
            applyValidationResult(event)
        default:
            break
        }
    }

    private func applyVersionCheckResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(VersionCheckPayload.self)
            versionCheckPayload = payload
            guard event.ok == true else {
                issues.append(versionMetadataIssue(message: payload.localizedSummary))
                pendingOperation = PendingReadinessOperation(operation: "capabilities")
                operationObserver.finish()
                runPendingOperation()
                return
            }
            if payload.source == "unavailable" {
                issues.append(versionMetadataIssue(message: payload.localizedSummary))
            }
            guard !payload.shouldBlock else {
                state = .blocked(BundleRuntimeIssue(
                    code: .unsupportedVersion,
                    severity: .error,
                    message: payload.message,
                    context: payload.downloadURL
                ))
                operationObserver.finish()
                return
            }
            pendingOperation = PendingReadinessOperation(operation: "capabilities")
            operationObserver.finish()
            runPendingOperation()
        } catch {
            operationObserver.finish()
            state = .blocked(contractIssue(operation: "version-check", error: error))
        }
    }

    private func applyCapabilitiesResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(CapabilitiesPayload.self)
            capabilities = payload
            guard event.ok == true else {
                state = .blocked(BundleRuntimeIssue(
                    code: .operationFailed,
                    severity: .error,
                    message: payload.localizedSummary
                ))
                operationObserver.finish()
                return
            }
            pendingOperation = PendingReadinessOperation(operation: "validate-install")
            operationObserver.finish()
            runPendingOperation()
        } catch {
            operationObserver.finish()
            state = .blocked(contractIssue(operation: "capabilities", error: error))
        }
    }

    private func applyValidationResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(InstallValidationPayload.self)
            validation = payload
            guard payload.ok else {
                state = .blocked(BundleRuntimeIssue(
                    code: .installValidationFailed,
                    severity: .error,
                    message: payload.localizedSummary
                ))
                operationObserver.finish()
                return
            }
            operationObserver.finish()
            finishReady(validation: payload)
        } catch {
            operationObserver.finish()
            state = .blocked(contractIssue(operation: "validate-install", error: error))
        }
    }

    private func finishReady(validation: InstallValidationPayload) {
        let summary = AppReadinessSummary(
            runtimeMode: runtimeMode,
            helperVersion: capabilities?.helperVersion ?? "",
            distributionRoot: capabilities?.distributionRoot ?? "",
            validationSummary: validation.localizedSummary,
            validationCounts: validation.counts
        )
        let warnings = issues.filter { $0.severity == .warning }
        state = warnings.isEmpty ? .ready(summary) : .degraded(summary, warnings)
    }

    private func runPendingOperation() {
        guard let pending = pendingOperation, !backend.isRunning else {
            return
        }
        pendingOperation = nil
        if pending.operation == "version-check" {
            state = .checkingVersion
        } else if pending.operation == "capabilities" {
            state = .checkingCapabilities
        } else if pending.operation == "validate-install" {
            state = .validatingInstall
        }
        let activeOperation = ActiveOperation(operation: pending.operation, profileID: nil, context: nil)
        operationObserver.start(activeOperation)
        backend.run(
            operation: pending.operation,
            params: pending.params,
            requestID: activeOperation.id.uuidString
        )
    }

    private func issue(from event: BackendEvent) -> BundleRuntimeIssue {
        let code: BundleRuntimeIssueCode
        switch event.code {
        case "helper_not_found":
            code = .helperMissing
        case "helper_launch_failed":
            code = .helperLaunchFailed
        default:
            code = .operationFailed
        }
        return BundleRuntimeIssue(
            code: code,
            severity: .error,
            message: event.message ?? event.localizedSummary,
            recovery: BackendErrorViewModel(event: event).recovery?.message
        )
    }

    private func contractIssue(operation: String, error: Error) -> BundleRuntimeIssue {
        BundleRuntimeIssue(
            code: .contractDecodeFailed,
            severity: .error,
            message: L10n.format("app_readiness.error.unexpected_payload", operation, error.localizedDescription)
        )
    }

    private func versionMetadataIssue(message: String) -> BundleRuntimeIssue {
        BundleRuntimeIssue(
            code: .versionMetadataUnavailable,
            severity: .warning,
            message: message
        )
    }

    private func normalized(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

private struct PendingReadinessOperation {
    let operation: String
    let params: [String: JSONValue]

    init(operation: String, params: [String: JSONValue] = [:]) {
        self.operation = operation
        self.params = params
    }
}
